"""
office-monitoring agent — Шаг 1: heartbeat.

Раз в HEARTBEAT_INTERVAL секунд агент сообщает серверу что жив.
Сервер: SERVER_URL (читается из env OM_SERVER_URL, по умолчанию localhost).
"""

import getpass
import hashlib
import logging
import os
import platform
import socket
import sys
import time
from pathlib import Path

import httpx

AGENT_VERSION = "0.1.0"
SERVER_URL = os.environ.get("OM_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")
HEARTBEAT_INTERVAL = int(os.environ.get("OM_HEARTBEAT_INTERVAL", "30"))

LOG_DIR = Path(os.environ.get("OM_LOG_DIR", str(Path.home() / ".office-monitoring")))
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "agent.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("agent")


def make_agent_id(hostname: str, username: str) -> str:
    raw = f"{hostname}::{username}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def send_heartbeat(client: httpx.Client, agent_id: str, hostname: str, username: str) -> bool:
    try:
        r = client.post(
            f"{SERVER_URL}/heartbeat",
            json={
                "agent_id": agent_id,
                "hostname": hostname,
                "username": username,
                "agent_version": AGENT_VERSION,
            },
            timeout=10.0,
        )
        r.raise_for_status()
        log.info("heartbeat ok: %s", r.json())
        return True
    except Exception as e:
        log.warning("heartbeat failed: %s", e)
        return False


def main() -> None:
    hostname = socket.gethostname()
    username = getpass.getuser()
    agent_id = make_agent_id(hostname, username)

    log.info(
        "agent starting: version=%s os=%s hostname=%s username=%s agent_id=%s server=%s",
        AGENT_VERSION,
        platform.platform(),
        hostname,
        username,
        agent_id,
        SERVER_URL,
    )

    with httpx.Client() as client:
        while True:
            send_heartbeat(client, agent_id, hostname, username)
            time.sleep(HEARTBEAT_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("agent stopped by user")
