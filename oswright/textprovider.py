"""
Text straight from the application, without OCR.

Windows UI Automation's TextPattern exposes a control's actual text buffer, and
`TextRange.GetBoundingRectangles()` maps any sub-range of it back to screen
coordinates. Where a control implements it, this is strictly better than OCR:
the characters are exact rather than recognised, the boxes are exact rather than
estimated, and it costs no recognition at all.

`TextRange.FindText` matters most here. An agent almost never needs to answer
"what does this say?" -- it needs "where does it say Save?". FindText answers
exactly that question directly against the app's own text buffer, which is why
this module is the rung the cascade tries before ever touching a pixel.

Coverage is the catch, and it is uneven: Chromium, WPF and Word-based controls
implement TextPattern well, while many Win32 and Electron surfaces expose
nothing. Callers must be prepared to fall back.
"""

import logging
import platform
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Optional

from oswright.dirty import Region

logger = logging.getLogger(__name__)

UIA_TEXT_PATTERN_ID = 10014

# Reading an entire document can mean megabytes crossing a process boundary.
# Visible ranges are what an agent can actually click, so cap accordingly.
MAX_RANGE_CHARS = 20000

# Every FindText call is a cross-process COM round-trip. A terminal with a long
# scrollback can spend seconds enumerating matches nobody asked for, so both the
# per-range match count and the overall wall clock are bounded.
MAX_MATCHES_PER_RANGE = 16
DEFAULT_BUDGET_S = 0.4

_AVAILABLE = False
_auto = None
_core = None

if platform.system() == "Windows":
    try:
        import uiautomation as _auto  # noqa: F811

        _core = _auto.uiautomation._AutomationClient.instance().UIAutomationCore
        _AVAILABLE = True
    except Exception:  # pragma: no cover - depends on optional dependency
        _AVAILABLE = False


def is_available() -> bool:
    """True if UI Automation TextPattern can be used on this machine."""
    return _AVAILABLE


@dataclass
class TextHit:
    """A located piece of text, in absolute screen coordinates."""

    text: str
    region: Region
    control_name: str = ""
    control_type: str = ""
    source: str = "uia-text"

    @property
    def center(self) -> tuple[int, int]:
        return (
            self.region.left + self.region.width // 2,
            self.region.top + self.region.height // 2,
        )

    def to_dict(self) -> dict:
        x, y = self.center
        return {
            "text": self.text,
            "x": x,
            "y": y,
            **self.region.to_dict(),
            "control": self.control_name,
            "control_type": self.control_type,
            "source": self.source,
        }


def _get_text_pattern(element):
    """Return the IUIAutomationTextPattern for an element, or None."""
    try:
        raw = element.GetCurrentPattern(UIA_TEXT_PATTERN_ID)
    except Exception:
        return None
    if not raw:
        return None
    try:
        return raw.QueryInterface(_core.IUIAutomationTextPattern)
    except Exception:
        return None


def _rects_of(text_range) -> list[Region]:
    """
    Convert a text range's bounding rectangles into Regions.

    UIA returns a flat array of doubles in (left, top, width, height) groups --
    one group per line the range spans, so a wrapped sentence yields several.
    """
    try:
        raw = text_range.GetBoundingRectangles()
    except Exception:
        return []
    if raw is None:
        return []

    try:
        values = list(raw)
    except TypeError:
        return []

    regions = []
    for i in range(0, len(values) - 3, 4):
        left, top, width, height = (float(v) for v in values[i : i + 4])
        if width <= 0 or height <= 0:
            continue
        regions.append(
            Region(int(left), int(top), int(left + width), int(top + height))
        )
    return regions


def _describe(control) -> tuple[str, str]:
    try:
        return (control.Name or "", control.ControlTypeName or "")
    except Exception:
        return ("", "")


def iter_text_controls(root, max_depth: int = 12, cap: int = 400) -> Iterator:
    """Yield descendants of `root` that implement TextPattern."""
    if not _AVAILABLE or root is None:
        return

    seen = 0
    stack = [(root, 0)]
    while stack:
        control, depth = stack.pop()
        if depth > max_depth or seen >= cap:
            return
        seen += 1

        try:
            if _get_text_pattern(control.Element) is not None:
                yield control
        except Exception:
            pass

        try:
            children = control.GetChildren()
        except Exception:
            children = []
        for child in reversed(children):
            stack.append((child, depth + 1))


def _visible_ranges(pattern) -> list:
    """Visible text ranges, falling back to the whole document."""
    try:
        visible = pattern.GetVisibleRanges()
        if visible is not None and visible.Length > 0:
            return [visible.GetElement(i) for i in range(visible.Length)]
    except Exception:
        pass
    try:
        return [pattern.DocumentRange]
    except Exception:
        return []


