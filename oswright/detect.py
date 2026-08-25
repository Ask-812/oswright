"""
Element detection module - finds UI elements on screen using OCR and image matching.
Analogous to Playwright's selectors and locators.
"""

import importlib.util
import logging
import os
import platform
from dataclasses import dataclass, replace
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

# EasyOCR as universal fallback. Detected without importing it: `import easyocr`
# transitively imports torch, which costs seconds of startup time and hundreds
# of MB of RSS. The real import happens lazily in OCREngine.__init__.
if importlib.util.find_spec("easyocr") is not None:
    _OCR_BACKENDS.append("easyocr")

_OCR_BACKEND = _OCR_BACKENDS[0] if _OCR_BACKENDS else None


def _no_backend_message() -> str:
    """Build a platform-specific message explaining how to get an OCR backend."""
    if _SYSTEM == "Windows":
        return (
            "No OCR backend available. OSWright normally uses the built-in Windows OCR "
            "engine, but its WinRT bindings are missing. Install them with:\n"
            "    pip install winrt-Windows.Media.Ocr winrt-Windows.Globalization "
            "winrt-Windows.Graphics.Imaging\n"
            "Alternatively install the (much larger) EasyOCR fallback: "
            "pip install 'oswright[easyocr]'\n"
            "Note: OCR is only needed for text-based lookups. The accessibility-tree "
            "tools (get_ui_tree/click_ui_element) work without it."
        )
    return (
        "No OCR backend available. Install EasyOCR:\n"
        "    pip install easyocr\n"
        "Note: OCR is only needed for text-based lookups. Image template matching "
        "(find_image_on_screen) works without it."
    )


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

    def offset(self, dx: int, dy: int) -> "ElementMatch":
        """
        Return a copy of this match translated by (dx, dy).

        This deliberately returns a new object instead of mutating in place:
        matches are handed out by the OCR cache and may be shared between
        callers, so mutating one would corrupt the cached coordinates and
        translate them again on every cache hit.
        """
        if not dx and not dy:
            return self
        return replace(
            self,
            x=self.x + dx,
            y=self.y + dy,
            left=self.left + dx,
            top=self.top + dy,
        )


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
            raise ImportError(_no_backend_message())

        if self._backend == "winocr":
            logger.info("Using Windows OCR engine (languages=%s)", self._languages)
            self._reader = None  # Windows OCR needs no preloading
        elif self._backend == "easyocr":
            logger.info("Loading EasyOCR engine (languages=%s)...", self._languages)
            import easyocr  # imported lazily: pulls in torch
            self._reader = easyocr.Reader(self._languages, gpu=False)
            logger.info("EasyOCR engine loaded")
        else:
            raise ValueError(
                f"Unknown OCR backend: {self._backend}. Available: {_OCR_BACKENDS}"
            )

        # Initialize OCR result cache
        from oswright.cache import ScreenCache
        self._cache = ScreenCache()

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
        """Run OCR on image using the active backend. Uses cache if image unchanged."""
        # Check cache first
        cached = self._cache.get_cached(image)
        if cached is not None:
            return cached

        if self._backend == "winocr":
            results = self._read_winocr(image)
        else:
            results = self._read_easyocr(image)

        # Store in cache
        self._cache.store(image, results)
        return results

    def _read_winocr(self, image: Image.Image) -> list[ElementMatch]:
        """OCR using Windows.Media.Ocr. Skips downsampling — Windows OCR is fast enough."""
        from oswright._ocr_windows import recognize

        languages = self._languages or ["en"]

        # A Windows OCR engine is bound to a single language, so multiple
        # configured languages need one pass each. Without this loop every
        # language after the first was silently ignored.
        elements: list[ElementMatch] = []
        seen: set[tuple] = set()
        errors: list[str] = []

        for lang in languages:
            try:
                results = recognize(image, language=lang)
            except RuntimeError as e:
                errors.append(f"{lang}: {e}")
                continue

            for r in results:
                text = r["text"]
                # Filter single-character noise (taskbar icons, etc.)
                if len(text) <= 1 and not text.isalnum():
                    continue
                left = int(r["left"])
                top_coord = int(r["top"])
                w = int(r["width"])
                h = int(r["height"])
                level = r.get("level", "word")

                key = (text, left, top_coord, w, h, level)
                if key in seen:
                    continue
                seen.add(key)

                elements.append(ElementMatch(
                    x=left + w // 2,
                    y=top_coord + h // 2,
                    left=left, top=top_coord, width=w, height=h,
                    confidence=0.95,
                    text=text,
                    method=f"ocr-winocr-{level}",
                ))

        if not elements and errors and len(errors) == len(languages):
            raise RuntimeError("Windows OCR failed for every language -> " + "; ".join(errors))
        for err in errors:
            logger.warning("Windows OCR unavailable for %s", err)

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

    # Cap on candidate points fed into non-maximum suppression. NMS is O(n^2),
    # and a low threshold against a large screen can produce tens of thousands
    # of candidates, so keep only the strongest ones.
    MAX_CANDIDATES = 2000

    @staticmethod
    def _match(
        screenshot: Image.Image,
        template_gray: "np.ndarray",
        threshold: float,
    ) -> list[ElementMatch]:
        """Run template matching and return de-duplicated matches."""
        screen_gray = cv2.cvtColor(np.array(screenshot), cv2.COLOR_RGB2GRAY)
        th, tw = template_gray.shape[:2]

        if th == 0 or tw == 0:
            raise ValueError("Template image is empty")
        if th > screen_gray.shape[0] or tw > screen_gray.shape[1]:
            raise ValueError(
                f"Template ({tw}x{th}) is larger than the captured screen "
                f"({screen_gray.shape[1]}x{screen_gray.shape[0]})"
            )

        # TM_CCOEFF_NORMED divides by the template's standard deviation, so a
        # flat (single-colour) template degenerates and scores ~1.0 everywhere,
        # reporting confident matches across the whole screen. Fall back to a
        # squared-difference metric, which stays well-defined.
        if float(template_gray.std()) < 1.0:
            scores = 1.0 - cv2.matchTemplate(
                screen_gray, template_gray, cv2.TM_SQDIFF_NORMED
            )
        else:
            scores = cv2.matchTemplate(screen_gray, template_gray, cv2.TM_CCOEFF_NORMED)

        ys, xs = np.where(scores >= threshold)
        if len(xs) == 0:
            return []

        # Strongest first, so non-maximum suppression keeps the best of a cluster.
        confidences = scores[ys, xs]
        order = np.argsort(confidences)[::-1][: ImageMatcher.MAX_CANDIDATES]

        matches: list[ElementMatch] = []
        kept: list[tuple[int, int]] = []
        for i in order:
            # Cast out of numpy: np.int64 is not JSON-serialisable, and these
            # coordinates are returned straight to MCP clients.
            x, y = int(xs[i]), int(ys[i])

            if any(
                abs(x - kx) < tw // 2 and abs(y - ky) < th // 2
                for kx, ky in kept
            ):
                continue
            kept.append((x, y))

            matches.append(
                ElementMatch(
                    x=x + tw // 2,
                    y=y + th // 2,
                    left=x,
                    top=y,
                    width=int(tw),
                    height=int(th),
                    confidence=float(confidences[i]),
                    method="image",
                )
            )

        return matches

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
            List of ElementMatch objects, strongest match first.
        """
        if not os.path.isfile(template_path):
            raise FileNotFoundError(f"Template image not found: {template_path}")

        template = cv2.imread(template_path, cv2.IMREAD_GRAYSCALE)
        if template is None:
            raise ValueError(f"Could not read template image: {template_path}")

        return ImageMatcher._match(screenshot, template, threshold)

    @staticmethod
    def find_image_from_array(
        screenshot: Image.Image,
        template: Image.Image,
        threshold: float = 0.8,
    ) -> list[ElementMatch]:
        """Find a template image (as PIL Image) within a screenshot."""
        tmpl_gray = cv2.cvtColor(np.array(template), cv2.COLOR_RGB2GRAY)
        return ImageMatcher._match(screenshot, tmpl_gray, threshold)
