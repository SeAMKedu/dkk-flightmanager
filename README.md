# dkk-jobgen

DJI terrain-following mapping job generator for Finnish agricultural field parcels.

`dkk-jobgen` is a planning tool for drone mapping surveys over Finnish farmland. Identify the survey area by pasting *peruslohkotunnus* field parcel IDs (Ruokavirasto), *kiinteistötunnus* cadastral property IDs, a bounding box, or a polygon drawn directly on the map — the tool fetches field boundaries, 2 m terrain elevation, and building footprints from National Land Survey of Finland (MML) open data APIs, checks Traficom UAS restriction zones, and writes a ready-to-fly DJI Pilot 2 mapping job. A built-in browser UI handles everything from parcel lookup and polygon editing to flight parameter tuning and batch job creation for large parcel sets.

All underlying data — field boundaries (Ruokavirasto *Peltolohkorekisteri*), terrain elevation and buildings (MML *Maastotietokanta*), cadastral geometry (MML *Kiinteistötietojärjestelmä*), and UAS restriction zones (Traficom) — is sourced from free Finnish open data APIs, with attribution recorded in every manifest. Only an MML API key (free) is required.

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

```bash
python3 -m venv .venv        # create the virtual environment in .venv/
source .venv/bin/activate    # activate it (your prompt will change to show (.venv))
```

> **Every time you open a new terminal** you need to activate again before using `jobgen`:
> ```bash
> source .venv/bin/activate
> ```
>
> Alternatively, you can call the script directly without activating — useful in shell scripts or if you prefer not to activate:
> ```bash
> .venv/bin/jobgen run --name my-job --parcels 5241087453
> ```

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

The single-page Leaflet map interface lets you:

- **Manage jobs** — the **Jobs panel** on the left lists all saved jobs with thumbnails, area, and status badges. Click any card to re-open a job (form and map restore instantly; a fresh preview runs automatically). Jobs can be organised into **group folders** — each folder section is collapsible, and every job card has a three-dot menu with **Open**, **Clone**, **Rename**, **Move to Folder**, and **Delete**. Click **＋ New Job** to start a fresh job. The panel can be collapsed with the `◄` tab on its right edge.
- **Batch-create skeleton jobs** — click **↓ Batch** to open the batch dialog. Paste a list of parcel or property IDs (one per line, `#` comments ignored), or load a `.txt`/`.csv` file. Assign a group folder and optional flight param overrides, then click **Create N jobs** — each ID becomes a skeleton job (polygon stored, no KMZ yet) ready to open and edit. The same operation is available from the CLI with `jobgen batch`.
- **Multi-select and bulk operations** — hover over any job card to reveal a checkbox. Check two or more jobs to activate the selection toolbar: **Merge** (union their polygons into a new job), **Move** (send to a folder), or **Delete** all at once.
- **Map view** — click the □ button in the panel header (or the **Map** button on any folder section header) to open a full-screen job map. All job polygons are shown with their assigned **map color**; **dash pattern** encodes status: solid = flight-ready, long dashes = needs review, short dashes = untouched batch job, dotted = unknown. Click a polygon for a popup with Open/Delete; Ctrl+click to select multiple polygons for bulk operations. A folder filter dropdown lets you focus on one group. Closing the overlay returns to the editor.
- **Job map color** — a small color swatch next to the **Name** field assigns a per-job display color used in the map view. The change is saved immediately. The job editor map always stays blue regardless of this setting.
- **Enter area IDs** — paste Ruokavirasto parcel IDs or MML kiinteistötunnus values; the map updates automatically when you leave the field.
- **Draw a scratch polygon** — if you have no parcel or property IDs, right-click anywhere on the empty map to create a 300×300 m square centred on the cursor. The map enters vertex-drag edit mode immediately so you can reshape it freely. Parcel/property IDs are not required — the drawn polygon is the sole input for preview and save.
- **Tune flight parameters** — subcategory (A2/A3 pills), drone, height (live GSD display), and warning radius (linked to 3× height by default; click the "3:1" label to restore the link after manual override).
- **Tune polygon** — offset (expand/contract), simplify (Auto pill + −/+ step buttons), keep-out toggle.
- **Preview the survey** — click **↻ Update** or edit any parameter to see the survey polygon, original parcel outlines, keep-out circles, buildings, warning radius circles, UAS zones, and a DSM elevation overlay — all toggleable from the legend. UAS zone polygons are sorted so inner zones render on top and are clickable even when nested inside a larger zone. Click any zone to see all overlapping zones at that point, including altitude floor/ceiling ranges and nesting relationships. Inner concentric zones of an airfield that don't directly intersect the survey buffer are shown with a dashed border for context. The UAS zones legend layer automatically turns on when zones first appear.
- **Edit the polygon** — double-click the survey polygon to enter vertex-drag edit mode; double-click the map background to exit and save. Exiting edit mode automatically refreshes buildings and UAS zones for the new polygon shape. In edit mode, vertex handles are white squares and midpoint handles are smaller white diamonds — drag a midpoint to create a new vertex. Click a vertex to delete it. Clicking **Save** while still in edit mode auto-commits the edit before saving. Click **↻ Reset polygon** to revert all manual edits.
- **Bridge / Cut** — in edit mode, right-click any vertex to enter Bridge/Cut mode (the vertex turns orange). Newly created vertices (promoted midpoints) are immediately available for selection. Left-click up to three more vertices to define the operation:
  - **3 vertices on the same polygon** → triangle cut (subtracts the triangle from the polygon)
  - **2 vertices on each of two separate polygons** → bridge (connects them into a single continuous polygon with a quadrilateral corridor)
  - Selected vertices highlight orange as you pick them; a dashed preview line shows the shape. Right-click anywhere or press **Esc** to cancel. The **♦ Bridge / Cut** button in the Polygon section is an alternative entry point.
