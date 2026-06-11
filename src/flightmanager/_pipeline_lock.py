"""Cross-process pipeline lock.

Prevents concurrent SQLite tile cache writes when the web UI server and the
MCP server (separate processes) both try to run a pipeline operation at the
same time.

Usage:
    from flightmanager._pipeline_lock import pipeline_lock
    from filelock import Timeout

    try:
        with pipeline_lock(cfg.cache.cache_dir):
            run_job(...)
    except Timeout:
        # another process holds the lock
        raise RuntimeError("Pipeline busy — another process is running a job.")
"""

from __future__ import annotations

from pathlib import Path

from filelock import FileLock

_LOCK_FILENAME = ".pipeline.lock"


def pipeline_lock(cache_dir: str | Path, timeout: float = 0) -> FileLock:
    """Return a FileLock for pipeline operations.

    timeout=0  → non-blocking, raises filelock.Timeout immediately if busy.
    timeout>0  → wait up to that many seconds before raising Timeout.
    """
    return FileLock(str(Path(cache_dir) / _LOCK_FILENAME), timeout=timeout)
