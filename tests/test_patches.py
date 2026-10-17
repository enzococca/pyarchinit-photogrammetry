"""Patches of the orthomosaic and of the texture: inside a polygon the colours come from the
photos chosen by hand. Every photo is replaced by a plain colour of its own, so the colour of a
pixel tells which photo it comes from."""
from __future__ import annotations

import hashlib
import shutil

import numpy as np
import pytest
import rasterio

from pyarchinit_photogrammetry.engine import patches as P


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


def _colour_of(key: int) -> tuple[int, int, int]:
    """A colour (BGR) of its own for every photo."""
    rng = np.random.default_rng(1000 + int(key))
    return tuple(int(v) for v in rng.integers(30, 226, 3))


@pytest.fixture
def plain_photos(monkeypatch):
    """Every photo read by the engines is a plain colour of its own."""
    from pyarchinit_photogrammetry.engine import ortho, visibility

    def fake(cam, flags=None):
        s = cam._chunk_sensor
        return np.full((s.height, s.width, 3), _colour_of(cam.key), np.uint8)

    monkeypatch.setattr(ortho, "read_photo", fake)
    monkeypatch.setattr(visibility, "read_photo", fake)
    return fake


def _prepare(ch):
    for cam in ch.cameras:
        cam._chunk_sensor = ch.sensor(cam)


def _read(path):
    with rasterio.open(path) as src:
        return src.read(), src.transform


def _cells(transform, shape, geom, inside: bool, margin: float):
    """Cells whose centre is inside ``geom`` shrunk (or outside it grown) by ``margin``."""
    from shapely import contains_xy

    rows, cols = np.mgrid[0:shape[0], 0:shape[1]]
    x = transform.c + (cols + 0.5) * transform.a
    y = transform.f + (rows + 0.5) * transform.e
    if inside:
        return contains_xy(geom.buffer(-margin), x, y)
    return ~contains_xy(geom.buffer(margin), x, y)


def _area(ch, frac=0.3):
    with rasterio.open(ch.orthomosaic_path) as src:
        b = src.bounds
    cx, cy = (b.left + b.right) / 2, (b.bottom + b.top) / 2
    hw, hh = frac * (b.right - b.left) / 2, frac * (b.top - b.bottom) / 2
    return [[cx - hw, cy - hh], [cx + hw, cy - hh], [cx + hw * 0.6, cy + hh], [cx - hw, cy + hh * 0.8]]


def test_assigned_photo_inside_the_polygon_only(processed, tmp_path, plain_photos):
    from shapely.geometry import Polygon

    _doc, ch = _copy(processed, tmp_path)
    _prepare(ch)
    ch.build_orthomosaic(resolution=0.01)
    before, tr = _read(ch.orthomosaic_path)
    poly = _area(ch)
    ranked = P.photos_for_area(ch, poly)
    assert ranked and ranked == sorted(ranked, key=lambda d: (-round(d["coverage"], 1), -d["score"]))
    assert abs(sum(d["used"] for d in ranked) - 1.0) < 0.05  # the photos the orthomosaic uses now
    # a photo that sees most of it but is hardly used there now (the walls of the trench hide
    # a part of the area from every photo)
    pick = max((d for d in ranked if d["used"] < 0.2), key=lambda d: d["coverage"])
    assert pick["coverage"] > 0.8
    patch = P.add_patch(ch, poly, [pick["label"]])
    assert patch["id"] == "p1" and P.changed_patches(ch) == [patch]
    P.update_orthomosaic(ch)
    after, _ = _read(ch.orthomosaic_path)
    geom = Polygon(poly)
    inside = _cells(tr, after.shape[1:], geom, True, 0.02) & (after[3] == 255)
    assert inside.sum() > 500
    want = _colour_of(ch.camera(pick["label"]).key)  # BGR
    got = after[:3, inside].T[:, ::-1]
    assigned = np.abs(got.astype(int) - want).max(axis=1) <= 1
    assert assigned.mean() > 0.75
    # the cells that photo does not see are made as without the patch
    assert np.array_equal(after[:, inside][:, ~assigned], before[:, inside][:, ~assigned])
    outside = _cells(tr, after.shape[1:], geom, False, 0.02)
    assert np.array_equal(after[:, outside], before[:, outside])  # the rest of the file as it was
    assert P.changed_patches(ch) == []
    # a new build applies the patch the same way, pixel for pixel
    rebuilt, _ = _read(ch.build_orthomosaic(resolution=0.01))
    assert np.array_equal(rebuilt, after)
    # taking the patch away gives the orthomosaic back
    P.remove_patch(ch, "p1")
    P.update_orthomosaic(ch)
    again, _ = _read(ch.orthomosaic_path)
    assert np.array_equal(again, before)


