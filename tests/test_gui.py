"""Desktop interface, run headless (Qt offscreen platform, software 3D renderer)."""
from __future__ import annotations

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
def window(qapp, processed, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path))
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    doc, ch, _ = processed
    w = MainWindow(doc)
    w.runner.show_dialog = False
    w.resize(1400, 900)
    w.show()
    qapp.processEvents()
    yield w
    w.setWindowModified(False)
    w.close()


def test_window_shows_products(window, qapp):
    w = window
    assert w.model_view.backend == "software"
    assert w.model_view.kind == "model_textured"
    img = w.model_view.canvas.grab_array()
    assert img.std() > 10  # something was drawn, not an empty background
    assert w.ortho_view.tr is not None and w.ortho_view.kind == "ortho"
    assert w.workspace.topLevelItemCount() == 1
    assert w.reference.table.rowCount() == 8
    assert w.photos.count() == len(w.chunk.cameras)
    for kind in ("tie_points", "point_cloud", "model"):
        w.model_view.show_kind(kind)
        qapp.processEvents()
        assert w.model_view.canvas.grab_array().std() > 10


def test_console_and_photo_view(window, qapp):
    w = window
    assert not w.console.run_source("n = len(chunk.aligned_cameras)")
    assert w.console.console.locals["n"] == len(w.chunk.cameras)
    cam = w.chunk.cameras[0]
    w.open_photo(cam)
    assert w.photo_view.pixmap_item is not None
    m = w.chunk.markers[0]
    before = len(m.projections)
    w.photo_view._place(m, 100.5, 200.5)
    assert m.projections[cam.key].pinned and len(m.projections) >= before
    w.photo_view._remove(m)
    assert cam.key not in m.projections


def test_drawing_tools_and_section(window, qapp, monkeypatch):
    from pyarchinit_photogrammetry.gui import main_window as mw

    w = window
    x0, y0 = w.chunk.to_world(w.chunk.aligned_cameras[0].center)[:2]
    z = w.ortho_view.elevation(x0, y0)
    assert z is not None and 99.0 < z < 101.0
    # section across the trench, no dialog
    monkeypatch.setattr(mw.ImageDialog, "exec", lambda self: 0)
    a = np.array([2480000.0 + 1.0, 4500000.0 + 1.5])
    b = np.array([2480000.0 + 3.0, 4500000.0 + 1.5])
    w._section_drawn(a, b)
    w.runner.wait()
    s = w._last_section
    assert s.polylines and 1.9 < s.length < 2.1
    z_all = np.concatenate([p[:, 1] for p in s.polylines])
    assert np.ptp(z_all) > 0.25  # wall stub and trench floor are cut


def test_background_task_cancel(window, qapp):
    import time

    from pyarchinit_photogrammetry.core.progress import Progress

    w = window
    seen = []

    def job(p: Progress):
        for i in range(200):
            p(i / 200, "lavoro")
            sub = p.sub(0.5, 0.5)
            sub(0.1)
            time.sleep(0.01)
        return "finito"

    assert w.runner.run("prova", job, seen.append, seen.append)
    w.runner.cancel()
    w.runner.wait()
    assert seen == [] and not w.runner.busy  # cancelled: neither done nor error


def test_pyarchinit_publish_from_gui(window, qapp, tmp_path, monkeypatch):
    if not _spatialite():
        pytest.skip("mod_spatialite not available")
    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon
    from pyarchinit_photogrammetry.gui import main_window as mw
    from pyarchinit_photogrammetry.pyarchinit.demo_db import create_demo_database
    from shapely.geometry import box

    w = window
    cfg = create_demo_database(tmp_path / "pa")
    w.pyarchinit_connect(config=cfg)
    assert w.pa is not None and w.pa_pane.site_names() == ["Saggio dimostrativo"]
    w.units = [UnitPolygon(box(2480001.7, 4500001.3, 2480002.3, 4500001.5), us="2", area="1", tipo="USM")]
    monkeypatch.setattr(mw.QInputDialog, "getItem", lambda *a, **k: ("Saggio dimostrativo", True))
    monkeypatch.setattr(mw, "message", lambda *a, **k: None)
    w.pyarchinit_publish_units()
    w.runner.wait()
    polys = w.pa.us_polygons("Saggio dimostrativo")
    assert len(polys) == 1 and polys[0].us == "2"


