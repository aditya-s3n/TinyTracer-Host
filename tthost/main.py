"""tthost CLI: entry point for the TinyTracer host.

Subcommands (see design doc, "Tech Stack" -> CLI):
    tthost render  scene.txt   compile, upload, render, receive pixels, save PNG
    tthost compile scene.txt   compile only; print word count/warnings, write words + hex dump
    tthost view    render.npy  open a saved linear buffer / PNG in the live view window
    tthost study   scene.txt   artifact study: chip vs software model vs float reference
    tthost ports               list serial ports
"""

BACKENDS = ("chip", "sim", "rtl", "replay")


def build_parser():
    """Build the argparse parser with all subcommands and their options.

    Shared options: --backend, --port, --baud, --out, --verbose.
    render: --passes, --no-window, --timeout, --denoise, --delta.
    compile: --hex, --words-out.
    study: --reference, --sim-path.
    """
    pass


def cmd_render(args):
    """Render a scene end to end.

    Compile the scene, open the link, open the raw byte and event logs,
    upload (full or delta), send RENDER, receive pixels into the frame buffer
    (live window or --no-window progress bar), repeat for each pass, then run
    the image pipeline and save the PNG + .npy. Returns an exit code; a short
    pixel count is reported as an error but the partial image is still saved.
    """
    pass


def cmd_compile(args):
    """Compile a scene without rendering.

    Prints words used out of 512, the bounding-volume split and any
    validation errors or saturation warnings; optionally writes the word
    list and a hex dump for checking against the RTL.
    """
    pass


def cmd_view(args):
    """Open a saved render (linear .npy or PNG) in the viewer, with raw/processed toggle."""
    pass


def cmd_study(args):
    """Run the artifact study: render the scene on the chain (chip/sim/float reference) and compare.

    Reports PSNR/SSIM and diff images so differences can be catalogued.
    """
    pass


def cmd_ports(args):
    """List available serial ports."""
    pass


def make_run_dir(out_dir, scene_path):
    """Create the output directory for a run (image, .npy, raw byte log, event log, copy of the scene)."""
    pass


def print_progress(received, total, elapsed, bad):
    """Text progress bar for --no-window: pixels received, rate, ETA, bad pixel count."""
    pass


def main(argv=None):
    """Parse arguments, dispatch to the chosen subcommand and return its exit code."""
    pass


if __name__ == "__main__":
    raise SystemExit(main())
