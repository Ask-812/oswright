"""
Clipboard module - cross-platform clipboard read/write.

Enables data transfer between the AI agent and desktop applications.
"""

import logging
import platform
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

_SYSTEM = platform.system()

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def _win32_api():
    """Bind the Win32 clipboard entry points with explicit signatures."""
    import ctypes
    import ctypes.wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    # Without explicit restypes, ctypes assumes a 32-bit int return and
    # truncates every handle/pointer on 64-bit Windows.
    user32.OpenClipboard.argtypes = [ctypes.wintypes.HWND]
    user32.OpenClipboard.restype = ctypes.wintypes.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = ctypes.wintypes.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = ctypes.wintypes.BOOL
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.wintypes.HANDLE
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.wintypes.HANDLE]
    user32.SetClipboardData.restype = ctypes.wintypes.HANDLE

    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.wintypes.HGLOBAL
    kernel32.GlobalFree.argtypes = [ctypes.wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = ctypes.wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [ctypes.wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype = ctypes.wintypes.BOOL

    return ctypes, user32, kernel32


def _run_capture(cmd: list[str]) -> Optional[str]:
    """Run a command and return stdout, or None if it is unavailable/fails."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=5, check=True
        )
        return result.stdout
    except (subprocess.SubprocessError, OSError):
        return None


def _run_feed(cmd: list[str], text: str) -> bool:
    """Feed text to a command's stdin. Returns True on a clean exit."""
    try:
        # subprocess.run handles the write, the timeout, and reaping the child.
        # Popen(timeout=...) is not valid — Popen has no timeout parameter, and
        # passing one raises TypeError before anything is ever copied.
        result = subprocess.run(
            cmd, input=text, text=True, timeout=5, capture_output=True
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def get_text() -> Optional[str]:
    """Get text from the system clipboard."""
    if _SYSTEM == "Windows":
        ctypes, user32, kernel32 = _win32_api()

        if not user32.OpenClipboard(None):
            logger.debug("Could not open the clipboard (another app may hold it)")
            return None
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return None
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return None
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    elif _SYSTEM == "Darwin":
        return _run_capture(["pbpaste"])

    elif _SYSTEM == "Linux":
        for cmd in (
            ["wl-paste", "--no-newline"],
            ["xclip", "-selection", "clipboard", "-o"],
            ["xsel", "--clipboard", "--output"],
        ):
            out = _run_capture(cmd)
            if out is not None:
                return out
        logger.warning(
            "No clipboard tool found. Install one of: wl-clipboard, xclip, xsel"
        )
        return None

    return None


def set_text(text: str) -> bool:
    """Set text to the system clipboard. Returns True on success."""
    if _SYSTEM == "Windows":
        ctypes, user32, kernel32 = _win32_api()

        encoded = text.encode("utf-16-le") + b"\x00\x00"

        if not user32.OpenClipboard(None):
            logger.debug("Could not open the clipboard (another app may hold it)")
            return False

        handle = None
        transferred = False
        try:
            if not user32.EmptyClipboard():
                return False

            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
            if not handle:
                return False

            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return False
            try:
                ctypes.memmove(ptr, encoded, len(encoded))
            finally:
                kernel32.GlobalUnlock(handle)

            # On success the clipboard takes ownership of the block, so it must
            # NOT be freed here. On failure ownership stays with us and the
            # allocation would otherwise leak for the life of the process.
            transferred = bool(user32.SetClipboardData(CF_UNICODETEXT, handle))
            return transferred
        finally:
            if handle and not transferred:
                kernel32.GlobalFree(handle)
            user32.CloseClipboard()

    elif _SYSTEM == "Darwin":
        return _run_feed(["pbcopy"], text)

    elif _SYSTEM == "Linux":
        for cmd in (
            ["wl-copy"],
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
        ):
            if _run_feed(cmd, text):
                return True
        logger.warning(
            "No clipboard tool found. Install one of: wl-clipboard, xclip, xsel"
        )
        return False

    return False
