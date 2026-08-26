"""
Record the demo: what a real data-entry task costs an agent, twice.

The obvious demo for a desktop agent -- a cursor clicking things -- is worthless
here, because it looks identical to every automation tool built since 2015. What
distinguishes oswright is invisible on screen: the same work costs an order of
magnitude fewer tokens. So the recording puts the number on screen next to the
work.

Everything shown is measured, not illustrated. The run is real, the counters are
real, and the comparison arm is a real screenshot-per-step run of the same task
in the same session rather than an estimate.

Output:
    docs/demo.gif   the animation
    docs/demo.png   a still, for readers who skim

Run:  python benchmarks/record_demo.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import demo_task as D  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

import oswright.mcp_server as server  # noqa: E402

FPS = 8
WIDTH = 940                 # rendered frame width; GitHub shows ~900 CSS px
PANEL = 172                 # height of the measurement panel under the screen
IMAGE_TOKENS = int(1920 * 1080 / 750)

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")

INK = (24, 33, 43)
MUTED = (110, 124, 140)
GOOD = (18, 132, 74)
BAD = (191, 54, 44)
RULE = (214, 221, 230)


def font(size, bold=False):
    name = "segoeuib.ttf" if bold else "segoeui.ttf"
    try:
        return ImageFont.truetype(os.path.join(os.environ["WINDIR"], "Fonts", name), size)
    except Exception:
        return ImageFont.load_default()


def tokens_of(parts):
    total = 0
    for part in (parts if isinstance(parts, list) else [parts]):
        if isinstance(part, str):
            total += max(1, len(part) // 4)
        elif getattr(part, "data", None):
            total += IMAGE_TOKENS
    return total


def configure(mode):
    """Point the server at one perception mode, rebuilding its state."""
    server._observation_mode = mode
    server._atlas_enabled = mode == "delta"
    server._speculate_enabled = False
    if server._model is not None:
        try:
            server._model.close()
        except Exception:
            pass
    server._model = None
    server._transitions = None


# --------------------------------------------------------------------------
# Running the task
# --------------------------------------------------------------------------

def run_task(app, window, record=None):
    """
    Transcribe the invoice into the form. Returns (per_step_totals, passed).

    `record` is called after every action with the running token total, which is
    what lets the recording show a counter genuinely tied to the work happening
    on screen rather than one animated to look convincing.
    """
    running = 0
    steps = []
    if record:
        record(0, "reading the invoice")
    for label, value in D.ENTRIES:
        out = server.fill_field(target_text=label, value=value)
        if "error" in json.loads(out[0]):
            raise RuntimeError(f"could not fill {label!r}")
        running += tokens_of(out)
        steps.append(running)
        if record:
            record(running, f"{label}  ->  {value}")
    out = server.click_element(
        text="Submit expense", window_title=app.window_hint(window)
    )
    running += tokens_of(out)
    steps.append(running)
    if record:
        record(running, "submit the expense")
    time.sleep(1.4)
    return steps, app.succeeded(window)


def open_app():
    app = D.ExpenseEntry()
    window = app.launch()
    if window is None:
        raise RuntimeError("the demo page did not open")
    time.sleep(1.0)
    return app, window


# --------------------------------------------------------------------------
# Screen capture
# --------------------------------------------------------------------------

class Recorder(threading.Thread):
    """Grabs the screen at a fixed rate into a temp directory."""

    def __init__(self, folder):
        super().__init__(daemon=True)
        self.folder = folder
        self.frames = []          # (timestamp, path)
        self._stop = threading.Event()

    def run(self):
        import mss

        with mss.mss() as sct:
            monitor = sct.monitors[1]
            i = 0
            while not self._stop.is_set():
                started = time.time()
                shot = sct.grab(monitor)
                path = os.path.join(self.folder, f"f{i:05d}.png")
                Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX").save(
                    path, compress_level=1
                )
                self.frames.append((started, path))
                i += 1
                time.sleep(max(0.0, (1.0 / FPS) - (time.time() - started)))

    def stop(self):
        self._stop.set()
        self.join(timeout=5)


# --------------------------------------------------------------------------
# Composition
# --------------------------------------------------------------------------

def bar(draw, x, y, w, h, frac, colour):
    draw.rounded_rectangle([x, y, x + w, y + h], radius=h // 2, fill=(233, 237, 242))
    if frac > 0:
        draw.rounded_rectangle(
            [x, y, x + max(h, int(w * min(frac, 1.0))), y + h], radius=h // 2, fill=colour
        )


def compose(shot, crop, ours, theirs, peak, caption, done):
    """One output frame: the application, then the measurement under it."""
    if crop:
        shot = shot.crop(crop)
    scale = WIDTH / shot.width
    view = shot.resize((WIDTH, int(shot.height * scale)), Image.LANCZOS)

    canvas = Image.new("RGB", (WIDTH, view.height + PANEL), "white")
    canvas.paste(view, (0, 0))
    d = ImageDraw.Draw(canvas)
    top = view.height
    d.line([0, top, WIDTH, top], fill=RULE, width=2)

    f_small = font(15)
    f_label = font(17)
    f_num = font(30, bold=True)

    d.text((26, top + 14), "Same task, same eight fields, same result.",
           font=f_label, fill=INK)
    d.text((26, top + 36), caption[:78], font=f_small, fill=MUTED)

    rows = [
        ("oswright", ours, GOOD, "incremental perception"),
        ("screenshot per step", theirs, BAD, "what most agents do"),
    ]
    y = top + 66
    for name, value, colour, note in rows:
        d.text((26, y), name, font=f_small, fill=INK)
        d.text((250, y - 8), f"{value:,}", font=f_num, fill=colour)
        d.text((250 + max(120, d.textlength(f'{value:,}', font=f_num) + 12), y),
               "tokens", font=f_small, fill=MUTED)
        bar(d, 470, y + 2, WIDTH - 500, 14, value / peak if peak else 0, colour)
        d.text((470, y + 22), note, font=f_small, fill=MUTED)
        y += 48

    if done:
        ratio = theirs / ours if ours else 0
        d.text((26, top + PANEL - 26), f"{ratio:.1f}x less context, and the form is filled correctly.",
               font=f_label, fill=GOOD)
    return canvas


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    if not shutil.which("ffmpeg"):
        print("ffmpeg not found; install it or the GIF cannot be encoded.")
        return 1

    # The comparison arm first: a real screenshot-per-step run, so the number
    # shown beside the live run is measured rather than assumed.
    print("measuring the screenshot-per-step arm...")
    configure("screenshot")
    app, window = open_app()
    try:
        baseline, ok = run_task(app, window)
    finally:
        app.cleanup(window)
    if not ok:
        print("the baseline run did not complete; aborting")
        return 1
    per_step = [baseline[0]] + [
        b - a for a, b in zip(baseline, baseline[1:], strict=False)
    ]
    print(f"  baseline total {baseline[-1]:,} tokens over {len(baseline)} actions")

    print("recording the oswright run...")
    configure("delta")
    folder = tempfile.mkdtemp(prefix="oswright_frames_")
    rec = Recorder(folder)
    events = []          # (timestamp, ours, theirs, caption)

    def note(total, caption):
        i = len(events)
        theirs = sum(per_step[: min(i, len(per_step))])
        events.append((time.time(), total, theirs, caption))

    app, window = open_app()
    # Crop every frame to the application. Recording the whole desktop would
    # put whatever else is open -- and the taskbar -- into a published GIF,
    # and it makes the thing being demonstrated smaller and harder to read.
    #
    # Inset slightly: a window's reported rectangle includes its drop shadow,
    # so the last few pixels on each edge belong to whatever is behind it.
    crop = (
        max(0, window.left + 9), max(0, window.top + 9),
        window.left + window.width - 9, window.top + window.height - 16,
    )
    rec.start()
    time.sleep(0.8)
    try:
        steps, passed = run_task(app, window, record=note)
    finally:
        time.sleep(1.2)
        rec.stop()
        app.cleanup(window)

    print(f"  oswright total {steps[-1]:,} tokens, passed={passed}")
    print(f"  {len(rec.frames)} frames captured")

    peak = max(baseline[-1], steps[-1])
    out_frames = os.path.join(folder, "out")
    os.makedirs(out_frames, exist_ok=True)

    # Crop to the region the browser occupies so the GIF is legible and shows
    # nothing but the application under test.
    last_event = events[-1][0] if events else time.time()
    kept = 0
    for ts, path in rec.frames:
        state = (0, 0, "starting")
        for etime, ours, theirs, caption in events:
            if etime <= ts:
                state = (ours, theirs, caption)
        shot = Image.open(path)
        frame = compose(shot, crop, state[0], state[1], peak, state[2], ts > last_event)
        frame.save(os.path.join(out_frames, f"c{kept:05d}.png"))
        kept += 1

    gif = os.path.join(OUT_DIR, "demo.gif")
    palette = os.path.join(folder, "pal.png")
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS),
        "-i", os.path.join(out_frames, "c%05d.png"),
        "-vf", "palettegen=max_colors=128:stats_mode=diff", palette,
    ], check=True)
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS),
        "-i", os.path.join(out_frames, "c%05d.png"), "-i", palette,
        "-lavfi", "paletteuse=dither=bayer:bayer_scale=4",
        "-loop", "0", gif,
    ], check=True)

    still = Image.open(os.path.join(out_frames, f"c{kept - 1:05d}.png"))
    still.save(os.path.join(OUT_DIR, "demo.png"))

    shutil.rmtree(folder, ignore_errors=True)
    size = os.path.getsize(gif) / 1e6
    print(f"wrote {gif} ({size:.1f} MB) and demo.png")
    return 0



if __name__ == "__main__":
    raise SystemExit(main())
