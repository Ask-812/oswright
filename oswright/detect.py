"""
Element detection module - finds UI elements on screen using OCR and image matching.
Analogous to Playwright's selectors and locators.
"""

import logging
import os
import platform
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

# --- Detect available OCR backends ---
_SYSTEM = platform.system()
_OCR_BACKENDS: list[str] = []

# Prefer Windows OCR (instant, no model download)
if _SYSTEM == "Windows":
    try:
        from oswright import _ocr_windows
        if _ocr_windows.is_available():
            _OCR_BACKENDS.append("winocr")
    except ImportError:
        pass

# EasyOCR as universal fallback
try:
    import easyocr
    _OCR_BACKENDS.append("easyocr")
except ImportError:
    pass

_OCR_BACKEND = _OCR_BACKENDS[0] if _OCR_BACKENDS else None


@dataclass
class ElementMatch:
    """Represents a found element on screen."""

    x: int          # Center x coordinate
    y: int          # Center y coordinate
    left: int       # Bounding box left
    top: int        # Bounding box top
    width: int      # Bounding box width
    height: int     # Bounding box height
    confidence: float  # Match confidence 0-1
    text: Optional[str] = None  # OCR text if applicable
    method: str = ""  # How it was found

    @property
    def center(self) -> tuple[int, int]:
        return (self.x, self.y)

    @property
    def box(self) -> tuple[int, int, int, int]:
        """Returns (left, top, right, bottom)."""
        return (self.left, self.top, self.left + self.width, self.top + self.height)


class OCREngine:
    """OCR-based text detection on screen.

    Automatically selects the best available backend:
    - Windows OCR (winocr): instant, no download, Windows 10+ only
    - EasyOCR: cross-platform, slower, downloads models on first use
    """

    # Max width for OCR processing (larger images are downsampled to save memory)
    MAX_OCR_WIDTH = 1280

    def __init__(self, languages: list[str] = None, backend: Optional[str] = None):
        self._languages = languages or ["en"]
        self._backend = backend or _OCR_BACKEND

        if self._backend is None:
            raise ImportError(
                "No OCR backend found. Install easyocr: pip install easyocr"
            )

        if self._backend == "winocr":
            logger.info("Using Windows OCR engine (languages=%s)", self._languages)
            self._reader = None  # Windows OCR needs no preloading
        elif self._backend == "easyocr":
            logger.info("Loading EasyOCR engine (languages=%s)...", self._languages)
            self._reader = easyocr.Reader(self._languages, gpu=False)
            logger.info("EasyOCR engine loaded")
        else:
            raise ValueError(f"Unknown OCR backend: {self._backend}")

    @property
    def backend_name(self) -> str:
        """Return the active OCR backend name."""
        return self._backend

    def _preprocess_image(self, image: Image.Image) -> tuple[Image.Image, float]:
        """
        Downsample large images to reduce memory usage.
        Returns (processed_image, scale_factor).
        Scale factor is used to map coordinates back to original size.
        """
        w, h = image.size
        if w <= self.MAX_OCR_WIDTH:
            return image, 1.0

        scale = self.MAX_OCR_WIDTH / w
        new_w = self.MAX_OCR_WIDTH
        new_h = int(h * scale)
        resized = image.resize((new_w, new_h), Image.LANCZOS)
        return resized, scale

    def _read_all_raw(self, image: Image.Image) -> list[ElementMatch]:
        """Run OCR on image using the active backend. Returns all detected elements."""
        if self._backend == "winocr":
            return self._read_winocr(image)
        else:
            return self._read_easyocr(image)

    def _read_winocr(self, image: Image.Image) -> list[ElementMatch]:
        """OCR using Windows.Media.Ocr."""
        from oswright._ocr_windows import recognize

        # Windows OCR handles its own scaling, but we still preprocess for consistency
        processed, scale = self._preprocess_image(image)
        lang = self._languages[0] if self._languages else "en"

        results = recognize(processed, language=lang)
        elements = []
        for r in results:
            left = int(r["left"] / scale)
            top_coord = int(r["top"] / scale)
            w = int(r["width"] / scale)
            h = int(r["height"] / scale)
            elements.append(ElementMatch(
                x=left + w // 2,
                y=top_coord + h // 2,
                left=left, top=top_coord, width=w, height=h,
                confidence=0.95,  # Windows OCR doesn't provide confidence
                text=r["text"],
                method="ocr-winocr",
            ))
        return elements

    def _read_easyocr(self, image: Image.Image) -> list[ElementMatch]:
        """OCR using EasyOCR."""
        processed, scale = self._preprocess_image(image)
        img_array = np.array(processed)
        results = self._reader.readtext(img_array)

        elements = []
        for bbox, text, confidence in results:
            pts = np.array(bbox)
            left = int(pts[:, 0].min() / scale)
            top_coord = int(pts[:, 1].min() / scale)
            right = int(pts[:, 0].max() / scale)
            bottom = int(pts[:, 1].max() / scale)
            w = right - left
            h = bottom - top_coord

            elements.append(ElementMatch(
                x=left + w // 2, y=top_coord + h // 2,
                left=left, top=top_coord, width=w, height=h,
                confidence=confidence, text=text, method="ocr-easyocr",
            ))
        return elements

    def find_text(
        self, image: Image.Image, target: str, exact: bool = False
    ) -> list[ElementMatch]:
        """
        Find all occurrences of text on screen.

        Args:
            image: PIL Image to search in.
            target: Text to find.
            exact: If True, requires exact match. If False, uses substring match.

        Returns:
            List of ElementMatch objects sorted by confidence.
        """
        all_elements = self._read_all_raw(image)

        matches = []
        target_lower = target.lower()

        for el in all_elements:
            text_lower = el.text.lower() if el.text else ""

            if exact and text_lower != target_lower:
                continue
            if not exact and target_lower not in text_lower:
                continue

            matches.append(el)

        matches.sort(key=lambda m: m.confidence, reverse=True)
        return matches

    def read_all(self, image: Image.Image) -> list[ElementMatch]:
        """Read all text found in the image."""
        return self._read_all_raw(image)


