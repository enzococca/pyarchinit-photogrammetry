"""Align and merge chunks: two overlapping parts of the synthetic trench, aligned separately."""
from __future__ import annotations

import json

import numpy as np
import pytest

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.core import geometry
from pyarchinit_photogrammetry.engine import chunks as chunks_mod
from pyarchinit_photogrammetry.engine.chunks import common_cameras

SHARED = 7  # photos in both chunks: nadir row 2 and two oblique photos


def _photos(scene_dir, rows, ring):
    photos = sorted((scene_dir / "photos").glob("IMG_*.jpg"))
    return [photos[j * 5 + i] for j in rows for i in range(5)] + [photos[20 + k] for k in ring]


@pytest.fixture(scope="module")
def two_chunks(scene_dir, tmp_path_factory):
    """Chunk A: nadir rows 0-2 and half of the oblique ring; chunk B: rows 2-3 and the other
    half. Each aligned on its own, so each lives in its own arbitrary frame."""
    path = tmp_path_factory.mktemp("chunks") / "two.ppg"
    doc = ppg.Document()
    doc.save(path)
    a, b = doc.add_chunk("A"), doc.add_chunk("B")
    a.add_photos(_photos(scene_dir, [0, 1, 2], [0, 4, 5, 6, 7]))
    b.add_photos(_photos(scene_dir, [2, 3], [0, 1, 2, 3, 4]))
    for ch in (a, b):
        ch.detect_markers("aruco_4x4_50")
        ch.match_photos(quality="high")
        assert ch.align_cameras() == len(ch.cameras)
    doc.save()
    return path


@pytest.fixture
def doc(two_chunks):
    """The project in memory (tests that write files save a copy first)."""
    return ppg.Document.open(two_chunks)


def _gcp(scene, label: str) -> np.ndarray:
    return np.asarray(scene.targets[int(label.split()[-1])], float)


def _metres_per_unit(chunk, scene) -> float:
    """Size of a world unit of a chunk in metres, from its markers and the surveyed targets."""
    ms = [m for m in chunk.markers if m.position is not None]
    world = chunk.to_world(np.array([m.position for m in ms]))
    gcp = np.array([_gcp(scene, m.label) for m in ms])
    return geometry.decompose_similarity(geometry.umeyama(world, gcp - gcp.mean(0)))[0]


def _shared_centre_errors(a, b) -> np.ndarray:
    pairs = common_cameras(b, a)
    assert len(pairs) == SHARED
    return np.array([np.linalg.norm(b.to_world(cb.center) - a.to_world(ca.center)) for cb, ca in pairs])


def _true_centres(scene) -> dict[str, np.ndarray]:
    from pyarchinit_photogrammetry.testing.synthetic import camera_poses

    return {f"IMG_{i:04d}": p[:3, 3] + np.asarray(scene.origin) for i, p in enumerate(camera_poses(scene))}


def _georeference(chunk, scene_dir) -> None:
    chunk.import_reference(scene_dir / "gcp.csv", crs="EPSG:3004")
    assert chunk.update_transform()["control_rmse"] < 0.002


# ------------------------------------------------------------------ align
@pytest.mark.slow
def test_align_markers_between_arbitrary_frames(doc, scene):
    a, b = doc.chunks
    rep = doc.align_chunks(a, [b], method="markers")
    info = rep["chunks"][0]
    assert rep["reference"] == {"key": a.key, "label": "A"} and info["label"] == "B"
    assert rep["levelled_reference"] and a.meta["levelled"] and b.meta["levelled"]
    unit = _metres_per_unit(a, scene)
    assert info["method"] == "markers" and info["correspondences"] >= 4
    assert info["rmse"] * unit < 0.002 and len(info["residuals"]) == info["correspondences"]
    assert np.allclose(b.transform, info["transform"]) and b.crs == a.crs
    assert b.meta["chunk_alignment"]["reference"] == "A"
    assert _shared_centre_errors(a, b).max() * unit < 0.006  # the same photo, aligned twice


