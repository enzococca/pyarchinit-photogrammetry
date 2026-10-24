"""Unit proxies (solids of stratigraphic units), the US form API and the IFC export with quantities."""
from __future__ import annotations

import os

import numpy as np
import pytest
from shapely.geometry import box

from pyarchinit_photogrammetry.archaeo import proxies, terrain
from pyarchinit_photogrammetry.engine.dem import write_geotiff
from pyarchinit_photogrammetry.pyarchinit.bridge import format_relationships, parse_relationships


@pytest.fixture
def pit_dem(tmp_path):
    z = np.full((100, 200), 10.0, np.float32)
    z[20:60, 50:150] = 9.5  # a pit 1.0 x 0.4 m, 0.5 m deep, 1 cm cells; origin (0, 1)
    return write_geotiff(tmp_path / "dem.tif", z, (0.0, 1.0), 0.01, "EPSG:3004", nodata=-32767.0)


def test_proxy_is_a_closed_solid_with_the_cut_volume(pit_dem, tmp_path):
    poly = box(0.3, 0.2, 1.7, 0.9)  # x 0.3-1.7, y 0.2-0.9 (rows 10-80, cols 30-170)
    p = proxies.unit_proxy(pit_dem, poly, base=9.0, us="12", tipo="US")
    assert p.name == "US12" and p.mesh.is_watertight and p.mesh.is_winding_consistent
    expected = 1.4 * 0.7 * 1.0 - 1.0 * 0.4 * 0.5  # 1 m above the base, minus the pit
    assert abs(p.volume_m3 - expected) / expected < 0.01
    assert abs(p.area_m2 - 1.4 * 0.7) < 1e-6
    assert p.z_bottom == (9.0, 9.0) and p.z_top[1] == pytest.approx(10.0)
    ref = terrain.volume(pit_dem, polygon=poly, reference=9.0)
    assert abs(p.volume_m3 - ref["fill_m3"]) / expected < 0.01  # the same as the bill of quantities
    thick = proxies.unit_proxy(pit_dem, poly, thickness=0.1, us="12")
    assert abs(thick.volume_m3 - 1.4 * 0.7 * 0.1) < 1e-3
    path = proxies.export_extended_matrix([p, thick], tmp_path / "em.obj", offset=(0, 0, 9))
    text = path.read_text()
    assert text.count("o US12") == 2 and (tmp_path / "em.obj.offset.txt").exists()
    assert proxies.save_proxy(p, tmp_path / "ply").exists()
    with pytest.raises(ValueError):
        proxies.unit_proxy(pit_dem, box(50, 50, 51, 51))


def test_a_proxy_in_map_coordinates_keeps_its_millimetres(tmp_path):
    import trimesh

    z = np.full((60, 80), 120.0, np.float32)
    dem = write_geotiff(tmp_path / "utm.tif", z, (300000.0, 4500003.0), 0.05, "EPSG:32633", nodata=-32767.0)
    p = proxies.unit_proxy(dem, box(300000.31, 4500000.52, 300001.43, 4500002.47), thickness=0.25, us="3")
    back = trimesh.load(str(proxies.save_proxy(p, tmp_path / "ply")), process=False, force="mesh")
    assert np.abs(np.asarray(back.vertices) - np.asarray(p.mesh.vertices)).max() < 1e-6  # float32: 0.25 m
    assert len(np.unique(np.asarray(back.vertices)[:, 1])) > 30  # every row of cells, not a 0.5 m grid
    assert abs(back.volume) == pytest.approx(p.volume_m3)


def test_relationships_round_trip():
    text = format_relationships([["Copre", "2"], ["Taglia", "7", "2", "Altro"]], default_site="Scavo", default_area="1")
    assert parse_relationships(text) == [["Copre", "2", "1", "Scavo"], ["Taglia", "7", "2", "Altro"]]
    assert parse_relationships("copre 3; tagliato da 7")[1][:2] == ["Tagliato da", "7"]
    assert parse_relationships(None) == [] and parse_relationships("") == []


def test_ifc_proxies_have_quantities_materials_and_relations(pit_dem, tmp_path):
    pytest.importorskip("ifcopenshell")
    import ifcopenshell

    from pyarchinit_photogrammetry.bim.ifc import export_proxies_ifc, read_ifc_elements

    a = proxies.unit_proxy(pit_dem, box(0.3, 0.2, 0.9, 0.9), base=9.0, us="1")
    b = proxies.unit_proxy(pit_dem, box(1.1, 0.2, 1.7, 0.9), base=9.0, us="2", tipo="USM")
    recs = {"1": {"formazione": "Naturale", "periodo_iniziale": "1", "fase_iniziale": "2",
                  "rapporti": format_relationships([["Copre", "2"]], "Scavo", "1")},
            "2": {"d_stratigrafica": "Muro"}}
    path = export_proxies_ifc([a, b], tmp_path / "us.ifc", crs="EPSG:3004", us_records=recs, site="Scavo")
    els = {e["name"]: e for e in read_ifc_elements(path)}
    assert set(els) == {"US1", "USM2"}
    q = els["US1"]["psets"]["Qto_PyArchInit_US"]
    assert q["NetVolume"] == pytest.approx(a.volume_m3, rel=1e-6) and q["GrossArea"] == pytest.approx(a.area_m2)
    f = ifcopenshell.open(str(path))
    rel = f.by_type("IfcRelConnectsElements")
    assert len(rel) == 1 and rel[0].Name == "Copre"
    assert {m.Name for m in f.by_type("IfcMaterial")} >= {"Naturale", "Muro"}
    assert [g.Name for g in f.by_type("IfcGroup")] == ["Periodo 1 - Fase 2"]


