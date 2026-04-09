"""
Window management module - list, focus, and capture specific windows.

Provides cross-platform window targeting so actions can be scoped to
a specific application instead of the full screen.
"""

import logging
import platform
from dataclasses import dataclass
from typing import Optional

from PIL import Image

logger = logging.getLogger(__name__)

_SYSTEM = platform.system()


@dataclass
class WindowInfo:
    """Information about an OS window."""
    handle: int
    title: str
    left: int
    top: int
    width: int
    height: int
    is_visible: bool
    process_name: Optional[str] = None
    process_id: Optional[int] = None


if _SYSTEM == "Windows":
    import ctypes
    import ctypes.wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi

    # Constants
    SW_RESTORE = 9
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    GW_OWNER = 4
    GA_ROOTOWNER = 3

    def _get_process_name(pid: int) -> Optional[str]:
        """Get process executable name from PID."""
        try:
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return None
            try:
                buf = ctypes.create_unicode_buffer(260)
                size = ctypes.wintypes.DWORD(260)
                if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                    path = buf.value
                    return path.rsplit("\\", 1)[-1]
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return None

    def list_windows(title_filter: Optional[str] = None) -> list[WindowInfo]:
        """List all visible top-level windows."""
        windows = []

        def enum_callback(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            # Skip windows owned by other windows (tooltips, etc.)
            if user32.GetWindow(hwnd, GW_OWNER) != 0:
                return True

            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True

            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value

            if title_filter and title_filter.lower() not in title.lower():
                return True

            rect = ctypes.wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))

            pid = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

            windows.append(WindowInfo(
                handle=hwnd,
                title=title,
                left=rect.left,
                top=rect.top,
                width=rect.right - rect.left,
                height=rect.bottom - rect.top,
                is_visible=True,
                process_name=_get_process_name(pid.value),
                process_id=pid.value,
            ))
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.c_long)
        user32.EnumWindows(WNDENUMPROC(enum_callback), 0)
        return windows

    def focus_window(title: Optional[str] = None, handle: Optional[int] = None) -> Optional[WindowInfo]:
        """Bring a window to the foreground."""
        hwnd = handle
        if hwnd is None and title:
            wins = list_windows(title_filter=title)
            if not wins:
                return None
            hwnd = wins[0].handle

        if hwnd is None:
            return None

        # Restore if minimized
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)

        user32.SetForegroundWindow(hwnd)

        # Get updated rect
        rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))

        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)

        return WindowInfo(
            handle=hwnd,
            title=buf.value,
            left=rect.left,
            top=rect.top,
            width=rect.right - rect.left,
            height=rect.bottom - rect.top,
            is_visible=True,
        )

    def get_window_region(title: Optional[str] = None, handle: Optional[int] = None) -> Optional[dict]:
        """Get the screen region of a window for screenshot capture."""
        hwnd = handle
        if hwnd is None and title:
            wins = list_windows(title_filter=title)
            if not wins:
                return None
            hwnd = wins[0].handle

        if hwnd is None:
            return None

        rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))

        # Clamp to non-negative (Windows shadow borders can give negative values)
        left = max(0, rect.left)
        top = max(0, rect.top)

        return {
            "left": left,
            "top": top,
            "width": rect.right - left,
            "height": rect.bottom - top,
        }

    def minimize_window(title: Optional[str] = None, handle: Optional[int] = None) -> bool:
        """Minimize a window."""
        hwnd = handle
        if hwnd is None and title:
            wins = list_windows(title_filter=title)
            if not wins:
                return False
            hwnd = wins[0].handle
        if hwnd is None:
            return False
        user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
        return True

    def close_window(title: Optional[str] = None, handle: Optional[int] = None) -> bool:
        """Send WM_CLOSE to a window."""
        WM_CLOSE = 0x0010
        hwnd = handle
        if hwnd is None and title:
            wins = list_windows(title_filter=title)
            if not wins:
                return False
            hwnd = wins[0].handle
        if hwnd is None:
            return False
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return True

