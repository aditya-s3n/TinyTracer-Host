"""Run log: one readable .txt file that says everything that happened in a run.

Written into the run folder as run_log.txt when the run ends, whether it
finished, failed or was cancelled. It is built from events.jsonl, the event log
the run writes as it goes (design doc, section 12.3), so it never disagrees
with it; raw_bytes.log remains the record of the bytes on the wire.

Sections, in order:

    RUN        scene, backend, settings, memory use, how the run ended
    LATENCY    upload time, RENDER to first pixel, pixel and row timing, pixel rate
    ANOMALIES  short pixels, unexpected bytes, gaps, timeouts
    TIMELINE   every step with its time since the run started
    ROWS       one line per image row: when it finished, how long it took, its average colour
    PIXELS     one line per pixel: index, x, y, r, g, b, arrival time, escaped or not, bad or not

Pixel values are the chip's linear 0-255 output as received, before any clean-up
or gamma. Arrival times are when the host read the bytes: several pixels read
in one go share a time, so they are exact to a read, not to a byte.
"""

import json
import os
import statistics

EVENTS_FILE = "events.jsonl"
LOG_FILE = "run_log.txt"
RULE = "=" * 78


def read_events(path):
    """Read an event log. A torn last line (the run was killed mid-write) is skipped."""
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    return events


def percentile(sorted_values, fraction):
    """The value below which `fraction` of the sorted values fall (nearest rank)."""
    index = min(len(sorted_values) - 1, max(0, round(fraction * (len(sorted_values) - 1))))
    return sorted_values[index]


def timing_line(label, values_ms):
    """One line of min / mean / median / 99% / max for a list of milliseconds."""
    if not values_ms:
        return f"{label:<26} no data"
    ordered = sorted(values_ms)
    return (f"{label:<26} min {ordered[0]:.3f}  mean {statistics.fmean(ordered):.3f}  "
            f"median {statistics.median(ordered):.3f}  99% {percentile(ordered, 0.99):.3f}  "
            f"max {ordered[-1]:.3f}  ms")


def describe(event):
    """An event's fields as "key=value" text, without the bookkeeping ones."""
    skip = ("event", "time")
    return "  ".join(f"{k}={v}" for k, v in event.items() if k not in skip)


def build_run_log(events, outcome, framebuffer=None, files=()):
    """Return the run log text for a list of events.

    `outcome` is the last line of the RUN section ("done", "cancelled", or an
    error message). `framebuffer` supplies pixel values when the per-pixel
    events were switched off; `files` are the names in the run folder.
    """
    first = events[0] if events else {}
    t0 = first.get("time", 0.0)

    def ms(event):
        return (event["time"] - t0) * 1000.0

    def find(name):
        return next((e for e in events if e["event"] == name), None)

    pixels = [e for e in events if e["event"] == "pixel"]
    rows = [e for e in events if e["event"] == "row"]
    anomalies = [e for e in events if e["event"] == "anomaly"]
    upload_start, upload_done, render = find("upload_start"), find("upload_done"), find("render")
    frame_end, summary = find("frame_end"), find("summary")
    last = events[-1] if events else None
    bad = {e["i"] for e in anomalies if e.get("kind") == "short_pixel" and "i" in e}

    out = [RULE, "TinyTracer run log", RULE, ""]

    # RUN
    out += ["RUN", "---"]
    for label, key in (("run", "run"), ("scene", "scene"), ("scene sha1", "scene_sha1"),
                       ("backend", "backend"), ("port", "port"), ("baud", "baud"),
                       ("samples per pixel", "spp"), ("passes", "passes"),
                       ("memory words", "words"), ("bounding volumes", "bvs"),
                       ("host commit", "host_commit")):
        value = first.get(key)
        if key == "spp" and value is None:
            value = "as the scene file says"
        out.append(f"{label:<20} {value}")
    if frame_end:
        out.append(f"{'pixels received':<20} {frame_end['pixels']} of {frame_end['expected']} "
                   f"({frame_end.get('reason', '?')})")
        out.append(f"{'bad pixels':<20} {frame_end['bad']}")
    out.append(f"{'ended':<20} {outcome}")
    if files:
        out.append(f"{'files':<20} {', '.join(sorted(files))}")
    out.append("")

    # LATENCY
    out += ["LATENCY", "-------"]
    if upload_start and upload_done:
        sent = upload_done.get("bytes", 0)
        took = ms(upload_done) - ms(upload_start)
        out.append(f"{'scene upload':<26} {upload_start.get('words')} words, {sent} bytes in {took:.1f} ms")
    arrivals = [(e["time"] - render["time"]) * 1000.0 for e in pixels] if render else []
    if render and (pixels or rows):
        first_data = pixels[0] if pixels else rows[0]
        what = "first pixel" if pixels else "first row done"
        out.append(f"{'RENDER to ' + what:<26} {(first_data['time'] - render['time']) * 1000.0:.3f} ms")
    if render and frame_end:
        took_s = frame_end["time"] - render["time"]
        out.append(f"{'RENDER to end of frame':<26} {took_s:.3f} s")
        if took_s > 0:
            out.append(f"{'pixel rate':<26} {frame_end['pixels'] / took_s:,.0f} pixels per second")
    if len(arrivals) > 1:
        out.append(timing_line("between pixels", [b - a for a, b in zip(arrivals, arrivals[1:])]))
    elif not pixels:
        out.append(f"{'between pixels':<26} not recorded (per-pixel logging was off)")
    out.append(timing_line("per row", [e["ms"] for e in rows]))
    if rows:
        slowest = sorted(rows, key=lambda e: -e["ms"])[:5]
        out.append(f"{'slowest rows':<26} " + ", ".join(f"row {e['y']} ({e['ms']:.1f} ms)" for e in slowest))
    if summary:
        out.append(f"{'whole run':<26} {summary.get('total_s')} s")
    elif last:
        out.append(f"{'whole run':<26} {ms(last) / 1000.0:.3f} s (to the last event)")
    out.append("")

    # ANOMALIES
    out += [f"ANOMALIES ({len(anomalies)})", "---------"]
    for e in anomalies:
        out.append(f"{ms(e):>12.3f} ms  {describe(e)}")
    if not anomalies:
        out.append("none")
    out.append("")

    # TIMELINE: everything except the per-pixel and per-row lines, which have their own sections.
    out += ["TIMELINE", "--------", f"{'t (ms)':>12}  event"]
    for e in events:
        if e["event"] not in ("pixel", "row"):
            out.append(f"{ms(e):>12.3f}  {e['event']:<13} {describe(e)}".rstrip())
    out.append("")

    # ROWS
    out += [f"ROWS ({len(rows)})", "----", f"{'row':>4} {'done at (ms)':>13} {'took (ms)':>10}   mean r   mean g   mean b"]
    for e in rows:
        mean = ""
        if framebuffer is not None and e["y"] < framebuffer.pixels.shape[0]:
            r, g, b = framebuffer.pixels[e["y"]].mean(axis=0)
            mean = f"  {r:7.2f}  {g:7.2f}  {b:7.2f}"
        out.append(f"{e['y']:>4} {ms(e):>13.3f} {e['ms']:>10.1f} {mean}".rstrip())
    out.append("")

    # PIXELS
    out += pixel_section(pixels, render, bad, framebuffer, frame_end)
    return "\n".join(out) + "\n"


