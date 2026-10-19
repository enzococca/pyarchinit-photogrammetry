"""US proposed from units without a number (SAM): numbers, kinds, measurements and the
stratigraphic relations their contacts suggest, written into the US forms of PyArchInit."""
from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import Point, box

from pyarchinit_photogrammetry.archaeo import us_proposal
from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon
from pyarchinit_photogrammetry.engine.dem import write_geotiff

RES = 0.01


def _scene(tmp_path):
    """A layer A (z 100.10) beside a layer B (z 100.00) with a pit fill P (99.95) inside it, a wall W
    (100.40) against B and a find F lying on A."""
    a = box(0, 0, 2, 3)
    p = Point(3, 1.5).buffer(0.4, 64)
    b = box(2, 0, 4, 3).difference(p)
    w = box(4, 0, 4.3, 3)
    f = box(0.5, 0.5, 0.7, 0.7)
    a = a.difference(f)
    xs = np.arange(0, 4.5, RES) + RES / 2
    ys = 3 - (np.arange(0, 3, RES) + RES / 2)
    gx, gy = np.meshgrid(xs, ys)
    z = np.full(gx.shape, 100.0, np.float32)
    z[gx < 2] = 100.10
    z[(gx - 3) ** 2 + (gy - 1.5) ** 2 < 0.4 ** 2] = 99.95
    z[gx > 4] = 100.40
    dem = write_geotiff(tmp_path / "dem.tif", z, (0.0, 3.0), RES, "EPSG:3004", nodata=-32767.0)
    units = [UnitPolygon(g, area="1", sito="Saggio") for g in (a, b, p, w, f)]
    cx, cy = np.meshgrid(np.arange(0, 4.3, 0.05) + 0.01, np.arange(0, 3, 0.05) + 0.01)
    x, y = cx.ravel(), cy.ravel()
    cls = np.full(len(x), 2, np.uint8)
    cls[x > 4] = 64  # masonry
    cls[(x > 0.5) & (x < 0.7) & (y > 0.5) & (y < 0.7)] = 68  # a find
    return units, dem, (x, y, cls)


def _proposal(tmp_path, **kw):
    units, dem, cloud = _scene(tmp_path)
    measures = us_proposal.measure_units(units, dem, cloud=cloud)
    touching = us_proposal.contacts(units, dem, tolerance=0.02)
    return units, measures, touching, us_proposal.propose(units, measures, touching, 10, **kw)


def test_measures_and_contacts(tmp_path):
    units, measures, touching, _ = _proposal(tmp_path)
    a, b, p, w, f = measures
    assert a["z_mean"] == pytest.approx(100.10, abs=0.002) and p["z_max"] == pytest.approx(99.95, abs=0.002)
    assert w["cloud_class"] == 64 and w["class_share"] > 0.9 and f["cloud_class"] == 68
    assert b["area_m2"] == pytest.approx(6 - np.pi * 0.16, rel=0.01)
    pairs = {(c["a"], c["b"]): c for c in touching}
    assert set(pairs) == {(0, 1), (1, 2), (1, 3), (0, 4)}
    assert pairs[(0, 1)]["length_m"] == pytest.approx(3.0, abs=0.05)
    assert pairs[(0, 1)]["step_m"] == pytest.approx(0.1, abs=0.005)
    assert pairs[(1, 2)]["inside"] == "b" and pairs[(1, 2)]["share_b"] > 0.95
    assert pairs[(1, 2)]["step_m"] == pytest.approx(0.05, abs=0.005)
    assert pairs[(1, 3)]["inside"] is None and pairs[(1, 3)]["step_m"] == pytest.approx(-0.4, abs=0.005)


