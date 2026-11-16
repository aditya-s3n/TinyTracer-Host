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
  the app, which loads it and replies with the compiler's verdict.

The chip has four materials, so the sliders are reduced when the file is
written: emissive above 0 becomes `glow`, otherwise metallic 0.5 or more becomes
`mirror`, otherwise `matte`.
