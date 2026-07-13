"""Job and global configuration model."""

from __future__ import annotations

import datetime
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator


# M3M (Mavic 3 Multispectral) RGB camera constants — confirmed from image EXIF (2026-05-29).
# Note: the drone model is M3M, not M3E; WPML drone enum 77 is correct for both.
# Kept as module-level constants for backward compatibility; prefer DroneConfig.
M3E_FOCAL_LENGTH_MM = 12.3
M3E_PIXEL_PITCH_UM = 3.3
M3E_IMAGE_WIDTH_PX = 5280
M3E_IMAGE_HEIGHT_PX = 3956


class DroneConfig(BaseModel):
    """Camera and WPML identifiers for one drone + payload combination."""

    name: str = Field(
        description="Short slug used on the CLI (e.g. 'm3m', 'm300-p1-24')"
    )
    label: str = Field(description="Human-readable name shown in summaries")

    # WPML identifiers — written into template.kml and waylines.wpml.
    # Confirmed sources noted per profile in config.example.toml.
    drone_enum: int
    drone_sub_enum: int = 0
    payload_enum: int
    payload_sub_enum: int = 0
    payload_position_index: int = 0

    # Camera optics — used for GSD ↔ AGL height conversion.
    focal_length_mm: float = Field(gt=0)
    pixel_pitch_um: float = Field(gt=0)
    image_width_px: int = Field(gt=0)
    image_height_px: int = Field(gt=0)

    # WPML imageFormat value written into payloadParam.
    # M3M: "visable,narrow_band" (RGB + multispectral simultaneously).
    # Everything else: "wide" (single RGB lens).
    image_format: str = "wide"

    # Battery planning — flag jobs that exceed this estimated flight time.
    battery_minutes: float = Field(default=28.0, gt=0)

    # Minimum time (s) between consecutive shutter triggers — the SD card write
    # bottleneck.  Used by auto_speed() to derive the maximum safe strip speed.
    # Calibrate by back-calculating from DJI Pilot 2's auto-speed at a known
    # altitude: interval = (1 - overlap) * altitude * sensor_h/focal / speed.
    # M3M: 2.38 s (5-file MS+RGB burst, ~49 MB; calibrated from 8.9 m/s at 100 m).
    min_capture_interval_s: float = Field(default=2.0, gt=0)

    @property
    def sensor_h_mm(self) -> float:
        """Sensor height in mm (along-flight, front-overlap axis)."""
        return self.image_height_px * self.pixel_pitch_um / 1000.0

    @property
    def sensor_w_mm(self) -> float:
        """Sensor width in mm (across-flight, side-overlap axis)."""
        return self.image_width_px * self.pixel_pitch_um / 1000.0

    def height_from_gsd(self, gsd_cm: float) -> float:
        """Return required AGL height (m) for a given GSD (cm/px)."""
        return (gsd_cm / 100) * self.focal_length_mm / (self.pixel_pitch_um / 1000)

    def gsd_from_height(self, height_m: float) -> float:
        """Return achievable GSD (cm/px) at a given AGL height (m)."""
        return height_m * self.pixel_pitch_um / (self.focal_length_mm * 10)

    def auto_speed(self, altitude_m: float, overlap_front_pct: int) -> float:
        """Return the maximum safe strip speed (m/s) at a given altitude and front overlap.

        Speed is limited by how quickly the camera can write each capture burst
        (min_capture_interval_s).  At lower altitudes the along-track footprint
        shrinks, so the drone must slow down to maintain the required overlap.
        """
        sensor_h_m = self.image_height_px * self.pixel_pitch_um * 1e-6
        footprint_m = altitude_m * sensor_h_m / (self.focal_length_mm * 1e-3)
        trigger_m = (1 - overlap_front_pct / 100) * footprint_m
        return trigger_m / self.min_capture_interval_s


def _default_drones() -> list[DroneConfig]:
    """Load built-in drone profiles from drones.toml (shipped alongside this file)."""
    toml_path = Path(__file__).with_name("drones.toml")
    with open(toml_path, "rb") as f:
        data = tomllib.load(f)
    return [DroneConfig(**entry) for entry in data["drone"]]


