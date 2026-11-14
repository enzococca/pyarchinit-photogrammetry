"""The styles made by hand kept by *Apri in QGIS*, with QGIS's own Python (skipped without QGIS): the project updated
in place (a style, a user's layer and group kept, a new layer added, a stale one removed, a raster moved), written
again with the kept .qml, restyled, the default styles of the live GeoPackage, and a project of 1.7.8 converted.
Headless, with a profile of QGIS in the temporary folder of the test: never the application, never the user's
profile or projects."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import numpy as np
import pytest

from pyarchinit_photogrammetry.io import qgis_link, qgis_styles
from pyarchinit_photogrammetry.io.qgis_link import LiveLink, build_project, layer_subset

CRS = "EPSG:3004"
X0, Y0 = 2480000.0, 4500000.0
UNITS = "Unita' stratigrafiche"


def _qgis():
    q = qgis_link.find_qgis()
    if q is None or not q.python or not Path(q.python).exists():
        pytest.skip("QGIS not installed")
    return q


_HEAD = r"""
import json, sys
from qgis.core import *
from qgis.PyQt.QtGui import QColor
app = QgsApplication([], False, sys.argv[1])  # a profile of the test, not the user's
app.initQgis()
proj = QgsProject.instance()
"""

_INSPECT = _HEAD + r"""
proj.read(sys.argv[2])
out = {}
for node in proj.layerTreeRoot().findLayers():
    lyr = node.layer()
    r = lyr.renderer() if hasattr(lyr, "renderer") else None
    info = {"source": lyr.source(), "group": node.parent().name() if node.parent() is not None else "",
            "renderer": r.type() if r is not None else None}
    if isinstance(lyr, QgsVectorLayer):
        info["subset"] = lyr.subsetString()
        sym = r.symbol() if r is not None and hasattr(r, "symbol") else None
        if sym is not None:
            info["colour"] = sym.color().name()
            sl = sym.symbolLayer(0)
            info["stroke"] = sl.strokeColor().name() if hasattr(sl, "strokeColor") else None
            info["stroke_width"] = sl.strokeWidth() if hasattr(sl, "strokeWidth") else None
        if r is not None and r.type() == "RuleRenderer":
            info["rules"] = [c.label() for c in r.rootRule().children()]
        info["labels"] = lyr.labelsEnabled()
        lab = lyr.labeling()
        if lab is not None and hasattr(lab, "settings"):
            info["label_field"] = lab.settings().fieldName
            info["label_size"] = lab.settings().format().size()
    out[lyr.name()] = info
print("STATE " + json.dumps(out), flush=True)
app.exitQgis()
"""

_HAND = _HEAD + r"""
proj.read(sys.argv[2])
units = proj.mapLayersByName(sys.argv[3])[0]
sym = units.renderer().symbol().clone()
sym.setColor(QColor("#123456"))
sym.symbolLayer(0).setStrokeColor(QColor("#654321"))
sym.symbolLayer(0).setStrokeWidth(1.7)
root = QgsRuleBasedRenderer.Rule(None)
rule = QgsRuleBasedRenderer.Rule(sym, 0, 0, "\"etichetta\" LIKE 'US%'", "US a mano")
root.appendChild(rule)
units.setRenderer(QgsRuleBasedRenderer(root))
pal = QgsPalLayerSettings()
pal.fieldName = "etichetta"
fmt = QgsTextFormat(); fmt.setSize(13); pal.setFormat(fmt)
units.setLabeling(QgsVectorLayerSimpleLabeling(pal))
units.setLabelsEnabled(True)
mine = QgsVectorLayer(sys.argv[4], "Catasto", "ogr")
proj.addMapLayer(mine, False)
proj.layerTreeRoot().addGroup("Miei").addLayer(mine)
print("STATE " + json.dumps({"valid": mine.isValid(), "written": proj.write()}), flush=True)
app.exitQgis()
"""

_FRESH = _HEAD + r"""
lyr = QgsVectorLayer(sys.argv[2] + "|layername=" + sys.argv[3], "fresh", "ogr")
out = {"valid": lyr.isValid(), "colour": lyr.renderer().symbol().color().name()}
if len(sys.argv) > 4:  # the user saves a default style of his own in the GeoPackage (Salva come predefinito)
    lyr.renderer().symbol().setColor(QColor(sys.argv[4]))
    lyr.saveStyleToDatabase("mio", "", True, "")
