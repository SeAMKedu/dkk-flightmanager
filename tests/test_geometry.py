"""Tests for geometry.py — Phase 3."""

from __future__ import annotations

import pytest
from shapely.geometry import MultiPolygon, Point, Polygon

from flightmanager.buildings import Building
from flightmanager.config import HomeSafetyConfig, PolygonConfig
from flightmanager.crs import CRSError
from flightmanager.geometry import (
    SurveyGeometry,
    _apply_edge_buffer,
    _apply_keepout,
    build_keepout,
    _close_gaps,
    _enforce_hole_policy,
    _enforce_policy,
    _fix_winding,
    _merge_parcels,
    reproject_to_4326,
    reproject_to_3067,
    apply_survey_offset,
    suggest_takeoff_point,
    process_survey,
)
from flightmanager.parcels import Parcel

# ---------------------------------------------------------------------------
# Helper factories — all in EPSG:3067 (Finnish range)
# ---------------------------------------------------------------------------


def make_parcel(
    x: float = 300_000, y: float = 6_900_000,
    w: float = 500, h: float = 500,
    parcel_id: str = "P1",
) -> Parcel:
    ring = [
        (x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)
    ]
    return Parcel(
        parcel_id=parcel_id,
        tunnus=1,
        year=2024,
        area_ha=w * h / 10_000,
        geometry=Polygon(ring),
    )


def make_building(
    x: float = 301_000, y: float = 6_900_250,
    w: float = 20, h: float = 20,
    kohdeluokka: int = 42211,
    mtk_id: int = 9001,
) -> Building:
    ring = [
        (x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)
    ]
    return Building(
        mtk_id=mtk_id,
        kohdeluokka=kohdeluokka,
        kayttotarkoitus=1,
        geometry=Polygon(ring),
        alkupvm="2025-01-01",
    )


_DEFAULT_HOME_SAFETY = HomeSafetyConfig(
    operating_subcategory="A3",
    home_buffer_m=150,
    offset_enabled=True,
    max_area_loss_pct=30,
)

_DEFAULT_POLY_CFG = PolygonConfig(
    edge_buffer_m=0,
    simplify_tolerance_m=0,
    multipart_policy="review",
    hole_policy="review",
)


# ---------------------------------------------------------------------------
# _merge_parcels
# ---------------------------------------------------------------------------


class TestMergeParcels:
    def test_single_parcel_returns_polygon(self):
        result = _merge_parcels([make_parcel()])
        assert isinstance(result, Polygon)

    def test_adjacent_parcels_merge(self):
        p1 = make_parcel(x=300_000, w=500)
        p2 = make_parcel(x=300_500, w=500, parcel_id="P2")
        result = _merge_parcels([p1, p2])
        # Area should be sum of both
        assert abs(result.area - (p1.geometry.area + p2.geometry.area)) < 1.0

    def test_overlapping_parcels_no_double_count(self):
        p1 = make_parcel(x=300_000, w=500)
        p2 = make_parcel(x=300_250, w=500, parcel_id="P2")  # overlaps by 250 m
        result = _merge_parcels([p1, p2])
        assert result.area < p1.geometry.area + p2.geometry.area


# ---------------------------------------------------------------------------
# _apply_edge_buffer
# ---------------------------------------------------------------------------


class TestCloseGaps:
    def _two_parcels_with_gap(self, gap_m: float = 2.0):
        """Two 500×500 m parcels separated by gap_m."""
        p1 = make_parcel(x=300_000, w=500)
        p2 = make_parcel(x=300_000 + 500 + gap_m, w=500, parcel_id="P2")
        return _merge_parcels([p1, p2])

    def test_zero_gap_fill_no_change(self):
        mp = self._two_parcels_with_gap(gap_m=4)
        result = _close_gaps(mp, gap_fill_m=0)
        assert result.equals(mp)

    def test_gap_smaller_than_fill_merges_to_single_polygon(self):
        mp = self._two_parcels_with_gap(gap_m=3)
        assert mp.geom_type == "MultiPolygon"  # gap present before fill
        result = _close_gaps(mp, gap_fill_m=3)  # fill ≥ gap/2 → bridges it
        assert result.geom_type == "Polygon"

    def test_gap_larger_than_fill_stays_multipolygon(self):
        mp = self._two_parcels_with_gap(gap_m=20)
        result = _close_gaps(mp, gap_fill_m=3)
        # 3 m fill can only bridge gaps up to 6 m wide
        assert result.geom_type == "MultiPolygon"

    def test_area_roughly_preserved(self):
        mp = self._two_parcels_with_gap(gap_m=2)
        result = _close_gaps(mp, gap_fill_m=2)
        # Area should be close to original (fill adds the gap strip, erode removes it)
        assert abs(result.area - mp.area) / mp.area < 0.05  # within 5%

    def test_process_survey_uses_gap_fill(self):
        """Two parcels with a small gap produce a single polygon with gap_fill enabled."""
        p1 = make_parcel(x=300_000, w=500, parcel_id="P1")
        p2 = make_parcel(x=300_503, w=500, parcel_id="P2")  # 3 m gap
        cfg = PolygonConfig(gap_fill_m=3, multipart_policy="split")
        result = process_survey([p1, p2], [], _DEFAULT_HOME_SAFETY, cfg)
        assert len(result.pieces_3067) == 1  # merged into one piece


