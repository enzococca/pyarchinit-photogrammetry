"""*Cerca picchetti* (Riferimenti pane, *Flusso di lavoro*): the search runs in the worker, the proposals stay in the
window with their picture, and only those accepted become known ties - one step of Modifica > Annulla. The synthetic
trench of ``test_stakes`` (EPSG:3004 synthetic origin, no real coordinates)."""
from __future__ import annotations

import os

import numpy as np
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")

from test_stakes import HEADS, project  # noqa: E402

from pyarchinit_photogrammetry.engine import ties  # noqa: E402
from pyarchinit_photogrammetry.testing import synthetic  # noqa: E402

CRS = "EPSG:3004"


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def scene():
    sc = synthetic.make_scene(size=(8.0, 6.0), texel=0.004, crs=CRS)
    truth = synthetic.add_stakes(sc, HEADS)
    return sc, truth


def _csv(path, truth, shift=(0.2, -0.15)):
    lines = ["nome,x,y,z,precisione"]
    for name, (x, y, z) in truth.items():
        lines.append(f"{name},{x + shift[0]:.4f},{y + shift[1]:.4f},{z:.4f},0.02")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


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
    return w


def _close(w):
    w._close_stakes_dialog()
    w._close_ties_dialog()
    w.photos.stop()
    w.setWindowModified(False)
    w.close()


def _searched(w, qapp, tmp_path, truth):
    w.reference.b_stakes.click()  # the button of the Riferimenti pane
    qapp.processEvents()
    dlg = w._stakes_dialog
    assert dlg is not None and dlg.isVisible()
    dlg.set_csv(_csv(tmp_path / "noti.csv", truth))
    dlg.s_radius.setValue(1.0)
    assert dlg.search()
    w.runner.wait()
    qapp.processEvents()
    assert not w.said, w.said
    return dlg


def test_nothing_is_added_until_accepted(qapp, tmp_path, monkeypatch, scene):
    sc, truth = scene
    doc, ch = project(tmp_path, sc)
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        markers = len(ch.markers)
        steps = len(w.history.entries)
        dlg = _searched(w, qapp, tmp_path, truth)
        assert dlg.table.rowCount() == len(truth)
        assert all(p.best is not None for p in dlg.proposals)
        for p in dlg.proposals:
            assert np.linalg.norm(p.best.xy_world - np.asarray(truth[p.point.name])[:2]) < 0.02
        assert dlg.table.item(0, 2).text() == "solo ortofoto"  # no photos in this project
        assert ties.ties_of(doc) == [] and len(ch.markers) == markers
        assert len(w.history.entries) == steps and not w.isWindowModified()
        # discard and another candidate change the list only
        dlg.select([dlg.proposals[0].point.name])
        assert dlg.discard_selected() == 1 and dlg.table.rowCount() == len(truth) - 1
        assert ties.ties_of(doc) == [] and len(w.history.entries) == steps
        # nothing confirmed by the photos: Accetta i concordi accepts nothing and says so
        assert dlg.accept_agreeing() == []
        assert ties.ties_of(doc) == [] and w.said and w.said[-1][0] == "info"
    finally:
        _close(w)


