"""
Knowing when the screen has finished responding.

After an action, an agent has to wait for the interface to react before looking
at it. The usual approach is a fixed sleep -- OSWright used 300 ms -- chosen to
be long enough for the slowest case. Every action then pays the worst case,
whether or not anything took that long. With perception now around 45 ms, that
fixed sleep was the single largest cost in the loop.

The compositor already knows when the screen is changing. Asking it costs well
under a millisecond and transfers no pixels, so the wait can end as soon as the
interface actually stops moving.

The subtlety is what "stopped" means. A real desktop is never completely still.
Measured on an idle screen, a blinking caret and a ticking clock produce a
change event roughly every 18 ms, covering a median of 32 pixels. Waiting for
zero change would wait forever -- the first implementation did exactly that and
timed out on every single sample. Genuine UI changes cover tens of thousands of
pixels, so the criterion is "nothing *large* has changed recently".
"""

import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# How often to ask the compositor. Each poll is a fraction of a millisecond, so
# this is set by how precisely we want to detect the end of the change, not cost.
POLL_S = 0.008

# How long without a large change before the screen counts as settled. Long
# enough to bridge the gap between two frames of a multi-step transition, short
# enough not to dominate the wait.
QUIET_MS = 60

# Changes smaller than this are ignored as background noise. Measured on an idle
# desktop: caret and clock updates cover a median of 32px and a 90th percentile
# of 64px. This threshold is ~63x63px -- far above that noise floor, far below
# any real interface change.
QUIET_AREA_PX = 4000

# Never wait longer than this. An animation, a video or a progress spinner may
# never settle, and an agent that hangs is worse than one that looks slightly
# early.
DEFAULT_TIMEOUT_S = 1.0


@dataclass
class SettleResult:
    """The outcome of waiting for the screen to stop changing."""

    settled: bool
    elapsed_ms: float
    waited_ms: float
    changed: bool = False
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "settled": self.settled,
            "changed": self.changed,
            "settle_ms": round(self.elapsed_ms, 1),
            "waited_ms": round(self.waited_ms, 1),
            **({"reason": self.reason} if self.reason else {}),
        }


def wait_until_settled(
    source,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    quiet_ms: int = QUIET_MS,
    quiet_area_px: int = QUIET_AREA_PX,
) -> SettleResult:
    """
    Wait for the screen to stop changing meaningfully.

    Args:
        source: A compositor change source exposing `poll()`.
        timeout_s: Give up after this long; an animation may never settle.
        quiet_ms: How long without a large change counts as settled.
        quiet_area_px: Changes smaller than this are treated as noise.

    Returns:
        A SettleResult. `settled` is False if the timeout was reached or the
        compositor could not answer, in which case the caller should fall back
        to a fixed wait.
    """
    started = time.perf_counter()
    if source is None:
        return SettleResult(False, 0.0, 0.0, reason="no compositor")

    last_big = started
    saw_change = False

    while True:
        now = time.perf_counter()
        waited = (now - started) * 1000

        if now - started > timeout_s:
            return SettleResult(
                False, waited, waited, changed=saw_change,
                reason=f"still changing after {timeout_s * 1000:.0f}ms",
            )

        rects = source.poll()
        if rects is None:
            return SettleResult(False, waited, waited, changed=saw_change,
                                reason="compositor stopped answering")

        if rects:
            area = sum((x2 - x1) * (y2 - y1) for x1, y1, x2, y2 in rects)
            if area > quiet_area_px:
                last_big = now
                saw_change = True

        if (now - last_big) * 1000 >= quiet_ms:
            return SettleResult(
                True,
                elapsed_ms=max(0.0, (last_big - started) * 1000),
                waited_ms=waited,
                changed=saw_change,
            )

        time.sleep(POLL_S)


def settle_after_action(tracker, timeout_s: float = DEFAULT_TIMEOUT_S) -> SettleResult:
    """
    Wait for the interface to finish responding to an action.

    Falls back to a conservative fixed sleep whenever the compositor cannot
    answer, so behaviour on a machine without Desktop Duplication is unchanged
    from before this existed.
    """
    source = tracker._get_compositor() if tracker is not None else None
    if source is None:
        time.sleep(0.3)
        return SettleResult(False, 300.0, 300.0, reason="no compositor; fixed wait")

    result = wait_until_settled(source, timeout_s=timeout_s)
    if not result.settled and result.reason == "compositor stopped answering":
        time.sleep(0.3)
        result.waited_ms += 300.0
    return result
