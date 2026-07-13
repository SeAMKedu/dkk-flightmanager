"""Unit tests for _open_sort_order_slot (merge/split flight-order placement)."""

from flightmanager.storage.job_store import load_params, save_params
from flightmanager.web.routers.management import _open_sort_order_slot


def _make_job(folder_dir, name, sort_order):
    job_dir = folder_dir / name
    job_dir.mkdir(parents=True, exist_ok=True)
    params = {"job_name": name}
    if sort_order is not None:
        params["sort_order"] = sort_order
    save_params(job_dir, params)
    return job_dir


def _so(folder_dir, name):
    return load_params(folder_dir / name).get("sort_order")


def test_opens_slot_after_and_shifts_later_siblings(tmp_path):
    _make_job(tmp_path, "a", 0)
    _make_job(tmp_path, "b", 1)
    _make_job(tmp_path, "c", 2)

    # Insert right after slot 0 ("a"); b and c shift up by one.
    slot = _open_sort_order_slot(tmp_path, after=0, exclude=set())

    assert slot == 1
    assert _so(tmp_path, "a") == 0  # at/below `after` is untouched
    assert _so(tmp_path, "b") == 2  # was 1 -> +1
    assert _so(tmp_path, "c") == 3  # was 2 -> +1


def test_excluded_and_unrouted_jobs_are_untouched(tmp_path):
    _make_job(tmp_path, "a", 0)
    _make_job(tmp_path, "keep", 5)  # excluded by name
    _make_job(tmp_path, "unrouted", None)  # no sort_order

    slot = _open_sort_order_slot(tmp_path, after=0, exclude={"keep"})

    assert slot == 1
    assert _so(tmp_path, "keep") == 5  # excluded -> not shifted
    assert _so(tmp_path, "unrouted") is None  # null-safe -> left alone


def test_ignores_dotdirs_and_nonjob_dirs(tmp_path):
    _make_job(tmp_path, "a", 1)
    (tmp_path / ".dkk-folder").mkdir()  # dotdir, no job_params.json
    (tmp_path / "stray").mkdir()  # plain dir, no job_params.json

    slot = _open_sort_order_slot(tmp_path, after=0, exclude=set())

    assert slot == 1
    assert _so(tmp_path, "a") == 2


# ---------------------------------------------------------------------------
# Split endpoint — thumbnail regeneration (regression)
# ---------------------------------------------------------------------------


def test_split_regenerates_thumbnails_for_both_halves(tmp_path):
    """Splitting must rewrite the original job's thumbnail (it kept the whole-area
    card before) and write one for the new half."""
    from fastapi.testclient import TestClient
    from flightmanager.config import load_config
    from flightmanager.storage.job_store import make_thumbnail_svg
    from flightmanager.web.server import create_app

    cfg = load_config("config.example.toml")
    cfg.output.output_dir = str(tmp_path)

    # Whole area is an L-shape; polygon_a is a plain square (distinct outline), so
    # the regenerated thumbnail differs from the pre-split one.
    whole = {
        "type": "Polygon",
        "coordinates": [
            [
                [25.0, 62.0],
                [25.2, 62.0],
                [25.2, 62.1],
                [25.1, 62.1],
                [25.1, 62.2],
                [25.0, 62.2],
                [25.0, 62.0],
            ]
        ],
    }
    half_a = {
        "type": "Polygon",
        "coordinates": [
            [[25.0, 62.0], [25.1, 62.0], [25.1, 62.1], [25.0, 62.1], [25.0, 62.0]]
        ],
    }
    half_b = {
        "type": "Polygon",
        "coordinates": [
            [[25.1, 62.0], [25.2, 62.0], [25.2, 62.1], [25.1, 62.1], [25.1, 62.0]]
        ],
    }

    jd = tmp_path / "orig"
    jd.mkdir()
    save_params(jd, {"job_name": "orig", "custom_polygon_4326": whole})
    (jd / "thumbnail.svg").write_text(make_thumbnail_svg(whole), encoding="utf-8")
    whole_svg = (jd / "thumbnail.svg").read_text(encoding="utf-8")

    app = create_app(cfg)
    with TestClient(app) as client:
        r = client.post(
            "/api/jobs/orig/split", json={"polygon_a": half_a, "polygon_b": half_b}
        )
    assert r.status_code == 200
    new_name = r.json()["new_name"]

    orig_after = (jd / "thumbnail.svg").read_text(encoding="utf-8")
    new_thumb = (tmp_path / new_name / "thumbnail.svg").read_text(encoding="utf-8")

    assert orig_after == make_thumbnail_svg(half_a)  # regenerated from polygon_a
    assert orig_after != whole_svg  # no longer the whole-area card
    assert new_thumb == make_thumbnail_svg(half_b)  # new half has its own thumbnail
