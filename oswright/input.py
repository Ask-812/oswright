"""
Input simulation module - mouse and keyboard control.
Analogous to Playwright's click(), type(), press() methods.

Platform-agnostic dispatcher that loads the appropriate backend:
  - Windows: Win32 API (SendInput) for precise, dependency-free input.
  - Linux/macOS: pynput for cross-platform support.
"""

import logging
import platform

logger = logging.getLogger(__name__)

_SYSTEM = platform.system()

if _SYSTEM == "Windows":
    from oswright._input_windows import Keyboard, Mouse  # noqa: F401
    logger.debug("Using Windows (Win32) input backend")
else:
    try:
        from oswright._input_pynput import Keyboard, Mouse  # noqa: F401
        logger.debug("Using pynput input backend for %s", _SYSTEM)
    except ImportError:
        raise ImportError(
            f"oswright requires 'pynput' on {_SYSTEM}. "
            "Install it with: pip install pynput"
        ) from None

__all__ = ["Mouse", "Keyboard"]
