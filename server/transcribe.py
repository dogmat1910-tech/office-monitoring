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


# Типичные галлюцинации Whisper на тихих/шумных сегментах. Если транскрипт
# целиком состоит из такой фразы — выкидываем.
_HALLUCINATION_PATTERNS = [
    "спасибо за просмотр", "субтитры", "продолжение следует",
    "редактор субтитров", "ставьте лайки", "подписывайтесь",
    "до встречи", "всем пока", "thank you", "thanks for watching",
    "субтитлы делал", "субтитры делал", "корректор", "ух, ах",
]


def _looks_like_hallucination(text: str) -> bool:
    if not text:
        return True
    t = text.lower().strip(" .,!?-—\"'")
    # короткое целиком в чёрном списке
    if len(t) < 80:
        for pat in _HALLUCINATION_PATTERNS:
            if pat in t:
                # если в коротком тексте есть «триггер» галлюцинации — выкидываем
                return True
    return False


def _transcribe_path(path: Path, vad_filter: bool = True) -> dict:
    """Ядро транскрипции. Возвращает {text, language, processing_time_seconds}."""
    model = get_model()
    t0 = time.monotonic()
    segments, info = model.transcribe(
        str(path),
        language=WHISPER_LANGUAGE if WHISPER_LANGUAGE else None,
        vad_filter=vad_filter,
        vad_parameters={"min_silence_duration_ms": 500} if vad_filter else None,
        # Фильтры против галлюцинаций:
        # - выше no_speech_threshold = чаще считаем что это тишина (по умолчанию 0.6)
        # - log_prob_threshold отсекает сегменты с низкой уверенностью
        # - condition_on_previous_text=False — не цепляемся за предыдущий
        #   контекст (это часто и приводит к "субтитры... субтитры... субтитры")
        no_speech_threshold=0.7,
        log_prob_threshold=-1.0,
        condition_on_previous_text=False,
        temperature=0.0,
    )
    parts: list[str] = []
    for s in segments:
        seg_text = s.text.strip()
        if not seg_text:
            continue
        # вторичный фильтр на уровне каждого сегмента
        if _looks_like_hallucination(seg_text):
            log.debug("отбросили галлюцинацию: %r", seg_text)
            continue
        parts.append(seg_text)
    text = " ".join(parts).strip()
    # Финальная проверка: если после фильтра остался один короткий обрывок,
    # который тоже похож на галлюцинацию — выкидываем целиком
    if _looks_like_hallucination(text):
        text = ""
    return {
        "text": text,
        "language": info.language,
        "processing_time_seconds": time.monotonic() - t0,
    }


def transcribe_meeting(meeting_id: int, chunk_paths: list[Path]) -> dict:
    """Склеивает WAV-чанки встречи и транскрибирует."""
    if not chunk_paths:
        raise ValueError("нет чанков для транскрипции")

    log.info("meeting %d: склеиваем %d чанков", meeting_id, len(chunk_paths))
    wav_bytes, duration = _concat_wav_chunks(chunk_paths)

    tmp_path = chunk_paths[0].parent / "_concat.wav"
    tmp_path.write_bytes(wav_bytes)
    log.info("meeting %d: aудио %.1f с, %d байт, путь=%s", meeting_id, duration, len(wav_bytes), tmp_path)

    try:
        result = _transcribe_path(tmp_path, vad_filter=True)
        log.info("meeting %d: транскрипт %.1f с обработки, %d символов, lang=%s",
                 meeting_id, result["processing_time_seconds"], len(result["text"]), result["language"])
        result["duration_seconds"] = duration
        return result
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def transcribe_voice_segment(opus_path: Path) -> dict:
    """Транскрибирует один Opus-сегмент. VAD на агенте уже отфильтровал тишину,
    дополнительный VAD на whisper-стороне отключаем."""
    return _transcribe_path(opus_path, vad_filter=False)
