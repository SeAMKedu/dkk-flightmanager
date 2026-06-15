"""Build explicit waylines.wpml for advanced (obstacle-aware) flight mode.

Generates a full DJI WPML waypoint sequence with one altitude per strip.
Each strip has an independent shooting session (startContinuousShooting at
the strip start, stopContinuousShooting at the strip end).  Transit waypoints
between strips carry no actions.

Route ordering assumption (from route.py):
  transit_segs_3067 has N+1 elements when a home point is included
  (transit[0]=home→strip0, transit[1..N-1]=inter-strip, transit[N]=stripN→home),
  or N-1 elements when no home point (all elements are inter-strip).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from lxml import etree
from pyproj import Transformer

if TYPE_CHECKING:
    from flightmanager.config import DroneConfig, FlightConfig
    from flightmanager.route import RouteResult

_WPML_NS = "http://www.dji.com/wpmz/1.0.6"
_KML_NS  = "http://www.opengis.net/kml/2.2"
_WPML    = f"{{{_WPML_NS}}}"
_KML     = f"{{{_KML_NS}}}"
_NSMAP   = {None: _KML_NS, "wpml": _WPML_NS}

_T = Transformer.from_crs("EPSG:3067", "EPSG:4326", always_xy=True)


def _to_lon_lat(x: float, y: float) -> tuple[float, float]:
    lon, lat = _T.transform(x, y)
    return float(lon), float(lat)


def _bearing(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Initial bearing in degrees (0=N, 90=E) from point 1 to point 2."""
    dlon = math.radians(lon2 - lon1)
    lat1r = math.radians(lat1)
    lat2r = math.radians(lat2)
    x = math.sin(dlon) * math.cos(lat2r)
    y = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon)
    return math.degrees(math.atan2(x, y))


def _tx(parent: etree._Element, tag: str, text: str) -> etree._Element:
    el = etree.SubElement(parent, tag)
    el.text = text
    return el


def _photo_interval_m(height_m: float, drone: DroneConfig, overlap_front_pct: float) -> float:
    """Along-track distance between consecutive photos (m)."""
    footprint_m = height_m * drone.sensor_h_mm / drone.focal_length_mm
    return max(0.5, footprint_m * (1.0 - overlap_front_pct / 100.0))


