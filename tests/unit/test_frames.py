import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from perimeter.wire.frames import (
    FrameError,
    FrameKind,
    FramePoint,
    decode_bundle,
    decode_tile,
    encode_bundle,
    encode_tile,
)

GOLDEN = Path(__file__).parent.parent / "golden" / "frames"

points = st.builds(
    FramePoint,
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


@given(batch=st.lists(points, max_size=50), kind=st.sampled_from(FrameKind))
def test_tile_frames_round_trip_within_quantisation(
    batch: list[FramePoint], kind: FrameKind
) -> None:
    data = encode_tile(kind, 12, 2105, 1346, batch)
    assert len(data) % 4 == 0
    frame = decode_tile(data)
    assert (frame.kind, frame.zoom, frame.x, frame.y) == (kind, 12, 2105, 1346)
    assert len(frame.points) == len(batch)
    for got, sent in zip(frame.points, batch, strict=True):
        assert got.device_id == sent.device_id
        assert abs(got.lat - sent.lat) <= 5e-8
        assert abs(got.lon - sent.lon) <= 5e-8
        assert got.recorded_at_ms == sent.recorded_at_ms
        if sent.speed_mps is None:
            assert got.speed_mps is None
        else:
            assert got.speed_mps is not None
            assert abs(got.speed_mps - sent.speed_mps) <= 0.005 + 1e-9
        if sent.heading_deg is None:
            assert got.heading_deg is None
        else:
            assert got.heading_deg is not None
            assert abs(got.heading_deg - sent.heading_deg) % 360 <= 0.005 + 1e-9


def test_arrays_start_on_four_byte_boundaries() -> None:
    frame = encode_tile(FrameKind.LIVE, 12, 1, 2, [FramePoint("a", 1.0, 2.0, 1_000)] * 3)
    # header (24) + 3 arrays of int32 (36) + 2 arrays of uint16 (12) = 72, id blob length at 72
    assert int.from_bytes(frame[72:76], "little") == len(b"a\x00a\x00a")
    assert len(frame) == 76 + 8


def test_bundles_carry_frames_unchanged() -> None:
    frames = [
        encode_tile(FrameKind.LIVE, 12, 1, 1, [FramePoint("a", 1.0, 1.0, 1)]),
        encode_tile(FrameKind.SNAPSHOT, 12, 1, 2, []),
    ]
    bundle = encode_bundle(frames)
    assert [bytes(f) for f in decode_bundle(bundle)] == frames
    assert decode_tile(decode_bundle(bundle)[1]).points == ()


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\xb7\x01",
        b"\x00" * 24,  # wrong magic
        b"\xb7\x02" + b"\x00" * 22,  # unknown version
        b"\xb7\x01\x01\x0c" + b"\x00" * 16 + (5).to_bytes(4, "little"),  # arrays missing
    ],
)
def test_malformed_frames_are_rejected(data: bytes) -> None:
    with pytest.raises(FrameError):
        decode_tile(data)


def test_truncated_frame_is_rejected() -> None:
    data = encode_tile(FrameKind.LIVE, 3, 1, 1, [FramePoint("abc", 1.0, 2.0, 5)])
    with pytest.raises(FrameError):
        decode_tile(data[:30])


def test_truncated_bundle_is_rejected() -> None:
    bundle = encode_bundle([encode_tile(FrameKind.LIVE, 3, 1, 1, [FramePoint("a", 0, 0, 0)])])
    with pytest.raises(FrameError):
        decode_bundle(bundle[:-4])
    with pytest.raises(FrameError):
        decode_bundle(b"\x00\x01\x00\x00")


@pytest.mark.parametrize("name", ["amsterdam", "edge_values"])
def test_golden_vectors_are_stable(name: str) -> None:
    """The browser decoder is tested against the same files, so both sides agree byte-for-byte."""
    expected = json.loads((GOLDEN / f"{name}.json").read_text())
    data = (GOLDEN / f"{name}.bin").read_bytes()
    points_in = [FramePoint(**p) for p in expected["points"]]
    zoom, x, y = expected["tile"]
    assert encode_tile(FrameKind(expected["kind"]), zoom, x, y, points_in) == data
    frame = decode_tile(data)
    assert [p.device_id for p in frame.points] == [p["device_id"] for p in expected["points"]]
