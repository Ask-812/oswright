"""
OSWright MCP Server - Exposes OS automation as MCP tools.

This turns OSWright into an MCP server so AI agents can control
the desktop through the Model Context Protocol.

Inspired by Playwright MCP's patterns:
- Tool annotations (readOnly, destructive, idempotent hints)
- Native image content type (not base64-in-JSON)
- Auto-snapshot after every action (agent always has current state)
- Compound tools for common multi-step workflows

Run:
    # Standard (stdio transport, used by most MCP clients)
    uvx oswright

    # Or with Python directly
    python -m oswright

    # With options
    python -m oswright --ocr-languages en es --timeout 15

    # SSE transport for remote/multi-client access
    python -m oswright --port 8931
"""

import argparse
import io
import json
import logging
import os
import time
from typing import Optional

from mcp.server.fastmcp import FastMCP, Image as MCPImage
from mcp.types import ToolAnnotations

from oswright.capture import ScreenCapture
from oswright.detect import OCREngine, ImageMatcher
from oswright.input import Mouse, Keyboard

logger = logging.getLogger(__name__)

# --- Initialize MCP server and shared state ---

mcp = FastMCP(
    "OSWright",
    instructions=(
        "OSWright is an OS-level automation server. It lets you control the "
        "desktop like Playwright controls a browser — take screenshots, find "
        "text/images on screen via OCR, click, type, press keys, scroll, and drag.\n\n"
        "Workflow: Start by taking a screenshot to see the screen. Use find_text_on_screen "
        "or read_screen_text to locate elements via OCR. Then use coordinate-based tools "
        "(mouse_click, type_text) or compound tools (click_text, fill_field) to interact.\n\n"
        "Action tools automatically return a screenshot so you always have current screen state. "
        "Use wait_for_text to poll for expected UI changes before proceeding."
    ),
)

_capture = ScreenCapture()
_ocr: Optional[OCREngine] = None
_ocr_languages: list[str] = ["en"]
_default_timeout: float = 10.0

# --- Tool annotation presets (mirrors Playwright MCP pattern) ---
_READONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
_INPUT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
_ACTION = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)


def _get_ocr() -> OCREngine:
    """Lazy-init OCR engine (heavy first load)."""
    global _ocr
    if _ocr is None:
        logger.info("Initializing OCR engine (first use, languages=%s)...", _ocr_languages)
        _ocr = OCREngine(languages=_ocr_languages)
    return _ocr


def _take_snapshot_image() -> MCPImage:
    """Capture a screenshot and return as MCP Image content."""
    img = _capture.screenshot()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return MCPImage(data=buf.getvalue(), format="png")


# =========================================================================
# SCREEN TOOLS
# =========================================================================


@mcp.tool(annotations=_READONLY)
def screenshot(
    save_path: Optional[str] = None,
    region_left: Optional[int] = None,
    region_top: Optional[int] = None,
    region_width: Optional[int] = None,
    region_height: Optional[int] = None,
    monitor: int = 0,
) -> list:
    """
    Take a screenshot of the screen or a region.
    Returns the image as native MCP image content.
    Optionally saves to a file path.

    Args:
        save_path: Optional file path to save the screenshot.
        region_left: Left coordinate for a sub-region capture.
        region_top: Top coordinate for a sub-region capture.
        region_width: Width of the sub-region.
        region_height: Height of the sub-region.
        monitor: Monitor index (0 = all monitors, 1 = primary, etc.).
    """
    region = None
    if all(v is not None for v in [region_left, region_top, region_width, region_height]):
        region = {
            "left": region_left,
            "top": region_top,
            "width": region_width,
            "height": region_height,
        }

    img = _capture.screenshot(path=save_path, region=region, monitor=monitor)

    buf = io.BytesIO()
    img.save(buf, format="PNG")

    result = [
        json.dumps({
            "width": img.size[0],
            "height": img.size[1],
            **({"saved_to": save_path} if save_path else {}),
        }),
        MCPImage(data=buf.getvalue(), format="png"),
    ]
    return result


@mcp.tool(annotations=_READONLY)
def get_screen_info(monitor: int = 0) -> str:
    """
    Get screen dimensions and monitor count.

    Args:
        monitor: Monitor index (0 = all monitors combined).
    """
    size = _capture.get_screen_size(monitor)
    count = _capture.get_monitor_count()
    return json.dumps({"screen_size": size, "monitor_count": count})


