import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from perimeter.domain.tiles import (
    MAX_MERCATOR_LAT,
    BBox,
    Tile,
    ancestors,
    covering_quadkeys,
    quadkey,
    quadkey_for,
    quadkey_of_subject,
    subject_for,
    subscription_for,
    tile_bounds,
    tile_for,
    tile_of_quadkey,
)

lons = st.floats(min_value=-180.0, max_value=180.0)
lats = st.floats(min_value=-MAX_MERCATOR_LAT, max_value=MAX_MERCATOR_LAT)
zooms = st.integers(min_value=0, max_value=16)


def test_quadkey_matches_the_published_reference_example() -> None:
    # Tile (3, 5) at level 3 is quadkey "213" in the Bing Maps tile system documentation.
    assert quadkey(Tile(3, 5, 3)) == "213"
    assert tile_of_quadkey("213") == Tile(3, 5, 3)


def test_world_tile_is_the_empty_quadkey() -> None:
    assert tile_for(12.3, 45.6, 0) == Tile(0, 0, 0)
    assert quadkey(Tile(0, 0, 0)) == ""


@given(lon=lons, lat=lats, zoom=zooms)
def test_a_point_lies_inside_the_bounds_of_its_tile(lon: float, lat: float, zoom: int) -> None:
    bounds = tile_bounds(tile_for(lon, lat, zoom))
    assert bounds.west - 1e-9 <= lon <= bounds.east + 1e-9
    assert bounds.south - 1e-9 <= lat <= bounds.north + 1e-9


@given(x=st.integers(0, 4095), y=st.integers(0, 4095))
def test_quadkey_round_trips(x: int, y: int) -> None:
    tile = Tile(x, y, 12)
    assert tile_of_quadkey(quadkey(tile)) == tile


@given(lon=lons, lat=lats)
def test_deeper_quadkeys_refine_their_parents(lon: float, lat: float) -> None:
    deep = quadkey_for(lon, lat, 12)
    for zoom in range(12):
        assert deep.startswith(quadkey_for(lon, lat, zoom))


def test_subjects_and_subscriptions() -> None:
    assert subject_for("0123") == "pos.0.1.2.3"
    assert quadkey_of_subject("pos.0.1.2.3") == "0123"
    assert subscription_for("01", leaf_zoom=4) == "pos.0.1.>"
    assert subscription_for("0123", leaf_zoom=4) == "pos.0.1.2.3"
    assert subscription_for("", leaf_zoom=4) == "pos.>"
    with pytest.raises(ValueError, match="deeper"):
        subscription_for("01234", leaf_zoom=4)
    with pytest.raises(ValueError, match="position subject"):
        quadkey_of_subject("tlm.0.dev")
    with pytest.raises(ValueError, match="quadkey digit"):
        tile_of_quadkey("0149")


def test_ancestors_go_from_root_to_leaf() -> None:
    assert list(ancestors("012")) == ["", "0", "01", "012"]


def _covered(keys: list[str], lon: float, lat: float) -> bool:
    return any(quadkey_for(lon, lat, len(key)) == key for key in keys)


@given(
    west=lons,
    south=lats,
    width=st.floats(min_value=0.0, max_value=40.0),
    height=st.floats(min_value=0.0, max_value=20.0),
    fx=st.floats(0, 1),
    fy=st.floats(0, 1),
    max_tiles=st.integers(1, 32),
)
def test_covering_quadkeys_cover_every_point_of_the_box(
    west: float,
    south: float,
    width: float,
    height: float,
    fx: float,
    fy: float,
    max_tiles: int,
) -> None:
    north = min(south + height, MAX_MERCATOR_LAT)
    east = west + width
    assume(east <= 180.0)
    keys = covering_quadkeys(BBox(west, south, east, north), max_tiles=max_tiles, max_zoom=12)
    assert len(keys) <= max(max_tiles, 1)
    assert keys == sorted(set(keys))
    lon = west + (east - west) * fx
    lat = south + (north - south) * fy
    assert _covered(keys, lon, lat)


def test_small_viewport_gets_deep_tiles() -> None:
    keys = covering_quadkeys(BBox(4.88, 52.36, 4.90, 52.37), max_tiles=16, max_zoom=12)
    assert all(len(key) == 12 for key in keys)
    assert 1 <= len(keys) <= 4


def test_whole_world_collapses_to_a_shallow_level() -> None:
    keys = covering_quadkeys(BBox(-180, -85, 180, 85), max_tiles=16, max_zoom=12)
    assert keys == ["00", "01", "02", "03", "10", "11", "12", "13",
                    "20", "21", "22", "23", "30", "31", "32", "33"]  # fmt: skip


def test_a_single_tile_budget_can_fall_back_to_the_root() -> None:
    assert covering_quadkeys(BBox(-10, -10, 10, 10), max_tiles=1, max_zoom=12) == [""]


@pytest.mark.parametrize(
    "bbox",
    [
        BBox(170.0, -10.0, -170.0, 10.0),  # wrapped representation
        BBox(170.0, -10.0, 190.0, 10.0),  # unwrapped representation (map panned east)
    ],
)
def test_antimeridian_viewports_cover_both_sides(bbox: BBox) -> None:
    keys = covering_quadkeys(bbox, max_tiles=16, max_zoom=12)
    assert _covered(keys, 175.0, 0.0)
    assert _covered(keys, -175.0, 0.0)
    assert not _covered(keys, 0.0, 0.0)


def test_invalid_budget_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_tiles"):
        covering_quadkeys(BBox(0, 0, 1, 1), max_tiles=0, max_zoom=12)
