"""The load generator against an in-process stand-in for the API.

The stand-in implements what the generator talks to, from the contract: HTTP ingest (section 11,
with throttling phases), the WebSocket credit protocol (ready/ack/hold/credit, credit enforced
with close 1008), and the parts of the session, zone and live-channel API observe mode uses. It
validates every report with the real schema, encodes positions and events with the real wire
codecs, and records what it saw, so each test judges the generator from the server's side:
nothing lost or duplicated, pauses honoured per connection, credit never exceeded.
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import math
import os
import signal
import socket
import sys
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, NamedTuple

import msgspec
import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

import generator
from perimeter.domain import tiles
from perimeter.domain.reports import LocationReport, ms_to_datetime
from perimeter.wire import frames as reference
from perimeter.wire.events import EventType, encode_event, live_frame, make_event

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "ingest-secret"
SESSION_TOKEN = "observer-session-token"
USER_ID = "0193a2b4-0000-7000-8000-000000000001"


class Envelope(msgspec.Struct):
    reports: list[msgspec.Raw]


decode_envelope = msgspec.json.Decoder(Envelope).decode
decode_report = msgspec.json.Decoder(LocationReport).decode


class Hit(NamedTuple):
    """One ingest request as the server saw it."""

    at: float
    port: int
    status: int
    keys: tuple[tuple[str, int], ...]


@dataclasses.dataclass
class Stream:
    """Credit bookkeeping of one ingest WebSocket, from the server's side."""

    allowance: int
    withheld: int = 0
    hold_sent: bool = False
    received_while_holding: int = 0


def problem(status: int, code: str, headers: dict[str, str] | None = None) -> web.Response:
    return web.json_response(
        {"type": f"/problems/{code}", "status": status, "code": code},
        status=status,
        content_type="application/problem+json",
        headers=headers,
    )


def zone_problems(zone: Any) -> list[str]:
    if not isinstance(zone, dict):
        return ["not an object"]
    checks = {
        "name": isinstance(zone.get("name"), str) and 1 <= len(zone["name"]) <= 80,
        "center": isinstance(zone.get("center"), dict)
        and -90 <= zone["center"].get("lat", 99) <= 90
        and -180 <= zone["center"].get("lon", 999) <= 180,
        "radius_m": isinstance(zone.get("radius_m"), float) and 10 <= zone["radius_m"] <= 100_000,
        "color": isinstance(zone.get("color"), str) and len(zone["color"]) == 7,
        "dwell_s": zone.get("dwell_s") is None or 10 <= zone["dwell_s"] <= 86_400,
    }
    return [name for name, ok in checks.items() if not ok]


def alert_payload(kind: str, point: reference.FramePoint) -> dict[str, Any]:
    return {
        "alert_id": str(uuid.uuid4()),
        "kind": kind,
        "device_id": point.device_id,
        "zone": {"id": str(uuid.uuid4()), "name": "Load zone 01"},
        "occurred_at": ms_to_datetime(point.recorded_at_ms).isoformat(),
        "position": {"lat": point.lat, "lon": point.lon},
    }


