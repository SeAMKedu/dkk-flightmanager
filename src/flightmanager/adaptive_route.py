"""Adaptive-sweep lawnmower route with variable strip spacing.

In advanced flight mode the drone must comply with the EU 1:1 rule near
buildings: flight altitude ≤ horizontal distance to the nearest building
rooftop (horizontal_dist + building_height).  Lower altitude shrinks the
camera footprint, requiring tighter strip spacing to preserve side overlap.

This module computes variable-spaced strips: near buildings the drone flies
lower and strips pinch together; in open areas the drone rises to H_max and
strips widen.  The single entry point ``compute_adaptive_route()`` returns
both a ``RouteResult`` (strips, transits, ordering) and the per-strip altitude
profile ready for ``waylines_builder.build_waylines()``.

All geometry is in EPSG:3067.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from shapely.affinity import rotate
from shapely.geometry import Point

from flightmanager.obstacle_heights import building_height_m

# Private but stable helpers from route.py — same package, no external API.
from flightmanager.route import (
    RouteResult,
    _boundary_route,
    _clip_strips_to_polygon,
    _greedy_nn_order,
)

if TYPE_CHECKING:
    from flightmanager.buildings import Building
    from flightmanager.config import DroneConfig
    from flightmanager.powerlines import PowerLine

_MAX_CLIMB_MS = 3.0   # conservative max vertical speed (m/s) in waypoint mode
_PL_SCAN_M   = 200.0  # powerline influence radius (m)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _altitude_at(
    pt: Point,
    bldg_pairs: list[tuple],    # list of (shapely_geom, height_m)
    pl_geoms: list,              # overhead powerline shapely geometries
    *,
    H_max: float,
    H_min: float,
    powerline_clearance_m: float,
) -> float:
    """Return the EU 1:1 compliant altitude at a 2-D world point.

    EU 1:1 rule: altitude ≤ horizontal distance to nearest building measured
    from the rooftop (d_horizontal + building_height).  Result is clamped to
    [H_min, H_max].  Powerline proximity overrides the floor upward.
    """
    if bldg_pairs:
        d_eff = min(pt.distance(g) + h for g, h in bldg_pairs)
        alt = float(min(max(d_eff, H_min), H_max))
    else:
        alt = H_max
    for pl_g in pl_geoms:
        if pt.distance(pl_g) < _PL_SCAN_M:
            alt = max(alt, powerline_clearance_m)
    return alt


def _sample_strip_altitude(
    x1: float, y1: float, x2: float, y2: float,
    bldg_pairs: list[tuple],
    pl_geoms: list,
    *,
    H_max: float,
    H_min: float,
    powerline_clearance_m: float,
    sample_m: float,
) -> float:
    """Return the minimum (most conservative) altitude along a strip segment.

    Samples at roughly every *sample_m* metres plus both endpoints.
    """
    length = math.hypot(x2 - x1, y2 - y1)
    n = max(2, int(length / sample_m) + 1)
    min_alt = H_max
    for k in range(n + 1):
        t = k / n
        alt = _altitude_at(
            Point(x1 + t * (x2 - x1), y1 + t * (y2 - y1)),
            bldg_pairs, pl_geoms,
            H_max=H_max, H_min=H_min,
            powerline_clearance_m=powerline_clearance_m,
        )
        if alt < min_alt:
            min_alt = alt
    return min_alt


def _slope_across(
    drone: DroneConfig,
    H_max: float,
    overlap_front_pct: int,
    overlap_side_pct: int,
    slope_f: float,
) -> float:
    """Max altitude change per metre of cross-track distance (m/m)."""
    speed = max(0.5, drone.auto_speed(H_max, overlap_front_pct))
    photogrammetry_limit = (
        slope_f * drone.focal_length_mm
        / (drone.sensor_w_mm * (1.0 - overlap_side_pct / 100.0))
    )
    physical_limit = _MAX_CLIMB_MS / speed
    return min(photogrammetry_limit, physical_limit)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def compute_adaptive_route(
    polygon_3067,
    angle_deg: float,
    buildings: list,          # list[Building]
    power_lines: list,        # list[PowerLine]
    *,
    drone: DroneConfig,
    H_max: float,
    H_min: float,
    overlap_front_pct: int,
    overlap_side_pct: int,
    powerline_clearance_m: float,
    slope_f: float,
    home_3067: tuple[float, float] | None = None,
    sample_m: float = 10.0,
) -> tuple[RouteResult, list[float]]:
    """Variable-spacing lawnmower route with per-strip altitude profile.

    Strip spacing is derived from the drone's altitude at each position:
        footprint_width(alt) = alt × sensor_w / focal_length
        stepover(alt)        = footprint_width × (1 − side_overlap)

    Near buildings the drone descends (EU 1:1 rule) → smaller footprint →
    tighter strips.  Far from buildings the drone rises to *H_max* → wide
    strips → efficient coverage.

    Returns ``(RouteResult, altitude_profile)`` where ``altitude_profile`` has
    one entry per strip in route-execution order (matching *RouteResult.strips_3067*).
    The profile has been slope-filtered so no adjacent-strip altitude transition
    exceeds the drone's physical climb/descent capability.
    """
    if polygon_3067.geom_type == "MultiPolygon":
        polygon_3067 = max(polygon_3067.geoms, key=lambda g: g.area)

    # Pre-build obstacle lookup structures (fast repeated distance queries).
    bldg_pairs: list[tuple] = [
        (b.geometry, building_height_m(b)) for b in buildings
    ]
    pl_geoms: list = [pl.geometry for pl in power_lines if pl.is_overhead]

    # Camera geometry constants
    sensor_w_m = drone.image_width_px  * drone.pixel_pitch_um * 1e-6
    sensor_h_m = drone.image_height_px * drone.pixel_pitch_um * 1e-6
    focal_m    = drone.focal_length_mm * 1e-3

    def _stepover(alt: float) -> float:
        return max(1.0, alt * sensor_w_m / focal_m * (1.0 - overlap_side_pct / 100.0))

    # ── Rotate polygon to strip-aligned space ─────────────────────────────────
    cx, cy    = polygon_3067.centroid.x, polygon_3067.centroid.y
    rot_angle = angle_deg - 90.0
    rot_rad   = math.radians(rot_angle)
    cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)

    rotated = rotate(polygon_3067, rot_angle, origin=(cx, cy), use_radians=False)
    ring    = list(rotated.exterior.coords)
    minx_r  = min(x for x, _ in ring)
    maxx_r  = max(x for x, _ in ring)
    _, miny, _, maxy = rotated.bounds

    # Back-rotation: rotated space → EPSG:3067
    back_rad = math.radians(90.0 - angle_deg)
    cos_b, sin_b = math.cos(back_rad), math.sin(back_rad)

    def _back(px: float, py: float) -> tuple[float, float]:
        dx, dy = px - cx, py - cy
        return cx + dx * cos_b - dy * sin_b, cy + dx * sin_b + dy * cos_b

    # ── Adaptive y-position generation ───────────────────────────────────────
    # First strip is placed at half a footprint from the boundary (same as
    # compute_route() so polygon edges land inside the overlap zone).
    first_stepover = _stepover(H_max)
    y = miny + first_stepover / 2.0

    strips_y: list[float] = []
    while y <= maxy + 1e-6:
        strips_y.append(y)

        # Representative point for altitude estimate: midpoint of the widest
        # segment at this y (or polygon x-centre if no coverage yet).
        test_segs = _clip_strips_to_polygon(ring, [y])
        if test_segs:
            _, x0, x1 = max(test_segs, key=lambda s: s[2] - s[1])
            wx, wy = _back((x0 + x1) / 2.0, y)
        else:
            wx, wy = _back((minx_r + maxx_r) / 2.0, y)

        alt = _altitude_at(
            Point(wx, wy), bldg_pairs, pl_geoms,
            H_max=H_max, H_min=H_min,
            powerline_clearance_m=powerline_clearance_m,
        )
        y += _stepover(alt)

    if not strips_y:
        return (
            RouteResult(
                strip_count=0, photo_count=0, strip_dist_m=0.0,
                turn_dist_m=0.0, angle_deg=angle_deg,
                strips_3067=[], transit_segs_3067=[],
                first_wp_3067=None, last_wp_3067=None,
            ),
            [],
        )

    # ── Clip strips to polygon ────────────────────────────────────────────────
    raw_segs = _clip_strips_to_polygon(ring, strips_y)

    if not raw_segs:
        return (
            RouteResult(
                strip_count=0, photo_count=0, strip_dist_m=0.0,
                turn_dist_m=0.0, angle_deg=angle_deg,
                strips_3067=[], transit_segs_3067=[],
                first_wp_3067=None, last_wp_3067=None,
            ),
            [],
        )

    # ── Greedy nearest-neighbour ordering (same as compute_route) ────────────
    home_rot_x = (minx_r + maxx_r) / 2.0
    home_rot_y = miny
    if home_3067 is not None:
        hx_rel = home_3067[0] - cx
        hy_rel = home_3067[1] - cy
        home_rot_x = cx + hx_rel * cos_r - hy_rel * sin_r
        home_rot_y = cy + hx_rel * sin_r + hy_rel * cos_r

    strips_3067 = _greedy_nn_order(raw_segs, home_rot_x, home_rot_y, _back)

    # ── Per-strip statistics ──────────────────────────────────────────────────
    # Photo count uses per-strip altitude (computed during profile step below).
    strip_dist_m = sum(
        math.hypot(x2 - x1, y2 - y1) for x1, y1, x2, y2 in strips_3067
    )

    # ── Transit segments ──────────────────────────────────────────────────────
    transit_segs: list[list[tuple[float, float]]] = []
    turn_dist_m = 0.0
    if strips_3067:
        if home_3067 is not None:
            transit_segs.append([home_3067, (strips_3067[0][0], strips_3067[0][1])])
        for i in range(len(strips_3067) - 1):
            p1 = (strips_3067[i][2],     strips_3067[i][3])
            p2 = (strips_3067[i + 1][0], strips_3067[i + 1][1])
            path = _boundary_route(polygon_3067, p1, p2)
            transit_segs.append(path)
            turn_dist_m += sum(
                math.hypot(path[k + 1][0] - path[k][0], path[k + 1][1] - path[k][1])
                for k in range(len(path) - 1)
            )
        if home_3067 is not None:
            transit_segs.append([(strips_3067[-1][2], strips_3067[-1][3]), home_3067])

    # ── Altitude profile: sample full strip length, take conservative minimum ─
    raw_alt: list[float] = []
    for x1, y1, x2, y2 in strips_3067:
        alt = _sample_strip_altitude(
            x1, y1, x2, y2, bldg_pairs, pl_geoms,
            H_max=H_max, H_min=H_min,
            powerline_clearance_m=powerline_clearance_m,
            sample_m=sample_m,
        )
        raw_alt.append(alt)

    # ── Two-pass slope filter (variable spacing between ordered strips) ───────
    sc = _slope_across(drone, H_max, overlap_front_pct, overlap_side_pct, slope_f)

    def _strip_mid(idx: int) -> tuple[float, float]:
        x1, y1, x2, y2 = strips_3067[idx]
        return (x1 + x2) / 2.0, (y1 + y2) / 2.0

    n = len(strips_3067)
    smooth = list(raw_alt)
    for i in range(1, n):
        ax, ay = _strip_mid(i - 1)
        bx, by = _strip_mid(i)
        max_step = sc * math.hypot(bx - ax, by - ay)
        smooth[i] = max(smooth[i], smooth[i - 1] - max_step)
        smooth[i] = min(smooth[i], smooth[i - 1] + max_step)

    for i in range(n - 2, -1, -1):
        ax, ay = _strip_mid(i)
        bx, by = _strip_mid(i + 1)
        max_step = sc * math.hypot(bx - ax, by - ay)
        smooth[i] = max(smooth[i], smooth[i + 1] - max_step)
        smooth[i] = min(smooth[i], smooth[i + 1] + max_step)

    altitude_profile = [max(H_min, h) for h in smooth]

    # ── Photo count with per-strip altitude ───────────────────────────────────
    photo_count = 0
    for i, (x1, y1, x2, y2) in enumerate(strips_3067):
        alt = altitude_profile[i]
        photo_m = max(0.5, alt * sensor_h_m / focal_m * (1.0 - overlap_front_pct / 100.0))
        strip_len = math.hypot(x2 - x1, y2 - y1)
        photo_count += max(1, math.ceil(strip_len / photo_m) + 1)

    route = RouteResult(
        strip_count=len(strips_3067),
        photo_count=photo_count,
        strip_dist_m=strip_dist_m,
        turn_dist_m=turn_dist_m,
        angle_deg=angle_deg,
        strips_3067=strips_3067,
        transit_segs_3067=transit_segs,
        first_wp_3067=(strips_3067[0][0], strips_3067[0][1]) if strips_3067 else None,
        last_wp_3067=(strips_3067[-1][2], strips_3067[-1][3]) if strips_3067 else None,
    )
    return route, altitude_profile
