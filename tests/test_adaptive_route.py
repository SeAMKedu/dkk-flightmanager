"""Tests for adaptive_route.py."""

from __future__ import annotations

import math

import pytest
from shapely.geometry import box

from flightmanager.adaptive_route import _altitude_at, _slope_across, compute_adaptive_route
from flightmanager.buildings import Building
from flightmanager.config import DroneConfig
from flightmanager.route import RouteResult

# ---------------------------------------------------------------------------
# Minimal DroneConfig for testing (M3M-like)
# ---------------------------------------------------------------------------

_DRONE = DroneConfig(
    name="test",
    label="Test drone",
    drone_enum=77,
    payload_enum=68,
    focal_length_mm=7.06,
    pixel_pitch_um=3.3,
    image_width_px=5280,
    image_height_px=3956,
    min_capture_interval_s=2.38,
)

# Survey polygon: 400 × 400 m field centred at EPSG:3067 origin
_POLY = box(0, 0, 400, 400)

# Building 100 m outside the polygon boundary (so strips inside the poly
# are at horizontal distance ≥ 100 m from the building)
_BUILDING = Building(
    mtk_id=1,
    kohdeluokka=42211,
    kayttotarkoitus=None,
    geometry=box(-200, 150, -110, 250),  # 110 m west of polygon west edge
    alkupvm=None,
    kerrosluku=2,  # 2 storeys × 3 m = 6 m building height
)


# ---------------------------------------------------------------------------
# _altitude_at
# ---------------------------------------------------------------------------


class TestAltitudeAt:
    def test_far_from_buildings_returns_h_max(self):
        from shapely.geometry import Point
        bldg_pairs = [(_BUILDING.geometry, 6.0)]
        alt = _altitude_at(
            Point(200, 200), bldg_pairs, [],
            H_max=50.0, H_min=10.0, powerline_clearance_m=70.0,
        )
        assert alt == pytest.approx(50.0)

    def test_close_to_building_clamps_to_d_eff(self):
        from shapely.geometry import Point
        # Building edge is at x=-110; point at x=0 → horizontal dist = 110 m
        # d_eff = 110 + 6 = 116 → capped at H_max=50
        bldg_pairs = [(_BUILDING.geometry, 6.0)]
        alt = _altitude_at(
            Point(0, 200), bldg_pairs, [],
            H_max=50.0, H_min=10.0, powerline_clearance_m=70.0,
        )
        assert alt == pytest.approx(50.0)

    def test_very_close_to_building_limited_to_h_min(self):
        from shapely.geometry import Point
        close_bldg = box(-15, 0, 0, 50)
        bldg_pairs = [(close_bldg, 6.0)]
        # edge at x=0, point at x=5 → dist=5, d_eff=11 < H_min=15
        alt = _altitude_at(
            Point(5, 25), bldg_pairs, [],
            H_max=50.0, H_min=15.0, powerline_clearance_m=70.0,
        )
        assert alt == pytest.approx(15.0)

    def test_no_buildings_returns_h_max(self):
        from shapely.geometry import Point
        alt = _altitude_at(
            Point(200, 200), [], [],
            H_max=80.0, H_min=20.0, powerline_clearance_m=70.0,
        )
        assert alt == pytest.approx(80.0)


# ---------------------------------------------------------------------------
# compute_adaptive_route — geometry
# ---------------------------------------------------------------------------


class TestComputeAdaptiveRoute:
    def test_no_buildings_uniform_altitude(self):
        """Without buildings the route degenerates to fixed spacing at H_max."""
        route, alts = compute_adaptive_route(
            _POLY, 0.0, [], [],
            drone=_DRONE,
            H_max=50.0, H_min=10.0,
            overlap_front_pct=80, overlap_side_pct=70,
            powerline_clearance_m=70.0, slope_f=0.2,
        )
        assert route.strip_count > 0
        assert len(alts) == route.strip_count
        assert all(a == pytest.approx(50.0) for a in alts)

    def test_strip_count_increases_with_lower_h_max(self):
        """Lowering H_max → smaller footprint → more strips needed."""
        route_hi, _ = compute_adaptive_route(
            _POLY, 0.0, [], [],
            drone=_DRONE, H_max=100.0, H_min=10.0,
            overlap_front_pct=80, overlap_side_pct=70,
            powerline_clearance_m=70.0, slope_f=0.2,
        )
        route_lo, _ = compute_adaptive_route(
            _POLY, 0.0, [], [],
            drone=_DRONE, H_max=30.0, H_min=10.0,
            overlap_front_pct=80, overlap_side_pct=70,
            powerline_clearance_m=70.0, slope_f=0.2,
        )
        assert route_lo.strip_count > route_hi.strip_count

    def test_altitude_profile_matches_strip_count(self):
        route, alts = compute_adaptive_route(
            _POLY, 0.0, [_BUILDING], [],
            drone=_DRONE, H_max=80.0, H_min=10.0,
            overlap_front_pct=80, overlap_side_pct=70,
            powerline_clearance_m=70.0, slope_f=0.2,
        )
        assert len(alts) == route.strip_count

    def test_altitude_within_bounds(self):
        route, alts = compute_adaptive_route(
            _POLY, 0.0, [_BUILDING], [],
            drone=_DRONE, H_max=80.0, H_min=15.0,
            overlap_front_pct=80, overlap_side_pct=70,
            powerline_clearance_m=70.0, slope_f=0.2,
        )
        assert all(15.0 - 1e-9 <= a <= 80.0 + 1e-9 for a in alts)

    def test_returns_routeresult(self):
        route, alts = compute_adaptive_route(
            _POLY, 0.0, [], [],
            drone=_DRONE, H_max=50.0, H_min=10.0,
            overlap_front_pct=80, overlap_side_pct=70,
            powerline_clearance_m=70.0, slope_f=0.2,
        )
        assert isinstance(route, RouteResult)
        assert route.strip_dist_m > 0
        assert route.photo_count > 0

    def test_transit_segs_length_without_home(self):
        route, _ = compute_adaptive_route(
            _POLY, 0.0, [], [],
            drone=_DRONE, H_max=50.0, H_min=10.0,
            overlap_front_pct=80, overlap_side_pct=70,
            powerline_clearance_m=70.0, slope_f=0.2,
        )
        # N-1 inter-strip transits when no home point
        assert len(route.transit_segs_3067) == route.strip_count - 1

    def test_with_home_adds_home_transits(self):
        route, _ = compute_adaptive_route(
            _POLY, 0.0, [], [],
            drone=_DRONE, H_max=50.0, H_min=10.0,
            overlap_front_pct=80, overlap_side_pct=70,
            powerline_clearance_m=70.0, slope_f=0.2,
            home_3067=(200.0, -100.0),
        )
        # N+1 segments: home→first, N-1 inter-strip, last→home
        assert len(route.transit_segs_3067) == route.strip_count + 1

    def test_tiny_polygon_returns_empty(self):
        """A polygon too small to fit any strip returns empty route."""
        tiny = box(0, 0, 0.5, 0.5)
        route, alts = compute_adaptive_route(
            tiny, 0.0, [], [],
            drone=_DRONE, H_max=50.0, H_min=10.0,
            overlap_front_pct=80, overlap_side_pct=70,
            powerline_clearance_m=70.0, slope_f=0.2,
        )
        assert route.strip_count == 0
        assert alts == []
