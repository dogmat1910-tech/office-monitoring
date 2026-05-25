from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
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


class HeartbeatIn(BaseModel):
    agent_id: str
    hostname: str
    username: str
    agent_version: str | None = None


app = FastAPI(title="office-monitoring server", version="0.1.0")


@app.on_event("startup")
def on_startup() -> None:
    SQLModel.metadata.create_all(engine)


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


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


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
