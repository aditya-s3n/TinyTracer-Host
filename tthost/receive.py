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

from dataclasses import dataclass, field

from tthost import transport

PIXEL_START = 0x00
ESCAPE = transport.ESCAPE            # 0x03
CHIP_ESCAPED = (0x00, 0x03)          # data bytes the chip escapes in PIXEL frames
PIXEL_DATA_BYTES = 3                 # r, g, b
IMAGE_SIZE = transport.IMAGE_SIZE    # 512, the chip ignores RENDER's size
TOTAL_PIXELS = IMAGE_SIZE * IMAGE_SIZE   # 262,144
DEFAULT_GAP_TIMEOUT = 10.0           # seconds with no byte before the frame is ended

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
    reason: str = END_COMPLETE      # END_COMPLETE, END_TIMEOUT or END_STOPPED
    elapsed: float = 0.0            # seconds from start() to the end of frame

    @property
    def complete(self):
        """True when every expected pixel arrived (bad ones still count as arrived)."""
        pass


# Helpers
def pixel_xy(index, width=IMAGE_SIZE):
    """Return (x, y) for pixel index n: (n mod width, n // width)."""
    pass


def escape_pixel(r, g, b):
    """Build the PIXEL bytes the chip would send for (r, g, b): 0x00 + escaped data.

    Uses transport.escape_bytes with CHIP_ESCAPED. Not used on the live path;
    handy for tests and for a fake chip backend.
    """
    pass


# State machine
class PixelParser:
    """Pure PIXEL state machine. No threads, no I/O, no frame buffer.

    Feed it raw (still escaped) bytes; it returns the events they produce, in order.
    Keeps its place across feed() calls, so bytes can arrive in any chunk sizes.
    """

    def __init__(self, width=IMAGE_SIZE, height=IMAGE_SIZE):
        """Set the frame size and start in WAIT_START with pixel index 0."""
        pass

    def reset(self):
        """Go back to WAIT_START, pixel index 0, empty data buffer (start of a new frame)."""
        pass

    @property
    def index(self):
        """Index of the next pixel to be written (= pixels finished so far, including bad ones)."""
        pass

    @property
    def done(self):
        """True once width * height pixels have been finished."""
        pass

    def feed(self, data):
        """Run every byte in `data` through feed_byte and return the list of events produced.

        Stops consuming once the frame is done; bytes after the last pixel are
        reported as UNEXPECTED_BYTE anomalies.
        """
        pass

    def feed_byte(self, b):
        """Advance the state machine by one byte. Returns a list of 0..2 events.

        WAIT_START: bare 0x00 -> _start_pixel; anything else -> UNEXPECTED_BYTE.
        DATA: 0x03 sets the escape flag; a bare 0x00 -> _short_pixel then _start_pixel;
        any other byte (or any byte after an escape) -> _add_data_byte.
        """
        pass

    def _start_pixel(self):
        """Clear the data buffer and escape flags, go to DATA."""
        pass

    def _add_data_byte(self, b, escaped):
        """Append one data byte; on the 3rd call return _finish_pixel()'s events."""
        pass

    def _finish_pixel(self):
        """Emit a PixelEvent for the current index, advance the index, go to WAIT_START.

        Also emits a RowEvent when the finished pixel was the last one in its row.
        """
        pass

    def _short_pixel(self):
        """A bare 0x00 arrived mid-pixel: emit SHORT_PIXEL for the current index and advance it.

        The index still advances so later pixels keep their place; the bad pixel
        is filled from neighbours later by the image pipeline. Can also emit a
        RowEvent if the bad pixel ended a row.
        """
        pass


# Receiver (thread + frame buffer + logs)
class PixelReceiver:
    """Drains the link on a background thread and fills the frame buffer as pixels arrive.

    `framebuffer` is any object with `pixels` (512 x 512 x 3 numpy array, linear),
    `count` (pixels received) and `bad` (bad-pixel mask or list). Only this
    thread writes it; the live view only reads it.

    Callbacks (all optional, all called on the reader thread, so keep them short):
        on_row(y, pixels, bad)      after every finished row
        on_done(FrameResult)        once, when the frame ends for any reason
    """

    def __init__(self, link, framebuffer, raw_log=None, event_log=None,
                 on_row=None, on_done=None,
                 gap_timeout=DEFAULT_GAP_TIMEOUT, expected_pixels=TOTAL_PIXELS,
                 log_pixels=True):
        """Store the link, frame buffer, logs and callbacks; build a PixelParser. Does not start anything.

        `log_pixels=False` skips the per-pixel event log line (~10 MB per frame).
        """
        pass

    def start(self):
        """Start the reader thread. Must be called before RENDER is sent (design doc 10.1 step 2).

        Records the start time used for row timing, gap detection and FrameResult.elapsed.
        Raises RuntimeError if already running.
        """
        pass

    def stop(self):
        """Ask the reader thread to end the frame now (cancel). Safe to call more than once.

        The frame ends with reason END_STOPPED; whatever arrived so far is kept.
        """
        pass

    def wait(self, timeout=None):
        """Block until the frame ends and return its FrameResult (or None if `timeout` runs out first)."""
        pass

    @property
    def running(self):
        """True while the reader thread is alive."""
        pass

    @property
    def received(self):
        """Pixels finished so far (good + bad)."""
        pass

    @property
    def bad(self):
        """Indices of bad (short / resynced) pixels so far."""
        pass

    def _run(self):
        """Reader thread body.

        Loop: transport.read_bytes(link) -> log the raw chunk (rx) -> parser.feed ->
        _handle_event for each event. Between reads, check for the end of frame
        (_check_gap, parser.done, stop flag). Always calls _finish exactly once.
        """
        pass

    def _handle_event(self, event, timestamp):
        """Dispatch one parser event to _on_pixel, _on_row or _on_anomaly."""
        pass

    def _on_pixel(self, event, timestamp):
        """Write (r, g, b) into framebuffer.pixels[y, x], bump framebuffer.count,
        and log a "pixel" event (index, x, y, r, g, b, esc)."""
        pass

    def _on_row(self, event, timestamp):
        """Log a "row" event with the row number and how long it took, then call on_row."""
        pass

    def _on_anomaly(self, event, timestamp):
        """Log an "anomaly" event; for SHORT_PIXEL also mark the index bad in the frame buffer."""
        pass

    def _check_gap(self, now):
        """Look at the time since the last byte.

        Logs a GAP anomaly once for a long pause that later recovers (e.g. > 1 s),
        and returns True when the pause passes gap_timeout so _run ends the frame
        with END_TIMEOUT (also logged as a TIMEOUT anomaly).
        """
        pass

    def _finish(self, reason):
        """Build the FrameResult, log "frame_end" (pixels, expected, bad), wake wait(), call on_done."""
        pass
