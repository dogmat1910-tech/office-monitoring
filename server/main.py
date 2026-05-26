import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
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
SCREENSHOTS_DIR = BASE_DIR / "screenshots_data"
SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
engine = create_engine(f"sqlite:///{DB_PATH}", echo=False, connect_args={"check_same_thread": False})

# Токен для эндпоинтов которые дёргает внешний сервис (твой самописный календарь).
# Конфигурируется через env. На сервере хранится в /etc/systemd/system/office-monitoring.service
# (Environment="OM_API_TOKEN=..."). Если не задан — meeting-эндпоинты вернут 503.
API_TOKEN = os.environ.get("OM_API_TOKEN", "")

# Per-machine токены для агента: при установке installer.ps1 шлёт
# install-код, сервер выдаёт уникальный bearer-токен для машины.
# Дальше агент шлёт его в Authorization: Bearer на все защищённые эндпоинты.
INSTALL_CODE = os.environ.get("OM_INSTALL_CODE", "")
REQUIRE_AGENT_AUTH = os.environ.get("OM_REQUIRE_AGENT_AUTH", "0") == "1"


class Office(SQLModel, table=True):
    """Офис / отдел / город, к которому привязан менеджер.
    Используется для группировки агентов в дашборде."""
    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    city: str | None = None
    timezone: str | None = None  # например 'Europe/Moscow', 'Asia/Vladivostok'
    work_hours_from: int = 9   # начало рабочего дня (час локального времени офиса)
    work_hours_to: int = 18    # конец рабочего дня
    created_at: datetime


