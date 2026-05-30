"""Fetch Finnish cadastral parcel (palsta) polygons from the MML kiinteistö OGC API.

Endpoint (OGC API Features v3, open CC-BY 4.0):
  https://avoin-paikkatieto.maanmittauslaitos.fi/kiinteisto-avoin/simple-features/v3/
  Collection: PalstanSijaintitiedot
  Auth:  api-key query parameter (same MML_API_KEY as elevation + buildings)
  CRS:   request EPSG:3067 explicitly; confirmed via Content-Crs response header

Key facts confirmed by probe (2026-05-30):
  - kiinteistötunnus must be 14-digit numeric (no dashes) — dash form returns HTTP 400.
  - One kiinteistötunnus can map to multiple palstat (non-contiguous pieces) — union them.
  - Response properties: kiinteistotunnus, kiinteistotunnuksenEsitysmuoto, centroid point.
  - No area field in response — compute from geometry.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests
from shapely.geometry import shape
from shapely.ops import unary_union
from shapely.geometry.base import BaseGeometry

from jobgen.crs import require_3067

log = logging.getLogger(__name__)

_BASE_URL = (
    "https://avoin-paikkatieto.maanmittauslaitos.fi"
    "/kiinteisto-avoin/simple-features/v3"
    "/collections/PalstanSijaintitiedot/items"
)
_CRS_3067 = "http://www.opengis.net/def/crs/EPSG/0/3067"
_SOURCE_ATTRIBUTION = (
    "Contains data from the National Land Survey of Finland, "
    "Cadastral Index Map, retrieved {date}."
)


class PropertyNotFoundError(KeyError):
    """Raised when one or more requested kiinteistötunnukset are not found."""


@dataclass
class Property:
    property_id: str        # kiinteistötunnus, 14-digit numeric form
    display_id: str         # kiinteistötunnuksenEsitysmuoto, dash form e.g. "399-891-1-1"
    area_ha: float          # computed from geometry (no area field in API response)
    geometry: BaseGeometry  # Shapely, EPSG:3067; unioned if multiple palstat


def fetch_properties(
    property_ids: list[str],
    api_key: str,
    *,
    timeout_s: int = 60,
    page_size: int = 100,
    session: requests.Session | None = None,
) -> list[Property]:
    """Return Property objects for the given kiinteistötunnukset.

    *property_ids* may be in either dash form ("399-891-1-1") or 14-digit
    numeric form ("39989100010001") — both are normalised internally.

    Multiple palstat with the same kiinteistötunnus are unioned into one
    Property so callers always get one geometry per input ID.

    Raises PropertyNotFoundError if any ID returns no features.
    """
    sess = session or requests.Session()

    normalised = {_normalise(pid): pid for pid in property_ids}
    log.info("Fetching %d kiinteistö(t) from MML OGC API", len(normalised))

    results: dict[str, Property] = {}
    for numeric_id, original in normalised.items():
        features = _fetch_one(numeric_id, api_key, timeout_s, page_size, sess)
        if not features:
            msg = f"Kiinteistötunnus not found: {original!r} (normalised: {numeric_id!r})"
            log.error(msg)
            raise PropertyNotFoundError(msg)
        prop = _to_property(numeric_id, features)
        results[numeric_id] = prop
        log.debug(
            "  %s → %s  %.2f ha  %d palsta(t)",
            prop.display_id, prop.property_id, prop.area_ha, len(features),
        )

    log.info("Retrieved %d kiinteistö(t)", len(results))
    return list(results.values())


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalise(tunnus: str) -> str:
    """Convert any kiinteistötunnus form to the 14-digit numeric form required by the API.

    Accepts:
      "399-891-1-1"     → "39989100010001"
      "39989100010001"  → "39989100010001"  (already normalised)
    """
    tunnus = tunnus.strip()
    if "-" in tunnus:
        parts = tunnus.split("-")
        if len(parts) != 4:
            raise ValueError(
                f"Invalid kiinteistötunnus format: {tunnus!r}. "
                "Expected 'KKKK-RRR-GG-TTTT' or 14-digit numeric."
            )
        kunta, sijainti, ryhma, yksikko = parts
        return kunta.zfill(3) + sijainti.zfill(3) + ryhma.zfill(4) + yksikko.zfill(4)
    if len(tunnus) == 14 and tunnus.isdigit():
        return tunnus
    raise ValueError(
        f"Invalid kiinteistötunnus format: {tunnus!r}. "
        "Expected 'KKKK-RRR-GG-TTTT' or 14-digit numeric."
    )


def _fetch_one(
    numeric_id: str,
    api_key: str,
    timeout_s: int,
    page_size: int,
    sess: requests.Session,
) -> list[dict]:
    """Fetch all palsta features for one 14-digit kiinteistötunnus."""
    features: list[dict] = []
    offset = 0

    while True:
        params: dict = {
            "kiinteistotunnus": numeric_id,
            "crs": _CRS_3067,
            "limit": page_size,
            "api-key": api_key,
        }
        if offset:
            params["offset"] = offset
        resp = sess.get(_BASE_URL, params=params, timeout=timeout_s)
        resp.raise_for_status()
        data = resp.json()

        page = data.get("features") or []
        features.extend(page)

        # OGC API Features: if the page is shorter than limit, we're done.
        if len(page) < page_size:
            break
        offset += len(page)

    return features


def _to_property(numeric_id: str, features: list[dict]) -> Property:
    """Build a Property from one or more palsta GeoJSON features."""
    geoms: list[BaseGeometry] = []
    display_id = ""

    for f in features:
        props = f.get("properties") or {}
        if not display_id:
            display_id = props.get("kiinteistotunnuksenEsitysmuoto", numeric_id)
        geom = shape(f["geometry"])
        require_3067(geom)
        geoms.append(geom)

    merged = unary_union(geoms) if len(geoms) > 1 else geoms[0]
    # Area in hectares from EPSG:3067 (metres) geometry
    area_ha = merged.area / 10_000

    return Property(
        property_id=numeric_id,
        display_id=display_id,
        area_ha=area_ha,
        geometry=merged,
    )
