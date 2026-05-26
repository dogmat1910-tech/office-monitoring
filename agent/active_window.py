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

# Браузеры, у которых AppleScript умеет отдавать URL + заголовок активной вкладки.
# Значение — это код AppleScript. {app} подставляется через .format(app=...)
_BROWSER_TAB_SCRIPTS = {
    "Google Chrome": (
        'tell application "Google Chrome"\n'
        '    if (count of windows) > 0 then\n'
        '        set t to active tab of front window\n'
        '        return (URL of t) & "|" & (title of t)\n'
        '    end if\n'
        'end tell'
    ),
    "Google Chrome Canary": (
        'tell application "Google Chrome Canary"\n'
        '    if (count of windows) > 0 then\n'
        '        set t to active tab of front window\n'
        '        return (URL of t) & "|" & (title of t)\n'
        '    end if\n'
        'end tell'
    ),
    "Microsoft Edge": (
        'tell application "Microsoft Edge"\n'
        '    if (count of windows) > 0 then\n'
        '        set t to active tab of front window\n'
        '        return (URL of t) & "|" & (title of t)\n'
        '    end if\n'
        'end tell'
    ),
    "Arc": (
        'tell application "Arc"\n'
        '    if (count of windows) > 0 then\n'
        '        set t to active tab of front window\n'
        '        return (URL of t) & "|" & (title of t)\n'
        '    end if\n'
        'end tell'
    ),
    "Brave Browser": (
        'tell application "Brave Browser"\n'
        '    if (count of windows) > 0 then\n'
        '        set t to active tab of front window\n'
        '        return (URL of t) & "|" & (title of t)\n'
        '    end if\n'
        'end tell'
    ),
    "Safari": (
        'tell application "Safari"\n'
        '    if (count of windows) > 0 then\n'
        '        set t to current tab of front window\n'
        '        return (URL of t) & "|" & (name of t)\n'
        '    end if\n'
        'end tell'
    ),
    # Яндекс.Браузер на Mac — тоже Chromium-based, использует тот же протокол
    "Yandex": (
        'tell application "Yandex"\n'
        '    if (count of windows) > 0 then\n'
        '        set t to active tab of front window\n'
        '        return (URL of t) & "|" & (title of t)\n'
        '    end if\n'
        'end tell'
    ),
}


def _get_browser_tab(app_name: str) -> tuple[str, str] | None:
    """Для известных браузеров пытается получить (url, tab_title) активной вкладки.
    Возвращает None если приложение не браузер или AppleScript упал
    (например пользователь не дал разрешение Automation для этого браузера)."""
    script = _BROWSER_TAB_SCRIPTS.get(app_name)
    if script is None:
        return None
    import subprocess
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=2.0,
        )
        if r.returncode != 0:
            log.debug("browser tab AS failed for %s: %s", app_name, r.stderr.strip())
            return None
        out = r.stdout.strip()
        if not out or "|" not in out:
            return None
        url, tab_title = out.split("|", 1)
        return url.strip(), tab_title.strip()
    except Exception as e:
        log.debug("browser tab AS error for %s: %s", app_name, e)
        return None


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

        # Для браузеров: пытаемся получить URL+title активной вкладки.
        # При первом обращении macOS спросит разрешение Automation для каждого
        # браузера отдельно (Terminal -> Google Chrome). Если откажешь — fallback
        # на title окна.
        tab_info = _get_browser_tab(app_name)
        if tab_info is not None:
            url, tab_title = tab_info
            # Сохраняем в title: «<заголовок вкладки> — <url>»
            # Если оба пусты — оставляем то что вернул front window.
            if tab_title or url:
                title = f"{tab_title} — {url}" if tab_title and url else (tab_title or url)

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


_WIN_BROWSER_PROCS = {
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
    "yandex.exe", "vivaldi.exe", "iexplore.exe",
}


def _get_browser_url_windows(hwnd: int) -> str | None:
    """Через UI Automation тянем URL из адресной строки активного браузера.
    Доступно с Win7+, для Chrome/Edge/Firefox работает «из коробки».
    Если что-то идёт не так — возвращаем None и продолжаем без URL.
    """
    try:
        import uiautomation as auto  # type: ignore
    except ImportError:
        return None
    try:
        ctrl = auto.ControlFromHandle(hwnd)
        if ctrl is None:
            return None
        # У Chrome/Edge адресная строка — Edit-контрол с Name 'Адресная строка'/'Address and search bar'.
        # Ищем универсально через ControlType=Edit, имеющий 'address'/'адрес' в Name.
        edit = ctrl.EditControl(searchDepth=15, foundIndex=1)
        if edit and edit.Exists(0.05, 0):
            v = edit.GetValuePattern().Value if edit.GetValuePattern() else ""
            if v:
                return v.strip()
        # Fallback: проходим по всем Edit-контролам, выбираем тот, что похож на URL
        for e in ctrl.GetChildren():
            try:
                if e.ControlTypeName == "EditControl":
                    vp = e.GetValuePattern()
                    if vp and vp.Value and ("." in vp.Value or vp.Value.startswith("http")):
                        return vp.Value.strip()
            except Exception:
                continue
        return None
    except Exception as e:
        log.debug("UIA URL extract failed: %s", e)
        return None


def _normalize_url(s: str) -> str | None:
    """Браузер показывает в адресной строке либо 'example.com/path', либо полный URL.
    Возвращаем что-нибудь, чему urlparse сможет извлечь hostname."""
    if not s:
        return None
    s = s.strip()
    if s.startswith(("http://", "https://", "chrome://", "edge://", "about:", "file:")):
        return s
    # Голый домен — добавим схему
    if "." in s.split("/")[0] and " " not in s:
        return "https://" + s
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

        if app_name.lower() in _WIN_BROWSER_PROCS:
            raw = _get_browser_url_windows(hwnd)
            url = _normalize_url(raw) if raw else None
            if url:
                # Тот же формат что и на Mac: «<заголовок вкладки> — <url>».
                # extract_url_from_title на сервере выловит URL отсюда и доменизирует.
                title = f"{title} — {url}" if title else url

        return {"app_name": app_name, "title": title, "pid": int(pid)}
    except Exception as e:
        log.warning("get_active_window windows failed: %s", e)
        return None
