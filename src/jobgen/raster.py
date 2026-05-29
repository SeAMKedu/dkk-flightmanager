"""Mosaic, reproject, and crop DEM tiles to site_dsm_wgs84.tif.

Pipeline:
  1. Mosaic all EPSG:3067 DEM tiles covering the job area (rasterio.merge).
  2. Reproject mosaic 3067 → 4326 (rasterio.warp.reproject), cropped to the
     survey polygon bounding box + margin_m on all sides.
  3. Write single-band float32 GeoTIFF, EPSG:4326, LZW-compressed.

No polygon masking is applied — the output is a clean rectangular raster.
Outside the survey polygon the terrain data is still present and used by
DJI for the RTH path and any takeoff/landing area outside the polygon.

Vertical datum note: heights remain N2000 as delivered by MML.  No vertical
datum transform is applied (see constraint 3 in the plan).
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

import numpy as np
import rasterio
import rasterio.merge
from rasterio.crs import CRS
from rasterio.transform import from_bounds as transform_from_bounds
from rasterio.warp import Resampling, reproject
from shapely.geometry.base import BaseGeometry

from jobgen.crs import require_4326

log = logging.getLogger(__name__)

_NODATA = -9999.0
_DST_CRS = CRS.from_epsg(4326)
_SRC_CRS = CRS.from_epsg(3067)

# Native resolution of korkeusmalli_2m
_SRC_RES_M = 2.0


def build_site_dsm(
    tile_paths: list[Path],
    survey_4326: BaseGeometry,
    output_path: Path,
    margin_m: int = 300,
) -> dict:
    """Mosaic DEM tiles, reproject to EPSG:4326, crop to survey bbox + margin.

    *tile_paths*  — cached EPSG:3067 DEM GeoTIFF paths from cache.get_tiles().
    *survey_4326* — final survey polygon in EPSG:4326 (used for bbox crop).
    *output_path* — destination path for site_dsm_wgs84.tif.
    *margin_m*    — extra metres of terrain data beyond the polygon bbox on all
                    sides; covers the RTH path and takeoff/landing area.

    Output is a clean rectangular raster — no polygon masking is applied.
    All terrain pixels within the bbox+margin are valid and available for
    DJI terrain-follow, including outside the survey polygon itself.

    Returns a stats dict for manifest inclusion:
      crs, shape, bounds_4326, elevation_min_m, elevation_max_m.
    """
    require_4326(survey_4326)

    if not tile_paths:
        raise ValueError("No DEM tile paths provided — cannot build site DSM.")

    missing = [p for p in tile_paths if not p.exists()]
    if missing:
        raise FileNotFoundError(f"Missing DEM tile(s): {missing}")

    log.info("Building site DSM from %d tile(s) → %s", len(tile_paths), output_path)

    # ------------------------------------------------------------------
    # Step 1: mosaic all tiles in EPSG:3067
    # ------------------------------------------------------------------
    datasets = [rasterio.open(p) for p in tile_paths]
    try:
        mosaic, mosaic_transform = rasterio.merge.merge(datasets)
    finally:
        for ds in datasets:
            ds.close()

    log.debug("Mosaic shape: %s", mosaic.shape)

    # ------------------------------------------------------------------
    # Step 2: compute output bounds in 4326 (polygon bbox + margin)
    # ------------------------------------------------------------------
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

    # Target resolution: match the source 2 m/px
    res_lon = _SRC_RES_M * deg_per_m_lon
    res_lat = _SRC_RES_M * deg_per_m_lat

    dst_width  = max(1, int(round((dst_right - dst_left)  / res_lon)))
    dst_height = max(1, int(round((dst_top   - dst_bottom) / res_lat)))
    dst_transform = transform_from_bounds(
        dst_left, dst_bottom, dst_right, dst_top, dst_width, dst_height
    )

    log.debug(
        "DSM output: %dx%d px, bounds (%.5f,%.5f)→(%.5f,%.5f), margin %d m",
        dst_width, dst_height, dst_left, dst_bottom, dst_right, dst_top, margin_m,
    )

    # ------------------------------------------------------------------
    # Step 3: reproject mosaic → 4326, cropped to target bounds
    # ------------------------------------------------------------------
    dst_data = np.full((1, dst_height, dst_width), _NODATA, dtype="float32")

    reproject(
        source=mosaic,
        destination=dst_data,
        src_transform=mosaic_transform,
        src_crs=_SRC_CRS,
        src_nodata=_NODATA,
        dst_transform=dst_transform,
        dst_crs=_DST_CRS,
        dst_nodata=_NODATA,
        resampling=Resampling.bilinear,
    )

    # ------------------------------------------------------------------
    # Step 4: write output
    # ------------------------------------------------------------------
    output_path.parent.mkdir(parents=True, exist_ok=True)

    profile = {
        "driver":    "GTiff",
        "dtype":     "float32",
        "width":     dst_width,
        "height":    dst_height,
        "count":     1,
        "crs":       _DST_CRS,
        "transform": dst_transform,
        "nodata":    _NODATA,
        "compress":  "lzw",
    }
    with rasterio.open(output_path, "w", **profile) as ds:
        ds.write(dst_data)

    log.info("site DSM written: %s", output_path)

    stats = _stats(output_path)
    log.info(
        "DSM stats: %dx%d px, elevation %.1f–%.1f m, margin %d m",
        stats["shape"][0], stats["shape"][1],
        stats["elevation_min_m"], stats["elevation_max_m"],
        margin_m,
    )
    return stats


def _stats(path: Path) -> dict:
    with rasterio.open(path) as ds:
        if ds.crs.to_epsg() != 4326:
            raise ValueError(f"Output DSM CRS is {ds.crs}, expected EPSG:4326")
        data = ds.read(1)
        valid = data[data != _NODATA]
        if len(valid) == 0:
            raise ValueError(
                "site_dsm_wgs84.tif has no valid pixels. "
                "Check that the DEM tiles cover the survey area."
            )
        bounds = ds.bounds
    return {
        "crs":              "EPSG:4326",
        "shape":            (ds.width, ds.height),
        "bounds_4326":      (bounds.left, bounds.bottom, bounds.right, bounds.top),
        "elevation_min_m":  float(np.min(valid)),
        "elevation_max_m":  float(np.max(valid)),
        "valid_pixel_count": int(len(valid)),
    }
