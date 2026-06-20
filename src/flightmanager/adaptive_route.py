"""Adaptive-sweep lawnmower route with variable strip spacing and per-waypoint altitude.

In advanced flight mode the drone must comply with the EU 1:1 rule near
buildings: altitude ≤ (horizontal distance to building) + building_height.

Two behavioural improvements over the fixed-spacing + per-strip-constant-altitude
approach:

1. **Variable strip spacing** — strip cross-track stepover equals
   footprint_width(alt) × (1 − side_overlap), evaluated from the MINIMUM
   altitude anywhere along the strip.  This ensures strips are placed tighter
   wherever buildings constrain altitude at any point of the strip, not just
   at the strip midpoint.

2. **Per-waypoint altitude within each strip** — altitude is sampled every
   *sample_m* metres along the strip and simplified (merge points within 2 m
   of each other).  The waylines builder emits intermediate waypoints so the
   drone climbs/descends continuously rather than holding the worst-case
   altitude for the full strip length.

Entry point: ``compute_adaptive_route()`` returns
``(RouteResult, altitude_profile, strip_waypoints, transit_waypoints)`` where

* ``altitude_profile[i]``     — minimum altitude for strip *i* (used for
  flight-time estimation and backward-compatible callers)
* ``strip_waypoints[i]``      — list of ``(x3067, y3067, alt_m, speed_ms)``
  waypoints for strip *i* including the two endpoints; len ≥ 2.
* ``transit_waypoints[i]``    — ``(x3067, y3067, alt_m)`` waypoints along
  the *i*-th inter-strip transit, sampled at *sample_m* intervals with
  1:1-compliant altitude at each point.  Only inter-strip transits (home
  transits excluded).

All geometry is in EPSG:3067.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from shapely.affinity import rotate
from shapely.geometry import Point

from flightmanager.obstacle_heights import building_height_m

# Stable private helpers from route.py — same package, no external API.
from flightmanager.route import (
    RouteResult,
    _boundary_route,
    _clip_strips_to_polygon,
    _greedy_nn_order,
)

if TYPE_CHECKING:
    from flightmanager.config import DroneConfig

_MAX_CLIMB_MS = (
    10.0  # DJI waypoint-mode max climb (m/s) — kept above photogrammetric limit
)
_PL_SCAN_M = 200.0  # powerline influence radius (m)
_ALT_MERGE_M = 2.0  # merge consecutive waypoints within this altitude delta
# Kohdeluokka codes treated as non-habited structures; excluded from 1:1 altitude rule.
# Agricultural/storage buildings are part of the farm operation, not uninvolved-person hazards.
_AGRI_CODES: frozenset[int] = frozenset({42260, 42261, 42262})


# ---------------------------------------------------------------------------
# Altitude computation
# ---------------------------------------------------------------------------


def _altitude_at(
    pt: Point,
    bldg_pairs: list[tuple],  # list of (shapely_geom, height_m)
    pl_geoms: list,
    *,
    H_max: float,
    H_min: float,
    powerline_clearance_m: float,
) -> float:
    """EU 1:1 altitude at a 2-D world point, clamped to [H_min, H_max]."""
    if bldg_pairs:
        d_eff = min(pt.distance(g) + h for g, h in bldg_pairs)
        alt = float(min(max(d_eff, H_min), H_max))
    else:
        alt = H_max
    for pl_g in pl_geoms:
        if pt.distance(pl_g) < _PL_SCAN_M:
            alt = max(alt, powerline_clearance_m)
    return alt


def _sample_strip(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    bldg_pairs: list[tuple],
    pl_geoms: list,
    *,
    H_max: float,
    H_min: float,
    powerline_clearance_m: float,
    sample_m: float,
) -> list[tuple[float, float, float]]:
    """Return ``(x, y, alt)`` samples along the strip at *sample_m* intervals.

    Samples always include both endpoints.  Altitude is the EU 1:1 value at
    each point.
    """
    length = math.hypot(x2 - x1, y2 - y1)
    n = max(1, int(length / sample_m))
    result: list[tuple[float, float, float]] = []
    for k in range(n + 1):
        t = k / n
        x = x1 + t * (x2 - x1)
        y = y1 + t * (y2 - y1)
        alt = _altitude_at(
            Point(x, y),
            bldg_pairs,
            pl_geoms,
            H_max=H_max,
            H_min=H_min,
            powerline_clearance_m=powerline_clearance_m,
        )
        result.append((x, y, alt))
    return result


def _sample_path(
    pts: list[tuple[float, float]],
    bldg_pairs: list[tuple],
    pl_geoms: list,
    *,
    H_max: float,
    H_min: float,
    powerline_clearance_m: float,
    sample_m: float,
) -> list[tuple[float, float, float]]:
    """Sample altitude along a piecewise-linear path at *sample_m* intervals.

    Each original vertex is preserved; long segments get intermediate samples.
    Returns ``(x, y, alt)`` for each point.
    """
    if len(pts) < 2:
        return []
    result: list[tuple[float, float, float]] = []
    for k in range(len(pts) - 1):
        seg = _sample_strip(
            pts[k][0],
            pts[k][1],
            pts[k + 1][0],
            pts[k + 1][1],
            bldg_pairs,
            pl_geoms,
            H_max=H_max,
            H_min=H_min,
            powerline_clearance_m=powerline_clearance_m,
            sample_m=sample_m,
        )
        if k == 0:
            result.extend(seg)
        else:
            result.extend(seg[1:])  # strip duplicate shared vertex
    return result


def _simplify_altitude_waypoints(
    samples: list[tuple[float, float, float]],
    merge_m: float = _ALT_MERGE_M,
) -> list[tuple[float, float, float]]:
    """Remove intermediate waypoints where altitude deviates < *merge_m* from
    the linear interpolation between its neighbours.

    Endpoints are always kept.  This reduces waypoint count on flat sections
    while preserving the shape of rapid altitude transitions.
    """
    if len(samples) <= 2:
        return list(samples)

    kept = [samples[0]]
    i = 1
    while i < len(samples) - 1:
        x0, y0, a0 = kept[-1]
        x2, y2, a2 = samples[i + 1]
        x1, y1, a1 = samples[i]
        # Linear interpolation of altitude at this point
        d_total = math.hypot(x2 - x0, y2 - y0)
        if d_total < 1e-6:
            i += 1
            continue
        d_to_pt = math.hypot(x1 - x0, y1 - y0)
        a_interp = a0 + (a2 - a0) * (d_to_pt / d_total)
        if abs(a1 - a_interp) >= merge_m:
            kept.append(samples[i])
        i += 1
    kept.append(samples[-1])
    return kept


# ---------------------------------------------------------------------------
# Along-strip smoothing
# ---------------------------------------------------------------------------


def _smooth_strip_along(
    samples: list[tuple[float, float, float]],
    slope_m_per_m: float,
    H_min: float,
    H_max: float,
) -> list[tuple[float, float, float]]:
    """Two-pass forward/backward slope filter along a strip's altitude samples.

    *slope_m_per_m* is the maximum altitude ASCENT rate per metre of horizontal
    travel (m/m).  Only ascent is limited — the drone may descend at any rate
    so that 1:1 building constraints are never overridden by this smoothing pass.
    Derived from the photogrammetric and physical climb limits via
    ``_slope_along_m_per_m()``.
    """
    if len(samples) <= 1 or slope_m_per_m <= 0.0:
        return list(samples)
    n = len(samples)
    alts = [s[2] for s in samples]
    for i in range(1, n):
        d = math.hypot(
            samples[i][0] - samples[i - 1][0], samples[i][1] - samples[i - 1][1]
        )
        step = slope_m_per_m * max(d, 1e-3)
        alts[i] = min(alts[i], alts[i - 1] + step)
    for i in range(n - 2, -1, -1):
        d = math.hypot(
            samples[i + 1][0] - samples[i][0], samples[i + 1][1] - samples[i][1]
        )
        step = slope_m_per_m * max(d, 1e-3)
        alts[i] = min(alts[i], alts[i + 1] + step)
    alts = [max(H_min, min(H_max, a)) for a in alts]
    return [(samples[i][0], samples[i][1], alts[i]) for i in range(n)]


def _fill_narrow_dips(
    samples: list[tuple[float, float, float]],
    min_dip_m: float,
) -> list[tuple[float, float, float]]:
    """Raise any valley whose horizontal span < *min_dip_m* to the chord
    connecting its surrounding higher altitudes.

    For every pair of sample points (i, j) within *min_dip_m* horizontal
    distance, any interior point below the straight-line altitude between
    i and j is raised to that chord.  This fills isolated dips from small
    obstacles while leaving wide, genuine low-altitude zones unchanged.
    """
    n = len(samples)
    if n <= 2 or min_dip_m <= 0.0:
        return list(samples)
    alts = [s[2] for s in samples]
    result = list(alts)
    cum = [0.0]
    for k in range(1, n):
        cum.append(
            cum[-1]
            + math.hypot(
                samples[k][0] - samples[k - 1][0],
                samples[k][1] - samples[k - 1][1],
            )
        )
    for i in range(n):
        for j in range(i + 1, n):
            span = cum[j] - cum[i]
            if span >= min_dip_m:
                break
            if span < 1e-6:
                continue
            for k in range(i + 1, j):
                t = (cum[k] - cum[i]) / span
                chord = alts[i] * (1.0 - t) + alts[j] * t
                result[k] = max(result[k], chord)
    return [(samples[i][0], samples[i][1], result[i]) for i in range(n)]


# ---------------------------------------------------------------------------
# Slope filter (cross-strip)
# ---------------------------------------------------------------------------


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
        slope_f
        * drone.focal_length_mm
        / (drone.sensor_w_mm * (1.0 - overlap_side_pct / 100.0))
    )
    physical_limit = _MAX_CLIMB_MS / speed
    return min(photogrammetry_limit, physical_limit)


def _slope_along_m_per_m(
    drone: DroneConfig,
    H_max: float,
    overlap_front_pct: int,
    slope_f: float,
) -> float:
    """Max altitude change per metre of along-strip travel (m/m).

    Mirrors ``_slope_across`` using the sensor's forward (height) dimension
    and front overlap so the slope limit is consistent in both directions.
    """
    speed = max(0.5, drone.auto_speed(H_max, overlap_front_pct))
    photogrammetry_limit = (
        slope_f
        * drone.focal_length_mm
        / (drone.sensor_h_mm * (1.0 - overlap_front_pct / 100.0))
    )
    physical_limit = _MAX_CLIMB_MS / speed
    return min(photogrammetry_limit, physical_limit)


def _apply_slope_filter(
    alt_list: list[float],
    midpoints: list[tuple[float, float]],
    slope_across: float,
    H_min: float,
) -> list[float]:
    """Two-pass forward/backward slope filter with per-pair variable spacing.

    Only limits ascent rate between adjacent strips — descent is unconstrained
    so that 1:1 building constraints are never overridden by the smoothing pass.
    """
    n = len(alt_list)
    smooth = list(alt_list)

    def _d(i: int) -> float:
        ax, ay = midpoints[i]
        bx, by = midpoints[i + 1]
        return math.hypot(bx - ax, by - ay)

    for i in range(1, n):
        step = slope_across * _d(i - 1)
        smooth[i] = min(smooth[i], smooth[i - 1] + step)

    for i in range(n - 2, -1, -1):
        step = slope_across * _d(i)
        smooth[i] = min(smooth[i], smooth[i + 1] + step)

    return [max(H_min, h) for h in smooth]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def compute_adaptive_route(  # noqa: C901
    polygon_3067,
    angle_deg: float,
    buildings: list,  # list[Building]
    power_lines: list,  # list[PowerLine]
    *,
    drone: DroneConfig,
    H_max: float,
    H_min: float,
    overlap_front_pct: int,
    overlap_side_pct: int,
    powerline_clearance_m: float,
    slope_f: float,
    min_dip_m: float = 0.0,
    habited_only: bool = True,
    home_3067: tuple[float, float] | None = None,
    sample_m: float = 10.0,
) -> tuple[
    RouteResult,
    list[float],
    list[list[tuple[float, float, float, float]]],
    list[list[tuple[float, float, float]]],
]:
    """Variable-spacing lawnmower with per-waypoint altitude and speed.

    Returns ``(route, altitude_profile, strip_waypoints, transit_waypoints)``:

    * *altitude_profile[i]*   — minimum AGL altitude for strip *i*
    * *strip_waypoints[i]*    — ``[(x3067, y3067, alt_m, speed_ms), ...]`` along
      strip *i*. Altitude is smoothed by an along-strip slope filter then a
      narrow-dip fill pass. Speed = ``drone.auto_speed(alt_m, overlap_front_pct)``.
    * *transit_waypoints[i]*  — ``[(x3067, y3067, alt_m), ...]`` along the
      *i*-th inter-strip transit with 1:1-compliant altitude at each point.
      Home transits (when *home_3067* is provided) are excluded.

    Strip cross-track spacing is derived from the minimum altitude ANYWHERE
    along each strip (not just the midpoint), so strips near building edges are
    placed correctly even when buildings run along the flight direction.
    """
    if polygon_3067.geom_type == "MultiPolygon":
        polygon_3067 = max(polygon_3067.geoms, key=lambda g: g.area)

    bldg_pairs: list[tuple] = [
        (b.geometry, building_height_m(b))
        for b in buildings
        if not habited_only or b.kohdeluokka not in _AGRI_CODES
    ]
    pl_geoms: list = [pl.geometry for pl in power_lines if pl.is_overhead]

    sensor_w_m = drone.image_width_px * drone.pixel_pitch_um * 1e-6
    sensor_h_m = drone.image_height_px * drone.pixel_pitch_um * 1e-6
    focal_m = drone.focal_length_mm * 1e-3

    def _stepover(alt: float) -> float:
        return max(1.0, alt * sensor_w_m / focal_m * (1.0 - overlap_side_pct / 100.0))

    # ── Rotate polygon to strip-aligned space ─────────────────────────────────
    cx, cy = polygon_3067.centroid.x, polygon_3067.centroid.y
    rot_angle = angle_deg - 90.0
    rot_rad = math.radians(rot_angle)
    cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)

    rotated = rotate(polygon_3067, rot_angle, origin=(cx, cy), use_radians=False)
    ring = list(rotated.exterior.coords)
    minx_r = min(x for x, _ in ring)
    maxx_r = max(x for x, _ in ring)
    _, miny, _, maxy = rotated.bounds

    back_rad = math.radians(90.0 - angle_deg)
    cos_b, sin_b = math.cos(back_rad), math.sin(back_rad)

    def _back(px: float, py: float) -> tuple[float, float]:
        dx, dy = px - cx, py - cy
        return cx + dx * cos_b - dy * sin_b, cy + dx * sin_b + dy * cos_b

    # ── Adaptive y-position generation ────────────────────────────────────────
    # Stepover is based on the MINIMUM altitude along the strip (full-length
    # sampling), so strips are spaced correctly even when buildings affect only
    # one end of a strip rather than the cross-track centre.
    first_stepover = _stepover(H_max)
    y = miny + first_stepover / 2.0

    strips_y: list[float] = []
    while y <= maxy + 1e-6:
        strips_y.append(y)

        # Sample the full strip width for minimum altitude
        segs = _clip_strips_to_polygon(ring, [y])
        if segs:
            _, x0, x1 = max(segs, key=lambda s: s[2] - s[1])
            wx0, wy0 = _back(x0, y)
            wx1, wy1 = _back(x1, y)
            samples = _sample_strip(
                wx0,
                wy0,
                wx1,
                wy1,
                bldg_pairs,
                pl_geoms,
                H_max=H_max,
                H_min=H_min,
                powerline_clearance_m=powerline_clearance_m,
                sample_m=sample_m,
            )
            alt = min(s[2] for s in samples)
        else:
            alt = H_max

        y += _stepover(alt)

    if not strips_y:
        return (
            RouteResult(
                strip_count=0,
                photo_count=0,
                strip_dist_m=0.0,
                turn_dist_m=0.0,
                angle_deg=angle_deg,
                strips_3067=[],
                transit_segs_3067=[],
                first_wp_3067=None,
                last_wp_3067=None,
            ),
            [],
            [],
            [],
        )

    raw_segs = _clip_strips_to_polygon(ring, strips_y)
    if not raw_segs:
        return (
            RouteResult(
                strip_count=0,
                photo_count=0,
                strip_dist_m=0.0,
                turn_dist_m=0.0,
                angle_deg=angle_deg,
                strips_3067=[],
                transit_segs_3067=[],
                first_wp_3067=None,
                last_wp_3067=None,
            ),
            [],
            [],
            [],
        )

    # ── Greedy nearest-neighbour ordering ─────────────────────────────────────
    home_rot_x = (minx_r + maxx_r) / 2.0
    home_rot_y = miny
    if home_3067 is not None:
        hx_rel = home_3067[0] - cx
        hy_rel = home_3067[1] - cy
        home_rot_x = cx + hx_rel * cos_r - hy_rel * sin_r
        home_rot_y = cy + hx_rel * sin_r + hy_rel * cos_r

    strips_3067 = _greedy_nn_order(raw_segs, home_rot_x, home_rot_y, _back)

    # ── Route geometry ─────────────────────────────────────────────────────────
    strip_dist_m = sum(math.hypot(x2 - x1, y2 - y1) for x1, y1, x2, y2 in strips_3067)

    transit_segs: list[list[tuple[float, float]]] = []
    turn_dist_m = 0.0
    if strips_3067:
        if home_3067 is not None:
            transit_segs.append([home_3067, (strips_3067[0][0], strips_3067[0][1])])
        for i in range(len(strips_3067) - 1):
            p1 = (strips_3067[i][2], strips_3067[i][3])
            p2 = (strips_3067[i + 1][0], strips_3067[i + 1][1])
            path = _boundary_route(polygon_3067, p1, p2)
            transit_segs.append(path)
            turn_dist_m += sum(
                math.hypot(path[k + 1][0] - path[k][0], path[k + 1][1] - path[k][1])
                for k in range(len(path) - 1)
            )
        if home_3067 is not None:
            transit_segs.append([(strips_3067[-1][2], strips_3067[-1][3]), home_3067])

    # ── Per-strip altitude waypoints ───────────────────────────────────────────
    # Pipeline:
    #   1. Sample raw 1:1 altitudes at sample_m intervals.
    #   2. Along-strip slope filter — gradual climbs/descents.
    #   3. Narrow-dip fill — erase dips narrower than min_dip_m.
    #   4. Simplify — drop redundant waypoints.
    slope_along = _slope_along_m_per_m(drone, H_max, overlap_front_pct, slope_f)

    strip_waypoints_raw: list[list[tuple[float, float, float]]] = []
    for x1, y1, x2, y2 in strips_3067:
        samples = _sample_strip(
            x1,
            y1,
            x2,
            y2,
            bldg_pairs,
            pl_geoms,
            H_max=H_max,
            H_min=H_min,
            powerline_clearance_m=powerline_clearance_m,
            sample_m=sample_m,
        )
        samples = _smooth_strip_along(samples, slope_along, H_min, H_max)
        samples = _fill_narrow_dips(samples, min_dip_m)
        strip_waypoints_raw.append(_simplify_altitude_waypoints(samples))

    # Per-strip minimum altitude (used for spacing decisions and flight time)
    raw_alt = [min(wp[2] for wp in wps) for wps in strip_waypoints_raw]

    # Slope-filter the per-strip minimum altitudes (cross-strip direction)
    midpoints = [((x1 + x2) / 2.0, (y1 + y2) / 2.0) for x1, y1, x2, y2 in strips_3067]
    sc = _slope_across(drone, H_max, overlap_front_pct, overlap_side_pct, slope_f)
    altitude_profile = _apply_slope_filter(raw_alt, midpoints, sc, H_min)

    # Apply the slope-filtered floor to per-strip waypoints: if the slope filter
    # raised a strip's minimum altitude, raise every waypoint in that strip by
    # the same delta so the waypoint curve is preserved but shifted up.
    # Attach per-waypoint speed: drone.auto_speed(alt) scales with altitude so
    # the camera fires at the correct interval for consistent forward overlap.
    strip_waypoints: list[list[tuple[float, float, float, float]]] = []
    for i, wps in enumerate(strip_waypoints_raw):
        floor = altitude_profile[i]
        old_min = min(wp[2] for wp in wps)
        shift = max(0.0, floor - old_min)
        out: list[tuple[float, float, float, float]] = []
        for x, y, a in wps:
            a_f = min(max(a + shift, H_min), H_max)
            out.append((x, y, a_f, drone.auto_speed(a_f, overlap_front_pct)))
        strip_waypoints.append(out)

    # ── Per-transit altitude waypoints ────────────────────────────────────────
    # Using _altitude_at() at every transit waypoint ensures the 1:1 rule is
    # respected throughout the transit, not just at the strip endpoints.
    # max(end_alt, start_alt) would fly too high near buildings at one end.
    has_home = home_3067 is not None
    inter_segs: list = transit_segs[1:-1] if has_home else transit_segs
    transit_waypoints: list[list[tuple[float, float, float]]] = []
    for seg in inter_segs:
        if len(seg) < 2:
            transit_waypoints.append([])
            continue
        samples = _sample_path(
            seg,
            bldg_pairs,
            pl_geoms,
            H_max=H_max,
            H_min=H_min,
            powerline_clearance_m=powerline_clearance_m,
            sample_m=sample_m,
        )
        transit_waypoints.append(_simplify_altitude_waypoints(samples))

    # ── Photo count (per-strip minimum altitude) ───────────────────────────────
    photo_count = 0
    for i, (x1, y1, x2, y2) in enumerate(strips_3067):
        alt = altitude_profile[i]
        photo_m = max(
            0.5, alt * sensor_h_m / focal_m * (1.0 - overlap_front_pct / 100.0)
        )
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
    return route, altitude_profile, strip_waypoints, transit_waypoints