@pytest.mark.slow
def test_align_cameras(doc, scene):
    a, b = doc.chunks
    info = doc.align_chunks("A", ["B"], method="cameras")["chunks"][0]
    unit = _metres_per_unit(a, scene)
    assert info["correspondences"] == SHARED and info["max_angle_deg"] < 0.3
    assert _shared_centre_errors(a, b).max() * unit < 0.002


@pytest.mark.slow
def test_align_points_from_markers(doc, scene):
    a, b = doc.chunks
    info = doc.align_chunks(a, [b], method="points")["chunks"][0]
    assert info["initial"] == "markers" and info["source"] == info["target"] == "tie_points"
    assert info["inlier_ratio"] > 0.7 and info["color_corr"] > 0.6
    unit = _metres_per_unit(a, scene)
    assert info["check"]["rmse"] * unit < 0.003  # the markers, not used by the registration
    assert _shared_centre_errors(a, b).max() * unit < 0.006


@pytest.mark.slow
def test_align_points_global_registration(doc, scene):
    """No initial position: FPFH descriptors + RANSAC, then the scaled ICP."""
    pytest.importorskip("open3d")
    a, b = doc.chunks
    info = doc.align_chunks(a, [b], method="points", initial="global")["chunks"][0]
    assert info["initial"] == "global"
    unit = _metres_per_unit(a, scene)
    assert info["check"]["rmse"] * unit < 0.003
    assert _shared_centre_errors(a, b).max() * unit < 0.006


@pytest.mark.slow
def test_align_points_with_dense_clouds(doc, scene, tmp_path):
    doc.save(tmp_path / "dense.ppg")
    a, b = doc.chunks
    for ch in (a, b):
        ch.build_depth_maps(quality="low")
        ch.build_point_cloud()
    info = doc.align_chunks(a, [b], method="points")["chunks"][0]
    assert info["source"] == info["target"] == "point_cloud" and info["color_corr"] > 0.6
    unit = _metres_per_unit(a, scene)
    assert info["check"]["rmse"] * unit < 0.006  # dense clouds from 150 x 112 pixel images
    assert _shared_centre_errors(a, b).max() * unit < 0.006


@pytest.mark.slow
def test_align_to_georeferenced_chunk(doc, scene, scene_dir):
    """Reference in a national grid (millions of metres): the aligned chunk takes its CRS and
    its photos land on the true positions; a second, rigid alignment keeps the scale."""
    a, b = doc.chunks
    _georeference(a, scene_dir)
    old = b.transform.copy()
    rep = doc.align_chunks(a, [b], method="markers")
    assert not rep["levelled_reference"] and b.crs == "EPSG:3004" and rep["crs"] == "EPSG:3004"
    assert rep["chunks"][0]["rmse"] < 0.002  # metres
    truth = _true_centres(scene)
    errors = [np.linalg.norm(b.to_world(c.center) - truth[c.label]) for c in b.aligned_cameras]
    assert max(errors) < 0.006
    assert _shared_centre_errors(a, b).max() < 0.006
    delta = np.asarray(rep["chunks"][0]["delta"])  # old world -> EPSG:3004
    centres = np.array([c.center for c in b.aligned_cameras])
    assert np.abs(geometry.apply(delta, geometry.apply(old, centres)) - b.to_world(centres)).max() < 1e-6
    scale = b.scale
    info = doc.align_chunks(a, [b], method="points", fix_scale=True, initial="reference")["chunks"][0]
    assert info["initial"] == "reference" and abs(info["scale"] - 1.0) < 1e-12
    assert abs(b.scale - scale) < 1e-9 * scale
    assert _shared_centre_errors(a, b).max() < 0.006