def _build_waypoint_list(
    strips_3067: list,
    altitude_profile: list[float],
    inter_transits: list,
    strip_waypoints: list[list[tuple[float, float, float]]] | None = None,
    transit_waypoints: list[list[tuple[float, float, float]]] | None = None,
    adv_min_height_m: float | None = None,
) -> tuple[list[tuple], list[int], list[int]]:
    """Build flat (x, y, alt, strip_idx, is_start, is_end) waypoint list.

    Returns (wps, strip_start_wp_idx, strip_end_wp_idx).
    Each entry: (x3067, y3067, alt_m, strip_idx_or_None, is_start, is_end).

    U-turn altitude strategy: strip start/end waypoints at each transition are
    clamped to ``min(strip_end_alt, next_strip_start_alt)`` so short U-turns
    stay level.  Transit altitude is the flat minimum across all sampled transit
    waypoints.  When any transit waypoint is constrained below H_max (path passes
    near a building), the entire transit uses ``adv_min_height_m`` instead — the
    10 m sampling interval can miss the true closest approach, and flying at H_min
    keeps the 1:1 exclusion radius at its minimum while the keepout-hole routing
    guarantees the path itself clears the building.
    """
    wps: list[tuple] = []
    strip_start_wp_idx: list[int] = []
    strip_end_wp_idx: list[int] = []
    n = len(strips_3067)

    def _raw_end_alt(i: int) -> float:
        if strip_waypoints and strip_waypoints[i]:
            return strip_waypoints[i][-1][2]
        return altitude_profile[i]

    def _raw_start_alt(i: int) -> float:
        if strip_waypoints and strip_waypoints[i]:
            return strip_waypoints[i][0][2]
        return altitude_profile[i]

    # Pre-compute a level "turn altitude" for every inter-strip transition.
    turn_alts = [
        min(_raw_end_alt(i), _raw_start_alt(i + 1))
        for i in range(n - 1)
    ]

    # Waypoint tuple: (x3067, y3067, alt_m, strip_idx, is_start, is_end)
    # strip_idx:
    #   i (int)  → this waypoint belongs to strip i (start, end, or intermediate)
    #   None     → transit waypoint between strips
    # Intermediate within-strip waypoints: strip_idx=i, is_start=False, is_end=False

    for i, (x1, y1, x2, y2) in enumerate(strips_3067):
        wps_for_strip = strip_waypoints[i] if strip_waypoints else None

        # Strip endpoint altitudes are overridden by the adjacent turn altitude
        # so the strip-to-transit altitude boundary is seamless.
        sa_override = turn_alts[i - 1] if i > 0     else None   # start of strip i
        ea_override = turn_alts[i]     if i < n - 1 else None   # end   of strip i

        if wps_for_strip and len(wps_for_strip) > 2:
            # Variable-altitude strip — emit intermediate waypoints so the drone
            # climbs/descends continuously rather than flying level.
            # strip_waypoints entries are (x, y, alt, speed) 4-tuples.
            strip_start_wp_idx.append(len(wps))
            sx, sy = wps_for_strip[0][0], wps_for_strip[0][1]
            sa = sa_override if sa_override is not None else wps_for_strip[0][2]
            wps.append((sx, sy, sa, i, True, False))

            for wp in wps_for_strip[1:-1]:
                mx, my, ma = wp[0], wp[1], wp[2]
                wps.append((mx, my, ma, i, False, False))

            strip_end_wp_idx.append(len(wps))
            ex, ey = wps_for_strip[-1][0], wps_for_strip[-1][1]
            ea = ea_override if ea_override is not None else wps_for_strip[-1][2]
            wps.append((ex, ey, ea, i, False, True))
        else:
            # Constant-altitude strip
            alt = altitude_profile[i]
            sa = sa_override if sa_override is not None else alt
            ea = ea_override if ea_override is not None else alt
            strip_start_wp_idx.append(len(wps))
            wps.append((x1, y1, sa, i, True, False))
            strip_end_wp_idx.append(len(wps))
            wps.append((x2, y2, ea, i, False, True))

        if i < n - 1:
            transit = inter_transits[i]
            transit_alt = turn_alts[i]
            if transit_waypoints and i < len(transit_waypoints) and transit_waypoints[i]:
                tw_min = min(ta for _, _, ta in transit_waypoints[i])
                if adv_min_height_m is not None and altitude_profile:
                    H_max_est = max(altitude_profile)
                    if tw_min < H_max_est * 0.99:
                        # Transit is building-constrained. Coarse 10 m sampling can
                        # miss the true closest approach, so using the sampled
                        # minimum is not safe. Drop to adv_min_height_m where the
                        # keepout-hole routing already guarantees safe clearance.
                        transit_alt = min(transit_alt, adv_min_height_m)
                    else:
                        transit_alt = min(transit_alt, tw_min)
                else:
                    transit_alt = min(transit_alt, tw_min)
            for tx, ty in transit[1:-1]:
                wps.append((tx, ty, transit_alt, None, False, False))

    return wps, strip_start_wp_idx, strip_end_wp_idx


