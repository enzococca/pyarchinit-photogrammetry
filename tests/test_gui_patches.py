"""Patches of the orthomosaic and of the texture in the interface (offscreen): draw an area,
choose its photo, the orthomosaic changes there only; delete the patch and it comes back."""
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
    w = MainWindow(doc, autoconnect=False)
    w.runner.show_dialog = False
    w.resize(1400, 900)
    w.show()
    qapp.processEvents()
    yield w
    w.photos.stop()
    w.setWindowModified(False)
    w.close()


class _Choose:
    """Stands in for PatchDialog: takes the photo the test asks for, remembers what it saw."""

    seen: list = []
    pick = staticmethod(lambda cands: [cands[-1]["label"]])
    texture = False
    surface = "dem"

    def __init__(self, title, current, candidates, intro="", kind="ortho", texture=None, parent=None):
        self.kind, self.current, self.candidates, self.texture_offered = kind, current, candidates, texture
        _Choose.seen.append(self)

    def exec(self):
        from PySide6.QtWidgets import QDialog

        return QDialog.DialogCode.Accepted

    def values(self):
        return {"photos": _Choose.pick(self.candidates), "mode": "assign", "surface": _Choose.surface,
                "texture": bool(_Choose.texture and self.texture_offered is not None)}


def _read(path):
    import rasterio

    with rasterio.open(path) as src:
        return src.read(), src.transform, src.bounds


def test_patch_tool_assigns_a_photo_and_delete_takes_it_away(win, qapp, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from shapely import contains_xy
    from shapely.geometry import Polygon

    from pyarchinit_photogrammetry.gui import main_window as mw

    w = win
    v = w.ortho_view
    ch = w.chunk
    monkeypatch.setattr(mw, "PatchDialog", _Choose)
    _Choose.seen = []
    _Choose.texture = True
    _Choose.surface = "ground"
    before, tr, b = _read(ch.orthomosaic_path)
    w._show_raster("dem")
    w.start_patch()  # the patch tool works on the orthomosaic: the view switches to it
    assert v.kind == "ortho" and v.mode == "patch" and w.draw_actions["patch"].isChecked()
    v.fit()
    qapp.processEvents()

    def vp(fx, fy):
        return v.mapFromScene(v.scene_pt(b.left + fx * (b.right - b.left), b.bottom + fy * (b.top - b.bottom)))

    QTest.mousePress(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vp(0.35, 0.35))
    QTest.mouseMove(v.viewport(), vp(0.5, 0.5))
    QTest.mouseMove(v.viewport(), vp(0.65, 0.6))
    QTest.mouseRelease(v.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, vp(0.65, 0.6))
    w.runner.wait()  # the photos of the area and their previews
    qapp.processEvents()
    w.runner.wait()  # the patch and the update of the orthomosaic
    qapp.processEvents()
    dlg = _Choose.seen[-1]
    assert dlg.kind == "ortho" and len(dlg.candidates) >= 3
    assert all(c["preview"] is not None and c["preview"].shape[2] == 4 for c in dlg.candidates)
    assert all(set(c["previews"]) == {"dem", "ground"} for c in dlg.candidates)
    assert dlg.current is not None and dlg.current.shape == dlg.candidates[0]["preview"].shape
    assert dlg.texture_offered is not None  # the model has a texture: it can be corrected too
    patches = ch.ortho_patches
    assert len(patches) == 1 and patches[0]["photos"] == [dlg.candidates[-1]["label"]]
    assert patches[0]["surface"] == "ground"
    assert ch.meta["orthomosaic"]["applied_patches"] == patches
    assert ch.meta["texture"]["patches"][-1]["photos"] == patches[0]["photos"]
    assert mw.PATCH_LAYER in v.layers and len(v.features(mw.PATCH_LAYER)) == 1
    after, _, _ = _read(ch.orthomosaic_path)
    geom = Polygon(patches[0]["polygon"])
    rows, cols = np.mgrid[0:after.shape[1], 0:after.shape[2]]
    x, y = tr.c + (cols + 0.5) * tr.a, tr.f + (rows + 0.5) * tr.e
    outside = ~contains_xy(geom.buffer(0.01), x, y)
    assert np.array_equal(after[:, outside], before[:, outside])
    assert not np.array_equal(after, before)
    # the patch layer: select it and delete it, the orthomosaic comes back as it was
    w.set_draw_mode("select")
    v.select_items([v.layers[mw.PATCH_LAYER]["features"][0][0]])
    assert w.delete_selected(confirm=False) == 1
    w.runner.wait()
    qapp.processEvents()
    assert ch.ortho_patches == []
    again, _, _ = _read(ch.orthomosaic_path)
    assert np.array_equal(again, before)
    assert not v.features(mw.PATCH_LAYER)


def test_texture_of_faces_chosen_with_the_lasso(win, qapp, monkeypatch):
    from pyarchinit_photogrammetry.gui import main_window as mw
    from pyarchinit_photogrammetry.io.mesh_ply import read_mesh

    w = win
    ch = w.chunk
    monkeypatch.setattr(mw, "PatchDialog", _Choose)
    _Choose.seen = []
    _Choose.pick = staticmethod(lambda cands: [c["label"] for c in cands[:2]])
    w._show_model("model_textured")
    qapp.processEvents()
    mesh = read_mesh(ch.product_path("model"))
    tri = ch.to_world(mesh.xyz)[mesh.faces]
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    up = np.nonzero(n[:, 2] / np.linalg.norm(n, axis=1) > 0.9)[0][:400]
    answers = {"action": "texture", "through": False, "keep_copy": False}
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(lambda cls, *a, **k: dict(answers)))
    mask = np.zeros(len(mesh.faces), bool)
    mask[up] = True
    w._faces_selected(mask, mask)
    w.runner.wait()
    qapp.processEvents()
    w.runner.wait()
    qapp.processEvents()
    dlg = _Choose.seen[-1]
    assert dlg.kind == "faces" and dlg.candidates and dlg.candidates[0]["preview"] is not None
    rec = ch.meta["texture"]["patches"][-1]
    assert rec["faces"] == len(up) and rec["photos"] == sorted(c["label"] for c in dlg.candidates[:2])
    assert len(read_mesh(ch.product_path("model")).faces) == len(mesh.faces)  # nothing deleted
