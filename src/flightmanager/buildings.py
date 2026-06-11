"""Fetch residential and other classified buildings from MML Maastotietokanta.

Confirmed endpoint (2025-05):
  Service:    OGC API Features (not WFS)
  Base URL:   https://avoin-paikkatieto.maanmittauslaitos.fi/maastotiedot/features/v1
  Collection: rakennus
  Auth:       api-key=<key>  (query parameter)
  Response CRS: EPSG:3067 via crs=http://www.opengis.net/def/crs/EPSG/0/3067
  Bbox CRS:   EPSG:3067 via bbox-crs=http://www.opengis.net/def/crs/EPSG/0/3067

kohdeluokka codes (confirmed empirically):
  42210–42212  asuinrakennus      residential    → keep-out A2 + A3
  42220–42222  liike-/julkinen    commercial     → keep-out A3
  42230–42232  lomarakennus       holiday        → keep-out A3
  42240–42242  teollinen          industrial     → keep-out A3
  42260–42262  maatalous/varasto  agricultural   → excluded (farm operation area)
  42270        muu                other          → excluded

Note: the CQL2 server-side filter for kohdeluokka is broken on this endpoint
(integer values are coerced to float strings, causing 400 errors). Filtering
is done client-side after fetching all buildings in the tile bbox.

This module provides a single-tile network fetcher for cache.py.  The pipeline
always calls cache.get_tiles("buildings", bbox, fetcher, config) — not this
module directly.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import requests
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from flightmanager.cache import FetcherFn
from flightmanager.config import CacheConfig, HomeSafetyConfig
from flightmanager.crs import require_3067

log = logging.getLogger(__name__)

_BASE_URL = "https://avoin-paikkatieto.maanmittauslaitos.fi/maastotiedot/features/v1"
_COLLECTION = "rakennus"
_ITEMS_URL = f"{_BASE_URL}/collections/{_COLLECTION}/items"
_CRS_3067 = "http://www.opengis.net/def/crs/EPSG/0/3067"
_PAGE_SIZE = 1000
_SOURCE_ATTRIBUTION = (
    "Contains data from the National Land Survey of Finland, "
    "Topographic Database, retrieved {date}."
)


@dataclass
class Building:
    mtk_id: int
    kohdeluokka: int
    kayttotarkoitus: int | None
    geometry: BaseGeometry   # Shapely polygon, EPSG:3067
    alkupvm: str | None      # source date (for provenance)


def tile_fetcher(
    api_key: str,
    session: requests.Session | None = None,
    timeout_s: int = 60,
) -> FetcherFn:
    """Return a FetcherFn compatible with cache.get_tiles() for the buildings dataset.

    The returned callable writes a GeoJSON file containing all buildings in the
    tile bbox to the given dest_path and returns (source_url, dataset_version).
    """
    sess = session or requests.Session()

    def _fetch(tile_id: str, tile_bbox: tuple, dest: Path) -> tuple[str, str | None]:
        xmin, ymin, xmax, ymax = tile_bbox
        features = _fetch_tile_features(xmin, ymin, xmax, ymax, api_key, sess, timeout_s)
        log.info("Buildings tile %s: %d feature(s)", tile_id, len(features))
        _write_geojson(features, dest)
        return (_ITEMS_URL, None)

    return _fetch


def load_tile(path: Path) -> list[Building]:
    """Read a cached buildings GeoJSON tile and return Building objects."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    buildings = []
    for feat in data.get("features", []):
        b = _to_building(feat)
        if b is not None:
            buildings.append(b)
    return buildings


def filter_buildings(
    buildings: list[Building],
    home_safety: HomeSafetyConfig,
) -> tuple[list[Building], list[Building]]:
    """Split buildings into (residential, a3_additional) based on kohdeluokka.

    residential   — always used as keep-out (A2 and A3)
    a3_additional — also used as keep-out when operating_subcategory == "A3"
    """
    res_codes = set(home_safety.residential_kohdeluokka)
    a3_codes = set(home_safety.a3_additional_kohdeluokka)

    residential = [b for b in buildings if b.kohdeluokka in res_codes]
    a3_additional = [b for b in buildings if b.kohdeluokka in a3_codes]
    return residential, a3_additional


def dedup_buildings(buildings: list[Building]) -> list[Building]:
    """Remove duplicates by mtk_id (buildings near tile boundaries appear in multiple tiles)."""
    seen: set[int] = set()
    result: list[Building] = []
    for b in buildings:
        if b.mtk_id not in seen:
            seen.add(b.mtk_id)
            result.append(b)
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _fetch_tile_features(
    xmin: float, ymin: float, xmax: float, ymax: float,
    api_key: str,
    sess: requests.Session,
    timeout_s: int,
) -> list[dict]:
    """Fetch all buildings in the EPSG:3067 bbox via OGC API Features paging."""
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
        resp = sess.get(_ITEMS_URL, params=params, timeout=timeout_s)
        resp.raise_for_status()
        data = resp.json()

        page = data.get("features") or []
        features.extend(page)
        returned = int(data.get("numberReturned", len(page)))

        log.debug("Buildings page offset=%d: %d features", offset, returned)

        if returned < _PAGE_SIZE:
            break
        offset += returned

    return features


def _to_building(feature: dict) -> Building | None:
    try:
        props = feature.get("properties") or {}
        geom = shape(feature["geometry"])
        require_3067(geom)
        return Building(
            mtk_id=int(props["mtk_id"]),
            kohdeluokka=int(props.get("kohdeluokka", 0)),
            kayttotarkoitus=props.get("kayttotarkoitus"),
            geometry=geom,
            alkupvm=props.get("alkupvm"),
        )
    except Exception as exc:
        log.warning("Skipping building feature: %s", exc)
        return None


def _write_geojson(features: list[dict], dest: Path) -> None:
    """Write a FeatureCollection GeoJSON to dest."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = {"type": "FeatureCollection", "features": features}
    with open(dest, "w", encoding="utf-8") as f:
        json.dump(payload, f)

