"""
Resolution cascade: spend the least compute that still answers the question.

Finding something on screen has many possible answers of very different cost.
Reading everything with OCR and then searching the result is the most expensive
one, and it is what most agents do on every single step.

Instead, try the rungs in increasing order of cost and stop at the first that
answers. Cost then tracks how *novel* the request is rather than how large the
screen is: a control the model already knows about is free, one that just
appeared costs a small incremental scan, and only a genuinely unfamiliar screen
pays for a full pass.

Rung order here follows measurements on real applications, not intuition. In
particular UI Automation's TextPattern sits *below* incremental OCR despite
being exact, because scanning a window's controls for it costs a few hundred
milliseconds of cross-process COM -- considerably more than OCR of the handful
of tiles that actually changed. It earns its place by being exact when OCR is
ambiguous, not by being fast.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from oswright.dirty import Region
from oswright.screenmodel import Element, ScreenModel

logger = logging.getLogger(__name__)


@dataclass
class Candidate:
    """One possible answer, with the rung that produced it."""

    text: str
    region: Region
    source: str
    rung: int
    confidence: float = 1.0

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
            "source": self.source,
            "rung": self.rung,
        }


@dataclass
class Resolution:
    """The outcome of a cascade lookup."""

    query: str
    found: bool = False
    best: Optional[Candidate] = None
    candidates: list[Candidate] = field(default_factory=list)
    rung: int = -1
    source: str = ""
    duration_ms: float = 0.0
    rungs_tried: list[str] = field(default_factory=list)

    def to_dict(self, max_candidates: int = 8) -> dict:
        payload = {
            "query": self.query,
            "found": self.found,
            "rungs_tried": self.rungs_tried,
            "duration_ms": round(self.duration_ms, 1),
        }
        if self.best is not None:
            x, y = self.best.center
            payload.update(
                {
                    "x": x,
                    "y": y,
                    "text": self.best.text,
                    "source": self.best.source,
                    "rung": self.rung,
                    "match_count": len(self.candidates),
                    "candidates": [c.to_dict() for c in self.candidates[:max_candidates]],
                }
            )
        else:
            payload["error"] = f"{self.query!r} not found on screen"
        return payload


def _from_elements(elements: list[Element], rung: int) -> list[Candidate]:
    return [
        Candidate(e.text, e.region, e.source, rung, e.confidence) for e in elements
    ]


def _rank(candidates: list[Candidate], query: str) -> list[Candidate]:
    """
    Order candidates by how well they answer the query.

    An exact match beats a substring, a short match beats a long one (a button
    labelled exactly "Save" is a better answer than a paragraph containing the
    word), and ties break top-to-bottom, left-to-right.
    """
    needle = query.lower()

    def key(c: Candidate):
        text = c.text.lower()
        return (
            0 if text == needle else 1,
            len(text),
            c.region.top,
            c.region.left,
        )

    return sorted(candidates, key=key)


def _window_bounds(window_title: Optional[str]):
    """
    The screen rectangle of `window_title`, if it can be resolved.

    Returns (left, top, right, bottom), or None when there is no window to
    restrict to -- in which case the caller must not filter, since filtering
    against an unknown rectangle would discard every correct answer.
    """
    if not window_title:
        return None
    try:
        from oswright.window import get_window_region

        region = get_window_region(title=window_title)
    except Exception:  # pragma: no cover - platform dependent
        logger.debug("Could not resolve window bounds", exc_info=True)
        return None

    if not region:
        return None
    return (
        region["left"],
        region["top"],
        region["left"] + region["width"],
        region["top"] + region["height"],
    )


def _within(candidates: list[Candidate], bounds) -> list[Candidate]:
    """
    Keep only candidates whose centre lies inside `bounds`.

    The pixel rungs read the whole screen, so without this a request to click
    "Eight" in Calculator can land on the word "Eight" in an unrelated window
    that happens to be visible. That is not hypothetical: this benchmark's own
    console output contained the labels it was asking for, and the click went
    to (1587, 452) -- outside a Calculator window spanning x 767..1185 -- so the
    sum silently came out wrong.

    `window_title` used to constrain only the accessibility rungs, which was
    accurate in the docstring and wrong in effect: the caller has named the
    window it means, and an automation tool acting on a different one is a
    correctness problem before it is a perception problem.
    """
    if bounds is None:
        return candidates

    left, top, right, bottom = bounds
    kept = []
    for candidate in candidates:
        region = candidate.region
        cx = region.left + region.width / 2
        cy = region.top + region.height / 2
        if left <= cx <= right and top <= cy <= bottom:
            kept.append(candidate)
    return kept


def _prefers_accessibility(query: str, exact: bool) -> bool:
    """
    Should the accessibility tree be tried before the pixel rungs?

    OCR does not merely misread very short labels -- it does not detect them at
    all. Measured on Calculator, Windows OCR returned 30 text elements from the
    window (DEG, MC, Function, Trigonometry, log...) and **not one digit**. OCR
    engines are trained on words and lines; an isolated glyph on a button has no
    line context to belong to.

    So for one- and two-character targets the pixel rungs are not a cheaper path
    to the same answer, they are a guaranteed miss followed by the accessibility
    rung anyway. Trying accessibility first is both faster and more likely to
    work.
    """
    return len(query.strip()) <= 2


def resolve(
    query: str,
    model: ScreenModel,
    exact: bool = False,
    window_title: Optional[str] = None,
    allow_uia: bool = True,
    allow_text_pattern: bool = True,
    allow_full_rescan: bool = True,
    allow_pixels: bool = True,
) -> Resolution:
    """
    Locate `query` on screen using the cheapest rung that can answer.

    Args:
        query: The visible text to find.
        model: The screen model to consult and update.
        exact: Require the whole label to equal `query`.
        window_title: Restrict accessibility lookups to one window.
        allow_uia: Permit the accessibility-tree rung.
        allow_text_pattern: Permit the exact-text rung.
        allow_full_rescan: Permit the final full-screen OCR pass.
        allow_pixels: Permit every rung that reads pixels. Turning this off
            leaves only the accessibility rungs, which is the posture other
            Windows GUI agents take; it exists so that the value of mixing the
            two can be measured rather than argued.
    """
    started = time.perf_counter()
    result = Resolution(query=query)

    # Resolved once, lazily: the pixel rungs read the whole screen, so every
    # one of them has to be held to the window the caller named.
    bounds = _window_bounds(window_title)

    def finish(candidates: list[Candidate], rung: int, source: str) -> Resolution:
        ranked = _rank(candidates, query)
        result.candidates = ranked
        result.best = ranked[0]
        result.found = True
        result.rung = rung
        result.source = source
        result.duration_ms = (time.perf_counter() - started) * 1000
        return result

    # Rung 0 -- what the model already knows, but only when the compositor can
    # confirm the model still describes the screen. No capture, no OCR, no IPC.
    #
    # Answering from memory *without* that confirmation is unsound, and it
    # fails as a wrong click rather than a slow one: against VS Code this rung
    # returned (600, 66) for a file that was at (211, 157), left over from a
    # previous window. The check costs a fraction of a millisecond, because the
    # compositor is already tracking the answer.
    result.rungs_tried.append("model")
    if allow_pixels and model.is_current():
        hits = _within(_from_elements(model.find(query, exact=exact), 0), bounds)
        if hits:
            return finish(hits, 0, "model")

    # For targets OCR cannot see at all, go straight to the accessibility tree
    # rather than paying for two pixel passes that are certain to miss.
    if allow_uia and _prefers_accessibility(query, exact):
        result.rungs_tried.append("uia-first")
        for candidate in _uia_candidates(query, exact, window_title):
            return finish([candidate], 2, "uia")

    # Rung 1 -- refresh only what moved, then look again. This is the common
    # path when the agent has just acted and the target has appeared.
    if allow_pixels:
        result.rungs_tried.append("incremental")
        model.observe()
        hits = _within(_from_elements(model.find(query, exact=exact), 1), bounds)
        if hits:
            return finish(hits, 1, "incremental")

    # Rung 2 -- the accessibility tree. Slower than the scan above, but it knows
    # what things *are*: a Button named "Save" rather than pixels reading "Save".
    if allow_uia:
        result.rungs_tried.append("uia")
        for candidate in _uia_candidates(query, exact, window_title):
            return finish([candidate], 2, "uia")

    # Rung 3 -- the application's own text buffer. Exact characters and exact
    # boxes, unaffected by font, DPI or antialiasing. Reserved for when the
    # cheaper pixel-based rungs came up empty.
    if allow_text_pattern:
        result.rungs_tried.append("uia-text")
        candidates = _text_pattern_candidates(query, window_title)
        if candidates:
            return finish(candidates, 3, "uia-text")

    # Rung 4 -- give up on being clever and read the whole screen.
    if allow_full_rescan and allow_pixels:
        result.rungs_tried.append("full-rescan")
        model.observe(force_full=True)
        hits = _within(_from_elements(model.find(query, exact=exact), 4), bounds)
        if hits:
            return finish(hits, 4, "full-rescan")

    result.duration_ms = (time.perf_counter() - started) * 1000
    return result


def _uia_candidates(
    query: str, exact: bool, window_title: Optional[str]
) -> list[Candidate]:
    """Accessibility-tree matches, if UI Automation is available."""
    try:
        from oswright.accessibility import find_all_elements, is_available
    except ImportError:  # pragma: no cover - optional dependency
        return []

    if not is_available():
        return []

    try:
        elements = find_all_elements(name=query, window_title=window_title)
    except Exception:
        logger.debug("UIA rung failed", exc_info=True)
        return []

    needle = query.lower()
    candidates = []
    for el in elements:
        if exact and el.name.lower() != needle:
            continue
        candidates.append(
            Candidate(
                text=el.name,
                region=Region(el.left, el.top, el.left + el.width, el.top + el.height),
                source=f"uia:{el.control_type}",
                rung=2,
            )
        )
    return _rank(candidates, query)


def _text_pattern_candidates(query: str, window_title: Optional[str]) -> list[Candidate]:
    """Exact matches from the application's own text buffer."""
    try:
        from oswright import textprovider
    except ImportError:  # pragma: no cover - optional dependency
        return []

    if not textprovider.is_available():
        return []

    try:
        hits = textprovider.find_text(query, window_title=window_title)
    except Exception:
        logger.debug("TextPattern rung failed", exc_info=True)
        return []

    return [
        Candidate(text=h.text, region=h.region, source=h.source, rung=3) for h in hits
    ]