@pytest.mark.slow
def test_align_refuses_and_changes_nothing(doc):
    a, b = doc.chunks
    before_a, before_b = a.transform.copy(), b.transform.copy()
    with pytest.raises(ValueError, match="arbitrary units"):
        doc.align_chunks(a, [b], method="markers", fix_scale=True)
    empty = doc.add_chunk("C")
    with pytest.raises(ValueError, match="markers in common"):
        doc.align_chunks(a, [b, empty], method="markers")
    progress = ppg.Progress()
    progress.cancel()
    with pytest.raises(ppg.Cancelled):
        doc.align_chunks(a, [b], method="points", progress=progress)
    assert np.array_equal(a.transform, before_a) and np.array_equal(b.transform, before_b)
    assert "levelled" not in a.meta and b.crs == "LOCAL"
    with pytest.raises(KeyError):
        doc.align_chunks("A", ["nope"])
    with pytest.raises(ValueError, match="method"):
        doc.align_chunks(a, [b], method="magic")


# ------------------------------------------------------------------ merge
@pytest.mark.slow
def test_merge_chunks(doc, scene, scene_dir, tmp_path):
    from pyarchinit_photogrammetry.engine.sfm import load_reconstruction

    doc.save(tmp_path / "merge.ppg")
    a, b = doc.chunks
    _georeference(a, scene_dir)
    doc.align_chunks(a, [b], method="markers")
    n_tie_a, n_tie_b = len(a.tie_points), len(b.tie_points)
    merged = doc.merge_chunks([a, b], label="A+B")
    assert [c.label for c in doc.chunks] == ["A", "B", "A+B"] and merged.key == 2
    assert len(a.cameras) == 20 and len(b.cameras) == 15  # the inputs are kept
    # photos: the 7 shared ones once, new keys, poses in the frame of A
    assert len(merged.cameras) == 28 and merged.meta["merge"]["duplicate_photos"] == SHARED
    assert sorted(c.key for c in merged.cameras) == list(range(28))
    assert len({c.path for c in merged.cameras}) == 28 and {c.sensor_key for c in merged.cameras} <= set(merged.sensors)
    assert merged.crs == "EPSG:3004" and np.allclose(merged.transform, a.transform)
    truth = _true_centres(scene)
    assert max(np.linalg.norm(merged.to_world(c.center) - truth[c.label]) for c in merged.cameras) < 0.006
    # markers merged by label, projections combined, positions triangulated again
    assert sorted(m.label for m in merged.markers) == sorted({m.label for m in a.markers + b.markers})
    for m in merged.markers:
        assert len(m.projections) >= max(len(x.projections) for x in a.markers + b.markers if x.label == m.label)
        assert np.linalg.norm(merged.to_world(m.position) - _gcp(scene, m.label)) < 0.003
        assert m.reference.location is not None  # the GCPs of A
    # tie points of both chunks, on the same surface
    tie = merged.tie_points
    assert len(tie) == n_tie_a + n_tie_b and tie.rgb is not None
    world = merged.to_world(tie.xyz)
    dz = world[:, 2] - scene.world_height(world[:, 0], world[:, 1])
    med_a, med_b = np.median(dz[:n_tie_a]), np.median(dz[n_tie_a:])
    assert abs(med_a) < 0.002 and abs(med_b) < 0.002 and abs(med_a - med_b) < 0.002
    assert np.median(np.abs(dz[n_tie_a:])) < 0.003
    # a COLMAP model for the dense step
    rec = load_reconstruction(merged)
    assert rec.num_reg_images() == 28 and rec.num_points3D() > 1000
    # saved and reopened with the document
    doc.save()
    again = ppg.Document.open(doc.path)
    m2 = again.chunks[2]
    assert m2.label == "A+B" and len(m2.cameras) == 28 and len(m2.markers) == len(merged.markers)
    assert m2.crs == "EPSG:3004" and np.allclose(m2.transform, merged.transform)
    assert len(m2.tie_points) == len(tie) and m2.product_path("reconstruction").exists()
    assert m2.data_dir.name == "chunk_2" and str(m2.data_dir).startswith(str(again.files_dir))
    m2.build_depth_maps(quality="low")
    assert len(m2.build_point_cloud()) > 1000


