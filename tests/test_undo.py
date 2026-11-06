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


def test_close_deletes_the_copies_of_the_steps_but_a_broken_hold(tmp_path):
    """The session ends: the copies of the products, the holds of an orientation and of a rebuild go; the hold a failed
    undo left on purpose (``broken``) stays; while the session runs nothing of a step that can be undone is touched."""
    chunk = _Chunk(tmp_path)
    (tmp_path / "point_cloud.ply").write_text("A")
    state = {"chunk": 0, "units": []}
    h = UndoHistory(lambda: dict(state), lambda *a: None, lambda: chunk)
    h.backup(["point_cloud"])
    (tmp_path / "point_cloud.ply").write_text("B")
    assert h.record("Lazo")
    held = tmp_path / "undo" / "orient-abc"
    (held / "x.ply").parent.mkdir(parents=True)
    (held / "x.ply").write_text("p")

    class Step:
        def __init__(self, hold, broken):
            self.hold, self.broken, self.dropped = hold, broken, False

        def undo(self):
            pass

        def redo(self):
            pass

        def drop(self):
            self.dropped = True

    broken = Step(tmp_path / "undo" / "rebuild-bad", True)
    (broken.hold / "before").mkdir(parents=True)
    h.push("Ricostruisci", broken, document=False)
    state["units"] = ["u1"]
    h.record()  # another edit in the session: the copies of the steps that can be undone stay
    assert len(list((tmp_path / "undo").iterdir())) == 3
    h.close()
    assert [p.name for p in (tmp_path / "undo").iterdir()] == ["rebuild-bad"]
    assert not broken.dropped and not h.can_undo()


def test_a_step_pushed_takes_its_plugin_data_with_it():
    """A plugin's change of its data (part ``plugins``) is an entry of its own, unless the plugin pushed its own step
    in the same turn: then the step covers it (no second entry)."""
    state = {"chunk": 0, "plugins": {}}
    deferred = []
    h = UndoHistory(lambda: {k: dict(v) if isinstance(v, dict) else v for k, v in state.items()},
                    lambda parts, names, products: state.update(parts), lambda: None, defer=deferred.append)
    state["plugins"] = {"mine": 1}
    assert h.record() and h.undo_text() == "Dati dei plugin"

    class Step:
        def undo(self):
            pass

        def redo(self):
            pass

    h.push("Il mio passo", Step(), document=False)
    state["plugins"] = {"mine": 2}  # changed again in the same turn (a second group of a deletion)
    assert not h.record() and len(h.entries) == 2
    for f in deferred:  # the turn ends
        f()
    state["plugins"] = {"mine": 3}
    assert h.record() and len(h.entries) == 3


class _PluginStep:
    """A step a plugin pushes (its class is not the program's)."""

    def undo(self):
        pass

    def redo(self):
        pass


def _history(state, applied=None):
    deferred = []

    def set_state(parts, names, products):
        if applied is not None:
            applied.append(parts)
        state.update(parts)

    h = UndoHistory(lambda: {k: dict(v) if isinstance(v, dict) else v for k, v in state.items()}, set_state,
                    lambda: None, defer=deferred.append)
    return h, deferred


def _turn_ends(deferred):
    while deferred:
        deferred.pop(0)()


def test_an_entry_takes_the_plugin_data_changed_in_its_turn():
    """Units added for a plugin, then its data marked in the same turn: one entry, undone and redone as a whole; a
    change of the data in a later turn is an entry of its own."""
    state = {"chunk": 0, "units": [], "plugins": {"samgeo": {"units": 0}}}
    h, deferred = _history(state)
    state["units"] = ["u1", "u2"]
    assert h.record()
    state["plugins"] = {"samgeo": {"units": 2}}
    assert not h.record() and len(h.entries) == 1 and h.undo_text() == "Disegni"
    state["plugins"] = {"samgeo": {"units": 2, "made": [1, 2]}}  # once more in the same turn
    assert not h.record() and len(h.entries) == 1
    h.undo()
    assert state["units"] == [] and state["plugins"] == {"samgeo": {"units": 0}}
    h.redo()
    assert state["units"] == ["u1", "u2"] and state["plugins"] == {"samgeo": {"units": 2, "made": [1, 2]}}
    _turn_ends(deferred)
    state["plugins"] = {"samgeo": {"units": 3}}
    assert h.record() and len(h.entries) == 2 and h.undo_text() == "Dati dei plugin"


