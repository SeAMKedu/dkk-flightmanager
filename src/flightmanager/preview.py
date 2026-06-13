"""Generate a static Leaflet HTML map preview for a mapping job.

Produces a single self-contained HTML file (CDN assets only) that can be
opened in any browser for visual review before driving to the field.

Shows:
  - Survey polygon (post-keepout, green fill)
  - Building pins (red = keep-out, yellow = informational)
  - UAS zone hits if any (orange overlay)
  - DSM grayscale thumbnail (black = low, white = high) as base layer
  - Info panel: area, height, GSD, flight time, status flags

No extra Python dependencies — data is embedded as GeoJSON in the HTML.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

import json as _json

import jinja2

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(_TEMPLATES_DIR),
    autoescape=False,
)
_jinja_env.filters["tojson"] = lambda v: _json.dumps(v, ensure_ascii=False)

from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from flightmanager.buildings import Building
from flightmanager.config import HomeSafetyConfig
from flightmanager.zones import ZoneHit

log = logging.getLogger(__name__)

_DSM_MAX_PX = 512   # longest side of the thumbnail embedded in the HTML

# Viridis colormap — 11 control points (0.0 … 1.0) derived from matplotlib's viridis LUT.
# Stored as float32 so the interpolation below works without an explicit cast.
import numpy as _np
_VIRIDIS_STOPS = _np.array([
    [ 68,   1,  84],
    [ 72,  36, 117],
    [ 65,  68, 135],
    [ 53,  95, 141],
    [ 42, 120, 142],
    [ 33, 144, 141],
    [ 39, 168, 128],
    [ 82, 191, 104],
    [140, 209,  72],
    [195, 223,  35],
    [253, 231,  37],
], dtype=_np.float32)
del _np


def _colorize_viridis(norm):
    """Map normalized float32 [0,1] array to viridis RGB. Returns (H, W, 3) uint8."""
    import numpy as np
    n = len(_VIRIDIS_STOPS)           # 11 stops → 10 intervals
    scaled = np.clip(norm, 0.0, 1.0) * (n - 1)
    idx_lo = np.floor(scaled).astype(np.int32)
    idx_hi = np.minimum(idx_lo + 1, n - 1)
    frac = (scaled - idx_lo)[..., np.newaxis]
    colors = (1.0 - frac) * _VIRIDIS_STOPS[idx_lo] + frac * _VIRIDIS_STOPS[idx_hi]
    return colors.astype(np.uint8)


def build_preview_dsm_thumbnail(
    tile_paths: list[Path],
    survey_4326,
    margin_m: int = 150,
) -> tuple[str, tuple[float, float, float, float]] | tuple[None, None]:
    """Build a display-only DSM thumbnail from cached DEM tiles entirely in memory.

    Unlike :func:`~flightmanager.raster.build_site_dsm` this skips geoid correction
    (irrelevant for visual display), writes no files, and downsamples directly
    to thumbnail resolution so it is fast enough for the web UI live preview.

    Returns ``(base64-PNG, (west, south, east, north))`` in EPSG:4326, or
    ``(None, None)`` if tiles are unavailable or empty.
    """
    import math

    import numpy as np
    import rasterio
    import rasterio.merge
    from rasterio.crs import CRS
    from rasterio.io import MemoryFile
    from rasterio.transform import from_bounds as transform_from_bounds
    from rasterio.warp import Resampling, reproject

    if not tile_paths:
        return None, None

    _NODATA = -9999.0
    _SRC_CRS = CRS.from_epsg(3067)
    _DST_CRS = CRS.from_epsg(4326)

    datasets = [rasterio.open(p) for p in tile_paths]
    try:
        mosaic, mosaic_transform = rasterio.merge.merge(datasets)
    finally:
        for ds in datasets:
            ds.close()

    bounds = survey_4326.bounds  # (minx, miny, maxx, maxy) in 4326
    mid_lat = (bounds[1] + bounds[3]) / 2
    deg_per_m_lat = 1.0 / 111_132.0
    deg_per_m_lon = 1.0 / (111_132.0 * math.cos(math.radians(mid_lat)))
    margin_lat = margin_m * deg_per_m_lat
    margin_lon = margin_m * deg_per_m_lon

    dst_left   = bounds[0] - margin_lon
    dst_bottom = bounds[1] - margin_lat
    dst_right  = bounds[2] + margin_lon
    dst_top    = bounds[3] + margin_lat

    # Thumbnail dimensions preserving aspect ratio
    aspect = (dst_right - dst_left) / max(dst_top - dst_bottom, 1e-9)
    if aspect >= 1:
        tw, th = _DSM_MAX_PX, max(1, int(round(_DSM_MAX_PX / aspect)))
    else:
        tw, th = max(1, int(round(_DSM_MAX_PX * aspect))), _DSM_MAX_PX

    dst_transform = transform_from_bounds(dst_left, dst_bottom, dst_right, dst_top, tw, th)
    dst_data = np.full((1, th, tw), _NODATA, dtype="float32")
    reproject(
        source=mosaic, destination=dst_data,
        src_transform=mosaic_transform, src_crs=_SRC_CRS,
        src_nodata=_NODATA, dst_transform=dst_transform, dst_crs=_DST_CRS,
        dst_nodata=_NODATA, resampling=Resampling.average,
    )

    valid_mask = dst_data[0] != _NODATA
    valid = dst_data[0][valid_mask]
    if len(valid) == 0:
        return None, None

    lo, hi = float(valid.min()), float(valid.max())
    norm = np.zeros((th, tw), dtype=np.float32)
    if hi > lo:
        norm[valid_mask] = (dst_data[0][valid_mask] - lo) / (hi - lo)
    else:
        norm[valid_mask] = 0.5

    rgb = _colorize_viridis(norm)     # (H, W, 3) uint8
    rgba = np.zeros((4, th, tw), dtype=np.uint8)
    rgba[0] = rgb[:, :, 0]
    rgba[1] = rgb[:, :, 1]
    rgba[2] = rgb[:, :, 2]
    rgba[3] = np.where(valid_mask, 255, 0).astype(np.uint8)

    with MemoryFile() as mem:
        with mem.open(driver="PNG", dtype="uint8", count=4, width=tw, height=th) as dst:
            dst.write(rgba)
        png_bytes = mem.read()

    return base64.b64encode(png_bytes).decode(), (dst_left, dst_bottom, dst_right, dst_top)


def _dsm_thumbnail_b64(dsm_path: Path) -> tuple[str, tuple[float, float, float, float]] | tuple[None, None]:
    """Return (base64-PNG, (west, south, east, north)) for the DSM, or (None, None)."""
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling as RasterioResampling
    from rasterio.io import MemoryFile

    with rasterio.open(dsm_path) as ds:
        bounds = ds.bounds
        h, w = ds.height, ds.width
        scale = min(1.0, _DSM_MAX_PX / max(h, w))
        th = max(1, int(round(h * scale)))
        tw = max(1, int(round(w * scale)))
        data = ds.read(1, out_shape=(th, tw), resampling=RasterioResampling.average)
        nodata = ds.nodata if ds.nodata is not None else -9999.0

    valid_mask = data != nodata
    valid = data[valid_mask]
    if len(valid) == 0:
        return None, None

    lo, hi = float(valid.min()), float(valid.max())
    norm = np.zeros((th, tw), dtype=np.float32)
    if hi > lo:
        norm[valid_mask] = (data[valid_mask] - lo) / (hi - lo)
    else:
        norm[valid_mask] = 0.5

    rgb = _colorize_viridis(norm)     # (H, W, 3) uint8
    rgba = np.zeros((4, th, tw), dtype=np.uint8)
    rgba[0] = rgb[:, :, 0]
    rgba[1] = rgb[:, :, 1]
    rgba[2] = rgb[:, :, 2]
    rgba[3] = np.where(valid_mask, 255, 0).astype(np.uint8)

    with MemoryFile() as mem:
        with mem.open(driver="PNG", dtype="uint8", count=4, width=tw, height=th) as dst:
            dst.write(rgba)
        png_bytes = mem.read()

    return base64.b64encode(png_bytes).decode(), (bounds.left, bounds.bottom, bounds.right, bounds.top)


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
    zone_result=None,  # ZoneCheckResult — provides related_zones; avoids circular import
    dsm_path: Path | None = None,
    preview_radius_m: float = 300.0,
    keepout_ignored: bool = False,
) -> Path:
    """Write a Leaflet HTML map preview for the job.

    *buildings* should already be filtered to the relevant set (within
    home_include_buffer_m of the survey polygon) and reprojected to EPSG:4326
    before being passed here — the pipeline handles this before calling.

    *preview_radius_m* sets the radius of the informational yellow circle drawn
    around each building.  Defaults to 3× derived flight height (the "3:1
    horizontal rule"); the pipeline passes the resolved value.

    Returns *output_path*.
    """
    keepout_codes = set(home_safety.residential_kohdeluokka)
    if home_safety.operating_subcategory == "A3":
        keepout_codes |= set(home_safety.a3_additional_kohdeluokka)

    # Survey polygon — wrap as a GeoJSON Feature so Leaflet renders it correctly
    survey_geojson = json.dumps({
        "type": "Feature",
        "geometry": mapping(survey_4326),
        "properties": {},
    })

    # Building pins — show all buildings; red = keep-out, yellow = informational
    pins = []
    for b in buildings:
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

    # Zone hit polygons — direct hits + related inner zones; detect nesting for popup
    direct_hits = list(zone_hits or [])
    related_hits = list(zone_result.related_zones if zone_result else [])
    all_hits = [(h, False) for h in direct_hits] + [(h, True) for h in related_hits]
    zone_layers = []
    for i, (hit, context_only) in enumerate(all_hits):
        if hit.geom is None:
            continue
        contained_by = [
            {"id": other.identifier, "name": other.name}
            for j, (other, _) in enumerate(all_hits)
            if i != j and other.geom is not None and other.geom.contains(hit.geom)
        ]
        zone_layers.append({
            "geojson":      json.dumps(mapping(hit.geom)),
            "name":         hit.name,
            "restriction":  hit.restriction,
            "upper_limit":  hit.altitude.upper_limit,
            "upper_uom":    hit.altitude.upper_uom,
            "upper_ref":    hit.altitude.upper_ref,
            "lower_limit":  hit.altitude.lower_limit,
            "lower_uom":    hit.altitude.lower_uom,
            "lower_ref":    hit.altitude.lower_ref,
            "contained_by": contained_by,
            "context_only": context_only,
        })

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

    # DSM thumbnail
    dsm_b64: str | None = None
    dsm_bounds: tuple[float, float, float, float] | None = None
    if dsm_path and dsm_path.exists():
        try:
            dsm_b64, dsm_bounds = _dsm_thumbnail_b64(dsm_path)
        except Exception:
            log.warning("Could not render DSM thumbnail for preview", exc_info=True)

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
        preview_radius_m=preview_radius_m,
        dsm_b64=dsm_b64,
        dsm_bounds=dsm_bounds,
        keepout_ignored=keepout_ignored,
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
        42260: "Agricultural", 42261: "Agricultural", 42262: "Agricultural",
        42270: "Other building",
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
    preview_radius_m: float = 300.0,
    dsm_b64: str | None = None,
    dsm_bounds: tuple[float, float, float, float] | None = None,
    keepout_ignored: bool = False,
) -> str:
    return _jinja_env.get_template("preview.html").render(
        job_name=job_name,
        survey_geojson=survey_geojson,
        pins_json=pins_json,
        zone_json=zone_json,
        parcels_json=parcels_json,
        status_colour=status_colour,
        status_text=status_text,
        summary_rows=summary_rows,
        review_reasons=review_reasons,
        center_lat=center_lat,
        center_lon=center_lon,
        home_buffer_m=home_buffer_m,
        preview_radius_m=preview_radius_m,
        dsm_b64=dsm_b64,
        dsm_bounds=dsm_bounds,
        keepout_ignored=keepout_ignored,
    )
