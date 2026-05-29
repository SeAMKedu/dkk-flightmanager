# mavic3e-jobgen

DJI Mavic 3 Enterprise terrain-following mapping job generator for Finnish field parcels.

Given a list of Ruokavirasto *peruslohko* parcel IDs (or a bounding box), produces a
ready-to-fly DJI Pilot 2 mapping job: a `.kmz` WPML route file, a reprojected DSM
GeoTIFF for terrain-follow, and a `homes.geojson` layer of nearby residential buildings.

## Setup

```bash
cp config.example.toml config.toml   # edit flight params, buffer distances, etc.
cp .env.example .env                  # add your MML_API_KEY
pip install -e ".[dev]"
```

## Reference fixtures

`tests/test_wpml.py` validates generated KMZ output against a reference mission exported
from DJI Pilot 2. These files are **gitignored** (flight data) and must be provided locally.

### Required files

| File | How to get it |
|---|---|
| `fixtures/reference_mission.kmz` | Export a mapping (survey) mission from DJI Pilot 2 on the M3E with terrain-follow enabled, then copy the `.kmz` off the RC. |
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
