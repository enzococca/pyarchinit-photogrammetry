"""Mesh from depth maps (TSDF fusion): conventions on exact depth maps, accuracy on the trench."""
from __future__ import annotations

import copy
import dataclasses
import logging
import shutil

import cv2
import numpy as np
import pytest

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.core.geometry import look_at
from pyarchinit_photogrammetry.core.progress import Progress
from pyarchinit_photogrammetry.engine import tsdf
from pyarchinit_photogrammetry.engine.dense import write_colmap_array

pytest.importorskip("open3d")

W, H, F, CX, CY = 320, 240, 300.0, 161.3, 118.6
SPHERE = np.array([0.6, 0.5, 0.9])  # a small ball floating above the ground


def _ground(x, y):
    return 0.3 * x + 0.04 * np.sin(4 * x) * np.cos(3 * y)


def _rgb(p: np.ndarray) -> np.ndarray:
    """Smooth colour of a surface point (RGB, float 0..255)."""
    return np.clip(np.column_stack([128 + 100 * np.sin(2 * p[:, 0]), 128 + 100 * np.cos(2 * p[:, 1]),
                                    128 + 200 * p[:, 2]]), 0, 255)


def _scene():
    import open3d as o3d
    import trimesh

    g = np.linspace(-1.2, 1.2, 121)
    x, y = np.meshgrid(g, g)
    idx = np.arange(x.size).reshape(x.shape)
    a, b, c, d = idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel(), idx[1:, :-1].ravel(), idx[1:, 1:].ravel()
    ground = trimesh.Trimesh(np.column_stack([x.ravel(), y.ravel(), _ground(x, y).ravel()]),
                             np.vstack([np.column_stack([a, b, c]), np.column_stack([b, d, c])]), process=False)
    box = trimesh.creation.box(extents=(0.4, 0.3, 0.4))  # from below the ground up to z = 0.25
    box.apply_translation([0.0, 0.0, 0.05])
    ball = trimesh.creation.icosphere(subdivisions=2, radius=0.04)
    ball.apply_translation(SPHERE)
    mesh = trimesh.util.concatenate([ground, box, ball])
    rc = o3d.t.geometry.RaycastingScene()
    rc.add_triangles(o3d.core.Tensor(mesh.vertices.astype(np.float32)), o3d.core.Tensor(mesh.faces.astype(np.uint32)))
    truth = o3d.t.geometry.RaycastingScene()  # without the ball, which the fusion must drop
    gb = trimesh.util.concatenate([ground, box])
    truth.add_triangles(o3d.core.Tensor(gb.vertices.astype(np.float32)), o3d.core.Tensor(gb.faces.astype(np.uint32)))
    return rc, truth


def _cast(rc, world_from_cam: np.ndarray, offset: float):
    """Hit points of the rays through ``K^-1 (col + offset, row + offset, 1)``: z in the camera
    frame (0 where nothing is hit) and world positions."""
    import open3d as o3d

    yy, xx = np.mgrid[0:H, 0:W].astype(np.float64)
    rays = np.dstack([(xx + offset - CX) / F, (yy + offset - CY) / F, np.ones_like(xx)]).reshape(-1, 3)
    norm = np.linalg.norm(rays, axis=1)
    dirs = (rays / norm[:, None]) @ world_from_cam[:3, :3].T
    origins = np.repeat(world_from_cam[None, :3, 3], len(dirs), 0)
    t = rc.cast_rays(o3d.core.Tensor(np.hstack([origins, dirs]).astype(np.float32)))["t_hit"].numpy()
    hit = np.isfinite(t)
    z = np.where(hit, t / norm, 0.0).reshape(H, W).astype(np.float32)
    return z, origins + dirs * np.where(hit, t, 0)[:, None], hit


