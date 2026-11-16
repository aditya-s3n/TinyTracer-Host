# TinyTracer-Host
Host-Side Software for support and use the TinyTracer

## Scene designer (Rust)

A desktop app for laying out a scene with a live raycast preview. It lives in
`designer/` and needs a Rust toolchain (https://rustup.rs).

```
cd designer
cargo run --release
```

The right-hand panel edits the camera, sky, ground and objects (spheres,
triangles, quads and boxes). In the view:
right-drag to look, hold the right button and use WASD/QE to move, scroll to
change the field of view, click an object to select it.

In the TinyTracer section of the panel:

- **Compile** writes the scene as a scene `.txt` file (default
  `scenes/designer_scene.txt`), in the same format as `scene_demo.txt`.
- **Start TinyTracer** launches the Python app (`python main.py --ipc`, using
  `.venv` when it exists) as a child process and talks to it over its stdin and
  stdout with JSON lines. While it is connected, Compile also sends the scene to
  the app, which compiles it and replies with the word count or the errors.
- **Render** asks the app to render the scene it accepted, and shows progress
  and where the image was saved. It uses the backend and port chosen in the
  TinyTracer window, unless "Use the stand-in chip" is ticked.

The chip has four materials, so the sliders are reduced when the file is
written: emissive above 0 becomes `glow`, otherwise metallic 0.5 or more becomes
`mirror`, otherwise `matte`.

## TinyTracer app (Python)

```
pip install numpy pillow pyserial imgui-bundle PyOpenGL pytest

python main.py [scene.txt]                 # the window: run panel and live view
python -m tthost compile scene_demo.txt -o sram.hex
python -m tthost decode  sram.hex
python -m tthost render  scene_demo.txt --backend chip --port /dev/ttyUSB0
python -m tthost ports
python -m pytest tests
```

Every render gets a folder under `runs/` with the PNG, the raw linear image,
the scene, the memory image and the logs:

- `run_log.txt`: the whole run as readable text, written when the run ends
  (finished, failed or cancelled). What ran, latency (upload, RENDER to first
  pixel, per pixel, per row, pixel rate), anomalies, a timeline, and one line
  per row and per pixel with its position, colour and arrival time.
- `events.jsonl`: the same events as they happened, one JSON object per line.
- `raw_bytes.log`: every byte sent and received, before unescaping.

### Without hardware

`tthost/fake_chip.py` is a stand-in chip. It speaks the real UART protocol
(OBJECT and RENDER in, PIXEL out, with escaping) and draws a plain raycast of
whatever was uploaded. Use it as the `sim` backend with the port `fake`:

```
python -m tthost render scene_demo.txt --backend sim --port fake
```

or press "Use stand-in chip" in the window. It tests the host from the scene
file to the PNG. It is not the chip's renderer and it decodes memory with the
host's own decoder, so it cannot show that the memory map matches the chip.

### Memory map

The compiler follows the word layout in the HW team's Rendering Flow page
("What the SRAM holds"): header, bounding volumes, and the sphere and triangle
records, bit for bit. Its tests check the values that page quotes for the demo
scene (295 words, BV 0 at `0x1284` with radius raw 2143).

Two things on the HW side are still open, and worth confirming before trusting
a render from the real chip:

- Rendering Flow says the header layout is itself assumed ("the docs define no
  header").
- `system-architecture.md` disagrees with Rendering Flow on the number format
  (Q8.8 vs Q9.7 / Q2.14), bounding-volume count (4, constant vs up to 16),
  samples (8 vs 1 to 32) and bounce limit (10 vs 8). The host follows
  Rendering Flow.
