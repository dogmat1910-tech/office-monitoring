"""
Weekly Report — LLM-сводка за рабочую неделю (пн-пт) менеджера.

Агрегирует данные из DailyReport'ов, WindowSample, Meeting, Analysis,
IdleSample, KeystrokeSample за 5 дней и генерирует сводку с трендами.

Архитектура: воркер берёт pending записи WeeklyReport, собирает данные за неделю,
шлёт в LLM, сохраняет результат.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlmodel import Session, select
from sqlalchemy import func

from main import (
    AppCategory, DailyReport, DomainCategory, IdleSample, KeystrokeSample,
    Meeting, Analysis, Screenshot, VoiceSegment, WeeklyReport, WindowSample, engine,
    DEFAULT_CATEGORIES, DEFAULT_DOMAIN_CATEGORIES, BROWSER_APPS,
    extract_url_from_title, extract_domain, _as_utc, _tz_offset_for_agent,
)
from llm_retry import with_llm_retry

log = logging.getLogger("worker")

API_KEY = os.environ.get("OM_OPENROUTER_API_KEY", "")
MODEL = os.environ.get("OM_LLM_MODEL_WEEKLY", os.environ.get("OM_LLM_MODEL", "anthropic/claude-sonnet-4-6"))
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def _fmt_secs(s: int) -> str:
    if s < 60:
        return f"{s} с"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m} мин {sec} с"
    h, mm = divmod(m, 60)
    return f"{h} ч {mm} мин"


def _week_date_bounds_utc(date_str: str, agent_id: str) -> tuple[datetime, datetime]:
    """Возвращает [00:00 понедельника, 24:00 пятницы) в UTC для рабочей недели."""
    d = datetime.fromisoformat(date_str)
    tz_offset = _tz_offset_for_agent(agent_id)
    start = (d - tz_offset).replace(tzinfo=timezone.utc)
    end = start + timedelta(days=5)
    return start, end


def _day_bounds_utc(date_str: str, agent_id: str) -> tuple[datetime, datetime]:
    """Один день [00:00, 24:00) в UTC."""
    d = datetime.fromisoformat(date_str)
    tz_offset = _tz_offset_for_agent(agent_id)
    start = (d - tz_offset).replace(tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start, end


def _workdays(week_start: str) -> list[str]:
    """Возвращает список 5 рабочих дней (пн-пт) начиная с week_start."""
    d = datetime.fromisoformat(week_start)
    return [(d + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(5)]


def collect_week_data(agent_id: str, week_start: str) -> dict:
    """Собирает агрегированные данные за рабочую неделю (пн-пт). Чистая выборка, без LLM."""
    days = _workdays(week_start)
    week_start_dt, week_end_dt = _week_date_bounds_utc(week_start, agent_id)

    with Session(engine) as session:
        # категории
        app_cat = dict(DEFAULT_CATEGORIES)
        for r in session.exec(select(AppCategory)).all():
            app_cat[r.app_name] = r.category
        domain_cat = dict(DEFAULT_DOMAIN_CATEGORIES)
        for r in session.exec(select(DomainCategory)).all():
            domain_cat[r.domain] = r.category

        # === Per-day data ===
        daily_data = []
        for day_str in days:
            day_start, day_end = _day_bounds_utc(day_str, agent_id)

            # Windows по категориям за день
            windows = session.exec(
                select(WindowSample)
                .where(WindowSample.agent_id == agent_id)
                .where(WindowSample.captured_at >= day_start)
                .where(WindowSample.captured_at < day_end)
            ).all()
            by_category = {"work": 0, "personal": 0, "neutral": 0}
            for w in windows:
                app = w.app_name or "unknown"
                url = extract_url_from_title(w.title)
                domain = extract_domain(url) if url and app in BROWSER_APPS else None
                if domain:
                    cat = domain_cat.get(domain) or app_cat.get(app, "neutral")
                else:
                    cat = app_cat.get(app, "neutral")
                by_category[cat] += w.duration_seconds

            # Встречи за день
            day_meetings = session.exec(
                select(Meeting)
                .where(Meeting.agent_id == agent_id)
                .where(Meeting.started_at >= day_start)
                .where(Meeting.started_at < day_end)
            ).all()
            day_meeting_scores = []
            for m in day_meetings:
                if m.id:
                    analysis = session.exec(select(Analysis).where(Analysis.meeting_id == m.id)).first()
                    if analysis and analysis.final_score is not None:
                        day_meeting_scores.append(analysis.final_score)

            # Idle за день
            idle_rows = session.exec(
                select(IdleSample)
                .where(IdleSample.agent_id == agent_id)
                .where(IdleSample.captured_at >= day_start)
                .where(IdleSample.captured_at < day_end)
            ).all()
            total_idle_interval = sum(r.interval_seconds for r in idle_rows)
            idle_seconds = sum(r.interval_seconds for r in idle_rows if r.idle_seconds > 60)
            active_seconds = total_idle_interval - idle_seconds
            active_samples = [r for r in idle_rows if r.idle_seconds <= 60]
            first_at = min((_as_utc(r.captured_at) for r in active_samples), default=None)
            last_at = max((_as_utc(r.captured_at) for r in active_samples), default=None)

            # DailyReport за день
            daily_rep = session.exec(
                select(DailyReport)
                .where(DailyReport.agent_id == agent_id)
                .where(DailyReport.report_date == day_str)
                .where(DailyReport.status == "done")
            ).first()
            daily_score = daily_rep.productivity_score if daily_rep else None
            daily_payload = json.loads(daily_rep.payload_json) if daily_rep and daily_rep.payload_json else None
            red_flags = (daily_payload or {}).get("red_flags", [])
            overall_summary = (daily_payload or {}).get("overall_summary", "")

            daily_data.append({
                "date": day_str,
                "by_category": by_category,
                "tracking_total_seconds": sum(by_category.values()),
                "meetings_count": len(day_meetings),
                "avg_meeting_score": round(sum(day_meeting_scores) / len(day_meeting_scores), 1) if day_meeting_scores else None,
                "active_seconds": active_seconds,
                "idle_seconds": idle_seconds,
                "first_activity_at": first_at.isoformat() if first_at else None,
                "last_activity_at": last_at.isoformat() if last_at else None,
                "daily_score": daily_score,
                "red_flags": red_flags,
                "overall_summary": overall_summary,
            })

        # === Week-level aggregations ===

        # Топ-5 приложений по времени за неделю
        windows_week = session.exec(
            select(WindowSample)
            .where(WindowSample.agent_id == agent_id)
            .where(WindowSample.captured_at >= week_start_dt)
            .where(WindowSample.captured_at < week_end_dt)
        ).all()
        by_entity: dict[str, dict] = {}
        week_by_category = {"work": 0, "personal": 0, "neutral": 0}
        for w in windows_week:
            app = w.app_name or "unknown"
            url = extract_url_from_title(w.title)
            domain = extract_domain(url) if url and app in BROWSER_APPS else None
            if domain:
                key = f"{app} · {domain}"
                cat = domain_cat.get(domain) or app_cat.get(app, "neutral")
            else:
                key = app
                cat = app_cat.get(app, "neutral")
            if key not in by_entity:
                by_entity[key] = {"name": key, "category": cat, "seconds": 0}
            by_entity[key]["seconds"] += w.duration_seconds
            week_by_category[cat] += w.duration_seconds
        top_entities = sorted(by_entity.values(), key=lambda x: -x["seconds"])[:10]

        # Встречи за неделю
        meetings_week = session.exec(
            select(Meeting)
            .where(Meeting.agent_id == agent_id)
            .where(Meeting.started_at >= week_start_dt)
            .where(Meeting.started_at < week_end_dt)
        ).all()
        meeting_scores = []
        for m in meetings_week:
            if m.id:
                analysis = session.exec(select(Analysis).where(Analysis.meeting_id == m.id)).first()
                if analysis and analysis.final_score is not None:
                    meeting_scores.append(analysis.final_score)
        avg_meeting_score = round(sum(meeting_scores) / len(meeting_scores), 1) if meeting_scores else None

        # Клавиатура за неделю
        ks_rows = session.exec(
            select(func.sum(KeystrokeSample.keystroke_count))
            .where(KeystrokeSample.agent_id == agent_id)
            .where(KeystrokeSample.captured_at >= week_start_dt)
            .where(KeystrokeSample.captured_at < week_end_dt)
        ).first()
        keystrokes_total = int(ks_rows or 0)

        # Daily scores за неделю
        daily_scores = [d["daily_score"] for d in daily_data if d["daily_score"] is not None]
        avg_daily_score = round(sum(daily_scores) / len(daily_scores), 1) if daily_scores else None

        # All red flags
        all_red_flags = []
        for d in daily_data:
            for rf in d["red_flags"]:
                all_red_flags.append({"date": d["date"], "flag": rf})

        # === Previous week comparison ===
        prev_week_start_str = (datetime.fromisoformat(week_start) - timedelta(days=7)).strftime("%Y-%m-%d")
        prev_week_start_dt, prev_week_end_dt = _week_date_bounds_utc(prev_week_start_str, agent_id)

        prev_windows = session.exec(
            select(WindowSample)
            .where(WindowSample.agent_id == agent_id)
            .where(WindowSample.captured_at >= prev_week_start_dt)
            .where(WindowSample.captured_at < prev_week_end_dt)
        ).all()
        prev_by_category = {"work": 0, "personal": 0, "neutral": 0}
        for w in prev_windows:
            app = w.app_name or "unknown"
            url = extract_url_from_title(w.title)
            domain = extract_domain(url) if url and app in BROWSER_APPS else None
            if domain:
                cat = domain_cat.get(domain) or app_cat.get(app, "neutral")
            else:
                cat = app_cat.get(app, "neutral")
            prev_by_category[cat] += w.duration_seconds

        prev_daily_reports = session.exec(
            select(DailyReport)
            .where(DailyReport.agent_id == agent_id)
            .where(DailyReport.status == "done")
            .where(DailyReport.report_date >= prev_week_start_str)
            .where(DailyReport.report_date < week_start)
        ).all()
        prev_scores = [r.productivity_score for r in prev_daily_reports if r.productivity_score is not None]
        prev_avg_daily_score = round(sum(prev_scores) / len(prev_scores), 1) if prev_scores else None

        prev_meetings_count = session.exec(
            select(func.count(Meeting.id))
            .where(Meeting.agent_id == agent_id)
            .where(Meeting.started_at >= prev_week_start_dt)
            .where(Meeting.started_at < prev_week_end_dt)
        ).one() or 0

        has_prev_data = len(prev_windows) > 0 or len(prev_daily_reports) > 0

        return {
            "agent_id": agent_id,
            "week_start": week_start,
            "days": days,
            "daily_data": daily_data,
            "week_by_category": week_by_category,
            "week_tracking_total_seconds": sum(week_by_category.values()),
            "top_entities": top_entities,
            "meetings_count": len(meetings_week),
            "avg_meeting_score": avg_meeting_score,
            "keystrokes_total": keystrokes_total,
            "avg_daily_score": avg_daily_score,
            "all_red_flags": all_red_flags,
            "comparison": {
                "has_prev_data": has_prev_data,
                "prev_week_start": prev_week_start_str,
                "prev_by_category": prev_by_category,
                "prev_avg_daily_score": prev_avg_daily_score,
                "prev_meetings_count": prev_meetings_count,
            },
        }


SYSTEM_PROMPT = """\
Ты — эксперт по контролю качества работы менеджеров в компании, которая помогает \
призывникам решать вопросы с военкоматом (отсрочки, призыв, медицинские заключения \
по РБ, юридическая помощь по 53-ФЗ).

