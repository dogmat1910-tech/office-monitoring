"""
Фоновой воркер транскрипции встреч.

Раз в POLL_INTERVAL опрашивает БД: ищет встречи где ended_at IS NOT NULL,
но нет Transcript. Берёт первую, транскрибирует, сохраняет.

Запускается отдельным systemd-сервисом office-monitoring-worker.service.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlmodel import Session, select

# импорт моделей и engine из main.py
from main import AUDIO_DIR, VOICE_DIR, Analysis, AudioChunk, Meeting, Transcript, VoiceSegment, engine
from analyze import analyze_transcript
from transcribe import get_model, transcribe_meeting, transcribe_voice_segment

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("worker")

POLL_INTERVAL = int(os.environ.get("OM_WORKER_POLL_INTERVAL", "10"))


def find_pending_transcription() -> int | None:
    """Закрытая встреча без транскрипта."""
    with Session(engine) as session:
        subq = select(Transcript.meeting_id)
        meeting = session.exec(
            select(Meeting)
            .where(Meeting.ended_at.is_not(None))
            .where(~Meeting.id.in_(subq))
            .order_by(Meeting.ended_at)
        ).first()
        return meeting.id if meeting else None


def find_pending_analysis() -> int | None:
    """Транскрибированная встреча без LLM-анализа (и с непустым текстом)."""
    with Session(engine) as session:
        subq = select(Analysis.meeting_id)
        transcript = session.exec(
            select(Transcript)
            .where(~Transcript.meeting_id.in_(subq))
            .where(Transcript.text != "")
            .order_by(Transcript.transcribed_at)
        ).first()
        return transcript.meeting_id if transcript else None


def find_pending_voice_segment() -> int | None:
    """VoiceSegment без транскрипции."""
    with Session(engine) as session:
        seg = session.exec(
            select(VoiceSegment)
            .where(VoiceSegment.text.is_(None))
            .order_by(VoiceSegment.started_at)
        ).first()
        return seg.id if seg else None


def process_voice_segment(segment_id: int) -> None:
    with Session(engine) as session:
        seg = session.exec(select(VoiceSegment).where(VoiceSegment.id == segment_id)).first()
        if seg is None:
            return
        opus_path = VOICE_DIR / seg.file_path
        now = datetime.now(timezone.utc)
        if not opus_path.exists():
            log.warning("voice_segment %d: файл %s не найден", segment_id, opus_path)
            seg.text = "[файл не найден]"
            seg.transcribed_at = now
            session.add(seg)
            session.commit()
            return

        try:
            log.info("voice_segment %d: транскрипция (%.1fs)", segment_id, seg.duration_seconds)
            result = transcribe_voice_segment(opus_path)
            seg.text = result["text"]
            seg.language = result.get("language")
            seg.transcribed_at = now
            session.add(seg)
            session.commit()
            log.info("voice_segment %d: %.1fs обработано, %d символов",
                     segment_id, result["processing_time_seconds"], len(result["text"]))
        except Exception as e:
            log.exception("voice_segment %d: ошибка: %s", segment_id, e)
            seg.text = f"[ошибка транскрипции: {e}]"
            seg.transcribed_at = now
            session.add(seg)
            session.commit()


def get_chunk_paths(meeting_id: int) -> list[Path]:
    with Session(engine) as session:
        chunks = session.exec(
            select(AudioChunk)
            .where(AudioChunk.meeting_id == meeting_id)
            .order_by(AudioChunk.chunk_index)
        ).all()
        return [AUDIO_DIR / c.file_path for c in chunks if (AUDIO_DIR / c.file_path).exists()]


def save_transcript(meeting_id: int, result: dict) -> None:
    with Session(engine) as session:
        t = Transcript(
            meeting_id=meeting_id,
            text=result["text"],
            language=result.get("language"),
            model=os.environ.get("OM_WHISPER_MODEL", "small"),
            duration_seconds=result.get("duration_seconds"),
            transcribed_at=datetime.now(timezone.utc),
            processing_time_seconds=result.get("processing_time_seconds"),
        )
        session.add(t)
        session.commit()


def save_analysis(meeting_id: int, payload: dict) -> None:
    import json as _json
    meta = payload.pop("_meta", {})
    with Session(engine) as session:
        a = Analysis(
            meeting_id=meeting_id,
            payload_json=_json.dumps(payload, ensure_ascii=False),
            final_score=payload.get("final_score"),
            model=meta.get("model", os.environ.get("OM_LLM_MODEL", "?")),
            analyzed_at=datetime.now(timezone.utc),
            processing_time_seconds=meta.get("processing_time_seconds"),
        )
        session.add(a)
        session.commit()


def get_transcript_text(meeting_id: int) -> str | None:
    with Session(engine) as session:
        t = session.exec(select(Transcript).where(Transcript.meeting_id == meeting_id)).first()
        return t.text if t else None


def process_one() -> bool:
    """Обрабатывает одну задачу. Транскрипция приоритетнее анализа."""
    # Этап 1: транскрипция
    meeting_id = find_pending_transcription()
    if meeting_id is not None:
        paths = get_chunk_paths(meeting_id)
        if not paths:
            log.warning("meeting %d закрыта, но нет audio chunks — пустой транскрипт", meeting_id)
            save_transcript(meeting_id, {"text": "", "language": None, "duration_seconds": 0.0, "processing_time_seconds": 0.0})
            return True
        try:
            log.info("meeting %d: старт транскрипции", meeting_id)
            result = transcribe_meeting(meeting_id, paths)
            save_transcript(meeting_id, result)
            log.info("meeting %d: транскрипт сохранён", meeting_id)
            return True
        except Exception as e:
            log.exception("meeting %d: ошибка транскрипции: %s", meeting_id, e)
            save_transcript(meeting_id, {
                "text": f"[ошибка транскрипции: {e}]",
                "language": None,
                "duration_seconds": 0.0,
                "processing_time_seconds": 0.0,
            })
            return True

    # Этап 2: транскрипция voice-сегментов (always-on)
    seg_id = find_pending_voice_segment()
    if seg_id is not None:
        process_voice_segment(seg_id)
        return True

    # Этап 3: LLM-анализ
    meeting_id = find_pending_analysis()
    if meeting_id is not None:
        text = get_transcript_text(meeting_id)
        if not text:
            return False
        try:
            log.info("meeting %d: старт LLM-анализа", meeting_id)
            payload = analyze_transcript(text)
            save_analysis(meeting_id, payload)
            log.info("meeting %d: анализ сохранён (score=%s)", meeting_id, payload.get("final_score"))
            return True
        except Exception as e:
            log.exception("meeting %d: ошибка LLM-анализа: %s", meeting_id, e)
            # отмечаем что пробовали — иначе зациклимся
            save_analysis(meeting_id, {
                "summary": f"[ошибка анализа: {e}]",
                "final_score": None,
                "checklist": {},
                "critical_errors": [],
                "strengths": [],
                "growth_areas": [],
                "_meta": {"model": "error", "processing_time_seconds": 0.0},
            })
            return True

    return False


def main() -> None:
    log.info("worker starting, poll_interval=%d s", POLL_INTERVAL)
    # прогреваем модель один раз — иначе первая транскрипция будет долго
    get_model()
    log.info("worker готов, ждём встречи")

    while True:
        try:
            did_work = process_one()
            if not did_work:
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            log.info("worker stopped by user")
            break
        except Exception as e:
            log.exception("worker loop error: %s", e)
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
