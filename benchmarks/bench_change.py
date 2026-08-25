"""
How much does the screen actually change between observations?

This is the load-bearing measurement for the whole perception design. If only a
few percent of the screen changes between agent steps, then re-reading the
entire screen every step is doing orders of magnitude more work than necessary.

Run:  python benchmarks/bench_change.py
"""

import statistics
import time

import numpy as np

from oswright.capture import ScreenCapture
from oswright.dirty import TILE, DirtyTracker, tile_signature

FRAMES = 24
INTERVAL = 0.5


def main():
    cap = ScreenCapture()
    print(f"Sampling the live desktop: {FRAMES} frames, {INTERVAL}s apart\n")

    frames = []
    for _ in range(FRAMES):
        frames.append(np.asarray(cap.screenshot().convert("RGB")))
        time.sleep(INTERVAL)

    pixel_ratios, tile_ratios, hash_ms = [], [], []
    for a, b in zip(frames, frames[1:], strict=False):
        diff = np.abs(a.astype(np.int16) - b.astype(np.int16)).max(axis=2)
        pixel_ratios.append(float(np.count_nonzero(diff > 10)) / diff.size)

        t = time.perf_counter()
        sa, sb = tile_signature(a), tile_signature(b)
        hash_ms.append((time.perf_counter() - t) * 1000)
        tile_ratios.append(float(np.count_nonzero(sa != sb)) / sa.size)

    def show(label, values):
        pct = [v * 100 for v in values]
        print(f"{label:<32} median={statistics.median(pct):7.3f}%  "
              f"mean={statistics.mean(pct):7.3f}%  max={max(pct):7.3f}%")

    show("changed pixels per frame", pixel_ratios)
    show(f"changed {TILE}px tiles per frame", tile_ratios)
    print(f"{'tile-hash cost (2 frames)':<32} median={statistics.median(hash_ms):7.2f}ms")

    moved = [r for r in tile_ratios if r > 0]
    still = len(tile_ratios) - len(moved)
    print(f"\nframes with no tile change: {still}/{len(tile_ratios)}")
    if moved:
        median = statistics.median(moved)
        print(f"when something moved, median tiles touched: {median * 100:.2f}%")
        print(f"=> analysing only dirty tiles is ~{1 / median:.0f}x less work")

    # Does the compositor agree, and how much does asking cost?
    tracker = DirtyTracker()
    tracker.update(cap.screenshot())
    if tracker.compositor_active:
        polls = []
        for _ in range(20):
            t = time.perf_counter()
            tracker.nothing_changed()
            polls.append((time.perf_counter() - t) * 1000)
            time.sleep(0.1)
        print(f"\ncompositor poll (no pixels transferred): "
              f"median {statistics.median(polls):.3f} ms")
        t = time.perf_counter()
        cap.screenshot()
        print(f"full screen capture for comparison:      "
              f"{(time.perf_counter() - t) * 1000:.1f} ms")
    else:
        print("\ncompositor change source unavailable on this machine")

    tracker.close()
    cap.close()


if __name__ == "__main__":
    main()
