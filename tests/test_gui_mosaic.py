"""*Strumenti > Ortofoto e DEM uniti...*: the merged product built from the window in the worker, put in the node
*Prodotti uniti* of the Area di lavoro and shown with its areas; one step of Annulla that puts its folder aside; the
chunk each cell comes from under the cursor. The three synthetic chunks of ``test_mosaic`` (a synthetic origin, no
real place)."""
from __future__ import annotations

import os

import numpy as np
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")

from test_mosaic import X0, Y1, project  # noqa: E402

from pyarchinit_photogrammetry.core import free_drawings  # noqa: E402
from pyarchinit_photogrammetry.engine import mosaic  # noqa: E402


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
    monkeypatch.setattr(mwmod, "choice", lambda *a, **k: said.append(("choice", a[2] if len(a) > 2 else "")) or 0)
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
    w._close_mosaic_dialog()
    w.photos.stop()
    w.setWindowModified(False)
    w.close()


def _open(w, qapp):
    from pyarchinit_photogrammetry.gui.command_search import clean, menu_commands

    act = next(a for a in menu_commands(w.menuBar()).values() if clean(a.text()) == clean("Ortofoto e DEM uniti..."))
    act.trigger()  # in Strumenti, so in the command search too
    qapp.processEvents()
    dlg = w._mosaic_dialog
    assert dlg is not None and dlg.isVisible()
    return dlg


def _wait(w, qapp):
    w.runner.wait()
    qapp.processEvents()


def _tree(w):
    from PySide6.QtCore import Qt

    out = []
    for item in w.workspace._walk():
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data and data[0] in ("mosaics", "mosaic"):
            out.append((data[0], data[2], item.text(0)))
    return out


def _build(w, qapp, monkeypatch, product="orthomosaic"):
    dlg = _open(w, qapp)
    dlg.set_order(["B", "A"])
    dlg.c_product.setCurrentIndex(dlg.c_product.findData(product))
    assert [c.label for c in dlg.chosen()] == ["B", "A"]
    a = next(c for c in w.doc.chunks if c.label == "A")
    row = next(r for r in range(dlg.table.rowCount()) if dlg._chunk(r) is a)
    dlg.table.selectRow(row)
    # the area of A drawn on the view: x 2.5-3.5 m (where B, first in the list, has data too)
    ring = [[X0 + 2.5, Y1 - 3.0], [X0 + 3.5, Y1 - 3.0], [X0 + 3.5, Y1 - 1.0], [X0 + 2.5, Y1 - 1.0]]
    monkeypatch.setattr(w, "request_geometry", lambda kind, done, *a, **k: (done(np.array(ring)), True)[1])
    assert dlg.start_area()
    assert dlg.c_background.isEnabled() and "disegnata" in dlg.table.item(row, 2).text()
    entries = len(w.history.entries)
    assert dlg.build()
    _wait(w, qapp)
    return dlg, entries


def test_build_from_the_dialog_adds_a_tree_node_and_shows_it(qapp, tmp_path, monkeypatch):
    doc, c = project(tmp_path)
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        dlg, entries = _build(w, qapp, monkeypatch)
        recs = mosaic.mosaics_of(w.doc)
        assert len(recs) == 1, w.said
        rec = recs[0]
        assert rec["chunks"] == [c["B"].key, c["A"].key] and rec["background"] == "faded"
        assert len(w.history.entries) == entries + 1 and w.history.entries[-1].text == "Ortofoto e DEM uniti"
        tree = _tree(w)
        assert tree[0][0] == "mosaics" and "(1)" in tree[0][2]
        assert tree[1][:2] == ("mosaic", rec["id"]) and "Ortofoto unita 1" in tree[1][2]
        assert "da rifare" not in tree[1][2]
        # shown in the 2D view with its areas
        assert w._mosaic_on_view()["id"] == rec["id"]
        assert mosaic.AREAS_LAYER in w.ortho_view.layers
        # a double click on another product and back on the merged one
        w._show_raster("dem")
        assert w._mosaic_on_view() is None
        w._workspace_item("mosaic", None, rec["id"])
        assert w._mosaic_on_view()["id"] == rec["id"]
        # an area from a layer of polygons of C (a free drawing)
        free_drawings.add(c["C"], "polygon", [[X0 + 5, Y1 - 3, 30], [X0 + 6, Y1 - 3, 30], [X0 + 6, Y1 - 1, 30]],
                          layer="Zona C")
        dlg.refresh()
        row = next(r for r in range(dlg.table.rowCount()) if dlg._chunk(r) is c["C"])
        dlg.table.item(row, 0).setCheckState(dlg.table.item(row, 0).checkState().Checked)
        dlg.table.selectRow(row)
        monkeypatch.setattr(dlg, "choose_layer", lambda chunk: "Zona C")
        assert dlg.area_from_layer() and "Zona C" in dlg.table.item(row, 2).text()
        dlg.c_product.setCurrentIndex(dlg.c_product.findData("dem"))  # the ticks and the areas kept
        assert [x.label for x in dlg.chosen()] == ["B", "A", "C"] and c["C"].key in dlg.areas
        assert dlg.build()
        _wait(w, qapp)
        recs = mosaic.mosaics_of(w.doc)
        assert len(recs) == 2 and recs[1]["product"] == "dem" and len(recs[1]["areas"]) == 2
        assert "(2)" in _tree(w)[0][2] and w._mosaic_on_view()["id"] == recs[1]["id"]
        # a chunk moved after the merge: the badge in the tree
        t = np.eye(4)
        t[0, 3] = 0.5
        c["A"].transform = t
        w.workspace.populate(w.doc)
        assert all("da rifare" in text for kind, _p, text in _tree(w) if kind == "mosaic")
        # an error of the engine said in the window's words, nothing built
        dlg.set_order(["A"])
        dlg.s_res.setValue(0.0)
        monkeypatch.setattr(mosaic, "build", lambda *a, **k: (_ for _ in ()).throw(
            mosaic.MosaicError("no", "nessun prodotto")))
        w.said.clear()
        assert dlg.build()
        _wait(w, qapp)
        assert ("warn", "Ortofoto e DEM uniti: nessun prodotto") in w.said and len(mosaic.mosaics_of(w.doc)) == 2
    finally:
        _close(w)