Тебе даны агрегированные данные за РАБОЧУЮ НЕДЕЛЮ (понедельник—пятница) одного менеджера:
- ежедневные показатели: время по категориям, встречи, оценки дня
- топ приложений/сайтов за неделю
- red flags из дневных отчётов
- сравнение с предыдущей неделей (если есть данные)

Твоя задача — дать честный недельный обзор с выводами, трендами и рекомендациями.
Цифры пиши явно. Не выдумывай. Если данных мало — отметь это.

Отвечай строго JSON-объектом без markdown-форматирования.\
"""


def build_weekly_prompt(data: dict) -> str:
    days_text = ""
    for d in data["daily_data"]:
        bc = d["by_category"]
        days_text += (
            f"\n  {d['date']}: работа={_fmt_secs(bc['work'])}, личное={_fmt_secs(bc['personal'])}, "
            f"нейтр.={_fmt_secs(bc['neutral'])}, "
            f"встречи={d['meetings_count']}, оценка_дня={d['daily_score'] or '?'}/10, "
            f"активен с {(d.get('first_activity_at') or '?')[11:16]} до {(d.get('last_activity_at') or '?')[11:16]}"
        )
        if d.get("overall_summary"):
            days_text += f"\n    кратко: {d['overall_summary'][:200]}"
        if d["red_flags"]:
            days_text += f"\n    red flags: {'; '.join(d['red_flags'][:3])}"

    apps_text = "\n".join(
        f"  • [{e['category']:>8}] {e['name']}: {_fmt_secs(e['seconds'])}"
        for e in data["top_entities"]
    ) or "  (нет данных)"

    red_flags_text = "\n".join(
        f"  [{rf['date']}] {rf['flag']}"
        for rf in data["all_red_flags"][:15]
    ) or "  (red flags не обнаружены)"

    bc = data["week_by_category"]

    # Comparison text
    comp = data["comparison"]
    if comp["has_prev_data"]:
        pbc = comp["prev_by_category"]
        comp_text = f"""\
