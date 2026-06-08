# dkk-jobgen

DJI terrain-following mapping job generator for Finnish agricultural field parcels.

`dkk-jobgen` is a planning tool for drone mapping surveys over Finnish farmland. Identify the survey area by pasting *peruslohkotunnus* field parcel IDs (Ruokavirasto), *kiinteistötunnus* cadastral property IDs, a bounding box, or a polygon drawn directly on the map — the tool fetches field boundaries, 2 m terrain elevation, building footprints, and high-voltage power line geometry from National Land Survey of Finland (MML) open data APIs, checks Traficom UAS restriction zones, and writes a ready-to-fly DJI Pilot 2 mapping job. A built-in browser UI handles everything from parcel lookup and polygon editing to flight parameter tuning and batch job creation for large parcel sets.

All underlying data — field boundaries (Ruokavirasto *Peltolohkorekisteri*), terrain elevation, buildings, and high-voltage power lines (MML *Maastotietokanta*), cadastral geometry (MML *Kiinteistötietojärjestelmä*), and UAS restriction zones (Traficom) — is sourced from free Finnish open data APIs, with attribution recorded in every manifest. Only an MML API key (free) is required.

Output files written per job:

| File | Description |
|---|---|
| `<name>.kmz` | WPML mapping route with embedded terrain-follow DSM — import into DJI Pilot 2 |
| `<name>_dsm.tif` | Terrain-follow DSM (also embedded in the KMZ, kept separately as a backup) |
| `<name>_homes.kml` | Building pins — import as a Pilot 2 custom map layer |
| `<name>_map.html` | Browser map preview — survey polygon, buildings, keep-out circles, warning radius circles, UAS zones, DSM elevation overlay; all layers toggleable |
| `manifest.json` | Full provenance record with flight stats and safety flags |
| `job_params.json` | Browser UI save state (inputs, flight params, polygon params, last preview) — used to re-open the job for editing |
| `thumbnail.svg` | Small polygon thumbnail shown in the jobs panel |
| `run.log` | Structured log for this run |

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

> **Every time you open a new terminal** you need to activate again before using `jobgen`:
>
> macOS / Linux: `source .venv/bin/activate`
> Windows: `.venv\Scripts\activate`
>
> Alternatively, call the script directly without activating:
>
> macOS / Linux: `.venv/bin/jobgen run --name my-job --parcels 5241087453`
> Windows: `.venv\Scripts\jobgen run --name my-job --parcels 5241087453`

### 2 — Install the tool

With the virtual environment active, install `jobgen` and its dependencies:

```bash
pip install -e ".[dev]"
```

This registers the `jobgen` command so you can run it by name from anywhere inside the project.

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

`jobgen` reads the `.env` file automatically on startup, so no extra steps are needed. Ruokavirasto parcel data is open and requires no key.

## Browser UI

The recommended way to use the tool is the built-in browser UI:

```bash
jobgen serve                         # opens http://localhost:8765 automatically
jobgen serve --port 8080 --no-open   # custom port, no auto-open
```

The single-page Leaflet map interface is organised around four areas: the **Jobs panel** on the left, the **editor form** in the centre, the **map** on the right, and the **⚙ Settings** gear in the header.

### Jobs panel

The panel lists all saved jobs grouped into folders. Use the header buttons to create a new job (**＋ New Job**), batch-import IDs (**↓ Batch**), or add a folder (**＋ Folder**). If a folder name already exists the dialog stays open and shows an inline error. A filter input sits below. Click any card to re-open a job — the form and map restore instantly and a fresh preview runs automatically. The three-dot card menu offers **Open**, **Clone**, **Rename**, **Move to Folder**, and **Delete**. Collapse the panel with the `◄` tab.

The panel updates live — changes made by the CLI, MCP server, or another tab appear immediately. If the currently open job is modified externally, a blue notice offers **Reload** or **Dismiss**.

