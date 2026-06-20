"""Tests for obstacle_heights.py — Phase 3 (building height heuristics)."""

from __future__ import annotations

import pytest
from shapely.geometry import box

from flightmanager.buildings import Building
from flightmanager.obstacle_heights import (
    _DEFAULT_HEIGHT_M,
    _FLOOR_HEIGHT_M,
    _KOHDELUOKKA_HEIGHT_M,
    building_height_m,
    compute_altitude_profile,
)


# ---------------------------------------------------------------------------
# building_height_m
# ---------------------------------------------------------------------------


def _building(kohdeluokka: int, kerrosluku: int | None = None) -> Building:
    return Building(
        mtk_id=1,
        kohdeluokka=kohdeluokka,
        kayttotarkoitus=None,
        geometry=box(300000, 6900000, 300050, 6900050),
        alkupvm=None,
        kerrosluku=kerrosluku,
    )


class TestBuildingHeightM:
    def test_kerrosluku_takes_priority(self):
        b = _building(42211, kerrosluku=4)
        assert building_height_m(b) == pytest.approx(4 * _FLOOR_HEIGHT_M)

    def test_kerrosluku_zero_falls_back_to_heuristic(self):
        b = _building(42211, kerrosluku=0)
        assert building_height_m(b) == _KOHDELUOKKA_HEIGHT_M[42211]

    def test_kerrosluku_none_falls_back(self):
        b = _building(42211, kerrosluku=None)
        assert building_height_m(b) == _KOHDELUOKKA_HEIGHT_M[42211]

    def test_residential_heuristic(self):
        assert building_height_m(_building(42210)) == pytest.approx(7.0)
        assert building_height_m(_building(42211)) == pytest.approx(7.0)
        assert building_height_m(_building(42212)) == pytest.approx(10.0)  # 3+ floors

    def test_holiday_heuristic(self):
        assert building_height_m(_building(42230)) == pytest.approx(4.0)
        assert building_height_m(_building(42231)) == pytest.approx(4.0)

    def test_industrial_heuristic(self):
        assert building_height_m(_building(42240)) == pytest.approx(15.0)
        assert building_height_m(_building(42242)) == pytest.approx(15.0)

    def test_agricultural_heuristic(self):
        assert building_height_m(_building(42260)) == pytest.approx(10.0)
        assert building_height_m(_building(42262)) == pytest.approx(10.0)

    def test_unknown_kohdeluokka_returns_default(self):
        assert building_height_m(_building(99999)) == pytest.approx(_DEFAULT_HEIGHT_M)


# ---------------------------------------------------------------------------
# compute_altitude_profile — height-aware d_effective
# ---------------------------------------------------------------------------


class _MockDrone:
    focal_length_mm = 12.3
    sensor_w_mm = 5280 * 3.3 / 1000  # ≈ 17.424 mm
    sensor_h_mm = 3956 * 3.3 / 1000

    def auto_speed(self, h, ovlp):
        return 5.0

    def gsd_from_height(self, h):
        return h * 3.3 / 12.3 / 10


class _MockRoute:
    def __init__(self, strips):
        # strips: list of (x1,y1,x2,y2) in EPSG:3067
        self.strips_3067 = strips
        self.transit_segs_3067 = []


