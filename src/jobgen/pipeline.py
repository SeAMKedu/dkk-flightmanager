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
    SurveyGeometry, build_keepout, process_survey,
    reproject_to_4326, reproject_to_3067, vertex_count,
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

    if custom_polygon_4326 is not None:
        # User-edited polygon bypasses process_survey(); we still need buildings for
        # homes KML. Use the custom polygon's bounds to determine tile bbox.
        survey_3067 = reproject_to_3067(custom_polygon_4326)
        prelim_bounds = survey_3067.bounds
    else:
        log.info("Processing survey geometry …")
        prelim = make_valid(unary_union([p.geometry for p in input_geoms]))
        prelim_bounds = prelim.bounds

    buf = config.home_safety.home_buffer_m
    include_buf = (
        config.home_safety.home_include_buffer_m
        if config.home_safety.home_include_buffer_m is not None
        else 2.0 * buf
    )
    _preview_radius_cfg = config.home_safety.preview_radius_m
    buildings_bbox = (
        prelim_bounds[0] - include_buf,
        prelim_bounds[1] - include_buf,
        prelim_bounds[2] + include_buf,
        prelim_bounds[3] + include_buf,
    )

    _cb(progress_cb, "buildings", "Fetching building tiles…", 25)
    log.info("Fetching building tiles …")
    b_fetcher = buildings_fetcher(api_key)
    b_records = get_tiles(
        "buildings", buildings_bbox, b_fetcher, config.cache, refresh=refresh
    )

    raw_buildings: list[Building] = []
    for rec in b_records:
        raw_buildings.extend(load_tile(rec.path))
    buildings = dedup_buildings(raw_buildings)
    log.info("%d building(s) loaded after dedup", len(buildings))

    pieces_count = 1
    if custom_polygon_4326 is not None:
        # Synthesise a SurveyGeometry from the user-supplied polygon.
        _area_ha = survey_3067.area / 10_000
        _vc = vertex_count(custom_polygon_4326)
        survey_geom = SurveyGeometry(
            survey_3067=survey_3067,
            survey_4326=custom_polygon_4326,
            pieces_3067=[survey_3067],
            pieces_4326=[custom_polygon_4326],
            bbox_3067=(prelim_bounds[0], prelim_bounds[1], prelim_bounds[2], prelim_bounds[3]),
            original_area_ha=_area_ha,
            final_area_ha=_area_ha,
            area_lost_pct=0.0,
            min_dist_to_home_m=None,
            offset_applied=False,
            survey_vertex_count=_vc,
            needs_review=True,
            review_reasons=["Survey polygon was manually edited — verify boundaries before flying."],
        )
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

    manifest = {
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
            "pieces_count":         pieces_count,
            "survey_vertex_count":  survey_geom.survey_vertex_count,
        },

        "flight": {
            "target_gsd_cm":       config.flight.target_gsd_cm,
            "derived_height_m":    round(flight_height_m, 2),
            "overlap_front_pct":   config.flight.overlap_front_pct,
            "overlap_side_pct":    config.flight.overlap_side_pct,
            "terrain_follow":      True,
            "drone":               drone_cfg.name,
            "drone_label":         drone_cfg.label,
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
                "elevation_min_m":  round(dsm_stats["elevation_min_m"], 1),
                "elevation_max_m":  round(dsm_stats["elevation_max_m"], 1),
                "valid_pixel_count": dsm_stats["valid_pixel_count"],
                "attribution":      _CC_BY["elevation"].format(
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
            "checked":           zone_result.checked,
            "flight_ready":      zone_result.flight_ready,
            "intersecting_zones": [
                {
                    "identifier":  h.identifier,
                    "name":        h.name,
                    "restriction": h.restriction,
                    "reason":      h.reason,
                    "upper_limit": h.altitude.upper_limit,
                    "upper_uom":   h.altitude.upper_uom,
                    "upper_ref":   h.altitude.upper_ref,
                    "ceiling_note": h.altitude.ceiling_note(flight_height_m),
                }
                for h in zone_result.intersecting_zones
            ],
        },

        "cache_provenance": {
            "dem":       dem_prov,
            "buildings": bldg_prov,
        },

        "needs_review":  needs_review,
        "flight_ready":  flight_ready,
        "review_reasons": all_review_reasons,
    }

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


class PreviewCache:
    """Cached result of the expensive fetch stages (parcels + buildings).

    Keyed by the sorted tuple of parcel/property IDs and the fetch bbox
    expansion used for buildings. When the same IDs are re-submitted with
    only flight-parameter changes (subcategory, height, simplify …) these
    stages are skipped and the cached geometries are reused directly.
    """

    def __init__(
        self,
        input_geoms: list,
        buildings: list,
        prelim_bounds: tuple,
        include_buf: float,
        parcel_key: tuple,
        property_key: tuple,
    ) -> None:
        self.input_geoms = input_geoms
        self.buildings = buildings
        self.prelim_bounds = prelim_bounds
        self.include_buf = include_buf
        self.parcel_key = parcel_key
        self.property_key = property_key

    def covers(
        self,
        parcel_ids: list[str] | None,
        property_ids: list[str] | None,
        include_buf: float,
    ) -> bool:
        pk = tuple(sorted(parcel_ids or []))
        ppk = tuple(sorted(property_ids or []))
        # Accept cache when the requested include_buf is ≤ what was fetched —
        # the larger fetch area is a superset.
        return pk == self.parcel_key and ppk == self.property_key and include_buf <= self.include_buf


