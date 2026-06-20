"""CRS guard rails.

The single most common silent failure in this tool is an EPSG:3067 coordinate
(e.g. E=305000, N=6900000) being passed somewhere that expects EPSG:4326
(lon≈22, lat≈62), or vice versa.  Call assert_crs() at every module boundary
where a geometry crosses a function interface.
"""

from __future__ import annotations

from shapely.geometry.base import BaseGeometry

EPSG_3067 = 3067
EPSG_4326 = 4326

# Bounding boxes that any valid Finnish geometry must fall within.
# Used to detect swapped CRS — a 3067 coord fed to a 4326 consumer fails here.
_FINLAND_BOUNDS: dict[int, tuple[float, float, float, float]] = {
    # ETRS-TM35FIN (projected metres)  xmin,   ymin,    xmax,    ymax
    EPSG_3067: (60_000.0, 6_580_000.0, 800_000.0, 7_780_000.0),
    # WGS84 geodetic (lon, lat)
    EPSG_4326: (17.0, 58.0, 32.0, 71.0),
}


class CRSError(ValueError):
    """Raised when a geometry's coordinates are inconsistent with the declared CRS."""


def assert_crs(geom: BaseGeometry, epsg: int) -> None:
    """Raise CRSError if *geom*'s bounding box is outside Finland's expected range for *epsg*.

    This is a range-based sanity check, not a full CRS transform.  It catches
    the swap-case (3067 coords passed as 4326) immediately rather than
    silently producing a KMZ polygon in the middle of the ocean.
    """
    if epsg not in _FINLAND_BOUNDS:
        raise ValueError(f"No guard bounds defined for EPSG:{epsg}")

    xmin, ymin, xmax, ymax = _FINLAND_BOUNDS[epsg]
    bx = geom.bounds  # (minx, miny, maxx, maxy)

    if not (
        xmin <= bx[0] <= xmax
        and xmin <= bx[2] <= xmax
        and ymin <= bx[1] <= ymax
        and ymin <= bx[3] <= ymax
    ):
        _label = {EPSG_3067: "EPSG:3067 (ETRS-TM35FIN)", EPSG_4326: "EPSG:4326 (WGS84)"}
        raise CRSError(
            f"Geometry bounds {bx} are outside the expected range for "
            f"{_label[epsg]} in Finland {(xmin, ymin, xmax, ymax)}. "
            f"Likely CRS mismatch — check that the geometry was reprojected "
            f"before being passed here."
        )


def require_4326(geom: BaseGeometry) -> None:
    """Assert the geometry is in EPSG:4326.  Convenience wrapper for KMZ-path code."""
    assert_crs(geom, EPSG_4326)


def require_3067(geom: BaseGeometry) -> None:
    """Assert the geometry is in EPSG:3067.  Convenience wrapper for Finnish-data code."""
    assert_crs(geom, EPSG_3067)
