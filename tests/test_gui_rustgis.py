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
import time
from pathlib import Path

import pytest
from shapely.geometry import Point, box

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
    engine answering behind the transport the window makes. ``opened`` gets each start of the app (none happens)."""
    fake = FakeEngine()
    opened = []
    monkeypatch.setattr(rg, "find_rustgis", lambda: rg.RustGisInstall(Path("/Applications/RustGIS.app"), None,
                                                                       "0.4.0"))
    monkeypatch.setattr(rg, "open_app", lambda paths=(): opened.append(list(paths)))
    monkeypatch.setattr(rg, "endpoint", lambda: rg.Endpoint("http://127.0.0.1:9/mcp", tmp_path / "mcp-http.token"))
    monkeypatch.setattr(rg, "HttpTransport", lambda endpoint, timeout=30.0: fake)
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
                        lambda self, rasters=(): given.append(list(rasters)) or real(self, rasters))
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
    monkeypatch.setattr(rg.RustGisLink, "start", lambda self, rasters=(): given.append(rasters))
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
    """The DEM rebuilt while the link is on: at the next look after the window shows the chunk again its layer in
    RustGIS is replaced (RustGIS keeps what it loaded of a file; one read by address would no longer answer), hidden as
    before; the orthophoto stays the layer it was."""
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
    assert rl.rasters[1] is dem  # the window did not show the products again yet
    w.refresh_all()
    _look(w, app)
    assert rl.rasters[0] is ortho and rl.rasters[1] is not dem and rl.rasters[1].name == "DEM"
    assert fake.layer(int(rl.rasters[1].layer))["visible"] is False
    assert int(dem.layer) not in [layer["id"] for layer in fake.layers]
    assert any("RustGIS" in line and "DEM" in line for line in _log(w))


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
    shows them twice."""
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
    """RustGIS refusing to give its edits (an export it refuses), said once: while the pull waits (10 s) nothing is sent
    either - RustGIS's edits are read before the program's go there - and the program's rows are kept; they go after
    the next pull."""
    w, app, mw = trench
    fake, _ = rustgis
    w._add_unit(_unit("6", box(GX + 1, GY + 1, GX + 2, GY + 2)))
    rl = _linked(w, app)
    pid = w.units[0].attributes[ID_FIELD]
    fake.rules.append(lambda command, p: "exports are switched off" if command == "share.exportLayer" else None)
    fake.draw(fake.linked(rl, "quote"), Point(GX + 0.5, GY + 0.5), GB, quota_q=100.5, us_q="6")
    _look(w, app)
    refused = [line for line in _log(w) if line.startswith("RustGIS ha rifiutato")]
    assert len(refused) == 1 and "exports are switched off" in refused[0]
    moved = box(GX + 1, GY + 1, GX + 2.5, GY + 2)
    w.units[0].geometry = moved
    w._redraw_overlays()
    w._export_qgis_layers()
    fake.scripts.clear()
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
    assert any(line.startswith("RustGIS non ha risposto") and "Saggio 1" in line for line in _log(w))
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
