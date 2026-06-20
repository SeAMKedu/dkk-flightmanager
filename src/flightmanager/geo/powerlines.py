"""Fetch overhead high-voltage power lines from MML Maastotietokanta.

Confirmed endpoint (2026-06):
  Service:    OGC API Features
  Base URL:   https://avoin-paikkatieto.maanmittauslaitos.fi/maastotiedot/features/v1
  Collection: sahkolinja
  Auth:       api-key=<key>  (query parameter)
  Response CRS: EPSG:3067 via crs=http://www.opengis.net/def/crs/EPSG/0/3067
  Bbox CRS:   EPSG:3067 via bbox-crs=http://www.opengis.net/def/crs/EPSG/0/3067

kohdeluokka codes in this collection:
  22312  ilmajohto  overhead high-voltage line (110 kV+)  → keep-out hazard
  22311  kaapeli    underground cable                     → no flight hazard, shown on map only

No explicit voltage attribute is present in the MTK data — the dataset covers
suurjännitejohto (110 kV+) only.  Voltage differentiation (110/220/400 kV)
requires Energiavirasto/Fingrid data and is out of scope here.

Known MTK data quality issue: some overhead spans between pylons are misclassified
as 22311 (kaapeli).  correct_overhead_from_pylons() detects this by cross-referencing
line endpoints against suurjannitelinjanpylvas pylon tower locations — if both
endpoints of a 22311 segment sit on pylon towers the segment is flagged overhead.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import requests
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from flightmanager.storage.cache import FetcherFn
from flightmanager.geo.crs import require_3067

log = logging.getLogger(__name__)

_BASE_URL = "https://avoin-paikkatieto.maanmittauslaitos.fi/maastotiedot/features/v1"
_COLLECTION = "sahkolinja"
_ITEMS_URL = f"{_BASE_URL}/collections/{_COLLECTION}/items"
_PYLONS_COLLECTION = "suurjannitelinjanpylvas"
_PYLONS_URL = f"{_BASE_URL}/collections/{_PYLONS_COLLECTION}/items"
_CRS_3067 = "http://www.opengis.net/def/crs/EPSG/0/3067"
_PAGE_SIZE = 1000

OVERHEAD_KOHDELUOKKA = 22312


@dataclass
class PowerLine:
    mtk_id: int
    kohdeluokka: int
    is_overhead: (
        bool  # True = overhead (22312 or pylon-corrected 22311); False = underground
    )
    geometry: BaseGeometry  # Shapely LineString, EPSG:3067
    alkupvm: str | None


@dataclass
class Pylon:
    mtk_id: int
    geometry: BaseGeometry  # Shapely Point, EPSG:3067


def tile_fetcher(
    api_key: str,
    session: requests.Session | None = None,
    timeout_s: int = 60,
) -> FetcherFn:
    """Return a FetcherFn compatible with cache.get_tiles() for the sahkolinja dataset."""
    sess = session or requests.Session()

    def _fetch(tile_id: str, tile_bbox: tuple, dest: Path) -> tuple[str, str | None]:
        xmin, ymin, xmax, ymax = tile_bbox
        features = _fetch_tile_features(
            xmin, ymin, xmax, ymax, api_key, sess, timeout_s
        )
        log.info("Power lines tile %s: %d feature(s)", tile_id, len(features))
        _write_geojson(features, dest)
        return (_ITEMS_URL, None)

    return _fetch


def load_tile(path: Path) -> list[PowerLine]:
    """Read a cached power lines GeoJSON tile and return PowerLine objects."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    result = []
    for feat in data.get("features", []):
        pl = _to_power_line(feat)
        if pl is not None:
            result.append(pl)
    return result


def dedup_power_lines(lines: list[PowerLine]) -> list[PowerLine]:
    """Remove duplicates by mtk_id (features near tile boundaries appear in multiple tiles)."""
    seen: set[int] = set()
    result: list[PowerLine] = []
    for pl in lines:
        if pl.mtk_id not in seen:
            seen.add(pl.mtk_id)
            result.append(pl)
    return result


def pylon_tile_fetcher(
    api_key: str,
    session: requests.Session | None = None,
    timeout_s: int = 60,
) -> FetcherFn:
    """Return a FetcherFn for the suurjannitelinjanpylvas (HV pylon towers) dataset."""
    sess = session or requests.Session()

    def _fetch(tile_id: str, tile_bbox: tuple, dest: Path) -> tuple[str, str | None]:
        xmin, ymin, xmax, ymax = tile_bbox
        features = _fetch_tile_features(
            xmin, ymin, xmax, ymax, api_key, sess, timeout_s, url=_PYLONS_URL
        )
        log.info("Pylons tile %s: %d feature(s)", tile_id, len(features))
        _write_geojson(features, dest)
        return (_PYLONS_URL, None)

    return _fetch


