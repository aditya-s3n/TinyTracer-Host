"""UART transport: host -> chip framing and sending over pyserial.

Messages (see design doc, "UART transport"):
    OBJECT  host -> chip  0x01, word lo, word hi, addr lo, addr hi    escape 0x00 0x01 0x03
    RENDER  host -> chip  0x00, width lo, width hi, height lo, height hi  escape 0x00 0x01 0x03
    PIXEL   chip -> host  0x00, r, g, b                                escape 0x00 0x03

An escape is 0x03 sent before the data byte. PIXEL frames are parsed by the
pixel receiver; this module only owns the link and the outgoing frames.
"""

OBJECT_START = 0x01
RENDER_START = 0x00
ESCAPE = 0x03
HOST_ESCAPED = (0x00, 0x01, 0x03)   # data bytes escaped in host -> chip frames
DEFAULT_BAUD = 115_200
IMAGE_SIZE = 512


# ---------------------------------------------------------------------------
# Link / backends
# ---------------------------------------------------------------------------

def open_link(backend, port=None, baud=DEFAULT_BAUD, timeout=1.0):
    """Open a byte link to the selected backend and return it.

    Backends: "chip" (real serial port), "sim" (C++ software model as a
    subprocess), "rtl" (Verilator/cocotb behind a pseudo-terminal),
    "replay" (plays back a raw byte log).
    """
    pass


def open_serial(port, baud, timeout):
    """Open a pyserial port to the TinyTracer chip / USB-serial adapter."""
    pass


def open_sim(sim_path):
    """Start sim/tinytracer_sim.cpp as a subprocess and wrap its stdin/stdout as a link."""
    pass


def open_rtl_pty(pty_path, baud):
    """Attach to the RTL simulation's pseudo-terminal, which speaks the same UART bytes as the chip."""
    pass


def open_replay(log_path, realtime=True):
    """Open a raw byte log as a link that replays received bytes at original timing or as fast as possible."""
    pass


def close_link(link):
    """Flush and close the link (and stop the sim subprocess if there is one)."""
    pass


def list_ports():
    """List available serial ports so the CLI can show or auto-pick one."""
    pass


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------

def escape_bytes(data, escaped=HOST_ESCAPED):
    """Insert 0x03 before every data byte that is in the escaped set."""
    pass


def split_u16(value):
    """Split a 16-bit value into (low byte, high byte)."""
    pass


def frame_object(word, address):
    """Build an OBJECT frame: 0x01 + escaped word lo/hi + address lo/hi."""
    pass


def frame_render(width=IMAGE_SIZE, height=IMAGE_SIZE):
    """Build a RENDER frame: 0x00 + escaped width lo/hi + height lo/hi."""
    pass


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def write_frame(link, frame, raw_log=None):
    """Write one frame to the link and record every byte sent (with timestamp) in the raw byte log."""
    pass


def send_object(link, word, address, raw_log=None, event_log=None):
    """Send one SRAM word to an address and log the OBJECT message."""
    pass


def upload_scene(link, words, raw_log=None, event_log=None):
    """Send the full SRAM image as one OBJECT message per word (at most 512)."""
    pass


def diff_words(previous, current):
    """Return the (address, word) pairs that differ between the last image sent and the new one."""
    pass


def upload_delta(link, previous, current, raw_log=None, event_log=None):
    """Delta upload: send only the words that changed since the last upload.

    Camera turns/height changes touch up to ~10 words; sideways moves shift
    every object because the chip camera is fixed at x = 0, y = 0.
    Returns the number of words sent.
    """
    pass


def start_render(link, width=IMAGE_SIZE, height=IMAGE_SIZE, raw_log=None, event_log=None):
    """Send the RENDER message; its timestamp marks the start of the render in the event log."""
    pass


def estimate_upload_time(n_words, baud=DEFAULT_BAUD):
    """Estimate upload time in seconds for n OBJECT messages (5 bytes each before escapes, 10 bits/byte)."""
    pass


# ---------------------------------------------------------------------------
# Receiving (raw bytes only; the pixel receiver does PIXEL parsing)
# ---------------------------------------------------------------------------

def read_bytes(link, max_bytes=4096):
    """Read whatever bytes are available from the link (non-blocking up to the link timeout)."""
    pass


def reader_thread(link, out_queue, stop_event, raw_log=None):
    """Background loop that drains the link into a queue, timestamping and raw-logging each chunk.

    The pixel receiver's state machine consumes this queue.
    """
    pass
