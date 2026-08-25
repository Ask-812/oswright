"""
Remembering screens across visits.

Applications are deterministic. The Save dialog looks the same every time it
opens; a settings page has the same controls in the same places on Tuesday as it
did on Monday. Yet an agent re-reads all of it from scratch on every visit, and
again in the next session, because nothing remembers what was learned.

The atlas stores what a screen looked like, keyed by a structural fingerprint,
and reuses it on the next visit. Arriving at a known screen becomes a warm start
rather than a full read.

The important word is *verify*. A cache that is simply trusted would eventually
hand an agent stale coordinates and make it click the wrong thing, which is the
worst failure this system can have. So a recognised screen is never used on the
strength of its fingerprint alone: a handful of distinctive elements are
re-read first, and the cached layout is used only if they are still where and
what they should be. A failed check costs three small OCR passes and falls back
to reading the screen normally; a passed check skips a full-screen read.

This is speculative execution: predict, verify cheaply, and pay the full price
only when the prediction was wrong.
"""

import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

from oswright.dirty import Region
from oswright.screenmodel import Element

logger = logging.getLogger(__name__)

# The signature works on a heavily downsampled edge map. Small enough that
# computing it is trivial next to OCR, coarse enough that a blinking caret or a
# ticking clock barely perturbs it, fine enough that a different dialog does.
SIGNATURE_GRID = (48, 27)
SIGNATURE_BITS = SIGNATURE_GRID[0] * SIGNATURE_GRID[1]

# Edge strength above which a cell counts as "structure". A fixed threshold,
# deliberately not normalised against the frame's own maximum: normalising makes
# every cell depend on the single strongest edge anywhere on screen, so one new
# dark pixel shifts the entire signature.
EDGE_THRESHOLD = 12

# Two screens are considered the same layout if their signatures differ in at
# most this fraction of cells. Measured on a live desktop: an idle screen drifts
# by 0.0000 between frames, while a structurally different image sits at 0.379 —
# an enormous gap, so the exact value is not delicate. Exact matching would be
# useless (a caret or clock always differs) and too loose a threshold starts
# confusing genuinely different screens. Verification is what makes an
# occasional false positive safe rather than dangerous.
MATCH_TOLERANCE = 0.02

# How many regions to spot-check when a remembered screen is recognised.
# Each check is a tiny image comparison, so this is cheap; the count is about
# spatial coverage, not cost.
VERIFY_SAMPLES = 4

# Verification compares small greyscale renderings of a region. The size adapts
# to the region's shape: squashing a wide heading into a fixed square throws
# away exactly the horizontal detail that distinguishes one heading from
# another, so the thumbnail keeps the region's aspect ratio.
THUMBNAIL_HEIGHT = 12
THUMBNAIL_MAX_WIDTH = 64
# A thumbnail cell counts as changed if it differs by this much, and two regions
# are considered different once this fraction of cells has changed.
#
# A mean absolute difference does not work here, which is worth recording: a
# small piece of text inside a mostly-blank region barely moves the mean, so a
# changed heading in a wide box scored 1.47 against 0.00 for an identical one --
# far too close to threshold safely. Counting changed cells is not diluted by
# the blank area around the change. Measured: identical regions score 0.000,
# genuinely changed ones 0.020 to 0.310.
THUMBNAIL_CELL_DELTA = 24
THUMBNAIL_CHANGE_LIMIT = 0.01

# Resolution of the whole-screen content check. Fine enough that changed text
# registers, coarse enough that a blinking caret is well under one cell.
CONTENT_GRID = (64, 36)

DEFAULT_MAX_ENTRIES = 250


def _default_path() -> Path:
    base = os.environ.get("OSWRIGHT_HOME") or (Path.home() / ".oswright")
    return Path(base) / "atlas.json"


@dataclass
class ScreenContext:
    """What window this screen belongs to."""

    app: str = ""
    window_class: str = ""
    title: str = ""
    width: int = 0
    height: int = 0

    def key(self) -> str:
        """
        Identity of the *container*, deliberately excluding the title.

        Titles change with content ("report.txt" versus "notes.txt") while the
        layout does not, so including them would miss most legitimate hits.
        """
        return f"{self.app}|{self.window_class}|{self.width}x{self.height}"


