"""The drawings of an elevation on a wall path placed on the survey: every vertex at the depth of the relief there,
so that a drawing exported to QGIS falls on the wall in plan within the elevation's resolution (units, stones, break
lines, the 3D DXF, the 3D view, the unfolded GeoPackage); planes and cylinders as before (``d`` = 0); the traces of
the elevations as one layer of the plan; drawings given from a plane elevation to a wall path."""
from __future__ import annotations

import logging

import numpy as np
import pytest
from shapely import wkt
from shapely.geometry import box

from pyarchinit_photogrammetry.archaeo import breaklines
from pyarchinit_photogrammetry.archaeo.stratigraphy import UnitPolygon
from pyarchinit_photogrammetry.engine import elevation as E
from pyarchinit_photogrammetry.engine import elevation_drawings as D
from pyarchinit_photogrammetry.engine.elevation_drawings import add_units
from pyarchinit_photogrammetry.engine.wall_path import WallPath, corner_radii, ordered
from pyarchinit_photogrammetry.io import gpkg, qgis_link

from wall_helpers import distance_to, wall_doc

RES = 0.01


def path_of(info) -> WallPath:
    face = ordered(info["face"], info["viewer"])
    return WallPath(face, radii=corner_radii(face))


def unit_box(u0, v0, u1, v1, us="1") -> UnitPolygon:
    return UnitPolygon(box(u0, v0, u1, v1), us=us, area="1", sito="Scavo")


@pytest.fixture(scope="module")
def built_once(wall_dataset, tmp_path_factory):
    _doc, (ch,), info = wall_doc(tmp_path_factory.mktemp("muro_disegni"), wall_dataset)
    entry = E.build_elevation(ch, path_of(info), resolution=RES, max_angle=89.0)
    return ch, info, entry


@pytest.fixture
def built(built_once):
    """The chunk, the info and the entry of the default wall at 1 cm, its drawings emptied for each test."""
    ch, info, entry = built_once
    ch.meta.pop("drawings", None)
    yield ch, info, entry
    ch.meta.pop("drawings", None)
    ch.meta["elevations"] = [e for e in ch.meta["elevations"] if e["id"] == entry["id"]]


def _frame_data(ch, eid) -> dict:
    return ch.meta.setdefault("drawings", {}).setdefault("frames", {}).setdefault(eid, {"units": [], "quote": []})


def _coords(geom) -> np.ndarray:
    if geom.geom_type in ("Polygon", "MultiPolygon"):
        return np.vstack([np.asarray(r) for r in D._rings(geom)])
    return np.asarray(geom.coords)


def test_a_unit_across_the_corner_falls_on_the_wall_in_plan(built):
    ch, info, entry = built
    add_units(ch, entry["id"], [unit_box(5.5, 100.5, 6.5, 101.0, us="7")])       # face lengths 5.5-6.5: both faces
    (geom, values), = E.world_tables(ch, entry)["unita"][2]
    xyz = np.asarray(geom.geoms[0].exterior.coords)
    assert distance_to(info["face"], xyz[:, :2]).max() <= RES + 1e-3             # within the elevation's resolution
    assert np.linalg.norm(xyz[:, :2] - info["face"][1], axis=1).min() < 0.02     # it goes round the corner
    assert xyz[:, 2].min() == pytest.approx(100.5, abs=1e-6) and xyz[:, 2].max() == pytest.approx(101.0, abs=1e-6)
    assert values["us"] == "7"


def test_the_trace_is_one_layer_with_the_name_and_the_id(built, tmp_path):
    ch, info, entry = built
    (t,) = E.traces(ch)
    assert t["id"] == entry["id"] and t["geometry"]["type"] == "LineString" and abs(t["length_m"] - 10.0) < 0.01
    assert distance_to(info["face"], np.asarray(t["geometry"]["coordinates"])).max() < 0.01
    files = qgis_link.export_layers(tmp_path, {E.TRACES_LAYER: E.trace_layer(ch)}, ch.crs)
    (row,) = gpkg.read_table(files["linee"], "linee")
    assert row[1]["layer"] == "Tracce dei prospetti" and row[1]["prospetto"] == entry["name"]
    assert row[1]["prospetto_id"] == entry["id"] and row[0].geom_type == "LineString"


