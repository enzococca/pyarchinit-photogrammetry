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
            assert dlg.preview_pixmap() is not None
    finally:
        _close(w)
