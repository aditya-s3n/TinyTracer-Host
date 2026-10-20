"""Tests for tthost.transport: framing, sending, and raw byte reads.

Expected bytes come from the design doc, "UART transport":
    OBJECT  0x01, word lo, word hi, addr lo, addr hi      escape 0x00 0x01 0x03
    RENDER  0x00, width lo, width hi, height lo, height hi  escape 0x00 0x01 0x03
An escape is 0x03 sent before the data byte. Start bytes are never escaped.

Links are faked with an in-memory object that mimics the pyserial calls the
transport uses (write / read / in_waiting / flush / close), so no hardware,
sim, or pyserial install is needed.


"""

"""
RUN from root:  python -m pytest tests
"""

import queue
import threading
import time

import pytest

from tthost import transport as t



# Fakes
class FakeLink:
    # Testing the serial link

    def __init__(self, incoming=b""):
        self.sent = bytearray()
        self.writes = []
        self._incoming = bytearray(incoming)
        self._lock = threading.Lock()
        self.closed = False
        self.flushed = False

    def feed(self, data):
        with self._lock:
            self._incoming.extend(data)

    @property
    def in_waiting(self):
        with self._lock:
            return len(self._incoming)

    def write(self, data):
        data = bytes(data)
        self.sent.extend(data)
        self.writes.append(data)
        return len(data)

    def read(self, size=1):
        with self._lock:
            chunk = bytes(self._incoming[:size])
            del self._incoming[:size]

        # behave like a short serial timeout
        if not chunk:
            time.sleep(0.001)   
        return chunk

    def flush(self):
        self.flushed = True

    def close(self):
        self.closed = True


def unescape(data):
    """Reference unescape used to check round-trips."""
    out = bytearray()
    it = iter(data)
    for b in it:
        if b == t.ESCAPE:
            out.append(next(it))
        else:
            out.append(b)
    return bytes(out)


def split_object_frames(stream):
   # Split a byte stream of OBJECT frames into unescaped (word, address) pairs
    frames = []
    i = 0
    while i < len(stream):
        assert stream[i] == t.OBJECT_START, f"expected OBJECT start at {i}"
        i += 1
        payload = bytearray()
        while len(payload) < 4:
            if stream[i] == t.ESCAPE:
                i += 1
            payload.append(stream[i])
            i += 1
        word = payload[0] | payload[1] << 8
        addr = payload[2] | payload[3] << 8
        frames.append((word, addr))
    return frames


# Constants
def test_constants_match_design_doc():
    assert t.OBJECT_START == 0x01
    assert t.RENDER_START == 0x00
    assert t.ESCAPE == 0x03
    assert set(t.HOST_ESCAPED) == {0x00, 0x01, 0x03}
    assert t.DEFAULT_BAUD == 115_200
    assert t.IMAGE_SIZE == 512


# split_u16
@pytest.mark.parametrize("value, expected", [
    (0x0000, (0x00, 0x00)),
    (0x0001, (0x01, 0x00)),
    (0x00FF, (0xFF, 0x00)),
    (0x0100, (0x00, 0x01)),
    (0x0200, (0x00, 0x02)),
    (0x1234, (0x34, 0x12)),
    (0xFFFF, (0xFF, 0xFF)),
])
def test_split_u16(value, expected):
    assert tuple(t.split_u16(value)) == expected


@pytest.mark.parametrize("value", [-1, 0x10000])
def test_split_u16_rejects_out_of_range(value):
    with pytest.raises(ValueError):
        t.split_u16(value)


# escape_bytes
def test_escape_leaves_plain_bytes_alone():
    data = bytes([0x02, 0x04, 0x7F, 0xFF])
    assert bytes(t.escape_bytes(data)) == data


@pytest.mark.parametrize("b", [0x00, 0x01, 0x03])
def test_escape_each_host_escaped_byte(b):
    assert bytes(t.escape_bytes(bytes([b]))) == bytes([0x03, b])


def test_escape_mixed_sequence():
    data = bytes([0x00, 0x02, 0x03, 0x01, 0x04])
    expected = bytes([0x03, 0x00, 0x02, 0x03, 0x03, 0x03, 0x01, 0x04])
    assert bytes(t.escape_bytes(data)) == expected


def test_escape_empty():
    assert bytes(t.escape_bytes(b"")) == b""


def test_escape_custom_set_chip_side():
    # pixel frames 
    # (chip -> host) only escape 0x00 and 0x03, not 0x01.
    data = bytes([0x00, 0x01, 0x03])
    assert bytes(t.escape_bytes(data, escaped=(0x00, 0x03))) == bytes([0x03, 0x00, 0x01, 0x03, 0x03])


def test_escape_round_trip_all_byte_values():
    data = bytes(range(256))
    escaped = bytes(t.escape_bytes(data))
    assert unescape(escaped) == data
    assert len(escaped) == 256 + 3


