"""Several chunks seen together: the points of a chunk placed in the system of another, their distances to its surface
(the overlap after *Allinea chunk*), and the 3D and 2D views that show them before and after the alignment."""
from __future__ import annotations

import os

import numpy as np
import pytest

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.core import geometry
from pyarchinit_photogrammetry.core.pointcloud import PointCloud
from pyarchinit_photogrammetry.engine import chunks as chunks_mod

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PPG_RENDERER", "software")

MARKS = np.array([[0.2, 0.2], [3.8, 0.4], [3.6, 2.7], [0.5, 2.8]])  # four markers on the trench


def surface(x, y):
    """A trench floor with a bank: shape enough for a normal and a distance, in metres."""
    return 0.15 * np.sin(x * 1.3) + 0.1 * np.cos(y * 1.7) - 0.3 * (x > 2.0)


def _cloud(n, seed, x0=0.0, x1=4.0):
    rng = np.random.default_rng(seed)
    x, y = rng.uniform(x0, x1, n), rng.uniform(0.0, 3.0, n)
    xyz = np.column_stack([x, y, surface(x, y)])
    rgb = np.column_stack([(x * 60) % 256, (y * 80) % 256, np.full(n, 120)]).astype(np.uint8)
    return xyz, rgb


def make_project(path, shift=(0.0, 0.0, 0.03), n=30_000):
    """Three chunks of the same trench: A the reference; B an aligned copy (the same surface, other points); C the
    same as B but shifted by ``shift`` through its transform (a chunk not aligned yet). Markers M1-M4 in each, at the
    true positions of their internal frame."""
    doc = ppg.Document()
    doc.save(path)
    for label, seed, move in (("A", 1, None), ("B", 2, None), ("C", 3, shift)):
        ch = doc.add_chunk(label)
        xyz, rgb = _cloud(n, seed)
        ch.point_cloud = PointCloud(xyz, rgb)
        ch.meta["metric"] = True  # a local frame in metres
        for i, (x, y) in enumerate(MARKS):
            ch.add_marker(f"M{i + 1}", position=np.array([x, y, surface(x, y)]))
        if move is not None:
            T = geometry.identity()
            T[:3, 3] = move
            ch.transform = T
    doc.chunk = doc.chunks[0]
    doc.save()
    return doc


@pytest.fixture
def three(tmp_path):
    return make_project(tmp_path / "tre.ppg")


