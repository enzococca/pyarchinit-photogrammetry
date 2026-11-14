"""The drawings kept with an elevation (engine.elevation_drawings) on a synthetic wall: its frame both ways, the
drawings read with their world positions, removed and given back with the elevation, given to another elevation,
the 3D DXF, the projects made before 1.7.0 moved on open, and the edits made in QGIS on the elevation unfolded read
back. A light project: one chunk, a wall plane with a relief raster written here, no photos processed."""
from __future__ import annotations

import json
import sqlite3

import numpy as np
import pytest

X0, Y0 = 2480000.0, 4500000.0
DEPTH = 0.03  # the surface of the wall: 3 cm in front of its plane


def _raster(path, bounds, value=None, bands=1, res=0.02):
    import rasterio
    from rasterio.transform import from_origin

    u0, v0, u1, v1 = bounds
    w, h = int(round((u1 - u0) / res)), int(round((v1 - v0) / res))
    path.parent.mkdir(parents=True, exist_ok=True)
    dtype = "float32" if value is not None else "uint8"
    with rasterio.open(path, "w", driver="GTiff", width=w, height=h, count=bands, dtype=dtype,
                       transform=from_origin(u0, v1, res, res), nodata=-9999.0 if value is not None else None) as dst:
        for b in range(1, bands + 1):
            dst.write(np.full((h, w), value if value is not None else 120 + 30 * b, dtype), b)


def _wall(ch, eid, origin, name, bounds=(0.0, 99.0, 2.0, 101.0)):
    from pyarchinit_photogrammetry.engine.elevation import Plane

    plane = Plane(np.asarray(origin, float), [1.0, 0.0, 0.0], [0.0, 0.0, 1.0])  # u east, v up (the height), facing south
    entry = {"id": eid, "name": name, "kind": "elevation", "frame": plane.to_dict(), "bounds": list(bounds),
             "resolution": 0.02, "crs": ch.crs, "v_is_height": True,
             "orthophoto": f"elevations/{eid}/orthophoto.tif", "relief": f"elevations/{eid}/elevation.tif"}
    _raster(ch.data_dir / entry["relief"], bounds, value=DEPTH)
    _raster(ch.data_dir / entry["orthophoto"], bounds, bands=3)
    ch.meta.setdefault("elevations", []).append(entry)
    return entry


@pytest.fixture
def project(tmp_path):
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.core.camera import Camera

    doc = ppg.Document()
    doc.save(tmp_path / "muro.ppg")
    ch = doc.add_chunk("Muro")
    ch.crs = "EPSG:32633"
    for i, (x, y) in enumerate([(0.0, 0.0), (3.0, 0.0), (0.0, 3.0), (3.0, 3.0)]):
        cam = Camera(key=i, label=f"IMG_{i}.JPG", path=f"./photos/IMG_{i}.JPG", sensor_key=0)
        cam.reference.location = [X0 + x, Y0 + y, 102.0]
        ch.cameras.append(cam)
    wall = _wall(ch, "w1", (X0 + 1.0, Y0 + 0.9, 0.0), "Parete sud")
    doc.save()
    return doc, ch, wall


def _unit(u, v, us="5", size=(0.1, 0.08)):
    from shapely.geometry import box

    from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon

    return UnitPolygon(box(u - size[0] / 2, v - size[1] / 2, u + size[0] / 2, v + size[1] / 2), us=us, area="1",
                       tipo="US", sito="Saggio")


def _known(u, v, d=0.0):
    """The world point of (u, v, d) on the wall of the fixture, by hand."""
    return np.array([X0 + 1.0 + u, Y0 + 0.9 - d, v])


def _quota(ch, entry, u, v):
    from pyarchinit_photogrammetry.engine import elevation_drawings as ED

    p = ED.world_of_uv(entry, [[u, v]], DEPTH)[0]
    return {"u": u, "v": v, "x": float(p[0]), "y": float(p[1]), "quota_q": float(p[2]), "sito_q": "Saggio",
            "ppg_id": f"q{u}"}


