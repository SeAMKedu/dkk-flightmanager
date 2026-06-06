"""Shared mutable state for the FastAPI server.

Keeping state here (rather than as module-level globals in server.py) lets the
router modules import it without creating circular dependencies.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from fastapi.responses import StreamingResponse

if TYPE_CHECKING:
    from jobgen.config import AppConfig

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
config_path: str | None = None  # absolute path to config.toml; used for override persistence
last_preview_result: dict | None = None
event_queues: set[asyncio.Queue] = set()  # SSE clients subscribed to /api/events
