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

import io
import json
import random

import numpy as np
import pytest

from test_transport import FakeLink
from tthost import receive as r


# Fakes / helpers
class FakeFrameBuffer:
    """Minimal frame buffer: `pixels` (512 x 512 x 3 numpy array), `count`, `bad`."""

    def __init__(self, width=r.IMAGE_SIZE, height=r.IMAGE_SIZE):
        self.pixels = np.zeros((height, width, 3), dtype=np.float32)
        self.bad = np.zeros((height, width), dtype=bool)
        self.count = 0


def pixel_stream(pixels):
    """Build the escaped chip byte stream for a list of (r, g, b) tuples, using r.escape_pixel."""
    return b"".join(r.escape_pixel(*p) for p in pixels)


def make_pixels(n):
    """n deterministic pixels that hit escaped values (0x00, 0x03) regularly."""
    return [(i % 256, (i * 7) % 256, (i * 13) % 256) for i in range(n)]


def only(events, kind):
    return [e for e in events if isinstance(e, kind)]


def run_receiver(stream, expected, gap_timeout=0.5, **kwargs):
    """Feed `stream` through a FakeLink into a receiver and wait for the frame to end."""
    link = FakeLink(incoming=stream)
    fb = FakeFrameBuffer()
    rx = r.PixelReceiver(link, fb, gap_timeout=gap_timeout, expected_pixels=expected, **kwargs)

    rx.start()
    result = rx.wait(timeout=5.0)
    assert result is not None, "receiver did not finish"

    return rx, fb, result


# Constants
def test_constants_match_design_doc():
    """PIXEL_START 0x00, ESCAPE 0x03, CHIP_ESCAPED {0x00, 0x03}, TOTAL_PIXELS 262,144, gap timeout 10 s."""
    assert r.PIXEL_START == 0x00
    assert r.ESCAPE == 0x03
    assert set(r.CHIP_ESCAPED) == {0x00, 0x03}
    assert r.TOTAL_PIXELS == 262_144
    assert r.DEFAULT_GAP_TIMEOUT == 10.0
    assert r.PixelParser().total == r.TOTAL_PIXELS


# pixel_xy / escape_pixel
@pytest.mark.parametrize("index, expected", [
    (0, (0, 0)), (511, (511, 0)), (512, (0, 1)), (262_143, (511, 511)),
])
def test_pixel_xy(index, expected):
    """Pixel n lands at (n mod 512, n // 512)."""
    assert r.pixel_xy(index) == expected


def test_escape_pixel_plain():
    """(0x8C, 0x2E, 0x34) -> 00 8C 2E 34, no escapes (raw byte log example, section 12.2)."""
    assert r.escape_pixel(0x8C, 0x2E, 0x34) == bytes([0x00, 0x8C, 0x2E, 0x34])


def test_escape_pixel_doc_example():
    """R=0, G=3, B=200 -> 00 03 00 03 03 C8 (section 4.2 example)."""
    assert r.escape_pixel(0, 3, 200) == bytes([0x00, 0x03, 0x00, 0x03, 0x03, 0xC8])


def test_escape_pixel_does_not_escape_0x01():
    """Chip -> host only escapes 0x00 and 0x03, so 0x01 goes through bare."""
    assert r.escape_pixel(1, 1, 1) == bytes([0x00, 0x01, 0x01, 0x01])


# PixelParser: normal pixels
def test_parser_single_pixel():
    """One plain PIXEL frame -> one PixelEvent with index 0, (0, 0), the right r/g/b, escaped=False."""
    events = r.PixelParser().feed(r.escape_pixel(140, 46, 52))
    assert events == [r.PixelEvent(0, 0, 0, 140, 46, 52, False)]


def test_parser_escaped_pixel_values():
    """Escaped 0x00 and 0x03 data bytes decode to their real values and set escaped=True."""
    events = r.PixelParser().feed(r.escape_pixel(0, 3, 200))
    assert events == [r.PixelEvent(0, 0, 0, 0, 3, 200, True)]


def test_parser_every_byte_value_round_trips():
    """Every value 0x00..0xFF in each of r, g, b decodes back to itself."""
    pixels = [(v, (v + 1) % 256, (v + 2) % 256) for v in range(256)]
    events = only(r.PixelParser().feed(pixel_stream(pixels)), r.PixelEvent)
    assert [(e.r, e.g, e.b) for e in events] == pixels
    assert [e.index for e in events] == list(range(256))


