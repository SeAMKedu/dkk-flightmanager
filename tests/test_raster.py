"""Tests for raster.py — Phase 5."""

from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_bounds
from shapely.geometry import box

from jobgen.crs import CRSError
from jobgen.raster import _NODATA, _stats, build_site_dsm

# Real 1km DEM tile fetched during Phase 4 development
_REAL_TILE = Path("/tmp/test_tile.tif")

# Survey polygon in WGS84 that falls inside the real tile's coverage.
# Tile E295000–296000, N6974000–6975000 → lon 22.973–22.991, lat 62.838–62.848
_SURVEY = box(22.975, 62.839, 22.989, 62.847)


@pytest.fixture
def real_tile(tmp_path) -> Path:
    if not _REAL_TILE.exists():
        pytest.skip("Real DEM tile not present — run live integration first")
    dest = tmp_path / "dem" / "E295000_N6974000.tif"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(_REAL_TILE, dest)
    return dest


def _make_synthetic_tile(
    tmp_path: Path,
    xmin=295_000, ymin=6_974_000, xmax=296_000, ymax=6_975_000,
    elevation=75.0,
) -> Path:
    p = tmp_path / "dem" / f"E{xmin}_N{ymin}.tif"
    p.parent.mkdir(parents=True, exist_ok=True)
    tr = from_bounds(xmin, ymin, xmax, ymax, 10, 10)
    data = np.full((1, 10, 10), elevation, dtype="float32")
    with rasterio.open(
        p, "w", driver="GTiff", height=10, width=10,
        count=1, dtype="float32", crs=CRS.from_epsg(3067),
        transform=tr, nodata=_NODATA,
    ) as ds:
        ds.write(data)
    return p


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_no_tiles_raises(self, tmp_path):
        with pytest.raises(ValueError, match="No DEM tile"):
            build_site_dsm([], _SURVEY, tmp_path / "out.tif")

    def test_missing_tile_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            build_site_dsm([tmp_path / "nonexistent.tif"], _SURVEY, tmp_path / "out.tif")

    def test_non_4326_survey_raises(self, tmp_path, real_tile):
        survey_3067 = box(295_000, 6_974_000, 295_500, 6_974_500)
        with pytest.raises(CRSError):
            build_site_dsm([real_tile], survey_3067, tmp_path / "out.tif")


# ---------------------------------------------------------------------------
# Output properties (real tile)
# ---------------------------------------------------------------------------


class TestOutputProperties:
    def test_output_is_4326(self, tmp_path, real_tile):
        out = tmp_path / "site_dsm_wgs84.tif"
        build_site_dsm([real_tile], _SURVEY, out)
        with rasterio.open(out) as ds:
            assert ds.crs.to_epsg() == 4326

    def test_output_is_float32(self, tmp_path, real_tile):
        out = tmp_path / "site_dsm_wgs84.tif"
        build_site_dsm([real_tile], _SURVEY, out)
        with rasterio.open(out) as ds:
            assert ds.dtypes[0] == "float32"

    def test_output_single_band(self, tmp_path, real_tile):
        out = tmp_path / "site_dsm_wgs84.tif"
        build_site_dsm([real_tile], _SURVEY, out)
        with rasterio.open(out) as ds:
            assert ds.count == 1

    def test_output_has_valid_pixels(self, tmp_path, real_tile):
        out = tmp_path / "site_dsm_wgs84.tif"
        stats = build_site_dsm([real_tile], _SURVEY, out)
        assert stats["valid_pixel_count"] > 0

    def test_elevation_range_plausible(self, tmp_path, real_tile):
        out = tmp_path / "site_dsm_wgs84.tif"
        stats = build_site_dsm([real_tile], _SURVEY, out)
        assert 0 < stats["elevation_min_m"] < 200
        assert stats["elevation_max_m"] >= stats["elevation_min_m"]

    def test_stats_returned(self, tmp_path, real_tile):
        out = tmp_path / "site_dsm_wgs84.tif"
        stats = build_site_dsm([real_tile], _SURVEY, out)
        assert stats["crs"] == "EPSG:4326"
        assert "shape" in stats
        assert "bounds_4326" in stats

    def test_output_compressed(self, tmp_path, real_tile):
        out = tmp_path / "site_dsm_wgs84.tif"
        build_site_dsm([real_tile], _SURVEY, out)
        with rasterio.open(out) as ds:
            assert ds.profile.get("compress", "").lower() in ("lzw", "deflate")

    def test_margin_makes_output_larger_than_polygon(self, tmp_path, real_tile):
        """Output bbox should extend beyond the survey polygon by the margin."""
        out = tmp_path / "site_dsm_wgs84.tif"
        stats = build_site_dsm([real_tile], _SURVEY, out, margin_m=300)
        xmin, ymin, xmax, ymax = stats["bounds_4326"]
        sx1, sy1, sx2, sy2 = _SURVEY.bounds
        # All four sides should be outside the polygon bounds
        assert xmin < sx1
        assert ymin < sy1
        assert xmax > sx2
        assert ymax > sy2

    def test_zero_margin_crops_to_polygon_bbox(self, tmp_path, real_tile):
        """Zero margin: output bbox should be approximately the polygon bbox."""
        out = tmp_path / "site_dsm_wgs84.tif"
        stats = build_site_dsm([real_tile], _SURVEY, out, margin_m=0)
        xmin, ymin, xmax, ymax = stats["bounds_4326"]
        sx1, sy1, sx2, sy2 = _SURVEY.bounds
        # Within one pixel tolerance
        assert abs(xmin - sx1) < 0.001
        assert abs(ymin - sy1) < 0.001

    def test_no_polygon_mask_applied(self, tmp_path, real_tile):
        """Output is a rectangle — no polygon masking, just tile coverage limits."""
        out = tmp_path / "site_dsm_wgs84.tif"
        stats = build_site_dsm([real_tile], _SURVEY, out, margin_m=0)
        # With zero margin and a survey polygon that fits inside the tile,
        # the output should have valid pixels (no polygon masking zeros things out)
        assert stats["valid_pixel_count"] > 0
        # The output bounds match the polygon bbox, not a shape-clipped extent
        xmin, ymin, xmax, ymax = stats["bounds_4326"]
        sx1, sy1, sx2, sy2 = _SURVEY.bounds
        assert abs(xmin - sx1) < 0.002  # within ~150m
        assert abs(ymax - sy2) < 0.002


