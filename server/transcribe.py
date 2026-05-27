"""
Транскрипция аудио: Gemini 2.5 Flash (через OpenRouter) + fallback на faster-whisper.

По умолчанию OM_TRANSCRIBE_BACKEND=gemini — облачный, качественный, не грузит CPU.
Если Gemini недоступен или env не задан — fallback на faster-whisper (small/int8).

Поддерживает:
- transcribe_meeting(meeting_id, chunk_paths) — склейка WAV-чанков встречи
- transcribe_voice_segment(opus_path) — один Opus-сегмент always-on микрофона
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import time
import wave
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx

log = logging.getLogger("worker")

TRANSCRIBE_BACKEND = os.environ.get("OM_TRANSCRIBE_BACKEND", "gemini")  # gemini | whisper
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_KEY = os.environ.get("OM_OPENROUTER_API_KEY", "")
GEMINI_MODEL = os.environ.get("OM_TRANSCRIBE_MODEL", "google/gemini-2.5-flash")

WHISPER_MODEL = os.environ.get("OM_WHISPER_MODEL", "small")
WHISPER_COMPUTE_TYPE = os.environ.get("OM_WHISPER_COMPUTE_TYPE", "int8")
WHISPER_LANGUAGE = os.environ.get("OM_WHISPER_LANGUAGE", "ru")

GEMINI_SYSTEM = """Ты — точный транскрибатор русскоязычной речи, записанной с
микрофона ноутбука в офисе. Запись может содержать одного или нескольких
говорящих (менеджер, коллега, клиент).

ПРАВИЛА:
1. Выводи ТОЛЬКО транскрипт. Без преамбул, без комментариев, без анализа.
2. Если слышно несколько говорящих — разделяй их по ролям:
   «Менеджер:», «Клиент:», «Коллега:», «Говорящий:».
   Если один — просто текст без меток.
3. Не исправляй грамматику и слова-паразиты — пиши как сказано.
4. Длительные паузы (>5 сек) обозначай [пауза].
5. Фоновый шум, телефонные звонки и т.п. — игнорируй, пиши только речь.
6. Тематика: юридические консультации по освобождению от армии (53-ФЗ).
   Термины: военкомат, повестка, ВВК, медкомиссия, категория годности,
   отсрочка, расписание болезней, КМО.
7. Если аудио — тишина или неразборчивый шум, верни пустую строку."""

GEMINI_PROMPT = """Расшифруй эту аудиозапись. Только текст, без комментариев."""


def _audio_format_from_ext(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".wav": "wav", ".mp3": "mp3", ".ogg": "ogg",
        ".opus": "ogg", ".flac": "flac", ".m4a": "m4a",
    }.get(ext, "wav")


# ── Gemini через OpenRouter ──

def _transcribe_gemini(audio_bytes: bytes, audio_format: str) -> dict:
    """Транскрибирует через Gemini 2.5 Flash (OpenRouter)."""
    if not OPENROUTER_KEY:
        raise RuntimeError("OM_OPENROUTER_API_KEY не задан")

    audio_b64 = base64.b64encode(audio_bytes).decode("utf-8")
    t0 = time.monotonic()

    with httpx.Client(timeout=120.0) as client:
        r = client.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://office.lkdzrkk.pro",
                "X-Title": "Office Monitoring - Transcription",
            },
            json={
                "model": GEMINI_MODEL,
                "messages": [
                    {"role": "system", "content": GEMINI_SYSTEM},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": GEMINI_PROMPT},
                            {
                                "type": "input_audio",
                                "input_audio": {"data": audio_b64, "format": audio_format},
                            },
                        ],
                    },
                ],
                "temperature": 0.0,
                "max_tokens": 4096,
            },
        )
        r.raise_for_status()
        text = (r.json()["choices"][0]["message"]["content"] or "").strip()

    elapsed = time.monotonic() - t0

    if _looks_like_hallucination(text):
        text = ""

    return {
        "text": text,
        "language": "ru",
        "processing_time_seconds": elapsed,
    }


# ── faster-whisper (fallback) ──

_whisper_model = None


def _get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel  # type: ignore
        log.info("loading whisper model=%s compute_type=%s", WHISPER_MODEL, WHISPER_COMPUTE_TYPE)
        t0 = time.monotonic()
        _whisper_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type=WHISPER_COMPUTE_TYPE)
        log.info("model loaded in %.1f s", time.monotonic() - t0)
    return _whisper_model


def _transcribe_whisper(path: Path, vad_filter: bool = True) -> dict:
    model = _get_whisper_model()
    t0 = time.monotonic()
    segments, info = model.transcribe(
        str(path),
        language=WHISPER_LANGUAGE if WHISPER_LANGUAGE else None,
        vad_filter=vad_filter,
        vad_parameters={"min_silence_duration_ms": 500} if vad_filter else None,
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
        if _looks_like_hallucination(seg_text):
            log.debug("отбросили галлюцинацию: %r", seg_text)
            continue
        parts.append(seg_text)
    text = " ".join(parts).strip()
    if _looks_like_hallucination(text):
        text = ""
    return {
        "text": text,
        "language": info.language,
        "processing_time_seconds": time.monotonic() - t0,
    }


# ── Общие утилиты ──

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
    if len(t) < 80:
        for pat in _HALLUCINATION_PATTERNS:
            if pat in t:
                return True
    words = t.split()
    if len(words) >= 10:
        unique_ratio = len(set(words)) / len(words)
        if unique_ratio < 0.25:
            return True
        for n in (2, 3, 4):
            if len(words) < n * 3:
                continue
            ngrams = [" ".join(words[i:i + n]) for i in range(len(words) - n + 1)]
            top_ngram, top_count = Counter(ngrams).most_common(1)[0]
            if top_count >= 3 and (top_count * n) / len(words) > 0.4:
                return True
    return False


def _concat_wav_chunks(chunk_paths: list[Path]) -> tuple[bytes, float]:
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


# ── Публичный API ──

def _transcribe_path(path: Path, vad_filter: bool = True) -> dict:
    """Универсальная точка входа: Gemini → fallback Whisper."""
    if TRANSCRIBE_BACKEND == "gemini" and OPENROUTER_KEY:
        try:
            audio_bytes = path.read_bytes()
            fmt = _audio_format_from_ext(path)
            result = _transcribe_gemini(audio_bytes, fmt)
            log.info("gemini transcription: %.1fs, %d chars",
                     result["processing_time_seconds"], len(result["text"]))
            return result
        except Exception as e:
            log.warning("gemini failed, fallback to whisper: %s", e)
    return _transcribe_whisper(path, vad_filter=vad_filter)


def transcribe_meeting(meeting_id: int, chunk_paths: list[Path]) -> dict:
    if not chunk_paths:
        raise ValueError("нет чанков для транскрипции")
    log.info("meeting %d: склеиваем %d чанков", meeting_id, len(chunk_paths))
    wav_bytes, duration = _concat_wav_chunks(chunk_paths)
    tmp_path = chunk_paths[0].parent / "_concat.wav"
    tmp_path.write_bytes(wav_bytes)
    log.info("meeting %d: аудио %.1f с, %d байт", meeting_id, duration, len(wav_bytes))
    try:
        result = _transcribe_path(tmp_path, vad_filter=True)
        log.info("meeting %d: транскрипт %.1f с обработки, %d символов",
                 meeting_id, result["processing_time_seconds"], len(result["text"]))
        result["duration_seconds"] = duration
        return result
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def transcribe_voice_segment(opus_path: Path) -> dict:
    return _transcribe_path(opus_path, vad_filter=False)
