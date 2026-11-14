"""The window: models merged into one (Strumenti), the orthophoto of a chunk without photos from its texture, the OBJ
export with groups. Synthetic textured chunks in a local frame."""
from __future__ import annotations

import os

import numpy as np
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")

from engine_gaps_helpers import checker, grid_mesh, new_doc, textured_chunk  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, tmp_path):
    from PySide6.QtCore import QSettings

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path))
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    doc = new_doc(tmp_path)
    xyz, f, uv = grid_mesh(0.0, 0.0, 1.0, 1.0, 10, 10)
    textured_chunk(doc, "A", xyz, f, uv, [checker(128, 128, (200, 50, 50))])
    xyz, f, uv = grid_mesh(0.8, 0.0, 1.8, 1.0, 10, 10)
    textured_chunk(doc, "B", xyz, f, uv, [checker(128, 128, (100, 25, 25))])
    doc.save()
    w = MainWindow(doc, autoconnect=False)
    w.runner.show_dialog = False
    w.resize(1200, 800)
    w.show()
    qapp.processEvents()
    yield w
    w.setWindowModified(False)
    w.close()


def _answers(monkeypatch, values):
    from pyarchinit_photogrammetry.gui import main_window as mw

    def fake(cls, parent, title, fields, intro=""):
        out = {f.name: f.default for f in fields if f.name}
        out.update(values)
        return out

    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(fake))


def test_merge_models_then_orthophoto_and_obj_with_groups(window, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QFileDialog

    w = window
    doc = w.doc
    monkeypatch.setattr(w, "_info", lambda *a, **k: None)
    monkeypatch.setattr(w, "_show_raster", lambda *a: None)
    _answers(monkeypatch, {"chunks": [c.key for c in doc.chunks], "label": "Unito", "colour": "gain",
                           "remove": True, "overlap": 0.01, "pages": "repack", "page_size": 1024, "activate": True})
    w.merge_models()
    w.runner.wait()
    merged = doc.chunks[-1]
    assert merged.label == "Unito" and merged.meta["merge_models"]["removed"]["B"] > 0
    assert w.chunk is merged
    _answers(monkeypatch, {"resolution": 0.01, "supersample": 0, "keep": "replace"})
    w.build_ortho()
    w.runner.wait()
    assert merged.orthomosaic_path is not None and merged.meta["orthomosaic"]["source"] == "texture"
    out = tmp_path / "unito.obj"
    _answers(monkeypatch, {"groups": "source", "max_texture": 256})
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    w.export("model")
    text = out.read_text()
    assert "g A" in text and "g B" in text
    import cv2

    assert max(cv2.imread(str(tmp_path / "unito_texture.jpg")).shape[:2]) <= 256
    assert np.isfinite(merged.scale)
