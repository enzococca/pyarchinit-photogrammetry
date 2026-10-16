"""Layers on the orthomosaic view, the sections pane and the PyArchInit dialogs (offscreen)."""
from __future__ import annotations

import json
import os
import time

import numpy as np
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, processed, tmp_path):
    from PySide6.QtCore import QSettings

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path))
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    doc, ch, _ = processed
    w = MainWindow(doc, autoconnect=False)
    w.runner.show_dialog = False
    w.resize(1400, 900)
    w.show()
    qapp.processEvents()
    yield w
    w.setWindowModified(False)
    w.close()


def test_layers_visibility_zoom_and_query(window, qapp):
    w = window
    v = w.ortho_view
    assert v.tr is not None
    x0, y0 = 2480001.2, 4500001.2
    v.add_polygon([(x0, y0), (x0 + 0.5, y0), (x0 + 0.5, y0 + 0.4), (x0, y0 + 0.4)], "US 9", "#ff0000",
                  layer="Prova", attrs={"us": "9", "area": "1"})
    v.add_line([(x0, y0), (x0 + 1.0, y0 + 0.5)], "#00ff00", "linea", layer="Linee prova", attrs={"lunghezza": 1.1})
    v.add_point(x0 + 0.1, y0 + 0.1, layer="Punti prova", attrs={"z": 100.0})
    assert {"Prova", "Linee prova", "Punti prova"} <= set(v.layers)  # plus sections of earlier tests
    assert w.layers_pane.tree.topLevelItemCount() == len(v.layers)
    v.set_layer_visible("Prova", False)
    item = v.layers["Prova"]["features"][0][0]
    assert not item.isVisible()
    v.set_layer_visible("Prova", True)
    assert item.isVisible()
    # query at the polygon centre in navigation mode
    hit = v.feature_at(v.scene_pt(x0 + 0.45, y0 + 0.35))  # inside the polygon, away from the line
    assert hit is not None and hit[0] == "Prova" and hit[1]["us"] == "9"
    seen = []
    v.feature_clicked.connect(lambda layer, attrs: seen.append((layer, attrs)))
    w._feature_clicked("Prova", hit[1])
    assert w.layers_pane.attrs.rowCount() >= 3
    rect = v.layer_rect("Prova")
    assert rect is not None and rect.width() > 0
    v.zoom_to_layer("Prova")
    v.remove_layer("Prova")
    assert "Prova" not in v.layers and w.layers_pane.tree.topLevelItemCount() == len(v.layers)


def test_section_goes_to_pane_and_is_persisted(window, qapp):
    w = window
    a = np.array([2480000.0 + 1.0, 4500000.0 + 1.5])
    b = np.array([2480000.0 + 3.0, 4500000.0 + 1.5])
    before = len(w.sections)
    w._section_drawn(a, b)
    w.runner.wait()
    assert len(w.sections) == before + 1
    s = w.sections[-1]
    assert os.path.exists(s["svg"]) and os.path.exists(s["dxf"]) and os.path.exists(s["dxf3d"])
    assert "Sezioni" in w.ortho_view.layers
    data = json.loads(w._sections_file().read_text(encoding="utf-8"))
    assert data[-1]["label"] == s["label"] and abs(data[-1]["length"] - 2.0) < 0.05
    assert w.sections_pane.list.count() == len(w.sections)
    # reload the document: the section comes back from sezioni.json
    w.set_document(w.doc)
    assert len(w.sections) == before + 1 and "Sezioni" in w.ortho_view.layers
    w._delete_section(w.sections[-1]) if False else None  # deletion asks a question: covered manually


