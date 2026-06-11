"""Mosaic, reproject, and crop DEM tiles to site_dsm_wgs84.tif.

Pipeline:
  1. Mosaic all EPSG:3067 DEM tiles covering the job area (rasterio.merge).
  2. Reproject mosaic 3067 → 4326 (rasterio.warp.reproject), cropped to the
     survey polygon bounding box + margin_m on all sides.
  3. Apply geoid correction: N2000 orthometric → WGS-84 ellipsoidal heights
     using the FIN2023N2000 model (fi_nls_fin2023n2000.tif from cdn.proj.org).
     The grid is auto-downloaded into pyproj's data directory on first use.
     DJI Pilot 2 reads DSM heights as WGS-84 ellipsoidal; without this step
     the drone flies ~18 m lower than commanded (the N2000/WGS-84 offset in
     Finland).
  4. Write single-band float32 GeoTIFF, EPSG:4326, LZW-compressed.

No polygon masking is applied — the output is a clean rectangular raster.
Outside the survey polygon the terrain data is still present and used by
DJI for the RTH path and any takeoff/landing area outside the polygon.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

import numpy as np
import rasterio
import rasterio.merge
import rasterio.transform as rio_transform
from pyproj import Transformer
from pyproj.datadir import get_data_dir
from pyproj.sync import _download_resource_file, get_proj_endpoint
from rasterio.crs import CRS
from rasterio.transform import from_bounds as transform_from_bounds
from rasterio.warp import Resampling, reproject
from shapely.geometry.base import BaseGeometry

from flightmanager.crs import require_4326

log = logging.getLogger(__name__)

_NODATA = -9999.0
_DST_CRS = CRS.from_epsg(4326)
_SRC_CRS = CRS.from_epsg(3067)

# Native resolution of korkeusmalli_2m
_SRC_RES_M = 2.0

_GEOID_GRID = "fi_nls_fin2023n2000.tif"

# Inverse vgridshift: N2000 orthometric → WGS-84 ellipsoidal (adds undulation)
_GEOID_PIPELINE = (
    "+proj=pipeline"
    " +step +proj=unitconvert +xy_in=deg +xy_out=rad"
    f" +step +inv +proj=vgridshift +grids={_GEOID_GRID}"
    " +step +proj=unitconvert +xy_in=rad +xy_out=deg"
)


def _ensure_geoid_grid() -> None:
    """Download fi_nls_fin2023n2000.tif into pyproj's data dir if absent."""
    dest = Path(get_data_dir()) / _GEOID_GRID
    if dest.exists():
        return
    log.info("Geoid grid %s not found — downloading from cdn.proj.org...", _GEOID_GRID)
    try:
        _download_resource_file(
            file_url=f"{get_proj_endpoint()}/{_GEOID_GRID}",
            short_name=_GEOID_GRID,
            directory=str(get_data_dir()),
            verbose=False,
        )
        log.info("Geoid grid downloaded → %s", dest)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to download geoid grid {_GEOID_GRID} from cdn.proj.org: {exc}\n"
            "Check network access or manually place the file in "
            f"{get_data_dir()}"
        ) from exc


def _apply_geoid_correction(dst_data: np.ndarray, dst_transform) -> float:
    """Convert N2000 heights to WGS-84 ellipsoidal in-place; return mean undulation."""
    _, H, W = dst_data.shape
    cols, rows = np.meshgrid(np.arange(W), np.arange(H))
    lons, lats = rio_transform.xy(dst_transform, rows, cols)
    lons = np.asarray(lons, dtype="float64").ravel()
    lats = np.asarray(lats, dtype="float64").ravel()

    band = dst_data[0]
    valid_mask = band != _NODATA
    h_in = band.ravel().astype("float64")

    transformer = Transformer.from_pipeline(_GEOID_PIPELINE)
    _, _, h_out = transformer.transform(lons, lats, h_in)
    h_out = h_out.reshape(H, W).astype("float32")

    # Preserve nodata; compute mean undulation over valid pixels only
    h_out[~valid_mask] = _NODATA
    undulation = float(np.mean(h_out[valid_mask] - band[valid_mask]))
    dst_data[0] = h_out
    return undulation


def build_site_dsm(
    tile_paths: list[Path],
    survey_4326: BaseGeometry,
    output_path: Path,
    margin_m: int = 300,
) -> dict:
    """Mosaic DEM tiles, reproject to EPSG:4326, apply geoid correction, write DSM.

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

    _ensure_geoid_grid()

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
    # Step 3.5: N2000 → WGS-84 ellipsoidal geoid correction (FIN2023N2000)
    # ------------------------------------------------------------------
    undulation = _apply_geoid_correction(dst_data, dst_transform)
    log.info(
        "Geoid correction applied (FIN2023N2000): mean undulation %.3f m", undulation
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
        "DSM stats: %dx%d px, elevation %.1f–%.1f m (WGS-84 ellipsoidal), margin %d m",
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
