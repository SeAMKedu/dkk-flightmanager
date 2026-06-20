"""Group jobs into *launch sites* for flight announcements.

A **launch site** is a maximal run of consecutive-flight-order jobs whose
takeoff points all sit within ``max_radius_m`` of the run's running takeoff
centroid — i.e. the set of jobs you fly from one parking spot before driving
elsewhere. Each site carries two distinct points:

* ``dot_4326`` — the takeoff centroid (where you stand / launch from), shown as
  the numbered route dot on the job-list map.
* ``circle_center_4326`` + ``radius_m`` — the **smallest enclosing circle** over
  all member survey polygons *and* their takeoff points. This centre + radius is
  exactly what a pilot enters into a flight-announcement app (e.g. Flyk):
  the operating-area centre and its radius/diameter.

Pure module — EPSG:3067 metric math via shapely, no FastAPI / file I/O. Shared
by the map-view ``/api/launch_sites`` endpoint and (later) the PDF mission
packet, so the clustering lives in exactly one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from shapely import minimum_bounding_circle, minimum_bounding_radius
from shapely.geometry import Point, shape
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from flightmanager.geometry import reproject_to_3067, reproject_to_4326

# Default proximity: jobs whose takeoffs stay within this of the running
# centroid belong to the same launch site. ~50 m ≈ "I'd park once for these";
# anything further means driving to a new spot.
DEFAULT_MAX_RADIUS_M = 50.0


@dataclass
class LaunchSite:
    """One parking spot's worth of consecutive jobs, plus its announcement circle."""

    index: int                              # 1-based visit order
    job_paths: list[str] = field(default_factory=list)
    job_names: list[str] = field(default_factory=list)
    sort_orders: list[int | None] = field(default_factory=list)
    dot_4326: list[float] = field(default_factory=list)            # [lon, lat] takeoff centroid
    circle_center_4326: list[float] = field(default_factory=list)  # [lon, lat] min-circle centre
    radius_m: float = 0.0
    flight_time_min: float | None = None    # Σ member flight times (None if unknown)
    max_altitude_m: float | None = None     # highest flight altitude over members (Flyk field)
    members: list[dict] = field(default_factory=list)  # per-job {path, name, route_index, takeoff_4326}

    @property
    def member_count(self) -> int:
        return len(self.job_paths)

    @property
    def first_route_index(self) -> int | None:
        """Route index (1-based) of the site's first job — shown on the map dot so
        it matches the per-job route-index circles rather than a separate count."""
        idxs = [m["route_index"] for m in self.members if m.get("route_index") is not None]
        return min(idxs) if idxs else None

    @property
    def diameter_m(self) -> float:
        return 2.0 * self.radius_m

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "job_paths": self.job_paths,
            "job_names": self.job_names,
            "sort_orders": self.sort_orders,
            "dot_4326": self.dot_4326,
            "circle_center_4326": self.circle_center_4326,
            "radius_m": round(self.radius_m, 1),
            "diameter_m": round(self.diameter_m, 1),
            "flight_time_min": (
                round(self.flight_time_min, 1) if self.flight_time_min is not None else None
            ),
            "max_altitude_m": (
                round(self.max_altitude_m, 1) if self.max_altitude_m is not None else None
            ),
            "first_route_index": self.first_route_index,
            "members": self.members,
            "member_count": self.member_count,
        }


def _routable(cards: list[dict]) -> list[dict]:
    """Jobs eligible for a launch site: a takeoff point and not skipped."""
    out = [c for c in cards if c.get("takeoff_point_4326") and not c.get("skipped")]
    # Flight order: explicit sort_order first (ascending), then name. Mirrors the
    # ordering used by the map-view route layer and KML export.
    out.sort(key=lambda c: (
        0 if c.get("sort_order") is not None else 1,
        c.get("sort_order") if c.get("sort_order") is not None else 0,
        c.get("name") or c.get("job_name") or "",
    ))
    return out


def _takeoff_point_3067(card: dict) -> Point:
    lon, lat = card["takeoff_point_4326"][0], card["takeoff_point_4326"][1]
    return reproject_to_3067(Point(lon, lat))


