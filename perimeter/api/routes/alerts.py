"""Alert history of the signed-in user, newest first, filterable, keyset-paginated."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query

from perimeter.api.deps import CurrentUser, State
from perimeter.api.errors import ProblemError
from perimeter.api.pagination import CursorError, decode_cursor, encode_cursor
from perimeter.api.schemas import Alert, AlertPage, AlertZone, LatLon
from perimeter.domain.presence import TransitionKind
from perimeter.domain.reports import DEVICE_ID_PATTERN
from perimeter.storage import alerts
from perimeter.storage.alerts import AlertFilter, AlertRecord

router = APIRouter(prefix="/alerts", tags=["alerts"])

CURSOR_KIND = "alerts"


def alert_body(record: AlertRecord) -> Alert:
    return Alert(
        id=record.id,
        kind=TransitionKind(record.kind),
        device_id=record.device_id,
        zone=AlertZone(id=record.zone_id, name=record.zone_name),
        position=LatLon(lat=record.lat, lon=record.lon),
        occurred_at=record.occurred_at,
        created_at=record.created_at,
    )


@router.get("", response_model=AlertPage, summary="Alerts of your zones, newest first")
async def list_alerts(
    *,
    principal: CurrentUser,
    state: State,
    zone_id: Annotated[uuid.UUID | None, Query(description="Only this zone")] = None,
    kind: Annotated[
        list[TransitionKind] | None, Query(description="Only these kinds (repeatable)")
    ] = None,
    device_id: Annotated[
        str | None, Query(max_length=64, pattern=DEVICE_ID_PATTERN, description="Only this device")
    ] = None,
    since: Annotated[
        datetime | None, Query(description="Only alerts that occurred at or after this time")
    ] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    cursor: Annotated[str | None, Query(description="`next_cursor` of the previous page")] = None,
) -> AlertPage:
    after = None
    if cursor is not None:
        try:
            after = decode_cursor(CURSOR_KIND, cursor)
        except CursorError as exc:
            raise ProblemError(400, "invalid_cursor", str(exc)) from exc
    if since is not None and since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    filters = AlertFilter(
        zone_id=zone_id,
        kinds=sorted({k.value for k in kind}) if kind else (),
        device_id=device_id,
        since=since,
    )
    async with state.db.connect() as conn:
        records = await alerts.page(conn, principal.user_id, filters, limit=limit + 1, after=after)
    items = records[:limit]
    next_cursor = None
    if len(records) > limit:
        next_cursor = encode_cursor(CURSOR_KIND, items[-1].occurred_at, items[-1].id)
    return AlertPage(items=[alert_body(record) for record in items], next_cursor=next_cursor)
