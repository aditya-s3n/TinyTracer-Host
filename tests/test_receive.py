"""Tests for tthost.receive: PIXEL parsing, resyncs, and the receiver thread.

Expected behaviour comes from the design doc, section 10.3 "Receiver":
    PIXEL  0x00, r, g, b      chip escapes 0x00 and 0x03 with a 0x03 in front
    pixel n -> (n mod 512, n // 512); a row is reported after every 512th pixel
    bare 0x00 mid-pixel -> short pixel, index marked bad, resync
    no byte for gap_timeout -> frame ends as a timeout, partial frame kept

Section 14 "Receiver faults": feed byte streams with a dropped byte, a stray
byte, a truncated frame and a long gap; check counts, resyncs and anomaly events.

RUN from root:  python -m pytest tests
"""

import pytest

from tthost import receive as r  # noqa: F401

# Skeleton: remove this line as the tests get filled in.
pytestmark = pytest.mark.skip(reason="skeleton: receive tests not written yet")


# Fakes / helpers
class FakeFrameBuffer:
    """Minimal frame buffer: `pixels` (512 x 512 x 3 numpy array), `count`, `bad`."""

    def __init__(self, width=r.IMAGE_SIZE, height=r.IMAGE_SIZE):
        pass


def pixel_stream(pixels):
    """Build the escaped chip byte stream for a list of (r, g, b) tuples, using r.escape_pixel."""
    pass


# Constants
def test_constants_match_design_doc():
    """PIXEL_START 0x00, ESCAPE 0x03, CHIP_ESCAPED {0x00, 0x03}, TOTAL_PIXELS 262,144, gap timeout 10 s."""
    pass


# pixel_xy / escape_pixel
@pytest.mark.parametrize("index, expected", [
    (0, (0, 0)), (511, (511, 0)), (512, (0, 1)), (262_143, (511, 511)),
])
def test_pixel_xy(index, expected):
    """Pixel n lands at (n mod 512, n // 512)."""
    pass


def test_escape_pixel_plain():
    """(0x8C, 0x2E, 0x34) -> 00 8C 2E 34, no escapes (raw byte log example, section 12.2)."""
    pass


def test_escape_pixel_doc_example():
    """R=0, G=3, B=200 -> 00 03 00 03 03 C8 (section 4.2 example)."""
    pass


def test_escape_pixel_does_not_escape_0x01():
    """Chip -> host only escapes 0x00 and 0x03, so 0x01 goes through bare."""
    pass


# PixelParser: normal pixels
def test_parser_single_pixel():
    """One plain PIXEL frame -> one PixelEvent with index 0, (0, 0), the right r/g/b, escaped=False."""
    pass


def test_parser_escaped_pixel_values():
    """Escaped 0x00 and 0x03 data bytes decode to their real values and set escaped=True."""
    pass


def test_parser_every_byte_value_round_trips():
    """Every value 0x00..0xFF in each of r, g, b decodes back to itself."""
    pass


def test_parser_handles_any_chunk_split():
    """Feeding the same stream one byte at a time, in random chunks, or all at once gives the same events."""
    pass


def test_parser_escape_split_across_chunks():
    """A 0x03 at the end of one chunk still escapes the first byte of the next chunk."""
    pass


def test_parser_row_event_every_512_pixels():
    """A RowEvent for y follows pixel 511 of row y, and not before."""
    pass


def test_parser_done_after_full_frame():
    """After 262,144 pixels `done` is True and `index` is 262,144."""
    pass


def test_parser_reset_starts_new_frame():
    """reset() puts the parser back at index 0 in WAIT_START."""
    pass


# PixelParser: faults (section 14)
def test_parser_dropped_data_byte_is_short_pixel():
    """00 r g | 00 r g b -> SHORT_PIXEL (index 0, got 2), then pixel 1 decodes normally."""
    pass


def test_parser_short_pixel_still_advances_index():
    """After a short pixel, later pixels keep their place (index advances past the bad one)."""
    pass


def test_parser_stray_byte_before_start():
    """A non-zero byte in WAIT_START -> UNEXPECTED_BYTE anomaly, parser stays in WAIT_START."""
    pass


def test_parser_bytes_after_full_frame_are_unexpected():
    """Bytes after pixel 262,143 are reported as UNEXPECTED_BYTE and do not write pixels."""
    pass


def test_parser_truncated_stream_leaves_pixel_unfinished():
    """A stream that stops mid-pixel produces no PixelEvent for that pixel and `done` stays False."""
    pass


# PixelReceiver (uses the FakeLink from test_transport style: in-memory bytes)
def test_receiver_fills_frame_buffer():
    """Feed a small stream through a fake link; framebuffer.pixels[y, x] and `count` match what was sent."""
    pass


def test_receiver_calls_on_row_per_row():
    """on_row is called once per finished row with (y, pixels, bad)."""
    pass


def test_receiver_complete_frame_result():
    """With expected_pixels small (e.g. 2 rows), wait() returns FrameResult reason "complete", complete=True."""
    pass


def test_receiver_gap_timeout_ends_frame():
    """With a short gap_timeout and a stream that stops early, wait() returns reason "timeout", partial pixels kept."""
    pass


def test_receiver_stop_ends_frame():
    """stop() while running ends the frame with reason "stopped" and the thread exits."""
    pass


def test_receiver_marks_bad_pixels():
    """A short pixel ends up in FrameResult.bad and in the frame buffer's bad mask."""
    pass


def test_receiver_logs_raw_bytes_and_events():
    """Raw byte log gets the rx chunks before unescaping; event log gets pixel, row, anomaly and frame_end."""
    pass


def test_receiver_start_twice_raises():
    """Calling start() on a running receiver raises RuntimeError."""
    pass
