"""
Screen capture module - captures screenshots and screen regions.
Analogous to Playwright's page screenshot functionality.
"""

import logging
from typing import Optional

import mss
import mss.tools
from PIL import Image

logger = logging.getLogger(__name__)


class ScreenCapture:
    """Handles screen capture operations."""

    def __init__(self):
        try:
            self._sct = mss.mss()
            logger.debug("Screen capture initialized (%d monitors)", len(self._sct.monitors) - 1)
        except Exception as e:
            raise RuntimeError(
                "Failed to initialize screen capture. "
                "Ensure a display server is available (X11/Wayland on Linux, "
                "screen recording permissions on macOS). "
                f"Error: {e}"
            ) from e

    def screenshot(
        self,
        path: Optional[str] = None,
        region: Optional[dict] = None,
        monitor: int = 0,
    ) -> Image.Image:
        """
        Capture a screenshot of the entire screen or a specific region.

        Args:
            path: File path to save the screenshot. If None, returns PIL Image.
            region: Dict with keys 'left', 'top', 'width', 'height' for a sub-region.
            monitor: Monitor index (0 = all monitors combined, 1 = primary, etc.)

        Returns:
            PIL Image of the captured screen.
        """
        if region:
            monitor_area = {
                "left": region["left"],
                "top": region["top"],
                "width": region["width"],
                "height": region["height"],
            }
        else:
            monitor_area = self._sct.monitors[monitor]

        sct_img = self._sct.grab(monitor_area)
        img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

        if path:
            img.save(path)

        return img

    def get_screen_size(self, monitor: int = 0) -> dict:
        """Get the size of a monitor."""
        mon = self._sct.monitors[monitor]
        return {
            "left": mon["left"],
            "top": mon["top"],
            "width": mon["width"],
            "height": mon["height"],
        }

    def get_monitor_count(self) -> int:
        """Get the number of monitors (excluding the 'all' virtual monitor)."""
        return len(self._sct.monitors) - 1

    def close(self):
        """Release resources."""
        self._sct.close()
