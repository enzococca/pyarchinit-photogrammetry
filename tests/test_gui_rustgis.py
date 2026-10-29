"""The window with RustGIS both ways: QGIS > Apri in RustGIS gives RustGIS the drawings and the products of the active
chunk (the live GeoPackage of the QGIS link written first), the live link looks at RustGIS every 2 s in a thread of its
own - what is drawn, changed or deleted there comes into the window with one step of Modifica > Annulla, what the
program writes goes there -, says when RustGIS does not answer, and reads RustGIS's last edits into the chunk they
belong to before the active chunk changes. RustGIS is the fake engine of tests/test_rustgis_link.py (the rules
rustgis-cli 0.4.0 showed) behind the window's transport: the real app answers only once its user ticked its agent
control (ruling P6)."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

import pytest
from shapely.geometry import Point, Polygon, box

from pyarchinit_photogrammetry.io import gpkg
from pyarchinit_photogrammetry.io import rustgis_link as rg
from pyarchinit_photogrammetry.io.qgis_link import ID_FIELD, LIVE_DB, PART_FIELD

from test_rustgis_link import FakeEngine, _commands, _reprojected

GB = "EPSG:3004"
GX, GY = 2480000.0, 4500000.0  # the trench of the window's tests (conftest.TRENCH_ORIGIN)
REAL_TRANSPORT = rg.HttpTransport


@pytest.fixture
def rustgis(monkeypatch, tmp_path):
    """RustGIS installed and running, its agent control ticked, as the window finds it: its address published, the fake
    engine answering behind the transport the window makes (``fake.timeout``: the wait it was made with, as the window
    sets it after). ``opened`` gets each start of the app (none happens)."""
    fake = FakeEngine()
    opened = []

    def transport(endpoint, timeout=30.0):
        fake.timeout = timeout
        return fake

    monkeypatch.setattr(rg, "find_rustgis", lambda: rg.RustGisInstall(Path("/Applications/RustGIS.app"), None,
                                                                       "0.4.0"))
    monkeypatch.setattr(rg, "open_app", lambda paths=(): opened.append(list(paths)))
    monkeypatch.setattr(rg, "endpoint", lambda: rg.Endpoint("http://127.0.0.1:9/mcp", tmp_path / "mcp-http.token"))
    monkeypatch.setattr(rg, "HttpTransport", transport)
    return fake, opened


def _settle(w, app, timeout: float = 30.0) -> None:
    """Until the calls the window sent to RustGIS, and a start of the link, are done and their answers are in the
    window (they come back as queued calls)."""
    end = time.monotonic() + timeout
    calm = 0
    while time.monotonic() < end:
        app.processEvents()
        calm = calm + 1 if not w._rustgis_busy() and not w.runner.busy else 0
        if calm >= 3:
            return
        time.sleep(0.01)
    raise AssertionError("the calls to RustGIS did not end")


def _linked(w, app):
    """QGIS > Apri in RustGIS, waited for; the looks every 2 s left to the test (it looks with :func:`_look`)."""
    w.open_in_rustgis()
    _settle(w, app)
    assert w._rustgis is not None, _log(w)[-5:]
    w._rustgis_poll.stop()
    return w._rustgis


def _look(w, app) -> None:
    w._poll_rustgis()
    _settle(w, app)


def _log(w) -> list[str]:
    t = w.log_pane.table
    return [t.item(r, 2).text() for r in range(t.rowCount())]


def _unit(us: str, geometry):
    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon

    return UnitPolygon(geometry, us=us, area="1", sito="Saggio")


def test_open_in_rustgis_starts_the_link(trench, rustgis, monkeypatch):
    """QGIS > Apri in RustGIS with RustGIS answering: the live GeoPackage written, the link started with the rasters the
    QGIS project gets - the orthophoto shown, the DEM once (the shaded DEM is the way QGIS draws it) and hidden (RustGIS
    draws a layer over every tile layer) -, in a group named after the chunk; the live action checked and both looks
    running; the app not started (it runs). Turning the action off stops the link: RustGIS keeps its layers."""
    w, app, mw = trench
    fake, opened = rustgis
    w._add_unit(_unit("6", box(GX + 1, GY + 1, GX + 2, GY + 2)))
    given = []
    real = rg.RustGisLink.start
    monkeypatch.setattr(rg.RustGisLink, "start",
                        lambda self, rasters=(), progress=None: given.append(list(rasters)) or real(self, rasters,
                                                                                                  progress))
    assert w.a_rustgis_open.text() == "Apri in RustGIS (collegamento dal vivo)"
    assert w.a_rustgis_live.text() == "Collegamento dal vivo con RustGIS (nei due sensi)"
    assert w.a_rustgis_live.isCheckable() and not w.a_rustgis_live.isChecked()
    menu = next(a.menu() for a in w.menuBar().actions() if a.menu() and w.a_qgis_open in a.menu().actions())
    assert w.a_rustgis_open in menu.actions() and w.a_rustgis_live in menu.actions()
    w.open_in_rustgis()
    _settle(w, app)
    ch = w.chunk
    assert (w._qgis_dir() / LIVE_DB).exists()
    assert given == [[{"path": str(ch.orthomosaic_path), "name": "Ortofoto", "hidden": False},
                      {"path": str(ch.dem_path), "name": "DEM", "hidden": True}]]
    rl = w._rustgis
    assert rl is not None and rl.chunk_label == "Saggio 1" and rl.copies == ch.data_dir / "rustgis"
    assert w.a_rustgis_live.isChecked() and w._rustgis_poll.isActive() and w._qgis_poll.isActive()
    assert opened == [] and [g["name"] for g in fake.groups] == ["Saggio 1"]
    assert next(layer for layer in fake.layers if layer["name"] == "DEM")["visible"] is False
    assert next(layer for layer in fake.layers if layer["name"] == "Ortofoto").get("visible", True)
    assert fake.identities(fake.linked(rl, "unita")) == [w.units[0].attributes[ID_FIELD]]
    assert any("RustGIS" in line and "Saggio 1" in line for line in _log(w))
    w.a_rustgis_live.setChecked(False)
    _settle(w, app)
    assert w._rustgis is None and rl.layers == {} and not w._rustgis_poll.isActive() and not w._qgis_poll.isActive()
    assert [g["name"] for g in fake.groups] == ["Saggio 1"]  # RustGIS keeps them


def test_a_unit_edited_in_rustgis_comes_back_with_one_undo(trench, rustgis):
    """A unit reshaped in RustGIS: the next look pulls it into the live GeoPackage and the window takes it as it takes
    QGIS's edits - its polygon RustGIS's (within the millimetre: EPSG:3004 is a datum shift), one entry of Modifica >
    Annulla, "Da RustGIS" in the log. What goes back is only what the program makes of it - its area computed again -,
    once, on RustGIS's polygon; one Annulla puts it back."""
    w, app, mw = trench
    fake, _ = rustgis
    six = box(GX + 1, GY + 1, GX + 2, GY + 2)
    w._add_unit(_unit("6", six))
    rl = _linked(w, app)
    pid = w.units[0].attributes[ID_FIELD]
    moved = box(GX + 1, GY + 1, GX + 2.5, GY + 2)
    fake.reshape(fake.linked(rl, "unita"), pid, moved, GB)
    _look(w, app)
    w._poll_qgis()
    (u,) = w.units
    assert u.attributes[ID_FIELD] == pid and u.geometry.hausdorff_distance(moved) < rg.TOLERANCE_M
    assert w.history.undo_text() == "Da RustGIS: US 6 modificata" and w.isWindowModified()
    assert "Da RustGIS: US 6 modificata" in _log(w)
    assert [d["us"] for d in w.chunk.meta["drawings"]["units"]] == ["6"]
    fake.scripts.clear()
    w._export_qgis_layers()  # the window writes its version of it: RustGIS's polygon, its area computed again
    _look(w, app)
    (script,) = fake.scripts
    (row,) = [p for c, p in _commands(script) if c == "edit.addFeature" and p["attributes"].get(ID_FIELD) == pid]
    assert row["attributes"]["area_m2"] == pytest.approx(1.5, abs=0.001)
    (there,) = fake.native(fake.linked(rl, "unita"), pid)
    assert there.hausdorff_distance(moved) < rg.TOLERANCE_M
    fake.scripts.clear()
    _look(w, app)
    assert fake.scripts == []  # sent once
    w.undo()
    assert len(w.units) == 1 and w.units[0].geometry.equals(six)


