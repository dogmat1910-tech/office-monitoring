"""
LLM-классификация голосовых сегментов (always-on аудио).

Каждый VoiceSegment получает:
- kind: meeting | phone_work | phone_personal | office_chat | other_speech | noise
- summary: 1 предложение о чём шла речь
- meeting_id: автоматически проставляется по timestamp если в это время была встреча

Контекст для LLM:
- транскрипт сегмента
- активное приложение в это время (если знаем)
- идёт ли в это время встреча
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime

import httpx
from sqlmodel import Session, select

from analyze import _extract_json
from llm_retry import with_llm_retry
from main import Meeting, VoiceSegment, WindowSample, engine, _as_utc

log = logging.getLogger("worker")

API_KEY = os.environ.get("OM_OPENROUTER_API_KEY", "")
# Haiku вместо Sonnet: при 180 агентах × ~50 сегментов/день = ~$2/день вместо ~$50/день.
# Для коротких 1-2 предложений Haiku справляется не хуже.
MODEL = os.environ.get("OM_LLM_MODEL_VOICE", "anthropic/claude-haiku-4.5")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

VALID_KINDS = {"meeting", "phone_work", "phone_personal", "office_chat", "other_speech", "noise"}


def auto_bind_meeting_id(segment_id: int) -> int | None:
    """Если сегмент по времени попадает в какую-то встречу — возвращает её id."""
    with Session(engine) as session:
        seg = session.exec(select(VoiceSegment).where(VoiceSegment.id == segment_id)).first()
        if seg is None:
            return None
        seg_start = _as_utc(seg.started_at)
        # ищем встречу где seg_start ∈ [started_at, ended_at] (или ended_at IS NULL и встреча ещё идёт)
        for m in session.exec(
            select(Meeting).where(Meeting.agent_id == seg.agent_id)
        ).all():
            mst = _as_utc(m.started_at)
            men = _as_utc(m.ended_at) if m.ended_at else None
            if mst <= seg_start and (men is None or seg_start < men):
                return m.id
    return None


def _get_active_window_at(agent_id: str, ts: datetime) -> str | None:
    """Что было активно в момент начала сегмента (с допуском ±30 сек)."""
    from datetime import timedelta
    with Session(engine) as session:
        ts_utc = _as_utc(ts)
        win = session.exec(
            select(WindowSample)
            .where(WindowSample.agent_id == agent_id)
            .where(WindowSample.captured_at >= ts_utc - timedelta(seconds=30))
            .where(WindowSample.captured_at <= ts_utc + timedelta(seconds=30))
            .order_by(WindowSample.captured_at)
        ).first()
        if win:
            return f"{win.app_name}: {win.title}" if win.title else win.app_name
    return None


SYSTEM_PROMPT = """\
Ты — эксперт по классификации записи речи менеджера компании, которая помогает \
призывникам решать вопросы с военкоматом (отсрочки, призыв, медицинские заключения по РБ, \
юридическая помощь по 53-ФЗ).

Тебе дан транскрипт короткого голосового сегмента менеджера + контекст (активное окно, \
время суток, идёт ли в этот момент встреча по календарю).

Классифицируй сегмент в один из 6 типов:
- meeting: разговор с клиентом по делу компании (продажа услуг, консультация по призыву)
- phone_work: рабочий разговор по телефону (продажа, переговоры с клиентом, согласование)
- phone_personal: личный разговор (с семьёй, друзьями, врачом и т.п. — НЕ по работе компании)
- office_chat: разговор с коллегой в офисе (рабочие моменты, перекур, бытовые)
- other_speech: другое (зачитал вслух, говорит сам с собой, реклама на фоне, ассистент)
- noise: ложное срабатывание VAD, реальной осмысленной речи нет

При неоднозначности — предпочитай менее обвинительный класс (например, при сомнении \
между phone_personal и office_chat бери office_chat).

Отвечай строго JSON-объектом без markdown.\
"""


def classify_voice_segment(segment_id: int) -> dict:
    if not API_KEY:
        raise RuntimeError("OM_OPENROUTER_API_KEY не сконфигурирован")

    with Session(engine) as session:
        seg = session.exec(select(VoiceSegment).where(VoiceSegment.id == segment_id)).first()
        if seg is None:
            raise ValueError(f"voice_segment {segment_id} не найден")
        if not seg.text:
            raise ValueError("сегмент без транскрипта — нечего классифицировать")
        ts = _as_utc(seg.started_at)
        agent_id = seg.agent_id
        duration = seg.duration_seconds
        text = seg.text
        meeting_id = seg.meeting_id

    active_window = _get_active_window_at(agent_id, ts)
    in_meeting = meeting_id is not None

    user_prompt = f"""\
Голосовой сегмент менеджера.

Время начала: {ts.isoformat()}
Длительность: {duration:.1f} сек
Активное окно/приложение в этот момент: {active_window or '(не зафиксировано)'}
В это время идёт встреча по календарю: {'да, meeting_id=' + str(meeting_id) if in_meeting else 'нет'}

Транскрипт:
"\"\"
{text}
"\"\"

Верни JSON:
{{
  "kind": "meeting | phone_work | phone_personal | office_chat | other_speech | noise",
  "summary": "<1 предложение о чём говорил>",
  "confidence": <число 0-1>,
  "reason": "<кратко почему такой kind>"
}}
"""

    @with_llm_retry
    def _call_llm():
        resp = httpx.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "HTTP-Referer": "https://office.lkdzrkk.pro",
                "X-Title": "office-monitoring-voice-classify",
                "Content-Type": "application/json",
            },
            json={
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.1,
                "max_tokens": 400,
                "response_format": {"type": "json_object"},
            },
            timeout=60.0,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    t0 = time.monotonic()
    content = _call_llm()
    parsed = _extract_json(content)
    elapsed = time.monotonic() - t0

    kind = parsed.get("kind", "").strip().lower()
    if kind not in VALID_KINDS:
        log.warning("voice_segment %d: неизвестный kind=%r, ставим other_speech", segment_id, kind)
        kind = "other_speech"

    parsed["kind"] = kind
    parsed["_meta"] = {"model": MODEL, "processing_time_seconds": elapsed}
    return parsed
