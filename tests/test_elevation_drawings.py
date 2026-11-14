"""Drawings made on an elevation or a section stay with it: stored in its own (u, v) frame, never on the plan,
exported with it and registered in PyArchInit as its media."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path

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


def _surface_cell_on_the_dem(ch, entry):
    """``(u, v, world point)`` of a cell of the elevation with a relief whose world point has a cell with a value in
    the plan DEM, the 3 x 3 cells around it too and flat there (within 2 cm), and the DEM there at the height of the
    point (within 3 cm: the ground the DEM sees, not a point down the face of a wall whose cells are the flat top of
    the wall), the nearest such to the middle: a section cell at the edge of the survey can fall outside the DEM or
    on its empty cells, the middle of a section is mostly the face of the far wall, and the fixture is built a little
    differently on every platform (the CI of 3.12 once chose a face cell 12 cm under the top)."""
    import rasterio

    with rasterio.open(E.elevation_paths(ch, entry)[1]) as src:
        d = src.read(1)
        t = src.transform
    rows, cols = np.nonzero(np.isfinite(d))
    order = np.argsort(np.abs(np.arange(len(rows)) - len(rows) // 2))  # from the middle outwards
    with rasterio.open(ch.dem_path) as dem:
        z = dem.read(1, masked=True)
        for k in order[:5000]:
            u, v = t.c + (cols[k] + 0.5) * t.a, t.f + (rows[k] + 0.5) * t.e
            p = E.world_of(entry, u, v, None, chunk=ch)
            if p is None:
                continue
            r, c = dem.index(p[0], p[1])
            if not (1 <= r < dem.height - 1 and 1 <= c < dem.width - 1):
                continue
            block = z[r - 1:r + 2, c - 1:c + 2]
            if np.ma.count_masked(block) or not np.isfinite(block.filled(np.nan)).all():
                continue
            if float(block.max() - block.min()) > 0.02:  # the ground, not the face of a wall or the edge of a cut
                continue
            if abs(float(block[1, 1]) - p[2]) > 0.03:  # the point is the ground the DEM sees, not under its cells
                continue
            return u, v, p
    raise AssertionError("no cell of the section has a cell of the plan DEM under it")


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
    # the elevation's sets apart, the plan's untouched (they stay loaded whatever is shown)
    assert len(w.frame_units) == 1 and len(w.frame_quote) == 1 and not w.units and not w.quote
    q = dict(w.frame_quote[0])
    assert (q["u"], q["v"]) == (pytest.approx(u), pytest.approx(v))
    wkt = w.frame_units[0].geometry.wkt
    w._load_raster()  # the plan: nothing drawn on the elevation is there
    assert w.drawings_frame is None and not w.units and not w.quote and not w.frame_units
    w.show_elevation(entry)
    assert [x.geometry.wkt for x in w.frame_units] == [wkt]
    assert (w.frame_quote[0]["u"], w.frame_quote[0]["v"]) == (q["u"], q["v"])
    # the plan gets its own drawing, the elevation does not see it
    w._load_raster()
    w._add_unit(_unit(ORIGIN[0] + 1.5, ORIGIN[1] + 1.5, us="9"))
    w.show_elevation(entry)
    assert [x.us for x in w.frame_units] == ["5"] and [x.us for x in w.units] == ["9"]
    drawn = {a.get("ppg_id") for _i, _l, a in w.ortho_view.layers["Unita' stratigrafiche"]["features"]}
    assert drawn == {w.frame_units[0].attributes["ppg_id"]}  # only the elevation's on its view
    w.save_document()
    stored = ch.meta["drawings"]
    assert [x["us"] for x in stored["units"]] == ["9"]
    assert [x["us"] for x in stored["frames"][sid]["units"]] == ["5"]
    # the project opened again: the same
    again = ppg.Document.open(doc.path)
    w2 = open_window(again)
    assert [x.us for x in w2.units] == ["9"]
    w2.show_elevation(_entry(again.chunk, sid))
    assert [x.geometry.wkt for x in w2.frame_units] == [wkt]
    assert (w2.frame_quote[0]["u"], w2.frame_quote[0]["v"]) == (q["u"], q["v"])
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
    (q,) = w.frame_quote
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
    w.shown_stones[w._stones_key()] = [box(0, 0, 0.1, 0.1), box(0.2, 0, 0.3, 0.1)]
    w.show_elevation(b)
    assert not any(w.shown_stones.values())
    w.shown_stones[w._stones_key()] = [box(1, 1, 1.2, 1.1)]
    w.show_elevation(a)
    assert [len(s) for s in w.shown_stones.values()] == [2] and not w.stones
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
    w.shown_stones[w._stones_key()] = [box(u - 0.3, v - 0.1, u - 0.2, v)]
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
    # the stones of the elevations up to 1.6 that no single elevation takes: not drawn, kept as they were
    assert ch.meta["drawings"] == before and ch.meta["stones"] == {"elevation.tif": ["POLYGON ((0 0, 1 0, 1 1, 0 0))"]}


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
    assert not w.frame_units and w.drawings_frame == sid and [x.us for x in w.units] == ["9"]
    w._load_raster()
    assert [x.us for x in w.units] == ["9"]
    w.redo()  # on the plan: the unit comes back on the elevation, the plan keeps its own
    assert [x.us for x in w.units] == ["9"]
    assert [x["us"] for x in ch.meta["drawings"]["frames"][sid]["units"]] == ["5"]
    w.show_elevation(entry)
    assert [x.us for x in w.frame_units] == ["5"]


def test_the_plan_stays_loaded_while_an_elevation_is_shown(with_elevations, tmp_path, gui):
    """Ruling T15-2: ``units``/``quote`` are the plan's whatever is shown - every plan feature (PyArchInit, proxies,
    volume, IFC, US proposal...) reads them - and the elevation's sets are apart; the site follows in every frame; an
    undo on an elevation keeps the plan's pending PyArchInit edits; a plugin's units go to the elevation shown."""
    open_window, _app, _mw = gui
    doc, sid, wid = _copy(with_elevations, tmp_path)
    w = open_window(doc)
    ch = w.chunk
    plan, published = _unit(ORIGIN[0] + 1.5, ORIGIN[1] + 1.5, us="9"), _unit(ORIGIN[0] + 2.0, ORIGIN[1] + 1.5, us="8")
    published.attributes["gid"] = 7
    w._add_unit(plan)
    w._add_unit(published)
    w.pa_pending = {7: published}
    w.show_elevation(_entry(ch, wid))  # another elevation keeps a unit of its own
    w._add_unit(_unit(*_surface_cell(ch, _entry(ch, wid)), us="4"))
    w.show_elevation(_entry(ch, sid))
    u, v = _surface_cell(ch, _entry(ch, sid))
    w._add_unit(_unit(u, v))
    assert [x.us for x in w.units] == ["9", "8"] and [x.us for x in w.frame_units] == ["5"]
    assert [x.us for x in w._publishable()[0]] == ["9"]  # the plan's, never the elevation's
    # an undo touching the PyArchInit part, from the elevation: the plan's pending edits stay
    state = json.loads(json.dumps(w._undo_state(), default=str))  # as the history keeps it
    w._undo_apply({k: state[k] for k in ("units", "pa")}, {"units", "pa"}, [])
    assert set(w.pa_pending) == {7} and [x.us for x in w.frame_units] == ["5"]
    # the site of the project: the plan, the elevation shown and the other one
    w.set_project_site("Altro")
    assert [x.sito for x in w.units] == ["Altro", "Saggio"]  # the one from PyArchInit keeps its site
    assert w.frame_units[0].sito == "Altro"
    assert ch.meta["drawings"]["frames"][wid]["units"][0]["sito"] == "Altro"
    # a plugin's units (Context.add_units) on the elevation shown, in its (u, v)
    w.add_units([_unit(u + 0.3, v, us="6")], source="prova")
    assert [x.us for x in w.frame_units] == ["5", "6"] and len(w.units) == 2
    assert w.selected_polygon() is None  # nothing selected: the elevation's selection, empty
    w.pa_pending = {}  # else closing asks whether to write them


def test_measuring_point_and_marker_on_an_elevation_read_the_surface(with_elevations, tmp_path, gui):
    """The measuring tool and the marker tool on an elevation read the world point of the surface (through its
    relief), never the plan's products at (u, v); the export places the point there."""
    import sqlite3

    from shapely import wkt as swkt

    open_window, _app, _mw = gui
    doc, sid, _wid = _copy(with_elevations, tmp_path)
    w = open_window(doc)
    ch = w.chunk
    entry = _entry(ch, sid)
    w.show_elevation(entry)
    u, v, p = _surface_cell_on_the_dem(ch, entry)
    w._probe_clicked(u, v)
    (q,) = w.frame_points
    assert not w.probe_points
    assert (q["u"], q["v"]) == (pytest.approx(u), pytest.approx(v))
    assert (q["x"], q["y"], q["z_superficie"]) == (pytest.approx(p[0]), pytest.approx(p[1]), pytest.approx(p[2], abs=1e-3))
    # the DEM of the plan read at the world point of the surface (never at (u, v)): its cell there, which is the
    # ground there within the relief of the DEM around that cell (a section cell may be on a steep edge)
    import rasterio

    with rasterio.open(ch.dem_path) as src:
        row, col = src.index(p[0], p[1])
        cells = src.read(1, window=((row - 1, row + 2), (col - 1, col + 2)))  # inside the DEM, with values
        here = float(cells[1, 1])
    assert q.get("z") == pytest.approx(here, abs=1e-3)
    assert q["z"] == pytest.approx(p[2], abs=0.05 + float(cells.max() - cells.min()))
    assert np.allclose(w._raster_point(u, v), ch.to_internal(p[None])[0])  # the marker tool: the same point
    w._store_drawings()
    out = E.export_drawings(ch, entry, tmp_path / "out")
    con = sqlite3.connect(out["gpkg"])
    (world,), = con.execute("SELECT geom_world FROM punti").fetchall()
    con.close()
    assert np.allclose(np.asarray(swkt.loads(world).coords)[0], p, atol=1e-3)