def test_local_chunk_and_agent_control_off(trench, rustgis, monkeypatch):
    """A chunk in a local frame: said, and RustGIS is never asked. RustGIS installed, its agent control off - the token
    file it names is not there: the window starts the app, waits for it (60 s; here a moment) and then says where the
    box to tick is. The live action stays off."""
    w, app, mw = trench
    fake, opened = rustgis
    shown, given = [], []
    monkeypatch.setattr(mw, "message", lambda parent, kind, title, text, *a, **k: shown.append(text))
    monkeypatch.setattr(rg.RustGisLink, "start", lambda self, rasters=(), progress=None: given.append(rasters))
    w.chunk.crs = "LOCAL"
    w.open_in_rustgis()
    _settle(w, app)
    assert len(shown) == 1 and "locale" in shown[0] and given == [] and fake.calls == [] and opened == []
    assert not w.a_rustgis_live.isChecked() and w._rustgis is None
    w.chunk.crs = GB
    monkeypatch.setattr(rg, "HttpTransport", REAL_TRANSPORT)
    monkeypatch.setattr(mw, "RUSTGIS_WAIT", 0.3)
    w.open_in_rustgis()
    _settle(w, app)
    assert opened == [[]] and given == [] and fake.calls == []
    assert not w.a_rustgis_live.isChecked() and w._rustgis is None
    assert len(shown) == 2 and "Let your Agents Control RustGIS" in shown[1] and "AI Assistant" in shown[1]
    monkeypatch.setattr(rg, "find_rustgis", lambda: None)  # and not even installed: where to download it
    monkeypatch.setattr(rg, "endpoint", lambda: None)
    w.open_in_rustgis()
    _settle(w, app)
    assert len(shown) == 3 and rg.RELEASES_URL in shown[2] and opened == [[]]


def test_busy_says_waiting(trench, rustgis):
    """RustGIS not drawing (minimized, hidden) when the program sends a unit moved here: the status bar says it waits
    for RustGIS, nothing is lost; the next look sends it, once."""
    w, app, mw = trench
    fake, _ = rustgis
    w._add_unit(_unit("6", box(GX + 1, GY + 1, GX + 2, GY + 2)))
    rl = _linked(w, app)
    pid = w.units[0].attributes[ID_FIELD]
    moved = box(GX + 1, GY + 1, GX + 2.5, GY + 2)
    w.units[0].geometry = moved
    w._redraw_overlays()
    w._export_qgis_layers()
    fake.scripts.clear()
    fake.busy.append("run")
    _look(w, app)
    assert "In attesa di RustGIS" in w.statusBar().currentMessage()
    assert fake.scripts == [] and rl.pending
    _look(w, app)
    assert len(fake.scripts) == 1 and not rl.pending
    (there,) = fake.native(fake.linked(rl, "unita"), pid)
    assert there.hausdorff_distance(moved) < rg.TOLERANCE_M
    assert "In attesa di RustGIS" not in w.statusBar().currentMessage()
    _look(w, app)
    assert len(fake.scripts) == 1  # sent once


