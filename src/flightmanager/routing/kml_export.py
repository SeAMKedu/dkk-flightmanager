"""Build a Google-Earth / Google-Maps KML from a set of saved jobs.

Server-side replacement for the old in-browser KML builder. Pure string
assembly from job_params dicts — no FastAPI dependency, fully testable.
Also exposes ``ordered_takeoffs()``, the flight-order takeoff sequence shared
with the (future) PDF mission packet.
"""

from __future__ import annotations

from flightmanager.storage.job_store import card_polygon

_DEFAULT_COLOR = "#3b82f6"

# An <IconStyle> with a colour but no <Icon><href> renders as *nothing* in
# Google My Maps (Google Earth quietly supplies a default pin; My Maps does
# not). Always pair the colour with a real pushpin URL so the markers show.
_TAKEOFF_ICON = "http://maps.google.com/mapfiles/kml/pushpin/wht-pushpin.png"
_LAUNCH_ICON = "http://maps.google.com/mapfiles/kml/paddle/red-circle.png"


def _escape_xml(s: str) -> str:
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def hex_to_kml_color(hex_color: str | None, alpha: str) -> str:
    """CSS ``#RRGGBB`` → KML ``AABBGGRR`` with the given two-char *alpha*."""
    h = (hex_color or _DEFAULT_COLOR).lstrip("#")
    if len(h) == 3:
        h = h[0] * 2 + h[1] * 2 + h[2] * 2
    return alpha + h[4:6] + h[2:4] + h[0:2]


def _polygon_rings(geom: dict | None) -> list[list]:
    """Return the outer ring(s) of a GeoJSON Polygon/MultiPolygon as coord lists."""
    if not geom:
        return []
    t = geom.get("type")
    coords = geom.get("coordinates") or []
    if t == "Polygon" and coords:
        return [coords[0]]
    if t == "MultiPolygon":
        return [poly[0] for poly in coords if poly]
    return []


def ordered_takeoffs(jobs: list[dict]) -> list[tuple[str, list]]:
    """Return ``[(job_name, [lon, lat]), …]`` for jobs that have a takeoff point.

    Order follows the supplied list (callers sort by flight order first).
    Shared with the PDF mission packet's inter-takeoff route.
    """
    out: list[tuple[str, list]] = []
    for p in jobs:
        tp = p.get("takeoff_point_4326")
        if tp:
            out.append((p.get("job_name") or "job", tp))
    return out


def _polygon_placemarks(p: dict) -> list[str]:
    """Survey-polygon placemark(s) for one job, coloured from its ``color``."""
    name = _escape_xml(p.get("job_name") or "job")
    line_color = hex_to_kml_color(p.get("color"), "ff")
    fill_color = hex_to_kml_color(p.get("color"), "55")
    out: list[str] = []
    for ring in _polygon_rings(card_polygon(p)):
        coords = " ".join(f"{c[0]},{c[1]},0" for c in ring)
        out.append(
            f"<Placemark><name>{name}</name>"
            f"<Style><LineStyle><color>{line_color}</color><width>2</width></LineStyle>"
            f"<PolyStyle><color>{fill_color}</color></PolyStyle></Style>"
            "<Polygon><outerBoundaryIs><LinearRing>"
            f"<coordinates>{coords}</coordinates>"
            "</LinearRing></outerBoundaryIs></Polygon></Placemark>"
        )
    return out


def _takeoff_placemark(p: dict) -> str | None:
    """Per-job takeoff marker (white pushpin tinted by job colour), or None."""
    tp = p.get("takeoff_point_4326")
    if not tp:
        return None
    name = _escape_xml(p.get("job_name") or "job")
    line_color = hex_to_kml_color(p.get("color"), "ff")
    return (
        f"<Placemark><name>{name}</name>"
        f"<Style><IconStyle><color>{line_color}</color>"
        f"<Icon><href>{_TAKEOFF_ICON}</href></Icon></IconStyle></Style>"
        f"<Point><coordinates>{tp[0]},{tp[1]},0</coordinates></Point></Placemark>"
    )


def _launch_placemark(lp: dict) -> str:
    """Launch-site marker (red paddle pin) for one launch point."""
    ls_name = _escape_xml(lp.get("name") or "launch")
    return (
        f"<Placemark><name>{ls_name}</name>"
        f"<Style><IconStyle><Icon><href>{_LAUNCH_ICON}</href></Icon></IconStyle></Style>"
        f"<Point><coordinates>{lp['lon']},{lp['lat']},0</coordinates></Point></Placemark>"
    )


def build_jobs_kml(
    jobs: list[dict],
    *,
    document_name: str = "DKK Jobs",
    launch_points: list[dict] | None = None,
) -> str:
    """Build a KML document of survey polygons and takeoff/launch markers.

    *jobs* is a list of job_params dicts (already in the desired flight order).
    The survey polygon is taken via :func:`card_polygon` so ID-derived jobs
    (which store only a ``survey_outline``) are included too.

    Without *launch_points* (detail zoom): one ``<Folder>`` per job — its survey
    polygon plus its own takeoff marker.

    With *launch_points* (``[{"name", "lon", "lat"}, …]``, sent at overview zoom
    where jobs collapse into launch sites): a single lightweight ``Launch sites``
    folder of just the launch markers — **no polygons** — so the file stays small
    enough for Google My Maps (survey outlines are vertex-heavy and blow past its
    size limit). The jobs are then used only for ordering context.
    """
    out: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2">',
        f"<Document><name>{_escape_xml(document_name)}</name>",
    ]

    if launch_points:
        out.append("<Folder><name>Launch sites</name>")
        for lp in launch_points:
            out.append(_launch_placemark(lp))
        out.append("</Folder>")
    else:
        for p in jobs:
            out.append(f"<Folder><name>{_escape_xml(p.get('job_name') or 'job')}</name>")
            out.extend(_polygon_placemarks(p))
            marker = _takeoff_placemark(p)
            if marker:
                out.append(marker)
            out.append("</Folder>")

    out.append("</Document></kml>")
    return "\n".join(out)
