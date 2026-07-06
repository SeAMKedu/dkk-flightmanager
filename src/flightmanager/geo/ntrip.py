"""NTRIP caster sourcetable lookup — nearby RTK base stations.

Polls each configured caster (``RtkConfig.networks``: rtk2go, centipede, …) for
its sourcetable, parses the ``STR`` entries into :class:`Station` records, and
answers "which base stations are usable from here?" for the map view, the job
popups, the PDF launch-site pages, and the ``flightmanager rtk`` CLI command.

A sourcetable lists only stations that are connected *right now*, so results are
cached per network at ``<cache_dir>/ntrip/<name>.json`` with a short mtime TTL
(``RtkConfig.cache_max_age_hours``) and every payload carries a ``fetched_at``
timestamp — community bases churn, re-check on the day of flight.

Casters speak two protocol revisions: NTRIP 2.0 is plain HTTP (fetched with
requests + ``Ntrip-Version`` header); NTRIP 1.0 replies with a bare
``SOURCETABLE 200 OK`` status line that is not valid HTTP, so a raw-socket
fallback handles v1-only casters. Distances are haversine on WGS84 lat/lon
(sub-% error at RTK ranges) — no shapely/pyproj dependency, pure math + the two
fetch functions.
"""

from __future__ import annotations

import json
import logging
import math
import socket
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

import flightmanager.net_stats as _ns
from flightmanager.config import RtkConfig, RtkNetworkConfig

log = logging.getLogger(__name__)

# Bump when the cached station shape changes so stale files are re-fetched.
_CACHE_VERSION = 1

_EARTH_R_KM = 6371.0088


@dataclass
class Station:
    """One base station (``STR`` sourcetable entry) of one network."""

    network: str
    mountpoint: str  # what the pilot enters into DJI Pilot 2
    identifier: str  # free-text station name/city
    format: str  # e.g. "RTCM 3.2"
    nav_system: str  # e.g. "GPS+GLO+GAL+BDS"
    country: str
    lat: float
    lon: float


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two WGS84 points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_R_KM * math.asin(math.sqrt(a))


def caster_host_port(caster_url: str) -> tuple[str, int]:
    """Split ``host:port`` / ``http://host:port`` into (host, port). Port defaults 2101."""
    u = caster_url if "//" in caster_url else "//" + caster_url
    parsed = urlparse(u, scheme="http")
    return parsed.hostname or "", parsed.port or 2101


def parse_sourcetable(text: str, network: str) -> list[Station]:
    """Parse ``STR`` lines of a sourcetable into stations (entries without a
    plausible lat/lon are dropped — they can't be ranked by distance)."""
    out: list[Station] = []
    for line in text.splitlines():
        if not line.startswith("STR;"):
            continue
        f = line.split(";")
        # NTRIP spec: STR;mount;identifier;format;details;carrier;nav;network;
        #             country;lat;lon;...
        if len(f) < 11:
            continue
        try:
            lat, lon = float(f[9]), float(f[10])
        except ValueError:
            continue
        if lat == 0.0 and lon == 0.0:
            continue
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            continue
        out.append(
            Station(
                network=network,
                mountpoint=f[1],
                identifier=f[2],
                format=f[3],
                nav_system=f[6],
                country=f[8],
                lat=lat,
                lon=lon,
            )
        )
    return out


def _fetch_v2(net: RtkNetworkConfig, timeout_s: int) -> str:
    """Sourcetable over plain HTTP (NTRIP 2.0). Raises on failure."""
    host, port = caster_host_port(net.caster_url)
    url = f"http://{host}:{port}/"
    headers = {
        "Ntrip-Version": "Ntrip/2.0",
        "User-Agent": "NTRIP dkk-flightmanager",
    }
    auth = (net.username, net.password) if net.username else None
    resp = requests.get(url, headers=headers, auth=auth, timeout=timeout_s)
    resp.raise_for_status()
    return resp.text


def _fetch_v1(net: RtkNetworkConfig, timeout_s: int) -> str:
    """Raw-socket fallback for NTRIP 1.0 casters, whose ``SOURCETABLE 200 OK``
    status line breaks HTTP clients. Reads until the caster closes."""
    host, port = caster_host_port(net.caster_url)
    req = (
        "GET / HTTP/1.0\r\n"
        f"Host: {host}\r\n"
        "User-Agent: NTRIP dkk-flightmanager\r\n"
        "Accept: */*\r\n\r\n"
    )
    chunks: list[bytes] = []
    with socket.create_connection((host, port), timeout=timeout_s) as sock:
        sock.sendall(req.encode("ascii"))
        sock.settimeout(timeout_s)
        while True:
            data = sock.recv(65536)
            if not data:
                break
            chunks.append(data)
            if b"ENDSOURCETABLE" in chunks[-1]:
                break
    text = b"".join(chunks).decode("latin-1", "replace")
    if "SOURCETABLE 200 OK" not in text and "STR;" not in text:
        raise RuntimeError(f"{host}:{port} did not return a sourcetable")
    return text


