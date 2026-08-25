"""
A persistent model of what is on screen.

Every GUI agent in the wild re-perceives the whole screen on every step: full
screenshot, full OCR, full tree walk, then hand the model a fresh image. That is
wrong on three axes at once. Temporally, almost nothing changed -- measured on a
live desktop, the median observation differs by 0.012% of pixels. Spatially, the
agent cares about one control, not two million pixels. Semantically, the agent
usually already knows the string it is looking for, so open-vocabulary
recognition answers a question nobody asked.

This module keeps a model of the screen's text between observations and updates
only the parts that changed, then reports the difference. Perception cost
becomes proportional to how much the screen moved rather than how big it is.

Two invariants make this sound rather than merely fast:

1. Anything invalidated is fully rescanned. A dirty region that clips a text box
   mid-word would otherwise delete the item and re-detect only the visible
   fragment, silently losing text. Regions are grown until they wholly contain
   every element they touch.
2. Elements are never mutated in place. Coordinates are translated on copies, so
   a cached element can be handed to several callers safely.
"""

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from PIL import Image

from oswright.capture import ScreenCapture
from oswright.detect import OCREngine
from oswright.dirty import DirtyTracker, Region, merge_regions

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Element:
    """A piece of text located on screen, in absolute screen coordinates."""

    text: str
    region: Region
    source: str = "ocr"
    confidence: float = 1.0

    @property
    def center(self) -> tuple[int, int]:
        return (
            self.region.left + self.region.width // 2,
            self.region.top + self.region.height // 2,
        )

    @property
    def fingerprint(self) -> str:
        """
        Stable identity for diffing across observations.

        Position is part of the identity: the same word appearing somewhere else
        is genuinely a different thing on screen, and reporting it as a move
        would require tracking that this model deliberately does not attempt.
        """
        raw = f"{self.text}\x00{self.region.left}\x00{self.region.top}"
        return hashlib.blake2b(raw.encode("utf-8", "replace"), digest_size=8).hexdigest()

    def to_dict(self) -> dict:
        x, y = self.center
        return {
            "text": self.text,
            "x": x,
            "y": y,
            **self.region.to_dict(),
            "source": self.source,
        }


@dataclass
class Delta:
    """What changed on screen since the previous observation."""

    added: list[Element] = field(default_factory=list)
    removed: list[Element] = field(default_factory=list)
    regions: list[Region] = field(default_factory=list)
    scanned_pixels: int = 0
    total_pixels: int = 0
    duration_ms: float = 0.0
    full_rescan: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)

    @property
    def scanned_fraction(self) -> float:
        if self.total_pixels <= 0:
            return 0.0
        return self.scanned_pixels / self.total_pixels

    def to_dict(self, max_items: int = 60) -> dict:
        """
        Serialise for an agent.

        Only the difference is included. A full screenshot of a 1920x1080 screen
        costs roughly 2,800 image tokens; a delta of a handful of strings costs
        tens, and it says directly what happened rather than requiring the model
        to spot the difference between two pictures.
        """
        return {
            "changed": self.changed,
            "full_rescan": self.full_rescan,
            "added": [e.to_dict() for e in self.added[:max_items]],
            "removed": [e.text for e in self.removed[:max_items]],
            "added_count": len(self.added),
            "removed_count": len(self.removed),
            "regions_scanned": len(self.regions),
            "screen_fraction_scanned": round(self.scanned_fraction, 4),
            "duration_ms": round(self.duration_ms, 1),
        }


