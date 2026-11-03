"""Which call drains the reference count of ``None`` (Python 3.11, CI): a probe, not a test.

Run with the program installed: ``python tests/probe_none.py`` runs every case in a process of its
own (a case that drains the count to zero aborts the interpreter: the others still report) and
prints the drop of ``sys.getrefcount(None)`` across the call, measured on the live objects (a
collection before and after), for N calls. On Python 3.12 and later None is immortal and every
drop is 0."""
from __future__ import annotations

import gc
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")
os.environ.setdefault("PPG_NO_UPDATE_CHECK", "1")
os.environ.setdefault("PPG_NO_PLUGINS", "1")

N = 2000


def rc() -> int:
    gc.collect()
    return sys.getrefcount(None)


def _cases(rng):
    from PySide6.QtCore import QLineF, QPointF, QRectF
    from PySide6.QtGui import QPainterPath, QPolygonF
    from PySide6.QtWidgets import QGraphicsScene

    seg = rng.random((N, 2, 2)) * 1000
    xs, ys = seg[..., 0].tolist(), seg[..., 1].tolist()

    def path_floats():
        p = QPainterPath()
        for (c0, c1), (r0, r1) in zip(xs, ys):
            p.moveTo(c0, r0)
            p.lineTo(c1, r1)

    def path_floats_moveto_only():
        p = QPainterPath()
        for (c0, c1), (r0, r1) in zip(xs, ys):
            p.moveTo(c0, r0)

    def path_floats_lineto_only():
        p = QPainterPath()
        p.moveTo(0.0, 0.0)
        for (c0, c1), (r0, r1) in zip(xs, ys):
            p.lineTo(c1, r1)

    def path_points():
        p = QPainterPath()
        for (c0, c1), (r0, r1) in zip(xs, ys):
            p.moveTo(QPointF(c0, r0))
            p.lineTo(QPointF(c1, r1))

    def qpointf_only():
        pts = [QPointF(c0, r0) for (c0, c1), (r0, r1) in zip(xs, ys)]
        del pts

    def path_polygons():
        p = QPainterPath()
        for (c0, c1), (r0, r1) in zip(xs, ys):
            p.addPolygon(QPolygonF([QPointF(c0, r0), QPointF(c1, r1)]))

    def one_polygon():
        pts = []
        for (c0, c1), (r0, r1) in zip(xs, ys):
            pts.append(QPointF(c0, r0))
            pts.append(QPointF(c1, r1))
        p = QPainterPath()
        p.addPolygon(QPolygonF(pts))

    def scene_lines():
        s = QGraphicsScene()
        for (c0, c1), (r0, r1) in zip(xs, ys):
            s.addLine(QLineF(c0, r0, c1, r1))
        s.clear()

    def scene_items():
        s = QGraphicsScene()
        p = QPainterPath()
        p.moveTo(QPointF(0.0, 0.0))
        it = s.addPath(p)
        it.setData(2, True)
        it.setZValue(-0.5)
        for _ in range(N):
            s.items(QRectF(0, 0, 500, 500))
        s.clear()

    def delaunay():
        from scipy.spatial import Delaunay

        Delaunay(rng.random((20000, 2)))

    def rasterio_io():
        import pyarchinit_photogrammetry  # noqa: F401  (PROJ's data as the program sets it)
        import rasterio
        from rasterio.transform import from_origin

        path = Path(tempfile.gettempdir()) / "probe_none.tif"
        data = rng.random((1, 500, 500)).astype(np.float32)
        with rasterio.open(path, "w", driver="GTiff", height=500, width=500, count=1, dtype="float32",
                           crs="EPSG:32633", transform=from_origin(400000, 4500000, 0.05, 0.05)) as dst:
            dst.write(data)
        with rasterio.open(path) as src:
            for _ in range(N):
                src.index(400010.5, 4499990.5)
                src.xy(5, 5)

    def tin_fallback():
        os.environ["PPG_NATIVE"] = "0"
        from pyarchinit_photogrammetry.engine import tin as T

        xy = rng.random((5000, 2)) * 100
        tri = T.triangulate(xy)
        triangles = getattr(tri, "triangles", tri)
        z = np.sin(xy[:, 0] / 10) + np.cos(xy[:, 1] / 10)
        T.rasterize(xy, z, triangles, 0.0, 100.0, 0.2, 500, 500)
        T.contour_segments(xy, z, triangles, np.linspace(-2, 2, 9))

    return {
        "path moveTo+lineTo floats": path_floats,
        "path moveTo floats only": path_floats_moveto_only,
        "path lineTo floats only": path_floats_lineto_only,
        "path moveTo+lineTo QPointF": path_points,
        "QPointF only": qpointf_only,
        "path addPolygon per segment": path_polygons,
        "one addPolygon of 2N points": one_polygon,
        "scene addLine QLineF": scene_lines,
        "scene items(rect)": scene_items,
        "scipy Delaunay 20k": delaunay,
        "rasterio index/xy": rasterio_io,
        "engine.tin fallback 5k": tin_fallback,
    }


def main() -> int:
    if len(sys.argv) > 1:  # one case, in this process
        from PySide6.QtWidgets import QApplication

        QApplication.instance() or QApplication([sys.argv[0]])
        rng = np.random.default_rng(0)
        fn = _cases(rng)[sys.argv[1]]
        a = rc()
        fn()
        b = rc()
        print(f"{a - b:+d} (count before {a})", flush=True)
        return 0
    names = list(_cases(np.random.default_rng(0)))
    for name in names:
        r = subprocess.run([sys.executable, __file__, name], capture_output=True, text=True, timeout=600)
        out = (r.stdout.strip().splitlines() or [""])[-1]
        err = "" if r.returncode == 0 else f"  [exit {r.returncode}: {(r.stderr.strip().splitlines() or [''])[-1][:80]}]"
        print(f"{name:32s} N={N}: {out}{err}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
