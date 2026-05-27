"""
Авто-категоризация приложений и доменов через LLM.

Запускается фоновой задачей раз в N минут:
1. Собирает все app_name и domain, которые встречаются в WindowSample,
   но не имеют записи в AppCategory / DomainCategory (или категория устарела).
2. Шлёт батч в LLM с описанием бизнес-контекста (помощь призывникам с 53-ФЗ).
3. LLM возвращает {имя: категория} для каждого.
4. Сохраняем с auto_categorized=True. Записи, помеченные admin'ом руками
   (auto_categorized=False), LLM не трогает.

Идемпотентно: каждый запуск пересматривает только новые/неклассифицированные элементы.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Iterable

import httpx
from sqlalchemy import func
from sqlmodel import Session, select

from llm_retry import with_llm_retry

log = logging.getLogger("auto_categorize")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
API_KEY = os.environ.get("OM_OPENROUTER_API_KEY", "")
MODEL = os.environ.get("OM_LLM_MODEL_CATEGORIZE", os.environ.get("OM_LLM_MODEL", "anthropic/claude-sonnet-4-6"))

BATCH_SIZE = 60  # сколько имён в одном запросе

SYSTEM_PROMPT = """Ты категоризатор активности менеджеров отдела продаж.

Контекст бизнеса: компания консультирует призывников по 53-ФЗ (отсрочка / освобождение от службы в армии по медицинским и другим основаниям). Менеджеры общаются с клиентами по телефону и онлайн, закрывают сделки, оформляют документы, иногда подают кредиты для клиентов через банки.

Типичные РАБОЧИЕ ресурсы менеджеров:
- CRM-системы: amoCRM (АМО), Битрикс24, Энигма
- Телефония: облачные АТС, Скорозвон, MANGO Office, любые dialer'ы
- Банковские страницы для оформления кредитов клиентам (Тинькофф, Сбер, Альфа и т.д.)
- Сайты собственной компании
- "Офис-машина" — внутренний терминал для работы с клиентами вне учётной системы
- Рабочая почта
- Google по вопросам медицины и 53-ФЗ
- Google-таблицы (workbench менеджера)
- Офисный пакет (Word/Excel/Google Docs)
- Кабинет агентской программы банков
- Документация по 53-ФЗ, военно-врачебной экспертизе, расписанию болезней

ЛИЧНОЕ:
- ВСЕ видеохостинги — YouTube, TikTok, Rutube, VK Video, Twitch, Vimeo, Dzen Video, Reels — ВСЕГДА personal. Обучение менеджеров в этой компании проходит только в очном формате, поэтому видео = развлечения, без исключений. confidence 0.95+.
- Игры (Steam, браузерные)
- Шопинг (Wildberries, Ozon — если не для работы)
- Личные соцсети, развлекательные новости
- Знакомства, форумы
- Фильмы, музыка для удовольствия

НЕЙТРАЛЬНОЕ (когда нельзя однозначно отличить или это системное):
- Telegram / WhatsApp / VK — могут быть и рабочие, и личные переписки. По имени приложения не определить — ставь "neutral", админ потом руками доуточнит.
- Системные приложения: Explorer, Task Manager, Settings, Windows Security
- Браузеры сами по себе (chrome.exe, firefox.exe, msedge.exe) — neutral. Конкретные домены внутри классифицируются отдельно.
- IDE (если менеджер не разработчик) — neutral

Формат ответа: ТОЛЬКО валидный JSON, без обёртки markdown.
Структура: {"имя1": {"category": "work|personal|neutral", "confidence": 0.0-1.0, "reason": "короткое объяснение почему"}, ...}

