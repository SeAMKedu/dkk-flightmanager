"""Pipeline execution routes: preview, export, progress SSE, and batch.

Each endpoint dispatches to a background thread and streams progress via SSE.
Shared mutable state (executor, job queue, etc.) lives in _server_state.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from flightmanager.web._server_state import SSEResponse
from flightmanager.routing.wpml import resolve_strip_speed
from pydantic import BaseModel

import flightmanager.web._server_state as _st
from flightmanager.storage.job_store import (
    make_survey_outline,
    make_thumbnail_svg,
    safe_path_segment,
    save_params,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class PreviewRequest(BaseModel):
    parcel_ids: list[str] = []
    property_ids: list[str] = []
    drone: str | None = None
    height_m: float | None = None
    subcategory: str = "A3"
    offset_m: float = 0.0
    simplify: str = "auto"
    keepout: bool = True
    preview_radius_m: float | None = None
    custom_polygon: dict | None = None  # GeoJSON Polygon geometry, or null
    route_angle_deg: float | None = None
    speed_ms: float | None = None
    takeoff_point_4326: list | None = None  # [lon, lat] — user's pinned takeoff, if any
    template_settings: dict | None = None  # per-job template/waylines overrides
    session_id: str | None = None  # client session — keys the per-session preview store


class RouteEstimateRequest(BaseModel):
    polygon_4326: dict  # GeoJSON Polygon geometry
    angle_deg: float | None = None
    height_m: float | None = None
    drone: str | None = None
    speed_ms: float | None = None
    takeoff_point_4326: list | None = None  # [lon, lat]
    overlap_front_pct: int | None = None
    overlap_side_pct: int | None = None
    advanced_mode: bool = False
    adv_min_height_m: float | None = None
    adv_max_height_m: float | None = None
    adv_powerline_clearance_m: float | None = None
    adv_slope_f: float | None = None
    adv_min_dip_m: float | None = None
    session_id: str | None = None  # client session — reads its own preview obstacles


class ExportRequest(PreviewRequest):
    job_name: str
    folder: str | None = None
    color: str | None = None
    takeoff_point_4326: list | None = None


class BatchRequest(BaseModel):
    ids: list[str]
    id_type: str = "parcels"  # "parcels" | "properties"
    folder: str | None = None
    params: dict = {}


class RefreshRequest(BaseModel):
    paths: list[str]  # job paths to recompute in place


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/api/preview")
async def start_preview(req: PreviewRequest):
    import asyncio

    job_id, queue = _begin_job()
    loop = asyncio.get_running_loop()
    cfg = _prepare_config(req)

    print(
        f"[preview] job {job_id[:8]} starting — parcels={req.parcel_ids} props={req.property_ids}"
    )

    def cb(stage: str, msg: str, pct: int) -> None:
        print(f"[preview] {pct:3d}% {stage}: {msg}")
        try:
            loop.call_soon_threadsafe(
                queue.put_nowait, {"stage": stage, "msg": msg, "pct": pct}
            )
        except Exception as e:
            print(f"[preview] callback error: {e}")

    def run() -> None:
        lock = _acquire_pipeline_lock(job_id, loop, queue, "preview")
        if lock is None:
            return
        try:
            from shapely.geometry import shape as _shape
            from flightmanager.pipeline import analyse_survey

            custom_poly_geom = (
                _shape(req.custom_polygon) if req.custom_polygon else None
            )
            result = analyse_survey(
                cfg,
                parcel_ids=req.parcel_ids or None,
                property_ids=req.property_ids or None,
                progress_cb=cb,
                custom_polygon_4326=custom_poly_geom,
                takeoff_point_4326=req.takeoff_point_4326 or None,
            )
            _st.store_preview(req.session_id, result)
            print(f"[preview] job {job_id[:8]} done")
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {"stage": "done", "pct": 100, "payload": result},
            )
        except Exception as exc:
            import traceback

            print(f"[preview] job {job_id[:8]} FAILED: {exc}")
            traceback.print_exc()
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {"stage": "error", "pct": 0, "msg": str(exc)},
            )
        finally:
            lock.release()
            _end_job()

    loop.run_in_executor(_st.executor, run)
    return {"job_id": job_id}


@router.post("/api/export")
async def start_export(req: ExportRequest):
    import asyncio

    safe_path_segment(req.job_name)
    if req.folder:
        safe_path_segment(req.folder)

    job_id, queue = _begin_job()
    loop = asyncio.get_running_loop()
    cfg = _prepare_config(req)

    custom_poly = None
    if req.custom_polygon:
        from shapely.geometry import shape

        custom_poly = shape(req.custom_polygon)

    print(f"[export] job {job_id[:8]} '{req.job_name}' starting")

    def cb(stage: str, msg: str, pct: int) -> None:
        print(f"[export] {pct:3d}% {stage}: {msg}")
        try:
            loop.call_soon_threadsafe(
                queue.put_nowait, {"stage": stage, "msg": msg, "pct": pct}
            )
        except Exception as e:
            print(f"[export] callback error: {e}")

    output_dir = str(Path(_st.config.output.output_dir).resolve())

    def run() -> None:
        lock = _acquire_pipeline_lock(job_id, loop, queue, "export")
        if lock is None:
            return
        # The caller's own last preview (session-keyed), captured before export_job.
        preview_snapshot = _st.get_preview(req.session_id)
        try:
            from flightmanager.pipeline import export_job

            manifest, route_geojson = export_job(
                req.job_name,
                cfg,
                parcel_ids=req.parcel_ids or None,
                property_ids=req.property_ids or None,
                progress_cb=cb,
                custom_polygon_4326=custom_poly,
                folder=req.folder or None,
            )
            if req.folder:
                job_dir = Path(output_dir) / req.folder / req.job_name
                job_dir.parent.mkdir(parents=True, exist_ok=True)
            else:
                job_dir = Path(output_dir) / req.job_name
            _write_job_params(job_dir, req, manifest, preview_snapshot, route_geojson)
            output_files = {
                k: str(p)
                for k, p in {
                    "kmz": job_dir / f"{req.job_name}.kmz",
                    "homes_kml": job_dir / f"{req.job_name}_homes.kml",
                    "dsm_tif": job_dir / f"{req.job_name}_dsm.tif",
                    "manifest": job_dir / "manifest.json",
                }.items()
                if p.exists()
            }
            g = manifest.get("geometry", {})
            f = manifest.get("flight", {})
            stats = {
                "original_area_ha": g.get("original_area_ha", 0),
                "final_area_ha": g.get("final_area_ha", 0),
                "area_lost_pct": g.get("area_lost_pct", 0),
                "survey_vertex_count": g.get("survey_vertex_count", 0),
                "flight_height_m": f.get("derived_height_m", 0),
                "target_gsd_cm": f.get("target_gsd_cm", 0),
                "drone": f.get("drone", ""),
                "drone_label": f.get("drone_label", ""),
                "waypoint_mode": f.get("waypoint_mode", False),
                "needs_review": manifest.get("needs_review", False),
                "flight_ready": manifest.get("flight_ready", False),
                "review_reasons": manifest.get("review_reasons", []),
                "zones_checked": manifest.get("zones", {}).get("checked", False),
                "zones_clear": not manifest.get("zones", {}).get("intersecting_zones"),
                "zone_count": len(
                    manifest.get("zones", {}).get("intersecting_zones", [])
                ),
            }
            job_rel = f"{req.folder}/{req.job_name}" if req.folder else req.job_name
            print(f"[export] job {job_id[:8]} done — {output_dir}/{job_rel}")
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {
                    "stage": "done",
                    "pct": 100,
                    "payload": {
                        "output_files": output_files,
                        "stats": stats,
                        "output_dir": output_dir,
                        "job_name": req.job_name,
                        "folder": req.folder,
                    },
                },
            )
        except Exception as exc:
            import traceback

            print(f"[export] job {job_id[:8]} FAILED: {exc}")
            traceback.print_exc()
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {"stage": "error", "pct": 0, "msg": str(exc)},
            )
        finally:
            lock.release()
            _end_job()

    loop.run_in_executor(_st.executor, run)
    return {"job_id": job_id}


@router.get("/api/progress/{job_id}")
async def progress_stream(job_id: str):
    import asyncio

    queue = _st.job_queues.get(job_id)
    if not queue:
        raise HTTPException(404, detail="Job not found")

    async def generate():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=25.0)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    if event.get("stage") in ("done", "error"):
                        _st.job_queues.pop(job_id, None)
                        break
                except asyncio.TimeoutError:
                    yield 'data: {"stage":"keepalive"}\n\n'
        except asyncio.CancelledError:
            pass

    return SSEResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/batch")
async def start_batch(req: BatchRequest):
    import asyncio
    import uuid

    if not req.ids:
        raise HTTPException(400, detail="ids list is empty")
    if req.id_type not in ("parcels", "properties"):
        raise HTTPException(400, detail="id_type must be 'parcels' or 'properties'")
    if req.folder:
        safe_path_segment(req.folder)

    job_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    _st.job_queues[job_id] = queue
    loop = asyncio.get_running_loop()
    output_dir = str(Path(_st.config.output.output_dir).resolve())

    def cb(stage: str, msg: str, pct: int) -> None:
        try:
            loop.call_soon_threadsafe(
                queue.put_nowait, {"stage": stage, "msg": msg, "pct": pct}
            )
        except Exception:
            pass

    def run() -> None:
        from flightmanager.storage.batch import create_skeleton_jobs

        try:
            results = create_skeleton_jobs(
                req.ids,
                req.id_type,
                Path(output_dir),
                req.folder,
                req.params,
                cb,
                _st.config,
            )
            ok = sum(1 for r in results if r["status"] == "ok")
            skipped = sum(1 for r in results if r["status"] == "skipped")
            failed = sum(1 for r in results if r["status"] == "error")
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {
                    "stage": "done",
                    "pct": 100,
                    "payload": {
                        "results": results,
                        "created": ok,
                        "skipped": skipped,
                        "failed": failed,
                    },
                },
            )
        except Exception as exc:
            import traceback

            traceback.print_exc()
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {"stage": "error", "pct": 0, "msg": str(exc)},
            )

    loop.run_in_executor(None, run)
    return {"job_id": job_id}


# ---------------------------------------------------------------------------
# Refresh — recompute stale jobs in place (recompute-only)
# ---------------------------------------------------------------------------


def _export_request_from_params(
    name: str, folder: str | None, params: dict
) -> ExportRequest:
    """Reconstruct an ExportRequest from a job's stored job_params."""
    inputs = params.get("inputs", {})
    flight = params.get("flight", {})
    poly = params.get("polygon", {})
    safety = params.get("safety", {})
    return ExportRequest(
        job_name=name,
        folder=folder,
        parcel_ids=inputs.get("parcel_ids") or [],
        property_ids=inputs.get("property_ids") or [],
        drone=flight.get("drone"),
        height_m=flight.get("height_m"),
        subcategory=flight.get("subcategory", "A3"),
        route_angle_deg=flight.get("route_angle_deg"),
        speed_ms=flight.get("speed_ms"),
        offset_m=(poly.get("offset_m") or 0.0),
        simplify=poly.get("simplify", "auto"),
        keepout=poly.get("keepout", True),
        preview_radius_m=safety.get("preview_radius_m"),
        custom_polygon=params.get("custom_polygon_4326"),
        takeoff_point_4326=params.get("takeoff_point_4326"),
        color=params.get("color"),
        template_settings=params.get("template_settings") or {},
    )


