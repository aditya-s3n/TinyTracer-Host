"""UART transport: host -> chip framing and sending over pyserial.

Messages (see design doc, "UART transport"):

    OBJECT  host -> chip  0x01, word lo, word hi, addr lo, addr hi        escape 0x00 0x01 0x03
    RENDER  host -> chip  0x00, width lo, width hi, height lo, height hi  escape 0x00 0x01 0x03
    PIXEL   chip -> host  0x00, r, g, b                                    escape 0x00 0x03

An escape is 0x03 sent before the data byte. Start bytes are never escaped.
PIXEL frames are parsed by the pixel receiver; this module only owns the link
and the outgoing frames.

A link is any object with the pyserial calls `write`, `read`, `in_waiting`,
`flush` and `close`. `open_link` returns one for each backend.

Logs (see design doc, "Render log"). Both are open text files, flushed after
every write so an interrupted run keeps everything up to that point:

    raw byte log  one line per chunk: "<unix time> <tx|rx> <hex bytes>"
                  e.g. "1760000000.123456 tx 01 34 12 56 03 00"
    event log     one JSON object per line, e.g.
                  {"event": "object", "time": 1760000000.12, "address": 86, "word": 4660}
"""

import json
import os
import select
import subprocess
import time

OBJECT_START = 0x01
RENDER_START = 0x00
ESCAPE = 0x03
HOST_ESCAPED = (0x00, 0x01, 0x03)   # data bytes escaped in host -> chip frames
DEFAULT_BAUD = 115_200
IMAGE_SIZE = 512
SRAM_WORDS = 512 # an upload is at most one OBJECT message per SRAM word
BYTES_PER_OBJECT = 5  # OBJECT message size before escapes
BITS_PER_BYTE = 10 # 8N1: start bit + 8 data bits + stop bit
BACKENDS = ("chip", "sim", "rtl", "replay")


# Logging
def log_raw(raw_log, direction, data, timestamp=None):
    """Append one chunk to the raw byte log as "<time> <tx|rx> <hex bytes>".

    Does nothing when `raw_log` is None or `data` is empty.
    """
    if raw_log is None or not data:
        return
    if timestamp is None:
        timestamp = time.time()
    raw_log.write(f"{timestamp:.6f} {direction} {bytes(data).hex(' ')}\n")
    raw_log.flush()


def log_event(event_log, event, **fields):
    """Append one JSON object to the event log: {"event": ..., "time": ..., **fields}.

    Does nothing when `event_log` is None.
    """
    if event_log is None:
        return
    record = {"event": event, "time": fields.pop("time", time.time()), **fields}
    event_log.write(json.dumps(record) + "\n")
    event_log.flush()


def read_raw_log(log_path, direction="rx"):
    """Read a raw byte log and return its (timestamp, bytes) chunks for one direction."""
    chunks = []
    with open(log_path) as f:
        for line in f:
            parts = line.split(maxsplit=2)

            if len(parts) == 3 and parts[1] == direction:
                chunks.append((float(parts[0]), bytes.fromhex(parts[2])))


    return chunks


# Link / backends
class SimLink:
    """Link to the C++ software model (`sim/tinytracer_sim.cpp`) running as a subprocess.

    Bytes written go to the sim's stdin; bytes read come from its stdout.
    `read` waits up to `timeout` seconds, like a pyserial port. POSIX only.
    """

    def __init__(self, sim_path, timeout=1.0):
        self.timeout = timeout
        self.proc = subprocess.Popen([sim_path], stdin=subprocess.PIPE, stdout=subprocess.PIPE, bufsize=0)
        self._fd = self.proc.stdout.fileno()
        os.set_blocking(self._fd, False)
        self._buffer = bytearray()

    def _fill(self, timeout):
        ready, _, _ = select.select([self._fd], [], [], timeout)
        if ready:

            try:
                self._buffer.extend(os.read(self._fd, 65536))
            except BlockingIOError:
                pass

    @property
    def in_waiting(self):
        """Number of bytes that can be read without waiting."""
        self._fill(0)
        return len(self._buffer)

    def write(self, data):
        """Send bytes to the sim's stdin. Returns the number of bytes written."""
        self.proc.stdin.write(data)
        return len(data)

    def read(self, size=1):
        """Read up to `size` bytes from the sim's stdout, waiting up to `timeout` if none are buffered."""
        if not self._buffer:
            self._fill(self.timeout)

        chunk = bytes(self._buffer[:size])
        del self._buffer[:size]
        return chunk

    def flush(self):
        """Push any buffered bytes through to the sim's stdin."""
        self.proc.stdin.flush()

    def close(self):
        """Close the pipes and stop the sim subprocess."""
        if self.proc.poll() is None:
            self.proc.stdin.close()

            try:
                self.proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()

        self.proc.stdout.close()


