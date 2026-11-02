"""Break lines in the drawings and the TIN in the window: a line drawn on the plan keeps the heights of the DEM,
comes back with the project, is undone, exported (DXF, GeoPackage, QField) and constrains the TIN built from
*Strumenti > TIN*; the TIN is a product of the tree with its menu, a mesh of the 3D view and edges on the plan."""
from __future__ import annotations

import sqlite3

import numpy as np
import pytest

from conftest import TRENCH_ORIGIN

X0, Y0 = TRENCH_ORIGIN  # the trench of the fixture: 6 x 6 m, a DEM sloping from 100 m


def _draw(w, xy, kind="breakline"):
    """A line drawn with the tool of the 2D view: its vertices clicked, Enter."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent

    v = w.ortho_view
    w.set_breakline_kind(kind)
    assert v.mode == "breakline"
    for x, y in xy:
        v._clicks.append(v.scene_pt(x, y))
        v._click_xy.append((x, y))
    v.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier))
    return w.shown_lines[-1]


WALL = [(X0 + 1.0, Y0 + 3.0), (X0 + 3.0, Y0 + 3.2), (X0 + 5.0, Y0 + 3.0)]


def test_break_line_on_the_plan_stored_reloaded_and_undone(trench):
    import pyarchinit_photogrammetry as ppg

    w, app, mw = trench
    rec = _draw(w, WALL)
    assert rec["kind"] == "breakline" and len(rec["coords"]) == 3
    for (x, y), c in zip(WALL, rec["coords"]):  # the height of the DEM at each vertex
        assert c[2] == pytest.approx(w._dem_value(x, y), abs=1e-6)
        assert c[2] == pytest.approx(100.0 + 0.01 * (x - X0) - 0.02 * (Y0 + 6.0 - y), abs=0.002)
    feats = w.ortho_view.features(mw.LINES_LAYER)
    assert len(feats) == 1 and feats[0]["kind"] == "breakline" and feats[0]["ppg_id"] == rec["id"]
    soft = _draw(w, [(X0 + 1.0, Y0 + 1.0), (X0 + 5.0, Y0 + 1.5)], kind="soft")
    assert [f["kind"] for f in w.ortho_view.features(mw.LINES_LAYER)] == ["breakline", "soft"]
    w.save_document()
    stored = w.chunk.meta["drawings"]["lines"]
    assert [r["kind"] for r in stored] == ["breakline", "soft"] and stored[0]["coords"] == rec["coords"]
    # the project opened again: the lines are back with their kind and heights
    again = ppg.Document.open(w.doc.path)
    w2 = mw.MainWindow(again, autoconnect=False, check_updates=False)
    try:
        assert [(r["kind"], r["coords"]) for r in w2.lines] == [("breakline", rec["coords"]),
                                                                ("soft", soft["coords"])]
        assert len(w2.ortho_view.features(mw.LINES_LAYER)) == 2
    finally:
        w2.setWindowModified(False)
        w2.close()
    # undo takes the last line away, redo puts it back; deleting from the selection too
    w.undo()
    assert [r["kind"] for r in w.lines] == ["breakline"]
    assert len(w.ortho_view.features(mw.LINES_LAYER)) == 1
    w.redo()
    assert [r["kind"] for r in w.lines] == ["breakline", "soft"]
    v = w.ortho_view
    item = v.layers[mw.LINES_LAYER]["features"][1][0]
    v.select_items([item])
    assert w.delete_selected(confirm=False) == 1
    assert [r["kind"] for r in w.lines] == ["breakline"]
    w.undo()
    assert len(w.lines) == 2
    app.processEvents()


def test_lines_exported_with_the_drawings(trench, tmp_path):
    import ezdxf

    from pyarchinit_photogrammetry.archaeo import breaklines
    from pyarchinit_photogrammetry.io import gpkg, qfield

    w, _app, _mw = trench
    _draw(w, WALL)
    _draw(w, [(X0 + 0.5, Y0 + 0.5), (X0 + 5.5, Y0 + 0.5), (X0 + 5.5, Y0 + 5.5)], kind="boundary")
    w._store_drawings()
    ch = w.chunk
    dxf = breaklines.write_lines(w.lines, tmp_path / "linee.dxf", ch.crs)
    ents = list(ezdxf.readfile(dxf).modelspace())
    assert {e.dxf.layer for e in ents} == {"LINEE"} and len(ents) == 2
    assert ents[0].dxftype() == "POLYLINE" and ents[0].is_3d_polyline  # with the heights of the DEM
    path = breaklines.write_lines(w.lines, tmp_path / "linee.gpkg", ch.crs)
    rows = gpkg.read_table(path, "linee")
    assert sorted(r[1]["kind"] for r in rows) == ["boundary", "breakline"]
    con = sqlite3.connect(path)
    assert con.execute("SELECT z FROM gpkg_geometry_columns WHERE table_name = 'linee'").fetchone() == (1,)
    con.close()
    out = qfield.export_package(ch, tmp_path / "qfield", resolution=None, project=False)
    assert out["lines"] == 2
    rows = gpkg.read_table(tmp_path / "qfield" / qfield.GPKG, "linee")
    assert sorted(r[1]["kind"] for r in rows) == ["boundary", "breakline"]
    assert qfield.read_edits(tmp_path / "qfield")["units"]["added"] == []  # the lines do not come back


def test_tin_from_the_drawn_line(trench, monkeypatch):
    from pyarchinit_photogrammetry import _native
    from pyarchinit_photogrammetry.core import versions
    from pyarchinit_photogrammetry.engine import tin as T

    w, app, mw = trench
    _draw(w, WALL)
    asked, said = [], []

    def ask(_parent, title, fields, intro=""):
        asked.append({f.name: f.default for f in fields})
        return {"source": "dem", "max_error": 0.01, "lines": True, "spots": True}

    monkeypatch.setattr(mw.ParamDialog, "ask", ask)
    monkeypatch.setattr(w, "_info", lambda title, text, *a, **k: said.append(text))
    w.build_tin()
    w.runner.wait()
    app.processEvents()
    assert asked and asked[0]["lines"] is True and asked[0]["source"] == "dem"
    ch = w.chunk
    tin = T.chunk_tin(ch)
    assert tin is not None and said and "spigoli vincolati" in said[-1]
    if _native.supports("tin"):
        assert len(tin.constrained) >= 2 and ch.meta["tin"]["n_constrained"] == len(tin.constrained)
    else:
        assert "nucleo nativo" in said[-1]
    # its edges on the plan, the product in the tree, the mesh in the 3D view
    assert w.ortho_view.layers[mw.TIN_LAYER]["features"]
    items = [it for it in w.workspace._walk() if (it.data(0, mw.Qt.ItemDataRole.UserRole) or (None,) * 3)[2] == "tin"]
    assert len(items) == 1 and "TIN" in items[0].text(0)
    w._workspace_item("product", ch, "tin")
    mv = w.model_view
    assert mv.combo.findData("tin") >= 0 and mv.kind == "tin"
    assert mv.sd is not None and mv.sd.mesh_faces is not None and len(mv.sd.mesh_faces) == tin.n_triangles
    # DEM dal TIN: the DEM as it was among the versions, the active one named after the TIN
    w.tin_to_dem()
    w.runner.wait()
    app.processEvents()
    assert ch.meta["dem"]["source"] == "tin" and versions.versions(ch, "dem")
    assert versions.active_info(ch, "dem")["name"].startswith("DEM dal TIN")
    # Rimuovi: through core.removal, the edges go with it, Annulla brings it back
    w.remove_workspace_items([("product", ch, "tin")], confirm=False)
    app.processEvents()
    assert ch.product_path("tin") is None and "tin" not in ch.meta
    assert not w.ortho_view.layers.get(mw.TIN_LAYER, {}).get("features")
    w.undo()
    assert T.chunk_tin(ch) is not None and ch.meta["tin"]["n_triangles"] == tin.n_triangles


def test_tin_without_the_native_core_says_so(trench, monkeypatch):
    w, app, mw = trench
    monkeypatch.setenv("PPG_NATIVE", "0")
    _draw(w, WALL)
    said = []
    monkeypatch.setattr(mw.ParamDialog, "ask",
                        lambda *a, **k: {"source": "spots", "max_error": None, "lines": True, "spots": True})
    monkeypatch.setattr(w, "_info", lambda title, text, *a, **k: said.append(text))
    w.quote.append({"x": X0 + 1.0, "y": Y0 + 1.0, "quota_q": 99.9, "sito_q": "Saggio"})
    w.quote.append({"x": X0 + 5.0, "y": Y0 + 5.0, "quota_q": 100.1, "sito_q": "Saggio"})
    w.build_tin()
    w.runner.wait()
    app.processEvents()
    assert said and "nucleo nativo" in said[-1]
    assert w.chunk.meta["tin"]["native"] is False


def test_contours_and_volume_from_the_tin(trench, monkeypatch, tmp_path):
    from pyarchinit_photogrammetry.engine import tin as T

    w, app, mw = trench
    monkeypatch.setattr(mw.ParamDialog, "ask", lambda *a, **k: {"source": "dem", "max_error": 0.01, "lines": False,
                                                                "spots": False})
    monkeypatch.setattr(w, "_info", lambda *a, **k: None)
    w.build_tin()
    w.runner.wait()
    out = tmp_path / "curve.gpkg"
    monkeypatch.setattr(mw.ParamDialog, "ask", lambda *a, **k: {"interval": 0.02, "index_every": 5, "path": str(out)})
    w.tin_contours()
    w.runner.wait()
    app.processEvents()
    assert out.exists() and any(name.startswith("Curve dal TIN") for name in w.line_layers)
    said = []
    monkeypatch.setattr(mw.ParamDialog, "ask", lambda *a, **k: {"reference": "height", "elevation": 99.0, "a": 0.0,
                                                                "b": 0.0, "c": 0.0})
    monkeypatch.setattr(w, "_info", lambda title, text, *a, **k: said.append(text))
    w.tin_volume()
    w.runner.wait()
    app.processEvents()
    tin = T.chunk_tin(w.chunk)
    expected = T.volume(tin, 99.0)
    assert said and w._last_volume["fill_m3"] == pytest.approx(expected["fill_m3"])
    assert expected["fill_m3"] == pytest.approx(36.0 * (np.mean(tin.z) - 99.0), rel=0.05)


def test_dem_edit_keeps_the_drawn_lines(trench):
    """A DEM edit with ``lines="drawn"`` keeps the heights the break lines were drawn with."""
    import rasterio
    from shapely.geometry import box

    from pyarchinit_photogrammetry.archaeo import breaklines
    from pyarchinit_photogrammetry.engine import dem_edit

    w, _app, _mw = trench
    ch = w.chunk
    ridge = breaklines.new_record("breakline", [(X0 + 2.0, Y0 + 3.0, 101.0), (X0 + 4.0, Y0 + 3.0, 101.0)])
    ch.meta.setdefault("drawings", {})["lines"] = [ridge]
    area = box(X0 + 2.0, Y0 + 2.0, X0 + 4.0, Y0 + 4.0)
    plain = dem_edit.fill_area(ch, area, "linear", keep_version=False)
    assert plain["lines"] == 0
    info = dem_edit.fill_area(ch, area, "linear", keep_version=False, lines="drawn")
    assert info["lines"] == 1
    with rasterio.open(ch.dem_path) as src:
        z = float(next(src.sample([(X0 + 3.0, Y0 + 3.0)]))[0])
    assert z == pytest.approx(101.0, abs=0.05)  # on the ridge, not on the plane of the ground around


def test_old_project_without_lines_opens_unchanged(trench):
    w, _app, mw = trench
    ch = w.chunk
    quota = {"x": X0 + 1.5, "y": Y0 + 1.5, "quota_q": 99.8, "sito_q": "Saggio", "ppg_id": "q1"}
    ch.meta["drawings"] = {"units": [], "quote": [quota]}
    w._reload_drawings()
    assert w.lines == [] and len(w.quote) == 1
    w._store_drawings()
    assert ch.meta["drawings"] == {"units": [], "quote": [quota]}


def test_lines_of_an_elevation_stay_in_its_frame(trench, tmp_path):
    import ezdxf

    from pyarchinit_photogrammetry.archaeo import breaklines
    from pyarchinit_photogrammetry.engine import elevation as E

    w, _app, mw = trench
    ch = w.chunk
    plan = breaklines.new_record("breakline", [(X0 + 1.0, Y0 + 1.0, 100.0), (X0 + 2.0, Y0 + 1.0, 100.0)])
    on_wall = breaklines.new_record("soft", [(0.1, 0.2), (0.8, 0.3)])
    mw.MainWindow._store_chunk_drawings(ch, [], [], [], lines=[plan])
    mw.MainWindow._store_chunk_drawings(ch, [], [], [], frame="e1", lines=[on_wall])
    mw.MainWindow._store_chunk_drawings(ch, [], [], [])  # the plan written again without saying: kept
    assert [r["id"] for r in mw.MainWindow._chunk_lines(ch)] == [plan["id"]]
    assert [r["id"] for r in mw.MainWindow._chunk_lines(ch, "e1")] == [on_wall["id"]]
    items = E._drawing_items(ch, {"id": "e1", "kind": "elevation"})
    assert [r["kind"] for _ln, r in items["lines"]] == ["soft"]
    path = E._write_drawings_dxf(items, {"resolution": 0.01}, tmp_path / "p.dxf")
    assert "LINEE" in {e.dxf.layer for e in ezdxf.readfile(path).modelspace()}


def test_cli_build_uses_the_drawn_lines(trench):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry import _native
    from pyarchinit_photogrammetry.cli import app as cli

    w, _app, _mw = trench
    _draw(w, WALL)
    w.save_document()
    r = CliRunner().invoke(cli, ["tin", "build", str(w.doc.path), "--max-error", "0.01"])
    assert r.exit_code == 0, r.output
    assert "1 break lines drawn on the plan" in r.output
    if _native.supports("tin"):
        import pyarchinit_photogrammetry as ppg

        assert ppg.Document.open(w.doc.path).chunk.meta["tin"]["n_constrained"] >= 2
    r = CliRunner().invoke(cli, ["tin", "build", str(w.doc.path), "--max-error", "0.01", "--no-drawn"])
    assert r.exit_code == 0 and "break lines drawn" not in r.output


def test_tin_edges_never_take_the_clicks_nor_the_selection(trench, monkeypatch):
    """Under a TIN of 50 000 edges a click, a query and a box selection find the unit; the TIN alone selected is not
    deleted; *Cancella disegni* leaves the TIN on the plan."""
    from shapely.geometry import box

    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon
    from pyarchinit_photogrammetry.engine import tin as T

    w, app, mw = trench
    T.build_tin(w.chunk, source="dem", max_error=0.01)
    w._redraw_overlays()
    w._add_unit(UnitPolygon(box(X0 + 2.0, Y0 + 2.0, X0 + 4.0, Y0 + 4.0), us="5", area="1", tipo="US", sito="Saggio"))
    v = w.ortho_view
    rng = np.random.default_rng(1)
    p = np.column_stack([X0 + 2.0 + 2.0 * rng.random(50000), Y0 + 2.0 + 2.0 * rng.random(50000)])
    v.add_segments(np.stack([p, p + rng.normal(0, 0.1, p.shape)], 1), layer=mw.TIN_LAYER)
    app.processEvents()
    c = v.scene_pt(X0 + 3.0, Y0 + 3.0)
    assert v.item_at(c).data(0) == "Unita' stratigrafiche"
    assert v.feature_at(c)[0] == "Unita' stratigrafiche"
    picked = v.items_in_rect(v.sceneRect())
    assert picked and mw.TIN_LAYER not in {it.data(0) for it in picked}
    v.select_items([f[0] for f in v.layers[mw.TIN_LAYER]["features"]])
    w.setWindowModified(False)
    assert w.delete_selected(confirm=False) == 0 and not w.isWindowModified()
    assert v.layers[mw.TIN_LAYER]["features"]
    w.clear_drawings()
    assert v.layers[mw.TIN_LAYER]["features"] and not w.units


def test_dem_edit_counts_the_lines_it_uses_and_never_twice(trench, monkeypatch):
    from shapely.geometry import box

    w, _app, mw = trench
    rec = _draw(w, [(X0 + 2.0, Y0 + 4.05), (X0 + 4.0, Y0 + 4.05)])  # outside the area, within its ring of cells
    area = box(X0 + 2.0, Y0 + 2.0, X0 + 4.0, Y0 + 4.0)
    v = w.ortho_view
    v.select_items([v.layers[mw.LINES_LAYER]["features"][0][0]])
    asked, called = [], []

    def ask(_parent, title, fields, intro=""):
        asked.append({f.name: f.label for f in fields})
        return {"method": "linear", "value": None, "breaklines": True, "drawn": True, "name": ""}

    monkeypatch.setattr(mw.ParamDialog, "ask", ask)
    monkeypatch.setattr(w.chunk, "edit_dem", lambda *a, **k: called.append((a, k)) or {})
    monkeypatch.setattr(w, "run_task", lambda label, fn, *a, **k: fn(None))
    w.edit_dem_area(area)
    assert "(1)" in asked[0]["drawn"]  # the line just outside the area counts, as the engine takes it
    (args, kwargs), = called
    assert args[3] is None and kwargs["lines"] == "drawn"  # the selected break line goes once, as drawn
    assert rec["kind"] == "breakline"
