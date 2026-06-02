# dkk-jobgen

DJI terrain-following mapping job generator for Finnish field parcels.

Given Ruokavirasto *peruslohko* parcel IDs, MML *kiinteistötunnus* property IDs, or a
bounding box, produces a ready-to-fly DJI Pilot 2 mapping job:

| File | Description |
|---|---|
| `<name>.kmz` | WPML mapping route with embedded terrain-follow DSM — import into DJI Pilot 2 |
| `<name>_dsm.tif` | Terrain-follow DSM (also embedded in the KMZ, kept separately as a backup) |
| `<name>_homes.kml` | Building pins — import as a Pilot 2 custom map layer |
| `<name>_map.html` | Browser map preview — survey polygon, buildings, keep-out circles, warning radius circles, UAS zones, DSM elevation overlay; all layers toggleable |
| `manifest.json` | Full provenance record with flight stats and safety flags |
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

- **Enter area IDs** — paste Ruokavirasto parcel IDs or MML kiinteistötunnus values; the map updates automatically when you leave the field.
- **Tune flight parameters** — subcategory (A2/A3 pills), drone, height (live GSD display), and warning radius (linked to 3× height by default; click the "3:1" label to restore the link after manual override).
- **Tune polygon** — offset (expand/contract), simplify (Auto pill + −/+ step buttons), keep-out toggle.
- **Preview the survey** — click **↻ Update** or edit any parameter to see the survey polygon, original parcel outlines, keep-out circles, buildings, warning radius circles, UAS zones, and a DSM elevation overlay — all toggleable from the legend.
- **Edit the polygon** — double-click the survey polygon to enter vertex-drag edit mode; double-click the map background to save. Click **↻ Reset polygon** to revert.
- **Export** — click **↓ Export KMZ** to write the full job (KMZ, DSM, homes KML, HTML preview, manifest) to disk.
- **New job** — click **＋ New job** to reset the editor for a new area without restarting the server.

The preview is fast on repeat runs because fetched parcel and building data is cached in memory between calls — only geometry and zone checks rerun when you change flight parameters.

---

## CLI Usage

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
2. Paste parcel or property IDs — the survey polygon appears automatically.
3. Adjust height, subcategory, simplify, and offset as needed.
4. Review the map: survey polygon, keep-out circles, warning radius circles, buildings, UAS zones, and DSM elevation overlay (toggle layers via the legend).
5. Edit the polygon if needed (double-click to enter, double-click background to save).
6. Click **↓ Export KMZ** when satisfied.
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
- **UAS zones** — checked automatically against Traficom's published zone data.
  This is a static dump of permanent zones — temporary restrictions (NOTAMs) are
  NOT included. Check NOTAMs manually on the day of the flight.
- The generated job is a planning aid. The remote pilot remains responsible for
  compliance, airspace checks, and uninvolved-person separation on the day.

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

- Elevation: "Contains data from the National Land Survey of Finland, Elevation model 2 m, retrieved \<date\>."
- Buildings: "Contains data from the National Land Survey of Finland, Topographic Database, retrieved \<date\>."
- Parcels: "Contains data from Ruokavirasto (Finnish Food Authority), Peltolohkorekisteri, retrieved \<date\>."

---

## Project

This tool was developed as part of the **[Datakasvukunto](https://projektit.seamk.fi/kestavat-ruokaratkaisut/datakasvukunto/)** project at [Seinäjoki University of Applied Sciences (SeAMK)](https://www.seamk.fi).

More projects from SeAMK: [github.com/SeAMKedu](https://github.com/SeAMKedu)