def test_a_key_changed_by_a_plugin_step_is_left_to_its_steps():
    """A plugin that pushes its own step for its key owns that key: a cache refreshed in it and marked without a step
    is no entry, and undoing an entry of another plugin's data does not put the key back behind its steps."""
    state = {"chunk": 0, "plugins": {"other": 1}}
    applied = []
    h, deferred = _history(state, applied)
    state["plugins"] = {"other": 1, "ship": {"items": [1]}}
    h.push("Relitto: proposte lette", _PluginStep(), document=False)
    assert h.step_keys == {"ship"} and len(h.entries) == 1
    _turn_ends(deferred)
    state["plugins"] = {"other": 1, "ship": {"items": [1], "cache": "plan"}}  # renamed nothing: a cache
    assert not h.record() and len(h.entries) == 1 and h.unchanged()
    state["plugins"] = {"other": 2, "ship": {"items": [1], "cache": "plan"}}
    assert h.record() and len(h.entries) == 2
    state["plugins"]["ship"]["items"] = [1, 2]  # the plugin's step after the other entry: its own
    h.push("Relitto: legno", _PluginStep(), document=False)
    _turn_ends(deferred)
    h.undo()  # the plugin's step (it undoes itself)
    h.undo()  # the other plugin's data: "ship" stays as the plugin's steps left it
    assert applied[-1] == {"plugins": {"other": 1}}  # "ship" not handed back to the window


def test_steps_of_the_program_and_of_the_project_claim_no_key():
    """Only a plugin's own step makes a key its own: the program's steps and the steps of the whole project (a script,
    a removal) leave the plugins' data in the snapshots."""
    state = {"chunk": 0, "plugins": {}}
    h, deferred = _history(state)
    program_step = type("OrientStep", (_PluginStep,), {"__module__": "pyarchinit_photogrammetry.gui.main_window"})
    state["plugins"] = {"a": 1}
    h.push("Orienta", program_step(), document=False)
    state["plugins"] = {"a": 1, "b": 1}
    h.push("Script", _PluginStep(), document=True)
    assert not h.step_keys
    _turn_ends(deferred)
    state["plugins"] = {"a": 2, "b": 2}
    assert h.record() and h.undo_text() == "Dati dei plugin"


def test_a_step_pushed_when_its_task_ends_claims_what_the_task_changed():
    """A plugin's background task writes its data, the window rebases when it ends (processing, not an edit), then the
    plugin pushes the step of that change in the same turn: the key is the step's. A rebase of an earlier turn says
    nothing about a later step."""
    state = {"chunk": 0, "plugins": {}}
    h, deferred = _history(state)
    state["plugins"] = {"ship": {"relief": 1}}
    h.rebase()
    h.push("Relitto: proposte lette", _PluginStep(), document=False)
    assert h.step_keys == {"ship"}
    _turn_ends(deferred)
    state["plugins"] = {"ship": {"relief": 1}, "mesh": 1}
    h.rebase()
    _turn_ends(deferred)
    h.push("Un altro passo", _PluginStep(), document=False)
    assert h.step_keys == {"ship"}


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


def test_script_entry_dropped(window):
    """A script's entry that falls out of the history: the versions kept for its undo go when its products are the
    active ones; what was put aside goes when they are not."""
    from pyarchinit_photogrammetry.core import versions
    from pyarchinit_photogrammetry.gui.undo import ScriptAction, files_under, snapshot_document

    w, app = window
    ch = w.chunk
    kept = len(versions.versions(ch, "dem"))
    snap = snapshot_document(w.doc)
    assert set(snap) >= {ch.key, "meta", "order"} and snap[ch.key]["products"]["dem"] == ch.products["dem"]
    assert not any("versions" in f.parts for f in files_under(w.doc.files_dir))
    assert w.run_script_source("chunk.build_dem(resolution=0.02)", "DEM nuovo")
    w.runner.wait()
    app.processEvents()
    action = w.history.entries[-1].action
    assert isinstance(action, ScriptAction) and len(versions.versions(ch, "dem")) == kept + 1
    w.history.reset()  # the entry falls out while the script's DEM is the active one: its version goes
    assert len(versions.versions(w.chunk, "dem")) == kept
    assert not (w.chunk.data_dir / "versions" / "dem" / action.versions[0][2]).exists()
    assert w.run_script_source("chunk.build_dem(resolution=0.03)", "DEM nuovo")
    w.runner.wait()
    app.processEvents()
    action = w.history.entries[-1].action
    w.history.undo()
    aside = w.chunk.data_dir / "versions" / "dem" / f"{action.versions[0][2]}-script"
    assert aside.exists()  # the script's DEM put aside, for Ripeti
    w.history.reset()
    assert not aside.exists() and len(versions.versions(w.chunk, "dem")) == kept


