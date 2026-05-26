"""
Кроссплатформенный capture скриншотов primary monitor.
Возвращает JPEG-байты после ресайза до 1280px и сжатия q=70.

На macOS требует Screen Recording permission (системный алерт при первом запуске).
На Windows работает без доп. разрешений (антивирусы могут отреагировать).
"""

from __future__ import annotations

import io
import logging

log = logging.getLogger("agent")

TARGET_WIDTH = 1280
JPEG_QUALITY = 70


def capture_primary_jpeg() -> bytes | None:
    """Снимает primary monitor и возвращает JPEG-байты.
    Возвращает None если не получилось (нет разрешения, нет дисплея, ошибка)."""
    try:
        import mss  # type: ignore
        from PIL import Image  # type: ignore
    except ImportError as e:
        log.warning("mss/Pillow не установлены: %s", e)
        return None

    try:
        with mss.mss() as sct:
            # monitors[0] = объединение всех; monitors[1] = primary
            mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            raw = sct.grab(mon)

        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

        # ресайз: ширина = TARGET_WIDTH, высота пропорционально
        if img.width > TARGET_WIDTH:
            ratio = TARGET_WIDTH / img.width
            new_size = (TARGET_WIDTH, int(img.height * ratio))
            img = img.resize(new_size, Image.LANCZOS)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return buf.getvalue()
    except Exception as e:
        log.warning("screenshot failed: %s", e)
        return None
