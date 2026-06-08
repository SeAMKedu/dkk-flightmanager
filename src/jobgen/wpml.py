"""Build template.kml and assemble the .kmz mapping route file.

All constants are derived from fixtures/FIXTURE_NOTES.md (Phase 0).

KMZ internal structure (must match exactly):
  <jobname>.kmz (ZIP)
  └── wpmz/
      ├── template.kml   ← survey polygon + parameters
      └── waylines.wpml  ← minimal stub; DJI Pilot 2 regenerates waypoints on import

Height mode:
  Both the reference fixture and generated missions use
  heightMode=relativeToStartPoint.  Terrain-following is activated by
  surfaceFollowModeEnable=1 and a dsmFile reference embedded in the KMZ —
  DJI Pilot 2 uses these to apply the DSM on import.

Battery budget:
  Estimated from actual strip intersections inside the survey polygon
  (route.py).  A job is flagged over_one_battery when estimated flight time
  exceeds battery_minutes.  Estimate includes takeoff climb and home transit.
  Actual consumption varies with wind, temperature, and payload.
"""

from __future__ import annotations

import logging
import math
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

from lxml import etree
from shapely.geometry.base import BaseGeometry

from jobgen.buildings import Building
from jobgen.config import (
    M3E_FOCAL_LENGTH_MM,
    M3E_IMAGE_HEIGHT_PX,
    M3E_IMAGE_WIDTH_PX,
    M3E_PIXEL_PITCH_UM,
    DroneConfig,
    FlightConfig,
)
from jobgen.crs import require_4326

log = logging.getLogger(__name__)

# ---- WPML constants (all from fixtures/FIXTURE_NOTES.md) ----
_WPML_NS  = "http://www.dji.com/wpmz/1.0.6"
_KML_NS   = "http://www.opengis.net/kml/2.2"
_WPML     = f"{{{_WPML_NS}}}"
_KML      = f"{{{_KML_NS}}}"
_NSMAP    = {None: _KML_NS, "wpml": _WPML_NS}

# Fallback M3M constants used when no DroneConfig is supplied (backward compat).
_DRONE_ENUM      = "77"
_DRONE_SUB_ENUM  = "0"
_PAYLOAD_ENUM    = "68"
_PAYLOAD_SUB_ENUM = "3"
_PAYLOAD_POS_IDX = "0"

# Default battery threshold (M3M).  Exported for use in tests.
ONE_BATTERY_MINUTES = 28.0

# Fallback capture interval when no DroneConfig is available.
# Calibrated from M3M: 8.9 m/s at 100 m AGL, 80% front overlap.
_FALLBACK_CAPTURE_INTERVAL_S = 2.38


def resolve_strip_speed(
    cfg: FlightConfig,
    drone: DroneConfig | None,
    height_m: float,
) -> float:
    """Return the strip speed (m/s) to write into the KMZ.

    If cfg.auto_flight_speed_ms is set it takes precedence (manual override).
    Otherwise the speed is calculated from the drone's min_capture_interval_s,
    the flight altitude, and the front overlap — matching DJI Pilot 2's own
    auto-speed logic.
    """
    if cfg.auto_flight_speed_ms is not None:
        return cfg.auto_flight_speed_ms
    if drone is not None:
        return drone.auto_speed(height_m, cfg.overlap_front_pct)
    # Fallback: M3M-like calculation when no drone profile is loaded.
    sensor_h_m = M3E_IMAGE_HEIGHT_PX * M3E_PIXEL_PITCH_UM * 1e-6
    footprint_m = height_m * sensor_h_m / (M3E_FOCAL_LENGTH_MM * 1e-3)
    trigger_m = (1 - cfg.overlap_front_pct / 100) * footprint_m
    return trigger_m / _FALLBACK_CAPTURE_INTERVAL_S


# ---- Public return type ----

@dataclass
class KmzResult:
    kmz_path: Path
    flight_height_m: float
    gsd_cm: float
    estimated_photo_count: int
    estimated_flight_time_min: float
    over_one_battery: bool
    drone_name: str = "m3m"
    strip_speed_ms: float = 0.0


# ---- Main entry point ----