def test_chunk_switch_pulls_first(trench, rustgis):
    """A unit drawn in RustGIS and not read yet when another chunk becomes the active one: it goes into the chunk it was
    drawn on, none into the new one; the chunk left goes from RustGIS (its layers, its group) and the new one comes,
    with a link of its own; back on the first chunk, the unit is there."""
    w, app, mw = trench
    fake, _ = rustgis
    first = w.chunk
    second = w.doc.add_chunk("Saggio 2")
    second.crs = GB
    w._add_unit(_unit("6", box(GX + 1, GY + 1, GX + 2, GY + 2)))
    rl = _linked(w, app)
    fake.draw(fake.linked(rl, "unita"), box(GX + 3, GY + 1, GX + 3.5, GY + 1.5), GB, us_s="12", area_s="1")
    w._activate_chunk(second)
    _settle(w, app)
    assert sorted(d["us"] for d in first.meta["drawings"]["units"]) == ["12", "6"]
    assert not (second.meta.get("drawings") or {}).get("units") and w.units == []
    assert rl.layers == {} and w._rustgis is not None and w._rustgis is not rl
    assert w._rustgis.chunk_label == "Saggio 2" and [g["name"] for g in fake.groups] == ["Saggio 2"]
    assert fake.identities(fake.linked(w._rustgis, "unita")) == []
    assert w.a_rustgis_live.isChecked()
    w._activate_chunk(first)
    _settle(w, app)
    assert sorted(u.us for u in w.units) == ["12", "6"] and [g["name"] for g in fake.groups] == ["Saggio 1"]
    assert len(fake.identities(fake.linked(w._rustgis, "unita"))) == 2


def test_closing_reads_rustgis_last_edits_first(trench, rustgis, monkeypatch):
    """Closing the window with a unit drawn in RustGIS and not read yet: it comes in before the question about saving
    the project, so that it is saved with it; the window stays when the question is cancelled, and the link with it."""
    from PySide6.QtWidgets import QMessageBox

    w, app, mw = trench
    fake, _ = rustgis
    rl = _linked(w, app)
    fake.draw(fake.linked(rl, "unita"), box(GX + 3, GY + 1, GX + 3.5, GY + 1.5), GB, us_s="12", area_s="1")
    asked = []
    monkeypatch.setattr(mw, "message", lambda parent, kind, title, text, *a, **k: asked.append(text)
                        or QMessageBox.StandardButton.Cancel)
    w.setWindowModified(False)
    assert not w.close()
    assert asked == ["Salvare il progetto prima di continuare?"] and [u.us for u in w.units] == ["12"]
    assert w._rustgis is rl and rl.layers


def test_back_to_the_window_reads_rustgis(trench, rustgis):
    """What is edited in RustGIS without changing its edit status (a feature edited twice): read when the program
    comes back to the front, and every 10 s while RustGIS lists edits in the linked layers."""
    from PySide6.QtCore import Qt

    w, app, mw = trench
    fake, _ = rustgis
    w._add_unit(_unit("6", box(GX + 1, GY + 1, GX + 2, GY + 2)))
    rl = _linked(w, app)
    pid = w.units[0].attributes[ID_FIELD]
    layer = fake.linked(rl, "unita")
    fake.reshape(layer, pid, box(GX + 1, GY + 1, GX + 2.5, GY + 2), GB)
    _look(w, app)
    status = json.dumps(fake.status, sort_keys=True)

    def edited_again(geometry):  # the feature changed again, RustGIS's count of modified features as it was
        (feature,) = [f for f in layer["features"] if f["attrs"].get(ID_FIELD) == pid]
        feature["geometry"] = _reprojected(geometry, GB, "EPSG:4326")

    third = box(GX + 1, GY + 1, GX + 3, GY + 2)
    edited_again(third)
    assert json.dumps(fake.status, sort_keys=True) == status
    _look(w, app)
    assert w.units[0].geometry.hausdorff_distance(third) > 0.4  # the status as it was: not read yet
    w._app_state_changed(Qt.ApplicationState.ApplicationActive)
    _settle(w, app)
    assert w.units[0].geometry.hausdorff_distance(third) < rg.TOLERANCE_M
    fourth = box(GX + 1, GY + 1, GX + 3.5, GY + 2)
    edited_again(fourth)
    w._rustgis_last_pull -= 11  # ten seconds and more since the last pull, RustGIS listing edits of the units
    _look(w, app)
    assert w.units[0].geometry.hausdorff_distance(fourth) < rg.TOLERANCE_M


def test_products_rebuilt_are_given_again(trench, rustgis):
    """The DEM rebuilt while the link is on: at the next look its layer in RustGIS is replaced (RustGIS keeps what it
    loaded of a file; one read by address would no longer answer), hidden as before - its file is another, whether or
    not the window showed the chunk again -; the orthophoto stays the layer it was. Shown again with nothing changed,
    the chunk's rasters cost RustGIS nothing."""
    import numpy as np

    from pyarchinit_photogrammetry.engine.dem import write_geotiff

    w, app, mw = trench
    fake, _ = rustgis
    rl = _linked(w, app)
    ortho, dem = rl.rasters
    path = w.chunk.dem_path
    write_geotiff(path, np.full((300, 300), 101.0, np.float32), (GX, GY + 6), 0.02, GB, nodata=-9999.0)
    later = path.stat().st_mtime_ns + 10**9
    os.utime(path, ns=(later, later))
    _look(w, app)
    assert rl.rasters[0] is ortho and rl.rasters[1] is not dem and rl.rasters[1].name == "DEM"
    assert fake.layer(int(rl.rasters[1].layer))["visible"] is False
    assert int(dem.layer) not in [layer["id"] for layer in fake.layers]
    assert any("RustGIS" in line and "DEM" in line for line in _log(w))
    given = list(rl.rasters)
    fake.calls.clear()
    w.refresh_all()
    _look(w, app)
    assert rl.rasters == given and [args.get("command") for _, args in fake.calls] == ["edit.status"]


def test_what_rustgis_refuses_is_said_once(trench, rustgis):
    """A unit RustGIS refuses (a rule of its layer): said in the log once, naming the layer and the unit, and not sent
    again by the looks after while the program does not change it (no send in a loop)."""
    w, app, mw = trench
    fake, _ = rustgis
    rl = _linked(w, app)
    fake.rules.append(lambda command, p: "the units of this layer cannot be 13"
                      if command == "edit.addFeature" and (p.get("attributes") or {}).get("us_s") == "13" else None)
    w._add_unit(_unit("13", box(GX + 3, GY + 1, GX + 4, GY + 2)))
    w._export_qgis_layers()
    _look(w, app)
    said = [line for line in _log(w) if "RustGIS" in line and "US 13" in line]
    assert len(said) == 1 and "Unita' stratigrafiche (ppg)" in said[0]
    assert rl.set_aside == {"unita": [w.units[0].attributes[ID_FIELD]]}
    fake.calls.clear()
    fake.scripts.clear()
    for _ in range(3):
        _look(w, app)
    assert fake.scripts == [] and [tool for tool, _ in fake.calls] == ["run_command"] * 3  # its status, no more
    assert [line for line in _log(w) if "RustGIS" in line and "US 13" in line] == said