def _job_flags(manifest: dict) -> dict:
    """Pull the safety/stat fields that a refresh may change, for the diff summary."""
    bat = manifest.get("battery") or {}
    if "estimated_flight_time_min" in bat:
        ft = bat["estimated_flight_time_min"]
    elif "pieces" in bat:
        ft = sum(p.get("estimated_flight_time_min", 0) for p in bat["pieces"])
    else:
        ft = None
    g = manifest.get("geometry", {})
    return {
        "flight_ready": manifest.get("flight_ready"),
        "needs_review": manifest.get("needs_review"),
        "flight_time_min": round(ft, 1) if ft is not None else None,
        "final_area_ha": g.get("final_area_ha"),
    }


def _refresh_one_job(path: str, output_dir) -> dict:
    """Recompute one job in place from its stored params. Returns a result dict."""
    import json as _json

    from shapely.geometry import shape

    from flightmanager.storage.job_store import load_params, resolve_job_dir
    from flightmanager.pipeline import export_job

    folder, name, job_dir = resolve_job_dir(output_dir, path)
    params = load_params(job_dir)
    if params is None or params.get("batch_created"):
        return {
            "path": path,
            "status": "skipped",
            "reason": "no exported job to recompute",
        }

    before: dict = {}
    manifest_path = job_dir / "manifest.json"
    if manifest_path.exists():
        try:
            before = _job_flags(_json.loads(manifest_path.read_text(encoding="utf-8")))
        except Exception:
            before = {}

    ereq = _export_request_from_params(name, folder, params)
    cfg = _prepare_config(ereq)
    custom_poly = shape(ereq.custom_polygon) if ereq.custom_polygon else None
    manifest, route_geojson = export_job(
        name,
        cfg,
        parcel_ids=ereq.parcel_ids or None,
        property_ids=ereq.property_ids or None,
        custom_polygon_4326=custom_poly,
        folder=folder or None,
    )
    _write_job_params(job_dir, ereq, manifest, None, route_geojson)
    after = _job_flags(manifest)
    flips = [
        f"{k}: {before.get(k)} → {after.get(k)}"
        for k in ("flight_ready", "needs_review")
        if before.get(k) != after.get(k)
    ]
    return {
        "path": path,
        "status": "ok",
        "before": before,
        "after": after,
        "flips": flips,
    }