- **Zone altitude cap** — when a preview returns zone hits that carry an altitude floor (Finnish vyöhyke B/C/D), flight height is automatically set to 75 % of the lowest floor and the warning radius re-syncs. Raising height above the floor triggers an orange warning in the status panel. The cap is advisory; you can override freely.
- **Map base layer** — a layer switcher in the top-left corner (next to the zoom buttons) toggles between OpenStreetMap and MML Ortokuva aerial imagery. The ortho layer requires a valid `MML_API_KEY` in `.env` and is served directly from MML's WMTS; browser HTTP cache handles tile caching automatically.
- **Save** — click **Save** to write the full job (KMZ, DSM, homes KML, HTML preview, manifest, `job_params.json`, thumbnail) to disk. Unsaved changes are tracked; you will be prompted before opening a different job or starting a new one.

Parcel and property geometries are cached locally (400-day TTL) so repeat previews of the same area do not hit the network. Building and DEM tiles are cached on a 1 km grid (configurable TTL).

---

## CLI usage

### Specifying the survey area

```bash
# Ruokavirasto peruslohkotunnus (comma-separated, or mixed with --properties)
jobgen run --name pelto-2024 --parcels 5241087453,5241087454

# From a file of parcel IDs (one per line)
jobgen run --name pelto-2024 --parcels-file ids.txt

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
| `--parcels-file` | — | File with one parcel ID per line |
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
# Auto: find the largest simplification that keeps the polygon ≤ 50 vertices
jobgen run --name pelto-2024 --parcels 5241087453 --simplify auto

# Fixed tolerance in metres (Douglas-Peucker)
jobgen run --name pelto-2024 --parcels 5241087453 --simplify 5

# Disable (keep every vertex from the source data)
jobgen run --name pelto-2024 --parcels 5241087453 --simplify 0
```

The default tolerance and mode are set in `config.toml` under `[polygon]`:

```toml
simplify_mode = "fixed"           # "fixed" or "auto"
simplify_tolerance_m = 1.0        # metres; used when simplify_mode = "fixed"
auto_simplify_max_vertices = 50   # vertex target for simplify_mode = "auto"
```

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

Set `survey_offset_m` in `config.toml` under `[polygon]` to apply a default offset to every job.

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

Use this only when you have the landowner's permission to fly close to buildings and have verified the required separation under your operating subcategory. Set `offset_enabled = false` in `config.toml` under `[home_safety]` to make it the default.

### Drone profiles (`--drone`)

The tool ships with built-in profiles for common DJI mapping drones. Each profile
carries the WPML drone/payload enum values and camera constants needed to generate
a correct KMZ and calculate GSD.

List available profiles and their GSD at typical altitudes:

```bash
jobgen drones
```