def test_frame_both_ways_and_drawings_with_world(project):
    from pyarchinit_photogrammetry.engine import elevation_drawings as ED

    _doc, ch, wall = project
    uv = np.array([[0.25, 99.5], [1.75, 100.8], [1.0, 100.0]])
    world = ED.world_of_uv(wall, uv, [0.0, DEPTH, -0.1])
    assert np.abs(world - np.array([_known(*uv[0]), _known(*uv[1], DEPTH), _known(*uv[2], -0.1)])).max() < 1e-9
    back = ED.uv_of(wall, world)
    assert np.abs(back - np.array([[0.25, 99.5, 0.0], [1.75, 100.8, DEPTH], [1.0, 100.0, -0.1]])).max() < 1e-9
    assert ED.surface_depths(ch, wall, [[1.0, 100.0], [5.0, 100.0]])[0] == pytest.approx(DEPTH, abs=1e-6)
    assert np.isnan(ED.surface_depths(ch, wall, [[5.0, 100.0]])[0])

    assert ED.add_units(ch, "w1", [_unit(0.5, 100.0)]) == 1
    ch.meta["drawings"]["frames"]["w1"]["quote"] = [_quota(ch, wall, 1.2, 100.3)]
    got = ED.drawings(ch, "w1", world=True)
    (unit,) = got["units"]
    assert unit["us"] == "5" and unit["id"]
    corners = np.asarray(unit["world"].exterior.coords)
    want = np.array([_known(u, v) for u, v in unit["geometry"].exterior.coords])
    assert np.abs(corners - want).max() < 1e-6  # sub-millimetre
    (q,) = got["quote"]
    assert np.abs(np.asarray(q["world"].coords)[0] - _known(1.2, 100.3, DEPTH)).max() < 1e-6
    assert ED.counts(ch, "w1") == {"units": 1, "quote": 1, "points": 0, "lines": 0, "stones": 0}
    got["units"][0]["geometry"] = None  # a copy: the project is untouched
    assert ED.drawings(ch, "w1")["units"][0]["geometry"] is not None
    assert not ch.meta["drawings"].get("units")  # nothing among the plan drawings
    with pytest.raises(KeyError):
        ED.drawings(ch, "nope")


def test_removed_with_the_elevation_and_back_with_undo(project):
    from pyarchinit_photogrammetry.core import removal
    from pyarchinit_photogrammetry.engine import elevation as E
    from pyarchinit_photogrammetry.engine import elevation_drawings as ED

    _doc, ch, wall = project
    other = _wall(ch, "w2", (X0 + 1.0, Y0 + 2.0, 0.0), "Parete nord")
    ED.add_units(ch, "w1", [_unit(0.5, 100.0)])
    ED.add_units(ch, "w2", [_unit(0.7, 100.0, us="8")])
    before = json.loads(json.dumps(ch.meta["drawings"]))
    r = removal.ItemsRemoval(ch, [("elevation", wall)])
    r.apply()
    assert "w1" not in ch.meta["drawings"]["frames"] and "w2" in ch.meta["drawings"]["frames"]
    r.undo()
    assert ch.meta["drawings"] == before and ED.entry_of(ch, "w1") is not None
    r.redo()
    assert "w1" not in ch.meta["drawings"]["frames"]
    r.undo()
    assert E.remove_elevation(ch, "w2") and "w2" not in ch.meta["drawings"]["frames"]
    assert ED.take_frame(ch, "w1") is not None and "drawings" not in ch.meta
    del other


