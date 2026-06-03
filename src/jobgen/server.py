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
from typing import Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from jobgen.config import AppConfig
from jobgen.pipeline import PreviewCache, run_job, run_preview

log = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=1)
_job_lock = threading.Lock()
_active_job_id: str | None = None
_job_queues: dict[str, asyncio.Queue] = {}
_config: AppConfig | None = None
_preview_cache: PreviewCache | None = None
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


class ExportRequest(PreviewRequest):
    job_name: str
    custom_polygon: dict | None = None  # GeoJSON Polygon geometry, or null


class PolygonOpRequest(BaseModel):
    operation: str          # "bridge" | "subtract"
    polygon: dict           # GeoJSON Polygon or MultiPolygon (current survey)
    points: list            # 4 [lng, lat] coordinates


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(config: AppConfig) -> FastAPI:
    global _config
    _config = config

    app = FastAPI(title="dkk-jobmaker", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    async def ui():
        return _UI_HTML

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

        cached = _preview_cache is not None and _preview_cache.covers(
            req.parcel_ids or None, req.property_ids or None,
            2.0 * cfg.home_safety.home_buffer_m,
        )
        print(
            f"[preview] job {job_id[:8]} starting — parcels={req.parcel_ids} props={req.property_ids}"
            + (" (cached parcels+buildings)" if cached else "")
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
            global _active_job_id, _preview_cache, _last_preview_result
            try:
                result, new_cache = run_preview(
                    cfg,
                    parcel_ids=req.parcel_ids or None,
                    property_ids=req.property_ids or None,
                    progress_cb=cb,
                    _cache=_preview_cache,
                )
                _preview_cache = new_cache
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
            try:
                manifest = run_job(
                    req.job_name,
                    cfg,
                    parcel_ids=req.parcel_ids or None,
                    property_ids=req.property_ids or None,
                    progress_cb=cb,
                    custom_polygon_4326=custom_poly,
                )
                job_dir = Path(output_dir) / req.job_name
                _write_job_params(job_dir, req, manifest)
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
                print(f"[export] job {job_id[:8]} done — {output_dir}/{req.job_name}")
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
    # -----------------------------------------------------------------------

    @app.get("/api/jobs")
    async def list_jobs():
        output_dir = Path(_config.output.output_dir).resolve()
        return {"jobs": _scan_jobs(output_dir)}

    @app.get("/api/jobs/{name}")
    async def get_job(name: str):
        output_dir = Path(_config.output.output_dir).resolve()
        job_dir = output_dir / name
        if not job_dir.is_dir():
            raise HTTPException(404, detail=f"Job '{name}' not found")
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
            raise HTTPException(404, detail=f"Job '{name}' has no readable data")
        stale: list[str] = []
        if manifest:
            stale = _check_cache_staleness(manifest, _config.cache)
        return {"params": params, "cache_stale": stale}

    @app.patch("/api/jobs/{name}")
    async def rename_job(name: str, body: dict):
        new_name: str = body.get("new_name", "").strip()
        if not new_name:
            raise HTTPException(400, detail="new_name is required")
        if new_name == name:
            return {"name": name}
        output_dir = Path(_config.output.output_dir).resolve()
        old_dir = output_dir / name
        new_dir = output_dir / new_name
        if not old_dir.is_dir():
            raise HTTPException(404, detail=f"Job '{name}' not found")
        if new_dir.exists():
            raise HTTPException(409, detail=f"Job '{new_name}' already exists")
        # Rename prefixed output files
        for f in old_dir.iterdir():
            if f.name.startswith(f"{name}.") or f.name.startswith(f"{name}_"):
                suffix = f.name[len(name):]
                f.rename(old_dir / f"{new_name}{suffix}")
        # Update job_name in JSON files
        for json_file in (old_dir / "manifest.json", old_dir / "job_params.json"):
            if json_file.exists():
                try:
                    data = json.loads(json_file.read_text(encoding="utf-8"))
                    if "job_name" in data:
                        data["job_name"] = new_name
                    json_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception:
                    pass
        old_dir.rename(new_dir)
        return {"name": new_name}

    @app.post("/api/jobs/{name}/clone")
    async def clone_job(name: str):
        output_dir = Path(_config.output.output_dir).resolve()
        src_dir = output_dir / name
        if not src_dir.is_dir():
            raise HTTPException(404, detail=f"Job '{name}' not found")
        params_path = src_dir / "job_params.json"
        manifest_path = src_dir / "manifest.json"
        if not params_path.exists() and not manifest_path.exists():
            raise HTTPException(404, detail=f"Job '{name}' has no data to clone")
        # Find a unique clone name
        base = f"{name}-copy"
        clone_name = base
        counter = 2
        while (output_dir / clone_name).exists():
            clone_name = f"{base}{counter}"
            counter += 1
        clone_dir = output_dir / clone_name
        clone_dir.mkdir(parents=True, exist_ok=True)
        if params_path.exists():
            try:
                params = json.loads(params_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise HTTPException(500, detail=str(exc))
        else:
            manifest_src = src_dir / "manifest.json"
            try:
                src_manifest = json.loads(manifest_src.read_text(encoding="utf-8"))
                params = _params_from_manifest(name, src_manifest)
            except Exception as exc:
                raise HTTPException(500, detail=str(exc))
        params["job_name"] = clone_name
        params["saved_at"] = datetime.now(timezone.utc).isoformat()
        (clone_dir / "job_params.json").write_text(
            json.dumps(params, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # Copy thumbnail if present
        thumb_src = src_dir / "thumbnail.svg"
        if thumb_src.exists():
            shutil.copy2(thumb_src, clone_dir / "thumbnail.svg")
        return {"name": clone_name}

    @app.delete("/api/jobs/{name}")
    async def delete_job(name: str):
        output_dir = Path(_config.output.output_dir).resolve()
        job_dir = output_dir / name
        if not job_dir.is_dir():
            raise HTTPException(404, detail=f"Job '{name}' not found")
        shutil.rmtree(job_dir)
        return {"deleted": name}

    @app.post("/api/jobs/{name}/reveal")
    async def reveal_job(name: str):
        """Open the job folder in the system file manager (Finder / Explorer / Nautilus)."""
        import subprocess, sys
        output_dir = Path(_config.output.output_dir).resolve()
        job_dir = output_dir / name
        if not job_dir.is_dir():
            raise HTTPException(404, detail=f"Job '{name}' not found")
        path = str(job_dir)
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
        elif sys.platform == "win32":
            subprocess.Popen(["explorer", path])
        else:
            subprocess.Popen(["xdg-open", path])
        return {"revealed": path}

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
            pts = " ".join(f"{to_svg(c[0], c[1])[0]:.1f},{to_svg(c[0], c[1])[1]:.1f}" for c in ring)
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


def _write_job_params(job_dir: Path, req: "ExportRequest", manifest: dict) -> None:
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
        "last_preview_geojson": {k: v for k, v in _last_preview_result.items() if k != "dsm_b64"}
        if _last_preview_result else None,
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


def _scan_jobs(output_dir: Path) -> list[dict]:
    """Scan output_dir for job subdirectories; return sorted list of job cards."""
    jobs = []
    if not output_dir.is_dir():
        return jobs
    for job_dir in sorted(output_dir.iterdir()):
        if not job_dir.is_dir():
            continue
        card = _read_job_card(job_dir)
        if card:
            jobs.append(card)
    jobs.sort(key=lambda j: j.get("saved_at") or j.get("modified_at") or "", reverse=True)
    return jobs


def _read_job_card(job_dir: Path) -> dict | None:
    """Build a summary card dict for one job directory."""
    name = job_dir.name
    manifest_path = job_dir / "manifest.json"
    params_path = job_dir / "job_params.json"
    thumb_path = job_dir / "thumbnail.svg"

    if not manifest_path.exists() and not params_path.exists():
        return {
            "name": name,
            "status": "failed",
            "saved_at": None,
            "modified_at": job_dir.stat().st_mtime,
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

    g = manifest.get("geometry", {})
    f = manifest.get("flight", {})
    return {
        "name": name,
        "status": "ok",
        "saved_at": params.get("saved_at"),
        "run_at": manifest.get("run_timestamp"),
        "area_ha": g.get("final_area_ha"),
        "vertex_count": g.get("survey_vertex_count"),
        "drone": f.get("drone"),
        "drone_label": f.get("drone_label"),
        "flight_ready": manifest.get("flight_ready"),
        "needs_review": manifest.get("needs_review"),
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
# Embedded HTML / CSS / JS UI
# ---------------------------------------------------------------------------

_UI_HTML = r"""\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>dkk-jobmaker</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet.draw/1.0.4/leaflet.draw.css">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;height:100vh;display:flex;flex-direction:column;overflow:hidden;background:#f1f5f9}
#hdr{background:#1e293b;color:#f8fafc;padding:9px 14px;display:flex;align-items:center;gap:10px;flex-shrink:0}
#hdr h1{font-size:14px;font-weight:700;letter-spacing:-.01em}
#main{display:flex;flex:1;overflow:visible}
#sb{width:272px;background:#f8fafc;border-right:1px solid #e2e8f0;overflow-y:auto;padding:8px 7px;flex-shrink:0;display:flex;flex-direction:column;gap:7px}
#mc{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0;position:relative}
#map{flex:1;z-index:0}
#legend{position:absolute;top:10px;right:10px;z-index:500;background:#fff;border:1px solid #e2e8f0;border-radius:6px;padding:8px 10px;font-size:11px;display:block;box-shadow:0 2px 8px rgba(0,0,0,.12);min-width:148px;transition:opacity .2s}
#legend.inactive{opacity:.35;pointer-events:none}
#legend h4{font-size:9px;font-weight:700;color:#94a3b8;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px}
@keyframes area-pulse{0%,100%{box-shadow:0 0 0 0 rgba(59,130,246,.5)}60%{box-shadow:0 0 0 5px rgba(59,130,246,.08)}}
.area-focus{border-color:#3b82f6 !important;animation:area-pulse 1.4s ease 3}
.leg-row{display:grid;grid-template-columns:20px 22px 1fr;align-items:center;margin:3px 0}
.leg-eye{background:none;border:none;cursor:pointer;padding:0;width:20px;display:flex;align-items:center;justify-content:center;color:#374151;transition:color .15s}
.leg-eye .eye-slash{display:none}
.leg-eye.off{color:#9ca3af}
.leg-eye.off .eye-open{display:none}
.leg-eye.off .eye-slash{display:block}
.leg-icon{display:flex;align-items:center;justify-content:center;gap:2px}
.l-swatch{width:18px;height:10px;border-radius:2px}
.l-dot{width:10px;height:10px;border-radius:50%}
#sp{height:200px;background:#f8fafc;border-top:1px solid #e2e8f0;padding:10px 12px;overflow-y:auto;flex-shrink:0}
.sec{background:#fff;border:1px solid #e2e8f0;border-radius:6px;padding:8px 10px}
.sec h3{font-size:10px;font-weight:700;color:#94a3b8;text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px}
label{display:block;font-size:11px;font-weight:500;color:#475569;margin-bottom:2px;margin-top:6px}
label:first-of-type{margin-top:0}
input,select,textarea{width:100%;padding:5px 7px;border:1px solid #cbd5e1;border-radius:4px;font-size:12px;background:#fff;color:#1e293b;outline:none}
input:focus,select:focus,textarea:focus{border-color:#3b82f6;box-shadow:0 0 0 2px rgba(59,130,246,.15)}
textarea{resize:vertical;min-height:44px;font-family:monospace;font-size:11px}
.path-hint{font-size:10px;color:#64748b;margin-top:3px;word-break:break-all;line-height:1.4}
.gsd-row{font-size:11px;color:#64748b;margin-top:3px}
.ck-row{display:flex;align-items:center;gap:6px;font-size:12px;color:#374151;margin-top:7px;cursor:pointer;user-select:none}
.ck-row input{width:auto;accent-color:#3b82f6;cursor:pointer}
.btn-pair{display:flex;gap:5px;margin-top:5px}
button{width:100%;padding:7px 10px;border:none;border-radius:4px;font-size:12px;font-weight:600;cursor:pointer;transition:background .12s,opacity .12s}
button:disabled{opacity:.4;cursor:not-allowed}
#xb{background:#16a34a;color:#fff;margin-top:5px}
#xb:not(:disabled):hover{background:#15803d}
.rst-btn{background:#7c3aed;color:#fff;font-size:11px}
.rst-btn:not(:disabled):hover{background:#6d28d9}
.pill-row{display:flex;gap:4px;margin-top:4px}
.pill{flex:1;padding:4px 0;border:1px solid #cbd5e1;border-radius:20px;background:#fff;color:#475569;font-size:11px;font-weight:600;cursor:pointer;transition:background .12s,color .12s,border-color .12s}
.pill.active{background:#0f172a;color:#f8fafc;border-color:#0f172a}
.pill:hover:not(.active){background:#f1f5f9;border-color:#94a3b8}
.simp-row{display:flex;align-items:center;gap:4px;margin-top:4px}
.simp-pill{flex:1;padding:4px 10px;border:1px solid #cbd5e1;border-radius:20px;background:#fff;color:#475569;font-size:11px;font-weight:600;cursor:pointer;transition:background .12s,color .12s,border-color .12s;white-space:nowrap}
.simp-pill.active{background:#0f172a;color:#f8fafc;border-color:#0f172a}
.simp-pill:hover:not(.active){background:#f1f5f9;border-color:#94a3b8}
.simp-step{width:26px;padding:4px 0;border:1px solid #cbd5e1;border-radius:4px;background:#fff;color:#374151;font-size:13px;font-weight:700;cursor:pointer;flex-shrink:0;transition:background .12s}
.simp-step:hover:not(:disabled){background:#f1f5f9}
.simp-val{width:36px;flex-shrink:0;text-align:center;font-size:11px;color:#475569;font-weight:600}
#pgwrap{margin-top:5px;opacity:0;transition:opacity .15s;pointer-events:none}
#pgtrack{background:#e2e8f0;border-radius:3px;height:5px;overflow:hidden}
#pgfill{background:#3b82f6;height:100%;width:0;transition:width .25s;border-radius:3px}
#pgmsg{font-size:10px;color:#64748b;margin-top:3px;height:14px;overflow:hidden}
#errdiv{display:none;background:#fef2f2;border:1px solid #fecaca;border-radius:4px;padding:6px 8px;font-size:11px;color:#dc2626;margin-top:5px;word-break:break-word}
#modbadge{display:none;background:#fef3c7;color:#92400e;border:1px solid #fde68a;padding:3px 7px;border-radius:4px;font-size:10px;margin-top:4px}
/* status panel */
.sh{font-size:13px;font-weight:700;margin-bottom:7px}
.sok{color:#16a34a}.swrn{color:#d97706}.serr{color:#dc2626}
.sgrid{display:grid;grid-template-columns:repeat(3,1fr);gap:5px;margin-bottom:7px}
.sbox{background:#fff;border:1px solid #e2e8f0;border-radius:4px;padding:5px 7px}
.slbl{font-size:9px;color:#94a3b8;text-transform:uppercase;letter-spacing:.04em}
.sval{font-size:13px;font-weight:700;color:#1e293b;line-height:1.3}
.rlist{margin-top:4px}
.ritem{font-size:10px;color:#dc2626;padding:1px 0}
/* toast */
#toast{position:fixed;bottom:14px;right:14px;background:#fff;border:1px solid #e2e8f0;border-radius:8px;padding:10px 13px;box-shadow:0 4px 16px rgba(0,0,0,.12);width:220px;z-index:9999;display:none}
#ttitle{font-size:11px;font-weight:700;color:#1e293b;margin-bottom:5px}
#ttrack{background:#e2e8f0;border-radius:3px;height:6px;overflow:hidden}
#tfill{background:#3b82f6;height:100%;width:0;transition:width .25s}
#tmsg{font-size:10px;color:#64748b;margin-top:3px}
#bridge-hint{position:absolute;top:50px;left:50%;transform:translateX(-50%);background:#1e293b;color:#fff;padding:5px 14px;border-radius:20px;font-size:11px;z-index:600;display:none;pointer-events:none;white-space:nowrap;box-shadow:0 2px 8px rgba(0,0,0,.25);transition:background .2s}
.bridge-btn{background:#7c3aed;color:#fff;font-size:11px;margin-top:4px}
.bridge-btn:not(:disabled):hover{background:#6d28d9}
.bridge-btn.active{background:#dc2626}
.bridge-btn.active:not(:disabled):hover{background:#b91c1c}
/* Jobs panel */
#jp{width:260px;flex-shrink:0;background:#0f172a;display:flex;flex-direction:column;position:relative;transition:margin-left .2s ease}
#jp.closed{margin-left:-260px}
#jp-inner{width:260px;height:100%;overflow-y:auto;overflow-x:hidden;display:flex;flex-direction:column;gap:0}
#jp-hdr{padding:8px 10px 6px;border-bottom:1px solid #1e293b;display:flex;align-items:center;gap:6px;flex-shrink:0}
#jp-hdr span{color:#94a3b8;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;white-space:nowrap}
#jp-filter{background:#1e293b;border:1px solid #334155;border-radius:4px;color:#f8fafc;font-size:11px;padding:3px 6px;flex:1;min-width:0;outline:none}
#jp-filter:focus{border-color:#3b82f6}
#jp-newjob{margin:6px 8px;padding:6px 10px;background:#1e293b;border:1px dashed #334155;border-radius:4px;color:#94a3b8;font-size:11px;font-weight:600;cursor:pointer;text-align:left;flex-shrink:0;transition:background .12s,color .12s}
#jp-newjob:hover{background:#334155;color:#f8fafc}
#jp-list{flex:1;overflow-y:auto;padding:4px 6px 8px}
#jp-tog{position:absolute;right:-16px;top:50%;transform:translateY(-50%);width:16px;height:44px;background:#0f172a;border:none;border-radius:0 4px 4px 0;color:#64748b;cursor:pointer;font-size:8px;display:flex;align-items:center;justify-content:center;z-index:10;transition:color .12s}
#jp-tog:hover{color:#f8fafc}
.jcard{background:#1e293b;border:1px solid #334155;border-radius:5px;margin-bottom:5px;display:flex;align-items:center;gap:0;cursor:pointer;transition:border-color .12s,background .12s;overflow:visible;position:relative}
.jcard:hover{border-color:#475569;background:#263548}
.jcard.active{border-color:#3b82f6;border-left:3px solid #3b82f6}
.jcard.failed{opacity:.55;cursor:default}
.jcard-thumb{width:48px;height:48px;flex-shrink:0;display:flex;align-items:center;justify-content:center;padding:3px;background:#0f172a;border-radius:4px 0 0 4px;overflow:hidden}
.jcard-thumb svg{width:42px;height:42px;border-radius:3px}
.jcard-body{flex:1;min-width:0;padding:5px 4px}
.jcard-name{font-size:11px;font-weight:600;color:#f8fafc;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.jcard-meta{font-size:9px;color:#64748b;margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.jcard-right{display:flex;flex-direction:column;align-items:center;gap:2px;padding:4px 4px 4px 0;flex-shrink:0}
.jbadge{font-size:9px;font-weight:700;width:16px;height:16px;border-radius:50%;display:flex;align-items:center;justify-content:center}
.jbadge.ok{background:#15803d;color:#fff}
.jbadge.wrn{background:#d97706;color:#fff}
.jbadge.fail{background:#dc2626;color:#fff}
.jcard-menu-btn{background:none;border:none;color:#475569;cursor:pointer;font-size:15px;width:22px;height:22px;display:flex;align-items:center;justify-content:center;border-radius:3px;padding:0;line-height:1;transition:color .12s}
.jcard-menu-btn:hover{color:#f8fafc}
.jmenu{position:absolute;right:4px;top:4px;background:#1e293b;border:1px solid #334155;border-radius:5px;box-shadow:0 4px 16px rgba(0,0,0,.4);z-index:200;min-width:110px;overflow:hidden}
.jmenu-item{display:block;width:100%;padding:7px 12px;background:none;border:none;color:#f8fafc;font-size:11px;text-align:left;cursor:pointer;transition:background .1s}
.jmenu-item:hover{background:#334155}
.jmenu-item.danger{color:#fca5a5}
.jmenu-item.danger:hover{background:#7f1d1d}
.jcard-rename-input{background:#0f172a;border:1px solid #3b82f6;border-radius:3px;color:#f8fafc;font-size:11px;font-weight:600;padding:1px 4px;width:100%;outline:none}
.jcard-del-yes{background:#dc2626;color:#fff;border:none;border-radius:3px;padding:3px 8px;font-size:11px;cursor:pointer;font-weight:600}
.jcard-del-no{background:#334155;color:#f8fafc;border:none;border-radius:3px;padding:3px 8px;font-size:11px;cursor:pointer;font-weight:600}
#stale-notice{display:none;background:#1e293b;border:1px solid #fde68a;border-radius:4px;padding:5px 8px;font-size:10px;color:#fde68a;margin-bottom:6px}
/* Confirm modal */
#confirm-modal{display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:9999;align-items:center;justify-content:center}
#confirm-card{background:#fff;border-radius:8px;padding:18px 20px;max-width:340px;width:92%;box-shadow:0 8px 32px rgba(0,0,0,.2)}
#confirm-msg{font-size:13px;color:#1e293b;margin-bottom:14px;line-height:1.5}
#confirm-card .btn-pair{display:flex;gap:6px}
#confirm-discard{background:#dc2626;color:#fff;border:none;border-radius:4px;padding:7px 12px;font-size:12px;font-weight:600;cursor:pointer;flex:1}
#confirm-cancel{background:#e2e8f0;color:#374151;border:none;border-radius:4px;padding:7px 12px;font-size:12px;font-weight:600;cursor:pointer;flex:1}
/* Leaflet.draw midpoint handles — diamond via ::before so Leaflet's own
   translate3d positioning transform on the icon element is not overridden.
   top/left 50% + translate(-50%,-50%) centres the diamond on the anchor point. */
.ld-mid{background:transparent!important;border:none!important;overflow:visible!important}
.ld-mid::before{content:''!important;position:absolute!important;width:7px!important;height:7px!important;top:50%!important;left:50%!important;transform:translate(-50%,-50%) rotate(45deg)!important;background:#fff!important;border:1px solid #94a3b8!important;box-sizing:border-box!important}
</style>
</head>
<body>
<div id="hdr">
  <h1>dkk-jobmaker</h1>
</div>
<div id="main">
  <div id="jp">
    <div id="jp-inner">
      <div id="jp-hdr">
        <span>Jobs</span>
        <input type="text" id="jp-filter" placeholder="Filter&#8230;">
      </div>
      <button id="jp-newjob" onclick="newJob()">&#43; New Job</button>
      <div id="jp-list"></div>
    </div>
    <button id="jp-tog" onclick="toggleJp()" title="Toggle jobs panel">&#9664;</button>
  </div>

  <div id="sb">
    <div id="stale-notice"></div>

    <div class="sec">
      <h3>Job</h3>
      <button id="xb" onclick="startExport()" disabled style="margin-top:3px">Save</button>
      <div id="pgwrap">
        <div id="pgtrack"><div id="pgfill"></div></div>
        <div id="pgmsg"></div>
      </div>
      <div id="errdiv"></div>
      <label style="margin-top:8px">Name</label>
      <input type="text" id="jname" placeholder="job-20260602-1423">
      <div class="path-hint" id="pathint">Output: —</div>
    </div>

    <div class="sec" id="area-sec">
      <h3>Area</h3>
      <label>Parcel IDs (peruslohkotunnus)</label>
      <textarea id="pids" rows="2" placeholder="5241087453&#10;5241087453, 5241087454"></textarea>
      <label>Property IDs (kiinteistötunnus)</label>
      <textarea id="kids" rows="2" placeholder="214-407-3-22"></textarea>
    </div>

    <div class="sec">
      <h3>Flight</h3>
      <label>Subcategory</label>
      <div class="pill-row">
        <button class="pill active" id="sub-a3" onclick="setSub('A3')">A3</button>
        <button class="pill"        id="sub-a2" onclick="setSub('A2')">A2</button>
      </div>
      <label>Drone</label>
      <select id="dsel"></select>
      <label>Height (m AGL)</label>
      <input type="number" id="hgt" min="20" max="120" step="1" value="60">
      <div class="gsd-row">GSD: <span id="gsdv">—</span> cm/px</div>
      <label>Warning radius (m) <span id="warn-radius-hint" style="font-weight:400;color:#94a3b8;cursor:default" title="3× flight height">3:1</span></label>
      <input type="number" id="warn-radius" min="0" max="2000" step="10" value="180">
    </div>

    <div class="sec">
      <h3>Polygon</h3>
      <label>Offset (m)</label>
      <input type="number" id="offset" value="0" step="1">
      <label>Simplify</label>
      <div class="simp-row">
        <button class="simp-pill active" id="simp-auto" onclick="setSimpAuto()">Auto</button>
        <button class="simp-step" id="simp-minus" onclick="simpStep(-1)" disabled>&#8722;</button>
        <span class="simp-val" id="simp-val">—</span>
        <button class="simp-step" id="simp-plus"  onclick="simpStep(+1)" disabled>&#43;</button>
      </div>
      <label class="ck-row">
        <input type="checkbox" id="kochk" checked>
        Keep-out subtraction
      </label>
      <div id="modbadge">&#9888; Polygon manually edited</div>
      <button class="rst-btn" id="rstbtn" onclick="resetPoly()" disabled style="margin-top:6px">&#8635; Reset polygon</button>
      <button class="bridge-btn" id="bridge-btn" onclick="toggleBridgeMode()" disabled title="Right-click any vertex to start, or use this button">&#9003; Bridge / Cut</button>
    </div>


  </div>

  <div id="mc">
    <div id="map"></div>
    <div id="bridge-hint"></div>
    <div id="legend">
      <h4>Layers</h4>
      <div class="leg-row" id="leg-dsm-row" style="display:none">
        <button class="leg-eye off" id="leg-dsm" title="DSM elevation"><svg class="eye-open" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg><svg class="eye-slash" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg></button>
        <div class="leg-icon"><div class="l-swatch" style="background:linear-gradient(to right,#000,#fff);border:1px solid #9ca3af;"></div></div>
        <span>DSM elevation</span>
      </div>
      <div class="leg-row">
        <button class="leg-eye" id="leg-areas" title="Original parcel"><svg class="eye-open" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg><svg class="eye-slash" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg></button>
        <div class="leg-icon"><div class="l-swatch" style="background:none;border:1.5px dashed #16a34a;"></div></div>
        <span>Original parcel</span>
      </div>
      <div class="leg-row">
        <button class="leg-eye" id="leg-survey" title="Survey polygon"><svg class="eye-open" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg><svg class="eye-slash" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg></button>
        <div class="leg-icon"><div class="l-swatch" style="background:#3b82f6;opacity:.7;border:2px solid #1d4ed8;"></div></div>
        <span>Survey polygon</span>
      </div>
      <div class="leg-row">
        <button class="leg-eye" id="leg-vertices" title="Polygon vertices"><svg class="eye-open" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg><svg class="eye-slash" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg></button>
        <div class="leg-icon"><div class="l-dot" style="background:#93c5fd;border:1px solid #1d4ed8;"></div></div>
        <span>Polygon vertices</span>
      </div>
      <div class="leg-row" id="leg-rings-row" style="display:none">
        <button class="leg-eye" id="leg-rings" title="Warning radius circles"><svg class="eye-open" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg><svg class="eye-slash" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg></button>
        <div class="leg-icon"><div class="l-swatch" style="background:#fef08a;opacity:.8;border:1px dashed #ca8a04;"></div></div>
        <span id="leg-rings-label">Warning radius</span>
      </div>
      <div class="leg-row" id="leg-ko-row">
        <button class="leg-eye" id="leg-ko" title="Keep-out circles"><svg class="eye-open" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg><svg class="eye-slash" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg></button>
        <div class="leg-icon"><div class="l-swatch" style="background:#fca5a5;opacity:.8;border:1px dashed #dc2626;"></div></div>
        <span>Keep-out</span>
      </div>
      <div class="leg-row" id="leg-bldgs-row">
        <button class="leg-eye" id="leg-bldgs" title="Buildings"><svg class="eye-open" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg><svg class="eye-slash" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg></button>
        <div class="leg-icon"><div class="l-dot" style="background:#dc2626;"></div><div class="l-dot" style="background:#d97706;"></div></div>
        <span>Buildings</span>
      </div>
      <div class="leg-row" id="leg-zones-row">
        <button class="leg-eye" id="leg-zones" title="UAS zones"><svg class="eye-open" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg><svg class="eye-slash" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg></button>
        <div class="leg-icon"><div class="l-swatch" style="background:#f97316;opacity:.4;border:1px solid #ea580c;"></div></div>
        <span>UAS zones</span>
      </div>
    </div>
    <div id="sp">
      <div id="spcontent"></div>
    </div>
  </div>
</div>


<div id="confirm-modal">
  <div id="confirm-card">
    <div id="confirm-msg"></div>
    <div class="btn-pair">
      <button id="confirm-discard">Discard changes</button>
      <button id="confirm-cancel" onclick="hideConfirmModal()">Cancel</button>
    </div>
  </div>
</div>

<div id="toast">
  <div id="ttitle">Running…</div>
  <div id="ttrack"><div id="tfill"></div></div>
  <div id="tmsg">Starting…</div>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet.draw/1.0.4/leaflet.draw.js"></script>
<script>
// ── State ─────────────────────────────────────────────────────────────────────
var drones = [];
var outputDir = '';
var previewData = null;
var editedPoly = null;
var polyModified = false;
var isRunning = false;
var _pendingPreview = false;  // startPreview() deferred because isRunning was true
var currentSSE = null;
var editMode = false;
var _bridgeMode = false;
// Jobs panel state
var _dirty = false;
var _activeJob = null;
var _jpOpen = localStorage.getItem('jp-open') !== 'false';
var _jobsCache = [];
var _bridgePts = [];        // [{coord:[lng,lat], polyIdx}]
var _bridgeVerts = [];      // all vertices of current survey geometry
var _bridgeGroup = null;
var _bridgeStyledEls = [];  // Leaflet.draw handle elements coloured during picking
var _editCHandler = null;  // container-level contextmenu capture (edit mode)
var _editKHandler = null;  // container-level click capture (bridge picking)
var _editVHandler = null;  // draw:editvertex → re-patch midpoint icons

// ── Map ───────────────────────────────────────────────────────────────────────
var map = L.map('map', {preferCanvas:true}).setView([64.5, 26.0], 5);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {attribution:'&copy; OpenStreetMap', maxZoom:19}).addTo(map);

// DSM pane sits below overlayPane (400) so vectors always render on top
map.createPane('dsmPane');
map.getPane('dsmPane').style.zIndex = 350;
map.getPane('dsmPane').style.pointerEvents = 'none';

var editLayers = new L.FeatureGroup().addTo(map);
map.addControl(new L.Control.Draw({draw:false, edit:{featureGroup:editLayers, remove:false}}));

map.on(L.Draw.Event.EDITED, function(e) {
  e.layers.eachLayer(function(l) {
    editedPoly = layerGeom(l);
    polyModified = true; markDirty();
    document.getElementById('modbadge').style.display = 'block';
  });
  editMode = false;
  map.doubleClickZoom.enable();
  if (lrs.survey) lrs.survey.addTo(map);
});

var lrs = {dsm:null, survey:null, vertices:null, rings:null, areas:null, bldgs:null, ko:null, zones:null};

function layerGeom(layer) {
  var lls = layer.getLatLngs();
  var ring = (Array.isArray(lls[0]) ? lls[0] : lls).map(function(ll){return [ll.lng,ll.lat];});
  ring.push(ring[0]);
  return {type:'Polygon', coordinates:[ring]};
}

// ── Init ──────────────────────────────────────────────────────────────────────
async function init() {
  // Default job name = timestamp
  document.getElementById('jname').value = defaultJobName();

  try {
    var r = await fetch('/api/drones');
    if (!r.ok) throw new Error('drones ' + r.status);
    drones = await r.json();
    var sel = document.getElementById('dsel');
    drones.forEach(function(d) {
      var o = document.createElement('option');
      o.value = d.name; o.textContent = d.label;
      sel.appendChild(o);
    });

    var cr = await fetch('/api/config');
    if (!cr.ok) throw new Error('config ' + cr.status);
    var cfg = await cr.json();

    outputDir = cfg.output_dir || '';
    updatePathHint();

    if (cfg.default_drone) sel.value = cfg.default_drone;
    if (cfg.subcategory) setSub(cfg.subcategory, true);
    if (cfg.offset_m !== undefined) document.getElementById('offset').value = cfg.offset_m;
    if (cfg.height_m) {
      var h0 = Math.round(cfg.height_m);
      document.getElementById('hgt').value = h0;
      document.getElementById('warn-radius').value = 3 * h0;
    }
    if (cfg.simplify && cfg.simplify !== 'auto') {
      setSimpManual(parseFloat(cfg.simplify) || 0, true);
    }
    document.getElementById('kochk').checked = cfg.keepout !== false;
    updateGsd();
    console.log('[init] config loaded, outputDir='+outputDir+', drone='+cfg.default_drone);
  } catch(e) {
    console.error('[init] failed:', e);
  }
  renderStatus(null);
  focusArea();
  // Jobs panel
  setJpOpen(_jpOpen);
  loadJobsList();
}

function defaultJobName() {
  var n = new Date();
  return 'job-' + n.getFullYear()
    + String(n.getMonth()+1).padStart(2,'0')
    + String(n.getDate()).padStart(2,'0')
    + '-'
    + String(n.getHours()).padStart(2,'0')
    + String(n.getMinutes()).padStart(2,'0');
}

function updatePathHint() {
  var jn = document.getElementById('jname').value.trim() || '(name)';
  document.getElementById('pathint').textContent = 'Output: ' + outputDir + '/' + jn;
}
document.getElementById('jname').addEventListener('input', updatePathHint);

// ── GSD ───────────────────────────────────────────────────────────────────────
function updateGsd() {
  var h = parseFloat(document.getElementById('hgt').value);
  var d = drones.find(function(x){return x.name === document.getElementById('dsel').value;});
  var el = document.getElementById('gsdv');
  if (!d || isNaN(h)) { el.textContent = '—'; return; }
  el.textContent = (h * d.pixel_pitch_um / (d.focal_length_mm * 10)).toFixed(2);
}
var _radiusLinked = true;

function setRadiusLinked(linked) {
  _radiusLinked = linked;
  var hint = document.getElementById('warn-radius-hint');
  hint.style.textDecoration = linked ? '' : 'line-through';
  hint.style.cursor = linked ? 'default' : 'pointer';
  hint.title = linked ? '3× flight height' : 'Double-click to restore 3:1 link';
  if (linked) {
    var h = parseFloat(document.getElementById('hgt').value);
    if (!isNaN(h) && h > 0) document.getElementById('warn-radius').value = Math.round(3 * h);
  }
}

document.getElementById('warn-radius-hint').addEventListener('dblclick', function() {
  if (!_radiusLinked) { setRadiusLinked(true); redrawRings(); }
});

document.getElementById('hgt').addEventListener('input', function() {
  updateGsd();
  if (_radiusLinked) {
    var h = parseFloat(this.value);
    if (!isNaN(h) && h > 0) document.getElementById('warn-radius').value = Math.round(3 * h);
  }
});
document.getElementById('dsel').addEventListener('change', updateGsd);
document.getElementById('warn-radius').addEventListener('input', function() {
  markDirty();
  setRadiusLinked(false);
  redrawRings();
});
document.getElementById('warn-radius').addEventListener('blur', function() {
  if (this.value === '') { setRadiusLinked(true); redrawRings(); }
});

// ── Subcategory pills ─────────────────────────────────────────────────────────
var _subVal = 'A3';
function setSub(v, silent) {
  _subVal = v;
  document.getElementById('sub-a3').classList.toggle('active', v === 'A3');
  document.getElementById('sub-a2').classList.toggle('active', v === 'A2');
  if (!silent) { clearPolyEdit(); scheduleAutoUpdate(); }
}
function getSub() { return _subVal; }

// ── Simplify control ──────────────────────────────────────────────────────────
var _simpSteps = [0, 1, 2, 3, 5, 8, 10, 15, 20];
var _simpIdx = 0;   // index into _simpSteps when in manual mode
var _simpAuto = true;

function _simpRender() {
  document.getElementById('simp-auto').classList.toggle('active', _simpAuto);
  document.getElementById('simp-minus').disabled = !_simpAuto && _simpIdx === 0;
  document.getElementById('simp-plus').disabled  = !_simpAuto && _simpIdx === _simpSteps.length - 1;
  document.getElementById('simp-val').textContent = _simpAuto ? '—' : (_simpSteps[_simpIdx] === 0 ? 'off' : _simpSteps[_simpIdx] + ' m');
}
function setSimpAuto(silent) {
  _simpAuto = true; _simpRender();
  if (!silent) { clearPolyEdit(); scheduleAutoUpdate(); }
}
function setSimpManual(v, silent) {
  _simpAuto = false;
  // snap to nearest step
  var best = 0;
  for (var i = 0; i < _simpSteps.length; i++) {
    if (Math.abs(_simpSteps[i] - v) < Math.abs(_simpSteps[best] - v)) best = i;
  }
  _simpIdx = best; _simpRender();
  if (!silent) { clearPolyEdit(); scheduleAutoUpdate(); }
}
function simpStep(dir) {
  _simpAuto = false;
  _simpIdx = Math.max(0, Math.min(_simpSteps.length - 1, _simpIdx + dir));
  _simpRender(); clearPolyEdit(); scheduleAutoUpdate();
}
function getSimplify() {
  return _simpAuto ? 'auto' : String(_simpSteps[_simpIdx]);
}
_simpRender();

// Clear polygon edit when geometry params change
['offset','kochk','dsel'].forEach(function(id){
  document.getElementById(id).addEventListener('change', clearPolyEdit);
});
document.getElementById('pids').addEventListener('input', clearPolyEdit);
document.getElementById('kids').addEventListener('input', clearPolyEdit);
function clearPolyEdit() {
  editedPoly = null; polyModified = false;
  document.getElementById('modbadge').style.display = 'none';
}

// Auto-update on flight / polygon param changes (only when a preview exists)
var _autoTimer = null;
var _lastPreviewedIds = '';

function idsKey() {
  return document.getElementById('pids').value.trim() + '||' + document.getElementById('kids').value.trim();
}

function scheduleAutoUpdate(force) {
  markDirty();
  if (!force && !previewData) return;
  if (_autoTimer) clearTimeout(_autoTimer);
  _autoTimer = setTimeout(function() { _autoTimer = null; startPreview(); }, 400);
}
['dsel','kochk'].forEach(function(id){
  document.getElementById(id).addEventListener('change', scheduleAutoUpdate);
});
document.getElementById('hgt').addEventListener('change', scheduleAutoUpdate);
document.getElementById('offset').addEventListener('change', scheduleAutoUpdate);

// Auto-update when area IDs are committed (both fields lose focus)
function onIdBlur() {
  setTimeout(function() {
    var active = document.activeElement;
    if (active === document.getElementById('pids') || active === document.getElementById('kids')) return;
    var key = idsKey();
    if (!key.replace('||','').trim()) return; // no IDs entered
    if (key === _lastPreviewedIds) return;     // unchanged since last fetch
    scheduleAutoUpdate(true);
  }, 150);
}
document.getElementById('pids').addEventListener('blur', onIdBlur);
document.getElementById('kids').addEventListener('blur', onIdBlur);

// New Job — reset editor to a blank slate
function newJob() {
  if (isRunning) return;
  confirmIfDirty(_doNewJob);
}
function _doNewJob() {
  // Cancel any pending auto-update
  if (_autoTimer) { clearTimeout(_autoTimer); _autoTimer = null; }
  // Reset IDs and job name
  document.getElementById('jname').value = defaultJobName();
  document.getElementById('pids').value = '';
  document.getElementById('kids').value = '';
  updatePathHint();
  // Clear map
  Object.values(lrs).forEach(function(l){ if(l) map.removeLayer(l); });
  lrs = {dsm:null, survey:null, vertices:null, rings:null, areas:null, bldgs:null, ko:null, zones:null};
  editLayers.clearLayers();
  editMode = false;
  _detachEditListeners();
  // Reset state
  previewData = null; editedPoly = null; polyModified = false; _lastPreviewedIds = '';
  _activeJob = null; _dirty = false;
  clearPolyEdit();
  clearError();
  // Reset polygon controls to a clean neutral state
  document.getElementById('offset').value = 0;
  setSimpAuto(true);  // silent — no scheduleAutoUpdate, no clearPolyEdit
  hideStaleNotice();
  document.getElementById('xb').disabled = true;
  document.getElementById('rstbtn').disabled = true;
  document.getElementById('bridge-btn').disabled = true;
  renderStatus(null);
  setRadiusLinked(true);
  document.getElementById('legend').classList.add('inactive');
  // Deselect panel card
  document.querySelectorAll('.jcard').forEach(function(c){ c.classList.remove('active'); });
  focusArea();
  map.setView([64.5, 26.0], 5);
}

// ── Area section focus hint ───────────────────────────────────────────────────
function focusArea() {
  var el = document.getElementById('area-sec');
  // Re-trigger animation by removing and re-adding the class
  el.classList.remove('area-focus');
  void el.offsetWidth; // force reflow
  el.classList.add('area-focus');
}
function clearAreaFocus() {
  document.getElementById('area-sec').classList.remove('area-focus');
}
// Clear highlight as soon as the user types in either ID field
document.getElementById('pids').addEventListener('input', clearAreaFocus);
document.getElementById('kids').addEventListener('input', clearAreaFocus);

// ── Warning rings ─────────────────────────────────────────────────────────────
function redrawRings() {
  if (lrs.rings) { map.removeLayer(lrs.rings); lrs.rings = null; }
  var warnR = parseFloat(document.getElementById('warn-radius').value) || 0;
  var row = document.getElementById('leg-rings-row');
  var lbl = document.getElementById('leg-rings-label');
  if (!previewData || !previewData.buildings || !warnR) {
    if (row) row.style.display = 'none';
    return;
  }
  var wg = L.layerGroup();
  var count = 0;
  previewData.buildings.forEach(function(b) {
    if (!b.is_keepout) return;
    var pt = centroid(b.geojson);
    if (!pt) return;
    L.circle(pt, {
      radius: warnR, color: '#ca8a04', weight: 1.5,
      fillColor: '#fef08a', fillOpacity: 0.25, dashArray: '4 4', interactive: false
    }).addTo(wg);
    count++;
  });
  if (!count) { if (row) row.style.display = 'none'; return; }
  lrs.rings = wg;
  if (lbl) lbl.textContent = warnR + ' m radius';
  var btn = document.getElementById('leg-rings');
  if (!btn || !btn.classList.contains('off')) lrs.rings.addTo(map);
  if (row) row.style.display = '';
}

// ── Legend ────────────────────────────────────────────────────────────────────
(function initLegend() {
  var rows = [
    {btnId:'leg-dsm',      lrKey:'dsm',      rowId:'leg-dsm-row',   startOff:true},
    {btnId:'leg-areas',    lrKey:'areas',    rowId:null},
    {btnId:'leg-survey',   lrKey:'survey',   rowId:null},
    {btnId:'leg-vertices', lrKey:'vertices', rowId:null},
    {btnId:'leg-rings',    lrKey:'rings',    rowId:'leg-rings-row'},
    {btnId:'leg-ko',       lrKey:'ko',       rowId:'leg-ko-row'},
    {btnId:'leg-bldgs',    lrKey:'bldgs',    rowId:'leg-bldgs-row'},
    {btnId:'leg-zones',    lrKey:'zones',    rowId:'leg-zones-row'},
  ];
  rows.forEach(function(r) {
    document.getElementById(r.btnId).addEventListener('click', function() {
      var layer = lrs[r.lrKey];
      if (!layer) return;
      if (this.classList.toggle('off')) { map.removeLayer(layer); }
      else { layer.addTo(map); }
    });
  });
  document.getElementById('legend').classList.add('inactive');
  window._legendRows = rows;
})();

// savedVis: optional {lrKey: bool} map of user-chosen visibility to restore.
// When omitted (e.g. first render, open-job), defaults are applied (startOff for DSM).
function resetLegend(savedVis) {
  window._legendRows.forEach(function(r) {
    var btn = document.getElementById(r.btnId);
    var hasLayer = !!lrs[r.lrKey];
    if (r.rowId) {
      document.getElementById(r.rowId).style.display = hasLayer ? '' : 'none';
    }
    if (!hasLayer) { btn.classList.add('off'); return; }
    // Restore user's toggle choice if available; otherwise apply the startup default
    var visible = (savedVis && r.lrKey in savedVis) ? savedVis[r.lrKey] : !r.startOff;
    btn.classList.toggle('off', !visible);
    // renderMap already added all new layers to the map; remove the ones that should be hidden
    if (!visible) map.removeLayer(lrs[r.lrKey]);
  });
  document.getElementById('legend').classList.remove('inactive');
}

// ── Form ──────────────────────────────────────────────────────────────────────
function parseIds(txt) {
  return txt.split(/[,\s]+/).map(function(s){return s.trim();}).filter(Boolean);
}

function getParams() {
  return {
    parcel_ids: parseIds(document.getElementById('pids').value),
    property_ids: parseIds(document.getElementById('kids').value),
    drone: document.getElementById('dsel').value || null,
    height_m: parseFloat(document.getElementById('hgt').value) || null,
    subcategory: getSub(),
    offset_m: parseFloat(document.getElementById('offset').value) || 0,
    simplify: getSimplify(),
    keepout: document.getElementById('kochk').checked,
    preview_radius_m: parseFloat(document.getElementById('warn-radius').value) || null
  };
}

function showError(msg) {
  var el = document.getElementById('errdiv');
  el.textContent = 'Error: ' + msg;
  el.style.display = 'block';
}
function clearError() {
  document.getElementById('errdiv').style.display = 'none';
  document.getElementById('errdiv').textContent = '';
}

// ── Preview ───────────────────────────────────────────────────────────────────
async function startPreview() {
  if (isRunning) return;
  clearError();
  var p = getParams();
  if (!p.parcel_ids.length && !p.property_ids.length) {
    showError('Enter at least one parcel ID or property ID.'); return;
  }
  await runJob('/api/preview', p, 'Preview', onPreviewDone);
}

// ── Save (formerly Export) ────────────────────────────────────────────────────
async function startExport() {
  if (isRunning) return;
  clearError();
  var jn = document.getElementById('jname').value.trim();
  if (!jn) { showError('Enter a job name.'); return; }
  var p = Object.assign(getParams(), {
    job_name: jn,
    custom_polygon: polyModified ? editedPoly : null
  });
  await runJob('/api/export', p, 'Saving…', onSaveDone);
}

// ── Job runner ────────────────────────────────────────────────────────────────
async function runJob(endpoint, params, label, onDone) {
  isRunning = true;
  document.getElementById('xb').disabled = true;
  showToast(label + '…', 0, 'Starting…');
  showPg(true, 0, 'Starting…');

  var res;
  try {
    res = await fetch(endpoint, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(params)
    });
  } catch(e) { onErr('Network error: ' + e.message); return; }

  if (!res.ok) {
    var e2 = await res.json().catch(function(){return {detail:'HTTP ' + res.status};});
    onErr((e2.detail || 'Server error') + ' (HTTP ' + res.status + ')'); return;
  }

  var data = await res.json();
  var jid = data.job_id;
  console.log('[' + label + '] job_id=' + jid);

  if (currentSSE) currentSSE.close();
  currentSSE = new EventSource('/api/progress/' + jid);

  currentSSE.onmessage = function(e) {
    var d;
    try { d = JSON.parse(e.data); } catch(ex) { console.error('SSE parse error', e.data); return; }
    console.log('[sse]', d.stage, d.pct + '%', d.msg || '');
    if (d.stage === 'keepalive') return;
    if (d.stage === 'error') {
      currentSSE.close(); onErr(d.msg);
    } else if (d.stage === 'done') {
      currentSSE.close(); finishRun(); onDone(d.payload);
    } else {
      showPg(true, d.pct, d.msg);
      showToast(null, d.pct, d.msg);
    }
  };

  currentSSE.onerror = function(ev) {
    console.error('[sse] onerror', ev, 'readyState='+currentSSE.readyState);
    // readyState 2 = CLOSED — means we already called .close() → ignore
    if (currentSSE.readyState === EventSource.CLOSED) return;
    currentSSE.close();
    onErr('SSE connection lost (check server terminal for details).');
  };
}

function showPg(on, pct, msg) {
  var wrap = document.getElementById('pgwrap');
  wrap.style.opacity = on ? '1' : '0';
  wrap.style.pointerEvents = on ? '' : 'none';
  document.getElementById('pgfill').style.width = (pct||0) + '%';
  document.getElementById('pgmsg').textContent = on ? (msg || '') : '';
}
function showToast(title, pct, msg) {
  var t = document.getElementById('toast');
  t.style.display = 'block';
  if (title) document.getElementById('ttitle').textContent = title;
  document.getElementById('tfill').style.width = (pct||0) + '%';
  document.getElementById('tmsg').textContent = msg || '';
}
function finishRun() {
  isRunning = false;
  // xb state is owned by each completion callback (onPreviewDone/onSaveDone/onErr)
  // — do NOT touch it here to avoid a stale-previewData flicker.
  document.getElementById('toast').style.display = 'none';
  showPg(false, 0, '');
  if (_pendingPreview) { _pendingPreview = false; startPreview(); }
}
function onErr(msg) {
  console.error('[err]', msg);
  finishRun();
  // Restore xb to whatever is correct given the current state
  document.getElementById('xb').disabled = !previewData;
  document.getElementById('toast').style.display = 'none';
  showError(msg);
}

// ── Map ───────────────────────────────────────────────────────────────────────
function onPreviewDone(payload) {
  console.log('[preview done]', payload.stats);
  // Capture user's eye-toggle choices before renderMap replaces the layer objects
  var savedVis = null;
  if (!document.getElementById('legend').classList.contains('inactive')) {
    savedVis = {};
    window._legendRows.forEach(function(r) {
      savedVis[r.lrKey] = !document.getElementById(r.btnId).classList.contains('off');
    });
  }
  previewData = payload;
  _lastPreviewedIds = idsKey();
  clearAreaFocus();
  document.getElementById('xb').disabled = false;
  document.getElementById('rstbtn').disabled = false;
  // bridge-btn stays disabled until the user enters edit mode
  try {
    renderMap(payload);
    redrawRings();
    resetLegend(savedVis);  // null on first render → applies startOff defaults
    renderStatus(payload.stats);
  } catch(e) {
    console.error('[onPreviewDone]', e);
    showError('Render error: ' + e.message);
  }
}

// Convert a GeoJSON geometry (Polygon or MultiPolygon) to an array of
// L.polygon layers. Does NOT add them to the map or editLayers.
function geomToPolys(geom, style) {
  var out = [];
  if (!geom) return out;
  if (geom.type === 'Polygon') {
    var lls = geom.coordinates[0].map(function(c){return [c[1],c[0]];});
    out.push(L.polygon(lls, style));
  } else if (geom.type === 'MultiPolygon') {
    geom.coordinates.forEach(function(pc) {
      var lls = pc[0].map(function(c){return [c[1],c[0]];});
      out.push(L.polygon(lls, style));
    });
  }
  return out;
}

function renderMap(data) {
  exitBridgeMode();
  // Remove all layers from map and reset lrs (do NOT touch editLayers)
  Object.values(lrs).forEach(function(l){ if(l) map.removeLayer(l); });
  lrs = {dsm:null, survey:null, vertices:null, rings:null, areas:null, bldgs:null, ko:null, zones:null};
  editLayers.clearLayers();

  // DSM grayscale overlay — rendered in dsmPane (z 350) below all vectors
  if (data.dsm_b64 && data.dsm_bounds) {
    var b = data.dsm_bounds; // [west, south, east, north]
    var dg = L.layerGroup();
    L.imageOverlay(
      'data:image/png;base64,' + data.dsm_b64,
      [[b[1], b[0]], [b[3], b[2]]],
      {opacity: 0.65, interactive: false, pane: 'dsmPane'}
    ).addTo(dg);
    lrs.dsm = dg;  // start hidden — user enables via legend
  }

  // Parcel/property outlines (green dashed)
  if (data.original_areas && data.original_areas.length) {
    var fc = {type:'FeatureCollection', features:data.original_areas.map(function(g){
      return {type:'Feature', geometry:g, properties:{}};
    })};
    lrs.areas = L.geoJSON(fc, {
      style:{color:'#16a34a',weight:2,dashArray:'6 3',fillOpacity:.04}
    }).addTo(map);
  }

  // Keep-out circles — one true circle per keepout building
  var koBuf = data.stats && data.stats.home_buffer_m;
  if (koBuf && data.buildings && data.buildings.length) {
    var kg = L.layerGroup();
    data.buildings.forEach(function(b) {
      if (!b.is_keepout) return;
      var pt = centroid(b.geojson);
      if (!pt) return;
      L.circle(pt, {
        radius: koBuf, color: '#dc2626', weight: 1,
        fillColor: '#fca5a5', fillOpacity: 0.20, dashArray: '4 4'
      }).addTo(kg);
    });
    if (kg.getLayers().length) lrs.ko = kg.addTo(map);
  }

  // UAS restriction zones (orange)
  var zf = (data.zone_hits||[]).filter(function(z){return z.geojson;}).map(function(z){
    return {type:'Feature', geometry:z.geojson, properties:{name:z.name, r:z.restriction}};
  });
  if (zf.length) {
    lrs.zones = L.geoJSON({type:'FeatureCollection', features:zf}, {
      style:{color:'#ea580c',weight:2,fillColor:'#f97316',fillOpacity:.14},
      onEachFeature:function(f,l){
        l.bindPopup('<b>'+f.properties.name+'</b><br>'+f.properties.r);
      }
    }).addTo(map);
  }

  // Buildings (red = keepout, yellow = info)
  if (data.buildings && data.buildings.length) {
    var bg = L.layerGroup();
    data.buildings.forEach(function(b) {
      var c = b.is_keepout ? '#dc2626' : '#FFBB00';
      var pt = centroid(b.geojson);
      if (pt) L.circleMarker(pt, {radius:5,color:c,fillColor:c,fillOpacity:.85,weight:1.5}).addTo(bg);
    });
    lrs.bldgs = bg.addTo(map);
  }

  // Survey polygon — display only; NOT added to editLayers to avoid Leaflet
  // double-ownership conflicts. Editing copies the polygon on demand.
  var surveyStyle = {color:'#1d4ed8', weight:2.5, fillColor:'#3b82f6', fillOpacity:.17};
  var surveyPolys = geomToPolys(data.survey, surveyStyle);
  if (surveyPolys.length) {
    lrs.survey = L.featureGroup(surveyPolys).addTo(map);
    lrs.survey.eachLayer(function(l) {
      l.on('dblclick', function(e) { L.DomEvent.stop(e); if (!editMode && !_bridgeMode) toggleEdit(); });
    });
    console.log('[renderMap] survey bounds', lrs.survey.getBounds());
    map.fitBounds(lrs.survey.getBounds(), {padding:[40,40]});
  } else {
    console.warn('[renderMap] no survey polygons rendered, survey type:', data.survey && data.survey.type);
  }

  // Vertex dots (on top of survey polygon)
  if (data.survey) lrs.vertices = _buildVertexLayer(data.survey).addTo(map);
}

function centroid(geom) {
  try {
    if (geom.type==='Point') return [geom.coordinates[1], geom.coordinates[0]];
    if (geom.type==='Polygon') {
      var cs = geom.coordinates[0];
      return [cs.reduce(function(s,c){return s+c[1];},0)/cs.length,
              cs.reduce(function(s,c){return s+c[0];},0)/cs.length];
    }
  } catch(e){}
  return null;
}

// ── Status panel ──────────────────────────────────────────────────────────────
var _dash = '<span style="color:#cbd5e1">—</span>';
function renderStatus(s) {
  var sh = !s ? ''
    : s.flight_ready ? '<div class="sh"><span class="sok">&#10003; FLIGHT READY</span></div>'
    : s.needs_review  ? '<div class="sh"><span class="swrn">&#9888; NEEDS REVIEW</span></div>'
                      : '<div class="sh"><span class="serr">&#10007; NOT FLIGHT READY</span></div>';
  var zh = !s ? _dash
    : !s.zones_checked ? '<span class="swrn">not checked</span>'
    : s.zones_clear    ? '<span class="sok">clear</span>'
                       : '<span class="serr">'+s.zone_count+' zone(s)</span>';
  function fmt1(v) { return s && v != null ? v.toFixed(1) : _dash; }
  function fmt0(v) { return s && v != null ? v.toFixed(0) : _dash; }
  function fmt2(v) { return s && v != null ? v.toFixed(2) : _dash; }
  function fmti(v) { return s && v != null ? String(v)    : _dash; }
  var rh = s ? (s.review_reasons||[]).map(function(r){
    return '<div class="ritem">&#9888; '+r+'</div>';
  }).join('') : '';
  document.getElementById('spcontent').innerHTML =
    sh
   +'<div class="sgrid">'
   +'<div class="sbox"><div class="slbl">Area</div><div class="sval">'+fmt1(s&&s.final_area_ha)+' '+(s?'ha':'')+' </div></div>'
   +'<div class="sbox"><div class="slbl">Height</div><div class="sval">'+fmt0(s&&s.flight_height_m)+' '+(s?'m':'')+' </div></div>'
   +'<div class="sbox"><div class="slbl">GSD</div><div class="sval">'+fmt2(s&&s.target_gsd_cm)+' '+(s?'cm':'')+' </div></div>'
   +'<div class="sbox"><div class="slbl">Vertices</div><div class="sval">'+fmti(s&&s.survey_vertex_count)+'</div></div>'
   +'<div class="sbox"><div class="slbl">Lost</div><div class="sval">'+fmt1(s&&s.area_lost_pct)+' '+(s?'%':'')+' </div></div>'
   +'<div class="sbox"><div class="slbl">Zones</div><div class="sval">'+zh+'</div></div>'
   +'</div>'
   +(rh ? '<div class="rlist">'+rh+'</div>' : '');
}

// ── Polygon editing ───────────────────────────────────────────────────────────
// Enter edit mode on dblclick on the polygon.
// Save edits on dblclick outside the polygon (or on the map background).
// We COPY the survey polygon into editLayers on demand — never share the same
// Leaflet layer object between two FeatureGroups, which causes silent drop.

function toggleEdit() {
  // Called by dblclick on polygon — enter edit mode only
  if (!previewData || !lrs.survey || editMode) return;
  editMode = true;
  map.doubleClickZoom.disable();  // prevent zoom while editing
  editLayers.clearLayers();
  if (lrs.survey) map.removeLayer(lrs.survey);
  var style = {color:'#1d4ed8', weight:2.5, fillColor:'#3b82f6', fillOpacity:.17};
  lrs.survey.eachLayer(function(dp) {
    var clone = L.polygon(dp.getLatLngs(), style);
    editLayers.addLayer(clone);
    if (clone.editing) clone.editing.enable();
  });
  // Midpoint markers are in the DOM now; tag them for diamond CSS
  setTimeout(_patchMidpointIcons, 0);
  // Re-patch after every vertex edit (midpoint promoted, new midpoints added)
  _editVHandler = function() { setTimeout(_patchMidpointIcons, 0); };
  map.on('draw:editvertex', _editVHandler);
  _attachEditListeners();
  document.getElementById('bridge-btn').disabled = false;
}

function saveEdit() {
  // Called by dblclick outside polygon — save and exit edit mode
  if (!editMode) return;
  editMode = false;
  map.doubleClickZoom.enable();
  editedPoly = null;
  editLayers.eachLayer(function(l) {
    if (l.editing && l.editing.enabled()) {
      l.editing.disable();
      if (!editedPoly) {
        editedPoly = layerGeom(l);
        polyModified = true; markDirty();
        document.getElementById('modbadge').style.display = 'block';
      }
    }
  });
  editLayers.clearLayers();
  if (lrs.survey) lrs.survey.addTo(map);
  exitBridgeMode();
  _detachEditListeners();
  if (_editVHandler) { map.off('draw:editvertex', _editVHandler); _editVHandler = null; }
  document.getElementById('bridge-btn').disabled = true;
}

// Dblclick on map background (not on polygon) saves the edit
map.on('dblclick', function(e) {
  if (editMode) saveEdit();
});

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    if (_bridgeMode) exitBridgeMode();
    else if (editMode) saveEdit();
  }
});

// ── Edit-mode container listeners (capture phase, bypass Leaflet.draw) ────────
// Registered on toggleEdit, removed on saveEdit / _detachEditListeners.
// Capture phase fires before Leaflet.draw can intercept, letting us intercept
// right-click for bridge entry and left-click for vertex picking in bridge mode.

function _attachEditListeners() {
  _detachEditListeners();

  _editCHandler = function(e) {
    // Right-click in edit mode: enter bridge (snapping to nearest vertex)
    // or cancel if already in bridge mode.
    e.preventDefault(); e.stopPropagation();
    if (_bridgeMode) { exitBridgeMode(); return; }
    var latlng = map.mouseEventToLatLng(e);
    var verts = _collectVerts(_currentSurveyGeom());
    var mp = map.latLngToContainerPoint(latlng);
    var best = null, bestD = 28;
    verts.forEach(function(v) {
      var vp = map.latLngToContainerPoint(L.latLng(v.coord[1], v.coord[0]));
      var d = Math.sqrt(Math.pow(vp.x-mp.x,2) + Math.pow(vp.y-mp.y,2));
      if (d < bestD) { bestD = d; best = v; }
    });
    if (best) _enterBridgeModeWithVertex(best);
  };

  _editKHandler = function(e) {
    // Left-click in bridge mode: pick a vertex.
    // When NOT in bridge mode, do nothing so normal Leaflet.draw drag works.
    if (!_bridgeMode || e.button !== 0) return;
    e.stopPropagation();
    var latlng = map.mouseEventToLatLng(e);
    var v = _nearestVertex(latlng, 28);
    if (!v) {
      var h = document.getElementById('bridge-hint');
      h.style.color = '#fca5a5';
      setTimeout(function(){ h.style.color = ''; }, 400);
      return;
    }
    var dup = _bridgePts.some(function(p){ return p.coord[0]===v.coord[0]&&p.coord[1]===v.coord[1]; });
    if (dup) return;
    _bridgePts.push(v);
    _highlightBridgeVertex(v);
    _checkAndCommit();
  };

  var c = map.getContainer();
  c.addEventListener('contextmenu', _editCHandler, true);
  c.addEventListener('click',       _editKHandler, true);
}

function _detachEditListeners() {
  var c = map.getContainer();
  if (_editCHandler) { c.removeEventListener('contextmenu', _editCHandler, true); _editCHandler = null; }
  if (_editKHandler) { c.removeEventListener('click',       _editKHandler, true); _editKHandler = null; }
}

// ── Bridge / Cut mode ─────────────────────────────────────────────────────────

// Build an interactive vertex layer for geom. Each dot accepts right-click to
// enter bridge mode (with that vertex pre-selected) or cancel if already active.
// Always non-interactive — bridge/cut interaction is handled via container-level
// capture listeners attached in edit mode (_attachEditListeners).
function _buildVertexLayer(geom) {
  var vg = L.layerGroup();
  var verts = _collectVerts(geom);
  var seen = {};
  verts.forEach(function(v) {
    var key = v.coord[0].toFixed(7)+','+v.coord[1].toFixed(7);
    if (seen[key]) return; seen[key] = true;
    L.circleMarker([v.coord[1], v.coord[0]], {
      radius: 3, color: '#1d4ed8', weight: 1,
      fillColor: '#93c5fd', fillOpacity: 0.9, interactive: false
    }).addTo(vg);
  });
  return vg;
}

function _enterBridgeModeWithVertex(v) {
  enterBridgeMode();   // sets up state, disables box-zoom, refreshes _bridgeVerts
  _bridgePts.push(v);
  _highlightBridgeVertex(v);
  _checkAndCommit();
}

// After each pick: auto-commit when the selection is complete.
// 3 picks all on same polygon → triangle cut.
// 4 picks spanning 2 polygons → quad bridge.
function _checkAndCommit() {
  _updateBridgePreview();
  var unique = _bridgePts.map(function(p){return p.polyIdx;})
                         .filter(function(v,i,a){return a.indexOf(v)===i;});
  if (_bridgePts.length === 3 && unique.length === 1) _commitBridge();
  else if (_bridgePts.length === 4) _commitBridge();
}

function _currentSurveyGeom() {
  return editedPoly || (previewData && previewData.survey) || null;
}

function _collectVerts(geom) {
  var verts = [];
  if (!geom) return verts;
  if (geom.type === 'Polygon') {
    var ring = geom.coordinates[0];
    for (var i = 0; i < ring.length - 1; i++) verts.push({coord: ring[i], polyIdx: 0});
  } else if (geom.type === 'MultiPolygon') {
    geom.coordinates.forEach(function(pc, pi) {
      var ring = pc[0];
      for (var i = 0; i < ring.length - 1; i++) verts.push({coord: ring[i], polyIdx: pi});
    });
  }
  return verts;
}

function _nearestVertex(latlng, snapPx) {
  var mp = map.latLngToContainerPoint(latlng);
  var best = null, bestD = snapPx;
  _bridgeVerts.forEach(function(v) {
    var vp = map.latLngToContainerPoint(L.latLng(v.coord[1], v.coord[0]));
    var d = Math.sqrt(Math.pow(vp.x - mp.x, 2) + Math.pow(vp.y - mp.y, 2));
    if (d < bestD) { bestD = d; best = v; }
  });
  return best;
}

function toggleBridgeMode() {
  if (_bridgeMode) exitBridgeMode();
  else enterBridgeMode();
}

function enterBridgeMode() {
  if (!previewData) return;
  _bridgeMode = true;
  _bridgePts = [];
  _bridgeVerts = _collectVerts(_currentSurveyGeom());
  if (_bridgeGroup) map.removeLayer(_bridgeGroup);
  _bridgeGroup = L.layerGroup().addTo(map);
  map.boxZoom.disable();  // prevent Shift+drag box-zoom during picking
  var btn = document.getElementById('bridge-btn');
  btn.textContent = '✕ Cancel bridge/cut';
  btn.classList.add('active');
  map.getContainer().style.cursor = 'crosshair';
  _updateBridgePreview();
}

// Find the nearest Leaflet.draw vertex handle element to a map container point.
// Excludes midpoint elements (.ld-mid) — only vertex squares.
function _findEditIconAt(cp) {
  var mr = map.getContainer().getBoundingClientRect();
  var best = null, bestD = 30;
  document.querySelectorAll('.leaflet-editing-icon:not(.ld-mid)').forEach(function(el) {
    var r = el.getBoundingClientRect();
    var cx = r.left + r.width / 2 - mr.left;
    var cy = r.top  + r.height / 2 - mr.top;
    var d  = Math.sqrt(Math.pow(cx - cp.x, 2) + Math.pow(cy - cp.y, 2));
    if (d < bestD) { bestD = d; best = el; }
  });
  return best;
}

// Colour the vertex handle nearest to vertex v in the bridge-selection orange.
function _highlightBridgeVertex(v) {
  var cp = map.latLngToContainerPoint(L.latLng(v.coord[1], v.coord[0]));
  var el = _findEditIconAt(cp);
  if (el && _bridgeStyledEls.indexOf(el) === -1) {
    el.style.background  = '#f97316';
    el.style.borderColor = '#c2410c';
    el.style.boxShadow   = '0 0 0 2px #fed7aa';
    _bridgeStyledEls.push(el);
  }
}

// Restore all coloured vertex handles to their default appearance.
function _restoreBridgeVertices() {
  _bridgeStyledEls.forEach(function(el) {
    el.style.background  = '';
    el.style.borderColor = '';
    el.style.boxShadow   = '';
  });
  _bridgeStyledEls = [];
}

function exitBridgeMode() {
  if (!_bridgeMode) return;
  _bridgeMode = false;
  _bridgePts = [];
  _bridgeVerts = [];
  if (_bridgeGroup) { map.removeLayer(_bridgeGroup); _bridgeGroup = null; }
  _restoreBridgeVertices();
  map.boxZoom.enable();
  var hint = document.getElementById('bridge-hint');
  hint.style.display = 'none';
  hint.style.background = '#1e293b';
  hint.style.color = '';
  var btn = document.getElementById('bridge-btn');
  btn.textContent = '⬡ Bridge / Cut';
  btn.classList.remove('active');
  map.getContainer().style.cursor = '';
}

function _updateBridgePreview() {
  if (!_bridgeGroup) return;
  _bridgeGroup.clearLayers();
  _bridgePts.forEach(function(p) {
    L.circleMarker([p.coord[1], p.coord[0]], {
      radius: 6, color: '#f97316', weight: 2.5,
      fillColor: '#fb923c', fillOpacity: 1, interactive: false
    }).addTo(_bridgeGroup);
  });
  if (_bridgePts.length >= 2) {
    var lls = _bridgePts.map(function(p){ return [p.coord[1], p.coord[0]]; });
    var unique = _bridgePts.map(function(p){return p.polyIdx;})
                           .filter(function(v,i,a){return a.indexOf(v)===i;});
    var willClose = (_bridgePts.length === 3 && unique.length === 1) || _bridgePts.length >= 4;
    if (willClose) lls.push(lls[0]);  // close the preview shape
    L.polyline(lls, {color:'#f97316', weight:2, dashArray:'5 4', interactive:false}).addTo(_bridgeGroup);
  }
  var n = _bridgePts.length;
  var u = _bridgePts.map(function(p){return p.polyIdx;}).filter(function(v,i,a){return a.indexOf(v)===i;});
  var allSame = u.length <= 1;
  var hintText = n === 0 ? 'Right-click a vertex to start — Esc to cancel'
    : n === 1 ? 'Vertex 1 — pick 2 more to cut triangle, or cross to bridge'
    : n === 2 && allSame  ? 'Vertex 2/3 — pick 1 more to cut triangle, or cross to bridge'
    : n === 2 && !allSame ? 'Vertex 2/4 — pick 2 more to bridge'
    : n === 3 && allSame  ? 'Cutting triangle…'
    : n === 3 && !allSame ? 'Vertex 3/4 — pick 1 more to bridge'
    : 'Bridging…';
  var hint = document.getElementById('bridge-hint');
  hint.style.display = 'block';
  hint.textContent = hintText;
}

function _showBridgeError(msg) {
  var hint = document.getElementById('bridge-hint');
  hint.style.display = 'block';
  hint.style.background = '#dc2626';
  hint.textContent = '✕ ' + msg;
  setTimeout(function(){ hint.style.display = 'none'; hint.style.background = '#1e293b'; }, 3500);
}

async function _commitBridge() {
  var geom = _currentSurveyGeom();
  if (!geom) { exitBridgeMode(); return; }

  var indices = _bridgePts.map(function(p){ return p.polyIdx; });
  var unique = indices.filter(function(v,i,a){ return a.indexOf(v)===i; });
  var op = unique.length === 1 ? 'subtract' : 'bridge';

  _updateBridgePreview();  // show "Processing…"

  try {
    var res = await fetch('/api/polygon_op', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        operation: op,
        polygon: geom,
        points: _bridgePts.map(function(p){ return p.coord; })
      })
    });
    if (!res.ok) {
      var err = await res.json().catch(function(){ return {detail:'Server error'}; });
      exitBridgeMode();
      _showBridgeError(err.detail || 'Operation failed');
      return;
    }
    var data = await res.json();
    exitBridgeMode();
    // Exit edit mode — editLayers has stale geometry, result replaces it
    if (editMode) {
      editMode = false;
      map.doubleClickZoom.enable();
      editLayers.clearLayers();
      document.getElementById('bridge-btn').disabled = true;
    }
    _detachEditListeners();
    editedPoly = data.geometry;
    polyModified = true; markDirty();
    document.getElementById('modbadge').style.display = 'block';
    document.getElementById('rstbtn').disabled = false;
    _updateSurveyDisplay(data.geometry);
  } catch(e) {
    exitBridgeMode();
    _showBridgeError('Network error: ' + e.message);
  }
}

function _updateSurveyDisplay(geom) {
  if (lrs.survey) { map.removeLayer(lrs.survey); lrs.survey = null; }
  if (lrs.vertices) { map.removeLayer(lrs.vertices); lrs.vertices = null; }

  var surveyStyle = {color:'#1d4ed8', weight:2.5, fillColor:'#3b82f6', fillOpacity:.17};
  var polys = geomToPolys(geom, surveyStyle);
  if (polys.length) {
    lrs.survey = L.featureGroup(polys).addTo(map);
    lrs.survey.eachLayer(function(l) {
      l.on('dblclick', function(e) { L.DomEvent.stop(e); if (!editMode && !_bridgeMode) toggleEdit(); });
    });
  }

  lrs.vertices = _buildVertexLayer(geom).addTo(map);
}

function resetPoly() {
  if (!previewData) return;
  saveEdit();  // exit edit mode cleanly if active
  clearPolyEdit();
  // Cancel any pending auto-update so the stale simplify value doesn't fire after reset
  if (_autoTimer) { clearTimeout(_autoTimer); _autoTimer = null; }
  // Reset polygon controls to neutral: no offset, no simplification
  document.getElementById('offset').value = 0;
  setSimpManual(0, true);  // silent=true so it doesn't trigger scheduleAutoUpdate
  // If a job is already running, defer the preview until it finishes
  if (isRunning) { _pendingPreview = true; } else { startPreview(); }
}

// ── Save result ───────────────────────────────────────────────────────────────
function onSaveDone(payload) {
  console.log('[save done]', payload);
  document.getElementById('xb').disabled = false;
  _activeJob = payload.job_name || null;
  _dirty = false;
  if (payload.stats) renderStatus(payload.stats);
  // Open the panel (first save reveals it) then refresh the job cards
  setJpOpen(true);
  loadJobsList();
}

// ── Leaflet.draw midpoint diamond styling ─────────────────────────────────────
// Vertex and midpoint handles share the same CSS class.  Leaflet.draw sets
// opacity 0.6 on midpoint marker elements and leaves vertex elements at the
// default (no inline opacity style).  After editing.enable() we query the DOM
// and add .ld-mid to midpoint elements so CSS can target them distinctly.
function _patchMidpointIcons() {
  // Called on edit-mode enter and after every draw:editvertex event so that
  // promoted midpoints lose the class and newly created midpoints gain it.
  var all = document.querySelectorAll('.leaflet-editing-icon');
  // Clear first — handles the case where a midpoint was just promoted to vertex
  all.forEach(function(el) { el.classList.remove('ld-mid'); });
  // Re-tag midpoints: Leaflet.draw sets opacity:0.6 inline on midpoint elements;
  // vertex handles have no inline opacity (or opacity:1).
  var found = 0;
  all.forEach(function(el) {
    var op = parseFloat(el.style.opacity);
    if (!isNaN(op) && op < 1) { el.classList.add('ld-mid'); found++; }
  });
  // Fallback: check parent element opacity
  if (!found) {
    all.forEach(function(el) {
      var op = el.parentElement && parseFloat(el.parentElement.style.opacity);
      if (op && op < 1) { el.classList.add('ld-mid'); }
    });
  }
}

// ── Dirty tracking ────────────────────────────────────────────────────────────
function markDirty() { _dirty = true; }

function confirmIfDirty(onConfirm) {
  if (!_dirty) { onConfirm(); return; }
  document.getElementById('confirm-msg').textContent =
    'You have unsaved changes. Discard them and continue?';
  document.getElementById('confirm-modal').style.display = 'flex';
  document.getElementById('confirm-discard').onclick = function() {
    hideConfirmModal(); _dirty = false; onConfirm();
  };
}
function hideConfirmModal() {
  document.getElementById('confirm-modal').style.display = 'none';
}
window.addEventListener('beforeunload', function(e) {
  if (_dirty) { e.preventDefault(); e.returnValue = ''; }
});

// ── Jobs panel ────────────────────────────────────────────────────────────────
function setJpOpen(open) {
  _jpOpen = open;
  localStorage.setItem('jp-open', open ? 'true' : 'false');
  document.getElementById('jp').classList.toggle('closed', !open);
  document.getElementById('jp-tog').innerHTML = open ? '&#9664;' : '&#9654;';
  document.getElementById('jp-tog').title = open ? 'Hide jobs panel' : 'Show jobs panel';
}
function toggleJp() { setJpOpen(!_jpOpen); }

document.getElementById('jp-filter').addEventListener('input', function() {
  renderJobsList(_jobsCache);
});

async function loadJobsList() {
  try {
    var r = await fetch('/api/jobs');
    if (!r.ok) return;
    var data = await r.json();
    _jobsCache = data.jobs || [];
    // Auto-open panel on first ever load if jobs exist
    if (_jobsCache.length > 0 && localStorage.getItem('jp-open') === null) {
      setJpOpen(true);
    }
    renderJobsList(_jobsCache);
    // Highlight active job card
    if (_activeJob) {
      document.querySelectorAll('.jcard').forEach(function(c){
        c.classList.toggle('active', c.dataset.name === _activeJob);
      });
    }
  } catch(e) { console.error('[loadJobsList]', e); }
}

function renderJobsList(jobs) {
  var list = document.getElementById('jp-list');
  var filter = (document.getElementById('jp-filter').value || '').toLowerCase();
  list.innerHTML = '';
  var filtered = jobs.filter(function(j){ return !filter || j.name.toLowerCase().includes(filter); });
  if (!filtered.length) {
    list.innerHTML = '<div style="padding:16px 8px;color:#475569;font-size:11px;text-align:center">'
      + (filter ? 'No matches' : 'No saved jobs yet') + '</div>';
    return;
  }
  filtered.forEach(function(j) { list.appendChild(buildJobCard(j)); });
}

function buildJobCard(j) {
  var card = document.createElement('div');
  card.className = 'jcard' + (j.name === _activeJob ? ' active' : '') + (j.status === 'failed' ? ' failed' : '');
  card.dataset.name = j.name;
  var date = j.saved_at || j.run_at || '';
  var dateStr = date ? new Date(date).toLocaleString('fi-FI',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '—';
  var meta = [dateStr, j.area_ha != null ? j.area_ha.toFixed(1)+' ha' : '', j.drone||''].filter(Boolean).join(' · ');
  var badge = j.status === 'failed' ? '<span class="jbadge fail">!</span>'
    : j.flight_ready === true  ? '<span class="jbadge ok">&#10003;</span>'
    : j.needs_review === true  ? '<span class="jbadge wrn">!</span>'
    : '';
  var thumb = j.thumbnail_svg || '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" fill="#1e293b"/><text x="32" y="40" text-anchor="middle" font-size="28" fill="#334155">?</text></svg>';
  card.innerHTML =
    '<div class="jcard-thumb">' + thumb + '</div>'
    + '<div class="jcard-body">'
    +   '<div class="jcard-name">' + escHtml(j.name) + '</div>'
    +   '<div class="jcard-meta">' + escHtml(meta) + '</div>'
    + '</div>'
    + '<div class="jcard-right">' + badge
    +   '<button class="jcard-menu-btn" title="Actions" onclick="toggleCardMenu(event,\'' + escHtml(j.name) + '\',\'' + j.status + '\')">&#8942;</button>'
    + '</div>';
  if (j.status !== 'failed') {
    card.addEventListener('click', function(e) {
      if (e.target.closest('.jcard-menu-btn') || e.target.closest('.jmenu')) return;
      openJob(j.name);
    });
  }
  return card;
}

// ── Card menu ─────────────────────────────────────────────────────────────────
var _openMenu = null;
function toggleCardMenu(e, name, status) {
  e.stopPropagation();
  closeCardMenu();
  var btn = e.currentTarget;
  var menu = document.createElement('div');
  menu.className = 'jmenu';
  var items = status === 'failed'
    ? [['Delete', function(){ confirmDeleteJob(name); }]]
    : [
        ['Open',            function(){ openJob(name); }],
        ['Show folder',     function(){ revealJob(name); }],
        ['Clone',           function(){ cloneJob(name); }],
        ['Rename',          function(){ startRename(name); }],
        ['Delete',          function(){ confirmDeleteJob(name); }],
      ];
  items.forEach(function(it) {
    var mi = document.createElement('button');
    mi.className = 'jmenu-item' + (it[0] === 'Delete' ? ' danger' : '');
    mi.textContent = it[0];
    mi.addEventListener('click', function(ev) { ev.stopPropagation(); closeCardMenu(); it[1](); });
    menu.appendChild(mi);
  });
  // Position relative to the card's right area
  btn.closest('.jcard-right').appendChild(menu);
  _openMenu = menu;
  setTimeout(function() { document.addEventListener('click', closeCardMenu, {once:true}); }, 0);
}
function closeCardMenu() {
  if (_openMenu) { _openMenu.remove(); _openMenu = null; }
}

// ── Open job ──────────────────────────────────────────────────────────────────
function openJob(name) {
  if (isRunning) return;
  confirmIfDirty(function() { _doOpenJob(name); });
}
async function _doOpenJob(name) {
  try {
    var r = await fetch('/api/jobs/' + encodeURIComponent(name));
    if (!r.ok) { showError('Could not load job: HTTP ' + r.status); return; }
    var data = await r.json();
    var p = data.params;
    // Cancel any pending timer
    if (_autoTimer) { clearTimeout(_autoTimer); _autoTimer = null; }
    // Clear map first
    Object.values(lrs).forEach(function(l){ if(l) map.removeLayer(l); });
    lrs = {dsm:null, survey:null, vertices:null, rings:null, areas:null, bldgs:null, ko:null, zones:null};
    editLayers.clearLayers();
    editMode = false; _detachEditListeners();
    // Restore form
    _restoreFormFromParams(p);
    document.getElementById('jname').value = name;
    updatePathHint();
    _activeJob = name;
    _dirty = false;
    clearError();
    // Highlight card
    document.querySelectorAll('.jcard').forEach(function(c){ c.classList.toggle('active', c.dataset.name === name); });
    // Restore map from stored preview (instant)
    if (p && p.last_preview_geojson) {
      previewData = p.last_preview_geojson;
      _lastPreviewedIds = ((p.inputs && p.inputs.parcel_ids)||[]).join(',')
        + '||' + ((p.inputs && p.inputs.property_ids)||[]).join(',');
      try {
        renderMap(previewData);
        redrawRings();
        resetLegend();
        renderStatus(previewData.stats);
        document.getElementById('xb').disabled = false;
        document.getElementById('rstbtn').disabled = false;
        document.getElementById('bridge-btn').disabled = true;
      } catch(ex) { console.error('[openJob] render error', ex); }
    } else {
      previewData = null;
      document.getElementById('xb').disabled = true;
      document.getElementById('rstbtn').disabled = true;
      document.getElementById('bridge-btn').disabled = true;
      renderStatus(null);
      document.getElementById('legend').classList.add('inactive');
      map.setView([64.5, 26.0], 5);
      focusArea();
    }
    // Cache staleness notice
    if (data.cache_stale && data.cache_stale.length) showStaleNotice(data.cache_stale);
    else hideStaleNotice();
    // Auto-run preview for fresh data (and DSM overlay)
    startPreview();
  } catch(ex) { showError('Failed to open job: ' + ex.message); }
}

function _restoreFormFromParams(p) {
  if (!p) return;
  if (p.inputs) {
    document.getElementById('pids').value = (p.inputs.parcel_ids||[]).join('\n');
    document.getElementById('kids').value = (p.inputs.property_ids||[]).join('\n');
  }
  if (p.flight) {
    if (p.flight.drone) document.getElementById('dsel').value = p.flight.drone;
    if (p.flight.height_m != null) {
      document.getElementById('hgt').value = p.flight.height_m;
      updateGsd();
    }
    if (p.flight.subcategory) setSub(p.flight.subcategory, true);
  }
  if (p.polygon) {
    if (p.polygon.offset_m != null) document.getElementById('offset').value = p.polygon.offset_m;
    if (p.polygon.simplify === 'auto') setSimpAuto(true);
    else if (p.polygon.simplify != null) setSimpManual(parseFloat(p.polygon.simplify)||0, true);
    if (p.polygon.keepout != null) document.getElementById('kochk').checked = p.polygon.keepout;
  }
  if (p.safety && p.safety.preview_radius_m != null) {
    document.getElementById('warn-radius').value = p.safety.preview_radius_m;
    setRadiusLinked(false);
  } else {
    setRadiusLinked(true);
  }
  if (p.custom_polygon_4326) {
    editedPoly = p.custom_polygon_4326; polyModified = true;
    document.getElementById('modbadge').style.display = 'block';
  } else {
    editedPoly = null; polyModified = false;
    document.getElementById('modbadge').style.display = 'none';
  }
}

// ── Reveal in file manager ────────────────────────────────────────────────────
async function revealJob(name) {
  try {
    var r = await fetch('/api/jobs/' + encodeURIComponent(name) + '/reveal', {method:'POST'});
    if (!r.ok) {
      var e = await r.json().catch(function(){return{detail:'HTTP '+r.status};});
      showError(e.detail || 'Could not open folder');
    }
  } catch(e) { showError('Could not open folder: ' + e.message); }
}

// ── Clone ─────────────────────────────────────────────────────────────────────
async function cloneJob(name) {
  if (isRunning) return;
  try {
    var r = await fetch('/api/jobs/' + encodeURIComponent(name) + '/clone', {method:'POST'});
    if (!r.ok) {
      var e = await r.json().catch(function(){return{detail:'HTTP '+r.status};});
      showError(e.detail || 'Clone failed'); return;
    }
    var data = await r.json();
    await loadJobsList();
    openJob(data.name);
  } catch(e) { showError('Clone failed: ' + e.message); }
}

// ── Delete ────────────────────────────────────────────────────────────────────
function confirmDeleteJob(name) {
  var card = document.querySelector('.jcard[data-name="' + CSS.escape(name) + '"]');
  if (!card) return;
  card.innerHTML =
    '<div style="padding:6px 10px;font-size:11px;color:#fca5a5;flex:1">Delete <b>' + escHtml(name) + '</b>?</div>'
    + '<div style="display:flex;gap:4px;padding:6px 8px;flex-shrink:0">'
    + '<button class="jcard-del-yes" onclick="deleteJob(\'' + escHtml(name).replace(/'/g,"\\'") + '\')">Delete</button>'
    + '<button class="jcard-del-no" onclick="loadJobsList()">Cancel</button>'
    + '</div>';
  card.style.alignItems = 'center';
}
async function deleteJob(name) {
  try {
    var r = await fetch('/api/jobs/' + encodeURIComponent(name), {method:'DELETE'});
    if (!r.ok) {
      var e = await r.json().catch(function(){return{detail:'HTTP '+r.status};});
      showError(e.detail || 'Delete failed'); return;
    }
    if (_activeJob === name) { _activeJob = null; _dirty = false; _doNewJob(); }
    await loadJobsList();
  } catch(e) { showError('Delete failed: ' + e.message); }
}

// ── Rename ────────────────────────────────────────────────────────────────────
function startRename(name) {
  var card = document.querySelector('.jcard[data-name="' + CSS.escape(name) + '"]');
  if (!card) return;
  var nameEl = card.querySelector('.jcard-name');
  if (!nameEl) return;
  var input = document.createElement('input');
  input.className = 'jcard-rename-input';
  input.value = name;
  nameEl.replaceWith(input);
  input.focus(); input.select();
  var committed = false;
  function commit() {
    if (committed) return; committed = true;
    var newName = input.value.trim();
    if (!newName || newName === name) { loadJobsList(); return; }
    doRename(name, newName);
  }
  input.addEventListener('blur', commit);
  input.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') { e.preventDefault(); commit(); }
    if (e.key === 'Escape') { committed = true; loadJobsList(); }
  });
}
async function doRename(oldName, newName) {
  try {
    var r = await fetch('/api/jobs/' + encodeURIComponent(oldName), {
      method:'PATCH', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({new_name: newName})
    });
    if (!r.ok) {
      var e = await r.json().catch(function(){return{detail:'HTTP '+r.status};});
      showError(e.detail || 'Rename failed'); await loadJobsList(); return;
    }
    if (_activeJob === oldName) {
      _activeJob = newName;
      document.getElementById('jname').value = newName;
      updatePathHint();
    }
    await loadJobsList();
  } catch(e) { showError('Rename failed: ' + e.message); await loadJobsList(); }
}

// ── Staleness notice ──────────────────────────────────────────────────────────
function showStaleNotice(stale) {
  var el = document.getElementById('stale-notice');
  el.textContent = 'Cached tiles may be stale (' + stale.length + ' missing) — preview will re-fetch.';
  el.style.display = 'block';
}
function hideStaleNotice() {
  var el = document.getElementById('stale-notice');
  el.style.display = 'none'; el.textContent = '';
}

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

init();
</script>
</body>
</html>
"""
