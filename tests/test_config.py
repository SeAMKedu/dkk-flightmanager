"""Tests for config.py — DroneConfig and AppConfig."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from flightmanager.config import AppConfig, DroneConfig, FlightConfig, HomeSafetyConfig


def _minimal_drone(**kwargs) -> DroneConfig:
    defaults = dict(
        name="test-drone",
        label="Test Drone",
        drone_enum=77,
        payload_enum=68,
        focal_length_mm=12.3,
        pixel_pitch_um=3.3,
        image_width_px=5280,
        image_height_px=3956,
    )
    defaults.update(kwargs)
    return DroneConfig(**defaults)


# ---------------------------------------------------------------------------
# DroneConfig — GSD ↔ height conversion
# ---------------------------------------------------------------------------


class TestDroneConfigGsd:
    def test_height_from_gsd_roundtrip(self):
        drone = _minimal_drone()
        gsd = 2.7
        height = drone.height_from_gsd(gsd)
        assert abs(drone.gsd_from_height(height) - gsd) < 0.001

    def test_higher_gsd_means_higher_flight(self):
        drone = _minimal_drone()
        assert drone.height_from_gsd(5.0) > drone.height_from_gsd(2.0)

    def test_longer_focal_length_means_lower_flight(self):
        d24 = _minimal_drone(focal_length_mm=24.0)
        d35 = _minimal_drone(focal_length_mm=35.0)
        # same GSD, longer lens → lower AGL (needs less height)
        assert d24.height_from_gsd(2.7) < d35.height_from_gsd(2.7)

    def test_gsd_from_height_is_positive(self):
        drone = _minimal_drone()
        assert drone.gsd_from_height(100) > 0

    def test_height_from_gsd_matches_formula(self):
        drone = _minimal_drone(focal_length_mm=12.3, pixel_pitch_um=3.3)
        expected = (2.7 / 100) * 12.3 / (3.3 / 1000)
        assert abs(drone.height_from_gsd(2.7) - expected) < 0.001


# ---------------------------------------------------------------------------
# AppConfig.active_drone
# ---------------------------------------------------------------------------


class TestAppConfigActiveDrone:
    def test_active_drone_returns_correct_profile(self):
        cfg = AppConfig(flight=FlightConfig(target_gsd_cm=2.7))
        drone = cfg.active_drone()
        assert drone.name == cfg.default_drone

    def test_all_built_in_drones_loadable(self):
        cfg = AppConfig(flight=FlightConfig(target_gsd_cm=2.7))
        assert len(cfg.drones) > 0
        for drone in cfg.drones:
            assert drone.focal_length_mm > 0

    def test_unknown_default_drone_rejected(self):
        with pytest.raises(ValidationError):
            AppConfig(
                flight=FlightConfig(target_gsd_cm=2.7),
                default_drone="nonexistent-drone",
            )


# ---------------------------------------------------------------------------
# HomeSafetyConfig.resolved_include_buffer_m
# ---------------------------------------------------------------------------


class TestResolvedIncludeBuffer:
    def test_defaults_to_twice_home_buffer(self):
        cfg = HomeSafetyConfig(home_buffer_m=150)
        assert cfg.resolved_include_buffer_m == 300.0

    def test_explicit_value_overrides_default(self):
        cfg = HomeSafetyConfig(home_buffer_m=150, home_include_buffer_m=100.0)
        assert cfg.resolved_include_buffer_m == 100.0

    def test_zero_include_buffer_allowed(self):
        cfg = HomeSafetyConfig(home_buffer_m=150, home_include_buffer_m=0.0)
        assert cfg.resolved_include_buffer_m == 0.0


# ---------------------------------------------------------------------------
# FlightConfig validation
# ---------------------------------------------------------------------------


class TestFlightConfigValidation:
    def test_rejects_zero_gsd(self):
        with pytest.raises(ValidationError):
            FlightConfig(target_gsd_cm=0)

    def test_rejects_negative_gsd(self):
        with pytest.raises(ValidationError):
            FlightConfig(target_gsd_cm=-1)

    def test_rejects_height_above_120(self):
        with pytest.raises(ValidationError):
            FlightConfig(target_gsd_cm=2.7, max_height_agl_m=121)