def run_preview(
    config: AppConfig,
    *,
    parcel_ids: list[str] | None = None,
    property_ids: list[str] | None = None,
    bbox_3067: tuple[float, float, float, float] | None = None,
    refresh: bool = False,
    progress_cb: Callable[[str, str, int], None] | None = None,
    _cache: PreviewCache | None = None,
) -> tuple[dict, PreviewCache]:
    """Run parcels → buildings → geometry → zones; return (GeoJSON dict, cache).

    No files are written. DEM and DSM stages are skipped to keep latency low.
    At least one of *parcel_ids*, *property_ids*, or *bbox_3067* must be provided.

    Pass a :class:`PreviewCache` from a previous call as *_cache* to skip the
    parcel and building-tile fetch stages when only flight parameters changed.
    """
    from shapely.geometry import mapping
    from shapely.ops import unary_union
    from shapely import make_valid

    _cb(progress_cb, "start", "Starting preview…", 0)
    api_key = _require_api_key()

    buf = config.home_safety.home_buffer_m
    include_buf = (
        config.home_safety.home_include_buffer_m
        if config.home_safety.home_include_buffer_m is not None
        else 2.0 * buf
    )

    # 1. Fetch input geometries (or reuse cache)
    if _cache is not None and _cache.covers(parcel_ids, property_ids, include_buf):
        log.info("Preview: reusing cached parcels + buildings")
        input_geoms = _cache.input_geoms
        buildings = _cache.buildings
        prelim_bounds = _cache.prelim_bounds
        new_cache = _cache
    else:
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

        if not input_geoms:
            raise ValueError("No input geometries — provide parcel IDs, property IDs, or bbox.")

        prelim = make_valid(unary_union([p.geometry for p in input_geoms]))
        prelim_bounds = prelim.bounds

        buildings_bbox = (
            prelim_bounds[0] - include_buf,
            prelim_bounds[1] - include_buf,
            prelim_bounds[2] + include_buf,
            prelim_bounds[3] + include_buf,
        )

        _cb(progress_cb, "buildings", "Fetching building tiles…", 35)
        log.info("Preview: fetching building tiles …")
        b_fetcher = buildings_fetcher(api_key)
        b_records = get_tiles(
            "buildings", buildings_bbox, b_fetcher, config.cache, refresh=refresh
        )
        raw_buildings: list[Building] = []
        for rec in b_records:
            raw_buildings.extend(load_tile(rec.path))
        buildings = dedup_buildings(raw_buildings)
        log.info("Preview: %d building(s)", len(buildings))

        new_cache = PreviewCache(
            input_geoms=input_geoms,
            buildings=buildings,
            prelim_bounds=prelim_bounds,
            include_buf=include_buf,
            parcel_key=tuple(sorted(parcel_ids or [])),
            property_key=tuple(sorted(property_ids or [])),
        )

    # 3. Survey polygon
    _cb(progress_cb, "geometry", "Computing survey polygon…", 55)
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

    # Zone geometries for map display
    zone_hits_data = []
    for h in zone_result.intersecting_zones:
        geojson = _zone_geojson(h)
        zone_hits_data.append({
            "geojson": geojson,
            "identifier": h.identifier,
            "name": h.name,
            "restriction": h.restriction,
        })

    all_review_reasons = list(survey_geom.review_reasons) + list(zone_result.reasons)
    needs_review = bool(all_review_reasons) or not zone_result.checked
    flight_ready = zone_result.flight_ready and not needs_review

    _cb(progress_cb, "complete", "Preview complete", 100)

    result = {
        "survey": dict(mapping(survey_geom.survey_4326)),
        "original_areas": [dict(mapping(reproject_to_4326(p.geometry))) for p in input_geoms],
        "buildings": buildings_data,
        "keepout_zone": dict(mapping(keepout_4326)) if keepout_4326 else None,
        "zone_hits": zone_hits_data,
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
            "home_buffer_m": buf,
        },
    }
    return result, new_cache


def _zone_geojson(hit: ZoneHit) -> dict | None:
    try:
        return hit.properties["geometry"][0].get("horizontalProjection")
    except (KeyError, IndexError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_api_key() -> str:
    key = os.environ.get("MML_API_KEY", "")
    if not key:
        raise RuntimeError(
            "MML_API_KEY environment variable is not set. "
            "Obtain a key at https://www.maanmittauslaitos.fi/rajapinnat/api-avaimen-ohje "
            "and set it in your .env file."
        )
    return key
