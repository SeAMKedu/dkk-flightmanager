"""Validate generated KMZ output against the Phase 0 reference fixture.

All constants here are derived from fixtures/FIXTURE_NOTES.md.
Add tests as wpml.py is implemented in Phase 6.
"""

import zipfile
from pathlib import Path

import pytest
from lxml import etree

FIXTURES = Path(__file__).parent.parent / "fixtures"
REFERENCE_KMZ = FIXTURES / "reference_mission.kmz"
WPML_NS = "http://www.dji.com/wpmz/1.0.6"
KML_NS = "http://www.opengis.net/kml/2.2"
WPML = f"{{{WPML_NS}}}"
KML = f"{{{KML_NS}}}"


# ---------------------------------------------------------------------------
# Fixture validation helpers
# ---------------------------------------------------------------------------


def parse_template_from_kmz(kmz_path: Path) -> etree._Element:
    """Extract and parse template.kml from a KMZ ZIP."""
    with zipfile.ZipFile(kmz_path) as zf:
        names = zf.namelist()
        template_path = next(n for n in names if n.endswith("template.kml"))
        with zf.open(template_path) as f:
            return etree.parse(f).getroot()


def has_waylines(kmz_path: Path) -> bool:
    with zipfile.ZipFile(kmz_path) as zf:
        return any(n.endswith("waylines.wpml") for n in zf.namelist())


# ---------------------------------------------------------------------------
# Reference fixture sanity checks (always run — verify the fixture itself)
# ---------------------------------------------------------------------------


class TestReferenceFixture:
    def test_reference_kmz_exists(self):
        assert REFERENCE_KMZ.exists(), "reference_mission.kmz missing from fixtures/"

    def test_reference_kmz_internal_structure(self):
        """Files must live inside wpmz/ subdirectory within the ZIP."""
        with zipfile.ZipFile(REFERENCE_KMZ) as zf:
            names = zf.namelist()
        assert any(n == "wpmz/template.kml" for n in names), (
            "template.kml must be at wpmz/template.kml inside the KMZ"
        )
        assert any(n == "wpmz/waylines.wpml" for n in names), (
            "waylines.wpml must be at wpmz/waylines.wpml inside the KMZ"
        )

    def test_reference_wpml_namespace(self):
        root = parse_template_from_kmz(REFERENCE_KMZ)
        assert root.nsmap.get("wpml") == WPML_NS

    def test_reference_drone_enum(self):
        root = parse_template_from_kmz(REFERENCE_KMZ)
        drone = root.find(f".//{WPML}droneEnumValue")
        assert drone is not None and drone.text == "77"

    def test_reference_drone_sub_enum(self):
        root = parse_template_from_kmz(REFERENCE_KMZ)
        sub = root.find(f".//{WPML}droneSubEnumValue")
        assert sub is not None and sub.text == "0"

    def test_reference_payload_enum(self):
        root = parse_template_from_kmz(REFERENCE_KMZ)
        p = root.find(f".//{WPML}payloadEnumValue")
        assert p is not None and p.text == "68"

    def test_reference_payload_sub_enum(self):
        root = parse_template_from_kmz(REFERENCE_KMZ)
        p = root.find(f".//{WPML}payloadSubEnumValue")
        assert p is not None and p.text == "3"

    def test_reference_template_type(self):
        root = parse_template_from_kmz(REFERENCE_KMZ)
        tt = root.find(f".//{WPML}templateType")
        assert tt is not None and tt.text == "mapping2d"

    def test_reference_coordinate_mode(self):
        root = parse_template_from_kmz(REFERENCE_KMZ)
        cm = root.find(f".//{WPML}coordinateMode")
        assert cm is not None and cm.text == "WGS84"

    def test_reference_polygon_coordinate_order(self):
        """Coordinates must be lon,lat order (first value ~22°E, second ~62°N for this fixture)."""
        root = parse_template_from_kmz(REFERENCE_KMZ)
        coords_el = root.find(f".//{KML}coordinates")
        assert coords_el is not None
        first_line = coords_el.text.strip().splitlines()[0].strip()
        parts = first_line.split(",")
        assert len(parts) >= 2
        lon, lat = float(parts[0]), float(parts[1])
        # This field is in Finland (~22°E, ~62°N) — lon must be ~22, lat ~62
        assert 20 < lon < 30, f"Expected longitude ~22°E, got {lon} (check coord order!)"
        assert 60 < lat < 70, f"Expected latitude ~62°N, got {lat} (check coord order!)"

    def test_reference_polygon_no_holes(self):
        """Fixture polygon has only outerBoundaryIs, no interior holes."""
        root = parse_template_from_kmz(REFERENCE_KMZ)
        inner = root.findall(f".//{KML}innerBoundaryIs")
        assert len(inner) == 0, "Reference fixture polygon should have no interior holes"

    def test_reference_height_fields_consistent(self):
        """globalShootHeight, ellipsoidHeight, and height must all be the same value."""
        root = parse_template_from_kmz(REFERENCE_KMZ)
        gh = root.find(f".//{WPML}globalShootHeight")
        eh = root.find(f".//{WPML}ellipsoidHeight")
        h = root.find(f".//{WPML}height")
        assert gh is not None and eh is not None and h is not None
        assert abs(float(gh.text) - float(eh.text)) < 0.001
        assert abs(float(gh.text) - float(h.text)) < 0.001

    def test_reference_waylines_present(self):
        assert has_waylines(REFERENCE_KMZ), (
            "waylines.wpml missing from reference KMZ — "
            "generator must include it (see open question 6)"
        )