# =========================================================================
# OCR / TEXT FINDING TOOLS
# =========================================================================


@mcp.tool(annotations=_READONLY)
def find_text_on_screen(
    text: str,
    exact: bool = False,
    region_left: Optional[int] = None,
    region_top: Optional[int] = None,
    region_width: Optional[int] = None,
    region_height: Optional[int] = None,
    monitor: int = 0,
) -> str:
    """
    Find all occurrences of text on screen using OCR.
    Returns a list of matches with coordinates and confidence.

    Args:
        text: The text to search for on screen.
        exact: If True, requires exact match. If False, substring match.
        region_left: Optional left bound to restrict search area.
        region_top: Optional top bound to restrict search area.
        region_width: Optional width to restrict search area.
        region_height: Optional height to restrict search area.
        monitor: Monitor index.
    """
    region = None
    if all(v is not None for v in [region_left, region_top, region_width, region_height]):
        region = {
            "left": region_left,
            "top": region_top,
            "width": region_width,
            "height": region_height,
        }

    img = _capture.screenshot(region=region, monitor=monitor)
    ocr = _get_ocr()
    matches = ocr.find_text(img, text, exact=exact)

    # Adjust coords if we used a region
    results = []
    for m in matches:
        x_off = region["left"] if region else 0
        y_off = region["top"] if region else 0
        results.append({
            "text": m.text,
            "x": m.x + x_off,
            "y": m.y + y_off,
            "left": m.left + x_off,
            "top": m.top + y_off,
            "width": m.width,
            "height": m.height,
            "confidence": round(m.confidence, 3),
        })

    return json.dumps({"query": text, "match_count": len(results), "matches": results})


@mcp.tool(annotations=_READONLY)
def read_screen_text(
    region_left: Optional[int] = None,
    region_top: Optional[int] = None,
    region_width: Optional[int] = None,
    region_height: Optional[int] = None,
    monitor: int = 0,
) -> str:
    """
    Read ALL visible text on the screen using OCR.
    Returns every detected text element with its position.

    Args:
        region_left: Optional left bound to restrict OCR area.
        region_top: Optional top bound to restrict OCR area.
        region_width: Optional width to restrict OCR area.
        region_height: Optional height to restrict OCR area.
        monitor: Monitor index.
    """
    region = None
    if all(v is not None for v in [region_left, region_top, region_width, region_height]):
        region = {
            "left": region_left,
            "top": region_top,
            "width": region_width,
            "height": region_height,
        }

    img = _capture.screenshot(region=region, monitor=monitor)
    ocr = _get_ocr()
    elements = ocr.read_all(img)

    x_off = region["left"] if region else 0
    y_off = region["top"] if region else 0

    results = []
    for el in elements:
        results.append({
            "text": el.text,
            "x": el.x + x_off,
            "y": el.y + y_off,
            "left": el.left + x_off,
            "top": el.top + y_off,
            "width": el.width,
            "height": el.height,
            "confidence": round(el.confidence, 3),
        })

    return json.dumps({"element_count": len(results), "elements": results})


# =========================================================================
# IMAGE MATCHING TOOLS
# =========================================================================


@mcp.tool(annotations=_READONLY)
def find_image_on_screen(
    template_path: str,
    threshold: float = 0.8,
    monitor: int = 0,
) -> str:
    """
    Find all occurrences of a template image on screen.

    Args:
        template_path: Absolute path to the template image file to find.
        threshold: Minimum match confidence (0.0-1.0, default 0.8).
        monitor: Monitor index.
    """
    img = _capture.screenshot(monitor=monitor)
    matches = ImageMatcher.find_image(img, template_path, threshold=threshold)

    results = []
    for m in matches:
        results.append({
            "x": m.x, "y": m.y,
            "left": m.left, "top": m.top,
            "width": m.width, "height": m.height,
            "confidence": round(m.confidence, 3),
        })

    return json.dumps({
        "template": template_path,
        "match_count": len(results),
        "matches": results,
    })


# =========================================================================
# MOUSE TOOLS
# =========================================================================


