"""
End-to-end tests that exercise the real desktop.

These are marked `e2e` and skip automatically when the machine has no display
or no OCR backend, so `pytest` is safe to run anywhere:

    pytest tests/                # everything available on this machine
    pytest tests/ -m "not e2e"   # unit tests only
"""

import json
import platform

import pytest

pytestmark = pytest.mark.e2e

IS_WINDOWS = platform.system() == "Windows"
windows_only = pytest.mark.skipif(not IS_WINDOWS, reason="Windows-only feature")


class TestCapture:
    def test_screenshot_has_size(self, screenshot):
        w, h = screenshot.size
        assert w > 0 and h > 0

    def test_screen_size_matches_screenshot(self, capture, screenshot):
        size = capture.get_screen_size(0)
        assert (size["width"], size["height"]) == screenshot.size

    def test_region_capture_is_cropped(self, capture):
        img = capture.screenshot(region={"left": 0, "top": 0, "width": 40, "height": 30})
        assert img.size == (40, 30)

    def test_offset_matches_monitor_origin(self, capture):
        size = capture.get_screen_size(0)
        assert capture.get_offset(monitor=0) == (size["left"], size["top"])

    def test_region_offset_is_region_origin(self, capture):
        region = {"left": 17, "top": 23, "width": 10, "height": 10}
        assert capture.get_offset(region=region) == (17, 23)


class TestOCREngine:
    def test_reads_some_text(self, ocr, screenshot):
        elements = ocr.read_all(screenshot)
        assert isinstance(elements, list)
        for el in elements[:20]:
            assert el.text is not None
            assert el.width >= 0 and el.height >= 0

    def test_coordinates_are_plain_ints(self, ocr, screenshot):
        for el in ocr.read_all(screenshot)[:20]:
            assert type(el.x) is int
            assert type(el.y) is int
            # Must survive JSON encoding to reach an MCP client.
            json.dumps({"x": el.x, "y": el.y, "conf": el.confidence})

    def test_find_text_returns_subset(self, ocr, screenshot):
        all_elements = ocr.read_all(screenshot)
        if not all_elements:
            pytest.skip("No text detected on screen")
        target = next((e.text for e in all_elements if e.text and len(e.text) > 3), None)
        if target is None:
            pytest.skip("No usable text detected on screen")

        matches = ocr.find_text(screenshot, target)
        assert matches
        assert all(target.lower() in m.text.lower() for m in matches)

    def test_cache_returns_equal_results(self, ocr, screenshot):
        ocr._cache.invalidate()
        first = ocr.read_all(screenshot)
        second = ocr.read_all(screenshot)
        assert len(first) == len(second)
        assert ocr._cache.stats["hits"] >= 1

    def test_cache_does_not_leak_mutations(self, ocr, screenshot):
        """A caller mutating returned results must not corrupt the cache."""
        ocr._cache.invalidate()
        first = ocr.read_all(screenshot)
        if not first:
            pytest.skip("No text detected on screen")
        count = len(first)
        first.append("junk")
        assert len(ocr.read_all(screenshot)) == count


@windows_only
class TestWindows:
    def test_lists_windows_with_valid_handles(self):
        from oswright.window import list_windows

        windows = list_windows()
        assert windows
        assert all(w.handle > 0 for w in windows)
        assert all(w.process_id for w in windows), "PID lookup failed"
        assert all(w.process_name for w in windows), "process name lookup failed"

    def test_at_most_one_foreground_window(self):
        from oswright.window import list_windows

        assert sum(1 for w in list_windows() if w.is_foreground) <= 1

    def test_window_region_within_virtual_screen(self):
        from oswright.window import _virtual_screen, get_window_region, list_windows

        vl, vt, vr, vb = _virtual_screen()
        for w in list_windows()[:5]:
            region = get_window_region(handle=w.handle)
            if region is None:
                continue
            assert region["width"] > 0 and region["height"] > 0
            assert region["left"] >= vl and region["top"] >= vt
            assert region["left"] + region["width"] <= vr
            assert region["top"] + region["height"] <= vb

    def test_dpi_coordinates_agree_with_capture(self, capture):
        """Window metrics and screenshot pixels must use the same scale."""
        from oswright.window import _virtual_screen

        vl, vt, vr, vb = _virtual_screen()
        size = capture.get_screen_size(0)
        assert (vr - vl, vb - vt) == (size["width"], size["height"])


