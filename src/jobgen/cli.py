"""CLI entrypoint — Phase 8 stub."""

import typer

app = typer.Typer(
    name="jobgen",
    help="DJI M3E terrain-following mapping job generator for Finnish field parcels.",
    no_args_is_help=True,
)

run_app = typer.Typer(help="Generate mapping job artifacts.")
cache_app = typer.Typer(help="Manage the tile cache.")
app.add_typer(run_app, name="run")
app.add_typer(cache_app, name="cache")


@run_app.command()
def run_job(
    parcels: str = typer.Option(None, help="Comma-separated peruslohkotunnus IDs"),
    parcels_file: str = typer.Option(None, help="Path to newline-separated parcel ID file"),
    bbox: str = typer.Option(None, help="Bounding box as 'xmin,ymin,xmax,ymax' (EPSG:3067)"),
    name: str = typer.Option(..., help="Job name (used for output directory)"),
    config: str = typer.Option("config.toml", help="Path to config file"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Fetch and validate only; skip artifact write"),
    offline: bool = typer.Option(False, help="Cache-only; fail on cache miss"),
    refresh: bool = typer.Option(False, help="Force re-fetch of all touched tiles"),
) -> None:
    """Generate a mapping job KMZ + DSM + homes GeoJSON for the given parcels."""
    raise NotImplementedError("Phase 7/8: pipeline not yet implemented")


@cache_app.command("warm")
def cache_warm(
    bbox: str = typer.Option(None, help="Bounding box as 'xmin,ymin,xmax,ymax' (EPSG:3067)"),
    region: str = typer.Option(None, help="Named region (future)"),
    config: str = typer.Option("config.toml"),
) -> None:
    """Pre-fetch all DEM + building tiles for an area."""
    raise NotImplementedError("Phase 8: cache warm not yet implemented")


@cache_app.command("status")
def cache_status(config: str = typer.Option("config.toml")) -> None:
    """Show tile counts, total size, oldest/newest fetch dates."""
    raise NotImplementedError("Phase 8: cache status not yet implemented")


@cache_app.command("refresh")
def cache_refresh(
    older_than: int = typer.Option(None, help="Re-pull tiles older than N days"),
    config: str = typer.Option("config.toml"),
) -> None:
    """Re-fetch stale tiles."""
    raise NotImplementedError("Phase 8: cache refresh not yet implemented")


if __name__ == "__main__":
    app()