class ReplayLink:
    """Link that plays back the received (rx) bytes of a raw byte log as if they came from the chip.

    With `realtime=True` each chunk becomes readable at its original offset
    from the first chunk; otherwise everything is readable at once. Writes
    are accepted and discarded.
    """

    def __init__(self, chunks, realtime=True, timeout=1.0):
        self.timeout = timeout
        self.realtime = realtime
        self._chunks = list(chunks)
        self._t0 = self._chunks[0][0] if self._chunks else 0.0
        self._start = time.monotonic()
        self._next = 0
        self._buffer = bytearray()
        self.closed = False

    def _check_open(self):
        if self.closed:
            raise ValueError("replay link is closed")

    def _release(self):
        elapsed = time.monotonic() - self._start

        while self._next < len(self._chunks):
            ts, data = self._chunks[self._next]
            if self.realtime and ts - self._t0 > elapsed:
                break
            
            self._buffer.extend(data)
            self._next += 1

    def _seconds_to_next(self):

        if self._next >= len(self._chunks):
            return None
        
        due = self._chunks[self._next][0] - self._t0
        return max(0.0, due - (time.monotonic() - self._start))

    @property
    def done(self):
        """True once every chunk in the log has been read."""
        self._release()

        return self._next >= len(self._chunks) and not self._buffer

    @property
    def in_waiting(self):
        """Number of bytes that can be read without waiting."""
        self._release()

        return len(self._buffer)

    def write(self, data):
        """Accept and discard bytes; the recording already holds the chip's replies."""
        self._check_open()

        return len(data)

    def read(self, size=1):
        """Read up to `size` replayed bytes, waiting for the next chunk's due time or `timeout`."""
        self._check_open()
        self._release()

        if not self._buffer:
            wait = self._seconds_to_next()
            time.sleep(self.timeout if wait is None else min(wait, self.timeout))
            self._release()
        chunk = bytes(self._buffer[:size])

        del self._buffer[:size]
        return chunk

    def close(self):
        """Drop the remaining chunks and buffer; later reads and writes raise ValueError."""
        self.closed = True
        self._chunks = []
        self._buffer.clear()


def _require_pyserial():
    try:
        import serial
    except ImportError as e:
        raise ImportError("pyserial is required for this backend: pip install pyserial") from e
    return serial


def open_link(backend, port=None, baud=DEFAULT_BAUD, timeout=1.0):
    """Open a byte link to the selected backend and return it.

    Backends: "chip" (real serial port), "sim" (C++ software model as a
    subprocess), "rtl" (Verilator/cocotb behind a pseudo-terminal),
    "replay" (plays back a raw byte log).

    `port` means the serial port for "chip", the sim executable for "sim",
    the pseudo-terminal path for "rtl" and the raw byte log for "replay".
    Raises ValueError for an unknown backend or a missing `port`.
    """
    if backend not in BACKENDS:
        raise ValueError(f"unknown backend {backend!r}; expected one of {', '.join(BACKENDS)}")
    if port is None:
        raise ValueError(f"backend {backend!r} needs a port/path")
    if backend == "chip":
        return open_serial(port, baud, timeout)
    if backend == "sim":
        return open_sim(port, timeout=timeout)
    if backend == "rtl":
        return open_rtl_pty(port, baud, timeout=timeout)
    return open_replay(port, timeout=timeout)


def open_serial(port, baud, timeout):
    """Open a pyserial port to the TinyTracer chip / USB-serial adapter."""
    serial = _require_pyserial()
    return serial.Serial(port, baudrate=baud, timeout=timeout)


def open_sim(sim_path, timeout=1.0):
    """Start sim/tinytracer_sim.cpp as a subprocess and wrap its stdin/stdout as a link."""
    return SimLink(sim_path, timeout=timeout)


def open_rtl_pty(pty_path, baud, timeout=1.0):
    """Attach to the RTL simulation's pseudo-terminal, which speaks the same UART bytes as the chip."""
    serial = _require_pyserial()
    return serial.Serial(pty_path, baudrate=baud, timeout=timeout)


def open_replay(log_path, realtime=True, timeout=1.0):
    """Open a raw byte log as a link that replays received bytes at original timing or as fast as possible."""
    return ReplayLink(read_raw_log(log_path, "rx"), realtime=realtime, timeout=timeout)


def close_link(link):
    """Flush and close the link (and stop the sim subprocess if there is one)."""
    try:
        link.flush()
    finally:
        link.close()


def list_ports():
    """List available serial ports so the CLI can show or auto-pick one.

    Returns a list of (device, description) tuples, e.g. ("/dev/ttyUSB0", "FT232R USB UART").
    """
    _require_pyserial()
    from serial.tools import list_ports as lp
    return [(p.device, p.description) for p in sorted(lp.comports(), key=lambda p: p.device)]


