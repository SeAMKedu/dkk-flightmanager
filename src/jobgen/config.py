"""Job and global configuration model."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator


# M3E camera constants — confirm against spec sheet (open question 11).
# These are used to derive flight height from target GSD.
M3E_FOCAL_LENGTH_MM = 12.3
M3E_PIXEL_PITCH_UM = 3.3
M3E_IMAGE_WIDTH_PX = 5280
M3E_IMAGE_HEIGHT_PX = 3956


class FlightConfig(BaseModel):
    target_gsd_cm: float = Field(gt=0, description="Target GSD in cm/px (authoritative)")
    max_height_agl_m: float = Field(default=110.0, le=120.0)
    agl_safety_margin_m: float = Field(default=10.0, ge=0)
    takeoff_security_height_m: float = Field(default=50.0)
    transitional_speed_ms: float = Field(default=15.0)
    overlap_front_pct: int = Field(default=80, ge=0, le=100)
    overlap_side_pct: int = Field(default=70, ge=0, le=100)
    finish_action: str = Field(default="goHome")
    rc_lost_action: str = Field(default="goBack")

    @model_validator(mode="after")
    def check_derived_height(self) -> "FlightConfig":
        h = self.derived_flight_height_m
        if h > self.max_height_agl_m:
            raise ValueError(
                f"Target GSD {self.target_gsd_cm} cm/px requires {h:.1f} m AGL "
                f"which exceeds max_height_agl_m={self.max_height_agl_m}"
            )
        return self

    @property
    def derived_flight_height_m(self) -> float:
        """Compute required AGL height from target GSD using M3E camera constants."""
        return (self.target_gsd_cm / 100) * M3E_FOCAL_LENGTH_MM / (M3E_PIXEL_PITCH_UM / 1000)


class HomeSafetyConfig(BaseModel):
    operating_subcategory: Literal["A2", "A3"] = "A3"
    home_buffer_m: float = Field(default=150.0, ge=0)
    residential_use_codes: list[str] = Field(default_factory=list)
    a3_additional_use_codes: list[str] = Field(default_factory=list)
    offset_enabled: bool = True
    max_area_loss_pct: float = Field(default=30.0, ge=0, le=100)


class PolygonConfig(BaseModel):
    edge_buffer_m: float = Field(default=0.0, ge=0)
    simplify_tolerance_m: float = Field(default=1.0, ge=0)
    multipart_policy: Literal["split", "largest", "review"] = "review"
    hole_policy: Literal["review", "fill", "clip"] = "review"


class CacheConfig(BaseModel):
    cache_dir: str = "cache"
    tile_size_m: int = Field(default=1000, gt=0)
    dem_ttl_days: int = Field(default=365, gt=0)
    buildings_ttl_days: int = Field(default=180, gt=0)
    offline: bool = False


class OutputConfig(BaseModel):
    output_dir: str = "output"


class AppConfig(BaseModel):
    flight: FlightConfig
    home_safety: HomeSafetyConfig = Field(default_factory=HomeSafetyConfig)
    polygon: PolygonConfig = Field(default_factory=PolygonConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)


def load_config(path: Path | str = "config.toml") -> AppConfig:
    """Load config.toml; fall back to config.example.toml if not found."""
    p = Path(path)
    if not p.exists():
        example = Path(__file__).parent.parent.parent / "config.example.toml"
        if example.exists():
            p = example
        else:
            raise FileNotFoundError(f"Config not found: {path}")
    with open(p, "rb") as f:
        raw = tomllib.load(f)
    return AppConfig.model_validate(raw)