def build_kmz(
    survey_4326: BaseGeometry,
    flight_config: FlightConfig,
    output_path: Path,
    *,
    dsm_path: Path | None = None,
    drone: DroneConfig | None = None,
) -> KmzResult:
    """Build a .kmz mapping route from a survey polygon and flight config.

    *survey_4326*  — final survey polygon in EPSG:4326, single Polygon with
                     no holes (validated by geometry.py before reaching here).
    *output_path*  — destination path for the .kmz file.
    *drone*        — drone + payload profile; falls back to M3M constants when None.

    Raises ValueError if the polygon is not a Polygon or has interior rings.
    """
    require_4326(survey_4326)
    _validate_polygon(survey_4326)

    height_m = (
        drone.height_from_gsd(flight_config.target_gsd_cm)
        if drone else flight_config.derived_flight_height_m
    )
    gsd_cm = flight_config.target_gsd_cm
    speed_ms = resolve_strip_speed(flight_config, drone, height_m)

    log.info(
        "Building KMZ: height=%.1f m, GSD=%.1f cm, speed=%.1f m/s, drone=%s, terrain_follow=%s",
        height_m, gsd_cm, speed_ms, drone.name if drone else "m3m(default)", dsm_path is not None,
    )

    # Battery budget estimate
    budget = _estimate_budget(survey_4326, flight_config, speed_ms=speed_ms, drone=drone)
    battery_limit = drone.battery_minutes if drone else ONE_BATTERY_MINUTES

    if budget["over_one_battery"]:
        log.warning(
            "Estimated flight time %.1f min exceeds one battery (%.0f min). "
            "Consider splitting the job.",
            budget["flight_time_min"], battery_limit,
        )

    # DSM filename inside the KMZ — confirmed path from fixture
    dsm_kmz_name = None
    if dsm_path is not None and dsm_path.exists():
        dsm_kmz_name = f"wpmz/res/dsm/{dsm_path.name}"

    template_xml = _build_template_kml(survey_4326, flight_config, dsm_kmz_name, speed_ms=speed_ms, drone=drone)
    waylines_xml = _build_waylines_stub(flight_config, speed_ms=speed_ms, drone=drone)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("wpmz/template.kml", template_xml)
        zf.writestr("wpmz/waylines.wpml", waylines_xml)
        if dsm_path is not None and dsm_path.exists():
            zf.write(dsm_path, dsm_kmz_name)
            log.info("DSM embedded in KMZ: %s", dsm_kmz_name)

    log.info("KMZ written: %s", output_path)

    return KmzResult(
        kmz_path=output_path,
        flight_height_m=height_m,
        gsd_cm=gsd_cm,
        estimated_photo_count=budget["photo_count"],
        estimated_flight_time_min=budget["flight_time_min"],
        over_one_battery=budget["over_one_battery"],
        drone_name=drone.name if drone else "m3m",
        strip_speed_ms=speed_ms,
    )


# ---- template.kml builder ----

