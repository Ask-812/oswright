"""
End-to-end: the v0.4.0 perception path against v0.5.0's.

Simulates an agent loop and measures both wall-clock latency and the token cost
of the observation returned after each action.

Run:  python benchmarks/bench_pipeline.py
"""

import json
import statistics
import time

from oswright.capture import ScreenCapture
from oswright.cascade import resolve
from oswright.detect import OCREngine
from oswright.screenmodel import ScreenModel

STEPS = 14
PAUSE = 0.4
IMAGE_TOKENS_PER_PIXEL = 1 / 750


def approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def main():
    cap = ScreenCapture()
    ocr = OCREngine()
    probe = cap.screenshot()
    image_tokens = int(probe.width * probe.height * IMAGE_TOKENS_PER_PIXEL)

    print(f"screen {probe.width}x{probe.height}")
    print(f"one screenshot observation = ~{image_tokens:,} image tokens\n")

    # --- v0.4.0: full OCR and a fresh screenshot on every action ---
    old_ms, old_tokens = [], []
    for _ in range(STEPS):
        t = time.perf_counter()
        frame = cap.screenshot()
        ocr._cache.invalidate()
        ocr.read_all(frame)
        old_ms.append((time.perf_counter() - t) * 1000)
        old_tokens.append(image_tokens)
        time.sleep(PAUSE)

    # --- v0.5.0: incremental model, delta observations ---
    model = ScreenModel(cap, ocr)
    model.observe()  # cold start, excluded
    new_ms, new_tokens = [], []
    for _ in range(STEPS):
        t = time.perf_counter()
        delta = model.observe()
        payload = json.dumps({"observation": delta.to_dict()})
        new_ms.append((time.perf_counter() - t) * 1000)
        new_tokens.append(approx_tokens(payload))
        time.sleep(PAUSE)

    def row(name, ms, tokens):
        print(f"{name:<28} {statistics.median(ms):>9.1f} {sum(ms):>10.0f} "
              f"{statistics.median(tokens):>11,.0f} {sum(tokens):>13,}")

    print(f"{'':<28} {'med ms':>9} {'total ms':>10} {'med tokens':>11} {'total tokens':>13}")
    print("-" * 76)
    row("v0.4.0 full OCR + shot", old_ms, old_tokens)
    row("v0.5.0 incremental delta", new_ms, new_tokens)

    print(f"\nlatency : {statistics.median(old_ms) / max(statistics.median(new_ms), 0.01):.1f}x "
          f"faster (median per step)")
    print(f"tokens  : {sum(old_tokens) / max(sum(new_tokens), 1):.0f}x fewer "
          f"({sum(old_tokens):,} -> {sum(new_tokens):,} over {STEPS} steps)")

    # --- lookup cost: the cascade's whole point ---
    known = [e.text for e in model.elements if len(e.text) > 4][:5]
    if known:
        times = []
        for q in known:
            t = time.perf_counter()
            r = resolve(q, model, allow_uia=False, allow_text_pattern=False)
            times.append((time.perf_counter() - t) * 1000)
            assert r.found
        cascade_ms = statistics.median(times)

        t = time.perf_counter()
        ocr._cache.invalidate()
        ocr.find_text(cap.screenshot(), known[0])
        old_lookup = (time.perf_counter() - t) * 1000

        print("\nlookup of known text:")
        print(f"  v0.4.0 (capture + full OCR) : {old_lookup:8.1f} ms")
        print(f"  v0.5.0 (cascade rung 0)     : {cascade_ms:8.3f} ms   "
              f"{old_lookup / max(cascade_ms, 0.001):,.0f}x cheaper")

    print(f"\n{json.dumps(model.efficiency(), indent=1)}")
    model.close()
    cap.close()


if __name__ == "__main__":
    main()
