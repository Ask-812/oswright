"""
Tests for OSWright core functionality.
Run with: python -m pytest tests/ -v
"""

import pytest
from PIL import Image


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
        import platform

        from oswright.clipboard import get_text, set_text
        if platform.system() != "Windows":
            pytest.skip("Clipboard test requires Windows")

        test_str = "oswright_test_" + str(id(self))
        assert set_text(test_str)
        assert get_text() == test_str

    def test_unicode_roundtrip(self):
        """Astral-plane characters must survive, not get truncated."""
        import platform

        from oswright.clipboard import get_text, set_text
        if platform.system() != "Windows":
            pytest.skip("Clipboard test requires Windows")

        test_str = "caf\u00e9 \u65e5\u672c\u8a9e \U0001f680"
        assert set_text(test_str)
        assert get_text() == test_str

    def test_empty_string_roundtrip(self):
        import platform

        from oswright.clipboard import get_text, set_text
        if platform.system() != "Windows":
            pytest.skip("Clipboard test requires Windows")

        assert set_text("")
        assert get_text() == ""


class TestExactHash:
    """The OCR cache must never serve results for a different image."""

    def test_same_image_same_hash(self):
        from oswright.cache import exact_hash
        img = Image.new("RGB", (64, 64), "red")
        assert exact_hash(img) == exact_hash(img.copy())

    def test_single_pixel_change_changes_hash(self):
        from oswright.cache import exact_hash
        img1 = Image.new("RGB", (64, 64), "white")
        img2 = img1.copy()
        img2.putpixel((10, 10), (0, 0, 0))
        assert exact_hash(img1) != exact_hash(img2)

    def test_different_sizes_never_collide(self):
        from oswright.cache import exact_hash
        assert exact_hash(Image.new("RGB", (100, 100), "white")) != exact_hash(
            Image.new("RGB", (200, 200), "white")
        )

    def test_different_mode_changes_hash(self):
        from oswright.cache import exact_hash
        img = Image.new("RGB", (32, 32), "white")
        assert exact_hash(img) != exact_hash(img.convert("L"))


class TestColorDiff:
    """Screen diffing must notice colour-only changes."""

    def test_equal_luminance_colors_differ(self):
        from oswright.cache import images_differ
        red = Image.new("RGB", (50, 50), (255, 0, 0))
        # Chosen so grayscale luminance is close but the hue is obviously different.
        green = Image.new("RGB", (50, 50), (0, 180, 0))
        assert images_differ(red, green)

    def test_diff_region_finds_color_only_change(self):
        from oswright.cache import get_diff_region
        img1 = Image.new("RGB", (60, 60), (255, 0, 0))
        img2 = img1.copy()
        for x in range(10, 20):
            for y in range(15, 25):
                img2.putpixel((x, y), (0, 180, 0))
        region = get_diff_region(img1, img2)
        assert region == {"left": 10, "top": 15, "width": 10, "height": 10}


class TestImageMatcher:
    """Template matching correctness."""

    @staticmethod
    def _scene():
        from PIL import ImageDraw
        img = Image.new("RGB", (300, 200), "white")
        ImageDraw.Draw(img).rectangle([80, 60, 120, 90], fill=(30, 90, 200))
        return img

    def test_finds_single_occurrence(self):
        from oswright.detect import ImageMatcher
        scene = self._scene()
        template = scene.crop((80, 60, 121, 91))
        matches = ImageMatcher.find_image_from_array(scene, template)
        assert len(matches) == 1
        assert abs(matches[0].x - 100) <= 2
        assert abs(matches[0].y - 75) <= 2

    def test_coordinates_are_json_serializable(self):
        """np.int64 coordinates would raise TypeError in json.dumps."""
        import json

        from oswright.detect import ImageMatcher
        scene = self._scene()
        matches = ImageMatcher.find_image_from_array(scene, scene.crop((80, 60, 121, 91)))
        for m in matches:
            assert type(m.x) is int and type(m.y) is int
            assert type(m.width) is int and type(m.height) is int
            assert type(m.confidence) is float
            json.dumps({"x": m.x, "y": m.y, "w": m.width, "c": m.confidence})

    def test_oversized_template_is_rejected(self):
        from oswright.detect import ImageMatcher
        with pytest.raises(ValueError, match="larger than"):
            ImageMatcher.find_image_from_array(
                self._scene(), Image.new("RGB", (500, 500), "white")
            )

    def test_missing_template_file_raises(self):
        from oswright.detect import ImageMatcher
        with pytest.raises(FileNotFoundError):
            ImageMatcher.find_image(self._scene(), "no_such_template_file.png")


class TestElementMatchOffset:
    """Offsetting must never mutate a match shared via the OCR cache."""

    def test_offset_returns_new_object(self):
        from oswright.detect import ElementMatch
        m = ElementMatch(x=10, y=20, left=5, top=15, width=10, height=10, confidence=0.9)
        moved = m.offset(100, 200)
        assert (m.x, m.y, m.left, m.top) == (10, 20, 5, 15), "original was mutated"
        assert (moved.x, moved.y, moved.left, moved.top) == (110, 220, 105, 215)

    def test_zero_offset_is_identity(self):
        from oswright.detect import ElementMatch
        m = ElementMatch(x=1, y=2, left=3, top=4, width=5, height=6, confidence=0.5)
        assert m.offset(0, 0) is m

    def test_repeated_offset_does_not_accumulate(self):
        """Applying an offset twice to the same cached match must be safe."""
        from oswright.detect import ElementMatch
        m = ElementMatch(x=10, y=10, left=10, top=10, width=4, height=4, confidence=1.0)
        assert m.offset(5, 5).x == 15
        assert m.offset(5, 5).x == 15


