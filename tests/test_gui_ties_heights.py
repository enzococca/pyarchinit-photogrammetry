"""Stable heights in *Flusso di lavoro > Aggancia chunk con legami*: measured in the worker and added as one step of
Modifica > Annulla, their residual after a check, shown in amber when the chunks moved apart in plan and measured again
with *Rimisura*. The two synthetic DEMs of ``test_stable_height`` (EPSG:3004 synthetic origin, no real coordinates)."""
from __future__ import annotations

import os

import numpy as np
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")

from PySide6.QtCore import Qt  # noqa: E402
from test_stable_height import BIAS, pit, project  # noqa: E402

from pyarchinit_photogrammetry.engine import stable_height  # noqa: E402

ORANGE = "#b9770e"


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _window(qapp, doc, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui import main_window as mwmod
    from pyarchinit_photogrammetry.gui import ties_dialog
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    for ch in doc.chunks:  # the DEMs built where the chunks are: a later move carries them
        ch.meta.setdefault("built_in", {})["dem"] = np.asarray(ch.transform, float).tolist()
    doc.save()
    said: list = []
    monkeypatch.setattr(mwmod, "message", lambda *a, **k: said.append(("message", a[2] if len(a) > 2 else "")))
    monkeypatch.setattr(mwmod, "choice", lambda *a, **k: said.append(("choice", a[2] if len(a) > 2 else "")))
    monkeypatch.setattr(ties_dialog, "choice", lambda *a, **k: said.append(("ask", a[3] if len(a) > 3 else "")))
    w = MainWindow(doc, autoconnect=False)
    w.said = said
    monkeypatch.setattr(w, "_warn", lambda text: said.append(("warn", text)))
    monkeypatch.setattr(w, "_info", lambda title, text: said.append(("info", text)))
    monkeypatch.setattr(w, "_error", lambda summary, details="", logged=True: said.append(("error", summary)))
    w.runner.show_dialog = False
    w.show()
    qapp.processEvents()
    return w


def _close_window(w):
    w._close_ties_dialog()
    w.photos.stop()
    w.setWindowModified(False)
    w.close()


def _open(w, qapp):
    w.tie_chunks()
    qapp.processEvents()
    dlg = w._ties_dialog
    assert dlg is not None and dlg.isVisible()
    return dlg


def _wait(w, qapp):
    w.runner.wait()
    qapp.processEvents()


def _measure(w, dlg, qapp, **kw):
    ka, kb = w.doc.chunks[0].key, w.doc.chunks[1].key
    assert dlg.measure_height(kb, ka, **kw)
    _wait(w, qapp)


def test_measure_height_adds_a_row_and_undo_removes_it(qapp, tmp_path, monkeypatch):
    doc, _a, _b = project(tmp_path, extra=lambda x, y: -pit(x, y))
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        dlg = _open(w, qapp)
        assert dlg.heights.rowCount() == 0
        steps = len(w.history.entries)
        _measure(w, dlg, qapp)
        assert not [s for s in w.said if s[0] in ("warn", "error")], w.said
        (h,) = stable_height.heights_of(w.doc)
        assert abs(h.dz - BIAS) < 0.003 and h.name == "quota stabile 1"
        assert dlg.heights.rowCount() == 1
        assert dlg.heights.item(0, 0).text() == "quota stabile 1"
        assert dlg.heights.item(0, 1).text() == "B" and dlg.heights.item(0, 2).text() == "A"
        assert dlg.heights.item(0, 3).text() == "0,370"  # Italian numbers
        assert dlg.heights.item(0, 7).checkState() == Qt.CheckState.Checked
        assert len(w.history.entries) == steps + 1
        # B has only a stable height: proposed with its height as unknown
        assert dlg.kinds()[w.doc.chunks[1].key] == "height"

        # switched off and on: one step each
        dlg.heights.item(0, 7).setCheckState(Qt.CheckState.Unchecked)
        qapp.processEvents()
        assert not stable_height.heights_of(w.doc)[0].active and len(w.history.entries) == steps + 2
        w.undo()
        qapp.processEvents()
        assert stable_height.heights_of(w.doc)[0].active

        w.undo()
        qapp.processEvents()
        assert not stable_height.heights_of(w.doc) and "height_ties" not in w.doc.meta
        assert dlg.heights.rowCount() == 0
        w.redo()
        qapp.processEvents()
        assert [x.to_dict() for x in stable_height.heights_of(w.doc)] == [h.to_dict()]

        # removed: one step, undone
        dlg.heights.selectRow(0)
        dlg.remove_selected_heights()
        assert not stable_height.heights_of(w.doc) and dlg.heights.rowCount() == 0
        w.undo()
        qapp.processEvents()
        assert dlg.heights.rowCount() == 1

        # the form: a plane in the layer chosen; the dialog's own question patched
        from pyarchinit_photogrammetry.gui import ties_dialog
        asked = {}

        def answer(parent, title, fields, intro=""):
            asked.update({f.name: f for f in fields})
            return {"chunk": w.doc.chunks[1].key, "reference": w.doc.chunks[0].key, "area": "", "exclude": "",
                    "plane": True}

        monkeypatch.setattr(ties_dialog.ParamDialog, "ask", staticmethod(answer))
        assert dlg.ask_measure()
        _wait(w, qapp)
        assert set(asked) == {"chunk", "reference", "area", "exclude", "plane"}
        assert [x.plane for x in stable_height.heights_of(w.doc)] == [False, True]
        assert dlg.heights.item(1, 6).text() == "sì"
    finally:
        _close_window(w)


def test_measure_error_is_said_and_nothing_added(qapp, tmp_path, monkeypatch):
    doc, _a, _b = project(tmp_path)
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        dlg = _open(w, qapp)
        steps = len(w.history.entries)
        _measure(w, dlg, qapp, layer="Nessuno")
        assert [s for s in w.said if s[0] == "warn" and "Quota stabile" in s[1]]
        assert not stable_height.heights_of(w.doc) and len(w.history.entries) == steps
        assert dlg.b_measure_height.isEnabled()
    finally:
        _close_window(w)


def test_check_lists_the_height_tie_residual(qapp, tmp_path, monkeypatch):
    doc, _a, _b = project(tmp_path)
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        ka, kb = w.doc.chunks[0].key, w.doc.chunks[1].key
        dlg = _open(w, qapp)
        _measure(w, dlg, qapp)
        dlg.set_kind(ka, "fixed")
        dlg.set_kind(kb, "height")
        assert dlg.run_check()
        _wait(w, qapp)
        assert dlg.result is not None, dlg.report.toPlainText()
        heights = dlg.result["report"]["heights"]
        assert len(heights) == 1
        assert dlg.heights.item(0, 8).text() != ""
        text = dlg.report.toPlainText()
        assert "Quote stabili" in text and "quota stabile 1 (B su A)" in text
        # a change of the heights makes the check stale
        dlg.heights.item(0, 7).setCheckState(Qt.CheckState.Unchecked)
        qapp.processEvents()
        assert dlg.result is None and dlg.heights.item(0, 8).text() == ""
    finally:
        _close_window(w)


def test_remeasure_after_apply_turns_the_row_back(qapp, tmp_path, monkeypatch):
    doc, _a, _b = project(tmp_path)
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        dlg = _open(w, qapp)
        _measure(w, dlg, qapp)
        (h,) = stable_height.heights_of(w.doc)
        assert not dlg.height_stale(h.id)
        # a solve applied moved B 0.5 m east (its DEM with it): the stable height was measured elsewhere
        b = w.doc.chunks[1]
        moved = np.asarray(b.transform, float).copy()
        moved[0, 3] += 0.5
        b.transform = moved
        w._ties_shown(False)
        qapp.processEvents()
        dlg = w._ties_dialog
        assert dlg.height_stale(h.id)
        assert dlg.heights.item(0, 0).foreground().color().name() == ORANGE
        assert "da rimisurare" in dlg.heights.item(0, 9).text()
        steps = len(w.history.entries)

        assert dlg.remeasure_selected()  # none selected: the ones to be measured again
        _wait(w, qapp)
        assert not [s for s in w.said if s[0] in ("warn", "error")], w.said
        (again,) = stable_height.heights_of(w.doc)
        assert again.id == h.id and again.name == h.name and again.placed["b"] == moved.tolist()
        assert not dlg.height_stale(h.id) and dlg.heights.item(0, 0).foreground().color().name() != ORANGE
        assert len(w.history.entries) == steps + 1

        w.undo()
        qapp.processEvents()
        assert stable_height.heights_of(w.doc)[0].to_dict() == h.to_dict()
        assert w._ties_dialog.height_stale(h.id)
    finally:
        _close_window(w)


def test_amber_of_a_check_by_id_and_gone_with_the_check(qapp, tmp_path, monkeypatch):
    """A check whose solve would move a height's chunks apart in plan turns that row amber (by its id: another height
    with the same name and chunks stays as it is), says why, and the amber goes with the check; *Rimisura* with
    nothing selected does not take it (nothing moved yet)."""
    doc, _a, _b = project(tmp_path)
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        dlg = _open(w, qapp)
        _measure(w, dlg, qapp)
        _measure(w, dlg, qapp)
        first, second = stable_height.heights_of(w.doc)
        twin = stable_height.heights_of(w.doc)
        twin[1].name = first.name  # the same name and chunks (a CLI or an appended project may give that)
        stable_height.save_heights(w.doc, twin)
        dlg.reload()
        rep = {"heights": [
            {"id": first.id, "dz": 0.001, "fitted": True, "normalised": 0.5, "horizontal": 0.5},
            {"id": second.id, "dz": 0.001, "fitted": True, "normalised": 0.5, "horizontal": 0.0}],
            "messages": []}
        dlg._filling = True
        try:
            dlg._checked_heights(rep, {})
        finally:
            dlg._filling = False
        r1, r2 = dlg.height_row_of(first.id), dlg.height_row_of(second.id)
        assert dlg.height_predicted(first.id) and not dlg.height_predicted(second.id)
        assert not dlg.height_stale(first.id)  # nothing moved yet
        assert dlg.heights.item(r1, 0).foreground().color().name() == ORANGE
        assert dlg.heights.item(r2, 0).foreground().color().name() != ORANGE
        assert "dopo Applica" in dlg.heights.item(r1, 9).text()
        assert dlg.heights.item(r1, 8).text() == "0,001"

        steps = len(w.history.entries)
        assert not dlg.remeasure_selected()  # none selected and none moved: nothing to measure again
        assert len(w.history.entries) == steps

        dlg.result = {"report": {}}  # a check held: invalidate drops it and what it wrote
        dlg.invalidate()
        assert not dlg.height_predicted(first.id)
        assert dlg.heights.item(r1, 0).foreground().color().name() != ORANGE
        assert "dopo Applica" not in dlg.heights.item(r1, 9).text() and dlg.heights.item(r1, 8).text() == ""
    finally:
        _close_window(w)
