"""UAS restriction-zone intersection check.

Confirmed data source (2026-05):
  URL:  https://eservices.traficom.fi/Ilmatilasovellus/api/uas-reservations/json?lang=fi
  Auth: none — open Traficom endpoint
  CRS:  EPSG:4326 (lon/lat) in geometry[].horizontalProjection
  Size: ~720 zones, single JSON dump, all marked permanent

  This is a **periodically regenerated static dump** of established UAS
  geographical zones, not a live API.  The response title carries a
  generation date (e.g. "Finnish UASZoneVersion 2026-5-29").  Temporary
  restrictions (NOTAMs, TFRs) are NOT included — those require a separate
  day-of check via Fintraffic/AIS NOTAMs.

Restriction values:
  PROHIBITED        → hard no-fly; sets flight_ready=False
  REQ_AUTHORISATION → authorisation needed; sets needs_review=True, flight_ready=False
  NO_RESTRICTION    → informational only (e.g. nature reserves); not flagged

Caching: the full dump is cached as a single JSON file under
  <cache_dir>/zones/uas_zones.json with mtime-based TTL.  Since the
  source is a periodic dump of permanent zones, a TTL of 7–30 days is
  appropriate.  If a zones_file override is set in config, that file is
  used instead (offline / custom zone data).

Safety note: this tool checks established permanent zones only.  The
  remote pilot must still verify NOTAMs and current temporary restrictions
  on the day of the flight.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests
from pyproj import Transformer
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform as shp_transform, unary_union

import flightmanager.net_stats as _ns
from flightmanager.config import ZonesConfig
from flightmanager.crs import require_4326

_to_3067 = Transformer.from_crs("EPSG:4326", "EPSG:3067", always_xy=True)
_to_4326 = Transformer.from_crs("EPSG:3067", "EPSG:4326", always_xy=True)

log = logging.getLogger(__name__)

_API_URL = "https://eservices.traficom.fi/Ilmatilasovellus/api/uas-reservations/json?lang=fi"

# Only these restriction values are flagged; NO_RESTRICTION is informational.
_FLAGGED_RESTRICTIONS = {"PROHIBITED", "REQ_AUTHORISATION"}


@dataclass
class AltitudeLimits:
    upper_limit: float | None       # numeric value
    upper_uom: str | None           # "M" or "FT"
    upper_ref: str | None           # "AGL" or "AMSL"
    lower_limit: float | None
    lower_uom: str | None
    lower_ref: str | None

    @property
    def upper_limit_m_agl(self) -> float | None:
        """Upper limit converted to metres AGL, or None if AMSL (needs terrain)."""
        if self.upper_limit is None or self.upper_ref != "AGL":
            return None
        if self.upper_uom == "FT":
            return self.upper_limit * 0.3048
        return float(self.upper_limit)

    @property
    def lower_limit_m_agl(self) -> float | None:
        """Lower limit (zone floor) converted to metres AGL, or None if AMSL or not set."""
        if self.lower_limit is None or self.lower_ref != "AGL":
            return None
        if self.lower_uom == "FT":
            return self.lower_limit * 0.3048
        return float(self.lower_limit)

    def ceiling_note(self, flight_height_m: float) -> str:
        """Human-readable note comparing planned height against zone altitude limits.

        Finnish UAS zones (vyöhykkeet A–D) use lower_limit as the *floor* of the
        restriction.  Flying below that floor exits the zone entirely — so
        lower_limit_m_agl is the binding safe-ceiling for UAS operators, not the
        upper_limit.  upper_limit is merely the top of the restricted volume.
        """
        lo_m = self.lower_limit_m_agl
        hi_m = self.upper_limit_m_agl

        if lo_m is not None and lo_m > 0:
            # Zone only applies above its floor — flying below lo_m avoids it.
            if flight_height_m < lo_m:
                return (
                    f"Planned height {flight_height_m:.0f} m AGL is below zone floor "
                    f"{lo_m:.0f} m AGL — no authorisation required at this height"
                )
            hi_str = f"–{hi_m:.0f} m" if hi_m is not None else "+"
            return (
                f"Planned height {flight_height_m:.0f} m AGL is inside zone "
                f"({lo_m:.0f}{hi_str} m AGL) — fly below {lo_m:.0f} m or obtain authorisation"
            )

        # Zone applies from ground up (lo = 0 or unknown).
        if hi_m is not None:
            if flight_height_m > hi_m:
                return (
                    f"Planned height {flight_height_m:.0f} m AGL is above zone ceiling "
                    f"{hi_m:.0f} m AGL — flight is above this restriction"
                )
            return (
                f"Zone applies from ground to {hi_m:.0f} m AGL — "
                f"planned height {flight_height_m:.0f} m is inside the zone"
            )
        if self.upper_ref == "AMSL":
            return (
                f"Zone ceiling {self.upper_limit} {self.upper_uom} AMSL "
                f"(cannot compare with {flight_height_m:.0f} m AGL without terrain elevation)"
            )
        return "Zone applies from ground with no upper limit — restriction at all altitudes"


@dataclass
class ZoneHit:
    identifier: str
    name: str
    restriction: str       # PROHIBITED | REQ_AUTHORISATION
    reason: list[str]
    altitude: AltitudeLimits
    properties: dict
    geom: BaseGeometry | None = None  # zone geometry in EPSG:4326, for nesting detection
    buffer_only: bool = False         # True when zone only hits the search buffer, not the actual survey polygon


@dataclass
class ZoneCheckResult:
    checked: bool
    intersecting_zones: list[ZoneHit] = field(default_factory=list)
    related_zones: list[ZoneHit] = field(default_factory=list)  # inner/nested zones, shown for context
    needs_review: bool = False
    flight_ready: bool = True
    reasons: list[str] = field(default_factory=list)
    fetched_date: str = ""   # ISO date when zone data was last fetched from Traficom
    attribution: str = ""    # CC-BY attribution string for manifest


def check_zones(
    survey_4326: BaseGeometry,
    config: ZonesConfig,
    flight_height_m: float = 100.0,
    cache_dir: str | Path = "cache",
    session: requests.Session | None = None,
) -> ZoneCheckResult:
    """Intersect *survey_4326* with Finnish UAS restriction zones.

    Fetches zone data from the Traficom API (or a configured override file),
    caches it locally, and returns a ZoneCheckResult.

    *survey_4326* must be in EPSG:4326.
    *flight_height_m* is used to compare against zone altitude ceilings and
    annotate each hit with a human-readable altitude note.
    The survey is expanded by *config.check_buffer_m* before the intersection
    so that nearby zones are also reported.
    """
    require_4326(survey_4326)

    features, fetched_date = _get_features(config, cache_dir, session)
    attribution = (
        f"Contains data from Traficom, UAS Geographical Zones, retrieved {fetched_date}."
        if fetched_date else
        "Contains data from Traficom, UAS Geographical Zones."
    )
    if features is None:
        reason = (
            "Zone data could not be loaded (no network and no cache). "
            "Zone intersection check was skipped — job flagged for review."
        )
        log.warning(reason)
        return ZoneCheckResult(
            checked=False, needs_review=True, reasons=[reason],
            fetched_date=fetched_date, attribution=attribution,
        )

    # Expand survey polygon for the search so nearby zones are also reported.
    if config.check_buffer_m > 0:
        survey_search = shp_transform(_to_4326.transform,
                                      shp_transform(_to_3067.transform, survey_4326)
                                      .buffer(config.check_buffer_m))
        log.debug("Zone search buffered by %.0f m", config.check_buffer_m)
    else:
        survey_search = survey_4326

    hits = _intersect(survey_search, features)
    if not hits:
        log.info("Zone check passed — no intersecting UAS restrictions found")
        return ZoneCheckResult(checked=True, fetched_date=fetched_date, attribution=attribution)

    # Mark each hit as buffer_only when it doesn't touch the actual survey polygon.
    for h in hits:
        h.buffer_only = h.geom is None or not survey_4326.intersects(h.geom)

    # Second pass: find zones whose centroid lies inside any hit zone.
    # These are the inner concentric zones of an airfield (e.g. vyöhyke C/D
    # inside a larger outer zone) that may not intersect the survey buffer but
    # are contextually important for understanding altitude limits.
    related = _find_related(hits, features)
    if related:
        log.info("Found %d related inner zone(s) for context", len(related))

    reasons = []
    for h in hits:
        alt_note = h.altitude.ceiling_note(flight_height_m)
        proximity = "is within {:.0f} m of".format(config.check_buffer_m) if h.buffer_only else "intersects"
        reason = (
            f"Survey area {proximity} UAS zone '{h.name}' [{h.identifier}] "
            f"restriction={h.restriction} reason={h.reason} — {alt_note}"
        )
        reasons.append(reason)
        log.warning(reason)

    return ZoneCheckResult(
        checked=True,
        intersecting_zones=hits,
        related_zones=related,
        needs_review=True,
        flight_ready=False,
        reasons=reasons,
        fetched_date=fetched_date,
        attribution=attribution,
    )


# ---------------------------------------------------------------------------
# Data loading / caching
# ---------------------------------------------------------------------------


def _file_date(path: Path) -> str:
    """ISO date (YYYY-MM-DD) of a file's last-modified time."""
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d")


