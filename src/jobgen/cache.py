"""Tile-grid cache for MML DEM and building datasets.

Stores whole tiles on a fixed EPSG:3067 grid so overlapping jobs reuse data
without re-hitting the network.  Network fetchers live in elevation.py and
buildings.py; this module only orchestrates storage, TTL, and provenance.

Grid tile ID convention: ``E{xmin}_N{ymin}`` where xmin/ymin are the
lower-left corner of the tile in integer EPSG:3067 metres.

File-path conventions (mirrors the plan's cache/ layout):
  dem tiles       → cache_dir/dem/<tile_id>.tif
  buildings tiles → cache_dir/buildings/<tile_id>.gpkg
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from math import floor
from pathlib import Path
from typing import Callable

import jobgen.net_stats as _ns
from jobgen.config import CacheConfig

log = logging.getLogger(__name__)

# Output file extension per dataset.
# Buildings use .geojson rather than .gpkg to avoid a fiona/OGR dependency;
# GeoJSON + shapely is sufficient for the tile sizes we work with.
_DATASET_EXT: dict[str, str] = {
    "dem": ".tif",
    "buildings": ".geojson",
    "powerlines": ".geojson",
    "pylons": ".geojson",
}

# (tile_id, bbox) → tile bbox in EPSG:3067
TileBbox = tuple[float, float, float, float]  # xmin, ymin, xmax, ymax

# Fetcher contract: given (tile_id, tile_bbox_3067, dest_path), write the tile
# to dest_path and return (source_url, dataset_version_or_None).
FetcherFn = Callable[[str, TileBbox, Path], tuple[str, str | None]]

# Per-tile threading locks — prevents two threads fetching the same tile simultaneously.
_tile_locks: dict[tuple[str, str], threading.Lock] = {}
_tile_locks_mu = threading.Lock()


class CacheMissError(RuntimeError):
    """Raised in offline mode when a required tile is not in the cache."""


# ---------------------------------------------------------------------------
# Grid math
# ---------------------------------------------------------------------------


def snap_down(value: float, tile_size_m: int) -> int:
    """Return the largest tile-grid multiple ≤ value."""
    return int(floor(value / tile_size_m)) * tile_size_m


def covering_tiles(bbox: TileBbox, tile_size_m: int) -> list[tuple[str, TileBbox]]:
    """Return all (tile_id, tile_bbox) pairs whose tiles intersect *bbox*.

    Uses the fixed EPSG:3067 grid with the given tile size.  The result is
    deterministic: same bbox + tile_size always yields the same tile IDs.
    """
    xmin, ymin, xmax, ymax = bbox
    x0 = snap_down(xmin, tile_size_m)
    y0 = snap_down(ymin, tile_size_m)

    tiles: list[tuple[str, TileBbox]] = []
    x = x0
    while x < xmax:
        y = y0
        while y < ymax:
            tile_id = f"E{x}_N{y}"
            tile_bbox: TileBbox = (
                float(x), float(y),
                float(x + tile_size_m), float(y + tile_size_m),
            )
            tiles.append((tile_id, tile_bbox))
            y += tile_size_m
        x += tile_size_m
    return tiles


# ---------------------------------------------------------------------------
# SQLite index
# ---------------------------------------------------------------------------


@dataclass
class TileRecord:
    tile_id: str
    dataset: str
    bbox: TileBbox          # EPSG:3067 xmin, ymin, xmax, ymax
    path: Path
    source_url: str | None
    fetch_timestamp: str    # ISO 8601 UTC — use this for CC-BY "retrieved <date>"
    dataset_version: str | None
    checksum: str           # SHA-256 hex
    byte_size: int


def _db_path(cache_dir: str | Path) -> Path:
    return Path(cache_dir) / "index.sqlite"


def _init_db(db: Path) -> None:
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tiles (
                dataset          TEXT NOT NULL,
                tile_id          TEXT NOT NULL,
                xmin             REAL NOT NULL,
                ymin             REAL NOT NULL,
                xmax             REAL NOT NULL,
                ymax             REAL NOT NULL,
                source_url       TEXT,
                fetch_timestamp  TEXT NOT NULL,
                dataset_version  TEXT,
                file_path        TEXT NOT NULL,
                checksum         TEXT NOT NULL,
                byte_size        INTEGER NOT NULL,
                PRIMARY KEY (dataset, tile_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS parcels (
                parcel_id        TEXT NOT NULL,
                lpis_year        INTEGER NOT NULL,
                tunnus           INTEGER NOT NULL,
                area_ha          REAL NOT NULL,
                geometry_wkt     TEXT NOT NULL,
                fetch_timestamp  TEXT NOT NULL,
                PRIMARY KEY (parcel_id, lpis_year)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS properties (
                property_id      TEXT NOT NULL,
                display_id       TEXT NOT NULL,
                area_ha          REAL NOT NULL,
                geometry_wkt     TEXT NOT NULL,
                fetch_timestamp  TEXT NOT NULL,
                PRIMARY KEY (property_id)
            )
        """)
        conn.commit()


