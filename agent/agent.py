"""
office-monitoring agent.

Шаг 1: heartbeat — раз в FLUSH_INTERVAL агент сообщает серверу что жив.
Шаг 2: трекинг активного окна — каждые SAMPLE_INTERVAL агент смотрит,
       какое окно сейчас активно. Накопленные сэмплы отправляются на сервер
       вместе со следующим heartbeat'ом.
"""

from __future__ import annotations

import getpass
import hashlib
import logging
import os
import platform
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from active_window import get_active_window
from audio import AudioRecorder

AGENT_VERSION = "0.5.0"
SERVER_URL = os.environ.get("OM_SERVER_URL", "http://127.0.0.1:8000").rstrip("/")
SAMPLE_INTERVAL = int(os.environ.get("OM_SAMPLE_INTERVAL", "5"))    # как часто смотреть на активное окно
FLUSH_INTERVAL = int(os.environ.get("OM_FLUSH_INTERVAL", "30"))     # как часто слать heartbeat + накопленные сэмплы

LOG_DIR = Path(os.environ.get("OM_LOG_DIR", str(Path.home() / ".office-monitoring")))
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "agent.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
# глушим debug-логи httpx — слишком много шума на каждый запрос
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("agent")


def make_agent_id(hostname: str, username: str) -> str:
    raw = f"{hostname}::{username}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


class WindowBuffer:
    """Аккумулирует время по уникальным (app_name, title)."""

    def __init__(self) -> None:
        self._buf: dict[tuple[str, str], int] = {}
        self._first_seen: dict[tuple[str, str], datetime] = {}

    def add(self, app_name: str, title: str, seconds: int) -> None:
        key = (app_name, title)
        self._buf[key] = self._buf.get(key, 0) + seconds
        if key not in self._first_seen:
            self._first_seen[key] = datetime.now(timezone.utc)

    def flush(self) -> list[dict]:
        result = [
            {
                "app_name": app,
                "title": title,
                "captured_at": self._first_seen[(app, title)].isoformat(),
                "duration_seconds": secs,
            }
            for (app, title), secs in self._buf.items()
        ]
        self._buf.clear()
        self._first_seen.clear()
        return result


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
        return True
    except Exception as e:
        log.warning("heartbeat failed: %s", e)
        return False


def send_window_samples(client: httpx.Client, agent_id: str, samples: list[dict]) -> bool:
    if not samples:
        return True
    try:
        r = client.post(
            f"{SERVER_URL}/window_samples",
            json={"agent_id": agent_id, "samples": samples},
            timeout=15.0,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log.warning("window_samples failed (%d samples buffered): %s", len(samples), e)
        return False


def get_active_meeting(client: httpx.Client, agent_id: str) -> dict | None:
    try:
        r = client.get(f"{SERVER_URL}/agents/{agent_id}/active_meeting", timeout=10.0)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning("active_meeting check failed: %s", e)
        return None


def upload_audio_chunk(client: httpx.Client, meeting_id: int, chunk_index: int, wav_bytes: bytes) -> bool:
    try:
        r = client.post(
            f"{SERVER_URL}/meetings/{meeting_id}/audio",
            files={"file": (f"chunk_{chunk_index:04d}.wav", wav_bytes, "audio/wav")},
            data={"chunk_index": str(chunk_index)},
            timeout=60.0,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log.warning("upload audio chunk %d failed: %s", chunk_index, e)
        return False


def main() -> None:
    hostname = socket.gethostname()
    username = getpass.getuser()
    agent_id = make_agent_id(hostname, username)

    log.info(
        "agent starting: version=%s os=%s hostname=%s username=%s agent_id=%s server=%s sample=%ds flush=%ds",
        AGENT_VERSION,
        platform.platform(),
        hostname,
        username,
        agent_id,
        SERVER_URL,
        SAMPLE_INTERVAL,
        FLUSH_INTERVAL,
    )

    buffer = WindowBuffer()
    recorder = AudioRecorder()
    audio_enabled = os.environ.get("OM_ENABLE_AUDIO", "1") == "1"
    last_flush = time.monotonic()
    debug_sample = os.environ.get("OM_DEBUG_SAMPLE", "0") == "1"

    with httpx.Client() as client:
        while True:
            window = get_active_window()
            if window is not None:
                buffer.add(window["app_name"], window["title"], SAMPLE_INTERVAL)
                if debug_sample:
                    log.info("sample: app=%r title=%r", window["app_name"], window["title"])

            now = time.monotonic()
            if now - last_flush >= FLUSH_INTERVAL:
                samples = buffer.flush()
                ok_hb = send_heartbeat(client, agent_id, hostname, username)
                ok_ws = send_window_samples(client, agent_id, samples)

                # --- встреча: включаем/выключаем запись микрофона по сигналу с сервера ---
                audio_info = ""
                if audio_enabled:
                    am = get_active_meeting(client, agent_id)
                    if am and am.get("active"):
                        if not recorder.is_recording():
                            recorder.start(int(am["meeting_id"]))
                    elif recorder.is_recording():
                        recorder.stop()

                    if recorder.is_recording() or recorder.meeting_id is not None:
                        chunks = recorder.drain()
                        if chunks and recorder.meeting_id is not None:
                            ok_chunks = sum(
                                1 for idx, wav in chunks
                                if upload_audio_chunk(client, recorder.meeting_id, idx, wav)
                            )
                            audio_info = f" audio_chunks={ok_chunks}/{len(chunks)}"

                if samples:
                    apps_summary = ", ".join(f"{s['app_name']}={s['duration_seconds']}s" for s in samples)
                    log.info("flush: hb=%s samples=%d ok=%s rec=%s [%s]%s",
                             ok_hb, len(samples), ok_ws, recorder.is_recording(), apps_summary, audio_info)
                else:
                    log.info("flush: hb=%s samples=0 ok=%s rec=%s%s",
                             ok_hb, ok_ws, recorder.is_recording(), audio_info)
                last_flush = now

            time.sleep(SAMPLE_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("agent stopped by user")
