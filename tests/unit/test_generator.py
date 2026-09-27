"""The load generator without a network: motion, schedule, wire formats and configuration."""

from __future__ import annotations

import ast
import asyncio
import io
import itertools
import json
import math
import random
import re
import statistics
import tomllib
from collections import defaultdict
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import msgspec
import pytest
from geographiclib.geodesic import Geodesic
from hypothesis import given
from hypothesis import strategies as st

import generator
from perimeter.domain.reports import LocationReport, ReportBatch
from perimeter.wire import frames as reference

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "golden" / "frames"
CENTER = (52.3676, 4.9041)
WALL0_MS = 1_790_000_000_000
TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z")


def make_fleet(
    seed: int,
    *,
    count: int = 300,
    interval: float = 1.0,
    jitter: float = 0.3,
    ramp: float = 0.0,
    radius_m: float = 3_000.0,
    stationary: float = 0.05,
) -> generator.Fleet:
    rng = random.Random(seed)
    mobility = generator.Mobility(center=CENTER, radius_m=radius_m, noise_m=4.0, rng=rng)
    return generator.Fleet(
        count=count,
        interval=interval,
        jitter=jitter,
        ramp=ramp,
        mix=generator.DEFAULT_MIX,
        stationary=stationary,
        id_prefix="",
        mobility=mobility,
        rng=rng,
    )


def emit(fleet: generator.Fleet, until: float, step: float) -> list[str]:
    reports: list[str] = []
    ticks = math.ceil(until / step)
    for tick in range(1, ticks + 1):
        fleet.emit(min(tick * step, until), WALL0_MS, reports, 1_000_000)
    return reports


def distance_m(lat0: float, lon0: float, lat1: float, lon1: float) -> float:
    return math.hypot(*generator.offset_m(lat0, lon0, lat1, lon1))


# --- motion --------------------------------------------------------------------------------------


def test_true_positions_never_leave_the_disc() -> None:
    radius = 2_000.0
    rng = random.Random(1)
    mobility = generator.Mobility(center=CENTER, radius_m=radius, noise_m=4.0, rng=rng)
    devices = [
        mobility.spawn(profile, stationary=False)
        for profile in generator.PROFILES.values()
        for _ in range(40)
    ]
    farthest = 0.0
    for _ in range(1_500):
        for device in devices:
            mobility.advance(device, rng.uniform(0.5, 4.0))
            farthest = max(farthest, mobility.distance_from_center(device.lat, device.lon))
    assert radius * 0.75 < farthest <= radius  # devices do reach the edge region, never past it
    for device in devices:  # the same bound with an independent geodesic distance
        geodesic = Geodesic.WGS84.Inverse(CENTER[0], CENTER[1], device.lat, device.lon)["s12"]
        assert geodesic <= radius * (1 + 1e-4)


@pytest.mark.parametrize("dt", [1.0, 3.7])
def test_motion_respects_speed_bounds_turn_rate_and_continuity(dt: float) -> None:
    rng = random.Random(2)
    mobility = generator.Mobility(center=CENTER, radius_m=5_000.0, noise_m=4.0, rng=rng)
    devices = [
        mobility.spawn(profile, stationary=False)
        for profile in generator.PROFILES.values()
        for _ in range(30)
    ]
    turns_seen = 0
    for _ in range(400):
        for device in devices:
            profile = device.profile
            before = (device.lat, device.lon, device.heading, device.speed)
            mobility.advance(device, dt)
            moved = distance_m(before[0], before[1], device.lat, device.lon)
            # No teleports. The 1e-5 slack covers integrating with the scale at the start
            # latitude while measuring at the mid latitude (a fraction of a millimetre here).
            assert moved <= profile.max_speed * dt * (1 + 1e-5)
            if device.speed > 0:
                assert profile.min_speed <= device.speed <= profile.max_speed
            else:
                assert device.pause > 0 or device.legs == 0
            if before[3] > 0 and device.speed > 0:
                turn = abs((device.heading - before[2] + 180.0) % 360.0 - 180.0)
                assert turn <= profile.turn_rate * dt + 1e-6  # smooth heading
                turns_seen += turn > 1.0
    assert turns_seen > 100  # the paths do curve


