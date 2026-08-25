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
import platform
import shlex
import subprocess
import threading
import time
from typing import Literal, Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp import Image as MCPImage
from mcp.types import ToolAnnotations

from oswright.capture import ScreenCapture
from oswright.detect import ImageMatcher, OCREngine
from oswright.input import Keyboard, Mouse

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
        "Use wait_for_text to poll for expected UI changes before proceeding.\n\n"
        "Window management: Use list_windows to see what's open, focus_window to bring an app "
        "to front, screenshot_window to capture a specific app, launch_app to start new apps.\n\n"
        "Clipboard: Use get_clipboard/set_clipboard to transfer data between apps.\n\n"
        "Tips: Always screenshot first. Use click_text instead of mouse_click when possible. "
        "After clicking, wait briefly then screenshot to see the result."
    ),
)

_capture: Optional[ScreenCapture] = None
_ocr: Optional[OCREngine] = None
_model = None
# Separate locks: building the OCR engine can take seconds on EasyOCR, and it
# must not block an unrelated screenshot.
_capture_lock = threading.Lock()
_ocr_lock = threading.Lock()
_model_lock = threading.Lock()

# How action tools report the resulting screen state:
#   screenshot - a full image every time (default; what every MCP client expects)
#   delta      - only what changed since the last observation
#   both       - the delta plus the image
_observation_mode: str = "screenshot"
_ocr_languages: list[str] = ["en"]
_default_timeout: float = 10.0
_default_poll_interval: float = 0.5
_snapshot_max_width: int = 0  # 0 = full resolution

# There is exactly one physical mouse and keyboard. Without this lock, two
# concurrent MCP requests can interleave their events — half of one string typed
# into the middle of another, or a click landing between another tool's
# select-all and its replacement text.
_action_lock = threading.RLock()

# --- Tool annotation presets (mirrors Playwright MCP pattern) ---
_READONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
_INPUT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
_ACTION = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
_READONLY_WAIT = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
# For actions a user cannot simply undo (closing an app may discard unsaved work).
_DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
)


def _check_save_path(save_path: Optional[str]) -> Optional[str]:
    """
    Return a JSON error if writing `save_path` would clobber an existing file.

    These capture tools are advertised as read-only, so they must never
    silently destroy something already on disk.
    """
    if save_path and os.path.exists(save_path):
        return json.dumps({
            "error": f"Refusing to overwrite existing file: {save_path}. "
                     f"Choose a different save_path.",
        })
    return None


def _get_capture() -> ScreenCapture:
    """
    Lazy-init screen capture.

    Deliberately not created at import time: importing the module must not
    require a usable display, or the server cannot even report a clean error.
    """
    global _capture
    with _capture_lock:
        if _capture is None:
            _capture = ScreenCapture()
        return _capture


def _get_ocr() -> OCREngine:
    """Lazy-init OCR engine (heavy first load)."""
    global _ocr
    with _ocr_lock:
        if _ocr is None:
            logger.info("Initializing OCR engine (first use, languages=%s)...", _ocr_languages)
            _ocr = OCREngine(languages=_ocr_languages)
        return _ocr


def _timeout(value: Optional[float]) -> float:
    """Resolve a tool's timeout against the server-wide --timeout default."""
    return _default_timeout if value is None else value


def _poll(value: Optional[float]) -> float:
    """Resolve a tool's poll interval against the server-wide default."""
    return _default_poll_interval if value is None else value


def _region_of(
    left: Optional[int], top: Optional[int],
    width: Optional[int], height: Optional[int],
) -> Optional[dict]:
    """
    Build a region dict from four bounds.

    Raises ValueError when only some bounds are given. Previously partial bounds
    were silently ignored and the whole screen was captured instead — the
    opposite of what the caller asked for.
    """
    supplied = [v is not None for v in (left, top, width, height)]
    if not any(supplied):
        return None
    if not all(supplied):
        raise ValueError(
            "A region needs all four of region_left, region_top, region_width "
            "and region_height."
        )
    if width <= 0 or height <= 0:
        raise ValueError(f"Region size must be positive (got {width}x{height}).")
    return {"left": left, "top": top, "width": width, "height": height}


def _encode_png(img) -> MCPImage:
    """Encode a PIL image as MCP image content, downscaling if configured."""
    if _snapshot_max_width and img.width > _snapshot_max_width:
        from PIL import Image as PILImage

        ratio = _snapshot_max_width / img.width
        img = img.resize(
            (_snapshot_max_width, max(1, round(img.height * ratio))),
            PILImage.LANCZOS,
        )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return MCPImage(data=buf.getvalue(), format="png")


def _take_snapshot_image() -> MCPImage:
    """Capture a screenshot and return as MCP Image content."""
    return _encode_png(_get_capture().screenshot())


def _get_model():
    """Lazy-init the incremental screen model."""
    global _model
    with _model_lock:
        if _model is None:
            from oswright.screenmodel import ScreenModel

            _model = ScreenModel(_get_capture(), _get_ocr())
        return _model


