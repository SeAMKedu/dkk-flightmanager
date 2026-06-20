"""MCP server for dkk-flightmanager.

Primary use — integrated with the web UI (no separate process, no file lock):
    flightmanager serve          # start the web server
    # Claude Desktop / Claude Code connect via:
    #   url: http://localhost:8765/mcp/sse

Fallback use — standalone stdio (for headless/automation without the web UI):
    flightmanager mcp
    flightmanager mcp --config /path/to/config.toml
    # Claude Desktop config: {"command": "flightmanager", "args": ["mcp"], "env": {"MML_API_KEY": "..."}}
"""

from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "dkk-flightmanager",
    instructions=(
        "Tools for managing DJI terrain-following mapping jobs for Finnish agricultural parcels. "
        "Jobs are stored on disk. Use list_jobs to browse, get_job for details, "
        "create_preview to check geometry and UAS zones before committing, "
        "create_batch to create skeleton jobs from parcel/property IDs, "
        "and run_export to run the full pipeline (KMZ + DSM). "
        "Parcel IDs (peruslohkotunnus) are 10-digit numbers. "
        "Property IDs (kiinteistötunnus) look like '214-407-3-22'."
    ),
)

# Standalone-mode config (only used when NOT mounted inside flightmanager serve)
_cfg = None
_cfg_path: str = "config.toml"


def set_config_path(path: str) -> None:
    global _cfg_path, _cfg
    _cfg_path = path
    _cfg = None


def _is_integrated() -> bool:
    """True when this module is running inside the flightmanager serve FastAPI process."""
    import flightmanager._server_state as _st

    return _st.config is not None


def _config():
    """Return the active AppConfig — from server state if integrated, else load locally."""
    import flightmanager._server_state as _st

    if _st.config is not None:
        return _st.config
    global _cfg
    if _cfg is None:
        from flightmanager.config import load_config

        _cfg = load_config(_cfg_path)
    return _cfg


def _output_dir() -> Path:
    return Path(_config().output.output_dir).resolve()


@contextmanager
def _pipeline_guard():
    """Serialize pipeline operations against the web UI and other callers.

    Integrated mode: uses the in-process threading lock (_st.job_lock /
    _st.active_job_id) — same mechanism as the web UI routes, no file I/O.

    Standalone mode: uses the cross-process file lock so a running flightmanager serve
    instance is not disturbed.
    """
    if _is_integrated():
        import flightmanager._server_state as _st

        with _st.job_lock:
            if _st.active_job_id is not None:
                raise RuntimeError(
                    "Pipeline busy — web UI is running a job. Try again shortly."
                )
            _st.active_job_id = "mcp"
        try:
            yield
        finally:
            with _st.job_lock:
                _st.active_job_id = None
    else:
        from flightmanager._pipeline_lock import pipeline_lock
        from filelock import Timeout

        try:
            with pipeline_lock(Path(_config().cache.cache_dir), timeout=0):
                yield
        except Timeout:
            raise RuntimeError(
                "Pipeline busy — web UI is running a job. Try again shortly."
            )


def _prepare_config(  # noqa: C901
    drone: str | None = None,
    height_m: float | None = None,
    subcategory: str | None = None,
    offset_m: float | None = None,
    simplify: str | None = None,
    keepout: bool = True,
    preview_radius_m: float | None = None,
):
    cfg = copy.deepcopy(_config())

    if drone and drone in [d.name for d in cfg.drones]:
        cfg.default_drone = drone

    if height_m is not None:
        active = cfg.active_drone()
        cfg.flight.target_gsd_cm = active.gsd_from_height(height_m)
        cfg.flight.max_height_agl_m = max(cfg.flight.max_height_agl_m, height_m + 1)

    if subcategory:
        sub = subcategory.upper()
        if sub in ("A2", "A3"):
            cfg.home_safety.operating_subcategory = sub
            if sub == "A2" and height_m is not None:
                cfg.home_safety.home_buffer_m = height_m

    if offset_m is not None:
        cfg.polygon.survey_offset_m = offset_m

    if simplify is not None:
        if simplify.lower() == "auto":
            cfg.polygon.simplify_mode = "auto"
        else:
            try:
                tol = float(simplify)
                cfg.polygon.simplify_mode = "fixed"
                cfg.polygon.simplify_tolerance_m = max(0.0, tol)
            except (ValueError, TypeError):
                cfg.polygon.simplify_mode = "auto"

    cfg.home_safety.offset_enabled = keepout

    if preview_radius_m is not None:
        cfg.home_safety.preview_radius_m = preview_radius_m

    return cfg


