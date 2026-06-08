"""Config editor API: GET /api/settings, PATCH /api/settings, DELETE /api/settings/override.

Schema is generated dynamically from Pydantic model JSON schemas + curated _FIELD_META.
Adding a new field to a config model causes it to appear automatically in the editor
(with an auto-generated label); add an entry to _FIELD_META to give it a proper label,
unit, and description.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

import jobgen._server_state as _st

router = APIRouter()


# ---------------------------------------------------------------------------
# Curated field metadata — labels, units, descriptions, and type overrides.
# Keys are dotted paths: "section_id.field_name" or bare "field_name" for top-level.
# Any key present here overrides the auto-generated value for that attribute.
# ---------------------------------------------------------------------------

_FIELD_META: dict[str, dict[str, Any]] = {
    # ── Flight ────────────────────────────────────────────────────────────────
    "flight.target_gsd_cm": {
        "label": "Target GSD", "unit": "cm/px",
        "description": "Ground sampling distance target. Flight height is derived from this and the selected drone's camera specs.",
    },
    "flight.max_height_agl_m": {
        "label": "Max flight height", "unit": "m AGL",
        "description": "Hard ceiling on flight height. DJI Pilot 2 caps the route at this value.",
    },
    "flight.agl_safety_margin_m": {
        "label": "Terrain clearance margin", "unit": "m",
        "description": "Extra altitude above the DSM terrain model to protect against DEM errors and standing crops.",
    },
    "flight.takeoff_security_height_m": {
        "label": "Takeoff safety height", "unit": "m",
        "description": "Initial climb height before the drone begins moving toward the survey area.",
    },
    "flight.transitional_speed_ms": {
        "label": "Transit speed", "unit": "m/s",
        "description": "Speed when flying between the takeoff point and the survey polygon.",
    },
    "flight.overlap_front_pct": {
        "label": "Front overlap", "unit": "%",
        "description": "Forward overlap between consecutive photos along each strip.",
    },
    "flight.overlap_side_pct": {
        "label": "Side overlap", "unit": "%",
        "description": "Lateral overlap between adjacent flight strips.",
    },
    "flight.auto_flight_speed_ms": {
        "label": "Strip flight speed", "unit": "m/s",
        "description": "Speed along mapping strips. DJI Pilot 2 derives shutter timing from this — match to your sensor's max shutter speed at the target GSD.",
    },
    "flight.dsm_margin_m": {
        "label": "DSM margin", "unit": "m",
        "description": "Extra margin around the survey polygon in the embedded terrain model. Covers the RTH path and takeoff area.",
    },
    "flight.finish_action": {
        "label": "Finish action",
        "type": "enum",
        "options": ["goHome", "hover", "autoLand", "gotoFirstWaypoint", "noAction"],
        "description": "What the drone does after completing all waypoints.",
    },
    "flight.rc_lost_action": {
        "label": "RC lost action",
        "type": "enum",
        "options": ["goBack", "hover", "autoLand"],
        "description": "What the drone does if the remote controller link is lost during the mission.",
    },
    # ── Safety ────────────────────────────────────────────────────────────────
    "home_safety.operating_subcategory": {
        "label": "Operating subcategory",
        "description": "EU UAS Regulation category. A2: keep-out = flight height from residential buildings. A3: fixed 150 m from populated areas.",
    },
    "home_safety.home_buffer_m": {
        "label": "Keep-out buffer", "unit": "m",
        "description": "Distance the survey polygon is contracted away from residential buildings. Auto-derived from flight height in A2 mode.",
    },
    "home_safety.home_include_buffer_m": {
        "label": "Buildings inclusion buffer", "unit": "m",
        "description": "Buildings within this distance of the polygon boundary are included in the Homes KML. Leave empty to use 2× keep-out buffer.",
    },
    "home_safety.preview_radius_m": {
        "label": "Preview radius", "unit": "m",
        "description": "Radius of the informational circle drawn around each building in the map preview. Leave empty for 3× flight height.",
    },
    "home_safety.vlos_range_m": {
        "label": "VLOS range", "unit": "m",
        "description": "Visual line-of-sight range shown as a dashed circle around the takeoff point in the UI.",
    },
    "home_safety.offset_enabled": {
        "label": "Enable keep-out buffer",
        "description": "When disabled, the survey polygon is not contracted away from buildings.",
    },
    "home_safety.max_area_loss_pct": {
        "label": "Max area loss threshold", "unit": "%",
        "description": "If the keep-out buffer removes more than this percentage of the survey area, the job is flagged for review.",
    },
    # ── Polygon ───────────────────────────────────────────────────────────────
    "polygon.edge_buffer_m": {
        "label": "Edge buffer", "unit": "m",
        "description": "Inward contraction of the survey polygon boundary. Use to leave a margin inside the cadastral line.",
    },
    "polygon.gap_fill_m": {
        "label": "Gap fill distance", "unit": "m",
        "description": "Morphological closing distance to bridge gaps between adjacent parcels. Gaps up to 2× this width are merged.",
    },
    "polygon.survey_offset_m": {
        "label": "Survey offset", "unit": "m",
        "description": "Push (+) or pull (–) the survey boundary relative to the parcel/cadastral line. Applied after gap-fill.",
    },
    "polygon.simplify_mode": {
        "label": "Simplify mode",
        "description": "fixed: apply a fixed Douglas-Peucker tolerance. auto: find the largest tolerance that keeps vertex count ≤ max vertices.",
    },
    "polygon.simplify_tolerance_m": {
        "label": "Simplify tolerance", "unit": "m",
        "description": "Douglas-Peucker tolerance used in fixed simplify mode.",
    },
    "polygon.auto_simplify_max_vertices": {
        "label": "Max vertices (auto mode)",
        "description": "Hard cap on vertex count when using auto simplify mode.",
    },
    "polygon.multipart_policy": {
        "label": "Multipart policy",
        "description": "What to do when the survey polygon breaks into multiple parts. split: save each separately. largest: keep the biggest piece. review: flag for manual check.",
    },
    "polygon.hole_policy": {
        "label": "Hole policy",
        "description": "What to do with holes in the survey polygon (e.g. from building subtraction). fill: remove the hole. clip: keep it. review: flag.",
    },
    # ── Zones ─────────────────────────────────────────────────────────────────
    "zones.max_age_days": {
        "label": "Zone cache TTL", "unit": "days",
        "description": "Re-fetch the Traficom UAS zone dump if the cached copy is older than this many days.",
    },
    "zones.check_buffer_m": {
        "label": "Zone check buffer", "unit": "m",
        "description": "Expand the survey polygon by this distance before checking for UAS zone intersections, so nearby zones are also reported.",
    },
    "zones.zones_file": {
        "label": "Local zones file override",
        "description": "Path to a local zones file. If set, the Traficom API is not called. Leave empty to use the live API.",
    },
    # ── Cache ─────────────────────────────────────────────────────────────────
    "cache.dem_ttl_days": {
        "label": "DEM cache TTL", "unit": "days",
        "description": "How long to keep downloaded elevation tiles before re-fetching from MML.",
    },
    "cache.buildings_ttl_days": {
        "label": "Buildings cache TTL", "unit": "days",
        "description": "How long to keep downloaded building tiles before re-fetching from MML Maastotietokanta.",
    },
    "cache.parcels_ttl_days": {
        "label": "Parcels cache TTL", "unit": "days",
        "description": "How long to keep cached parcel geometry before re-fetching from Ruokavirasto.",
    },
    "cache.properties_ttl_days": {
        "label": "Properties cache TTL", "unit": "days",
        "description": "How long to keep cached property geometry before re-fetching from MML.",
    },
    "cache.offline": {
        "label": "Offline mode",
        "description": "When enabled, only cached data is used — no network requests are made. Useful for areas already fully cached.",
    },
    # ── Output ────────────────────────────────────────────────────────────────
    "output.output_dir": {
        "label": "Output directory",
        "description": "Where jobs are saved on disk. Relative paths are resolved from the working directory.",
    },
    # ── Parcels ───────────────────────────────────────────────────────────────
    "parcels.lpis_year": {
        "label": "LPIS layer year",
        "description": "Agricultural year for the Ruokavirasto parcel layer. Defaults to the previous calendar year (the most recently published season).",
    },
    "parcels.page_size": {
        "label": "WFS page size",
        "description": "Number of features per WFS request page to Ruokavirasto (server cap: 1000).",
    },
    "parcels.timeout_s": {
        "label": "Request timeout", "unit": "s",
        "description": "HTTP timeout for Ruokavirasto WFS requests.",
    },
    # ── Properties ────────────────────────────────────────────────────────────
    "properties.page_size": {
        "label": "OGC API page size",
        "description": "Number of features per OGC API Features page for MML property (kiinteistö) requests.",
    },
    "properties.timeout_s": {
        "label": "Request timeout", "unit": "s",
        "description": "HTTP timeout for MML property (kiinteistö) requests.",
    },
    # ── Power Lines ───────────────────────────────────────────────────────────
    "powerlines.enabled": {
        "label": "Enable power line keep-out",
        "description": "Fetch overhead high-voltage lines from MML Maastotietokanta and show them on the map. Overhead lines are kept out of the survey area by the safe distance below.",
    },
    "powerlines.overhead_buffer_m": {
        "label": "Power line safe distance", "unit": "m",
        "description": "Keep-out buffer around each overhead high-voltage line (110 kV+). The survey polygon is contracted away from lines by this distance. Finnish aviation guidance recommends staying well clear of high-voltage lines.",
    },
    # ── Top-level ─────────────────────────────────────────────────────────────
    "default_drone": {
        "label": "Default drone",
        "description": "The drone profile used for new jobs when not overridden per-job in the UI.",
    },
}

# Ordered sections — (section_id, label)
_SECTIONS_DEF: list[tuple[str, str]] = [
    ("flight",      "Flight"),
    ("home_safety", "Safety"),
    ("polygon",     "Polygon"),
    ("powerlines",  "Power Lines"),
    ("zones",       "UAS Zones"),
    ("cache",       "Cache"),
    ("output",      "Output"),
    ("parcels",     "Parcels"),
    ("properties",  "Properties"),
]

# Fields to hide per section (internal / dangerous to edit via UI)
_SKIP_FIELDS: dict[str, set[str]] = {
    "home_safety": {"residential_kohdeluokka", "a3_additional_kohdeluokka"},
    "zones":       {"api_url"},
    "cache":       {"tile_size_m", "cache_dir"},
}


def _extract_type_info(fschema: dict) -> dict:
    """Convert a Pydantic JSON-schema field dict into UI type metadata."""
    result: dict[str, Any] = {"nullable": False}
    schema = fschema

    # Unwrap anyOf (Optional[X] → [X, null])
    if "anyOf" in fschema:
        non_null = [x for x in fschema["anyOf"] if x.get("type") != "null"]
        result["nullable"] = True
        schema = non_null[0] if non_null else {}

    # Literal / enum values (may have no explicit "type" in Pydantic 2)
    if "enum" in schema:
        result["type"] = "enum"
        result["options"] = schema["enum"]
        return result

    t = schema.get("type", "string")
    if t == "number":
        result["type"] = "number"
        result["step"] = 0.1
    elif t == "integer":
        result["type"] = "integer"
        result["step"] = 1
    elif t == "boolean":
        result["type"] = "boolean"
    elif t == "array":
        result["type"] = "array"  # caller skips these
    else:
        result["type"] = "string"

    # Numeric constraints
    for src, dst in [
        ("minimum",          "min"),
        ("exclusiveMinimum", "min"),
        ("maximum",          "max"),
        ("exclusiveMaximum", "max"),
    ]:
        if src in schema:
            result[dst] = schema[src]

    return result


def _build_sections(config: Any) -> list[dict]:
    from jobgen.config import (
        CacheConfig, FlightConfig, HomeSafetyConfig, OutputConfig,
        ParcelsConfig, PolygonConfig, PowerLinesConfig, PropertiesConfig, ZonesConfig,
    )
    model_map: dict[str, type] = {
        "flight":      FlightConfig,
        "home_safety": HomeSafetyConfig,
        "polygon":     PolygonConfig,
        "powerlines":  PowerLinesConfig,
        "zones":       ZonesConfig,
        "cache":       CacheConfig,
        "output":      OutputConfig,
        "parcels":     ParcelsConfig,
        "properties":  PropertiesConfig,
    }

    # ── Drone section (top-level field + enum from loaded drone list) ──────────
    sections: list[dict] = [{
        "id":    "drone",
        "label": "Drone",
        "fields": [{
            "key":          "default_drone",
            "label":        _FIELD_META["default_drone"]["label"],
            "description":  _FIELD_META["default_drone"]["description"],
            "unit":         "",
            "type":         "enum",
            "options":      [d.name for d in config.drones],
            "option_labels": {d.name: d.label for d in config.drones},
            "nullable":     False,
            "value":        config.default_drone,
        }],
    }]

    for section_id, section_label in _SECTIONS_DEF:
        model_cls = model_map[section_id]
        json_schema = model_cls.model_json_schema()
        skip = _SKIP_FIELDS.get(section_id, set())
        section_obj = getattr(config, section_id)
        fields: list[dict] = []

        for fname, fschema in json_schema.get("properties", {}).items():
            if fname in skip:
                continue
            key = f"{section_id}.{fname}"
            meta = _FIELD_META.get(key, {})
            type_info = _extract_type_info(fschema)

            # _FIELD_META can override any type attribute
            for attr in ("type", "options", "option_labels", "nullable", "step", "min", "max"):
                if attr in meta:
                    type_info[attr] = meta[attr]

            if type_info.get("type") == "array":
                continue  # skip complex list fields for now

            fields.append({
                "key":         key,
                "label":       meta.get("label", fname.replace("_", " ").title()),
                "description": meta.get("description", fschema.get("description", "")),
                "unit":        meta.get("unit", ""),
                **type_info,
                "value": getattr(section_obj, fname),
            })

        sections.append({"id": section_id, "label": section_label, "fields": fields})

    return sections


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/api/settings")
async def get_settings():
    """Return all editable settings as grouped sections with current values."""
    return {"sections": _build_sections(_st.config)}


@router.patch("/api/settings")
async def update_settings(changes: dict):
    """Apply a flat {dotted.key: value} dict, validate via Pydantic, hot-reload, and persist."""
    from jobgen.config import AppConfig

    raw = _st.config.model_dump(mode="python")

    for key, value in changes.items():
        parts = key.split(".", 1)
        if len(parts) == 1:
            raw[parts[0]] = value
        else:
            section, field = parts
            if section not in raw or not isinstance(raw.get(section), dict):
                raise HTTPException(400, detail=f"Unknown config section: {section!r}")
            raw[section][field] = value

    try:
        new_config = AppConfig.model_validate(raw)
    except Exception as exc:
        raise HTTPException(422, detail=str(exc))

    _st.config = new_config

    if _st.config_path:
        from jobgen.config import save_config
        import logging
        try:
            save_config(new_config, Path(_st.config_path))
        except Exception as exc:
            logging.getLogger(__name__).warning("Failed to write config.toml: %s", exc)

    return {"ok": True}
