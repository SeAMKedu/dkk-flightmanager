"""Per-strip altitude profile for advanced (obstacle-aware) flight mode.

Given a lawnmower route and obstacle layers (buildings, overhead power lines),
returns one target altitude (m AGL) per strip.  Near buildings the drone flies
lower (A2 1:1 rule — horizontal distance ≥ altitude).  Near 110 kV lines the
drone climbs to a safe clearance.  A two-pass slope filter ensures the altitude
profile is physically achievable given the drone's climb/descent rate.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from shapely.geometry import Point

if TYPE_CHECKING:
    from flightmanager.buildings import Building
    from flightmanager.powerlines import PowerLine
    from flightmanager.config import DroneConfig
    from flightmanager.route import RouteResult

_MAX_CLIMB_MS = 3.0  # conservative max vertical speed (m/s) for M3M/M3E in waypoint mode

_FLOOR_HEIGHT_M = 3.0  # metres per storey (kerrosluku)

# Estimated building height by MML kohdeluokka when kerrosluku is absent.
# Applied as d_effective = horizontal_distance + building_height so that the
# 1:1 proximity metric is relative to the rooftop rather than the ground.
_KOHDELUOKKA_HEIGHT_M: dict[int, float] = {
    42210: 7.0, 42211: 7.0, 42212: 10.0,  # asuinrakennus (residential; 42212 = 3+ floors)
    42220: 7.0, 42221: 7.0, 42222: 7.0,   # liike-/julkinen (commercial/public)
    42230: 4.0, 42231: 4.0, 42232: 4.0,   # lomarakennus (holiday/cabin)
    42240: 15.0, 42241: 15.0, 42242: 15.0, # teollinen (industrial/silo)
    42260: 10.0, 42261: 10.0, 42262: 10.0, # maatalous/varasto (agricultural/storage)
}
_DEFAULT_HEIGHT_M = 7.0


def building_height_m(b: Building) -> float:
    """Estimated building height in metres.

    Uses ``kerrosluku`` (floor count) × 3 m when available; falls back to
    a per-kohdeluokka heuristic.
    """
    if b.kerrosluku is not None and b.kerrosluku > 0:
        return b.kerrosluku * _FLOOR_HEIGHT_M
    return _KOHDELUOKKA_HEIGHT_M.get(b.kohdeluokka, _DEFAULT_HEIGHT_M)


def compute_altitude_profile(
    route: RouteResult,
    buildings: list,        # list[Building]
    power_lines: list,      # list[PowerLine]
    *,
    flight_height_m: float,
    min_h: float,
    powerline_clearance_m: float,
    overlap_front_pct: float,
    overlap_side_pct: float,
    slope_f: float,
    drone: DroneConfig,
) -> list[float]:
    """Return one altitude (m AGL) per strip in *route.strips_3067*.

    The altitude is derived from obstacle proximity:
    - Buildings: distance to nearest footprint edge → altitude (A2 1:1 rule).
    - Overhead power lines: if a line is within 200 m, altitude is raised to
      *powerline_clearance_m* (overrides buildings).

    A forward + backward slope-limiting pass ensures no transition between
    adjacent strips exceeds the physical climb/descent capability.
    """
    n = len(route.strips_3067)
    if n == 0:
        return []

    # Max altitude change per strip spacing (m/m)
    speed_ms = max(0.5, drone.auto_speed(flight_height_m, overlap_front_pct))
    slope_across = min(
        slope_f * drone.focal_length_mm / (drone.sensor_w_mm * (1.0 - overlap_side_pct / 100.0)),
        _MAX_CLIMB_MS / speed_ms,
    )

    # Shapely geometry for obstacle lookups
    pl_geoms = [pl.geometry for pl in power_lines if pl.is_overhead]

    # Raw target altitude per strip
    raw: list[float] = []
    for x1, y1, x2, y2 in route.strips_3067:
        mid = Point((x1 + x2) / 2.0, (y1 + y2) / 2.0)

        if buildings:
            # d_effective = horizontal_distance + building_height applies the
            # 1:1 proximity rule from the rooftop rather than the ground.
            # The building with the smallest d_eff is the binding constraint.
            best_d_eff = min(
                mid.distance(b.geometry) + building_height_m(b)
                for b in buildings
            )
            h = float(min(max(best_d_eff, min_h), flight_height_m))
        else:
            h = flight_height_m

        for pl_g in pl_geoms:
            if mid.distance(pl_g) < 200.0:
                h = max(h, powerline_clearance_m)

        raw.append(h)

    # Average strip spacing (m) used to convert slope (m/m) → max step (m)
    if n > 1:
        total = 0.0
        for i in range(1, n):
            x1a, y1a, x2a, y2a = route.strips_3067[i - 1]
            x1b, y1b, x2b, y2b = route.strips_3067[i]
            mxa = (x1a + x2a) / 2.0
            mya = (y1a + y2a) / 2.0
            mxb = (x1b + x2b) / 2.0
            myb = (y1b + y2b) / 2.0
            total += math.hypot(mxb - mxa, myb - mya)
        strip_spacing_m = total / (n - 1)
    else:
        strip_spacing_m = 50.0

    max_step = slope_across * strip_spacing_m

    # Forward pass: limit descent and climb from the preceding strip
    smooth = list(raw)
    for i in range(1, n):
        smooth[i] = max(smooth[i], smooth[i - 1] - max_step)
        smooth[i] = min(smooth[i], smooth[i - 1] + max_step)

    # Backward pass: ensure early enough start of any climb or descent
    for i in range(n - 2, -1, -1):
        smooth[i] = max(smooth[i], smooth[i + 1] - max_step)
        smooth[i] = min(smooth[i], smooth[i + 1] + max_step)

    # Clamp to min_h (don't cap above flight_height_m — power lines may exceed it)
    return [max(min_h, h) for h in smooth]
