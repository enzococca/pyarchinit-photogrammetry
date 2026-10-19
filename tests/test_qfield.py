"""QField package: export to the tablet and edits brought back."""
from __future__ import annotations

import sqlite3

import numpy as np
import pytest
from shapely.geometry import Point, box

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.engine.dem import write_geotiff
from pyarchinit_photogrammetry.io import gpkg, qfield

X0, Y1 = 500000.0, 4500004.0


@pytest.fixture
def chunk(tmp_path):
    doc = ppg.Document()
    ch = doc.add_chunk("Saggio 1")
    ch.crs = "EPSG:32633"
    doc.save(tmp_path / "scavo.ppg")
    ch.data_dir.mkdir(parents=True, exist_ok=True)
    rgb = np.full((3, 400, 400), 150, np.uint8)
    ch._set_product("orthomosaic", write_geotiff(ch.data_dir / "orthomosaic.tif", rgb, (X0, Y1), 0.01, ch.crs))
    ch._set_product("dem", write_geotiff(ch.data_dir / "dem.tif", np.full((80, 80), 100.0, np.float32), (X0, Y1),
                                         0.05, ch.crs, nodata=-32767.0))
    ch.meta["drawings"] = {"units": [
        {"wkt": box(X0 + 0.5, Y1 - 2, X0 + 1.5, Y1 - 1).wkt, "sito": "Saggio", "area": "1", "us": "1", "tipo": "US",
         "attributes": {"stratigraph_index_us": 1}},
        {"wkt": box(X0 + 2, Y1 - 3, X0 + 3, Y1 - 2).wkt, "sito": "Saggio", "area": "1", "us": "2", "tipo": "USM",
         "attributes": {}}],
        "quote": [{"x": X0 + 1, "y": Y1 - 1.5, "sito_q": "Saggio", "area_q": "1", "us_q": "1", "quota_q": 99.8,
                   "unita_misu_q": "m s.l.m."}]}
    doc.save()
    return ch


def test_package_and_edits_back(chunk, tmp_path):
    out = qfield.export_package(chunk, tmp_path / "qfield", resolution=0.02, project=False)
    folder = out["folder"]
    assert (folder / "rilievo.gpkg").exists() and (folder / "ppg_qfield.json").exists()
    import rasterio

    with rasterio.open(out["ortho"]) as src:  # resampled for the tablet: 2 cm instead of 1
        assert src.res[0] == pytest.approx(0.02) and src.crs.to_epsg() == 32633
    units = gpkg.read_table(folder / "rilievo.gpkg", "unita")
    assert sorted(v["us"] for _, v in units) == ["1", "2"] and all(v["ppg_id"] for _, v in units)
    ids = {v["us"]: v["ppg_id"] for _, v in units}
    assert {(u["attributes"]["ppg_id"]) for u in chunk.meta["drawings"]["units"]} == set(ids.values())
    # on the tablet: US 1 redrawn and renumbered, US 2 deleted, a new unit, the spot height corrected, a new one
    con = sqlite3.connect(folder / "rilievo.gpkg")
    srs = 32633
    con.execute("UPDATE unita SET geom = ?, us = '10' WHERE ppg_id = ?",
                (gpkg.blob(box(X0 + 0.5, Y1 - 2.2, X0 + 1.6, Y1 - 1), srs), ids["1"]))
    con.execute("DELETE FROM unita WHERE ppg_id = ?", (ids["2"],))
    con.execute("INSERT INTO unita (geom, sito, area, us, tipo) VALUES (?, 'Saggio', '1', '3', 'US')",
                (gpkg.blob(box(X0 + 3, Y1 - 4, X0 + 3.5, Y1 - 3.5), srs),))
    con.execute("UPDATE quote SET quota_q = 99.75")
    con.execute("INSERT INTO quote (geom, sito_q, area_q, us_q, quota_q) VALUES (?, 'Saggio', '1', '3', 99.6)",
                (gpkg.blob(Point(X0 + 3.2, Y1 - 3.7), srs),))
    con.commit()
    con.close()
    counts = qfield.import_edits(chunk, folder)
    assert counts == {"units": {"added": 1, "changed": 1, "deleted": 1},
                      "quote": {"added": 1, "changed": 1, "deleted": 0}}
    d = chunk.meta["drawings"]
    assert sorted(u["us"] for u in d["units"]) == ["10", "3"]
    moved = next(u for u in d["units"] if u["us"] == "10")
    assert moved["attributes"]["ppg_id"] == ids["1"] and "4500001.8" in moved["wkt"]
    assert sorted(q["quota_q"] for q in d["quote"]) == [99.6, 99.75]
    other = chunk  # a package goes back only into the chunk it came from
    other_doc = ppg.Document()
    stranger = other_doc.add_chunk("Altro")
    with pytest.raises(ValueError):
        qfield.import_edits(stranger, folder)
    assert other is chunk


