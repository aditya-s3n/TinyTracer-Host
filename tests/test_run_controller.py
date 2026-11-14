"""Tests for tthost.run_controller: state order, render rules, cancel and run folders.

Expected behaviour comes from the design doc, section 8.2 "Run controller":
    idle -> compiling -> uploading -> rendering -> processing -> done
    cancel, timeout or error -> resetting -> idle
    reject render while a run is active; reject render if the last compile had errors
    run folder runs/<date_time>_<scene>/ with scene.txt, sram.hex, raw_bytes.log,
    events.jsonl, render.png, render_raw.png, render_linear.npy (section 12.1)

The link is faked (in-memory chip that answers RENDER with PIXEL bytes) and the
compiler is stubbed, so no hardware, sim or pyserial is needed.

RUN from root:  python -m pytest tests
"""

import json
import os
import threading
import time
from datetime import datetime

import pytest

from tthost import receive
from tthost import run_controller as rc
from tthost import transport as t

PIXELS = 1024          # two rows keeps each fake render fast
WORDS = [(i * 37) & 0xFFFF for i in range(20)]


# Fakes / helpers
class FakeChipLink:
    """In-memory link: records writes, and after a RENDER frame starts streaming PIXEL bytes.

    `stop_after` sends only that many pixels (timeout test); `stop_after=0` never
    answers. `fail_on_write` raises OSError on the first write (link error test).
    """

    def __init__(self, pixels=None, stop_after=None, fail_on_write=False):
        self.pixels = pixels if pixels is not None else [
            (i % 256, (i * 7) % 256, (i * 13) % 256) for i in range(PIXELS)]
        self.stop_after = stop_after
        self.fail_on_write = fail_on_write
        self.writes = []
        self.resets = 0
        self.closed = False
        self._incoming = bytearray()
        self._lock = threading.Lock()

    def write(self, data):
        if self.fail_on_write:
            raise OSError("fake link unplugged")

        data = bytes(data)
        self.writes.append(data)

        if data[0] == t.RENDER_START:       # RENDER: the chip starts sending pixels
            n = len(self.pixels) if self.stop_after is None else self.stop_after
            with self._lock:
                self._incoming += b"".join(receive.escape_pixel(*p) for p in self.pixels[:n])

        return len(data)

    @property
    def in_waiting(self):
        with self._lock:
            return len(self._incoming)

    def read(self, size=1):
        with self._lock:
            chunk = bytes(self._incoming[:size])
            del self._incoming[:size]

        if not chunk:
            time.sleep(0.001)
        return chunk

    def reset(self):
        self.resets += 1

    def flush(self):
        pass

    def close(self):
        self.closed = True


class Recorder:
    """Collects every on_state / on_progress / on_done / on_error callback in order."""

    def __init__(self):
        self.events = []

    def callbacks(self):
        return {
            "on_state": lambda s: self.events.append(("state", s)),
            "on_progress": lambda p: self.events.append(("progress", p)),
            "on_done": lambda res: self.events.append(("done", res)),
            "on_error": lambda m: self.events.append(("error", m)),
        }

    def of(self, kind):
        return [v for k, v in self.events if k == kind]

    @property
    def states(self):
        return self.of("state")


def fake_compiler(text, filename):
    """Stands in for scene_compiler: FOV over 90 on the camera line is an error, else WORDS."""
    for n, line in enumerate(text.splitlines(), 1):
        fields = line.split("#", 1)[0].split()
        if fields and fields[0] == "camera" and float(fields[6]) > 90:
            return None, {"words": 0, "bvs": 0, "warnings": [], "hex": "",
                          "errors": [{"line": n, "rule": f"field of view {fields[6]} is over the limit of 90"}]}

    return list(WORDS), {"words": len(WORDS), "bvs": 1, "errors": [], "warnings": [],
                         "hex": rc.simple_hex_dump(WORDS)}


def make_controller(link=None, recorder=None):
    link = link if link is not None else FakeChipLink()
    recorder = recorder if recorder is not None else Recorder()

    ctl = rc.RunController(**recorder.callbacks(), compiler=fake_compiler,
                           open_link=lambda backend, port, baud: link)

    return ctl, link, recorder