@pytest.mark.slow
def test_merge_options_clouds_and_models(doc, tmp_path):
    """Dense clouds (with and without colours) and meshes are concatenated in the frame of
    the first chunk; markers can stay separate; tie points can be left out."""
    import trimesh

    doc.save(tmp_path / "options.ppg")
    a, b = doc.chunks
    doc.align_chunks(a, [b], method="cameras")
    S = chunks_mod.affine_inverse(a.transform) @ b.transform  # B internal -> A internal
    hand = np.zeros(len(a.tie_points), bool)
    hand[:10] = True  # classes corrected by hand in A only
    a.point_cloud = ppg.PointCloud(a.tie_points.xyz, a.tie_points.rgb, classification=np.full(len(a.tie_points), 2),
                                   manual=hand)
    b.point_cloud = ppg.PointCloud(b.tie_points.xyz[:500])
    for ch, size in ((a, 1.0), (b, 2.0)):
        box = trimesh.creation.box(extents=(size, size, size))
        box.visual.vertex_colors = np.tile([200, 10, 10, 255], (len(box.vertices), 1)).astype(np.uint8)
        box.export(str(ch.data_dir / "model.ply"))
        ch._set_product("model", ch.data_dir / "model.ply")
    merged = doc.merge_chunks([a, b], merge_markers=False, merge_tie_points=False, merge_models=True)
    assert merged.label == "Merged chunk" and merged.tie_points is None
    assert len(merged.markers) == len(a.markers) + len(b.markers)
    pc = merged.point_cloud
    assert len(pc) == len(a.tie_points) + 500
    assert np.allclose(pc.xyz[:len(a.tie_points)], a.tie_points.xyz)
    assert np.allclose(pc.xyz[len(a.tie_points):], geometry.apply(S, b.tie_points.xyz[:500]))
    assert (pc.rgb[len(a.tie_points):] == 128).all() and (pc.classification[:len(a.tie_points)] == 2).all()
    assert pc.manual is not None and pc.manual.sum() == 10 and pc.manual[:10].all()
    mesh = merged.model
    assert len(mesh.vertices) == 16 and len(mesh.faces) == 24
    box_b = trimesh.creation.box(extents=(2.0, 2.0, 2.0))
    assert np.allclose(mesh.vertices[8:], geometry.apply(S, box_b.vertices), atol=1e-5)
    assert np.all(mesh.faces[12:] >= 8) and (np.asarray(mesh.visual.vertex_colors)[:, 0] == 200).all()
    from pyarchinit_photogrammetry.engine.sfm import load_reconstruction

    # the COLMAP model still gets the tie points of both chunks as 3D points
    assert load_reconstruction(merged).num_points3D() > 1000


@pytest.mark.slow
def test_merge_needs_a_common_frame(doc, scene_dir):
    a, b = doc.chunks
    _georeference(a, scene_dir)  # A in EPSG:3004, B still in its own local frame
    with pytest.raises(ValueError, match="align them first"):
        doc.merge_chunks([a, b])
    with pytest.raises(ValueError, match="at least two"):
        doc.merge_chunks([a])
    assert len(doc.chunks) == 2
    progress = ppg.Progress()
    progress.cancel()
    doc.align_chunks(a, [b])
    with pytest.raises(ppg.Cancelled):
        doc.merge_chunks([a, b], progress=progress)
    assert len(doc.chunks) == 2  # the half-made chunk is removed


