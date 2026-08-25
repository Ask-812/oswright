"""
What the screen atlas saves on a return visit.

Arriving at a screen the agent has seen before should not cost a full read. The
atlas recognises the layout, spot-checks a few regions, and reuses what it
learned last time.

Run:  python benchmarks/bench_atlas.py
"""

import statistics
import tempfile
import time
from pathlib import Path

from PIL import Image

from oswright.atlas import ScreenContext, UIAtlas, layout_signature
from oswright.capture import ScreenCapture
from oswright.detect import OCREngine
from oswright.screenmodel import ScreenModel

TRIALS = 5


def main():
    cap = ScreenCapture()
    ocr = OCREngine()
    path = Path(tempfile.gettempdir()) / "oswright_atlas_bench.json"
    path.unlink(missing_ok=True)

    atlas = UIAtlas(path=path, autoload=False)
    context = ScreenContext(app="benchmark", window_class="bench", width=0, height=0)

    frame = cap.screenshot()
    context.width, context.height = frame.size

    # --- cold: read the whole screen ---
    cold = []
    for _ in range(3):
        model = ScreenModel(cap, ocr)
        t = time.perf_counter()
        model.observe(image=frame)
        cold.append((time.perf_counter() - t) * 1000)
        model.close()
    elements = ScreenModel(cap, ocr)
    elements.observe(image=frame)

    entry = atlas.remember(frame, context, elements.elements)
    if entry is None:
        print("Nothing verifiable on screen; open a window with some text and retry.")
        cap.close()
        return

    # --- warm: recognise and verify ---
    warm, sig = [], []
    hits = 0
    for _ in range(TRIALS):
        current = cap.screenshot()

        t = time.perf_counter()
        layout_signature(current)
        sig.append((time.perf_counter() - t) * 1000)

        t = time.perf_counter()
        recalled = atlas.recall(current, context)
        warm.append((time.perf_counter() - t) * 1000)
        hits += recalled is not None
        time.sleep(0.3)

    cold_ms = statistics.median(cold)
    warm_ms = statistics.median(warm)

    print(f"screen {frame.size[0]}x{frame.size[1]}, {len(elements.elements)} elements remembered")
    print(f"{'':<34} {'median ms':>10}")
    print("-" * 46)
    print(f"{'cold: full screen read':<34} {cold_ms:>10.1f}")
    print(f"{'  of which layout signature':<34} {statistics.median(sig):>10.2f}")
    print(f"{'warm: recognise + verify':<34} {warm_ms:>10.2f}")
    print(f"\nrecall hit rate: {hits}/{TRIALS}")
    if warm_ms > 0:
        print(f"warm start is {cold_ms / warm_ms:,.0f}x cheaper than reading the screen")

    # --- it must refuse screens it should not recognise ---
    print("\nrejections (all should be None):")
    blank = Image.new("RGB", frame.size, "white")
    print(f"  blank screen            : {atlas.recall(blank, context)}")
    print(f"  inverted screen         : {atlas.recall(Image.eval(frame, lambda v: 255 - v), context)}")
    other = ScreenContext(
        app="different-app",
        window_class="bench",
        width=frame.size[0],
        height=frame.size[1],
    )
    print(f"  same pixels, other app  : {atlas.recall(frame, other)}")

    print(f"\n{atlas.summary()}")
    path.unlink(missing_ok=True)
    elements.close()
    cap.close()


if __name__ == "__main__":
    main()
