"""Generate a static Leaflet HTML map preview for a mapping job.

Produces a single self-contained HTML file (CDN assets only) that can be
opened in any browser for visual review before driving to the field.

Shows:
  - Survey polygon (post-keepout, green fill)
  - Building pins (red = keep-out, yellow = informational)
  - UAS zone hits if any (orange overlay)
  - Info panel: area, height, GSD, flight time, status flags

No extra Python dependencies — data is embedded as GeoJSON in the HTML.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from jobgen.buildings import Building
from jobgen.config import HomeSafetyConfig
from jobgen.zones import ZoneHit

log = logging.getLogger(__name__)

# Colours matching the DJI KML pin colours (converted from AABBGGRR to CSS hex)
_RED    = "#E23C39"
_YELLOW = "#FFBB00"


def build_map_preview(
    survey_4326: BaseGeometry,
    buildings: list[Building],
    output_path: Path,
    *,
    job_name: str,
    home_safety: HomeSafetyConfig,
    manifest: dict,
    parcels_4326: list[BaseGeometry] | None = None,
    zone_hits: list[ZoneHit] | None = None,
) -> Path:
    """Write a Leaflet HTML map preview for the job.

    *buildings* should already be filtered to the relevant set (within
    home_buffer_m of the survey polygon) and reprojected to EPSG:4326
    before being passed here — the pipeline handles this before calling.

    Returns *output_path*.
    """
    keepout_codes = set(home_safety.residential_kohdeluokka)
    if home_safety.operating_subcategory == "A3":
        keepout_codes |= set(home_safety.a3_additional_kohdeluokka)
    shown_codes = keepout_codes | set(home_safety.a3_additional_kohdeluokka)

    # Survey polygon — wrap as a GeoJSON Feature so Leaflet renders it correctly
    survey_geojson = json.dumps({
        "type": "Feature",
        "geometry": mapping(survey_4326),
        "properties": {},
    })

    # Building pins
    pins = []
    for b in buildings:
        if b.kohdeluokka not in shown_codes:
            continue
        c = b.geometry.centroid
        colour = _RED if b.kohdeluokka in keepout_codes else _YELLOW
        label = _building_label(b.kohdeluokka)
        pins.append({
            "lat":    c.y,
            "lon":    c.x,
            "colour": colour,
            "label":  f"{label} (mtk_id {b.mtk_id})",
        })

    pins_json = json.dumps(pins)

    # Zone hit polygons
    zone_layers = []
    for hit in (zone_hits or []):
        geom = hit.properties.get("geometry")
        if geom:
            try:
                from shapely.geometry import shape
                g = shape(geom["geometry"] if "geometry" in geom else geom)
                zone_layers.append({
                    "geojson": json.dumps(mapping(g)),
                    "name":    hit.name,
                    "type":    hit.restriction,
                })
            except Exception:
                pass

    zone_json = json.dumps(zone_layers)

    # Summary panel content
    g = manifest.get("geometry", {})
    f = manifest.get("flight", {})
    b = manifest.get("battery", {})
    needs_review = manifest.get("needs_review", False)
    flight_ready = manifest.get("flight_ready", False)

    status_colour = "#16a34a" if flight_ready else "#dc2626"
    status_text   = "✓ FLIGHT READY" if flight_ready else "⚠ NEEDS REVIEW"
    if needs_review and flight_ready:
        status_colour = "#d97706"
        status_text   = "⚠ NEEDS REVIEW"

    summary_rows = [
        ("Area",        f"{g.get('original_area_ha', 0):.2f} ha → {g.get('final_area_ha', 0):.2f} ha"),
        ("Keep-out",    f"{g.get('area_lost_pct', 0):.1f}% removed"),
        ("Height",      f"{f.get('derived_height_m', 0):.0f} m AGL"),
        ("GSD",         f"{f.get('target_gsd_cm', 0):.1f} cm/px"),
    ]
    if isinstance(b, dict) and "estimated_flight_time_min" in b:
        summary_rows.append(("Flight time", f"~{b['estimated_flight_time_min']:.0f} min"))
        summary_rows.append(("Photos",      f"~{b.get('estimated_photo_count', 0)}"))

    reasons = manifest.get("review_reasons", [])

    # Original parcel outlines — simplified for display (tolerance ~5m at Finnish lat)
    parcels_geojson = json.dumps([
        {"type": "Feature", "geometry": mapping(p.simplify(0.00005)), "properties": {}}
        for p in (parcels_4326 or [])
    ])

    bounds = survey_4326.bounds
    center_lat = (bounds[1] + bounds[3]) / 2
    center_lon = (bounds[0] + bounds[2]) / 2

    html = _render(
        job_name=job_name,
        survey_geojson=survey_geojson,
        pins_json=pins_json,
        zone_json=zone_json,
        parcels_json=parcels_geojson,
        status_colour=status_colour,
        status_text=status_text,
        summary_rows=summary_rows,
        review_reasons=reasons,
        center_lat=center_lat,
        center_lon=center_lon,
        home_buffer_m=home_safety.home_buffer_m,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    log.info("Map preview written: %s", output_path)
    return output_path


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------


def _building_label(kohdeluokka: int) -> str:
    mapping_kl = {
        42210: "Residence", 42211: "Residence", 42212: "Residence",
        42220: "Commercial", 42221: "Commercial", 42222: "Commercial",
        42230: "Holiday cottage", 42231: "Holiday cottage", 42232: "Holiday cottage",
        42240: "Industrial", 42241: "Industrial", 42242: "Industrial",
    }
    return mapping_kl.get(kohdeluokka, f"Building {kohdeluokka}")


def _render(
    job_name: str,
    survey_geojson: str,
    pins_json: str,
    zone_json: str,
    status_colour: str,
    status_text: str,
    summary_rows: list[tuple[str, str]],
    review_reasons: list[str],
    parcels_json: str = "[]",
    center_lat: float = 62.0,
    center_lon: float = 25.0,
    home_buffer_m: float = 150.0,
) -> str:
    rows_html = "".join(
        f"<tr><td>{k}</td><td><b>{v}</b></td></tr>"
        for k, v in summary_rows
    )
    reasons_html = ""
    if review_reasons:
        items = "".join(f"<li>{r}</li>" for r in review_reasons)
        reasons_html = f'<div class="reasons"><b>Review reasons:</b><ul>{items}</ul></div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>{job_name} — jobgen preview</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9/dist/leaflet.js"></script>
<style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  html, body {{ margin: 0; height: 100%; font-family: sans-serif; }}
  body {{ display: flex; }}
  #map {{ flex: 1; min-width: 0; }}
  #panel {{
    width: 320px; flex-shrink: 0;
    overflow-y: auto; overflow-x: hidden;
    padding: 16px; box-shadow: -2px 0 8px rgba(0,0,0,.15);
    background: #fff;
  }}
  h2 {{ margin: 0 0 4px; font-size: 1rem; color: #374151; word-break: break-all; }}
  .status {{
    display: inline-block; padding: 4px 10px; border-radius: 4px;
    color: #fff; font-weight: bold; margin-bottom: 12px;
    background: {status_colour};
  }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 12px; }}
  td {{ padding: 3px 0; font-size: .85rem; color: #374151; }}
  td:first-child {{ color: #6b7280; width: 50%; }}
  .legend {{ margin-top: 12px; font-size: .82rem; }}
  .legend-item {{ display: flex; align-items: center; gap: 8px; margin: 4px 0; }}
  .dot {{ width: 12px; height: 12px; border-radius: 50%; flex-shrink: 0; }}
  .swatch {{ width: 20px; height: 12px; flex-shrink: 0; border-radius: 2px; }}
  .reasons {{ background: #fef3c7; border: 1px solid #f59e0b; border-radius: 4px;
              padding: 8px; font-size: .8rem; margin-top: 10px; }}
  .reasons ul {{ margin: 4px 0 0; padding-left: 16px; }}
  .reasons li {{ margin: 2px 0; }}
</style>
</head>
<body>
<div id="map"></div>
<div id="panel">
  <h2>{job_name}</h2>
  <div class="status">{status_text}</div>
  <table>{rows_html}</table>
  <div class="legend">
    <div class="legend-item">
      <div class="swatch" style="background:none;border:1.5px dashed #6b7280;"></div>
      Original parcel
    </div>
    <div class="legend-item">
      <div class="swatch" style="background:#4ade80;opacity:.5;border:2px solid #16a34a;"></div>
      Survey polygon
    </div>
    <div class="legend-item">
      <div class="dot" style="background:{_RED};"></div>
      Keep-out building (red pin)
    </div>
    <div class="legend-item">
      <div class="dot" style="background:{_YELLOW};"></div>
      Nearby building (yellow pin)
    </div>
    <div class="legend-item">
      <div class="swatch" style="background:#fef08a;opacity:.6;border:1px dashed #ca8a04;"></div>
      100 m radius
    </div>
    <div class="legend-item">
      <div class="swatch" style="background:#fca5a5;opacity:.6;border:1px dashed #dc2626;"></div>
      {home_buffer_m:.0f} m keep-out radius
    </div>
  </div>
  {reasons_html}
</div>
<script>
var surveyData = {survey_geojson};
var parcels = {parcels_json};
var pins = {pins_json};
var zones = {zone_json};

var map = L.map('map').setView([{center_lat}, {center_lon}], 13);
L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
  attribution: '© <a href="https://openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  maxZoom: 19
}}).addTo(map);

map.invalidateSize();

// Original parcel outlines — dashed grey, drawn first so survey sits on top
parcels.forEach(function(f) {{
  L.geoJSON(f, {{
    style: {{ color: '#6b7280', weight: 1.5, dashArray: '5 5', fill: false }}
  }}).addTo(map);
}});

// Survey polygon
var surveyLayer = L.geoJSON(surveyData, {{
  style: {{ color: '#16a34a', weight: 2, fillColor: '#4ade80', fillOpacity: 0.35 }}
}}).addTo(map);

// Fit to polygon bounds
if (surveyLayer.getLayers().length > 0) {{
  map.fitBounds(surveyLayer.getBounds().pad(0.15));
}}

// Distance rings + pins (rings drawn first so they sit under the pin dots)
pins.forEach(function(p) {{
  // 100 m ring — light yellow, informational
  L.circle([p.lat, p.lon], {{
    radius: 100, color: '#ca8a04', weight: 1,
    fillColor: '#fef08a', fillOpacity: 0.25, dashArray: '4 4'
  }}).addTo(map);
  // keep-out ring (home_buffer_m) — light red, marks the no-fly boundary
  L.circle([p.lat, p.lon], {{
    radius: {home_buffer_m}, color: '#dc2626', weight: 1,
    fillColor: '#fca5a5', fillOpacity: 0.20, dashArray: '4 4'
  }}).addTo(map);
  // Pin dot on top
  L.circleMarker([p.lat, p.lon], {{
    radius: 7, color: '#fff', weight: 1.5,
    fillColor: p.colour, fillOpacity: 0.9
  }}).bindPopup(p.label).addTo(map);
}});

// Zone overlays
zones.forEach(function(z) {{
  L.geoJSON(JSON.parse(z.geojson), {{
    style: {{ color: '#f97316', weight: 2, fillColor: '#fed7aa', fillOpacity: 0.4 }}
  }}).bindPopup('<b>' + z.name + '</b><br>' + z.type).addTo(map);
}});
</script>
</body>
</html>
"""
