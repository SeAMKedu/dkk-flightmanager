"""Tests for properties.py — kiinteistötunnus normalisation and geometry building."""

from __future__ import annotations

import pytest
from shapely.geometry import Polygon, mapping
from unittest.mock import MagicMock, patch

from jobgen.properties import (
    Property,
    PropertyNotFoundError,
    _normalise,
    _to_property,
    fetch_properties,
)


# ---------------------------------------------------------------------------
# _normalise
# ---------------------------------------------------------------------------


class TestNormalise:
    def test_dash_form_four_parts(self):
        assert _normalise("399-891-1-1") == "39989100010001"

    def test_dash_form_pads_each_segment(self):
        # kunta=5, sijainti=5, ryhma=3, yksikko=22
        assert _normalise("5-5-3-22") == "00500500030022"

    def test_numeric_form_passthrough(self):
        assert _normalise("39989100010001") == "39989100010001"

    def test_numeric_form_with_whitespace_stripped(self):
        assert _normalise("  39989100010001  ") == "39989100010001"

    def test_dash_form_with_whitespace_stripped(self):
        assert _normalise("  399-891-1-1  ") == "39989100010001"

    def test_too_few_dash_parts_raises(self):
        with pytest.raises(ValueError):
            _normalise("399-891-1")

    def test_too_many_dash_parts_raises(self):
        with pytest.raises(ValueError):
            _normalise("399-891-1-1-extra")

    def test_non_numeric_non_dash_raises(self):
        with pytest.raises(ValueError):
            _normalise("ABCDE")

    def test_wrong_length_numeric_raises(self):
        with pytest.raises(ValueError):
            _normalise("12345")


# ---------------------------------------------------------------------------
# _to_property
# ---------------------------------------------------------------------------


_POLY_3067 = Polygon([
    (300_000, 6_900_000), (301_000, 6_900_000),
    (301_000, 6_901_000), (300_000, 6_901_000),
    (300_000, 6_900_000),
])

_CRS_3067_URI = "http://www.opengis.net/def/crs/EPSG/0/3067"


def _make_feature(geom: Polygon, display_id: str = "399-891-1-1") -> dict:
    return {
        "type": "Feature",
        "properties": {
            "kiinteistotunnus": "39989100010001",
            "kiinteistotunnuksenEsitysmuoto": display_id,
        },
        "geometry": {
            "type": "Polygon",
            "coordinates": [list(geom.exterior.coords)],
            "crs": {"type": "name", "properties": {"name": _CRS_3067_URI}},
        },
    }


class TestToProperty:
    def test_single_feature_correct_fields(self):
        feature = _make_feature(_POLY_3067)
        prop = _to_property("39989100010001", [feature])
        assert prop.property_id == "39989100010001"
        assert prop.display_id == "399-891-1-1"
        assert prop.area_ha > 0
        assert not prop.geometry.is_empty

    def test_area_computed_from_geometry(self):
        feature = _make_feature(_POLY_3067)
        prop = _to_property("39989100010001", [feature])
        expected_ha = _POLY_3067.area / 10_000
        assert abs(prop.area_ha - expected_ha) < 0.001

    def test_two_palstat_unioned(self):
        p1 = _POLY_3067
        p2 = Polygon([
            (302_000, 6_900_000), (303_000, 6_900_000),
            (303_000, 6_901_000), (302_000, 6_901_000),
            (302_000, 6_900_000),
        ])
        features = [_make_feature(p1), _make_feature(p2)]
        prop = _to_property("39989100010001", features)
        # Union area ≤ sum of individual areas (no overlap)
        assert prop.geometry.area >= p1.area + p2.area - 1  # within rounding

    def test_display_id_from_first_feature(self):
        f1 = _make_feature(_POLY_3067, display_id="111-111-1-1")
        f2 = _make_feature(_POLY_3067, display_id="222-222-2-2")
        prop = _to_property("11111100010001", [f1, f2])
        assert prop.display_id == "111-111-1-1"


# ---------------------------------------------------------------------------
# fetch_properties (mocked network)
# ---------------------------------------------------------------------------


class TestFetchPropertiesMocked:
    def _feature_response(self, display_id: str = "399-891-1-1") -> dict:
        return {
            "features": [_make_feature(_POLY_3067, display_id=display_id)],
        }

    def test_returns_property_for_valid_id(self):
        mock_sess = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = self._feature_response()
        mock_resp.raise_for_status.return_value = None
        mock_sess.get.return_value = mock_resp

        results = fetch_properties(["399-891-1-1"], "fake-key", session=mock_sess)
        assert len(results) == 1
        assert results[0].display_id == "399-891-1-1"

    def test_accepts_numeric_id(self):
        mock_sess = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = self._feature_response()
        mock_resp.raise_for_status.return_value = None
        mock_sess.get.return_value = mock_resp

        results = fetch_properties(["39989100010001"], "fake-key", session=mock_sess)
        assert len(results) == 1

    def test_not_found_raises_property_not_found_error(self):
        mock_sess = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"features": []}
        mock_resp.raise_for_status.return_value = None
        mock_sess.get.return_value = mock_resp

        with pytest.raises(PropertyNotFoundError):
            fetch_properties(["0-0-0-1"], "fake-key", session=mock_sess)

    def test_cache_hit_skips_network(self, tmp_path):
        from jobgen.config import CacheConfig
        from jobgen.geo_cache import put_property_cache
        from shapely.wkt import dumps as wkt_dumps

        cfg = CacheConfig(cache_dir=str(tmp_path / "cache"))
        put_property_cache(
            cfg, "39989100010001", "399-891-1-1",
            area_ha=100.0, geometry_wkt=wkt_dumps(_POLY_3067),
        )

        mock_sess = MagicMock()
        results = fetch_properties(
            ["399-891-1-1"], "fake-key", session=mock_sess, cache_config=cfg
        )
        mock_sess.get.assert_not_called()
        assert len(results) == 1
        assert abs(results[0].area_ha - 100.0) < 0.001
