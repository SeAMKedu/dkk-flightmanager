"""Tests for batch.py — skeleton job creation."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from shapely.geometry import Polygon

from jobgen.batch import create_skeleton_jobs
from jobgen.config import AppConfig, CacheConfig, FlightConfig
from jobgen.parcels import Parcel


_POLY_3067 = Polygon([
    (300_000, 6_900_000), (301_000, 6_900_000),
    (301_000, 6_901_000), (300_000, 6_901_000),
    (300_000, 6_900_000),
])

_PARCEL = Parcel(
    parcel_id="TEST001",
    tunnus=1,
    year=2025,
    area_ha=100.0,
    geometry=_POLY_3067,
)


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        flight=FlightConfig(target_gsd_cm=2.7),
        cache=CacheConfig(cache_dir=str(tmp_path / "cache")),
    )


def _run(tmp_path, ids, id_type="parcels", folder=None, params=None):
    cfg = _config(tmp_path)
    with (
        patch.dict(os.environ, {"MML_API_KEY": "test-key"}),
        patch("jobgen.batch.fetch_parcels", return_value=[_PARCEL]),
    ):
        return create_skeleton_jobs(
            ids=ids,
            id_type=id_type,
            output_dir=tmp_path / "output",
            folder=folder,
            params=params or {},
            progress_cb=None,
            config=cfg,
        )


class TestCreateSkeletonJobs:
    def test_creates_job_params_file(self, tmp_path):
        results = _run(tmp_path, ["TEST001"])
        assert results[0]["status"] == "ok"
        params_path = tmp_path / "output" / "TEST001" / "job_params.json"
        assert params_path.exists()

    def test_job_params_contains_polygon(self, tmp_path):
        _run(tmp_path, ["TEST001"])
        params = json.loads((tmp_path / "output" / "TEST001" / "job_params.json").read_text())
        assert params["custom_polygon_4326"] is not None
        assert params["custom_polygon_4326"]["type"] in ("Polygon", "MultiPolygon")

    def test_batch_created_flag_set(self, tmp_path):
        _run(tmp_path, ["TEST001"])
        params = json.loads((tmp_path / "output" / "TEST001" / "job_params.json").read_text())
        assert params["batch_created"] is True

    def test_parcel_id_recorded_in_inputs(self, tmp_path):
        _run(tmp_path, ["TEST001"])
        params = json.loads((tmp_path / "output" / "TEST001" / "job_params.json").read_text())
        assert "TEST001" in params["inputs"]["parcel_ids"]

    def test_skips_existing_job_dir(self, tmp_path):
        existing = tmp_path / "output" / "TEST001"
        existing.mkdir(parents=True)
        results = _run(tmp_path, ["TEST001"])
        assert results[0]["status"] == "skipped"

    def test_creates_folder_marker(self, tmp_path):
        _run(tmp_path, ["TEST001"], folder="my-group")
        marker = tmp_path / "output" / "my-group" / ".dkk-folder"
        assert marker.exists()

    def test_job_placed_inside_folder(self, tmp_path):
        _run(tmp_path, ["TEST001"], folder="my-group")
        params_path = tmp_path / "output" / "my-group" / "TEST001" / "job_params.json"
        assert params_path.exists()

    def test_multiple_ids_all_created(self, tmp_path):
        parcel_b = Parcel(parcel_id="TEST002", tunnus=2, year=2025, area_ha=50.0, geometry=_POLY_3067)
        cfg = _config(tmp_path)
        with (
            patch.dict(os.environ, {"MML_API_KEY": "test-key"}),
            patch("jobgen.batch.fetch_parcels", side_effect=[[_PARCEL], [parcel_b]]),
        ):
            results = create_skeleton_jobs(
                ids=["TEST001", "TEST002"],
                id_type="parcels",
                output_dir=tmp_path / "output",
                folder=None,
                params={},
                progress_cb=None,
                config=cfg,
            )
        assert len(results) == 2
        assert all(r["status"] == "ok" for r in results)

    def test_params_written_into_job(self, tmp_path):
        _run(tmp_path, ["TEST001"], params={"subcategory": "A2", "offset_m": 5.0})
        params = json.loads((tmp_path / "output" / "TEST001" / "job_params.json").read_text())
        assert params["flight"]["subcategory"] == "A2"
        assert params["polygon"]["offset_m"] == 5.0

    def test_progress_callback_called(self, tmp_path):
        events = []
        cfg = _config(tmp_path)
        with (
            patch.dict(os.environ, {"MML_API_KEY": "test-key"}),
            patch("jobgen.batch.fetch_parcels", return_value=[_PARCEL]),
        ):
            create_skeleton_jobs(
                ids=["TEST001"],
                id_type="parcels",
                output_dir=tmp_path / "output",
                folder=None,
                params={},
                progress_cb=lambda stage, msg, pct: events.append((stage, pct)),
                config=cfg,
            )
        assert len(events) >= 2  # at least one progress + done event