def test_stationary_devices_stay_put_and_report_no_heading() -> None:
    fleet = make_fleet(3, count=200, stationary=1.0)
    starts = [(device.lat, device.lon) for device in fleet.devices]
    reports = [json.loads(text) for text in emit(fleet, 20.0, 0.5)]
    assert all(report["speed"] == 0 and "heading" not in report for report in reports)
    assert [(d.lat, d.lon) for d in fleet.devices] == starts


def test_gps_noise_is_gaussian_with_the_configured_sigma() -> None:
    rng = random.Random(4)
    mobility = generator.Mobility(center=CENTER, radius_m=5_000.0, noise_m=4.0, rng=rng)
    device = generator.Device(generator.PROFILES["pedestrian"], *CENTER, stationary=True)
    offsets = [generator.offset_m(*CENTER, *mobility.fix(device)) for _ in range(20_000)]
    for axis in zip(*offsets, strict=True):
        assert abs(statistics.fmean(axis)) < 0.15  # 5 standard errors of the mean
        assert statistics.pstdev(axis) == pytest.approx(4.0, rel=0.04)
    within_one_sigma = sum(math.hypot(e, n) <= 4.0 for e, n in offsets) / len(offsets)
    assert within_one_sigma == pytest.approx(
        1 - math.exp(-0.5), abs=0.02
    )  # Rayleigh CDF at 1 sigma


def test_zero_noise_reports_the_true_position() -> None:
    mobility = generator.Mobility(center=CENTER, radius_m=5_000.0, noise_m=0.0, rng=random.Random())
    device = generator.Device(generator.PROFILES["vehicle"], *CENTER, stationary=True)
    assert mobility.fix(device) == CENTER


def test_geodesy_helpers_agree_with_wgs84() -> None:
    rng = random.Random(5)
    for _ in range(300):
        lat, lon = rng.uniform(-60.0, 70.0), rng.uniform(-170.0, 170.0)
        east, north = rng.uniform(-15_000, 15_000), rng.uniform(-15_000, 15_000)
        lat1, lon1 = generator.displace(lat, lon, east, north)
        back = generator.offset_m(lat, lon, lat1, lon1)
        assert back == pytest.approx((east, north), abs=1e-3)
        exact = Geodesic.WGS84.Inverse(lat, lon, lat1, lon1)["s12"]
        assert math.hypot(east, north) == pytest.approx(exact, rel=5e-4)


def test_viewport_covers_the_whole_disc() -> None:
    for center in [CENTER, (-33.87, 151.21), (64.13, -21.9)]:
        radius = 12_000.0
        (west, south, east, north), zoom = generator.viewport(center, radius)
        for bearing in range(0, 360, 5):
            theta = math.radians(bearing)
            lat, lon = generator.displace(
                *center, radius * math.sin(theta), radius * math.cos(theta)
            )
            assert west <= lon <= east
            assert south <= lat <= north
        assert 8 <= zoom <= 14


# --- schedule and reports ------------------------------------------------------------------------


def test_same_seed_replays_the_same_reports_whatever_the_clock_does() -> None:
    fine = emit(make_fleet(7), 30.0, 0.01)
    coarse = emit(make_fleet(7), 30.0, 0.37)
    assert fine == coarse  # report content and order do not depend on when the clock ticks
    assert len(fine) > 8_000
    assert emit(make_fleet(8), 30.0, 0.37) != fine


