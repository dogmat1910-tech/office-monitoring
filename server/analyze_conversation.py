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
from llm_retry import with_llm_retry
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

Тебе дан транскрипт целого разговора менеджера + контекст.

## Шаг 1: определи тип разговора

Типы (kind):
- meeting: очная встреча или дистанционная консультация с клиентом по продаже услуг
- phone_work: рабочий звонок (продажа, согласование с клиентом)
- phone_personal: личный разговор (с семьёй, друзьями — НЕ по работе)
- office_chat: разговор с коллегой в офисе
- other_speech: монолог, чтение вслух, ассистент

При неоднозначности предпочитай менее обвинительный класс.

## Шаг 2: если is_sale_attempt=true — оцени по 10-пунктному чеклисту

Каждый этап оценивается:
- "done" — выполнен хорошо
- "partial" — выполнен с недочётами
- "missed" — пропущен или провален
- "n/a" — неприменим (например, если клиент сам сразу согласился — "работа с возражениями" = n/a)

### ЧЕКЛИСТ ВСТРЕЧИ:

1. **Приветствие** — менеджер представился, назвал компанию, поздоровался.
   done: "Здравствуйте, меня зовут Иван, компания ХХХ". partial: поздоровался но не представился. missed: начал без приветствия.

2. **Установление контакта** — small talk, расположил к себе, создал доверительную атмосферу.
   done: лёгкий вопрос не по делу, улыбка в голосе. partial: формально. missed: сразу к делу без разогрева.

3. **Выявление ситуации** — спросил про текущий статус: повестка, стадия призыва, возраст, диагнозы, был ли на медкомиссии.
   done: 3+ уточняющих вопроса. partial: 1-2 вопроса. missed: не спросил, сам начал рассказывать.

4. **Выявление потребности** — понял чего конкретно хочет клиент (отсрочка, освобождение, военный билет, категория В).
   done: клиент сам озвучил или менеджер точно сформулировал. partial: примерно понял. missed: не уточнил.

5. **Презентация решения** — объяснил как компания помогает, какие варианты по Расписанию Болезней, что включает услуга, сроки.
   done: конкретный план действий под ситуацию клиента. partial: общие слова "мы поможем". missed: не презентовал.

6. **Работа с возражениями** — если клиент сказал "дорого" / "подумаю" / "сам разберусь" / "не уверен" — менеджер отработал.
   done: аргументированный ответ, кейсы, гарантии. partial: попытался, но слабо. missed: сдался или проигнорировал. n/a: возражений не было.

7. **Оглашение стоимости** — назвал цену, объяснил из чего складывается, предложил варианты оплаты.
   done: чётко назвал сумму + обосновал ценность. partial: назвал, но без обоснования. missed: не озвучил цену или мямлил.

8. **Закрытие сделки** — прямо предложил оплатить / подписать договор / назначить дату оплаты.
   done: "Давайте оформим сегодня" / "Готовы подписать?". partial: намекнул. missed: не предложил закрыть.

9. **Оформление** — если клиент согласился: оформил документы, обсудил кредит/рассрочку/наличные, заполнил договор.
   done: всё оформлено. partial: частично. missed: клиент согласился, но менеджер не довёл. n/a: клиент не согласился.

10. **Прощание** — резюмировал договорённости, назначил следующий шаг (если не закрыл), попрощался вежливо.
    done: "Итак, мы договорились о..., жду вас в среду". partial: просто "до свидания". missed: разговор оборвался.

### ОЦЕНКА sale_quality_score:
- Считай: done = 1 балл, partial = 0.5, missed = 0, n/a = не считается.
- score = (сумма баллов / количество применимых этапов) × 10, округли до целого.
- 0-3: провал, 4-6: слабо, 7-8: хорошо, 9-10: отлично.

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
  "is_with_client": <true|false>,
  "is_sale_attempt": <true|false>,
  "is_sale_closed": <true|false>,
  "sale_quality_score": <число 0-10 если is_sale_attempt, иначе null>,
  "checklist": [
    {{"step": 1, "name": "Приветствие", "status": "done|partial|missed|n/a", "comment": "<что конкретно>"}},
    {{"step": 2, "name": "Установление контакта", "status": "...", "comment": "..."}},
    {{"step": 3, "name": "Выявление ситуации", "status": "...", "comment": "..."}},
    {{"step": 4, "name": "Выявление потребности", "status": "...", "comment": "..."}},
    {{"step": 5, "name": "Презентация решения", "status": "...", "comment": "..."}},
    {{"step": 6, "name": "Работа с возражениями", "status": "...", "comment": "..."}},
    {{"step": 7, "name": "Оглашение стоимости", "status": "...", "comment": "..."}},
    {{"step": 8, "name": "Закрытие сделки", "status": "...", "comment": "..."}},
    {{"step": 9, "name": "Оформление", "status": "...", "comment": "..."}},
    {{"step": 10, "name": "Прощание", "status": "...", "comment": "..."}}
  ],
  "key_observations": ["<заметные моменты>"],
  "critical_errors": ["<серьёзные ошибки менеджера>"],
  "recommendation": "<конкретная рекомендация менеджеру, 1-2 предложения>"
}}

Если is_sale_attempt=false — checklist пустой массив [], sale_quality_score=null.
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

    @with_llm_retry
    def _call_llm():
        resp = httpx.post(
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
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    t0 = time.monotonic()
    content = _call_llm()
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