@mcp.tool(annotations=_INPUT)
def mouse_click(
    x: Optional[int] = None,
    y: Optional[int] = None,
    button: str = "left",
    clicks: int = 1,
) -> list:
    """
    Click the mouse at coordinates or current position.
    Returns action result and a screenshot of current screen state.

    Args:
        x: X screen coordinate. If not provided, clicks at current position.
        y: Y screen coordinate. If not provided, clicks at current position.
        button: Mouse button - 'left', 'right', or 'middle'.
        clicks: Number of clicks (2 for double-click).
    """
    Mouse.click(x, y, button=button, clicks=clicks)
    time.sleep(0.3)
    pos = Mouse.get_position()
    return [
        json.dumps({"action": "click", "button": button, "clicks": clicks, "position": pos}),
        _take_snapshot_image(),
    ]


@mcp.tool(annotations=_INPUT)
def mouse_double_click(x: Optional[int] = None, y: Optional[int] = None) -> list:
    """
    Double-click at coordinates or current position.
    Returns action result and a screenshot of current screen state.

    Args:
        x: X screen coordinate.
        y: Y screen coordinate.
    """
    Mouse.double_click(x, y)
    time.sleep(0.3)
    pos = Mouse.get_position()
    return [
        json.dumps({"action": "double_click", "position": pos}),
        _take_snapshot_image(),
    ]


@mcp.tool(annotations=_INPUT)
def mouse_move(x: int, y: int) -> str:
    """
    Move the mouse cursor to screen coordinates.

    Args:
        x: X screen coordinate.
        y: Y screen coordinate.
    """
    Mouse.move(x, y)
    return json.dumps({"action": "move", "position": [x, y]})


@mcp.tool(annotations=_INPUT)
def mouse_scroll(amount: int, x: Optional[int] = None, y: Optional[int] = None) -> list:
    """
    Scroll the mouse wheel.
    Returns action result and a screenshot of current screen state.

    Args:
        amount: Scroll amount. Positive = up, negative = down.
        x: Optional X coordinate to scroll at.
        y: Optional Y coordinate to scroll at.
    """
    Mouse.scroll(amount, x, y)
    time.sleep(0.3)
    return [
        json.dumps({"action": "scroll", "amount": amount}),
        _take_snapshot_image(),
    ]


@mcp.tool(annotations=_INPUT)
def mouse_drag(
    start_x: int, start_y: int, end_x: int, end_y: int,
    button: str = "left", duration: float = 0.3,
) -> list:
    """
    Drag from one point to another.
    Returns action result and a screenshot of current screen state.

    Args:
        start_x: Starting X coordinate.
        start_y: Starting Y coordinate.
        end_x: Ending X coordinate.
        end_y: Ending Y coordinate.
        button: Mouse button to hold ('left' or 'right').
        duration: Duration of the drag in seconds.
    """
    Mouse.drag(start_x, start_y, end_x, end_y, button=button, duration=duration)
    time.sleep(0.2)
    return [
        json.dumps({
            "action": "drag",
            "from": [start_x, start_y],
            "to": [end_x, end_y],
        }),
        _take_snapshot_image(),
    ]


@mcp.tool(annotations=_READONLY)
def get_mouse_position() -> str:
    """Get the current mouse cursor position."""
    pos = Mouse.get_position()
    return json.dumps({"x": pos[0], "y": pos[1]})


# =========================================================================
# KEYBOARD TOOLS
# =========================================================================


@mcp.tool(annotations=_INPUT)
def type_text(text: str, delay: float = 0.02) -> list:
    """
    Type text using the keyboard (character by character).
    Returns action result and a screenshot of current screen state.

    Args:
        text: The text to type.
        delay: Delay in seconds between each character.
    """
    Keyboard.type_text(text, delay=delay)
    time.sleep(0.2)
    return [
        json.dumps({"action": "type_text", "length": len(text)}),
        _take_snapshot_image(),
    ]


@mcp.tool(annotations=_INPUT)
def press_key(key: str) -> list:
    """
    Press a key or key combination.
    Returns action result and a screenshot of current screen state.

    Supports single keys and combos like 'Enter', 'Ctrl+C', 'Alt+Tab', 'Ctrl+Shift+S'.

    Args:
        key: Key name or combo (e.g., 'Enter', 'Ctrl+A', 'Alt+F4').
    """
    Keyboard.press(key)
    time.sleep(0.3)
    return [
        json.dumps({"action": "press_key", "key": key}),
        _take_snapshot_image(),
    ]


# =========================================================================
# COMPOUND TOOLS (high-level actions)
# =========================================================================


