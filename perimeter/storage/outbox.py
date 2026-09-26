"""Transactional outbox: user events are written in the same transaction as the change that
caused them, then relayed to JetStream (see :mod:`perimeter.bus.relay`)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, BIGINT, BYTEA, TEXT
from sqlalchemy.ext.asyncio import AsyncConnection


@dataclass(frozen=True, slots=True)
class PendingEvent:
    subject: str
    msg_id: str
    payload: bytes


@dataclass(frozen=True, slots=True)
class OutboxRow:
    id: int
    subject: str
    msg_id: str
    payload: bytes


_INSERT = text(
    """
    INSERT INTO outbox (subject, msg_id, payload)
    SELECT subject, msg_id, payload
    FROM unnest(CAST(:subjects AS text[]), CAST(:msg_ids AS text[]), CAST(:payloads AS bytea[]))
         WITH ORDINALITY AS e(subject, msg_id, payload, ord)
    ORDER BY ord
    RETURNING id, subject, msg_id, payload
    """
).bindparams(
    bindparam("subjects", type_=ARRAY(TEXT)),
    bindparam("msg_ids", type_=ARRAY(TEXT)),
    bindparam("payloads", type_=ARRAY(BYTEA)),
)

_DELETE = text("DELETE FROM outbox WHERE id = ANY(CAST(:ids AS bigint[]))").bindparams(
    bindparam("ids", type_=ARRAY(BIGINT))
)

_CLAIM = text(
    """
    SELECT id, subject, msg_id, payload
    FROM outbox
    WHERE created_at < now() - make_interval(secs => :min_age_s)
    ORDER BY id
    LIMIT :limit
    FOR UPDATE SKIP LOCKED
    """
)

_BACKLOG = text(
    "SELECT count(*), coalesce(extract(epoch FROM now() - min(created_at)), 0) FROM outbox"
)


async def insert(conn: AsyncConnection, events: Sequence[PendingEvent]) -> list[OutboxRow]:
    if not events:
        return []
    result = await conn.execute(
        _INSERT,
        {
            "subjects": [e.subject for e in events],
            "msg_ids": [e.msg_id for e in events],
            "payloads": [e.payload for e in events],
        },
    )
    rows = [OutboxRow(r.id, r.subject, r.msg_id, bytes(r.payload)) for r in result]
    rows.sort(key=lambda row: row.id)
    return rows


async def delete(conn: AsyncConnection, ids: Sequence[int]) -> None:
    if ids:
        await conn.execute(_DELETE, {"ids": list(ids)})


async def claim_stale(conn: AsyncConnection, *, min_age_s: float, limit: int) -> list[OutboxRow]:
    result = await conn.execute(_CLAIM, {"min_age_s": min_age_s, "limit": limit})
    return [OutboxRow(r.id, r.subject, r.msg_id, bytes(r.payload)) for r in result]


async def backlog(conn: AsyncConnection) -> tuple[int, float]:
    """Number of unrelayed events and the age of the oldest one, in seconds."""
    row = (await conn.execute(_BACKLOG)).one()
    return int(row[0]), float(row[1])
