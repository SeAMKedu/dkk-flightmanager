"""Earth-observation satellite overpass computation.

Computes upcoming near-nadir overpasses of tracked EO satellites (Sentinel-2,
Landsat, …) over the Sentinel-2 MGRS tile(s) that a set of jobs falls in.

Data sources
------------
* **Orbital elements** — OMM (Orbit Mean-Elements Message) JSON from CelesTrak,
  fetched per NORAD id and cached under ``<cache_dir>/satellites/omm_<id>.json``
  with an mtime-based TTL (``SatellitesConfig.omm_max_age_days``).  Sun-synchronous
  EO orbits are stable, so multi-day-old elements are fine for a "which day"
  listing.  Propagated with Skyfield's SGP4 (``EarthSatellite.from_omm`` +
  ``find_events``); the timescale is the Skyfield builtin (no network download).

* **MGRS grid** — a static Sentinel-2 tiling-grid GeoJSON (tile id in the ``Name``
  property).  ~20 MB, NOT bundled: the user downloads it (see
  https://zenodo.org/records/10998972) and points ``SatellitesConfig.grid_file`` at
  it.  Loaded lazily once and indexed with a shapely STRtree.

Overpass logic
--------------
For each MGRS tile of interest, an observer is placed at the tile centre and
``find_events(..., altitude_degrees=min_elevation_deg)`` is run over the search
window.  Culmination events whose peak elevation exceeds the threshold are kept —
these are the near-nadir passes that yield usable imagery.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import flightmanager.net_stats as _ns
from flightmanager.config import SatellitesConfig

log = logging.getLogger(__name__)

# Cached grid index per resolved file path: path -> (STRtree, names, geoms, centers)
_GRID_CACHE: dict[str, "_GridIndex"] = {}


@dataclass
class _GridIndex:
    tree: object                      # shapely.strtree.STRtree
    geoms: list                       # parallel to tree input order
    names: list[str]                  # MGRS tile id per geom
    centers: dict[str, tuple[float, float]]  # tile_id -> (lat, lon) of centroid
    geom_by_name: dict[str, object]   # tile_id -> shapely geometry

    def geojson(self, tile_id: str) -> dict | None:
        """GeoJSON geometry (EPSG:4326) for *tile_id*, or None."""
        from shapely.geometry import mapping
        g = self.geom_by_name.get(tile_id)
        return mapping(g) if g is not None else None


@dataclass
class Overpass:
    norad_id: int
    name: str
    tile_id: str
    peak_utc: datetime
    max_elev_deg: float


@dataclass
class OverpassResult:
    tile_ids: list[str]
    overpasses: list[Overpass]
    grid_ok: bool
    grid_msg: str = ""
    tile_centers: dict[str, tuple[float, float]] = field(default_factory=dict)
    tile_geojson: dict[str, dict] = field(default_factory=dict)  # tile_id -> GeoJSON geometry
    attribution: str = (
        "Orbital data from CelesTrak. Sentinel-2 tiling grid: "
        "https://zenodo.org/records/10998972"
    )


# ---------------------------------------------------------------------------
# MGRS grid lookup
# ---------------------------------------------------------------------------


def load_grid(grid_path: str | Path) -> _GridIndex | None:
    """Load and index the MGRS tiling-grid GeoJSON (cached per path).

    Returns None if the file does not exist.
    """
    from shapely.geometry import shape
    from shapely.strtree import STRtree
    import json

    p = Path(grid_path)
    key = str(p.resolve()) if p.exists() else str(p)
    if key in _GRID_CACHE:
        return _GRID_CACHE[key]
    if not p.exists():
        log.warning("Sentinel-2 grid file not found: %s", p)
        return None

    log.info("Loading Sentinel-2 MGRS grid: %s", p)
    data = json.loads(p.read_text(encoding="utf-8"))
    geoms, names = [], []
    centers: dict[str, tuple[float, float]] = {}
    geom_by_name: dict[str, object] = {}
    for feat in data.get("features", []):
        name = (feat.get("properties") or {}).get("Name")
        if not name:
            continue
        geom = shape(feat["geometry"])
        geoms.append(geom)
        names.append(name)
        c = geom.centroid
        centers[name] = (c.y, c.x)  # (lat, lon)
        geom_by_name[name] = geom

    idx = _GridIndex(tree=STRtree(geoms), geoms=geoms, names=names,
                     centers=centers, geom_by_name=geom_by_name)
    _GRID_CACHE[key] = idx
    log.info("Indexed %d MGRS tiles", len(names))
    return idx


def tile_for_point(lat: float, lon: float, grid: _GridIndex) -> str | None:
    """Return the MGRS tile id whose polygon contains (lat, lon), or None.

    MGRS tiles overlap at their edges; if several contain the point the one whose
    centre is nearest is returned (most-central coverage).
    """
    from shapely.geometry import Point

    pt = Point(lon, lat)
    candidates = grid.tree.query(pt)  # indices of bbox-overlapping geoms
    hits: list[str] = []
    for i in candidates:
        i = int(i)
        if grid.geoms[i].contains(pt) or grid.geoms[i].intersects(pt):
            hits.append(grid.names[i])
    if not hits:
        return None
    if len(hits) == 1:
        return hits[0]
    # Multiple overlapping tiles — pick the one whose centre is closest.
    def _dist(name: str) -> float:
        clat, clon = grid.centers[name]
        return (clat - lat) ** 2 + (clon - lon) ** 2
    return min(hits, key=_dist)


def tiles_for_points(
    points_latlon: list[tuple[float, float]], grid: _GridIndex
) -> dict[str, tuple[float, float]]:
    """Map a set of (lat, lon) points to the MGRS tiles they fall in.

    Returns ``{tile_id: (centre_lat, centre_lon)}`` — deduplicated, so several
    jobs in the same tile collapse to one entry.
    """
    out: dict[str, tuple[float, float]] = {}
    for lat, lon in points_latlon:
        tid = tile_for_point(lat, lon, grid)
        if tid and tid not in out:
            out[tid] = grid.centers[tid]
    return out


def neighbor_tiles(grid: _GridIndex, tile_ids: set[str]) -> list[str]:
    """Return MGRS tiles adjacent to (intersecting) any of *tile_ids*, excluding
    the inputs themselves. Sentinel-2 tiles overlap, so the surrounding tiles
    geometrically intersect the job tile."""
    found: set[str] = set()
    for tid in tile_ids:
        g = grid.geom_by_name.get(tid)
        if g is None:
            continue
        for i in grid.tree.query(g):
            i = int(i)
            name = grid.names[i]
            if name in tile_ids or name in found:
                continue
            if grid.geoms[i].intersects(g):
                found.add(name)
    return sorted(found)


def tiles_with_neighbors(
    points_latlon: list[tuple[float, float]], cfg: SatellitesConfig
) -> dict:
    """Job tiles + their neighbours for the map-view 'MGRS tiles' overlay.

    Returns ``{grid_ok, grid_msg, tiles: [{id, geometry, center, is_job, job_count}]}``.
    Degrades gracefully (``grid_ok: False``) when the grid file is missing.
    """
    grid = load_grid(cfg.grid_file)
    if grid is None:
        return {"grid_ok": False,
                "grid_msg": f"Sentinel-2 grid file not found: {cfg.grid_file}",
                "tiles": []}

    counts: dict[str, int] = {}
    for lat, lon in points_latlon:
        tid = tile_for_point(lat, lon, grid)
        if tid:
            counts[tid] = counts.get(tid, 0) + 1

    job_ids = set(counts)
    tiles = [
        {"id": tid, "geometry": grid.geojson(tid),
         "center": list(grid.centers[tid]), "is_job": True, "job_count": counts[tid]}
        for tid in sorted(job_ids)
    ]
    for tid in neighbor_tiles(grid, job_ids):
        tiles.append({"id": tid, "geometry": grid.geojson(tid),
                      "center": list(grid.centers[tid]), "is_job": False, "job_count": 0})

    msg = "" if job_ids else "No MGRS tile matched the job location(s)."
    return {"grid_ok": True, "grid_msg": msg, "tiles": tiles}


# ---------------------------------------------------------------------------
# OMM fetch + cache
# ---------------------------------------------------------------------------


def fetch_omm(
    norad_ids: list[int],
    cfg: SatellitesConfig,
    cache_dir: str | Path,
    session: requests.Session | None = None,
) -> dict[int, dict]:
    """Fetch OMM orbital elements for *norad_ids*, one cache file per id.

    Returns ``{norad_id: omm_dict}`` for the ids that resolved (failures skipped).
    """
    out: dict[int, dict] = {}
    sess = session or requests.Session()
    owns_session = session is None
    cache_root = Path(cache_dir) / "satellites"
    import json

    try:
        for nid in norad_ids:
            cache_path = cache_root / f"omm_{nid}.json"
            if _cache_fresh(cache_path, cfg.omm_max_age_days):
                _ns.record_hit("satellites")
                out[nid] = json.loads(cache_path.read_text(encoding="utf-8"))
                continue
            omm = _fetch_one_omm(nid, cfg, sess)
            if omm is None:
                # Fall back to stale cache if present.
                if cache_path.exists():
                    log.warning("Using stale OMM cache for %d", nid)
                    out[nid] = json.loads(cache_path.read_text(encoding="utf-8"))
                continue
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(omm, ensure_ascii=False), encoding="utf-8")
            out[nid] = omm
    finally:
        if owns_session:
            sess.close()
    return out


# CelesTrak's gp.php has no multi-id query, so each NORAD is fetched separately and
# can intermittently time out. A couple of quick retries smooths cold-start drops
# (warm loads come from the per-id disk cache and never reach here).
_OMM_RETRIES = 2
_OMM_RETRY_BACKOFF_S = 1.0


def _fetch_one_omm(nid: int, cfg: SatellitesConfig, sess: requests.Session) -> dict | None:
    url = cfg.omm_url.format(catnr=nid)
    for attempt in range(_OMM_RETRIES + 1):
        try:
            log.info("Fetching OMM for NORAD %d from CelesTrak", nid)
            resp = sess.get(url, timeout=cfg.timeout_s)
            resp.raise_for_status()
            _ns.record_download("satellites", len(resp.content))
            data = resp.json()
        except Exception as exc:
            if attempt < _OMM_RETRIES:
                time.sleep(_OMM_RETRY_BACKOFF_S * (attempt + 1))
                continue
            log.error("Failed to fetch OMM for %d after %d attempts: %s",
                      nid, _OMM_RETRIES + 1, exc)
            return None
        if not isinstance(data, list) or not data:
            log.warning("CelesTrak returned no element set for NORAD %d", nid)
            return None
        return data[0]
    return None


def _cache_fresh(path: Path, max_age_days: int) -> bool:
    if not path.exists():
        return False
    age_days = (time.time() - path.stat().st_mtime) / 86_400
    return age_days <= max_age_days


# ---------------------------------------------------------------------------
# Overpass computation
# ---------------------------------------------------------------------------


def compute_overpasses(
    tile_centers: dict[str, tuple[float, float]],
    omm_by_id: dict[int, dict],
    names_by_id: dict[int, str],
    *,
    days_ahead: int,
    min_elev_deg: float,
    start: datetime | None = None,
) -> list[Overpass]:
    """Compute culmination overpasses above *min_elev_deg* for each tile.

    *tile_centers* maps tile id -> (lat, lon).  *omm_by_id* maps NORAD id -> OMM
    dict.  *names_by_id* maps NORAD id -> display name.  Returns a list of
    Overpass sorted by peak time.
    """
    from skyfield.api import load, wgs84, EarthSatellite

    ts = load.timescale(builtin=True)
    start = start or datetime.now(tz=timezone.utc)
    t0 = ts.from_datetime(start)
    t1 = ts.from_datetime(start + timedelta(days=days_ahead))

    sats: list[tuple[int, str, object]] = []
    for nid, omm in omm_by_id.items():
        try:
            sat = EarthSatellite.from_omm(ts, omm)
        except Exception as exc:
            log.error("Could not build satellite %d from OMM: %s", nid, exc)
            continue
        sats.append((nid, names_by_id.get(nid, str(nid)), sat))

    results: list[Overpass] = []
    for tile_id, (lat, lon) in tile_centers.items():
        observer = wgs84.latlon(lat, lon)
        for nid, name, sat in sats:
            try:
                times, events = sat.find_events(
                    observer, t0, t1, altitude_degrees=min_elev_deg
                )
            except Exception as exc:
                log.error("find_events failed for %s over %s: %s", name, tile_id, exc)
                continue
            for ti, ev in zip(times, events):
                if ev != 1:  # 1 == culmination (peak)
                    continue
                alt, _az, _dist = (sat - observer).at(ti).altaz()
                if alt.degrees < min_elev_deg:
                    continue
                results.append(Overpass(
                    norad_id=nid,
                    name=name,
                    tile_id=tile_id,
                    peak_utc=ti.utc_datetime(),
                    max_elev_deg=round(alt.degrees, 1),
                ))

    results.sort(key=lambda o: o.peak_utc)
    return results


# ---------------------------------------------------------------------------
# High-level convenience
# ---------------------------------------------------------------------------


def overpasses_for_points(
    points_latlon: list[tuple[float, float]],
    cfg: SatellitesConfig,
    cache_dir: str | Path,
    *,
    start: datetime | None = None,
    session: requests.Session | None = None,
) -> OverpassResult:
    """End-to-end: map points to MGRS tiles, fetch OMM, compute overpasses.

    Degrades gracefully when the grid file is missing or no tracked satellites
    are enabled — ``grid_ok`` / ``overpasses`` reflect what was available.
    """
    enabled = [s for s in cfg.tracked if s.enabled]
    grid = load_grid(cfg.grid_file)
    if grid is None:
        return OverpassResult(
            tile_ids=[], overpasses=[], grid_ok=False,
            grid_msg=f"Sentinel-2 grid file not found: {cfg.grid_file}",
        )

    tile_centers = tiles_for_points(points_latlon, grid)
    if not tile_centers:
        return OverpassResult(
            tile_ids=[], overpasses=[], grid_ok=True,
            grid_msg="No MGRS tile matched the job location(s).",
        )
    tile_geojson = {tid: grid.geojson(tid) for tid in tile_centers}
    if not enabled:
        return OverpassResult(
            tile_ids=sorted(tile_centers), overpasses=[], grid_ok=True,
            grid_msg="No tracked satellites enabled.",
            tile_centers=tile_centers, tile_geojson=tile_geojson,
        )

    names_by_id = {s.norad_id: s.name for s in enabled}
    omm_by_id = fetch_omm([s.norad_id for s in enabled], cfg, cache_dir, session)
    overpasses = compute_overpasses(
        tile_centers, omm_by_id, names_by_id,
        days_ahead=cfg.days_ahead, min_elev_deg=cfg.min_elevation_deg, start=start,
    )
    return OverpassResult(
        tile_ids=sorted(tile_centers), overpasses=overpasses, grid_ok=True,
        tile_centers=tile_centers, tile_geojson=tile_geojson,
    )
