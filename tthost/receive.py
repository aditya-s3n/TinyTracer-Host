"""Pixel receiver: turns the chip's PIXEL byte stream into pixels in the frame buffer.

Message (see design doc, section 10.3 "Receiver"):

    PIXEL   chip -> host  0x00, r, g, b      escape 0x00 0x03

PIXEL messages carry no coordinates, so the receiver's only job is to never
lose its place. Pixels arrive row by row, top to bottom: pixel n goes to
(x, y) = (n mod 512, n // 512).

State machine (one byte at a time, after the raw bytes are logged):

    STATE       ON BYTE                     ACTION
    WAIT_START  bare 0x00                   start a pixel, go to DATA
    WAIT_START  anything else               log "unexpected_byte", stay
    DATA        0x03                        next byte is data, whatever its value
    DATA        bare 0x00 before 3 data     short pixel: mark index bad, log a resync,
                                            start a new pixel
    DATA        3rd data byte               write pixel n, log it, n += 1, go to
                                            WAIT_START; every 512th pixel report a row

End of frame: 262,144 pixels, no byte for `gap_timeout` seconds (default 10 s),
or stop(). A short frame is reported as an error and kept as far as it got.

Split in two so the state machine can be tested without threads:

    PixelParser    pure state machine: bytes in, PixelEvent / RowEvent / AnomalyEvent out
    PixelReceiver  owns the serial reader thread, feeds the parser, writes the
                   frame buffer and the logs, and reports rows / end of frame

Call rule: this module imports only `transport` (for reads and logging). It never
imports the GUI, IPC or run controller; they hear about progress through callbacks.

Known limit: a whole lost pixel (all 4 bytes) shifts every later pixel by one and
nothing in today's protocol can detect it (open HW question, section 16).
"""

import threading
import time
from dataclasses import dataclass, field

from tthost import transport

PIXEL_START = 0x00
ESCAPE = transport.ESCAPE            # 0x03
CHIP_ESCAPED = (0x00, 0x03)          # data bytes the chip escapes in PIXEL frames
PIXEL_DATA_BYTES = 3                 # r, g, b
IMAGE_SIZE = transport.IMAGE_SIZE    # 512, the chip ignores RENDER's size
TOTAL_PIXELS = IMAGE_SIZE * IMAGE_SIZE   # 262,144
DEFAULT_GAP_TIMEOUT = 10.0           # seconds with no byte before the frame is ended
GAP_WARN_S = 1.0                     # a pause longer than this is logged as a "gap" anomaly

# Parser states
WAIT_START = "wait_start"
DATA = "data"

# Anomaly kinds (event log "anomaly" events)
SHORT_PIXEL = "short_pixel"
UNEXPECTED_BYTE = "unexpected_byte"
GAP = "gap"
TIMEOUT = "timeout"

# End-of-frame reasons
END_COMPLETE = "complete"   # all expected pixels arrived
END_TIMEOUT = "timeout"     # no byte for gap_timeout seconds
END_STOPPED = "stopped"     # stop() was called (cancel)
END_ERROR = "error"         # the link raised while reading (port unplugged, sim died)


# Events produced by the parser
@dataclass
class PixelEvent:
    """One finished pixel: index n, its (x, y), linear r/g/b 0-255, and whether any byte was escaped."""

    index: int
    x: int
    y: int
    r: int
    g: int
    b: int
    escaped: bool


@dataclass
class RowEvent:
    """Row y just finished (fired after every 512th pixel)."""

    y: int


@dataclass
class AnomalyEvent:
    """Something off in the stream.

    kind is one of SHORT_PIXEL, UNEXPECTED_BYTE. For SHORT_PIXEL `index` is the
    pixel marked bad and `got` is how many data bytes arrived before the resync;
    for UNEXPECTED_BYTE `byte` is the stray value.
    """

    kind: str
    index: int | None = None
    got: int | None = None
    byte: int | None = None