def build_waylines(  # noqa: C901
    route: RouteResult,
    altitude_profile: list[float],
    *,
    drone: DroneConfig,
    cfg: FlightConfig,
    strip_waypoints: list[list[tuple[float, float, float]]] | None = None,
    transit_waypoints: list[list[tuple[float, float, float]]] | None = None,
) -> str:
    """Return waylines.wpml XML string with per-strip (or per-waypoint) altitudes.

    *route*             — RouteResult from compute_route() (EPSG:3067).
    *altitude_profile*  — one altitude (m AGL) per entry in route.strips_3067.
    *strip_waypoints*   — per-strip waypoint lists from ``compute_adaptive_route()``.
                          Intermediate waypoints are emitted so the drone
                          climbs/descends continuously along each strip.
    *transit_waypoints* — per-transit waypoint lists from ``compute_adaptive_route()``.
                          1:1-compliant altitude at each transit point so the
                          drone does not fly at max(end, start) near buildings.
    """
    n = len(route.strips_3067)
    if n == 0 or len(altitude_profile) != n:
        raise ValueError(
            f"altitude_profile length ({len(altitude_profile)}) "
            f"must match strip count ({n})"
        )

    pp       = str(drone.payload_position_index)
    img_fmt  = drone.image_format
    speed_ms = (
        cfg.auto_flight_speed_ms
        if cfg.auto_flight_speed_ms is not None
        else drone.auto_speed(altitude_profile[0], cfg.overlap_front_pct)
    )

    # Inter-strip transit segments (strip-end → next-strip-start paths)
    # transit_segs includes home transits when a home point was set.
    has_home = len(route.transit_segs_3067) == n + 1
    if has_home:
        inter_transits = route.transit_segs_3067[1:n]
    else:
        inter_transits = route.transit_segs_3067   # N-1 elements

    # ── Build flat waypoint list ──────────────────────────────────────────────
    wps, strip_start_wp_idx, strip_end_wp_idx = _build_waypoint_list(
        route.strips_3067, altitude_profile, inter_transits,
        strip_waypoints, transit_waypoints,
        adv_min_height_m=cfg.adv_min_height_m,
    )
    total_wps = len(wps)

    # Pre-compute 4326 coordinates and bearings
    coords_4326 = [_to_lon_lat(wp[0], wp[1]) for wp in wps]
    bearings: list[float] = []
    for i in range(total_wps):
        if i < total_wps - 1:
            lon1, lat1 = coords_4326[i]
            lon2, lat2 = coords_4326[i + 1]
            if abs(lon2 - lon1) < 1e-12 and abs(lat2 - lat1) < 1e-12:
                bearings.append(bearings[-1] if bearings else 0.0)
            else:
                bearings.append(_bearing(lon1, lat1, lon2, lat2))
        else:
            bearings.append(bearings[-1] if bearings else 0.0)

    # ── Build XML ─────────────────────────────────────────────────────────────
    root = etree.Element(f"{_KML}kml", nsmap=_NSMAP)
    doc  = etree.SubElement(root, f"{_KML}Document")

    # missionConfig
    mc = etree.SubElement(doc, f"{_WPML}missionConfig")
    _tx(mc, f"{_WPML}flyToWaylineMode",       "safely")
    _tx(mc, f"{_WPML}finishAction",            cfg.finish_action)
    _tx(mc, f"{_WPML}exitOnRCLost",            "executeLostAction")
    _tx(mc, f"{_WPML}executeRCLostAction",     cfg.rc_lost_action)
    _tx(mc, f"{_WPML}takeOffSecurityHeight",   f"{cfg.takeoff_security_height_m:.6g}")
    _tx(mc, f"{_WPML}globalRTHHeight",         f"{cfg.rth_height_m:.6g}")
    _tx(mc, f"{_WPML}globalTransitionalSpeed", f"{cfg.transitional_speed_ms:.6g}")
    di = etree.SubElement(mc, f"{_WPML}droneInfo")
    _tx(di, f"{_WPML}droneEnumValue",    str(drone.drone_enum))
    _tx(di, f"{_WPML}droneSubEnumValue", str(drone.drone_sub_enum))
    _tx(mc, f"{_WPML}waylineAvoidLimitAreaMode", "0")
    pi_el = etree.SubElement(mc, f"{_WPML}payloadInfo")
    _tx(pi_el, f"{_WPML}payloadEnumValue",     str(drone.payload_enum))
    _tx(pi_el, f"{_WPML}payloadSubEnumValue",  str(drone.payload_sub_enum))
    _tx(pi_el, f"{_WPML}payloadPositionIndex", pp)

    folder = etree.SubElement(doc, f"{_KML}Folder")
    _tx(folder, f"{_WPML}templateId",        "0")
    _tx(folder, f"{_WPML}executeHeightMode", "relativeToStartPoint")
    _tx(folder, f"{_WPML}waylineId",         "0")
    _tx(folder, f"{_WPML}distance",          "0")
    _tx(folder, f"{_WPML}duration",          "0")
    _tx(folder, f"{_WPML}autoFlightSpeed",   f"{speed_ms:.15g}")

    # startActionGroup — gimbal nadir + hover + focus (matches DJI Pilot 2 fixture)
    sag = etree.SubElement(folder, f"{_WPML}startActionGroup")
    _add_gimbal_rotate(sag, action_id=0, pp=pp)
    _add_hover(sag, action_id=1, hover_s=0.5)
    _add_set_focus_manual(sag, action_id=2, pp=pp)
    _add_focus_infinite(sag, action_id=3, pp=pp)
    _add_hover(sag, action_id=4, hover_s=1.0)

    # ── Per-segment shooting sessions ─────────────────────────────────────────
    # Each adjacent pair of waypoints within a strip gets its own
    # startContinuousShooting / stopContinuousShooting action pair with
    # photo_m calibrated to the AVERAGE altitude of that segment.  This keeps
    # speed and photo interval in sync as altitude changes within the strip:
    # both scale with altitude so the SD card is never overloaded and forward
    # overlap stays consistent throughout.
    sessions: list[tuple[int, int, float]] = []   # (start_wp_idx, end_wp_idx, photo_m)
    for strip_i in range(n):
        s = strip_start_wp_idx[strip_i]
        e = strip_end_wp_idx[strip_i]
        if e - s >= 2:
            # Variable-altitude strip: one session per segment
            for k in range(s, e):
                avg_alt = (wps[k][2] + wps[k + 1][2]) / 2.0
                sessions.append((k, k + 1, _photo_interval_m(avg_alt, drone, cfg.overlap_front_pct)))
        else:
            # Single-segment strip: one session using average of start/end altitude
            avg_alt = (wps[s][2] + wps[e][2]) / 2.0
            sessions.append((s, e, _photo_interval_m(avg_alt, drone, cfg.overlap_front_pct)))

    # Build fast lookup maps: wp_idx → sessions starting/ending there
    sessions_starting: dict[int, list[tuple[int, float]]] = {}
    sessions_ending:   dict[int, list[int]] = {}
    for sess_s, sess_e, sess_pm in sessions:
        sessions_starting.setdefault(sess_s, []).append((sess_e, sess_pm))
        sessions_ending.setdefault(sess_e, []).append(sess_s)

    # ── Placemarks ────────────────────────────────────────────────────────────
    ag_idx = 0
    for i, (x3067, y3067, alt_m, strip_idx, is_start, is_end) in enumerate(wps):
        lon, lat = coords_4326[i]
        brg = bearings[i]

        wp_speed = speed_ms
        if strip_idx is not None:
            if cfg.auto_flight_speed_ms is None:
                wp_speed = drone.auto_speed(alt_m, cfg.overlap_front_pct)

        pm = etree.SubElement(folder, f"{_KML}Placemark")
        pt = etree.SubElement(pm, f"{_KML}Point")
        co = etree.SubElement(pt, f"{_KML}coordinates")
        co.text = f"\n            {lon:.13f},{lat:.13f}\n          "

        _tx(pm, f"{_WPML}index",          str(i))
        _tx(pm, f"{_WPML}executeHeight",  f"{alt_m:.15g}")
        _tx(pm, f"{_WPML}waypointSpeed",  f"{wp_speed:.15g}")

        hp = etree.SubElement(pm, f"{_WPML}waypointHeadingParam")
        _tx(hp, f"{_WPML}waypointHeadingMode",      "followWayline")
        _tx(hp, f"{_WPML}waypointHeadingAngle",     f"{brg:.15g}")
        _tx(hp, f"{_WPML}waypointPoiPoint",         "0.000000,0.000000,0.000000")
        _tx(hp, f"{_WPML}waypointHeadingAngleEnable", "1")
        _tx(hp, f"{_WPML}waypointHeadingPathMode",  "followBadArc")
        _tx(hp, f"{_WPML}waypointHeadingPoiIndex",  "0")

        # Intermediate within-strip waypoints (strip_idx=i, not start, not end)
        # are distinct from transit waypoints (strip_idx=None).
        is_instrip_mid = (strip_idx is not None and not is_start and not is_end)

        tp = etree.SubElement(pm, f"{_WPML}waypointTurnParam")
        if is_start:
            _tx(tp, f"{_WPML}waypointTurnMode",        "toPointAndStopWithDiscontinuityCurvature")
            _tx(tp, f"{_WPML}waypointTurnDampingDist", "0")
        elif is_instrip_mid:
            # Fly through without stopping; drone interpolates altitude toward next wp.
            _tx(tp, f"{_WPML}waypointTurnMode",        "coordinateTurn")
            _tx(tp, f"{_WPML}waypointTurnDampingDist", "3")
        else:
            _tx(tp, f"{_WPML}waypointTurnMode",        "coordinateTurn")
            _tx(tp, f"{_WPML}waypointTurnDampingDist", "10")

        _tx(pm, f"{_WPML}useStraightLine", "1")

        # Shooting actions: stop before start so there's no overlap between sessions
        for _ in sessions_ending.get(i, []):
            _add_ag_stop_shooting(pm, ag_id=ag_idx, idx=i, img_fmt=img_fmt, pp=pp)
            ag_idx += 1
        for sess_end, sess_pm in sessions_starting.get(i, []):
            _add_ag_start_shooting(pm, ag_id=ag_idx, start=i, end=sess_end,
                                   photo_m=sess_pm, img_fmt=img_fmt, pp=pp)
            ag_idx += 1

        gh = etree.SubElement(pm, f"{_WPML}waypointGimbalHeadingParam")
        _tx(gh, f"{_WPML}waypointGimbalPitchAngle", "0")
        _tx(gh, f"{_WPML}waypointGimbalYawAngle",   "0")
        _tx(pm, f"{_WPML}isRisky",        "0")
        _tx(pm, f"{_WPML}waypointWorkType", "0")

    return etree.tostring(
        root,
        xml_declaration=True,
        encoding="UTF-8",
        pretty_print=True,
    ).decode("utf-8")


