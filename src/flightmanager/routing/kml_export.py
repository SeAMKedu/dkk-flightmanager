"""Build a Google-Earth / Google-Maps KML from a set of saved jobs.

Server-side replacement for the old in-browser KML builder. Pure string
assembly from job_params dicts — no FastAPI dependency, fully testable.
Also exposes ``ordered_takeoffs()``, the flight-order takeoff sequence shared
with the (future) PDF mission packet.
"""

from __future__ import annotations

from flightmanager.storage.job_store import card_polygon

_DEFAULT_COLOR = "#3b82f6"


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


def build_jobs_kml(jobs: list[dict], *, document_name: str = "DKK Jobs") -> str:
    """Build a KML document: one ``<Folder>`` per job with its survey polygon and
    takeoff marker, coloured from the job's ``color``.

    *jobs* is a list of job_params dicts (already in the desired folder order).
    The survey polygon is taken via :func:`card_polygon` so ID-derived jobs
    (which store only a ``survey_outline``) are included too.
    """
    out: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2">',
        f"<Document><name>{_escape_xml(document_name)}</name>",
    ]
    for p in jobs:
        name = _escape_xml(p.get("job_name") or "job")
        line_color = hex_to_kml_color(p.get("color"), "ff")
        fill_color = hex_to_kml_color(p.get("color"), "55")
        out.append(f"<Folder><name>{name}</name>")

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

        tp = p.get("takeoff_point_4326")
        if tp:
            out.append(
                f"<Placemark><name>{name}</name>"
                f"<Style><IconStyle><color>{line_color}</color></IconStyle></Style>"
                f"<Point><coordinates>{tp[0]},{tp[1]},0</coordinates></Point></Placemark>"
            )

        out.append("</Folder>")

    out.append("</Document></kml>")
    return "\n".join(out)
