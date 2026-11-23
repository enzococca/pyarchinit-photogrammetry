"""Helpers shared by the tests of the walls traced on the plan (test_wall_scene, test_wall_elevation and the tests
of the tasks after them): documents made of the synthetic wall (``testing.synthetic.make_wall_scene``) - its photos
with their known poses, or its model alone - a plan written as the products of a chunk, and what the tests measure
on an orthophoto or on a line. Plain functions, no fixtures (as tests/elevation_helpers.py); the datasets are the
session fixtures ``wall_dataset`` and ``wall_dataset_inside`` of conftest.py."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np

SHIFT = (500000.0, 4400000.0, 100.0)  # the local metres of the scene in a projected system
CRS = "EPSG:32633"


def _chunk(doc, label: str, crs: str, shift):
    """A chunk whose internal frame is the local frame of the scene, moved by ``shift`` into ``crs``, in metres."""
    ch = doc.add_chunk(label)
    ch.crs = crs
    t = np.eye(4)
    t[:3, 3] = shift
    ch.transform = t
    ch.meta["metric"] = True
    return ch


def _in_world(info: dict, shift) -> dict:
    """``info`` (``face``, ``marks``... in the local metres of the scene) with ``face`` and ``marks`` moved by
    ``shift``, and ``shift`` itself."""
    shift = np.asarray(shift, np.float64)
    out = dict(info)
    out["face"] = (np.asarray(info["face"], np.float64) + shift[:2]).tolist()
    out["marks"] = [(float(length), float(z), tuple(float(v) for v in np.asarray(xyz, np.float64) + shift))
                    for length, z, xyz in info["marks"]]
    out["shift"] = [float(v) for v in shift]
    return out


def scene_of(info: dict):
    """The scene of a dataset from its ``poses.json`` (the local one: the default texture and seed)."""
    from pyarchinit_photogrammetry.testing.synthetic import make_wall_scene

    return make_wall_scene(trace=info["face"], viewer=info["viewer"], height=info["height"],
                           thickness=info["thickness"], marks=[(m[0], m[1]) for m in info["marks"]])


def write_plan(chunk, dem, rgb, top_left, cell: float):
    """A DEM and an orthophoto as the products of ``chunk``: ``dem`` (h, w), ``rgb`` (3, h, w) and ``top_left`` as
    :func:`testing.synthetic.wall_plan` gives them, in the local metres of the scene, written where the chunk's
    transform (a translation) puts them."""
    from pyarchinit_photogrammetry.engine.dem import NODATA, write_geotiff

    t = np.asarray(chunk.transform, np.float64)
    origin = (float(top_left[0]) + t[0, 3], float(top_left[1]) + t[1, 3])
    z = (np.asarray(dem, np.float64) + t[2, 3]).astype(np.float32)
    rgba = np.concatenate([np.asarray(rgb, np.uint8), np.full((1, *z.shape), 255, np.uint8)])
    chunk._set_product("dem", write_geotiff(chunk.data_dir / "dem.tif", z, origin, cell, chunk.crs, nodata=NODATA))
    chunk._set_product("orthomosaic", write_geotiff(chunk.data_dir / "orthomosaic.tif", rgba, origin, cell,
                                                    chunk.crs))


def wall_doc(folder, dataset, chunks: int = 1, crs: str = CRS, shift=SHIFT, plan: bool = False, dates=None):
    """A saved document in ``folder`` with ``chunks`` chunks of the wall dataset ``dataset`` (a folder written by
    ``write_wall_dataset``): each with the photos of the dataset - a PINHOLE sensor, every photo at its pose -
    its model, the scene moved by ``shift`` into ``crs``, in metres; ``plan``: the DEM and the orthophoto of the
    plan of the scene too; ``dates``: the date of the photos of each chunk (``"2017:11:05 10:00:00"``). Returns
    ``(doc, [chunks], info)``, ``info`` the ``poses.json`` of the dataset with ``face`` and ``marks`` in the
    coordinates of the chunks (and ``shift``)."""
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.testing.synthetic import wall_plan

    folder, dataset = Path(folder), Path(dataset)
    folder.mkdir(parents=True, exist_ok=True)
    local = json.loads((dataset / "poses.json").read_text())
    doc = ppg.Document()
    made = [_chunk(doc, f"Muro {i + 1}", crs, shift) for i in range(chunks)]
    doc.save(folder / "wall.ppg")
    w, h = local["size"]
    for i, ch in enumerate(made):
        cams = ch.add_photos([dataset / "photos"])
        sensor = ch.sensor(cams[0])
        sensor.model, sensor.params = "PINHOLE", [local["focal"], local["focal"], w / 2, h / 2]
        for cam, pose in zip(sorted(cams, key=lambda c: c.label), local["poses"]):
            cam.transform = np.array(pose, np.float64)
            if dates is not None:
                cam.meta["datetime"] = dates[i]
        shutil.copy2(dataset / "model.ply", ch.data_dir / "model.ply")
        ch._set_product("model", ch.data_dir / "model.ply")
        if plan:
            write_plan(ch, *wall_plan(scene_of(local), 0.02), 0.02)
    doc.save()
    return doc, made, _in_world(local, shift)


def model_doc(folder, scene, crs: str = CRS, shift=SHIFT, drop=None):
    """A saved document in ``folder`` with one chunk that has the model of ``scene`` and nothing else (no photo: an
    elevation of it is its relief alone), moved by ``shift`` into ``crs``; ``drop``: ``(l0, l1)``, the triangles
    of the face between those face lengths left out (a hole in the wall). Returns ``(doc, chunk, info)``, ``info``
    with ``face``, ``viewer``, ``height``, ``thickness`` and ``marks`` in the coordinates of the chunk."""
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.io.mesh_ply import MeshData, write_mesh

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    doc = ppg.Document()
    ch = _chunk(doc, "Muro", crs, shift)
    doc.save(folder / "model.ppg")
    faces = np.asarray(scene.faces, np.int64)
    if drop is not None:
        column = scene.face_columns
        faces = faces[~((column >= drop[0]) & (column < drop[1]))]
    v = np.empty(len(scene.vertices), [("x", "<f4"), ("y", "<f4"), ("z", "<f4")])
    v["x"], v["y"], v["z"] = np.asarray(scene.vertices, np.float64).T
    ch._set_product("model", write_mesh(ch.data_dir / "model.ply", MeshData(v, faces)))
    doc.save()
    info = {"face": scene.face.tolist(), "viewer": scene.viewer, "height": scene.height,
            "thickness": scene.thickness, "marks": [[length, z, list(xyz)] for length, z, xyz in scene.marks]}
    return doc, ch, _in_world(info, shift)


def dark_blobs(rgba, level: int = 40, min_cells: int = 20) -> list[tuple[float, float]]:
    """The centroids ``(row, col)`` of the dark blobs of an orthophoto ``rgba`` (4, h, w): the groups of ``min_cells``
    cells or more that have alpha and all three channels under ``level``."""
    from scipy import ndimage

    rgba = np.asarray(rgba)
    dark = (rgba[3] > 0) & (rgba[:3].max(axis=0) < level)
    labels, n = ndimage.label(dark)
    sizes = ndimage.sum(dark, labels, np.arange(1, n + 1)) if n else []
    keep = [i + 1 for i, size in enumerate(sizes) if size >= min_cells]
    return [(float(r), float(c)) for r, c in ndimage.center_of_mass(dark, labels, keep)]


def distance_to(line_xy, points_xy) -> np.ndarray:
    """The distance of each of the plan points (M, 2) from the polyline ``line_xy`` (N, 2)."""
    line = np.asarray(line_xy, np.float64).reshape(-1, 2)
    p = np.asarray(points_xy, np.float64).reshape(-1, 2)
    best = np.full(len(p), np.inf)
    for a, b in zip(line[:-1], line[1:]):
        step = b - a
        tau = np.clip((p - a) @ step / max(float(step @ step), 1e-300), 0.0, 1.0)
        best = np.minimum(best, np.hypot(*(p - a - tau[:, None] * step).T))
    return best


def share_within(line_xy, truth_xy, tolerance: float) -> float:
    """The share of the length of the polyline ``line_xy``, sampled every 2 cm, that lies within ``tolerance`` of
    the polyline ``truth_xy``."""
    line = np.asarray(line_xy, np.float64).reshape(-1, 2)
    run = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(line, axis=0).T))])
    at = np.linspace(0.0, run[-1], max(2, int(np.ceil(run[-1] / 0.02)) + 1))
    points = np.column_stack([np.interp(at, run, line[:, 0]), np.interp(at, run, line[:, 1])])
    return float((distance_to(truth_xy, points) <= tolerance).mean())