@windows_only
class TestAccessibility:
    def test_tree_elements_are_addressable(self):
        from oswright.accessibility import get_focused_window_tree, is_available

        if not is_available():
            pytest.skip("uiautomation not installed")

        for el in get_focused_window_tree(max_depth=3):
            assert el.name or el.automation_id
            assert el.width > 0 and el.height > 0


class TestMCPTools:
    """The MCP tool surface, called directly as plain functions."""

    def test_get_screen_info(self, capture):
        from oswright.mcp_server import get_screen_info

        info = json.loads(get_screen_info())
        assert info["screen_size"]["width"] > 0
        assert info["monitor_count"] >= 1

    def test_screenshot_reports_origin(self, capture):
        from oswright.mcp_server import screenshot as screenshot_tool

        result = screenshot_tool()
        meta = json.loads(result[0])
        assert meta["width"] > 0
        assert "origin_x" in meta and "origin_y" in meta

    def test_screenshot_rejects_partial_region(self, capture):
        from oswright.mcp_server import screenshot as screenshot_tool

        meta = json.loads(screenshot_tool(region_left=0, region_top=0)[0])
        assert "error" in meta

    def test_screenshot_refuses_to_overwrite(self, capture, tmp_path):
        from oswright.mcp_server import screenshot as screenshot_tool

        existing = tmp_path / "taken.png"
        existing.write_bytes(b"do not clobber me")

        meta = json.loads(screenshot_tool(save_path=str(existing))[0])
        assert "error" in meta
        assert existing.read_bytes() == b"do not clobber me"

    def test_read_screen_text_is_json(self, capture, ocr):
        from oswright.mcp_server import read_screen_text

        data = json.loads(read_screen_text())
        assert data["element_count"] == len(data["elements"])

    def test_find_image_reports_missing_template(self, capture):
        from oswright.mcp_server import find_image_on_screen

        data = json.loads(find_image_on_screen(template_path="definitely_not_here.png"))
        assert "error" in data

    def test_mouse_click_rejects_half_coordinates(self):
        from oswright.mcp_server import mouse_click

        assert "error" in json.loads(mouse_click(x=100)[0])

    def test_get_ocr_info(self, ocr):
        from oswright.mcp_server import get_ocr_info

        info = json.loads(get_ocr_info())
        assert info["active_backend"] in info["available_backends"]

    def test_get_mouse_position(self):
        from oswright.mcp_server import get_mouse_position

        pos = json.loads(get_mouse_position())
        assert isinstance(pos["x"], int) and isinstance(pos["y"], int)

    @windows_only
    def test_list_windows_tool(self):
        from oswright.mcp_server import list_windows

        data = json.loads(list_windows())
        assert data["window_count"] == len(data["windows"])

    @windows_only
    def test_active_window_tool(self):
        from oswright.mcp_server import get_active_window

        assert "title" in json.loads(get_active_window())

    @windows_only
    def test_clipboard_roundtrip_tool(self):
        from oswright.mcp_server import get_clipboard, set_clipboard

        marker = "oswright_e2e_clipboard"
        assert json.loads(set_clipboard(marker))["success"]
        assert json.loads(get_clipboard())["clipboard_text"] == marker

    def test_launch_app_rejects_empty_command(self):
        from oswright.mcp_server import launch_app

        assert "error" in json.loads(launch_app(command="   ")[0])