def test_plan_tools_are_refused_on_an_elevation(with_elevations, tmp_path, gui):
    """A section trace, a crop or an area measured are plan tools: on an elevation they are refused, said in the
    status bar, nothing done with (u, v) as plan coordinates. A patch is not: it goes on the elevation."""
    open_window, _app, _mw = gui
    doc, sid, _wid = _copy(with_elevations, tmp_path)
    w = open_window(doc)
    entry = _entry(w.chunk, sid)
    w.show_elevation(entry)
    sections = len(w.sections)
    u, v = _surface_cell(w.chunk, entry)
    w._section_drawn((u, v), (u + 0.5, v))
    assert len(w.sections) == sections and "pianta" in w.statusBar().currentMessage()
    asked = []
    w.crop_products = lambda geom: asked.append(geom)
    w._crop_drawn([(u, v), (u + 0.2, v), (u + 0.2, v + 0.2)])
    w.measure_area = lambda geom: asked.append(geom)
    w._area_drawn([(u, v), (u + 0.2, v), (u + 0.2, v + 0.2)])
    assert asked == [] and w._plan_selection() is None
    # the patch tool works on the elevation now: the photos of the area are looked for, in its (u, v)
    tasks = []
    w.run_task = lambda name, job, done, **k: tasks.append(name)
    w.statusBar().clearMessage()
    w._patch_drawn([(u, v), (u + 0.2, v), (u + 0.2, v + 0.2)])
    assert tasks == ["Foto dell'area"] and "pianta" not in w.statusBar().currentMessage()


