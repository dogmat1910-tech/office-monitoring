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
from main import AUDIO_DIR, VOICE_DIR, Analysis, AudioChunk, DailyReport, Meeting, Transcript, VoiceSegment, engine
from analyze import analyze_transcript
from classify_voice import auto_bind_meeting_id, classify_voice_segment
from daily_report import generate_daily_report
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


def find_pending_voice_classification() -> int | None:
    """VoiceSegment транскрибированный, но ещё не классифицированный.
    Игнорируем пустые транскрипты и явные ошибки."""
    with Session(engine) as session:
        seg = session.exec(
            select(VoiceSegment)
            .where(VoiceSegment.text.is_not(None))
            .where(VoiceSegment.kind.is_(None))
            .where(VoiceSegment.text != "")
            .order_by(VoiceSegment.started_at)
        ).first()
        return seg.id if seg else None


def process_voice_classification(segment_id: int) -> None:
    """LLM классифицирует kind + summary, плюс auto-bind к встрече по времени."""
    with Session(engine) as session:
        seg = session.exec(select(VoiceSegment).where(VoiceSegment.id == segment_id)).first()
        if seg is None:
            return
        # пропускаем явные ошибки транскрипции / служебные тексты
        if seg.text and (seg.text.startswith("[ошибка") or seg.text.startswith("[файл")):
            seg.kind = "noise"
            seg.kind_summary = "не классифицировано (ошибка транскрипции)"
            seg.classified_at = datetime.now(timezone.utc)
            session.add(seg)
            session.commit()
            return
    # auto-bind к встрече (бесплатно, не LLM)
    bound_meeting = auto_bind_meeting_id(segment_id)

    try:
        log.info("voice_segment %d: классификация", segment_id)
        result = classify_voice_segment(segment_id)
        with Session(engine) as session:
            seg = session.exec(select(VoiceSegment).where(VoiceSegment.id == segment_id)).first()
            if seg is None:
                return
            seg.kind = result.get("kind")
            seg.kind_summary = result.get("summary")
            seg.kind_confidence = result.get("confidence")
            seg.classified_at = datetime.now(timezone.utc)
            if bound_meeting is not None:
                seg.meeting_id = bound_meeting
            session.add(seg)
            session.commit()
        log.info("voice_segment %d: kind=%s confidence=%s meeting_id=%s",
                 segment_id, result.get("kind"), result.get("confidence"), bound_meeting)
    except Exception as e:
        log.exception("voice_segment %d: ошибка классификации: %s", segment_id, e)
        with Session(engine) as session:
            seg = session.exec(select(VoiceSegment).where(VoiceSegment.id == segment_id)).first()
            if seg:
                seg.kind = "other_speech"
                seg.kind_summary = f"[ошибка классификации: {e}]"[:200]
                seg.classified_at = datetime.now(timezone.utc)
                if bound_meeting is not None:
                    seg.meeting_id = bound_meeting
                session.add(seg)
                session.commit()


def find_pending_daily_report() -> int | None:
    with Session(engine) as session:
        rep = session.exec(
            select(DailyReport).where(DailyReport.status == "pending").order_by(DailyReport.requested_at)
        ).first()
        return rep.id if rep else None


def process_daily_report(report_id: int) -> None:
    import json as _json
    with Session(engine) as session:
        rep = session.exec(select(DailyReport).where(DailyReport.id == report_id)).first()
        if rep is None:
            return
        log.info("daily_report %s/%s: старт генерации", rep.agent_id, rep.report_date)
        try:
            result = generate_daily_report(rep.agent_id, rep.report_date)
            meta = result.pop("_meta", {})
            rep.status = "done"
            rep.payload_json = _json.dumps(result, ensure_ascii=False)
            rep.productivity_score = result.get("productivity_score")
            rep.model = meta.get("model")
            rep.processing_time_seconds = meta.get("processing_time_seconds")
            rep.completed_at = datetime.now(timezone.utc)
            session.add(rep)
            session.commit()
            log.info("daily_report %s/%s: готов (score=%s)", rep.agent_id, rep.report_date, rep.productivity_score)
        except Exception as e:
            log.exception("daily_report %s/%s: ошибка: %s", rep.agent_id, rep.report_date, e)
            rep.status = "error"
            rep.error_message = str(e)[:500]
            rep.completed_at = datetime.now(timezone.utc)
            session.add(rep)
            session.commit()


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

    # Этап 2.5: классификация транскрибированных, но ещё не классифицированных сегментов
    seg_id = find_pending_voice_classification()
    if seg_id is not None:
        process_voice_classification(seg_id)
        return True

    # Этап 3: pending daily reports
    pending_report = find_pending_daily_report()
    if pending_report is not None:
        process_daily_report(pending_report)
        return True

    # Этап 4: LLM-анализ встреч
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