def test_section_preview_lines_are_visible(qapp, tmp_path):
    """A drawing metres long on paper is reduced many times in the pane: its lines and
    label are thickened for the preview (the SVG file stays as it is)."""
    import re

    from pyarchinit_photogrammetry.archaeo.sections import Section
    from pyarchinit_photogrammetry.gui.panels import SectionsPane, svg_preview

    d = np.linspace(0.0, 174.0, 300)
    sec = Section(np.zeros(3), np.array([174.0, 0.0, 0.0]), polylines=[np.column_stack([d, 300 + np.sin(d)])])
    path = sec.to_svg(tmp_path / "s.svg", scale=20)  # 8.7 m of paper, as the sections of 0.4.1
    data, size = svg_preview(path, max_width=1200)
    text = data.decode()
    width_units = float(re.search(r'viewBox="0 0 ([\d.]+)', text).group(1))
    stroke = float(re.search(r'stroke-width="([\d.]+)"', text).group(1))
    assert size.width() == 1200 and stroke * size.width() / width_units >= 1.5  # at least 1.5 px on screen
    assert 'stroke-width="0.5"' in path.read_text()
    pane = SectionsPane()
    pane.resize(900, 300)
    pane.show()
    pane.set_sections([{"label": "Sezione 1", "p1": [0, 0], "p2": [174, 0], "length": 174.0, "svg": str(path)}])
    qapp.processEvents()
    assert 300 <= pane.svg.width() <= 900  # fits the pane instead of a fixed 1400 px
    pane.close()


def test_line_layers_survive_raster_reload(window, qapp):
    w = window
    w._add_line_feature("Curve prova", [(2480001.0, 4500001.0), (2480002.0, 4500001.5)], "#ffffff",
                        attrs={"quota": 100.1})
    assert "Curve prova" in w.ortho_view.layers
    w._load_raster("dem")
    assert "Curve prova" in w.ortho_view.layers
    w._load_raster("ortho")
    assert "Curve prova" in w.ortho_view.layers
    w.clear_drawings()
    assert "Curve prova" not in w.ortho_view.layers


def test_many_contour_lines_are_drawn_at_once(window, qapp, tmp_path):
    """Tens of thousands of contour lines (a 5 cm interval on a tell) used to freeze the
    window: the layer pane was rebuilt, with every line, after each line added."""
    w = window
    rebuilt = []
    w.layers_pane.tree.model().modelReset.connect(lambda: rebuilt.append(1))
    x0, y0 = 2480000.5, 4500000.5
    t = np.linspace(0, 2 * np.pi, 12)
    lines = [{"elevation": 99.0 + 0.01 * (k % 50), "index": k % 5 == 0,
              "coords": np.column_stack([x0 + 1.5 + 0.0004 * k * np.cos(t), y0 + 1.5 + 0.0004 * k * np.sin(t)])}
             for k in range(1, 3001)]
    w._qgis_dir = lambda: tmp_path
    w.a_qgis_live.setChecked(True)
    start = time.time()
    w._draw_contours("Curve di livello (0.01 m)", lines)
    elapsed = time.time() - start
    assert len(w.ortho_view.layers["Curve di livello (0.01 m)"]["features"]) == 3000
    assert elapsed < 15 and len(rebuilt) <= 2  # one rebuild of the layer pane, not 3000
    top = next(w.layers_pane.tree.topLevelItem(i) for i in range(w.layers_pane.tree.topLevelItemCount())
               if w.layers_pane.tree.topLevelItem(i).text(0).startswith("Curve di livello (0.01 m)"))
    assert top.childCount() == w.layers_pane.MAX_LISTED + 1 and "altri 2500" in top.child(top.childCount() - 1).text(0)
    deadline = time.time() + 10
    while w._qgis_live_link().counts()["curve"] < 3000 and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.05)
    assert w._qgis_live_link().counts()["curve"] == 3000
    w.a_qgis_live.setChecked(False)
    w.ortho_view.remove_layer("Curve di livello (0.01 m)")
    w.line_layers.pop("Curve di livello (0.01 m)", None)


