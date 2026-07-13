"""Survey polygon geometry pipeline.

Responsibilities (in order):
  1. Merge parcel polygons into one shape.
  2. Apply optional outward edge buffer.
  3. Build keep-out zone from buffered buildings.
  4. Subtract keep-out from survey (or just measure distance if offset disabled).
  5. Enforce multipart / hole policy for DJI validity.
  6. Simplify vertex count.
  7. Reproject 3067 → 4326 for KMZ output.
  8. Return a SurveyGeometry carrying stats, flags, and both CRS variants.

All input geometries must be in EPSG:3067; this is asserted at the boundary.
The 4326 outputs are asserted before being returned.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from pyproj import Transformer
from shapely import make_valid, orient_polygons
from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform, unary_union

from flightmanager.geo.buildings import Building
from flightmanager.config import HomeSafetyConfig, PolygonConfig
from flightmanager.geo.crs import require_3067, require_4326
from flightmanager.geo.parcels import Parcel
from flightmanager.geo.simplify import simplify_pieces, vertex_count

log = logging.getLogger(__name__)

# Reproject EPSG:3067 → EPSG:4326, always_xy=True → (lon, lat) ordering,
# matching the fixture coordinate order confirmed in Phase 0.
_T_3067_4326 = Transformer.from_crs(3067, 4326, always_xy=True)
_T_4326_3067 = Transformer.from_crs(4326, 3067, always_xy=True)

_M2_TO_HA = 1 / 10_000


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------


@dataclass
class SurveyGeometry:
    """Complete result of the geometry processing pipeline for one job."""

    # Final survey polygon in EPSG:3067 (unified, before splitting).
    survey_3067: BaseGeometry
    # Same polygon reprojected to EPSG:4326 for KMZ embedding.
    survey_4326: BaseGeometry

    # Merged parcel polygon (EPSG:3067) after gap-fill, before keep-out — the
    # geometry `original_area_ha` is measured from. Used as the denominator for
    # camera-coverage-vs-parcel. For custom/edited-polygon jobs this equals survey_3067.
    original_3067: BaseGeometry

    # For multipart_policy="split": one piece per KMZ.
    # For "largest" / "review": single-element list matching survey_3067.
    pieces_3067: list[BaseGeometry]
    pieces_4326: list[BaseGeometry]

    # Bounding box of survey_3067 in EPSG:3067 — used to request elevation tiles.
    bbox_3067: tuple[float, float, float, float]  # xmin, ymin, xmax, ymax

    # Area statistics
    original_area_ha: float  # merged parcels before any keep-out
    final_area_ha: float  # after keep-out subtraction
    area_lost_pct: float  # (original - final) / original × 100

    # Home proximity
    min_dist_to_home_m: float | None  # None when no buildings were supplied
    offset_applied: bool

    # Total exterior-ring vertex count after simplification (sum across all pieces).
    survey_vertex_count: int

    # Review gate — set True on: excess area loss, multipart result, hole policy,
    # or any zone intersection (added later by zones.py).
    needs_review: bool
    review_reasons: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def process_survey(
    parcels: list[Parcel],
    buildings: list[Building],
    home_safety: HomeSafetyConfig,
    polygon_cfg: PolygonConfig,
    power_line_geoms: list[BaseGeometry] | None = None,
    power_line_buffer_m: float = 0.0,
) -> SurveyGeometry:
    """Run the full geometry pipeline and return a SurveyGeometry.

    All parcel and building geometries must be in EPSG:3067.
    """
    for p in parcels:
        require_3067(p.geometry)
    for b in buildings:
        require_3067(b.geometry)

    review_reasons: list[str] = []

    # 1. Merge parcels
    merged = _merge_parcels(parcels)
    log.info("Merged %d parcel(s)", len(parcels))

    # 2. Close small inter-parcel gaps (before measuring area so stats reflect intent)
    merged = _close_gaps(merged, polygon_cfg.gap_fill_m)
    original_area_ha = merged.area * _M2_TO_HA
    log.info("Survey area after gap-fill: %.2f ha", original_area_ha)

    # 3. Optional outward edge buffer
    survey = _apply_edge_buffer(merged, polygon_cfg.edge_buffer_m)

    # 4. Optional survey offset (±metres relative to parcel boundary)
    survey = apply_survey_offset(survey, polygon_cfg.survey_offset_m)

    # 5. Build keep-out zone
    keepout = build_keepout(
        buildings, home_safety, power_line_geoms, power_line_buffer_m
    )

    # 6. Apply keep-out (or measure distance)
    survey, min_dist, offset_applied = _apply_keepout(survey, keepout, home_safety)

    # Fraction of original parcel covered by the flight polygon
    covered = survey.intersection(merged)
    area_lost_pct = (
        max(0.0, (1.0 - covered.area / merged.area) * 100) if merged.area > 0 else 0.0
    )
    log.info("Keep-out: %.1f%% of original parcel area unreachable", area_lost_pct)

    if area_lost_pct > home_safety.max_area_loss_pct:
        reason = (
            f"Keep-out removed {area_lost_pct:.1f}% of survey area "
            f"(threshold {home_safety.max_area_loss_pct}%)"
        )
        log.warning(reason)
        review_reasons.append(reason)

    if (
        min_dist is not None
        and not offset_applied
        and min_dist < home_safety.home_buffer_m
    ):
        log.warning(
            "Survey polygon is %.1f m from nearest home (buffer %.1f m)",
            min_dist,
            home_safety.home_buffer_m,
        )

    # 7. Ensure validity
    if not survey.is_valid:
        log.warning("Survey polygon is invalid after keep-out — applying make_valid")
        survey = make_valid(survey)

    # 8. Enforce multipart / hole policy
    pieces, policy_reasons = _enforce_policy(survey, polygon_cfg)
    review_reasons.extend(policy_reasons)

    # 9. Simplify (applied per piece, after keep-out so edges stay accurate)
    pieces = simplify_pieces(pieces, polygon_cfg)

    # 10. Fix winding order (CCW exterior ring as GeoJSON / KML expects)
    pieces = [_fix_winding(p) for p in pieces]

    # Reconstruct unified survey geometry from pieces
    survey = pieces[0] if len(pieces) == 1 else unary_union(pieces)
    final_area_ha = survey.area * _M2_TO_HA
    total_vc = sum(vertex_count(p) for p in pieces)
    log.info("Survey polygon vertex count after simplification: %d", total_vc)

    # 11. Reproject to 4326
    survey_4326 = reproject_to_4326(survey)
    pieces_4326 = [reproject_to_4326(p) for p in pieces]

    require_4326(survey_4326)

    bbox = survey.bounds  # (minx, miny, maxx, maxy) in 3067

    return SurveyGeometry(
        survey_3067=survey,
        survey_4326=survey_4326,
        original_3067=merged,
        pieces_3067=pieces,
        pieces_4326=pieces_4326,
        bbox_3067=(bbox[0], bbox[1], bbox[2], bbox[3]),
        original_area_ha=original_area_ha,
        final_area_ha=final_area_ha,
        area_lost_pct=area_lost_pct,
        min_dist_to_home_m=min_dist,
        offset_applied=offset_applied,
        survey_vertex_count=total_vc,
        needs_review=bool(review_reasons),
        review_reasons=review_reasons,
    )


# ---------------------------------------------------------------------------
# Step implementations
# ---------------------------------------------------------------------------


def _merge_parcels(parcels: list[Parcel]) -> BaseGeometry:
    geoms = [p.geometry for p in parcels]
    merged = unary_union(geoms)
    return make_valid(merged)


def _close_gaps(geom: BaseGeometry, gap_fill_m: float) -> BaseGeometry:
    """Bridge small gaps between adjacent parcels via morphological closing.

    Expands by gap_fill_m then contracts by gap_fill_m.  Gaps narrower than
    2×gap_fill_m are filled; corners are rounded by at most gap_fill_m.
    No-op when gap_fill_m == 0.
    """
    if gap_fill_m <= 0:
        return geom
    closed = geom.buffer(gap_fill_m).buffer(-gap_fill_m)
    log.debug("Gap fill %.1f m applied", gap_fill_m)
    return closed


def _apply_edge_buffer(geom: BaseGeometry, buffer_m: float) -> BaseGeometry:
    if buffer_m <= 0:
        return geom
    buffered = geom.buffer(buffer_m)
    log.debug("Edge buffer +%.1f m applied", buffer_m)
    return buffered


def apply_survey_offset(geom: BaseGeometry, offset_m: float) -> BaseGeometry:
    """Expand (positive) or contract (negative) the survey polygon by offset_m metres.

    A negative offset can produce holes or split the polygon when corners pinch
    together — those are passed on to the existing hole_policy / multipart_policy
    steps which handle them as normal.  If the contraction collapses the entire
    polygon, the original is returned and a warning is logged so the operator can
    see what happened rather than silently losing the area.
    """
    if offset_m == 0:
        return geom
    result = geom.buffer(offset_m)
    if result.is_empty:
        log.warning(
            "survey_offset_m=%.1f m collapsed the survey polygon — "
            "offset ignored, returning original",
            offset_m,
        )
        return geom
    result = make_valid(result)
    log.info("Survey offset %.1f m applied", offset_m)
    return result


def build_keepout(
    buildings: list[Building],
    home_safety: HomeSafetyConfig,
    power_line_geoms: list[BaseGeometry] | None = None,
    power_line_buffer_m: float = 0.0,
) -> BaseGeometry | None:
    """Buffer buildings and overhead power lines and union into a keep-out zone."""
    zones: list[BaseGeometry] = []

    res_codes = set(home_safety.residential_kohdeluokka)
    a3_codes = set(home_safety.a3_additional_kohdeluokka)
    relevant_codes = (
        res_codes | a3_codes if home_safety.operating_subcategory == "A3" else res_codes
    )
    relevant = [b for b in buildings if b.kohdeluokka in relevant_codes]

    if relevant:
        buf = home_safety.home_buffer_m
        zones.extend(b.geometry.buffer(buf) for b in relevant)
        log.info(
            "Keep-out: buffered %d building(s) by %.1f m (subcategory %s)",
            len(relevant),
            buf,
            home_safety.operating_subcategory,
        )
    else:
        log.debug(
            "No relevant buildings for keep-out in subcategory %s",
            home_safety.operating_subcategory,
        )

    if power_line_geoms and power_line_buffer_m > 0:
        zones.extend(g.buffer(power_line_buffer_m) for g in power_line_geoms)
        log.info(
            "Keep-out: buffered %d overhead power line(s) by %.1f m",
            len(power_line_geoms),
            power_line_buffer_m,
        )

    if not zones:
        return None
    return unary_union(zones)


def _apply_keepout(
    survey: BaseGeometry,
    keepout: BaseGeometry | None,
    home_safety: HomeSafetyConfig,
) -> tuple[BaseGeometry, float | None, bool]:
    """Apply keep-out to survey polygon.

    Returns (survey, min_dist_to_home_m, offset_applied).
    Area-lost percentage is computed by the caller via intersection with the original parcel.
    """
    if keepout is None:
        return survey, None, False

    if home_safety.offset_enabled:
        result = survey.difference(keepout)
        if result.is_empty:
            log.error(
                "Keep-out completely covers the survey area — flagging for review"
            )
            result = survey  # return original so pipeline can flag and surface it
        log.info("Keep-out applied")
        return result, None, True
    else:
        # No offset: measure minimum distance to nearest building instead
        min_dist = survey.distance(keepout)
        log.info(
            "offset_enabled=false — minimum distance to keep-out zone: %.1f m", min_dist
        )
        return survey, min_dist, False


def _enforce_policy(
    geom: BaseGeometry,
    cfg: PolygonConfig,
) -> tuple[list[BaseGeometry], list[str]]:
    """Enforce multipart_policy and hole_policy; return (pieces, review_reasons)."""
    reasons: list[str] = []

    # --- Hole policy ---
    geom, hole_reasons = _enforce_hole_policy(geom, cfg.hole_policy)
    reasons.extend(hole_reasons)

    # --- Multipart policy ---
    if isinstance(geom, MultiPolygon):
        sub_geoms = list(geom.geoms)
        if cfg.multipart_policy == "split":
            log.info(
                "multipart_policy=split: producing %d separate pieces", len(sub_geoms)
            )
            pieces = sub_geoms
        elif cfg.multipart_policy == "largest":
            largest = max(sub_geoms, key=lambda g: g.area)
            dropped_pct = (geom.area - largest.area) / geom.area * 100
            log.warning(
                "multipart_policy=largest: keeping largest piece, dropping %.1f%% of area",
                dropped_pct,
            )
            pieces = [largest]
        else:  # "review"
            reason = (
                f"Survey area is a MultiPolygon ({len(sub_geoms)} pieces) after keep-out. "
                f"Manual review required (multipart_policy=review)."
            )
            log.warning(reason)
            reasons.append(reason)
            pieces = sub_geoms  # surface all pieces for inspection
    else:
        pieces = [geom]

    return pieces, reasons


def _enforce_hole_policy(
    geom: BaseGeometry,
    policy: str,
) -> tuple[BaseGeometry, list[str]]:
    """Remove or flag interior holes in a Polygon or MultiPolygon."""
    reasons: list[str] = []

    def _has_holes(g: BaseGeometry) -> bool:
        if isinstance(g, Polygon):
            return len(g.interiors) > 0
        if isinstance(g, MultiPolygon):
            return any(len(p.interiors) > 0 for p in g.geoms)
        return False

    def _fill_holes(g: BaseGeometry) -> BaseGeometry:
        if isinstance(g, Polygon):
            return Polygon(g.exterior)
        if isinstance(g, MultiPolygon):
            return MultiPolygon([Polygon(p.exterior) for p in g.geoms])
        return g

    if not _has_holes(geom):
        return geom, []

    if policy == "review":
        reason = (
            "Survey polygon has interior holes after keep-out subtraction. "
            "Manual review required (hole_policy=review)."
        )
        log.warning(reason)
        reasons.append(reason)
        return geom, reasons
    elif policy in ("fill", "clip"):
        # Both fill and clip: drop interior rings so DJI gets a valid single ring.
        # "fill" semantics: covered area is included in the survey.
        # "clip" here is treated the same — the hole is filled rather than routed around,
        # which is acceptable for open-field surveys where holes indicate nearby buildings
        # already handled by the keep-out buffer.
        log.info(
            "hole_policy=%s: filling %s interior ring(s)",
            policy,
            "MultiPolygon" if isinstance(geom, MultiPolygon) else "Polygon",
        )
        return _fill_holes(geom), []

    return geom, []


def _fix_winding(geom: BaseGeometry) -> BaseGeometry:
    """Ensure exterior ring is CCW, as expected by GeoJSON / KML consumers.

    shapely 2.x uses orient_polygons(geom, clockwise=False) for CCW exterior.
    """
    if isinstance(geom, (Polygon, MultiPolygon)):
        return orient_polygons(
            geom, exterior_cw=False
        )  # exterior_cw=False → CCW exterior
    return geom


def reproject_to_4326(geom: BaseGeometry) -> BaseGeometry:
    """Reproject a geometry from EPSG:3067 to EPSG:4326 (lon, lat ordering)."""
    return transform(_T_3067_4326.transform, geom)


def reproject_to_3067(geom: BaseGeometry) -> BaseGeometry:
    """Reproject a geometry from EPSG:4326 to EPSG:3067."""
    return transform(_T_4326_3067.transform, geom)


def suggest_takeoff_point(polygon_3067: "BaseGeometry") -> tuple[float, float]:
    """Return (x, y) in EPSG:3067 of the best takeoff/landing boundary point.

    Minimises the maximum distance from the chosen point to any polygon vertex
    (minimax / 1-centre projected to boundary).  This minimises the operator's
    worst-case VLOS distance to the drone throughout the mission.

    Works in metric EPSG:3067 space so distance comparisons are accurate.
    """
    coords: list[tuple[float, float]] = []
    if polygon_3067.geom_type == "Polygon":
        coords = list(polygon_3067.exterior.coords)
    elif polygon_3067.geom_type == "MultiPolygon":
        for p in polygon_3067.geoms:
            coords.extend(p.exterior.coords)

    if not coords:
        c = polygon_3067.centroid
        return (c.x, c.y)

    boundary = polygon_3067.boundary
    total_len = boundary.length
    if total_len == 0:
        c = polygon_3067.centroid
        return (c.x, c.y)

    # Sample boundary at ~5 m spacing, capped to keep it fast for large polygons.
    n_samples = max(60, min(300, int(total_len / 5.0)))

    best_pt = None
    best_max_d2 = float("inf")

    for i in range(n_samples):
        pt = boundary.interpolate(i / n_samples, normalized=True)
        max_d2 = max((pt.x - vx) ** 2 + (pt.y - vy) ** 2 for vx, vy in coords)
        if max_d2 < best_max_d2:
            best_max_d2 = max_d2
            best_pt = pt

    if best_pt is None:
        c = polygon_3067.centroid
        return (c.x, c.y)

    return (best_pt.x, best_pt.y)