class TestEdgeBuffer:
    def test_zero_buffer_returns_same(self):
        poly = make_parcel().geometry
        result = _apply_edge_buffer(poly, 0)
        assert result.equals(poly)

    def test_positive_buffer_increases_area(self):
        poly = make_parcel().geometry
        result = _apply_edge_buffer(poly, 10)
        assert result.area > poly.area


# ---------------------------------------------------------------------------
# build_keepout
# ---------------------------------------------------------------------------


class TestBuildKeeput:
    def test_no_buildings_returns_none(self):
        assert build_keepout([], _DEFAULT_HOME_SAFETY) is None

    def test_residential_building_in_a3_keepout(self):
        b = make_building(kohdeluokka=42211)
        keepout = build_keepout([b], _DEFAULT_HOME_SAFETY)
        assert keepout is not None
        assert keepout.area > b.geometry.area  # buffer makes it larger

    def test_agricultural_building_excluded(self):
        b = make_building(kohdeluokka=42261)  # agricultural
        keepout = build_keepout([b], _DEFAULT_HOME_SAFETY)
        assert keepout is None  # excluded for all subcategories

    def test_a2_excludes_commercial(self):
        cfg = HomeSafetyConfig(operating_subcategory="A2", home_buffer_m=150)
        b = make_building(kohdeluokka=42221)  # commercial — only A3
        keepout = build_keepout([b], cfg)
        assert keepout is None

    def test_a3_includes_commercial(self):
        cfg = HomeSafetyConfig(operating_subcategory="A3", home_buffer_m=150)
        b = make_building(kohdeluokka=42221)  # commercial — A3 rule
        keepout = build_keepout([b], cfg)
        assert keepout is not None

    def test_keepout_covers_buffer_distance(self):
        b = make_building(x=302_000, y=6_900_250)  # 2000 m from parcel
        cfg = HomeSafetyConfig(home_buffer_m=150)
        keepout = build_keepout([b], cfg)
        # A point 100 m from building should be inside the 150 m buffer
        assert keepout.contains(Point(302_100, 6_900_260))
        # A point 200 m away should not
        assert not keepout.contains(Point(302_200, 6_900_250))


# ---------------------------------------------------------------------------
# _apply_keepout
# ---------------------------------------------------------------------------


class TestApplyKeeput:
    def test_offset_enabled_subtracts_keepout(self):
        survey = make_parcel(w=500, h=500).geometry
        # Building right at the edge of the survey
        b = make_building(x=300_480, y=6_900_250, kohdeluokka=42211)
        cfg = HomeSafetyConfig(home_buffer_m=50, offset_enabled=True)
        keepout = build_keepout([b], cfg)
        result, dist, applied = _apply_keepout(survey, keepout, cfg)
        assert applied
        assert result.area < survey.area

    def test_offset_disabled_measures_distance(self):
        survey = make_parcel(w=500, h=500).geometry
        b = make_building(x=301_000, y=6_900_250, kohdeluokka=42211)
        cfg = HomeSafetyConfig(home_buffer_m=150, offset_enabled=False)
        keepout = build_keepout([b], cfg)
        result, dist, applied = _apply_keepout(survey, keepout, cfg)
        assert not applied
        assert result.area == survey.area  # unchanged
        assert dist is not None
        assert dist >= 0

    def test_offset_enabled_but_no_keepout(self):
        survey = make_parcel().geometry
        result, dist, applied = _apply_keepout(survey, None, _DEFAULT_HOME_SAFETY)
        assert not applied
        assert dist is None

    def test_offset_clears_home_buffer(self):
        """After offset, survey polygon must be >= home_buffer_m from the building."""
        parcel = make_parcel(x=300_000, y=6_900_000, w=1000, h=1000)
        # Building just inside the parcel
        b = make_building(x=300_100, y=6_900_100, w=30, h=30, kohdeluokka=42211)
        cfg = HomeSafetyConfig(home_buffer_m=150, offset_enabled=True)
        keepout = build_keepout([b], cfg)
        result, _, _ = _apply_keepout(parcel.geometry, keepout, cfg)
        if not result.is_empty:
            # Buffer is approximated with line segments so the measured distance is
            # slightly less than the nominal 150 m; allow 1% tolerance.
            assert result.distance(b.geometry) >= 148.5


