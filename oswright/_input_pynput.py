"""
Cross-platform input backend using pynput.

Used on Linux and macOS. Requires the 'pynput' package.
On Linux: requires X11 display server (Wayland has limited support).
On macOS: requires Accessibility permissions (System Settings > Privacy > Accessibility).
"""

import logging
import time
from typing import Literal, Optional

logger = logging.getLogger(__name__)

# Lazy-loaded controllers to avoid import-time side effects
_mouse_ctrl = None
_kbd_ctrl = None


def _get_mouse():
    global _mouse_ctrl
    if _mouse_ctrl is None:
        try:
            from pynput.mouse import Controller
            _mouse_ctrl = Controller()
        except Exception as e:
            raise RuntimeError(
                "Failed to initialize mouse controller. "
                "On macOS, grant Accessibility permissions in System Settings. "
                "On Linux, ensure an X11 display server is running. "
                f"Error: {e}"
            ) from e
    return _mouse_ctrl


def _get_keyboard():
    global _kbd_ctrl
    if _kbd_ctrl is None:
        try:
            from pynput.keyboard import Controller
            _kbd_ctrl = Controller()
        except Exception as e:
            raise RuntimeError(
                "Failed to initialize keyboard controller. "
                "On macOS, grant Accessibility permissions in System Settings. "
                "On Linux, ensure an X11 display server is running. "
                f"Error: {e}"
            ) from e
    return _kbd_ctrl


# Lazy-loaded key map (avoids importing pynput at module level)
_KEY_MAP = None


def _get_key_map():
    global _KEY_MAP
    if _KEY_MAP is None:
        from pynput.keyboard import Key
        _KEY_MAP = {
            "backspace": Key.backspace,
            "tab": Key.tab,
            "enter": Key.enter,
            "return": Key.enter,
            "shift": Key.shift,
            "ctrl": Key.ctrl,
            "control": Key.ctrl,
            "alt": Key.alt,
            "menu": Key.alt,
            "pause": Key.pause,
            "capslock": Key.caps_lock,
            "escape": Key.esc,
            "esc": Key.esc,
            "space": Key.space,
            "pageup": Key.page_up,
            "pagedown": Key.page_down,
            "end": Key.end,
            "home": Key.home,
            "left": Key.left,
            "up": Key.up,
            "right": Key.right,
            "down": Key.down,
            "insert": Key.insert,
            "delete": Key.delete,
            "win": Key.cmd,
            "windows": Key.cmd,
            "meta": Key.cmd,
            "f1": Key.f1, "f2": Key.f2, "f3": Key.f3, "f4": Key.f4,
            "f5": Key.f5, "f6": Key.f6, "f7": Key.f7, "f8": Key.f8,
            "f9": Key.f9, "f10": Key.f10, "f11": Key.f11, "f12": Key.f12,
            "numlock": Key.num_lock,
            "scrolllock": Key.scroll_lock,
        }
    return _KEY_MAP


# Modifier key names for combo parsing
MODIFIER_KEYS = {"ctrl", "control", "shift", "alt", "meta", "win", "windows"}


def _resolve_key(key_name: str):
    """Convert a key name string to a pynput Key or character."""
    key_lower = key_name.lower()
    key_map = _get_key_map()

    if key_lower in key_map:
        return key_map[key_lower]

    # Single character keys (a-z, 0-9, etc.)
    if len(key_name) == 1:
        return key_name.lower()

    raise ValueError(f"Unknown key: {key_name}")


