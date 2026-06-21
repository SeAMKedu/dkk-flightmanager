"""Job management routes: list, get, rename, clone, move, delete, reveal,
merge, polygon_op, and folder create/delete.

Note: /api/jobs/geojson must be registered BEFORE /api/jobs/{path:path} so
FastAPI matches the literal path first.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response
from flightmanager.web._server_state import SSEResponse
from pydantic import BaseModel

import flightmanager.web._server_state as _st
from flightmanager.storage.job_store import (
    best_polygon,
    card_polygon,
    check_cache_staleness,
    JobRenameError,
    is_folder_dir,
    load_params,
    params_from_manifest,
    read_job_card,
    refresh_status,
    rename_job_dir,
    resolve_folder_dir,
    resolve_job_dir,
    route_rename_name,
    safe_path_segment,
    save_params,
    scan_jobs,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# SSE event stream
# ---------------------------------------------------------------------------


@router.get("/api/events")
async def job_events():
    """SSE stream for job store changes.

    Clients receive ``{"type":"jobs_changed","paths":[...]}`` when the watcher
    detects writes to job_params.json, manifest.json or .dkk-folder. A keepalive
    comment is sent every 30 s so proxies don't close the connection.
    """
    import asyncio

    queue: asyncio.Queue = asyncio.Queue(maxsize=20)
    _st.event_queues.add(queue)

    async def generate():
        try:
            yield 'data: {"type":"connected"}\n\n'
            while True:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=30.0)
                    if data is None:  # shutdown sentinel — exit cleanly
                        return
                    yield f"data: {data}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            _st.event_queues.discard(queue)

    return SSEResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class PolygonOpRequest(BaseModel):
    operation: str  # "bridge" | "subtract"
    polygon: dict  # GeoJSON Polygon or MultiPolygon (current survey)
    points: list  # 3 or 4 [lng, lat] coordinates


class SplitRequest(BaseModel):
    polygon_a: dict  # GeoJSON for the modified existing job
    polygon_b: dict  # GeoJSON for the new sibling job


class ExportKmlRequest(BaseModel):
    paths: list[str]


class MergeRequest(BaseModel):
    job_paths: list[str]
    new_name: str
    folder: str | None = None
    delete_sources: bool = False


# ---------------------------------------------------------------------------
# Jobs list / geojson
# ---------------------------------------------------------------------------


@router.get("/api/jobs")
async def list_jobs():
    output_dir = Path(_st.config.output.output_dir).resolve()
    return {"groups": scan_jobs(output_dir)}


@router.get("/api/jobs/geojson")
async def jobs_geojson(folder: str | None = None):
    """Return all jobs as a GeoJSON FeatureCollection for the map view."""
    output_dir = Path(_st.config.output.output_dir).resolve()
    groups = scan_jobs(output_dir, with_polygon=True)
    features = []
    for group in groups:
        if folder is not None and group["name"] != folder:
            continue
        for card in group["jobs"]:
            features.append(
                {
                    "type": "Feature",
                    "geometry": card.get("_geometry"),
                    "properties": {
                        "path": card["path"],
                        "name": card["name"],
                        "folder": card["folder"],
                        "color": card["color"],
                        "untouched": card["untouched"],
                        "flight_ready": card.get("flight_ready"),
                        "needs_review": card.get("needs_review"),
                        "area_ha": card.get("area_ha"),
                        "original_area_ha": card.get("original_area_ha"),
                        "area_lost_pct": card.get("area_lost_pct"),
                        "subcategory": card.get("subcategory"),
                        "height_m": card.get("height_m"),
                        "waypoint_mode": card.get("waypoint_mode", False),
                        "adv_min_height_m": card.get("adv_min_height_m"),
                        "adv_max_height_m": card.get("adv_max_height_m"),
                        "battery_count": card.get("battery_count"),
                        "strip_speed_ms": card.get("strip_speed_ms"),
                        "flight_time_min": card.get("flight_time_min"),
                        "photo_count": card.get("photo_count"),
                        "over_one_battery": card.get("over_one_battery"),
                        "drone": card.get("drone"),
                        "status": card.get("status", "ok"),
                        "sort_order": card.get("sort_order"),
                        "takeoff_point_4326": card.get("takeoff_point_4326"),
                        "skipped": card.get("skipped", False),
                    },
                }
            )
    return {"type": "FeatureCollection", "features": features}


@router.get("/api/launch_sites")
async def launch_sites(folder: str | None = None):
    """Group a folder's jobs into launch sites for flight announcements.

    A launch site = a run of consecutive-flight-order jobs flown from one parking
    spot (takeoffs within ~50 m). Each carries the takeoff-centroid dot plus the
    smallest enclosing circle (centre + radius) over its survey polygons — the
    operating area you announce on Flyk. Registered before ``/api/jobs/{path:path}``.
    """
    from flightmanager.forecasting.launch_sites import cluster_jobs

    output_dir = Path(_st.config.output.output_dir).resolve()
    cards: list[dict] = []
    for group in scan_jobs(output_dir, with_polygon=True):
        if folder is not None and group["name"] != folder:
            continue
        if folder is None and group["name"] is not None:
            continue
        cards.extend(group["jobs"])

    sites = cluster_jobs(cards)
    return {"sites": [s.to_dict() for s in sites]}


# ── PDF report ────────────────────────────────────────────────────────────────


def _load_job_entry(output_dir: Path, path: str) -> dict | None:
    """Return ``{"params", "manifest"}`` for a job path (manifest reconstructed
    for CLI-only jobs). None if the job dir is missing."""
    folder, name, job_dir = resolve_job_dir(output_dir, path)
    if not job_dir.is_dir():
        return None
    params = load_params(job_dir)
    manifest: dict = {}
    mp = job_dir / "manifest.json"
    if mp.exists():
        try:
            manifest = json.loads(mp.read_text(encoding="utf-8"))
        except Exception:
            manifest = {}
    if params is None:
        params = params_from_manifest(name, manifest) if manifest else {}
    params.setdefault("job_name", name)
    params["folder"] = folder
    params["path"] = path
    return {"params": params, "manifest": manifest}


@router.get("/api/jobs/{path:path}/report.pdf")
async def job_report(path: str, basemap: str = "mml"):
    """One-page PDF flight card for a job. Registered before ``/api/jobs/{path:path}``."""
    import asyncio

    from flightmanager.reporting import report

    output_dir = Path(_st.config.output.output_dir).resolve()
    entry = _load_job_entry(output_dir, path)
    if entry is None:
        raise HTTPException(404, detail=f"Job '{path}' not found")
    pdf = await asyncio.to_thread(
        report.render_job_report,
        _st.config,
        entry["params"],
        entry["manifest"],
        basemap=basemap,
    )
    fname = (entry["params"].get("job_name") or "job") + ".pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{fname}"'},
    )


# Async report jobs: { job_id: {"queue", "result": bytes|None, "filename", "error"} }.
# A report draws many maps (overview + per-site + per-job), so generation is run in
# a thread and streams progress over SSE; the finished PDF is fetched separately.
_report_jobs: dict = {}


@router.post("/api/report/start")
async def report_start(body: dict):
    """Start a PDF report job. One path -> flight card, several -> mission packet.

    Body: ``{paths: [...], folder?, basemap?, include_job_cards?}`` -> ``{job_id}``.
    Stream progress via ``GET /api/report/progress/{job_id}``, then fetch the PDF
    from ``GET /api/report/result/{job_id}``.
    """
    import asyncio
    import uuid

    from flightmanager.reporting import report

    paths = body.get("paths") or []
    if not paths:
        raise HTTPException(400, detail="No job paths given")
    output_dir = Path(_st.config.output.output_dir).resolve()
    entries = [e for p in paths if (e := _load_job_entry(output_dir, p))]
    if not entries:
        raise HTTPException(404, detail="No jobs found for the given paths")

    basemap = body.get("basemap", "mml")
    folder = body.get("folder")
    job_id = uuid.uuid4().hex
    queue: asyncio.Queue = asyncio.Queue()
    _report_jobs[job_id] = {
        "queue": queue,
        "result": None,
        "error": None,
        "filename": None,
    }
    loop = asyncio.get_running_loop()

    def progress_cb(stage, msg, pct):
        loop.call_soon_threadsafe(
            queue.put_nowait, {"stage": stage, "msg": msg, "pct": pct}
        )

    async def run():
        try:
            if len(entries) == 1 and not folder:
                pdf = await asyncio.to_thread(
                    report.render_job_report,
                    _st.config,
                    entries[0]["params"],
                    entries[0]["manifest"],
                    basemap=basemap,
                    progress_cb=progress_cb,
                )
                fname = (entries[0]["params"].get("job_name") or "job") + ".pdf"
            else:
                pdf = await asyncio.to_thread(
                    report.render_packet,
                    _st.config,
                    entries,
                    folder=folder,
                    basemap=basemap,
                    include_job_cards=body.get("include_job_cards", True),
                    progress_cb=progress_cb,
                )
                fname = (folder or "jobs") + ".pdf"
            _report_jobs[job_id]["result"] = pdf
            _report_jobs[job_id]["filename"] = fname
            queue.put_nowait({"stage": "done", "msg": "Ready", "pct": 100})
        except Exception as e:  # noqa: BLE001
            _report_jobs[job_id]["error"] = str(e)
            queue.put_nowait({"stage": "error", "msg": str(e), "pct": 0})

    asyncio.create_task(run())
    return {"job_id": job_id}


@router.get("/api/report/progress/{job_id}")
async def report_progress(job_id: str):
    """SSE progress for a report job (``{stage, msg, pct}``; ends on done/error)."""
    import asyncio

    entry = _report_jobs.get(job_id)
    if not entry:
        raise HTTPException(404, detail="Report job not found")

    async def generate():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(entry["queue"].get(), timeout=25.0)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    if event.get("stage") in ("done", "error"):
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


@router.get("/api/report/result/{job_id}")
async def report_result(job_id: str):
    """Return the finished PDF for a report job (one-shot; frees it afterwards)."""
    entry = _report_jobs.pop(job_id, None)
    if entry is None:
        raise HTTPException(404, detail="Report job not found")
    if entry.get("error"):
        raise HTTPException(500, detail=entry["error"])
    if entry.get("result") is None:
        raise HTTPException(409, detail="Report not finished")
    return Response(
        content=entry["result"],
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{entry["filename"]}"'},
    )


def _resolve_centroids(folder: str | None, paths: str | None):
    """Return ``(centroids, folder_dir)`` for a folder and/or comma-separated paths.

    Centroids are (lat, lon) of each job's best polygon; root jobs when neither given.
    """
    from shapely.geometry import shape

    output_dir = Path(_st.config.output.output_dir).resolve()
    if paths:
        wanted = [p for p in paths.split(",") if p.strip()]
        job_dirs = [resolve_job_dir(output_dir, p)[2] for p in wanted]
    else:
        job_dirs = []
        for group in scan_jobs(output_dir):
            if folder is not None and group["name"] != folder:
                continue
            for card in group["jobs"]:
                job_dirs.append(resolve_job_dir(output_dir, card["path"])[2])
    folder_dir = resolve_folder_dir(output_dir, folder)

    centroids: list[tuple[float, float]] = []
    for jd in job_dirs:
        geom = best_polygon(jd)
        if geom:
            c = shape(geom).centroid
            centroids.append((c.y, c.x))
    return centroids, folder_dir


@router.get("/api/forecast")
async def forecast(folder: str | None = None, paths: str | None = None):
    """Satellite-overpass + weather day-slots for the map-view bar.

    Scope: a single ``folder`` (its jobs) and/or an explicit comma-separated
    ``paths`` list. Falls back to root-level jobs when neither is given.
    """
    import asyncio

    from flightmanager.forecasting.forecast import build_forecast

    centroids, folder_dir = _resolve_centroids(folder, paths)
    return await asyncio.to_thread(
        build_forecast,
        centroids,
        _st.config.satellites,
        _st.config.weather,
        _st.config.cache.cache_dir,
        folder_dir=folder_dir,
    )


@router.get("/api/refresh/scan")
async def refresh_scan(folder: str | None = None):
    """List exported jobs that should be recomputed (stale pipeline / newer source data).

    Cheap detection only — no recompute. Skips untouched batch skeletons (nothing built
    yet). Registered before ``/api/jobs/{path:path}``.
    """
    from flightmanager.storage.manifest import PIPELINE_VERSION

    output_dir = Path(_st.config.output.output_dir).resolve()
    stale: list[dict] = []
    for group in scan_jobs(output_dir):
        if folder is not None and group["name"] != folder:
            continue
        for card in group["jobs"]:
            if card.get("untouched"):
                continue
            _, _, job_dir = resolve_job_dir(output_dir, card["path"])
            manifest_path = job_dir / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            status = refresh_status(manifest, _st.config.cache, PIPELINE_VERSION)
            if status["needs_refresh"]:
                stale.append(
                    {
                        "path": card["path"],
                        "name": card["name"],
                        "folder": card["folder"],
                        "reasons": status["reasons"],
                        "missing_tiles": status["missing_tiles"],
                    }
                )
    return {"pipeline_version": PIPELINE_VERSION, "stale": stale}


@router.get("/api/mgrs_tiles")
async def mgrs_tiles(folder: str | None = None, paths: str | None = None):
    """MGRS tiles the jobs fall in plus their neighbours, for the 'MGRS tiles' stat
    view. Grid-only (no weather/orbit network). Each tile: id, geometry, center,
    is_job, job_count."""
    import asyncio

    from flightmanager.forecasting import satellites as sat

    centroids, _ = _resolve_centroids(folder, paths)
    return await asyncio.to_thread(
        sat.tiles_with_neighbors,
        centroids,
        _st.config.satellites,
    )


@router.post("/api/export/kml")
async def export_kml(req: ExportKmlRequest):
    """Build a Google-Earth KML for the selected jobs (survey polygons + takeoffs).

    Replaces the old in-browser KML builder. Jobs are ordered by flight order
    (sort_order first, then name); the survey polygon comes from card_polygon so
    ID-derived jobs (survey_outline only) are included.
    """
    from flightmanager.routing.kml_export import build_jobs_kml

    output_dir = Path(_st.config.output.output_dir).resolve()
    jobs: list[dict] = []
    for path in req.paths:
        _, _, job_dir = resolve_job_dir(output_dir, path)
        params = load_params(job_dir)
        if params is None:
            manifest_path = job_dir / "manifest.json"
            if manifest_path.exists():
                try:
                    params = params_from_manifest(
                        job_dir.name,
                        json.loads(manifest_path.read_text(encoding="utf-8")),
                    )
                except Exception:
                    continue
            else:
                continue
        params.setdefault("job_name", job_dir.name)
        jobs.append(params)

    jobs.sort(
        key=lambda p: (
            0 if p.get("sort_order") is not None else 1,
            p.get("sort_order") or 0,
            p.get("job_name") or "",
        )
    )
    kml = build_jobs_kml(jobs)
    return Response(content=kml, media_type="application/vnd.google-earth.kml+xml")


# ---------------------------------------------------------------------------
# Single-job CRUD
# ---------------------------------------------------------------------------


@router.post("/api/jobs/reorder")
async def reorder_jobs(body: dict):
    """Assign sort_order 0..n-1 to the supplied ordered list of job paths.

    Body: ``{paths: ["folder/a", "folder/b", ...]}``
    Jobs not in the list have their sort_order cleared (set to null).
    All paths must belong to the same folder.
    """
    paths: list[str] = body.get("paths") or []
    if not paths:
        return {"ok": True}
    output_dir = Path(_st.config.output.output_dir).resolve()

    # Derive folder from the first path; all must match
    folder0, _, _ = resolve_job_dir(output_dir, paths[0])
    for p in paths[1:]:
        f, _, _ = resolve_job_dir(output_dir, p)
        if f != folder0:
            raise HTTPException(400, detail="All paths must be in the same folder")

    # Clear sort_order for all sibling jobs, then set new values
    siblings: list[Path] = []
    if folder0:
        parent = output_dir / folder0
    else:
        parent = output_dir
    try:
        siblings = [d for d in parent.iterdir() if d.is_dir()]
    except PermissionError:
        pass

    ordered_set = {p: i for i, p in enumerate(paths)}

    for job_dir in siblings:
        data = load_params(job_dir)
        if data is None:
            continue
        try:
            job_path = f"{folder0}/{job_dir.name}" if folder0 else job_dir.name
            new_so = ordered_set.get(job_path)  # None if not in list
            if data.get("sort_order") != new_so:
                data["sort_order"] = new_so
                save_params(job_dir, data)
        except Exception:
            pass

    return {"ok": True}


@router.post("/api/jobs/route_rename")
async def route_rename(body: dict):  # noqa: C901
    """Rename an ordered list of route jobs to ``YYYYMMDD-NN-base`` flight names.

    Body: ``{paths: ["folder/a", "folder/b", ...], date?: "YYYYMMDD"}``.
    Paths are taken in the supplied (flight) order; index ``NN`` is 1-based.
    Any existing route prefix on a name is stripped first, so re-running is
    idempotent. All paths must belong to the same folder.

    Renaming runs in two phases (every job to a unique temp name, then to its
    final name) so order swaps that reuse a sibling's current name cannot
    collide mid-way — the failure mode of the old client-side per-job loop.
    """
    paths: list[str] = body.get("paths") or []
    if not paths:
        return {"ok": True, "renamed": []}

    date_str: str = (body.get("date") or "").strip() or datetime.now(
        timezone.utc
    ).strftime("%Y%m%d")
    if not (len(date_str) == 8 and date_str.isdigit()):
        raise HTTPException(400, detail="date must be 'YYYYMMDD'")

    output_dir = Path(_st.config.output.output_dir).resolve()

    folder0, _, _ = resolve_job_dir(output_dir, paths[0])
    total = len(paths)
    plan: list[tuple[Path, str, str]] = []  # (job_dir, old_name, new_name)
    seen: set[str] = set()
    for i, p in enumerate(paths, start=1):
        folder, name, job_dir = resolve_job_dir(output_dir, p)
        if folder != folder0:
            raise HTTPException(400, detail="All paths must be in the same folder")
        if not job_dir.is_dir():
            raise HTTPException(404, detail=f"Job '{p}' not found")
        if p in seen:
            raise HTTPException(400, detail=f"Duplicate path: {p}")
        seen.add(p)
        new_name = route_rename_name(date_str, i, total, name)
        plan.append((job_dir, name, new_name))

    # Reject a target that already exists as a sibling not part of this batch.
    batch_dirs = {job_dir for job_dir, _, _ in plan}
    for job_dir, _old, new_name in plan:
        target = job_dir.parent / new_name
        if target.exists() and target not in batch_dirs:
            raise HTTPException(
                409, detail=f"Target name '{new_name}' already exists in this folder"
            )

    # Phase 1 — move every changing job to a unique temp name.
    staged: dict[int, tuple[Path, str, str]] = {}  # idx -> (tmp_dir, tmp_name, final)
    for idx, (job_dir, old_name, new_name) in enumerate(plan):
        if new_name == old_name:
            continue
        tmp_name = f"__rr_tmp_{idx}__"
        _rename_job(job_dir, old_name, tmp_name, folder0)
        staged[idx] = (job_dir.parent / tmp_name, tmp_name, new_name)

    # Phase 2 — move each temp name to its final flight name; build a result
    # aligned 1:1 with the input paths so callers can remap reliably.
    renamed: list[dict] = []
    for idx, (orig_path, (job_dir, old_name, new_name)) in enumerate(
        zip(paths, plan)
    ):
        if idx in staged:
            tmp_dir, tmp_name, final_name = staged[idx]
            info = _rename_job(tmp_dir, tmp_name, final_name, folder0)
            info["old_path"] = orig_path
            info["changed"] = True
            renamed.append(info)
        else:
            renamed.append(
                {
                    "old_path": orig_path,
                    "path": orig_path,
                    "name": old_name,
                    "folder": folder0,
                    "changed": False,
                }
            )

    return {"ok": True, "date": date_str, "renamed": renamed}


@router.get("/api/jobs/{path:path}")
async def get_job(path: str):
    output_dir = Path(_st.config.output.output_dir).resolve()
    folder, name, job_dir = resolve_job_dir(output_dir, path)
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
        params = load_params(job_dir)
        if params is None:
            raise HTTPException(500, detail="Could not read job_params.json")
    elif manifest:
        params = params_from_manifest(name, manifest)
    else:
        raise HTTPException(404, detail=f"Job '{path}' has no readable data")
    stale: list[str] = []
    if manifest:
        stale = check_cache_staleness(manifest, _st.config.cache)
    return {"params": params, "cache_stale": stale, "folder": folder}


def _rename_job(
    job_dir: Path, old_name: str, new_name: str, folder: str | None
) -> dict:
    """Web wrapper over job_store.rename_job_dir, mapping errors to HTTP codes."""
    try:
        return rename_job_dir(job_dir, old_name, new_name, folder)
    except JobRenameError as exc:
        raise HTTPException(exc.status, detail=str(exc)) from exc


@router.patch("/api/jobs/{path:path}")
async def update_job(path: str, body: dict):
    output_dir = Path(_st.config.output.output_dir).resolve()
    folder, name, job_dir = resolve_job_dir(output_dir, path)
    if not job_dir.is_dir():
        raise HTTPException(404, detail=f"Job '{path}' not found")

    # Simple field update (color, sort_order, skipped — no rename)
    if "new_name" not in body and (
        "color" in body or "sort_order" in body or "skipped" in body
    ):
        data = load_params(job_dir)
        if data is not None:
            try:
                if "color" in body:
                    data["color"] = body["color"]
                if "sort_order" in body:
                    so = body["sort_order"]
                    data["sort_order"] = int(so) if so is not None else None
                if "skipped" in body:
                    data["skipped"] = bool(body["skipped"])
                save_params(job_dir, data)
            except Exception as exc:
                raise HTTPException(500, detail=f"Could not update job: {exc}")
        return {
            "path": path,
            "color": body.get("color"),
            "sort_order": body.get("sort_order"),
            "skipped": body.get("skipped"),
        }

    # Rename
    new_name: str = body.get("new_name", "").strip()
    if not new_name:
        raise HTTPException(400, detail="new_name is required")
    safe_path_segment(new_name)
    if new_name == name:
        return {"path": path, "name": name, "folder": folder}

    return _rename_job(job_dir, name, new_name, folder)


@router.post("/api/jobs/{path:path}/clone")
async def clone_job(path: str):
    output_dir = Path(_st.config.output.output_dir).resolve()
    folder, name, src_dir = resolve_job_dir(output_dir, path)
    if not src_dir.is_dir():
        raise HTTPException(404, detail=f"Job '{path}' not found")
    params_path = src_dir / "job_params.json"
    manifest_path = src_dir / "manifest.json"
    if not params_path.exists() and not manifest_path.exists():
        raise HTTPException(404, detail=f"Job '{path}' has no data to clone")

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
        params = load_params(src_dir)
        if params is None:
            raise HTTPException(500, detail="Could not read job_params.json")
    else:
        try:
            src_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            params = params_from_manifest(name, src_manifest)
        except Exception as exc:
            raise HTTPException(500, detail=str(exc))

    params["job_name"] = clone_name
    params["saved_at"] = datetime.now(timezone.utc).isoformat()
    save_params(clone_dir, params)
    thumb_src = src_dir / "thumbnail.svg"
    if thumb_src.exists():
        shutil.copy2(thumb_src, clone_dir / "thumbnail.svg")

    clone_path = f"{folder}/{clone_name}" if folder else clone_name
    return {"path": clone_path, "name": clone_name, "folder": folder}


@router.post("/api/jobs/{path:path}/split")
async def split_job(path: str, req: SplitRequest):
    """Split a job into two sibling jobs.

    Updates the existing job's polygon to ``polygon_a`` and creates a new
    sibling job with ``polygon_b``, copying all other params (IDs, flight,
    polygon settings, color).  Returns ``{modified_path, new_path, new_name}``.
    """
    output_dir = Path(_st.config.output.output_dir).resolve()
    folder, name, job_dir = resolve_job_dir(output_dir, path)
    if not job_dir.is_dir():
        raise HTTPException(404, detail=f"Job '{path}' not found")

    params_path = job_dir / "job_params.json"
    if not params_path.exists():
        raise HTTPException(404, detail=f"Job '{path}' has no job_params.json")
    params = load_params(job_dir)
    if params is None:
        raise HTTPException(500, detail="Could not read job_params.json")

    # Derive a unique name for the new job
    parent_dir = job_dir.parent
    base_name = f"{name}-split"
    new_name = base_name
    counter = 2
    while (parent_dir / new_name).exists():
        new_name = f"{base_name}-{counter}"
        counter += 1

    now = datetime.now(timezone.utc).isoformat()

    # Update existing job in place (polygon_a). Clear the stored outline so
    # save_params re-derives it from the new polygon.
    params["custom_polygon_4326"] = req.polygon_a
    params["survey_outline"] = None
    params.pop("last_preview_geojson", None)
    params["saved_at"] = now
    save_params(job_dir, params)

    # Create new sibling job (polygon_b, copy all other params)
    new_params = dict(params)
    new_params["job_name"] = new_name
    new_params["custom_polygon_4326"] = req.polygon_b
    new_params["survey_outline"] = None
    new_params["saved_at"] = now

    new_dir = parent_dir / new_name
    new_dir.mkdir(parents=True, exist_ok=True)
    save_params(new_dir, new_params)

    new_path = f"{folder}/{new_name}" if folder else new_name
    return {"modified_path": path, "new_path": new_path, "new_name": new_name}


@router.post("/api/jobs/{path:path}/move")
async def move_job(path: str, body: dict):
    """Move a job to a different folder (or root if folder is null)."""
    output_dir = Path(_st.config.output.output_dir).resolve()
    folder, name, src_dir = resolve_job_dir(output_dir, path)
    if not src_dir.is_dir():
        raise HTTPException(404, detail=f"Job '{path}' not found")
    to_folder: str | None = body.get("folder") or None
    if to_folder == folder:
        return {"path": path, "folder": folder}
    if to_folder:
        dest_parent = resolve_folder_dir(output_dir, to_folder)
        dest_parent.mkdir(parents=True, exist_ok=True)
        marker = dest_parent / ".dkk-folder"
        if not marker.exists():
            marker.write_text("", encoding="utf-8")
    else:
        dest_parent = output_dir
    dest_dir = dest_parent / name
    if dest_dir.exists():
        raise HTTPException(
            409, detail=f"A job named '{name}' already exists in the target location"
        )
    src_dir.rename(dest_dir)
    if folder:
        src_parent = output_dir / folder
        remaining = [d for d in src_parent.iterdir() if not d.name.startswith(".")]
        if not remaining:
            shutil.rmtree(src_parent)
    new_path = f"{to_folder}/{name}" if to_folder else name
    return {"path": new_path, "folder": to_folder}


@router.delete("/api/jobs/{path:path}")
async def delete_job(path: str):
    output_dir = Path(_st.config.output.output_dir).resolve()
    folder, name, job_dir = resolve_job_dir(output_dir, path)
    if not job_dir.is_dir():
        raise HTTPException(404, detail=f"Job '{path}' not found")
    shutil.rmtree(job_dir)
    if folder:
        parent = output_dir / folder
        if parent.is_dir():
            remaining = [d for d in parent.iterdir() if not d.name.startswith(".")]
            if not remaining:
                shutil.rmtree(parent)
    return {"deleted": path}


@router.post("/api/jobs/{path:path}/reveal")
async def reveal_job(path: str):
    """Open the job folder in the system file manager (desktop-only)."""
    import subprocess
    import sys

    if not _st.config.output.allow_local_fs:
        raise HTTPException(403, detail="Local filesystem actions are disabled")
    output_dir = Path(_st.config.output.output_dir).resolve()
    folder, name, job_dir = resolve_job_dir(output_dir, path)
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


# ---------------------------------------------------------------------------
# Polygon operation
# ---------------------------------------------------------------------------


def _build_operation_polygon(pts: list):
    """Build a valid Shapely polygon from 3 (triangle) or 4 (quad) points.

    Returns the polygon or raises HTTPException(400) on invalid input.
    """
    from shapely.geometry import Polygon as ShapelyPolygon
    from shapely.validation import make_valid

    if len(pts) == 3:
        poly = ShapelyPolygon(pts)
        if not poly.is_valid:
            poly = make_valid(poly)
        return poly
    if len(pts) == 4:
        for order in [
            [pts[0], pts[1], pts[2], pts[3]],
            [pts[0], pts[1], pts[3], pts[2]],
        ]:
            candidate = ShapelyPolygon(order)
            if not candidate.is_valid:
                candidate = make_valid(candidate)
            if candidate.is_valid and not candidate.is_empty and candidate.area > 0:
                return candidate
        raise HTTPException(
            400, detail="Selected points do not form a valid quadrilateral"
        )
    raise HTTPException(400, detail=f"Expected 3 or 4 points, got {len(pts)}")


@router.post("/api/polygon_op")
async def polygon_op(req: PolygonOpRequest):
    from shapely.geometry import mapping, shape
    from shapely.ops import unary_union

    try:
        survey = shape(req.polygon)
        pts = [(c[0], c[1]) for c in req.points]  # lng, lat
        quad = _build_operation_polygon(pts)

        if not quad.is_valid or quad.is_empty:
            raise HTTPException(400, detail="Selected points do not form a valid shape")

        result = (
            unary_union([survey, quad])
            if req.operation == "bridge"
            else survey.difference(quad)
        )

        if result is None or result.is_empty:
            raise HTTPException(400, detail="Operation produced empty geometry")

        return {"geometry": mapping(result)}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, detail=str(exc))


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------


def _unique_ids(id_lists: list[list[str]]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for ids in id_lists:
        for x in ids:
            if x not in seen:
                seen.add(x)
                result.append(x)
    return result


def _merge_by_ids(all_params: list[tuple[Path, dict]], new_name: str) -> dict:
    """Combine parcel/property IDs from all sources into one skeleton job."""
    parcel_ids = _unique_ids(
        [p.get("inputs", {}).get("parcel_ids") or [] for _, p in all_params]
    )
    property_ids = _unique_ids(
        [p.get("inputs", {}).get("property_ids") or [] for _, p in all_params]
    )
    first = all_params[0][1]
    return {
        "job_name": new_name,
        "saved_at": None,
        "inputs": {"parcel_ids": parcel_ids, "property_ids": property_ids},
        "flight": first.get("flight", {}),
        "polygon": first.get(
            "polygon", {"offset_m": 0.0, "simplify": "auto", "keepout": True}
        ),
        "safety": first.get("safety", {"preview_radius_m": None}),
        "custom_polygon_4326": None,
        "batch_created": False,
        "color": None,
        "merge_strategy": "ids",
    }


def _merge_by_polygon(all_params: list[tuple[Path, dict]], new_name: str) -> dict:
    """Union custom polygons from all sources into a single merged job."""
    from shapely.geometry import mapping, shape
    from shapely.ops import unary_union
    from shapely.validation import make_valid

    polys = []
    for job_dir, p in all_params:
        geojson = card_polygon(p)
        if not geojson:
            raise HTTPException(
                400,
                detail=f"Job '{job_dir.name}' has no polygon — run a preview first",
            )
        try:
            geom = shape(geojson)
            if not geom.is_valid:
                geom = make_valid(geom)
            polys.append(geom)
        except Exception as exc:
            raise HTTPException(
                400, detail=f"Invalid geometry for '{job_dir.name}': {exc}"
            )

    merged = unary_union(polys)
    if not merged.is_valid:
        merged = make_valid(merged)
    if merged.is_empty:
        raise HTTPException(400, detail="Union produced empty geometry")

    parcel_ids = _unique_ids(
        [p.get("inputs", {}).get("parcel_ids") or [] for _, p in all_params]
    )
    property_ids = _unique_ids(
        [p.get("inputs", {}).get("property_ids") or [] for _, p in all_params]
    )
    first = all_params[0][1]
    return {
        "job_name": new_name,
        "saved_at": None,
        "inputs": {"parcel_ids": parcel_ids, "property_ids": property_ids},
        "flight": first.get("flight", {}),
        "polygon": first.get(
            "polygon", {"offset_m": 0.0, "simplify": "auto", "keepout": True}
        ),
        "safety": first.get("safety", {"preview_radius_m": None}),
        "custom_polygon_4326": dict(mapping(merged)),
        "batch_created": False,
        "color": None,
        "merge_strategy": "polygon_union",
    }


def _load_job_params(output_dir: Path, job_paths: list[str]) -> list[tuple[Path, dict]]:
    """Load job_params.json for each path. Raises HTTPException on missing/unreadable files."""
    all_params: list[tuple[Path, dict]] = []
    for path in job_paths:
        _, _, job_dir = resolve_job_dir(output_dir, path)
        if not (job_dir / "job_params.json").exists():
            raise HTTPException(404, detail=f"job_params.json not found for '{path}'")
        p = load_params(job_dir)
        if p is None:
            raise HTTPException(500, detail=f"Could not read params for '{path}'")
        all_params.append((job_dir, p))
    return all_params


def _delete_merged_sources(output_dir: Path, job_paths: list[str]) -> None:
    """Delete source job dirs and prune now-empty parent folders after a merge."""
    for path in job_paths:
        _, _, job_dir = resolve_job_dir(output_dir, path)
        if job_dir.is_dir():
            shutil.rmtree(job_dir)
    for path in job_paths:
        parts = path.strip("/").split("/", 1)
        if len(parts) == 2:
            parent = output_dir / parts[0]
            if parent.is_dir():
                remaining = [d for d in parent.iterdir() if not d.name.startswith(".")]
                if not remaining:
                    shutil.rmtree(parent)


@router.post("/api/merge")
async def merge_jobs(req: MergeRequest):
    if len(req.job_paths) < 2:
        raise HTTPException(400, detail="At least two jobs are required to merge")
    new_name = req.new_name.strip()
    if not new_name:
        raise HTTPException(400, detail="new_name is required")
    safe_path_segment(new_name)

    output_dir = Path(_st.config.output.output_dir).resolve()
    all_params = _load_job_params(output_dir, req.job_paths)

    def _is_id_job(job_dir: Path, p: dict) -> bool:
        inputs = p.get("inputs", {})
        has_ids = bool(inputs.get("parcel_ids") or inputs.get("property_ids"))
        return (
            has_ids and p.get("batch_created", False) and not any(job_dir.glob("*.kmz"))
        )

    if all(_is_id_job(d, p) for d, p in all_params):
        merged_params = _merge_by_ids(all_params, new_name)
    else:
        merged_params = _merge_by_polygon(all_params, new_name)

    dest_parent = resolve_folder_dir(output_dir, req.folder)
    dest_parent.mkdir(parents=True, exist_ok=True)
    if req.folder:
        marker = dest_parent / ".dkk-folder"
        if not marker.exists():
            marker.write_text("", encoding="utf-8")

    dest_dir = dest_parent / new_name
    if dest_dir.exists():
        raise HTTPException(
            409, detail=f"A job named '{new_name}' already exists in that location"
        )
    dest_dir.mkdir(parents=True, exist_ok=True)
    save_params(dest_dir, merged_params)

    if req.delete_sources:
        _delete_merged_sources(output_dir, req.job_paths)

    card = read_job_card(dest_dir, folder=req.folder)
    card["merge_strategy"] = merged_params["merge_strategy"]
    return card


# ---------------------------------------------------------------------------
# Folder management
# ---------------------------------------------------------------------------


@router.post("/api/folders")
async def create_folder(body: dict):
    folder_name: str = body.get("name", "").strip()
    if not folder_name or "/" in folder_name or folder_name.startswith("."):
        raise HTTPException(400, detail="Invalid folder name")
    output_dir = Path(_st.config.output.output_dir).resolve()
    folder_dir = resolve_folder_dir(output_dir, folder_name)
    if folder_dir.exists():
        raise HTTPException(409, detail=f"Folder '{folder_name}' already exists")
    folder_dir.mkdir(parents=True, exist_ok=True)
    (folder_dir / ".dkk-folder").write_text("", encoding="utf-8")
    return {"name": folder_name}


@router.post("/api/folders/{folder_name}/rename")
async def rename_folder(folder_name: str, body: dict):
    """Rename a group folder (route).

    Folder membership is derived from the directory location, not stored in
    job_params.json, so a folder rename is a single directory rename — the
    contained jobs resolve under the new folder automatically.
    """
    new_name: str = body.get("new_name", "").strip()
    if not new_name or "/" in new_name or new_name.startswith("."):
        raise HTTPException(400, detail="Invalid folder name")
    output_dir = Path(_st.config.output.output_dir).resolve()
    src_dir = resolve_folder_dir(output_dir, folder_name)
    if not src_dir.is_dir():
        raise HTTPException(404, detail=f"Folder '{folder_name}' not found")
    if not is_folder_dir(src_dir):
        raise HTTPException(400, detail=f"'{folder_name}' is not a group folder")
    if new_name == folder_name:
        return {"name": new_name}
    dst_dir = resolve_folder_dir(output_dir, new_name)
    if dst_dir.exists():
        raise HTTPException(409, detail=f"Folder '{new_name}' already exists")
    try:
        src_dir.rename(dst_dir)
    except OSError as exc:
        raise HTTPException(500, detail=f"Folder rename failed: {exc}") from exc
    return {"name": new_name, "previous_name": folder_name}


def _copy_route_job(job_dir: Path, job_name: str, dest_path: Path) -> int:
    """Copy KMZ files and homes.kml from one job to dest_path. Returns file count."""
    count = 0
    for kmz_file in sorted(job_dir.glob("*.kmz")):
        shutil.copy2(kmz_file, dest_path / kmz_file.name)
        count += 1
    homes_kml = job_dir / "homes.kml"
    if homes_kml.exists():
        shutil.copy2(homes_kml, dest_path / f"{job_name}_homes.kml")
        count += 1
    return count


@router.post("/api/export-route")
async def export_route(body: dict):
    """Copy .kmz and homes.kml for every route job to a local directory.

    Route jobs are those with a takeoff_point_4326 and skipped != true.
    ``folder`` scopes to a specific group folder; null exports all folders.
    homes.kml is renamed ``<job_name>_homes.kml`` to avoid collisions.
    """
    if not _st.config.output.allow_local_fs:
        raise HTTPException(403, detail="Local filesystem actions are disabled")
    dest_str = (body.get("dest_dir") or "").strip()
    if not dest_str:
        raise HTTPException(400, detail="dest_dir is required")

    folder: str | None = body.get("folder")

    dest_path = Path(dest_str).expanduser().resolve()
    try:
        dest_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(
            400, detail=f"Cannot create destination folder: {exc}"
        ) from exc

    output_dir = Path(_st.config.output.output_dir).resolve()
    groups = scan_jobs(output_dir)

    copied = 0
    for group in groups:
        if folder is None:
            if group["name"] is not None:
                continue
        elif group["name"] != folder:
            continue
        for card in group["jobs"]:
            if not card.get("takeoff_point_4326") or card.get("skipped", False):
                continue
            _, _, job_dir = resolve_job_dir(output_dir, card["path"])
            copied += _copy_route_job(job_dir, card["name"], dest_path)

    return {"ok": True, "copied": copied, "dest_dir": str(dest_path)}


@router.delete("/api/folders/{folder_name}")
async def delete_folder(folder_name: str, force: bool = False):
    output_dir = Path(_st.config.output.output_dir).resolve()
    folder_dir = resolve_folder_dir(output_dir, folder_name)
    if not folder_dir.is_dir():
        raise HTTPException(404, detail=f"Folder '{folder_name}' not found")
    if not is_folder_dir(folder_dir):
        raise HTTPException(400, detail=f"'{folder_name}' is not a group folder")
    jobs = [d for d in folder_dir.iterdir() if d.is_dir()]
    if jobs and not force:
        raise HTTPException(
            409,
            detail=f"Folder '{folder_name}' contains jobs; use force=true to delete all",
        )
    shutil.rmtree(folder_dir)
    return {"deleted": folder_name}
