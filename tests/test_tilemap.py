"""Unit tests for tilemap bbox/projection math (no network)."""

from __future__ import annotations

from flightmanager import tilemap


def test_pad_bbox_grows_outward():
    b = (22.0, 62.0, 22.1, 62.05)
    p = tilemap.pad_bbox(b, 0.1)
    assert p[0] < b[0] and p[1] < b[1] and p[2] > b[2] and p[3] > b[3]


def test_fit_bbox_matches_aspect():
    import math
    b = (22.0, 62.0, 22.1, 62.05)
    fitted = tilemap.fit_bbox(b, 2.0)   # want mercator width = 2x height
    wx = math.radians(fitted[2] - fitted[0])                          # mercator-x span
    hy = tilemap._merc_y(fitted[3]) - tilemap._merc_y(fitted[1])      # mercator-y span
    assert abs((wx / hy) - 2.0) < 1e-6
    # only grows
    assert fitted[0] <= b[0] and fitted[2] >= b[2]


def test_fit_bbox_wide_field_stays_landscape():
    # A wide, thin field must not blow up the height (the lon-deg vs merc-rad bug).
    import math
    b = (22.0, 62.000, 22.08, 62.004)   # ~4 km wide, ~0.4 km tall
    fitted = tilemap.fit_bbox(b, 1.58)
    wx = math.radians(fitted[2] - fitted[0])
    hy = tilemap._merc_y(fitted[3]) - tilemap._merc_y(fitted[1])
    assert abs((wx / hy) - 1.58) < 1e-6
    # height span stays a small fraction of the width span (no giant strip)
    assert (fitted[3] - fitted[1]) < (fitted[2] - fitted[0])


def test_world_px_increasing_east_and_south():
    z = 14
    x_w, _ = tilemap._lonlat_to_world_px(22.0, 62.0, z)
    x_e, _ = tilemap._lonlat_to_world_px(23.0, 62.0, z)
    _, y_n = tilemap._lonlat_to_world_px(22.0, 63.0, z)
    _, y_s = tilemap._lonlat_to_world_px(22.0, 62.0, z)
    assert x_e > x_w          # east -> larger x
    assert y_s > y_n          # south -> larger y


def test_choose_zoom_clamped():
    b = (22.0, 62.0, 22.001, 62.001)   # tiny bbox -> wants a high zoom
    assert tilemap._choose_zoom(b, 1000, max_zoom=15) == 15


def test_basemap_transform_maps_corners():
    # A Basemap built from a known bbox maps its corners to image extents.
    bbox = (22.0, 62.0, 22.1, 62.05)
    z = tilemap._choose_zoom(bbox, 800, 19)
    left, top = tilemap._lonlat_to_world_px(22.0, 62.05, z)
    right, bottom = tilemap._lonlat_to_world_px(22.1, 62.0, z)
    from PIL import Image
    img = Image.new("RGB", (int(right - left), int(bottom - top)))
    bm = tilemap.Basemap(image=img, attribution="x", _ox=left, _oy=top, _z=z)
    tlx, tly = bm.lonlat_to_px(22.0, 62.05)
    brx, bry = bm.lonlat_to_px(22.1, 62.0)
    assert abs(tlx) < 1 and abs(tly) < 1
    assert abs(brx - img.size[0]) < 1 and abs(bry - img.size[1]) < 1