def test_transfer_to_another_elevation_keeps_the_world(project):
    from shapely import wkt

    from pyarchinit_photogrammetry.engine import elevation_drawings as ED

    _doc, ch, wall = project
    # the same wall made again on a plane 5 cm behind, its u starting 30 cm further west
    again = _wall(ch, "w3", (X0 + 0.7, Y0 + 0.95, 0.0), "Parete sud (rifatta)", bounds=(0.0, 99.0, 2.6, 101.0))
    ED.add_units(ch, "w1", [_unit(0.5, 100.0)])
    frames = ch.meta["drawings"]["frames"]
    frames["w1"]["quote"] = [_quota(ch, wall, 1.2, 100.3)]
    frames["w1"]["lines"] = [{"id": "l1", "kind": "breakline", "coords": [[0.2, 99.8, None], [1.4, 99.9, None]],
                              "attributes": {}}]
    frames["w1"]["stones"] = {"w1": ["POLYGON ((1 100, 1.1 100, 1.1 100.1, 1 100))"]}
    done = ED.transfer(ch, "w1", "w3", copy_=True)
    assert done == {"units": 1, "quote": 1, "points": 0, "lines": 1, "stones": 1}
    src, dst = frames["w1"], frames["w3"]
    for (u0, v0), (u1, v1) in zip(wkt.loads(src["units"][0]["wkt"]).exterior.coords,
                                  wkt.loads(dst["units"][0]["wkt"]).exterior.coords):
        assert (u1, v1) == (pytest.approx(u0 + 0.3, abs=1e-9), pytest.approx(v0, abs=1e-9))
        # the vertex on the surface of the source is on the target at the same world point
        w_src = ED.world_of_uv(wall, [[u0, v0]], DEPTH)[0]
        d_dst = ED.uv_of(again, [w_src])[0, 2]
        assert np.abs(ED.world_of_uv(again, [[u1, v1]], d_dst)[0] - w_src).max() < 1e-9
    assert dst["units"][0]["attributes"]["ppg_id"] != src["units"][0]["attributes"]["ppg_id"]
    q0, q1 = src["quote"][0], dst["quote"][0]
    assert (q1["x"], q1["y"], q1["quota_q"]) == (q0["x"], q0["y"], q0["quota_q"])
    assert q1["u"] == pytest.approx(1.5, abs=1e-9)
    assert dst["lines"][0]["coords"][0][0] == pytest.approx(0.5, abs=1e-9) and dst["lines"][0]["id"] != "l1"
    assert len(dst["stones"]["w3"]) == 1
    ED.transfer(ch, "w3", "w1", copy_=False)  # moved: the source has none left
    assert "w3" not in frames and len(frames["w1"]["units"]) == 2
    with pytest.raises(ValueError):
        ED.transfer(ch, "w1", "w1")


def test_dxf_3d_in_world_coordinates(project, tmp_path):
    import ezdxf

    from pyarchinit_photogrammetry.engine import elevation_drawings as ED

    _doc, ch, wall = project
    ED.add_units(ch, "w1", [_unit(0.5, 100.0)])
    ch.meta["drawings"]["frames"]["w1"]["quote"] = [_quota(ch, wall, 1.2, 100.3)]
    path = ED.write_dxf_3d(ch, wall, tmp_path / "muro_3d.dxf")
    msp = ezdxf.readfile(path).modelspace()
    (poly,) = [e for e in msp if e.dxftype() == "POLYLINE" and e.dxf.layer == "US"]
    pts = np.array([tuple(v.dxf.location) for v in poly.vertices])
    want = np.array([_known(u, v) for u, v in [(0.45, 99.96), (0.55, 99.96), (0.55, 100.04), (0.45, 100.04)]])
    assert len(pts) == 4, pts
    assert all(np.min(np.linalg.norm(want - p, axis=1)) < 1e-6 for p in pts), (pts, want)
    (point,) = [e for e in msp if e.dxftype() == "POINT" and e.dxf.layer == "QUOTE"]
    assert np.abs(np.array(point.dxf.location) - _known(1.2, 100.3, DEPTH)).max() < 1e-6
    feats = ED.world_features(ch)
    assert {f["kind"] for f in feats} == {"polygon", "point"} and all(f["elevation"] == "w1" for f in feats)
    assert np.abs(next(f for f in feats if f["kind"] == "point")["xyz"][0] - _known(1.2, 100.3, DEPTH)).max() < 1e-6


def _legacy(ch, plan_unit, uv_unit, quota_uv):
    """A project as 1.5.5-1.6 wrote it: what was drawn on the elevation among the plan's, in its (u, v)."""
    ch.meta["drawings"] = {"units": [plan_unit, uv_unit],
                           "quote": [{"x": X0 + 1.5, "y": Y0 + 1.5, "quota_q": 99.8, "ppg_id": "qp"},
                                     {"x": quota_uv[0], "y": quota_uv[1], "quota_q": 0.0, "ppg_id": "qe"}]}
    ch.meta["stones"] = {"orthomosaic.tif": ["POLYGON ((2480001 4500001, 2480001.1 4500001, 2480001.1 4500001.1, "
                                             "2480001 4500001))"],
                         "elevation.tif": ["POLYGON ((1 100, 1.1 100, 1.1 100.1, 1 100))"]}


