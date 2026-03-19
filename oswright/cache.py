"""
Screenshot comparison and caching utilities.

Provides efficient image hashing to avoid redundant OCR scans,
and screenshot diffing to detect when the screen actually changes.
"""

import hashlib
import logging
import time
from typing import Optional

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)


def image_hash(image: Image.Image, hash_size: int = 16) -> str:
    """
    Compute a perceptual hash of an image.
    Uses a combination of average hash and color mean for differentiation.
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

    # Combine structural hash with color info
    raw = hash_bytes + bytes([r_mean, g_mean, b_mean])
    return hashlib.md5(raw).hexdigest()


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

    arr1 = np.array(img1.convert("L"), dtype=np.int16)
    arr2 = np.array(img2.convert("L"), dtype=np.int16)

    # Count pixels that differ by more than 10 brightness levels
    diff = np.abs(arr1 - arr2)
    changed_pixels = np.sum(diff > 10)
    total_pixels = arr1.size

    change_ratio = changed_pixels / total_pixels
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

    arr1 = np.array(img1.convert("L"), dtype=np.int16)
    arr2 = np.array(img2.convert("L"), dtype=np.int16)

    diff = np.abs(arr1 - arr2) > 10
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
    """

    def __init__(self):
        self._last_hash: Optional[str] = None
        self._last_results: Optional[list] = None
        self._last_image: Optional[Image.Image] = None
        self._hit_count = 0
        self._miss_count = 0

    def get_cached(self, image: Image.Image) -> Optional[list]:
        """
        Check if we have cached OCR results for this image.
        Returns cached results if the image hasn't changed, None otherwise.
        """
        h = image_hash(image)
        if h == self._last_hash and self._last_results is not None:
            self._hit_count += 1
            logger.debug("OCR cache hit (%d hits, %d misses)", self._hit_count, self._miss_count)
            return self._last_results

        self._miss_count += 1
        return None

    def store(self, image: Image.Image, results: list):
        """Store OCR results for the given image."""
        self._last_hash = image_hash(image)
        self._last_results = results
        self._last_image = image

    @property
    def stats(self) -> dict:
        """Return cache hit/miss statistics."""
        total = self._hit_count + self._miss_count
        return {
            "hits": self._hit_count,
            "misses": self._miss_count,
            "hit_rate": round(self._hit_count / total, 2) if total > 0 else 0,
        }

    def invalidate(self):
        """Clear the cache."""
        self._last_hash = None
        self._last_results = None
        self._last_image = None
