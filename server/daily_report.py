"""
Daily Report — LLM-разбор дня менеджера по 5 пунктам:
1. Сколько провёл встреч и качество
2. Анализ встреч (сильные стороны / зоны роста)
3. Телефонные звонки (рабочие vs личные — пока эвристика по голос-сегментам
   вне встреч; полное разделение появится на этапе 12 со Скорозвоном)
4. Прокрастинация и активность вне интересов компании
5. Общая оценка дня

Архитектура: воркер берёт pending записи DailyReport, собирает данные за день,
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

from main import (
    AppCategory, DailyReport, DomainCategory, IdleSample, KeystrokeSample,
    Meeting, Analysis, Transcript, VoiceSegment, WindowSample, engine,
    DEFAULT_CATEGORIES, DEFAULT_DOMAIN_CATEGORIES, BROWSER_APPS,
    extract_url_from_title, extract_domain, _as_utc,
)
from sqlalchemy import func

log = logging.getLogger("worker")

API_KEY = os.environ.get("OM_OPENROUTER_API_KEY", "")
MODEL = os.environ.get("OM_LLM_MODEL_DAILY", os.environ.get("OM_LLM_MODEL", "anthropic/claude-sonnet-4-6"))
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def _fmt_secs(s: int) -> str:
    if s < 60:
        return f"{s} с"
    m, sec = divmod(s, 60)
    if m < 60:
        return f"{m} мин {sec} с"
    h, mm = divmod(m, 60)
    return f"{h} ч {mm} мин"


def _date_bounds_utc(report_date: str) -> tuple[datetime, datetime]:
    """report_date в формате YYYY-MM-DD интерпретируется как локальная дата.
    Возвращает [00:00, 24:00) в UTC. На проде надо учитывать таймзону менеджера."""
    d = datetime.fromisoformat(report_date)
    # Считаем что менеджер в UTC+3 (Москва). Для прода — взять из настроек агента.
    tz_offset = timedelta(hours=3)
    start = (d - tz_offset).replace(tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return start, end


def collect_day_data(agent_id: str, report_date: str) -> dict:
    """Собирает всё что произошло у агента за день. Чистая выборка, без LLM."""
    start, end = _date_bounds_utc(report_date)
    with Session(engine) as session:
        # категории
        app_cat = dict(DEFAULT_CATEGORIES)
        for r in session.exec(select(AppCategory)).all():
            app_cat[r.app_name] = r.category
        domain_cat = dict(DEFAULT_DOMAIN_CATEGORIES)
        for r in session.exec(select(DomainCategory)).all():
            domain_cat[r.domain] = r.category

        # Окна
        windows = session.exec(
            select(WindowSample)
            .where(WindowSample.agent_id == agent_id)
            .where(WindowSample.captured_at >= start)
            .where(WindowSample.captured_at < end)
        ).all()

        # Агрегируем по (app, domain)
        by_entity: dict[str, dict] = {}
        for w in windows:
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

        by_category = {"work": 0, "personal": 0, "neutral": 0}
        for e in by_entity.values():
            by_category[e["category"]] += e["seconds"]

        top_entities = sorted(by_entity.values(), key=lambda x: -x["seconds"])[:20]

        # Встречи + их анализы
        meetings = session.exec(
            select(Meeting)
            .where(Meeting.agent_id == agent_id)
            .where(Meeting.started_at >= start)
            .where(Meeting.started_at < end)
        ).all()
        meeting_list = []
        for m in meetings:
            duration = (
                int((_as_utc(m.ended_at) - _as_utc(m.started_at)).total_seconds())
                if m.ended_at else None
            )
            analysis = session.exec(select(Analysis).where(Analysis.meeting_id == m.id)).first()
            analysis_payload = json.loads(analysis.payload_json) if analysis and analysis.payload_json else None
            transcript = session.exec(select(Transcript).where(Transcript.meeting_id == m.id)).first()
            meeting_list.append({
                "meeting_id": m.id,
                "started_at": _as_utc(m.started_at).isoformat(),
                "duration_seconds": duration,
                "client_name": m.client_name,
                "ended": m.ended_at is not None,
                "final_score": analysis.final_score if analysis else None,
                "summary": (analysis_payload or {}).get("summary"),
                "critical_errors": (analysis_payload or {}).get("critical_errors", []),
                "strengths": (analysis_payload or {}).get("strengths", []),
                "growth_areas": (analysis_payload or {}).get("growth_areas", []),
                "transcript_excerpt": (transcript.text[:500] + "...") if transcript and transcript.text and len(transcript.text) > 500 else (transcript.text if transcript else None),
            })

        # Idle: сколько времени менеджер был «не за компом» (idle > 60 сек)
        idle_rows = session.exec(
            select(IdleSample)
            .where(IdleSample.agent_id == agent_id)
            .where(IdleSample.captured_at >= start)
            .where(IdleSample.captured_at < end)
        ).all()
        idle_threshold = 60
        total_idle_interval = sum(r.interval_seconds for r in idle_rows)
        idle_seconds = sum(r.interval_seconds for r in idle_rows if r.idle_seconds > idle_threshold)
        active_at_pc_seconds = total_idle_interval - idle_seconds

        # Keystrokes: сколько и где набирал
        ks_rows = session.exec(
            select(KeystrokeSample.app_name, func.sum(KeystrokeSample.keystroke_count))
            .where(KeystrokeSample.agent_id == agent_id)
            .where(KeystrokeSample.captured_at >= start)
            .where(KeystrokeSample.captured_at < end)
            .group_by(KeystrokeSample.app_name)
        ).all()
        keystrokes_by_app = sorted(
            [{"app_name": app or "unknown", "count": int(n or 0)} for app, n in ks_rows],
            key=lambda x: -x["count"],
        )
        keystrokes_total = sum(x["count"] for x in keystrokes_by_app)

        # Голосовые сегменты — делим на «во время встречи» и «вне встречи»
        voice = session.exec(
            select(VoiceSegment)
            .where(VoiceSegment.agent_id == agent_id)
            .where(VoiceSegment.started_at >= start)
            .where(VoiceSegment.started_at < end)
        ).all()
        meeting_ranges = [
            (_as_utc(m.started_at), _as_utc(m.ended_at) if m.ended_at else end)
            for m in meetings
        ]
        in_meeting_seconds = 0
        outside_meeting_segments = []
        for s in voice:
            seg_start = _as_utc(s.started_at)
            inside = any(mst <= seg_start < men for mst, men in meeting_ranges)
            if inside:
                in_meeting_seconds += int(s.duration_seconds)
            else:
                outside_meeting_segments.append({
                    "started_at": seg_start.isoformat(),
                    "duration_seconds": int(s.duration_seconds),
                    "text": s.text,
                })
        outside_meeting_seconds = sum(s["duration_seconds"] for s in outside_meeting_segments)

        return {
            "agent_id": agent_id,
            "report_date": report_date,
            "tracking_total_seconds": sum(by_category.values()),
            "by_category": by_category,
            "top_entities": top_entities,
            "meetings": meeting_list,
            "voice_outside_meetings_seconds": outside_meeting_seconds,
            "voice_outside_meetings_segments": outside_meeting_segments[:50],
            "voice_inside_meetings_seconds": in_meeting_seconds,
            "active_at_pc_seconds": active_at_pc_seconds,
            "idle_seconds": idle_seconds,
            "keystrokes_total": keystrokes_total,
            "keystrokes_by_app": keystrokes_by_app[:15],
        }


SYSTEM_PROMPT = """\
Ты — эксперт по контролю качества работы менеджеров в компании, которая помогает \
призывникам решать вопросы с военкоматом (отсрочки, призыв, медицинские заключения \
по РБ, юридическая помощь по 53-ФЗ).

