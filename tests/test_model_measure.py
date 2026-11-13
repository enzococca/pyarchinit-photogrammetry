"""Measuring on the 3D view: *Misura distanza* and *Misura area* of the drawing bar measure on the model when the 3D
view is in front (or when there is no orthophoto or DEM), with the clicks of the mouse on the canvas; the distances
against the targets of the synthetic trench, the area of a flat plate against the scene, a point against its
coordinates, Esc, and a chunk without scale said in model units."""
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


def _copy(processed, tmp_path, drop=()):
    """The processed trench copied (georeferenced in EPSG:3004), the products in ``drop`` taken out of it."""
    import pyarchinit_photogrammetry as ppg

    doc0, _ch, _ = processed
    d = tmp_path / "copia"
    d.mkdir()
    shutil.copy2(doc0.path, d / doc0.path.name)
    shutil.copytree(doc0.files_dir, d / doc0.files_dir.name)
    shutil.copytree(doc0.path.parent / "photos", d / "photos")
    doc = ppg.Document.open(d / doc0.path.name)
    for name in drop:
        doc.chunk.products.pop(name, None)
    doc.save()
    return doc


def _window(doc, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui import main_window as mwmod
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    said: list = []
    monkeypatch.setattr(mwmod, "message", lambda *a, **k: said.append(("message", a[2] if len(a) > 2 else "")))
    monkeypatch.setattr(mwmod, "choice", lambda *a, **k: said.append(("choice", a[2] if len(a) > 2 else "")))
    w = MainWindow(doc, autoconnect=False)
    w.said = said
    monkeypatch.setattr(w, "_warn", lambda text: said.append(("warn", text)))
    monkeypatch.setattr(w, "_info", lambda title, text: said.append(("info", text)))
    w.runner.show_dialog = False
    w.resize(1900, 1400)  # a big canvas: a pixel of the view about 2 mm on the trench
    w.show()
    return w


def _close(w) -> None:
    w.photos.stop()
    w.setWindowModified(False)
    w.close()


@pytest.fixture
def win(qapp, processed, tmp_path, monkeypatch):
    w = _window(_copy(processed, tmp_path), tmp_path, monkeypatch)
    qapp.processEvents()
    yield w
    _close(w)


def _model_view(win, qapp, kind="model"):
    mv = win.model_view
    win.show_view(mv)
    mv.set_chunk(win.chunk, kind)
    qapp.processEvents()
    mv.canvas.top_view()
    qapp.processEvents()
    return mv


def _key(mv, key) -> None:
    from PySide6.QtTest import QTest

    QTest.keyClick(mv._draw_target(), key)


def _world_of_pixel(mv, x: float, y: float, z: float) -> np.ndarray:
    """Where the line of sight through the pixel (x, y) meets the horizontal plane at height ``z`` (chunk
    coordinates): the true point of the scene under a click on a flat part."""
    o, d, _metre = mv._sight(x, y)
    a, b = mv.chunk.to_world(np.array([o, o + d]))
    t = (z - a[2]) / (b[2] - a[2])
    return a + t * (b - a)


def test_distance_on_the_model_between_two_targets(win, qapp, scene):
    from free3d_helpers import click_at, screen_of
    from PySide6.QtCore import Qt

    ch = win.chunk
    mv = _model_view(win, qapp)
    assert mv.canvas.width() > 1000
    win.said.clear()
    win.set_draw_mode("measure")  # the 3D view in front: on the model, not refused for the orthophoto
    assert mv.measuring and win.draw_actions["measure"].isChecked()
    assert win.tabs.currentWidget() is mv and not win.said
    a, b = (np.asarray(scene.targets[k], float) for k in (0, 2))  # two opposite corners of the trench, about 3.8 m
    for target in (a, b):
        click_at(mv, *screen_of(mv, ch.to_internal(target[None])[0]))
    assert len(mv.drawing_vertices()) == 2
    assert "3D" in win.statusBar().currentMessage()  # the measure shown while drawing
    _key(mv, Qt.Key.Key_Return)
    qapp.processEvents()
    m = win._model_measure
    assert m is not None and m["kind"] == "line" and m["metric"]
    picked = m["xyz"]
    assert len(picked) == 2
    assert m["values"]["length_3d"] == pytest.approx(float(np.linalg.norm(picked[1] - picked[0])), abs=1e-9)
    assert m["values"]["length_plan"] == pytest.approx(float(np.linalg.norm(picked[1, :2] - picked[0, :2])), abs=1e-9)
    known = float(np.linalg.norm(b - a))
    assert abs(m["values"]["length_3d"] - known) < 0.005, (m["values"]["length_3d"], known)
    assert abs(m["values"]["dz"] - (b[2] - a[2])) < 0.005
    assert m["values"]["length_surface"] > m["values"]["length_3d"]  # across the trench: down and up again
    assert "Distanza sul modello" in m["text"] and "m," in m["text"]
    assert m["text"] in win.statusBar().currentMessage()
    assert mv.last_measure is not None and mv.sd.measure is not None  # drawn until the next one
    assert mv.measuring  # the tool stays on for the next measure
    _key(mv, Qt.Key.Key_Escape)
    assert not mv.measuring and win.draw_actions["pan"].isChecked()
    assert mv.last_measure is not None  # Esc leaves the measure drawn
    win.clear_drawings()
    assert mv.last_measure is None and mv.sd.measure is None


def test_escape_gives_up_the_measure_in_progress(win, qapp, scene):
    from free3d_helpers import click_at, screen_of
    from PySide6.QtCore import Qt

    ch = win.chunk
    mv = _model_view(win, qapp)
    win.set_draw_mode("measure")
    click_at(mv, *screen_of(mv, ch.to_internal(np.asarray(scene.targets[1], float)[None])[0]))
    assert len(mv.drawing_vertices()) == 1
    _key(mv, Qt.Key.Key_Escape)
    qapp.processEvents()
    assert not mv.measuring and win._model_measure is None and mv.last_measure is None
    assert win.draw_actions["pan"].isChecked()
    # another tool of the bar leaves the measure in progress too
    win.set_draw_mode("area")
    assert mv.measuring and mv.drawing_kind == "polygon"
    win.show_view(win.ortho_view)
    win.set_draw_mode("pan")
    assert not mv.measuring


def test_area_of_a_flat_plate_and_its_model_surface(win, qapp, scene):
    from free3d_helpers import click_at, double_click_at, screen_of, wait_for

    ch = win.chunk
    mv = _model_view(win, qapp)
    win.set_draw_mode("area")
    assert mv.measuring and mv.drawing_kind == "polygon"
    t = np.asarray(scene.targets[1], float)  # the plate under a target is flat, at the height of the target
    corners = [t + np.array([dx, dy, 0.0]) for dx, dy in ((-0.06, -0.06), (0.06, -0.06), (0.06, 0.06), (-0.06, 0.06))]
    pixels = [tuple(float(round(c)) for c in screen_of(mv, ch.to_internal(p[None])[0])) for p in corners]
    for x, y in pixels[:-1]:
        click_at(mv, x, y)
    double_click_at(mv, *pixels[-1])  # the last vertex and the end
    assert wait_for(lambda: win._model_measure is not None and win._model_measure["text"] is not None, 20000)
    m = win._model_measure
    assert m["kind"] == "polygon" and len(m["xyz"]) == 4
    truth = np.array([_world_of_pixel(mv, x, y, t[2]) for x, y in pixels])  # the plate under the clicked pixels
    x, y = truth[:, 0] - t[0], truth[:, 1] - t[1]  # near the target: no digits lost to the large coordinates
    known = 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))
    assert known == pytest.approx(0.0144, rel=0.05)
    v = m["values"]
    assert v["area_plan"] == pytest.approx(known, rel=0.02), (v["area_plan"], known)
    assert v["area_plane"] == pytest.approx(known, rel=0.02)
    assert v["model_surface"] == pytest.approx(known, rel=0.05), (v["model_surface"], known)
    assert v["perimeter_plan"] == pytest.approx(0.48, abs=0.01)
    assert "superficie del modello" in m["text"]
    assert ("info", m["text"]) in win.said  # as Misura area on the 2D view: in a box


