"""Texture of the model: generic and adaptive orthophoto mappings, stored in model.ply with the
image beside it; the textured model is shown whole."""
from __future__ import annotations

import shutil

import cv2
import numpy as np
import rasterio


def _copy(processed, tmp_path, name="copia"):
    import pyarchinit_photogrammetry as ppg

    doc, _ch, _ = processed
    d = tmp_path / name
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")
    out = ppg.Document.open(d / doc.path.name)
    return out, out.chunk


def _texture_colours(ch, n=4000, seed=0):
    """Colour of the texture and plan position of random points on the faces facing up."""
    from pyarchinit_photogrammetry.io.mesh_ply import read_mesh

    mesh = read_mesh(ch.product_path("model"))
    img = cv2.imread(str(ch.product_path("model_textured")))[:, :, ::-1]
    h, w = img.shape[:2]
    world = ch.to_world(mesh.xyz)
    tri = world[mesh.faces]
    nrm = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    up = nrm[:, 2] / np.maximum(np.linalg.norm(nrm, axis=1), 1e-20) > 0.9
    rng = np.random.default_rng(seed)
    fi = rng.choice(np.nonzero(up)[0], n)
    b = rng.dirichlet([1, 1, 1], n)
    xy = (tri[fi] * b[:, :, None]).sum(1)[:, :2]
    uv = (mesh.texcoords.reshape(-1, 3, 2)[fi] * b[:, :, None]).sum(1)
    px = np.clip((uv[:, 0] * w).astype(int), 0, w - 1)
    py = np.clip(((1 - uv[:, 1]) * h).astype(int), 0, h - 1)
    return xy, img[py, px].astype(float)


def test_orthophoto_mapping_matches_the_orthophoto(processed, tmp_path):
    doc, ch = _copy(processed, tmp_path)
    tex = ch.build_texture(texture_size=2048, mapping="orthophoto")
    assert tex.name == "model_texture.jpg" and ch.product_path("model_textured") == tex
    info = ch.meta["texture"]
    assert info["mapping"] == "orthophoto" and info["size"][0] == 2048 and info["size"][1] >= 2048 * 0.5
    xy, colours = _texture_colours(ch)
    with rasterio.open(ch.orthomosaic_path) as src:
        ortho = np.array([v[:3] for v in src.sample([tuple(p) for p in xy])], float)
        alpha = np.array([v[3] for v in src.sample([tuple(p) for p in xy])])
    ok = alpha > 0
    diff = np.abs(colours[ok] - ortho[ok]).mean(axis=1)
    assert ok.mean() > 0.5 and np.median(diff) < 12, (ok.mean(), np.median(diff))  # the same photos from above
    # the textured model is shown whole, with the texture
    from pyarchinit_photogrammetry.gui.scene import build_scene
    from pyarchinit_photogrammetry.io.mesh_ply import read_mesh

    sd = build_scene(ch, "model_textured")
    assert sd.texture == tex and len(sd.mesh_faces) == len(read_mesh(ch.product_path("model")).faces)
    # every other property of the model is kept, and the OBJ export carries the texture
    assert read_mesh(ch.product_path("model")).colors is not None
    obj = ch.export_model(tmp_path / "modello.obj")
    assert (tmp_path / "modello_texture.jpg").exists() and "vt " in obj.read_text()


def test_generic_mapping_is_chosen_for_small_models(processed, tmp_path):
    from pyarchinit_photogrammetry.engine.texturing import choose_mapping
    from pyarchinit_photogrammetry.io.mesh_ply import read_mesh

    doc, ch = _copy(processed, tmp_path)
    mesh = read_mesh(ch.product_path("model"))
    assert choose_mapping(ch, mesh.xyz, mesh.faces) == "generic"  # a few thousand faces
    ch.build_texture(texture_size=1024)
    assert ch.meta["texture"]["mapping"] == "generic"
    xy, colours = _texture_colours(ch, seed=1)
    assert colours.std() > 10  # the photos, not the grey of empty cells
