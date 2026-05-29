"""MML korkeusmalli_2m WCS tile fetcher.

Confirmed endpoint (2026-05):
  URL:        https://avoin-karttakuva.maanmittauslaitos.fi/ortokuvat-ja-korkeusmallit/wcs/v2
  Auth:       api-key=<key>  (query parameter)
  CoverageID: korkeusmalli_2m
  CRS:        EPSG:3067 (ETRS-TM35FIN) — SUBSET axes are E and N
  Format:     image/tiff, float32
  Resolution: 2 m/px  →  1 km tile = 500×500 px ≈ 750 KB LZW-compressed
  NoData:     none (all pixels valid over Finnish land area)
  Vertical:   N2000 geoid heights (metres)

Size cap:  No hard cap encountered up to 5 km tiles (tested).  Default
  1 km tile size is well within limits.  If tile_size_m is ever increased
  substantially, sub-tiling may be needed; the fetcher raises on HTTP error.

Vertical datum note (constraint 3):
  MML heights are N2000 (geoid).  The drone references EGM96 via GNSS.
  The offset in Finland is sub-metre; without RTK, GNSS vertical accuracy
  is ±several metres anyway.  Terrain-follow only needs the *relative*
  terrain shape, which is preserved.  No vertical datum transform is applied.

This module provides a single-tile network fetcher for cache.get_tiles().
The pipeline assembles the job DEM by mosaicking tiles via raster.py.
"""

from __future__ import annotations

import logging
from pathlib import Path

import requests

from jobgen.cache import FetcherFn

log = logging.getLogger(__name__)

_WCS_URL = (
    "https://avoin-karttakuva.maanmittauslaitos.fi"
    "/ortokuvat-ja-korkeusmallit/wcs/v2"
)
_COVERAGE_ID = "korkeusmalli_2m"
_SOURCE_ATTRIBUTION = (
    "Contains data from the National Land Survey of Finland, "
    "Elevation model 2 m, retrieved {date}."
)


def tile_fetcher(
    api_key: str,
    session: requests.Session | None = None,
    timeout_s: int = 120,
) -> FetcherFn:
    """Return a FetcherFn compatible with cache.get_tiles() for the DEM dataset.

    The returned callable downloads one korkeusmalli_2m tile in EPSG:3067
    as a LZW-compressed float32 GeoTIFF, writes it to dest_path, and returns
    (source_url, dataset_version).
    """
    sess = session or requests.Session()

    def _fetch(tile_id: str, tile_bbox: tuple, dest: Path) -> tuple[str, str | None]:
        xmin, ymin, xmax, ymax = tile_bbox
        params = {
            "service": "WCS",
            "version": "2.0.1",
            "request": "GetCoverage",
            "CoverageID": _COVERAGE_ID,
            "SUBSET": [f"E({xmin},{xmax})", f"N({ymin},{ymax})"],
            "format": "image/tiff",
            "geotiff:compression": "LZW",
            "api-key": api_key,
        }

        log.info("Fetching DEM tile %s (%.0f×%.0f m)", tile_id,
                 xmax - xmin, ymax - ymin)

        resp = sess.get(_WCS_URL, params=params, timeout=timeout_s, stream=True)
        resp.raise_for_status()

        # Verify we got a TIFF, not an XML error body
        content_type = resp.headers.get("Content-Type", "")
        if "tiff" not in content_type and "octet-stream" not in content_type:
            body = resp.content[:500].decode("utf-8", errors="replace")
            raise RuntimeError(
                f"WCS returned unexpected Content-Type '{content_type}' "
                f"for tile {tile_id}. Body: {body}"
            )

        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)

        size_kb = dest.stat().st_size // 1024
        log.debug("DEM tile %s saved: %d KB", tile_id, size_kb)

        return (_WCS_URL, None)

    return _fetch


def validate_tile(path: Path) -> dict:
    """Open a cached DEM tile and return basic stats for manifest / sanity check.

    Returns dict with: crs, shape, bounds, elevation_min, elevation_max.
    Raises if the file is not a valid GeoTIFF or is in the wrong CRS.
    """
    import rasterio
    import numpy as np

    with rasterio.open(path) as ds:
        if ds.crs is None or ds.crs.to_epsg() != 3067:
            raise ValueError(
                f"DEM tile {path} has unexpected CRS {ds.crs} (expected EPSG:3067)"
            )
        data = ds.read(1, masked=True)
        valid = data.compressed() if hasattr(data, "compressed") else data.flatten()

    return {
        "crs": "EPSG:3067",
        "shape": (ds.width, ds.height),
        "bounds": tuple(ds.bounds),
        "elevation_min_m": float(np.min(valid)) if len(valid) else None,
        "elevation_max_m": float(np.max(valid)) if len(valid) else None,
    }
