"""Route estimation for survey polygons.

Computes the actual lawnmower strip pattern inside a survey polygon and
estimates photo count, route distance, and total flight time.
All geometry in EPSG:3067.

No file I/O, no server dependencies — pure Shapely math.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from shapely.affinity import rotate


@dataclass
class RouteResult:
    """Output of compute_route()."""
    strip_count: int
    photo_count: int
    strip_dist_m: float            # sum of strip traverse lengths
    turn_dist_m: float             # inter-strip transition distances
    angle_deg: float               # the angle that was used
    strips_3067: list[tuple]       # (x1,y1,x2,y2) per strip in EPSG:3067
    transit_segs_3067: list[list[tuple[float, float]]]  # each transit: ordered (x,y) waypoints
    first_wp_3067: tuple | None    # (x,y) start of first strip
    last_wp_3067: tuple | None     # (x,y) end of last strip

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
    long_edge = max(edges, key=lambda e: math.hypot(e[1][0]-e[0][0], e[1][1]-e[0][1]))
    dx = long_edge[1][0] - long_edge[0][0]
    dy = long_edge[1][1] - long_edge[0][1]
    return math.degrees(math.atan2(dx, dy)) % 180


def _boundary_route(
    polygon,
    p1: tuple[float, float],
    p2: tuple[float, float],
) -> list[tuple[float, float]]:
    """Return waypoints from p1 to p2 that stay inside polygon.

    Falls back to [p1, p2] when the direct path is already contained.
    Otherwise walks the exterior ring in the shorter direction.
    Both points are expected to lie on or very near the polygon boundary
    (scanline intersection endpoints).
    """
    from shapely.geometry import LineString

    if polygon.buffer(0.5).contains(LineString([p1, p2])):
        return [p1, p2]

    ring = list(polygon.exterior.coords[:-1])  # open ring, no repeated first vertex
    n = len(ring)

    def seg_idx(pt: tuple[float, float]) -> int:
        best_d, best = float("inf"), 0
        for i in range(n):
            ax, ay = ring[i]
            bx, by = ring[(i + 1) % n]
            dx, dy = bx - ax, by - ay
            l2 = dx * dx + dy * dy
            t = max(0.0, min(1.0, ((pt[0] - ax) * dx + (pt[1] - ay) * dy) / l2)) if l2 > 1e-10 else 0.0
            d = math.hypot(pt[0] - (ax + t * dx), pt[1] - (ay + t * dy))
            if d < best_d:
                best_d, best = d, i
        return best

    i1, i2 = seg_idx(p1), seg_idx(p2)

    # Number of ring vertices traversed in each direction
    steps_cw  = (i2 - i1) % n   # clockwise: ring[i1+1 .. i2]
    steps_ccw = (i1 - i2) % n   # counterclockwise: ring[i1 .. i2+1]

    path_cw  = [p1] + [ring[(i1 + 1 + k) % n] for k in range(steps_cw)]  + [p2]
    path_ccw = [p1] + [ring[(i1 - k) % n]     for k in range(steps_ccw)] + [p2]

    def plen(pts: list) -> float:
        return sum(
            math.hypot(pts[k + 1][0] - pts[k][0], pts[k + 1][1] - pts[k][1])
            for k in range(len(pts) - 1)
        )

    return path_cw if plen(path_cw) <= plen(path_ccw) else path_ccw


def compute_route(
    polygon_3067,
    angle_deg: float,
    strip_spacing_m: float,
    photo_spacing_m: float,
    *,
    footprint_width_m: float | None = None,
    home_3067: tuple[float, float] | None = None,
) -> RouteResult:
    """Compute lawnmower strip pattern clipped to *polygon_3067*.

    *angle_deg*       — flight heading in degrees from North, CW (0=N, 90=E).
    *footprint_width_m* — camera footprint width perpendicular to strips.
                          When supplied the first/last strips are placed at
                          footprint_width_m/2 from the boundary (matching DJI
                          Pilot 2's margin=0 behaviour).  Falls back to
                          strip_spacing_m/2 when None (legacy).
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
    n_ring = len(ring)
    _, miny, _, maxy = rotated.bounds

    # First/last strip centres at half-footprint from the boundary (DJI margin=0 convention).
    # With overlap_side > 50 % the footprint/2 offset guarantees edge coverage even when
    # the last strip lands up to strip_spacing before maxy.
    first_offset_m = (footprint_width_m / 2.0) if footprint_width_m is not None else (strip_spacing_m / 2.0)

    strips_y: list[float] = []
    y = miny + first_offset_m
    while y <= maxy + 1e-6:
        strips_y.append(y)
        y += strip_spacing_m

    raw_segs: list[tuple[float, float, float]] = []  # (y, x_enter, x_exit)
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
            if xs[i + 1] > xs[i] + 0.1:
                raw_segs.append((y_strip, xs[i], xs[i + 1]))

    if not raw_segs:
        return RouteResult(
            strip_count=0, photo_count=0, strip_dist_m=0.0,
            turn_dist_m=0.0, angle_deg=angle_deg,
            strips_3067=[], transit_segs_3067=[],  # type: ignore[arg-type]
            first_wp_3067=None, last_wp_3067=None,
        )

    # Nearest-first ordering: choose which y end starts closer to home
    home_rot_y = (miny + maxy) / 2
    home_rot_x = 0.0
    if home_3067 is not None:
        hx_rel = home_3067[0] - cx
        hy_rel = home_3067[1] - cy
        home_rot_x = hx_rel * cos_r - hy_rel * sin_r
        home_rot_y = hx_rel * sin_r + hy_rel * cos_r

    if abs(home_rot_y - maxy) < abs(home_rot_y - miny):
        raw_segs = raw_segs[::-1]

    # Boustrophedon: alternate left/right each strip; first strip toward home x
    midx = sum(s[1] + s[2] for s in raw_segs) / (2 * len(raw_segs))
    first_from_left = home_rot_x <= midx

    # Back-rotation helper
    back_rad = math.radians(90.0 - angle_deg)
    cos_b, sin_b = math.cos(back_rad), math.sin(back_rad)

    def _back(px: float, py: float) -> tuple[float, float]:
        dx, dy = px - cx, py - cy
        return cx + dx * cos_b - dy * sin_b, cy + dx * sin_b + dy * cos_b

    strips_3067: list[tuple] = []
    for i, (y_strip, x0, x1) in enumerate(raw_segs):
        from_left = first_from_left if i % 2 == 0 else not first_from_left
        a = _back(x0 if from_left else x1, y_strip)
        b = _back(x1 if from_left else x0, y_strip)
        strips_3067.append((a[0], a[1], b[0], b[1]))

    strip_dist_m = sum(math.hypot(x2 - x1, y2 - y1) for x1, y1, x2, y2 in strips_3067)

    photo_count = sum(
        max(1, math.ceil(math.hypot(x2-x1, y2-y1) / photo_spacing_m) + 1)
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
            p1 = (strips_3067[i][2],    strips_3067[i][3])
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
        d = math.hypot(route.first_wp_3067[0] - home_3067[0],
                       route.first_wp_3067[1] - home_3067[1])
        home_transit_s = d / transit_speed_ms

    survey_s = (route.strip_dist_m / auto_speed_ms
                + route.turn_dist_m / transit_speed_ms)

    return_s = 0.0
    if home_3067 and route.last_wp_3067:
        d = math.hypot(route.last_wp_3067[0] - home_3067[0],
                       route.last_wp_3067[1] - home_3067[1])
        return_s = d / transit_speed_ms

    descent_s = climb_m / VERT_SPEED

    return (climb_s + home_transit_s + survey_s + return_s + descent_s) / 60.0