@pytest.mark.slow
def test_cli_align_and_merge(two_chunks, tmp_path):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    ppg.Document.open(two_chunks).save(tmp_path / "cli.ppg")
    runner = CliRunner()
    res = runner.invoke(app, ["align-chunks", str(tmp_path / "cli.ppg"), "B", "--reference", "A",
                              "--method", "cameras", "--json"])
    assert res.exit_code == 0, res.output
    report = json.loads(res.stdout[res.stdout.index("{"):])
    assert report["chunks"][0]["label"] == "B" and report["chunks"][0]["correspondences"] == SHARED
    res = runner.invoke(app, ["merge-chunks", str(tmp_path / "cli.ppg"), "A", "B", "--label", "AB", "--activate"])
    assert res.exit_code == 0, res.output
    assert "AB: 28 photos" in res.output
    again = ppg.Document.open(tmp_path / "cli.ppg")
    assert [c.label for c in again.chunks] == ["A", "B", "AB"] and again.chunk.label == "AB"
    res = runner.invoke(app, ["align-chunks", str(tmp_path / "cli.ppg"), "B", "-r", "A", "--method", "markers",
                              "--fix-scale"])
    assert res.exit_code == 1 and "arbitrary units" in res.output


# ------------------------------------------------------------------ building blocks
def test_pose_similarity_with_photos_on_a_line(rng):
    """Photos along a strip (centres on a line): the rotation comes from the orientations."""
    S = geometry.compose_similarity(0.4, geometry.rotation_from_euler_ypr(40, -10, 25), [3.0, -1.0, 7.0])
    src = [geometry.look_at([x, 0.0, 2.0], [x + 0.1, 0.3, 0.0]) for x in (0.0, 1.0, 2.5, 4.0)]
    dst = [chunks_mod._rigid_pose(S, p) for p in src]
    for with_scale in (True, False):
        M = chunks_mod.pose_similarity(src, dst, with_scale=with_scale)
        if with_scale:
            assert np.allclose(M, S, atol=1e-9)
        else:
            assert np.allclose(M[:3, :3], S[:3, :3] / 0.4, atol=1e-9)
    with pytest.raises(ValueError, match="two common photos"):
        chunks_mod.pose_similarity(src[:1], dst[:1], with_scale=True)


def _surface(rng, n, x0=0.0, x1=4.0):
    x, y = rng.uniform(x0, x1, n), rng.uniform(0, 3, n)
    z = 0.08 * np.sin(x * 1.7) * np.cos(y * 2.1) + 0.04 * np.sin(4 * x + 1)
    z -= 0.35 * ((x > 1) & (x < 3) & (y > 0.9) & (y < 2.1))  # the trench
    return np.column_stack([x, y, z])


def test_icp_and_global_registration(rng):
    """Two samplings of the same surface overlapping in part, the moving one in another
    frame (rotation, translation, scale 0.37): ICP from a rough position, and the FPFH +
    RANSAC position without any, both land within a millimetre."""
    dst = _surface(rng, 60000)
    S = geometry.compose_similarity(0.37, geometry.rotation_from_euler_ypr(30, 5, -3), [1.0, -2.0, 0.5])
    src = geometry.apply(chunks_mod.affine_inverse(S), _surface(rng, 15000, 1.5, 4.0))
    probe = np.array([[2.0, 1.5, 0.0], [3.5, 0.5, 0.1], [1.6, 2.8, -0.1]])
    rough = geometry.compose_similarity(0.37 * 1.03, geometry.rotation_from_euler_ypr(32, 6, -2), [1.05, -2.03, 0.52])
    M = chunks_mod.icp(src, dst, rough, start=0.3, final=0.03)
    assert np.linalg.norm(geometry.apply(M @ chunks_mod.affine_inverse(S), probe) - probe, axis=1).max() < 0.002
    q = chunks_mod.registration_quality(src, dst, M, 0.03)
    assert q["inlier_ratio"] > 0.95 and q["overlap"] > 0.95
    pytest.importorskip("open3d")
    best = None
    for G, voxel in chunks_mod.global_guesses(src, dst, ratio=0.37):
        W = chunks_mod.icp(src, dst, G, start=2 * voxel, final=0.03)
        q = chunks_mod.registration_quality(src, dst, W, 0.03)
        if best is None or q["inlier_ratio"] > best[1]["inlier_ratio"]:
            best = (W, q)
    assert np.linalg.norm(geometry.apply(best[0] @ chunks_mod.affine_inverse(S), probe) - probe, axis=1).max() < 0.002


