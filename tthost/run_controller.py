"""Run controller: the one place that knows the order of a render.

Called by the window (gui.py), the IPC thread (serve mode) and the CLI, so the
same render logic runs from all three (design doc, section 8.2).

States (section 8.2, figure 8.1):

    idle -> compiling -> uploading -> rendering -> processing -> done -> (next render) idle
    cancel, timeout or error from uploading / rendering -> resetting -> idle
    an error with nothing to reset -> error

    Before   compile the scene, create the run folder, save scene.txt and sram.hex,
             open the raw byte log and event log
    During   open the link, reset the chip, start the receiver, upload every word,
             send RENDER, turn receiver rows into progress messages
    After    run the image pipeline, save render.png / render_raw.png /
             render_linear.npy, write the log summary, send done

Rules:
    - reject render while a run is active
    - reject render if the last compile had errors
    - cancel from any active state stops the receiver and resets the chip

Call rule: calls downward only (scene_compiler, transport, receive, image
pipeline). It never imports the GUI or IPC; they subscribe through callbacks,
which are called on the run worker thread.

NOTE: the design doc puts open/reset/upload/render/receive behind a
UartController (section 10). Until that exists, _drive_link below does those
steps directly with transport + receive.
"""

from dataclasses import dataclass, field

# State names; these strings are also what the IPC "state" message sends.
IDLE = "idle"
COMPILING = "compiling"
UPLOADING = "uploading"
RENDERING = "rendering"
PROCESSING = "processing"
DONE = "done"
ERROR = "error"
RESETTING = "resetting"

ACTIVE_STATES = (COMPILING, UPLOADING, RENDERING, PROCESSING, RESETTING)
DEFAULT_RUNS_DIR = "runs"
DEFAULT_TIMEOUT = 10.0      # seconds with no byte before the receiver ends the frame


class RunError(Exception):
    """A render was rejected (already running, compile errors) or failed part way."""


@dataclass
class RunSettings:
    """Everything one render needs. Built by the window, the IPC "render" message or the CLI."""
    scene_path: str | None = None      # file to compile; or use the last scene text sent
    backend: str = "chip"              # one of transport.BACKENDS
    port: str | None = None            # serial port / sim path / pty / log, see transport.open_link
    baud: int = 115_200
    spp: int | None = None             # override the scene's spp (1..32, power of 2)
    passes: int = 1                    # multi-pass averaging (open HW question on the LFSR)
    runs_dir: str = DEFAULT_RUNS_DIR
    timeout: float = DEFAULT_TIMEOUT


@dataclass
class CheckResult:
    """The compiler's verdict on a scene; maps 1:1 to the IPC "checked" message."""
    rev: int | None
    words: int
    bvs: int
    errors: list = field(default_factory=list)     # [{"line": int, "rule": str}]
    warnings: list = field(default_factory=list)

    @property
    def ok(self):
        """True when there are no errors (warnings are allowed)."""
        pass


@dataclass
class Progress:
    """One progress update, sent once per finished row; maps to the IPC "progress" message."""
    row: int
    pixels: int
    eta_s: float | None
    bad: int


@dataclass
class RunResult:
    """End of a run; maps to the IPC "done" message (or "error" when not complete)."""
    run_dir: str
    png: str | None
    pixels: int
    expected: int
    bad: int
    complete: bool
    total_s: float


