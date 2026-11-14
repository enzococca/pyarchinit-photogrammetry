"""Measuring on the 3D view independent of the level of detail shown: a model shown as its coarse preview is picked
and measured on the model at full resolution (the point, the length along the surface, at two zooms and with the
model shown whole), a miss of the model is nothing there, and a pick on a cloud is refined on the whole cloud through
its grid index, built in the background for a big cloud while the clicks wait. A small terrain with an analytic
surface in a chunk far from the origin (UTM-size coordinates, a scaled and turned transform)."""
from __future__ import annotations

import functools
import os

import numpy as np
import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")

ORIGIN = np.array([500100.0, 4500400.0, 200.0])
SIDE = 20.0  # metres


def f(x, y):
    return 0.4 * np.sin(2 * np.pi * x / 7.0) * np.cos(2 * np.pi * y / 5.0) + 0.03 * x


def _transform() -> np.ndarray:
    a = np.radians(30.0)
    T = np.eye(4)
    T[:3, :3] = 2.0 * np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1.0]])
    T[:3, 3] = (500123.4, 4500456.7, 210.0)
    return T


def _internal(local) -> np.ndarray:
    T = _transform()
    return (np.asarray(local, np.float64) + ORIGIN - T[:3, 3]) @ np.linalg.inv(T[:3, :3]).T


def _section(a, b, n: int = 100_001) -> float:
    t = np.linspace(0.0, 1.0, n)
    x, y = a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])
    return float(np.linalg.norm(np.diff(np.column_stack([x, y, f(x, y)]), axis=0), axis=1).sum())


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _chunk(tmp_path, model: bool = True, cloud: int = 0):
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.io.mesh_ply import write_mesh_ply

    doc = ppg.Document()
    ch = doc.add_chunk("terreno")
    doc.save(tmp_path / "t.ppg")
    ch.transform = _transform()
    ch.crs = "EPSG:32633"
    if model:  # 2 x 199 x 199 = 79 202 faces, an edge of 10 cm
        s = np.linspace(0.0, SIDE, 200)
        X, Y = np.meshgrid(s, s)
        V = _internal(np.column_stack([X.ravel(), Y.ravel(), f(X, Y).ravel()]))
        i, j = np.meshgrid(np.arange(199), np.arange(199))
        a = (j * 200 + i).ravel()
        F = np.vstack([np.column_stack([a, a + 1, a + 201]), np.column_stack([a, a + 201, a + 200])])
        ch._set_product("model", write_mesh_ply(ch.data_dir / "model.ply", V, F,
                                                np.full((len(V), 3), 180, np.uint8)))
    if cloud:
        from pyarchinit_photogrammetry.core.pointcloud import PointCloud

        rng = np.random.default_rng(3)
        x, y = rng.random(cloud) * SIDE, rng.random(cloud) * SIDE
        xyz = _internal(np.column_stack([x, y, f(x, y)]))
        ch.point_cloud = PointCloud(xyz, rgb=np.full((cloud, 3), 150, np.uint8))
    doc.save()
    return doc, ch


def _view(qapp, ch, kind: str, max_faces: int | None = None, monkeypatch=None):
    from pyarchinit_photogrammetry.gui import model_view as mvm

    if max_faces is not None:  # the model shown as its coarse preview (the level of detail of a big model)
        monkeypatch.setattr(mvm, "build_scene", functools.partial(mvm.build_scene, max_faces=max_faces))
    mv = mvm.ModelView()
    mv.resize(1000, 800)
    mv.show()
    qapp.processEvents()
    mv.set_chunk(ch, kind)
    mv.canvas.set_ortho(True)
    mv.canvas.top_view()
    qapp.processEvents()
    return mv


def _pixel(mv, local):
    sd = mv.sd
    px, py, _d, ok = mv.canvas.projector()(sd.to_display(np.atleast_2d(np.asarray(local) + ORIGIN) - sd.offset))
    assert ok.all()
    return px, py


