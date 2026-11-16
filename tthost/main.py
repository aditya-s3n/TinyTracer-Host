"""tthost command line (design doc, section 8.3).

    tthost render  scene.txt [--backend chip|sim|rtl|replay] [--port PORT] [--spp N] [--passes N]
    tthost compile scene.txt [-o sram.hex]     compile only; print the report
    tthost decode  sram.hex                    memory image back to a scene
    tthost ports                               list serial ports

Run it as `python -m tthost ...`. `render` prints a text progress bar and saves
the run folder (PNG, logs, scene, memory image) under --out. For the window,
run `python main.py` from the repository root instead.
"""

import argparse
import sys

from tthost import run_controller as rc
from tthost import scene_compiler, transport


def build_parser():
    """Build the argparse parser with all subcommands and their options."""
    parser = argparse.ArgumentParser(prog="tthost", description="Host software for the TinyTracer chip.")
    sub = parser.add_subparsers(dest="command", required=True)

    render = sub.add_parser("render", help="compile, upload, render, receive pixels, save a PNG")
    render.add_argument("scene", help="scene .txt file")
    render.add_argument("--backend", choices=transport.BACKENDS, default="chip")
    render.add_argument("--port", help="serial port for chip, program for sim ('fake' = stand-in chip), "
                                       "pty for rtl, raw byte log for replay")
    render.add_argument("--baud", type=int, default=transport.DEFAULT_BAUD)
    render.add_argument("--spp", type=int, help="override the scene's samples per pixel")
    render.add_argument("--passes", type=int, default=1, help="renders to average")
    render.add_argument("--timeout", type=float, default=rc.DEFAULT_TIMEOUT,
                        help="seconds with no byte before the frame is ended")
    render.add_argument("--out", default=rc.DEFAULT_RUNS_DIR, help="folder for run folders")
    render.add_argument("--no-pixel-log", action="store_true",
                        help="skip the per-pixel event log lines (about 30 MB per frame)")
    render.set_defaults(func=cmd_render)

    compile_ = sub.add_parser("compile", help="compile only and print the report")
    compile_.add_argument("scene", help="scene .txt file")
    compile_.add_argument("-o", "--output", help="write the hex dump here")
    compile_.set_defaults(func=cmd_compile)

    decode = sub.add_parser("decode", help="turn a memory image (hex dump) back into a scene")
    decode.add_argument("hex", help="sram.hex file, or any 'address word' listing in hex")
    decode.set_defaults(func=cmd_decode)

    ports = sub.add_parser("ports", help="list serial ports")
    ports.set_defaults(func=cmd_ports)
    return parser


def print_report(report):
    """Print a compile report: errors, or the word budget and warnings."""
    for error in report["errors"]:
        where = f"line {error['line']}: " if error["line"] else ""
        print(f"error: {where}{error['rule']}", file=sys.stderr)
    if report["errors"]:
        return
    print(f"{report['words']} of {scene_compiler.SRAM_WORDS} words: {report['spheres']} spheres, "
          f"{report['triangles']} triangles, {report['bvs']} bounding volumes")
    for warning in report["warnings"]:
        print(f"warning: {warning}")


def cmd_compile(args):
    """Compile a scene, print the report and optionally write the hex dump. Exit code 1 on errors."""
    with open(args.scene, encoding="utf-8") as f:
        words, report = scene_compiler.compile_text(f.read(), args.scene)
    print_report(report)
    if words is None:
        return 1
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(report["hex"])
        print(f"wrote {args.output}")
    return 0


def cmd_decode(args):
    """Print the scene stored in a memory image, as scene .txt."""
    with open(args.hex, encoding="utf-8") as f:
        text = f.read()
    try:
        scene = scene_compiler.decode_words(scene_compiler.parse_hex_dump(text))
    except (scene_compiler.SceneError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"# decoded from {args.hex}: camera at x = 0, y = 0; quads and boxes are plain triangles")
    print(scene_compiler.scene_to_text(scene), end="")
    return 0


def print_progress(progress, total=rc.TOTAL_PIXELS, width=30):
    """One-line text progress bar: pixels received, time left, bad pixel count."""
    done = progress.pixels / total
    bar = "#" * int(done * width)
    eta = "" if progress.eta_s is None else f"  {progress.eta_s:5.0f} s left"
    print(f"\r[{bar:<{width}}] {100 * done:5.1f}%  row {progress.row + 1:3d}{eta}  bad {progress.bad}",
          end="", file=sys.stderr, flush=True)


def cmd_render(args):
    """Render a scene end to end and print where the image went. Exit code 1 on any failure.

    A short frame is an error, but the partial image and the logs are still saved.
    """
    with open(args.scene, encoding="utf-8") as f:
        _, report = scene_compiler.compile_text(f.read(), args.scene)
    print_report(report)
    if report["errors"]:
        return 1

    controller = rc.RunController(on_progress=print_progress)
    settings = rc.RunSettings(
        scene_path=args.scene, backend=args.backend, port=args.port, baud=args.baud, spp=args.spp,
        passes=args.passes, runs_dir=args.out, timeout=args.timeout, log_pixels=not args.no_pixel_log)
    try:
        result = controller.render_blocking(settings)
    except rc.RunError as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        controller.cancel()
        print("\ncancelled", file=sys.stderr)
        return 1
    print(file=sys.stderr)
    print(f"{result.pixels} pixels in {result.total_s:.1f} s, {result.bad} bad")
    print(f"log: {result.log}")
    print(result.png)
    return 0


def cmd_ports(args):
    """List available serial ports."""
    try:
        ports = transport.list_ports()
    except ImportError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    for device, description in ports:
        print(f"{device}\t{description}")
    if not ports:
        print("no serial ports found", file=sys.stderr)
    return 0


def main(argv=None):
    """Parse arguments, run the chosen subcommand and return its exit code."""
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
