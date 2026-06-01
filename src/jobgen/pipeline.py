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

from jobgen import tool_version
from jobgen.buildings import Building, dedup_buildings, load_tile, tile_fetcher as buildings_fetcher
from jobgen.cache import FetcherFn, TileRecord, get_tiles, tile_provenance
from jobgen.config import AppConfig
from jobgen.elevation import tile_fetcher as dem_fetcher, validate_tile
from jobgen.geometry import process_survey, reproject_to_4326
from jobgen.logging_setup import setup_logging
from jobgen.parcels import fetch_parcels
from jobgen.properties import fetch_properties
from jobgen.raster import build_site_dsm
from jobgen.preview import build_map_preview
from jobgen.wpml import build_homes_kml, build_kmz
from jobgen.zones import check_zones

log = logging.getLogger(__name__)

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
        log.info("Fetching parcels …")
        parcels = fetch_parcels(
            parcel_ids=parcel_ids,
            bbox=bbox_3067,
            config=config.parcels,
        )
        if not parcels:
            raise ValueError("No parcels returned — check parcel IDs or bbox.")
        parcel_ids_used = [p.parcel_id for p in parcels]
        log.info("%d parcel(s) fetched: %s", len(parcels), parcel_ids_used)
        input_geoms.extend(parcels)

    if property_ids is not None:
        property_fetch_ts = datetime.now(timezone.utc).isoformat()
        log.info("Fetching kiinteistöt …")
        props = fetch_properties(
            property_ids,
            api_key,
            timeout_s=config.properties.timeout_s,
            page_size=config.properties.page_size,
        )
        property_ids_used = [p.property_id for p in props]
        property_display_ids_used = [p.display_id for p in props]
        log.info(
            "%d kiinteistö(t) fetched: %s",
            len(props), property_display_ids_used,
        )
        input_geoms.extend(props)

    if not input_geoms:
        raise ValueError("No input geometries — provide --parcels, --properties, or --bbox.")

    # ------------------------------------------------------------------
    # 2. Geometry — merge, gap-fill, edge buffer, keep-out, policy, reproject
    # ------------------------------------------------------------------
    log.info("Processing survey geometry …")

    # First we need buildings, so compute the survey bbox to know which tiles to fetch.
    # Use a preliminary merge bbox + buffer to warm buildings cache.
    from shapely.ops import unary_union
    from shapely import make_valid
    prelim = make_valid(unary_union([p.geometry for p in input_geoms]))
    prelim_bounds = prelim.bounds  # xmin, ymin, xmax, ymax in EPSG:3067

    buf = config.home_safety.home_buffer_m
    include_buf = (
        config.home_safety.home_include_buffer_m
        if config.home_safety.home_include_buffer_m is not None
        else 2.0 * buf
    )
    # Preview yellow circle radius — resolved lazily below once flight_height_m is known
    _preview_radius_cfg = config.home_safety.preview_radius_m
    buildings_bbox = (
        prelim_bounds[0] - include_buf,
        prelim_bounds[1] - include_buf,
        prelim_bounds[2] + include_buf,
        prelim_bounds[3] + include_buf,
    )

    # ------------------------------------------------------------------
    # 3. Buildings (cache)
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # 4. Full geometry pipeline (with buildings)
    # ------------------------------------------------------------------
    pieces_count = 1
    survey_geom = process_survey(
        input_geoms, buildings, config.home_safety, config.polygon
    )
    pieces_count = len(survey_geom.pieces_3067)

    all_review_reasons: list[str] = list(survey_geom.review_reasons)

    # ------------------------------------------------------------------
    # 5. Elevation tiles (cache) + site DSM
    # ------------------------------------------------------------------
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
    return manifest


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