def test_a_tin_built_again_by_a_script_is_undone(window):
    """The TIN (tin.npz and tin.json) written again by a script: Annulla puts the old files and meta back, Ripeti the
    new ones."""
    from pyarchinit_photogrammetry.engine import tin as T

    w, app = window
    ch = w.chunk
    T.build_tin(ch, source="dem", max_error=0.05)
    w.doc.save()
    w.history.reset()
    folder = ch.data_dir / "tin"
    old = {p.name: p.read_bytes() for p in folder.iterdir()}
    old_meta = dict(ch.meta["tin"])
    assert set(old) == {"tin.npz", "tin.json"}
    assert w.run_script_source("from pyarchinit_photogrammetry.engine import tin as T\n"
                               "T.build_tin(chunk, source='dem', max_error=0.01)", "TIN nuovo")
    w.runner.wait()
    app.processEvents()
    new = {p.name: p.read_bytes() for p in folder.iterdir()}
    new_meta = dict(w.chunk.meta["tin"])
    assert new != old and new_meta["n_vertices"] != old_meta["n_vertices"]
    w.undo()
    ch = w.chunk
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == old and ch.meta["tin"] == old_meta
    assert T.chunk_tin(ch).n_vertices == old_meta["n_vertices"]
    w.redo()
    ch = w.chunk
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == new and ch.meta["tin"] == new_meta
    assert T.chunk_tin(ch).n_vertices == new_meta["n_vertices"]


def test_versions_of_a_saved_script_are_kept_and_missing_ones_pruned(window, caplog):
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.core import versions

    w, app = window
    kept = len(versions.versions(w.chunk, "dem"))
    assert w.run_script_source("chunk.build_dem(resolution=0.02)", "DEM nuovo")
    w.runner.wait()
    app.processEvents()
    vid = w.history.entries[-1].action.versions[0][2]
    w.doc.save()  # saved after the script: the saved project lists the version
    w.history.reset()
    assert len(versions.versions(w.chunk, "dem")) == kept + 1
    assert versions.version_file(w.chunk, "dem", vid).exists()
    shutil.rmtree(w.chunk.data_dir / "versions" / "dem" / vid)
    with caplog.at_level("WARNING"):
        again = ppg.Document.open(w.doc.path)
    assert all(e["id"] != vid for e in versions.versions(again.chunk, "dem"))
    assert any(vid in m for m in caplog.messages)


def test_undo_part_free(window):
    """The free drawings are a part of the snapshots, "Disegni 3D": added, then undone and redone."""
    from pyarchinit_photogrammetry.core import free_drawings as fd

    w, app = window
    ch = w.chunk
    w._dirty(True)
    rec = fd.add(ch, "line", [[2480001.0, 4500001.0, 99.0], [2480002.0, 4500001.5, 99.2]], layer="Muri")
    fd.set_layer(ch, "Muri", colour="#00ff00")
    w._dirty(True)
    assert w.a_undo.text() == "Annulla: Disegni 3D"
    w.undo()
    assert fd.features(ch) == [] and [x["name"] for x in fd.layers(ch)] == [fd.DEFAULT_LAYER]
    w.redo()
    assert [f["id"] for f in fd.features(ch)] == [rec["id"]] and fd.layers(ch)[1]["colour"] == "#00ff00"
    w._store_drawings()  # the window writes its drawings into the chunk: the free ones stay
    assert [f["id"] for f in fd.features(ch)] == [rec["id"]]


def test_undo_across_a_follow_keeps_them_on_the_model(window):
    """Edited, then the transform changes (the drawings follow it), then the edit undone: the drawing comes back with
    the basis it was written in and follows the transform of now - on the model, not where the old numbers are."""
    from pyarchinit_photogrammetry.core import free_drawings as fd

    w, app = window
    ch = w.chunk
    w._dirty(True)
    xyz = [[2480001.0, 4500001.0, 99.0], [2480002.0, 4500001.5, 99.2]]
    rec = fd.add(ch, "line", xyz)
    w._dirty(True)
    inside = geometry_internal(ch.transform, xyz)
    fd.update(ch, rec["id"], name="asse")
    w._dirty(True)
    shift = np.eye(4)
    shift[:3, 3] = [0.05, -0.04, 0.03]  # the georeference fitted again (7.7 cm)
    ch.transform = shift @ ch.transform
    fd.follow(ch)
    w.history.rebase()  # processing, not an edit
    w.undo()  # the name back
    got = fd.features(ch)[0]
    assert got["name"] != "asse"
    assert np.abs(geometry_internal(ch.transform, got["xyz"]) - inside).max() < 1e-6


def geometry_internal(transform, xyz):
    from pyarchinit_photogrammetry.core import geometry as geo

    return geo.apply(np.linalg.inv(np.asarray(transform, float)), np.asarray(xyz, float).reshape(-1, 3))


