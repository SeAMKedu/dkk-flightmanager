"""CLI entrypoint — Phase 8.

Commands:
  jobgen run    --name <name> --parcels <ids> | --bbox <bbox> | --parcels-file <file>
  jobgen cache  warm | status | refresh
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(
    name="jobgen",
    help="DJI M3E terrain-following mapping job generator for Finnish field parcels.",
    no_args_is_help=True,
)
cache_app = typer.Typer(help="Manage the tile cache.", no_args_is_help=True)
app.add_typer(cache_app, name="cache")


# ---------------------------------------------------------------------------
# jobgen run
# ---------------------------------------------------------------------------


@app.command("run")
def run_job_cmd(
    name: str = typer.Option(..., "--name", "-n", help="Job name (used as output subdirectory)."),
    parcels: Optional[str] = typer.Option(
        None, "--parcels", "-p",
        help="Comma-separated peruslohkotunnus IDs.",
    ),
    parcels_file: Optional[str] = typer.Option(
        None, "--parcels-file",
        help="Path to a newline-separated file of parcel IDs.",
    ),
    bbox: Optional[str] = typer.Option(
        None, "--bbox",
        help="Bounding box as 'xmin,ymin,xmax,ymax' in EPSG:3067 metres.",
    ),
    config_path: str = typer.Option(
        "config.toml", "--config", "-c",
        help="Path to config.toml.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Fetch and validate only — skip writing output files.",
    ),
    offline: bool = typer.Option(
        False, "--offline",
        help="Cache-only mode — fail cleanly on any cache miss.",
    ),
    refresh: bool = typer.Option(
        False, "--refresh",
        help="Force re-fetch of all touched cache tiles.",
    ),
) -> None:
    """Generate a mapping job: KMZ route, terrain-follow DSM, and homes map layer."""
    from jobgen.config import load_config
    from jobgen.pipeline import run_job

    # --- input validation ---
    inputs = sum([
        parcels is not None,
        parcels_file is not None,
        bbox is not None,
    ])
    if inputs == 0:
        typer.echo("Error: provide --parcels, --parcels-file, or --bbox.", err=True)
        raise typer.Exit(1)
    if inputs > 1:
        typer.echo("Error: --parcels, --parcels-file, and --bbox are mutually exclusive.", err=True)
        raise typer.Exit(1)

    # --- parse inputs ---
    parcel_ids: list[str] | None = None
    bbox_3067: tuple[float, float, float, float] | None = None

    if parcels:
        parcel_ids = [p.strip() for p in parcels.split(",") if p.strip()]

    if parcels_file:
        p = Path(parcels_file)
        if not p.exists():
            typer.echo(f"Error: parcels file not found: {p}", err=True)
            raise typer.Exit(1)
        parcel_ids = [line.strip() for line in p.read_text().splitlines() if line.strip()]

    if bbox:
        try:
            parts = [float(x) for x in bbox.split(",")]
            if len(parts) != 4:
                raise ValueError
            bbox_3067 = (parts[0], parts[1], parts[2], parts[3])
        except ValueError:
            typer.echo("Error: --bbox must be 'xmin,ymin,xmax,ymax' (four floats).", err=True)
            raise typer.Exit(1)

    # --- load config ---
    try:
        cfg = load_config(config_path)
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    except Exception as e:
        typer.echo(f"Config error: {e}", err=True)
        raise typer.Exit(1)

    if offline:
        cfg.cache.offline = True

    # --- run ---
    typer.echo(f"Starting job '{name}' …")
    try:
        manifest = run_job(
            name, cfg,
            parcel_ids=parcel_ids,
            bbox_3067=bbox_3067,
            dry_run=dry_run,
            refresh=refresh,
        )
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    _print_job_summary(manifest, dry_run)

    if manifest.get("needs_review") or not manifest.get("flight_ready"):
        raise typer.Exit(2)   # non-zero so scripts can detect review-needed


# ---------------------------------------------------------------------------
# jobgen cache warm
# ---------------------------------------------------------------------------


@cache_app.command("warm")
def cache_warm(
    bbox: str = typer.Option(
        ..., "--bbox",
        help="Bounding box as 'xmin,ymin,xmax,ymax' in EPSG:3067 metres.",
    ),
    config_path: str = typer.Option("config.toml", "--config", "-c"),
    refresh: bool = typer.Option(False, "--refresh", help="Force re-fetch of all tiles."),
) -> None:
    """Pre-fetch DEM and building tiles for an area ahead of a field day."""
    import os
    from jobgen.buildings import tile_fetcher as b_fetcher
    from jobgen.cache import covering_tiles, get_tiles
    from jobgen.config import load_config
    from jobgen.elevation import tile_fetcher as d_fetcher

    try:
        parts = [float(x) for x in bbox.split(",")]
        bbox_3067 = (parts[0], parts[1], parts[2], parts[3])
    except (ValueError, IndexError):
        typer.echo("Error: --bbox must be 'xmin,ymin,xmax,ymax' (four floats).", err=True)
        raise typer.Exit(1)

    cfg = _load_cfg(config_path)
    api_key = _require_key()

    n_tiles = len(covering_tiles(bbox_3067, cfg.cache.tile_size_m))
    typer.echo(f"Warming {n_tiles} tile(s) for bbox {bbox_3067} …")

    dem_records = get_tiles("dem", bbox_3067, d_fetcher(api_key), cfg.cache, refresh=refresh)
    typer.echo(f"  DEM:       {len(dem_records)} tile(s) cached.")

    bldg_records = get_tiles("buildings", bbox_3067, b_fetcher(api_key), cfg.cache, refresh=refresh)
    typer.echo(f"  Buildings: {len(bldg_records)} tile(s) cached.")

    typer.echo("Cache warm complete.")


# ---------------------------------------------------------------------------
# jobgen cache status
# ---------------------------------------------------------------------------


@cache_app.command("status")
def cache_status(
    config_path: str = typer.Option("config.toml", "--config", "-c"),
) -> None:
    """Show tile counts, total size, and date ranges for the local cache."""
    cfg = _load_cfg(config_path)
    db_path = Path(cfg.cache.cache_dir) / "index.sqlite"

    if not db_path.exists():
        typer.echo("Cache is empty (no index.sqlite found).")
        return

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("""
            SELECT dataset,
                   COUNT(*) as tiles,
                   SUM(byte_size) as total_bytes,
                   MIN(fetch_timestamp) as oldest,
                   MAX(fetch_timestamp) as newest
            FROM tiles
            GROUP BY dataset
            ORDER BY dataset
        """).fetchall()

    if not rows:
        typer.echo("Cache index exists but is empty.")
        return

    typer.echo(f"\nCache: {db_path}\n")
    typer.echo(f"{'Dataset':<12} {'Tiles':>6} {'Size':>10}  {'Oldest fetch':<26} {'Newest fetch'}")
    typer.echo("-" * 80)
    for dataset, tiles, total_bytes, oldest, newest in rows:
        size_str = _human_size(total_bytes or 0)
        typer.echo(
            f"{dataset:<12} {tiles:>6} {size_str:>10}  "
            f"{(oldest or '')[:19]:<26} {(newest or '')[:19]}"
        )
    typer.echo()


# ---------------------------------------------------------------------------
# jobgen cache refresh
# ---------------------------------------------------------------------------


@cache_app.command("refresh")
def cache_refresh(
    older_than: Optional[int] = typer.Option(
        None, "--older-than",
        help="Re-fetch tiles older than N days. Defaults to each dataset's configured TTL.",
    ),
    config_path: str = typer.Option("config.toml", "--config", "-c"),
) -> None:
    """Re-fetch stale cache tiles."""
    import sqlite3
    from jobgen.buildings import tile_fetcher as b_fetcher
    from jobgen.cache import _db_path, _init_db, get_tiles, TileBbox
    from jobgen.config import load_config
    from jobgen.elevation import tile_fetcher as d_fetcher

    cfg = _load_cfg(config_path)
    api_key = _require_key()
    db = _db_path(cfg.cache.cache_dir)

    if not db.exists():
        typer.echo("Cache is empty — nothing to refresh.")
        return

    cutoff_days = older_than  # None means use per-dataset TTL (handled by get_tiles refresh flag)

    with sqlite3.connect(db) as conn:
        if cutoff_days is not None:
            cutoff_ts = (datetime.now(timezone.utc) - timedelta(days=cutoff_days)).isoformat()
            rows = conn.execute(
                "SELECT dataset, tile_id, xmin, ymin, xmax, ymax "
                "FROM tiles WHERE fetch_timestamp < ?",
                (cutoff_ts,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT dataset, tile_id, xmin, ymin, xmax, ymax FROM tiles"
            ).fetchall()

    if not rows:
        typer.echo("No stale tiles found.")
        return

    typer.echo(f"Re-fetching {len(rows)} stale tile(s) …")

    fetchers = {
        "dem":       d_fetcher(api_key),
        "buildings": b_fetcher(api_key),
    }

    refreshed = 0
    for dataset, tile_id, xmin, ymin, xmax, ymax in rows:
        fetcher = fetchers.get(dataset)
        if fetcher is None:
            continue
        tile_bbox: TileBbox = (xmin, ymin, xmax, ymax)
        try:
            get_tiles(dataset, tile_bbox, fetcher, cfg.cache, refresh=True)
            refreshed += 1
        except Exception as e:
            typer.echo(f"  Warning: failed to refresh {dataset}/{tile_id}: {e}", err=True)

    typer.echo(f"Refreshed {refreshed}/{len(rows)} tile(s).")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_cfg(config_path: str):
    from jobgen.config import load_config
    try:
        return load_config(config_path)
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    except Exception as e:
        typer.echo(f"Config error: {e}", err=True)
        raise typer.Exit(1)


def _require_key() -> str:
    key = os.environ.get("MML_API_KEY", "")
    if not key:
        typer.echo(
            "Error: MML_API_KEY environment variable is not set.\n"
            "Obtain a key at https://www.maanmittauslaitos.fi/rajapinnat/api-avaimen-ohje\n"
            "and add it to your .env file.",
            err=True,
        )
        raise typer.Exit(1)
    return key


def _human_size(n_bytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n_bytes < 1024:
            return f"{n_bytes:.0f} {unit}"
        n_bytes //= 1024
    return f"{n_bytes:.0f} TB"


def _print_job_summary(manifest: dict, dry_run: bool) -> None:
    prefix = "[DRY RUN] " if dry_run else ""
    g = manifest.get("geometry", {})
    f = manifest.get("flight", {})
    b = manifest.get("battery", {})
    z = manifest.get("zones", {})

    typer.echo()
    typer.echo(f"{prefix}Job '{manifest['job_name']}' complete")
    typer.echo(f"  Area:        {g.get('original_area_ha', 0):.2f} ha → "
               f"{g.get('final_area_ha', 0):.2f} ha "
               f"({g.get('area_lost_pct', 0):.1f}% lost to keep-out)")
    typer.echo(f"  Height:      {f.get('derived_height_m', 0):.1f} m AGL  "
               f"GSD {f.get('target_gsd_cm', 0):.1f} cm/px")

    if isinstance(b, dict) and "estimated_flight_time_min" in b:
        battery_warn = " ⚠ EXCEEDS ONE BATTERY" if b.get("over_one_battery") else ""
        typer.echo(f"  Flight time: ~{b.get('estimated_flight_time_min', 0):.0f} min  "
                   f"~{b.get('estimated_photo_count', 0)} photos{battery_warn}")

    zone_hits = z.get("intersecting_zones", [])
    if zone_hits:
        typer.echo(f"  Zones:       {len(zone_hits)} restricted zone(s) intersect survey area!")
    elif not z.get("checked"):
        typer.echo("  Zones:       check skipped (no zone data configured)")
    else:
        typer.echo("  Zones:       clear")

    reasons = manifest.get("review_reasons", [])
    if reasons:
        typer.echo()
        typer.echo("  ⚠ NEEDS REVIEW:")
        for r in reasons:
            typer.echo(f"    - {r}")

    status = (
        "✓ FLIGHT READY" if manifest.get("flight_ready")
        else "✗ NOT FLIGHT READY — review required"
    )
    typer.echo(f"\n  Status: {status}")
    typer.echo()


if __name__ == "__main__":
    app()