@mcp.tool(annotations=_INPUT)
def click_text(
    text: str,
    exact: bool = False,
    button: str = "left",
    timeout: float = 10.0,
    poll_interval: float = 0.5,
    monitor: int = 0,
) -> list:
    """
    Find text on screen using OCR and click on it. Auto-retries until found or timeout.
    Returns action result and a screenshot of current screen state.

    Args:
        text: The visible text to find and click.
        exact: Require exact text match (not just substring).
        button: Mouse button ('left', 'right', 'middle').
        timeout: Maximum seconds to wait for the text to appear.
        poll_interval: Seconds between each retry.
        monitor: Monitor index.
    """
    deadline = time.time() + timeout
    ocr = _get_ocr()

    while True:
        img = _capture.screenshot(monitor=monitor)
        matches = ocr.find_text(img, text, exact=exact)

        if matches:
            best = matches[0]
            Mouse.click(best.x, best.y, button=button)
            time.sleep(0.3)
            return [
                json.dumps({
                    "action": "click_text",
                    "text_found": best.text,
                    "clicked_at": [best.x, best.y],
                    "confidence": round(best.confidence, 3),
                }),
                _take_snapshot_image(),
            ]

        if time.time() >= deadline:
            return [json.dumps({
                "action": "click_text",
                "error": f"Text '{text}' not found on screen within {timeout}s",
            })]

        time.sleep(poll_interval)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
def wait_for_text(
    text: str,
    exact: bool = False,
    timeout: float = 10.0,
    poll_interval: float = 0.5,
    monitor: int = 0,
) -> str:
    """
    Wait for specific text to appear on screen. Polls via OCR until found or timeout.

    Args:
        text: The text to wait for.
        exact: Require exact match.
        timeout: Maximum seconds to wait.
        poll_interval: Seconds between polls.
        monitor: Monitor index.
    """
    deadline = time.time() + timeout
    ocr = _get_ocr()

    while True:
        img = _capture.screenshot(monitor=monitor)
        matches = ocr.find_text(img, text, exact=exact)

        if matches:
            best = matches[0]
            return json.dumps({
                "found": True,
                "text": best.text,
                "x": best.x, "y": best.y,
                "confidence": round(best.confidence, 3),
            })

        if time.time() >= deadline:
            return json.dumps({
                "found": False,
                "error": f"Text '{text}' not found within {timeout}s",
            })

        time.sleep(poll_interval)


@mcp.tool(annotations=_INPUT)
def fill_field(
    target_text: str,
    value: str,
    exact: bool = False,
    timeout: float = 10.0,
    monitor: int = 0,
) -> list:
    """
    Find a text label on screen, click it, clear the field, and type a value.
    Useful for filling form fields by their label text.
    Returns action result and a screenshot of current screen state.

    Args:
        target_text: The label text to find and click.
        value: The text to type into the field.
        exact: Require exact text match for the label.
        timeout: Maximum seconds to wait for the label.
        monitor: Monitor index.
    """
    # First find and click the target
    result = json.loads(click_text(target_text, exact=exact, timeout=timeout, monitor=monitor)[0])
    if "error" in result:
        return [json.dumps({"action": "fill_field", "error": result["error"]})]

    time.sleep(0.1)
    Keyboard.press("Ctrl+A")
    time.sleep(0.05)
    Keyboard.press("Delete")
    time.sleep(0.05)
    Keyboard.type_text(value)
    time.sleep(0.2)

    return [
        json.dumps({
            "action": "fill_field",
            "target": target_text,
            "value": value,
            "clicked_at": result["clicked_at"],
        }),
        _take_snapshot_image(),
    ]


# =========================================================================
# NEW COMPOUND TOOLS (inspired by Playwright MCP patterns)
# =========================================================================


@mcp.tool(annotations=_INPUT)
def double_click_text(
    text: str,
    exact: bool = False,
    timeout: float = 10.0,
    poll_interval: float = 0.5,
    monitor: int = 0,
) -> list:
    """
    Find text on screen using OCR and double-click on it.
    Returns action result and a screenshot of current screen state.

    Args:
        text: The visible text to find and double-click.
        exact: Require exact text match.
        timeout: Maximum seconds to wait for the text to appear.
        poll_interval: Seconds between each retry.
        monitor: Monitor index.
    """
    deadline = time.time() + timeout
    ocr = _get_ocr()

    while True:
        img = _capture.screenshot(monitor=monitor)
        matches = ocr.find_text(img, text, exact=exact)

        if matches:
            best = matches[0]
            Mouse.double_click(best.x, best.y)
            time.sleep(0.3)
            return [
                json.dumps({
                    "action": "double_click_text",
                    "text_found": best.text,
                    "clicked_at": [best.x, best.y],
                    "confidence": round(best.confidence, 3),
                }),
                _take_snapshot_image(),
            ]

        if time.time() >= deadline:
            return [json.dumps({
                "action": "double_click_text",
                "error": f"Text '{text}' not found on screen within {timeout}s",
            })]

        time.sleep(poll_interval)