Тебе даны структурированные данные за один рабочий день одного менеджера:
- агрегация активных окон/сайтов по категориям (работа/личное/нейтр.)
- список встреч с клиентами и их LLM-анализами
- голосовые сегменты (always-on микрофон) — часть во время встреч, часть вне
- (опционально) данные телефонии — пока эвристика

Твоя задача — дать честный, конкретный разбор работы менеджера по 5 пунктам.
Цифры пиши явно. Не выдумывай. Если данных мало — отметь это.

Отвечай строго JSON-объектом без markdown-форматирования.\
"""


def build_user_prompt(data: dict) -> str:
    meetings_text = "\n".join(
        f"  • встреча #{m['meeting_id']}, {_fmt_secs(m['duration_seconds'] or 0)}, "
        f"клиент={m['client_name'] or '—'}, оценка={m.get('final_score') or '?'}/10\n"
        f"    summary: {m.get('summary') or '—'}\n"
        f"    strengths: {m.get('strengths') or []}\n"
        f"    growth_areas: {m.get('growth_areas') or []}"
        for m in data["meetings"]
    ) or "  (встреч не было)"

    apps_text = "\n".join(
        f"  • [{e['category']:>8}] {e['name']}: {_fmt_secs(e['seconds'])}"
        for e in data["top_entities"]
    ) or "  (нет активности)"

    voice_outside_text = "\n".join(
        f"  [{v['started_at'][11:19]}, {_fmt_secs(v['duration_seconds'])}] {v['text'] or '(транскрипт пуст)'}"
        for v in data["voice_outside_meetings_segments"][:30]
    ) or "  (вне встреч голос не зафиксирован)"

    keystrokes_text = "\n".join(
        f"  • {k['app_name']}: {k['count']} знаков"
        for k in data.get("keystrokes_by_app", [])[:10]
    ) or "  (нет данных по клавиатуре)"

    by_cat = data["by_category"]

    return f"""\
ДАННЫЕ ЗА {data['report_date']}:

