"""
OSWright - Playwright-like automation framework for the operating system.

Usage:
    from oswright import OSWright

    with OSWright() as ow:
        screen = ow.screen()
        screen.click(text="Start")
        screen.type_text("Hello World")
        screen.screenshot("output.png")
"""

from oswright._version import __version__
from oswright.core import OSWright
from oswright.locator import ElementNotFoundError, Locator, OSWrightError, TimeoutError
from oswright.screen import Screen

__all__ = [
    "__version__",
    "OSWright",
    "Screen",
    "Locator",
    "OSWrightError",
    "ElementNotFoundError",
    "TimeoutError",
]
