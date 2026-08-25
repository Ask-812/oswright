"""
Screen capture module - captures screenshots and screen regions.
Analogous to Playwright's page screenshot functionality.
"""

import logging
import threading
from typing import Optional

import mss
import mss.tools
from PIL import Image

from oswright._dpi import ensure_dpi_aware

logger = logging.getLogger(__name__)

ensure_dpi_aware()


class ScreenCapture:
    """
    Handles screen capture operations.

    `mss` instances are not thread-safe, so one is created lazily per thread.
    This matters because the MCP server runs synchronous tools in a thread pool,
    meaning a single ScreenCapture object is shared across many threads.
    """

    def __init__(self):
        self._local = threading.local()
        self._instances: list = []
        self._lock = threading.Lock()
        self._closed = False
        # Grab one eagerly so an unusable display fails fast, at construction
        # time, with an actionable message rather than on first screenshot.
        self._get_sct()

    def _get_sct(self) -> "mss.base.MSSBase":
        """Return this thread's mss instance, creating it on first use."""
        if self._closed:
            raise RuntimeError("ScreenCapture has been closed")

        sct = getattr(self._local, "sct", None)
        if sct is not None:
            return sct

        try:
            # mss.mss is deprecated in favour of mss.MSS in newer releases.
            factory = getattr(mss, "MSS", None) or mss.mss
            sct = factory()
        except Exception as e:
            raise RuntimeError(
                "Failed to initialize screen capture. "
                "Ensure a display server is available (X11/Wayland on Linux, "
                "screen recording permissions on macOS). "
                f"Error: {e}"
            ) from e

        self._local.sct = sct
        with self._lock:
            self._instances.append(sct)
        logger.debug("Screen capture initialized (%d monitors)", len(sct.monitors) - 1)
        return sct

    def _monitor_area(self, monitor: int) -> dict:
        """Look up a monitor's area, with a clear error for bad indices."""
        sct = self._get_sct()
        monitors = sct.monitors
        if not -len(monitors) <= monitor < len(monitors):
            raise ValueError(
                f"Invalid monitor index {monitor}. Valid values are 0 "
                f"(all monitors combined) through {len(monitors) - 1}."
            )
        return monitors[monitor]

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
                    Coordinates are absolute virtual-screen coordinates.
            monitor: Monitor index (0 = all monitors combined, 1 = primary, etc.)
                     Ignored when `region` is given.

        Returns:
            PIL Image of the captured screen.
        """
        if region:
            area = {
                "left": region["left"],
                "top": region["top"],
                "width": region["width"],
                "height": region["height"],
            }
        else:
            area = self._monitor_area(monitor)

        sct_img = self._get_sct().grab(area)
        img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")

        if path:
            img.save(path)

        return img

    def get_offset(self, region: Optional[dict] = None, monitor: int = 0) -> tuple[int, int]:
        """
        Get the absolute screen coordinate that pixel (0, 0) of a capture maps to.

        Screenshots are indexed from their own top-left corner, but the mouse is
        driven in absolute virtual-screen coordinates. Those differ whenever a
        region is used, and also for monitor 0 on multi-monitor setups where a
        secondary display sits above or to the left of the primary one (giving
        the virtual screen a negative origin).

        Add this offset to any coordinate derived from a screenshot before
        clicking it.

        Returns:
            (x_offset, y_offset) to add to image-relative coordinates.
        """
        if region:
            return (region["left"], region["top"])
        area = self._monitor_area(monitor)
        return (area["left"], area["top"])

    def get_screen_size(self, monitor: int = 0) -> dict:
        """Get the size and origin of a monitor."""
        mon = self._monitor_area(monitor)
        return {
            "left": mon["left"],
            "top": mon["top"],
            "width": mon["width"],
            "height": mon["height"],
        }

    def get_monitor_count(self) -> int:
        """Get the number of monitors (excluding the 'all' virtual monitor)."""
        return len(self._get_sct().monitors) - 1

    def close(self):
        """Release resources held by every thread that used this capture."""
        with self._lock:
            self._closed = True
            instances, self._instances = self._instances, []

        for sct in instances:
            try:
                sct.close()
            except Exception:  # pragma: no cover - best-effort cleanup
                logger.debug("Failed to close an mss instance", exc_info=True)

        self._local = threading.local()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