def test_preview_points_are_a_stride_of_the_file(three):
    a = three.chunks[0]
    xyz, rgb, what = chunks_mod.preview_points(a, 1000)
    assert what == "point_cloud" and 1000 >= len(xyz) >= 900 and rgb.shape == (len(xyz), 3)
    whole = a.point_cloud.xyz
    np.testing.assert_allclose(xyz[1], whole[-(-len(whole) // 1000)])  # every so many points of the file
    empty = three.add_chunk("vuoto")
    with pytest.raises(ValueError):
        chunks_mod.preview_points(empty)


def test_distances_near_zero_for_an_aligned_copy_and_the_shift_for_a_shifted_one(three):
    a, b, c = three.chunks
    surf = chunks_mod.DistanceSurface(a)
    rb = chunks_mod.cloud_distances(a, b, surface=surf)
    rc = chunks_mod.cloud_distances(a, c, surface=surf)
    assert rb["placed"] and rb["what"] == "point_cloud"
    assert abs(rb["median"]) < 0.002 and rb["nmad"] < 0.004 and rb["overlap"] > 0.9
    assert rc["median"] == pytest.approx(0.03, abs=0.003)  # up: the normals turn upwards
    assert rc["overlap"] < 0.3  # 3 cm is more than three spacings of 30 000 points on 12 m2
    # by key or label through the project, the default threshold three spacings
    r = chunks_mod.cloud_distances("A", "C", doc=three, threshold=0.05)
    assert r["threshold"] == 0.05 and r["overlap"] > 0.9


def test_a_point_beyond_the_edge_is_far(tmp_path):
    doc = ppg.Document()
    doc.save(tmp_path / "bordo.ppg")
    a, b = doc.add_chunk("A"), doc.add_chunk("B")
    a.point_cloud = PointCloud(_cloud(20_000, 1, 0.0, 2.0)[0])
    b.point_cloud = PointCloud(_cloud(20_000, 2, 3.0, 4.0)[0])  # the same surface, a metre beyond A
    r = chunks_mod.cloud_distances(a, b)
    assert r["covered"] == 0.0 and r["overlap"] == 0.0 and r["median"] is None


def test_chunks_in_other_systems(tmp_path):
    doc = ppg.Document()
    doc.save(tmp_path / "sistemi.ppg")
    a, b, c = doc.add_chunk("A"), doc.add_chunk("B"), doc.add_chunk("C")
    for ch in (a, b, c):
        ch.point_cloud = PointCloud(_cloud(5000, 1)[0])
    T = geometry.identity()
    T[:3, 3] = (300000.0, 4600000.0, 50.0)
    a.transform, a.crs = T.copy(), "EPSG:32633"
    # B: the same place in the zone next door (EPSG:32634): converted with PROJ
    from pyarchinit_photogrammetry.core import crs as crs_mod

    there = crs_mod.transform_points(np.array([[300000.0, 4600000.0, 50.0]]), "EPSG:32633", "EPSG:32634")[0]
    T2 = geometry.identity()
    T2[:3, 3] = there
    b.transform, b.crs = T2, "EPSG:32634"
    world, placed = chunks_mod.shown_in(b, np.zeros((1, 3)), a.crs)
    assert placed and np.allclose(world[0], T[:3, 3], atol=0.01)
    r = chunks_mod.cloud_distances(a, b)
    assert r["placed"] and abs(r["median"]) < 0.02  # a few mm of the conversion over 4 m
    world, placed = chunks_mod.shown_in(c, np.zeros((1, 3)), a.crs)  # local frame: in its own
    assert not placed and np.allclose(world, 0.0)


def test_align_chunks_check_reports_overlap_and_distance(three):
    report = three.align_chunks("A", ["C"], method="markers", check=True)
    d = report["chunks"][0]["distances"]
    assert d["label"] == "C" and abs(d["median"]) < 0.002 and d["overlap"] > 0.9
    report = three.align_chunks("A", ["B"], method="markers")  # without check: nothing measured
    assert "distances" not in report["chunks"][0]


def test_cli_chunk_distances_and_align_check(three):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    r = CliRunner().invoke(app, ["chunk-distances", str(three.path), "B", "C", "--reference", "A"])
    assert r.exit_code == 0, r.output
    assert "B -> A" in r.output and "C -> A" in r.output and "median" in r.output
    r = CliRunner().invoke(app, ["chunk-distances", str(three.path), "C", "-r", "A", "--json"])
    import json

    assert json.loads(r.output)[0]["median"] == pytest.approx(0.03, abs=0.003)
    r = CliRunner().invoke(app, ["align-chunks", str(three.path), "C", "-r", "A", "--check"])
    assert r.exit_code == 0, r.output
    assert "overlap" in r.output and "median distance" in r.output


# ---------------------------------------------------------------- 3D view
@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def model_view(qapp):
    from pyarchinit_photogrammetry.gui.model_view import ModelView

    v = ModelView()
    v.resize(900, 600)
    v.show()
    qapp.processEvents()
    yield v
    v.close()


def _layer(view, label):
    return next(ly for ly in view.base_sd.others if ly.label == label)


def test_3d_view_shows_the_ticked_chunks_where_they_are(three, model_view, qapp):
    from pyarchinit_photogrammetry.gui import chunk_views

    a, b, c = three.chunks
    v = model_view
    v.set_chunk(a, "point_cloud")
    assert v.base_sd.others == [] and not v.others_row.isVisibleTo(v) and not v.chunks_legend.isVisibleTo(v)
    assert chunk_views.set_shown_3d(three, b, True) and chunk_views.set_shown_3d(three, c, True)
    assert not chunk_views.set_shown_3d(three, c, True)  # already shown
    assert three.meta["view"]["chunks_3d"] == [b.key, c.key]
    v.refresh()
    assert [ly.label for ly in v.base_sd.others] == ["B", "C"]
    assert v.others_row.isVisibleTo(v) and v.chunks_legend.isVisibleTo(v)
    lb, lc = _layer(v, "B"), _layer(v, "C")
    assert lb.placed and lb.what == "point_cloud" and len(lb.points) == 30_000
    # C is moved 3 cm up by its transform: its points drawn 3 cm above the same surface
    xyz = lc.points.astype(float) + v.base_sd.offset
    assert np.median(xyz[:, 2] - surface(xyz[:, 0], xyz[:, 1])) == pytest.approx(0.03, abs=1e-4)
    # one colour per chunk (the active one too), legend rows with the names
    assert (lb.colors == chunk_views.rgb_of(chunk_views.chunk_colour(three, b))).all()
    assert (v.base_sd.colors == chunk_views.rgb_of(chunk_views.chunk_colour(three, a))).all()
    rows = [v.chunks_legend.item(i).text() for i in range(v.chunks_legend.count())]
    assert rows == ["A (attivo)", "B", "C"]
    v.set_color_mode("class")  # the active chunk in its chunk colour: no legend of classes beside it
    assert not v.legend.isVisibleTo(v) and v.chunks_legend.isVisibleTo(v)
    v.set_color_mode("rgb")
    # the image: the three colours are on the screen
    img = v.canvas.grab_array()
    for ch in (a, b, c):
        rgb = chunk_views.rgb_of(chunk_views.chunk_colour(three, ch)).astype(int)
        assert (np.abs(img.astype(int) - rgb).sum(axis=2) < 30).sum() > 200, ch.label


def test_3d_distance_colours_and_alignment(three, model_view, qapp):
    from pyarchinit_photogrammetry.gui import chunk_views

    a, b, c = three.chunks
    for ch in (b, c):
        chunk_views.set_shown_3d(three, ch, True)
    v = model_view
    v.set_chunk(a, "point_cloud")
    changed = []
    v.others_changed.connect(lambda: changed.append(1))
    v.others_combo.setCurrentIndex(v.others_combo.findData("distance"))
    assert three.meta["view"]["colour"] == "distance" and changed
    lb, lc = _layer(v, "B"), _layer(v, "C")
    assert abs(lb.stats["median"]) < 0.002 and lc.stats["median"] == pytest.approx(0.03, abs=0.003)
    rows = [v.chunks_legend.item(i).text() for i in range(v.chunks_legend.count())]
    import re

    assert any(re.match(r"C: mediana \+(29|30)(,\d)? mm, NMAD [\d,]+ mm, entro [\d,]+ mm: \d+ %", r) for r in rows), rows
    assert any("Fuori dal chunk attivo" in r for r in rows)
    # C is red (above), B is white (on the surface)
    assert np.median(lc.colors[:, 0].astype(int) - lc.colors[:, 2].astype(int)) > 60
    assert np.median(np.abs(lb.colors.astype(int) - 247).sum(axis=1)) < 60
    # aligned on the markers: C on A, its distances near 0
    three.align_chunks(a, [c], method="markers")
    v.refresh()
    assert abs(_layer(v, "C").stats["median"]) < 0.002 and _layer(v, "C").stats["overlap"] > 0.9
    # the opacity kept with the project; the scene drawn again without reading anything
    v.others_slider.setValue(40)
    v._others_opacity_apply()
    assert three.meta["view"]["opacity"] == pytest.approx(0.4) and v.sd.others_opacity == pytest.approx(0.4)
    # exaggerated heights: the other chunks too
    v.set_exaggeration(3.0)
    shown = next(ly for ly in v.sd.others if ly.label == "B")
    assert np.ptp(shown.points[:, 2]) == pytest.approx(3 * np.ptp(_layer(v, "B").points[:, 2]), rel=0.01)
    # a kept version shown: only the active chunk
    v.show_version("point_cloud", a.product_path("point_cloud"), "copia")
    assert v.base_sd.others == [] and not v.others_row.isVisibleTo(v)


def test_3d_model_preview_of_other_chunks(three, model_view, qapp, tmp_path):
    """With the model shown, the other chunks show the preview of their model; a chunk without one shows its
    points."""
    from pyarchinit_photogrammetry.gui import chunk_views
    from pyarchinit_photogrammetry.io.mesh_ply import write_mesh, MeshData  # noqa: F401

    a, b, c = three.chunks
    import trimesh

    x, y = np.meshgrid(np.linspace(0, 4, 41), np.linspace(0, 3, 31))
    verts = np.column_stack([x.ravel(), y.ravel(), surface(x.ravel(), y.ravel())])
    faces = []
    for r in range(30):
        for q in range(40):
            i = r * 41 + q
            faces += [[i, i + 1, i + 42], [i, i + 42, i + 41]]
    for ch in (a, b):
        path = ch.data_dir / "model.ply"
        trimesh.Trimesh(verts, np.array(faces), process=False).export(path)
        ch._set_product("model", path)
    chunk_views.set_shown_3d(three, b, True)
    chunk_views.set_shown_3d(three, c, True)
    v = model_view
    v.set_chunk(a, "model")
    assert _layer(v, "B").what == "model" and _layer(v, "B").mesh_faces is not None
    assert _layer(v, "C").what == "point_cloud"
    v.canvas.grab_array()  # drawn by the software renderer (the mesh as splats)


# ---------------------------------------------------------------- the window: workspace boxes, 2D rows
def _dem(ch, res=0.02):
    """A DEM of the trench in the internal frame of ``ch``, stamped as built with the identity: a chunk moved later
    (aligned) has it in its old place."""
    import rasterio
    from rasterio.transform import from_origin

    cols, rows = int(4 / res), int(3 / res)
    x = (np.arange(cols) + 0.5) * res
    y = 3.0 - (np.arange(rows) + 0.5) * res
    z = surface(x[None, :], y[:, None]).astype(np.float32)
    path = ch.data_dir / "dem.tif"
    with rasterio.open(path, "w", driver="GTiff", height=rows, width=cols, count=1, dtype="float32",
                       transform=from_origin(0.0, 3.0, res, res), nodata=np.nan) as dst:
        dst.write(z, 1)
    ch._set_product("dem", path)
    ch.meta.setdefault("built_in", {})["dem"] = geometry.identity().tolist()


@pytest.fixture
def win(qapp, tmp_path):
    from PySide6.QtCore import QSettings

    from pyarchinit_photogrammetry.gui.main_window import MainWindow

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    doc = make_project(tmp_path / "finestra.ppg", shift=(0.5, 0.0, 0.0), n=20_000)
    for ch in (doc.chunks[0], doc.chunks[2]):
        _dem(ch)
    doc.save()
    w = MainWindow(ppg.Document.open(doc.path), autoconnect=False, check_updates=False)
    w.runner.show_dialog = False
    w.resize(1400, 900)
    w.show()
    qapp.processEvents()
    yield w
    w.setWindowModified(False)
    w.close()


def _chunk_items(w):
    from PySide6.QtCore import Qt

    out = {}
    for item in w.workspace._walk():
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data and data[0] == "chunk":
            out[data[1].label] = item
    return out


def test_workspace_boxes_show_chunks_in_3d(win, qapp):
    from PySide6.QtCore import Qt

    w = win
    items = _chunk_items(w)
    assert items["A"].checkState(0) == Qt.CheckState.Checked  # the active one
    assert items["B"].checkState(0) == Qt.CheckState.Unchecked
    items["B"].setCheckState(0, Qt.CheckState.Checked)
    assert w.doc.meta["view"]["chunks_3d"] == [w.doc.chunks[1].key] and w.isWindowModified()
    assert [ly.label for ly in w.model_view.base_sd.others] == ["B"]
    assert w.history is None or not w.history.can_undo()  # a choice of the view, no step of Modifica > Annulla
    items["A"].setCheckState(0, Qt.CheckState.Unchecked)  # the active one stays
    assert items["A"].checkState(0) == Qt.CheckState.Checked
    w.workspace.populate(w.doc)  # filled again: the box as kept
    assert _chunk_items(w)["B"].checkState(0) == Qt.CheckState.Checked
    _chunk_items(w)["B"].setCheckState(0, Qt.CheckState.Unchecked)
    assert w.doc.meta["view"]["chunks_3d"] == [] and w.model_view.base_sd.others == []


def test_2d_rows_of_the_other_chunks(win, qapp):
    w = win
    a, b, c = w.doc.chunks
    w._show_raster("dem")
    qapp.processEvents()
    pane, view = w.layers_pane, w.ortho_view
    assert pane.chunks_box.isVisibleTo(pane) and list(pane._chunk_widgets) == [c.key]  # B has no DEM
    box, slider, _label, _hint = pane._chunk_widgets[c.key]
    assert not box.isChecked() and not view.chunk_overlays
    box.setChecked(True)
    qapp.processEvents()
    item = view.chunk_overlays[c.key]
    assert item.isVisible() and item.opacity() == pytest.approx(0.6)
    assert w.doc.meta["view"]["chunks_2d"][str(c.key)] == {"shown": True, "opacity": 0.6}
    # C was moved 0.5 m east after its DEM was built: its DEM drawn 0.5 m east of A's
    rect = item.mapRectToScene(item.boundingRect())
    base = view.pixmap_item.mapRectToScene(view.pixmap_item.boundingRect())
    assert view.world(rect.topLeft())[0] - view.world(base.topLeft())[0] == pytest.approx(0.5, abs=0.03)
    # the view can be moved over the part beyond the raster; the raster itself is what it was
    assert view.sceneRect().contains(rect) and view.raster_rect() == base
    _box, _slider, _label, _hint = pane._chunk_widgets[c.key]  # updated in place (the note)
    assert "spostato con il chunk" in _box.toolTip()
    _slider.setValue(30)
    assert item.opacity() == pytest.approx(0.3)
    assert w.doc.meta["view"]["chunks_2d"][str(c.key)]["opacity"] == pytest.approx(0.3)
    _box.setChecked(False)
    qapp.processEvents()
    assert not view.chunk_overlays[c.key].isVisible()
    # aligned on the markers: C's transform is A's again, its old DEM drawn where A's is
    _box = pane._chunk_widgets[c.key][0]
    _box.setChecked(True)
    w.doc.align_chunks(a, [c], method="markers")
    w._show_raster("dem")
    qapp.processEvents()
    item = view.chunk_overlays[c.key]
    rect = item.mapRectToScene(item.boundingRect())
    assert view.world(rect.topLeft())[0] == pytest.approx(view.world(base.topLeft())[0], abs=0.03)


def test_scene_of_a_chunk_without_cameras_has_its_own_origin(tmp_path):
    """No cameras (a laser scan, a cloud of another program) in a projected system: the scene's origin is the middle of
    its points, so that float32 keeps millimetres."""
    from pyarchinit_photogrammetry.gui.scene import build_scene

    doc = ppg.Document()
    doc.save(tmp_path / "scansione.ppg")
    ch = doc.add_chunk("scan")
    ch.point_cloud = PointCloud(_cloud(5000, 1)[0])
    T = geometry.identity()
    T[:3, 3] = (2480000.0, 4500000.0, 100.0)
    ch.transform, ch.crs = T, "EPSG:3004"
    sd = build_scene(ch, "point_cloud")
    assert np.allclose(sd.offset, (2480002.0, 4500002.0, 100.0), atol=1.0)
    back = sd.points.astype(float) + sd.offset
    assert np.abs(back - ch.to_world(ch.point_cloud.xyz)).max() < 1e-4
