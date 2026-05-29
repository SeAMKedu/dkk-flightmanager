"""Tests for geometry.py — Phase 3."""

import pytest

pytest.importorskip("shapely", reason="shapely required")


class TestHomekeepout:
    def test_offset_polygon_clears_buffer(self):
        pytest.skip("Phase 3 not yet implemented")

    def test_multipart_result_flagged(self):
        pytest.skip("Phase 3 not yet implemented")

    def test_area_loss_above_threshold_flagged(self):
        pytest.skip("Phase 3 not yet implemented")


class TestCRSGuard:
    def test_3067_coords_rejected_as_4326(self):
        pytest.skip("Phase 3 not yet implemented")
