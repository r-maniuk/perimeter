"""Geozones: CRUD, occupancy and occupants, optimistic concurrency and zone events.

* Strictly per owner: another user's zone answers 404, exactly like a zone that never existed.
* Every change bumps ``version``. Responses carry ``ETag: "v<version>"``; ``PATCH`` and ``DELETE``
  honour ``If-Match`` and answer 412 when the zone changed since the client read it, so two tabs
  editing one zone cannot silently overwrite each other.
* Every change writes its zone event to the outbox in the same transaction and relays it right
  after commit: all sessions of the owner, on any replica, see the change live, and a change is
  never announced without having happened, nor happens without being announced.
* Deactivating a zone forgets who is inside it, in the same transaction, so a re-activated zone
  starts empty and its first report inside raises ``enter``. Geometry edits keep presence and let
  the next report decide, because ``enter``/``exit`` must describe physical movement.
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Header, Path, Query, Response, status
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from perimeter.api.deps import CurrentUser, State
from perimeter.api.errors import ProblemError, not_found, unauthorized
from perimeter.api.pagination import CursorError, decode_cursor, encode_cursor
from perimeter.api.schemas import (
    LatLon,
    Occupant,
    Occupants,
    Zone,
    ZoneCreate,
    ZonePage,
    ZonePatch,
)
from perimeter.api.state import AppState
from perimeter.storage import outbox, zones
from perimeter.storage.outbox import OutboxRow, PendingEvent
from perimeter.storage.zones import ZoneRecord, ZoneSpec
from perimeter.wire import subjects
from perimeter.wire.events import EventType, encode_event, make_event

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/geozones", tags=["geozones"])

CURSOR_KIND = "zones"
TRANSACTION_ATTEMPTS = 3
RETRYABLE_SQLSTATES = frozenset({"40001", "40P01"})  # serialization failure, deadlock detected
FOREIGN_KEY_VIOLATION = "23503"

ZoneId = Annotated[uuid.UUID, Path(description="Zone id")]
IfMatch = Annotated[
    str | None,
    Header(description='Apply only if the zone is still at this ETag (`"v<version>"`) or `*`'),
]

_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"description": "No such zone (or it belongs to someone else)"}
}
_PRECONDITION: dict[int | str, dict[str, Any]] = {
    412: {"description": "The zone changed since the If-Match ETag was read"}
}


def etag(version: int) -> str:
    return f'"v{version}"'


def if_match_allows(header: str | None, version: int) -> bool:
    """RFC 9110 ``If-Match`` with strong comparison: a weak tag never matches."""
    if header is None:
        return True
    tags = {tag.strip() for tag in header.split(",")}
    return "*" in tags or etag(version) in tags


def zone_body(record: ZoneRecord) -> Zone:
    spec = record.spec
    return Zone(
        id=record.id,
        name=spec.name,
        color=spec.color,
        center=LatLon(lat=spec.lat, lon=spec.lon),
        radius_m=spec.radius_m,
        is_active=spec.is_active,
        notify_enter=spec.notify_enter,
        notify_exit=spec.notify_exit,
        dwell_s=spec.dwell_s,
        version=record.version,
        created_at=record.created_at,
        updated_at=record.updated_at,
        occupancy=record.occupancy,
    )


def merged(spec: ZoneSpec, patch: ZonePatch) -> ZoneSpec:
    """``spec`` with the fields the client sent in ``patch``."""
    changes: dict[str, Any] = {
        name: getattr(patch, name)
        for name in patch.model_fields_set
        if name != "center" and name in ZoneSpec.__dataclass_fields__
    }
    if patch.center is not None:
        changes["lat"], changes["lon"] = patch.center.lat, patch.center.lon
    return dataclasses.replace(spec, **changes)


def _event(event_type: EventType, owner_id: uuid.UUID, data: dict[str, Any]) -> PendingEvent:
    event = make_event(event_type, data)
    return PendingEvent(subjects.events(owner_id), event.id, encode_event(event))


def _precondition(if_match: str | None, version: int) -> None:
    if not if_match_allows(if_match, version):
        raise ProblemError(
            412,
            "precondition_failed",
            "the zone changed since you read it; reload it and apply your change again",
            headers={"ETag": etag(version)},
            extra={"current_version": version},
        )


def _sqlstate(exc: DBAPIError) -> str | None:
    state: str | None = getattr(exc.orig, "sqlstate", None)
    return state


async def _in_transaction[T](state: AppState, work: Callable[[AsyncConnection], Awaitable[T]]) -> T:
    """Run ``work`` in a transaction, retrying the ones PostgreSQL aborted to break a deadlock.

    Deactivating or deleting a zone removes its presence rows while the engine may be updating
    some of them; PostgreSQL resolves such a lock cycle by aborting one side, and retrying the
    aborted side is the correct response.
    """
    for attempt in range(1, TRANSACTION_ATTEMPTS + 1):
        try:
            async with state.db.begin() as conn:
                return await work(conn)
        except DBAPIError as exc:
            sqlstate = _sqlstate(exc)
            if sqlstate not in RETRYABLE_SQLSTATES or attempt == TRANSACTION_ATTEMPTS:
                raise
            log.info("zones.transaction_retry", attempt=attempt, sqlstate=sqlstate)
            await asyncio.sleep(0.02 * attempt)
    raise AssertionError("unreachable")  # pragma: no cover


def _after(cursor: str | None) -> tuple[datetime, uuid.UUID] | None:
    if cursor is None:
        return None
    try:
        return decode_cursor(CURSOR_KIND, cursor)
    except CursorError as exc:
        raise ProblemError(400, "invalid_cursor", str(exc)) from exc


@router.get("", response_model=ZonePage, summary="Your zones, newest first")
async def list_zones(
    principal: CurrentUser,
    state: State,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(description="`next_cursor` of the previous page")] = None,
) -> ZonePage:
    after = _after(cursor)
    async with state.db.connect() as conn:
        records = await zones.page(conn, principal.user_id, limit=limit + 1, after=after)
    items = records[:limit]
    next_cursor = None
    if len(records) > limit:
        next_cursor = encode_cursor(CURSOR_KIND, items[-1].created_at, items[-1].id)
    return ZonePage(items=[zone_body(record) for record in items], next_cursor=next_cursor)


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=Zone,
    summary="Create a zone",
)
async def create_zone(
    body: ZoneCreate, principal: CurrentUser, state: State, response: Response
) -> Zone:
    spec = ZoneSpec(
        name=body.name,
        color=body.color,
        lat=body.center.lat,
        lon=body.center.lon,
        radius_m=body.radius_m,
        is_active=body.is_active,
        notify_enter=body.notify_enter,
        notify_exit=body.notify_exit,
        dwell_s=body.dwell_s,
    )

    async def work(conn: AsyncConnection) -> tuple[Zone, list[OutboxRow]]:
        try:
            record = await zones.create(conn, principal.user_id, spec)
        except IntegrityError as exc:
            if _sqlstate(exc) == FOREIGN_KEY_VIOLATION:  # the token outlived its account
                raise unauthorized("this account no longer exists") from exc
            raise
        zone = zone_body(record)
        event = _event(EventType.ZONE_CREATED, principal.user_id, zone.model_dump(mode="json"))
        return zone, await outbox.insert(conn, [event])

    zone, rows = await _in_transaction(state, work)
    await state.relay.relay(rows)
    response.headers["Location"] = f"/v1/geozones/{zone.id}"
    response.headers["ETag"] = etag(zone.version)
    return zone


@router.get("/{zone_id}", response_model=Zone, responses=_NOT_FOUND, summary="One zone")
async def get_zone(
    zone_id: ZoneId, principal: CurrentUser, state: State, response: Response
) -> Zone:
    async with state.db.connect() as conn:
        record = await zones.get(conn, principal.user_id, zone_id)
    if record is None:
        raise not_found("zone")
    response.headers["ETag"] = etag(record.version)
    return zone_body(record)


@router.patch(
    "/{zone_id}",
    response_model=Zone,
    responses={**_NOT_FOUND, **_PRECONDITION},
    summary="Change a zone (partial update)",
)
async def update_zone(
    *,
    zone_id: ZoneId,
    body: ZonePatch,
    principal: CurrentUser,
    state: State,
    response: Response,
    if_match: IfMatch = None,
) -> Zone:
    async def work(conn: AsyncConnection) -> tuple[Zone, list[OutboxRow]]:
        current = await zones.get(conn, principal.user_id, zone_id, for_update=True)
        if current is None:
            raise not_found("zone")
        _precondition(if_match, current.version)
        spec = merged(current.spec, body)
        if spec == current.spec:  # nothing changes: no new version, no event
            return zone_body(current), []
        if current.spec.is_active and not spec.is_active:
            await zones.clear_presence(conn, zone_id)
        zone = zone_body(await zones.replace(conn, zone_id, spec))
        event = _event(EventType.ZONE_UPDATED, principal.user_id, zone.model_dump(mode="json"))
        return zone, await outbox.insert(conn, [event])

    zone, rows = await _in_transaction(state, work)
    await state.relay.relay(rows)
    response.headers["ETag"] = etag(zone.version)
    return zone


@router.delete(
    "/{zone_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={**_NOT_FOUND, **_PRECONDITION},
    summary="Delete a zone (its alerts stay in the history)",
)
async def delete_zone(
    zone_id: ZoneId, principal: CurrentUser, state: State, if_match: IfMatch = None
) -> Response:
    async def work(conn: AsyncConnection) -> list[OutboxRow]:
        current = await zones.get(conn, principal.user_id, zone_id, for_update=True)
        if current is None:
            raise not_found("zone")
        _precondition(if_match, current.version)
        await zones.remove(conn, zone_id)
        event = _event(EventType.ZONE_DELETED, principal.user_id, {"id": str(zone_id)})
        return await outbox.insert(conn, [event])

    rows = await _in_transaction(state, work)
    await state.relay.relay(rows)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{zone_id}/occupants",
    response_model=Occupants,
    responses=_NOT_FOUND,
    summary="Devices inside the zone now, most recent arrivals first",
)
async def zone_occupants(
    zone_id: ZoneId,
    principal: CurrentUser,
    state: State,
    limit: Annotated[int, Query(ge=1, le=10_000)] = 1_000,
) -> Occupants:
    async with state.db.connect() as conn:
        record = await zones.get(conn, principal.user_id, zone_id)
        if record is None:
            raise not_found("zone")
        rows = await zones.occupants(conn, zone_id, limit=limit)
    return Occupants(
        zone_id=zone_id,
        occupancy=record.occupancy,
        items=[
            Occupant(
                device_id=row.device_id,
                position=LatLon(lat=row.lat, lon=row.lon),
                recorded_at=row.recorded_at,
                speed_mps=row.speed_mps,
                heading_deg=row.heading_deg,
                entered_at=row.entered_at,
                last_seen_at=row.last_seen_at,
            )
            for row in rows
        ],
    )
