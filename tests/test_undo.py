"""Undo and redo: snapshots of the project, files of products swapped back."""
from __future__ import annotations

import shutil

import numpy as np
import pytest

from pyarchinit_photogrammetry.gui.undo import UndoHistory


class _Chunk:
    def __init__(self, folder, key=0):
        self.key, self.data_dir = key, folder
        self.products = {"point_cloud": "point_cloud.ply"}

    def product_path(self, name):
        rel = self.products.get(name)
        return None if rel is None else self.data_dir / rel


def test_history_of_parts_and_files(tmp_path):
    chunk = _Chunk(tmp_path)
    (tmp_path / "point_cloud.ply").write_text("A")
    state = {"chunk": 0, "units": [], "markers": ["m1"]}
    applied = []

    def set_state(parts, names, products):
        state.update(parts)
        applied.append((sorted(names), products))

    h = UndoHistory(lambda: dict(state), set_state, lambda: chunk, limit=3)
    assert not h.can_undo() and not h.record()  # nothing changed
    state["units"] = ["u1"]
    assert h.record() and h.undo_text() == "Disegni"
    state["markers"] = ["m1", "m2"]
    h.record()
    h.backup(["point_cloud"])  # an edit of the dense cloud in place
    (tmp_path / "point_cloud.ply").write_text("B")
    assert h.record("Lazo sulla nuvola") and h.undo_text() == "Lazo sulla nuvola"
    assert h.undo() == "Lazo sulla nuvola" and (tmp_path / "point_cloud.ply").read_text() == "A"
    assert h.redo() and (tmp_path / "point_cloud.ply").read_text() == "B"
    h.undo()
    assert h.undo() == "Marker" and state["markers"] == ["m1"]
    assert h.undo() == "Disegni" and state["units"] == [] and not h.can_undo()
    assert h.redo() == "Disegni" and state["units"] == ["u1"] and h.can_redo()
    state["units"] = ["u1", "u9"]  # a new edit: what was undone is gone
    h.record()
    assert not h.can_redo() and len(h.entries) == 2
    assert not list((tmp_path / "undo").glob("*"))  # the copies of the dropped entry were deleted
    for i in range(5):  # at most ``limit`` entries
        state["units"] = [f"x{i}"]
        h.record()
    assert len(h.entries) == 3
    # a failed edit leaves no entry and no copy
    h.backup(["point_cloud"])
    h.discard_pending()
    assert not h.record() and not list((tmp_path / "undo").glob("*"))
    # another chunk: another history
    state["chunk"] = 1
    assert not h.record() and not h.can_undo()


@pytest.fixture
def window(processed, tmp_path):
    import os

    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    import pyarchinit_photogrammetry as ppg

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui import main_window as mw

    doc, _ch, _ = processed
    d = tmp_path / "copia"
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")
    w = mw.MainWindow(ppg.Document.open(d / doc.path.name), autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    yield w, app
    w.setWindowModified(False)
    w.close()
    app.processEvents()


def test_undo_in_the_window(window):
    from pyarchinit_photogrammetry.core.marker import Projection

    w, app = window
    ch = w.chunk
    assert not w.a_undo.isEnabled()
    # a spot height
    w._add_quota({"x": 2480001.0, "y": 4500001.0, "quota_q": 99.5, "us_q": "3"})
    assert w.a_undo.isEnabled() and w.a_undo.text() == "Annulla: Quote"
    w.undo()
    assert w.quote == [] and w.a_redo.text() == "Ripeti: Quote"
    w.redo()
    assert len(w.quote) == 1 and w.quote[0]["us_q"] == "3"
    # a marker measured in one photo less
    m = ch.markers[0]
    before = {k: (p.x, p.y) for k, p in m.projections.items() if p.measured}
    gone = next(iter(before))
    del m.projections[gone]
    w._markers_edited(m)
    w.undo()
    m = ch.marker_by_key(m.key)
    assert {k: (p.x, p.y) for k, p in m.projections.items() if p.measured} == before
    assert isinstance(m.projections[gone], Projection)
    # classes given by hand to some points, then points deleted with the lasso
    pc = ch.point_cloud
    n0, cls0 = len(pc), np.asarray(pc.classification).copy()
    mask = np.zeros(n0, bool)
    mask[:500] = True
    w._set_point_class(mask, 64)
    w._dirty(True)
    assert np.all(np.asarray(ch.point_cloud.classification)[:500] == 64)
    from pyarchinit_photogrammetry.engine.cleaning import delete_points

    w.run_task("Lazo sulla nuvola", w._versioned_edit("point_cloud", {}, lambda p: delete_points(ch, mask),
                                                      "Nuvola modificata con il lazo"), refresh=False)
    w.runner.wait()
    app.processEvents()
    assert len(ch.point_cloud) == n0 - 500 and w.a_undo.text() == "Annulla: Lazo sulla nuvola"
    w.undo()
    assert len(ch.point_cloud) == n0 and np.all(np.asarray(ch.point_cloud.classification)[:500] == 64)
    w.undo()
    assert np.array_equal(np.asarray(ch.point_cloud.classification), cls0)
    w.redo()
    w.redo()
    assert len(ch.point_cloud) == n0 - 500
    # a processing step is not an edit: it leaves the history as it is
    entries = len(w.history.entries)
    w.estimate_quality()
    w.runner.wait()
    app.processEvents()
    assert len(w.history.entries) == entries
