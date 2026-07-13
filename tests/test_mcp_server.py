"""Unit tests for the MCP tools that don't need network or the pipeline.

The FastMCP ``@mcp.tool()`` decorator returns the underlying function, so the
tools are called directly. Each returns a JSON string. ``_st.config`` is pointed
at a tmp output dir so ``_output_dir()`` resolves there (the tools run in
"integrated" mode whenever ``_st.config`` is set)."""

from __future__ import annotations

import json

import pytest

import flightmanager.mcp_server as mcp
import flightmanager.web._server_state as _st
from flightmanager.config import load_config
from flightmanager.storage.job_store import save_params


@pytest.fixture
def out_dir(tmp_path):
    cfg = load_config("config.example.toml")
    cfg.output.output_dir = str(tmp_path)
    prev = _st.config
    _st.config = cfg
    try:
        yield tmp_path
    finally:
        _st.config = prev


def _job(out, folder, name, **params):
    job_dir = (out / folder / name) if folder else (out / name)
    job_dir.mkdir(parents=True, exist_ok=True)
    if folder:
        (out / folder / ".dkk-folder").write_text("", encoding="utf-8")
    doc = {"job_name": name}
    doc.update(params)
    save_params(job_dir, doc)
    return f"{folder}/{name}" if folder else name


_SQUARE = {
    "type": "Polygon",
    "coordinates": [
        [[25.0, 62.0], [25.001, 62.0], [25.001, 62.001], [25.0, 62.001], [25.0, 62.0]]
    ],
}


# ── lifecycle ─────────────────────────────────────────────────────────────────


def test_rename_job(out_dir):
    p = _job(out_dir, "trip", "alpha", color="#abc")
    res = json.loads(mcp.rename_job(p, "bravo"))
    assert res["ok"] and res["path"] == "trip/bravo"
    assert (out_dir / "trip" / "bravo" / "job_params.json").exists()
    assert not (out_dir / "trip" / "alpha").exists()


def test_rename_job_invalid_name(out_dir):
    p = _job(out_dir, "trip", "alpha")
    assert "error" in json.loads(mcp.rename_job(p, "bad/name"))


def test_rename_job_missing(out_dir):
    assert "error" in json.loads(mcp.rename_job("trip/ghost", "x"))


def test_move_job_to_folder_and_root(out_dir):
    p = _job(out_dir, None, "loose")
    res = json.loads(mcp.move_job(p, "dest"))
    assert res["ok"] and res["path"] == "dest/loose"
    assert (out_dir / "dest" / "loose").is_dir()
    # move back to root
    res2 = json.loads(mcp.move_job("dest/loose", None))
    assert res2["path"] == "loose"
    # empty source folder auto-removed
    assert not (out_dir / "dest").exists()


def test_move_job_collision(out_dir):
    _job(out_dir, "a", "x")
    _job(out_dir, "b", "x")
    assert "error" in json.loads(mcp.move_job("a/x", "b"))


def test_clone_job(out_dir):
    p = _job(out_dir, "trip", "alpha", color="#123")
    res = json.loads(mcp.clone_job(p))
    assert res["ok"] and res["name"] == "alpha-copy"
    clone = json.loads(
        (out_dir / "trip" / "alpha-copy" / "job_params.json").read_text()
    )
    assert clone["job_name"] == "alpha-copy"
    assert clone["color"] == "#123"


def test_set_job_color(out_dir):
    p = _job(out_dir, None, "j")
    json.loads(mcp.set_job_color(p, "#ff0000"))
    stored = json.loads((out_dir / "j" / "job_params.json").read_text())
    assert stored["color"] == "#ff0000"
    json.loads(mcp.set_job_color(p, None))
    stored = json.loads((out_dir / "j" / "job_params.json").read_text())
    assert stored["color"] is None


def test_set_job_skipped(out_dir):
    p = _job(out_dir, None, "j")
    json.loads(mcp.set_job_skipped(p, True))
    stored = json.loads((out_dir / "j" / "job_params.json").read_text())
    assert stored["skipped"] is True


