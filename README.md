![Datakasvukunto](images/logo_dkk.png)

# dkk-flightmanager

> **Work in progress** — this is an internal research tool under active development. It is not yet polished or packaged for general use. Expect rough edges, breaking changes, and incomplete documentation.

DJI terrain-following mapping job generator for Finnish agricultural field parcels.

`dkk-flightmanager` is a planning tool for drone mapping surveys over Finnish farmland. Identify the survey area by pasting *peruslohkotunnus* field parcel IDs (Ruokavirasto), *kiinteistötunnus* cadastral property IDs, a bounding box, or a polygon drawn directly on the map — the tool fetches field boundaries, 2 m terrain elevation, building footprints, and high-voltage power line geometry from National Land Survey of Finland (MML) open data APIs, checks Traficom UAS restriction zones, and writes a ready-to-fly DJI Pilot 2 mapping job. A built-in browser UI handles everything from parcel lookup and polygon editing to flight parameter tuning and batch job creation for large parcel sets.

All underlying data — field boundaries (Ruokavirasto *Peltolohkorekisteri*), terrain elevation, buildings, and high-voltage power lines (MML *Maastotietokanta*), cadastral geometry (MML *Kiinteistötietojärjestelmä*), and UAS restriction zones (Traficom) — is sourced from free Finnish open data APIs, with attribution recorded in every manifest. Only an MML API key (free) is required.

Output files written per job:

| File | Description |
|---|---|
| `<name>.kmz` | WPML mapping route with embedded terrain-follow DSM — import into DJI Pilot 2 |
| `<name>_dsm.tif` | Terrain-follow DSM (also embedded in the KMZ, kept separately as a backup) |
| `<name>_homes.kml` | Building pins — import as a Pilot 2 custom map layer |
| `manifest.json` | Full provenance record with flight stats and safety flags |
| `job_params.json` | Browser UI save state (inputs, flight params, polygon params, simplified survey outline) — used to re-open the job for editing. Written atomically; `schema_version` tracks the on-disk format |
| `thumbnail.svg` | Small polygon thumbnail shown in the jobs panel |
| `run.log` | Structured log for this run |

## Contents

