"""*Strumenti > Livello dell'acqua e rifrazione...*: the level from a layer of polygons or a colour range measured in
the worker, shown, saved and drawn on the view; the level typed and undone; the correction of the dense cloud as a
version. The synthetic pond of ``test_water`` (a synthetic origin, no real place)."""
from __future__ import annotations

import os

import numpy as np
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")

from shapely.geometry import Point  # noqa: E402
from test_water import LEVEL, RADIUS, X0, Y0, project  # noqa: E402

from pyarchinit_photogrammetry.core import free_drawings, versions  # noqa: E402
from pyarchinit_photogrammetry.engine import water  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _window(qapp, doc, tmp_path, monkeypatch):
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
    monkeypatch.setattr(w, "_error", lambda summary, details="", logged=True: said.append(("error", summary)))
    w.runner.show_dialog = False
    w.show()
    qapp.processEvents()
    if w.ortho_view.tr is None:
        w._show_raster("ortho")
        qapp.processEvents()
    return w


def _close(w):
    w._close_water_dialog()
    w._close_colour_mask_dialog()
    w.photos.stop()
    w.setWindowModified(False)
    w.close()


def _open(w, qapp):
    from pyarchinit_photogrammetry.gui.command_search import clean, menu_commands

    act = next(a for a in menu_commands(w.menuBar()).values()
               if clean(a.text()) == clean("Livello dell'acqua e rifrazione..."))
    act.trigger()  # in Strumenti, so in the command search too
    qapp.processEvents()
    dlg = w._water_dialog
    assert dlg is not None and dlg.isVisible()
    return dlg


def _wait(w, qapp):
    w.runner.wait()
    qapp.processEvents()


def _water_items(w):
    from PySide6.QtCore import Qt

    out = []
    stack = [w.workspace.topLevelItem(i) for i in range(w.workspace.topLevelItemCount())]
    while stack:
        it = stack.pop()
        data = it.data(0, Qt.ItemDataRole.UserRole)
        if data and data[0] == "water":
            out.append(it.text(0))
        stack.extend(it.child(i) for i in range(it.childCount()))
    return out


def _pond_layer(a, name="Acqua"):
    ring = np.asarray(Point(X0, Y0).buffer(RADIUS, quad_segs=64).exterior.coords, float)[:-1]
    world = np.column_stack([ring, np.full(len(ring), LEVEL)])
    free_drawings.add(a, "polygon", world, layer=name)  # free drawings are in world coordinates


def test_level_from_a_layer_is_shown_and_saved(qapp, tmp_path, monkeypatch):
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.gui.water_dialog import EDGE_LAYER

    doc, a = project(tmp_path)
    _pond_layer(a)
    doc.save()
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        dlg = _open(w, qapp)
        assert dlg.r_edge.isChecked() and dlg.r_layer.isChecked()
        assert [dlg.c_layer.itemText(i) for i in range(dlg.c_layer.count())] == ["Acqua"]
        assert "Nessun livello" in dlg.l_level.text()
        assert not dlg.b_correct.isEnabled()  # no level yet
        dlg.c_n.setCurrentIndex(dlg.c_n.findData("fresh"))
        assert dlg.n() == pytest.approx(water.N_FRESH) and not dlg.s_n.isEnabled()
        n_steps = len(w.history.entries)
        assert dlg.measure()
        _wait(w, qapp)
        assert not w.said, w.said
        rec = w.chunk.meta["water"]
        assert rec["source"] == "edge" and rec["level"] == pytest.approx(LEVEL, abs=0.002)
        assert rec["n"] == pytest.approx(water.N_FRESH) and rec["edge_cells"] > 1000
        assert len(w.history.entries) == n_steps + 1  # one step of Annulla
        text = dlg.l_level.text()
        assert "0,80" in text and "dal bordo dell'acqua" in text and "dal 10 al 90 %" in text and "n 1,33" in text
        assert EDGE_LAYER in w.ortho_view.layers  # the heights of the bank, drawn
        assert _water_items(w) == [f"Acqua: livello {LEVEL:.3f} m, n 1,33".replace(".", ",")]
        assert dlg.b_correct.isEnabled()
        # saved with the project
        assert w.save_document()
        again = ppg.Document.open(tmp_path / "p.ppg")
        assert again.chunks[0].meta["water"]["level"] == pytest.approx(rec["level"])
        # the colour range: sampled on the water, the level within a centimetre (the outline of a 2 cm mask)
        monkeypatch.setattr(w, "request_geometry", lambda kind, done, cancelled=None, hint="":
                            done(np.array([[X0 + 0.5, Y0]])) or True)
        assert dlg.start_sample()
        assert dlg.r_colour.isChecked()
        rng = dlg.range()
        assert 170 < (rng.hue[0] + rng.hue[1]) / 2 < 210
        assert dlg.measure()
        _wait(w, qapp)
        assert not w.said, w.said
        assert w.chunk.meta["water"]["level"] == pytest.approx(LEVEL, abs=0.01)
        assert len(w.history.entries) == n_steps + 2
        # no water of those colours: said, nothing written
        dlg.r_hue.set_values(60, 80)
        before = dict(w.chunk.meta["water"])
        assert dlg.measure()
        _wait(w, qapp)
        assert w.said and w.said[-1][0] == "warn" and "Livello dell'acqua" in w.said[-1][1]
        assert w.chunk.meta["water"] == before and len(w.history.entries) == n_steps + 2
        dlg.close()
        qapp.processEvents()
        assert EDGE_LAYER not in w.ortho_view.layers
    finally:
        _close(w)


