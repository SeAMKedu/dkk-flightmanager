"""Tests for zones.py — Phase 3b."""

from __future__ import annotations

import json
import os
import time
from unittest.mock import MagicMock

import pytest
from shapely.geometry import Polygon

from flightmanager.config import ZonesConfig
from flightmanager.crs import CRSError
from flightmanager.zones import AltitudeLimits, check_zones

# ---------------------------------------------------------------------------
# Survey polygon in EPSG:4326 — Finnish field area
# ---------------------------------------------------------------------------

SURVEY_4326 = Polygon([
    (22.60, 62.50), (22.70, 62.50),
    (22.70, 62.60), (22.60, 62.60),
    (22.60, 62.50),
])

# Traficom API response format
def _api_response(features: list[dict]) -> dict:
    return {"title": "Finnish UASZoneVersion test", "features": features}


def _zone_feature(
    identifier: str = "EFTEST",
    name: str = "Test Zone",
    restriction: str = "REQ_AUTHORISATION",
    reason: list[str] | None = None,
    coords: list | None = None,
    upper_limit: float | None = 120,
    upper_uom: str = "M",
    upper_ref: str = "AGL",
) -> dict:
    if coords is None:
        # Overlapping with SURVEY_4326
        coords = [[[22.55, 62.45], [22.75, 62.45],
                   [22.75, 62.65], [22.55, 62.65], [22.55, 62.45]]]
    geom_entry: dict = {
        "uomDimensions": upper_uom,
        "upperVerticalReference": upper_ref,
        "lowerVerticalReference": "AGL",
        "horizontalProjection": {"type": "Polygon", "coordinates": coords},
    }
    if upper_limit is not None:
        geom_entry["upperLimit"] = upper_limit
    return {
        "identifier": identifier,
        "name": name,
        "type": "COMMON",
        "restriction": restriction,
        "reason": reason or ["AIR_TRAFFIC"],
        "applicability": [{"permanent": "YES"}],
        "geometry": [geom_entry],
    }


def _away_zone() -> dict:
    return _zone_feature(
        identifier="EFAWAY", name="Helsinki TMA",
        coords=[[[24.90, 60.30], [25.10, 60.30],
                 [25.10, 60.50], [24.90, 60.50], [24.90, 60.30]]],
    )


def _mock_session(features: list[dict]) -> MagicMock:
    sess = MagicMock()
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = _api_response(features)
    sess.get.return_value = resp
    return sess


# ---------------------------------------------------------------------------
# CRS guard
# ---------------------------------------------------------------------------


class TestCrsGuard:
    def test_rejects_3067_input(self, tmp_path):
        survey_3067 = Polygon([
            (300_000, 6_900_000), (301_000, 6_900_000),
            (301_000, 6_901_000), (300_000, 6_901_000),
        ])
        cfg = ZonesConfig()
        with pytest.raises(CRSError):
            check_zones(survey_3067, cfg, cache_dir=tmp_path)


# ---------------------------------------------------------------------------
# No intersection
# ---------------------------------------------------------------------------


class TestNoIntersection:
    def test_no_hit_returns_flight_ready(self, tmp_path):
        sess = _mock_session([_away_zone()])
        cfg = ZonesConfig()
        result = check_zones(SURVEY_4326, cfg, cache_dir=tmp_path, session=sess)
        assert result.checked is True
        assert result.flight_ready is True
        assert result.needs_review is False
        assert result.intersecting_zones == []

    def test_no_restriction_zones_not_flagged(self, tmp_path):
        # Nature reserve with NO_RESTRICTION should not trigger a hit
        zone = _zone_feature(restriction="NO_RESTRICTION", reason=["SENSITIVE"])
        sess = _mock_session([zone])
        cfg = ZonesConfig()
        result = check_zones(SURVEY_4326, cfg, cache_dir=tmp_path, session=sess)
        assert result.flight_ready is True
        assert result.intersecting_zones == []

    def test_empty_zone_list_passes(self, tmp_path):
        sess = _mock_session([])
        cfg = ZonesConfig()
        result = check_zones(SURVEY_4326, cfg, cache_dir=tmp_path, session=sess)
        assert result.checked is True
        assert result.flight_ready is True


# ---------------------------------------------------------------------------
# Intersection
# ---------------------------------------------------------------------------


