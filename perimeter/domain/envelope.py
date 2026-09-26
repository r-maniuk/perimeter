"""Conservative lon/lat bounding boxes for geodesic circles on the WGS84 ellipsoid.

A geofence is a circle of radius ``r`` metres around a centre, measured along the ellipsoid.
PostGIS can decide membership exactly with ``ST_DWithin`` on ``geography``, but it cannot use an
index when every zone has its own radius. The database therefore stores, per zone, a planar
lon/lat box that is guaranteed to contain the whole circle and indexes that box with GiST; the
exact test only runs on the few zones whose box contains the point.

The box must never be too small, otherwise a device inside a zone would be missed. Two facts of
the ellipsoid give a proof rather than a guess:

* The length of one degree of latitude along a meridian is smallest at the equator,
  ``a(1 - e²)·π/180 ≈ 110 574 m``. Moving ``r`` metres therefore changes latitude by at most
  ``r / 110 574`` degrees.
* Every point of a geodesic of length ``≤ r`` from the centre stays within that latitude band, and
  the radius of the parallel at latitude ``φ`` is ``N(φ)·cos φ ≥ a·cos φ``. Moving ``r`` metres
  therefore changes longitude by at most ``r / (a·cos φ*·π/180)`` degrees, where ``φ*`` is the
  highest absolute latitude in the band.

This module mirrors the SQL function ``perimeter_envelope`` created by the initial migration; the
test suite checks that both agree and that no point of any circle falls outside its box.
"""

from __future__ import annotations

import math
from typing import NamedTuple

WGS84_A = 6_378_137.0
WGS84_E2 = 0.00669437999014
METERS_PER_DEGREE_LAT_MIN = WGS84_A * (1.0 - WGS84_E2) * math.pi / 180.0
METERS_PER_DEGREE_EQUATOR = WGS84_A * math.pi / 180.0
EPSILON_DEG = 1e-9
POLAR_CAP_DEG = 89.999999


class Box(NamedTuple):
    west: float
    south: float
    east: float
    north: float

    def contains(self, lon: float, lat: float) -> bool:
        return self.west <= lon <= self.east and self.south <= lat <= self.north


def envelope(lon: float, lat: float, radius_m: float) -> tuple[Box, ...]:
    """Return one box, or two when the circle crosses the antimeridian."""
    if radius_m < 0 or not math.isfinite(radius_m):
        msg = f"radius must be a finite non-negative number, got {radius_m!r}"
        raise ValueError(msg)
    dlat = radius_m / METERS_PER_DEGREE_LAT_MIN + EPSILON_DEG
    south = max(lat - dlat, -90.0)
    north = min(lat + dlat, 90.0)
    widest = max(abs(south), abs(north))
    if widest >= POLAR_CAP_DEG:
        return (Box(-180.0, south, 180.0, north),)
    dlon = radius_m / (METERS_PER_DEGREE_EQUATOR * math.cos(math.radians(widest))) + EPSILON_DEG
    if dlon >= 180.0:
        return (Box(-180.0, south, 180.0, north),)
    west, east = lon - dlon, lon + dlon
    if west < -180.0:
        return (Box(-180.0, south, east, north), Box(west + 360.0, south, 180.0, north))
    if east > 180.0:
        return (Box(west, south, 180.0, north), Box(-180.0, south, east - 360.0, north))
    return (Box(west, south, east, north),)


def covers(boxes: tuple[Box, ...], lon: float, lat: float) -> bool:
    return any(box.contains(lon, lat) for box in boxes)