def test_reports_validate_against_the_ingest_schema() -> None:
    reports = emit(make_fleet(11, count=500), 10.0, 0.25)
    batch = msgspec.json.decode(
        ('{"reports":[' + ",".join(reports) + "]}").encode(), type=ReportBatch
    )
    assert len(batch.reports) == len(reports)
    for text, report in zip(reports, batch.reports, strict=True):
        raw = json.loads(text)
        assert set(raw) <= {"device_id", "latitude", "longitude", "timestamp", "speed", "heading"}
        assert TIMESTAMP.fullmatch(raw["timestamp"])
        single = msgspec.json.decode(text.encode(), type=LocationReport)
        assert single == report
        assert WALL0_MS <= report.recorded_at_ms(0) <= WALL0_MS + 10_000
        assert report.speed is not None
        if report.speed > 0:
            assert report.heading is not None
            assert 0.0 <= report.heading < 360.0
        else:
            assert report.heading is None
    ids = {report.device_id for report in batch.reports}
    assert {device_id.split("-")[0] for device_id in ids} == {"veh", "bike", "ped"}


def test_intervals_are_jittered_and_the_ramp_spreads_the_start() -> None:
    count, interval, ramp = 3_000, 3.0, 15.0
    fleet = make_fleet(12, count=count, interval=interval, ramp=ramp)
    per_device: defaultdict[str, list[int]] = defaultdict(list)
    for text in emit(fleet, 40.0, 0.1):
        report = json.loads(text)
        stamp = datetime.fromisoformat(report["timestamp"])
        per_device[report["device_id"]].append(round(stamp.timestamp() * 1000) - WALL0_MS)
    firsts = [stamps[0] for stamps in per_device.values()]
    assert len(firsts) == count
    assert min(firsts) >= 0
    assert max(firsts) <= ramp * 1000
    gaps = [b - a for stamps in per_device.values() for a, b in itertools.pairwise(stamps)]
    assert min(gaps) >= interval * 0.7 * 1000 - 1
    assert max(gaps) <= interval * 1.3 * 1000 + 1
    assert statistics.fmean(gaps) == pytest.approx(interval * 1000, rel=0.02)

    def rate(start: float, end: float) -> float:
        hits = sum(start * 1000 <= t < end * 1000 for s in per_device.values() for t in s)
        return hits / (end - start)

    assert rate(0, 5) < rate(10, 15)  # ramping up
    assert rate(21, 39) == pytest.approx(count / interval, rel=0.05)  # full rate afterwards


def test_device_ids_carry_prefix_profile_and_a_unique_number() -> None:
    rng = random.Random(0)
    mobility = generator.Mobility(center=CENTER, radius_m=3_000.0, noise_m=4.0, rng=rng)
    fleet = generator.Fleet(
        count=120,
        interval=1.0,
        jitter=0.0,
        ramp=0.0,
        mix={"cyclist": 1.0},
        stationary=0.0,
        id_prefix="gen2_",
        mobility=mobility,
        rng=rng,
    )
    assert fleet.ids[:2] == ["gen2_bike-00000", "gen2_bike-00001"]
    assert len(set(fleet.ids)) == len(fleet)


# --- measurement ---------------------------------------------------------------------------------


@given(st.lists(st.floats(0.01, 1e6), min_size=1, max_size=400), st.floats(0.0, 1.0))
def test_histogram_percentiles_are_within_one_percent_of_nearest_rank(
    values: list[float], q: float
) -> None:
    histogram = generator.Histogram()
    for value in values:
        histogram.record(value)
    exact = sorted(values)[max(1, math.ceil(q * len(values))) - 1]
    assert histogram.percentile(q) == pytest.approx(exact, rel=0.0101)


def test_histogram_edges_merge_and_clear() -> None:
    histogram = generator.Histogram()
    assert histogram.percentile(0.5) is None
    for value in (0.0, 0.001, 0.004, -3.0):
        histogram.record(value)
    assert 0.0 <= (histogram.percentile(0.99) or 0.0) <= 0.004
    other = generator.Histogram()
    other.record(250.0)
    histogram.merge(other)
    assert histogram.count == 5
    assert histogram.percentile(1.0) == pytest.approx(250.0, rel=0.01)
    assert histogram.summary()["max"] == 250.0
    histogram.clear()
    assert histogram.count == 0
    assert histogram.summary() == {"count": 0, "p50": None, "p95": None, "p99": None, "max": None}


