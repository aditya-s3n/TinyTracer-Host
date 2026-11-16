"""TinyTracer app window: run panel and live render view (see design doc, section 8.1).

One ImGui window (imgui-bundle) with three columns:

    Run panel         scene loading, backend picker, Send (render) and Cancel,
                      progress, pixels, current row, elapsed, ETA, pixel rate,
                      bad pixel count, and the pipe IPC log (raw traffic with
                      the Rust scene designer).
    Live render view  the 512 x 512 frame filling in row by row, with a marker
                      line on the current row. Shows the raw chip values; the
                      processed (sRGB) view comes later.
    Event log         the latest event log lines, beside the render view.

The frame buffer is a linear float32 numpy array shared with the worker
thread: only the worker writes it, only the window reads it, and the texture
is re-uploaded whenever the received pixel count changes.

Send and Cancel go through the run controller (run_controller.py), which
compiles the scene, drives the UART link and fills the frame buffer. Its
callbacks arrive on background threads; they only set plain attributes and
append to the logs, which the window then reads on its next frame.

When the scene designer started this app (--ipc), the same callbacks also send
it state, progress, done and error messages, and its scene, render and cancel
messages land in the on_designer_* methods below.
"""

import collections
import os
import sys
import time

import numpy as np
from OpenGL import GL
from imgui_bundle import hello_imgui, imgui, immapp, portable_file_dialogs as pfd

from tthost import __version__
from tthost.framebuffer import FrameBuffer
from tthost.ipc import DesignerLink
from tthost.run_controller import ACTIVE_STATES, DONE, ERROR, IDLE, RunController, RunError, RunSettings
from tthost.transport import BACKENDS, DEFAULT_BAUD, FAKE_CHIP, IMAGE_SIZE

TOTAL_PIXELS = IMAGE_SIZE * IMAGE_SIZE
LOG_LINES = 500           # event log lines kept in the panel
PANEL_WIDTH = 420         # run panel width in pixels
LOG_WIDTH = 380           # event log column width in pixels

GOOD = (0.30, 0.85, 0.40, 1.0)
BAD = (0.90, 0.35, 0.30, 1.0)


