"""Daily weather forecast for the map-view day-slot bar.

Fetches an hourly forecast for a point and aggregates it to one ``DayWeather`` per
local day over the **daytime window** only (06:00–18:00 by default) — night-time
conditions are irrelevant for aerial mapping. Open-Meteo (keyless JSON, up to 16
days) is the default; an FMI Open Data adapter is stubbed for a later phase.

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
from dataclasses import asdict, dataclass, field
from datetime import timedelta, timezone
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

# Icon severity for picking a representative daytime code (worst conditions win).
_SEVERITY = {"storm": 6, "snow": 5, "rain": 4, "fog": 3, "cloudy": 2,
             "partly": 1, "clear": 0, "unknown": -1}


def code_to_icon(code: int | None) -> tuple[str, str]:
    """Map a WMO weather code to an (icon_key, label) pair."""
    if code is None:
        return ("unknown", "Unknown")
    return _WMO.get(int(code), ("cloudy", f"Code {code}"))


@dataclass
class DayWeather:
    date: str          # ISO yyyy-mm-dd (local)
    weather_code: int | None
    icon: str
    label: str
    t_avg_c: float | None      # daytime average temperature
    wind_avg_ms: float | None  # daytime average wind speed
    precip_mm: float | None    # daytime total precipitation
    cloud_pct: float | None    # daytime average cloud cover


@dataclass
class WeatherResult:
    days: list[DayWeather] = field(default_factory=list)
    utc_offset_s: int = 0      # local time = UTC + this many seconds


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
) -> WeatherResult:
    """Return a daytime-aggregated forecast for (lat, lon), cached per coordinate.

    Returns an empty ``WeatherResult`` on failure (with no cache fallback).
    """
    # ~2 decimal places ≈ 1 km — coarse enough to share cache between nearby jobs.
    key_lat, key_lon = round(lat, 2), round(lon, 2)
    cache_path = Path(cache_dir) / "weather" / f"{cfg.provider}_{key_lat}_{key_lon}.json"

    if _cache_fresh(cache_path, cfg.cache_max_age_hours):
        cached = _load_cache(cache_path)
        if cached is not None:
            _ns.record_hit("weather")
            return cached

    if cfg.provider == "fmi":
        result = _fetch_fmi(key_lat, key_lon, cfg, session)
    else:
        result = _fetch_open_meteo(key_lat, key_lon, cfg, session)

    if not result.days:
        cached = _load_cache(cache_path)  # serve stale on failure if available
        if cached is not None:
            log.warning("Using stale weather cache for %s,%s", key_lat, key_lon)
            return cached
        return result

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({"days": [asdict(d) for d in result.days],
                    "utc_offset_s": result.utc_offset_s}, ensure_ascii=False),
        encoding="utf-8",
    )
    return result


def _load_cache(path: Path) -> WeatherResult | None:
    """Tolerantly load a cached WeatherResult; None on any schema/parse mismatch."""
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        days = [DayWeather(**d) for d in raw.get("days", [])]
        return WeatherResult(days=days, utc_offset_s=int(raw.get("utc_offset_s", 0)))
    except Exception:
        return None  # stale schema → treat as miss, re-fetch


def _cache_fresh(path: Path, max_age_hours: int) -> bool:
    if not path.exists():
        return False
    age_h = (time.time() - path.stat().st_mtime) / 3600
    return age_h <= max_age_hours


# ---------------------------------------------------------------------------
# Open-Meteo adapter (hourly → daytime aggregates)
# ---------------------------------------------------------------------------

_OM_HOURLY = "temperature_2m,weather_code,wind_speed_10m,precipitation,cloud_cover"


def _fetch_open_meteo(
    lat: float, lon: float, cfg: WeatherConfig, session: requests.Session | None
) -> WeatherResult:
    sess = session or requests.Session()
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": _OM_HOURLY,
        "wind_speed_unit": "ms",
        "timezone": "auto",  # hourly timestamps + utc_offset_seconds in local time
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
        return WeatherResult()
    return _parse_open_meteo(data, cfg.daytime_start_h, cfg.daytime_end_h)


def _parse_open_meteo(data: dict, start_h: int, end_h: int) -> WeatherResult:
    offset = int(data.get("utc_offset_seconds", 0))
    buckets, order = _bucket_daytime_hours(data.get("hourly") or {}, start_h, end_h)
    midday = (start_h + end_h) / 2  # weight the icon toward solar-noon hours

    days: list[DayWeather] = []
    for day in order:
        b = buckets[day]
        rep_code = _representative_code(b["c"], midday)
        icon, label = code_to_icon(rep_code)
        days.append(DayWeather(
            date=day,
            weather_code=rep_code,
            icon=icon,
            label=label,
            t_avg_c=_avg(b["t"]),
            wind_avg_ms=_avg(b["w"]),
            precip_mm=round(sum(b["p"]), 1) if b["p"] else None,
            cloud_pct=_avg(b["cl"]),
        ))
    return WeatherResult(days=days, utc_offset_s=offset)


def _bucket_daytime_hours(hourly: dict, start_h: int, end_h: int):
    """Group daytime hourly samples by local date → ({day: {t,c,w,p,cl}}, order)."""
    times = hourly.get("time") or []

    def col(name: str) -> list:
        return hourly.get(name) or [None] * len(times)

    series = {"t": col("temperature_2m"), "c": col("weather_code"),
              "w": col("wind_speed_10m"), "p": col("precipitation"),
              "cl": col("cloud_cover")}
    buckets: dict[str, dict[str, list]] = {}
    order: list[str] = []
    for i, t in enumerate(times):
        hour = int(t[11:13])
        if not (start_h <= hour < end_h):
            continue
        day = t[:10]
        if day not in buckets:
            buckets[day] = {k: [] for k in series}
            order.append(day)
        b = buckets[day]
        for k, vals in series.items():
            v = vals[i]
            if v is None:
                continue
            # Weather codes keep their hour so the icon can be weighted toward noon.
            b[k].append((hour, int(v)) if k == "c" else v)
    return buckets, order


def _avg(xs: list) -> float | None:
    return round(sum(xs) / len(xs), 1) if xs else None


def _representative_code(hour_codes: list[tuple[int, int]], midday: float = 12) -> int | None:
    """Pick the daytime weather icon, weighted toward solar-noon hours.

    *hour_codes* is a list of ``(local_hour, wmo_code)``. The representative code is
    the most severe among the hours closest to *midday* (±2 h) — so a drizzly dawn
    that clears by noon no longer dominates the day's icon. Falls back to the most
    severe across all daytime hours when nothing sits in the midday core.
    """
    if not hour_codes:
        return None
    core = [c for (h, c) in hour_codes if abs(h - midday) <= 2]
    pool = core or [c for (_h, c) in hour_codes]
    return max(pool, key=lambda c: _SEVERITY.get(code_to_icon(c)[0], 0))


# ---------------------------------------------------------------------------
# FMI adapter (stub — implemented in a later phase)
# ---------------------------------------------------------------------------


def _fetch_fmi(
    lat: float, lon: float, cfg: WeatherConfig, session: requests.Session | None
) -> WeatherResult:
    log.warning("FMI weather provider is not yet implemented; returning no forecast.")
    return WeatherResult()


# ---------------------------------------------------------------------------
# Day-slot merge (weather + satellite overpasses)
# ---------------------------------------------------------------------------


def build_day_slots(
    result: WeatherResult,
    overpasses: list,
    *,
    daytime_start_h: int,
    daytime_end_h: int,
    clear_sky_max_cloud_pct: int = 30,
) -> list[dict]:
    """Merge weather days with satellite overpasses into per-day slots.

    Overpasses are bucketed by **local** calendar day (using ``result.utc_offset_s``)
    so they align with the local weather days. Each pass is flagged ``daytime``
    (within the daytime window) — night passes are hidden behind a count marker in
    the UI — and daytime passes on a low-cloud day are flagged ``clear_window`` (a
    likely usable optical-imagery opportunity). Weather days drive the slots;
    overpass days beyond the forecast horizon are appended as weather-less slots so
    passes are not lost.
    """
    tz = timezone(timedelta(seconds=result.utc_offset_s))
    slots: dict[str, dict] = {}
    order: list[str] = []

    for d in result.days:
        slots[d.date] = {
            "date": d.date,
            "weather": {
                "icon": d.icon,
                "label": d.label,
                "code": d.weather_code,
                "t_avg_c": d.t_avg_c,
                "wind_avg_ms": d.wind_avg_ms,
                "precip_mm": d.precip_mm,
                "cloud_pct": d.cloud_pct,
            },
            "satellites": [],
        }
        order.append(d.date)

    for op in overpasses:
        local = op.peak_utc.astimezone(tz)
        day = local.strftime("%Y-%m-%d")
        if day not in slots:
            slots[day] = {"date": day, "weather": None, "satellites": []}
            order.append(day)
        daytime = daytime_start_h <= local.hour < daytime_end_h
        wx_day = slots[day]["weather"]
        cloud = wx_day["cloud_pct"] if wx_day else None
        clear_window = bool(daytime and cloud is not None and cloud <= clear_sky_max_cloud_pct)
        slots[day]["satellites"].append({
            "name": op.name,
            "norad_id": op.norad_id,
            "tile_id": op.tile_id,
            "peak_utc": op.peak_utc.isoformat(),
            "peak_local": local.isoformat(),
            "daytime": daytime,
            "clear_window": clear_window,
            "max_elev_deg": op.max_elev_deg,
        })

    # Keep each day's passes time-ordered (already global-sorted, but be explicit).
    for s in slots.values():
        s["satellites"].sort(key=lambda x: x["peak_local"])

    return [slots[d] for d in sorted(order)]
