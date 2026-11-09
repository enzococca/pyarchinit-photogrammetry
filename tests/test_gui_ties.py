"""Flusso di lavoro > Aggancia chunk con legami: the ties of the project in the dialog, the unknowns per chunk, the
check in the worker with the report that names a wrong tie, the apply as one step of Modifica > Annulla (undone and
done again exactly), the separate question without redundancy, a solve refused said in the window's words, a broken
tie shown with why. Synthetic surveys of the trench of ``test_ties_solve`` only (no real coordinates)."""
from __future__ import annotations

import os

import numpy as np
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")

from PySide6.QtCore import Qt  # noqa: E402
from test_ties_solve import AB, _close, _three, _tie, _world  # noqa: E402

from pyarchinit_photogrammetry.engine import ties  # noqa: E402

FIVE = AB + [(3.0, 1.0)]


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def scene():
    from pyarchinit_photogrammetry.testing.synthetic import make_scene

    return make_scene(texel=0.01)


def _window(qapp, doc, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui import main_window as mwmod
    from pyarchinit_photogrammetry.gui import ties_dialog
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    doc.save(tmp_path / "ties.ppg")
    said: list = []
    monkeypatch.setattr(mwmod, "message", lambda *a, **k: said.append(("message", a[2] if len(a) > 2 else "")))
    monkeypatch.setattr(mwmod, "choice", lambda *a, **k: said.append(("choice", a[2] if len(a) > 2 else "")))
    answers: list = []

    def asked(parent, kind, title, text, options, default=0, cancel="Annulla"):
        said.append(("ask", text, default))
        return answers.pop(0) if answers else None

    monkeypatch.setattr(ties_dialog, "choice", asked)
    w = MainWindow(doc, autoconnect=False)
    w.said, w.answers = said, answers
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


def _check(w, dlg, qapp):
    assert dlg.run_check()
    w.runner.wait()
    qapp.processEvents()


def _red(item) -> bool:
    return item.foreground().color().name() == "#c0392b"


def test_check_names_a_wrong_tie_then_apply_and_undo(qapp, scene, tmp_path, monkeypatch):
    doc, a, b, c = _three(scene)
    for xy in FIVE[:4]:
        _tie(doc, a, b, _world(scene, xy))
    w5 = _world(scene, FIVE[4])[0]
    wrong = ties.add_tie(doc, a.chunk, a.internal(w5)[0], b.chunk, b.internal(w5 + np.array([1.0, 0.0, 0.0]))[0],
                         name="sbagliato")
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        doc = w.doc
        ka, kb, kc = (ch.key for ch in doc.chunks)
        dlg = _open(w, qapp)
        assert dlg.table.rowCount() == 5
        kinds = dlg.kinds(include_out=True)
        assert kinds[kb] == "similarity" and kinds[kc] == ""  # C has no ties: out of the solve
        assert w.chunk is doc.chunks[0] and kinds[ka] == "fixed"  # the active chunk fixed
        dlg.set_kind(ka, "fixed")
        dlg.set_kind(kb, "similarity")
        assert not dlg.b_apply.isEnabled()

        _check(w, dlg, qapp)
        assert dlg.result is not None and dlg.b_apply.isEnabled()
        rep = dlg.result["report"]
        assert wrong.id in rep["suspects"]
        row = dlg.row_of(wrong.id)
        assert _red(dlg.table.item(row, 1))
        text = dlg.report.toPlainText()
        assert "sbagliato" in text and "probabilmente sbagliato" in text
        assert dlg.table.item(row, 5).text()  # its residual

        # the wrong tie switched off: the check no longer holds, a new one is clean
        dlg.table.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
        qapp.processEvents()
        assert not ties.tie_by_id(w.doc, wrong.id).active
        assert dlg.result is None and not dlg.b_apply.isEnabled()
        _check(w, dlg, qapp)
        assert dlg.result["report"]["suspects"] == []
        assert "non attivo" in dlg.table.item(row, 6).text()

        before = {ch.key: (np.asarray(ch.transform, float).copy(), ch.crs) for ch in w.doc.chunks}
        ties_before = [t.to_dict() for t in ties.ties_of(w.doc)]
        markers_before = {ch.key: len(ch.markers) for ch in w.doc.chunks}
        assert dlg.apply()
        qapp.processEvents()
        _close(w.doc.chunks[1].transform, b.truth, atol=1e-4)
        np.testing.assert_array_equal(w.doc.chunks[0].transform, before[ka][0])
        assert wrong.id not in w.doc.meta["ties_report"]["checks"]["suspects"]  # the report of the checks kept
        assert not [s for s in w.said if s[0] == "ask"]  # with redundancy: no question
        assert w.history.can_undo()

        w.undo()
        qapp.processEvents()
        for ch in w.doc.chunks:
            np.testing.assert_array_equal(np.asarray(ch.transform, float), before[ch.key][0])
            assert ch.crs == before[ch.key][1]
        assert [t.to_dict() for t in ties.ties_of(w.doc)] == ties_before
        assert w.doc.meta.get("ties_report") is None

        w.redo()
        qapp.processEvents()
        _close(w.doc.chunks[1].transform, b.truth, atol=1e-4)
        assert {ch.key: len(ch.markers) for ch in w.doc.chunks} == markers_before  # no end duplicated
        w.undo()
        qapp.processEvents()
        np.testing.assert_array_equal(np.asarray(w.doc.chunks[1].transform, float), before[kb][0])
    finally:
        _close_window(w)


def test_no_redundancy_asks_its_own_question_default_no(qapp, scene, tmp_path, monkeypatch):
    doc, a, b, _c = _three(scene)
    _tie(doc, a, b, _world(scene, AB[0]))
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        ka, kb = w.doc.chunks[0].key, w.doc.chunks[1].key
        dlg = _open(w, qapp)
        dlg.set_kind(ka, "fixed")
        dlg.set_kind(kb, "shift")
        _check(w, dlg, qapp)
        assert dlg.result["solution"].redundancy == 0
        assert "Nessun controllo" in dlg.report.toPlainText()
        before = np.asarray(w.doc.chunks[1].transform, float).copy()
        w.answers.append(1)  # Non applicare
        assert not dlg.apply()
        asked = [s for s in w.said if s[0] == "ask"]
        assert len(asked) == 1 and asked[0][2] == 1  # the default is No
        np.testing.assert_array_equal(w.doc.chunks[1].transform, before)
        w.answers.append(None)  # closed: not applied either
        assert not dlg.apply()
        np.testing.assert_array_equal(w.doc.chunks[1].transform, before)
        w.answers.append(0)  # Applica senza controllo
        assert dlg.apply()
        assert not np.array_equal(w.doc.chunks[1].transform, before)
    finally:
        _close_window(w)


def test_refused_solve_and_broken_tie_are_said(qapp, scene, tmp_path, monkeypatch):
    doc, a, b, _c = _three(scene)
    t1 = _tie(doc, a, b, _world(scene, AB[0]))
    _tie(doc, a, b, _world(scene, AB[1]))
    t3 = _tie(doc, a, b, _world(scene, AB[2]))
    b.chunk.markers.remove(b.chunk.marker(t3.b[1]))  # an end gone: the tie is broken
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        ka, kb = w.doc.chunks[0].key, w.doc.chunks[1].key
        dlg = _open(w, qapp)
        row = dlg.row_of(t3.id)
        assert "tolto o sostituito" in dlg.table.item(row, 6).text()
        assert not dlg.table.item(row, 0).flags() & Qt.ItemFlag.ItemIsUserCheckable
        dlg.set_kind(ka, "fixed")
        dlg.set_kind(kb, "similarity")  # 2 ties: 6 equations for 7 unknowns
        _check(w, dlg, qapp)
        assert dlg.result is None and not dlg.b_apply.isEnabled()
        assert "equazioni per 7 incognite" in dlg.report.toPlainText()
        dlg.set_kind(kb, "plan")
        _check(w, dlg, qapp)
        assert dlg.result is not None
        text = dlg.report.toPlainText()
        assert "Esclusi" in text and t3.name in text
        assert dlg.table.item(dlg.row_of(t1.id), 5).text()
    finally:
        _close_window(w)


def test_menu_command_and_remove(qapp, scene, tmp_path, monkeypatch):
    doc, a, b, _c = _three(scene)
    t1 = _tie(doc, a, b, _world(scene, AB[0]))
    _tie(doc, a, b, _world(scene, AB[1]))
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        from pyarchinit_photogrammetry.gui.command_search import menu_commands

        assert any("Aggancia chunk con legami" in k for k in menu_commands(w.menuBar()))
        dlg = _open(w, qapp)
        n_markers = len(w.doc.chunks[0].markers)
        dlg.table.selectRow(dlg.row_of(t1.id))
        dlg.remove_selected()
        assert [t.id for t in ties.ties_of(w.doc)] != [t1.id] and len(ties.ties_of(w.doc)) == 1
        assert len(w.doc.chunks[0].markers) == n_markers - 1
        assert dlg.table.rowCount() == 1
        # one step brings the tie and its markers back together, in both chunks
        assert w.history.undo_text() == "Rimuovi legami"
        w.undo()
        qapp.processEvents()
        back = ties.tie_by_id(w.doc, t1.id)
        assert back.active and not back.broken
        assert len(w.doc.chunks[0].markers) == n_markers and dlg.table.rowCount() == 2
        w.redo()
        qapp.processEvents()
        assert len(ties.ties_of(w.doc)) == 1 and len(w.doc.chunks[0].markers) == n_markers - 1
        assert len(w.doc.chunks[1].markers) == 1
    finally:
        _close_window(w)


def test_precision_from_markers_and_import(qapp, scene, tmp_path, monkeypatch):
    from pyarchinit_photogrammetry.gui import ties_dialog

    doc, a, b, c = _three(scene)
    for i, xy in enumerate(AB[:3]):
        p = _world(scene, xy)[0]
        a.chunk.add_marker(f"T{i}", a.internal(p)[0])
        b.chunk.add_marker(f"T{i}", b.internal(p)[0])
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        ka, kb, kc = (ch.key for ch in w.doc.chunks)
        dlg = _open(w, qapp)
        assert dlg.table.rowCount() == 0 and dlg.kinds(include_out=True)[kb] == ""
        monkeypatch.setattr(ties_dialog.ParamDialog, "ask", classmethod(lambda cls, *a, **k: {"a": ka, "b": kb}))
        dlg.from_markers()
        assert sorted(t.name for t in ties.ties_of(w.doc)) == ["T0", "T1", "T2"]
        assert dlg.table.rowCount() == 3 and dlg.kinds()[kb] == "plan"  # 3 ties: proposed in plan
        assert w.history.undo_text() == "Legami dai marker omonimi"

        _check(w, dlg, qapp)
        assert dlg.b_apply.isEnabled()
        tid = dlg.table.item(0, 0).data(Qt.ItemDataRole.UserRole)
        dlg.table.cellWidget(0, 4).setValue(0.123)
        assert ties.tie_by_id(w.doc, tid).precision == pytest.approx(0.123)
        assert not dlg.b_apply.isEnabled()  # the check no longer holds

        # a table: chunk A, x, y, z, chunk B, x, y, z (each in its own world) -> a tie to the third chunk too
        p = _world(scene, AB[3])[0]
        cc = w.doc.chunks[2]
        pa = w.doc.chunks[0].to_world(a.internal(p))[0]
        pc = cc.to_world(c.internal(p))[0]
        csv = tmp_path / "legami.csv"
        csv.write_text("chunk_a,x_a,y_a,z_a,chunk_b,x_b,y_b,z_b,precision,name\n"
                       f"2024,{pa[0]},{pa[1]},{pa[2]},2026,{pc[0]},{pc[1]},{pc[2]},0.02,picchetto\n")
        dlg.import_table(str(csv))
        assert dlg.table.rowCount() == 4
        assert dlg.kinds()[kc] == "plan" and dlg.kinds()[kb] == "plan"
        dlg.import_table(str(tmp_path / "nessuno.csv"))
        assert any(s[0] == "warn" and "Tabella dei legami" in s[1] for s in w.said)
    finally:
        _close_window(w)


def _table(tmp_path, w, a, c, scene, xy, name="picchetto"):
    p = _world(scene, xy)[0]
    pa = w.doc.chunks[0].to_world(a.internal(p))[0]
    pc = w.doc.chunks[2].to_world(c.internal(p))[0]
    csv = tmp_path / f"{name}.csv"
    csv.write_text("chunk_a,x_a,y_a,z_a,chunk_b,x_b,y_b,z_b,precision,name\n"
                   f"2024,{pa[0]},{pa[1]},{pa[2]},2026,{pc[0]},{pc[1]},{pc[2]},0.02,{name}\n")
    return csv


def test_undo_of_an_import_takes_the_ties_and_their_markers_only(qapp, scene, tmp_path, monkeypatch):
    doc, a, b, c = _three(scene)
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        ca, cc = w.doc.chunks[0], w.doc.chunks[2]
        ca.add_marker("mio", a.internal(_world(scene, AB[1]))[0])  # the user's marker: an entry of the snapshots
        w._dirty(True)
        assert w.history.can_undo()
        dlg = _open(w, qapp)
        dlg.import_table(str(_table(tmp_path, w, a, c, scene, AB[0])))
        assert [m.label for m in ca.markers] == ["mio", "picchetto"] and len(cc.markers) == 1
        assert w.history.undo_text() == "Importa legami"  # on top of the older entry of the markers
        w.undo()
        qapp.processEvents()
        assert ties.ties_of(w.doc) == [] and [m.label for m in w.doc.chunks[0].markers] == ["mio"]
        assert w.doc.chunks[2].markers == [] and dlg.table.rowCount() == 0
        w.redo()
        qapp.processEvents()
        (t,) = ties.ties_of(w.doc)
        assert not t.broken and [m.label for m in w.doc.chunks[0].markers] == ["mio", "picchetto"]
        assert len(w.doc.chunks[2].markers) == 1 and dlg.table.rowCount() == 1
        w.undo()
        w.undo()  # now the older entry: the user's marker, nothing else to lose
        qapp.processEvents()
        assert w.doc.chunks[0].markers == [] and ties.ties_of(w.doc) == []
    finally:
        _close_window(w)


def test_precision_steps_join_and_checks_that_no_longer_hold(qapp, scene, tmp_path, monkeypatch):
    doc, a, b, _c = _three(scene)
    for xy in FIVE:
        _tie(doc, a, b, _world(scene, xy))
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        kb = w.doc.chunks[1].key
        dlg = _open(w, qapp)
        tid = dlg.table.item(0, 0).data(Qt.ItemDataRole.UserRole)
        spin = dlg.table.cellWidget(0, 4)
        for v in (0.06, 0.07, 0.08):
            spin.setValue(v)
        assert len(w.history.entries) == 1 and w.history.undo_text() == "Precisione di un legame"
        w.undo()
        qapp.processEvents()
        assert ties.tie_by_id(w.doc, tid).precision == pytest.approx(0.05)

        # a change while the check runs: its result is not taken
        dlg = w._ties_dialog
        assert dlg.run_check()
        dlg.set_kind(kb, "rigid")
        w.runner.wait()
        qapp.processEvents()
        assert dlg.result is None and not dlg.b_apply.isEnabled()

        # a marker moved outside the dialog after the check: Applica refuses
        _check(w, dlg, qapp)
        assert dlg.b_apply.isEnabled()
        t = ties.tie_by_id(w.doc, tid)
        m = w.doc.chunks[1].marker(t.b[1])
        m.position = np.asarray(m.position, float) + 0.5
        before = np.asarray(w.doc.chunks[1].transform, float).copy()
        assert not dlg.apply()
        assert any(s[0] == "warn" and "dopo il controllo" in s[1] for s in w.said)
        np.testing.assert_array_equal(w.doc.chunks[1].transform, before)
        assert dlg.result is None
    finally:
        _close_window(w)


def test_an_edit_step_checks_only_its_chunks_and_says_a_refusal(qapp, scene, tmp_path, monkeypatch):
    doc, a, b, c = _three(scene)
    t1 = _tie(doc, a, b, _world(scene, AB[0]))
    _tie(doc, a, b, _world(scene, AB[1]))
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        ca, cb, cc = w.doc.chunks
        dlg = _open(w, qapp)
        dlg.table.selectRow(dlg.row_of(t1.id))
        dlg.remove_selected()
        step = w.history.entries[-1].action
        assert set(step.before["markers"]) == {ca.key, cb.key}  # the chunks the edit changed, not the third
        cc.add_marker("altrove", c.internal(_world(scene, AB[2]))[0])  # a change outside the history, elsewhere
        w.undo()
        qapp.processEvents()
        assert ties.tie_by_id(w.doc, t1.id).active and [m.label for m in cc.markers] == ["altrove"]
        w.redo()
        qapp.processEvents()
        assert len(ties.ties_of(w.doc)) == 1

        # a tie marker changed outside the history: the step refuses, said in the window's words, no error box
        errors = []
        monkeypatch.setattr(w, "_exc", lambda *a, **k: errors.append(a))
        ca.markers[0].position = np.asarray(ca.markers[0].position, float) + 0.1
        w.undo()
        qapp.processEvents()
        assert not errors and not [s for s in w.said if s[0] == "error"]
        assert "Il passo dei legami non si può più annullare" in w.statusBar().currentMessage()
        assert len(ties.ties_of(w.doc)) == 1 and not w.history.can_redo()
    finally:
        _close_window(w)


def test_ends_to_measure_again_after_a_new_alignment_are_said_and_confirmed(qapp, scene, tmp_path, monkeypatch):
    doc, a, b, _c = _three(scene)
    made = [_tie(doc, a, b, _world(scene, p)) for p in FIVE]
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        ka, kb = w.doc.chunks[0].key, w.doc.chunks[1].key
        dlg = _open(w, qapp)
        assert "riallineamento" not in dlg.table.item(dlg.row_of(made[0].id), 6).text()
        w.doc.chunks[1].meta["alignment_id"] = "nuovo"  # 2025 aligned again
        w._say_stale_ties()
        w.refresh_all()  # the dialog follows the state of the ties
        qapp.processEvents()
        state = dlg.table.item(dlg.row_of(made[0].id), 6).text()
        assert "da rimisurare dopo il riallineamento" in state and "2025" in state
        assert dlg.table.item(dlg.row_of(made[0].id), 1).foreground().color().name() == "#b9770e"
        dlg.set_kind(ka, "fixed")
        dlg.set_kind(kb, "plan")
        _check(w, dlg, qapp)
        assert dlg.result is None  # every tie left out: nothing places 2025
        dlg.table.selectRow(dlg.row_of(made[0].id))
        dlg.confirm_selected()
        assert w.history.undo_text() == "Conferma estremi"
        assert [s["id"] for s in ties.stale_ends(w.doc)] == [t.id for t in made[1:]]
        dlg.table.clearSelection()
        dlg.confirm_selected()  # none selected: every tie
        assert ties.stale_ends(w.doc) == []
        assert "riallineamento" not in dlg.table.item(dlg.row_of(made[0].id), 6).text()
        w.undo()
        qapp.processEvents()
        assert len(ties.stale_ends(w.doc)) == len(made) - 1
    finally:
        _close_window(w)


def test_the_dialog_follows_chunks_removed_and_added(qapp, scene, tmp_path, monkeypatch):
    doc, a, b, c = _three(scene)
    for p in AB:
        _tie(doc, a, b, _world(scene, p))
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        dlg = _open(w, qapp)
        assert dlg.chunks.rowCount() == 3
        dlg.set_kind(w.doc.chunks[0].key, "fixed")
        dlg.set_kind(w.doc.chunks[1].key, "plan")
        w.doc.remove_chunk(w.doc.chunks[2])
        w.refresh_all()
        qapp.processEvents()
        assert dlg.chunks.rowCount() == 2
        assert dlg.kinds() == {w.doc.chunks[0].key: "fixed", w.doc.chunks[1].key: "plan"}  # choices kept
        w.doc.add_chunk("2027")
        w.refresh_all()
        qapp.processEvents()
        assert dlg.chunks.rowCount() == 3
        dlg.close()
        qapp.processEvents()
        w.refresh_all()  # disconnected: nothing reaches the closed dialog
        qapp.processEvents()
    finally:
        _close_window(w)


def test_saving_writes_the_ties_back_without_an_edit_in_the_history(qapp, scene, tmp_path, monkeypatch):
    """Saving gives a marker of a tie its identity back (``ties.settle_stored``): the history is rebased, so it is
    still unchanged after the save and the next edit carries nothing of it."""
    doc, a, b, _c = _three(scene)
    tie = _tie(doc, a, b, _world(scene, AB[0]))
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        assert w.chunk is a.chunk and w.history is not None
        end = a.chunk.marker_by_key(tie.a[1])
        end.uid = None  # as a project saved by 1.7.5 would have it, until the ties are read again
        w.history.rebase()
        assert w.history.unchanged()
        assert w.save_document()
        assert end.uid == tie.a_uid  # given back by the save
        assert w.history.unchanged()
        w.sections.append({"label": "Sezione 1", "p1": [0, 0], "p2": [1, 1]})
        assert w.history.record()
        assert w.history.undo_text() == "Sezioni"  # no "Marker" part from the save
    finally:
        w.sections.clear()
        _close_window(w)
