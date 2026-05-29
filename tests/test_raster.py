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
from jobgen.raster import _NODATA, _validate_and_stats, build_site_dsm

# Real 1km DEM tile fetched during Phase 4 development
_REAL_TILE = Path("/tmp/test_tile.tif")

# Survey polygon in WGS84 that falls inside the real tile's coverage.
# Tile E295000–296000, N6974000–6975000 in EPSG:3067 maps to
# lon 22.973–22.991, lat 62.838–62.848 in EPSG:4326 (confirmed empirically).
_SURVEY_INSIDE = box(22.975, 62.839, 22.989, 62.847)


@pytest.fixture
def real_tile(tmp_path) -> Path:
    if not _REAL_TILE.exists():
        pytest.skip("Real DEM tile not present — run live integration first")
    dest = tmp_path / "dem" / "E295000_N6974000.tif"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(_REAL_TILE, dest)
    return dest


def _make_synthetic_tile(tmp_path: Path, epsg: int = 3067) -> Path:
    """Write a tiny 10×10 synthetic GeoTIFF for unit tests."""
    p = tmp_path / "dem" / "synthetic.tif"
    p.parent.mkdir(parents=True, exist_ok=True)
    transform = from_bounds(295_000, 6_974_000, 296_000, 6_975_000, 10, 10)
    crs = CRS.from_epsg(epsg)
    data = np.linspace(60.0, 90.0, 100, dtype="float32").reshape(1, 10, 10)
    with rasterio.open(
        p, "w", driver="GTiff", height=10, width=10,
        count=1, dtype="float32", crs=crs, transform=transform, nodata=_NODATA,
    ) as ds:
        ds.write(data)
    return p


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_no_tiles_raises(self, tmp_path):
        with pytest.raises(ValueError, match="No DEM tile"):
            build_site_dsm([], _SURVEY_INSIDE, tmp_path / "out.tif")

    def test_missing_tile_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            build_site_dsm(
                [tmp_path / "nonexistent.tif"],
                _SURVEY_INSIDE,
                tmp_path / "out.tif",
            )

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
        build_site_dsm([real_tile], _SURVEY_INSIDE, out)
        with rasterio.open(out) as ds:
            assert ds.crs.to_epsg() == 4326

    def test_output_is_float32(self, tmp_path, real_tile):
        out = tmp_path / "site_dsm_wgs84.tif"
        build_site_dsm([real_tile], _SURVEY_INSIDE, out)
        with rasterio.open(out) as ds:
            assert ds.dtypes[0] == "float32"

    def test_output_single_band(self, tmp_path, real_tile):
        out = tmp_path / "site_dsm_wgs84.tif"
        build_site_dsm([real_tile], _SURVEY_INSIDE, out)
        with rasterio.open(out) as ds:
            assert ds.count == 1

    def test_output_has_valid_pixels(self, tmp_path, real_tile):
        out = tmp_path / "site_dsm_wgs84.tif"
        stats = build_site_dsm([real_tile], _SURVEY_INSIDE, out)
        assert stats["valid_pixel_count"] > 0

    def test_elevation_range_plausible(self, tmp_path, real_tile):
        out = tmp_path / "site_dsm_wgs84.tif"
        stats = build_site_dsm([real_tile], _SURVEY_INSIDE, out)
        # Finnish field terrain in South Ostrobothnia: ~50–150 m
        assert 0 < stats["elevation_min_m"] < 200
        assert stats["elevation_max_m"] >= stats["elevation_min_m"]

    def test_stats_returned(self, tmp_path, real_tile):
        out = tmp_path / "site_dsm_wgs84.tif"
        stats = build_site_dsm([real_tile], _SURVEY_INSIDE, out)
        assert stats["crs"] == "EPSG:4326"
        assert "shape" in stats
        assert "bounds_4326" in stats
        assert "elevation_min_m" in stats
        assert "elevation_max_m" in stats

    def test_output_bounds_within_survey(self, tmp_path, real_tile):
        out = tmp_path / "site_dsm_wgs84.tif"
        stats = build_site_dsm([real_tile], _SURVEY_INSIDE, out)
        xmin, ymin, xmax, ymax = stats["bounds_4326"]
        # Output extent should be close to (not much larger than) the survey polygon
        sx1, sy1, sx2, sy2 = _SURVEY_INSIDE.bounds
        assert xmin >= sx1 - 0.01
        assert ymin >= sy1 - 0.01
        assert xmax <= sx2 + 0.01
        assert ymax <= sy2 + 0.01

    def test_output_compressed(self, tmp_path, real_tile):
        out = tmp_path / "site_dsm_wgs84.tif"
        build_site_dsm([real_tile], _SURVEY_INSIDE, out)
        with rasterio.open(out) as ds:
            profile = ds.profile
        assert profile.get("compress", "").lower() in ("lzw", "deflate")