def _rec(u):
    return {**u.to_dict(), "layer": None}


def test_old_project_drawings_moved_to_their_elevation_on_open(project):
    import pyarchinit_photogrammetry as ppg

    doc, ch, wall = project
    plan = _rec(_unit(X0 + 1.5, Y0 + 1.5, us="9"))
    on_wall = _rec(_unit(0.5, 100.0, us="5"))
    _legacy(ch, plan, on_wall, (1.2, 100.3))
    doc.save()
    raw = doc.path.read_bytes()
    again = ppg.Document.open(doc.path)
    assert doc.path.read_bytes() == raw  # opening writes nothing
    c = again.chunk
    d = c.meta["drawings"]
    assert [u["us"] for u in d["units"]] == ["9"] and [q["ppg_id"] for q in d["quote"]] == ["qp"]
    frame = d["frames"]["w1"]
    assert [u["us"] for u in frame["units"]] == ["5"] and frame["units"][0]["wkt"] == on_wall["wkt"]
    (q,) = frame["quote"]
    assert (q["u"], q["v"]) == (1.2, 100.3)
    assert np.abs(np.array([q["x"], q["y"], q["quota_q"]]) - _known(1.2, 100.3, DEPTH)).max() < 1e-3
    assert q["migrated_from_plan"] == {"x": 1.2, "y": 100.3, "quota_q": 0.0}
    assert frame["stones"]["w1"] == ["POLYGON ((1 100, 1.1 100, 1.1 100.1, 1 100))"]
    assert set(c.meta["stones"]) == {"orthomosaic.tif"}
    again.save()  # saved: the new layout in the file, and nothing moved twice
    third = ppg.Document.open(doc.path).chunk
    assert third.meta["drawings"]["frames"]["w1"]["units"][0]["us"] == "5"
    assert [u["us"] for u in third.meta["drawings"]["units"]] == ["9"]


def test_old_drawings_that_fit_two_elevations_stay(project):
    from pyarchinit_photogrammetry.engine import elevation_drawings as ED

    _doc, ch, _entry = project
    _wall(ch, "w2", (X0 + 1.0, Y0 + 2.0, 0.0), "Parete nord")  # the same (u, v) extent
    _legacy(ch, _rec(_unit(X0 + 1.5, Y0 + 1.5, us="9")), _rec(_unit(0.5, 100.0)), (1.2, 100.3))
    before = json.loads(json.dumps(ch.meta))
    report = ED.migrate_legacy(ch)
    assert report["ambiguous"] == 3 and report["units"] == 0
    assert json.loads(json.dumps(ch.meta)) == before  # nothing moved, nothing lost


def test_local_project_without_a_clear_plan_is_left_alone(project):
    """A local survey whose plan and an elevation with v up from 0 share the same numbers: nothing tells a drawing of
    the elevation from one of the plan, so nothing moves."""
    from pyarchinit_photogrammetry.engine import elevation_drawings as ED

    _doc, ch, wall = project
    for cam in ch.cameras:  # the photos about the origin
        cam.reference.location = [cam.reference.location[0] - X0, cam.reference.location[1] - Y0, 2.0]
    wall["bounds"] = [0.0, 0.0, 2.0, 2.0]
    _legacy(ch, _rec(_unit(1.5, 1.5, us="9")), _rec(_unit(0.5, 1.0)), (1.2, 1.3))
    ch.meta["stones"].pop("elevation.tif")
    before = json.loads(json.dumps(ch.meta["drawings"]))
    report = ED.migrate_legacy(ch)
    assert ch.meta["drawings"] == before and not any(report.values())