def test_stones_found_after_a_switch_go_to_the_image_of_the_search(with_elevations, tmp_path, gui, monkeypatch):
    """SAM's stones go to the image the search started on, whatever is shown when it ends, said in the log; the export
    of an elevation carries only its own stones."""
    from shapely.geometry import box

    open_window, _app, mw = gui
    doc, sid, wid = _copy(with_elevations, tmp_path)
    w = open_window(doc)
    ch = w.chunk
    a, b = _entry(ch, sid), _entry(ch, wid)
    w._load_raster()  # the plan: a search started on the orthophoto
    monkeypatch.setattr(w, "_sam_available", lambda then=None: True)
    monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(lambda cls, *x, **k: {
        "min_size": 0.05, "max_size": 0.8, "thoroughness": "normale"}))
    tasks = []
    monkeypatch.setattr(w, "run_task", lambda name, job, done, **k: tasks.append(done))
    found = [box(ORIGIN[0] + 1, ORIGIN[1] + 1, ORIGIN[0] + 1.1, ORIGIN[1] + 1.1)]
    w.draw_stones()
    key = w._stones_key()
    w.show_elevation(b)  # the search ends while elevation B is shown
    tasks.pop()(found)
    assert not w.frame_stones.get(wid) and len(w.stones[key]) == 1
    # a search on elevation A, ending on the plan: into A's frame in the chunk
    w.show_elevation(a)
    w.draw_stones()
    w._load_raster()
    tasks.pop()([box(0, 0, 0.1, 0.1)])
    assert len(ch.meta["drawings"]["frames"][sid]["stones"][sid]) == 1
    # B's export: none of the plan's stones
    w._store_drawings()
    stray = {"orthomosaic.tif": ["POLYGON ((0 0, 1 0, 1 1, 0 0))"]}  # a key of another image in B's frame
    ch.meta["drawings"]["frames"].setdefault(wid, {"units": [], "quote": []})["stones"] = stray
    assert E._drawing_items(ch, b)["stones"] == []


