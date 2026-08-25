"""
Windows input backend using Win32 API (SendInput, SetCursorPos, GetCursorPos).

Used automatically on Windows. No external dependencies beyond ctypes (stdlib).
"""

import ctypes
import ctypes.wintypes
import logging
import time
from typing import Literal, Optional

from oswright._dpi import ensure_dpi_aware

logger = logging.getLogger(__name__)

# Mouse coordinates are physical pixels; without this they would be logical
# pixels on a scaled display and every click would be offset.
ensure_dpi_aware()

# use_last_error lets SendInput failures report a real GetLastError value.
user32 = ctypes.WinDLL("user32", use_last_error=True)

# Windows API constants
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004


# Key name to virtual key code mapping
VK_CODES = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12, "menu": 0x12,
    "pause": 0x13, "capslock": 0x14, "escape": 0x1B, "esc": 0x1B,
    "space": 0x20, "pageup": 0x21, "pagedown": 0x22,
    "end": 0x23, "home": 0x24,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "insert": 0x2D, "delete": 0x2E,
    "win": 0x5B, "windows": 0x5B, "meta": 0x5B,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
    "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
    "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "numlock": 0x90, "scrolllock": 0x91,
    "a": 0x41, "b": 0x42, "c": 0x43, "d": 0x44, "e": 0x45,
    "f": 0x46, "g": 0x47, "h": 0x48, "i": 0x49, "j": 0x4A,
    "k": 0x4B, "l": 0x4C, "m": 0x4D, "n": 0x4E, "o": 0x4F,
    "p": 0x50, "q": 0x51, "r": 0x52, "s": 0x53, "t": 0x54,
    "u": 0x55, "v": 0x56, "w": 0x57, "x": 0x58, "y": 0x59, "z": 0x5A,
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
}

