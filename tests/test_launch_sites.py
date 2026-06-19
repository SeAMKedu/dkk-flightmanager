"""Unit tests for launch-site clustering (no network, pure geometry)."""

from __future__ import annotations

import math

from flightmanager.launch_sites import cluster_jobs

# A point in central Finland; metre→degree offsets are good enough for ~hundreds of m.
LAT0, LON0 = 62.0, 23.0
_M_PER_DEG_LAT = 111_320.0
_M_PER_DEG_LON = 111_320.0 * math.cos(math.radians(LAT0))


def _lon(dx_m: float) -> float:
    return LON0 + dx_m / _M_PER_DEG_LON


def _lat(dy_m: float) -> float:
    return LAT0 + dy_m / _M_PER_DEG_LAT


def _card(name: str, sort_order, dx_m: float, dy_m: float, *, half=10.0, **extra) -> dict:
    """A job card whose takeoff + square polygon sit at (dx_m, dy_m) from origin."""
    lon, lat = _lon(dx_m), _lat(dy_m)
    ring = [
        [_lon(dx_m - half), _lat(dy_m - half)],
        [_lon(dx_m + half), _lat(dy_m - half)],
        [_lon(dx_m + half), _lat(dy_m + half)],
        [_lon(dx_m - half), _lat(dy_m + half)],
        [_lon(dx_m - half), _lat(dy_m - half)],
    ]
    card = {
        "path": name, "name": name, "sort_order": sort_order,
        "takeoff_point_4326": [lon, lat],
        "_geometry": {"type": "Polygon", "coordinates": [ring]},
        "flight_time_min": 5.0,
    }
    card.update(extra)
    return card


def test_close_jobs_form_one_site_far_job_splits():
    cards = [
        _card("a", 0, 0, 0),
        _card("b", 1, 20, 0),
        _card("c", 2, 0, 25),
        _card("d", 3, 300, 0),   # ~300 m away → new site
    ]
    sites = cluster_jobs(cards)
    assert [s.member_count for s in sites] == [3, 1]
    assert sites[0].job_names == ["a", "b", "c"]
    assert sites[1].job_names == ["d"]
    assert [s.index for s in sites] == [1, 2]


def test_later_job_near_earlier_site_is_its_own_site():
    # Visit two pairs far apart, then return near the first pair much later.
    cards = [
        _card("a", 0, 0, 0),
        _card("b", 1, 15, 0),
        _card("c", 2, 400, 0),
        _card("d", 3, 415, 0),
        _card("e", 4, 5, 10),     # physically near a/b but visited last → own site
    ]
    sites = cluster_jobs(cards)
    assert [s.member_count for s in sites] == [2, 2, 1]
    assert sites[2].job_names == ["e"]


def test_centroid_rule_prevents_drift():
    # Each consecutive takeoff is 40 m from the last but the chain spans 120 m.
    # Centroid rule must split once the running centroid drifts past 50 m.
    cards = [_card(str(i), i, 40 * i, 0) for i in range(4)]
    sites = cluster_jobs(cards)
    assert len(sites) >= 2
    # No single site may span more than ~100 m end-to-end.
    assert all(s.radius_m < 120 for s in sites)


def test_skipped_and_takeoffless_jobs_excluded():
    cards = [
        _card("a", 0, 0, 0),
        _card("b", 1, 10, 0, skipped=True),
        {"path": "c", "name": "c", "sort_order": 2, "takeoff_point_4326": None,
         "_geometry": None},
    ]
    sites = cluster_jobs(cards)
    assert len(sites) == 1
    assert sites[0].job_names == ["a"]


def test_site_carries_circle_and_summed_time():
    cards = [_card("a", 0, 0, 0, half=10), _card("b", 1, 20, 0, half=10)]
    sites = cluster_jobs(cards)
    s = sites[0]
    assert s.radius_m > 0
    assert s.diameter_m == 2 * s.radius_m
    assert s.flight_time_min == 10.0          # 5 + 5
    assert len(s.circle_center_4326) == 2
    assert len(s.dot_4326) == 2


def test_max_altitude_is_max_over_members():
    cards = [_card("a", 0, 0, 0, height_m=80), _card("b", 1, 20, 0, height_m=120)]
    assert cluster_jobs(cards)[0].max_altitude_m == 120


def test_advanced_job_uses_adv_max_height():
    cards = [_card("c", 0, 0, 0, height_m=80, waypoint_mode=True, adv_max_height_m=140)]
    assert cluster_jobs(cards)[0].max_altitude_m == 140


def test_first_route_index_and_members():
    # sort_order is 0-based; route index is 1-based. Site of sorts 1..3 → first
    # route index 2, members carry per-job route_index + takeoff.
    cards = [_card("a", 1, 0, 0), _card("b", 2, 20, 0), _card("c", 3, 0, 20)]
    s = cluster_jobs(cards)[0]
    assert s.first_route_index == 2
    assert [m["route_index"] for m in s.members] == [2, 3, 4]
    assert all(len(m["takeoff_4326"]) == 2 for m in s.members)
    assert [m["name"] for m in s.members] == ["a", "b", "c"]


def test_empty_input():
    assert cluster_jobs([]) == []