def test_hand_level_and_undo(qapp, tmp_path, monkeypatch):
    doc, a = project(tmp_path)
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        dlg = _open(w, qapp)
        assert dlg.r_colour.isChecked()  # no layer of polygons: the colours
        dlg.r_hand.setChecked(True)
        assert dlg.hand_box.isEnabled() and not dlg.edge_box.isEnabled()
        dlg.c_n.setCurrentIndex(dlg.c_n.findData("other"))
        assert dlg.s_n.isEnabled()
        dlg.s_n.setValue(1.5)
        dlg.s_level.setValue(0.5)
        n_steps = len(w.history.entries)
        assert dlg.set_by_hand()
        rec = w.chunk.meta["water"]
        assert rec["level"] == pytest.approx(0.5) and rec["source"] == "hand" and rec["n"] == pytest.approx(1.5)
        assert len(w.history.entries) == n_steps + 1
        assert "a mano" in dlg.l_level.text()
        assert _water_items(w) == ["Acqua: livello 0,500 m, n 1,50"]
        dlg.s_level.setValue(0.6)
        assert dlg.set_by_hand()
        assert len(w.history.entries) == n_steps + 2
        w.undo()
        qapp.processEvents()
        assert w.chunk.meta["water"]["level"] == pytest.approx(0.5)
        assert dlg.s_level.value() == pytest.approx(0.5)  # the window follows
        w.undo()
        qapp.processEvents()
        assert "water" not in w.chunk.meta
        assert _water_items(w) == []
        assert "Nessun livello" in dlg.l_level.text() and not dlg.b_correct.isEnabled()
        w.redo()
        qapp.processEvents()
        assert w.chunk.meta["water"]["level"] == pytest.approx(0.5)
        assert _water_items(w) == ["Acqua: livello 0,500 m, n 1,50"]
        # the double click on the tree opens the window
        dlg.close()
        qapp.processEvents()
        w._workspace_item("water", w.chunk, None)
        qapp.processEvents()
        assert w._water_dialog is not None and w._water_dialog is not dlg
    finally:
        _close(w)


def test_correct_runs_and_adds_a_version(qapp, tmp_path, monkeypatch):
    doc, a = project(tmp_path)
    before = a.point_cloud
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        dlg = _open(w, qapp)
        dlg.r_hand.setChecked(True)
        dlg.s_level.setValue(LEVEL)
        assert dlg.set_by_hand()  # the sea by default
        assert dlg.b_correct.isEnabled()
        dlg.c_dem.setChecked(True)
        n_steps = len(w.history.entries)
        assert dlg.correct()
        _wait(w, qapp)
        assert not w.said, w.said
        ch = w.chunk
        refr = ch.meta["dense"]["refraction"]
        assert refr["n"] == pytest.approx(water.N_SEA) and refr["below"] > 0
        kept = versions.versions(ch, "point_cloud")
        assert [e["id"] for e in kept] == [refr["version"]]
        assert versions.active_info(ch, "point_cloud")["name"] == "Nuvola corretta per la rifrazione (n 1,34)"
        assert len(versions.versions(ch, "dem")) == 1  # the DEM built again, as a new version
        world0, world1 = ch.to_world(before.xyz), ch.to_world(ch.point_cloud.xyz)
        assert np.allclose(world1[:, 2], water.corrected(world0[:, 2], LEVEL, water.N_SEA), atol=1e-9)
        assert len(w.history.entries) == n_steps  # a build: undone by the versions, not by Annulla
        assert not dlg.b_correct.isEnabled() and "già corretta" in dlg.l_correct.text()
        # the cloud before made active again: it can be corrected again
        versions.activate(ch, "point_cloud", refr["version"])
        w.refresh_all()
        qapp.processEvents()
        assert dlg.b_correct.isEnabled() and "già corretta" not in dlg.l_correct.text()
        # a level above no point: said, nothing written
        dlg.s_level.setValue(-100.0)
        assert dlg.set_by_hand()
        dlg.c_dem.setChecked(False)
        assert dlg.correct()
        _wait(w, qapp)
        assert w.said and w.said[-1][0] == "warn" and "niente da correggere" in w.said[-1][1]
        assert len(versions.versions(ch, "point_cloud")) == 1
    finally:
        _close(w)