def test_latency_window_rolls_into_the_total() -> None:
    latency = generator.Latency()
    for value in range(1, 101):
        latency.record(float(value))
    p50, p95, p99 = latency.roll()
    assert p50 == pytest.approx(50, rel=0.01)
    assert p95 == pytest.approx(95, rel=0.01)
    assert p99 == pytest.approx(99, rel=0.01)
    assert latency.roll() == (None, None, None)
    assert latency.total.count == 100


def test_receipts_settle_every_report() -> None:
    stats, console = generator.Stats(), generator.Console(io.StringIO(), io.StringIO())
    stats.offered = 10
    accepted, codes = generator.parse_receipt(
        {"accepted": 7, "rejected": [{"index": 1, "code": "timestamp_in_future"}, "odd"]}, 10
    )
    generator.settle(stats, console, 10, accepted, codes)
    assert stats.accepted == 7
    assert stats.rejected_codes == {"timestamp_in_future": 1, "rejected": 1, "unacknowledged": 1}
    assert stats.unsettled == 0
    assert generator.parse_receipt(None, 5) == (5, [])
    assert generator.parse_receipt({"rejected": [{"code": "x"}]}, 5) == (4, ["x"])


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("5", 5.0),
        (" 7 ", 7.0),
        ("0", 0.0),
        ("1.5", 1.5),
        ("99999", generator.RETRY_AFTER_CAP_S),
        ("inf", generator.RETRY_AFTER_CAP_S),
        ("-3", None),
        ("nan", None),
        ("soon", None),
        ("", None),
        (None, None),
    ],
)
def test_retry_after_in_seconds(header: str | None, expected: float | None) -> None:
    assert generator.parse_retry_after(header) == expected


def test_retry_after_as_an_http_date() -> None:
    now = 1_790_000_000.0
    future = format_datetime(datetime.fromtimestamp(now + 12, UTC), usegmt=True)
    past = format_datetime(datetime.fromtimestamp(now - 30, UTC), usegmt=True)
    assert generator.parse_retry_after(future, now=now) == pytest.approx(12.0)
    assert generator.parse_retry_after(past, now=now) == 0.0
    assert generator.parse_retry_after("Wed, 99 Foo 2026 00:00:00 GMT", now=now) is None


def test_backoff_grows_with_jitter_up_to_the_cap() -> None:
    backoff = generator.Backoff(0.25, 4.0, random.Random(6))
    delays = [backoff.next() for _ in range(10)]
    for attempt, delay in enumerate(delays):
        ceiling = min(4.0, 0.25 * 2**attempt)
        assert ceiling / 2 <= delay <= ceiling
    backoff.reset()
    assert backoff.next() <= 0.25


async def test_queue_drops_the_oldest_batches_when_full() -> None:
    stats = generator.Stats()
    queue = generator.BatchQueue(250, stats)
    for index in range(4):
        queue.put([f"r{index}-{n}" for n in range(100)])
    assert queue.size == 200
    assert stats.dropped == {"overflow": 200}
    first = await queue.get()
    assert first is not None
    assert first[0] == "r2-0"
    queue.close()
    assert await queue.get() is not None
    assert await queue.get() is None