@dataclass
class FrameResult:
    """What PixelReceiver.wait() returns once the frame has ended."""

    pixels: int                     # pixels written (good + bad)
    expected: int                   # usually TOTAL_PIXELS
    bad: list = field(default_factory=list)   # indices of short / resynced pixels
    reason: str = END_COMPLETE      # END_COMPLETE, END_TIMEOUT, END_STOPPED or END_ERROR
    elapsed: float = 0.0            # seconds from start() to the end of frame
    error: str | None = None        # the link error message when reason is END_ERROR

    @property
    def complete(self):
        """True when every expected pixel arrived (bad ones still count as arrived)."""
        return self.pixels >= self.expected


# Helpers
def pixel_xy(index, width=IMAGE_SIZE):
    """Return (x, y) for pixel index n: (n mod width, n // width)."""
    return index % width, index // width


def escape_pixel(r, g, b):
    """Build the PIXEL bytes the chip would send for (r, g, b): 0x00 + escaped data.

    Uses transport.escape_bytes with CHIP_ESCAPED. Not used on the live path;
    handy for tests and for a fake chip backend.
    """
    return bytes([PIXEL_START]) + transport.escape_bytes(bytes([r, g, b]), CHIP_ESCAPED)


# State machine
class PixelParser:
    """Pure PIXEL state machine. No threads, no I/O, no frame buffer.

    Feed it raw (still escaped) bytes; it returns the events they produce, in order.
    Keeps its place across feed() calls, so bytes can arrive in any chunk sizes.
    `total` defaults to width * height; set it lower to end a frame early (tests).
    """

    def __init__(self, width=IMAGE_SIZE, height=IMAGE_SIZE, total=None):
        """Set the frame size and start in WAIT_START with pixel index 0."""
        self.width = width
        self.height = height
        self.total = width * height if total is None else total

        self.reset()

    def reset(self):
        """Go back to WAIT_START, pixel index 0, empty data buffer (start of a new frame)."""
        self.state = WAIT_START
        self._index = 0
        self._data = []
        self._escape_next = False
        self._any_escaped = False

    @property
    def index(self):
        """Index of the next pixel to be written (= pixels finished so far, including bad ones)."""
        return self._index

    @property
    def done(self):
        """True once `total` pixels have been finished."""
        return self._index >= self.total

    def feed(self, data):
        """Run every byte in `data` through feed_byte and return the list of events produced.

        Once the frame is done, further bytes are reported as UNEXPECTED_BYTE
        anomalies and do not write pixels.
        """
        events = []

        for b in data:
            events.extend(self.feed_byte(b))

        return events

    def feed_byte(self, b):
        """Advance the state machine by one byte. Returns a list of 0..2 events.

        WAIT_START: bare 0x00 -> _start_pixel; anything else -> UNEXPECTED_BYTE.
        DATA: 0x03 sets the escape flag; a bare 0x00 -> _short_pixel then _start_pixel;
        any other byte (or any byte after an escape) -> _add_data_byte.
        """
        if self.done:
            return [AnomalyEvent(UNEXPECTED_BYTE, byte=b)]

        if self.state == WAIT_START:
            if b == PIXEL_START:
                self._start_pixel()
                return []
            return [AnomalyEvent(UNEXPECTED_BYTE, byte=b)]

        # DATA
        if self._escape_next:
            self._escape_next = False
            return self._add_data_byte(b, escaped=True)

        if b == ESCAPE:
            self._escape_next = True
            return []

        if b == PIXEL_START:
            events = self._short_pixel()
            if not self.done:
                self._start_pixel()
            return events

        return self._add_data_byte(b, escaped=False)

    def _start_pixel(self):
        """Clear the data buffer and escape flags, go to DATA."""
        self._data = []
        self._escape_next = False
        self._any_escaped = False
        self.state = DATA

    def _add_data_byte(self, b, escaped):
        """Append one data byte; on the 3rd call return _finish_pixel()'s events."""
        self._data.append(b)
        self._any_escaped = self._any_escaped or escaped

        if len(self._data) == PIXEL_DATA_BYTES:
            return self._finish_pixel()

        return []

    def _advance(self):
        """Move to the next pixel index, go to WAIT_START, and return a RowEvent if a row just ended."""
        y = self._index // self.width
        self._index += 1
        self.state = WAIT_START
        self._data = []
        self._escape_next = False
        self._any_escaped = False

        if self._index % self.width == 0:
            return [RowEvent(y)]

        return []

    def _finish_pixel(self):
        """Emit a PixelEvent for the current index, advance the index, go to WAIT_START.

        Also emits a RowEvent when the finished pixel was the last one in its row.
        """
        x, y = pixel_xy(self._index, self.width)
        r, g, b = self._data
        event = PixelEvent(self._index, x, y, r, g, b, self._any_escaped)

        return [event, *self._advance()]

    def _short_pixel(self):
        """A bare 0x00 arrived mid-pixel: emit SHORT_PIXEL for the current index and advance it.

        The index still advances so later pixels keep their place; the bad pixel
        is filled from neighbours later by the image pipeline. Can also emit a
        RowEvent if the bad pixel ended a row.
        """
        event = AnomalyEvent(SHORT_PIXEL, index=self._index, got=len(self._data))

        return [event, *self._advance()]


