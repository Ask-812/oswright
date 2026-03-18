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

from oswright.core import OSWright
from oswright.screen import Screen
from oswright.locator import Locator, OSWrightError, ElementNotFoundError, TimeoutError

__version__ = "0.1.0"
__all__ = [
    "OSWright",
    "Screen",
    "Locator",
    "OSWrightError",
    "ElementNotFoundError",
    "TimeoutError",
]
