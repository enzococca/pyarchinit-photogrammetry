"""The styles made by hand in QGIS kept by *Apri in QGIS* (io.qgis_styles), without a QGIS: the GeoPackage's
layer_styles (the program's default written once, the user's rows kept, rows and fids stable), and the project
- left alone when complete, updated in place otherwise, the hand-made styles kept as .qml - through a stand-in for
QGIS's Python that writes the .qgs as QGIS would, more or less."""
from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from xml.etree import ElementTree

import pytest

from pyarchinit_photogrammetry.io import qgis_link, qgis_styles
from pyarchinit_photogrammetry.io.qgis_link import LIVE_TABLES, LiveLink, QgisInstall, build_project

CRS = "EPSG:3004"
X0, Y0 = 2480000.0, 4500000.0
UNITS = "Unita' stratigrafiche"


def _layers(n=3, extra=False):
    units = [{"_geom": ("Polygon", [(X0 + i, Y0), (X0 + i + 0.5, Y0), (X0 + i + 0.5, Y0 + 0.5), (X0 + i, Y0)]),
              "etichetta": f"US {i}", "ppg_id": f"u{i}"} for i in range(n)]
    out = {UNITS: {"kind": "polygon", "features": units}}
    if extra:
        out["Muri"] = {"kind": "line", "features": [{"_geom": ("LineString", [(X0, Y0), (X0 + 2, Y0 + 1)]),
                                                     "etichetta": "M1"}]}
    return out


def _styles(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT f_table_name, styleName, owner, useAsDefault, styleQML FROM layer_styles "
                           "ORDER BY id").fetchall()
    finally:
        con.close()


def _fids(path, table):
    con = sqlite3.connect(path)
    try:
        return dict(con.execute(f"SELECT ppg_id, fid FROM {table}").fetchall())
    finally:
        con.close()


def test_the_geopackage_keeps_its_styles_and_rows(tmp_path):
    link = LiveLink(tmp_path, CRS)
    link.write(_layers())
    rows = _styles(link.path)
    assert sorted(r[0] for r in rows) == sorted(LIVE_TABLES)  # one default per table, written once
    assert all(r[1] == qgis_styles.STYLE_NAME and r[2] == qgis_styles.OWNER and r[3] == 1 for r in rows)
    unita = next(r[4] for r in rows if r[0] == "unita")
    ElementTree.fromstring(unita.split("\n", 1)[1])  # a QML document
    assert "renderer-v2" in unita and "230,25,75" in unita  # the colour of the units (#e6194b)
    fids = _fids(link.path, "unita")
    # QGIS: the user saves a style of his own as the default of the units, and another one by name
    con = sqlite3.connect(link.path)
    con.execute("UPDATE layer_styles SET useAsDefault = 0 WHERE f_table_name = 'unita'")
    con.execute("INSERT INTO layer_styles (f_table_name, f_geometry_column, styleName, styleQML, useAsDefault, owner) "
                "VALUES ('unita', 'geom', 'mio', '<qgis/>', 1, 'enzo')")
    con.execute("DELETE FROM layer_styles WHERE f_table_name = 'punti'")  # one deleted on purpose
    con.commit()
    con.close()
    link.write(_layers(5, extra=True))  # more units, a new layer: rows written again
    link.close()
    link = LiveLink(tmp_path, CRS)  # another session of the program
    link.write(_layers(6, extra=True), force=True)
    link.close()
    after = _styles(link.path)
    assert len(after) == len(rows)  # nothing written again: the deleted default stays deleted
    assert ("unita", "mio", "enzo", 1, "<qgis/>") in after
    assert not any(r[0] == "punti" for r in after)
    assert qgis_styles.user_default_tables(link.path) == {"unita"}
    new_fids = _fids(link.path, "unita")
    assert all(new_fids[k] == v for k, v in fids.items())  # the rows of before keep their fid
    assert len(new_fids) == 6
    con = sqlite3.connect(link.path)
    assert con.execute("SELECT data_type FROM gpkg_contents WHERE table_name = 'layer_styles'").fetchone() == (
        "attributes",)
    con.close()