def _lookup(db: Path, dataset: str, tile_id: str) -> TileRecord | None:
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT tile_id, dataset, xmin, ymin, xmax, ymax, source_url, "
            "fetch_timestamp, dataset_version, file_path, checksum, byte_size "
            "FROM tiles WHERE dataset=? AND tile_id=?",
            (dataset, tile_id),
        ).fetchone()
    if row is None:
        return None
    return TileRecord(
        tile_id=row[0], dataset=row[1],
        bbox=(row[2], row[3], row[4], row[5]),
        source_url=row[6], fetch_timestamp=row[7],
        dataset_version=row[8], path=Path(row[9]),
        checksum=row[10], byte_size=row[11],
    )


def _register(db: Path, record: TileRecord) -> None:
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO tiles
               (dataset, tile_id, xmin, ymin, xmax, ymax, source_url,
                fetch_timestamp, dataset_version, file_path, checksum, byte_size)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (record.dataset, record.tile_id,
             record.bbox[0], record.bbox[1], record.bbox[2], record.bbox[3],
             record.source_url, record.fetch_timestamp, record.dataset_version,
             str(record.path), record.checksum, record.byte_size),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_ts_expired(fetch_timestamp: str, ttl_days: int) -> bool:
    fetched = datetime.fromisoformat(fetch_timestamp)
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - fetched).days
    return age_days > ttl_days


def _is_expired(record: TileRecord, ttl_days: int) -> bool:
    return _is_ts_expired(record.fetch_timestamp, ttl_days)


def _tile_lock(dataset: str, tile_id: str) -> threading.Lock:
    key = (dataset, tile_id)
    with _tile_locks_mu:
        if key not in _tile_locks:
            _tile_locks[key] = threading.Lock()
        return _tile_locks[key]


