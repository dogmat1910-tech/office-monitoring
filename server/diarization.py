"""
Speaker diarization для Conversation через pyannote.audio.

Алгоритм:
1. Берём все voice_segment'ы одной conversation
2. Склеиваем их PCM в один WAV-файл (с тишиной между для сохранения timeline)
3. Прогоняем через pyannote pipeline → получаем (speaker_label, start, end)
4. Для каждого voice_segment ищем speaker, который занимал максимум времени
   в его относительном окне → сохраняем в voice_segment.speaker_label
5. На уровне Conversation сохраняем speakers_count и speakers_timeline_json

Требует:
- OM_HF_TOKEN — HuggingFace token с доступом к pyannote/speaker-diarization-3.1
- Модель скачивается при первом запуске (~500 MB)
"""

from __future__ import annotations

import io
import logging
import os
import tempfile
import time
import wave
from pathlib import Path

log = logging.getLogger("worker")

HF_TOKEN = os.environ.get("OM_HF_TOKEN", "")
MODEL_NAME = os.environ.get("OM_DIARIZATION_MODEL", "pyannote/speaker-diarization-3.1")

_pipeline = None
_load_failed = False


def get_pipeline():
    """Lazy-load pyannote pipeline. Возвращает None если HF_TOKEN не задан или модель не доступна."""
    global _pipeline, _load_failed
    if _pipeline is not None:
        return _pipeline
    if _load_failed:
        return None
    if not HF_TOKEN:
        log.warning("OM_HF_TOKEN не задан — diarization отключён")
        _load_failed = True
        return None
    try:
        from pyannote.audio import Pipeline  # type: ignore
        import torch  # type: ignore
        log.info("loading diarization pipeline %s", MODEL_NAME)
        t0 = time.monotonic()
        _pipeline = Pipeline.from_pretrained(MODEL_NAME, use_auth_token=HF_TOKEN)
        _pipeline.to(torch.device("cpu"))
        log.info("diarization pipeline loaded in %.1fs", time.monotonic() - t0)
        return _pipeline
    except Exception as e:
        log.exception("ошибка загрузки diarization pipeline: %s", e)
        _load_failed = True
        return None


def _concat_opus_to_wav(segment_paths_with_offsets: list[tuple[Path, float, float]],
                       total_duration_sec: float) -> bytes:
    """Склеивает Opus-сегменты в один WAV 16kHz mono, сохраняя относительные timestamps.
    Между сегментами заполняем тишиной.

    segment_paths_with_offsets: [(opus_path, start_offset_sec, end_offset_sec), ...]
    start_offset_sec — секунды от начала conversation
    """
    import numpy as np  # type: ignore
    import soundfile as sf  # type: ignore

    SR = 16000
    total_samples = int(total_duration_sec * SR) + SR  # +1 сек запас
    canvas = np.zeros(total_samples, dtype=np.int16)

    for path, start_off, end_off in segment_paths_with_offsets:
        try:
            audio, sr = sf.read(str(path), dtype="int16")
            if sr != SR:
                # ресэмплинг через scipy если нужно — но у нас Opus 16kHz, должно совпадать
                continue
            start_sample = int(start_off * SR)
            end_sample = min(start_sample + len(audio), total_samples)
            audio_truncated = audio[:end_sample - start_sample]
            canvas[start_sample:start_sample + len(audio_truncated)] = audio_truncated
        except Exception as e:
            log.warning("не удалось прочитать %s: %s", path, e)
            continue

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(canvas.tobytes())
    return buf.getvalue()


def diarize_audio_file(wav_path: Path, num_speakers: int | None = None) -> list[dict]:
    """Прогоняет WAV через pyannote и возвращает [{"speaker", "start", "end"}, ...]."""
    pipe = get_pipeline()
    if pipe is None:
        return []

    kwargs = {}
    if num_speakers is not None:
        kwargs["num_speakers"] = num_speakers

    t0 = time.monotonic()
    try:
        diarization = pipe(str(wav_path), **kwargs)
    except Exception as e:
        log.exception("diarization упала: %s", e)
        return []

    result = []
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        result.append({
            "speaker": str(speaker),
            "start": float(turn.start),
            "end": float(turn.end),
        })
    log.info("diarized %d turns in %.1fs", len(result), time.monotonic() - t0)
    return result


def diarize_conversation(conversation_id: int) -> dict:
    """Полный pipeline: получаем сегменты из БД, склеиваем, диаризуем, сохраняем.
    Возвращает {"speakers_count", "timeline", "segment_speakers": [(seg_id, label)]}."""
    from sqlmodel import Session, select
    from main import VOICE_DIR, Conversation, VoiceSegment, engine, _as_utc

    with Session(engine) as session:
        conv = session.exec(select(Conversation).where(Conversation.id == conversation_id)).first()
        if conv is None:
            raise ValueError(f"conversation {conversation_id} не найден")
        segs = session.exec(
            select(VoiceSegment).where(VoiceSegment.conversation_id == conversation_id).order_by(VoiceSegment.started_at)
        ).all()
        if not segs:
            return {"speakers_count": 0, "timeline": [], "segment_speakers": []}

        conv_start = _as_utc(conv.started_at)
        total_duration = (_as_utc(conv.ended_at) - conv_start).total_seconds()

        # подготавливаем segment_paths_with_offsets
        paths_offsets = []
        seg_relative_ranges = []
        for s in segs:
            full_path = VOICE_DIR / s.file_path
            if not full_path.exists():
                continue
            seg_start_off = (_as_utc(s.started_at) - conv_start).total_seconds()
            seg_end_off = (_as_utc(s.ended_at) - conv_start).total_seconds()
            paths_offsets.append((full_path, seg_start_off, seg_end_off))
            seg_relative_ranges.append((s.id, seg_start_off, seg_end_off))

        if not paths_offsets:
            return {"speakers_count": 0, "timeline": [], "segment_speakers": []}

    # склейка
    wav_bytes = _concat_opus_to_wav(paths_offsets, total_duration)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(wav_bytes)
        tmp_path = Path(tmp.name)

    try:
        # диаризация — для коротких разговоров (<5 сегментов) можем оставить num_speakers=None
        turns = diarize_audio_file(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    if not turns:
        return {"speakers_count": 0, "timeline": [], "segment_speakers": []}

    # сопоставление: для каждого сегмента находим speaker который занимает максимум времени
    segment_speakers = []
    for seg_id, seg_start, seg_end in seg_relative_ranges:
        speaker_durations: dict[str, float] = {}
        for t in turns:
            overlap_start = max(seg_start, t["start"])
            overlap_end = min(seg_end, t["end"])
            overlap = max(0, overlap_end - overlap_start)
            if overlap > 0:
                speaker_durations[t["speaker"]] = speaker_durations.get(t["speaker"], 0) + overlap
        if speaker_durations:
            best = max(speaker_durations.items(), key=lambda x: x[1])
            segment_speakers.append((seg_id, best[0]))
        else:
            segment_speakers.append((seg_id, None))

    speakers_count = len({t["speaker"] for t in turns})
    return {
        "speakers_count": speakers_count,
        "timeline": turns,
        "segment_speakers": segment_speakers,
    }