# ---------------------------------------------------------------------------
# Generated KMZ validation (skipped until Phase 6 produces output)
# ---------------------------------------------------------------------------


@pytest.fixture
def generated_kmz(tmp_path):
    """Returns a generated KMZ for the reference field, or skips if not yet built."""
    pytest.skip("Phase 6 not yet implemented — wpml.py is a stub")


class TestGeneratedKMZ:
    """Validate that the generator reproduces the fixture schema exactly."""

    def test_internal_structure(self, generated_kmz):
        with zipfile.ZipFile(generated_kmz) as zf:
            names = zf.namelist()
        assert any(n == "wpmz/template.kml" for n in names)
        assert any(n == "wpmz/waylines.wpml" for n in names)

    def test_wpml_namespace(self, generated_kmz):
        root = parse_template_from_kmz(generated_kmz)
        assert root.nsmap.get("wpml") == WPML_NS

    def test_drone_enum(self, generated_kmz):
        root = parse_template_from_kmz(generated_kmz)
        assert root.find(f".//{WPML}droneEnumValue").text == "77"

    def test_payload_enum(self, generated_kmz):
        root = parse_template_from_kmz(generated_kmz)
        assert root.find(f".//{WPML}payloadEnumValue").text == "68"

    def test_template_type(self, generated_kmz):
        root = parse_template_from_kmz(generated_kmz)
        assert root.find(f".//{WPML}templateType").text == "mapping2d"

    def test_coordinate_order(self, generated_kmz):
        root = parse_template_from_kmz(generated_kmz)
        coords_el = root.find(f".//{KML}coordinates")
        first = coords_el.text.strip().splitlines()[0].strip().split(",")
        lon, lat = float(first[0]), float(first[1])
        assert 20 < lon < 30
        assert 60 < lat < 70

    def test_no_holes(self, generated_kmz):
        root = parse_template_from_kmz(generated_kmz)
        assert len(root.findall(f".//{KML}innerBoundaryIs")) == 0

    def test_height_fields_consistent(self, generated_kmz):
        root = parse_template_from_kmz(generated_kmz)
        gh = float(root.find(f".//{WPML}globalShootHeight").text)
        eh = float(root.find(f".//{WPML}ellipsoidHeight").text)
        h = float(root.find(f".//{WPML}height").text)
        assert abs(gh - eh) < 0.001
        assert abs(gh - h) < 0.001

    def test_height_within_ceiling(self, generated_kmz):
        root = parse_template_from_kmz(generated_kmz)
        h = float(root.find(f".//{WPML}globalShootHeight").text)
        assert h <= 110.0, f"Generated flight height {h} m exceeds allowed ceiling"
