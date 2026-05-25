from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlmodel import Field, Session, SQLModel, create_engine, select

DB_PATH = Path(__file__).parent / "office_monitoring.db"
engine = create_engine(f"sqlite:///{DB_PATH}", echo=False, connect_args={"check_same_thread": False})


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


app = FastAPI(title="office-monitoring server", version="0.2.0")


@app.on_event("startup")
def on_startup() -> None:
    SQLModel.metadata.create_all(engine)


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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
        result = []
        for a in agents:
            last_seen = _as_utc(a.last_seen)
            first_seen = _as_utc(a.first_seen)
            result.append({
                "agent_id": a.agent_id,
                "hostname": a.hostname,
                "username": a.username,
                "first_seen": first_seen.isoformat(),
                "last_seen": last_seen.isoformat(),
                "online": (now - last_seen).total_seconds() < 60,
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