**Batch import:** click **↓ Batch**, paste parcel/property IDs (one per line, `#` comments ignored) or load a `.txt`/`.csv` file, pick a folder and optional param overrides, then click **Create N jobs**. Each ID becomes a skeleton job (polygon stored, no KMZ yet). Equivalent CLI command: `jobgen batch`.

**Multi-select:** hover a card to reveal its checkbox. Select two or more to activate the toolbar: **Merge** (union polygons into a new job), **Export KML** (download selected jobs as a KML file), **Google Maps** (open navigation waypoints in Google Maps), **Route rename** (prefix each selected job with `YYYYMMDD-NN-` in route order, skipping skeleton jobs — re-running on the same selection replaces the existing prefix), **Move**, or **Delete**.

**Map view:** click **Map** on any folder header to see all its job polygons on the map. Dash pattern encodes status: solid = flight-ready, long dashes = needs review, short dashes = untouched, dotted = unknown. Hover a polygon to open a popup with the job name, status, area, and two quick actions: **⊘ Skip** (exclude the job from route ordering and counting) and **Delete**. Skipped jobs render at low opacity. Hover the popup to keep it open; mouse out to dismiss. Click a polygon to select it; double-click to open the job for editing. Ctrl+click to multi-select. Click **Map** again, open a job, or click **＋ New Job** to return to the editor.

A toolbar floats at the top of the map whenever map view is active. **Export Route** copies the `.kmz` and homes KML for every route job in the current folder to a local directory you specify — a quick way to collect all mission files before heading to the field. Route jobs are those with a computed takeoff point that have not been marked as skipped; `homes.kml` files are renamed `<job_name>_homes.kml` to avoid collisions. The remaining toolbar buttons (**Merge**, **Export KML**, **Google Maps**, **Route rename**, **Move**, **Delete**) become active when one or more jobs are selected.