def _build_template_kml(
    survey_4326: BaseGeometry,
    cfg: FlightConfig,
    dsm_kmz_name: str | None,
    *,
    speed_ms: float,
    drone: DroneConfig | None = None,
) -> str:
    height   = drone.height_from_gsd(cfg.target_gsd_cm) if drone else cfg.derived_flight_height_m
    height_s = f"{height:.15g}"
    now_ms   = str(int(time.time() * 1000))

    root = etree.Element(f"{_KML}kml", nsmap=_NSMAP)
    doc  = etree.SubElement(root, f"{_KML}Document")

    _tx(doc, f"{_WPML}createTime", now_ms)
    _tx(doc, f"{_WPML}updateTime", now_ms)

    # missionConfig
    mc = etree.SubElement(doc, f"{_WPML}missionConfig")
    _tx(mc, f"{_WPML}flyToWaylineMode",     "safely")
    _tx(mc, f"{_WPML}finishAction",          cfg.finish_action)
    _tx(mc, f"{_WPML}exitOnRCLost",          "executeLostAction")
    _tx(mc, f"{_WPML}executeRCLostAction",   cfg.rc_lost_action)
    _tx(mc, f"{_WPML}takeOffSecurityHeight", f"{cfg.takeoff_security_height_m:.6g}")
    _tx(mc, f"{_WPML}globalTransitionalSpeed", f"{cfg.transitional_speed_ms:.6g}")
    de = str(drone.drone_enum)         if drone else _DRONE_ENUM
    ds = str(drone.drone_sub_enum)     if drone else _DRONE_SUB_ENUM
    pe = str(drone.payload_enum)       if drone else _PAYLOAD_ENUM
    ps = str(drone.payload_sub_enum)   if drone else _PAYLOAD_SUB_ENUM
    pp = str(drone.payload_position_index) if drone else _PAYLOAD_POS_IDX
    img_fmt = drone.image_format       if drone else "visable,narrow_band"

    di = etree.SubElement(mc, f"{_WPML}droneInfo")
    _tx(di, f"{_WPML}droneEnumValue",    de)
    _tx(di, f"{_WPML}droneSubEnumValue", ds)
    _tx(mc, f"{_WPML}waylineAvoidLimitAreaMode", "0")
    pi_el = etree.SubElement(mc, f"{_WPML}payloadInfo")
    _tx(pi_el, f"{_WPML}payloadEnumValue",    pe)
    _tx(pi_el, f"{_WPML}payloadSubEnumValue", ps)
    _tx(pi_el, f"{_WPML}payloadPositionIndex", pp)

    # Folder
    folder = etree.SubElement(doc, f"{_KML}Folder")
    _tx(folder, f"{_WPML}templateType", "mapping2d")
    _tx(folder, f"{_WPML}templateId",   "0")

    wc = etree.SubElement(folder, f"{_WPML}waylineCoordinateSysParam")
    _tx(wc, f"{_WPML}coordinateMode",    "WGS84")
    _tx(wc, f"{_WPML}heightMode",        "relativeToStartPoint")
    _tx(wc, f"{_WPML}globalShootHeight", height_s)
    if dsm_kmz_name:
        # Terrain-follow via DSM — confirmed field names from fixture (2026-05-30)
        _tx(wc, f"{_WPML}surfaceFollowModeEnable",  "1")
        _tx(wc, f"{_WPML}isRealtimeSurfaceFollow",  "0")
        _tx(wc, f"{_WPML}surfaceRelativeHeight",     height_s)
        _tx(wc, f"{_WPML}dsmFile",                  dsm_kmz_name)

    _tx(folder, f"{_WPML}autoFlightSpeed", f"{speed_ms:.15g}")

    # Placemark
    pm = etree.SubElement(folder, f"{_KML}Placemark")
    _tx(pm, f"{_WPML}caliFlightEnable",        "0")
    _tx(pm, f"{_WPML}elevationOptimizeEnable",  "0")
    _tx(pm, f"{_WPML}smartObliqueEnable",       "0")
    _tx(pm, f"{_WPML}quickOrthoMappingEnable",  "0")
    _tx(pm, f"{_WPML}facadeWaylineEnable",      "0")
    _tx(pm, f"{_WPML}isLookAtSceneSet",         "0")
    _tx(pm, f"{_WPML}smartObliqueGimbalPitch",  "-45")
    _tx(pm, f"{_WPML}shootType",                "distance")
    _tx(pm, f"{_WPML}direction",                "0")
    _tx(pm, f"{_WPML}margin",                   "0")
    _tx(pm, f"{_WPML}efficiencyFlightModeEnable", "0")

    ov = etree.SubElement(pm, f"{_WPML}overlap")
    _tx(ov, f"{_WPML}orthoLidarOverlapH",  str(cfg.overlap_front_pct))
    _tx(ov, f"{_WPML}orthoLidarOverlapW",  str(cfg.overlap_side_pct))
    _tx(ov, f"{_WPML}orthoCameraOverlapH", str(cfg.overlap_front_pct))
    _tx(ov, f"{_WPML}orthoCameraOverlapW", str(cfg.overlap_side_pct))

    # Survey polygon
    poly_el  = etree.SubElement(pm, f"{_KML}Polygon")
    outer    = etree.SubElement(poly_el, f"{_KML}outerBoundaryIs")
    ring     = etree.SubElement(outer, f"{_KML}LinearRing")
    coords_el = etree.SubElement(ring, f"{_KML}coordinates")
    coords_el.text = "\n" + _polygon_coords(survey_4326) + "\n"

    _tx(pm, f"{_WPML}ellipsoidHeight", height_s)
    _tx(pm, f"{_WPML}height",          height_s)

    # payloadParam
    pp_el = etree.SubElement(folder, f"{_WPML}payloadParam")
    _tx(pp_el, f"{_WPML}payloadPositionIndex", pp)
    _tx(pp_el, f"{_WPML}dewarpingEnable",     "0")
    _tx(pp_el, f"{_WPML}returnMode",          "singleReturnFirst")
    _tx(pp_el, f"{_WPML}samplingRate",        "240000")
    _tx(pp_el, f"{_WPML}scanningMode",        "nonRepetitive")
    _tx(pp_el, f"{_WPML}modelColoringEnable", "0")
    _tx(pp_el, f"{_WPML}imageFormat",         img_fmt)

    return etree.tostring(
        root,
        xml_declaration=True,
        encoding="UTF-8",
        pretty_print=True,
    ).decode("utf-8")


