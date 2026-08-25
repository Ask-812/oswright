"""
Core OSWright module - the main entry point.
Analogous to Playwright's playwright object and Browser.
"""

import logging
from typing import Optional

from oswright.capture import ScreenCapture
from oswright.detect import OCREngine
from oswright.screen import Screen

logger = logging.getLogger(__name__)


class OSWright:
    """
    Main entry point for OSWright - OS-level automation framework.

    Analogous to Playwright's `playwright` and `browser` objects combined.

    Usage:
        # Context manager (recommended)
        with OSWright() as ow:
            screen = ow.screen()
            screen.click(text="Start")

        # Manual lifecycle
        ow = OSWright()
        screen = ow.screen()
        screen.click(text="Start")
        ow.close()
    """

    def __init__(
        self,
        ocr_languages: list[str] = None,
        timeout: float = 10.0,
        poll_interval: float = 0.5,
    ):
        """
        Initialize OSWright.

        Args:
            ocr_languages: Languages for OCR (default: ['en']).
            timeout: Default timeout in seconds for auto-wait operations.
            poll_interval: How often to poll for elements during wait.
        """
        self._ocr_languages = ocr_languages
        self._timeout = timeout
        self._poll_interval = poll_interval
        self._screens: list[Screen] = []
        self._capture: Optional[ScreenCapture] = None
        self._ocr: Optional[OCREngine] = None
        self._closed = False
        logger.debug(
            "OSWright initialized (timeout=%.1fs, poll=%.1fs)",
            timeout, poll_interval,
        )

    @property
    def capture(self) -> ScreenCapture:
        """The shared screen capture, created on first use."""
        if self._capture is None:
            self._capture = ScreenCapture()
        return self._capture

    @property
    def ocr(self) -> OCREngine:
        """
        The shared OCR engine, created on first use.

        Sharing matters: on Linux/macOS each EasyOCR reader loads its own copy
        of the models, so building one per Screen would waste seconds and
        hundreds of MB.
        """
        if self._ocr is None:
            self._ocr = OCREngine(languages=self._ocr_languages)
        return self._ocr

    def screen(self, monitor: int = 0) -> Screen:
        """
        Get a Screen instance for the given monitor.

        Args:
            monitor: Monitor index (0 = all monitors, 1 = primary, etc.)

        Returns:
            Screen instance for interaction.
        """
        if self._closed:
            raise RuntimeError("OSWright has been closed")

        s = Screen(
            monitor=monitor,
            ocr_languages=self._ocr_languages,
            timeout=self._timeout,
            poll_interval=self._poll_interval,
            capture=self.capture,
            # Passed as a callable, not a value: reading `self.ocr` here would
            # construct the engine immediately, which is what we want to avoid.
            ocr_provider=lambda: self.ocr,
        )
        self._screens.append(s)
        return s

    def close(self):
        """Release all resources."""
        self._closed = True
        for s in self._screens:
            s.close()
        self._screens.clear()

        if self._capture is not None:
            self._capture.close()
            self._capture = None
        self._ocr = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
