"""Server-side slippy-map basemap for the PDF report.

Fetches Web-Mercator (EPSG:3857) XYZ tiles for a geographic bounding box,
stitches them into a single Pillow image cropped to the box, and hands back a
``lon/lat → (px, py)`` transform so the report can draw **vector** overlays
(survey polygons, strips, takeoffs, launch-site circles) crisply on top of the
bitmap basemap.

Two providers, both standard XYZ over the Web-Mercator tile matrix (mirrors the
browser UI's base layers): MML *ortokuva* orthophoto (native zoom ≤ 15, needs a
key) and OpenStreetMap (zoom ≤ 19). Pure helper — only network is the tile GETs.
"""

from __future__ import annotations

import io
import math
from collections import OrderedDict
from dataclasses import dataclass

import requests
from PIL import Image

_TILE = 256          # standard slippy-map tile size (px)
_TIMEOUT = 20
_UA = "dkk-flightmanager/1.0 (+https://maanmittauslaitos.fi)"

# In-memory tile cache + reusable session: a packet draws many overlapping maps
# (overview + per-site + per-job), so the same tiles are requested repeatedly.
_TILE_CACHE: "OrderedDict[tuple, Image.Image]" = OrderedDict()
_TILE_CACHE_MAX = 1500
_SESSION = requests.Session()


def _get_tile(provider: "Provider", z: int, x: int, y: int, *, mml_key, session) -> "Image.Image | None":
    key = (provider.name, z, x, y)
    cached = _TILE_CACHE.get(key)
    if cached is not None:
        _TILE_CACHE.move_to_end(key)
        return cached
    try:
        url = provider.tile_url(z, x, y, mml_key=mml_key)
        r = session.get(url, timeout=_TIMEOUT, headers={"User-Agent": _UA})
        r.raise_for_status()
        tile = Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception:
        return None
    _TILE_CACHE[key] = tile
    if len(_TILE_CACHE) > _TILE_CACHE_MAX:
        _TILE_CACHE.popitem(last=False)
    return tile


@dataclass
class Provider:
    name: str
    max_zoom: int
    attribution: str

    def tile_url(self, z: int, x: int, y: int, *, mml_key: str | None) -> str:
        raise NotImplementedError


@dataclass
class _OSM(Provider):
    def tile_url(self, z, x, y, *, mml_key=None):
        return f"https://a.tile.openstreetmap.org/{z}/{x}/{y}.png"


@dataclass
class _MML(Provider):
    def tile_url(self, z, x, y, *, mml_key=None):
        return (
            "https://avoin-karttakuva.maanmittauslaitos.fi/avoin/wmts"
            "?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0"
            "&LAYER=ortokuva&STYLE=default&TILEMATRIXSET=WGS84_Pseudo-Mercator"
            f"&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}&FORMAT=image/jpeg"
            f"&api-key={mml_key}"
        )


OSM = _OSM("osm", 19, "© OpenStreetMap contributors (ODbL)")
MML = _MML("mml", 15, "© Maanmittauslaitos orthophoto (CC BY 4.0)")


def get_provider(name: str, *, mml_key: str | None) -> Provider:
    """Pick the basemap: MML ortho when a key is available, else OSM."""
    if name == "mml" and mml_key:
        return MML
    return OSM


# ── Bounding-box helpers ──────────────────────────────────────────────────────

def _merc_y(lat: float) -> float:
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def _inv_merc_y(y: float) -> float:
    return math.degrees(2 * math.atan(math.exp(y)) - math.pi / 2)


def pad_bbox(bbox: tuple[float, float, float, float], frac: float = 0.12) -> tuple[float, float, float, float]:
    """Grow a bbox outward by *frac* of its span on every side (min span enforced)."""
    minlon, minlat, maxlon, maxlat = bbox
    dx = max(maxlon - minlon, 1e-4) * frac
    dy = max(maxlat - minlat, 1e-4) * frac
    return (minlon - dx, minlat - dy, maxlon + dx, maxlat + dy)