def test_undo_puts_the_folder_aside(qapp, tmp_path, monkeypatch):
    doc, _c = project(tmp_path)
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        _dlg, entries = _build(w, qapp, monkeypatch)
        rec = mosaic.mosaics_of(w.doc)[0]
        folder = mosaic.folder(w.doc, rec["id"])
        assert folder.is_dir()
        w.history.undo()
        qapp.processEvents()
        assert not mosaic.mosaics_of(w.doc) and not folder.exists() and not _tree(w)
        assert any((w.doc.files_dir / ".removed").rglob("mosaic.tif"))
        assert w._mosaic_on_view() is None
        w.history.redo()
        qapp.processEvents()
        assert mosaic.mosaics_of(w.doc)[0]["id"] == rec["id"] and (folder / "mosaic.tif").exists()
        assert _tree(w)[1][1] == rec["id"]
        # removed from the tree, back with Annulla
        w.show_mosaic(rec["id"])
        assert w.remove_workspace_items([("mosaic", None, rec["id"])], confirm=False)
        assert not mosaic.mosaics_of(w.doc) and not folder.exists() and w._mosaic_on_view() is None
        assert w.history.entries[-1].text == "Rimuovi il prodotto unito «Ortofoto unita 1»"
        w.history.undo()
        qapp.processEvents()
        assert mosaic.mosaics_of(w.doc)[0]["id"] == rec["id"] and folder.is_dir()
        w.history.redo()
        assert not folder.exists()
        # the project saved (by the build) lists it: closing without saving puts it back
        w.doc.settle_removals()
        assert folder.is_dir()
    finally:
        _close(w)


def test_unsaved_build_undone_leaves_no_folder(tmp_path):
    """A build never saved and undone: the end of the session deletes its folder; a crash leaves it to recover."""
    from pyarchinit_photogrammetry.core import removal

    doc, _c = project(tmp_path)
    rec = mosaic.build(doc, ["A", "B"])
    r = removal.MosaicRemoval.added(doc, [rec["id"]])
    folder = mosaic.folder(doc, rec["id"])
    r.redo()
    assert not folder.exists() and not mosaic.mosaics_of(doc)
    r.undo()
    assert folder.is_dir() and mosaic.mosaics_of(doc)[0]["id"] == rec["id"]
    doc.settle_removals()  # never saved with it: the project on disk does not list it
    assert not folder.exists()
    # a crash after a build not saved: the journal says which folders the saved project does not list
    rec2 = mosaic.build(doc, ["A", "B"])
    removal.MosaicRemoval.added(doc, [rec2["id"]])
    doc.removals = []  # the session gone without settling
    doc.meta.pop("mosaics")
    assert doc.recover_removed()["deleted"] == 1 and not mosaic.folder(doc, rec2["id"]).exists()


def test_source_under_the_cursor(qapp, tmp_path, monkeypatch):
    doc, c = project(tmp_path)
    w = _window(qapp, doc, tmp_path, monkeypatch)
    try:
        rec = mosaic.build(w.doc, ["B", "A", "C"], product="dem")
        w._mosaics_changed()
        assert w.show_mosaic(rec["id"])
        w._show_coords(X0 + 1.0, Y1 - 1.0, 10.0)
        assert "da A" in w.coord_label.text() and "Z 10.000" in w.coord_label.text()
        w._show_coords(X0 + 3.0, Y1 - 1.0, 20.0)
        assert "da B" in w.coord_label.text()
        w._show_coords(X0 + 7.0, Y1 - 1.0, 30.0)
        assert "da C" in w.coord_label.text()
        w._show_coords(X0 - 5.0, Y1 - 1.0, None)
        assert "nessun chunk" in w.coord_label.text()
        # another raster shown: the coordinates of the chunk again
        w._show_raster("ortho")
        w._show_coords(X0 + 1.0, Y1 - 1.0, None)
        assert "da " not in w.coord_label.text()
        # a merged product in another system than the active chunk: said in the status bar
        c["A"].crs = "EPSG:3003"
        w.doc.chunk = c["A"]
        assert w.show_mosaic(rec["id"])
        assert "EPSG:3003" in w.statusBar().currentMessage()
    finally:
        _close(w)
