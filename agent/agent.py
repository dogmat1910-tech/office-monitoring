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
from pathlib import Path  # noqa: F811  # Path also used inside drain_buffer for blob path

import httpx

from active_window import get_active_window
from audio import AudioRecorder
from always_on_audio import AlwaysOnRecorder
from categories import CategoryResolver
from diagnostics import collect_diagnostics
from idle import get_idle_seconds
from keylogger import KeystrokeAggregator
from local_buffer import LocalBuffer
from screenshot import capture_primary_jpeg
from updater import check_and_apply_update

AGENT_VERSION = "0.9.3"
DIAGNOSTICS_INTERVAL_SEC = 3600  # раз в час
UPDATE_CHECK_INTERVAL_SEC = int(os.environ.get("OM_UPDATE_CHECK_SEC", "300"))  # 5 минут
BUFFER_DRAIN_BATCH = 20  # сколько накопленных запросов отправляем за один цикл

PERIODIC_PERSONAL_SEC = int(os.environ.get("OM_SCREENSHOT_PERSONAL_SEC", "300"))  # 5 мин
PERIODIC_NEUTRAL_SEC = int(os.environ.get("OM_SCREENSHOT_NEUTRAL_SEC", "600"))    # 10 мин
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


_buffer: LocalBuffer | None = None


def get_buffer() -> LocalBuffer:
    global _buffer
    if _buffer is None:
        _buffer = LocalBuffer()
    return _buffer


def send_with_buffer_json(client: httpx.Client, url: str, payload: dict, timeout: float = 10.0) -> bool:
    """POST JSON. При ошибке сохраняем в локальный буфер для повторной отправки."""
    try:
        r = client.post(url, json=payload, timeout=timeout)
        r.raise_for_status()
        return True
    except Exception as e:
        log.warning("POST %s failed (буферизую): %s", url, e)
        get_buffer().enqueue_json("POST", url, payload)
        return False


def send_with_buffer_multipart(client: httpx.Client, url: str, form: dict,
                                file_bytes: bytes, filename: str, content_type: str,
                                timeout: float = 60.0) -> bool:
    try:
        r = client.post(url, data=form, files={"file": (filename, file_bytes, content_type)}, timeout=timeout)
        r.raise_for_status()
        return True
    except Exception as e:
        log.warning("multipart POST %s failed (буферизую): %s", url, e)
        get_buffer().enqueue_multipart(url, form, file_bytes, filename, content_type)
        return False


def drain_buffer(client: httpx.Client) -> tuple[int, int]:
    """Пытается отправить накопленные запросы. Возвращает (отправлено, осталось)."""
    buf = get_buffer()
    items = buf.peek(BUFFER_DRAIN_BATCH)
    sent = 0
    for item in items:
        try:
            if item["kind"] == "json":
                r = client.post(item["url"], json=item["data"], timeout=15.0)
            else:
                blob_path = item["blob_path"]
                if not blob_path or not Path(blob_path).exists():
                    buf.delete(item["id"])
                    continue
                file_bytes = Path(blob_path).read_bytes()
                r = client.post(
                    item["url"],
                    data=item["data"],
                    files={"file": (item["blob_filename"], file_bytes, item["blob_content_type"])},
                    timeout=60.0,
                )
            r.raise_for_status()
            buf.delete(item["id"])
            sent += 1
        except Exception as e:
            buf.mark_error(item["id"], str(e))
            # дальше не пробуем — скорее всего и следующие упадут
            break
    return sent, buf.size()


def send_heartbeat(client: httpx.Client, agent_id: str, hostname: str, username: str) -> bool:
    # heartbeat буферизовать смысла нет — он самый частый и устаревает мгновенно.
    # Если сеть упала, шлём при восстановлении следующим тиком.
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
    return send_with_buffer_json(
        client, f"{SERVER_URL}/window_samples",
        {"agent_id": agent_id, "samples": samples}, timeout=15.0,
    )


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


def upload_idle_samples(client: httpx.Client, agent_id: str, samples: list[dict]) -> bool:
    if not samples:
        return True
    return send_with_buffer_json(
        client, f"{SERVER_URL}/idle_samples",
        {"agent_id": agent_id, "samples": samples},
    )


def upload_keystroke_samples(client: httpx.Client, agent_id: str, samples: list[dict]) -> bool:
    if not samples:
        return True
    return send_with_buffer_json(
        client, f"{SERVER_URL}/keystroke_samples",
        {"agent_id": agent_id, "samples": samples},
    )


