"""Stand-in chip: speaks the TinyTracer UART protocol on stdin / stdout.

    python -m tthost.fake_chip            (or use it as the "sim" backend's program)

For testing the host from top to bottom with no hardware. It does what the chip
does on the wire: takes OBJECT messages into a 512-word memory, and on RENDER
sends back 512 x 512 PIXEL messages, row by row, with the chip's escaping.

It is NOT the chip's renderer. To have a picture to send, it decodes its memory
with the host's own decoder and draws a plain raycast (first hit, one fixed
light, no bounces, no noise). So it proves the framing, upload, receive and
image steps, and that a compiled scene decodes back to something sensible. It
reads memory with the host's own decoder, so an encoding mistake the encoder
and decoder share would not show up here. Use the team's C++ model or the RTL
for that. The sky, the ground checker and glow follow the chip's formulas
(Rendering Flow 5f, 5h), so framing and flat colours are comparable.

Set FAKE_CHIP_ROW_MS to pause that many milliseconds after each row, to watch
the live view fill in at something like the chip's pace.
"""

import os
import sys
import time

import numpy as np

from tthost import scene_compiler
from tthost.receive import escape_pixel
from tthost.transport import ESCAPE, IMAGE_SIZE, OBJECT_START, RENDER_START, SRAM_WORDS

CHECKER_SIZE = 4.0
MAX_DISTANCE = scene_compiler.MAX_DISTANCE
LIGHT = np.array([0.4, -0.6, 0.8]) / np.linalg.norm([0.4, -0.6, 0.8])
EPS = 1e-4


class FrameParser:
    """Host -> chip framing: a start byte (0x00 RENDER, 0x01 OBJECT) and four data bytes.

    The host escapes data bytes 0x00, 0x01 and 0x03 with 0x03, so a bare 0x00 or
    0x01 is always the start of a message, even in the middle of a broken one.
    """

    def __init__(self):
        self.kind = None
        self.data = bytearray()
        self.escaped = False

    def feed(self, byte):
        """Take one byte. Returns (start byte, four data bytes) when a message is complete, else None."""
        if self.escaped:
            self.escaped = False
        elif byte == ESCAPE:
            self.escaped = True
            return None
        elif byte in (RENDER_START, OBJECT_START):
            self.kind, self.data = byte, bytearray()
            return None
        if self.kind is None:
            return None   # data with no message open: ignore, like noise on the line
        self.data.append(byte)
        if len(self.data) < 4:
            return None
        message = (self.kind, bytes(self.data))
        self.kind = None
        return message