# ---------------------------------------------------------------------------
# Resources — read-only data snapshots
# ---------------------------------------------------------------------------


@mcp.resource("jobs://list")
def jobs_list_resource() -> str:
    """All jobs grouped by folder. Use the list_jobs tool for filtering."""
    from flightmanager.job_store import scan_jobs

    groups = scan_jobs(_output_dir())
    return json.dumps(groups, ensure_ascii=False, indent=2)


@mcp.resource("jobs://{path}")
def job_detail_resource(path: str) -> str:
    """Full params and manifest for one job. path = 'name' or 'folder/name'."""
    from flightmanager.job_store import resolve_job_dir

    folder, name, job_dir = resolve_job_dir(_output_dir(), path)
    if not job_dir.exists():
        return json.dumps({"error": f"Job not found: {path}"})
    result: dict[str, Any] = {"path": path, "folder": folder, "name": name}
    for fname in ("job_params.json", "manifest.json"):
        p = job_dir / fname
        if p.exists():
            try:
                result[fname.replace(".json", "")] = json.loads(
                    p.read_text(encoding="utf-8")
                )
            except Exception as e:
                result[fname.replace(".json", "")] = {"error": str(e)}
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.resource("config://current")
def config_resource() -> str:
    """Active configuration summary (flight params, drone, safety settings)."""
    cfg = _config()
    drone = cfg.active_drone()
    height = drone.height_from_gsd(cfg.flight.target_gsd_cm)
    return json.dumps(
        {
            "default_drone": cfg.default_drone,
            "active_drone": drone.label,
            "target_gsd_cm": cfg.flight.target_gsd_cm,
            "derived_height_m": round(height, 1),
            "max_height_agl_m": cfg.flight.max_height_agl_m,
            "overlap_front_pct": cfg.flight.overlap_front_pct,
            "overlap_side_pct": cfg.flight.overlap_side_pct,
            "operating_subcategory": cfg.home_safety.operating_subcategory,
            "home_buffer_m": cfg.home_safety.home_buffer_m,
            "offset_enabled": cfg.home_safety.offset_enabled,
            "simplify_mode": cfg.polygon.simplify_mode,
            "simplify_tolerance_m": cfg.polygon.simplify_tolerance_m,
            "output_dir": str(_output_dir()),
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.resource("drones://list")
def drones_resource() -> str:
    """All drone profiles with camera constants and GSD at common heights."""
    cfg = _config()
    result = []
    for d in cfg.drones:
        result.append(
            {
                "name": d.name,
                "label": d.label,
                "is_default": d.name == cfg.default_drone,
                "gsd_at_50m_cm": round(d.gsd_from_height(50), 2),
                "gsd_at_80m_cm": round(d.gsd_from_height(80), 2),
                "gsd_at_100m_cm": round(d.gsd_from_height(100), 2),
                "battery_minutes": d.battery_minutes,
            }
        )
    return json.dumps(result, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Tools — read operations
# ---------------------------------------------------------------------------


@mcp.tool()
def list_jobs(
    folder: str | None = None,
    needs_review: bool | None = None,
    flight_ready: bool | None = None,
    untouched: bool | None = None,
) -> str:
    """List jobs with optional filtering.

    Args:
        folder: Filter to a specific folder name. None returns all folders.
        needs_review: If True, only jobs that need manual review before flying.
        flight_ready: If True, only jobs that are cleared to fly.
        untouched: If True, only batch skeleton jobs that haven't been exported yet.

    Returns JSON list of job cards. Each card includes: path, folder, name,
    area_ha, original_area_ha, area_lost_pct, vertex_count, drone, drone_label,
    height_m, flight_time_min, photo_count, battery_count, over_one_battery,
    strip_speed_ms, waypoint_mode, flight_ready, needs_review, untouched,
    subcategory, color, skipped, sort_order, takeoff_point_4326.
    """
    from flightmanager.job_store import scan_jobs

    groups = scan_jobs(_output_dir())

    all_jobs: list[dict] = []
    for group in groups:
        if folder is not None and group["name"] != folder:
            continue
        all_jobs.extend(group["jobs"])

    if needs_review is not None:
        all_jobs = [j for j in all_jobs if j.get("needs_review") == needs_review]
    if flight_ready is not None:
        all_jobs = [j for j in all_jobs if j.get("flight_ready") == flight_ready]
    if untouched is not None:
        all_jobs = [j for j in all_jobs if j.get("untouched") == untouched]

    for j in all_jobs:
        j.pop("thumbnail_svg", None)

    return json.dumps(all_jobs, ensure_ascii=False, indent=2)


@mcp.tool()
def get_job(path: str) -> str:
    """Get full details for one job including manifest, zone info and flight status.

    Args:
        path: Job path as 'name' or 'folder/name'.

    Returns JSON with job_params, manifest, and a derived summary.
    """
    from flightmanager.job_store import resolve_job_dir, read_job_card

    folder, name, job_dir = resolve_job_dir(_output_dir(), path)
    if not job_dir.exists():
        return json.dumps({"error": f"Job not found: {path}"})

    card = read_job_card(job_dir, folder)
    card.pop("thumbnail_svg", None)

    params: dict = {}
    manifest: dict = {}
    for fname, target in [("job_params.json", params), ("manifest.json", manifest)]:
        p = job_dir / fname
        if p.exists():
            try:
                target.update(json.loads(p.read_text(encoding="utf-8")))
            except Exception:
                pass

    zones = manifest.get("zones", {})
    zone_hits = zones.get("intersecting_zones", [])

    return json.dumps(
        {
            "card": card,
            "inputs": params.get("inputs", {}),  # editable intent
            "flight": params.get("flight", {}),  # editable intent (requested params)
            "polygon": params.get("polygon", {}),  # editable intent
            # Provenance flags come from the card (the single manifest+params merge point).
            "flight_ready": card.get("flight_ready"),
            "needs_review": card.get("needs_review"),
            "review_reasons": manifest.get("review_reasons", []),
            "geometry": manifest.get("geometry", {}),
            "stats": {
                "area_ha": card.get("area_ha"),
                "original_area_ha": card.get("original_area_ha"),
                "area_lost_pct": card.get("area_lost_pct"),
                "vertex_count": card.get("vertex_count"),
                "drone": card.get("drone"),
                "drone_label": card.get("drone_label"),
                "height_m": card.get("height_m"),
                "subcategory": card.get("subcategory"),
                "strip_speed_ms": card.get("strip_speed_ms"),
                "waypoint_mode": card.get("waypoint_mode", False),
                "adv_min_height_m": card.get("adv_min_height_m"),
                "adv_max_height_m": card.get("adv_max_height_m"),
                "flight_time_min": card.get("flight_time_min"),
                "photo_count": card.get("photo_count"),
                "battery_count": card.get("battery_count"),
                "over_one_battery": card.get("over_one_battery", False),
            },
            "zones": {
                "checked": zones.get("checked", False),
                "clear": not zone_hits,
                "hit_count": len(zone_hits),
                "hits": [
                    {
                        "name": h.get("name", ""),
                        "type": h.get("zone_type", ""),
                        "lower_limit_m": h.get("lower_limit_m_agl"),
                        "upper_limit_m": h.get("upper_limit_m_agl"),
                    }
                    for h in zone_hits
                ],
            },
            "output_files": {
                "kmz": str(next(job_dir.glob("*.kmz"), "")),
                "manifest": str(job_dir / "manifest.json")
                if (job_dir / "manifest.json").exists()
                else "",
            },
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def job_stats(folder: str | None = None) -> str:
    """Aggregate statistics across jobs or a single folder.

    Args:
        folder: Limit stats to this folder. None = all jobs.

    Returns counts, total survey area, zone hit summary, and folder list.
    """
    from flightmanager.job_store import scan_jobs

    groups = scan_jobs(_output_dir())

    all_jobs: list[dict] = []
    folder_names: list[str] = []
    for group in groups:
        if group["name"] is not None:
            folder_names.append(group["name"])
        if folder is None or group["name"] == folder:
            all_jobs.extend(group["jobs"])

    total_area = sum(j.get("area_ha") or 0.0 for j in all_jobs)

    total_flight_time = sum(j.get("flight_time_min") or 0.0 for j in all_jobs)
    total_photos = sum(j.get("photo_count") or 0 for j in all_jobs)
    total_batteries = sum(
        j.get("battery_count") or 0 for j in all_jobs if not j.get("untouched")
    )
    total_area_lost = sum(
        (j.get("area_ha") or 0.0) * (j.get("area_lost_pct") or 0.0) / 100
        for j in all_jobs
    )

    return json.dumps(
        {
            "total_jobs": len(all_jobs),
            "total_area_ha": round(total_area, 2),
            "total_area_lost_ha": round(total_area_lost, 2),
            "total_flight_time_min": round(total_flight_time, 1),
            "total_flight_time_h": round(total_flight_time / 60, 2),
            "total_photo_count": total_photos,
            "total_battery_count": total_batteries,
            "flight_ready": sum(1 for j in all_jobs if j.get("flight_ready")),
            "needs_review": sum(1 for j in all_jobs if j.get("needs_review")),
            "untouched": sum(1 for j in all_jobs if j.get("untouched")),
            "failed": sum(1 for j in all_jobs if j.get("status") == "failed"),
            "folders": folder_names,
            "filter": folder,
        },
        ensure_ascii=False,
        indent=2,
    )


# ---------------------------------------------------------------------------
# Tools — write operations
# ---------------------------------------------------------------------------


@mcp.tool()
def create_folder(name: str) -> str:
    """Create a named job group folder.

    Args:
        name: Folder name (no slashes; used as a subdirectory of output_dir).

    Returns the folder path on success.
    """
    if "/" in name or "\\" in name or not name.strip():
        return json.dumps(
            {"error": "Folder name must not contain slashes or be blank."}
        )
    folder_dir = _output_dir() / name
    folder_dir.mkdir(parents=True, exist_ok=True)
    (folder_dir / ".dkk-folder").touch()
    return json.dumps({"ok": True, "path": str(folder_dir)})


@mcp.tool()
def delete_job(path: str) -> str:
    """Delete a job and all its output files.

    Args:
        path: Job path as 'name' or 'folder/name'.

    Returns ok on success, error if the job does not exist.
    The parent folder is removed automatically if it becomes empty after deletion.
    """
    import shutil
    from flightmanager.job_store import resolve_job_dir

    _, _, job_dir = resolve_job_dir(_output_dir(), path)
    if not job_dir.exists():
        return json.dumps({"error": f"Job not found: {path}"})

    shutil.rmtree(job_dir)

    parent = job_dir.parent
    if parent != _output_dir() and parent.exists():
        remaining = [p for p in parent.iterdir() if p.name != ".dkk-folder"]
        if not remaining:
            shutil.rmtree(parent)

    return json.dumps({"ok": True, "deleted": path})


@mcp.tool()
def export_existing_job(  # noqa: C901
    path: str,
    drone: str | None = None,
    height_m: float | None = None,
    subcategory: str | None = None,
    offset_m: float | None = None,
    keepout: bool | None = None,
    simplify: str | None = None,
    color: str | None = None,
) -> str:
    """Export (generate KMZ + DSM) for a job that already exists on disk.

    Use this to export batch skeleton jobs or re-export existing jobs without
    re-supplying the original parcel/property IDs — the stored polygon and
    parameters are read from job_params.json automatically.

    Optionally override any flight or polygon parameter; stored values are used
    as defaults for anything not specified here.

    Args:
        path: Job path as 'name' or 'folder/name'.
        drone: Drone profile override (e.g. 'm3m', 'm300-p1-24').
        height_m: Flight height AGL override in metres.
        subcategory: 'A2' or 'A3' override.
        offset_m: Survey polygon expansion (+) or contraction (−) in metres.
        keepout: Whether to subtract building keep-out buffers.
        simplify: 'auto' or tolerance in metres.
        color: Hex color for map display (e.g. '#3b82f6').

    Returns job path, output files, flight status, and key stats.
    """
    from flightmanager.pipeline import export_job
    from flightmanager.job_store import resolve_job_dir

    folder, name, job_dir = resolve_job_dir(_output_dir(), path)
    if not job_dir.exists():
        return json.dumps({"error": f"Job not found: {path}"})

    params_path = job_dir / "job_params.json"
    if not params_path.exists():
        return json.dumps(
            {"error": f"No job_params.json found for {path} — cannot re-export."}
        )

    try:
        stored = json.loads(params_path.read_text(encoding="utf-8"))
    except Exception as e:
        return json.dumps({"error": f"Could not read job_params.json: {e}"})

    inputs = stored.get("inputs", {})
    parcel_ids = inputs.get("parcel_ids") or None
    property_ids = inputs.get("property_ids") or None
    custom_polygon = stored.get("custom_polygon_4326")

    if not parcel_ids and not property_ids and not custom_polygon:
        return json.dumps(
            {"error": "Stored job has no parcel IDs or polygon — cannot re-export."}
        )

    stored_flight = stored.get("flight", {})
    stored_poly = stored.get("polygon", {})
    stored_ts = stored.get("template_settings") or {}

    cfg = _prepare_config(
        drone=drone or stored_flight.get("drone"),
        height_m=height_m if height_m is not None else stored_flight.get("height_m"),
        subcategory=subcategory or stored_flight.get("subcategory"),
        offset_m=offset_m if offset_m is not None else stored_poly.get("offset_m"),
        simplify=simplify or stored_poly.get("simplify"),
        keepout=keepout if keepout is not None else stored_poly.get("keepout", True),
    )

    # Apply stored template settings (overlap, safety, advanced mode)
    if stored_ts:
        from flightmanager.routers.execution import _apply_template_settings

        _apply_template_settings(cfg, stored_ts)

    if folder:
        cfg.output.output_dir = str(_output_dir() / folder)
    else:
        cfg.output.output_dir = str(_output_dir())

    try:
        with _pipeline_guard():
            manifest, _route_geojson = export_job(
                name,
                cfg,
                parcel_ids=parcel_ids,
                property_ids=property_ids,
                custom_polygon_4326=custom_polygon,
            )
    except RuntimeError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": str(e)})

    if color:
        try:
            from flightmanager.job_store import save_params

            stored["color"] = color
            save_params(job_dir, stored)
        except Exception:
            pass

    g = manifest.get("geometry", {})
    f = manifest.get("flight", {})
    z = manifest.get("zones", {})
    bat = manifest.get("battery") or {}
    output_files = {
        k: str(p)
        for k, p in {
            "kmz": next(job_dir.glob("*.kmz"), None),
            "homes_kml": next(job_dir.glob("*_homes.kml"), None),
            "manifest": job_dir / "manifest.json",
        }.items()
        if p is not None and Path(p).exists()
    }

    return json.dumps(
        {
            "job_path": path,
            "output_dir": str(job_dir),
            "flight_ready": manifest.get("flight_ready", False),
            "needs_review": manifest.get("needs_review", False),
            "review_reasons": manifest.get("review_reasons", []),
            "survey_area_ha": g.get("final_area_ha"),
            "area_lost_pct": g.get("area_lost_pct"),
            "drone_label": f.get("drone_label"),
            "flight_height_m": f.get("derived_height_m"),
            "flight_time_min": bat.get("estimated_flight_time_min"),
            "photo_count": bat.get("estimated_photo_count"),
            "battery_count": 2 if bat.get("over_one_battery") else (1 if bat else None),
            "zones_clear": not z.get("intersecting_zones"),
            "zone_hit_count": len(z.get("intersecting_zones", [])),
            "output_files": output_files,
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def create_preview(
    parcel_ids: list[str] | None = None,
    property_ids: list[str] | None = None,
    bbox: str | None = None,
    drone: str | None = None,
    height_m: float | None = None,
    subcategory: str | None = None,
    offset_m: float | None = None,
    keepout: bool = True,
    simplify: str | None = None,
) -> str:
    """Run geometry processing and UAS zone check without writing any files.

    Use this before run_export to verify the survey area, check for restricted
    zones, and review flight parameters. Takes ~10–30 s.

    Args:
        parcel_ids: List of peruslohkotunnus IDs (10-digit strings).
        property_ids: List of kiinteistötunnus values (e.g. '214-407-3-22').
        bbox: Bounding box 'xmin,ymin,xmax,ymax' in EPSG:3067 metres.
        drone: Drone profile name (e.g. 'm3m', 'm300-p1-24'). Uses default if omitted.
        height_m: Flight height AGL in metres. Overrides GSD-derived height.
        subcategory: 'A2' or 'A3'. Default from config.
        offset_m: Expand (+) or contract (−) survey polygon by this many metres.
        keepout: Whether to subtract building keep-out buffers (default True).
        simplify: 'auto' or a tolerance in metres (e.g. '5'). Default from config.

    Returns survey area, vertex count, zone hits, and flight readiness.
    """
    from flightmanager.pipeline import analyse_survey

    if not parcel_ids and not property_ids and not bbox:
        return json.dumps(
            {"error": "Provide at least one of parcel_ids, property_ids, or bbox."}
        )

    cfg = _prepare_config(
        drone=drone,
        height_m=height_m,
        subcategory=subcategory,
        offset_m=offset_m,
        simplify=simplify,
        keepout=keepout,
    )

    bbox_3067 = None
    if bbox:
        try:
            parts = [float(x) for x in bbox.split(",")]
            if len(parts) != 4:
                raise ValueError
            bbox_3067 = tuple(parts)
        except ValueError:
            return json.dumps(
                {
                    "error": "bbox must be 'xmin,ymin,xmax,ymax' (four floats in EPSG:3067)."
                }
            )

    try:
        with _pipeline_guard():
            result = analyse_survey(
                cfg,
                parcel_ids=parcel_ids or None,
                property_ids=property_ids or None,
                bbox_3067=bbox_3067,
            )
    except RuntimeError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": str(e)})

    stats = result.get("stats", {})
    zone_hits = result.get("zone_hits", [])
    return json.dumps(
        {
            "original_area_ha": stats.get("original_area_ha"),
            "final_area_ha": stats.get("final_area_ha"),
            "area_lost_pct": stats.get("area_lost_pct"),
            "survey_vertex_count": stats.get("survey_vertex_count"),
            "flight_height_m": stats.get("flight_height_m"),
            "target_gsd_cm": stats.get("target_gsd_cm"),
            "drone": stats.get("drone"),
            "drone_label": stats.get("drone_label"),
            "home_buffer_m": stats.get("home_buffer_m"),
            "needs_review": stats.get("needs_review"),
            "flight_ready": stats.get("flight_ready"),
            "review_reasons": stats.get("review_reasons", []),
            "zones_checked": stats.get("zones_checked"),
            "zones_clear": stats.get("zones_clear"),
            "zone_count": stats.get("zone_count", 0),
            "zone_hits": [
                {
                    "name": h.get("name", ""),
                    "type": h.get("zone_type", ""),
                    "lower_limit_m": h.get("lower_limit_m_agl"),
                    "upper_limit_m": h.get("upper_limit_m_agl"),
                    "context_only": h.get("context_only", False),
                }
                for h in zone_hits
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def create_batch(
    ids: list[str],
    id_type: str,
    folder: str | None = None,
    drone: str | None = None,
    height_m: float | None = None,
    subcategory: str | None = None,
) -> str:
    """Create skeleton jobs from a list of parcel or property IDs.

    Fetches geometry for each ID and writes job_params.json with the polygon.
    No KMZ, DSM, or manifest is generated — jobs appear as 'untouched' in the
    browser UI until exported.

    Args:
        ids: List of parcel or property IDs.
        id_type: 'parcels' for peruslohkotunnus, 'properties' for kiinteistötunnus.
        folder: Group folder name (created automatically if it doesn't exist).
        drone: Drone profile override.
        height_m: Flight height AGL override.
        subcategory: 'A2' or 'A3' override.

    Returns per-ID results with counts of created, skipped, and failed.
    """
    from flightmanager.batch import create_skeleton_jobs

    if id_type not in ("parcels", "properties"):
        return json.dumps({"error": "id_type must be 'parcels' or 'properties'."})
    if not ids:
        return json.dumps({"error": "ids list is empty."})

    cfg = _prepare_config(drone=drone, height_m=height_m, subcategory=subcategory)
    output_dir = _output_dir()

    if folder:
        folder_dir = output_dir / folder
        folder_dir.mkdir(parents=True, exist_ok=True)
        (folder_dir / ".dkk-folder").touch()

    params = {
        "drone": cfg.default_drone,
        "height_m": height_m,
        "subcategory": subcategory or cfg.home_safety.operating_subcategory,
        "offset_m": cfg.polygon.survey_offset_m,
        "simplify": "auto"
        if cfg.polygon.simplify_mode == "auto"
        else str(cfg.polygon.simplify_tolerance_m),
        "keepout": cfg.home_safety.offset_enabled,
        "preview_radius_m": None,
    }

    try:
        with _pipeline_guard():
            results = create_skeleton_jobs(
                ids,
                id_type,
                output_dir,
                folder,
                params,
                progress_cb=None,
                config=cfg,
            )
    except RuntimeError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": str(e)})

    ok = sum(1 for r in results if r["status"] == "ok")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    failed = sum(1 for r in results if r["status"] == "failed")

    return json.dumps(
        {
            "created": ok,
            "skipped": skipped,
            "failed": failed,
            "folder": folder,
            "results": results,
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def run_export(
    name: str,
    parcel_ids: list[str] | None = None,
    property_ids: list[str] | None = None,
    bbox: str | None = None,
    folder: str | None = None,
    drone: str | None = None,
    height_m: float | None = None,
    subcategory: str | None = None,
    offset_m: float | None = None,
    keepout: bool = True,
    simplify: str | None = None,
    color: str | None = None,
) -> str:
    """Run the full pipeline: fetch terrain data, generate KMZ route, DSM, and homes layer.

    Takes 30–120 seconds. Run create_preview first to verify geometry and zones.

    Args:
        name: Job name (output subdirectory, no slashes).
        parcel_ids: List of peruslohkotunnus IDs.
        property_ids: List of kiinteistötunnus values.
        bbox: Bounding box 'xmin,ymin,xmax,ymax' in EPSG:3067 metres.
        folder: Group folder (created if needed).
        drone: Drone profile (e.g. 'm3m', 'm300-p1-24').
        height_m: Flight height AGL in metres.
        subcategory: 'A2' or 'A3'.
        offset_m: Survey polygon expansion (+) or contraction (−) in metres.
        keepout: Subtract building keep-out buffers (default True).
        simplify: 'auto' or tolerance in metres.
        color: Hex color for map display (e.g. '#3b82f6').

    Returns job path, output files, flight status, and key stats.
    """
    from flightmanager.pipeline import export_job

    if not parcel_ids and not property_ids and not bbox:
        return json.dumps(
            {"error": "Provide at least one of parcel_ids, property_ids, or bbox."}
        )
    if not name or "/" in name or "\\" in name:
        return json.dumps(
            {"error": "Job name must be non-empty and contain no slashes."}
        )

    cfg = _prepare_config(
        drone=drone,
        height_m=height_m,
        subcategory=subcategory,
        offset_m=offset_m,
        simplify=simplify,
        keepout=keepout,
    )

    base_output_dir = _output_dir()
    if folder:
        folder_dir = base_output_dir / folder
        folder_dir.mkdir(parents=True, exist_ok=True)
        (folder_dir / ".dkk-folder").touch()
        cfg.output.output_dir = str(folder_dir)
    else:
        cfg.output.output_dir = str(base_output_dir)

    bbox_3067 = None
    if bbox:
        try:
            parts = [float(x) for x in bbox.split(",")]
            if len(parts) != 4:
                raise ValueError
            bbox_3067 = tuple(parts)
        except ValueError:
            return json.dumps(
                {
                    "error": "bbox must be 'xmin,ymin,xmax,ymax' (four floats in EPSG:3067)."
                }
            )

    try:
        with _pipeline_guard():
            manifest, _route_geojson = export_job(
                name,
                cfg,
                parcel_ids=parcel_ids or None,
                property_ids=property_ids or None,
                bbox_3067=bbox_3067,
            )
    except RuntimeError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": str(e)})

    job_dir = Path(cfg.output.output_dir) / name
    job_rel = f"{folder}/{name}" if folder else name

    params_doc = {
        "job_name": name,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "inputs": {"parcel_ids": parcel_ids, "property_ids": property_ids},
        "flight": {
            "drone": drone or cfg.default_drone,
            "height_m": height_m,
            "subcategory": subcategory or cfg.home_safety.operating_subcategory,
        },
        "polygon": {
            "offset_m": offset_m
            if offset_m is not None
            else cfg.polygon.survey_offset_m,
            "simplify": simplify or cfg.polygon.simplify_mode,
            "keepout": keepout,
        },
        "safety": {"preview_radius_m": None},
        "custom_polygon_4326": None,
        "color": color or None,
    }
    try:
        from flightmanager.job_store import save_params

        save_params(job_dir, params_doc)
    except Exception:
        pass

    g = manifest.get("geometry", {})
    f = manifest.get("flight", {})
    z = manifest.get("zones", {})
    output_files = {
        k: str(p)
        for k, p in {
            "kmz": next(job_dir.glob("*.kmz"), None),
            "homes_kml": next(job_dir.glob("*_homes.kml"), None),
            "manifest": job_dir / "manifest.json",
        }.items()
        if p is not None and Path(p).exists()
    }

    return json.dumps(
        {
            "job_path": job_rel,
            "output_dir": str(job_dir),
            "flight_ready": manifest.get("flight_ready", False),
            "needs_review": manifest.get("needs_review", False),
            "review_reasons": manifest.get("review_reasons", []),
            "survey_area_ha": g.get("final_area_ha"),
            "area_lost_pct": g.get("area_lost_pct"),
            "survey_vertex_count": g.get("survey_vertex_count"),
            "drone_label": f.get("drone_label"),
            "flight_height_m": f.get("derived_height_m"),
            "target_gsd_cm": f.get("target_gsd_cm"),
            "zones_checked": z.get("checked", False),
            "zones_clear": not z.get("intersecting_zones"),
            "zone_hit_count": len(z.get("intersecting_zones", [])),
            "output_files": output_files,
        },
        ensure_ascii=False,
        indent=2,
    )
