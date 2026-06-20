"""Tests for route.plan_route / footprint_spacings / route_result_to_geojson
(the shared planner + GeoJSON builder extracted in the Phase 3 refactor)."""

from __future__ import annotations

from shapely.geometry import box

from flightmanager.config import DroneConfig
from flightmanager.route import (
    PlannedRoute, footprint_spacings, plan_route, route_result_to_geojson,
)

_DRONE = DroneConfig(
    name="test", label="Test drone", drone_enum=77, payload_enum=68,
    focal_length_mm=7.06, pixel_pitch_um=3.3,
    image_width_px=5280, image_height_px=3956, min_capture_interval_s=2.38,
)

# 400 × 400 m field (EPSG:3067 metres).
_POLY = box(0, 0, 400, 400)


class TestFootprintSpacings:
    def test_positive_and_within_footprint(self):
        strip, photo = footprint_spacings(_DRONE, 80, 80, 70)
        assert strip > 0 and photo > 0
        # Spacing is the footprint reduced by the overlap, so it's below the full footprint.
        full_w = 80 * _DRONE.image_width_px * _DRONE.pixel_pitch_um * 1e-6 / (_DRONE.focal_length_mm * 1e-3)
        assert strip < full_w

    def test_more_overlap_means_tighter_spacing(self):
        loose, _ = footprint_spacings(_DRONE, 80, 80, 50)
        tight, _ = footprint_spacings(_DRONE, 80, 80, 80)
        assert tight < loose

    def test_higher_altitude_means_wider_spacing(self):
        low, _ = footprint_spacings(_DRONE, 60, 80, 70)
        high, _ = footprint_spacings(_DRONE, 120, 80, 70)
        assert high > low


class TestPlanRouteSimple:
    def test_returns_planned_route_with_flat_altitude(self):
        pr = plan_route(_POLY, drone=_DRONE, height_m=80,
                        overlap_front_pct=80, overlap_side_pct=70)
        assert isinstance(pr, PlannedRoute)
        assert pr.route.strip_count > 0
        # Simple mode: flat profile at the requested height, one entry per strip.
        assert pr.altitude_profile == [80.0] * len(pr.route.strips_3067)
        assert pr.strip_waypoints is None and pr.transit_waypoints is None

    def test_auto_angle_when_unset(self):
        pr = plan_route(_POLY, drone=_DRONE, height_m=80,
                        overlap_front_pct=80, overlap_side_pct=70)
        assert 0 <= pr.angle_deg < 180

    def test_explicit_angle_is_honoured(self):
        pr = plan_route(_POLY, drone=_DRONE, height_m=80, angle_deg=42.0,
                        overlap_front_pct=80, overlap_side_pct=70)
        assert pr.angle_deg == 42.0


class TestRouteResultToGeojson:
    def test_strips_and_transits_feature_collections(self):
        pr = plan_route(_POLY, drone=_DRONE, height_m=80,
                        overlap_front_pct=80, overlap_side_pct=70)
        gj = route_result_to_geojson(pr.route, pr.altitude_profile, _DRONE, 80)

        strips = gj["strips_geojson"]
        transits = gj["transits_geojson"]
        assert strips["type"] == "FeatureCollection"
        assert transits["type"] == "FeatureCollection"
        assert len(strips["features"]) == pr.route.strip_count

        feat = strips["features"][0]
        assert feat["geometry"]["type"] == "LineString"
        assert "altitude_m" in feat["properties"]
        assert "speed_ms" in feat["properties"]
        # Reprojected to EPSG:4326 — coordinates are lon/lat degrees.
        lon, lat = feat["geometry"]["coordinates"][0]
        assert -180 <= lon <= 180 and -90 <= lat <= 90
