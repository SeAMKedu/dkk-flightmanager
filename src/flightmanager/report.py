"""PDF flight report - per-job cards and a selected-jobs mission packet.

A printable, offline field document (the artifact that replaced the old static
HTML preview). Pages are drawn with fpdf2; the maps are a stitched bitmap
basemap (``tilemap``) with **vector** overlays (survey polygons, strips,
transits, takeoffs, launch-site circles) drawn on top so they stay crisp at any
print resolution.

Entry points:
- ``render_job_report(config, params, manifest)`` → one-page card (bytes).
- ``render_packet(config, jobs)`` → cover + overview + summary + launch-site
  pages + per-job cards (bytes).

Per-job geometry/strips/DSM come from a cache-first ``pipeline.analyse_survey``
call at generation time (online basemap assumed); the manifest supplies the
as-exported flight params and safety flags.
"""

from __future__ import annotations

import base64
import io
import os

from fpdf import FPDF
from PIL import Image
from shapely.geometry import shape

from flightmanager import tilemap
from flightmanager.launch_sites import cluster_jobs

A4_W, A4_H = 210.0, 297.0
MARGIN = 12.0
CONTENT_W = A4_W - 2 * MARGIN

# Palette (RGB) - mirrors the web UI.
C_INK = (30, 41, 59)
C_MUTED = (100, 116, 139)
C_SURVEY = (59, 130, 246)
C_STRIP = (245, 158, 11)
C_TRANSIT = (251, 191, 36)
C_KEEPOUT = (220, 38, 38)
C_TAKEOFF = (15, 23, 42)
C_LAUNCH = (245, 158, 11)
C_READY = (22, 163, 74)
C_REVIEW = (217, 119, 6)
C_LINE = (203, 213, 225)


# ── geometry helpers ──────────────────────────────────────────────────────────

def _rings(geom: dict | None):
    """Yield exterior rings (list of [lon,lat]) of a Polygon/MultiPolygon dict."""
    if not geom:
        return
    t, coords = geom.get("type"), geom.get("coordinates") or []
    if t == "Polygon" and coords:
        yield coords[0]
    elif t == "MultiPolygon":
        for poly in coords:
            if poly:
                yield poly[0]


def _lines(geom: dict | None):
    """Yield coordinate lists of a LineString/MultiLineString dict."""
    if not geom:
        return
    t, coords = geom.get("type"), geom.get("coordinates") or []
    if t == "LineString" and coords:
        yield coords
    elif t == "MultiLineString":
        for ln in coords:
            yield ln


def _bbox_of(geoms: list[dict], extra_points: list | None = None):
    """Union bbox (minlon, minlat, maxlon, maxlat) of geojson geoms + points."""
    xs: list[float] = []
    ys: list[float] = []
    for g in geoms:
        if not g:
            continue
        try:
            minx, miny, maxx, maxy = shape(g).bounds
            xs += [minx, maxx]
            ys += [miny, maxy]
        except Exception:
            continue
    for p in (extra_points or []):
        if p:
            xs.append(p[0])
            ys.append(p[1])
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


# ── map drawing ───────────────────────────────────────────────────────────────

