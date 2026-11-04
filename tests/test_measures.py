"""Measures: surface area in 3D, slope and aspect, height above the ground."""
from __future__ import annotations

import numpy as np
import pytest

from pyarchinit_photogrammetry.archaeo import measures
from pyarchinit_photogrammetry.engine.dem import write_geotiff


def _plane_dem(tmp_path, gx: float, gy: float, res: float = 0.05, size: int = 200):
    """A DEM of the plane z = 100 + gx * x + gy * y (x east, y north) from (0, 0) to size * res."""
    x = (np.arange(size) + 0.5) * res
    y = (size - np.arange(size) - 0.5) * res  # row 0 at the top (north)
    X, Y = np.meshgrid(x, y)
    z = (100 + gx * X + gy * Y).astype(np.float32)
    return write_geotiff(tmp_path / "dem.tif", z, (0.0, size * res), res, "EPSG:3004", nodata=-32767.0)


def test_surface_area_slope_and_aspect(tmp_path):
    import trimesh
    from shapely.geometry import box

    slope = np.tan(np.radians(30))
    dem = _plane_dem(tmp_path, slope, 0.0)  # rising to the east: facing west
    poly = box(2.0, 2.0, 7.0, 6.0)  # 5 x 4 m
    out = measures.surface_area(dem, poly)
    assert out["plan"] == pytest.approx(20.0, rel=1e-3)
    assert out["surface"] == pytest.approx(20.0 / np.cos(np.radians(30)), rel=2e-3)
    assert out["mean_slope"] == pytest.approx(30.0, abs=0.1)
    s, a = measures.slope_aspect_at(dem, 5.0, 5.0)
    assert s == pytest.approx(30.0, abs=0.01) and a == pytest.approx(270.0, abs=0.01)
    dem2 = _plane_dem(tmp_path, 0.0, 0.2)  # rising to the north: facing south
    s, a = measures.slope_aspect_at(dem2, 5.0, 5.0)
    assert s == pytest.approx(np.degrees(np.arctan(0.2)), abs=0.01) and a == pytest.approx(180.0, abs=0.01)
    assert measures.slope_aspect_at(dem2, -5.0, 5.0) is None  # outside
    import rasterio

    out_path = measures.aspect_raster(dem2, tmp_path / "aspect.tif", crs="EPSG:3004")
    with rasterio.open(out_path) as src:
        a = src.read(1)
    assert np.median(a[5:-5, 5:-5]) == pytest.approx(180.0, abs=0.01)
    # the faces of a model: the same plane, inclined 30 degrees
    v = np.array([[0, 0, 100], [10, 0, 100 + 10 * slope], [10, 10, 100 + 10 * slope], [0, 10, 100]], float)
    mesh = trimesh.Trimesh(v, [[0, 1, 2], [0, 2, 3]], process=False)
    mesh = mesh.subdivide().subdivide().subdivide().subdivide()
    out = measures.surface_area(dem, poly, model=mesh)
    assert out["model_plan"] == pytest.approx(20.0, rel=0.05)
    assert out["model_surface"] == pytest.approx(out["model_plan"] / np.cos(np.radians(30)), rel=1e-6)


def test_height_above_ground():
    rng = np.random.default_rng(0)
    n = 20000
    xy = rng.uniform(0, 10, (n, 2))
    ground = 100 + 0.1 * xy[:, 0]
    veg = rng.random(n) < 0.2
    z = ground + np.where(veg, 1.5, 0.0) + rng.normal(0, 0.003, n)
    classes = np.where(veg, 3, 2)
    hag = measures.height_above_ground(np.column_stack([xy, z]), classes)
    assert np.median(hag[veg]) == pytest.approx(1.5, abs=0.02)
    assert np.median(np.abs(hag[~veg])) < 0.01
    with pytest.raises(ValueError):
        measures.height_above_ground(np.column_stack([xy, z]), np.full(n, 1))


