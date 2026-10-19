"""Diagnostics of the alignment, camera calibration, merging markers."""
from __future__ import annotations

import shutil

import numpy as np
import pytest

from pyarchinit_photogrammetry.core.camera import Sensor
from pyarchinit_photogrammetry.engine import diagnostics


def _copy(processed, tmp_path):
    import pyarchinit_photogrammetry as ppg

    doc, _ch, _ = processed
    d = tmp_path / "copia"
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")
    out = ppg.Document.open(d / doc.path.name)
    return out, out.chunk


def test_matches_and_residuals(processed, tmp_path):
    _doc, ch = _copy(processed, tmp_path)
    graph = diagnostics.match_graph(ch)
    keys = [c.key for c in ch.cameras]
    assert graph and all(a < b for a, b in graph)
    assert diagnostics.components(keys, graph) == [sorted(keys, key=keys.index)] or \
        len(diagnostics.components(keys, graph)) == 1
    (a, b), n = max(graph.items(), key=lambda kv: kv[1])
    m = diagnostics.pair_matches(ch, a, b)
    assert len(m["verified"]) == n and len(m["candidates"]) >= n
    assert m["verified"][:, 0].max() < len(m["a"]) and m["verified"][:, 1].max() < len(m["b"])
    reversed_ = diagnostics.pair_matches(ch, b, a)  # the same pair from the other side
    assert np.array_equal(reversed_["verified"], m["verified"][:, ::-1])
    xa, xb = diagnostics.shared_tie_points(ch, a, b)
    assert len(xa) == len(xb) > 20
    measured, projected = diagnostics.residuals(ch, a)
    err = np.hypot(*(projected - measured).T)
    assert len(err) > 50 and np.mean(err) < 1.0
    # a photo cut off from the others
    graph2 = {k: v for k, v in graph.items() if keys[0] not in k}
    groups = diagnostics.components(keys, graph2)
    assert len(groups) == 2 and groups[1] == [keys[0]]


def test_distortion_and_models():
    from pyarchinit_photogrammetry.gui.calibration_dialog import distortion_field, parameter_names

    s = Sensor(key=0, label="x", width=1200, height=900, model="OPENCV",
               params=[1000.0, 1000.0, 600.0, 450.0, -0.04, 0.0, 0.0, 0.0])
    ideal, real = distortion_field(s)
    d = np.hypot(*(real - ideal).T)
    corner = np.argmax(np.hypot(ideal[:, 0] - 600, ideal[:, 1] - 450))
    assert d.min() < 1e-6 and d[corner] == pytest.approx(d.max()) and d.max() > 10  # barrel: corners pulled in
    assert np.hypot(*(real[corner] - [600, 450])) < np.hypot(*(ideal[corner] - [600, 450]))
    assert parameter_names(s) == ["fx", "fy", "cx", "cy", "k1", "k2", "p1", "p2"]
    assert s.converted("PINHOLE") == [1000.0, 1000.0, 600.0, 450.0]
    assert s.converted("RADIAL_FISHEYE") == [1000.0, 600.0, 450.0, 0.0, 0.0]
    pin = Sensor(key=1, label="y", width=1200, height=900, model="PINHOLE", params=s.converted("PINHOLE"))
    ideal, real = distortion_field(pin)
    assert np.allclose(real, ideal)


def test_merge_and_remove_markers(processed, tmp_path):
    _doc, ch = _copy(processed, tmp_path)
    a, b = ch.markers[0], ch.markers[1]
    manual = ch.add_marker("GCP A")
    cam = next(iter(a.projections))
    manual.projections[cam] = type(a.projections[cam])(a.projections[cam].x + 1.0, a.projections[cam].y, True)
    manual.reference.location = None
    other = next(k for k in b.projections if k not in a.projections) if set(b.projections) - set(a.projections) \
        else None
    bar = ch.add_scalebar(manual, b, 1.0)
    keep = ch.merge_markers([a, manual])
    assert keep is a and manual not in ch.markers
    assert a.projections[cam].pinned is True  # placed by hand wins over the detection
    assert ch.scalebars[0].marker_a == a.key and bar.marker_b == b.key
    assert a.position is not None and a.reference.location
    ch.remove_markers([b.key])
    assert b not in ch.markers and not ch.scalebars  # the bar ended at it
    with pytest.raises(ValueError):
        ch.merge_markers([a])
    assert other is None or other not in a.projections


def test_held_calibration_is_not_refined(processed, tmp_path):
    _doc, ch = _copy(processed, tmp_path)
    (sensor,) = ch.sensors.values()
    sensor.params = list(sensor.params)
    sensor.params[4] = -0.02  # k1 of the photos is -0.04: held at -0.02 it stays there
    held = list(sensor.params)
    sensor.fixed = True
    ch.match_photos(quality="medium")
    ch.align_cameras()
    assert ch.sensors[sensor.key].params == pytest.approx(held)
    ch.optimize_cameras()
    assert ch.sensors[sensor.key].params == pytest.approx(held)
    sensor.fixed = False
    ch.optimize_cameras()
    assert ch.sensors[sensor.key].params[4] == pytest.approx(-0.04, abs=0.005)


def test_interface(processed, tmp_path, monkeypatch):
    import os

    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui import main_window as mw
    from pyarchinit_photogrammetry.gui.calibration_dialog import CalibrationDialog
    from pyarchinit_photogrammetry.gui.matches_dialog import MatchGraphDialog, PairMatchesDialog

    doc, ch = _copy(processed, tmp_path)
    g = MatchGraphDialog(ch)
    assert "28 foto" in g.info.text() and g.view.scene().items()
    g.min_box.setValue(100000)
    assert "foto isolate (28)" in g.info.text()
    (a, b), _n = max(g.graph.items(), key=lambda kv: kv[1])
    pm = PairMatchesDialog(ch, a, b)
    assert pm.images[0][0] is not None and "Verificate" in pm.c_ver.text()
    pm.c_rej.setChecked(True)
    dlg = CalibrationDialog(ch)
    dlg.model.setCurrentIndex(dlg.model.findData("OPENCV_FISHEYE"))
    assert dlg.table.item(4, 0).text() == "k1" and float(dlg.table.item(4, 1).text()) == 0.0
    dlg.fixed.setChecked(True)
    assert dlg.apply() and dlg.changed
    (sensor,) = ch.sensors.values()
    assert sensor.model == "OPENCV_FISHEYE" and sensor.fixed
    for d in (g, pm, dlg):
        d.deleteLater()
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    w.a_residuals.setChecked(True)
    w.open_photo(w.chunk.cameras[0])
    assert w.photo_view.residual_items and "residuo medio" in w.statusBar().currentMessage()
    w.a_residuals.setChecked(False)
    assert not w.photo_view.residual_items
    k0, k1 = w.chunk.markers[0].key, w.chunk.markers[1].key
    w.reference.merge_requested.emit([k0, k1])
    assert len(w.chunk.markers) == 7 and w.history.undo_text() == "Marker"
    monkeypatch.setattr(mw, "choice", lambda *a, **k: 0)
    w.reference.remove_requested.emit([k0])
    assert len(w.chunk.markers) == 6
    w.undo()
    w.undo()
    assert len(w.chunk.markers) == 8
    w.setWindowModified(False)
    w.close()
    app.processEvents()