def _observation() -> list:
    """
    Build the post-action observation.

    A full screenshot of a 1920x1080 screen costs roughly 2,800 image tokens,
    and an agent taking fifty actions pays that fifty times over to look at a
    screen that changed by a fraction of a percent. In `delta` mode it instead
    receives the handful of strings that actually appeared or disappeared, which
    also states what happened rather than asking the model to spot the
    difference between two pictures.
    """
    if _observation_mode == "screenshot":
        return [_take_snapshot_image()]

    try:
        delta = _get_model().observe()
    except Exception as e:
        logger.warning("Delta observation failed, falling back to screenshot: %s", e)
        return [_take_snapshot_image()]

    payload = json.dumps({"observation": delta.to_dict()})
    if _observation_mode == "both":
        return [payload, _take_snapshot_image()]
    return [payload]


def _is_loopback(host: str) -> bool:
    """True if `host` only accepts connections from this machine."""
    import ipaddress

    normalized = (host or "").strip().strip("[]").lower()
    if normalized in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        # A hostname we cannot classify: treat as remote and require opt-in.
        return False


def _split_command(command: str) -> list[str]:
    """
    Split a command string into argv.

    On Windows `shlex.split` in POSIX mode eats backslashes, mangling
    ``C:\\Windows\\notepad.exe`` into ``C:Windowsnotepad.exe``. Non-POSIX mode
    preserves them but leaves quotes attached, so strip those afterwards.
    """
    if platform.system() == "Windows":
        parts = shlex.split(command, posix=False)
        return [
            p[1:-1] if len(p) >= 2 and p[0] == '"' and p[-1] == '"' else p
            for p in parts
        ]
    return shlex.split(command)


def _click_and_replace(match, value: str):
    """Click a field, clear whatever is in it, and type the new value."""
    Mouse.click(match.x, match.y)
    time.sleep(0.1)
    Keyboard.press("Ctrl+A")
    time.sleep(0.05)
    Keyboard.press("Delete")
    time.sleep(0.05)
    Keyboard.type_text(value)
    time.sleep(0.1)


def _act_on_text(
    action: str,
    text: str,
    exact: bool,
    timeout: Optional[float],
    poll_interval: Optional[float],
    monitor: int,
    perform,
) -> list:
    """
    Shared body for the OCR-driven compound tools (click/double/right/hover).

    Keeping one implementation means the coordinate translation and timeout
    handling cannot drift between them.
    """
    with _action_lock:
        match = _find_text_match(text, exact, timeout, poll_interval, monitor)
        if match is None:
            return [json.dumps({
                "action": action,
                "error": f"Text '{text}' not found on screen within {_timeout(timeout)}s",
            })]

        perform(match)
        time.sleep(0.3)

    return [
        json.dumps({
            "action": action,
            "text_found": match.text,
            "target": [match.x, match.y],
            "confidence": round(match.confidence, 3),
        }),
        *_observation(),
    ]


def _check_xy(x: Optional[int], y: Optional[int]) -> Optional[str]:
    """
    Return a JSON error if exactly one of x/y was supplied.

    Silently falling back to "click wherever the cursor happens to be" when the
    agent supplied only one coordinate would produce a random click.
    """
    if (x is None) != (y is None):
        return json.dumps({
            "error": f"Provide both x and y, or neither (got x={x}, y={y}).",
        })
    return None


def _find_text_match(
    text: str,
    exact: bool,
    timeout: Optional[float],
    poll_interval: Optional[float],
    monitor: int,
):
    """
    Poll OCR until `text` is found, or the timeout expires.

    Returns the best match already translated into absolute screen coordinates,
    or None if it never appeared.
    """
    timeout = _timeout(timeout)
    poll_interval = _poll(poll_interval)
    capture = _get_capture()
    ocr = _get_ocr()
    dx, dy = capture.get_offset(monitor=monitor)
    deadline = time.time() + timeout

    while True:
        img = capture.screenshot(monitor=monitor)
        matches = ocr.find_text(img, text, exact=exact)
        if matches:
            return matches[0].offset(dx, dy)
        if time.time() >= deadline:
            return None
        time.sleep(poll_interval)


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
    try:
        region = _region_of(region_left, region_top, region_width, region_height)
    except ValueError as e:
        return [json.dumps({"error": str(e)})]

    err = _check_save_path(save_path)
    if err:
        return [err]

    capture = _get_capture()
    img = capture.screenshot(path=save_path, region=region, monitor=monitor)
    origin_x, origin_y = capture.get_offset(region=region, monitor=monitor)

    return [
        json.dumps({
            "width": img.size[0],
            "height": img.size[1],
            # Pixel (0,0) of this image is at this absolute screen coordinate.
            # Add it to any coordinate you read off the image before clicking.
            "origin_x": origin_x,
            "origin_y": origin_y,
            **({"saved_to": save_path} if save_path else {}),
        }),
        _encode_png(img),
    ]


