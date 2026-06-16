"""Tests for weather.py — hourly daytime aggregation, mapping, merge (no network)."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone

import pytest

from flightmanager.config import WeatherConfig
from flightmanager import weather as wx
from flightmanager.weather import DayWeather, WeatherResult
from flightmanager.satellites import Overpass

# Open-Meteo hourly response (timezone=auto → local times + utc_offset_seconds).
# Daytime window [6, 18): hour 5 and hour 18 are deliberately excluded.
OM_RESPONSE = {
    "utc_offset_seconds": 10800,  # UTC+3 (Europe/Helsinki summer)
    "hourly": {
        "time": [
            "2026-06-15T05:00", "2026-06-15T06:00", "2026-06-15T12:00",
            "2026-06-15T17:00", "2026-06-15T18:00",
            "2026-06-16T08:00", "2026-06-16T14:00",
        ],
        "temperature_2m": [5, 10, 20, 16, 30, 12, 18],
        "weather_code":   [0, 3, 61, 3, 95, 0, 2],
        "wind_speed_10m": [1, 4, 6, 5, 9, 3, 7],
        "precipitation":  [0, 0, 0.5, 0, 5, 0, 0.2],
        "cloud_cover":    [10, 50, 80, 40, 90, 5, 30],
    },
}


def test_code_to_icon():
    assert wx.code_to_icon(0) == ("clear", "Clear")
    assert wx.code_to_icon(61)[0] == "rain"
    assert wx.code_to_icon(75)[0] == "snow"
    assert wx.code_to_icon(95)[0] == "storm"
    assert wx.code_to_icon(None) == ("unknown", "Unknown")
    assert wx.code_to_icon(123)[0] == "cloudy"


def test_parse_open_meteo_daytime_aggregation():
    res = wx._parse_open_meteo(OM_RESPONSE, 6, 18)
    assert res.utc_offset_s == 10800
    assert [d.date for d in res.days] == ["2026-06-15", "2026-06-16"]
    d0 = res.days[0]
    # hours 6,12,17 only (5 and 18 excluded): temps 10,20,16 → 15.3
    assert d0.t_avg_c == 15.3
    assert d0.wind_avg_ms == 5.0                 # (4+6+5)/3
    assert d0.precip_mm == 0.5                   # daytime sum (storm hour 18 excluded)
    assert d0.icon == "rain"                     # most severe daytime code (61)
    d1 = res.days[1]
    assert d1.icon == "partly"                   # codes 0,2 → partly
    assert d1.t_avg_c == 15.0


def test_parse_open_meteo_missing_columns():
    res = wx._parse_open_meteo(
        {"utc_offset_seconds": 0, "hourly": {"time": ["2026-06-15T12:00"]}}, 6, 18)
    assert len(res.days) == 1
    assert res.days[0].weather_code is None
    assert res.days[0].icon == "unknown"
    assert res.days[0].t_avg_c is None


def test_representative_code_picks_worst_in_core():
    # All near midday (12): worst severity wins.
    assert wx._representative_code([(11, 0), (12, 3), (13, 95), (12, 2)], 12) == 95
    assert wx._representative_code([(12, 0), (11, 1), (13, 2)], 12) == 2
    assert wx._representative_code([], 12) is None


def test_representative_code_weights_midday():
    # Storm at dawn but clear at noon → noon wins (dawn is outside the ±2 h core).
    assert wx._representative_code([(6, 95), (12, 0)], 12) == 0
    # Nothing in the midday core → fall back to worst across all daytime hours.
    assert wx._representative_code([(6, 95), (7, 0)], 12) == 95


def test_fetch_forecast_uses_fresh_cache(tmp_path):
    cfg = WeatherConfig()
    cache_path = tmp_path / "weather" / "open-meteo_62.79_22.84.json"
    cache_path.parent.mkdir(parents=True)
    res = wx._parse_open_meteo(OM_RESPONSE, 6, 18)
    cache_path.write_text(json.dumps(
        {"v": wx._CACHE_VERSION, "days": [asdict(d) for d in res.days],
         "utc_offset_s": res.utc_offset_s, "hourly_cloud": res.hourly_cloud}
    ), encoding="utf-8")

    # Fresh cache → no network (session=None would fail if it tried).
    got = wx.fetch_forecast(62.79, 22.84, cfg, tmp_path, session=None)
    assert [d.date for d in got.days] == ["2026-06-15", "2026-06-16"]
    assert got.utc_offset_s == 10800


def test_load_cache_tolerates_stale_schema(tmp_path):
    p = tmp_path / "w.json"
    p.write_text(json.dumps({"days": [{"old_field": 1}], "utc_offset_s": 0}), encoding="utf-8")
    assert wx._load_cache(p) is None  # schema mismatch → miss, will re-fetch


def test_attribution_switch():
    assert "Open-Meteo" in wx.attribution(WeatherConfig(provider="open-meteo"))
    assert "Meteorological" in wx.attribution(WeatherConfig(provider="fmi"))


def test_build_day_slots_merges_and_flags_daytime():
    res = wx._parse_open_meteo(OM_RESPONSE, 6, 18)  # offset +3h
    ops = [
        # 05:00Z → 08:00 local → daytime
        Overpass(40697, "Sentinel-2A", "34VEQ",
                 datetime(2026, 6, 15, 5, 0, tzinfo=timezone.utc), 80.0),
        # 16:00Z → 19:00 local → not daytime (>=18)
        Overpass(39084, "Landsat 8", "34VEQ",
                 datetime(2026, 6, 15, 16, 0, tzinfo=timezone.utc), 72.0),
    ]
    slots = wx.build_day_slots(res, ops, daytime_start_h=6, daytime_end_h=18)
    assert [s["date"] for s in slots] == ["2026-06-15", "2026-06-16"]
    sats = slots[0]["satellites"]
    assert len(sats) == 2
    # Time-ordered, with correct daytime flags.
    assert sats[0]["name"] == "Sentinel-2A" and sats[0]["daytime"] is True
    assert sats[1]["name"] == "Landsat 8" and sats[1]["daytime"] is False


def test_build_day_slots_appends_overpass_beyond_horizon():
    res = wx._parse_open_meteo(OM_RESPONSE, 6, 18)  # days end 2026-06-16
    ops = [Overpass(39084, "L8", "34VEQ",
                    datetime(2026, 6, 20, 10, 0, tzinfo=timezone.utc), 70.0)]
    slots = wx.build_day_slots(res, ops, daytime_start_h=6, daytime_end_h=18)
    assert slots[-1]["date"] == "2026-06-20"
    assert slots[-1]["weather"] is None
    assert len(slots[-1]["satellites"]) == 1


def test_build_day_slots_clear_window_flag():
    # 08:00Z → 11:00 local → daytime pass.
    # Pass at 08:00Z → 11:00 local. clear_window uses cloud AT 11:00, not the day avg.
    ops = [Overpass(40697, "Sentinel-2A", "34VEQ",
                    datetime(2026, 6, 15, 8, 0, tzinfo=timezone.utc), 85.0)]
    # Day average is cloudy (90) but the 11:00 hour is clear (10) → still a clear window.
    res = WeatherResult(
        days=[DayWeather("2026-06-15", 3, "cloudy", "Overcast", 15.0, 3.0, 0.0, 90.0)],
        utc_offset_s=10800, hourly_cloud={"2026-06-15T11": 10.0},
    )
    s = wx.build_day_slots(res, ops, daytime_start_h=6, daytime_end_h=18,
                           clear_sky_max_cloud_pct=30)
    assert s[0]["satellites"][0]["clear_window"] is True
    assert s[0]["satellites"][0]["cloud_at_pass"] == 10

    # Same day average, but the 11:00 hour is overcast → not a clear window.
    res2 = WeatherResult(
        days=res.days, utc_offset_s=10800, hourly_cloud={"2026-06-15T11": 95.0})
    s2 = wx.build_day_slots(res2, ops, daytime_start_h=6, daytime_end_h=18,
                            clear_sky_max_cloud_pct=30)
    assert s2[0]["satellites"][0]["clear_window"] is False


def test_build_day_slots_night_pass_not_clear():
    # 21:00Z → 00:00 local next day → night, never a clear window even if cloud low.
    ops = [Overpass(40697, "S2A", "34VEQ",
                    datetime(2026, 6, 15, 21, 0, tzinfo=timezone.utc), 85.0)]
    res = WeatherResult(days=[DayWeather("2026-06-16", 0, "clear", "Clear",
                                         15.0, 3.0, 0.0, 5.0)], utc_offset_s=10800,
                        hourly_cloud={"2026-06-16T00": 5.0})
    slots = wx.build_day_slots(res, ops, daytime_start_h=6, daytime_end_h=18,
                               clear_sky_max_cloud_pct=30)
    pass_ = slots[0]["satellites"][0]
    assert pass_["daytime"] is False
    assert pass_["clear_window"] is False


def test_build_day_slots_golden_day():
    # Clear-window pass (11:00 local, low cloud) + flyable weather (low wind, fair sky).
    ops = [Overpass(40697, "S2A", "34VEQ",
                    datetime(2026, 6, 15, 8, 0, tzinfo=timezone.utc), 85.0)]
    flyable = WeatherResult(
        days=[DayWeather("2026-06-15", 1, "partly", "Partly", 15.0, 4.0, 0.0, 20.0)],
        utc_offset_s=10800, hourly_cloud={"2026-06-15T11": 10.0})
    windy = WeatherResult(
        days=[DayWeather("2026-06-15", 1, "partly", "Partly", 15.0, 14.0, 0.0, 20.0)],
        utc_offset_s=10800, hourly_cloud={"2026-06-15T11": 10.0})
    s_golden = wx.build_day_slots(flyable, ops, daytime_start_h=6, daytime_end_h=18,
                                  clear_sky_max_cloud_pct=30, drone_wind_limit_ms=10.0)
    s_windy = wx.build_day_slots(windy, ops, daytime_start_h=6, daytime_end_h=18,
                                 clear_sky_max_cloud_pct=30, drone_wind_limit_ms=10.0)
    assert s_golden[0]["golden"] is True
    assert s_windy[0]["golden"] is False        # clear sky but too windy to fly


def test_build_day_slots_per_tile_cloud():
    # Two tiles, two passes at the same hour; each qualified by its OWN tile's cloud.
    ops = [
        Overpass(40697, "S2A", "34VEQ", datetime(2026, 6, 15, 8, 0, tzinfo=timezone.utc), 85.0),
        Overpass(42063, "S2B", "35VML", datetime(2026, 6, 15, 8, 0, tzinfo=timezone.utc), 85.0),
    ]
    rep = WeatherResult(days=[DayWeather("2026-06-15", 0, "clear", "Clear",
                                         15.0, 3.0, 0.0, 10.0)], utc_offset_s=10800,
                        hourly_cloud={"2026-06-15T11": 10.0})
    other = WeatherResult(days=[], utc_offset_s=10800,
                          hourly_cloud={"2026-06-15T11": 90.0})
    slots = wx.build_day_slots(rep, ops, daytime_start_h=6, daytime_end_h=18,
                               clear_sky_max_cloud_pct=30,
                               weather_by_tile={"34VEQ": rep, "35VML": other})
    sats = {s["tile_id"]: s for s in slots[0]["satellites"]}
    assert sats["34VEQ"]["clear_window"] is True    # clear over its tile
    assert sats["35VML"]["clear_window"] is False    # cloudy over its tile


def test_fmi_stub_returns_empty(tmp_path):
    cfg = WeatherConfig(provider="fmi")
    res = wx._fetch_fmi(60.0, 24.0, cfg, None)
    assert res.days == []


def test_hourly_cloud_parse_and_lookup():
    res = wx._parse_open_meteo(OM_RESPONSE, 6, 18)
    # cloud_cover at "2026-06-15T12" was 80 in the fixture.
    assert res.hourly_cloud.get("2026-06-15T12") == 80
    from datetime import datetime as _dt
    assert res.cloud_at(_dt(2026, 6, 15, 12)) == 80


# ---------------------------------------------------------------------------
# Integration (live network — run with: pytest -m integration)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_open_meteo_live(tmp_path):
    """Open-Meteo returns a daytime-aggregated forecast with hourly cloud."""
    cfg = WeatherConfig(forecast_days=3)
    res = wx.fetch_forecast(62.79, 22.84, cfg, tmp_path)
    assert res.days, "expected at least one forecast day"
    assert res.utc_offset_s != 0  # Europe/Helsinki is offset from UTC
    assert res.hourly_cloud       # populated for pass-time lookups
    d0 = res.days[0]
    assert d0.t_avg_c is not None and d0.wind_avg_ms is not None
