"""
Locator module - Playwright-style locators for OS elements.

Locators are lazy - they don't search until an action is performed.
They auto-wait for elements to appear before acting.
"""

import builtins
import logging
import time
from typing import Optional, Literal

from PIL import Image

from oswright.capture import ScreenCapture
from oswright.detect import OCREngine, ImageMatcher, ElementMatch
from oswright.input import Mouse, Keyboard

logger = logging.getLogger(__name__)


class OSWrightError(Exception):
    """Base exception for OSWright."""
    pass


class ElementNotFoundError(OSWrightError):
    """Raised when an element cannot be found on screen."""
    pass


class TimeoutError(OSWrightError, builtins.TimeoutError):
    """Raised when an operation times out. Also a builtins.TimeoutError."""
    pass


class Locator:
    """
    Playwright-style locator for OS screen elements.

    Locators are lazy and auto-wait. They describe HOW to find an element,
    and only search when an action (click, etc.) is performed.

    Usage:
        screen.locator(text="Save").click()
        screen.locator(image="icon.png").wait_for()
        screen.locator(text="Submit").expect().to_be_visible()
    """

    def __init__(
        self,
        capture: ScreenCapture,
        ocr: OCREngine,
        text: Optional[str] = None,
        image: Optional[str] = None,
        image_obj: Optional[Image.Image] = None,
        exact: bool = False,
        timeout: float = 10.0,
        poll_interval: float = 0.5,
        region: Optional[dict] = None,
        monitor: int = 0,
        nth: int = 0,
    ):
        self._capture = capture
        self._ocr = ocr
        self._text = text
        self._image = image
        self._image_obj = image_obj
        self._exact = exact
        self._timeout = timeout
        self._poll_interval = poll_interval
        self._region = region
        self._monitor = monitor
        self._nth = nth

    def _resolve(self, timeout: Optional[float] = None) -> ElementMatch:
        """
        Find the element on screen, auto-waiting until timeout.

        Returns the nth match (default: first/best match).
        """
        timeout = timeout if timeout is not None else self._timeout
        deadline = time.time() + timeout

        last_error = None
        while True:
            try:
                screenshot = self._capture.screenshot(
                    region=self._region, monitor=self._monitor
                )
                matches = self._find_matches(screenshot)

                if matches:
                    # Handle negative indices (e.g., last() uses nth(-1))
                    idx = self._nth
                    if idx < 0:
                        idx = len(matches) + idx
                    if 0 <= idx < len(matches):
                        match = matches[idx]

                        # Adjust coordinates if using a sub-region
                        if self._region:
                            match.x += self._region["left"]
                            match.y += self._region["top"]
                            match.left += self._region["left"]
                            match.top += self._region["top"]

                        return match

            except Exception as e:
                last_error = e

            if time.time() >= deadline:
                selector_desc = self._describe()
                logger.warning("Timeout %.1fs waiting for: %s", timeout, selector_desc)
                raise TimeoutError(
                    f"Timeout {timeout}s waiting for element: {selector_desc}. "
                    f"Last error: {last_error}"
                )

            time.sleep(self._poll_interval)

    def _find_matches(self, screenshot: Image.Image) -> list[ElementMatch]:
        """Run the actual search on a screenshot."""
        if self._text is not None:
            return self._ocr.find_text(screenshot, self._text, exact=self._exact)
        elif self._image is not None:
            return ImageMatcher.find_image(screenshot, self._image)
        elif self._image_obj is not None:
            return ImageMatcher.find_image_from_array(screenshot, self._image_obj)
        else:
            raise OSWrightError("Locator has no search criteria (text or image)")

    def _describe(self) -> str:
        """Human-readable description of this locator."""
        if self._text:
            return f'text="{self._text}"' + (" (exact)" if self._exact else "")
        elif self._image:
            return f'image="{self._image}"'
        return "unknown"

    # --- Actions (mirror Playwright's API) ---

    def click(
        self,
        button: Literal["left", "right", "middle"] = "left",
        clicks: int = 1,
        timeout: Optional[float] = None,
    ):
        """Find the element and click it."""
        match = self._resolve(timeout)
        Mouse.click(match.x, match.y, button=button, clicks=clicks)

    def double_click(self, timeout: Optional[float] = None):
        """Find the element and double-click it."""
        match = self._resolve(timeout)
        Mouse.double_click(match.x, match.y)

    def right_click(self, timeout: Optional[float] = None):
        """Find the element and right-click it."""
        self.click(button="right", timeout=timeout)

    def hover(self, timeout: Optional[float] = None):
        """Find the element and move the mouse over it."""
        match = self._resolve(timeout)
        Mouse.move(match.x, match.y)

    def fill(self, text: str, timeout: Optional[float] = None, clear: bool = True):
        """
        Click the element and type text into it.

        Args:
            text: Text to type.
            timeout: Override default timeout.
            clear: If True, select all and clear before typing.
        """
        self.click(timeout=timeout)
        time.sleep(0.05)
        if clear:
            Keyboard.press("Ctrl+A")
            time.sleep(0.02)
            Keyboard.press("Delete")
            time.sleep(0.02)
        Keyboard.type_text(text)

    def type_text(self, text: str, delay: float = 0.02, timeout: Optional[float] = None):
        """Click the element and type text with delay between characters."""
        self.click(timeout=timeout)
        time.sleep(0.05)
        Keyboard.type_text(text, delay=delay)

    def drag_to(
        self, target: "Locator",
        duration: float = 0.3,
        timeout: Optional[float] = None,
    ):
        """Drag this element to another element."""
        src = self._resolve(timeout)
        dst = target._resolve(timeout)
        Mouse.drag(src.x, src.y, dst.x, dst.y, duration=duration)

    def scroll(self, amount: int, timeout: Optional[float] = None):
        """Scroll at this element's position."""
        match = self._resolve(timeout)
        Mouse.scroll(amount, match.x, match.y)

    # --- Waiting ---

    def wait_for(
        self,
        state: Literal["visible", "hidden"] = "visible",
        timeout: Optional[float] = None,
    ) -> Optional[ElementMatch]:
        """
        Wait for the element to reach the specified state.

        Args:
            state: 'visible' waits for element to appear, 'hidden' waits for it to disappear.
            timeout: Override default timeout.
        """
        timeout = timeout if timeout is not None else self._timeout
        deadline = time.time() + timeout

        if state == "visible":
            return self._resolve(timeout)

        elif state == "hidden":
            while True:
                try:
                    screenshot = self._capture.screenshot(
                        region=self._region, monitor=self._monitor
                    )
                    matches = self._find_matches(screenshot)
                    if not matches or len(matches) <= self._nth:
                        return None  # Element is gone
                except Exception:
                    return None

                if time.time() >= deadline:
                    raise TimeoutError(
                        f"Timeout {timeout}s waiting for element to be hidden: "
                        f"{self._describe()}"
                    )
                time.sleep(self._poll_interval)

    def is_visible(self, timeout: float = 0) -> bool:
        """Check if the element is currently visible (non-waiting)."""
        try:
            self._resolve(timeout=timeout)
            return True
        except (TimeoutError, OSWrightError):
            return False

    # --- Info ---

    def bounding_box(self, timeout: Optional[float] = None) -> dict:
        """Get the bounding box of the element."""
        match = self._resolve(timeout)
        return {
            "x": match.left, "y": match.top,
            "width": match.width, "height": match.height,
        }

    def screenshot(self, path: str, timeout: Optional[float] = None) -> Image.Image:
        """Take a screenshot of just this element."""
        match = self._resolve(timeout)
        return self._capture.screenshot(
            path=path,
            region={
                "left": match.left, "top": match.top,
                "width": match.width, "height": match.height,
            },
        )

    def text_content(self, timeout: Optional[float] = None) -> Optional[str]:
        """Get the detected text content of this element."""
        match = self._resolve(timeout)
        return match.text

    # --- Chaining ---

    def first(self) -> "Locator":
        """Return a new locator targeting the first match."""
        return self.nth(0)

    def last(self) -> "Locator":
        """Return a new locator targeting the last match."""
        return self.nth(-1)

    def nth(self, index: int) -> "Locator":
        """Return a new locator targeting the nth match."""
        new = Locator(
            capture=self._capture, ocr=self._ocr,
            text=self._text, image=self._image, image_obj=self._image_obj,
            exact=self._exact, timeout=self._timeout,
            poll_interval=self._poll_interval, region=self._region,
            monitor=self._monitor, nth=index,
        )
        return new

    # --- Assertions ---

    def expect(self) -> "LocatorAssertions":
        """Return an assertions object for this locator (Playwright-style)."""
        return LocatorAssertions(self)

    def __repr__(self):
        return f"<Locator {self._describe()}>"


