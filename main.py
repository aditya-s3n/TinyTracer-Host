"""Launch the TinyTracer app window.

    python main.py [scene.txt]
"""

import sys

from tthost import gui


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    gui.run(argv[0] if argv else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