def test_a_start_rustgis_did_not_finish_says_so(trench, rustgis, monkeypatch):
    """RustGIS stops answering in the middle of the start (the tables and the rasters added, the group not made): the
    link does not start, the live action stays off, and the window names the layers to take away by hand if RustGIS
    shows them twice. RustGIS gone in the middle of it (closed meanwhile) says that it stopped answering during the
    start, never that its agent control is off."""
    w, app, mw = trench
    fake, _ = rustgis
    shown = []
    monkeypatch.setattr(mw, "message", lambda parent, kind, title, text, *a, **k: shown.append(text))
    fake.busy.append("run_command:group.add")
    w.open_in_rustgis()
    _settle(w, app)
    assert w._rustgis is None and not w.a_rustgis_live.isChecked()
    (text,) = shown
    assert "RustGIS non rispondeva" in text and "togli a mano" in text
    assert "Unita' stratigrafiche (ppg)" in text and "Ortofoto" in text and "DEM" in text

    def closed(tool, args):
        if args.get("command") == "group.add":
            raise rg.RustGisOff("nothing answers at http://127.0.0.1:9/mcp")

    fake.hooks.append(closed)
    w.open_in_rustgis()
    _settle(w, app)
    assert w._rustgis is None and not w.a_rustgis_live.isChecked() and len(shown) == 2
    assert "ha smesso di rispondere durante l'avvio" in shown[1] and "Let your Agents" not in shown[1]
    assert "togli a mano" in shown[1] and "Unita' stratigrafiche (ppg)" in shown[1]


def test_a_layer_removed_in_rustgis_comes_back_and_the_others_are_read(trench, rustgis):
    """The units' layer removed in RustGIS while a spot height is drawn there: the pull refuses (a linked layer is
    gone), so the look sends first - the layer added again, with its rows - and pulls again, once: the spot height comes
    in, the layer of the units is there once."""
    w, app, mw = trench
    fake, _ = rustgis
    w._add_unit(_unit("6", box(GX + 1, GY + 1, GX + 2, GY + 2)))
    rl = _linked(w, app)
    fake.remove(int(rl.layers["unita"]))
    fake.draw(fake.linked(rl, "quote"), Point(GX + 0.5, GY + 0.5), GB, quota_q=100.5, us_q="6")
    _look(w, app)
    assert [q["quota_q"] for q in w.quote] == [100.5] and "Da RustGIS: quota 100.50 aggiunta" in _log(w)
    assert [layer["name"] for layer in fake.layers].count("Unita' stratigrafiche (ppg)") == 1
    assert fake.identities(fake.linked(rl, "unita")) == [w.units[0].attributes[ID_FIELD]]


def test_a_unit_the_window_has_is_never_added_twice(trench):
    """A row under an identity the live GeoPackage's reader never knew comes as added (ruling T2-3); when the window
    holds that identity already - the program started again, unsaved, between deleting the unit and a pull that brought
    it back - it is a change of that unit, never a second one."""
    w, app, mw = trench
    w._add_unit(_unit("6", box(GX + 1, GY + 1, GX + 2, GY + 2)))
    w.a_qgis_live.setChecked(True)
    w._export_qgis_layers()
    w._poll_qgis()
    pid = w.units[0].attributes[ID_FIELD]
    path = w._qgis_dir() / LIVE_DB
    w._live_link.close()
    w._live_link = None  # a reader that knows nothing of the file, as after a start
    moved = box(GX + 1, GY + 1, GX + 3, GY + 2)
    con = sqlite3.connect(path, isolation_level=None)
    try:
        srs = con.execute("SELECT srs_id FROM gpkg_geometry_columns WHERE table_name='unita'").fetchone()[0]
        con.execute("BEGIN IMMEDIATE")
        con.execute(f'DELETE FROM "unita" WHERE "{ID_FIELD}" = ?', (pid,))
        con.execute(f'INSERT INTO "unita" (geom, us_s, area_s, "{ID_FIELD}", "{PART_FIELD}") VALUES (?, ?, ?, ?, ?)',
                    (gpkg.blob(moved, srs), "6", "1", pid, 1))
        con.execute("COMMIT")
    finally:
        con.close()
    w._poll_qgis()
    (u,) = w.units
    assert u.attributes[ID_FIELD] == pid and u.geometry.equals(moved)


def test_open_again_gives_the_chunk_again(trench, rustgis):
    """QGIS > Apri in RustGIS while linked: RustGIS's edits read first, the chunk's layers taken away and given again
    once the old link stopped - one group in RustGIS, never two."""
    w, app, mw = trench
    fake, _ = rustgis
    rl = _linked(w, app)
    fake.draw(fake.linked(rl, "unita"), box(GX + 3, GY + 1, GX + 3.5, GY + 1.5), GB, us_s="12", area_s="1")
    w.open_in_rustgis()
    _settle(w, app)
    assert [u.us for u in w.units] == ["12"] and "Da RustGIS: US 12 aggiunta" in _log(w)
    assert w._rustgis is not None and w._rustgis is not rl and rl.layers == {}
    assert [g["name"] for g in fake.groups] == ["Saggio 1"]
    assert fake.identities(fake.linked(w._rustgis, "unita")) == [w.units[0].attributes[ID_FIELD]]