def _spatialite():
    import sqlite3

    try:
        import rasterio.crs

        rasterio.crs.CRS.from_epsg(4326).to_wkt()
        con = sqlite3.connect(":memory:")
        con.enable_load_extension(True)
        con.load_extension("mod_spatialite")
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 0.3: icons, theme, log, errors, start page
def test_icons_and_theme(qapp):
    from pyarchinit_photogrammetry.gui import theme
    from pyarchinit_photogrammetry.gui.icons import ICONS, app_icon, available, icon

    assert not app_icon().isNull()
    if available():
        for name in ("align", "dense", "error", "camera_ok"):
            assert not icon(name).isNull()
        assert len(ICONS) > 60
    for mode in ("dark", "light"):
        theme.apply(qapp, mode)
        assert theme.current(qapp) == mode and qapp.styleSheet()


def test_log_pane_levels_filter_and_text(qapp):
    import logging

    from pyarchinit_photogrammetry.gui.panels import LogPane

    pane = LogPane()
    try:
        logging.getLogger("pyarchinit_photogrammetry").info("allineamento finito")
        qapp.processEvents()
        pane.warning("attenzione")
        pane.error("fallito", "Traceback: boom")
        assert pane.table.rowCount() == 3 and pane.counts == {"error": 1, "warning": 1}
        pane.level.setCurrentIndex(2)  # only errors
        assert pane.table.rowCount() == 1 and pane.table.item(0, 2).text() == "fallito"
        assert "boom" in pane.table.item(0, 2).toolTip()
        pane.level.setCurrentIndex(0)
        pane.search.setText("atten")
        assert pane.table.rowCount() == 1
        pane.search.setText("")
        txt = pane.text(visible_only=False)
        assert "allineamento finito" in txt and "Traceback: boom" in txt
    finally:
        pane.detach()


def test_error_dialog_is_non_native_and_has_hint(qapp):
    from pyarchinit_photogrammetry.gui.dialogs import ErrorDialog, suggestion_for

    tb = "Traceback\nIsolatedCrash: _poisson_worker crashed in the worker process (exit code -11)"
    assert suggestion_for(tb) is not None
    d = ErrorDialog("Errore", "Modello: fallito", tb)
    d.toggle.setChecked(True)
    assert d.box.isVisible() or d.box.toPlainText() == tb
    assert "fallito" in d.report_text() and "exit code -11" in d.report_text()
    d.close()


def test_message_helper_uses_qt_dialog(qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from pyarchinit_photogrammetry.gui import dialogs

    seen = {}

    def fake_exec(self):
        seen["native_off"] = self.testOption(QMessageBox.Option.DontUseNativeDialog)
        seen["text"] = self.text()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    dialogs.message(None, "warning", "Titolo", "Attenzione")
    assert seen == {"native_off": True, "text": "Attenzione"}


def test_start_page_without_project(qapp, tmp_path):
    from PySide6.QtCore import QSettings

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path))
    from pyarchinit_photogrammetry.app import app as application
    from pyarchinit_photogrammetry.core.document import Document
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    application.document = Document()  # as when the interface starts without a project
    w = MainWindow(None)
    w.runner.show_dialog = False
    w.show()
    qapp.processEvents()
    assert w.stack.currentWidget() is w.start_page
    w.new_document()
    assert w.stack.currentWidget() is w.tabs
    w.show_start_page()
    assert w.stack.currentWidget() is w.start_page
    w.setWindowModified(False)
    w.close()


def test_task_failure_goes_to_log_and_dialog(window, qapp, monkeypatch):
    from pyarchinit_photogrammetry.gui import main_window as mw

    shown = []
    monkeypatch.setattr(mw.ErrorDialog, "exec", lambda self: shown.append((self.summary, self.details)) or 0)

    def job(p):
        raise RuntimeError("not enough points in point_cloud to build a mesh")

    w = window
    before = w.log_pane.counts["error"]
    w.run_task("Modello", job, refresh=False, save=False)
    w.runner.wait()
    assert w.log_pane.counts["error"] == before + 1
    assert shown and "not enough points" in shown[0][0] and "RuntimeError" in shown[0][1]


def test_doctor_report(qapp):
    from pyarchinit_photogrammetry.doctor import run_checks

    rep = run_checks(gui=True)
    names = [c.name for c in rep.checks]
    assert "SciPy" in names and "PySide6 (Qt)" in names and "Open3D" in names
    assert not rep.failures, rep.text()
    assert "PySide6" in rep.text()
