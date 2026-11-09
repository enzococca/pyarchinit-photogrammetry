"""The height of a chunk on stable ground (archaeo/coreg.height_offset, engine/stable_height): two synthetic DEMs of
the same rough ground, the second higher by a bias (and tilted, or with a pit dug in between), measured as they are
placed and kept as a height tie with its anchors in the internal frames of the chunks."""
from __future__ import annotations

import numpy as np
import pytest

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.archaeo import coreg
from pyarchinit_photogrammetry.core import free_drawings
from pyarchinit_photogrammetry.engine import stable_height, ties
from pyarchinit_photogrammetry.engine.dem import NODATA, write_geotiff

CRS = "EPSG:3004"
RES = 0.02
SIZE = 6.0
X0, Y1 = 2480000.0, 4500006.0  # the synthetic origin of the trench
XC, YC = X0 + SIZE / 2, Y1 - SIZE / 2
BIAS = 0.37


def ground(x, y):
    """Rough ground with slopes every way."""
    rng = np.random.default_rng(7)
    z = 100.0 + 0.04 * (x - XC) + 0.02 * (y - YC)
    for lam in np.geomspace(0.4, 3.0, 10):
        ang, phase = rng.uniform(0, 2 * np.pi, 2)
        k = 2 * np.pi / lam
        z = z + 0.12 / k * np.sin(k * (np.cos(ang) * (x - XC) + np.sin(ang) * (y - YC)) + phase)
    return z


def cells(origin=(X0, Y1), size=SIZE, res=RES):
    n = int(round(size / res))
    rows, cols = np.mgrid[0:n, 0:n]
    return origin[0] + (cols + 0.5) * res, origin[1] - (rows + 0.5) * res


def pit(x, y):
    """1 m deep over about 10 % of the area."""
    return np.where(np.hypot(x - (XC + 1.2), y - (YC - 0.8)) < 1.07, 1.0, 0.0)


def surveys(noise=0.001, extra=None, origin2=(X0, Y1)):
    """``(z of A, z of B, transform)``: B = A + BIAS (+ ``extra(x, y)``), with noise."""
    from rasterio.transform import from_origin

    x, y = cells()
    za = ground(x, y)
    x2, y2 = cells(origin2)
    zb = ground(x2, y2) + BIAS + (extra(x2, y2) if extra is not None else 0.0)
    zb = zb + np.random.default_rng(1).normal(0, noise, zb.shape)
    return za.astype(np.float32), zb.astype(np.float32), from_origin(X0, Y1, RES, RES), from_origin(*origin2, RES, RES)


def project(tmp_path, extra=None, origin2=(X0, Y1), crs_b=CRS, built_b=None, shift_b=(0.0, 0.0, 0.0)):
    """A document with chunks A and B, their DEMs written as :func:`surveys` (B's file shifted by ``shift_b`` in
    the world it was built in, ``built_b`` its stamp)."""
    doc = ppg.Document()
    a, b = doc.add_chunk("A"), doc.add_chunk("B")
    a.crs, b.crs = CRS, crs_b
    doc.save(tmp_path / "p.ppg")
    za, zb, _tr, _tr2 = surveys(extra=extra, origin2=origin2)
    a._set_product("dem", write_geotiff(a.data_dir / "dem.tif", za, (X0, Y1), RES, CRS, nodata=NODATA))
    o2 = (origin2[0] + shift_b[0], origin2[1] + shift_b[1])
    b._set_product("dem", write_geotiff(b.data_dir / "dem.tif", zb + np.float32(shift_b[2]), o2, RES,
                                        None if crs_b == "LOCAL" else crs_b, nodata=NODATA))
    if built_b is not None:
        b.meta.setdefault("built_in", {})["dem"] = np.asarray(built_b, float).tolist()
    return doc, a, b


def test_height_offset_finds_the_bias_and_ignores_a_pit():
    za, zb, tr, _ = surveys(extra=lambda x, y: -pit(x, y))
    x, y = cells()
    assert 0.08 < pit(x, y).mean() < 0.12
    r = coreg.height_offset(za, tr, zb, tr)
    assert abs(r.dz - BIAS) < 0.002 and r.nmad_after < 0.002 and not r.plane
    assert r.cells < 0.95 * r.compared  # the pit left out
    assert set(r.to_dict()) >= {"dz", "tilt_x", "tilt_y", "nmad_before", "nmad_after", "median", "cells", "plane"}


def test_height_offset_plane():
    za, zb, tr, _ = surveys(extra=lambda x, y: -BIAS + 0.2 + 0.01 * (x - XC) - 0.005 * (y - YC))
    r = coreg.height_offset(za, tr, zb, tr, plane=True)
    assert abs(r.tilt_x - 0.01) < 1e-4 and abs(r.tilt_y + 0.005) < 1e-4
    assert abs(r.dz - 0.2) < 0.002 and r.plane and r.nmad_after < r.nmad_before


