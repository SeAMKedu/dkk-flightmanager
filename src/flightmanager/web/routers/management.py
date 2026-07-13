"""Job + folder CRUD routes: list, geojson, get, rename, clone, split, move,
delete, reveal, reorder, route_rename, merge, polygon_op, and folder
create/rename/delete.

Read-only analytics/reporting views over the job set (PDF reports, forecast,
RTK, MGRS tiles, launch sites, fit-circle, refresh scan, KML export) live in the
sibling ``insights`` router.

Note: /api/jobs/geojson must be registered BEFORE /api/jobs/{path:path} so
FastAPI matches the literal path first.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from flightmanager.web._server_state import SSEResponse
from pydantic import BaseModel

import flightmanager.web._server_state as _st
from flightmanager.storage.job_store import (
    apply_route_order,
    card_polygon,
    check_cache_staleness,
    JobRenameError,
    is_folder_dir,
    load_params,
    make_thumbnail_svg,
    params_from_manifest,
    read_job_card,
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
                        "parcel_coverage_pct": card.get("parcel_coverage_pct"),
                        "coverage_area_ha": card.get("coverage_area_ha"),
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
    output_dir = Path(_st.config.output.output_dir).resolve()
    try:
        apply_route_order(output_dir, paths)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
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
    for idx, (orig_path, (job_dir, old_name, new_name)) in enumerate(zip(paths, plan)):
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


def _write_thumbnail(job_dir: Path, geom: dict | None) -> None:
    """(Re)write ``thumbnail.svg`` for *job_dir* from a survey geometry."""
    svg = make_thumbnail_svg(geom)
    if svg:
        (job_dir / "thumbnail.svg").write_text(svg, encoding="utf-8")


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
    # save_params re-derives it from the new polygon, and regenerate the
    # thumbnail — otherwise the original job keeps its pre-split (whole-area) card.
    params["custom_polygon_4326"] = req.polygon_a
    params["survey_outline"] = None
    params.pop("last_preview_geojson", None)
    params["saved_at"] = now
    save_params(job_dir, params)
    _write_thumbnail(job_dir, req.polygon_a)

    # Create new sibling job (polygon_b, copy all other params)
    new_params = dict(params)
    new_params["job_name"] = new_name
    new_params["custom_polygon_4326"] = req.polygon_b
    new_params["survey_outline"] = None
    new_params["saved_at"] = now

    # The original keeps its flight-order slot; the new half is inserted right
    # after it (shifting later siblings). dict(params) copied the original's
    # sort_order, which would otherwise collide — override it here.
    orig_so = params.get("sort_order")
    new_params["sort_order"] = (
        _open_sort_order_slot(parent_dir, orig_so, exclude={name, new_name})
        if orig_so is not None
        else None
    )

    new_dir = parent_dir / new_name
    new_dir.mkdir(parents=True, exist_ok=True)
    save_params(new_dir, new_params)
    _write_thumbnail(new_dir, req.polygon_b)

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


def _merged_sort_order(
    all_params: list[tuple[Path, dict]],
    dest_parent: Path,
    new_name: str,
    *,
    same_folder: bool,
    delete_sources: bool,
) -> int | None:
    """Pick the flight-order slot for a merged job among its sources' siblings.

    Returns ``None`` (leave the merged job unrouted) when the sources don't all
    share the destination folder — sort_order is per-folder, so a cross-folder
    merge has no meaningful slot — or when no source carried a ``sort_order``.
    Otherwise: if the sources are deleted they vacate their slots, so the merged
    job takes the group's first one (``min``); if they stay, it is inserted right
    after the last of them (shifting later siblings).
    """
    if not same_folder:
        return None
    src_sort_orders = [
        p.get("sort_order") for _, p in all_params if p.get("sort_order") is not None
    ]
    if not src_sort_orders:
        return None
    if delete_sources:
        return min(src_sort_orders)
    return _open_sort_order_slot(dest_parent, max(src_sort_orders), exclude={new_name})


def _open_sort_order_slot(folder_dir: Path, after: int, exclude: set[str]) -> int:
    """Free the flight-order slot right after ``after`` in ``folder_dir``.

    Bumps every sibling job whose ``sort_order`` is greater than ``after`` by +1
    (so nothing collides) and returns ``after + 1`` — the slot the caller should
    assign to the job being inserted. Jobs in ``exclude`` (by directory name) and
    those without a ``sort_order`` are left untouched.
    """
    for job_dir in folder_dir.iterdir():
        if (
            not job_dir.is_dir()
            or job_dir.name in exclude
            or job_dir.name.startswith(".")
        ):
            continue
        if not (job_dir / "job_params.json").exists():
            continue
        p = load_params(job_dir)
        if p is None:
            continue
        so = p.get("sort_order")
        if so is not None and so > after:
            p["sort_order"] = so + 1
            save_params(job_dir, p)
    return after + 1


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

    # Default the destination to the sources' shared folder (when they all live
    # in one), so a merge stays put instead of dropping into the output root.
    src_folders = {(p.rsplit("/", 1)[0] if "/" in p else None) for p in req.job_paths}
    common_folder = next(iter(src_folders)) if len(src_folders) == 1 else None
    folder = req.folder or common_folder

    dest_parent = resolve_folder_dir(output_dir, folder)
    dest_parent.mkdir(parents=True, exist_ok=True)
    if folder:
        marker = dest_parent / ".dkk-folder"
        if not marker.exists():
            marker.write_text("", encoding="utf-8")

    dest_dir = dest_parent / new_name
    if dest_dir.exists():
        raise HTTPException(
            409, detail=f"A job named '{new_name}' already exists in that location"
        )
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Position the merged job in the flight order (only meaningful when the
    # sources all live in the destination folder — see _merged_sort_order).
    slot = _merged_sort_order(
        all_params,
        dest_parent,
        new_name,
        same_folder=common_folder is not None and folder == common_folder,
        delete_sources=req.delete_sources,
    )
    if slot is not None:
        merged_params["sort_order"] = slot
    save_params(dest_dir, merged_params)

    if req.delete_sources:
        _delete_merged_sources(output_dir, req.job_paths)

    card = read_job_card(dest_dir, folder=folder)
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
    """Copy .kmz and homes.kml for the selected route jobs to a local directory.

    ``paths`` is the list of selected job paths; only route jobs among them
    (those with a takeoff_point_4326 and skipped != true) are copied.
    homes.kml is renamed ``<job_name>_homes.kml`` to avoid collisions.
    """
    if not _st.config.output.allow_local_fs:
        raise HTTPException(403, detail="Local filesystem actions are disabled")
    dest_str = (body.get("dest_dir") or "").strip()
    if not dest_str:
        raise HTTPException(400, detail="dest_dir is required")

    paths = body.get("paths") or []
    if not paths:
        raise HTTPException(400, detail="paths is required")

    dest_path = Path(dest_str).expanduser().resolve()
    try:
        dest_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(
            400, detail=f"Cannot create destination folder: {exc}"
        ) from exc

    output_dir = Path(_st.config.output.output_dir).resolve()

    copied = 0
    for path in paths:
        _, _, job_dir = resolve_job_dir(output_dir, path)
        card = read_job_card(job_dir, folder=None)
        if not card.get("takeoff_point_4326") or card.get("skipped", False):
            continue
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
