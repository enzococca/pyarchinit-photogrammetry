"""Exports for the web and for other programs: Cloud Optimized GeoTIFF, binary glTF (GLB) and
Cesium 3D Tiles; the cloud as COPC is in :mod:`.copc`.

* :func:`export_cog`: a raster (orthomosaic, DEM, any GeoTIFF) as a COG - tiled, with overviews,
  read by parts over the network (QGIS, web maps). ``jpeg`` for orthomosaics (the alpha becomes a
  mask), ``webp`` (keeps the alpha, smaller), ``zstd``/``deflate``/``lzw`` lossless (DEMs, with the
  floating point predictor);
* :func:`export_glb`: the model as one GLB with its texture (or its vertex colours), coordinates
  around its centre so that single precision keeps the millimetres - the centre (world, CRS) is in
  the ``extras`` of the scene and in a ``.json`` beside it;
* :func:`export_3dtiles`: the model as 3D Tiles 1.1 for CesiumJS (and Cesium for Unreal, QGIS):
  a quadtree of GLB tiles in plan, each level the model with fewer faces (vertex colours), the
  leaves at full detail; the root placed on the globe from the CRS of the chunk (east, north, up at
  the centre of the model; heights as they are, the geoid not applied).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

COMPRESSIONS = ("jpeg", "webp", "zstd", "deflate", "lzw")


def export_cog(src: str | os.PathLike, dst: str | os.PathLike, compression: str = "auto", quality: int = 85,
               overviews: bool = True) -> Path:
    """Write ``src`` as a Cloud Optimized GeoTIFF (see the module docstring); ``auto``: JPEG for
    8-bit images, ZSTD for anything else."""
    import rasterio
    import rasterio.shutil

    with rasterio.open(src) as s:
        dtype, count = s.dtypes[0], s.count
    comp = compression.lower()
    if comp == "auto":
        comp = "jpeg" if dtype == "uint8" and count in (3, 4) else "zstd"
    if comp not in COMPRESSIONS:
        raise ValueError(f"compression: one of auto, {', '.join(COMPRESSIONS)}")
    if comp in ("jpeg", "webp") and dtype != "uint8":
        raise ValueError(f"{comp} is for 8-bit images (orthomosaics): use zstd or deflate for a DEM")
    opts = {"driver": "COG", "COMPRESS": comp.upper(), "BLOCKSIZE": 512, "BIGTIFF": "IF_SAFER",
            "OVERVIEWS": "AUTO" if overviews else "NONE", "NUM_THREADS": "ALL_CPUS"}
    if comp in ("jpeg", "webp"):
        opts["QUALITY"] = int(quality)
    elif dtype.startswith("float"):
        opts["PREDICTOR"] = "FLOATING_POINT"
    else:
        opts["PREDICTOR"] = "YES"
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    rasterio.shutil.copy(str(src), str(dst), **opts)
    return dst


def _world_mesh(chunk):
    """The model of the chunk: world vertices (float64), faces, colours, per-face texcoords and the
    texture image (or None)."""
    from .mesh_ply import read_mesh

    path = chunk.product_path("model")
    if path is None or not path.exists():
        raise RuntimeError("build the model first")
    mesh = read_mesh(path)
    world = mesh.xyz @ chunk.transform[:3, :3].T + chunk.transform[:3, 3]
    texture = chunk.product_path("model_textured")
    if texture is None or not texture.exists() or texture.suffix.lower() == ".obj" or mesh.texcoords is None:
        texture = None
    return world, np.asarray(mesh.faces, np.int64), mesh.colors, mesh.texcoords, texture


def _trimesh(vertices, faces, colors=None, texcoords=None, texture=None):
    """A trimesh with the texture (vertices split per face corner, as the texture coordinates
    are per face) or with the vertex colours."""
    import trimesh

    if texture is not None and texcoords is not None:
        from PIL import Image

        corner = faces.reshape(-1)
        uv = np.asarray(texcoords, np.float64).reshape(-1, 2)
        v = vertices[corner]
        f = np.arange(len(corner)).reshape(-1, 3)
        visual = trimesh.visual.TextureVisuals(uv=uv, image=Image.open(texture))
        return trimesh.Trimesh(v, f, visual=visual, process=False)
    return trimesh.Trimesh(vertices, faces, vertex_colors=colors, process=False)


def export_glb(chunk, path: str | os.PathLike) -> Path:
    """The model as a binary glTF (see the module docstring); returns the path."""
    import trimesh

    world, faces, colors, texcoords, texture = _world_mesh(chunk)
    centre = (world.min(0) + world.max(0)) / 2
    local = world - centre
    yup = np.column_stack([local[:, 0], local[:, 2], -local[:, 1]])  # glTF is Y-up
    mesh = _trimesh(yup, faces, colors, texcoords, texture)
    scene = trimesh.Scene(mesh)
    scene.metadata["extras"] = {"crs": chunk.crs, "origin": [float(v) for v in centre], "axes": "x east, y up, z south"}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(scene.export(file_type="glb"))
    path.with_suffix(".json").write_text(json.dumps({"crs": chunk.crs, "origin": [float(v) for v in centre],
                                                     "axes": "glTF: x east, y up, z south"}, indent=2))
    return path


def _enu_to_ecef(chunk, centre) -> list[float]:
    """Column-major 4 x 4 transform from east, north, up at ``centre`` (world, CRS of the chunk) to
    earth-centred coordinates; identity for local coordinates."""
    from ..core import crs as crs_mod

    if crs_mod.is_local(chunk.crs):
        return [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]
    from pyproj import Transformer

    lon, lat = Transformer.from_crs(chunk.crs, "EPSG:4326", always_xy=True).transform(centre[0], centre[1])
    h = float(centre[2])
    x, y, z = Transformer.from_crs("EPSG:4979", "EPSG:4978", always_xy=True).transform(lon, lat, h)
    lo, la = np.radians(lon), np.radians(lat)
    east = [-np.sin(lo), np.cos(lo), 0.0]
    north = [-np.sin(la) * np.cos(lo), -np.sin(la) * np.sin(lo), np.cos(la)]
    up = [np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)]
    return [*east, 0.0, *north, 0.0, *up, 0.0, x, y, z, 1.0]


def export_3dtiles(chunk, folder: str | os.PathLike, max_faces: int = 150_000, progress=None) -> Path:
    """The model as 3D Tiles 1.1 in ``folder`` (``tileset.json`` and ``tiles/*.glb``, see the
    module docstring): tiles of at most ``max_faces`` faces. Returns the tileset."""
    import trimesh

    from ..core.progress import as_progress
    from .mesh_ply import cluster_decimate

    progress = as_progress(progress)
    world, faces, colors, _uv, _tex = _world_mesh(chunk)
    if colors is None:
        colors = np.full((len(world), 3), 200, np.uint8)
    centre = (world.min(0) + world.max(0)) / 2
    local = world - centre  # east, north, up (the axes of a projected CRS) around the centre
    folder = Path(folder)
    (folder / "tiles").mkdir(parents=True, exist_ok=True)
    tri_centre = local[faces].mean(axis=1)
    count = [0]

    def write_tile(v, f, c) -> str:
        name = f"tiles/{count[0]}.glb"
        count[0] += 1
        yup = np.column_stack([v[:, 0], v[:, 2], -v[:, 1]])  # glTF Y-up: the runtime turns it to Z-up
        mesh = trimesh.Trimesh(yup, f, vertex_colors=c, process=False)
        (folder / name).write_bytes(trimesh.Scene(mesh).export(file_type="glb"))
        return name

    def box(v) -> list[float]:
        lo, hi = v.min(0), v.max(0)
        c, h = (lo + hi) / 2, np.maximum((hi - lo) / 2, 1e-3)
        return [*map(float, c), float(h[0]), 0.0, 0.0, 0.0, float(h[1]), 0.0, 0.0, 0.0, float(h[2])]

    total = len(faces)

    def node(sel: np.ndarray, depth: int) -> dict:
        F = faces[sel]
        used, inv = np.unique(F, return_inverse=True)
        v, f, c = local[used], inv.reshape(-1, 3), colors[used]
        size = float(np.linalg.norm(v.max(0) - v.min(0)))
        progress(min(0.95, count[0] / max(4 * total / max_faces, 1)), f"3D Tiles: tile {count[0] + 1}")
        if len(sel) <= max_faces or depth >= 12:
            return {"boundingVolume": {"box": box(v)}, "geometricError": 0.0, "content": {"uri": write_tile(v, f, c)}}
        dv, df, dc = cluster_decimate(v, lambda: iter([f]), c, target_faces=max_faces)
        mid = (v.min(0) + v.max(0)) / 2
        q = (tri_centre[sel, 0] >= mid[0]).astype(int) + 2 * (tri_centre[sel, 1] >= mid[1]).astype(int)
        children = [node(sel[q == k], depth + 1) for k in range(4) if (q == k).any()]
        # the error of the coarse level: the size of its clusters, as the viewer judges it
        error = size / np.sqrt(max_faces) * 2
        return {"boundingVolume": {"box": box(v)}, "geometricError": error, "refine": "REPLACE",
                "content": {"uri": write_tile(dv, df, dc)}, "children": children}

    root = node(np.arange(total), 0)
    root["transform"] = _enu_to_ecef(chunk, centre)
    tileset = {"asset": {"version": "1.1", "generator": "pyarchinit-photogrammetry"},
               "geometricError": float(root.get("geometricError", 0.0)) * 2 or 1.0,
               "root": root, "extras": {"crs": chunk.crs, "origin": [float(x) for x in centre]}}
    path = folder / "tileset.json"
    path.write_text(json.dumps(tileset, indent=1))
    progress(1.0, f"3D Tiles: {count[0]} tiles")
    return path
