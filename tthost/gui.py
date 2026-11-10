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

The run controller (session.py) does not exist yet, so Send starts a
placeholder worker that fills the frame with a test pattern. Replace
`_placeholder_render` with a call into the run controller when it lands.
"""

import collections
import os
import threading
import time

import numpy as np
from OpenGL import GL
from imgui_bundle import hello_imgui, imgui, immapp, portable_file_dialogs as pfd

from tthost.transport import BACKENDS, DEFAULT_BAUD, IMAGE_SIZE

TOTAL_PIXELS = IMAGE_SIZE * IMAGE_SIZE
LOG_LINES = 500           # event log lines kept in the panel
PANEL_WIDTH = 420         # run panel width in pixels
LOG_WIDTH = 380           # event log column width in pixels

# run controller states (design doc, figure 8.1)
IDLE, COMPILING, UPLOADING, RENDERING, PROCESSING, DONE, ERROR = (
    "idle", "compiling", "uploading", "rendering", "processing", "done", "error")
ACTIVE_STATES = (COMPILING, UPLOADING, RENDERING, PROCESSING)


class FrameBuffer:
    """Linear float32 512 x 512 RGB image plus a received count and bad-pixel mask."""

    def __init__(self):
        self.pixels = np.zeros((IMAGE_SIZE, IMAGE_SIZE, 3), dtype=np.float32)
        self.bad = np.zeros((IMAGE_SIZE, IMAGE_SIZE), dtype=bool)
        self.count = 0

    def clear(self):
        self.pixels[:] = 0
        self.bad[:] = False
        self.count = 0


class GuiState:
    """Everything the window shows. Rebuilt into widgets every frame (immediate mode)."""

    def __init__(self):
        # loading
        self.scene_path = ""
        self.scene_text = ""
        self.scene_status = "no scene loaded"
        self.open_dialog = None

        # scene designer (Rust app) link over pipes: it sends START, we answer ACK.
        # Nothing sets this yet; the pipe handler will flip it when the handshake completes.
        self.designer_connected = False

        # link and render settings
        self.backend_idx = 0
        self.port = "/dev/ttyUSB0"
        self.baud = DEFAULT_BAUD

        # run
        self.state = IDLE
        self.fb = FrameBuffer()
        self.start_time = None
        self.end_time = None
        self.cancel_event = threading.Event()
        self.worker = None

        # log
        self.log = collections.deque(maxlen=LOG_LINES)
        self.log_autoscroll = True

        # pipe IPC log: messages to/from the scene designer. Nothing feeds it yet;
        # the pipe handler will call add_ipc_log() for every message in each direction.
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
        lines = self.scene_text.count("\n") + 1
        self.scene_status = f"loaded {os.path.basename(path)} ({lines} lines)"
        self.add_log(f"scene loaded: {path}")


# -- run control ----------------------------------------------------------------

def start_render(s):
    s.fb.clear()
    s.cancel_event.clear()
    s.start_time = time.monotonic()
    s.end_time = None
    s.add_log(f"render: backend={BACKENDS[s.backend_idx]} port={s.port} baud={s.baud}")
    s.worker = threading.Thread(target=_placeholder_render, args=(s,), daemon=True)
    s.worker.start()


def cancel_render(s):
    s.cancel_event.set()
    s.add_log("cancel requested")


def _placeholder_render(s):
    """Stand-in for the run controller: walks the states and fills a test pattern row by row."""
    s.state = COMPILING
    s.add_log("state: compiling")
    time.sleep(0.2)
    s.state = UPLOADING
    s.add_log("state: uploading")
    time.sleep(0.2)
    s.state = RENDERING
    s.add_log("state: rendering")

    xs = np.arange(IMAGE_SIZE, dtype=np.float32)
    for y in range(IMAGE_SIZE):
        if s.cancel_event.is_set():
            s.state = IDLE
            s.end_time = time.monotonic()
            s.add_log(f"cancelled at row {y}")
            return
        row = s.fb.pixels[y]
        row[:, 0] = xs / IMAGE_SIZE * 255
        row[:, 1] = y / IMAGE_SIZE * 255
        row[:, 2] = ((xs // 32 + y // 32) % 2) * 128 + 64
        s.fb.count += IMAGE_SIZE
        s.add_log(f"row {y}")
        time.sleep(0.01)

    s.state = PROCESSING
    s.add_log("state: processing")
    time.sleep(0.2)
    s.state = DONE
    s.end_time = time.monotonic()
    s.add_log(f"frame_end pixels={s.fb.count} expected={TOTAL_PIXELS} bad={int(s.fb.bad.sum())}")


# -- panels ------------------------------------------------------------------------

def draw_designer_status(s):
    """One line under the scene row: is the scene designer connected over the pipes?"""
    if s.designer_connected:
        color, label = imgui.ImVec4(0.30, 0.85, 0.40, 1.0), "connected"
    else:
        color, label = imgui.ImVec4(0.85, 0.30, 0.30, 1.0), "not connected"
    imgui.text("Scene designer:")
    imgui.same_line()
    imgui.text_colored(color, label)
    if imgui.is_item_hovered():
        imgui.set_tooltip("Pipe link to the scene designer.\n"
                          "Connected once it sends START and we reply ACK.")


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
    imgui.set_next_item_width(-80)
    _, s.baud = imgui.input_int("Baud", s.baud, 0)
    imgui.end_disabled()


def draw_send_section(s):
    imgui.separator_text("Render")
    can_send = bool(s.scene_text) and not s.is_active()
    imgui.begin_disabled(not can_send)
    if imgui.button("Send", imgui.ImVec2(120, 0)):
        start_render(s)
    imgui.end_disabled()
    if not s.scene_text and imgui.is_item_hovered(imgui.HoveredFlags_.allow_when_disabled):
        imgui.set_tooltip("Load a scene first")
    imgui.same_line()
    imgui.begin_disabled(not s.is_active())
    if imgui.button("Cancel", imgui.ImVec2(120, 0)):
        cancel_render(s)
    imgui.end_disabled()
    imgui.same_line()
    imgui.text(f"state: {s.state}")

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


def run(scene_path=None):
    """Open the TinyTracer window and block until it is closed."""
    state = GuiState()
    if scene_path:
        state.load_scene(scene_path)
    params = hello_imgui.RunnerParams()
    params.app_window_params.window_title = "TinyTracer"
    params.app_window_params.window_geometry.size = (1600, 820)
    params.fps_idling.enable_idling = False

    # draw() makes its own full-screen window; hello_imgui's default one would sit on top and eat input
    params.imgui_window_params.default_imgui_window_type = hello_imgui.DefaultImGuiWindowType.no_default_window
    params.callbacks.show_gui = lambda: draw(state)
    immapp.run(params)
    state.cancel_event.set()
