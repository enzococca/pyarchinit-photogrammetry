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
  the centre of the model; heights as they are, the geoid not applied). With ``origin`` the tiles
  are in a frame of their own instead (the coordinates less ``origin``, no place on the globe: the
  model of a web site, :mod:`.site`); with ``texture_size`` the leaves keep the texture of the model.
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
    """The model of the chunk: world vertices (float64), faces, colours, per-face texcoords, the
    texture pages (images; [] without a texture) and the page of every face (None: one page)."""
    from ..engine.texturing import texture_pages
    from .mesh_ply import read_mesh

    path = chunk.product_path("model")
    if path is None or not path.exists():
        raise RuntimeError("build the model first")
    mesh = read_mesh(path)
    world = mesh.xyz @ chunk.transform[:3, :3].T + chunk.transform[:3, 3]
    pages = texture_pages(chunk)
    if not pages or pages[0].suffix.lower() == ".obj" or mesh.texcoords is None:
        pages = []
    return world, np.asarray(mesh.faces, np.int64), mesh.colors, mesh.texcoords, pages, \
        (mesh.texnumber if len(pages) > 1 else None)


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

    world, faces, colors, texcoords, pages, texnumber = _world_mesh(chunk)
    centre = (world.min(0) + world.max(0)) / 2
    local = world - centre
    yup = np.column_stack([local[:, 0], local[:, 2], -local[:, 1]])  # glTF is Y-up
    if texnumber is None:
        scene = trimesh.Scene(_trimesh(yup, faces, colors, texcoords, pages[0] if pages else None))
    else:  # a mesh (a material) per texture page
        scene = trimesh.Scene([_trimesh(yup, faces[texnumber == k], colors, texcoords[texnumber == k], page)
                               for k, page in enumerate(pages) if np.any(texnumber == k)])
    scene.metadata["extras"] = {"crs": chunk.crs, "origin": [float(v) for v in centre], "axes": "x east, y up, z south"}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(scene.export(file_type="glb"))
    path.with_suffix(".json").write_text(json.dumps({"crs": chunk.crs, "origin": [float(v) for v in centre],
                                                     "axes": "glTF: x east, y up, z south"}, indent=2))
    return path


def _enu_to_ecef(chunk, centre) -> list[float]:
    """Column-major 4 x 4 transform from east, north, up at ``centre`` (world, CRS of the chunk) to
    earth-centred coordinates; identity for local coordinates. The globe takes heights above the
    ellipsoid of WGS 84: heights above a geoid are converted with its grid, refused without it
    (:func:`..core.crs.transform_points`); those of a system without heights of its own go as they are."""
    from ..core import crs as crs_mod

    if crs_mod.is_local(chunk.crs):
        return [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]
    from pyproj import Transformer

    lon, lat, h = crs_mod.transform_points([[float(v) for v in centre[:3]]], chunk.crs, "EPSG:4979")[0]
    x, y, z = Transformer.from_crs("EPSG:4979", "EPSG:4978", always_xy=True).transform(lon, lat, h)
    lo, la = np.radians(lon), np.radians(lat)
    east = [-np.sin(lo), np.cos(lo), 0.0]
    north = [-np.sin(la) * np.cos(lo), -np.sin(la) * np.sin(lo), np.cos(la)]
    up = [np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)]
    return [*east, 0.0, *north, 0.0, *up, 0.0, x, y, z, 1.0]


def _page_images(pages: list[Path], folder: Path, texture_size: int) -> tuple[list[str], list[np.ndarray]]:
    """The texture pages as JPEG images of at most ``texture_size`` pixels a side in ``folder/textures``
    (written by the program: no tag of the source images); their names and their pixels (RGB)."""
    from PIL import Image

    names, pixels = [], []
    (folder / "textures").mkdir(parents=True, exist_ok=True)
    for k, page in enumerate(pages):
        with Image.open(page) as im:
            im = im.convert("RGB")
            if max(im.size) > texture_size:
                f = texture_size / max(im.size)
                im = im.resize((max(1, round(im.width * f)), max(1, round(im.height * f))), Image.LANCZOS)
            name = f"textures/page_{k}.jpg"
            im.save(folder / name, "JPEG", quality=85, optimize=True)
            names.append(name)
            pixels.append(np.asarray(im))
    return names, pixels


