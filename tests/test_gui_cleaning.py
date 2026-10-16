"""Confidence, cleaning, cropping, the lasso and unreadable photos in the interface (offscreen)."""
from __future__ import annotations

import os
import shutil

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
def win(qapp, processed, tmp_path):
    """The main window on a copy of the processed trench (these tests change the products)."""
    from PySide6.QtCore import QSettings

    import pyarchinit_photogrammetry as ppg

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    src, _ch, _ = processed
    d = tmp_path / "progetto"
    d.mkdir()
    shutil.copy2(src.path, d / src.path.name)
    shutil.copytree(src.files_dir, d / src.files_dir.name)
    shutil.copytree(src.path.parent / "photos", d / "photos")
    doc = ppg.Document.open(d / src.path.name)
    doc.meta["sito"] = "Saggio"
    w = MainWindow(doc, autoconnect=False)
    w.runner.show_dialog = False
    w.resize(1400, 900)
    w.show()
    qapp.processEvents()
    yield w
    w.photos.stop()
    w.setWindowModified(False)
    w.close()


def _answers(monkeypatch, answers, seen=None):
    from pyarchinit_photogrammetry.gui import main_window as mw

    def fake(cls, parent, title, fields, intro=""):
        if seen is not None:
            seen.append((title, {f.name: f for f in fields}, intro))
        return dict(answers)

    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(fake))


def _triangle(ch, shrink=0.25):
    w = ch.to_world(ch.point_cloud.xyz)
    lo, hi = np.percentile(w[:, :2], 10, axis=0), np.percentile(w[:, :2], 90, axis=0)
    c, h = (lo + hi) / 2, (hi - lo) / 2 * (1 - shrink)
    return np.array([[c[0] - h[0], c[1] - h[1]], [c[0] + h[0], c[1] - h[1]], [c[0], c[1] + h[1]]])


def test_crop_tool_draws_a_box_or_a_polygon(win, qapp, monkeypatch):
    import rasterio
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    from pyarchinit_photogrammetry.gui import main_window as mw

    w = win
    v = w.ortho_view
    got = []
    monkeypatch.setattr(w, "crop_products", lambda geom: got.append(geom))
    w.start_crop()
    assert v.mode == "crop" and w.draw_actions["crop"].isChecked()
    v.fit()
    qapp.processEvents()
    with rasterio.open(w.chunk.orthomosaic_path) as src:
        b = src.bounds

    def vp(fx, fy):
        return v.mapFromScene(v.scene_pt(b.left + fx * (b.right - b.left), b.bottom + fy * (b.top - b.bottom)))

    def world(fx, fy):
        return b.left + fx * (b.right - b.left), b.bottom + fy * (b.top - b.bottom)

    # a box: press, drag, release
    QTest.mousePress(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vp(0.3, 0.3))
    QTest.mouseMove(v.viewport(), vp(0.45, 0.5))
    QTest.mouseMove(v.viewport(), vp(0.6, 0.7))
    QTest.mouseRelease(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vp(0.6, 0.7))
    assert len(got) == 1 and got[0].geom_type == "Polygon"
    (ax, ay), (bx, by) = world(0.3, 0.3), world(0.6, 0.7)
    assert abs(got[0].area - abs(bx - ax) * abs(by - ay)) < 0.02 * got[0].area
    assert mw.CROP_LAYER in v.layers
    # a polygon: clicks, then a double click closes it
    for f in ((0.3, 0.3), (0.7, 0.3), (0.5, 0.7)):
        QTest.mouseClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vp(*f))
    QTest.mouseDClick(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vp(0.5, 0.7))
    assert len(got) == 2
    tri_area = 0.5 * 0.4 * (b.right - b.left) * 0.4 * (b.top - b.bottom)
    assert abs(got[1].area - tri_area) < 0.05 * tri_area and len(got[1].exterior.coords) - 1 >= 3
    assert len(v.features(mw.CROP_LAYER)) == 1  # the latest crop polygon only
    # the polygon of a selected feature
    w.set_draw_mode("select")
    item = v.layers[mw.CROP_LAYER]["features"][0][0]
    v.select_items([item])
    w.crop_with_selection()
    assert len(got) == 3 and abs(got[2].area - got[1].area) < 1e-9


