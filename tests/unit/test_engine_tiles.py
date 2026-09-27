"""Live position frames and occupancy pulses: coalescing, framing, publishing."""

from __future__ import annotations

import asyncio
import json
from uuid import UUID

from perimeter.domain.reports import TelemetryRecord
from perimeter.domain.tiles import quadkey_for, subject_for, tile_of_quadkey
from perimeter.engine.tiles import (
    MAX_POINTS_PER_FRAME,
    PulseAccumulator,
    TileAccumulator,
    TilePublisher,
    pulse_frame,
    tile_frames,
)
from perimeter.wire.frames import FrameKind, FramePoint, decode_tile
from tests.support import eventually

T0 = 1_790_000_000_000
DAM = (52.3731, 4.8926)  # lat, lon
ZAANDAM = (52.4389, 4.8258)  # the neighbouring z12 tile to the north
Z1, Z2 = UUID(int=1), UUID(int=2)
ALICE, BOB = UUID(int=10), UUID(int=20)


def report(
    device: str, at: int, where: tuple[float, float] = DAM, **extra: float
) -> TelemetryRecord:
    return TelemetryRecord(device, at, at + 5, where[0], where[1], **extra)


def test_each_tile_keeps_only_the_newest_position_of_each_device() -> None:
    tiles = TileAccumulator(zoom=12)
    tiles.add([report("veh-1", T0 + 2_000), report("veh-2", T0)])
    tiles.add([report("veh-1", T0 + 1_000, speed=9.0), report("veh-1", T0 + 3_000, heading=90.0)])
    drained = tiles.drain()
    assert list(drained) == [quadkey_for(DAM[1], DAM[0], 12)]
    points = {p.device_id: p for p in next(iter(drained.values()))}
    assert points["veh-1"].recorded_at_ms == T0 + 3_000
    assert points["veh-1"].heading_deg == 90.0
    assert points["veh-2"].recorded_at_ms == T0
    assert tiles.drain() == {}


def test_devices_are_grouped_by_the_leaf_tile_they_are_in() -> None:
    tiles = TileAccumulator(zoom=12)
    tiles.add([report("veh-1", T0, DAM), report("veh-2", T0, ZAANDAM), report("veh-3", T0, DAM)])
    assert len(tiles) == 2
    drained = tiles.drain()
    assert all(len(key) == 12 for key in drained)
    by_tile = {key: sorted(p.device_id for p in points) for key, points in drained.items()}
    assert by_tile == {
        quadkey_for(DAM[1], DAM[0], 12): ["veh-1", "veh-3"],
        quadkey_for(ZAANDAM[1], ZAANDAM[0], 12): ["veh-2"],
    }


def test_a_tile_frame_decodes_back_to_its_tile_and_points() -> None:
    key = quadkey_for(DAM[1], DAM[0], 12)
    points = [
        FramePoint("veh-2", 52.37, 4.89, T0 + 500, speed_mps=12.5, heading_deg=180.0),
        FramePoint("veh-1", 52.371, 4.891, T0),
    ]
    (frame,) = tile_frames(key, points)
    decoded = decode_tile(frame)
    tile = tile_of_quadkey(key)
    assert (decoded.kind, decoded.zoom, decoded.x, decoded.y) == (
        FrameKind.LIVE,
        12,
        tile.x,
        tile.y,
    )
    assert [p.device_id for p in decoded.points] == ["veh-1", "veh-2"]  # oldest first
    assert decoded.points[1].speed_mps == 12.5
    assert decoded.points[1].recorded_at_ms == T0 + 500


def test_a_frame_never_spans_more_time_than_its_offsets_can_hold() -> None:
    key = quadkey_for(DAM[1], DAM[0], 12)
    weeks_ago = T0 - 60 * 86_400_000  # a device uploading its backlog next to live ones
    frames = tile_frames(
        key, [FramePoint("live", 52.37, 4.89, T0), FramePoint("old", 52.37, 4.89, weeks_ago)]
    )
    assert [[p.device_id for p in decode_tile(f).points] for f in frames] == [["old"], ["live"]]


def test_crowded_tiles_are_split_into_bounded_frames() -> None:
    key = quadkey_for(DAM[1], DAM[0], 12)
    crowd = [FramePoint(f"d{i}", 52.37, 4.89, T0 + i) for i in range(MAX_POINTS_PER_FRAME + 1)]
    frames = tile_frames(key, crowd)
    assert [len(decode_tile(f).points) for f in frames] == [MAX_POINTS_PER_FRAME, 1]


def test_a_pulse_is_the_exact_websocket_frame() -> None:
    frame = pulse_frame({Z2: {"veh-9"}, Z1: ["veh-2", "veh-1"]}, window_ms=100)
    assert frame == (
        b'{"type":"pulse","window_ms":100,"zones":{'
        b'"00000000-0000-0000-0000-000000000001":["veh-1","veh-2"],'
        b'"00000000-0000-0000-0000-000000000002":["veh-9"]}}'
    )


def test_pulses_are_coalesced_per_user_and_zone() -> None:
    pulses = PulseAccumulator()
    pulses.add({ALICE: {Z1: ["veh-1"]}})
    pulses.add({ALICE: {Z1: ["veh-2", "veh-1"], Z2: ["veh-3"]}, BOB: {Z2: ["veh-3"]}})
    assert len(pulses) == 2
    assert pulses.drain() == {
        ALICE: {Z1: {"veh-1", "veh-2"}, Z2: {"veh-3"}},
        BOB: {Z2: {"veh-3"}},
    }
    assert pulses.drain() == {}


class Broker:
    def __init__(self, *, connected: bool = True) -> None:
        self.is_connected = connected
        self.published: list[tuple[str, bytes]] = []

    async def publish(self, subject: str, payload: bytes = b"") -> None:
        self.published.append((subject, payload))


def publisher(broker: Broker, flush_ms: int = 100) -> TilePublisher:
    return TilePublisher(broker, zoom=12, flush_ms=flush_ms)  # type: ignore[arg-type]


async def test_a_flush_publishes_one_frame_per_dirty_tile_and_one_pulse_per_user() -> None:
    broker = Broker()
    live = publisher(broker)
    live.positions(
        [report("veh-1", T0, DAM), report("veh-2", T0, ZAANDAM), report("veh-1", T0 + 1, DAM)]
    )
    live.pulses({ALICE: {Z1: ["veh-1"]}})
    live.pulses({ALICE: {Z1: ["veh-3"]}})
    await live.flush()
    subjects = sorted(subject for subject, _ in broker.published)
    assert subjects == sorted(
        [
            subject_for(quadkey_for(DAM[1], DAM[0], 12)),
            subject_for(quadkey_for(ZAANDAM[1], ZAANDAM[0], 12)),
            f"live.occ.{ALICE}",
        ]
    )
    pulse = json.loads(dict(broker.published)[f"live.occ.{ALICE}"])
    assert pulse == {"type": "pulse", "window_ms": 100, "zones": {str(Z1): ["veh-1", "veh-3"]}}
    broker.published.clear()
    await live.flush()
    assert broker.published == []  # nothing new, nothing sent


async def test_frames_are_dropped_rather_than_queued_while_disconnected() -> None:
    broker = Broker(connected=False)
    live = publisher(broker)
    live.positions([report("veh-1", T0)])
    await live.flush()
    broker.is_connected = True
    await live.flush()
    assert broker.published == []  # the stale frame was not kept for later


async def test_the_publisher_flushes_on_a_cadence_and_once_more_when_stopped() -> None:
    broker = Broker()
    live = publisher(broker, flush_ms=20)
    stop = asyncio.Event()
    task = asyncio.create_task(live.run(stop))
    live.positions([report("veh-1", T0)])
    await eventually(lambda: len(broker.published) == 1, within=1.0, interval=0.005)
    live.positions([report("veh-1", T0 + 1)])
    stop.set()
    await asyncio.wait_for(task, 1.0)
    assert len(broker.published) == 2
