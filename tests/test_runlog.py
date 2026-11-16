"""Tests for tthost.runlog: run_log.txt, the readable log written when a run ends.

Checks that every section is there, that the latency numbers are computed from
the event times, and that the log is written for failed and cancelled runs too.

RUN from root:  python -m pytest tests
"""

import threading
import time
from pathlib import Path

import numpy as np
import pytest

from tthost import run_controller as rc
from tthost import runlog, transport
from tthost.framebuffer import FrameBuffer

DEMO = Path(__file__).resolve().parent.parent / "scene_demo.txt"
T0 = 1_000.0


def event(name, at_ms, **fields):
    return {"event": name, "time": T0 + at_ms / 1000.0, **fields}


def sample_events():
    """A tiny made-up run: 2 rows of 2 pixels, one of them short."""
    return [
        event("run_start", 0, run="r1", scene="s.txt", scene_sha1="abc", words=43, bvs=1, spp=None,
              passes=1, backend="sim", port="fake", baud=115200, host_commit="1234567"),
        event("state", 1, state="uploading"),
        event("upload_start", 2, words=43),
        event("upload_done", 12, bytes=230),
        event("render", 20, width=512, height=512),
        event("pixel", 120, i=0, x=0, y=0, r=1, g=2, b=3, esc=False),
        event("pixel", 130, i=1, x=1, y=0, r=0, g=3, b=255, esc=True),
        event("row", 130, y=0, ms=110.0),
        event("anomaly", 150, kind="short_pixel", i=2, got=2, action="resync"),
        event("pixel", 170, i=3, x=1, y=1, r=9, g=8, b=7, esc=False),
        event("row", 170, y=1, ms=40.0),
        event("frame_end", 220, pixels=4, expected=4, bad=1, reason="complete"),
        event("summary", 300, total_s=0.3, px_per_s=20.0, bad=1, png="render.png"),
    ]