class RunController:
    """Owns one render at a time: state, compiled words, run worker thread and cancel.

    Callbacks (all optional, called on the worker thread):
        on_state(state)         every state change
        on_progress(Progress)   once per finished row
        on_done(RunResult)      end of a successful run
        on_error(message)       run rejected or failed
    """

    def __init__(self, on_state=None, on_progress=None, on_done=None, on_error=None):
        """Store callbacks; start in IDLE with no compiled scene and no worker."""
        pass

    # Public API (window, IPC, CLI)
    @property
    def state(self):
        """Current state name (one of the constants above)."""
        pass

    @property
    def is_active(self):
        """True while a run is in an ACTIVE_STATES state."""
        pass

    def check_scene(self, text, rev=None, filename="<scene>"):
        """Compile scene text and remember the result for the next render.

        Used for the IPC "scene" message and before every render. Stores the
        scene text, the 512-word image and the CheckResult. Returns the
        CheckResult; never raises for scene errors (they go in .errors).
        """
        pass

    def load_scene(self, path):
        """Read a scene file from disk and run check_scene on its text. Returns the CheckResult."""
        pass

    def render(self, settings):
        """Start a render on the run worker thread and return immediately.

        Raises RunError if a run is active or the last compile had errors.
        Used by the window and serve mode.
        """
        pass

    def render_blocking(self, settings):
        """Run a render on the calling thread and return its RunResult. Used by the CLI.

        Same checks and steps as render(); raises RunError on rejection or failure.
        """
        pass

    def cancel(self):
        """Cancel the current run from any active state: stop the receiver and reset the chip.

        Does nothing when idle. The worker ends in RESETTING -> IDLE.
        """
        pass

    def wait(self, timeout=None):
        """Block until the worker thread finishes (for tests and shutdown). Returns True if it finished."""
        pass

    # Worker
    def _run_worker(self, settings):
        """Body of one render: _before_run -> _drive_link -> _after_run.

        Catches every exception so the worker never dies silently: on cancel,
        timeout or error it goes through _reset_and_idle and reports on_error.
        Always closes the link and log files.
        """
        pass

    def _check_can_render(self):
        """Raise RunError if a run is active, no scene was compiled, or the last compile had errors."""
        pass

    def _before_run(self, settings):
        """COMPILING: recompile (applying settings.spp), make the run folder, save
        scene.txt and sram.hex, open the logs and write the "run_start" event.

        Returns the run folder path.
        """
        pass

    def _drive_link(self, settings, run_dir):
        """UPLOADING then RENDERING (stand-in for UartController.run).

        Open the link (transport.open_link), reset the chip, start the PixelReceiver
        BEFORE sending anything, upload every word (transport.upload_scene), send
        RENDER (transport.start_render), then wait for the receiver's FrameResult.
        Repeats the render + receive for each pass. Returns the last FrameResult.
        """
        pass

    def _after_run(self, run_dir, frame_result):
        """PROCESSING: run the image pipeline on the frame buffer (fill bad pixels,
        firefly clamp, sRGB gamma), save render.png, render_raw.png and
        render_linear.npy, write the "summary" event, then DONE and on_done.

        Returns the RunResult. A short frame is still saved but reported as not complete.
        """
        pass

    def _on_row(self, y, pixels, bad):
        """Receiver row callback: build a Progress (with ETA) and send it to on_progress."""
        pass

    def _reset_chip(self):
        """Reset the chip so the next run starts clean (rst_n / serial control line; open HW question)."""
        pass

    def _reset_and_idle(self, message=None):
        """RESETTING: stop the receiver, reset the chip, close the link, then IDLE.

        Reports `message` through on_error when given (timeout or error, not plain cancel).
        """
        pass

    def _set_state(self, state):
        """Change state, write a "state" event to the event log and call on_state."""
        pass


# Helpers (no controller state; easy to test alone)
def make_run_dir(runs_dir, scene_path, now=None):
    """Create and return runs/<YYYY-MM-DD_HHMMSS>_<scene name>/ (section 12.1).

    `now` is a datetime for tests; defaults to the current time. Adds a suffix
    if the folder already exists rather than overwriting a previous run.
    """
    pass


def save_run_inputs(run_dir, scene_text, hex_dump):
    """Write scene.txt (exact text compiled) and sram.hex (memory image) into the run folder."""
    pass


def open_run_logs(run_dir):
    """Open raw_bytes.log and events.jsonl in the run folder for writing.

    Returns (raw_log, event_log), both open text files the caller must close.
    """
    pass


def estimate_eta(pixels_done, elapsed_s, total=262_144):
    """Seconds left at the current pixel rate, or None before any pixels have arrived."""
    pass
