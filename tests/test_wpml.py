"""Tests for wpml.py — Phase 6.

TestReferenceFixture validates the reference KMZ from Phase 0.
TestGeneratedKMZ validates that build_kmz() reproduces the fixture schema.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from lxml import etree
from shapely.geometry import Polygon

from flightmanager.buildings import Building
from flightmanager.config import FlightConfig
from flightmanager.crs import CRSError
from flightmanager.homes_kml import build_homes_kml
from flightmanager.waypoints import ONE_BATTERY_MINUTES, budget_estimate
from flightmanager.wpml import KmzResult, build_kmz

FIXTURES = Path(__file__).parent.parent / "fixtures"
REFERENCE_KMZ = FIXTURES / "reference_mission.kmz"
WPML_NS = "http://www.dji.com/wpmz/1.0.6"
KML_NS  = "http://www.opengis.net/kml/2.2"
WPML    = f"{{{WPML_NS}}}"
KML     = f"{{{KML_NS}}}"

# A small valid survey polygon in EPSG:4326 (Finnish field area)
_SURVEY = Polygon([
    (22.660, 62.551), (22.674, 62.551),
    (22.674, 62.541), (22.660, 62.541),
    (22.660, 62.551),
])

# 2.7 cm GSD → derived height ≈ 100.6 m AGL (under default 110 m ceiling)
_FLIGHT = FlightConfig(target_gsd_cm=2.7)


def parse_template_from_kmz(kmz_path: Path) -> etree._Element:
    with zipfile.ZipFile(kmz_path) as zf:
        name = next(n for n in zf.namelist() if n.endswith("template.kml"))
        with zf.open(name) as f:
            return etree.parse(f).getroot()


def has_waylines(kmz_path: Path) -> bool:
    with zipfile.ZipFile(kmz_path) as zf:
        return any(n.endswith("waylines.wpml") for n in zf.namelist())


# ---------------------------------------------------------------------------
# Reference fixture sanity checks
# ---------------------------------------------------------------------------


class TestReferenceFixture:
    def test_reference_kmz_exists(self):
        assert REFERENCE_KMZ.exists(), "reference_mission.kmz missing from fixtures/"

    def test_reference_kmz_internal_structure(self):
        with zipfile.ZipFile(REFERENCE_KMZ) as zf:
            names = zf.namelist()
        assert "wpmz/template.kml"  in names
        assert "wpmz/waylines.wpml" in names

    def test_reference_wpml_namespace(self):
        root = parse_template_from_kmz(REFERENCE_KMZ)
        assert root.nsmap.get("wpml") == WPML_NS

    def test_reference_drone_enum(self):
        root = parse_template_from_kmz(REFERENCE_KMZ)
        assert root.find(f".//{WPML}droneEnumValue").text == "77"

    def test_reference_drone_sub_enum(self):
        root = parse_template_from_kmz(REFERENCE_KMZ)
        assert root.find(f".//{WPML}droneSubEnumValue").text == "0"

    def test_reference_payload_enum(self):
        root = parse_template_from_kmz(REFERENCE_KMZ)
        assert root.find(f".//{WPML}payloadEnumValue").text == "68"

    def test_reference_payload_sub_enum(self):
        root = parse_template_from_kmz(REFERENCE_KMZ)
        assert root.find(f".//{WPML}payloadSubEnumValue").text == "3"

    def test_reference_template_type(self):
        root = parse_template_from_kmz(REFERENCE_KMZ)
        assert root.find(f".//{WPML}templateType").text == "mapping2d"

    def test_reference_coordinate_mode(self):
        root = parse_template_from_kmz(REFERENCE_KMZ)
        assert root.find(f".//{WPML}coordinateMode").text == "WGS84"

    def test_reference_polygon_coordinate_order(self):
        root = parse_template_from_kmz(REFERENCE_KMZ)
        coords_el = root.find(f".//{KML}coordinates")
        first = coords_el.text.strip().splitlines()[0].strip().split(",")
        lon, lat = float(first[0]), float(first[1])
        assert 20 < lon < 30, f"Expected longitude ~22°E, got {lon}"
        assert 60 < lat < 70, f"Expected latitude ~62°N, got {lat}"

    def test_reference_polygon_no_holes(self):
        root = parse_template_from_kmz(REFERENCE_KMZ)
        assert len(root.findall(f".//{KML}innerBoundaryIs")) == 0

    def test_reference_height_fields_consistent(self):
        root = parse_template_from_kmz(REFERENCE_KMZ)
        gh = float(root.find(f".//{WPML}globalShootHeight").text)
        eh = float(root.find(f".//{WPML}ellipsoidHeight").text)
        h  = float(root.find(f".//{WPML}height").text)
        assert abs(gh - eh) < 0.001
        assert abs(gh - h)  < 0.001

    def test_reference_waylines_present(self):
        assert has_waylines(REFERENCE_KMZ)


# ---------------------------------------------------------------------------
# Generated KMZ schema validation
# ---------------------------------------------------------------------------


@pytest.fixture
def generated_kmz(tmp_path) -> Path:
    result = build_kmz(_SURVEY, _FLIGHT, tmp_path / "test.kmz")
    return result.kmz_path


class TestGeneratedKMZ:
    def test_internal_structure(self, generated_kmz):
        with zipfile.ZipFile(generated_kmz) as zf:
            names = zf.namelist()
        assert "wpmz/template.kml"  in names
        assert "wpmz/waylines.wpml" in names

    def test_wpml_namespace(self, generated_kmz):
        root = parse_template_from_kmz(generated_kmz)
        assert root.nsmap.get("wpml") == WPML_NS

    def test_drone_enum(self, generated_kmz):
        root = parse_template_from_kmz(generated_kmz)
        assert root.find(f".//{WPML}droneEnumValue").text == "77"

    def test_drone_sub_enum(self, generated_kmz):
        root = parse_template_from_kmz(generated_kmz)
        assert root.find(f".//{WPML}droneSubEnumValue").text == "0"

    def test_payload_enum(self, generated_kmz):
        root = parse_template_from_kmz(generated_kmz)
        assert root.find(f".//{WPML}payloadEnumValue").text == "68"

    def test_payload_sub_enum(self, generated_kmz):
        root = parse_template_from_kmz(generated_kmz)
        assert root.find(f".//{WPML}payloadSubEnumValue").text == "3"

    def test_template_type(self, generated_kmz):
        root = parse_template_from_kmz(generated_kmz)
        assert root.find(f".//{WPML}templateType").text == "mapping2d"

    def test_coordinate_order_lon_lat(self, generated_kmz):
        root = parse_template_from_kmz(generated_kmz)
        coords_el = root.find(f".//{KML}coordinates")
        first = coords_el.text.strip().splitlines()[0].strip().split(",")
        lon, lat = float(first[0]), float(first[1])
        assert 20 < lon < 30, f"Expected longitude ~22°E, got {lon}"
        assert 60 < lat < 70, f"Expected latitude ~62°N, got {lat}"

    def test_no_holes(self, generated_kmz):
        root = parse_template_from_kmz(generated_kmz)
        assert len(root.findall(f".//{KML}innerBoundaryIs")) == 0

    def test_height_fields_all_set_and_consistent(self, generated_kmz):
        root = parse_template_from_kmz(generated_kmz)
        gh = float(root.find(f".//{WPML}globalShootHeight").text)
        eh = float(root.find(f".//{WPML}ellipsoidHeight").text)
        h  = float(root.find(f".//{WPML}height").text)
        assert abs(gh - eh) < 0.001
        assert abs(gh - h)  < 0.001

    def test_height_within_ceiling(self, generated_kmz):
        root = parse_template_from_kmz(generated_kmz)
        h = float(root.find(f".//{WPML}globalShootHeight").text)
        assert h <= _FLIGHT.max_height_agl_m

    def test_height_matches_derived(self, generated_kmz):
        root = parse_template_from_kmz(generated_kmz)
        h = float(root.find(f".//{WPML}globalShootHeight").text)
        assert abs(h - _FLIGHT.derived_flight_height_m) < 0.01

    def test_height_mode_is_relative_to_start_point(self, generated_kmz):
        # Confirmed from fixture: heightMode is always relativeToStartPoint.
        # Terrain follow is controlled by surfaceFollowModeEnable, not heightMode.
        root = parse_template_from_kmz(generated_kmz)
        assert root.find(f".//{WPML}heightMode").text == "relativeToStartPoint"

    def test_no_dsm_no_surface_follow_elements(self, generated_kmz):
        root = parse_template_from_kmz(generated_kmz)
        assert root.find(f".//{WPML}surfaceFollowModeEnable") is None

    def test_dsm_enables_surface_follow_elements(self, tmp_path):
        dsm = tmp_path / "test_dsm.tif"
        dsm.write_bytes(b"fake")
        result = build_kmz(_SURVEY, _FLIGHT, tmp_path / "terrain.kmz", dsm_path=dsm)
        root = parse_template_from_kmz(result.kmz_path)
        assert root.find(f".//{WPML}surfaceFollowModeEnable").text == "1"
        assert root.find(f".//{WPML}isRealtimeSurfaceFollow").text == "0"
        assert root.find(f".//{WPML}dsmFile") is not None

    def test_dsm_embedded_in_kmz(self, tmp_path):
        import zipfile
        dsm = tmp_path / "myfield_dsm.tif"
        dsm.write_bytes(b"fake tif content")
        result = build_kmz(_SURVEY, _FLIGHT, tmp_path / "out.kmz", dsm_path=dsm)
        with zipfile.ZipFile(result.kmz_path) as zf:
            names = zf.namelist()
        assert any("res/dsm" in n for n in names)

    def test_overlap_values_written(self, generated_kmz):
        root = parse_template_from_kmz(generated_kmz)
        assert root.find(f".//{WPML}orthoCameraOverlapH").text == "80"
        assert root.find(f".//{WPML}orthoCameraOverlapW").text == "70"

    def test_polygon_coords_match_survey(self, generated_kmz):
        root = parse_template_from_kmz(generated_kmz)
        coords_text = root.find(f".//{KML}coordinates").text
        # All survey vertices (22.66 and 22.674) should appear
        assert "22.660" in coords_text or "22.66," in coords_text
        assert "62.551" in coords_text

    def test_result_dataclass_fields(self, tmp_path):
        result = build_kmz(_SURVEY, _FLIGHT, tmp_path / "r.kmz")
        assert isinstance(result, KmzResult)
        assert result.kmz_path.exists()
        assert result.flight_height_m > 0
        assert result.gsd_cm == _FLIGHT.target_gsd_cm
        assert result.estimated_photo_count > 0
        assert result.estimated_flight_time_min > 0


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_rejects_3067_polygon(self, tmp_path):
        poly_3067 = Polygon([
            (300_000, 6_900_000), (301_000, 6_900_000),
            (301_000, 6_901_000), (300_000, 6_901_000),
        ])
        with pytest.raises(CRSError):
            build_kmz(poly_3067, _FLIGHT, tmp_path / "out.kmz")

    def test_rejects_multipolygon(self, tmp_path):
        from shapely.geometry import MultiPolygon
        mp = MultiPolygon([_SURVEY, _SURVEY.buffer(0.1)])
        with pytest.raises(ValueError, match="single Polygon"):
            build_kmz(mp, _FLIGHT, tmp_path / "out.kmz")

    def test_rejects_polygon_with_holes(self, tmp_path):
        outer = [(22.60, 62.50), (22.70, 62.50),
                 (22.70, 62.60), (22.60, 62.60), (22.60, 62.50)]
        inner = [(22.63, 62.53), (22.67, 62.53),
                 (22.67, 62.57), (22.63, 62.57), (22.63, 62.53)]
        holed = Polygon(outer, [inner])
        with pytest.raises(ValueError, match="holes"):
            build_kmz(holed, _FLIGHT, tmp_path / "out.kmz")


# ---------------------------------------------------------------------------
# Battery budget
# ---------------------------------------------------------------------------


class TestHomesKml:
    """Validate homes KML against the DJI pin format confirmed in fixtures."""

    def _make_building(self, mtk_id: int, kohdeluokka: int) -> Building:
        from shapely.geometry import Point
        # Buildings must be in EPSG:4326 for the centroid to produce valid pin coords
        # (the pipeline reprojects before calling build_homes_kml)
        return Building(
            mtk_id=mtk_id,
            kohdeluokka=kohdeluokka,
            kayttotarkoitus=1,
            geometry=Point(22.658, 62.551).buffer(0.0001),
            alkupvm="2025-01-01",
        )

    def test_output_file_created(self, tmp_path):
        b = self._make_building(1001, 42211)
        out = build_homes_kml([b], tmp_path / "homes.kml")
        assert out.exists()

    def test_valid_kml_structure(self, tmp_path):
        b = self._make_building(1001, 42211)
        out = build_homes_kml([b], tmp_path / "homes.kml")
        content = out.read_text()
        assert content.startswith('<?xml version="1.0"')
        assert 'xmlns="http://www.opengis.net/kml/2.2"' in content
        assert '<Document xmlns="">' in content

    def test_all_five_dji_styles_present(self, tmp_path):
        out = build_homes_kml([], tmp_path / "homes.kml")
        content = out.read_text()
        for style in ("dji_style_red", "dji_style_green", "dji_style_yellow",
                      "dji_style_blue", "dji_style_purple"):
            assert style in content

    def test_dji_colour_values_correct(self, tmp_path):
        out = build_homes_kml([], tmp_path / "homes.kml")
        content = out.read_text()
        assert "#FF393CE2" in content   # red
        assert "#FF6BBE19" in content   # green
        assert "#FF00BBFF" in content   # yellow
        assert "#FFF08C2D" in content   # blue
        assert "#FFE020B6" in content   # purple

    def test_residential_gets_red_pin_a2(self, tmp_path):
        from flightmanager.config import HomeSafetyConfig
        b = self._make_building(1001, 42211)
        out = build_homes_kml([b], tmp_path / "homes.kml",
                              HomeSafetyConfig(operating_subcategory="A2"))
        assert "#dji_style_red" in out.read_text()

    def test_residential_gets_red_pin_a3(self, tmp_path):
        from flightmanager.config import HomeSafetyConfig
        b = self._make_building(1001, 42211)
        out = build_homes_kml([b], tmp_path / "homes.kml",
                              HomeSafetyConfig(operating_subcategory="A3"))
        assert "#dji_style_red" in out.read_text()

    def test_commercial_red_for_a3_yellow_for_a2(self, tmp_path):
        from flightmanager.config import HomeSafetyConfig
        b = self._make_building(1002, 42221)
        out_a3 = build_homes_kml([b], tmp_path / "a3.kml",
                                 HomeSafetyConfig(operating_subcategory="A3"))
        out_a2 = build_homes_kml([b], tmp_path / "a2.kml",
                                 HomeSafetyConfig(operating_subcategory="A2"))
        assert "#dji_style_red"    in out_a3.read_text()
        assert "#dji_style_yellow" in out_a2.read_text()

    def test_agricultural_excluded_entirely(self, tmp_path):
        # Agricultural buildings (42261) not shown at all — no pin generated
        b = self._make_building(1004, 42261)
        out = build_homes_kml([b], tmp_path / "homes.kml")
        assert "<Placemark>" not in out.read_text()

    def test_other_unknown_types_excluded(self, tmp_path):
        b = self._make_building(1005, 42270)
        out = build_homes_kml([b], tmp_path / "homes.kml")
        assert "<Placemark>" not in out.read_text()

    def test_a3_no_yellow_pins(self, tmp_path):
        from flightmanager.config import HomeSafetyConfig
        # All relevant buildings are red under A3 — yellow is never used
        buildings = [
            self._make_building(1001, 42211),  # residential
            self._make_building(1002, 42221),  # commercial
            self._make_building(1003, 42231),  # holiday
        ]
        out = build_homes_kml(buildings, tmp_path / "homes.kml",
                              HomeSafetyConfig(operating_subcategory="A3"))
        content = out.read_text()
        assert "#dji_style_yellow" not in content
        assert content.count("#dji_style_red") == 3

    def test_blue_purple_green_never_used(self, tmp_path):
        buildings = [
            self._make_building(1001, 42211),
            self._make_building(1002, 42221),
        ]
        out = build_homes_kml(buildings, tmp_path / "homes.kml")
        content = out.read_text()
        assert "#dji_style_blue"   not in content
        assert "#dji_style_purple" not in content
        assert "#dji_style_green"  not in content

    def test_coordinates_are_lon_lat(self, tmp_path):
        b = self._make_building(1001, 42211)
        out = build_homes_kml([b], tmp_path / "homes.kml")
        content = out.read_text()
        # Centroid of a 4326 polygon near lon=22.658 should appear
        assert "22.658" in content
        assert "62.551" in content

    def test_altitude_mode_absolute(self, tmp_path):
        b = self._make_building(1001, 42211)
        out = build_homes_kml([b], tmp_path / "homes.kml")
        assert "<altitudeMode>absolute</altitudeMode>" in out.read_text()

    def test_empty_buildings_still_produces_valid_kml(self, tmp_path):
        out = build_homes_kml([], tmp_path / "homes.kml")
        content = out.read_text()
        assert "<kml" in content
        assert "<Placemark>" not in content

    def test_mtk_id_in_description(self, tmp_path):
        b = self._make_building(12345, 42211)
        out = build_homes_kml([b], tmp_path / "homes.kml")
        assert "12345" in out.read_text()

    def test_multiple_buildings(self, tmp_path):
        buildings = [
            self._make_building(1001, 42211),
            self._make_building(1002, 42221),
            self._make_building(1003, 42231),
        ]
        out = build_homes_kml(buildings, tmp_path / "homes.kml")
        content = out.read_text()
        assert content.count("<Placemark>") == 3

    def test_fixture_format_roundtrip(self, tmp_path):
        """Parse the fixture PIN KML and verify our output matches its structure."""
        fixture = FIXTURES / "PIN-20260529224114.kml"
        if not fixture.exists():
            pytest.skip("Pin fixture not present")
        b = self._make_building(9001, 42211)
        out = build_homes_kml([b], tmp_path / "homes.kml")
        # Both should start with the same KML declaration and namespace
        fix_content = fixture.read_text()
        out_content = out.read_text()
        assert 'xmlns="http://www.opengis.net/kml/2.2"' in out_content
        assert '<Document xmlns="">' in out_content
        assert "dji_style_red" in fix_content  # fixture uses red style
        assert "dji_style_red" in out_content   # ours does too for residential


class TestBatteryBudget:
    def test_small_field_under_one_battery(self, tmp_path):
        # ~1 ha field — should be well under one battery
        small = Polygon([
            (22.660, 62.551), (22.661, 62.551),
            (22.661, 62.550), (22.660, 62.550), (22.660, 62.551),
        ])
        result = build_kmz(small, _FLIGHT, tmp_path / "small.kmz")
        assert not result.over_one_battery

    def test_large_field_flags_over_battery(self, tmp_path):
        # A very large field (~1000 ha) should exceed one battery
        large = Polygon([
            (22.60, 62.50), (22.80, 62.50),
            (22.80, 62.65), (22.60, 62.65), (22.60, 62.50),
        ])
        result = build_kmz(large, _FLIGHT, tmp_path / "large.kmz")
        assert result.over_one_battery

    def test_estimate_budget_returns_expected_keys(self):
        from flightmanager.wpml import resolve_strip_speed
        _speed = resolve_strip_speed(_FLIGHT, None, _FLIGHT.derived_flight_height_m)
        budget = budget_estimate(_SURVEY, _FLIGHT, speed_ms=_speed)
        assert "photo_count"      in budget
        assert "flight_time_min"  in budget
        assert "over_one_battery" in budget
        assert budget["photo_count"] > 0
        assert budget["flight_time_min"] > 0
