"""Tests for tthost.run_controller: state order, render rules, cancel and run folders.

Expected behaviour comes from the design doc, section 8.2 "Run controller":
    idle -> compiling -> uploading -> rendering -> processing -> done
    cancel, timeout or error -> resetting -> idle
    reject render while a run is active; reject render if the last compile had errors
    run folder runs/<date_time>_<scene>/ with scene.txt, sram.hex, raw_bytes.log,
    events.jsonl, render.png, render_raw.png, render_linear.npy (section 12.1)

The link is faked (in-memory chip that answers RENDER with PIXEL bytes) and the
compiler can be stubbed, so no hardware, sim or pyserial is needed.

RUN from root:  python -m pytest tests
"""

import pytest

from tthost import run_controller as rc  # noqa: F401

# Skeleton: remove this line as the tests get filled in.
pytestmark = pytest.mark.skip(reason="skeleton: run controller tests not written yet")


# Fakes / helpers
class FakeChipLink:
    """In-memory link: records writes, and after a RENDER frame starts streaming PIXEL bytes.

    Options to drop a byte, stop early (timeout) or never answer, for fault tests.
    """

    def __init__(self, pixels=None, stop_after=None):
        pass


class Recorder:
    """Collects every on_state / on_progress / on_done / on_error callback in order."""

    def __init__(self):
        pass


@pytest.fixture
def scene_text():
    """A small valid scene (one sphere) as .txt text."""
    pass


@pytest.fixture
def bad_scene_text():
    """A scene that fails validation (e.g. FOV 100 or spp 24)."""
    pass


# Constants / data classes
def test_state_names_match_ipc_protocol():
    """State strings are the ones the IPC "state" message uses: idle, compiling, ..., done, error."""
    pass


def test_check_result_ok():
    """CheckResult.ok is True with only warnings and False with any error."""
    pass


# check_scene
def test_check_scene_valid():
    """A valid scene returns words, bvs, no errors, and echoes the rev."""
    pass


def test_check_scene_errors_do_not_raise():
    """A bad scene returns errors with line and rule instead of raising."""
    pass


def test_check_scene_stays_idle():
    """check_scene alone does not leave IDLE."""
    pass


# Render rules
def test_render_rejected_without_scene():
    """render() with nothing compiled raises RunError."""
    pass


def test_render_rejected_after_compile_errors():
    """render() after a check_scene with errors raises RunError and sends nothing."""
    pass


def test_render_rejected_while_active():
    """A second render() during a run raises RunError."""
    pass


# Happy path
def test_render_state_order():
    """on_state sees compiling, uploading, rendering, processing, done in that order."""
    pass


def test_render_uploads_every_word_then_render():
    """The fake link sees one OBJECT per word in address order, then exactly one RENDER."""
    pass


def test_receiver_started_before_render_sent():
    """Pixels the fake chip sends immediately after RENDER are not lost."""
    pass


def test_progress_once_per_row():
    """on_progress is called once per row with row, pixels, eta_s and bad."""
    pass


def test_done_result():
    """on_done gets RunResult with png, run_dir, bad=0, complete=True."""
    pass


def test_render_blocking_returns_result():
    """render_blocking (CLI path) returns the same RunResult on the calling thread."""
    pass


# Cancel / faults
def test_cancel_during_render_resets_and_returns_idle():
    """cancel() mid-render: receiver stops, chip reset, states end resetting -> idle."""
    pass


def test_cancel_when_idle_does_nothing():
    """cancel() while idle does not change state or call callbacks."""
    pass


def test_timeout_reports_error_and_keeps_partial_frame():
    """A chip that stops early ends in an error report, but the partial image and logs are saved."""
    pass


def test_link_error_goes_to_idle():
    """An exception from the link during upload is reported through on_error and the state returns to idle."""
    pass


def test_can_render_again_after_done():
    """After done, a new render() is accepted."""
    pass


# Run folder and logs
def test_make_run_dir_name(tmp_path):
    """make_run_dir(tmp_path, "scenes/scene_demo.txt", now=2026-11-03 16:20:05) -> 2026-11-03_162005_scene_demo."""
    pass


def test_make_run_dir_does_not_overwrite(tmp_path):
    """Two runs in the same second get different folders."""
    pass


def test_run_folder_contents(tmp_path):
    """After a run the folder holds scene.txt, sram.hex, raw_bytes.log, events.jsonl, render.png,
    render_raw.png and render_linear.npy."""
    pass


def test_event_log_starts_with_run_start_and_ends_with_summary(tmp_path):
    """events.jsonl first line is run_start, last line is summary."""
    pass


# estimate_eta
def test_estimate_eta_none_before_pixels():
    """No pixels yet -> None."""
    pass


def test_estimate_eta_constant_rate():
    """Half the pixels in 100 s -> about 100 s left."""
    pass