class TestIntersection:
    def test_prohibited_sets_not_flight_ready(self, tmp_path):
        zone = _zone_feature(restriction="PROHIBITED", reason=["SENSITIVE"])
        sess = _mock_session([zone])
        cfg = ZonesConfig()
        result = check_zones(SURVEY_4326, cfg, cache_dir=tmp_path, session=sess)
        assert result.checked is True
        assert result.needs_review is True
        assert result.flight_ready is False

    def test_req_authorisation_sets_needs_review(self, tmp_path):
        zone = _zone_feature(restriction="REQ_AUTHORISATION", reason=["AIR_TRAFFIC"])
        sess = _mock_session([zone])
        cfg = ZonesConfig()
        result = check_zones(SURVEY_4326, cfg, cache_dir=tmp_path, session=sess)
        assert result.needs_review is True
        assert result.flight_ready is False

    def test_zone_name_and_identifier_recorded(self, tmp_path):
        zone = _zone_feature(identifier="EFSEINAS", name="Seinäjoki CTR")
        sess = _mock_session([zone])
        cfg = ZonesConfig()
        result = check_zones(SURVEY_4326, cfg, cache_dir=tmp_path, session=sess)
        hit = result.intersecting_zones[0]
        assert hit.identifier == "EFSEINAS"
        assert hit.name == "Seinäjoki CTR"

    def test_multiple_hits_all_recorded(self, tmp_path):
        zones = [
            _zone_feature(identifier="Z1", name="Zone 1"),
            _zone_feature(identifier="Z2", name="Zone 2", restriction="PROHIBITED"),
        ]
        sess = _mock_session(zones)
        cfg = ZonesConfig()
        result = check_zones(SURVEY_4326, cfg, cache_dir=tmp_path, session=sess)
        assert len(result.intersecting_zones) == 2
        assert len(result.reasons) == 2

    def test_non_overlapping_zone_not_recorded(self, tmp_path):
        # First overlaps, second doesn't. Give away_zone REQ_AUTHORISATION so the
        # test exercises the spatial filter only (not the restriction filter).
        away = {**_away_zone(), "restriction": "REQ_AUTHORISATION"}
        sess = _mock_session([_zone_feature(), away])
        cfg = ZonesConfig()
        result = check_zones(SURVEY_4326, cfg, cache_dir=tmp_path, session=sess)
        assert len(result.intersecting_zones) == 1


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


class TestCaching:
    def test_api_called_on_cache_miss(self, tmp_path):
        sess = _mock_session([])
        cfg = ZonesConfig()
        check_zones(SURVEY_4326, cfg, cache_dir=tmp_path, session=sess)
        sess.get.assert_called_once()

    def test_cache_hit_skips_api(self, tmp_path):
        sess = _mock_session([])
        cfg = ZonesConfig()
        # First call populates cache
        check_zones(SURVEY_4326, cfg, cache_dir=tmp_path, session=sess)
        # Second call should hit cache
        sess2 = _mock_session([])
        check_zones(SURVEY_4326, cfg, cache_dir=tmp_path, session=sess2)
        sess2.get.assert_not_called()

    def test_stale_cache_refetched(self, tmp_path):
        sess = _mock_session([])
        cfg = ZonesConfig(max_age_days=1)
        check_zones(SURVEY_4326, cfg, cache_dir=tmp_path, session=sess)
        # Backdate cache file by 2 days
        cache_file = tmp_path / "zones" / "uas_zones.json"
        old = time.time() - 2 * 86_400
        os.utime(cache_file, (old, old))
        sess2 = _mock_session([])
        check_zones(SURVEY_4326, cfg, cache_dir=tmp_path, session=sess2)
        sess2.get.assert_called_once()

    def test_api_failure_uses_stale_cache(self, tmp_path):
        # Populate cache
        good_sess = _mock_session([])
        cfg = ZonesConfig(max_age_days=1)
        check_zones(SURVEY_4326, cfg, cache_dir=tmp_path, session=good_sess)
        # Backdate cache
        cache_file = tmp_path / "zones" / "uas_zones.json"
        old = time.time() - 2 * 86_400
        os.utime(cache_file, (old, old))
        # Failing session
        fail_sess = MagicMock()
        fail_sess.get.side_effect = Exception("network error")
        result = check_zones(SURVEY_4326, cfg, cache_dir=tmp_path, session=fail_sess)
        assert result.checked is True  # stale cache was used


# ---------------------------------------------------------------------------
# Override file
# ---------------------------------------------------------------------------