# Modifier key names for combo parsing
MODIFIER_KEYS = {"ctrl", "control", "shift", "alt", "meta", "win", "windows"}


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.wintypes.LONG),
        ("dy", ctypes.wintypes.LONG),
        ("mouseData", ctypes.wintypes.DWORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.wintypes.WORD),
        ("wScan", ctypes.wintypes.WORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", ctypes.wintypes.DWORD), ("union", INPUT_UNION)]


# Explicit signatures: on 64-bit Windows the default int return/args silently
# truncate pointers and produce wrong results.
user32.SendInput.argtypes = [ctypes.wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = ctypes.wintypes.UINT
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = ctypes.wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(ctypes.wintypes.POINT)]
user32.GetCursorPos.restype = ctypes.wintypes.BOOL


def _send_input(*inputs) -> int:
    """
    Send input events to the OS.

    Returns the number of events accepted. A short count means the events were
    blocked — commonly by UIPI, when a more-elevated window has focus.
    """
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    sent = user32.SendInput(n, arr, ctypes.sizeof(INPUT))
    if sent != n:
        err = ctypes.get_last_error()
        logger.warning(
            "SendInput delivered %d/%d events (error %d). Input may be blocked by "
            "a more-privileged window; try running with matching privileges.",
            sent, n, err,
        )
    return sent


class Mouse:
    """Mouse input simulation."""

    @staticmethod
    def get_position() -> tuple[int, int]:
        """Get current cursor position."""
        point = ctypes.wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(point)):
            raise OSError(
                f"GetCursorPos failed (error {ctypes.get_last_error()}). "
                "The session may be locked or on a secure desktop."
            )
        return (point.x, point.y)

    @staticmethod
    def move(x: int, y: int):
        """Move cursor to absolute screen coordinates."""
        if not user32.SetCursorPos(int(x), int(y)):
            raise OSError(
                f"SetCursorPos({x}, {y}) failed (error {ctypes.get_last_error()}). "
                "The coordinates may be off-screen, or the session may be locked."
            )

    @staticmethod
    def click(
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: Literal["left", "right", "middle"] = "left",
        clicks: int = 1,
        interval: float = 0.05,
    ):
        """
        Click at position. If x/y not given, clicks at current position.

        Args:
            x, y: Screen coordinates. Defaults to current cursor position.
            button: Mouse button to click.
            clicks: Number of clicks (2 for double-click).
            interval: Delay between multiple clicks.
        """
        if x is not None and y is not None:
            Mouse.move(x, y)
            time.sleep(0.01)  # Small delay for cursor to settle

        down_flag, up_flag = {
            "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
            "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
            "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
        }[button]

        for i in range(clicks):
            if i > 0:
                time.sleep(interval)

            down = INPUT()
            down.type = INPUT_MOUSE
            down.union.mi.dwFlags = down_flag

            up = INPUT()
            up.type = INPUT_MOUSE
            up.union.mi.dwFlags = up_flag

            _send_input(down, up)

    @staticmethod
    def double_click(x: Optional[int] = None, y: Optional[int] = None):
        """Double-click at position."""
        Mouse.click(x, y, clicks=2, interval=0.05)

    @staticmethod
    def scroll(amount: int, x: Optional[int] = None, y: Optional[int] = None):
        """
        Scroll the mouse wheel.

        Args:
            amount: Positive = scroll up, negative = scroll down.
            x, y: Position to scroll at. Defaults to current position.
        """
        if x is not None and y is not None:
            Mouse.move(x, y)
            time.sleep(0.01)

        inp = INPUT()
        inp.type = INPUT_MOUSE
        inp.union.mi.mouseData = ctypes.wintypes.DWORD(amount * 120)
        inp.union.mi.dwFlags = MOUSEEVENTF_WHEEL
        _send_input(inp)

    @staticmethod
    def drag(
        start_x: int, start_y: int, end_x: int, end_y: int,
        button: Literal["left", "right"] = "left",
        duration: float = 0.3, steps: int = 20,
    ):
        """
        Drag from one point to another.

        Args:
            start_x, start_y: Starting coordinates.
            end_x, end_y: Ending coordinates.
            button: Mouse button to hold during drag.
            duration: Total drag duration in seconds.
            steps: Number of intermediate positions.
        """
        if steps < 1:
            raise ValueError(f"steps must be >= 1 (got {steps})")
        if duration < 0:
            raise ValueError(f"duration must be >= 0 (got {duration})")

        down_flag, up_flag = {
            "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
            "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        }[button]

        Mouse.move(start_x, start_y)
        time.sleep(0.05)

        # Press down
        down = INPUT()
        down.type = INPUT_MOUSE
        down.union.mi.dwFlags = down_flag
        _send_input(down)

        # Always release the button, even if a move fails partway through.
        # Leaving it held would make every later click behave as a drag.
        try:
            step_delay = duration / steps
            for i in range(1, steps + 1):
                frac = i / steps
                cx = int(start_x + (end_x - start_x) * frac)
                cy = int(start_y + (end_y - start_y) * frac)
                Mouse.move(cx, cy)
                time.sleep(step_delay)
        finally:
            up = INPUT()
            up.type = INPUT_MOUSE
            up.union.mi.dwFlags = up_flag
            _send_input(up)


class Keyboard:
    """Keyboard input simulation."""

    @staticmethod
    def _key_down(vk: int):
        """Press a key down."""
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.union.ki.wVk = vk
        _send_input(inp)

    @staticmethod
    def _key_up(vk: int):
        """Release a key."""
        inp = INPUT()
        inp.type = INPUT_KEYBOARD
        inp.union.ki.wVk = vk
        inp.union.ki.dwFlags = KEYEVENTF_KEYUP
        _send_input(inp)

    @staticmethod
    def _type_char(char: str):
        """
        Type a single character as one or more UTF-16 code units.

        Characters outside the Basic Multilingual Plane (emoji, rarer CJK) are
        two UTF-16 code units. `wScan` is a 16-bit field, so sending `ord(char)`
        directly truncated them into an unrelated character. The surrogate pair
        is sent as one batch so the halves cannot be separated.
        """
        events = []
        encoded = char.encode("utf-16-le")
        for i in range(0, len(encoded), 2):
            unit = encoded[i] | (encoded[i + 1] << 8)

            down = INPUT()
            down.type = INPUT_KEYBOARD
            down.union.ki.wScan = unit
            down.union.ki.dwFlags = KEYEVENTF_UNICODE

            up = INPUT()
            up.type = INPUT_KEYBOARD
            up.union.ki.wScan = unit
            up.union.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP

            events.extend((down, up))

        if events:
            _send_input(*events)

    @staticmethod
    def type_text(text: str, delay: float = 0.02):
        """
        Type text character by character using unicode events.

        Args:
            text: The text to type.
            delay: Delay between each character.
        """
        for char in text:
            Keyboard._type_char(char)
            if delay > 0:
                time.sleep(delay)

    @staticmethod
    def press(key: str):
        """
        Press a key or key combination.

        Supports combos like 'Ctrl+C', 'Alt+Tab', 'Ctrl+Shift+S'.

        Args:
            key: Key name or combo string (e.g., 'Enter', 'Ctrl+A').
        """
        parts = [p.strip().lower() for p in key.split("+") if p.strip()]
        if not parts:
            raise ValueError(f"Empty key specification: {key!r}")

        modifiers = [p for p in parts if p in MODIFIER_KEYS]
        main_keys = [p for p in parts if p not in MODIFIER_KEYS]

        unknown = [k for k in main_keys if k not in VK_CODES]
        if unknown:
            raise ValueError(
                f"Unknown key(s): {', '.join(unknown)}. "
                f"Known keys: {', '.join(sorted(VK_CODES))}"
            )

        # Press modifiers down
        pressed = []
        try:
            for mod in modifiers:
                Keyboard._key_down(VK_CODES[mod])
                pressed.append(mod)

            # Press and release main keys
            for mk in main_keys:
                vk = VK_CODES[mk]
                Keyboard._key_down(vk)
                time.sleep(0.01)
                Keyboard._key_up(vk)
        finally:
            # Release modifiers in reverse, even on failure. A stuck Ctrl or Alt
            # would corrupt every subsequent keystroke sent to the desktop.
            for mod in reversed(pressed):
                Keyboard._key_up(VK_CODES[mod])

    @staticmethod
    def hotkey(*keys: str):
        """
        Press a hotkey combination.

        Args:
            keys: Keys to press together, e.g. hotkey('ctrl', 'c')
        """
        combo = "+".join(keys)
        Keyboard.press(combo)