def upload_diagnostics(client: httpx.Client, agent_id: str) -> bool:
    try:
        info = collect_diagnostics(AGENT_VERSION)
        r = client.post(
            f"{SERVER_URL}/diagnostics",
            json={"agent_id": agent_id, "info": info},
            timeout=15.0,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log.warning("upload diagnostics failed: %s", e)
        return False


def upload_screenshot(client: httpx.Client, agent_id: str, captured_at, app_name: str, title: str,
                      category: str, trigger: str, jpeg_bytes: bytes) -> bool:
    return send_with_buffer_multipart(
        client, f"{SERVER_URL}/screenshots",
        form={
            "agent_id": agent_id,
            "captured_at": captured_at.isoformat(),
            "app_name": app_name or "",
            "title": title or "",
            "category": category or "",
            "trigger": trigger or "",
        },
        file_bytes=jpeg_bytes,
        filename="screenshot.jpg",
        content_type="image/jpeg",
        timeout=30.0,
    )


def upload_voice_segment(client: httpx.Client, agent_id: str, started_at, ended_at, opus_bytes: bytes) -> bool:
    return send_with_buffer_multipart(
        client, f"{SERVER_URL}/voice_segments",
        form={
            "agent_id": agent_id,
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
        },
        file_bytes=opus_bytes,
        filename="seg.opus",
        content_type="audio/ogg",
        timeout=60.0,
    )


def _ensure_single_instance() -> None:
    """Если уже запущен другой agent.exe (по PID-файлу или поиску процесса) — выходим.

    Защита от случая, когда при логине срабатывают одновременно Scheduled Task
    агента и watchdog: оба могут попытаться запустить агента.
    """
    try:
        import psutil
    except ImportError:
        return  # psutil не установлен — пропускаем проверку, не блокируем запуск

    my_pid = os.getpid()
    my_name = Path(sys.executable).name.lower()
    # При запуске из .exe — sys.executable == office-monitoring-agent.exe.
    # При запуске из python — это python.exe (dev-режим), не плодим дубли в проде.
    if not my_name.startswith("office-monitoring-agent"):
        return

    pid_file = LOG_DIR / "agent.pid"
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            if old_pid != my_pid and psutil.pid_exists(old_pid):
                try:
                    p = psutil.Process(old_pid)
                    if p.name().lower() == my_name:
                        log.warning("another agent already running (pid=%s), exiting", old_pid)
                        sys.exit(0)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except (ValueError, OSError):
            pass
    try:
        pid_file.write_text(str(my_pid))
    except OSError as e:
        log.warning("failed to write pid-file: %s", e)


def main() -> None:
    _ensure_single_instance()

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
    always_on_enabled = os.environ.get("OM_ENABLE_ALWAYS_ON_AUDIO", "0") == "1"
    keystrokes_enabled = os.environ.get("OM_ENABLE_KEYSTROKES", "1") == "1"
    idle_enabled = os.environ.get("OM_ENABLE_IDLE", "1") == "1"
    screenshots_enabled = os.environ.get("OM_ENABLE_SCREENSHOTS", "1") == "1"

    # категоризатор окон/доменов — для триггера скриншотов
    category_resolver = CategoryResolver(SERVER_URL)

    # триггеры скриншотов
    prev_category: str | None = None
    prev_window_key: str | None = None
    last_screenshot_at_monotonic: float = 0.0

    # текущее активное окно — нужно и для buffer и для keystroke aggregator
    current_window: dict = {}

    keystroke_agg = None
    if keystrokes_enabled:
        keystroke_agg = KeystrokeAggregator(get_window_fn=lambda: current_window)
        if not keystroke_agg.start():
            log.warning("keystroke aggregator не запустился — продолжаем без него")
            keystroke_agg = None

    idle_buffer: list[dict] = []
    last_flush = time.monotonic()
    debug_sample = os.environ.get("OM_DEBUG_SAMPLE", "0") == "1"

    # trust_env=False — игнорируем HTTP_PROXY/HTTPS_PROXY/ALL_PROXY (включая SOCKS).
    # Корпоративные ноутбуки иногда сидят за SOCKS-VPN (Outline/Shadowsocks), который
    # ломает httpx без отдельной либы. Наш агент ходит на свой сервер напрямую.
    with httpx.Client(trust_env=False) as client:
        # первичная загрузка категорий с сервера
        category_resolver.refresh(client)
        # стартовый снимок diagnostics
        upload_diagnostics(client, agent_id)
        last_diagnostics_mono = time.monotonic()
        last_update_check_mono = time.monotonic()

        # Always-on рекордер: пишет голосовые сегменты весь рабочий день.
        # Включается через OM_ENABLE_ALWAYS_ON_AUDIO=1.
        always_on = None
        if always_on_enabled:
            def _send_voice(started, ended, opus_bytes):
                upload_voice_segment(client, agent_id, started, ended, opus_bytes)
            always_on = AlwaysOnRecorder(send_callback=_send_voice)
            if not always_on.start():
                log.warning("always-on не запустился — продолжаем без него")
                always_on = None
        while True:
            window = get_active_window()
            if window is not None:
                current_window.clear()
                current_window.update(window)
                buffer.add(window["app_name"], window["title"], SAMPLE_INTERVAL)
                if debug_sample:
                    log.info("sample: app=%r title=%r", window["app_name"], window["title"])

                # триггер скриншотов
                if screenshots_enabled:
                    cat = category_resolver.categorize(window["app_name"], window["title"])
                    win_key = f"{window['app_name']}|{window.get('title', '')[:100]}"
                    now_mono = time.monotonic()
                    take = False
                    trigger = ""
                    if cat in ("personal", "neutral"):
                        # триггер 1: смена окна на personal/neutral
                        if win_key != prev_window_key and prev_category != cat:
                            take = True
                            trigger = "window_change"
                        # триггер 2: периодический в personal
                        elif cat == "personal" and (now_mono - last_screenshot_at_monotonic) >= PERIODIC_PERSONAL_SEC:
                            take = True
                            trigger = "periodic_personal"
                        # триггер 3: периодический в neutral
                        elif cat == "neutral" and (now_mono - last_screenshot_at_monotonic) >= PERIODIC_NEUTRAL_SEC:
                            take = True
                            trigger = "periodic_neutral"

                    if take:
                        jpeg = capture_primary_jpeg()
                        if jpeg:
                            upload_screenshot(client, agent_id,
                                              datetime.now(timezone.utc),
                                              window["app_name"], window.get("title", ""),
                                              cat, trigger, jpeg)
                            last_screenshot_at_monotonic = now_mono

                    prev_category = cat
                    prev_window_key = win_key

            # idle: фиксируем текущий idle (с момента последнего ввода)
            if idle_enabled:
                idle_s = get_idle_seconds()
                if idle_s is not None:
                    idle_buffer.append({
                        "captured_at": datetime.now(timezone.utc).isoformat(),
                        "idle_seconds": float(idle_s),
                        "interval_seconds": SAMPLE_INTERVAL,
                    })

            now = time.monotonic()
            if now - last_flush >= FLUSH_INTERVAL:
                samples = buffer.flush()
                ok_hb = send_heartbeat(client, agent_id, hostname, username)
                ok_ws = send_window_samples(client, agent_id, samples)

                # idle samples
                ok_idle = True
                if idle_buffer:
                    ok_idle = upload_idle_samples(client, agent_id, idle_buffer)
                    if ok_idle:
                        idle_buffer = []

                # keystrokes
                ok_ks = True
                ks_total = 0
                if keystroke_agg is not None:
                    ks = keystroke_agg.drain()
                    if ks:
                        now_iso = datetime.now(timezone.utc).isoformat()
                        ks_payload = [
                            {
                                "app_name": k["app_name"],
                                "domain": k["domain"],
                                "captured_at": now_iso,
                                "interval_seconds": FLUSH_INTERVAL,
                                "keystroke_count": k["count"],
                            }
                            for k in ks
                        ]
                        ks_total = sum(k["count"] for k in ks)
                        ok_ks = upload_keystroke_samples(client, agent_id, ks_payload)

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

                ao_info = f" ao={always_on.status}" if always_on else ""
                ks_info = f" ks={ks_total}" if keystroke_agg else ""
                idle_info = ""
                if idle_buffer == [] and idle_enabled:
                    # покажем последний idle
                    last_idle = get_idle_seconds()
                    if last_idle is not None:
                        idle_info = f" idle={last_idle:.0f}s"
                # drain буфера: пытаемся отправить накопленное при отвале сети
                drained, pending = drain_buffer(client)
                buf_info = f" buf={pending}" if pending or drained else ""
                if drained:
                    log.info("buffer drained: %d sent, %d pending", drained, pending)

                # diagnostics раз в час
                if time.monotonic() - last_diagnostics_mono >= DIAGNOSTICS_INTERVAL_SEC:
                    upload_diagnostics(client, agent_id)
                    last_diagnostics_mono = time.monotonic()

                # проверка обновлений раз в час
                if time.monotonic() - last_update_check_mono >= UPDATE_CHECK_INTERVAL_SEC:
                    if check_and_apply_update(client, SERVER_URL, AGENT_VERSION):
                        log.info("update applied — выхожу, watchdog перезапустит")
                        return  # watchdog поднимет новую версию
                    last_update_check_mono = time.monotonic()

                if samples:
                    apps_summary = ", ".join(f"{s['app_name']}={s['duration_seconds']}s" for s in samples)
                    log.info("flush: hb=%s samples=%d ok=%s rec=%s [%s]%s%s%s%s%s",
                             ok_hb, len(samples), ok_ws, recorder.is_recording(), apps_summary, audio_info, ao_info, ks_info, idle_info, buf_info)
                else:
                    log.info("flush: hb=%s samples=0 ok=%s rec=%s%s%s%s%s%s",
                             ok_hb, ok_ws, recorder.is_recording(), audio_info, ao_info, ks_info, idle_info, buf_info)
                last_flush = now

            time.sleep(SAMPLE_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("agent stopped by user")
