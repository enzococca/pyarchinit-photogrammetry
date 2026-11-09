"""*Strumenti > Classifica un disegno...*: a layer of free drawings classified along an axis drawn with two clicks, the
plate in the window and saved, the layers per group written as one step of Modifica > Annulla. The chunk of
``test_colour_mask`` (a synthetic origin, no real place)."""
from __future__ import annotations

import os

import numpy as np
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")

from test_colour_mask import X0, Y1, project  # noqa: E402
from test_gui_colour_mask import _action, _window  # noqa: E402
from test_orient_lines import hull_lines  # noqa: E402

from pyarchinit_photogrammetry.core import free_drawings  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _close(w):
    w._close_orient_lines_dialog()
    w.photos.stop()
    w.setWindowModified(False)
    w.close()


def test_dialog_classifies_a_layer_and_writes_layers_undoably(qapp, tmp_path, monkeypatch):
    from pyarchinit_photogrammetry.gui.free_drawing_actions import LAYER_PREFIX

    doc, a = project(tmp_path)
    shift = np.array([X0 + 0.5, Y1 - 1.5])
    for ln in hull_lines():
        free_drawings.add(a, "line", [[x, y, 50.0] for x, y in ln * 0.6 + shift], layer="Rilievo")
    a.meta["elevations"] = [{"id": "e1", "name": "Muro", "kind": "elevation",
                             "frame": {"kind": "plane", "origin": [X0, Y1 - 1.0, 50.0], "u_axis": [1.0, 0.0, 0.0],
                                       "v_axis": [0.0, 0.0, 1.0]}}]
    a.meta["drawings"]["frames"] = {"e1": {"lines": [
        {"id": f"l{i}", "kind": "breakline", "coords": [[0, 0.25 * i, None], [2, 0.25 * i, None]], "attributes": {}}
        for i in range(3)]}}
    doc.save()
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        if w.ortho_view.tr is None:
            w._show_raster("ortho")
            qapp.processEvents()
        _action(w, "Classifica un disegno...").trigger()  # in Strumenti, so in the command search too
        qapp.processEvents()
        dlg = w._orient_lines_dialog
        assert dlg is not None and dlg.isVisible()
        assert not any(b.autoDefault() for b in (dlg.b_classify, dlg.b_save, dlg.b_layers, dlg.b_close))
        assert dlg.set_source("layer", "Rilievo")
        # the axis with two clicks on the plan (a line lent by request_geometry)
        p1, p2 = shift, shift + np.array([3.0, 0.0])
        monkeypatch.setattr(w, "request_geometry", lambda kind, done, cancelled=None, hint="":
                            done(np.array([p1, p2 + [0.0, 1e-9]])) or True)
        assert dlg.start_axis()
        assert np.allclose(np.ravel(dlg.axis()), np.ravel([p1, p2]), atol=1e-3)
        # the advanced parameters: hidden until asked, the defaults
        assert not dlg.w_advanced.isVisible()
        dlg.b_advanced.setChecked(True)
        assert dlg.w_advanced.isVisible()
        assert dlg.params()["pair_max"] == pytest.approx(0.5)
        assert dlg.classify()
        w.runner.wait()
        qapp.processEvents()
        assert not w.said, w.said
        assert dlg.result is not None and dlg.result.counts()["pair_across"] == 24
        assert "coppie di traverso: 24" in dlg.l_result.text()
        assert dlg.l_plate.pixmap() is not None and not dlg.l_plate.pixmap().isNull()
        svg = dlg.save_plate(tmp_path / "tavola.svg")
        assert svg is not None and "<svg" in svg.read_text()
        # the layers: one step of Annulla
        n_steps = len(w.history.entries)
        written = dlg.write_layers()
        qapp.processEvents()
        assert written == {"Classificato - coppie di traverso": 24, "Classificato - lungo l'asse": 4}
        assert len(w.history.entries) == n_steps + 1
        assert LAYER_PREFIX + "Classificato - coppie di traverso" in w.ortho_view.layers
        w.undo()
        qapp.processEvents()
        assert free_drawings.features(w.chunk, "Classificato - coppie di traverso") == []
        w.redo()
        qapp.processEvents()
        assert len(free_drawings.features(w.chunk, "Classificato - coppie di traverso")) == 24
        # an elevation's drawing: along its u axis, no layers on the plan
        assert dlg.set_source("elevation", "e1")
        assert dlg.axis() == ((0.0, 0.0), (1.0, 0.0)) and not dlg.s_axis[0].isEnabled()
        assert dlg.result is None and not dlg.b_save.isEnabled()
        assert dlg.classify()
        w.runner.wait()
        qapp.processEvents()
        assert dlg.result.frame == "e1" and dlg.result.counts()["along"] == 3
        assert dlg.b_save.isEnabled() and not dlg.b_layers.isEnabled()
        # the axis of a plan drawing from the horizontal of the elevation
        dlg.set_source("layer", "Rilievo")
        dlg.c_elev.setCurrentIndex(1)
        dlg._axis_from_elevation()
        assert np.allclose(np.ravel(dlg.axis()), [X0, Y1 - 1.0, X0 + 1.0, Y1 - 1.0])
        # a zero-length axis: said, nothing started
        dlg.set_axis(((1.0, 1.0), (1.0, 1.0)))
        w.said.clear()
        assert not dlg.classify()
        assert any("lunghezza" in t for _k, t in w.said), w.said
        dlg.close()
        qapp.processEvents()
        from pyarchinit_photogrammetry.gui.ties_dialog import alive

        assert not alive(dlg)
    finally:
        _close(w)