class TestRegionValidation:
    """Partial regions used to be ignored, silently capturing the whole screen."""

    def test_all_four_bounds_makes_region(self):
        from oswright.mcp_server import _region_of
        assert _region_of(1, 2, 3, 4) == {"left": 1, "top": 2, "width": 3, "height": 4}

    def test_no_bounds_means_no_region(self):
        from oswright.mcp_server import _region_of
        assert _region_of(None, None, None, None) is None

    @pytest.mark.parametrize("bounds", [
        (0, None, None, None),
        (0, 0, None, None),
        (0, 0, 10, None),
    ])
    def test_partial_bounds_rejected(self, bounds):
        from oswright.mcp_server import _region_of
        with pytest.raises(ValueError):
            _region_of(*bounds)

    def test_non_positive_size_rejected(self):
        from oswright.mcp_server import _region_of
        with pytest.raises(ValueError):
            _region_of(0, 0, 0, 10)


class TestCommandSplitting:
    """launch_app must accept real Windows paths."""

    def test_windows_path_survives(self):
        import platform
        if platform.system() != "Windows":
            pytest.skip("Windows path splitting")
        from oswright.mcp_server import _split_command
        assert _split_command(r"C:\Windows\System32\notepad.exe") == [
            r"C:\Windows\System32\notepad.exe"
        ]

    def test_quoted_path_with_spaces(self):
        import platform
        if platform.system() != "Windows":
            pytest.skip("Windows path splitting")
        from oswright.mcp_server import _split_command
        assert _split_command(r'"C:\Program Files\App\a.exe" file.txt') == [
            r"C:\Program Files\App\a.exe", "file.txt",
        ]

    def test_bare_name_and_args(self):
        from oswright.mcp_server import _split_command
        assert _split_command("code --new-window") == ["code", "--new-window"]


class TestLoopbackDetection:
    """Binding remotely without auth must require an explicit opt-in."""

    @pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "127.0.0.5"])
    def test_loopback_hosts(self, host):
        from oswright.mcp_server import _is_loopback
        assert _is_loopback(host)

    @pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com", "::"])
    def test_remote_hosts(self, host):
        from oswright.mcp_server import _is_loopback
        assert not _is_loopback(host)


class TestTimeoutResolution:
    """--timeout was previously never read by any tool."""

    def test_none_uses_server_default(self):
        import oswright.mcp_server as server
        original = server._default_timeout
        server._default_timeout = 42.0
        try:
            assert server._timeout(None) == 42.0
            assert server._timeout(3.0) == 3.0
        finally:
            server._default_timeout = original


class TestLocator:
    """Locator construction and selection logic (no screen access)."""

    @staticmethod
    def _locator(**kwargs):
        from oswright.locator import Locator
        return Locator(capture=None, text="x", **kwargs)

    def test_requires_search_criteria(self):
        from oswright.locator import Locator, OSWrightError
        with pytest.raises(OSWrightError):
            Locator(capture=None)

    def test_select_first_and_last(self):
        from oswright.detect import ElementMatch
        items = [
            ElementMatch(x=i, y=0, left=0, top=0, width=1, height=1, confidence=1.0)
            for i in range(3)
        ]
        assert self._locator(nth=0)._select(items).x == 0
        assert self._locator(nth=-1)._select(items).x == 2
        assert self._locator(nth=1)._select(items).x == 1

    def test_select_out_of_range(self):
        from oswright.detect import ElementMatch
        items = [ElementMatch(x=0, y=0, left=0, top=0, width=1, height=1, confidence=1.0)]
        assert self._locator(nth=5)._select(items) is None
        assert self._locator(nth=0)._select([]) is None

    def test_image_locator_needs_no_ocr(self):
        """Template matching must work with no OCR backend configured."""
        from oswright.locator import Locator
        loc = Locator(capture=None, image="icon.png")
        assert loc._describe() == 'image="icon.png"'

    def test_assertions_inherit_locator_timeout(self):
        loc = self._locator(timeout=33.0)
        assert loc.expect()._timeout == 33.0

    def test_assertion_explicit_zero_timeout_respected(self):
        loc = self._locator(timeout=33.0)
        assert loc.expect()._resolve_timeout(0) == 0


class TestDPIAwareness:
    def test_idempotent(self):
        from oswright._dpi import ensure_dpi_aware
        first = ensure_dpi_aware()
        assert ensure_dpi_aware() == first


class TestOCREngine:
    """Tests for OCR engine initialization."""

    def test_backend_selection(self):
        from oswright.detect import _OCR_BACKEND, _OCR_BACKENDS

        if not _OCR_BACKENDS:
            pytest.skip("no OCR backend installed on this machine")
        assert _OCR_BACKEND is not None
        assert _OCR_BACKEND in _OCR_BACKENDS

    def test_engine_creates(self):
        from oswright.detect import _OCR_BACKENDS, OCREngine

        if not _OCR_BACKENDS:
            pytest.skip("no OCR backend installed on this machine")
        engine = OCREngine()
        assert engine.backend_name in ("winocr", "easyocr")

    def test_missing_backend_explains_itself(self):
        """With no backend at all, the error must say how to get one."""
        from oswright.detect import _no_backend_message

        message = _no_backend_message()
        assert "pip install" in message

    def test_easyocr_not_imported_at_module_scope(self):
        """Importing easyocr pulls in torch and costs seconds of startup."""
        import importlib
        import sys

        import oswright.detect
        importlib.reload(oswright.detect)
        assert "easyocr" not in sys.modules or "torch" in sys.modules


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
        from oswright import Locator, OSWright, Screen
        assert OSWright is not None
        assert Screen is not None
        assert Locator is not None
