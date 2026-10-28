"""A survey system with heights above a geoid (``EPSG:7792+3855``, the one the program proposes) handed to the
consumers of 2D geometries - the layers of PyArchInit, the GeoPackages of QGIS and QField, the QGIS projects, IFC: they
all take the horizontal part of the system (``EPSG:7792``), its heights staying in the attributes that carry them. A
compound system has no EPSG code of its own: before, PyArchInit wrote the numbers of the chunk without reprojecting
them (2,020 km off in a layer in Monte Mario), the GeoPackages had no system and QGIS an invalid one."""
from __future__ import annotations

import json
import sqlite3
import subprocess

import pytest
from pyproj import Transformer
from shapely import wkt as swkt
from shapely.geometry import box

from pyarchinit_photogrammetry.core import crs as crs_mod
from pyarchinit_photogrammetry.io import gpkg

COMPOUND = "EPSG:7792+3855"  # RDN2008 / UTM 33N with heights above EGM2008
ROME = (12.4964, 41.9028)


def _rome_utm() -> tuple[float, float]:
    return Transformer.from_crs("EPSG:4326", "EPSG:7792", always_xy=True).transform(*ROME)


def test_the_epsg_code_of_a_compound_system_is_that_of_its_plan():
    assert crs_mod.epsg_code(COMPOUND) == 7792
    assert crs_mod.epsg_code("EPSG:32633+3855") == 32633
    assert crs_mod.epsg_code("EPSG:7792") == 7792 and crs_mod.epsg_code("EPSG:3004") == 3004
    assert crs_mod.epsg_code("EPSG:7415") == 28992  # a compound system with a code of its own: its plan too
    assert crs_mod.epsg_code("LOCAL") is None and crs_mod.epsg_code("") is None
    assert crs_mod.epsg_code("not a system") is None


@pytest.mark.slow
def test_pyarchinit_layers_take_the_plan_of_a_compound_system(pa_db):
    """The same unit, spot height and section published from a chunk in RDN2008 / UTM 33N with and without heights
    above EGM2008: the same geometries in the layers of PyArchInit (Monte Mario, SRID 3004), reprojected; read back in
    the chunk's system, the numbers of the chunk."""
    from shapely.geometry import LineString

    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon
    from pyarchinit_photogrammetry.pyarchinit import PyArchInit

    pa = PyArchInit.connect()
    assert pa.srid() == 3004
    x, y = _rome_utm()
    square = box(x, y, x + 2.0, y + 2.0)
    for us, system in (("1", "EPSG:7792"), ("2", COMPOUND)):
        pa.publish_units([UnitPolygon(square, us=us, area="1", tipo="US")], "Saggio", crs=system)
        pa.publish_quote([{"x": x, "y": y, "quota_q": 21.5, "us_q": us, "area_q": "1"}], "Saggio", crs=system)
        section = type("Section", (), {"p1": (x, y, 0.0), "p2": (x + 5.0, y, 0.0), "label": f"S{us}"})()
        pa.publish_section_trace(section, "Saggio", f"S{us}", area="1", crs=system)
    rows = {r["us_s"]: swkt.loads(r["wkt"]) for r in pa.db.geometries_wkt("pyunitastratigrafiche")}
    quote = {r["us_q"]: swkt.loads(r["wkt"]) for r in pa.db.geometries_wkt("pyarchinit_quote")}
    traces = {r["id_sezione"]: swkt.loads(r["wkt"]) for r in pa.db.geometries_wkt("pyarchinit_sezioni")}
    expected = Transformer.from_crs("EPSG:7792", "EPSG:3004", always_xy=True).transform(x, y)
    assert rows["1"].bounds[0] == pytest.approx(expected[0], abs=0.01)  # Monte Mario: about 2,312 km east
    assert rows["2"].equals_exact(rows["1"], 1e-6), "the compound system's unit not reprojected"
    assert quote["2"].equals_exact(quote["1"], 1e-6) and quote["1"].x == pytest.approx(expected[0], abs=0.01)
    assert traces["S2"].equals_exact(traces["S1"], 1e-6)
    assert isinstance(traces["S1"], LineString)
    back = {u.us: u.geometry for u in pa.us_polygons("Saggio", crs=COMPOUND)}
    assert back["2"].bounds[:2] == pytest.approx((x, y), abs=1e-3)  # there and back through Monte Mario
    assert back["1"].equals_exact(back["2"], 1e-6)


def _srs_of_table(path, table) -> tuple[int, str]:
    con = sqlite3.connect(path)
    try:
        srs_id = con.execute("SELECT srs_id FROM gpkg_geometry_columns WHERE table_name = ?", (table,)).fetchone()[0]
        definition = con.execute("SELECT definition FROM gpkg_spatial_ref_sys WHERE srs_id = ?", (srs_id,)).fetchone()
        return srs_id, definition[0] if definition else ""
    finally:
        con.close()


