"""CLI entrypoint.

Commands:
  flightmanager run    --name <name> --parcels <ids> | --bbox <bbox> | --parcels-file <file>
  flightmanager cache  warm | status | refresh
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()  # reads .env (or .env.local) from cwd upward; no-op if not found
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(
    name="flightmanager",
    help="DJI terrain-following flight manager for Finnish agricultural field surveys.",
    no_args_is_help=True,
)
cache_app = typer.Typer(help="Manage the tile cache.", no_args_is_help=True)
app.add_typer(cache_app, name="cache")


def _parse_area_inputs(
    parcels: str | None,
    properties: str | None,
    bbox: str | None,
) -> tuple[
    list[str] | None, list[str] | None, tuple[float, float, float, float] | None
]:
    """Parse and validate the three mutually-exclusive area input flags.

    Returns (parcel_ids, property_ids, bbox_3067). Raises typer.Exit(1) on invalid input.
    """
    parcel_ids: list[str] | None = None
    property_ids: list[str] | None = None
    bbox_3067: tuple[float, float, float, float] | None = None

    if parcels:
        parcel_ids = [p.strip() for p in parcels.split(",") if p.strip()]
    if properties:
        property_ids = [k.strip() for k in properties.split(",") if k.strip()]
    if bbox:
        try:
            parts = [float(x) for x in bbox.split(",")]
            if len(parts) != 4:
                raise ValueError
            bbox_3067 = (parts[0], parts[1], parts[2], parts[3])
        except ValueError:
            typer.echo(
                "Error: --bbox must be 'xmin,ymin,xmax,ymax' (four floats).", err=True
            )
            raise typer.Exit(1)

    return parcel_ids, property_ids, bbox_3067


def _apply_run_overrides(  # noqa: C901
    cfg,
    *,
    drone: str | None,
    height: float | None,
    subcategory: str | None,
    buffer: float | None,
    homes_distance: float | None,
    preview_radius: float | None,
    simplify: str | None,
    offset: float | None,
    no_keepout: bool,
    offline: bool,
) -> None:
    """Apply CLI overrides to cfg in-place. Raises typer.Exit(1) on invalid values."""
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
                typer.echo(
                    "Error: --simplify must be 'auto' or a non-negative number.",
                    err=True,
                )
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


def _collect_batch_ids(
    parcels: str | None,
    properties: str | None,
    file: Path | None,
) -> list[str]:
    """Collect IDs from inline flags and/or a text file into a single flat list."""
    raw_ids: list[str] = []
    if parcels is not None:
        raw_ids.extend(p.strip() for p in parcels.split(",") if p.strip())
    if properties is not None:
        raw_ids.extend(k.strip() for k in properties.split(",") if k.strip())
    if file is not None:
        for line in Path(file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                raw_ids.extend(p.strip() for p in line.split(",") if p.strip())
    return raw_ids


def _detect_id_type(
    raw_ids: list[str], parcels: str | None, properties: str | None
) -> str:
    """Return 'parcels' or 'properties'. Uses flag presence first, regex fallback."""
    if parcels is not None:
        return "parcels"
    if properties is not None:
        return "properties"
    import re

    id_type = "parcels" if re.match(r"^\d{8,}$", raw_ids[0]) else "properties"
    typer.echo(f"Auto-detected ID type: {id_type}")
    return id_type


def _print_batch_results(results: list[dict]) -> int:
    """Print per-ID outcome table. Returns the count of failures."""
    ok = skipped = failed = 0
    for r in results:
        if r["status"] == "ok":
            ok += 1
            typer.echo(f"  ✓  {r['id']}")
        elif r["status"] == "skipped":
            skipped += 1
            typer.echo(f"  –  {r['id']}  (skipped: {r.get('reason', '')})")
        else:
            failed += 1
            typer.echo(f"  ✗  {r['id']}  {r.get('reason', '')}")
    typer.echo()
    typer.echo(f"Created: {ok}  Skipped: {skipped}  Failed: {failed}")
    return failed


# ---------------------------------------------------------------------------
# flightmanager drones
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
        gsd50 = d.gsd_from_height(50)
        gsd100 = d.gsd_from_height(100)
        typer.echo(
            f"{d.name + marker:<18} {gsd50:>7.2f} cm {gsd100:>8.2f} cm  {d.label}"
        )
    typer.echo(
        "\n  * = default drone (override with --drone or default_drone in config.toml)\n"
    )


# ---------------------------------------------------------------------------
# flightmanager run
# ---------------------------------------------------------------------------


@app.command("run")
def run_job_cmd(
    name: str = typer.Option(
        ..., "--name", "-n", help="Job name (used as output subdirectory)."
    ),
    parcels: Optional[str] = typer.Option(
        None,
        "--parcels",
        "-p",
        help="Comma-separated peruslohkotunnus IDs.",
    ),
    properties: Optional[str] = typer.Option(
        None,
        "--properties",
        "-k",
        help=(
            "Comma-separated kiinteistötunnus values. "
            "Accepts dash form (399-891-1-1) or 14-digit numeric (39989100010001). "
            "May be combined with --parcels."
        ),
    ),
    bbox: Optional[str] = typer.Option(
        None,
        "--bbox",
        help="Bounding box as 'xmin,ymin,xmax,ymax' in EPSG:3067 metres.",
    ),
    config_path: str = typer.Option(
        "config.toml",
        "--config",
        "-c",
        help="Path to config.toml.",
    ),
    drone: Optional[str] = typer.Option(
        None,
        "--drone",
        help=(
            "Drone + payload profile name (e.g. 'm3m', 'm300-p1-24'). "
            "Must match a name in the [[drones]] list in config.toml. "
            "Overrides default_drone from config."
        ),
    ),
    height: Optional[float] = typer.Option(
        None,
        "--height",
        help="Override flight height in metres AGL (back-calculates GSD from the active drone's camera constants).",
    ),
    subcategory: Optional[str] = typer.Option(
        None,
        "--subcategory",
        help="Override operating subcategory: A2 or A3 (default from config).",
    ),
    buffer: Optional[float] = typer.Option(
        None,
        "--buffer",
        help=(
            "Override home keep-out buffer in metres. "
            "For A2 defaults to derived flight height (≈ flight height from people). "
            "For A3 defaults to 150 m."
        ),
    ),
    homes_distance: Optional[float] = typer.Option(
        None,
        "--homes-distance",
        help=(
            "Maximum distance (m) from the survey polygon to include a building in the "
            "homes KML, measured to the nearest point on the polygon boundary. "
            "Defaults to 2× the keep-out buffer (e.g. 300 m for A3, 2× flight height for A2)."
        ),
    ),
    preview_radius: Optional[float] = typer.Option(
        None,
        "--preview-radius",
        help=(
            "Radius (m) of the yellow informational circle drawn around each building "
            "in the HTML map preview. Defaults to 3× flight height (the 3:1 horizontal rule). "
            "E.g. at 100 m AGL the default is 300 m."
        ),
    ),
    simplify: Optional[str] = typer.Option(
        None,
        "--simplify",
        help=(
            "Polygon simplification: 'auto' to target ≤50 vertices (good for RC touch screen), "
            "a tolerance in metres (e.g. '5'), or '0' to disable. "
            "Overrides config simplify_mode / simplify_tolerance_m."
        ),
    ),
    offset: Optional[float] = typer.Option(
        None,
        "--offset",
        help=(
            "Expand (+) or contract (−) the survey polygon by this many metres relative to the "
            "parcel/property boundary. Applied after gap-fill and before keep-out subtraction. "
            "Positive values push the survey area outward; negative pull it inward. "
            "Degenerate shapes from contraction are handled by hole_policy / multipart_policy. "
            "Overrides polygon.survey_offset_m from config."
        ),
    ),
    no_keepout: bool = typer.Option(
        False,
        "--no-keepout",
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
        False,
        "--open",
        help="Reveal the job output folder in the system file manager after the job completes.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Fetch and validate only — skip writing output files.",
    ),
    offline: bool = typer.Option(
        False,
        "--offline",
        help="Cache-only mode — fail cleanly on any cache miss.",
    ),
    refresh: bool = typer.Option(
        False,
        "--refresh",
        help="Force re-fetch of all touched cache tiles.",
    ),
) -> None:
    """Generate a mapping job: KMZ route, terrain-follow DSM, and homes map layer."""
    from flightmanager.config import load_config
    from flightmanager.pipeline import export_job

    _require_key()

    area_inputs = sum([parcels is not None, bbox is not None, properties is not None])
    if area_inputs == 0:
        typer.echo(
            "Error: provide at least one of --parcels, --properties, or --bbox.",
            err=True,
        )
        raise typer.Exit(1)
    if bbox is not None and area_inputs > 1:
        typer.echo("Error: --bbox cannot be combined with other area inputs.", err=True)
        raise typer.Exit(1)

    parcel_ids, property_ids, bbox_3067 = _parse_area_inputs(parcels, properties, bbox)

    try:
        cfg = load_config(config_path)
    except FileNotFoundError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    except Exception as e:
        typer.echo(f"Config error: {e}", err=True)
        raise typer.Exit(1)

    _apply_run_overrides(
        cfg,
        drone=drone,
        height=height,
        subcategory=subcategory,
        buffer=buffer,
        homes_distance=homes_distance,
        preview_radius=preview_radius,
        simplify=simplify,
        offset=offset,
        no_keepout=no_keepout,
        offline=offline,
    )

    typer.echo(f"Starting job '{name}' …")
    try:
        manifest, _route_geojson = export_job(
            name,
            cfg,
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
        import subprocess
        import sys

        job_dir = Path(cfg.output.output_dir) / name
        if job_dir.is_dir():
            opener = {"darwin": "open", "win32": "explorer"}.get(
                sys.platform, "xdg-open"
            )
            subprocess.Popen([opener, str(job_dir)])
        else:
            typer.echo(f"Warning: job folder not found at {job_dir}", err=True)

    from flightmanager.net_stats import print_summary as _print_net_stats

    _print_net_stats(cfg.cache.cache_dir)

    if manifest.get("needs_review") or not manifest.get("flight_ready"):
        raise typer.Exit(2)


# ---------------------------------------------------------------------------
# flightmanager cache warm
# ---------------------------------------------------------------------------


@cache_app.command("warm")
def cache_warm(
    bbox: str = typer.Option(
        ...,
        "--bbox",
        help="Bounding box as 'xmin,ymin,xmax,ymax' in EPSG:3067 metres.",
    ),
    config_path: str = typer.Option("config.toml", "--config", "-c"),
    refresh: bool = typer.Option(
        False, "--refresh", help="Force re-fetch of all tiles."
    ),
) -> None:
    """Pre-fetch DEM and building tiles for an area ahead of a field day."""
    from flightmanager.buildings import tile_fetcher as b_fetcher
    from flightmanager.cache import covering_tiles, get_tiles
    from flightmanager.elevation import tile_fetcher as d_fetcher

    try:
        parts = [float(x) for x in bbox.split(",")]
        bbox_3067 = (parts[0], parts[1], parts[2], parts[3])
    except (ValueError, IndexError):
        typer.echo(
            "Error: --bbox must be 'xmin,ymin,xmax,ymax' (four floats).", err=True
        )
        raise typer.Exit(1)

    cfg = _load_cfg(config_path)
    api_key = _require_key()

    n_tiles = len(covering_tiles(bbox_3067, cfg.cache.tile_size_m))
    typer.echo(f"Warming {n_tiles} tile(s) for bbox {bbox_3067} …")

    dem_records = get_tiles(
        "dem", bbox_3067, d_fetcher(api_key), cfg.cache, refresh=refresh
    )
    typer.echo(f"  DEM:       {len(dem_records)} tile(s) cached.")

    bldg_records = get_tiles(
        "buildings", bbox_3067, b_fetcher(api_key), cfg.cache, refresh=refresh
    )
    typer.echo(f"  Buildings: {len(bldg_records)} tile(s) cached.")

    typer.echo("Cache warm complete.")
    from flightmanager.net_stats import print_summary as _print_net_stats

    _print_net_stats(cfg.cache.cache_dir)


# ---------------------------------------------------------------------------
# flightmanager cache status
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

    with closing(sqlite3.connect(db_path)) as conn, conn:
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
        grand_total = (
            conn.execute("SELECT SUM(byte_size) FROM tiles").fetchone()[0] or 0
        )

    if not rows:
        typer.echo("Cache index exists but is empty.")
        return

    max_mb = cfg.cache.max_cache_size_mb
    limit_str = (
        f" / {_human_size(max_mb * 1024 * 1024)}" if max_mb > 0 else " (unlimited)"
    )
    pct_str = (
        f"  ({100 * grand_total // (max_mb * 1024 * 1024)}% full)" if max_mb > 0 else ""
    )

    typer.echo(f"\nCache: {db_path}")
    typer.echo(f"Total: {_human_size(grand_total)}{limit_str}{pct_str}\n")
    typer.echo(
        f"{'Dataset':<12} {'Tiles':>6} {'Size':>10}  {'Oldest fetch':<26} {'Newest fetch'}"
    )
    typer.echo("-" * 80)
    for dataset, tiles, total_bytes, oldest, newest in rows:
        size_str = _human_size(total_bytes or 0)
        typer.echo(
            f"{dataset:<12} {tiles:>6} {size_str:>10}  "
            f"{(oldest or '')[:19]:<26} {(newest or '')[:19]}"
        )
    typer.echo()


# ---------------------------------------------------------------------------
# flightmanager cache refresh
# ---------------------------------------------------------------------------


@cache_app.command("refresh")
def cache_refresh(
    older_than: Optional[int] = typer.Option(
        None,
        "--older-than",
        help="Re-fetch tiles older than N days. Defaults to each dataset's configured TTL.",
    ),
    config_path: str = typer.Option("config.toml", "--config", "-c"),
) -> None:
    """Re-fetch stale cache tiles."""
    import sqlite3
    from flightmanager.buildings import tile_fetcher as b_fetcher
    from flightmanager.cache import _db_path, get_tiles, TileBbox
    from flightmanager.elevation import tile_fetcher as d_fetcher

    cfg = _load_cfg(config_path)
    api_key = _require_key()
    db = _db_path(cfg.cache.cache_dir)

    if not db.exists():
        typer.echo("Cache is empty — nothing to refresh.")
        return

    cutoff_days = (
        older_than  # None means use per-dataset TTL (handled by get_tiles refresh flag)
    )

    with closing(sqlite3.connect(db)) as conn, conn:
        if cutoff_days is not None:
            cutoff_ts = (
                datetime.now(timezone.utc) - timedelta(days=cutoff_days)
            ).isoformat()
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
        "dem": d_fetcher(api_key),
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
            typer.echo(
                f"  Warning: failed to refresh {dataset}/{tile_id}: {e}", err=True
            )

    typer.echo(f"Refreshed {refreshed}/{len(rows)} tile(s).")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_cfg(config_path: str):
    from flightmanager.config import load_config

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
    typer.echo(
        f"  Area:        {g.get('original_area_ha', 0):.2f} ha → "
        f"{g.get('final_area_ha', 0):.2f} ha "
        f"({g.get('area_lost_pct', 0):.1f}% lost to keep-out)"
    )
    vc = g.get("survey_vertex_count")
    if vc is not None:
        typer.echo(f"  Vertices:    {vc}")
    typer.echo(f"  Drone:       {f.get('drone_label', f.get('drone', ''))}")
    typer.echo(
        f"  Height:      {f.get('derived_height_m', 0):.1f} m AGL  "
        f"GSD {f.get('target_gsd_cm', 0):.1f} cm/px"
    )

    if isinstance(b, dict) and "estimated_flight_time_min" in b:
        battery_warn = " ⚠ EXCEEDS ONE BATTERY" if b.get("over_one_battery") else ""
        typer.echo(
            f"  Flight time: ~{b.get('estimated_flight_time_min', 0):.0f} min  "
            f"~{b.get('estimated_photo_count', 0)} photos{battery_warn}"
        )

    zone_hits = z.get("intersecting_zones", [])
    if zone_hits:
        typer.echo(
            f"  Zones:       {len(zone_hits)} restricted zone(s) intersect survey area!"
        )
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
        "✓ FLIGHT READY"
        if manifest.get("flight_ready")
        else "✗ NOT FLIGHT READY — review required"
    )
    typer.echo(f"\n  Status: {status}")
    typer.echo()


@app.command("batch")
def batch_cmd(
    parcels: Optional[str] = typer.Option(
        None,
        "--parcels",
        "-p",
        help=(
            "Comma-separated peruslohkotunnus IDs, or omit the value to use --parcels "
            "as a type selector with --file (e.g. --parcels --file ids.txt). "
            "Cannot be combined with --properties."
        ),
    ),
    properties: Optional[str] = typer.Option(
        None,
        "--properties",
        "-k",
        help=(
            "Comma-separated kiinteistötunnus values, or bare flag with --file. "
            "Cannot be combined with --parcels."
        ),
    ),
    file: Optional[Path] = typer.Option(
        None,
        "--file",
        "-f",
        help="Text file with one ID per line. # lines and blank lines are skipped.",
        exists=True,
        file_okay=True,
        dir_okay=False,
    ),
    folder: Optional[str] = typer.Option(
        None,
        "--folder",
        help="Output subfolder to group the batch jobs under.",
    ),
    drone: Optional[str] = typer.Option(
        None, "--drone", help="Drone profile override."
    ),
    height: Optional[float] = typer.Option(
        None, "--height", help="Flight height (m AGL)."
    ),
    subcategory: Optional[str] = typer.Option(None, "--subcategory", help="A2 or A3."),
    config_path: str = typer.Option("config.toml", "--config", "-c"),
) -> None:
    """Create skeleton jobs from a list of parcel or property IDs.

    Fetches parcel/property geometry and writes job_params.json for each ID —
    no KMZ or DSM is generated.  Jobs appear in the UI ready to edit and export.

    ID type is determined by which flag is used.  If neither --parcels nor
    --properties is given, the type is auto-detected from the ID format
    (all-digits → parcels; NNN-NNN-N-NN → properties).
    """
    from flightmanager.batch import create_skeleton_jobs

    _require_key()
    cfg = _load_cfg(config_path)

    if parcels is not None and properties is not None:
        typer.echo("Error: --parcels and --properties cannot be combined.", err=True)
        raise typer.Exit(1)

    raw_ids = _collect_batch_ids(parcels, properties, file)
    if not raw_ids:
        typer.echo(
            "Error: no IDs provided (use --parcels, --properties, and/or --file).",
            err=True,
        )
        raise typer.Exit(1)

    id_type = _detect_id_type(raw_ids, parcels, properties)

    if subcategory:
        subcategory = subcategory.upper()
        if subcategory not in ("A2", "A3"):
            typer.echo("Error: --subcategory must be A2 or A3.", err=True)
            raise typer.Exit(1)

    import copy

    cfg = copy.deepcopy(cfg)
    if drone:
        cfg.default_drone = drone
    if height is not None:
        active = cfg.active_drone()
        cfg.flight.target_gsd_cm = active.gsd_from_height(height)
    if subcategory:
        cfg.home_safety.operating_subcategory = subcategory

    params = {
        "drone": cfg.default_drone,
        "height_m": height,
        "subcategory": subcategory or cfg.home_safety.operating_subcategory,
        "offset_m": cfg.polygon.survey_offset_m,
        "simplify": "auto"
        if cfg.polygon.simplify_mode == "auto"
        else str(cfg.polygon.simplify_tolerance_m),
        "keepout": cfg.home_safety.offset_enabled,
        "preview_radius_m": None,
    }

    output_dir = Path(cfg.output.output_dir).resolve()
    typer.echo(f"\nCreating {len(raw_ids)} skeleton job(s)  [type={id_type}]")
    if folder:
        typer.echo(f"Folder: {folder}")
    typer.echo()

    results = create_skeleton_jobs(
        raw_ids,
        id_type,
        output_dir,
        folder,
        params,
        progress_cb=None,
        config=cfg,
    )

    failed = _print_batch_results(results)
    from flightmanager.net_stats import print_summary as _print_net_stats

    _print_net_stats(cfg.cache.cache_dir)
    if failed:
        raise typer.Exit(1)


def _collect_job_centroids(
    out_dir: Path, folder: str | None
) -> list[tuple[float, float]]:
    """Return (lat, lon) centroids of all job polygons in *out_dir* (or one folder)."""
    from shapely.geometry import shape

    from flightmanager.job_store import best_polygon, is_job_dir

    points: list[tuple[float, float]] = []
    base = out_dir / folder if folder else out_dir
    if not base.exists():
        return points
    for d in sorted(base.iterdir()):
        if not d.is_dir() or not is_job_dir(d):
            continue
        geom = best_polygon(d)
        if not geom:
            continue
        c = shape(geom).centroid
        points.append((c.y, c.x))
    return points


def _collect_stale_paths(
    output_dir: Path, cache_config, folder: Optional[str]
) -> list[str]:
    """Return job paths flagged stale (skipping untouched skeletons), optionally one folder."""
    import json

    from flightmanager.job_store import refresh_status, resolve_job_dir, scan_jobs
    from flightmanager.manifest import PIPELINE_VERSION

    targets: list[str] = []
    for group in scan_jobs(output_dir):
        if folder and group["name"] != folder:
            continue
        for card in group["jobs"]:
            if card.get("untouched"):
                continue
            _, _, jd = resolve_job_dir(output_dir, card["path"])
            manifest_path = jd / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if refresh_status(manifest, cache_config, PIPELINE_VERSION)[
                "needs_refresh"
            ]:
                targets.append(card["path"])
    return targets


@app.command("refresh")
def refresh_cmd(
    paths: list[str] = typer.Argument(
        default=None, help="Job paths (folder/name or name) to refresh."
    ),
    all_stale: bool = typer.Option(
        False,
        "--all-stale",
        help="Refresh every job flagged stale (pipeline / source data).",
    ),
    folder: Optional[str] = typer.Option(
        None, "--folder", help="Limit --all-stale to one group folder."
    ),
    config_path: str = typer.Option("config.toml", "--config", "-c"),
) -> None:
    """Recompute exported jobs in place with the current pipeline (recompute-only).

    Pass explicit job paths, or --all-stale to refresh every job whose pipeline_version
    is behind or whose source tiles the cache now holds a newer copy of (--folder narrows
    that to one group). The edited / ID-derived geometry is preserved — only the route,
    DSM, stats, KMZ and manifest are recomputed from cached tiles.
    """
    from filelock import Timeout

    import flightmanager._server_state as _st
    from flightmanager._pipeline_lock import pipeline_lock
    from flightmanager.routers.execution import _refresh_one_job

    cfg = _load_cfg(config_path)
    _st.config = cfg  # _refresh_one_job builds per-job config from the shared state
    output_dir = Path(cfg.output.output_dir).resolve()

    targets = (
        _collect_stale_paths(output_dir, cfg.cache, folder)
        if (all_stale or folder)
        else list(paths or [])
    )

    if not targets:
        typer.echo("No jobs to refresh.")
        raise typer.Exit(0)

    typer.echo(f"Refreshing {len(targets)} job(s) …")
    ok = flips = failed = skipped = 0
    try:
        with pipeline_lock(cfg.cache.cache_dir):
            for i, p in enumerate(targets, 1):
                typer.echo(f"[{i}/{len(targets)}] {p}")
                try:
                    r = _refresh_one_job(p, output_dir)
                    if r["status"] == "ok":
                        ok += 1
                        if r["flips"]:
                            flips += 1
                            typer.echo("    ⚠ " + "; ".join(r["flips"]))
                    else:
                        skipped += 1
                        typer.echo(f"    skipped: {r.get('reason', '')}")
                except Exception as e:
                    failed += 1
                    typer.echo(f"    ERROR: {e}", err=True)
    except Timeout:
        typer.echo(
            "Pipeline busy — another process holds the lock. Try again shortly.",
            err=True,
        )
        raise typer.Exit(1)

    typer.echo(
        f"Done — {ok} recomputed, {flips} with flag changes, {skipped} skipped, {failed} failed."
    )


@app.command("satellites")
def satellites_cmd(
    folder: Optional[str] = typer.Option(
        None, "--folder", help="Only consider jobs in this output subfolder."
    ),
    point: Optional[str] = typer.Option(
        None, "--point", help="Check a single 'lat,lon' point instead of jobs."
    ),
    config_path: str = typer.Option("config.toml", "--config", "-c"),
) -> None:
    """List upcoming satellite overpasses for the job grid square(s).

    Computes near-nadir overpasses of the tracked Earth-observation satellites
    (configured in [satellites]) over the Sentinel-2 MGRS tile(s) that the jobs
    fall in. Requires the MGRS grid file (see [satellites].grid_file).

      flightmanager satellites --folder my-group
      flightmanager satellites --point 62.79,22.84
    """
    from collections import defaultdict

    from flightmanager.satellites import overpasses_for_points

    cfg = _load_cfg(config_path)

    if point:
        try:
            lat_s, lon_s = point.split(",")
            points = [(float(lat_s), float(lon_s))]
        except ValueError:
            typer.echo("Error: --point must be 'lat,lon'", err=True)
            raise typer.Exit(1)
    else:
        points = _collect_job_centroids(Path(cfg.output.output_dir), folder)
        if not points:
            typer.echo(
                "No job polygons found. Use --point to test a coordinate.", err=True
            )
            raise typer.Exit(1)

    typer.echo(f"Checking {len(points)} location(s)…")
    result = overpasses_for_points(points, cfg.satellites, cfg.cache.cache_dir)

    if not result.grid_ok:
        typer.echo(f"⚠ {result.grid_msg}", err=True)
        raise typer.Exit(1)
    typer.echo(f"MGRS tile(s): {', '.join(result.tile_ids) or '(none)'}")
    if result.grid_msg:
        typer.echo(result.grid_msg)
    if not result.overpasses:
        typer.echo("No qualifying overpasses in the search window.")
        raise typer.Exit(0)

    by_day: dict[str, list] = defaultdict(list)
    for op in result.overpasses:
        by_day[op.peak_utc.strftime("%Y-%m-%d")].append(op)

    typer.echo("")
    for day in sorted(by_day):
        typer.echo(day)
        for op in by_day[day]:
            t = op.peak_utc.strftime("%H:%M UTC")
            typer.echo(
                f"  {t}  {op.name:<14} {op.tile_id:<6} peak {op.max_elev_deg:.0f}°"
            )
    typer.echo("")
    typer.echo(result.attribution)


@app.command("mcp")
def mcp_cmd(
    config_path: str = typer.Option("config.toml", "--config", "-c"),
) -> None:
    """Start the MCP server in standalone stdio mode (no web UI required).

    The primary way to use MCP is via flightmanager serve — the MCP server is
    mounted at /mcp/sse on the running web UI server:

      flightmanager serve
      # Claude Desktop: {"url": "http://localhost:8765/mcp/sse"}
      # Claude Code:    claude mcp add flightmanager --url http://localhost:8765/mcp/sse

    Use this command only when you need MCP without the web UI running:

      flightmanager mcp
      # Claude Desktop: {"command": "flightmanager", "args": ["mcp"], "env": {"MML_API_KEY": "..."}}
      # Claude Code:    claude mcp add flightmanager -- flightmanager mcp
    """
    from flightmanager.mcp_server import mcp, set_config_path

    set_config_path(config_path)
    mcp.run(transport="stdio")


@app.command("serve")
def serve_cmd(
    port: int = typer.Option(8765, "--port", help="Port to listen on."),
    config_path: str = typer.Option("config.toml", "--config", "-c"),
    no_open: bool = typer.Option(
        False, "--no-open", help="Do not open browser automatically."
    ),
) -> None:
    """Start the browser UI server."""
    import threading
    import webbrowser
    import uvicorn
    from flightmanager.server import create_app

    cfg = _load_cfg(config_path)
    web_app = create_app(cfg, config_path=str(Path(config_path).resolve()))
    url = f"http://localhost:{port}"
    typer.echo(f"dkk-flightmanager web UI → {url}")
    typer.echo("Press Ctrl+C to stop.")

    if not no_open:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    # Suppress CancelledError noise from SSE connections cancelled at shutdown.
    # Starlette's listen_for_disconnect raises CancelledError when uvicorn
    # force-cancels open connections after the graceful-shutdown timeout;
    # this is expected behaviour, not a real error.
    import asyncio as _asyncio
    import logging as _logging

    class _DropCancelledError(_logging.Filter):
        def filter(self, record: _logging.LogRecord) -> bool:
            if record.exc_info and record.exc_info[0] is not None:
                if issubclass(record.exc_info[0], _asyncio.CancelledError):
                    return False
            return True

    _logging.getLogger("uvicorn.error").addFilter(_DropCancelledError())

    uvicorn.run(
        web_app,
        host="127.0.0.1",
        port=port,
        log_level="info",
        access_log=False,
        timeout_graceful_shutdown=3,
    )


@app.command("report")
def report_cmd(
    paths: list[str] = typer.Argument(
        default=None, help="Job paths (folder/name or name) to report on."
    ),
    folder: Optional[str] = typer.Option(
        None, "--folder", help="Report every job in this group folder."
    ),
    packet: bool = typer.Option(
        False,
        "--packet",
        help="Mission packet (cover + overview + launch sites + cards).",
    ),
    basemap: str = typer.Option(
        "mml", "--basemap", help="Basemap: 'mml' (orthophoto) or 'osm'."
    ),
    no_cards: bool = typer.Option(
        False, "--no-cards", help="Packet without the per-job detail cards."
    ),
    out: Optional[str] = typer.Option(None, "--out", "-o", help="Output PDF path."),
    open_pdf: bool = typer.Option(
        False, "--open", help="Open the PDF after generating."
    ),
    config_path: str = typer.Option("config.toml", "--config", "-c"),
) -> None:
    """Generate a PDF flight card (single job) or mission packet (multiple jobs).

    A single job path produces a one-page card. Multiple paths, ``--folder``, or
    ``--packet`` produce a mission packet: cover, overview map, per-launch-site
    flight-announcement pages, then the per-job cards.
    """
    import flightmanager._server_state as _st
    from flightmanager import report
    from flightmanager.job_store import scan_jobs
    from flightmanager.routers.management import _load_job_entry

    cfg = _load_cfg(config_path)
    _st.config = cfg
    output_dir = Path(cfg.output.output_dir).resolve()

    targets = list(paths or [])
    if folder:
        for group in scan_jobs(output_dir):
            if group["name"] == folder:
                targets += [c["path"] for c in group["jobs"]]
    if not targets:
        typer.echo("No jobs given. Pass job paths or --folder.", err=True)
        raise typer.Exit(1)

    entries = [e for p in targets if (e := _load_job_entry(output_dir, p))]
    if not entries:
        typer.echo("No matching jobs found.", err=True)
        raise typer.Exit(1)

    as_packet = packet or folder or len(entries) > 1
    typer.echo(
        f"Rendering {'packet' if as_packet else 'card'} for {len(entries)} job(s) …"
    )
    if as_packet:
        pdf = report.render_packet(
            cfg, entries, folder=folder, basemap=basemap, include_job_cards=not no_cards
        )
        default_name = f"dkk-{folder or 'packet'}.pdf"
    else:
        e = entries[0]
        pdf = report.render_job_report(cfg, e["params"], e["manifest"], basemap=basemap)
        default_name = f"{e['params'].get('job_name') or 'job'}.pdf"

    out_path = Path(out) if out else (output_dir / default_name)
    out_path.write_bytes(pdf)
    typer.echo(f"Wrote {out_path}  ({len(pdf) // 1024} KB)")
    if open_pdf:
        typer.launch(str(out_path))


if __name__ == "__main__":
    app()