def test_edits_made_in_qgis_on_the_unfolded_elevation_read_back(project, tmp_path):
    from shapely.geometry import MultiPolygon, Point, box

    from pyarchinit_photogrammetry.engine import elevation as E
    from pyarchinit_photogrammetry.engine import elevation_drawings as ED
    from pyarchinit_photogrammetry.io import gpkg

    _doc, ch, wall = project
    ED.add_units(ch, "w1", [_unit(0.5, 100.0, us="5"), _unit(1.5, 100.0, us="6")])
    frame = ch.meta["drawings"]["frames"]["w1"]
    frame["quote"] = [_quota(ch, wall, 1.2, 100.3), _quota(ch, wall, 0.3, 99.5)]
    keep_id, gone_id = (u["attributes"]["ppg_id"] for u in frame["units"])
    out = E.export_drawings(ch, wall, tmp_path / "qgis", raster=True)
    ED.write_snapshot(out["gpkg"])
    assert ED._empty_changes(ED.read_unfolded(out["gpkg"]))
    # QGIS: the first unit reshaped and renumbered, the second deleted, one drawn; a spot height moved, one deleted
    con = sqlite3.connect(out["gpkg"])
    con.execute("UPDATE unita SET geom = ?, us = '15' WHERE ppg_id = ?",
                (gpkg.blob(MultiPolygon([box(0.4, 99.9, 0.7, 100.2)]), -1), keep_id))
    con.execute("DELETE FROM unita WHERE ppg_id = ?", (gone_id,))
    con.execute("INSERT INTO unita (geom, us, tipo, sito) VALUES (?, '20', 'USM', 'Saggio')",
                (gpkg.blob(MultiPolygon([box(1.0, 100.5, 1.3, 100.8)]), -1),))
    con.execute("UPDATE quote SET geom = ? WHERE ppg_id = 'q1.2'", (gpkg.blob(Point(1.6, 100.6), -1),))
    con.execute("DELETE FROM quote WHERE ppg_id = 'q0.3'")
    con.commit()
    con.close()
    frame["quote"].append(_quota(ch, wall, 1.9, 100.9))  # drawn here meanwhile: it stays
    done = ED.apply_unfolded(ch, "w1", out["gpkg"])
    assert done["units"] == 3 and done["quote"] == 2
    units = {u["us"]: u for u in frame["units"]}
    assert set(units) == {"15", "20"} and units["15"]["attributes"]["ppg_id"] == keep_id
    assert units["15"]["wkt"].startswith("POLYGON") and units["20"]["tipo"] == "USM"
    assert units["20"]["attributes"]["ppg_id"]
    quote = {q["ppg_id"]: q for q in frame["quote"]}
    assert set(quote) == {"q1.2", "q1.9"}
    q = quote["q1.2"]
    assert (q["u"], q["v"]) == (pytest.approx(1.6), pytest.approx(100.6))
    assert np.abs(np.array([q["x"], q["y"], q["quota_q"]]) - _known(1.6, 100.6, DEPTH)).max() < 1e-3
    assert ED._empty_changes(ED.read_unfolded(out["gpkg"]))  # read once


def test_plugin_api_reads_and_adds_elevation_drawings(project, tmp_path):
    from shapely.geometry import box

    from pyarchinit_photogrammetry.plugins.api import Context

    _doc, ch, _wall = project
    ctx = Context("prova", tmp_path / "plugin")
    assert ctx.shown_elevation() is None
    assert [e["id"] for e in ctx.elevations(ch)] == ["w1"]
    assert ctx.add_units([box(0.4, 99.9, 0.6, 100.1)], attributes={"us": "7"}, chunk=ch, elevation="w1") == 1
    got = ctx.elevation_drawings("w1", chunk=ch, world=True)
    (unit,) = got["units"]
    assert unit["us"] == "7" and unit["id"]
    assert np.abs(np.asarray(unit["world"].exterior.coords)[:, 2].max() - 100.1) < 1e-9
    assert not (ch.meta["drawings"].get("units"))  # not on the plan
    with pytest.raises(ValueError):
        ctx.add_units([box(0, 0, 1, 1)], chunk=ch, elevation="nope")
    with pytest.raises(ValueError):
        ctx.elevation_drawings(chunk=ch)  # none shown, none given
