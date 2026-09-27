from collections.abc import Mapping, Sequence
from itertools import pairwise
from uuid import UUID

from hypothesis import given
from hypothesis import strategies as st

from perimeter.domain.presence import (
    DeviceOutcome,
    Observation,
    Stay,
    TransitionKind,
    ZoneRules,
    evaluate_batch,
    evaluate_device,
)
from perimeter.domain.reports import TelemetryRecord

OWNER = UUID(int=100)
OTHER_OWNER = UUID(int=200)
Z1, Z2, Z3 = UUID(int=1), UUID(int=2), UUID(int=3)
ENTER, EXIT, DWELL = TransitionKind.ENTER, TransitionKind.EXIT, TransitionKind.DWELL


def zone(zone_id: UUID, owner: UUID = OWNER, **kwargs: object) -> ZoneRules:
    return ZoneRules(zone_id=zone_id, owner_id=owner, name=f"zone-{zone_id.int}", **kwargs)  # type: ignore[arg-type]


ZONES = {z: zone(z) for z in (Z1, Z2, Z3)}


def obs(at: int, *zones: UUID, device: str = "dev-1") -> Observation:
    record = TelemetryRecord(device, at, at, lat=at / 1e6, lon=-at / 1e6)
    return Observation(record, frozenset(zones))


def run(
    observations: Sequence[Observation],
    *,
    stays: Mapping[UUID, Stay] | None = None,
    last: int | None = None,
    zones: Mapping[UUID, ZoneRules] = ZONES,
) -> DeviceOutcome:
    return evaluate_device(
        "dev-1",
        last_recorded_at_ms=last,
        observations=observations,
        stays=stays or {},
        zones=zones,
    )


def kinds(outcome: DeviceOutcome) -> list[tuple[TransitionKind, UUID]]:
    return [(a.kind, a.zone.zone_id) for a in outcome.alerts]


def test_first_report_inside_a_zone_enters_it() -> None:
    outcome = run([obs(1_000, Z1)])
    assert kinds(outcome) == [(ENTER, Z1)]
    assert outcome.upserts[Z1] == Stay(Z1, entered_at_ms=1_000, last_seen_at_ms=1_000)
    assert outcome.inside == {Z1}
    assert outcome.alerts[0].occurred_at_ms == 1_000


def test_staying_inside_only_refreshes_last_seen() -> None:
    stay = Stay(Z1, entered_at_ms=1_000, last_seen_at_ms=1_000)
    outcome = run([obs(2_000, Z1)], stays={Z1: stay}, last=1_000)
    assert outcome.alerts == []
    assert outcome.upserts[Z1].last_seen_at_ms == 2_000
    assert outcome.upserts[Z1].entered_at_ms == 1_000


def test_leaving_a_zone_exits_it() -> None:
    stay = Stay(Z1, entered_at_ms=1_000, last_seen_at_ms=1_000)
    outcome = run([obs(2_000)], stays={Z1: stay}, last=1_000)
    assert kinds(outcome) == [(EXIT, Z1)]
    assert outcome.deletes == {Z1}
    assert outcome.upserts == {}


def test_crossing_a_zone_within_one_batch_yields_enter_and_exit() -> None:
    outcome = run([obs(1_000), obs(2_000, Z1), obs(3_000)])
    assert kinds(outcome) == [(ENTER, Z1), (EXIT, Z1)]
    assert outcome.deletes == {Z1}
    assert outcome.upserts == {}
    assert [r.recorded_at_ms for r in outcome.accepted] == [1_000, 2_000, 3_000]


def test_reports_are_applied_in_event_time_order_whatever_the_arrival_order() -> None:
    outcome = run([obs(3_000), obs(1_000), obs(2_000, Z1)])
    assert kinds(outcome) == [(ENTER, Z1), (EXIT, Z1)]


def test_late_and_duplicate_reports_change_nothing() -> None:
    stay = Stay(Z1, entered_at_ms=1_000, last_seen_at_ms=5_000)
    outcome = run(
        [obs(4_000), obs(5_000), obs(6_000, Z1), obs(6_000)], stays={Z1: stay}, last=5_000
    )
    assert len(outcome.late) == 3
    assert outcome.alerts == []
    assert outcome.latest is not None
    assert outcome.latest.recorded_at_ms == 6_000


def test_dwell_fires_once_when_the_stay_is_long_enough() -> None:
    zones = {Z1: zone(Z1, dwell_s=60)}
    stay = Stay(Z1, entered_at_ms=0, last_seen_at_ms=30_000)
    outcome = run(
        [obs(59_999, Z1), obs(60_000, Z1), obs(90_000, Z1)],
        stays={Z1: stay},
        last=30_000,
        zones=zones,
    )
    assert kinds(outcome) == [(DWELL, Z1)]
    assert outcome.alerts[0].occurred_at_ms == 60_000
    assert outcome.upserts[Z1].dwell_alerted is True


def test_dwell_can_fire_for_a_stay_that_started_in_the_same_batch() -> None:
    zones = {Z1: zone(Z1, dwell_s=10)}
    outcome = run([obs(0, Z1), obs(5_000, Z1), obs(10_000, Z1)], zones=zones)
    assert kinds(outcome) == [(ENTER, Z1), (DWELL, Z1)]


def test_a_new_stay_resets_the_dwell_flag() -> None:
    zones = {Z1: zone(Z1, dwell_s=10)}
    stay = Stay(Z1, entered_at_ms=0, last_seen_at_ms=20_000, dwell_alerted=True)
    outcome = run(
        [obs(21_000), obs(22_000, Z1), obs(40_000, Z1)], stays={Z1: stay}, last=20_000, zones=zones
    )
    assert kinds(outcome) == [(EXIT, Z1), (ENTER, Z1), (DWELL, Z1)]


