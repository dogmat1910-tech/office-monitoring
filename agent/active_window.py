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


_OSASCRIPT_FRONTMOST = '''
tell application "System Events"
    set frontApp to first application process whose frontmost is true
    set appName to name of frontApp
    set windowTitle to ""
    try
        set windowTitle to name of front window of frontApp
    end try
    return appName & "|" & windowTitle
end tell
'''


def _get_mac() -> dict | None:
    """
    На Mac используем osascript: NSWorkspace API в долго живущих Python-процессах
    без main runloop кеширует frontmostApplication() и возвращает залипшее значение.
    osascript запускает свежий процесс System Events каждый раз — состояние всегда актуально.

    Заголовок окна требует разрешения Accessibility (System Settings → Privacy →
    Accessibility → добавить Terminal/iTerm/Python). Если разрешения нет — title пустой,
    имя приложения всё равно вернётся корректно.
    """
    import subprocess

    try:
        r = subprocess.run(
            ["osascript", "-e", _OSASCRIPT_FRONTMOST],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        if r.returncode != 0:
            log.debug("osascript failed (rc=%d): %s", r.returncode, r.stderr.strip())
            return _get_mac_nsworkspace_fallback()
        out = r.stdout.strip()
        if "|" in out:
            app_name, title = out.split("|", 1)
        else:
            app_name, title = out, ""
        if not app_name:
            return None
        return {"app_name": app_name, "title": title, "pid": 0}
    except FileNotFoundError:
        log.warning("osascript не найден — fallback на NSWorkspace")
        return _get_mac_nsworkspace_fallback()
    except subprocess.TimeoutExpired:
        log.warning("osascript timeout")
        return None
    except Exception as e:
        log.warning("osascript ошибка: %s", e)
        return _get_mac_nsworkspace_fallback()


def _get_mac_nsworkspace_fallback() -> dict | None:
    try:
        from AppKit import NSWorkspace  # type: ignore
    except ImportError:
        return None
    try:
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return None
        return {
            "app_name": str(app.localizedName() or "unknown"),
            "title": "",
            "pid": int(app.processIdentifier()),
        }
    except Exception:
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
