"""
How long does a screen actually take to settle after an action?

Every action tool used to sleep a fixed 300 ms before reporting. Perception is
now around 45 ms, so if real UIs settle faster than that, the sleep is the
largest single cost in the agent loop and most of it is pure waiting.

The measurement uses the compositor, which reports what changed for well under a
millisecond and transfers no pixels.

Note what "settled" has to mean. A real desktop is never completely still: a
blinking caret and a ticking clock produce a change event roughly every 18 ms.
Measured while idle, those events cover a median of 32 pixels and a 90th
percentile of 64 -- while any genuine UI change covers tens of thousands. So
settling is defined as "no *large* change recently", not "no change".

Run:  python benchmarks/bench_settle.py
"""

import statistics
import time

from oswright.dirty import DirtyTracker
from oswright.input import Keyboard, Mouse
from oswright.settle import QUIET_AREA_PX, QUIET_MS, wait_until_settled


def main():
    tracker = DirtyTracker()
    source = tracker._get_compositor()
    if source is None:
        print("Compositor unavailable; cannot measure settle time here.")
        return

    print("Measuring settle time after real input events.")
    print("Actions are harmless: cursor moves and a modifier key press.")
    print(f"Settled means: no change larger than {QUIET_AREA_PX:,}px for {QUIET_MS}ms\n")

    wait_until_settled(source)
    results = {}
    waited = {}
    x0, y0 = Mouse.get_position()

    for label, action in (
        ("mouse move (small)", lambda: Mouse.move(x0 + 40, y0 + 30)),
        ("mouse move (large)", lambda: Mouse.move(x0 + 500, y0 + 260)),
        ("harmless key press", lambda: Keyboard.press("shift")),
    ):
        samples = []
        waits = []
        for _ in range(10):
            wait_until_settled(source)
            action()
            result = wait_until_settled(source)
            if result.settled:
                samples.append(result.elapsed_ms)
                waits.append(result.waited_ms)
            time.sleep(0.12)
        if samples:
            results[label] = samples
            waited[label] = waits

    Mouse.move(x0, y0)

    print(f"{'action':<24} {'median ms':>10} {'p90 ms':>9} {'max ms':>9} {'n':>4}")
    print("-" * 62)
    for label, samples in results.items():
        ordered = sorted(samples)
        p90 = ordered[min(int(len(ordered) * 0.9), len(ordered) - 1)]
        print(f"{label:<24} {statistics.median(samples):>10.1f} {p90:>9.1f} "
              f"{max(samples):>9.1f} {len(samples):>4}")

    everything = [s for samples in results.values() for s in samples]
    waits = [w for w in waited.values() for w in w]
    if everything and waits:
        median_change = statistics.median(everything)
        median_wait = statistics.median(waits)
        print("\nThe honest figure is the *wait*, not the change: detecting quiet")
        print(f"requires observing {QUIET_MS}ms of it, so that is a floor.\n")
        print("fixed sleep previously used per action : 300.0 ms")
        print(f"median time the screen was changing    : {median_change:>6.1f} ms")
        print(f"median actual wait                     : {median_wait:>6.1f} ms")
        print(f"saved per action                       : {300 - median_wait:>6.1f} ms")
        print(f"over a 50-step task                    : {(300 - median_wait) * 50 / 1000:>6.1f} s")

    tracker.close()


if __name__ == "__main__":
    main()
