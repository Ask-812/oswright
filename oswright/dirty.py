"""
Change detection: find the parts of the screen that actually moved.

Re-reading the whole screen on every observation is the single largest waste in
a GUI agent. Measured on a live desktop, the median observation changes 0.012%
of pixels and 0.42% of 64px tiles -- a full rescan does roughly 240x more work
than the change warrants.

This module answers "what changed since last time?" cheaply, so the expensive
passes (OCR, UI Automation) can be pointed only at those regions.

The tile-signature implementation here needs no special privileges and works on
every platform. On Windows, DXGI Desktop Duplication can supply dirty rectangles
directly from the compositor for free; see `DirtyTracker` for the seam where that
plugs in.
"""

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

TILE = 64

# Text runs horizontally, so a crop that cuts a line vertically splits words and
# changes how OCR groups glyphs ("Placer" -> "Plac" + "er"). Measured agreement
# between full-frame OCR and OCR of a crop of the same area is only ~91%, and
# the disagreements are almost entirely this. Padding wider than it is tall buys
# back most of that for very little extra area.
H_PAD = 96
V_PAD = 24


@dataclass(frozen=True)
class Region:
    """An axis-aligned screen rectangle, in absolute screen coordinates."""

    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def area(self) -> int:
        return max(0, self.width) * max(0, self.height)

    def intersects(self, other: "Region") -> bool:
        return not (
            self.right <= other.left
            or self.left >= other.right
            or self.bottom <= other.top
            or self.top >= other.bottom
        )

    def union(self, other: "Region") -> "Region":
        return Region(
            min(self.left, other.left),
            min(self.top, other.top),
            max(self.right, other.right),
            max(self.bottom, other.bottom),
        )

    def clamp(self, width: int, height: int) -> "Region":
        return Region(
            max(0, self.left),
            max(0, self.top),
            min(width, self.right),
            min(height, self.bottom),
        )

    def padded(self, h_pad: int = H_PAD, v_pad: int = V_PAD) -> "Region":
        return Region(
            self.left - h_pad, self.top - v_pad, self.right + h_pad, self.bottom + v_pad
        )

    def to_dict(self) -> dict:
        return {
            "left": self.left,
            "top": self.top,
            "width": self.width,
            "height": self.height,
        }


def tile_signature(arr: np.ndarray, tile: int = TILE, stride: int = 4) -> np.ndarray:
    """
    Compute a per-tile digest of a frame.

    Subsampled by `stride` in both axes: digesting every pixel costs more than
    the extra sensitivity is worth, and a change that a 4x4 sampling grid misses
    entirely is smaller than any glyph worth reading.

    The position weighting matters -- a plain sum would collide whenever pixels
    are merely rearranged within a tile, which is exactly what scrolling text
    does.
    """
    if arr.ndim != 3:
        raise ValueError(f"Expected an HxWxC array, got shape {arr.shape}")

    h, w = arr.shape[:2]
    ty, tx = h // tile, w // tile
    if ty == 0 or tx == 0:
        return np.zeros((0, 0), dtype=np.int64)

    trimmed = arr[: ty * tile, : tx * tile]
    blocks = trimmed.reshape(ty, tile, tx, tile, -1).swapaxes(1, 2)
    sampled = blocks[:, :, ::stride, ::stride, :]
    flat = sampled.reshape(ty, tx, -1).astype(np.int64)
    weights = np.arange(1, flat.shape[2] + 1, dtype=np.int64)
    return (flat * weights).sum(axis=2)


def merge_regions(regions: list[Region], max_passes: int = 6) -> list[Region]:
    """Coalesce overlapping rectangles until none overlap."""
    regions = list(regions)
    for _ in range(max_passes):
        merged: list[Region] = []
        consumed = [False] * len(regions)
        changed = False

        for i, region in enumerate(regions):
            if consumed[i]:
                continue
            current = region
            consumed[i] = True
            for j in range(i + 1, len(regions)):
                if consumed[j]:
                    continue
                if current.intersects(regions[j]):
                    current = current.union(regions[j])
                    consumed[j] = True
                    changed = True
            merged.append(current)

        regions = merged
        if not changed:
            break
    return regions


