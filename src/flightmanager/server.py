"""Browser UI server — launched with `flightmanager serve`.

Route groups:
  execution  — POST /api/preview, /api/export, /api/batch; GET /api/progress/{id}
  management — GET/PATCH/POST/DELETE /api/jobs/…; /api/merge; /api/polygon_op; /api/folders/…

Single-job-at-a-time: 409 returned if a job is already running.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import flightmanager._server_state as _st
from flightmanager.config import AppConfig
from flightmanager.routers import execution, management, settings

_TEMPLATES_DIR = Path(__file__).parent / "templates"

_WATCHER_TRIGGERS = frozenset({"job_params.json", "manifest.json", ".dkk-folder"})


def broadcast_event(event: dict) -> None:
    """Send an event dict to all connected SSE clients (fire-and-forget)."""
    import json
    data = json.dumps(event)
    for q in list(_st.event_queues):
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            pass


async def _watch_output_dir(output_dir: Path) -> None:
    """Background task: watch output_dir for job store changes and broadcast."""
    from watchfiles import awatch
    try:
        async for changes in awatch(output_dir):
            modified_paths: set[str] = set()
            for _, changed_file in changes:
                p = Path(changed_file)
                if p.name not in _WATCHER_TRIGGERS:
                    continue
                try:
                    rel = p.parent.relative_to(output_dir)
                    parts = rel.parts
                    if len(parts) == 1:
                        modified_paths.add(parts[0])
                    elif len(parts) == 2:
                        modified_paths.add(f"{parts[0]}/{parts[1]}")
                except ValueError:
                    pass
            if modified_paths:
                broadcast_event({"type": "jobs_changed", "paths": sorted(modified_paths)})
    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.warning("Job watcher stopped unexpectedly: %s", e)

log = logging.getLogger(__name__)


def _load_ui() -> str:
    import jinja2
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(_TEMPLATES_DIR),
        autoescape=False,
    )
    return env.get_template("ui.html").render()


def create_app(config: AppConfig, config_path: str | None = None) -> FastAPI:
    _st.config = config
    _st.config_path = config_path

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        output_dir = Path(_st.config.output.output_dir).resolve()
        task = asyncio.create_task(_watch_output_dir(output_dir))
        yield
        # Send shutdown sentinel to all /api/events clients so their generators
        # exit cleanly before uvicorn tears down the connections. Without this,
        # Starlette's listen_for_disconnect gets a CancelledError mid-wait and
        # uvicorn logs it as ERROR.
        for q in list(_st.event_queues):
            try:
                q.put_nowait(None)
            except asyncio.QueueFull:
                pass
        await asyncio.sleep(0.15)  # let all SSE generators drain their sentinel
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        from flightmanager.net_stats import print_summary as _print_net_stats
        _print_net_stats(_st.config.cache.cache_dir)

    app = FastAPI(title="dkk-flightmanager", docs_url=None, redoc_url=None, lifespan=lifespan)

    @app.get("/", response_class=HTMLResponse)
    async def ui():
        return _load_ui()

    @app.get("/api/drones")
    async def get_drones():
        return [
            {
                "name":            d.name,
                "label":           d.label,
                "focal_length_mm": d.focal_length_mm,
                "pixel_pitch_um":  d.pixel_pitch_um,
                "image_width_px":  d.image_width_px,
                "image_height_px": d.image_height_px,
                "battery_minutes":       d.battery_minutes,
                "min_capture_interval_s": d.min_capture_interval_s,
            }
            for d in _st.config.drones
        ]

    @app.get("/api/version")
    async def get_version():
        from flightmanager import __version__
        return {"name": "dkk-flightmanager", "version": __version__}

    @app.get("/api/stats")
    async def get_stats():
        from flightmanager.net_stats import get as _get_stats
        from flightmanager.cache import query_disk_size
        data = _get_stats()
        data["cache_disk_bytes"] = query_disk_size(_st.config.cache.cache_dir)
        return data

    def _compute_default_speed() -> float:
        """Return the strip speed that will be used for the current config/drone.

        When auto_flight_speed_ms is None the speed is altitude-dependent; we
        compute it at the current default altitude so the UI can show a sensible
        placeholder.  The actual KMZ value is re-computed at export time.
        """
        from flightmanager.wpml import resolve_strip_speed
        drone = _st.config.active_drone()
        H = drone.height_from_gsd(_st.config.flight.target_gsd_cm)
        return resolve_strip_speed(_st.config.flight, drone, H)

    @app.get("/api/config")
    async def get_config():
        import os
        drone = _st.config.active_drone()
        return {
            "default_drone": _st.config.default_drone,
            "output_dir":    str(Path(_st.config.output.output_dir).resolve()),
            "subcategory":   _st.config.home_safety.operating_subcategory,
            "height_m":      int(drone.height_from_gsd(_st.config.flight.target_gsd_cm)),
            "offset_m":      _st.config.polygon.survey_offset_m,
            "simplify": (
                "auto" if _st.config.polygon.simplify_mode == "auto"
                else str(_st.config.polygon.simplify_tolerance_m)
            ),
            "keepout":     _st.config.home_safety.offset_enabled,
            "vlos_range_m": _st.config.home_safety.vlos_range_m,
            "mml_api_key": os.environ.get("MML_API_KEY", ""),
            "overlap_front_pct": _st.config.flight.overlap_front_pct,
            "overlap_side_pct":  _st.config.flight.overlap_side_pct,
            "auto_flight_speed_ms": _compute_default_speed(),
            "takeoff_security_height_m": _st.config.flight.takeoff_security_height_m,
            "color_palette": _st.config.output.color_palette,
            "max_area_loss_pct": _st.config.home_safety.max_area_loss_pct,
        }

    app.include_router(execution.router)
    app.include_router(management.router)
    app.include_router(settings.router)

    app.mount("/static", StaticFiles(directory=_TEMPLATES_DIR), name="static")

    from flightmanager.mcp_server import mcp as _mcp
    app.mount("/mcp", _mcp.sse_app())

    return app