# ---------------------------------------------------------------------------
# _enforce_hole_policy
# ---------------------------------------------------------------------------


class TestHolePolicy:
    def _polygon_with_hole(self) -> Polygon:
        outer = [(300_000, 6_900_000), (301_000, 6_900_000),
                 (301_000, 6_901_000), (300_000, 6_901_000), (300_000, 6_900_000)]
        inner = [(300_200, 6_900_200), (300_800, 6_900_200),
                 (300_800, 6_900_800), (300_200, 6_900_800), (300_200, 6_900_200)]
        return Polygon(outer, [inner])

    def test_no_holes_returns_unchanged(self):
        poly = make_parcel().geometry
        result, reasons = _enforce_hole_policy(poly, "review")
        assert len(result.interiors) == 0
        assert reasons == []

    def test_review_policy_flags_hole(self):
        poly = self._polygon_with_hole()
        result, reasons = _enforce_hole_policy(poly, "review")
        assert len(reasons) == 1
        assert len(list(result.interiors)) > 0  # hole preserved

    def test_fill_policy_removes_hole(self):
        poly = self._polygon_with_hole()
        result, reasons = _enforce_hole_policy(poly, "fill")
        assert len(list(result.interiors)) == 0
        assert result.area > poly.area  # filled area is larger
        assert reasons == []

    def test_clip_policy_removes_hole(self):
        poly = self._polygon_with_hole()
        result, reasons = _enforce_hole_policy(poly, "clip")
        assert len(list(result.interiors)) == 0


# ---------------------------------------------------------------------------
# _enforce_policy (multipart)
# ---------------------------------------------------------------------------


class TestMultipartPolicy:
    def _make_two_separate_polygons(self) -> MultiPolygon:
        p1 = Polygon([(300_000, 6_900_000), (300_500, 6_900_000),
                      (300_500, 6_900_500), (300_000, 6_900_500)])
        p2 = Polygon([(301_000, 6_900_000), (301_500, 6_900_000),
                      (301_500, 6_900_500), (301_000, 6_900_500)])
        return MultiPolygon([p1, p2])

    def test_review_policy_flags_multipart(self):
        mp = self._make_two_separate_polygons()
        cfg = PolygonConfig(multipart_policy="review")
        pieces, reasons = _enforce_policy(mp, cfg)
        assert len(reasons) >= 1
        assert len(pieces) == 2  # both pieces surfaced

    def test_largest_policy_keeps_biggest(self):
        p1 = Polygon([(300_000, 6_900_000), (300_500, 6_900_000),
                      (300_500, 6_900_500), (300_000, 6_900_500)])
        p2 = Polygon([(301_000, 6_900_000), (301_200, 6_900_000),
                      (301_200, 6_900_200), (301_000, 6_900_200)])  # smaller
        mp = MultiPolygon([p1, p2])
        cfg = PolygonConfig(multipart_policy="largest")
        pieces, reasons = _enforce_policy(mp, cfg)
        assert len(pieces) == 1
        assert pieces[0].area == p1.area

    def test_split_policy_returns_all_pieces(self):
        mp = self._make_two_separate_polygons()
        cfg = PolygonConfig(multipart_policy="split")
        pieces, reasons = _enforce_policy(mp, cfg)
        assert len(pieces) == 2
        assert reasons == []

    def test_single_polygon_returns_one_piece(self):
        poly = make_parcel().geometry
        cfg = PolygonConfig(multipart_policy="review")
        pieces, reasons = _enforce_policy(poly, cfg)
        assert len(pieces) == 1
        assert reasons == []


# ---------------------------------------------------------------------------
# _fix_winding
# ---------------------------------------------------------------------------


class TestFixWinding:
    def test_exterior_is_ccw(self):
        # Create CW exterior and verify _fix_winding corrects it
        from shapely import orient_polygons as _orient
        cw_poly = _orient(make_parcel().geometry, exterior_cw=True)
        fixed = _fix_winding(cw_poly)
        # In shapely, CCW exterior has positive signed area
        import shapely
        assert shapely.get_coordinate_dimension(fixed) >= 0
        # Verify exterior coords are in CCW order by checking area sign
        coords = list(fixed.exterior.coords)
        # Shoelace formula
        n = len(coords)
        area = sum(
            coords[i][0] * coords[(i + 1) % n][1] - coords[(i + 1) % n][0] * coords[i][1]
            for i in range(n)
        )
        assert area > 0  # positive = CCW