# ------------------------------------------------------------------------------------------ a stand-in for QGIS
def _maplayer_xml(key_source, name, provider, kind, colour):
    return (f'<maplayer type="{kind}" autoRefreshEnabled="1" labelsEnabled="0">'
            f'<id>{name}_id</id><datasource>{key_source}</datasource><layername>{name}</layername>'
            f'<provider>{provider}</provider>'
            f'<renderer-v2 type="singleSymbol"><symbols><symbol><layer><Option name="color" value="{colour}"/>'
            f'</layer></symbol></symbols></renderer-v2><blendMode>0</blendMode></maplayer>')


class FakeQgis:
    """What QGIS's Python does with the configuration of build_project: a new project, or the old one with the
    missing layers added and those no longer wanted removed, the styles kept, given or restored."""

    def __init__(self):
        self.runs: list[dict] = []

    def __call__(self, args, **kwargs):
        cfg = json.loads(Path(args[2]).read_text(encoding="utf-8"))
        self.runs.append(cfg)
        project = Path(cfg["project"])
        layers = {}
        if cfg["mode"] == "update":
            root = ElementTree.parse(project).getroot()
            for m in root.iter("maplayer"):
                layers[qgis_styles.layer_key(m) or m.findtext("id")] = ElementTree.tostring(m, encoding="unicode")
            for k in cfg["ours"]:
                wanted = {v["key"] for v in cfg["vectors"]} | {r["key"] for r in cfg["rasters"]}
                if k not in wanted:
                    layers.pop(k, None)
        report = {"vectors": [], "rasters": [], "added": [], "removed": [], "restyled": []}
        for v in cfg["vectors"]:
            source = v["path"] + "|layername=" + v["table"] + (("|subset=" + v["subset"]) if v.get("subset") else "")
            colour = "default:" + v["color"]
            if v.get("qml"):
                colour = ElementTree.fromstring(Path(v["qml"]).read_text().split("\n", 1)[1]).find(
                    ".//Option").get("value")
            if v["key"] not in layers:
                layers[v["key"]] = _maplayer_xml(source, v["name"], "ogr", "vector", colour)
                report["added"].append(v["name"])
            elif cfg["restyle"] and v["key"] in cfg["ours"]:
                layers[v["key"]] = _maplayer_xml(source, v["name"], "ogr", "vector", "default:" + v["color"])
                report["restyled"].append(v["name"])
            report["vectors"].append({"name": v["name"], "valid": True})
        for r in cfg["rasters"]:
            if r["key"] not in layers:
                layers[r["key"]] = _maplayer_xml(r["path"], r["name"], "gdal", "raster", "ramp")
            report["rasters"].append({"name": r["name"], "valid": True})
        body = "".join(layers.values())
        project.write_text(f'<qgis version="3.40.0"><projectlayers>{body}</projectlayers></qgis>', encoding="utf-8")
        report["written"] = True
        return subprocess.CompletedProcess(args, 0, "PPG_QGIS_REPORT " + json.dumps(report) + "\n", "")


def _colour(project, name):
    root = ElementTree.parse(project).getroot()
    m = next(m for m in root.iter("maplayer") if m.findtext("layername") == name)
    return m.find(".//Option").get("value")


def _paint(project, name, colour):
    """The user changes the colour of a layer in QGIS and saves the project."""
    tree = ElementTree.parse(project)
    m = next(m for m in tree.getroot().iter("maplayer") if m.findtext("layername") == name)
    m.find(".//Option").set("value", colour)
    tree.write(project, encoding="utf-8")