def _draw_map(pdf: FPDF, x: float, y: float, w: float, h: float, *, bbox, overlays: dict,  # noqa: C901
              mml_key: str | None, basemap: str = "osm") -> str:
    """Draw a basemap + vector overlays into the box (x,y,w,h). Returns attribution.

    *basemap* is "osm" (crisp to z19, good for navigation context) or "mml" (MML
    orthophoto; caps at native z15 so it softens on small parcels).
    """
    bbox = tilemap.fit_bbox(tilemap.pad_bbox(bbox, 0.12), w / h)
    provider = tilemap.get_provider(basemap, mml_key=mml_key)
    bm = tilemap.fetch_basemap(bbox, target_px=int(w * 6), provider=provider, mml_key=mml_key)
    iw, ih = bm.size
    # Embed the basemap as JPEG so the PDF stays small (lossless PNG embedding of a
    # full-page aerial balloons the file to tens of MB).
    buf = io.BytesIO()
    bm.image.save(buf, format="JPEG", quality=82)
    buf.seek(0)
    pdf.image(buf, x=x, y=y, w=w, h=h)

    def T(lon, lat):
        px, py = bm.lonlat_to_px(lon, lat)
        return x + px / iw * w, y + py / ih * h

    def poly(geom, color, lw, fill=False, fillc=None, dash=None):
        pdf.set_draw_color(*color)
        pdf.set_line_width(lw)
        if dash:
            pdf.set_dash_pattern(dash=dash, gap=dash)
        for ring in _rings(geom):
            pts = [T(c[0], c[1]) for c in ring]
            if len(pts) >= 2:
                if fill and fillc:
                    pdf.set_fill_color(*fillc)
                pdf.polygon(pts, style="DF" if fill else "D")
        if dash:
            pdf.set_dash_pattern()

    def lines(fc, color, lw, dash=None):
        if not fc:
            return
        pdf.set_draw_color(*color)
        pdf.set_line_width(lw)
        if dash:
            pdf.set_dash_pattern(dash=dash, gap=dash)
        for feat in fc.get("features", []):
            for ln in _lines(feat.get("geometry")):
                pts = [T(c[0], c[1]) for c in ln]
                if len(pts) >= 2:
                    pdf.polyline(pts)
        if dash:
            pdf.set_dash_pattern()

    # Order: launch circles (bottom) -> keepout -> survey -> transits -> strips -> takeoffs.
    for lc in overlays.get("launch_circles", []):
        cx, cy = T(lc["center"][0], lc["center"][1])
        ex, ey = T(lc["edge"][0], lc["edge"][1])     # a point radius_m from centre
        r = ((ex - cx) ** 2 + (ey - cy) ** 2) ** 0.5
        pdf.set_draw_color(*C_LAUNCH)
        pdf.set_line_width(0.4)
        pdf.set_dash_pattern(dash=1.2, gap=1.2)
        pdf.circle(cx, cy, r, style="D")
        pdf.set_dash_pattern()
        # crosshair at the circle centre
        pdf.set_line_width(0.3)
        pdf.line(cx - 1.4, cy, cx + 1.4, cy)
        pdf.line(cx, cy - 1.4, cx, cy + 1.4)

    poly(overlays.get("keepout"), C_KEEPOUT, 0.3)
    for op in overlays.get("polygons", []):        # overview: many survey polygons
        poly(op, C_SURVEY, 0.4)
    poly(overlays.get("survey"), C_SURVEY, 0.6)
    lines(overlays.get("transits"), C_TRANSIT, 0.35, dash=1.0)
    lines(overlays.get("strips"), C_STRIP, 0.5)

    # Flight-order legs (overview): straight dashed lines between takeoffs.
    legs = overlays.get("legs")
    if legs and len(legs) >= 2:
        pdf.set_draw_color(*C_STRIP)
        pdf.set_line_width(0.4)
        pdf.set_dash_pattern(dash=1.5, gap=1.2)
        pdf.polyline([T(p[0], p[1]) for p in legs])
        pdf.set_dash_pattern()

    for tk in overlays.get("takeoffs", []):
        tx, ty = T(tk["pt"][0], tk["pt"][1])
        pdf.set_fill_color(*C_STRIP)
        pdf.set_draw_color(255, 255, 255)
        pdf.set_line_width(0.4)
        pdf.circle(tx, ty, 1.8, style="DF")
        if tk.get("label") is not None:
            pdf.set_font("Helvetica", "B", 7)
            pdf.set_text_color(0, 0, 0)
            pdf.set_xy(tx - 3, ty - 1.5)
            pdf.cell(6, 3, str(tk["label"]), align="C")

    pdf.set_draw_color(*C_LINE)
    pdf.set_line_width(0.3)
    pdf.set_dash_pattern()
    pdf.rect(x, y, w, h)
    return bm.attribution


# ── text helpers ──────────────────────────────────────────────────────────────