СРАВНЕНИЕ С ПРЕДЫДУЩЕЙ НЕДЕЛЕЙ ({comp['prev_week_start']}):
  работа: было {_fmt_secs(pbc['work'])} → сейчас {_fmt_secs(bc['work'])}
  личное: было {_fmt_secs(pbc['personal'])} → сейчас {_fmt_secs(bc['personal'])}
  встречи: было {comp['prev_meetings_count']} → сейчас {data['meetings_count']}
  ср.оценка дня: было {comp['prev_avg_daily_score'] or '?'} → сейчас {data['avg_daily_score'] or '?'}"""
    else:
        comp_text = "СРАВНЕНИЕ С ПРЕДЫДУЩЕЙ НЕДЕЛЕЙ: данных за прошлую неделю нет."

    return f"""\
НЕДЕЛЬНЫЙ ОТЧЁТ ЗА {data['week_start']} — {data['days'][-1]}:

ОБЩИЕ ПОКАЗАТЕЛИ:
  работа: {_fmt_secs(bc['work'])}
  личное: {_fmt_secs(bc['personal'])}
  нейтр.: {_fmt_secs(bc['neutral'])}
  ────────────
  всего трекинга: {_fmt_secs(data['week_tracking_total_seconds'])}
  встреч: {data['meetings_count']}, ср. оценка встреч: {data['avg_meeting_score'] or '?'}/10
  клавиатура: {data['keystrokes_total']} нажатий
  средняя оценка дня: {data['avg_daily_score'] or '?'}/10

