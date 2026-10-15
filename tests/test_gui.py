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
    w = MainWindow(doc, autoconnect=False)
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
    w.runner.wait()
    assert w.pa is not None and w.pa_pane.site_names() == ["Saggio dimostrativo"]
    before = len(w.pa.us_polygons("Saggio dimostrativo"))  # the demo database already has unit polygons
    w.units = [UnitPolygon(box(2480001.7, 4500001.3, 2480002.3, 4500001.5), us="2", area="1", tipo="USM")]
    monkeypatch.setattr(mw.QInputDialog, "getItem", lambda *a, **k: ("Saggio dimostrativo", True))
    monkeypatch.setattr(mw, "message", lambda *a, **k: None)
    w.pyarchinit_publish_units()
    w.runner.wait()
    polys = w.pa.us_polygons("Saggio dimostrativo")
    assert len(polys) == before + 1 and polys[-1].us == "2"


def _spatialite():
    from conftest import _spatialite_available

    return _spatialite_available()


def test_window_fits_a_laptop_screen(window, qapp):
    """The panels wrap their buttons and scroll, the toolbars drop their texts: the window
    goes down to a small laptop screen without hiding commands (it was 1940 px wide)."""
    from PySide6.QtWidgets import QToolBar, QToolButton

    w = window
    for page in (w.layers_pane, w.proxies_pane, w.reference):  # the widest panes
        w._show_pane(w.left_tabs, page)
        qapp.processEvents()
        hint = w.minimumSizeHint()
        assert hint.width() <= 900 and hint.height() <= 640, (page, hint)
    styles = {}
    for width in (2200, 1280):
        w.resize(width, 720)
        for _ in range(3):
            qapp.processEvents()
        for tb in w.findChildren(QToolBar):
            buttons = [b for b in (tb.widgetForAction(a) for a in tb.actions()) if isinstance(b, QToolButton)]
            assert all(b.isVisible() for b in buttons), (width, tb.objectName())  # none behind >>
            assert tb.sizeHint().width() <= width
            styles[width, tb.objectName()] = [b.toolButtonStyle() for b in buttons]
    icon_only = styles[1280, "drawing_toolbar"][-1]
    assert icon_only not in styles[2200, "drawing_toolbar"]  # texts everywhere on a wide screen
    pan = styles[1280, "drawing_toolbar"][0]
    assert pan != icon_only  # the first drawing tools keep their names


def test_flow_layout_wraps_and_makes_room(qapp):
    from PySide6.QtWidgets import QListWidget, QPushButton, QVBoxLayout, QWidget

    from pyarchinit_photogrammetry.gui.flow_layout import FlowLayout

    page = QWidget()
    lay = QVBoxLayout(page)
    row = FlowLayout()
    buttons = [QPushButton(t) for t in ("Primo pulsante", "Secondo", "Terzo pulsante lungo", "Quarto")]
    for b in buttons:
        row.addWidget(b)
    lay.addLayout(row)
    below = QListWidget()
    lay.addWidget(below, 1)
    page.resize(900, 400)
    page.show()
    for _ in range(3):
        qapp.processEvents()
    assert len({b.y() for b in buttons}) == 1  # one line
    widest = max(b.sizeHint().width() for b in buttons)
    assert page.minimumSizeHint().width() <= widest + 40
    page.resize(widest + 30, 400)
    for _ in range(3):
        qapp.processEvents()
    ys = [b.y() for b in buttons]
    assert ys == sorted(ys) and len(set(ys)) == 4  # one per line
    assert below.y() >= buttons[-1].geometry().bottom()  # room made for the lines, nothing drawn over them
    assert page.minimumSizeHint().height() >= buttons[-1].geometry().bottom()
    page.close()
