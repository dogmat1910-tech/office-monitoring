import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Field, Session, SQLModel, create_engine, select

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "office_monitoring.db"
DASHBOARD_HTML = BASE_DIR / "dashboard.html"
AUDIO_DIR = BASE_DIR / "audio_data"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)
VOICE_DIR = BASE_DIR / "voice_data"
VOICE_DIR.mkdir(parents=True, exist_ok=True)
engine = create_engine(f"sqlite:///{DB_PATH}", echo=False, connect_args={"check_same_thread": False})

# Токен для эндпоинтов которые дёргает внешний сервис (твой самописный календарь).
# Конфигурируется через env. На сервере хранится в /etc/systemd/system/office-monitoring.service
# (Environment="OM_API_TOKEN=..."). Если не задан — meeting-эндпоинты вернут 503.
API_TOKEN = os.environ.get("OM_API_TOKEN", "")


class Agent(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    agent_id: str = Field(index=True, unique=True)
    hostname: str
    username: str
    first_seen: datetime
    last_seen: datetime


class Heartbeat(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    agent_id: str = Field(index=True)
    received_at: datetime
    agent_version: str | None = None


class WindowSample(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    agent_id: str = Field(index=True)
    app_name: str = Field(index=True)
    title: str
    captured_at: datetime = Field(index=True)
    duration_seconds: int


class Meeting(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    agent_id: str = Field(index=True)
    started_at: datetime = Field(index=True)
    ended_at: datetime | None = Field(default=None, index=True)
    client_name: str | None = None
    notes: str | None = None
    external_id: str | None = Field(default=None, index=True)  # id из самописного календаря


class AudioChunk(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    meeting_id: int = Field(index=True)
    chunk_index: int
    file_path: str  # относительно AUDIO_DIR
    received_at: datetime = Field(index=True)
    size_bytes: int


class Transcript(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    meeting_id: int = Field(index=True, unique=True)
    text: str
    language: str | None = None
    model: str
    duration_seconds: float | None = None
    transcribed_at: datetime
    processing_time_seconds: float | None = None


class Analysis(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    meeting_id: int = Field(index=True, unique=True)
    payload_json: str  # сериализованный JSON от LLM (checklist, errors, summary, ...)
    final_score: int | None = None
    model: str
    analyzed_at: datetime
    processing_time_seconds: float | None = None


class AppCategory(SQLModel, table=True):
    """Категория приложения — work | personal | neutral.
    Назначается глобально (одно правило для всех агентов)."""
    id: int | None = Field(default=None, primary_key=True)
    app_name: str = Field(index=True, unique=True)
    category: str  # work | personal | neutral
    updated_at: datetime


class DomainCategory(SQLModel, table=True):
    """Категория веб-домена для активных вкладок в браузере."""
    id: int | None = Field(default=None, primary_key=True)
    domain: str = Field(index=True, unique=True)
    category: str  # work | personal | neutral
    updated_at: datetime


class IdleSample(SQLModel, table=True):
    """Замер idle-времени (как давно нет активности мыши/клавиатуры).
    Если idle_seconds > порога (обычно 60-120), менеджер «не за компом»."""
    id: int | None = Field(default=None, primary_key=True)
    agent_id: str = Field(index=True)
    captured_at: datetime = Field(index=True)
    idle_seconds: float
    interval_seconds: int  # длительность периода с прошлого сэмпла


class KeystrokeSample(SQLModel, table=True):
    """Агрегированная статистика нажатий клавиш по приложению/окну.
    НЕ хранит содержимое нажатий — только счётчик."""
    id: int | None = Field(default=None, primary_key=True)
    agent_id: str = Field(index=True)
    app_name: str = Field(index=True)
    domain: str | None = Field(default=None, index=True)
    captured_at: datetime = Field(index=True)
    interval_seconds: int  # длительность периода с прошлого батча
    keystroke_count: int  # сколько нажатий за период


class DailyReport(SQLModel, table=True):
    """LLM-разбор дня менеджера: встречи, звонки, прокрастинация, оценка."""
    id: int | None = Field(default=None, primary_key=True)
    agent_id: str = Field(index=True)
    report_date: str = Field(index=True)  # YYYY-MM-DD по локальному дню менеджера
    status: str = Field(index=True)  # pending | done | error
    payload_json: str | None = None  # JSON от LLM
    productivity_score: int | None = None
    model: str | None = None
    requested_at: datetime
    completed_at: datetime | None = None
    processing_time_seconds: float | None = None
    error_message: str | None = None


class VoiceSegment(SQLModel, table=True):
    """Сегмент непрерывной речи, найденный VAD-фильтром в always-on записи."""
    id: int | None = Field(default=None, primary_key=True)
    agent_id: str = Field(index=True)
    started_at: datetime = Field(index=True)
    ended_at: datetime
    duration_seconds: float
    file_path: str
    format: str = "opus"
    size_bytes: int
    received_at: datetime
    text: str | None = None
    language: str | None = None
    transcribed_at: datetime | None = None
    kind: str | None = Field(default=None, index=True)
    kind_summary: str | None = None
    kind_confidence: float | None = None
    classified_at: datetime | None = None
    meeting_id: int | None = Field(default=None, index=True)
    conversation_id: int | None = Field(default=None, index=True)


class Conversation(SQLModel, table=True):
    """Группа последовательных VoiceSegment'ов с паузами < CLUSTER_GAP_SECONDS.
    Один разговор = одно «событие» (встреча, звонок, болтовня, ...).
    LLM анализирует разговор целиком, а не каждый сегмент по отдельности."""
    id: int | None = Field(default=None, primary_key=True)
    agent_id: str = Field(index=True)
    started_at: datetime = Field(index=True)
    ended_at: datetime
    duration_seconds: float
    segment_count: int
    full_text: str | None = None  # кэш: склейка transcript'ов сегментов
    clustered_at: datetime

    # Заполняется этапом B (LLM-анализ):
    kind: str | None = Field(default=None, index=True)
    confidence: float | None = None
    is_with_client: bool | None = None
    is_sale_attempt: bool | None = None
    is_sale_closed: bool | None = None
    sale_quality_score: int | None = None
    summary: str | None = None
    payload_json: str | None = None
    analyzed_at: datetime | None = None

    # Связь с meeting (по кнопке календаря):
    related_meeting_id: int | None = Field(default=None, index=True)
    # matched | missed_button | no_recording | standalone
    sync_status: str | None = Field(default=None, index=True)


class HeartbeatIn(BaseModel):
    agent_id: str
    hostname: str
    username: str
    agent_version: str | None = None


class WindowSampleIn(BaseModel):
    app_name: str
    title: str
    captured_at: datetime
    duration_seconds: int


class WindowSamplesIn(BaseModel):
    agent_id: str
    samples: list[WindowSampleIn]


class IdleSampleIn(BaseModel):
    captured_at: datetime
    idle_seconds: float
    interval_seconds: int


class IdleSamplesIn(BaseModel):
    agent_id: str
    samples: list[IdleSampleIn]


class KeystrokeSampleIn(BaseModel):
    app_name: str
    domain: str | None = None
    captured_at: datetime
    interval_seconds: int
    keystroke_count: int


class KeystrokeSamplesIn(BaseModel):
    agent_id: str
    samples: list[KeystrokeSampleIn]


class MeetingStartIn(BaseModel):
    agent_id: str
    client_name: str | None = None
    notes: str | None = None
    external_id: str | None = None


class MeetingStopIn(BaseModel):
    meeting_id: int | None = None
    external_id: str | None = None
    agent_id: str | None = None  # для остановки активной встречи по agent_id


app = FastAPI(title="office-monitoring server", version="0.4.0")


@app.on_event("startup")
def on_startup() -> None:
    SQLModel.metadata.create_all(engine)


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def require_token(authorization: str | None = Header(default=None)) -> None:
    """Проверка Bearer-токена для приватных эндпоинтов."""
    if not API_TOKEN:
        raise HTTPException(503, "OM_API_TOKEN не сконфигурирован на сервере")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "требуется Authorization: Bearer <token>")
    token = authorization.split(" ", 1)[1].strip()
    if token != API_TOKEN:
        raise HTTPException(403, "неверный токен")


@app.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    return HTMLResponse(DASHBOARD_HTML.read_text(encoding="utf-8"))


@app.get("/health")
def health() -> dict[str, str | bool]:
    return {"status": "ok", "api_token_configured": bool(API_TOKEN)}


@app.post("/heartbeat")
def heartbeat(payload: HeartbeatIn) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        agent = session.exec(select(Agent).where(Agent.agent_id == payload.agent_id)).first()
        if agent is None:
            agent = Agent(
                agent_id=payload.agent_id,
                hostname=payload.hostname,
                username=payload.username,
                first_seen=now,
                last_seen=now,
            )
            session.add(agent)
        else:
            agent.last_seen = now
            agent.hostname = payload.hostname
            agent.username = payload.username
            session.add(agent)
        session.add(Heartbeat(agent_id=payload.agent_id, received_at=now, agent_version=payload.agent_version))
        session.commit()
    return {"status": "ok", "server_time": now.isoformat()}


@app.get("/agents")
def list_agents() -> list[dict]:
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        agents = session.exec(select(Agent).order_by(Agent.last_seen.desc())).all()
        # активные встречи для всех агентов одним запросом
        active = session.exec(select(Meeting).where(Meeting.ended_at.is_(None))).all()
        active_by_agent = {m.agent_id: m for m in active}
        result = []
        for a in agents:
            last_seen = _as_utc(a.last_seen)
            first_seen = _as_utc(a.first_seen)
            meeting = active_by_agent.get(a.agent_id)
            result.append({
                "agent_id": a.agent_id,
                "hostname": a.hostname,
                "username": a.username,
                "first_seen": first_seen.isoformat(),
                "last_seen": last_seen.isoformat(),
                "online": (now - last_seen).total_seconds() < 60,
                "active_meeting": (
                    {
                        "meeting_id": meeting.id,
                        "started_at": _as_utc(meeting.started_at).isoformat(),
                        "client_name": meeting.client_name,
                    }
                    if meeting else None
                ),
            })
        return result


@app.post("/window_samples")
def post_window_samples(payload: WindowSamplesIn) -> dict[str, int | str]:
    with Session(engine) as session:
        for s in payload.samples:
            session.add(WindowSample(
                agent_id=payload.agent_id,
                app_name=s.app_name,
                title=s.title,
                captured_at=_as_utc(s.captured_at),
                duration_seconds=s.duration_seconds,
            ))
        session.commit()
    return {"status": "ok", "count": len(payload.samples)}


@app.post("/idle_samples")
def post_idle_samples(payload: IdleSamplesIn) -> dict:
    with Session(engine) as session:
        for s in payload.samples:
            session.add(IdleSample(
                agent_id=payload.agent_id,
                captured_at=_as_utc(s.captured_at),
                idle_seconds=s.idle_seconds,
                interval_seconds=s.interval_seconds,
            ))
        session.commit()
    return {"status": "ok", "count": len(payload.samples)}


@app.post("/keystroke_samples")
def post_keystroke_samples(payload: KeystrokeSamplesIn) -> dict:
    with Session(engine) as session:
        for s in payload.samples:
            if s.keystroke_count <= 0:
                continue
            session.add(KeystrokeSample(
                agent_id=payload.agent_id,
                app_name=s.app_name,
                domain=s.domain,
                captured_at=_as_utc(s.captured_at),
                interval_seconds=s.interval_seconds,
                keystroke_count=s.keystroke_count,
            ))
        session.commit()
    return {"status": "ok", "count": len(payload.samples)}


@app.get("/agents/{agent_id}/activity_summary")
def agent_activity_summary(agent_id: str, hours: int = 24, idle_threshold: int = 60) -> dict:
    """Сводка по бездействию и набору символов за период.
    idle_threshold — порог idle_seconds, выше которого считаем «не за компом»."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    with Session(engine) as session:
        # idle: суммируем interval_seconds где idle_seconds > threshold = «не за компом»
        idle_rows = session.exec(
            select(IdleSample)
            .where(IdleSample.agent_id == agent_id)
            .where(IdleSample.captured_at >= since)
        ).all()
        total_interval = sum(r.interval_seconds for r in idle_rows)
        idle_interval = sum(r.interval_seconds for r in idle_rows if r.idle_seconds > idle_threshold)
        active_interval = total_interval - idle_interval

        # клавиатура: агрегация по app
        ks_rows = session.exec(
            select(KeystrokeSample.app_name, func.sum(KeystrokeSample.keystroke_count))
            .where(KeystrokeSample.agent_id == agent_id)
            .where(KeystrokeSample.captured_at >= since)
            .group_by(KeystrokeSample.app_name)
        ).all()
        ks_by_app = sorted(
            [{"app_name": app or "unknown", "keystrokes": int(n or 0)} for app, n in ks_rows],
            key=lambda x: -x["keystrokes"],
        )
        ks_total = sum(x["keystrokes"] for x in ks_by_app)

        return {
            "agent_id": agent_id,
            "hours": hours,
            "idle_threshold_seconds": idle_threshold,
            "total_tracked_seconds": total_interval,
            "active_seconds": active_interval,
            "idle_seconds": idle_interval,
            "keystrokes_total": ks_total,
            "keystrokes_by_app": ks_by_app[:20],
        }


@app.get("/agents/{agent_id}/windows")
def list_windows(agent_id: str, limit: int = 200) -> list[dict]:
    with Session(engine) as session:
        samples = session.exec(
            select(WindowSample)
            .where(WindowSample.agent_id == agent_id)
            .order_by(WindowSample.captured_at.desc())
            .limit(limit)
        ).all()
        return [
            {
                "app_name": s.app_name,
                "title": s.title,
                "captured_at": _as_utc(s.captured_at).isoformat(),
                "duration_seconds": s.duration_seconds,
            }
            for s in samples
        ]


@app.get("/agents/{agent_id}/summary")
def agent_summary(agent_id: str, hours: int = 24) -> dict:
    """Свод по приложениям за последние N часов: сколько секунд в каждом."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    with Session(engine) as session:
        rows = session.exec(
            select(WindowSample.app_name, func.sum(WindowSample.duration_seconds))
            .where(WindowSample.agent_id == agent_id)
            .where(WindowSample.captured_at >= since)
            .group_by(WindowSample.app_name)
        ).all()
        items = sorted(
            [{"app_name": app or "unknown", "seconds": int(secs or 0)} for app, secs in rows],
            key=lambda x: x["seconds"],
            reverse=True,
        )
        total = sum(item["seconds"] for item in items)
        return {
            "agent_id": agent_id,
            "hours": hours,
            "total_seconds": total,
            "by_app": items,
        }


# ---------- meeting endpoints ----------

@app.post("/meeting/start", dependencies=[Depends(require_token)])
def meeting_start(payload: MeetingStartIn) -> dict:
    """Вызывается твоим календарём при нажатии «Старт встречи»."""
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        # если уже есть открытая встреча — возвращаем её (идемпотентность)
        if payload.external_id:
            existing = session.exec(
                select(Meeting).where(Meeting.external_id == payload.external_id, Meeting.ended_at.is_(None))
            ).first()
            if existing:
                return {"status": "already_running", "meeting_id": existing.id, "started_at": _as_utc(existing.started_at).isoformat()}
        # если у агента уже идёт встреча без external_id — не плодим параллельные
        open_for_agent = session.exec(
            select(Meeting).where(Meeting.agent_id == payload.agent_id, Meeting.ended_at.is_(None))
        ).first()
        if open_for_agent and not payload.external_id:
            return {"status": "already_running", "meeting_id": open_for_agent.id, "started_at": _as_utc(open_for_agent.started_at).isoformat()}
        m = Meeting(
            agent_id=payload.agent_id,
            started_at=now,
            client_name=payload.client_name,
            notes=payload.notes,
            external_id=payload.external_id,
        )
        session.add(m)
        session.commit()
        session.refresh(m)
        return {"status": "ok", "meeting_id": m.id, "started_at": now.isoformat()}


@app.post("/meeting/stop", dependencies=[Depends(require_token)])
def meeting_stop(payload: MeetingStopIn) -> dict:
    """Вызывается твоим календарём при нажатии «Стоп встречи». Принимает один из:
    meeting_id, external_id или agent_id (закроет активную встречу агента)."""
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        q = select(Meeting).where(Meeting.ended_at.is_(None))
        if payload.meeting_id is not None:
            q = q.where(Meeting.id == payload.meeting_id)
        elif payload.external_id:
            q = q.where(Meeting.external_id == payload.external_id)
        elif payload.agent_id:
            q = q.where(Meeting.agent_id == payload.agent_id)
        else:
            raise HTTPException(400, "нужен один из: meeting_id, external_id, agent_id")
        meeting = session.exec(q).first()
        if meeting is None:
            raise HTTPException(404, "активная встреча не найдена")
        meeting.ended_at = now
        session.add(meeting)
        session.commit()
        duration = int((now - _as_utc(meeting.started_at)).total_seconds())
        return {
            "status": "ok",
            "meeting_id": meeting.id,
            "started_at": _as_utc(meeting.started_at).isoformat(),
            "ended_at": now.isoformat(),
            "duration_seconds": duration,
        }


@app.get("/agents/{agent_id}/active_meeting")
def get_active_meeting(agent_id: str) -> dict:
    """Используется агентом: «есть ли у меня сейчас активная встреча?»
    Не требует токена — агент сам не знает токена. Идентифицируется по agent_id."""
    with Session(engine) as session:
        meeting = session.exec(
            select(Meeting).where(Meeting.agent_id == agent_id, Meeting.ended_at.is_(None))
        ).first()
        if meeting is None:
            return {"active": False}
        return {
            "active": True,
            "meeting_id": meeting.id,
            "started_at": _as_utc(meeting.started_at).isoformat(),
            "client_name": meeting.client_name,
        }


# ---------- app + domain categories ----------

# Дефолтный словарь категорий: ключи — app_name (как видит ОС), значения — категория.
DEFAULT_CATEGORIES: dict[str, str] = {
    "AmoCRM": "work", "amoCRM": "work",
    "Outlook": "work", "Microsoft Outlook": "work",
    "Word": "work", "Microsoft Word": "work",
    "Excel": "work", "Microsoft Excel": "work",
    "PowerPoint": "work",
    "Skorozvon": "work",
    "Zoom": "work", "zoom.us": "work",
    "Telemost": "work",
    "Telegram": "neutral", "Telegram Lite": "neutral",
    "WhatsApp": "neutral",
    "ВКонтакте": "neutral",
    "Google Chrome": "neutral", "Chrome": "neutral",
    "Safari": "neutral", "Firefox": "neutral", "Microsoft Edge": "neutral",
    "Arc": "neutral", "Brave Browser": "neutral", "Yandex": "neutral",
    "Finder": "neutral", "Explorer": "neutral", "Windows Explorer": "neutral",
    "Terminal": "neutral", "iTerm2": "neutral", "Windows Terminal": "neutral",
    "System Settings": "neutral", "Системные настройки": "neutral",
    "YouTube": "personal", "TikTok": "personal", "Instagram": "personal",
    "Spotify": "personal", "Steam": "personal", "Discord": "personal",
}

# Дефолтный словарь категорий доменов. Используется когда трекаем браузерную вкладку.
DEFAULT_DOMAIN_CATEGORIES: dict[str, str] = {
    # work
    "amocrm.ru": "work", "amocrm.com": "work",
    "bitrix24.ru": "work", "bitrix24.com": "work",
    "gmail.com": "work", "mail.google.com": "work",
    "outlook.live.com": "work", "outlook.office.com": "work", "outlook.office365.com": "work",
    "docs.google.com": "work", "drive.google.com": "work", "sheets.google.com": "work",
    "skorozvon.ru": "work",
    "lkdzrkk.pro": "work", "office.lkdzrkk.pro": "work",
    "github.com": "work", "gitlab.com": "work",
    "notion.so": "work", "trello.com": "work", "asana.com": "work",
    "office.com": "work",
    "zoom.us": "work", "meet.google.com": "work", "telemost.yandex.ru": "work",
    # personal
    "youtube.com": "personal", "youtu.be": "personal", "m.youtube.com": "personal",
    "tiktok.com": "personal",
    "instagram.com": "personal",
    "twitter.com": "personal", "x.com": "personal",
    "reddit.com": "personal", "old.reddit.com": "personal",
    "twitch.tv": "personal",
    "spotify.com": "personal", "open.spotify.com": "personal", "music.yandex.ru": "personal",
    "netflix.com": "personal", "kinopoisk.ru": "personal", "ivi.ru": "personal", "okko.tv": "personal",
    "store.steampowered.com": "personal", "steamcommunity.com": "personal",
    "discord.com": "personal",
    "pikabu.ru": "personal", "habr.com": "personal",
    "9gag.com": "personal", "joyreactor.cc": "personal",
    # neutral (зависит от контекста)
    "google.com": "neutral", "google.ru": "neutral",
    "yandex.ru": "neutral", "ya.ru": "neutral",
    "vk.com": "neutral", "m.vk.com": "neutral",
    "t.me": "neutral", "web.telegram.org": "neutral", "telegram.org": "neutral",
    "ozon.ru": "neutral", "wildberries.ru": "neutral", "avito.ru": "neutral",
    "stackoverflow.com": "neutral",
    "wikipedia.org": "neutral", "ru.wikipedia.org": "neutral", "en.wikipedia.org": "neutral",
    "github.io": "neutral",
}

BROWSER_APPS = {
    "Google Chrome", "Google Chrome Canary", "Chrome",
    "Safari", "Firefox", "Microsoft Edge",
    "Arc", "Brave Browser", "Yandex", "Яндекс.Браузер",
}

# title окон от агента приходит как «Page Title — https://example.com/page»
_URL_IN_TITLE_RE = re.compile(r" — (https?://\S+)\s*$")


def extract_url_from_title(title: str | None) -> str | None:
    if not title:
        return None
    m = _URL_IN_TITLE_RE.search(title)
    return m.group(1) if m else None


def extract_domain(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = urlparse(url).hostname
        if host and host.startswith("www."):
            host = host[4:]
        return host.lower() if host else None
    except Exception:
        return None


def get_category_map(session: Session) -> dict[str, str]:
    """Объединяет дефолтный словарь с пользовательскими переопределениями."""
    merged = dict(DEFAULT_CATEGORIES)
    for r in session.exec(select(AppCategory)).all():
        merged[r.app_name] = r.category
    return merged


def get_domain_category_map(session: Session) -> dict[str, str]:
    merged = dict(DEFAULT_DOMAIN_CATEGORIES)
    for r in session.exec(select(DomainCategory)).all():
        merged[r.domain] = r.category
    return merged


def categorize_sample(app_name: str, title: str | None, app_map: dict, domain_map: dict) -> tuple[str, str, str]:
    """Возвращает (display_name, category, target_kind).
    target_kind = 'app' | 'domain' — для UI чтобы знать что менять в dropdown."""
    app = app_name or "unknown"
    if app in BROWSER_APPS:
        url = extract_url_from_title(title)
        domain = extract_domain(url)
        if domain:
            cat = domain_map.get(domain)
            if cat is None:
                # fallback на категорию приложения если домен не известен
                cat = app_map.get(app, "neutral")
            return f"{app} · {domain}", cat, "domain"
    cat = app_map.get(app, "neutral")
    return app, cat, "app"


class CategoryIn(BaseModel):
    name: str  # app_name или domain
    category: str  # work | personal | neutral
    kind: str = "app"  # app | domain


@app.get("/app_categories")
def list_app_categories() -> dict:
    with Session(engine) as session:
        app_merged = get_category_map(session)
        app_user = {r.app_name for r in session.exec(select(AppCategory)).all()}
        domain_merged = get_domain_category_map(session)
        domain_user = {r.domain for r in session.exec(select(DomainCategory)).all()}
    return {
        "apps": [
            {"name": k, "category": v, "user_defined": k in app_user}
            for k, v in sorted(app_merged.items())
        ],
        "domains": [
            {"name": k, "category": v, "user_defined": k in domain_user}
            for k, v in sorted(domain_merged.items())
        ],
    }


@app.post("/app_categories")
def set_app_category(payload: CategoryIn) -> dict:
    if payload.category not in ("work", "personal", "neutral"):
        raise HTTPException(400, "category должен быть work | personal | neutral")
    if payload.kind not in ("app", "domain"):
        raise HTTPException(400, "kind должен быть app или domain")
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        if payload.kind == "domain":
            existing = session.exec(select(DomainCategory).where(DomainCategory.domain == payload.name)).first()
            if existing:
                existing.category = payload.category
                existing.updated_at = now
                session.add(existing)
            else:
                session.add(DomainCategory(domain=payload.name, category=payload.category, updated_at=now))
        else:
            existing = session.exec(select(AppCategory).where(AppCategory.app_name == payload.name)).first()
            if existing:
                existing.category = payload.category
                existing.updated_at = now
                session.add(existing)
            else:
                session.add(AppCategory(app_name=payload.name, category=payload.category, updated_at=now))
        session.commit()
    return {"status": "ok", "kind": payload.kind, "name": payload.name, "category": payload.category}


@app.get("/agents/{agent_id}/day_summary")
def agent_day_summary(agent_id: str, hours: int = 24) -> dict:
    """Свод дня менеджера: время по категориям + список приложений (с разбивкой
    по доменам для браузеров) + время на встречах."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    with Session(engine) as session:
        app_map = get_category_map(session)
        domain_map = get_domain_category_map(session)

        # Для браузеров не можем тупо группировать в БД — title содержит URL,
        # домены разные. Берём все сэмплы и группируем в Python.
        samples = session.exec(
            select(WindowSample.app_name, WindowSample.title, WindowSample.duration_seconds)
            .where(WindowSample.agent_id == agent_id)
            .where(WindowSample.captured_at >= since)
        ).all()

        by_category: dict[str, int] = {"work": 0, "personal": 0, "neutral": 0}
        by_app: dict[str, dict] = {}
        for app_name, title, secs in samples:
            secs = int(secs or 0)
            display, cat, kind = categorize_sample(app_name, title, app_map, domain_map)
            by_category[cat] = by_category.get(cat, 0) + secs
            if display not in by_app:
                # target_name — что менять при изменении категории в UI
                if kind == "domain":
                    # display = "Google Chrome · youtube.com" → target = "youtube.com"
                    target_name = display.split(" · ", 1)[1]
                else:
                    target_name = display
                by_app[display] = {
                    "seconds": secs,
                    "category": cat,
                    "target_kind": kind,
                    "target_name": target_name,
                }
            else:
                by_app[display]["seconds"] += secs

        # время на встречах
        meetings = session.exec(
            select(Meeting)
            .where(Meeting.agent_id == agent_id)
            .where(Meeting.ended_at.is_not(None))
            .where(Meeting.ended_at >= since)
        ).all()
        meeting_seconds = sum(
            int((_as_utc(m.ended_at) - _as_utc(m.started_at)).total_seconds())
            for m in meetings
            if m.ended_at
        )

        total_tracked = sum(by_category.values())
        return {
            "agent_id": agent_id,
            "hours": hours,
            "since": since.isoformat(),
            "total_tracked_seconds": total_tracked,
            "meeting_seconds": meeting_seconds,
            "by_category": by_category,
            "by_app": [
                {"app_name": k, **v}
                for k, v in sorted(by_app.items(), key=lambda x: -x[1]["seconds"])
            ],
            "meetings_count": len(meetings),
        }


@app.post("/recategorize")
def recategorize_old_data() -> dict:
    """Пересчёт ничего не сохраняет — категории всегда вычисляются на лету."""
    return {"status": "ok", "note": "категории вычисляются динамически, пересчёт не нужен"}


# ---------- Команда (overview всех агентов) ----------

@app.get("/overview")
def team_overview(hours: int = 24) -> dict:
    """Свод по всем агентам за период hours: для главного экрана.
    Возвращает по каждому агенту: статус online, разбивка времени по категориям,
    кол-во встреч + средняя оценка, продуктивность из последнего daily report,
    кол-во голос-сегментов с разбивкой по kind."""
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    today_str = now.strftime("%Y-%m-%d")

    with Session(engine) as session:
        agents = session.exec(select(Agent).order_by(Agent.last_seen.desc())).all()
        app_map = get_category_map(session)
        domain_map = get_domain_category_map(session)

        # Активные встречи — для бейджа
        active_meetings = session.exec(select(Meeting).where(Meeting.ended_at.is_(None))).all()
        active_by_agent = {m.agent_id: m for m in active_meetings}

        result = []
        for a in agents:
            last_seen = _as_utc(a.last_seen)
            online = (now - last_seen).total_seconds() < 60

            # окна → категории
            samples = session.exec(
                select(WindowSample.app_name, WindowSample.title, WindowSample.duration_seconds)
                .where(WindowSample.agent_id == a.agent_id)
                .where(WindowSample.captured_at >= since)
            ).all()
            by_cat = {"work": 0, "personal": 0, "neutral": 0}
            for app_name, title, secs in samples:
                _, cat, _ = categorize_sample(app_name, title, app_map, domain_map)
                by_cat[cat] = by_cat.get(cat, 0) + int(secs or 0)

            # встречи
            meetings = session.exec(
                select(Meeting)
                .where(Meeting.agent_id == a.agent_id)
                .where(Meeting.started_at >= since)
            ).all()
            meeting_seconds = sum(
                int((_as_utc(m.ended_at) - _as_utc(m.started_at)).total_seconds())
                for m in meetings if m.ended_at
            )
            # средняя оценка по завершённым встречам за период
            meeting_ids = [m.id for m in meetings if m.id]
            scores = []
            if meeting_ids:
                analyses = session.exec(
                    select(Analysis).where(Analysis.meeting_id.in_(meeting_ids))
                ).all()
                scores = [an.final_score for an in analyses if an.final_score is not None]
            avg_meeting_score = round(sum(scores) / len(scores), 1) if scores else None

            # голос
            voice = session.exec(
                select(VoiceSegment.kind, func.count(VoiceSegment.id), func.sum(VoiceSegment.duration_seconds))
                .where(VoiceSegment.agent_id == a.agent_id)
                .where(VoiceSegment.started_at >= since)
                .group_by(VoiceSegment.kind)
            ).all()
            voice_by_kind = {(k or "unclassified"): {"count": int(n or 0), "seconds": int(s or 0)} for k, n, s in voice}
            voice_total_seconds = sum(v["seconds"] for v in voice_by_kind.values())

            # последний daily report (сегодня)
            daily = session.exec(
                select(DailyReport)
                .where(DailyReport.agent_id == a.agent_id)
                .where(DailyReport.report_date == today_str)
            ).first()
            daily_score = daily.productivity_score if daily and daily.status == "done" else None
            daily_status = daily.status if daily else None
            red_flags_count = 0
            if daily and daily.payload_json:
                try:
                    payload = json.loads(daily.payload_json)
                    red_flags_count = len(payload.get("red_flags") or [])
                except Exception:
                    pass

            result.append({
                "agent_id": a.agent_id,
                "hostname": a.hostname,
                "username": a.username,
                "online": online,
                "last_seen": last_seen.isoformat(),
                "active_meeting": (
                    {"started_at": _as_utc(active_by_agent[a.agent_id].started_at).isoformat(),
                     "client_name": active_by_agent[a.agent_id].client_name}
                    if a.agent_id in active_by_agent else None
                ),
                "by_category": by_cat,
                "meeting_seconds": meeting_seconds,
                "meetings_count": len(meetings),
                "avg_meeting_score": avg_meeting_score,
                "voice_total_seconds": voice_total_seconds,
                "voice_by_kind": voice_by_kind,
                "daily_score": daily_score,
                "daily_status": daily_status,
                "red_flags_count": red_flags_count,
            })

        # Сортировка: онлайн сначала, далее по daily_score asc (худшие сверху)
        result.sort(key=lambda x: (
            not x["online"],
            x["daily_score"] if x["daily_score"] is not None else 999,
            -x["meetings_count"],
        ))

        return {
            "hours": hours,
            "now": now.isoformat(),
            "total_agents": len(result),
            "online_count": sum(1 for r in result if r["online"]),
            "agents": result,
        }


# ---------- Daily Report ----------

@app.post("/agents/{agent_id}/daily_report")
def request_daily_report(agent_id: str, date: str) -> dict:
    """Создаёт pending-запись отчёта (или возвращает существующий). Воркер генерирует."""
    # валидация даты
    try:
        datetime.fromisoformat(date)
    except ValueError:
        raise HTTPException(400, "date должен быть YYYY-MM-DD")
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        existing = session.exec(
            select(DailyReport).where(DailyReport.agent_id == agent_id, DailyReport.report_date == date)
        ).first()
        if existing:
            if existing.status == "done":
                return {"status": "done", "report_id": existing.id, "regenerated": False}
            if existing.status == "pending":
                return {"status": "pending", "report_id": existing.id}
            # error → пересоздаём
            session.delete(existing)
            session.commit()
        rep = DailyReport(
            agent_id=agent_id,
            report_date=date,
            status="pending",
            requested_at=now,
        )
        session.add(rep)
        session.commit()
        session.refresh(rep)
        return {"status": "pending", "report_id": rep.id}


@app.post("/agents/{agent_id}/daily_report/regenerate")
def regenerate_daily_report(agent_id: str, date: str) -> dict:
    """Удаляет существующий отчёт и просит новый."""
    try:
        datetime.fromisoformat(date)
    except ValueError:
        raise HTTPException(400, "date должен быть YYYY-MM-DD")
    with Session(engine) as session:
        existing = session.exec(
            select(DailyReport).where(DailyReport.agent_id == agent_id, DailyReport.report_date == date)
        ).first()
        if existing:
            session.delete(existing)
            session.commit()
    return request_daily_report(agent_id, date)


@app.get("/agents/{agent_id}/daily_report")
def get_daily_report(agent_id: str, date: str) -> dict:
    try:
        datetime.fromisoformat(date)
    except ValueError:
        raise HTTPException(400, "date должен быть YYYY-MM-DD")
    with Session(engine) as session:
        rep = session.exec(
            select(DailyReport).where(DailyReport.agent_id == agent_id, DailyReport.report_date == date)
        ).first()
        if rep is None:
            return {"status": "absent", "agent_id": agent_id, "date": date}
        payload = json.loads(rep.payload_json) if rep.payload_json else None
        return {
            "status": rep.status,
            "report_id": rep.id,
            "agent_id": rep.agent_id,
            "date": rep.report_date,
            "requested_at": _as_utc(rep.requested_at).isoformat(),
            "completed_at": _as_utc(rep.completed_at).isoformat() if rep.completed_at else None,
            "model": rep.model,
            "productivity_score": rep.productivity_score,
            "processing_time_seconds": rep.processing_time_seconds,
            "error_message": rep.error_message,
            "payload": payload,
        }


# ---------- voice segments (always-on аудио) ----------

@app.post("/voice_segments")
async def upload_voice_segment(
    agent_id: str = Form(...),
    started_at: str = Form(...),  # ISO datetime
    ended_at: str = Form(...),
    file: UploadFile = File(...),
) -> dict:
    """Always-on рекордер шлёт сегменты речи (отфильтрованные VAD'ом)."""
    contents = await file.read()
    try:
        t_start = datetime.fromisoformat(started_at)
        t_end = datetime.fromisoformat(ended_at)
    except ValueError as e:
        raise HTTPException(400, f"некорректный datetime: {e}")
    t_start = _as_utc(t_start)
    t_end = _as_utc(t_end)
    duration = (t_end - t_start).total_seconds()

    # путь: voice_data/{agent_id}/{YYYY-MM-DD}/{HH}/{segment_TS}.opus
    rel_dir = Path(agent_id) / t_start.strftime("%Y-%m-%d") / t_start.strftime("%H")
    abs_dir = VOICE_DIR / rel_dir
    abs_dir.mkdir(parents=True, exist_ok=True)
    fname = f"seg_{int(t_start.timestamp() * 1000)}.opus"
    abs_path = abs_dir / fname
    abs_path.write_bytes(contents)
    rel_path = str(rel_dir / fname)

    with Session(engine) as session:
        seg = VoiceSegment(
            agent_id=agent_id,
            started_at=t_start,
            ended_at=t_end,
            duration_seconds=duration,
            file_path=rel_path,
            format="opus",
            size_bytes=len(contents),
            received_at=datetime.now(timezone.utc),
        )
        session.add(seg)
        session.commit()
        session.refresh(seg)
        return {"status": "ok", "segment_id": seg.id, "size_bytes": len(contents), "duration_seconds": duration}


@app.get("/agents/{agent_id}/conversations")
def list_conversations(agent_id: str, hours: int = 24, limit: int = 100) -> list[dict]:
    """Сгруппированные разговоры (последовательные voice segments) за период."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    with Session(engine) as session:
        rows = session.exec(
            select(Conversation)
            .where(Conversation.agent_id == agent_id)
            .where(Conversation.started_at >= since)
            .order_by(Conversation.started_at.desc())
            .limit(limit)
        ).all()
        return [
            {
                "conversation_id": c.id,
                "started_at": _as_utc(c.started_at).isoformat(),
                "ended_at": _as_utc(c.ended_at).isoformat(),
                "duration_seconds": c.duration_seconds,
                "segment_count": c.segment_count,
                "full_text": c.full_text,
                "kind": c.kind,
                "confidence": c.confidence,
                "is_with_client": c.is_with_client,
                "is_sale_attempt": c.is_sale_attempt,
                "is_sale_closed": c.is_sale_closed,
                "sale_quality_score": c.sale_quality_score,
                "summary": c.summary,
                "related_meeting_id": c.related_meeting_id,
                "sync_status": c.sync_status,
                "analyzed_at": _as_utc(c.analyzed_at).isoformat() if c.analyzed_at else None,
            }
            for c in rows
        ]


@app.get("/agents/{agent_id}/conversations_summary")
def conversations_summary(agent_id: str, hours: int = 24) -> dict:
    """Сводка: сколько разговоров каждого типа, сверка кнопка vs запись."""
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    with Session(engine) as session:
        convs = session.exec(
            select(Conversation)
            .where(Conversation.agent_id == agent_id)
            .where(Conversation.started_at >= since)
        ).all()
        by_kind: dict[str, dict] = {}
        sales_attempts = 0
        sales_closed = 0
        for c in convs:
            kind = c.kind or "unclassified"
            if kind not in by_kind:
                by_kind[kind] = {"count": 0, "duration_seconds": 0}
            by_kind[kind]["count"] += 1
            by_kind[kind]["duration_seconds"] += int(c.duration_seconds or 0)
            if c.is_sale_attempt:
                sales_attempts += 1
            if c.is_sale_closed:
                sales_closed += 1

        # встречи по кнопке за тот же период
        meetings = session.exec(
            select(Meeting)
            .where(Meeting.agent_id == agent_id)
            .where(Meeting.started_at >= since)
        ).all()
        meetings_by_button = len(meetings)
        meetings_by_recording = by_kind.get("meeting", {}).get("count", 0)

        # missed buttons: встречи по кнопке без conversation
        meetings_with_conv = {c.related_meeting_id for c in convs if c.related_meeting_id}
        meetings_without_recording = [m.id for m in meetings if m.id not in meetings_with_conv]

        # missed recordings: conversations с kind=meeting без related_meeting_id
        conversations_without_button = sum(
            1 for c in convs if c.kind == "meeting" and not c.related_meeting_id
        )

        return {
            "agent_id": agent_id,
            "hours": hours,
            "total_conversations": len(convs),
            "by_kind": by_kind,
            "meetings_by_button": meetings_by_button,
            "meetings_by_recording": meetings_by_recording,
            "meetings_without_recording": meetings_without_recording,
            "conversations_without_button": conversations_without_button,
            "sales_attempts": sales_attempts,
            "sales_closed": sales_closed,
        }


@app.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: int) -> dict:
    import json as _json
    with Session(engine) as session:
        c = session.exec(select(Conversation).where(Conversation.id == conversation_id)).first()
        if c is None:
            raise HTTPException(404, "conversation не найден")
        segs = session.exec(
            select(VoiceSegment).where(VoiceSegment.conversation_id == conversation_id).order_by(VoiceSegment.started_at)
        ).all()
        return {
            "conversation_id": c.id,
            "agent_id": c.agent_id,
            "started_at": _as_utc(c.started_at).isoformat(),
            "ended_at": _as_utc(c.ended_at).isoformat(),
            "duration_seconds": c.duration_seconds,
            "full_text": c.full_text,
            "kind": c.kind,
            "summary": c.summary,
            "is_with_client": c.is_with_client,
            "is_sale_attempt": c.is_sale_attempt,
            "is_sale_closed": c.is_sale_closed,
            "sale_quality_score": c.sale_quality_score,
            "related_meeting_id": c.related_meeting_id,
            "sync_status": c.sync_status,
            "payload": _json.loads(c.payload_json) if c.payload_json else None,
            "segments": [
                {
                    "segment_id": s.id,
                    "started_at": _as_utc(s.started_at).isoformat(),
                    "duration_seconds": s.duration_seconds,
                    "text": s.text,
                    "kind": s.kind,
                }
                for s in segs
            ],
        }


@app.get("/agents/{agent_id}/voice_segments")
def list_voice_segments(
    agent_id: str,
    hours: int = 8,
    limit: int = 200,
    transcribed_only: bool = False,
) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    with Session(engine) as session:
        q = (
            select(VoiceSegment)
            .where(VoiceSegment.agent_id == agent_id)
            .where(VoiceSegment.started_at >= since)
            .order_by(VoiceSegment.started_at.desc())
            .limit(limit)
        )
        if transcribed_only:
            q = q.where(VoiceSegment.text.is_not(None))
        segs = session.exec(q).all()
        return [
            {
                "segment_id": s.id,
                "started_at": _as_utc(s.started_at).isoformat(),
                "ended_at": _as_utc(s.ended_at).isoformat(),
                "duration_seconds": s.duration_seconds,
                "size_bytes": s.size_bytes,
                "transcribed": s.text is not None,
                "text": s.text,
                "language": s.language,
                "kind": s.kind,
                "kind_summary": s.kind_summary,
                "kind_confidence": s.kind_confidence,
                "meeting_id": s.meeting_id,
            }
            for s in segs
        ]


@app.post("/meetings/{meeting_id}/audio")
async def upload_audio_chunk(
    meeting_id: int,
    chunk_index: int = Form(...),
    file: UploadFile = File(...),
) -> dict:
    """Агент шлёт WAV-чанки во время записи встречи."""
    contents = await file.read()
    audio_dir = AUDIO_DIR / str(meeting_id)
    audio_dir.mkdir(parents=True, exist_ok=True)
    path = audio_dir / f"chunk_{chunk_index:04d}.wav"
    path.write_bytes(contents)

    rel_path = str(path.relative_to(AUDIO_DIR))
    with Session(engine) as session:
        # idempotency: один и тот же (meeting_id, chunk_index) перезаписываем
        existing = session.exec(
            select(AudioChunk).where(AudioChunk.meeting_id == meeting_id, AudioChunk.chunk_index == chunk_index)
        ).first()
        if existing:
            existing.file_path = rel_path
            existing.size_bytes = len(contents)
            existing.received_at = datetime.now(timezone.utc)
            session.add(existing)
        else:
            session.add(AudioChunk(
                meeting_id=meeting_id,
                chunk_index=chunk_index,
                file_path=rel_path,
                received_at=datetime.now(timezone.utc),
                size_bytes=len(contents),
            ))
        session.commit()
    return {"status": "ok", "meeting_id": meeting_id, "chunk_index": chunk_index, "size_bytes": len(contents)}


@app.get("/meetings/{meeting_id}/analysis")
def get_meeting_analysis(meeting_id: int) -> dict:
    with Session(engine) as session:
        a = session.exec(select(Analysis).where(Analysis.meeting_id == meeting_id)).first()
        if a is None:
            return {"meeting_id": meeting_id, "status": "pending"}
        return {
            "meeting_id": meeting_id,
            "status": "done",
            "final_score": a.final_score,
            "model": a.model,
            "analyzed_at": _as_utc(a.analyzed_at).isoformat(),
            "processing_time_seconds": a.processing_time_seconds,
            **json.loads(a.payload_json),
        }


@app.get("/meetings/{meeting_id}/transcript")
def get_meeting_transcript(meeting_id: int) -> dict:
    with Session(engine) as session:
        t = session.exec(select(Transcript).where(Transcript.meeting_id == meeting_id)).first()
        if t is None:
            return {"meeting_id": meeting_id, "status": "pending"}
        return {
            "meeting_id": meeting_id,
            "status": "done",
            "text": t.text,
            "language": t.language,
            "model": t.model,
            "duration_seconds": t.duration_seconds,
            "transcribed_at": _as_utc(t.transcribed_at).isoformat(),
            "processing_time_seconds": t.processing_time_seconds,
        }


@app.get("/meetings/{meeting_id}/audio")
def list_meeting_audio(meeting_id: int) -> dict:
    with Session(engine) as session:
        chunks = session.exec(
            select(AudioChunk).where(AudioChunk.meeting_id == meeting_id).order_by(AudioChunk.chunk_index)
        ).all()
        return {
            "meeting_id": meeting_id,
            "chunks": [
                {
                    "chunk_index": c.chunk_index,
                    "file_path": c.file_path,
                    "received_at": _as_utc(c.received_at).isoformat(),
                    "size_bytes": c.size_bytes,
                }
                for c in chunks
            ],
            "total_chunks": len(chunks),
            "total_bytes": sum(c.size_bytes for c in chunks),
        }


@app.get("/meetings")
def list_meetings(agent_id: str | None = None, limit: int = 50) -> list[dict]:
    """История встреч. Без авторизации — пока используется только дашбордом."""
    with Session(engine) as session:
        q = select(Meeting).order_by(Meeting.started_at.desc()).limit(limit)
        if agent_id:
            q = q.where(Meeting.agent_id == agent_id)
        meetings = session.exec(q).all()
        return [
            {
                "meeting_id": m.id,
                "agent_id": m.agent_id,
                "started_at": _as_utc(m.started_at).isoformat(),
                "ended_at": _as_utc(m.ended_at).isoformat() if m.ended_at else None,
                "duration_seconds": (
                    int((_as_utc(m.ended_at) - _as_utc(m.started_at)).total_seconds())
                    if m.ended_at else None
                ),
                "client_name": m.client_name,
                "external_id": m.external_id,
            }
            for m in meetings
        ]
