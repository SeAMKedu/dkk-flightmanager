"""Tests for kml_export.py — server-side jobs KML builder."""

from __future__ import annotations

from flightmanager.routing.kml_export import (
    build_jobs_kml,
    hex_to_kml_color,
    ordered_takeoffs,
)

_SQUARE = {
    "type": "Polygon",
    "coordinates": [
        [[25.0, 62.0], [25.1, 62.0], [25.1, 62.1], [25.0, 62.1], [25.0, 62.0]]
    ],
}


class TestHexToKmlColor:
    def test_rrggbb_to_aabbggrr(self):
        # #3b82f6 → alpha + BB GG RR
        assert hex_to_kml_color("#3b82f6", "ff") == "fff6823b"

    def test_short_hex_expanded(self):
        assert hex_to_kml_color("#abc", "55") == "55ccbbaa"

    def test_none_uses_default(self):
        assert hex_to_kml_color(None, "ff").startswith("ff")


class TestOrderedTakeoffs:
    def test_only_jobs_with_takeoff(self):
        jobs = [
            {"job_name": "a", "takeoff_point_4326": [25.0, 62.0]},
            {"job_name": "b"},  # no takeoff
            {"job_name": "c", "takeoff_point_4326": [25.1, 62.1]},
        ]
        assert ordered_takeoffs(jobs) == [("a", [25.0, 62.0]), ("c", [25.1, 62.1])]


class TestBuildJobsKml:
    def test_polygon_and_takeoff_emitted(self):
        jobs = [
            {
                "job_name": "field-1",
                "custom_polygon_4326": _SQUARE,
                "takeoff_point_4326": [25.05, 62.05],
                "color": "#3b82f6",
            }
        ]
        kml = build_jobs_kml(jobs)
        assert kml.startswith("<?xml")
        assert "<Folder><name>field-1</name>" in kml
        assert "<Polygon>" in kml and "25.0,62.0,0" in kml
        assert "<Point><coordinates>25.05,62.05,0</coordinates>" in kml
        assert kml.rstrip().endswith("</kml>")

    def test_id_job_uses_survey_outline(self):
        # No custom polygon — falls back to survey_outline via card_polygon.
        jobs = [
            {
                "job_name": "parcel-1",
                "custom_polygon_4326": None,
                "survey_outline": _SQUARE,
            }
        ]
        kml = build_jobs_kml(jobs)
        assert "<Polygon>" in kml

    def test_xml_escaping(self):
        kml = build_jobs_kml([{"job_name": "a & <b>", "custom_polygon_4326": _SQUARE}])
        assert "a &amp; &lt;b&gt;" in kml

    def test_job_without_geometry_still_has_folder(self):
        kml = build_jobs_kml([{"job_name": "empty"}])
        assert "<Folder><name>empty</name>" in kml
        assert "<Polygon>" not in kml