def _runs_to_regions(mask: np.ndarray, tile: int, width: int, height: int) -> list[Region]:
    """Turn a boolean tile mask into padded rectangles, merging horizontal runs."""
    regions: list[Region] = []
    for row in range(mask.shape[0]):
        cols = np.flatnonzero(mask[row])
        if cols.size == 0:
            continue

        start = prev = int(cols[0])
        for col in list(cols[1:]) + [None]:
            if col is not None and int(col) == prev + 1:
                prev = int(col)
                continue
            regions.append(
                Region(
                    start * tile,
                    row * tile,
                    (prev + 1) * tile,
                    (row + 1) * tile,
                )
                .padded()
                .clamp(width, height)
            )
            if col is not None:
                start = prev = int(col)
    return merge_regions(regions)


class DirtyTracker:
    """
    Tracks which parts of the screen changed between successive frames.

    Where the Windows compositor can answer, it is consulted first: asking
    "did anything change?" via Desktop Duplication costs well under a
    millisecond and transfers no pixels, whereas capturing a frame to hash costs
    around 33 ms. Most observations during an agent session are of an idle
    screen, so this skips the capture entirely rather than performing it and
    then discovering there was nothing to do.

    The compositor is used *only* as a fast negative. When it reports that
    something did change, the regions still come from hashing the captured
    frame, because the two are measured over slightly different intervals: the
    capture happens after the poll, so compositor rectangles can under-report
    relative to the pixels actually captured. Under-reporting would mean a
    changed region never gets re-read, which is exactly the silent text loss
    this design is meant to prevent. Hashing is measured against the very frame
    being analysed, so it cannot disagree with it.

    The first frame is always reported as fully dirty, since there is no prior
    state to compare against.
    """

    def __init__(self, tile: int = TILE, stride: int = 4, use_compositor: bool = True):
        self._tile = tile
        self._stride = stride
        self._signature: Optional[np.ndarray] = None
        self._size: Optional[tuple[int, int]] = None
        self._use_compositor = use_compositor
        self._compositor = None
        self._compositor_tried = False
        self.compositor_skips = 0

    def _get_compositor(self):
        """Lazily create the compositor change source, once."""
        if not self._use_compositor or self._compositor_tried:
            return self._compositor
        self._compositor_tried = True
        try:
            from oswright._dxgi_windows import DxgiDirtySource, is_available

            if is_available():
                self._compositor = DxgiDirtySource()
        except Exception:  # pragma: no cover - platform dependent
            logger.debug("Compositor change source unavailable", exc_info=True)
        return self._compositor

    @property
    def compositor_active(self) -> bool:
        """True if the compositor is answering change queries."""
        source = self._get_compositor()
        return source is not None and source.failure_reason is None

    def nothing_changed(self) -> bool:
        """
        Cheap, authoritative check for "the screen is untouched".

        Returns True only when the compositor positively confirms it. A False
        result means either something changed or we could not tell, so callers
        must fall back to capturing and hashing.
        """
        if self._signature is None:
            return False  # nothing observed yet, so there is no baseline

        source = self._get_compositor()
        if source is None:
            return False

        rects = source.poll()
        if rects is None:
            return False
        if rects:
            return False

        self.compositor_skips += 1
        return True

    def reset(self):
        """Forget prior state, so the next frame counts as entirely dirty."""
        self._signature = None
        self._size = None

    def close(self):
        """Release the compositor source, if one was created."""
        if self._compositor is not None:
            self._compositor.close()
            self._compositor = None

    def update(self, image: Image.Image) -> list[Region]:
        """
        Compare `image` against the previous frame.

        Returns:
            Padded, non-overlapping regions covering everything that changed.
            An empty list means the frame is identical to the last one.
        """
        arr = np.asarray(image.convert("RGB"))
        height, width = arr.shape[:2]
        signature = tile_signature(arr, self._tile, self._stride)

        previous = self._signature
        resized = self._size != (width, height)
        self._signature = signature
        self._size = (width, height)

        if previous is None or resized or previous.shape != signature.shape:
            return [Region(0, 0, width, height)]

        mask = previous != signature
        if not mask.any():
            return []

        return _runs_to_regions(mask, self._tile, width, height)

    @staticmethod
    def coverage(regions: list[Region], width: int, height: int) -> float:
        """Fraction of the frame the regions cover (overlaps counted once)."""
        total = width * height
        if total <= 0:
            return 0.0
        return min(1.0, sum(r.area for r in merge_regions(regions)) / total)
