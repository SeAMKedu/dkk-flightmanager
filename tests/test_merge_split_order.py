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
    assert _so(tmp_path, "a") == 0   # at/below `after` is untouched
    assert _so(tmp_path, "b") == 2   # was 1 -> +1
    assert _so(tmp_path, "c") == 3   # was 2 -> +1


def test_excluded_and_unrouted_jobs_are_untouched(tmp_path):
    _make_job(tmp_path, "a", 0)
    _make_job(tmp_path, "keep", 5)        # excluded by name
    _make_job(tmp_path, "unrouted", None)  # no sort_order

    slot = _open_sort_order_slot(tmp_path, after=0, exclude={"keep"})

    assert slot == 1
    assert _so(tmp_path, "keep") == 5      # excluded -> not shifted
    assert _so(tmp_path, "unrouted") is None  # null-safe -> left alone


def test_ignores_dotdirs_and_nonjob_dirs(tmp_path):
    _make_job(tmp_path, "a", 1)
    (tmp_path / ".dkk-folder").mkdir()      # dotdir, no job_params.json
    (tmp_path / "stray").mkdir()            # plain dir, no job_params.json

    slot = _open_sort_order_slot(tmp_path, after=0, exclude=set())

    assert slot == 1
    assert _so(tmp_path, "a") == 2