def _get_features(
    config: ZonesConfig,
    cache_dir: str | Path,
    session: requests.Session | None,
) -> tuple[list[dict] | None, str]:
    """Load zone features from override file, fresh API, or cache.

    Returns ``(features, fetched_date)`` where *fetched_date* is an ISO date
    string reflecting when the data was last retrieved from Traficom.
    """
    # Override file takes precedence (offline / custom zone data)
    if config.zones_file:
        p = Path(config.zones_file)
        if not p.exists():
            log.warning("Zones override file not found: %s", p)
            return None, ""
        _warn_if_stale(p, config.max_age_days)
        return _parse_features(json.loads(p.read_text(encoding="utf-8"))), _file_date(p)

    # Otherwise use the API with a local cache
    cache_path = Path(cache_dir) / "zones" / "uas_zones.json"
    if _cache_fresh(cache_path, config.max_age_days):
        log.debug("Zone cache hit: %s", cache_path)
        _ns.record_hit("zones")
        return _parse_features(json.loads(cache_path.read_text(encoding="utf-8"))), _file_date(cache_path)

    return _fetch_and_cache(cache_path, session)


def _cache_fresh(path: Path, max_age_days: int) -> bool:
    if not path.exists():
        return False
    age_days = (time.time() - path.stat().st_mtime) / 86_400
    return age_days <= max_age_days