**Battery / flight-time timeline:** a proportional bar appears near the bottom centre of the map whenever there is at least one routable job (a job with a computed route and a flight-time estimate in its manifest). Each segment represents one job, scaled by its estimated flight time. Route index numbers appear below each segment in the same amber circles used on the map. Battery boundaries are shown as outline battery icons above the bar: a new battery starts whenever the remaining charge (85 % of the drone's rated battery duration) is insufficient to cover the next job. The total flight time for the displayed route is shown to the right. Click any segment to pan and zoom the map to that job's polygon. When jobs are multi-selected, only the selected jobs appear on the timeline; otherwise all routable jobs in the folder are shown.

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
| Survey speed | optional per-job override; leave blank to use auto-calculated speed (see below) |
| Offset | expand (+) or contract (−) the survey polygon in metres |
| Simplify | Auto pill + −/+ step buttons |
| Keep-out | toggle to disable building buffer subtraction |

### Preview

Click **↻ Update** (or change any parameter) to run a preview. The map shows the survey polygon, original parcel outlines, keep-out circles, buildings, warning radius circles, UAS zones, DSM elevation overlay, and the flight route overlay — all layers are toggleable from the legend.

- **Power lines** — high-voltage lines (110 kV+) from MML *Maastotietokanta* (`sahkolinja`) are fetched for the same area as buildings. Overhead spans (solid amber on the map) automatically subtract a configurable keep-out buffer from the survey polygon. Underground cables (dashed amber) are shown for situational awareness only — no keep-out buffer applied. MTK misclassification is corrected automatically: any 22311-coded segment whose endpoints match pylon tower locations in `suurjannitelinjanpylvas` is re-classified as overhead before the keep-out is computed. Disable or adjust the buffer under **⚙ Settings → Power Lines**.
- **UAS zones** are clickable: see altitude floor/ceiling and all overlapping zones at a point. Inner concentric zones of an airfield are shown with a dashed border for context. The zones legend layer auto-enables when zones first appear.
- **Zone altitude cap** — when a zone hit carries an altitude floor, flight height is automatically set to 75 % of that floor and the warning radius re-syncs. An orange warning appears if you raise height above the floor. The cap is advisory; override freely.
- **Takeoff marker** — a white ✕ on the polygon boundary marks the auto-suggested takeoff/landing point (the boundary point that minimises worst-case VLOS distance). Drag it to a more convenient spot. Click **↺ Reset takeoff position** to revert. Saved with the job.
- **Route overlay** — amber lines show the planned lawnmower survey strips and all transit legs (inter-strip turns, takeoff-to-start, and return-to-home). The status panel below the map shows strip count, estimated photo count, and estimated total flight time. The route auto-computes on every parameter change; an accurate Python estimate (EPSG:3067 geometry, correct home transit distance) replaces the instant JS approximation 800 ms after input settles. Layer visibility is remembered across parameter changes and job switches. Legend eye toggles are persistent for the session.

### Polygon editing

Double-click the survey polygon to enter vertex-drag edit mode; double-click the map background to exit and save. On exit, buildings and UAS zones refresh automatically for the new shape.

In edit mode:
- White squares = vertices; smaller white diamonds = midpoints. Drag a midpoint to add a vertex. Click a vertex to delete it.
- **Bridge / Split** — right-click any vertex to start (turns orange), then left-click more vertices:
  - 2 vertices on the **same polygon** → a split line appears and a **Split job** button shows in the hint bar. Click it to divide the job into two sibling jobs, each containing one half of the polygon with all other parameters (IDs, flight settings, color) copied across.
  - 2 vertices on each of **two polygons** (4 total) → **bridge** (joins them with a quadrilateral corridor)
  - Press **Esc** or right-click to cancel.
- Click **↻ Reset polygon** to revert all edits.

### Map tools

- **Base layer** — the layer switcher (top-left, next to zoom buttons) toggles between OpenStreetMap and MML Ortokuva aerial imagery. The ortho layer requires `MML_API_KEY` in `.env`. MML's ortho tiles are natively available up to zoom 15; zooming in further upscales those tiles so the imagery stays visible for boundary editing rather than going blank.
- **Measure** — hold **Ctrl** and right-click-drag to draw a dimensioning line with a distance label. Hold **Ctrl+Shift** to draw a radius circle instead. Click **✕** in the map controls to clear all measurements.
- **Job color** — the color swatch next to the **Name** field sets the per-job display color used in map view. Saved immediately.

### Save and settings

- **Save** — writes KMZ, DSM, homes KML, HTML preview, manifest, `job_params.json`, and thumbnail to disk. Unsaved changes are tracked and you are prompted before switching jobs.
- **⚙ Settings** — opens the in-browser config editor (all sections: Flight, Safety, Polygon, UAS Zones, Cache, Output, Parcels, Properties). Changed fields highlight in amber; a search box filters across all sections. Saving hot-reloads the server and writes directly to `config.toml`. Drone profiles must be edited in `config.toml` directly; `config.example.toml` is the reference for all options.
- **ⓘ About** — the `⋯` button in the header opens the About dialog, which shows the software version and a **session statistics** table: how many tiles, parcel geometries, and zone records were fetched from the network vs. served from the local cache, and the total bytes downloaded.

Parcel and property geometries are cached locally (400-day TTL) so repeat previews do not hit the network. Building and DEM tiles are cached on a 1 km grid (configurable TTL). The same statistics are also printed to the terminal when `jobgen serve` shuts down (Ctrl-C).

---

## AI assistant integration (MCP)

`dkk-jobmaker` exposes a [Model Context Protocol](https://modelcontextprotocol.io) server so AI assistants — Claude Desktop, Claude Code, or any MCP-compatible client — can query job data and trigger pipeline operations directly.

### Primary path — integrated with the web UI

When `jobgen serve` is running, the MCP server is mounted at `/mcp/sse` in the same process. No separate command, no extra process, no coordination overhead.

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
jobgen mcp                              # stdio transport, reads config.toml from cwd
jobgen mcp --config /path/to/config.toml
```

**Claude Code:**
```bash
claude mcp add jobmaker -- jobgen mcp
```

**Claude Desktop** — use the config file path from the table above, with the platform-appropriate binary path:

macOS / Linux:
```json
{
  "mcpServers": {
    "jobmaker": {
      "command": "/path/to/project/.venv/bin/jobgen",
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
      "command": "C:\\path\\to\\project\\.venv\\Scripts\\jobgen.exe",
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
| `list_jobs` | List jobs with filters: folder, needs\_review, flight\_ready, untouched |
| `get_job` | Full details for one job — inputs, flight params, zone hits, flight status |
| `job_stats` | Aggregate stats across all jobs or a folder (total area, review counts) |
| `jobs://list` | All jobs as a resource (same as list\_jobs with no filters) |
| `jobs://{path}` | Raw params + manifest for one job |
| `config://current` | Active drone, GSD, flight and safety settings |
| `drones://list` | All drone profiles with GSD at 50/80/100 m |

**Write (pipeline operations):**

| Tool | Description |
|---|---|
| `create_folder` | Create a named job group folder |
| `create_preview` | Run geometry + UAS zone check without writing files (~10–30 s) |
| `create_batch` | Create skeleton jobs from parcel/property IDs (no KMZ) |
| `run_export` | Full pipeline — KMZ, DSM, homes KML, manifest written to disk (~30–120 s) |

Pipeline tools are serialised against the web UI: if a browser job is already running, the MCP tool returns an error immediately rather than colliding on the shared tile cache.

### Example queries

```
Which of my jobs have UAS zone conflicts?
What's the total survey area in the Vaasa-2026 folder?
Create batch jobs for parcel IDs 5241087453, 5241087454, 5241087455 in folder Seinäjoki
What drone should I use for a 12 ha field at 3 cm GSD?
Run a preview for parcel 5241087453 and tell me if there are any zone issues
```

---

## CLI usage

### Specifying the survey area

```bash
# Ruokavirasto peruslohkotunnus (comma-separated, or mixed with --properties)
jobgen run --name pelto-2024 --parcels 5241087453,5241087454

# Finnish kiinteistötunnus — dash form or 14-digit numeric
jobgen run --name pelto-2024 --properties 214-407-3-22
jobgen run --name pelto-2024 --properties 21440700030022

# Combine parcel and property IDs
jobgen run --name pelto-2024 --parcels 5241087453 --properties 214-407-3-22

# Bounding box (EPSG:3067 metres)
jobgen run --name pelto-2024 --bbox 295000,6974000,305000,6984000
```

### All `jobgen run` options

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
| `--preview-radius` | 3× height | Radius (m) of the yellow informational circle in the HTML preview — see below |
| `--simplify` | from config | Polygon vertex reduction — see below |
| `--offset` | `0` | Expand (+) or contract (−) the survey polygon by this many metres relative to the parcel boundary — see below |
| `--no-keepout` | off | Disable automatic keep-out subtraction around buildings — see below |
| `--open` | off | Open the HTML map preview in the default browser after the job completes |
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
jobgen run --name pelto-2024 --parcels 5241087453 --simplify auto

# Fixed tolerance in metres (Douglas-Peucker)
jobgen run --name pelto-2024 --parcels 5241087453 --simplify 5

# Disable (keep every vertex from the source data)
jobgen run --name pelto-2024 --parcels 5241087453 --simplify 0
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
jobgen run --name pelto-2024 --parcels 5241087453 --offset 10

# Pull the survey area 5 m inside the cadastral line (field-edge margin)
jobgen run --name pelto-2024 --parcels 5241087453 --offset -5
```

A negative offset can split the polygon or introduce holes at narrow corners — these are handled automatically by `hole_policy` / `multipart_policy` in the same way as keep-out subtraction results. If the contraction collapses the polygon entirely, the original geometry is preserved and a warning is logged.

Set `survey_offset_m` under `[polygon]` in `config.toml` (or via **⚙ Settings → Polygon**) to apply a default offset to every job.

**Offset and polygon editing:** Edit mode shows the offset-applied polygon. Exiting bakes that shape in and resets offset to 0, preventing double-application. To re-apply an offset after editing, type a new value in the Offset field after exiting edit mode.

### Disabling keep-out subtraction (`--no-keepout`)

By default the tool subtracts a buffer around buildings from the survey polygon. This produces a clean separation from structures but adds arc vertices to the polygon boundary, which can make it harder to edit on the RC touch screen.

```bash
# Skip the keep-out subtraction
jobgen run --name pelto-2024 --parcels 5241087453 --no-keepout
```

When `--no-keepout` is used:
- The survey polygon is not cut back around buildings — it covers the full parcel area.
- Buildings and their distance circles are still shown on the HTML preview map.
- A prominent red warning is added to the preview panel reminding the operator to verify distances to all buildings manually.

Use this only when you have the landowner's permission to fly close to buildings and have verified the required separation under your operating subcategory. Set `offset_enabled = false` under `[home_safety]` in `config.toml` (or via **⚙ Settings → Safety**) to make it the default.

### Drone profiles (`--drone`)

List available profiles and their GSD at typical altitudes:

```bash
jobgen drones
```

```
Name               GSD@50m   GSD@100m  Label
m3m                  1.34 cm    2.68 cm  DJI Mavic 3 Multispectral — RGB channel
m3m-ms               2.14 cm    4.28 cm  DJI Mavic 3 Multispectral — MS-limited GSD
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
jobgen run --name pelto-2024 --parcels 5241087453 --drone m300-p1-24
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
| `m3m`, `m3m-ms` | RGB + MS simultaneously (5 files ≈ 49 MB/capture) | 2.38 s | DJI Pilot 2 auto-speed: 8.9 m/s at 100 m AGL, 80% front overlap |
| `m3e` | RGB only (1 file ≈ 9 MB/capture) | 1.41 s | M3M in RGB-only mode: 15 m/s at 100 m (same sensor) |
| P1 profiles | — | 2.0 s | Estimate — calibrate from DJI Pilot 2 at a known altitude |

Example speeds for the M3M (RGB+MS) at 80% front overlap:

| Altitude | Strip speed |
|---|---|
| 30 m | ~2.7 m/s |
| 50 m | ~4.5 m/s |
| 80 m | ~7.1 m/s |
| 100 m | ~8.9 m/s |

To override with a fixed speed, set `auto_flight_speed_ms` under `[flight]` in `config.toml` or use the **Survey speed** field in the browser UI. Leave it blank (the default) to keep auto mode. To calibrate a new drone or capture mode, read the auto-speed value from DJI Pilot 2 at a known altitude and back-calculate: `interval = (1 − overlap) × altitude × (sensor_height / focal_length) / speed`.

### Batch skeleton job creation (`jobgen batch`)

Creates skeleton jobs for a list of parcel or property IDs — fetches geometry for each ID and writes `job_params.json` (no KMZ or DSM). Jobs appear in the browser UI ready to open, tune, and export.

```bash
# Inline parcel IDs
jobgen batch --parcels 5241087453,5241087454 --folder my-group

# Inline property IDs
jobgen batch --properties 214-407-3-22,214-407-3-23 --folder my-group

# IDs from a file (one per line; # comments and blank lines ignored)
jobgen batch --file ids.txt --folder my-group

# Mix inline IDs and file
jobgen batch --parcels 5241087453 --file more.txt --folder my-group

# Auto-detect ID type from format (all-digit → parcels; NNN-NNN-N-NN → properties)
jobgen batch --file ids.txt --folder my-group
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

### MCP server (standalone)

See the [AI assistant integration](#ai-assistant-integration-mcp) section above for the primary (integrated) approach. The `mcp` command is for headless use without `jobgen serve`:

```bash
jobgen mcp                               # stdio transport, config.toml in cwd
jobgen mcp --config /path/to/config.toml
```

### Cache management

Pre-fetch tiles before a field day (no network needed on-site with --offline):

```bash
# Warm the cache for an area
jobgen cache warm --bbox 295000,6974000,310000,6985000

# Check what's cached
jobgen cache status

# Refresh tiles older than 30 days
jobgen cache refresh --older-than 30
```

After `jobgen run`, `jobgen batch`, and `jobgen cache warm` complete, a session statistics table is printed showing fetches vs. cache hits per data source and total bytes downloaded:

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

The same table appears on `jobgen serve` shutdown and in the **ⓘ About** dialog in the browser UI.

## Operator workflow

### Planning (office / laptop)

1. Run `jobgen serve` and open http://localhost:8765.
2. Define the survey area — either:
   - Paste parcel or property IDs and let the polygon generate automatically, or
   - Right-click on the empty map to draw a 300×300 m scratch square, then reshape it in edit mode.
3. Adjust height, subcategory, simplify, and offset as needed.
4. Review the map: survey polygon, keep-out circles, warning radius circles, buildings, UAS zones, and DSM elevation overlay (toggle layers via the legend). Switch to MML Ortokuva aerial imagery via the layer control for ground-truth reference.
5. Edit the polygon if needed (double-click to enter, double-click background to save). Buildings and zones refresh automatically on exit.
6. Check the white ✕ takeoff/landing marker — the tool suggests a boundary point that minimises your worst-case VLOS distance to the drone. Drag it to a more accessible location if needed (e.g. closer to a road or gate). Use **↺ Reset takeoff position** to go back to the auto suggestion.
7. Click **Save** when satisfied.
8. Open `<name>_map.html` for a full-detail pre-flight review with all overlays.

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
jobgen run --name pelto-2024 --parcels 5241087453 --subcategory A3 --homes-distance 400
```

Set `home_include_buffer_m` under `[home_safety]` in `config.toml` (or via **⚙ Settings → Safety**) to change the default permanently.

### Map preview yellow circle (`--preview-radius`)

The HTML preview draws a yellow dashed circle around each keep-out building. This is a visual reference only — it does not affect the KMZ or homes KML.

The default radius is **3× derived flight height** (the "3:1 horizontal rule" sometimes used for risk assessment). At 100 m AGL the default is 300 m.

```bash
# Use a fixed 200 m circle instead of the 3:1 default
jobgen run --name pelto-2024 --parcels 5241087453 --preview-radius 200

# Tighten to 1:1 (same as the keep-out buffer)
jobgen run --name pelto-2024 --parcels 5241087453 --height 80 --subcategory A2 --preview-radius 80
```

Set `preview_radius_m` under `[home_safety]` in `config.toml` (or via **⚙ Settings → Safety**) to change the default permanently.

## Safety notes

- **120 m AGL limit** — enforced by the tool; `max_height_agl_m` in config.
- **UAS zones** — checked automatically against Traficom's published permanent zone data. The survey area is expanded by 500 m before the check so boundary-adjacent zones are also flagged. Finnish UAS vyöhykkeet (A–D) are concentric altitude bands; the browser UI treats the zone floor (`lower_limit`) as the binding altitude cap and auto-sets height to 75 % of that floor when a zone is hit. Zone data is re-fetched daily. When the survey intersects an airfield's outer zone, inner concentric zones are shown with a dashed border for context. **Temporary restrictions (NOTAMs) are NOT included** — check NOTAMs manually on the day.
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

All data sources require attribution. The manifest records the exact strings with retrieval dates. Both the browser UI map and the static HTML preview display the applicable credits in the Leaflet attribution control.

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
