"""
LLM-классификация записанного текста переписки.

Фоновый воркер каждые N секунд берёт KeystrokeText.llm_category == None
и определяет work / personal / unclear через LLM с контекстом бизнеса.

Категории:
- work — переписка с клиентом или коллегой по делу (упоминания 53-ФЗ,
  военкомата, документов, диагноза, оплаты, профессиональный тон)
- personal — личная переписка с друзьями/семьёй (бытовые темы,
  неформальный тон, мат, эмодзи)
- unclear — короткие неоднозначные ('ок', 'привет') — не считаем ни как
  работу, ни как личное при подсчёте статистики

Confidence ниже 0.6 рекомендуем для ручного аудита.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone

import httpx
from sqlmodel import Session, select

from llm_retry import with_llm_retry

log = logging.getLogger("classify_keystroke_text")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
API_KEY = os.environ.get("OM_OPENROUTER_API_KEY", "")
# Лёгкая модель — текст обычно короткий, дорогая модель здесь избыточна.
MODEL = os.environ.get(
    "OM_LLM_MODEL_KEYSTROKE",
    os.environ.get("OM_LLM_MODEL_FAST", "anthropic/claude-haiku-4.5"),
)

BATCH_SIZE = 15  # сколько обрабатываем за один тик воркера
MIN_TEXT_CHARS = 3  # совсем короткие (типа "ок") сразу помечаем unclear, не шлём в LLM

SYSTEM_PROMPT = """Ты классификатор корпоративной переписки.

Контекст: компания консультирует клиентов по 53-ФЗ (отсрочка/освобождение от службы в армии по медицинским и другим основаниям). Менеджеры общаются с клиентами и коллегами через Telegram / WhatsApp / VK с рабочих ноутбуков.

Задача: определи, рабочая это переписка с клиентом/коллегой или личная с друзьями/семьёй.

Признаки РАБОЧЕЙ (work):
- упоминание военно-врачебной экспертизы, военкомата, призыва, отсрочки, освобождения
- профессиональная терминология: статья, диагноз, КЭК, ВВК, расписание болезней, оплата, договор, документы, справка, выписка
- обращение по имени-отчеству, "на вы", деловой тон
- запрос или отправка документов, медицинских справок, фотографий снимков
- упоминание медучреждений, военкоматов, юридических норм
- упоминание ФИО клиента, фамилии-имени-отчества
- финансовое: рассрочка, кредит, оплата, перевод, договор
- координация между менеджерами по клиентам ("Иванов перенёс встречу", "позвони ему")

Признаки ЛИЧНОЙ (personal):
- неформальный тон, частые сокращения, разговорная лексика, мат
- эмодзи и стикеры (упоминание ":)", "))", "ахах", "лол" и подобное)
- бытовые темы: что готовить, кино, музыка, отношения, дети, секс, спорт-тусовка
- "привет / как дела / что делаешь / куда поедем"
- упоминания семьи, друзей, домашних дел, путешествий не по работе
- сленг подростковый/молодёжный

UNCLEAR — когда нельзя однозначно определить:
- очень коротко ("ок", "ага", "спасибо", "до завтра", "пока", "+", "👍")
- одно-два слова без контекста
- "я в офисе", "перезвоню" — могут быть и рабочие и личные

Confidence:
- 0.9+ — очень уверен (есть явные маркеры из списков выше)
- 0.7-0.9 — уверен (тон+тема явно один из двух)
- 0.5-0.7 — есть сомнения, склоняюсь к одному
- <0.5 — лучше unclear

Формат ответа: ТОЛЬКО валидный JSON, без markdown:
{"category": "work|personal|unclear", "confidence": 0.0-1.0, "reason": "макс 80 символов почему именно эта категория"}
"""


def _clean_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def classify_one(text: str, app_name: str, window_title: str) -> tuple[str, float, str] | None:
    """Классифицирует один текст. Возвращает (category, confidence, reason) или None при ошибке."""
    if not API_KEY:
        return None

    user_prompt = (
        f"Менеджер набрал в приложении '{app_name}' "
        f"(окно: '{window_title or '?'}'):\n\n"
        f"«{text}»\n\n"
        f"Верни JSON {{category, confidence, reason}}."
    )

    @with_llm_retry
    def _call_llm():
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": MODEL,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": 0.1,
                    "max_tokens": 200,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]

    try:
        content = _call_llm()
        data = json.loads(_clean_json(content))
    except Exception as e:
        log.warning("classify_one failed: %s", e)
        return None

    category = data.get("category")
    if category not in {"work", "personal", "unclear"}:
        return None
    try:
        conf = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    conf = max(0.0, min(1.0, conf))
    reason = (data.get("reason") or "").strip()[:160]
    return category, conf, reason


def process_batch(engine) -> int:
    """Один тик: берёт до BATCH_SIZE необработанных сессий, классифицирует, сохраняет.
    Возвращает число обработанных."""
    from main import KeystrokeText  # отложенный импорт

    processed = 0
    with Session(engine) as session:
        rows = session.exec(
            select(KeystrokeText)
            .where(KeystrokeText.llm_category.is_(None))
            .order_by(KeystrokeText.received_at.desc())
            .limit(BATCH_SIZE)
        ).all()

        if not rows:
            return 0

        for row in rows:
            text = (row.text or "").strip()
            # Слишком короткое — сразу unclear без обращения в LLM
            if len(text) < MIN_TEXT_CHARS:
                row.llm_category = "unclear"
                row.llm_confidence = 0.0
                row.llm_reason = "слишком короткое для классификации"
                session.add(row)
                processed += 1
                continue

            result = classify_one(text, row.app_name, row.window_title or "")
            if result is None:
                # LLM не ответил — не помечаем, попробуем в след. тик
                continue
            cat, conf, reason = result
            row.llm_category = cat
            row.llm_confidence = conf
            row.llm_reason = reason
            session.add(row)
            processed += 1

        session.commit()
    return processed


def run_once(engine) -> dict:
    processed = process_batch(engine)
    return {"processed": processed}