# ---------------------------------------------------------------------------
# reproject_to_4326
# ---------------------------------------------------------------------------


class TestReproject:
    def test_output_in_4326_range(self):
        poly = make_parcel().geometry
        result = reproject_to_4326(poly)
        bounds = result.bounds
        # Should be somewhere in Finland (lon: 18-32, lat: 59-71)
        assert 18 < bounds[0] < 32, f"Expected Finnish longitude, got {bounds[0]}"
        assert 59 < bounds[1] < 71, f"Expected Finnish latitude, got {bounds[1]}"

    def test_coordinate_order_is_lon_lat(self):
        # A point at known 3067 location near Seinäjoki
        from shapely.geometry import Point
        pt_3067 = Point(300_000, 6_900_000)
        pt_4326 = reproject_to_4326(pt_3067)
        lon, lat = pt_4326.x, pt_4326.y
        # Longitude should be ~22-24°E, latitude ~62°N for this area
        assert 20 < lon < 28, f"Expected longitude ~22°E, got {lon}"
        assert 60 < lat < 65, f"Expected latitude ~62°N, got {lat}"


# ---------------------------------------------------------------------------
# process_survey (integration)
# ---------------------------------------------------------------------------


class TestProcessSurvey:
    def test_basic_no_buildings(self):
        parcels = [make_parcel()]
        result = process_survey(parcels, [], _DEFAULT_HOME_SAFETY, _DEFAULT_POLY_CFG)
        assert isinstance(result, SurveyGeometry)
        assert not result.needs_review
        assert result.area_lost_pct == 0.0
        assert result.min_dist_to_home_m is None
        assert result.offset_applied is False

    def test_building_outside_parcel_no_area_lost(self):
        parcel = make_parcel(x=300_000, y=6_900_000, w=500, h=500)
        # Building far from parcel — buffer doesn't intersect
        b = make_building(x=305_000, y=6_905_000)
        result = process_survey([parcel], [b], _DEFAULT_HOME_SAFETY, _DEFAULT_POLY_CFG)
        assert result.area_lost_pct == 0.0
        assert result.offset_applied

    def test_building_inside_parcel_causes_area_loss(self):
        parcel = make_parcel(x=300_000, y=6_900_000, w=1000, h=1000)
        # Building well inside the parcel
        b = make_building(x=300_100, y=6_900_100, w=30, h=30)
        cfg = HomeSafetyConfig(home_buffer_m=150, offset_enabled=True, max_area_loss_pct=50)
        result = process_survey([parcel], [b], cfg, _DEFAULT_POLY_CFG)
        assert result.area_lost_pct > 0
        assert result.offset_applied

    def test_excess_area_loss_sets_needs_review(self):
        parcel = make_parcel(x=300_000, y=6_900_000, w=300, h=300)
        # Building whose buffer covers most of the small parcel
        b = make_building(x=300_150, y=6_900_150, w=20, h=20)
        cfg = HomeSafetyConfig(home_buffer_m=150, offset_enabled=True, max_area_loss_pct=10)
        result = process_survey([parcel], [b], cfg, _DEFAULT_POLY_CFG)
        assert result.needs_review
        assert any("area" in r.lower() for r in result.review_reasons)

    def test_survey_4326_is_valid_wgs84(self):
        parcels = [make_parcel()]
        result = process_survey(parcels, [], _DEFAULT_HOME_SAFETY, _DEFAULT_POLY_CFG)
        from flightmanager.crs import assert_crs
        assert_crs(result.survey_4326, 4326)

    def test_bbox_3067_covers_survey(self):
        parcel = make_parcel(x=300_000, y=6_900_000, w=500, h=500)
        result = process_survey([parcel], [], _DEFAULT_HOME_SAFETY, _DEFAULT_POLY_CFG)
        xmin, ymin, xmax, ymax = result.bbox_3067
        bounds = result.survey_3067.bounds
        assert xmin <= bounds[0] and ymin <= bounds[1]
        assert xmax >= bounds[2] and ymax >= bounds[3]

    def test_crs_error_on_non_3067_parcel(self):
        # A parcel with WGS84 coordinates should be caught by the CRS guard
        bad = Parcel(
            parcel_id="bad", tunnus=1, year=2024, area_ha=1.0,
            geometry=Polygon([(22.6, 62.5), (22.7, 62.5), (22.7, 62.6), (22.6, 62.5)]),
        )
        with pytest.raises(CRSError):
            process_survey([bad], [], _DEFAULT_HOME_SAFETY, _DEFAULT_POLY_CFG)

    def test_offset_disabled_records_min_distance(self):
        parcel = make_parcel(x=300_000, y=6_900_000, w=500, h=500)
        b = make_building(x=300_600, y=6_900_250)  # just outside parcel edge
        cfg = HomeSafetyConfig(home_buffer_m=150, offset_enabled=False)
        result = process_survey([parcel], [b], cfg, _DEFAULT_POLY_CFG)
        assert not result.offset_applied
        assert result.min_dist_to_home_m is not None
        assert result.area_lost_pct == 0.0

    def test_multiple_parcels_merged(self):
        p1 = make_parcel(x=300_000, w=500, parcel_id="P1")
        p2 = make_parcel(x=300_500, w=500, parcel_id="P2")
        result = process_survey([p1, p2], [], _DEFAULT_HOME_SAFETY, _DEFAULT_POLY_CFG)
        # Total area should be approximately sum of both (they share one edge)
        total = p1.area_ha + p2.area_ha
        assert abs(result.original_area_ha - total) < 0.01


