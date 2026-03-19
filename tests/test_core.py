"""
Tests for OSWright core functionality.
Run with: python -m pytest tests/ -v
"""

import pytest
from PIL import Image
import numpy as np


class TestElementMatch:
    """Tests for the ElementMatch dataclass."""

    def test_center_property(self):
        from oswright.detect import ElementMatch
        m = ElementMatch(x=100, y=200, left=50, top=150, width=100, height=100, confidence=0.9)
        assert m.center == (100, 200)

    def test_box_property(self):
        from oswright.detect import ElementMatch
        m = ElementMatch(x=100, y=200, left=50, top=150, width=100, height=100, confidence=0.9)
        assert m.box == (50, 150, 150, 250)


class TestImageDiff:
    """Tests for screenshot comparison utilities."""

    def test_identical_images_not_different(self):
        from oswright.cache import images_differ
        img = Image.new("RGB", (100, 100), "white")
        assert not images_differ(img, img)

    def test_different_images_detected(self):
        from oswright.cache import images_differ
        img1 = Image.new("RGB", (100, 100), "white")
        img2 = Image.new("RGB", (100, 100), "black")
        assert images_differ(img1, img2)

    def test_small_change_below_threshold(self):
        from oswright.cache import images_differ
        img1 = Image.new("RGB", (100, 100), "white")
        # Change just 1 pixel — should be below 2% threshold
        img2 = img1.copy()
        img2.putpixel((50, 50), (0, 0, 0))
        assert not images_differ(img1, img2, threshold=0.02)

    def test_different_sizes_are_different(self):
        from oswright.cache import images_differ
        img1 = Image.new("RGB", (100, 100), "white")
        img2 = Image.new("RGB", (200, 200), "white")
        assert images_differ(img1, img2)

    def test_diff_region_identical(self):
        from oswright.cache import get_diff_region
        img = Image.new("RGB", (100, 100), "white")
        assert get_diff_region(img, img) is None

    def test_diff_region_changed(self):
        from oswright.cache import get_diff_region
        img1 = Image.new("RGB", (100, 100), "white")
        img2 = img1.copy()
        # Draw a black rectangle at (20,30) -> (40,50)
        for x in range(20, 40):
            for y in range(30, 50):
                img2.putpixel((x, y), (0, 0, 0))
        region = get_diff_region(img1, img2)
        assert region is not None
        assert region["left"] == 20
        assert region["top"] == 30


class TestImageHash:
    """Tests for perceptual image hashing."""

    def test_same_image_same_hash(self):
        from oswright.cache import image_hash
        img = Image.new("RGB", (200, 200), "red")
        assert image_hash(img) == image_hash(img)

    def test_different_image_different_hash(self):
        from oswright.cache import image_hash
        img1 = Image.new("RGB", (200, 200), "red")
        img2 = Image.new("RGB", (200, 200), "blue")
        assert image_hash(img1) != image_hash(img2)


class TestScreenCache:
    """Tests for the OCR result cache."""

    def test_cache_miss_on_new_image(self):
        from oswright.cache import ScreenCache
        cache = ScreenCache()
        img = Image.new("RGB", (100, 100), "white")
        assert cache.get_cached(img) is None

    def test_cache_hit_on_same_image(self):
        from oswright.cache import ScreenCache
        cache = ScreenCache()
        img = Image.new("RGB", (100, 100), "white")
        results = [{"text": "hello", "x": 10, "y": 20}]
        cache.store(img, results)
        assert cache.get_cached(img) == results

    def test_cache_miss_on_different_image(self):
        from oswright.cache import ScreenCache
        cache = ScreenCache()
        img1 = Image.new("RGB", (100, 100), "white")
        img2 = Image.new("RGB", (100, 100), "black")
        cache.store(img1, [{"text": "hello"}])
        assert cache.get_cached(img2) is None

    def test_cache_stats(self):
        from oswright.cache import ScreenCache
        cache = ScreenCache()
        img = Image.new("RGB", (100, 100), "white")
        cache.store(img, [])
        cache.get_cached(img)  # hit
        cache.get_cached(img)  # hit
        stats = cache.stats
        assert stats["hits"] == 2
        assert stats["misses"] == 0

    def test_invalidate(self):
        from oswright.cache import ScreenCache
        cache = ScreenCache()
        img = Image.new("RGB", (100, 100), "white")
        cache.store(img, [{"text": "hello"}])
        cache.invalidate()
        assert cache.get_cached(img) is None


class TestClipboard:
    """Tests for clipboard operations (platform-specific)."""

    def test_set_and_get(self):
        from oswright.clipboard import get_text, set_text
        import platform
        if platform.system() != "Windows":
            pytest.skip("Clipboard test requires Windows")

        test_str = "oswright_test_" + str(id(self))
        assert set_text(test_str)
        assert get_text() == test_str


class TestOCREngine:
    """Tests for OCR engine initialization."""

    def test_backend_selection(self):
        from oswright.detect import _OCR_BACKENDS, _OCR_BACKEND
        assert len(_OCR_BACKENDS) > 0
        assert _OCR_BACKEND is not None
        assert _OCR_BACKEND in _OCR_BACKENDS

    def test_engine_creates(self):
        from oswright.detect import OCREngine
        engine = OCREngine()
        assert engine.backend_name in ("winocr", "easyocr")


class TestWindowManagement:
    """Tests for window management (platform-specific)."""

    def test_list_windows(self):
        import platform
        if platform.system() != "Windows":
            pytest.skip("Window test requires Windows")

        from oswright.window import list_windows
        windows = list_windows()
        assert len(windows) > 0
        assert windows[0].title  # At least one window has a title

    def test_list_windows_filter(self):
        import platform
        if platform.system() != "Windows":
            pytest.skip("Window test requires Windows")

        from oswright.window import list_windows
        # Filter for something that definitely won't match
        windows = list_windows(title_filter="xyznonexistent123")
        assert len(windows) == 0


class TestVersion:
    """Tests for package metadata."""

    def test_version_format(self):
        from oswright import __version__
        parts = __version__.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_exports(self):
        from oswright import OSWright, Screen, Locator
        from oswright import OSWrightError, TimeoutError, ElementNotFoundError
        assert OSWright is not None
        assert Screen is not None
        assert Locator is not None