def test_unchanged_package_has_no_edits(chunk, tmp_path):
    out = qfield.export_package(chunk, tmp_path / "q2", resolution=None, project=False)
    edits = qfield.read_edits(out["folder"])
    assert all(not v[k] for v in edits.values() for k in ("added", "changed", "deleted"))


def test_qgis_project(chunk, tmp_path):
    from pyarchinit_photogrammetry.io.qgis_link import find_qgis

    if find_qgis() is None or not find_qgis().python:
        pytest.skip("QGIS not installed")
    out = qfield.export_package(chunk, tmp_path / "q3", resolution=0.02)
    text = out["project"].read_text(encoding="utf-8")
    assert "./rilievo.gpkg" in text and "./ortofoto.tif" in text and "ValueMap" in text


def test_window_round_trip(chunk, tmp_path, monkeypatch):
    import os

    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui import main_window as mw

    doc = chunk.document
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    w.show()
    app.processEvents()
    shown = []
    monkeypatch.setattr(w, "_info", lambda title, text: shown.append(text))
    monkeypatch.setattr(mw.QFileDialog, "getExistingDirectory", lambda *a, **k: str(tmp_path / "pkg"))
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(lambda cls, *a, **k: {"resolution": 0.02,
                                                                                    "project": False}))
    (tmp_path / "pkg").mkdir()
    w.export_qfield()
    w.runner.wait()
    app.processEvents()
    folder = tmp_path / "pkg" / "Saggio_1_qfield"
    assert (folder / "rilievo.gpkg").exists() and all(u.attributes.get("ppg_id") for u in w.units)
    con = sqlite3.connect(folder / "rilievo.gpkg")
    con.execute("UPDATE unita SET us = '7' WHERE us = '2'")
    con.commit()
    con.close()
    w.import_qfield(str(folder))
    assert sorted(u.us for u in w.units) == ["1", "7"] and "1 modificate" in shown[-1]
    w.setWindowModified(False)
    w.close()


def test_a_geopackage_touched_by_gdal_can_be_written_again(tmp_path):
    """GDAL and QGIS add the column definition_12_063 to gpkg_spatial_ref_sys (CRS WKT extension):
    writing a table again must not count on the six columns of the base standard."""
    path = tmp_path / "disegni.gpkg"
    gpkg.write_table(path, "punti", "POINT", {"nome": "TEXT"}, [(Point(1, 2), {"nome": "a"})], "EPSG:3004")
    con = sqlite3.connect(path)
    con.execute("ALTER TABLE gpkg_spatial_ref_sys ADD COLUMN definition_12_063 TEXT NOT NULL DEFAULT 'undefined'")
    con.commit()
    con.close()
    gpkg.write_table(path, "linee", "POINT", {"nome": "TEXT"}, [(Point(3, 4), {"nome": "b"})], "EPSG:32637")
    assert [v["nome"] for _g, v in gpkg.read_table(path, "linee")] == ["b"]
    con = sqlite3.connect(path)
    assert con.execute("SELECT COUNT(*) FROM gpkg_spatial_ref_sys WHERE srs_id = 32637").fetchone()[0] == 1
    con.close()