def test_interface(processed, tmp_path, monkeypatch):
    import os
    import shutil

    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    os.environ.setdefault("PPG_RENDERER", "software")
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    import pyarchinit_photogrammetry as ppg

    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    from pyarchinit_photogrammetry.ai.classify import classify_chunk
    from pyarchinit_photogrammetry.gui import main_window as mw

    doc, _ch, _ = processed
    d = tmp_path / "copia"
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")
    doc = ppg.Document.open(d / doc.path.name)
    w = mw.MainWindow(doc, autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    shown = []
    monkeypatch.setattr(w, "_info", lambda title, text: shown.append(text))
    x0, y0 = 2480000.0, 4500000.0
    w._area_drawn([(x0 + 1.2, y0 + 1.0), (x0 + 2.8, y0 + 1.0), (x0 + 2.8, y0 + 2.0), (x0 + 1.2, y0 + 2.0)])
    w.runner.wait()
    app.processEvents()
    import re

    plan = float(re.search(r"Area in pianta ([\d,]+) m2", shown[0]).group(1).replace(",", "."))
    assert plan == pytest.approx(1.6, abs=0.02) and "Dal modello" in shown[0]
    assert "Misure" in w.line_layers
    w._show_raster("dem")
    w._show_coords(x0 + 0.5, y0 + 0.5, 100.0)
    assert "pend." in w.coord_label.text()
    for kind, word in (("slope", "pendenza"), ("aspect", "esposizione")):
        w.terrain_raster(kind)
        w.runner.wait()
        app.processEvents()
        assert w.ortho_view.kind == kind and w.chunk.product_path(kind).exists()
        w._show_coords(x0 + 0.5, y0 + 0.5, 20.0)
        assert word in w.coord_label.text()
    # heights above the ground in the 3D view, once the ground is classified
    classify_chunk(w.chunk)
    w._show_model("point_cloud")
    w.model_view.set_color_mode("hag")
    assert not w.model_view.sd.hag_missing and len(w.model_view.sd.colors)
    w.setWindowModified(False)
    w.close()
    app.processEvents()


def _tilted_square(deg=30.0):
    c, s = np.cos(np.radians(deg)), np.sin(np.radians(deg))
    return np.array([[0, 0, 0], [c, 0, s], [c, 1, s], [0, 1, 0]], float)


def test_lengths():
    line = np.array([[0, 0, 0], [3, 4, 0], [3, 4, 12]], float)
    assert measures.length_3d(line) == pytest.approx(17.0, abs=1e-12)
    line = np.array([[0, 0, 0], [3, 4, 12]], float)
    assert measures.length_3d(line) == pytest.approx(13.0, abs=1e-12)
    assert measures.length_plan(line) == pytest.approx(5.0, abs=1e-12)
    assert measures.length_3d(line, closed=True) == pytest.approx(26.0, abs=1e-12)


def test_area_of_a_tilted_square():
    sq = _tilted_square(30.0)
    assert measures.area_3d(sq) == pytest.approx(1.0, abs=1e-9)
    assert measures.area_plan(sq) == pytest.approx(np.cos(np.radians(30)), abs=1e-9)
    slope, aspect = measures.slope_aspect_of(sq)
    assert slope == pytest.approx(30.0, abs=1e-9) and aspect == pytest.approx(270.0, abs=1e-9)
    flat = sq * [1, 1, 0]
    assert measures.slope_aspect_of(flat)[1] == -1.0


def _grid_mesh(n, m=None):
    """The faces of a regular n x m grid of vertices (row-major, m per row) in two-triangle cells."""
    m = n if m is None else m
    i, j = np.meshgrid(np.arange(n - 1), np.arange(m - 1), indexing="ij")
    k = (i * m + j).ravel()
    return np.concatenate([np.column_stack([k, k + 1, k + m]), np.column_stack([k + 1, k + m + 1, k + m])])


def _tilted_mesh(deg, n=6):
    import trimesh

    t = np.tan(np.radians(deg))
    xs, ys = np.meshgrid(np.linspace(0, 5, n), np.linspace(0, 5, n))
    v = np.column_stack([xs.ravel(), ys.ravel(), t * xs.ravel()])
    return trimesh.Trimesh(v, _grid_mesh(n), process=False)


def test_model_area_clipped_by_the_vertical_projection():
    t = np.tan(np.radians(20))
    pts = np.array([[0.7, 0.6], [3.9, 1.3], [2.2, 3.7]])
    poly = np.column_stack([pts, t * pts[:, 0]])
    out = measures.model_area_inside(_tilted_mesh(20), poly)
    assert out["projection"] == "plan" and out["faces"] > 2
    assert out["surface"] == pytest.approx(measures.area_3d(poly), abs=1e-9)
    assert out["plan"] == pytest.approx(measures.area_plan(poly), abs=1e-9)


def test_wall_polygon_uses_its_plane():
    import trimesh

    n = 5
    xs, zs = np.meshgrid(np.linspace(0, 4, n), np.linspace(0, 4, n))
    v = np.column_stack([xs.ravel(), np.zeros(n * n), zs.ravel()])
    wall = trimesh.Trimesh(v, _grid_mesh(n), process=False)
    poly = np.array([[0.6, 0, 0.5], [3.3, 0, 1.1], [2.4, 0, 3.2], [1.0, 0, 2.7]])
    out = measures.model_area_inside(wall, poly)
    assert out["projection"] == "plane"
    assert out["surface"] == pytest.approx(measures.area_3d(poly), abs=1e-9)


def test_feature_measures_keys():
    line = {"kind": "line", "xyz": [[0, 0, 0], [3, 4, 12]]}
    assert set(measures.feature_measures(line)) == {"length_3d", "length_plan", "dz"}
    assert measures.feature_measures(line)["dz"] == pytest.approx(12.0)
    sq = _tilted_square(20.0)
    poly = {"kind": "polygon", "xyz": sq.tolist()}
    keys = {"perimeter_3d", "area_3d", "area_plan", "slope", "aspect"}
    assert set(measures.feature_measures(poly)) == keys
    assert set(measures.feature_measures(poly, model=_tilted_mesh(20))) == keys | {"model_surface"}
    assert measures.feature_measures({"kind": "point", "xyz": [[1, 2, 3]]}) == {"z": 3.0}


def test_a_closed_object_is_counted_once():
    import trimesh

    slab = trimesh.creation.box(extents=(4.0, 0.5, 3.0))
    front = np.array([[-1, 0.25, -1], [1, 0.25, -1], [1, 0.25, 1], [-1, 0.25, 1]], float)
    out = measures.model_area_inside(slab, front)
    assert out["projection"] == "plane" and out["surface"] == pytest.approx(4.0, abs=1e-6)
    assert out["coverage"] == pytest.approx(1.0, abs=1e-6)
    back = front * [1, -1, 1]
    assert measures.model_area_inside(slab, back)["surface"] == pytest.approx(4.0, abs=1e-6)
    box = trimesh.creation.box(extents=(2.0, 2.0, 2.0))
    top = np.array([[-0.5, -0.5, 1], [0.5, -0.5, 1], [0.5, 0.5, 1], [-0.5, 0.5, 1]], float)
    out = measures.model_area_inside(box, top)
    assert out["projection"] == "plan" and out["surface"] == pytest.approx(1.0, abs=1e-6)


def test_large_coordinates_keep_their_precision():
    sq = np.array([[0, 0, 0], [0.1, 0, 0], [0.1, 0.1, 0], [0, 0.1, 0]], float) + [500000.0, 4500000.0, 0]
    assert measures.area_plan(sq) == pytest.approx(0.01, abs=1e-9)
    import trimesh

    m = trimesh.Trimesh(np.array([[-1, -1, 0], [2, -1, 0], [2, 2, 0], [-1, 2, 0]], float) + [500000.0, 4500000.0, 0],
                        [[0, 1, 2], [0, 2, 3]], process=False)
    assert measures.model_area_inside(m, sq)["surface"] == pytest.approx(0.01, abs=1e-9)


def test_bowtie_and_degenerate_polygons():
    bow = np.array([[0, 0, 0], [1, 1, 0], [1, 0, 0], [0, 1, 0]], float)
    assert measures.area_plan(bow) == pytest.approx(0.5, abs=1e-9)
    assert measures.area_3d(bow) == pytest.approx(0.5, abs=1e-9)
    with pytest.raises(ValueError):
        measures.area_3d(bow[:2])
    with pytest.raises(ValueError):
        measures.area_plan(bow[:2])
    with pytest.raises(ValueError):
        measures.slope_aspect_of(bow[:2])
    assert measures.model_area_inside(_tilted_mesh(0), bow + [1, 1, 0])["coverage"] == pytest.approx(1.0)
    out = measures.model_area_inside(_tilted_mesh(0), np.array([[4, 1, 0], [6, 1, 0], [6, 2, 0], [4, 2, 0]], float))
    assert out["coverage"] == pytest.approx(0.5, abs=1e-9)


def _heightfield(xs, ys, height):
    """A grid mesh over ``xs`` x ``ys`` (sorted) with z = height(x, y)."""
    import trimesh

    X, Y = np.meshgrid(xs, ys, indexing="ij")
    v = np.column_stack([X.ravel(), Y.ravel(), height(X, Y).ravel()])
    return trimesh.Trimesh(v, _grid_mesh(len(xs), len(ys)), process=False)


def _battered_tower(radius=4.0, batter_deg=8.0, height=6.0, segments=720, rings=25):
    """A closed tower: a cone frustum leaning in by ``batter_deg``, with its two caps."""
    import trimesh

    t = np.tan(np.radians(batter_deg))
    zs = np.linspace(0.0, height, rings)
    th = np.linspace(0.0, 2 * np.pi, segments, endpoint=False)
    Z, T = np.meshgrid(zs, th, indexing="ij")
    R = radius - t * Z
    v = np.column_stack([(R * np.cos(T)).ravel(), (R * np.sin(T)).ravel(), Z.ravel()])
    k = np.arange(segments)
    faces = []
    for r in range(rings - 1):
        a, b = r * segments + k, r * segments + (k + 1) % segments
        faces += [np.column_stack([a, b, a + segments]), np.column_stack([b, b + segments, a + segments])]
    centres = np.array([[0, 0, 0], [0, 0, height]], float)
    c0, c1 = len(v), len(v) + 1
    top = (rings - 1) * segments
    faces += [np.column_stack([np.full(segments, c0), (k + 1) % segments, k]),
              np.column_stack([np.full(segments, c1), top + k, top + (k + 1) % segments])]
    return trimesh.Trimesh(np.vstack([v, centres]), np.concatenate(faces), process=False)


@pytest.mark.parametrize("half", [0.25, 0.5])
def test_a_curved_wall_drawn_by_its_four_corners(half):
    radius, batter, z0, z1 = 4.0, np.radians(8.0), 1.0, 3.5
    tower = _battered_tower(radius, 8.0)

    def corner(theta, z):
        r = radius - np.tan(batter) * z
        return [r * np.cos(theta), r * np.sin(theta), z]

    poly = np.array([corner(-half, z0), corner(half, z0), corner(half, z1), corner(-half, z1)])
    # the cone between the two angles and the two heights, measured along its slant
    true = 2 * half * (radius * (z1 - z0) - np.tan(batter) * (z1 ** 2 - z0 ** 2) / 2) / np.cos(batter)
    out = measures.model_area_inside(tower, poly)
    assert out["projection"] == "plane"
    assert out["surface"] == pytest.approx(true, rel=0.01)
    # seen from the cameras outside: the same; seen from inside the tower, the inner side of the far wall
    assert measures.model_area_inside(tower, poly, toward=[20.0, 0, 2])["surface"] == pytest.approx(out["surface"])
    assert measures.model_area_inside(tower, poly, toward=[0.0, 0, 2])["surface"] != pytest.approx(out["surface"])


def test_a_block_inside_an_outline_drawn_on_the_ground():
    eps = 1e-3  # the block's sides: nearly vertical faces
    xs = np.unique(np.concatenate([np.linspace(-2, 2, 41), [-0.5 - eps, 0.5 + eps]]))
    mesh = _heightfield(xs, xs, lambda x, y: np.where((np.abs(x) <= 0.5) & (np.abs(y) <= 0.5), 0.15, 0.0))
    poly = np.array([[-1.5, -1.5, 0], [1.5, -1.5, 0], [1.5, 1.5, 0], [-1.5, 1.5, 0]], float)
    out = measures.model_area_inside(mesh, poly)
    assert out["projection"] == "plan" and out["coverage"] == pytest.approx(1.0, abs=1e-9)
    assert out["surface"] == pytest.approx(9.0 + 4 * 0.15, rel=0.01)


def test_a_step_crossing_the_outline():
    eps, h = 1e-3, 0.15
    xs = np.unique(np.concatenate([np.linspace(-2, 2, 41), [eps]]))
    mesh = _heightfield(xs, xs, lambda x, y: np.where(x >= eps, h, 0.0))
    poly = np.array([[-1.5, -1.5, 0], [1.5, -1.5, h], [1.5, 1.5, h], [-1.5, 1.5, 0]], float)
    out = measures.model_area_inside(mesh, poly)
    assert out["projection"] == "plan"
    assert out["surface"] == pytest.approx(9.0 + 3 * h, rel=0.01)


def test_feature_measures_pass_the_viewer_on():
    tower = _battered_tower()
    r0, r1 = 4.0 - np.tan(np.radians(8)) * 1.0, 4.0 - np.tan(np.radians(8)) * 3.5
    poly = [[r0 * np.cos(-0.3), r0 * np.sin(-0.3), 1.0], [r0 * np.cos(0.3), r0 * np.sin(0.3), 1.0],
            [r1 * np.cos(0.3), r1 * np.sin(0.3), 3.5], [r1 * np.cos(-0.3), r1 * np.sin(-0.3), 3.5]]
    rec = {"kind": "polygon", "xyz": poly}
    for toward in (None, [20.0, 0, 2], [0.0, 0, 2]):
        expected = measures.model_area_inside(tower, poly, toward=toward)["surface"]
        assert measures.feature_measures(rec, model=tower, toward=toward)["model_surface"] == pytest.approx(expected)


def test_degenerate_lines_and_collinear_polygons():
    collinear = np.array([[0, 0, 0], [1, 1, 1], [2, 2, 2]], float)
    with pytest.raises(ValueError):
        measures.slope_aspect_of(collinear)
    with pytest.raises(ValueError):
        measures.area_3d(collinear)
    with pytest.raises(ValueError):
        measures.feature_measures({"kind": "line", "xyz": []})


def test_model_area_is_fast_on_a_large_mesh():
    import time

    n = 317  # 199,712 faces
    xs = np.linspace(0, 100, n)
    mesh = _heightfield(xs, xs, lambda x, y: 0.5 * np.sin(x / 7) * np.cos(y / 5))
    assert len(mesh.faces) >= 199_000
    side = 100 * np.sqrt(0.1)
    xy = np.array([[20, 20], [20 + side, 20], [20 + side, 20 + side], [20, 20 + side]], float)
    poly = np.column_stack([xy, 0.5 * np.sin(xy[:, 0] / 7) * np.cos(xy[:, 1] / 5)])
    start = time.perf_counter()
    out = measures.model_area_inside(mesh, poly)
    assert time.perf_counter() - start < 2.0
    assert out["coverage"] == pytest.approx(1.0, abs=1e-9) and out["surface"] > out["plan"]
