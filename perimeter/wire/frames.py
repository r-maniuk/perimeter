"""Binary position frames.

Live positions are the only high-volume stream a browser receives, so they travel as compact
binary frames instead of JSON: about 26 bytes per device instead of ~110, encoded once in the
engine and forwarded byte-for-byte by every API replica to every interested socket.

Tile frame layout (little-endian, 4-byte aligned arrays)::

    0   u8   magic 0xB7
    1   u8   version (1)
    2   u8   kind (1 = live update, 2 = snapshot)
    3   u8   zoom
    4   u32  tile x
    8   u32  tile y
    12  u64  base time, epoch milliseconds (smallest timestamp in the frame)
    20  u32  count N
    24  i32[N]  latitude  x 1e7
        i32[N]  longitude x 1e7
        u32[N]  recorded_at - base time, milliseconds
        u16[N]  speed, cm/s (0xFFFF = unknown)
        u16[N]  heading, centidegrees (0xFFFF = unknown)
        u32     length L of the id blob
        u8[L]   device ids, UTF-8, separated by 0x00
        (zero padding to a multiple of 4)

A WebSocket message carries a *bundle* of tile frames::

    0   u8   magic 0xB8
    1   u8   version (1)
    2   u16  count K
    then K times: u32 length, frame bytes (lengths are multiples of 4)
"""

from __future__ import annotations

import struct
import sys
from array import array
from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum

FRAME_MAGIC = 0xB7
BUNDLE_MAGIC = 0xB8
VERSION = 1
UNKNOWN_U16 = 0xFFFF
MAX_BUNDLE_FRAMES = 0xFFFF

_HEADER = struct.Struct("<BBBBIIQI")
_BUNDLE_HEADER = struct.Struct("<BBH")
_U32 = struct.Struct("<I")

if sys.byteorder != "little":  # pragma: no cover - every supported platform is little-endian
    msg = "frame encoding assumes a little-endian host"
    raise RuntimeError(msg)


class FrameKind(IntEnum):
    LIVE = 1
    SNAPSHOT = 2


class FrameError(ValueError):
    """Raised for bytes that are not a well-formed frame or bundle."""


@dataclass(frozen=True, slots=True)
class FramePoint:
    device_id: str
    lat: float
    lon: float
    recorded_at_ms: int
    speed_mps: float | None = None
    heading_deg: float | None = None


@dataclass(frozen=True, slots=True)
class TileFrame:
    kind: FrameKind
    zoom: int
    x: int
    y: int
    points: tuple[FramePoint, ...]


def _pad4(size: int) -> int:
    return (-size) & 3


def _speed_u16(value: float | None) -> int:
    if value is None:
        return UNKNOWN_U16
    return min(max(round(value * 100), 0), UNKNOWN_U16 - 1)


def _heading_u16(value: float | None) -> int:
    if value is None:
        return UNKNOWN_U16
    return round(value * 100) % 36_000


def encode_tile(kind: FrameKind, zoom: int, x: int, y: int, points: Sequence[FramePoint]) -> bytes:
    count = len(points)
    base = min((p.recorded_at_ms for p in points), default=0)
    ids = b"\x00".join(p.device_id.encode() for p in points)
    parts = [
        _HEADER.pack(FRAME_MAGIC, VERSION, int(kind), zoom, x, y, base, count),
        array("i", [round(p.lat * 1e7) for p in points]).tobytes(),
        array("i", [round(p.lon * 1e7) for p in points]).tobytes(),
        array("I", [p.recorded_at_ms - base for p in points]).tobytes(),
        array("H", [_speed_u16(p.speed_mps) for p in points]).tobytes(),
        array("H", [_heading_u16(p.heading_deg) for p in points]).tobytes(),
        _U32.pack(len(ids)),
        ids,
        b"\x00" * _pad4(len(ids)),
    ]
    return b"".join(parts)


def _read_array(typecode: str, view: memoryview, offset: int, count: int) -> array[int]:
    values: array[int] = array(typecode)
    values.frombytes(view[offset : offset + count * values.itemsize])
    return values


def decode_tile(data: bytes | memoryview) -> TileFrame:
    view = memoryview(data)
    if len(view) < _HEADER.size:
        msg = "frame shorter than its header"
        raise FrameError(msg)
    magic, version, kind, zoom, x, y, base, count = _HEADER.unpack_from(view, 0)
    if magic != FRAME_MAGIC or version != VERSION:
        msg = f"unsupported frame magic/version {magic:#x}/{version}"
        raise FrameError(msg)
    offset = _HEADER.size
    if len(view) < offset + 16 * count + 4:
        msg = "frame truncated inside its arrays"
        raise FrameError(msg)
    lats = _read_array("i", view, offset, count)
    offset += 4 * count
    lons = _read_array("i", view, offset, count)
    offset += 4 * count
    deltas = _read_array("I", view, offset, count)
    offset += 4 * count
    speeds = _read_array("H", view, offset, count)
    offset += 2 * count
    headings = _read_array("H", view, offset, count)
    offset += 2 * count
    (ids_len,) = _U32.unpack_from(view, offset)
    offset += 4
    if len(view) < offset + ids_len:
        msg = "frame truncated inside its id blob"
        raise FrameError(msg)
    ids = bytes(view[offset : offset + ids_len]).decode().split("\x00") if count else []
    if len(ids) != count:
        msg = f"frame declares {count} devices but carries {len(ids)} ids"
        raise FrameError(msg)
    points = tuple(
        FramePoint(
            device_id=ids[i],
            lat=lats[i] / 1e7,
            lon=lons[i] / 1e7,
            recorded_at_ms=base + deltas[i],
            speed_mps=None if speeds[i] == UNKNOWN_U16 else speeds[i] / 100,
            heading_deg=None if headings[i] == UNKNOWN_U16 else headings[i] / 100,
        )
        for i in range(count)
    )
    return TileFrame(FrameKind(kind), zoom, x, y, points)


def encode_bundle(frames: Sequence[bytes]) -> bytes:
    if len(frames) > MAX_BUNDLE_FRAMES:
        msg = f"a bundle holds at most {MAX_BUNDLE_FRAMES} frames"
        raise FrameError(msg)
    parts: list[bytes] = [_BUNDLE_HEADER.pack(BUNDLE_MAGIC, VERSION, len(frames))]
    for frame in frames:
        parts.append(_U32.pack(len(frame)))
        parts.append(frame)
    return b"".join(parts)


def decode_bundle(data: bytes | memoryview) -> list[memoryview]:
    view = memoryview(data)
    if len(view) < _BUNDLE_HEADER.size:
        msg = "bundle shorter than its header"
        raise FrameError(msg)
    magic, version, count = _BUNDLE_HEADER.unpack_from(view, 0)
    if magic != BUNDLE_MAGIC or version != VERSION:
        msg = f"unsupported bundle magic/version {magic:#x}/{version}"
        raise FrameError(msg)
    offset = _BUNDLE_HEADER.size
    frames = []
    for _ in range(count):
        if len(view) < offset + 4:
            msg = "bundle truncated before a frame length"
            raise FrameError(msg)
        (size,) = _U32.unpack_from(view, offset)
        offset += 4
        if len(view) < offset + size:
            msg = "bundle truncated inside a frame"
            raise FrameError(msg)
        frames.append(view[offset : offset + size])
        offset += size
    return frames
