"""
Windows OCR backend using Windows.Media.Ocr (WinRT).

~10x faster than EasyOCR, zero model download, built into Windows 10+.
Used automatically on Windows when available.
"""

import asyncio
import logging
from typing import Optional

from PIL import Image

logger = logging.getLogger(__name__)

try:
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import (
        SoftwareBitmap,
        BitmapPixelFormat,
        BitmapAlphaMode,
    )
    from winrt.windows.storage.streams import (
        DataWriter,
        InMemoryRandomAccessStream,
    )

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

    bitmap = SoftwareBitmap(BitmapPixelFormat.RGBA8, w, h, BitmapAlphaMode.PREMULTIPLIED)
    bitmap.copy_from_buffer(pixel_data)
    return bitmap


def _run_async(coro):
    """Run an async function, handling event loop lifecycle."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # We're inside an existing event loop — create a new thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    else:
        return asyncio.run(coro)


async def _recognize_async(image: Image.Image, language: str) -> list[dict]:
    """Run OCR recognition asynchronously."""
    lang = Language(language)
    engine = OcrEngine.try_create_from_language(lang)
    if engine is None:
        raise RuntimeError(
            f"Windows OCR engine not available for language '{language}'. "
            f"Install the language pack in Windows Settings > Time & Language > Language."
        )

    bitmap = _pil_to_software_bitmap(image)
    result = await engine.recognize_async(bitmap)

    elements = []
    for line in result.lines:
        words_list = list(line.words)
        # Add each word
        for word in words_list:
            rect = word.bounding_rect
            elements.append({
                "text": word.text,
                "left": int(rect.x),
                "top": int(rect.y),
                "width": int(rect.width),
                "height": int(rect.height),
                "level": "word",
            })

        # Also add the full line text with combined bounding box
        if line.words:
            words_list = list(line.words)
            first_rect = words_list[0].bounding_rect
            last_rect = words_list[len(words_list) - 1].bounding_rect
            line_left = int(first_rect.x)
            line_top = int(min(w.bounding_rect.y for w in words_list))
            line_right = int(last_rect.x + last_rect.width)
            line_bottom = int(max(w.bounding_rect.y + w.bounding_rect.height for w in words_list))
            elements.append({
                "text": line.text,
                "left": line_left,
                "top": line_top,
                "width": line_right - line_left,
                "height": line_bottom - line_top,
                "level": "line",
            })

    return elements


def recognize(image: Image.Image, language: str = "en") -> list[dict]:
    """
    Run Windows OCR on a PIL Image.

    Returns list of dicts with keys: text, left, top, width, height.
    """
    return _run_async(_recognize_async(image, language))