def test_a_refused_pull_holds_the_sends(trench, rustgis):
    """RustGIS refusing to give its edits (an export it refuses), said once: nothing is sent at that look, though a
    unit was moved here before it, nor while the pull waits (10 s) - RustGIS's edits are read before the program's go
    there (only a layer gone there is added again first) - and the program's rows are kept; they go after the next
    pull."""
    w, app, mw = trench
    fake, _ = rustgis
    w._add_unit(_unit("6", box(GX + 1, GY + 1, GX + 2, GY + 2)))
    rl = _linked(w, app)
    pid = w.units[0].attributes[ID_FIELD]
    fake.rules.append(lambda command, p: "exports are switched off" if command == "share.exportLayer" else None)
    fake.draw(fake.linked(rl, "quote"), Point(GX + 0.5, GY + 0.5), GB, quota_q=100.5, us_q="6")
    moved = box(GX + 1, GY + 1, GX + 2.5, GY + 2)
    w.units[0].geometry = moved
    w._redraw_overlays()
    w._export_qgis_layers()
    fake.scripts.clear()
    _look(w, app)
    refused = [line for line in _log(w) if line.startswith("RustGIS ha rifiutato")]
    assert len(refused) == 1 and "exports are switched off" in refused[0]
    assert fake.scripts == [] and rl.pending  # the refused pull holds the send
    _look(w, app)
    assert fake.scripts == [] and w.quote == []  # neither sent nor read
    assert [line for line in _log(w) if line.startswith("RustGIS ha rifiutato")] == refused
    fake.rules.clear()
    w._rustgis_refused_at -= 11
    _look(w, app)
    assert [q["quota_q"] for q in w.quote] == [100.5]
    (there,) = fake.native(fake.linked(rl, "unita"), pid)
    assert there.hausdorff_distance(moved) < rg.TOLERANCE_M


def test_chunk_switch_while_rustgis_does_not_answer_keeps_its_layers(trench, rustgis):
    """RustGIS not answering when the active chunk changes: what was edited there cannot be read, so the layers of the
    chunk left stay in RustGIS with it - unlinked, said in the log, never taken away - and the new chunk comes beside
    them."""
    w, app, mw = trench
    fake, _ = rustgis
    second = w.doc.add_chunk("Saggio 2")
    second.crs = GB
    rl = _linked(w, app)
    fake.draw(fake.linked(rl, "unita"), box(GX + 3, GY + 1, GX + 3.5, GY + 1.5), GB, us_s="12", area_s="1")
    fake.busy.append("run_command:edit.status")  # the first call of the pull: RustGIS does not draw
    w._activate_chunk(second)
    _settle(w, app)
    assert sorted(g["name"] for g in fake.groups) == ["Saggio 1", "Saggio 2"] and rl.layers == {}
    assert any(line.startswith("Le ultime modifiche fatte in RustGIS") and "Saggio 1" in line for line in _log(w))
    assert not (w.doc.chunks[0].meta.get("drawings") or {}).get("units") and w.units == []
    assert w._rustgis is not None and w._rustgis.chunk_label == "Saggio 2"


def test_rustgis_gone_stops_the_link(trench, rustgis, monkeypatch):
    """RustGIS closed (or its agent control taken away) while linked: the next look stops the link, unchecks the live
    action and says it in a window."""
    w, app, mw = trench
    fake, _ = rustgis
    shown = []
    monkeypatch.setattr(mw, "message", lambda parent, kind, title, text, *a, **k: shown.append(text))
    rl = _linked(w, app)

    def gone(tool, args):
        raise rg.RustGisOff("nothing answers at http://127.0.0.1:9/mcp")

    fake.hooks.append(gone)
    _look(w, app)
    assert w._rustgis is None and not w.a_rustgis_live.isChecked() and rl.layers == {}
    assert len(shown) == 1 and "non risponde più" in shown[0]


def _other_chunk(w):
    second = w.doc.add_chunk("Saggio 2")
    second.crs = GB
    return second


def _stuck(fake):
    """RustGIS answering nothing to the next call made to it (the window's last pull) until ``release`` is set: a
    function of the fake engine's hooks, and the event."""
    release, stuck = threading.Event(), []

    def hook(tool, args):
        if not stuck:
            stuck.append(args.get("command") or tool)
            release.wait(30)

    fake.hooks.append(hook)
    return release


def test_opening_another_project_reads_rustgis_then_asks(trench, rustgis, monkeypatch, tmp_path):
    """Another project opened (menu, recent files, start page, demo) with a unit drawn in RustGIS and not read yet: it
    comes in first, then the question about saving; answered "no", the project left goes as it was saved and RustGIS
    keeps its layers, with what was drawn there - nothing is lost."""
    import pyarchinit_photogrammetry as ppg
    from PySide6.QtWidgets import QMessageBox

    w, app, mw = trench
    fake, _ = rustgis
    other = ppg.Document()
    other.add_chunk("Altro")
    other.save(tmp_path / "altro.ppg")
    rl = _linked(w, app)
    layer = fake.linked(rl, "unita")
    fake.draw(layer, box(GX + 3, GY + 1, GX + 3.5, GY + 1.5), GB, us_s="12", area_s="1")
    asked = []
    monkeypatch.setattr(mw, "message", lambda parent, kind, title, text, *a, **k: asked.append(
        [u.us for u in w.units]) or QMessageBox.StandardButton.Discard)
    w.setWindowModified(False)
    w.open_document(str(tmp_path / "altro.ppg"))
    _settle(w, app)
    assert asked == [["12"]]  # read before the question
    assert w.doc.path == tmp_path / "altro.ppg" and w._rustgis is None and rl.layers == {}
    assert [g["name"] for g in fake.groups] == ["Saggio 1"]  # kept there: the project left was not saved with it
    assert layer in fake.layers and "12" in [f["attrs"].get("us_s") for f in layer["features"]]
    assert any("non è stato salvato" in line and "Saggio 1" in line for line in _log(w))