def test_numbers_kinds_and_relations(tmp_path):
    units, measures, touching, prop = _proposal(tmp_path)
    by_source = {it["source"]: it for it in prop["units"] if it["kind"] == "unit"}
    numbers = {i: by_source[i]["unit"].us for i in range(5)}
    assert numbers == {3: "10", 0: "11", 4: "12", 1: "13", 2: "14"}  # the highest first
    cut = next(it for it in prop["units"] if it["kind"] == "cut")
    assert cut["unit"].us == "15" and cut["fill"] == by_source[2]["key"]  # numbered after its fill
    assert by_source[3]["unit"].tipo == "USM" and by_source[3]["fields"]["d_stratigrafica"] == "Struttura muraria"
    assert by_source[2]["fields"]["d_stratigrafica"] == "Riempimento" and cut["fields"]["d_stratigrafica"] == "Taglio"
    assert "US 13" in cut["fields"]["descrizione"] and "US 14" in cut["fields"]["descrizione"]
    assert by_source[0]["fields"]["quota_max_abs"] == pytest.approx(100.1, abs=0.002)
    assert not by_source[4]["use"]  # a find is not a stratigraphic unit
    names = {it["key"]: it["unit"].us for it in prop["units"]}
    rels = {(names[r["upper"]], r["relation"], names[r["lower"]]): r for r in prop["relations"]}
    assert set(rels) == {("11", "Copre", "13"), ("14", "Riempie", "15"), ("15", "Taglia", "13"),
                         ("13", "Si appoggia a", "10")}
    assert rels[("11", "Copre", "13")]["why"] == "higher" and rels[("11", "Copre", "13")]["inverse"] == "Coperto da"
    assert rels[("13", "Si appoggia a", "10")]["inverse"] == "Gli si appoggia"


def test_without_dem_or_cuts(tmp_path):
    units, dem, cloud = _scene(tmp_path)
    measures = us_proposal.measure_units(units, None, cloud=cloud)
    touching = us_proposal.contacts(units, None, tolerance=0.02)
    prop = us_proposal.propose(units, measures, touching, 1, cuts=False, lang="en")
    names = {it["key"]: it["unit"].us for it in prop["units"]}
    rels = {(names[r["upper"]], r["relation"], names[r["lower"]]) for r in prop["relations"]}
    # without elevations: the wall still has a layer against it, the pit fill no cut
    assert all(it["kind"] == "unit" for it in prop["units"]) and len(rels) == 1
    assert next(iter(rels))[1] == "Si appoggia a"
    assert prop["units"][0]["fields"]["descrizione"].startswith("Proposed from the segmentation")


def test_numbers_already_given_are_kept(tmp_path):
    units, dem, cloud = _scene(tmp_path)
    units[1].us = "11"  # B was numbered by hand
    measures = us_proposal.measure_units(units, dem, cloud=cloud)
    prop = us_proposal.propose(units, measures, us_proposal.contacts(units, dem, tolerance=0.02), 10)
    numbers = sorted(int(it["unit"].us) for it in prop["units"])
    b = next(it for it in prop["units"] if it["source"] == 1)
    assert numbers == [10, 11, 12, 13, 14, 15] and b["unit"].us == "11"


def test_written_into_the_us_forms(pa_db, tmp_path):
    from pyarchinit_photogrammetry.pyarchinit import PyArchInit
    from pyarchinit_photogrammetry.pyarchinit.bridge import parse_relationships

    pa = PyArchInit.connect()
    assert pa.next_us("Saggio") == 4 and pa.next_us("Nuovo sito") == 1
    _, _, _, prop = _proposal(tmp_path)
    out = us_proposal.write_proposal(pa, prop, "Saggio")
    assert sorted(out["created"]) == ["US 11", "US 13", "US 14", "US 15", "USM 10"]  # the find stays out
    assert out["relations"] == 8 and not out["missing"]
    b = pa.us_record("Saggio", "1", "13")
    assert b["node_uuid"] and b["entity_uuid"] and b["d_stratigrafica"] == "Strato"
    assert float(b["quota_min_abs"]) == pytest.approx(100.0, abs=0.002)
    rels = parse_relationships(b["rapporti"])
    assert sorted(r[:2] for r in rels) == [["Coperto da", "11"], ["Si appoggia a", "10"], ["Tagliato da", "15"]]
    wall = pa.us_record("Saggio", "1", "10", "USM")
    assert parse_relationships(wall["rapporti"]) == [["Gli si appoggia", "13", "1", "Saggio"]]
    again = us_proposal.write_proposal(pa, prop, "Saggio")
    assert again["created"] == [] and len(again["existing"]) == 5 and again["relations"] == 0  # never twice