class FlightConfig(BaseModel):
    target_gsd_cm: float = Field(
        gt=0, description="Target GSD in cm/px (authoritative)"
    )
    max_height_agl_m: float = Field(default=110.0, le=120.0)
    agl_safety_margin_m: float = Field(default=10.0, ge=0)
    takeoff_security_height_m: float = Field(default=50.0)
    rth_height_m: float = Field(default=80.0)
    transitional_speed_ms: float = Field(default=15.0)
    overlap_front_pct: int = Field(default=80, ge=0, le=100)
    overlap_side_pct: int = Field(default=70, ge=0, le=100)
    # Speed along mapping strips (m/s).  None (default) = auto-calculate from
    # the active drone's min_capture_interval_s, altitude, and front overlap.
    # Set a fixed value here only to override the auto calculation.
    auto_flight_speed_ms: float | None = Field(default=None, gt=0)
    # Extra margin around the survey polygon bbox in the terrain-follow DSM (metres).
    # Covers the RTH path and takeoff/landing area outside the survey polygon.
    dsm_margin_m: int = Field(default=300, ge=0)
    finish_action: str = Field(default="goHome")
    rc_lost_action: str = Field(default="goBack")
    # Advanced (obstacle-aware) waypoint mode
    advanced_mode: bool = Field(default=False)
    adv_min_height_m: float = Field(default=30.0, ge=0)
    # Maximum altitude in adaptive sweep mode.  None (default) means the drone
    # flies at the GSD-derived height when clear of obstacles.  Set explicitly
    # to allow flying higher than the GSD target in open areas (coarser GSD but
    # fewer strips and faster coverage).
    adv_max_height_m: float | None = Field(default=None, ge=0)
    adv_powerline_clearance_m: float = Field(default=70.0, ge=0)
    adv_slope_f: float = Field(default=0.30, gt=0)
    adv_min_dip_m: float = Field(default=40.0, ge=0)

    @property
    def derived_flight_height_m(self) -> float:
        """AGL height for target GSD using M3E/M3M constants (backward-compat).

        Prefer DroneConfig.height_from_gsd() when a specific drone is selected.
        """
        return (
            (self.target_gsd_cm / 100)
            * M3E_FOCAL_LENGTH_MM
            / (M3E_PIXEL_PITCH_UM / 1000)
        )


class HomeSafetyConfig(BaseModel):
    operating_subcategory: Literal["A2", "A3"] = "A2"
    # A2: buffer ≈ flight height AGL (EU reg: ≥ flight height from people).
    #     The CLI --subcategory A2 flag auto-derives this from flight height.
    #     Set explicitly here if running pipeline programmatically.
    # A3: fixed 150 m from residential/commercial/industrial/recreational areas.
    home_buffer_m: float = Field(default=150.0, ge=0)
    # MML Maastotietokanta kohdeluokka codes treated as residential for keep-out.
    # Confirmed codes: 42210=asuinrakennus (point), 42211 (1-2 krs), 42212 (3+ krs).
    residential_kohdeluokka: list[int] = Field(
        default_factory=lambda: [42210, 42211, 42212]
    )
    # For A3 subcategory: also keep 150 m from commercial, holiday, and industrial buildings.
    # 42220-42222=liike-/julkinen, 42230-42232=lomarakennus, 42240-42242=teollinen.
    # Agricultural/storage (42260-42262) are excluded — they are part of the farm operation.
    a3_additional_kohdeluokka: list[int] = Field(
        default_factory=lambda: [
            42220,
            42221,
            42222,
            42230,
            42231,
            42232,
            42240,
            42241,
            42242,
        ]
    )
    # Distance from the survey polygon boundary used to include buildings in the
    # homes KML.  None (default) means 2× home_buffer_m, which gives a visible
    # safety margin around the keep-out zone.  Set explicitly to override.
    home_include_buffer_m: float | None = Field(default=None, ge=0)
    # Radius (m) of the yellow informational circle drawn around each building
    # in the HTML map preview.  None (default) means 3× derived flight height
    # (the "3:1 horizontal rule" often used for risk assessment).
    # CLI: --preview-radius <metres>
    preview_radius_m: float | None = Field(default=None, ge=0)
    # Radius (m) of the VLOS circle shown around the takeoff/landing marker in
    # the browser UI.  Displayed as two concentric dashed rings (full + half).
    vlos_range_m: float = Field(default=300.0, gt=0)
    offset_enabled: bool = True
    max_area_loss_pct: float = Field(default=30.0, ge=0, le=100)

    @property
    def resolved_include_buffer_m(self) -> float:
        """Effective building-inclusion buffer distance (metres).

        Defaults to 2× home_buffer_m when home_include_buffer_m is not set
        explicitly.  Use this everywhere instead of repeating the ternary.
        """
        return (
            self.home_include_buffer_m
            if self.home_include_buffer_m is not None
            else 2.0 * self.home_buffer_m
        )