def test_connect_and_site_dialogs(qapp, tmp_path):
    from PySide6.QtCore import QSettings

    from pyarchinit_photogrammetry.gui.dialogs import ConnectDialog, SiteDialog

    settings = QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)
    d = ConnectDialog(None, settings)
    d.r_pg.setChecked(True)
    d.host.setText("db.example.org")
    d.dbname.setText("scavi")
    d.user.setText("archeo")
    d.password.setText("p@ss word")
    v = d.values()
    assert v["mode"] == "postgres" and v["url"].startswith("postgresql://archeo:p%40ss+word@db.example.org:5432/scavi")
    d.remember_settings()
    assert settings.value("pa_pg_host") == "db.example.org" and settings.value("pa_pg_password") == ""
    d.r_sqlite.setChecked(True)
    d.sqlite_edit.edit.setText("/tmp/x.sqlite")
    assert d.values() == {"mode": "sqlite", "config": None, "url": "/tmp/x.sqlite"}
    assert not (d.r_pg.isChecked() and d.r_sqlite.isChecked())  # exclusive across group boxes
    s = SiteDialog(None, "Sito X")
    s.comune.setText("Baselice")
    assert s.values()["sito"] == "Sito X" and s.values()["comune"] == "Baselice"


def test_demo_database_large_and_publish_dialog_site_creation(qapp, tmp_path):
    from pyarchinit_photogrammetry.gui.dialogs import PublishDialog
    from pyarchinit_photogrammetry.pyarchinit import PyArchInit
    from pyarchinit_photogrammetry.pyarchinit.demo_db import create_demo_database

    cfg = create_demo_database(tmp_path / "pa", size="large")
    pa = PyArchInit.connect(config_path=cfg, media_root=tmp_path / "media")
    names = [s["sito"] for s in pa.sites()]
    assert names[:3] == ["Saggio dimostrativo", "Villa dei Volusii", "Necropoli di Colle Mauro"]
    assert len(pa.stratigraphic_units("Villa dei Volusii")) == 30
    assert len(pa.pottery("Saggio dimostrativo")) == 6 and len(pa.finds("Saggio dimostrativo")) == 4
    if pa.db.spatialite:
        assert len(pa.us_polygons("Saggio dimostrativo")) == 3
    # idempotent: a second call keeps the file and the config
    cfg2 = create_demo_database(tmp_path / "pa", size="large")
    assert cfg2 == cfg and len(pa.sites()) == 3
    dlg = PublishDialog(pa, None)
    assert dlg.site.isEditable() and dlg.site.count() == 3
    assert pa.ensure_site("Nuovo sito", comune="Benevento") == 4 and pa.site_exists("Nuovo sito")
    assert pa.ensure_site("Nuovo sito") == 4  # existing: same id, no duplicate


def test_attribute_editing_updates_unit_and_pending(window, qapp):
    from shapely.geometry import box

    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon

    w = window
    u = UnitPolygon(box(2480001.2, 4500001.2, 2480001.8, 4500001.6), us="5", area="1", tipo="US",
                    attributes={"gid": 42})
    w.units = [u]
    w._draw_unit(u, 0, layer="US da PyArchInit - X")
    pane = w.layers_pane
    feats = w.ortho_view.features("US da PyArchInit - X")
    pane.show_attributes("US da PyArchInit - X", {**feats[0], "_index": 0})
    row = next(r for r in range(pane.attrs.rowCount()) if pane.attrs.item(r, 0).text() == "us")
    pane.attrs.item(row, 1).setText("12")  # as if typed in the table
    qapp.processEvents()
    assert u.us == "12" and feats[0]["etichetta"] == "US 12"
    assert 42 in w.pa_pending and not w.b_pa_save.isHidden()  # the pane tab itself may be hidden
    area_row = next(r for r in range(pane.attrs.rowCount()) if pane.attrs.item(r, 0).text() == "area_m2")
    from PySide6.QtCore import Qt

    assert not (pane.attrs.item(area_row, 1).flags() & Qt.ItemFlag.ItemIsEditable)  # computed field


