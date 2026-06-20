"""Tests for buildings.py — Phase 2b."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from flightmanager.buildings import (
    Building,
    _to_building,
    _write_geojson,
    dedup_buildings,
    filter_buildings,
    load_tile,
    tile_fetcher,
)
from flightmanager.config import HomeSafetyConfig

# ---------------------------------------------------------------------------
# Fixture features (EPSG:3067 coords, Finnish range)
# ---------------------------------------------------------------------------

def _make_feature(
    mtk_id: int,
    kohdeluokka: int,
    kayttotarkoitus: int,
    kerrosluku: int | None = None,
) -> dict:
    props: dict = {
        "mtk_id": mtk_id,
        "kohdeluokka": kohdeluokka,
        "kayttotarkoitus": kayttotarkoitus,
        "alkupvm": "2025-03-01",
    }
    if kerrosluku is not None:
        props["kerrosluku"] = kerrosluku
    return {
        "type": "Feature",
        "properties": props,
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [300000, 6900000], [300050, 6900000],
                [300050, 6900050], [300000, 6900050],
                [300000, 6900000],
            ]],
        },
    }


FEAT_RESIDENTIAL   = _make_feature(1001, 42211, 1)   # asuinrakennus
FEAT_COMMERCIAL    = _make_feature(1002, 42221, 2)   # liike-/julkinen
FEAT_HOLIDAY       = _make_feature(1003, 42231, 3)   # lomarakennus
FEAT_AGRICULTURAL  = _make_feature(1004, 42261, 6)   # maatalous/varasto


def _mock_response(features: list[dict]) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "type": "FeatureCollection",
        "numberReturned": len(features),
        "features": features,
    }
    return resp


# ---------------------------------------------------------------------------
# _to_building
# ---------------------------------------------------------------------------


class TestToBuilding:
    def test_fields_mapped(self):
        b = _to_building(FEAT_RESIDENTIAL)
        assert b is not None
        assert b.mtk_id == 1001
        assert b.kohdeluokka == 42211
        assert b.kayttotarkoitus == 1
        assert b.kerrosluku is None  # not present in FEAT_RESIDENTIAL fixture

    def test_kerrosluku_parsed(self):
        feat = _make_feature(9001, 42211, 1, kerrosluku=3)
        b = _to_building(feat)
        assert b is not None
        assert b.kerrosluku == 3

    def test_kerrosluku_absent_is_none(self):
        b = _to_building(FEAT_RESIDENTIAL)
        assert b.kerrosluku is None

    def test_geometry_in_3067(self):
        b = _to_building(FEAT_RESIDENTIAL)
        bounds = b.geometry.bounds
        assert 60_000 < bounds[0] < 800_000
        assert 6_500_000 < bounds[1] < 7_800_000

    def test_invalid_geometry_returns_none(self):
        bad = {**FEAT_RESIDENTIAL, "geometry": {"type": "Polygon", "coordinates": [[[22.6, 62.5]]]}}
        # 4326 coords → CRSError → returns None
        b = _to_building(bad)
        assert b is None


# ---------------------------------------------------------------------------
# GeoJSON round-trip
# ---------------------------------------------------------------------------


class TestGeoJSONRoundTrip:
    def test_write_and_load(self, tmp_path):
        dest = tmp_path / "buildings" / "E300000_N6900000.geojson"
        _write_geojson([FEAT_RESIDENTIAL, FEAT_COMMERCIAL], dest)
        buildings = load_tile(dest)
        assert len(buildings) == 2
        ids = {b.mtk_id for b in buildings}
        assert 1001 in ids
        assert 1002 in ids

    def test_load_empty_tile(self, tmp_path):
        dest = tmp_path / "E0_N0.geojson"
        _write_geojson([], dest)
        assert load_tile(dest) == []


# ---------------------------------------------------------------------------
# filter_buildings
# ---------------------------------------------------------------------------


class TestFilterBuildings:
    def _make_buildings(self) -> list[Building]:
        return [
            b for f in [FEAT_RESIDENTIAL, FEAT_COMMERCIAL, FEAT_HOLIDAY, FEAT_AGRICULTURAL]
            if (b := _to_building(f)) is not None
        ]

    def test_a3_residential_in_both_groups(self):
        cfg = HomeSafetyConfig(operating_subcategory="A3")
        buildings = self._make_buildings()
        res, a3 = filter_buildings(buildings, cfg)
        res_ids = {b.mtk_id for b in res}
        assert 1001 in res_ids

    def test_a3_commercial_in_additional(self):
        cfg = HomeSafetyConfig(operating_subcategory="A3")
        buildings = self._make_buildings()
        res, a3 = filter_buildings(buildings, cfg)
        a3_ids = {b.mtk_id for b in a3}
        assert 1002 in a3_ids
        assert 1003 in a3_ids  # holiday

    def test_agricultural_excluded(self):
        cfg = HomeSafetyConfig(operating_subcategory="A3")
        buildings = self._make_buildings()
        res, a3 = filter_buildings(buildings, cfg)
        all_ids = {b.mtk_id for b in res} | {b.mtk_id for b in a3}
        assert 1004 not in all_ids  # agricultural excluded

    def test_a2_only_residential(self):
        cfg = HomeSafetyConfig(operating_subcategory="A2")
        buildings = self._make_buildings()
        res, a3 = filter_buildings(buildings, cfg)
        # residential always returned
        assert any(b.mtk_id == 1001 for b in res)
        # commercial is in a3_additional group regardless — caller decides whether to use it
        assert any(b.mtk_id == 1002 for b in a3)

    def test_custom_codes(self):
        cfg = HomeSafetyConfig(
            residential_kohdeluokka=[42270],
            a3_additional_kohdeluokka=[],
        )
        buildings = self._make_buildings()
        res, a3 = filter_buildings(buildings, cfg)
        assert len(res) == 0  # none match 42270
        assert len(a3) == 0


# ---------------------------------------------------------------------------
# dedup_buildings
# ---------------------------------------------------------------------------


class TestDedupBuildings:
    def test_removes_duplicate_mtk_id(self):
        b1 = _to_building(FEAT_RESIDENTIAL)
        b2 = _to_building(FEAT_RESIDENTIAL)  # same mtk_id=1001
        result = dedup_buildings([b1, b2])
        assert len(result) == 1

    def test_keeps_distinct_ids(self):
        b1 = _to_building(FEAT_RESIDENTIAL)
        b2 = _to_building(FEAT_COMMERCIAL)
        result = dedup_buildings([b1, b2])
        assert len(result) == 2


# ---------------------------------------------------------------------------
# tile_fetcher integration with cache API shape
# ---------------------------------------------------------------------------


class TestTileFetcher:
    def test_fetcher_writes_geojson(self, tmp_path):
        sess = MagicMock()
        sess.get.return_value = _mock_response([FEAT_RESIDENTIAL, FEAT_COMMERCIAL])

        fetcher = tile_fetcher("test-api-key", session=sess)
        dest = tmp_path / "E300000_N6900000.geojson"
        source_url, version = fetcher(
            "E300000_N6900000",
            (300_000.0, 6_900_000.0, 301_000.0, 6_901_000.0),
            dest,
        )

        assert dest.exists()
        data = json.loads(dest.read_text())
        assert len(data["features"]) == 2
        assert source_url.startswith("https://")

    def test_fetcher_bbox_in_3067(self, tmp_path):
        sess = MagicMock()
        sess.get.return_value = _mock_response([])

        fetcher = tile_fetcher("test-api-key", session=sess)
        dest = tmp_path / "tile.geojson"
        fetcher("E300000_N6900000", (300_000.0, 6_900_000.0, 301_000.0, 6_901_000.0), dest)

        params = sess.get.call_args[1]["params"]
        assert "bbox-crs" in params
        assert "3067" in params["bbox-crs"]
        assert "crs" in params
        assert "3067" in params["crs"]

    def test_fetcher_api_key_in_params(self, tmp_path):
        sess = MagicMock()
        sess.get.return_value = _mock_response([])

        fetcher = tile_fetcher("my-secret-key", session=sess)
        dest = tmp_path / "tile.geojson"
        fetcher("E0_N0", (0.0, 0.0, 1000.0, 1000.0), dest)

        params = sess.get.call_args[1]["params"]
        assert params.get("api-key") == "my-secret-key"


# ---------------------------------------------------------------------------
# Live integration test (skipped by default)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skip(reason="Hits live MML API — run with -m integration and set MML_API_KEY")
def test_live_fetch_tile():
    import os
    api_key = os.environ["MML_API_KEY"]
    fetcher = tile_fetcher(api_key)
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".geojson", delete=False) as f:
        dest = Path(f.name)
    # Small 1km tile near Seinäjoki known to have buildings
    fetcher("E295000_N6974000", (295_000.0, 6_974_000.0, 296_000.0, 6_975_000.0), dest)
    buildings = load_tile(dest)
    assert len(buildings) >= 0  # area may be sparse; just confirm no crash
    for b in buildings:
        from flightmanager.crs import assert_crs
        assert_crs(b.geometry, 3067)
