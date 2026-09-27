"""Fan-out benchmark: open many dashboard sessions and measure what each one receives.

Viewers sign in as a handful of users (``--per-user`` sessions each, staying under the sign-in rate
limit and the per-user session cap), open ``/v1/live`` with the same viewport (the whole simulated
area) and decode every binary bundle they get. While ``generator.py`` drives
the fleet, this shows how the live channel scales with the number of watchers: frames and bytes
delivered per second, and the device-to-socket latency seen by every viewer (position timestamp
to arrival, averaged per frame), which grows if any replica falls behind on its sockets.

    uv run python scripts/viewers.py --viewers 200 --duration 60
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import statistics
import struct
import time
from array import array
from dataclasses import dataclass, field

import httpx
from websockets.asyncio.client import connect

from perimeter.wire.frames import FrameKind, decode_bundle

HEADER = struct.Struct("<BBBBIIQI")  # the tile frame header of perimeter.wire.frames

AMSTERDAM_BBOX = (4.70, 52.25, 5.10, 52.48)


@dataclass(slots=True)
class Tally:
    frames: int = 0
    bytes: int = 0
    positions: int = 0
    latencies_ms: list[float] = field(default_factory=list)


async def sign_in(base: str) -> str:
    async with httpx.AsyncClient(base_url=base, timeout=10) as http:
        response = await http.post(
            "/v1/session", json={"username": f"viewer-{secrets.token_hex(4)}"}
        )
        response.raise_for_status()
        token: str = response.json()["token"]
        return token


async def viewer(
    base: str, token: str, bbox: tuple[float, ...], until: float, tally: Tally
) -> None:
    url = base.replace("http", "ws", 1) + f"/v1/live?token={token}"
    async with connect(url, max_size=None, compression=None, proxy=None) as ws:
        await ws.send(json.dumps({"type": "viewport", "bbox": list(bbox), "zoom": 11}))
        loop = asyncio.get_running_loop()
        while (left := until - loop.time()) > 0:
            try:
                message = await asyncio.wait_for(ws.recv(), left)
            except TimeoutError:
                break
            if isinstance(message, str):
                continue
            now_ms = time.time() * 1000
            tally.frames += 1
            tally.bytes += len(message)
            for frame in decode_bundle(message):
                # Header and timestamp column only: the tool must stay far cheaper than the
                # server it measures, even with hundreds of viewers in one process.
                _, _, kind, _, _, _, base, count = HEADER.unpack_from(frame, 0)
                if kind != FrameKind.LIVE or count == 0:  # snapshots carry older positions
                    continue
                deltas: array[int] = array("I")
                deltas.frombytes(frame[HEADER.size + 8 * count : HEADER.size + 12 * count])
                tally.positions += count
                mean_delta: float = sum(deltas) / count
                tally.latencies_ms.append(now_ms - float(base) - mean_delta)


async def run(args: argparse.Namespace) -> None:
    tally = Tally()
    users = -(-args.viewers // args.per_user)
    tokens = [await sign_in(args.url) for _ in range(users)]
    loop = asyncio.get_running_loop()
    until = loop.time() + args.duration
    tasks = [
        asyncio.create_task(
            viewer(args.url, tokens[i // args.per_user], AMSTERDAM_BBOX, until, tally)
        )
        for i in range(args.viewers)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    failed = [r for r in results if isinstance(r, BaseException)]
    seconds = args.duration
    lat = sorted(tally.latencies_ms)

    def pct(q: float) -> float:
        return lat[min(len(lat) - 1, int(q * (len(lat) - 1)))] if lat else float("nan")

    print(f"viewers        {args.viewers} ({len(failed)} failed)")
    print(
        f"delivered      {tally.positions / seconds:,.0f} positions/s in all, "
        f"{tally.positions / seconds / max(1, args.viewers - len(failed)):,.0f} per viewer"
    )
    print(
        f"bundles        {tally.frames / seconds:,.0f}/s, {tally.bytes / seconds / 1e6:,.1f} MB/s"
    )
    print(
        f"latency        p50 {pct(0.5):.0f} ms  p95 {pct(0.95):.0f} ms  p99 {pct(0.99):.0f} ms"
        f"  (device clock to socket, {len(lat):,} samples)"
    )
    if lat:
        print(f"               mean {statistics.fmean(lat):.0f} ms")
    for error in failed[:3]:
        print(f"error          {error!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--viewers", type=int, default=100)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--per-user", type=int, default=10, help="sessions per signed-in user")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
