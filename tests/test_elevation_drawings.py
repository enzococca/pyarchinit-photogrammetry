"""Drawings made on an elevation or a section stay with it: stored in its own (u, v) frame, never on the plan,
exported with it and registered in PyArchInit as its media."""
from __future__ import annotations

import os
import shutil
import sqlite3

import numpy as np
import pytest

from pyarchinit_photogrammetry.engine import elevation as E

ORIGIN = np.array([2480000.0, 4500000.0, 100.0])  # the synthetic trench (testing.synthetic.make_scene)


@pytest.fixture(scope="module")
def with_elevations(processed, tmp_path_factory):
    """A copy of the processed project with a section across the trench and an elevation of its south wall."""
    import pyarchinit_photogrammetry as ppg

    doc, _ch, _ = processed
    d = tmp_path_factory.mktemp("prospetti") / "base"
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")
    out = ppg.Document.open(d / doc.path.name)
    ch = out.chunk
    a, b = ORIGIN[:2] + [2.0, 0.4], ORIGIN[:2] + [2.0, 2.6]
    section = E.build_elevation(ch, E.plane_from_line(ch, a, b, side=ORIGIN[:2] + [0.5, 1.5]), resolution=0.01,
                                depth=(-1.0, 0.5), max_angle=89.0, kind="section", name="Sezione A")
    wall = E.build_elevation(ch, E.plane_from_line(ch, ORIGIN[:2] + [1.05, 0.9], ORIGIN[:2] + [2.95, 0.9],
                                                   side=ORIGIN[:2] + [2.0, 1.5]),
                             resolution=0.01, depth=(-0.08, 0.08), max_angle=89.0, name="Parete sud")
    out.meta["sito"] = "Saggio"
    out.save()
    return out.path, section["id"], wall["id"]


def _copy(with_elevations, tmp_path):
    import pyarchinit_photogrammetry as ppg

    path, sid, wid = with_elevations
    d = tmp_path / "progetto"
    d.mkdir()
    shutil.copy2(path, d / path.name)
    shutil.copytree(path.parent / (path.name + ".files"), d / (path.name + ".files"))
    shutil.copytree(path.parent / "photos", d / "photos")
    doc = ppg.Document.open(d / path.name)
    return doc, sid, wid


@pytest.fixture
def gui(tmp_path):
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.gui import main_window as mw

    windows = []

    def open_window(doc):
        w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
        w.runner.show_dialog = False
        windows.append(w)
        return w

    yield open_window, app, mw
    for w in windows:
        w.setWindowModified(False)
        w.close()
    app.processEvents()


def _entry(ch, eid):
    return next(e for e in ch.meta["elevations"] if e["id"] == eid)


def _surface_cell(ch, entry):
    """(u, v) of a cell of the elevation where its relief has a value, near the middle."""
    import rasterio

    with rasterio.open(E.elevation_paths(ch, entry)[1]) as src:
        d = src.read(1)
        t = src.transform
    rows, cols = np.nonzero(np.isfinite(d))
    k = len(rows) // 2
    return t.c + (cols[k] + 0.5) * t.a, t.f + (rows[k] + 0.5) * t.e


def _unit(u, v, us="5"):
    from shapely.geometry import box

    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon

    return UnitPolygon(box(u - 0.05, v - 0.04, u + 0.05, v + 0.04), us=us, area="1", tipo="US", sito="Saggio")


def test_drawings_on_an_elevation_stay_with_it(with_elevations, tmp_path, gui):
    import pyarchinit_photogrammetry as ppg

    open_window, app, _mw = gui
    doc, sid, _wid = _copy(with_elevations, tmp_path)
    w = open_window(doc)
    ch = w.chunk
    entry = _entry(ch, sid)
    assert w.drawings_frame is None
    w.show_elevation(entry)
    assert w.drawings_frame == sid
    u, v = _surface_cell(ch, entry)
    w._add_unit(_unit(u, v))
    w._quota_keep = {}  # the values of the last spot height: no dialog
    w._quota_clicked(u, v)  # the centre of a cell with a relief: the height is read there
    assert len(w.units) == 1 and len(w.quote) == 1
    q = dict(w.quote[0])
    assert (q["u"], q["v"]) == (pytest.approx(u), pytest.approx(v))
    wkt = w.units[0].geometry.wkt
    w._load_raster()  # the plan: nothing drawn on the elevation is there
    assert w.drawings_frame is None and not w.units and not w.quote
    w.show_elevation(entry)
    assert [x.geometry.wkt for x in w.units] == [wkt]
    assert (w.quote[0]["u"], w.quote[0]["v"]) == (q["u"], q["v"])
    # the plan gets its own drawing, the elevation does not see it
    w._load_raster()
    w._add_unit(_unit(ORIGIN[0] + 1.5, ORIGIN[1] + 1.5, us="9"))
    w.show_elevation(entry)
    assert [x.us for x in w.units] == ["5"]
    w.save_document()
    stored = ch.meta["drawings"]
    assert [x["us"] for x in stored["units"]] == ["9"]
    assert [x["us"] for x in stored["frames"][sid]["units"]] == ["5"]
    # the project opened again: the same
    again = ppg.Document.open(doc.path)
    w2 = open_window(again)
    assert [x.us for x in w2.units] == ["9"]
    w2.show_elevation(_entry(again.chunk, sid))
    assert [x.geometry.wkt for x in w2.units] == [wkt]
    assert (w2.quote[0]["u"], w2.quote[0]["v"]) == (q["u"], q["v"])
    app.processEvents()