class ImageMatcher:
    """Template-based image matching to find UI elements."""

    @staticmethod
    def find_image(
        screenshot: Image.Image,
        template_path: str,
        threshold: float = 0.8,
    ) -> list[ElementMatch]:
        """
        Find all occurrences of a template image within a screenshot.

        Args:
            screenshot: The screen image to search in.
            template_path: Path to the template image to find.
            threshold: Minimum match confidence (0-1).

        Returns:
            List of ElementMatch objects.
        """
        if not os.path.isfile(template_path):
            raise FileNotFoundError(f"Template image not found: {template_path}")

        screen_gray = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2GRAY)
        template = cv2.imread(template_path, cv2.IMREAD_GRAYSCALE)

        if template is None:
            raise ValueError(f"Could not read template image: {template_path}")

        th, tw = template.shape[:2]
        result = cv2.matchTemplate(screen_gray, template, cv2.TM_CCOEFF_NORMED)

        locations = np.where(result >= threshold)
        matches = []

        # Group nearby matches (non-maximum suppression)
        used = set()
        for pt in zip(*locations[::-1]):
            # Skip if too close to an already-found match
            skip = False
            for ux, uy in used:
                if abs(pt[0] - ux) < tw // 2 and abs(pt[1] - uy) < th // 2:
                    skip = True
                    break
            if skip:
                continue

            used.add(pt)
            conf = float(result[pt[1], pt[0]])
            matches.append(
                ElementMatch(
                    x=pt[0] + tw // 2,
                    y=pt[1] + th // 2,
                    left=pt[0],
                    top=pt[1],
                    width=tw,
                    height=th,
                    confidence=conf,
                    method="image",
                )
            )

        matches.sort(key=lambda m: m.confidence, reverse=True)
        return matches

    @staticmethod
    def find_image_from_array(
        screenshot: Image.Image,
        template: Image.Image,
        threshold: float = 0.8,
    ) -> list[ElementMatch]:
        """Find a template image (as PIL Image) within a screenshot."""
        screen_gray = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2GRAY)
        tmpl_gray = cv2.cvtColor(np.array(template), cv2.COLOR_RGB2GRAY)

        th, tw = tmpl_gray.shape[:2]
        result = cv2.matchTemplate(screen_gray, tmpl_gray, cv2.TM_CCOEFF_NORMED)

        locations = np.where(result >= threshold)
        matches = []

        used = set()
        for pt in zip(*locations[::-1]):
            skip = False
            for ux, uy in used:
                if abs(pt[0] - ux) < tw // 2 and abs(pt[1] - uy) < th // 2:
                    skip = True
                    break
            if skip:
                continue
            used.add(pt)

            conf = float(result[pt[1], pt[0]])
            matches.append(
                ElementMatch(
                    x=pt[0] + tw // 2, y=pt[1] + th // 2,
                    left=pt[0], top=pt[1], width=tw, height=th,
                    confidence=conf, method="image",
                )
            )

        matches.sort(key=lambda m: m.confidence, reverse=True)
        return matches
