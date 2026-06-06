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

from jobgen import tool_version
from jobgen.buildings import Building, dedup_buildings, load_tile, tile_fetcher as buildings_fetcher
from jobgen.cache import FetcherFn, TileRecord, get_tiles, tile_provenance
from jobgen.config import AppConfig
from jobgen.elevation import tile_fetcher as dem_fetcher, validate_tile
from jobgen.geometry import (
    SurveyGeometry, apply_survey_offset, build_keepout, process_survey,
    reproject_to_4326, reproject_to_3067, vertex_count, suggest_takeoff_point,
)
from jobgen.logging_setup import setup_logging
from jobgen.parcels import fetch_parcels
from jobgen.properties import fetch_properties
from jobgen.raster import build_site_dsm
from jobgen.preview import build_map_preview, build_preview_dsm_thumbnail
from jobgen.wpml import build_homes_kml, build_kmz
from jobgen.zones import ZoneHit, check_zones

log = logging.getLogger(__name__)


def _cb(fn: Callable | None, stage: str, msg: str, pct: int) -> None:
    if fn:
        fn(stage, msg, pct)

_CC_BY = {
    "elevation": (
        "Contains data from the National Land Survey of Finland, "
        "Elevation model 2 m, retrieved {date}."
    ),
    "buildings": (
        "Contains data from the National Land Survey of Finland, "
        "Topographic Database, retrieved {date}."
    ),
    "parcels": (
        "Contains data from Ruokavirasto (Finnish Food Authority), "
        "Peltolohkorekisteri, retrieved {date}."
    ),
    "properties": (
        "Contains data from the National Land Survey of Finland, "
        "Cadastral Index Map, retrieved {date}."
    ),
    "zones": (
        "Contains data from Traficom, "
        "UAS Geographical Zones, retrieved {date}."
    ),
}


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
    job_dir = Path(config.output.output_dir) / job_name
    job_dir.mkdir(parents=True, exist_ok=True)

    setup_logging(job_name, config.output.output_dir)
    log.info("=== Job %s starting (dry_run=%s) ===", job_name, dry_run)

    _cb(progress_cb, "start", "Starting job…", 0)
    api_key = _require_api_key()
    run_ts = datetime.now(timezone.utc).isoformat()
    parcel_fetch_ts = datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # 1. Area inputs — parcels and/or properties
    # ------------------------------------------------------------------
    input_geoms: list = []
    parcel_fetch_ts = datetime.now(timezone.utc).isoformat()
    parcel_ids_used: list[str] = []
    property_ids_used: list[str] = []
    property_display_ids_used: list[str] = []
    property_fetch_ts: str | None = None

    if parcel_ids is not None or bbox_3067 is not None:
        _cb(progress_cb, "parcels", "Fetching parcels…", 8)
        log.info("Fetching parcels …")
        parcels = fetch_parcels(
            parcel_ids=parcel_ids,
            bbox=bbox_3067,
            config=config.parcels,
            cache_config=config.cache,
        )
        if not parcels:
            raise ValueError("No parcels returned — check parcel IDs or bbox.")
        parcel_ids_used = [p.parcel_id for p in parcels]
        log.info("%d parcel(s) fetched: %s", len(parcels), parcel_ids_used)
        input_geoms.extend(parcels)

    if property_ids is not None:
        property_fetch_ts = datetime.now(timezone.utc).isoformat()
        _cb(progress_cb, "properties", "Fetching properties…", 15)
        log.info("Fetching kiinteistöt …")
        props = fetch_properties(
            property_ids,
            api_key,
            timeout_s=config.properties.timeout_s,
            page_size=config.properties.page_size,
            cache_config=config.cache,
        )
        property_ids_used = [p.property_id for p in props]
        property_display_ids_used = [p.display_id for p in props]
        log.info(
            "%d kiinteistö(t) fetched: %s",
            len(props), property_display_ids_used,
        )
        input_geoms.extend(props)

    if not input_geoms and custom_polygon_4326 is None:
        raise ValueError("No input geometries — provide --parcels, --properties, or --bbox.")

    # ------------------------------------------------------------------
    # 2 & 3. Buildings + geometry pipeline (or accept custom_polygon_4326)
    # ------------------------------------------------------------------
    from shapely.ops import unary_union
    from shapely import make_valid

    # Determine prelim bounds for the building-fetch bbox.
    # custom_polygon_4326 takes precedence when provided; the offset is applied
    # later inside _synth_survey_geom, so raw bounds are fine here.
    if custom_polygon_4326 is not None:
        prelim_bounds = reproject_to_3067(custom_polygon_4326).bounds
    else:
        log.info("Processing survey geometry …")
        prelim_bounds = make_valid(unary_union([p.geometry for p in input_geoms])).bounds

    buf = config.home_safety.home_buffer_m
    include_buf = config.home_safety.resolved_include_buffer_m
    _preview_radius_cfg = config.home_safety.preview_radius_m
    buildings_bbox = (
        prelim_bounds[0] - include_buf,
        prelim_bounds[1] - include_buf,
        prelim_bounds[2] + include_buf,
        prelim_bounds[3] + include_buf,
    )

    _cb(progress_cb, "buildings", "Fetching building tiles…", 25)
    buildings, b_records = _load_buildings(buildings_bbox, api_key, config.cache, refresh)

    if custom_polygon_4326 is not None:
        survey_geom = _synth_survey_geom(custom_polygon_4326, config.polygon.survey_offset_m)
    else:
        _cb(progress_cb, "geometry", "Computing survey polygon…", 40)
        survey_geom = process_survey(
            input_geoms, buildings, config.home_safety, config.polygon
        )
    pieces_count = len(survey_geom.pieces_3067)

    all_review_reasons: list[str] = list(survey_geom.review_reasons)

    # ------------------------------------------------------------------
    # 5. Elevation tiles (cache) + site DSM
    # ------------------------------------------------------------------
    _cb(progress_cb, "elevation", "Fetching elevation tiles…", 55)
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
    _cb(progress_cb, "zones", "Checking UAS restriction zones…", 70)
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
    _cb(progress_cb, "kmz", "Building KMZ route…", 80)
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
    _cb(progress_cb, "homes", "Writing homes KML…", 90)
    # Filter to buildings within include_buf of the closest point on the survey
    # polygon. include_buf defaults to 2× keep-out buffer so houses just outside
    # the keep-out zone still appear on the RC map.
    nearby = [
        b for b in buildings
        if survey_geom.survey_3067.distance(b.geometry) <= include_buf
    ]
    log.info(
        "%d of %d building(s) within %.0f m of survey polygon",
        len(nearby), len(buildings), include_buf,
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
    bldg_prov = tile_provenance(b_records)

    manifest = _build_manifest(
        job_name=job_name,
        run_ts=run_ts,
        dry_run=dry_run,
        config=config,
        parcel_ids_used=parcel_ids_used,
        parcel_fetch_ts=parcel_fetch_ts,
        property_ids_used=property_ids_used,
        property_display_ids_used=property_display_ids_used,
        property_fetch_ts=property_fetch_ts,
        survey_geom=survey_geom,
        pieces_count=pieces_count,
        drone_cfg=drone_cfg,
        flight_height_m=flight_height_m,
        kmz_results=kmz_results,
        dsm_stats=dsm_stats,
        dem_prov=dem_prov,
        buildings=buildings,
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
        parcels_4326 = [reproject_to_4326(p.geometry) for p in input_geoms]
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
    from shapely.ops import unary_union
    from shapely import make_valid

    _cb(progress_cb, "start", "Starting preview…", 0)
    api_key = _require_api_key()

    buf = config.home_safety.home_buffer_m
    include_buf = config.home_safety.resolved_include_buffer_m

    # 1. Fetch input geometries (parcels/properties are served from SQLite cache,
    #    so this is a fast local lookup on repeat previews).
    input_geoms = []
    if parcel_ids is not None or bbox_3067 is not None:
        _cb(progress_cb, "parcels", "Fetching parcels…", 10)
        log.info("Preview: fetching parcels …")
        parcels = fetch_parcels(
            parcel_ids=parcel_ids, bbox=bbox_3067, config=config.parcels,
            cache_config=config.cache,
        )
        if not parcels:
            raise ValueError("No parcels returned — check parcel IDs or bbox.")
        log.info("Preview: %d parcel(s)", len(parcels))
        input_geoms.extend(parcels)

    if property_ids is not None:
        _cb(progress_cb, "properties", "Fetching properties…", 20)
        log.info("Preview: fetching kiinteistöt …")
        props = fetch_properties(
            property_ids,
            api_key,
            timeout_s=config.properties.timeout_s,
            page_size=config.properties.page_size,
            cache_config=config.cache,
        )
        log.info("Preview: %d kiinteistö(t)", len(props))
        input_geoms.extend(props)

    if not input_geoms and custom_polygon_4326 is None:
        raise ValueError("No input geometries — provide parcel IDs, property IDs, or bbox.")

    # 2. Compute prelim_bounds for building fetch area.
    if custom_polygon_4326 is not None and not input_geoms:
        prelim_bounds = reproject_to_3067(custom_polygon_4326).bounds
    else:
        prelim_bounds = make_valid(unary_union([p.geometry for p in input_geoms])).bounds

    # 3. Fetch buildings (tiles served from SQLite cache — fast local I/O).
    buildings_bbox = (
        prelim_bounds[0] - include_buf,
        prelim_bounds[1] - include_buf,
        prelim_bounds[2] + include_buf,
        prelim_bounds[3] + include_buf,
    )
    _cb(progress_cb, "buildings", "Fetching building tiles…", 35)
    buildings, b_records = _load_buildings(buildings_bbox, api_key, config.cache, refresh)
    log.info("Preview: %d building(s)", len(buildings))

    # 3. Survey polygon
    _cb(progress_cb, "geometry", "Computing survey polygon…", 55)
    if custom_polygon_4326 is not None:
        log.info("Preview: using custom polygon (bridge/cut applied)")
        survey_geom = _synth_survey_geom(custom_polygon_4326, config.polygon.survey_offset_m)
    else:
        log.info("Preview: computing survey geometry …")
        survey_geom = process_survey(input_geoms, buildings, config.home_safety, config.polygon)

    # 4. Keep-out zone geometry for visualisation
    keepout_3067 = build_keepout(buildings, config.home_safety)
    keepout_4326 = reproject_to_4326(keepout_3067) if keepout_3067 else None

    # 5. Zone check
    drone_cfg = config.active_drone()
    flight_height_m = drone_cfg.height_from_gsd(config.flight.target_gsd_cm)
    # 5. DSM thumbnail — fetch DEM tiles (cached) and build a low-res display image
    dsm_b64: str | None = None
    dsm_bounds: tuple | None = None
    try:
        _cb(progress_cb, "elevation", "Fetching elevation tiles…", 65)
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

    _cb(progress_cb, "zones", "Checking UAS restriction zones…", 80)
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
    for b in buildings:
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

    result = {
        "survey": dict(mapping(survey_geom.survey_4326)),
        "original_areas": [dict(mapping(reproject_to_4326(p.geometry))) for p in input_geoms],
        "buildings": buildings_data,
        "keepout_zone": dict(mapping(keepout_4326)) if keepout_4326 else None,
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
            "has_parcels": any(hasattr(g, "parcel_id") for g in input_geoms),
            "has_properties": any(hasattr(g, "property_id") for g in input_geoms),
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


def create_skeleton_jobs(
    ids: list[str],
    id_type: str,
    output_dir: Path,
    folder: str | None,
    params: dict,
    progress_cb: Callable | None,
    config: AppConfig,
) -> list[dict]:
    """Fetch geometry for each ID and write a skeleton job_params.json.

    No KMZ, DSM, or manifest is written.  The polygon is stored as
    ``custom_polygon_4326`` so ``openJob()`` can display it immediately
    and a full export can skip the WFS re-fetch.

    Returns a list of per-ID result dicts with keys ``id``, ``status``
    (``"ok"`` | ``"skipped"`` | ``"error"``), and optional ``reason``.
    """
    from shapely.geometry import mapping as _mapping

    api_key = _require_api_key() if id_type == "properties" else ""

    target_dir = (output_dir / folder) if folder else output_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    if folder:
        marker = target_dir / ".dkk-folder"
        if not marker.exists():
            marker.write_text("", encoding="utf-8")

    results: list[dict] = []
    total = len(ids)

    for i, id_ in enumerate(ids):
        _cb(progress_cb, "batch", f"[{i + 1}/{total}] Fetching {id_}…", int(i / total * 90))

        job_dir = target_dir / id_
        if job_dir.exists():
            results.append({"id": id_, "status": "skipped", "reason": "already exists"})
            continue

        try:
            if id_type == "parcels":
                geoms = fetch_parcels(
                    parcel_ids=[id_], config=config.parcels, cache_config=config.cache
                )
                poly_3067 = geoms[0].geometry if geoms else None
            else:
                geoms = fetch_properties(
                    [id_], api_key,
                    timeout_s=config.properties.timeout_s,
                    page_size=config.properties.page_size,
                    cache_config=config.cache,
                )
                poly_3067 = geoms[0].geometry if geoms else None

            if poly_3067 is None:
                results.append({"id": id_, "status": "error", "reason": "no geometry returned"})
                continue

            poly_4326 = reproject_to_4326(poly_3067)
            geojson = dict(_mapping(poly_4326))

            job_dir.mkdir(parents=True, exist_ok=True)
            job_params = {
                "job_name": id_,
                "saved_at": None,
                "inputs": {
                    "parcel_ids": [id_] if id_type == "parcels" else [],
                    "property_ids": [id_] if id_type == "properties" else [],
                },
                "flight": {
                    "drone": params.get("drone"),
                    "height_m": params.get("height_m"),
                    "subcategory": params.get("subcategory", "A3"),
                },
                "polygon": {
                    "offset_m": params.get("offset_m", 0.0),
                    "simplify": params.get("simplify", "auto"),
                    "keepout": params.get("keepout", True),
                },
                "safety": {
                    "preview_radius_m": params.get("preview_radius_m"),
                },
                "custom_polygon_4326": geojson,
                "batch_created": True,
                "color": None,
                "last_preview_geojson": None,
            }
            (job_dir / "job_params.json").write_text(
                json.dumps(job_params, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            results.append({"id": id_, "status": "ok"})

        except Exception as exc:
            log.warning("Batch: failed to create skeleton for %s: %s", id_, exc)
            results.append({"id": id_, "status": "error", "reason": str(exc)})

    _cb(progress_cb, "batch", f"Done — {sum(r['status']=='ok' for r in results)}/{total} created", 95)
    return results


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
        needs_review=True,
        review_reasons=["Survey polygon was manually edited — verify boundaries before flying."],
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


def _build_manifest(
    *,
    job_name: str,
    run_ts: str,
    dry_run: bool,
    config: AppConfig,
    # inputs
    parcel_ids_used: list[str],
    parcel_fetch_ts: str,
    property_ids_used: list[str],
    property_display_ids_used: list[str],
    property_fetch_ts: str | None,
    # geometry / flight
    survey_geom: SurveyGeometry,
    pieces_count: int,
    drone_cfg: Any,
    flight_height_m: float,
    # pipeline outputs
    kmz_results: list,
    dsm_stats: dict,
    # buildings / safety
    buildings: list,
    nearby: list,
    include_buf: float,
    preview_radius_m: float,
    # provenance
    dem_prov: dict,
    bldg_prov: dict,
    # zones + review flags
    zone_result: Any,
    needs_review: bool,
    flight_ready: bool,
    all_review_reasons: list[str],
) -> dict:
    """Assemble and return the full provenance manifest dict for a completed job."""
    return {
        "tool_version":  tool_version(),
        "job_name":      job_name,
        "run_timestamp": run_ts,
        "dry_run":       dry_run,

        **(
            {
                "parcels": {
                    "parcel_ids":  parcel_ids_used,
                    "lpis_year":   config.parcels.lpis_year,
                    "fetched_at":  parcel_fetch_ts,
                    "attribution": _CC_BY["parcels"].format(date=parcel_fetch_ts[:10]),
                }
            }
            if parcel_ids_used else {}
        ),
        **(
            {
                "properties": {
                    "property_ids":         property_ids_used,
                    "property_display_ids": property_display_ids_used,
                    "fetched_at":           property_fetch_ts,
                    "attribution":          _CC_BY["properties"].format(
                        date=(property_fetch_ts or "")[:10]
                    ),
                }
            }
            if property_ids_used else {}
        ),

        "geometry": {
            "original_area_ha": round(survey_geom.original_area_ha, 4),
            "final_area_ha":    round(survey_geom.final_area_ha, 4),
            "area_lost_pct":    round(survey_geom.area_lost_pct, 2),
            "bbox_3067": {
                "xmin": survey_geom.bbox_3067[0],
                "ymin": survey_geom.bbox_3067[1],
                "xmax": survey_geom.bbox_3067[2],
                "ymax": survey_geom.bbox_3067[3],
            },
            "pieces_count":        pieces_count,
            "survey_vertex_count": survey_geom.survey_vertex_count,
        },

        "flight": {
            "target_gsd_cm":     config.flight.target_gsd_cm,
            "derived_height_m":  round(flight_height_m, 2),
            "overlap_front_pct": config.flight.overlap_front_pct,
            "overlap_side_pct":  config.flight.overlap_side_pct,
            "terrain_follow":    True,
            "drone":             drone_cfg.name,
            "drone_label":       drone_cfg.label,
        },

        "battery": (
            (
                {
                    "estimated_photo_count":     kmz_results[0].estimated_photo_count,
                    "estimated_flight_time_min": round(kmz_results[0].estimated_flight_time_min, 1),
                    "over_one_battery":          kmz_results[0].over_one_battery,
                }
                if len(kmz_results) == 1
                else {
                    "pieces": [
                        {
                            "piece":                     i + 1,
                            "estimated_photo_count":     r.estimated_photo_count,
                            "estimated_flight_time_min": round(r.estimated_flight_time_min, 1),
                            "over_one_battery":          r.over_one_battery,
                        }
                        for i, r in enumerate(kmz_results)
                    ],
                    "over_any_battery": any(r.over_one_battery for r in kmz_results),
                }
            )
            if kmz_results else {"note": "dry_run — not computed"}
        ),

        "dsm": (
            {
                "elevation_min_m":   round(dsm_stats["elevation_min_m"], 1),
                "elevation_max_m":   round(dsm_stats["elevation_max_m"], 1),
                "valid_pixel_count": dsm_stats["valid_pixel_count"],
                "attribution":       _CC_BY["elevation"].format(
                    date=dem_prov.get("fetch_date_min", "")[:10]
                ),
            }
            if dsm_stats else {"note": "dry_run — not built"}
        ),

        "home_safety": {
            "operating_subcategory":  config.home_safety.operating_subcategory,
            "home_buffer_m":          config.home_safety.home_buffer_m,
            "home_include_buffer_m":  round(include_buf, 1),
            "preview_radius_m":       round(preview_radius_m, 1),
            "offset_applied":         survey_geom.offset_applied,
            "min_dist_to_home_m":     (
                round(survey_geom.min_dist_to_home_m, 1)
                if survey_geom.min_dist_to_home_m is not None else None
            ),
            "buildings_fetched":      len(buildings),
            "buildings_in_homes_kml": len(nearby),
            "buildings_attribution":  _CC_BY["buildings"].format(
                date=bldg_prov.get("fetch_date_min", "")[:10]
            ),
        },

        "zones": {
            "checked":            zone_result.checked,
            "flight_ready":       zone_result.flight_ready,
            "attribution":        zone_result.attribution,
            "intersecting_zones": [
                {
                    "identifier":   h.identifier,
                    "name":         h.name,
                    "restriction":  h.restriction,
                    "reason":       h.reason,
                    "upper_limit":  h.altitude.upper_limit,
                    "upper_uom":    h.altitude.upper_uom,
                    "upper_ref":    h.altitude.upper_ref,
                    "ceiling_note": h.altitude.ceiling_note(flight_height_m),
                }
                for h in zone_result.intersecting_zones
            ],
        },

        "cache_provenance": {
            "dem":       dem_prov,
            "buildings": bldg_prov,
        },

        "needs_review":   needs_review,
        "flight_ready":   flight_ready,
        "review_reasons": all_review_reasons,
    }


def _require_api_key() -> str:
    key = os.environ.get("MML_API_KEY", "")
    if not key:
        raise RuntimeError(
            "MML_API_KEY environment variable is not set. "
            "Obtain a key at https://www.maanmittauslaitos.fi/rajapinnat/api-avaimen-ohje "
            "and set it in your .env file."
        )
    return key
