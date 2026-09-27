#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "aiohttp>=3.14.3",
# ]
# ///
"""Load generator for Perimeter: a fleet of simulated devices reporting their positions.

Run it against a stack with ``uv run generator.py --token ...``, inside the application image with
``python /app/generator.py``, or as the compose ``load`` service. ``--help`` lists every option and
the ``GENERATOR_*`` environment variable that sets it.

Open loop
    Every device reports on its own schedule however the server is doing, so a slow or shedding
    server shows up as queueing, throttling and dropped reports instead of quietly lowering the
    offered load (the "coordinated omission" trap of closed-loop load tools).

One clock, no task per device
    A heap holds each device's next report time. One task wakes every few milliseconds, pops the
    devices that are due, advances their motion to the exact due time and appends the encoded
    report to the current batch. Motion, GPS noise and report timing come from one seeded random
    generator consumed in schedule order, so ``--seed`` replays a run exactly. Timestamps are the
    scheduled times, and how late the clock runs behind them is reported as ``lag``.

Motion
    Random waypoint inside a disc. A trip is a few legs joined by turn-rate-limited curves and
    ends with a pause; vehicles, cyclists and pedestrians differ in speed, agility and pauses, and
    a share of devices never moves. Positions carry Gaussian GPS noise; speed and heading are
    reported (heading only while moving: a receiver has no course over ground when standing still).

Transports
    ``http`` posts ``{"reports": [...]}`` batches, one keep-alive connection per lane. ``ws``
    streams the same batches over WebSockets with credit-based flow control: the server grants a
    number of reports (``ready``/``ack``/``credit`` messages add to the allowance), acknowledges
    frames in order and withholds credit while it sheds load (``hold``); an ``error`` answer
    returns its frame's credit too. A frame never carries more reports than the credit left;
    frames unacknowledged when a socket drops are sent again after reconnecting (the server
    de-duplicates by device and timestamp within its 30 s window, and ignores a later copy).

Backpressure
    A 503 or 429 pauses only the lane that received it, for ``Retry-After`` (plus jitter); other
    failures back off exponentially. A batch is retried a bounded number of times. Reports wait
    in a bounded buffer that drops the oldest when full. Every report ends up accepted, rejected
    or dropped (with the reason), and the summary checks that the books balance.

Observe
    ``--observe USER`` signs in, optionally creates demo zones, opens the live channel with a
    viewport over the simulated area, decodes the binary position frames and measures end-to-end
    latency (device clock to live socket), and counts alert events and their latency. The API limits
    how fast an account changes its zones, so creating or deleting many of them waits out every 429
    as its ``Retry-After`` says, with a status line now and then; an interrupt stops the creating,
    and whatever was created is deleted at the end all the same.

Reading guide, in the order a report travels
    ``Profile``, ``Device``, ``Mobility``   how a device moves and what it reports
    ``Fleet``                              the heap that decides who reports next
    ``BatchQueue``, ``Drain``              the bounded buffer between the clock and the network
    ``HttpTransport``, ``WsTransport``     the two ways reports leave, with their backpressure
    ``Observer``                           sign-in, demo zones and the live channel's latency
    ``Stats``, ``Histogram``, ``Latency``  the books and the percentiles
    ``Config``, ``Option``                 every setting, its flag and its ``GENERATOR_*`` variable
    ``LoadRun``                            the run itself: start-up, the clock, the report, the end
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import heapq
import json
import math
import os
import platform
import random
import re
import secrets
import signal
import struct
import sys
import time
import traceback
from array import array
from collections import Counter, deque
from collections.abc import Callable, Coroutine, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import IO, Any, Final, Literal, NamedTuple, Protocol, TypeAlias
from urllib.parse import urlsplit, urlunsplit

import aiohttp

VERSION: Final = "1.0.0"
USER_AGENT: Final = f"perimeter-generator/{VERSION}"
RUNTIME: Final = (
    f"{platform.python_implementation()} {platform.python_version()}, aiohttp {aiohttp.__version__}"
)
ENV_PREFIX: Final = "GENERATOR_"

EXIT_OK: Final = 0
EXIT_FATAL: Final = 1
EXIT_UNREACHABLE: Final = 3
EXIT_FORCED: Final = 130

INGEST_PATH: Final = "/v1/telemetry"
STREAM_PATH: Final = "/v1/telemetry/stream"
SESSION_PATH: Final = "/v1/session"
TOKEN_PATH: Final = "/v1/token"  # noqa: S105 - a path, not a secret
ZONES_PATH: Final = "/v1/geozones"
LIVE_PATH: Final = "/v1/live"
JSON_TO_STDOUT: Final = Path("-")

TICK_S: Final = 0.01  # fleet clock resolution
STEP_S: Final = 1.0  # longest motion integration step
MAX_SLICE: Final = 4_000  # reports produced before the clock yields to the event loop
WAYPOINT_ATTEMPTS: Final = 8
INITIAL_PAUSED_SHARE: Final = 0.2
CONNECT_TIMEOUT_S: Final = 5.0
KEEPALIVE_S: Final = 30.0
WS_HEARTBEAT_S: Final = 15.0
WS_CLOSE_TIMEOUT_S: Final = 2.0
LIVE_MAX_MESSAGE: Final = 16 * 1024 * 1024
RETRY_AFTER_CAP_S: Final = 60.0
RETRY_AFTER_SPREAD: Final = 0.2
BACKOFF_BASE_S: Final = 0.25
BACKOFF_CAP_S: Final = 15.0
RECONNECT_BASE_S: Final = 0.5
RECONNECT_CAP_S: Final = 30.0
OBSERVE_TAIL_S: Final = 2.0
ZONE_ATTEMPTS: Final = 20  # times one zone change is sent at most while the API asks to wait
ZONE_PROGRESS_EVERY_S: Final = 5.0
WARN_EVERY_S: Final = 10.0
MAX_LATITUDE: Final = 85.0
AUTH_CLOSE_CODES: Final = frozenset({4001, 4003})
OBSERVER_FINAL_CLOSE_CODES: Final = frozenset({4001, 4003, 4009})
OVERLOADED_CLOSE_CODE: Final = 1013
FATAL_HTTP_STATUSES: Final = frozenset({401, 403, 404, 405})
THROTTLE_HTTP_STATUSES: Final = frozenset({429, 503})
GATEWAY_HTTP_STATUSES: Final = frozenset({502, 503, 504})
ZONE_COLORS: Final = (
    "#6d5dfc",
    "#0ea5e9",
    "#10b981",
    "#f59e0b",
    "#ef4444",
    "#ec4899",
    "#14b8a6",
    "#8b5cf6",
)

TransportName: TypeAlias = Literal["http", "ws"]
Batch: TypeAlias = list[str]


# --- errors --------------------------------------------------------------------------------------


class StartupError(Exception):
    """The target could not be reached, or refused this client, before the run started."""


class FatalError(Exception):
    """The server rejects this client in a way no retry can fix (credentials, wrong URL)."""


class StreamError(Exception):
    """A WebSocket stream broke: stalled, off-protocol or closed by the server."""


class StreamClosedError(StreamError):
    def __init__(self, code: int | None, reason: str = "") -> None:
        self.code = code
        super().__init__(f"closed with code {code}" + (f" ({reason})" if reason else ""))


class ProtocolError(StreamError):
    """The server sent something the protocol does not allow."""


# --- geodesy -------------------------------------------------------------------------------------


def degree_lengths(lat: float) -> tuple[float, float]:
    """Metres per degree of latitude and per degree of longitude at ``lat`` on WGS84."""
    phi = math.radians(lat)
    return (
        111_132.954 - 559.822 * math.cos(2.0 * phi) + 1.175 * math.cos(4.0 * phi),
        111_412.84 * math.cos(phi) - 93.5 * math.cos(3.0 * phi) + 0.118 * math.cos(5.0 * phi),
    )


def offset_m(lat0: float, lon0: float, lat1: float, lon1: float) -> tuple[float, float]:
    """East and north offset in metres from point 0 to point 1.

    Equirectangular, with both axes scaled at the mid latitude: over a city-sized area this is
    accurate to a fraction of a metre, which is all the simulation needs.
    """
    lat_m, lon_m = degree_lengths((lat0 + lat1) * 0.5)
    return (lon1 - lon0) * lon_m, (lat1 - lat0) * lat_m


def displace(lat: float, lon: float, east_m: float, north_m: float) -> tuple[float, float]:
    """The point ``east_m`` and ``north_m`` metres away: the inverse of :func:`offset_m`."""
    lat1 = lat + north_m / degree_lengths(lat)[0]
    lat1 = lat + north_m / degree_lengths((lat + lat1) * 0.5)[0]
    return lat1, lon + east_m / degree_lengths((lat + lat1) * 0.5)[1]


def viewport(center: tuple[float, float], radius_m: float) -> tuple[list[float], int]:
    """A ``[west, south, east, north]`` box around the simulated disc and a map zoom showing it."""
    lat, lon = center
    reach = radius_m * 1.02  # room for GPS noise at the edge
    dlat = reach / degree_lengths(lat)[0]
    south, north = max(lat - dlat, -MAX_LATITUDE), min(lat + dlat, MAX_LATITUDE)
    dlon = reach / degree_lengths(max(abs(south), abs(north)))[1]
    bbox = [round(lon - dlon, 6), round(south, 6), round(lon + dlon, 6), round(north, 6)]
    zoom = int(math.log2(1280.0 * 360.0 / (256.0 * 2.0 * dlon)))
    return bbox, min(max(zoom, 0), 20)


# --- motion --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Profile:
    """How one kind of device moves."""

    name: str
    tag: str  # device id prefix
    min_speed: float  # m/s
    max_speed: float
    turn_rate: float  # deg/s
    leg_min: float  # m between the waypoints of one trip
    leg_max: float
    max_legs: int  # waypoints per trip
    pause_min: float  # s between trips
    pause_max: float
    reach: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # Pure pursuit cannot reach a point inside its turning circle (it would orbit it), so a
        # waypoint counts as reached within twice the turning radius, plus one step of slack.
        object.__setattr__(self, "reach", 2.0 / math.radians(self.turn_rate) + STEP_S)

    def arrival_radius(self, speed: float) -> float:
        return self.reach * speed

    @property
    def edge_margin(self) -> float:
        """How far past its waypoints a device can get: a waypoint switch plus a full turn."""
        return 2.0 * self.arrival_radius(self.max_speed)


PROFILES: Final = {
    profile.name: profile
    for profile in (
        Profile("vehicle", "veh", 6.0, 17.0, 12.0, 300.0, 2_500.0, 5, 5.0, 60.0),
        Profile("cyclist", "bike", 3.0, 7.0, 30.0, 100.0, 1_000.0, 4, 5.0, 45.0),
        Profile("pedestrian", "ped", 1.0, 2.0, 60.0, 30.0, 400.0, 3, 10.0, 120.0),
    )
}
DEFAULT_MIX: Final = {"vehicle": 0.5, "cyclist": 0.3, "pedestrian": 0.2}


class Device:
    """Motion state of one simulated device: its true position, before GPS noise."""

    __slots__ = (
        "clock",
        "cruise",
        "heading",
        "lat",
        "legs",
        "lon",
        "pause",
        "profile",
        "speed",
        "stationary",
        "target_lat",
        "target_lon",
    )

    def __init__(self, profile: Profile, lat: float, lon: float, *, stationary: bool) -> None:
        self.profile = profile
        self.lat = lat
        self.lon = lon
        self.stationary = stationary
        self.heading = 0.0
        self.speed = 0.0
        self.cruise = 0.0
        self.target_lat = lat
        self.target_lon = lon
        self.legs = 0
        self.pause = 0.0
        self.clock = 0.0  # simulation time this state refers to


class Mobility:
    """Random-waypoint motion inside a disc, with smooth turns, pauses and GPS noise.

    Waypoints are drawn inside an inner disc whose margin covers the largest overshoot any profile
    can make (a waypoint switch plus a complete turn), so true positions never leave the disc.
    """

    def __init__(
        self,
        *,
        center: tuple[float, float],
        radius_m: float,
        noise_m: float,
        rng: random.Random,
    ) -> None:
        self.center_lat, self.center_lon = center
        self.radius_m = radius_m
        self.inner_radius_m = radius_m - max(p.edge_margin for p in PROFILES.values())
        if self.inner_radius_m < radius_m / 2:
            msg = f"a {radius_m:.0f} m disc is too small for the movement profiles"
            raise ValueError(msg)
        self.noise_m = noise_m
        self._rng = rng
        lat_m, lon_m = degree_lengths(self.center_lat)
        self._noise_lat = noise_m / lat_m
        self._noise_lon = noise_m / lon_m

    def distance_from_center(self, lat: float, lon: float) -> float:
        return math.hypot(*offset_m(self.center_lat, self.center_lon, lat, lon))

    def random_point(self, radius_m: float) -> tuple[float, float]:
        """A point drawn uniformly (by area) from the disc of ``radius_m`` around the centre."""
        distance = radius_m * math.sqrt(self._rng.random())
        theta = self._rng.uniform(0.0, math.tau)
        return displace(
            self.center_lat, self.center_lon, distance * math.sin(theta), distance * math.cos(theta)
        )

    def spawn(self, profile: Profile, *, stationary: bool) -> Device:
        lat, lon = self.random_point(self.inner_radius_m)
        device = Device(profile, lat, lon, stationary=stationary)
        if not stationary:
            if self._rng.random() < INITIAL_PAUSED_SHARE:
                device.pause = self._rng.uniform(0.0, profile.pause_max)
            else:
                self._start_trip(device)
        return device

    def advance(self, device: Device, dt: float) -> None:
        """Move ``device`` forward by ``dt`` seconds of simulated time."""
        if device.stationary or dt <= 0.0:
            return
        profile = device.profile
        if device.pause <= 0.0:
            # Speed wanders around the trip's cruise speed, inside the profile's range.
            drift = (device.cruise - device.speed) * 0.5 + self._rng.gauss(
                0.0, 0.03 * device.cruise
            )
            device.speed = min(max(device.speed + drift, profile.min_speed), profile.max_speed)
        lat_m, lon_m = degree_lengths(device.lat)
        remaining = dt
        while remaining > 1e-9:
            if device.pause > 0.0:
                spent = min(device.pause, remaining)
                device.pause -= spent
                remaining -= spent
                if device.pause <= 0.0:
                    device.pause = 0.0
                    self._start_trip(device)
                continue
            east = (device.target_lon - device.lon) * lon_m
            north = (device.target_lat - device.lat) * lat_m
            if math.hypot(east, north) <= profile.arrival_radius(device.speed):
                if device.legs > 1:
                    device.legs -= 1
                    self._next_waypoint(device)
                else:
                    device.legs = 0
                    device.speed = 0.0
                    device.pause = self._rng.uniform(profile.pause_min, profile.pause_max)
                continue
            step = min(remaining, STEP_S)
            remaining -= step
            error = (math.degrees(math.atan2(east, north)) - device.heading + 540.0) % 360.0 - 180.0
            limit = profile.turn_rate * step
            heading = (device.heading + min(max(error, -limit), limit)) % 360.0
            device.heading = heading
            travel = device.speed * step
            rad = math.radians(heading)
            device.lat += travel * math.cos(rad) / lat_m
            device.lon += travel * math.sin(rad) / lon_m

    def fix(self, device: Device) -> tuple[float, float]:
        """The position a GPS receiver reports: the true one plus Gaussian noise."""
        if self.noise_m <= 0.0:
            return device.lat, device.lon
        gauss = self._rng.gauss
        return device.lat + gauss(0.0, self._noise_lat), device.lon + gauss(0.0, self._noise_lon)

    def _start_trip(self, device: Device) -> None:
        profile = device.profile
        device.legs = self._rng.randint(1, profile.max_legs)
        device.cruise = self._rng.uniform(profile.min_speed, profile.max_speed)
        device.speed = device.cruise
        self._next_waypoint(device)
        # A device turns while standing still, so every trip starts on course.
        east, north = offset_m(device.lat, device.lon, device.target_lat, device.target_lon)
        device.heading = math.degrees(math.atan2(east, north)) % 360.0

    def _next_waypoint(self, device: Device) -> None:
        profile = device.profile
        for _ in range(WAYPOINT_ATTEMPTS):
            distance = self._rng.uniform(profile.leg_min, profile.leg_max)
            theta = self._rng.uniform(0.0, math.tau)
            lat, lon = displace(
                device.lat, device.lon, distance * math.sin(theta), distance * math.cos(theta)
            )
            if self.distance_from_center(lat, lon) <= self.inner_radius_m:
                break
        else:
            lat, lon = self.random_point(self.inner_radius_m)
        device.target_lat = lat
        device.target_lon = lon


_MOVING_REPORT: Final = (
    '{"device_id":"%s","latitude":%.7f,"longitude":%.7f,"timestamp":"%s.%03dZ",'
    '"speed":%.2f,"heading":%.1f}'
)
_STILL_REPORT: Final = (
    '{"device_id":"%s","latitude":%.7f,"longitude":%.7f,"timestamp":"%s.%03dZ","speed":0}'
)


class Fleet:
    """The simulated devices and the schedule of their next reports."""

    def __init__(
        self,
        *,
        count: int,
        interval: float,
        jitter: float,
        ramp: float,
        mix: Mapping[str, float],
        stationary: float,
        id_prefix: str,
        mobility: Mobility,
        rng: random.Random,
    ) -> None:
        self.interval = interval
        self.jitter = jitter
        self.mobility = mobility
        self._rng = rng
        names = [name for name, weight in mix.items() if weight > 0]
        profiles = rng.choices([PROFILES[name] for name in names], [mix[n] for n in names], k=count)
        width = max(5, len(str(count - 1)))
        # Start times spread over the ramp (at least one interval) so the fleet never reports in
        # lock-step; after that every device keeps its own jittered rhythm.
        window = max(ramp, interval)
        self.ids: list[str] = []
        self.devices: list[Device] = []
        self._schedule: list[tuple[float, int]] = []
        for index, profile in enumerate(profiles):
            device = mobility.spawn(profile, stationary=rng.random() < stationary)
            device.clock = rng.uniform(0.0, window)
            self.devices.append(device)
            self.ids.append(f"{id_prefix}{profile.tag}-{index:0{width}d}")
            self._schedule.append((device.clock, index))
        heapq.heapify(self._schedule)
        self._second = -1
        self._stamp = ""

    def __len__(self) -> int:
        return len(self.devices)

    def emit(self, until: float, wall0_ms: int, out: list[str], limit: int) -> float | None:
        """Append the reports of up to ``limit`` devices due at or before ``until``.

        ``until`` is simulation time in seconds; a report's timestamp is ``wall0_ms`` plus its due
        time. Returns the due time of the oldest report emitted, or ``None`` if none was due.
        """
        schedule = self._schedule
        if not schedule or schedule[0][0] > until:
            return None
        oldest = schedule[0][0]
        devices, ids = self.devices, self.ids
        advance, fix = self.mobility.advance, self.mobility.fix
        uniform, interval, jitter = self._rng.uniform, self.interval, self.jitter
        pop, push, append = heapq.heappop, heapq.heappush, out.append
        second, stamp = self._second, self._stamp
        emitted = 0
        while schedule and schedule[0][0] <= until and emitted < limit:
            due, index = pop(schedule)
            device = devices[index]
            advance(device, due - device.clock)
            device.clock = due
            lat, lon = fix(device)
            seconds, millis = divmod(wall0_ms + int(due * 1000.0), 1000)
            if seconds != second:
                second = seconds
                stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(seconds))
            if device.speed > 0.0:
                heading = device.heading if device.heading < 359.95 else 0.0
                append(
                    _MOVING_REPORT % (ids[index], lat, lon, stamp, millis, device.speed, heading)
                )
            else:
                append(_STILL_REPORT % (ids[index], lat, lon, stamp, millis))
            push(schedule, (due + interval * (1.0 + uniform(-jitter, jitter)), index))
            emitted += 1
        self._second, self._stamp = second, stamp
        return oldest


# --- measurement ---------------------------------------------------------------------------------


class Histogram:
    """Latency histogram with logarithmic buckets: O(1) recording, fixed memory, ~1% error."""

    __slots__ = ("_counts", "count", "maximum", "minimum")

    LOWEST: Final = 0.01  # ms
    GROWTH: Final = 1.02
    BUCKETS: Final = 1_000  # up to ~67 minutes
    _SCALE: Final = 1.0 / math.log(GROWTH)

    def __init__(self) -> None:
        self._counts = [0] * self.BUCKETS
        self.count = 0
        self.maximum = 0.0
        self.minimum = math.inf

    def record(self, value: float) -> None:
        value = max(value, 0.0)
        index = int(math.log(value / self.LOWEST) * self._SCALE) if value > self.LOWEST else 0
        self._counts[min(index, self.BUCKETS - 1)] += 1
        self.count += 1
        self.maximum = max(self.maximum, value)
        self.minimum = min(self.minimum, value)

    def percentile(self, q: float) -> float | None:
        """Nearest-rank percentile for ``q`` in [0, 1]; ``None`` while empty."""
        if not self.count:
            return None
        rank = max(1, math.ceil(q * self.count))
        seen = 0
        for index, bucket in enumerate(self._counts):
            seen += bucket
            if seen >= rank:
                middle = self.LOWEST * math.pow(self.GROWTH, index + 0.5)
                return min(max(middle, self.minimum), self.maximum)
        return self.maximum

    def merge(self, other: Histogram) -> None:
        self._counts = [a + b for a, b in zip(self._counts, other._counts, strict=True)]
        self.count += other.count
        self.maximum = max(self.maximum, other.maximum)
        self.minimum = min(self.minimum, other.minimum)

    def clear(self) -> None:
        self._counts = [0] * self.BUCKETS
        self.count = 0
        self.maximum = 0.0
        self.minimum = math.inf

    def summary(self) -> dict[str, float | int | None]:
        return {
            "count": self.count,
            "p50": _round(self.percentile(0.50)),
            "p95": _round(self.percentile(0.95)),
            "p99": _round(self.percentile(0.99)),
            "max": _round(self.maximum) if self.count else None,
        }


class Latency:
    """A latency series: the current status window plus everything since the start."""

    __slots__ = ("total", "window")

    def __init__(self) -> None:
        self.window = Histogram()
        self.total = Histogram()

    def record(self, ms: float) -> None:
        self.window.record(ms)

    def roll(self) -> tuple[float | None, float | None, float | None]:
        """p50/p95/p99 of the window just ended; the window then joins the total."""
        window = self.window
        result = (window.percentile(0.50), window.percentile(0.95), window.percentile(0.99))
        self.total.merge(window)
        window.clear()
        return result


class Stats:
    """Counters of one run. Every offered report ends up accepted, rejected or dropped."""

    def __init__(self) -> None:
        self.offered = 0
        self.sent = 0
        self.resent = 0
        self.accepted = 0
        self.rejected = 0
        self.throttled = 0
        self.connections_opened = 0
        self.dropped: Counter[str] = Counter()
        self.rejected_codes: Counter[str] = Counter()
        self.errors: Counter[str] = Counter()
        self.ack = Latency()
        self.lag = Latency()
        self.e2e = Latency()
        self.alert_latency = Latency()
        self.alerts: Counter[str] = Counter()
        self.events = 0
        self.pulses = 0
        self.positions = 0
        self.snapshot_positions = 0

    def drop(self, count: int, reason: str) -> None:
        if count > 0:
            self.dropped[reason] += count

    def reject(self, count: int, code: str) -> None:
        if count > 0:
            self.rejected += count
            self.rejected_codes[code] += count

    def error(self, kind: str) -> None:
        self.errors[kind] += 1

    @property
    def dropped_total(self) -> int:
        return self.dropped.total()

    @property
    def unsettled(self) -> int:
        """Reports neither accepted, rejected nor dropped yet (zero once a run is over)."""
        return self.offered - self.accepted - self.rejected - self.dropped_total


def settle(stats: Stats, console: Console, size: int, accepted: int, codes: Sequence[str]) -> None:
    """Book a server receipt for ``size`` reports: ``accepted`` of them plus one code per reject."""
    accepted = min(max(accepted, 0), size - len(codes))
    stats.accepted += accepted
    for code in codes:
        stats.reject(1, code)
    missing = size - accepted - len(codes)
    if missing > 0:
        stats.reject(missing, "unacknowledged")
        console.warn("receipt", f"a receipt left {missing} of {size} reports unaccounted for")


def rejection_codes(rejected: Any) -> list[str]:
    """One code per entry of a ``rejected`` list (``[{"index", "code", "detail"}, ...]``)."""
    if not isinstance(rejected, list):
        return []
    return [
        str(item.get("code") or "rejected") if isinstance(item, dict) else "rejected"
        for item in rejected
    ]


def parse_receipt(payload: Any, size: int) -> tuple[int, list[str]]:
    """``accepted`` and the rejection codes of ``{"accepted": n, "rejected": [...]}``."""
    if not isinstance(payload, dict):
        return size, []
    codes = rejection_codes(payload.get("rejected"))
    accepted = payload.get("accepted")
    if not isinstance(accepted, int) or isinstance(accepted, bool):
        accepted = size - len(codes)
    return accepted, codes


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    """Seconds to wait according to a ``Retry-After`` header, capped; ``None`` if unusable.

    Accepts delta-seconds (decimals tolerated) and HTTP dates; a date in the past means "now".
    """
    if value is None or not value.strip():
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = max(when.timestamp() - (time.time() if now is None else now), 0.0)
    if math.isnan(seconds) or seconds < 0:
        return None
    return min(seconds, RETRY_AFTER_CAP_S)


class Backoff:
    """Exponential back-off with "equal jitter": attempt n waits U(c/2, c), c = min(cap, base*2^n).

    Half of each delay is fixed, so failing clients never retry in a tight loop; the other half is
    random, so clients that failed together do not come back together.
    """

    def __init__(self, base: float, cap: float, rng: random.Random) -> None:
        self._base = base
        self._cap = cap
        self._rng = rng
        self._attempt = 0

    def next(self) -> float:
        ceiling = min(self._cap, self._base * 2.0**self._attempt)
        self._attempt = min(self._attempt + 1, 30)
        return ceiling / 2.0 + self._rng.uniform(0.0, ceiling / 2.0)

    def reset(self) -> None:
        self._attempt = 0


class Console:
    """Status lines on one stream, warnings and errors on another; repeats are rate limited."""

    def __init__(self, out: IO[str], err: IO[str]) -> None:
        self._out = out
        self._err = err
        self._origin = time.monotonic()
        self._warned: dict[str, float] = {}
        self._suppressed: Counter[str] = Counter()

    def elapsed(self) -> float:
        return time.monotonic() - self._origin

    def line(self, text: str = "") -> None:
        print(text, file=self._out, flush=True)

    def warn(self, key: str, text: str) -> None:
        now = time.monotonic()
        last = self._warned.get(key)
        if last is not None and now - last < WARN_EVERY_S:
            self._suppressed[key] += 1
            return
        self._warned[key] = now
        repeats = self._suppressed.pop(key, 0)
        suffix = f" (and {repeats} more like it)" if repeats else ""
        print(f"[{self.elapsed():7.1f}s] warning: {text}{suffix}", file=self._err, flush=True)

    def error(self, text: str) -> None:
        print(f"error: {text}", file=self._err, flush=True)

    def exception(self, error: BaseException) -> None:
        print("".join(traceback.format_exception(error)), end="", file=self._err, flush=True)


def describe(exc: BaseException) -> str:
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


_ERROR_KINDS: Final = (
    (aiohttp.ClientConnectorError, "connect"),
    (TimeoutError, "timeout"),
    (aiohttp.ServerDisconnectedError, "disconnected"),
    (ProtocolError, "protocol"),
)


def error_kind(exc: BaseException) -> str:
    """A short, stable name for a failure, used to count errors by kind."""
    if isinstance(exc, aiohttp.WSServerHandshakeError):
        return f"handshake_{exc.status}"
    if isinstance(exc, StreamClosedError):
        return f"ws_close_{exc.code}"
    return next((kind for cls, kind in _ERROR_KINDS if isinstance(exc, cls)), type(exc).__name__)


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


# --- batching ------------------------------------------------------------------------------------


class BatchQueue:
    """Bounded FIFO of batches between the fleet clock and the senders.

    When full it drops the *oldest* batches: a tracking system values the newest positions most,
    and a device with a full buffer would do the same. Getters are only woken up and pop items
    themselves, so cancelling a waiting sender can never lose a batch.
    """

    def __init__(self, capacity: int, stats: Stats) -> None:
        self._capacity = capacity
        self._stats = stats
        self._batches: deque[Batch] = deque()
        self._size = 0
        self._waiters: deque[asyncio.Future[None]] = deque()
        self._closed = False

    @property
    def size(self) -> int:
        """Reports waiting in the queue."""
        return self._size

    @property
    def closed(self) -> bool:
        return self._closed

    def put(self, batch: Batch) -> None:
        if self._closed:
            msg = "the queue is closed"
            raise RuntimeError(msg)
        while self._batches and self._size + len(batch) > self._capacity:
            oldest = self._batches.popleft()
            self._size -= len(oldest)
            self._stats.drop(len(oldest), "overflow")
        self._batches.append(batch)
        self._size += len(batch)
        self._wake_one()

    async def get(self) -> Batch | None:
        """The oldest batch, waiting for one if needed; ``None`` once closed and empty."""
        while not self._batches:
            if self._closed:
                return None
            waiter = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)
            try:
                await waiter
            except asyncio.CancelledError:
                if waiter.done() and not waiter.cancelled():
                    self._wake_one()  # pass on a wake-up this getter can no longer use
                raise
            finally:
                with contextlib.suppress(ValueError):
                    self._waiters.remove(waiter)
        batch = self._batches.popleft()
        self._size -= len(batch)
        return batch

    def close(self) -> None:
        self._closed = True
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)

    def clear(self) -> int:
        """Remove everything still queued; returns the number of reports removed."""
        removed = self._size
        self._batches.clear()
        self._size = 0
        return removed

    def _wake_one(self) -> None:
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
                return


# --- transports ----------------------------------------------------------------------------------


class Drain:
    """The shutdown signal shared by the senders of one transport.

    Once the run drains, a back-off sleep that would end after the grace deadline is cut short,
    so shutdown never waits on a long ``Retry-After``.
    """

    def __init__(self) -> None:
        self.deadline: float | None = None
        self._started = asyncio.Event()

    def begin(self, deadline: float) -> None:
        self.deadline = deadline
        self._started.set()

    async def sleep(self, delay: float) -> bool:
        """Sleep ``delay`` seconds; ``False`` (early) if that would outlast the grace deadline."""
        loop = asyncio.get_running_loop()
        wake_at = loop.time() + delay
        if not self._started.is_set():
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(delay):
                    await self._started.wait()
        if self.deadline is not None and wake_at > self.deadline:
            return False
        await asyncio.sleep(max(wake_at - loop.time(), 0.0))
        return True


class Transport(Protocol):
    drain: Drain

    async def open(self) -> None: ...

    def lanes(self) -> list[Coroutine[Any, Any, None]]: ...

    def in_flight(self) -> int: ...

    def waiting(self) -> int: ...

    async def close(self) -> None: ...


def startup_verdict(url: str, status: int, retry_after: float | None) -> str | None:
    """Why a first response means the run cannot start, or ``None`` if it can.

    A 503 or 429 that carries ``Retry-After`` is the API shedding load: it is reachable and the
    senders will honour the pause. The same statuses without it, and 502/504, are what a proxy
    answers when no API instance is available.
    """
    if status in (401, 403):
        return f"{url} rejected the ingest token (HTTP {status})"
    if status in (404, 405):
        return f"no ingest endpoint at {url} (HTTP {status})"
    if status in GATEWAY_HTTP_STATUSES and retry_after is None:
        return f"{url} answered HTTP {status}: no API instance is available behind it"
    return None


def _retry_after_of(exc: aiohttp.ClientResponseError) -> float | None:
    return parse_retry_after(exc.headers.get("Retry-After") if exc.headers else None)


def _json_body(payload: bytes) -> Any:
    try:
        return json.loads(payload)
    except ValueError:
        return None


class HttpTransport:
    """``POST /v1/telemetry`` batches; each lane owns one keep-alive connection."""

    def __init__(self, config: Config, queue: BatchQueue, stats: Stats, console: Console) -> None:
        self._config = config
        self._queue = queue
        self._stats = stats
        self._console = console
        self._url = config.url + INGEST_PATH
        self._sessions: list[aiohttp.ClientSession] = []
        self._held: list[Batch | None] = []
        self._requesting: list[bool] = []
        self._jitter = random.Random()  # network timing must not consume the seeded fleet stream
        self.drain = Drain()

    async def open(self) -> None:
        timeout = aiohttp.ClientTimeout(
            total=self._config.timeout, connect=min(self._config.timeout, CONNECT_TIMEOUT_S)
        )
        headers = {
            "Authorization": f"Bearer {self._config.token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        for _ in range(self._config.connections):
            connector = aiohttp.TCPConnector(limit=1, keepalive_timeout=KEEPALIVE_S)
            self._sessions.append(
                aiohttp.ClientSession(connector=connector, timeout=timeout, headers=headers)
            )
        self._held = [None] * len(self._sessions)
        self._requesting = [False] * len(self._sessions)
        await self._preflight(self._sessions[0])

    async def _preflight(self, session: aiohttp.ClientSession) -> None:
        """An empty batch: proves the endpoint exists and the token is accepted."""
        try:
            async with session.post(self._url, data=b'{"reports":[]}') as response:
                await response.read()
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
        except (aiohttp.ClientError, TimeoutError) as exc:
            msg = f"cannot reach {self._url}: {describe(exc)}"
            raise StartupError(msg) from exc
        verdict = startup_verdict(self._url, response.status, retry_after)
        if verdict is not None:
            raise StartupError(verdict)

    def lanes(self) -> list[Coroutine[Any, Any, None]]:
        return [self._lane(index) for index in range(len(self._sessions))]

    def in_flight(self) -> int:
        return sum(
            len(batch)
            for batch, active in zip(self._held, self._requesting, strict=True)
            if batch is not None and active
        )

    def waiting(self) -> int:
        return sum(
            len(batch)
            for batch, active in zip(self._held, self._requesting, strict=True)
            if batch is not None and not active
        )

    async def close(self) -> None:
        for session in self._sessions:
            await session.close()

    async def _lane(self, index: int) -> None:
        session = self._sessions[index]
        backoff = Backoff(BACKOFF_BASE_S, BACKOFF_CAP_S, self._jitter)
        while (batch := await self._queue.get()) is not None:
            self._held[index] = batch
            await self._deliver(index, session, batch, backoff)
            # Only reached once the batch is settled; a cancelled lane leaves it held, and the run
            # books it as dropped.
            self._held[index] = None

    async def _deliver(
        self, index: int, session: aiohttp.ClientSession, batch: Batch, backoff: Backoff
    ) -> None:
        stats, console = self._stats, self._console
        loop = asyncio.get_running_loop()
        size = len(batch)
        body = ('{"reports":[' + ",".join(batch) + "]}").encode()
        attempt = 0
        while True:
            if attempt:
                stats.resent += size
            else:
                stats.sent += size
            attempt += 1
            started = loop.time()
            self._requesting[index] = True
            try:
                async with session.post(self._url, data=body) as response:
                    payload = await response.read()
                    status = response.status
                    retry_after = parse_retry_after(response.headers.get("Retry-After"))
            except (aiohttp.ClientError, TimeoutError) as exc:
                kind = error_kind(exc)
                stats.error(kind)
                console.warn(kind, f"ingest request failed: {describe(exc)}")
                delay = backoff.next()
            else:
                if 200 <= status < 300:
                    stats.ack.record((loop.time() - started) * 1000.0)
                    accepted, codes = parse_receipt(_json_body(payload), size)
                    settle(stats, console, size, accepted, codes)
                    backoff.reset()
                    return
                if status in THROTTLE_HTTP_STATUSES:
                    stats.throttled += 1
                    delay = retry_after if retry_after is not None else backoff.next()
                    delay *= 1.0 + RETRY_AFTER_SPREAD * self._jitter.random()
                elif status in FATAL_HTTP_STATUSES:
                    msg = f"{self._url} answered HTTP {status}: {_problem_text(payload)}"
                    raise FatalError(msg)
                elif status >= 500:
                    stats.error(f"http_{status}")
                    console.warn(f"http_{status}", f"server error HTTP {status}, backing off")
                    delay = backoff.next()
                else:
                    self._reject_batch(size, status, payload)
                    return
            finally:
                self._requesting[index] = False
            if attempt > self._config.retries:
                stats.drop(size, "retries")
                return
            if not await self.drain.sleep(delay):
                stats.drop(size, "shutdown")
                return

    def _reject_batch(self, size: int, status: int, payload: bytes) -> None:
        """A 4xx for the whole batch: every report is rejected, per-report codes when given."""
        problem = _json_body(payload)
        fallback = f"http_{status}"
        codes: list[str] = []
        if isinstance(problem, dict):
            fallback = str(problem.get("code") or fallback)
            codes = rejection_codes(problem.get("rejected"))[:size]
        codes += [fallback] * (size - len(codes))
        settle(self._stats, self._console, size, 0, codes)
        if status == 413:
            self._console.warn("413", "the server refused a batch as too large: lower --batch")
        else:
            self._console.warn(f"http_{status}", f"server rejected a whole batch: HTTP {status}")


def _problem_text(payload: bytes) -> str:
    problem = _json_body(payload)
    if isinstance(problem, dict):
        return str(problem.get("detail") or problem.get("title") or problem.get("code") or "")
    return payload[:200].decode(errors="replace")


def _websocket_url(base: str, path: str) -> str:
    parts = urlsplit(base + path)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, ""))


class _SentFrame(NamedTuple):
    seq: int
    reports: Batch
    sent_at: float


class WsTransport:
    """``/v1/telemetry/stream``: batches as frames under credit-based flow control."""

    def __init__(self, config: Config, queue: BatchQueue, stats: Stats, console: Console) -> None:
        self.config = config
        self.queue = queue
        self.stats = stats
        self.console = console
        self.url = _websocket_url(config.url, STREAM_PATH)
        self.jitter = random.Random()  # network timing must not consume the seeded fleet stream
        self.drain = Drain()
        self._lanes: list[_WsLane] = []
        self._sessions: list[aiohttp.ClientSession] = []
        self._first_delay = 0.0

    async def open(self) -> None:
        # No total timeout: it would bound the lifetime of the socket, not just the handshake.
        timeout = aiohttp.ClientTimeout(
            total=None, connect=min(self.config.timeout, CONNECT_TIMEOUT_S)
        )
        for index in range(self.config.connections):
            session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=1),
                timeout=timeout,
                headers={"User-Agent": USER_AGENT},
            )
            self._sessions.append(session)
            self._lanes.append(_WsLane(index, self, session))
        try:
            await self._lanes[0].connect()
        except aiohttp.WSServerHandshakeError as exc:
            retry_after = _retry_after_of(exc)
            verdict = startup_verdict(self.url, exc.status, retry_after)
            if verdict is None and exc.status in THROTTLE_HTTP_STATUSES and retry_after is not None:
                self.stats.throttled += 1  # shedding: the first lane starts when told to
                self._first_delay = retry_after
                return
            raise StartupError(verdict or f"cannot open {self.url}: HTTP {exc.status}") from exc
        except StreamClosedError as exc:
            msg = f"{self.url} closed the stream before it was ready: {exc}"
            raise StartupError(msg) from exc
        except (aiohttp.ClientError, TimeoutError, StreamError, ValueError) as exc:
            msg = f"cannot open {self.url}: {describe(exc)}"
            raise StartupError(msg) from exc

    def lanes(self) -> list[Coroutine[Any, Any, None]]:
        return [lane.run(self._first_delay if lane.index == 0 else 0.0) for lane in self._lanes]

    def in_flight(self) -> int:
        return sum(lane.in_flight() for lane in self._lanes)

    def waiting(self) -> int:
        return sum(lane.waiting() for lane in self._lanes)

    async def close(self) -> None:
        for session in self._sessions:
            await session.close()

    def finished(self, lane: _WsLane) -> bool:
        return self.queue.closed and not self.queue.size and not lane.carry and not lane.inflight


class _WsLane:
    """One WebSocket: its credit, the frames awaiting acknowledgement and reports on hold."""

    def __init__(self, index: int, transport: WsTransport, session: aiohttp.ClientSession) -> None:
        self.index = index
        self._t = transport
        self._session = session
        self.credit = 0
        self.seq = 0
        self.hold_until = 0.0
        self.carry: deque[tuple[Batch, bool]] = deque()  # (reports, sent before)
        self.inflight: deque[_SentFrame] = deque()
        self._wake = asyncio.Event()
        self._backoff = Backoff(RECONNECT_BASE_S, RECONNECT_CAP_S, transport.jitter)
        self._ws: aiohttp.ClientWebSocketResponse | None = None

    def in_flight(self) -> int:
        return sum(len(frame.reports) for frame in self.inflight)

    def waiting(self) -> int:
        return sum(len(reports) for reports, _ in self.carry)

    async def connect(self) -> None:
        """Open the socket and wait for ``ready``, which carries the initial credit."""
        t = self._t
        async with asyncio.timeout(t.config.timeout):
            ws = await self._session.ws_connect(
                t.url,
                headers={"Authorization": f"Bearer {t.config.token}"},
                heartbeat=WS_HEARTBEAT_S,
                timeout=aiohttp.ClientWSTimeout(ws_close=WS_CLOSE_TIMEOUT_S),
            )
            try:
                message = await ws.receive()
                if message.type is not aiohttp.WSMsgType.TEXT:
                    raise StreamClosedError(ws.close_code, "before the ready message")
                ready = json.loads(message.data)
                if not isinstance(ready, dict) or ready.get("type") != "ready":
                    msg = f"expected a ready message, got {str(message.data)[:120]!r}"
                    raise ProtocolError(msg)
            except BaseException:
                await ws.close()
                raise
        self.credit = _grant(ready)
        self.seq = 0
        self.hold_until = 0.0
        self._ws = ws
        t.stats.connections_opened += 1

    async def run(self, first_delay: float = 0.0) -> None:
        """Stream until the run drains; reconnect with back-off whenever the socket drops."""
        if first_delay and not await self._t.drain.sleep(first_delay):
            return
        while not (self._ws is None and self._t.finished(self)):
            try:
                if self._ws is None:
                    await self._reconnect()
                await self._pump()
            except (aiohttp.ClientError, TimeoutError, StreamError, ValueError) as exc:
                if not await self._t.drain.sleep(self._lost(exc)):
                    return
                continue
            return

    async def _reconnect(self) -> None:
        try:
            await self.connect()
        except aiohttp.WSServerHandshakeError as exc:
            if exc.status in FATAL_HTTP_STATUSES:
                msg = f"{self._t.url} refused the stream: HTTP {exc.status}"
                raise FatalError(msg) from exc
            raise
        self._backoff.reset()

    async def _pump(self) -> None:
        """Run reader and writer until the lane is drained or the socket breaks."""
        ws = self._ws
        assert ws is not None
        reader = asyncio.create_task(self._read(ws))
        writer = asyncio.create_task(self._write(ws))
        try:
            done, _ = await asyncio.wait({reader, writer}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (reader, writer):
                task.cancel()
            await asyncio.gather(reader, writer, return_exceptions=True)
            self._ws = None
            with contextlib.suppress(aiohttp.ClientError, OSError):
                await ws.close()
        if writer in done and writer.exception() is None:
            return  # drained
        failed = reader if reader in done else writer
        error = failed.exception()
        raise error if error is not None else StreamClosedError(ws.close_code, "stream ended")

    async def _write(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        t = self._t
        stats = t.stats
        loop = asyncio.get_running_loop()
        while True:
            if self.credit <= 0 or loop.time() < self.hold_until:
                await self._await_credit()
                continue
            chunk = await self._take(min(self.credit, t.config.batch))
            if chunk is None:
                if not self.inflight:
                    return  # drained: nothing left to send, everything acknowledged
                self._wake.clear()
                await self._wake.wait()
                continue
            reports, resend = chunk
            if loop.time() < self.hold_until:
                self.carry.appendleft(chunk)  # the server asked to hold while we waited
                continue
            self.seq += 1
            self.inflight.append(_SentFrame(self.seq, reports, loop.time()))
            self.credit -= len(reports)
            if resend:
                stats.resent += len(reports)
            else:
                stats.sent += len(reports)
            await ws.send_str(
                '{"type":"reports","seq":'
                + str(self.seq)
                + ',"reports":['
                + ",".join(reports)
                + "]}"
            )

    async def _take(self, limit: int) -> tuple[Batch, bool] | None:
        """Up to ``limit`` reports: first those on hold in this lane, then the next batch."""
        if not self.carry:
            batch = await self._t.queue.get()
            if batch is None:
                return None
            self.carry.append((batch, False))
        reports, resend = self.carry.popleft()
        if len(reports) > limit:
            self.carry.appendleft((reports[limit:], resend))
            reports = reports[:limit]
        return reports, resend

    async def _await_credit(self) -> None:
        self._wake.clear()
        loop = asyncio.get_running_loop()
        held_for = self.hold_until - loop.time()
        if self.credit > 0 and held_for > 0:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(held_for):
                    await self._wake.wait()
        else:
            await self._wake.wait()

    async def _read(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        t = self._t
        while True:
            # Frames in flight must be acknowledged within the timeout, or the stream is stalled.
            try:
                message = await ws.receive(timeout=t.config.timeout if self.inflight else None)
            except TimeoutError:
                msg = f"no acknowledgement within {t.config.timeout:g} s"
                raise ProtocolError(msg) from None
            if message.type is aiohttp.WSMsgType.TEXT:
                self._handle(json.loads(message.data))
            elif message.type is aiohttp.WSMsgType.BINARY:
                msg = "unexpected binary message on the ingest stream"
                raise ProtocolError(msg)
            elif message.type is aiohttp.WSMsgType.ERROR:
                raise StreamClosedError(ws.close_code, describe(ws.exception() or Exception()))
            else:
                reason = message.extra if isinstance(message.extra, str) else ""
                raise StreamClosedError(ws.close_code, reason)

    def _handle(self, message: Any) -> None:
        if not isinstance(message, dict):
            msg = "the server sent a message that is not a JSON object"
            raise ProtocolError(msg)
        t = self._t
        kind = message.get("type")
        grant = _grant(message)
        if grant:
            self.credit += grant
            self._wake.set()
        if kind in ("ack", "error"):
            seq = message.get("seq")
            if not self.inflight or self.inflight[0].seq != seq:
                expected = self.inflight[0].seq if self.inflight else None
                msg = f"{kind} for frame {seq!r} while frame {expected!r} is the oldest in flight"
                raise ProtocolError(msg)
            frame = self.inflight.popleft()
            size = len(frame.reports)
            if kind == "ack":
                t.stats.ack.record((asyncio.get_running_loop().time() - frame.sent_at) * 1000.0)
                accepted, codes = parse_receipt(message, size)
                settle(t.stats, t.console, size, accepted, codes)
            else:
                code = str(message.get("code") or "frame_error")
                settle(t.stats, t.console, size, 0, [code] * size)
                t.console.warn(f"frame_{code}", f"the server rejected a frame: {code}")
            self._wake.set()
        elif kind == "hold":
            wait = parse_retry_after(str(message.get("retry_after", "")))
            t.stats.throttled += 1
            self.hold_until = asyncio.get_running_loop().time() + (
                wait if wait is not None else 1.0
            )
            self._wake.set()
        elif kind == "credit":
            self.hold_until = 0.0
            self._wake.set()

    def _lost(self, exc: BaseException) -> float:
        """Book a failed connect or a broken stream, put unacknowledged frames back.

        Returns how long to wait before reconnecting: what a refusing server asked for, or the
        next back-off step.
        """
        t = self._t
        if isinstance(exc, StreamClosedError) and exc.code in AUTH_CLOSE_CODES:
            msg = f"the server closed the stream: {exc}"
            raise FatalError(msg) from exc
        # Unacknowledged frames go first on the next connection, in their original order.
        while self.inflight:
            frame = self.inflight.pop()
            self.carry.appendleft((frame.reports, True))
        self.credit = 0
        if isinstance(exc, aiohttp.WSServerHandshakeError) and exc.status in THROTTLE_HTTP_STATUSES:
            t.stats.throttled += 1
            retry_after = _retry_after_of(exc)
            if retry_after is not None:
                return retry_after * (1.0 + RETRY_AFTER_SPREAD * t.jitter.random())
            return self._backoff.next()
        if isinstance(exc, StreamClosedError) and exc.code == OVERLOADED_CLOSE_CODE:
            t.stats.throttled += 1  # "try again later": shedding, not a failure
            return self._backoff.next()
        kind = error_kind(exc)
        t.stats.error(kind)
        t.console.warn(f"ws_{kind}", f"stream {self.index}: {describe(exc)}; reconnecting")
        return self._backoff.next()


def _grant(message: Mapping[str, Any]) -> int:
    credit = message.get("credit")
    if isinstance(credit, int) and not isinstance(credit, bool) and credit > 0:
        return credit
    return 0


# --- position frames (layout of perimeter/wire/frames.py) ----------------------------------------

FRAME_MAGIC: Final = 0xB7
BUNDLE_MAGIC: Final = 0xB8
FRAME_VERSION: Final = 1
FRAME_LIVE: Final = 1
FRAME_SNAPSHOT: Final = 2
UNKNOWN_U16: Final = 0xFFFF
_FRAME_HEADER: Final = struct.Struct("<BBBBIIQI")
_BUNDLE_HEADER: Final = struct.Struct("<BBH")
_U32: Final = struct.Struct("<I")


class FrameError(ValueError):
    """Bytes that are not a well-formed position frame or bundle."""


class TileFrame(NamedTuple):
    """One decoded tile frame, column by column.

    Layout (little-endian): u8 magic 0xB7, u8 version, u8 kind, u8 zoom, u32 x, u32 y,
    u64 base time (ms), u32 count N; then i32[N] latitude x 1e7, i32[N] longitude x 1e7,
    u32[N] time since base (ms), u16[N] speed (cm/s), u16[N] heading (centidegrees),
    0xFFFF meaning unknown; u32 length and the device ids joined by NUL, padded to 4 bytes.
    """

    kind: int
    zoom: int
    x: int
    y: int
    base_ms: int
    device_ids: list[str]
    lat_e7: array[int]
    lon_e7: array[int]
    offsets_ms: array[int]
    speed_cms: array[int]
    heading_cdeg: array[int]

    def points(self) -> list[tuple[str, float, float, int, float | None, float | None]]:
        """``(device_id, lat, lon, recorded_at_ms, speed_mps, heading_deg)`` per device."""
        return [
            (
                device_id,
                lat / 1e7,
                lon / 1e7,
                self.base_ms + offset,
                None if speed == UNKNOWN_U16 else speed / 100,
                None if heading == UNKNOWN_U16 else heading / 100,
            )
            for device_id, lat, lon, offset, speed, heading in zip(
                self.device_ids,
                self.lat_e7,
                self.lon_e7,
                self.offsets_ms,
                self.speed_cms,
                self.heading_cdeg,
                strict=True,
            )
        ]


def _column(typecode: str, view: memoryview, offset: int, count: int) -> array[int]:
    values: array[int] = array(typecode)
    values.frombytes(view[offset : offset + count * values.itemsize])
    if sys.byteorder == "big":
        values.byteswap()
    return values


def decode_tile(data: bytes | memoryview) -> TileFrame:
    view = memoryview(data)
    if len(view) < _FRAME_HEADER.size:
        msg = "frame shorter than its header"
        raise FrameError(msg)
    magic, version, kind, zoom, x, y, base, count = _FRAME_HEADER.unpack_from(view, 0)
    if magic != FRAME_MAGIC or version != FRAME_VERSION:
        msg = f"unsupported frame magic/version {magic:#x}/{version}"
        raise FrameError(msg)
    offset = _FRAME_HEADER.size
    if len(view) < offset + 16 * count + 4:
        msg = "frame truncated inside its arrays"
        raise FrameError(msg)
    lats = _column("i", view, offset, count)
    lons = _column("i", view, offset + 4 * count, count)
    offsets = _column("I", view, offset + 8 * count, count)
    speeds = _column("H", view, offset + 12 * count, count)
    headings = _column("H", view, offset + 14 * count, count)
    offset += 16 * count
    (ids_length,) = _U32.unpack_from(view, offset)
    offset += 4
    if len(view) < offset + ids_length:
        msg = "frame truncated inside its id blob"
        raise FrameError(msg)
    try:
        ids = bytes(view[offset : offset + ids_length]).decode().split("\x00") if count else []
    except UnicodeDecodeError as exc:
        msg = "device ids are not valid UTF-8"
        raise FrameError(msg) from exc
    if len(ids) != count:
        msg = f"frame declares {count} devices but carries {len(ids)} ids"
        raise FrameError(msg)
    return TileFrame(kind, zoom, x, y, base, ids, lats, lons, offsets, speeds, headings)


def decode_bundle(data: bytes | memoryview) -> list[memoryview]:
    """Split a bundle (u8 0xB8, u8 version, u16 count, then u32 length + frame) into frames."""
    view = memoryview(data)
    if len(view) < _BUNDLE_HEADER.size:
        msg = "bundle shorter than its header"
        raise FrameError(msg)
    magic, version, count = _BUNDLE_HEADER.unpack_from(view, 0)
    if magic != BUNDLE_MAGIC or version != FRAME_VERSION:
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


# --- observe mode --------------------------------------------------------------------------------


class ObserveError(Exception):
    """Observe mode could not sign in, create its zones or open the live channel."""


def plan_zones(
    count: int, center: tuple[float, float], radius_m: float, seed: int
) -> list[dict[str, Any]]:
    """Demo zones scattered over the simulated area (deterministic for a seed)."""
    rng = random.Random(f"{seed}/zones")
    zones = []
    for index in range(count):
        distance = 0.85 * radius_m * math.sqrt(rng.random())
        theta = rng.uniform(0.0, math.tau)
        lat, lon = displace(
            center[0], center[1], distance * math.sin(theta), distance * math.cos(theta)
        )
        zones.append(
            {
                "name": f"Load zone {index + 1:02d}",
                "center": {"lat": round(lat, 6), "lon": round(lon, 6)},
                "radius_m": float(rng.randrange(20, 90) * 10),
                "color": ZONE_COLORS[index % len(ZONE_COLORS)],
                "notify_enter": True,
                "notify_exit": True,
                "dwell_s": 60 if index % 4 == 3 else None,
            }
        )
    return zones


class ZoneProgress:
    """Status lines while demo zones are created or deleted through the API's rate limit."""

    def __init__(self, console: Console, verb: str, total: int, done: Callable[[], int]) -> None:
        self._console = console
        self._verb = verb
        self._total = total
        self._done = done
        self._started = time.monotonic()
        self._shown_at: float | None = None
        self.waits = 0

    def waiting(self, delay: float) -> None:
        """Book a wait of ``delay`` seconds, and say so every ``ZONE_PROGRESS_EVERY_S`` at most."""
        self.waits += 1
        now = time.monotonic()
        if self._shown_at is None or now - self._shown_at >= ZONE_PROGRESS_EVERY_S:
            self._shown_at = now
            self._say(f"; the API limits zone changes, waiting {delay:g} s")

    def finish(self) -> None:
        if self.waits:  # a change that never had to wait is not worth a line
            elapsed = time.monotonic() - self._started
            self._say(f" in {elapsed:.1f} s, after {self.waits:,} waits for the API's limit")

    def _say(self, rest: str) -> None:
        self._console.line(
            f"[{self._console.elapsed():7.1f}s] observe: {self._done():,} of {self._total:,} "
            f"demo zones {self._verb}{rest}"
        )


