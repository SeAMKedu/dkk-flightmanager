"""Manifest dict construction for completed mapping jobs.

``build_manifest()`` assembles the full provenance record written to
``manifest.json``.  Kept separate from the pipeline so the manifest
schema lives in one focused place, independently readable.
"""

from __future__ import annotations

from typing import Any

from flightmanager import tool_version
from flightmanager.config import AppConfig
from flightmanager.geometry import SurveyGeometry

# CC-BY attribution templates keyed by dataset.
# The ``{date}`` placeholder is filled with the tile's fetch date (YYYY-MM-DD).
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


def build_manifest(
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
    strip_speed_ms: float | None = None,
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
            "strip_speed_ms":    round(strip_speed_ms, 2) if strip_speed_ms is not None else None,
            "overlap_front_pct": config.flight.overlap_front_pct,
            "overlap_side_pct":  config.flight.overlap_side_pct,
            "terrain_follow":    not config.flight.advanced_mode,
            "waypoint_mode":     config.flight.advanced_mode,
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