def _world_delta(doc, tie, i=0):
    a, b = ties._chunk(doc, tie.a), ties._chunk(doc, tie.b)
    wa = a.to_world(np.array([tie.anchors[i]["a"]]))[0]
    wb = b.to_world(np.array([tie.anchors[i]["b"]]))[0]
    return wb - wa


def test_measure_gives_anchors_in_internal_frames(tmp_path):
    doc, a, b = project(tmp_path, extra=lambda x, y: -pit(x, y))
    # transforms that are not the identity: the anchors are internal points of each chunk
    ta = np.eye(4)
    ta[:3, 3] = (10.0, -5.0, 2.0)
    a.transform = ta
    a.meta.setdefault("built_in", {})["dem"] = ta.tolist()
    tie = stable_height.measure(doc, "B", "A")
    assert len(tie.anchors) == 1 and abs(tie.dz - BIAS) < 0.002 and tie.a == a.key and tie.b == b.key
    assert np.allclose(_world_delta(doc, tie), (0.0, 0.0, BIAS), atol=0.002)
    assert tie.anchors[0]["precision"] == pytest.approx(max(tie.nmad / 10, 0.005))
    assert tie.id == "" and not doc.meta.get("height_ties")  # not stored
    stored = stable_height.add(doc, tie)
    assert stored.id and stored.name == "quota stabile 1"
    again = stable_height.heights_of(doc)
    assert [t.to_dict() for t in again] == [stored.to_dict()]
    # the pit is never the anchor: the anchor's world point is on ground that did not change
    xw, yw = (a.to_world(np.array([tie.anchors[0]["a"]]))[0])[:2]
    assert pit(xw, yw) == 0.0
    remeasured = stable_height.remeasure(doc, stored.id)
    assert remeasured.id == stored.id and remeasured.name == stored.name
    assert stable_height.remove(doc, stored.id).id == stored.id and not stable_height.heights_of(doc)
    assert "height_ties" not in doc.meta
    with pytest.raises(KeyError):
        stable_height.remove(doc, stored.id)


def test_measure_inside_a_polygon_layer(tmp_path):
    # the right 60 % of B rose by another 0.10 m (changed ground, the larger part): only the left strip is stable
    split = X0 + 0.4 * SIZE
    doc, a, b = project(tmp_path, extra=lambda x, y: np.where(x > split, 0.10, 0.0))
    auto = stable_height.measure(doc, "B", "A")
    assert abs(auto.dz - (BIAS + 0.10)) < 0.005  # the automatic rules take the larger part
    ring = [[X0 + 0.1, Y1 - 0.1, 100.0], [split - 0.1, Y1 - 0.1, 100.0], [split - 0.1, Y1 - SIZE + 0.1, 100.0],
            [X0 + 0.1, Y1 - SIZE + 0.1, 100.0]]
    free_drawings.add(a, "polygon", ring, layer="Stabile")
    tie = stable_height.measure(doc, "B", "A", area="Stabile")
    assert abs(tie.dz - BIAS) < 0.002 and tie.area == {"layer": "Stabile"}
    with pytest.raises(ValueError, match="no polygon"):
        stable_height.measure(doc, "B", "A", area={"layer": "Nessuno"})


def test_plane_has_three_spread_anchors(tmp_path):
    from scipy.spatial import ConvexHull

    doc, a, b = project(tmp_path, extra=lambda x, y: -BIAS + 0.2 + 0.01 * (x - XC) - 0.005 * (y - YC))
    tie = stable_height.measure(doc, "B", "A", plane=True)
    assert tie.plane and len(tie.anchors) == 3
    xy = np.array([a.to_world(np.array([x["a"]]))[0][:2] for x in tie.anchors])
    u, v = xy[1] - xy[0], xy[2] - xy[0]
    tri = 0.5 * abs(u[0] * v[1] - u[1] * v[0])
    rows, cols = np.mgrid[0:300, 0:300]
    hull = ConvexHull(np.column_stack([cols.ravel(), rows.ravel()]) * RES).volume  # the whole DEM: all stable
    assert tri >= 0.25 * hull
    for i, x in enumerate(tie.anchors):
        wx, wy = xy[i]
        expected = 0.2 + 0.01 * (wx - XC) - 0.005 * (wy - YC)
        assert _world_delta(doc, tie, i)[2] == pytest.approx(expected, abs=0.003)
        assert x["precision"] == pytest.approx(max(tie.nmad / 10, 0.005) * np.sqrt(3))