def _texture_colours(n: int, faces, texcoords, texnumber, pixels: list[np.ndarray]) -> np.ndarray:
    """A colour for every vertex from the texture (the mean of its corners): the coarse tiles have no texture."""
    acc = np.zeros((n, 3))
    cnt = np.zeros(n)
    uv = np.asarray(texcoords, np.float64).reshape(-1, 3, 2)
    page = np.zeros(len(faces), int) if texnumber is None else np.asarray(texnumber, int)
    for k, img in enumerate(pixels):
        sel = page == k
        if not sel.any():
            continue
        h, w = img.shape[:2]
        u, v = uv[sel, :, 0].ravel(), uv[sel, :, 1].ravel()
        col = np.clip((u * w).astype(int), 0, w - 1)
        row = np.clip(((1 - v) * h).astype(int), 0, h - 1)
        idx = faces[sel].ravel()
        rgb = img[row, col].astype(float)
        for c in range(3):
            acc[:, c] += np.bincount(idx, rgb[:, c], minlength=n)
        cnt += np.bincount(idx, minlength=n)
    return np.where(cnt[:, None] > 0, acc / np.maximum(cnt, 1)[:, None], 200).astype(np.uint8)


def _glb(primitives: list[dict]) -> bytes:
    """A binary glTF of meshes without lights (``KHR_materials_unlit``, both sides): every primitive a dict of
    ``position`` (n, 3, Y-up), ``index`` (m,), and ``uv`` (n, 2, glTF orientation) with its texture ``page`` -
    the image itself is not in the tile, the viewer puts the page on it (``extras.page`` of the material) so
    that the tiles share one image - or ``colour`` (n, 3, 0-255)."""
    import struct

    blob = bytearray()
    views, accessors, meshes_p, materials = [], [], [], []

    def add(array: np.ndarray, kind: str, component: int, target: int, bounds: bool = False) -> int:
        while len(blob) % 4:
            blob.append(0)
        data = np.ascontiguousarray(array).tobytes()
        views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": len(data), "target": target})
        blob.extend(data)
        acc = {"bufferView": len(views) - 1, "componentType": component, "count": int(len(array)), "type": kind}
        if bounds:
            acc["min"] = [float(x) for x in array.min(0)]
            acc["max"] = [float(x) for x in array.max(0)]
        accessors.append(acc)
        return len(accessors) - 1

    for p in primitives:
        attrs = {"POSITION": add(np.asarray(p["position"], np.float32), "VEC3", 5126, 34962, True)}
        material = {"pbrMetallicRoughness": {"baseColorFactor": [1.0, 1.0, 1.0, 1.0], "metallicFactor": 0.0,
                                             "roughnessFactor": 1.0},
                    "doubleSided": True, "extensions": {"KHR_materials_unlit": {}}}
        if p.get("uv") is not None:
            attrs["TEXCOORD_0"] = add(np.asarray(p["uv"], np.float32), "VEC2", 5126, 34962)
            material["extras"] = {"page": int(p["page"])}
        elif p.get("colour") is not None:
            srgb = np.asarray(p["colour"], np.float64) / 255.0  # glTF colours are linear
            linear = np.where(srgb <= 0.04045, srgb / 12.92, ((srgb + 0.055) / 1.055) ** 2.4)
            attrs["COLOR_0"] = add(linear.astype(np.float32), "VEC3", 5126, 34962)
        index = add(np.asarray(p["index"], np.uint32).ravel(), "SCALAR", 5125, 34963)
        materials.append(material)
        meshes_p.append({"attributes": attrs, "indices": index, "material": len(materials) - 1, "mode": 4})
    gltf = {"asset": {"version": "2.0", "generator": "pyarchinit-photogrammetry"},
            "extensionsUsed": ["KHR_materials_unlit"], "scene": 0, "scenes": [{"nodes": [0]}],
            "nodes": [{"mesh": 0}], "meshes": [{"primitives": meshes_p}], "materials": materials,
            "accessors": accessors, "bufferViews": views, "buffers": [{"byteLength": len(blob)}]}
    text = json.dumps(gltf, separators=(",", ":")).encode()
    text += b" " * (-len(text) % 4)
    while len(blob) % 4:
        blob.append(0)
    total = 12 + 8 + len(text) + 8 + len(blob)
    return (struct.pack("<4sII", b"glTF", 2, total) + struct.pack("<I4s", len(text), b"JSON") + text
            + struct.pack("<I4s", len(blob), b"BIN\0") + bytes(blob))


def _textured(v, f, uv, page) -> list[dict]:
    """The primitives of a textured tile: one per texture page, the vertices split where the texture is."""
    out = []
    for k in np.unique(page):
        sel = page == k
        corner = f[sel].reshape(-1)
        st = np.asarray(uv[sel], np.float32).reshape(-1, 2)
        key = np.column_stack([corner.astype(np.int64), st.view(np.uint32).astype(np.int64)])
        uniq, inv = np.unique(key, axis=0, return_inverse=True)
        first = np.zeros(len(uniq), np.int64)
        first[inv.ravel()] = np.arange(len(corner))
        tex = st[first]
        out.append({"position": v[uniq[:, 0]], "index": inv.ravel(), "page": int(k),
                    "uv": np.column_stack([tex[:, 0], 1.0 - tex[:, 1]])})  # glTF counts v from the top
    return out


