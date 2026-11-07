"""The raster of the 2D view (orthophoto, DEM, relief, an elevation or a section) can be hidden and faded so that the
drawings on top can be seen: the view, the row *Sfondo* of the Layer pane, *Vista > Mostra lo sfondo*, the choice kept
per raster for the session, no detail read while hidden, drawing on a hidden raster."""
from __future__ import annotations

import os
import shutil

import numpy as np
import pytest
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_origin

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")

ORIGIN = np.array([2480000.0, 4500000.0, 100.0])  # the synthetic trench (testing.synthetic.make_scene)


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _ortho(path, size=2048, res=0.01):
    rows, cols = np.mgrid[0:size, 0:size]
    rgba = np.stack([cols % 256, rows % 256, ((rows // 2 + cols // 2) % 2) * 255,
                     np.full((size, size), 255)]).astype(np.uint8)
    profile = {"driver": "GTiff", "height": size, "width": size, "count": 4, "dtype": "uint8",
               "transform": from_origin(1000.0, 2000.0, res, res), "tiled": True, "blockxsize": 256,
               "blockysize": 256, "photometric": "RGB"}
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(rgba)
        dst.build_overviews([2, 4, 8], Resampling.average)


def _dem(path, size=256, res=0.08):
    z = np.add.outer(np.arange(size), np.arange(size)).astype(np.float32) * 0.01
    with rasterio.open(path, "w", driver="GTiff", height=size, width=size, count=1, dtype="float32",
                       transform=from_origin(1000.0, 2000.0, res, res)) as dst:
        dst.write(z, 1)


@pytest.fixture
def view(qapp):
    from pyarchinit_photogrammetry.gui.image_views import RasterView

    v = RasterView()
    v.resize(400, 300)
    v.show()
    qapp.processEvents()
    yield v
    v.close()


def _brush(v):
    return v.backgroundBrush().color().name()


def test_hide_show_and_fade_the_raster(view, qapp, tmp_path):
    from pyarchinit_photogrammetry.gui import theme

    _ortho(tmp_path / "o.tif")
    v = view
    v.load(tmp_path / "o.tif", "ortho", max_side=512)
    normal = _brush(v)
    seen = []
    v.raster_visibility_changed.connect(lambda shown, opacity: seen.append((shown, opacity)))
    assert v.has_raster() and v.raster_visible and v.raster_opacity == 1.0
    v.set_raster_visible(False)
    assert not v.pixmap_item.isVisible() and not v.raster_visible
    assert _brush(v) == QColor_name(theme.tokens()["canvas_off"])  # the neutral background of the theme
    assert seen == [(False, 1.0)]
    v.set_raster_visible(False)  # no change, no signal
    assert seen == [(False, 1.0)]
    v.set_raster_visible(True)
    assert v.pixmap_item.isVisible() and _brush(v) == normal
    v.set_raster_opacity(0.4)
    assert v.pixmap_item.opacity() == pytest.approx(0.4) and v.raster_opacity == pytest.approx(0.4)
    assert _brush(v) == QColor_name(theme.tokens()["canvas_off"])  # faded over the theme's background
    assert seen[-1] == (True, pytest.approx(0.4))
    # the detail read when zooming in is faded as the reduced copy
    v.fitInView(v.scene_pt(1000.0 + 2.0, 2000.0 - 2.0).x(), v.scene_pt(1000.0 + 2.0, 2000.0 - 2.0).y(), 20, 15)
    v.update_detail(wait=True)
    assert v._detail_item is not None and v._detail_item.isVisible()
    assert v._detail_item.opacity() == pytest.approx(0.4)
    v.set_raster_opacity(5.0)  # clipped to 0-1
    assert v.raster_opacity == 1.0 and _brush(v) == normal


def QColor_name(text):
    from PySide6.QtGui import QColor

    return QColor(text).name()


def test_no_detail_is_read_while_hidden(view, qapp, tmp_path, monkeypatch):
    from pyarchinit_photogrammetry.gui.image_views import RasterView

    _ortho(tmp_path / "o.tif")
    v = view
    v.load(tmp_path / "o.tif", "ortho", max_side=512)
    reads = []
    real = RasterView._read_detail
    monkeypatch.setattr(RasterView, "_read_detail", staticmethod(lambda job: reads.append(job) or real(job)))
    v.fitInView(v.scene_pt(1000.0 + 2.0, 2000.0 - 2.0).x(), v.scene_pt(1000.0 + 2.0, 2000.0 - 2.0).y(), 20, 15)
    v.set_raster_visible(False)
    v.update_detail(wait=True)
    assert reads == [] and (v._detail_item is None or not v._detail_item.isVisible())
    assert not v._detail_timer.isActive()  # panning or zooming a hidden raster asks for nothing
    v.scale(1.2, 1.2)
    v._view_changed()
    assert not v._detail_timer.isActive()
    v.set_raster_visible(True)
    v.update_detail(wait=True)
    assert len(reads) == 1 and v._detail_item.isVisible()


def test_the_choice_is_kept_per_raster(view, tmp_path):
    _ortho(tmp_path / "o.tif", size=512)
    _dem(tmp_path / "d.tif")
    v = view
    v.load(tmp_path / "o.tif", "ortho", max_side=512)
    v.set_raster_visible(False)
    v.set_raster_opacity(0.5)
    v.load(tmp_path / "d.tif", "dem")
    assert v.raster_visible and v.raster_opacity == 1.0 and v.pixmap_item.isVisible()  # another raster: shown
    v.set_raster_opacity(0.7)
    v.load(tmp_path / "o.tif", "ortho", max_side=512)
    assert not v.raster_visible and v.raster_opacity == pytest.approx(0.5) and not v.pixmap_item.isVisible()
    assert v.pixmap_item.opacity() == pytest.approx(0.5)
    v.load(tmp_path / "d.tif", "dem")
    assert v.raster_visible and v.raster_opacity == pytest.approx(0.7)
    v.load(tmp_path / "d.tif", "slrm")  # the same file shown as another kind: a raster of its own
    assert v.raster_visible and v.raster_opacity == 1.0


def test_an_empty_frame_has_no_background_to_hide(view, tmp_path):
    _ortho(tmp_path / "o.tif", size=512)
    v = view
    v.load(tmp_path / "o.tif", "ortho", max_side=512)
    normal = _brush(v)
    v.set_raster_visible(False)
    v.show_frame((0.0, 0.0, 10.0, 5.0), 0.01, "Cornice")
    assert not v.has_raster() and v.raster_visible and v.raster_opacity == 1.0 and _brush(v) == normal
    v.set_raster_visible(False)  # nothing to hide: nothing changes
    assert v.raster_visible and _brush(v) == normal
    v.clear_raster("niente")
    assert not v.has_raster() and v.pixmap_item is None and _brush(v) == normal


def test_the_background_follows_the_theme(view, qapp, tmp_path):
    from pyarchinit_photogrammetry.gui import theme

    _ortho(tmp_path / "o.tif", size=512)
    v = view
    v.load(tmp_path / "o.tif", "ortho", max_side=512)
    v.set_raster_visible(False)
    before = qapp.property("ppg_theme")
    try:
        qapp.setProperty("ppg_theme", "dark")
        v.refresh_background()
        assert _brush(v) == QColor_name(theme.DARK["canvas_off"])
        qapp.setProperty("ppg_theme", "light")
        v.refresh_background()
        assert _brush(v) == QColor_name(theme.LIGHT["canvas_off"])
    finally:
        qapp.setProperty("ppg_theme", before)
    assert theme.LIGHT["canvas_off"] != theme.DARK["canvas_off"]


# ---------------------------------------------------------------- the window
@pytest.fixture(scope="module")
def project(processed, tmp_path_factory):
    """A copy of the processed project with a section across the trench and an elevation of its south wall."""
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.engine import elevation as E

    doc, _ch, _ = processed
    d = tmp_path_factory.mktemp("sfondo") / "base"
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")
    out = ppg.Document.open(d / doc.path.name)
    ch = out.chunk
    ch.meta.pop("drawings", None)
    a, b = ORIGIN[:2] + [2.0, 0.4], ORIGIN[:2] + [2.0, 2.6]
    section = E.build_elevation(ch, E.plane_from_line(ch, a, b, side=ORIGIN[:2] + [0.5, 1.5]), resolution=0.01,
                                depth=(-1.0, 0.5), max_angle=89.0, kind="section", name="Sezione A")
    wall = E.build_elevation(ch, E.plane_from_line(ch, ORIGIN[:2] + [1.05, 0.9], ORIGIN[:2] + [2.95, 0.9],
                                                   side=ORIGIN[:2] + [2.0, 1.5]),
                             resolution=0.01, depth=(-0.08, 0.08), max_angle=89.0, name="Parete sud")
    out.meta["sito"] = "Saggio"
    out.save()
    return out.path, section["id"], wall["id"]


@pytest.fixture
def win(qapp, project, tmp_path):
    from PySide6.QtCore import QSettings

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    path, sid, wid = project
    d = tmp_path / "progetto"
    d.mkdir()
    shutil.copy2(path, d / path.name)
    shutil.copytree(path.parent / (path.name + ".files"), d / (path.name + ".files"))
    (d / "photos").symlink_to(path.parent / "photos")
    doc = ppg.Document.open(d / path.name)
    w = MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    w.resize(1400, 900)
    w.show()
    qapp.processEvents()
    w.ids = (sid, wid)
    yield w
    w.photos.stop()
    w.setWindowModified(False)
    w.close()


def _entry(ch, eid):
    return next(e for e in ch.meta["elevations"] if e["id"] == eid)


def test_the_pane_row_and_the_menu_action_go_together(win, qapp):
    from pyarchinit_photogrammetry.gui.command_search import menu_commands

    w = win
    v, pane = w.ortho_view, w.layers_pane
    w._show_raster("ortho")
    qapp.processEvents()
    assert not pane.raster_row.isHidden()
    assert pane.raster_box.text() == "Sfondo: Ortofoto" and pane.raster_box.isChecked()
    assert w.a_show_background.isEnabled() and w.a_show_background.isChecked()
    assert w.a_show_background.shortcut().toString() == "Ctrl+Shift+B"
    found = [k for k in menu_commands(w.menuBar()) if k.startswith("Mostra lo sfondo")]
    assert found and found[0].endswith("Vista")  # *Cerca un comando* finds it
    pane.raster_box.setChecked(False)  # the box of the pane hides it ...
    assert not v.raster_visible and not w.a_show_background.isChecked()
    assert not pane.opacity_slider.isEnabled()
    w.a_show_background.trigger()  # ... the menu shows it again
    assert v.raster_visible and pane.raster_box.isChecked() and pane.opacity_slider.isEnabled()
    pane.opacity_slider.setValue(40)
    assert v.raster_opacity == pytest.approx(0.4) and pane.opacity_label.text() == "40 %"
    # Tutti / Nessuno: the drawn layers only, the background stays as it is
    v.add_line(np.array([ORIGIN[:2] + [1.0, 1.0], ORIGIN[:2] + [2.0, 1.5]]), layer="Prova")
    pane.b_none.click()
    assert not v.layers["Prova"]["visible"] and v.raster_visible and v.raster_opacity == pytest.approx(0.4)
    pane.b_all.click()
    assert v.layers["Prova"]["visible"] and v.raster_visible
    assert "sfondo" in pane.b_all.toolTip() and "sfondo" in pane.b_none.toolTip()
    # nothing shown: no row, the action disabled
    v.clear_raster("niente")
    qapp.processEvents()
    assert pane.raster_row.isHidden() and not w.a_show_background.isEnabled()


def test_ortho_and_dem_keep_their_own_choice(win, qapp):
    w = win
    v, pane = w.ortho_view, w.layers_pane
    w._show_raster("ortho")
    pane.raster_box.setChecked(False)
    w._show_raster("dem")
    assert pane.raster_box.text() == "Sfondo: DEM" and pane.raster_box.isChecked() and v.raster_visible
    w._show_raster("ortho")
    assert not v.raster_visible and not pane.raster_box.isChecked() and not w.a_show_background.isChecked()
    w._show_raster("dem")
    assert v.raster_visible and w.a_show_background.isChecked()


def test_elevation_and_section_keep_their_own_choice(win, qapp):
    w = win
    v, pane = w.ortho_view, w.layers_pane
    sid, wid = w.ids
    ch = w.chunk
    w.show_elevation(_entry(ch, wid))
    assert pane.raster_box.text() == "Sfondo: Prospetto «Parete sud»" and v.raster_visible
    w.a_show_background.trigger()
    assert not v.raster_visible
    w._show_raster("ortho")  # the plan: its own choice
    assert v.raster_visible and pane.raster_box.text() == "Sfondo: Ortofoto"
    w.show_elevation(_entry(ch, sid))
    assert pane.raster_box.text() == "Sfondo: Sezione «Sezione A»" and v.raster_visible
    pane.opacity_slider.setValue(30)
    w.show_elevation(_entry(ch, wid))
    assert not v.raster_visible and not pane.raster_box.isChecked()
    w.show_elevation(_entry(ch, sid))
    assert v.raster_visible and v.raster_opacity == pytest.approx(0.3) and pane.opacity_slider.value() == 30


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


def test_drawing_on_a_hidden_raster(win, qapp, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from pyarchinit_photogrammetry.gui import main_window as mw

    monkeypatch.setattr(mw, "UnitDialog", _AcceptUnit)
    w = win
    v = w.ortho_view
    w._show_raster("ortho")
    x0, y0 = ORIGIN[:2]
    v.zoom_to(v.scene().sceneRect(), 0.0)
    w.a_show_background.trigger()
    assert not v.raster_visible
    assert v.elevation(x0 + 2.0, y0 + 1.5) is not None  # the heights are still read from the files
    qapp.processEvents()

    def vp(x, y):
        return v.mapFromScene(v.scene_pt(x0 + x, y0 + y))

    n = len(w.units)
    w.a_freehand.setChecked(True)
    w.set_draw_mode("polygon")
    ring = [(2.0, 1.2), (2.3, 1.2), (2.5, 1.3), (2.5, 1.7), (2.2, 1.8), (2.0, 1.6)]
    trace = [(a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])) for a, b in zip(ring, ring[1:])
             for t in np.linspace(0, 1, 12, endpoint=False)]
    QTest.mousePress(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vp(*trace[0]))
    for p in trace[1:]:
        QTest.mouseMove(v.viewport(), vp(*p))
    QTest.mouseRelease(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vp(*trace[-1]))
    qapp.processEvents()
    w.a_freehand.setChecked(False)
    w.set_draw_mode("pan")
    assert len(w.units) == n + 1 and w.units[-1].us == "9"
    assert not v.raster_visible  # drawing leaves the background hidden
    assert v.layers["Unita' stratigrafiche"]["visible"]


def test_a_faded_raster_composes_once_under_the_detail(view, qapp, tmp_path):
    """Faded at 50 %, the full-resolution detail and the reduced copy under it would compose to 75 %: the reduced copy
    is not painted where the detail is, and no seam shows at the edge of the detail."""
    import rasterio
    from PySide6.QtCore import QPoint

    path = tmp_path / "bianco.tif"
    size = 2048
    with rasterio.open(path, "w", driver="GTiff", height=size, width=size, count=3, dtype="uint8",
                       transform=from_origin(1000.0, 2000.0, 0.01, 0.01), tiled=True, blockxsize=256,
                       blockysize=256) as dst:
        dst.write(np.full((3, size, size), 255, np.uint8))
        dst.build_overviews([2, 4, 8], Resampling.average)
    v = view
    v.load(path, "ortho", max_side=512)
    v.set_raster_opacity(0.5)
    p = v.scene_pt(1000.0 + 10.0, 2000.0 - 10.0)
    v.fitInView(p.x(), p.y(), 20, 15)
    v.update_detail(wait=True)
    qapp.processEvents()
    assert v._detail_item is not None and v._detail_item.isVisible() and v.pixmap_item.hole is not None
    from pyarchinit_photogrammetry.gui import theme

    bg = QColor_rgb(theme.tokens()["canvas_off"])
    expected = 0.5 * 255 + 0.5 * bg

    def pixel(img, x, y):
        c = img.pixelColor(x, y)
        return np.array([c.red(), c.green(), c.blue()], float)

    img = v.viewport().grab().toImage()
    centre = pixel(img, img.width() // 2, img.height() // 2)
    assert np.abs(centre - expected).max() <= 3, (centre, expected)
    # move the view without reading again: the edge of the detail comes into the view, the same grey on both sides
    edge = v.mapFromScene(v._detail_item.mapRectToScene(v._detail_item.boundingRect()).topLeft())
    v.centerOn(v.mapToScene(edge + QPoint(5, 5)))
    v._detail_timer.stop()
    qapp.processEvents()
    v._detail_timer.stop()
    img = v.viewport().grab().toImage()
    e = v.mapFromScene(v._detail_item.mapRectToScene(v._detail_item.boundingRect()).topLeft())
    assert 4 <= e.x() <= img.width() - 5 and 4 <= e.y() <= img.height() - 5
    for dx, dy in ((-3, 3), (3, 3), (3, -3), (0, 3), (3, 0)):
        assert np.abs(pixel(img, e.x() + dx + 10, e.y() + dy + 10) - expected).max() <= 3
    across = [pixel(img, e.x() + k, e.y() + 20) for k in range(-3, 4)]
    assert max(np.abs(a - expected).max() for a in across) <= 6, across
    v.set_raster_opacity(1.0)
    assert v.pixmap_item.hole is None


def QColor_rgb(name):
    from PySide6.QtGui import QColor

    c = QColor(name)
    return np.array([c.red(), c.green(), c.blue()], float)