@router.post("/api/refresh")
async def start_refresh(req: RefreshRequest):
    """Recompute the given jobs in place with the current pipeline (recompute-only).

    Auto-applies: each job's KMZ/DSM/manifest/job_params are rewritten. Progress and a
    per-job summary (including flight_ready / needs_review flips) stream over SSE.
    """
    import asyncio
    from pathlib import Path as _Path

    if not req.paths:
        raise HTTPException(400, detail="paths list is empty")

    job_id, queue = _begin_job()
    loop = asyncio.get_running_loop()
    output_dir = _Path(_st.config.output.output_dir).resolve()

    def cb(stage: str, msg: str, pct: int) -> None:
        try:
            loop.call_soon_threadsafe(
                queue.put_nowait, {"stage": stage, "msg": msg, "pct": pct}
            )
        except Exception:
            pass

    def run() -> None:
        lock = _acquire_pipeline_lock(job_id, loop, queue, "refresh")
        if lock is None:
            return
        try:
            results: list[dict] = []
            total = len(req.paths)
            for i, path in enumerate(req.paths):
                cb("refresh", f"[{i + 1}/{total}] {path}…", int(i / total * 100))
                try:
                    results.append(_refresh_one_job(path, output_dir))
                except Exception as exc:
                    import traceback

                    traceback.print_exc()
                    results.append(
                        {"path": path, "status": "error", "reason": str(exc)}
                    )

            ok = sum(1 for r in results if r["status"] == "ok")
            flipped = sum(1 for r in results if r.get("flips"))
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {
                    "stage": "done",
                    "pct": 100,
                    "payload": {
                        "results": results,
                        "recomputed": ok,
                        "skipped": sum(1 for r in results if r["status"] == "skipped"),
                        "failed": sum(1 for r in results if r["status"] == "error"),
                        "flipped": flipped,
                    },
                },
            )
        except Exception as exc:
            import traceback

            traceback.print_exc()
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {"stage": "error", "pct": 0, "msg": str(exc)},
            )
        finally:
            lock.release()
            _end_job()

    loop.run_in_executor(_st.executor, run)
    return {"job_id": job_id}


