"""Unit tests for report.py pure helpers (no network / no PDF render)."""

from __future__ import annotations

from flightmanager import report


def test_t_sanitizes_dashes_keeps_finnish():
    assert report._t("a—b–c") == "a-b-c"          # em/en dashes -> hyphen
    assert report._t("Jalasjärvi pönttö") == "Jalasjärvi pönttö"  # latin-1 ok
    assert "?" in report._t("emoji \U0001F600")   # outside latin-1 -> replacement


def test_mf_nested_lookup():
    m = {"geometry": {"final_area_ha": 1.5}, "flight_ready": True}
    assert report._mf(m, "geometry.final_area_ha") == 1.5
    assert report._mf(m, "flight_ready") is True
    assert report._mf(m, "battery.over_one_battery", "x") == "x"
    assert report._mf(m, "missing.deep.key") is None


def test_fmt():
    assert report._fmt(None) == "-"
    assert report._fmt(3.4, " m") == "3 m"
    assert report._fmt(3.45, " cm", 1) == "3.5 cm"
    assert report._fmt(5, "x") == "5x"


def test_rings_and_lines():
    poly = {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 0]]]}
    assert list(report._rings(poly)) == [[[0, 0], [1, 0], [1, 1], [0, 0]]]
    mp = {"type": "MultiPolygon", "coordinates": [[[[0, 0], [1, 1], [0, 0]]]]}
    assert len(list(report._rings(mp))) == 1
    ls = {"type": "LineString", "coordinates": [[0, 0], [1, 1]]}
    assert list(report._lines(ls)) == [[[0, 0], [1, 1]]]
    assert list(report._rings(None)) == []


def test_bbox_of():
    g = {"type": "Polygon", "coordinates": [[[0, 0], [2, 0], [2, 1], [0, 0]]]}
    assert report._bbox_of([g], [[3, 3]]) == (0, 0, 3, 3)
    assert report._bbox_of([None], None) is None


def test_edge_and_circle_extent():
    edge = report._edge_point([22.0, 62.0], 100.0)
    assert edge[1] == 62.0 and edge[0] > 22.0           # due east
    sw, ne = report._circle_extent([22.0, 62.0], 100.0)
    assert sw[0] < 22.0 < ne[0] and sw[1] < 62.0 < ne[1]


def test_sorted_routable_orders_and_excludes():
    cards = [
        {"name": "b", "sort_order": 1, "takeoff_point_4326": [0, 0]},
        {"name": "a", "sort_order": 0, "takeoff_point_4326": [0, 0]},
        {"name": "skip", "sort_order": 2, "takeoff_point_4326": [0, 0], "skipped": True},
        {"name": "noto", "sort_order": 3, "takeoff_point_4326": None},
    ]
    out = report._sorted_routable(cards)
    assert [c["name"] for c in out] == ["a", "b"]
