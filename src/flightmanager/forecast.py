"""Forecast composition: satellite overpasses + weather → day-slots.

Combines :mod:`flightmanager.satellites` and :mod:`flightmanager.weather` into the
payload consumed by the map-view day-slot bar, and applies a per-folder disk cache
so that re-opening an unchanged folder does no recomputation.

Cache strategy
--------------
The payload is cached at ``<folder_dir>/.forecast_cache.json`` keyed by a
fingerprint of ``(rounded job centroids, UTC date)``.  Because the fingerprint is
computed from the centroids alone — no grid load, no orbit propagation, no network
— a cache hit returns instantly.  Cache validity is tied to the weather TTL
(``WeatherConfig.cache_max_age_hours``), the fastest-changing component; overpasses
change far more slowly and their OMM elements have their own multi-day disk cache.
A hit therefore requires: matching fingerprint (jobs unmoved, same day) **and**
freshness within the weather TTL.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from flightmanager.config import SatellitesConfig, WeatherConfig

log = logging.getLogger(__name__)

_CACHE_FILENAME = ".forecast_cache.json"
# Bump when the payload shape changes so stale per-folder caches are invalidated.
_CACHE_VERSION = 4


def _fingerprint(centroids: list[tuple[float, float]], day: str) -> str:
    """Stable hash of payload version + rounded centroids + date — cheap, no I/O."""
    rounded = sorted((round(lat, 3), round(lon, 3)) for lat, lon in centroids)
    payload = json.dumps(
        {"v": _CACHE_VERSION, "pts": rounded, "day": day}, sort_keys=True
    )
    return hashlib.sha1(payload.encode()).hexdigest()


def _read_cache(cache_path: Path, fingerprint: str, max_age_hours: int) -> dict | None:
    if not cache_path.exists():
        return None
    age_h = (time.time() - cache_path.stat().st_mtime) / 3600
    if age_h > max_age_hours:
        return None
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if cached.get("fingerprint") != fingerprint:
        return None
    return cached.get("payload")


def _write_cache(cache_path: Path, fingerprint: str, payload: dict) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(
                {"fingerprint": fingerprint, "payload": payload}, ensure_ascii=False
            ),
            encoding="utf-8",
        )
    except Exception as exc:  # caching is best-effort
        log.warning("Could not write forecast cache %s: %s", cache_path, exc)


def _representative_point(
    tile_centers_first: tuple[float, float] | None,
    centroids: list[tuple[float, float]],
) -> tuple[float, float]:
    """Pick the point to fetch weather for: a tile centre if available, else the
    mean of the job centroids."""
    if tile_centers_first is not None:
        return tile_centers_first
    lat = sum(c[0] for c in centroids) / len(centroids)
    lon = sum(c[1] for c in centroids) / len(centroids)
    return (lat, lon)


def build_forecast(
    centroids: list[tuple[float, float]],
    sat_cfg: SatellitesConfig,
    wx_cfg: WeatherConfig,
    cache_dir: str | Path,
    *,
    folder_dir: Path | None = None,
    now: datetime | None = None,
    session: requests.Session | None = None,
) -> dict:
    """Build the day-slot forecast payload for a set of job centroids.

    *folder_dir*, when given, enables the per-folder ``.forecast_cache.json``.
    Returns ``{generated_at, tile_ids, grid_ok, grid_msg, days, attribution}``.
    """
    from flightmanager import satellites as sat
    from flightmanager import weather as wx

    now = now or datetime.now(tz=timezone.utc)

    if not centroids:
        return {
            "generated_at": now.isoformat(),
            "tile_ids": [],
            "grid_ok": False,
            "grid_msg": "No jobs to forecast.",
            "days": [],
            "attribution": {},
        }

    cache_path = folder_dir / _CACHE_FILENAME if folder_dir else None
    fp = _fingerprint(centroids, now.strftime("%Y-%m-%d"))
    if cache_path is not None:
        hit = _read_cache(cache_path, fp, wx_cfg.cache_max_age_hours)
        if hit is not None:
            log.debug("Forecast cache hit: %s", cache_path)
            return hit

    # Overpasses (OMM disk-cached; grid loaded once per process).
    op_result = sat.overpasses_for_points(
        centroids, sat_cfg, cache_dir, start=now, session=session
    )

    # Weather per MGRS tile; the representative tile (most jobs) drives the day rows,
    # while each pass's clear-window is qualified by its own tile's cloud forecast.
    rep_weather, weather_by_tile, rep_tile = _resolve_weather(
        op_result, centroids, sat_cfg, wx_cfg, cache_dir, session
    )

    payload = {
        "generated_at": now.isoformat(),
        "tile_ids": op_result.tile_ids,
        "grid_ok": op_result.grid_ok,
        "grid_msg": op_result.grid_msg,
        "utc_offset_s": rep_weather.utc_offset_s,
        "daytime_window": [wx_cfg.daytime_start_h, wx_cfg.daytime_end_h],
        "weather_tile_id": rep_tile,
        "tiles": [
            {
                "id": tid,
                "center": list(op_result.tile_centers[tid]),
                "geometry": op_result.tile_geojson.get(tid),
            }
            for tid in sorted(op_result.tile_centers)
        ],
        "days": wx.build_day_slots(
            rep_weather,
            op_result.overpasses,
            daytime_start_h=wx_cfg.daytime_start_h,
            daytime_end_h=wx_cfg.daytime_end_h,
            clear_sky_max_cloud_pct=wx_cfg.clear_sky_max_cloud_pct,
            weather_by_tile=weather_by_tile,
            drone_wind_limit_ms=wx_cfg.drone_wind_limit_ms,
        ),
        "attribution": {
            "weather": wx.attribution(wx_cfg),
            "satellites": op_result.attribution,
        },
    }
    if wx_cfg.provider == "fmi":
        payload["weather_warning"] = (
            "FMI weather provider is not implemented yet — no weather shown. "
            "Switch to Open-Meteo in Settings."
        )

    if cache_path is not None:
        _write_cache(cache_path, fp, payload)
    return payload


def _resolve_weather(op_result, centroids, sat_cfg, wx_cfg, cache_dir, session):
    """Fetch weather for every MGRS tile and pick the representative one (most jobs).

    Returns ``(rep_weather, weather_by_tile, rep_tile_id)``. When the grid is missing
    (no tiles) weather is fetched once at the centroid mean.
    """
    from flightmanager import satellites as sat
    from flightmanager import weather as wx

    tile_centers = op_result.tile_centers
    if not tile_centers:
        rep_lat, rep_lon = _representative_point(None, centroids)
        return wx.fetch_forecast(rep_lat, rep_lon, wx_cfg, cache_dir, session), {}, None

    weather_by_tile = {
        tid: wx.fetch_forecast(clat, clon, wx_cfg, cache_dir, session)
        for tid, (clat, clon) in tile_centers.items()
    }
    # Representative tile = the one holding the most job centroids.
    grid = sat.load_grid(sat_cfg.grid_file)
    counts: dict[str, int] = {}
    if grid is not None:
        for lat, lon in centroids:
            tid = sat.tile_for_point(lat, lon, grid)
            if tid:
                counts[tid] = counts.get(tid, 0) + 1
    rep_tile = (
        max(tile_centers, key=lambda t: counts.get(t, 0))
        if counts
        else sorted(tile_centers)[0]
    )
    return weather_by_tile[rep_tile], weather_by_tile, rep_tile
