"""Synthetic textured models for the tests of merged models, orthophotos from the texture and OBJ groups: flat or
gently curved patches on a grid, textured with a known image, in a local frame (no real place)."""
from __future__ import annotations

import cv2
import numpy as np

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.io.mesh_ply import MeshData, write_mesh


def checker(width: int, height: int, colour=(180, 120, 60), cells: int = 8) -> np.ndarray:
    """A BGR image: a checkerboard of ``colour`` (RGB) and its half."""
    yy, xx = np.mgrid[0:height, 0:width]
    on = ((xx * cells // width) + (yy * cells // height)) % 2 == 0
    rgb = np.where(on[..., None], np.asarray(colour, np.uint8), (np.asarray(colour) // 2).astype(np.uint8))
    return np.ascontiguousarray(rgb[:, :, ::-1])


def grid_mesh(x0, y0, x1, y1, nx, ny, z=lambda x, y: 0.0 * x):
    """A height field over ``[x0, x1] x [y0, y1]`` with ``nx`` x ``ny`` cells: vertices (world) and faces, and the
    texture coordinates of the face corners mapping the box on the whole page (u along x, v along y)."""
    xs, ys = np.linspace(x0, x1, nx + 1), np.linspace(y0, y1, ny + 1)
    X, Y = np.meshgrid(xs, ys)
    xyz = np.column_stack([X.ravel(), Y.ravel(), np.asarray(z(X, Y), float).ravel()])
    idx = np.arange((nx + 1) * (ny + 1)).reshape(ny + 1, nx + 1)
    a, b, c, d = idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel(), idx[1:, 1:].ravel(), idx[1:, :-1].ravel()
    faces = np.vstack([np.column_stack([a, b, c]), np.column_stack([a, c, d])]).astype(np.int64)
    u = (xyz[:, 0] - x0) / (x1 - x0)
    v = (xyz[:, 1] - y0) / (y1 - y0)
    uv = np.stack([u[faces], v[faces]], -1).reshape(-1, 6).astype(np.float32)
    return xyz, faces, uv


def textured_chunk(doc, label, xyz_world, faces, uv, pages, texnumber=None, shift=(0.0, 0.0, 0.0), crs="LOCAL"):
    """A chunk without photos whose model is the mesh (world coordinates, internal = world - ``shift``) with the
    texture ``pages`` (BGR arrays)."""
    ch = doc.add_chunk(label)
    ch.crs = crs
    t = np.eye(4)
    t[:3, 3] = shift
    ch.transform = t
    ch.meta["metric"] = True
    internal = ch.to_internal(np.asarray(xyz_world, float))
    v = np.empty(len(internal), [("x", "<f4"), ("y", "<f4"), ("z", "<f4"), ("red", "u1"), ("green", "u1"),
                                 ("blue", "u1")])
    v["x"], v["y"], v["z"] = internal.T
    v["red"], v["green"], v["blue"] = 128, 128, 128
    names = ["model_texture.jpg" if k == 0 else f"model_texture_{k}.jpg" for k in range(len(pages))]
    for img, name in zip(pages, names):
        cv2.imwrite(str(ch.data_dir / name), img, [cv2.IMWRITE_JPEG_QUALITY, 98])
    mesh = MeshData(v, np.asarray(faces, np.int64), np.asarray(uv, np.float32), [],
                    None if len(pages) == 1 else np.asarray(texnumber, np.int32))
    if len(pages) > 1:
        mesh.set_texture_files(names)
    else:
        mesh.set_texture_file(names[0])
    ch._set_product("model", write_mesh(ch.own_product_path("model"), mesh))
    ch._set_product("model_textured", ch.data_dir / names[0])
    return ch


def new_doc(tmp_path, name="p.ppg"):
    doc = ppg.Document()
    doc.save(tmp_path / name)
    return doc