def test_elevation_drawings_in_qgis_and_saved_media_refreshed(with_elevations, tmp_path, gui, monkeypatch):
    """Ruling T15-3: the export writes the elevation unfolded (u, v, no CRS, its orthophoto beside it) and in 3D on the
    survey; the live GeoPackage of QGIS and RustGIS carries the world tables of every elevation with drawings; *Apri
    il prospetto in QGIS* builds a project in the unfolded frame. Saving the elevation: removed elevations' drawings
    go at saving."""
    import sqlite3

    from pyarchinit_photogrammetry.io import gpkg, qgis_link

    open_window, _app, mw = gui
    doc, sid, wid = _copy(with_elevations, tmp_path)
    w = open_window(doc)
    ch = w.chunk
    entry = _entry(ch, sid)
    w.show_elevation(entry)
    u, v = _surface_cell(ch, entry)
    w._add_unit(_unit(u, v))
    w._store_drawings()
    out = E.export_drawings(ch, entry, tmp_path / "out", raster=True)
    assert out["ortho"].exists() and out["png"].exists()
    con = sqlite3.connect(out["gpkg_world"])
    rows = {r[0]: (r[1], r[2]) for r in con.execute("SELECT table_name, srs_id, z FROM gpkg_geometry_columns")}
    (blob,), = con.execute("SELECT geom FROM unita").fetchall()
    con.close()
    assert rows["unita"] == (3004, 1) and rows["profilo"] == (3004, 1)  # a section: its profile too
    world = gpkg.geometry(blob)
    corner = E.world_of(entry, u - 0.05, v - 0.04, 0.0)
    assert world.has_z and np.min(np.linalg.norm(np.asarray(world.geoms[0].exterior.coords) - corner, axis=1)) < 1e-3
    # the live GeoPackage: the plan as it was, the elevation's tables added - written on the plan first, then again
    # with the elevation shown (its sets in memory)
    w._load_raster()
    assert w._export_qgis_layers(force=True)
    w.show_elevation(entry)
    w._add_unit(_unit(u + 0.3, v, us="6"))
    files = w._export_qgis_layers(force=True)
    link = w._qgis_live_link()
    con = sqlite3.connect(link.path)
    assert con.execute(f"SELECT count(*) FROM prospetto_{sid}_unita").fetchone()[0] == 2
    con.close()
    tables = dict(link.elevation_tables())
    assert files and f"prospetto_{sid}_unita" in tables and not any(wid in t for t in tables)
    vectors = w._qgis_elevation_vectors(ch)
    assert any(x["table"] == f"prospetto_{sid}_unita" and "Sezione A" in x["name"] for x in vectors)
    # Apri il prospetto in QGIS: the unfolded frame, no reference system
    built = {}
    monkeypatch.setattr(qgis_link, "find_qgis", lambda: object())
    monkeypatch.setattr(qgis_link, "build_project", lambda project, crs, files, rasters, **k: built.update(
        project=project, crs=crs, rasters=rasters, vectors=k["extra_vectors"]) or {"vectors": [], "rasters": []})
    monkeypatch.setattr(qgis_link, "open_in_qgis", lambda *a, **k: None)
    w.open_elevation_in_qgis()
    w.runner.wait()
    assert built["crs"] == "" and Path(built["rasters"][0]["path"]).exists()
    assert {x["table"] for x in built["vectors"]} == {"unita", "quote", "punti", "pietre", "linee"}
    assert all(x["path"].endswith("_disegni.gpkg") for x in built["vectors"])
    # an elevation removed and the project saved: its drawings go with it
    ch.meta["elevations"] = [e for e in ch.meta["elevations"] if e["id"] != sid]
    w._load_raster()
    w.save_document()
    assert sid not in ((ch.meta.get("drawings") or {}).get("frames") or {})