def _build_waylines_stub(cfg: FlightConfig, *, speed_ms: float, drone: DroneConfig | None = None) -> str:
    """Minimal waylines.wpml — missionConfig only, no waypoints.

    DJI Pilot 2 regenerates the lawnmower waypoints from template.kml on
    import.  We include this file because the reference KMZ contains it
    (open question 6) and omitting it may cause import failures.
    """
    de = str(drone.drone_enum)             if drone else _DRONE_ENUM
    ds = str(drone.drone_sub_enum)         if drone else _DRONE_SUB_ENUM
    pe = str(drone.payload_enum)           if drone else _PAYLOAD_ENUM
    ps = str(drone.payload_sub_enum)       if drone else _PAYLOAD_SUB_ENUM
    pp = str(drone.payload_position_index) if drone else _PAYLOAD_POS_IDX

    root = etree.Element(f"{_KML}kml", nsmap=_NSMAP)
    doc  = etree.SubElement(root, f"{_KML}Document")

    mc = etree.SubElement(doc, f"{_WPML}missionConfig")
    _tx(mc, f"{_WPML}flyToWaylineMode",     "safely")
    _tx(mc, f"{_WPML}finishAction",          cfg.finish_action)
    _tx(mc, f"{_WPML}exitOnRCLost",          "executeLostAction")
    _tx(mc, f"{_WPML}executeRCLostAction",   cfg.rc_lost_action)
    _tx(mc, f"{_WPML}takeOffSecurityHeight", f"{cfg.takeoff_security_height_m:.6g}")
    _tx(mc, f"{_WPML}globalTransitionalSpeed", f"{cfg.transitional_speed_ms:.6g}")
    di = etree.SubElement(mc, f"{_WPML}droneInfo")
    _tx(di, f"{_WPML}droneEnumValue",    de)
    _tx(di, f"{_WPML}droneSubEnumValue", ds)
    _tx(mc, f"{_WPML}waylineAvoidLimitAreaMode", "0")
    pi_el = etree.SubElement(mc, f"{_WPML}payloadInfo")
    _tx(pi_el, f"{_WPML}payloadEnumValue",    pe)
    _tx(pi_el, f"{_WPML}payloadSubEnumValue", ps)
    _tx(pi_el, f"{_WPML}payloadPositionIndex", pp)

    # Empty folder — Pilot 2 fills this from the template polygon
    folder = etree.SubElement(doc, f"{_KML}Folder")
    _tx(folder, f"{_WPML}templateId",        "0")
    _tx(folder, f"{_WPML}executeHeightMode", "WGS84")
    _tx(folder, f"{_WPML}waylineId",         "0")
    _tx(folder, f"{_WPML}distance",          "0")
    _tx(folder, f"{_WPML}duration",          "0")
    _tx(folder, f"{_WPML}autoFlightSpeed",   f"{speed_ms:.15g}")

    return etree.tostring(
        root,
        xml_declaration=True,
        encoding="UTF-8",
        pretty_print=True,
    ).decode("utf-8")


# ---- Battery budget estimate ----