ПО ДНЯМ:{days_text}

ТОП ПРИЛОЖЕНИЙ/САЙТОВ ЗА НЕДЕЛЮ:
{apps_text}

RED FLAGS ИЗ ДНЕВНЫХ ОТЧЁТОВ:
{red_flags_text}

{comp_text}

────────────────────────────────────────

Дай разбор СТРОГО в формате JSON:
{{
  "productivity_score": <число 0-10, средневзвешенная оценка недели>,
  "summary": "<Краткая сводка за неделю, 2-3 предложения>",
  "strengths": ["<что хорошо, 2-3 пункта>"],
  "concerns": ["<что плохо / что улучшить, 2-3 пункта>"],
  "trend": "<improving | stable | declining>",
  "trend_details": "<По сравнению с прошлой неделей... (1-2 предложения). Если данных за прошлую неделю нет — напиши 'Нет данных для сравнения'>",
  "daily_breakdown": [
    {{"date": "YYYY-MM-DD", "score": <число 0-10 из daily или твоя оценка>, "highlight": "<1 предложение — главное за день>"}},
    ...для каждого дня
  ],
  "recommendation": "<Конкретная рекомендация руководителю, 1-2 предложения>"
}}

productivity_score:
- 0-3: серьёзные проблемы всю неделю
- 4-6: средняя неделя — есть чем заняться
- 7-8: хорошая неделя — стабильная работа
- 9-10: отличная неделя — высокая продуктивность

trend:
- "improving" если текущая неделя ЛУЧШЕ предыдущей (больше работы, выше оценки, меньше прокрастинации)
- "stable" если примерно на том же уровне
- "declining" если текущая неделя ХУЖЕ предыдущей
- если нет данных за прошлую неделю — "stable"
"""


def generate_weekly_report(agent_id: str, week_start: str) -> dict:
    if not API_KEY:
        raise RuntimeError("OM_OPENROUTER_API_KEY не сконфигурирован")

    data = collect_week_data(agent_id, week_start)
    user_prompt = build_weekly_prompt(data)
    log.info("weekly_report %s/%s: prompt_len=%d", agent_id, week_start, len(user_prompt))

    @with_llm_retry
    def _call_llm() -> httpx.Response:
        return httpx.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "HTTP-Referer": "https://office.lkdzrkk.pro",
                "X-Title": "office-monitoring-weekly",
                "Content-Type": "application/json",
            },
            json={
                "model": MODEL,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.2,
                "max_tokens": 3000,
                "response_format": {"type": "json_object"},
            },
            timeout=120.0,
        )

    t0 = time.monotonic()
    r = _call_llm()
    r.raise_for_status()
    resp = r.json()
    content = resp["choices"][0]["message"]["content"]
    elapsed = time.monotonic() - t0
    usage = resp.get("usage", {})
    log.info("weekly_report %s/%s: %.1fs prompt_tokens=%s completion_tokens=%s",
             agent_id, week_start, elapsed, usage.get("prompt_tokens"), usage.get("completion_tokens"))

    # robust JSON parse
    from analyze import _extract_json
    parsed = _extract_json(content)
    parsed["_meta"] = {
        "model": MODEL,
        "processing_time_seconds": elapsed,
        "data_snapshot": {
            "week_tracking_total_seconds": data["week_tracking_total_seconds"],
            "week_by_category": data["week_by_category"],
            "meetings_count": data["meetings_count"],
            "avg_daily_score": data["avg_daily_score"],
        },
    }
    return parsed
