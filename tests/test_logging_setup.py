"""Regression tests for logging_setup — guards against handler accumulation.

The single-job CLI era added a console + file handler per job. In the
long-lived `serve` process this leaked one open run.log descriptor per distinct
job and produced duplicated console lines (one per accumulated StreamHandler).
These tests pin the invariant: handlers must not accumulate across jobs.
"""

from __future__ import annotations

import logging

import pytest

from flightmanager.logging_setup import setup_logging


@pytest.fixture
def clean_logger():
    """Detach/close any handlers before and after so tests are isolated."""
    logger = logging.getLogger("flightmanager")
    saved = list(logger.handlers)
    for h in saved:
        logger.removeHandler(h)
    yield logger
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    for h in saved:
        logger.addHandler(h)


def _counts(logger: logging.Logger) -> tuple[int, int]:
    """Return (console_handler_count, file_handler_count)."""
    files = sum(isinstance(h, logging.FileHandler) for h in logger.handlers)
    consoles = sum(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in logger.handlers
    )
    return consoles, files


def test_handlers_do_not_accumulate_across_distinct_jobs(tmp_path, clean_logger):
    for i in range(10):
        setup_logging(f"job-{i}", tmp_path)
        consoles, files = _counts(clean_logger)
        # Exactly one console handler for the process and one file handler at a time.
        assert consoles == 1, f"console handlers grew to {consoles} after {i + 1} jobs"
        assert files == 1, f"file handlers grew to {files} after {i + 1} jobs"


def test_previous_file_handler_is_closed_on_switch(tmp_path, clean_logger):
    setup_logging("job-a", tmp_path)
    first_fh = next(h for h in clean_logger.handlers if isinstance(h, logging.FileHandler))
    setup_logging("job-b", tmp_path)
    # The old handler must be detached and its descriptor closed.
    assert first_fh not in clean_logger.handlers
    assert first_fh.stream is None or first_fh.stream.closed


def test_same_job_is_idempotent(tmp_path, clean_logger):
    setup_logging("job-a", tmp_path)
    fh = next(h for h in clean_logger.handlers if isinstance(h, logging.FileHandler))
    setup_logging("job-a", tmp_path)
    # Same job keeps the very same handler instance (no churn, no new descriptor).
    handlers = [h for h in clean_logger.handlers if isinstance(h, logging.FileHandler)]
    assert handlers == [fh]
