"""Picking and measuring on large models and clouds in the 3D view: a benchmark, not a test (never collected by
pytest: it writes hundreds of megabytes and takes minutes).

A procedural terrain z = f(x, y) (sines over a 100 x 100 m square, metres, EPSG:32633 with UTM-size coordinates) is
written as a triangle grid (``model.ply`` in the internal frame of a chunk whose transform scales, turns and moves it
far from the origin) or as a cloud of points on it. The 3D view (:class:`..gui.model_view.ModelView`, the software
canvas under ``QT_QPA_PLATFORM=offscreen``) shows it as it shows a real chunk: the preview of a model of more than
600 000 faces, a sample of the points. Then, at random places of the surface, for each size:

* the time to show it, the time and memory of the ray caster of the model (or of the index of the cloud) built in the
  background, the time of one pick (a click of a measure) and its error against the analytic surface (the line of
  sight through the target's own pixel meets the surface at the target: the distance between them);
* a distance along the surface and an area on the surface against their analytic values;
* ``--vtk``: the same scene drawn by VTK off screen (pyvista ``off_screen``: works on macOS without a window), the
  projection and the line of sight of :class:`..gui.model_view.VtkCanvas` read from that real VTK camera, the hit of
  our pick compared with VTK's own pickers on the image drawn (``vtkCellPicker`` on the geometry shown,
  ``vtkWorldPointPicker`` on the depth buffer).

Run from the repository (the venv of the project)::

    QT_QPA_PLATFORM=offscreen PPG_RENDERER=software PYTHONPATH=src python tools/bench_model_pick.py \\
        --work /Users/Shared/ppg_measure_work --faces 5e6 20e6 --points 20e6 --vtk

Each size is written, measured and deleted before the next one. Results are printed as one JSON line per case.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import numpy as np

L = 100.0  # metres of side of the terrain
CRS = "EPSG:32633"
NOISE = 0.002  # metres: the noise of the synthetic dense cloud across the surface (a good dense cloud)


def f(x, y):
    """The analytic terrain (metres): two sines and a gentle slope."""
    return 1.5 * np.sin(2 * np.pi * x / 23.0) * np.cos(2 * np.pi * y / 17.0) + 0.02 * x


def grad(x, y):
    fx = 1.5 * (2 * np.pi / 23.0) * np.cos(2 * np.pi * x / 23.0) * np.cos(2 * np.pi * y / 17.0) + 0.02
    fy = -1.5 * (2 * np.pi / 17.0) * np.sin(2 * np.pi * x / 23.0) * np.sin(2 * np.pi * y / 17.0)
    return fx, fy


def transform() -> np.ndarray:
    """internal -> world: scale 2 (an internal unit is half a metre), 30 degrees about z, UTM-size offset."""
    a = np.radians(30.0)
    R = np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1.0]])
    T = np.eye(4)
    T[:3, :3] = 2.0 * R
    T[:3, 3] = (500123.4, 4500456.7, 210.0)
    return T


ORIGIN = np.array([500100.0, 4500400.0, 200.0])  # the world (UTM) place of the local (0, 0, 0) of the terrain


def to_internal(local: np.ndarray) -> np.ndarray:
    """Internal coordinates of terrain points given in local metres (``ORIGIN`` + local is the UTM point)."""
    T = transform()
    return (np.asarray(local, np.float64) + ORIGIN - T[:3, 3]) @ np.linalg.inv(T[:3, :3]).T


def rss_mb() -> float:
    try:
        import psutil

        return psutil.Process().memory_info().rss / 2 ** 20
    except ImportError:
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 2 ** 20


class Peak:
    """The highest resident memory seen while it runs (sampled every 50 ms)."""

    def __enter__(self):
        self.peak, self._stop = rss_mb(), threading.Event()

        def watch():
            while not self._stop.wait(0.05):
                self.peak = max(self.peak, rss_mb())

        self._t = threading.Thread(target=watch, daemon=True)
        self._t.start()
        return self

    def __exit__(self, *_a):
        self._stop.set()
        self._t.join()
        self.peak = max(self.peak, rss_mb())


def write_terrain(path: Path, faces: int, pages: int = 0) -> int:
    """A grid of about ``faces`` triangles on the terrain, internal coordinates, written row block by row block;
    with ``pages`` texture pages (texcoord and texnumber per face). Returns the faces written."""
    n = int(round(np.sqrt(faces / 2.0))) + 1
    nf = 2 * (n - 1) ** 2
    vdt = np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("red", "u1"), ("green", "u1"), ("blue", "u1")])
    fields = [("n", "u1"), ("vertex_indices", "<i4", (3,))]
    if pages:
        fields += [("nt", "u1"), ("texcoord", "<f4", (6,)), ("texnumber", "<i4")]
    fdt = np.dtype(fields)
    head = ["ply", "format binary_little_endian 1.0"]
    head += [f"comment TextureFile page_{k}.jpg" for k in range(pages)]
    head += [f"element vertex {n * n}", "property float x", "property float y", "property float z",
             "property uchar red", "property uchar green", "property uchar blue", f"element face {nf}",
             "property list uchar int vertex_indices"]
    if pages:
        head += ["property list uchar float texcoord", "property int texnumber"]
    head.append("end_header")
    s = np.linspace(0.0, L, n)
    rows = max(1, 4_000_000 // n)
    with open(path, "wb") as fh:
        fh.write(("\n".join(head) + "\n").encode())
        for r0 in range(0, n, rows):
            yy = s[r0:r0 + rows]
            X, Y = np.meshgrid(s, yy)
            W = np.column_stack([X.ravel(), Y.ravel(), f(X, Y).ravel()])
            v = np.zeros(len(W), vdt)
            inn = to_internal(W)
            v["x"], v["y"], v["z"] = inn[:, 0], inn[:, 1], inn[:, 2]
            g = np.clip((W[:, 2] + 2) * 50, 0, 255).astype(np.uint8)
            v["red"], v["green"], v["blue"] = g, 180, 255 - g
            fh.write(v.tobytes())
        for r0 in range(0, n - 1, rows):
            r1 = min(n - 1, r0 + rows)
            i, j = np.meshgrid(np.arange(n - 1), np.arange(r0, r1))
            a = (j * n + i).ravel()
            b, c, d = a + 1, a + n, a + n + 1
            tri = np.empty((2 * len(a), 3), np.int32)
            tri[0::2] = np.column_stack([a, b, d])
            tri[1::2] = np.column_stack([a, d, c])
            rec = np.zeros(len(tri), fdt)
            rec["n"], rec["vertex_indices"] = 3, tri
            if pages:
                col = (tri % n) / (n - 1)
                row = (tri // n) / (n - 1)
                page = np.minimum((row[:, 0] * pages).astype(np.int32), pages - 1)
                v_in = row * pages - page[:, None]
                rec["nt"] = 6
                rec["texcoord"] = np.column_stack([col[:, 0], v_in[:, 0], col[:, 1], v_in[:, 1], col[:, 2],
                                                   v_in[:, 2]]).astype(np.float32)
                rec["texnumber"] = page
            fh.write(rec.tobytes())
    return nf


def write_cloud(path: Path, points: int, seed: int = 0) -> None:
    """``points`` points spread at random on the terrain (internal coordinates, float), written block by block."""
    rng = np.random.default_rng(seed)
    vdt = np.dtype([("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("red", "u1"), ("green", "u1"), ("blue", "u1")])
    head = ["ply", "format binary_little_endian 1.0", f"element vertex {points}", "property float x",
            "property float y", "property float z", "property uchar red", "property uchar green",
            "property uchar blue", "end_header"]
    with open(path, "wb") as fh:
        fh.write(("\n".join(head) + "\n").encode())
        for s in range(0, points, 5_000_000):
            k = min(5_000_000, points - s)
            x, y = rng.random(k) * L, rng.random(k) * L
            inn = to_internal(np.column_stack([x, y, f(x, y) + rng.normal(0.0, NOISE, k)]))
            v = np.zeros(k, vdt)
            v["x"], v["y"], v["z"] = inn[:, 0], inn[:, 1], inn[:, 2]
            v["red"] = v["green"] = v["blue"] = 160
            fh.write(v.tobytes())


def make_chunk(work: Path, name: str):
    import pyarchinit_photogrammetry as ppg

    doc = ppg.Document()
    ch = doc.add_chunk(name)
    doc.save(work / f"{name}.ppg")
    ch.transform = transform()
    ch.crs = CRS
    return doc, ch


def surface_length(a, b, n: int = 200_001) -> float:
    """Length along the terrain of the plan segment a-b (world, metres): the vertical section."""
    t = np.linspace(0.0, 1.0, n)
    x, y = a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])
    p = np.column_stack([x, y, f(x, y)])
    return float(np.linalg.norm(np.diff(p, axis=0), axis=1).sum())


def surface_area(poly, n: int = 2000) -> float:
    """Area of the terrain above the plan polygon ``poly`` (world, metres): sqrt(1 + |grad f|^2) summed on a grid."""
    from matplotlib.path import Path as MPath

    lo, hi = poly[:, :2].min(axis=0), poly[:, :2].max(axis=0)
    xs, ys = np.linspace(lo[0], hi[0], n), np.linspace(lo[1], hi[1], n)
    X, Y = np.meshgrid((xs[:-1] + xs[1:]) / 2, (ys[:-1] + ys[1:]) / 2)
    inside = MPath(poly[:, :2]).contains_points(np.column_stack([X.ravel(), Y.ravel()])).reshape(X.shape)
    fx, fy = grad(X, Y)
    cell = (xs[1] - xs[0]) * (ys[1] - ys[0])
    return float((np.sqrt(1 + fx ** 2 + fy ** 2) * inside).sum() * cell)


def wait_for(app, cond, timeout: float = 1800.0) -> float:
    t0 = time.monotonic()
    while not cond():
        app.processEvents()
        time.sleep(0.01)
        if time.monotonic() - t0 > timeout:
            raise TimeoutError("timed out")
    return time.monotonic() - t0


def view(app, ch, kind: str, size=(1600, 1200), vtk_scene: bool = False):
    """The 3D view of ``ch`` (software canvas); ``vtk_scene``: the scene the VTK canvas would get (up to 3 million
    points, a textured model up to 8 million faces shown whole), which is what a pick projects and searches."""
    from pyarchinit_photogrammetry.gui.model_view import ModelView

    if vtk_scene:
        class VtkSized(ModelView):  # the scene of the VTK canvas on the software canvas
            backend = property(lambda self: "vtk")

        mv = VtkSized()
    else:
        mv = ModelView()
    mv.resize(*size)
    mv.show()
    app.processEvents()
    t0 = time.monotonic()
    with Peak() as pk:
        mv.set_chunk(ch, kind)
        app.processEvents()
    shown = time.monotonic() - t0
    mv.canvas.set_ortho(True)
    mv.canvas.top_view()
    app.processEvents()
    return mv, shown, pk.peak


def targets(n: int, seed: int = 1, margin: float = 10.0) -> np.ndarray:
    """``n`` random points of the terrain, world (UTM) coordinates."""
    rng = np.random.default_rng(seed)
    x, y = margin + rng.random(n) * (L - 2 * margin), margin + rng.random(n) * (L - 2 * margin)
    return np.column_stack([x, y, f(x, y)]) + ORIGIN


def pixels(mv, world):
    sd = mv.sd
    px, py, _d, ok = mv.canvas.projector()(sd.to_display(np.asarray(world, np.float64) - sd.offset))
    return px, py, ok


def pick_errors(mv, ch, world, refine: bool = True, shown_only: bool = False):
    """(seconds per pick, errors in metres) of picks at the exact pixels of the ``world`` targets; ``shown_only``: on
    the points and vertices shown alone (what a model gives before its ray caster is ready)."""
    px, py, ok = pixels(mv, world)
    project = mv.canvas.projector()
    times, errs = [], []
    for k in np.nonzero(ok)[0]:
        t0 = time.perf_counter()
        if shown_only:
            p = mv.pick_point(float(px[k]), float(py[k]), project)
        else:
            p = mv._surface_at(float(px[k]), float(py[k]), project, refine=refine)
        times.append(time.perf_counter() - t0)
        errs.append(np.inf if p is None else float(np.linalg.norm(ch.to_world(np.asarray(p)[None])[0] - world[k])))
    return np.array(times), np.array(errs)


def measure_line(app, mv, ch, a, b):
    """A distance measured through the clicks of the view (the line draped as the 3D view does), its values."""
    from pyarchinit_photogrammetry.gui import model_measure as mm

    got = []
    mv.measure_drawn.connect(lambda kind, xyz, draped: got.append((kind, xyz, draped)))
    assert mv.start_measure("line")
    px, py, _ok = pixels(mv, np.array([a, b]))
    t0 = time.perf_counter()
    for k in range(2):
        mv._click(float(px[k]), float(py[k]))
    mv._finish()
    secs = time.perf_counter() - t0
    mv.stop_drawing()
    kind, xyz, draped = got[-1]
    v = mm.values("line", ch.to_world(xyz), None if draped is None else ch.to_world(draped))
    return v, secs, (0 if draped is None else len(draped))


def measure_area(ch, poly):
    """The surface of the model inside ``poly`` (world) as the 3D view's measure gets it (the worker of the free
    polygons, run here in this thread): (value, seconds, peak MB)."""
    from pyarchinit_photogrammetry.engine.orient import toward_cameras
    from pyarchinit_photogrammetry.gui import free_drawing_dialog as fdd

    rec = {"kind": "polygon", "xyz": poly.tolist(), "name": "m"}
    t0 = time.perf_counter()
    with Peak() as pk:
        model = fdd.model_in_chunk(fdd.ModelSource(ch), around=poly)
        m = fdd._measures(rec, model, toward_cameras(ch, poly))
        del model
        gc.collect()
    return (None if m is None else m.get("model_surface")), time.perf_counter() - t0, pk.peak


def vtk_check(mv, ch, world, out: dict, size=(1600, 1200)) -> None:
    """The scene shown by ``mv`` drawn by VTK off screen; our projection and line of sight read from that VTK camera
    (the methods of VtkCanvas on a real renderer, at pixel centres near the targets); our pick compared with what VTK
    drew at that pixel (its depth buffer) and, on a mesh, with VTK's cell picker on the geometry shown. VTK's
    hardware pickers (vtkWorldPointPicker, vtkPropPicker) crash off screen on this macOS and are not used (the
    program does not use them either)."""
    import pyvista as pv
    from vtkmodules.vtkRenderingCore import vtkCellPicker

    from pyarchinit_photogrammetry.gui.model_view import VtkCanvas

    sd = mv.sd
    pl = pv.Plotter(off_screen=True, window_size=size)
    t0 = time.perf_counter()
    mesh = sd.mesh_vertices is not None and len(sd.mesh_vertices)
    if mesh:
        poly = pv.PolyData(np.asarray(sd.mesh_vertices, np.float32),
                           faces=np.c_[np.full(len(sd.mesh_faces), 3), sd.mesh_faces].ravel())
    else:
        poly = pv.PolyData(np.asarray(sd.points, np.float32))
    pl.add_mesh(poly, color="#c8b496", point_size=2)
    pl.view_xy()  # a perspective camera (the depth buffer read back as distances along the view by pyvista)
    pl.camera.zoom(1.3)
    pl.show(auto_close=False)
    out["vtk_first_render_s"] = round(time.perf_counter() - t0, 3)
    zbuf = pl.get_image_depth(fill_value=np.nan)  # view-space depth, rows from the top

    class Stub:  # what VtkCanvas.projector and view_ray read: the plotter and the size of the widget
        plotter = pl

        def width(self):
            return size[0]

        def height(self):
            return size[1]

    stub = Stub()
    project = VtkCanvas.projector(stub)

    def view_ray(x, y):
        return VtkCanvas.view_ray(stub, x, y)

    cam = pl.camera
    eye, ahead = np.array(cam.GetPosition()), np.array(cam.GetDirectionOfProjection())
    shown = sd.to_display(np.asarray(world, np.float64) - sd.offset)
    px, py, _d, ok = project(shown)
    cell = vtkCellPicker()
    cell.SetTolerance(0.0)
    e_surf, e_depth, e_cell, t_ours, t_cell = [], [], [], [], []
    for k in np.nonzero(ok)[0]:
        col, row = int(px[k]), int(py[k])
        x, y = col + 0.5, row + 0.5  # the centre of the pixel VTK drew
        t1 = time.perf_counter()
        p = mv._surface_at(x, y, project, refine=True, ray=view_ray)
        t_ours.append(time.perf_counter() - t1)
        if p is None:
            e_surf.append(np.inf)
            e_depth.append(np.inf)
            continue
        w = ch.to_world(np.asarray(p, np.float64)[None])[0] - ORIGIN
        e_surf.append(abs(w[2] - f(w[0], w[1])))  # off the analytic terrain, vertically
        q = sd.to_display(ch.to_world(np.asarray(p, np.float64)[None]) - sd.offset)[0]
        z = zbuf[row, col]
        e_depth.append(abs(float((q - eye) @ ahead) - abs(float(z))) if np.isfinite(z) else np.inf)
        if mesh:
            t1 = time.perf_counter()
            hit = cell.Pick(x, size[1] - y, 0, pl.renderer)  # VTK counts rows from the bottom
            t_cell.append(time.perf_counter() - t1)
            if hit:
                c = np.array(cell.GetPickPosition(), np.float64)
                e_cell.append(float(np.linalg.norm(sd.from_display(c[None])[0] - sd.from_display(q[None])[0])))
            else:
                e_cell.append(np.inf)
    out.update(vtk_ours_ms=_ms(t_ours), vtk_ours_off_surface_mm=_stats(e_surf),
               vtk_ours_vs_zbuffer_mm=_stats(e_depth))
    if mesh:
        out.update(vtk_cellpicker_ms=_ms(t_cell), vtk_ours_vs_cellpicker_mm=_stats(e_cell))
    pl.close()


def _stats(errs) -> dict:
    e = np.asarray(errs, float) * 1000
    fin = e[np.isfinite(e)]
    return {"median": round(float(np.median(fin)), 3) if len(fin) else None,
            "max": round(float(fin.max()), 3) if len(fin) else None, "misses": int((~np.isfinite(e)).sum())}


def _ms(times) -> dict:
    t = np.asarray(times, float) * 1000
    return {"median": round(float(np.median(t)), 2), "max": round(float(t.max()), 2)} if len(t) else {}


def bench_model(app, work: Path, faces: float, pages: int, vtk: bool, n_targets: int) -> dict:
    out = {"case": "model", "faces_asked": int(faces)}
    doc, ch = make_chunk(work, f"model_{int(faces)}")
    path = ch.data_dir / "model.ply"
    t0 = time.perf_counter()
    out["faces"] = write_terrain(path, int(faces), pages)
    out["write_s"] = round(time.perf_counter() - t0, 1)
    out["file_mb"] = round(path.stat().st_size / 2 ** 20)
    ch._set_product("model", path)
    kind = "model"
    if pages:
        for k in range(pages):
            img = np.zeros((1024, 1024, 3), np.uint8)
            img[..., 0] = 60 + 40 * k
            img[::64, :] = 255
            import cv2

            cv2.imwrite(str(ch.data_dir / f"page_{k}.jpg"), img)
        ch._set_product("model_textured", ch.data_dir / "page_0.jpg")
        kind = "model_textured"
    doc.save()
    out["kind"] = kind
    rss0 = rss_mb()
    mv, shown, peak = view(app, ch, kind, vtk_scene=vtk)
    out.update(show_s=round(shown, 1), show_peak_mb=round(peak), shown_faces=int(len(mv.sd.mesh_faces)),
               rss_shown_mb=round(rss_mb() - rss0))
    world = targets(n_targets)
    # before the ray caster: what the points shown alone give (the level displayed)
    t_shown, e_shown = pick_errors(mv, ch, world, shown_only=True)
    out["shown_pick_ms"], out["shown_err_mm"] = _ms(t_shown), _stats(e_shown)
    t0 = time.perf_counter()
    with Peak() as pk:
        mv._caster()
        ui = []  # the interface thread's longest stall while the caster is built (a timer every 20 ms)
        last = [time.perf_counter()]

        def tick():
            now = time.perf_counter()
            ui.append(now - last[0])
            last[0] = now

        from PySide6.QtCore import QTimer

        timer = QTimer()
        timer.timeout.connect(tick)
        timer.start(20)
        wait_for(app, lambda view=mv: not view.caster_preparing)
        timer.stop()
    out.update(caster_s=round(time.perf_counter() - t0, 1), caster_peak_mb=round(pk.peak),
               rss_with_caster_mb=round(rss_mb() - rss0), ui_longest_stall_ms=round(max(ui, default=0) * 1000))
    t_full, e_full = pick_errors(mv, ch, world)
    out["pick_ms"], out["pick_err_mm"] = _ms(t_full), _stats(e_full)
    # a miss: a pixel beside the terrain
    project = mv.canvas.projector()
    t1 = time.perf_counter()
    miss = mv._surface_at(2.0, 2.0, project, refine=True)
    out["miss_ms"] = round((time.perf_counter() - t1) * 1000, 2)
    out["miss_is_none"] = miss is None
    a, b = np.array([15.0, 20.0, f(15.0, 20.0)]), np.array([80.0, 70.0, f(80.0, 70.0)])
    truth = surface_length(a, b)
    v, secs, n = measure_line(app, mv, ch, a + ORIGIN, b + ORIGIN)
    out.update(line_s=round(secs, 3), drape_points=n, length_3d=round(v["length_3d"], 4),
               length_surface=round(v.get("length_surface", float("nan")), 4), length_surface_true=round(truth, 4),
               length_surface_err_mm=round((v.get("length_surface", np.nan) - truth) * 1000, 2))
    poly = np.array([[30.0, 30.0], [60.0, 32.0], [62.0, 58.0], [33.0, 61.0]])
    poly = np.column_stack([poly, f(poly[:, 0], poly[:, 1])])
    truth = surface_area(poly)
    area, secs, peak = measure_area(ch, poly + ORIGIN)
    out.update(area_s=round(secs, 1), area_peak_mb=round(peak), area_surface=None if area is None else round(area, 4),
               area_surface_true=round(truth, 4),
               area_rel_err=None if area is None else float(f"{(area - truth) / truth:.2e}"))
    if vtk:
        vtk_check(mv, ch, world, out)
    mv.close()
    mv.deleteLater()
    app.processEvents()
    del mv
    gc.collect()
    shutil.rmtree(ch.data_dir.parent, ignore_errors=True)
    for p in work.glob(f"model_{int(faces)}.ppg*"):
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink()
    return out


def bench_cloud(app, work: Path, points: float, vtk: bool, n_targets: int) -> dict:
    out = {"case": "cloud", "points": int(points)}
    doc, ch = make_chunk(work, f"cloud_{int(points)}")
    path = ch.data_dir / "point_cloud.ply"
    t0 = time.perf_counter()
    write_cloud(path, int(points))
    out["write_s"] = round(time.perf_counter() - t0, 1)
    out["file_mb"] = round(path.stat().st_size / 2 ** 20)
    ch._set_product("point_cloud", path)
    doc.save()
    rss0 = rss_mb()
    mv, shown, peak = view(app, ch, "point_cloud", vtk_scene=vtk)
    out.update(show_s=round(shown, 1), show_peak_mb=round(peak), shown_points=int(len(mv.sd.points)),
               rss_shown_mb=round(rss_mb() - rss0))
    world = targets(n_targets)
    t_fast, e_fast = pick_errors(mv, ch, world, refine=False)
    out["shown_pick_ms"], out["shown_err_mm"] = _ms(t_fast), _stats(e_fast)
    t0 = time.perf_counter()
    with Peak() as pk:
        prep = getattr(mv, "_cloud_index", None)
        if prep is not None:
            prep()
            wait_for(app, lambda view=mv: not view.caster_preparing)
        t_ref, e_ref = pick_errors(mv, ch, world[:1], refine=True)  # the first refined pick (loads what it needs)
    out.update(first_refined_s=round(time.perf_counter() - t0, 1), first_refined_peak_mb=round(pk.peak))
    t_ref, e_ref = pick_errors(mv, ch, world, refine=True)
    out["pick_ms"], out["pick_err_mm"] = _ms(t_ref), _stats(e_ref)
    out["rss_after_mb"] = round(rss_mb() - rss0)
    if vtk:
        vtk_check(mv, ch, world, out)
    mv.close()
    mv.deleteLater()
    app.processEvents()
    del mv
    gc.collect()
    shutil.rmtree(ch.data_dir.parent, ignore_errors=True)
    for p in work.glob(f"cloud_{int(points)}.ppg*"):
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink()
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--work", type=Path, required=True, help="folder for the synthetic files (deleted per case)")
    ap.add_argument("--faces", type=float, nargs="*", default=[], help="model sizes in triangles")
    ap.add_argument("--pages", type=int, default=0, help="texture pages of the models (0: coloured model)")
    ap.add_argument("--points", type=float, nargs="*", default=[], help="cloud sizes in points")
    ap.add_argument("--targets", type=int, default=40, help="random places picked per case")
    ap.add_argument("--vtk", action="store_true", help="also draw with VTK off screen and compare its pickers")
    ap.add_argument("--min-free-gb", type=float, default=4.0, help="stop before a case when less memory is free")
    args = ap.parse_args(argv)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from pyarchinit_photogrammetry import _env

    _env.load_open3d_first()
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv)
    args.work.mkdir(parents=True, exist_ok=True)
    cases = [("model", s) for s in args.faces] + [("cloud", s) for s in args.points]
    for kind, size in cases:
        try:
            import psutil

            free = psutil.virtual_memory().available / 2 ** 30
            if free < args.min_free_gb:
                print(json.dumps({"case": kind, "size": size, "skipped": f"{free:.1f} GB free"}), flush=True)
                continue
        except ImportError:
            pass
        if kind == "model":
            out = bench_model(app, args.work, size, args.pages, args.vtk, args.targets)
        else:
            out = bench_cloud(app, args.work, size, args.vtk, args.targets)
        print(json.dumps(out), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