def load_pylon_tile(path: Path) -> list[Pylon]:
    """Read a cached pylon GeoJSON tile and return Pylon objects."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    result = []
    for feat in data.get("features", []):
        p = _to_pylon(feat)
        if p is not None:
            result.append(p)
    return result


def dedup_pylons(pylons: list[Pylon]) -> list[Pylon]:
    """Remove duplicates by mtk_id."""
    seen: set[int] = set()
    result: list[Pylon] = []
    for p in pylons:
        if p.mtk_id not in seen:
            seen.add(p.mtk_id)
            result.append(p)
    return result


def correct_overhead_from_pylons(
    lines: list[PowerLine],
    pylons: list[Pylon],
    tolerance_m: float = 2.0,
) -> list[PowerLine]:
    """Re-classify 22311 lines whose endpoints sit on pylon towers as overhead.

    MTK has known misclassifications where ilmajohto spans between pylons are
    coded as 22311 (kaapeli).  If both endpoints of a 22311 segment land within
    *tolerance_m* of a pylon tower, the segment is aerial and gets is_overhead=True.
    """
    if not pylons or not lines:
        return lines

    import dataclasses as _dc
    from shapely.geometry import Point
    from shapely.ops import unary_union

    pylon_zone = unary_union([p.geometry.buffer(tolerance_m) for p in pylons])

    corrected: list[PowerLine] = []
    n_fixed = 0
    for pl in lines:
        if not pl.is_overhead:
            geom = pl.geometry
            if geom.geom_type == "LineString":
                coords = list(geom.coords)
                start, end = Point(coords[0][:2]), Point(coords[-1][:2])
            elif geom.geom_type == "MultiLineString":
                first = list(geom.geoms[0].coords)
                last = list(geom.geoms[-1].coords)
                start, end = Point(first[0][:2]), Point(last[-1][:2])
            else:
                corrected.append(pl)
                continue
            if pylon_zone.contains(start) and pylon_zone.contains(end):
                pl = _dc.replace(pl, is_overhead=True)
                n_fixed += 1
        corrected.append(pl)

    if n_fixed:
        log.info(
            "Pylon cross-reference: corrected %d line(s) from underground to overhead",
            n_fixed,
        )
    return corrected


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _fetch_tile_features(
    xmin: float,
    ymin: float,
    xmax: float,
    ymax: float,
    api_key: str,
    sess: requests.Session,
    timeout_s: int,
    *,
    url: str = _ITEMS_URL,
) -> list[dict]:
    params: dict = {
        "api-key": api_key,
        "bbox": f"{xmin},{ymin},{xmax},{ymax}",
        "bbox-crs": _CRS_3067,
        "crs": _CRS_3067,
        "limit": _PAGE_SIZE,
        "f": "json",
    }
    features: list[dict] = []
    offset = 0

    while True:
        if offset > 0:
            params["offset"] = offset
        elif "offset" in params:
            del params["offset"]
        resp = sess.get(url, params=params, timeout=timeout_s)
        resp.raise_for_status()
        data = resp.json()

        page = data.get("features") or []
        features.extend(page)
        returned = int(data.get("numberReturned", len(page)))

        log.debug("Power lines page offset=%d: %d features", offset, returned)

        if returned < _PAGE_SIZE:
            break
        offset += returned

    return features


_KNOWN_KOHDELUOKKA = {OVERHEAD_KOHDELUOKKA, 22311}


def _to_power_line(feature: dict) -> PowerLine | None:
    try:
        props = feature.get("properties") or {}
        kohdeluokka = int(props.get("kohdeluokka", 0))
        if kohdeluokka not in _KNOWN_KOHDELUOKKA:
            return None
        geom = shape(feature["geometry"])
        require_3067(geom)
        return PowerLine(
            mtk_id=int(props["mtk_id"]),
            kohdeluokka=kohdeluokka,
            is_overhead=(kohdeluokka == OVERHEAD_KOHDELUOKKA),
            geometry=geom,
            alkupvm=props.get("alkupvm"),
        )
    except Exception as exc:
        log.warning("Skipping power line feature: %s", exc)
        return None


def _to_pylon(feature: dict) -> "Pylon | None":
    try:
        props = feature.get("properties") or {}
        geom = shape(feature["geometry"])
        require_3067(geom)
        return Pylon(mtk_id=int(props["mtk_id"]), geometry=geom)
    except Exception as exc:
        log.warning("Skipping pylon feature: %s", exc)
        return None


def _write_geojson(features: list[dict], dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = {"type": "FeatureCollection", "features": features}
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(payload, f)
