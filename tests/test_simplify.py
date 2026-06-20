"""Tests for simplify.py."""

from __future__ import annotations

import math

from shapely.geometry import MultiPolygon, Point, Polygon

from flightmanager.config import PolygonConfig
from flightmanager.simplify import (
    _auto_simplify,
    _simplify_within,
    simplify_pieces,
    vertex_count,
)


def _dense_polygon(
    cx: float = 300_000, cy: float = 6_900_000, r: float = 500, n: int = 200
) -> Polygon:
    """Regular n-gon centred at (cx, cy) — gives n+1 vertices (closed ring)."""
    coords = [
        (cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n))
        for i in range(n)
    ]
    return Polygon(coords)


# ---------------------------------------------------------------------------
# vertex_count
# ---------------------------------------------------------------------------


class TestVertexCount:
    def test_polygon_includes_closing_vertex(self):
        poly = Polygon([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)])
        assert vertex_count(poly) == 5

    def test_triangle(self):
        poly = Polygon([(0, 0), (1, 0), (0.5, 1), (0, 0)])
        assert vertex_count(poly) == 4

    def test_multipolygon_sums_both_rings(self):
        p1 = Polygon([(0, 0), (1, 0), (1, 1), (0, 0)])
        p2 = Polygon([(2, 0), (3, 0), (3, 1), (2, 0)])
        mp = MultiPolygon([p1, p2])
        assert vertex_count(mp) == vertex_count(p1) + vertex_count(p2)

    def test_non_polygon_returns_zero(self):
        assert vertex_count(Point(0, 0)) == 0

    def test_dense_polygon_correct_count(self):
        poly = _dense_polygon(n=100)
        assert vertex_count(poly) == 101  # n exterior coords + closing


# ---------------------------------------------------------------------------
# _simplify_within
# ---------------------------------------------------------------------------


class TestSimplifyWithin:
    def test_zero_tolerance_leaves_polygon_valid(self):
        poly = _dense_polygon(n=50)
        result = _simplify_within(poly, 0)
        assert not result.is_empty
        assert result.is_valid

    def test_positive_tolerance_reduces_vertices(self):
        poly = _dense_polygon(r=500, n=200)
        result = _simplify_within(poly, 10)
        assert vertex_count(result) < vertex_count(poly)

    def test_does_not_return_empty_geometry(self):
        tiny = Polygon([(0, 0), (1, 0), (1, 1), (0, 0)])
        result = _simplify_within(tiny, 1e9)
        assert not result.is_empty

    def test_very_large_tolerance_still_valid(self):
        poly = _dense_polygon(n=100)
        result = _simplify_within(poly, 1000)
        assert result.is_valid
        assert not result.is_empty

    def test_area_roughly_preserved(self):
        poly = _dense_polygon(r=500, n=200)
        result = _simplify_within(poly, 5)
        assert abs(result.area - poly.area) / poly.area < 0.02


# ---------------------------------------------------------------------------
# _auto_simplify
# ---------------------------------------------------------------------------


class TestAutoSimplify:
    def test_tiny_polygon_returned_unchanged(self):
        # ≤5 vertices — should skip simplification entirely
        tri = Polygon([(0, 0), (1, 0), (0.5, 1), (0, 0)])
        result = _auto_simplify(tri, max_vertices=15)
        assert result.equals(tri)

    def test_dense_polygon_reduced_below_cap(self):
        poly = _dense_polygon(n=200)
        cap = 20
        result = _auto_simplify(poly, max_vertices=cap)
        assert vertex_count(result) <= cap

    def test_respects_max_vertices_hard_cap(self):
        poly = _dense_polygon(r=1000, n=300)
        cap = 10
        result = _auto_simplify(poly, max_vertices=cap)
        assert vertex_count(result) <= cap

    def test_result_is_valid_non_empty(self):
        poly = _dense_polygon(n=150)
        result = _auto_simplify(poly, max_vertices=15)
        assert not result.is_empty
        assert result.is_valid

    def test_already_simple_polygon_unchanged(self):
        simple = Polygon(
            [
                (300_000, 6_900_000),
                (300_500, 6_900_000),
                (300_500, 6_900_500),
                (300_000, 6_900_500),
                (300_000, 6_900_000),
            ]
        )
        result = _auto_simplify(simple, max_vertices=15)
        assert vertex_count(result) == vertex_count(simple)


# ---------------------------------------------------------------------------
# simplify_pieces
# ---------------------------------------------------------------------------


class TestSimplifyPieces:
    def test_auto_mode_reduces_dense_polygon(self):
        poly = _dense_polygon(n=200)
        cfg = PolygonConfig(simplify_mode="auto", auto_simplify_max_vertices=15)
        (result,) = simplify_pieces([poly], cfg)
        assert vertex_count(result) <= 15

    def test_fixed_mode_applies_tolerance(self):
        poly = _dense_polygon(n=200)
        cfg = PolygonConfig(simplify_mode="fixed", simplify_tolerance_m=20)
        (result,) = simplify_pieces([poly], cfg)
        assert vertex_count(result) < vertex_count(poly)

    def test_zero_tolerance_returns_original(self):
        poly = _dense_polygon(n=50)
        cfg = PolygonConfig(simplify_mode="fixed", simplify_tolerance_m=0)
        (result,) = simplify_pieces([poly], cfg)
        assert vertex_count(result) == vertex_count(poly)

    def test_multiple_pieces_each_simplified(self):
        pieces = [_dense_polygon(cx=300_000 + i * 2000, n=100) for i in range(3)]
        cfg = PolygonConfig(simplify_mode="auto", auto_simplify_max_vertices=10)
        results = simplify_pieces(pieces, cfg)
        assert len(results) == 3
        assert all(vertex_count(r) <= 10 for r in results)

    def test_auto_mode_uses_max_vertices_field(self):
        poly = _dense_polygon(n=200)
        cap = 12
        cfg = PolygonConfig(simplify_mode="auto", auto_simplify_max_vertices=cap)
        (result,) = simplify_pieces([poly], cfg)
        assert vertex_count(result) <= cap