class Agent(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    agent_id: str = Field(index=True, unique=True)
    hostname: str
    username: str
    first_seen: datetime
    last_seen: datetime
    office_id: int | None = Field(default=None, index=True)
    display_name: str | None = None  # ФИО менеджера для дашборда; если пусто — hostname


class Heartbeat(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    agent_id: str = Field(index=True)
    received_at: datetime
    agent_version: str | None = None


class AgentToken(SQLModel, table=True):
    """Per-machine Bearer-токен. Хранится только sha256(token), сам токен — нет.
    При компрометации БД нельзя восстановить рабочие токены."""
    id: int | None = Field(default=None, primary_key=True)
    agent_id: str = Field(index=True)
    token_hash: str = Field(unique=True, index=True)
    created_at: datetime
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None
    note: str | None = None  # "ноут Васи Пупкина" — для админа


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
    auto_categorized: bool = False  # True если LLM определил
    confidence: float | None = None  # 0..1, оценка уверенности LLM


class DomainCategory(SQLModel, table=True):
    """Категория веб-домена для активных вкладок в браузере."""
    id: int | None = Field(default=None, primary_key=True)
    domain: str = Field(index=True, unique=True)
    category: str  # work | personal | neutral
    updated_at: datetime
    auto_categorized: bool = False
    confidence: float | None = None


class Screenshot(SQLModel, table=True):
    """Скриншот primary monitor'а агента, делается при заходе в personal/neutral.
    Хранится JPEG 1280px q=70 в screenshots_data/{agent_id}/{date}/{HH}/.
    OCR-текст заполняется фоновым воркером (Tesseract рус+англ)."""
    id: int | None = Field(default=None, primary_key=True)
    agent_id: str = Field(index=True)
    captured_at: datetime = Field(index=True)
    app_name: str | None = Field(default=None, index=True)
    title: str | None = None
    category: str | None = Field(default=None, index=True)  # work | personal | neutral
    trigger: str | None = None  # window_change | periodic_personal | periodic_neutral
    file_path: str  # относительно SCREENSHOTS_DIR
    size_bytes: int
    received_at: datetime
    # OCR (заполняется воркером):
    ocr_text: str | None = None
    ocr_at: datetime | None = None


class AgentDiagnostics(SQLModel, table=True):
    """Self-report агента: версия, разрешения, доступные модули, состояние сети.
    Перезаписываем при каждом приёме — храним только последний снимок на агента."""
    id: int | None = Field(default=None, primary_key=True)
    agent_id: str = Field(index=True, unique=True)
    received_at: datetime
    agent_version: str | None = None
    python_version: str | None = None
    platform: str | None = None
    payload_json: str  # полный JSON со всеми деталями


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
    speaker_label: str | None = None  # SPEAKER_00, SPEAKER_01, ... после diarization


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

    # Diarization (заполняется отдельным этапом воркера):
    speakers_count: int | None = None
    speakers_timeline_json: str | None = None  # сохранённый список turns
    diarized_at: datetime | None = None


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


# Защищённые агентские пути — для записи данных. GET-эндпоинты остаются публичными
# (категории, версия) — это нужно для bootstrap'а нового агента.
_AGENT_PROTECTED_EXACT = {
    "/heartbeat", "/window_samples", "/idle_samples", "/keystroke_samples",
    "/voice_segments", "/screenshots", "/diagnostics",
}
_AGENT_PROTECTED_RE = [
    re.compile(r"^/meetings/[^/]+/audio$"),
    re.compile(r"^/agents/[^/]+/active_meeting$"),
]


def _is_protected_agent_path(path: str) -> bool:
    if path in _AGENT_PROTECTED_EXACT:
        return True
    return any(p.match(path) for p in _AGENT_PROTECTED_RE)


@app.middleware("http")
async def agent_auth_middleware(request: Request, call_next):
    """Проверяет Authorization: Bearer <token> для агентских записывающих эндпоинтов.

    Если OM_REQUIRE_AGENT_AUTH=0 — пропускает всё (backward compat для агентов
    которые ещё не обновились на версию с per-machine токенами).
    После того как все агенты получат токены — включаем enforce."""
    if REQUIRE_AGENT_AUTH and request.method in ("POST", "PUT") and _is_protected_agent_path(request.url.path):
        authz = request.headers.get("authorization", "")
        if not authz.lower().startswith("bearer "):
            return JSONResponse({"detail": "missing bearer token"}, status_code=401)
        token = authz[7:].strip()
        if not token:
            return JSONResponse({"detail": "empty bearer token"}, status_code=401)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with Session(engine) as session:
            row = session.exec(
                select(AgentToken)
                .where(AgentToken.token_hash == token_hash)
                .where(AgentToken.revoked_at == None)  # noqa: E711
            ).first()
            if not row:
                return JSONResponse({"detail": "invalid or revoked token"}, status_code=401)
            row.last_used_at = datetime.now(timezone.utc)
            session.add(row)
            session.commit()
    return await call_next(request)


_AUTO_CATEGORIZE_INTERVAL = int(os.environ.get("OM_AUTO_CATEGORIZE_INTERVAL", "900"))  # 15 минут


async def _auto_categorize_loop() -> None:
    """Периодически прогоняет LLM-категоризацию для новых app/domain."""
    import auto_categorize as ac
    loop = asyncio.get_running_loop()
    # Небольшая задержка перед первым прогоном, чтобы дать API подняться
    await asyncio.sleep(30)
    while True:
        try:
            result = await loop.run_in_executor(None, ac.run_once, engine)
            if result["apps_added"] or result["domains_added"]:
                logging.getLogger("main").info(
                    "auto-categorize: %d apps + %d domains добавлено",
                    result["apps_added"], result["domains_added"],
                )
        except Exception as e:
            logging.getLogger("main").warning("auto-categorize loop error: %s", e)
        await asyncio.sleep(_AUTO_CATEGORIZE_INTERVAL)


@app.on_event("startup")
def on_startup() -> None:
    SQLModel.metadata.create_all(engine)
    # Background task для LLM-категоризации — запускаем только если ключ OpenRouter есть
    if os.environ.get("OM_OPENROUTER_API_KEY"):
        asyncio.create_task(_auto_categorize_loop())


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# Локальный таймзон сервера: для прода вынести в env или брать из Office.timezone
LOCAL_TZ_OFFSET = timedelta(hours=3)  # UTC+3 (Москва)


def _time_range(date: str | None, hours: int) -> tuple[datetime, datetime]:
    """Возвращает [start, end) в UTC.
    - date=YYYY-MM-DD: весь день этой даты (00:00 - 24:00 локального времени)
    - date=None: последние hours часов от сейчас."""
    if date:
        try:
            d = datetime.fromisoformat(date)
        except ValueError:
            raise HTTPException(400, f"date должен быть YYYY-MM-DD, получено {date!r}")
        start = (d - LOCAL_TZ_OFFSET).replace(tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        return start, end
    now = datetime.now(timezone.utc)
    return now - timedelta(hours=hours), now


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


_SHA256_CACHE: dict[Path, tuple[float, str]] = {}


def _cached_sha256(path: Path) -> str:
    mtime = path.stat().st_mtime
    cached = _SHA256_CACHE.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    import hashlib as _hl
    h = _hl.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    digest = h.hexdigest()
    _SHA256_CACHE[path] = (mtime, digest)
    return digest


@app.get("/agent/version")
def get_agent_version() -> dict:
    """Возвращает текущую версию .exe и URL'ы для скачивания.

    Источник: /opt/office-monitoring/public/ — туда systemd-timer
    кладёт свежие office-monitoring-{agent,watchdog}.exe из GitHub releases
    и VERSION-файл с тегом релиза. Агент сравнивает свою AGENT_VERSION
    с этим и качает обновление если новее.
    """
    public_dir = Path(os.environ.get("OM_PUBLIC_DIR", "/opt/office-monitoring/public"))
    base_url = os.environ.get("OM_PUBLIC_BASE_URL", "https://office.lkdzrkk.pro")

    version_file = public_dir / "VERSION"
    agent_exe = public_dir / "agent.exe"
    watchdog_exe = public_dir / "watchdog.exe"

    version = "0.0.0"
    if version_file.exists():
        version = version_file.read_text().strip().lstrip("v")
    else:
        version = os.environ.get("OM_AGENT_VERSION", "0.0.0")

    out: dict = {
        "version": version,
        "agent_exe_url": f"{base_url}/agent.exe",
        "watchdog_exe_url": f"{base_url}/watchdog.exe",
    }
    if agent_exe.exists():
        out["sha256_agent"] = _cached_sha256(agent_exe)
    if watchdog_exe.exists():
        out["sha256_watchdog"] = _cached_sha256(watchdog_exe)
    return out


class AgentRegisterIn(BaseModel):
    install_code: str
    agent_id: str
    hostname: str | None = None
    username: str | None = None


@app.post("/agent/register")
def agent_register(payload: AgentRegisterIn) -> dict:
    """Выдаёт per-machine Bearer-токен в обмен на install-код.
    Если для agent_id уже есть активный токен — отзывает его (переустановка машины).
    """
    if not INSTALL_CODE:
        raise HTTPException(503, "install code not configured on server")
    # constant-time сравнение чтобы не давать таймингу подсказать длину
    if not secrets.compare_digest(payload.install_code, INSTALL_CODE):
        raise HTTPException(401, "invalid install code")

    token = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    now = datetime.now(timezone.utc)

    with Session(engine) as session:
        active = session.exec(
            select(AgentToken)
            .where(AgentToken.agent_id == payload.agent_id)
            .where(AgentToken.revoked_at == None)  # noqa: E711
        ).all()
        for old in active:
            old.revoked_at = now
            session.add(old)
        new_row = AgentToken(
            agent_id=payload.agent_id,
            token_hash=token_hash,
            created_at=now,
            note=f"{payload.hostname or '?'}/{payload.username or '?'}",
        )
        session.add(new_row)
        session.commit()

    return {"token": token, "agent_id": payload.agent_id}


@app.get("/agent_tokens")
def list_agent_tokens() -> list[dict]:
    with Session(engine) as session:
        rows = session.exec(
            select(AgentToken).order_by(AgentToken.created_at.desc())
        ).all()
        return [
            {
                "id": r.id,
                "agent_id": r.agent_id,
                "created_at": _as_utc(r.created_at).isoformat(),
                "revoked_at": _as_utc(r.revoked_at).isoformat() if r.revoked_at else None,
                "last_used_at": _as_utc(r.last_used_at).isoformat() if r.last_used_at else None,
                "note": r.note,
            }
            for r in rows
        ]


@app.post("/agent_tokens/{token_id}/revoke")
def revoke_agent_token(token_id: int) -> dict:
    with Session(engine) as session:
        row = session.get(AgentToken, token_id)
        if not row:
            raise HTTPException(404, "token not found")
        if row.revoked_at is None:
            row.revoked_at = datetime.now(timezone.utc)
            session.add(row)
            session.commit()
        return {"ok": True, "agent_id": row.agent_id}


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
                "display_name": a.display_name,
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


class AgentDisplayNameIn(BaseModel):
    display_name: str | None = None  # пустая строка / None сбрасывает имя


@app.post("/agents/{agent_id}/display_name")
def set_agent_display_name(agent_id: str, payload: AgentDisplayNameIn) -> dict:
    with Session(engine) as session:
        agent = session.exec(select(Agent).where(Agent.agent_id == agent_id)).first()
        if not agent:
            raise HTTPException(404, "agent not found")
        new_name = (payload.display_name or "").strip() or None
        agent.display_name = new_name
        session.add(agent)
        session.commit()
        return {"agent_id": agent_id, "display_name": new_name}


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


@app.post("/diagnostics")
async def post_diagnostics(request: Request) -> dict:
    """Агент шлёт self-diagnostics при старте + раз в час."""
    body = await request.json()
    agent_id = body.get("agent_id")
    if not agent_id:
        raise HTTPException(400, "agent_id обязателен")
    info = body.get("info", {})
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        existing = session.exec(select(AgentDiagnostics).where(AgentDiagnostics.agent_id == agent_id)).first()
        payload = json.dumps(info, ensure_ascii=False)
        if existing:
            existing.received_at = now
            existing.agent_version = info.get("agent_version")
            existing.python_version = info.get("python_version")
            existing.platform = info.get("platform")
            existing.payload_json = payload
            session.add(existing)
        else:
            session.add(AgentDiagnostics(
                agent_id=agent_id,
                received_at=now,
                agent_version=info.get("agent_version"),
                python_version=info.get("python_version"),
                platform=info.get("platform"),
                payload_json=payload,
            ))
        session.commit()
    return {"status": "ok"}


@app.get("/agents/{agent_id}/diagnostics")
def get_diagnostics(agent_id: str) -> dict:
    with Session(engine) as session:
        d = session.exec(select(AgentDiagnostics).where(AgentDiagnostics.agent_id == agent_id)).first()
        if d is None:
            return {"agent_id": agent_id, "status": "no_diagnostics"}
        return {
            "agent_id": agent_id,
            "received_at": _as_utc(d.received_at).isoformat(),
            "agent_version": d.agent_version,
            "python_version": d.python_version,
            "platform": d.platform,
            "info": json.loads(d.payload_json),
        }


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
def agent_activity_summary(agent_id: str, hours: int = 24, date: str | None = None, idle_threshold: int = 60) -> dict:
    """Сводка по бездействию и набору символов за период."""
    since, until = _time_range(date, hours)
    with Session(engine) as session:
        # idle: суммируем interval_seconds где idle_seconds > threshold = «не за компом»
        idle_rows = session.exec(
            select(IdleSample)
            .where(IdleSample.agent_id == agent_id)
            .where(IdleSample.captured_at >= since)
            .where(IdleSample.captured_at < until)
        ).all()
        total_interval = sum(r.interval_seconds for r in idle_rows)
        idle_interval = sum(r.interval_seconds for r in idle_rows if r.idle_seconds > idle_threshold)
        active_interval = total_interval - idle_interval

        # начало и конец работы — первый и последний активный сэмпл за период
        active_samples = [r for r in idle_rows if r.idle_seconds <= idle_threshold]
        first_at = min((r.captured_at for r in active_samples), default=None)
        last_at = max((r.captured_at for r in active_samples), default=None)

        # клавиатура: агрегация по app
        ks_rows = session.exec(
            select(KeystrokeSample.app_name, func.sum(KeystrokeSample.keystroke_count))
            .where(KeystrokeSample.agent_id == agent_id)
            .where(KeystrokeSample.captured_at >= since)
            .where(KeystrokeSample.captured_at < until)
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
            "first_activity_at": _as_utc(first_at).isoformat() if first_at else None,
            "last_activity_at": _as_utc(last_at).isoformat() if last_at else None,
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
def agent_summary(agent_id: str, hours: int = 24, date: str | None = None) -> dict:
    """Свод по приложениям за период."""
    since, until = _time_range(date, hours)
    with Session(engine) as session:
        rows = session.exec(
            select(WindowSample.app_name, func.sum(WindowSample.duration_seconds))
            .where(WindowSample.agent_id == agent_id)
            .where(WindowSample.captured_at >= since)
            .where(WindowSample.captured_at < until)
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
        domain_merged = get_domain_category_map(session)
        app_rows = {r.app_name: r for r in session.exec(select(AppCategory)).all()}
        domain_rows = {r.domain: r for r in session.exec(select(DomainCategory)).all()}

    def app_meta(k: str, v: str) -> dict:
        r = app_rows.get(k)
        return {
            "name": k,
            "category": v,
            "user_defined": k in app_rows,
            "auto_categorized": bool(r.auto_categorized) if r else False,
            "confidence": r.confidence if r else None,
        }

    def domain_meta(k: str, v: str) -> dict:
        r = domain_rows.get(k)
        return {
            "name": k,
            "category": v,
            "user_defined": k in domain_rows,
            "auto_categorized": bool(r.auto_categorized) if r else False,
            "confidence": r.confidence if r else None,
        }

    return {
        "apps": [app_meta(k, v) for k, v in sorted(app_merged.items())],
        "domains": [domain_meta(k, v) for k, v in sorted(domain_merged.items())],
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
                existing.auto_categorized = False  # админ переопределил, LLM больше не трогает
                existing.confidence = None
                session.add(existing)
            else:
                session.add(DomainCategory(
                    domain=payload.name, category=payload.category,
                    updated_at=now, auto_categorized=False,
                ))
        else:
            existing = session.exec(select(AppCategory).where(AppCategory.app_name == payload.name)).first()
            if existing:
                existing.category = payload.category
                existing.updated_at = now
                existing.auto_categorized = False
                existing.confidence = None
                session.add(existing)
            else:
                session.add(AppCategory(
                    app_name=payload.name, category=payload.category,
                    updated_at=now, auto_categorized=False,
                ))
        session.commit()
    return {"status": "ok", "kind": payload.kind, "name": payload.name, "category": payload.category}


@app.post("/admin/recategorize")
def admin_recategorize(lookback_days: int = 7) -> dict:
    """Запускает LLM-категоризацию приложений и доменов, у которых пока нет категории."""
    import auto_categorize as ac
    apps_added = ac.categorize_new_apps(engine, lookback_days=lookback_days)
    domains_added = ac.categorize_new_domains(engine, lookback_days=lookback_days)
    return {"apps_added": apps_added, "domains_added": domains_added}


@app.get("/agents/{agent_id}/day_summary")
def agent_day_summary(agent_id: str, hours: int = 24, date: str | None = None) -> dict:
    """Свод дня менеджера: время по категориям + список приложений + время на встречах."""
    since, until = _time_range(date, hours)
    with Session(engine) as session:
        app_map = get_category_map(session)
        domain_map = get_domain_category_map(session)

        samples = session.exec(
            select(WindowSample.app_name, WindowSample.title, WindowSample.duration_seconds)
            .where(WindowSample.agent_id == agent_id)
            .where(WindowSample.captured_at >= since)
            .where(WindowSample.captured_at < until)
        ).all()

        # для иконки 🤖 — какие категории расставлены LLM, а какие админом
        app_auto = {r.app_name: bool(r.auto_categorized) for r in session.exec(select(AppCategory)).all()}
        domain_auto = {r.domain: bool(r.auto_categorized) for r in session.exec(select(DomainCategory)).all()}

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
                    auto = domain_auto.get(target_name, False)
                else:
                    target_name = display
                    auto = app_auto.get(target_name, False)
                by_app[display] = {
                    "seconds": secs,
                    "category": cat,
                    "target_kind": kind,
                    "target_name": target_name,
                    "auto_categorized": auto,
                }
            else:
                by_app[display]["seconds"] += secs

        # время на встречах
        meetings = session.exec(
            select(Meeting)
            .where(Meeting.agent_id == agent_id)
            .where(Meeting.ended_at.is_not(None))
            .where(Meeting.ended_at >= since)
            .where(Meeting.ended_at < until)
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


# ---------- Offices ----------

class OfficeIn(BaseModel):
    name: str
    city: str | None = None
    timezone: str | None = None
    work_hours_from: int = 9
    work_hours_to: int = 18


@app.get("/offices")
def list_offices() -> list[dict]:
    with Session(engine) as session:
        offices = session.exec(select(Office).order_by(Office.name)).all()
        # подсчёт агентов на офис
        counts: dict[int, int] = {}
        for a in session.exec(select(Agent)).all():
            if a.office_id is not None:
                counts[a.office_id] = counts.get(a.office_id, 0) + 1
        return [
            {
                "office_id": o.id,
                "name": o.name,
                "city": o.city,
                "timezone": o.timezone,
                "work_hours_from": o.work_hours_from,
                "work_hours_to": o.work_hours_to,
                "agents_count": counts.get(o.id, 0),
            }
            for o in offices
        ]


@app.post("/offices")
def create_office(payload: OfficeIn) -> dict:
    now = datetime.now(timezone.utc)
    with Session(engine) as session:
        existing = session.exec(select(Office).where(Office.name == payload.name)).first()
        if existing:
            raise HTTPException(400, f"офис «{payload.name}» уже существует")
        o = Office(
            name=payload.name,
            city=payload.city,
            timezone=payload.timezone,
            work_hours_from=payload.work_hours_from,
            work_hours_to=payload.work_hours_to,
            created_at=now,
        )
        session.add(o)
        session.commit()
        session.refresh(o)
        return {"status": "ok", "office_id": o.id, "name": o.name}


@app.delete("/offices/{office_id}")
def delete_office(office_id: int) -> dict:
    with Session(engine) as session:
        o = session.exec(select(Office).where(Office.id == office_id)).first()
        if o is None:
            raise HTTPException(404, "офис не найден")
        # отвязываем агентов от удаляемого офиса
        for a in session.exec(select(Agent).where(Agent.office_id == office_id)).all():
            a.office_id = None
            session.add(a)
        session.delete(o)
        session.commit()
        return {"status": "ok"}


@app.post("/agents/{agent_id}/office")
def assign_agent_to_office(agent_id: str, office_id: int | None = None) -> dict:
    """Привязка агента к офису. office_id=null отвязывает."""
    with Session(engine) as session:
        a = session.exec(select(Agent).where(Agent.agent_id == agent_id)).first()
        if a is None:
            raise HTTPException(404, "агент не найден")
        if office_id is not None:
            o = session.exec(select(Office).where(Office.id == office_id)).first()
            if o is None:
                raise HTTPException(404, "офис не найден")
        a.office_id = office_id
        session.add(a)
        session.commit()
        return {"status": "ok", "agent_id": agent_id, "office_id": office_id}


# ---------- Команда (overview всех агентов) ----------

@app.get("/overview")
def team_overview(hours: int = 24, date: str | None = None) -> dict:
    """Свод по всем агентам за период hours или за конкретный день."""
    now = datetime.now(timezone.utc)
    since, until = _time_range(date, hours)
    # для daily report берём дату из параметра, или сегодняшнюю
    today_str = date if date else now.strftime("%Y-%m-%d")

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
                .where(WindowSample.captured_at < until)
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

            office = None
            if a.office_id is not None:
                o = session.exec(select(Office).where(Office.id == a.office_id)).first()
                if o:
                    office = {"office_id": o.id, "name": o.name, "city": o.city}
            result.append({
                "agent_id": a.agent_id,
                "hostname": a.hostname,
                "username": a.username,
                "display_name": a.display_name,
                "online": online,
                "last_seen": last_seen.isoformat(),
                "office": office,
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

        # сводка по офисам
        offices_summary: dict = {}
        for r in result:
            key = r["office"]["name"] if r["office"] else "(без офиса)"
            if key not in offices_summary:
                offices_summary[key] = {
                    "name": key,
                    "agents_total": 0,
                    "agents_online": 0,
                    "meetings_count": 0,
                    "work_seconds": 0,
                    "personal_seconds": 0,
                    "red_flags_count": 0,
                }
            s = offices_summary[key]
            s["agents_total"] += 1
            if r["online"]: s["agents_online"] += 1
            s["meetings_count"] += r["meetings_count"]
            s["work_seconds"] += r["by_category"].get("work", 0)
            s["personal_seconds"] += r["by_category"].get("personal", 0)
            s["red_flags_count"] += r["red_flags_count"]

        return {
            "hours": hours,
            "now": now.isoformat(),
            "total_agents": len(result),
            "online_count": sum(1 for r in result if r["online"]),
            "agents": result,
            "offices_summary": sorted(offices_summary.values(), key=lambda x: -x["agents_total"]),
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


# ---------- Screenshots ----------

@app.post("/screenshots")
async def upload_screenshot(
    agent_id: str = Form(...),
    captured_at: str = Form(...),
    app_name: str = Form(""),
    title: str = Form(""),
    category: str = Form(""),
    trigger: str = Form(""),
    file: UploadFile = File(...),
) -> dict:
    """Агент шлёт скриншот при triger-условии."""
    contents = await file.read()
    try:
        ts = _as_utc(datetime.fromisoformat(captured_at))
    except ValueError as e:
        raise HTTPException(400, f"некорректный captured_at: {e}")

    # путь: screenshots_data/{agent_id}/{YYYY-MM-DD}/{HH}/scr_{ms}.jpg
    rel_dir = Path(agent_id) / ts.strftime("%Y-%m-%d") / ts.strftime("%H")
    abs_dir = SCREENSHOTS_DIR / rel_dir
    abs_dir.mkdir(parents=True, exist_ok=True)
    fname = f"scr_{int(ts.timestamp() * 1000)}.jpg"
    abs_path = abs_dir / fname
    abs_path.write_bytes(contents)
    rel_path = str(rel_dir / fname)

    with Session(engine) as session:
        sh = Screenshot(
            agent_id=agent_id,
            captured_at=ts,
            app_name=app_name or None,
            title=title or None,
            category=category or None,
            trigger=trigger or None,
            file_path=rel_path,
            size_bytes=len(contents),
            received_at=datetime.now(timezone.utc),
        )
        session.add(sh)
        session.commit()
        session.refresh(sh)
        return {"status": "ok", "screenshot_id": sh.id, "size_bytes": len(contents)}


@app.get("/screenshots/{screenshot_id}/image")
def get_screenshot_image(screenshot_id: int):
    """Отдаёт сам JPEG (под basic auth через Caddy для дашборда)."""
    from fastapi.responses import FileResponse
    with Session(engine) as session:
        sh = session.exec(select(Screenshot).where(Screenshot.id == screenshot_id)).first()
        if sh is None:
            raise HTTPException(404, "screenshot not found")
        path = SCREENSHOTS_DIR / sh.file_path
        if not path.exists():
            raise HTTPException(404, "file not found")
        return FileResponse(path, media_type="image/jpeg")


@app.get("/agents/{agent_id}/screenshots")
def list_screenshots(agent_id: str, hours: int = 24, date: str | None = None, limit: int = 200) -> list[dict]:
    since, until = _time_range(date, hours)
    with Session(engine) as session:
        rows = session.exec(
            select(Screenshot)
            .where(Screenshot.agent_id == agent_id)
            .where(Screenshot.captured_at >= since)
            .where(Screenshot.captured_at < until)
            .order_by(Screenshot.captured_at.desc())
            .limit(limit)
        ).all()
        return [
            {
                "screenshot_id": s.id,
                "captured_at": _as_utc(s.captured_at).isoformat(),
                "app_name": s.app_name,
                "title": s.title,
                "category": s.category,
                "trigger": s.trigger,
                "size_bytes": s.size_bytes,
                "ocr_text": s.ocr_text,
                "ocr_done": s.ocr_at is not None,
            }
            for s in rows
        ]


@app.get("/agents/{agent_id}/conversations")
def list_conversations(agent_id: str, hours: int = 24, date: str | None = None, limit: int = 100) -> list[dict]:
    """Сгруппированные разговоры (последовательные voice segments) за период."""
    since, until = _time_range(date, hours)
    with Session(engine) as session:
        rows = session.exec(
            select(Conversation)
            .where(Conversation.agent_id == agent_id)
            .where(Conversation.started_at >= since)
            .where(Conversation.started_at < until)
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
def conversations_summary(agent_id: str, hours: int = 24, date: str | None = None) -> dict:
    """Сводка: сколько разговоров каждого типа, сверка кнопка vs запись."""
    since, until = _time_range(date, hours)
    with Session(engine) as session:
        convs = session.exec(
            select(Conversation)
            .where(Conversation.agent_id == agent_id)
            .where(Conversation.started_at >= since)
            .where(Conversation.started_at < until)
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
            .where(Meeting.started_at < until)
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
    date: str | None = None,
    limit: int = 200,
    transcribed_only: bool = False,
) -> list[dict]:
    since, until = _time_range(date, hours)
    with Session(engine) as session:
        q = (
            select(VoiceSegment)
            .where(VoiceSegment.agent_id == agent_id)
            .where(VoiceSegment.started_at >= since)
            .where(VoiceSegment.started_at < until)
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
