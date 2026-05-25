"""
Кластеризация VoiceSegment'ов в Conversation.

Два сегмента → одна conversation, если разрыв между ними < CLUSTER_GAP_SECONDS.
Сегменты с kind=noise или пустым текстом игнорируем.

Auto-bind к Meeting: если conversation попадает в окно активной встречи по
кнопке календаря — связываем. Это даёт сверку «нажимает ли менеджер кнопку».
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from sqlmodel import Session, select

from main import Conversation, Meeting, VoiceSegment, engine, _as_utc

log = logging.getLogger("worker")

CLUSTER_GAP_SECONDS = 60  # разрыв тишины > этого = новая conversation


def _find_matching_meeting(session: Session, conv: Conversation) -> Meeting | None:
    """Ищет meeting (по кнопке), в окно которого попадает conversation."""
    started = _as_utc(conv.started_at)
    ended = _as_utc(conv.ended_at)
    meetings = session.exec(
        select(Meeting).where(Meeting.agent_id == conv.agent_id)
    ).all()
    for m in meetings:
        m_start = _as_utc(m.started_at)
        m_end = _as_utc(m.ended_at) if m.ended_at else datetime.now(timezone.utc)
        # overlap: max(start) < min(end)
        if max(m_start, started) < min(m_end, ended):
            return m
    return None


def cluster_pending_segments(agent_id: str) -> int:
    """Создаёт Conversation'ы из не-кластеризованных сегментов агента.
    Возвращает кол-во созданных conversation'ов."""
    with Session(engine) as session:
        # берём только классифицированные и не-шумовые
        segs = session.exec(
            select(VoiceSegment)
            .where(VoiceSegment.agent_id == agent_id)
            .where(VoiceSegment.conversation_id.is_(None))
            .where(VoiceSegment.text.is_not(None))
            .where(VoiceSegment.text != "")
            .where(VoiceSegment.kind.is_not(None))
            .where(VoiceSegment.kind != "noise")
            .order_by(VoiceSegment.started_at)
        ).all()

        if not segs:
            return 0

        # группируем
        clusters: list[list[VoiceSegment]] = []
        current = [segs[0]]
        for seg in segs[1:]:
            prev_end = _as_utc(current[-1].ended_at)
            cur_start = _as_utc(seg.started_at)
            gap = (cur_start - prev_end).total_seconds()
            if gap < CLUSTER_GAP_SECONDS:
                current.append(seg)
            else:
                clusters.append(current)
                current = [seg]
        clusters.append(current)

        created = 0
        for cluster in clusters:
            if not cluster:
                continue
            t_start = _as_utc(cluster[0].started_at)
            t_end = _as_utc(cluster[-1].ended_at)
            full_text = " ".join(s.text or "" for s in cluster).strip()

            conv = Conversation(
                agent_id=agent_id,
                started_at=t_start,
                ended_at=t_end,
                duration_seconds=(t_end - t_start).total_seconds(),
                segment_count=len(cluster),
                full_text=full_text,
                clustered_at=datetime.now(timezone.utc),
            )

            # привязка к meeting (по кнопке)
            matching = _find_matching_meeting(session, conv)
            if matching:
                conv.related_meeting_id = matching.id
                conv.sync_status = "matched"
            else:
                conv.sync_status = "standalone"

            session.add(conv)
            session.flush()  # получаем conv.id

            # привязываем сегменты к conversation
            for seg in cluster:
                seg.conversation_id = conv.id
                if matching and seg.meeting_id is None:
                    seg.meeting_id = matching.id
                session.add(seg)

            created += 1

        session.commit()
        if created:
            log.info("agent %s: создано %d conversation(s)", agent_id, created)
        return created


def find_meetings_without_recording(agent_id: str) -> list[int]:
    """Встречи (по кнопке), на которые нет ни одной conversation — sync_status=no_recording.
    Возвращает meeting_id для пометки."""
    with Session(engine) as session:
        meetings = session.exec(
            select(Meeting).where(Meeting.agent_id == agent_id).where(Meeting.ended_at.is_not(None))
        ).all()
        result = []
        for m in meetings:
            has_conv = session.exec(
                select(Conversation).where(Conversation.related_meeting_id == m.id)
            ).first()
            if not has_conv:
                result.append(m.id)
        return result


def get_active_agents_with_pending_segments() -> list[str]:
    """Список agent_id у которых есть не-кластеризованные segments."""
    with Session(engine) as session:
        rows = session.exec(
            select(VoiceSegment.agent_id)
            .where(VoiceSegment.conversation_id.is_(None))
            .where(VoiceSegment.text.is_not(None))
            .where(VoiceSegment.kind.is_not(None))
            .where(VoiceSegment.kind != "noise")
            .distinct()
        ).all()
        return list({r if isinstance(r, str) else r[0] for r in rows})
