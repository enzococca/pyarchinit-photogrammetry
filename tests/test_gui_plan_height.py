"""*Pianta e piano...* in the window *Aggancia chunk con legami*: the measure in the worker, its report and the question,
the transform applied as one step of Modifica > Annulla (undone and done again). Synthetic trench only
(``test_plan_height``)."""
from __future__ import annotations

import os

import numpy as np
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")

from test_gui_ties import _close_window, _open, _window  # noqa: E402
from test_plan_height import _surveys  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def scene():
    from pyarchinit_photogrammetry.testing.synthetic import make_scene

    return make_scene(texel=0.004)


def test_plan_and_plane_measured_confirmed_applied_and_undone(qapp, scene, tmp_path, monkeypatch):
    doc, _a, _b, _truth = _surveys(tmp_path / "p", scene)
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        dlg = _open(w, qapp)
        a, b = w.doc.chunks
        before = np.asarray(b.transform, float).copy()
        w.answers.append(1)  # "Non applicare" first
        assert dlg.measure_plan_height(b.key, a.key)
        w.runner.wait()
        qapp.processEvents()
        asked = [x for x in w.said if x[0] == "ask"]
        assert asked and "P5" in asked[-1][1] and np.allclose(np.asarray(b.transform, float), before)
        w.answers.append(0)  # "Applica"
        assert dlg.measure_plan_height(b.key, a.key)
        w.runner.wait()
        qapp.processEvents()
        moved = np.asarray(b.transform, float).copy()
        assert not np.allclose(moved, before) and b.meta["chunk_alignment"]["method"] == "plan_plane"
        w.undo()
        qapp.processEvents()
        assert np.allclose(np.asarray(b.transform, float), before) and "chunk_alignment" not in b.meta
        w.redo()
        qapp.processEvents()
        assert np.allclose(np.asarray(b.transform, float), moved)
    finally:
        _close_window(w)