@mcp.tool(annotations=_INPUT)
def right_click_text(
    text: str,
    exact: bool = False,
    timeout: float = 10.0,
    poll_interval: float = 0.5,
    monitor: int = 0,
) -> list:
    """
    Find text on screen using OCR and right-click on it (context menu).
    Returns action result and a screenshot of current screen state.

    Args:
        text: The visible text to find and right-click.
        exact: Require exact text match.
        timeout: Maximum seconds to wait for the text to appear.
        poll_interval: Seconds between each retry.
        monitor: Monitor index.
    """
    deadline = time.time() + timeout
    ocr = _get_ocr()

    while True:
        img = _capture.screenshot(monitor=monitor)
        matches = ocr.find_text(img, text, exact=exact)

        if matches:
            best = matches[0]
            Mouse.click(best.x, best.y, button="right")
            time.sleep(0.3)
            return [
                json.dumps({
                    "action": "right_click_text",
                    "text_found": best.text,
                    "clicked_at": [best.x, best.y],
                    "confidence": round(best.confidence, 3),
                }),
                _take_snapshot_image(),
            ]

        if time.time() >= deadline:
            return [json.dumps({
                "action": "right_click_text",
                "error": f"Text '{text}' not found on screen within {timeout}s",
            })]

        time.sleep(poll_interval)


@mcp.tool(annotations=_INPUT)
def hover_text(
    text: str,
    exact: bool = False,
    timeout: float = 10.0,
    poll_interval: float = 0.5,
    monitor: int = 0,
) -> list:
    """
    Find text on screen using OCR and move the mouse over it (hover).
    Useful for triggering tooltips, hover menus, or inspecting elements.
    Returns action result and a screenshot of current screen state.

    Args:
        text: The visible text to find and hover over.
        exact: Require exact text match.
        timeout: Maximum seconds to wait for the text to appear.
        poll_interval: Seconds between each retry.
        monitor: Monitor index.
    """
    deadline = time.time() + timeout
    ocr = _get_ocr()

    while True:
        img = _capture.screenshot(monitor=monitor)
        matches = ocr.find_text(img, text, exact=exact)

        if matches:
            best = matches[0]
            Mouse.move(best.x, best.y)
            time.sleep(0.3)
            return [
                json.dumps({
                    "action": "hover_text",
                    "text_found": best.text,
                    "hovered_at": [best.x, best.y],
                    "confidence": round(best.confidence, 3),
                }),
                _take_snapshot_image(),
            ]

        if time.time() >= deadline:
            return [json.dumps({
                "action": "hover_text",
                "error": f"Text '{text}' not found on screen within {timeout}s",
            })]

        time.sleep(poll_interval)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
def wait_for_text_gone(
    text: str,
    exact: bool = False,
    timeout: float = 10.0,
    poll_interval: float = 0.5,
    monitor: int = 0,
) -> str:
    """
    Wait for text to disappear from screen. Polls via OCR until gone or timeout.
    Useful for waiting for loading spinners, progress dialogs, etc. to finish.

    Args:
        text: The text to wait to disappear.
        exact: Require exact match.
        timeout: Maximum seconds to wait.
        poll_interval: Seconds between polls.
        monitor: Monitor index.
    """
    deadline = time.time() + timeout
    ocr = _get_ocr()

    while True:
        img = _capture.screenshot(monitor=monitor)
        matches = ocr.find_text(img, text, exact=exact)

        if not matches:
            return json.dumps({"gone": True, "text": text})

        if time.time() >= deadline:
            return json.dumps({
                "gone": False,
                "error": f"Text '{text}' still visible after {timeout}s",
            })

        time.sleep(poll_interval)


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False,
))
def wait_for_time(seconds: float) -> list:
    """
    Wait for a specified amount of time, then take a screenshot.
    Use sparingly — prefer wait_for_text or wait_for_text_gone instead.
    Capped at 30 seconds maximum.

    Args:
        seconds: Number of seconds to wait (max 30).
    """
    seconds = min(seconds, 30.0)
    time.sleep(seconds)
    return [
        json.dumps({"action": "wait", "waited_seconds": seconds}),
        _take_snapshot_image(),
    ]