def _fetch_and_register(
    dataset: str,
    tile_id: str,
    tile_bbox: TileBbox,
    fetcher: FetcherFn,
    db: Path,
    cache_dir: Path,
) -> TileRecord:
    """Fetch one tile atomically and register it in the index.

    Write order: temp file → fsync → atomic rename → sqlite insert.
    An interrupted fetch never leaves a half-tile that looks registered.
    """
    ext = _DATASET_EXT.get(dataset, ".bin")
    tile_dir = cache_dir / dataset
    tile_dir.mkdir(parents=True, exist_ok=True)
    dest = tile_dir / f"{tile_id}{ext}"
    tmp = dest.with_suffix(".tmp")

    log.info("Fetching %s tile %s", dataset, tile_id)
    try:
        source_url, dataset_version = fetcher(tile_id, tile_bbox, tmp)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    # fsync before rename so the data is durable on crash
    with open(tmp, "rb") as f:
        os.fsync(f.fileno())
    os.replace(tmp, dest)  # atomic on POSIX

    checksum = _sha256(dest)
    byte_size = dest.stat().st_size
    fetch_ts = datetime.now(timezone.utc).isoformat()

    record = TileRecord(
        tile_id=tile_id, dataset=dataset, bbox=tile_bbox,
        path=dest, source_url=source_url, fetch_timestamp=fetch_ts,
        dataset_version=dataset_version, checksum=checksum, byte_size=byte_size,
    )
    _register(db, record)
    log.debug(
        "Cached %s tile %s (%d bytes, sha256=%s…)",
        dataset, tile_id, byte_size, checksum[:8],
    )
    return record


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_tiles(
    dataset: str,
    bbox: TileBbox,
    fetcher: FetcherFn,
    config: CacheConfig,
    *,
    refresh: bool = False,
) -> list[TileRecord]:
    """Return TileRecords for all grid tiles covering *bbox*.

    For each tile:
    - If cached and within TTL (and not *refresh*): return from index.
    - If missing, expired, or *refresh*: call *fetcher*, store atomically,
      register in the index.
    - If ``config.offline`` is True: never call fetcher; raise CacheMissError
      on any miss so the operator knows the area needs warming first.

    The returned TileRecord list can be passed directly to
    ``tile_provenance()`` to extract CC-BY attribution dates.
    """
    if dataset not in _DATASET_EXT:
        raise ValueError(f"Unknown dataset '{dataset}'; expected one of {list(_DATASET_EXT)}")

    ttl_map = {
        "dem": config.dem_ttl_days,
        "buildings": config.buildings_ttl_days,
        "powerlines": config.powerlines_ttl_days,
        "pylons": config.pylons_ttl_days,
    }
    ttl_days = ttl_map.get(dataset, config.buildings_ttl_days)
    cache_dir = Path(config.cache_dir)
    db = _db_path(cache_dir)
    _init_db(db)

    tiles = covering_tiles(bbox, config.tile_size_m)
    records: list[TileRecord] = []

    for tile_id, tile_bbox in tiles:
        with _tile_lock(dataset, tile_id):
            existing = _lookup(db, dataset, tile_id)
            needs_fetch = (
                existing is None
                or not existing.path.exists()
                or refresh
                or _is_expired(existing, ttl_days)
            )

            if needs_fetch:
                if config.offline:
                    raise CacheMissError(
                        f"Tile {dataset}/{tile_id} not in cache and offline=true. "
                        f"Run 'jobgen cache warm' to pre-fetch this area."
                    )
                record = _fetch_and_register(
                    dataset, tile_id, tile_bbox, fetcher, db, cache_dir
                )
                _ns.record_download(dataset, record.byte_size)
            else:
                log.debug("Cache hit: %s tile %s", dataset, tile_id)
                _ns.record_hit(dataset)
                record = existing  # type: ignore[assignment]

        records.append(record)

    return records


def tile_provenance(records: list[TileRecord]) -> dict:
    """Summarise tile provenance for inclusion in manifest.json.

    The CC-BY attribution "retrieved <date>" must reflect when the *tile*
    was fetched, not when the job ran.  Use fetch_date_min / fetch_date_max
    as the date range in the attribution string.
    """
    if not records:
        return {"tile_ids": [], "fetch_date_min": None, "fetch_date_max": None}
    dates = sorted(r.fetch_timestamp for r in records)
    return {
        "tile_ids": [r.tile_id for r in records],
        "fetch_date_min": dates[0],
        "fetch_date_max": dates[-1],
        "source_urls": list({r.source_url for r in records if r.source_url}),
        "dataset_versions": list({r.dataset_version for r in records if r.dataset_version}),
    }


# Parcel and property geometry caches live in geo_cache.py.
# They use the same SQLite file via _db_path/_init_db above.


def check_tile_exists(cache_config: "CacheConfig", dataset: str, tile_id: str) -> bool:
    """Return True if the tile is in the cache index and its file exists on disk."""
    db = _db_path(Path(cache_config.cache_dir))
    if not db.exists():
        return False
    record = _lookup(db, dataset, tile_id)
    return record is not None and record.path.exists()