def test_parser_handles_any_chunk_split():
    """Feeding the same stream one byte at a time, in random chunks, or all at once gives the same events."""
    stream = pixel_stream(make_pixels(600))
    whole = r.PixelParser().feed(stream)

    one_by_one = r.PixelParser()
    bytewise = [e for b in stream for e in one_by_one.feed(bytes([b]))]

    rng = random.Random(0)
    chunked_parser = r.PixelParser()
    chunked, i = [], 0
    while i < len(stream):
        n = rng.randint(1, 9)
        chunked += chunked_parser.feed(stream[i:i + n])
        i += n

    assert bytewise == whole
    assert chunked == whole
    assert len(only(whole, r.PixelEvent)) == 600


def test_parser_escape_split_across_chunks():
    """A 0x03 at the end of one chunk still escapes the first byte of the next chunk."""
    stream = r.escape_pixel(0, 9, 9)          # 00 03 00 09 09
    p = r.PixelParser()

    assert p.feed(stream[:2]) == []           # 00 03
    assert p.feed(stream[2:]) == [r.PixelEvent(0, 0, 0, 0, 9, 9, True)]


def test_parser_row_event_every_512_pixels():
    """A RowEvent for y follows pixel 511 of row y, and not before."""
    p = r.PixelParser()

    first = p.feed(pixel_stream(make_pixels(511)))
    assert only(first, r.RowEvent) == []

    last = p.feed(pixel_stream([(1, 2, 4)]))
    assert last == [r.PixelEvent(511, 511, 0, 1, 2, 4, False), r.RowEvent(0)]

    more = p.feed(pixel_stream(make_pixels(512)))
    assert only(more, r.RowEvent) == [r.RowEvent(1)]
    assert more[-1] == r.RowEvent(1)


def test_parser_done_after_full_frame():
    """After width * height pixels `done` is True and `index` is width * height."""
    p = r.PixelParser(width=4, height=2)

    p.feed(pixel_stream(make_pixels(7)))
    assert not p.done

    p.feed(pixel_stream(make_pixels(1)))
    assert p.done
    assert p.index == 8


def test_parser_reset_starts_new_frame():
    """reset() puts the parser back at index 0 in WAIT_START."""
    p = r.PixelParser()

    p.feed(pixel_stream(make_pixels(5)) + bytes([0x00, 0x10]))   # stop mid-pixel
    assert p.state == r.DATA

    p.reset()
    assert p.index == 0
    assert p.state == r.WAIT_START
    assert p.feed(r.escape_pixel(4, 5, 6)) == [r.PixelEvent(0, 0, 0, 4, 5, 6, False)]


# PixelParser: faults (section 14)
def test_parser_dropped_data_byte_is_short_pixel():
    """00 r g | 00 r g b -> SHORT_PIXEL (index 0, got 2), then pixel 1 decodes normally."""
    stream = bytes([0x00, 0x10, 0x20]) + r.escape_pixel(0x11, 0x22, 0x33)
    events = r.PixelParser().feed(stream)

    assert events == [
        r.AnomalyEvent(r.SHORT_PIXEL, index=0, got=2),
        r.PixelEvent(1, 1, 0, 0x11, 0x22, 0x33, False),
    ]


def test_parser_short_pixel_still_advances_index():
    """After a short pixel, later pixels keep their place (index advances past the bad one)."""
    stream = pixel_stream(make_pixels(3)) + bytes([0x00, 0x10]) + pixel_stream(make_pixels(3))
    p = r.PixelParser()
    events = p.feed(stream)

    assert [e.index for e in only(events, r.PixelEvent)] == [0, 1, 2, 4, 5, 6]
    assert only(events, r.AnomalyEvent) == [r.AnomalyEvent(r.SHORT_PIXEL, index=3, got=1)]
    assert p.index == 7


def test_parser_stray_byte_before_start():
    """A non-zero byte in WAIT_START -> UNEXPECTED_BYTE anomaly, parser stays in WAIT_START."""
    p = r.PixelParser()

    assert p.feed(bytes([0x05])) == [r.AnomalyEvent(r.UNEXPECTED_BYTE, byte=0x05)]
    assert p.state == r.WAIT_START
    assert p.feed(r.escape_pixel(7, 8, 9)) == [r.PixelEvent(0, 0, 0, 7, 8, 9, False)]


def test_parser_bytes_after_full_frame_are_unexpected():
    """Bytes after the last pixel are reported as UNEXPECTED_BYTE and do not write pixels."""
    p = r.PixelParser(width=2, height=1)

    p.feed(pixel_stream(make_pixels(2)))
    extra = p.feed(r.escape_pixel(9, 9, 9))

    assert all(e.kind == r.UNEXPECTED_BYTE for e in extra)
    assert len(extra) == 4
    assert p.index == 2