@pytest.mark.slow
def test_create_and_update_us(pa_db):
    from pyarchinit_photogrammetry.pyarchinit import PyArchInit

    pa = PyArchInit.connect()
    new_id = pa.create_us("Saggio", "1", "10", "US", d_stratigrafica="Riempimento",
                          rapporti=[["Copre", "3", "1", "Saggio"]], quota_min_abs=99.5)
    rec = pa.us_record("Saggio", "1", "10")
    assert rec["id_us"] == new_id and rec["d_stratigrafica"] == "Riempimento"
    assert parse_relationships(rec["rapporti"]) == [["Copre", "3", "1", "Saggio"]]
    with pytest.raises(FileExistsError):
        pa.create_us("Saggio", "1", "10", "US")
    pa.update_us(new_id, descrizione="Terra scura", quota_max_abs=99.8)
    rec = pa.us_record("Saggio", "1", "10")
    assert rec["descrizione"] == "Terra scura" and float(rec["quota_max_abs"]) == pytest.approx(99.8)
    pa.create_us("Nuovo sito", "1", "1")  # the site record is created as well
    assert "Nuovo sito" in [s["sito"] for s in pa.sites()]


@pytest.mark.slow
def test_gui_proxies_and_us_form(processed, pa_db, tmp_path):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    qapp = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path))
    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon
    from pyarchinit_photogrammetry.gui import main_window as mw
    from pyarchinit_photogrammetry.gui.dialogs import UsFormDialog
    from pyarchinit_photogrammetry.pyarchinit import PyArchInit

    doc, ch, _ = processed
    w = mw.MainWindow(doc, autoconnect=False)
    w.runner.show_dialog = False
    w.show()
    try:
        x0, y0 = 2480000.0, 4500000.0
        u = UnitPolygon(box(x0 + 1.2, y0 + 1.2, x0 + 1.8, y0 + 1.6), us="7", area="1", tipo="US")
        w.units = [u]
        w._draw_unit(u, 0)
        orig = mw.ParamDialog.ask
        mw.ParamDialog.ask = classmethod(lambda cls, *a, **k: {"units": "drawn", "base": "thickness", "z": 0.0,
                                                                "thickness": 0.1, "dem": ""})
        try:
            w.create_proxies()
            w.runner.wait()
            qapp.processEvents()
        finally:
            mw.ParamDialog.ask = orig
        entry = next(e for e in ch.meta["proxies"] if e["name"] == "US7")
        assert entry["volume_m3"] == pytest.approx(0.6 * 0.4 * 0.1, rel=0.05)
        assert (ch.data_dir / entry["file"]).exists() and w.model_view.kind == "proxies"
        assert w.proxies_pane.table.rowCount() >= 1

        pa = PyArchInit.connect()
        d = UsFormDialog(pa, "Saggio", "1", "", "US", w)
        d.us.setText("20")
        d._add_rel(["Coperto da", "1", "1", "Saggio"])
        d.save()
        rec = pa.us_record("Saggio", "1", "20")
        assert rec is not None and d.saved_id == rec["id_us"]
        assert parse_relationships(rec["rapporti"]) == [["Coperto da", "1", "1", "Saggio"]]
    finally:
        w.setWindowModified(False)
        w.close()


OLD_US_TABLE = """CREATE TABLE us_table (id_us INTEGER PRIMARY KEY, sito TEXT, area TEXT, us TEXT, d_stratigrafica TEXT,
  d_interpretativa TEXT, descrizione TEXT, interpretazione TEXT, periodo_iniziale TEXT, fase_iniziale TEXT,
  periodo_finale TEXT, fase_finale TEXT, unita_tipo TEXT, entity_uuid TEXT)"""


def test_us_form_upgrades_an_old_reduced_schema(tmp_path):
    """The demo database of version 0.2 had a 14-column us_table: the form says so, hides the
    pages without fields and adds the PyArchInit 5 columns on request (records untouched)."""
    import sqlite3

    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from pyarchinit_photogrammetry.gui.dialogs import UsFormDialog
    from pyarchinit_photogrammetry.pyarchinit import PyArchInit

    QApplication.instance() or QApplication([])
    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(db)
    con.execute(OLD_US_TABLE)
    con.execute("INSERT INTO us_table VALUES (1, 'Scavo', '1', '1', 'Strato', '', 'terra', '', 'I', '1', '', '', "
                "'US', NULL)")
    con.commit()
    con.close()
    pa = PyArchInit.connect(url=str(db))
    assert {"rapporti", "colore", "quota_max_abs"} <= set(pa.missing_us_columns())
    d = UsFormDialog(pa, "Scavo", "1", "1", "US")
    pages = [d.tabs.tabText(i) for i in range(d.tabs.count())]
    assert not d.banner.isHidden() and "Rapporti" not in pages and "Caratteri" not in pages
    assert d.upgrade_structure(confirm=False)
    pages = [d.tabs.tabText(i) for i in range(d.tabs.count())]
    assert {"Caratteri", "Quote e misure", "Rapporti"} <= set(pages) and d.banner.isHidden()
    assert list(tmp_path.glob("old.sqlite.backup_*")) and pa.missing_us_columns() == []
    d._add_rel(["Copre", "2", "1", "Scavo"])
    d.fields["colore"].setText("bruno")
    d.save()
    rec = pa.us_record("Scavo", "1", "1")
    assert rec["d_stratigrafica"] == "Strato" and rec["colore"] == "bruno"
    assert parse_relationships(rec["rapporti"]) == [["Copre", "2", "1", "Scavo"]]
    assert pa.upgrade_us_table() == {"added": [], "backup": None}