def test_ransac_is_deterministic(rng):
    a = rng.uniform(-1, 1, (300, 3))
    T = geometry.compose_similarity(1.0, geometry.rotation_from_euler_ypr(70, 20, -40), [0.5, 2.0, -1.0])
    b = geometry.apply(T, a)
    b[:200] = rng.uniform(-3, 3, (200, 3))  # two thirds of the pairs are wrong
    M1, n1 = chunks_mod.ransac_rigid(a, b, 0.01)
    M2, n2 = chunks_mod.ransac_rigid(a, b, 0.01)
    assert np.array_equal(M1, M2) and n1 == n2 == 100
    assert np.allclose(M1, T, atol=1e-9)


def test_ransac_keeps_the_other_motion_of_a_symmetric_scene(rng):
    """A symmetric scene (a rectangular trench) matches points to their mirror images too: the
    motion with most pairs can be the wrong one, so RANSAC keeps the best distinct motions and
    the ICP with the colours chooses (the global registration failed on Linux without it)."""
    a = rng.uniform(-1, 1, (300, 3))
    right = geometry.compose_similarity(1.0, geometry.rotation_from_euler_ypr(30, 0, 0), [0.5, 0.2, 0.0])
    turned = geometry.compose_similarity(1.0, geometry.rotation_from_euler_ypr(210, 0, 0), [0.5, 0.2, 0.0])
    b = np.vstack([geometry.apply(turned, a[:180]), geometry.apply(right, a[180:])])
    best, n = chunks_mod.ransac_rigid(a, b, 0.01)
    assert n == 180 and np.allclose(best, turned, atol=1e-9)
    found = chunks_mod.ransac_hypotheses(a, b, 0.01)
    assert [m for _M, m in found][:2] == [180, 120]
    assert any(np.allclose(M, right, atol=1e-9) for M, _m in found)


def test_align_and_merge_from_the_interface(two_chunks, tmp_path, monkeypatch):
    """Flusso di lavoro > Allinea chunk / Unisci chunk: the dialogs choose the chunks, the
    tasks run in the background and the merged chunk becomes the active one."""
    pytest.importorskip("PySide6")
    import shutil

    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "settings"))
    app = QApplication.instance() or QApplication([])
    from pyarchinit_photogrammetry.gui import main_window as mw

    copy = tmp_path / "gui" / "two.ppg"
    copy.parent.mkdir()
    shutil.copy(two_chunks, copy)
    shutil.copytree(str(two_chunks) + ".files", str(copy) + ".files")
    doc = ppg.Document.open(copy)
    a, b = doc.chunks
    w = mw.MainWindow(doc, autoconnect=False)
    w.runner.show_dialog = False
    infos = []
    monkeypatch.setattr(w, "_info", lambda title, text: infos.append(text))
    try:
        monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(lambda cls, *args, **kw: {
            "reference": a.key, "chunks": [b.key], "method": "cameras", "fix_scale": False}))
        before = b.transform.copy()
        w.align_chunks()
        w.runner.wait()
        app.processEvents()
        assert not np.allclose(b.transform, before) and b.meta["chunk_alignment"]
        assert infos and "B: " in infos[-1] and "corrispondenze" in infos[-1]
        monkeypatch.setattr(mw.ParamDialog, "ask", classmethod(lambda cls, *args, **kw: {
            "chunks": [a.key, b.key], "label": "Saggio unito", "markers": True, "tie_points": True,
            "clouds": True, "models": False, "activate": True}))
        w.merge_chunks()
        w.runner.wait()
        app.processEvents()
        merged = doc.chunks[-1]
        assert len(doc.chunks) == 3 and merged.label == "Saggio unito" and doc.chunk is merged
        assert len(merged.cameras) == len({c.label for c in a.cameras} | {c.label for c in b.cameras})
        assert "Saggio unito" in infos[-1]
    finally:
        w.setWindowModified(False)
        w.close()
