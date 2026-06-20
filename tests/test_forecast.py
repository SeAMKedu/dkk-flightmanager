"""Tests for forecast.py — composition + per-folder caching (no network)."""

from __future__ import annotations

from datetime import datetime, timezone


from flightmanager.config import SatellitesConfig, WeatherConfig
from flightmanager import forecast as fc
from flightmanager import satellites as sat
from flightmanager import weather as wx
from flightmanager.satellites import Overpass, OverpassResult
from flightmanager.weather import DayWeather, WeatherResult

PTS = [(62.79, 22.84)]
NOW = datetime(2026, 6, 15, tzinfo=timezone.utc)


def _fake_overpass_result():
    return OverpassResult(
        tile_ids=["34VEQ"],
        overpasses=[
            Overpass(
                39084,
                "Landsat 8",
                "34VEQ",
                datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc),
                70.0,
            )
        ],
        grid_ok=True,
    )


def _fake_weather():
    return WeatherResult(
        days=[DayWeather("2026-06-15", 0, "clear", "Clear", 15.0, 4.0, 0.0, 5.0)],
        utc_offset_s=10800,
    )


# ---------------------------------------------------------------------------
# Fingerprint + cache primitives
# ---------------------------------------------------------------------------


def test_fingerprint_stable_and_sensitive():
    a = fc._fingerprint(PTS, "2026-06-15")
    assert a == fc._fingerprint(PTS, "2026-06-15")  # stable
    assert a != fc._fingerprint(PTS, "2026-06-16")  # date matters
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
    import os
    import time

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
    monkeypatch.setattr(
        sat, "overpasses_for_points", lambda *a, **k: _fake_overpass_result()
    )
    monkeypatch.setattr(sat, "load_grid", lambda *a, **k: None)
    monkeypatch.setattr(wx, "fetch_forecast", lambda *a, **k: _fake_weather())

    res = fc.build_forecast(
        PTS,
        SatellitesConfig(),
        WeatherConfig(),
        tmp_path,
        folder_dir=tmp_path,
        now=NOW,
    )
    assert res["tile_ids"] == ["34VEQ"]
    assert res["grid_ok"] is True
    assert len(res["days"]) == 1
    assert len(res["days"][0]["satellites"]) == 1
    assert "Open-Meteo" in res["attribution"]["weather"]
    # Cache file written.
    assert (tmp_path / ".forecast_cache.json").exists()


def test_forecast_endpoint_routes_and_gathers_centroids(tmp_path, monkeypatch):
    """The /api/forecast handler resolves centroids and delegates to build_forecast."""
    from fastapi.testclient import TestClient
    from flightmanager.config import load_config
    from flightmanager.server import create_app

    cfg = load_config("config.example.toml")
    cfg.output.output_dir = str(tmp_path)  # empty → no centroids, no network

    captured = {}

    def fake_build(centroids, sat_cfg, wx_cfg, cache_dir, **kw):
        captured["centroids"] = centroids
        captured["folder_dir"] = kw.get("folder_dir")
        return {"grid_ok": True, "tile_ids": [], "days": []}

    monkeypatch.setattr("flightmanager.forecast.build_forecast", fake_build)

    app = create_app(cfg)
    with TestClient(app) as client:
        r = client.get("/api/forecast")
        assert r.status_code == 200
        assert r.json()["grid_ok"] is True
    assert captured["centroids"] == []  # empty output dir
    assert str(captured["folder_dir"]) == str(tmp_path)


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
        PTS,
        SatellitesConfig(),
        WeatherConfig(),
        tmp_path,
        folder_dir=tmp_path,
        now=NOW,
    )
    assert res["tile_ids"] == ["CACHED"]