def _measure_line(qapp, mv, a, b):
    got = []
    mv.measure_drawn.connect(lambda kind, xyz, draped: got.append((xyz, draped)))
    assert mv.start_measure("line")
    px, py = _pixel(mv, np.array([a, b]))
    for k in range(2):
        mv._click(float(px[k]), float(py[k]))
    mv._finish()
    mv.stop_drawing()
    xyz, draped = got[-1]
    from pyarchinit_photogrammetry.gui import model_measure as mm

    return mm.values("line", mv.chunk.to_world(xyz), mv.chunk.to_world(draped)), len(draped)


def _wait(qapp, cond, timeout=60.0):
    import time

    t0 = time.monotonic()
    while not cond():
        qapp.processEvents()
        time.sleep(0.01)
        assert time.monotonic() - t0 < timeout


def test_the_preview_shown_is_measured_at_full_resolution(qapp, tmp_path, monkeypatch):
    _doc, ch = _chunk(tmp_path)
    a, b = np.array([2.0, 3.0, f(2.0, 3.0)]), np.array([17.0, 15.0, f(17.0, 15.0)])
    truth = _section(a, b)
    mv = _view(qapp, ch, "model", max_faces=4000, monkeypatch=monkeypatch)
    try:
        assert len(mv.sd.mesh_faces) < 6000  # the coarse level is what is shown
        assert mv._caster() is not None  # 79 202 faces: built at once, from model.ply
        # points: at their own pixels the line of sight meets the full model, not the preview
        rng = np.random.default_rng(0)
        local = np.column_stack([2 + rng.random(30) * 16, 2 + rng.random(30) * 16])
        local = np.column_stack([local, f(local[:, 0], local[:, 1])])
        px, py = _pixel(mv, local)
        project = mv.canvas.projector()
        errs = [np.linalg.norm(ch.to_world(mv._surface_at(float(x), float(y), project, refine=True)[None])[0]
                               - ORIGIN - p) for x, y, p in zip(px, py, local)]
        assert max(errs) < 0.002, max(errs)  # the full model is within a millimetre of the terrain (10 cm edges)
        coarse = [np.linalg.norm(ch.to_world(mv.pick_point(float(x), float(y), project, reach=60)[None])[0] - ORIGIN - p)
                  for x, y, p in zip(px, py, local)]
        assert np.median(coarse) > 10 * np.median(errs)  # what the preview alone would give
        # the length along the surface at full resolution, the same at another zoom
        # a miss: nothing of the model under the cursor (beside the terrain)
        mx, my = _pixel(mv, np.array([-3.0, -3.0, 0.0]))
        assert mv._surface_at(float(mx[0]), float(my[0]), project, refine=True) is None
        v1, n1 = _measure_line(qapp, mv, a, b)
        assert n1 > 150  # a point every half edge (5 cm) of the full model along 19 m, not every 20 pixels
        assert abs(v1["length_surface"] - truth) < 0.003, (v1["length_surface"], truth)
        cam = mv.canvas.get_cam()
        cam.distance *= 0.6
        mv.canvas.set_cam(cam)
        qapp.processEvents()
        v2, _n2 = _measure_line(qapp, mv, a, b)
        assert abs(v2["length_surface"] - v1["length_surface"]) < 1e-3
    finally:
        mv.close()
    monkeypatch.undo()
    (ch.data_dir / "model_preview.ply").unlink()  # the preview of 4000 faces made above, kept beside the model
    whole = _view(qapp, ch, "model")  # the model shown whole: the same measure
    try:
        assert len(whole.sd.mesh_faces) == 79_202
        v3, _n3 = _measure_line(qapp, whole, a, b)
        assert abs(v3["length_surface"] - v1["length_surface"]) < 1e-3
        assert abs(v3["length_3d"] - v1["length_3d"]) < 1e-3
    finally:
        whole.close()