# ---------------------------------------------------------------------------
# Mosaic (synthetic — no real tile needed)
# ---------------------------------------------------------------------------


class TestMosaic:
    def test_two_adjacent_tiles_mosaicked(self, tmp_path):
        """Two adjacent synthetic tiles should produce a wider output."""
        # tile1: E295000–296000, tile2: E296000–297000
        t1 = tmp_path / "dem" / "t1.tif"
        t2 = tmp_path / "dem" / "t2.tif"
        t1.parent.mkdir(parents=True, exist_ok=True)
        for p, xmin, xmax in [(t1, 295_000, 296_000), (t2, 296_000, 297_000)]:
            tr = from_bounds(xmin, 6_974_000, xmax, 6_975_000, 10, 10)
            data = np.full((1, 10, 10), 75.0, dtype="float32")
            with rasterio.open(
                p, "w", driver="GTiff", height=10, width=10, count=1,
                dtype="float32", crs=CRS.from_epsg(3067),
                transform=tr, nodata=_NODATA,
            ) as ds:
                ds.write(data)

        # Survey in 4326 that spans both tiles (lon 22.97–23.01)
        survey = box(22.975, 62.840, 23.010, 62.847)
        out = tmp_path / "out.tif"
        stats = build_site_dsm([t1, t2], survey, out)
        assert stats["valid_pixel_count"] > 0

    def test_single_synthetic_tile(self, tmp_path):
        tile = _make_synthetic_tile(tmp_path)
        # Synthetic tile: E295000–296000 → lon 22.973–22.991, lat 62.838–62.848
        survey = box(22.975, 62.839, 22.989, 62.847)
        out = tmp_path / "out.tif"
        stats = build_site_dsm([tile], survey, out)
        assert stats["crs"] == "EPSG:4326"
        assert stats["elevation_min_m"] >= 60.0


# ---------------------------------------------------------------------------
# _validate_and_stats edge cases
# ---------------------------------------------------------------------------


class TestValidateAndStats:
    def test_all_nodata_raises(self, tmp_path):
        p = tmp_path / "nodata.tif"
        tr = from_bounds(21.0, 62.0, 22.0, 63.0, 10, 10)
        data = np.full((1, 10, 10), _NODATA, dtype="float32")
        with rasterio.open(
            p, "w", driver="GTiff", height=10, width=10, count=1,
            dtype="float32", crs=CRS.from_epsg(4326),
            transform=tr, nodata=_NODATA,
        ) as ds:
            ds.write(data)
        survey = box(21.1, 62.1, 21.9, 62.9)
        with pytest.raises(ValueError, match="no valid pixels"):
            _validate_and_stats(p, survey)

    def test_wrong_crs_raises(self, tmp_path):
        p = tmp_path / "wrong.tif"
        tr = from_bounds(295_000, 6_974_000, 296_000, 6_975_000, 10, 10)
        data = np.ones((1, 10, 10), dtype="float32") * 75.0
        with rasterio.open(
            p, "w", driver="GTiff", height=10, width=10, count=1,
            dtype="float32", crs=CRS.from_epsg(3067),
            transform=tr, nodata=_NODATA,
        ) as ds:
            ds.write(data)
        survey = box(21.85, 62.83, 21.95, 62.88)
        with pytest.raises(ValueError, match="EPSG:4326"):
            _validate_and_stats(p, survey)


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
    fetcher = tile_fetcher(api_key)
    records = get_tiles("dem", bbox, fetcher, cfg)
    tile_paths = [r.path for r in records]

    out = tmp_path / "site_dsm_wgs84.tif"
    stats = build_site_dsm(tile_paths, _SURVEY_INSIDE, out)

    assert out.exists()
    assert stats["crs"] == "EPSG:4326"
    assert stats["elevation_min_m"] > 0
    assert stats["valid_pixel_count"] > 0
