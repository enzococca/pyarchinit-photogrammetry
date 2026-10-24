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


def test_vertical_exaggeration_changes_only_the_view(window):
    from PySide6.QtTest import QTest

    from pyarchinit_photogrammetry.gui.model_view import exaggeration_text

    w, app, mw = window
    ch = w.chunk
    w._show_model("model")
    mv = w.model_view
    true_vertices = np.array(mv.base_sd.mesh_vertices, float)
    mv.set_exaggeration(3.0)
    sd = mv.sd
    assert sd.exaggeration == 3.0 and mv.exag_spin.value() == 3.0 and mv.canvas.sd is sd
    up = sd.up / np.linalg.norm(sd.up)
    shown = np.array(sd.mesh_vertices, float)
    h_true, h_shown = (true_vertices - sd.pivot) @ up, (shown - sd.pivot) @ up
    assert np.ptp(h_true) > 0.05 and np.allclose(h_shown, 3 * h_true, atol=1e-4)
    assert np.allclose(shown - np.outer(h_shown, up), true_vertices - np.outer(h_true, up), atol=1e-4)
    assert np.allclose(sd.from_display(shown), true_vertices, atol=1e-4)
    assert exaggeration_text(3.0) == "Esagerazione verticale x3"
    # a click gives a true point of the model: shown again, it falls under the cursor
    image, project = mv.canvas.snapshot()
    x, y, _depth, ok = project(shown)
    i = int(np.argmin(np.where(ok, (x - mv.canvas.width() / 2) ** 2 + (y - mv.canvas.height() / 2) ** 2, np.inf)))
    for ray in (None, mv.canvas.view_ray):
        picked = mv.pick_point(float(x[i]), float(y[i]), project, ray)
        back = sd.to_display(ch.to_world(picked[None]) - sd.offset)
        px, py, _, _ = project(back)
        assert np.hypot(px[0] - x[i], py[0] - y[i]) < 10

    # seen from the side (100 px per metre, heights up the screen) the exaggeration moves things a lot
    def side(pts):
        pts = np.asarray(pts, float)
        return pts[:, 0] * 100, -((pts - sd.pivot) @ up) * 100, pts[:, 1] + 100, np.ones(len(pts), bool)

    j = int(np.argmax(np.abs(h_true)))
    sx, sy, _, _ = side(shown[j][None])
    picked = mv.pick_point(float(sx[0]), float(sy[0]), side, reach=0.5)
    assert np.allclose(picked, ch.to_internal(true_vertices[j][None] + sd.offset)[0], atol=1e-4)
    # the lasso takes the faces where they are shown
    v, f = np.asarray(ch.model.vertices, float), np.asarray(ch.model.faces)
    true_centres = ch.to_world(v[f].mean(axis=1)) - sd.offset
    k = int(np.argmax(np.abs((true_centres - sd.pivot) @ up)))
    cx, cy, _, _ = side(sd.to_display(true_centres[k][None]))
    square = [(cx[0] - 6, cy[0] - 6), (cx[0] + 6, cy[0] - 6), (cx[0] + 6, cy[0] + 6), (cx[0] - 6, cy[0] + 6)]
    _visible, inside = mv.select_faces(square, side)
    tx, ty, _, _ = side(true_centres[k][None])
    assert inside[k] and np.hypot(tx[0] - cx[0], ty[0] - cy[0]) > 12  # not where the true face would be
    # a refresh keeps it; the spin box changes it (once the arrows stop); 1 = the true scene
    mv.cb_cams.toggle()
    assert mv.sd.exaggeration == 3.0
    mv.exag_spin.setValue(2.0)
    QTest.qWait(400)
    assert mv.sd.exaggeration == 2.0
    mv.set_exaggeration(1.0)
    assert np.allclose(np.array(mv.sd.mesh_vertices, float), true_vertices)
