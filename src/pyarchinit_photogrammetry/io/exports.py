"""Exports of chunk products in world (CRS) coordinates."""
from __future__ import annotations

import csv
import shutil
from pathlib import Path

import numpy as np

from ..core import geometry


def _subset(verts: np.ndarray, faces: np.ndarray, keep: np.ndarray):
    """Faces ``keep`` and only the vertices they use: (vertices, faces, used vertex mask)."""
    faces = faces[keep]
    used = np.zeros(len(verts), bool)
    used[faces.ravel()] = True
    remap = np.cumsum(used) - 1
    return verts[used], remap[faces], used


def export_model(chunk, path, region=None, zmin: float | None = None, zmax: float | None = None) -> Path:
    """Export the mesh in world coordinates. ``.obj`` keeps the texture when built; ``.ply``
    keeps the vertex colours and confidence, with double-precision coordinates (UTM and other
    map coordinates lose centimetres in single precision). ``region`` (a polygon in the chunk
    CRS, see :func:`engine.crop.as_polygon`) and ``zmin``/``zmax``: only the faces inside."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cropping = region is not None or zmin is not None or zmax is not None
    textured = chunk.product_path("model_textured")
    if path.suffix.lower() == ".obj" and textured is not None and textured.exists():
        from ..engine.texturing import write_obj

        verts, faces, uv = _read_textured_obj(textured)
        if cropping:
            from ..engine.crop import faces_inside

            keep = faces_inside(chunk, verts, faces, _region(region), zmin, zmax)
            if not keep.any():
                raise ValueError("no face of the model lies inside the region")
            uv = None if uv is None else uv[keep]
            verts, faces, _ = _subset(verts, faces, keep)
        write_obj(path, verts, faces, uv, path.stem + "_texture.jpg", transform=chunk.transform)
        shutil.copy2(textured.parent / "texture.jpg", path.with_name(path.stem + "_texture.jpg"))
        return path
    model_path = chunk.product_path("model")
    if model_path is None or not model_path.exists():
        raise RuntimeError("no model in chunk")
    import trimesh

    from ..engine.confidence import read_model_confidence

    mesh = trimesh.load(str(model_path), process=False, force="mesh")
    verts, faces = np.asarray(mesh.vertices, np.float64), np.asarray(mesh.faces, np.int64)
    colors = np.asarray(mesh.visual.vertex_colors, np.uint8)[:, :3] if mesh.visual.kind == "vertex" else None
    conf = read_model_confidence(model_path)
    if cropping:
        from ..engine.crop import faces_inside

        keep = faces_inside(chunk, verts, faces, _region(region), zmin, zmax)
        if not keep.any():
            raise ValueError("no face of the model lies inside the region")
        verts, faces, used = _subset(verts, faces, keep)
        colors = None if colors is None else colors[used]
        conf = None if conf is None else conf[used]
    world = verts @ chunk.transform[:3, :3].T + chunk.transform[:3, 3]
    if path.suffix.lower() == ".ply":
        from .mesh_ply import write_mesh_ply

        return write_mesh_ply(path, world, faces, colors, double=True,
                              extra={"confidence": conf} if conf is not None else None)
    out = trimesh.Trimesh(world, faces, vertex_colors=colors, process=False)
    out.export(str(path))
    return path


def _region(region):
    if region is None:
        return None
    from ..engine.crop import as_polygon

    return as_polygon(region)


def _read_textured_obj(path: Path):
    v, vt, f = [], [], []
    with open(path) as fh:
        for line in fh:
            if line.startswith("v "):
                v.append(line.split()[1:4])
            elif line.startswith("vt "):
                vt.append(line.split()[1:3])
            elif line.startswith("f "):
                f.append([int(p.split("/")[0]) - 1 for p in line.split()[1:4]])
    verts = np.asarray(v, float)
    faces = np.asarray(f, np.int64)
    uv = np.asarray(vt, float).reshape(-1, 3, 2) if vt else None
    return verts, faces, uv


def opk_from_rotation(r_world_from_cam: np.ndarray) -> tuple[float, float, float]:
    """Omega, phi, kappa (degrees) of a photogrammetric rotation (camera looking along -Z)."""
    # convert OpenCV camera axes (+Z forward, +Y down) to photogrammetric (+Z back, +Y up)
    flip = np.diag([1.0, -1.0, -1.0])
    m = (r_world_from_cam @ flip).T
    phi = np.arcsin(np.clip(m[2, 0], -1, 1))
    omega = np.arctan2(-m[2, 1], m[2, 2])
    kappa = np.arctan2(-m[1, 0], m[0, 0])
    return tuple(float(np.degrees(a)) for a in (omega, phi, kappa))


def export_cameras(chunk, path, fmt: str = "colmap") -> Path:
    """``colmap``: COLMAP text model (internal frame); ``csv``: camera centres in world
    coordinates with omega/phi/kappa."""
    path = Path(path)
    if fmt == "colmap":
        from ..engine.sfm import load_reconstruction

        path.mkdir(parents=True, exist_ok=True)
        load_reconstruction(chunk).write_text(str(path))
        return path
    if fmt == "csv":
        _, rot, _ = geometry.decompose_similarity(chunk.transform)
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["label", "x", "y", "z", "omega", "phi", "kappa", "sensor", "path"])
            for cam in chunk.aligned_cameras:
                c = chunk.to_world(cam.center)
                o, p, k = opk_from_rotation(rot @ cam.transform[:3, :3])
                w.writerow([cam.label, *np.round(c, 4), round(o, 5), round(p, 5), round(k, 5),
                            chunk.sensor(cam).label, cam.path])
        return path
    raise ValueError(f"unknown camera export format {fmt!r}")


def copy_raster(src: Path | None, dst) -> Path:
    if src is None or not Path(src).exists():
        raise RuntimeError("raster not built yet")
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.suffix.lower() in (".tif", ".tiff"):
        shutil.copy2(src, dst)
        return dst
    import rasterio
    from rasterio.shutil import copy as rio_copy

    driver = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".asc": "AAIGrid", ".gpkg": "GPKG"}.get(dst.suffix.lower())
    if driver is None:
        raise ValueError(f"unsupported raster format {dst.suffix}")
    with rasterio.open(src) as s:
        rio_copy(s, dst, driver=driver)
    return dst
