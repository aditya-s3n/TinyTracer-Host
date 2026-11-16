"""Top to bottom with no hardware: scene text -> compiler -> UART frames -> a chip -> pixels -> PNG.

The other test files check one module each with fakes around it. These join
the real ones: the real compiler, the real transport framing, the real pixel
receiver and the real run controller, talking to the stand-in chip
(tthost/fake_chip.py) through an OS pipe ("sim" backend) and through a serial
port (a pseudo-terminal opened with pyserial, as the "rtl" and "chip" backends do).

What this proves: every word arrives at the right address, every pixel comes
back in the right place, and escaping survives both directions. What it cannot
prove: that the real chip reads memory the same way (the stand-in chip uses the
host's own decoder; tests/test_scene_compiler.py checks the layout against the docs).

RUN from root:  python -m pytest tests
"""

import io
import json
import os
import threading
from pathlib import Path

import numpy as np
import pytest

from tthost import fake_chip, receive, scene_compiler, transport
from tthost import main as cli
from tthost import run_controller as rc

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "scene_demo.txt"
SIZE = transport.IMAGE_SIZE

# A scene made of the bytes that need escaping: every colour channel is 0 or 3,
# so a large share of the PIXEL messages carry an escaped 0x00 or 0x03. The demo scene
# happens to contain neither, and would leave the escape path untested.
DARK_SCENE = ("image 512\nspp 1\ncamera 0 0 3 0 -20 90\n"
              "sky 0 3 0 3 0 3\nground 0 0 0\nchecker 3 3 3\n"
              "sphere 12 0 3 3 glow 3 0 3 1\n"
              "box 8 -8 0 10 -6 2 glow 0 3 0 1\n")


@pytest.fixture(scope="module")
def demo_words():
    words, report = scene_compiler.compile_text(DEMO.read_text())
    assert report["errors"] == []
    return words


def expected_rows(words, rows):
    """What the stand-in chip draws for these rows, computed directly (no UART involved)."""
    scene = scene_compiler.decode_words(words)
    return {y: fake_chip.render_row(scene, y) for y in rows}


def assert_frame_matches(fb, words):
    assert fb.count == SIZE * SIZE and not fb.bad.any()
    for y, row in expected_rows(words, (0, 1, 200, 255, 256, 300, 511)).items():
        assert np.array_equal(fb.pixels[y], row), f"row {y} differs"


# The stand-in chip's side of the protocol
def test_chip_parser_recovers_every_word_and_address():
    parser = fake_chip.FrameParser()
    tricky = [0x0000, 0x0001, 0x0003, 0x0100, 0x0300, 0x0303, 0x0101, 0xFFFF, 0x1234]
    for word in tricky:
        for address in (0, 1, 3, 0x100, 0x1FF):
            messages = [m for m in map(parser.feed, transport.frame_object(word, address)) if m]
            assert len(messages) == 1
            kind, data = messages[0]
            assert kind == transport.OBJECT_START
            assert (data[0] | data[1] << 8, data[2] | data[3] << 8) == (word, address)


def test_chip_parser_resyncs_on_a_start_byte_inside_a_broken_message():
    parser = fake_chip.FrameParser()
    broken = transport.frame_object(0x1234, 5)[:3]            # cut off after two data bytes
    stream = b"\x55" + broken + transport.frame_render()
    messages = [m for m in map(parser.feed, stream) if m]
    assert messages == [(transport.RENDER_START, bytes([0, 2, 0, 2]))]   # 512 x 512, low byte first


def test_chip_answers_render_with_a_full_frame_of_escaped_pixels():
    words, report = scene_compiler.compile_text(DARK_SCENE)
    assert report["errors"] == []
    upload = b"".join(transport.frame_object(w, a) for a, w in enumerate(words))
    out = io.BytesIO()
    fake_chip.serve(io.BytesIO(upload + transport.frame_render()), out)

    parser = receive.PixelParser()
    pixels = [e for e in parser.feed(out.getvalue()) if isinstance(e, receive.PixelEvent)]
    assert len(pixels) == SIZE * SIZE and parser.done
    assert sum(e.escaped for e in pixels) > 50_000, "a large share of the pixels need an escape"
    assert {(e.r, e.g, e.b) for e in pixels} >= {(3, 0, 3), (0, 3, 0), (0, 0, 0)}


