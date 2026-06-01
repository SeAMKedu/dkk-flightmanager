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
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from jobgen.config import ZonesConfig
from jobgen.crs import require_4326

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

    def ceiling_note(self, flight_height_m: float) -> str:
        """Human-readable note comparing planned height against zone ceiling."""
        m_agl = self.upper_limit_m_agl
        if m_agl is None:
            if self.upper_ref == "AMSL":
                return (
                    f"Zone ceiling {self.upper_limit} {self.upper_uom} AMSL "
                    f"(cannot compare with {flight_height_m:.0f} m AGL without terrain elevation)"
                )
            return f"No upper limit — restriction applies at all altitudes"
        if flight_height_m > m_agl:
            return (
                f"Planned height {flight_height_m:.0f} m AGL is ABOVE zone ceiling "
                f"{m_agl:.0f} m AGL — flight may be above this restriction"
            )
        return (
            f"Planned height {flight_height_m:.0f} m AGL is within zone "
            f"(ceiling {m_agl:.0f} m AGL)"
        )


@dataclass
class ZoneHit:
    identifier: str
    name: str
    restriction: str       # PROHIBITED | REQ_AUTHORISATION
    reason: list[str]
    altitude: AltitudeLimits
    properties: dict


@dataclass
class ZoneCheckResult:
    checked: bool
    intersecting_zones: list[ZoneHit] = field(default_factory=list)
    needs_review: bool = False
    flight_ready: bool = True
    reasons: list[str] = field(default_factory=list)


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
    """
    require_4326(survey_4326)

    features = _get_features(config, cache_dir, session)
    if features is None:
        reason = (
            "Zone data could not be loaded (no network and no cache). "
            "Zone intersection check was skipped — job flagged for review."
        )
        log.warning(reason)
        return ZoneCheckResult(checked=False, needs_review=True, reasons=[reason])

    hits = _intersect(survey_4326, features)
    if not hits:
        log.info("Zone check passed — no intersecting UAS restrictions found")
        return ZoneCheckResult(checked=True)

    reasons = []
    for h in hits:
        alt_note = h.altitude.ceiling_note(flight_height_m)
        reason = (
            f"Survey area intersects UAS zone '{h.name}' [{h.identifier}] "
            f"restriction={h.restriction} reason={h.reason} — {alt_note}"
        )
        reasons.append(reason)
        log.warning(reason)

    return ZoneCheckResult(
        checked=True,
        intersecting_zones=hits,
        needs_review=True,
        flight_ready=False,
        reasons=reasons,
    )


# ---------------------------------------------------------------------------
# Data loading / caching
# ---------------------------------------------------------------------------


def _get_features(
    config: ZonesConfig,
    cache_dir: str | Path,
    session: requests.Session | None,
) -> list[dict] | None:
    """Load zone features from override file, fresh API, or cache."""
    # Override file takes precedence (offline / custom zone data)
    if config.zones_file:
        p = Path(config.zones_file)
        if not p.exists():
            log.warning("Zones override file not found: %s", p)
            return None
        _warn_if_stale(p, config.max_age_days)
        return _parse_features(json.loads(p.read_text(encoding="utf-8")))

    # Otherwise use the API with a local cache
    cache_path = Path(cache_dir) / "zones" / "uas_zones.json"
    if _cache_fresh(cache_path, config.max_age_days):
        log.debug("Zone cache hit: %s", cache_path)
        return _parse_features(json.loads(cache_path.read_text(encoding="utf-8")))

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
) -> list[dict] | None:
    """Fetch zones from the Traficom API and save to cache_path."""
    sess = session or requests.Session()
    try:
        log.info("Fetching UAS zones from Traficom API")
        resp = sess.get(_API_URL, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.error("Failed to fetch UAS zones: %s", exc)
        # Fall back to stale cache if present
        if cache_path.exists():
            log.warning("Using stale zone cache as fallback")
            return _parse_features(json.loads(cache_path.read_text(encoding="utf-8")))
        return None

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    log.info(
        "UAS zones cached: %d feature(s) → %s",
        len(data.get("features", [])), cache_path,
    )
    return _parse_features(data)


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
            ))

    return hits