def test_a_chunk_switch_never_waits_long_for_rustgis(trench, rustgis, monkeypatch):
    """RustGIS not giving its last edits when the active chunk changes: the window waits RUSTGIS_LEAVE_WAIT (here half a
    second), then asks; "Continua senza": the switch goes on at once, the link given up - what RustGIS gives later is
    written nowhere - and the layers of the chunk left stay in RustGIS, nothing withdrawn, said in the log. The new
    chunk comes beside them once RustGIS answers again."""
    w, app, mw = trench
    fake, _ = rustgis
    first, second = w.chunk, _other_chunk(w)
    rl = _linked(w, app)
    fake.draw(fake.linked(rl, "unita"), box(GX + 3, GY + 1, GX + 3.5, GY + 1.5), GB, us_s="12", area_s="1")
    monkeypatch.setattr(mw, "RUSTGIS_LEAVE_WAIT", 0.5)
    asked = []
    monkeypatch.setattr(mw, "choice", lambda parent, kind, title, text, options, *a, **k: asked.append(
        (text, options)) or 1)
    release = _stuck(fake)
    try:
        start = time.monotonic()
        w._activate_chunk(second)
        took = time.monotonic() - start
        assert took < 5 and len(asked) == 1 and asked[0][1] == ["Aspetta ancora", "Continua senza"]
        assert "niente si perde" in asked[0][0] and w.chunk is second and w.units == [] and rl._abandoned
    finally:
        release.set()
    _settle(w, app)
    assert not (first.meta.get("drawings") or {}).get("units")  # the late answer written nowhere
    assert sorted(g["name"] for g in fake.groups) == ["Saggio 1", "Saggio 2"]  # the chunk left not withdrawn
    assert any(line.startswith("Le ultime modifiche fatte in RustGIS") and "Saggio 1" in line for line in _log(w))
    assert w._rustgis is not None and w._rustgis.chunk_label == "Saggio 2"


def test_waiting_again_for_rustgis_reads_its_edits(trench, rustgis, monkeypatch):
    """RustGIS slow to give its last edits when the active chunk changes: asked after RUSTGIS_LEAVE_WAIT, "Aspetta
    ancora" waits again; RustGIS answers meanwhile: the unit drawn there goes into the chunk it was drawn on, and the
    chunk left goes from RustGIS, as when RustGIS answers at once."""
    w, app, mw = trench
    fake, _ = rustgis
    first, second = w.chunk, _other_chunk(w)
    rl = _linked(w, app)
    fake.draw(fake.linked(rl, "unita"), box(GX + 3, GY + 1, GX + 3.5, GY + 1.5), GB, us_s="12", area_s="1")
    monkeypatch.setattr(mw, "RUSTGIS_LEAVE_WAIT", 0.5)
    release, asked = _stuck(fake), []

    def wait_again(parent, kind, title, text, options, *a, **k):
        asked.append(options)
        threading.Timer(0.1, release.set).start()  # RustGIS draws again a moment later
        return 0

    monkeypatch.setattr(mw, "choice", wait_again)
    try:
        w._activate_chunk(second)
    finally:
        release.set()
    _settle(w, app)
    assert len(asked) == 1 and [d["us"] for d in first.meta["drawings"]["units"]] == ["12"]
    assert [g["name"] for g in fake.groups] == ["Saggio 2"] and rl.layers == {}


def test_a_switch_while_rustgis_was_not_answering_does_not_wait(trench, rustgis, monkeypatch):
    """RustGIS not answering at the last look (the status bar says so): the next change of chunk does not wait for it
    - nothing asked, the link given up at once - and its layers stay there, with what was drawn in them; what it gives
    later is written nowhere."""
    w, app, mw = trench
    fake, _ = rustgis
    first, second = w.chunk, _other_chunk(w)
    rl = _linked(w, app)
    fake.draw(fake.linked(rl, "unita"), box(GX + 3, GY + 1, GX + 3.5, GY + 1.5), GB, us_s="12", area_s="1")
    fake.busy.append("run_command:edit.status")
    _look(w, app)
    assert w._rustgis_waiting and "In attesa di RustGIS" in w.statusBar().currentMessage()
    asked = []
    monkeypatch.setattr(mw, "choice", lambda *a, **k: asked.append(a) or 1)
    start = time.monotonic()
    w._activate_chunk(second)
    assert time.monotonic() - start < 2 and asked == [] and rl._abandoned and w.chunk is second
    _settle(w, app)
    assert not (first.meta.get("drawings") or {}).get("units")
    assert sorted(g["name"] for g in fake.groups) == ["Saggio 1", "Saggio 2"]


def test_the_start_waits_longer_than_a_look(trench, rustgis):
    """The start waits RUSTGIS_START_TIMEOUT for each answer of RustGIS (it adds every layer of the chunk; only its
    reading of the tile layers that earlier links left waits less, as at a stop); a look waits RUSTGIS_STATUS_WAIT for
    the edit status - a RustGIS that does not draw makes a short look - and RUSTGIS_TIMEOUT for the rest."""
    w, app, mw = trench
    fake, _ = rustgis
    used = []
    fake.hooks.append(lambda tool, args: used.append((args.get("command") or tool, fake.timeout)))
    rl = _linked(w, app)
    assert used and {t for c, t in used if c != "list_layers"} == {mw.RUSTGIS_START_TIMEOUT}
    used.clear()
    fake.draw(fake.linked(rl, "unita"), box(GX + 3, GY + 1, GX + 3.5, GY + 1.5), GB, us_s="12", area_s="1")
    _look(w, app)
    assert used[0] == ("edit.status", mw.RUSTGIS_STATUS_WAIT) and "share.exportLayer" in [c for c, _ in used]
    assert {t for _, t in used[1:]} == {mw.RUSTGIS_TIMEOUT} and [u.us for u in w.units] == ["12"]


def test_a_patch_of_the_orthophoto_reaches_rustgis(trench, rustgis, monkeypatch):
    """Aggiorna l'ortofoto (patch) writes the orthophoto again without showing the chunk anew: RustGIS gets it again
    at the next look all the same - its tile layer read from a new address, the old one gone."""
    import numpy as np

    from pyarchinit_photogrammetry.engine import patches as P
    from pyarchinit_photogrammetry.engine.dem import write_geotiff

    w, app, mw = trench
    fake, _ = rustgis
    monkeypatch.setattr(rg, "RASTER_SAMPLES", 100_000)  # the trench's orthophoto read in place, by its address
    rl = _linked(w, app)
    ortho = rl.rasters[0]
    assert ortho.url and [t["url"] for t in fake.tile_layers] == [ortho.url]
    ch = w.chunk

    def update_orthomosaic(progress=None, skip_unreadable=None):  # the patch painted: the file written again
        rgb = np.full((3, 300, 300), 90, np.uint8)
        write_geotiff(ch.orthomosaic_path, rgb, (GX, GY + 6), 0.02, GB)
        later = ch.orthomosaic_path.stat().st_mtime_ns + 10**9
        os.utime(ch.orthomosaic_path, ns=(later, later))

    monkeypatch.setattr(P, "changed_patches", lambda chunk: [{"id": "p1"}])
    monkeypatch.setattr(ch, "update_orthomosaic", update_orthomosaic)
    monkeypatch.setattr(w, "_photos_to_skip", lambda step: [])
    w.update_ortho_patches()
    _settle(w, app)
    _look(w, app)
    new = next(r for r in rl.rasters if r.name == "Ortofoto")
    assert new is not ortho and new.name == "Ortofoto" and [t["url"] for t in fake.tile_layers] == [new.url]
    assert new.url != ortho.url


