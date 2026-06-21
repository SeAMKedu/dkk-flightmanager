"""Tests for the route-rename + folder-rename backend features.

The naming convention (``YYYYMMDD-NN-base`` prefix, idempotent strip) now lives
server-side in ``job_store`` and is exercised end-to-end through the
``POST /api/jobs/route_rename`` and ``POST /api/folders/{name}/rename`` routes.
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from flightmanager.config import load_config
from flightmanager.storage.job_store import (
    route_rename_name,
    save_params,
    strip_route_prefix,
)
from flightmanager.web.server import create_app


def _client(tmp_path):
    cfg = load_config("config.example.toml")
    cfg.output.output_dir = str(tmp_path)
    return TestClient(create_app(cfg))


def _make_job(output_dir, folder, name, **params):
    """Create a job dir with a job_params.json and return its 'folder/name' path."""
    if folder:
        fdir = output_dir / folder
        fdir.mkdir(parents=True, exist_ok=True)
        (fdir / ".dkk-folder").write_text("", encoding="utf-8")
        job_dir = fdir / name
    else:
        job_dir = output_dir / name
    job_dir.mkdir(parents=True, exist_ok=True)
    doc = {"job_name": name}
    doc.update(params)
    save_params(job_dir, doc)
    return f"{folder}/{name}" if folder else name


# ── strip_route_prefix (shared pure helper) ───────────────────────────────────


class TestStripPrefix:
    def test_strips_two_digit_index(self):
        assert strip_route_prefix("20260608-01-myfarm") == "myfarm"

    def test_strips_three_digit_index(self):
        assert strip_route_prefix("20260608-001-bigfield") == "bigfield"

    def test_does_not_strip_one_digit_index(self):
        assert strip_route_prefix("20260608-1-name") == "20260608-1-name"

    def test_does_not_strip_non_date_prefix(self):
        assert strip_route_prefix("job-01-name") == "job-01-name"

    def test_does_not_strip_short_date(self):
        assert strip_route_prefix("2026060-01-name") == "2026060-01-name"

    def test_no_prefix_unchanged(self):
        assert strip_route_prefix("plainname") == "plainname"

    def test_preserves_hyphens_in_base(self):
        assert strip_route_prefix("20260608-03-my-field-name") == "my-field-name"


# ── route_rename_name (shared pure helper) ────────────────────────────────────


class TestRouteName:
    def test_basic_two_digit(self):
        assert route_rename_name("20260608", 1, 5, "myfarm") == "20260608-01-myfarm"

    def test_two_digit_boundary(self):
        assert route_rename_name("20260608", 99, 99, "x") == "20260608-99-x"

    def test_three_digit_at_100(self):
        assert route_rename_name("20260608", 1, 100, "field") == "20260608-001-field"

    def test_rerename_replaces_prefix(self):
        assert route_rename_name("20260608", 1, 3, "20260601-03-farmA") == (
            "20260608-01-farmA"
        )


# ── POST /api/jobs/route_rename ───────────────────────────────────────────────


class TestRouteRenameEndpoint:
    def test_renames_in_flight_order(self, tmp_path):
        a = _make_job(tmp_path, "trip", "alpha", sort_order=0)
        b = _make_job(tmp_path, "trip", "bravo", sort_order=1)
        c = _make_job(tmp_path, "trip", "charlie", sort_order=2)
        with _client(tmp_path) as client:
            r = client.post(
                "/api/jobs/route_rename",
                json={"paths": [a, b, c], "date": "20260608"},
            )
            assert r.status_code == 200, r.text
            data = r.json()
            assert [e["path"] for e in data["renamed"]] == [
                "trip/20260608-01-alpha",
                "trip/20260608-02-bravo",
                "trip/20260608-03-charlie",
            ]
        assert (tmp_path / "trip" / "20260608-01-alpha").is_dir()
        assert (tmp_path / "trip" / "20260608-03-charlie").is_dir()
        assert not (tmp_path / "trip" / "alpha").exists()

    def test_idempotent_rerun_keeps_names_stable(self, tmp_path):
        a = _make_job(tmp_path, "trip", "alpha", sort_order=0)
        b = _make_job(tmp_path, "trip", "bravo", sort_order=1)
        with _client(tmp_path) as client:
            first = client.post(
                "/api/jobs/route_rename",
                json={"paths": [a, b], "date": "20260608"},
            ).json()
            paths = [e["path"] for e in first["renamed"]]
            second = client.post(
                "/api/jobs/route_rename",
                json={"paths": paths, "date": "20260608"},
            ).json()
        assert [e["path"] for e in second["renamed"]] == paths
        assert all(e["changed"] is False for e in second["renamed"])

    def test_order_swap_does_not_collide(self, tmp_path):
        # Both already carry today's date prefix and the same base, so a swap
        # makes each job's target equal the OTHER's current name — a 409 in the
        # old per-job loop. The two-phase temp rename must avoid it.
        x1 = _make_job(tmp_path, "trip", "20260608-01-foo", sort_order=0, color="#aaa")
        x2 = _make_job(tmp_path, "trip", "20260608-02-foo", sort_order=1, color="#bbb")
        with _client(tmp_path) as client:
            r = client.post(
                "/api/jobs/route_rename",
                json={"paths": [x2, x1], "date": "20260608"},
            )
            assert r.status_code == 200, r.text
        # Net swap: the job that was 02 is now 01, and vice versa.
        c1 = json.loads(
            (tmp_path / "trip" / "20260608-01-foo" / "job_params.json").read_text()
        )
        c2 = json.loads(
            (tmp_path / "trip" / "20260608-02-foo" / "job_params.json").read_text()
        )
        assert c1["color"] == "#bbb"
        assert c2["color"] == "#aaa"

    def test_cross_folder_paths_rejected(self, tmp_path):
        a = _make_job(tmp_path, "trip1", "alpha", sort_order=0)
        b = _make_job(tmp_path, "trip2", "bravo", sort_order=0)
        with _client(tmp_path) as client:
            r = client.post("/api/jobs/route_rename", json={"paths": [a, b]})
            assert r.status_code == 400

    def test_missing_job_404(self, tmp_path):
        with _client(tmp_path) as client:
            r = client.post("/api/jobs/route_rename", json={"paths": ["trip/ghost"]})
            assert r.status_code == 404

    def test_bad_date_400(self, tmp_path):
        a = _make_job(tmp_path, "trip", "alpha", sort_order=0)
        with _client(tmp_path) as client:
            r = client.post(
                "/api/jobs/route_rename", json={"paths": [a], "date": "2026-6-8"}
            )
            assert r.status_code == 400

    def test_empty_paths_noop(self, tmp_path):
        with _client(tmp_path) as client:
            r = client.post("/api/jobs/route_rename", json={"paths": []})
            assert r.status_code == 200
            assert r.json()["renamed"] == []


# ── POST /api/folders/{name}/rename ───────────────────────────────────────────


class TestRenameFolderEndpoint:
    def test_renames_folder_and_keeps_jobs(self, tmp_path):
        _make_job(tmp_path, "alpha", "field1", sort_order=0)
        with _client(tmp_path) as client:
            r = client.post("/api/folders/alpha/rename", json={"new_name": "beta"})
            assert r.status_code == 200, r.text
            assert r.json()["name"] == "beta"
        assert not (tmp_path / "alpha").exists()
        assert (tmp_path / "beta" / "field1" / "job_params.json").exists()
        assert (tmp_path / "beta" / ".dkk-folder").exists()

    def test_target_exists_409(self, tmp_path):
        _make_job(tmp_path, "alpha", "f1")
        _make_job(tmp_path, "beta", "f2")
        with _client(tmp_path) as client:
            r = client.post("/api/folders/alpha/rename", json={"new_name": "beta"})
            assert r.status_code == 409

    def test_missing_folder_404(self, tmp_path):
        with _client(tmp_path) as client:
            r = client.post("/api/folders/ghost/rename", json={"new_name": "beta"})
            assert r.status_code == 404

    def test_invalid_new_name_400(self, tmp_path):
        _make_job(tmp_path, "alpha", "f1")
        with _client(tmp_path) as client:
            r = client.post("/api/folders/alpha/rename", json={"new_name": "bad/name"})
            assert r.status_code == 400

    def test_traversal_new_name_rejected(self, tmp_path):
        _make_job(tmp_path, "alpha", "f1")
        with _client(tmp_path) as client:
            r = client.post("/api/folders/alpha/rename", json={"new_name": ".."})
            assert r.status_code == 400
        assert (tmp_path / "alpha").exists()