def read_control_text(control) -> list[TextHit]:
    """Read the visible text of one control, with per-line coordinates."""
    pattern = _get_text_pattern(control.Element)
    if pattern is None:
        return []

    name, ctype = _describe(control)
    hits = []
    for text_range in _visible_ranges(pattern):
        try:
            text = text_range.GetText(MAX_RANGE_CHARS)
        except Exception:
            continue
        if not text or not text.strip():
            continue
        for region in _rects_of(text_range):
            hits.append(
                TextHit(
                    text=text.strip(),
                    region=region,
                    control_name=name,
                    control_type=ctype,
                )
            )
    return hits


def find_in_control(
    control,
    needle: str,
    ignore_case: bool = True,
    max_hits: int = 0,
    deadline: Optional[float] = None,
) -> list[TextHit]:
    """
    Locate occurrences of `needle` inside one control.

    Uses the app's own text buffer via FindText, so this is exact string
    matching rather than recognition -- no OCR, no glyph comparison, and
    immune to font, DPI and antialiasing.

    Args:
        max_hits: Stop after this many hits. 0 means no limit.
        deadline: `time.monotonic()` value after which to give up. Each COM call
            crosses a process boundary and a control with a large scrollback can
            take seconds, so an unbounded search is not safe to put first in a
            cascade.
    """
    pattern = _get_text_pattern(control.Element)
    if pattern is None:
        return []

    name, ctype = _describe(control)
    hits: list[TextHit] = []

    for scope in _visible_ranges(pattern):
        search = scope
        for _ in range(MAX_MATCHES_PER_RANGE):
            if deadline is not None and time.monotonic() > deadline:
                return hits
            try:
                found = search.FindText(needle, False, ignore_case)
            except Exception:
                break
            if not found:
                break

            try:
                matched = found.GetText(MAX_RANGE_CHARS)
            except Exception:
                matched = needle

            for region in _rects_of(found):
                hits.append(
                    TextHit(
                        text=matched,
                        region=region,
                        control_name=name,
                        control_type=ctype,
                        source="uia-findtext",
                    )
                )
                if max_hits and len(hits) >= max_hits:
                    return hits

            # Continue after this match: collapse the cursor to the match end,
            # then extend it back out to the end of the enclosing scope.
            try:
                nxt = search.Clone()
                # 0 = Start endpoint, 1 = End endpoint
                nxt.MoveEndpointByRange(0, found, 1)
                if nxt.CompareEndpoints(0, nxt, 1) >= 0:
                    break
                search = nxt
            except Exception:
                break

    return hits


def _root_for(window_title: Optional[str]):
    if not _AVAILABLE:
        return None
    try:
        if window_title:
            control = _auto.WindowControl(searchDepth=1, SubName=window_title)
            return control if control.Exists(maxSearchSeconds=1) else None
        return _auto.GetForegroundControl()
    except Exception:
        return None


def find_text(
    needle: str,
    window_title: Optional[str] = None,
    ignore_case: bool = True,
    max_controls: int = 60,
    max_hits: int = 8,
    budget_s: float = DEFAULT_BUDGET_S,
) -> list[TextHit]:
    """
    Find `needle` on screen using the application's own text, without OCR.

    Bounded by both `max_hits` and `budget_s`: this is the first rung of a
    cascade, so it must fail fast rather than block a caller that has cheaper
    options available.

    Returns an empty list when nothing matches *or* when no control in the
    window implements TextPattern; callers cannot distinguish the two and
    should treat empty as "this rung could not answer".
    """
    root = _root_for(window_title)
    if root is None:
        return []

    deadline = time.monotonic() + budget_s
    hits: list[TextHit] = []
    for index, control in enumerate(iter_text_controls(root)):
        if index >= max_controls or time.monotonic() > deadline:
            break
        remaining = max_hits - len(hits) if max_hits else 0
        hits.extend(
            find_in_control(
                control,
                needle,
                ignore_case=ignore_case,
                max_hits=remaining,
                deadline=deadline,
            )
        )
        if max_hits and len(hits) >= max_hits:
            break
    return hits


def read_visible_text(
    window_title: Optional[str] = None,
    max_controls: int = 60,
    budget_s: float = DEFAULT_BUDGET_S,
) -> list[TextHit]:
    """Read all visible TextPattern text in a window, with coordinates."""
    root = _root_for(window_title)
    if root is None:
        return []

    deadline = time.monotonic() + budget_s
    hits: list[TextHit] = []
    for index, control in enumerate(iter_text_controls(root)):
        if index >= max_controls or time.monotonic() > deadline:
            break
        hits.extend(read_control_text(control))
    return hits