async def test_queue_never_loses_a_batch_to_a_cancelled_getter() -> None:
    queue = generator.BatchQueue(1_000, generator.Stats())
    getters = [asyncio.create_task(queue.get()) for _ in range(3)]
    await asyncio.sleep(0)  # all three are waiting
    queue.put(["only"])  # wakes the first getter...
    getters[0].cancel()  # ...which is cancelled before it can take the batch
    done, _ = await asyncio.wait(getters[1:], timeout=1, return_when=asyncio.FIRST_COMPLETED)
    assert [task.result() for task in done] == [["only"]]
    queue.close()
    results = await asyncio.gather(*getters, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert sorted(map(str, results[1:])) == ["None", "['only']"]


# --- position frames -----------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["amsterdam", "edge_values"])
def test_frame_decoder_reads_the_golden_vectors(name: str) -> None:
    expected = json.loads((GOLDEN / f"{name}.json").read_text())
    frame = generator.decode_tile((GOLDEN / f"{name}.bin").read_bytes())
    zoom, x, y = expected["tile"]
    assert (frame.kind, frame.zoom, frame.x, frame.y) == (expected["kind"], zoom, x, y)
    for point, want in zip(frame.points(), expected["points"], strict=True):
        device_id, lat, lon, recorded_at_ms, speed, heading = point
        assert device_id == want["device_id"]
        assert lat == pytest.approx(want["lat"], abs=5e-8)
        assert lon == pytest.approx(want["lon"], abs=5e-8)
        assert recorded_at_ms == want["recorded_at_ms"]
        for got, sent in ((speed, want["speed_mps"]), (heading, want["heading_deg"])):
            if sent is None:
                assert got is None
            else:
                assert got == pytest.approx(sent, abs=0.005)


def test_bundles_split_into_the_frames_they_carry() -> None:
    golden = [(GOLDEN / f"{name}.bin").read_bytes() for name in ("amsterdam", "edge_values")]
    frames = generator.decode_bundle(reference.encode_bundle(golden))
    assert [bytes(frame) for frame in frames] == golden
    assert generator.decode_bundle(reference.encode_bundle([])) == []


points = st.builds(
    reference.FramePoint,
    device_id=st.text(
        alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-",
        min_size=1,
        max_size=64,
    ),
    lat=st.floats(-90, 90),
    lon=st.floats(-180, 180),
    recorded_at_ms=st.integers(1_600_000_000_000, 1_600_000_000_000 + 2**31),
    speed_mps=st.one_of(st.none(), st.floats(0, 600)),
    heading_deg=st.one_of(st.none(), st.floats(0, 359.99)),
)


@given(batch=st.lists(points, max_size=40), kind=st.sampled_from(reference.FrameKind))
def test_frame_decoder_agrees_with_the_reference_codec(
    batch: list[reference.FramePoint], kind: reference.FrameKind
) -> None:
    data = reference.encode_tile(kind, 12, 2105, 1346, batch)
    ours = generator.decode_tile(data)
    theirs = reference.decode_tile(data)
    assert (ours.kind, ours.zoom, ours.x, ours.y) == (theirs.kind, 12, 2105, 1346)
    assert ours.points() == [
        (p.device_id, p.lat, p.lon, p.recorded_at_ms, p.speed_mps, p.heading_deg)
        for p in theirs.points
    ]


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\xb7\x01",
        b"\x00" * 24,
        b"\xb7\x02" + b"\x00" * 22,
        b"\xb7\x01\x01\x0c" + b"\x00" * 16 + (5).to_bytes(4, "little"),
        reference.encode_tile(
            reference.FrameKind.LIVE, 3, 1, 1, [reference.FramePoint("abc", 1, 2, 5)]
        )[:30],
    ],
)
def test_malformed_frames_are_rejected(data: bytes) -> None:
    with pytest.raises(generator.FrameError):
        generator.decode_tile(data)


def test_malformed_bundles_are_rejected() -> None:
    bundle = reference.encode_bundle([(GOLDEN / "amsterdam.bin").read_bytes()])
    for data in (b"", bundle[:-4], b"\x00\x01\x00\x00", bundle[:6]):
        with pytest.raises(generator.FrameError):
            generator.decode_bundle(data)


# --- configuration -------------------------------------------------------------------------------


