"""Shared mutable state for the FastAPI server.

Keeping state here (rather than as module-level globals in server.py) lets the
router modules import it without creating circular dependencies.
"""

from __future__ import annotations

import asyncio
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from fastapi.responses import StreamingResponse

if TYPE_CHECKING:
    from flightmanager.config import AppConfig


class SSEResponse(StreamingResponse):
    """StreamingResponse that suppresses CancelledError on shutdown.

    Starlette's listen_for_disconnect task raises CancelledError when uvicorn
    force-cancels open connections at graceful-shutdown timeout. Catching it
    here prevents uvicorn from logging it as ERROR: Exception in ASGI application.
    """

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        except asyncio.CancelledError:
            pass


executor: ThreadPoolExecutor = ThreadPoolExecutor(max_workers=1)
job_lock: threading.Lock = threading.Lock()
active_job_id: str | None = None
job_queues: dict[str, asyncio.Queue] = {}
config: "AppConfig | None" = None
config_path: str | None = (
    None  # absolute path to config.toml; used for override persistence
)
event_queues: set[asyncio.Queue] = set()  # SSE clients subscribed to /api/events

# Per-session preview results, keyed by the client's session id so concurrent
# clients don't clobber each other (route_estimate / export read the caller's
# own last preview for obstacle data). A None session id falls back to a shared
# "_default" bucket (single-user / pre-session clients). Bounded LRU.
_DEFAULT_SESSION = "_default"
PREVIEW_RESULTS_CAP = 32
preview_results: "OrderedDict[str, dict]" = OrderedDict()


def store_preview(session_id: str | None, result: dict) -> None:
    """Store *result* as the caller's last preview, evicting the oldest past the cap."""
    key = session_id or _DEFAULT_SESSION
    preview_results[key] = result
    preview_results.move_to_end(key)
    while len(preview_results) > PREVIEW_RESULTS_CAP:
        preview_results.popitem(last=False)


def get_preview(session_id: str | None) -> dict | None:
    """Return the caller's last preview result, or None."""
    return preview_results.get(session_id or _DEFAULT_SESSION)