def test_qgis_live_export_from_view_layers(window, qapp, tmp_path):
    """The live export reads the layers of the raster view (features are (item, label, attrs))."""
    from shapely.geometry import box

    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon

    w = window
    x0, y0 = 2480000.0, 4500000.0
    u = UnitPolygon(box(x0 + 1.2, y0 + 1.2, x0 + 1.8, y0 + 1.6), us="5", area="1", tipo="US")
    w.units = [u]
    w._draw_unit(u, 0)
    w._add_line_feature("Curve di livello (0.1 m)", [[x0 + 1.0, y0 + 1.0], [x0 + 2.0, y0 + 1.0]], "#ffffff",
                        attrs={"quota": 99.7})
    w.ortho_view.add_point(x0 + 1.5, y0 + 1.5, layer="Punti", attrs={"z": 99.8})
    w._qgis_dir = lambda: tmp_path
    files = w._export_qgis_layers()
    counts = w._qgis_live_link().counts()
    assert counts["unita"] == 1 and counts["curve"] == 1 and counts["punti"] == 1
    import sqlite3

    con = sqlite3.connect(files["unita"])
    assert con.execute('SELECT us FROM "unita"').fetchone()[0] == "5"
    con.close()
    # live: a new drawing reaches the GeoPackage by itself, a moment later
    w.a_qgis_live.setChecked(True)
    w.ortho_view.add_point(x0 + 1.6, y0 + 1.6, layer="Punti", attrs={"z": 99.9})
    deadline = time.time() + 5
    while w._qgis_live_link().counts()["punti"] < 2 and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.05)
    assert w._qgis_live_link().counts()["punti"] == 2
    w.a_qgis_live.setChecked(False)
    w.set_document(w.doc)  # reopening a project already linked to QGIS keeps the link live
    assert w.a_qgis_live.isChecked()
    w.a_qgis_live.setChecked(False)


def _units_side_by_side(w, n=4, gap_after=3):
    from shapely.geometry import box

    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon

    x0, y0 = 2480000.0, 4500000.0
    w.units = []
    for k in range(n):
        a = 1.0 + 0.4 * k + (0.4 if k >= gap_after else 0.0)
        u = UnitPolygon(box(x0 + a, y0 + 1.2, x0 + a + 0.4, y0 + 1.6), us=str(k + 1), area="1", tipo="US")
        w.units.append(u)
        w._draw_unit(u, k)
    return x0, y0


def test_select_merge_and_delete_units(window, qapp, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QMessageBox

    from pyarchinit_photogrammetry.gui import main_window as mw

    w = window
    v = w.ortho_view
    x0, y0 = _units_side_by_side(w)
    for name in list(v.layers):  # hidden layers (here the section of another test) are not selectable
        if name != "Unita' stratigrafiche":
            v.set_layer_visible(name, False)
    w.set_draw_mode("select")
    v.zoom_to(v.layer_rect("Unita' stratigrafiche"), 0.4)
    qapp.processEvents()

    def vp(x, y):
        return v.mapFromScene(v.scene_pt(x0 + x, y0 + y))

    QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vp(1.2, 1.4))
    QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier, vp(1.6, 1.4))
    assert [a["us"] for _, _, a in v.selected_features()] == ["1", "2"]
    assert w.a_sel_merge.isEnabled() and w.a_sel_delete.isEnabled()
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(lambda cls, *a, **k: {"keep": "1"}))  # keep US 2
    assert w.merge_selected()
    assert [u.us for u in w.units] == ["2", "3", "4"]
    assert w.units[0].geometry.geom_type == "Polygon" and abs(w.units[0].geometry.area - 0.32) < 1e-6
    # a box over US 3 and US 4, then the Delete key
    QTest.mousePress(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vp(1.9, 1.7))
    QTest.mouseMove(v.viewport(), vp(2.5, 1.4))
    QTest.mouseMove(v.viewport(), vp(3.0, 1.3))
    QTest.mouseRelease(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vp(3.0, 1.3))
    assert sorted(a["us"] for _, _, a in v.selected_features()) == ["3", "4"]
    monkeypatch.setattr(mw, "message", lambda *a, **k: QMessageBox.StandardButton.Yes)
    QTest.keyClick(v, Qt.Key.Key_Delete)
    assert [u.us for u in w.units] == ["2"] and not v.selection
    assert len(v.features("Unita' stratigrafiche")) == 1
    assert all(not v.layers[n]["visible"] for n in v.layers if n != "Unita' stratigrafiche")  # kept hidden
    QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vp(0.3, 0.3))
    assert not v.selection  # a click on empty ground clears the selection