def test_a_raster_rustgis_reads_as_a_copy_is_copied_in_a_step(trench, rustgis, monkeypatch):
    """The DEM written again while linked, compressed as RustGIS does not decode: the look leaves its copy to a step of
    the TaskRunner - its progress and its Annulla, never in the thread a change of chunk waits for -, the old layer
    kept meanwhile, and the next look gives RustGIS the new copy."""
    import rasterio

    w, app, mw = trench
    fake, _ = rustgis
    rl = _linked(w, app)
    ortho, dem = rl.rasters
    path = w.chunk.dem_path
    with rasterio.open(path) as src:
        data, profile = src.read(), src.profile
    profile.update(compress="lzma")
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data + 1)
    assert rg.raster_route(path) == "copy"
    steps = []
    real = w.runner.run
    monkeypatch.setattr(w.runner, "run", lambda label, *a, **k: steps.append(label) or real(label, *a, **k))
    _look(w, app)
    assert steps == ["Copie dei raster per RustGIS"] and rl.rasters[1] is dem  # the old layer, until the next look
    _look(w, app)
    new = rl.rasters[1]
    assert new is not dem and new.copy is not None and new.copy.is_file() and new.name == "DEM"
    assert int(dem.layer) not in [layer["id"] for layer in fake.layers]
    assert fake.layer(int(new.layer))["visible"] is False


def test_who_made_the_edits_with_both_links_on(trench, rustgis, monkeypatch):
    """Both links on: what a look at RustGIS pulls is RustGIS's in the log, an invalid polygon of it too ("Da RustGIS:
    la geometria ... corretta"); the look of QGIS waits while a look at RustGIS has not given its answer here (the rows
    in the file are RustGIS's); rows a pull wrote that its answer could not apply are "Da QGIS o RustGIS" at the next
    read, as are those a change of chunk reads in a hurry."""
    w, app, mw = trench
    fake, _ = rustgis
    w._add_unit(_unit("6", box(GX + 1, GY + 1, GX + 2, GY + 2)))
    second = _other_chunk(w)
    rl = _linked(w, app)
    w.a_qgis_live.setChecked(True)
    w._qgis_poll.stop()
    w._poll_qgis()
    pid = w.units[0].attributes[ID_FIELD]
    bowtie = Polygon([(GX + 1, GY + 1), (GX + 2, GY + 2), (GX + 2, GY + 1), (GX + 1, GY + 2)])
    fake.reshape(fake.linked(rl, "unita"), pid, bowtie, GB)
    _look(w, app)
    assert "Da RustGIS: la geometria di US 6 non era valida ed è stata corretta" in _log(w)
    assert "Da RustGIS: US 6 modificata" in _log(w)
    # a look done in its thread, its answer not taken here yet: the look of QGIS waits
    fake.draw(fake.linked(rl, "unita"), box(GX + 3, GY + 1, GX + 3.5, GY + 1.5), GB, us_s="12", area_s="1")
    w._poll_rustgis()
    w._rustgis_job.result(timeout=30)
    assert w._poll_qgis() is False and [u.us for u in w.units] == ["6"]
    _settle(w, app)
    assert "Da RustGIS: US 12 aggiunta" in _log(w) and not any("QGIS: US 12" in line for line in _log(w))
    # rows the answer could not apply (vertices being edited): read later, either may have made them
    fake.draw(fake.linked(rl, "unita"), box(GX + 4, GY + 1, GX + 4.5, GY + 1.5), GB, us_s="13", area_s="1")
    monkeypatch.setattr(w.ortho_view, "editing_vertices", lambda: True)
    _look(w, app)
    assert "13" not in [u.us for u in w.units]
    monkeypatch.setattr(w.ortho_view, "editing_vertices", lambda: False)
    assert w._poll_qgis() is True and "Da QGIS o RustGIS: US 13 aggiunta" in _log(w)
    # a change of chunk: RustGIS's last edits read in a hurry, with both links on
    fake.draw(fake.linked(rl, "unita"), box(GX + 5, GY + 1, GX + 5.5, GY + 1.5), GB, us_s="14", area_s="1")
    first = w.chunk
    w._activate_chunk(second)
    _settle(w, app)
    assert "Da QGIS o RustGIS: US 14 aggiunta" in _log(w)
    assert sorted(d["us"] for d in first.meta["drawings"]["units"]) == ["12", "13", "14", "6"]


def test_a_read_that_fails_keeps_rustgis_layers(trench, rustgis, monkeypatch):
    """RustGIS's last edits pulled when the active chunk changes, but the live GeoPackage held by another program when
    the window reads them: they are not in the chunk left, so its layers stay in RustGIS - nothing withdrawn, said in
    the log."""
    w, app, mw = trench
    fake, _ = rustgis
    first, second = w.chunk, _other_chunk(w)
    rl = _linked(w, app)
    fake.draw(fake.linked(rl, "unita"), box(GX + 3, GY + 1, GX + 3.5, GY + 1.5), GB, us_s="12", area_s="1")

    def locked():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(w._live_link, "read_changes", locked)
    w._activate_chunk(second)
    _settle(w, app)
    assert not (first.meta.get("drawings") or {}).get("units") and rl.layers == {}
    assert sorted(g["name"] for g in fake.groups) == ["Saggio 1", "Saggio 2"]
    assert any(line.startswith("Le ultime modifiche fatte in RustGIS") and "Saggio 1" in line for line in _log(w))


