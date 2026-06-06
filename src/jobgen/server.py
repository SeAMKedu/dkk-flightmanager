"""Browser UI server — launched with `jobgen serve`.

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

import jobgen._server_state as _st
from jobgen.config import AppConfig
from jobgen.routers import execution, management, settings

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_ui_html_cache: str | None = None

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
    global _ui_html_cache
    if _ui_html_cache is None:
        import jinja2
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(_TEMPLATES_DIR),
            autoescape=False,
        )
        _ui_html_cache = env.get_template("ui.html").render()
    return _ui_html_cache


def create_app(config: AppConfig, config_path: str | None = None) -> FastAPI:
    _st.config = config
    _st.config_path = config_path

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        output_dir = Path(_st.config.output.output_dir).resolve()
        task = asyncio.create_task(_watch_output_dir(output_dir))
        yield
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    app = FastAPI(title="dkk-jobmaker", docs_url=None, redoc_url=None, lifespan=lifespan)

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
                "battery_minutes": d.battery_minutes,
            }
            for d in _st.config.drones
        ]

    @app.get("/api/version")
    async def get_version():
        from jobgen import __version__
        return {"name": "dkk-jobmaker", "version": __version__}

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
        }

    app.include_router(execution.router)
    app.include_router(management.router)
    app.include_router(settings.router)

    from jobgen.mcp_server import mcp as _mcp
    app.mount("/mcp", _mcp.sse_app())

    return app
