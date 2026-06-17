"""Logging setup: console handler + per-run file handler.

Call setup_logging() once at the start of each job run.  All modules use the
standard `logging.getLogger(__name__)` pattern; this module wires the handlers
onto the root 'flightmanager' logger so every sub-module's messages flow through.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_FMT = "%(asctime)s %(levelname)-8s %(name)s — %(message)s"
_DATEFMT = "%Y-%m-%dT%H:%M:%S"


def setup_logging(
    job_name: str,
    output_dir: Path | str,
    level: int = logging.INFO,
) -> logging.Logger:
    """Configure the 'flightmanager' logger with a console handler and a run.log file handler.

    Returns the root flightmanager logger.  Safe to call once per job in a
    long-lived server: the console handler is attached only once, and the
    per-job file handler replaces (and closes) any previous job's file handler
    so descriptors and console handlers never accumulate across jobs.
    """
    log_dir = Path(output_dir) / job_name
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "run.log"

    logger = logging.getLogger("flightmanager")
    logger.setLevel(level)

    formatter = logging.Formatter(_FMT, datefmt=_DATEFMT)

    # Console handler: attach exactly once for the process lifetime. Adding one
    # per job is what produced duplicated console lines (same message/timestamp
    # repeated once per accumulated handler).
    if not any(isinstance(h, logging.StreamHandler)
               and not isinstance(h, logging.FileHandler)
               for h in logger.handlers):
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        logger.addHandler(console)

    # File handler: at most one at a time. If we're already logging to this
    # job's run.log, keep it; otherwise close and detach any existing file
    # handler (the previous job's) before opening the new one. Leaving old
    # file handlers attached leaked one descriptor per distinct job.
    target = str(log_file.resolve())
    for h in list(logger.handlers):
        if isinstance(h, logging.FileHandler):
            if h.baseFilename == target:
                return logger
            logger.removeHandler(h)
            h.close()

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    logger.info("Logging to %s", log_file)
    return logger