def test_spot_height_on_an_elevation_has_the_world_height(with_elevations, tmp_path, gui):
    import open3d as o3d

    from pyarchinit_photogrammetry.io.mesh_ply import read_mesh

    open_window, _app, mw = gui
    doc, sid, _wid = _copy(with_elevations, tmp_path)
    w = open_window(doc)
    ch = w.chunk
    entry = _entry(ch, sid)
    w.show_elevation(entry)
    u, v = _surface_cell(ch, entry)
    w._quota_keep = {}
    w._quota_clicked(u, v)
    (q,) = w.quote
    p = E.world_of(entry, u, v, None, chunk=ch)
    assert q["quota_q"] == pytest.approx(p[2], abs=1e-3)
    assert (q["x"], q["y"]) == (pytest.approx(p[0], abs=1e-3), pytest.approx(p[1], abs=1e-3))
    assert q["quota_q"] == pytest.approx(v, abs=0.01)  # a section: v is the height
    mesh = read_mesh(ch.product_path("model"))
    xyz = ch.to_world(np.asarray(mesh.xyz, np.float64)) - ORIGIN
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(o3d.core.Tensor(xyz.astype(np.float32)), o3d.core.Tensor(np.asarray(mesh.faces, np.uint32)))
    dist = scene.compute_distance(o3d.core.Tensor((np.array([[q["x"], q["y"], q["quota_q"]]]) - ORIGIN)
                                                  .astype(np.float32))).numpy()[0]
    assert dist < 0.01
    labels = [lab.text() for _item, lab, _attrs in w.ortho_view.layers[mw.QUOTE_LAYER]["features"]]
    assert labels == [f"{q['quota_q']:.2f}"]


def test_stones_are_keyed_per_elevation(with_elevations, tmp_path, gui):
    from shapely.geometry import box

    open_window, _app, _mw = gui
    doc, sid, wid = _copy(with_elevations, tmp_path)
    w = open_window(doc)
    ch = w.chunk
    a, b = _entry(ch, sid), _entry(ch, wid)
    w.show_elevation(a)
    w.stones[w._stones_key()] = [box(0, 0, 0.1, 0.1), box(0.2, 0, 0.3, 0.1)]
    w.show_elevation(b)
    assert not any(w.stones.values())
    w.stones[w._stones_key()] = [box(1, 1, 1.2, 1.1)]
    w.show_elevation(a)
    assert [len(s) for s in w.stones.values()] == [2]
    w._load_raster()
    w._store_drawings()
    frames = ch.meta["drawings"]["frames"]
    assert len(frames[sid]["stones"][sid]) == 2 and len(frames[wid]["stones"][wid]) == 1
    assert not (ch.meta.get("stones") or {})  # the plan has none


def test_elevation_export_carries_the_drawings(with_elevations, tmp_path, gui, monkeypatch):
    import ezdxf
    from shapely import wkt as swkt
    from shapely.geometry import box

    open_window, _app, mw = gui
    doc, sid, _wid = _copy(with_elevations, tmp_path)
    w = open_window(doc)
    ch = w.chunk
    entry = _entry(ch, sid)
    w.show_elevation(entry)
    u, v = _surface_cell(ch, entry)
    w._add_unit(_unit(u, v))
    usm = _unit(u + 0.2, v, us="6")
    usm.tipo = "USM"
    w._add_unit(usm)
    w._quota_keep = {}
    w._quota_clicked(u, v)
    w.stones[w._stones_key()] = [box(u - 0.3, v - 0.1, u - 0.2, v)]
    w._store_drawings()
    out = E.export_drawings(ch, entry, tmp_path / "out")
    layers = {e.dxf.layer for e in ezdxf.readfile(out["dxf"]).modelspace()}
    assert {"US", "USM", "QUOTE", "PIETRE", "PROFILO"} <= layers
    svg = out["svg"].read_text()
    assert "<image" in svg and 'id="US"' in svg and "1:" in svg
    con = sqlite3.connect(out["gpkg"])
    tables = {r[0] for r in con.execute("SELECT table_name FROM gpkg_contents")}
    assert {"frame", "unita", "quote", "punti", "pietre"} <= tables
    (fid, kind, plane, heights), = con.execute("SELECT id, kind, plane, v_is_height FROM frame").fetchall()
    assert fid == sid and kind == "section" and heights
    rows = con.execute("SELECT us, tipo, geom_world FROM unita ORDER BY us").fetchall()
    assert [(r[0], r[1]) for r in rows] == [("5", "US"), ("6", "USM")]
    world = np.asarray(swkt.loads(rows[0][2]).exterior.coords)
    corner = E.world_of(entry, u - 0.05, v - 0.04, 0.0)
    assert np.min(np.linalg.norm(world - corner, axis=1)) < 0.001
    (qw,), = con.execute("SELECT geom_world FROM quote").fetchall()
    assert np.allclose(np.asarray(swkt.loads(qw).coords)[0], E.world_of(entry, u, v, None, chunk=ch), atol=0.001)
    con.close()
    # the window's export: the products of the elevation and its drawings
    folder = tmp_path / "esportato"
    folder.mkdir()
    monkeypatch.setattr(mw.QFileDialog, "getExistingDirectory", lambda *a, **k: str(folder))
    monkeypatch.setattr(w, "_info", lambda *a, **k: None)
    w.export_elevation(entry)
    names = {p.name for p in folder.iterdir()}
    assert {"Sezione_A_disegni.dxf", "Sezione_A_disegni.svg", "Sezione_A_disegni.gpkg"} <= names