def test_crop_products_export_and_in_the_project(win, monkeypatch, tmp_path):
    from shapely.geometry import Polygon

    from pyarchinit_photogrammetry.core import versions
    from pyarchinit_photogrammetry.gui import main_window as mw

    w = win
    ch = w.chunk
    geom = Polygon(_triangle(ch))
    infos = []
    monkeypatch.setattr(w, "_info", lambda title, text: infos.append(text))
    monkeypatch.setattr(mw, "choice", lambda *a, **k: 0)
    out = tmp_path / "ritagli"
    seen = []
    base = {"what": ["dem", "orthomosaic", "model", "point_cloud"], "folder": str(out), "name": "saggio 1",
            "model_format": "ply", "cloud_format": "las", "use_z": False, "zmin": 0.0, "zmax": 0.0, "keep": True}
    _answers(monkeypatch, {**base, "action": "export"}, seen)
    w.crop_products(geom)
    w.runner.wait()
    assert {k for k, _ in seen[0][1]["what"].choices} == {"dem", "orthomosaic", "model", "point_cloud"}
    assert sorted(p.name for p in out.iterdir()) == ["saggio_1_dem.tif", "saggio_1_modello.ply", "saggio_1_nuvola.las",
                                                     "saggio_1_ortofoto.tif", "saggio_1_poligono.geojson"]
    assert infos and "saggio_1_dem.tif" in infos[-1]
    n_points = len(ch.point_cloud)
    _answers(monkeypatch, {**base, "action": "project"})
    w.crop_products(geom)
    w.runner.wait()
    assert len(ch.point_cloud) < n_points
    for k in ("dem", "orthomosaic", "model", "point_cloud"):
        assert len(versions.versions(ch, k)) == 1 and versions.active_info(ch, k)["name"] == "Ritagliato"


def test_confidence_and_cleaning_from_the_menus(win, monkeypatch):
    from pyarchinit_photogrammetry.core import versions
    from pyarchinit_photogrammetry.gui import main_window as mw

    w = win
    ch = w.chunk
    _answers(monkeypatch, {"what": ["point_cloud", "model"], "tol": 1.0})
    w.compute_confidence()
    w.runner.wait()
    mv = w.model_view
    assert mv.kind == "point_cloud" and mv.color_mode() == "confidence"
    assert mv.legend.mode == "confidence" and mv.legend.count() >= 2 and not mv.info_label.isVisibleTo(mv)
    assert ch.meta["confidence"]["model"]["points"] > 0
    # cleaning the cloud: the points found are shown in red, then removed once confirmed
    marked = []

    def confirm(parent, kind, title, text, options, **k):
        marked.append(int(mv.highlight.sum()))
        return 0

    monkeypatch.setattr(mw, "choice", confirm)
    _answers(monkeypatch, {"min_confidence": 3, "statistical": True, "neighbors": 16, "std_ratio": 2.0, "groups": True,
                           "group_points": 50, "group_distance": 0.0, "sparse": False, "radius": 0.0,
                           "min_neighbors": 4, "use_color": False, "color": (0, 0, 0), "color_tol": 40,
                           "keep": "keep", "version_name": ""})
    n = len(ch.point_cloud)
    w.clean_cloud()
    w.runner.wait()
    assert marked and marked[0] > 0 and mv.highlight is None
    assert len(ch.point_cloud) == n - marked[0]
    assert len(versions.versions(ch, "point_cloud")) == 1 and versions.active_info(ch, "point_cloud")["name"] == \
        "Nuvola pulita"
    assert ch.point_cloud.confidence.min() >= 3
    # cleaning the model
    _answers(monkeypatch, {"min_component": 1.0, "min_confidence": 0, "max_edge": 0.0, "repair": True,
                           "fill_holes": 0.0, "smooth": 1, "target_faces": 0, "keep": "keep", "version_name": ""})
    w.clean_model()
    w.runner.wait()
    assert ch.meta["model"]["cleaned"]["faces"] > 0 and len(versions.versions(ch, "model")) == 1


