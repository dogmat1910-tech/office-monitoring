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
from main import AUDIO_DIR, AudioChunk, Meeting, Transcript, engine
from transcribe import get_model, transcribe_meeting

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("worker")

POLL_INTERVAL = int(os.environ.get("OM_WORKER_POLL_INTERVAL", "10"))


def find_pending_meeting() -> int | None:
    """Возвращает meeting_id первой закрытой встречи без транскрипта."""
    with Session(engine) as session:
        # подзапрос: meeting_id у которых есть Transcript
        subq = select(Transcript.meeting_id)
        meeting = session.exec(
            select(Meeting)
            .where(Meeting.ended_at.is_not(None))
            .where(~Meeting.id.in_(subq))
            .order_by(Meeting.ended_at)
        ).first()
        return meeting.id if meeting else None


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


def process_one() -> bool:
    """Обрабатывает одну встречу. Возвращает True если что-то сделали."""
    meeting_id = find_pending_meeting()
    if meeting_id is None:
        return False
    paths = get_chunk_paths(meeting_id)
    if not paths:
        log.warning("meeting %d закрыта, но нет audio chunks — записываем пустой транскрипт", meeting_id)
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
        # чтобы не зацикливаться на сломанной встрече — сохраняем пустой транскрипт с пометкой
        save_transcript(meeting_id, {
            "text": f"[ошибка транскрипции: {e}]",
            "language": None,
            "duration_seconds": 0.0,
            "processing_time_seconds": 0.0,
        })
        return True


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