def test_the_undo_copies_go_when_the_window_closes(window):
    """While the window is open the copies of the steps that can be undone stay; closing it deletes them (they used to
    stay until the next open or switch of chunk)."""
    w, app = window
    ch = w.chunk
    w.history.backup(["point_cloud"])
    w.history.record("Lazo sulla nuvola")
    hold = ch.data_dir / "undo" / "orient-test"
    hold.mkdir(parents=True)
    (hold / "proxy.ply").write_text("x")
    w._add_quota({"x": 2480001.0, "y": 4500001.0, "quota_q": 99.5, "us_q": "3"})
    copies = ch.data_dir / "undo"
    assert copies.is_dir() and len(list(copies.iterdir())) == 2 and w.a_undo.isEnabled()
    w.setWindowModified(False)
    w.close()
    app.processEvents()
    assert not copies.exists()


def test_a_plugin_change_of_chunk_meta_is_undone(window):
    """``mark_modified`` after a plugin wrote its key of ``chunk.meta``: *Annulla* takes it back, *Ripeti* again."""
    w, app = window
    ch = w.chunk
    told = []
    w.refreshed.connect(lambda: told.append(True))
    ch.meta["fake_plugin"] = {"n": 1, "names": ["a"]}
    w.mark_modified()
    assert w.a_undo.text() == "Annulla: Dati dei plugin"
    app.processEvents()  # another action of the user: a turn of the event loop later (in the same turn it would join)
    ch.meta["fake_plugin"]["n"] = 2
    w.mark_modified()
    w.undo()
    assert ch.meta["fake_plugin"] == {"n": 1, "names": ["a"]} and told
    w.undo()
    assert "fake_plugin" not in ch.meta
    w.redo()
    w.redo()
    assert ch.meta["fake_plugin"] == {"n": 2, "names": ["a"]}


def test_a_window_without_a_document_closes(window):
    """A script or a plugin's test took the document away (``application.document = None``): the window still closes,
    the copies of the steps of the history it can reach are let go."""
    from PySide6.QtGui import QCloseEvent

    from pyarchinit_photogrammetry import app as application

    w, app = window
    doc = application.document
    w.history.backup(["point_cloud"])
    w.history.record("Lazo sulla nuvola")
    try:
        application.document = None
        assert w.chunk is None
        w.setWindowModified(False)
        ev = QCloseEvent()
        w.closeEvent(ev)  # 1.7.3: AttributeError 'NoneType' object has no attribute 'chunks'
        assert ev.isAccepted() and not w.history.can_undo()
    finally:
        application.document = doc


def test_a_plugin_action_is_one_undo_entry_or_none(window):
    """As a plugin uses the window: units added for it and its data marked in the same callback are one entry (undone
    together); its own step plus ``mark_modified`` is one entry; a key its steps own, refreshed as a cache and marked
    without a step, is no entry - in a later turn too."""
    from shapely.geometry import box

    from pyarchinit_photogrammetry.plugins import Context

    w, app = window
    ch = w.chunk
    ctx = Context("fake", w.chunk.data_dir / "fake_plugin", window=w)
    ch.meta["fake_runs"] = {"units": 0}  # a run of the plugin, marked when it was found
    ctx.mark_modified()
    app.processEvents()
    entries = len(w.history.entries)
    x, y = 2480001.0, 4500001.0
    before = len(w.units)
    assert ctx.add_units([box(x + 0.1, y + 0.1, x + 0.3, y + 0.3)]) == 1
    ch.meta["fake_runs"]["units"] = 1
    ctx.mark_modified()
    assert len(w.history.entries) == entries + 1 and len(w.units) == before + 1
    w.undo()
    assert len(w.units) == before and ch.meta["fake_runs"] == {"units": 0}
    w.redo()
    assert len(w.units) == before + 1 and ch.meta["fake_runs"] == {"units": 1}
    app.processEvents()

    ch.meta["fake_ship"] = {"items": [1]}
    w.history.push("Relitto: proposte lette", _PluginStep(), document=False)
    ctx.mark_modified()
    assert len(w.history.entries) == entries + 2 and w.a_undo.text() == "Annulla: Relitto: proposte lette"
    app.processEvents()
    ch.meta["fake_ship"]["cache"] = "plan"  # renamed nothing: only a cache of the plugin
    ctx.mark_modified()
    assert len(w.history.entries) == entries + 2 and w.isWindowModified()
    app.processEvents()
    ch.meta["fake_runs"]["units"] = 5  # the other plugin's data: an entry, undone without touching "fake_ship"
    ctx.mark_modified()
    assert len(w.history.entries) == entries + 3
    w.undo()
    assert ch.meta["fake_runs"] == {"units": 1} and ch.meta["fake_ship"] == {"items": [1], "cache": "plan"}
