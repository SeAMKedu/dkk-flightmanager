"""Network download and cache statistics for the current process.

Counters accumulate in-memory for the **whole process lifetime**, by design —
not per job. For the CLI that is a single run; for ``flightmanager serve`` it
spans every job handled since the server started (``/api/stats`` reports these
server-session totals). Call ``print_summary()`` on exit to display a
human-readable table, ``get()`` to retrieve raw numbers, or ``reset()`` to
zero them (used in tests).
"""

from __future__ import annotations

import threading
from pathlib import Path

_lock = threading.Lock()

_downloads: dict[str, int] = {}   # source → network fetches this process
_bytes: dict[str, int] = {}       # source → bytes downloaded this process
_hits: dict[str, int] = {}        # source → cache hits this process

_SOURCES = ("dem", "buildings", "powerlines", "pylons", "parcels", "properties", "zones", "satellites", "weather")

_LABELS: dict[str, str] = {
    "dem":        "DEM tiles   ",
    "buildings":  "Buildings   ",
    "powerlines": "Power lines ",
    "pylons":     "Pylons      ",
    "parcels":    "Parcels     ",
    "properties": "Properties  ",
    "zones":      "UAS zones   ",
    "satellites": "Satellites  ",
    "weather":    "Weather     ",
}


def record_download(source: str, byte_size: int = 0) -> None:
    """Record one network fetch for *source* transferring *byte_size* bytes."""
    with _lock:
        _downloads[source] = _downloads.get(source, 0) + 1
        _bytes[source] = _bytes.get(source, 0) + byte_size


def record_hit(source: str) -> None:
    """Record one cache hit for *source* (no network fetch needed)."""
    with _lock:
        _hits[source] = _hits.get(source, 0) + 1


def get() -> dict:
    """Return a snapshot of all counters as a plain dict."""
    with _lock:
        result: dict[str, dict] = {}
        for src in _SOURCES:
            result[src] = {
                "downloads": _downloads.get(src, 0),
                "bytes":     _bytes.get(src, 0),
                "hits":      _hits.get(src, 0),
            }
        return result


def reset() -> None:
    """Clear all counters (useful in tests)."""
    with _lock:
        _downloads.clear()
        _bytes.clear()
        _hits.clear()


def _fmt_bytes(n: int) -> str:
    if n == 0:
        return ""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def print_summary(cache_dir: "str | Path | None" = None) -> None:
    """Print a session statistics table to stdout. No-op if nothing was tracked."""
    s = get()
    total_dl    = sum(v["downloads"] for v in s.values())
    total_hits  = sum(v["hits"] for v in s.values())
    total_bytes = sum(v["bytes"] for v in s.values())

    if total_dl == 0 and total_hits == 0:
        return

    W = 54
    lines: list[str] = ["─" * W, f"{'Session network statistics':^{W}}", "─" * W]

    for src in _SOURCES:
        v = s[src]
        if v["downloads"] == 0 and v["hits"] == 0:
            continue
        label = _LABELS[src]
        parts: list[str] = []
        if v["downloads"]:
            b = _fmt_bytes(v["bytes"])
            parts.append(f"{v['downloads']} fetched" + (f"  ({b})" if b else ""))
        if v["hits"]:
            parts.append(f"{v['hits']} cached")
        total = v["downloads"] + v["hits"]
        if total > 1 and v["hits"]:
            pct = int(100 * v["hits"] / total)
            parts.append(f"{pct}% cache rate")
        lines.append(f"  {label}{',  '.join(parts)}")

    lines.append("─" * W)
    b_total = _fmt_bytes(total_bytes)
    summary = f"{total_dl} fetched,  {total_hits} cached"
    if b_total:
        summary += f",  {b_total} downloaded"
    lines.append(f"  {'Total':12}{summary}")

    if cache_dir is not None:
        from flightmanager.cache import query_disk_size
        disk_bytes = query_disk_size(cache_dir)
        if disk_bytes:
            lines.append(f"  {'Cache on disk':14}{_fmt_bytes(disk_bytes)}")

    lines.append("─" * W)

    print("\n".join(lines))
