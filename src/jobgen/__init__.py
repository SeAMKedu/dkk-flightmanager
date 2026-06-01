"""dkk-jobgen — DJI terrain-following mapping job generator for Finnish field parcels."""

from __future__ import annotations

import subprocess


def _version_from_git() -> str:
    try:
        tag = subprocess.check_output(
            ["git", "describe", "--tags", "--dirty", "--always"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        return tag if tag else "0.0.0+dev"
    except Exception:
        return "0.0.0+dev"


try:
    from importlib.metadata import version as _pkg_version
    __version__ = _pkg_version("dkk-jobgen")
except Exception:
    __version__ = _version_from_git()


def tool_version() -> str:
    """Return the version string to embed in manifest.json."""
    return __version__