class GuiState:
    """Everything the window shows. Rebuilt into widgets every frame (immediate mode)."""

    def __init__(self):
        # loading
        self.scene_path = ""
        self.scene_text = ""
        self.scene_status = "no scene loaded"
        self.open_dialog = None

        # scene designer (Rust app) link over pipes: it sends hello, we answer ready.
        # Set by the pipe handler (tthost/ipc.py) on its reader thread.
        self.designer_connected = False
        self.designer_closed = False   # the designer closed our stdin: time to exit
        self.link = None               # DesignerLink when started with --ipc

        # link and render settings
        self.backend_idx = 0
        self.port = "/dev/ttyUSB0"
        self.baud = DEFAULT_BAUD

        # run: the controller owns the render; the window shares its frame buffer
        self.fb = FrameBuffer()
        self.controller = RunController(
            on_state=self._on_state, on_progress=self._on_progress,
            on_done=self._on_done, on_error=self._on_error, framebuffer=self.fb)
        self.check = None              # the compiler's verdict on the loaded scene
        self.start_time = None
        self.end_time = None
        self.last_error = ""
        self.last_png = ""

        # log
        self.log = collections.deque(maxlen=LOG_LINES)
        self.log_autoscroll = True

        # pipe IPC log: every message to/from the scene designer, fed by the pipe handler.
        self.app_start = time.monotonic()
        self.ipc_log = collections.deque(maxlen=LOG_LINES)
        self.ipc_log_autoscroll = True

        # live view
        self.texture_id = None
        self.uploaded_count = -1

    # -- helpers --------------------------------------------------------------

    def add_log(self, message):
        t = 0.0 if self.start_time is None else time.monotonic() - self.start_time
        self.log.append(f"{t:9.3f}  {message}")

    def add_ipc_log(self, direction, message):
        """Record one pipe message. `direction` is "rx" (from the designer) or "tx" (to it).

        Safe to call from the pipe reader thread (deque.append is atomic).
        """
        t = time.monotonic() - self.app_start
        arrow = "<-" if direction == "rx" else "->"
        self.ipc_log.append(f"{t:9.3f}  {arrow} {message}")

    @property
    def state(self):
        return self.controller.state

    def is_active(self):
        return self.state in ACTIVE_STATES

    def elapsed(self):
        if self.start_time is None:
            return 0.0
        end = self.end_time if self.end_time is not None else time.monotonic()
        return end - self.start_time

    def load_scene(self, path):
        try:
            with open(path, encoding="utf-8") as f:
                self.scene_text = f.read()
        except OSError as e:
            self.scene_text = ""
            self.scene_status = f"error: {e.strerror}"
            self.add_log(f"load failed: {path}: {e.strerror}")
            return
        self.scene_path = path
        self.add_log(f"scene loaded: {path}")
        self.check_scene(self.scene_text, name=path)

    def check_scene(self, text, rev=None, name="<scene>"):
        """Compile scene text, remember it for the next render and show the verdict."""
        self.check = check = self.controller.check_scene(text, rev=rev, filename=name)
        label = os.path.basename(name) if not name.startswith("<") else "scene"
        if check.ok:
            self.scene_status = f"{label}: {check.words} of 512 words, {check.bvs} BVs"
            self.add_log(f"compiled: {check.words} words, {check.bvs} BVs")
        else:
            self.scene_status = f"{label}: {len(check.errors)} error(s), see the event log"
        for error in check.errors:
            where = f"line {error['line']}: " if error.get("line") else ""
            self.add_log(f"scene error: {where}{error['rule']}")
        for warning in check.warnings:
            self.add_log(f"scene warning: {warning}")
        return check

    # -- run controller callbacks (called on the run worker / serial reader threads) --

    def _on_state(self, state):
        self.add_log(f"state: {state}")
        if state in (IDLE, DONE, ERROR) and self.start_time is not None and self.end_time is None:
            self.end_time = time.monotonic()
        self.send_to_designer({"type": "state", "state": state})

    def _on_progress(self, progress):
        if progress.row % 32 == 31:
            self.add_log(f"row {progress.row + 1} of {IMAGE_SIZE}")
        eta = None if progress.eta_s is None else round(progress.eta_s, 1)
        self.send_to_designer({"type": "progress", "row": progress.row, "pixels": progress.pixels,
                               "eta_s": eta, "bad": progress.bad})

    def _on_done(self, result):
        # Absolute paths: the designer may not share this app's working directory.
        png = os.path.abspath(result.png) if result.png else ""
        self.last_png = png
        log = os.path.abspath(result.log) if result.log else ""
        self.add_log(f"done: {result.pixels} pixels, {result.bad} bad, saved {png}")
        self.add_log(f"run log: {log}")
        self.send_to_designer({"type": "done", "png": png, "run_dir": os.path.abspath(result.run_dir),
                               "log": log, "bad": result.bad})

    def _on_error(self, message):
        self.last_error = message
        self.add_log(f"error: {message}")
        self.send_to_designer({"type": "error", "message": message})

    def send_to_designer(self, message):
        if self.link is not None and self.link.connected:
            self.link.send(message)

    # -- scene designer link (called on the pipe reader thread) ---------------

    def on_designer_scene(self, rev, text):
        """A "scene" message arrived: show it as the loaded scene and return the compiler's verdict."""
        if self.is_active():
            return {"errors": [{"line": None, "rule": "a render is running; scene not loaded"}]}
        self.scene_text = text
        self.scene_path = ""
        self.add_log(f"scene received from designer: rev {rev}")
        check = self.check_scene(text, rev=rev, name="designer.txt")
        return {"words": check.words, "bvs": check.bvs, "errors": check.errors, "warnings": check.warnings}

    def on_designer_render(self, message):
        """A "render" message arrived: start a render of the last scene. Returns an error text or None.

        The link settings (backend, port, baud) are the ones chosen in this window,
        unless the message names a backend (and port), which then replace them.
        """
        backend = message.get("backend")
        if backend is not None:
            if backend not in BACKENDS:
                return f"unknown backend {backend!r}; expected one of {', '.join(BACKENDS)}"
            self.backend_idx = BACKENDS.index(backend)
            self.port = message.get("port") or ""
        return start_render(self, spp=message.get("spp"), passes=message.get("passes") or 1)

    def on_designer_cancel(self):
        cancel_render(self)

    def on_designer_connected(self):
        self.designer_connected = True
        self.add_log("scene designer connected")

    def on_designer_closed(self):
        self.designer_connected = False
        self.designer_closed = True