@pytest.fixture(scope="module")
def exact(tmp_path_factory):
    """A chunk whose dense workspace holds exact depth maps (COLMAP MVS convention: pixel
    (col, row) is the ray through K^-1 (col, row, 1)) and photos (pixel centres at +0.5)
    of a sloping, bumpy ground with a box on it and a ball floating above it."""
    import pycolmap

    rc, truth = _scene()
    grid = [(x, y) for x in (-0.6, 0, 0.6) for y in (-0.6, 0, 0.6)]
    poses = [look_at((x, y, 2.5), (0.5 * x, 0.5 * y, 0.0), up=(0, 1, 0)) for x, y in grid]  # nadir
    poses += [look_at(eye, (0, 0, 0), up=(0, 0, 1)) for eye in ((1.8, 0, 1.8), (-1.8, 0, 1.8), (0, 1.8, 1.8),
                                                                (0, -1.8, 1.8))]  # oblique
    tmp = tmp_path_factory.mktemp("exact")
    doc = ppg.Document(tmp / "exact.ppg")
    ch = doc.add_chunk("exact")
    ws = ch.data_dir / "dense"
    for sub in ("images", "stereo/depth_maps", "sparse", "txt"):
        (ws / sub).mkdir(parents=True)
    lines = []
    for i, pose in enumerate(poses):
        name = f"c{i:02d}.png"
        depth, _, _ = _cast(rc, pose, 0.0)
        write_colmap_array(ws / "stereo" / "depth_maps" / f"{name}.geometric.bin", depth)
        _, pts, hit = _cast(rc, pose, 0.5)
        rgb = np.where(hit[:, None], _rgb(pts), 0).reshape(H, W, 3)
        cv2.imwrite(str(ws / "images" / name), np.round(rgb[:, :, ::-1]).astype(np.uint8))
        cfw = np.linalg.inv(pose)
        q, t = pycolmap.Rotation3d(cfw[:3, :3]).quat, cfw[:3, 3]  # quat: x, y, z, w
        pose_txt = " ".join(format(float(v), ".17g") for v in (q[3], q[0], q[1], q[2], *t))
        lines.append(f"{i + 1} {pose_txt} 1 {name}\n\n")
    (ws / "txt" / "cameras.txt").write_text(f"1 PINHOLE {W} {H} {F} {F} {CX} {CY}\n")
    (ws / "txt" / "images.txt").write_text("".join(lines))
    (ws / "txt" / "points3D.txt").write_text("")
    rec = pycolmap.Reconstruction()
    rec.read_text(str(ws / "txt"))
    rec.write(str(ws / "sparse"))
    ch.meta["dense"] = {"backend": "cpu", "quality": "high", "filter": "mild"}  # fusion wants 2 depth maps
    return ch, truth


def _distance(scene, points: np.ndarray) -> np.ndarray:
    import open3d as o3d

    return scene.compute_distance(o3d.core.Tensor(np.asarray(points, np.float32))).numpy()


def test_fusion_of_exact_depth_maps(exact):
    ch, truth = exact
    seen = []
    fused = tsdf.fuse_depth_maps(ch, progress=Progress(lambda f, m: seen.append((f, m))))
    v, f = fused.vertices, fused.faces
    assert fused.depth_maps == 13 and abs(fused.pixel_size - 2.5 / F) < 0.1 * 2.5 / F
    assert fused.voxel_size == pytest.approx(tsdf.VOXEL_FACTOR * fused.pixel_size)
    d = _distance(truth, v)
    assert np.median(d) < 0.5e-3 and np.percentile(d, 95) < 3e-3  # voxel ~17 mm
    # no half-pixel shift: on the 17-degree slope it would move the ground by ~1.2 mm
    ground = (np.abs(v[:, 0]) < 1.0) & (np.abs(v[:, 1]) < 1.0) & ((np.abs(v[:, 0]) > 0.35) | (np.abs(v[:, 1]) > 0.3))
    assert abs(np.median(v[ground, 2] - _ground(v[ground, 0], v[ground, 1]))) < 0.3e-3
    # front faces towards the cameras, colours of the photos, the floating ball dropped
    tri = v[f]
    up = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])[:, 2] > 0
    assert up[ground[f[:, 0]]].mean() > 0.98
    assert np.abs(fused.colors[ground].astype(float) - _rgb(v[ground])).mean() < 4
    assert np.linalg.norm(v - SPHERE, axis=1).min() > 0.1
    # the child's reports are relayed (polled: the last one always arrives)
    fractions = [fr for fr, _ in seen]
    assert any(m == "Removing small pieces" for _, m in seen) and fractions == sorted(fractions)
    assert fractions[-1] == 1.0


def test_model_from_depth_maps(exact):
    ch, truth = exact
    ch.products["model_textured"] = "old.obj"
    mesh = ch.build_model(source="depth_maps", face_count=3000)
    assert 2000 < len(mesh.faces) <= 3600 and "model_textured" not in ch.products
    saved = ch.model
    assert len(saved.faces) == len(mesh.faces) and saved.visual.kind == "vertex"
    assert np.percentile(_distance(truth, saved.vertices), 90) < 5e-3
    flat = ch.build_model(source="depth_maps", surface="height_field", face_count=3000, vertex_colors=False)
    assert len(flat.faces) <= 3600 and (flat.face_normals[:, 2] > 0).mean() > 0.99
    assert ch.model.visual.kind is None


