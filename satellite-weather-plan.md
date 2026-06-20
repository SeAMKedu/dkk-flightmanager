# Satellite overpass + weather forecast bar — implementation plan

A top-center bar in the **job list map view** showing, per day, the date, a weather
forecast, and which tracked Earth-observation satellites pass over the job grid(s)
that day. Mirrors the battery/flight-time timeline already pinned at the bottom of
the map.

## Decisions (confirmed with user 2026-06-15)

- **Weather:** Open-Meteo now (keyless JSON, daily forecast up to 16 days, WMO
  weathercodes that map to our icons, daily max wind). **FMI Open Data WFS** added
  later as a config-switchable alternate / FI-official source.
- **Orbit propagation:** `skyfield` (SGP4, `EarthSatellite.from_omm()`,
  `find_events()`). New dependency (pulls `numpy`, `sgp4`).
- **Slot count:** dynamic — driven by how many forecast days the weather source
  returns (Open-Meteo ≈16). Config caps it; bar renders whatever is available.
- **Golden-window flag** (today + cloud<20% + wind<drone limit highlight):
  **deferred** to a later phase.
- **Grid file:** the ~20 MB Sentinel-2 MGRS tiling-grid GeoJSON is **not bundled**.
  User places it in `data/sentinel2_tiling_grid_wgs84.geojson` (default) and/or sets
  `satellites.grid_file`. If unset/missing, the satellite panel degrades gracefully
  ("grid file not configured"). Source for docs/README:
  https://zenodo.org/records/10998972 . Confirmed schema: `FeatureCollection`,
  56,984 `MultiPolygon` features, MGRS tile id in the **`Name`** property (e.g. `01CCV`).
- **Tile selection:** take the centroid of all (selected) jobs in the current
  folder, look up which MGRS tile it falls in, use that tile's center for overpass
  + weather. Multi-square handling (per-grid grouping, drawing grid extents) is
  **deferred** to a last phase.
- **Tracked satellites:** a list of NORAD ids in config (display name + enabled +
  id). Seeded with good agricultural Sentinel ids.

## Icon note

No icon library is bundled. UI uses hand-authored inline SVG `<symbol>`s
(Lucide/Feather style, `viewBox="0 0 24 24"`, `currentColor`) in `ui.html`. Weather
icons will be added the same way as `ic-wx-*` symbols (copied from Lucide:
sun, cloud, cloud-sun, cloud-rain, cloud-snow, cloud-lightning, wind, cloud-fog),
keyed off WMO weathercodes.

---

## Phase 1 — Satellite overpass backend (no UI)

**New module `satellites.py`:**
- `SatellitesConfig` in `config.py` (registered on `AppConfig`):
  - `tracked: list[TrackedSat]` where `TrackedSat = {norad_id:int, name:str, enabled:bool}`.
    Seed defaults: Sentinel-2A (40697), Sentinel-2B (42564), Sentinel-2C (60989),
    Landsat 8 (39084), Landsat 9 (49260). (Verify ids before flight use.)
  - `grid_file: str = ""` — path to Sentinel-2 MGRS grid GeoJSON.
  - `omm_url: str` — CelesTrak OMM endpoint template (by CATNR, JSON format).
  - `min_elevation_deg: float = 60.0` — near-nadir threshold.
  - `days_ahead: int = 14` — overpass search window.
  - `omm_max_age_days: int = 3` — OMM cache TTL. Sun-synchronous EO orbits are very
    stable; multi-day-old elements are fine for a "which day" overpass listing, and
    the user wants infrequent refresh. (Was 12 h — relaxed to days.)
- `fetch_omm(norad_ids, cfg, cache_dir)` — fetch OMM JSON per id from CelesTrak,
  cache one file per id under `<cache_dir>/satellites/omm_<id>.json` with mtime TTL.
  Reuse the zones.py fetch/cache idiom (`net_stats`, atomic write, TTL check).
- `load_grid(grid_path)` — lazy-load GeoJSON once; build a shapely STRtree index of
  the 56,984 tile MultiPolygons keyed by the `Name` property. Cache the built index
  in a module-level var (the file is large; load only on first use).
- `tile_for_point(lat, lon)` / `tile_center(tile_id)` — point-in-tile lookup + centroid.
- `compute_overpasses(tile_centers, sats, *, days_ahead, min_elev, start=now_utc)`
  → `list[Overpass]` where `Overpass = {norad_id, name, tile_id, peak_utc, max_elev_deg}`.
  Uses skyfield `wgs84.latlon(...)` observer + `satellite.find_events()`, filters to
  culmination events above `min_elev`.
- Pure-ish; network only in `fetch_omm`.

**CLI:** `flightmanager satellites --folder <name>` (or `--parcels/--bbox`) prints the
upcoming overpass table. Makes Phase 1 testable with no UI.

**Tests:** grid lookup with a tiny fixture GeoJSON; overpass computation against a
pinned OMM fixture (deterministic with a fixed `start`); config defaults. Network
fetch behind `-m integration`.

**Deps:** add `skyfield>=1.49` to `pyproject.toml`.