@mcp.tool(annotations=_READONLY)
def get_screen_info(monitor: int = 0) -> str:
    """
    Get screen dimensions and monitor count.

    Args:
        monitor: Monitor index (0 = all monitors combined).
    """
    capture = _get_capture()
    size = capture.get_screen_size(monitor)
    count = capture.get_monitor_count()
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
    Returns a list of matches with coordinates and confidence. Coordinates are
    absolute screen coordinates, ready to pass straight to mouse_click.

    Args:
        text: The text to search for on screen.
        exact: If True, requires exact match. If False, substring match.
        region_left: Optional left bound to restrict search area.
        region_top: Optional top bound to restrict search area.
        region_width: Optional width to restrict search area.
        region_height: Optional height to restrict search area.
        monitor: Monitor index.
    """
    try:
        region = _region_of(region_left, region_top, region_width, region_height)
    except ValueError as e:
        return json.dumps({"query": text, "error": str(e)})

    capture = _get_capture()
    img = capture.screenshot(region=region, monitor=monitor)
    matches = _get_ocr().find_text(img, text, exact=exact)
    # Translate to absolute screen coordinates (region offset + monitor origin).
    dx, dy = capture.get_offset(region=region, monitor=monitor)
    results = [_match_to_dict(m.offset(dx, dy)) for m in matches]

    return json.dumps({"query": text, "match_count": len(results), "matches": results})


def _match_to_dict(m) -> dict:
    """Serialise an ElementMatch for a tool response."""
    return {
        "text": m.text,
        "x": m.x,
        "y": m.y,
        "left": m.left,
        "top": m.top,
        "width": m.width,
        "height": m.height,
        "confidence": round(m.confidence, 3),
    }


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
    Returns every detected text element with its position, in absolute
    screen coordinates that are ready to click.

    Args:
        region_left: Optional left bound to restrict OCR area.
        region_top: Optional top bound to restrict OCR area.
        region_width: Optional width to restrict OCR area.
        region_height: Optional height to restrict OCR area.
        monitor: Monitor index.
    """
    try:
        region = _region_of(region_left, region_top, region_width, region_height)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    capture = _get_capture()
    img = capture.screenshot(region=region, monitor=monitor)
    elements = _get_ocr().read_all(img)

    dx, dy = capture.get_offset(region=region, monitor=monitor)
    results = [_match_to_dict(el.offset(dx, dy)) for el in elements]

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
    Coordinates are absolute screen coordinates, ready to pass to mouse_click.

    Args:
        template_path: Absolute path to the template image file to find.
        threshold: Minimum match confidence (0.0-1.0, default 0.8).
        monitor: Monitor index.
    """
    capture = _get_capture()
    img = capture.screenshot(monitor=monitor)
    try:
        matches = ImageMatcher.find_image(img, template_path, threshold=threshold)
    except (FileNotFoundError, ValueError) as e:
        return json.dumps({"template": template_path, "error": str(e)})

    dx, dy = capture.get_offset(monitor=monitor)
    results = []
    for m in (m.offset(dx, dy) for m in matches):
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
    button: Literal["left", "right", "middle"] = "left",
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
    err = _check_xy(x, y)
    if err:
        return [err]

    Mouse.click(x, y, button=button, clicks=clicks)
    time.sleep(0.3)
    pos = Mouse.get_position()
    return [
        json.dumps({"action": "click", "button": button, "clicks": clicks, "position": pos}),
        *_observation(),
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
    err = _check_xy(x, y)
    if err:
        return [err]

    Mouse.double_click(x, y)
    time.sleep(0.3)
    pos = Mouse.get_position()
    return [
        json.dumps({"action": "double_click", "position": pos}),
        *_observation(),
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
        *_observation(),
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
        *_observation(),
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
        *_observation(),
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
        *_observation(),
    ]


# =========================================================================
# COMPOUND TOOLS (high-level actions)
# =========================================================================


@mcp.tool(annotations=_INPUT)
def click_text(
    text: str,
    exact: bool = False,
    button: Literal["left", "right", "middle"] = "left",
    timeout: Optional[float] = None,
    poll_interval: Optional[float] = None,
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
                 Defaults to the server's --timeout setting.
        poll_interval: Seconds between each retry.
        monitor: Monitor index.
    """
    return _act_on_text(
        "click_text", text, exact, timeout, poll_interval, monitor,
        lambda m: Mouse.click(m.x, m.y, button=button),
    )