```
Name               GSD@50m  GSD@100m  Label
m3m *                1.34 cm    2.68 cm  DJI Mavic 3 Multispectral — RGB channel
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

Set the default in `config.toml`:

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

## Operator workflow

### Planning (office / laptop)

1. Run `jobgen serve` and open http://localhost:8765.
2. Define the survey area — either:
   - Paste parcel or property IDs and let the polygon generate automatically, or
   - Right-click on the empty map to draw a 300×300 m scratch square, then reshape it in edit mode.
3. Adjust height, subcategory, simplify, and offset as needed.
4. Review the map: survey polygon, keep-out circles, warning radius circles, buildings, UAS zones, and DSM elevation overlay (toggle layers via the legend). Switch to MML Ortokuva aerial imagery via the layer control for ground-truth reference.
5. Edit the polygon if needed (double-click to enter, double-click background to save). Buildings and zones refresh automatically on exit.
6. Click **Save** when satisfied.
7. Open `<name>_map.html` for a full-detail pre-flight review with all overlays.

### On the RC

1. Copy `<name>.kmz` and `<name>_homes.kml` to the RC via USB.
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

The `operating_subcategory` in `config.toml` sets the default; override per-job with `--subcategory`.

### Homes KML inclusion distance (`--homes-distance`)

Buildings are included in `<name>_homes.kml` if their nearest point is within this distance of the survey polygon boundary. The default is 2× the keep-out buffer (e.g. 300 m for A3, 2× flight height for A2) — wide enough to capture buildings that sit just outside the keep-out zone but are still visible from the survey area.

```bash
# Include buildings up to 400 m from the polygon (A3 job)
jobgen run --name pelto-2024 --parcels 5241087453 --subcategory A3 --homes-distance 400
```

Set `home_include_buffer_m` in `config.toml` under `[home_safety]` to change the default permanently.

### Map preview yellow circle (`--preview-radius`)

The HTML preview draws a yellow dashed circle around each keep-out building. This is a visual reference only — it does not affect the KMZ or homes KML.

The default radius is **3× derived flight height** (the "3:1 horizontal rule" sometimes used for risk assessment). At 100 m AGL the default is 300 m.

```bash
# Use a fixed 200 m circle instead of the 3:1 default
jobgen run --name pelto-2024 --parcels 5241087453 --preview-radius 200

# Tighten to 1:1 (same as the keep-out buffer)
jobgen run --name pelto-2024 --parcels 5241087453 --height 80 --subcategory A2 --preview-radius 80
```

Set `preview_radius_m` in `config.toml` under `[home_safety]` to change the default permanently.

## Safety notes

- **120 m AGL limit** — enforced by the tool; `max_height_agl_m` in config.
- **UAS zones** — checked automatically against Traficom's published permanent zone data. The survey area is expanded by 500 m before the check so zones near the boundary are also flagged. Finnish UAS vyöhykkeet (A–D) are concentric altitude bands; the browser UI reads the zone *floor* (`lower_limit`) as the binding altitude cap — flying below it exits that zone without authorisation. The zone dump is re-fetched daily. **Temporary restrictions (NOTAMs) are NOT included** — check NOTAMs manually on the day.
- **Inner zones shown for context** — when the survey intersects the outer zone of an airfield, all inner concentric zones (tighter vyöhykkeet with lower altitude limits) are also displayed with a dashed border so you can judge how close you are to the more restrictive inner areas.
- **Altitude auto-cap** — when zone hits carry an altitude floor, the browser UI automatically sets flight height to 75 % of that floor and warns if you raise it above the limit. The cap is advisory only; override requires no confirmation, but the warning persists.
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
| Parcels (Ruokavirasto) | Contains data from Ruokavirasto (Finnish Food Authority), Peltolohkorekisteri, retrieved \<date\>. |
| Properties (Kiinteistötietojärjestelmä) | Contains data from the National Land Survey of Finland, Cadastral Index Map, retrieved \<date\>. |
| UAS zones | Contains data from Traficom, UAS Geographical Zones, retrieved \<date\>. |

---

## Project

This tool was developed as part of the **[Datakasvukunto](https://projektit.seamk.fi/kestavat-ruokaratkaisut/datakasvukunto/)** project at [Seinäjoki University of Applied Sciences (SeAMK)](https://www.seamk.fi).

More projects from SeAMK: [github.com/SeAMKedu](https://github.com/SeAMKedu)
