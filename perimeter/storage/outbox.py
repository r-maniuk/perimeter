"""Transactional outbox: user events are written in the same transaction as the change that
caused them, then relayed to JetStream (see :mod:`perimeter.bus.relay`).

Each row keeps the trace context of the span that wrote it (the request or the engine batch), so
the event joins that trace whichever path relays it, and however much later. Without tracing the
column stays empty.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Row, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY, BIGINT, BYTEA, JSONB, TEXT
from sqlalchemy.ext.asyncio import AsyncConnection

from perimeter.ops import tracing


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
    trace_context: dict[str, str] | None = None  # headers (``traceparent``...) to publish with


_INSERT = text(
    """
    INSERT INTO outbox (subject, msg_id, payload, trace_context)
    SELECT subject, msg_id, payload, :trace_context
    FROM unnest(CAST(:subjects AS text[]), CAST(:msg_ids AS text[]), CAST(:payloads AS bytea[]))
         WITH ORDINALITY AS e(subject, msg_id, payload, ord)
    ORDER BY ord
    RETURNING id, subject, msg_id, payload, trace_context
    """
).bindparams(
    bindparam("subjects", type_=ARRAY(TEXT)),
    bindparam("msg_ids", type_=ARRAY(TEXT)),
    bindparam("payloads", type_=ARRAY(BYTEA)),
    bindparam("trace_context", type_=JSONB(none_as_null=True)),
)

_DELETE = text("DELETE FROM outbox WHERE id = ANY(CAST(:ids AS bigint[]))").bindparams(
    bindparam("ids", type_=ARRAY(BIGINT))
)

_CLAIM = text(
    """
    UPDATE outbox SET claimed_until = now() + make_interval(secs => :hold_s)
    WHERE id IN (
        SELECT id
        FROM outbox
        WHERE created_at < now() - make_interval(secs => :min_age_s)
          AND (claimed_until IS NULL OR claimed_until < now())
        ORDER BY id
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
    )
    RETURNING id, subject, msg_id, payload, trace_context
    """
)

_BACKLOG = text(
    "SELECT count(*), coalesce(extract(epoch FROM now() - min(created_at)), 0) FROM outbox"
)


async def insert(conn: AsyncConnection, events: Sequence[PendingEvent]) -> list[OutboxRow]:
    """Write ``events``, each with the trace context of the span writing them, if any."""
    if not events:
        return []
    result = await conn.execute(
        _INSERT,
        {
            "subjects": [e.subject for e in events],
            "msg_ids": [e.msg_id for e in events],
            "payloads": [e.payload for e in events],
            "trace_context": tracing.context(),
        },
    )
    rows = [_outbox_row(r) for r in result]
    rows.sort(key=lambda row: row.id)
    return rows


async def delete(conn: AsyncConnection, ids: Sequence[int]) -> None:
    if ids:
        await conn.execute(_DELETE, {"ids": list(ids)})


async def claim_stale(
    conn: AsyncConnection, *, min_age_s: float, limit: int, hold_s: float
) -> list[OutboxRow]:
    """Rows older than ``min_age_s``, reserved for this caller for ``hold_s`` seconds.

    The reservation is its own short transaction, so nothing stays open while the rows are
    published; rows of a caller that died before deleting them can be claimed again once it lapses.
    """
    result = await conn.execute(_CLAIM, {"min_age_s": min_age_s, "limit": limit, "hold_s": hold_s})
    rows = [_outbox_row(r) for r in result]
    rows.sort(key=lambda row: row.id)  # publish in commit order
    return rows


def _outbox_row(row: Row[Any]) -> OutboxRow:
    return OutboxRow(row.id, row.subject, row.msg_id, bytes(row.payload), row.trace_context)


async def backlog(conn: AsyncConnection) -> tuple[int, float]:
    """Number of unrelayed events and the age of the oldest one, in seconds."""
    row = (await conn.execute(_BACKLOG)).one()
    return int(row[0]), float(row[1])