class TestAltitudeProfileWithHeights:
    def test_no_buildings_returns_flight_height(self):
        route = _MockRoute([(300000, 6900200, 300100, 6900200)])
        result = compute_altitude_profile(
            route,
            [],
            [],
            flight_height_m=74.5,
            min_h=30.0,
            powerline_clearance_m=70.0,
            overlap_front_pct=80.0,
            overlap_side_pct=70.0,
            slope_f=0.20,
            drone=_MockDrone(),
        )
        assert result == [pytest.approx(74.5)]

    def test_near_building_altitude_uses_d_plus_height(self):
        # Building centred at (300100, 6900000) → 200 m from strip midpoint
        # Strip midpoint at (300100, 6900200)
        # Horizontal distance ≈ 200 m; building height = 7 m (kohdeluokka 42211)
        # d_eff ≈ 207 → clamped to flight_height_m=200 if flight_height_m=200
        building = Building(
            mtk_id=1,
            kohdeluokka=42211,
            kayttotarkoitus=None,
            geometry=box(300075, 6899975, 300125, 6900025),
            alkupvm=None,
            kerrosluku=None,
        )
        route = _MockRoute([(300050, 6900200, 300150, 6900200)])
        result = compute_altitude_profile(
            route,
            [building],
            [],
            flight_height_m=250.0,
            min_h=30.0,
            powerline_clearance_m=70.0,
            overlap_front_pct=80.0,
            overlap_side_pct=70.0,
            slope_f=0.20,
            drone=_MockDrone(),
        )
        # d ≈ 175 m (from midpoint to building edge), bh = 7 → d_eff ≈ 182 → h ≈ 182
        assert result[0] > 30.0
        assert result[0] < 250.0

    def test_tall_building_requires_more_altitude_than_short(self):
        # Same horizontal distance, compare industrial (15 m) vs residential (7 m)
        mid_y = 6900300.0
        bldg_geom = box(
            300075, 6900075, 300125, 6900125
        )  # centred ~175 m south of strip

        residential = Building(
            mtk_id=1,
            kohdeluokka=42211,
            kayttotarkoitus=None,
            geometry=bldg_geom,
            alkupvm=None,
            kerrosluku=None,
        )
        industrial = Building(
            mtk_id=2,
            kohdeluokka=42241,
            kayttotarkoitus=None,
            geometry=bldg_geom,
            alkupvm=None,
            kerrosluku=None,
        )
        route = _MockRoute([(300050, mid_y, 300150, mid_y)])

        h_res = compute_altitude_profile(
            route,
            [residential],
            [],
            flight_height_m=300.0,
            min_h=10.0,
            powerline_clearance_m=70.0,
            overlap_front_pct=80.0,
            overlap_side_pct=70.0,
            slope_f=0.20,
            drone=_MockDrone(),
        )[0]
        h_ind = compute_altitude_profile(
            route,
            [industrial],
            [],
            flight_height_m=300.0,
            min_h=10.0,
            powerline_clearance_m=70.0,
            overlap_front_pct=80.0,
            overlap_side_pct=70.0,
            slope_f=0.20,
            drone=_MockDrone(),
        )[0]

        assert h_ind > h_res  # industrial building → higher required altitude

    def test_kerrosluku_raises_altitude_vs_heuristic(self):
        # 5-storey building (5×3=15 m) should require higher altitude than 1-storey heuristic (7 m)
        bldg_geom = box(300075, 6899975, 300125, 6900025)
        b_few = Building(
            mtk_id=1,
            kohdeluokka=42211,
            kayttotarkoitus=None,
            geometry=bldg_geom,
            alkupvm=None,
            kerrosluku=1,
        )
        b_many = Building(
            mtk_id=2,
            kohdeluokka=42211,
            kayttotarkoitus=None,
            geometry=bldg_geom,
            alkupvm=None,
            kerrosluku=5,
        )

        route = _MockRoute([(300050, 6900200, 300150, 6900200)])

        h_few = compute_altitude_profile(
            route,
            [b_few],
            [],
            flight_height_m=300.0,
            min_h=10.0,
            powerline_clearance_m=70.0,
            overlap_front_pct=80.0,
            overlap_side_pct=70.0,
            slope_f=0.20,
            drone=_MockDrone(),
        )[0]
        h_many = compute_altitude_profile(
            route,
            [b_many],
            [],
            flight_height_m=300.0,
            min_h=10.0,
            powerline_clearance_m=70.0,
            overlap_front_pct=80.0,
            overlap_side_pct=70.0,
            slope_f=0.20,
            drone=_MockDrone(),
        )[0]

        assert h_many > h_few  # taller building → higher altitude required
