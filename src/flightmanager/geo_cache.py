"""Geometry cache for parcel and property polygons.

Stores fetched parcel (Ruokavirasto) and kiinteistö (MML) geometries in the
same SQLite database as the tile cache, but in separate tables keyed by ID
rather than grid position.  This avoids re-hitting the network when the same
parcel is previewed or exported multiple times.

TTL is controlled by ``CacheConfig.parcels_ttl_days`` and
``CacheConfig.properties_ttl_days`` (both default 400 days — parcels change
only at the LPIS update cycle; property boundaries change rarely).

Shared DB infrastructure (``_db_path``, ``_init_db``, ``_is_ts_expired``) is
imported from :mod:`flightmanager.cache`, which owns the single SQLite file.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from flightmanager.cache import _db_path, _init_db, _is_ts_expired
from flightmanager.config import CacheConfig

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parcel geometry cache
# ---------------------------------------------------------------------------


@dataclass
class ParcelRecord:
    parcel_id: str
    lpis_year: int
    tunnus: int
    area_ha: float
    geometry_wkt: str    # WKT in EPSG:3067
    fetch_timestamp: str  # ISO 8601 UTC


def get_parcel_cache(
    cache_config: CacheConfig,
    parcel_id: str,
    lpis_year: int,
) -> ParcelRecord | None:
    """Return the cached parcel record, or ``None`` if missing or expired."""
    db = _db_path(Path(cache_config.cache_dir))
    _init_db(db)
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT parcel_id, lpis_year, tunnus, area_ha, geometry_wkt, fetch_timestamp "
            "FROM parcels WHERE parcel_id=? AND lpis_year=?",
            (parcel_id, lpis_year),
        ).fetchone()
    if row is None:
        return None
    record = ParcelRecord(
        parcel_id=row[0], lpis_year=row[1], tunnus=row[2],
        area_ha=row[3], geometry_wkt=row[4], fetch_timestamp=row[5],
    )
    if _is_ts_expired(record.fetch_timestamp, cache_config.parcels_ttl_days):
        return None
    return record


def put_parcel_cache(
    cache_config: CacheConfig,
    parcel_id: str,
    lpis_year: int,
    tunnus: int,
    area_ha: float,
    geometry_wkt: str,
) -> None:
    """Insert or replace a parcel record in the cache."""
    db = _db_path(Path(cache_config.cache_dir))
    _init_db(db)
    fetch_ts = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO parcels
               (parcel_id, lpis_year, tunnus, area_ha, geometry_wkt, fetch_timestamp)
               VALUES (?,?,?,?,?,?)""",
            (parcel_id, lpis_year, tunnus, area_ha, geometry_wkt, fetch_ts),
        )
        conn.commit()
    log.debug("Cached parcel %s year=%d", parcel_id, lpis_year)


# ---------------------------------------------------------------------------
# Property geometry cache
# ---------------------------------------------------------------------------


@dataclass
class PropertyRecord:
    property_id: str   # 14-digit numeric kiinteistötunnus
    display_id: str    # dash form e.g. "399-891-1-1"
    area_ha: float
    geometry_wkt: str  # WKT in EPSG:3067 (unioned if multiple palstat)
    fetch_timestamp: str


def get_property_cache(
    cache_config: CacheConfig,
    property_id: str,
) -> PropertyRecord | None:
    """Return the cached property record, or ``None`` if missing or expired."""
    db = _db_path(Path(cache_config.cache_dir))
    _init_db(db)
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT property_id, display_id, area_ha, geometry_wkt, fetch_timestamp "
            "FROM properties WHERE property_id=?",
            (property_id,),
        ).fetchone()
    if row is None:
        return None
    record = PropertyRecord(
        property_id=row[0], display_id=row[1], area_ha=row[2],
        geometry_wkt=row[3], fetch_timestamp=row[4],
    )
    if _is_ts_expired(record.fetch_timestamp, cache_config.properties_ttl_days):
        return None
    return record


def put_property_cache(
    cache_config: CacheConfig,
    property_id: str,
    display_id: str,
    area_ha: float,
    geometry_wkt: str,
) -> None:
    """Insert or replace a property record in the cache."""
    db = _db_path(Path(cache_config.cache_dir))
    _init_db(db)
    fetch_ts = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO properties
               (property_id, display_id, area_ha, geometry_wkt, fetch_timestamp)
               VALUES (?,?,?,?,?)""",
            (property_id, display_id, area_ha, geometry_wkt, fetch_ts),
        )
        conn.commit()
    log.debug("Cached property %s", property_id)