def _load_preview_obstacles(preview: dict | None, reproject_to_3067):
    """Return (buildings, power_lines) from a stored preview result dict."""
    from flightmanager.geo.buildings import Building
    from flightmanager.geo.powerlines import PowerLine
    from shapely.geometry import shape as _shape_geom

    preview = preview or {}
    buildings: list[Building] = []
    for bd in preview.get("buildings", []):
        try:
            geom_3067 = reproject_to_3067(_shape_geom(bd["geojson"]))
            buildings.append(
                Building(
                    mtk_id=0,
                    kohdeluokka=bd.get("kohdeluokka", 42210),
                    kayttotarkoitus=None,
                    geometry=geom_3067,
                    alkupvm=None,
                    kerrosluku=None,
                )
            )
        except Exception:
            pass

    power_lines: list[PowerLine] = []
    for pl in preview.get("power_lines", []):
        try:
            geom_3067 = reproject_to_3067(_shape_geom(pl["geojson"]))
            power_lines.append(
                PowerLine(
                    mtk_id=0,
                    kohdeluokka=22312,
                    is_overhead=bool(pl.get("is_overhead", True)),
                    geometry=geom_3067,
                    alkupvm=None,
                )
            )
        except Exception:
            pass

    return buildings, power_lines


@router.post("/api/route_estimate")
async def route_estimate(req: RouteEstimateRequest):
    """Quick route estimate: actual strip intersections, no pipeline needed."""
    from shapely.geometry import Point, shape as _shape
    from flightmanager.routing import route as _route
    from flightmanager.geo.geometry import reproject_to_3067

    cfg = _st.config
    drone = (
        next((d for d in cfg.drones if d.name == req.drone), None)
        if req.drone
        else None
    )
    if drone is None:
        drone = cfg.active_drone()

    H = (
        req.height_m
        if req.height_m
        else drone.height_from_gsd(cfg.flight.target_gsd_cm)
    )
    ovf = (
        req.overlap_front_pct
        if req.overlap_front_pct is not None
        else cfg.flight.overlap_front_pct
    )
    ovs = (
        req.overlap_side_pct
        if req.overlap_side_pct is not None
        else cfg.flight.overlap_side_pct
    )
    speed_ms = (
        req.speed_ms if req.speed_ms else resolve_strip_speed(cfg.flight, drone, H)
    )

    poly_3067 = reproject_to_3067(_shape(req.polygon_4326))

    home_3067 = None
    if req.takeoff_point_4326:
        hp = reproject_to_3067(Point(req.takeoff_point_4326))
        home_3067 = (hp.x, hp.y)

    fl = cfg.flight
    buildings, power_lines = (
        _load_preview_obstacles(_st.get_preview(req.session_id), reproject_to_3067)
        if req.advanced_mode
        else (None, None)
    )
    pr = _route.plan_route(
        poly_3067,
        drone=drone,
        height_m=H,
        overlap_front_pct=ovf,
        overlap_side_pct=ovs,
        angle_deg=req.angle_deg,
        home_3067=home_3067,
        advanced=req.advanced_mode,
        buildings=buildings,
        power_lines=power_lines,
        adv_min_height_m=req.adv_min_height_m or fl.adv_min_height_m,
        adv_max_height_m=req.adv_max_height_m or fl.adv_max_height_m,
        adv_powerline_clearance_m=req.adv_powerline_clearance_m
        or fl.adv_powerline_clearance_m,
        adv_slope_f=req.adv_slope_f or fl.adv_slope_f,
        adv_min_dip_m=req.adv_min_dip_m
        if req.adv_min_dip_m is not None
        else fl.adv_min_dip_m,
    )
    adv_min_h = (
        (req.adv_min_height_m or fl.adv_min_height_m) if req.advanced_mode else None
    )

    flight_time = _route.estimate_flight_time(
        pr.route,
        flight_height_m=pr.altitude_profile[0] if pr.altitude_profile else H,
        auto_speed_ms=speed_ms,
        transit_speed_ms=cfg.flight.transitional_speed_ms,
        takeoff_security_height_m=cfg.flight.takeoff_security_height_m,
        home_3067=home_3067,
    )

    # Build strips/transits GeoJSON via the same helper the preview/export paths use,
    # so transit features carry the 1:1-safe ``altitude_m`` (the 3D view falls back to
    # strip-end turn altitudes otherwise, dipping into building frustums).
    gj = _route.route_result_to_geojson(
        pr.route,
        pr.altitude_profile,
        drone,
        ovf,
        strip_waypoints=pr.strip_waypoints,
        transit_waypoints=pr.transit_waypoints,
        adv_min_height_m=adv_min_h,
    )

    return {
        "strip_count": pr.route.strip_count,
        "photo_count": pr.route.photo_count,
        "route_dist_m": round(pr.route.total_route_dist_m),
        "flight_time_min": round(flight_time, 1),
        "angle_deg_used": round(pr.angle_deg, 1),
        "over_one_battery": flight_time > drone.battery_minutes,
        "battery_minutes": drone.battery_minutes,
        "strips_geojson": gj["strips_geojson"],
        "transits_geojson": gj["transits_geojson"],
        "advanced_mode": req.advanced_mode,
        "altitude_profile": [round(a, 1) for a in pr.altitude_profile],
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _begin_job() -> tuple[str, "object"]:
    """Reserve the single active-job slot. Returns (job_id, sse_queue) or raises 409.

    The one-job-at-a-time gate (single ThreadPoolExecutor worker + cross-process file
    lock) is the seam a real job queue replaces during the hosting epic.
    """
    import asyncio
    import uuid

    with _st.job_lock:
        if _st.active_job_id is not None:
            raise HTTPException(409, detail="A job is already running — please wait.")
        job_id = str(uuid.uuid4())
        _st.active_job_id = job_id
    queue: asyncio.Queue = asyncio.Queue()
    _st.job_queues[job_id] = queue
    return job_id, queue


def _end_job() -> None:
    """Release the active-job slot so the next preview/export can start."""
    with _st.job_lock:
        _st.active_job_id = None


def _acquire_pipeline_lock(job_id: str, loop, queue, label: str):
    """Try to acquire the cross-process file lock for a pipeline run.

    Returns the FileLock on success so the caller can release it in a finally
    block. On Timeout (standalone MCP server holds the lock), broadcasts an
    SSE error event, clears active_job_id, and returns None — the caller
    should return immediately.
    """
    from flightmanager._pipeline_lock import pipeline_lock
    from filelock import Timeout

    lock = pipeline_lock(Path(_st.config.cache.cache_dir))
    try:
        lock.acquire(timeout=0)
        return lock
    except Timeout:
        print(
            f"[{label}] job {job_id[:8]} blocked — pipeline lock held by another process"
        )
        loop.call_soon_threadsafe(
            queue.put_nowait,
            {
                "stage": "error",
                "pct": 0,
                "msg": "Pipeline busy — MCP server is running a job. Try again shortly.",
            },
        )
        with _st.job_lock:
            _st.active_job_id = None
        return None


def _apply_template_settings(cfg, ts: dict) -> None:  # noqa: C901
    """Apply template_settings dict fields (overlaps, safety, advanced mode) to cfg in-place."""
    if ts.get("overlap_front_pct") is not None:
        cfg.flight.overlap_front_pct = int(ts["overlap_front_pct"])
    if ts.get("overlap_side_pct") is not None:
        cfg.flight.overlap_side_pct = int(ts["overlap_side_pct"])
    if ts.get("takeoff_security_height_m") is not None:
        cfg.flight.takeoff_security_height_m = float(ts["takeoff_security_height_m"])
    if ts.get("rth_height_m") is not None:
        cfg.flight.rth_height_m = float(ts["rth_height_m"])
    if ts.get("rc_lost_action") is not None:
        cfg.flight.rc_lost_action = str(ts["rc_lost_action"])
    if ts.get("finish_action") is not None:
        cfg.flight.finish_action = str(ts["finish_action"])
    cfg.flight.advanced_mode = bool(ts.get("advanced_mode", False))
    if ts.get("adv_min_height_m") is not None:
        cfg.flight.adv_min_height_m = float(ts["adv_min_height_m"])
    if ts.get("adv_max_height_m") is not None:
        cfg.flight.adv_max_height_m = float(ts["adv_max_height_m"])
    if ts.get("adv_powerline_clearance_m") is not None:
        cfg.flight.adv_powerline_clearance_m = float(ts["adv_powerline_clearance_m"])
    if ts.get("adv_slope_f") is not None:
        cfg.flight.adv_slope_f = float(ts["adv_slope_f"])
    if ts.get("adv_min_dip_m") is not None:
        cfg.flight.adv_min_dip_m = float(ts["adv_min_dip_m"])

    # Inverted-cone keepout: in adaptive flight the drone descends to H_min
    # near buildings, so the A2 exclusion buffer only needs to equal H_min —
    # not the (potentially much larger) nominal height used for GSD.  The
    # altitude algorithm enforces the 1:1 rule at higher altitudes in-flight.
    if cfg.flight.advanced_mode and cfg.home_safety.operating_subcategory == "A2":
        cfg.home_safety.home_buffer_m = cfg.flight.adv_min_height_m


def _prepare_config(req: PreviewRequest, base_config=None):
    import copy

    cfg = copy.deepcopy(base_config if base_config is not None else _st.config)

    if req.drone and req.drone in [d.name for d in cfg.drones]:
        cfg.default_drone = req.drone

    if req.height_m is not None:
        active = cfg.active_drone()
        cfg.flight.target_gsd_cm = active.gsd_from_height(req.height_m)
        cfg.flight.max_height_agl_m = max(cfg.flight.max_height_agl_m, req.height_m + 1)

    sub = req.subcategory.upper()
    if sub in ("A2", "A3"):
        cfg.home_safety.operating_subcategory = sub
        if sub == "A2" and req.height_m is not None:
            cfg.home_safety.home_buffer_m = req.height_m

    cfg.polygon.survey_offset_m = req.offset_m

    if req.simplify == "auto":
        cfg.polygon.simplify_mode = "auto"
    else:
        try:
            tol = float(req.simplify)
            cfg.polygon.simplify_mode = "fixed"
            cfg.polygon.simplify_tolerance_m = max(0.0, tol)
        except (ValueError, TypeError):
            cfg.polygon.simplify_mode = "auto"

    cfg.home_safety.offset_enabled = req.keepout

    if req.preview_radius_m is not None:
        cfg.home_safety.preview_radius_m = req.preview_radius_m

    if req.speed_ms is not None and req.speed_ms > 0:
        cfg.flight.auto_flight_speed_ms = req.speed_ms

    _apply_template_settings(cfg, req.template_settings or {})

    return cfg


def _write_job_params(
    job_dir: Path,
    req: ExportRequest,
    manifest: dict,
    preview_result: dict | None = None,
    route_geojson: dict | None = None,
) -> None:
    """Write job_params.json and thumbnail.svg alongside the manifest.

    The full preview/route GeoJSON is no longer persisted — only a small
    ``survey_outline`` (for the map view and an instant first-paint on open).
    Strips/transits are recomputed by the live preview that runs on job open.
    """
    survey = req.custom_polygon or (preview_result or {}).get("survey")
    params = {
        "job_name": req.job_name,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "parcel_ids": req.parcel_ids,
            "property_ids": req.property_ids,
        },
        "flight": {
            "drone": req.drone,
            "height_m": req.height_m,
            "subcategory": req.subcategory,
            "route_angle_deg": req.route_angle_deg,
            "speed_ms": req.speed_ms,
        },
        "polygon": {
            "offset_m": req.offset_m,
            "simplify": req.simplify,
            "keepout": req.keepout,
        },
        "safety": {
            "preview_radius_m": req.preview_radius_m,
        },
        "template_settings": req.template_settings or {},
        "custom_polygon_4326": req.custom_polygon,
        "survey_outline": make_survey_outline(survey),
        "takeoff_point_4326": req.takeoff_point_4326,
        "color": req.color or None,
    }
    # Preserve existing color, sort_order, and skipped from prior save
    from flightmanager.storage.job_store import load_params

    existing = load_params(job_dir)
    if existing:
        if params["color"] is None:
            params["color"] = existing.get("color")
        if "sort_order" in existing:
            params["sort_order"] = existing["sort_order"]
        if "skipped" in existing:
            params["skipped"] = existing["skipped"]

    save_params(job_dir, params)

    svg = make_thumbnail_svg(survey)
    if svg:
        (job_dir / "thumbnail.svg").write_text(svg, encoding="utf-8")
