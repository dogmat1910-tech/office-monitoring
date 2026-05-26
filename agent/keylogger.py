"""
Кейлоггер office-monitoring.

Два независимых режима:

1. KeystrokeAggregator — всегда включён. Считает число нажатий по
   (app_name, domain). НЕ хранит содержимое. Юридически безопасно.

2. KeystrokeTextBuffer — опционально включается через OM_ENABLE_KEYSTROKE_TEXT=1
   и список приложений через OM_KEYSTROKE_TEXT_APPS=Telegram.exe,WhatsApp.exe.
   Записывает текст в буфер по (app_name, window_title), нарезает на «сообщения»
   по сменам окна и таймауту бездействия. Маскирует потенциальные секреты
   (длинные alphanumeric/пунктуационные строки → [REDACTED]). Игнорирует
   Ctrl-сочетания (copy/paste/etc).

   Этот режим требует юридического оформления (152-ФЗ, 138 УК):
   - письменное согласие сотрудника в трудовом договоре
   - локальный нормативный акт о мониторинге
   - запись только с корпоративных устройств в рабочее время

На macOS обоим нужно Accessibility-разрешение (как и трекинг окон).
На Windows работают без доп. разрешений.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone

log = logging.getLogger("agent")


class KeystrokeAggregator:
    """Только счёт нажатий, без содержимого."""

    def __init__(self, get_window_fn) -> None:
        self.get_window_fn = get_window_fn
        self._lock = threading.Lock()
        self._counts: dict[str, int] = defaultdict(int)
        self._listener = None
        self._text_buffer: "KeystrokeTextBuffer | None" = None

    def attach_text_buffer(self, buffer: "KeystrokeTextBuffer") -> None:
        """Прокси-режим: тот же listener кормит и счётчик, и текст-буфер,
        чтобы не плодить два pynput-listener'а."""
        self._text_buffer = buffer

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
                # текст-буфер (если включён)
                if self._text_buffer is not None:
                    self._text_buffer.on_press(key, app, title)
            except Exception:
                pass

        def _on_release(key):  # noqa: ANN001
            if self._text_buffer is not None:
                try:
                    self._text_buffer.on_release(key)
                except Exception:
                    pass

        try:
            self._listener = keyboard.Listener(on_press=_on_press, on_release=_on_release)
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


# Регекспы для маскирования потенциальных секретов в записываемом тексте.
# Срабатывают на длинные строки без пробелов с миксом цифр/букв/символов —
# типично для паролей, токенов, номеров карт.
_SECRET_PATTERNS = [
    re.compile(r"\b[A-Za-z0-9!@#$%^&*_\-+=]{16,}\b"),  # длинные комбинированные строки
    re.compile(r"\b\d{13,19}\b"),                      # длинные числа (карты, телефоны+)
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),  # email — маскируем тоже (не критично если протечёт, но юр-безопаснее)
]

# Клавиши-модификаторы — пропускаем
_MODIFIER_KEYS = {"ctrl", "ctrl_l", "ctrl_r", "alt", "alt_l", "alt_r",
                  "shift", "shift_l", "shift_r", "cmd", "cmd_l", "cmd_r", "win"}

# Управляющие клавиши, которые сбрасывают буфер (нажатие = "сообщение отправлено")
_FLUSH_KEYS = {"enter"}


