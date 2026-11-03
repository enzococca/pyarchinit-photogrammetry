"""Which call drains the reference count of ``None`` (Python 3.11, CI): a probe, not a test.

Run with the program installed: ``python tests/probe_none.py``. Each case prints the drop of
``sys.getrefcount(None)`` across the call, measured on the live objects (a collection before
and after). On Python 3.12 and later None is immortal and every drop is 0."""
from __future__ import annotations

import gc
import os
import sys
import tempfile
from pathlib import Path

import numpy as np

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")
os.environ.setdefault("PPG_NO_UPDATE_CHECK", "1")
os.environ.setdefault("PPG_NO_PLUGINS", "1")


def rc() -> int:
    gc.collect()
    return sys.getrefcount(None)


def case(name, fn):
    a = rc()
    try:
        fn()
    except Exception as exc:  # the probe goes on
        print(f"{name:46s} error: {exc!r}"[:160], flush=True)
        return
    b = rc()
    print(f"{name:46s} {a - b:+d}", flush=True)


def main() -> int:
    from PySide6.QtCore import QPointF, QRectF
    from PySide6.QtGui import QPainterPath, QPolygonF
    from PySide6.QtWidgets import QApplication, QGraphicsScene

    app = QApplication.instance() or QApplication([sys.argv[0]])
    rng = np.random.default_rng(0)
    seg = rng.random((50000, 2, 2)) * 1000

    def path_floats():
        p = QPainterPath()
        for (c0, c1), (r0, r1) in zip(seg[..., 0].tolist(), seg[..., 1].tolist()):
            p.moveTo(c0, r0)
            p.lineTo(c1, r1)

    def path_points():
        p = QPainterPath()
        for (c0, r0), (c1, r1) in seg.tolist():
            p.moveTo(QPointF(c0, r0))
            p.lineTo(QPointF(c1, r1))

    def polygon_lines():
        s = QGraphicsScene()
        for k in range(1000):
            pts = [QPointF(float(x), float(y)) for x, y in rng.random((20, 2)) * 1000]
            p = QPainterPath()
            p.addPolygon(QPolygonF(pts))
            it = s.addPath(p)
            it.setData(0, "layer")
            it.setData(1, k)
            it.setToolTip("x")
        s.clear()

    def scene_items():
        s = QGraphicsScene()
        p = QPainterPath()
        for (c0, c1), (r0, r1) in zip(seg[..., 0].tolist(), seg[..., 1].tolist()):
            p.moveTo(c0, r0)
            p.lineTo(c1, r1)
        it = s.addPath(p)
        it.setData(2, True)
        it.setZValue(-0.5)
        for _ in range(100):
            s.items(QRectF(0, 0, 500, 500))
        s.clear()

    case("QPainterPath moveTo/lineTo floats x50k", path_floats)
    case("QPainterPath moveTo/lineTo QPointF x50k", path_points)
    case("1000 polygon path items + setData/tooltip", polygon_lines)
    case("scene addPath 50k + items(rect) x100", scene_items)

    def delaunay():
        from scipy.spatial import Delaunay

        Delaunay(rng.random((50000, 2)))

    case("scipy Delaunay 50k", delaunay)

    def numpy_ops():
        a = rng.random((2000, 2000)).astype(np.float32)
        for _ in range(20):
            np.unique(np.floor(a * 100).astype(np.int64).ravel())
            np.nanmedian(a, axis=0)
            np.savez(Path(tempfile.gettempdir()) / "probe_none.npz", a=a)
            np.load(Path(tempfile.gettempdir()) / "probe_none.npz")["a"]

    case("numpy unique/nanmedian/savez/load x20", numpy_ops)

    def rasterio_io():
        import pyarchinit_photogrammetry  # noqa: F401  (PROJ's data as the program sets it)
        import rasterio
        from rasterio.transform import from_origin

        path = Path(tempfile.gettempdir()) / "probe_none.tif"
        data = rng.random((1, 1500, 1500)).astype(np.float32)
        with rasterio.open(path, "w", driver="GTiff", height=1500, width=1500, count=1, dtype="float32",
                           crs="EPSG:32633", transform=from_origin(400000, 4500000, 0.05, 0.05)) as dst:
            dst.write(data)
        with rasterio.open(path) as src:
            for _ in range(200):
                src.read(1, window=rasterio.windows.Window(10, 10, 200, 200))
                src.index(400010.5, 4499990.5)
                list(src.sample([(400010.5, 4499990.5)] * 50))
                src.xy(5, 5)

    case("rasterio write/read/index/sample/xy", rasterio_io)

    def shapely_ops():
        from shapely import unary_union
        from shapely.geometry import Point
        from shapely.strtree import STRtree

        polys = [Point(x, y).buffer(0.6) for x, y in rng.random((5000, 2)) * 100]
        tree = STRtree(polys)
        for p in polys[:1000]:
            tree.query(p)
        unary_union(polys)

    case("shapely buffer/STRtree/unary_union", shapely_ops)

    def cv2_ops():
        import cv2

        img = (rng.random((1500, 1500, 3)) * 255).astype(np.uint8)
        path = str(Path(tempfile.gettempdir()) / "probe_none.jpg")
        for _ in range(50):
            cv2.imwrite(path, img)
            cv2.imread(path)
            cv2.resize(img, (300, 300), interpolation=cv2.INTER_AREA)
            cv2.GaussianBlur(img, (5, 5), 1.0)

    case("cv2 imwrite/imread/resize/blur x50", cv2_ops)

    def tin_fallback():
        os.environ["PPG_NATIVE"] = "0"
        from pyarchinit_photogrammetry.engine import tin as T

        xy = rng.random((20000, 2)) * 100
        tri = T.triangulate(xy)
        triangles = getattr(tri, "triangles", tri)
        z = np.sin(xy[:, 0] / 10) + np.cos(xy[:, 1] / 10)
        T.rasterize(xy, z, triangles, 0.0, 100.0, 0.1, 1000, 1000)
        T.contour_segments(xy, z, triangles, np.linspace(-2, 2, 9))

    case("engine.tin fallback triangulate/rasterize/...", tin_fallback)

    def view_segments():
        from pyarchinit_photogrammetry.gui.image_views import RasterView

        v = RasterView()
        v.resize(800, 600)
        v.show_frame((0.0, 0.0, 1000.0, 1000.0), 1.0)
        v.add_segments(seg[:, :, :], layer="TIN")
        v.add_segments(seg[:, :, :], layer="TIN2")
        v.remove_layer("TIN")

    case("RasterView.add_segments 50k x2 + remove", view_segments)
    return 0


if __name__ == "__main__":
    sys.exit(main())
