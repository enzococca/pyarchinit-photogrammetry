"""Exports of chunk products in world (CRS) coordinates."""
from __future__ import annotations

import csv
import shutil
from pathlib import Path

import numpy as np

from ..core import geometry


def export_model(chunk, path) -> Path:
    """Export the mesh in world coordinates. ``.obj`` keeps the texture when built."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    textured = chunk.product_path("model_textured")
    if path.suffix.lower() == ".obj" and textured is not None and textured.exists():
        from ..engine.texturing import write_obj

        verts, faces, uv = _read_textured_obj(textured)
        write_obj(path, verts, faces, uv, path.stem + "_texture.jpg", transform=chunk.transform)
        shutil.copy2(textured.parent / "texture.jpg", path.with_name(path.stem + "_texture.jpg"))
        return path
    mesh = chunk.model
    if mesh is None:
        raise RuntimeError("no model in chunk")
    mesh.apply_transform(chunk.transform)
    mesh.export(str(path))
    return path


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