def section(text, name):
    """The lines of one section, without its heading and underline."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.split(" (")[0] == name)
    end = next((i for i in range(start + 1, len(lines)) if lines[i] == ""), len(lines))
    return lines[start + 2:end]


def test_log_has_every_section_in_order():
    text = runlog.build_run_log(sample_events(), "done")
    headings = [line.split(" (")[0] for line in text.splitlines()
                if line.split(" (")[0] in ("RUN", "LATENCY", "ANOMALIES", "TIMELINE", "ROWS", "PIXELS")]
    assert headings == ["RUN", "LATENCY", "ANOMALIES", "TIMELINE", "ROWS", "PIXELS"]


def test_run_section_says_what_ran_and_how_it_ended():
    run = "\n".join(section(runlog.build_run_log(sample_events(), "done", files={"render.png", "run_log.txt"}), "RUN"))
    for expected in ("scene                s.txt", "backend              sim", "memory words         43",
                     "samples per pixel    as the scene file says", "pixels received      4 of 4 (complete)",
                     "bad pixels           1", "ended                done", "files                render.png, run_log.txt"):
        assert expected in run


def test_latency_is_worked_out_from_the_event_times():
    latency = "\n".join(section(runlog.build_run_log(sample_events(), "done"), "LATENCY"))
    assert "scene upload               43 words, 230 bytes in 10.0 ms" in latency
    assert "RENDER to first pixel      100.000 ms" in latency
    assert "RENDER to end of frame     0.200 s" in latency
    assert "pixel rate                 20 pixels per second" in latency
    # Pixels arrived 100, 110 and 150 ms after RENDER: gaps of 10 and 40 ms.
    assert "between pixels             min 10.000  mean 25.000  median 25.000  99% 40.000  max 40.000  ms" in latency
    assert "per row                    min 40.000  mean 75.000" in latency
    assert "slowest rows               row 0 (110.0 ms), row 1 (40.0 ms)" in latency
    assert "whole run                  0.3 s" in latency


def test_pixel_lines_carry_position_colour_time_escape_and_bad_flag():
    pixels = section(runlog.build_run_log(sample_events(), "done"), "PIXELS")
    assert pixels[-3:] == [
        "     0   0   0     1   2   3      100.000   no  no",
        "     1   1   0     0   3 255      110.000  yes  no",
        "     3   1   1     9   8   7      150.000   no  no",
    ]


def test_anomalies_timeline_and_rows():
    text = runlog.build_run_log(sample_events(), "done")
    assert section(text, "ANOMALIES") == ["     150.000 ms  kind=short_pixel  i=2  got=2  action=resync"]
    timeline = section(text, "TIMELINE")
    assert timeline[1].split()[:2] == ["0.000", "run_start"]
    assert [line.split()[1] for line in timeline[1:]] == [
        "run_start", "state", "upload_start", "upload_done", "render", "anomaly", "frame_end", "summary"]
    assert section(text, "ROWS")[1:] == ["   0       130.000      110.0", "   1       170.000       40.0"]


def test_without_pixel_events_values_come_from_the_frame_buffer():
    events = [e for e in sample_events() if e["event"] != "pixel"]
    fb = FrameBuffer(width=2, height=2)
    fb.pixels[:] = np.arange(12, dtype=np.float32).reshape(2, 2, 3)
    text = runlog.build_run_log(events, "done", framebuffer=fb)
    pixels = section(text, "PIXELS")
    assert "arrival times and escapes were not recorded" in pixels[0]
    assert pixels[2] == "     0   0   0     0   1   2            -    -   no"
    assert pixels[4] == "     2   0   1     6   7   8            -    -  yes"          # the short pixel
    assert "between pixels             not recorded" in text
    assert "RENDER to first row done   110.000 ms" in text
    assert section(text, "ROWS")[1] == "   0       130.000      110.0      1.50     2.50     3.50"


def test_a_run_that_stopped_early_still_makes_a_log():
    events = sample_events()[:3] + [event("error", 5, message="port unplugged")]
    text = runlog.build_run_log(events, "error: port unplugged")
    assert "ended                error: port unplugged" in text
    assert "whole run                  0.005 s (to the last event)" in text
    assert section(text, "PIXELS") == ["none received"]
    assert runlog.build_run_log([], "error: nothing happened").count("\n") > 10


def test_read_events_skips_a_torn_last_line(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"event": "state", "time": 1.0, "state": "idle"}\n{"event": "pix')
    assert runlog.read_events(path) == [{"event": "state", "time": 1.0, "state": "idle"}]


# Through the run controller, against the stand-in chip
def test_finished_render_writes_the_log_with_every_pixel(tmp_path):
    controller = rc.RunController()
    result = controller.render_blocking(rc.RunSettings(
        scene_path=str(DEMO), backend="sim", port=transport.FAKE_CHIP, runs_dir=str(tmp_path), timeout=5.0))
    assert result.log == str(Path(result.run_dir) / "run_log.txt")
    text = Path(result.log).read_text()

    assert "ended                done" in text and "pixels received      262144 of 262144 (complete)" in text
    assert "PIXELS (262144)" in text and "ROWS (512)" in text and "ANOMALIES (0)" in text
    pixels = section(text, "PIXELS")[-262144:]
    # Spot-check the log against the frame buffer: first, last and one in the middle.
    fb = controller.framebuffer.pixels
    for index in (0, 131_072 + 77, 262_143):
        i, x, y, r, g, b = (int(v) for v in pixels[index].split()[:6])
        assert (i, x, y) == (index, index % 512, index // 512)
        assert (r, g, b) == tuple(int(v) for v in fb[y, x])
    times = [float(line.split()[6]) for line in pixels[::997]]
    assert times == sorted(times) and times[0] > 0, "arrival times only go up"
    assert [line.split()[1] for line in section(text, "TIMELINE")[1:]][-1] == "summary"


def test_failed_and_cancelled_runs_write_the_log_too(tmp_path):
    silent = tmp_path / "silent_chip.py"
    silent.write_text("import sys\nsys.stdin.buffer.read()\n")       # takes the upload, never answers
    settings = rc.RunSettings(scene_path=str(DEMO), backend="sim", port=str(silent),
                              runs_dir=str(tmp_path / "runs"), timeout=0.4)

    controller = rc.RunController()
    with pytest.raises(rc.RunError):
        controller.render_blocking(settings)
    text = next((tmp_path / "runs").glob("*/run_log.txt")).read_text()
    assert "ended                error: frame ended early (timeout): 0 of 262144 pixels" in text
    assert "kind=timeout" in "\n".join(section(text, "ANOMALIES"))
    assert "none received" in text

    settings.runs_dir, settings.timeout = str(tmp_path / "cancelled"), 30.0
    controller = rc.RunController()
    controller.render(settings)
    deadline = time.monotonic() + 5
    while controller.state != rc.RENDERING and time.monotonic() < deadline:
        time.sleep(0.01)
    threading.Thread(target=controller.cancel).start()
    assert controller.wait(5.0)
    text = next((tmp_path / "cancelled").glob("*/run_log.txt")).read_text()
    assert "ended                cancelled" in text


def test_scene_error_before_the_run_folder_exists_writes_nothing(tmp_path):
    bad = tmp_path / "bad.txt"
    bad.write_text(DEMO.read_text().replace("45 -12 90", "45 -12 120"))
    with pytest.raises(rc.RunError):
        rc.RunController().render_blocking(rc.RunSettings(scene_path=str(bad), backend="sim",
                                                          port=transport.FAKE_CHIP, runs_dir=str(tmp_path / "runs")))
    assert not (tmp_path / "runs").exists()