def settings(tmp_path, **kw):
    kw.setdefault("timeout", 0.3)
    return rc.RunSettings(backend="sim", port="fake", runs_dir=str(tmp_path),
                          expected_pixels=PIXELS, log_pixels=False, **kw)


def wait_for(cond, timeout=3.0):
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.005)

    return False


@pytest.fixture
def scene_text():
    """A small valid scene (one sphere) as .txt text."""
    return ("image 512\nspp 8\ncamera 5 5 12 45 -12 90\n"
            "sky 235 240 255 110 160 255\nground 205 205 200\nchecker 120 120 130\n"
            "sphere 32 22 6 6 matte 220 60 50 0 0\n")


@pytest.fixture
def bad_scene_text(scene_text):
    """A scene that fails validation (FOV 100)."""
    return scene_text.replace("45 -12 90", "45 -12 100")


# Constants / data classes
def test_state_names_match_ipc_protocol():
    """State strings are the ones the IPC "state" message uses: idle, compiling, ..., done, error."""
    assert (rc.IDLE, rc.COMPILING, rc.UPLOADING, rc.RENDERING, rc.PROCESSING, rc.DONE, rc.ERROR) == (
        "idle", "compiling", "uploading", "rendering", "processing", "done", "error")


def test_check_result_ok():
    """CheckResult.ok is True with only warnings and False with any error."""
    assert rc.CheckResult(rev=1, words=10, bvs=1, warnings=["saturated"]).ok
    assert not rc.CheckResult(rev=1, words=0, bvs=0, errors=[{"line": 3, "rule": "x"}]).ok


# check_scene
def test_check_scene_valid(scene_text):
    """A valid scene returns words, bvs, no errors, and echoes the rev."""
    ctl, _, _ = make_controller()
    check = ctl.check_scene(scene_text, rev=7)

    assert check == rc.CheckResult(rev=7, words=len(WORDS), bvs=1, errors=[], warnings=[])


def test_check_scene_errors_do_not_raise(bad_scene_text):
    """A bad scene returns errors with line and rule instead of raising."""
    ctl, _, _ = make_controller()
    check = ctl.check_scene(bad_scene_text, rev=8)

    assert not check.ok
    assert check.errors == [{"line": 3, "rule": "field of view 100 is over the limit of 90"}]


def test_check_scene_stays_idle(scene_text):
    """check_scene alone does not leave IDLE."""
    ctl, _, rec = make_controller()
    ctl.check_scene(scene_text)

    assert ctl.state == rc.IDLE
    assert rec.events == []


# Render rules
def test_render_rejected_without_scene(tmp_path):
    """render() with nothing compiled raises RunError."""
    ctl, link, _ = make_controller()

    with pytest.raises(rc.RunError):
        ctl.render(settings(tmp_path))

    assert link.writes == []


def test_render_rejected_after_compile_errors(tmp_path, bad_scene_text):
    """render() after a check_scene with errors raises RunError and sends nothing."""
    ctl, link, _ = make_controller()
    ctl.check_scene(bad_scene_text)

    with pytest.raises(rc.RunError):
        ctl.render(settings(tmp_path))

    assert link.writes == []
    assert ctl.state == rc.IDLE


def test_render_rejected_while_active(tmp_path, scene_text):
    """A second render() during a run raises RunError."""
    ctl, _, _ = make_controller(FakeChipLink(stop_after=0))
    ctl.check_scene(scene_text)
    ctl.render(settings(tmp_path, timeout=5.0))

    try:
        assert ctl.is_active
        with pytest.raises(rc.RunError):
            ctl.render(settings(tmp_path))
    finally:
        ctl.cancel()
        assert ctl.wait(3.0)


# Happy path
def test_render_state_order(tmp_path, scene_text):
    """on_state sees compiling, uploading, rendering, processing, done in that order."""
    ctl, _, rec = make_controller()
    ctl.check_scene(scene_text)
    ctl.render_blocking(settings(tmp_path))

    assert rec.states == [rc.COMPILING, rc.UPLOADING, rc.RENDERING, rc.PROCESSING, rc.DONE]
    assert ctl.state == rc.DONE


