"""
Per-machine Bearer-токен для авторизации на сервере.

При первом запуске:
- читаем токен из OM_DATA_DIR/auth.token (если есть — используем)
- если нет — дёргаем POST /agent/register с install-кодом (env OM_INSTALL_CODE)
- сервер выдаёт уникальный токен, сохраняем в файл

Дальше agent.py подмешивает токен в httpx.Client как default header.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx

log = logging.getLogger("agent")

DATA_DIR = Path(os.environ.get("OM_DATA_DIR", os.environ.get("OM_LOG_DIR", str(Path.home() / ".office-monitoring"))))
TOKEN_FILE = DATA_DIR / "auth.token"


def load_token() -> str | None:
    if not TOKEN_FILE.exists():
        return None
    try:
        t = TOKEN_FILE.read_text(encoding="utf-8").strip()
        return t or None
    except OSError as e:
        log.warning("failed to read auth.token: %s", e)
        return None


def save_token(token: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(token, encoding="utf-8")
    try:
        # На Windows атрибуты прав ОС регулируются ACL — права через chmod игнорируются,
        # но на mac/linux выставим 0600 чтобы другие пользователи не читали.
        TOKEN_FILE.chmod(0o600)
    except OSError:
        pass


def register_agent(server_url: str, agent_id: str, hostname: str, username: str) -> str | None:
    """Регистрирует машину на сервере, возвращает выданный токен или None при ошибке."""
    install_code = os.environ.get("OM_INSTALL_CODE", "").strip()
    if not install_code:
        log.warning("OM_INSTALL_CODE not set — нечем зарегистрироваться")
        return None
    payload = {
        "install_code": install_code,
        "agent_id": agent_id,
        "hostname": hostname,
        "username": username,
    }
    try:
        with httpx.Client(trust_env=False, timeout=15.0) as c:
            r = c.post(f"{server_url.rstrip('/')}/agent/register", json=payload)
        if r.status_code == 401:
            log.error("install code rejected by server (401)")
            return None
        r.raise_for_status()
        return r.json().get("token")
    except Exception as e:
        log.warning("agent register failed: %s", e)
        return None


def ensure_token(server_url: str, agent_id: str, hostname: str, username: str) -> str | None:
    """Возвращает actively used bearer token (или None если не удалось получить)."""
    token = load_token()
    if token:
        return token
    log.info("auth.token not found — регистрируюсь на сервере")
    token = register_agent(server_url, agent_id, hostname, username)
    if token:
        save_token(token)
        log.info("agent registered, token saved")
    return token
