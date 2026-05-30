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
            "lat":      c.y,
            "lon":      c.x,
            "colour":   colour,
            "label":    f"{label} (mtk_id {b.mtk_id})",
            "keepout":  b.kohdeluokka in keepout_codes,
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

    hs = manifest.get("home_safety", {})
    sub = hs.get("operating_subcategory", "A3")
    buf = hs.get("home_buffer_m", 150)
    summary_rows = [
        ("Subcategory",  f"{sub}  ({buf:.0f} m buffer)"),
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
  .legend {{ margin-top: 12px; font-size: .82rem; border-top: 1px solid #e5e7eb; padding-top: 10px; }}
  .leg-row {{
    display: grid;
    grid-template-columns: 22px 24px 1fr;
    align-items: center;
    margin: 5px 0;
  }}
  .eye-btn {{
    background: none; border: none; cursor: pointer; padding: 0;
    width: 22px; display: flex; align-items: center; justify-content: center;
    color: #374151; transition: color .15s;
  }}
  .eye-btn .eye-slash {{ display: none; }}
  .eye-btn.off {{ color: #9ca3af; }}
  .eye-btn.off .eye-open {{ display: none; }}
  .eye-btn.off .eye-slash {{ display: block; }}
  .leg-icon {{ display: flex; justify-content: center; align-items: center; }}
  .dot {{ width: 12px; height: 12px; border-radius: 50%; }}
  .swatch {{ width: 20px; height: 11px; border-radius: 2px; }}
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
    <div class="leg-row">
      <button class="eye-btn" id="eye-parcel" title="Toggle parcel outline"><svg class="eye-open" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg><svg class="eye-slash" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg></button>
      <div class="leg-icon"><div class="swatch" style="background:none;border:1.5px dashed #6b7280;"></div></div>
      <span>Original parcel</span>
    </div>
    <div class="leg-row">
      <button class="eye-btn" id="eye-survey" title="Toggle survey polygon"><svg class="eye-open" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg><svg class="eye-slash" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg></button>
      <div class="leg-icon"><div class="swatch" style="background:#4ade80;opacity:.6;border:2px solid #16a34a;"></div></div>
      <span>Survey polygon</span>
    </div>
    <div class="leg-row">
      <button class="eye-btn" id="eye-yellow-c" title="Toggle 100 m circles"><svg class="eye-open" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg><svg class="eye-slash" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg></button>
      <div class="leg-icon"><div class="swatch" style="background:#fef08a;opacity:.7;border:1px dashed #ca8a04;"></div></div>
      <span>100 m radius</span>
    </div>
    <div class="leg-row">
      <button class="eye-btn" id="eye-red-c" title="Toggle keep-out circles"><svg class="eye-open" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg><svg class="eye-slash" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg></button>
      <div class="leg-icon"><div class="swatch" style="background:#fca5a5;opacity:.7;border:1px dashed #dc2626;"></div></div>
      <span>{home_buffer_m:.0f} m keep-out</span>
    </div>
    <div class="leg-row">
      <button class="eye-btn" id="eye-vertices" title="Toggle polygon vertices"><svg class="eye-open" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg><svg class="eye-slash" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg></button>
      <div class="leg-icon"><div class="dot" style="background:#93c5fd;border:1px solid #1d4ed8;"></div></div>
      <span>Polygon vertices</span>
    </div>
    <div class="leg-row">
      <div></div>
      <div class="leg-icon"><div class="dot" style="background:{_RED};"></div></div>
      <span>Keep-out building</span>
    </div>
    <div class="leg-row">
      <div></div>
      <div class="leg-icon"><div class="dot" style="background:{_YELLOW};"></div></div>
      <span>Nearby building</span>
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

// Pins pane sits above the default overlayPane (z 400) so dots always render on top
map.createPane('pinsPane');
map.getPane('pinsPane').style.zIndex = 450;

// Drawing order (bottom to top):
//   1. survey polygon
//   2. yellow 100 m circles (all buildings)
//   3. red keep-out circles (all buildings)
//   4. original parcel outline
//   5. survey vertex dots
//   6. pin markers (pinsPane, z 450)

var surveyGroup  = L.layerGroup().addTo(map);
var yellowGroup  = L.layerGroup().addTo(map);
var redGroup     = L.layerGroup().addTo(map);
var circleGroup  = L.layerGroup();  // virtual group for the toggle (controls both)
var parcelGroup  = L.layerGroup().addTo(map);
var vertexGroup  = L.layerGroup().addTo(map);

// 1. Survey polygon
var surveyLayer = L.geoJSON(surveyData, {{
  style: {{ color: '#16a34a', weight: 2, fillColor: '#4ade80', fillOpacity: 0.35 }}
}}).addTo(surveyGroup);

if (surveyLayer.getLayers().length > 0) {{
  map.fitBounds(surveyLayer.getBounds().pad(0.15));
}}

// 2. Yellow 100 m circles — keep-out buildings only
pins.forEach(function(p) {{
  if (!p.keepout) return;
  L.circle([p.lat, p.lon], {{
    radius: 100, color: '#ca8a04', weight: 1,
    fillColor: '#fef08a', fillOpacity: 0.25, dashArray: '4 4'
  }}).addTo(yellowGroup);
}});

// 3. Red keep-out circles — only for keep-out buildings (red pins)
pins.forEach(function(p) {{
  if (!p.keepout) return;
  L.circle([p.lat, p.lon], {{
    radius: {home_buffer_m}, color: '#dc2626', weight: 1,
    fillColor: '#fca5a5', fillOpacity: 0.20, dashArray: '4 4'
  }}).addTo(redGroup);
}});

// 4. Original parcel outlines
parcels.forEach(function(f) {{
  L.geoJSON(f, {{
    style: {{ color: '#374151', weight: 1.5, dashArray: '5 5', fill: false }}
  }}).addTo(parcelGroup);
}});

// 5. Survey polygon vertex dots
(function() {{
  var geom = surveyData.geometry;
  var rings = geom.type === 'Polygon' ? geom.coordinates
            : geom.type === 'MultiPolygon' ? geom.coordinates.reduce(function(a, poly) {{ return a.concat(poly); }}, [])
            : [];
  var seen = {{}};
  rings.forEach(function(ring) {{
    ring.forEach(function(coord) {{
      var key = coord[0].toFixed(7) + ',' + coord[1].toFixed(7);
      if (seen[key]) return;
      seen[key] = true;
      L.circleMarker([coord[1], coord[0]], {{
        radius: 3, color: '#1d4ed8', weight: 1,
        fillColor: '#93c5fd', fillOpacity: 0.9, interactive: false
      }}).addTo(vertexGroup);
    }});
  }});
}})();

// 6. Pin markers — always on top via pinsPane
pins.forEach(function(p) {{
  L.circleMarker([p.lat, p.lon], {{
    radius: 7, color: '#fff', weight: 1.5,
    fillColor: p.colour, fillOpacity: 0.9,
    pane: 'pinsPane'
  }}).bindPopup(p.label).addTo(map);
}});

// Zone overlays (above everything except pins)
zones.forEach(function(z) {{
  L.geoJSON(JSON.parse(z.geojson), {{
    style: {{ color: '#f97316', weight: 2, fillColor: '#fed7aa', fillOpacity: 0.4 }}
  }}).bindPopup('<b>' + z.name + '</b><br>' + z.type).addTo(map);
}});

// Eye-button toggles — click cycles visible↔hidden, 'off' class = hidden
function eyeTog(btnId, showFn, hideFn) {{
  document.getElementById(btnId).addEventListener('click', function() {{
    if (this.classList.toggle('off')) {{ hideFn(); }} else {{ showFn(); }}
  }});
}}
eyeTog('eye-parcel',
  function() {{ parcelGroup.addTo(map); }},
  function() {{ map.removeLayer(parcelGroup); }});
eyeTog('eye-survey',
  function() {{ surveyGroup.addTo(map); }},
  function() {{ map.removeLayer(surveyGroup); }});
eyeTog('eye-yellow-c',
  function() {{ yellowGroup.addTo(map); }},
  function() {{ map.removeLayer(yellowGroup); }});
eyeTog('eye-red-c',
  function() {{ redGroup.addTo(map); }},
  function() {{ map.removeLayer(redGroup); }});
eyeTog('eye-vertices',
  function() {{ vertexGroup.addTo(map); }},
  function() {{ map.removeLayer(vertexGroup); }});

</script>
</body>
</html>
"""