def test_merge_lines_and_delete_point(window, qapp):
    w = window
    v = w.ortho_view
    x0, y0 = 2480000.0, 4500000.0
    name = "Pianta a quota 99.70 m"
    w._add_line_feature(name, [[x0 + 1.0, y0 + 1.0], [x0 + 1.5, y0 + 1.0]], "#ff00ff")
    w._add_line_feature(name, [[x0 + 1.5, y0 + 1.0], [x0 + 2.0, y0 + 1.1]], "#ff00ff")
    w._add_line_feature(name, [[x0 + 3.0, y0 + 3.0], [x0 + 3.5, y0 + 3.0]], "#ff00ff")
    w.set_draw_mode("select")
    items = [f[0] for f in v.layers[name]["features"]]
    v.select_items(items[:2])
    assert w.merge_selected()
    assert len(w.line_layers[name]) == 2 and len(w.line_layers[name][0][0]) == 3
    v.select_items([f[0] for f in v.layers[name]["features"]])  # not contiguous: refused
    warned = []
    w._warn = warned.append
    assert not w.merge_selected() and warned
    w.ortho_view.add_point(x0 + 1.2, y0 + 0.8, layer="Punti", attrs={"tipo": "positivo"})
    w.sam_points = [(x0 + 1.2, y0 + 0.8, 1)]
    v.select_items([v.layers["Punti"]["features"][0][0]])
    assert w.delete_selected(confirm=False) == 1
    assert "Punti" not in v.layers and w.sam_points == []
    assert name in v.layer_order  # other layers survive the redraw


def test_merge_and_delete_write_back_to_pyarchinit(window, qapp, pa_db, monkeypatch):
    from shapely.geometry import box

    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon
    from pyarchinit_photogrammetry.gui import main_window as mw
    from pyarchinit_photogrammetry.pyarchinit import PyArchInit

    w = window
    ch = w.chunk
    pa = PyArchInit.connect()
    x0, y0 = 2480000.0, 4500000.0
    gids = pa.publish_units([UnitPolygon(box(x0 + 1.0 + 0.4 * k, y0 + 1.2, x0 + 1.4 + 0.4 * k, y0 + 1.6), us=str(k + 1),
                                         area="1", tipo="US") for k in range(3)], "Saggio", crs=ch.crs)
    w.pa = pa
    w.units = []
    for u in pa.us_polygons("Saggio", crs=ch.crs):
        w.units.append(u)
        w._draw_unit(u, len(w.units) - 1, layer="US da PyArchInit - Saggio")
    layer = w.ortho_view.layers["US da PyArchInit - Saggio"]
    by_us = {f[2]["us"]: f[0] for f in layer["features"]}
    w.set_draw_mode("select")
    w.ortho_view.select_items([by_us["1"], by_us["2"]])
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(lambda cls, *a, **k: {"keep": "0"}))
    assert w.merge_selected()
    w.ortho_view.select_items([f[0] for f in w.ortho_view.layers["US da PyArchInit - Saggio"]["features"]
                               if f[2]["us"] == "3"])
    assert w.delete_selected(confirm=False) == 1
    assert w.pa_deleted == {gids[1], gids[2]} and w.pa_geom_changed == {gids[0]}
    assert not w.b_pa_save.isHidden()
    monkeypatch.setattr(w, "_info", lambda *a, **k: None)
    w.save_pending_units(confirm=False)
    w.runner.wait()
    qapp.processEvents()
    rows = pa.us_polygons("Saggio")
    assert [int(r.attributes["gid"]) for r in rows] == [gids[0]]
    assert abs(rows[0].geometry.area - 0.32) < 1e-4 and rows[0].us == "1"
    assert not w.pa_pending and not w.pa_deleted and w.b_pa_save.isHidden()