async def _pause(seconds: float, interrupt: asyncio.Event | None) -> bool:
    """Sleep ``seconds``; ``False`` as soon as ``interrupt`` is set, if there is one."""
    if interrupt is None:
        await asyncio.sleep(seconds)
        return True
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(seconds):
            await interrupt.wait()
    return not interrupt.is_set()


def _iso_to_ms(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return int(stamp.timestamp() * 1000)


class Observer:
    """A live-map client: signs in, adds demo zones, watches the fleet and measures latency."""

    def __init__(
        self,
        config: Config,
        stats: Stats,
        console: Console,
        devices: frozenset[str],
        *,
        interrupt: asyncio.Event,
    ) -> None:
        assert config.observe is not None
        self._config = config
        self._username = config.observe
        self._stats = stats
        self._console = console
        self._devices = devices
        self._interrupt = interrupt  # the run's stop: no zone is created once it is set
        self._session: aiohttp.ClientSession | None = None
        self._token: str | None = None
        self._zones: list[str] = []
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._last_seq = 0
        bbox, zoom = viewport(config.center, config.radius_km * 1000.0)
        self._viewport = json.dumps({"type": "viewport", "bbox": bbox, "zoom": zoom})
        self._closing = asyncio.Event()
        self._backoff = Backoff(RECONNECT_BASE_S, RECONNECT_CAP_S, random.Random())
        self.zones_created = 0
        self.zones_deleted = 0

    @property
    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def open(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(
                total=None, connect=min(self._config.timeout, CONNECT_TIMEOUT_S)
            ),
            headers={"User-Agent": USER_AGENT},
        )
        try:
            async with asyncio.timeout(self._config.timeout):
                await self._sign_in()
            await self._create_zones()
            self._ws = await self._connect()
        except (aiohttp.ClientError, TimeoutError, ValueError, ObserveError, StreamError) as exc:
            msg = f"observe mode: {describe(exc)}"
            raise StartupError(msg) from exc

    async def run(self) -> None:
        """Receive until closed; after a drop, reconnect and resume events where they stopped."""
        while not self._closing.is_set():
            ws = self._ws
            if ws is None:
                try:
                    ws = self._ws = await self._connect()
                except (aiohttp.ClientError, TimeoutError, ValueError, StreamError) as exc:
                    self._stats.error("observe_connect")
                    self._console.warn("observe", f"live channel unavailable: {describe(exc)}")
                    await self._sleep(self._backoff.next())
                    continue
                self._backoff.reset()
            try:
                await self._receive(ws)
            except (aiohttp.ClientError, TimeoutError, ValueError, StreamError) as exc:
                if self._closing.is_set():
                    return
                if isinstance(exc, StreamClosedError) and exc.code in OBSERVER_FINAL_CLOSE_CODES:
                    self._console.warn("observe", f"live channel ended for good: {exc}")
                    return
                self._stats.error("observe_lost")
                self._console.warn("observe", f"live channel lost: {describe(exc)}; reconnecting")
            finally:
                with contextlib.suppress(Exception):
                    await ws.close()
                self._ws = None
            await self._sleep(self._backoff.next())

    async def stop(self) -> None:
        """Stop watching; :meth:`run` returns."""
        self._closing.set()
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()

    async def close(self) -> None:
        """Stop watching, remove the demo zones (unless kept) and sign out. Idempotent."""
        await self.stop()
        session, self._session = self._session, None
        if session is None:
            return
        try:
            if self._token is not None:
                if not self._config.keep_zones:
                    await self._delete_zones(session)
                with contextlib.suppress(aiohttp.ClientError, TimeoutError):
                    async with (
                        asyncio.timeout(self._config.timeout),
                        session.delete(self._url(SESSION_PATH), headers=self._auth),
                    ):
                        pass
        finally:
            await session.close()

    def _url(self, path: str) -> str:
        return self._config.url + path

    async def _sign_in(self) -> None:
        assert self._session is not None
        async with self._session.post(
            self._url(TOKEN_PATH), json={"username": self._username}
        ) as response:
            payload = await response.read()
            if response.status not in (200, 201):
                msg = f"sign-in as {self._username!r} failed: HTTP {response.status}"
                raise ObserveError(msg)
        body = _json_body(payload)
        token = body.get("token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            msg = "the sign-in response carries no token"
            raise ObserveError(msg)
        self._token = token

    async def _create_zones(self) -> None:
        assert self._session is not None
        radius_m = self._config.radius_km * 1000.0
        planned = plan_zones(self._config.zones, self._config.center, radius_m, self._config.seed)
        progress = ZoneProgress(self._console, "created", len(planned), lambda: self.zones_created)
        for spec in planned:
            if self._interrupt.is_set():  # the run ends before it starts; close() tidies up
                break
            status, payload = await self._change_zone(
                self._session, "POST", ZONES_PATH, progress, spec, interrupt=self._interrupt
            )
            if status != 201:
                if self._interrupt.is_set():  # stopped while the API made it wait
                    break
                msg = f"creating zone {spec['name']!r} failed: HTTP {status}"
                raise ObserveError(msg)
            body = _json_body(payload)
            zone_id = body.get("id") if isinstance(body, dict) else None
            if not isinstance(zone_id, str):
                msg = "the zone response carries no id"
                raise ObserveError(msg)
            self._zones.append(zone_id)
            self.zones_created += 1
        progress.finish()

    async def _delete_zones(self, session: aiohttp.ClientSession) -> None:
        progress = ZoneProgress(
            self._console, "deleted", len(self._zones), lambda: self.zones_deleted
        )
        for zone_id in self._zones:
            try:
                status, _ = await self._change_zone(
                    session, "DELETE", f"{ZONES_PATH}/{zone_id}", progress
                )
            except (aiohttp.ClientError, TimeoutError) as exc:
                self._console.warn("zones", f"could not delete a demo zone: {describe(exc)}")
                continue
            if status in (204, 404):
                self.zones_deleted += 1
            else:
                self._console.warn("zones", f"could not delete a demo zone: HTTP {status}")
        progress.finish()

    async def _change_zone(
        self,
        session: aiohttp.ClientSession,
        method: str,
        path: str,
        progress: ZoneProgress,
        spec: dict[str, Any] | None = None,
        *,
        interrupt: asyncio.Event | None = None,
    ) -> tuple[int, bytes]:
        """Status and body of one zone change, sent again while the API says when to retry.

        Zone changes are rate limited per account on every API replica, so creating or deleting
        many demo zones soon meets a 429 with ``Retry-After`` (or a 503 with one, while the
        database is busy): that pause is kept, ``ZONE_ATTEMPTS`` times at most. Once ``interrupt``
        is set nothing waits any more, and the last answer is returned.
        """
        attempt = 0
        while True:
            attempt += 1
            async with (
                asyncio.timeout(self._config.timeout),
                session.request(method, self._url(path), json=spec, headers=self._auth) as response,
            ):
                payload = await response.read()
                status = response.status
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
            if status not in THROTTLE_HTTP_STATUSES or retry_after is None:
                return status, payload
            if attempt == ZONE_ATTEMPTS:
                return status, payload
            progress.waiting(retry_after)
            if not await _pause(retry_after, interrupt):
                return status, payload

    async def _connect(self) -> aiohttp.ClientWebSocketResponse:
        assert self._session is not None
        assert self._token is not None
        params = {"resume_after": str(self._last_seq)} if self._last_seq else {}
        async with asyncio.timeout(self._config.timeout):
            ws = await self._session.ws_connect(
                _websocket_url(self._config.url, LIVE_PATH),
                params=params,
                headers=self._auth,
                heartbeat=WS_HEARTBEAT_S,
                max_msg_size=LIVE_MAX_MESSAGE,
                timeout=aiohttp.ClientWSTimeout(ws_close=WS_CLOSE_TIMEOUT_S),
            )
            try:
                message = await ws.receive()
                if message.type is not aiohttp.WSMsgType.TEXT:
                    raise StreamClosedError(ws.close_code, "before the hello message")
                hello = json.loads(message.data)
                if not isinstance(hello, dict) or hello.get("type") != "hello":
                    msg = "the live channel did not start with a hello message"
                    raise ProtocolError(msg)
                await ws.send_str(self._viewport)
            except BaseException:
                await ws.close()
                raise
        return ws

    async def _receive(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        while True:
            message = await ws.receive()
            if message.type is aiohttp.WSMsgType.BINARY:
                self._positions(message.data)
            elif message.type is aiohttp.WSMsgType.TEXT:
                if self._text(json.loads(message.data)):
                    await ws.send_str(self._viewport)
            elif message.type is aiohttp.WSMsgType.ERROR:
                raise StreamClosedError(ws.close_code, describe(ws.exception() or Exception()))
            else:
                raise StreamClosedError(ws.close_code, "live channel closed")

    def _positions(self, data: bytes) -> None:
        now_ms = time.time_ns() // 1_000_000
        stats, ours = self._stats, self._devices
        try:
            frames = [decode_tile(raw) for raw in decode_bundle(data)]
        except FrameError as exc:
            stats.error("bad_frame")
            self._console.warn("bad_frame", f"undecodable position bundle: {exc}")
            return
        for frame in frames:
            if frame.kind != FRAME_LIVE:
                stats.snapshot_positions += len(frame.device_ids)
                continue
            base = frame.base_ms
            for device_id, offset in zip(frame.device_ids, frame.offsets_ms, strict=True):
                if device_id in ours:
                    stats.positions += 1
                    stats.e2e.record(float(now_ms - base - offset))

    def _text(self, message: Any) -> bool:
        """Book one text message; ``True`` when the viewport must be sent again."""
        if not isinstance(message, dict):
            return False
        kind = message.get("type")
        if kind == "event":
            self._event(message)
        elif kind == "pulse":
            self._stats.pulses += 1
        elif kind == "resync":
            return True
        return False

    def _event(self, message: dict[str, Any]) -> None:
        seq = message.get("seq")
        if isinstance(seq, int):
            if seq <= self._last_seq:
                return  # replayed after a reconnect and already counted
            self._last_seq = seq
        event = message.get("event")
        if not isinstance(event, dict) or event.get("type") != "alert":
            self._stats.events += 1
            return
        data = event.get("data")
        data = data if isinstance(data, dict) else {}
        self._stats.alerts[str(data.get("kind", "unknown"))] += 1
        occurred = _iso_to_ms(data.get("occurred_at"))
        if occurred is not None:
            self._stats.alert_latency.record(float(time.time_ns() // 1_000_000 - occurred))

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(seconds):
                await self._closing.wait()


# --- configuration -------------------------------------------------------------------------------

USERNAME_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9_.-]{1,31}\Z")
ID_PREFIX_PATTERN: Final = re.compile(r"^[A-Za-z0-9_-]{0,32}\Z")


@dataclass(frozen=True, slots=True, kw_only=True)
class Config:
    """Everything one run needs. :func:`parse_config` builds it from flags and environment."""

    url: str = "http://localhost:8080"
    token: str = ""
    transport: TransportName = "http"
    devices: int = 10_000
    interval: float = 3.0
    jitter: float = 0.3
    ramp: float = 15.0
    duration: float = 300.0
    connections: int = 32
    batch: int = 250
    linger_ms: float = 100.0
    buffer: int = 100_000
    retries: int = 4
    timeout: float = 10.0
    center: tuple[float, float] = (52.3676, 4.9041)
    radius_km: float = 12.0
    mix: Mapping[str, float] = field(default_factory=lambda: dict(DEFAULT_MIX))
    stationary: float = 0.05
    noise_m: float = 4.0
    seed: int = 0
    id_prefix: str = ""
    observe: str | None = None
    zones: int = 0
    keep_zones: bool = False
    report_every: float = 5.0
    json_path: Path | None = None

    def __post_init__(self) -> None:
        problems = list(self._problems())
        if problems:
            raise ValueError("; ".join(problems))

    def _problems(self) -> Iterator[str]:
        if self.transport not in ("http", "ws"):
            yield "transport must be http or ws"
        if not self.token:
            yield (
                "an ingest token is required "
                "(--token, --token-file, GENERATOR_TOKEN or GENERATOR_TOKEN_FILE)"
            )
        if self.devices < 1 or self.connections < 1 or self.batch < 1:
            yield "devices, connections and batch must be at least 1"
        if self.interval <= 0 or not 0 <= self.jitter < 1:
            yield "interval must be positive and jitter within [0, 1)"
        if self.buffer < self.batch:
            yield "--buffer must hold at least one batch"
        if self.zones and not self.observe:
            yield "--zones needs --observe"
        unknown = sorted(set(self.mix) - set(PROFILES))
        if unknown:
            yield f"unknown movement profiles: {', '.join(unknown)}"
        if any(weight < 0 for weight in self.mix.values()) or sum(self.mix.values()) <= 0:
            yield "profile shares must be non-negative with a positive total"
        yield from self._area_problems()
        width = max(5, len(str(self.devices - 1)))
        longest = len(self.id_prefix) + max(len(p.tag) for p in PROFILES.values()) + 1 + width
        if longest > 64:
            yield "device ids would be longer than 64 characters: shorten --id-prefix"
        parts = urlsplit(self.url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            yield f"--url must be an http(s) URL, got {self.url!r}"

    def _area_problems(self) -> Iterator[str]:
        lat, lon = self.center
        radius_m = self.radius_km * 1000.0
        if abs(lat) > MAX_LATITUDE or abs(lon) > 180:
            yield f"the centre {lat},{lon} is outside the supported range"
            return
        dlat = radius_m / degree_lengths(lat)[0]
        if abs(lat) + dlat > MAX_LATITUDE:
            yield f"the simulated area must stay within {MAX_LATITUDE:g} degrees of the equator"
            return
        dlon = radius_m / degree_lengths(abs(lat) + dlat)[1]
        if abs(lon) + dlon > 180:
            yield "the simulated area must not cross the antimeridian"
        if radius_m / 2 < max(p.edge_margin for p in PROFILES.values()):
            yield "--radius-km is too small for the movement profiles"

    @property
    def expected_rate(self) -> float:
        return self.devices / self.interval


@dataclass(frozen=True, slots=True)
class Option:
    """One command-line flag, its ``GENERATOR_*`` twin and how to read either of them."""

    flag: str
    dest: str
    parse: Callable[[str], Any]
    help: str
    metavar: str | None = None
    boolean: bool = False
    shown_default: str | None = None

    @property
    def env(self) -> str:
        return ENV_PREFIX + self.flag.removeprefix("--").replace("-", "_").upper()


def _integer(low: int, high: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            value = int(text.strip())
        except ValueError:
            msg = f"{text!r} is not a whole number"
            raise argparse.ArgumentTypeError(msg) from None
        if not low <= value <= high:
            msg = f"must be between {low:,} and {high:,}, got {value:,}"
            raise argparse.ArgumentTypeError(msg)
        return value

    return parse


def _number(low: float, high: float) -> Callable[[str], float]:
    def parse(text: str) -> float:
        try:
            value = float(text.strip())
        except ValueError:
            msg = f"{text!r} is not a number"
            raise argparse.ArgumentTypeError(msg) from None
        if not (math.isfinite(value) and low <= value <= high):
            msg = f"must be between {low:g} and {high:g}, got {text.strip()}"
            raise argparse.ArgumentTypeError(msg)
        return value

    return parse


def _center(text: str) -> tuple[float, float]:
    try:
        lat_text, lon_text = text.split(",")
        lat, lon = float(lat_text), float(lon_text)
    except ValueError:
        msg = f"expected LAT,LON such as 52.3676,4.9041, got {text!r}"
        raise argparse.ArgumentTypeError(msg) from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        msg = f"{text!r} is not a valid latitude,longitude"
        raise argparse.ArgumentTypeError(msg)
    return lat, lon


def _mix(text: str) -> dict[str, float]:
    shares: dict[str, float] = {}
    for part in text.split(","):
        name, _, weight = part.partition("=")
        name = name.strip().lower()
        if name not in PROFILES:
            msg = f"unknown profile {name!r} (known: {', '.join(PROFILES)})"
            raise argparse.ArgumentTypeError(msg)
        try:
            shares[name] = float(weight)
        except ValueError:
            msg = f"expected {name}=WEIGHT, got {part.strip()!r}"
            raise argparse.ArgumentTypeError(msg) from None
    total = sum(shares.values())
    if any(not math.isfinite(w) or w < 0 for w in shares.values()) or total <= 0:
        msg = "weights must be non-negative numbers with a positive total"
        raise argparse.ArgumentTypeError(msg)
    return {name: weight / total for name, weight in shares.items()}


def _url(text: str) -> str:
    parts = urlsplit(text.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        msg = f"expected an http:// or https:// URL, got {text!r}"
        raise argparse.ArgumentTypeError(msg)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _transport(text: str) -> str:
    value = text.strip().lower()
    if value not in ("http", "ws"):
        msg = f"expected http or ws, got {text!r}"
        raise argparse.ArgumentTypeError(msg)
    return value


def _username(text: str) -> str:
    value = text.strip().lower()
    if not USERNAME_PATTERN.match(value):
        msg = "usernames are 2-32 characters: letters, digits and _ . - (starting alphanumeric)"
        raise argparse.ArgumentTypeError(msg)
    return value


def _id_prefix(text: str) -> str:
    if not ID_PREFIX_PATTERN.match(text):
        msg = "up to 32 characters from A-Z a-z 0-9 _ -"
        raise argparse.ArgumentTypeError(msg)
    return text


def _secret(text: str) -> str:
    if not text.strip():
        msg = "must not be empty"
        raise argparse.ArgumentTypeError(msg)
    return text.strip()


def _path(text: str) -> Path:
    return Path(text).expanduser()


def _flag(text: str) -> bool:
    value = text.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    msg = f"expected true/false, got {text!r}"
    raise argparse.ArgumentTypeError(msg)


OPTIONS: Final = (
    Option("--url", "url", _url, "base URL of the API (the edge proxy of the stack)", "URL"),
    Option(
        "--token",
        "token",
        _secret,
        "ingest token, sent as a bearer token",
        "TOKEN",
        shown_default="none",
    ),
    Option(
        "--token-file",
        "token_file",
        _path,
        "read the ingest token from a file",
        "PATH",
        shown_default="none",
    ),
    Option(
        "--transport",
        "transport",
        _transport,
        "http: batched POST requests; ws: WebSocket stream with credit flow control",
        "{http,ws}",
    ),
    Option("--devices", "devices", _integer(1, 1_000_000), "number of simulated devices", "N"),
    Option(
        "--interval", "interval", _number(0.05, 3_600), "seconds between reports of a device", "S"
    ),
    Option(
        "--jitter",
        "jitter",
        _number(0, 0.9),
        "random spread of every interval, as a fraction (0.3 = +/-30%)",
        "F",
    ),
    Option(
        "--ramp",
        "ramp",
        _number(0, 3_600),
        "seconds over which devices start reporting (at least one interval)",
        "S",
    ),
    Option(
        "--duration", "duration", _number(0, 1e9), "seconds to run; 0 runs until interrupted", "S"
    ),
    Option(
        "--connections",
        "connections",
        _integer(1, 1_024),
        "parallel HTTP connections or WebSockets",
        "N",
    ),
    Option("--batch", "batch", _integer(1, 10_000), "reports per request or frame", "N"),
    Option(
        "--linger-ms",
        "linger_ms",
        _number(0, 10_000),
        "longest a report waits for its batch to fill",
        "MS",
    ),
    Option(
        "--buffer",
        "buffer",
        _integer(1, 100_000_000),
        "reports kept while the server is slow; beyond it the oldest are dropped",
        "N",
    ),
    Option(
        "--retries",
        "retries",
        _integer(0, 100),
        "retries of a throttled or failed batch before it is dropped",
        "N",
    ),
    Option(
        "--timeout",
        "timeout",
        _number(0.1, 600),
        "seconds allowed for a connection, a response or an acknowledgement",
        "S",
    ),
    Option("--center", "center", _center, "centre of the simulated area", "LAT,LON"),
    Option("--radius-km", "radius_km", _number(1, 500), "radius of the simulated area", "KM"),
    Option(
        "--mix",
        "mix",
        _mix,
        "relative shares of the movement profiles",
        "vehicle=W,cyclist=W,pedestrian=W",
    ),
    Option("--stationary", "stationary", _number(0, 1), "share of devices that never move", "F"),
    Option("--noise-m", "noise_m", _number(0, 1_000), "GPS noise (standard deviation), m", "M"),
    Option(
        "--seed",
        "seed",
        _integer(0, 2**63 - 1),
        "random seed: the same seed replays the same fleet",
        "N",
        shown_default="random, printed at start",
    ),
    Option(
        "--id-prefix",
        "id_prefix",
        _id_prefix,
        "prefix for device ids, to run several generators side by side",
        "TEXT",
    ),
    Option(
        "--observe",
        "observe",
        _username,
        "sign in as USERNAME and watch the live channel: end-to-end latency and alerts",
        "USERNAME",
    ),
    Option("--zones", "zones", _integer(0, 1_000), "demo zones to create (needs --observe)", "N"),
    Option("--keep-zones", "keep_zones", _flag, "keep the demo zones after the run", boolean=True),
    Option(
        "--report-every",
        "report_every",
        _number(0, 3_600),
        "seconds between status lines; 0 prints only the summary",
        "S",
    ),
    Option(
        "--json",
        "json_path",
        _path,
        "also write the summary as JSON to this file ('-': standard output; the report then goes "
        "to standard error)",
        "PATH",
    ),
)

_EPILOG: Final = f"""\
environment:
  Every option can be set with a GENERATOR_* variable named after the flag, for example
  GENERATOR_DEVICES=10000 or GENERATOR_TOKEN_FILE=/run/secrets/ingest_token. Booleans take
  true/false. A flag wins over the environment; setting both GENERATOR_TOKEN and
  GENERATOR_TOKEN_FILE is an error.

exit status:
  {EXIT_OK}    the run completed (duration reached, or stopped with Ctrl-C / SIGTERM)
  {EXIT_FATAL}    aborted: the server rejected the client mid-run (credentials, wrong URL)
  2    invalid arguments
  {EXIT_UNREACHABLE}    the target was unreachable or refused the token at start
  {EXIT_FORCED}  interrupted twice, without waiting for in-flight reports

examples:
  uv run generator.py --url http://localhost:8080 --token-file .secrets/ingest_token
  uv run generator.py --transport ws --devices 20000 --interval 2 --duration 120
  uv run generator.py --observe demo --zones 20 --json summary.json
"""


def _shown_default(option: Option) -> str:
    if option.shown_default is not None:
        return option.shown_default
    value = _DEFAULTS[option.dest]
    if isinstance(value, bool):
        return "on" if value else "off"
    if value is None or value == "":
        return "none"
    if option.dest == "center":
        return ",".join(f"{part:g}" for part in value)
    if option.dest == "mix":
        return ",".join(f"{name}={share:g}" for name, share in value.items())
    return f"{value:g}" if isinstance(value, float) else str(value)


def _config_defaults() -> dict[str, Any]:
    defaults: dict[str, Any] = {}
    for item in dataclasses.fields(Config):
        if item.default is not dataclasses.MISSING:
            defaults[item.name] = item.default
        elif item.default_factory is not dataclasses.MISSING:
            defaults[item.name] = item.default_factory()
    return defaults


_DEFAULTS: Final = _config_defaults()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="generator.py",
        description=(
            "Simulate a fleet of moving devices reporting their positions to Perimeter's ingest\n"
            "API, honouring its backpressure, and optionally watch the live channel to measure\n"
            "end-to-end latency and count alerts."
        ),
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        argument_default=argparse.SUPPRESS,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION} ({RUNTIME})")
    for option in OPTIONS:
        text = f"{option.help} [default: {_shown_default(option)}; env {option.env}]"
        text = text.replace("%", "%%")
        if option.boolean:
            parser.add_argument(
                option.flag, dest=option.dest, action=argparse.BooleanOptionalAction, help=text
            )
        else:
            parser.add_argument(
                option.flag, dest=option.dest, type=option.parse, metavar=option.metavar, help=text
            )
    return parser


def parse_config(
    argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None
) -> Config:
    """Flags first, then ``GENERATOR_*`` variables, then built-in defaults."""
    environ = os.environ if environ is None else environ
    parser = build_parser()
    given = vars(parser.parse_args(argv))
    values: dict[str, Any] = {}
    for option in OPTIONS:
        if option.dest in given:
            values[option.dest] = given[option.dest]
            continue
        raw = environ.get(option.env)
        if raw is None or not raw.strip():
            continue
        try:
            values[option.dest] = option.parse(raw)
        except (ValueError, argparse.ArgumentTypeError) as exc:
            parser.error(f"{option.env}: {exc}")
    _resolve_token(parser, given, values)
    values.setdefault("seed", secrets.randbelow(2**32))
    try:
        return Config(**values)
    except ValueError as exc:
        parser.error(str(exc))


def _resolve_token(
    parser: argparse.ArgumentParser, given: Mapping[str, Any], values: dict[str, Any]
) -> None:
    """``--token`` and ``--token-file`` exclude each other; a flag beats either variable."""
    if "token" in given and "token_file" in given:
        parser.error("use either --token or --token-file, not both")
    if "token" in given:
        values.pop("token_file", None)
    elif "token_file" in given:
        values.pop("token", None)
    elif "token" in values and "token_file" in values:
        parser.error("set either GENERATOR_TOKEN or GENERATOR_TOKEN_FILE, not both")
    path = values.pop("token_file", None)
    if path is None:
        return
    try:
        token = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        parser.error(f"cannot read the token file {str(path)!r}: {exc.strerror}")
    if not token:
        parser.error(f"the token file {str(path)!r} is empty")
    values["token"] = token


# --- the run -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunResult:
    exit_code: int
    summary: dict[str, Any]


def _fmt_ms(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.1f}" if value < 10 else f"{value:.0f}"


class LoadRun:
    """One run: preflight, the fleet clock, senders, observer, status lines and the summary."""

    def __init__(
        self, config: Config, console: Console, stop: asyncio.Event, force: asyncio.Event
    ) -> None:
        self.config = config
        self.console = console
        self.stop = stop
        self.force = force
        self.stats = Stats()
        rng = random.Random(config.seed)
        mobility = Mobility(
            center=config.center,
            radius_m=config.radius_km * 1000.0,
            noise_m=config.noise_m,
            rng=rng,
        )
        self.fleet = Fleet(
            count=config.devices,
            interval=config.interval,
            jitter=config.jitter,
            ramp=config.ramp,
            mix=config.mix,
            stationary=config.stationary,
            id_prefix=config.id_prefix,
            mobility=mobility,
            rng=rng,
        )
        self.queue = BatchQueue(config.buffer, self.stats)
        self.transport: Transport = (
            HttpTransport(config, self.queue, self.stats, console)
            if config.transport == "http"
            else WsTransport(config, self.queue, self.stats, console)
        )
        self.observer = (
            Observer(config, self.stats, console, frozenset(self.fleet.ids), interrupt=stop)
            if config.observe
            else None
        )
        self._halt = asyncio.Event()
        self._reported: set[asyncio.Task[None]] = set()
        self._fatal = False
        self._started_at = datetime.now(UTC)
        self._t0 = 0.0
        self._wall0_ms = 0
        self._cpu0 = time.process_time()
        self._active = 0.0
        self._elapsed = 0.0

    async def execute(self) -> RunResult:
        for line in self._header():
            self.console.line(line)
        try:
            try:
                await self.transport.open()
                if self.observer is not None:
                    await self.observer.open()
            except StartupError as exc:
                self.console.error(str(exc))
                code, reason = EXIT_UNREACHABLE, "unreachable"
            else:
                code, reason = await self._run()
        finally:
            await self.transport.close()
            if self.observer is not None:
                await self.observer.close()
        return self._finish(code, reason)

    async def _run(self) -> tuple[int, str]:
        loop = asyncio.get_running_loop()
        self._t0 = loop.time()
        self._wall0_ms = time.time_ns() // 1_000_000
        self._cpu0 = time.process_time()
        clock = asyncio.create_task(self._drive(), name="fleet clock")
        lanes = [
            asyncio.create_task(lane, name=f"{self.config.transport} lane {index}")
            for index, lane in enumerate(self.transport.lanes())
        ]
        observing = (
            asyncio.create_task(self.observer.run(), name="observer")
            if self.observer is not None
            else None
        )
        reporter = (
            asyncio.create_task(self._report(), name="status")
            if self.config.report_every > 0
            else None
        )
        try:
            reason = await self._supervise(clock, lanes)
            self._active = loop.time() - self._t0
            forced = await self._shutdown(clock, lanes, observing)
        finally:
            if reporter is not None:
                reporter.cancel()
                await asyncio.gather(reporter, return_exceptions=True)
            self._elapsed = loop.time() - self._t0
        if forced:
            return EXIT_FORCED, "forced"
        if self._fatal:
            return EXIT_FATAL, "fatal"
        return EXIT_OK, reason

    async def _supervise(self, clock: asyncio.Task[None], lanes: list[asyncio.Task[None]]) -> str:
        """Wait for the duration to pass, a stop request, or a task that gives up."""
        stopper = asyncio.create_task(self.stop.wait())
        try:
            done, _ = await asyncio.wait(
                {stopper, clock, *lanes},
                timeout=self.config.duration or None,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            stopper.cancel()
        for task in (clock, *lanes):
            if task in done:
                self._report_failure(task)
        return "interrupted" if stopper in done else "duration"

    def _report_failure(self, task: asyncio.Task[None]) -> None:
        """Print why a clock or lane task ended; any such ending makes the run fail."""
        if task in self._reported or task.cancelled():
            return
        self._reported.add(task)
        error = task.exception()
        if error is None:
            return
        self._fatal = True
        if isinstance(error, FatalError):
            self.console.error(str(error))
        else:
            self.console.error(f"{task.get_name()} stopped unexpectedly")
            self.console.exception(error)

    async def _drive(self) -> None:
        """The fleet clock: emits due reports every tick and seals them into batches."""
        loop = asyncio.get_running_loop()
        fleet, queue, stats = self.fleet, self.queue, self.stats
        size = self.config.batch
        linger = self.config.linger_ms / 1000.0
        pending: Batch = []
        pending_since = 0.0
        next_tick = loop.time()
        while not self._halt.is_set():
            now = loop.time() - self._t0
            before = len(pending)
            oldest = fleet.emit(now, self._wall0_ms, pending, MAX_SLICE)
            produced = len(pending) - before
            if oldest is not None:
                stats.offered += produced
                # Lateness beyond the clock's own resolution: ~0 while it keeps up.
                stats.lag.record(max(now - oldest - TICK_S, 0.0) * 1000.0)
                if not before:
                    pending_since = oldest
                while len(pending) >= size:
                    queue.put(pending[:size])
                    del pending[:size]
                    pending_since = now
            if pending and now - pending_since >= linger:
                queue.put(pending)
                pending = []
            if produced >= MAX_SLICE:
                await asyncio.sleep(0)  # more is due: let the senders run, then continue
                continue
            next_tick += TICK_S
            delay = next_tick - loop.time()
            if delay < 0:
                next_tick = loop.time()
                delay = 0.0
            await asyncio.sleep(delay)
        if pending:
            queue.put(pending)

    async def _shutdown(
        self,
        clock: asyncio.Task[None],
        lanes: list[asyncio.Task[None]],
        observing: asyncio.Task[None] | None,
    ) -> bool:
        """Stop the clock, give the senders the grace period to drain, account for the rest.

        Returns ``True`` when a second interrupt cut the grace period short.
        """
        loop = asyncio.get_running_loop()
        self._halt.set()
        await asyncio.gather(clock, return_exceptions=True)
        self.queue.close()
        deadline = loop.time() + self.config.timeout
        self.transport.drain.begin(deadline)
        forced = await self._until(deadline, lanes)
        for task in lanes:
            if task.done():
                self._report_failure(task)
            else:
                task.cancel()
        await asyncio.gather(*lanes, return_exceptions=True)
        leftover = self.queue.clear() + self.transport.in_flight() + self.transport.waiting()
        self.stats.drop(leftover, "shutdown")
        if observing is not None and self.observer is not None:
            if not forced:  # trailing positions and alerts are still on their way
                forced = await self._until(loop.time() + OBSERVE_TAIL_S)
            await self.observer.stop()
            await asyncio.gather(observing, return_exceptions=True)
        return forced

    async def _until(self, deadline: float, tasks: Sequence[asyncio.Task[None]] = ()) -> bool:
        """Wait until all ``tasks`` are done (without tasks: until ``deadline``).

        Returns ``True`` if a forced stop arrived first.
        """
        forcing = asyncio.create_task(self.force.wait())
        try:
            pending: set[asyncio.Task[Any]] = {task for task in tasks if not task.done()}
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0 or (tasks and not pending):
                    return False
                done, pending = await asyncio.wait(
                    {forcing, *pending}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
                )
                if forcing in done:
                    return True
                pending.discard(forcing)
        finally:
            forcing.cancel()

    async def _report(self) -> None:
        loop = asyncio.get_running_loop()
        every = self.config.report_every
        stats = self.stats
        last_time, last_cpu = loop.time(), time.process_time()
        last = (stats.offered, stats.sent, stats.accepted)
        while True:
            await asyncio.sleep(every - (loop.time() - self._t0) % every)
            now, cpu = loop.time(), time.process_time()
            span = max(now - last_time, 1e-9)
            current = (stats.offered, stats.sent, stats.accepted)
            offered, sent, accepted = ((a - b) / span for a, b in zip(current, last, strict=True))
            ack = stats.ack.roll()
            e2e = stats.e2e.roll()
            lag = stats.lag.roll()
            stats.alert_latency.roll()
            parts = [
                f"[{now - self._t0:7.1f}s]",
                f"offered {offered:7,.0f}/s",
                f"sent {sent:7,.0f}/s",
                f"accepted {accepted:7,.0f}/s",
                f"rejected {stats.rejected:,}",
                f"throttled {stats.throttled:,}",
                f"dropped {stats.dropped_total:,}",
                f"errors {stats.errors.total():,}",
                f"in-flight {self.transport.in_flight():,}",
                f"queued {self.queue.size + self.transport.waiting():,}",
                f"ack p50/p95/p99 {_fmt_ms(ack[0])}/{_fmt_ms(ack[1])}/{_fmt_ms(ack[2])} ms",
            ]
            if self.observer is not None:
                parts.append(f"e2e p50/p99 {_fmt_ms(e2e[0])}/{_fmt_ms(e2e[2])} ms")
                parts.append(f"alerts {stats.alerts.total():,}")
            parts.append(f"lag p99 {_fmt_ms(lag[2])} ms")
            parts.append(f"cpu {100.0 * (cpu - last_cpu) / span:.0f}%")
            self.console.line("  ".join(parts))
            last_time, last_cpu, last = now, cpu, current

    def _header(self) -> list[str]:
        config = self.config
        lat, lon = config.center
        mix = ", ".join(f"{name} {share:.0%}" for name, share in config.mix.items() if share > 0)
        duration = f"{config.duration:g} s" if config.duration else "until interrupted"
        lines = [
            f"perimeter load generator {VERSION} ({RUNTIME})",
            f"  target    {config.url} ({config.transport}, {config.connections} connections, "
            f"batches of {config.batch})",
            f"  fleet     {config.devices:,} devices within {config.radius_km:g} km of "
            f"{lat:.5f},{lon:.5f}: {mix}; {config.stationary:.0%} stationary",
            f"  schedule  every {config.interval:g} s +/-{config.jitter:.0%} "
            f"(~{config.expected_rate:,.0f} reports/s), ramp {config.ramp:g} s, {duration}, "
            f"seed {config.seed}",
        ]
        if config.observe:
            zones = f" with {config.zones} demo zones" if config.zones else ""
            lines.append(f"  observe   as {config.observe!r}{zones}")
        return lines

    def _finish(self, code: int, reason: str) -> RunResult:
        summary = self._summary(code, reason)
        for line in render_summary(summary):
            self.console.line(line)
        if self.config.json_path is not None:
            try:
                write_json(self.config.json_path, summary)
            except OSError as exc:
                self.console.error(f"cannot write {self.config.json_path}: {exc.strerror}")
                if code == EXIT_OK:
                    code = EXIT_FATAL
                    summary["exit_code"] = code
        return RunResult(code, summary)

    def _summary(self, code: int, reason: str) -> dict[str, Any]:
        stats, config = self.stats, self.config
        for series in (stats.ack, stats.e2e, stats.lag, stats.alert_latency):
            series.roll()
        elapsed, active = self._elapsed, self._active
        cpu = time.process_time() - self._cpu0
        summary: dict[str, Any] = {
            "generator": VERSION,
            "runtime": RUNTIME,
            "started_at": self._started_at.isoformat(timespec="milliseconds"),
            "ended_at": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "active_s": round(active, 3),
            "elapsed_s": round(elapsed, 3),
            "stop_reason": reason,
            "exit_code": code,
            "config": {
                "url": config.url,
                "transport": config.transport,
                "devices": config.devices,
                "interval_s": config.interval,
                "jitter": config.jitter,
                "ramp_s": config.ramp,
                "duration_s": config.duration,
                "connections": config.connections,
                "batch": config.batch,
                "center": list(config.center),
                "radius_km": config.radius_km,
                "mix": dict(config.mix),
                "stationary": config.stationary,
                "noise_m": config.noise_m,
                "seed": config.seed,
                "observe": config.observe,
                "zones": config.zones,
            },
            "reports": {
                "offered": stats.offered,
                "sent": stats.sent,
                "resent": stats.resent,
                "accepted": stats.accepted,
                "rejected": stats.rejected,
                "dropped": stats.dropped_total,
                "unsettled": stats.unsettled,
            },
            "rates_per_s": {
                "offered": round(stats.offered / active, 1) if active else 0.0,
                "accepted": round(stats.accepted / active, 1) if active else 0.0,
            },
            "dropped_by_reason": dict(stats.dropped),
            "rejected_by_code": dict(stats.rejected_codes),
            "throttled": stats.throttled,
            "errors": stats.errors.total(),
            "errors_by_kind": dict(stats.errors),
            "connections_opened": stats.connections_opened,
            "ack_latency_ms": stats.ack.total.summary(),
            "scheduler_lag_ms": stats.lag.total.summary(),
            "process": {
                "cpu_s": round(cpu, 2),
                "cpu_percent": round(100.0 * cpu / elapsed, 1) if elapsed else None,
                "max_rss_mb": _max_rss_mb(),
            },
        }
        if self.observer is not None:
            summary["observe"] = {
                "username": config.observe,
                "positions": stats.positions,
                "snapshot_positions": stats.snapshot_positions,
                "e2e_latency_ms": stats.e2e.total.summary(),
                "alerts": dict(stats.alerts),
                "alert_latency_ms": stats.alert_latency.total.summary(),
                "other_events": stats.events,
                "pulses": stats.pulses,
                "zones_created": self.observer.zones_created,
                "zones_deleted": self.observer.zones_deleted,
            }
        return summary


def _max_rss_mb() -> float | None:
    try:
        import resource  # noqa: PLC0415 - POSIX only
    except ImportError:  # pragma: no cover - Windows
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(peak / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)


def render_summary(summary: Mapping[str, Any]) -> list[str]:
    reports = summary["reports"]
    rates = summary["rates_per_s"]
    config = summary["config"]
    process = summary["process"]

    def latency(values: Mapping[str, Any]) -> str:
        if not values["count"]:
            return "-"
        return (
            f"p50 {_fmt_ms(values['p50'])} ms, p95 {_fmt_ms(values['p95'])} ms, "
            f"p99 {_fmt_ms(values['p99'])} ms, max {_fmt_ms(values['max'])} ms "
            f"({values['count']:,} samples)"
        )

    def listing(values: Mapping[str, int]) -> str:
        return ", ".join(f"{key} {count:,}" for key, count in sorted(values.items()))

    def breakdown(values: Mapping[str, int]) -> str:
        return f" ({listing(values)})" if values else ""

    balance = "balanced" if reports["unsettled"] == 0 else f"UNSETTLED {reports['unsettled']:,}"
    lines = [
        "",
        "summary " + "-" * 72,
        f"  run        {summary['active_s']:.1f} s active, {summary['elapsed_s']:.1f} s in all, "
        f"ended by {summary['stop_reason']}, exit code {summary['exit_code']}, "
        f"seed {config['seed']}",
        f"  reports    offered {reports['offered']:,}, accepted {reports['accepted']:,}, "
        f"rejected {reports['rejected']:,}, dropped {reports['dropped']:,} ({balance})",
        f"  sending    sent {reports['sent']:,}, resent {reports['resent']:,}, "
        f"throttled {summary['throttled']:,}, errors {summary['errors']:,}"
        f"{breakdown(summary['errors_by_kind'])}",
        f"  rate       offered {rates['offered']:,.0f}/s, accepted {rates['accepted']:,.0f}/s",
    ]
    if summary["rejected_by_code"]:
        lines.append(f"  rejected   {listing(summary['rejected_by_code'])}")
    if summary["dropped_by_reason"]:
        lines.append(f"  dropped    {listing(summary['dropped_by_reason'])}")
    lines.append(f"  ack        {latency(summary['ack_latency_ms'])}")
    observe = summary.get("observe")
    if observe is not None:
        alerts = observe["alerts"]
        lines.append(
            f"  e2e        {latency(observe['e2e_latency_ms'])}; "
            f"{observe['snapshot_positions']:,} snapshot positions"
        )
        lines.append(
            f"  alerts     {sum(alerts.values()):,}{breakdown(alerts)}; "
            f"latency {latency(observe['alert_latency_ms'])}"
        )
        if observe.get("zones_created"):
            lines.append(
                f"  zones      {observe['zones_created']:,} created, "
                f"{observe['zones_deleted']:,} deleted"
            )
    cpu = process["cpu_percent"]
    rss = process["max_rss_mb"]
    lines.append(
        f"  generator  cpu {'-' if cpu is None else f'{cpu:.1f}%'}, "
        f"peak memory {'-' if rss is None else f'{rss:.0f} MB'}, "
        f"scheduler lag {latency(summary['scheduler_lag_ms'])}"
    )
    return lines


def write_json(path: Path, summary: Mapping[str, Any]) -> None:
    """Write atomically, so a reader never sees half a summary (``-``: standard output)."""
    if path == JSON_TO_STDOUT:
        sys.stdout.write(json.dumps(summary, indent=2) + "\n")
        sys.stdout.flush()
        return
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


async def run(
    config: Config,
    *,
    stop: asyncio.Event | None = None,
    force: asyncio.Event | None = None,
    out: IO[str] | None = None,
    err: IO[str] | None = None,
) -> RunResult:
    """Run the generator until ``duration`` passes or ``stop`` is set; print and return a summary.

    Setting ``force`` after ``stop`` skips the grace period for in-flight reports.
    """
    console = Console(out or sys.stdout, err or sys.stderr)
    load = LoadRun(config, console, stop or asyncio.Event(), force or asyncio.Event())
    return await load.execute()


async def _main(config: Config) -> int:
    loop = asyncio.get_running_loop()
    stop, force = asyncio.Event(), asyncio.Event()

    def interrupted() -> None:
        (force if stop.is_set() else stop).set()

    installed: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, interrupted)
            installed.append(signum)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - Windows
            signal.signal(signum, lambda *_: loop.call_soon_threadsafe(interrupted))
    # With the summary on standard output, the running report moves to standard error.
    out = sys.stderr if config.json_path == JSON_TO_STDOUT else None
    try:
        result = await run(config, stop=stop, force=force, out=out)
    finally:
        for signum in installed:
            loop.remove_signal_handler(signum)
    return result.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    config = parse_config(argv)
    with asyncio.Runner() as runner:
        return runner.run(_main(config))


if __name__ == "__main__":
    raise SystemExit(main())
