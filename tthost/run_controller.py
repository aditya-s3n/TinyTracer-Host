"""Run controller: the one place that knows the order of a render.

Called by the window (gui.py), the IPC thread (serve mode) and the CLI, so the
same render logic runs from all three (design doc, section 8.2).

States (section 8.2, figure 8.1):

    idle -> compiling -> uploading -> rendering -> processing -> done -> (next render)
    cancel, timeout or error from uploading / rendering -> resetting -> idle
    an error before the link was opened (nothing to reset) -> error

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

Call rule: calls downward only (scene_compiler, transport, receive, framebuffer).
It never imports the GUI or IPC; they subscribe through callbacks, which are
called on background threads (the run worker or the serial reader).

NOTE: the design doc puts open/reset/upload/render/receive behind a
UartController (section 10), and the image stages in an image/ package
(section 11). Until those exist, _drive_link does the link steps directly
with transport + receive, and the small pipeline at the bottom of this file
(fill bad pixels, sRGB gamma, save) stands in for image/.
"""

import hashlib
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from tthost import scene_compiler, transport
from tthost.framebuffer import FrameBuffer
from tthost.receive import TOTAL_PIXELS, PixelReceiver

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


class _Cancelled(Exception):
    """Raised inside the worker when cancel() was called; never escapes the controller."""