class PolygonConfig(BaseModel):
    edge_buffer_m: float = Field(default=0.0, ge=0)
    # Close small gaps between adjacent parcels via morphological closing
    # (buffer +d then -d).  Gaps up to 2×gap_fill_m wide are bridged.
    # Corners are rounded by up to gap_fill_m — acceptable for survey polygons.
    # Typical Finnish field cadastral gaps: 0–5 m.  Set 0 to disable.
    gap_fill_m: float = Field(default=0.0, ge=0)
    # Expand (+) or contract (−) the survey polygon relative to the parcel
    # boundary, applied after gap-fill and before keep-out subtraction.
    # Positive: push the survey area outward beyond the cadastral line.
    # Negative: pull it inward (useful to leave a margin inside the field edge).
    # Degenerate shapes from inward contraction (holes, lost pieces) are handled
    # by hole_policy / multipart_policy as normal.  Units: metres.
    survey_offset_m: float = Field(default=0.0)
    # Vertex simplification.
    # simplify_mode="fixed": apply simplify_tolerance_m (Douglas-Peucker).
    # simplify_mode="auto":  binary-search for the largest tolerance that keeps
    #                        the vertex count ≤ auto_simplify_max_vertices.
    # Both modes intersect the result with the original to guarantee the
    # simplified polygon never exits the original parcel boundary.
    simplify_mode: Literal["fixed", "auto"] = "fixed"
    simplify_tolerance_m: float = Field(default=1.0, ge=0)
    auto_simplify_max_vertices: int = Field(default=15, ge=4)
    multipart_policy: Literal["split", "largest", "review"] = "review"
    hole_policy: Literal["review", "fill", "clip"] = "fill"


class PowerLinesConfig(BaseModel):
    # MTK only contains suurjännitejohto (110 kV+) overhead lines; no voltage attribute.
    # enabled=False skips fetching and keep-out entirely.
    enabled: bool = True
    # Keep-out buffer around overhead power lines (kohdeluokka 22312), metres.
    # Finnish aviation guidance recommends staying well clear of high-voltage lines.
    overhead_buffer_m: float = Field(default=30.0, ge=0)


class CacheConfig(BaseModel):
    cache_dir: str = "cache"
    tile_size_m: int = Field(default=1000, gt=0)
    dem_ttl_days: int = Field(default=365, gt=0)
    buildings_ttl_days: int = Field(default=180, gt=0)
    powerlines_ttl_days: int = Field(default=180, gt=0)
    pylons_ttl_days: int = Field(default=180, gt=0)
    parcels_ttl_days: int = Field(default=400, gt=0)
    properties_ttl_days: int = Field(default=400, gt=0)
    offline: bool = False
    max_cache_size_mb: int = Field(default=0, ge=0)


class OutputConfig(BaseModel):
    output_dir: str = "output"
    # Desktop-only filesystem actions (open file manager, export to an arbitrary
    # local path). Safe on a single-user localhost box; set False for any
    # multi-user / hosted deployment so those endpoints return 403.
    allow_local_fs: bool = True
    color_palette: list[str] = Field(
        default=[
            "#3b82f6",  # blue
            "#16a34a",  # green
            "#dc2626",  # red
            "#d97706",  # amber
            "#7c3aed",  # violet
            "#0891b2",  # cyan
            "#db2777",  # pink
            "#65a30d",  # lime
            "#ea580c",  # orange
            "#475569",  # slate
        ]
    )