# Receiver (thread + frame buffer + logs)
class PixelReceiver:
    """Drains the link on a background thread and fills the frame buffer as pixels arrive.

    `framebuffer` is any object with `pixels` (512 x 512 x 3 numpy array, linear),
    `count` (pixels received) and `bad` (512 x 512 bool mask). Only this
    thread writes it; the live view only reads it.

    Callbacks (all optional, all called on the reader thread, so keep them short):
        on_row(y, pixels, bad)      after every finished row (pixels so far, bad count)
        on_done(FrameResult)        once, when the frame ends for any reason
    """

    def __init__(self, link, framebuffer, raw_log=None, event_log=None,
                 on_row=None, on_done=None,
                 gap_timeout=DEFAULT_GAP_TIMEOUT, expected_pixels=TOTAL_PIXELS,
                 log_pixels=True):
        """Store the link, frame buffer, logs and callbacks; build a PixelParser. Does not start anything.

        `log_pixels=False` skips the per-pixel event log line (~10 MB per frame).
        """
        self.link = link
        self.fb = framebuffer
        self.raw_log = raw_log
        self.event_log = event_log
        self.on_row = on_row
        self.on_done = on_done
        self.gap_timeout = gap_timeout
        self.log_pixels = log_pixels
        self.parser = PixelParser(total=expected_pixels)

        self._thread = None
        self._stop = threading.Event()
        self._finished = threading.Event()
        self._result = None
        self._bad = []
        self._start_time = None
        self._last_byte_time = None
        self._row_start = None

    def start(self):
        """Start the reader thread. Must be called before RENDER is sent (design doc 10.1 step 2).

        Records the start time used for row timing, gap detection and FrameResult.elapsed.
        Raises RuntimeError if already started; make a new receiver for each frame.
        """
        if self._thread is not None:
            raise RuntimeError("receiver already started; use a new PixelReceiver per frame")

        now = time.monotonic()
        self._start_time = now
        self._last_byte_time = now
        self._row_start = now

        self._thread = threading.Thread(target=self._run, name="pixel-receiver", daemon=True)
        self._thread.start()

    def stop(self):
        """Ask the reader thread to end the frame now (cancel). Safe to call more than once.

        The frame ends with reason END_STOPPED; whatever arrived so far is kept.
        """
        self._stop.set()

    def wait(self, timeout=None):
        """Block until the frame ends and return its FrameResult (or None if `timeout` runs out first)."""
        if self._thread is None:
            return None

        if self._finished.wait(timeout):
            return self._result

        return None

    @property
    def running(self):
        """True while the reader thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def received(self):
        """Pixels finished so far (good + bad)."""
        return self.parser.index

    @property
    def bad(self):
        """Indices of bad (short / resynced) pixels so far."""
        return list(self._bad)

    def _run(self):
        """Reader thread body.

        Loop: transport.read_bytes(link) -> log the raw chunk (rx) -> parser.feed ->
        _handle_event for each event. Between reads, check for the end of frame
        (parser.done, stop flag, _check_gap). Always calls _finish exactly once.
        """
        reason = END_STOPPED
        error = None

        try:
            while True:
                if self._stop.is_set():
                    reason = END_STOPPED
                    break

                chunk = transport.read_bytes(self.link)
                now = time.monotonic()

                if chunk:
                    timestamp = time.time()
                    transport.log_raw(self.raw_log, "rx", chunk, timestamp)
                    self._note_bytes(now, timestamp)

                    for event in self.parser.feed(chunk):
                        self._handle_event(event, timestamp)

                    if self.parser.done:
                        reason = END_COMPLETE
                        break

                elif self._check_gap(now):
                    reason = END_TIMEOUT
                    break

        except Exception as e:  # link died mid-frame: keep what we have and report it
            reason = END_ERROR
            error = f"{type(e).__name__}: {e}"
            transport.log_event(self.event_log, "anomaly", kind="link_error", message=error)

        finally:
            self._finish(reason, error)

    def _handle_event(self, event, timestamp):
        """Dispatch one parser event to _on_pixel, _on_row or _on_anomaly."""
        if isinstance(event, PixelEvent):
            self._on_pixel(event, timestamp)
        elif isinstance(event, RowEvent):
            self._on_row(event, timestamp)
        else:
            self._on_anomaly(event, timestamp)

    def _on_pixel(self, event, timestamp):
        """Write (r, g, b) into framebuffer.pixels[y, x], bump framebuffer.count,
        and log a "pixel" event (index, x, y, r, g, b, esc)."""
        self.fb.pixels[event.y, event.x] = (event.r, event.g, event.b)
        self.fb.count = event.index + 1

        if self.log_pixels:
            transport.log_event(self.event_log, "pixel", time=timestamp, i=event.index,
                                x=event.x, y=event.y, r=event.r, g=event.g, b=event.b,
                                esc=event.escaped)

    def _on_row(self, event, timestamp):
        """Log a "row" event with the row number and how long it took, then call on_row."""
        now = time.monotonic()
        ms = (now - self._row_start) * 1000.0
        self._row_start = now

        transport.log_event(self.event_log, "row", time=timestamp, y=event.y, ms=round(ms, 1))

        if self.on_row is not None:
            # parser.index may already be further on (the whole chunk is parsed
            # before events are handled), so report where this row ended
            self.on_row(event.y, (event.y + 1) * self.parser.width, len(self._bad))

    def _on_anomaly(self, event, timestamp):
        """Log an "anomaly" event; for SHORT_PIXEL also mark the index bad in the frame buffer."""
        fields = {k: v for k, v in (("i", event.index), ("got", event.got), ("byte", event.byte))
                  if v is not None}

        if event.kind == SHORT_PIXEL:
            fields["action"] = "resync"
            self._bad.append(event.index)
            x, y = pixel_xy(event.index, self.parser.width)
            self.fb.bad[y, x] = True
            self.fb.count = event.index + 1

        transport.log_event(self.event_log, "anomaly", time=timestamp, kind=event.kind, **fields)

    def _note_bytes(self, now, timestamp):
        """Bytes just arrived: log a GAP anomaly if the pause before them was longer than GAP_WARN_S."""
        gap = now - self._last_byte_time
        if gap > GAP_WARN_S and self.parser.index > 0:
            transport.log_event(self.event_log, "anomaly", time=timestamp, kind=GAP,
                                after_i=self.parser.index - 1, gap_ms=round(gap * 1000.0, 1))

        self._last_byte_time = now

    def _check_gap(self, now):
        """No bytes this read: return True (and log a TIMEOUT anomaly) once the pause passes gap_timeout."""
        gap = now - self._last_byte_time
        if gap >= self.gap_timeout:
            transport.log_event(self.event_log, "anomaly", kind=TIMEOUT,
                                after_i=self.parser.index - 1, gap_ms=round(gap * 1000.0, 1))
            return True

        return False

    def _finish(self, reason, error=None):
        """Build the FrameResult, log "frame_end" (pixels, expected, bad), wake wait(), call on_done."""
        result = FrameResult(
            pixels=self.parser.index,
            expected=self.parser.total,
            bad=list(self._bad),
            reason=reason,
            elapsed=time.monotonic() - self._start_time,
            error=error,
        )

        self._result = result

        try:
            transport.log_event(self.event_log, "frame_end", pixels=result.pixels,
                                expected=result.expected, bad=len(result.bad), reason=reason)
        finally:
            self._finished.set()

        if self.on_done is not None:
            self.on_done(result)
