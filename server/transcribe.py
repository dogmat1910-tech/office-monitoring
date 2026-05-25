"""
Транскрипция аудио-чанков встречи через faster-whisper.

Подход:
1. Собираем все wav-чанки встречи по chunk_index ASC.
2. Конкатенируем raw PCM (все чанки 16kHz mono int16, склейка тривиальна).
3. Загружаем во временный WAV-файл.
4. Transcribe через faster-whisper.
5. Сохраняем Transcript в БД.

Модель: small с int8 квантизацией (~500 MB RAM, ~2-3x slower than realtime на 2 ядрах CPU).
Подходит для русского в большинстве случаев. Можно подменить через env OM_WHISPER_MODEL.
"""

from __future__ import annotations

import io
import logging
import os
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("worker")

WHISPER_MODEL = os.environ.get("OM_WHISPER_MODEL", "small")
WHISPER_COMPUTE_TYPE = os.environ.get("OM_WHISPER_COMPUTE_TYPE", "int8")
WHISPER_LANGUAGE = os.environ.get("OM_WHISPER_LANGUAGE", "ru")

_model = None


def get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel  # type: ignore
        log.info("loading whisper model=%s compute_type=%s", WHISPER_MODEL, WHISPER_COMPUTE_TYPE)
        t0 = time.monotonic()
        _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type=WHISPER_COMPUTE_TYPE)
        log.info("model loaded in %.1f s", time.monotonic() - t0)
    return _model


def _concat_wav_chunks(chunk_paths: list[Path]) -> tuple[bytes, float]:
    """Склеивает несколько WAV-файлов с одинаковыми параметрами в один WAV.
    Возвращает (wav_bytes, duration_seconds)."""
    if not chunk_paths:
        raise ValueError("no chunks")

    pcm_data = bytearray()
    rate = ch = sw = None
    for p in chunk_paths:
        with wave.open(str(p), "rb") as r:
            if rate is None:
                rate = r.getframerate()
                ch = r.getnchannels()
                sw = r.getsampwidth()
            pcm_data.extend(r.readframes(r.getnframes()))

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(sw)
        w.setframerate(rate)
        w.writeframes(pcm_data)
    duration = len(pcm_data) / (rate * ch * sw)
    return buf.getvalue(), duration


def transcribe_meeting(meeting_id: int, chunk_paths: list[Path]) -> dict:
    """Возвращает {text, language, duration_seconds, processing_time_seconds}.
    Бросает исключение при ошибке."""
    if not chunk_paths:
        raise ValueError("нет чанков для транскрипции")

    log.info("meeting %d: склеиваем %d чанков", meeting_id, len(chunk_paths))
    wav_bytes, duration = _concat_wav_chunks(chunk_paths)

    # сохраняем во временный файл — faster-whisper берёт путь
    tmp_path = chunk_paths[0].parent / "_concat.wav"
    tmp_path.write_bytes(wav_bytes)
    log.info("meeting %d: aудио %.1f с, %d байт, путь=%s", meeting_id, duration, len(wav_bytes), tmp_path)

    try:
        model = get_model()
        t0 = time.monotonic()
        segments, info = model.transcribe(
            str(tmp_path),
            language=WHISPER_LANGUAGE if WHISPER_LANGUAGE else None,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
        )
        text_parts = [seg.text.strip() for seg in segments]
        full_text = " ".join(text_parts).strip()
        elapsed = time.monotonic() - t0
        log.info("meeting %d: транскрипт %.1f с обработки, %d символов, lang=%s",
                 meeting_id, elapsed, len(full_text), info.language)
        return {
            "text": full_text,
            "language": info.language,
            "duration_seconds": duration,
            "processing_time_seconds": elapsed,
        }
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass
