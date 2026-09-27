"""Live position frames and occupancy pulses.

Positions are state, not events: a client that misses a frame catches up with the next one, or
with the PostGIS snapshot it receives on (re)subscribe. So they travel over core NATS (at most
once, never stored) and are coalesced: per leaf tile and device, only the newest position of the
flush interval is sent. Each dirty tile becomes one binary frame (:mod:`perimeter.wire.frames`),
encoded once here and forwarded byte-for-byte by every api replica, on the subject spelled by the
tile's quadkey, so the broker routes it only to replicas whose clients look at that area.

Occupancy pulses ("these devices reported from inside these zones") are coalesced the same way:
at most one message per user per flush, already in the exact WebSocket frame the browser gets.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from contextlib import suppress
from uuid import UUID

import msgspec
import structlog
from nats.aio.client import Client as NatsClient

from perimeter.domain import tiles
from perimeter.domain.reports import TelemetryRecord
from perimeter.engine.metrics import FRAME_BYTES, FRAMES, PUBLISH_FAILURES, PULSES
from perimeter.wire import subjects
from perimeter.wire.frames import FrameKind, FramePoint, encode_tile

log = structlog.get_logger(__name__)

MAX_POINTS_PER_FRAME = 4_096  # about 150 KiB, far below the broker's payload limit
MAX_FRAME_SPAN_MS = 0xFFFF_FFFF  # point times are u32 offsets from the frame's base time


class PulseFrame(msgspec.Struct, frozen=True, tag_field="type", tag="pulse"):
    """The WebSocket text frame of one pulse; the api forwards it untouched."""

    window_ms: int
    zones: dict[str, list[str]]


_pulse_encoder = msgspec.json.Encoder()


def pulse_frame(zones: Mapping[UUID, Iterable[str]], *, window_ms: int) -> bytes:
    """``{"type":"pulse","window_ms":..,"zones":{"<zone_id>":["<device_id>", ...]}}``."""
    ordered = {str(zone_id): sorted(devices) for zone_id, devices in sorted(zones.items())}
    return _pulse_encoder.encode(PulseFrame(window_ms=window_ms, zones=ordered))


def tile_frames(quadkey: str, points: Iterable[FramePoint]) -> list[bytes]:
    """Frames for one leaf tile: one, unless it would be too large or span too much time.

    A frame stores each point's time as a 32-bit offset from the oldest point, so a device
    replaying reports weeks old next to live ones gets a frame of its own instead of overflowing.
    """
    tile = tiles.tile_of_quadkey(quadkey)
    frames: list[bytes] = []
    chunk: list[FramePoint] = []
    for point in sorted(points, key=lambda p: p.recorded_at_ms):
        if chunk and (
            len(chunk) >= MAX_POINTS_PER_FRAME
            or point.recorded_at_ms - chunk[0].recorded_at_ms > MAX_FRAME_SPAN_MS
        ):
            frames.append(encode_tile(FrameKind.LIVE, tile.z, tile.x, tile.y, chunk))
            chunk = []
        chunk.append(point)
    if chunk:
        frames.append(encode_tile(FrameKind.LIVE, tile.z, tile.x, tile.y, chunk))
    return frames


class TileAccumulator:
    """The newest position of every device that moved, grouped by the leaf tile it is in."""

    def __init__(self, zoom: int) -> None:
        self._zoom = zoom
        self._tiles: dict[str, dict[str, FramePoint]] = {}

    def __len__(self) -> int:
        return len(self._tiles)

    def add(self, records: Iterable[TelemetryRecord]) -> None:
        for record in records:
            key = tiles.quadkey_for(record.lon, record.lat, self._zoom)
            points = self._tiles.setdefault(key, {})
            current = points.get(record.device_id)
            if current is None or current.recorded_at_ms < record.recorded_at_ms:
                points[record.device_id] = FramePoint(
                    device_id=record.device_id,
                    lat=record.lat,
                    lon=record.lon,
                    recorded_at_ms=record.recorded_at_ms,
                    speed_mps=record.speed,
                    heading_deg=record.heading,
                )

    def drain(self) -> dict[str, list[FramePoint]]:
        """Everything accumulated since the last drain, by leaf quadkey."""
        drained = {key: list(points.values()) for key, points in self._tiles.items()}
        self._tiles = {}
        return drained


class PulseAccumulator:
    """Owner -> zone -> devices seen inside it since the last drain."""

    def __init__(self) -> None:
        self._owners: dict[UUID, dict[UUID, set[str]]] = {}

    def __len__(self) -> int:
        return len(self._owners)

    def add(self, pulses: Mapping[UUID, Mapping[UUID, Iterable[str]]]) -> None:
        for owner_id, zones in pulses.items():
            per_zone = self._owners.setdefault(owner_id, {})
            for zone_id, devices in zones.items():
                per_zone.setdefault(zone_id, set()).update(devices)

    def drain(self) -> dict[UUID, dict[UUID, set[str]]]:
        drained, self._owners = self._owners, {}
        return drained


class TilePublisher:
    """Collects what the partition workers hand over and publishes it once per flush interval."""

    def __init__(self, nc: NatsClient, *, zoom: int, flush_ms: int) -> None:
        self._nc = nc
        self._flush_ms = flush_ms
        self._positions = TileAccumulator(zoom)
        self._pulses = PulseAccumulator()

    def positions(self, records: Iterable[TelemetryRecord]) -> None:
        self._positions.add(records)

    def pulses(self, pulses: Mapping[UUID, Mapping[UUID, Iterable[str]]]) -> None:
        self._pulses.add(pulses)

    async def flush(self) -> None:
        """Publish every dirty tile and every user's pulse; never raises."""
        failures = 0
        for quadkey, points in self._positions.drain().items():
            try:
                frames = tile_frames(quadkey, points)
            except Exception:
                log.exception("engine.frame_encoding_failed", quadkey=quadkey, points=len(points))
                failures += 1
                continue
            subject = tiles.subject_for(quadkey)
            for frame in frames:
                if await self._publish(subject, frame, "frame"):
                    FRAMES.inc()
                    FRAME_BYTES.inc(len(frame))
                else:
                    failures += 1
        for owner_id, zones in self._pulses.drain().items():
            frame = pulse_frame(zones, window_ms=self._flush_ms)
            if await self._publish(subjects.live_pulses(owner_id), frame, "pulse"):
                PULSES.inc()
            else:
                failures += 1
        if failures:
            log.warning("engine.live_publish_failed", failed=failures)

    async def _publish(self, subject: str, payload: bytes, kind: str) -> bool:
        # While the connection is down the client would buffer frames and deliver them late;
        # positions are state, so a stale frame is worth nothing: drop it instead.
        if not self._nc.is_connected:
            PUBLISH_FAILURES.labels(kind).inc()
            return False
        try:
            await self._nc.publish(subject, payload)
        except Exception:
            PUBLISH_FAILURES.labels(kind).inc()
            return False
        return True

    async def run(self, stop: asyncio.Event) -> None:
        """Flush on a steady cadence until ``stop``, then once more for what is left."""
        loop = asyncio.get_running_loop()
        interval = self._flush_ms / 1000
        next_at = loop.time() + interval
        while True:
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), max(0.0, next_at - loop.time()))
            await self.flush()
            if stop.is_set():
                return
            next_at += interval
            if next_at < loop.time():  # fell behind (a stalled loop): skip, do not burst
                next_at = loop.time() + interval
