"""Groups of chunks in the window: the folder in the Area di lavoro, chunks dropped into it, shown and hidden together
in the 3D view, renamed, dissolved, every change one step of Modifica > Annulla; the operations on a group (ties as one
block, merge, batch)."""
from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")

from test_gui_ties import _close_window, _window  # noqa: E402

import pyarchinit_photogrammetry as ppg  # noqa: E402
from pyarchinit_photogrammetry.core import groups  # noqa: E402
from pyarchinit_photogrammetry.engine import ties  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _doc():
    doc = ppg.Document()
    for label in ("A", "B", "C", "D"):
        doc.add_chunk(label).crs = "EPSG:3004"
    return doc


def _node(w, gid):
    for item in w.workspace._walk():
        data = item.data(0, 0x0100)
        if data and data[0] == "chunk_group" and data[2] == gid:
            return item
    return None


def _children(item):
    return [item.child(i).data(0, 0x0100)[1].label for i in range(item.childCount())
            if item.child(i).data(0, 0x0100)[0] == "chunk"]


class _Event:
    def __init__(self, point):
        self.point = point

    def position(self):
        from PySide6.QtCore import QPointF

        return QPointF(self.point)


def test_groups_in_the_tree_with_undo(qapp, tmp_path, monkeypatch):
    from pyarchinit_photogrammetry.gui import chunk_groups
    from pyarchinit_photogrammetry.gui.chunk_views import settings

    w = _window(qapp, _doc(), tmp_path, monkeypatch)
    try:
        doc = w.doc
        a, b, c, d = doc.chunks
        assert chunk_groups.new_group(w, [b, c], name="2025")
        gid = groups.group(doc, "2025")["id"]
        node = _node(w, gid)
        assert node is not None and _children(node) == ["B", "C"] and "2025" in node.text(0)
        top = [node.parent().child(i) for i in range(node.parent().childCount())]
        assert [t.data(0, 0x0100)[0] for t in top][:3] == ["chunk", "chunk_group", "chunk"]  # in B's place
        # dropped into the group before C, then out of it onto the project
        rect = w.workspace.visualItemRect(node)
        assert chunk_groups.drop_target(w.workspace, _Event(rect.center())) == (gid, None)
        w.workspace.chunks_dropped.emit([d.key], gid, c.key)
        qapp.processEvents()
        assert _children(_node(w, gid)) == ["B", "D", "C"]
        w.workspace.chunks_dropped.emit([b.key], None, None)
        assert groups.group(doc, gid)["chunks"] == [d.key, c.key]
        w.undo()
        assert groups.group(doc, gid)["chunks"] == [b.key, d.key, c.key]
        w.undo()
        w.undo()
        assert groups.groups_of(doc) == []
        w.redo()
        assert groups.group(doc, "2025")["chunks"] == [b.key, c.key]
        # the box of the group: its chunks in the 3D view (the active one always is)
        node = _node(w, gid)
        from PySide6.QtCore import Qt

        node.setCheckState(0, Qt.CheckState.Checked)
        qapp.processEvents()
        assert {b.key, c.key} <= set(settings(doc)["chunks_3d"])
        assert chunk_groups.shown_state(doc, gid, w.chunk) == 2
        chunk_groups.show(w, gid, False)
        assert chunk_groups.shown_state(doc, gid, w.chunk) == 0
        assert chunk_groups.rename(w, gid, "anno 2025")
        assert _node(w, gid).text(0).startswith("anno 2025")
        assert chunk_groups.dissolve(w, gid)
        assert groups.groups_of(doc) == [] and [c.label for c in doc.chunks] == ["A", "B", "C", "D"]
        w.undo()
        assert groups.group(doc, gid)["name"] == "anno 2025"
    finally:
        _close_window(w)


def test_operations_on_a_group(qapp, tmp_path, monkeypatch):
    from pyarchinit_photogrammetry.gui import chunk_groups

    doc = _doc()
    ties.add_tie(doc, "A", (0, 0, 0), "B", (0, 0, 0))
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        doc = w.doc
        a, b, c, d = doc.chunks
        chunk_groups.new_group(w, [b, c, d], name="2025")
        gid = groups.group(doc, "2025")["id"]
        chunk_groups.tie_as_block(w, gid)
        dlg = w._live_ties_dialog()
        kinds = dlg.kinds(include_out=True)
        assert kinds[a.key] == "fixed" and kinds[b.key] not in ("", ties.GROUP)
        assert kinds[c.key] == kinds[d.key] == ties.GROUP
        asked = {}
        monkeypatch.setattr(w, "merge_chunks", lambda keys=None: asked.setdefault("merge", keys))
        monkeypatch.setattr(w, "batch_process", lambda preset=None: asked.setdefault("batch", preset))
        chunk_groups.merge(w, gid)
        chunk_groups.batch(w, gid, ["build_dem", "build_orthomosaic"])
        assert asked["merge"] == [b.key, c.key, d.key]
        assert [s["op"] for s in asked["batch"]] == ["build_dem", "build_orthomosaic"]
        assert asked["batch"][0]["chunks"] == ["B", "C", "D"]
        from PySide6.QtWidgets import QMenu

        menu = QMenu()
        chunk_groups.group_menu(w, menu, gid)
        texts = [x.text() for x in menu.actions() if x.text()]
        assert "Aggancia il gruppo con legami..." in texts and "Sciogli il gruppo" in texts
    finally:
        _close_window(w)