# ── Action helpers ────────────────────────────────────────────────────────────

def _add_gimbal_rotate(parent: etree._Element, *, action_id: int, pp: str) -> None:
    a = etree.SubElement(parent, f"{_WPML}action")
    _tx(a, f"{_WPML}actionId",            str(action_id))
    _tx(a, f"{_WPML}actionActuatorFunc",  "gimbalRotate")
    fp = etree.SubElement(a, f"{_WPML}actionActuatorFuncParam")
    _tx(fp, f"{_WPML}gimbalHeadingYawBase",     "aircraft")
    _tx(fp, f"{_WPML}gimbalRotateMode",         "absoluteAngle")
    _tx(fp, f"{_WPML}gimbalPitchRotateEnable",  "1")
    _tx(fp, f"{_WPML}gimbalPitchRotateAngle",   "-90")
    _tx(fp, f"{_WPML}gimbalRollRotateEnable",   "0")
    _tx(fp, f"{_WPML}gimbalRollRotateAngle",    "0")
    _tx(fp, f"{_WPML}gimbalYawRotateEnable",    "1")
    _tx(fp, f"{_WPML}gimbalYawRotateAngle",     "0")
    _tx(fp, f"{_WPML}gimbalRotateTimeEnable",   "0")
    _tx(fp, f"{_WPML}gimbalRotateTime",         "10")
    _tx(fp, f"{_WPML}payloadPositionIndex",     pp)