class KeystrokeTextBuffer:
    """Буферизирует набираемый текст в whitelisted приложениях.

    Сегментация на «сообщения»:
    - смена активного окна → flush текущего
    - Enter → flush (это типичный способ отправить сообщение в чатах)
    - idle > FLUSH_AFTER_IDLE_SEC секунд без ввода → flush
    """

    FLUSH_AFTER_IDLE_SEC = 20

    def __init__(self, whitelist_apps: set[str]) -> None:
        # Сравнение регистронезависимое — на Windows "Telegram.exe", "telegram.exe" одно и то же
        self.whitelist = {a.lower() for a in whitelist_apps}
        self._lock = threading.Lock()
        self._buffer: list[str] = []
        self._current_app: str | None = None
        self._current_title: str | None = None
        self._started_at: float | None = None
        self._last_press_at: float | None = None
        self._completed: list[dict] = []
        self._mods_pressed: set[str] = set()

    def _is_whitelisted(self, app_name: str) -> bool:
        return app_name.lower() in self.whitelist

    def _key_to_char(self, key) -> str | None:  # noqa: ANN001
        """Превращает pynput key в печатный символ или None."""
        try:
            if hasattr(key, "char") and key.char is not None:
                return key.char
        except Exception:
            return None
        # Специальные клавиши
        try:
            name = key.name
        except AttributeError:
            return None
        if name == "space":
            return " "
        if name == "tab":
            return "\t"
        return None

    def _flush_locked(self) -> None:
        """Должно вызываться под self._lock. Сохраняет накопленный текст как
        завершённую сессию, если в нём есть содержательные символы."""
        if not self._buffer or not self._current_app:
            self._buffer.clear()
            self._started_at = None
            return
        raw = "".join(self._buffer).strip()
        if len(raw) < 2:
            self._buffer.clear()
            self._started_at = None
            return
        masked = self._mask_secrets(raw)
        ended_at_ts = self._last_press_at or time.time()
        started_at_ts = self._started_at or ended_at_ts
        self._completed.append({
            "app_name": self._current_app,
            "window_title": self._current_title or "",
            "started_at": datetime.fromtimestamp(started_at_ts, tz=timezone.utc).isoformat(),
            "ended_at": datetime.fromtimestamp(ended_at_ts, tz=timezone.utc).isoformat(),
            "text": masked,
            "char_count": len(raw),
        })
        self._buffer.clear()
        self._started_at = None

    @staticmethod
    def _mask_secrets(text: str) -> str:
        out = text
        for pat in _SECRET_PATTERNS:
            out = pat.sub("[REDACTED]", out)
        return out

    def on_press(self, key, app_name: str, window_title: str) -> None:  # noqa: ANN001
        """Вызывается из общего pynput-listener'а на каждое нажатие."""
        # На случай если в режим вошли посредине работы — игнорим нажатие
        # не в whitelist-приложении.
        if not self._is_whitelisted(app_name):
            with self._lock:
                # Если был активный буфер в другом окне — сохраняем его
                if self._current_app is not None:
                    self._flush_locked()
                self._current_app = None
                self._current_title = None
            return

        now = time.time()
        key_name = None
        try:
            key_name = key.name  # noqa: SLF001
        except AttributeError:
            pass

        with self._lock:
            # Смена окна (другой app или другой title) — flush старое
            if self._current_app != app_name or self._current_title != window_title:
                if self._current_app is not None:
                    self._flush_locked()
                self._current_app = app_name
                self._current_title = window_title

            # Idle timeout — flush
            if self._last_press_at and (now - self._last_press_at > self.FLUSH_AFTER_IDLE_SEC):
                self._flush_locked()

            # Модификаторы — запоминаем, символ не пишем
            if key_name in _MODIFIER_KEYS:
                self._mods_pressed.add(key_name)
                self._last_press_at = now
                return

            # Ctrl/Cmd + что-то — игнорируем (copy/paste/select-all/etc), сбрасываем буфер символа
            ctrl_like = any(m.startswith("ctrl") or m.startswith("cmd") or m == "win" for m in self._mods_pressed)
            if ctrl_like:
                self._last_press_at = now
                return

            # Enter — сообщение отправлено в чате, flush
            if key_name in _FLUSH_KEYS:
                self._last_press_at = now
                self._flush_locked()
                return

            # Backspace — удаляем последний символ из буфера
            if key_name == "backspace":
                if self._buffer:
                    self._buffer.pop()
                self._last_press_at = now
                return

            # Печатный символ
            char = self._key_to_char(key)
            if char is not None:
                if self._started_at is None:
                    self._started_at = now
                self._buffer.append(char)
                self._last_press_at = now

    def on_release(self, key) -> None:  # noqa: ANN001
        try:
            name = key.name
        except AttributeError:
            return
        with self._lock:
            self._mods_pressed.discard(name)

    def drain(self) -> list[dict]:
        """Забирает завершённые сессии. Текущий буфер не трогаем —
        он закроется при flush'е или таймауте."""
        with self._lock:
            # Если давно ничего не вводилось — flush текущего тоже
            now = time.time()
            if self._last_press_at and now - self._last_press_at > self.FLUSH_AFTER_IDLE_SEC:
                self._flush_locked()
            result = list(self._completed)
            self._completed.clear()
        return result