# ── route organization ────────────────────────────────────────────────────────


def test_reorder_route(out_dir):
    a = _job(out_dir, "trip", "a", sort_order=0)
    b = _job(out_dir, "trip", "b", sort_order=1)
    res = json.loads(mcp.reorder_route([b, a]))
    assert res["ok"] and res["ordered"] == 2
    pa = json.loads((out_dir / "trip" / "a" / "job_params.json").read_text())
    pb = json.loads((out_dir / "trip" / "b" / "job_params.json").read_text())
    assert pb["sort_order"] == 0 and pa["sort_order"] == 1


def test_reorder_route_cross_folder_rejected(out_dir):
    a = _job(out_dir, "f1", "a", sort_order=0)
    b = _job(out_dir, "f2", "b", sort_order=0)
    assert "error" in json.loads(mcp.reorder_route([a, b]))


def test_route_rename(out_dir):
    a = _job(out_dir, "trip", "alpha", sort_order=0)
    b = _job(out_dir, "trip", "bravo", sort_order=1)
    res = json.loads(mcp.route_rename([a, b], date="20260608"))
    assert res["ok"]
    assert [e["path"] for e in res["renamed"]] == [
        "trip/20260608-01-alpha",
        "trip/20260608-02-bravo",
    ]


def test_route_rename_bad_date(out_dir):
    a = _job(out_dir, "trip", "alpha", sort_order=0)
    assert "error" in json.loads(mcp.route_rename([a], date="2026-6-8"))


def test_rename_folder(out_dir):
    _job(out_dir, "alpha", "j", sort_order=0)
    res = json.loads(mcp.rename_folder("alpha", "beta"))
    assert res["ok"] and res["name"] == "beta"
    assert (out_dir / "beta" / "j" / "job_params.json").exists()
    assert not (out_dir / "alpha").exists()


def test_rename_folder_collision(out_dir):
    _job(out_dir, "alpha", "j")
    _job(out_dir, "beta", "k")
    assert "error" in json.loads(mcp.rename_folder("alpha", "beta"))


def test_launch_sites(out_dir):
    _job(
        out_dir,
        "trip",
        "a",
        sort_order=0,
        takeoff_point_4326=[25.0005, 62.0005],
        custom_polygon_4326=_SQUARE,
    )
    _job(
        out_dir,
        "trip",
        "b",
        sort_order=1,
        takeoff_point_4326=[25.0006, 62.0006],
        custom_polygon_4326=_SQUARE,
    )
    res = json.loads(mcp.launch_sites("trip"))
    assert res["site_count"] >= 1
    site = res["sites"][0]
    assert "radius_m" in site and "members" in site


# ── scan_stale ────────────────────────────────────────────────────────────────


def test_scan_stale_flags_old_pipeline_version(out_dir):
    job_dir = out_dir / "trip" / "old"
    job_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "trip" / ".dkk-folder").write_text("", encoding="utf-8")
    save_params(job_dir, {"job_name": "old"})
    # A manifest with an old pipeline_version makes the job stale.
    (job_dir / "manifest.json").write_text(
        json.dumps({"job_name": "old", "pipeline_version": 0}), encoding="utf-8"
    )
    res = json.loads(mcp.scan_stale("trip"))
    assert res["stale_count"] == 1
    assert res["stale"][0]["path"] == "trip/old"
    assert any("pipeline" in r for r in res["stale"][0]["reasons"])


def test_scan_stale_skips_current_version(out_dir):
    from flightmanager.storage.manifest import PIPELINE_VERSION

    job_dir = out_dir / "trip" / "cur"
    job_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "trip" / ".dkk-folder").write_text("", encoding="utf-8")
    save_params(job_dir, {"job_name": "cur"})
    (job_dir / "manifest.json").write_text(
        json.dumps({"job_name": "cur", "pipeline_version": PIPELINE_VERSION}),
        encoding="utf-8",
    )
    res = json.loads(mcp.scan_stale("trip"))
    assert res["stale_count"] == 0