@mcp.tool(annotations=_INPUT)
def double_click_text(
    text: str,
    exact: bool = False,
    timeout: Optional[float] = None,
    poll_interval: Optional[float] = None,
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
    return _act_on_text(
        "double_click_text", text, exact, timeout, poll_interval, monitor,
        lambda m: Mouse.double_click(m.x, m.y),
    )


@mcp.tool(annotations=_INPUT)
def right_click_text(
    text: str,
    exact: bool = False,
    timeout: Optional[float] = None,
    poll_interval: Optional[float] = None,
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
    return _act_on_text(
        "right_click_text", text, exact, timeout, poll_interval, monitor,
        lambda m: Mouse.click(m.x, m.y, button="right"),
    )


@mcp.tool(annotations=_INPUT)
def hover_text(
    text: str,
    exact: bool = False,
    timeout: Optional[float] = None,
    poll_interval: Optional[float] = None,
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
    return _act_on_text(
        "hover_text", text, exact, timeout, poll_interval, monitor,
        lambda m: Mouse.move(m.x, m.y),
    )


@mcp.tool(annotations=_READONLY_WAIT)
def wait_for_text(
    text: str,
    exact: bool = False,
    timeout: Optional[float] = None,
    poll_interval: Optional[float] = None,
    monitor: int = 0,
) -> str:
    """
    Wait for specific text to appear on screen. Polls via OCR until found or timeout.

    Args:
        text: The text to wait for.
        exact: Require exact match.
        timeout: Maximum seconds to wait. Defaults to the server's --timeout.
        poll_interval: Seconds between polls.
        monitor: Monitor index.
    """
    match = _find_text_match(text, exact, timeout, poll_interval, monitor)
    if match is None:
        return json.dumps({
            "found": False,
            "error": f"Text '{text}' not found within {_timeout(timeout)}s",
        })
    return json.dumps({
        "found": True,
        "text": match.text,
        "x": match.x, "y": match.y,
        "confidence": round(match.confidence, 3),
    })


@mcp.tool(annotations=_READONLY_WAIT)
def wait_for_text_gone(
    text: str,
    exact: bool = False,
    timeout: Optional[float] = None,
    poll_interval: Optional[float] = None,
    monitor: int = 0,
) -> str:
    """
    Wait for text to disappear from screen. Polls via OCR until gone or timeout.
    Useful for waiting for loading spinners, progress dialogs, etc. to finish.

    Args:
        text: The text to wait to disappear.
        exact: Require exact match.
        timeout: Maximum seconds to wait. Defaults to the server's --timeout.
        poll_interval: Seconds between polls.
        monitor: Monitor index.
    """
    resolved_timeout = _timeout(timeout)
    interval = _poll(poll_interval)
    capture = _get_capture()
    ocr = _get_ocr()
    deadline = time.time() + resolved_timeout

    while True:
        img = capture.screenshot(monitor=monitor)
        if not ocr.find_text(img, text, exact=exact):
            return json.dumps({"gone": True, "text": text})

        if time.time() >= deadline:
            return json.dumps({
                "gone": False,
                "error": f"Text '{text}' still visible after {resolved_timeout}s",
            })

        time.sleep(interval)


@mcp.tool(annotations=_INPUT)
def fill_field(
    target_text: str,
    value: str,
    exact: bool = False,
    timeout: Optional[float] = None,
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
    with _action_lock:
        match = _find_text_match(target_text, exact, timeout, None, monitor)
        if match is None:
            return [json.dumps({
                "action": "fill_field",
                "error": f"Text '{target_text}' not found on screen within "
                         f"{_timeout(timeout)}s",
            })]

        _click_and_replace(match, value)

    return [
        json.dumps({
            "action": "fill_field",
            "target": target_text,
            "value": value,
            "clicked_at": [match.x, match.y],
        }),
        *_observation(),
    ]


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
    seconds = max(0.0, min(seconds, 30.0))
    time.sleep(seconds)
    return [
        json.dumps({"action": "wait", "waited_seconds": seconds}),
        *_observation(),
    ]


@mcp.tool(annotations=_INPUT)
def fill_form(
    fields: list[dict],
    timeout: Optional[float] = None,
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

    # Held across every field so another request cannot type into the middle of
    # this form.
    with _action_lock:
        for field in fields:
            if not isinstance(field, dict) or "label" not in field or "value" not in field:
                errors.append({
                    "label": str(field),
                    "error": "Each field must have 'label' and 'value' keys",
                })
                continue

            label = field["label"]
            value = field["value"]

            match = _find_text_match(label, False, timeout, None, monitor)
            if match is None:
                errors.append({"label": label, "error": f"Label '{label}' not found"})
                continue

            _click_and_replace(match, value)
            filled.append({"label": label, "value": value, "at": [match.x, match.y]})

    result = {"action": "fill_form", "filled": filled}
    if errors:
        result["errors"] = errors

    return [
        json.dumps(result),
        *_observation(),
    ]


# =========================================================================
# WINDOW MANAGEMENT TOOLS
# =========================================================================


@mcp.tool(annotations=_READONLY)
def list_windows(title_filter: Optional[str] = None) -> str:
    """
    List all visible windows on the desktop. Optionally filter by title substring.
    Returns window titles, positions, sizes, and process names.

    Args:
        title_filter: Optional substring to filter window titles (case-insensitive).
    """
    from oswright.window import list_windows as _list_windows

    windows = _list_windows(title_filter=title_filter)
    results = []
    for w in windows:
        results.append({
            "title": w.title,
            "handle": w.handle,
            "left": w.left,
            "top": w.top,
            "width": w.width,
            "height": w.height,
            "process_name": w.process_name,
            "process_id": w.process_id,
        })

    return json.dumps({"window_count": len(results), "windows": results})


@mcp.tool(annotations=_ACTION)
def focus_window(title: str) -> list:
    """
    Bring a window to the foreground by its title (substring match).
    Returns the focused window info and a screenshot.

    Args:
        title: Window title substring to find and focus.
    """
    from oswright.window import focus_window as _focus_window

    win = _focus_window(title=title)
    time.sleep(0.3)

    if win:
        return [
            json.dumps({
                "action": "focus_window",
                "title": win.title,
                "handle": win.handle,
                "position": [win.left, win.top, win.width, win.height],
            }),
            *_observation(),
        ]
    return [json.dumps({"action": "focus_window", "error": f"Window '{title}' not found"})]


@mcp.tool(annotations=_DESTRUCTIVE)
def close_window(title: str) -> list:
    """
    Close a window by its title (substring match). Sends WM_CLOSE.
    This may discard unsaved work in the target application.
    Returns action result and a screenshot.

    Args:
        title: Window title substring to find and close.
    """
    from oswright.window import close_window as _close_window

    success = _close_window(title=title)
    time.sleep(0.5)

    return [
        json.dumps({
            "action": "close_window",
            "title": title,
            "success": success,
        }),
        *_observation(),
    ]


@mcp.tool(annotations=_ACTION)
def minimize_window(title: str) -> list:
    """
    Minimize a window by its title (substring match).
    Returns action result and a screenshot.

    Args:
        title: Window title substring to find and minimize.
    """
    from oswright.window import minimize_window as _minimize_window

    success = _minimize_window(title=title)
    time.sleep(0.3)

    return [
        json.dumps({
            "action": "minimize_window",
            "title": title,
            "success": success,
        }),
        *_observation(),
    ]


@mcp.tool(annotations=_READONLY)
def screenshot_window(title: str, save_path: Optional[str] = None) -> list:
    """
    Take a screenshot of a specific window (by title).
    Useful for capturing just one application without the rest of the desktop.
    Returns the image as native MCP image content.

    Args:
        title: Window title substring to capture.
        save_path: Optional file path to save the screenshot.
    """
    from oswright.window import get_window_region

    err = _check_save_path(save_path)
    if err:
        return [err]

    region = get_window_region(title=title)
    if region is None:
        return [json.dumps({
            "error": f"Window '{title}' not found, or it is minimized and has "
                     f"nothing to capture. Use focus_window first."
        })]

    img = _get_capture().screenshot(path=save_path, region=region)

    return [
        json.dumps({
            "window": title,
            "width": img.size[0],
            "height": img.size[1],
            # Pixel (0,0) of this image maps here on the virtual screen.
            "origin_x": region["left"],
            "origin_y": region["top"],
            **({"saved_to": save_path} if save_path else {}),
        }),
        _encode_png(img),
    ]


# =========================================================================
# CLIPBOARD TOOLS
# =========================================================================


@mcp.tool(annotations=_READONLY)
def get_clipboard() -> str:
    """
    Get the current text content of the system clipboard.
    Returns the clipboard text or null if empty.
    """
    from oswright.clipboard import get_text

    text = get_text()
    return json.dumps({"clipboard_text": text})


@mcp.tool(annotations=_INPUT)
def set_clipboard(text: str) -> str:
    """
    Set text to the system clipboard. Useful for pasting data into applications.

    Args:
        text: The text to copy to the clipboard.
    """
    from oswright.clipboard import set_text

    success = set_text(text)
    return json.dumps({"action": "set_clipboard", "success": success, "length": len(text)})


# =========================================================================
# APP LAUNCH TOOL
# =========================================================================


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True,
))
def launch_app(
    command: str,
    args: Optional[list[str]] = None,
    wait_text: Optional[str] = None,
    timeout: Optional[float] = None,
) -> list:
    """
    Launch an application. Optionally wait for specific text to appear on
    screen (indicating the app has loaded). Returns a screenshot after launch.

    The command is executed directly, never through a shell, so shell syntax
    (pipes, redirection, chaining) is not interpreted and not available.

    Args:
        command: Application name or full path (e.g. 'notepad', 'calc',
                 r'C:\\Windows\\System32\\notepad.exe').
        args: Optional argument list. Prefer this over embedding arguments in
              `command`, since it needs no quoting at all.
        wait_text: Optional text to wait for after launch (e.g., the app title).
        timeout: How long to wait for wait_text to appear.
    """
    if not command or not command.strip():
        return [json.dumps({"action": "launch_app", "error": "command must not be empty"})]

    # No shell metacharacter blocklist: with shell=False nothing interprets
    # them, so they cannot inject anything. The old blocklist included the
    # backslash, which rejected every absolute Windows path.
    if args is not None:
        argv = [command, *args]
    else:
        try:
            argv = _split_command(command)
        except ValueError as e:
            return [json.dumps({"action": "launch_app", "error": str(e)})]

    if not argv:
        return [json.dumps({"action": "launch_app", "error": "command must not be empty"})]

    try:
        if platform.system() == "Windows":
            proc = subprocess.Popen(argv, shell=False)
        else:
            proc = subprocess.Popen(argv, shell=False, start_new_session=True)
    except (OSError, ValueError) as e:
        return [json.dumps({
            "action": "launch_app",
            "command": command,
            "error": f"Could not launch: {e}",
        })]

    time.sleep(1)  # Give the app a moment to start

    found = None
    if wait_text:
        found = _find_text_match(wait_text, False, timeout, None, 0) is not None

    return [
        json.dumps({
            "action": "launch_app",
            "command": command,
            "argv": argv,
            "pid": proc.pid,
            # Report whether the text actually appeared, rather than implying
            # success just because a wait was requested.
            **({"wait_text": wait_text, "wait_text_found": found} if wait_text else {}),
        }),
        *_observation(),
    ]


# =========================================================================
# OCR INFO TOOL
# =========================================================================


@mcp.tool(annotations=_READONLY)
def get_ocr_info() -> str:
    """
    Get information about the active OCR backend and available backends.
    Useful for debugging OCR issues.
    """
    from oswright.detect import _OCR_BACKENDS

    ocr = _get_ocr()
    return json.dumps({
        "active_backend": ocr.backend_name,
        "available_backends": _OCR_BACKENDS,
        "languages": ocr._languages,
    })


# =========================================================================
# INCREMENTAL PERCEPTION TOOLS
# =========================================================================


@mcp.tool(annotations=_READONLY)
def observe(force_full: bool = False) -> str:
    """
    Report what changed on screen since the last observation.

    Prefer this over `screenshot` for tracking state. It maintains a running
    model of on-screen text and rescans only the regions that actually moved,
    so it costs a fraction of a full screen read and returns tens of tokens
    instead of thousands. The first call scans everything.

    Args:
        force_full: Rescan the entire screen instead of only what changed.
                    Use if you suspect the model has drifted.
    """
    delta = _get_model().observe(force_full=force_full)
    return json.dumps(delta.to_dict())


@mcp.tool(annotations=_READONLY)
def find_element(
    text: str,
    exact: bool = False,
    window_title: Optional[str] = None,
) -> str:
    """
    Find on-screen text using the cheapest method that can answer.

    Tries, in order: the existing screen model (free), an incremental rescan of
    changed regions, the accessibility tree, the application's own text buffer,
    and finally a full screen read. Returns coordinates ready for mouse_click,
    plus which rung answered.

    Args:
        text: The visible text to find.
        exact: Require the label to equal `text` rather than contain it.
        window_title: Restrict accessibility lookups to one window.
    """
    from oswright.cascade import resolve

    return json.dumps(resolve(text, _get_model(), exact=exact, window_title=window_title).to_dict())


@mcp.tool(annotations=_INPUT)
def click_element(
    text: str,
    exact: bool = False,
    button: Literal["left", "right", "middle"] = "left",
    window_title: Optional[str] = None,
) -> list:
    """
    Find on-screen text via the resolution cascade and click it.

    The cheaper alternative to click_text: it reuses what is already known about
    the screen instead of running OCR over the whole display on every call.

    Args:
        text: The visible text to find and click.
        exact: Require the label to equal `text` rather than contain it.
        button: Mouse button.
        window_title: Restrict accessibility lookups to one window.
    """
    from oswright.cascade import resolve

    with _action_lock:
        result = resolve(text, _get_model(), exact=exact, window_title=window_title)
        if not result.found:
            return [json.dumps({"action": "click_element", **result.to_dict()})]

        x, y = result.best.center
        Mouse.click(x, y, button=button)
        time.sleep(0.3)

    return [
        json.dumps({
            "action": "click_element",
            "clicked": result.best.to_dict(),
            "rung": result.rung,
            "source": result.source,
            "duration_ms": round(result.duration_ms, 1),
        }),
        *_observation(),
    ]


@mcp.tool(annotations=_READONLY)
def read_model_text(query: Optional[str] = None, limit: int = 200) -> str:
    """
    Read on-screen text from the incremental model.

    Unlike read_screen_text this does not re-OCR the display; it refreshes only
    the regions that changed and then answers from the model.

    Args:
        query: Optional substring filter.
        limit: Maximum elements to return.
    """
    model = _get_model()
    model.observe()
    elements = model.find(query) if query else model.elements
    elements = sorted(elements, key=lambda e: (e.region.top, e.region.left))
    return json.dumps({
        "element_count": len(elements),
        "returned": min(len(elements), limit),
        "elements": [e.to_dict() for e in elements[:limit]],
    })


@mcp.tool(annotations=_READONLY)
def perception_stats() -> str:
    """
    Report how much perception work the incremental model has avoided.
    Useful for verifying the screen model is actually saving effort.
    """
    return json.dumps({
        "observation_mode": _observation_mode,
        **_get_model().efficiency(),
    })


# =========================================================================
# ACCESSIBILITY / UI AUTOMATION TOOLS (Windows)
# =========================================================================


@mcp.tool(annotations=_READONLY)
def get_ui_tree(window_title: Optional[str] = None, max_depth: int = 8) -> str:
    """
    Get the accessibility tree of the focused window (or a specific window).
    Returns all interactive UI elements with their names, types, and positions.
    This is deterministic and instant — much more reliable than OCR for apps
    with proper accessibility support (most modern Windows apps).

    Windows only. Falls back gracefully on Linux/macOS.

    Args:
        window_title: Optional window title to inspect (default: focused window).
        max_depth: How deep to walk the UI tree (default: 8).
    """
    from oswright.accessibility import find_all_elements, is_available

    if not is_available():
        return json.dumps({
            "error": "UI Automation not available (Windows only, requires 'uiautomation' package)",
            "hint": "pip install uiautomation",
        })

    elements = find_all_elements(window_title=window_title, max_depth=max_depth)
    results = [el.to_dict() for el in elements]

    return json.dumps({
        "element_count": len(results),
        "elements": results,
        **({"window": window_title} if window_title else {"window": "focused"}),
    })


@mcp.tool(annotations=_INPUT)
def click_ui_element(
    name: Optional[str] = None,
    control_type: Optional[str] = None,
    automation_id: Optional[str] = None,
    window_title: Optional[str] = None,
) -> list:
    """
    Click a UI element using the accessibility tree (Windows only).
    More reliable than OCR — finds elements deterministically by their role and name.

    Args:
        name: Element name/label (substring match). E.g., "Save", "OK", "File".
        control_type: Element type: Button, Edit, CheckBox, MenuItem, etc.
        automation_id: Element automation ID (exact match, if known from get_ui_tree).
        window_title: Target a specific window.
    """
    from oswright.accessibility import click_element, is_available

    if not is_available():
        return [json.dumps({"error": "UI Automation not available (Windows only)"})]

    el = click_element(
        name=name, control_type=control_type,
        automation_id=automation_id, window_title=window_title,
    )
    time.sleep(0.3)

    if el:
        return [
            json.dumps({
                "action": "click_ui_element",
                "clicked": el.to_dict(),
            }),
            *_observation(),
        ]
    return [json.dumps({
        "action": "click_ui_element",
        "error": f"Element not found (name={name}, type={control_type})",
    })]


@mcp.tool(annotations=_INPUT)
def fill_ui_element(
    value: str,
    name: Optional[str] = None,
    automation_id: Optional[str] = None,
    window_title: Optional[str] = None,
) -> list:
    """
    Set the value of a UI element (e.g., type text into a text box) using
    the accessibility tree (Windows only). More reliable than OCR-based fill.

    Args:
        value: Text to enter.
        name: Element name/label to find.
        automation_id: Automation ID of the element.
        window_title: Target a specific window.
    """
    from oswright.accessibility import is_available, set_element_value

    if not is_available():
        return [json.dumps({"error": "UI Automation not available (Windows only)"})]

    success = set_element_value(
        value=value, name=name,
        automation_id=automation_id, window_title=window_title,
    )
    time.sleep(0.2)

    return [
        json.dumps({
            "action": "fill_ui_element",
            "success": success,
            "name": name,
            "value": value,
        }),
        *_observation(),
    ]


# =========================================================================
# ADVANCED SCREEN TOOLS
# =========================================================================


@mcp.tool(annotations=_READONLY)
def get_active_window() -> str:
    """
    Get information about the currently active/focused window.
    Returns the window title, position, size, and process name.
    """
    import platform

    from oswright.window import list_windows as _list_windows

    if platform.system() == "Windows":
        import ctypes
        import ctypes.wintypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)

        rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))

        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

        return json.dumps({
            "title": buf.value,
            "handle": hwnd,
            "left": rect.left,
            "top": rect.top,
            "width": rect.right - rect.left,
            "height": rect.bottom - rect.top,
            "process_id": pid.value,
        })
    else:
        # Fallback: return first window from list
        wins = _list_windows()
        if wins:
            w = wins[0]
            return json.dumps({
                "title": w.title,
                "left": w.left, "top": w.top,
                "width": w.width, "height": w.height,
            })
        return json.dumps({"error": "No active window found"})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=True))
