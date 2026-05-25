"""
Always-on рекордер: пишет микрофон весь рабочий день, режет на сегменты
по VAD (тишина → конец сегмента), кодирует в Opus, шлёт на сервер.

Pause: создай файл OM_PAUSE_FILE (по умолчанию ~/.office-monitoring/PAUSE)
       — рекордер перестанет писать и появится в дашборде как «⏸ пауза».
       На этапе 10E будет настоящая tray-кнопка.

Рабочие часы: OM_WORK_HOURS=9-18 — пишем только в этом диапазоне (по local time).
"""

from __future__ import annotations

import io
import logging
import os
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("agent")

SAMPLE_RATE = 16000
FRAME_MS = 30  # webrtcvad принимает 10/20/30ms
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 480
FRAME_BYTES = FRAME_SAMPLES * 2  # int16 = 2 bytes/sample

VAD_AGGRESSIVENESS = int(os.environ.get("OM_VAD_AGGRESSIVENESS", "2"))  # 0-3
SILENCE_TO_END_MS = int(os.environ.get("OM_SILENCE_TO_END_MS", "1500"))
MIN_SEGMENT_MS = int(os.environ.get("OM_MIN_SEGMENT_MS", "1000"))
MAX_SEGMENT_MS = int(os.environ.get("OM_MAX_SEGMENT_MS", "60000"))

PAUSE_FILE = Path(os.environ.get(
    "OM_PAUSE_FILE",
    str(Path.home() / ".office-monitoring" / "PAUSE"),
))

# OM_WORK_HOURS=9-18  (включительно по часам, по локальному времени)
_work_hours_env = os.environ.get("OM_WORK_HOURS", "")
if "-" in _work_hours_env:
    _h_from, _h_to = _work_hours_env.split("-", 1)
    WORK_HOURS_FROM = int(_h_from)
    WORK_HOURS_TO = int(_h_to)
else:
    WORK_HOURS_FROM = 0
    WORK_HOURS_TO = 24


def _within_work_hours() -> bool:
    h = datetime.now().hour
    return WORK_HOURS_FROM <= h < WORK_HOURS_TO


class AlwaysOnRecorder:
    """
    send_callback(started_at: datetime, ended_at: datetime, opus_bytes: bytes) -> None
    """

    def __init__(self, send_callback) -> None:
        self.send_callback = send_callback
        self._stream = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._send_queue: queue.Queue = queue.Queue()
        self._sender_thread: threading.Thread | None = None
        self._vad = None  # ленивая инициализация

        # буфер 30-мс кадров (накапливается из callback'а)
        self._frame_buffer = bytearray()
        # активный сегмент речи
        self._segment_pcm = bytearray()
        self._segment_started_at: datetime | None = None
        self._silence_frames = 0

        self._status = "idle"  # idle | recording | paused | offhours

    @property
    def status(self) -> str:
        return self._status

    def _should_record(self) -> bool:
        if PAUSE_FILE.exists():
            self._status = "paused"
            return False
        if not _within_work_hours():
            self._status = "offhours"
            return False
        self._status = "recording"
        return True

    def start(self) -> bool:
        if self._stream is not None:
            return True
        try:
            import numpy as np  # noqa: F401
            import sounddevice as sd  # type: ignore
            import webrtcvad  # type: ignore
        except ImportError as e:
            log.warning("always-on: зависимости не установлены (%s) — рекордер отключён", e)
            return False

        self._vad = webrtcvad.Vad(VAD_AGGRESSIVENESS)

        def _cb(indata, frames, time_info, status):  # noqa: ANN001
            if status:
                log.debug("always-on stream status: %s", status)
            if not self._should_record():
                # сбросим активный сегмент если есть — не накапливаем тишину
                with self._lock:
                    self._segment_pcm.clear()
                    self._segment_started_at = None
                    self._silence_frames = 0
                return
            data = bytes(indata)  # int16 mono → raw bytes
            with self._lock:
                self._frame_buffer.extend(data)
                while len(self._frame_buffer) >= FRAME_BYTES:
                    frame = bytes(self._frame_buffer[:FRAME_BYTES])
                    del self._frame_buffer[:FRAME_BYTES]
                    self._process_frame_locked(frame)

        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=0,
                callback=_cb,
            )
            self._stream.start()
        except Exception as e:
            log.warning("always-on: не удалось открыть микрофон: %s", e)
            self._stream = None
            return False

        self._sender_thread = threading.Thread(target=self._sender_loop, daemon=True)
        self._sender_thread.start()
        log.info("always-on: запущен (vad=%d silence=%dms max=%dms hours=%d-%d)",
                 VAD_AGGRESSIVENESS, SILENCE_TO_END_MS, MAX_SEGMENT_MS,
                 WORK_HOURS_FROM, WORK_HOURS_TO)
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception as e:
                log.warning("always-on stop error: %s", e)
            self._stream = None
        with self._lock:
            self._finalize_segment_locked()

    def _process_frame_locked(self, frame: bytes) -> None:
        is_speech = self._vad.is_speech(frame, SAMPLE_RATE) if self._vad else False
        if is_speech:
            if self._segment_started_at is None:
                self._segment_started_at = datetime.now(timezone.utc)
                self._segment_pcm.clear()
                self._silence_frames = 0
            self._segment_pcm.extend(frame)
            self._silence_frames = 0
            # принудительный split длинных сегментов
            seg_ms = len(self._segment_pcm) // (SAMPLE_RATE * 2 // 1000)
            if seg_ms >= MAX_SEGMENT_MS:
                self._finalize_segment_locked()
        else:
            if self._segment_started_at is not None:
                # держим хвост из тишины в сегменте, чтобы Whisper лучше слышал контекст
                self._segment_pcm.extend(frame)
                self._silence_frames += 1
                if self._silence_frames * FRAME_MS >= SILENCE_TO_END_MS:
                    self._finalize_segment_locked()

    def _finalize_segment_locked(self) -> None:
        if not self._segment_pcm or self._segment_started_at is None:
            self._segment_pcm.clear()
            self._segment_started_at = None
            self._silence_frames = 0
            return
        pcm = bytes(self._segment_pcm)
        started_at = self._segment_started_at
        self._segment_pcm.clear()
        self._segment_started_at = None
        self._silence_frames = 0

        duration_ms = len(pcm) // (SAMPLE_RATE * 2 // 1000)
        if duration_ms < MIN_SEGMENT_MS:
            return
        ended_at = datetime.now(timezone.utc)

        try:
            import numpy as np  # type: ignore
            import soundfile as sf  # type: ignore
            pcm_array = np.frombuffer(pcm, dtype=np.int16)
            buf = io.BytesIO()
            # Opus в OGG-контейнере; libsndfile >= 1.0.31
            sf.write(buf, pcm_array, SAMPLE_RATE, format="OGG", subtype="OPUS")
            opus_bytes = buf.getvalue()
        except Exception as e:
            log.warning("opus encode failed: %s (длина PCM=%d байт) — пропускаем", e, len(pcm))
            return

        self._send_queue.put((started_at, ended_at, opus_bytes))

    def _sender_loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._send_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            started_at, ended_at, opus_bytes = item
            try:
                self.send_callback(started_at, ended_at, opus_bytes)
            except Exception as e:
                log.warning("always-on send failed: %s", e)
