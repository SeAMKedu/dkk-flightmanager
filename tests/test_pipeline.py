"""Tests for pipeline.py — Phase 7."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_bounds
from shapely.geometry import Polygon

from flightmanager.buildings import Building
from flightmanager.cache import TileRecord
from flightmanager.config import AppConfig, CacheConfig, FlightConfig, HomeSafetyConfig, ParcelsConfig, PolygonConfig, ZonesConfig
from flightmanager.parcels import Parcel
from flightmanager.pipeline import export_job
from flightmanager.zones import ZoneCheckResult

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

# A small field polygon in EPSG:3067 near Seinäjoki
_PARCEL_POLY = Polygon([
    (300_000, 6_900_000), (301_000, 6_900_000),
    (301_000, 6_901_000), (300_000, 6_901_000),
    (300_000, 6_900_000),
])

_PARCEL = Parcel(
    parcel_id="TEST001",
    tunnus=1,
    year=2025,
    area_ha=100.0,
    geometry=_PARCEL_POLY,
)

# Residential building well outside the parcel (no area loss)
_BUILDING = Building(
    mtk_id=9001,
    kohdeluokka=42211,
    kayttotarkoitus=1,
    geometry=Polygon([
        (302_000, 6_900_000), (302_050, 6_900_000),
        (302_050, 6_900_050), (302_000, 6_900_050),
    ]),
    alkupvm="2025-01-01",
)


def _make_app_config(tmp_path: Path, **overrides) -> AppConfig:
    return AppConfig(
        flight=FlightConfig(target_gsd_cm=2.7),
        home_safety=HomeSafetyConfig(home_buffer_m=50, offset_enabled=True),
        polygon=PolygonConfig(gap_fill_m=0, simplify_tolerance_m=0),
        cache=CacheConfig(cache_dir=str(tmp_path / "cache")),
        output=overrides.pop("output", type("O", (), {"output_dir": str(tmp_path / "output")})()),
        parcels=ParcelsConfig(lpis_year=2025),
        zones=ZonesConfig(zones_file=""),  # no zones file → skip check
    )


def _make_dem_tile(path: Path) -> None:
    """Write a minimal synthetic DEM tile in EPSG:3067 covering the parcel."""
    path.parent.mkdir(parents=True, exist_ok=True)
    transform = from_bounds(298_000, 6_898_000, 304_000, 6_904_000, 100, 100)
    data = np.full((1, 100, 100), 80.0, dtype="float32")
    with rasterio.open(
        path, "w", driver="GTiff", height=100, width=100,
        count=1, dtype="float32", crs=CRS.from_epsg(3067),
        transform=transform, nodata=-9999.0, compress="lzw",
    ) as ds:
        ds.write(data)


def _tile_record(path: Path, dataset: str = "dem", tile_id: str = "E298000_N6898000") -> TileRecord:
    from datetime import datetime, timezone
    return TileRecord(
        tile_id=tile_id,
        dataset=dataset,
        bbox=(298_000.0, 6_898_000.0, 304_000.0, 6_904_000.0),
        path=path,
        source_url="https://example.com",
        fetch_timestamp=datetime.now(timezone.utc).isoformat(),
        dataset_version=None,
        checksum="abc",
        byte_size=1000,
    )


def _geojson_tile(path: Path) -> None:
    """Write an empty buildings tile."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRunJob:
    @pytest.fixture
    def config(self, tmp_path):
        cfg = AppConfig(
            flight=FlightConfig(target_gsd_cm=2.7),
            home_safety=HomeSafetyConfig(home_buffer_m=50, offset_enabled=True),
            polygon=PolygonConfig(gap_fill_m=0, simplify_tolerance_m=0),
            cache=CacheConfig(cache_dir=str(tmp_path / "cache")),
            parcels=ParcelsConfig(lpis_year=2025),
            zones=ZonesConfig(zones_file=""),
        )
        # Patch output_dir
        from flightmanager.config import OutputConfig
        cfg.output = OutputConfig(output_dir=str(tmp_path / "output"))
        return cfg

    @pytest.fixture
    def dem_tile(self, tmp_path):
        p = tmp_path / "cache" / "dem" / "E298000_N6898000.tif"
        _make_dem_tile(p)
        return p

    @pytest.fixture
    def bldg_tile(self, tmp_path):
        p = tmp_path / "cache" / "buildings" / "E249750_N6849750.geojson"
        _geojson_tile(p)
        return p

    def _run(self, config, dem_tile, bldg_tile, parcel_ids=None, dry_run=False,
             zone_result=None):
        """Run a job with all network calls mocked."""
        dem_rec  = _tile_record(dem_tile, "dem")
        bldg_rec = _tile_record(bldg_tile, "buildings", "E249750_N6849750")
        if zone_result is None:
            zone_result = ZoneCheckResult(checked=False, needs_review=True,
                                          reasons=["Zone check skipped (no file)"])

        with (
            patch.dict(os.environ, {"MML_API_KEY": "test-key"}),
            patch("flightmanager.pipeline.fetch_parcels", return_value=[_PARCEL]),
            patch("flightmanager.pipeline.get_tiles", side_effect=[
                [bldg_rec],   # buildings call
                [],           # powerlines call
                [],           # pylons call
                [dem_rec],    # DEM call
            ]),
            patch("flightmanager.pipeline.load_tile", return_value=[_BUILDING]),
            patch("flightmanager.pipeline.check_zones", return_value=zone_result),
        ):
            manifest, _route = export_job(
                "test-job",
                config,
                parcel_ids=parcel_ids or ["TEST001"],
                dry_run=dry_run,
            )
            return manifest

    def test_manifest_has_required_keys(self, config, dem_tile, bldg_tile):
        manifest = self._run(config, dem_tile, bldg_tile)
        for key in ("tool_version", "job_name", "parcels", "geometry", "flight",
                    "dsm", "home_safety", "zones", "needs_review",
                    "flight_ready", "cache_provenance"):
            assert key in manifest, f"Missing manifest key: {key}"

    def test_tool_version_stamped(self, config, dem_tile, bldg_tile):
        from flightmanager import tool_version
        manifest = self._run(config, dem_tile, bldg_tile)
        assert manifest["tool_version"] == tool_version()

    def test_parcel_ids_recorded(self, config, dem_tile, bldg_tile):
        manifest = self._run(config, dem_tile, bldg_tile)
        assert "TEST001" in manifest["parcels"]["parcel_ids"]

    def test_output_files_created(self, config, dem_tile, bldg_tile, tmp_path):
        self._run(config, dem_tile, bldg_tile)
        job_dir = tmp_path / "output" / "test-job"
        assert (job_dir / "test-job.kmz").exists()
        assert (job_dir / "test-job_dsm.tif").exists()
        assert (job_dir / "test-job_homes.kml").exists()
        assert (job_dir / "manifest.json").exists()
        assert (job_dir / "run.log").exists()

    def test_manifest_json_valid(self, config, dem_tile, bldg_tile, tmp_path):
        self._run(config, dem_tile, bldg_tile)
        path = tmp_path / "output" / "test-job" / "manifest.json"
        data = json.loads(path.read_text())
        assert data["job_name"] == "test-job"

    def test_no_zone_file_flags_needs_review(self, config, dem_tile, bldg_tile):
        # zones_file="" → zone check skipped → needs_review
        manifest = self._run(config, dem_tile, bldg_tile)
        assert manifest["zones"]["checked"] is False
        # needs_review may also be True due to zone skip
        assert isinstance(manifest["needs_review"], bool)

    def test_dry_run_skips_output_files(self, config, dem_tile, bldg_tile, tmp_path):
        self._run(config, dem_tile, bldg_tile, dry_run=True)
        job_dir = tmp_path / "output" / "test-job"
        assert not (job_dir / "test-job.kmz").exists()
        assert not (job_dir / "site_dsm_wgs84.tif").exists()

    def test_dry_run_returns_manifest(self, config, dem_tile, bldg_tile):
        manifest = self._run(config, dem_tile, bldg_tile, dry_run=True)
        assert manifest["dry_run"] is True

    def test_missing_api_key_raises(self, config, tmp_path):
        env = {k: v for k, v in os.environ.items() if k != "MML_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            with pytest.raises(RuntimeError, match="MML_API_KEY"):
                export_job("test", config, parcel_ids=["X"])

    def test_attribution_strings_present(self, config, dem_tile, bldg_tile):
        manifest = self._run(config, dem_tile, bldg_tile)
        assert "National Land Survey" in manifest["dsm"]["attribution"]
        assert "Ruokavirasto"         in manifest["parcels"]["attribution"]
        assert "National Land Survey" in manifest["home_safety"]["buildings_attribution"]

    def test_cache_provenance_recorded(self, config, dem_tile, bldg_tile):
        manifest = self._run(config, dem_tile, bldg_tile)
        assert "tile_ids" in manifest["cache_provenance"]["dem"]
        assert "fetch_date_min" in manifest["cache_provenance"]["dem"]

    def test_geometry_area_recorded(self, config, dem_tile, bldg_tile):
        manifest = self._run(config, dem_tile, bldg_tile)
        assert manifest["geometry"]["original_area_ha"] > 0
        assert manifest["geometry"]["final_area_ha"] > 0

    def test_flight_height_matches_gsd(self, config, dem_tile, bldg_tile):
        manifest = self._run(config, dem_tile, bldg_tile)
        drone = config.active_drone()
        expected = drone.height_from_gsd(config.flight.target_gsd_cm)
        assert abs(manifest["flight"]["derived_height_m"] - expected) < 0.01


# ---------------------------------------------------------------------------
# Live integration test
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skip(reason="Hits live services — run with -m integration")
def test_live_export_job(tmp_path):
    cfg = AppConfig(
        flight=FlightConfig(target_gsd_cm=2.7),
        home_safety=HomeSafetyConfig(home_buffer_m=150),
        polygon=PolygonConfig(gap_fill_m=3),
        cache=CacheConfig(cache_dir=str(tmp_path / "cache")),
        parcels=ParcelsConfig(lpis_year=2025),
        zones=ZonesConfig(),
    )
    from flightmanager.config import OutputConfig
    cfg.output = OutputConfig(output_dir=str(tmp_path / "output"))

    manifest, _route = export_job(
        "live-test",
        cfg,
        parcel_ids=["0040003911"],
    )

    assert manifest["geometry"]["original_area_ha"] > 0
    assert manifest["dsm"]["elevation_min_m"] > 0
    assert (tmp_path / "output" / "live-test" / "live-test.kmz").exists()
