"""Write the DJI Pilot 2 custom map layer KML (building pins).

Extracted from wpml.py so wpml.py can grow toward full per-waypoint WPML
without carrying unrelated homes logic.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from flightmanager.geo.buildings import Building
from flightmanager.config import HomeSafetyConfig

log = logging.getLogger(__name__)

# DJI colour values (AABBGGRR — confirmed from fixture)
_COLOURS = {
    "red": "#FF393CE2",
    "green": "#FF6BBE19",
    "yellow": "#FF00BBFF",
    "blue": "#FFF08C2D",
    "purple": "#FFE020B6",
}

_KOHDELUOKKA_LABELS = {
    42210: "residence",
    42211: "residence",
    42212: "residence",
    42220: "commercial",
    42221: "commercial",
    42222: "commercial",
    42230: "holiday",
    42231: "holiday",
    42232: "holiday",
    42240: "industrial",
    42241: "industrial",
    42242: "industrial",
}


def build_homes_kml(
    buildings: list[Building],
    output_path: Path,
    home_safety: HomeSafetyConfig | None = None,
) -> Path:
    """Write a DJI Pilot 2 custom map layer KML with one pin per building.

    Format confirmed from PIN-20260529224114.kml (fixtures/FIXTURE_NOTES.md).
    The file can be imported into Pilot 2 as a new map layer for situational
    awareness — pins appear on the map overlay during pre-flight planning.

    Color coding:
      red    — buildings subject to the keep-out rule for the configured
               subcategory (residential for A2; residential + commercial +
               holiday + industrial for A3).
      yellow — all other buildings (noted but outside the keep-out codes).

    Blue, purple and green are left free for the operator's own use.

    Returns *output_path*.
    """
    cfg = home_safety or HomeSafetyConfig()

    red_codes = set(cfg.residential_kohdeluokka)
    yellow_codes = set(cfg.a3_additional_kohdeluokka)
    if cfg.operating_subcategory == "A3":
        red_codes |= yellow_codes
        yellow_codes = set()

    shown_codes = red_codes | yellow_codes

    def _style(b: Building) -> str:
        return "#dji_style_red" if b.kohdeluokka in red_codes else "#dji_style_yellow"

    def _label(b: Building) -> str:
        return _KOHDELUOKKA_LABELS.get(b.kohdeluokka, f"building-{b.kohdeluokka}")

    timestamp = time.strftime("%Y%m%d%H%M%S")

    lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append('<kml xmlns="http://www.opengis.net/kml/2.2">')
    lines.append(f'<Document xmlns=""><name>homes-{timestamp}.kml</name>')

    for colour, hex_val in _COLOURS.items():
        lines.append(f'<Style id="dji_style_{colour}">')
        lines.append(f"  <IconStyle><color>{hex_val}</color></IconStyle>")
        lines.append(f"  <LabelStyle><color>{hex_val}</color></LabelStyle>")
        lines.append("</Style>")

    for b in [b for b in buildings if b.kohdeluokka in shown_codes]:
        centroid = b.geometry.centroid
        lines.append("<Placemark>")
        lines.append(f"  <name>{_label(b)}</name>")
        lines.append(
            f"  <description>mtk_id={b.mtk_id} kohdeluokka={b.kohdeluokka}</description>"
        )
        lines.append(f"  <styleUrl>{_style(b)}</styleUrl>")
        lines.append("  <Point>")
        lines.append(f"    <coordinates>{centroid.x},{centroid.y},0.0</coordinates>")
        lines.append("    <altitudeMode>absolute</altitudeMode>")
        lines.append("  </Point>")
        lines.append("</Placemark>")

    lines.append("</Document></kml>")

    pin_count = sum(1 for b in buildings if b.kohdeluokka in shown_codes)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Homes KML written: %d pin(s) → %s", pin_count, output_path)
    return output_path