def test_chip_sends_a_magenta_frame_for_memory_that_is_not_a_scene():
    out = io.BytesIO()
    fake_chip.serve(io.BytesIO(transport.frame_object(0xFFFF, 15) + transport.frame_render()), out)
    assert out.getvalue().startswith(receive.escape_pixel(255, 0, 255) * 2)


# The whole host against the stand-in chip
def render(tmp_path, **settings):
    events = []
    controller = rc.RunController(on_state=lambda s: events.append(s), on_error=lambda m: events.append(m))
    settings.setdefault("scene_path", str(DEMO))
    result = controller.render_blocking(rc.RunSettings(runs_dir=str(tmp_path), timeout=5.0, **settings))
    return controller, result, events


def test_demo_scene_renders_through_the_sim_pipe(tmp_path, demo_words):
    controller, result, states = render(tmp_path, backend="sim", port=transport.FAKE_CHIP, log_pixels=False)

    assert states == ["compiling", "uploading", "rendering", "processing", "done"]
    assert (result.complete, result.pixels, result.bad) == (True, SIZE * SIZE, 0)
    assert_frame_matches(controller.framebuffer, demo_words)

    # The run folder holds everything section 12.1 lists, and the logs agree with what was sent.
    run = Path(result.run_dir)
    assert sorted(p.name for p in run.iterdir()) == [
        "events.jsonl", "raw_bytes.log", "render.png", "render_linear.npy", "render_raw.png",
        "run_log.txt", "scene.txt", "sram.hex"]
    assert (run / "scene.txt").read_text() == DEMO.read_text()
    assert scene_compiler.parse_hex_dump((run / "sram.hex").read_text()) == demo_words

    sent = b"".join(data for _, data in transport.read_raw_log(run / "raw_bytes.log", "tx"))
    assert sent == b"".join(transport.frame_object(w, a) for a, w in enumerate(demo_words)) + transport.frame_render()

    events = [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines()]
    assert events[0]["event"] == "run_start" and (events[0]["words"], events[0]["bvs"]) == (295, 4)
    assert [e for e in events if e["event"] == "anomaly"] == []

    from PIL import Image
    png = np.asarray(Image.open(run / "render.png"))
    assert png.shape == (SIZE, SIZE, 3)
    assert np.array_equal(png, rc.linear_to_srgb8(controller.framebuffer.pixels)), "the PNG is the gamma-encoded frame"
    assert np.array_equal(np.load(run / "render_linear.npy"), controller.framebuffer.pixels)


def test_received_picture_shows_the_scene(tmp_path):
    """A sanity check a person would make by eye: sky on top, checker below, the red sphere in between."""
    controller, _, _ = render(tmp_path, backend="sim", port=transport.FAKE_CHIP, log_pixels=False)
    image = controller.framebuffer.pixels
    r, g, b = image[0, 0]
    assert 110 < r < g < 240 and b == 255, "sky: between the top and horizon colours"
    bottom = image[500].reshape(-1, 3)
    greys = {tuple(int(v) for v in p) for p in bottom}
    assert all(abs(r - g) < 30 and abs(g - b) < 30 for r, g, b in greys), "the ground checker is grey"
    assert len(greys) > 1, "and has two colours"
    red = (image[..., 0] > 2 * image[..., 1]) & (image[..., 0] > 2 * image[..., 2]) & (image[..., 0] > 60)
    assert 3000 < red.sum() < 40000, "the red sphere covers a sensible part of the image"


def test_pixels_full_of_escaped_bytes_survive_the_round_trip(tmp_path):
    """Upload and download both lean on escaping here: colours of 0 and 3, and words like 0x0003."""
    scene = tmp_path / "dark.txt"
    scene.write_text(DARK_SCENE)
    words, _ = scene_compiler.compile_text(DARK_SCENE)
    controller, result, _ = render(tmp_path / "runs", scene_path=str(scene), backend="sim",
                                   port=transport.FAKE_CHIP, log_pixels=False)
    assert (result.complete, result.bad) == (True, 0)
    assert_frame_matches(controller.framebuffer, words)
    assert controller.framebuffer.pixels.max() == 3

    received = b"".join(data for _, data in transport.read_raw_log(Path(result.run_dir) / "raw_bytes.log", "rx"))
    assert received.count(bytes([transport.ESCAPE])) > SIZE * SIZE


def test_spp_override_reaches_the_chip_memory(tmp_path):
    _, result, _ = render(tmp_path, backend="sim", port=transport.FAKE_CHIP, log_pixels=False, spp=4)
    words = scene_compiler.parse_hex_dump((Path(result.run_dir) / "sram.hex").read_text())
    assert words[15] & 0x7 == 2                       # log2(4)
    assert "spp 4" in (Path(result.run_dir) / "scene.txt").read_text()


