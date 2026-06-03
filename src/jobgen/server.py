"""Browser UI server — launched with `jobgen serve`.

Endpoints:
  GET  /                        → single-page HTML UI
  GET  /api/drones              → list drone profiles
  GET  /api/config              → config defaults for form prefill
  POST /api/preview             → start geometry+zone preview job (no files written)
  POST /api/export              → start full pipeline job (writes files)
  GET  /api/progress/{job_id}  → SSE progress stream

Single-job-at-a-time: 409 returned if a job is already running.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from jobgen.config import AppConfig
from jobgen.pipeline import run_job, run_preview

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_ui_html_cache: str | None = None


def _load_ui() -> str:
    global _ui_html_cache
    if _ui_html_cache is None:
        import jinja2
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(_TEMPLATES_DIR),
            autoescape=False,
        )
        _ui_html_cache = env.get_template("ui.html").render()
    return _ui_html_cache

log = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=1)
_job_lock = threading.Lock()
_active_job_id: str | None = None
_job_queues: dict[str, asyncio.Queue] = {}
_config: AppConfig | None = None
_last_preview_result: dict | None = None   # full run_preview() result, for job_params.json


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


class PolygonOpRequest(BaseModel):
    operation: str          # "bridge" | "subtract"
    polygon: dict           # GeoJSON Polygon or MultiPolygon (current survey)
    points: list            # 4 [lng, lat] coordinates


class BatchRequest(BaseModel):
    ids: list[str]
    id_type: str = "parcels"   # "parcels" | "properties"
    folder: str | None = None
    params: dict = {}


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(config: AppConfig) -> FastAPI:
    global _config
    _config = config

    app = FastAPI(title="dkk-jobmaker", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    async def ui():
        return _load_ui()

    @app.get("/api/drones")
    async def get_drones():
        return [
            {
                "name": d.name,
                "label": d.label,
                "focal_length_mm": d.focal_length_mm,
                "pixel_pitch_um": d.pixel_pitch_um,
                "battery_minutes": d.battery_minutes,
            }
            for d in _config.drones
        ]

    @app.get("/api/config")
    async def get_config():
        import os
        drone = _config.active_drone()
        return {
            "default_drone": _config.default_drone,
            "output_dir": str(Path(_config.output.output_dir).resolve()),
            "subcategory": _config.home_safety.operating_subcategory,
            "height_m": int(drone.height_from_gsd(_config.flight.target_gsd_cm)),
            "offset_m": _config.polygon.survey_offset_m,
            "simplify": (
                "auto" if _config.polygon.simplify_mode == "auto"
                else str(_config.polygon.simplify_tolerance_m)
            ),
            "keepout": _config.home_safety.offset_enabled,
            "mml_api_key": os.environ.get("MML_API_KEY", ""),
        }

    @app.post("/api/preview")
    async def start_preview(req: PreviewRequest):
        global _active_job_id
        with _job_lock:
            if _active_job_id is not None:
                raise HTTPException(409, detail="A job is already running — please wait.")
            import uuid
            job_id = str(uuid.uuid4())
            _active_job_id = job_id

        queue: asyncio.Queue = asyncio.Queue()
        _job_queues[job_id] = queue
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
            global _active_job_id, _last_preview_result
            try:
                from shapely.geometry import shape as _shape
                custom_poly_geom = _shape(req.custom_polygon) if req.custom_polygon else None
                result = run_preview(
                    cfg,
                    parcel_ids=req.parcel_ids or None,
                    property_ids=req.property_ids or None,
                    progress_cb=cb,
                    custom_polygon_4326=custom_poly_geom,
                )
                _last_preview_result = result
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
                with _job_lock:
                    _active_job_id = None

        loop.run_in_executor(_executor, run)
        return {"job_id": job_id}

    @app.post("/api/export")
    async def start_export(req: ExportRequest):
        global _active_job_id
        with _job_lock:
            if _active_job_id is not None:
                raise HTTPException(409, detail="A job is already running — please wait.")
            import uuid
            job_id = str(uuid.uuid4())
            _active_job_id = job_id

        queue: asyncio.Queue = asyncio.Queue()
        _job_queues[job_id] = queue
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

        output_dir = str(Path(_config.output.output_dir).resolve())

        def run() -> None:
            global _active_job_id
            # Snapshot the preview result that was current when this export was
            # triggered.  Reading it here (before run_job) rather than inside
            # _write_job_params prevents a race where a concurrent preview could
            # overwrite the global between job completion and the write.
            preview_snapshot = _last_preview_result
            try:
                manifest = run_job(
                    req.job_name,
                    cfg,
                    parcel_ids=req.parcel_ids or None,
                    property_ids=req.property_ids or None,
                    progress_cb=cb,
                    custom_polygon_4326=custom_poly,
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
                        "kmz": job_dir / f"{req.job_name}.kmz",
                        "homes_kml": job_dir / f"{req.job_name}_homes.kml",
                        "dsm_tif": job_dir / f"{req.job_name}_dsm.tif",
                        "preview_html": job_dir / f"{req.job_name}_map.html",
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
                    "needs_review": manifest.get("needs_review", False),
                    "flight_ready": manifest.get("flight_ready", False),
                    "review_reasons": manifest.get("review_reasons", []),
                    "zones_checked": manifest.get("zones", {}).get("checked", False),
                    "zones_clear": not manifest.get("zones", {}).get("intersecting_zones"),
                    "zone_count": len(manifest.get("zones", {}).get("intersecting_zones", [])),
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
                with _job_lock:
                    _active_job_id = None

        loop.run_in_executor(_executor, run)
        return {"job_id": job_id}

    @app.get("/api/progress/{job_id}")
    async def progress_stream(job_id: str):
        queue = _job_queues.get(job_id)
        if not queue:
            raise HTTPException(404, detail="Job not found")

        async def generate():
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=25.0)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    if event.get("stage") in ("done", "error"):
                        _job_queues.pop(job_id, None)
                        break
                except asyncio.TimeoutError:
                    yield 'data: {"stage":"keepalive"}\n\n'

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/polygon_op")
    async def polygon_op(req: PolygonOpRequest):
        from shapely.geometry import Polygon as ShapelyPolygon, mapping, shape
        from shapely.ops import unary_union
        from shapely.validation import make_valid

        try:
            survey = shape(req.polygon)
            pts = [(c[0], c[1]) for c in req.points]  # lng, lat

            if len(pts) == 3:
                # Triangle subtract — winding order doesn't matter for 3 points
                quad = ShapelyPolygon(pts)
                if not quad.is_valid:
                    quad = make_valid(quad)
            elif len(pts) == 4:
                # Quad bridge — try both winding orders; pick non-self-intersecting
                quad = None
                for order in [
                    [pts[0], pts[1], pts[2], pts[3]],
                    [pts[0], pts[1], pts[3], pts[2]],
                ]:
                    candidate = ShapelyPolygon(order)
                    if not candidate.is_valid:
                        candidate = make_valid(candidate)
                    if candidate.is_valid and not candidate.is_empty and candidate.area > 0:
                        quad = candidate
                        break
                if quad is None:
                    raise HTTPException(400, detail="Selected points do not form a valid quadrilateral")
            else:
                raise HTTPException(400, detail=f"Expected 3 or 4 points, got {len(pts)}")

            if not quad.is_valid or quad.is_empty:
                raise HTTPException(400, detail="Selected points do not form a valid shape")

            if req.operation == "bridge":
                result = unary_union([survey, quad])
            else:
                result = survey.difference(quad)

            if result is None or result.is_empty:
                raise HTTPException(400, detail="Operation produced empty geometry")

            return {"geometry": mapping(result)}
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(400, detail=str(exc))

    # -----------------------------------------------------------------------
    # Job management endpoints
    # Note: specific literal routes (like /api/jobs/geojson) must be defined
    # BEFORE /api/jobs/{path:path} so they are matched first.
    # -----------------------------------------------------------------------

    @app.get("/api/jobs")
    async def list_jobs():
        output_dir = Path(_config.output.output_dir).resolve()
        return {"groups": _scan_jobs(output_dir)}

    @app.get("/api/jobs/{path:path}")
    async def get_job(path: str):
        output_dir = Path(_config.output.output_dir).resolve()
        folder, name, job_dir = _resolve_job_dir(output_dir, path)
        if not job_dir.is_dir():
            raise HTTPException(404, detail=f"Job '{path}' not found")
        params_path = job_dir / "job_params.json"
        manifest_path = job_dir / "manifest.json"
        manifest: dict = {}
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        if params_path.exists():
            try:
                params = json.loads(params_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise HTTPException(500, detail=f"Could not read job_params.json: {exc}")
        elif manifest:
            params = _params_from_manifest(name, manifest)
        else:
            raise HTTPException(404, detail=f"Job '{path}' has no readable data")
        stale: list[str] = []
        if manifest:
            stale = _check_cache_staleness(manifest, _config.cache)
        return {"params": params, "cache_stale": stale, "folder": folder}

    @app.patch("/api/jobs/{path:path}")
    async def update_job(path: str, body: dict):
        output_dir = Path(_config.output.output_dir).resolve()
        folder, name, job_dir = _resolve_job_dir(output_dir, path)
        if not job_dir.is_dir():
            raise HTTPException(404, detail=f"Job '{path}' not found")

        # Handle color update
        if "color" in body and "new_name" not in body:
            color = body.get("color")  # None clears the color
            params_path = job_dir / "job_params.json"
            if params_path.exists():
                try:
                    data = json.loads(params_path.read_text(encoding="utf-8"))
                    data["color"] = color
                    params_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception as exc:
                    raise HTTPException(500, detail=f"Could not update color: {exc}")
            return {"path": path, "color": color}

        # Handle rename
        new_name: str = body.get("new_name", "").strip()
        if not new_name:
            raise HTTPException(400, detail="new_name is required")
        if new_name == name:
            return {"path": path, "name": name, "folder": folder}

        new_dir = job_dir.parent / new_name
        if new_dir.exists():
            raise HTTPException(409, detail=f"Job '{new_name}' already exists in this location")

        renames: list[tuple[Path, Path]] = []
        for f in job_dir.iterdir():
            if f.name.startswith(f"{name}.") or f.name.startswith(f"{name}_"):
                suffix = f.name[len(name):]
                renames.append((f, job_dir / f"{new_name}{suffix}"))

        done: list[tuple[Path, Path]] = []
        try:
            for src, dst in renames:
                src.rename(dst)
                done.append((src, dst))
        except OSError as exc:
            for src, dst in reversed(done):
                try:
                    dst.rename(src)
                except OSError:
                    pass
            raise HTTPException(500, detail=f"Rename failed mid-way, rolled back: {exc}")

        for json_file in (job_dir / "manifest.json", job_dir / "job_params.json"):
            if json_file.exists():
                try:
                    data = json.loads(json_file.read_text(encoding="utf-8"))
                    if "job_name" in data:
                        data["job_name"] = new_name
                    json_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception:
                    pass

        try:
            job_dir.rename(new_dir)
        except OSError as exc:
            for src, dst in reversed(done):
                try:
                    dst.rename(src)
                except OSError:
                    pass
            raise HTTPException(500, detail=f"Directory rename failed, rolled back: {exc}")

        new_path = f"{folder}/{new_name}" if folder else new_name
        return {"path": new_path, "name": new_name, "folder": folder}

    @app.post("/api/jobs/{path:path}/clone")
    async def clone_job(path: str):
        output_dir = Path(_config.output.output_dir).resolve()
        folder, name, src_dir = _resolve_job_dir(output_dir, path)
        if not src_dir.is_dir():
            raise HTTPException(404, detail=f"Job '{path}' not found")
        params_path = src_dir / "job_params.json"
        manifest_path = src_dir / "manifest.json"
        if not params_path.exists() and not manifest_path.exists():
            raise HTTPException(404, detail=f"Job '{path}' has no data to clone")
        # Clone stays in the same folder as the source
        parent_dir = src_dir.parent
        base = f"{name}-copy"
        clone_name = base
        counter = 2
        while (parent_dir / clone_name).exists():
            clone_name = f"{base}{counter}"
            counter += 1
        clone_dir = parent_dir / clone_name
        clone_dir.mkdir(parents=True, exist_ok=True)
        if params_path.exists():
            try:
                params = json.loads(params_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise HTTPException(500, detail=str(exc))
        else:
            try:
                src_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                params = _params_from_manifest(name, src_manifest)
            except Exception as exc:
                raise HTTPException(500, detail=str(exc))
        params["job_name"] = clone_name
        params["saved_at"] = datetime.now(timezone.utc).isoformat()
        (clone_dir / "job_params.json").write_text(
            json.dumps(params, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        thumb_src = src_dir / "thumbnail.svg"
        if thumb_src.exists():
            shutil.copy2(thumb_src, clone_dir / "thumbnail.svg")
        clone_path = f"{folder}/{clone_name}" if folder else clone_name
        return {"path": clone_path, "name": clone_name, "folder": folder}

    @app.post("/api/jobs/{path:path}/move")
    async def move_job(path: str, body: dict):
        """Move a job to a different folder (or root if folder is null)."""
        output_dir = Path(_config.output.output_dir).resolve()
        folder, name, src_dir = _resolve_job_dir(output_dir, path)
        if not src_dir.is_dir():
            raise HTTPException(404, detail=f"Job '{path}' not found")
        to_folder: str | None = body.get("folder") or None
        if to_folder == folder:
            return {"path": path, "folder": folder}
        if to_folder:
            dest_parent = output_dir / to_folder
            dest_parent.mkdir(parents=True, exist_ok=True)
            # Write folder marker if not present
            marker = dest_parent / ".dkk-folder"
            if not marker.exists():
                marker.write_text("", encoding="utf-8")
        else:
            dest_parent = output_dir
        dest_dir = dest_parent / name
        if dest_dir.exists():
            raise HTTPException(409, detail=f"A job named '{name}' already exists in the target location")
        src_dir.rename(dest_dir)
        # Clean up source folder if now empty (only if it was a group folder)
        if folder:
            src_parent = output_dir / folder
            remaining = [d for d in src_parent.iterdir() if not d.name.startswith(".")]
            if not remaining:
                shutil.rmtree(src_parent)
        new_path = f"{to_folder}/{name}" if to_folder else name
        return {"path": new_path, "folder": to_folder}

    @app.delete("/api/jobs/{path:path}")
    async def delete_job(path: str):
        output_dir = Path(_config.output.output_dir).resolve()
        folder, name, job_dir = _resolve_job_dir(output_dir, path)
        if not job_dir.is_dir():
            raise HTTPException(404, detail=f"Job '{path}' not found")
        shutil.rmtree(job_dir)
        # Auto-remove empty folder
        if folder:
            parent = output_dir / folder
            if parent.is_dir():
                remaining = [d for d in parent.iterdir() if not d.name.startswith(".")]
                if not remaining:
                    shutil.rmtree(parent)
        return {"deleted": path}

    @app.post("/api/jobs/{path:path}/reveal")
    async def reveal_job(path: str):
        """Open the job folder in the system file manager."""
        import subprocess, sys
        output_dir = Path(_config.output.output_dir).resolve()
        folder, name, job_dir = _resolve_job_dir(output_dir, path)
        if not job_dir.is_dir():
            raise HTTPException(404, detail=f"Job '{path}' not found")
        job_path = str(job_dir)
        if sys.platform == "darwin":
            subprocess.Popen(["open", job_path])
        elif sys.platform == "win32":
            subprocess.Popen(["explorer", job_path])
        else:
            subprocess.Popen(["xdg-open", job_path])
        return {"revealed": job_path}

    # -----------------------------------------------------------------------
    # Batch skeleton job creation
    # -----------------------------------------------------------------------

    @app.post("/api/batch")
    async def start_batch(req: BatchRequest):
        import uuid
        if not req.ids:
            raise HTTPException(400, detail="ids list is empty")
        if req.id_type not in ("parcels", "properties"):
            raise HTTPException(400, detail="id_type must be 'parcels' or 'properties'")

        job_id = str(uuid.uuid4())
        queue: asyncio.Queue = asyncio.Queue()
        _job_queues[job_id] = queue
        loop = asyncio.get_running_loop()
        output_dir = str(Path(_config.output.output_dir).resolve())

        def cb(stage: str, msg: str, pct: int) -> None:
            try:
                loop.call_soon_threadsafe(
                    queue.put_nowait, {"stage": stage, "msg": msg, "pct": pct}
                )
            except Exception:
                pass

        def run() -> None:
            from jobgen.pipeline import create_skeleton_jobs
            try:
                results = create_skeleton_jobs(
                    req.ids, req.id_type, Path(output_dir),
                    req.folder, req.params, cb, _config,
                )
                ok = sum(1 for r in results if r["status"] == "ok")
                skipped = sum(1 for r in results if r["status"] == "skipped")
                failed = sum(1 for r in results if r["status"] == "error")
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

    # -----------------------------------------------------------------------
    # Folder management endpoints
    # -----------------------------------------------------------------------

    @app.post("/api/folders")
    async def create_folder(body: dict):
        folder_name: str = body.get("name", "").strip()
        if not folder_name or "/" in folder_name or folder_name.startswith("."):
            raise HTTPException(400, detail="Invalid folder name")
        output_dir = Path(_config.output.output_dir).resolve()
        folder_dir = output_dir / folder_name
        if folder_dir.exists():
            raise HTTPException(409, detail=f"Folder '{folder_name}' already exists")
        folder_dir.mkdir(parents=True, exist_ok=True)
        (folder_dir / ".dkk-folder").write_text("", encoding="utf-8")
        return {"name": folder_name}

    @app.delete("/api/folders/{folder_name}")
    async def delete_folder(folder_name: str, force: bool = False):
        output_dir = Path(_config.output.output_dir).resolve()
        folder_dir = output_dir / folder_name
        if not folder_dir.is_dir():
            raise HTTPException(404, detail=f"Folder '{folder_name}' not found")
        if not _is_folder_dir(folder_dir):
            raise HTTPException(400, detail=f"'{folder_name}' is not a group folder")
        jobs = [d for d in folder_dir.iterdir() if d.is_dir()]
        if jobs and not force:
            raise HTTPException(409, detail=f"Folder '{folder_name}' contains jobs; use force=true to delete all")
        shutil.rmtree(folder_dir)
        return {"deleted": folder_name}

    return app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _prepare_config(req: PreviewRequest) -> AppConfig:
    import copy

    cfg = copy.deepcopy(_config)

    if req.drone:
        if req.drone in [d.name for d in cfg.drones]:
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


# ---------------------------------------------------------------------------
# Job persistence helpers
# ---------------------------------------------------------------------------


def _survey_geojson_for_job(req: "ExportRequest") -> dict | None:
    """Return the survey GeoJSON geometry to use for thumbnail generation."""
    if req.custom_polygon:
        return req.custom_polygon
    if _last_preview_result:
        return _last_preview_result.get("survey")
    return None


def _make_thumbnail_svg(survey_geojson: dict | None) -> str | None:
    """Return a tiny SVG string from a GeoJSON Polygon/MultiPolygon geometry."""
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
            paths.append(f'<polygon points="{pts}" fill="#3b82f6" fill-opacity="0.7" stroke="#1d4ed8" stroke-width="1"/>')

        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" '
            f'width="{size}" height="{size}">'
            f'<rect width="{size}" height="{size}" fill="#f8fafc"/>'
            + "".join(paths)
            + "</svg>"
        )
    except Exception:
        return None


def _write_job_params(
    job_dir: Path,
    req: "ExportRequest",
    manifest: dict,
    preview_result: dict | None = None,
) -> None:
    """Write job_params.json and thumbnail.svg alongside the manifest."""
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
        },
        "polygon": {
            "offset_m": req.offset_m,
            "simplify": req.simplify,
            "keepout": req.keepout,
        },
        "safety": {
            "preview_radius_m": req.preview_radius_m,
        },
        "custom_polygon_4326": req.custom_polygon,
        "last_preview_geojson": {k: v for k, v in preview_result.items() if k != "dsm_b64"}
        if preview_result else None,
    }
    params_path = job_dir / "job_params.json"
    params_path.write_text(json.dumps(params, ensure_ascii=False, indent=2), encoding="utf-8")

    survey = _survey_geojson_for_job(req)
    svg = _make_thumbnail_svg(survey)
    if svg:
        (job_dir / "thumbnail.svg").write_text(svg, encoding="utf-8")


# ---------------------------------------------------------------------------
# Job list API helpers
# ---------------------------------------------------------------------------


def _is_job_dir(d: Path) -> bool:
    """True if d contains job marker files (job_params.json or manifest.json)."""
    return (d / "job_params.json").exists() or (d / "manifest.json").exists()


def _is_folder_dir(d: Path) -> bool:
    """True if d is a group folder (has .dkk-folder marker or contains job subdirs)."""
    if (d / ".dkk-folder").exists():
        return True
    try:
        return any(sub.is_dir() and _is_job_dir(sub) for sub in d.iterdir() if sub.is_dir())
    except PermissionError:
        return False


def _resolve_job_dir(output_dir: Path, path: str) -> tuple[str | None, str, Path]:
    """Split a job path (name or folder/name) into (folder, name, directory)."""
    parts = path.strip("/").split("/", 1)
    if len(parts) == 2:
        folder, name = parts
        return folder, name, output_dir / folder / name
    return None, parts[0], output_dir / parts[0]


def _scan_jobs(output_dir: Path) -> list[dict]:
    """Scan output_dir; return groups [{name, jobs}] with one-level folder support."""
    if not output_dir.is_dir():
        return []

    root_jobs: list[dict] = []
    folder_groups: list[dict] = []

    for entry in sorted(output_dir.iterdir()):
        if not entry.is_dir():
            continue
        if _is_folder_dir(entry):
            folder_jobs = []
            try:
                for sub in sorted(entry.iterdir()):
                    if sub.is_dir():
                        card = _read_job_card(sub, folder=entry.name)
                        folder_jobs.append(card)
            except PermissionError:
                pass
            folder_jobs.sort(
                key=lambda j: j.get("saved_at") or j.get("modified_at") or "", reverse=True
            )
            folder_groups.append({"name": entry.name, "jobs": folder_jobs})
        else:
            root_jobs.append(_read_job_card(entry, folder=None))

    root_jobs.sort(key=lambda j: j.get("saved_at") or j.get("modified_at") or "", reverse=True)

    groups = []
    if root_jobs:
        groups.append({"name": None, "jobs": root_jobs})
    groups.extend(folder_groups)
    return groups


def _read_job_card(job_dir: Path, folder: str | None = None) -> dict:
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
            "modified_at": job_dir.stat().st_mtime,
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
    return {
        "name": name,
        "folder": folder,
        "path": path,
        "status": "ok",
        "saved_at": params.get("saved_at"),
        "run_at": manifest.get("run_timestamp"),
        "area_ha": g.get("final_area_ha"),
        "vertex_count": g.get("survey_vertex_count"),
        "drone": f.get("drone"),
        "drone_label": f.get("drone_label"),
        "flight_ready": manifest.get("flight_ready"),
        "needs_review": manifest.get("needs_review"),
        "untouched": untouched,
        "color": params.get("color"),
        "thumbnail_svg": thumbnail_svg,
    }


def _params_from_manifest(name: str, manifest: dict) -> dict:
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


def _check_cache_staleness(manifest: dict, cache_config: "CacheConfig") -> list[str]:
    """Return list of tile IDs missing from the local cache."""
    from jobgen.cache import check_tile_exists
    stale = []
    provenance = manifest.get("cache_provenance", {})
    for dataset in ("dem", "buildings"):
        for tile_id in provenance.get(dataset, {}).get("tile_ids", []):
            if not check_tile_exists(cache_config, dataset, tile_id):
                stale.append(f"{dataset}/{tile_id}")
    return stale


# ---------------------------------------------------------------------------
