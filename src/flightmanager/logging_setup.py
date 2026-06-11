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

    Returns the root flightmanager logger.  Call once per job; subsequent calls for
    the same job_name are idempotent (handlers are not duplicated).
    """
    log_dir = Path(output_dir) / job_name
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "run.log"

    logger = logging.getLogger("flightmanager")
    logger.setLevel(level)

    # Idempotency: skip if handlers already attached for this job
    if any(isinstance(h, logging.FileHandler) and h.baseFilename == str(log_file.resolve())
           for h in logger.handlers):
        return logger

    formatter = logging.Formatter(_FMT, datefmt=_DATEFMT)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    logger.addHandler(console)

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    logger.info("Logging to %s", log_file)
    return logger
