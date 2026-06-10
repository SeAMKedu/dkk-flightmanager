"""Filesystem helpers for job persistence.

Read/write job_params.json, thumbnail.svg, and scan the output directory tree.
No FastAPI or server-level globals — all functions are pure I/O, fully testable
in isolation.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Thumbnail
# ---------------------------------------------------------------------------


def make_thumbnail_svg(survey_geojson: dict | None) -> str | None:
    """Return a 64×64 SVG string from a GeoJSON Polygon/MultiPolygon geometry."""
    if survey_geojson is None:
        return None
    try:
        geom_type = survey_geojson.get("type", "")
        if geom_type == "Polygon":
            rings = [survey_geojson["coordinates"][0]]
        elif geom_type == "MultiPolygon":
            rings = [poly[0] for poly in survey_geojson["coordinates"]]
        else:
            return None

        all_lons = [c[0] for ring in rings for c in ring]
        all_lats = [c[1] for ring in rings for c in ring]
        lon_min, lon_max = min(all_lons), max(all_lons)
        lat_min, lat_max = min(all_lats), max(all_lats)
        lon_span = lon_max - lon_min or 1e-9
        lat_span = lat_max - lat_min or 1e-9
        size = 64
        pad = 4

        def to_svg(lon: float, lat: float) -> tuple[float, float]:
            x = pad + (lon - lon_min) / lon_span * (size - 2 * pad)
            y = pad + (1.0 - (lat - lat_min) / lat_span) * (size - 2 * pad)
            return x, y

        paths = []
        for ring in rings:
            pts = " ".join(f"{x:.1f},{y:.1f}" for c in ring for x, y in (to_svg(c[0], c[1]),))
            paths.append(
                f'<polygon points="{pts}" fill="#3b82f6" fill-opacity="0.7"'
                f' stroke="#1d4ed8" stroke-width="1"/>'
            )

        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}"'
            f' width="{size}" height="{size}">'
            f'<rect width="{size}" height="{size}" fill="#f8fafc"/>'
            + "".join(paths)
            + "</svg>"
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Directory introspection
# ---------------------------------------------------------------------------


def is_job_dir(d: Path) -> bool:
    """True if *d* contains job marker files (job_params.json or manifest.json)."""
    return (d / "job_params.json").exists() or (d / "manifest.json").exists()


def is_folder_dir(d: Path) -> bool:
    """True if *d* is a group folder (.dkk-folder marker or contains job subdirs)."""
    if (d / ".dkk-folder").exists():
        return True
    try:
        return any(sub.is_dir() and is_job_dir(sub) for sub in d.iterdir() if sub.is_dir())
    except PermissionError:
        return False


def resolve_job_dir(output_dir: Path, path: str) -> tuple[str | None, str, Path]:
    """Split *path* (``name`` or ``folder/name``) into ``(folder, name, directory)``."""
    parts = path.strip("/").split("/", 1)
    if len(parts) == 2:
        folder, name = parts
        return folder, name, output_dir / folder / name
    return None, parts[0], output_dir / parts[0]


def best_polygon(job_dir: Path) -> dict | None:
    """Return the best available GeoJSON polygon for a job directory."""
    params_path = job_dir / "job_params.json"
    if params_path.exists():
        try:
            params = json.loads(params_path.read_text(encoding="utf-8"))
            geom = params.get("custom_polygon_4326")
            if geom:
                return geom
            preview = params.get("last_preview_geojson") or {}
            geom = preview.get("survey")
            if geom:
                return geom
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# Job card / list scanning
# ---------------------------------------------------------------------------


def read_job_card(job_dir: Path, folder: str | None = None) -> dict:
    """Build a summary card dict for one job directory."""
    name = job_dir.name
    path = f"{folder}/{name}" if folder else name
    manifest_path = job_dir / "manifest.json"
    params_path = job_dir / "job_params.json"
    thumb_path = job_dir / "thumbnail.svg"

    if not manifest_path.exists() and not params_path.exists():
        return {
            "name": name,
            "folder": folder,
            "path": path,
            "status": "failed",
            "saved_at": None,
            "modified_at": datetime.fromtimestamp(
                job_dir.stat().st_mtime, tz=timezone.utc
            ).isoformat(),
            "untouched": False,
            "color": None,
        }

    manifest: dict = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    params: dict = {}
    if params_path.exists():
        try:
            params = json.loads(params_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    thumbnail_svg = None
    if thumb_path.exists():
        try:
            thumbnail_svg = thumb_path.read_text(encoding="utf-8")
        except Exception:
            pass

    has_kmz = any(job_dir.glob("*.kmz"))
    batch_created = params.get("batch_created", False)
    untouched = bool(batch_created and not has_kmz)

    g = manifest.get("geometry", {})
    f = manifest.get("flight", {})
    bat = manifest.get("battery") or {}
    if "estimated_flight_time_min" in bat:
        flight_time_min: float | None = bat["estimated_flight_time_min"]
        photo_count: int | None = bat.get("estimated_photo_count")
        over_one_battery: bool = bat.get("over_one_battery", False)
    elif "pieces" in bat:
        flight_time_min = sum(p.get("estimated_flight_time_min", 0) for p in bat["pieces"])
        photo_count = sum(p.get("estimated_photo_count", 0) for p in bat["pieces"])
        over_one_battery = bat.get("over_any_battery", False)
    else:
        flight_time_min = None
        photo_count = None
        over_one_battery = False

    return {
        "name": name,
        "folder": folder,
        "path": path,
        "status": "ok",
        "saved_at": params.get("saved_at"),
        "run_at": manifest.get("run_timestamp"),
        "area_ha": g.get("final_area_ha"),
        "area_lost_pct": g.get("area_lost_pct"),
        "vertex_count": g.get("survey_vertex_count"),
        "drone": f.get("drone"),
        "drone_label": f.get("drone_label"),
        "height_m": f.get("derived_height_m"),
        "strip_speed_ms": f.get("strip_speed_ms"),
        "flight_time_min": flight_time_min,
        "photo_count": photo_count,
        "over_one_battery": over_one_battery,
        "flight_ready": manifest.get("flight_ready"),
        "needs_review": manifest.get("needs_review"),
        "untouched": untouched,
        "color": params.get("color"),
        "thumbnail_svg": thumbnail_svg,
        "sort_order": params.get("sort_order"),
        "takeoff_point_4326": params.get("takeoff_point_4326"),
        "skipped": params.get("skipped", False),
    }


def _tier_sort_key(j: dict) -> tuple:
    """Four-tier sort key for job lists.

    Tier 0 — ready, explicit sort_order (numbered, drag-ordered)
    Tier 1 — ready, no sort_order (has takeoff point, awaiting ordering)
    Tier 2 — exported (has run_at or flight_ready field), no takeoff point
    Tier 3 — untouched batch skeletons
    Within each tier: saved_at / modified_at ascending (oldest first keeps list stable).
    """
    has_takeoff = j.get("takeoff_point_4326") is not None
    so = j.get("sort_order")
    untouched = j.get("untouched", False)
    ts = j.get("saved_at") or j.get("run_at") or j.get("modified_at") or ""

    if has_takeoff and so is not None:
        return (0, so, ts)
    if has_takeoff:
        return (1, 0, ts)
    if not untouched:
        return (2, 0, ts)
    return (3, 0, ts)


def scan_jobs(output_dir: Path) -> list[dict]:
    """Scan *output_dir*; return groups ``[{name, jobs}]`` with one-level folder support."""
    if not output_dir.is_dir():
        return []

    root_jobs: list[dict] = []
    folder_groups: list[dict] = []

    for entry in sorted(output_dir.iterdir()):
        if not entry.is_dir():
            continue
        if is_folder_dir(entry):
            folder_jobs: list[dict] = []
            try:
                for sub in sorted(entry.iterdir()):
                    if sub.is_dir():
                        folder_jobs.append(read_job_card(sub, folder=entry.name))
            except PermissionError:
                pass
            folder_jobs.sort(key=_tier_sort_key)
            folder_groups.append({"name": entry.name, "jobs": folder_jobs})
        else:
            root_jobs.append(read_job_card(entry, folder=None))

    root_jobs.sort(key=_tier_sort_key)

    groups: list[dict] = []
    if root_jobs:
        groups.append({"name": None, "jobs": root_jobs})
    groups.extend(folder_groups)
    return groups


# ---------------------------------------------------------------------------
# Manifest / params reconstruction
# ---------------------------------------------------------------------------


def params_from_manifest(name: str, manifest: dict) -> dict:
    """Reconstruct best-effort job_params from a manifest (CLI-created jobs)."""
    parcels = manifest.get("parcels", {})
    props = manifest.get("properties", {})
    flight = manifest.get("flight", {})
    safety = manifest.get("home_safety", {})
    return {
        "job_name": name,
        "saved_at": None,
        "inputs": {
            "parcel_ids": parcels.get("parcel_ids", []),
            "property_ids": props.get("property_ids", []),
        },
        "flight": {
            "drone": flight.get("drone"),
            "height_m": flight.get("derived_height_m"),
            "subcategory": safety.get("operating_subcategory", "A3"),
        },
        "polygon": {
            "offset_m": 0.0,
            "simplify": "auto",
            "keepout": True,
        },
        "safety": {
            "preview_radius_m": safety.get("preview_radius_m"),
        },
        "custom_polygon_4326": None,
        "last_preview_geojson": None,
    }


def check_cache_staleness(manifest: dict, cache_config) -> list[str]:
    """Return list of tile IDs missing from the local cache."""
    from jobgen.cache import check_tile_exists

    stale = []
    provenance = manifest.get("cache_provenance", {})
    for dataset in ("dem", "buildings"):
        for tile_id in provenance.get(dataset, {}).get("tile_ids", []):
            if not check_tile_exists(cache_config, dataset, tile_id):
                stale.append(f"{dataset}/{tile_id}")
    return stale