# frame_object
def test_frame_object_no_escapes():
    assert bytes(t.frame_object(0x1234, 0x0056)) == bytes([0x01, 0x34, 0x12, 0x56, 0x03, 0x00])


def test_frame_object_simple():
    assert bytes(t.frame_object(0xABCD, 0x0102)) == bytes([0x01, 0xCD, 0xAB, 0x02, 0x03, 0x01])


def test_frame_object_zero_word_address_zero():
    # Every payload byte is 0x00 so every one is escaped.
    assert bytes(t.frame_object(0x0000, 0x0000)) == bytes(
        [0x01, 0x03, 0x00, 0x03, 0x00, 0x03, 0x00, 0x03, 0x00])


def test_frame_object_escapes_0x01_and_0x03():
    assert bytes(t.frame_object(0x0301, 0x0103)) == bytes(
        [0x01, 0x03, 0x01, 0x03, 0x03, 0x03, 0x03, 0x03, 0x01])


def test_frame_object_start_byte_not_escaped():
    frame = bytes(t.frame_object(0x2222, 0x4444))
    assert frame[0] == 0x01
    assert frame.count(0x03) == 0


def test_frame_object_max_address():
    # 512 words -> highest address 511 = 0x01FF; hi byte 0x01 is escaped.
    assert bytes(t.frame_object(0x0505, 511)) == bytes([0x01, 0x05, 0x05, 0xFF, 0x03, 0x01])


@pytest.mark.parametrize("word, address", [
    (0, 0), (1, 1), (3, 3), (0xFFFF, 511), (0x0300, 256), (0xBEEF, 0x0042),
])
def test_frame_object_round_trip(word, address):
    assert split_object_frames(bytes(t.frame_object(word, address))) == [(word, address)]


# frame render
def test_frame_render_default_512():
    # 512 = 0x0200 -> lo 0x00 (escaped), hi 0x02
    assert bytes(t.frame_render()) == bytes([0x00, 0x03, 0x00, 0x02, 0x03, 0x00, 0x02])


def test_frame_render_explicit_matches_default():
    assert bytes(t.frame_render(512, 512)) == bytes(t.frame_render())


def test_frame_render_non_square():
    # 0x0102 x 0x0304 -> lo/hi bytes 02 01 04 03, with 01 and 03 escaped
    assert bytes(t.frame_render(0x0102, 0x0304)) == bytes([0x00, 0x02, 0x03, 0x01, 0x04, 0x03, 0x03])


def test_frame_render_start_byte_is_bare_zero():
    frame = bytes(t.frame_render())
    assert frame[0] == 0x00
    # After the start byte, every 0x00 must be preceded by an escape.
    for i in range(1, len(frame)):
        if frame[i] == 0x00:
            assert frame[i - 1] == 0x03


# write_frame / send_object / start_render
def test_write_frame_writes_bytes_exactly():
    link = FakeLink()
    frame = bytes(t.frame_object(0x1234, 7))
    t.write_frame(link, frame)
    assert bytes(link.sent) == frame


def test_send_object_writes_object_frame():
    link = FakeLink()
    t.send_object(link, 0xBEEF, 42)
    assert bytes(link.sent) == bytes(t.frame_object(0xBEEF, 42))


def test_start_render_writes_render_frame():
    link = FakeLink()
    t.start_render(link)
    assert bytes(link.sent) == bytes(t.frame_render())


def test_start_render_custom_size():
    link = FakeLink()
    t.start_render(link, 256, 128)
    assert bytes(link.sent) == bytes(t.frame_render(256, 128))


# upload_scene
def test_upload_scene_one_frame_per_word_in_address_order():
    link = FakeLink()
    words = [0x0000, 0x0001, 0x0203, 0xFFFF, 0x1234]
    t.upload_scene(link, words)
    assert split_object_frames(bytes(link.sent)) == [(w, a) for a, w in enumerate(words)]


def test_upload_scene_full_512_words():
    link = FakeLink()
    words = [(i * 37) & 0xFFFF for i in range(512)]
    t.upload_scene(link, words)
    frames = split_object_frames(bytes(link.sent))
    assert len(frames) == 512
    assert frames == [(w, a) for a, w in enumerate(words)]


def test_upload_scene_empty_sends_nothing():
    link = FakeLink()
    t.upload_scene(link, [])
    assert bytes(link.sent) == b""


def test_upload_scene_rejects_more_than_512_words():
    link = FakeLink()
    with pytest.raises(ValueError):
        t.upload_scene(link, [0] * 513)
    assert bytes(link.sent) == b""


# diff_words / upload_delta
def test_diff_words_identical_is_empty():
    words = [1, 2, 3, 4]
    assert list(t.diff_words(words, list(words))) == []


def test_diff_words_reports_changed_addresses():
    prev = [10, 20, 30, 40, 50]
    curr = [10, 21, 30, 41, 50]
    assert list(t.diff_words(prev, curr)) == [(1, 21), (3, 41)]


