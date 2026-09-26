import math

import pytest
from geographiclib.geodesic import Geodesic
from hypothesis import given, settings
from hypothesis import strategies as st

from perimeter.domain.envelope import Box, covers, envelope

WGS84 = Geodesic.WGS84

latitudes = st.one_of(
    st.floats(min_value=-85.0, max_value=85.0),
    st.floats(min_value=80.0, max_value=89.9999),
    st.floats(min_value=-89.9999, max_value=-80.0),
)
longitudes = st.one_of(
    st.floats(min_value=-180.0, max_value=180.0),
    st.floats(min_value=179.0, max_value=180.0),
    st.floats(min_value=-180.0, max_value=-179.0),
)
radii = st.one_of(
    st.floats(min_value=10.0, max_value=5_000.0),
    st.floats(min_value=5_000.0, max_value=100_000.0),
    st.floats(min_value=100_000.0, max_value=2_000_000.0),
)


@settings(max_examples=400, deadline=None)
@given(
    lon=longitudes,
    lat=latitudes,
    radius=radii,
    azimuths=st.lists(st.floats(-180, 180), min_size=8, max_size=8),
)
def test_every_point_of_the_circle_lies_in_the_envelope(
    lon: float, lat: float, radius: float, azimuths: list[float]
) -> None:
    boxes = envelope(lon, lat, radius)
    for azimuth in azimuths:
        for fraction in (1.0, 0.5, 1e-3):
            point = WGS84.Direct(lat, lon, azimuth, radius * fraction * (1 - 1e-12))
            assert covers(boxes, point["lon2"], point["lat2"]), (azimuth, fraction, boxes)


def test_extreme_points_of_a_mid_latitude_circle_touch_the_box_closely() -> None:
    # The envelope is conservative but should not be absurdly loose away from the poles.
    lon, lat, radius = 4.9041, 52.3676, 1_000.0
    (box,) = envelope(lon, lat, radius)
    north = WGS84.Direct(lat, lon, 0.0, radius)["lat2"]
    east = WGS84.Direct(lat, lon, 90.0, radius)["lon2"]
    assert box.north >= north
    assert box.north - north < 1e-4  # ~11 m of slack at most
    assert box.east >= east
    assert box.east - east < 1e-4


def test_antimeridian_circle_is_split_into_two_boxes() -> None:
    boxes = envelope(179.99, 10.0, 50_000.0)
    assert len(boxes) == 2
    west_part, east_part = boxes
    assert west_part.east == 180.0
    assert east_part.west == -180.0
    assert covers(boxes, -179.9, 10.0)
    assert covers(boxes, 179.9, 10.0)


def test_west_side_of_antimeridian_is_split_too() -> None:
    boxes = envelope(-179.99, -30.0, 50_000.0)
    assert len(boxes) == 2
    assert covers(boxes, 179.95, -30.0)


def test_circle_reaching_a_pole_spans_all_longitudes() -> None:
    (box,) = envelope(0.0, 89.99, 5_000.0)
    assert (box.west, box.east) == (-180.0, 180.0)
    assert box.north == 90.0


def test_zero_radius_is_a_tiny_box_around_the_centre() -> None:
    (box,) = envelope(10.0, 20.0, 0.0)
    assert box.contains(10.0, 20.0)
    assert box.east - box.west < 1e-8


@pytest.mark.parametrize("radius", [-1.0, math.inf, math.nan])
def test_invalid_radius_is_rejected(radius: float) -> None:
    with pytest.raises(ValueError, match="radius"):
        envelope(0.0, 0.0, radius)


def test_box_contains_is_inclusive() -> None:
    box = Box(0.0, 0.0, 1.0, 1.0)
    assert box.contains(0.0, 0.0)
    assert box.contains(1.0, 1.0)
    assert not box.contains(1.0000001, 0.5)