def test_proposal_window(pa_db, tmp_path, monkeypatch):
    """Strumenti > Proponi US e rapporti: the units without a number of the chunk, numbered and
    related in the window, applied to the project and written into PyArchInit."""
    import os

    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings, Qt
    from PySide6.QtWidgets import QApplication

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.gui import main_window as mw
    from pyarchinit_photogrammetry.pyarchinit import PyArchInit
    from pyarchinit_photogrammetry.pyarchinit.bridge import parse_relationships

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    doc = ppg.Document()
    doc.save(tmp_path / "scavo.ppg")
    doc.meta["sito"] = "Saggio"
    ch = doc.add_chunk("Pianta")
    ch.crs = "EPSG:3004"
    ch.data_dir.mkdir(parents=True, exist_ok=True)
    units, dem, _ = _scene(ch.data_dir)
    ch._set_product("dem", dem)
    ch.meta["drawings"] = {"units": [dict(u.to_dict(), attributes={"_layer": "Unita' da SAM"}) for u in units[:4]],
                           "quote": []}
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    w.pa = PyArchInit.connect()
    answers = iter([{"first": 20, "area": "1", "step": 2.0, "cuts": True}])
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(lambda cls, *a, **k: next(answers)))
    monkeypatch.setattr(w, "_info", lambda *a, **k: None)
    assert len(w.units) == 4 and not any(u.us for u in w.units)
    w.propose_units()
    w.runner.wait()
    app.processEvents()
    d = w._proposal_win
    assert d.isVisible() and d.units.rowCount() == 5 and d.b_write.isEnabled()  # 4 units and the cut of the pit
    assert [d.units.item(r, 1).text() for r in range(5)] == ["20", "21", "22", "23", "24"]
    d.units.item(0, 1).setText("30")  # a number corrected by hand
    rels = [(d.relations.item(r, 1).text(), d.relations.item(r, 2).text(), d.relations.item(r, 3).text())
            for r in range(d.relations.rowCount())]
    assert ("US 30", "Copre", "US 22") in rels  # the wall without a classified cloud is a layer higher than B
    d.relations.item(0, 0).setCheckState(Qt.CheckState.Unchecked)
    d._apply(True)
    w.runner.wait()
    app.processEvents()
    assert sorted(u.us for u in w.units) == ["21", "22", "23", "24", "30"] and len(w.units) == 5
    assert all(u.attributes.get("gid") is not None for u in w.units)
    b = w.pa.us_record("Saggio", "1", "22")
    assert b is not None and b["node_uuid"]
    written = {tuple(r[:2]) for r in parse_relationships(b["rapporti"])}
    assert ("Coperto da", "21") in written or ("Coperto da", "30") in written
    w.setWindowModified(False)
    w.close()


def test_cli(pa_db, tmp_path):
    from typer.testing import CliRunner

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.cli import app
    from pyarchinit_photogrammetry.pyarchinit import PyArchInit

    doc = ppg.Document()
    doc.save(tmp_path / "scavo.ppg")
    doc.meta["sito"] = "Saggio"
    ch = doc.add_chunk("Pianta")
    ch.crs = "EPSG:3004"
    ch.data_dir.mkdir(parents=True, exist_ok=True)
    units, dem, _ = _scene(ch.data_dir)
    ch._set_product("dem", dem)
    ch.meta["drawings"] = {"units": [u.to_dict() for u in units[:3]], "quote": []}  # A, B and the pit
    doc.save()
    runner = CliRunner()
    res = runner.invoke(app, ["us-proposal", str(tmp_path / "scavo.ppg")])
    assert res.exit_code == 0, res.output
    # without PyArchInit the numbers follow those of the project (none yet)
    assert "US 1 Copre US 2" in res.output and "US 3 Riempie US 4" in res.output and "US 4 Taglia US 2" in res.output
    res = runner.invoke(app, ["us-proposal", str(tmp_path / "scavo.ppg"), "--write"])  # after US 1-3 of the site
    assert res.exit_code == 0, res.output
    assert "PyArchInit: 4 US forms, 6 relations, 4 polygons" in res.output
    doc = ppg.Document.open(tmp_path / "scavo.ppg")
    records = doc.chunks[0].meta["drawings"]["units"]
    assert sorted(r["us"] for r in records) == ["4", "5", "6", "7"] and all(r["attributes"].get("gid") for r in records)
    assert PyArchInit.connect().us_record("Saggio", "1", "7")["d_stratigrafica"] == "Cut"