def test_point_coordinates_on_the_cloud(win, qapp, scene):
    from free3d_helpers import click_at, screen_of

    ch = win.chunk
    mv = _model_view(win, qapp, "point_cloud")
    assert mv.start_measure("point")
    t = np.asarray(scene.targets[3], float)
    click_at(mv, *screen_of(mv, ch.to_internal(t[None])[0]))
    qapp.processEvents()
    m = win._model_measure
    assert m is not None and m["kind"] == "point"
    got = np.array([m["values"][k] for k in "xyz"])
    assert np.linalg.norm(got[:2] - t[:2]) < 0.01 and abs(got[2] - t[2]) < 0.01, got - t
    assert "EPSG:3004" in m["text"] and "Punto sul modello" in m["text"]


def test_measuring_without_orthophoto_and_dem(qapp, processed, tmp_path, monkeypatch, scene):
    from free3d_helpers import click_at, screen_of
    from PySide6.QtCore import Qt

    w = _window(_copy(processed, tmp_path, drop=("dem", "orthomosaic")), tmp_path, monkeypatch)
    try:
        ch = w.chunk
        assert ch.dem_path is None and ch.orthomosaic_path is None
        mv = w.model_view
        mv.set_chunk(ch, "model")
        w.show_view(w.ortho_view)  # the 2D view in front, with nothing to measure on: the model measures
        qapp.processEvents()
        w.said.clear()
        w.set_draw_mode("measure")
        assert not any(kind == "warn" for kind, _t in w.said), w.said
        assert w.tabs.currentWidget() is mv and mv.measuring
        mv.canvas.top_view()
        qapp.processEvents()
        for k in (4, 5):
            click_at(mv, *screen_of(mv, ch.to_internal(np.asarray(scene.targets[k], float)[None])[0]))
        _key(mv, Qt.Key.Key_Return)
        known = float(np.linalg.norm(np.subtract(scene.targets[5], scene.targets[4])))
        assert abs(w._model_measure["values"]["length_3d"] - known) < 0.005
    finally:
        _close(w)


