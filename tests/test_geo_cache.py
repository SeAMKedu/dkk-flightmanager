"""Tests for geo_cache.py — parcel and property geometry cache."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from jobgen.config import CacheConfig
from jobgen.geo_cache import (
    ParcelRecord,
    PropertyRecord,
    get_parcel_cache,
    get_property_cache,
    put_parcel_cache,
    put_property_cache,
)


def _cache_config(tmp_path: Path, **kwargs) -> CacheConfig:
    return CacheConfig(cache_dir=str(tmp_path / "cache"), **kwargs)


_WKT = "POLYGON ((300000 6900000, 300500 6900000, 300500 6900500, 300000 6900500, 300000 6900000))"


# ---------------------------------------------------------------------------
# Parcel cache
# ---------------------------------------------------------------------------


class TestParcelCacheRoundTrip:
    def test_put_then_get_returns_record(self, tmp_path):
        cfg = _cache_config(tmp_path)
        put_parcel_cache(cfg, "P001", 2024, tunnus=1, area_ha=25.0, geometry_wkt=_WKT)
        record = get_parcel_cache(cfg, "P001", 2024)
        assert record is not None
        assert record.parcel_id == "P001"
        assert record.lpis_year == 2024
        assert record.tunnus == 1
        assert abs(record.area_ha - 25.0) < 0.001
        assert record.geometry_wkt == _WKT

    def test_missing_returns_none(self, tmp_path):
        cfg = _cache_config(tmp_path)
        assert get_parcel_cache(cfg, "MISSING", 2024) is None

    def test_different_lpis_year_is_cache_miss(self, tmp_path):
        cfg = _cache_config(tmp_path)
        put_parcel_cache(cfg, "P001", 2024, tunnus=1, area_ha=25.0, geometry_wkt=_WKT)
        assert get_parcel_cache(cfg, "P001", 2025) is None

    def test_replace_updates_record(self, tmp_path):
        cfg = _cache_config(tmp_path)
        put_parcel_cache(cfg, "P001", 2024, tunnus=1, area_ha=25.0, geometry_wkt=_WKT)
        put_parcel_cache(cfg, "P001", 2024, tunnus=1, area_ha=30.0, geometry_wkt=_WKT)
        record = get_parcel_cache(cfg, "P001", 2024)
        assert record is not None
        assert abs(record.area_ha - 30.0) < 0.001

    def test_fetch_timestamp_is_set(self, tmp_path):
        cfg = _cache_config(tmp_path)
        put_parcel_cache(cfg, "P001", 2024, tunnus=1, area_ha=25.0, geometry_wkt=_WKT)
        record = get_parcel_cache(cfg, "P001", 2024)
        assert record is not None
        # Should parse without error and be recent
        ts = datetime.fromisoformat(record.fetch_timestamp)
        assert (datetime.now(timezone.utc) - ts).total_seconds() < 10


class TestParcelCacheTTL:
    def test_fresh_record_returned(self, tmp_path):
        cfg = _cache_config(tmp_path, parcels_ttl_days=400)
        put_parcel_cache(cfg, "P001", 2024, tunnus=1, area_ha=1.0, geometry_wkt=_WKT)
        assert get_parcel_cache(cfg, "P001", 2024) is not None

    def test_expired_record_returns_none(self, tmp_path):
        from jobgen.cache import _db_path, _init_db
        import sqlite3

        cfg = _cache_config(tmp_path, parcels_ttl_days=1)
        put_parcel_cache(cfg, "P001", 2024, tunnus=1, area_ha=1.0, geometry_wkt=_WKT)

        # Backdate the fetch_timestamp so it looks old
        old_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        db = _db_path(Path(cfg.cache_dir))
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE parcels SET fetch_timestamp=? WHERE parcel_id=? AND lpis_year=?",
                (old_ts, "P001", 2024),
            )
            conn.commit()

        assert get_parcel_cache(cfg, "P001", 2024) is None


# ---------------------------------------------------------------------------
# Property cache
# ---------------------------------------------------------------------------


class TestPropertyCacheRoundTrip:
    def test_put_then_get_returns_record(self, tmp_path):
        cfg = _cache_config(tmp_path)
        put_property_cache(cfg, "39989100010001", "399-891-1-1", area_ha=15.5, geometry_wkt=_WKT)
        record = get_property_cache(cfg, "39989100010001")
        assert record is not None
        assert record.property_id == "39989100010001"
        assert record.display_id == "399-891-1-1"
        assert abs(record.area_ha - 15.5) < 0.001
        assert record.geometry_wkt == _WKT

    def test_missing_returns_none(self, tmp_path):
        cfg = _cache_config(tmp_path)
        assert get_property_cache(cfg, "00000000000000") is None

    def test_replace_updates_record(self, tmp_path):
        cfg = _cache_config(tmp_path)
        put_property_cache(cfg, "39989100010001", "399-891-1-1", area_ha=15.5, geometry_wkt=_WKT)
        put_property_cache(cfg, "39989100010001", "399-891-1-1", area_ha=20.0, geometry_wkt=_WKT)
        record = get_property_cache(cfg, "39989100010001")
        assert record is not None
        assert abs(record.area_ha - 20.0) < 0.001

    def test_different_ids_stored_independently(self, tmp_path):
        cfg = _cache_config(tmp_path)
        put_property_cache(cfg, "00000000000001", "0-0-0-1", area_ha=1.0, geometry_wkt=_WKT)
        put_property_cache(cfg, "00000000000002", "0-0-0-2", area_ha=2.0, geometry_wkt=_WKT)
        r1 = get_property_cache(cfg, "00000000000001")
        r2 = get_property_cache(cfg, "00000000000002")
        assert r1 is not None and abs(r1.area_ha - 1.0) < 0.001
        assert r2 is not None and abs(r2.area_ha - 2.0) < 0.001


class TestPropertyCacheTTL:
    def test_expired_property_returns_none(self, tmp_path):
        from jobgen.cache import _db_path, _init_db
        import sqlite3

        cfg = _cache_config(tmp_path, properties_ttl_days=1)
        put_property_cache(cfg, "39989100010001", "399-891-1-1", area_ha=1.0, geometry_wkt=_WKT)

        old_ts = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        db = _db_path(Path(cfg.cache_dir))
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE properties SET fetch_timestamp=? WHERE property_id=?",
                (old_ts, "39989100010001"),
            )
            conn.commit()

        assert get_property_cache(cfg, "39989100010001") is None

    def test_parcels_and_properties_in_same_db(self, tmp_path):
        cfg = _cache_config(tmp_path)
        put_parcel_cache(cfg, "P001", 2024, tunnus=1, area_ha=1.0, geometry_wkt=_WKT)
        put_property_cache(cfg, "39989100010001", "399-891-1-1", area_ha=2.0, geometry_wkt=_WKT)
        assert get_parcel_cache(cfg, "P001", 2024) is not None
        assert get_property_cache(cfg, "39989100010001") is not None
