"""Dev server entry point for `uvicorn jobgen._devserver:app`."""
from pathlib import Path
from jobgen.config import load_config
from jobgen.server import create_app

_cfg = load_config(Path("config.toml"))
app = create_app(_cfg)