def _badge(pdf: FPDF, x: float, y: float, ready: bool, needs_review: bool):
    if ready and not needs_review:
        label, color = "FLIGHT READY", C_READY
    else:
        label, color = "NEEDS REVIEW", C_REVIEW
    pdf.set_fill_color(*color)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 9)
    w = pdf.get_string_width(label) + 6
    pdf.set_xy(x - w, y)
    pdf.cell(w, 6, label, align="C", fill=True, border=0)
    return label


def _kv_table(pdf: FPDF, x: float, y: float, w: float, rows: list[tuple[str, str]], title: str | None = None):
    pdf.set_xy(x, y)
    if title:
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(*C_INK)
        pdf.cell(w, 5.5, _t(title))
        y += 6.5
    kw = w * 0.46
    for k, v in rows:
        pdf.set_xy(x, y)
        pdf.set_font("Helvetica", "", 8.5)
        pdf.set_text_color(*C_MUTED)
        pdf.cell(kw, 5, _t(k))
        pdf.set_font("Helvetica", "B", 8.5)
        pdf.set_text_color(*C_INK)
        pdf.multi_cell(w - kw, 5, _t(v))
        y = pdf.get_y()
    return y


def _fmt(v, suffix="", nd=0):
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.{nd}f}{suffix}" if nd else f"{round(v)}{suffix}"
    return f"{v}{suffix}"


# Core PDF fonts are latin-1 only. Map the punctuation that turns up in job data
# (em/en dashes, arrows, etc.) to latin-1 equivalents; Finnish a/o/a-ring already
# fit. Anything still outside latin-1 becomes '?'. All rendered text goes through
# this.
_SUBS = {"—": "-", "–": "-", "→": "->", "≈": "~",
         "•": "-", "…": "...", " ": " "}


def _t(s) -> str:
    s = str(s)
    for a, b in _SUBS.items():
        s = s.replace(a, b)
    return s.encode("latin-1", "replace").decode("latin-1")


def _mf(manifest: dict, dotted: str, default=None):
    """Read a (possibly nested) manifest field, e.g. ``geometry.final_area_ha``."""
    cur = manifest
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur if cur is not None else default


# ── per-job card ──────────────────────────────────────────────────────────────

def _job_overlays(rd: dict) -> dict:
    return {
        "survey": rd.get("survey"),
        "keepout": rd.get("keepout_zone"),
        "strips": rd.get("strips_geojson"),
        "transits": rd.get("transits_geojson"),
        "takeoffs": ([{"pt": rd["takeoff_point_4326"], "label": None}]
                     if rd.get("takeoff_point_4326") else []),
    }


