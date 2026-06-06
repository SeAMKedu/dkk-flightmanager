"""Tests for job_store.py — filesystem helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobgen.job_store import (
    best_polygon,
    check_cache_staleness,
    is_folder_dir,
    is_job_dir,
    make_thumbnail_svg,
    params_from_manifest,
    read_job_card,
    resolve_job_dir,
    scan_jobs,
)


# ---------------------------------------------------------------------------
# make_thumbnail_svg
# ---------------------------------------------------------------------------


_POLYGON_GEOJSON = {
    "type": "Polygon",
    "coordinates": [
        [[25.0, 62.0], [25.1, 62.0], [25.1, 62.1], [25.0, 62.1], [25.0, 62.0]]
    ],
}

_MULTIPOLYGON_GEOJSON = {
    "type": "MultiPolygon",
    "coordinates": [
        [[[25.0, 62.0], [25.1, 62.0], [25.1, 62.1], [25.0, 62.0]]],
        [[[25.5, 62.0], [25.6, 62.0], [25.6, 62.1], [25.5, 62.0]]],
    ],
}


class TestMakeThumbnailSvg:
    def test_polygon_returns_svg_string(self):
        svg = make_thumbnail_svg(_POLYGON_GEOJSON)
        assert svg is not None
        assert svg.startswith("<svg")
        assert "polygon" in svg.lower()

    def test_multipolygon_produces_two_paths(self):
        svg = make_thumbnail_svg(_MULTIPOLYGON_GEOJSON)
        assert svg is not None
        assert svg.count("<polygon") == 2

    def test_none_input_returns_none(self):
        assert make_thumbnail_svg(None) is None

    def test_unknown_type_returns_none(self):
        assert make_thumbnail_svg({"type": "Point", "coordinates": [0, 0]}) is None

    def test_svg_dimensions_are_64(self):
        svg = make_thumbnail_svg(_POLYGON_GEOJSON)
        assert 'width="64"' in svg
        assert 'height="64"' in svg


# ---------------------------------------------------------------------------
# is_job_dir / is_folder_dir
# ---------------------------------------------------------------------------


class TestIsJobDir:
    def test_empty_dir_is_not_job(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        assert not is_job_dir(d)

    def test_job_params_marks_job(self, tmp_path):
        d = tmp_path / "job"
        d.mkdir()
        (d / "job_params.json").write_text("{}", encoding="utf-8")
        assert is_job_dir(d)

    def test_manifest_marks_job(self, tmp_path):
        d = tmp_path / "job"
        d.mkdir()
        (d / "manifest.json").write_text("{}", encoding="utf-8")
        assert is_job_dir(d)


class TestIsFolderDir:
    def test_dkk_folder_marker_detected(self, tmp_path):
        d = tmp_path / "group"
        d.mkdir()
        (d / ".dkk-folder").write_text("", encoding="utf-8")
        assert is_folder_dir(d)

    def test_contains_job_subdir_detected(self, tmp_path):
        group = tmp_path / "group"
        group.mkdir()
        job = group / "my-job"
        job.mkdir()
        (job / "job_params.json").write_text("{}", encoding="utf-8")
        assert is_folder_dir(group)

    def test_empty_dir_is_not_folder(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        assert not is_folder_dir(d)


# ---------------------------------------------------------------------------
# resolve_job_dir
# ---------------------------------------------------------------------------


class TestResolveJobDir:
    def test_name_only(self, tmp_path):
        folder, name, path = resolve_job_dir(tmp_path, "my-job")
        assert folder is None
        assert name == "my-job"
        assert path == tmp_path / "my-job"

    def test_folder_slash_name(self, tmp_path):
        folder, name, path = resolve_job_dir(tmp_path, "my-group/my-job")
        assert folder == "my-group"
        assert name == "my-job"
        assert path == tmp_path / "my-group" / "my-job"

    def test_leading_slash_stripped(self, tmp_path):
        folder, name, path = resolve_job_dir(tmp_path, "/my-job")
        assert folder is None
        assert name == "my-job"


# ---------------------------------------------------------------------------
# best_polygon
# ---------------------------------------------------------------------------


class TestBestPolygon:
    def test_custom_polygon_preferred(self, tmp_path):
        job = tmp_path / "job"
        job.mkdir()
        geom = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
        params = {"custom_polygon_4326": geom, "last_preview_geojson": {"survey": {"other": True}}}
        (job / "job_params.json").write_text(json.dumps(params), encoding="utf-8")
        result = best_polygon(job)
        assert result == geom

    def test_falls_back_to_preview_geojson(self, tmp_path):
        job = tmp_path / "job"
        job.mkdir()
        survey_geom = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
        params = {"custom_polygon_4326": None, "last_preview_geojson": {"survey": survey_geom}}
        (job / "job_params.json").write_text(json.dumps(params), encoding="utf-8")
        assert best_polygon(job) == survey_geom

    def test_no_params_file_returns_none(self, tmp_path):
        job = tmp_path / "job"
        job.mkdir()
        assert best_polygon(job) is None


# ---------------------------------------------------------------------------
# read_job_card
# ---------------------------------------------------------------------------


def _make_job(tmp_path: Path, name: str = "my-job", folder: str | None = None, **params_extra) -> Path:
    base = tmp_path / folder / name if folder else tmp_path / name
    base.mkdir(parents=True, exist_ok=True)
    params = {
        "job_name": name,
        "saved_at": "2025-06-01T12:00:00Z",
        "color": "#ff0000",
        **params_extra,
    }
    (base / "job_params.json").write_text(json.dumps(params), encoding="utf-8")
    return base


class TestReadJobCard:
    def test_ok_status_with_params(self, tmp_path):
        d = _make_job(tmp_path)
        card = read_job_card(d)
        assert card["status"] == "ok"
        assert card["name"] == "my-job"
        assert card["color"] == "#ff0000"

    def test_failed_status_when_no_files(self, tmp_path):
        d = tmp_path / "ghost"
        d.mkdir()
        card = read_job_card(d)
        assert card["status"] == "failed"

    def test_path_includes_folder(self, tmp_path):
        d = _make_job(tmp_path, "my-job", folder="grp")
        card = read_job_card(d, folder="grp")
        assert card["path"] == "grp/my-job"
        assert card["folder"] == "grp"

    def test_untouched_batch_job(self, tmp_path):
        d = _make_job(tmp_path, batch_created=True)
        card = read_job_card(d)
        assert card["untouched"] is True

    def test_not_untouched_when_kmz_present(self, tmp_path):
        d = _make_job(tmp_path, batch_created=True)
        (d / "my-job.kmz").write_bytes(b"")
        card = read_job_card(d)
        assert card["untouched"] is False

    def test_manifest_fields_surfaced(self, tmp_path):
        d = _make_job(tmp_path)
        manifest = {
            "geometry": {"final_area_ha": 12.5, "survey_vertex_count": 8},
            "flight": {"drone": "m3m-ms", "drone_label": "M3M"},
            "flight_ready": True,
            "needs_review": False,
        }
        (d / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        card = read_job_card(d)
        assert abs(card["area_ha"] - 12.5) < 0.001
        assert card["drone"] == "m3m-ms"
        assert card["flight_ready"] is True


# ---------------------------------------------------------------------------
# scan_jobs
# ---------------------------------------------------------------------------


class TestScanJobs:
    def test_empty_output_dir_returns_empty(self, tmp_path):
        assert scan_jobs(tmp_path) == []

    def test_nonexistent_dir_returns_empty(self, tmp_path):
        assert scan_jobs(tmp_path / "missing") == []

    def test_root_job_in_null_group(self, tmp_path):
        _make_job(tmp_path, "job-a")
        groups = scan_jobs(tmp_path)
        assert len(groups) == 1
        assert groups[0]["name"] is None
        assert groups[0]["jobs"][0]["name"] == "job-a"

    def test_folder_group_returned(self, tmp_path):
        grp = tmp_path / "my-group"
        grp.mkdir()
        (grp / ".dkk-folder").write_text("", encoding="utf-8")
        _make_job(grp, "job-1")
        groups = scan_jobs(tmp_path)
        folder_group = next(g for g in groups if g["name"] == "my-group")
        assert len(folder_group["jobs"]) == 1
        assert folder_group["jobs"][0]["name"] == "job-1"

    def test_root_and_folder_jobs_separated(self, tmp_path):
        _make_job(tmp_path, "root-job")
        grp = tmp_path / "grp"
        grp.mkdir()
        (grp / ".dkk-folder").write_text("", encoding="utf-8")
        _make_job(grp, "folder-job")
        groups = scan_jobs(tmp_path)
        null_group = next(g for g in groups if g["name"] is None)
        folder_group = next(g for g in groups if g["name"] == "grp")
        assert null_group["jobs"][0]["name"] == "root-job"
        assert folder_group["jobs"][0]["name"] == "folder-job"


# ---------------------------------------------------------------------------
# params_from_manifest
# ---------------------------------------------------------------------------


class TestParamsFromManifest:
    def _manifest(self):
        return {
            "parcels": {"parcel_ids": ["P001", "P002"]},
            "flight": {"drone": "m3m-ms", "derived_height_m": 63.1},
            "home_safety": {"operating_subcategory": "A3", "preview_radius_m": 200},
        }

    def test_parcel_ids_extracted(self):
        params = params_from_manifest("test-job", self._manifest())
        assert params["inputs"]["parcel_ids"] == ["P001", "P002"]

    def test_drone_extracted(self):
        params = params_from_manifest("test-job", self._manifest())
        assert params["flight"]["drone"] == "m3m-ms"

    def test_subcategory_extracted(self):
        params = params_from_manifest("test-job", self._manifest())
        assert params["flight"]["subcategory"] == "A3"

    def test_custom_polygon_is_none(self):
        params = params_from_manifest("test-job", self._manifest())
        assert params["custom_polygon_4326"] is None

    def test_empty_manifest_does_not_raise(self):
        params = params_from_manifest("test-job", {})
        assert params["inputs"]["parcel_ids"] == []


# ---------------------------------------------------------------------------
# check_cache_staleness
# ---------------------------------------------------------------------------


class TestCheckCacheStaleness:
    def test_all_tiles_present_returns_empty(self, tmp_path):
        from datetime import datetime as _dt, timezone
        from jobgen.config import CacheConfig
        from jobgen.cache import TileRecord, _db_path, _init_db, _register

        cfg = CacheConfig(cache_dir=str(tmp_path / "cache"))
        manifest = {
            "cache_provenance": {
                "dem": {"tile_ids": ["E298000_N6898000"]},
                "buildings": {"tile_ids": []},
            }
        }
        tile_path = tmp_path / "cache" / "dem" / "E298000_N6898000.tif"
        tile_path.parent.mkdir(parents=True)
        tile_path.write_bytes(b"")

        db = _db_path(tmp_path / "cache")
        _init_db(db)
        record = TileRecord(
            tile_id="E298000_N6898000",
            dataset="dem",
            bbox=(298_000.0, 6_898_000.0, 299_000.0, 6_899_000.0),
            path=tile_path,
            source_url="http://x",
            fetch_timestamp=_dt.now(timezone.utc).isoformat(),
            dataset_version=None,
            checksum="abc",
            byte_size=1000,
        )
        _register(db, record)

        stale = check_cache_staleness(manifest, cfg)
        assert stale == []

    def test_missing_tile_flagged(self, tmp_path):
        from jobgen.config import CacheConfig

        cfg = CacheConfig(cache_dir=str(tmp_path / "cache"))
        manifest = {
            "cache_provenance": {
                "dem": {"tile_ids": ["E298000_N6898000"]},
                "buildings": {"tile_ids": []},
            }
        }
        stale = check_cache_staleness(manifest, cfg)
        assert "dem/E298000_N6898000" in stale

    def test_empty_provenance_returns_empty(self, tmp_path):
        from jobgen.config import CacheConfig

        cfg = CacheConfig(cache_dir=str(tmp_path / "cache"))
        stale = check_cache_staleness({}, cfg)
        assert stale == []