def test_apri_in_qgis_keeps_the_styles_made_by_hand(tmp_path, monkeypatch):
    fake = FakeQgis()
    monkeypatch.setattr(qgis_link.subprocess, "run", fake)
    q = QgisInstall(app="qgis", python="python-of-qgis")
    link = LiveLink(tmp_path, CRS)
    files = link.write(_layers())
    dem = tmp_path / "dem.tif"
    dem.write_bytes(b"II*\x00")
    rasters = [{"name": "DEM", "path": str(dem)}]
    project = tmp_path / "scavo_ppg.qgs"
    rep = build_project(project, CRS, files, rasters, qgis=q)
    assert fake.runs[-1]["mode"] == "new" and rep["written"]
    assert _colour(project, UNITS) == "default:#e6194b"
    # 1. nothing new: the project is left alone, QGIS's Python not even started
    before = project.read_bytes()
    rep = build_project(project, CRS, files, rasters, qgis=q)
    assert rep["kept"] and len(fake.runs) == 1 and project.read_bytes() == before
    # 2. the user paints the units and saves; still nothing new: left alone, the style kept as .qml meanwhile
    _paint(project, UNITS, "12,34,56,255")
    rep = build_project(project, CRS, files, rasters, qgis=q)
    assert rep["kept"] and len(fake.runs) == 1 and _colour(project, UNITS) == "12,34,56,255"
    key = qgis_styles.vector_key(link.path, "unita", qgis_link.layer_subset(UNITS))
    qml = qgis_styles.qml_for(project, key)
    assert qml is not None and "12,34,56,255" in qml.read_text() and qml.parent == qgis_styles.style_dir(project)
    assert qgis_styles.kept_styles(project) == [UNITS]
    # 3. a new layer of the program: the project updated in place, the units keep their colour
    files = link.write(_layers(extra=True))
    rep = build_project(project, CRS, files, rasters, qgis=q)
    cfg = fake.runs[-1]
    assert cfg["mode"] == "update" and not cfg["restyle"] and key in cfg["ours"]
    assert rep["added"] == ["Muri"]
    assert _colour(project, UNITS) == "12,34,56,255" and _colour(project, "Muri") == "default:#00a0ff"
    # 4. the project lost (deleted, a .qgz): written again, with the style made by hand
    project.unlink()
    build_project(project, CRS, files, rasters, qgis=q)
    assert fake.runs[-1]["mode"] == "new" and _colour(project, UNITS) == "12,34,56,255"
    # 5. a layer gone from the program: removed from the project, the others stay
    files = link.write(_layers())
    rep = build_project(project, CRS, files, rasters, qgis=q)
    assert fake.runs[-1]["mode"] == "update"
    assert "Muri" not in project.read_text(encoding="utf-8") and _colour(project, UNITS) == "12,34,56,255"
    # 6. Ripristina gli stili predefiniti: the kept style forgotten, the program's colour again
    rep = build_project(project, CRS, files, rasters, qgis=q, restyle=True)
    assert fake.runs[-1]["restyle"] and rep["restyled"] == [UNITS]
    assert _colour(project, UNITS) == "default:#e6194b" and qgis_styles.qml_for(project, key) is None
    assert qgis_styles.kept_styles(project) == []
    link.close()


def test_a_layer_of_the_user_and_a_default_saved_in_qgis_are_respected(tmp_path, monkeypatch):
    fake = FakeQgis()
    monkeypatch.setattr(qgis_link.subprocess, "run", fake)
    q = QgisInstall(app="qgis", python="python-of-qgis")
    link = LiveLink(tmp_path, CRS)
    files = link.write(_layers())
    project = tmp_path / "p.qgs"
    build_project(project, CRS, files, [], qgis=q)
    # the user adds a layer of his own (a basemap) and saves a default style for the units in the GeoPackage
    text = project.read_text(encoding="utf-8").replace(
        "</projectlayers>", _maplayer_xml("type=xyz&amp;url=https://tile", "OSM", "wms", "raster", "x") + "</projectlayers>")
    project.write_text(text, encoding="utf-8")
    con = sqlite3.connect(link.path)
    con.execute("UPDATE layer_styles SET useAsDefault = 0 WHERE f_table_name = 'unita'")
    con.execute("INSERT INTO layer_styles (f_table_name, f_geometry_column, styleName, styleQML, useAsDefault, owner) "
                "VALUES ('unita', 'geom', 'mio', '<qgis/>', 1, 'enzo')")
    con.commit()
    con.close()
    files = link.write(_layers(extra=True))
    build_project(project, CRS, files, [], qgis=q)
    cfg = fake.runs[-1]
    assert cfg["mode"] == "update"
    assert "OSM" in project.read_text(encoding="utf-8")  # the user's layer is still there
    assert all(v.get("db_default") for v in cfg["vectors"] if v["table"] == "unita")
    assert not any(v.get("db_default") for v in cfg["vectors"] if v["table"] != "unita")
    link.close()


