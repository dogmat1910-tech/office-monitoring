"""
Watchdog для office-monitoring agent.

Следит за основным процессом агента:
- Каждые WATCHDOG_INTERVAL секунд проверяет mtime agent.log
- Если лог не обновлялся > STALE_LOG_SEC секунд → агент считается мёртвым
- Запускает агент заново

Запускается отдельной Scheduled Task с тем же триггером At-LogOn.
Если убили watchdog — Scheduled Task поднимет через ~1 минуту.
Если убили оба — Scheduled Task поднимет watchdog → watchdog поднимет агент.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

# ── пути на Windows ──
INSTALL_DIR = Path(os.environ.get("OM_INSTALL_DIR", r"C:\Program Files\office-monitoring"))
DATA_DIR = Path(os.environ.get("OM_DATA_DIR", r"C:\ProgramData\office-monitoring"))
AGENT_LOG = DATA_DIR / "agent.log"
WATCHDOG_LOG = DATA_DIR / "watchdog.log"
PYTHON_EXE = INSTALL_DIR / ".venv" / "Scripts" / "pythonw.exe"
AGENT_SCRIPT = INSTALL_DIR / "agent.py"

WATCHDOG_INTERVAL = int(os.environ.get("OM_WATCHDOG_INTERVAL", "30"))
STALE_LOG_SEC = int(os.environ.get("OM_WATCHDOG_STALE_SEC", "90"))


def log(msg: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    try:
        with open(WATCHDOG_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def agent_is_alive() -> bool:
    """Считаем агента живым если agent.log обновлялся за последние STALE_LOG_SEC секунд."""
    if not AGENT_LOG.exists():
        return False
    try:
        age = time.time() - AGENT_LOG.stat().st_mtime
        return age < STALE_LOG_SEC
    except Exception as e:
        log(f"stat agent.log failed: {e}")
        return False


def start_agent() -> None:
    if not PYTHON_EXE.exists() or not AGENT_SCRIPT.exists():
        log(f"missing: python={PYTHON_EXE.exists()} agent={AGENT_SCRIPT.exists()}")
        return
    env = os.environ.copy()
    env.setdefault("OM_SERVER_URL", "https://office.lkdzrkk.pro")
    env.setdefault("OM_LOG_DIR", str(DATA_DIR))
    env.setdefault("OM_ENABLE_ALWAYS_ON_AUDIO", "1")
    try:
        # DETACHED_PROCESS | CREATE_NO_WINDOW = 0x00000008 | 0x08000000
        subprocess.Popen(
            [str(PYTHON_EXE), str(AGENT_SCRIPT)],
            cwd=str(INSTALL_DIR),
            env=env,
            creationflags=0x00000008 | 0x08000000,
        )
        log("agent (re)started")
    except Exception as e:
        log(f"start_agent error: {e}")


def main() -> None:
    log(f"watchdog starting (interval={WATCHDOG_INTERVAL}s, stale={STALE_LOG_SEC}s)")
    while True:
        try:
            if not agent_is_alive():
                log("agent down or log stale → restart")
                start_agent()
                time.sleep(WATCHDOG_INTERVAL * 2)  # даём агенту разогнаться
            else:
                time.sleep(WATCHDOG_INTERVAL)
        except KeyboardInterrupt:
            log("watchdog stopped by user")
            return
        except Exception as e:
            log(f"loop error: {e}")
            time.sleep(WATCHDOG_INTERVAL)


if __name__ == "__main__":
    main()