# Framing
def escape_bytes(data, escaped=HOST_ESCAPED):
    """Insert 0x03 before every data byte that is in the escaped set."""
    out = bytearray()
    for b in data:

        if b in escaped:
            out.append(ESCAPE)
        out.append(b)
        
    return bytes(out)


def split_u16(value):
    """Split a 16-bit value into (low byte, high byte). Raises ValueError outside 0..0xFFFF."""
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"{value} does not fit in 16 bits")
    return value & 0xFF, value >> 8


def frame_object(word, address):
    """Build an OBJECT frame: 0x01 + escaped word lo/hi + address lo/hi."""
    payload = bytes([*split_u16(word), *split_u16(address)])

    return bytes([OBJECT_START]) + escape_bytes(payload)


def frame_render(width=IMAGE_SIZE, height=IMAGE_SIZE):
    """Build a RENDER frame: 0x00 + escaped width lo/hi + height lo/hi."""
    payload = bytes([*split_u16(width), *split_u16(height)])

    return bytes([RENDER_START]) + escape_bytes(payload)


# Sending
def write_frame(link, frame, raw_log=None):
    """Write one frame to the link and record every byte sent (with timestamp) in the raw byte log."""
    link.write(frame)
    log_raw(raw_log, "tx", frame)


def send_object(link, word, address, raw_log=None, event_log=None):
    """Send one SRAM word to an address and log the OBJECT message."""
    write_frame(link, frame_object(word, address), raw_log)
    log_event(event_log, "object", address=address, word=word)


def upload_scene(link, words, raw_log=None, event_log=None):
    """Send the full SRAM image as one OBJECT message per word (at most 512).

    Word i goes to address i. Raises ValueError before sending anything if
    there are more than 512 words.
    """
    if len(words) > SRAM_WORDS:
        raise ValueError(f"scene has {len(words)} words; SRAM holds {SRAM_WORDS}")
    
    for address, word in enumerate(words):
        send_object(link, word, address, raw_log, event_log)


def diff_words(previous, current):
    """Return the (address, word) pairs that differ between the last image sent and the new one.

    With no previous image (None) every word counts as changed. Addresses
    past the end of `previous` are treated as changed.
    """
    if previous is None:
        previous = []

    changed = []
    for address, word in enumerate(current):
        is_new = address >= len(previous)

        if is_new or previous[address] != word:
            changed.append((address, word))

    return changed


def upload_delta(link, previous, current, raw_log=None, event_log=None):
    """Delta upload: send only the words that changed since the last upload.

    Camera turns/height changes touch up to ~10 words; sideways moves shift
    every object because the chip camera is fixed at x = 0, y = 0.
    Returns the number of words sent.
    """
    if len(current) > SRAM_WORDS:
        raise ValueError(f"scene has {len(current)} words; SRAM holds {SRAM_WORDS}")
    
    changed = diff_words(previous, current)


    for address, word in changed:
        send_object(link, word, address, raw_log, event_log)

    return len(changed)


def start_render(link, width=IMAGE_SIZE, height=IMAGE_SIZE, raw_log=None, event_log=None):
    """Send the RENDER message; its timestamp marks the start of the render in the event log."""
    write_frame(link, frame_render(width, height), raw_log)
    log_event(event_log, "render", width=width, height=height)


def estimate_upload_time(n_words, baud=DEFAULT_BAUD):
    """Estimate upload time in seconds for n OBJECT messages (5 bytes each before escapes, 10 bits/byte)."""
    return n_words * BYTES_PER_OBJECT * BITS_PER_BYTE / baud


# Receiving (raw bytes only; the pixel receiver does PIXEL parsing)
def read_bytes(link, max_bytes=4096):
    """Read whatever bytes are available from the link (non-blocking up to the link timeout).

    Returns the bytes as received, still escaped. Empty if nothing arrived
    before the timeout.
    """
    waiting = link.in_waiting
    data = bytes(link.read(min(max(waiting, 1), max_bytes)))
    if data and not waiting:
        # Woke up on the first byte; take whatever arrived with it.
        more = min(link.in_waiting, max_bytes - len(data))
        
        if more:
            data += bytes(link.read(more))

    return data


def reader_thread(link, out_queue, stop_event, raw_log=None):
    """Background loop that drains the link into a queue, timestamping and raw-logging each chunk.

    Each queue item is a (unix time, bytes) tuple. Runs until `stop_event`
    is set. The pixel receiver's state machine consumes this queue.
    """
    while not stop_event.is_set():
        chunk = read_bytes(link)

        if chunk:
            timestamp = time.time()
            log_raw(raw_log, "rx", chunk, timestamp)
            out_queue.put((timestamp, chunk))
