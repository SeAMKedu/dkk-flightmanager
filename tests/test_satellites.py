"""Tests for satellites.py — overpass computation (no network)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from flightmanager.config import SatellitesConfig, TrackedSatellite
from flightmanager import satellites as sat

# A real Landsat 8 OMM element set (CelesTrak, epoch 2026-06-15). Propagation from
# this fixed epoch over a fixed window is deterministic regardless of wall-clock.
LANDSAT8_OMM = {
    "OBJECT_NAME": "LANDSAT 8",
    "OBJECT_ID": "2013-008A",
    "EPOCH": "2026-06-15T10:41:15.321408",
    "MEAN_MOTION": 14.5709343,
    "ECCENTRICITY": 0.0001359,
    "INCLINATION": 98.2302,
    "RA_OF_ASC_NODE": 236.6012,
    "ARG_OF_PERICENTER": 93.7033,
    "MEAN_ANOMALY": 266.432,
    "EPHEMERIS_TYPE": 0,
    "CLASSIFICATION_TYPE": "U",
    "NORAD_CAT_ID": 39084,
    "ELEMENT_SET_NO": 999,
    "REV_AT_EPOCH": 69764,
    "BSTAR": 7.0283e-05,
    "MEAN_MOTION_DOT": 2.71e-06,
    "MEAN_MOTION_DDOT": 0,
}

# Field area near Seinäjoki, Finland (lat, lon).
SEINAJOKI = (62.79, 22.84)


def _tiny_grid(tmp_path: Path) -> str:
    """Write a small grid GeoJSON: two square MGRS tiles around the test point."""
    fc = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"Name": "34VEQ"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [22.0, 62.5], [23.0, 62.5],
                        [23.0, 63.0], [22.0, 63.0], [22.0, 62.5],
                    ]],
                },
            },
            {
                "type": "Feature",
                "properties": {"Name": "35VML"},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [24.0, 60.0], [25.0, 60.0],
                        [25.0, 60.5], [24.0, 60.5], [24.0, 60.0],
                    ]],
                },
            },
        ],
    }
    p = tmp_path / "grid.geojson"
    p.write_text(json.dumps(fc), encoding="utf-8")
    sat._GRID_CACHE.clear()  # avoid cross-test cache bleed
    return str(p)


# ---------------------------------------------------------------------------
# Grid lookup
# ---------------------------------------------------------------------------


def test_tile_for_point_inside(tmp_path):
    grid = sat.load_grid(_tiny_grid(tmp_path))
    assert sat.tile_for_point(62.79, 22.84, grid) == "34VEQ"


def test_tile_for_point_outside_returns_none(tmp_path):
    grid = sat.load_grid(_tiny_grid(tmp_path))
    assert sat.tile_for_point(10.0, 10.0, grid) is None


def test_tiles_for_points_dedups(tmp_path):
    grid = sat.load_grid(_tiny_grid(tmp_path))
    tiles = sat.tiles_for_points([(62.79, 22.84), (62.6, 22.2), (60.2, 24.5)], grid)
    assert set(tiles) == {"34VEQ", "35VML"}
    # centre of 34VEQ ~ (62.75, 22.5)
    clat, clon = tiles["34VEQ"]
    assert 62.4 < clat < 63.1 and 22.0 < clon < 23.1


def test_load_grid_missing_file_returns_none(tmp_path):
    sat._GRID_CACHE.clear()
    assert sat.load_grid(tmp_path / "nope.geojson") is None


# ---------------------------------------------------------------------------
# OMM cache (no network)
# ---------------------------------------------------------------------------


def test_fetch_omm_uses_fresh_cache(tmp_path):
    cache_dir = tmp_path / "cache"
    (cache_dir / "satellites").mkdir(parents=True)
    (cache_dir / "satellites" / "omm_39084.json").write_text(
        json.dumps(LANDSAT8_OMM), encoding="utf-8"
    )
    cfg = SatellitesConfig()
    # No session passed and cache is fresh → must not hit network.
    out = sat.fetch_omm([39084], cfg, cache_dir, session=None)
    assert out[39084]["NORAD_CAT_ID"] == 39084


# ---------------------------------------------------------------------------
# Overpass computation (deterministic from fixed OMM + start)
# ---------------------------------------------------------------------------


def test_compute_overpasses_deterministic():
    start = datetime(2026, 6, 15, tzinfo=timezone.utc)
    ops = sat.compute_overpasses(
        {"34VEQ": SEINAJOKI},
        {39084: LANDSAT8_OMM},
        {39084: "Landsat 8"},
        days_ahead=7,
        min_elev_deg=60.0,
        start=start,
    )
    assert ops, "expected at least one Landsat 8 overpass over the tile in 7 days"
    for op in ops:
        assert op.tile_id == "34VEQ"
        assert op.norad_id == 39084
        assert op.max_elev_deg >= 60.0
        assert op.peak_utc >= start
    # sorted by time
    assert ops == sorted(ops, key=lambda o: o.peak_utc)


def test_compute_overpasses_threshold_excludes_low_passes():
    start = datetime(2026, 6, 15, tzinfo=timezone.utc)
    high = sat.compute_overpasses(
        {"34VEQ": SEINAJOKI}, {39084: LANDSAT8_OMM}, {39084: "L8"},
        days_ahead=7, min_elev_deg=85.0, start=start,
    )
    low = sat.compute_overpasses(
        {"34VEQ": SEINAJOKI}, {39084: LANDSAT8_OMM}, {39084: "L8"},
        days_ahead=7, min_elev_deg=30.0, start=start,
    )
    assert len(low) >= len(high)
    assert all(o.max_elev_deg >= 85.0 for o in high)


# ---------------------------------------------------------------------------
# High-level + graceful degradation
# ---------------------------------------------------------------------------


def test_overpasses_for_points_missing_grid(tmp_path):
    sat._GRID_CACHE.clear()
    cfg = SatellitesConfig(grid_file=str(tmp_path / "absent.geojson"))
    res = sat.overpasses_for_points([SEINAJOKI], cfg, tmp_path)
    assert res.grid_ok is False
    assert res.overpasses == []
    assert "not found" in res.grid_msg.lower()


def test_overpasses_for_points_no_satellites_enabled(tmp_path):
    cfg = SatellitesConfig(
        grid_file=_tiny_grid(tmp_path),
        tracked=[TrackedSatellite(norad_id=39084, name="Landsat 8", enabled=False)],
    )
    res = sat.overpasses_for_points([SEINAJOKI], cfg, tmp_path)
    assert res.grid_ok is True
    assert res.tile_ids == ["34VEQ"]
    assert res.overpasses == []


def test_overpasses_for_points_no_tile_match(tmp_path):
    cfg = SatellitesConfig(grid_file=_tiny_grid(tmp_path))
    res = sat.overpasses_for_points([(10.0, 10.0)], cfg, tmp_path)
    assert res.grid_ok is True
    assert res.tile_ids == []
    assert "no mgrs tile" in res.grid_msg.lower()


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------


def test_default_tracked_satellites():
    cfg = SatellitesConfig()
    ids = {s.norad_id for s in cfg.tracked}
    # Sentinel-2 trio + Landsat 8/9, verified ids.
    assert {40697, 42063, 60989, 39084, 49260} <= ids
    assert cfg.min_elevation_deg == 60.0
