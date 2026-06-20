"""Tests for the per-session preview store in _server_state."""

from __future__ import annotations

import pytest

import flightmanager._server_state as st


@pytest.fixture(autouse=True)
def _clear_store():
    st.preview_results.clear()
    yield
    st.preview_results.clear()


def test_sessions_are_isolated():
    """Two clients' previews don't clobber each other — the original bug."""
    st.store_preview("alice", {"survey": "A"})
    st.store_preview("bob", {"survey": "B"})
    assert st.get_preview("alice") == {"survey": "A"}
    assert st.get_preview("bob") == {"survey": "B"}


def test_none_session_uses_shared_default():
    st.store_preview(None, {"survey": "default"})
    assert st.get_preview(None) == {"survey": "default"}
    # A named session does not see the default bucket.
    assert st.get_preview("someone") is None


def test_unknown_session_returns_none():
    assert st.get_preview("never-stored") is None


def test_lru_eviction_past_cap():
    for i in range(st.PREVIEW_RESULTS_CAP + 5):
        st.store_preview(f"s{i}", {"n": i})
    assert len(st.preview_results) == st.PREVIEW_RESULTS_CAP
    # Oldest evicted, newest kept.
    assert st.get_preview("s0") is None
    assert st.get_preview(f"s{st.PREVIEW_RESULTS_CAP + 4}") == {"n": st.PREVIEW_RESULTS_CAP + 4}


def test_restore_keeps_recent_session_warm():
    """Re-storing a session refreshes its recency so it survives eviction."""
    st.store_preview("keep", {"v": 1})
    for i in range(st.PREVIEW_RESULTS_CAP):
        st.store_preview(f"s{i}", {"n": i})
        st.store_preview("keep", {"v": 1})  # touch it each round
    assert st.get_preview("keep") == {"v": 1}