class TestOverrideFile:
    def test_zones_file_overrides_api(self, tmp_path):
        override = tmp_path / "custom_zones.geojson"
        override.write_text(json.dumps({
            "type": "FeatureCollection",
            "features": [_zone_feature()],
        }))
        cfg = ZonesConfig(zones_file=str(override))
        # No session provided — would fail if API were called
        result = check_zones(SURVEY_4326, cfg, cache_dir=tmp_path)
        assert result.checked is True
        assert result.needs_review is True

    def test_missing_override_file_returns_unchecked(self, tmp_path):
        cfg = ZonesConfig(zones_file="/nonexistent/zones.geojson")
        result = check_zones(SURVEY_4326, cfg, cache_dir=tmp_path)
        assert result.checked is False
        assert result.needs_review is True


# ---------------------------------------------------------------------------
# Altitude limits
# ---------------------------------------------------------------------------


class TestAltitudeLimits:
    def test_upper_limit_m_agl_direct(self):
        a = AltitudeLimits(120, "M", "AGL", 0, "M", "AGL")
        assert a.upper_limit_m_agl == 120.0

    def test_upper_limit_ft_converted(self):
        a = AltitudeLimits(500, "FT", "AGL", 0, "FT", "AGL")
        assert abs(a.upper_limit_m_agl - 152.4) < 0.1

    def test_amsl_returns_none(self):
        a = AltitudeLimits(2000, "FT", "AMSL", 0, "FT", "AMSL")
        assert a.upper_limit_m_agl is None

    def test_no_upper_limit(self):
        a = AltitudeLimits(None, None, None, 0, "M", "AGL")
        assert a.upper_limit_m_agl is None

    def test_ceiling_note_above_zone(self):
        # lower=0: ground-up zone; flight above the ceiling
        a = AltitudeLimits(50, "M", "AGL", 0, "M", "AGL")
        note = a.ceiling_note(100.0)
        assert "above" in note.lower()
        assert "50" in note

    def test_ceiling_note_within_zone(self):
        # lower=0: ground-up zone; flight inside it
        a = AltitudeLimits(120, "M", "AGL", 0, "M", "AGL")
        note = a.ceiling_note(100.0)
        assert "inside" in note or "within" in note

    def test_ceiling_note_amsl(self):
        a = AltitudeLimits(2000, "FT", "AMSL", 0, "FT", "AMSL")
        note = a.ceiling_note(100.0)
        assert "AMSL" in note
        assert "cannot compare" in note

    def test_ceiling_note_no_limit(self):
        a = AltitudeLimits(None, None, None, 0, "M", "AGL")
        note = a.ceiling_note(100.0)
        assert "all altitudes" in note

    def test_hit_carries_altitude_info(self, tmp_path):
        # Zone with 50m ceiling — flight at 100m is above it
        zone = _zone_feature(upper_limit=50, upper_uom="M", upper_ref="AGL")
        sess = _mock_session([zone])
        cfg = ZonesConfig()
        result = check_zones(SURVEY_4326, cfg, flight_height_m=100.0,
                             cache_dir=tmp_path, session=sess)
        hit = result.intersecting_zones[0]
        assert hit.altitude.upper_limit == 50
        assert "above" in result.reasons[0].lower()

    def test_hit_within_zone_note(self, tmp_path):
        # Zone with 120m ceiling — flight at 100m is inside it
        zone = _zone_feature(upper_limit=120, upper_uom="M", upper_ref="AGL")
        sess = _mock_session([zone])
        cfg = ZonesConfig()
        result = check_zones(SURVEY_4326, cfg, flight_height_m=100.0,
                             cache_dir=tmp_path, session=sess)
        assert "within" in result.reasons[0] or "inside" in result.reasons[0]


# ---------------------------------------------------------------------------
# Live integration test
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skip(reason="Hits live Traficom API — run with -m integration")
def test_live_zone_check():
    """Verify the Traficom API is reachable and returns parseable zones."""
    from shapely.geometry import Point
    import tempfile
    # A point in rural South Ostrobothnia — should be outside all restricted zones
    rural_survey = Point(22.66, 62.55).buffer(0.01)
    with tempfile.TemporaryDirectory() as tmp:
        cfg = ZonesConfig()
        result = check_zones(rural_survey, cfg, cache_dir=tmp)
        assert result.checked is True
        # (Result may or may not intersect — just confirm no crash and API works)
