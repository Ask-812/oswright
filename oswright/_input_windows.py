"""
Windows input backend using Win32 API (SendInput, SetCursorPos, GetCursorPos).

Used automatically on Windows. No external dependencies beyond ctypes (stdlib).
"""

import time
from typing import Optional, Literal

import ctypes
import ctypes.wintypes

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


def _send_input(*inputs):
    """Send input events to the OS."""
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    ctypes.windll.user32.SendInput(n, arr, ctypes.sizeof(INPUT))


class Mouse:
    """Mouse input simulation."""

    @staticmethod
    def get_position() -> tuple[int, int]:
        """Get current cursor position."""
        point = ctypes.wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
        return (point.x, point.y)

    @staticmethod
    def move(x: int, y: int):
        """Move cursor to absolute screen coordinates."""
        ctypes.windll.user32.SetCursorPos(x, y)

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

        # Move gradually
        step_delay = duration / steps
        for i in range(1, steps + 1):
            frac = i / steps
            cx = int(start_x + (end_x - start_x) * frac)
            cy = int(start_y + (end_y - start_y) * frac)
            Mouse.move(cx, cy)
            time.sleep(step_delay)

        # Release
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
        """Type a single unicode character."""
        down = INPUT()
        down.type = INPUT_KEYBOARD
        down.union.ki.wScan = ord(char)
        down.union.ki.dwFlags = KEYEVENTF_UNICODE

        up = INPUT()
        up.type = INPUT_KEYBOARD
        up.union.ki.wScan = ord(char)
        up.union.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP

        _send_input(down, up)

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
        parts = [p.strip().lower() for p in key.split("+")]
        modifiers = [p for p in parts if p in MODIFIER_KEYS]
        main_keys = [p for p in parts if p not in MODIFIER_KEYS]

        # Press modifiers down
        for mod in modifiers:
            Keyboard._key_down(VK_CODES[mod])

        # Press and release main keys
        for mk in main_keys:
            vk = VK_CODES.get(mk)
            if vk is None:
                raise ValueError(f"Unknown key: {mk}")
            Keyboard._key_down(vk)
            time.sleep(0.01)
            Keyboard._key_up(vk)

        # Release modifiers in reverse
        for mod in reversed(modifiers):
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
