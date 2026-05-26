"""
Watchdog для office-monitoring agent.

Следит за основным процессом агента:
- Каждые WATCHDOG_INTERVAL секунд проверяет, что office-monitoring-agent.exe
  жив, и что agent.log обновлялся в пределах STALE_LOG_SEC секунд.
- Если процесса нет ИЛИ лог завис → запускает агента заново.
- Перед запуском проверяет, что агент действительно не запущен —
  чтобы не плодить второй экземпляр поверх живого.

Сам watchdog защищён PID-файлом через psutil: если уже запущен
другой watchdog (PID жив и имя процесса совпадает) — выходим.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import psutil

INSTALL_DIR = Path(os.environ.get("OM_INSTALL_DIR", r"C:\Program Files\office-monitoring"))
DATA_DIR = Path(os.environ.get("OM_DATA_DIR", r"C:\ProgramData\office-monitoring"))
AGENT_LOG = DATA_DIR / "agent.log"
WATCHDOG_LOG = DATA_DIR / "watchdog.log"
WATCHDOG_PID_FILE = DATA_DIR / "watchdog.pid"
AGENT_EXE = INSTALL_DIR / "office-monitoring-agent.exe"
AGENT_PROCESS_NAME = "office-monitoring-agent.exe"
WATCHDOG_PROCESS_NAME = "office-monitoring-watchdog.exe"

WATCHDOG_INTERVAL = int(os.environ.get("OM_WATCHDOG_INTERVAL", "30"))
STALE_LOG_SEC = int(os.environ.get("OM_WATCHDOG_STALE_SEC", "120"))


def log(msg: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    try:
        with open(WATCHDOG_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def find_process_by_name(name: str, exclude_pid: int | None = None) -> psutil.Process | None:
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if p.info["name"] and p.info["name"].lower() == name.lower():
                if exclude_pid is not None and p.info["pid"] == exclude_pid:
                    continue
                return p
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def ensure_single_watchdog() -> None:
    """Если уже работает другой watchdog (PID-файл валиден и процесс жив) — выходим."""
    my_pid = os.getpid()
    if WATCHDOG_PID_FILE.exists():
        try:
            old_pid = int(WATCHDOG_PID_FILE.read_text().strip())
            if old_pid != my_pid and psutil.pid_exists(old_pid):
                try:
                    p = psutil.Process(old_pid)
                    if p.name().lower() == WATCHDOG_PROCESS_NAME.lower():
                        log(f"another watchdog already running (pid={old_pid}), exiting")
                        sys.exit(0)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except (ValueError, OSError):
            pass
    try:
        WATCHDOG_PID_FILE.write_text(str(my_pid))
    except OSError as e:
        log(f"failed to write pid-file: {e}")


def agent_log_fresh() -> bool:
    if not AGENT_LOG.exists():
        return False
    try:
        age = time.time() - AGENT_LOG.stat().st_mtime
        return age < STALE_LOG_SEC
    except Exception as e:
        log(f"stat agent.log failed: {e}")
        return False


def agent_process_alive() -> bool:
    return find_process_by_name(AGENT_PROCESS_NAME) is not None


def start_agent() -> None:
    if not AGENT_EXE.exists():
        log(f"agent exe not found: {AGENT_EXE}")
        return
    # Двойная проверка прямо перед запуском — на случай race с другим триггером
    if agent_process_alive():
        log("agent already running, skip start")
        return
    env = os.environ.copy()
    env.setdefault("OM_SERVER_URL", "https://office.lkdzrkk.pro")
    env.setdefault("OM_LOG_DIR", str(DATA_DIR))
    env.setdefault("OM_DATA_DIR", str(DATA_DIR))
    env.setdefault("OM_INSTALL_DIR", str(INSTALL_DIR))
    env.setdefault("OM_ENABLE_ALWAYS_ON_AUDIO", "1")
    try:
        # DETACHED_PROCESS | CREATE_NO_WINDOW
        subprocess.Popen(
            [str(AGENT_EXE)],
            cwd=str(INSTALL_DIR),
            env=env,
            creationflags=0x00000008 | 0x08000000,
        )
        log("agent (re)started")
    except Exception as e:
        log(f"start_agent error: {e}")


def main() -> None:
    ensure_single_watchdog()
    log(f"watchdog starting (interval={WATCHDOG_INTERVAL}s, stale={STALE_LOG_SEC}s, pid={os.getpid()})")
    while True:
        try:
            alive = agent_process_alive()
            fresh = agent_log_fresh()
            if not alive:
                log("agent process not found → restart")
                start_agent()
                time.sleep(WATCHDOG_INTERVAL * 2)
            elif not fresh:
                log("agent process alive but log stale → restart")
                # Сначала прибиваем зависший экземпляр, потом стартуем новый
                old = find_process_by_name(AGENT_PROCESS_NAME)
                if old is not None:
                    try:
                        old.kill()
                        old.wait(timeout=5)
                    except Exception as e:
                        log(f"kill stale agent failed: {e}")
                start_agent()
                time.sleep(WATCHDOG_INTERVAL * 2)
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
