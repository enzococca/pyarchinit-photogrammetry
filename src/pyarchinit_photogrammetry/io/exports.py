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
    """Export the mesh in world coordinates. ``.ply`` keeps every vertex property (colours,
    confidence) with double-precision coordinates (UTM and other map coordinates lose
    centimetres in single precision) and the texture coordinates, with the texture next to it;
    ``.obj`` keeps the texture. ``region`` (a polygon in the chunk CRS, see
    :func:`engine.crop.as_polygon`) and ``zmin``/``zmax``: only the faces inside."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cropping = region is not None or zmin is not None or zmax is not None
    textured = chunk.product_path("model_textured")
    legacy = textured is not None and textured.suffix.lower() == ".obj" and textured.exists()
    if path.suffix.lower() == ".obj" and legacy:  # a texture of version 0.5 and earlier: its own OBJ
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
    from .mesh_ply import read_mesh

    mesh = read_mesh(model_path)
    if cropping:
        from ..engine.crop import faces_inside

        keep = faces_inside(chunk, mesh.xyz, mesh.faces, _region(region), zmin, zmax)
        if not keep.any():
            raise ValueError("no face of the model lies inside the region")
        mesh = mesh.subset_faces(keep)
    return export_mesh(chunk, mesh, path)


def export_mesh(chunk, mesh, path) -> Path:
    """A mesh in the internal frame of ``chunk`` (the model, a part of it) written in world
    coordinates as :func:`export_model` writes the model: ``.ply`` with every vertex property in
    double precision and the texture pages the faces use, ``.obj`` with the texture, other formats
    through trimesh with the vertex colours."""
    from .mesh_ply import write_mesh

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    textured = chunk.product_path("model_textured")
    legacy = textured is not None and textured.suffix.lower() == ".obj"
    from ..engine.texturing import texture_pages

    pages = texture_pages(chunk) if textured is not None and not legacy and textured.exists() else []
    if mesh.texcoords is None:
        pages = []
    if len(pages) > 1:  # only the pages the faces use, numbered again
        used = np.unique(mesh.texnumber) if mesh.texnumber is not None else np.array([0])
        renumber = np.full(len(pages), -1, np.int32)
        renumber[used] = np.arange(len(used))
        mesh.texnumber = renumber[mesh.texnumber] if mesh.texnumber is not None else None
        pages = [pages[k] for k in used]
    names = [path.stem + "_texture" + ("" if k == 0 else f"_{k}") + p.suffix for k, p in enumerate(pages)]
    world = mesh.xyz @ chunk.transform[:3, :3].T + chunk.transform[:3, 3]
    if path.suffix.lower() == ".ply":
        out = mesh.copy()
        out.set_xyz(world, double=True)
        if pages:
            for page, name in zip(pages, names):
                shutil.copy2(page, path.with_name(name))
            out.set_texture_files(names)
            if len(pages) == 1:
                out.texnumber = None
        else:
            out.texcoords = None
            out.set_texture_file(None)
        return write_mesh(path, out)
    if path.suffix.lower() == ".obj" and pages:
        from ..engine.texturing import write_obj

        write_obj(path, mesh.xyz, mesh.faces, mesh.texcoords.reshape(-1, 3, 2), names if len(names) > 1 else names[0],
                  transform=chunk.transform, pages=mesh.texnumber if len(names) > 1 else None)
        for page, name in zip(pages, names):
            shutil.copy2(page, path.with_name(name))
        return path
    import trimesh

    colors = mesh.colors
    out = trimesh.Trimesh(world, mesh.faces, vertex_colors=colors, process=False)
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
    coordinates with omega/phi/kappa; ``xlsx``: the same as an Excel workbook."""
    path = Path(path)
    if fmt == "colmap":
        from ..engine.sfm import load_reconstruction

        path.mkdir(parents=True, exist_ok=True)
        load_reconstruction(chunk).write_text(str(path))
        return path
    if fmt in ("csv", "xlsx"):
        _, rot, _ = geometry.decompose_similarity(chunk.transform)
        header = ["label", "x", "y", "z", "omega", "phi", "kappa", "sensor", "path"]
        rows = []
        for cam in chunk.aligned_cameras:
            c = chunk.to_world(cam.center)
            o, p, k = opk_from_rotation(rot @ cam.transform[:3, :3])
            rows.append([cam.label, *(round(float(v), 4) for v in c), round(o, 5), round(p, 5), round(k, 5),
                         chunk.sensor(cam).label, cam.path])
        if fmt == "xlsx":
            from .tables import Sheet, write_xlsx

            return write_xlsx(path, {"cameras": Sheet(header, rows, [None, 4, 4, 4, 5, 5, 5, None, None])})
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(rows)
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
