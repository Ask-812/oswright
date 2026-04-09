"""End-to-end tests for all oswright subsystems."""
import sys
import time
import traceback


def test_screenshot():
    print("=== TEST 1: Screenshot ===")
    from oswright.capture import ScreenCapture
    cap = ScreenCapture()
    img = cap.screenshot()
    print(f"OK: Screenshot {img.size}")
    cap.close()
    return img


def test_windows_ocr(img):
    print("\n=== TEST 2: Windows OCR ===")
    from oswright._ocr_windows import is_available, recognize
    print(f"Available: {is_available()}")
    if is_available():
        results = recognize(img, "en")
        print(f"OK: Found {len(results)} words")
        for r in results[:3]:
            print(f"  {r['text']} at ({r['left']},{r['top']})")
    return is_available()


def test_ocr_engine(img):
    print("\n=== TEST 3: OCR Engine (auto backend) ===")
    from oswright.detect import OCREngine
    ocr = OCREngine()
    print(f"Backend: {ocr.backend_name}")
    elements = ocr.read_all(img)
    print(f"OK: Found {len(elements)} text elements")
    for el in elements[:3]:
        print(f'  "{el.text}" conf={el.confidence:.2f} at ({el.x},{el.y})')
    return ocr, elements


def test_find_text(ocr, img):
    print("\n=== TEST 4: Find specific text ===")
    for term in ["Start", "Search", "File", "Edit", "View", "Type", "Help"]:
        matches = ocr.find_text(img, term)
        if matches:
            m = matches[0]
            print(f'OK: Found "{term}" ({len(matches)} matches, best conf={m.confidence:.2f} at ({m.x},{m.y}))')
            return True
    print("WARN: Could not find common UI text on screen")
    return False


def test_window_management():
    print("\n=== TEST 5: Window management ===")
    from oswright.window import list_windows, get_window_region
    wins = list_windows()
    print(f"OK: {len(wins)} windows")
    for w in wins[:3]:
        print(f"  [{w.process_name}] {w.title[:50]}")
    if wins:
        region = get_window_region(title=wins[0].title[:20])
        print(f"Region for first window: {region}")


def test_clipboard():
    print("\n=== TEST 6: Clipboard ===")
    from oswright.clipboard import get_text, set_text
    test_str = "oswright_e2e_test_" + str(int(time.time()))
    ok = set_text(test_str)
    got = get_text()
    passed = got == test_str
    print(f"OK: set={ok}, match={passed}")
    if not passed:
        print(f"  Expected: {test_str!r}")
        print(f"  Got: {got!r}")


def test_accessibility():
    print("\n=== TEST 7: Accessibility ===")
    from oswright.accessibility import is_available, get_focused_window_tree
    print(f"UIA available: {is_available()}")
    if is_available():
        elements = get_focused_window_tree(max_depth=3)
        print(f"OK: {len(elements)} UI elements in focused window")
        for el in elements[:5]:
            print(f'  [{el.control_type}] "{el.name[:40]}"')


def test_cache_performance(ocr, img):
    print("\n=== TEST 8: Cache performance ===")
    # First call (may or may not be cached from earlier)
    ocr._cache.invalidate()

    t1 = time.perf_counter()
    r1 = ocr.read_all(img)
    t2 = time.perf_counter()
    r2 = ocr.read_all(img)  # should be cached
    t3 = time.perf_counter()

    first = (t2 - t1) * 1000
    cached = (t3 - t2) * 1000
    speedup = first / max(cached, 0.001)
    print(f"First scan: {first:.0f}ms, Cached: {cached:.1f}ms")
    print(f"Cache speedup: {speedup:.0f}x")
    print(f"Cache stats: {ocr._cache.stats}")
    print(f"Results consistent: {len(r1) == len(r2)}")


def test_mcp_tools():
    print("\n=== TEST 9: MCP server tools ===")
    from oswright.mcp_server import (
        screenshot, get_screen_info, find_text_on_screen,
        read_screen_text, mouse_click, type_text, press_key,
        click_text, get_clipboard, set_clipboard,
        list_windows, get_active_window, get_ocr_info,
        get_mouse_position, launch_app, wait_for_change,
    )
    import json

    # Test get_screen_info
    info = json.loads(get_screen_info())
    print(f"Screen: {info['screen_size']['width']}x{info['screen_size']['height']}, {info['monitor_count']} monitors")

    # Test get_mouse_position
    pos = json.loads(get_mouse_position())
    print(f"Mouse: ({pos['x']}, {pos['y']})")

    # Test screenshot (returns list with JSON + image)
    result = screenshot()
    meta = json.loads(result[0])
    print(f"Screenshot tool: {meta['width']}x{meta['height']}")

    # Test get_ocr_info
    ocr_info = json.loads(get_ocr_info())
    print(f"OCR: backend={ocr_info['active_backend']}, available={ocr_info['available_backends']}")

    # Test get_active_window
    win = json.loads(get_active_window())
    print(f"Active window: {win.get('title', 'unknown')[:50]}")

    # Test clipboard tools
    set_clipboard("mcp_tool_test")
    clip = json.loads(get_clipboard())
    print(f"Clipboard tool: {clip['clipboard_text'] == 'mcp_tool_test'}")

    # Test list_windows
    wins = json.loads(list_windows())
    print(f"List windows tool: {wins['window_count']} windows")

    # Test read_screen_text
    text = json.loads(read_screen_text())
    print(f"Read screen text: {text['element_count']} elements")

    print("All MCP tools OK")


def main():
    passed = 0
    failed = 0
    tests = [
        ("Screenshot", lambda: test_screenshot()),
    ]

    try:
        img = test_screenshot()
        passed += 1
    except Exception as e:
        print(f"FAIL: {e}")
        traceback.print_exc()
        failed += 1
        return

    ocr = None
    for name, fn, args in [
        ("Windows OCR", test_windows_ocr, (img,)),
        ("OCR Engine", test_ocr_engine, (img,)),
    ]:
        try:
            result = fn(*args)
            passed += 1
            if name == "OCR Engine":
                ocr, elements = result
        except Exception as e:
            print(f"FAIL [{name}]: {e}")
            traceback.print_exc()
            failed += 1

    if ocr is None:
        print("\nSKIPPING remaining tests (OCR engine failed)")
        print(f"\nRESULTS: {passed} passed, {failed} failed")
        return

    for name, fn, args in [
        ("Find Text", test_find_text, (ocr, img)),
        ("Window Mgmt", test_window_management, ()),
        ("Clipboard", test_clipboard, ()),
        ("Accessibility", test_accessibility, ()),
        ("Cache Perf", test_cache_performance, (ocr, img)),
        ("MCP Tools", test_mcp_tools, ()),
    ]:
        try:
            fn(*args)
            passed += 1
        except Exception as e:
            print(f"FAIL [{name}]: {e}")
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*50}")
    print(f"RESULTS: {passed} passed, {failed} failed")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