def build_job_card(pdf: FPDF, params: dict, manifest: dict, rd: dict, mml_key: str | None,
                   basemap: str = "mml"):
    """Render one job onto a fresh page."""
    pdf.add_page()
    stats = rd.get("stats", {})
    name = params.get("job_name") or manifest.get("job_name") or "job"

    pdf.set_xy(MARGIN, MARGIN)
    pdf.set_font("Helvetica", "B", 15)
    pdf.set_text_color(*C_INK)
    pdf.cell(CONTENT_W * 0.6, 8, _t(name))
    ready = bool(manifest.get("flight_ready", stats.get("flight_ready")))
    needs = bool(manifest.get("needs_review", stats.get("needs_review")))
    _badge(pdf, A4_W - MARGIN, MARGIN + 1, ready, needs)

    reasons = manifest.get("review_reasons") or stats.get("review_reasons") or []
    y = MARGIN + 10
    if reasons:
        pdf.set_xy(MARGIN, y)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*C_REVIEW)
        pdf.multi_cell(CONTENT_W, 4, _t("Review: " + "; ".join(reasons)))
        y = pdf.get_y() + 1

    # Map.
    bbox = _bbox_of([rd.get("survey"), rd.get("keepout_zone")],
                    [rd.get("takeoff_point_4326")])
    map_h = 118.0
    attribution = ""
    if bbox:
        attribution = _draw_map(pdf, MARGIN, y, CONTENT_W, map_h,
                                bbox=bbox, overlays=_job_overlays(rd), mml_key=mml_key,
                                basemap=basemap)
    y += map_h + 5

    # Flight params (left) + DSM/zones (right).
    col_w = (CONTENT_W - 8) / 2
    drone = _mf(manifest, "flight.drone_label") or stats.get("drone_label") or stats.get("drone")
    sub = stats.get("subcategory") or _mf(manifest, "operating_subcategory")
    rows = [
        ("Drone", str(drone or "-")),
        ("Height / GSD", f"{_fmt(stats.get('flight_height_m'),' m')}  ·  {_fmt(stats.get('target_gsd_cm'),' cm',1)}"),
        ("Subcategory", f"{sub or '-'}  ·  buffer {_fmt(stats.get('home_buffer_m'),' m')}"),
        ("Strips / photos", f"{_fmt(stats.get('route_strip_count'))}  /  {_fmt(stats.get('route_photo_count'))}"),
        ("Flight time", _fmt(stats.get("route_flight_time_min") or _mf(manifest, "battery.estimated_flight_time_min"), " min", 1)),
        ("Speed", _fmt(_mf(manifest, "flight.strip_speed_ms"), " m/s", 1)),
        ("Area (final)", f"{_fmt(stats.get('final_area_ha'),' ha',2)}  (lost {_fmt(stats.get('area_lost_pct'),'%',1)})"),
        ("Batteries", ">1 battery" if _mf(manifest, "battery.over_one_battery") else "1 battery"),
    ]
    _kv_table(pdf, MARGIN, y, col_w, rows, title="Flight parameters")

    rx = MARGIN + col_w + 8
    ry = y
    dsm = rd.get("dsm_b64")
    if dsm:
        try:
            img = Image.open(io.BytesIO(base64.b64decode(dsm)))
            ar = img.height / img.width
            iw = col_w
            ih = min(iw * ar, 46)
            pdf.set_font("Helvetica", "B", 9)
            pdf.set_text_color(*C_INK)
            pdf.set_xy(rx, ry)
            pdf.cell(col_w, 5.5, "Terrain (DSM)")
            pdf.image(img, x=rx, y=ry + 6.5, w=iw, h=ih)
            ry += 6.5 + ih + 3
            elev = f"{_fmt(stats.get('elevation_min_m') or _mf(manifest,'dsm.elevation_min_m'),' m')} - {_fmt(stats.get('elevation_max_m') or _mf(manifest,'dsm.elevation_max_m'),' m')}"
            pdf.set_font("Helvetica", "", 7.5)
            pdf.set_text_color(*C_MUTED)
            pdf.set_xy(rx, ry)
            pdf.cell(col_w, 4, _t("Elevation range: " + elev))
            ry += 6
        except Exception:
            pass

    zone_hits = rd.get("zone_hits") or []
    direct = [z for z in zone_hits if not z.get("context_only") and not z.get("buffer_only")]
    zrows = []
    for z in direct[:5]:
        floor = z.get("lower_limit_m_agl")
        zrows.append((z.get("name", "zone")[:34], f"floor {floor} m" if floor is not None else "-"))
    if zrows:
        _kv_table(pdf, rx, ry, col_w, zrows, title="UAS zones")
    elif not dsm:
        pdf.set_xy(rx, ry)
        pdf.set_font("Helvetica", "", 8.5)
        pdf.set_text_color(*C_MUTED)
        pdf.cell(col_w, 5, "No UAS zone intersections.")

    # Footer.
    pdf.set_y(A4_H - 14)
    pdf.set_font("Helvetica", "", 6.5)
    pdf.set_text_color(*C_MUTED)
    pdf.multi_cell(CONTENT_W, 3.2, _t(
        "Permanent Traficom zones only - verify NOTAMs on the day of flight.  "
        "flight_ready / needs_review are safety gates; a flagged job must be human-checked before flying.\n"
        + (("Basemap " + attribution + ".  ") if attribution else "")
        + "Generated by dkk-flightmanager."))


# ── orchestration (recompute render data from stored params) ──────────────────