def test_flags_beat_the_environment_which_beats_the_defaults() -> None:
    environ = {
        "GENERATOR_DEVICES": "123",
        "GENERATOR_INTERVAL": "2.5",
        "GENERATOR_TOKEN": "from-env",
        "GENERATOR_TRANSPORT": "ws",
        "GENERATOR_CENTER": "48.8566,2.3522",
        "GENERATOR_MIX": "vehicle=3,pedestrian=1",
        "GENERATOR_OBSERVE": "Demo",
        "GENERATOR_ZONES": "4",
        "GENERATOR_KEEP_ZONES": "yes",
        "GENERATOR_JSON": "~/summary.json",
        "GENERATOR_SEED": "",  # empty means unset
    }
    config = generator.parse_config(["--devices", "456", "--url", "https://edge.test/"], environ)
    assert config.devices == 456
    assert config.url == "https://edge.test"
    assert config.interval == 2.5
    assert config.transport == "ws"
    assert config.center == (48.8566, 2.3522)
    assert config.mix == {"vehicle": 0.75, "pedestrian": 0.25}
    assert config.observe == "demo"
    assert config.zones == 4
    assert config.keep_zones is True
    assert config.json_path == Path("~/summary.json").expanduser()
    assert config.jitter == 0.3  # built-in default
    assert 0 <= config.seed < 2**32  # chosen at random, reported at start
    overridden = generator.parse_config(["--no-keep-zones", "--token", "flag"], environ)
    assert overridden.keep_zones is False
    assert overridden.token == "flag"


def test_help_documents_every_flag_and_its_environment_variable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exited:
        generator.parse_config(["--help"], {})
    assert exited.value.code == 0
    text = capsys.readouterr().out
    for option in generator.OPTIONS:
        assert option.flag in text
        assert option.env in text
    assert "exit status" in text


def test_token_can_come_from_a_file(tmp_path: Path) -> None:
    secret = tmp_path / "ingest_token"
    secret.write_text("from-file\n")
    assert generator.parse_config(["--token-file", str(secret)], {}).token == "from-file"
    from_env = generator.parse_config([], {"GENERATOR_TOKEN_FILE": str(secret)})
    assert from_env.token == "from-file"
    flag_wins = generator.parse_config(["--token", "flag"], {"GENERATOR_TOKEN_FILE": str(secret)})
    assert flag_wins.token == "flag"
    file_flag_wins = generator.parse_config(
        ["--token-file", str(secret)], {"GENERATOR_TOKEN": "env"}
    )
    assert file_flag_wins.token == "from-file"


