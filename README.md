# mavic3e-jobgen

DJI Mavic 3 Enterprise terrain-following mapping job generator for Finnish field parcels.

Given a list of Ruokavirasto *peruslohko* parcel IDs (or a bounding box), produces a
ready-to-fly DJI Pilot 2 mapping job:

| File | Description |
|---|---|
| `<name>.kmz` | WPML mapping route — import into DJI Pilot 2 |
| `<name>_dsm.tif` | Terrain-follow DSM — link to the route on the RC |
| `<name>_homes.kml` | Building pins — import as a Pilot 2 map layer |
| `manifest.json` | Full provenance record with flight stats and safety flags |
| `run.log` | Structured log for this run |

## Setup

```bash
cp config.example.toml config.toml   # edit flight params and buffer distances
cp .env.example .env                  # add your MML_API_KEY
pip install -e ".[dev]"
```

The MML API key (free) is required for elevation and building data.
Obtain one at https://www.maanmittauslaitos.fi/rajapinnat/api-avaimen-ohje

Ruokavirasto parcel data is open and requires no key.

## Usage

```bash
# Generate a job by parcel IDs (comma-separated peruslohkotunnus)
jobgen run --name jalasto-north --parcels 1641355689,1641355690

# From a file of IDs (one per line)
jobgen run --name jalasto-north --parcels-file ids.txt

# From a bounding box (EPSG:3067 metres)
jobgen run --name jalasto-north --bbox 295000,6974000,305000,6984000

# Flags
--dry-run      Fetch and validate only — no output files written
--offline      Cache-only; fail cleanly if tiles are missing (use after cache warm)
--refresh      Force re-download of all touched tiles
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

## Operator workflow (on the RC)

1. Copy `<name>.kmz`, `<name>_dsm.tif`, and `<name>_homes.kml` to the RC via USB.
2. Open DJI Pilot 2 → **Routes** → import `<name>.kmz`.
3. In the route editor, set **Terrain Follow** and point the DSM field at `<name>_dsm.tif`.
4. In the map view → **Custom layers** → import `<name>_homes.kml` to see the building pins.
5. Verify the height readout over the field looks correct (~100 m AGL).
6. **Never fly a job with `flight_ready: false` or `needs_review: true` in the manifest
   without a human check** — these flags indicate home-distance, zone, or geometry issues.

## Safety notes

- **120 m AGL limit** — enforced by the tool; `max_height_agl_m` in config.
- **Home keep-out** — defaults to 150 m (A3 subcategory). Confirm the drone's
  C-label marking and check Traficom guidance before changing to A2 rules.
- **UAS zones** — checked against Traficom's published zone data automatically.
  Temporary restrictions (NOTAMs) are NOT in this dataset — check NOTAMs manually
  on the day of the flight.
- The generated job is a planning aid. The remote pilot remains responsible for
  compliance, airspace checks, and uninvolved-person separation on the day.

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

## Attribution (CC-BY 4.0)

- Elevation: "Contains data from the National Land Survey of Finland, Elevation model 2 m, retrieved \<date\>."
- Buildings: "Contains data from the National Land Survey of Finland, Topographic Database, retrieved \<date\>."
- Parcels: "Contains data from Ruokavirasto (Finnish Food Authority), Peltolohkorekisteri, retrieved \<date\>."