@dataclass
class Verifier:
    """
    A spot-check: a small patch of the screen and what it looked like.

    Verification compares *pixels*, not text. Re-reading the region with OCR
    and comparing strings sounds natural but does not work: OCR output is not a
    stable identity. Its word segmentation depends on the crop it is given
    (measured: full-frame and crop OCR of the same area agree only ~91%), and
    small on-screen text is frequently garbled the same way twice only by luck
    -- stored labels here included 'Elevatc' and 'Subarr&'. Comparing one
    garbling against a differently-cropped garbling rejects screens that are
    perfectly intact.

    Pixels have none of those problems, and cost nothing to compare.
    """

    region: Region
    thumbnail: bytes
    text: str = ""  # kept only so failures are readable in logs

    def to_dict(self) -> dict:
        return {
            "left": self.region.left,
            "top": self.region.top,
            "right": self.region.right,
            "bottom": self.region.bottom,
            "thumbnail": self.thumbnail.hex(),
            "text": self.text,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Verifier":
        return cls(
            region=Region(data["left"], data["top"], data["right"], data["bottom"]),
            thumbnail=bytes.fromhex(data["thumbnail"]),
            text=data.get("text", ""),
        )


def _content_thumbnail(image: Image.Image) -> bytes:
    """
    A coarse greyscale rendering of the whole screen.

    The sampled verifier regions only prove that the places they cover are
    unchanged; a difference anywhere else goes unnoticed. The layout signature
    does cover everything, but it is an edge map at 48x27 and two screens whose
    text differs while their boxes do not look identical to it.

    This fills that gap: it is content rather than structure, and it covers the
    whole frame. Still coarse enough that a caret changes well under one cell.
    """
    small = image.convert("L").resize(CONTENT_GRID, Image.BILINEAR)
    return np.asarray(small, dtype=np.uint8).tobytes()


def _thumbnail_size(region: Region) -> tuple[int, int]:
    """Thumbnail dimensions for a region, preserving its aspect ratio."""
    if region.height <= 0:
        return (8, THUMBNAIL_HEIGHT)
    aspect = region.width / region.height
    width = int(round(THUMBNAIL_HEIGHT * aspect))
    return (max(8, min(THUMBNAIL_MAX_WIDTH, width)), THUMBNAIL_HEIGHT)


def _thumbnail(image: Image.Image, region: Region) -> Optional[bytes]:
    """A tiny greyscale rendering of a screen region, for comparison."""
    box = (region.left, region.top, region.right, region.bottom)
    if box[2] - box[0] < 4 or box[3] - box[1] < 4:
        return None
    if box[0] < 0 or box[1] < 0 or box[2] > image.width or box[3] > image.height:
        return None
    patch = image.crop(box).convert("L").resize(_thumbnail_size(region), Image.BILINEAR)
    return np.asarray(patch, dtype=np.uint8).tobytes()


def _thumbnails_match(a: bytes, b: bytes) -> bool:
    """
    Do two region thumbnails look the same?

    Compares how many cells changed rather than by how much on average, so a
    small but real change inside a largely blank region is not averaged away.
    The per-cell threshold keeps antialiasing and subpixel jitter from counting.
    """
    if a is None or b is None or len(a) != len(b):
        return False
    left = np.frombuffer(a, dtype=np.uint8).astype(np.int16)
    right = np.frombuffer(b, dtype=np.uint8).astype(np.int16)
    diff = np.abs(left - right)
    changed = float(np.count_nonzero(diff > THUMBNAIL_CELL_DELTA)) / diff.size
    return changed <= THUMBNAIL_CHANGE_LIMIT


@dataclass(eq=False)
class AtlasEntry:
    """
    A remembered screen.

    `eq=False` because the signature is a numpy array: a generated `__eq__`
    would return an array rather than a bool, and anything using `in` or
    `list.remove` on these would raise "truth value of an array is ambiguous".
    Identity comparison is what callers actually want here.
    """

    signature: np.ndarray
    context_key: str
    elements: list[Element] = field(default_factory=list)
    verifiers: list[Verifier] = field(default_factory=list)
    content: bytes = b""
    hits: int = 0
    misses: int = 0
    created_at: float = field(default_factory=time.time)
    last_used_at: float = field(default_factory=time.time)

    @property
    def entry_id(self) -> str:
        """
        Stable identifier for this remembered screen.

        Derived from content rather than assigned, so it survives saving and
        reloading and lets other structures (such as the transition model) refer
        to a screen without holding it.
        """
        return hashlib.blake2b(
            (_pack(self.signature) + "|" + self.context_key).encode(),
            digest_size=8,
        ).hexdigest()

    def to_dict(self) -> dict:
        return {
            "signature": _pack(self.signature),
            "context_key": self.context_key,
            "hits": self.hits,
            "misses": self.misses,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
            "verifiers": [v.to_dict() for v in self.verifiers],
            "content": self.content.hex(),
            "elements": [
                {
                    "text": e.text,
                    "left": e.region.left,
                    "top": e.region.top,
                    "right": e.region.right,
                    "bottom": e.region.bottom,
                    "source": e.source,
                }
                for e in self.elements
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AtlasEntry":
        elements = [
            Element(
                text=item["text"],
                region=Region(item["left"], item["top"], item["right"], item["bottom"]),
                source=item.get("source", "atlas"),
            )
            for item in data.get("elements", [])
        ]
        return cls(
            signature=_unpack(data["signature"]),
            context_key=data.get("context_key", ""),
            elements=elements,
            verifiers=[Verifier.from_dict(v) for v in data.get("verifiers", [])],
            content=bytes.fromhex(data.get("content", "")),
            hits=data.get("hits", 0),
            misses=data.get("misses", 0),
            created_at=data.get("created_at", time.time()),
            last_used_at=data.get("last_used_at", time.time()),
        )


def _choose_verifier_regions(
    elements: list[Element], count: int = VERIFY_SAMPLES
) -> list[Element]:
    """
    Pick which parts of the screen to spot-check.

    Chosen to be substantial and spatially spread: a larger region carries more
    signal than a few glyphs, and sampling far-apart regions catches a layout
    that shifted in only one place.
    """
    usable = [
        e for e in elements
        if e.region.width >= 24 and e.region.height >= 8 and len(e.text.strip()) >= 3
        # Very wide regions are poor checks: the text inside them occupies a
        # small fraction of the area, so a real change is easy to miss. Measured,
        # a tight box around changed text scored 0.180 while a full-width box
        # around the same change scored only 0.020.
        and e.region.width <= 600
    ]
    if not usable:
        return []

    # Biggest first, then spread the picks over the vertical extent.
    usable.sort(key=lambda e: e.region.area, reverse=True)
    candidates = usable[: max(count * 8, 16)]
    candidates.sort(key=lambda e: (e.region.top, e.region.left))

    if len(candidates) <= count:
        return candidates

    step = len(candidates) / count
    return [candidates[min(int(i * step), len(candidates) - 1)] for i in range(count)]


def layout_signature(image: Image.Image) -> np.ndarray:
    """
    A structural signature of a screen's layout, as a boolean grid.

    Built from a downsampled edge map rather than raw pixels: what matters is
    where the boxes and text blocks are, not their exact colours. Returned as a
    grid rather than a hash so that two screens can be compared by *how much*
    they differ — an exact hash would be defeated by a single blinking caret.

    Note what this deliberately does not capture: at this resolution, two
    screens with the same arrangement but different words look identical. That
    is the division of labour. The signature decides *which* remembered screen
    might apply; verification decides whether it actually does.
    """
    width, height = image.size

    # PIL's `reduce` is an integer box filter and is markedly cheaper than
    # resizing a full-resolution frame directly (measured 2.7 ms versus 7.7 ms
    # at 1920x1080). Shrink most of the way with it, then resize the remainder.
    factor = max(1, min(width // SIGNATURE_GRID[0], height // SIGNATURE_GRID[1]) // 4)
    source = image.reduce(factor) if factor > 1 else image

    small = source.resize(SIGNATURE_GRID, Image.BILINEAR).convert("L")
    arr = np.asarray(small, dtype=np.int16)

    # Gradient magnitude: edges are layout, flat areas are content.
    dx = np.abs(np.diff(arr, axis=1, prepend=arr[:, :1]))
    dy = np.abs(np.diff(arr, axis=0, prepend=arr[:1, :]))
    return (dx + dy) > EDGE_THRESHOLD


def signature_distance(a: np.ndarray, b: np.ndarray) -> float:
    """Fraction of cells in which two layout signatures disagree."""
    if a.shape != b.shape:
        return 1.0
    return float(np.count_nonzero(a != b)) / a.size


def _pack(signature: np.ndarray) -> str:
    return np.packbits(signature.ravel()).tobytes().hex()


def _unpack(packed: str) -> np.ndarray:
    raw = np.frombuffer(bytes.fromhex(packed), dtype=np.uint8)
    bits = np.unpackbits(raw)[:SIGNATURE_BITS]
    return bits.reshape(SIGNATURE_GRID[1], SIGNATURE_GRID[0]).astype(bool)


def current_context(monitor_size: Optional[tuple[int, int]] = None) -> ScreenContext:
    """
    Describe the window currently in front, for keying the atlas.

    Falls back to an empty context off Windows or when the window cannot be
    identified; an empty key still works, it just groups everything together.
    """
    try:
        from oswright.window import list_windows

        for window in list_windows():
            if window.is_foreground:
                return ScreenContext(
                    app=window.process_name or "",
                    window_class="",
                    title=window.title or "",
                    width=monitor_size[0] if monitor_size else window.width,
                    height=monitor_size[1] if monitor_size else window.height,
                )
    except Exception:
        logger.debug("Could not determine the foreground window", exc_info=True)

    return ScreenContext(
        width=monitor_size[0] if monitor_size else 0,
        height=monitor_size[1] if monitor_size else 0,
    )


class UIAtlas:
    """
    Remembers screens so repeat visits do not need a full read.

    Usage:
        atlas = UIAtlas()
        entry = atlas.lookup(frame, context)
        if entry and atlas.verify(entry, frame, ocr):
            elements = entry.elements       # warm start, no full scan
        else:
            elements = expensive_full_scan()
            atlas.remember(frame, context, elements)
    """

    def __init__(
        self,
        path: Optional[Path] = None,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        autoload: bool = True,
    ):
        self.path = Path(path) if path else _default_path()
        self.max_entries = max_entries
        # Grouped by window identity: only screens from the same application and
        # window size can possibly match, which keeps comparison cheap and makes
        # cross-application collisions impossible by construction.
        self._entries: dict[str, list[AtlasEntry]] = {}
        self.stats = {
            "lookups": 0,
            "recognised": 0,
            "verified": 0,
            "rejected": 0,
            "stored": 0,
        }
        if autoload:
            self.load()

    def __len__(self) -> int:
        return sum(len(group) for group in self._entries.values())

    @property
    def entries(self) -> list[AtlasEntry]:
        return [e for group in self._entries.values() for e in group]

    # --- recall ---

    def lookup(
        self, image: Image.Image, context: ScreenContext
    ) -> Optional[AtlasEntry]:
        """
        Find a remembered screen whose layout matches this frame.

        Returns the closest entry within tolerance, or None. This does not
        verify the entry -- `verify()` does, and callers must not act on the
        result before it passes.
        """
        self.stats["lookups"] += 1
        group = self._entries.get(context.key())
        if not group:
            return None

        signature = layout_signature(image)
        best, best_distance = None, 1.0
        for entry in group:
            distance = signature_distance(signature, entry.signature)
            if distance < best_distance:
                best, best_distance = entry, distance

        if best is None or best_distance > MATCH_TOLERANCE:
            return None

        self.stats["recognised"] += 1
        return best

    def verify(self, entry: AtlasEntry, image: Image.Image) -> bool:
        """
        Spot-check that a recognised screen really is the remembered one.

        Compares small renderings of a few regions. Costs no OCR at all, so
        this is cheap enough to run on every recall.

        Fails closed: anything unexpected -- no verifiers, a region that no
        longer fits on screen, a comparison that cannot be made -- is treated
        as a failure, because acting on a stale layout is worse than doing the
        work again.
        """
        if not entry.verifiers:
            entry.misses += 1
            self.stats["rejected"] += 1
            return False

        if entry.content and not _thumbnails_match(entry.content, _content_thumbnail(image)):
            entry.misses += 1
            self.stats["rejected"] += 1
            logger.debug("Atlas entry rejected: screen content differs")
            return False

        for verifier in entry.verifiers:
            current = _thumbnail(image, verifier.region)
            if not _thumbnails_match(verifier.thumbnail, current):
                entry.misses += 1
                self.stats["rejected"] += 1
                logger.debug(
                    "Atlas entry rejected: region at %s no longer matches (%r)",
                    verifier.region.to_dict(), verifier.text,
                )
                return False

        entry.hits += 1
        entry.last_used_at = time.time()
        self.stats["verified"] += 1
        return True

    def recall(
        self, image: Image.Image, context: ScreenContext
    ) -> Optional[AtlasEntry]:
        """Look up and verify in one step. Returns the entry only if it holds."""
        entry = self.lookup(image, context)
        if entry is None:
            return None
        return entry if self.verify(entry, image) else None

    # --- learning ---

    def remember(
        self, image: Image.Image, context: ScreenContext, elements: list[Element]
    ) -> Optional[AtlasEntry]:
        """
        Store what this screen looks like. Screens with no text are skipped.

        If a near-identical layout is already remembered it is replaced rather
        than duplicated, so revisiting a screen refreshes it instead of filling
        the atlas with near-copies.
        """
        if not elements:
            return None

        verifiers = []
        for element in _choose_verifier_regions(elements):
            thumbnail = _thumbnail(image, element.region)
            if thumbnail is not None:
                verifiers.append(
                    Verifier(region=element.region, thumbnail=thumbnail, text=element.text)
                )

        if not verifiers:
            # Without a way to check it later, a remembered screen could only be
            # trusted blindly, which is exactly what this design refuses to do.
            logger.debug("Not remembering screen: no verifiable regions found")
            return None

        signature = layout_signature(image)
        content = _content_thumbnail(image)
        key = context.key()
        group = self._entries.setdefault(key, [])

        for i, existing in enumerate(group):
            if signature_distance(signature, existing.signature) <= MATCH_TOLERANCE:
                group[i] = AtlasEntry(
                    signature=signature,
                    context_key=key,
                    elements=list(elements),
                    verifiers=verifiers,
                    content=content,
                    hits=existing.hits,
                    misses=existing.misses,
                    created_at=existing.created_at,
                )
                self.stats["stored"] += 1
                return group[i]

        entry = AtlasEntry(
            signature=signature,
            context_key=key,
            elements=list(elements),
            verifiers=verifiers,
            content=content,
        )
        group.append(entry)
        self.stats["stored"] += 1
        self._evict()
        return entry

    def _evict(self):
        """Drop the least useful entries once the atlas is full."""
        total = len(self)
        if total <= self.max_entries:
            return

        ranked = sorted(
            ((key, entry) for key, group in self._entries.items() for entry in group),
            key=lambda kv: (kv[1].hits, kv[1].last_used_at),
        )
        for key, entry in ranked[: total - self.max_entries]:
            group = self._entries.get(key, [])
            if entry in group:
                group.remove(entry)
            if not group:
                self._entries.pop(key, None)

    def forget(self, entry: AtlasEntry):
        """Remove an entry, e.g. after it repeatedly fails verification."""
        group = self._entries.get(entry.context_key, [])
        if entry in group:
            group.remove(entry)
        if not group:
            self._entries.pop(entry.context_key, None)

    # --- persistence ---

    def load(self) -> bool:
        """Load a previously saved atlas. Never raises."""
        try:
            if not self.path.exists():
                return False
            data = json.loads(self.path.read_text(encoding="utf-8"))
            entries: dict[str, list[AtlasEntry]] = {}
            for item in data.get("entries", []):
                entry = AtlasEntry.from_dict(item)
                entries.setdefault(entry.context_key, []).append(entry)
            self._entries = entries
            logger.debug("Loaded %d atlas entries from %s", len(self), self.path)
            return True
        except Exception:
            # A corrupt or unreadable cache must never break the agent; the
            # worst case is doing the perception work that a cache would have
            # saved.
            logger.warning("Could not load atlas from %s; starting empty", self.path)
            self._entries = {}
            return False

    def save(self) -> bool:
        """Persist the atlas. Never raises."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "entries": [e.to_dict() for e in self.entries],
            }
            # Write via a temporary file so an interrupted save cannot leave a
            # half-written atlas behind.
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(self.path)
            return True
        except Exception:
            logger.warning("Could not save atlas to %s", self.path, exc_info=True)
            return False

    def find_by_id(self, entry_id: str) -> Optional[AtlasEntry]:
        """Look up a remembered screen by its stable identifier."""
        for entry in self.entries:
            if entry.entry_id == entry_id:
                return entry
        return None

    def summary(self) -> dict:
        looked = self.stats["lookups"]
        return {
            "entries": len(self),
            "path": str(self.path),
            **self.stats,
            "recognition_rate": (
                round(self.stats["recognised"] / looked, 3) if looked else 0.0
            ),
            "verification_rate": (
                round(self.stats["verified"] / self.stats["recognised"], 3)
                if self.stats["recognised"] else 0.0
            ),
        }