def test_the_project_remembers_its_live_links(trench, rustgis, monkeypatch):
    """Which live links are on is kept in the project: opened again, those come back on - RustGIS's without asking,
    with the active chunk - and only those: the live GeoPackage written for RustGIS alone does not turn QGIS's on. A
    project of an earlier version (nothing kept) has QGIS's on when its live GeoPackage is there."""
    from PySide6.QtWidgets import QMessageBox

    w, app, mw = trench
    fake, _ = rustgis
    monkeypatch.setattr(mw, "message", lambda *a, **k: QMessageBox.StandardButton.Discard)
    _linked(w, app)
    assert w.doc.meta["live_links"] == {"qgis": False, "rustgis": True}
    assert w.save_document()
    path = w.doc.path
    w.a_rustgis_live.setChecked(False)
    _settle(w, app)
    assert w.doc.meta["live_links"] == {"qgis": False, "rustgis": False} and w._rustgis is None
    fake.layers.clear()  # RustGIS opened anew (it keeps the layers of a link turned off while it runs)
    fake.groups.clear()
    fake.tile_layers.clear()
    w.setWindowModified(False)
    w.open_document(str(path))  # as it was saved: RustGIS on
    _settle(w, app)
    assert w.a_rustgis_live.isChecked() and w._rustgis_poll.isActive()
    _look(w, app)  # the first look (the one asked at once may find the window still opening the project)
    assert not w.a_qgis_live.isChecked() and (w._qgis_dir() / LIVE_DB).exists()
    assert w._rustgis is not None and w._rustgis.chunk_label == "Saggio 1"
    assert [g["name"] for g in fake.groups] == ["Saggio 1"] and w._rustgis_poll.isActive()
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["meta"]["live_links"]  # as saved by an earlier version
    path.write_text(json.dumps(data), encoding="utf-8")
    w.setWindowModified(False)
    w.open_document(str(path))
    _settle(w, app)
    assert w.a_qgis_live.isChecked() and not w.a_rustgis_live.isChecked() and w._rustgis is None


def test_a_change_of_system_reads_rustgis_first(trench, rustgis, monkeypatch):
    """Sistema del rilievo: RustGIS's last edits are read into the chunk, and the link stopped, before anything of the
    change is worked out."""
    w, app, mw = trench
    fake, _ = rustgis
    rl = _linked(w, app)
    fake.draw(fake.linked(rl, "unita"), box(GX + 3, GY + 1, GX + 3.5, GY + 1.5), GB, us_s="12", area_s="1")
    ran = []
    monkeypatch.setattr(w, "run_task", lambda label, *a, **k: ran.append(
        (label, [u.us for u in w.units], w._rustgis, [d["us"] for d in w.chunk.meta["drawings"]["units"]])))
    w._set_survey_crs("EPSG:6708")
    assert ran == [("Sistema del rilievo", ["12"], None, ["12"])]
    _settle(w, app)
    assert rl.layers == {}  # stopped, its layers gone from RustGIS: the chunk is to be shown in another system


def test_new_and_import_read_rustgis_before_asking(trench, rustgis, monkeypatch):
    """Nuovo progetto and File > Importa read RustGIS's last edits before the question about saving the project, so
    that they are saved with it; cancelled, the project and the link stay."""
    from PySide6.QtWidgets import QMessageBox

    w, app, mw = trench
    fake, _ = rustgis
    rl = _linked(w, app)
    layer = fake.linked(rl, "unita")
    asked = []
    monkeypatch.setattr(mw, "message", lambda parent, kind, title, text, *a, **k: asked.append(
        sorted(u.us for u in w.units)) or QMessageBox.StandardButton.Cancel)
    fake.draw(layer, box(GX + 3, GY + 1, GX + 3.5, GY + 1.5), GB, us_s="12", area_s="1")
    w.setWindowModified(False)
    w.new_document()
    assert asked == [["12"]] and w._rustgis is rl and rl.layers
    fake.draw(layer, box(GX + 4, GY + 1, GX + 4.5, GY + 1.5), GB, us_s="13", area_s="1")
    w.import_external("metashape")
    assert asked == [["12"], ["12", "13"]] and w._rustgis is rl and rl.layers


def test_closing_ends_the_link_thread_and_its_looks(trench, rustgis):
    """The window closed: the looks stop, the thread of RustGIS's calls ends, the link stops (RustGIS keeps its
    layers) - and nothing of it starts again after, whatever calls come late."""
    w, app, mw = trench
    fake, _ = rustgis
    rl = _linked(w, app)
    _look(w, app)  # the thread of RustGIS's calls made
    w._rustgis_poll.start()
    thread = w._rustgis_thread
    w.setWindowModified(False)
    assert w.close()
    assert not w._rustgis_poll.isActive() and w._rustgis_thread is None and not thread._thread.is_alive()
    assert w._rustgis is None and rl.layers == {} and [g["name"] for g in fake.groups] == ["Saggio 1"]
    w._poll_rustgis()
    w._rustgis_stopped()
    w._rustgis_resume()
    with pytest.raises(RuntimeError):
        w._rustgis_send(lambda: None).result(timeout=1)
    app.processEvents()
    assert w._rustgis_thread is None and w._rustgis is None


def test_steps_left_waiting_at_closing_are_named(trench, caplog):
    """Proxies to build again and sections to cut again after edits read from QGIS or RustGIS, held when those edits
    were read in a hurry right before closing: the window closes, and the log (and its file) names them."""
    import logging

    w, app, mw = trench
    section = {"label": "Sezione 3", "p1": [GX, GY], "p2": [GX + 1, GY]}
    w.sections.append(section)
    w._qgis_waiting["proxies"].append(("pid-6", "US 6", {"base": "plane"}))
    w._qgis_waiting["sections"].append(section)
    w.setWindowModified(False)
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        assert w.close()
    assert "Alla chiusura i proxy US 6 restano com'erano" in caplog.text
    assert "Alla chiusura le sezioni Sezione 3 restano com'erano" in caplog.text
