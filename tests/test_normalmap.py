"""Light models: colour texture and normal map baked from the full model."""
from __future__ import annotations

import shutil

import numpy as np
import pytest

from pyarchinit_photogrammetry.engine.normalmap import bake_maps


def _waves(n: int = 200, amplitude: float = 0.03, k: float = 2 * np.pi * 3):
    """A square metre of ground with ridges along y (z = a sin(k x)), finely tessellated."""
    x, y = np.meshgrid(np.linspace(0, 1, n), np.linspace(0, 1, n))
    z = amplitude * np.sin(k * x)
    v = np.column_stack([x.ravel(), y.ravel(), z.ravel()])
    idx = np.arange(n * n).reshape(n, n)
    a, b, c, d = idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel(), idx[1:, :-1].ravel(), idx[1:, 1:].ravel()
    f = np.vstack([np.column_stack([a, b, c]), np.column_stack([b, d, c])])
    colors = np.column_stack([np.clip(128 + 120 * np.sin(k * x.ravel()), 0, 255), np.full(n * n, 90),
                              np.full(n * n, 40)])
    return v, f, colors, amplitude, k


def test_normal_map_holds_the_relief_of_the_full_model():
    fv, ff, colors, a, k = _waves()
    lv = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0]], float)  # the light model: two triangles
    lf = np.array([[0, 1, 2], [1, 3, 2]])
    colour, normal, uv, share = bake_maps(fv, ff, lv, lf, size=256, full_colors=colors)
    assert share > 0.98 and colour.shape == normal.shape == (256, 256, 3)
    # decode the normal map of face 0 in world coordinates and compare with the true normals
    side = colour.shape[0]
    e1, e2 = lv[1] - lv[0], lv[2] - lv[0]
    d1, d2 = uv[0, 1] - uv[0, 0], uv[0, 2] - uv[0, 0]
    det = d1[0] * d2[1] - d2[0] * d1[1]
    T = (e1 * d2[1] - e2 * d1[1]) / det
    B = (e2 * d1[0] - e1 * d2[0]) / det
    N = np.array([0, 0, 1.0])
    T, B = T / np.linalg.norm(T), B / np.linalg.norm(B)
    errors = []
    for _ in range(400):
        w = np.random.default_rng(len(errors)).dirichlet([2, 2, 2])
        p = (w[:, None] * lv[lf[0]]).sum(0)
        st = (w[:, None] * uv[0]).sum(0)
        col, row = int(st[0] * side), int((1 - st[1]) * side)
        ts = normal[row, col].astype(float) / 127.5 - 1
        world = ts[0] * T + ts[1] * B + ts[2] * N
        world /= np.linalg.norm(world)
        true = np.array([-a * k * np.cos(k * p[0]), 0.0, 1.0])
        true /= np.linalg.norm(true)
        errors.append(np.degrees(np.arccos(np.clip(world @ true, -1, 1))))
    assert np.median(errors) < 3.0 and np.percentile(errors, 90) < 8.0, (np.median(errors), np.max(errors))
    # the colour follows the ridges too: red where sin(k x) is high (BGR: red last)
    reds, truth = [], []
    for i in range(200):
        w = np.random.default_rng(1000 + i).dirichlet([2, 2, 2])
        p = (w[:, None] * lv[lf[0]]).sum(0)
        st = (w[:, None] * uv[0]).sum(0)
        reds.append(colour[int((1 - st[1]) * side), int(st[0] * side), 2])
        truth.append(128 + 120 * np.sin(k * p[0]))
    assert np.corrcoef(reds, truth)[0, 1] > 0.9


def test_same_mesh_takes_each_texel_from_its_own_face():
    """A light mesh that is the full one (the leaves of an I3S package): every texel from the
    texture at the same place of its face, without rays, as the rays find it."""
    fv, ff, _colors, _a, _k = _waves(n=40)
    texcoords = fv[ff][:, :, :2].reshape(-1, 6)  # the texture lies on the plan: u = x, v = y
    yy, xx = np.mgrid[0:256, 0:256]
    texture = np.dstack([(xx * 0.9).astype(np.uint8), (yy * 0.9).astype(np.uint8),
                         np.where((xx // 32 + yy // 32) % 2, 230, 30).astype(np.uint8)])  # BGR
    direct = bake_maps(fv, ff, fv, ff, size=512, full_texcoords=texcoords, full_texture=texture,
                       same_mesh=True, normals=False)
    rays = bake_maps(fv, ff, fv, ff, size=512, full_texcoords=texcoords, full_texture=texture, normals=False)
    assert direct[1] is None and direct[3] == 1.0
    side = direct[0].shape[0]
    rng = np.random.default_rng(3)
    errors, apart = [], []
    for face in rng.choice(len(ff), 300, replace=False):
        w = rng.dirichlet([3, 3, 3])
        st = (w[:, None] * direct[2][face]).sum(0)
        row, col = int((1 - st[1]) * side), int(st[0] * side)
        x, y = (w[:, None] * fv[ff[face]]).sum(0)[:2]
        true = texture[min(255, int((1 - y) * 256)), min(255, int(x * 256))].astype(float)
        if true[2] in (30, 230) and abs(((x * 256) % 32) - 16) < 13 and abs(((1 - y) * 256 % 32) - 16) < 13:
            errors.append(np.abs(direct[0][row, col].astype(float) - true).max())  # away from the checker edges
        apart.append(np.abs(direct[0][row, col].astype(float) - rays[0][row, col]).max())
    assert len(errors) > 100 and np.median(errors) <= 3 and np.percentile(errors, 95) <= 8
    assert np.median(apart) <= 2


def test_light_model_of_the_trench(processed, tmp_path):
    import trimesh

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.engine.normalmap import light_model

    doc, _ch, _ = processed
    d = tmp_path / "copia"
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    ch = ppg.Document.open(d / doc.path.name).chunk
    out = light_model(ch, target_faces=3000, texture_size=1024)
    assert out["faces"] <= 3300 and out["hits"] > 0.9
    scene = trimesh.load(out["glb"])
    mesh = next(iter(scene.geometry.values()))
    mat = mesh.visual.material
    assert mat.normalTexture is not None and mat.baseColorTexture is not None
    assert "norm light_normal.png" in out["obj"].with_suffix(".mtl").read_text()
    import cv2

    nm = cv2.imread(str(out["normal"]))
    assert np.median(nm[..., 0]) > 200  # mostly facing out of the surface (blue in BGR order first)
    with pytest.raises(RuntimeError):
        light_model(ppg.Document().add_chunk("vuoto"))


def test_command_line(processed, tmp_path):
    from typer.testing import CliRunner

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.cli import app

    doc, _ch, _ = processed
    d = tmp_path / "copia"
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    path = ppg.Document.open(d / doc.path.name).path
    r = CliRunner().invoke(app, ["light-model", str(path), str(tmp_path / "leggero"), "--faces", "1500",
                                 "--texture", "512"])
    assert r.exit_code == 0, r.output + repr(r.exception)
    assert (tmp_path / "leggero" / "light.glb").exists() and "faces" in r.output
