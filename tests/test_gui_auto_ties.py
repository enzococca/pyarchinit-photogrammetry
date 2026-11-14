"""The automatic ties in the window *Aggancia chunk con legami*: pairs, matching in the worker, the wrong pair in red,
the pairs accepted as ties in one step of Modifica > Annulla, then Controlla and Applica bring B back to its truth;
the steps and their states; a group's chunks moving with it. Synthetic trench only (``test_auto_ties``)."""
from __future__ import annotations

import os

import numpy as np
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")

from test_auto_ties import _errors, _project  # noqa: E402
from test_gui_ties import _check, _close_window, _open, _window  # noqa: E402

from pyarchinit_photogrammetry.core import groups  # noqa: E402
from pyarchinit_photogrammetry.engine import auto_ties, ties  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def scenes():
    from test_auto_ties import _noise_texture, make_scene

    other = make_scene(texel=0.004, seed=99)
    h, w = other.texture.shape[:2]
    other.texture = _noise_texture(h, w, np.random.default_rng(99))
    return make_scene(texel=0.004), other


def test_pairs_matched_accepted_undone_and_applied(qapp, scenes, tmp_path, monkeypatch):
    doc, truth = _project(tmp_path / "p", scenes)
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        dlg = _open(w, qapp)
        auto = dlg.auto
        assert auto.state == {"pairs": "todo", "match": "todo", "accept": "todo", "solve": "todo"}
        assert not auto.b_match.isEnabled() and not auto.b_accept.isEnabled()
        assert auto.find_pairs()
        w.runner.wait()
        qapp.processEvents()
        assert auto.state["pairs"] == "done" and auto.table.rowCount() == 3
        a, b, c = (ch.key for ch in w.doc.chunks)
        assert {(a, b), (a, c)} <= set(auto.checked_pairs())
        assert auto.match()
        w.runner.wait()
        qapp.processEvents()
        assert auto.state["match"] == "done"
        assert auto.results[(a, b)].ok and not auto.results[(a, c)].ok
        assert (a, b) in auto.checked_pairs() and (a, c) not in auto.checked_pairs()
        rows = {auto.table.item(i, 1).text(): i for i in range(auto.table.rowCount())}
        assert auto.table.item(rows["C → A"], 10).foreground().color().name() == "#c0392b"
        auto.table.selectRow(rows["B → A"])
        qapp.processEvents()
        assert "B -> A" in dlg.report.toPlainText()
        n = auto.accept()
        assert n >= auto_ties.MAX_TIES and dlg.table.rowCount() == n and auto.state["accept"] == "done"
        assert auto_ties.records_of(w.doc)
        w.undo()
        qapp.processEvents()
        assert not ties.ties_of(w.doc) and not auto_ties.records_of(w.doc)
        w.redo()
        qapp.processEvents()
        assert len(ties.ties_of(w.doc)) == n and auto_ties.records_of(w.doc)
        kinds = dlg.kinds()
        assert kinds[b] == "similarity"
        dlg.set_kind(c, "")
        _check(w, dlg, qapp)
        assert dlg.result is not None, w.said
        assert dlg.apply()
        qapp.processEvents()
        assert auto.state["solve"] == "done" and auto.state["match"] == "stale"  # B moved: match again if wanted
        _shift, _rot, _ppm, worst = _errors(w.doc, w.doc.chunks[1], truth["B"], scenes[0].origin)
        assert worst < 0.003
    finally:
        _close_window(w)


def test_chunks_of_a_group_get_the_group_unknowns(qapp, scenes, tmp_path, monkeypatch):
    doc, _truth = _project(tmp_path / "p", scenes)
    groups.add_group(doc, "2025", chunks=["B", "C"])
    ties.add_tie(doc, "A", (0, 0, 0), "B", (0, 0, 0))
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        dlg = _open(w, qapp)
        a, b, c = (ch.key for ch in w.doc.chunks)
        kinds = dlg.kinds(include_out=True)
        assert kinds[a] == "fixed" and kinds[b] == "plan" and kinds[c] == ties.GROUP  # C without ties moves with B
        assert dlg.chunks.cellWidget(2, 1).currentText() == "con il gruppo"
    finally:
        _close_window(w)
