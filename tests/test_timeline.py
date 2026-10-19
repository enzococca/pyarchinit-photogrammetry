"""Excavation over time: phases in date order, volumes removed, suggested relations."""
from __future__ import annotations

import numpy as np
import pytest
from shapely.geometry import box

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.archaeo import timeline
from pyarchinit_photogrammetry.engine.dem import NODATA, write_geotiff

RES = 0.05
X0, Y1 = 500000.0, 4500004.0  # a 4 x 4 m square, cells of 5 cm


def _phase(doc, label, date, lowered=(), units=()):
    """A chunk whose DEM is at 100 m and ``depth`` lower inside each ``(geometry, depth)``."""
    ch = doc.add_chunk(label)
    ch.crs = "EPSG:32633"
    n = int(4 / RES)
    z = np.full((n, n), 100.0)
    rows, cols = np.mgrid[0:n, 0:n]
    x, y = X0 + (cols + 0.5) * RES, Y1 - (rows + 0.5) * RES
    for geom, depth in lowered:
        x0, y0, x1, y1 = geom.bounds
        z[(x >= x0) & (x <= x1) & (y >= y0) & (y <= y1)] -= depth
    ch.data_dir.mkdir(parents=True, exist_ok=True)
    ch._set_product("dem", write_geotiff(ch.data_dir / "dem.tif", z.astype(np.float32), (X0, Y1), RES, ch.crs,
                                         nodata=NODATA))
    timeline.set_phase_date(ch, date)
    ch.meta["drawings"] = {"units": [{"wkt": g.wkt, "sito": "Saggio", "area": "1", "us": us, "tipo": "US",
                                      "attributes": {}} for us, g in units], "quote": []}
    return ch


@pytest.fixture
def project(tmp_path):
    doc = ppg.Document()
    doc.save(tmp_path / "scavo.ppg")
    a = box(X0 + 1, Y1 - 3, X0 + 3, Y1 - 1)  # US 1, dug first (30 cm)
    b = box(X0 + 1.5, Y1 - 2.5, X0 + 2.5, Y1 - 1.5)  # US 2 under it, dug next (20 cm more)
    c = box(X0 + 3.2, Y1 - 3.8, X0 + 3.8, Y1 - 3.2)  # US 3 elsewhere
    # added out of order: the dates decide
    _phase(doc, "Fine US 2", "2026-07-03T17:00", [(a, 0.3), (b, 0.2)])
    _phase(doc, "Inizio", "2026-07-01T09:00", [], [("1", a), ("3", c)])
    _phase(doc, "Fine US 1", "2026-07-02T12:00", [(a, 0.3)], [("2", b)])
    return doc


def test_phases_in_date_order(project):
    assert [ch.label for ch in timeline.phases(project)] == ["Inizio", "Fine US 1", "Fine US 2"]
    ch = project.chunks[0]
    timeline.set_phase_date(ch, None)  # back to the photos: none here, so last
    assert [c.label for c in timeline.phases(project)][-1] == "Fine US 2"


def test_volumes_removed(project):
    steps = timeline.removed(project)
    assert [(s["from"], s["to"]) for s in steps] == [("Inizio", "Fine US 1"), ("Fine US 1", "Fine US 2")]
    assert steps[0]["cut_m3"] == pytest.approx(0.3 * 4.0, rel=0.02) and steps[0]["fill_m3"] == pytest.approx(0)
    assert steps[1]["cut_m3"] == pytest.approx(0.2 * 1.0, rel=0.03)
    us1 = {u["us"]: u for u in steps[0]["units"]}
    assert us1["1"]["cut_m3"] == pytest.approx(1.2, rel=0.02) and us1["3"]["cut_m3"] == pytest.approx(0, abs=1e-6)
    assert steps[1]["units"][0]["us"] == "2" and steps[1]["units"][0]["cut_m3"] == pytest.approx(0.2, rel=0.03)


def test_relations_from_the_order_of_removal(project):
    rels = timeline.suggested_relations(project)
    assert [(r["upper"].us, r["relation"], r["lower"].us) for r in rels] == [("1", "Copre", "2")]
    assert rels[0]["overlap_m2"] == pytest.approx(1.0, rel=1e-6) and rels[0]["phases"] == ("Inizio", "Fine US 1")
    report = timeline.timeline(project)
    assert [p["label"] for p in report["phases"]] == ["Inizio", "Fine US 1", "Fine US 2"]
    csv = timeline.timeline_csv(report)
    assert "US 1,1,Copre,US 2,1" in csv and "Inizio,Fine US 1,US 1" in csv

    class FakePa:
        calls = []

        def add_relationships(self, sito, area, us, rels, tipo):
            self.calls.append((us, rels[0][0], rels[0][1]))
            return 1 if us != "2" else None  # US 2 has no form yet

    out = timeline.write_relations(FakePa(), rels)
    assert FakePa.calls == [("1", "Copre", "2"), ("2", "Coperto da", "1")]
    assert out == {"added": 1, "missing": ["US 2"]}


def test_relations_in_the_us_forms(pa_db):
    from pyarchinit_photogrammetry.pyarchinit.bridge import PyArchInit, parse_relationships

    pa = PyArchInit.connect()
    assert pa.add_relationships("Saggio", "1", "1", [["Copre", "2", "1", "Saggio"]]) == 1
    assert pa.add_relationships("Saggio", "1", "1", [["Copre", "2", "", ""]]) == 0  # already there
    assert pa.add_relationships("Saggio", "1", "99", [["Copre", "2"]]) is None
    rels = parse_relationships(pa.us_record("Saggio", "1", "1")["rapporti"])
    assert rels.count(["Copre", "2", "1", "Saggio"]) == 1


def test_phases_window(project, tmp_path):
    import os

    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    project.save()
    w = MainWindow(project, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    w.show()
    app.processEvents()
    w.excavation_phases()
    w.runner.wait()
    app.processEvents()
    d = w._timeline
    assert d.isVisible() and d.phases.rowCount() == 3 and d.relations.rowCount() == 1
    assert d.steps.rowCount() == 5  # two steps; units 1 and 3 under the first, unit 2 under the second
    target = next(c for c in project.chunks if c.label == "Inizio")
    d.phase_chosen.emit(target.key)  # a double click on a phase shows it, with the units drawn on it
    app.processEvents()
    assert w.chunk is target and sorted(u.us for u in w.units) == ["1", "3"]
    d.date_changed.emit(target.key, "2026-07-05 08:00")
    assert target.meta["date"].startswith("2026-07-05")
    w.setWindowModified(False)
    d.close()
    w.close()


def test_cli(project, tmp_path):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    project.save()
    out = tmp_path / "fasi.csv"
    r = CliRunner().invoke(app, ["timeline", str(project.path), "--csv", str(out)])
    assert r.exit_code == 0, r.output
    assert "Inizio -> Fine US 1: removed 1.2" in r.output and "US 1 Copre US 2" in r.output
    assert "Copre" in out.read_text()