def test_pyarchinit_us_from_an_elevation_without_plan_geometry(with_elevations, tmp_path, gui, pa_db, monkeypatch):
    from pyarchinit_photogrammetry.pyarchinit import PyArchInit

    open_window, _app, _mw = gui
    doc, sid, _wid = _copy(with_elevations, tmp_path)
    w = open_window(doc)
    ch = w.chunk
    w.pa = PyArchInit.connect(media_root=tmp_path / "media")
    said = []
    monkeypatch.setattr(w, "_info", lambda title, text, *a, **k: said.append(text))
    entry = _entry(ch, sid)
    w.show_elevation(entry)
    u, v = _surface_cell(ch, entry)
    w._add_unit(_unit(u, v, us="7"))
    polygons = len(w.pa.us_polygons("Saggio"))
    media = len(w.pa.db.query("media_table"))
    w.pyarchinit_publish_drawings()
    w.runner.wait()
    assert len(w.pa.stratigraphic_units("Saggio", us="7")) == 1
    assert len(w.pa.us_polygons("Saggio")) == polygons  # no plan geometry
    assert said and "pianta" in said[-1]
    ids = sorted(entry["media"].values())
    assert len(ids) == 2 and len(w.pa.db.query("media_table")) == media + 2
    docs = w.pa.db.query("documentazione_table", sito="Saggio", tipo_documentazione="Sezione")
    assert [d["nome_doc"] for d in docs] == ["Sezione A"]
    w.pyarchinit_publish_drawings()  # a second time: the record updated, nothing registered again
    w.runner.wait()
    assert len(w.pa.stratigraphic_units("Saggio", us="7")) == 1
    assert len(w.pa.db.query("media_table")) == media + 2
    assert w.pa.register_elevation_media(ch, entry, E.export_drawings(ch, entry, tmp_path / "x")) == ids
    assert len(w.pa.db.query("media_table")) == media + 2


def test_old_project_without_frames_opens_unchanged(with_elevations, tmp_path, gui):
    open_window, _app, _mw = gui
    doc, _sid, _wid = _copy(with_elevations, tmp_path)
    ch = doc.chunk
    unit = {"wkt": "POLYGON ((2480001 4500001, 2480001.2 4500001, 2480001.2 4500001.2, 2480001 4500001))",
            "sito": "Saggio", "area": "1", "us": "3", "tipo": "US", "attributes": {"ppg_id": "abc"},
            "layer": "Unita' stratigrafiche"}
    quota = {"x": 2480001.5, "y": 4500001.5, "quota_q": 99.8, "sito_q": "Saggio", "ppg_id": "q1"}
    ch.meta["drawings"] = {"units": [unit], "quote": [quota]}
    ch.meta["stones"] = {"elevation.tif": ["POLYGON ((0 0, 1 0, 1 1, 0 0))"]}  # every elevation shared it
    before = {"units": [dict(unit)], "quote": [dict(quota)]}
    w = open_window(doc)
    assert [u.us for u in w.units] == ["3"] and "elevation.tif" not in w.stones
    w._store_drawings()
    assert ch.meta["drawings"] == before and "stones" not in ch.meta


def test_undo_on_an_elevation(with_elevations, tmp_path, gui):
    open_window, _app, _mw = gui
    doc, sid, _wid = _copy(with_elevations, tmp_path)
    w = open_window(doc)
    ch = w.chunk
    entry = _entry(ch, sid)
    w._add_unit(_unit(ORIGIN[0] + 1.5, ORIGIN[1] + 1.5, us="9"))  # on the plan
    n = len(w.history.entries)
    w.show_elevation(entry)
    assert len(w.history.entries) == n  # switching adds no entry of its own
    u, v = _surface_cell(ch, entry)
    w._add_unit(_unit(u, v))
    assert len(w.history.entries) == n + 1
    w.undo()
    assert not w.units and w.drawings_frame == sid
    w._load_raster()
    assert [x.us for x in w.units] == ["9"]
    w.redo()  # on the plan: the unit comes back on the elevation, the plan keeps its own
    assert [x.us for x in w.units] == ["9"]
    w.show_elevation(entry)
    assert [x.us for x in w.units] == ["5"]