def test_excluded_photo_is_left_out(processed, tmp_path, plain_photos):
    from shapely.geometry import Polygon

    _doc, ch = _copy(processed, tmp_path)
    _prepare(ch)
    ch.build_orthomosaic(resolution=0.01)
    before, tr = _read(ch.orthomosaic_path)
    poly = _area(ch, 0.4)
    most = max(P.photos_for_area(ch, poly), key=lambda d: d["used"])
    assert most["used"] > 0.05
    colour = np.array(_colour_of(ch.camera(most["label"]).key))
    geom = Polygon(poly)
    inside = _cells(tr, before.shape[1:], geom, True, 0.02) & (before[3] == 255)

    def share(img):
        return float((np.abs(img[:3, inside].T[:, ::-1].astype(int) - colour).max(axis=1) <= 1).mean())

    assert share(before) > 0.05
    P.add_patch(ch, poly, [most["label"]], mode="exclude")
    P.update_orthomosaic(ch)
    after, _ = _read(ch.orthomosaic_path)
    assert share(after) == 0.0
    assert (after[3, inside] == 255).mean() > 0.99  # the other photos fill it
    outside = _cells(tr, after.shape[1:], geom, False, 0.02)
    assert np.array_equal(after[:, outside], before[:, outside])


def test_orthomosaic_of_another_program_is_never_changed(processed, tmp_path, plain_photos):
    from shapely.geometry import Polygon

    _doc, ch = _copy(processed, tmp_path)
    _prepare(ch)
    ch.build_orthomosaic(resolution=0.01)
    other = tmp_path / "metashape" / "ortofoto.tif"
    other.parent.mkdir()
    shutil.move(str(ch.orthomosaic_path), other)
    ch._set_product("orthomosaic", other)
    ch.meta["orthomosaic"] = {}  # as imported: nothing about how it was made
    digest = hashlib.sha256(other.read_bytes()).hexdigest()
    original, tr = _read(other)
    poly = _area(ch)
    pick = P.photos_for_area(ch, poly)[0]
    P.add_patch(ch, poly, [pick["label"]])
    path = P.update_orthomosaic(ch)
    assert path == ch.own_product_path("orthomosaic") == ch.orthomosaic_path
    assert hashlib.sha256(other.read_bytes()).hexdigest() == digest
    after, _ = _read(path)
    outside = _cells(tr, after.shape[1:], Polygon(poly), False, 0.02)
    assert np.array_equal(after[:, outside], original[:, outside])
    assert not np.array_equal(after, original)
    # without the patch: the pixels of the other program again, everywhere
    P.clear_patches(ch)
    P.update_orthomosaic(ch)
    restored, _ = _read(path)
    assert np.array_equal(restored, original)
    assert hashlib.sha256(other.read_bytes()).hexdigest() == digest


def test_preview_from_one_photo(processed, tmp_path, plain_photos):
    _doc, ch = _copy(processed, tmp_path)
    _prepare(ch)
    poly = _area(ch, 0.2)
    pick = P.photos_for_area(ch, poly)[0]
    rgba, bounds = P.preview_area(ch, poly, [pick["label"]], max_side=120)
    assert max(rgba.shape[:2]) == 120 and bounds[0] < poly[0][0]
    seen = rgba[..., 3] == 255
    assert seen.mean() > 0.5
    want = _colour_of(ch.camera(pick["label"]).key)[::-1]  # RGB
    assert (np.abs(rgba[seen, :3].astype(int) - want).max(axis=1) <= 1).mean() > 0.99


def test_texture_faces_from_the_chosen_photo(processed, tmp_path, plain_photos):
    import cv2

    from pyarchinit_photogrammetry.io.mesh_ply import read_mesh

    _doc, ch = _copy(processed, tmp_path)
    _prepare(ch)
    tex = ch.product_path("model_textured")
    before = cv2.imread(str(tex))
    poly = _area(ch, 0.3)
    inside = P.faces_in_polygon(ch, poly)
    mesh = read_mesh(ch.product_path("model"))
    tri = ch.to_world(mesh.xyz)[mesh.faces[inside]]
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    faces = inside[n[:, 2] / np.linalg.norm(n, axis=1) > 0.5]  # the floor of the trench, seen from above
    assert len(faces) > 100
    ranked = P.photos_for_faces(ch, faces)
    picks = [d["label"] for d in ranked[:2]]
    assert ranked[0]["coverage"] > 0.8
    result = P.retexture(ch, picks, faces=faces)
    assert result["faces"] == len(faces) and result["coloured"] > 0.8 * len(faces)  # walls hide the rest
    after = cv2.imread(str(ch.product_path("model_textured")))
    h, w = after.shape[:2]

    def texels(face_ids):
        uv = mesh.texcoords[face_ids].reshape(-1, 3, 2).astype(float)
        tri = np.stack([uv[..., 0] * w, (1 - uv[..., 1]) * h], -1)
        _f, cols, rows, _b = P._texels(tri, w, h, margin=-0.75)  # well inside the faces
        return rows, cols

    rows, cols = texels(faces)
    colours = np.array([_colour_of(ch.camera(label).key) for label in picks])
    dist = np.abs(after[rows, cols].astype(int)[:, None, :] - colours[None]).max(axis=2).min(axis=1)
    assert (dist <= 8).mean() > 0.8 * result["coloured"] / len(faces)  # one of the chosen photos (JPEG)
    others = np.setdiff1d(np.arange(len(mesh.faces)), faces)[::50]
    rows, cols = texels(others)
    assert np.abs(after[rows, cols].astype(int) - before[rows, cols].astype(int)).mean() < 3
    assert ch.meta["texture"]["patches"][-1]["photos"] == sorted(picks)