def _build_site(index: int, members: list[dict]) -> LaunchSite:
    """Assemble a :class:`LaunchSite` from its (already grouped) member cards."""
    takeoffs_3067 = [_takeoff_point_3067(c) for c in members]
    cx = sum(p.x for p in takeoffs_3067) / len(takeoffs_3067)
    cy = sum(p.y for p in takeoffs_3067) / len(takeoffs_3067)
    dot_4326 = reproject_to_4326(Point(cx, cy))

    # Smallest enclosing circle over every member polygon + every takeoff point.
    geoms: list[BaseGeometry] = list(takeoffs_3067)
    for c in members:
        g = c.get("_geometry")
        if g:
            try:
                geoms.append(reproject_to_3067(shape(g)))
            except Exception:
                pass
    union = unary_union(geoms)
    circle = minimum_bounding_circle(union)
    radius_m = float(minimum_bounding_radius(union))
    center_4326 = reproject_to_4326(circle.centroid)

    times = [c.get("flight_time_min") for c in members]
    known = [t for t in times if t is not None]
    flight_time = sum(known) if known else None

    # Highest flight altitude across the site — advanced (variable-altitude) jobs
    # cap at adv_max_height_m, simple jobs at their fixed height_m.
    alts: list[float] = []
    for c in members:
        a = c.get("adv_max_height_m") if c.get("waypoint_mode") else None
        if a is None:
            a = c.get("height_m")
        if a is not None:
            alts.append(float(a))
    max_alt = max(alts) if alts else None

    member_dicts = [
        {
            "path": c.get("path") or c.get("name") or "",
            "name": c.get("name") or c.get("job_name") or "job",
            # Route index shown on the map is 1-based; stored sort_order is 0-based.
            "route_index": (c.get("sort_order") + 1) if c.get("sort_order") is not None else None,
            "takeoff_4326": c.get("takeoff_point_4326"),
        }
        for c in members
    ]

    return LaunchSite(
        index=index,
        job_paths=[c.get("path") or c.get("name") or "" for c in members],
        job_names=[c.get("name") or c.get("job_name") or "job" for c in members],
        sort_orders=[c.get("sort_order") for c in members],
        members=member_dicts,
        dot_4326=[dot_4326.x, dot_4326.y],
        circle_center_4326=[center_4326.x, center_4326.y],
        radius_m=radius_m,
        flight_time_min=flight_time,
        max_altitude_m=max_alt,
    )


def cluster_jobs(
    cards: list[dict], *, max_radius_m: float = DEFAULT_MAX_RADIUS_M
) -> list[LaunchSite]:
    """Group routable jobs into launch sites by flight order + takeoff proximity.

    Walks the jobs in flight order, accumulating each into the current site as
    long as its takeoff stays within ``max_radius_m`` of the site's *running*
    takeoff centroid. The first job that falls outside closes the current site
    and opens a new one — so each site is a contiguous run of the visit sequence
    and stays physically compact (a later job near an earlier site but far in the
    sequence becomes its own site).

    *cards* are job dicts carrying ``takeoff_point_4326`` ([lon, lat]),
    ``_geometry`` (4326 GeoJSON), ``sort_order``, ``name``/``path``, optional
    ``flight_time_min`` and ``skipped``.
    """
    jobs = _routable(cards)
    if not jobs:
        return []

    sites: list[LaunchSite] = []
    cur: list[dict] = []
    cur_takeoffs: list[Point] = []

    def _flush() -> None:
        if cur:
            sites.append(_build_site(len(sites) + 1, list(cur)))

    for card in jobs:
        tp = _takeoff_point_3067(card)
        if not cur:
            cur, cur_takeoffs = [card], [tp]
            continue
        cx = sum(p.x for p in cur_takeoffs) / len(cur_takeoffs)
        cy = sum(p.y for p in cur_takeoffs) / len(cur_takeoffs)
        if tp.distance(Point(cx, cy)) <= max_radius_m:
            cur.append(card)
            cur_takeoffs.append(tp)
        else:
            _flush()
            cur, cur_takeoffs = [card], [tp]
    _flush()
    return sites