def test_diff_words_all_changed():
    prev = [0] * 4
    curr = [1, 2, 3, 4]
    assert list(t.diff_words(prev, curr)) == [(0, 1), (1, 2), (2, 3), (3, 4)]


def test_upload_delta_sends_only_changes():
    link = FakeLink()
    prev = [0x1111] * 300
    curr = list(prev)
    for addr in (0, 5, 299):
        curr[addr] = 0x2222 + addr
    n = t.upload_delta(link, prev, curr)
    assert n == 3
    assert split_object_frames(bytes(link.sent)) == [(curr[a], a) for a in (0, 5, 299)]


def test_upload_delta_no_changes_sends_nothing():
    link = FakeLink()
    words = list(range(50))
    assert t.upload_delta(link, words, list(words)) == 0
    assert bytes(link.sent) == b""


def test_upload_delta_camera_turn_touches_few_words():
    # Camera turn/height change touches up to ~10 words (design doc).
    link = FakeLink()
    prev = [(i * 7) & 0xFFFF for i in range(295)]
    curr = list(prev)
    for addr in range(10):
        curr[addr] ^= 0x00F0
    assert t.upload_delta(link, prev, curr) == 10
    assert len(split_object_frames(bytes(link.sent))) == 10


# estimate_upload_time
def test_estimate_upload_time_formula():
    # 5 bytes/message, 10 bits/byte
    assert t.estimate_upload_time(1) == pytest.approx(50 / 115_200)
    assert t.estimate_upload_time(100, baud=9600) == pytest.approx(100 * 50 / 9600)


def test_estimate_upload_time_full_scene_about_0_2s():
    assert t.estimate_upload_time(512) == pytest.approx(0.222, abs=0.005)


def test_estimate_upload_time_demo_scene_about_0_13s():
    assert t.estimate_upload_time(295) == pytest.approx(0.128, abs=0.005)


def test_estimate_upload_time_zero():
    assert t.estimate_upload_time(0) == 0


# read_bytes
def test_read_bytes_returns_available_data():
    link = FakeLink(incoming=bytes([0x00, 0x10, 0x20, 0x30]))
    assert bytes(t.read_bytes(link)) == bytes([0x00, 0x10, 0x20, 0x30])


def test_read_bytes_respects_max_bytes():
    link = FakeLink(incoming=bytes(range(100)))
    assert len(t.read_bytes(link, max_bytes=10)) <= 10


def test_read_bytes_empty_link():
    link = FakeLink()
    assert bytes(t.read_bytes(link)) == b""


def test_read_bytes_does_not_unescape():
    # Raw bytes only: PIXEL parsing belongs to the pixel receiver.
    raw = bytes([0x00, 0x03, 0x00, 0x05, 0x03, 0x03])
    link = FakeLink(incoming=raw)
    assert bytes(t.read_bytes(link)) == raw


# reader_thread
def _drain(q):
    """Collect queued chunks; items may be raw bytes or (timestamp, bytes)."""
    data = bytearray()
    while True:
        try:
            item = q.get_nowait()
        except queue.Empty:
            return bytes(data)
        chunk = item[-1] if isinstance(item, tuple) else item
        data.extend(chunk)


def test_reader_thread_drains_link_into_queue_and_stops():
    payload = bytes([0x00, 0x11, 0x22, 0x33] * 64)
    link = FakeLink()
    out_q = queue.Queue()
    stop = threading.Event()
    th = threading.Thread(target=t.reader_thread, args=(link, out_q, stop), daemon=True)
    th.start()

    link.feed(payload[:100])
    time.sleep(0.05)
    link.feed(payload[100:])

    deadline = time.monotonic() + 2.0
    got = bytearray()
    while len(got) < len(payload) and time.monotonic() < deadline:
        got.extend(_drain(out_q))
        time.sleep(0.01)

    stop.set()
    th.join(timeout=2.0)
    assert not th.is_alive(), "reader_thread did not exit after stop_event was set"
    got.extend(_drain(out_q))
    assert bytes(got) == payload


def test_reader_thread_chunks_are_timestamped():
    link = FakeLink(incoming=bytes([0x00, 0x01, 0x02, 0x04]))
    out_q = queue.Queue()
    stop = threading.Event()
    th = threading.Thread(target=t.reader_thread, args=(link, out_q, stop), daemon=True)
    th.start()
    item = out_q.get(timeout=2.0)
    stop.set()
    th.join(timeout=2.0)

    assert isinstance(item, tuple) and len(item) == 2
    ts, chunk = item
    assert isinstance(ts, float)
    assert len(chunk) > 0


# Link management
def test_open_link_rejects_unknown_backend():
    with pytest.raises(ValueError):
        t.open_link("bogus")


def test_close_link_flushes_and_closes():
    link = FakeLink()
    t.close_link(link)
    assert link.flushed
    assert link.closed
