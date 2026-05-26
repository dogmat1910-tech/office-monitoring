"""
OCR скриншотов через Tesseract.
Установка на сервере: apt install tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng
Python: pytesseract
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger("worker")

OCR_LANG = "rus+eng"


def ocr_image(image_path: Path) -> str:
    """Извлекает текст из JPEG через Tesseract. Возвращает строку."""
    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
    except ImportError as e:
        log.warning("OCR-зависимости не установлены: %s", e)
        return ""

    t0 = time.monotonic()
    try:
        img = Image.open(image_path)
        text = pytesseract.image_to_string(img, lang=OCR_LANG)
        elapsed = time.monotonic() - t0
        log.info("OCR %s: %.1fs, %d chars", image_path.name, elapsed, len(text))
        return text.strip()
    except Exception as e:
        log.warning("OCR упал для %s: %s", image_path, e)
        return ""