print("STATE " + json.dumps(out), flush=True)
app.exitQgis()
"""

_OLD_PROJECT = _HEAD + r"""
crs = QgsCoordinateReferenceSystem("EPSG:3004")
proj.setCrs(crs)
g = proj.layerTreeRoot().addGroup("Disegni (aggiornati dal vivo)")
whole = QgsVectorLayer(sys.argv[3] + "|layername=unita", "Unita' stratigrafiche (ppg)", "ogr")
proj.addMapLayer(whole, False); g.addLayer(whole)
mine = QgsVectorLayer(sys.argv[4], "Catasto", "ogr")
proj.addMapLayer(mine, False)
proj.layerTreeRoot().addGroup("Miei").addLayer(mine)
print("STATE " + json.dumps({"written": proj.write(sys.argv[2])}), flush=True)
app.exitQgis()
"""


def _run(q, script: str, tmp: Path, *args) -> dict:
    path = tmp / "script.py"
    path.write_text(script, encoding="utf-8")
    profile = tmp / "qgis_profile"
    profile.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "PROJ_", "GDAL_", "QT_"))}
    env.update(q.env)
    env["QT_QPA_PLATFORM"] = "offscreen"
    r = subprocess.run([q.python, str(path), str(profile), *map(str, args)], capture_output=True, text=True, env=env,
                       timeout=300, close_fds=False)
    line = next((ln for ln in r.stdout.splitlines() if ln.startswith("STATE ")), None)
    assert line is not None, (r.stdout + r.stderr)[-3000:]
    return json.loads(line.split(" ", 1)[1])


def _features(names):
    out = {}
    for k, name in enumerate(names):
        kind = "polygon" if name == UNITS else "line"
        if kind == "polygon":
            feats = [{"_geom": ("Polygon", [(X0 + i, Y0), (X0 + i + .5, Y0), (X0 + i + .5, Y0 + .5), (X0 + i, Y0)]),
                      "etichetta": f"US {i}"} for i in range(3)]
        else:
            feats = [{"_geom": ("LineString", [(X0, Y0 + k), (X0 + 2, Y0 + k + 1)]), "etichetta": name}]
        out[name] = {"kind": kind, "features": feats}
    return out


def _raster(path: Path, value: float) -> Path:
    from pyarchinit_photogrammetry.engine.dem import write_geotiff

    return write_geotiff(path, np.full((20, 20), value, np.float32), (X0, Y0 + 2), 0.1, CRS, nodata=-9999.0)


def _user_layer(tmp: Path) -> Path:
    path = tmp / "catasto.geojson"
    path.write_text(json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"n": 1},
                    "geometry": {"type": "Point", "coordinates": [X0, Y0]}}]}), encoding="utf-8")
    return path


def test_the_project_is_updated_in_place_and_written_again_with_the_styles_made_by_hand(tmp_path):
    q = _qgis()
    link = LiveLink(tmp_path / "qgis", CRS)
    files = link.write(_features([UNITS, "Muri"]))
    project = tmp_path / "qgis" / "scavo_ppg.qgs"
    dem = _raster(tmp_path / "dem.tif", 100.0)
    rep = build_project(project, CRS, files, [{"name": "DEM", "path": str(dem)}], qgis=q)
    assert rep["written"] and set(rep["added"]) == {UNITS, "Muri", "DEM"}
    state = _run(q, _INSPECT, tmp_path, project)
    assert state[UNITS]["colour"].lower() == "#e6194b" and state[UNITS]["labels"]
    # the user styles the units by hand (rules, stroke, labels), adds a layer of his own in a group, saves
    hand = _run(q, _HAND, tmp_path, project, UNITS, _user_layer(tmp_path))
    assert hand == {"valid": True, "written": True}
    # nothing new: the project is opened as it is
    rep = build_project(project, CRS, files, [{"name": "DEM", "path": str(dem)}], qgis=q)
    assert rep["kept"]
    # a new layer, one gone, the DEM another file: updated in place
    files = link.write(_features([UNITS, "Pietre"]))
    dem2 = _raster(tmp_path / "dem2.tif", 101.0)
    rep = build_project(project, CRS, files, [{"name": "DEM", "path": str(dem2)}], qgis=q)
    assert rep["written"] and rep["added"] == ["Pietre"] and rep["removed"] == ["Muri"]
    state = _run(q, _INSPECT, tmp_path, project)
    units = state[UNITS]
    assert units["renderer"] == "RuleRenderer" and units["rules"] == ["US a mano"]
    assert units["labels"] and units["label_size"] == 13 and units["label_field"] == "etichetta"
    assert "Pietre" in state and "Muri" not in state
    assert state["Pietre"]["subset"] == layer_subset("Pietre")
    assert Path(state["DEM"]["source"]).resolve() == dem2.resolve()
    assert state["Catasto"]["group"] == "Miei" and Path(state["Catasto"]["source"].split("|")[0]).name == "catasto.geojson"
    assert qgis_styles.kept_styles(project) == [UNITS]  # the hand-made style saved as .qml meanwhile
    # the project lost: written again, the units take their .qml - read back by QGIS, not just loaded
    project.unlink()
    rep = build_project(project, CRS, files, [{"name": "DEM", "path": str(dem2)}], qgis=q)
    state = _run(q, _INSPECT, tmp_path, project)
    units = state[UNITS]
    assert units["renderer"] == "RuleRenderer" and units["rules"] == ["US a mano"]
    assert units["labels"] and units["label_size"] == 13
    assert state["Pietre"]["colour"].lower() == "#00a0ff"  # the others the program's
    # Ripristina gli stili predefiniti: the program's style again
    rep = build_project(project, CRS, files, [{"name": "DEM", "path": str(dem2)}], qgis=q, restyle=True)
    assert UNITS in rep["restyled"]
    state = _run(q, _INSPECT, tmp_path, project)
    assert state[UNITS]["renderer"] == "singleSymbol" and state[UNITS]["colour"].lower() == "#e6194b"
    assert state[UNITS]["label_size"] == 9 and qgis_styles.kept_styles(project) == []
    link.close()


def test_the_default_styles_of_the_geopackage_are_read_by_qgis(tmp_path):
    q = _qgis()
    link = LiveLink(tmp_path / "qgis", CRS)
    files = link.write(_features([UNITS, "Muri"]))
    fresh = _run(q, _FRESH, tmp_path, link.path, "unita")
    assert fresh["valid"] and fresh["colour"].lower() == "#e6194b"  # our row of layer_styles, taken as the default
    assert _run(q, _FRESH, tmp_path, link.path, "linee")["colour"].lower() == "#00a0ff"
    # the user saves a default style of his own for the units, from QGIS
    _run(q, _FRESH, tmp_path, link.path, "unita", "#00ff00")
    link.write(_features([UNITS, "Muri", "Pietre"]), force=True)
    link.close()
    link = LiveLink(tmp_path / "qgis", CRS)  # another session
    link.write(_features([UNITS]), force=True)
    assert _run(q, _FRESH, tmp_path, link.path, "unita")["colour"].lower() == "#00ff00"  # his, not overwritten
    con = sqlite3.connect(link.path)
    owners = con.execute("SELECT owner, useAsDefault FROM layer_styles WHERE f_table_name = 'unita'").fetchall()
    con.close()
    assert sum(1 for o, d in owners if d) == 1
    # a project of the program does not paint over it
    project = tmp_path / "qgis" / "p.qgs"
    build_project(project, CRS, files, [], qgis=q)
    assert _run(q, _INSPECT, tmp_path, project)[UNITS]["colour"].lower() == "#00ff00"
    link.close()


def test_a_project_of_1_7_8_is_converted_keeping_the_layers_of_the_user(tmp_path):
    q = _qgis()
    link = LiveLink(tmp_path / "qgis", CRS)
    files = link.write(_features([UNITS]))
    project = tmp_path / "qgis" / "old_ppg.qgs"
    assert _run(q, _OLD_PROJECT, tmp_path, project, link.path, _user_layer(tmp_path))["written"]
    rep = build_project(project, CRS, files, [], qgis=q)
    assert rep["written"] and rep["removed"] == ["Unita' stratigrafiche (ppg)"] and rep["added"] == [UNITS]
    state = _run(q, _INSPECT, tmp_path, project)
    assert set(state) == {UNITS, "Catasto"} and state["Catasto"]["group"] == "Miei"
    assert state[UNITS]["subset"] == layer_subset(UNITS)
    link.close()
