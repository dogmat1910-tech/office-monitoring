"""
Self-update агента (.exe модель).

Раз в N минут агент:
1. GET /agent/version → {version, agent_exe_url, watchdog_exe_url, sha256_agent, sha256_watchdog}
2. Если новее текущей:
   - Скачивает office-monitoring-agent.exe.new и office-monitoring-watchdog.exe.new
     в INSTALL_DIR (но НЕ переписывает живые .exe — Windows не даёт).
   - Сверяет SHA256.
   - Пишет маркер DATA_DIR/UPDATE_PENDING.
3. Главный цикл агента выходит → watchdog при следующем тике видит маркер,
   делает atomic swap (.new → .exe) и запускает новую версию.

Откат: если новая версия упала, watchdog видит что лог не обновляется → перезапускает.
Если несколько раз подряд — старая .exe.old рядом, можно вернуть руками.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

import httpx

log = logging.getLogger("agent")

INSTALL_DIR = Path(os.environ.get("OM_INSTALL_DIR", r"C:\Program Files\office-monitoring"))
DATA_DIR = Path(os.environ.get("OM_LOG_DIR", str(Path.home() / ".office-monitoring")))
UPDATE_MARKER = DATA_DIR / "UPDATE_PENDING"


def _version_tuple(v: str) -> tuple:
    parts = []
    for p in v.replace("-", ".").split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(client: httpx.Client, url: str, dest: Path) -> None:
    with client.stream("GET", url, timeout=120.0) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(65536):
                f.write(chunk)


def check_and_apply_update(client: httpx.Client, server_url: str, current_version: str) -> bool:
    """True если новая версия скачана и готова к применению (главный цикл должен выйти).

    Сам swap делает watchdog — он первым проверяет UPDATE_PENDING и переименовывает
    .exe.new → .exe пока agent не работает.
    """
    try:
        r = client.get(f"{server_url.rstrip('/')}/agent/version", timeout=10.0)
        r.raise_for_status()
        info = r.json()
    except Exception as e:
        log.debug("update check failed: %s", e)
        return False

    new_version = info.get("version")
    if not new_version:
        return False
    if _version_tuple(new_version) <= _version_tuple(current_version):
        log.debug("update: current=%s server=%s — up to date", current_version, new_version)
        return False

    agent_url = info.get("agent_exe_url")
    watchdog_url = info.get("watchdog_exe_url")
    sha_agent = info.get("sha256_agent")
    sha_watchdog = info.get("sha256_watchdog")
    if not agent_url or not sha_agent:
        log.warning("update: ответ сервера без agent_exe_url/sha256_agent: %r", info)
        return False

    log.info("update available: %s → %s, скачиваю...", current_version, new_version)

    agent_new = INSTALL_DIR / "office-monitoring-agent.exe.new"
    watchdog_new = INSTALL_DIR / "office-monitoring-watchdog.exe.new"

    try:
        _download(client, agent_url, agent_new)
        actual = _sha256_file(agent_new)
        if actual != sha_agent:
            log.warning("update: SHA mismatch agent.exe (exp=%s got=%s)", sha_agent, actual)
            agent_new.unlink(missing_ok=True)
            return False

        # Watchdog опциональный — если SHA не сошлась или не пришёл url, обновим только агент
        watchdog_ready = False
        if watchdog_url and sha_watchdog:
            try:
                _download(client, watchdog_url, watchdog_new)
                actual_w = _sha256_file(watchdog_new)
                if actual_w == sha_watchdog:
                    watchdog_ready = True
                else:
                    log.warning("update: SHA mismatch watchdog.exe — пропускаю")
                    watchdog_new.unlink(missing_ok=True)
            except Exception as e:
                log.warning("update: watchdog download failed: %s", e)
                watchdog_new.unlink(missing_ok=True)

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        UPDATE_MARKER.write_text(
            f"{current_version} -> {new_version}\nwatchdog_ready={watchdog_ready}\n",
            encoding="utf-8",
        )
        log.info(
            "update prepared: %s → %s (watchdog_ready=%s), agent exit → watchdog swaps",
            current_version, new_version, watchdog_ready,
        )
        return True
    except Exception as e:
        log.warning("update failed: %s", e)
        agent_new.unlink(missing_ok=True)
        watchdog_new.unlink(missing_ok=True)
        return False