class Mouse:
    """Mouse input simulation using pynput (cross-platform)."""

    @staticmethod
    def get_position() -> tuple[int, int]:
        """Get current cursor position."""
        mouse = _get_mouse()
        x, y = mouse.position
        return (int(x), int(y))

    @staticmethod
    def move(x: int, y: int):
        """Move cursor to absolute screen coordinates."""
        mouse = _get_mouse()
        mouse.position = (x, y)

    @staticmethod
    def click(
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: Literal["left", "right", "middle"] = "left",
        clicks: int = 1,
        interval: float = 0.05,
    ):
        """Click at position. If x/y not given, clicks at current position."""
        from pynput.mouse import Button

        if x is not None and y is not None:
            Mouse.move(x, y)
            time.sleep(0.01)

        btn = {
            "left": Button.left,
            "right": Button.right,
            "middle": Button.middle,
        }[button]

        mouse = _get_mouse()
        for i in range(clicks):
            if i > 0:
                time.sleep(interval)
            mouse.click(btn, 1)

    @staticmethod
    def double_click(x: Optional[int] = None, y: Optional[int] = None):
        """Double-click at position."""
        Mouse.click(x, y, clicks=2, interval=0.05)

    @staticmethod
    def scroll(amount: int, x: Optional[int] = None, y: Optional[int] = None):
        """Scroll the mouse wheel. Positive = up, negative = down."""
        if x is not None and y is not None:
            Mouse.move(x, y)
            time.sleep(0.01)

        mouse = _get_mouse()
        mouse.scroll(0, amount)

    @staticmethod
    def drag(
        start_x: int, start_y: int, end_x: int, end_y: int,
        button: Literal["left", "right"] = "left",
        duration: float = 0.3, steps: int = 20,
    ):
        """Drag from one point to another."""
        from pynput.mouse import Button

        if steps < 1:
            raise ValueError(f"steps must be >= 1 (got {steps})")
        if duration < 0:
            raise ValueError(f"duration must be >= 0 (got {duration})")

        btn = {
            "left": Button.left,
            "right": Button.right,
        }[button]

        mouse = _get_mouse()
        Mouse.move(start_x, start_y)
        time.sleep(0.05)

        mouse.press(btn)
        # Always release, even if a move fails partway through: a stuck button
        # turns every later click into a drag.
        try:
            step_delay = duration / steps
            for i in range(1, steps + 1):
                frac = i / steps
                cx = int(start_x + (end_x - start_x) * frac)
                cy = int(start_y + (end_y - start_y) * frac)
                Mouse.move(cx, cy)
                time.sleep(step_delay)
        finally:
            mouse.release(btn)


class Keyboard:
    """Keyboard input simulation using pynput (cross-platform)."""

    @staticmethod
    def type_text(text: str, delay: float = 0.02):
        """Type text character by character."""
        kbd = _get_keyboard()
        for char in text:
            kbd.type(char)
            if delay > 0:
                time.sleep(delay)

    @staticmethod
    def press(key: str):
        """
        Press a key or key combination.
        Supports combos like 'Ctrl+C', 'Alt+Tab', 'Ctrl+Shift+S'.
        """
        kbd = _get_keyboard()
        parts = [p.strip().lower() for p in key.split("+") if p.strip()]
        if not parts:
            raise ValueError(f"Empty key specification: {key!r}")

        modifiers = [p for p in parts if p in MODIFIER_KEYS]
        main_keys = [p for p in parts if p not in MODIFIER_KEYS]

        # Resolve everything up front so an unknown key cannot leave a
        # modifier held down.
        resolved_mods = [(m, _resolve_key(m)) for m in modifiers]
        resolved_main = [_resolve_key(k) for k in main_keys]

        pressed = []
        try:
            for _name, k in resolved_mods:
                kbd.press(k)
                pressed.append(k)

            for k in resolved_main:
                kbd.press(k)
                time.sleep(0.01)
                kbd.release(k)
        finally:
            # Release modifiers in reverse, even on failure. A stuck Ctrl or Alt
            # would corrupt every subsequent keystroke sent to the desktop.
            for k in reversed(pressed):
                kbd.release(k)

    @staticmethod
    def hotkey(*keys: str):
        """Press a hotkey combination, e.g. hotkey('ctrl', 'c')."""
        combo = "+".join(keys)
        Keyboard.press(combo)
