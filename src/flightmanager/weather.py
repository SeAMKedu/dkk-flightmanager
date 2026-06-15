"""Daily weather forecast for the map-view day-slot bar.

Fetches a multi-day daily forecast for a point and normalises it to a list of
``DayWeather`` regardless of provider.  Open-Meteo (keyless JSON, up to 16 days)
is the default; an FMI Open Data adapter is stubbed for a later phase.

Forecasts are cached per rounded coordinate under ``<cache_dir>/weather/`` with a
short mtime TTL (``WeatherConfig.cache_max_age_hours``) — forecasts update through
the day and the public APIs are rate-limited.

WMO weather codes are mapped to icon keys (resolved client-side to inline SVG
weather symbols) so the frontend needs no weather-code knowledge.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import requests

import flightmanager.net_stats as _ns
from flightmanager.config import WeatherConfig

log = logging.getLogger(__name__)

# WMO weather code → (icon key, human label). Icon keys map to ic-wx-* SVG symbols.
_WMO: dict[int, tuple[str, str]] = {
    0:  ("clear",  "Clear"),
    1:  ("clear",  "Mainly clear"),
    2:  ("partly", "Partly cloudy"),
    3:  ("cloudy", "Overcast"),
    45: ("fog",    "Fog"),
    48: ("fog",    "Rime fog"),
    51: ("rain",   "Light drizzle"),
    53: ("rain",   "Drizzle"),
    55: ("rain",   "Dense drizzle"),
    56: ("rain",   "Freezing drizzle"),
    57: ("rain",   "Freezing drizzle"),
    61: ("rain",   "Light rain"),
    63: ("rain",   "Rain"),
    65: ("rain",   "Heavy rain"),
    66: ("rain",   "Freezing rain"),
    67: ("rain",   "Freezing rain"),
    71: ("snow",   "Light snow"),
    73: ("snow",   "Snow"),
    75: ("snow",   "Heavy snow"),
    77: ("snow",   "Snow grains"),
    80: ("rain",   "Rain showers"),
    81: ("rain",   "Rain showers"),
    82: ("rain",   "Violent rain showers"),
    85: ("snow",   "Snow showers"),
    86: ("snow",   "Heavy snow showers"),
    95: ("storm",  "Thunderstorm"),
    96: ("storm",  "Thunderstorm with hail"),
    99: ("storm",  "Thunderstorm with hail"),
}


def code_to_icon(code: int | None) -> tuple[str, str]:
    """Map a WMO weather code to an (icon_key, label) pair."""
    if code is None:
        return ("unknown", "Unknown")
    return _WMO.get(int(code), ("cloudy", f"Code {code}"))


@dataclass
class DayWeather:
    date: str          # ISO yyyy-mm-dd
    weather_code: int | None
    icon: str
    label: str
    t_min_c: float | None
    t_max_c: float | None
    wind_max_ms: float | None
    precip_mm: float | None
    cloud_pct: float | None


def attribution(cfg: WeatherConfig) -> str:
    if cfg.provider == "fmi":
        return "Weather data: Finnish Meteorological Institute (CC BY 4.0)."
    return "Weather data by Open-Meteo.com (CC BY 4.0)."


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def fetch_forecast(
    lat: float,
    lon: float,
    cfg: WeatherConfig,
    cache_dir: str | Path,
    session: requests.Session | None = None,
) -> list[DayWeather]:
    """Return a daily forecast for (lat, lon), cached per rounded coordinate.

    Returns an empty list on failure (with no cache fallback available).
    """
    # ~2 decimal places ≈ 1 km — coarse enough to share cache between nearby jobs.
    key_lat, key_lon = round(lat, 2), round(lon, 2)
    cache_path = Path(cache_dir) / "weather" / f"{cfg.provider}_{key_lat}_{key_lon}.json"

    if _cache_fresh(cache_path, cfg.cache_max_age_hours):
        _ns.record_hit("weather")
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
        return [DayWeather(**d) for d in raw]

    if cfg.provider == "fmi":
        days = _fetch_fmi(key_lat, key_lon, cfg, session)
    else:
        days = _fetch_open_meteo(key_lat, key_lon, cfg, session)

    if not days:
        # Serve stale cache if the live fetch failed but we have something.
        if cache_path.exists():
            log.warning("Using stale weather cache for %s,%s", key_lat, key_lon)
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
            return [DayWeather(**d) for d in raw]
        return []

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps([asdict(d) for d in days], ensure_ascii=False), encoding="utf-8"
    )
    return days


def _cache_fresh(path: Path, max_age_hours: int) -> bool:
    if not path.exists():
        return False
    age_h = (time.time() - path.stat().st_mtime) / 3600
    return age_h <= max_age_hours


# ---------------------------------------------------------------------------
# Open-Meteo adapter
# ---------------------------------------------------------------------------

_OM_DAILY = (
    "weather_code,temperature_2m_max,temperature_2m_min,"
    "wind_speed_10m_max,precipitation_sum,cloud_cover_mean"
)


def _fetch_open_meteo(
    lat: float, lon: float, cfg: WeatherConfig, session: requests.Session | None
) -> list[DayWeather]:
    sess = session or requests.Session()
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": _OM_DAILY,
        "wind_speed_unit": "ms",
        "timezone": "UTC",
        "forecast_days": cfg.forecast_days,
    }
    try:
        log.info("Fetching Open-Meteo forecast for %s,%s", lat, lon)
        resp = sess.get(cfg.open_meteo_url, params=params, timeout=cfg.timeout_s)
        resp.raise_for_status()
        _ns.record_download("weather", len(resp.content))
        data = resp.json()
    except Exception as exc:
        log.error("Open-Meteo fetch failed: %s", exc)
        return []
    return _parse_open_meteo(data)


def _parse_open_meteo(data: dict) -> list[DayWeather]:
    daily = data.get("daily") or {}
    dates = daily.get("time") or []

    def col(name: str) -> list:
        return daily.get(name) or [None] * len(dates)

    codes = col("weather_code")
    tmax = col("temperature_2m_max")
    tmin = col("temperature_2m_min")
    wind = col("wind_speed_10m_max")
    precip = col("precipitation_sum")
    cloud = col("cloud_cover_mean")

    out: list[DayWeather] = []
    for i, date in enumerate(dates):
        icon, label = code_to_icon(codes[i])
        out.append(DayWeather(
            date=date,
            weather_code=codes[i],
            icon=icon,
            label=label,
            t_min_c=tmin[i],
            t_max_c=tmax[i],
            wind_max_ms=wind[i],
            precip_mm=precip[i],
            cloud_pct=cloud[i],
        ))
    return out


# ---------------------------------------------------------------------------
# FMI adapter (stub — implemented in a later phase)
# ---------------------------------------------------------------------------


def _fetch_fmi(
    lat: float, lon: float, cfg: WeatherConfig, session: requests.Session | None
) -> list[DayWeather]:
    log.warning("FMI weather provider is not yet implemented; returning no forecast.")
    return []


# ---------------------------------------------------------------------------
# Day-slot merge (weather + satellite overpasses)
# ---------------------------------------------------------------------------


def build_day_slots(days: list[DayWeather], overpasses: list) -> list[dict]:
    """Merge weather days with satellite overpasses into per-day slots.

    *overpasses* is a list of ``satellites.Overpass``.  Weather days drive the
    slots; overpasses are attached to the matching calendar day (UTC).  Overpass
    days with no weather (beyond the forecast horizon) are appended as
    weather-less slots so passes are not lost.
    """
    slots: dict[str, dict] = {}
    order: list[str] = []

    for d in days:
        slots[d.date] = {
            "date": d.date,
            "weather": {
                "icon": d.icon,
                "label": d.label,
                "code": d.weather_code,
                "t_min_c": d.t_min_c,
                "t_max_c": d.t_max_c,
                "wind_max_ms": d.wind_max_ms,
                "precip_mm": d.precip_mm,
                "cloud_pct": d.cloud_pct,
            },
            "satellites": [],
        }
        order.append(d.date)

    for op in overpasses:
        day = op.peak_utc.strftime("%Y-%m-%d")
        if day not in slots:
            slots[day] = {"date": day, "weather": None, "satellites": []}
            order.append(day)
        slots[day]["satellites"].append({
            "name": op.name,
            "norad_id": op.norad_id,
            "tile_id": op.tile_id,
            "peak_utc": op.peak_utc.isoformat(),
            "max_elev_deg": op.max_elev_deg,
        })

    return [slots[d] for d in sorted(order)]
