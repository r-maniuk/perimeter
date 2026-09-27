import json
from uuid import UUID

from perimeter.domain.presence import Transition, TransitionKind, ZoneRules
from perimeter.wire import subjects
from perimeter.wire.events import (
    EventType,
    alert_data,
    decode_event,
    encode_event,
    live_frame,
    make_event,
)


def test_event_round_trip_and_live_frame() -> None:
    zone = ZoneRules(zone_id=UUID(int=1), owner_id=UUID(int=2), name="Dam Square")
    transition = Transition(TransitionKind.ENTER, "veh-1", zone, 1_790_000_000_000, 52.37, 4.89)
    alert_id = UUID(int=9)
    event = make_event(EventType.ALERT, alert_data(alert_id, transition), event_id=alert_id)
    payload = encode_event(event)
    assert decode_event(payload) == event
    frame = json.loads(live_frame(12, 7, payload))
    assert frame["type"] == "event"
    assert (frame["seq"], frame["prev"]) == (12, 7)
    assert frame["event"]["data"]["zone"]["name"] == "Dam Square"
    assert frame["event"]["data"]["occurred_at"].startswith("2026-09-")


def test_subjects() -> None:
    assert subjects.telemetry("veh-1") == "tlm.veh-1"
    assert subjects.telemetry_partition_transform(16) == "tlm.{{partition(16,1)}}.{{wildcard(1)}}"
    assert subjects.partitions_from_transform(subjects.telemetry_partition_transform(16)) == 16
    assert subjects.partitions_from_transform("tlm.>") is None
    assert subjects.partition_of("tlm.7.veh-1") == 7
    assert subjects.device_of("tlm.7.veh-1") == "veh-1"
    assert subjects.events(UUID(int=1)) == f"evt.{UUID(int=1)}"
    assert subjects.live_events("u") == "live.evt.u"
    assert subjects.engine_consumer(3) == "engine-p3"