def test_a_chunk_without_scale_says_model_units(qapp):
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.gui import model_measure as mm

    doc = ppg.Document()
    ch = doc.add_chunk("senza scala")
    assert not mm.is_metric(ch)
    v = mm.values("line", np.array([[0, 0, 0], [3, 4, 0]], float))
    assert v["length_3d"] == pytest.approx(5.0)
    text = mm.text("line", v, mm.is_metric(ch))
    assert "unità del modello" in text and " m," not in text
    ch.transform = np.diag([2.0, 2.0, 2.0, 1.0])  # a scale applied: metres
    assert mm.is_metric(ch)
    assert " m," in mm.text("line", v, True)
    imported = doc.add_chunk("modello importato")  # a model imported in metres: local frame, no references
    imported.meta["metric"] = True
    assert mm.is_metric(imported) and " m," in mm.text("line", v, mm.is_metric(imported))


def test_vtk_distance_on_the_model(processed, qapp, monkeypatch, scene):
    """The same distance through the VTK canvas (opt-in, as the other VTK tests of the 3D view)."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest

    from pyarchinit_photogrammetry.gui import model_view as mvm

    monkeypatch.setenv("PPG_RENDERER", "vtk")
    if os.environ.get("PPG_TEST_VTK") != "1":
        pytest.skip("VTK canvas: opt-in with PPG_TEST_VTK=1 (a QtInteractor without an OpenGL context may "
                    "crash on some platforms)")
    if not mvm._vtk_usable():
        pytest.skip("VTK is not usable here")
    from pyarchinit_photogrammetry.gui.gc_guard import install

    collector = install(qapp)
    _doc, chunk, _ = processed
    mv = mvm.ModelView()
    got = []
    mv.measure_drawn.connect(lambda kind, xyz, draped: got.append((kind, xyz, draped)))
    try:
        assert mv.backend == "vtk"
        mv.resize(1100, 800)
        mv.show()
        qapp.processEvents()
        mv.set_chunk(chunk, "model")
        qapp.processEvents()
        mv.canvas.top_view()
        qapp.processEvents()
        assert mv.start_measure("line")
        target = mv._draw_target()
        a, b = (np.asarray(scene.targets[k], float) for k in (0, 2))
        project = mv.canvas.projector()
        for p in (a, b):
            shown = mv.sd.to_display(p[None] - mv.sd.offset)
            px, py, _depth, ok = project(shown)
            assert ok[0]
            x, y = float(px[0]), float(py[0])
            QTest.mouseClick(target, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                             QPoint(int(round(x)), int(round(y))))
        QTest.keyClick(target, Qt.Key.Key_Return)
        qapp.processEvents()
        assert len(got) == 1 and got[0][0] == "line"
        world = chunk.to_world(got[0][1])
        assert abs(float(np.linalg.norm(world[1] - world[0])) - float(np.linalg.norm(b - a))) < 0.01
        assert mv.sd.measure is not None
    finally:
        mv.close()
        collector.stop()