def test_notification_flags_silence_alerts_but_presence_is_still_tracked() -> None:
    zones = {Z1: zone(Z1, notify_enter=False, notify_exit=False)}
    entered = run([obs(1_000, Z1)], zones=zones)
    assert entered.alerts == []
    assert Z1 in entered.upserts
    left = run([obs(2_000)], stays={Z1: entered.upserts[Z1]}, last=1_000, zones=zones)
    assert left.alerts == []
    assert left.deletes == {Z1}


def test_presence_in_deleted_or_deactivated_zones_is_dropped_silently() -> None:
    zones = {Z2: zone(Z2, is_active=False)}
    stays = {
        Z1: Stay(Z1, entered_at_ms=0, last_seen_at_ms=0),
        Z2: Stay(Z2, entered_at_ms=0, last_seen_at_ms=0),
    }
    outcome = run([obs(1_000)], stays=stays, last=0, zones=zones)
    assert outcome.alerts == []
    assert outcome.deletes == {Z1, Z2}


def test_moving_between_overlapping_zones() -> None:
    outcome = run([obs(1_000, Z1, Z2), obs(2_000, Z2), obs(3_000, Z2, Z3)])
    assert kinds(outcome) == [(ENTER, Z1), (ENTER, Z2), (EXIT, Z1), (ENTER, Z3)]
    assert set(outcome.upserts) == {Z2, Z3}
    assert outcome.deletes == {Z1}


def test_batch_groups_pulses_by_zone_owner() -> None:
    zones = {Z1: zone(Z1), Z2: zone(Z2, owner=OTHER_OWNER)}
    batch = evaluate_batch(
        {"a": [obs(1_000, Z1, device="a")], "b": [obs(1_000, Z1, Z2, device="b")]},
        last_recorded_at_ms={},
        stays={},
        zones=zones,
    )
    assert batch.pulses(zones) == {OWNER: {Z1: ["a", "b"]}, OTHER_OWNER: {Z2: ["b"]}}
    assert batch.accepted == 2
    assert len(batch.alerts()) == 3
    assert {d for d, _ in batch.upserts()} == {"a", "b"}


# --- properties -----------------------------------------------------------------------------

zone_sets = st.frozensets(st.sampled_from([Z1, Z2, Z3]))
tracks = st.lists(zone_sets, min_size=1, max_size=40)


def apply_in_chunks(
    track: list[frozenset[UUID]], cuts: list[int], zones: Mapping[UUID, ZoneRules] = ZONES
) -> tuple[list[tuple[TransitionKind, UUID, int]], dict[UUID, Stay], int | None]:
    observations = [obs((i + 1) * 1_000, *hits) for i, hits in enumerate(track)]
    bounds = sorted({0, len(observations), *(c % (len(observations) + 1) for c in cuts)})
    stays: dict[UUID, Stay] = {}
    last: int | None = None
    alerts = []
    for start, end in pairwise(bounds):
        outcome = run(observations[start:end], stays=stays, last=last, zones=zones)
        alerts += [(a.kind, a.zone.zone_id, a.occurred_at_ms) for a in outcome.alerts]
        for zone_id in outcome.deletes:
            stays.pop(zone_id, None)
        stays.update(outcome.upserts)
        if outcome.latest is not None:
            last = outcome.latest.recorded_at_ms
    return alerts, stays, last


@given(track=tracks, cuts=st.lists(st.integers(0, 40), max_size=6))
def test_batching_never_changes_the_outcome(track: list[frozenset[UUID]], cuts: list[int]) -> None:
    assert apply_in_chunks(track, cuts) == apply_in_chunks(track, [])


@given(track=tracks)
def test_alerts_alternate_enter_exit_per_zone(track: list[frozenset[UUID]]) -> None:
    alerts, stays, _ = apply_in_chunks(track, [])
    for zone_id in (Z1, Z2, Z3):
        sequence = [kind for kind, z, _ in alerts if z == zone_id]
        assert sequence == [ENTER, EXIT] * (len(sequence) // 2) + [ENTER] * (len(sequence) % 2)
        assert (zone_id in stays) == (len(sequence) % 2 == 1)


@given(track=tracks, replay=st.lists(st.integers(0, 39), max_size=10))
def test_replaying_already_applied_reports_is_a_no_op(
    track: list[frozenset[UUID]], replay: list[int]
) -> None:
    _, stays, last = apply_in_chunks(track, [])
    again = [obs((i % len(track) + 1) * 1_000, *track[i % len(track)]) for i in replay]
    outcome = run(again, stays=stays, last=last)
    assert outcome.alerts == []
    assert outcome.upserts == {}
    assert len(outcome.late) == len(again)


@given(track=tracks)
def test_dwell_fires_at_most_once_per_stay(track: list[frozenset[UUID]]) -> None:
    zones = {z: zone(z, dwell_s=10) for z in (Z1, Z2, Z3)}
    alerts, _, _ = apply_in_chunks(track, [], zones=zones)
    for zone_id in (Z1, Z2, Z3):
        dwells_in_stay = 0
        for kind, z, _ in alerts:
            if z != zone_id:
                continue
            if kind == ENTER:
                dwells_in_stay = 0
            elif kind == DWELL:
                dwells_in_stay += 1
                assert dwells_in_stay == 1
