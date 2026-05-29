"""Job and global configuration model."""

from __future__ import annotations

import datetime
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
    # Speed along mapping strips (m/s). Default matches the reference fixture.
    # DJI Pilot 2 derives this from sensor shutter speed + GSD; 8.9 m/s is
    # appropriate for 80% front overlap at ~100 m AGL on the M3E.
    auto_flight_speed_ms: float = Field(default=8.9, gt=0)
    # Extra margin around the survey polygon bbox in the terrain-follow DSM (metres).
    # Covers the RTH path and takeoff/landing area outside the survey polygon.
    dsm_margin_m: int = Field(default=300, ge=0)
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
    # MML Maastotietokanta kohdeluokka codes treated as residential for keep-out.
    # Confirmed codes: 42210=asuinrakennus (point), 42211 (1-2 krs), 42212 (3+ krs).
    residential_kohdeluokka: list[int] = Field(default_factory=lambda: [42210, 42211, 42212])
    # For A3 subcategory: also keep 150 m from commercial, holiday, and industrial buildings.
    # 42220-42222=liike-/julkinen, 42230-42232=lomarakennus, 42240-42242=teollinen.
    # Agricultural/storage (42260-42262) are excluded — they are part of the farm operation.
    a3_additional_kohdeluokka: list[int] = Field(
        default_factory=lambda: [42220, 42221, 42222, 42230, 42231, 42232, 42240, 42241, 42242]
    )
    offset_enabled: bool = True
    max_area_loss_pct: float = Field(default=30.0, ge=0, le=100)


class PolygonConfig(BaseModel):
    edge_buffer_m: float = Field(default=0.0, ge=0)
    # Close small gaps between adjacent parcels via morphological closing
    # (buffer +d then -d).  Gaps up to 2×gap_fill_m wide are bridged.
    # Corners are rounded by up to gap_fill_m — acceptable for survey polygons.
    # Typical Finnish field cadastral gaps: 0–5 m.  Set 0 to disable.
    gap_fill_m: float = Field(default=0.0, ge=0)
    simplify_tolerance_m: float = Field(default=1.0, ge=0)
    multipart_policy: Literal["split", "largest", "review"] = "review"
    hole_policy: Literal["review", "fill", "clip"] = "fill"


class CacheConfig(BaseModel):
    cache_dir: str = "cache"
    tile_size_m: int = Field(default=1000, gt=0)
    dem_ttl_days: int = Field(default=365, gt=0)
    buildings_ttl_days: int = Field(default=180, gt=0)
    offline: bool = False


class OutputConfig(BaseModel):
    output_dir: str = "output"


class ZonesConfig(BaseModel):
    # Confirmed open API (no auth): Traficom UAS zones for Finland.
    # Fetched automatically and cached locally; no manual download needed.
    api_url: str = (
        "https://eservices.traficom.fi/Ilmatilasovellus/api/uas-reservations/json?lang=fi"
    )
    # Optional path to a local override file (offline use / custom zones).
    # If set, the API is not called and this file is used instead.
    zones_file: str = ""
    # Re-fetch the dump if the cached copy is older than this many days.
    # The source is a periodic static export of permanent zones (not live),
    # so 7–30 days is appropriate.  Temporary restrictions are not in this
    # dataset — those require a manual NOTAM check on the day.
    max_age_days: int = Field(default=14, gt=0)


class ParcelsConfig(BaseModel):
    # Year of the LPIS layer to use. Defaults to current_year - 1 because LPIS
    # data covers completed agricultural seasons: in any given year N the most
    # recent published layer is N-1.  Override in config if a specific year is needed.
    # Available years on the service: 2020–2025 (verify via GetCapabilities).
    lpis_year: int = Field(default_factory=lambda: datetime.date.today().year - 1)
    # WFS page size; GeoServer default cap is 1000.
    page_size: int = Field(default=1000, gt=0)
    # Request timeout in seconds.
    timeout_s: int = Field(default=60, gt=0)


class AppConfig(BaseModel):
    flight: FlightConfig
    home_safety: HomeSafetyConfig = Field(default_factory=HomeSafetyConfig)
    polygon: PolygonConfig = Field(default_factory=PolygonConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    parcels: ParcelsConfig = Field(default_factory=ParcelsConfig)
    zones: ZonesConfig = Field(default_factory=ZonesConfig)


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
