"""Pipeline execution routes: preview, export, progress SSE, and batch.

Each endpoint dispatches to a background thread and streams progress via SSE.
Shared mutable state (executor, job queue, etc.) lives in _server_state.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from jobgen._server_state import SSEResponse
from pydantic import BaseModel

import jobgen._server_state as _st
from jobgen.job_store import make_thumbnail_svg

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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/api/preview")
async def start_preview(req: PreviewRequest):
    import asyncio
    import uuid

    with _st.job_lock:
        if _st.active_job_id is not None:
            raise HTTPException(409, detail="A job is already running — please wait.")
        job_id = str(uuid.uuid4())
        _st.active_job_id = job_id

    queue: asyncio.Queue = asyncio.Queue()
    _st.job_queues[job_id] = queue
    loop = asyncio.get_running_loop()
    cfg = _prepare_config(req)

    print(f"[preview] job {job_id[:8]} starting — parcels={req.parcel_ids} props={req.property_ids}")

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
            from jobgen.pipeline import run_preview

            custom_poly_geom = _shape(req.custom_polygon) if req.custom_polygon else None
            result = run_preview(
                cfg,
                parcel_ids=req.parcel_ids or None,
                property_ids=req.property_ids or None,
                progress_cb=cb,
                custom_polygon_4326=custom_poly_geom,
            )
            _st.last_preview_result = result
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
            with _st.job_lock:
                _st.active_job_id = None

    loop.run_in_executor(_st.executor, run)
    return {"job_id": job_id}


@router.post("/api/export")
async def start_export(req: ExportRequest):
    import asyncio
    import uuid

    with _st.job_lock:
        if _st.active_job_id is not None:
            raise HTTPException(409, detail="A job is already running — please wait.")
        job_id = str(uuid.uuid4())
        _st.active_job_id = job_id

    queue: asyncio.Queue = asyncio.Queue()
    _st.job_queues[job_id] = queue
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
        # Snapshot the preview result before run_job so a concurrent preview
        # can't overwrite the global between job completion and the file write.
        preview_snapshot = _st.last_preview_result
        try:
            from jobgen.pipeline import run_job

            manifest = run_job(
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
            _write_job_params(job_dir, req, manifest, preview_snapshot)
            output_files = {
                k: str(p)
                for k, p in {
                    "kmz":          job_dir / f"{req.job_name}.kmz",
                    "homes_kml":    job_dir / f"{req.job_name}_homes.kml",
                    "dsm_tif":      job_dir / f"{req.job_name}_dsm.tif",
                    "preview_html": job_dir / f"{req.job_name}_map.html",
                    "manifest":     job_dir / "manifest.json",
                }.items()
                if p.exists()
            }
            g = manifest.get("geometry", {})
            f = manifest.get("flight", {})
            stats = {
                "original_area_ha":   g.get("original_area_ha", 0),
                "final_area_ha":      g.get("final_area_ha", 0),
                "area_lost_pct":      g.get("area_lost_pct", 0),
                "survey_vertex_count": g.get("survey_vertex_count", 0),
                "flight_height_m":    f.get("derived_height_m", 0),
                "target_gsd_cm":      f.get("target_gsd_cm", 0),
                "drone":              f.get("drone", ""),
                "drone_label":        f.get("drone_label", ""),
                "needs_review":       manifest.get("needs_review", False),
                "flight_ready":       manifest.get("flight_ready", False),
                "review_reasons":     manifest.get("review_reasons", []),
                "zones_checked":      manifest.get("zones", {}).get("checked", False),
                "zones_clear":        not manifest.get("zones", {}).get("intersecting_zones"),
                "zone_count":         len(manifest.get("zones", {}).get("intersecting_zones", [])),
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
                        "stats":        stats,
                        "output_dir":   output_dir,
                        "job_name":     req.job_name,
                        "folder":       req.folder,
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
            with _st.job_lock:
                _st.active_job_id = None

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
        from jobgen.batch import create_skeleton_jobs
        try:
            results = create_skeleton_jobs(
                req.ids, req.id_type, Path(output_dir),
                req.folder, req.params, cb, _st.config,
            )
            ok      = sum(1 for r in results if r["status"] == "ok")
            skipped = sum(1 for r in results if r["status"] == "skipped")
            failed  = sum(1 for r in results if r["status"] == "error")
            loop.call_soon_threadsafe(
                queue.put_nowait,
                {"stage": "done", "pct": 100, "payload": {
                    "results": results,
                    "created": ok, "skipped": skipped, "failed": failed,
                }},
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
# Helpers
# ---------------------------------------------------------------------------


def _acquire_pipeline_lock(job_id: str, loop, queue, label: str):
    """Try to acquire the cross-process file lock for a pipeline run.

    Returns the FileLock on success so the caller can release it in a finally
    block. On Timeout (standalone MCP server holds the lock), broadcasts an
    SSE error event, clears active_job_id, and returns None — the caller
    should return immediately.
    """
    from jobgen._pipeline_lock import pipeline_lock
    from filelock import Timeout
    lock = pipeline_lock(Path(_st.config.cache.cache_dir))
    try:
        lock.acquire(timeout=0)
        return lock
    except Timeout:
        print(f"[{label}] job {job_id[:8]} blocked — pipeline lock held by another process")
        loop.call_soon_threadsafe(
            queue.put_nowait,
            {"stage": "error", "pct": 0, "msg": "Pipeline busy — MCP server is running a job. Try again shortly."},
        )
        with _st.job_lock:
            _st.active_job_id = None
        return None


def _prepare_config(req: PreviewRequest):
    import copy

    cfg = copy.deepcopy(_st.config)

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

    return cfg


def _write_job_params(
    job_dir: Path,
    req: ExportRequest,
    manifest: dict,
    preview_result: dict | None = None,
) -> None:
    """Write job_params.json and thumbnail.svg alongside the manifest."""
    params = {
        "job_name": req.job_name,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "parcel_ids":   req.parcel_ids,
            "property_ids": req.property_ids,
        },
        "flight": {
            "drone":       req.drone,
            "height_m":    req.height_m,
            "subcategory": req.subcategory,
        },
        "polygon": {
            "offset_m": req.offset_m,
            "simplify":  req.simplify,
            "keepout":   req.keepout,
        },
        "safety": {
            "preview_radius_m": req.preview_radius_m,
        },
        "custom_polygon_4326": req.custom_polygon,
        "takeoff_point_4326":  req.takeoff_point_4326,
        "color": req.color or None,
        "last_preview_geojson": (
            {k: v for k, v in preview_result.items() if k != "dsm_b64"}
            if preview_result else None
        ),
    }
    # Preserve existing color when this request carries none
    if params["color"] is None and (job_dir / "job_params.json").exists():
        try:
            existing = json.loads(
                (job_dir / "job_params.json").read_text(encoding="utf-8")
            )
            params["color"] = existing.get("color")
        except Exception:
            pass

    (job_dir / "job_params.json").write_text(
        json.dumps(params, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    survey = req.custom_polygon or (preview_result or {}).get("survey")
    svg = make_thumbnail_svg(survey)
    if svg:
        (job_dir / "thumbnail.svg").write_text(svg, encoding="utf-8")
