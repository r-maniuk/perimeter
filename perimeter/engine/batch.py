"""One batch of one partition, applied in one transaction.

Inside the transaction, in this order:

1. fence: the partition's epoch row is claimed with the lease's current token; a newer token
   already recorded there means another instance owns the partition now, and nothing is written;
2. the newest committed event time of each device in the batch;
3. which active zones contain each report newer than that, in one indexed statement for the whole
   batch (every report, not only the newest, so a device crossing a zone between two batches
   still yields both ``enter`` and ``exit``);
4. the devices' current presence, and the rules of every zone involved, locked against deletion;
5. the presence state machine (:mod:`perimeter.domain.presence`), in pure Python;
6. set-based writes: newest positions, the track of every applied report, presence changes,
   alerts and their outbox events.

After the commit the new events are relayed to JetStream straight away (the outbox sweeper covers
a crash in between), moved devices go to the tile publisher and occupancy to the pulse publisher.
Only then does the worker acknowledge the batch's messages.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import sqlalchemy.exc
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from perimeter.bus.leases import FencingToken
from perimeter.bus.relay import OutboxRelay
from perimeter.domain.clock import SYSTEM_CLOCK, Clock
from perimeter.domain.presence import (
    BatchOutcome,
    Observation,
    Transition,
    ZoneRules,
    evaluate_batch,
)
from perimeter.domain.reports import TelemetryRecord
from perimeter.engine import sql
from perimeter.engine.metrics import (
    ALERTS,
    BATCH_SECONDS,
    BATCH_SIZE,
    BATCHES,
    COMMIT_LAG,
    REPORTS,
    EngineStats,
)
from perimeter.ops import tracing
from perimeter.storage import outbox
from perimeter.storage.outbox import OutboxRow, PendingEvent
from perimeter.storage.spatial import match_zones
from perimeter.wire import subjects
from perimeter.wire.events import EventType, alert_data, encode_event, make_event, new_id

# Retrying the same batch later can succeed: lost or refused connections, lock contention,
# serialisation conflicts, statement timeouts, a server that is starting or shutting down.
TRANSIENT_SQLSTATES = frozenset({"40001", "40P01", "55P03", "57014"})
TRANSIENT_SQLSTATE_CLASSES = ("08", "53", "57P")


class FencedOut(Exception):  # noqa: N818 - a state of the partition, like LeaseLost
    """Another instance has committed to this partition with a newer lease: stop, do not ack."""


def is_transient(exc: BaseException) -> bool:
    """Whether ``exc`` (or anything that caused it) is an operational hiccup, not a defect."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, TimeoutError | OSError | sqlalchemy.exc.TimeoutError):
            return True
        if isinstance(current, sqlalchemy.exc.DBAPIError) and current.connection_invalidated:
            return True
        sqlstate = getattr(current, "sqlstate", None)
        if isinstance(sqlstate, str) and (
            sqlstate in TRANSIENT_SQLSTATES or sqlstate.startswith(TRANSIENT_SQLSTATE_CLASSES)
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


class LiveSink(Protocol):
    """Where committed movement goes for live delivery (the tile publisher)."""

    def positions(self, records: Iterable[TelemetryRecord]) -> None: ...

    def pulses(self, pulses: Mapping[UUID, Mapping[UUID, Iterable[str]]]) -> None: ...


@dataclass(frozen=True, slots=True)
class Applied:
    """What one committed batch did."""

    reports: int
    accepted: int
    late: int
    alerts: int
    moved: int


@dataclass(frozen=True, slots=True)
class _Written:
    moved: list[TelemetryRecord]
    raised: list[Transition]
    events: list[OutboxRow]


class BatchProcessor:
    """Applies the batches of every partition this instance owns."""

    def __init__(
        self,
        db: AsyncEngine,
        relay: OutboxRelay,
        live: LiveSink,
        *,
        owner: str,
        stats: EngineStats,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._db = db
        self._relay = relay
        self._live = live
        self._owner = owner
        self._stats = stats
        self._clock = clock

    async def apply(
        self,
        partition: int,
        token: FencingToken,
        records: Sequence[TelemetryRecord],
        *,
        trace: Mapping[str, str] | None = None,
    ) -> Applied:
        """Apply ``records`` (stream order) as partition ``partition`` under fencing ``token``.

        Raises :class:`FencedOut` without writing anything when a newer owner has committed.
        """
        with tracing.span("engine.batch", headers=trace, partition=partition, reports=len(records)):
            started = self._clock.monotonic()
            async with self._db.begin() as conn:
                if not await sql.fence(conn, partition=partition, token=token, owner=self._owner):
                    msg = (
                        f"partition {partition}: a newer owner has committed (token {tuple(token)})"
                    )
                    raise FencedOut(msg)
                outcome, zones = await _evaluate(conn, records)
                written = await _write(conn, outcome)
            batch_s = self._clock.monotonic() - started
            committed_ms = self._clock.now_ms()
            await self._relay.relay(written.events)
            self._live.positions(written.moved)
            self._live.pulses(outcome.pulses(zones))
        self._observe(records, outcome, written, batch_s=batch_s, committed_ms=committed_ms)
        return Applied(
            reports=len(records),
            accepted=outcome.accepted,
            late=outcome.late,
            alerts=len(written.raised),
            moved=len(written.moved),
        )

    def _observe(
        self,
        records: Sequence[TelemetryRecord],
        outcome: BatchOutcome,
        written: _Written,
        *,
        batch_s: float,
        committed_ms: int,
    ) -> None:
        BATCHES.labels("committed").inc()
        BATCH_SIZE.observe(len(records))
        BATCH_SECONDS.observe(batch_s)
        REPORTS.labels("accepted").inc(outcome.accepted)
        REPORTS.labels("late").inc(outcome.late)
        for transition in written.raised:
            ALERTS.labels(transition.kind.value).inc()
        lags_ms = [
            max(0, committed_ms - record.received_at_ms)
            for device in outcome.devices.values()
            for record in device.accepted
        ]
        for lag_ms in lags_ms:
            COMMIT_LAG.observe(lag_ms / 1000)
        self._stats.record(
            reports=len(records),
            late=outcome.late,
            alerts=len(written.raised),
            batch_s=batch_s,
            lags_ms=lags_ms,
        )


async def _evaluate(
    conn: AsyncConnection, records: Sequence[TelemetryRecord]
) -> tuple[BatchOutcome, dict[UUID, ZoneRules]]:
    devices = sorted({record.device_id for record in records})
    last = await sql.last_recorded(conn, devices)
    observations: dict[str, list[Observation]] = {device: [] for device in devices}
    fresh: list[TelemetryRecord] = []
    for record in records:
        if record.recorded_at_ms > last.get(record.device_id, -1):
            fresh.append(record)
        else:  # already superseded: the state machine counts it as late, no need to match it
            observations[record.device_id].append(Observation(record, frozenset()))
    hits = await match_zones(conn, [r.lon for r in fresh], [r.lat for r in fresh])
    stays = await sql.presence(conn, devices)
    involved = {zone_id for found in hits for zone_id in found}
    for held in stays.values():
        involved.update(held)
    zones = await sql.zone_rules(conn, involved)
    for record, found in zip(fresh, hits, strict=True):
        observations[record.device_id].append(Observation(record, frozenset(found)))
    outcome = evaluate_batch(observations, last_recorded_at_ms=last, stays=stays, zones=zones)
    return outcome, zones


async def _write(conn: AsyncConnection, outcome: BatchOutcome) -> _Written:
    latest = outcome.latest()
    moved = await sql.upsert_devices(conn, latest)
    await sql.insert_tracks(
        conn, [r for device in outcome.devices.values() for r in device.accepted]
    )
    await sql.delete_presence(conn, outcome.deletes())
    await sql.upsert_presence(conn, outcome.upserts())
    candidates = [(new_id(), transition) for transition in outcome.alerts()]
    inserted = await sql.insert_alerts(conn, candidates)
    raised = [(alert_id, t) for alert_id, t in candidates if alert_id in inserted]
    events = await outbox.insert(conn, [_alert_event(alert_id, t) for alert_id, t in raised])
    return _Written(
        moved=[record for record in latest if record.device_id in moved],
        raised=[transition for _, transition in raised],
        events=events,
    )


def _alert_event(alert_id: UUID, transition: Transition) -> PendingEvent:
    event = make_event(EventType.ALERT, alert_data(alert_id, transition), event_id=alert_id)
    return PendingEvent(
        subject=subjects.events(transition.zone.owner_id),
        msg_id=str(alert_id),
        payload=encode_event(event),
    )
