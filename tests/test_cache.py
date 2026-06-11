"""Tests for cache.py — Phase 1b."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from flightmanager.cache import (
    CacheMissError,
    TileBbox,
    TileRecord,
    _db_path,
    _init_db,
    _is_expired,
    _lookup,
    _register,
    _sha256,
    covering_tiles,
    get_tiles,
    snap_down,
    tile_provenance,
)
from flightmanager.config import CacheConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_config(tmp_path: Path, *, offline: bool = False, tile_size_m: int = 1000) -> CacheConfig:
    return CacheConfig(
        cache_dir=str(tmp_path),
        tile_size_m=tile_size_m,
        dem_ttl_days=365,
        buildings_ttl_days=180,
        offline=offline,
    )


def fake_fetcher(content: bytes = b"TILEDATA") -> MagicMock:
    """Returns a mock fetcher that writes *content* to dest_path."""
    def _fetch(tile_id: str, bbox: TileBbox, dest: Path) -> tuple[str, str | None]:
        dest.write_bytes(content)
        return (f"https://example.com/{tile_id}", "v1.0")
    m = MagicMock(side_effect=_fetch)
    return m


def make_tile_record(
    tmp_path: Path,
    tile_id: str = "E300000_N6900000",
    dataset: str = "dem",
    age_days: int = 0,
) -> TileRecord:
    tile_file = tmp_path / dataset / f"{tile_id}.tif"
    tile_file.parent.mkdir(parents=True, exist_ok=True)
    tile_file.write_bytes(b"DATA")
    fetch_ts = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    return TileRecord(
        tile_id=tile_id, dataset=dataset,
        bbox=(300_000.0, 6_900_000.0, 301_000.0, 6_901_000.0),
        path=tile_file, source_url="https://example.com/tile",
        fetch_timestamp=fetch_ts, dataset_version="v1",
        checksum="abc123", byte_size=4,
    )


# ---------------------------------------------------------------------------
# Grid math
# ---------------------------------------------------------------------------


class TestSnapDown:
    def test_aligned_value(self):
        assert snap_down(1000.0, 1000) == 1000

    def test_below_multiple(self):
        assert snap_down(1500.0, 1000) == 1000

    def test_negative(self):
        assert snap_down(-1.0, 1000) == -1000

    def test_zero(self):
        assert snap_down(0.0, 1000) == 0


class TestCoveringTiles:
    def test_single_tile_exactly_aligned(self):
        tiles = covering_tiles((0.0, 0.0, 1000.0, 1000.0), 1000)
        assert len(tiles) == 1
        assert tiles[0][0] == "E0_N0"
        assert tiles[0][1] == (0.0, 0.0, 1000.0, 1000.0)

    def test_bbox_spanning_two_columns(self):
        tiles = covering_tiles((500.0, 0.0, 1500.0, 1000.0), 1000)
        ids = [t[0] for t in tiles]
        assert "E0_N0" in ids
        assert "E1000_N0" in ids
        assert len(tiles) == 2

    def test_bbox_spanning_four_tiles(self):
        tiles = covering_tiles((500.0, 500.0, 1500.0, 1500.0), 1000)
        ids = [t[0] for t in tiles]
        assert set(ids) == {"E0_N0", "E0_N1000", "E1000_N0", "E1000_N1000"}

    def test_exact_upper_boundary_not_included(self):
        # bbox ending exactly at tile boundary should not pull in the next tile
        tiles = covering_tiles((0.0, 0.0, 1000.0, 1000.0), 1000)
        assert len(tiles) == 1

    def test_real_finnish_bbox(self):
        # Seinäjoki area in EPSG:3067 — should produce a small set of 1km tiles
        bbox = (295_000.0, 6_980_000.0, 315_000.0, 7_000_000.0)
        tiles = covering_tiles(bbox, 1000)
        assert len(tiles) == 20 * 20
        assert all(t[0].startswith("E") for t, _ in [(t, b) for t, b in tiles])

    def test_tile_id_deterministic(self):
        a = covering_tiles((300_000.0, 6_900_000.0, 302_000.0, 6_902_000.0), 1000)
        b = covering_tiles((300_000.0, 6_900_000.0, 302_000.0, 6_902_000.0), 1000)
        assert [t[0] for t in a] == [t[0] for t in b]


# ---------------------------------------------------------------------------
# SQLite index
# ---------------------------------------------------------------------------


class TestIndex:
    def test_init_creates_table(self, tmp_path):
        db = _db_path(tmp_path)
        _init_db(db)
        with sqlite3.connect(db) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tiles'"
            ).fetchall()
        assert rows

    def test_register_and_lookup(self, tmp_path):
        db = _db_path(tmp_path)
        _init_db(db)
        record = make_tile_record(tmp_path)
        _register(db, record)
        found = _lookup(db, "dem", "E300000_N6900000")
        assert found is not None
        assert found.tile_id == "E300000_N6900000"
        assert found.checksum == "abc123"

    def test_lookup_missing_returns_none(self, tmp_path):
        db = _db_path(tmp_path)
        _init_db(db)
        assert _lookup(db, "dem", "E999_N999") is None

    def test_register_replaces_existing(self, tmp_path):
        db = _db_path(tmp_path)
        _init_db(db)
        record = make_tile_record(tmp_path)
        _register(db, record)
        updated = TileRecord(**{**record.__dict__, "checksum": "newchecksum"})
        _register(db, updated)
        found = _lookup(db, "dem", record.tile_id)
        assert found.checksum == "newchecksum"


# ---------------------------------------------------------------------------
# TTL
# ---------------------------------------------------------------------------


class TestTTL:
    def test_fresh_tile_not_expired(self, tmp_path):
        record = make_tile_record(tmp_path, age_days=0)
        assert not _is_expired(record, ttl_days=365)

    def test_old_tile_expired(self, tmp_path):
        record = make_tile_record(tmp_path, age_days=400)
        assert _is_expired(record, ttl_days=365)

    def test_exactly_at_ttl_not_expired(self, tmp_path):
        record = make_tile_record(tmp_path, age_days=365)
        assert not _is_expired(record, ttl_days=365)


# ---------------------------------------------------------------------------
# get_tiles — cache hit / miss
# ---------------------------------------------------------------------------


class TestGetTiles:
    def test_cache_miss_calls_fetcher(self, tmp_path):
        config = make_config(tmp_path)
        fetcher = fake_fetcher()
        bbox = (300_000.0, 6_900_000.0, 301_000.0, 6_901_000.0)
        records = get_tiles("dem", bbox, fetcher, config)
        assert len(records) == 1
        fetcher.assert_called_once()

    def test_cache_hit_skips_fetcher(self, tmp_path):
        config = make_config(tmp_path)
        fetcher = fake_fetcher()
        bbox = (300_000.0, 6_900_000.0, 301_000.0, 6_901_000.0)
        get_tiles("dem", bbox, fetcher, config)
        get_tiles("dem", bbox, fetcher, config)
        assert fetcher.call_count == 1  # second call hits cache

    def test_refresh_refetches_cached_tile(self, tmp_path):
        config = make_config(tmp_path)
        fetcher = fake_fetcher()
        bbox = (300_000.0, 6_900_000.0, 301_000.0, 6_901_000.0)
        get_tiles("dem", bbox, fetcher, config)
        get_tiles("dem", bbox, fetcher, config, refresh=True)
        assert fetcher.call_count == 2

    def test_expired_tile_refetched(self, tmp_path):
        config = make_config(tmp_path)
        fetcher = fake_fetcher()
        bbox = (300_000.0, 6_900_000.0, 301_000.0, 6_901_000.0)
        # Warm cache then manually backdate the fetch_timestamp
        get_tiles("dem", bbox, fetcher, config)
        db = _db_path(tmp_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE tiles SET fetch_timestamp=? WHERE dataset='dem'", (old_ts,)
            )
            conn.commit()
        get_tiles("dem", bbox, fetcher, config)
        assert fetcher.call_count == 2

    def test_record_has_correct_path(self, tmp_path):
        config = make_config(tmp_path)
        fetcher = fake_fetcher()
        bbox = (300_000.0, 6_900_000.0, 301_000.0, 6_901_000.0)
        records = get_tiles("dem", bbox, fetcher, config)
        assert records[0].path.exists()
        assert records[0].path.suffix == ".tif"

    def test_buildings_tile_has_geojson_extension(self, tmp_path):
        config = make_config(tmp_path)
        fetcher = fake_fetcher()
        bbox = (300_000.0, 6_900_000.0, 301_000.0, 6_901_000.0)
        records = get_tiles("buildings", bbox, fetcher, config)
        assert records[0].path.suffix == ".geojson"

    def test_unknown_dataset_raises(self, tmp_path):
        config = make_config(tmp_path)
        with pytest.raises(ValueError, match="Unknown dataset"):
            get_tiles("bogus", (0, 0, 1000, 1000), fake_fetcher(), config)


# ---------------------------------------------------------------------------
# Offline mode
# ---------------------------------------------------------------------------


class TestOfflineMode:
    def test_offline_miss_raises(self, tmp_path):
        config = make_config(tmp_path, offline=True)
        fetcher = fake_fetcher()
        bbox = (300_000.0, 6_900_000.0, 301_000.0, 6_901_000.0)
        with pytest.raises(CacheMissError, match="offline=true"):
            get_tiles("dem", bbox, fetcher, config)
        fetcher.assert_not_called()

    def test_offline_hit_succeeds(self, tmp_path):
        # Warm the cache first (online), then switch to offline
        online = make_config(tmp_path, offline=False)
        offline = make_config(tmp_path, offline=True)
        fetcher = fake_fetcher()
        bbox = (300_000.0, 6_900_000.0, 301_000.0, 6_901_000.0)
        get_tiles("dem", bbox, fetcher, online)
        records = get_tiles("dem", bbox, fake_fetcher(), offline)
        assert len(records) == 1
        assert records[0].path.exists()


# ---------------------------------------------------------------------------
# Atomic write — interrupted fetch leaves no registered tile
# ---------------------------------------------------------------------------


class TestAtomicWrite:
    def test_failed_fetch_leaves_no_index_entry(self, tmp_path):
        config = make_config(tmp_path)
        bbox = (300_000.0, 6_900_000.0, 301_000.0, 6_901_000.0)

        def failing_fetcher(tile_id, tile_bbox, dest):
            dest.write_bytes(b"partial")
            raise RuntimeError("network error mid-fetch")

        with pytest.raises(RuntimeError, match="network error"):
            get_tiles("dem", bbox, failing_fetcher, config)

        # Index must have no entry for this tile
        db = _db_path(tmp_path)
        _init_db(db)
        assert _lookup(db, "dem", "E300000_N6900000") is None

    def test_failed_fetch_leaves_no_tmp_file(self, tmp_path):
        config = make_config(tmp_path)
        bbox = (300_000.0, 6_900_000.0, 301_000.0, 6_901_000.0)

        def failing_fetcher(tile_id, tile_bbox, dest):
            dest.write_bytes(b"partial")
            raise RuntimeError("network error")

        with pytest.raises(RuntimeError):
            get_tiles("dem", bbox, failing_fetcher, config)

        tmp = tmp_path / "dem" / "E300000_N6900000.tmp"
        assert not tmp.exists()


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


class TestProvenance:
    def test_provenance_keys(self, tmp_path):
        config = make_config(tmp_path)
        fetcher = fake_fetcher()
        bbox = (300_000.0, 6_900_000.0, 301_000.0, 6_901_000.0)
        records = get_tiles("dem", bbox, fetcher, config)
        prov = tile_provenance(records)
        assert "tile_ids" in prov
        assert "fetch_date_min" in prov
        assert "fetch_date_max" in prov
        assert "source_urls" in prov

    def test_provenance_reflects_tile_fetch_date_not_run_time(self, tmp_path):
        config = make_config(tmp_path)
        fetcher = fake_fetcher()
        bbox = (300_000.0, 6_900_000.0, 301_000.0, 6_901_000.0)
        get_tiles("dem", bbox, fetcher, config)
        # Backdate by 30 days — still within TTL (365 days), so cache is hit on
        # the second call.  The provenance should return the stored timestamp,
        # not the current time.
        old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        db = _db_path(tmp_path)
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE tiles SET fetch_timestamp=?", (old_ts,))
            conn.commit()
        records2 = get_tiles("dem", bbox, fake_fetcher(), config)
        prov = tile_provenance(records2)
        assert prov["fetch_date_min"] == old_ts

    def test_empty_records_provenance(self):
        prov = tile_provenance([])
        assert prov["tile_ids"] == []
        assert prov["fetch_date_min"] is None