# -- run control ----------------------------------------------------------------

def start_render(s, spp=None, passes=1):
    """Render the loaded scene with the link settings from the window.

    Returns None when the run started, or the reason it could not (also shown in the window).
    """
    settings = RunSettings(backend=BACKENDS[s.backend_idx], port=s.port.strip() or None, baud=s.baud,
                           spp=spp, passes=passes)
    s.add_log(f"render: backend={settings.backend} port={settings.port} baud={settings.baud}")
    try:
        s.controller.render(settings)
    except RunError as e:
        s.last_error = str(e)
        s.add_log(f"render refused: {e}")
        return str(e)
    s.last_error = ""
    s.last_png = ""
    s.start_time = time.monotonic()
    s.end_time = None
    return None


def cancel_render(s):
    s.add_log("cancel requested")
    s.controller.cancel()


# -- panels ------------------------------------------------------------------------

def draw_designer_status(s):
    """One line under the scene row: is the scene designer connected over the pipes?"""
    if s.designer_connected:
        color, label = imgui.ImVec4(*GOOD), "connected"
    else:
        color, label = imgui.ImVec4(*BAD), "not connected"
    imgui.text("Scene designer:")
    imgui.same_line()
    imgui.text_colored(color, label)
    if imgui.is_item_hovered():
        imgui.set_tooltip("Pipe link to the scene designer.\n"
                          "Connected once it sends hello and we reply ready.")


def draw_loading_section(s):
    imgui.separator_text("Scene")
    imgui.set_next_item_width(-160)
    _, s.scene_path = imgui.input_text("##scene_path", s.scene_path)
    imgui.same_line()
    if imgui.button("Browse..."):
        s.open_dialog = pfd.open_file("Open scene", os.getcwd(), ["Scene files", "*.txt", "All files", "*"])
    imgui.same_line()
    imgui.begin_disabled(not s.scene_path or s.is_active())
    if imgui.button("Load"):
        s.load_scene(s.scene_path)
    imgui.end_disabled()

    draw_designer_status(s)

    if s.open_dialog is not None and s.open_dialog.ready():
        result = s.open_dialog.result()
        s.open_dialog = None
        if result:
            s.load_scene(result[0])

    imgui.text_disabled(s.scene_status)
    if s.scene_text and imgui.tree_node("Scene text"):
        imgui.input_text_multiline("##scene_text", s.scene_text, imgui.ImVec2(-1, 140),
                                   imgui.InputTextFlags_.read_only)
        imgui.tree_pop()

    imgui.separator_text("Link")
    imgui.begin_disabled(s.is_active())
    imgui.set_next_item_width(-80)
    _, s.backend_idx = imgui.combo("Backend", s.backend_idx, list(BACKENDS))
    imgui.set_next_item_width(-80)
    _, s.port = imgui.input_text("Port", s.port)
    if imgui.is_item_hovered():
        imgui.set_tooltip("chip: serial port, e.g. /dev/ttyUSB0\n"
                          f"sim: program to run, or '{FAKE_CHIP}' for the stand-in chip\n"
                          "rtl: pseudo-terminal path\n"
                          "replay: a raw_bytes.log from an earlier run")
    imgui.set_next_item_width(-80)
    _, s.baud = imgui.input_int("Baud", s.baud, 0)
    if imgui.small_button("Use stand-in chip (no hardware)"):
        s.backend_idx, s.port = BACKENDS.index("sim"), FAKE_CHIP
    imgui.end_disabled()


