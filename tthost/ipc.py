"""Pipe link to the scene designer (design doc, sections 4.3, 6.5 and 7).

The Rust scene designer starts this app as a child process with stdin and
stdout piped. Messages are JSON lines: one JSON object per line, each with a
"type" field.

    designer -> app   hello {protocol}        first message
    app -> designer   ready {protocol, tthost}
    designer -> app   scene {rev, text}       full scene .txt text
    app -> designer   checked {rev, words, bvs, errors, warnings}
    app -> designer   error {message}         a message could not be handled

render / cancel / state / progress / done are in the design doc but not handled
here yet; like any unknown type they are logged and ignored.

Rules: stdout carries protocol messages only, so call claim_stdout() before
anything can print. This module never imports the GUI; the window passes
callbacks, which are called on the reader thread.
"""

import json
import os
import sys
import threading

PROTOCOL = 1


def claim_stdout():
    """Keep the real stdout for protocol messages and point everything else at stderr.

    Returns a line-buffered text stream on the original stdout pipe. After this,
    print() and output written to file descriptor 1 by C libraries (ImGui,
    drivers) go to stderr, so a stray print cannot break the designer's parser.
    """
    out = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    sys.stdout.flush()
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return out


class DesignerLink:
    """Reads the designer's messages on a background thread and writes replies.

    Callbacks (all optional, called on the reader thread):
        on_scene(rev, text) -> dict   fields for the "checked" reply: words, bvs, errors, warnings
        on_connected()                the hello / ready handshake completed
        on_closed()                   stdin closed: the designer quit or dropped the link
        on_log(direction, line)       every raw line; direction is "rx" or "tx"
    """

    def __init__(self, inp, out, version="", on_scene=None, on_connected=None, on_closed=None, on_log=None):
        self.inp = inp
        self.out = out
        self.version = version
        self.on_scene = on_scene
        self.on_connected = on_connected
        self.on_closed = on_closed
        self.on_log = on_log
        self.connected = False
        self._write_lock = threading.Lock()
        self._thread = None

    def start(self):
        """Start the reader thread and return immediately."""
        self._thread = threading.Thread(target=self.run, name="ipc-reader", daemon=True)
        self._thread.start()

    def run(self):
        """Read lines until stdin closes. start() runs this on a thread; tests call it directly."""
        try:
            for line in self.inp:
                line = line.strip()
                if line:
                    self._log("rx", line)
                    self.handle_line(line)
        finally:
            self.connected = False
            if self.on_closed:
                self.on_closed()

    def send(self, message):
        """Write one message as a single JSON line. Safe to call from any thread."""
        line = json.dumps(message, separators=(",", ":"))
        with self._write_lock:
            try:
                self.out.write(line + "\n")
                self.out.flush()
            except (OSError, ValueError):
                return  # the designer is gone; the reader thread will see stdin close
        self._log("tx", line)

    def handle_line(self, line):
        """Parse one line and act on it. A bad line gets an "error" reply, never an exception."""
        try:
            message = json.loads(line)
            kind = message["type"]
        except (ValueError, TypeError, KeyError):
            self.send({"type": "error", "message": "not a JSON object with a type field"})
            return

        if kind == "hello":
            if message.get("protocol") != PROTOCOL:
                self.send({"type": "error",
                           "message": f"protocol {message.get('protocol')} is not supported; this app speaks {PROTOCOL}"})
                return
            self.send({"type": "ready", "protocol": PROTOCOL, "tthost": self.version})
            self.connected = True
            if self.on_connected:
                self.on_connected()
        elif kind == "scene":
            self._handle_scene(message)
        # Unknown types are ignored (they are already in the log), so either side
        # can add messages without breaking the other.

    def _handle_scene(self, message):
        rev, text = message.get("rev"), message.get("text")
        if not isinstance(text, str):
            self.send({"type": "error", "message": "scene message needs a text field"})
            return
        verdict = {"words": 0, "bvs": 0, "errors": [], "warnings": []}
        if self.on_scene:
            try:
                verdict.update(self.on_scene(rev, text) or {})
            except Exception as e:  # a handler bug must not kill the link
                self.send({"type": "error", "message": f"scene handler failed: {e}"})
                return
        self.send({"type": "checked", "rev": rev, **verdict})

    def _log(self, direction, line):
        if self.on_log:
            self.on_log(direction, line)
