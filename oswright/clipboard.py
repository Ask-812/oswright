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


def get_text() -> Optional[str]:
    """Get text from the system clipboard."""
    if _SYSTEM == "Windows":
        import ctypes
        import ctypes.wintypes
        CF_UNICODETEXT = 13
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        user32.GetClipboardData.restype = ctypes.c_void_p

        if not user32.OpenClipboard(0):
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
        try:
            return subprocess.check_output(["pbpaste"], text=True, timeout=5)
        except subprocess.SubprocessError:
            return None

    elif _SYSTEM == "Linux":
        for cmd in [["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"]]:
            try:
                return subprocess.check_output(cmd, text=True, timeout=5)
            except (subprocess.SubprocessError, FileNotFoundError):
                continue
        return None

    return None


def set_text(text: str) -> bool:
    """Set text to the system clipboard."""
    if _SYSTEM == "Windows":
        import ctypes
        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        kernel32.GlobalAlloc.restype = ctypes.c_void_p
        kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]

        encoded = text.encode("utf-16-le") + b"\x00\x00"
        if not user32.OpenClipboard(0):
            return False
        try:
            user32.EmptyClipboard()
            handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
            if not handle:
                return False
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return False
            ctypes.memmove(ptr, encoded, len(encoded))
            kernel32.GlobalUnlock(handle)
            user32.SetClipboardData(CF_UNICODETEXT, handle)
            return True
        finally:
            user32.CloseClipboard()

    elif _SYSTEM == "Darwin":
        try:
            proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE, timeout=5)
            proc.communicate(text.encode("utf-8"))
            return proc.returncode == 0
        except subprocess.SubprocessError:
            return False

    elif _SYSTEM == "Linux":
        for cmd in [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]:
            try:
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
                proc.communicate(text.encode("utf-8"), timeout=5)
                if proc.returncode == 0:
                    return True
            except (subprocess.SubprocessError, FileNotFoundError):
                continue
        return False

    return False