@mcp.tool(annotations=_INPUT)
def fill_form(
    fields: list[dict],
    timeout: float = 10.0,
    monitor: int = 0,
) -> list:
    """
    Fill multiple form fields in one call. Each field is identified by its
    label text and filled with the given value. Reduces round-trips for forms.
    Returns action result and a screenshot of current screen state.

    Args:
        fields: List of dicts with 'label' and 'value' keys.
                Example: [{"label": "Username", "value": "admin"}, {"label": "Password", "value": "secret"}]
        timeout: Maximum seconds to wait for each label.
        monitor: Monitor index.
    """
    filled = []
    errors = []
    ocr = _get_ocr()

    for field in fields:
        if not isinstance(field, dict) or "label" not in field or "value" not in field:
            errors.append({"label": str(field), "error": "Each field must have 'label' and 'value' keys"})
            continue

        label = field["label"]
        value = field["value"]

        deadline = time.time() + timeout
        while True:
            img = _capture.screenshot(monitor=monitor)
            matches = ocr.find_text(img, label)

            if matches:
                best = matches[0]
                Mouse.click(best.x, best.y)
                time.sleep(0.1)
                Keyboard.press("Ctrl+A")
                time.sleep(0.05)
                Keyboard.press("Delete")
                time.sleep(0.05)
                Keyboard.type_text(value)
                time.sleep(0.1)
                filled.append({"label": label, "value": value, "at": [best.x, best.y]})
                break

            if time.time() >= deadline:
                errors.append({"label": label, "error": f"Label '{label}' not found"})
                break

            time.sleep(0.5)

    result = {"action": "fill_form", "filled": filled}
    if errors:
        result["errors"] = errors

    return [
        json.dumps(result),
        _take_snapshot_image(),
    ]


# =========================================================================
# ENTRY POINT
# =========================================================================


def main():
    """Run the OSWright MCP server."""
    global _ocr_languages, _default_timeout

    parser = argparse.ArgumentParser(
        prog="oswright",
        description="OSWright MCP Server — Playwright-like OS automation for AI agents.",
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="Port for SSE transport. If omitted, uses stdio (default for most MCP clients).",
    )
    parser.add_argument(
        "--host", type=str, default="localhost",
        help="Host to bind SSE server to. Default: localhost. Use 0.0.0.0 for all interfaces.",
    )
    parser.add_argument(
        "--transport", type=str, default=None,
        choices=["stdio", "sse", "streamable-http"],
        help="Transport protocol. Auto-detected: stdio if no --port, sse if --port is set.",
    )
    parser.add_argument(
        "--ocr-languages", nargs="+", default=None,
        help="OCR languages (default: en). Example: --ocr-languages en es fr",
    )
    parser.add_argument(
        "--timeout", type=float, default=None,
        help="Default timeout in seconds for auto-wait operations (default: 10).",
    )
    parser.add_argument(
        "--log-level", type=str, default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO).",
    )

    args = parser.parse_args()

    # Also read from environment variables (env takes precedence if CLI not set)
    if args.ocr_languages:
        _ocr_languages = args.ocr_languages
    elif os.environ.get("OSWRIGHT_OCR_LANGUAGES"):
        _ocr_languages = os.environ["OSWRIGHT_OCR_LANGUAGES"].split(",")

    if args.timeout is not None:
        _default_timeout = args.timeout
    elif os.environ.get("OSWRIGHT_TIMEOUT"):
        _default_timeout = float(os.environ["OSWRIGHT_TIMEOUT"])

    log_level = os.environ.get("OSWRIGHT_LOG_LEVEL", args.log_level).upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    # Determine transport
    transport = args.transport
    if transport is None:
        transport = "sse" if args.port else "stdio"

    if args.port:
        os.environ["FASTMCP_PORT"] = str(args.port)
        os.environ["FASTMCP_HOST"] = args.host

    logger.info(
        "Starting OSWright MCP server (transport=%s, ocr=%s, timeout=%.1fs)",
        transport, _ocr_languages, _default_timeout,
    )

    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