class ZonesConfig(BaseModel):
    # Confirmed open API (no auth): Traficom UAS zones for Finland.
    # Fetched automatically and cached locally; no manual download needed.
    api_url: str = "https://eservices.traficom.fi/Ilmatilasovellus/api/uas-reservations/json?lang=fi"
    # Optional path to a local override file (offline use / custom zones).
    # If set, the API is not called and this file is used instead.
    zones_file: str = ""
    # Re-fetch the dump if the cached copy is older than this many days.
    # 1 day: the dump is regenerated frequently; daily refresh picks up new zones.
    # Temporary restrictions (NOTAMs) are NOT in this dataset regardless of TTL.
    max_age_days: int = Field(default=1, gt=0)
    # Expand the survey polygon by this many metres before the zone intersection
    # check so that zones nearby — but not overlapping — are also reported.
    check_buffer_m: float = Field(default=500.0, ge=0)


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


class PropertiesConfig(BaseModel):
    # MML kiinteistö OGC API Features (PalstanSijaintitiedot).
    # Uses the same MML_API_KEY as elevation and buildings.
    # OGC API page size (limit= parameter).
    page_size: int = Field(default=100, gt=0)
    # Request timeout in seconds.
    timeout_s: int = Field(default=60, gt=0)


class TrackedSatellite(BaseModel):
    # Permanent NORAD Catalog Number — orbital data is fetched from CelesTrak by id.
    norad_id: int = Field(gt=0)
    # UI display name (free text; not used to look up orbital data).
    name: str
    # Toggle without removing the entry.
    enabled: bool = True


class SatellitesConfig(BaseModel):
    # Earth-observation satellites whose overpasses are computed for the job grid.
    # Defaults: the optical Sentinel-2 trio + Landsat 8/9 (good for agriculture).
    # NORAD ids verified against CelesTrak 2026-06-15.
    tracked: list[TrackedSatellite] = Field(
        default_factory=lambda: [
            TrackedSatellite(norad_id=40697, name="Sentinel-2A"),
            TrackedSatellite(norad_id=42063, name="Sentinel-2B"),
            TrackedSatellite(norad_id=60989, name="Sentinel-2C"),
            TrackedSatellite(norad_id=39084, name="Landsat 8"),
            TrackedSatellite(norad_id=49260, name="Landsat 9"),
        ]
    )
    # Path to the Sentinel-2 MGRS tiling-grid GeoJSON (tile id in the "Name"
    # property). ~20 MB — NOT bundled. Download from https://zenodo.org/records/10998972
    # and place at the path below. If missing, overpass features degrade gracefully.
    grid_file: str = "data/sentinel2_tiling_grid_wgs84.geojson"
    # CelesTrak OMM (Orbit Mean-Elements Message) JSON endpoint. {catnr} is the NORAD id.
    omm_url: str = (
        "https://celestrak.org/NORAD/elements/gp.php?CATNR={catnr}&FORMAT=json"
    )
    # Only count overpasses whose peak elevation exceeds this (near-nadir capture).
    min_elevation_deg: float = Field(default=60.0, ge=0, le=90)
    # How many days ahead to search for overpasses.
    days_ahead: int = Field(default=14, gt=0, le=30)
    # Re-fetch OMM if the cached copy is older than this. Sun-synchronous EO orbits
    # are stable, so multi-day-old elements are fine for a "which day" listing.
    omm_max_age_days: int = Field(default=3, gt=0)
    # CelesTrak request timeout (seconds).
    timeout_s: int = Field(default=30, gt=0)