def _render_data_for_job(base_config, params: dict) -> dict:
    """Recompute a job's render geometry/strips/DSM via the pipeline (cache-first).

    Reuses the router's param→config rebuild so the recomputed route matches the
    exported job. Network at generation time (online basemap assumed).
    """
    from flightmanager.pipeline import analyse_survey
    from flightmanager.routers.execution import _export_request_from_params, _prepare_config

    name = params.get("job_name") or "job"
    folder = params.get("folder")
    ereq = _export_request_from_params(name, folder, params)
    cfg = _prepare_config(ereq, base_config)
    custom = shape(ereq.custom_polygon) if ereq.custom_polygon else None
    return analyse_survey(
        cfg,
        parcel_ids=ereq.parcel_ids or None,
        property_ids=ereq.property_ids or None,
        custom_polygon_4326=custom,
        takeoff_point_4326=params.get("takeoff_point_4326"),
    )


def _new_pdf() -> FPDF:
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=False)
    pdf.set_margins(MARGIN, MARGIN, MARGIN)
    return pdf


def _mml_key() -> str | None:
    return os.environ.get("MML_API_KEY") or None


def render_job_report(base_config, params: dict, manifest: dict, *, basemap: str = "mml",
                      progress_cb=None) -> bytes:
    """Render a one-page card for a single job. Returns PDF bytes."""
    def _p(msg, pct):
        if progress_cb:
            progress_cb("report", msg, pct)
    _p("Analysing survey", 8)
    rd = _render_data_for_job(base_config, params)
    _p("Rendering map", 55)
    pdf = _new_pdf()
    build_job_card(pdf, params, manifest, rd, _mml_key(), basemap)
    _p("Finalizing", 98)
    return bytes(pdf.output())


# ── mission packet ────────────────────────────────────────────────────────────

def _clip_card(params: dict, manifest: dict) -> dict:
    """A lightweight card dict (geometry + flight order + flight time) for the
    overview map, summary table, and launch-site clustering."""
    from flightmanager.job_store import card_polygon
    return {
        "path": params.get("path") or params.get("job_name") or "job",
        "name": params.get("job_name") or manifest.get("job_name") or "job",
        "sort_order": params.get("sort_order"),
        "skipped": params.get("skipped", False),
        "takeoff_point_4326": params.get("takeoff_point_4326"),
        "_geometry": card_polygon(params),
        "flight_time_min": _mf(manifest, "battery.estimated_flight_time_min"),
        "height_m": _mf(manifest, "flight.derived_height_m"),
        "adv_max_height_m": params.get("adv_max_height_m"),
        "waypoint_mode": params.get("waypoint_mode", False),
        "flight_ready": manifest.get("flight_ready"),
        "needs_review": manifest.get("needs_review"),
        "final_area_ha": _mf(manifest, "geometry.final_area_ha"),
    }


def _edge_point(center: list, radius_m: float) -> list:
    """A lon/lat roughly *radius_m* east of *center* (for drawing a circle radius)."""
    import math
    lon, lat = center
    return [lon + radius_m / (111320.0 * max(math.cos(math.radians(lat)), 1e-6)), lat]


def _circle_extent(center: list, radius_m: float) -> list:
    """SW + NE corner points of a circle's bbox, so the full circle fits the map."""
    import math
    lon, lat = center
    dlat = radius_m / 111320.0
    dlon = radius_m / (111320.0 * max(math.cos(math.radians(lat)), 1e-6))
    return [[lon - dlon, lat - dlat], [lon + dlon, lat + dlat]]


def _sorted_routable(cards: list[dict]) -> list[dict]:
    r = [c for c in cards if c.get("takeoff_point_4326") and not c.get("skipped")]
    r.sort(key=lambda c: (c.get("sort_order") if c.get("sort_order") is not None else 1e9,
                          c.get("name") or ""))
    return r


