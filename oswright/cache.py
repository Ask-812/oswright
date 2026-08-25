"""
Screenshot comparison and caching utilities.

Provides efficient image hashing to avoid redundant OCR scans,
and screenshot diffing to detect when the screen actually changes.
"""

import hashlib
import logging
import threading
from typing import Optional

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)


def exact_hash(image: Image.Image) -> str:
    """
    Compute an exact content digest of an image.

    This is what the OCR cache keys on. A perceptual hash is the wrong tool for
    a cache: its whole purpose is to collide on "similar" images, so a screen
    that genuinely changed (a different digit, a toggled checkbox) can hash to
    its previous value and be served stale OCR results.

    Size and mode are folded in so that images which differ only in dimensions
    can never collide.
    """
    return hashlib.blake2b(
        image.tobytes(),
        digest_size=16,
        key=f"{image.mode}:{image.size[0]}x{image.size[1]}".encode(),
    ).hexdigest()


def image_hash(image: Image.Image, hash_size: int = 16) -> str:
    """
    Compute a perceptual hash of an image.

    Deliberately lossy: similar-looking images hash alike. Use `exact_hash` for
    caching, and this only when approximate similarity is what you want.
    """
    small = image.resize((hash_size, hash_size), Image.LANCZOS).convert("RGB")
    pixels = np.array(small)

    # Include color channel means to distinguish solid colors
    r_mean = int(pixels[:, :, 0].mean())
    g_mean = int(pixels[:, :, 1].mean())
    b_mean = int(pixels[:, :, 2].mean())

    gray = np.array(small.convert("L"))
    avg = gray.mean()
    bits = (gray > avg).flatten()
    hash_bytes = np.packbits(bits).tobytes()

    # Combine structural hash with color info and the real dimensions, so that
    # two differently-sized images cannot produce the same digest.
    raw = hash_bytes + bytes([r_mean, g_mean, b_mean]) + f"{image.size}".encode()
    return hashlib.md5(raw).hexdigest()


def _channel_diff(img1: Image.Image, img2: Image.Image) -> np.ndarray:
    """
    Per-pixel maximum absolute difference across R, G and B.

    Comparing luminance alone would miss pure hue changes: a red and a green of
    equal brightness convert to the same grayscale value, so a status light
    flipping red to green would read as "no change".
    """
    arr1 = np.asarray(img1.convert("RGB"), dtype=np.int16)
    arr2 = np.asarray(img2.convert("RGB"), dtype=np.int16)
    return np.abs(arr1 - arr2).max(axis=2)


def images_differ(img1: Image.Image, img2: Image.Image, threshold: float = 0.02) -> bool:
    """
    Check if two screenshots are meaningfully different.

    Args:
        img1: First image.
        img2: Second image.
        threshold: Fraction of pixels that must differ (0.0-1.0).
                   Default 0.02 = 2% of pixels changed.

    Returns:
        True if images are significantly different.
    """
    if img1.size != img2.size:
        return True

    # Count pixels that differ by more than 10 levels in any channel
    diff = _channel_diff(img1, img2)
    change_ratio = float(np.count_nonzero(diff > 10)) / diff.size
    return change_ratio > threshold


def get_diff_region(img1: Image.Image, img2: Image.Image) -> Optional[dict]:
    """
    Get the bounding box of the region that changed between two screenshots.

    Returns:
        Dict with left, top, width, height of the changed region, or None if identical.
    """
    if img1.size != img2.size:
        return {"left": 0, "top": 0, "width": max(img1.size[0], img2.size[0]),
                "height": max(img1.size[1], img2.size[1])}

    diff = _channel_diff(img1, img2) > 10
    if not diff.any():
        return None

    rows = np.any(diff, axis=1)
    cols = np.any(diff, axis=0)
    top = int(np.argmax(rows))
    bottom = int(len(rows) - np.argmax(rows[::-1]))
    left = int(np.argmax(cols))
    right = int(len(cols) - np.argmax(cols[::-1]))

    return {
        "left": left,
        "top": top,
        "width": right - left,
        "height": bottom - top,
    }


class ScreenCache:
    """
    Caches OCR results and avoids redundant scans when screen hasn't changed.

    Safe to share across threads: the MCP server runs tools in a thread pool,
    so without locking one thread could store an image hash while another
    stores a different image's results, pairing a hash with the wrong results.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._last_hash: Optional[str] = None
        self._last_results: Optional[list] = None
        self._hit_count = 0
        self._miss_count = 0

    def get_cached(self, image: Image.Image) -> Optional[list]:
        """
        Check if we have cached OCR results for this image.
        Returns cached results if the image hasn't changed, None otherwise.
        """
        h = exact_hash(image)
        with self._lock:
            if h == self._last_hash and self._last_results is not None:
                self._hit_count += 1
                logger.debug(
                    "OCR cache hit (%d hits, %d misses)", self._hit_count, self._miss_count
                )
                # Hand back a copy of the list so a caller cannot append to, or
                # otherwise mutate, the cached results in place.
                return list(self._last_results)

            self._miss_count += 1
            return None

    def store(self, image: Image.Image, results: list):
        """Store OCR results for the given image."""
        h = exact_hash(image)
        with self._lock:
            self._last_hash = h
            self._last_results = list(results)

    @property
    def stats(self) -> dict:
        """Return cache hit/miss statistics."""
        with self._lock:
            hits, misses = self._hit_count, self._miss_count
        total = hits + misses
        return {
            "hits": hits,
            "misses": misses,
            "hit_rate": round(hits / total, 2) if total > 0 else 0,
        }

    def invalidate(self):
        """Clear the cache."""
        with self._lock:
            self._last_hash = None
            self._last_results = None