def render_row(scene, y, size=IMAGE_SIZE):
    """One image row as a (size, 3) uint8 array: a plain raycast of the decoded scene."""
    f, r, u = (np.array(v) for v in scene.vectors)
    a = (np.arange(size) + 0.5) / (size / 2) - 1.0
    b = 1.0 - (y + 0.5) / (size / 2)
    d = f + a[:, None] * r + b * u
    d /= np.linalg.norm(d, axis=1, keepdims=True)
    origin = np.array([0.0, 0.0, scene.camera[2]])

    horizon, top = (np.array(c, dtype=float) for c in scene.sky)

    def sky(direction):
        # Rendering Flow 5h: horizon + (top - horizon) x D_z, with D_z below 0 counted as 0.
        return horizon + (top - horizon) * np.clip(direction[:, 2:3], 0.0, 1.0)

    best_t = np.full(size, np.inf)
    colour = sky(d)
    normal = np.zeros((size, 3))
    albedo = np.zeros((size, 3))
    material = np.full(size, -1)
    strength = np.zeros(size)

    def take(t, hit, n, obj):
        closer = hit & (t > EPS) & (t < best_t)
        best_t[closer] = t[closer]
        normal[closer] = n[closer] if n.ndim == 2 else n
        albedo[closer] = obj.colour
        material[closer] = scene_compiler.MATERIALS.index(obj.material)
        strength[closer] = obj.strength

    for obj in scene.objects:
        if obj.kind == "sphere":
            oc = np.array(obj.points[0]) - origin
            along = d @ oc
            offset2 = oc @ oc - along * along
            hit = offset2 <= obj.radius ** 2
            t = along - np.sqrt(np.where(hit, obj.radius ** 2 - offset2, 0.0))
            n = (origin + d * t[:, None] - np.array(obj.points[0])) / obj.radius
            take(t, hit, n, obj)
        else:
            v0, v1, v2 = (np.array(p) for p in obj.points)
            e1, e2 = v1 - v0, v2 - v0
            p = np.cross(d, e2)
            det = p @ e1
            ok = np.abs(det) > 1e-9
            inv = 1.0 / np.where(ok, det, 1.0)
            s = origin - v0
            bu = (p @ s) * inv
            q = np.cross(s, e1)
            bv = (d @ q) * inv
            t = (q @ e2) * inv
            n = np.cross(e1, e2)
            n = n / (np.linalg.norm(n) or 1.0)
            take(t, ok & (bu >= 0) & (bv >= 0) & (bu + bv <= 1), n, obj)

    # Ground: the plane z = 0 with 4-unit checker squares. Past 256 the chip's divide
    # clamps and it draws sky instead.
    down = d[:, 2] < 0
    t_ground = np.where(down, -origin[2] / np.where(down, d[:, 2], -1.0), np.inf)
    ground = down & (t_ground < best_t) & (t_ground < MAX_DISTANCE)
    point = origin + d * np.where(ground, t_ground, 0.0)[:, None]
    # Rendering Flow 5f: checker = bit 9 of x XOR bit 9 of y; 1 = colour A, 0 = colour B.
    odd = (np.floor(point[:, 0] / CHECKER_SIZE) + np.floor(point[:, 1] / CHECKER_SIZE)) % 2 == 1
    albedo[ground] = np.where(odd[ground, None], scene.ground, scene.checker)
    normal[ground] = (0.0, 0.0, 1.0)
    material[ground] = 0
    best_t[ground] = t_ground[ground]

    hit = material >= 0
    facing = np.where((normal * d).sum(axis=1, keepdims=True) > 0, -normal, normal)
    diffuse = 0.35 + 0.65 * np.clip(facing @ LIGHT, 0.0, 1.0)[:, None]
    reflected = d - 2.0 * (d * facing).sum(axis=1, keepdims=True) * facing
    shaded = {
        0: albedo * diffuse,                                   # matte
        1: albedo / 255.0 * sky(reflected),                    # mirror: shows the sky
        2: 0.5 * albedo * diffuse + 0.5 * sky(d),              # glass: half see-through
        3: albedo * strength[:, None],                         # glow: colour x strength, clipped at 255
    }
    for index, value in shaded.items():
        chosen = hit & (material == index)
        colour[chosen] = value[chosen]
    return np.clip(np.rint(colour), 0, 255).astype(np.uint8)


def render(memory, out, row_delay=0.0):
    """Send one frame of PIXEL messages for the scene in `memory`.

    A memory image that does not decode still gets a full frame (magenta), so
    the host sees a picture that is plainly wrong instead of a timeout.
    """
    try:
        scene = scene_compiler.decode_words(memory)
    except ValueError as e:
        print(f"fake chip: memory does not decode: {e}", file=sys.stderr)
        scene = None

    for y in range(IMAGE_SIZE):
        row = render_row(scene, y) if scene else np.full((IMAGE_SIZE, 3), (255, 0, 255), dtype=np.uint8)
        out.write(b"".join(escape_pixel(*pixel) for pixel in row.tolist()))
        out.flush()
        if row_delay:
            time.sleep(row_delay)


def serve(inp, out, row_delay=0.0):
    """Run until the input closes: store OBJECT words, answer each RENDER with a frame."""
    memory = [0] * SRAM_WORDS
    parser = FrameParser()
    while True:
        chunk = inp.read(1)
        if not chunk:
            return
        message = parser.feed(chunk[0])
        if message is None:
            continue
        kind, data = message
        if kind == OBJECT_START:
            word, address = data[0] | data[1] << 8, data[2] | data[3] << 8
            if address < SRAM_WORDS:
                memory[address] = word
        else:
            render(memory, out, row_delay)


def main():
    row_delay = float(os.environ.get("FAKE_CHIP_ROW_MS", "0")) / 1000.0
    try:
        serve(sys.stdin.buffer, sys.stdout.buffer, row_delay)
    except (BrokenPipeError, KeyboardInterrupt):
        pass   # the host hung up mid-frame (cancel): nothing left to do
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