def test_scene_with_errors_never_reaches_the_link(tmp_path):
    bad = tmp_path / "bad.txt"
    bad.write_text(DEMO.read_text().replace("45 -12 90", "45 -12 120"))
    with pytest.raises(rc.RunError, match="line 14: field of view 120 is over the limit of 90"):
        render(tmp_path / "runs", scene_path=str(bad), backend="sim", port="/nonexistent/program")


def test_sim_program_that_dies_is_reported_and_the_partial_run_is_kept(tmp_path):
    silent = tmp_path / "silent_chip.py"
    silent.write_text("import sys\nsys.stdin.buffer.read()\n")       # takes the upload, never answers
    events = []
    controller = rc.RunController(on_error=events.append)
    with pytest.raises(rc.RunError, match="0 of 262144 pixels"):
        controller.render_blocking(rc.RunSettings(
            scene_path=str(DEMO), backend="sim", port=str(silent), runs_dir=str(tmp_path / "runs"), timeout=0.4))
    assert controller.state == rc.IDLE
    assert len(events) == 1 and "timeout" in events[0]


def test_demo_scene_renders_through_a_serial_port(tmp_path, demo_words):
    """The same render over a real tty: pyserial on one end of a pseudo-terminal, the chip on the other."""
    pytest.importorskip("serial")
    pty = pytest.importorskip("pty")
    import tty

    master, slave = pty.openpty()
    tty.setraw(master)
    tty.setraw(slave)
    port = os.ttyname(slave)

    def chip():
        with os.fdopen(os.dup(master), "rb", buffering=0) as inp, os.fdopen(os.dup(master), "wb") as out:
            try:
                fake_chip.serve(inp, out)
            except OSError:
                pass                                   # the host closed the port

    thread = threading.Thread(target=chip, daemon=True)
    thread.start()
    try:
        controller, result, _ = render(tmp_path, backend="rtl", port=port, log_pixels=False)
    finally:
        os.close(slave)
        os.close(master)
    assert (result.complete, result.bad) == (True, 0)
    assert_frame_matches(controller.framebuffer, demo_words)


# Command line
def test_cli_compile_writes_the_hex_dump(tmp_path, capsys):
    out = tmp_path / "sram.hex"
    assert cli.main(["compile", str(DEMO), "-o", str(out)]) == 0
    assert "295 of 512 words: 6 spheres, 18 triangles, 4 bounding volumes" in capsys.readouterr().out
    assert len(scene_compiler.parse_hex_dump(out.read_text())) == 295


def test_cli_compile_lists_every_error_and_fails(tmp_path, capsys):
    bad = tmp_path / "bad.txt"
    bad.write_text(DEMO.read_text().replace("spp     32", "spp     24").replace("45 -12 90", "45 -12 100"))
    assert cli.main(["compile", str(bad)]) == 1
    err = capsys.readouterr().err
    assert "error: line 13: spp 24 must be 1, 2, 4, 8, 16 or 32" in err
    assert "error: line 14: field of view 100 is over the limit of 90" in err
    assert cli.main(["compile", str(tmp_path / "missing.txt")]) == 1


def test_cli_decode_prints_a_scene_that_compiles(tmp_path, capsys):
    out = tmp_path / "sram.hex"
    cli.main(["compile", str(DEMO), "-o", str(out)])
    capsys.readouterr()
    assert cli.main(["decode", str(out)]) == 0
    text = capsys.readouterr().out
    assert "sphere 27 17 6  6   matte  220 60 50  0  0" in text
    assert scene_compiler.compile_text(text)[1]["words"] == 295

    out.write_text("000 0000\n")
    assert cli.main(["decode", str(out)]) == 1


def test_cli_render_saves_a_png(tmp_path, capsys):
    code = cli.main(["render", str(DEMO), "--backend", "sim", "--port", "fake", "--out", str(tmp_path),
                     "--no-pixel-log", "--timeout", "5"])
    captured = capsys.readouterr()
    assert code == 0
    assert "262144 pixels" in captured.out and "0 bad" in captured.out
    assert Path(captured.out.strip().splitlines()[-1]).is_file()
    assert "100.0%" in captured.err

    assert cli.main(["render", str(DEMO), "--out", str(tmp_path)]) == 1     # chip backend with no port
    assert "needs a port" in capsys.readouterr().err