def test_render_uploads_every_word_then_render(tmp_path, scene_text):
    """The fake link sees one OBJECT per word in address order, then exactly one RENDER."""
    ctl, link, _ = make_controller()
    ctl.check_scene(scene_text)
    ctl.render_blocking(settings(tmp_path))

    expected = [t.frame_object(w, a) for a, w in enumerate(WORDS)] + [t.frame_render()]
    assert link.writes == expected


def test_receiver_started_before_render_sent(tmp_path, scene_text):
    """Pixels the fake chip sends immediately after RENDER are not lost."""
    ctl, _, _ = make_controller()
    ctl.check_scene(scene_text)
    result = ctl.render_blocking(settings(tmp_path))

    assert result.pixels == PIXELS
    assert result.complete
    assert ctl.framebuffer.count == PIXELS


def test_progress_once_per_row(tmp_path, scene_text):
    """on_progress is called once per row with row, pixels, eta_s and bad."""
    ctl, _, rec = make_controller()
    ctl.check_scene(scene_text)
    ctl.render_blocking(settings(tmp_path))

    progress = rec.of("progress")
    assert [(p.row, p.pixels, p.bad) for p in progress] == [(0, 512, 0), (1, 1024, 0)]
    assert progress[-1].eta_s == pytest.approx(0.0)


def test_done_result(tmp_path, scene_text):
    """on_done gets RunResult with png, run_dir, bad=0, complete=True."""
    ctl, _, rec = make_controller()
    ctl.check_scene(scene_text)
    ctl.render_blocking(settings(tmp_path))

    [result] = rec.of("done")
    assert result.complete
    assert result.bad == 0
    assert os.path.isfile(result.png)
    assert os.path.dirname(result.png) == result.run_dir
    assert rec.of("error") == []


def test_render_blocking_returns_result(tmp_path, scene_text):
    """render_blocking (CLI path) returns the same RunResult on the calling thread."""
    ctl, _, rec = make_controller()
    ctl.check_scene(scene_text)
    result = ctl.render_blocking(settings(tmp_path))

    assert rec.of("done") == [result]


def test_render_in_background_finishes(tmp_path, scene_text):
    """render() (window / serve path) runs on the worker thread and ends in DONE."""
    ctl, _, rec = make_controller()
    ctl.check_scene(scene_text)
    ctl.render(settings(tmp_path))

    assert ctl.wait(5.0)
    assert ctl.state == rc.DONE
    assert len(rec.of("done")) == 1


# Cancel / faults
def test_cancel_during_render_resets_and_returns_idle(tmp_path, scene_text):
    """cancel() mid-render: receiver stops, chip reset, states end resetting -> idle."""
    ctl, link, rec = make_controller(FakeChipLink(stop_after=0))
    ctl.check_scene(scene_text)
    ctl.render(settings(tmp_path, timeout=5.0))

    assert wait_for(lambda: ctl.state == rc.RENDERING)
    ctl.cancel()
    assert ctl.wait(3.0)

    assert rec.states[-2:] == [rc.RESETTING, rc.IDLE]
    assert link.resets == 2          # once at the start of the run, once for the cancel
    assert link.closed
    assert rec.of("error") == []
    assert rec.of("done") == []


def test_cancel_when_idle_does_nothing():
    """cancel() while idle does not change state or call callbacks."""
    ctl, _, rec = make_controller()

    ctl.cancel()
    assert ctl.state == rc.IDLE
    assert rec.events == []


def test_timeout_reports_error_and_keeps_partial_frame(tmp_path, scene_text):
    """A chip that stops early ends in an error report, but the partial image and logs are saved."""
    ctl, _, rec = make_controller(FakeChipLink(stop_after=600))
    ctl.check_scene(scene_text)

    with pytest.raises(rc.RunError, match="600 of 1024"):
        ctl.render_blocking(settings(tmp_path, timeout=0.2))

    assert len(rec.of("error")) == 1
    assert ctl.state == rc.IDLE

    [run_dir] = os.listdir(tmp_path)
    assert os.path.isfile(os.path.join(tmp_path, run_dir, "render.png"))
    assert os.path.isfile(os.path.join(tmp_path, run_dir, "render_linear.npy"))


