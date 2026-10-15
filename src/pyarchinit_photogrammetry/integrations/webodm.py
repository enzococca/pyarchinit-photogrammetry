"""Import WebODM / ODM results (the task folder or the ``all.zip`` download).

ODM writes, inside the task folder:

* ``odm_orthophoto/odm_orthophoto.tif``          orthomosaic (GeoTIFF)
* ``odm_dem/dsm.tif`` and/or ``odm_dem/dtm.tif``   elevation models (GeoTIFF)
* ``odm_georeferencing/odm_georeferenced_model.laz`` (or ``.las``/``.ply``)  point cloud, world coordinates
* ``odm_georeferencing/coords.txt``              CRS (``WGS84 UTM 33N`` or a proj string) and the offset
* ``odm_texturing/odm_textured_model_geo.obj``    textured mesh, coordinates relative to that offset
* ``images/`` (only in the task folder, not in ``all.zip``)

The chunk uses the orthophoto/DEM CRS; its internal frame is the world shifted by the ODM
offset (small numbers), so the mesh is used as is. Camera poses are not imported: the
products are what sections, contours, volumes, PyArchInit publishing, IFC and QGIS use.
"""
from __future__ import annotations

import os
import re
import zipfile
from pathlib import Path

import numpy as np


def _crs_from_coords(line: str) -> str | None:
    m = re.match(r"WGS84 UTM (\d+)([NS])", line.strip(), re.I)
    if m:
        return f"EPSG:{(32600 if m.group(2).upper() == 'N' else 32700) + int(m.group(1))}"
    if line.strip().startswith("+proj") or line.strip().upper().startswith("EPSG:"):
        from pyproj import CRS

        epsg = CRS.from_user_input(line.strip()).to_epsg()
        return f"EPSG:{epsg}" if epsg else None
    return None


def read_task(folder: str | os.PathLike) -> dict:
    """Paths, CRS and offset of an ODM task folder."""
    folder = Path(folder)

    def first(*rel):
        for r in rel:
            p = folder / r
            if p.exists():
                return p
        return None

    info = {
        "folder": folder,
        "orthomosaic": first("odm_orthophoto/odm_orthophoto.tif"),
        "dem": first("odm_dem/dsm.tif", "odm_dem/dtm.tif"),
        "dtm": first("odm_dem/dtm.tif"),
        "cloud": first("odm_georeferencing/odm_georeferenced_model.laz", "odm_georeferencing/odm_georeferenced_model.las",
                       "odm_georeferencing/odm_georeferenced_model.ply", "odm_filterpoints/point_cloud.ply"),
        "model": first("odm_texturing/odm_textured_model_geo.obj", "odm_texturing_25d/odm_textured_model_geo.obj",
                       "odm_meshing/odm_mesh.ply"),
        "images": first("images"),
        "crs": None, "offset": np.zeros(3),
    }
    coords = first("odm_georeferencing/coords.txt")
    if coords:
        lines = [ln.strip() for ln in coords.read_text().splitlines() if ln.strip()]
        if lines:
            info["crs"] = _crs_from_coords(lines[0])
        if len(lines) > 1:
            e, n = (float(v) for v in lines[1].split()[:2])
            info["offset"] = np.array([e, n, 0.0])
    for key in ("orthomosaic", "dem"):  # the CRS the rasters are written in wins over coords.txt
        if info[key] is not None:
            import rasterio

            with rasterio.open(info[key]) as src:
                if src.crs is not None and src.crs.to_epsg():
                    info["crs"] = f"EPSG:{src.crs.to_epsg()}"
                    break
    if not any(info[k] for k in ("orthomosaic", "dem", "cloud", "model")):
        raise FileNotFoundError(f"{folder} does not look like an ODM/WebODM task (no odm_* products)")
    return info


def _extract(zip_path: Path, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        for m in z.infolist():
            target = (dest / m.filename).resolve()
            if not str(target).startswith(str(dest.resolve())):  # zip slip
                raise ValueError(f"unsafe path in {zip_path}: {m.filename}")
        z.extractall(dest)
    return dest


def import_task(source: str | os.PathLike, project: str | os.PathLike, label: str | None = None, progress=None):
    """A ``.ppg`` project with the products of a WebODM/ODM task (folder or ``all.zip``)."""
    from ..core.document import Document
    from ..core.pointcloud import PointCloud
    from ..core.progress import as_progress

    progress = as_progress(progress)
    source = Path(source)
    project = Path(project)
    doc = Document()
    doc.save(project)
    if source.suffix.lower() == ".zip":
        progress(0.05, "Estrazione dell'archivio WebODM")
        source = _extract(source, project.parent / f"{project.stem}_odm")
    info = read_task(source)
    ch = doc.add_chunk(label or source.name)
    ch.crs = info["crs"] or "LOCAL"
    off = info["offset"]
    m = np.eye(4)
    m[:3, 3] = off
    ch.transform = m  # internal = world - offset
    if info["images"] is not None:
        progress(0.1, "Foto")
        try:
            ch.add_photos([info["images"]])
        except Exception:
            pass
    if info["cloud"] is not None:
        progress(0.2, f"Nuvola di punti ({info['cloud'].name})")
        pc = PointCloud.load(info["cloud"])
        pc = PointCloud(np.asarray(pc.xyz, float) - off, pc.rgb, pc.normals, pc.classification)
        ch.point_cloud = pc
    if info["model"] is not None:
        progress(0.55, f"Modello ({info['model'].name})")
        import trimesh

        mesh = trimesh.load(str(info["model"]), force="mesh", process=False)
        if info["model"].suffix.lower() == ".ply" and np.abs(np.asarray(mesh.vertices)).max() > 1e5:
            mesh.vertices = np.asarray(mesh.vertices) - off  # odm_mesh.ply is in world coordinates
        dst = ch.data_dir / "model.ply"
        mesh.export(str(dst))
        ch._set_product("model", dst)
    for key in ("dem", "orthomosaic"):
        if info[key] is not None:
            ch._set_product(key, info[key].resolve())
    ch.meta["import"] = {"source": "webodm", "folder": str(source), "offset": off.tolist()}
    doc.save()
    progress(1.0, "Progetto WebODM importato")
    return doc


__all__ = ["read_task", "import_task"]
