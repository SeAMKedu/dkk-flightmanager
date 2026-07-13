"""Read-only analytics / reporting routes derived over the job set.

These endpoints don't mutate jobs — they produce derived views: PDF flight
reports and mission packets, the satellite+weather forecast bar, NTRIP RTK base
stations, MGRS tile coverage, launch-site clustering, the fit-circle, staleness
scanning, and the jobs KML export.

Note: several of these routes (``/api/jobs/{path}/report.pdf``, ``/api/mgrs_tiles``,
``/api/fit_circle``, ``/api/rtk_stations``, ``/api/launch_sites``) must be matched
before management's greedy ``/api/jobs/{path:path}`` catch-all, so this router is
registered BEFORE management.router in server.py.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response
from flightmanager.web._server_state import SSEResponse
from pydantic import BaseModel

import flightmanager.web._server_state as _st
from flightmanager.storage.job_store import (
    best_polygon,
    load_params,
    params_from_manifest,
    refresh_status,
    resolve_folder_dir,
    resolve_job_dir,
    scan_jobs,
)

router = APIRouter()


class LaunchPoint(BaseModel):
    name: str
    lon: float
    lat: float


class ExportKmlRequest(BaseModel):
    paths: list[str]
    # When the map view is at overview zoom (jobs collapsed into launch sites)
    # the client sends launch-site centroids here; the KML then carries a single
    # "Launch sites" folder instead of per-job takeoff markers.
    launch_points: list[LaunchPoint] | None = None


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

    Body: ``{paths: [...], folder?, basemap?, include_job_cards?, single_rtk?}``
    -> ``{job_id}``.
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
                    single_rtk=body.get("single_rtk", False),
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


@router.get("/api/fit_circle")
async def fit_circle(folder: str | None = None, paths: str | None = None):
    """Smallest enclosing circle over the given jobs' polygons + takeoffs.

    Same fitting as a launch site's announcement circle, but over an arbitrary
    selection — the map view uses it to measure RTK station distances from the
    selection's centre. Returns ``{center_4326, radius_m, job_count}`` (404 when
    no job has geometry). Registered before ``/api/jobs/{path:path}``."""
    from flightmanager.forecasting.launch_sites import enclosing_circle

    output_dir = Path(_st.config.output.output_dir).resolve()
    wanted = {p for p in (paths or "").split(",") if p.strip()}
    cards: list[dict] = []
    for group in scan_jobs(output_dir, with_polygon=True):
        if not wanted and folder is not None and group["name"] != folder:
            continue
        if not wanted and folder is None and group["name"] is not None:
            continue
        for card in group["jobs"]:
            if not wanted or card["path"] in wanted:
                cards.append(card)
    fit = enclosing_circle(cards)
    if fit is None:
        raise HTTPException(404, detail="No geometry in the given jobs")
    center, radius_m = fit
    return {
        "center_4326": center,
        "radius_m": round(radius_m, 1),
        "job_count": len(cards),
    }


@router.get("/api/rtk_stations")
async def rtk_stations(folder: str | None = None, paths: str | None = None):
    """NTRIP RTK base stations near the jobs, for the 'RTK base stations' stat
    view, the map-view popups, and the PDF launch-site pages. Sourcetables are
    cache-first (RtkConfig.cache_max_age_hours); stations are filtered to
    search_radius_km of any job centroid. Registered before ``/api/jobs/{path:path}``."""
    import asyncio

    from flightmanager.geo import ntrip

    centroids, _ = _resolve_centroids(folder, paths)
    return await asyncio.to_thread(
        ntrip.stations_near,
        centroids,
        _st.config.rtk,
        _st.config.cache.cache_dir,
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
    launch_points = (
        [lp.model_dump() for lp in req.launch_points] if req.launch_points else None
    )
    kml = build_jobs_kml(jobs, launch_points=launch_points)
    return Response(content=kml, media_type="application/vnd.google-earth.kml+xml")