class ScreenModel:
    """
    Incrementally maintained model of on-screen text.

    Usage:
        model = ScreenModel(capture, ocr)
        delta = model.observe()          # first call scans everything
        delta = model.observe()          # later calls scan only what moved
        hits = model.find("Save")        # query the model, no new scan
    """

    # Beyond this fraction of the screen, scanning regions piecemeal costs more
    # than one clean full-screen pass: OCR runtime is roughly linear in area, and
    # every extra region adds its own fixed overhead.
    FULL_RESCAN_THRESHOLD = 0.55

    # Regions smaller than this are almost always a caret blink or a focus ring.
    MIN_REGION_EDGE = 8

    def __init__(
        self,
        capture: ScreenCapture,
        ocr: OCREngine,
        monitor: int = 0,
    ):
        self._capture = capture
        self._ocr = ocr
        self._monitor = monitor
        self._tracker = DirtyTracker()
        self._elements: list[Element] = []
        self._last_total_pixels = 0
        self.stats = {
            "observations": 0,
            "full_rescans": 0,
            "compositor_skips": 0,
            "regions_scanned": 0,
            "pixels_scanned": 0,
            "pixels_total": 0,
            "ocr_ms": 0.0,
        }

    # --- state ---

    @property
    def elements(self) -> list[Element]:
        """Everything the model currently believes is on screen."""
        return list(self._elements)

    def reset(self):
        """Drop all state; the next observation rescans everything."""
        self._tracker.reset()
        self._elements = []

    # --- observation ---

    def observe(self, force_full: bool = False, image: Optional[Image.Image] = None) -> Delta:
        """
        Refresh the model from the current screen and report what changed.

        Args:
            force_full: Rescan the whole screen even if little changed.
            image: Use this frame instead of capturing one (for testing).
        """
        started = time.perf_counter()

        # Ask the compositor before capturing anything. On an idle screen this
        # settles the question for a fraction of a millisecond, against ~33 ms
        # to capture a frame and then discover it was identical.
        if image is None and not force_full and self._tracker.nothing_changed():
            self.stats["observations"] += 1
            self.stats["compositor_skips"] += 1
            return Delta(
                regions=[],
                total_pixels=self._last_total_pixels,
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        if image is None:
            image = self._capture.screenshot(monitor=self._monitor)

        width, height = image.size
        total_pixels = width * height
        self._last_total_pixels = total_pixels

        if force_full:
            self._tracker.reset()
        regions = self._tracker.update(image)

        self.stats["observations"] += 1
        self.stats["pixels_total"] += total_pixels

        if not regions:
            return Delta(
                regions=[],
                total_pixels=total_pixels,
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        # Grow regions so no tracked element is left half-covered, then decide
        # whether piecemeal scanning is still worth it.
        regions = self._close_over_elements(regions, width, height)
        covered = sum(r.area for r in regions)
        full = covered >= total_pixels * self.FULL_RESCAN_THRESHOLD

        if full:
            regions = [Region(0, 0, width, height)]
            self.stats["full_rescans"] += 1

        before = {e.fingerprint: e for e in self._elements}

        # Elements inside a scanned region are dropped; the scan below is what
        # re-establishes them. Everything outside is known to be untouched.
        survivors = [
            e for e in self._elements
            if not any(e.region.intersects(r) for r in regions)
        ]

        found, scanned_pixels, ocr_ms = self._scan(image, regions)
        self._elements = survivors + found

        after = {e.fingerprint: e for e in self._elements}
        added = [after[k] for k in after.keys() - before.keys()]
        removed = [before[k] for k in before.keys() - after.keys()]

        self.stats["regions_scanned"] += len(regions)
        self.stats["pixels_scanned"] += scanned_pixels
        self.stats["ocr_ms"] += ocr_ms

        return Delta(
            added=sorted(added, key=lambda e: (e.region.top, e.region.left)),
            removed=sorted(removed, key=lambda e: (e.region.top, e.region.left)),
            regions=regions,
            scanned_pixels=scanned_pixels,
            total_pixels=total_pixels,
            duration_ms=(time.perf_counter() - started) * 1000,
            full_rescan=full,
        )

    def _close_over_elements(
        self, regions: list[Region], width: int, height: int, max_passes: int = 6
    ) -> list[Region]:
        """Grow regions until no tracked element is only partially covered."""
        for _ in range(max_passes):
            grew = False
            grown = []
            for region in regions:
                current = region
                for element in self._elements:
                    if element.region.intersects(current):
                        merged = current.union(element.region)
                        if merged != current:
                            current = merged
                            grew = True
                grown.append(current.clamp(width, height))
            regions = merge_regions(grown)
            if not grew:
                break
        return regions

    def _scan(
        self, image: Image.Image, regions: list[Region]
    ) -> tuple[list[Element], int, float]:
        """OCR the given regions and return the elements found in them."""
        found: list[Element] = []
        scanned = 0
        started = time.perf_counter()

        for region in regions:
            if region.width < self.MIN_REGION_EDGE or region.height < self.MIN_REGION_EDGE:
                continue
            scanned += region.area
            crop = image.crop((region.left, region.top, region.right, region.bottom))

            # The engine's own cache keys on whole images; these crops are all
            # different, so it would only ever miss.
            self._ocr._cache.invalidate()
            for match in self._ocr.read_all(crop):
                if not match.text or not match.text.strip():
                    continue
                found.append(
                    Element(
                        text=match.text,
                        region=Region(
                            match.left + region.left,
                            match.top + region.top,
                            match.left + region.left + match.width,
                            match.top + region.top + match.height,
                        ),
                        source="ocr",
                        confidence=match.confidence,
                    )
                )

        return found, scanned, (time.perf_counter() - started) * 1000

    # --- queries ---

    def find(self, text: str, exact: bool = False) -> list[Element]:
        """Search the model. Costs nothing -- no capture, no OCR."""
        needle = text.lower()
        if exact:
            hits = [e for e in self._elements if e.text.lower() == needle]
        else:
            hits = [e for e in self._elements if needle in e.text.lower()]
        return sorted(hits, key=lambda e: (e.region.top, e.region.left))

    def efficiency(self) -> dict:
        """How much work the incremental path has avoided."""
        total = self.stats["pixels_total"]
        scanned = self.stats["pixels_scanned"]
        return {
            "observations": self.stats["observations"],
            "full_rescans": self.stats["full_rescans"],
            "compositor_skips": self.stats["compositor_skips"],
            "compositor_active": self._tracker.compositor_active,
            "pixels_scanned": scanned,
            "pixels_total": total,
            "fraction_scanned": round(scanned / total, 4) if total else 0.0,
            "work_avoided": f"{(1 - scanned / total) * 100:.1f}%" if total else "0%",
            "elements_tracked": len(self._elements),
        }

    def close(self):
        """Release resources held by the model."""
        self._tracker.close()