def _warn_if_stale(path: Path, max_age_days: int) -> None:
    age_days = (time.time() - path.stat().st_mtime) / 86_400
    if age_days > max_age_days:
        log.warning(
            "UAS zones file is %.0f days old (max %d). "
            "Restrictions may have changed — consider refreshing.",
            age_days, max_age_days,
        )


def _fetch_and_cache(
    cache_path: Path,
    session: requests.Session | None,
) -> tuple[list[dict] | None, str]:
    """Fetch zones from the Traficom API and save to cache_path."""
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    sess = session or requests.Session()
    try:
        log.info("Fetching UAS zones from Traficom API")
        resp = sess.get(_API_URL, timeout=30)
        resp.raise_for_status()
        _ns.record_download("zones", len(resp.content))
        data = resp.json()
    except Exception as exc:
        log.error("Failed to fetch UAS zones: %s", exc)
        # Fall back to stale cache if present
        if cache_path.exists():
            log.warning("Using stale zone cache as fallback")
            return _parse_features(json.loads(cache_path.read_text(encoding="utf-8"))), _file_date(cache_path)
        return None, ""

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    log.info(
        "UAS zones cached: %d feature(s) → %s",
        len(data.get("features", [])), cache_path,
    )
    return _parse_features(data), today


def _parse_features(data: dict) -> list[dict]:
    return data.get("features", [])


# ---------------------------------------------------------------------------
# Intersection
# ---------------------------------------------------------------------------


def _intersect(survey: BaseGeometry, features: list[dict]) -> list[ZoneHit]:
    """Return ZoneHit for every flagged zone that intersects the survey polygon."""
    hits: list[ZoneHit] = []
    for feat in features:
        restriction = feat.get("restriction", "")
        if restriction not in _FLAGGED_RESTRICTIONS:
            continue  # NO_RESTRICTION — informational, skip

        geom_entries = feat.get("geometry", [])
        if not geom_entries:
            continue

        g = geom_entries[0]
        try:
            zone_geom = shape(g.get("horizontalProjection", {}))
        except Exception as exc:
            log.warning("Skipping malformed zone geometry [%s]: %s",
                        feat.get("identifier", "?"), exc)
            continue

        if survey.intersects(zone_geom):
            altitude = AltitudeLimits(
                upper_limit=g.get("upperLimit"),
                upper_uom=g.get("uomDimensions"),
                upper_ref=g.get("upperVerticalReference"),
                lower_limit=g.get("lowerLimit"),
                lower_uom=g.get("uomDimensions"),
                lower_ref=g.get("lowerVerticalReference"),
            )
            hits.append(ZoneHit(
                identifier=feat.get("identifier", ""),
                name=feat.get("name", "unknown"),
                restriction=restriction,
                reason=feat.get("reason", []),
                altitude=altitude,
                properties=feat,
                geom=zone_geom,
            ))

    return hits


def _find_related(hits: list[ZoneHit], all_features: list[dict]) -> list[ZoneHit]:
    """Return flagged zones whose centroid lies inside any directly-hit zone.

    These are inner concentric zones (e.g. closer-in vyöhykkeet of an airfield)
    that don't intersect the survey buffer but are relevant context for
    understanding the altitude restrictions in the area.
    """
    hit_ids = {h.identifier for h in hits}
    hit_geoms = [h.geom for h in hits if h.geom is not None]
    if not hit_geoms:
        return []
    hit_union = unary_union(hit_geoms) if len(hit_geoms) > 1 else hit_geoms[0]

    related: list[ZoneHit] = []
    for feat in all_features:
        ident = feat.get("identifier", "")
        if ident in hit_ids:
            continue
        if feat.get("restriction", "") not in _FLAGGED_RESTRICTIONS:
            continue
        geom_entries = feat.get("geometry", [])
        if not geom_entries:
            continue
        g = geom_entries[0]
        try:
            zone_geom = shape(g.get("horizontalProjection", {}))
        except Exception:
            continue
        if not hit_union.contains(zone_geom.centroid):
            continue
        altitude = AltitudeLimits(
            upper_limit=g.get("upperLimit"),
            upper_uom=g.get("uomDimensions"),
            upper_ref=g.get("upperVerticalReference"),
            lower_limit=g.get("lowerLimit"),
            lower_uom=g.get("uomDimensions"),
            lower_ref=g.get("lowerVerticalReference"),
        )
        related.append(ZoneHit(
            identifier=ident,
            name=feat.get("name", "unknown"),
            restriction=feat.get("restriction", ""),
            reason=feat.get("reason", []),
            altitude=altitude,
            properties=feat,
            geom=zone_geom,
        ))

    return related
