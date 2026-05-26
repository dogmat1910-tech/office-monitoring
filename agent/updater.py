"""
Self-update агента.

Раз в час агент:
1. Спрашивает у сервера GET /agent/version → {version, files: [...], sha: {...}}
2. Если version > AGENT_VERSION:
   - Скачивает все .py файлы из списка в `*.new`
   - Проверяет sha256 (если сервер прислал)
   - Атомарно переименовывает .new → актуальные имена
   - Записывает маркер `~/.office-monitoring/UPDATE_PENDING`
3. Watchdog при следующей проверке видит маркер → перезапускает агента
4. Новый процесс стартует с новой версии

Откат: если новая версия упала, watchdog видит что лог не обновляется
→ перезапускает. Если 3 раза подряд — оставляем как есть (без авто-отката).
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from pathlib import Path

import httpx

log = logging.getLogger("agent")

INSTALL_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("OM_LOG_DIR", str(Path.home() / ".office-monitoring")))
UPDATE_MARKER = DATA_DIR / "UPDATE_PENDING"


def _version_tuple(v: str) -> tuple:
    """Парсит '0.9.1' → (0, 9, 1) для сравнения."""
    parts = []
    for p in v.replace("-", ".").split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def check_and_apply_update(client: httpx.Client, server_url: str, current_version: str) -> bool:
    """Возвращает True если обновление было применено (и watchdog должен перезапустить)."""
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

    log.info("update available: %s → %s, скачиваю...", current_version, new_version)
    files = info.get("files", [])
    shas = info.get("sha256", {})
    base_url = info.get("base_url", "").rstrip("/")
    if not base_url or not files:
        log.warning("update: некорректный ответ сервера: %r", info)
        return False

    downloaded: list[tuple[Path, Path]] = []  # (new_path, target_path)
    try:
        for fname in files:
            new_path = INSTALL_DIR / f"{fname}.new"
            target = INSTALL_DIR / fname
            rr = client.get(f"{base_url}/{fname}", timeout=30.0)
            rr.raise_for_status()
            content = rr.content
            # проверка sha256 если сервер дал
            expected = shas.get(fname)
            if expected:
                actual = hashlib.sha256(content).hexdigest()
                if actual != expected:
                    log.warning("update: SHA mismatch для %s — отменяю", fname)
                    raise RuntimeError("SHA mismatch")
            new_path.write_bytes(content)
            downloaded.append((new_path, target))

        # все скачали — атомарно подменяем
        for new_path, target in downloaded:
            # backup на всякий случай
            if target.exists():
                backup = INSTALL_DIR / f"{target.name}.bak"
                shutil.copy2(target, backup)
            new_path.replace(target)

        # маркер для watchdog
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        UPDATE_MARKER.write_text(f"{current_version} -> {new_version}", encoding="utf-8")
        log.info("update applied: %s → %s, перезапуск через watchdog", current_version, new_version)
        return True
    except Exception as e:
        log.warning("update failed, чищу .new: %s", e)
        for new_path, _ in downloaded:
            try:
                new_path.unlink(missing_ok=True)
            except Exception:
                pass
        return False