def test_lasso_deletes_or_keeps_points_and_faces(win, monkeypatch):
    w = win
    ch = w.chunk
    pc = ch.point_cloud
    n = len(pc)
    mask = np.zeros(n, bool)
    mask[: n // 10] = True
    _answers(monkeypatch, {"action": "delete", "code": 64, "other": 0, "name": "", "through": True,
                           "keep_copy": False})
    w._points_selected(mask, mask)
    w.runner.wait()
    assert len(ch.point_cloud) == n - n // 10
    # the model: the lasso over the whole view takes the visible faces
    w._show_model("model")
    mv = w.model_view
    image, project = mv.canvas.snapshot()
    width, height = mv.canvas.width(), mv.canvas.height()
    visible, inside = mv.select_faces([(0, 0), (width, 0), (width, height), (0, height)], project)
    faces = len(ch.model.faces)
    assert len(inside) == faces and inside.mean() > 0.5 and 0 < visible.sum() <= inside.sum()
    _answers(monkeypatch, {"action": "keep", "through": False, "keep_copy": True})
    w._faces_selected(visible, inside)
    w.runner.wait()
    assert len(ch.model.faces) == int(visible.sum())


def test_gradual_selection_of_the_tie_points(win, monkeypatch):
    from PySide6.QtWidgets import QDialog

    from pyarchinit_photogrammetry.engine.tiepoints import select_tie_points, tie_point_metrics
    from pyarchinit_photogrammetry.gui import main_window as mw
    from pyarchinit_photogrammetry.gui.dialogs import GradualSelectionDialog

    w = win
    ch = w.chunk
    metrics = tie_point_metrics(ch)
    d = GradualSelectionDialog(metrics, lambda **kw: select_tie_points(ch, metrics=metrics, **kw), w)
    assert not d.ok_button.isEnabled()  # nothing ticked
    box, spin = d.rows["max_reprojection_error"]
    box.setChecked(True)
    spin.setValue(float(np.percentile(metrics["reprojection_error"], 90)))
    assert d.selection["selected"] > 0 and "Selezionati" in d.status.text() and d.ok_button.isEnabled()
    assert d.values()["max_reprojection_error"] == spin.value() and d.values()["optimize"]
    d.close()

    class Fake:  # the dialog accepted with the 90th percentile of the errors
        def __init__(self, metrics, select, parent=None):
            self.selection = select(max_reprojection_error=float(np.percentile(metrics["reprojection_error"], 90)))

        def exec(self):
            return QDialog.DialogCode.Accepted

        def values(self):
            return {"optimize": True}

    monkeypatch.setattr(mw, "GradualSelectionDialog", Fake)
    before = len(metrics["ids"])
    w.clean_tie_points()
    w.runner.wait()
    after = tie_point_metrics(ch)
    assert len(after["ids"]) < before and "optimize" in ch.meta["sfm"]


def test_unreadable_photos_asked_before_a_step(win, monkeypatch, tmp_path):
    from pyarchinit_photogrammetry.gui import main_window as mw

    w = win
    ch = w.chunk
    cam = ch.aligned_cameras[0]
    real = cam.path
    empty = tmp_path / "dropbox" / os.path.basename(real)
    empty.parent.mkdir()
    empty.write_bytes(b"")
    cam.path = str(empty)
    answers = iter([1])
    monkeypatch.setattr(mw, "choice", lambda *a, **k: next(answers))
    assert w._photos_to_skip("Ortofoto") is True  # go on without it
    monkeypatch.setattr(mw, "choice", lambda *a, **k: None)
    assert w._photos_to_skip("Ortofoto") is None  # give up
    # link it to the folder of the real photos
    monkeypatch.setattr(mw, "choice", lambda *a, **k: 0)
    monkeypatch.setattr(mw.QFileDialog, "getExistingDirectory", lambda *a, **k: os.path.dirname(real))
    assert w._photos_to_skip("Ortofoto") is False
    assert os.path.samefile(cam.path, real) and not ch.unreadable_photos()
