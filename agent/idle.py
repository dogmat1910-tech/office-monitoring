"""
Кроссплатформенный idle-time getter.

Возвращает кол-во секунд с последнего ввода (клавиатура или мышь).
Большой idle (>60-120 сек) = менеджер отошёл от компьютера.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger("agent")

_warned = False


def get_idle_seconds() -> float | None:
    if sys.platform == "darwin":
        return _get_idle_mac()
    if sys.platform == "win32":
        return _get_idle_win()
    return None


def _get_idle_mac() -> float | None:
    global _warned
    try:
        from Quartz import (  # type: ignore
            CGEventSourceSecondsSinceLastEventType,
            kCGEventSourceStateHIDSystemState,
            kCGAnyInputEventType,
        )
        return float(CGEventSourceSecondsSinceLastEventType(
            kCGEventSourceStateHIDSystemState, kCGAnyInputEventType
        ))
    except ImportError:
        if not _warned:
            log.warning("Quartz не установлен (pyobjc-framework-Quartz) — idle не отслеживается")
            _warned = True
        return None
    except Exception as e:
        log.debug("idle mac failed: %s", e)
        return None


def _get_idle_win() -> float | None:
    try:
        import ctypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            tick_count = ctypes.windll.kernel32.GetTickCount()
            # tick переполняется через 49 дней — берём модуль
            diff_ms = (tick_count - info.dwTime) & 0xFFFFFFFF
            return diff_ms / 1000.0
    except Exception as e:
        log.debug("idle win failed: %s", e)
    return None