else:
    # Linux/macOS stub — basic support via subprocess
    import subprocess
    import shutil

    def list_windows(title_filter: Optional[str] = None) -> list[WindowInfo]:
        """List windows using wmctrl (Linux) or osascript (macOS)."""
        windows = []

        if _SYSTEM == "Linux" and shutil.which("wmctrl"):
            try:
                output = subprocess.check_output(
                    ["wmctrl", "-l", "-G"], text=True, timeout=5
                )
                for line in output.strip().split("\n"):
                    parts = line.split(None, 8)
                    if len(parts) < 9:
                        continue
                    hwnd_str, _desktop, x, y, w, h, _host = parts[:7]
                    title = parts[8] if len(parts) > 8 else ""

                    if title_filter and title_filter.lower() not in title.lower():
                        continue

                    windows.append(WindowInfo(
                        handle=int(hwnd_str, 16),
                        title=title,
                        left=int(x), top=int(y),
                        width=int(w), height=int(h),
                        is_visible=True,
                    ))
            except (subprocess.SubprocessError, FileNotFoundError):
                logger.warning("wmctrl not available. Install: sudo apt install wmctrl")

        elif _SYSTEM == "Darwin":
            try:
                script = '''
                tell application "System Events"
                    set windowList to ""
                    repeat with proc in (every process whose visible is true)
                        repeat with win in (every window of proc)
                            set windowList to windowList & name of proc & "|||" & name of win & "|||" & (position of win as text) & "|||" & (size of win as text) & linefeed
                        end repeat
                    end repeat
                end tell
                return windowList
                '''
                output = subprocess.check_output(
                    ["osascript", "-e", script], text=True, timeout=10
                )
                for line in output.strip().split("\n"):
                    if "|||" not in line:
                        continue
                    parts = line.split("|||")
                    if len(parts) < 4:
                        continue
                    proc_name, win_title = parts[0], parts[1]
                    title = f"{proc_name} - {win_title}" if win_title else proc_name

                    if title_filter and title_filter.lower() not in title.lower():
                        continue

                    try:
                        pos = parts[2].split(", ")
                        size = parts[3].split(", ")
                        x, y = int(pos[0]), int(pos[1])
                        w, h = int(size[0]), int(size[1])
                    except (ValueError, IndexError):
                        x, y, w, h = 0, 0, 0, 0

                    windows.append(WindowInfo(
                        handle=0,
                        title=title,
                        left=x, top=y, width=w, height=h,
                        is_visible=True,
                        process_name=proc_name,
                    ))
            except (subprocess.SubprocessError, FileNotFoundError):
                logger.warning("osascript not available for window listing")

        return windows

    def focus_window(title: Optional[str] = None, handle: Optional[int] = None) -> Optional[WindowInfo]:
        """Focus a window by title."""
        if _SYSTEM == "Linux" and shutil.which("wmctrl") and title:
            try:
                subprocess.run(["wmctrl", "-a", title], timeout=5, check=True)
                wins = list_windows(title_filter=title)
                return wins[0] if wins else None
            except subprocess.SubprocessError:
                return None

        elif _SYSTEM == "Darwin" and title:
            try:
                script = f'tell application "{title}" to activate'
                subprocess.run(["osascript", "-e", script], timeout=5, check=True)
                wins = list_windows(title_filter=title)
                return wins[0] if wins else None
            except subprocess.SubprocessError:
                return None

        return None

    def get_window_region(title: Optional[str] = None, handle: Optional[int] = None) -> Optional[dict]:
        """Get window region for captures."""
        wins = list_windows(title_filter=title)
        if not wins:
            return None
        w = wins[0]
        return {"left": w.left, "top": w.top, "width": w.width, "height": w.height}

    def minimize_window(title: Optional[str] = None, handle: Optional[int] = None) -> bool:
        """Minimize a window."""
        if _SYSTEM == "Linux" and shutil.which("xdotool") and title:
            try:
                wid = subprocess.check_output(
                    ["xdotool", "search", "--name", title], text=True, timeout=5
                ).strip().split("\n")[0]
                subprocess.run(["xdotool", "windowminimize", wid], timeout=5)
                return True
            except subprocess.SubprocessError:
                return False
        return False

    def close_window(title: Optional[str] = None, handle: Optional[int] = None) -> bool:
        """Close a window."""
        if _SYSTEM == "Linux" and shutil.which("wmctrl") and title:
            try:
                subprocess.run(["wmctrl", "-c", title], timeout=5, check=True)
                return True
            except subprocess.SubprocessError:
                return False
        return False
