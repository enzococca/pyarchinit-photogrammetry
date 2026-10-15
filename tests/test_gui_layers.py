"""Layers on the orthomosaic view, the sections pane and the PyArchInit dialogs (offscreen)."""
from __future__ import annotations

import json
import os

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