confidence — твоя уверенность (0.9 = очень уверен; 0.5 = неоднозначно).
reason — 1 предложение, максимум 80 символов, почему именно эта категория. Это видит админ при аудите.
Если не знаешь — confidence 0.3-0.5, категорию "neutral", reason "недостаточно контекста".
"""


def _clean_json(text: str) -> str:
    """LLM иногда оборачивает JSON в ```json ... ```. Чистим."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def classify_batch(items: list[str], kind: str) -> dict[str, tuple[str, float, str]]:
    """Шлёт батч имён в LLM, возвращает {item: (category, confidence, reason)}.

    kind: "приложений" или "веб-доменов" — для подсказки LLM в user-промпте.
    """
    if not items or not API_KEY:
        return {}

    user_prompt = (
        f"Раскатегорируй следующий список {kind}. Верни JSON {{имя: {{category, confidence}}}}.\n\n"
        + json.dumps(items, ensure_ascii=False, indent=2)
    )

    @with_llm_retry
    def _call_llm():
        with httpx.Client(timeout=60.0) as client:
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
                    "max_tokens": 4000,
                },
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]

    try:
        content = _call_llm()
        data = json.loads(_clean_json(content))
    except Exception as e:
        log.warning("LLM classify failed: %s", e)
        return {}

    result: dict[str, tuple[str, float, str]] = {}
    for item in items:
        entry = data.get(item)
        if not isinstance(entry, dict):
            continue
        category = entry.get("category")
        confidence = entry.get("confidence")
        reason = (entry.get("reason") or "").strip()[:160]
        if category not in {"work", "personal", "neutral"}:
            continue
        try:
            conf = float(confidence)
        except (TypeError, ValueError):
            conf = 0.5
        result[item] = (category, max(0.0, min(1.0, conf)), reason)
    return result


def categorize_new_apps(engine, lookback_days: int = 7) -> int:
    """Находит app_name из WindowSample за последние N дней без записи в AppCategory,
    шлёт в LLM, сохраняет. Возвращает число добавленных категорий."""
    from main import AppCategory, WindowSample  # отложенный импорт

    since = datetime.now(timezone.utc).replace(tzinfo=None)  # SQLite в naive UTC хранит
    from datetime import timedelta
    since = since - timedelta(days=lookback_days)

    added = 0
    with Session(engine) as session:
        existing = set(session.exec(select(AppCategory.app_name)).all())
        rows = session.exec(
            select(WindowSample.app_name)
            .where(WindowSample.captured_at >= since)
            .distinct()
        ).all()
        unknown = sorted([(a or "").strip() for a in rows if a and a.strip() and a not in existing])
        if not unknown:
            return 0

        log.info("auto-categorize: %d новых приложений для классификации", len(unknown))

        for i in range(0, len(unknown), BATCH_SIZE):
            batch = unknown[i:i + BATCH_SIZE]
            results = classify_batch(batch, "приложений")
            now = datetime.now(timezone.utc)
            for name in batch:
                cat, conf, reason = results.get(name, ("neutral", 0.3, "LLM не ответил"))
                session.add(AppCategory(
                    app_name=name,
                    category=cat,
                    updated_at=now,
                    auto_categorized=True,
                    confidence=conf,
                    reason=reason or None,
                ))
                added += 1
            session.commit()
            log.info("  batch %d/%d → %d/%d классифицировано", i // BATCH_SIZE + 1,
                     (len(unknown) + BATCH_SIZE - 1) // BATCH_SIZE, len(results), len(batch))
    return added


def categorize_new_domains(engine, lookback_days: int = 7) -> int:
    """То же, но для доменов — выделяем из title через extract_url_from_title."""
    from main import DomainCategory, WindowSample, extract_url_from_title, extract_domain, BROWSER_APPS
    from datetime import timedelta

    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    added = 0
    with Session(engine) as session:
        existing = set(session.exec(select(DomainCategory.domain)).all())
        rows = session.exec(
            select(WindowSample.app_name, WindowSample.title)
            .where(WindowSample.captured_at >= since)
            .where(WindowSample.app_name.in_(BROWSER_APPS))
        ).all()
        domains_seen: set[str] = set()
        for app_name, title in rows:
            url = extract_url_from_title(title)
            d = extract_domain(url)
            if d and d not in existing:
                domains_seen.add(d)
        unknown = sorted(domains_seen)
        if not unknown:
            return 0

        log.info("auto-categorize: %d новых доменов для классификации", len(unknown))

        for i in range(0, len(unknown), BATCH_SIZE):
            batch = unknown[i:i + BATCH_SIZE]
            results = classify_batch(batch, "веб-доменов")
            now = datetime.now(timezone.utc)
            for name in batch:
                cat, conf, reason = results.get(name, ("neutral", 0.3, "LLM не ответил"))
                session.add(DomainCategory(
                    domain=name,
                    category=cat,
                    updated_at=now,
                    auto_categorized=True,
                    confidence=conf,
                    reason=reason or None,
                ))
                added += 1
            session.commit()
    return added


def run_once(engine) -> dict:
    """Один проход. Возвращает счётчики."""
    apps_added = categorize_new_apps(engine)
    domains_added = categorize_new_domains(engine)
    return {"apps_added": apps_added, "domains_added": domains_added}