def test_parser_truncated_stream_leaves_pixel_unfinished():
    """A stream that stops mid-pixel produces no PixelEvent for that pixel and `done` stays False."""
    p = r.PixelParser(width=2, height=1)
    events = p.feed(r.escape_pixel(1, 2, 4) + bytes([0x00, 0x05, 0x06]))

    assert len(only(events, r.PixelEvent)) == 1
    assert p.index == 1
    assert not p.done


# PixelReceiver
def test_receiver_fills_frame_buffer():
    """Feed a small stream through a fake link; framebuffer.pixels[y, x] and `count` match what was sent."""
    pixels = make_pixels(1024)
    _, fb, result = run_receiver(pixel_stream(pixels), expected=1024)

    assert fb.count == 1024
    expected = np.array(pixels, dtype=np.float32).reshape(2, 512, 3)
    assert np.array_equal(fb.pixels[:2], expected)
    assert not fb.pixels[2:].any()
    assert result.pixels == 1024


def test_receiver_calls_on_row_per_row():
    """on_row is called once per finished row with (y, pixels, bad)."""
    rows = []
    run_receiver(pixel_stream(make_pixels(1024)), expected=1024,
                 on_row=lambda y, n, bad: rows.append((y, n, bad)))

    assert rows == [(0, 512, 0), (1, 1024, 0)]


def test_receiver_complete_frame_result():
    """With expected_pixels small (2 rows), wait() returns FrameResult reason "complete", complete=True."""
    done = []
    rx, _, result = run_receiver(pixel_stream(make_pixels(1024)), expected=1024, on_done=done.append)

    assert result.reason == r.END_COMPLETE
    assert result.complete
    assert result.expected == 1024
    assert result.bad == []
    assert done == [result]

    rx._thread.join(1.0)
    assert not rx.running


def test_receiver_gap_timeout_ends_frame():
    """With a short gap_timeout and a stream that stops early, wait() returns reason "timeout", partial pixels kept."""
    _, fb, result = run_receiver(pixel_stream(make_pixels(600)), expected=1024, gap_timeout=0.2)

    assert result.reason == r.END_TIMEOUT
    assert not result.complete
    assert result.pixels == 600
    assert fb.count == 600


def test_receiver_stop_ends_frame():
    """stop() while running ends the frame with reason "stopped" and the thread exits."""
    rx = r.PixelReceiver(FakeLink(), FakeFrameBuffer(), gap_timeout=10.0)
    rx.start()
    assert rx.running

    rx.stop()
    result = rx.wait(timeout=2.0)
    assert result is not None and result.reason == r.END_STOPPED

    rx._thread.join(1.0)
    assert not rx.running


def test_receiver_marks_bad_pixels():
    """A short pixel ends up in FrameResult.bad and in the frame buffer's bad mask."""
    stream = pixel_stream(make_pixels(3)) + bytes([0x00, 0x10]) + pixel_stream(make_pixels(1020))
    _, fb, result = run_receiver(stream, expected=1024)

    assert result.complete
    assert result.bad == [3]
    assert fb.bad[0, 3]
    assert fb.bad.sum() == 1


def test_receiver_logs_raw_bytes_and_events():
    """Raw byte log gets the rx chunks before unescaping; event log gets pixel, row, anomaly and frame_end."""
    raw_log, event_log = io.StringIO(), io.StringIO()
    stream = bytes([0x00, 0x10]) + pixel_stream(make_pixels(1023))
    run_receiver(stream, expected=1024, raw_log=raw_log, event_log=event_log)

    raw_lines = raw_log.getvalue().splitlines()
    assert raw_lines and all(line.split()[1] == "rx" for line in raw_lines)
    logged = b"".join(bytes.fromhex(line.split(maxsplit=2)[2]) for line in raw_lines)
    assert logged == stream

    events = [json.loads(line) for line in event_log.getvalue().splitlines()]
    kinds = {e["event"] for e in events}
    assert {"pixel", "row", "anomaly", "frame_end"} <= kinds
    anomaly = next(e for e in events if e["event"] == "anomaly")
    assert anomaly["kind"] == r.SHORT_PIXEL and anomaly["i"] == 0

    assert events[-1]["event"] == "frame_end"
    assert events[-1]["pixels"] == 1024 and events[-1]["bad"] == 1


def test_receiver_start_twice_raises():
    """Calling start() on a running receiver raises RuntimeError."""
    rx = r.PixelReceiver(FakeLink(), FakeFrameBuffer(), gap_timeout=10.0)
    rx.start()

    try:
        with pytest.raises(RuntimeError):
            rx.start()
    finally:
        rx.stop()
        rx.wait(2.0)