class LocatorAssertions:
    """
    Playwright-style assertions for locators.

    Usage:
        screen.locator(text="Welcome").expect().to_be_visible()
        screen.locator(text="Error").expect().not_to_be_visible()
        screen.locator(text="Hello").expect().to_have_text("Hello World")
    """

    def __init__(self, locator: Locator, timeout: float = 10.0):
        self._locator = locator
        self._timeout = timeout

    def to_be_visible(self, timeout: Optional[float] = None):
        """Assert the element is visible on screen."""
        timeout = timeout or self._timeout
        try:
            self._locator._resolve(timeout=timeout)
        except TimeoutError:
            raise AssertionError(
                f"Expected {self._locator._describe()} to be visible, "
                f"but it was not found within {timeout}s"
            )

    def not_to_be_visible(self, timeout: Optional[float] = None):
        """Assert the element is NOT visible on screen."""
        timeout = timeout or self._timeout
        try:
            self._locator.wait_for(state="hidden", timeout=timeout)
        except TimeoutError:
            raise AssertionError(
                f"Expected {self._locator._describe()} to be hidden, "
                f"but it was still visible after {timeout}s"
            )

    def to_have_text(self, expected: str, timeout: Optional[float] = None):
        """Assert the element contains the expected text."""
        timeout = timeout or self._timeout
        match = self._locator._resolve(timeout=timeout)
        if match.text is None or expected.lower() not in match.text.lower():
            raise AssertionError(
                f"Expected {self._locator._describe()} to have text '{expected}', "
                f"but got '{match.text}'"
            )

    def to_have_exact_text(self, expected: str, timeout: Optional[float] = None):
        """Assert the element has exactly the expected text."""
        timeout = timeout or self._timeout
        match = self._locator._resolve(timeout=timeout)
        if match.text != expected:
            raise AssertionError(
                f"Expected {self._locator._describe()} to have exact text '{expected}', "
                f"but got '{match.text}'"
            )
