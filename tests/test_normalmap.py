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
