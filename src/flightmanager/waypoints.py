"""Per-waypoint flight planning: budget estimation and 3D altitude profiles.

Bridges route geometry (route.py) and drone/flight configuration (config.py).
budget_estimate() is the current workhorse used by wpml.py.
compute_altitude_profile() is the stub for upcoming 3D terrain-following work.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from flightmanager import route as _route
from flightmanager.config import (
    M3E_FOCAL_LENGTH_MM,
    M3E_IMAGE_HEIGHT_PX,
    M3E_IMAGE_WIDTH_PX,
    M3E_PIXEL_PITCH_UM,
    DroneConfig,
    FlightConfig,
)
from flightmanager.geometry import reproject_to_3067

log = logging.getLogger(__name__)

# Default battery threshold (M3M fallback when no DroneConfig is supplied).
ONE_BATTERY_MINUTES = 28.0


@dataclass
class Waypoint:
    """A single flight waypoint with 3D position (EPSG:4326)."""
    lon: float
    lat: float
    altitude_m: float


@dataclass
class AltitudeProfile:
    """Per-waypoint altitude profile for a survey route."""
    waypoints: list[Waypoint] = field(default_factory=list)
    max_altitude_m: float = 0.0
    min_altitude_m: float = 0.0


def budget_estimate(
    survey_4326,
    cfg: FlightConfig,
    *,
    speed_ms: float,
    drone: DroneConfig | None = None,
    home_3067: tuple[float, float] | None = None,
) -> dict:
    """Estimate photo count and flight time for manifest + battery warning.

    Uses actual scanline strip intersections in EPSG:3067 (via route.py).
    Returns dict: photo_count, flight_time_min, over_one_battery.
    """
    pitch_m = (drone.pixel_pitch_um  if drone else M3E_PIXEL_PITCH_UM)  * 1e-6
    focal_m = (drone.focal_length_mm if drone else M3E_FOCAL_LENGTH_MM) * 1e-3
    w_px    =  drone.image_width_px  if drone else M3E_IMAGE_WIDTH_PX
    h_px    =  drone.image_height_px if drone else M3E_IMAGE_HEIGHT_PX
    bat_min =  drone.battery_minutes if drone else ONE_BATTERY_MINUTES
    H       =  drone.height_from_gsd(cfg.target_gsd_cm) if drone else cfg.derived_flight_height_m

    footprint_m = H * (w_px * pitch_m) / focal_m
    strip_m     = footprint_m * (1 - cfg.overlap_side_pct  / 100)
    photo_m     = H * (h_px * pitch_m) / focal_m * (1 - cfg.overlap_front_pct / 100)

    survey_3067 = reproject_to_3067(survey_4326)
    angle_deg   = _route.compute_auto_angle(survey_3067)
    result      = _route.compute_route(
        survey_3067, angle_deg, strip_m, photo_m,
        home_3067=home_3067,
    )
    flight_time_min = _route.estimate_flight_time(
        result,
        flight_height_m=H,
        auto_speed_ms=speed_ms,
        transit_speed_ms=cfg.transitional_speed_ms,
        takeoff_security_height_m=cfg.takeoff_security_height_m,
        home_3067=home_3067,
    )
    return {
        "photo_count":      result.photo_count,
        "flight_time_min":  flight_time_min,
        "over_one_battery": flight_time_min > bat_min,
    }


def compute_altitude_profile(
    strips_3067: list,
    buildings,
    max_altitude_m: float,
) -> AltitudeProfile:
    """Compute per-waypoint altitude using the 1:1 horizontal-distance rule.

    Altitude at each waypoint = max(min_safe_m, horizontal distance to nearest building).
    Far from buildings → fly at max_altitude_m.
    Near buildings → descend so altitude ≈ horizontal distance to nearest structure.

    TODO: implement — requires buildings in EPSG:3067 + waypoint spacing config.
          Called by wpml.build_kmz() once 3D mode is enabled.
    """
    raise NotImplementedError("3D altitude profile not yet implemented")
