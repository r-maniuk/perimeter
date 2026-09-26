"""Web-Mercator tiles and quadkeys, used to address live position channels.

Every position update is published on a NATS subject built from the quadkey of the tile that
contains it, one digit per token: ``pos.1.2.0.3...``. Because each quadkey digit refines its
parent, a viewer interested in a large area subscribes to a short prefix with a wildcard
(``pos.1.2.>``) and receives every leaf tile below it; a viewer zoomed into a street subscribes to
a long prefix and receives only that neighbourhood. The broker does the filtering.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from typing import NamedTuple

MAX_MERCATOR_LAT = 85.05112877980659
SUBJECT_ROOT = "pos"


class Tile(NamedTuple):
    x: int
    y: int
    z: int


class BBox(NamedTuple):
    west: float
    south: float
    east: float
    north: float


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(value, high))


def tile_for(lon: float, lat: float, zoom: int) -> Tile:
    n = 1 << zoom
    lat_r = math.radians(_clamp(lat, -MAX_MERCATOR_LAT, MAX_MERCATOR_LAT))
    x = int((_clamp(lon, -180.0, 180.0) + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
    return Tile(min(max(x, 0), n - 1), min(max(y, 0), n - 1), zoom)


def quadkey(tile: Tile) -> str:
    digits = []
    for level in range(tile.z, 0, -1):
        mask = 1 << (level - 1)
        digit = (1 if tile.x & mask else 0) + (2 if tile.y & mask else 0)
        digits.append(str(digit))
    return "".join(digits)


def tile_of_quadkey(key: str) -> Tile:
    x = y = 0
    for digit in key:
        if digit not in "0123":
            msg = f"invalid quadkey digit {digit!r} in {key!r}"
            raise ValueError(msg)
        value = int(digit)
        x = (x << 1) | (value & 1)
        y = (y << 1) | (value >> 1)
    return Tile(x, y, len(key))


def quadkey_for(lon: float, lat: float, zoom: int) -> str:
    return quadkey(tile_for(lon, lat, zoom))


def tile_bounds(tile: Tile) -> BBox:
    n = 1 << tile.z

    def lat_of(y: int) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))

    return BBox(
        west=tile.x / n * 360.0 - 180.0,
        south=lat_of(tile.y + 1),
        east=(tile.x + 1) / n * 360.0 - 180.0,
        north=lat_of(tile.y),
    )


def subject_for(key: str) -> str:
    """Subject a frame for the leaf tile ``key`` is published on."""
    return ".".join((SUBJECT_ROOT, *key)) if key else SUBJECT_ROOT


def subscription_for(prefix: str, leaf_zoom: int) -> str:
    """Subject pattern that receives every leaf frame at or below ``prefix``."""
    if len(prefix) > leaf_zoom:
        msg = f"prefix {prefix!r} is deeper than the leaf zoom {leaf_zoom}"
        raise ValueError(msg)
    if len(prefix) == leaf_zoom:
        return subject_for(prefix)
    return ".".join((SUBJECT_ROOT, *prefix, ">"))


def quadkey_of_subject(subject: str) -> str:
    root, _, rest = subject.partition(".")
    if root != SUBJECT_ROOT or not rest:
        msg = f"not a position subject: {subject!r}"
        raise ValueError(msg)
    key = rest.replace(".", "")
    tile_of_quadkey(key)  # validates digits
    return key


def ancestors(key: str) -> Iterator[str]:
    """Yield ``key`` and every prefix of it, from the root ("") down."""
    for length in range(len(key) + 1):
        yield key[:length]


def _wrap(lon: float) -> float:
    """Bring a longitude into [-180, 180] without moving values already inside it."""
    if -180.0 <= lon <= 180.0:
        return lon
    return ((lon + 180.0) % 360.0) - 180.0


def _split_antimeridian(bbox: BBox) -> list[BBox]:
    south = _clamp(min(bbox.south, bbox.north), -MAX_MERCATOR_LAT, MAX_MERCATOR_LAT)
    north = _clamp(max(bbox.south, bbox.north), -MAX_MERCATOR_LAT, MAX_MERCATOR_LAT)
    if bbox.east - bbox.west >= 360.0:
        return [BBox(-180.0, south, 180.0, north)]
    west, east = _wrap(bbox.west), _wrap(bbox.east)
    if west <= east:
        return [BBox(west, south, east, north)]
    return [BBox(west, south, 180.0, north), BBox(-180.0, south, east, north)]


def _tile_ranges(parts: list[BBox], zoom: int) -> list[tuple[int, int, int, int]]:
    ranges = []
    for part in parts:
        top_left = tile_for(part.west, part.north, zoom)
        bottom_right = tile_for(part.east, part.south, zoom)
        ranges.append((top_left.x, bottom_right.x, top_left.y, bottom_right.y))
    return ranges


def covering_quadkeys(bbox: BBox, *, max_tiles: int, max_zoom: int) -> list[str]:
    """Quadkeys of the deepest level at which at most ``max_tiles`` tiles cover ``bbox``.

    The result is sorted and duplicate-free. A box crossing the antimeridian (``west > east``) is
    split in two. At zoom 0 the answer is the root prefix ``""`` (the whole world).
    """
    if max_tiles < 1:
        msg = "max_tiles must be at least 1"
        raise ValueError(msg)
    parts = _split_antimeridian(bbox)
    for zoom in range(max_zoom, -1, -1):
        ranges = _tile_ranges(parts, zoom)
        cells: set[tuple[int, int]] = set()
        too_many = False
        for x0, x1, y0, y1 in ranges:
            if (x1 - x0 + 1) * (y1 - y0 + 1) > max_tiles:
                too_many = True
                break
            cells.update((x, y) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1))
            if len(cells) > max_tiles:
                too_many = True
                break
        if not too_many:
            return sorted(quadkey(Tile(x, y, zoom)) for x, y in cells)
    return [""]