def test_the_trace_keeps_its_sharp_corner(built):
    from pyarchinit_photogrammetry.io import site_sections

    ch, info, entry = built
    g = site_sections.trace(entry, E.bounds_of(ch, entry))
    xy = np.asarray(g["coordinates"])
    assert np.linalg.norm(xy - info["face"][1], axis=1).min() < 0.01           # the corner itself, not the fillet
    (layer_feature,) = E.trace_layer(ch)["features"]
    assert layer_feature["_geom"][0] == "LineString" and layer_feature["lunghezza_m"] == pytest.approx(10.0, abs=0.01)
    assert layer_feature["tipo"] == "path" and layer_feature["etichetta"] == entry["name"]


def test_stones_lines_and_3d_follow_the_surface(built, tmp_path):
    ch, info, entry = built
    data = _frame_data(ch, entry["id"])
    data.setdefault("stones", {})[entry["id"]] = [box(5.8, 100.6, 6.2, 100.9).wkt]
    data["lines"] = [breaklines.new_record("breakline", [(5.0, 101.2), (7.0, 101.4)])]
    tables = E.world_tables(ch, entry)
    tol = RES + 1e-3
    for name in ("pietre", "linee"):
        (geom, _v), = tables[name][2]
        xyz = _coords(geom)
        assert len(xyz) > 10 and distance_to(info["face"], xyz[:, :2]).max() <= tol, name
    features = D.world_features(ch)
    assert len(features) == 2
    for f in features:
        assert distance_to(info["face"], f["xyz"][:, :2]).max() <= tol
    import ezdxf

    dxf = ezdxf.readfile(D.write_dxf_3d(ch, entry, tmp_path / "muro_3d.dxf"))
    polys = [np.asarray([v.dxf.location for v in p.vertices]) for p in dxf.modelspace().query("POLYLINE")]
    assert len(polys) == 2
    for xyz in polys:
        assert distance_to(info["face"], xyz[:, :2]).max() <= tol
    out = E.export_drawings(ch, entry, tmp_path / "export", dxf_3d=False)
    for table in ("pietre", "linee"):
        (row,) = gpkg.read_table(out["gpkg"], table)
        world = _coords(wkt.loads(row[1]["geom_world"]))
        assert distance_to(info["face"], world[:, :2]).max() <= tol, table


def test_where_the_relief_is_empty_the_drawing_lies_on_the_line(built):
    ch, info, entry = built
    top = info["height"] + info["shift"][2]
    add_units(ch, entry["id"], [unit_box(2.0, top - 0.5, 3.0, top + 0.6)])
    (geom, _values), = E.world_tables(ch, entry)["unita"][2]
    xyz = np.asarray(geom.geoms[0].exterior.coords)
    frame = E.frame_from_dict(entry["frame"])
    uvd = frame.coords(xyz)
    above = xyz[:, 2] > top + 0.15
    assert above.sum() > 10
    on_line = frame.point(uvd[above, 0], xyz[above, 2], np.zeros(above.sum()))
    assert np.abs(on_line - xyz[above]).max() < 1e-9
    assert np.abs(uvd[above, 2]).max() < 1e-9
    below = xyz[:, 2] < top - 0.1
    assert below.sum() > 10 and distance_to(info["face"], xyz[below, :2]).max() <= RES + 1e-3


def _hand_entry(ch, eid, frame, bounds, depth=0.03, res=0.02):
    """An elevation of ``frame`` written here: a relief of ``depth`` everywhere in ``bounds``."""
    import rasterio
    from rasterio.transform import from_origin

    u0, v0, u1, v1 = bounds
    entry = {"id": eid, "name": f"Prospetto {eid}", "kind": "elevation", "frame": frame.to_dict(),
             "bounds": list(bounds), "resolution": res, "crs": ch.crs, "v_is_height": True, "depth": [-0.5, 0.5],
             "orthophoto": f"elevations/{eid}/orthophoto.tif", "relief": f"elevations/{eid}/elevation.tif"}
    for name, value, bands, dtype in (("relief", depth, 1, "float32"), ("orthophoto", 150, 3, "uint8")):
        path = ch.data_dir / entry[name]
        path.parent.mkdir(parents=True, exist_ok=True)
        w, h = int(round((u1 - u0) / res)), int(round((v1 - v0) / res))
        with rasterio.open(path, "w", driver="GTiff", width=w, height=h, count=bands, dtype=dtype,
                           transform=from_origin(u0, v1, res, res), BIGTIFF="IF_SAFER") as dst:
            for b in range(1, bands + 1):
                dst.write(np.full((h, w), value, dtype), b)
    ch.meta.setdefault("elevations", []).append(entry)
    return entry


