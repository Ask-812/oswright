"""
Windows OCR backend using Windows.Media.Ocr (WinRT).

~10x faster than EasyOCR, zero model download, built into Windows 10+.
Used automatically on Windows when available.
"""

import asyncio
import logging
import threading
from typing import Optional

from PIL import Image

logger = logging.getLogger(__name__)

try:
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import (
        BitmapAlphaMode,
        BitmapPixelFormat,
        SoftwareBitmap,
    )
    from winrt.windows.media.ocr import OcrEngine

    _WINOCR_AVAILABLE = True
except ImportError:
    _WINOCR_AVAILABLE = False


def is_available() -> bool:
    """Check if Windows OCR is available."""
    return _WINOCR_AVAILABLE


def _pil_to_software_bitmap(image: Image.Image) -> "SoftwareBitmap":
    """Convert a PIL Image to a WinRT SoftwareBitmap."""
    if image.mode != "RGBA":
        image = image.convert("RGBA")

    w, h = image.size
    pixel_data = image.tobytes()

    # Screenshots are opaque, so alpha is ignored rather than premultiplied.
    # PIL produces straight (non-premultiplied) alpha, so declaring
    # PREMULTIPLIED here would be a lie for any non-opaque input.
    bitmap = SoftwareBitmap(BitmapPixelFormat.RGBA8, w, h, BitmapAlphaMode.IGNORE)
    bitmap.copy_from_buffer(pixel_data)
    return bitmap


# A single dedicated event loop thread serves every OCR call. This keeps the
# WinRT/COM apartment alive across calls (re-initialising it per call is slow)
# and makes `recognize()` safe to call from any thread, including from inside
# a thread that is already running an event loop.
_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_lock = threading.Lock()


def _get_loop() -> asyncio.AbstractEventLoop:
    """Get (or lazily start) the dedicated OCR event loop."""
    global _loop
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=_loop.run_forever,
                name="oswright-winocr",
                daemon=True,
            )
            thread.start()
        return _loop


def _run_async(coro):
    """Run a coroutine on the dedicated OCR loop and block for its result."""
    loop = _get_loop()
    return asyncio.run_coroutine_threadsafe(coro, loop).result()


async def _recognize_async(image: Image.Image, language: str) -> list[dict]:
    """Run OCR recognition asynchronously."""
    lang = Language(language)
    engine = OcrEngine.try_create_from_language(lang)
    if engine is None:
        available = ", ".join(_available_languages()) or "none"
        raise RuntimeError(
            f"Windows OCR engine not available for language '{language}'. "
            f"Installed OCR languages: {available}. "
            f"Add a language pack in Windows Settings > Time & Language > Language."
        )

    bitmap = _pil_to_software_bitmap(image)
    result = await engine.recognize_async(bitmap)

    elements = []
    for line in result.lines:
        words = list(line.words)
        if not words:
            continue

        for word in words:
            rect = word.bounding_rect
            elements.append({
                "text": word.text,
                "left": int(rect.x),
                "top": int(rect.y),
                "width": int(rect.width),
                "height": int(rect.height),
                "level": "word",
            })

        # Also emit the full line, so multi-word phrases can be matched.
        # The bounding box spans every word rather than assuming the words are
        # ordered left-to-right (which is false for RTL scripts).
        rects = [w.bounding_rect for w in words]
        line_left = int(min(r.x for r in rects))
        line_top = int(min(r.y for r in rects))
        line_right = int(max(r.x + r.width for r in rects))
        line_bottom = int(max(r.y + r.height for r in rects))
        elements.append({
            "text": line.text,
            "left": line_left,
            "top": line_top,
            "width": line_right - line_left,
            "height": line_bottom - line_top,
            "level": "line",
        })

    return elements


def _available_languages() -> list[str]:
    """List the OCR languages Windows currently has installed."""
    if not _WINOCR_AVAILABLE:
        return []
    try:
        return [lang.language_tag for lang in OcrEngine.available_recognizer_languages]
    except Exception:  # pragma: no cover - depends on OS state
        return []


def recognize(image: Image.Image, language: str = "en") -> list[dict]:
    """
    Run Windows OCR on a PIL Image.

    Returns list of dicts with keys: text, left, top, width, height, level.
    """
    if not _WINOCR_AVAILABLE:
        raise RuntimeError(
            "Windows OCR bindings are not installed. Install them with: "
            "pip install winrt-Windows.Media.Ocr winrt-Windows.Globalization "
            "winrt-Windows.Graphics.Imaging"
        )
    return _run_async(_recognize_async(image, language))