def test_voxel_grows_to_fit_the_memory(exact, caplog):
    ch, _ = exact
    with caplog.at_level(logging.WARNING, logger="pyarchinit_photogrammetry"):
        fused = tsdf.fuse_depth_maps(ch, memory_gb=0.001, colors=False)
    assert fused.voxel_size > 1.5 * tsdf.VOXEL_FACTOR * fused.pixel_size and fused.colors is None
    assert any("Voxel size raised" in r.getMessage() for r in caplog.records)


def test_depth_maps_needed(tmp_path):
    ch = ppg.Document(tmp_path / "p.ppg").add_chunk()
    with pytest.raises(RuntimeError, match="build depth maps first"):
        ch.build_model(source="depth_maps")
    (ch.data_dir / "dense" / "sparse").mkdir(parents=True)
    ch.meta["dense"] = {"backend": "openmvs", "quality": "medium", "filter": "moderate"}
    with pytest.raises(RuntimeError, match="OpenMVS"):
        ch.build_model(source="depth_maps")


# ---------------------------------------------------------------- synthetic trench
@pytest.fixture(scope="module")
def trench_twin(processed, tmp_path_factory):
    """The processed trench as a second chunk with its own products and the same depth maps."""
    _, ch, _ = processed
    doc = ppg.Document(tmp_path_factory.mktemp("twin") / "twin.ppg")
    twin = dataclasses.replace(ch, key=0, document=doc, products={}, meta=copy.deepcopy(ch.meta))
    doc.chunks.append(twin)
    try:
        (twin.data_dir / "dense").symlink_to(ch.data_dir / "dense", target_is_directory=True)
    except OSError:  # pragma: no cover - no symbolic links (Windows without privileges)
        shutil.copytree(ch.data_dir / "dense", twin.data_dir / "dense")
    return ch, twin


def _surface_errors(chunk, mesh, scene, truth):
    """Distances (m) to the true surface of points sampled uniformly on the mesh inside the
    trench area, and the share of the true surface the mesh covers within 1 cm."""
    import open3d as o3d
    import trimesh

    local = chunk.to_world(np.asarray(mesh.vertices)) - np.asarray(scene.origin)
    pts, _ = trimesh.sample.sample_surface(trimesh.Trimesh(local, mesh.faces, process=False), 100_000, seed=1)
    inside = (pts[:, 0] > 0.05) & (pts[:, 0] < 3.95) & (pts[:, 1] > 0.05) & (pts[:, 1] < 2.95)
    d = _distance(truth, pts[inside])
    rc = o3d.t.geometry.RaycastingScene()
    rc.add_triangles(o3d.core.Tensor(local.astype(np.float32)), o3d.core.Tensor(np.asarray(mesh.faces, np.uint32)))
    tv, _ = scene.surface(step=2)
    inner = tv[(tv[:, 0] > 0.4) & (tv[:, 0] < 3.6) & (tv[:, 1] > 0.4) & (tv[:, 1] < 2.6)][::7]
    return d, float(np.mean(_distance(rc, inner) < 0.01))


@pytest.mark.slow
def test_trench_mesh_from_depth_maps(trench_twin, scene):
    """On the synthetic trench the fused mesh is at least as close to the true surface as
    the Poisson mesh of the dense cloud, and texturing works on it."""
    import open3d as o3d

    ch, twin = trench_twin
    truth = o3d.t.geometry.RaycastingScene()
    tv, tf = scene.surface(step=2)
    truth.add_triangles(o3d.core.Tensor(tv), o3d.core.Tensor(tf))
    mesh = twin.build_model(source="depth_maps")
    d, covered = _surface_errors(twin, mesh, scene, truth)
    poisson, poisson_covered = _surface_errors(ch, ch.model, scene, truth)
    # measured: median 0.9 mm, p90 2.2 mm, 99.3 % covered (Poisson: 1.0 mm, 2.8 mm, 99.8 %)
    assert np.median(d) < 0.002 and np.percentile(d, 90) < 0.005  # trench walls included
    assert np.median(d) < np.median(poisson) + 0.0005 and np.percentile(d, 90) < np.percentile(poisson, 90) + 0.001
    assert covered > 0.97 and covered > poisson_covered - 0.01
    assert twin.model.visual.kind == "vertex"
    obj = twin.build_texture(texture_size=1024)
    assert obj.exists() and twin.product_path("model_textured") is not None
