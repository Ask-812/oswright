"""
What each way of perceiving the screen actually costs.

Establishes the numbers the resolution cascade is ordered by. Notably it shows
that the accessibility tree is not automatically cheaper than pixels: on a deep
tree it can be slower than a full OCR pass.

Run:  python benchmarks/bench_methods.py
"""

import base64
import io
import platform
import statistics
import time

from oswright.capture import ScreenCapture
from oswright.detect import OCREngine

REPS = 5
IMAGE_TOKENS_PER_PIXEL = 1 / 750  # vision models bill roughly w*h/750


def bench(fn, reps=REPS):
    fn()
    times = []
    for _ in range(reps):
        t = time.perf_counter()
        result = fn()
        times.append((time.perf_counter() - t) * 1000)
    return statistics.median(times), result


def main():
    cap = ScreenCapture()
    ocr = OCREngine()
    rows = []

    ms, img = bench(lambda: cap.screenshot())
    rows.append(("screen capture (mss)", ms, f"{img.size[0]}x{img.size[1]}"))

    def full_ocr():
        ocr._cache.invalidate()
        return ocr.read_all(img)

    ms, elements = bench(full_ocr, reps=3)
    rows.append(("OCR, full screen", ms, f"{len(elements)} elements"))

    for frac, label in ((0.25, "quarter"), (0.0625, "sixteenth")):
        side = frac ** 0.5
        crop = img.crop((0, 0, int(img.width * side), int(img.height * side)))

        def region_ocr(crop=crop):
            ocr._cache.invalidate()
            return ocr.read_all(crop)

        ms, els = bench(region_ocr, reps=3)
        rows.append((f"OCR, {label} of screen", ms, f"{len(els)} elements"))

    # Region capture does NOT scale with area: mss has a fixed per-grab cost.
    small = {"left": 100, "top": 100, "width": 256, "height": 176}
    ms, _ = bench(lambda: cap.screenshot(region=small))
    rows.append(("capture 256x176 region", ms, "2.2% of screen area"))

    if platform.system() == "Windows":
        rows.extend(_windows_rows(cap))

    print(f"{'method':<40} {'median ms':>10}   result")
    print("-" * 82)
    for label, ms, extra in rows:
        print(f"{label:<40} {ms:>10.2f}   {extra}")

    _payload_table(img, elements)
    cap.close()


def _windows_rows(cap):
    import uiautomation as auto

    from oswright.dirty import DirtyTracker

    rows = []

    def walk(ctrl, out, depth=0, max_depth=12):
        if depth > max_depth or len(out) > 3000:
            return
        try:
            children = ctrl.GetChildren()
        except Exception:
            return
        for child in children:
            try:
                r = child.BoundingRectangle
                out.append((child.Name, r.left, r.top))
            except Exception:
                pass
            walk(child, out, depth + 1, max_depth)

    def uia_walk():
        root = auto.GetForegroundControl()
        out = []
        if root is not None:
            walk(root, out)
        return out

    ms, nodes = bench(uia_walk, reps=3)
    rows.append(("UIA tree walk (foreground window)", ms, f"{len(nodes)} elements"))

    from oswright import textprovider

    if textprovider.is_available():
        ms, hits = bench(lambda: textprovider.read_visible_text(), reps=3)
        rows.append(("UIA TextPattern read", ms, f"{len(hits)} text ranges"))
        ms, hits = bench(lambda: textprovider.find_text("the"), reps=3)
        rows.append(("UIA FindText (exact string)", ms, f"{len(hits)} hits"))

    tracker = DirtyTracker()
    tracker.update(cap.screenshot())
    if tracker.compositor_active:
        ms, _ = bench(lambda: tracker.nothing_changed(), reps=10)
        rows.append(("compositor change poll (no pixels)", ms, "DXGI dirty rects"))
    tracker.close()
    return rows


def _payload_table(img, elements):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue())
    image_tokens = int(img.width * img.height * IMAGE_TOKENS_PER_PIXEL)

    ocr_text = "\n".join(f"{e.text}|{e.x},{e.y}" for e in elements if e.text)

    print(f"\n{'observation payload':<40} {'bytes':>12} {'~tokens':>10}")
    print("-" * 66)
    print(f"{'screenshot (as vision tokens)':<40} {len(b64):>12,} {image_tokens:>10,}")
    print(f"{'OCR text dump':<40} {len(ocr_text):>12,} {len(ocr_text) // 4:>10,}")
    print(f"\nA 50-step task returning a screenshot per action costs "
          f"~{image_tokens * 50:,} image tokens.")


if __name__ == "__main__":
    main()