class WeatherConfig(BaseModel):
    # Weather forecast for the map-view day-slot bar, qualifying satellite overpasses.
    # "open-meteo": keyless JSON, daily forecast up to 16 days (recommended).
    # "fmi": Finnish Meteorological Institute Open Data WFS (added later).
    provider: Literal["open-meteo", "fmi"] = "open-meteo"
    # How many forecast days to request. Open-Meteo serves up to 16; the bar renders
    # however many the source actually returns.
    forecast_days: int = Field(default=14, gt=0, le=16)
    # Re-fetch a cached forecast if older than this. Forecasts update through the day,
    # so a few hours keeps the bar fresh without hammering the API.
    cache_max_age_hours: int = Field(default=3, gt=0)
    open_meteo_url: str = "https://api.open-meteo.com/v1/forecast"
    # FMI keyless WFS download endpoint (used when provider = "fmi").
    fmi_wfs_url: str = "https://opendata.fmi.fi/wfs"
    timeout_s: int = Field(default=30, gt=0)
    # Max wind (m/s) you'll fly a mapping mission in. A day counts as a "golden"
    # match (highlighted) when its daytime wind is at or below this AND a clear-sky
    # satellite pass falls on the same day. Set null to disable golden highlighting.
    drone_wind_limit_ms: float | None = 10.0
    # Daytime window (local time) used for the forecast bar: temperature, wind, and
    # weather are aggregated over these hours only (night is irrelevant for mapping),
    # and satellite passes outside this window are hidden behind a count marker.
    daytime_start_h: int = Field(default=6, ge=0, le=23)
    daytime_end_h: int = Field(default=18, ge=1, le=24)
    # A daytime satellite pass on a day whose daytime cloud cover is at or below this
    # percentage is flagged as a "clear window" (good chance of a usable optical image)
    # and highlighted in the forecast bar.
    clear_sky_max_cloud_pct: int = Field(default=30, ge=0, le=100)


class RtkNetworkConfig(BaseModel):
    """One NTRIP caster whose base stations are shown as RTK candidates.

    The username/password are what the pilot enters into DJI Pilot 2 (custom
    network RTK) — they are also sent as HTTP basic auth when fetching the
    caster's sourcetable, which public casters simply ignore.
    """

    name: str = Field(description="Short label shown in the UI/PDF (e.g. 'rtk2go')")
    # "host:port" or "http://host:port". Sourcetable is fetched from the root path.
    caster_url: str
    # rtk2go requires a registered email as username (password is ignored);
    # centipede is open with centipede/centipede.
    username: str = ""
    password: str = ""
    # Map dot / legend colour for this network's stations. Leave unset to
    # auto-pick a distinct bright hue from `_RTK_PALETTE` by position (see
    # RtkConfig._assign_network_colors) so networks are told apart without
    # hand-picking. Set an explicit hex to override. Kept bright on purpose —
    # the RTK stat mode dims the basemap and muted hues get lost.
    color: str | None = None
    # Toggle without removing the entry.
    enabled: bool = True


# Bright, well-separated hues (lost-area palette family) auto-assigned to
# networks that don't set an explicit `color`. Cycles if there are more
# networks than entries.
_RTK_PALETTE: tuple[str, ...] = (
    "#4ade80",  # green
    "#facc15",  # yellow
    "#38bdf8",  # sky
    "#f472b6",  # pink
    "#a78bfa",  # violet
    "#fb7185",  # rose
    "#a3e635",  # lime
    "#fb923c",  # orange — last, too close to the amber launch-site dots
)


def _default_rtk_networks() -> list[RtkNetworkConfig]:
    return [
        RtkNetworkConfig(
            name="rtk2go",
            caster_url="http://rtk2go.com:2101",
            username="you@example.com",
            password="none",
        ),
        RtkNetworkConfig(
            name="centipede",
            caster_url="http://crtk.net:2101",
            username="centipede",
            password="centipede",
        ),
    ]


class RtkConfig(BaseModel):
    # NTRIP casters polled for base stations. Like [[drones]], adding any
    # [[rtk.networks]] entry in config.toml replaces the whole default list.
    networks: list[RtkNetworkConfig] = Field(default_factory=_default_rtk_networks)
    # Usable RTK baseline radius drawn around each station (dashed circle) and
    # used as the "nearby" threshold in popups/PDF. ~20 km is the common guidance
    # for a fixed-quality RTK solution; accuracy degrades with distance.
    circle_radius_km: float = Field(default=20.0, gt=0)
    # Stations farther than this from every job are dropped from API/PDF payloads
    # (rtk2go alone lists thousands of stations worldwide).
    search_radius_km: float = Field(default=100.0, gt=0)
    # Sourcetables list only currently-online stations, so this TTL is a liveness
    # trade-off: community bases churn, keep it hours not days.
    cache_max_age_hours: int = Field(default=6, gt=0)
    timeout_s: int = Field(default=20, gt=0)

    @model_validator(mode="after")
    def _assign_network_colors(self) -> "RtkConfig":
        """Give every colour-less network a distinct palette hue by position.

        Runs after networks are parsed, so a config that omits `color` (or omits
        it on some entries) still renders each network in a different colour
        instead of collapsing to one. Explicit colours are left untouched; the
        palette index counts only the auto-assigned ones so they stay distinct.
        """
        auto_i = 0
        for net in self.networks:
            if not net.color:
                net.color = _RTK_PALETTE[auto_i % len(_RTK_PALETTE)]
                auto_i += 1
        return self