@dataclass
class RunSettings:
    """Everything one render needs. Built by the window, the IPC "render" message or the CLI."""

    scene_path: str | None = None      # file to compile; or use the last scene text sent
    backend: str = "chip"              # one of transport.BACKENDS
    port: str | None = None            # serial port / sim path / pty / log, see transport.open_link
    baud: int = transport.DEFAULT_BAUD
    spp: int | None = None             # override the scene's spp (1..32, power of 2)
    passes: int = 1                    # multi-pass averaging (open HW question on the LFSR)
    runs_dir: str = DEFAULT_RUNS_DIR
    timeout: float = DEFAULT_TIMEOUT
    expected_pixels: int = TOTAL_PIXELS   # lower only for tests / RTL smoke runs (first rows)
    log_pixels: bool = True            # one event log line per pixel (~10 MB per frame)


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
        return not self.errors


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

    Callbacks (all optional, called on background threads):
        on_state(state)         every state change
        on_progress(Progress)   once per finished row
        on_done(RunResult)      end of a successful run
        on_error(message)       run failed or timed out (not called for a plain cancel)

    Hooks (for tests and for swapping parts in later):
        compiler(text, filename) -> (words or None, report dict); default compile_text
        open_link(backend, port, baud) -> link; default transport.open_link
        framebuffer: shared with the live view; a new FrameBuffer if not given
    """

    def __init__(self, on_state=None, on_progress=None, on_done=None, on_error=None,
                 compiler=None, open_link=None, framebuffer=None):
        """Store callbacks; start in IDLE with no compiled scene and no worker."""
        self.on_state = on_state
        self.on_progress = on_progress
        self.on_done = on_done
        self.on_error = on_error
        self.compiler = compiler or compile_text
        self.open_link = open_link or (lambda backend, port, baud: transport.open_link(backend, port, baud))
        self.framebuffer = framebuffer if framebuffer is not None else FrameBuffer()

        self._lock = threading.Lock()
        self._state = IDLE
        self._scene_text = None
        self._scene_name = None
        self._words = None
        self._hex = ""
        self._check = None

        self._worker = None
        self._cancel = threading.Event()
        self._receiver = None
        self._link = None
        self._raw_log = None
        self._event_log = None
        self._run_start = None
        self._render_start = None
        self._expected = TOTAL_PIXELS

    # Public API (window, IPC, CLI)
    @property
    def state(self):
        """Current state name (one of the constants above)."""
        return self._state

    @property
    def is_active(self):
        """True while a run is in an ACTIVE_STATES state."""
        return self._state in ACTIVE_STATES

    @property
    def last_check(self):
        """The CheckResult from the most recent compile, or None."""
        return self._check

    def check_scene(self, text, rev=None, filename="<scene>"):
        """Compile scene text and remember the result for the next render.

        Used for the IPC "scene" message and before every render. Stores the
        scene text, the 512-word image and the CheckResult. Returns the
        CheckResult; never raises for scene errors (they go in .errors).
        """
        words, report = self.compiler(text, filename)
        check = CheckResult(
            rev=rev,
            words=report.get("words", 0),
            bvs=report.get("bvs", 0),
            errors=list(report.get("errors", [])),
            warnings=list(report.get("warnings", [])),
        )


        with self._lock:
            self._scene_text = text
            self._scene_name = filename
            self._words = list(words) if words is not None and check.ok else None
            self._hex = report.get("hex", "") if check.ok else ""
            self._check = check


        return check

    def load_scene(self, path):
        """Read a scene file from disk and run check_scene on its text. Returns the CheckResult."""
        with open(path, encoding="utf-8") as f:
            text = f.read()


        return self.check_scene(text, filename=str(path))

    def render(self, settings):
        """Start a render on the run worker thread and return immediately.

        Raises RunError if a run is active or the last compile had errors.
        Used by the window and serve mode.
        """
        self._claim_run(settings)
        self._worker = threading.Thread(target=self._run_worker, args=(settings,), name="run-worker", daemon=True)
        self._worker.start()

    def render_blocking(self, settings):
        """Run a render on the calling thread and return its RunResult. Used by the CLI.

        Same checks and steps as render(); raises RunError on rejection, failure,
        timeout or cancel.
        """
        self._claim_run(settings)
        return self._run_worker(settings, raise_errors=True)

    def cancel(self):
        """Cancel the current run from any active state: stop the receiver and reset the chip.

        Does nothing when idle. The worker ends in RESETTING -> IDLE.
        """
        if not self.is_active:
            return


        self._cancel.set()
        receiver = self._receiver
        if receiver is not None:
            receiver.stop()

    def wait(self, timeout=None):
        """Block until the worker thread finishes (for tests and shutdown). Returns True if it finished."""
        worker = self._worker
        if worker is None:
            return True

        worker.join(timeout)
        return not worker.is_alive()

    # Worker
    def _claim_run(self, settings):
        """Check the render rules and move to COMPILING under the lock, so two renders can't both start.

        The state change is announced (on_state) by _before_run on the worker.
        """
        with self._lock:
            self._check_can_render(settings)
            self._state = COMPILING
            self._cancel.clear()

    def _run_worker(self, settings, raise_errors=False):
        """Body of one render: _before_run -> _drive_link -> _after_run.

        Catches every exception so the worker never dies silently: on cancel,
        timeout or error it goes through _reset_and_idle and reports on_error.
        Always closes the link and log files. Returns the RunResult (or None).
        """
        self._run_start = time.monotonic()

        try:
            run_dir = self._before_run(settings)
            frame = self._drive_link(settings, run_dir)

            if self._cancel.is_set():
                raise _Cancelled()

            result = self._after_run(settings, run_dir, frame)


            if not result.complete:
                why = frame.error or frame.reason
                raise RunError(f"frame ended early ({why}): {result.pixels} of "
                               f"{result.expected} pixels; partial image saved in {run_dir}")

            self._close_logs()
            self._close_link()
            self._set_state(DONE)


            if self.on_done is not None:
                self.on_done(result)
            return result

        except _Cancelled:
            self._reset_and_idle(None)
            if raise_errors:
                raise RunError("run cancelled") from None
            return None

        except Exception as e:
            message = str(e) if isinstance(e, RunError) else f"{type(e).__name__}: {e}"
            self._reset_and_idle(message)
            if raise_errors:
                raise e if isinstance(e, RunError) else RunError(message) from e
            return None


        finally:
            self._receiver = None
            self._close_link()
            self._close_logs()

    def _check_can_render(self, settings=None):
        """Raise RunError if a run is active, no scene was compiled, or the last compile had errors.

        A render with settings.scene_path compiles that file itself, so it only
        needs the "not active" rule here.
        """
        if self.is_active:
            raise RunError(f"a run is already active ({self._state})")

        if settings is not None and settings.scene_path:
            return

        if self._check is None or self._scene_text is None:
            raise RunError("no scene has been compiled yet")

        if not self._check.ok:
            raise RunError(f"last compile had {len(self._check.errors)} error(s)")

    def _check_cancel(self):
        """Raise _Cancelled if cancel() was called."""
        if self._cancel.is_set():
            raise _Cancelled()

    def _before_run(self, settings):
        """COMPILING: recompile (applying settings.spp), make the run folder, save
        scene.txt and sram.hex, open the logs and write the "run_start" event.

        Returns the run folder path.
        """
        self._set_state(COMPILING)

        if settings.scene_path:
            with open(settings.scene_path, encoding="utf-8") as f:
                text = f.read()
            name = settings.scene_path
        else:
            text, name = self._scene_text, self._scene_name

        if settings.spp is not None:
            text = apply_spp(text, settings.spp)

        check = self.check_scene(text, rev=self._check.rev if self._check else None, filename=name)
        if not check.ok:
            rules = "; ".join(f"line {e.get('line')}: {e.get('rule')}" for e in check.errors)
            raise RunError(f"scene has errors: {rules}")
        self._check_cancel()

        run_dir = make_run_dir(settings.runs_dir, name)
        save_run_inputs(run_dir, text, self._hex)
        self._raw_log, self._event_log = open_run_logs(run_dir)

        transport.log_event(
            self._event_log, "run_start",
            run=os.path.basename(run_dir),
            scene=os.path.basename(name or "<scene>"),
            scene_sha1=hashlib.sha1(text.encode("utf-8")).hexdigest(),
            words=check.words, bvs=check.bvs, spp=settings.spp, passes=settings.passes,
            backend=settings.backend, port=settings.port, baud=settings.baud,
            host_commit=host_git_commit(),
        )

        return run_dir

    def _drive_link(self, settings, run_dir):
        """UPLOADING then RENDERING (stand-in for UartController.run).

        Open the link, reset the chip, start the PixelReceiver BEFORE sending
        anything, upload every word (transport.upload_scene), send RENDER
        (transport.start_render), then wait for the receiver's FrameResult.
        Repeats the render + receive for each pass and leaves the average of the
        passes in the frame buffer. Returns the last FrameResult.
        """
        self._check_cancel()
        self._set_state(UPLOADING)
        self._expected = settings.expected_pixels
        self._link = self.open_link(settings.backend, settings.port, settings.baud)
        self._reset_chip()

        fb = self.framebuffer
        passes = max(1, settings.passes)
        total = None
        bad_any = None
        frame = None

        for p in range(passes):
            self._check_cancel()
            fb.clear()

            receiver = PixelReceiver(
                self._link, fb, self._raw_log, self._event_log,
                on_row=self._on_row, gap_timeout=settings.timeout,
                expected_pixels=settings.expected_pixels, log_pixels=settings.log_pixels,
            )
            self._receiver = receiver
            receiver.start()
            self._check_cancel_and_stop(receiver)

            if p == 0:
                transport.log_event(self._event_log, "upload_start", words=len(self._words))
                transport.upload_scene(self._link, self._words, self._raw_log)
                sent = sum(len(transport.frame_object(w, a)) for a, w in enumerate(self._words))
                transport.log_event(self._event_log, "upload_done", bytes=sent)
                self._check_cancel_and_stop(receiver)
                self._set_state(RENDERING)

            self._render_start = time.monotonic()
            transport.start_render(self._link, raw_log=self._raw_log, event_log=self._event_log)

            frame = receiver.wait()
            self._receiver = None
            self._check_cancel()

            if passes > 1:
                total = fb.pixels.copy() if total is None else total + fb.pixels
                bad_any = fb.bad.copy() if bad_any is None else (bad_any | fb.bad)

            if not frame.complete:
                break

        if total is not None:
            fb.pixels[:] = total / (p + 1)
            fb.bad[:] = bad_any

        return frame

    def _check_cancel_and_stop(self, receiver):
        """Like _check_cancel, but stops a running receiver first (cancel can land between steps)."""
        if self._cancel.is_set():
            receiver.stop()
            receiver.wait(2.0)
            raise _Cancelled()

    def _after_run(self, settings, run_dir, frame_result):
        """PROCESSING: run the image pipeline on the frame buffer (fill bad pixels,
        sRGB gamma), save render.png, render_raw.png and render_linear.npy, and
        write the "summary" event.

        Returns the RunResult. A short frame is still saved but reported as not
        complete; the worker decides between DONE and an error.
        """
        self._set_state(PROCESSING)
        fb = self.framebuffer
        linear = fb.pixels.copy()
        bad_mask = fb.bad.copy()

        np.save(os.path.join(run_dir, "render_linear.npy"), linear)
        save_png(os.path.join(run_dir, "render_raw.png"), to_uint8(linear))

        processed = fill_bad_pixels(linear, bad_mask)
        png_path = os.path.join(run_dir, "render.png")
        total_s = time.monotonic() - self._run_start
        bad_list = [int(i) for i in np.flatnonzero(bad_mask)]

        save_png(png_path, linear_to_srgb8(processed), metadata={
            "Software": "tthost",
            "Scene": self._scene_text or "",
            "Settings": f"backend={settings.backend} spp={settings.spp} passes={settings.passes}",
            "Pixels": f"{frame_result.pixels}/{frame_result.expected}",
            "BadPixels": " ".join(map(str, bad_list)),
            "TotalSeconds": f"{total_s:.1f}",
        })

        render_s = time.monotonic() - (self._render_start or self._run_start)
        transport.log_event(
            self._event_log, "summary",
            total_s=round(total_s, 1),
            px_per_s=round(frame_result.pixels / render_s, 1) if render_s > 0 else None,
            bad=len(bad_list), png="render.png",
        )

        return RunResult(
            run_dir=run_dir,
            png=png_path,
            pixels=frame_result.pixels,
            expected=frame_result.expected,
            bad=len(bad_list),
            complete=frame_result.complete,
            total_s=total_s,
        )

    def _on_row(self, y, pixels, bad):
        """Receiver row callback: build a Progress (with ETA) and send it to on_progress."""
        if self.on_progress is None:
            return

        elapsed = time.monotonic() - (self._render_start or self._run_start)
        eta = estimate_eta(pixels, elapsed, self._expected)
        self.on_progress(Progress(row=y, pixels=pixels, eta_s=eta, bad=bad))

    def _reset_chip(self):
        """Reset the chip so the next run starts clean.

        Calls link.reset() when the link has one (fake chips, future backends).
        How the real board is reset (rst_n / a serial control line) is an open
        HW question, so a plain pyserial port only gets its input buffer
        flushed and the event log notes that no reset happened.
        """
        link = self._link
        if link is None:
            return

        reset = getattr(link, "reset", None)
        if callable(reset):
            reset()
            transport.log_raw(self._raw_log, "rst", b"\x00")
            transport.log_event(self._event_log, "reset")
        else:
            transport.log_event(self._event_log, "reset", supported=False)

        drop_input = getattr(link, "reset_input_buffer", None)
        if callable(drop_input):
            drop_input()

    def _reset_and_idle(self, message=None):
        """RESETTING: stop the receiver, reset the chip, close the link, then IDLE.

        With no link open there is nothing to reset, so the state goes to ERROR
        instead (for an error) or IDLE (for a cancel). Reports `message` through
        on_error when given (timeout or error, not plain cancel).
        """
        receiver = self._receiver
        if receiver is not None:
            receiver.stop()
            receiver.wait(2.0)
            self._receiver = None

        if self._link is not None:
            self._set_state(RESETTING)
            try:
                self._reset_chip()
            except Exception as e:
                transport.log_event(self._event_log, "anomaly", kind="reset_failed", message=str(e))
            self._close_link()
            self._set_state(IDLE)
        else:
            self._set_state(ERROR if message else IDLE)

        if message is not None:
            transport.log_event(self._event_log, "error", message=message)
            if self.on_error is not None:
                self.on_error(message)

    def _set_state(self, state):
        """Change state, write a "state" event to the event log and call on_state."""
        self._state = state
        transport.log_event(self._event_log, "state", state=state)

        if self.on_state is not None:
            self.on_state(state)

    def _close_link(self):
        """Close the link if one is open (ignores errors from an already-dead link)."""
        link, self._link = self._link, None
        if link is not None:
            try:
                transport.close_link(link)
            except Exception:
                pass

    def _close_logs(self):
        """Close the raw byte log and event log if they are open."""
        for name in ("_raw_log", "_event_log"):
            f = getattr(self, name)
            setattr(self, name, None)
            if f is not None:
                f.close()


# Compiler hook
def compile_text(text, filename="<scene>"):
    """Default compiler hook: run scene_compiler on scene text.

    Returns (words, report) where report has words, bvs, errors, warnings and
    hex (the sram.hex text). words is None when there are errors. Never raises
    for a bad scene: every broken rule is one entry in report["errors"].
    """
    return scene_compiler.compile_text(text, filename)


def simple_hex_dump(words):
    """Fallback sram.hex: one "addr word" line per word, used until scene_compiler.hex_dump exists."""
    lines = ["# tthost sram image v1", f"# words {len(words)} / {scene_compiler.SRAM_WORDS}"]
    lines += [f"{a:03X} {w:04X}" for a, w in enumerate(words)]

    return "\n".join(lines) + "\n"


def apply_spp(text, spp):
    """Return scene text with its spp line set to `spp` (added after the image line if missing).

    Validation (power of 2, at most 32) is left to the compiler.
    """
    lines = text.splitlines()
    for i, line in enumerate(lines):
        fields = line.split("#", 1)[0].split()
        if fields and fields[0] == "spp":
            lines[i] = f"spp {spp}"
            return "\n".join(lines) + "\n"

    insert_at = 0
    for i, line in enumerate(lines):
        fields = line.split("#", 1)[0].split()
        if fields and fields[0] == "image":
            insert_at = i + 1
            break

    lines.insert(insert_at, f"spp {spp}")
    return "\n".join(lines) + "\n"


# Helpers (no controller state; easy to test alone)
def make_run_dir(runs_dir, scene_path, now=None):
    """Create and return runs/<YYYY-MM-DD_HHMMSS>_<scene name>/ (section 12.1).

    `now` is a datetime for tests; defaults to the current time. Adds a _1, _2...
    suffix if the folder already exists rather than overwriting a previous run.
    """
    now = now or datetime.now()
    stem = Path(scene_path).stem if scene_path and not scene_path.startswith("<") else "scene"
    base = os.path.join(runs_dir, f"{now:%Y-%m-%d_%H%M%S}_{stem}")
    path = base
    n = 0

    while True:
        try:
            os.makedirs(path)
            return path
        except FileExistsError:
            n += 1
            path = f"{base}_{n}"


def save_run_inputs(run_dir, scene_text, hex_dump):
    """Write scene.txt (exact text compiled) and sram.hex (memory image) into the run folder."""
    with open(os.path.join(run_dir, "scene.txt"), "w", encoding="utf-8") as f:
        f.write(scene_text)

    with open(os.path.join(run_dir, "sram.hex"), "w", encoding="utf-8") as f:
        f.write(hex_dump or "")


def open_run_logs(run_dir):
    """Open raw_bytes.log and events.jsonl in the run folder for writing.

    Returns (raw_log, event_log), both open text files the caller must close.
    """
    raw_log = open(os.path.join(run_dir, "raw_bytes.log"), "w", encoding="utf-8")
    raw_log.write("# tthost raw byte log v1\n")
    raw_log.write("# <unix time> <tx|rx|rst> <hex bytes, before unescaping>\n")
    raw_log.flush()

    event_log = open(os.path.join(run_dir, "events.jsonl"), "w", encoding="utf-8")

    return raw_log, event_log


def estimate_eta(pixels_done, elapsed_s, total=TOTAL_PIXELS):
    """Seconds left at the current pixel rate, or None before any pixels have arrived."""
    if pixels_done <= 0 or elapsed_s <= 0:
        return None

    rate = pixels_done / elapsed_s
    return max(0.0, (total - pixels_done) / rate)


def host_git_commit():
    """Short git commit of the host code, for the run_start event; None if git is unavailable."""
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=os.path.dirname(os.path.abspath(__file__)),
                             capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None

    return out.stdout.strip() or None


# Minimal image pipeline (stand-in for image/; linear until the last step)
def to_uint8(linear):
    """Round and clamp a linear 0-255 float image to uint8 (no gamma): the chip values as received."""
    return np.clip(np.rint(linear), 0, 255).astype(np.uint8)


def fill_bad_pixels(linear, bad_mask):
    """Return a copy with each bad pixel replaced by the mean of its good 8-neighbours."""
    out = linear.copy()
    h, w = bad_mask.shape

    for y, x in zip(*np.nonzero(bad_mask)):
        y0, y1 = max(0, y - 1), min(h, y + 2)
        x0, x1 = max(0, x - 1), min(w, x + 2)
        good = ~bad_mask[y0:y1, x0:x1]
        if good.any():
            out[y, x] = linear[y0:y1, x0:x1][good].mean(axis=0)

    return out


def linear_to_srgb8(linear):
    """Linear 0-255 values -> sRGB-encoded uint8 (design doc section 4.5)."""
    c = np.clip(linear / 255.0, 0.0, 1.0)
    srgb = np.where(c <= 0.0031308, 12.92 * c, 1.055 * np.power(c, 1 / 2.4) - 0.055)

    return np.clip(np.rint(srgb * 255.0), 0, 255).astype(np.uint8)


def save_png(path, image_u8, metadata=None):
    """Save an (H, W, 3) uint8 image as PNG, with `metadata` as PNG text chunks."""
    from PIL import Image, PngImagePlugin

    info = PngImagePlugin.PngInfo()

    for key, value in (metadata or {}).items():
        info.add_text(key, str(value))

    Image.fromarray(image_u8).save(path, pnginfo=info)