def test_media_are_written_again_at_every_save(with_elevations, tmp_path, gui, pa_db, monkeypatch):
    """*Salva in PyArchInit* of an elevation: the same media records every time, their files written again with the
    drawings, the US added since linked to them; only the spot heights: no media."""
    from pyarchinit_photogrammetry.pyarchinit import PyArchInit

    open_window, _app, _mw = gui
    doc, sid, _wid = _copy(with_elevations, tmp_path)
    w = open_window(doc)
    ch = w.chunk
    w.pa = PyArchInit.connect(media_root=tmp_path / "media")
    monkeypatch.setattr(w, "_info", lambda *a, **k: None)
    entry = _entry(ch, sid)
    w.show_elevation(entry)
    u, v = _surface_cell(ch, entry)
    w._add_unit(_unit(u, v, us="7"))
    w.pyarchinit_publish_drawings()
    w.runner.wait()
    ids = dict(entry["media"])
    (dxf,), = [[r["filepath"] for r in w.pa.db.query("media_table", id_media=ids["dxf"])]]
    before = Path(dxf).read_bytes()
    w._add_unit(_unit(u + 0.3, v, us="11"))
    w.pyarchinit_publish_drawings()
    w.runner.wait()
    assert entry["media"] == ids and Path(dxf).read_bytes() != before
    us11 = w.pa.us_id("Saggio", "1", "11")
    linked = {(r["entity_type"], int(r["id_entity"])) for r in w.pa.db.query("media_to_entity_table",
                                                                              id_media=ids["dxf"])}
    assert ("US", us11) in linked
    w.pyarchinit_publish_points()
    w.runner.wait()
    assert entry["media"] == ids


def test_a_survey_crs_change_keeps_the_elevations_system():
    """The elevations stay in the system they were made in when the survey's changes: earlier ones get it written."""
    from types import SimpleNamespace

    from pyarchinit_photogrammetry.gnss.heights import _drawn

    chunk = SimpleNamespace(meta={"elevations": [{"id": "a"}, {"id": "b", "crs": "EPSG:32633"}]}, crs="EPSG:6875",
                            products={})
    values, _ = _drawn(chunk, None, None, "EPSG:3004")
    assert [e["crs"] for e in values["elevations"]] == ["EPSG:3004", "EPSG:32633"]