def _cache_path(cache_dir: str | Path, network_name: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in network_name)
    return Path(cache_dir) / "ntrip" / f"{safe}.json"


def _load_cache(path: Path) -> tuple[list[Station], str] | None:
    """(stations, fetched_at ISO) from a cached sourcetable, or None on mismatch."""
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("v") != _CACHE_VERSION:
            return None
        stations = [Station(**s) for s in raw.get("stations", [])]
        return stations, raw.get("fetched_at", "")
    except Exception:
        return None


def fetch_stations(
    net: RtkNetworkConfig, cache_dir: str | Path, cfg: RtkConfig
) -> tuple[list[Station], str, str | None]:
    """Return (stations, fetched_at ISO, error) for one network, cache-first.

    On fetch failure a stale cache is served (with its old timestamp) and the
    error string is still set so callers can surface it.
    """
    path = _cache_path(cache_dir, net.name)
    cached = _load_cache(path)
    if cached is not None:
        age_h = (time.time() - path.stat().st_mtime) / 3600
        if age_h <= cfg.cache_max_age_hours:
            _ns.record_hit("ntrip")
            return cached[0], cached[1], None

    error: str | None = None
    text = ""
    try:
        text = _fetch_v2(net, cfg.timeout_s)
    except Exception as exc2:
        log.info("NTRIP v2 fetch failed for %s (%s); trying v1", net.name, exc2)
        try:
            text = _fetch_v1(net, cfg.timeout_s)
        except Exception as exc1:
            error = str(exc1)
            log.error("Sourcetable fetch failed for %s: %s", net.name, exc1)

    if error is None:
        stations = parse_sourcetable(text, net.name)
        if stations:
            _ns.record_download("ntrip", len(text))
            fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "v": _CACHE_VERSION,
                        "fetched_at": fetched_at,
                        "stations": [asdict(s) for s in stations],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return stations, fetched_at, None
        error = "sourcetable contained no stations"

    if cached is not None:
        log.warning("Serving stale NTRIP cache for %s", net.name)
        return cached[0], cached[1], error
    return [], "", error


def min_distance_km(station: Station, points: list[tuple[float, float]]) -> float:
    """Distance (km) from *station* to the closest of *points* ((lat, lon))."""
    return min(haversine_km(station.lat, station.lon, la, lo) for la, lo in points)


def stations_near(
    points: list[tuple[float, float]], cfg: RtkConfig, cache_dir: str | Path
) -> dict:
    """All configured networks' stations within ``search_radius_km`` of any point.

    Returns the API/report payload::

        {generated_at, circle_radius_km,
         networks: [{name, color, caster_url, caster_host, caster_port,
                     username, password, fetched_at, error, station_count}],
         stations: [{network, color, mountpoint, identifier, format, nav_system,
                     lat, lon, dist_km}]}   # sorted by dist_km

    ``station_count`` is the count *within range*; fetch errors leave the network
    listed with its error so the UI/PDF can say so.
    """
    networks: list[dict] = []
    stations: list[dict] = []
    for net in cfg.networks:
        if not net.enabled:
            continue
        sts, fetched_at, error = fetch_stations(net, cache_dir, cfg)
        host, port = caster_host_port(net.caster_url)
        near: list[dict] = []
        if points:
            for s in sts:
                d = min_distance_km(s, points)
                if d <= cfg.search_radius_km:
                    e = asdict(s)
                    e["dist_km"] = round(d, 1)
                    e["color"] = net.color
                    near.append(e)
        networks.append(
            {
                "name": net.name,
                "color": net.color,
                "caster_url": net.caster_url,
                "caster_host": host,
                "caster_port": port,
                "username": net.username,
                "password": net.password,
                "fetched_at": fetched_at,
                "error": error,
                "station_count": len(near),
            }
        )
        stations.extend(near)
    stations.sort(key=lambda s: s["dist_km"])
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "circle_radius_km": cfg.circle_radius_km,
        "search_radius_km": cfg.search_radius_km,
        "networks": networks,
        "stations": stations,
    }


def recommend_for_point(
    payload: dict, lat: float, lon: float, radius_km: float
) -> tuple[dict | None, list[dict]]:
    """(nearest station, alternatives within *radius_km*) for one point.

    Works on a :func:`stations_near` payload; distances are recomputed for the
    given point (payload ``dist_km`` is the min over *all* jobs). The nearest
    station is returned even when it is beyond *radius_km* — callers show the
    distance and let the pilot judge.
    """
    ranked = sorted(
        (
            {**s, "dist_km": round(haversine_km(lat, lon, s["lat"], s["lon"]), 1)}
            for s in payload.get("stations", [])
        ),
        key=lambda s: s["dist_km"],
    )
    if not ranked:
        return None, []
    nearest = ranked[0]
    alternatives = [s for s in ranked[1:] if s["dist_km"] <= radius_km]
    return nearest, alternatives
