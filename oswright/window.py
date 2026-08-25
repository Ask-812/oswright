"""
Window management module - list, focus, and capture specific windows.

Provides cross-platform window targeting so actions can be scoped to
a specific application instead of the full screen.
"""

import logging
import platform
from dataclasses import dataclass
from typing import Optional

from oswright._dpi import ensure_dpi_aware

logger = logging.getLogger(__name__)

_SYSTEM = platform.system()

# Must happen before any window rect is queried, or a scaled display reports
# logical pixels that will not match screenshot pixels.
ensure_dpi_aware()


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
    is_foreground: bool = False


if _SYSTEM == "Windows":
    import ctypes
    import ctypes.wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    # Constants
    SW_MINIMIZE = 6
    SW_RESTORE = 9
    WM_CLOSE = 0x0010
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    GW_OWNER = 4

    SM_XVIRTUALSCREEN = 76
    SM_YVIRTUALSCREEN = 77
    SM_CXVIRTUALSCREEN = 78
    SM_CYVIRTUALSCREEN = 79

    HWND = ctypes.wintypes.HWND
    HANDLE = ctypes.wintypes.HANDLE
    LPARAM = ctypes.wintypes.LPARAM
    DWORD = ctypes.wintypes.DWORD
    BOOL = ctypes.wintypes.BOOL

    # Declaring argtypes/restype is not optional on 64-bit Windows: ctypes
    # defaults to a C int return, which silently truncates the upper 32 bits of
    # any returned HWND/HANDLE and yields an invalid handle.
    user32.GetForegroundWindow.restype = HWND
    user32.GetForegroundWindow.argtypes = []
    user32.GetWindow.restype = HWND
    user32.GetWindow.argtypes = [HWND, ctypes.wintypes.UINT]
    user32.IsWindowVisible.restype = BOOL
    user32.IsWindowVisible.argtypes = [HWND]
    user32.IsIconic.restype = BOOL
    user32.IsIconic.argtypes = [HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextLengthW.argtypes = [HWND]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [HWND, ctypes.wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowRect.restype = BOOL
    user32.GetWindowRect.argtypes = [HWND, ctypes.POINTER(ctypes.wintypes.RECT)]
    user32.GetWindowThreadProcessId.restype = DWORD
    user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(DWORD)]
    user32.SetForegroundWindow.restype = BOOL
    user32.SetForegroundWindow.argtypes = [HWND]
    user32.BringWindowToTop.restype = BOOL
    user32.BringWindowToTop.argtypes = [HWND]
    user32.ShowWindow.restype = BOOL
    user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
    user32.PostMessageW.restype = BOOL
    user32.PostMessageW.argtypes = [HWND, ctypes.wintypes.UINT, ctypes.wintypes.WPARAM, LPARAM]
    user32.AttachThreadInput.restype = BOOL
    user32.AttachThreadInput.argtypes = [DWORD, DWORD, BOOL]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]

    kernel32.OpenProcess.restype = HANDLE
    kernel32.OpenProcess.argtypes = [DWORD, BOOL, DWORD]
    kernel32.CloseHandle.restype = BOOL
    kernel32.CloseHandle.argtypes = [HANDLE]
    kernel32.QueryFullProcessImageNameW.restype = BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        HANDLE, DWORD, ctypes.wintypes.LPWSTR, ctypes.POINTER(DWORD)
    ]
    kernel32.GetCurrentThreadId.restype = DWORD
    kernel32.GetCurrentThreadId.argtypes = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(BOOL, HWND, LPARAM)
    user32.EnumWindows.restype = BOOL
    user32.EnumWindows.argtypes = [WNDENUMPROC, LPARAM]

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
            # Skip windows owned by other windows (tooltips, etc.).
            # Note: with restype=HWND, ctypes maps a NULL return to None, so
            # this must be a truthiness check rather than a `!= 0` comparison.
            if user32.GetWindow(hwnd, GW_OWNER):
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

        WNDENUMPROC_INSTANCE = WNDENUMPROC(enum_callback)
        user32.EnumWindows(WNDENUMPROC_INSTANCE, 0)
        return windows

    def _resolve_hwnd(title: Optional[str], handle: Optional[int]) -> Optional[int]:
        """Resolve a window handle from an explicit handle or a title substring."""
        if handle is not None:
            return handle
        if not title:
            return None
        wins = list_windows(title_filter=title)
        return wins[0].handle if wins else None

    def _window_info(hwnd: int) -> WindowInfo:
        """Build a WindowInfo for an existing handle."""
        rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))

        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, buf, 512)

        pid = DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

        return WindowInfo(
            handle=hwnd,
            title=buf.value,
            left=rect.left,
            top=rect.top,
            width=rect.right - rect.left,
            height=rect.bottom - rect.top,
            is_visible=bool(user32.IsWindowVisible(hwnd)),
            process_name=_get_process_name(pid.value),
            process_id=pid.value,
            is_foreground=(user32.GetForegroundWindow() or 0) == hwnd,
        )

    def _virtual_screen() -> tuple[int, int, int, int]:
        """The virtual desktop rect as (left, top, right, bottom)."""
        left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
        top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
        width = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
        height = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        return left, top, left + width, top + height

    def focus_window(title: Optional[str] = None, handle: Optional[int] = None) -> Optional[WindowInfo]:
        """
        Bring a window to the foreground.

        Windows refuses SetForegroundWindow from a process that does not own the
        current foreground window, so this temporarily attaches to the
        foreground thread's input queue — the standard workaround.
        """
        hwnd = _resolve_hwnd(title, handle)
        if hwnd is None:
            return None

        # Restore if minimized
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)

        if not user32.SetForegroundWindow(hwnd):
            # ctypes maps a NULL HWND return to None, hence the `or 0`.
            foreground = user32.GetForegroundWindow() or 0
            fg_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
            our_thread = kernel32.GetCurrentThreadId()

            attached = False
            if fg_thread and fg_thread != our_thread:
                attached = bool(user32.AttachThreadInput(our_thread, fg_thread, True))
            try:
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
            finally:
                if attached:
                    user32.AttachThreadInput(our_thread, fg_thread, False)

        info = _window_info(hwnd)
        if not info.is_foreground:
            logger.warning(
                "Could not bring window %s to the foreground; "
                "another application may be holding the foreground lock.", hwnd
            )
        return info

    def get_window_region(title: Optional[str] = None, handle: Optional[int] = None) -> Optional[dict]:
        """
        Get the screen region of a window for screenshot capture.

        Returns None if the window is minimized or has no on-screen area, since
        there is nothing to capture in that case.
        """
        hwnd = _resolve_hwnd(title, handle)
        if hwnd is None:
            return None

        # A minimized window reports an off-screen rect around (-32000, -32000).
        if user32.IsIconic(hwnd):
            logger.debug("Window %s is minimized; no capturable region", hwnd)
            return None

        rect = ctypes.wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None

        # Clip against the real virtual desktop, which can legitimately start at
        # a negative coordinate when a monitor sits left of or above the primary.
        # Clamping to zero instead would crop — or entirely lose — those windows.
        vl, vt, vr, vb = _virtual_screen()
        left = max(vl, rect.left)
        top = max(vt, rect.top)
        right = min(vr, rect.right)
        bottom = min(vb, rect.bottom)
        width = right - left
        height = bottom - top

        if width <= 0 or height <= 0:
            logger.debug("Window %s has no on-screen area (%dx%d)", hwnd, width, height)
            return None

        return {"left": left, "top": top, "width": width, "height": height}

    def minimize_window(title: Optional[str] = None, handle: Optional[int] = None) -> bool:
        """Minimize a window."""
        hwnd = _resolve_hwnd(title, handle)
        if hwnd is None:
            return False
        user32.ShowWindow(hwnd, SW_MINIMIZE)
        return True

    def close_window(title: Optional[str] = None, handle: Optional[int] = None) -> bool:
        """Send WM_CLOSE to a window."""
        hwnd = _resolve_hwnd(title, handle)
        if hwnd is None:
            return False
        return bool(user32.PostMessageW(hwnd, WM_CLOSE, 0, 0))