def draw_send_section(s):
    imgui.separator_text("Render")
    scene_ok = s.check is not None and s.check.ok
    imgui.begin_disabled(not scene_ok or s.is_active())
    if imgui.button("Send", imgui.ImVec2(120, 0)):
        start_render(s)
    imgui.end_disabled()
    if not scene_ok and imgui.is_item_hovered(imgui.HoveredFlags_.allow_when_disabled):
        imgui.set_tooltip("Load a scene that compiles without errors first")
    imgui.same_line()
    imgui.begin_disabled(not s.is_active())
    if imgui.button("Cancel", imgui.ImVec2(120, 0)):
        cancel_render(s)
    imgui.end_disabled()
    imgui.same_line()
    imgui.text(f"state: {s.state}")

    if s.last_error:
        imgui.push_text_wrap_pos(0.0)
        imgui.text_colored(imgui.ImVec4(*BAD), s.last_error)
        imgui.pop_text_wrap_pos()
    elif s.last_png:
        imgui.push_text_wrap_pos(0.0)
        imgui.text_colored(imgui.ImVec4(*GOOD), f"saved {s.last_png}")
        imgui.pop_text_wrap_pos()

    count = s.fb.count
    elapsed = s.elapsed()
    rate = count / elapsed if elapsed > 0 else 0.0
    eta = (TOTAL_PIXELS - count) / rate if rate > 0 and count < TOTAL_PIXELS else 0.0
    imgui.progress_bar(count / TOTAL_PIXELS, imgui.ImVec2(-1, 0),
                       f"{100 * count / TOTAL_PIXELS:.1f}%")

    if imgui.begin_table("stats", 2):
        for label, value in (
            ("Pixels", f"{count:,} / {TOTAL_PIXELS:,}"),
            ("Row", f"{min(count // IMAGE_SIZE, IMAGE_SIZE - 1) if count else '-'}"),
            ("Elapsed", f"{elapsed:.1f} s"),
            ("ETA", f"{eta:.1f} s" if s.is_active() else "-"),
            ("Rate", f"{rate:,.0f} px/s"),
            ("Noisy pixels", f"{int(s.fb.bad.sum())}"),
        ):
            imgui.table_next_row()
            imgui.table_next_column()
            imgui.text_disabled(label)
            imgui.table_next_column()
            imgui.text(value)
        imgui.end_table()


def draw_scrolling_log(title, child_id, lines, autoscroll):
    """Header, Clear + Auto-scroll controls and a bordered child filling the remaining space.

    Returns the new auto-scroll setting.
    """
    imgui.separator_text(title)
    imgui.push_id(child_id)
    if imgui.small_button("Clear"):
        lines.clear()
    imgui.same_line()
    _, autoscroll = imgui.checkbox("Auto-scroll", autoscroll)
    imgui.begin_child(child_id, imgui.ImVec2(-1, -1), imgui.ChildFlags_.borders,
                      imgui.WindowFlags_.horizontal_scrollbar)
    for line in list(lines):
        imgui.text_unformatted(line)
    if autoscroll and imgui.get_scroll_y() >= imgui.get_scroll_max_y() - 1:
        imgui.set_scroll_here_y(1.0)
    imgui.end_child()
    imgui.pop_id()
    return autoscroll


def draw_log_section(s):
    s.log_autoscroll = draw_scrolling_log("Event log", "log", s.log, s.log_autoscroll)


def draw_ipc_log_section(s):
    s.ipc_log_autoscroll = draw_scrolling_log("Pipe IPC log", "ipc_log", s.ipc_log,
                                              s.ipc_log_autoscroll)


def upload_texture(s):
    """Create the texture once, re-upload when new pixels arrived."""
    if s.texture_id is None:
        s.texture_id = GL.glGenTextures(1)
        GL.glBindTexture(GL.GL_TEXTURE_2D, s.texture_id)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
        GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_NEAREST)
        GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
        GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGB, IMAGE_SIZE, IMAGE_SIZE, 0,
                        GL.GL_RGB, GL.GL_UNSIGNED_BYTE, None)

    count = s.fb.count
    if count == s.uploaded_count:
        return
    image = np.clip(s.fb.pixels, 0, 255).astype(np.uint8)
    GL.glBindTexture(GL.GL_TEXTURE_2D, s.texture_id)
    GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
    GL.glTexSubImage2D(GL.GL_TEXTURE_2D, 0, 0, 0, IMAGE_SIZE, IMAGE_SIZE,
                       GL.GL_RGB, GL.GL_UNSIGNED_BYTE, np.ascontiguousarray(image))
    s.uploaded_count = count


