"""Mosaic, reproject, and clip DEM tiles to site_dsm_wgs84.tif.

Pipeline:
  1. Mosaic all EPSG:3067 DEM tiles covering the job area (rasterio.merge).
  2. Reproject mosaic 3067 → 4326, cropped to the survey polygon bounding
     box (rasterio.warp.reproject + calculate_default_transform).
  3. Apply polygon mask so pixels outside the survey area are set to nodata
     (rasterio.mask.mask).
  4. Write single-band float32 GeoTIFF, EPSG:4326, LZW-compressed.

Output: site_dsm_wgs84.tif — the terrain-follow DSM to be loaded on the RC.

Vertical datum note: heights remain N2000 as delivered by MML.  No vertical
datum transform is applied (see constraint 3 in the plan).
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import numpy as np
import rasterio
import rasterio.mask
import rasterio.merge
from rasterio.crs import CRS
from rasterio.warp import Resampling, calculate_default_transform, reproject
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry

from jobgen.crs import require_4326

log = logging.getLogger(__name__)

_NODATA = -9999.0
_DST_CRS = CRS.from_epsg(4326)
_SRC_CRS = CRS.from_epsg(3067)


def build_site_dsm(
    tile_paths: list[Path],
    survey_4326: BaseGeometry,
    output_path: Path,
) -> dict:
    """Mosaic DEM tiles, reproject to EPSG:4326, clip to survey polygon.

    *tile_paths*   — list of cached EPSG:3067 DEM GeoTIFF paths from cache.get_tiles().
    *survey_4326*  — final survey polygon in EPSG:4326 (used for clipping).
    *output_path*  — destination path for site_dsm_wgs84.tif.

    Returns a stats dict for manifest inclusion:
      crs, shape, bounds_4326, elevation_min_m, elevation_max_m.

    Raises if any tile is missing, or if the output has no valid pixels
    inside the survey polygon.
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

    src_profile = rasterio.open(tile_paths[0]).profile
    src_profile.update(
        height=mosaic.shape[1],
        width=mosaic.shape[2],
        transform=mosaic_transform,
        crs=_SRC_CRS,
        driver="GTiff",
        dtype="float32",
        count=1,
        nodata=_NODATA,
    )

    log.debug("Mosaic shape: %s, transform: %s", mosaic.shape, mosaic_transform)

    # ------------------------------------------------------------------
    # Step 2: reproject 3067 → 4326, cropped to survey polygon bounds
    # ------------------------------------------------------------------
    bounds = survey_4326.bounds  # (minx, miny, maxx, maxy) in 4326

    # Calculate output transform covering the survey polygon extent
    dst_transform, dst_width, dst_height = calculate_default_transform(
        _SRC_CRS, _DST_CRS,
        src_profile["width"], src_profile["height"],
        left=mosaic_transform.c,
        bottom=mosaic_transform.f + mosaic_transform.e * src_profile["height"],
        right=mosaic_transform.c + mosaic_transform.a * src_profile["width"],
        top=mosaic_transform.f,
        dst_width=None, dst_height=None,
    )

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

    dst_profile = {
        "driver": "GTiff",
        "dtype": "float32",
        "width": dst_width,
        "height": dst_height,
        "count": 1,
        "crs": _DST_CRS,
        "transform": dst_transform,
        "nodata": _NODATA,
        "compress": "lzw",
    }

    # ------------------------------------------------------------------
    # Step 3: mask to survey polygon and write output
    # ------------------------------------------------------------------
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(suffix=".tif", delete=False) as tmp_f:
        tmp_path = Path(tmp_f.name)

    try:
        with rasterio.open(tmp_path, "w", **dst_profile) as tmp_ds:
            tmp_ds.write(dst_data)

        with rasterio.open(tmp_path) as tmp_ds:
            clipped, clip_transform = rasterio.mask.mask(
                tmp_ds,
                [mapping(survey_4326)],
                crop=True,
                nodata=_NODATA,
                filled=True,
            )
            clip_profile = tmp_ds.profile.copy()
            clip_profile.update(
                height=clipped.shape[1],
                width=clipped.shape[2],
                transform=clip_transform,
            )
    finally:
        tmp_path.unlink(missing_ok=True)

    with rasterio.open(output_path, "w", **clip_profile) as out_ds:
        out_ds.write(clipped)

    log.info("site DSM written: %s", output_path)

    # ------------------------------------------------------------------
    # Validate and collect stats
    # ------------------------------------------------------------------
    stats = _validate_and_stats(output_path, survey_4326)
    log.info(
        "DSM stats: shape=%s elevation=%.1f–%.1f m",
        stats["shape"], stats["elevation_min_m"], stats["elevation_max_m"],
    )
    return stats


def _validate_and_stats(path: Path, survey_4326: BaseGeometry) -> dict:
    """Open the output DSM, validate it, and return stats for the manifest."""
    with rasterio.open(path) as ds:
        if ds.crs.to_epsg() != 4326:
            raise ValueError(f"Output DSM CRS is {ds.crs}, expected EPSG:4326")

        data = ds.read(1)
        valid_mask = data != _NODATA
        valid_pixels = data[valid_mask]

        if len(valid_pixels) == 0:
            raise ValueError(
                "site_dsm_wgs84.tif has no valid pixels inside the survey polygon. "
                "Check that the DEM tiles cover the survey area."
            )

        bounds = ds.bounds
        shape = (ds.width, ds.height)

    return {
        "crs": "EPSG:4326",
        "shape": shape,
        "bounds_4326": (bounds.left, bounds.bottom, bounds.right, bounds.top),
        "elevation_min_m": float(np.min(valid_pixels)),
        "elevation_max_m": float(np.max(valid_pixels)),
        "valid_pixel_count": int(len(valid_pixels)),
    }
