"""Route estimation for survey polygons.

Computes the actual lawnmower strip pattern inside a survey polygon and
estimates photo count, route distance, and total flight time.
All geometry in EPSG:3067.

No file I/O, no server dependencies — pure Shapely math.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from shapely.affinity import rotate

log = logging.getLogger(__name__)


@dataclass
class RouteResult:
    """Output of compute_route()."""

    strip_count: int
    photo_count: int
    strip_dist_m: float  # sum of strip traverse lengths
    turn_dist_m: float  # inter-strip transition distances
    angle_deg: float  # the angle that was used
    strips_3067: list[tuple]  # (x1,y1,x2,y2) per strip in EPSG:3067
    transit_segs_3067: list[
        list[tuple[float, float]]
    ]  # each transit: ordered (x,y) waypoints
    first_wp_3067: tuple | None  # (x,y) start of first strip
    last_wp_3067: tuple | None  # (x,y) end of last strip

    @property
    def total_route_dist_m(self) -> float:
        return self.strip_dist_m + self.turn_dist_m


def compute_auto_angle(polygon_3067) -> float:
    """Return optimal route angle (0=N, 90=E, CW, range [0,180)) from the MBR.

    Flies strips along the polygon's longest axis to minimise U-turn count.
    """
    mbr = polygon_3067.minimum_rotated_rectangle
    coords = list(mbr.exterior.coords)
    edges = [(coords[i], coords[i + 1]) for i in range(4)]
    long_edge = max(
        edges, key=lambda e: math.hypot(e[1][0] - e[0][0], e[1][1] - e[0][1])
    )
    dx = long_edge[1][0] - long_edge[0][0]
    dy = long_edge[1][1] - long_edge[0][1]
    return math.degrees(math.atan2(dx, dy)) % 180


def _ring_route(
    ring: list[tuple[float, float]],
    p1: tuple[float, float],
    p2: tuple[float, float],
) -> list[tuple[float, float]]:
    """Walk *ring* (open list, no repeated last vertex) from p1-side to p2-side.

    Returns the shorter of the two traversal directions.
    """
    n = len(ring)

    def seg_idx(pt: tuple[float, float]) -> int:
        best_d, best = float("inf"), 0
        for i in range(n):
            ax, ay = ring[i]
            bx, by = ring[(i + 1) % n]
            dx, dy = bx - ax, by - ay
            l2 = dx * dx + dy * dy
            t = (
                max(0.0, min(1.0, ((pt[0] - ax) * dx + (pt[1] - ay) * dy) / l2))
                if l2 > 1e-10
                else 0.0
            )
            d = math.hypot(pt[0] - (ax + t * dx), pt[1] - (ay + t * dy))
            if d < best_d:
                best_d, best = d, i
        return best

    i1, i2 = seg_idx(p1), seg_idx(p2)
    steps_cw = (i2 - i1) % n
    steps_ccw = (i1 - i2) % n
    path_cw = [p1] + [ring[(i1 + 1 + k) % n] for k in range(steps_cw)] + [p2]
    path_ccw = [p1] + [ring[(i1 - k) % n] for k in range(steps_ccw)] + [p2]

    def plen(pts: list) -> float:
        return sum(
            math.hypot(pts[k + 1][0] - pts[k][0], pts[k + 1][1] - pts[k][1])
            for k in range(len(pts) - 1)
        )

    return path_cw if plen(path_cw) <= plen(path_ccw) else path_ccw


def _boundary_route(
    polygon,
    p1: tuple[float, float],
    p2: tuple[float, float],
) -> list[tuple[float, float]]:
    """Return waypoints from p1 to p2 that stay within or along the polygon boundary.

    Falls back to [p1, p2] when the direct path is already contained.
    Otherwise tries both the exterior ring and all interior rings (holes) and
    returns the shortest candidate — this correctly handles transitions across
    keepout holes without routing all the way around the outer boundary.
    """
    from shapely.geometry import LineString

    if polygon.buffer(0.5).contains(LineString([p1, p2])):
        return [p1, p2]

    def plen(pts: list) -> float:
        return sum(
            math.hypot(pts[k + 1][0] - pts[k][0], pts[k + 1][1] - pts[k][1])
            for k in range(len(pts) - 1)
        )

    candidates = [_ring_route(list(polygon.exterior.coords[:-1]), p1, p2)]
    for interior in polygon.interiors:
        candidates.append(_ring_route(list(interior.coords[:-1]), p1, p2))

    return min(candidates, key=plen)


def _clip_strips_to_polygon(
    ring: list[tuple[float, float]],
    strips_y: list[float],
) -> list[tuple[float, float, float]]:
    """Intersect horizontal scan lines with a rotated polygon ring.

    Returns a list of (y, x_enter, x_exit) for each valid strip segment.
    Segments shorter than 1 m are discarded to avoid artefacts from arc tangencies.
    """
    n_ring = len(ring)
    raw_segs: list[tuple[float, float, float]] = []
    for y_strip in strips_y:
        xs: list[float] = []
        for i in range(n_ring - 1):
            ax, ay = ring[i]
            bx, by = ring[i + 1]
            if (ay <= y_strip < by) or (by <= y_strip < ay):
                t = (y_strip - ay) / (by - ay)
                xs.append(ax + t * (bx - ax))
        xs.sort()
        for i in range(0, len(xs) - 1, 2):
            if xs[i + 1] > xs[i] + 1.0:
                raw_segs.append((y_strip, xs[i], xs[i + 1]))
    return raw_segs


def _greedy_nn_order(
    raw_segs: list[tuple[float, float, float]],
    home_rot_x: float,
    home_rot_y: float,
    back_fn,
) -> list[tuple[float, float, float, float]]:
    """Greedy nearest-neighbour strip ordering in rotated space.

    At each step the drone extends the route to the unvisited strip whose
    closest endpoint is nearest to the current position (in rotated space),
    entering from that end.  For simple rectangular polygons this degenerates
    to standard boustrophedon; on C/U-shaped polygons it avoids repeated
    long gap-crossing transits by naturally grouping same-arm strips.

    *back_fn* converts (x, y) in rotated space back to EPSG:3067.
    Returns strips_3067 as list of (x1, y1, x2, y2).
    """
    n_strips = len(raw_segs)
    visited = [False] * n_strips

    def _rot_ep(i: int) -> tuple[tuple[float, float], tuple[float, float]]:
        y_s, x0_s, x1_s = raw_segs[i]
        return (x0_s, y_s), (x1_s, y_s)

    def _nearest_ep_dist(i: int, pos: tuple[float, float]) -> float:
        e1, e2 = _rot_ep(i)
        return min(
            math.hypot(e1[0] - pos[0], e1[1] - pos[1]),
            math.hypot(e2[0] - pos[0], e2[1] - pos[1]),
        )

    cur_rot: tuple[float, float] = (home_rot_x, home_rot_y)
    cur_strip = min(range(n_strips), key=lambda i: _nearest_ep_dist(i, cur_rot))

    strips_3067: list[tuple] = []
    while len(strips_3067) < n_strips:
        visited[cur_strip] = True
        ep_l, ep_r = _rot_ep(cur_strip)
        dl = math.hypot(ep_l[0] - cur_rot[0], ep_l[1] - cur_rot[1])
        dr = math.hypot(ep_r[0] - cur_rot[0], ep_r[1] - cur_rot[1])
        a_rot, b_rot = (ep_l, ep_r) if dl <= dr else (ep_r, ep_l)
        a = back_fn(a_rot[0], a_rot[1])
        b = back_fn(b_rot[0], b_rot[1])
        strips_3067.append((a[0], a[1], b[0], b[1]))
        cur_rot = b_rot
        remaining = [i for i in range(n_strips) if not visited[i]]
        if remaining:
            cur_strip = min(remaining, key=lambda i: _nearest_ep_dist(i, cur_rot))

    return strips_3067


def compute_route(
    polygon_3067,
    angle_deg: float,
    strip_spacing_m: float,
    photo_spacing_m: float,
    *,
    home_3067: tuple[float, float] | None = None,
) -> RouteResult:
    """Compute lawnmower strip pattern clipped to *polygon_3067*.

    *angle_deg*       — flight heading in degrees from North, CW (0=N, 90=E).
    *home_3067*       — optional (x, y) takeoff point for nearest-corner-first ordering.

    Inter-strip transitions that would exit the polygon are automatically rerouted
    along the exterior ring (shorter direction).
    """
    if polygon_3067.geom_type == "MultiPolygon":
        polygon_3067 = max(polygon_3067.geoms, key=lambda g: g.area)

    cx, cy = polygon_3067.centroid.x, polygon_3067.centroid.y

    # Rotate so flight direction becomes horizontal (parallel to x-axis).
    # angle_deg from North → rot_angle = angle_deg - 90 (CCW convention).
    rot_angle = angle_deg - 90.0
    rot_rad = math.radians(rot_angle)
    cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)

    rotated = rotate(polygon_3067, rot_angle, origin=(cx, cy), use_radians=False)
    ring = list(rotated.exterior.coords)
    _, miny, _, maxy = rotated.bounds

    # First/last strip centres at half a strip-spacing from the boundary so that the
    # polygon edge falls within the overlap zone of the two outermost strips.
    # Outer footprint extends fw*overlap/2 outside the boundary; for overlap > 50 %
    # (always true in practice) the second strip also reaches the edge.
    first_offset_m = strip_spacing_m / 2.0

    strips_y: list[float] = []
    y = miny + first_offset_m
    while y <= maxy + 1e-6:
        strips_y.append(y)
        y += strip_spacing_m

    raw_segs = _clip_strips_to_polygon(ring, strips_y)

    if not raw_segs:
        return RouteResult(
            strip_count=0,
            photo_count=0,
            strip_dist_m=0.0,
            turn_dist_m=0.0,
            angle_deg=angle_deg,
            strips_3067=[],
            transit_segs_3067=[],  # type: ignore[arg-type]
            first_wp_3067=None,
            last_wp_3067=None,
        )

    # Home position in rotated space (absolute coordinates, same frame as raw_segs).
    # Rotation is around (cx, cy), so a world point (hx, hy) maps to:
    #   rot_x = cx + (hx-cx)*cos_r - (hy-cy)*sin_r
    #   rot_y = cy + (hx-cx)*sin_r + (hy-cy)*cos_r
    # Default (no home): start from the polygon's midpoint so all strips are
    # equidistant in the y-direction and the NN picks a sensible starting strip.
    minx_r = min(x for x, _ in ring)
    maxx_r = max(x for x, _ in ring)
    home_rot_x = (minx_r + maxx_r) / 2
    home_rot_y = miny  # start from the y-near edge
    if home_3067 is not None:
        hx_rel = home_3067[0] - cx
        hy_rel = home_3067[1] - cy
        home_rot_x = cx + hx_rel * cos_r - hy_rel * sin_r
        home_rot_y = cy + hx_rel * sin_r + hy_rel * cos_r

    # Back-rotation helper: convert (x, y) in rotated space back to EPSG:3067.
    back_rad = math.radians(90.0 - angle_deg)
    cos_b, sin_b = math.cos(back_rad), math.sin(back_rad)

    def _back(px: float, py: float) -> tuple[float, float]:
        dx, dy = px - cx, py - cy
        return cx + dx * cos_b - dy * sin_b, cy + dx * sin_b + dy * cos_b

    strips_3067 = _greedy_nn_order(raw_segs, home_rot_x, home_rot_y, _back)

    strip_dist_m = sum(math.hypot(x2 - x1, y2 - y1) for x1, y1, x2, y2 in strips_3067)

    photo_count = sum(
        max(1, math.ceil(math.hypot(x2 - x1, y2 - y1) / photo_spacing_m) + 1)
        for x1, y1, x2, y2 in strips_3067
    )

    # Build transit segments with boundary routing for concave polygons.
    # Home↔route legs travel outside the survey area, so they keep the direct path.
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

    return RouteResult(
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


def estimate_flight_time(
    route: RouteResult,
    *,
    flight_height_m: float,
    auto_speed_ms: float,
    transit_speed_ms: float,
    takeoff_security_height_m: float = 50.0,
    home_3067: tuple[float, float] | None = None,
) -> float:
    """Return estimated total flight time in minutes.

    Includes takeoff climb, home→route transit, lawnmower survey, route→home,
    and descent.  Vertical speed is a conservative 3 m/s.
    """
    VERT_SPEED = 3.0  # m/s

    climb_m = max(takeoff_security_height_m, flight_height_m)
    climb_s = climb_m / VERT_SPEED

    home_transit_s = 0.0
    if home_3067 and route.first_wp_3067:
        d = math.hypot(
            route.first_wp_3067[0] - home_3067[0], route.first_wp_3067[1] - home_3067[1]
        )
        home_transit_s = d / transit_speed_ms

    survey_s = route.strip_dist_m / auto_speed_ms + route.turn_dist_m / transit_speed_ms

    return_s = 0.0
    if home_3067 and route.last_wp_3067:
        d = math.hypot(
            route.last_wp_3067[0] - home_3067[0], route.last_wp_3067[1] - home_3067[1]
        )
        return_s = d / transit_speed_ms

    descent_s = climb_m / VERT_SPEED

    return (climb_s + home_transit_s + survey_s + return_s + descent_s) / 60.0


# ---------------------------------------------------------------------------
# Route planning + GeoJSON serialization (shared by preview, export, estimate)
# ---------------------------------------------------------------------------


@dataclass
class PlannedRoute:
    """Output of :func:`plan_route` — the route plus its altitude/waypoint profile."""

    route: RouteResult
    altitude_profile: list[float]
    strip_waypoints: list | None
    transit_waypoints: list | None
    angle_deg: float


def footprint_spacings(
    drone, height_m: float, overlap_front_pct: float, overlap_side_pct: float
) -> tuple[float, float]:
    """Return ``(strip_spacing_m, photo_spacing_m)`` for the camera footprint at *height_m*."""
    p_m = drone.pixel_pitch_um * 1e-6
    f_m = drone.focal_length_mm * 1e-3
    fp_w = height_m * drone.image_width_px * p_m / f_m
    fp_h = height_m * drone.image_height_px * p_m / f_m
    return fp_w * (1 - overlap_side_pct / 100), fp_h * (1 - overlap_front_pct / 100)


def plan_route(
    polygon_3067,
    *,
    drone,
    height_m: float,
    overlap_front_pct: float,
    overlap_side_pct: float,
    angle_deg: float | None = None,
    home_3067: tuple[float, float] | None = None,
    advanced: bool = False,
    buildings: list | None = None,
    power_lines: list | None = None,
    adv_min_height_m: float | None = None,
    adv_max_height_m: float | None = None,
    adv_powerline_clearance_m: float | None = None,
    adv_slope_f: float | None = None,
    adv_min_dip_m: float | None = None,
) -> PlannedRoute:
    """Plan a lawnmower route — shared by the preview, export, and route_estimate paths.

    In advanced mode the adaptive (variable-altitude) planner is tried and falls back
    to a flat-altitude route at *height_m* on failure. *height_m* is the nominal flight
    height used for the footprint spacing and the flat altitude profile (simple mode and
    fallback); advanced mode derives per-strip altitudes internally from the ``adv_*``
    bounds. Returns the route, per-strip altitude profile, optional advanced-mode
    waypoint lists, and the heading used.
    """
    if angle_deg is None:
        angle_deg = compute_auto_angle(polygon_3067)
    strip_m, photo_m = footprint_spacings(
        drone, height_m, overlap_front_pct, overlap_side_pct
    )

    if advanced:
        try:
            from flightmanager.routing.adaptive_route import compute_adaptive_route

            route, altitude_profile, strip_wps, transit_wps = compute_adaptive_route(
                polygon_3067,
                angle_deg,
                buildings or [],
                power_lines or [],
                drone=drone,
                H_max=adv_max_height_m or height_m,
                H_min=adv_min_height_m,
                overlap_front_pct=overlap_front_pct,
                overlap_side_pct=overlap_side_pct,
                powerline_clearance_m=adv_powerline_clearance_m,
                slope_f=adv_slope_f,
                min_dip_m=adv_min_dip_m,
                home_3067=home_3067,
            )
            return PlannedRoute(
                route, altitude_profile, strip_wps, transit_wps, angle_deg
            )
        except Exception as exc:
            log.warning(
                "Adaptive route failed — %s; falling back to flat altitude", exc
            )

    route = compute_route(
        polygon_3067, angle_deg, strip_m, photo_m, home_3067=home_3067
    )
    altitude_profile = [height_m] * len(route.strips_3067)
    return PlannedRoute(route, altitude_profile, None, None, angle_deg)


def route_result_to_geojson(
    route: RouteResult,
    altitude_profile: list[float],
    drone,
    overlap_front_pct: float,
    strip_waypoints: list | None = None,
    transit_waypoints: list | None = None,
    adv_min_height_m: float | None = None,
) -> dict:
    """Convert a RouteResult + altitude profile to strips/transits GeoJSON dicts.

    When *strip_waypoints* is provided (advanced mode), each strip feature carries
    the full waypoint coordinates and ``wpt_alts``/``wpt_speeds`` properties.
    When *transit_waypoints* is provided, each transit feature carries a 1:1-safe
    ``altitude_m`` so the 3D view renders along long transits without dipping.
    """
    from shapely.geometry import LineString, mapping as _mapping

    from flightmanager.geo.geometry import reproject_to_4326

    def _seg_feat(i, x1, y1, x2, y2, alt, speed):
        wps = (
            strip_waypoints[i] if strip_waypoints and i < len(strip_waypoints) else None
        )
        if wps and len(wps) > 2:
            line = reproject_to_4326(LineString([(wp[0], wp[1]) for wp in wps]))
            wpt_alts = [round(wp[2], 1) for wp in wps]
            wpt_speeds = [round(wp[3], 2) for wp in wps]
            props = {
                "altitude_m": round(alt, 1),
                "speed_ms": round(speed, 2),
                "wpt_alts": wpt_alts,
                "wpt_speeds": wpt_speeds,
            }
        else:
            line = reproject_to_4326(LineString([(x1, y1), (x2, y2)]))
            props = {"altitude_m": round(alt, 1), "speed_ms": round(speed, 2)}
        return {
            "type": "Feature",
            "geometry": dict(_mapping(line)),
            "properties": props,
        }

    # transit_waypoints covers only inter-strip transits (N-1 entries).
    # When home_3067 was set, transit_segs_3067 has N+1 entries:
    #   [home→strip0, inter-strip×(N-1), stripN-1→home]
    # The inter-strip transits are at indices 1..N-1; their transit_waypoints
    # indices are 0..N-2.  Without home, segs[0..N-2] maps directly.
    n_strips = len(route.strips_3067)
    has_home_transit = len(route.transit_segs_3067) == n_strips + 1

    def _tw_idx(seg_idx: int) -> int | None:
        """Map a transit_segs index to the corresponding transit_waypoints index."""
        if has_home_transit:
            if 1 <= seg_idx <= n_strips - 1:
                return seg_idx - 1
            return None
        return seg_idx  # direct 1-to-1 when no home transits

    def _path_feat(pts, seg_idx=None):
        line = reproject_to_4326(LineString(pts))
        props: dict = {}
        tw_i = _tw_idx(seg_idx) if seg_idx is not None else None
        if (
            tw_i is not None
            and transit_waypoints
            and tw_i < len(transit_waypoints)
            and transit_waypoints[tw_i]
        ):
            tw_min = min(tw[2] for tw in transit_waypoints[tw_i])
            if adv_min_height_m is not None and altitude_profile:
                H_max_est = max(altitude_profile)
                if tw_min < H_max_est * 0.99:
                    props["altitude_m"] = round(adv_min_height_m, 1)
                else:
                    props["altitude_m"] = round(tw_min, 1)
            else:
                props["altitude_m"] = round(tw_min, 1)
        return {
            "type": "Feature",
            "geometry": dict(_mapping(line)),
            "properties": props,
        }

    strips = [
        _seg_feat(
            i,
            *s,
            alt=altitude_profile[i],
            speed=drone.auto_speed(altitude_profile[i], overlap_front_pct),
        )
        for i, s in enumerate(route.strips_3067)
    ]
    transits = [_path_feat(seg, i) for i, seg in enumerate(route.transit_segs_3067)]
    return {
        "strips_geojson": {"type": "FeatureCollection", "features": strips},
        "transits_geojson": {"type": "FeatureCollection", "features": transits},
    }
