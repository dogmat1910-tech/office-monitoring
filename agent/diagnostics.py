"""
Self-diagnostics агента — собирает информацию о состоянии всех компонентов
и доступных разрешений. Отправляется на сервер при старте и периодически.

Помогает быстро понять у каких машин что-то не работает:
- Нет Screen Recording → скрины не идут
- Нет Microphone → аудио не идёт
- Нет Input Monitoring → счётчик клавиш не работает
- Версия Python, OS, доступные библиотеки
"""

from __future__ import annotations

import importlib
import os
import platform
import socket
import subprocess
import sys
from pathlib import Path


def _check_import(module: str) -> bool:
    try:
        importlib.import_module(module)
        return True
    except ImportError:
        return False


def _check_mac_permission(service: str) -> str:
    """Проверка через TCC.db не доступна без полного доступа.
    Вместо этого делаем функциональный тест через быстрый вызов API."""
    if service == "screen_recording":
        try:
            import mss  # type: ignore
            with mss.mss() as sct:
                shot = sct.grab(sct.monitors[1])
            # Если получили только чёрный/прозрачный или ошибка — нет разрешения
            data = shot.bgra[:1000]
            if sum(data) < 100:
                return "denied"
            return "granted"
        except Exception:
            return "denied"
    if service == "microphone":
        try:
            import sounddevice as sd  # type: ignore
            sd.check_input_settings(samplerate=16000, channels=1, dtype="int16")
            return "granted"
        except Exception:
            return "denied"
    if service == "accessibility":
        # косвенно через osascript к System Events
        try:
            r = subprocess.run(
                ["osascript", "-e", 'tell application "System Events" to get name of first process whose frontmost is true'],
                capture_output=True, text=True, timeout=2.0,
            )
            return "granted" if r.returncode == 0 else "denied"
        except Exception:
            return "denied"
    return "unknown"


def collect_diagnostics(agent_version: str) -> dict:
    """Собирает полное состояние агента для отправки на сервер."""
    is_mac = sys.platform == "darwin"
    is_win = sys.platform == "win32"

    # Базовая информация
    info = {
        "agent_version": agent_version,
        "python_version": sys.version.split()[0],
        "platform": sys.platform,
        "platform_full": platform.platform(),
        "hostname": socket.gethostname(),
        "cwd": os.getcwd(),
    }

    # Доступные модули
    modules = {
        "httpx": _check_import("httpx"),
        "psutil": _check_import("psutil"),
        "sounddevice": _check_import("sounddevice"),
        "soundfile": _check_import("soundfile"),
        "webrtcvad": _check_import("webrtcvad"),
        "pynput": _check_import("pynput"),
        "mss": _check_import("mss"),
        "PIL": _check_import("PIL"),
        "numpy": _check_import("numpy"),
    }
    if is_mac:
        modules["AppKit"] = _check_import("AppKit")
        modules["Quartz"] = _check_import("Quartz")
    if is_win:
        modules["win32gui"] = _check_import("win32gui")
        modules["win32process"] = _check_import("win32process")

    info["modules"] = modules

    # Разрешения (только Mac пока — на Windows TCC аналога нет)
    if is_mac:
        info["permissions"] = {
            "screen_recording": _check_mac_permission("screen_recording"),
            "microphone": _check_mac_permission("microphone"),
            "accessibility": _check_mac_permission("accessibility"),
        }
    else:
        info["permissions"] = {}

    # Доступное место на диске для логов
    try:
        log_dir = Path(os.environ.get("OM_LOG_DIR", str(Path.home() / ".office-monitoring")))
        if log_dir.exists():
            import shutil
            total, used, free = shutil.disk_usage(log_dir)
            info["disk"] = {
                "log_dir": str(log_dir),
                "free_gb": round(free / 1024 / 1024 / 1024, 1),
                "used_gb": round(used / 1024 / 1024 / 1024, 1),
            }
    except Exception:
        info["disk"] = {}

    # Память процесса
    try:
        import psutil  # type: ignore
        p = psutil.Process()
        info["process"] = {
            "rss_mb": round(p.memory_info().rss / 1024 / 1024, 1),
            "cpu_percent": p.cpu_percent(interval=0.1),
            "pid": p.pid,
        }
    except Exception:
        info["process"] = {}

    # Сетевая доступность сервера (быстрая проверка)
    server_url = os.environ.get("OM_SERVER_URL", "")
    if server_url:
        try:
            import httpx
            r = httpx.get(f"{server_url.rstrip('/')}/health", timeout=5.0, trust_env=False)
            info["server_reachable"] = r.status_code == 200
        except Exception as e:
            info["server_reachable"] = False
            info["server_error"] = str(e)[:200]

    return info
