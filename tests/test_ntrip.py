"""Tests for geo/ntrip.py — sourcetable parsing, distances, cache, payloads (no network)."""

from __future__ import annotations

import json
import time

import pytest

from flightmanager.config import RtkConfig, RtkNetworkConfig
from flightmanager.geo import ntrip

# Real-shaped sourcetable: NTRIP 2.0 casters return this over plain HTTP.
# One Seinäjoki station, one Helsinki station, one junk STR (no coords), one
# 0/0 placeholder, plus CAS/NET lines that must be ignored.
SOURCETABLE = "\r\n".join(
    [
        "SOURCETABLE 200 OK",
        "Server: NTRIP Caster 2.0/2.0",
        "",
        "CAS;rtk2go.com;2101;RTK2go;SNIP;0;FIN;62.0;25.0;http://rtk2go.com",
        "NET;SNIP;RTK2go;B;N;http://rtk2go.com;none;none;none",
        "STR;SeAMK;Seinajoki;RTCM 3.2;1005(1),1077(1);2;GPS+GLO+GAL+BDS;SNIP;FIN;62.7876;22.8504;1;0;sNTRIP;none;B;N;15200;",
        "STR;Kumpula;Helsinki;RTCM 3.3;1005(1);2;GPS+GLO;SNIP;FIN;60.2040;24.9610;1;0;sNTRIP;none;B;N;15200;",
        "STR;BadEntry;NoCoords;RTCM 3.2;1005(1);2;GPS;SNIP;FIN;xx;yy;1;0;sNTRIP;none;B;N;15200;",
        "STR;Nowhere;NullIsland;RTCM 3.2;1005(1);2;GPS;SNIP;XXX;0.0;0.0;1;0;sNTRIP;none;B;N;15200;",
        "ENDSOURCETABLE",
    ]
)

SEINAJOKI = (62.79, 22.84)


def _cfg(**kw) -> RtkConfig:
    return RtkConfig(
        networks=[
            RtkNetworkConfig(
                name="testnet", caster_url="example.com:2101", color="#123456"
            )
        ],
        **kw,
    )


# ── parsing ───────────────────────────────────────────────────────────────────


def test_parse_sourcetable_keeps_valid_str_entries_only():
    stations = ntrip.parse_sourcetable(SOURCETABLE, "testnet")
    assert [s.mountpoint for s in stations] == ["SeAMK", "Kumpula"]
    s = stations[0]
    assert s.network == "testnet"
    assert s.identifier == "Seinajoki"
    assert s.format == "RTCM 3.2"
    assert s.nav_system == "GPS+GLO+GAL+BDS"
    assert s.country == "FIN"
    assert s.lat == pytest.approx(62.7876)
    assert s.lon == pytest.approx(22.8504)


def test_caster_host_port_forms():
    assert ntrip.caster_host_port("http://rtk2go.com:2101") == ("rtk2go.com", 2101)
    assert ntrip.caster_host_port("crtk.net:2101") == ("crtk.net", 2101)
    assert ntrip.caster_host_port("caster.example.com") == ("caster.example.com", 2101)


def test_haversine_seinajoki_helsinki():
    # Seinäjoki–Helsinki ≈ 300 km
    d = ntrip.haversine_km(62.7876, 22.8504, 60.2040, 24.9610)
    assert 290 < d < 320


# ── cache ─────────────────────────────────────────────────────────────────────


def test_fetch_stations_uses_fresh_cache_without_network(tmp_path, monkeypatch):
    cfg = _cfg()
    net = cfg.networks[0]

    def _boom(*a, **kw):
        raise AssertionError("network must not be touched on a fresh cache")

    monkeypatch.setattr(ntrip, "_fetch_v2", _boom)
    monkeypatch.setattr(ntrip, "_fetch_v1", _boom)

    path = ntrip._cache_path(tmp_path, net.name)
    path.parent.mkdir(parents=True)
    stations = ntrip.parse_sourcetable(SOURCETABLE, net.name)
    path.write_text(
        json.dumps(
            {
                "v": ntrip._CACHE_VERSION,
                "fetched_at": "2026-07-06T10:00:00+00:00",
                "stations": [s.__dict__ for s in stations],
            }
        )
    )

    got, fetched_at, error = ntrip.fetch_stations(net, tmp_path, cfg)
    assert [s.mountpoint for s in got] == ["SeAMK", "Kumpula"]
    assert fetched_at == "2026-07-06T10:00:00+00:00"
    assert error is None