def test_planes_and_cylinders_export_as_before(built):
    ch, info, _entry = built
    sx, sy = info["shift"][:2]
    plane = E.Plane(np.array([sx, sy - 3.0, 0.0]), [1.0, 0.0, 0.0], [0.0, 0.0, 1.0])
    cyl = E.Cylinder(np.array([sx + 20.0, sy, 0.0]), np.array([0.0, 0.0, 1.0]), 3.0)
    for eid, frame, bounds in (("piano", plane, (0.0, 99.0, 4.0, 102.0)), ("cilindro", cyl, (0.0, 99.0, 4.0, 102.0))):
        entry = _hand_entry(ch, eid, frame, bounds)
        drawn = unit_box(1.0, 100.0, 2.5, 101.0)
        add_units(ch, eid, [drawn])
        (geom, _v), = E.world_tables(ch, entry)["unita"][2]
        xyz = np.asarray(geom.geoms[0].exterior.coords)
        uv = np.asarray(drawn.geometry.exterior.coords)
        assert len(xyz) == len(uv), eid
        assert np.abs(xyz - frame.point(uv[:, 0], uv[:, 1], np.zeros(len(uv)))).max() < 1e-9, eid
        world = D.drawings(ch, eid, world=True)["units"][0]["world"]
        assert np.abs(np.asarray(world.exterior.coords) - xyz).max() < 1e-9, eid
        assert "traccia" not in E.world_tables(ch, entry)


def test_the_exported_world_geopackage_has_the_trace(built, tmp_path):
    ch, info, entry = built
    add_units(ch, entry["id"], [unit_box(2.0, 100.5, 3.0, 101.0)])
    out = E.export_drawings(ch, entry, tmp_path, dxf_3d=False)
    assert out["gpkg_world"].name.endswith("_disegni_mondo.gpkg")
    (row,) = gpkg.read_table(out["gpkg_world"], "traccia")
    assert row[1]["nome"] == entry["name"] and row[1]["prospetto"] == entry["name"] and row[1]["tipo"] == "path"
    assert row[1]["prospetto_id"] == entry["id"]
    assert row[0].geom_type == "LineString"
    assert distance_to(info["face"], np.asarray(row[0].coords)[:, :2]).max() < 0.01
    tables = E.world_tables(ch, entry, trace=True)
    (line, _v), = tables["traccia"][2]
    z = np.asarray(line.coords)[:, 2]
    u0, v0, u1, v1 = E.bounds_of(ch, entry)
    assert np.allclose(z, (v0 + v1) / 2)


def test_a_transfer_keeps_what_falls_off_the_wall(built, caplog):
    ch, info, entry = built
    a, b = np.asarray(info["face"][0]), np.asarray(info["face"][1])
    plane = E.build_elevation(ch, E.plane_from_line(ch, a, b, (a + b) / 2 + [0.0, -3.0]), resolution=0.02,
                              depth=(-0.3, 0.5), max_angle=89.0, bounds=(0.5, 100.2, 4.5, 101.8))
    on_wall, off_wall = unit_box(2.0, 100.6, 2.6, 101.2, us="1"), unit_box(14.0, 100.6, 14.5, 101.2, us="2")
    add_units(ch, plane["id"], [on_wall, off_wall])
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        done = D.transfer(ch, plane["id"], entry["id"])
    assert done["units"] == 1
    assert any("cadono fuori" in r.getMessage() for r in caplog.records)
    (moved,) = D.drawings(ch, entry["id"])["units"]
    assert moved["us"] == "1"
    # the same world place: the plane's point on its surface, the path's on its own
    src = np.asarray(on_wall.geometry.exterior.coords)
    src_world = D.world_of_uv(plane, src, np.nan_to_num(D.surface_depths(ch, plane, src)))
    dst = np.asarray(moved["geometry"].exterior.coords)
    dst_world = D.world_of_uv(entry, dst, np.nan_to_num(D.surface_depths(ch, entry, dst)))
    assert np.linalg.norm(dst_world - src_world, axis=1).max() < 0.01
    assert len(D.drawings(ch, plane["id"])["units"]) == 2                      # a copy: the plane keeps both
    caplog.clear()
    done = D.transfer(ch, plane["id"], entry["id"], copy_=False)
    assert done["units"] == 1
    left = D.drawings(ch, plane["id"])["units"]
    assert [u["us"] for u in left] == ["2"]                                      # what fell off stays where it is


