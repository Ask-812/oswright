"""
Screen module - the main interaction surface.
Analogous to Playwright's Page object.
"""

import logging
import time
from collections.abc import Callable
from typing import Literal, Optional

from PIL import Image

from oswright.capture import ScreenCapture
from oswright.detect import ElementMatch, ImageMatcher, OCREngine
from oswright.input import Keyboard, Mouse
from oswright.locator import Locator, OSWrightError

logger = logging.getLogger(__name__)


class Screen:
    """
    Represents the OS screen - analogous to Playwright's Page.

    Provides methods to interact with the screen: find elements,
    click, type, take screenshots, and more.

    Usage:
        screen = Screen()
        screen.click(text="Start")
        screen.type_text("Hello World")
        screen.screenshot("desktop.png")
    """

    def __init__(
        self,
        monitor: int = 0,
        ocr_languages: list[str] = None,
        timeout: float = 10.0,
        poll_interval: float = 0.5,
        capture: Optional[ScreenCapture] = None,
        ocr: Optional[OCREngine] = None,
        ocr_provider: Optional[Callable[[], OCREngine]] = None,
    ):
        """
        Args:
            monitor: Monitor index (0 = all monitors combined, 1 = primary).
            ocr_languages: Languages for OCR (default: ['en']).
            timeout: Default timeout in seconds for auto-wait operations.
            poll_interval: How often to poll for elements during a wait.
            capture: Share an existing ScreenCapture instead of creating one.
            ocr: Share an existing OCREngine instead of creating one.
            ocr_provider: Callable returning a shared OCREngine, invoked on first
                use. Lets an owner share one engine without building it eagerly.
        """
        self._monitor = monitor
        self._timeout = timeout
        self._poll_interval = poll_interval
        self._ocr_languages = ocr_languages

        self._owns_capture = capture is None
        self._capture = capture or ScreenCapture()

        # The OCR engine is created on first use. Constructing it eagerly would
        # load EasyOCR (and therefore torch) even for a script that only ever
        # takes screenshots or uses the accessibility tree.
        self._ocr = ocr
        self._ocr_provider = ocr_provider

    @property
    def ocr(self) -> OCREngine:
        """The OCR engine, created on first use."""
        if self._ocr is None:
            if self._ocr_provider is not None:
                self._ocr = self._ocr_provider()
            else:
                self._ocr = OCREngine(languages=self._ocr_languages)
        return self._ocr

    def _offset(self, region: Optional[dict] = None) -> tuple[int, int]:
        """Absolute screen coordinate of pixel (0, 0) of a capture."""
        return self._capture.get_offset(region=region, monitor=self._monitor)

    def _to_screen_coords(
        self, matches: list[ElementMatch], region: Optional[dict] = None
    ) -> list[ElementMatch]:
        """Translate image-relative matches into absolute screen coordinates."""
        dx, dy = self._offset(region)
        return [m.offset(dx, dy) for m in matches]

    # --- Locator factory (Playwright's primary pattern) ---

    def locator(
        self,
        text: Optional[str] = None,
        image: Optional[str] = None,
        exact: bool = False,
        timeout: Optional[float] = None,
        region: Optional[dict] = None,
    ) -> Locator:
        """
        Create a locator to find an element on screen.

        Args:
            text: Find element by visible text (OCR).
            image: Find element by template image file path.
            exact: Require exact text match instead of substring.
            timeout: Override default timeout for this locator.
            region: Restrict search to a screen region {left, top, width, height}.

        Returns:
            A Locator instance (lazy - searches only on action).
        """
        return Locator(
            capture=self._capture,
            # Passed lazily so an image-only locator never constructs an OCR
            # engine (or fails on a machine with no OCR backend installed).
            ocr_provider=lambda: self.ocr,
            text=text,
            image=image,
            exact=exact,
            timeout=timeout if timeout is not None else self._timeout,
            poll_interval=self._poll_interval,
            region=region,
            monitor=self._monitor,
        )

    def get_by_text(self, text: str, exact: bool = False) -> Locator:
        """Shorthand for locator(text=...). Mirrors Playwright's get_by_text."""
        return self.locator(text=text, exact=exact)

    def get_by_image(self, image_path: str) -> Locator:
        """Shorthand for locator(image=...). Find element by template image."""
        return self.locator(image=image_path)

    # --- Direct actions (convenience shortcuts) ---

    @staticmethod
    def _check_coords(x: Optional[int], y: Optional[int]):
        """Reject a half-specified coordinate pair instead of silently ignoring it."""
        if (x is None) != (y is None):
            raise OSWrightError(
                "Both x and y must be provided together (got "
                f"x={x!r}, y={y!r})."
            )

    def click(
        self,
        text: Optional[str] = None,
        image: Optional[str] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: Literal["left", "right", "middle"] = "left",
        clicks: int = 1,
        timeout: Optional[float] = None,
    ):
        """
        Click on screen. Can target by text, image, or coordinates.

        Args:
            text: Click element containing this text.
            image: Click element matching this template image.
            x, y: Click at exact coordinates (no element search).
            button: Mouse button.
            clicks: Number of clicks.
            timeout: Override default timeout.
        """
        self._check_coords(x, y)
        if x is not None and y is not None:
            Mouse.click(x, y, button=button, clicks=clicks)
        elif text or image:
            self.locator(text=text, image=image, timeout=timeout).click(
                button=button, clicks=clicks
            )
        else:
            Mouse.click(button=button, clicks=clicks)

    def double_click(
        self,
        text: Optional[str] = None,
        image: Optional[str] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        timeout: Optional[float] = None,
    ):
        """Double-click on screen."""
        self._check_coords(x, y)
        if x is not None and y is not None:
            Mouse.double_click(x, y)
        elif text or image:
            self.locator(text=text, image=image, timeout=timeout).double_click()
        else:
            Mouse.double_click()

    def right_click(
        self,
        text: Optional[str] = None,
        image: Optional[str] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        timeout: Optional[float] = None,
    ):
        """Right-click on screen."""
        self.click(text=text, image=image, x=x, y=y, button="right", timeout=timeout)

    def type_text(self, text: str, delay: float = 0.02):
        """Type text using the keyboard (no element targeting)."""
        Keyboard.type_text(text, delay=delay)

    def press(self, key: str):
        """Press a key or key combination, e.g. 'Enter', 'Ctrl+S'."""
        Keyboard.press(key)

    def hotkey(self, *keys: str):
        """Press a hotkey combination, e.g. hotkey('ctrl', 'c')."""
        Keyboard.hotkey(*keys)

    def fill(
        self,
        text: str,
        target_text: Optional[str] = None,
        target_image: Optional[str] = None,
        timeout: Optional[float] = None,
    ):
        """
        Click a target element and fill it with text (clears existing content).

        Args:
            text: Text to type.
            target_text: Find target by text.
            target_image: Find target by image.
            timeout: Override default timeout.
        """
        if target_text or target_image:
            self.locator(text=target_text, image=target_image, timeout=timeout).fill(text)
        else:
            Keyboard.press("Ctrl+A")
            time.sleep(0.02)
            Keyboard.type_text(text)

    def scroll(
        self, amount: int,
        x: Optional[int] = None, y: Optional[int] = None,
    ):
        """Scroll the mouse wheel at a position."""
        self._check_coords(x, y)
        Mouse.scroll(amount, x, y)

    def drag(
        self, start_x: int, start_y: int, end_x: int, end_y: int,
        duration: float = 0.3,
    ):
        """Drag from one point to another."""
        Mouse.drag(start_x, start_y, end_x, end_y, duration=duration)

    def move(self, x: int, y: int):
        """Move mouse to a position."""
        Mouse.move(x, y)

    # --- Screen reading ---

    def screenshot(self, path: Optional[str] = None, region: Optional[dict] = None) -> Image.Image:
        """Take a screenshot of the screen."""
        return self._capture.screenshot(path=path, region=region, monitor=self._monitor)

    def read_text(self, region: Optional[dict] = None) -> list[ElementMatch]:
        """
        Read all visible text on screen using OCR.

        Coordinates are absolute screen coordinates, ready to click.
        """
        screenshot = self._capture.screenshot(region=region, monitor=self._monitor)
        return self._to_screen_coords(self.ocr.read_all(screenshot), region)

    def find_text(self, text: str, exact: bool = False, region: Optional[dict] = None) -> list[ElementMatch]:
        """
        Find all occurrences of text on screen.

        Coordinates are absolute screen coordinates, ready to click.
        """
        screenshot = self._capture.screenshot(region=region, monitor=self._monitor)
        return self._to_screen_coords(self.ocr.find_text(screenshot, text, exact=exact), region)

    def find_image(
        self, template_path: str, threshold: float = 0.8, region: Optional[dict] = None
    ) -> list[ElementMatch]:
        """
        Find all occurrences of a template image on screen.

        Coordinates are absolute screen coordinates, ready to click.
        """
        screenshot = self._capture.screenshot(region=region, monitor=self._monitor)
        matches = ImageMatcher.find_image(screenshot, template_path, threshold=threshold)
        return self._to_screen_coords(matches, region)

    # --- Waiting ---

    def wait_for_text(
        self, text: str, timeout: Optional[float] = None, exact: bool = False,
    ) -> ElementMatch:
        """Wait for text to appear on screen."""
        return self.locator(text=text, exact=exact, timeout=timeout).wait_for(state="visible")

    def wait_for_image(
        self, image_path: str, timeout: Optional[float] = None,
    ) -> ElementMatch:
        """Wait for an image to appear on screen."""
        return self.locator(image=image_path, timeout=timeout).wait_for(state="visible")

    def wait_for_text_gone(
        self, text: str, timeout: Optional[float] = None, exact: bool = False,
    ):
        """Wait for text to disappear from screen."""
        self.locator(text=text, exact=exact, timeout=timeout).wait_for(state="hidden")

    def pause(self, seconds: float):
        """Pause execution for a duration (use sparingly, prefer wait_for)."""
        time.sleep(seconds)

    # --- Screen info ---

    def get_size(self) -> dict:
        """Get screen dimensions."""
        return self._capture.get_screen_size(monitor=self._monitor)

    def get_mouse_position(self) -> tuple[int, int]:
        """Get current mouse cursor position."""
        return Mouse.get_position()

    def close(self):
        """Release resources owned by this Screen."""
        if self._owns_capture:
            self._capture.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