def test_dem_built_in_another_transform_is_moved(tmp_path):
    # B's DEM was built when B's transform put it 1 m east, 0.5 m north and 0.2 m higher; the transform was corrected
    # later (identity now): the DEM is moved with the chunk, never used where it was built
    built = np.eye(4)
    built[:3, 3] = (1.0, 0.5, 0.2)
    doc, a, b = project(tmp_path, built_b=built, shift_b=(1.0, 0.5, 0.2))
    z, tr = stable_height.dem_in_world(b, a)
    assert tr.c == pytest.approx(X0) and tr.f == pytest.approx(Y1) and tr.b == 0 and tr.d == 0
    tie = stable_height.measure(doc, "B", "A")
    assert abs(tie.dz - BIAS) < 0.002
    # built in a frame turned by 3 degrees about the middle: resampled north up, the bias found again
    rot = np.eye(4)
    c, s = np.cos(np.radians(3.0)), np.sin(np.radians(3.0))
    rot[:2, :2] = [[c, -s], [s, c]]
    rot[:2, 3] = np.array([XC, YC]) - rot[:2, :2] @ np.array([XC, YC])
    x, y = cells(origin=(X0 - 0.5, Y1 + 0.5), size=SIZE + 1.0)
    inv = np.linalg.inv(rot)
    gx = inv[0, 0] * x + inv[0, 1] * y + inv[0, 3]  # the ground of each cell of the file, in the world of now
    gy = inv[1, 0] * x + inv[1, 1] * y + inv[1, 3]
    zr = (ground(gx, gy) + BIAS).astype(np.float32)
    b._set_product("dem", write_geotiff(b.data_dir / "dem.tif", zr, (X0 - 0.5, Y1 + 0.5), RES, CRS, nodata=NODATA))
    b.meta["built_in"]["dem"] = rot.tolist()
    z, tr = stable_height.dem_in_world(b, a)
    assert tr.b == 0 and tr.d == 0 and np.isfinite(z).mean() > 0.5
    assert abs(stable_height.measure(doc, "B", "A").dz - BIAS) < 0.002


def test_local_and_projected_are_refused(tmp_path):
    doc, a, b = project(tmp_path, crs_b="LOCAL")
    with pytest.raises(ties.TieError) as err:
        stable_height.measure(doc, "B", "A")
    expected = ties._not_same_frame("B", "LOCAL", "A", CRS)
    assert str(err.value) == str(expected) and err.value.italian == expected.italian
    with pytest.raises(ties.TieError):
        stable_height.dem_in_world(b, a)


def test_no_overlap_says_so(tmp_path):
    doc, a, b = project(tmp_path, origin2=(X0 + 50.0, Y1))
    with pytest.raises(ValueError, match="overlap"):
        stable_height.measure(doc, "B", "A")
    za, zb, tr, tr2 = surveys(origin2=(X0 + 50.0, Y1))
    with pytest.raises(ValueError, match="overlap"):
        coreg.height_offset(za, tr, zb, tr2)


def test_two_projected_systems_are_reprojected(tmp_path):
    # B in another projected system: its DEM reprojected onto A's, the bias found again (synthetic ground near the
    # origin of EPSG:3004, B written in EPSG:3003 through PROJ)
    from pyarchinit_photogrammetry.core import crs as crs_mod

    doc, a, b = project(tmp_path)
    za, zb, tr, _ = surveys()
    other = "EPSG:3003"
    x, y = cells()
    pts = crs_mod.transform_points(np.column_stack([x.ravel(), y.ravel(), np.zeros(x.size)]), CRS, other)
    x0, y1 = pts[:, 0].min() - 0.5, pts[:, 1].max() + 0.5
    n = int(round((pts[:, 0].max() + 0.5 - x0) / RES))
    rows, cols = np.mgrid[0:n, 0:n]
    gx, gy = x0 + (cols + 0.5) * RES, y1 - (rows + 0.5) * RES
    back = crs_mod.transform_points(np.column_stack([gx.ravel(), gy.ravel(), np.zeros(gx.size)]), other, CRS)
    zb2 = (ground(back[:, 0], back[:, 1]) + BIAS).reshape(gx.shape).astype(np.float32)
    b.crs = other
    b._set_product("dem", write_geotiff(b.data_dir / "dem.tif", zb2, (x0, y1), RES, other, nodata=NODATA))
    tie = stable_height.measure(doc, "B", "A")
    assert abs(tie.dz - BIAS) < 0.003
    assert np.allclose(_world_delta_systems(doc, tie, CRS), (0.0, 0.0, BIAS), atol=0.003)


def _world_delta_systems(doc, tie, system):
    from pyarchinit_photogrammetry.core import crs as crs_mod

    a, b = ties._chunk(doc, tie.a), ties._chunk(doc, tie.b)
    wa = a.to_world(np.array([tie.anchors[0]["a"]]))[0]
    wb = crs_mod.transform_points(b.to_world(np.array([tie.anchors[0]["b"]])), b.crs, system)[0]
    return wb - wa
