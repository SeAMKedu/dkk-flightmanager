"""Tests for job_store.py — filesystem helpers."""

from __future__ import annotations

import json
from pathlib import Path


from flightmanager.job_store import (
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
        params = {
            "custom_polygon_4326": geom,
            "last_preview_geojson": {"survey": {"other": True}},
        }
        (job / "job_params.json").write_text(json.dumps(params), encoding="utf-8")
        result = best_polygon(job)
        assert result == geom

    def test_falls_back_to_preview_geojson(self, tmp_path):
        job = tmp_path / "job"
        job.mkdir()
        survey_geom = {
            "type": "Polygon",
            "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]],
        }
        params = {
            "custom_polygon_4326": None,
            "last_preview_geojson": {"survey": survey_geom},
        }
        (job / "job_params.json").write_text(json.dumps(params), encoding="utf-8")
        assert best_polygon(job) == survey_geom

    def test_no_params_file_returns_none(self, tmp_path):
        job = tmp_path / "job"
        job.mkdir()
        assert best_polygon(job) is None


# ---------------------------------------------------------------------------
# read_job_card
# ---------------------------------------------------------------------------


def _make_job(
    tmp_path: Path, name: str = "my-job", folder: str | None = None, **params_extra
) -> Path:
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
        from flightmanager.config import CacheConfig
        from flightmanager.cache import TileRecord, _db_path, _init_db, _register

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
        from flightmanager.config import CacheConfig

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
        from flightmanager.config import CacheConfig

        cfg = CacheConfig(cache_dir=str(tmp_path / "cache"))
        stale = check_cache_staleness({}, cfg)
        assert stale == []


# ---------------------------------------------------------------------------
# refresh_status (staleness detection)
# ---------------------------------------------------------------------------


def _register_tile(cache_dir, dataset, tile_id, fetch_ts):
    """Insert a bare tile record into the cache index (file need not exist)."""
    from pathlib import Path

    from flightmanager.cache import TileRecord, _db_path, _init_db, _register

    db = _db_path(Path(cache_dir))
    _init_db(db)
    _register(
        db,
        TileRecord(
            tile_id=tile_id,
            dataset=dataset,
            bbox=(0, 0, 1000, 1000),
            path=Path(cache_dir) / f"{tile_id}.tif",
            source_url=None,
            fetch_timestamp=fetch_ts,
            dataset_version=None,
            checksum="x",
            byte_size=1,
        ),
    )


class TestBatterySummary:
    def test_single_piece(self):
        from flightmanager.job_store import _battery_summary

        out = _battery_summary(
            {
                "estimated_flight_time_min": 12.3,
                "estimated_photo_count": 200,
                "over_one_battery": False,
            }
        )
        assert out == {
            "flight_time_min": 12.3,
            "photo_count": 200,
            "over_one_battery": False,
            "battery_count": 1,
        }

    def test_single_piece_over_battery(self):
        from flightmanager.job_store import _battery_summary

        out = _battery_summary(
            {"estimated_flight_time_min": 40, "over_one_battery": True}
        )
        assert out["battery_count"] == 2 and out["over_one_battery"] is True

    def test_pieces_summed(self):
        from flightmanager.job_store import _battery_summary

        out = _battery_summary(
            {
                "pieces": [
                    {
                        "estimated_flight_time_min": 10,
                        "estimated_photo_count": 100,
                        "over_one_battery": False,
                    },
                    {
                        "estimated_flight_time_min": 25,
                        "estimated_photo_count": 300,
                        "over_one_battery": True,
                    },
                ],
                "over_any_battery": True,
            }
        )
        assert out["flight_time_min"] == 35
        assert out["photo_count"] == 400
        assert out["over_one_battery"] is True
        assert out["battery_count"] == 3  # 1 + 2

    def test_empty(self):
        from flightmanager.job_store import _battery_summary

        out = _battery_summary({})
        assert out == {
            "flight_time_min": None,
            "photo_count": None,
            "over_one_battery": False,
            "battery_count": None,
        }


class TestRefreshStatus:
    def _cfg(self, tmp_path):
        from flightmanager.config import CacheConfig

        return CacheConfig(cache_dir=str(tmp_path / "cache"))

    def test_older_pipeline_version_flagged(self, tmp_path):
        from flightmanager.job_store import refresh_status

        out = refresh_status({"pipeline_version": 1}, self._cfg(tmp_path), 2)
        assert out["needs_refresh"] is True
        assert any("pipeline" in r for r in out["reasons"])

    def test_current_version_not_flagged(self, tmp_path):
        from flightmanager.job_store import refresh_status

        out = refresh_status({"pipeline_version": 2}, self._cfg(tmp_path), 2)
        assert out["needs_refresh"] is False
        assert out["reasons"] == []

    def test_missing_pipeline_version_treated_as_zero(self, tmp_path):
        from flightmanager.job_store import refresh_status

        out = refresh_status({}, self._cfg(tmp_path), 1)
        assert out["needs_refresh"] is True

    def test_newer_source_data_flagged(self, tmp_path):
        from flightmanager.job_store import refresh_status

        cfg = self._cfg(tmp_path)
        # Job used data fetched in January; cache now holds a March copy.
        _register_tile(cfg.cache_dir, "dem", "E1_N1", "2026-03-01T00:00:00+00:00")
        manifest = {
            "pipeline_version": 1,
            "cache_provenance": {
                "dem": {
                    "tile_ids": ["E1_N1"],
                    "fetch_date_max": "2026-01-01T00:00:00+00:00",
                }
            },
        }
        out = refresh_status(manifest, cfg, 1)
        assert out["needs_refresh"] is True
        assert any("dem" in r for r in out["reasons"])

    def test_same_source_data_not_flagged(self, tmp_path):
        from flightmanager.job_store import refresh_status

        cfg = self._cfg(tmp_path)
        _register_tile(cfg.cache_dir, "dem", "E1_N1", "2026-01-01T00:00:00+00:00")
        manifest = {
            "pipeline_version": 1,
            "cache_provenance": {
                "dem": {
                    "tile_ids": ["E1_N1"],
                    "fetch_date_max": "2026-01-01T00:00:00+00:00",
                }
            },
        }
        assert refresh_status(manifest, cfg, 1)["needs_refresh"] is False


# ---------------------------------------------------------------------------
# Params storage: save_params / load_params / migration
# ---------------------------------------------------------------------------


_SQUARE = {
    "type": "Polygon",
    "coordinates": [
        [[25.0, 62.0], [25.1, 62.0], [25.1, 62.1], [25.0, 62.1], [25.0, 62.0]]
    ],
}


class TestParamsStorage:
    def test_round_trip_stamps_schema_version(self, tmp_path):
        from flightmanager.job_store import SCHEMA_VERSION, load_params, save_params

        save_params(tmp_path, {"job_name": "j", "custom_polygon_4326": _SQUARE})
        data = load_params(tmp_path)
        assert data["job_name"] == "j"
        assert data["schema_version"] == SCHEMA_VERSION

    def test_save_drops_legacy_blob_and_derives_outline(self, tmp_path):
        """Old jobs with an embedded last_preview_geojson migrate to survey_outline."""
        from flightmanager.job_store import load_params, save_params

        legacy = {
            "job_name": "old",
            "custom_polygon_4326": None,
            "last_preview_geojson": {
                "survey": _SQUARE,
                "strips_geojson": {"big": "blob"},
            },
        }
        save_params(tmp_path, legacy)
        data = load_params(tmp_path)
        assert "last_preview_geojson" not in data
        assert data["survey_outline"] is not None
        assert data["survey_outline"]["type"] == "Polygon"

    def test_explicit_outline_preserved(self, tmp_path):
        from flightmanager.job_store import load_params, save_params

        save_params(tmp_path, {"survey_outline": _SQUARE, "custom_polygon_4326": None})
        assert load_params(tmp_path)["survey_outline"] == _SQUARE

    def test_outline_from_custom_polygon_when_absent(self, tmp_path):
        from flightmanager.job_store import load_params, save_params

        save_params(tmp_path, {"custom_polygon_4326": _SQUARE})
        assert load_params(tmp_path)["survey_outline"]["type"] == "Polygon"

    def test_load_missing_returns_none(self, tmp_path):
        from flightmanager.job_store import load_params

        assert load_params(tmp_path) is None

    def test_write_json_atomic_no_tmp_left(self, tmp_path):
        from flightmanager.job_store import write_json_atomic

        target = tmp_path / "x.json"
        write_json_atomic(target, {"a": 1})
        assert json.loads(target.read_text()) == {"a": 1}
        assert list(tmp_path.glob("*.tmp")) == []

    def test_unknown_keys_preserved(self, tmp_path):
        """extra=allow keeps forward/unknown keys on round-trip."""
        from flightmanager.job_store import load_params, save_params

        save_params(tmp_path, {"job_name": "j", "future_field": 42})
        assert load_params(tmp_path)["future_field"] == 42

    def test_card_polygon_priority(self):
        from flightmanager.job_store import card_polygon

        other = {"type": "Polygon", "coordinates": [[[9, 9], [9, 8], [8, 8], [9, 9]]]}
        # custom polygon wins over outline and legacy
        assert (
            card_polygon({"custom_polygon_4326": _SQUARE, "survey_outline": other})
            == _SQUARE
        )
        # outline wins over legacy survey
        assert (
            card_polygon(
                {"survey_outline": _SQUARE, "last_preview_geojson": {"survey": other}}
            )
            == _SQUARE
        )
        # legacy survey is the final fallback
        assert card_polygon({"last_preview_geojson": {"survey": _SQUARE}}) == _SQUARE
