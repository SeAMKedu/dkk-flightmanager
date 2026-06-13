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

from fastapi import APIRouter, HTTPException
from flightmanager._server_state import SSEResponse
from pydantic import BaseModel

import flightmanager._server_state as _st
from flightmanager.job_store import (
    best_polygon,
    check_cache_staleness,
    is_folder_dir,
    params_from_manifest,
    read_job_card,
    resolve_job_dir,
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
    polygon: dict   # GeoJSON Polygon or MultiPolygon (current survey)
    points: list    # 3 or 4 [lng, lat] coordinates


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
    groups = scan_jobs(output_dir)
    features = []
    for group in groups:
        if folder is not None and group["name"] != folder:
            continue
        for card in group["jobs"]:
            _, _, job_dir = resolve_job_dir(output_dir, card["path"])
            geom = best_polygon(job_dir)
            features.append({
                "type": "Feature",
                "geometry": geom,
                "properties": {
                    "path":              card["path"],
                    "name":              card["name"],
                    "folder":            card["folder"],
                    "color":             card["color"],
                    "untouched":         card["untouched"],
                    "flight_ready":      card.get("flight_ready"),
                    "needs_review":      card.get("needs_review"),
                    "area_ha":           card.get("area_ha"),
                    "original_area_ha":  card.get("original_area_ha"),
                    "area_lost_pct":     card.get("area_lost_pct"),
                    "subcategory":       card.get("subcategory"),
                    "height_m":          card.get("height_m"),
                    "waypoint_mode":     card.get("waypoint_mode", False),
                    "adv_min_height_m":  card.get("adv_min_height_m"),
                    "adv_max_height_m":  card.get("adv_max_height_m"),
                    "battery_count":     card.get("battery_count"),
                    "strip_speed_ms":    card.get("strip_speed_ms"),
                    "flight_time_min":   card.get("flight_time_min"),
                    "photo_count":       card.get("photo_count"),
                    "over_one_battery":  card.get("over_one_battery"),
                    "drone":             card.get("drone"),
                    "status":            card.get("status", "ok"),
                    "sort_order":        card.get("sort_order"),
                    "takeoff_point_4326": card.get("takeoff_point_4326"),
                    "skipped":           card.get("skipped", False),
                },
            })
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
        params_path = job_dir / "job_params.json"
        if not params_path.exists():
            continue
        try:
            data = json.loads(params_path.read_text(encoding="utf-8"))
            job_path = f"{folder0}/{job_dir.name}" if folder0 else job_dir.name
            new_so = ordered_set.get(job_path)  # None if not in list
            if data.get("sort_order") != new_so:
                data["sort_order"] = new_so
                params_path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
        except Exception:
            pass

    return {"ok": True}


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
        try:
            params = json.loads(params_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise HTTPException(500, detail=f"Could not read job_params.json: {exc}")
    elif manifest:
        params = params_from_manifest(name, manifest)
    else:
        raise HTTPException(404, detail=f"Job '{path}' has no readable data")
    stale: list[str] = []
    if manifest:
        stale = check_cache_staleness(manifest, _st.config.cache)
    return {"params": params, "cache_stale": stale, "folder": folder}


@router.patch("/api/jobs/{path:path}")
async def update_job(path: str, body: dict):
    output_dir = Path(_st.config.output.output_dir).resolve()
    folder, name, job_dir = resolve_job_dir(output_dir, path)
    if not job_dir.is_dir():
        raise HTTPException(404, detail=f"Job '{path}' not found")

    # Simple field update (color, sort_order, skipped — no rename)
    if "new_name" not in body and ("color" in body or "sort_order" in body or "skipped" in body):
        params_path = job_dir / "job_params.json"
        if params_path.exists():
            try:
                data = json.loads(params_path.read_text(encoding="utf-8"))
                if "color" in body:
                    data["color"] = body["color"]
                if "sort_order" in body:
                    so = body["sort_order"]
                    data["sort_order"] = int(so) if so is not None else None
                if "skipped" in body:
                    data["skipped"] = bool(body["skipped"])
                params_path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception as exc:
                raise HTTPException(500, detail=f"Could not update job: {exc}")
        return {"path": path, "color": body.get("color"), "sort_order": body.get("sort_order"), "skipped": body.get("skipped")}

    # Rename
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
                json_file.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
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
        try:
            params = json.loads(params_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise HTTPException(500, detail=str(exc))
    else:
        try:
            src_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            params = params_from_manifest(name, src_manifest)
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
    try:
        params = json.loads(params_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(500, detail=f"Could not read job_params.json: {exc}")

    # Derive a unique name for the new job
    parent_dir = job_dir.parent
    base_name = f"{name}-split"
    new_name = base_name
    counter = 2
    while (parent_dir / new_name).exists():
        new_name = f"{base_name}-{counter}"
        counter += 1

    now = datetime.now(timezone.utc).isoformat()

    # Update existing job in place (polygon_a)
    params["custom_polygon_4326"] = req.polygon_a
    params["last_preview_geojson"] = None
    params["saved_at"] = now
    params_path.write_text(json.dumps(params, ensure_ascii=False, indent=2), encoding="utf-8")

    # Create new sibling job (polygon_b, copy all other params)
    new_params = dict(params)
    new_params["job_name"] = new_name
    new_params["custom_polygon_4326"] = req.polygon_b
    new_params["last_preview_geojson"] = None
    new_params["saved_at"] = now

    new_dir = parent_dir / new_name
    new_dir.mkdir(parents=True, exist_ok=True)
    (new_dir / "job_params.json").write_text(
        json.dumps(new_params, ensure_ascii=False, indent=2), encoding="utf-8"
    )

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
        dest_parent = output_dir / to_folder
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
    """Open the job folder in the system file manager."""
    import subprocess
    import sys

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
        for order in [[pts[0], pts[1], pts[2], pts[3]], [pts[0], pts[1], pts[3], pts[2]]]:
            candidate = ShapelyPolygon(order)
            if not candidate.is_valid:
                candidate = make_valid(candidate)
            if candidate.is_valid and not candidate.is_empty and candidate.area > 0:
                return candidate
        raise HTTPException(400, detail="Selected points do not form a valid quadrilateral")
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

        result = unary_union([survey, quad]) if req.operation == "bridge" else survey.difference(quad)

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


@router.post("/api/merge")
async def merge_jobs(req: MergeRequest):
    if len(req.job_paths) < 2:
        raise HTTPException(400, detail="At least two jobs are required to merge")
    new_name = req.new_name.strip()
    if not new_name:
        raise HTTPException(400, detail="new_name is required")

    output_dir = Path(_st.config.output.output_dir).resolve()

    all_params: list[tuple[Path, dict]] = []
    for path in req.job_paths:
        _, _, job_dir = resolve_job_dir(output_dir, path)
        params_path = job_dir / "job_params.json"
        if not params_path.exists():
            raise HTTPException(404, detail=f"job_params.json not found for '{path}'")
        try:
            p = json.loads(params_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise HTTPException(500, detail=f"Could not read params for '{path}': {exc}")
        all_params.append((job_dir, p))

    def _is_id_job(job_dir: Path, p: dict) -> bool:
        inputs = p.get("inputs", {})
        has_ids = bool(inputs.get("parcel_ids") or inputs.get("property_ids"))
        return has_ids and p.get("batch_created", False) and not any(job_dir.glob("*.kmz"))

    use_id_strategy = all(_is_id_job(d, p) for d, p in all_params)

    if use_id_strategy:
        parcel_ids: list[str] = []
        property_ids: list[str] = []
        for _, p in all_params:
            inputs = p.get("inputs", {})
            parcel_ids.extend(inputs.get("parcel_ids") or [])
            property_ids.extend(inputs.get("property_ids") or [])
        seen: set[str] = set()
        parcel_ids   = [x for x in parcel_ids   if not (x in seen or seen.add(x))]  # type: ignore[func-returns-value]
        seen.clear()
        property_ids = [x for x in property_ids if not (x in seen or seen.add(x))]  # type: ignore[func-returns-value]

        first = all_params[0][1]
        merged_params = {
            "job_name": new_name,
            "saved_at": None,
            "inputs":   {"parcel_ids": parcel_ids, "property_ids": property_ids},
            "flight":   first.get("flight", {}),
            "polygon":  first.get("polygon", {"offset_m": 0.0, "simplify": "auto", "keepout": True}),
            "safety":   first.get("safety",  {"preview_radius_m": None}),
            "custom_polygon_4326": None,
            "batch_created": False,
            "color": None,
            "last_preview_geojson": None,
            "merge_strategy": "ids",
        }
    else:
        from shapely.geometry import mapping, shape
        from shapely.ops import unary_union
        from shapely.validation import make_valid

        polys = []
        for job_dir, p in all_params:
            geojson = p.get("custom_polygon_4326") or (
                (p.get("last_preview_geojson") or {}).get("survey")
            )
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
                raise HTTPException(400, detail=f"Invalid geometry for '{job_dir.name}': {exc}")

        merged = unary_union(polys)
        if not merged.is_valid:
            merged = make_valid(merged)
        if merged.is_empty:
            raise HTTPException(400, detail="Union produced empty geometry")

        all_parcel_ids: list[str] = []
        all_property_ids: list[str] = []
        seen2: set[str] = set()
        for _, p in all_params:
            inp = p.get("inputs", {})
            for pid in inp.get("parcel_ids") or []:
                if pid not in seen2:
                    seen2.add(pid)
                    all_parcel_ids.append(pid)
        seen2.clear()
        for _, p in all_params:
            inp = p.get("inputs", {})
            for pid in inp.get("property_ids") or []:
                if pid not in seen2:
                    seen2.add(pid)
                    all_property_ids.append(pid)

        merged_params = {
            "job_name": new_name,
            "saved_at": None,
            "inputs":  {"parcel_ids": all_parcel_ids, "property_ids": all_property_ids},
            "flight":  all_params[0][1].get("flight", {}),
            "polygon": all_params[0][1].get("polygon", {"offset_m": 0.0, "simplify": "auto", "keepout": True}),
            "safety":  all_params[0][1].get("safety",  {"preview_radius_m": None}),
            "custom_polygon_4326": dict(mapping(merged)),
            "batch_created": False,
            "color": None,
            "last_preview_geojson": None,
            "merge_strategy": "polygon_union",
        }

    dest_parent = (output_dir / req.folder) if req.folder else output_dir
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
    (dest_dir / "job_params.json").write_text(
        json.dumps(merged_params, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    if req.delete_sources:
        for path in req.job_paths:
            _, _, job_dir = resolve_job_dir(output_dir, path)
            if job_dir.is_dir():
                shutil.rmtree(job_dir)
        for path in req.job_paths:
            parts = path.strip("/").split("/", 1)
            if len(parts) == 2:
                parent = output_dir / parts[0]
                if parent.is_dir():
                    remaining = [d for d in parent.iterdir() if not d.name.startswith(".")]
                    if not remaining:
                        shutil.rmtree(parent)

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
    folder_dir = output_dir / folder_name
    if folder_dir.exists():
        raise HTTPException(409, detail=f"Folder '{folder_name}' already exists")
    folder_dir.mkdir(parents=True, exist_ok=True)
    (folder_dir / ".dkk-folder").write_text("", encoding="utf-8")
    return {"name": folder_name}


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
    dest_str = (body.get("dest_dir") or "").strip()
    if not dest_str:
        raise HTTPException(400, detail="dest_dir is required")

    folder: str | None = body.get("folder")

    dest_path = Path(dest_str).expanduser().resolve()
    try:
        dest_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(400, detail=f"Cannot create destination folder: {exc}") from exc

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
    folder_dir = output_dir / folder_name
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
