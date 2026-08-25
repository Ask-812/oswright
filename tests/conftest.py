"""
Shared pytest fixtures.

The end-to-end tests drive the real desktop. They are skipped automatically
when no display is available (CI containers, headless servers) so that
`pytest` works everywhere without extra flags.
"""

import pytest


@pytest.fixture(scope="session")
def capture():
    """A ScreenCapture, or skip the test if this machine has no display."""
    from oswright.capture import ScreenCapture

    try:
        cap = ScreenCapture()
    except RuntimeError as e:
        pytest.skip(f"No display available for screen capture: {e}")

    yield cap
    cap.close()


@pytest.fixture(scope="session")
def screenshot(capture):
    """One screenshot of the live desktop, shared by the e2e tests."""
    return capture.screenshot()


@pytest.fixture(scope="session")
def ocr():
    """An OCR engine, or skip if no OCR backend is installed."""
    from oswright.detect import OCREngine

    try:
        return OCREngine()
    except ImportError as e:
        pytest.skip(f"No OCR backend available: {e}")
