"""Security-hardening tests: path-traversal 400, reveal gating, optional auth."""

from __future__ import annotations

from fastapi.testclient import TestClient

from flightmanager.config import load_config
from flightmanager.web.server import create_app


def _client(tmp_path, **output_overrides):
    cfg = load_config("config.example.toml")
    cfg.output.output_dir = str(tmp_path)
    for k, v in output_overrides.items():
        setattr(cfg.output, k, v)
    return TestClient(create_app(cfg))


class TestPathTraversal:
    def test_folder_query_traversal_400(self, tmp_path):
        # `folder=..` reaches resolve_folder_dir → UnsafePathError → 400.
        # mgrs_tiles is grid-only (no network) and routes through _resolve_centroids.
        with _client(tmp_path) as client:
            r = client.get("/api/mgrs_tiles", params={"folder": ".."})
            assert r.status_code == 400
            assert r.json()["detail"] == "Invalid path"

    def test_export_bad_job_name_400(self, tmp_path):
        with _client(tmp_path) as client:
            r = client.post("/api/export", json={"job_name": "../evil"})
            assert r.status_code == 400


class TestRevealGating:
    def test_reveal_disabled_returns_403(self, tmp_path):
        with _client(tmp_path, allow_local_fs=False) as client:
            r = client.post("/api/jobs/whatever/reveal")
            assert r.status_code == 403

    def test_export_route_disabled_returns_403(self, tmp_path):
        with _client(tmp_path, allow_local_fs=False) as client:
            r = client.post("/api/export-route", json={"dest_dir": str(tmp_path)})
            assert r.status_code == 403


class TestOptionalAuth:
    def test_no_token_env_is_open(self, tmp_path, monkeypatch):
        monkeypatch.delenv("FLIGHTMANAGER_API_TOKEN", raising=False)
        with _client(tmp_path) as client:
            assert client.get("/api/version").status_code == 200

    def test_token_required_when_set(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FLIGHTMANAGER_API_TOKEN", "s3cret")
        with _client(tmp_path) as client:
            assert client.get("/api/version").status_code == 401
            ok = client.get("/api/version", headers={"Authorization": "Bearer s3cret"})
            assert ok.status_code == 200

    def test_wrong_token_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FLIGHTMANAGER_API_TOKEN", "s3cret")
        with _client(tmp_path) as client:
            r = client.get("/api/version", headers={"Authorization": "Bearer nope"})
            assert r.status_code == 401

    def test_ui_shell_stays_public(self, tmp_path, monkeypatch):
        # The HTML shell is not under /api, so it loads without a token (SPA
        # then injects the token for API calls).
        monkeypatch.setenv("FLIGHTMANAGER_API_TOKEN", "s3cret")
        with _client(tmp_path) as client:
            assert client.get("/").status_code == 200