@pytest.mark.parametrize(
    ("argv", "environ", "message"),
    [
        ([], {}, "ingest token is required"),
        (["--token", "t", "--token-file", "x"], {}, "not both"),
        ([], {"GENERATOR_TOKEN": "t", "GENERATOR_TOKEN_FILE": "x"}, "not both"),
        (["--token-file", "/nonexistent/token"], {}, "cannot read the token file"),
        (["--token", "t"], {"GENERATOR_DEVICES": "many"}, "GENERATOR_DEVICES"),
        (["--token", "t", "--devices", "0"], {}, "between 1"),
        (["--token", "t", "--center", "91,0"], {}, "not a valid latitude"),
        (["--token", "t", "--center", "52.3"], {}, "LAT,LON"),
        (["--token", "t", "--mix", "bus=1"], {}, "unknown profile"),
        (["--token", "t", "--mix", "vehicle=0"], {}, "positive total"),
        (["--token", "t", "--zones", "3"], {}, "--zones needs --observe"),
        (["--token", "t", "--observe", "x"], {}, "usernames"),
        (["--token", "t", "--radius-km", "0.5"], {}, "between 1"),
        (["--token", "t", "--center", "0,179.95"], {}, "antimeridian"),
        (["--token", "t", "--center", "84.9,0"], {}, "degrees of the equator"),
        (["--token", "t", "--batch", "500", "--buffer", "100"], {}, "at least one batch"),
        (["--token", "t", "--url", "ftp://x"], {}, "http"),
        (["--token", "t", "--id-prefix", "no spaces"], {}, "up to 32"),
        (["--token", "t"], {"GENERATOR_KEEP_ZONES": "maybe"}, "true/false"),
    ],
)
def test_invalid_settings_are_usage_errors(
    argv: list[str],
    environ: dict[str, str],
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exited:
        generator.parse_config(argv, environ)
    assert exited.value.code == 2
    assert message in capsys.readouterr().err


def test_demo_zones_are_valid_and_deterministic() -> None:
    zones = generator.plan_zones(12, CENTER, 12_000.0, seed=9)
    assert zones == generator.plan_zones(12, CENTER, 12_000.0, seed=9)
    assert zones != generator.plan_zones(12, CENTER, 12_000.0, seed=10)
    for zone in zones:
        assert 1 <= len(zone["name"]) <= 80
        assert 10 <= zone["radius_m"] <= 100_000
        assert re.fullmatch(r"#[0-9a-f]{6}", zone["color"])
        assert zone["dwell_s"] is None or 10 <= zone["dwell_s"] <= 86_400
        assert distance_m(*CENTER, zone["center"]["lat"], zone["center"]["lon"]) <= 12_000 * 0.86
    assert any(zone["dwell_s"] for zone in zones)


def test_summary_renders_every_section() -> None:
    summary: dict[str, Any] = {
        "active_s": 60.0,
        "elapsed_s": 61.5,
        "stop_reason": "duration",
        "exit_code": 0,
        "config": {"seed": 1},
        "reports": {
            "offered": 10,
            "sent": 10,
            "resent": 2,
            "accepted": 8,
            "rejected": 1,
            "dropped": 1,
            "unsettled": 0,
        },
        "rates_per_s": {"offered": 0.2, "accepted": 0.1},
        "rejected_by_code": {"timestamp_in_future": 1},
        "dropped_by_reason": {"overflow": 1},
        "throttled": 3,
        "errors": 1,
        "errors_by_kind": {"timeout": 1},
        "ack_latency_ms": {"count": 2, "p50": 1.0, "p95": 2.0, "p99": 2.0, "max": 2.0},
        "scheduler_lag_ms": {"count": 0, "p50": None, "p95": None, "p99": None, "max": None},
        "process": {"cpu_percent": 12.5, "max_rss_mb": 80.0},
        "observe": {
            "e2e_latency_ms": {"count": 1, "p50": 120.0, "p95": 120.0, "p99": 120.0, "max": 120.0},
            "snapshot_positions": 4,
            "alerts": {"enter": 2},
            "alert_latency_ms": {
                "count": 2,
                "p50": 300.0,
                "p95": 300.0,
                "p99": 300.0,
                "max": 300.0,
            },
        },
    }
    text = "\n".join(generator.render_summary(summary))
    for fragment in (
        "balanced",
        "timestamp_in_future 1",
        "overflow 1",
        "timeout 1",
        "e2e        p50 120 ms",
        "alerts     2 (enter 2)",
        "cpu 12.5%",
    ):
        assert fragment in text


# --- packaging -----------------------------------------------------------------------------------


def test_script_stays_runnable_on_python_311() -> None:
    source = (ROOT / "generator.py").read_text()
    ast.parse(source, feature_version=(3, 11))
    block = re.search(r"^# /// script\n((?:#.*\n)+?)^# ///$", source, flags=re.MULTILINE)
    assert block is not None
    metadata = tomllib.loads(
        "\n".join(line.removeprefix("#").removeprefix(" ") for line in block[1].splitlines())
    )
    assert metadata["requires-python"] == ">=3.11"
    assert any(dep.startswith("aiohttp") for dep in metadata["dependencies"])
