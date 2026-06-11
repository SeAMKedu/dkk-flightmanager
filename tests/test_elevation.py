"""Tests for elevation.py — Phase 4."""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from flightmanager.elevation import tile_fetcher, validate_tile

# Real tile downloaded during development — used as a fixture if present.
_REAL_TILE = Path("/tmp/test_tile.tif")


def _mock_tiff_response(content: bytes | None = None) -> MagicMock:
    """Return a mock requests response that looks like a WCS TIFF response."""
    if content is None:
        # Minimal valid 1-byte placeholder; enough for write tests
        content = b"TIFF_PLACEHOLDER"
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.headers = {"Content-Type": "image/tiff"}
    resp.iter_content = MagicMock(return_value=iter([content]))
    return resp


def _mock_xml_error_response() -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.headers = {"Content-Type": "text/xml"}
    resp.content = b"<ExceptionReport><Exception>Invalid parameter</Exception></ExceptionReport>"
    resp.iter_content = MagicMock(return_value=iter([resp.content]))
    return resp


# ---------------------------------------------------------------------------
# tile_fetcher — request shape
# ---------------------------------------------------------------------------


class TestTileFetcherRequest:
    def test_correct_wcs_url(self, tmp_path):
        sess = MagicMock()
        sess.get.return_value = _mock_tiff_response()
        fetcher = tile_fetcher("test-key", session=sess)
        fetcher("E295000_N6974000", (295_000.0, 6_974_000.0, 296_000.0, 6_975_000.0),
                tmp_path / "tile.tif")
        url = sess.get.call_args[0][0]
        assert "avoin-karttakuva.maanmittauslaitos.fi" in url

    def test_api_key_in_params(self, tmp_path):
        sess = MagicMock()
        sess.get.return_value = _mock_tiff_response()
        fetcher = tile_fetcher("my-secret", session=sess)
        fetcher("E295000_N6974000", (295_000.0, 6_974_000.0, 296_000.0, 6_975_000.0),
                tmp_path / "tile.tif")
        params = sess.get.call_args[1]["params"]
        assert params["api-key"] == "my-secret"

    def test_coverage_id_in_params(self, tmp_path):
        sess = MagicMock()
        sess.get.return_value = _mock_tiff_response()
        fetcher = tile_fetcher("key", session=sess)
        fetcher("E295000_N6974000", (295_000.0, 6_974_000.0, 296_000.0, 6_975_000.0),
                tmp_path / "tile.tif")
        params = sess.get.call_args[1]["params"]
        assert params["CoverageID"] == "korkeusmalli_2m"

    def test_subset_contains_bbox(self, tmp_path):
        sess = MagicMock()
        sess.get.return_value = _mock_tiff_response()
        fetcher = tile_fetcher("key", session=sess)
        fetcher("E295000_N6974000", (295_000.0, 6_974_000.0, 296_000.0, 6_975_000.0),
                tmp_path / "tile.tif")
        params = sess.get.call_args[1]["params"]
        subsets = params["SUBSET"]
        assert any("295000" in s and "296000" in s for s in subsets)
        assert any("6974000" in s and "6975000" in s for s in subsets)

    def test_returns_source_url(self, tmp_path):
        sess = MagicMock()
        sess.get.return_value = _mock_tiff_response()
        fetcher = tile_fetcher("key", session=sess)
        url, version = fetcher("E295000_N6974000",
                               (295_000.0, 6_974_000.0, 296_000.0, 6_975_000.0),
                               tmp_path / "tile.tif")
        assert url.startswith("https://")
        assert version is None  # WCS doesn't expose a dataset version string

    def test_tile_written_to_dest(self, tmp_path):
        content = b"FAKE_TIFF_DATA"
        sess = MagicMock()
        sess.get.return_value = _mock_tiff_response(content)
        fetcher = tile_fetcher("key", session=sess)
        dest = tmp_path / "dem" / "E295000_N6974000.tif"
        fetcher("E295000_N6974000", (295_000.0, 6_974_000.0, 296_000.0, 6_975_000.0), dest)
        assert dest.exists()
        assert dest.read_bytes() == content

    def test_xml_error_response_raises(self, tmp_path):
        sess = MagicMock()
        sess.get.return_value = _mock_xml_error_response()
        fetcher = tile_fetcher("key", session=sess)
        with pytest.raises(RuntimeError, match="unexpected Content-Type"):
            fetcher("E295000_N6974000", (295_000.0, 6_974_000.0, 296_000.0, 6_975_000.0),
                    tmp_path / "tile.tif")

    def test_http_error_propagates(self, tmp_path):
        sess = MagicMock()
        resp = MagicMock()
        resp.raise_for_status.side_effect = Exception("403 Forbidden")
        resp.headers = {}
        sess.get.return_value = resp
        fetcher = tile_fetcher("bad-key", session=sess)
        with pytest.raises(Exception, match="403"):
            fetcher("E295000_N6974000", (295_000.0, 6_974_000.0, 296_000.0, 6_975_000.0),
                    tmp_path / "tile.tif")


