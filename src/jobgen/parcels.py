"""Fetch Finnish field parcel polygons from the Ruokavirasto INSPIRE WFS.

Confirmed endpoint (GetCapabilities 2025-05):
  https://inspire.ruokavirasto-awsa.com/geoserver/wfs
  Layer: inspire:LC.LandCoverSurfaces.LPIS.<year>
  CRS:   EPSG:3067  (ETRS-TM35FIN)
  Auth:  none — open CC-BY 4.0
  Key property: PERUSLOHKOTUNNUS  (parcel identifier)

No API key is required for this service.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.wkt import dumps as wkt_dumps, loads as wkt_loads

import jobgen.net_stats as _ns
from jobgen.config import CacheConfig, ParcelsConfig
from jobgen.crs import require_3067

log = logging.getLogger(__name__)

_WFS_URL = "https://inspire.ruokavirasto-awsa.com/geoserver/wfs"
_LAYER_TEMPLATE = "inspire:LC.LandCoverSurfaces.LPIS.{year}"
_ID_FIELD = "PERUSLOHKOTUNNUS"
_SOURCE_ATTRIBUTION = (
    "Contains data from Ruokavirasto (Finnish Food Authority), "
    "Peltolohkorekisteri, retrieved {date}."
)


class ParcelNotFoundError(KeyError):
    """Raised when one or more requested parcel IDs are not in the WFS response."""


@dataclass
class Parcel:
    parcel_id: str          # PERUSLOHKOTUNNUS
    tunnus: int             # TUNNUS (internal numeric ID)
    year: int               # VUOSI
    area_ha: float          # PINTA_ALA
    geometry: BaseGeometry  # Shapely, EPSG:3067


def fetch_parcels(
    *,
    parcel_ids: list[str] | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    config: ParcelsConfig | None = None,
    cache_config: CacheConfig | None = None,
    session: requests.Session | None = None,
) -> list[Parcel]:
    """Return Parcel objects from the Ruokavirasto INSPIRE WFS.

    Exactly one of *parcel_ids* or *bbox* must be provided.

    *parcel_ids*: list of PERUSLOHKOTUNNUS strings.  Raises ParcelNotFoundError
        if any ID is missing from the response.  Results are cached in the
        tile-cache SQLite index when *cache_config* is provided (TTL: 400 days).
    *bbox*: (xmin, ymin, xmax, ymax) in EPSG:3067 metres.  Bbox fetches are
        not cached (unbounded queries).

    All returned geometries are in EPSG:3067.
    """
    if (parcel_ids is None) == (bbox is None):
        raise ValueError("Provide exactly one of parcel_ids or bbox.")

    cfg = config or ParcelsConfig()
    layer = _LAYER_TEMPLATE.format(year=cfg.lpis_year)
    sess = session or requests.Session()

    if parcel_ids is not None:
        parcels = _fetch_by_ids_cached(parcel_ids, layer, cfg, sess, cache_config)
        _check_missing(parcel_ids, parcels)
    else:
        log.info("Fetching parcels from %s layer=%s mode=bbox", _WFS_URL, layer)
        features = _fetch_by_bbox(bbox, layer, cfg, sess)  # type: ignore[arg-type]
        parcels = [_to_parcel(f) for f in features]

    log.info("Retrieved %d parcel(s)", len(parcels))
    return parcels


def _fetch_by_ids_cached(
    parcel_ids: list[str],
    layer: str,
    cfg: ParcelsConfig,
    sess: requests.Session,
    cache_config: CacheConfig | None,
) -> list[Parcel]:
    """Serve ID-based parcel fetch from cache where possible; network for the rest."""
    from jobgen.geo_cache import get_parcel_cache, put_parcel_cache

    if cache_config is None:
        log.info("Fetching parcels from %s layer=%s mode=ids (no cache)", _WFS_URL, layer)
        return [_to_parcel(f) for f in _fetch_by_ids(parcel_ids, layer, cfg, sess)]

    cached: list[Parcel] = []
    missing_ids: list[str] = []

    for pid in parcel_ids:
        record = get_parcel_cache(cache_config, pid, cfg.lpis_year)
        if record is not None:
            geom = wkt_loads(record.geometry_wkt)
            cached.append(Parcel(
                parcel_id=record.parcel_id,
                tunnus=record.tunnus,
                year=record.lpis_year,
                area_ha=record.area_ha,
                geometry=geom,
            ))
            _ns.record_hit("parcels")
            log.debug("Parcel cache hit: %s year=%d", pid, cfg.lpis_year)
        else:
            missing_ids.append(pid)

    if missing_ids:
        log.info(
            "Fetching parcels from %s layer=%s mode=ids (%d/%d not cached)",
            _WFS_URL, layer, len(missing_ids), len(parcel_ids),
        )
        features = _fetch_by_ids(missing_ids, layer, cfg, sess)
        fetched = [_to_parcel(f) for f in features]
        for p in fetched:
            put_parcel_cache(
                cache_config, p.parcel_id, p.year, p.tunnus, p.area_ha,
                wkt_dumps(p.geometry),
            )
        cached.extend(fetched)
    else:
        log.info("All %d parcel(s) served from cache", len(parcel_ids))

    return cached


# ---------------------------------------------------------------------------
# Internal fetch helpers
# ---------------------------------------------------------------------------


def _base_params(layer: str, page_size: int) -> dict:
    return {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": layer,
        "outputFormat": "application/json",
        "count": page_size,
    }


def _fetch_by_ids(
    parcel_ids: list[str],
    layer: str,
    cfg: ParcelsConfig,
    sess: requests.Session,
) -> list[dict]:
    """Fetch features matching a list of PERUSLOHKOTUNNUS values via CQL_FILTER."""
    quoted = ", ".join(f"'{pid}'" for pid in parcel_ids)
    cql = f"{_ID_FIELD} IN ({quoted})"

    params = _base_params(layer, cfg.page_size)
    params["CQL_FILTER"] = cql

    return _paginate(params, cfg, sess)


def _fetch_by_bbox(
    bbox: tuple[float, float, float, float],
    layer: str,
    cfg: ParcelsConfig,
    sess: requests.Session,
) -> list[dict]:
    """Fetch features intersecting *bbox* (EPSG:3067) via BBOX parameter."""
    xmin, ymin, xmax, ymax = bbox
    params = _base_params(layer, cfg.page_size)
    # WFS 2.0 KVP: BBOX=minx,miny,maxx,maxy,srsName
    params["BBOX"] = f"{xmin},{ymin},{xmax},{ymax},EPSG:3067"

    return _paginate(params, cfg, sess)


def _paginate(
    base_params: dict,
    cfg: ParcelsConfig,
    sess: requests.Session,
) -> list[dict]:
    """Collect all features, following WFS 2.0 startIndex paging."""
    features: list[dict] = []
    start = 0

    while True:
        params = {**base_params, "startIndex": start}
        resp = sess.get(_WFS_URL, params=params, timeout=cfg.timeout_s)
        resp.raise_for_status()
        _ns.record_download("parcels", len(resp.content))
        data = resp.json()

        page = data.get("features") or []
        features.extend(page)

        returned = int(data.get("numberReturned", len(page)))
        matched = data.get("numberMatched")

        log.debug(
            "Page startIndex=%d: got %d features (matched=%s)",
            start, returned, matched,
        )

        # Stop when the server tells us we have everything, or the page is short.
        if matched is not None and len(features) >= int(matched):
            break
        if returned < cfg.page_size:
            break
        start += returned

    return features


# ---------------------------------------------------------------------------
# Feature → Parcel
# ---------------------------------------------------------------------------


def _to_parcel(feature: dict) -> Parcel:
    props = feature.get("properties") or {}
    geom = shape(feature["geometry"])
    require_3067(geom)
    return Parcel(
        parcel_id=str(props.get(_ID_FIELD, "")),
        tunnus=int(props.get("TUNNUS", 0)),
        year=int(props.get("VUOSI", 0)),
        area_ha=float(props.get("PINTA_ALA", 0.0)),
        geometry=geom,
    )


def _check_missing(requested: list[str], parcels: list[Parcel]) -> None:
    returned_ids = {p.parcel_id for p in parcels}
    missing = [pid for pid in requested if pid not in returned_ids]
    if missing:
        raise ParcelNotFoundError(
            f"{len(missing)} parcel ID(s) not found in WFS response: {missing}"
        )