def test_the_script_of_qgis_updates_in_place():
    """What the script given to QGIS's Python does (it cannot run here): read the project in update mode, never
    remove a layer that is not the program's, give the kept .qml first."""
    script = qgis_link._BUILD_SCRIPT
    compile(script, "build.py", "exec")
    assert 'proj.read(cfg["project"])' in script and "loadNamedStyle" in script
    assert "for k in mine:" in script and "removeMapLayer" in script
    assert 'v.get("db_default")' in script


@pytest.mark.parametrize("subset", ["", "\"layer\" = 'Muri d''ambito'"])
def test_keys_of_the_layers(subset):
    source = "/a/b/disegni_ppg.gpkg|layername=linee" + (f"|subset={subset}" if subset else "")
    m = ElementTree.fromstring(_maplayer_xml("x", "L", "ogr", "vector", "c"))
    m.find("datasource").text = source
    assert qgis_styles.layer_key(m) == qgis_styles.vector_key("C:\\other\\disegni_ppg.gpkg", "linee", subset)
    r = ElementTree.fromstring(_maplayer_xml("/x/dem.tif", "DEM", "gdal", "raster", "c"))
    assert qgis_styles.layer_key(r) == qgis_styles.raster_key("DEM")


def test_interface_restores_the_default_styles(trench, monkeypatch):
    """QGIS > Ripristina gli stili predefiniti: asked first, then *Apri in QGIS* with the styles of the program."""
    from PySide6.QtWidgets import QMessageBox

    w, app, mw = trench
    built = []
    monkeypatch.setattr(qgis_link, "find_qgis", lambda: QgisInstall(app="qgis", python="python-of-qgis"))
    monkeypatch.setattr(qgis_link, "build_project", lambda project, crs, files, rasters, **k: built.append(k) or {
        "vectors": [], "rasters": [], "written": True, "restyled": ["DEM"]})
    monkeypatch.setattr(qgis_link, "open_in_qgis", lambda *a, **k: None)
    answers = [QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes]
    monkeypatch.setattr(mw, "message", lambda *a, **k: answers.pop(0))
    w.restore_qgis_styles()
    assert built == []  # No: nothing done
    w.restore_qgis_styles()
    w.runner.wait()
    app.processEvents()
    assert built and built[-1]["restyle"] is True
    w.open_in_qgis()
    w.runner.wait()
    assert built[-1]["restyle"] is False


def test_a_project_of_1_7_8_loses_its_layers_of_whole_tables_only(tmp_path, monkeypatch):
    fake = FakeQgis()
    monkeypatch.setattr(qgis_link.subprocess, "run", fake)
    link = LiveLink(tmp_path, CRS)
    files = link.write(_layers())
    project = tmp_path / "old.qgs"
    whole = _maplayer_xml(f"{link.path}|layername=unita", "Unita' stratigrafiche (ppg)", "ogr", "vector", "mine")
    own = _maplayer_xml("/data/catasto.gpkg|layername=particelle", "Catasto", "ogr", "vector", "x")
    project.write_text(f"<qgis><projectlayers>{whole}{own}</projectlayers></qgis>", encoding="utf-8")
    build_project(project, CRS, files, [], qgis=QgisInstall(app="qgis", python="python-of-qgis"))
    cfg = fake.runs[-1]
    assert cfg["mode"] == "update" and cfg["ours"] == [qgis_styles.vector_key(link.path, "unita", "")]
    text = project.read_text(encoding="utf-8")
    assert "Catasto" in text and "(ppg)" not in text and UNITS in text
    link.close()