else:
    # Linux/macOS stub — basic support via subprocess
    import shutil
    import subprocess

    def list_windows(title_filter: Optional[str] = None) -> list[WindowInfo]:
        """List windows using wmctrl (Linux) or osascript (macOS)."""
        windows = []

        if _SYSTEM == "Linux" and shutil.which("wmctrl"):
            try:
                output = subprocess.check_output(
                    ["wmctrl", "-l", "-G"], text=True, timeout=5
                )
                for line in output.strip().split("\n"):
                    # Format: <id> <desktop> <x> <y> <w> <h> <host> <title...>
                    # That is 7 fixed fields, so split 7 times and the remainder
                    # is the title. Splitting 8 times would strip the title's
                    # first word and drop single-word titles entirely.
                    parts = line.split(None, 7)
                    if len(parts) < 7:
                        continue
                    hwnd_str, _desktop, x, y, w, h, _host = parts[:7]
                    title = parts[7] if len(parts) > 7 else ""

                    if title_filter and title_filter.lower() not in title.lower():
                        continue

                    try:
                        windows.append(WindowInfo(
                            handle=int(hwnd_str, 16),
                            title=title,
                            left=int(x), top=int(y),
                            width=int(w), height=int(h),
                            is_visible=True,
                        ))
                    except ValueError:
                        continue
            except (subprocess.SubprocessError, FileNotFoundError):
                logger.warning("wmctrl not available. Install: sudo apt install wmctrl")

        elif _SYSTEM == "Darwin":
            try:
                # Each coordinate is emitted as its own field. Coercing an
                # AppleScript list to text concatenates it without a separator
                # ("{10, 20} as text" -> "1020"), which is unparseable.
                script = '''
                tell application "System Events"
                    set windowList to ""
                    repeat with proc in (every process whose visible is true)
                        repeat with win in (every window of proc)
                            set p to position of win
                            set s to size of win
                            set windowList to windowList & name of proc & "|||" & name of win ¬
                                & "|||" & (item 1 of p) & "|||" & (item 2 of p) ¬
                                & "|||" & (item 1 of s) & "|||" & (item 2 of s) & linefeed
                        end repeat
                    end repeat
                end tell
                return windowList
                '''
                output = subprocess.check_output(
                    ["osascript", "-e", script], text=True, timeout=10
                )
                for line in output.strip().split("\n"):
                    parts = line.split("|||")
                    if len(parts) < 6:
                        continue
                    proc_name, win_title = parts[0], parts[1]
                    title = f"{proc_name} - {win_title}" if win_title else proc_name

                    if title_filter and title_filter.lower() not in title.lower():
                        continue

                    try:
                        x, y, w, h = (int(v) for v in parts[2:6])
                    except ValueError:
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
        if not title:
            return None

        if _SYSTEM == "Linux" and shutil.which("wmctrl"):
            try:
                subprocess.run(["wmctrl", "-a", title], timeout=5, check=True)
                wins = list_windows(title_filter=title)
                return wins[0] if wins else None
            except subprocess.SubprocessError:
                return None

        elif _SYSTEM == "Darwin":
            wins = list_windows(title_filter=title)
            if not wins:
                return None
            # Activate the owning process. Using the window title as an
            # application name does not work: list_windows reports titles as
            # "ProcessName - Window Title", which is not an app name.
            proc_name = wins[0].process_name or title
            try:
                subprocess.run(
                    ["osascript", "-e",
                     f'tell application "System Events" to set frontmost of '
                     f'process "{proc_name}" to true'],
                    timeout=5, check=True,
                )
                return wins[0]
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
