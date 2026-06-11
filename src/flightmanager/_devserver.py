"""Dev server entry point for `uvicorn flightmanager._devserver:app`."""
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()
from flightmanager.config import load_config
from flightmanager.server import create_app

_cfg = load_config(Path("config.toml"))
app = create_app(_cfg)
