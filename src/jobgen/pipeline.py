"""Orchestrate one mapping job end-to-end.

Entry point: ``run_job()``.  Importable as a module for Airflow use.

Output directory layout::

    output/<jobname>/
    ├── <jobname>.kmz         WPML mapping route (EPSG:4326, terrain-follow)
    ├── site_dsm_wgs84.tif    DTM clipped to survey polygon, EPSG:4326
    ├── homes.kml             DJI Pilot 2 map layer: building pins
    ├── run.log               structured log for this run
    └── manifest.json         full provenance, stats, flags, attribution

MML API key is read from the environment variable ``MML_API_KEY``.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from jobgen.buildings import Building, dedup_buildings, load_tile, tile_fetcher as buildings_fetcher
from jobgen.powerlines import (
    PowerLine, dedup_power_lines, load_tile as load_pl_tile, tile_fetcher as powerlines_fetcher,
    Pylon, dedup_pylons, load_pylon_tile, pylon_tile_fetcher, correct_overhead_from_pylons,
)
from jobgen.cache import FetcherFn, TileRecord, get_tiles, tile_provenance
from jobgen.config import AppConfig
from jobgen.elevation import tile_fetcher as dem_fetcher, validate_tile
from jobgen.geometry import (
    SurveyGeometry, apply_survey_offset, build_keepout, process_survey,
    reproject_to_4326, reproject_to_3067, vertex_count, suggest_takeoff_point,
)
from jobgen.logging_setup import setup_logging
from jobgen.manifest import build_manifest
from jobgen.parcels import fetch_parcels
from jobgen.properties import fetch_properties
from jobgen.raster import build_site_dsm
from jobgen.preview import build_map_preview, build_preview_dsm_thumbnail
from jobgen.wpml import build_homes_kml, build_kmz, resolve_strip_speed
from jobgen.zones import ZoneHit, check_zones

log = logging.getLogger(__name__)


def _cb(fn: Callable | None, stage: str, msg: str, pct: int) -> None:
    if fn:
        fn(stage, msg, pct)

# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_job(
    job_name: str,
    config: AppConfig,
    *,
    parcel_ids: list[str] | None = None,
    property_ids: list[str] | None = None,
    bbox_3067: tuple[float, float, float, float] | None = None,
    dry_run: bool = False,
    refresh: bool = False,
    progress_cb: Callable[[str, str, int], None] | None = None,
    custom_polygon_4326: Any | None = None,
    folder: str | None = None,
) -> dict:
    """Run one mapping job and return the manifest dict.

    At least one of *parcel_ids*, *property_ids*, or *bbox_3067* must be
    provided. *parcel_ids* and *property_ids* may be combined — their
    geometries are unioned before processing.
    If *dry_run* is True, all fetching and validation runs but no output
    files are written.

    Raises on hard errors (missing tiles in offline mode, invalid config, etc.).
    Review flags are recorded in the manifest rather than raising.
    """
    base = Path(config.output.output_dir)
    job_dir = base / folder / job_name if folder else base / job_name
    job_dir.mkdir(parents=True, exist_ok=True)

    setup_logging(job_name, job_dir.parent)
    log.info("=== Job %s starting (dry_run=%s) ===", job_name, dry_run)

    _cb(progress_cb, "start", "Starting job…", 0)
    api_key = _require_api_key()
    run_ts = datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # 1–3. Inputs (parcels / properties / buildings)
    # ------------------------------------------------------------------
    inp = _fetch_survey_inputs(
        parcel_ids=parcel_ids,
        bbox_3067=bbox_3067,
        property_ids=property_ids,
        custom_polygon_4326=custom_polygon_4326,
        api_key=api_key,
        config=config,
        refresh=refresh,
        progress_cb=progress_cb,
    )
    include_buf = config.home_safety.resolved_include_buffer_m
    _preview_radius_cfg = config.home_safety.preview_radius_m

    if custom_polygon_4326 is not None:
        survey_geom = _synth_survey_geom(custom_polygon_4326, config.polygon.survey_offset_m)
    else:
        _cb(progress_cb, "geometry", "Computing survey polygon…", 45)
        _pl_geoms_job = [pl.geometry for pl in inp.power_lines if pl.is_overhead] or None
        _pl_buf_job = config.powerlines.overhead_buffer_m if config.powerlines.enabled else 0.0
        survey_geom = process_survey(
            inp.input_geoms, inp.buildings, config.home_safety, config.polygon,
            _pl_geoms_job, _pl_buf_job,
        )
    pieces_count = len(survey_geom.pieces_3067)

    all_review_reasons: list[str] = list(survey_geom.review_reasons)

    # ------------------------------------------------------------------
    # 5. Elevation tiles (cache) + site DSM
    # ------------------------------------------------------------------
    _cb(progress_cb, "elevation", "Fetching elevation tiles…", 60)
    log.info("Fetching DEM tiles …")
    d_fetcher = dem_fetcher(api_key)
    _dem_margin = config.flight.dsm_margin_m
    _bx0, _by0, _bx1, _by1 = survey_geom.bbox_3067
    _dem_bbox = (_bx0 - _dem_margin, _by0 - _dem_margin,
                 _bx1 + _dem_margin, _by1 + _dem_margin)
    d_records = get_tiles(
        "dem", _dem_bbox, d_fetcher, config.cache, refresh=refresh
    )
    tile_paths = [r.path for r in d_records]

    dsm_stats: dict = {}
    if not dry_run:
        log.info("Building site DSM …")
        dsm_path = job_dir / f"{job_name}_dsm.tif"
        dsm_stats = build_site_dsm(
            tile_paths, survey_geom.survey_4326, dsm_path,
            margin_m=config.flight.dsm_margin_m,
        )

    # ------------------------------------------------------------------
    # 6. Zone check
    # ------------------------------------------------------------------
    _cb(progress_cb, "zones", "Checking UAS restriction zones…", 75)
    drone_cfg = config.active_drone()
    flight_height_m = drone_cfg.height_from_gsd(config.flight.target_gsd_cm)
    # 3× flight height is the "horizontal 3:1 rule" often used for risk assessment.
    preview_radius_m = (
        _preview_radius_cfg if _preview_radius_cfg is not None
        else 3.0 * flight_height_m
    )
    log.info("Preview yellow-circle radius: %.0f m", preview_radius_m)

    log.info("Checking UAS restriction zones …")
    zone_result = check_zones(
        survey_geom.survey_4326,
        config.zones,
        flight_height_m=flight_height_m,
        cache_dir=config.cache.cache_dir,
    )
    all_review_reasons.extend(zone_result.reasons)

    # ------------------------------------------------------------------
    # 7. KMZ — one per piece (split policy) or single
    # ------------------------------------------------------------------
    _cb(progress_cb, "kmz", "Building KMZ route…", 85)
    kmz_results = []
    if not dry_run:
        for i, (piece_3067, piece_4326) in enumerate(
            zip(survey_geom.pieces_3067, survey_geom.pieces_4326)
        ):
            suffix = f"-{i+1}" if pieces_count > 1 else ""
            kmz_path = job_dir / f"{job_name}{suffix}.kmz"
            try:
                result = build_kmz(piece_4326, config.flight, kmz_path,
                                   dsm_path=dsm_path if not dry_run else None,
                                   drone=drone_cfg)
                kmz_results.append(result)
                if result.over_one_battery:
                    reason = (
                        f"Piece {i+1}: estimated flight time "
                        f"{result.estimated_flight_time_min:.1f} min "
                        f"exceeds one battery. Consider splitting."
                    )
                    all_review_reasons.append(reason)
            except ValueError as e:
                reason = f"KMZ not written for piece {i+1}: {e}"
                log.warning(reason)
                all_review_reasons.append(reason)

    # ------------------------------------------------------------------
    # 8. Homes KML
    # ------------------------------------------------------------------
    _cb(progress_cb, "homes", "Writing homes KML…", 93)
    # Filter to buildings within include_buf of the closest point on the survey
    # polygon. include_buf defaults to 2× keep-out buffer so houses just outside
    # the keep-out zone still appear on the RC map.
    nearby = [
        b for b in inp.buildings
        if survey_geom.survey_3067.distance(b.geometry) <= include_buf
    ]
    log.info(
        "%d of %d building(s) within %.0f m of survey polygon",
        len(nearby), len(inp.buildings), include_buf,
    )
    if not dry_run:
        log.info("Writing homes KML …")
        # Buildings must be in EPSG:4326 for the KML pin coordinates
        buildings_4326 = [
            dataclasses.replace(b, geometry=reproject_to_4326(b.geometry))
            for b in nearby
        ]
        build_homes_kml(
            buildings_4326,
            job_dir / f"{job_name}_homes.kml",
            home_safety=config.home_safety,
        )

    # ------------------------------------------------------------------
    # 9. Manifest
    # ------------------------------------------------------------------
    needs_review = bool(all_review_reasons) or not zone_result.checked
    flight_ready = zone_result.flight_ready and not needs_review

    dem_prov  = tile_provenance(d_records)
    bldg_prov = tile_provenance(inp.b_records)

    manifest = build_manifest(
        job_name=job_name,
        run_ts=run_ts,
        dry_run=dry_run,
        config=config,
        parcel_ids_used=inp.parcel_ids_used,
        parcel_fetch_ts=inp.parcel_fetch_ts,
        property_ids_used=inp.property_ids_used,
        property_display_ids_used=inp.property_display_ids_used,
        property_fetch_ts=inp.property_fetch_ts,
        survey_geom=survey_geom,
        pieces_count=pieces_count,
        drone_cfg=drone_cfg,
        flight_height_m=flight_height_m,
        strip_speed_ms=resolve_strip_speed(config.flight, drone_cfg, flight_height_m),
        kmz_results=kmz_results,
        dsm_stats=dsm_stats,
        dem_prov=dem_prov,
        buildings=inp.buildings,
        nearby=nearby,
        include_buf=include_buf,
        preview_radius_m=preview_radius_m,
        bldg_prov=bldg_prov,
        zone_result=zone_result,
        needs_review=needs_review,
        flight_ready=flight_ready,
        all_review_reasons=all_review_reasons,
    )

    if not dry_run:
        parcels_4326 = [reproject_to_4326(p.geometry) for p in inp.input_geoms]
        build_map_preview(
            survey_geom.survey_4326,
            buildings_4326,
            job_dir / f"{job_name}_map.html",
            job_name=job_name,
            home_safety=config.home_safety,
            manifest=manifest,
            parcels_4326=parcels_4326,
            zone_hits=zone_result.intersecting_zones,
            zone_result=zone_result,
            dsm_path=dsm_path,
            preview_radius_m=preview_radius_m,
            keepout_ignored=not config.home_safety.offset_enabled,
        )

        manifest_path = job_dir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        log.info("Manifest written: %s", manifest_path)

    status = "NEEDS REVIEW" if needs_review else ("FLIGHT READY" if flight_ready else "NOT FLIGHT READY")
    log.info("=== Job %s complete — %s ===", job_name, status)
    _cb(progress_cb, "complete", f"Job complete — {status}", 100)
    return manifest


# ---------------------------------------------------------------------------
# Preview entry point (web UI — no file I/O, no DEM/DSM/KMZ)
# ---------------------------------------------------------------------------



def run_preview(
    config: AppConfig,
    *,
    parcel_ids: list[str] | None = None,
    property_ids: list[str] | None = None,
    bbox_3067: tuple[float, float, float, float] | None = None,
    refresh: bool = False,
    progress_cb: Callable[[str, str, int], None] | None = None,
    custom_polygon_4326: Any | None = None,
) -> dict:
    """Run parcels → buildings → geometry → zones; return GeoJSON dict.

    No files are written. DEM and DSM stages are skipped to keep latency low.
    At least one of *parcel_ids*, *property_ids*, or *bbox_3067* must be provided.
    Building tiles are served from the SQLite tile cache — no in-memory caching needed.
    """
    from shapely.geometry import mapping

    _cb(progress_cb, "start", "Starting preview…", 0)
    api_key = _require_api_key()

    buf = config.home_safety.home_buffer_m

    # 1–3. Inputs (parcels / properties / buildings)
    inp = _fetch_survey_inputs(
        parcel_ids=parcel_ids,
        bbox_3067=bbox_3067,
        property_ids=property_ids,
        custom_polygon_4326=custom_polygon_4326,
        api_key=api_key,
        config=config,
        refresh=refresh,
        progress_cb=progress_cb,
    )
    log.info("Preview: %d building(s)", len(inp.buildings))

    # 3. Survey polygon
    _cb(progress_cb, "geometry", "Computing survey polygon…", 45)
    if custom_polygon_4326 is not None:
        log.info("Preview: using custom polygon (bridge/cut applied)")
        survey_geom = _synth_survey_geom(custom_polygon_4326, config.polygon.survey_offset_m)
    else:
        log.info("Preview: computing survey geometry …")
        _pl_geoms_prev = [pl.geometry for pl in inp.power_lines if pl.is_overhead] or None
        _pl_buf_prev = config.powerlines.overhead_buffer_m if config.powerlines.enabled else 0.0
        survey_geom = process_survey(
            inp.input_geoms, inp.buildings, config.home_safety, config.polygon,
            _pl_geoms_prev, _pl_buf_prev,
        )

    # 4. Keep-out zone geometry for visualisation (overhead lines only for buffer)
    _pl_buf = config.powerlines.overhead_buffer_m if config.powerlines.enabled else 0.0
    _overhead_geoms = [pl.geometry for pl in inp.power_lines if pl.is_overhead] or None
    keepout_3067 = build_keepout(inp.buildings, config.home_safety, _overhead_geoms, _pl_buf)
    keepout_4326 = reproject_to_4326(keepout_3067) if keepout_3067 else None

    # Power line keepout buffer for map display (overhead only)
    _pl_keepout_4326 = None
    if _overhead_geoms and _pl_buf > 0:
        from shapely.ops import unary_union as _uu
        _pl_ko_3067 = _uu([g.buffer(_pl_buf) for g in _overhead_geoms])
        _pl_keepout_4326 = reproject_to_4326(_pl_ko_3067)

    # 5. Zone check
    drone_cfg = config.active_drone()
    flight_height_m = drone_cfg.height_from_gsd(config.flight.target_gsd_cm)
    # 5. DSM thumbnail — fetch DEM tiles (cached) and build a low-res display image
    dsm_b64: str | None = None
    dsm_bounds: tuple | None = None
    try:
        _cb(progress_cb, "elevation", "Fetching elevation tiles…", 60)
        log.info("Preview: fetching DEM tiles for thumbnail …")
        d_fetcher = dem_fetcher(api_key)
        _dem_margin = 150
        _bx0, _by0, _bx1, _by1 = survey_geom.bbox_3067
        _dem_bbox = (_bx0 - _dem_margin, _by0 - _dem_margin,
                     _bx1 + _dem_margin, _by1 + _dem_margin)
        d_records = get_tiles("dem", _dem_bbox, d_fetcher, config.cache, refresh=refresh)
        dsm_b64, dsm_bounds = build_preview_dsm_thumbnail(
            [r.path for r in d_records], survey_geom.survey_4326
        )
        log.info("Preview: DSM thumbnail %s", "built" if dsm_b64 else "empty")
    except Exception as _dsm_exc:
        log.warning("Preview: DSM thumbnail skipped — %s", _dsm_exc)

    _cb(progress_cb, "zones", "Checking UAS restriction zones…", 75)
    log.info("Preview: checking zones …")
    zone_result = check_zones(
        survey_geom.survey_4326,
        config.zones,
        flight_height_m=flight_height_m,
        cache_dir=config.cache.cache_dir,
    )

    # Determine which building codes are "relevant" for keep-out display
    res_codes = set(config.home_safety.residential_kohdeluokka)
    a3_codes = set(config.home_safety.a3_additional_kohdeluokka)
    relevant_codes = (
        res_codes | a3_codes
        if config.home_safety.operating_subcategory == "A3"
        else res_codes
    )

    # Build buildings data for map display
    buildings_data = []
    for b in inp.buildings:
        is_ko = b.kohdeluokka in relevant_codes
        b_4326 = reproject_to_4326(b.geometry)
        buf_geom_4326 = reproject_to_4326(b.geometry.buffer(buf)) if is_ko else None
        buildings_data.append({
            "geojson": dict(mapping(b_4326)),
            "kohdeluokka": b.kohdeluokka,
            "is_keepout": is_ko,
            "buffer_geojson": dict(mapping(buf_geom_4326)) if buf_geom_4326 else None,
        })

    # Zone geometries for map display; include altitude limits and nesting.
    # Direct hits + related inner zones are all included; related ones are flagged context_only.
    all_zone_hits = [
        (h, False) for h in zone_result.intersecting_zones
    ] + [
        (h, True)  for h in zone_result.related_zones
    ]
    all_hit_objs = [h for h, _ in all_zone_hits]
    zone_hits_data = []
    for i, (h, context_only) in enumerate(all_zone_hits):
        geojson = _zone_geojson(h)
        contained_by = []
        if h.geom is not None:
            for j, (other, _) in enumerate(all_zone_hits):
                if i != j and other.geom is not None and other.geom.contains(h.geom):
                    contained_by.append({"id": other.identifier, "name": other.name})
        zone_hits_data.append({
            "geojson":      geojson,
            "identifier":   h.identifier,
            "name":         h.name,
            "restriction":  h.restriction,
            "upper_limit":  h.altitude.upper_limit,
            "upper_uom":    h.altitude.upper_uom,
            "upper_ref":    h.altitude.upper_ref,
            "lower_limit":  h.altitude.lower_limit,
            "lower_uom":    h.altitude.lower_uom,
            "lower_ref":    h.altitude.lower_ref,
            "contained_by": contained_by,
            "context_only": context_only,
            "buffer_only":  h.buffer_only if not context_only else False,
        })

    all_review_reasons = list(survey_geom.review_reasons) + list(zone_result.reasons)
    needs_review = bool(all_review_reasons) or not zone_result.checked
    flight_ready = zone_result.flight_ready and not needs_review

    _cb(progress_cb, "complete", "Preview complete", 100)

    # Suggest takeoff/landing position: boundary point minimising max VLOS distance.
    try:
        _tkx, _tky = suggest_takeoff_point(survey_geom.survey_3067)
        from shapely.geometry import Point as _Point
        _tk_4326 = reproject_to_4326(_Point(_tkx, _tky))
        takeoff_point_4326 = [_tk_4326.x, _tk_4326.y]
    except Exception:
        takeoff_point_4326 = None

    # Route estimate: actual strip intersections for flight-time/photo preview.
    from jobgen import route as _route
    _home_3067 = None
    if takeoff_point_4326:
        from shapely.geometry import Point as _HPt
        _hp = reproject_to_3067(_HPt(*takeoff_point_4326))
        _home_3067 = (_hp.x, _hp.y)
    _drone_cfg = drone_cfg  # already resolved above
    _H   = flight_height_m
    _p_m = _drone_cfg.pixel_pitch_um * 1e-6
    _f_m = _drone_cfg.focal_length_mm * 1e-3
    _sm  = _H * _drone_cfg.image_width_px  * _p_m / _f_m * (1 - config.flight.overlap_side_pct  / 100)
    _pm  = _H * _drone_cfg.image_height_px * _p_m / _f_m * (1 - config.flight.overlap_front_pct / 100)
    try:
        _angle_auto = _route.compute_auto_angle(survey_geom.survey_3067)
        _rr = _route.compute_route(survey_geom.survey_3067, _angle_auto, _sm, _pm,
                                   home_3067=_home_3067)
        _ft = _route.estimate_flight_time(
            _rr,
            flight_height_m=_H,
            auto_speed_ms=resolve_strip_speed(config.flight, _drone_cfg, _H),
            transit_speed_ms=config.flight.transitional_speed_ms,
            takeoff_security_height_m=config.flight.takeoff_security_height_m,
            home_3067=_home_3067,
        )
        _route_stats = {
            "route_angle_deg_auto": round(_angle_auto, 1),
            "route_strip_count":    _rr.strip_count,
            "route_photo_count":    _rr.photo_count,
            "route_flight_time_min": round(_ft, 1),
        }
    except Exception as _re:
        log.warning("Preview: route estimate failed — %s", _re)
        _angle_auto = 0.0
        _route_stats = {"route_angle_deg_auto": None, "route_strip_count": None,
                        "route_photo_count": None, "route_flight_time_min": None}

    # Power lines for map display (both overhead and underground)
    power_lines_data = []
    for pl in inp.power_lines:
        pl_4326 = reproject_to_4326(pl.geometry)
        power_lines_data.append({"geojson": dict(mapping(pl_4326)), "is_overhead": pl.is_overhead})

    result = {
        "survey": dict(mapping(survey_geom.survey_4326)),
        "original_areas": [dict(mapping(reproject_to_4326(p.geometry))) for p in inp.input_geoms],
        "buildings": buildings_data,
        "keepout_zone": dict(mapping(keepout_4326)) if keepout_4326 else None,
        "power_lines": power_lines_data,
        "powerlines_keepout": dict(mapping(_pl_keepout_4326)) if _pl_keepout_4326 else None,
        "zone_hits": zone_hits_data,
        "takeoff_point_4326": takeoff_point_4326,
        "dsm_b64": dsm_b64,
        "dsm_bounds": list(dsm_bounds) if dsm_bounds else None,
        "stats": {
            "original_area_ha": round(survey_geom.original_area_ha, 4),
            "final_area_ha": round(survey_geom.final_area_ha, 4),
            "area_lost_pct": round(survey_geom.area_lost_pct, 2),
            "survey_vertex_count": survey_geom.survey_vertex_count,
            "flight_height_m": round(flight_height_m, 2),
            "target_gsd_cm": round(config.flight.target_gsd_cm, 2),
            "drone": drone_cfg.name,
            "drone_label": drone_cfg.label,
            "needs_review": needs_review,
            "flight_ready": flight_ready,
            "review_reasons": all_review_reasons,
            "zones_checked": zone_result.checked,
            "zones_clear": not zone_result.intersecting_zones,
            "zone_count": len(zone_result.intersecting_zones),
            "zones_attribution": zone_result.attribution,
            "home_buffer_m": buf,
            "has_parcels": any(hasattr(g, "parcel_id") for g in inp.input_geoms),
            "has_properties": any(hasattr(g, "property_id") for g in inp.input_geoms),
            **_route_stats,
        },
    }
    return result


def _zone_geojson(hit: ZoneHit) -> dict | None:
    try:
        return hit.properties["geometry"][0].get("horizontalProjection")
    except (KeyError, IndexError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _InputResult:
    """Bundled output of the shared input-fetching stages (parcels/properties/buildings)."""
    input_geoms: list
    buildings: list[Building]
    b_records: list[TileRecord]
    power_lines: list[PowerLine]
    pl_records: list[TileRecord]
    # Manifest provenance — populated for every run; empty when no IDs were fetched
    parcel_ids_used: list[str]
    parcel_fetch_ts: str
    property_ids_used: list[str]
    property_display_ids_used: list[str]
    property_fetch_ts: str | None


def _synth_survey_geom(poly_4326: Any, offset_m: float) -> SurveyGeometry:
    """Build a synthetic SurveyGeometry from a user-supplied polygon.

    Used when the user has drawn or edited the polygon directly, bypassing
    ``process_survey()``.  The offset is applied immediately; the result is
    flagged ``needs_review=True`` so the pilot must verify boundaries.
    """
    survey_3067 = reproject_to_3067(poly_4326)
    survey_3067_off = apply_survey_offset(survey_3067, offset_m)
    survey_4326_off = reproject_to_4326(survey_3067_off)
    area_ha = survey_3067_off.area / 10_000
    vc = vertex_count(survey_4326_off)
    return SurveyGeometry(
        survey_3067=survey_3067_off,
        survey_4326=survey_4326_off,
        pieces_3067=[survey_3067_off],
        pieces_4326=[survey_4326_off],
        bbox_3067=survey_3067_off.bounds,
        original_area_ha=area_ha,
        final_area_ha=area_ha,
        area_lost_pct=0.0,
        min_dist_to_home_m=None,
        offset_applied=offset_m != 0.0,
        survey_vertex_count=vc,
        needs_review=False,
        review_reasons=[],
    )


def _load_buildings(
    buildings_bbox: tuple,
    api_key: str,
    cache_config,
    refresh: bool,
) -> tuple[list[Building], list[TileRecord]]:
    """Fetch building tiles from cache and return ``(buildings, tile_records)``."""
    b_fetcher = buildings_fetcher(api_key)
    b_records = get_tiles("buildings", buildings_bbox, b_fetcher, cache_config, refresh=refresh)
    raw: list[Building] = []
    for rec in b_records:
        raw.extend(load_tile(rec.path))
    buildings = dedup_buildings(raw)
    log.info("%d building(s) loaded after dedup", len(buildings))
    return buildings, b_records


def _load_powerlines(
    bbox: tuple,
    api_key: str,
    cache_config,
    refresh: bool,
) -> tuple[list[PowerLine], list[TileRecord]]:
    """Fetch power line tiles from cache and return ``(lines, tile_records)``."""
    pl_fetcher = powerlines_fetcher(api_key)
    pl_records = get_tiles("powerlines", bbox, pl_fetcher, cache_config, refresh=refresh)
    raw: list[PowerLine] = []
    for rec in pl_records:
        raw.extend(load_pl_tile(rec.path))
    lines = dedup_power_lines(raw)
    log.info("%d power line(s) loaded after dedup", len(lines))
    return lines, pl_records


def _load_pylons(
    bbox: tuple,
    api_key: str,
    cache_config,
    refresh: bool,
) -> list[Pylon]:
    """Fetch HV pylon tower tiles from cache and return pylon list."""
    py_fetcher = pylon_tile_fetcher(api_key)
    py_records = get_tiles("pylons", bbox, py_fetcher, cache_config, refresh=refresh)
    raw: list[Pylon] = []
    for rec in py_records:
        raw.extend(load_pylon_tile(rec.path))
    pylons = dedup_pylons(raw)
    log.info("%d pylon(s) loaded after dedup", len(pylons))
    return pylons


def _fetch_survey_inputs(
    *,
    parcel_ids: list[str] | None,
    bbox_3067: tuple[float, float, float, float] | None,
    property_ids: list[str] | None,
    custom_polygon_4326: Any | None,
    api_key: str,
    config: AppConfig,
    refresh: bool,
    progress_cb: Callable | None,
) -> "_InputResult":
    """Fetch parcels, properties and building tiles; return a bundled result.

    Shared by ``run_job()`` and ``run_preview()``.  Progress uses the unified
    ladder: parcels=10%, properties=20%, buildings=30%.

    ``custom_polygon_4326`` takes precedence over ``input_geoms`` for the
    building-fetch bounding box when both are provided.
    """
    from shapely import make_valid
    from shapely.ops import unary_union

    input_geoms: list = []
    parcel_ids_used: list[str] = []
    parcel_fetch_ts: str = datetime.now(timezone.utc).isoformat()
    property_ids_used: list[str] = []
    property_display_ids_used: list[str] = []
    property_fetch_ts: str | None = None

    if parcel_ids is not None or bbox_3067 is not None:
        _cb(progress_cb, "parcels", "Fetching parcels…", 10)
        log.info("Fetching parcels …")
        parcels = fetch_parcels(
            parcel_ids=parcel_ids, bbox=bbox_3067,
            config=config.parcels, cache_config=config.cache,
        )
        if not parcels:
            raise ValueError("No parcels returned — check parcel IDs or bbox.")
        parcel_ids_used = [p.parcel_id for p in parcels]
        log.info("%d parcel(s) fetched: %s", len(parcels), parcel_ids_used)
        input_geoms.extend(parcels)

    if property_ids is not None:
        property_fetch_ts = datetime.now(timezone.utc).isoformat()
        _cb(progress_cb, "properties", "Fetching properties…", 20)
        log.info("Fetching kiinteistöt …")
        props = fetch_properties(
            property_ids, api_key,
            timeout_s=config.properties.timeout_s,
            page_size=config.properties.page_size,
            cache_config=config.cache,
        )
        property_ids_used = [p.property_id for p in props]
        property_display_ids_used = [p.display_id for p in props]
        log.info("%d kiinteistö(t) fetched: %s", len(props), property_display_ids_used)
        input_geoms.extend(props)

    if not input_geoms and custom_polygon_4326 is None:
        raise ValueError("No input geometries — provide --parcels, --properties, or --bbox.")

    if custom_polygon_4326 is not None:
        prelim_bounds = reproject_to_3067(custom_polygon_4326).bounds
    else:
        prelim_bounds = make_valid(unary_union([p.geometry for p in input_geoms])).bounds

    include_buf = config.home_safety.resolved_include_buffer_m
    buildings_bbox = (
        prelim_bounds[0] - include_buf,
        prelim_bounds[1] - include_buf,
        prelim_bounds[2] + include_buf,
        prelim_bounds[3] + include_buf,
    )
    _cb(progress_cb, "buildings", "Fetching building tiles…", 30)
    buildings, b_records = _load_buildings(buildings_bbox, api_key, config.cache, refresh)

    power_lines: list[PowerLine] = []
    pl_records: list[TileRecord] = []
    if config.powerlines.enabled:
        try:
            power_lines, pl_records = _load_powerlines(buildings_bbox, api_key, config.cache, refresh)
            pylons = _load_pylons(buildings_bbox, api_key, config.cache, refresh)
            power_lines = correct_overhead_from_pylons(power_lines, pylons)
        except Exception as _pl_exc:
            log.warning("Power lines fetch failed — skipping: %s", _pl_exc)

    return _InputResult(
        input_geoms=input_geoms,
        buildings=buildings,
        b_records=b_records,
        power_lines=power_lines,
        pl_records=pl_records,
        parcel_ids_used=parcel_ids_used,
        parcel_fetch_ts=parcel_fetch_ts,
        property_ids_used=property_ids_used,
        property_display_ids_used=property_display_ids_used,
        property_fetch_ts=property_fetch_ts,
    )




def _require_api_key() -> str:
    key = os.environ.get("MML_API_KEY", "")
    if not key:
        raise RuntimeError(
            "MML_API_KEY environment variable is not set. "
            "Obtain a key at https://www.maanmittauslaitos.fi/rajapinnat/api-avaimen-ohje "
            "and set it in your .env file."
        )
    return key
