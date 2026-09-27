"""Geofence presence: which device is inside which zone, and the alerts that changes produce.

The engine asks PostGIS which zones contain each report (spatial work stays in the database) and
hands the answer to this module, which is pure: no I/O, no clock, fully deterministic. That keeps
the part with the subtle rules small and exhaustively testable.

Rules, per device, over reports in event-time order:

* A report not newer than the last committed one is *late*. It is kept in the device's track (a
  device that uploads buffered history still draws its trail) but never changes presence or
  raises an alert: out-of-order and redelivered messages are therefore harmless.
* Zones the device was in but the report is not in produce ``exit``; zones it is in but was not
  produce ``enter``; zones it stays in refresh ``last_seen`` and may produce a single ``dwell``
  once the stay lasts ``dwell_s`` seconds.
* Several reports of one device inside one batch are applied one after another, so a device that
  crosses a zone within a single batch still yields both ``enter`` and ``exit``.
* Presence is tracked for every active zone; whether a transition becomes an *alert* depends on
  the zone's notification settings. Presence rows of zones that were deleted or deactivated are
  dropped silently.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from uuid import UUID

from perimeter.domain.reports import TelemetryRecord


class TransitionKind(StrEnum):
    ENTER = "enter"
    EXIT = "exit"
    DWELL = "dwell"


@dataclass(frozen=True, slots=True)
class ZoneRules:
    zone_id: UUID
    owner_id: UUID
    name: str
    is_active: bool = True
    notify_enter: bool = True
    notify_exit: bool = True
    dwell_s: int | None = None


@dataclass(frozen=True, slots=True)
class Stay:
    """A device being inside a zone since ``entered_at_ms``."""

    zone_id: UUID
    entered_at_ms: int
    last_seen_at_ms: int
    dwell_alerted: bool = False


@dataclass(frozen=True, slots=True)
class Observation:
    """A report together with the ids of the zones PostGIS found it inside."""

    record: TelemetryRecord
    zone_ids: frozenset[UUID]


@dataclass(frozen=True, slots=True)
class Transition:
    kind: TransitionKind
    device_id: str
    zone: ZoneRules
    occurred_at_ms: int
    lat: float
    lon: float


@dataclass(slots=True)
class DeviceOutcome:
    device_id: str
    accepted: list[TelemetryRecord] = field(default_factory=list)
    late: list[TelemetryRecord] = field(default_factory=list)
    alerts: list[Transition] = field(default_factory=list)
    upserts: dict[UUID, Stay] = field(default_factory=dict)
    deletes: set[UUID] = field(default_factory=set)
    inside: set[UUID] = field(default_factory=set)

    @property
    def latest(self) -> TelemetryRecord | None:
        return self.accepted[-1] if self.accepted else None


@dataclass(slots=True)
class BatchOutcome:
    devices: dict[str, DeviceOutcome] = field(default_factory=dict)

    @property
    def late(self) -> int:
        return sum(len(outcome.late) for outcome in self.devices.values())

    @property
    def accepted(self) -> int:
        return sum(len(outcome.accepted) for outcome in self.devices.values())

    def latest(self) -> list[TelemetryRecord]:
        return [o.latest for o in self.devices.values() if o.latest is not None]

    def tracked(self) -> list[TelemetryRecord]:
        """The reports for the device tracks: applied and late ones (a replay is stored once)."""
        return [r for o in self.devices.values() for r in (*o.accepted, *o.late)]

    def alerts(self) -> list[Transition]:
        return [alert for o in self.devices.values() for alert in o.alerts]

    def upserts(self) -> list[tuple[str, Stay]]:
        return [(device, stay) for device, o in self.devices.items() for stay in o.upserts.values()]

    def deletes(self) -> list[tuple[str, UUID]]:
        return [(device, zone) for device, o in self.devices.items() for zone in sorted(o.deletes)]

    def pulses(self, zones: Mapping[UUID, ZoneRules]) -> dict[UUID, dict[UUID, list[str]]]:
        """Owner -> zone -> devices that reported from inside the zone in this batch."""
        by_owner: dict[UUID, dict[UUID, set[str]]] = {}
        for device, outcome in self.devices.items():
            for zone_id in outcome.inside:
                rules = zones.get(zone_id)
                if rules is not None:
                    by_owner.setdefault(rules.owner_id, {}).setdefault(zone_id, set()).add(device)
        return {
            owner: {zone: sorted(devices) for zone, devices in per_zone.items()}
            for owner, per_zone in by_owner.items()
        }


def evaluate_device(
    device_id: str,
    *,
    last_recorded_at_ms: int | None,
    observations: Iterable[Observation],
    stays: Mapping[UUID, Stay],
    zones: Mapping[UUID, ZoneRules],
) -> DeviceOutcome:
    outcome = DeviceOutcome(device_id)
    current: dict[UUID, Stay] = {}
    for zone_id, stay in stays.items():
        rules = zones.get(zone_id)
        if rules is None or not rules.is_active:
            outcome.deletes.add(zone_id)
        else:
            current[zone_id] = stay

    last = last_recorded_at_ms
    ordered = sorted(observations, key=lambda obs: obs.record.recorded_at_ms)
    for obs in ordered:
        record = obs.record
        at = record.recorded_at_ms
        if last is not None and at <= last:
            outcome.late.append(record)
            continue
        last = at
        outcome.accepted.append(record)
        hits = sorted(
            zone_id
            for zone_id in obs.zone_ids
            if (rules := zones.get(zone_id)) is not None and rules.is_active
        )
        hit_set = set(hits)

        for zone_id in sorted(current.keys() - hit_set):
            del current[zone_id]
            outcome.upserts.pop(zone_id, None)
            outcome.deletes.add(zone_id)
            rules = zones[zone_id]
            if rules.notify_exit:
                outcome.alerts.append(_transition(TransitionKind.EXIT, device_id, rules, record))

        for zone_id in hits:
            rules = zones[zone_id]
            outcome.inside.add(zone_id)
            existing = current.get(zone_id)
            if existing is None:
                updated = Stay(zone_id, entered_at_ms=at, last_seen_at_ms=at)
                outcome.deletes.discard(zone_id)
                if rules.notify_enter:
                    outcome.alerts.append(
                        _transition(TransitionKind.ENTER, device_id, rules, record)
                    )
            else:
                alerted = existing.dwell_alerted
                if (
                    rules.dwell_s is not None
                    and not alerted
                    and at - existing.entered_at_ms >= rules.dwell_s * 1000
                ):
                    alerted = True
                    outcome.alerts.append(
                        _transition(TransitionKind.DWELL, device_id, rules, record)
                    )
                updated = replace(existing, last_seen_at_ms=at, dwell_alerted=alerted)
            current[zone_id] = updated
            outcome.upserts[zone_id] = updated
    return outcome


def evaluate_batch(
    observations: Mapping[str, Sequence[Observation]],
    *,
    last_recorded_at_ms: Mapping[str, int],
    stays: Mapping[str, Mapping[UUID, Stay]],
    zones: Mapping[UUID, ZoneRules],
) -> BatchOutcome:
    batch = BatchOutcome()
    for device_id in sorted(observations):
        batch.devices[device_id] = evaluate_device(
            device_id,
            last_recorded_at_ms=last_recorded_at_ms.get(device_id),
            observations=observations[device_id],
            stays=stays.get(device_id, {}),
            zones=zones,
        )
    return batch


def _transition(
    kind: TransitionKind, device_id: str, rules: ZoneRules, record: TelemetryRecord
) -> Transition:
    return Transition(
        kind=kind,
        device_id=device_id,
        zone=rules,
        occurred_at_ms=record.recorded_at_ms,
        lat=record.lat,
        lon=record.lon,
    )