def test_search_then_accept_makes_known_ties_and_undo_removes_them(qapp, tmp_path, monkeypatch, scene):
    sc, truth = scene
    doc, ch = project(tmp_path, sc)
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        markers = len(ch.markers)
        dlg = _searched(w, qapp, tmp_path, truth)
        names = [p.point.name for p in dlg.proposals[:2]]
        dlg.select(names)
        assert dlg.b_accept.isEnabled()
        steps = len(w.history.entries)
        made = dlg.accept_selected()
        qapp.processEvents()
        assert sorted(t.name for t in made) == sorted(names)
        got = ties.ties_of(doc)
        assert sorted(t.name for t in got) == sorted(names) and all(t.b is None for t in got)
        assert len(ch.markers) == markers + 2
        for t in got:  # the known coordinates of the CSV, the end on the head found
            assert np.allclose(t.known[:2], np.asarray(truth[t.name])[:2] + (0.2, -0.15), atol=1e-6)
        assert len(w.history.entries) == steps + 1  # one step for the whole acceptance
        assert dlg.table.rowCount() == len(truth) - 2  # the accepted ones leave the list
        w.undo()
        qapp.processEvents()
        assert ties.ties_of(doc) == [] and len(ch.markers) == markers
        w.redo()
        qapp.processEvents()
        assert sorted(t.name for t in ties.ties_of(doc)) == sorted(names)
        # the menu entry opens the same window, and the search of the commands finds it
        assert w.find_stakes() is dlg
        from pyarchinit_photogrammetry.gui.command_search import menu_commands

        assert any(name.startswith("Cerca picchetti") for name in menu_commands(w.menuBar()))
    finally:
        _close(w)


