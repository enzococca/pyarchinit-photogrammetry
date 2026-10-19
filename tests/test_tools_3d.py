"""Sections, plan slices and markers placed in the 3D view."""
from __future__ import annotations

import shutil

import numpy as np
import pytest


@pytest.fixture
def window(processed, tmp_path, monkeypatch):
    import os

    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    import pyarchinit_photogrammetry as ppg

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui import main_window as mw

    doc, _ch, _ = processed
    d = tmp_path / "copia"
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")
    w = mw.MainWindow(ppg.Document.open(d / doc.path.name), autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    yield w, app, mw
    w.setWindowModified(False)
    w.close()
    app.processEvents()


def test_section_and_slice_from_the_3d_view(window, monkeypatch):
    w, app, mw = window
    ch = w.chunk
    w._show_model("model")
    mv = w.model_view
    pts = ch.tie_points.xyz
    a, b = pts[np.argmin(pts[:, 0])], pts[np.argmax(pts[:, 0])]
    # two clicks: the first point is kept and the pick starts again for the second
    picked = iter([a, b])
    monkeypatch.setattr(mv, "pick_point", lambda *args, **kw: next(picked))
    got = []
    mv.section_requested.connect(lambda p, q: got.append((p, q)))
    mv.start_pick("section")
    mv._pick_finished((10, 10))
    assert mv._purpose == "section_end" and mv.stack.currentWidget() is mv.picker
    mv._pick_finished((20, 10))
    app.processEvents()
    w.runner.wait()
    app.processEvents()
    assert got and np.allclose(got[0][0], a) and len(w.sections) == 1
    # a plan slice at the height of a picked point: the middle of the tie points, which the plane cuts
    # (an extreme one may lie above or below the model, whose edges change with the meshing)
    c = pts[np.argmin(np.linalg.norm(pts - np.median(pts, axis=0), axis=1))]
    asked = []
    z = float(ch.to_world(c[None])[0, 2])

    def ask(cls, parent, title, fields, intro=""):
        asked.append(fields[0].default)
        return {"z": fields[0].default}

    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(ask))
    mv.slice_requested.emit(c)
    w.runner.wait()
    app.processEvents()
    assert asked and asked[0] == pytest.approx(z, abs=1e-3)
    assert any(name.startswith("Pianta a quota") for name in w.line_layers)
    # a plane above the model cuts nothing: said, and no empty drawing written
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(lambda cls, *a, **k: {"z": z + 100.0}))
    w.plan_slice()
    w.runner.wait()
    app.processEvents()
    assert "non taglia il modello" in w.statusBar().currentMessage()
    assert not (w.doc.path.parent / f"pianta_{z + 100.0:.3f}.dxf").exists()


def test_marker_moved_in_3d(window, monkeypatch):
    w, app, mw = window
    ch = w.chunk
    m = ch.add_marker("Nuovo", position=ch.tie_points.xyz[0])
    w._dirty(True)  # placed: an entry of the history
    w.move_marker_in_3d(m.key)
    assert w.model_view._purpose == f"move:{m.key}"
    target = ch.tie_points.xyz[100]
    w.model_view.move_marker_requested.emit(m.key, target)
    assert np.allclose(m.position, target) and m.projections  # blue flags where the photos see it now
    assert not any(p.measured for p in m.projections.values())
    # a marker measured in the photos is moved in the photos, not here
    warned = []
    monkeypatch.setattr(w, "_warn", lambda text: warned.append(text))
    w.move_marker_in_3d(ch.markers[0].key)
    assert warned and "misurato nelle foto" in warned[0]
    w.undo()
    assert not np.allclose(ch.marker_by_key(m.key).position, target)
