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


def _page_colours(ch, n=4000, seed=0):
    """As :func:`_texture_colours`, each point coloured from the page of its face."""
    from pyarchinit_photogrammetry.engine.texturing import texture_pages
    from pyarchinit_photogrammetry.io.mesh_ply import read_mesh

    mesh = read_mesh(ch.product_path("model"))
    pages = [cv2.imread(str(p))[:, :, ::-1] for p in texture_pages(ch)]
    world = ch.to_world(mesh.xyz)
    tri = world[mesh.faces]
    nrm = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    up = nrm[:, 2] / np.maximum(np.linalg.norm(nrm, axis=1), 1e-20) > 0.9
    rng = np.random.default_rng(seed)
    fi = rng.choice(np.nonzero(up)[0], n)
    b = rng.dirichlet([1, 1, 1], n)
    xy = (tri[fi] * b[:, :, None]).sum(1)[:, :2]
    uv = (mesh.texcoords.reshape(-1, 3, 2)[fi] * b[:, :, None]).sum(1)
    page = mesh.texnumber[fi] if mesh.texnumber is not None else np.zeros(n, int)
    out = np.zeros((n, 3))
    for k, img in enumerate(pages):
        sel = page == k
        h, w = img.shape[:2]
        px = np.clip((uv[sel, 0] * w).astype(int), 0, w - 1)
        py = np.clip(((1 - uv[sel, 1]) * h).astype(int), 0, h - 1)
        out[sel] = img[py, px]
    return xy, out


def test_texture_pages(processed, tmp_path):
    """A texture larger than a page is split into pages (here pages of 512 px): the orthophoto
    cut in tiles and the generic cells in groups of nearby faces, each face on its page; exports,
    the 3D view and the versions carry every page."""
    import trimesh

    from pyarchinit_photogrammetry.core import versions
    from pyarchinit_photogrammetry.engine.texturing import texture_pages
    from pyarchinit_photogrammetry.gui.scene import build_scene
    from pyarchinit_photogrammetry.io.mesh_ply import read_mesh

    doc, ch = _copy(processed, tmp_path)
    ch.build_texture(texture_size=1024, mapping="orthophoto", page_size=512)
    pages = texture_pages(ch)
    mesh = read_mesh(ch.product_path("model"))
    assert len(pages) >= 4 and ch.meta["texture"]["pages"] == len(pages) and all(p.exists() for p in pages)
    assert mesh.texture_files == [p.name for p in pages] and set(np.unique(mesh.texnumber)) == set(range(len(pages)))
    assert max(cv2.imread(str(p)).shape[1] for p in pages) <= 512 + 64  # a page reaches a little beyond its tile
    xy, colours = _page_colours(ch)
    with rasterio.open(ch.orthomosaic_path) as src:
        ortho = np.array([v[:3] for v in src.sample([tuple(p) for p in xy])], float)
        alpha = np.array([v[3] for v in src.sample([tuple(p) for p in xy])])
    ok = alpha > 0
    assert np.median(np.abs(colours[ok] - ortho[ok]).mean(axis=1)) < 12  # every page in its place
    # the 3D view: the pages side by side in one image, the coordinates moved into it
    sd = build_scene(ch, "model_textured")
    assert sd.texture.name == "model_texture_display.jpg" and len(sd.mesh_uv) == 3 * len(mesh.faces)
    # exports: an OBJ with a material per page, a PLY with the pages and texnumber, a GLB with a mesh each
    obj = ch.export_model(tmp_path / "out" / "modello.obj")
    text = obj.read_text()
    assert text.count("usemtl material") == len(pages) and (tmp_path / "out" / "modello_texture_1.jpg").exists()
    assert obj.with_suffix(".mtl").read_text().count("map_Kd") == len(pages)
    ply = read_mesh(ch.export_model(tmp_path / "out" / "modello.ply"))
    assert ply.texture_files[1] == "modello_texture_1.jpg" and ply.texnumber is not None
    from pyarchinit_photogrammetry.io.web import export_glb

    scene = trimesh.load(export_glb(ch, tmp_path / "out" / "modello.glb"))
    assert len(scene.geometry) == len(pages)
    # the versions keep every page; a new, single-page texture removes the old pages
    entry = versions.keep_active(ch, "model")
    assert sum(1 for role in entry["files"] if role.startswith("texture")) == len(pages)
    versions.restore(ch, "model", entry)
    assert all(p.exists() for p in texture_pages(ch))
    ch.build_texture(texture_size=1024, mapping="generic", page_size=512)
    assert len(texture_pages(ch)) == 4  # 1024 px over pages of 512: four groups of faces
    xy, colours = _page_colours(ch, seed=1)
    assert colours.std() > 10
    ch.build_texture(texture_size=512, mapping="generic")
    assert texture_pages(ch) == [ch.data_dir / "model_texture.jpg"] and not (ch.data_dir / "model_texture_1.jpg").exists()