class FakeApi:
    """The parts of the API the generator talks to, implemented from the contract."""

    def __init__(self) -> None:
        self.url = ""
        self.started: float | None = None
        self.throttle: Callable[[int, float], float | None] = lambda number, elapsed: None
        self.fail_auth_after: int | None = None
        self.failures: dict[int, str] = {}  # request number -> "500", "503", "413", "422", "drop"
        self.delay_s = 0.0
        self.reject_prefix: str | None = None
        self.hits: list[Hit] = []
        self.received: Counter[tuple[str, int]] = Counter()
        self.invalid = 0
        self.rejected = 0
        # WebSocket ingest
        self.credit = 1_000
        self.holding = False
        self.hold_retry_after = 5
        self.state = asyncio.Condition()
        self.frame_times: list[float] = []
        self.violations = 0
        self.close_after_frames: tuple[int, int] | None = None  # (frame number, close code)
        self.closed_frame_size = 0
        self.error_frames: set[int] = set()
        self.refused_frame_size = 0
        self.refuse_handshakes = 0
        self.refusal_retry_after: str | None = None
        self.streams = 0
        self.max_while_holding = 0
        # observe mode
        self.zones: dict[str, dict[str, Any]] = {}
        self.deleted: list[str] = []
        self.viewports: list[dict[str, Any]] = []
        self.signed_out = False
        self.live_sessions = 0
        self.refuse_live = False
        self.drop_live_after_alerts = False
        self.resume_requests: list[str | None] = []
        self.fresh: list[reference.FramePoint] = []
        self.app = web.Application()
        self.app.add_routes(
            [
                web.post("/v1/telemetry", self.telemetry),
                web.get("/v1/telemetry/stream", self.stream),
                web.post("/v1/token", self.sign_in),
                web.delete("/v1/session", self.sign_out),
                web.post("/v1/geozones", self.create_zone),
                web.delete("/v1/geozones/{zone_id}", self.delete_zone),
                web.get("/v1/live", self.live),
            ]
        )

    @property
    def duplicates(self) -> int:
        return sum(self.received.values()) - len(self.received)

    def book(
        self, raws: list[msgspec.Raw]
    ) -> tuple[int, list[dict[str, Any]], tuple[tuple[str, int], ...]]:
        accepted: int = 0
        rejected: list[dict[str, Any]] = []
        keys: list[tuple[str, int]] = []
        for index, raw in enumerate(raws):
            try:
                report = decode_report(raw)
            except msgspec.ValidationError as exc:
                self.invalid += 1
                rejected.append({"index": index, "code": "invalid", "detail": str(exc)})
                continue
            key = (report.device_id, report.recorded_at_ms())
            keys.append(key)
            if self.reject_prefix and report.device_id.startswith(self.reject_prefix):
                self.rejected += 1
                rejected.append({"index": index, "code": "test_rejected", "detail": "by the test"})
                continue
            self.received[key] += 1
            accepted += 1
            if self.live_sessions:
                self.fresh.append(
                    reference.FramePoint(
                        report.device_id,
                        report.latitude,
                        report.longitude,
                        key[1],
                        report.speed,
                        report.heading,
                    )
                )
        return accepted, rejected, tuple(keys)

    # --- ingest ----------------------------------------------------------------------------------

    async def telemetry(self, request: web.Request) -> web.Response:
        now = asyncio.get_running_loop().time()
        if self.started is None:
            self.started = now
        number = len(self.hits)
        peer = request.transport.get_extra_info("peername") if request.transport else None
        port = int(peer[1]) if peer else 0
        revoked = self.fail_auth_after is not None and number >= self.fail_auth_after
        if request.headers.get("Authorization") != f"Bearer {TOKEN}" or revoked:
            self.hits.append(Hit(now, port, 401, ()))
            return problem(401, "unauthorized")
        raws = decode_envelope(await request.read()).reports
        retry_after = self.throttle(number, now - self.started)
        if retry_after is not None:
            # The real API answers before reading the body; reading it here names the batch, so
            # the test can recognise its retry.
            keys = tuple((r.device_id, r.recorded_at_ms()) for r in map(decode_report, raws))
            self.hits.append(Hit(now, port, 503, keys))
            return problem(503, "overloaded", {"Retry-After": f"{retry_after:g}"})
        failure = self.failures.pop(number, None)
        if failure is not None:
            keys = tuple((r.device_id, r.recorded_at_ms()) for r in map(decode_report, raws))
            self.hits.append(Hit(now, port, int(failure) if failure.isdigit() else 0, keys))
            return self.fail(request, failure, len(raws))
        if self.delay_s and raws:
            await asyncio.sleep(self.delay_s)
        accepted, rejected, keys = self.book(raws)
        self.hits.append(Hit(now, port, 202, keys))
        return web.json_response({"accepted": accepted, "rejected": rejected}, status=202)

    def fail(self, request: web.Request, failure: str, size: int) -> web.Response:
        if failure == "drop":
            assert request.transport is not None
            request.transport.abort()  # the client sees the connection go, never a response
            return web.Response(status=500)
        if failure == "422":
            rejected = [
                {"index": index, "code": "timestamp_in_future", "detail": "30 s ahead"}
                for index in range(size)
            ]
            return web.json_response(
                {"status": 422, "code": "validation_failed", "rejected": rejected},
                status=422,
                content_type="application/problem+json",
            )
        codes = {"500": "internal", "503": "unavailable", "413": "payload_too_large"}
        return problem(int(failure), codes[failure])

    async def stream(self, request: web.Request) -> web.StreamResponse:
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return problem(401, "unauthorized")
        if self.refuse_handshakes:
            self.refuse_handshakes -= 1
            retry = {"Retry-After": self.refusal_retry_after} if self.refusal_retry_after else None
            return problem(503, "unavailable", retry)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.streams += 1
        stream = Stream(allowance=self.credit)
        await ws.send_json({"type": "ready", "credit": self.credit})
        granter = asyncio.create_task(self._grant_withheld(ws, stream))
        try:
            async for message in ws:
                if message.type is not WSMsgType.TEXT:
                    break
                frame = json.loads(message.data)
                raws = [msgspec.Raw(msgspec.json.encode(item)) for item in frame["reports"]]
                if len(raws) > stream.allowance:
                    self.violations += 1
                    await ws.close(code=1008, message=b"credit exceeded")
                    break
                stream.allowance -= len(raws)
                self.frame_times.append(asyncio.get_running_loop().time())
                if self.holding:
                    stream.received_while_holding += len(raws)
                    self.max_while_holding = max(
                        self.max_while_holding, stream.received_while_holding
                    )
                if len(self.frame_times) in self.error_frames:
                    self.refused_frame_size = len(raws)
                    stream.allowance += len(raws)
                    refusal = {"type": "error", "seq": frame["seq"], "code": "frame_rejected"}
                    await ws.send_json({**refusal, "credit": len(raws)})
                    continue
                accepted, rejected, _ = self.book(raws)
                if self.close_after_frames and len(self.frame_times) >= self.close_after_frames[0]:
                    code = self.close_after_frames[1]
                    self.close_after_frames = None
                    self.closed_frame_size = len(raws)
                    await ws.close(code=code, message=b"stored but never acknowledged")
                    break
                grant = len(raws)
                if self.holding:
                    if not stream.hold_sent:
                        await ws.send_json({"type": "hold", "retry_after": self.hold_retry_after})
                        stream.hold_sent = True
                    stream.withheld += grant
                    grant = 0
                stream.allowance += grant
                await ws.send_json(
                    {
                        "type": "ack",
                        "seq": frame["seq"],
                        "accepted": accepted,
                        "rejected": rejected,
                        "credit": grant,
                    }
                )
        finally:
            granter.cancel()
            with suppress(asyncio.CancelledError):
                await granter
        return ws

    async def _grant_withheld(self, ws: web.WebSocketResponse, stream: Stream) -> None:
        while True:
            async with self.state:
                await self.state.wait_for(lambda: not self.holding and stream.withheld > 0)
                credit, stream.withheld = stream.withheld, 0
                stream.hold_sent = False
                stream.received_while_holding = 0
            stream.allowance += credit
            await ws.send_json({"type": "credit", "credit": credit})

    async def set_holding(self, holding: bool) -> None:
        async with self.state:
            self.holding = holding
            self.state.notify_all()

    # --- observe mode ----------------------------------------------------------------------------

    def _signed_in(self, request: web.Request) -> bool:
        return request.headers.get("Authorization") == f"Bearer {SESSION_TOKEN}"

    async def sign_in(self, request: web.Request) -> web.Response:
        body = await request.json()
        username = str(body["username"]).lower()
        user = {"id": USER_ID, "username": username}
        return web.json_response(
            {"token": SESSION_TOKEN, "token_type": "bearer", "expires_at": 0, "user": user}
        )

    async def sign_out(self, request: web.Request) -> web.Response:
        if not self._signed_in(request):
            return problem(401, "unauthorized")
        self.signed_out = True
        return web.Response(status=204)

    async def create_zone(self, request: web.Request) -> web.Response:
        if not self._signed_in(request):
            return problem(401, "unauthorized")
        zone = await request.json()
        if zone_problems(zone):
            return problem(422, "validation_failed")
        zone_id = str(uuid.uuid4())
        self.zones[zone_id] = zone
        return web.json_response({**zone, "id": zone_id, "version": 1, "occupancy": 0}, status=201)

    async def delete_zone(self, request: web.Request) -> web.Response:
        zone_id = request.match_info["zone_id"]
        if not self._signed_in(request) or self.zones.pop(zone_id, None) is None:
            return problem(404, "not_found")
        self.deleted.append(zone_id)
        return web.Response(status=204)

    async def live(self, request: web.Request) -> web.StreamResponse:
        if not self._signed_in(request) or self.refuse_live:
            return problem(403, "forbidden")
        self.resume_requests.append(request.query.get("resume_after"))
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        hello = {"type": "hello", "session_id": "s1", "protocol": 1, "tile_zoom": 12}
        await ws.send_json({**hello, "resume": {"mode": "fresh", "after": None}})
        self.viewports.append(await ws.receive_json(timeout=5))
        self.live_sessions += 1
        pusher = asyncio.create_task(self._push(ws, reconnected=len(self.resume_requests) > 1))
        try:
            async for message in ws:
                if message.type is WSMsgType.TEXT:
                    self.viewports.append(json.loads(message.data))
        finally:
            self.live_sessions -= 1
            pusher.cancel()
            with suppress(asyncio.CancelledError):
                await pusher
        return ws

    async def _push(self, ws: web.WebSocketResponse, *, reconnected: bool) -> None:
        """A snapshot, then every 100 ms the fresh positions as tile frames, like the engine."""
        parked = reference.FramePoint("parked-van", 52.37, 4.9, time.time_ns() // 1_000_000)
        tile = tiles.tile_for(parked.lon, parked.lat, 12)
        snapshot = reference.encode_tile(reference.FrameKind.SNAPSHOT, 12, tile.x, tile.y, [parked])
        await ws.send_bytes(reference.encode_bundle([snapshot]))
        alerted = reconnected
        if reconnected:
            # A replay that overlaps what the client already has (9, 12), then a new alert (15).
            point = reference.FramePoint("veh-00001", 52.37, 4.9, time.time_ns() // 1_000_000)
            for seq, prev, kind in ((9, 5, "exit"), (12, 9, "dwell"), (15, 12, "enter")):
                payload = encode_event(make_event(EventType.ALERT, alert_payload(kind, point)))
                await ws.send_str(live_frame(seq, prev, payload).decode())
        while True:
            await asyncio.sleep(0.1)
            fresh, self.fresh = self.fresh, []
            if not fresh:
                continue
            by_tile: defaultdict[tiles.Tile, list[reference.FramePoint]] = defaultdict(list)
            for point in fresh:
                by_tile[tiles.tile_for(point.lon, point.lat, 12)].append(point)
            frames = [
                reference.encode_tile(reference.FrameKind.LIVE, 12, t.x, t.y, points)
                for t, points in by_tile.items()
            ]
            await ws.send_bytes(reference.encode_bundle(frames))
            if not alerted:
                alerted = True
                # Three alerts; the exit arrives twice, as after a replay on reconnect.
                for seq, prev, kind in (
                    (5, 0, "enter"),
                    (9, 5, "exit"),
                    (9, 5, "exit"),
                    (12, 9, "dwell"),
                ):
                    payload = encode_event(
                        make_event(EventType.ALERT, alert_payload(kind, fresh[0]))
                    )
                    await ws.send_str(live_frame(seq, prev, payload).decode())
                pulse = {"type": "pulse", "window_ms": 100, "zones": {"z1": [fresh[0].device_id]}}
                await ws.send_json(pulse)
                await ws.send_json({"type": "resync", "scope": "positions"})
                if self.drop_live_after_alerts:
                    await ws.close(code=1012, message=b"restarting")
                    return


@pytest.fixture
async def api() -> AsyncIterator[FakeApi]:
    fake = FakeApi()
    server = TestServer(fake.app)
    await server.start_server()
    fake.url = str(server.make_url("")).rstrip("/")
    try:
        yield fake
    finally:
        await server.close()


def settings(api: FakeApi, **changes: Any) -> generator.Config:
    base = generator.Config(
        url=api.url,
        token=TOKEN,
        devices=400,
        interval=0.2,
        jitter=0.3,
        ramp=0.0,
        duration=2.0,
        connections=4,
        batch=100,
        linger_ms=50.0,
        buffer=50_000,
        retries=4,
        timeout=5.0,
        radius_km=3.0,
        report_every=0.5,
        seed=11,
    )
    return dataclasses.replace(base, **changes)


async def run_generator(
    config: generator.Config,
    stop: asyncio.Event | None = None,
    force: asyncio.Event | None = None,
) -> tuple[generator.RunResult, str, str]:
    out, err = io.StringIO(), io.StringIO()
    result = await generator.run(config, stop=stop, force=force, out=out, err=err)
    return result, out.getvalue(), err.getvalue()


def assert_books_balance(result: generator.RunResult) -> None:
    reports = result.summary["reports"]
    assert reports["unsettled"] == 0
    assert reports["offered"] == reports["accepted"] + reports["rejected"] + reports["dropped"]


def assert_each_report_stored_once(result: generator.RunResult, api: FakeApi) -> None:
    assert api.invalid == 0
    assert api.duplicates == 0
    assert len(api.received) == result.summary["reports"]["accepted"]


def closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# --- HTTP ----------------------------------------------------------------------------------------


async def test_every_report_is_delivered_and_stored_once(api: FakeApi) -> None:
    result, out, err = await run_generator(settings(api))
    assert result.exit_code == 0, err
    summary = result.summary
    assert summary["stop_reason"] == "duration"
    assert summary["reports"]["accepted"] == summary["reports"]["offered"] > 3_000
    assert summary["errors"] == 0
    assert_books_balance(result)
    assert_each_report_stored_once(result, api)
    assert out.count("offered") >= 3  # status lines every 0.5 s
    assert "(balanced)" in out


async def test_a_throttled_connection_pauses_alone_then_retries_its_batch(api: FakeApi) -> None:
    api.throttle = lambda number, elapsed: 1.0 if number == 8 else None
    result, _, _ = await run_generator(settings(api, duration=3.0))
    (refused,) = [hit for hit in api.hits if hit.status == 503]
    retry = next(hit for hit in api.hits if hit.status == 202 and hit.keys == refused.keys)
    assert retry.port == refused.port  # same keep-alive connection
    assert 1.0 <= retry.at - refused.at <= 1.0 + generator.RETRY_AFTER_SPREAD + 0.3
    meanwhile = [hit for hit in api.hits if refused.at < hit.at < retry.at]
    assert len(meanwhile) >= 10  # the rest of the fleet kept reporting...
    assert all(hit.port != refused.port for hit in meanwhile)  # ...but not on that connection
    summary = result.summary
    assert summary["throttled"] == 1
    assert summary["reports"]["resent"] == len(refused.keys)
    assert summary["reports"]["dropped"] == 0
    assert_books_balance(result)
    assert_each_report_stored_once(result, api)


async def test_shedding_is_honoured_without_a_retry_storm(api: FakeApi) -> None:
    api.throttle = lambda number, elapsed: 1.0 if 0.5 <= elapsed < 2.0 else None
    result, out, _ = await run_generator(settings(api, duration=3.5))
    refusals = Counter(hit.port for hit in api.hits if hit.status == 503)
    assert refusals
    assert max(refusals.values()) <= 2  # 1.5 s of shedding, Retry-After 1 s: two tries at most
    assert sum(refusals.values()) <= 2 * 4
    assert result.summary["throttled"] == sum(refusals.values())
    assert result.summary["reports"]["dropped"] == 0  # the backlog drains once shedding ends
    assert_books_balance(result)
    assert_each_report_stored_once(result, api)
    assert "queued" in out


async def test_rejections_are_counted_by_code(api: FakeApi) -> None:
    api.reject_prefix = "ped-"
    result, _, _ = await run_generator(settings(api))
    summary = result.summary
    assert summary["reports"]["rejected"] == api.rejected > 0
    assert summary["rejected_by_code"] == {"test_rejected": api.rejected}
    assert_books_balance(result)
    assert_each_report_stored_once(result, api)


async def test_batches_that_keep_failing_are_dropped_after_the_retries(api: FakeApi) -> None:
    api.throttle = lambda number, elapsed: 0.05
    result, _, _ = await run_generator(settings(api, retries=1, duration=1.5))
    summary = result.summary
    assert result.exit_code == 0
    assert summary["reports"]["accepted"] == 0
    assert summary["dropped_by_reason"]["retries"] > 1_000
    batches = Counter(hit.keys for hit in api.hits if hit.keys)
    assert set(batches.values()) <= {1, 2}  # one retry per batch, never more
    assert_books_balance(result)


async def test_a_full_buffer_drops_the_oldest_reports(api: FakeApi) -> None:
    api.delay_s = 0.3
    result, _, _ = await run_generator(
        settings(api, connections=1, batch=50, buffer=200, duration=2.0)
    )
    summary = result.summary
    assert summary["dropped_by_reason"]["overflow"] > 1_000
    assert summary["reports"]["accepted"] > 0
    assert_books_balance(result)
    assert_each_report_stored_once(result, api)
    newest = max(key[1] for key in api.received)
    assert newest - min(key[1] for key in api.received) < 2_500  # what got through is recent


async def test_stop_drains_in_flight_reports_and_writes_the_json_summary(
    api: FakeApi, tmp_path: Path
) -> None:
    api.delay_s = 0.2
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(1.0, stop.set)
    path = tmp_path / "summary.json"
    result, _, _ = await run_generator(settings(api, duration=0, json_path=path), stop)
    summary = result.summary
    assert result.exit_code == 0
    assert summary["stop_reason"] == "interrupted"
    assert summary["reports"]["dropped"] == 0
    assert json.loads(path.read_text()) == summary
    assert_books_balance(result)
    assert_each_report_stored_once(result, api)


async def test_a_second_interrupt_skips_the_grace_period(api: FakeApi) -> None:
    api.delay_s = 3.0
    stop, force = asyncio.Event(), asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.call_later(0.8, stop.set)
    loop.call_later(1.0, force.set)
    started = loop.time()
    result, _, _ = await run_generator(settings(api, duration=0, timeout=10.0), stop, force)
    assert loop.time() - started < 2.5
    assert result.exit_code == generator.EXIT_FORCED
    assert result.summary["stop_reason"] == "forced"
    assert result.summary["dropped_by_reason"]["shutdown"] > 0
    assert_books_balance(result)


async def test_credentials_revoked_mid_run_abort_the_run(api: FakeApi) -> None:
    api.fail_auth_after = 6
    result, _, err = await run_generator(settings(api, duration=5.0))
    assert result.exit_code == generator.EXIT_FATAL
    assert result.summary["stop_reason"] == "fatal"
    assert "HTTP 401" in err
    assert result.summary["active_s"] < 2.0
    assert_books_balance(result)


@pytest.mark.parametrize("transport", ["http", "ws"])
async def test_an_unreachable_target_fails_fast(transport: generator.TransportName) -> None:
    config = generator.Config(
        url=f"http://127.0.0.1:{closed_port()}", token=TOKEN, transport=transport, devices=10
    )
    started = time.monotonic()
    result, _, err = await run_generator(config)
    assert result.exit_code == generator.EXIT_UNREACHABLE
    assert result.summary["stop_reason"] == "unreachable"
    assert time.monotonic() - started < 5.0
    assert "cannot" in err


@pytest.mark.parametrize("transport", ["http", "ws"])
async def test_a_rejected_token_fails_fast(
    api: FakeApi, transport: generator.TransportName
) -> None:
    config = dataclasses.replace(settings(api, transport=transport), token="wrong")
    result, _, err = await run_generator(config)
    assert result.exit_code == generator.EXIT_UNREACHABLE
    assert "rejected the ingest token (HTTP 401)" in err
    assert result.summary["reports"]["offered"] == 0


@pytest.mark.parametrize(
    ("failure", "retried", "codes"),
    [
        ("500", True, {}),
        ("503", True, {}),
        ("drop", True, {}),
        ("422", False, {"timestamp_in_future"}),
        ("413", False, {"payload_too_large"}),
    ],
)
async def test_failed_requests_are_retried_or_rejected_never_lost(
    api: FakeApi, failure: str, retried: bool, codes: set[str]
) -> None:
    api.failures = {6: failure}
    result, _, err = await run_generator(settings(api))
    summary = result.summary
    (failed,) = [hit for hit in api.hits if hit.status in (0, 413, 422, 500, 503)]
    stored = {hit.keys for hit in api.hits if hit.status == 202}
    if retried:
        assert failed.keys in stored  # the same batch came again and was stored
        assert summary["reports"]["resent"] == len(failed.keys)
        assert summary["reports"]["rejected"] == 0
        assert summary["throttled"] + summary["errors"] == 1
    else:
        assert failed.keys not in stored
        assert summary["reports"]["resent"] == 0
        assert summary["rejected_by_code"] == dict.fromkeys(codes, len(failed.keys))
    if failure == "413":
        assert "lower --batch" in err
    assert summary["reports"]["dropped"] == 0
    assert_books_balance(result)
    assert_each_report_stored_once(result, api)


@pytest.mark.parametrize("transport", ["http", "ws"])
async def test_a_proxy_without_an_api_behind_it_fails_fast(
    api: FakeApi, transport: generator.TransportName
) -> None:
    api.failures = {0: "503"}  # a 503 without Retry-After, as a proxy answers
    api.refuse_handshakes = 1
    result, _, err = await run_generator(settings(api, transport=transport))
    assert result.exit_code == generator.EXIT_UNREACHABLE
    assert "no API instance is available" in err


# --- WebSocket -----------------------------------------------------------------------------------


async def test_ws_stream_stays_within_credit_and_delivers_everything(api: FakeApi) -> None:
    api.credit = 300
    result, _, err = await run_generator(settings(api, transport="ws", connections=2))
    assert result.exit_code == 0, err
    assert api.violations == 0
    assert api.streams == 2
    summary = result.summary
    assert summary["errors"] == 0
    assert summary["reports"]["accepted"] == summary["reports"]["offered"] > 3_000
    assert summary["ack_latency_ms"]["count"] == len(api.frame_times)
    assert_books_balance(result)
    assert_each_report_stored_once(result, api)


async def test_ws_hold_stops_the_stream_until_credit_is_granted_again(api: FakeApi) -> None:
    api.credit = 300
    loop = asyncio.get_running_loop()
    window: list[float] = []

    async def hold_for_a_while() -> None:
        await asyncio.sleep(0.8)
        await api.set_holding(True)
        window.append(loop.time())
        await asyncio.sleep(1.2)
        window.append(loop.time())
        await api.set_holding(False)

    holder = asyncio.create_task(hold_for_a_while())
    result, _, _ = await run_generator(settings(api, transport="ws", connections=2, duration=3.0))
    await holder
    start, end = window
    assert api.violations == 0
    assert result.summary["throttled"] == 2  # one hold per stream
    assert api.max_while_holding <= api.credit
    assert not [t for t in api.frame_times if start + 0.3 <= t < end]  # quiet while held
    assert [t for t in api.frame_times if t >= end]  # and flowing again once credit returns
    assert result.summary["reports"]["dropped"] == 0
    assert_books_balance(result)
    assert_each_report_stored_once(result, api)


async def test_ws_reconnects_and_resends_what_was_not_acknowledged(api: FakeApi) -> None:
    api.close_after_frames = (8, 1011)
    result, _, err = await run_generator(settings(api, transport="ws", connections=2, duration=2.5))
    summary = result.summary
    assert result.exit_code == 0
    assert summary["connections_opened"] == 3
    assert summary["errors_by_kind"] == {"ws_close_1011": 1}
    assert summary["reports"]["resent"] >= api.closed_frame_size > 0
    assert "stream" in err
    assert_books_balance(result)
    # The frame the server stored but never acknowledged came again; de-duplication absorbs it.
    assert api.duplicates == api.closed_frame_size
    assert len(api.received) == summary["reports"]["accepted"] == summary["reports"]["offered"]


async def test_ws_frames_the_server_refuses_are_rejected_without_reconnecting(api: FakeApi) -> None:
    api.error_frames = {3}
    result, _, err = await run_generator(settings(api, transport="ws", connections=2))
    summary = result.summary
    assert summary["rejected_by_code"] == {"frame_rejected": api.refused_frame_size}
    assert summary["connections_opened"] == 2
    assert summary["errors"] == 0
    assert "the server rejected a frame" in err
    assert_books_balance(result)
    assert_each_report_stored_once(result, api)


async def test_ws_close_for_revoked_credentials_aborts_the_run(api: FakeApi) -> None:
    api.close_after_frames = (5, 4003)
    result, _, err = await run_generator(settings(api, transport="ws", connections=2, duration=5.0))
    assert result.exit_code == generator.EXIT_FATAL
    assert result.summary["stop_reason"] == "fatal"
    assert "4003" in err
    assert result.summary["active_s"] < 2.0
    assert_books_balance(result)


async def test_ws_handshakes_refused_while_shedding_wait_for_retry_after(api: FakeApi) -> None:
    api.refuse_handshakes = 3
    api.refusal_retry_after = "0.3"
    result, _, _ = await run_generator(settings(api, transport="ws", connections=2))
    summary = result.summary
    assert result.exit_code == 0
    assert summary["throttled"] == 3  # the start-up refusal included: it did not end the run
    assert summary["errors"] == 0
    assert summary["connections_opened"] == 2
    assert summary["reports"]["dropped"] == 0
    assert_books_balance(result)
    assert_each_report_stored_once(result, api)


# --- observe mode --------------------------------------------------------------------------------


async def test_observe_mode_measures_latency_and_counts_each_alert_once(api: FakeApi) -> None:
    result, out, _ = await run_generator(
        settings(api, observe="load-watcher", zones=3, duration=2.5)
    )
    assert result.exit_code == 0
    observe = result.summary["observe"]
    assert observe["positions"] == result.summary["reports"]["accepted"] > 3_000
    assert 0 < observe["e2e_latency_ms"]["p50"] < 1_500
    assert observe["snapshot_positions"] == 1
    assert observe["alerts"] == {"enter": 1, "exit": 1, "dwell": 1}
    assert observe["alert_latency_ms"]["count"] == 3
    assert observe["pulses"] == 1
    assert observe["zones_created"] == observe["zones_deleted"] == 3
    assert not api.zones
    assert len(api.deleted) == 3
    assert api.signed_out
    west, south, east, north = api.viewports[0]["bbox"]
    for bearing in range(0, 360, 30):
        theta = math.radians(bearing)
        lat, lon = generator.displace(
            52.3676, 4.9041, 3_000 * math.sin(theta), 3_000 * math.cos(theta)
        )
        assert west <= lon <= east
        assert south <= lat <= north
    assert len(api.viewports) == 2  # the resync was answered with the viewport again
    assert "e2e p50/p99" in out


async def test_observe_mode_resumes_events_after_the_live_channel_drops(api: FakeApi) -> None:
    api.drop_live_after_alerts = True
    result, _, err = await run_generator(settings(api, observe="load-watcher", duration=3.0))
    observe = result.summary["observe"]
    assert result.exit_code == 0
    assert api.resume_requests == [None, "12"]  # reconnected, resuming after the last event seen
    assert observe["alerts"] == {"enter": 2, "exit": 1, "dwell": 1}  # the overlap counted once
    assert result.summary["errors_by_kind"] == {"observe_lost": 1}
    assert "live channel lost" in err
    assert observe["positions"] > 1_000


async def test_observe_mode_fails_fast_when_the_live_channel_refuses(api: FakeApi) -> None:
    api.refuse_live = True
    result, _, err = await run_generator(settings(api, observe="load-watcher", zones=2))
    assert result.exit_code == generator.EXIT_UNREACHABLE
    assert "observe mode" in err
    assert not api.zones  # zones created before the failure are removed again


# --- as a process --------------------------------------------------------------------------------


def generator_env(api: FakeApi, **values: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GENERATOR_")}
    env.update({f"GENERATOR_{key.upper()}": value for key, value in values.items()})
    env.update({"GENERATOR_URL": api.url, "GENERATOR_TOKEN": TOKEN})
    return env


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGTERM])
async def test_the_script_runs_from_the_environment_and_stops_cleanly_on_a_signal(
    api: FakeApi, tmp_path: Path, signum: signal.Signals
) -> None:
    path = tmp_path / "summary.json"
    env = generator_env(
        api,
        devices="300",
        interval="0.2",
        ramp="0",
        duration="0",
        connections="2",
        report_every="0.3",
        json=str(path),
        seed="5",
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(ROOT / "generator.py"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    assert process.stdout is not None
    head = []
    async with asyncio.timeout(15):
        while not (line := (await process.stdout.readline()).decode()).startswith("["):
            assert line, "the generator exited before its first status line"
            head.append(line)
    process.send_signal(signum)
    async with asyncio.timeout(20):
        rest, errors = await process.communicate()
    assert process.returncode == 0, errors.decode()
    text = "".join(head) + rest.decode()
    assert "seed 5" in text
    assert "(balanced)" in text
    summary = json.loads(path.read_text())
    assert summary["stop_reason"] == "interrupted"
    assert summary["reports"]["unsettled"] == 0
    assert summary["reports"]["accepted"] == len(api.received) > 0
    assert api.duplicates == 0


async def test_the_script_exits_with_3_when_the_target_is_down(tmp_path: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(ROOT / "generator.py"),
        "--url",
        f"http://127.0.0.1:{closed_port()}",
        "--token",
        TOKEN,
        "--json",
        str(tmp_path / "summary.json"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    async with asyncio.timeout(15):
        _, errors = await process.communicate()
    assert process.returncode == generator.EXIT_UNREACHABLE
    assert b"cannot reach" in errors
    assert json.loads((tmp_path / "summary.json").read_text())["exit_code"] == 3


async def test_the_summary_can_go_to_standard_output_as_json_alone(api: FakeApi) -> None:
    # How a caller in another container (the failure drill) reads the result: no shared files.
    env = generator_env(
        api, devices="50", interval="0.2", ramp="0", duration="1", connections="1", json="-"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(ROOT / "generator.py"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    async with asyncio.timeout(30):
        out, report = await process.communicate()
    assert process.returncode == 0, report.decode()
    summary = json.loads(out)
    assert summary["reports"]["accepted"] == len(api.received) > 0
    assert "(balanced)" in report.decode()  # the human report went to standard error
