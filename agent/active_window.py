"""
Кроссплатформенное получение активного окна.

Возвращает dict: {"app_name": str, "title": str, "pid": int} или None если не удалось.

Mac: имя приложения через NSWorkspace. Заголовок окна на Mac не получаем —
для него нужен Accessibility-доступ (System Settings → Privacy → Accessibility),
добавим позже.

Windows: имя процесса через psutil + заголовок окна через win32gui.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger("agent")


def get_active_window() -> dict | None:
    if sys.platform == "darwin":
        return _get_mac()
    if sys.platform == "win32":
        return _get_windows()
    return None


def _get_mac() -> dict | None:
    try:
        from AppKit import NSWorkspace  # type: ignore
    except ImportError:
        log.warning("AppKit (pyobjc-framework-Cocoa) не установлен — окна не отслеживаются")
        return None

    try:
        ws = NSWorkspace.sharedWorkspace()
        app = ws.frontmostApplication()
        if app is None:
            return None
        return {
            "app_name": str(app.localizedName() or ""),
            "title": "",
            "pid": int(app.processIdentifier()),
        }
    except Exception as e:
        log.warning("get_active_window mac failed: %s", e)
        return None


def _get_windows() -> dict | None:
    try:
        import psutil  # type: ignore
        import win32gui  # type: ignore
        import win32process  # type: ignore
    except ImportError:
        log.warning("pywin32/psutil не установлены — окна не отслеживаются")
        return None

    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None
        title = win32gui.GetWindowText(hwnd) or ""
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            app_name = psutil.Process(pid).name()
        except Exception:
            app_name = "unknown"
        return {"app_name": app_name, "title": title, "pid": int(pid)}
    except Exception as e:
        log.warning("get_active_window windows failed: %s", e)
        return None