# ---------------------------------------------------------------------------
# validate_tile — uses real downloaded tile if available
# ---------------------------------------------------------------------------


class TestValidateTile:
    @pytest.fixture
    def real_tile(self, tmp_path):
        if not _REAL_TILE.exists():
            pytest.skip("Real DEM tile not present — run live integration test first")
        dest = tmp_path / "tile.tif"
        shutil.copy(_REAL_TILE, dest)
        return dest

    def test_validate_returns_stats(self, real_tile):
        stats = validate_tile(real_tile)
        assert stats["crs"] == "EPSG:3067"
        assert stats["elevation_min_m"] is not None
        assert stats["elevation_max_m"] >= stats["elevation_min_m"]

    def test_validate_shape_matches_1km_tile(self, real_tile):
        stats = validate_tile(real_tile)
        w, h = stats["shape"]
        # 1 km / 2 m resolution = 500 px
        assert w == 500 and h == 500

    def test_validate_bounds_in_3067(self, real_tile):
        stats = validate_tile(real_tile)
        xmin, ymin, xmax, ymax = stats["bounds"]
        assert 60_000 < xmin < 800_000
        assert 6_500_000 < ymin < 7_800_000

    def test_wrong_crs_raises(self, tmp_path):
        """A non-3067 raster should be rejected by validate_tile."""
        import numpy as np
        import rasterio
        from rasterio.transform import from_bounds
        from rasterio.crs import CRS

        fake = tmp_path / "wrong_crs.tif"
        transform = from_bounds(22.0, 62.0, 22.1, 62.1, 100, 100)
        with rasterio.open(
            fake, "w", driver="GTiff", height=100, width=100,
            count=1, dtype="float32", crs=CRS.from_epsg(4326),
            transform=transform,
        ) as ds:
            ds.write(np.ones((1, 100, 100), dtype="float32"))

        with pytest.raises(ValueError, match="EPSG:3067"):
            validate_tile(fake)


# ---------------------------------------------------------------------------
# Live integration test
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skip(reason="Hits live MML WCS — run with -m integration and set MML_API_KEY")
def test_live_fetch_tile():
    import os
    import tempfile
    api_key = os.environ["MML_API_KEY"]
    fetcher = tile_fetcher(api_key)
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "E295000_N6974000.tif"
        url, _ = fetcher("E295000_N6974000",
                         (295_000.0, 6_974_000.0, 296_000.0, 6_975_000.0), dest)
        assert dest.exists()
        assert dest.stat().st_size > 100_000  # should be ~750 KB
        stats = validate_tile(dest)
        assert stats["crs"] == "EPSG:3067"
        assert stats["shape"] == (500, 500)
        assert stats["elevation_min_m"] > 0  # Finnish terrain above sea level