def _estimate_budget(
    survey_4326: BaseGeometry,
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
    from jobgen import route as _route
    from jobgen.geometry import reproject_to_3067

    pitch_m = (drone.pixel_pitch_um  if drone else M3E_PIXEL_PITCH_UM)  * 1e-6
    focal_m = (drone.focal_length_mm if drone else M3E_FOCAL_LENGTH_MM) * 1e-3
    w_px    =  drone.image_width_px  if drone else M3E_IMAGE_WIDTH_PX
    h_px    =  drone.image_height_px if drone else M3E_IMAGE_HEIGHT_PX
    bat_min =  drone.battery_minutes if drone else ONE_BATTERY_MINUTES
    H       =  drone.height_from_gsd(cfg.target_gsd_cm) if drone else cfg.derived_flight_height_m

    strip_m = H * (w_px * pitch_m) / focal_m * (1 - cfg.overlap_side_pct  / 100)
    photo_m = H * (h_px * pitch_m) / focal_m * (1 - cfg.overlap_front_pct / 100)

    survey_3067 = reproject_to_3067(survey_4326)
    angle_deg = _route.compute_auto_angle(survey_3067)
    result = _route.compute_route(survey_3067, angle_deg, strip_m, photo_m,
                                  home_3067=home_3067)
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


# ---- Helpers ----

def _validate_polygon(geom: BaseGeometry) -> None:
    from shapely.geometry import Polygon
    if not isinstance(geom, Polygon):
        raise ValueError(
            f"KMZ polygon must be a single Polygon, got {type(geom).__name__}. "
            f"Apply multipart_policy before calling build_kmz()."
        )
    if list(geom.interiors):
        raise ValueError(
            "KMZ polygon has interior rings (holes). "
            "Apply hole_policy before calling build_kmz()."
        )


def _polygon_coords(geom: BaseGeometry) -> str:
    """Format exterior ring as 'lon,lat,0' triplets — the DJI coordinate order."""
    lines = []
    for x, y in geom.exterior.coords:
        lines.append(f"                {x},{y},0")
    return "\n".join(lines)


def build_homes_kml(
    buildings: list[Building],
    output_path: Path,
    home_safety: "HomeSafetyConfig | None" = None,
) -> Path:
    """Write a DJI Pilot 2 custom map layer KML with one pin per building.

    Format confirmed from PIN-20260529224114.kml (fixtures/FIXTURE_NOTES.md).
    The file can be imported into Pilot 2 as a new map layer for situational
    awareness — pins appear on the map overlay during pre-flight planning.

    Color coding:
      red    — buildings subject to the keep-out rule for the configured
               subcategory (residential for A2; residential + commercial +
               holiday + industrial for A3).
      yellow — all other buildings (noted but outside the keep-out codes).

    Blue, purple and green are left free for the operator's own use.

    Returns *output_path*.
    """
    from jobgen.config import HomeSafetyConfig as _HSC
    cfg = home_safety or _HSC()

    red_codes    = set(cfg.residential_kohdeluokka)
    yellow_codes = set(cfg.a3_additional_kohdeluokka)
    if cfg.operating_subcategory == "A3":
        red_codes |= yellow_codes
        yellow_codes = set()

    shown_codes = red_codes | yellow_codes

    def _style(b: Building) -> str:
        return "#dji_style_red" if b.kohdeluokka in red_codes else "#dji_style_yellow"

    def _label(b: Building) -> str:
        mapping = {
            42210: "residence", 42211: "residence", 42212: "residence",
            42220: "commercial", 42221: "commercial", 42222: "commercial",
            42230: "holiday", 42231: "holiday", 42232: "holiday",
            42240: "industrial", 42241: "industrial", 42242: "industrial",
        }
        return mapping.get(b.kohdeluokka, f"building-{b.kohdeluokka}")

    # DJI colour values (AABBGGRR — confirmed from fixture)
    _COLOURS = {
        "red":    "#FF393CE2",
        "green":  "#FF6BBE19",
        "yellow": "#FF00BBFF",
        "blue":   "#FFF08C2D",
        "purple": "#FFE020B6",
    }

    timestamp = time.strftime("%Y%m%d%H%M%S")

    # Build XML — Document must have xmlns="" to match Pilot 2 export format
    lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append('<kml xmlns="http://www.opengis.net/kml/2.2">')
    lines.append(f'<Document xmlns=""><name>homes-{timestamp}.kml</name>')

    for colour, hex_val in _COLOURS.items():
        lines.append(f'<Style id="dji_style_{colour}">')
        lines.append(f'  <IconStyle><color>{hex_val}</color></IconStyle>')
        lines.append(f'  <LabelStyle><color>{hex_val}</color></LabelStyle>')
        lines.append('</Style>')

    for b in [b for b in buildings if b.kohdeluokka in shown_codes]:
        centroid = b.geometry.centroid
        style = _style(b)
        label = _label(b)
        lines.append('<Placemark>')
        lines.append(f'  <name>{label}</name>')
        lines.append(f'  <description>mtk_id={b.mtk_id} kohdeluokka={b.kohdeluokka}</description>')
        lines.append(f'  <styleUrl>{style}</styleUrl>')
        lines.append('  <Point>')
        lines.append(f'    <coordinates>{centroid.x},{centroid.y},0.0</coordinates>')
        lines.append('    <altitudeMode>absolute</altitudeMode>')
        lines.append('  </Point>')
        lines.append('</Placemark>')

    lines.append('</Document></kml>')

    pin_count = sum(1 for b in buildings if b.kohdeluokka in shown_codes)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Homes KML written: %d pin(s) → %s", pin_count, output_path)
    return output_path


def _tx(parent: etree._Element, tag: str, text: str) -> etree._Element:
    el = etree.SubElement(parent, tag)
    el.text = text
    return el