# ---------------------------------------------------------------------------
# Mosaic (synthetic tiles — no real tile needed)
# ---------------------------------------------------------------------------


class TestMosaic:
    def test_two_adjacent_tiles_mosaicked(self, tmp_path):
        t1 = _make_synthetic_tile(tmp_path, xmin=295_000, xmax=296_000)
        t2 = _make_synthetic_tile(tmp_path, xmin=296_000, xmax=297_000)
        # Survey spanning both tiles in 4326
        survey = box(22.975, 62.840, 23.010, 62.847)
        out = tmp_path / "out.tif"
        stats = build_site_dsm([t1, t2], survey, out, margin_m=0)
        assert stats["valid_pixel_count"] > 0

    def test_single_synthetic_tile(self, tmp_path):
        tile = _make_synthetic_tile(tmp_path)
        out = tmp_path / "out.tif"
        stats = build_site_dsm([tile], _SURVEY, out, margin_m=0)
        assert stats["crs"] == "EPSG:4326"
        # After FIN2023N2000 geoid correction, N2000 75.0 m → WGS-84 ~92-94 m
        # at this location in Finland (undulation ≈ 17.9 m).
        assert abs(stats["elevation_min_m"] - 75.0) > 10.0, "geoid correction not applied"
        assert 88.0 < stats["elevation_min_m"] < 98.0


# ---------------------------------------------------------------------------
# _stats edge cases
# ---------------------------------------------------------------------------


class TestStats:
    def test_all_nodata_raises(self, tmp_path):
        p = tmp_path / "nodata.tif"
        tr = from_bounds(21.0, 62.0, 22.0, 63.0, 10, 10)
        data = np.full((1, 10, 10), _NODATA, dtype="float32")
        with rasterio.open(
            p, "w", driver="GTiff", height=10, width=10, count=1,
            dtype="float32", crs=CRS.from_epsg(4326), transform=tr, nodata=_NODATA,
        ) as ds:
            ds.write(data)
        with pytest.raises(ValueError, match="no valid pixels"):
            _stats(p)

    def test_wrong_crs_raises(self, tmp_path):
        p = tmp_path / "wrong.tif"
        tr = from_bounds(295_000, 6_974_000, 296_000, 6_975_000, 10, 10)
        data = np.ones((1, 10, 10), dtype="float32") * 75.0
        with rasterio.open(
            p, "w", driver="GTiff", height=10, width=10, count=1,
            dtype="float32", crs=CRS.from_epsg(3067), transform=tr, nodata=_NODATA,
        ) as ds:
            ds.write(data)
        with pytest.raises(ValueError, match="EPSG:4326"):
            _stats(p)


# ---------------------------------------------------------------------------
# Live integration test
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skip(reason="Hits live MML WCS — run with -m integration and set MML_API_KEY")
def test_live_build_site_dsm(tmp_path):
    import os
    from jobgen.cache import get_tiles
    from jobgen.config import CacheConfig
    from jobgen.elevation import tile_fetcher

    api_key = os.environ["MML_API_KEY"]
    cfg = CacheConfig(cache_dir=str(tmp_path / "cache"))
    bbox = (295_000.0, 6_974_000.0, 297_000.0, 6_976_000.0)
    records = get_tiles("dem", bbox, tile_fetcher(api_key), cfg)
    out = tmp_path / "site_dsm_wgs84.tif"
    stats = build_site_dsm([r.path for r in records], _SURVEY, out, margin_m=300)
    assert out.exists()
    assert stats["crs"] == "EPSG:4326"
    assert stats["elevation_min_m"] > 0
