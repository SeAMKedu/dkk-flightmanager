"""mavic3e-jobgen — DJI M3E terrain-following mapping job generator."""

try:
    from importlib.metadata import version
    __version__ = version("mavic3e-jobgen")
except Exception:
    __version__ = "0.0.0+dev"