def fit_bbox(bbox: tuple[float, float, float, float], aspect: float) -> tuple[float, float, float, float]:
    """Expand a bbox to a target ``width/height`` *aspect* (in Web-Mercator) so a
    basemap fills the page box without distortion. Only ever grows the bbox.

    Both axes are compared in mercator units: x = ``radians(lon)``, y = the
    Gudermannian ``_merc_y(lat)``. (Comparing lon-degrees against merc-y radians
    inflates one axis ~57x and fetches a giant strip of tiles.)
    """
    minlon, minlat, maxlon, maxlat = bbox
    cx = (minlon + maxlon) / 2
    wx = math.radians(maxlon - minlon)            # mercator-x span
    y0, y1 = _merc_y(minlat), _merc_y(maxlat)
    cym, hy = (y0 + y1) / 2, (y1 - y0)
    if hy <= 0 or wx <= 0:
        return bbox
    if wx / hy < aspect:
        wx = aspect * hy
    else:
        hy = wx / aspect
    half_lon = math.degrees(wx) / 2
    return (cx - half_lon, _inv_merc_y(cym - hy / 2), cx + half_lon, _inv_merc_y(cym + hy / 2))


# ── Web-Mercator math ─────────────────────────────────────────────────────────

def _lonlat_to_world_px(lon: float, lat: float, z: int) -> tuple[float, float]:
    """Global pixel coords at zoom *z* (world is ``256·2^z`` px square)."""
    n = _TILE * (2 ** z)
    x = (lon + 180.0) / 360.0 * n
    s = math.sin(math.radians(lat))
    y = (0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * n
    return x, y


def _choose_zoom(bbox: tuple[float, float, float, float], target_px: int, max_zoom: int) -> int:
    """Largest zoom at which the bbox width stays within ~target_px (clamped)."""
    minlon, _, maxlon, _ = bbox
    for z in range(max_zoom, -1, -1):
        x0, _ = _lonlat_to_world_px(minlon, 0, z)
        x1, _ = _lonlat_to_world_px(maxlon, 0, z)
        if (x1 - x0) <= target_px * 1.3:
            return z
    return 0


@dataclass
class Basemap:
    """A stitched basemap image plus its lon/lat → image-pixel transform."""

    image: Image.Image
    attribution: str
    _ox: float          # crop origin in global px
    _oy: float
    _z: int

    def lonlat_to_px(self, lon: float, lat: float) -> tuple[float, float]:
        gx, gy = _lonlat_to_world_px(lon, lat, self._z)
        return gx - self._ox, gy - self._oy

    @property
    def size(self) -> tuple[int, int]:
        return self.image.size


def fetch_basemap(
    bbox_4326: tuple[float, float, float, float],
    *,
    target_px: int = 1100,
    provider: Provider = OSM,
    mml_key: str | None = None,
    session: requests.Session | None = None,
) -> Basemap:
    """Fetch + stitch a basemap for *bbox_4326* ``(minlon, minlat, maxlon, maxlat)``.

    ``target_px`` is the desired width of the cropped image; the zoom is chosen to
    get close to it (capped at the provider's native zoom). Missing/failed tiles
    are left transparent so a partial basemap still renders.
    """
    minlon, minlat, maxlon, maxlat = bbox_4326
    z = _choose_zoom(bbox_4326, target_px, provider.max_zoom)
    sess = session or _SESSION

    # Box corners in global pixels (note: y grows southward).
    x0, y0 = _lonlat_to_world_px(minlon, maxlat, z)   # top-left
    x1, y1 = _lonlat_to_world_px(maxlon, minlat, z)   # bottom-right
    left, right = min(x0, x1), max(x0, x1)
    top, bottom = min(y0, y1), max(y0, y1)

    tx0, ty0 = int(left // _TILE), int(top // _TILE)
    tx1, ty1 = int(right // _TILE), int(bottom // _TILE)
    n = 2 ** z

    canvas = Image.new("RGB", ((tx1 - tx0 + 1) * _TILE, (ty1 - ty0 + 1) * _TILE), (235, 235, 235))
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            if not (0 <= tx < n and 0 <= ty < n):
                continue
            tile = _get_tile(provider, z, tx, ty, mml_key=mml_key, session=sess)
            if tile is not None:
                canvas.paste(tile, ((tx - tx0) * _TILE, (ty - ty0) * _TILE))

    # Crop the canvas to the exact bbox rectangle.
    cox, coy = tx0 * _TILE, ty0 * _TILE
    crop = (int(left - cox), int(top - coy), int(right - cox), int(bottom - coy))
    cropped = canvas.crop(crop)
    return Basemap(image=cropped, attribution=provider.attribution, _ox=left, _oy=top, _z=z)