def _cover(pdf: FPDF, cards: list[dict], folder: str | None):
    pdf.add_page()
    pdf.set_xy(MARGIN, 40)
    pdf.set_font("Helvetica", "B", 24)
    pdf.set_text_color(*C_INK)
    pdf.cell(CONTENT_W, 12, _t("Mission packet"))
    pdf.set_xy(MARGIN, 54)
    pdf.set_font("Helvetica", "", 13)
    pdf.set_text_color(*C_MUTED)
    pdf.cell(CONTENT_W, 8, _t(folder or "Selected jobs"))

    routable = _sorted_routable(cards)
    total_time = sum(c.get("flight_time_min") or 0 for c in cards)
    total_area = sum(c.get("final_area_ha") or 0 for c in cards)
    sites = cluster_jobs(cards)
    rows = [
        ("Jobs", str(len(cards))),
        ("Launch sites", str(len(sites))),
        ("Total area", _fmt(total_area, " ha", 1)),
        ("Total flight time", _fmt(total_time, " min", 0)),
        ("Routable (ordered)", str(len(routable))),
        ("Needs review", str(sum(1 for c in cards if c.get("needs_review")))),
    ]
    _kv_table(pdf, MARGIN, 76, CONTENT_W * 0.6, rows, title="Summary")
    pdf.set_y(A4_H - 16)
    pdf.set_font("Helvetica", "", 7)
    pdf.set_text_color(*C_MUTED)
    pdf.multi_cell(CONTENT_W, 3.2, _t(
        "Permanent Traficom zones only - verify NOTAMs on the day of flight. "
        "Generated by dkk-flightmanager."))


def _overview_map(pdf: FPDF, cards: list[dict], mml_key: str | None, basemap: str):
    sites = cluster_jobs(cards)
    geoms = [c.get("_geometry") for c in cards]
    pts = [c.get("takeoff_point_4326") for c in cards]
    for s in sites:
        pts += _circle_extent(s.circle_center_4326, s.radius_m)
    bbox = _bbox_of(geoms, pts)
    if not bbox:
        return
    pdf.set_xy(MARGIN, MARGIN)
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(*C_INK)
    pdf.cell(CONTENT_W, 8, _t("Overview - flight order & launch sites"))
    overlays = {
        "polygons": geoms,
        "launch_circles": [{"center": s.circle_center_4326,
                            "edge": _edge_point(s.circle_center_4326, s.radius_m)} for s in sites],
        "legs": [s.dot_4326 for s in sites],
        "takeoffs": [{"pt": s.dot_4326, "label": s.first_route_index} for s in sites],
    }
    _draw_map(pdf, MARGIN, MARGIN + 10, CONTENT_W, 150, bbox=bbox, overlays=overlays,
              mml_key=mml_key, basemap=basemap)

    # Summary table under the overview.
    y = MARGIN + 164
    pdf.set_xy(MARGIN, y)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(*C_INK)
    pdf.cell(CONTENT_W, 6, _t("Jobs in flight order"))
    y += 7
    headers = [("#", 10), ("Job", 86), ("Area ha", 26), ("Time min", 26), ("Status", 38)]
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*C_MUTED)
    cx = MARGIN
    for label, ww in headers:
        pdf.set_xy(cx, y)
        pdf.cell(ww, 5, _t(label))
        cx += ww
    y += 5.5
    for c in _sorted_routable(cards):
        cx = MARGIN
        ri = (c.get("sort_order") + 1) if c.get("sort_order") is not None else "-"
        status = "review" if c.get("needs_review") else ("ready" if c.get("flight_ready") else "-")
        vals = [(str(ri), 10), (c.get("name", ""), 86), (_fmt(c.get("final_area_ha"), "", 2), 26),
                (_fmt(c.get("flight_time_min"), "", 0), 26), (status, 38)]
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*C_INK)
        for val, ww in vals:
            pdf.set_xy(cx, y)
            pdf.cell(ww, 4.6, _t(val))
            cx += ww
        y += 4.8
        if y > A4_H - 16:
            break


