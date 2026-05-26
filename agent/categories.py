"""
Категоризация app/domain на стороне агента.
Подгружает с сервера /app_categories, кеширует, обновляет раз в час.
Если сервер недоступен — использует встроенные дефолты.
"""

from __future__ import annotations

import logging
import re
import time
from urllib.parse import urlparse

import httpx

log = logging.getLogger("agent")


# Встроенные дефолты — копия с сервера на случай если сервер недоступен.
_DEFAULT_APPS: dict[str, str] = {
    "AmoCRM": "work", "amoCRM": "work",
    "Outlook": "work", "Microsoft Outlook": "work",
    "Word": "work", "Microsoft Word": "work",
    "Excel": "work", "Microsoft Excel": "work",
    "PowerPoint": "work",
    "Skorozvon": "work",
    "Zoom": "work", "Telemost": "work",
    "Telegram": "neutral", "Telegram Lite": "neutral",
    "WhatsApp": "neutral",
    "ВКонтакте": "neutral",
    "Google Chrome": "neutral", "Chrome": "neutral",
    "Safari": "neutral", "Firefox": "neutral", "Microsoft Edge": "neutral",
    "Arc": "neutral", "Brave Browser": "neutral", "Yandex": "neutral",
    "Finder": "neutral", "Explorer": "neutral",
    "Terminal": "neutral", "iTerm2": "neutral",
    "System Settings": "neutral", "Системные настройки": "neutral",
    "YouTube": "personal", "TikTok": "personal", "Instagram": "personal",
    "Spotify": "personal", "Steam": "personal", "Discord": "personal",
}

_DEFAULT_DOMAINS: dict[str, str] = {
    "amocrm.ru": "work", "amocrm.com": "work",
    "bitrix24.ru": "work",
    "skorozvon.ru": "work",
    "lkdzrkk.pro": "work", "office.lkdzrkk.pro": "work",
    "youtube.com": "personal", "youtu.be": "personal",
    "tiktok.com": "personal",
    "instagram.com": "personal",
    "twitter.com": "personal", "x.com": "personal",
    "reddit.com": "personal",
    "twitch.tv": "personal",
    "netflix.com": "personal", "kinopoisk.ru": "personal", "okko.tv": "personal",
    "vk.com": "neutral", "t.me": "neutral", "web.telegram.org": "neutral",
}

_BROWSER_APPS = {
    "Google Chrome", "Google Chrome Canary", "Chrome",
    "Safari", "Firefox", "Microsoft Edge",
    "Arc", "Brave Browser", "Yandex", "Яндекс.Браузер",
}

_URL_IN_TITLE_RE = re.compile(r" — (https?://\S+)\s*$")


class CategoryResolver:
    """Кеширует категории, обновляет раз в час."""

    def __init__(self, server_url: str) -> None:
        self.server_url = server_url.rstrip("/")
        self._apps = dict(_DEFAULT_APPS)
        self._domains = dict(_DEFAULT_DOMAINS)
        self._last_refresh = 0.0

    def refresh(self, client: httpx.Client) -> bool:
        try:
            r = client.get(f"{self.server_url}/app_categories", timeout=10.0)
            r.raise_for_status()
            data = r.json()
            apps = dict(_DEFAULT_APPS)
            for item in data.get("apps", []):
                apps[item["name"]] = item["category"]
            domains = dict(_DEFAULT_DOMAINS)
            for item in data.get("domains", []):
                domains[item["name"]] = item["category"]
            self._apps = apps
            self._domains = domains
            self._last_refresh = time.monotonic()
            log.info("category cache refreshed: %d apps, %d domains", len(apps), len(domains))
            return True
        except Exception as e:
            log.warning("category refresh failed (использую дефолты): %s", e)
            return False

    def maybe_refresh(self, client: httpx.Client, interval: float = 3600.0) -> None:
        if time.monotonic() - self._last_refresh > interval:
            self.refresh(client)

    def categorize(self, app_name: str | None, title: str | None) -> str:
        """Возвращает work | personal | neutral для активного окна."""
        app = app_name or "unknown"
        if app in _BROWSER_APPS and title:
            m = _URL_IN_TITLE_RE.search(title)
            if m:
                try:
                    host = urlparse(m.group(1)).hostname
                    if host and host.startswith("www."):
                        host = host[4:]
                    if host:
                        host = host.lower()
                        if host in self._domains:
                            return self._domains[host]
                except Exception:
                    pass
        return self._apps.get(app, "neutral")
