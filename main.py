"""Launch the TinyTracer app window.

    python main.py [--ipc] [scene.txt]

--ipc is passed by the scene designer when it starts this app as a child
process: stdin and stdout then carry JSON-lines messages (see tthost/ipc.py).
"""

import sys

from tthost import gui, ipc


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    use_ipc = "--ipc" in argv
    if use_ipc:
        argv.remove("--ipc")
    # Claim stdout before the window exists, so nothing else can write to the pipe.
    ipc_out = ipc.claim_stdout() if use_ipc else None
    gui.run(argv[0] if argv else None, ipc_out=ipc_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