def test_fetch_stations_refetches_expired_cache_and_writes(tmp_path, monkeypatch):
    cfg = _cfg()
    net = cfg.networks[0]
    monkeypatch.setattr(ntrip, "_fetch_v2", lambda n, t: SOURCETABLE)

    path = ntrip._cache_path(tmp_path, net.name)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"v": ntrip._CACHE_VERSION, "fetched_at": "old", "stations": []})
    )
    old = time.time() - (cfg.cache_max_age_hours + 1) * 3600
    import os

    os.utime(path, (old, old))

    got, fetched_at, error = ntrip.fetch_stations(net, tmp_path, cfg)
    assert len(got) == 2 and error is None
    assert fetched_at != "old"
    assert json.loads(path.read_text())["fetched_at"] == fetched_at


def test_fetch_stations_serves_stale_cache_on_failure(tmp_path, monkeypatch):
    cfg = _cfg()
    net = cfg.networks[0]

    def _fail(*a, **kw):
        raise RuntimeError("caster down")

    monkeypatch.setattr(ntrip, "_fetch_v2", _fail)
    monkeypatch.setattr(ntrip, "_fetch_v1", _fail)

    path = ntrip._cache_path(tmp_path, net.name)
    path.parent.mkdir(parents=True)
    stations = ntrip.parse_sourcetable(SOURCETABLE, net.name)
    path.write_text(
        json.dumps(
            {
                "v": ntrip._CACHE_VERSION,
                "fetched_at": "2026-07-05T10:00:00+00:00",
                "stations": [s.__dict__ for s in stations],
            }
        )
    )
    old = time.time() - (cfg.cache_max_age_hours + 1) * 3600
    import os

    os.utime(path, (old, old))

    got, fetched_at, error = ntrip.fetch_stations(net, tmp_path, cfg)
    assert [s.mountpoint for s in got] == ["SeAMK", "Kumpula"]  # stale but served
    assert fetched_at == "2026-07-05T10:00:00+00:00"
    assert error is not None


def test_fetch_stations_total_failure_returns_error(tmp_path, monkeypatch):
    cfg = _cfg()

    def _fail(*a, **kw):
        raise RuntimeError("caster down")

    monkeypatch.setattr(ntrip, "_fetch_v2", _fail)
    monkeypatch.setattr(ntrip, "_fetch_v1", _fail)
    got, fetched_at, error = ntrip.fetch_stations(cfg.networks[0], tmp_path, cfg)
    assert got == [] and fetched_at == "" and "caster down" in error


# ── payloads ──────────────────────────────────────────────────────────────────


def test_stations_near_filters_and_sorts(tmp_path, monkeypatch):
    cfg = _cfg(search_radius_km=100.0)
    monkeypatch.setattr(ntrip, "_fetch_v2", lambda n, t: SOURCETABLE)

    payload = ntrip.stations_near([SEINAJOKI], cfg, tmp_path)
    # Kumpula (~300 km) is beyond the 100 km search radius.
    assert [s["mountpoint"] for s in payload["stations"]] == ["SeAMK"]
    st = payload["stations"][0]
    assert st["dist_km"] < 2.0
    assert st["color"] == "#123456"
    net = payload["networks"][0]
    assert net["name"] == "testnet"
    assert net["caster_host"] == "example.com"
    assert net["caster_port"] == 2101
    assert net["station_count"] == 1
    assert payload["circle_radius_km"] == cfg.circle_radius_km


def test_stations_near_skips_disabled_network(tmp_path, monkeypatch):
    cfg = _cfg()
    cfg.networks[0].enabled = False

    def _boom(*a, **kw):
        raise AssertionError("disabled network must not be fetched")

    monkeypatch.setattr(ntrip, "_fetch_v2", _boom)
    payload = ntrip.stations_near([SEINAJOKI], cfg, tmp_path)
    assert payload["networks"] == [] and payload["stations"] == []


def test_recommend_for_point_nearest_and_alternatives(tmp_path, monkeypatch):
    cfg = _cfg(search_radius_km=500.0)
    monkeypatch.setattr(ntrip, "_fetch_v2", lambda n, t: SOURCETABLE)
    payload = ntrip.stations_near([SEINAJOKI], cfg, tmp_path)

    # From Seinäjoki: nearest is SeAMK; Kumpula is ~300 km so not an alternative.
    nearest, alts = ntrip.recommend_for_point(payload, *SEINAJOKI, 20.0)
    assert nearest["mountpoint"] == "SeAMK"
    assert alts == []

    # From a point between the two, both rank; with a huge radius Kumpula qualifies.
    nearest, alts = ntrip.recommend_for_point(payload, 61.5, 23.9, 500.0)
    assert nearest is not None
    assert {nearest["mountpoint"]} | {a["mountpoint"] for a in alts} == {
        "SeAMK",
        "Kumpula",
    }


def test_recommend_for_point_empty_payload():
    assert ntrip.recommend_for_point({"stations": []}, 62.0, 22.0, 20.0) == (None, [])