def export_3dtiles(chunk, folder: str | os.PathLike, max_faces: int = 150_000, progress=None, *,
                   origin=None, texture_size: int | None = None) -> Path:
    """The model as 3D Tiles 1.1 in ``folder`` (``tileset.json`` and ``tiles/*.glb``, see the
    module docstring): tiles of at most ``max_faces`` faces. Returns the tileset.

    ``origin`` (x, y, z in the coordinates of the chunk): the tiles in a frame of their own, the coordinates
    less ``origin`` (east, north, up), with no transform to the globe and no reference system or origin
    written anywhere. ``texture_size``: the leaves keep the texture of the model, its pages written in
    ``folder/textures`` with at most this many pixels a side and named in ``extras.textures`` of the tileset;
    each leaf has the texture coordinates and the page in ``extras.page`` of its material, not the image
    (the viewer of a site loads every page once for all the tiles); the coarser levels and a model without a
    texture keep the vertex colours. Tiles written so have no lights (``KHR_materials_unlit``)."""
    import trimesh

    from ..core.progress import as_progress
    from .mesh_ply import cluster_decimate

    progress = as_progress(progress)
    world, faces, colors, uv, pages, texnumber = _world_mesh(chunk)
    folder = Path(folder)
    (folder / "tiles").mkdir(parents=True, exist_ok=True)
    textured = texture_size is not None and bool(pages) and uv is not None
    names: list[str] = []
    if textured:
        names, pixels = _page_images(pages, folder, int(texture_size))
        page = np.zeros(len(faces), int) if texnumber is None else np.asarray(texnumber, int)
        uv = np.asarray(uv, np.float64).reshape(-1, 3, 2)
        if colors is None:
            colors = _texture_colours(len(world), faces, uv, texnumber, pixels)
    if colors is None:
        colors = np.full((len(world), 3), 200, np.uint8)
    centre = (world.min(0) + world.max(0)) / 2 if origin is None else np.asarray(origin, np.float64)
    local = world - centre  # east, north, up (the axes of a projected CRS) around the centre
    tri_centre = local[faces].mean(axis=1)
    count = [0]

    def yup(v):
        return np.column_stack([v[:, 0], v[:, 2], -v[:, 1]])  # glTF Y-up: the runtime turns it to Z-up

    def write_tile(v, f, c, sel=None) -> str:
        name = f"tiles/{count[0]}.glb"
        count[0] += 1
        if textured:
            prims = _textured(yup(v), f, uv[sel], page[sel]) if sel is not None else [
                {"position": yup(v), "index": f, "colour": c}]
            (folder / name).write_bytes(_glb(prims))
        else:
            mesh = trimesh.Trimesh(yup(v), f, vertex_colors=c, process=False)
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
            return {"boundingVolume": {"box": box(v)}, "geometricError": 0.0,
                    "content": {"uri": write_tile(v, f, c, sel)}}
        dv, df, dc = cluster_decimate(v, lambda: iter([f]), c, target_faces=max_faces)
        mid = (v.min(0) + v.max(0)) / 2
        q = (tri_centre[sel, 0] >= mid[0]).astype(int) + 2 * (tri_centre[sel, 1] >= mid[1]).astype(int)
        children = [node(sel[q == k], depth + 1) for k in range(4) if (q == k).any()]
        # the error of the coarse level: the size of its clusters, as the viewer judges it
        error = size / np.sqrt(max_faces) * 2
        return {"boundingVolume": {"box": box(v)}, "geometricError": error, "refine": "REPLACE",
                "content": {"uri": write_tile(dv, df, dc)}, "children": children}

    root = node(np.arange(total), 0)
    extras: dict = {}
    if origin is None:
        root["transform"] = _enu_to_ecef(chunk, centre)
        extras = {"crs": chunk.crs, "origin": [float(x) for x in centre]}
    if names:
        extras["textures"] = names
    tileset = {"asset": {"version": "1.1", "generator": "pyarchinit-photogrammetry"},
               "geometricError": float(root.get("geometricError", 0.0)) * 2 or 1.0, "root": root}
    if extras:
        tileset["extras"] = extras
    path = folder / "tileset.json"
    path.write_text(json.dumps(tileset, indent=1))
    progress(1.0, f"3D Tiles: {count[0]} tiles")
    return path