def test_link_error_goes_to_idle(tmp_path, scene_text):
    """An exception from the link during upload is reported through on_error and the state returns to idle."""
    ctl, _, rec = make_controller(FakeChipLink(fail_on_write=True))
    ctl.check_scene(scene_text)

    with pytest.raises(rc.RunError, match="unplugged"):
        ctl.render_blocking(settings(tmp_path))

    assert len(rec.of("error")) == 1
    assert ctl.state == rc.IDLE
    assert not ctl.is_active


def test_can_render_again_after_done(tmp_path, scene_text):
    """After done, a new render() is accepted."""
    ctl, _, rec = make_controller()
    ctl.check_scene(scene_text)
    ctl.render_blocking(settings(tmp_path))
    ctl.render_blocking(settings(tmp_path))

    assert len(rec.of("done")) == 2
    assert len(os.listdir(tmp_path)) == 2


# Run folder and logs
def test_make_run_dir_name(tmp_path):
    """make_run_dir(tmp_path, "scenes/scene_demo.txt", now=2026-11-03 16:20:05) -> 2026-11-03_162005_scene_demo."""
    path = rc.make_run_dir(str(tmp_path), "scenes/scene_demo.txt", now=datetime(2026, 11, 3, 16, 20, 5))

    assert os.path.basename(path) == "2026-11-03_162005_scene_demo"
    assert os.path.isdir(path)


def test_make_run_dir_does_not_overwrite(tmp_path):
    """Two runs in the same second get different folders."""
    now = datetime(2026, 11, 3, 16, 20, 5)
    a = rc.make_run_dir(str(tmp_path), "scene_demo.txt", now=now)
    b = rc.make_run_dir(str(tmp_path), "scene_demo.txt", now=now)

    assert a != b
    assert os.path.isdir(a) and os.path.isdir(b)


def test_run_folder_contents(tmp_path, scene_text):
    """After a run the folder holds scene.txt, sram.hex, raw_bytes.log, events.jsonl, render.png,
    render_raw.png and render_linear.npy."""
    ctl, _, _ = make_controller()
    ctl.check_scene(scene_text)
    result = ctl.render_blocking(settings(tmp_path))

    assert set(os.listdir(result.run_dir)) == {
        "scene.txt", "sram.hex", "raw_bytes.log", "events.jsonl",
        "render.png", "render_raw.png", "render_linear.npy"}

    with open(os.path.join(result.run_dir, "scene.txt")) as f:
        assert f.read() == scene_text


def test_event_log_starts_with_run_start_and_ends_with_summary(tmp_path, scene_text):
    """events.jsonl first line is run_start, last line is summary."""
    ctl, _, _ = make_controller()
    ctl.check_scene(scene_text)
    result = ctl.render_blocking(settings(tmp_path))

    with open(os.path.join(result.run_dir, "events.jsonl")) as f:
        events = [json.loads(line) for line in f]

    assert events[0]["event"] == "run_start"
    assert events[0]["words"] == len(WORDS)
    assert events[-1]["event"] == "summary"
    assert events[-1]["bad"] == 0


# estimate_eta
def test_estimate_eta_none_before_pixels():
    """No pixels yet -> None."""
    assert rc.estimate_eta(0, 5.0) is None
    assert rc.estimate_eta(100, 0.0) is None


def test_estimate_eta_constant_rate():
    """Half the pixels in 100 s -> about 100 s left."""
    assert rc.estimate_eta(131_072, 100.0) == pytest.approx(100.0)
    assert rc.estimate_eta(rc.TOTAL_PIXELS, 50.0) == 0.0


# Pipeline helpers
def test_apply_spp_replaces_existing_line(scene_text):
    """apply_spp swaps the value on the existing spp line."""
    assert "spp 32\n" in rc.apply_spp(scene_text, 32)
    assert "spp 8" not in rc.apply_spp(scene_text, 32)


def test_linear_to_srgb8_reference_values():
    """sRGB encode matches reference values (section 4.5): 0 -> 0, 255 -> 255, mid-grey 128 -> 188."""
    import numpy as np
    out = rc.linear_to_srgb8(np.array([0.0, 128.0, 255.0], dtype=np.float32))
    assert out.tolist() == [0, 188, 255]
