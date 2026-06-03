"""CLI entrypoint — Phase 8.

Commands:
  jobgen run    --name <name> --parcels <ids> | --bbox <bbox> | --parcels-file <file>
  jobgen cache  warm | status | refresh
"""

from __future__ import annotations

import json
import os

from dotenv import load_dotenv

load_dotenv()  # reads .env (or .env.local) from cwd upward; no-op if not found
import sqlite3
import sys
import webbrowser
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
# jobgen drones
# ---------------------------------------------------------------------------


@app.command("drones")
def list_drones(
    config_path: str = typer.Option("config.toml", "--config", "-c"),
) -> None:
    """List available drone profiles and their camera specs."""
    cfg = _load_cfg(config_path)
    default = cfg.default_drone

    typer.echo(f"\n{'Name':<18} {'GSD@50m':>8} {'GSD@100m':>9}  {'Label'}")
    typer.echo("-" * 80)
    for d in cfg.drones:
        marker = " *" if d.name == default else "  "
        gsd50  = d.gsd_from_height(50)
        gsd100 = d.gsd_from_height(100)
        typer.echo(
            f"{d.name + marker:<18} {gsd50:>7.2f} cm {gsd100:>8.2f} cm  {d.label}"
        )
    typer.echo(f"\n  * = default drone (override with --drone or default_drone in config.toml)\n")


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
    properties: Optional[str] = typer.Option(
        None, "--properties", "-k",
        help=(
            "Comma-separated kiinteistötunnus values. "
            "Accepts dash form (399-891-1-1) or 14-digit numeric (39989100010001). "
            "May be combined with --parcels."
        ),
    ),
    bbox: Optional[str] = typer.Option(
        None, "--bbox",
        help="Bounding box as 'xmin,ymin,xmax,ymax' in EPSG:3067 metres.",
    ),
    config_path: str = typer.Option(
        "config.toml", "--config", "-c",
        help="Path to config.toml.",
    ),
    drone: Optional[str] = typer.Option(
        None, "--drone",
        help=(
            "Drone + payload profile name (e.g. 'm3m', 'm300-p1-24'). "
            "Must match a name in the [[drones]] list in config.toml. "
            "Overrides default_drone from config."
        ),
    ),
    height: Optional[float] = typer.Option(
        None, "--height",
        help="Override flight height in metres AGL (back-calculates GSD from the active drone's camera constants).",
    ),
    subcategory: Optional[str] = typer.Option(
        None, "--subcategory",
        help="Override operating subcategory: A2 or A3 (default from config).",
    ),
    buffer: Optional[float] = typer.Option(
        None, "--buffer",
        help=(
            "Override home keep-out buffer in metres. "
            "For A2 defaults to derived flight height (≈ flight height from people). "
            "For A3 defaults to 150 m."
        ),
    ),
    homes_distance: Optional[float] = typer.Option(
        None, "--homes-distance",
        help=(
            "Maximum distance (m) from the survey polygon to include a building in the "
            "homes KML, measured to the nearest point on the polygon boundary. "
            "Defaults to 2× the keep-out buffer (e.g. 300 m for A3, 2× flight height for A2)."
        ),
    ),
    preview_radius: Optional[float] = typer.Option(
        None, "--preview-radius",
        help=(
            "Radius (m) of the yellow informational circle drawn around each building "
            "in the HTML map preview. Defaults to 3× flight height (the 3:1 horizontal rule). "
            "E.g. at 100 m AGL the default is 300 m."
        ),
    ),
    simplify: Optional[str] = typer.Option(
        None, "--simplify",
        help=(
            "Polygon simplification: 'auto' to target ≤50 vertices (good for RC touch screen), "
            "a tolerance in metres (e.g. '5'), or '0' to disable. "
            "Overrides config simplify_mode / simplify_tolerance_m."
        ),
    ),
    offset: Optional[float] = typer.Option(
        None, "--offset",
        help=(
            "Expand (+) or contract (−) the survey polygon by this many metres relative to the "
            "parcel/property boundary. Applied after gap-fill and before keep-out subtraction. "
            "Positive values push the survey area outward; negative pull it inward. "
            "Degenerate shapes from contraction are handled by hole_policy / multipart_policy. "
            "Overrides polygon.survey_offset_m from config."
        ),
    ),
    no_keepout: bool = typer.Option(
        False, "--no-keepout",
        help=(
            "Disable automatic keep-out subtraction around buildings. "
            "The survey polygon will not be cut back from buildings, reducing polygon complexity "
            "(fewer vertices from buffer arcs) and allowing closer flight when you have the "
            "landowner's permission. "
            "Buildings are still shown on the preview map with their distance circles. "
            "A prominent warning is added to the HTML preview. "
            "Overrides home_safety.offset_enabled in config."
        ),
    ),
    open_map: bool = typer.Option(
        False, "--open",
        help="Open the HTML map preview in the default browser after the job completes.",
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

    # --- preflight checks ---
    _require_key()

    # --- input validation ---
    # --bbox is exclusive; --parcels / --parcels-file / --properties may be combined.
    area_inputs = sum([
        parcels is not None,
        parcels_file is not None,
        bbox is not None,
        properties is not None,
    ])
    if area_inputs == 0:
        typer.echo(
            "Error: provide at least one of --parcels, --parcels-file, --properties, or --bbox.",
            err=True,
        )
        raise typer.Exit(1)
    if bbox is not None and area_inputs > 1:
        typer.echo("Error: --bbox cannot be combined with other area inputs.", err=True)
        raise typer.Exit(1)
    if parcels is not None and parcels_file is not None:
        typer.echo("Error: --parcels and --parcels-file are mutually exclusive.", err=True)
        raise typer.Exit(1)

    # --- parse inputs ---
    parcel_ids: list[str] | None = None
    property_ids: list[str] | None = None
    bbox_3067: tuple[float, float, float, float] | None = None

    if parcels:
        parcel_ids = [p.strip() for p in parcels.split(",") if p.strip()]

    if parcels_file:
        p = Path(parcels_file)
        if not p.exists():
            typer.echo(f"Error: parcels file not found: {p}", err=True)
            raise typer.Exit(1)
        parcel_ids = [line.strip() for line in p.read_text().splitlines() if line.strip()]

    if properties:
        property_ids = [k.strip() for k in properties.split(",") if k.strip()]

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

    if drone is not None:
        names = [d.name for d in cfg.drones]
        if drone not in names:
            typer.echo(
                f"Error: unknown drone '{drone}'. Available: {', '.join(names)}",
                err=True,
            )
            raise typer.Exit(1)
        cfg.default_drone = drone
        typer.echo(f"Drone override: {drone} ({cfg.active_drone().label})")

    if height is not None:
        active = cfg.active_drone()
        gsd = active.gsd_from_height(height)
        cfg.flight.target_gsd_cm = gsd
        cfg.flight.max_height_agl_m = max(cfg.flight.max_height_agl_m, height + 1)
        typer.echo(f"Height override: {height:.0f} m AGL  (GSD {gsd:.2f} cm/px)")

    if subcategory:
        sub = subcategory.upper()
        if sub not in ("A2", "A3"):
            typer.echo("Error: --subcategory must be A2 or A3.", err=True)
            raise typer.Exit(1)
        cfg.home_safety.operating_subcategory = sub
        # A2: buffer ≈ flight height (EU reg: ≥ flight height from people).
        # Apply automatically unless the operator overrides with --buffer.
        if sub == "A2" and buffer is None:
            cfg.home_safety.home_buffer_m = cfg.active_drone().height_from_gsd(
                cfg.flight.target_gsd_cm
            )
        typer.echo(
            f"Subcategory override: {sub}  "
            f"(buffer {cfg.home_safety.home_buffer_m:.0f} m)"
        )

    if buffer is not None:
        cfg.home_safety.home_buffer_m = buffer
        typer.echo(f"Buffer override: {buffer:.0f} m")

    if homes_distance is not None:
        cfg.home_safety.home_include_buffer_m = homes_distance
        typer.echo(f"Homes distance override: {homes_distance:.0f} m")

    if preview_radius is not None:
        cfg.home_safety.preview_radius_m = preview_radius
        typer.echo(f"Preview radius override: {preview_radius:.0f} m")

    if simplify is not None:
        if simplify.lower() == "auto":
            cfg.polygon.simplify_mode = "auto"
            typer.echo(
                f"Simplify override: auto (target ≤{cfg.polygon.auto_simplify_max_vertices} vertices)"
            )
        else:
            try:
                tol = float(simplify)
                if tol < 0:
                    raise ValueError
                cfg.polygon.simplify_mode = "fixed"
                cfg.polygon.simplify_tolerance_m = tol
                typer.echo(f"Simplify override: {tol:.1f} m tolerance")
            except ValueError:
                typer.echo("Error: --simplify must be 'auto' or a non-negative number.", err=True)
                raise typer.Exit(1)

    if offset is not None:
        cfg.polygon.survey_offset_m = offset
        direction = "outward" if offset > 0 else ("inward" if offset < 0 else "none")
        typer.echo(f"Survey offset override: {offset:+.1f} m ({direction})")

    if no_keepout:
        cfg.home_safety.offset_enabled = False
        typer.echo(
            "⚠  Keep-out disabled — buildings will NOT be subtracted from the survey polygon. "
            "Verify distances to all buildings manually before flying."
        )

    # --- run ---
    typer.echo(f"Starting job '{name}' …")
    try:
        manifest = run_job(
            name, cfg,
            parcel_ids=parcel_ids,
            property_ids=property_ids,
            bbox_3067=bbox_3067,
            dry_run=dry_run,
            refresh=refresh,
        )
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    _print_job_summary(manifest, dry_run)

    if open_map and not dry_run:
        map_path = Path(cfg.output.output_dir) / name / f"{name}_map.html"
        if map_path.exists():
            webbrowser.open(map_path.resolve().as_uri())
        else:
            typer.echo(f"Warning: map file not found at {map_path}", err=True)

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
    vc = g.get("survey_vertex_count")
    if vc is not None:
        typer.echo(f"  Vertices:    {vc}")
    typer.echo(f"  Drone:       {f.get('drone_label', f.get('drone', ''))}")
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


@app.command("serve")
def serve_cmd(
    port: int = typer.Option(8765, "--port", help="Port to listen on."),
    config_path: str = typer.Option("config.toml", "--config", "-c"),
    no_open: bool = typer.Option(False, "--no-open", help="Do not open browser automatically."),
) -> None:
    """Start the browser UI server."""
    import threading
    import webbrowser
    import uvicorn
    from jobgen.server import create_app

    cfg = _load_cfg(config_path)
    web_app = create_app(cfg)
    url = f"http://localhost:{port}"
    typer.echo(f"dkk-jobmaker web UI → {url}")
    typer.echo("Press Ctrl+C to stop.")

    if not no_open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run(web_app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    app()