def pixel_section(pixels, render, bad, framebuffer, frame_end):
    """The PIXELS section: from the per-pixel events when there are any, else from the frame buffer."""
    header = f"{'index':>6} {'x':>3} {'y':>3}   {'r':>3} {'g':>3} {'b':>3}  {'t (ms)':>11}  esc bad"
    if pixels:
        t_render = render["time"] if render else pixels[0]["time"]
        lines = [f"PIXELS ({len(pixels)})", "------",
                 "t = arrival time since RENDER was sent; esc = a byte of it was escaped on the wire;",
                 "bad = short pixel, value not trustworthy.", header]
        lines += [f"{e['i']:>6} {e['x']:>3} {e['y']:>3}   {e['r']:>3} {e['g']:>3} {e['b']:>3}  "
                  f"{(e['time'] - t_render) * 1000.0:>11.3f}  {'yes' if e.get('esc') else ' no'} "
                  f"{'yes' if e['i'] in bad else ' no'}"
                  for e in pixels]
        return lines

    count = frame_end["pixels"] if frame_end else 0
    if framebuffer is None or count == 0:
        return ["PIXELS (0)", "------", "none received"]
    height, width = framebuffer.pixels.shape[:2]
    count = min(count, width * height)
    values = framebuffer.pixels.reshape(-1, 3)[:count].round().astype(int).tolist()
    lines = [f"PIXELS ({count})", "------",
             "Per-pixel logging was off for this run, so arrival times and escapes were not recorded.", header]
    lines += [f"{i:>6} {i % width:>3} {i // width:>3}   {r:>3} {g:>3} {b:>3}  {'-':>11}    -  "
              f"{'yes' if i in bad else ' no'}"
              for i, (r, g, b) in enumerate(values)]
    return lines


def write_run_log(run_dir, outcome, framebuffer=None):
    """Build run_log.txt in `run_dir` from its events.jsonl and return the file's path."""
    events = read_events(os.path.join(run_dir, EVENTS_FILE))
    files = [name for name in os.listdir(run_dir)] + [LOG_FILE]
    path = os.path.join(run_dir, LOG_FILE)
    with open(path, "w", encoding="utf-8") as f:
        f.write(build_run_log(events, outcome, framebuffer, set(files)))
    return path
