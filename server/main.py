import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