def _add_hover(parent: etree._Element, *, action_id: int, hover_s: float) -> None:
    a = etree.SubElement(parent, f"{_WPML}action")
    _tx(a, f"{_WPML}actionId",           str(action_id))
    _tx(a, f"{_WPML}actionActuatorFunc", "hover")
    fp = etree.SubElement(a, f"{_WPML}actionActuatorFuncParam")
    _tx(fp, f"{_WPML}hoverTime", f"{hover_s:g}")


def _add_set_focus_manual(parent: etree._Element, *, action_id: int, pp: str) -> None:
    a = etree.SubElement(parent, f"{_WPML}action")
    _tx(a, f"{_WPML}actionId",           str(action_id))
    _tx(a, f"{_WPML}actionActuatorFunc", "setFocusType")
    fp = etree.SubElement(a, f"{_WPML}actionActuatorFuncParam")
    _tx(fp, f"{_WPML}cameraFocusType",      "manual")
    _tx(fp, f"{_WPML}payloadPositionIndex", pp)


def _add_focus_infinite(parent: etree._Element, *, action_id: int, pp: str) -> None:
    a = etree.SubElement(parent, f"{_WPML}action")
    _tx(a, f"{_WPML}actionId",           str(action_id))
    _tx(a, f"{_WPML}actionActuatorFunc", "focus")
    fp = etree.SubElement(a, f"{_WPML}actionActuatorFuncParam")
    _tx(fp, f"{_WPML}focusX",               "0")
    _tx(fp, f"{_WPML}focusY",               "0")
    _tx(fp, f"{_WPML}focusRegionWidth",     "0")
    _tx(fp, f"{_WPML}focusRegionHeight",    "0")
    _tx(fp, f"{_WPML}isPointFocus",         "0")
    _tx(fp, f"{_WPML}isInfiniteFocus",      "1")
    _tx(fp, f"{_WPML}payloadPositionIndex", pp)
    _tx(fp, f"{_WPML}isCalibrationFocus",   "0")