def wait_for_change(
    timeout: Optional[float] = None, poll_interval: Optional[float] = None
) -> list:
    """
    Wait for the screen to visually change. Takes a baseline screenshot,
    then polls until the screen looks different (or timeout).
    Useful after triggering an action to wait for the UI to update.

    Args:
        timeout: Maximum seconds to wait for a change.
        poll_interval: Seconds between polls.
    """
    from oswright.cache import get_diff_region, images_differ

    resolved_timeout = _timeout(timeout)
    interval = _poll(poll_interval)
    capture = _get_capture()

    baseline = capture.screenshot()
    deadline = time.time() + resolved_timeout

    while time.time() < deadline:
        time.sleep(interval)
        current = capture.screenshot()

        if images_differ(baseline, current):
            return [
                json.dumps({
                    "changed": True,
                    "diff_region": get_diff_region(baseline, current),
                }),
                _encode_png(current),
            ]

    return [json.dumps({
        "changed": False,
        "error": f"Screen did not change within {resolved_timeout}s",
    })]


def main():
    """Run the OSWright MCP server."""
    global _ocr_languages, _default_timeout, _snapshot_max_width, _observation_mode

    parser = argparse.ArgumentParser(
        prog="oswright",
        description="OSWright MCP Server - Playwright-like OS automation for AI agents.",
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="Port for SSE transport. If omitted, uses stdio (default for most MCP clients).",
    )
    parser.add_argument(
        "--host", type=str, default=None,
        help="Host to bind the HTTP/SSE server to. Default: 127.0.0.1 (local only).",
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
        "--snapshot-max-width", type=int, default=None,
        help=(
            "Downscale the auto-snapshot returned by action tools to at most this "
            "width. 0 (default) keeps full resolution. Lower values cut token cost "
            "substantially; coordinates from OCR tools stay in real screen pixels."
        ),
    )
    parser.add_argument(
        "--observation-mode", type=str, default=None,
        choices=["screenshot", "delta", "both"],
        help=(
            "What action tools return as the resulting screen state. "
            "'screenshot' (default) returns a full image, costing ~2800 image "
            "tokens per action. 'delta' returns only the text that appeared or "
            "disappeared, typically a few dozen tokens. 'both' returns each."
        ),
    )
    parser.add_argument(
        "--allow-remote", action="store_true",
        help=(
            "Permit binding to a non-loopback address. OSWright has no "
            "authentication, and exposing it grants full control of this desktop "
            "to anyone who can reach the port."
        ),
    )
    parser.add_argument(
        "--log-level", type=str, default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO).",
    )

    args = parser.parse_args()

    # For every setting: an explicit CLI flag wins, otherwise the environment
    # variable, otherwise the built-in default. --log-level used to be handled
    # the other way round, so the environment silently overrode the flag.
    if args.ocr_languages:
        _ocr_languages = args.ocr_languages
    elif os.environ.get("OSWRIGHT_OCR_LANGUAGES"):
        _ocr_languages = [
            lang.strip()
            for lang in os.environ["OSWRIGHT_OCR_LANGUAGES"].replace(",", " ").split()
            if lang.strip()
        ]

    if args.timeout is not None:
        _default_timeout = args.timeout
    elif os.environ.get("OSWRIGHT_TIMEOUT"):
        _default_timeout = float(os.environ["OSWRIGHT_TIMEOUT"])

    if args.snapshot_max_width is not None:
        _snapshot_max_width = max(0, args.snapshot_max_width)
    elif os.environ.get("OSWRIGHT_SNAPSHOT_MAX_WIDTH"):
        _snapshot_max_width = max(0, int(os.environ["OSWRIGHT_SNAPSHOT_MAX_WIDTH"]))

    _observation_mode = (
        args.observation_mode
        or os.environ.get("OSWRIGHT_OBSERVATION_MODE")
        or "screenshot"
    ).lower()
    if _observation_mode not in ("screenshot", "delta", "both"):
        parser.error(f"Invalid observation mode: {_observation_mode!r}")

    log_level = (
        args.log_level
        or os.environ.get("OSWRIGHT_LOG_LEVEL")
        or "INFO"
    ).upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    # Determine transport
    transport = args.transport
    if transport is None:
        transport = "sse" if args.port else "stdio"

    host = args.host or os.environ.get("FASTMCP_HOST") or "127.0.0.1"

    if transport != "stdio":
        if not _is_loopback(host) and not args.allow_remote:
            parser.error(
                f"Refusing to bind to {host}: OSWright exposes full keyboard, "
                f"mouse, screen and clipboard control with no authentication. "
                f"Anyone who can reach that port can take over this machine. "
                f"Bind to 127.0.0.1 and use an SSH tunnel, or pass --allow-remote "
                f"if you genuinely intend this."
            )
        if not _is_loopback(host):
            logger.warning(
                "OSWright is listening on %s with NO AUTHENTICATION. Any host that "
                "can reach this port has full control of this desktop.", host,
            )

        # These must be applied to the live settings object. Setting the
        # environment variables here has no effect, because FastMCP already read
        # them when it was constructed at import time.
        mcp.settings.host = host
        if args.port:
            mcp.settings.port = args.port

    logger.info(
        "Starting OSWright MCP server (transport=%s, ocr=%s, timeout=%.1fs%s)",
        transport, _ocr_languages, _default_timeout,
        f", bind={host}:{mcp.settings.port}" if transport != "stdio" else "",
    )

    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