def test_geopackages_of_a_compound_system_take_its_plan(tmp_path):
    """The GeoPackages written from a chunk in EPSG:7792+3855 - a table (zones, measuring points, contours go this
    way), the QGIS live link, the QField package - carry RDN2008 / UTM 33N, not an undefined system."""
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.io import qfield
    from pyarchinit_photogrammetry.io.qgis_link import LiveLink, new_id

    assert gpkg.srs_of(COMPOUND)[0] == 7792 and "RDN2008" in gpkg.srs_of(COMPOUND)[1]
    x, y = _rome_utm()
    path = gpkg.write_table(tmp_path / "zone.gpkg", "zone", "POLYGON", {"zona": "INTEGER"},
                            [(box(x, y, x + 1.0, y + 1.0), {"zona": 1})], COMPOUND)
    srs_id, definition = _srs_of_table(path, "zone")
    assert srs_id == 7792 and "RDN2008" in definition

    link = LiveLink(tmp_path / "qgis", COMPOUND)
    link.write({"Unita' stratigrafiche": {"kind": "polygon", "features": [
        {"etichetta": "US 1", "us_s": "1", "unita_tipo_s": "US", "ppg_id": new_id(),
         "_geom": ("Polygon", [[x, y], [x + 1.0, y], [x + 1.0, y + 1.0], [x, y + 1.0]])}]}})
    link.close()
    assert _srs_of_table(link.path, "unita")[0] == 7792

    doc = ppg.Document()
    chunk = doc.add_chunk("Saggio")
    doc.save(tmp_path / "scavo.ppg")
    chunk.crs = COMPOUND
    chunk.meta["drawings"] = {"units": [{"wkt": box(x, y, x + 1.0, y + 1.0).wkt, "us": "1", "area": "1",
                                         "tipo": "US", "ppg_id": new_id()}],
                              "quote": [{"x": x, "y": y, "quota_q": 21.5, "ppg_id": new_id()}]}
    out = qfield.export_package(chunk, tmp_path / "qfield", resolution=None, project=False)
    assert out["units"] == 1 and out["quote"] == 1
    for table in ("unita", "quote"):
        assert _srs_of_table(tmp_path / "qfield" / qfield.GPKG, table)[0] == 7792


def _captured_project(monkeypatch, module, tmp_path):
    """Run the QGIS project builder of ``module`` with a QGIS of the test: what it would give QGIS's Python (the
    configuration), and a report that says the project is written."""
    from pyarchinit_photogrammetry.io.qgis_link import QgisInstall

    seen = {}

    def run(args, **kwargs):
        cfg = json.loads(open(args[2], encoding="utf-8").read())
        seen.update(cfg)
        open(cfg["project"], "w", encoding="utf-8").write("<qgis/>")
        report = {"written": True, "vectors": [], "rasters": []}
        return subprocess.CompletedProcess(args, 0, "PPG_QGIS_REPORT " + json.dumps(report) + "\n", "")

    monkeypatch.setattr(module.subprocess, "run", run)
    return seen, QgisInstall(app="qgis", python="python-of-qgis")


def test_qgis_projects_of_a_compound_system_take_its_plan(tmp_path, monkeypatch):
    """QGIS 3 has no ``EPSG:7792+3855``: the projects of the live link and of QField get the system of the plan."""
    from pyarchinit_photogrammetry.io import qfield, qgis_link

    seen, qgis = _captured_project(monkeypatch, qgis_link, tmp_path)
    qgis_link.build_project(tmp_path / "live.qgs", COMPOUND, {}, [], qgis=qgis)
    assert seen["crs"] == "EPSG:7792"
    seen.clear()
    qgis_link.build_project(tmp_path / "local.qgs", "LOCAL", {}, [], qgis=qgis)
    assert seen["crs"] == ""
    seen, qgis = _captured_project(monkeypatch, qfield, tmp_path)
    qfield._write_project(tmp_path, "rilievo", COMPOUND, [], "Rilievo", qgis)
    assert seen["crs"] == "EPSG:7792"


def test_ifc_of_a_compound_system_names_its_plan_and_its_heights(tmp_path):
    """IFC: the projected system of the plan (``EPSG:7792``) and the vertical datum of the heights (``EPSG:3855``)."""
    ifcopenshell = pytest.importorskip("ifcopenshell")
    import trimesh

    from pyarchinit_photogrammetry.bim.ifc import IfcElement, export_ifc

    x, y = _rome_utm()
    mesh = trimesh.creation.box(extents=(1.0, 1.0, 1.0))
    mesh.apply_translation((x, y, 21.0))
    path = export_ifc(tmp_path / "us.ifc", [IfcElement(mesh, "US 1")], crs=COMPOUND)
    (projected,) = ifcopenshell.open(str(path)).by_type("IfcProjectedCRS")
    assert projected.Name == "EPSG:7792" and projected.VerticalDatum == "EPSG:3855"
    path = export_ifc(tmp_path / "plain.ifc", [IfcElement(mesh, "US 1")], crs="EPSG:7792")
    (projected,) = ifcopenshell.open(str(path)).by_type("IfcProjectedCRS")
    assert projected.Name == "EPSG:7792" and projected.VerticalDatum is None


def test_the_measuring_points_of_a_compound_system(tmp_path):
    """Measuring points written to a GeoPackage carry the plan's system; their heights are an attribute."""
    from pyarchinit_photogrammetry.engine import probe

    x, y = _rome_utm()
    path = probe.write_points([{"label": "P1", "x": x, "y": y, "z": 21.5}], tmp_path / "punti.gpkg", COMPOUND)
    assert _srs_of_table(path, "punti_misura")[0] == 7792