def _copy_entry(ch, entry, eid, **changes):
    """Another record of the same elevation (its files shared), with ``changes``."""
    other = {**entry, "id": eid, **changes}
    ch.meta["elevations"].append(other)
    return other


def test_a_corner_without_relief_takes_the_depth_of_its_neighbours(built, tmp_path):
    import rasterio

    ch, info, entry = built
    _ortho, relief = E.elevation_paths(ch, entry)
    with rasterio.open(relief) as src:
        band, profile = src.read(1), src.profile
        t = src.transform
    col = int((6.0 - t.c) / t.a)
    band[:, col - 2:col + 3] = np.nan                                            # five columns round the corner
    holed = tmp_path / "relief_holed.tif"
    with rasterio.open(holed, "w", **profile) as dst:
        dst.write(band, 1)
    other = _copy_entry(ch, entry, "buco", relief=str(holed))                   # an absolute path: data_dir / it
    data = {"units": [unit_box(5.8, 100.5, 6.2, 101.0).to_dict()], "quote": []}
    (geom, _v), = E.world_tables(ch, other, data)["unita"][2]
    xyz = np.asarray(geom.geoms[0].exterior.coords)
    assert distance_to(info["face"], xyz[:, :2]).max() <= RES + 1e-3


def _plane_entry(ch, info, eid, offset):
    """A plane of the first leg ``offset`` metres in front of its face, its relief 0 everywhere."""
    sx, sy = info["shift"][:2]
    plane = E.Plane(np.array([sx, sy - offset, 0.0]), [1.0, 0.0, 0.0], [0.0, 0.0, 1.0])
    return _hand_entry(ch, eid, plane, (0.0, 99.0, 16.0, 102.0), depth=0.0)


def test_a_wide_band_does_not_take_what_stands_off_the_wall(built):
    ch, info, entry = built
    wide = _copy_entry(ch, entry, "largo", depth=[-10.0, 10.0])             # as with depth=None on a big model
    on_line = _plane_entry(ch, info, "sul_muro", 0.0)
    add_units(ch, "sul_muro", [unit_box(2.0, 100.6, 2.5, 101.0, us="a"), unit_box(14.0, 100.6, 14.5, 101.0, us="b"),
                               unit_box(-0.4, 100.6, -0.1, 101.0, us="c")])   # c: just before the start of the path
    assert D.transfer(ch, "sul_muro", "largo")["units"] == 2
    moved = {u["us"]: u for u in D.drawings(ch, "largo")["units"]}
    assert sorted(moved) == ["a", "c"]
    src = np.asarray(unit_box(-0.4, 100.6, -0.1, 101.0).geometry.exterior.coords)
    dst = np.asarray(moved["c"]["geometry"].exterior.coords)
    assert np.abs(D.world_of_uv(on_line, src) - D.world_of_uv(wide, dst)).max() < 0.01
    _plane_entry(ch, info, "vicino", 0.04)                                       # the tolerance: 5 cm at 1 cm
    _plane_entry(ch, info, "lontano", 0.07)
    add_units(ch, "vicino", [unit_box(2.0, 100.6, 2.5, 101.0)])
    add_units(ch, "lontano", [unit_box(2.0, 100.6, 2.5, 101.0)])
    assert D.transfer(ch, "vicino", "largo")["units"] == 1
    assert D.transfer(ch, "lontano", "largo")["units"] == 0