- [Architecture](#architecture)
- [Setup](#setup)
- [Browser UI](#browser-ui)
  - [Jobs panel](#jobs-panel)
  - [Defining the survey area](#defining-the-survey-area)
  - [Flight and polygon parameters](#flight-and-polygon-parameters)
  - [Preview](#preview)
  - [3D flight preview](#3d-flight-preview)
  - [Variable-altitude flight path](#variable-altitude-flight-path)
  - [Polygon editing](#polygon-editing)
  - [Map tools](#map-tools)
  - [Save and settings](#save-and-settings)
- [AI assistant integration (MCP)](#ai-assistant-integration-mcp)
- [CLI usage](#cli-usage)
  - [Specifying the survey area](#specifying-the-survey-area)
  - [All run options](#all-flightmanager-run-options)
  - [Polygon simplification](#polygon-simplification---simplify)
  - [Survey polygon offset](#survey-polygon-offset---offset)
  - [Disabling keep-out subtraction](#disabling-keep-out-subtraction---no-keepout)
  - [Drone profiles](#drone-profiles---drone)
  - [Strip speed (auto mode)](#strip-speed-auto-mode)
  - [Batch skeleton job creation](#batch-skeleton-job-creation-flightmanager-batch)
  - [Refreshing stale jobs](#refreshing-stale-jobs-flightmanager-refresh)
  - [Cache management](#cache-management)
- [Operator workflow](#operator-workflow)
- [Subcategory and keep-out distances](#subcategory-and-keep-out-distances)
- [Safety notes](#safety-notes)
- [Disclaimer](#disclaimer)
- [Attribution (CC-BY 4.0)](#attribution-cc-by-40)

---

## Architecture

> **Development note — AI-assisted ("vibe coded").** This application was built largely through iterative prompting of an LLM coding agent rather than line-by-line hand authoring; the architecture, tests, and this documentation were shaped that way. It is a working planning aid with a real test suite, but it has not had a line-by-line human security/safety audit. Review the safety-critical paths yourself (UAS-zone checks, building keep-out, altitude/terrain-follow) before relying on any output — and see the [Disclaimer](#disclaimer).

### Technology stack

| Layer | Technology |
|---|---|
| Backend | Python 3.10+, FastAPI, uvicorn |
| Geometry | Shapely, pyproj (EPSG:3067 internally, EPSG:4326 output) |
| Raster / DSM | rasterio, numpy |
| KMZ / WPML | lxml, zipfile |
| Tile cache | SQLite (1 km grid, atomic writes) |
| Job storage | Per-job JSON (`job_params.json` intent + `manifest.json` provenance), atomic writes, schema-versioned |
| Frontend | Vanilla JS (ES modules), Leaflet 1.9, Leaflet.draw, CesiumJS (CDN, 3D view) |
| Config | TOML (`config.toml`), Pydantic v2 models |
| CLI | Typer |

All geometry is kept in EPSG:3067 (Finnish national projection, metres) throughout the pipeline and reprojected to EPSG:4326 only for KMZ/KML output and the browser map.

### Pipeline

Every job runs through the same ordered stages in `pipeline.py:run_job()`:

```
Parcel / property fetch  →  Buildings fetch  →  Geometry processing
  →  DEM tiles  →  DSM mosaic  →  Zone check  →  KMZ  →  Homes KML
  →  Manifest
```

`run_preview()` runs only the first five stages (no file output) and returns a GeoJSON payload with a base64 DSM thumbnail. The browser calls preview on every parameter change; export (Save) runs the full pipeline.

Progress is streamed to the browser via Server-Sent Events (`GET /api/progress/{job_id}`). The pipeline runs in a single-worker `ThreadPoolExecutor`; a second job returns HTTP 409 while one is in flight.

### Data sources and caching

| Source | What it provides | Cache |
|---|---|---|
| Ruokavirasto WFS | Field parcel boundaries (*peruslohko*) | SQLite, 400-day TTL |
| MML OGC API | Cadastral property boundaries (*kiinteistö*) | SQLite, 400-day TTL |
| MML WMTS/WCS | 2 m DEM tiles (elevation model) | SQLite tile cache, 1 km grid |
| MML Maastotietokanta | Building footprints + power lines | SQLite tile cache, 1 km grid |
| Traficom REST | UAS restriction zones | SQLite, 1-day TTL |
| CelesTrak OMM | Satellite orbital elements (overpass forecast) | JSON per NORAD id, 3-day TTL |
| Open-Meteo | Daily weather forecast (map-view bar) | JSON per coordinate, 3-hour TTL |

Tile cache keys are `E{xmin}_N{ymin}` on the 1 km EPSG:3067 grid. All writes are atomic. Building and DEM tiles share the same SQLite database as parcel/property geometry records (`cache.py` and `geo_cache.py` share the same DB primitives).

### KMZ structure

The output KMZ is a ZIP containing:
- `wpmz/template.kml` — mission envelope (drone enum, camera, action defaults). `templateType=mapping2d` in simple mode; `templateType=waypoint` in advanced (variable-altitude) mode.
- `wpmz/waylines.wpml` — route waypoints. Simple mode: a stub generated by DJI Pilot 2 at import time. Advanced mode: explicit per-waypoint altitudes and speeds generated by `waylines_builder.py`.
- `<name>_dsm.tif` — terrain-follow DSM clipped to the survey polygon, embedded for Pilot 2 to link automatically.

### Advanced (variable-altitude) mode

When the Terrain tab's **Variable altitude** toggle is on, two additional modules run between route planning and KMZ generation:

1. **`obstacle_heights.py`** — computes one altitude per strip from building proximity (A2 1:1 rule relative to rooftop height) and power-line clearance requirements. A forward + backward slope filter ensures the profile is physically achievable given the drone's climb rate.
2. **`waylines_builder.py`** — converts the altitude profile into a WPML waypoint sequence with per-waypoint speed (`drone.auto_speed(h, overlap)`) and continuous-shooting action groups per strip.

### Frontend structure

The single-page UI (`templates/ui.html`) loads JavaScript as ES modules from `templates/js/`. Key modules:

| Module | Responsibility |
|---|---|
| `main.js` | Init, wires up all other modules |
| `api.js` | Thin `fetch` wrapper (`apiGet/apiPost/apiPatch/apiDelete`); single seam for JSON calls + error handling |
| `route-planner.js` | Calls `POST /api/route_estimate`, updates 2D overlay and notifies Cesium |
| `preview-runner.js` | Calls `POST /api/preview`, manages SSE progress |
| `job-ops.js` | Save / open / clone / delete job |
| `refresh-banner.js` | Detects stale jobs (`GET /api/refresh/scan`) and runs in-place recompute (`POST /api/refresh`) |
| `map-layers.js` | Leaflet layer management (route, coverage, buildings, zones, DSM) |
| `cesium-view.js` | CesiumJS 3D view; lazy-loads from CDN on first use |
| `tpl-modal.js` | Template Settings modal (overlap, safety, variable-altitude params) |
| `map-view.js` | Folder map view (job polygon overlays, statistics, timeline) |
| `launch-sites.js` | Renders launch-site dots, route line, and flight-announcement circles in map view |
| `stat-view.js` | Map-view statistics panel and modes (incl. MGRS-tiles overlay) |
| `forecast-bar.js` | Map-view satellite-overpass + weather forecast bar |
| `dirty-tracking.js` | Unsaved-change detection and confirmation prompts |

## Setup

### 1 — Create and activate a virtual environment

A virtual environment keeps the tool and its dependencies isolated from your system Python. You only do this once.

**macOS / Linux:**
```bash
python3 -m venv .venv        # create the virtual environment in .venv/
source .venv/bin/activate    # activate it (your prompt will change to show (.venv))
```

**Windows (Command Prompt):**
```bat
python -m venv .venv
.venv\Scripts\activate
```

> **Every time you open a new terminal** you need to activate again before using `flightmanager`:
>
> macOS / Linux: `source .venv/bin/activate`
> Windows: `.venv\Scripts\activate`
>
> Alternatively, call the script directly without activating:
>
> macOS / Linux: `.venv/bin/flightmanager run --name my-job --parcels 5241087453`
> Windows: `.venv\Scripts\flightmanager run --name my-job --parcels 5241087453`

### 2 — Install the tool

With the virtual environment active, install `flightmanager` and its dependencies:

```bash
pip install -e ".[dev]"
```

This registers the `flightmanager` command so you can run it by name from anywhere inside the project.

### 3 — Copy and edit the config files

```bash
cp config.example.toml config.toml   # edit flight params and buffer distances
cp .env.example .env                  # add your MML API key (see below)
```

Most flight and polygon settings can also be changed via the **⚙ Settings** panel in the browser UI (see below). Those changes are written directly back to `config.toml` — no manual file editing required.

### 4 — Add your MML API key

Open `.env` in a text editor and set your key:

```ini
MML_API_KEY=your_key_here
```

The MML API key is free — obtain one at https://www.maanmittauslaitos.fi/rajapinnat/api-avaimen-ohje

`flightmanager` reads the `.env` file automatically on startup, so no extra steps are needed. Ruokavirasto parcel data is open and requires no key.

### 5 — (Optional) Add the Sentinel-2 grid for satellite overpasses

The satellite-overpass feature maps your jobs onto Sentinel-2 MGRS tiles and computes upcoming near-nadir overpasses of the tracked Earth-observation satellites. This needs the Sentinel-2 tiling-grid GeoJSON, which is **not bundled** (~20 MB). Download it once and place it where `config.toml` expects it:

```bash
mkdir -p data
# Download sentinel2_tiling_grid_wgs84.geojson from:
#   https://zenodo.org/records/10998972
# and save it to data/sentinel2_tiling_grid_wgs84.geojson
```

Override the location with `grid_file` in the `[satellites]` section if you keep it elsewhere. Orbital elements are fetched from CelesTrak on demand and cached locally; no key is required. If the grid file is absent, overpass info is simply omitted — nothing else is affected. List overpasses from the CLI:

```bash
flightmanager satellites --folder my-group   # tiles + overpasses for a job folder
flightmanager satellites --point 62.79,22.84 # check a single lat,lon
```

## Browser UI

The recommended way to use the tool is the built-in browser UI:

```bash
flightmanager serve                         # opens http://localhost:8765 automatically
flightmanager serve --port 8080 --no-open   # custom port, no auto-open
```

The single-page Leaflet map interface is organised around four areas: the **Jobs panel** on the left, the **editor form** in the centre, the **map** on the right, and the **⚙ Settings** gear in the header.

### Jobs panel

The panel lists all saved jobs grouped into folders. Use the header buttons to create a new job (**＋ New Job**), batch-import IDs (**↓ Batch**), or add a folder (**＋ Folder**). If a folder name already exists the dialog stays open and shows an inline error. A filter input sits below. Click any card to re-open a job — the form and map restore instantly and a fresh preview runs automatically. The three-dot card menu offers **Open**, **Clone**, **Rename**, **Move to Folder**, and **Delete**. Collapse the panel with the `◄` tab.

The panel updates live — changes made by the CLI, MCP server, or another tab appear immediately. If the currently open job is modified externally, a blue notice offers **Reload** or **Dismiss**.

If any saved jobs were built by an older pipeline version or now have newer source data available, a green banner appears at the top of the panel (**"N jobs can be refreshed · Refresh all"**). **Refresh all** recomputes them in place (route, DSM, stats, KMZ, manifest) from cached tiles, preserving each job's geometry, and reports how many changed flight-ready/review status. Equivalent CLI command: `flightmanager refresh --all-stale`.

**Batch import:** click **↓ Batch**, paste parcel/property IDs (one per line, `#` comments ignored) or load a `.txt`/`.csv` file, pick a folder and optional param overrides, then click **Create N jobs**. Each ID becomes a skeleton job (polygon stored, no KMZ yet). Equivalent CLI command: `flightmanager batch`.

**Multi-select:** hover a card to reveal its checkbox. Select two or more to activate the toolbar: **Merge** (union polygons into a new job), **Export KML** (download selected jobs as a KML file), **PDF** (download a flight report: a one-page card for a single selected job, or a mission packet for several), **Google Maps** (open navigation waypoints in Google Maps), **Route rename** (prefix each selected job with `YYYYMMDD-NN-` in route order, skipping skeleton jobs — re-running on the same selection replaces the existing prefix), **Move**, or **Delete**.

**Map view:** click **Map** on any folder header to see all its job polygons on the map. Dash pattern encodes status: solid = flight-ready, long dashes = needs review, short dashes = untouched, dotted = unknown. Hover a polygon to open a popup with the job name, status, area, and two quick actions: **⊘ Skip** (exclude the job from route ordering and counting) and **Delete**. Skipped jobs render at low opacity. Hover the popup to keep it open; mouse out to dismiss. Click a polygon to select it; double-click to open the job for editing. Ctrl+click to multi-select. Click **Map** again, open a job, or click **＋ New Job** to return to the editor.

**Launch sites:** consecutive jobs flown from the same parking spot are grouped into a single **launch site** rather than shown as one route dot per job. Jobs are walked in flight order and accumulated into a site as long as each takeoff stays within ~50 m of the site's running takeoff centroid; the first takeoff beyond that radius starts a new site (so a job near an earlier site but visited much later in the sequence becomes its own site). Each site renders as one amber dot at its takeoff centroid, labelled with the site's **first route index** so it lines up with the per-job route numbering (a thin amber outline marks multi-job sites), and the dots are joined in flight order by the amber dashed route line. Dots that would overlap on screen are nudged apart for legibility (their circles keep their true positions). When you zoom in to street level (zoom ≥ 17, the same level the route chevrons appear), each site **splits back into its individual per-job route-index circles** at the real takeoff spots and the route lines are hidden, so the close-up shows exactly where each job launches. Toggle the layer with **Launch sites on/off** in the legend.

Hovering a launch-site dot reveals the **smallest enclosing circle** over all of that site's survey polygons and takeoff points, with a crosshair at the circle centre, and fills a fixed **announcement box** at the bottom-left of the map. The box holds everything for the site: the member jobs (with the route-index range, e.g. `#3–#5`) plus exactly the fields you enter into an announcement app such as Flyk — the circle-centre coordinates, the diameter (and radius), the maximum flight altitude across the site, and the estimated duration (rounded *up* to the next half hour for margin). A white dotted line (with a dark halo, so it reads on any basemap) links the box to the hovered dot, drawn beneath the dots. *(Note: the launch-site dot is where you stand/launch from; the circle centre is the geometric centre of the area you cover — they are deliberately two different points.)*

**Clicking** a launch-site dot selects all of that site's jobs at once — handy for then routing, exporting, or moving the whole parking-spot group together via the map-view toolbar. At the zoomed-in per-job view, clicking a single takeoff circle selects just that one job.

A toolbar floats at the top of the map whenever map view is active. **Export Route** copies the `.kmz` and homes KML for every route job in the current folder to a local directory you specify — a quick way to collect all mission files before heading to the field. Route jobs are those with a computed takeoff point that have not been marked as skipped; `homes.kml` files are renamed `<job_name>_homes.kml` to avoid collisions. **Auto route** computes the optimal survey order for all ready jobs in the folder using a greedy nearest-neighbour algorithm (starting from the northernmost takeoff point); if some jobs already have route positions, a modal offers to re-route everything from scratch or slot in only the unrouted ones. The remaining toolbar buttons (**Merge**, **Export KML**, **PDF**, **Google Maps**, **Route rename**, **Move**, **Delete**) become active when one or more jobs are selected.

**PDF flight report:** a printable, offline field document. For a single selected job it is a one-page **card** (flight-ready / needs-review badge and reasons, a map of the survey area with the lawnmower strips, transits, takeoff and keep-out drawn over an aerial basemap, a flight-parameter table, the terrain DSM thumbnail, intersecting UAS zones with their altitude floors, and a NOTAM reminder). For several jobs it is a **mission packet**: a cover with totals, an overview map showing every job in flight order with the launch sites and inter-takeoff legs, a summary table, one **flight-announcement page per launch site** (centre coordinates, diameter, max altitude and duration - the fields you enter into Flyk), then the per-job cards. The map basemap is the MML orthophoto by default (OSM as a fallback / `--basemap osm`); overlays are drawn as vector graphics so they stay sharp at print resolution. Generate from the toolbar **PDF** button, or with the CLI:

```bash
flightmanager report 20260611-02-test2 --open          # one-page card
flightmanager report --folder my-group --packet        # full mission packet
flightmanager report a b c --no-cards -o out.pdf        # packet without per-job cards
```

**Statistics panel:** a card below the status legend (top-right) shows summary statistics for the current folder. Use the dropdown to switch between modes:

| Mode | What it shows |
|---|---|
| **Jobs** (default) | Total job count, total area, combined flight time, and battery count — job colors unchanged |
| **Subcategory** | A1 / A2 / A3 color-coded polygons (green / amber / blue) |
| **Altitude** | Four-bin blue palette (dark = low, light = high); lists 5 lowest-altitude jobs |
| **Area** | Five-bin yellow-to-red palette (light = small, dark = large); lists 5 largest and 5 smallest |
| **Lost area %** | Green-to-red palette; 0 % loss shown as a separate green bucket; lists 10 jobs with most lost area |
| **Lost area ha** | Same palette as Lost %, but in absolute hectares |
| **Flight time** | Five-bin light-to-dark green palette; lists 5 longest and 5 shortest jobs |
| **MGRS tiles** | Draws the Sentinel-2 tile(s) the jobs fall in plus their neighbours, each in a distinct colour, listed in the legend with job counts — shows how close the folder sits to a tile/UTM-zone border. Job polygons keep their own colours. Requires the grid file (see setup step 5). |

Jobs without data for the selected stat are shown in grey. In the binning modes (everything except **Jobs** and **MGRS tiles**) a dim overlay is added between the base map tiles and the job polygons to improve color contrast. If jobs are selected (Ctrl+click), the stats reflect only the selected set. Click a job name in any list to pan and zoom the map to that polygon and add it to the selection. The selected stat mode is remembered across sessions.

**Satellite & weather forecast bar:** a bar at the top centre of the map view (mirroring the battery timeline) shows a per-day forecast for the folder's grid square: weather (daytime-average icon, temperature, wind, and cloud cover) and which tracked Earth-observation satellites pass overhead that day. Only daytime passes are shown as badges (colour-coded by family — Sentinel green, Landsat orange); night passes collapse into a `+N☾` marker. A pass is marked a **clear-sky window** (yellow glow) when the cloud forecast at its actual overpass time is low, and a whole day is tinted **green ("golden")** when it is both drone-flyable (wind below the configured limit, fair sky) and has a clear-sky pass — i.e. a day you can both fly and expect usable satellite imagery. The header shows the MGRS tile id; collapse the bar with the chevron (state is remembered). Weather comes from Open-Meteo (`[weather]` config; FMI is a planned alternative); thresholds (`clear_sky_max_cloud_pct`, `drone_wind_limit_ms`, the daytime window) and the tracked satellites are configurable in **⚙ Settings** or `config.toml`. If the grid file is absent the weather still shows; only the satellite overpasses are omitted.

**Battery / flight-time timeline:** a proportional bar appears near the bottom centre of the map whenever there is at least one routable job (a job with a computed route and a flight-time estimate in its manifest). Each segment represents one job, scaled by its estimated flight time. Per-job route index numbers appear below each segment in amber circles (note these are individual job positions; the map groups jobs into launch sites). Battery boundaries are shown as outline battery icons above the bar: a new battery starts whenever the remaining charge (85 % of the drone's rated battery duration) is insufficient to cover the next job. The total flight time for the displayed route is shown to the right. Click any segment or its index circle to zoom the map to that job (fitting its polygon and takeoff point). When jobs are multi-selected, only the selected jobs appear on the timeline; otherwise all routable jobs in the folder are shown.

### Defining the survey area

- **Parcel / property IDs** — paste Ruokavirasto parcel IDs or MML kiinteistötunnus values; the map updates on blur.
- **Scratch polygon** — right-click on the empty map to place a 300×300 m square centred on the cursor, then drag vertices to reshape. No IDs required.

### Flight and polygon parameters

| Parameter | Controls |
|---|---|
| Subcategory | A2 / A3 pills |
| Drone & height | dropdown + number field (live GSD shown) |
| Warning radius | linked to 3× height by default; click "3:1" to restore the link |
| Route angle | Auto pill picks the MBR longest-axis bearing; −/+ step buttons (hold for continuous rotation) override it by 1° increments |
| Survey speed | **Auto** pill recomputes from altitude and drone profile; −/+ buttons (hold for continuous change, 0.1 m/s steps) override it — see below |
| Offset | expand (+) or contract (−) the survey polygon in metres |
| Simplify | Auto pill + −/+ step buttons |
| Keep-out | toggle to disable building buffer subtraction |

The **⚙** gear icon next to the Flight section header opens the **Template Settings** modal — a three-tab panel for DJI WPML parameters not in the main sidebar:

**Capture tab**
- **Front overlap / Side overlap** — ± steppers (default from config). Overlap affects strip spacing and the maximum slope the drone can follow in variable-altitude mode; changing these values also updates the route estimate.

**Safety tab**
- **Take-off security height** — DJI `takeOffSecurityHeight` in metres.
- **Return-to-home altitude** — `globalRTHHeight`; independent of take-off security height.
- **RC signal lost** — Hover / Return to home / Land immediately (`rcLostAction`).
- **Mission finish action** — Return to home / Hover / Auto land / No action (`finishAction`).

**Terrain tab (variable altitude)**
- **Variable altitude** — checkbox; when enabled the tool generates an explicit `waylines.wpml` with per-waypoint altitudes derived from obstacle proximity (see [Variable-altitude flight path](#variable-altitude-flight-path) below). The Flight section header shows a small **ADV** badge when this mode is active.
- **Min altitude** — floor altitude in metres AGL (default 30 m, matches the A2 1:1 rule floor).
- **Max altitude** — ceiling over open field (blank = same as the main height field).
- **Power line clearance** — altitude in metres AGL the drone must reach before crossing or passing near a 110 kV line (default 70 m, assumes a 45 m tower + 25 m margin).
- **Slope tolerance F** — photogrammetry slope tolerance (default 0.20 = 20 % altitude change per photo baseline); controls how aggressively the altitude ramp follows obstacles.

### Preview

Preview runs automatically whenever a parameter changes. The map shows the survey polygon, original parcel outlines, keep-out circles, buildings, warning radius circles, UAS zones, DSM elevation overlay, and the flight route overlay — all layers are toggleable from the legend.

- **Power lines** — high-voltage lines (110 kV+) from MML *Maastotietokanta* (`sahkolinja`) are fetched for the same area as buildings. Overhead spans (solid amber on the map) automatically subtract a configurable keep-out buffer from the survey polygon. Underground cables (dashed amber) are shown for situational awareness only — no keep-out buffer applied. MTK misclassification is corrected automatically: any 22311-coded segment whose endpoints match pylon tower locations in `suurjannitelinjanpylvas` is re-classified as overhead before the keep-out is computed. Disable or adjust the buffer under **⚙ Settings → Power Lines**.
- **UAS zones** are clickable: see altitude floor/ceiling and all overlapping zones at a point. Inner concentric zones of an airfield are shown with a dashed border for context. The zones legend layer auto-enables when zones first appear.
- **Zone altitude cap** — when a zone hit carries an altitude floor, flight height is automatically capped to that floor value and the warning radius re-syncs. Buffer-only and context-only zones (e.g. inner concentric airfield zones shown for reference) are excluded from the cap. The cap is advisory; override freely.
- **Takeoff marker** — a white ✕ on the polygon boundary marks the auto-suggested takeoff/landing point (the boundary point that minimises worst-case VLOS distance). Drag it to a more convenient spot. Click **↺ Reset takeoff position** to revert. Saved with the job.
- **Route overlay** — amber lines show the planned lawnmower survey strips and all transit legs (inter-strip turns, takeoff-to-start, and return-to-home). Each strip has a `›` direction chevron at its midpoint so you can see which end the drone departs from. Strip ordering uses a greedy nearest-neighbour algorithm so same-arm strips on C/U-shaped fields are grouped together, minimising long transits across keepout gaps. For concave polygons, inter-strip transitions that would exit the survey area are automatically rerouted along the polygon boundary (shorter of the exterior ring or any interior keepout ring). The status panel below the map shows strip count, estimated photo count, and estimated total flight time. The route auto-computes on every parameter change; an accurate Python estimate (EPSG:3067 geometry, correct home transit distance, NN ordering) replaces the instant rough scanline preview 500 ms after input settles. Layer visibility is remembered across parameter changes and job switches. Legend eye toggles are persistent for the session.
- **Coverage layer** (off by default, toggle in legend) — semi-transparent amber rectangles showing the exact camera footprint for every strip. Toggle on to verify edge coverage: the first/last strips are centred at `strip_spacing / 2` from the boundary so the polygon edge falls within the overlap zone of the two outermost strips, giving consistent side overlap all the way to the field edge. Strip endpoints along each strip are clipped to the polygon boundary (consistent with DJI Pilot 2 `margin=0`).
- **DSM elevation overlay** — the terrain-follow DSM thumbnail is rendered using a viridis colour palette (purple = low, yellow = high).

### 3D flight preview

Click the **3D** button in the map controls (top-left, next to the layer switcher) to switch from the Leaflet 2D view to an interactive CesiumJS 3D globe. The button is enabled once a route estimate has been computed.

In 3D view:
- The flight path is rendered as a tube. In simple mode all strips are shown in a single colour. In **variable-altitude** mode (ADV active) strips are colour-coded by altitude using the same viridis palette as the DSM overlay (purple = low altitude near buildings, yellow = high altitude over open field) and a colour legend appears in the top-right corner of the 3D view.
- A translucent curtain hangs below the path to visualise the ground clearance profile.
- **Obstacle and restriction volumes** are drawn around the route so you can eyeball clearances:
  - **Building keep-out** (red) — in A2, a 1:1 climb envelope around each keep-out building: a cylinder from ground to the minimum altitude plus a widening frustum up to the maximum in variable-altitude mode, or a single cylinder sized by the flat flight altitude in simple mode. In A1/A3 a fixed 150 m radius × 150 m tall separation cylinder per the aviation rule.
  - **Power lines** (amber) — each overhead 110 kV+ line as a 60 m wide × 40 m tall rectangular keep-out pipe.
  - **UAS zones** (orange) — each restriction zone extruded from its altitude floor to its ceiling, so an airfield's concentric A/B/C/D bands stack into the characteristic stepped inverted pyramid.
- A **layer legend** (top-right) lists every active layer — DSM, area, flight path, curtain, drone, keep-out, power lines, UAS zones — each with an eye toggle for visibility.
- **Playback controls** appear at the bottom of the 3D view: ▶ play/pause, ⟳ reset, a time scrubber, and a playback-speed selector. The drone icon animates along the route in real time, with an altitude/speed telemetry readout.
- Click **2D** (the mirror of the toggle, shown over the Cesium view) to return to the Leaflet map.

The 3D view loads CesiumJS from a CDN on first use; subsequent activations are instant.

### Variable-altitude flight path

> **Work in progress** Just the initial code to handle this is in place. The path generation is flawed and hasn't been tested yet on actual drone.

Enable **Variable altitude** in the Terrain tab of the Template Settings modal (⚙ next to the Flight section header). When active:

1. The server computes a per-strip altitude profile using `obstacle_heights.py`:
   - **Near buildings** — altitude decreases toward the configured minimum (A2 1:1 rule: horizontal distance must be ≥ flight altitude, so flying lower lets the drone approach closer). The effective proximity distance is measured to the building rooftop, estimated from MML `kerrosluku` (floor count × 3 m) or a per-kohdeluokka heuristic.
   - **Near 110 kV power lines** — altitude increases to the configured clearance height (default 70 m AGL).
   - A forward + backward slope filter ensures the altitude profile is physically achievable given the drone's climb/descent rate (~3 m/s) and the photogrammetry overlap constraints.
2. An explicit `waylines.wpml` is generated (instead of the `mapping2d` template stub), with `templateType=waypoint` so DJI Pilot 2 imports the pre-computed altitudes without regenerating them. Per-waypoint speed is set via `drone.auto_speed(h, overlap)` — the drone automatically slows at lower altitudes.
3. `executeHeightMode=relativeToStartPoint` — all altitudes are relative to the actual takeoff point, so the mission stays correct even if the takeoff position differs slightly from the planned point.
4. The route overlay in both 2D (Leaflet) and 3D (Cesium) views colour-codes strips by altitude. The status panel shows the altitude range (min – max m AGL).
5. The map-view job popup shows the altitude range for variable-altitude jobs.

### Polygon editing

Double-click the survey polygon to enter vertex-drag edit mode; double-click the map background to exit and save. On exit, buildings and UAS zones refresh automatically for the new shape.

In edit mode:
- White squares = vertices; smaller white diamonds = midpoints. Drag a midpoint to add a vertex. Click a vertex to delete it. A polygon can be reduced to a triangle (3 vertices); to remove it entirely, right-click any of its remaining vertices.
- **Bridge / Split** — right-click any vertex to start (turns orange), then left-click more vertices:
  - 2 vertices on the **same polygon** → a split line appears and a **Split job** button shows in the hint bar. Click it to divide the job into two sibling jobs, each containing one half of the polygon with all other parameters (IDs, flight settings, color) copied across.
  - 2 vertices on each of **two polygons** (4 total) → **bridge** (joins them with a quadrilateral corridor)
  - Press **Esc** or right-click to cancel.
- When a keep-out buffer (e.g. power line) splits the parcel into multiple polygons, each piece is independently editable. Unwanted fragments can be removed by reducing them to a triangle and right-clicking a vertex.
- Click **↻ Reset polygon** to revert all edits.

### Map tools

- **Base layer** — the layer switcher (top-left, next to zoom buttons) toggles between OpenStreetMap and MML Ortokuva aerial imagery. The ortho layer requires `MML_API_KEY` in `.env`. MML's ortho tiles are natively available up to zoom 15; zooming in further upscales those tiles so the imagery stays visible for boundary editing rather than going blank.
- **Measure** — hold **Ctrl** and right-click-drag to draw a dimensioning line with a distance label. Hold **Ctrl+Shift** to draw a radius circle instead. Click **✕** in the map controls to clear all measurements.
- **Job color** — the color swatch next to the **Name** field sets the per-job display color used in map view. Clicking the swatch opens a popup with a palette of preset colours (configurable via `color_palette` in `config.toml`) and an inline HSV picker (saturation/value canvas, hue slider, RGB inputs, hex field). Colour changes are saved immediately without requiring a full export.

### Save and settings

- **Save** — writes KMZ, DSM, homes KML, manifest, `job_params.json`, and thumbnail to disk. Unsaved changes are tracked and you are prompted before switching jobs, pressing **Esc**, or clicking the **←** back arrow.
- **⚙ Settings** — opens the in-browser config editor (all sections: Flight, Safety, Polygon, UAS Zones, Cache, Output, Parcels, Properties). Changed fields highlight in amber; a search box filters across all sections. Saving hot-reloads the server and writes directly to `config.toml`. Drone profiles must be edited in `config.toml` directly; `config.example.toml` is the reference for all options.
- **ⓘ About** — the `⋯` button in the header opens the About dialog, which shows the software version and a **session statistics** table: how many tiles, parcel geometries, and zone records were fetched from the network vs. served from the local cache, and the total bytes downloaded.

Parcel and property geometries are cached locally (400-day TTL) so repeat previews do not hit the network. Building and DEM tiles are cached on a 1 km grid (configurable TTL). The same statistics are also printed to the terminal when `flightmanager serve` shuts down (Ctrl-C).

---

## AI assistant integration (MCP)

`dkk-flightmanager` exposes a [Model Context Protocol](https://modelcontextprotocol.io) server so AI assistants — Claude Desktop, Claude Code, or any MCP-compatible client — can query job data and trigger pipeline operations directly.

### Primary path — integrated with the web UI

When `flightmanager serve` is running, the MCP server is mounted at `/mcp/sse` in the same process. No separate command, no extra process, no coordination overhead.

**Claude Code:**
```bash
claude mcp add jobmaker --url http://localhost:8765/mcp/sse
```

**Claude Desktop** — add to the config file for your platform:

| Platform | Config file location |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |

```json
{
  "mcpServers": {
    "jobmaker": {
      "url": "http://localhost:8765/mcp/sse"
    }
  }
}
```

If you use a non-default port, replace `8765` accordingly.

### Fallback path — standalone (no web UI)

For headless use or automation scripts that don't need the browser UI:

```bash
flightmanager mcp                              # stdio transport, reads config.toml from cwd
flightmanager mcp --config /path/to/config.toml
```

**Claude Code:**
```bash
claude mcp add jobmaker -- flightmanager mcp
```

**Claude Desktop** — use the config file path from the table above, with the platform-appropriate binary path:

macOS / Linux:
```json
{
  "mcpServers": {
    "jobmaker": {
      "command": "/path/to/project/.venv/bin/flightmanager",
      "args": ["mcp"],
      "env": { "MML_API_KEY": "your-key-here" }
    }
  }
}
```

Windows:
```json
{
  "mcpServers": {
    "jobmaker": {
      "command": "C:\\path\\to\\project\\.venv\\Scripts\\flightmanager.exe",
      "args": ["mcp"],
      "env": { "MML_API_KEY": "your-key-here" }
    }
  }
}
```

Use the full absolute path to the `.venv` binary so Claude Desktop finds the right environment. `MML_API_KEY` must be passed explicitly here — Claude Desktop launches subprocesses from its own working directory, not the project directory, so the project `.env` file is not picked up automatically. On macOS/Linux you can alternatively export the key in `~/.zshenv` or `~/.profile` so it is inherited by all GUI-launched processes.

### What the AI can do

**Read (always safe):**

| Tool / Resource | Description |
|---|---|
| `list_jobs` | List jobs with filters: folder, needs\_review, flight\_ready, untouched. Each card includes area, flight time, photo count, battery count, height, drone, lost area %, and more |
| `get_job` | Full details for one job — inputs, flight params, zone hits, flight status, and a `stats` block with all numeric fields |
| `job_stats` | Aggregate stats across all jobs or a folder: total area, total flight time, total photo count, total battery count, lost area, and review/ready/untouched counts |
| `jobs://list` | All jobs as a resource (same as list\_jobs with no filters) |
| `jobs://{path}` | Raw params + manifest for one job |
| `config://current` | Active drone, GSD, flight and safety settings |
| `drones://list` | All drone profiles with GSD at 50/80/100 m |

**Write (pipeline operations):**

| Tool | Description |
|---|---|
| `create_folder` | Create a named job group folder |
| `delete_job` | Delete a job and all its output files; auto-removes empty parent folder |
| `create_preview` | Run geometry + UAS zone check without writing files (~10–30 s) |
| `create_batch` | Create skeleton jobs from parcel/property IDs (no KMZ) |
| `export_existing_job` | Export a job that already exists on disk — reads stored polygon and params from `job_params.json`, no need to re-supply IDs (~30–120 s) |
| `run_export` | Full pipeline from scratch — KMZ, DSM, homes KML, manifest written to disk (~30–120 s) |

Pipeline tools are serialised against the web UI: if a browser job is already running, the MCP tool returns an error immediately rather than colliding on the shared tile cache.

### Example queries

```
Which of my jobs have UAS zone conflicts?
What's the total survey area and estimated flight time for the Vaasa-2026 folder?
How many batteries will I need for all flight-ready jobs in folder Seinäjoki?
Which jobs have more than 10% lost area, and why?
Create batch jobs for parcel IDs 5241087453, 5241087454, 5241087455 in folder Seinäjoki
Export all untouched jobs in folder Seinäjoki
What drone should I use for a 12 ha field at 3 cm GSD?
Run a preview for parcel 5241087453 and tell me if there are any zone issues
Delete all jobs in folder test
```

---

## CLI usage

### Specifying the survey area

```bash
# Ruokavirasto peruslohkotunnus (comma-separated, or mixed with --properties)
flightmanager run --name pelto-2024 --parcels 5241087453,5241087454

# Finnish kiinteistötunnus — dash form or 14-digit numeric
flightmanager run --name pelto-2024 --properties 214-407-3-22
flightmanager run --name pelto-2024 --properties 21440700030022

# Combine parcel and property IDs
flightmanager run --name pelto-2024 --parcels 5241087453 --properties 214-407-3-22

# Bounding box (EPSG:3067 metres)
flightmanager run --name pelto-2024 --bbox 295000,6974000,305000,6984000
```

### All `flightmanager run` options

| Flag | Default | Description |
|---|---|---|
| `--name`, `-n` | *(required)* | Job name; used as output subdirectory |
| `--parcels`, `-p` | — | Comma-separated peruslohkotunnus IDs |
| `--properties`, `-k` | — | Comma-separated kiinteistötunnus values (dash or 14-digit form) |
| `--bbox` | — | Bounding box `xmin,ymin,xmax,ymax` in EPSG:3067 metres |
| `--drone` | from config | Drone + payload profile name (e.g. `m3m`, `m300-p1-24`) — see below |
| `--height` | from config | Flight height in metres AGL (back-calculates GSD from the active drone's camera constants) |
| `--subcategory` | from config | Operating subcategory: `A2` or `A3` |
| `--buffer` | from config | Home keep-out buffer in metres (overrides the subcategory default) |
| `--homes-distance` | 2× buffer | Max distance (m) from survey polygon to include a building in the homes KML — see below |
| `--preview-radius` | 3× height | Radius (m) of the yellow informational circle on the preview map — see below |
| `--simplify` | from config | Polygon vertex reduction — see below |
| `--offset` | `0` | Expand (+) or contract (−) the survey polygon by this many metres relative to the parcel boundary — see below |
| `--no-keepout` | off | Disable automatic keep-out subtraction around buildings — see below |
| `--open` | off | Reveal the job output folder in the system file manager after the job completes |
| `--config`, `-c` | `config.toml` | Path to config file |
| `--dry-run` | off | Fetch and validate only — no output files written |
| `--offline` | off | Cache-only mode; fail cleanly on any cache miss |
| `--refresh` | off | Force re-download of all touched tiles |

### Polygon simplification (`--simplify`)

Parcel and property boundaries follow cadastral lines precisely and can produce
dense polygons that are difficult to edit on the DJI RC touch screen.
`--simplify` reduces vertex count before writing the KMZ.

```bash
# Auto: find the knee of the simplification curve (recommended)
flightmanager run --name pelto-2024 --parcels 5241087453 --simplify auto

# Fixed tolerance in metres (Douglas-Peucker)
flightmanager run --name pelto-2024 --parcels 5241087453 --simplify 5

# Disable (keep every vertex from the source data)
flightmanager run --name pelto-2024 --parcels 5241087453 --simplify 0
```

The defaults are set under `[polygon]` in `config.toml` (or via **⚙ Settings → Polygon** in the browser UI):

```toml
simplify_mode = "fixed"           # "fixed" or "auto"
simplify_tolerance_m = 1.0        # metres; used when simplify_mode = "fixed"
auto_simplify_max_vertices = 15   # hard cap for simplify_mode = "auto"
```

**Auto mode** samples vertex count at ten log-spaced tolerances (0.5 m → 500 m) and picks the tolerance at the **knee of the complexity curve** — the point where further simplification stops removing noise and starts distorting the actual field boundary. For Finnish cadastral data (survey noise ~2–5 m), the knee typically falls in the 5–20 m range, producing 8–20 vertices. `auto_simplify_max_vertices` acts as a hard upper bound; if the knee result exceeds it a binary search enforces the cap.

The vertex count after simplification is printed in the job summary and recorded
in `manifest.json` under `geometry.survey_vertex_count`.

### Survey polygon offset (`--offset`)

Expand or contract the survey polygon relative to the parcel/property cadastral boundary, applied after gap-fill and before keep-out subtraction.

```bash
# Push the survey area 10 m outside the cadastral line
flightmanager run --name pelto-2024 --parcels 5241087453 --offset 10

# Pull the survey area 5 m inside the cadastral line (field-edge margin)
flightmanager run --name pelto-2024 --parcels 5241087453 --offset -5
```

A negative offset can split the polygon or introduce holes at narrow corners — these are handled automatically by `hole_policy` / `multipart_policy` in the same way as keep-out subtraction results. If the contraction collapses the polygon entirely, the original geometry is preserved and a warning is logged.

Set `survey_offset_m` under `[polygon]` in `config.toml` (or via **⚙ Settings → Polygon**) to apply a default offset to every job.

**Offset and polygon editing:** Edit mode shows the offset-applied polygon. Exiting bakes that shape in and resets offset to 0, preventing double-application. To re-apply an offset after editing, type a new value in the Offset field after exiting edit mode.

### Disabling keep-out subtraction (`--no-keepout`)

By default the tool subtracts a buffer around buildings from the survey polygon. This produces a clean separation from structures but adds arc vertices to the polygon boundary, which can make it harder to edit on the RC touch screen.

```bash
# Skip the keep-out subtraction
flightmanager run --name pelto-2024 --parcels 5241087453 --no-keepout
```

When `--no-keepout` is used:
- The survey polygon is not cut back around buildings — it covers the full parcel area.
- Buildings and their distance circles are still shown on the preview map.
- A prominent red warning is added to the preview panel reminding the operator to verify distances to all buildings manually.

Use this only when you have the landowner's permission to fly close to buildings and have verified the required separation under your operating subcategory. Set `offset_enabled = false` under `[home_safety]` in `config.toml` (or via **⚙ Settings → Safety**) to make it the default.

### Drone profiles (`--drone`)

List available profiles and their GSD at typical altitudes:

```bash
flightmanager drones
```

```
Name               GSD@50m   GSD@100m  Label
m3m                  1.34 cm    2.68 cm  DJI Mavic 3 Multispectral — RGB channel
m3m-ms               2.14 cm    4.28 cm  DJI Mavic 3 Multispectral — MS-limited GSD
m3m-rgb              1.34 cm    2.68 cm  DJI Mavic 3 Multispectral — RGB only
m3e                  1.34 cm    2.68 cm  DJI Mavic 3 Enterprise — RGB camera
m300-p1-24           0.92 cm    1.83 cm  DJI Matrice 300 RTK + Zenmuse P1 (24 mm)
m300-p1-35           0.63 cm    1.26 cm  DJI Matrice 300 RTK + Zenmuse P1 (35 mm)
m300-p1-50           0.44 cm    0.88 cm  DJI Matrice 300 RTK + Zenmuse P1 (50 mm)
m350-p1-24           0.92 cm    1.83 cm  DJI Matrice 350 RTK + Zenmuse P1 (24 mm)
m350-p1-35           0.63 cm    1.26 cm  DJI Matrice 350 RTK + Zenmuse P1 (35 mm)
m350-p1-50           0.44 cm    0.88 cm  DJI Matrice 350 RTK + Zenmuse P1 (50 mm)
```

Select a drone for a job:

```bash
flightmanager run --name pelto-2024 --parcels 5241087453 --drone m300-p1-24
```

Set the default in `config.toml` (or via **⚙ Settings → Drone** in the browser UI):

```toml
default_drone = "m3m"
```

To add a custom drone, add a `[[drones]]` entry to `config.toml` — see the commented
examples in `config.example.toml`. If you add any `[[drones]]` entries, they replace
the built-in list entirely, so copy across any profiles you still want to use.

> **Note on M350 RTK:** the `drone_enum` value (89) for M350 RTK profiles is from
> community sources and has not been confirmed from official DJI WPML documentation.
> Before flying a job generated for an M350 RTK, verify the value by exporting a
> test mission from DJI Pilot 2 on the aircraft and checking `wpml:droneEnumValue`
> in the KMZ.

### Strip speed (auto mode)

The KMZ `autoFlightSpeed` value written into every mission is calculated automatically from the drone's SD card write throughput, the flight altitude, and the front overlap setting. This matches how DJI Pilot 2 computes its own "auto speed" and prevents the drone from triggering captures faster than the card can write — which causes buffer overflow and mid-flight image loss at low altitudes.

The formula is:

```
strip_speed = (1 − front_overlap) × altitude × (sensor_height / focal_length) / capture_interval
```

`capture_interval` is the minimum time between consecutive shutter triggers, limited by how fast the SD card can flush one burst of files. Each drone profile in `drones.toml` carries a calibrated `min_capture_interval_s` value:

| Profile | Capture mode | Interval | Calibrated from |
|---|---|---|---|
| `m3m` | RGB + MS simultaneously (5 files ≈ 49 MB/capture) | 2.38 s | DJI Pilot 2: 8.9 m/s at 100 m AGL, 80% front overlap |
| `m3m-ms` | RGB + MS simultaneously, GSD planned for MS sensor | 1.868 s | Back-calculated from DJI's 8.9 m/s using the MS sensor footprint |
| `m3m-rgb`, `m3e` | RGB only (1 file ≈ 9 MB/capture) | 1.41 s | DJI Pilot 2: 15 m/s at 100 m AGL, 80% front overlap |
| P1 profiles | — | 2.0 s | Estimate — calibrate from DJI Pilot 2 at a known altitude |

Example speeds for the M3M (RGB+MS) at 80% front overlap:

| Altitude | Strip speed |
|---|---|
| 30 m | ~2.7 m/s |
| 50 m | ~4.5 m/s |
| 80 m | ~7.1 m/s |
| 100 m | ~8.9 m/s |

To override with a fixed speed for all jobs, set `auto_flight_speed_ms` under `[flight]` in `config.toml`. In the browser UI the **Survey speed** control defaults to **Auto** (recomputes from altitude and drone profile); use the −/+ buttons to set a per-job override, or click **Auto** to return to computed mode. To calibrate a new drone or capture mode, read the auto-speed value from DJI Pilot 2 at a known altitude and back-calculate: `interval = (1 − overlap) × altitude × (sensor_height / focal_length) / speed`.

### Batch skeleton job creation (`flightmanager batch`)

Creates skeleton jobs for a list of parcel or property IDs — fetches geometry for each ID and writes `job_params.json` (no KMZ or DSM). Jobs appear in the browser UI ready to open, tune, and export.

```bash
# Inline parcel IDs
flightmanager batch --parcels 5241087453,5241087454 --folder my-group

# Inline property IDs
flightmanager batch --properties 214-407-3-22,214-407-3-23 --folder my-group

# IDs from a file (one per line; # comments and blank lines ignored)
flightmanager batch --file ids.txt --folder my-group

# Mix inline IDs and file
flightmanager batch --parcels 5241087453 --file more.txt --folder my-group

# Auto-detect ID type from format (all-digit → parcels; NNN-NNN-N-NN → properties)
flightmanager batch --file ids.txt --folder my-group
```

The `--parcels` / `--properties` flag determines ID type. If neither is given, type is auto-detected from the first ID in the file. Each ID becomes a job named after the ID itself. Existing jobs with the same name in the target folder are skipped (not overwritten). A per-ID pass/fail table is printed on completion.

| Flag | Description |
|---|---|
| `--parcels TEXT` | Comma-separated parcel IDs (also sets ID type to parcels) |
| `--properties TEXT` | Comma-separated property IDs (also sets ID type to properties) |
| `--file PATH` | Text file of IDs (one per line) |
| `--folder TEXT` | Output subfolder to group the batch under |
| `--drone TEXT` | Drone profile override |
| `--height FLOAT` | Flight height override (m AGL) |
| `--subcategory TEXT` | `A2` or `A3` |

### Refreshing stale jobs (`flightmanager refresh`)

Recomputes already-exported jobs **in place** with the current pipeline. A job is "stale" when it was built by an older pipeline version (the route/altitude/keep-out logic has since changed) or when the local cache now holds newer source tiles than the job used. Refresh is **recompute-only**: the edited / ID-derived geometry is preserved — only the route, DSM, stats, KMZ, and manifest are rebuilt from cached tiles.

```bash
# Refresh specific jobs
flightmanager refresh 20260611-02-test2 my-group/5241087453

# Refresh every stale job
flightmanager refresh --all-stale

# Limit --all-stale to one folder
flightmanager refresh --all-stale --folder my-group
```

The browser UI surfaces the same thing as a banner ("N jobs can be refreshed · Refresh all") when stale jobs are detected on load. Refreshing flags any job whose `flight_ready` / `needs_review` status changes as a result.

### MCP server (standalone)

See the [AI assistant integration](#ai-assistant-integration-mcp) section above for the primary (integrated) approach. The `mcp` command is for headless use without `flightmanager serve`:

```bash
flightmanager mcp                               # stdio transport, config.toml in cwd
flightmanager mcp --config /path/to/config.toml
```

### Cache management

Pre-fetch tiles before a field day (no network needed on-site with --offline):

```bash
# Warm the cache for an area
flightmanager cache warm --bbox 295000,6974000,310000,6985000

# Check what's cached
flightmanager cache status

# Refresh tiles older than 30 days
flightmanager cache refresh --older-than 30
```

After `flightmanager run`, `flightmanager batch`, and `flightmanager cache warm` complete, a session statistics table is printed showing fetches vs. cache hits per data source and total bytes downloaded:

```
──────────────────────────────────────────────────────
              Session network statistics
──────────────────────────────────────────────────────
  DEM tiles   3 fetched  (2.3 MB),  61 cached,  95% cache rate
  Buildings   3 fetched  (56.3 KB),  57 cached,  95% cache rate
  Power lines 3 fetched  (12.1 KB),  57 cached,  95% cache rate
  Pylons      3 fetched  (4.2 KB),   57 cached,  95% cache rate
  Parcels     18 cached,  100% cache rate
  UAS zones   18 cached,  100% cache rate
──────────────────────────────────────────────────────
  Total       12 fetched,  268 cached,  2.4 MB downloaded
──────────────────────────────────────────────────────
```

The same table appears on `flightmanager serve` shutdown and in the **ⓘ About** dialog in the browser UI.

## Operator workflow

### Planning (office / laptop)

1. Run `flightmanager serve` and open http://localhost:8765.
2. Define the survey area — either:
   - Paste parcel or property IDs and let the polygon generate automatically, or
   - Right-click on the empty map to draw a 300×300 m scratch square, then reshape it in edit mode.
3. Adjust height, subcategory, simplify, and offset as needed.
4. Review the map: survey polygon, keep-out circles, warning radius circles, buildings, UAS zones, and DSM elevation overlay (toggle layers via the legend). Switch to MML Ortokuva aerial imagery via the layer control for ground-truth reference.
5. Edit the polygon if needed (double-click to enter, double-click background to save). Buildings and zones refresh automatically on exit.
6. Check the white ✕ takeoff/landing marker — the tool suggests a boundary point that minimises your worst-case VLOS distance to the drone. Drag it to a more accessible location if needed (e.g. closer to a road or gate). Use **↺ Reset takeoff position** to go back to the auto suggestion.
7. Click **Save** when satisfied.

### On the RC

1. Copy mission files to the RC via USB. Use **Export Route** in map view to copy all `.kmz` and homes KML files for the planned route to a single folder in one click, then transfer that folder to the RC.
2. Open DJI Pilot 2 → **Routes** → import `<name>.kmz`.
   The DSM is embedded in the KMZ — Pilot 2 links it automatically.
3. In the map view → **Custom layers** → import `<name>_homes.kml` to see building pins.
4. Verify the height readout over the field looks correct.
5. **Never fly a job with `flight_ready: false` or `needs_review: true` in the manifest
   without a human check** — these flags indicate home-distance, zone, or geometry issues.

## Subcategory and keep-out distances

| Subcategory | Requirement | Buffer used |
|---|---|---|
| **A2** (C2-labelled drone + A2 certificate) | ≥ flight height from people | Derived from `--height` or config |
| **A3** (no C-label / C3 / C4) | ≥ 150 m from residential/commercial/industrial/recreational areas | 150 m fixed |

### Power line keep-out (`overhead_buffer_m`)

High-voltage power lines are a significant hazard for low-altitude drone operations. The tool fetches MML *Maastotietokanta* `sahkolinja` features (110 kV+ lines only) for the same bounding box used for buildings.

| Feature type | MTK kohdeluokka | Treatment |
|---|---|---|
| Ilmajohto (overhead span) | 22312 | Keep-out buffer subtracted from survey polygon |
| Kaapeli (underground cable) | 22311 | Shown on map only — no keep-out |

Because MML data contains known misclassifications (some overhead spans between pylon towers are coded as 22311), the tool cross-references line endpoints against `suurjannitelinjanpylvas` pylon tower locations. Any 22311 segment whose both endpoints sit within 2 m of a pylon tower is treated as overhead for keep-out purposes.

Configure under `[powerlines]` in `config.toml` (or **⚙ Settings → Power Lines** in the browser UI):

```toml
[powerlines]
enabled = true
overhead_buffer_m = 30.0   # metres; Finnish aviation guidance recommends staying well clear
```

Set `overhead_buffer_m = 0` to show lines on the map without subtracting a buffer.

The `operating_subcategory` in `config.toml` (or **⚙ Settings → Safety**) sets the default; override per-job with `--subcategory`.

### Homes KML inclusion distance (`--homes-distance`)

Buildings are included in `<name>_homes.kml` if their nearest point is within this distance of the survey polygon boundary. The default is 2× the keep-out buffer (e.g. 300 m for A3, 2× flight height for A2) — wide enough to capture buildings that sit just outside the keep-out zone but are still visible from the survey area.

```bash
# Include buildings up to 400 m from the polygon (A3 job)
flightmanager run --name pelto-2024 --parcels 5241087453 --subcategory A3 --homes-distance 400
```

Set `home_include_buffer_m` under `[home_safety]` in `config.toml` (or via **⚙ Settings → Safety**) to change the default permanently.

### Map preview yellow circle (`--preview-radius`)

The preview map draws a yellow dashed circle around each keep-out building. This is a visual reference only — it does not affect the KMZ or homes KML.

The default radius is **3× derived flight height** (the "3:1 horizontal rule" sometimes used for risk assessment). At 100 m AGL the default is 300 m.

```bash
# Use a fixed 200 m circle instead of the 3:1 default
flightmanager run --name pelto-2024 --parcels 5241087453 --preview-radius 200

# Tighten to 1:1 (same as the keep-out buffer)
flightmanager run --name pelto-2024 --parcels 5241087453 --height 80 --subcategory A2 --preview-radius 80
```

Set `preview_radius_m` under `[home_safety]` in `config.toml` (or via **⚙ Settings → Safety**) to change the default permanently.

## Safety notes

- **120 m AGL limit** — enforced by the tool; `max_height_agl_m` in config.
- **UAS zones** — checked automatically against Traficom's published permanent zone data. The survey area is expanded by 500 m before the check so boundary-adjacent zones are also flagged. Finnish UAS vyöhykkeet (A–D) are concentric altitude bands; the browser UI treats the zone floor (`lower_limit`) as the binding altitude cap and auto-sets height to 75 % of that floor when a zone is hit. Zone data is re-fetched daily. When the survey intersects an airfield's outer zone, inner concentric zones are shown with a dashed border for context. **Temporary restrictions are NOT included** — NOTAMs *and* temporary reserved/segregated airspace (TRA/TSA, published via AUP/UUP, e.g. military-exercise areas) must be checked manually on the day (ais.fi / Fintraffic Sky / Droneinfo).
- The generated job is a planning aid. The remote pilot remains responsible for compliance, airspace checks, and uninvolved-person separation on the day.

## Disclaimer

This software is a flight-planning aid. It does not replace the legal and operational responsibilities of the remote pilot or the UAS operator.

**The remote pilot and operator are solely responsible for:**
- Ensuring the flight complies with all applicable regulations (EU UAS Regulation 2019/947, Finnish national rules, and Traficom guidance).
- Verifying airspace status, NOTAMs, and temporary restrictions before every flight.
- Confirming that uninvolved-person separation distances are maintained throughout the operation.
- Checking that the aircraft, batteries, and equipment are airworthy.
- Making the final go/no-go decision on the day.

The tool flags potential issues (zones, buffer violations, geometry problems) via `flight_ready` and `needs_review` in the manifest, but **a clean manifest is not a clearance to fly**. These checks are limited to the data sources available at job-generation time and may be incomplete or out of date.

The authors and SeAMK accept no liability for accidents, incidents, regulatory violations, or any other consequences arising from the use of this software.

## Reference fixtures

`tests/test_wpml.py` validates generated KMZ output against a reference mission
exported from DJI Pilot 2. These files are gitignored (flight data) and must
be provided locally.

### Required files

| File | How to get it |
|---|---|
| `fixtures/reference_mission.kmz` | Export a mapping mission from DJI Pilot 2 on the M3E with terrain-follow enabled, then copy the `.kmz` off the RC. |
| `fixtures/template.kml` | Extracted from the KMZ (see below). |
| `fixtures/waylines.wpml` | Extracted from the KMZ (see below). |

```bash
cd fixtures
unzip -j reference_mission.kmz 'wpmz/template.kml' 'wpmz/waylines.wpml'
```

See `fixtures/FIXTURE_NOTES.md` for annotated analysis of all M3E-specific values.

## Attribution (CC-BY 4.0)

All data sources require attribution. The manifest records the exact strings with retrieval dates. The browser UI map displays the applicable credits in the Leaflet attribution control.

| Data | Attribution string |
|---|---|
| Elevation (DEM 2 m) | Contains data from the National Land Survey of Finland, Elevation model 2 m, retrieved \<date\>. |
| Buildings (Maastotietokanta) | Contains data from the National Land Survey of Finland, Topographic Database, retrieved \<date\>. |
| Power lines (Maastotietokanta) | Contains data from the National Land Survey of Finland, Topographic Database, retrieved \<date\>. |
| Parcels (Ruokavirasto) | Contains data from Ruokavirasto (Finnish Food Authority), Peltolohkorekisteri, retrieved \<date\>. |
| Properties (Kiinteistötietojärjestelmä) | Contains data from the National Land Survey of Finland, Cadastral Index Map, retrieved \<date\>. |
| UAS zones | Contains data from Traficom, UAS Geographical Zones, retrieved \<date\>. |

---

## Project

This tool was developed as part of the **[Datakasvukunto](https://projektit.seamk.fi/kestavat-ruokaratkaisut/datakasvukunto/)** project at [Seinäjoki University of Applied Sciences (SeAMK)](https://www.seamk.fi).

More projects from SeAMK: [github.com/SeAMKedu](https://github.com/SeAMKedu)

---

![SeAMK](images/logo_seamk.jpg) 
---
![Luke](images/logo_luke.png) 
---
![Co-funded by the European Union](images/logo_cofunded.png)
