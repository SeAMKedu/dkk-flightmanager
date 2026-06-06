"""Browser UI server — launched with `jobgen serve`.

Route groups:
  execution  — POST /api/preview, /api/export, /api/batch; GET /api/progress/{id}
  management — GET/PATCH/POST/DELETE /api/jobs/…; /api/merge; /api/polygon_op; /api/folders/…

Single-job-at-a-time: 409 returned if a job is already running.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

import jobgen._server_state as _st
from jobgen.config import AppConfig
from jobgen.routers import execution, management

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_ui_html_cache: str | None = None

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


def create_app(config: AppConfig) -> FastAPI:
    _st.config = config

    app = FastAPI(title="dkk-jobmaker", docs_url=None, redoc_url=None)

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
            "mml_api_key": os.environ.get("MML_API_KEY", ""),
        }

    app.include_router(execution.router)
    app.include_router(management.router)

    return app
