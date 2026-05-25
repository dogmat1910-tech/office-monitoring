"""
LLM-анализ Conversation целиком.

В отличие от classify_voice (один сегмент), этот анализирует весь разговор
склейкой нескольких сегментов. Длинный диалог LLM понимает лучше:
видит структуру (приветствие → выявление потребности → презентация → отработка
возражений → попытка закрытия), может оценить продажу.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone

import httpx
from sqlmodel import Session, select

from analyze import _extract_json
from main import Conversation, Meeting, WindowSample, engine, _as_utc

log = logging.getLogger("worker")

API_KEY = os.environ.get("OM_OPENROUTER_API_KEY", "")
MODEL = os.environ.get("OM_LLM_MODEL_CONV", os.environ.get("OM_LLM_MODEL", "anthropic/claude-sonnet-4-6"))
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

VALID_KINDS = {"meeting", "phone_work", "phone_personal", "office_chat", "other_speech"}


def _get_context_windows(agent_id: str, started_at, ended_at) -> str:
    """Какие приложения были активны во время разговора."""
    with Session(engine) as session:
        wins = session.exec(
            select(WindowSample.app_name, WindowSample.title)
            .where(WindowSample.agent_id == agent_id)
            .where(WindowSample.captured_at >= started_at)
            .where(WindowSample.captured_at <= ended_at)
            .limit(20)
        ).all()
        if not wins:
            return "(нет данных по окнам)"
        seen = set()
        lines = []
        for app, title in wins:
            key = (app or "?", (title or "")[:80])
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"  • {app}: {title or ''}")
            if len(lines) >= 10:
                break
        return "\n".join(lines)


SYSTEM_PROMPT = """\
Ты — эксперт по контролю качества работы менеджеров компании, помогающей \
призывникам решать вопросы с военкоматом (отсрочки, призыв, медицинские заключения по \
Расписанию Болезней, юридическая помощь по 53-ФЗ).

Тебе дан транскрипт целого разговора менеджера (длительностью от нескольких минут \
до часа) + контекст: какие приложения были открыты в это время, был ли в этот \
момент активен «meeting» по кнопке календаря.

Твоя задача — определить тип разговора и оценить попытку продажи (если была).

Типы (kind):
- meeting: очная встреча или дистанционная консультация с клиентом по продаже услуг
- phone_work: рабочий звонок (исходящий/входящий по телефонии, продажа, согласование)
- phone_personal: личный разговор по телефону (с семьёй, друзьями, врачом)
- office_chat: разговор с коллегой в офисе (бытовые/рабочие моменты, не продажа)
- other_speech: монолог, чтение вслух, реклама, ассистент — не продажа

При неоднозначности предпочитай менее обвинительный класс.

Отвечай строго JSON-объектом без markdown.\
"""


def build_user_prompt(conv) -> str:
    duration_min = (conv.duration_seconds or 0) / 60
    return f"""\
РАЗГОВОР:
- Длительность: {duration_min:.1f} минут
- Сегментов речи: {conv.segment_count}
- Начало: {_as_utc(conv.started_at).isoformat()}
- В это время активна встреча по кнопке: {"ДА, meeting_id=" + str(conv.related_meeting_id) if conv.related_meeting_id else "НЕТ"}

АКТИВНЫЕ ОКНА/САЙТЫ во время разговора:
{_get_context_windows(conv.agent_id, _as_utc(conv.started_at), _as_utc(conv.ended_at))}

ТРАНСКРИПТ:
\"\"\"
{conv.full_text or "(пусто)"}
\"\"\"

Верни строго JSON:
{{
  "kind": "meeting | phone_work | phone_personal | office_chat | other_speech",
  "confidence": <число 0-1>,
  "summary": "<2-3 предложения о чём шёл разговор>",
  "is_with_client": <true|false — это разговор с клиентом по делу компании?>,
  "is_sale_attempt": <true|false — была ли попытка продажи услуг?>,
  "is_sale_closed": <true|false — клиент согласился оплатить?>,
  "sale_quality_score": <число 0-10 если is_sale_attempt=true, иначе null>,
  "key_observations": ["<заметные моменты>", ...],
  "critical_errors": ["<серьёзные ошибки менеджера, если есть>"]
}}

sale_quality_score:
- 0-3: грубо нарушены принципы или клиент явно был против
- 4-6: попытка была, но без чёткой структуры или были недочёты
- 7-8: качественная попытка с минорными недочётами
- 9-10: эталонная продажа
"""


def analyze_conversation(conversation_id: int) -> dict:
    if not API_KEY:
        raise RuntimeError("OM_OPENROUTER_API_KEY не сконфигурирован")

    with Session(engine) as session:
        conv = session.exec(select(Conversation).where(Conversation.id == conversation_id)).first()
        if conv is None:
            raise ValueError(f"conversation {conversation_id} не найден")
        if not conv.full_text or not conv.full_text.strip():
            raise ValueError("пустой transcript")

    user_prompt = build_user_prompt(conv)
    log.info("conversation %d: LLM-анализ (длительность %.1fs)", conversation_id, conv.duration_seconds)

    t0 = time.monotonic()
    r = httpx.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "HTTP-Referer": "https://office.lkdzrkk.pro",
            "X-Title": "office-monitoring-conversation",
            "Content-Type": "application/json",
        },
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 1500,
            "response_format": {"type": "json_object"},
        },
        timeout=90.0,
    )
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    parsed = _extract_json(content)
    elapsed = time.monotonic() - t0

    # нормализация
    kind = (parsed.get("kind") or "").strip().lower()
    if kind not in VALID_KINDS:
        kind = "other_speech"
    parsed["kind"] = kind
    parsed["_meta"] = {"model": MODEL, "processing_time_seconds": elapsed}

    log.info("conversation %d: kind=%s sale_attempt=%s sale_closed=%s (%.1fs)",
             conversation_id, kind, parsed.get("is_sale_attempt"), parsed.get("is_sale_closed"), elapsed)
    return parsed