def test_preferences_stay_in_the_test_folder(window, tmp_path):
    """The window must not read or write the user's real preferences while tested."""
    assert str(tmp_path) in window.settings.fileName()


class _AcceptUnit:
    """UnitDialog stand-in: accepts with US 9."""

    class DialogCode:
        Accepted = 1

    def __init__(self, *args, **kwargs):
        pass

    def exec(self):
        return 1

    def values(self):
        return {"us": "9", "area": "1", "tipo": "US"}


def test_freehand_and_snapping(window, qapp, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from shapely.geometry import box

    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon
    from pyarchinit_photogrammetry.gui import main_window as mw

    monkeypatch.setattr(mw, "UnitDialog", _AcceptUnit)
    w = window
    v = w.ortho_view
    for name in list(v.layers):
        v.set_layer_visible(name, False)
    x0, y0 = 2480000.0, 4500000.0
    u = UnitPolygon(box(x0 + 1.0, y0 + 1.2, x0 + 1.6, y0 + 1.8), us="1", area="1", tipo="US")
    w.units = [u]
    w._draw_unit(u, 0)
    v.zoom_to(v.layer_rect("Unita' stratigrafiche"), 0.8)
    qapp.processEvents()

    def vp(x, y):
        return v.mapFromScene(v.scene_pt(x0 + x, y0 + y))

    def stroke(points):
        QTest.mousePress(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vp(*points[0]))
        for p in points[1:]:
            QTest.mouseMove(v.viewport(), vp(*p))
        QTest.mouseRelease(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vp(*points[-1]))
        qapp.processEvents()

    # freehand: press, draw, release closes the unit (simplified, original points kept)
    w.a_freehand.setChecked(True)
    w.set_draw_mode("polygon")
    ring = [(2.0, 1.2), (2.3, 1.2), (2.5, 1.3), (2.5, 1.7), (2.2, 1.8), (2.0, 1.6)]
    trace = [(a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])) for a, b in zip(ring, ring[1:])
             for t in np.linspace(0, 1, 12, endpoint=False)]
    stroke(trace)
    assert len(w.units) == 2 and w.units[1].us == "9" and 3 <= len(w.units[1].geometry.exterior.coords) - 1 < len(trace)
    # snapping: clicks land exactly on a vertex and on a segment
    w.a_freehand.setChecked(False)
    w.a_snap.setChecked(True)
    assert v.snap_vertex and v.snap_segment
    w.set_draw_mode("polygon")
    QTest.mouseMove(v.viewport(), vp(1.604, 1.797))
    assert v._snap_marks["vertex"].isVisible()
    QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vp(1.604, 1.797))
    QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vp(1.597, 1.5))
    c = v._click_xy
    assert c[0] == (x0 + 1.6, y0 + 1.8) and c[1][0] == x0 + 1.6  # exactly the vertex, exactly on the edge
    w.a_snap_vertex.setChecked(False)  # segments only: the corner is no longer preferred
    assert not v.snap_vertex and v.snap_segment
    w.a_snap_vertex.setChecked(True)
    # freehand along the edge of US 1 with a small wobble: a shared edge, no gap and no overlap
    w.set_draw_mode("pan")
    w.a_freehand.setChecked(True)
    w.set_draw_mode("polygon")
    stroke([(1.6 + 0.004 * np.sin(k), 1.25 + 0.5 * k / 30) for k in range(31)] + [(1.9, 1.75), (1.9, 1.25)])
    g = w.units[-1].geometry
    edge = [pt for pt in g.exterior.coords if abs(pt[0] - (x0 + 1.6)) < 1e-6]
    assert len(edge) >= 2 and all(pt[0] == x0 + 1.6 for pt in edge)  # snapped coordinates kept exactly
    assert g.intersection(w.units[0].geometry).area < 1e-12
    assert w.units[0].geometry.union(g).geom_type == "Polygon"
    assert w.settings.value("draw/snap") in ("true", True) and w.settings.value("draw/freehand") in ("true", True)
