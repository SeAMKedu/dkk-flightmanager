"""Tests for parcels.py — Phase 2."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from flightmanager.config import ParcelsConfig
from flightmanager.parcels import (
    ParcelNotFoundError,
    _fetch_by_bbox,
    _fetch_by_ids,
    _to_parcel,
    fetch_parcels,
)

# ---------------------------------------------------------------------------
# Fixture GeoJSON features (EPSG:3067 coords in Finnish range)
# ---------------------------------------------------------------------------

_FEATURE_A = {
    "type": "Feature",
    "properties": {
        "TUNNUS": 47,
        "VUOSI": 2024,
        "PERUSLOHKOTUNNUS": "0040003911",
        "PINTA_ALA": 3.4,
    },
    "geometry": {
        "type": "Polygon",
        "coordinates": [[
            [300000, 6900000], [301000, 6900000],
            [301000, 6901000], [300000, 6901000],
            [300000, 6900000],
        ]],
    },
}

_FEATURE_B = {
    "type": "Feature",
    "properties": {
        "TUNNUS": 48,
        "VUOSI": 2024,
        "PERUSLOHKOTUNNUS": "0040003912",
        "PINTA_ALA": 2.1,
    },
    "geometry": {
        "type": "Polygon",
        "coordinates": [[
            [302000, 6900000], [303000, 6900000],
            [303000, 6901000], [302000, 6901000],
            [302000, 6900000],
        ]],
    },
}


def _mock_response(features: list[dict], number_matched: int | None = None) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    payload = {
        "type": "FeatureCollection",
        "numberReturned": len(features),
        "features": features,
    }
    if number_matched is not None:
        payload["numberMatched"] = number_matched
    resp.json.return_value = payload
    return resp


# ---------------------------------------------------------------------------
# _to_parcel
# ---------------------------------------------------------------------------


class TestToParcel:
    def test_fields_mapped_correctly(self):
        p = _to_parcel(_FEATURE_A)
        assert p.parcel_id == "0040003911"
        assert p.tunnus == 47
        assert p.year == 2024
        assert abs(p.area_ha - 3.4) < 1e-6

    def test_geometry_is_3067(self):
        p = _to_parcel(_FEATURE_A)
        # Bounds should be in Finnish EPSG:3067 range
        bounds = p.geometry.bounds
        assert 60_000 < bounds[0] < 800_000
        assert 6_500_000 < bounds[1] < 7_800_000

    def test_geometry_outside_3067_raises(self):
        from flightmanager.crs import CRSError
        bad_feature = {**_FEATURE_A, "geometry": {
            "type": "Polygon",
            "coordinates": [[[22.6, 62.5], [22.7, 62.5], [22.7, 62.6], [22.6, 62.6], [22.6, 62.5]]],
        }}
        with pytest.raises(CRSError):
            _to_parcel(bad_feature)


# ---------------------------------------------------------------------------
# fetch_parcels input validation
# ---------------------------------------------------------------------------


class TestFetchParcelsValidation:
    def test_neither_ids_nor_bbox_raises(self):
        with pytest.raises(ValueError, match="exactly one"):
            fetch_parcels()

    def test_both_ids_and_bbox_raises(self):
        with pytest.raises(ValueError, match="exactly one"):
            fetch_parcels(parcel_ids=["123"], bbox=(0, 0, 1000, 1000))


# ---------------------------------------------------------------------------
# ID-based fetch
# ---------------------------------------------------------------------------


class TestFetchByIds:
    def test_single_id_returns_parcel(self):
        cfg = ParcelsConfig(lpis_year=2024)
        sess = MagicMock()
        sess.get.return_value = _mock_response([_FEATURE_A])

        parcels = fetch_parcels(
            parcel_ids=["0040003911"], config=cfg, session=sess
        )

        assert len(parcels) == 1
        assert parcels[0].parcel_id == "0040003911"

    def test_cql_filter_in_request(self):
        cfg = ParcelsConfig(lpis_year=2024)
        sess = MagicMock()
        sess.get.return_value = _mock_response([_FEATURE_A, _FEATURE_B])

        fetch_parcels(
            parcel_ids=["0040003911", "0040003912"], config=cfg, session=sess
        )

        call_kwargs = sess.get.call_args
        params = call_kwargs[1]["params"] if "params" in call_kwargs[1] else call_kwargs[0][1]
        assert "CQL_FILTER" in params
        assert "0040003911" in params["CQL_FILTER"]
        assert "0040003912" in params["CQL_FILTER"]

    def test_missing_id_raises(self):
        cfg = ParcelsConfig(lpis_year=2024)
        sess = MagicMock()
        # Only returns one of the two requested parcels
        sess.get.return_value = _mock_response([_FEATURE_A])

        with pytest.raises(ParcelNotFoundError) as exc:
            fetch_parcels(
                parcel_ids=["0040003911", "0040003912"], config=cfg, session=sess
            )
        assert "0040003912" in str(exc.value)

    def test_layer_uses_configured_year(self):
        cfg = ParcelsConfig(lpis_year=2023)
        sess = MagicMock()
        sess.get.return_value = _mock_response([_FEATURE_A])

        fetch_parcels(parcel_ids=["0040003911"], config=cfg, session=sess)

        params = sess.get.call_args[1]["params"]
        assert "2023" in params.get("typeNames", "")

    def test_default_year_is_current_minus_one(self):
        import datetime
        cfg = ParcelsConfig()
        assert cfg.lpis_year == datetime.date.today().year - 1


# ---------------------------------------------------------------------------
# Bbox-based fetch
# ---------------------------------------------------------------------------


class TestFetchByBbox:
    def test_bbox_returns_parcels(self):
        cfg = ParcelsConfig(lpis_year=2024)
        sess = MagicMock()
        sess.get.return_value = _mock_response([_FEATURE_A, _FEATURE_B])

        parcels = fetch_parcels(
            bbox=(299_000.0, 6_899_000.0, 304_000.0, 6_902_000.0),
            config=cfg,
            session=sess,
        )

        assert len(parcels) == 2

    def test_bbox_param_in_request(self):
        cfg = ParcelsConfig(lpis_year=2024)
        sess = MagicMock()
        sess.get.return_value = _mock_response([_FEATURE_A])

        fetch_parcels(
            bbox=(299_000.0, 6_899_000.0, 304_000.0, 6_902_000.0),
            config=cfg,
            session=sess,
        )

        params = sess.get.call_args[1]["params"]
        assert "BBOX" in params
        assert "EPSG:3067" in params["BBOX"]

    def test_empty_bbox_returns_empty_list(self):
        cfg = ParcelsConfig(lpis_year=2024)
        sess = MagicMock()
        sess.get.return_value = _mock_response([])

        parcels = fetch_parcels(
            bbox=(0.0, 0.0, 1000.0, 1000.0),
            config=cfg,
            session=sess,
        )

        assert parcels == []


# ---------------------------------------------------------------------------
# Paging
# ---------------------------------------------------------------------------


class TestPaging:
    def test_second_page_fetched_when_first_full(self):
        cfg = ParcelsConfig(lpis_year=2024, page_size=1)
        sess = MagicMock()
        # numberMatched=2 tells the client there are 2 features total
        sess.get.side_effect = [
            _mock_response([_FEATURE_A], number_matched=2),
            _mock_response([_FEATURE_B], number_matched=2),
        ]

        parcels = fetch_parcels(
            bbox=(299_000.0, 6_899_000.0, 304_000.0, 6_902_000.0),
            config=cfg,
            session=sess,
        )

        assert sess.get.call_count == 2
        assert len(parcels) == 2

    def test_startindex_increments_correctly(self):
        cfg = ParcelsConfig(lpis_year=2024, page_size=1)
        sess = MagicMock()
        sess.get.side_effect = [
            _mock_response([_FEATURE_A], number_matched=2),
            _mock_response([_FEATURE_B], number_matched=2),
        ]

        fetch_parcels(
            bbox=(299_000.0, 6_899_000.0, 304_000.0, 6_902_000.0),
            config=cfg,
            session=sess,
        )

        first_start = sess.get.call_args_list[0][1]["params"]["startIndex"]
        second_start = sess.get.call_args_list[1][1]["params"]["startIndex"]
        assert first_start == 0
        assert second_start == 1


# ---------------------------------------------------------------------------
# Live network integration test (skipped by default)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skip(reason="Hits live Ruokavirasto WFS — run with -m integration to enable")
def test_live_fetch_by_id():
    """Fetch a real parcel and verify geometry is in EPSG:3067."""
    from flightmanager.crs import assert_crs
    parcels = fetch_parcels(parcel_ids=["0040003911"])
    assert len(parcels) == 1
    assert parcels[0].parcel_id == "0040003911"
    assert parcels[0].area_ha > 0
    assert_crs(parcels[0].geometry, 3067)


@pytest.mark.integration
@pytest.mark.skip(reason="Hits live Ruokavirasto WFS — run with -m integration to enable")
def test_live_fetch_by_bbox():
    """Fetch parcels by bbox near Seinäjoki and verify results."""
    # Small area near Seinäjoki in EPSG:3067
    parcels = fetch_parcels(bbox=(297_000.0, 6_976_000.0, 300_000.0, 6_979_000.0))
    assert len(parcels) > 0
    for p in parcels:
        assert p.year >= 2024
        assert p.area_ha > 0
