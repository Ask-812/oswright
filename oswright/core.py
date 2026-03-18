"""
Core OSWright module - the main entry point.
Analogous to Playwright's playwright object and Browser.
"""

from typing import Optional
import logging

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
        logger.debug(
            "OSWright initialized (timeout=%.1fs, poll=%.1fs)",
            timeout, poll_interval,
        )

    def screen(self, monitor: int = 0) -> Screen:
        """
        Get a Screen instance for the given monitor.

        Args:
            monitor: Monitor index (0 = all monitors, 1 = primary, etc.)

        Returns:
            Screen instance for interaction.
        """
        s = Screen(
            monitor=monitor,
            ocr_languages=self._ocr_languages,
            timeout=self._timeout,
            poll_interval=self._poll_interval,
        )
        self._screens.append(s)
        return s

    def close(self):
        """Release all resources."""
        for s in self._screens:
            s.close()
        self._screens.clear()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
