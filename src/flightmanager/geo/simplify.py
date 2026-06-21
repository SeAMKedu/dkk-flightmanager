"""Polygon simplification utilities.

Provides vertex counting and two simplification strategies:

* **Fixed** — Douglas-Peucker at a caller-supplied tolerance (metres).
* **Auto** — automatically picks the tolerance at the *knee* of the
  (log-tolerance, vertex-count) complexity curve, with a hard vertex-count
  cap and a binary-search fallback.

All functions work on Shapely geometries and assume metric coordinates
(EPSG:3067 or any other projection where distances are in metres).
"""

from __future__ import annotations

import logging
import math

from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Vertex counting
# ---------------------------------------------------------------------------


def vertex_count(geom: BaseGeometry) -> int:
    """Return total exterior-ring coordinate count (closing vertex included)."""
    if isinstance(geom, Polygon):
        return len(geom.exterior.coords)
    if isinstance(geom, MultiPolygon):
        return sum(len(p.exterior.coords) for p in geom.geoms)
    return 0


# ---------------------------------------------------------------------------
# Fixed-tolerance simplification
# ---------------------------------------------------------------------------


def _simplify_within(geom: BaseGeometry, tolerance_m: float) -> BaseGeometry:
    """Simplify with topology-preserving Douglas-Peucker.

    ``preserve_topology=True`` prevents self-intersections and bounds the
    maximum boundary deviation to roughly *tolerance_m*.  We intentionally do
    **not** intersect back with the original: doing so would restore the dense
    circular-arc vertices introduced by the building-buffer keep-out, defeating
    simplification around keep-out boundaries.  The keep-out buffer already
    carries a large safety margin (≥ flight height), so a few metres of
    deviation at the arc is negligible.
    """
    result = geom.simplify(tolerance_m, preserve_topology=True)
    if result.is_empty:
        return geom
    return result


# ---------------------------------------------------------------------------
# Automatic (knee-finding) simplification
# ---------------------------------------------------------------------------


def _auto_simplify(geom: BaseGeometry, max_vertices: int) -> BaseGeometry:
    """Pick the simplification tolerance at the knee of the complexity curve.

    Samples vertex count at log-spaced tolerances, then finds the sample whose
    ``(log-tolerance, vertex-count)`` point lies farthest from the chord
    connecting the first and last samples — the elbow/knee method.  This picks
    the tolerance where the curve bends: beyond it you keep trading shape
    accuracy for very few extra vertices removed.

    *max_vertices* is a hard cap: if the knee result still exceeds it the
    binary-search fallback is used to enforce the cap.
    """
    original_vc = vertex_count(geom)
    if original_vc <= 5:
        return geom

    # Log-spaced probe tolerances (metres).  The range covers fine noise
    # removal through full field-scale simplification.
    tolerances = [0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500]
    samples: list[tuple[float, int, BaseGeometry]] = []
    for tol in tolerances:
        s = _simplify_within(geom, tol)
        vc = vertex_count(s)
        samples.append((tol, vc, s))
        if vc <= 4:
            break

    if len(samples) < 2:
        return samples[0][2] if samples else geom

    # Knee detection: perpendicular distance from chord in (log10(tol), vc)
    # space.  The curve runs top-left → bottom-right; the knee is the point
    # that bulges most away from the straight line between the endpoints.
    log_tols = [math.log10(t) for t, _, _ in samples]
    vcs = [vc for _, vc, _ in samples]

    x0, x1 = log_tols[0], log_tols[-1]
    y0, y1 = vcs[0], vcs[-1]
    dx, dy = x1 - x0, y1 - y0
    line_len = math.hypot(dx, dy)

    if line_len == 0:
        return samples[-1][2]

    best_idx, best_dist = 0, -1.0
    for i, (xi, yi) in enumerate(zip(log_tols, vcs)):
        d = (dy * (xi - x0) - dx * (yi - y0)) / line_len
        if d > best_dist:
            best_dist, best_idx = d, i

    tol_chosen, vc_chosen, geom_chosen = samples[best_idx]
    log.info(
        "Auto-simplify knee at %.1f m → %d vertices (was %d)",
        tol_chosen,
        vc_chosen,
        original_vc,
    )

    if vc_chosen <= max_vertices:
        return geom_chosen

    return _binary_search_cap(
        geom, start_tol=tol_chosen, knee_geom=geom_chosen, max_vertices=max_vertices
    )


def _binary_search_cap(
    geom: BaseGeometry,
    *,
    start_tol: float,
    knee_geom: BaseGeometry,
    max_vertices: int,
) -> BaseGeometry:
    """Binary-search for the smallest tolerance that keeps vertex count ≤ max_vertices."""
    log.info(
        "Knee vertex count %d exceeds cap %d, falling back to binary search",
        vertex_count(knee_geom),
        max_vertices,
    )
    lo, hi = start_tol, 500.0
    result = knee_geom
    for _ in range(20):
        mid = (lo + hi) / 2.0
        candidate = _simplify_within(geom, mid)
        if vertex_count(candidate) <= max_vertices:
            result = candidate
            hi = mid
        else:
            lo = mid
        if hi - lo < 0.1:
            break
    log.debug(
        "Binary-search fallback converged at %.1f m → %d vertices",
        hi,
        vertex_count(result),
    )
    return result


# ---------------------------------------------------------------------------
# Per-piece dispatch
# ---------------------------------------------------------------------------


def simplify_pieces(pieces: list[BaseGeometry], cfg) -> list[BaseGeometry]:
    """Apply the configured simplification strategy to each polygon piece.

    *cfg* is a :class:`~flightmanager.config.PolygonConfig` instance (duck-typed to
    avoid a circular import).
    """
    if cfg.simplify_mode == "auto":
        target = cfg.auto_simplify_max_vertices
        log.info("Simplify mode=auto, target ≤%d vertices per piece", target)
        return [_auto_simplify(p, target) for p in pieces]
    if cfg.simplify_tolerance_m > 0:
        tol = cfg.simplify_tolerance_m
        log.info("Simplify mode=fixed, tolerance=%.1f m", tol)
        return [_simplify_within(p, tol) for p in pieces]
    return pieces