def draw_live_view(s):
    imgui.separator_text("Live render view")
    upload_texture(s)

    # largest square that fits the remaining space
    avail = imgui.get_content_region_avail()
    side = max(64.0, min(avail.x, avail.y))
    origin = imgui.get_cursor_screen_pos()
    imgui.image(imgui.ImTextureRef(int(s.texture_id)), imgui.ImVec2(side, side))

    # marker line on the row currently being received
    count = s.fb.count
    if s.is_active() and 0 < count < TOTAL_PIXELS:
        row = count // IMAGE_SIZE
        y = origin.y + (row + 0.5) * side / IMAGE_SIZE
        imgui.get_window_draw_list().add_line(
            imgui.ImVec2(origin.x, y), imgui.ImVec2(origin.x + side, y),
            imgui.get_color_u32(imgui.ImVec4(1.0, 0.25, 0.25, 0.9)), 1.5)


def draw(s):
    if s.designer_closed:
        # We are the designer's child process: when it goes away, so do we.
        hello_imgui.get_runner_params().app_shall_exit = True

    viewport = imgui.get_main_viewport()
    imgui.set_next_window_pos(viewport.work_pos)
    imgui.set_next_window_size(viewport.work_size)
    flags = (imgui.WindowFlags_.no_decoration | imgui.WindowFlags_.no_move
             | imgui.WindowFlags_.no_saved_settings | imgui.WindowFlags_.no_bring_to_front_on_focus)
    imgui.begin("TinyTracer", None, flags)

    imgui.begin_child("run_panel", imgui.ImVec2(PANEL_WIDTH, 0), imgui.ChildFlags_.resize_x)
    draw_loading_section(s)
    draw_send_section(s)
    draw_ipc_log_section(s)
    imgui.end_child()

    imgui.same_line()

    # live view takes whatever width is left after the log column
    spacing = imgui.get_style().item_spacing.x
    view_width = max(64.0, imgui.get_content_region_avail().x - LOG_WIDTH - spacing)
    imgui.begin_child("live_view", imgui.ImVec2(view_width, 0))
    draw_live_view(s)
    imgui.end_child()

    imgui.same_line()

    imgui.begin_child("log_panel", imgui.ImVec2(0, 0))
    draw_log_section(s)
    imgui.end_child()

    imgui.end()


def run(scene_path=None, ipc_out=None):
    """Open the TinyTracer window and block until it is closed.

    `ipc_out` is the protocol stream from ipc.claim_stdout() when the scene
    designer started this app; None when the app runs on its own.
    """
    state = GuiState()
    if scene_path:
        state.load_scene(scene_path)
    if ipc_out is not None:
        state.link = DesignerLink(sys.stdin, ipc_out, version=__version__,
                                  on_scene=state.on_designer_scene,
                                  on_render=state.on_designer_render,
                                  on_cancel=state.on_designer_cancel,
                                  on_connected=state.on_designer_connected,
                                  on_closed=state.on_designer_closed,
                                  on_log=state.add_ipc_log)
        state.link.start()
    params = hello_imgui.RunnerParams()
    params.app_window_params.window_title = "TinyTracer"
    params.app_window_params.window_geometry.size = (1600, 820)
    params.fps_idling.enable_idling = False

    # draw() makes its own full-screen window; hello_imgui's default one would sit on top and eat input
    params.imgui_window_params.default_imgui_window_type = hello_imgui.DefaultImGuiWindowType.no_default_window
    params.callbacks.show_gui = lambda: draw(state)
    immapp.run(params)
    # Window closed: stop a render in progress so the chip is reset and the logs are closed.
    state.controller.cancel()
    state.controller.wait(3.0)