class AppConfig(BaseModel):
    flight: FlightConfig
    home_safety: HomeSafetyConfig = Field(default_factory=HomeSafetyConfig)
    polygon: PolygonConfig = Field(default_factory=PolygonConfig)
    cache: CacheConfig = Field(default_factory=CacheConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    parcels: ParcelsConfig = Field(default_factory=ParcelsConfig)
    properties: PropertiesConfig = Field(default_factory=PropertiesConfig)
    zones: ZonesConfig = Field(default_factory=ZonesConfig)
    powerlines: PowerLinesConfig = Field(default_factory=PowerLinesConfig)
    satellites: SatellitesConfig = Field(default_factory=SatellitesConfig)
    weather: WeatherConfig = Field(default_factory=WeatherConfig)
    rtk: RtkConfig = Field(default_factory=RtkConfig)
    # Drone / payload profiles.  The built-in list covers common DJI mapping drones.
    # Add [[drones]] entries in config.toml to extend or override.
    default_drone: str = "m3m-ms"
    drones: list[DroneConfig] = Field(default_factory=_default_drones)

    @model_validator(mode="after")
    def _check_default_drone(self) -> "AppConfig":
        names = [d.name for d in self.drones]
        if self.default_drone not in names:
            raise ValueError(
                f"default_drone '{self.default_drone}' not found. Available: {names}"
            )
        return self

    def active_drone(self) -> DroneConfig:
        """Return the drone profile selected by default_drone."""
        return next(d for d in self.drones if d.name == self.default_drone)


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


# Fields not written back by save_config() — either internal/structural or managed
# via config.toml directly (drone profiles, MML API URL, cache grid geometry).
_SAVE_SKIP: dict[str, set[str]] = {
    "home_safety": {"residential_kohdeluokka", "a3_additional_kohdeluokka"},
    "zones": {"api_url"},
    "cache": {"tile_size_m", "cache_dir"},
    "satellites": {"omm_url"},
    "weather": {"open_meteo_url", "fmi_wfs_url"},
    # The caster list (urls/credentials/auto-assigned colors) is managed in
    # config.toml directly, like [[drones]] — never rewrite it on a settings save
    # (that would freeze the auto-picked network colours and materialise the
    # placeholder rtk2go credentials into the file).
    "rtk": {"networks"},
}

_SAVE_SECTIONS = [
    "flight",
    "home_safety",
    "polygon",
    "zones",
    "cache",
    "output",
    "parcels",
    "properties",
    "powerlines",
    "satellites",
    "weather",
    "rtk",
]


def save_config(config: AppConfig, path: Path) -> None:
    """Write editable settings back to *path* (config.toml) using tomli-w.

    Reads the current file first so that un-managed keys (drone profiles, TOML
    comments structure) are preserved in the raw dict, then overwrites only the
    sections the Settings UI controls.  None values are removed from the dict
    (TOML has no null type; omitting the key lets Pydantic use the field default
    on next load, which is also None for Optional fields).
    """
    import tomli_w as _tomli_w

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    raw["default_drone"] = config.default_drone

    for section_id in _SAVE_SECTIONS:
        section_obj = getattr(config, section_id)
        skip = _SAVE_SKIP.get(section_id, set())
        if section_id not in raw:
            raw[section_id] = {}
        for key, value in section_obj.model_dump(mode="python").items():
            if key in skip:
                continue
            if value is None:
                raw[section_id].pop(key, None)
            else:
                raw[section_id][key] = value

    with open(path, "wb") as f:
        _tomli_w.dump(raw, f)