def test_cloud_index_finds_the_points_near_a_line(monkeypatch):
    from pyarchinit_photogrammetry.gui import pick_index

    rng = np.random.default_rng(1)
    xyz = rng.normal(0.0, 3.0, (20_000, 3)) + (1000.0, -200.0, 50.0)
    monkeypatch.setattr(pick_index, "BLOCK", 3000)  # read in blocks, as a big file is

    def blocks():
        for s in range(0, len(xyz), pick_index.BLOCK):
            yield xyz[s:s + pick_index.BLOCK]

    index = pick_index.CloudIndex(len(xyz), blocks, cells=24)
    assert index.points.dtype == np.float32 and len(index.points) == len(xyz)
    for _ in range(5):
        d = rng.normal(0.0, 1.0, 3)
        d /= np.linalg.norm(d)
        o = rng.normal(0.0, 1.0, 3) + (1000.0, -200.0, 50.0) - 10.0 * d  # through the cloud at t = 10
        middle, half, radius = 10.0, 6.0, 0.8
        got = index.near_line(o, d, middle, half, radius)
        v = xyz - o
        t = v @ d
        want = xyz[(np.linalg.norm(v - t[:, None] * d, axis=1) <= radius) & (np.abs(t - middle) <= half + radius)]
        assert len(got) == len(want) and len(want) > 0
        assert np.abs(np.sort(got, axis=0) - np.sort(want, axis=0)).max() < 1e-4


def test_a_big_cloud_is_indexed_in_the_background_and_the_click_waits(qapp, tmp_path, monkeypatch):
    from pyarchinit_photogrammetry.gui import pick_index

    monkeypatch.setattr(pick_index, "SYNC_POINTS", 1000)  # 200 000 points: "big"
    _doc, ch = _chunk(tmp_path, model=False, cloud=200_000)
    mv = _view(qapp, ch, "point_cloud")
    got = []
    mv.measure_drawn.connect(lambda kind, xyz, draped: got.append(xyz))
    try:
        p = np.array([9.0, 11.0, f(9.0, 11.0)])
        assert mv.start_measure("point")
        assert mv.caster_preparing  # the index started with the tool, in a worker thread
        px, py = _pixel(mv, p)
        mv._click(float(px[0]), float(py[0]))
        assert mv._kept_clicks and not got  # kept while the index is built
        _wait(qapp, lambda: not mv.caster_preparing)
        _wait(qapp, lambda: bool(got))
        world = ch.to_world(np.asarray(got[0]).reshape(-1, 3))[0] - ORIGIN
        assert np.linalg.norm(world - p) < 0.01, world - p  # refined on the whole cloud, at the target
    finally:
        mv.close()


def test_a_model_cast_on_in_pieces_hits_as_a_whole():
    from pyarchinit_photogrammetry import _env

    _env.load_open3d_first()
    from pyarchinit_photogrammetry.engine.visibility import Raycaster
    from pyarchinit_photogrammetry.gui.pick_index import PiecewiseCaster

    s = np.linspace(-5.0, 5.0, 120)
    X, Y = np.meshgrid(s, s)
    V = np.column_stack([X.ravel(), Y.ravel(), f(X, Y).ravel()]).astype(np.float32)
    i, j = np.meshgrid(np.arange(119), np.arange(119))
    a = (j * 120 + i).ravel()
    F = np.vstack([np.column_stack([a, a + 1, a + 121]), np.column_stack([a, a + 121, a + 120])]).astype(np.uint32)
    pieces = PiecewiseCaster(V, F, piece=3000)
    assert len(pieces.pieces) == int(np.ceil(len(F) / 3000))
    whole = Raycaster(V, F)
    rng = np.random.default_rng(2)
    o = np.column_stack([rng.uniform(-6, 6, 2000), rng.uniform(-6, 6, 2000), np.full(2000, 3.0)])
    d = rng.normal(0.0, 0.3, (2000, 3))
    d[:, 2] = -1.0
    d /= np.linalg.norm(d, axis=1)[:, None]
    t1, t2 = pieces.hit_distance(o, d), whole.hit_distance(o, d)
    assert np.array_equal(np.isfinite(t1), np.isfinite(t2)) and np.isfinite(t1).sum() > 1000
    ok = np.isfinite(t1)
    assert np.abs(t1[ok] - t2[ok]).max() < 1e-5
