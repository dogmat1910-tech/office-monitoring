"""
Кейлоггер office-monitoring.

KeystrokeAggregator — считает число нажатий по (app_name, domain).
НЕ хранит содержимое нажатий, только счётчик. Юридически безопасно.

На macOS нужно Accessibility-разрешение (как и трекинг окон).
На Windows работает без доп. разрешений.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict

log = logging.getLogger("agent")


class KeystrokeAggregator:
    """Только счёт нажатий, без содержимого."""

    def __init__(self, get_window_fn) -> None:
        self.get_window_fn = get_window_fn
        self._lock = threading.Lock()
        self._counts: dict[str, int] = defaultdict(int)
        self._listener = None

    def start(self) -> bool:
        try:
            from pynput import keyboard  # type: ignore
        except ImportError:
            log.warning("pynput не установлен — счётчик клавиш отключён")
            return False

        def _on_press(key):  # noqa: ANN001
            try:
                w = self.get_window_fn() or {}
                app = w.get("app_name") or "unknown"
                title = w.get("title") or ""
                domain = self._extract_domain_from_title(title) if " — http" in title else None
                key_id = f"{app}|{domain or ''}"
                with self._lock:
                    self._counts[key_id] += 1
            except Exception:
                pass

        try:
            self._listener = keyboard.Listener(on_press=_on_press)
            self._listener.start()
            log.info("keystroke aggregator started")
            return True
        except Exception as e:
            log.warning("keystroke listener failed: %s", e)
            self._listener = None
            return False

    def stop(self) -> None:
        if self._listener:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None

    def is_running(self) -> bool:
        return self._listener is not None

    @staticmethod
    def _extract_domain_from_title(title: str) -> str | None:
        try:
            from urllib.parse import urlparse
            url = title.rsplit(" — ", 1)[-1].strip()
            if not url.startswith(("http://", "https://")):
                return None
            host = urlparse(url).hostname
            if host and host.startswith("www."):
                host = host[4:]
            return host.lower() if host else None
        except Exception:
            return None

    def drain(self) -> list[dict]:
        with self._lock:
            snapshot = dict(self._counts)
            self._counts.clear()
        result = []
        for key, count in snapshot.items():
            app, domain = key.split("|", 1)
            result.append({
                "app_name": app,
                "domain": domain or None,
                "count": count,
            })
        return result
