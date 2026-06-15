"""Tests for forecast.py — composition + per-folder caching (no network)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from flightmanager.config import SatellitesConfig, WeatherConfig
from flightmanager import forecast as fc
from flightmanager import satellites as sat
from flightmanager import weather as wx
from flightmanager.satellites import Overpass, OverpassResult
from flightmanager.weather import DayWeather

PTS = [(62.79, 22.84)]
NOW = datetime(2026, 6, 15, tzinfo=timezone.utc)


def _fake_overpass_result():
    return OverpassResult(
        tile_ids=["34VEQ"],
        overpasses=[Overpass(39084, "Landsat 8", "34VEQ",
                             datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc), 70.0)],
        grid_ok=True,
    )


def _fake_days():
    return [DayWeather("2026-06-15", 0, "clear", "Clear", 10.0, 20.0, 4.0, 0.0, 5.0)]


# ---------------------------------------------------------------------------
# Fingerprint + cache primitives
# ---------------------------------------------------------------------------


def test_fingerprint_stable_and_sensitive():
    a = fc._fingerprint(PTS, "2026-06-15")
    assert a == fc._fingerprint(PTS, "2026-06-15")            # stable
    assert a != fc._fingerprint(PTS, "2026-06-16")            # date matters
    assert a != fc._fingerprint([(60.0, 24.0)], "2026-06-15")  # points matter
    # Order-independent.
    two = [(62.79, 22.84), (60.0, 24.0)]
    assert fc._fingerprint(two, "x") == fc._fingerprint(list(reversed(two)), "x")


def test_cache_roundtrip_and_mismatch(tmp_path):
    p = tmp_path / ".forecast_cache.json"
    fc._write_cache(p, "fp1", {"hello": "world"})
    assert fc._read_cache(p, "fp1", 3) == {"hello": "world"}
    # Wrong fingerprint → miss.
    assert fc._read_cache(p, "fp2", 3) is None
    # Expired TTL → miss.
    import os, time
    old = time.time() - 4 * 3600
    os.utime(p, (old, old))
    assert fc._read_cache(p, "fp1", 3) is None


# ---------------------------------------------------------------------------
# build_forecast
# ---------------------------------------------------------------------------


def test_build_forecast_empty_centroids(tmp_path):
    res = fc.build_forecast([], SatellitesConfig(), WeatherConfig(), tmp_path, now=NOW)
    assert res["grid_ok"] is False
    assert res["days"] == []


def test_build_forecast_composes_and_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(sat, "overpasses_for_points",
                        lambda *a, **k: _fake_overpass_result())
    monkeypatch.setattr(sat, "load_grid", lambda *a, **k: None)
    monkeypatch.setattr(wx, "fetch_forecast", lambda *a, **k: _fake_days())

    res = fc.build_forecast(
        PTS, SatellitesConfig(), WeatherConfig(), tmp_path,
        folder_dir=tmp_path, now=NOW,
    )
    assert res["tile_ids"] == ["34VEQ"]
    assert res["grid_ok"] is True
    assert len(res["days"]) == 1
    assert len(res["days"][0]["satellites"]) == 1
    assert "Open-Meteo" in res["attribution"]["weather"]
    # Cache file written.
    assert (tmp_path / ".forecast_cache.json").exists()


def test_build_forecast_cache_short_circuits(tmp_path, monkeypatch):
    # Pre-write a valid cache entry matching the fingerprint.
    fp = fc._fingerprint(PTS, NOW.strftime("%Y-%m-%d"))
    payload = {"tile_ids": ["CACHED"], "days": [], "grid_ok": True}
    fc._write_cache(tmp_path / ".forecast_cache.json", fp, payload)

    def _boom(*a, **k):
        raise AssertionError("should not recompute on cache hit")

    monkeypatch.setattr(sat, "overpasses_for_points", _boom)
    monkeypatch.setattr(wx, "fetch_forecast", _boom)

    res = fc.build_forecast(
        PTS, SatellitesConfig(), WeatherConfig(), tmp_path,
        folder_dir=tmp_path, now=NOW,
    )
    assert res["tile_ids"] == ["CACHED"]