def _add_ag_start_shooting(
    pm: etree._Element, *,
    ag_id: int, start: int, end: int,
    photo_m: float, img_fmt: str, pp: str,
) -> None:
    ag = etree.SubElement(pm, f"{_WPML}actionGroup")
    _tx(ag, f"{_WPML}actionGroupId",         str(ag_id))
    _tx(ag, f"{_WPML}actionGroupStartIndex", str(start))
    _tx(ag, f"{_WPML}actionGroupEndIndex",   str(end))
    _tx(ag, f"{_WPML}actionGroupMode",       "sequence")
    trig = etree.SubElement(ag, f"{_WPML}actionTrigger")
    _tx(trig, f"{_WPML}actionTriggerType",   "multipleDistance")
    _tx(trig, f"{_WPML}actionTriggerParam",  f"{photo_m:.15g}")
    _add_gimbal_rotate(ag, action_id=0, pp=pp)
    _add_start_shooting(ag, action_id=1, img_fmt=img_fmt, pp=pp)


def _add_ag_stop_shooting(
    pm: etree._Element, *,
    ag_id: int, idx: int, img_fmt: str, pp: str,
) -> None:
    ag = etree.SubElement(pm, f"{_WPML}actionGroup")
    _tx(ag, f"{_WPML}actionGroupId",         str(ag_id))
    _tx(ag, f"{_WPML}actionGroupStartIndex", str(idx))
    _tx(ag, f"{_WPML}actionGroupEndIndex",   str(idx))
    _tx(ag, f"{_WPML}actionGroupMode",       "sequence")
    trig = etree.SubElement(ag, f"{_WPML}actionTrigger")
    _tx(trig, f"{_WPML}actionTriggerType",   "reachPoint")
    _add_stop_shooting(ag, action_id=0, img_fmt=img_fmt, pp=pp)


def _add_start_shooting(
    parent: etree._Element, *, action_id: int, img_fmt: str, pp: str,
) -> None:
    a = etree.SubElement(parent, f"{_WPML}action")
    _tx(a, f"{_WPML}actionId",           str(action_id))
    _tx(a, f"{_WPML}actionActuatorFunc", "startContinuousShooting")
    fp = etree.SubElement(a, f"{_WPML}actionActuatorFuncParam")
    _tx(fp, f"{_WPML}payloadPositionIndex",       pp)
    _tx(fp, f"{_WPML}useGlobalPayloadLensIndex",  "0")
    _tx(fp, f"{_WPML}payloadLensIndex",           img_fmt)


def _add_stop_shooting(
    parent: etree._Element, *, action_id: int, img_fmt: str, pp: str,
) -> None:
    a = etree.SubElement(parent, f"{_WPML}action")
    _tx(a, f"{_WPML}actionId",           str(action_id))
    _tx(a, f"{_WPML}actionActuatorFunc", "stopContinuousShooting")
    fp = etree.SubElement(a, f"{_WPML}actionActuatorFuncParam")
    _tx(fp, f"{_WPML}payloadPositionIndex", pp)
    _tx(fp, f"{_WPML}payloadLensIndex",     img_fmt)
