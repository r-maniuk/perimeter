#!/usr/bin/env python3
"""End-to-end smoke test of a running stack, through the edge only.

    docker compose exec -T api cat /run/secrets/ingest_token \\
        | uv run python scripts/smoke.py --ingest-token-file -        # what `make smoke` runs

In order: the edge serves the dashboard with its security headers and hides /metrics; the API is
live and ready behind it; a user signs in and draws a zone; a live session opens on the zone's
area; one device report from inside the zone is ingested; the session then receives the ``enter``
alert and the device's position as a binary frame, each within the deadline; finally the zone is
deleted and the user signs out. Exit status 0 means every step passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from websockets.asyncio.client import ClientConnection, connect

from perimeter.wire.frames import FrameError, decode_bundle, decode_tile

# Dam Square, Amsterdam.
CENTER_LAT, CENTER_LON = 52.3731, 4.8926
ZONE_RADIUS_M = 250.0
REQUIRED_HEADERS = {
    "content-security-policy": "script-src 'self'",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "strict-origin-when-cross-origin",
    "permissions-policy": "camera=()",
}


class SmokeTestError(Exception):
    """A step did not behave as the stack promises."""


@dataclass(frozen=True, slots=True)
class Options:
    base_url: str
    ingest_token: str
    timeout_s: float


def _expect(condition: object, message: str) -> None:
    if not condition:
        raise SmokeTestError(message)


def _expect_status(response: httpx.Response, status: int) -> None:
    if response.status_code != status:
        body = response.text[:300]
        msg = f"{response.request.method} {response.request.url.path} -> {response.status_code}"
        raise SmokeTestError(f"{msg} (expected {status}): {body}")


async def _step[T](name: str, action: Callable[[], Awaitable[T]]) -> T:
    started = time.perf_counter()
    try:
        result = await action()
    except SmokeTestError as exc:
        print(f"FAIL  {name}: {exc}", flush=True)
        raise
    except (httpx.HTTPError, OSError, TimeoutError) as exc:
        print(f"FAIL  {name}: {type(exc).__name__}: {exc}", flush=True)
        raise SmokeTestError(name) from exc
    print(f"  ok  {name} ({(time.perf_counter() - started) * 1000:.0f} ms)", flush=True)
    return result


async def check_edge(http: httpx.AsyncClient) -> None:
    page = await http.get("/")
    _expect_status(page, 200)
    _expect(page.headers.get("content-type", "").startswith("text/html"), "/ is not HTML")
    for header, fragment in REQUIRED_HEADERS.items():
        _expect(fragment in page.headers.get(header, ""), f"{header} missing or weak on /")
    _expect("server" not in page.headers, "the edge discloses a Server header")
    _expect_status(await http.get("/metrics"), 404)


async def check_api(http: httpx.AsyncClient) -> None:
    _expect_status(await http.get("/healthz"), 200)
    ready = await http.get("/readyz")
    _expect_status(ready, 200)
    _expect(ready.json().get("status") == "ready", f"api not ready: {ready.text}")
    schema = await http.get("/openapi.json")
    _expect_status(schema, 200)
    _expect(schema.json().get("info", {}).get("title") == "Perimeter", "unexpected OpenAPI title")


async def sign_in(http: httpx.AsyncClient, username: str) -> str:
    response = await http.post("/v1/session", json={"username": username})
    _expect(response.status_code in {200, 201}, f"sign-in answered {response.status_code}")
    token = response.json().get("token")
    _expect(isinstance(token, str) and token, "sign-in returned no token")
    return str(token)


async def create_zone(http: httpx.AsyncClient, token: str) -> str:
    response = await http.post(
        "/v1/geozones",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "name": "Smoke test",
            "center": {"lat": CENTER_LAT, "lon": CENTER_LON},
            "radius_m": ZONE_RADIUS_M,
        },
    )
    _expect_status(response, 201)
    return str(response.json()["id"])


async def open_live(live: ClientConnection) -> None:
    hello = json.loads(await asyncio.wait_for(live.recv(), 5))
    _expect(hello.get("type") == "hello", f"first live frame is not hello: {hello}")
    await live.send(
        json.dumps(
            {
                "type": "viewport",
                "bbox": [
                    CENTER_LON - 0.02,
                    CENTER_LAT - 0.01,
                    CENTER_LON + 0.02,
                    CENTER_LAT + 0.01,
                ],
                "zoom": 14,
            }
        )
    )


async def ingest(http: httpx.AsyncClient, ingest_token: str, device_id: str) -> None:
    now = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    report = {
        "device_id": device_id,
        "latitude": CENTER_LAT,
        "longitude": CENTER_LON,
        "timestamp": now,
        "speed": 1.4,
        "heading": 90.0,
    }
    response = await http.post(
        "/v1/telemetry",
        headers={"Authorization": f"Bearer {ingest_token}"},
        json={"reports": [report]},
    )
    _expect_status(response, 202)
    _expect(response.json().get("accepted") == 1, f"report not accepted: {response.text}")


def _is_enter_alert(frame: dict[str, Any], device_id: str) -> bool:
    event = frame.get("event") or {}
    data = event.get("data") or {}
    return (
        frame.get("type") == "event"
        and event.get("type") == "alert"
        and data.get("kind") == "enter"
        and data.get("device_id") == device_id
    )


def _carries_device(bundle: bytes, device_id: str) -> bool:
    try:
        return any(
            point.device_id == device_id
            for frame in decode_bundle(bundle)
            for point in decode_tile(frame).points
        )
    except FrameError as exc:
        raise SmokeTestError(f"malformed binary frame: {exc}") from exc


async def await_alert_and_position(
    live: ClientConnection, device_id: str, timeout_s: float
) -> None:
    alert = position = False
    try:
        async with asyncio.timeout(timeout_s):
            while not (alert and position):
                message = await live.recv()
                if isinstance(message, bytes):
                    position = position or _carries_device(message, device_id)
                else:
                    alert = alert or _is_enter_alert(json.loads(message), device_id)
    except TimeoutError:
        missing = [name for name, seen in (("alert", alert), ("position", position)) if not seen]
        msg = f"no {' or '.join(missing)} for {device_id} within {timeout_s:g} s"
        raise SmokeTestError(msg) from None


@asynccontextmanager
async def live_session(base_url: str, token: str) -> AsyncIterator[ClientConnection]:
    url = base_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
    headers = {"Authorization": f"Bearer {token}"}
    async with connect(
        f"{url}/v1/live", additional_headers=headers, open_timeout=5, max_size=None
    ) as live:
        yield live


async def run(options: Options) -> None:
    run_id = secrets.token_hex(4)
    username, device_id = f"smoke-{run_id}", f"smoke-{run_id}"
    async with httpx.AsyncClient(base_url=options.base_url, timeout=10) as http:
        await _step("edge serves the dashboard with security headers", lambda: check_edge(http))
        await _step("api is live and ready behind the edge", lambda: check_api(http))
        token = await _step(f"sign in as {username}", lambda: sign_in(http, username))
        auth = {"Authorization": f"Bearer {token}"}
        zone_id = await _step("create a zone", lambda: create_zone(http, token))
        try:
            async with live_session(options.base_url, token) as live:
                await _step("open the live channel and watch the zone", lambda: open_live(live))
                await _step(
                    f"ingest one report of {device_id} inside the zone",
                    lambda: ingest(http, options.ingest_token, device_id),
                )
                await _step(
                    "receive the enter alert and the position frame",
                    lambda: await_alert_and_position(live, device_id, options.timeout_s),
                )
        finally:
            await _step(
                "delete the zone",
                lambda: _delete(http, f"/v1/geozones/{zone_id}", auth),
            )
            await _step("sign out", lambda: _delete(http, "/v1/session", auth))


async def _delete(http: httpx.AsyncClient, path: str, headers: dict[str, str]) -> None:
    response = await http.delete(path, headers=headers)
    _expect(response.status_code in {200, 204}, f"DELETE {path} -> {response.status_code}")


def _read_token(source: str) -> str:
    raw = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    token = raw.strip()
    if not token:
        msg = f"no ingest token in {'standard input' if source == '-' else source}"
        raise SystemExit(msg)
    return token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8080", help="the edge")
    parser.add_argument(
        "--ingest-token-file",
        required=True,
        help="file holding the device ingest token, or - to read it from standard input",
    )
    parser.add_argument("--timeout", type=float, default=5.0, help="deadline for live delivery")
    args = parser.parse_args(argv)
    options = Options(
        base_url=args.base_url.rstrip("/"),
        ingest_token=_read_token(args.ingest_token_file),
        timeout_s=args.timeout,
    )
    try:
        asyncio.run(run(options))
    except SmokeTestError:
        return 1
    print("smoke test passed", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