## Phase 2 — Weather backend + combined endpoint

**New module `weather.py`:**
- `WeatherConfig` in `config.py`: `provider: "open-meteo" | "fmi" = "open-meteo"`,
  `forecast_days: int = 14` (clamped to provider max), `cache_max_age_hours: int = 3`,
  `open_meteo_url`, (later) `fmi_wfs_url`, optional `drone_wind_limit_ms` for the
  deferred golden window.
- `fetch_forecast(lat, lon, days, cfg, cache_dir)` → `list[DayWeather]` where
  `DayWeather = {date, weathercode, t_min_c, t_max_c, wind_max_ms, precip_mm, ...}`.
  Open-Meteo daily endpoint; cache per rounded lat/lon+day under
  `<cache_dir>/weather/` with short TTL (respect rate limits, ~3 h).
- `_open_meteo_adapter` now; `_fmi_adapter` stub for the later phase. Provider chosen
  by config; same `DayWeather` shape regardless.
- WMO weathercode → icon-key + label mapping table lives here (shared with frontend
  via the endpoint, which returns the resolved `icon` key).

**New endpoint `GET /api/forecast`** (in `routers/management.py` or a small new
router): query by `folder` and/or `paths`. Server computes job centroid → tile →
tile center, calls `compute_overpasses` + `fetch_forecast`, merges into day-slots:
```
{ generated_at, tile_ids:[...], grid_ok:bool,
  days:[ {date, weather:{icon,label,t_min,t_max,wind_max,precip},
          satellites:[{name, norad_id, peak_local, max_elev_deg, tile_id}] } ] }
```
**Caching cadence (per user — recompute rarely):**
- **Overpasses** depend only on (tile set, OMM epoch, calendar day). Recompute when:
  the folder's tile set changes, OMM cache expires (`omm_max_age_days`), or the day
  rolls over. Persist a small per-folder result under
  `<output>/<folder>/.forecast_cache.json` keyed by a fingerprint of
  `(sorted tile ids, omm mtime bucket, date)`; serve from it otherwise — so opening a
  folder whose jobs haven't moved does **no** recomputation.
- **Weather** has its own short TTL (~3 h) since forecasts update through the day.
- The endpoint composes the two: weather may refresh while overpasses stay cached.
- Layered in-process memoization keyed by (tile set, day) keeps the bar instant on
  re-open within a session.

**Attribution** (returned in the endpoint payload and added to `manifest.py`'s
`_CC_BY` templates / job manifests where relevant):
- Open-Meteo: "Weather data by Open-Meteo.com" (CC BY 4.0).
- FMI (later): "Source: Finnish Meteorological Institute" / "Ilmatieteen laitos"
  (CC BY 4.0).
- Sentinel-2 grid: cite the Zenodo record (https://zenodo.org/records/10998972).
The forecast bar shows a small attribution line/tooltip; cached weather files store
their source + fetch time.

**Tests:** Open-Meteo adapter against a recorded JSON fixture; endpoint merge logic;
weathercode→icon mapping.

## Phase 3 — Forecast bar UI (map view)

- **`forecast-bar.js`** (mirrors `battery-timeline.js`): a `#forecast-bar` div pinned
  top-center of `#map` (CSS mirrors `#battery-timeline` but `top:28px`). Renders an
  SVG row of day slots; each slot: date header, weather `<use href="#ic-wx-*">` icon
  + temp/wind, and satellite badges (dots/initials) for sats passing that day.
  Tooltip/hover gives exact local overpass times + elevation.
- **Weather icon symbols** added to `ui.html` defs (`ic-wx-clear`, `-cloudy`,
  `-partly`, `-rain`, `-snow`, `-storm`, `-wind`, `-fog`).
- **CSS** `#forecast-bar` in `map.css`.
- **Hook into `map-view.js`:** on entering map view (and on folder/selection change),
  fetch `/api/forecast` for the current folder/selection and render; hide on exit.
  Show a quiet placeholder when `grid_ok` is false.

**Verify** with preview tool on port 8766 per CLAUDE.md workflow.

## Phase 4 — Later / polish (deferred)

- **FMI adapter** wired behind `weather.provider = "fmi"`.
- **Golden-window** flag: backend boolean (today + cloud<20% + wind<drone limit) +
  UI highlight on the matching slot.
- **Multi-grid-square mode:** when jobs span >1 MGRS tile, group the stat panel per
  tile and optionally draw tile extents on the map.
- Overpass time-of-day shown in slots if layout has room.

---

## Files touched (by phase)

- P1: `config.py`, new `satellites.py`, `cli.py`, `config.example.toml`, `README.md`,
  `pyproject.toml`, `tests/`.
- P2: `config.py`, new `weather.py`, `routers/` (+ wire in `server.py`),
  `config.example.toml`, `tests/`.
- P3: `templates/ui.html`, `templates/map.css`, new `templates/js/forecast-bar.js`,
  `templates/js/map-view.js`.
- P4: `weather.py`, `routers/`, `forecast-bar.js`, `map-view.js`.