# ---------------------------------------------------------------------------
# reproject_to_3067
# ---------------------------------------------------------------------------


class TestReprojectTo3067:
    def test_output_in_finnish_metre_range(self):
        # A point in WGS84 near Seinäjoki → should land in Finnish 3067 range
        pt_4326 = Point(22.8, 62.8)
        pt_3067 = reproject_to_3067(pt_4326)
        # Finnish 3067 x: ~200 000–800 000; y: ~6 600 000–7 800 000
        assert 200_000 < pt_3067.x < 800_000
        assert 6_600_000 < pt_3067.y < 7_800_000

    def test_roundtrip_preserves_position(self):
        poly = make_parcel().geometry  # already in 3067
        poly_4326 = reproject_to_4326(poly)
        poly_back = reproject_to_3067(poly_4326)
        # Centroid should return to within a decimetre
        cx_orig, cy_orig = poly.centroid.x, poly.centroid.y
        assert abs(poly_back.centroid.x - cx_orig) < 0.1
        assert abs(poly_back.centroid.y - cy_orig) < 0.1


# ---------------------------------------------------------------------------
# apply_survey_offset
# ---------------------------------------------------------------------------


class TestApplySurveyOffset:
    def test_zero_offset_returns_identical(self):
        poly = make_parcel().geometry
        result = apply_survey_offset(poly, 0)
        assert result.equals(poly)

    def test_positive_offset_increases_area(self):
        poly = make_parcel().geometry
        result = apply_survey_offset(poly, 10)
        assert result.area > poly.area

    def test_negative_offset_decreases_area(self):
        poly = make_parcel(w=1000, h=1000).geometry
        result = apply_survey_offset(poly, -50)
        assert result.area < poly.area

    def test_collapse_offset_returns_original(self):
        # A tiny polygon with a huge inward offset collapses to empty → original returned
        tiny = make_parcel(w=5, h=5).geometry
        result = apply_survey_offset(tiny, -100)
        assert result.equals(tiny)

    def test_result_is_valid(self):
        poly = make_parcel().geometry
        result = apply_survey_offset(poly, 25)
        assert result.is_valid


# ---------------------------------------------------------------------------
# suggest_takeoff_point
# ---------------------------------------------------------------------------


class TestSuggestTakeoffPoint:
    def test_returns_two_floats(self):
        poly = make_parcel().geometry
        pt = suggest_takeoff_point(poly)
        assert len(pt) == 2
        assert all(isinstance(v, float) for v in pt)

    def test_point_on_boundary(self):
        poly = make_parcel(x=300_000, y=6_900_000, w=500, h=500).geometry
        x, y = suggest_takeoff_point(poly)
        pt = Point(x, y)
        # Should lie on (or very close to) the boundary
        assert poly.boundary.distance(pt) < 1.0

    def test_multipolygon_handled(self):
        p1 = make_parcel(x=300_000, w=500).geometry
        p2 = make_parcel(x=302_000, w=500, parcel_id="P2").geometry
        mp = MultiPolygon([p1, p2])
        pt = suggest_takeoff_point(mp)
        assert len(pt) == 2

    def test_within_bounding_box(self):
        poly = make_parcel(x=300_000, y=6_900_000, w=500, h=500).geometry
        x, y = suggest_takeoff_point(poly)
        xmin, ymin, xmax, ymax = poly.bounds
        assert xmin <= x <= xmax
        assert ymin <= y <= ymax
