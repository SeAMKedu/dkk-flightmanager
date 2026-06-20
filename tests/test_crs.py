"""Tests for crs.py guard rails."""

import pytest
from shapely.geometry import Point, Polygon

from flightmanager.crs import CRSError, assert_crs, require_3067, require_4326

# A real Finnish field centroid in each CRS
POINT_3067 = Point(305_000, 6_900_000)  # ETRS-TM35FIN, near Seinäjoki
POINT_4326 = Point(22.66, 62.55)  # WGS84, same area


class TestAssertCrs:
    def test_valid_3067_passes(self):
        assert_crs(POINT_3067, 3067)

    def test_valid_4326_passes(self):
        assert_crs(POINT_4326, 4326)

    def test_3067_coords_rejected_as_4326(self):
        with pytest.raises(CRSError):
            assert_crs(POINT_3067, 4326)

    def test_4326_coords_rejected_as_3067(self):
        with pytest.raises(CRSError):
            assert_crs(POINT_4326, 3067)

    def test_unknown_epsg_raises_value_error(self):
        with pytest.raises(ValueError, match="No guard bounds"):
            assert_crs(POINT_4326, 32635)


class TestConvenienceWrappers:
    def test_require_4326_valid(self):
        require_4326(POINT_4326)

    def test_require_4326_rejects_3067(self):
        with pytest.raises(CRSError):
            require_4326(POINT_3067)

    def test_require_3067_valid(self):
        require_3067(POINT_3067)

    def test_require_3067_rejects_4326(self):
        with pytest.raises(CRSError):
            require_3067(POINT_4326)

    def test_polygon_3067(self):
        poly = Polygon(
            [
                (300_000, 6_895_000),
                (310_000, 6_895_000),
                (310_000, 6_905_000),
                (300_000, 6_905_000),
            ]
        )
        require_3067(poly)

    def test_polygon_4326(self):
        poly = Polygon(
            [
                (22.60, 62.50),
                (22.70, 62.50),
                (22.70, 62.60),
                (22.60, 62.60),
            ]
        )
        require_4326(poly)
