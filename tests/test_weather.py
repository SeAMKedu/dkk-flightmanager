"""Tests for weather.py — adapters, mapping, day-slot merge (no network)."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone

from flightmanager.config import WeatherConfig
from flightmanager import weather as wx
from flightmanager.satellites import Overpass

# A trimmed Open-Meteo daily response.
OM_RESPONSE = {
    "daily_units": {"temperature_2m_max": "°C", "wind_speed_10m_max": "m/s"},
    "daily": {
        "time": ["2026-06-15", "2026-06-16", "2026-06-17"],
        "weather_code": [0, 61, 95],
        "temperature_2m_max": [22.0, 17.3, 15.0],
        "temperature_2m_min": [12.2, 11.4, 9.0],
        "wind_speed_10m_max": [5.3, 4.2, 8.0],
        "precipitation_sum": [0.0, 0.4, 12.0],
        "cloud_cover_mean": [10, 100, 90],
    },
}


def test_code_to_icon():
    assert wx.code_to_icon(0) == ("clear", "Clear")
    assert wx.code_to_icon(61)[0] == "rain"
    assert wx.code_to_icon(75)[0] == "snow"
    assert wx.code_to_icon(95)[0] == "storm"
    assert wx.code_to_icon(None) == ("unknown", "Unknown")
    # Unknown numeric code falls back to cloudy.
    assert wx.code_to_icon(123)[0] == "cloudy"


def test_parse_open_meteo():
    days = wx._parse_open_meteo(OM_RESPONSE)
    assert len(days) == 3
    assert days[0].date == "2026-06-15"
    assert days[0].icon == "clear"
    assert days[0].t_max_c == 22.0
    assert days[1].icon == "rain"
    assert days[2].icon == "storm"
    assert days[2].wind_max_ms == 8.0


def test_parse_open_meteo_missing_columns():
    # Only time present — other columns default to None, no crash.
    days = wx._parse_open_meteo({"daily": {"time": ["2026-06-15"]}})
    assert len(days) == 1
    assert days[0].weather_code is None
    assert days[0].icon == "unknown"


def test_fetch_forecast_uses_fresh_cache(tmp_path):
    cfg = WeatherConfig()
    cache_path = tmp_path / "weather" / "open-meteo_62.79_22.84.json"
    cache_path.parent.mkdir(parents=True)
    cached = [asdict(d) for d in wx._parse_open_meteo(OM_RESPONSE)]
    cache_path.write_text(json.dumps(cached), encoding="utf-8")

    # Fresh cache → no network (session=None would fail if it tried).
    days = wx.fetch_forecast(62.79, 22.84, cfg, tmp_path, session=None)
    assert [d.date for d in days] == ["2026-06-15", "2026-06-16", "2026-06-17"]


def test_attribution_switch():
    assert "Open-Meteo" in wx.attribution(WeatherConfig(provider="open-meteo"))
    assert "Meteorological" in wx.attribution(WeatherConfig(provider="fmi"))


def test_build_day_slots_merges_overpasses():
    days = wx._parse_open_meteo(OM_RESPONSE)
    ops = [
        Overpass(39084, "Landsat 8", "34VEQ",
                 datetime(2026, 6, 16, 10, 5, tzinfo=timezone.utc), 72.0),
        Overpass(40697, "Sentinel-2A", "34VEQ",
                 datetime(2026, 6, 16, 9, 30, tzinfo=timezone.utc), 80.0),
    ]
    slots = wx.build_day_slots(days, ops)
    assert [s["date"] for s in slots] == ["2026-06-15", "2026-06-16", "2026-06-17"]
    assert slots[0]["satellites"] == []
    assert len(slots[1]["satellites"]) == 2
    assert slots[1]["weather"]["icon"] == "rain"


def test_build_day_slots_appends_overpass_beyond_horizon():
    days = wx._parse_open_meteo(OM_RESPONSE)  # ends 2026-06-17
    ops = [Overpass(39084, "L8", "34VEQ",
                    datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc), 70.0)]
    slots = wx.build_day_slots(days, ops)
    assert slots[-1]["date"] == "2026-06-20"
    assert slots[-1]["weather"] is None
    assert len(slots[-1]["satellites"]) == 1


def test_fmi_stub_returns_empty(tmp_path):
    cfg = WeatherConfig(provider="fmi")
    assert wx._fetch_fmi(60.0, 24.0, cfg, None) == []