def test_the_preview_shows_the_selected_proposal(qapp, tmp_path, monkeypatch, scene):
    from pyarchinit_photogrammetry.gui.stakes_dialog import PREVIEW_SIZE

    sc, truth = scene
    doc, _ch = project(tmp_path, sc)
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        dlg = _searched(w, qapp, tmp_path, truth)
        assert dlg.preview_pixmap() is None  # nothing selected yet
        dlg.select([dlg.proposals[1].point.name])
        dlg.wait_preview()  # read in a thread of its own
        pix = dlg.preview_pixmap()
        assert pix is not None and not pix.isNull()
        assert (pix.width(), pix.height()) == (2 * PREVIEW_SIZE, PREVIEW_SIZE)
        img = pix.toImage()
        # the red cross on the head, in the middle of the orthophoto half
        c = img.pixelColor(PREVIEW_SIZE // 2 - 8, PREVIEW_SIZE // 2)
        assert c.red() > 200 and c.green() < 100
        # another candidate: the picture follows it
        p = dlg.proposals[1]
        if p.others:
            assert dlg.other_candidate()
            assert dlg.proposals[1].best is p.others[0]
            dlg.wait_preview()
            assert dlg.preview_pixmap() is not None
    finally:
        _close(w)


def _confirmed(p):
    """A proposal as if the photos had confirmed it (no photo measurement kept: the window's choice is tested)."""
    p.best.ortho_only, p.best.agreeing, p.best.photos = False, 4, []
    p.flags = [f for f in p.flags if f != "ortho_only"]


def test_accept_the_agreeing_ones_leaves_out_the_red_and_the_orthophoto_only(qapp, tmp_path, monkeypatch, scene):
    sc, truth = scene
    doc, _ch = project(tmp_path, sc)
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        dlg = _searched(w, qapp, tmp_path, truth)
        names = [p.point.name for p in dlg.proposals]
        for p in dlg.proposals[:3]:
            _confirmed(p)
        dlg._refill()
        dlg.result["suspects"] = [names[0]]  # a red row: never accepted with the agreeing ones
        steps = len(w.history.entries)
        made = dlg.accept_agreeing()
        assert sorted(t.name for t in made) == sorted(names[1:3])
        assert sorted(t.name for t in ties.ties_of(doc)) == sorted(names[1:3])
        assert len(w.history.entries) == steps + 1
        assert [p.point.name for p in dlg.proposals] == [names[0], *names[3:]]
    finally:
        _close(w)


def test_accept_refused_after_a_new_alignment_or_when_the_chunk_is_gone(qapp, tmp_path, monkeypatch, scene):
    sc, truth = scene
    doc, ch = project(tmp_path, sc)
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        dlg = _searched(w, qapp, tmp_path, truth)
        steps, markers = len(w.history.entries), len(ch.markers)
        first = dlg.proposals[0].point.name
        dlg.proposals[0].alignment = "un altro allineamento"
        dlg.select([first])
        assert dlg.accept_selected() == []
        assert w.said[-1][0] == "warn" and ties.ties_of(doc) == []
        assert len(w.history.entries) == steps and len(ch.markers) == markers
        # the chunk moved by a solve after the search: the window says the table is of before
        t = np.asarray(ch.transform, float).copy()
        t[0, 3] += 0.5
        ch.transform = t
        w.refresh_all()
        qapp.processEvents()
        assert "spostato dopo la ricerca" in dlg.summary.text()
        # the chunk removed: said, nothing added
        dlg.chunk_key = 10_000
        dlg.select([dlg.proposals[1].point.name])
        assert dlg.accept_selected() == []
        assert w.said[-1][0] == "warn" and "non c'è più" in w.said[-1][1]
        assert ties.ties_of(doc) == [] and len(w.history.entries) == steps
    finally:
        _close(w)


def test_a_failure_half_way_is_still_one_step(qapp, tmp_path, monkeypatch, scene):
    from pyarchinit_photogrammetry.engine import stakes

    sc, truth = scene
    doc, _ch = project(tmp_path, sc)
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        dlg = _searched(w, qapp, tmp_path, truth)
        real = ties.add_known
        calls = []

        def add_known(*a, **k):  # the second proposal fails after the first one is made
            calls.append(1)
            if len(calls) == 2:
                raise ValueError("precisione non valida")
            return real(*a, **k)

        monkeypatch.setattr(stakes.ties, "add_known", add_known)
        dlg.select([p.point.name for p in dlg.proposals[:2]])
        steps = len(w.history.entries)
        assert dlg.accept_selected() == []
        assert w.said[-1][0] == "warn"
        assert len(ties.ties_of(doc)) == 1 and len(w.history.entries) == steps + 1  # what was made, undoable
        w.undo()
        qapp.processEvents()
        assert ties.ties_of(doc) == []
    finally:
        _close(w)


def test_the_window_follows_chunks_removed_while_open(qapp, tmp_path, monkeypatch, scene):
    """Chunks removed (Rimuovi in the Area di lavoro) with the window open: the chunk searched leaves the combo, the
    proposals are said to be of a chunk gone, the picture cleared, accepting refused; a chunk whose markers were a
    source leaves the sources; Annulla brings the chunk back to the combo. Nothing raises."""
    from pyarchinit_photogrammetry.gui.stakes_dialog import find_data

    sc, truth = scene
    doc, ch = project(tmp_path, sc)
    other = doc.add_chunk("Rilievo")
    other.crs = CRS
    other.add_marker("GCP1", reference=(*truth[next(iter(truth))][:2], 1.0))
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        dlg = _searched(w, qapp, tmp_path, truth)
        assert find_data(dlg.c_source, ("chunk", other.key)) >= 0
        assert w.remove_workspace_items([("chunk", other, None)], confirm=False)
        qapp.processEvents()
        assert find_data(dlg.c_source, ("chunk", other.key)) < 0  # its markers no longer a source
        assert dlg.c_chunk.findData(ch.key) >= 0 and dlg.proposals
        dlg.select([dlg.proposals[0].point.name])
        dlg.wait_preview()
        assert dlg.preview_pixmap() is not None
        key = ch.key
        assert w.remove_workspace_items([("chunk", ch, None)], confirm=False)
        qapp.processEvents()
        assert dlg.isVisible() and dlg.c_chunk.findData(key) < 0
        assert "non c&#x27;è più" in dlg.summary.text()
        assert dlg.preview_pixmap() is None
        dlg.select([dlg.proposals[1].point.name])  # a row of a chunk gone: no picture, no crash
        qapp.processEvents()
        assert dlg.preview_pixmap() is None
        w.said.clear()
        assert dlg.accept_selected() == [] and w.said[-1][0] == "warn"
        assert not dlg.search() and w.said[-1][0] == "warn"
        w.history.undo()
        qapp.processEvents()
        assert dlg.c_chunk.findData(key) >= 0
    finally:
        _close(w)
