"""
Process DPI awareness.

On a scaled display (125%, 150%, ...) Windows lies to DPI-unaware processes:
GetWindowRect and GetSystemMetrics report *logical* pixels (e.g. 1536x864)
while the framebuffer, and therefore every screenshot, is in *physical* pixels
(e.g. 1920x1080). Mixing the two makes clicks land in the wrong place.

`mss` happens to call SetProcessDPIAware() when it initialises, so the process
silently switched coordinate systems the first time a screenshot was taken.
That made window coordinates depend on import order. Declaring awareness once,
explicitly and up front, keeps every API in physical pixels from the start.

This must run before any window is created, so it is invoked at import time.
"""

import ctypes
import logging
import platform

logger = logging.getLogger(__name__)

_applied = False

# DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
_PER_MONITOR_AWARE_V2 = -4
# PROCESS_PER_MONITOR_DPI_AWARE
_PROCESS_PER_MONITOR_DPI_AWARE = 2


def ensure_dpi_aware() -> bool:
    """
    Make this process DPI aware. Idempotent, and a no-op off Windows.

    Returns True if the process is DPI aware afterwards.
    """
    global _applied
    if _applied or platform.system() != "Windows":
        return _applied

    # Newest API first; each is available on progressively older Windows.
    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(_PER_MONITOR_AWARE_V2)):
            _applied = True
            logger.debug("DPI awareness: per-monitor v2")
            return True
    except (AttributeError, OSError):
        pass

    try:
        # S_OK, or E_ACCESSDENIED when it was already set (also fine).
        hr = ctypes.windll.shcore.SetProcessDpiAwareness(_PROCESS_PER_MONITOR_DPI_AWARE)
        if hr in (0, 0x80070005):
            _applied = True
            logger.debug("DPI awareness: per-monitor (shcore)")
            return True
    except (AttributeError, OSError):
        pass

    try:
        if ctypes.windll.user32.SetProcessDPIAware():
            _applied = True
            logger.debug("DPI awareness: system")
            return True
    except (AttributeError, OSError):
        pass

    logger.warning(
        "Could not make the process DPI aware. On a scaled display, window "
        "coordinates may not line up with screenshot pixels."
    )
    return False