Общая активность (по окнам/сайтам):
  работа: {_fmt_secs(by_cat['work'])}
  личное: {_fmt_secs(by_cat['personal'])}
  нейтр.: {_fmt_secs(by_cat['neutral'])}
  ────────────
  всего трекинга: {_fmt_secs(data['tracking_total_seconds'])}

ПРИСУТСТВИЕ ЗА КОМПЬЮТЕРОМ:
  активно за компом: {_fmt_secs(data.get('active_at_pc_seconds', 0))}
  бездействие (idle > 60 сек, отошёл): {_fmt_secs(data.get('idle_seconds', 0))}

КЛАВИАТУРНАЯ АКТИВНОСТЬ:
  всего нажатий: {data.get('keystrokes_total', 0)} знаков
  по приложениям (топ-10):
{keystrokes_text}

ТОП приложений и сайтов:
{apps_text}

ВСТРЕЧИ ({len(data['meetings'])} шт):
{meetings_text}

ГОЛОС ВНЕ ВСТРЕЧ — суммарно {_fmt_secs(data['voice_outside_meetings_seconds'])}.
Это может быть: телефонные звонки (рабочие через телефонию или личные),
очное общение в офисе, разговор по личному телефону. Фрагменты транскрипта:
{voice_outside_text}

ГОЛОС ВО ВРЕМЯ ВСТРЕЧ: {_fmt_secs(data['voice_inside_meetings_seconds'])}.

────────────────────────────────────────

Дай разбор СТРОГО в формате JSON:
{{
  "meetings": {{
    "count": <число>,
    "total_seconds": <число>,
    "average_score": <число 0-10 или null>,
    "key_observations": ["<наблюдение>", ...],
    "top_strengths": ["<сильная сторона по итогам всех встреч>", ...],
    "top_growth_areas": ["<зона роста>", ...]
  }},
  "phone_calls": {{
    "voice_outside_meetings_seconds": <число>,
    "estimated_work_calls_count": <число>,
    "estimated_personal_calls_count": <число>,
    "key_observations": ["<наблюдение>", ...]
  }},
  "procrastination": {{
    "personal_seconds": <число>,
    "top_distractions": [{{"name": "<сайт/прилож>", "seconds": <число>}}, ...],
    "idle_seconds": <число секунд бездействия — отходил от компа>,
    "keystroke_distribution": "<2-3 предложения: где менеджер набирал текст. Например: 'Из 4200 знаков 2800 в AmoCRM (работа) и 1200 в Telegram (личное) — половина клавиатурной активности на личное'>",
    "observations": ["<наблюдение>", ...]
  }},
  "productivity_score": <число 0-10>,
  "overall_summary": "<2-4 предложения общей оценки дня>",
  "red_flags": ["<серьёзная проблема>", ...],
  "recommendations": ["<что улучшить>", ...]
}}

productivity_score:
- 0-3: серьёзные проблемы — много прокрастинации, продаж нет/плохие, время не работал
- 4-6: средне — работал, но с большими отвлечениями или слабые продажи
- 7-8: хорошо — работа+продажи в норме, прокрастинация в пределах
- 9-10: эталон — высокая активность, качественные встречи, минимум личного

ВАЖНО при оценке прокрастинации учитывай:
- idle_seconds большой (>2 ч в день) = менеджер часто отходит от компа
- много keystrokes в личных приложениях (Telegram, ВКонтакте) = личная переписка
- мало keystrokes в work-приложениях + мало встреч = бездействие за компом
"""


def generate_daily_report(agent_id: str, report_date: str) -> dict:
    if not API_KEY:
        raise RuntimeError("OM_OPENROUTER_API_KEY не сконфигурирован")

    data = collect_day_data(agent_id, report_date)
    user_prompt = build_user_prompt(data)
    log.info("daily_report %s/%s: prompt_len=%d", agent_id, report_date, len(user_prompt))

    t0 = time.monotonic()
    r = httpx.post(
        OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "HTTP-Referer": "https://office.lkdzrkk.pro",
            "X-Title": "office-monitoring-daily",
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
    r.raise_for_status()
    resp = r.json()
    content = resp["choices"][0]["message"]["content"]
    elapsed = time.monotonic() - t0
    usage = resp.get("usage", {})
    log.info("daily_report %s/%s: %.1fs prompt_tokens=%s completion_tokens=%s",
             agent_id, report_date, elapsed, usage.get("prompt_tokens"), usage.get("completion_tokens"))

    # robust JSON parse (как в analyze.py)
    from analyze import _extract_json
    parsed = _extract_json(content)
    parsed["_meta"] = {
        "model": MODEL,
        "processing_time_seconds": elapsed,
        "data_snapshot": {  # копируем агрегированные числа в результат, чтобы UI мог их показать
            "tracking_total_seconds": data["tracking_total_seconds"],
            "by_category": data["by_category"],
            "meetings_count": len(data["meetings"]),
        },
    }
    return parsed
