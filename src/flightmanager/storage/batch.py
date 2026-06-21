"""Batch skeleton job creation.

``create_skeleton_jobs()`` fetches geometry for a list of parcel or property
IDs and writes a ``job_params.json`` for each — no KMZ, DSM, or manifest.
Jobs created this way appear in the UI as "untouched" (grey badge) and are
ready to preview and export.

Used by both the ``flightmanager batch`` CLI command and the ``POST /api/batch``
server endpoint.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable

from flightmanager.config import AppConfig
from flightmanager.geo.geometry import reproject_to_4326
from flightmanager.geo.parcels import fetch_parcels
from flightmanager.geo.properties import fetch_properties

log = logging.getLogger(__name__)


def _cb(fn: Callable | None, stage: str, msg: str, pct: int) -> None:
    if fn:
        fn(stage, msg, pct)


def _require_api_key() -> str:
    key = os.environ.get("MML_API_KEY", "")
    if not key:
        raise RuntimeError(
            "MML_API_KEY environment variable is not set. "
            "Obtain a key at https://www.maanmittauslaitos.fi/rajapinnat/api-avaimen-ohje "
            "and set it in your .env file."
        )
    return key


def create_skeleton_jobs(
    ids: list[str],
    id_type: str,
    output_dir: Path,
    folder: str | None,
    params: dict,
    progress_cb: Callable | None,
    config: AppConfig,
) -> list[dict]:
    """Fetch geometry for each ID and write a skeleton ``job_params.json``.

    No KMZ, DSM, or manifest is written.  The polygon is stored as
    ``custom_polygon_4326`` so ``openJob()`` can display it immediately
    and a full export can skip the WFS re-fetch.

    Returns a list of per-ID result dicts with keys ``id``, ``status``
    (``"ok"`` | ``"skipped"`` | ``"error"``), and optional ``reason``.
    """
    from shapely.geometry import mapping as _mapping

    api_key = _require_api_key() if id_type == "properties" else ""

    target_dir = (output_dir / folder) if folder else output_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    if folder:
        marker = target_dir / ".dkk-folder"
        if not marker.exists():
            marker.write_text("", encoding="utf-8")

    results: list[dict] = []
    total = len(ids)

    for i, id_ in enumerate(ids):
        _cb(
            progress_cb,
            "batch",
            f"[{i + 1}/{total}] Fetching {id_}…",
            int(i / total * 90),
        )

        job_dir = target_dir / id_
        if job_dir.exists():
            results.append({"id": id_, "status": "skipped", "reason": "already exists"})
            continue

        try:
            if id_type == "parcels":
                geoms = fetch_parcels(
                    parcel_ids=[id_], config=config.parcels, cache_config=config.cache
                )
                poly_3067 = geoms[0].geometry if geoms else None
            else:
                geoms = fetch_properties(
                    [id_],
                    api_key,
                    timeout_s=config.properties.timeout_s,
                    page_size=config.properties.page_size,
                    cache_config=config.cache,
                )
                poly_3067 = geoms[0].geometry if geoms else None

            if poly_3067 is None:
                results.append(
                    {"id": id_, "status": "error", "reason": "no geometry returned"}
                )
                continue

            poly_4326 = reproject_to_4326(poly_3067)
            geojson = dict(_mapping(poly_4326))

            job_dir.mkdir(parents=True, exist_ok=True)
            job_params = {
                "job_name": id_,
                "saved_at": None,
                "inputs": {
                    "parcel_ids": [id_] if id_type == "parcels" else [],
                    "property_ids": [id_] if id_type == "properties" else [],
                },
                "flight": {
                    "drone": params.get("drone"),
                    "height_m": params.get("height_m"),
                    "subcategory": params.get("subcategory", "A3"),
                },
                "polygon": {
                    "offset_m": params.get("offset_m", 0.0),
                    "simplify": params.get("simplify", "auto"),
                    "keepout": params.get("keepout", True),
                },
                "safety": {
                    "preview_radius_m": params.get("preview_radius_m"),
                },
                "custom_polygon_4326": geojson,
                "batch_created": True,
                "color": None,
            }
            from flightmanager.storage.job_store import save_params

            save_params(job_dir, job_params)
            results.append({"id": id_, "status": "ok"})

        except Exception as exc:
            log.warning("Batch: failed to create skeleton for %s: %s", id_, exc)
            results.append({"id": id_, "status": "error", "reason": str(exc)})

    _cb(
        progress_cb,
        "batch",
        f"Done — {sum(r['status'] == 'ok' for r in results)}/{total} created",
        95,
    )
    return results
