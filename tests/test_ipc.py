"""Pipe link to the scene designer: the example session from the design doc, section 7."""

import io
import json

from tthost.ipc import PROTOCOL, DesignerLink


def run_session(lines, **callbacks):
    """Feed lines to a link and return (link, list of reply messages)."""
    out = io.StringIO()
    link = DesignerLink(io.StringIO("".join(line + "\n" for line in lines)), out, version="9.9", **callbacks)
    link.run()
    return link, [json.loads(line) for line in out.getvalue().splitlines()]


def test_hello_gets_ready_and_connects():
    events = []
    _, replies = run_session(['{"type":"hello","protocol":1}'],
                             on_connected=lambda: events.append("connected"),
                             on_closed=lambda: events.append("closed"))
    assert replies == [{"type": "ready", "protocol": PROTOCOL, "tthost": "9.9"}]
    assert events == ["connected", "closed"]


def test_wrong_protocol_is_refused():
    link, replies = run_session(['{"type":"hello","protocol":2}'])
    assert replies[0]["type"] == "error"
    assert not link.connected


def test_scene_is_passed_on_and_checked_with_its_rev():
    seen = []

    def on_scene(rev, text):
        seen.append((rev, text))
        return {"words": 295, "bvs": 4}

    _, replies = run_session([json.dumps({"type": "scene", "rev": 12, "text": "image 512\nspp 32\n"})],
                             on_scene=on_scene)
    assert seen == [(12, "image 512\nspp 32\n")]
    assert replies == [{"type": "checked", "rev": 12, "words": 295, "bvs": 4, "errors": [], "warnings": []}]


def test_bad_lines_get_an_error_and_the_link_keeps_going():
    _, replies = run_session(["not json", "[1, 2]", '{"type":"scene","rev":1}', '{"type":"hello","protocol":1}'])
    assert [r["type"] for r in replies] == ["error", "error", "error", "ready"]


def test_unknown_types_are_ignored_but_logged():
    log = []
    _, replies = run_session(['{"type":"future_message"}'], on_log=lambda d, line: log.append((d, line)))
    assert replies == []
    assert log == [("rx", '{"type":"future_message"}')]


def test_failing_scene_handler_reports_an_error():
    def on_scene(rev, text):
        raise RuntimeError("boom")

    _, replies = run_session(['{"type":"scene","rev":1,"text":""}'], on_scene=on_scene)
    assert replies == [{"type": "error", "message": "scene handler failed: boom"}]