def _launch_site_page(pdf: FPDF, site, cards: list[dict], mml_key: str | None, basemap: str):
    pdf.add_page()
    pdf.set_xy(MARGIN, MARGIN)
    pdf.set_font("Helvetica", "B", 15)
    pdf.set_text_color(*C_INK)
    rng = ""
    so = [m["route_index"] for m in site.members if m.get("route_index") is not None]
    if so:
        rng = f"#{min(so)}" + (f"-#{max(so)}" if max(so) != min(so) else "")
    pdf.cell(CONTENT_W, 8, _t(f"Launch site {rng}".strip()))

    # Announcement fields (the Flyk inputs).
    c = site.circle_center_4326
    import math as _m
    dur = site.flight_time_min
    rows = [
        ("Centre (lat, lon)", f"{c[1]:.5f}, {c[0]:.5f}"),
        ("Diameter", f"{_fmt(site.diameter_m, ' m', 0)}  (r {_fmt(site.radius_m, ' m', 0)})"),
        ("Max altitude", _fmt(site.max_altitude_m, " m", 0)),
        ("Duration", _fmt(_m.ceil((dur or 0) / 30) * 30, " min", 0) if dur else "-"),
        ("Jobs", str(site.member_count)),
    ]
    _kv_table(pdf, MARGIN, MARGIN + 11, CONTENT_W * 0.5, rows, title="Flight announcement")

    # Site map: member polygons + enclosing circle.
    member_paths = {m["path"] for m in site.members}
    member_cards = [cc for cc in cards if cc["path"] in member_paths]
    geoms = [cc.get("_geometry") for cc in member_cards]
    pts = [m.get("takeoff_4326") for m in site.members]
    pts += _circle_extent(site.circle_center_4326, site.radius_m)
    bbox = _bbox_of(geoms, pts)
    if bbox:
        overlays = {
            "polygons": geoms,
            "launch_circles": [{"center": site.circle_center_4326,
                                "edge": _edge_point(site.circle_center_4326, site.radius_m)}],
            "takeoffs": [{"pt": m["takeoff_4326"], "label": m.get("route_index")}
                         for m in site.members if m.get("takeoff_4326")],
        }
        _draw_map(pdf, MARGIN, MARGIN + 56, CONTENT_W, 150, bbox=bbox, overlays=overlays,
                  mml_key=mml_key, basemap=basemap)

    pdf.set_y(MARGIN + 210)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*C_INK)
    pdf.cell(CONTENT_W, 5.5, _t("Member jobs"))
    pdf.set_font("Helvetica", "", 8.5)
    pdf.set_text_color(*C_MUTED)
    pdf.set_xy(MARGIN, MARGIN + 216)
    pdf.multi_cell(CONTENT_W, 4.4, _t(", ".join(
        f"#{m['route_index']} {m['name']}" if m.get("route_index") else m["name"]
        for m in site.members)))


def render_packet(base_config, job_entries: list[dict], *, folder: str | None = None,
                  basemap: str = "mml", include_job_cards: bool = True, progress_cb=None) -> bytes:
    """Render the full mission packet for the given jobs.

    *job_entries*: list of ``{"params": dict, "manifest": dict}`` (params should
    carry ``path``/``job_name``/``folder``). Cover + overview + summary +
    per-launch-site announcement pages + (optionally) per-job cards.
    """
    def _p(msg, pct):
        if progress_cb:
            progress_cb("report", msg, int(pct))

    mml_key = _mml_key()
    cards = [_clip_card(e["params"], e.get("manifest") or {}) for e in job_entries]
    sites = cluster_jobs(cards)
    _p("Preparing", 3)

    pdf = _new_pdf()
    _cover(pdf, cards, folder)
    _p("Cover", 6)
    pdf.add_page()
    _overview_map(pdf, cards, mml_key, basemap)
    _p("Overview map", 14)

    for i, site in enumerate(sites):
        _launch_site_page(pdf, site, cards, mml_key, basemap)
        _p(f"Launch site {i + 1}/{len(sites)}", 14 + (i + 1) / max(len(sites), 1) * 12)

    if include_job_cards:
        n = len(job_entries)
        for i, e in enumerate(job_entries):
            _p(f"Job card {i + 1}/{n}", 26 + i / max(n, 1) * 72)
            try:
                rd = _render_data_for_job(base_config, e["params"])
                build_job_card(pdf, e["params"], e.get("manifest") or {}, rd, mml_key, basemap)
            except Exception:
                continue
    _p("Finalizing", 99)
    return bytes(pdf.output())
