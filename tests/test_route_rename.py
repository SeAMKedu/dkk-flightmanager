"""
Tests for the client-side route-rename logic (bulk-ops.js: routeRename).

The JS uses:
    _ROUTE_PREFIX_RE = /^\\d{8}-\\d{2,}-/
    newName = dd + '-' + idx + '-' + baseName

We mirror that here so regressions are caught without a JS runtime.
"""

import re

# Mirror of JS _ROUTE_PREFIX_RE
_ROUTE_PREFIX_RE = re.compile(r"^\d{8}-\d{2,}-")  # mirrors JS: /^\d{8}-\d{2,}-/


def strip_prefix(name: str) -> str:
    return _ROUTE_PREFIX_RE.sub("", name)


def is_routed(params: dict) -> bool:
    """Mirror of the skeleton-job filter in routeRename().

    JS: j.params.sort_order != null || j.params.takeoff_point_4326 != null
    Skeleton jobs have neither and are excluded from the rename sequence.
    """
    return (
        params.get("sort_order") is not None
        or params.get("takeoff_point_4326") is not None
    )


def route_name(date_str: str, index: int, total: int, original_name: str) -> str:
    """Equivalent of the new-name construction in routeRename()."""
    base = strip_prefix(original_name)
    digits = 3 if total >= 100 else 2
    idx = str(index).zfill(digits)
    return f"{date_str}-{idx}-{base}"


# ── strip_prefix ──────────────────────────────────────────────────────────────


class TestStripPrefix:
    def test_strips_two_digit_index(self):
        assert strip_prefix("20260608-01-myfarm") == "myfarm"

    def test_strips_two_digit_index_high(self):
        assert strip_prefix("20260608-99-fieldA") == "fieldA"

    def test_strips_three_digit_index(self):
        assert strip_prefix("20260608-001-bigfield") == "bigfield"

    def test_strips_three_digit_index_high(self):
        assert strip_prefix("20260608-123-parcel") == "parcel"

    def test_does_not_strip_one_digit_index(self):
        # single digit is not matched by \d{2,}
        assert strip_prefix("20260608-1-name") == "20260608-1-name"

    def test_does_not_strip_non_date_prefix(self):
        assert strip_prefix("job-01-name") == "job-01-name"

    def test_does_not_strip_short_date(self):
        assert strip_prefix("2026060-01-name") == "2026060-01-name"

    def test_no_prefix_unchanged(self):
        assert strip_prefix("plainname") == "plainname"

    def test_preserves_hyphens_in_base(self):
        assert strip_prefix("20260608-03-my-field-name") == "my-field-name"

    def test_different_date_stripped(self):
        assert strip_prefix("20251231-12-oldname") == "oldname"


# ── route_name ────────────────────────────────────────────────────────────────


class TestRouteName:
    def test_basic_two_digit(self):
        assert route_name("20260608", 1, 5, "myfarm") == "20260608-01-myfarm"

    def test_two_digit_padding(self):
        assert route_name("20260608", 9, 10, "field") == "20260608-09-field"

    def test_two_digit_boundary(self):
        # 99 jobs → still 2-digit padding
        assert route_name("20260608", 99, 99, "x") == "20260608-99-x"

    def test_three_digit_at_100(self):
        assert route_name("20260608", 1, 100, "field") == "20260608-001-field"

    def test_three_digit_padding(self):
        assert route_name("20260608", 42, 150, "field") == "20260608-042-field"

    def test_rerename_two_digit_replaces_prefix(self):
        # already has a 2-digit prefix from a previous run
        original = "20260601-03-farmA"
        assert route_name("20260608", 1, 3, original) == "20260608-01-farmA"

    def test_rerename_three_digit_replaces_prefix(self):
        original = "20260601-042-bigfield"
        assert route_name("20260608", 5, 10, original) == "20260608-05-bigfield"

    def test_rerename_preserves_inner_hyphens(self):
        original = "20260601-02-my-complex-name"
        assert route_name("20260608", 2, 5, original) == "20260608-02-my-complex-name"

    def test_no_existing_prefix_untouched_base(self):
        assert route_name("20260608", 3, 5, "plainfield") == "20260608-03-plainfield"


# ── is_routed (skeleton filter) ───────────────────────────────────────────────


class TestIsRouted:
    def test_sort_order_alone_is_routed(self):
        assert is_routed({"sort_order": 1, "takeoff_point_4326": None}) is True

    def test_takeoff_point_alone_is_routed(self):
        assert (
            is_routed({"sort_order": None, "takeoff_point_4326": [25.0, 60.0]}) is True
        )

    def test_both_present_is_routed(self):
        assert is_routed({"sort_order": 0, "takeoff_point_4326": [25.0, 60.0]}) is True

    def test_neither_is_skeleton(self):
        assert is_routed({"sort_order": None, "takeoff_point_4326": None}) is False

    def test_missing_keys_is_skeleton(self):
        # batch skeleton jobs have no keys at all
        assert is_routed({}) is False

    def test_sort_order_zero_is_routed(self):
        # 0 is a valid sort_order — must not be treated as falsy
        assert is_routed({"sort_order": 0}) is True

    def test_mixed_selection_only_routed_jobs_renamed(self):
        # Simulate routeRename filtering a mixed selection.
        jobs = [
            {
                "name": "field-A",
                "params": {"sort_order": 1, "takeoff_point_4326": [25.0, 60.0]},
            },
            {
                "name": "skeleton-1",
                "params": {"sort_order": None, "takeoff_point_4326": None},
            },
            {
                "name": "field-B",
                "params": {"sort_order": 2, "takeoff_point_4326": [25.1, 60.1]},
            },
            {"name": "skeleton-2", "params": {}},
        ]
        routed = [j for j in jobs if is_routed(j["params"])]
        assert [j["name"] for j in routed] == ["field-A", "field-B"]
        # Index sequence is 1..n of routed jobs only
        names = [
            route_name("20260608", i + 1, len(routed), j["name"])
            for i, j in enumerate(routed)
        ]
        assert names == ["20260608-01-field-A", "20260608-02-field-B"]
