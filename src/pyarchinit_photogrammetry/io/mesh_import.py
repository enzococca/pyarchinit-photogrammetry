"""Meshes made by other programs as the model of a chunk.

:func:`import_model` reads an OBJ (with its MTL and texture images), a PLY (vertex colours and
other vertex properties, texture coordinates per face corner or per vertex, texture pages named
by ``TextureFile`` comments, as Metashape and MeshLab write them), a glTF/GLB, or any other mesh
trimesh reads (STL, OFF, ...) and makes it the model of a chunk: ``model.ply`` in the internal
frame of the chunk with the texture pages beside it (``model_texture.jpg``,
``model_texture_1.jpg``, ...), as :func:`engine.texturing.build_texture` writes them. The model it
replaces is kept among the versions unless ``keep=False``. :func:`import_model_chunk` puts the
mesh in a new chunk.

Coordinates. ``frame="world"`` (the default): the file is in the coordinate system of the chunk
(its CRS, or the local system of a chunk without reference), like the meshes this program,
Metashape or CloudCompare export in that system. ``scale`` multiplies the coordinates of the file
(0.001 for millimetres) and ``offset`` is added after it (a file exported around a shifted
origin: the shift). ``frame="internal"``: the file is in the internal frame of the chunk (its own
``model.ply`` edited in MeshLab or Blender). glTF is Y-up: ``up="auto"`` turns glTF files to Z-up
and leaves the others; the GLB written by this program keeps its origin in the JSON file beside
it, used when no offset is given. A chunk without photos, products or reference takes the mesh
as it comes: its internal frame starts at a round origin under the mesh (single precision keeps
the millimetres of map coordinates) and ``crs`` names its coordinate system.

Every object of the file joins one mesh (their transforms applied); every texture image becomes
a page. Vertices at the same position are joined (texture coordinates are per face corner), except
between closed objects: solids touching each other (the units of a stratigraphic model, proxies)
stay separate solids, so a cut closes each one in its own colour. The vertex colours of a textured
mesh are read from the texture, normals are left out (the program does not keep them).
"""
from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from ..core.progress import Progress, as_progress

TEXTURE_QUALITY = 92  # JPEG quality of the texture pages, as build_texture writes them
UV_FIELDS = (("texture_u", "texture_v"), ("s", "t"), ("u", "v"))  # texture coordinates per vertex in a PLY
LEFT_OUT = {"x", "y", "z", "red", "green", "blue", "alpha", "nx", "ny", "nz"}
DOUBLE_BEYOND = 1e4  # internal coordinates larger than this are written in double precision
FORMATS = (".obj", ".ply", ".glb", ".gltf", ".stl", ".off", ".dae")


@dataclass
class Mesh:
    """A mesh read from a file: positions (float64, coordinates of the file), faces, vertex colours
    (RGB uint8), texture coordinates per face corner ``(F, 6)`` (origin at the bottom left), the page
    of each face (None with one page), the pages (an image file or a BGR array each), the other
    vertex properties of a PLY and what was read (``info``)."""

    xyz: np.ndarray
    faces: np.ndarray
    colors: np.ndarray | None = None
    texcoords: np.ndarray | None = None
    texnumber: np.ndarray | None = None
    pages: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)
    info: dict = field(default_factory=dict)
    groups: np.ndarray | None = None  # per vertex: its closed object, -1 (or None) for the rest


# ------------------------------------------------------------------------------------- reading
def read_mesh_file(path, progress: Progress | None = None) -> Mesh:
    """The mesh of ``path`` in the coordinates of the file (see the module docstring)."""
    progress = as_progress(progress)
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    progress(0.0, f"Reading {path.name}")
    mesh = _read_ply(path) if path.suffix.lower() == ".ply" else _read_trimesh(path)
    if not len(mesh.faces):
        raise ValueError(f"{path.name}: no triangles in the file")
    progress(0.7, f"{len(mesh.faces):,} faces")
    mesh.groups = closed_components(mesh.xyz, mesh.faces)
    mesh = _join_duplicates(mesh)
    if mesh.colors is None and mesh.texcoords is not None:
        mesh.colors = _colours_from_texture(mesh)
    progress(1.0, f"{len(mesh.faces):,} faces, {len(mesh.pages)} texture pages")
    return mesh


def _read_ply(path: Path) -> Mesh:
    from .mesh_ply import read_mesh

    m = read_mesh(path)
    names = list(m.vertices.dtype.names or ())
    tex, page = m.texcoords, m.texnumber
    uv = next((p for p in UV_FIELDS if p[0] in names and p[1] in names), None)
    if tex is None and uv is not None:
        u, v = (np.asarray(m.vertices[n], np.float64) for n in uv)
        tex = np.stack([u[m.faces], v[m.faces]], -1).reshape(-1, 6).astype(np.float32)
    left_out = LEFT_OUT | set(uv or ())
    extra = {n: np.asarray(m.vertices[n]) for n in names
             if n not in left_out and m.vertices.dtype[n].shape == () and m.vertices.dtype[n].kind in "uif"}
    pages, missing = [], []
    if tex is not None:
        names_of_pages = m.texture_files
        for name in names_of_pages:
            p = path.parent / name
            (pages if p.exists() else missing).append(p)
        if not names_of_pages:  # texture coordinates without a TextureFile comment: an image with the same name
            pages = [p for p in (path.with_suffix(s) for s in (".jpg", ".jpeg", ".png", ".tif", ".tiff"))
                     if p.exists()][:1]
        if missing or not pages:
            tex = page = None
            pages = []
        elif len(pages) == 1:
            page = None
        elif page is None:
            page = np.zeros(len(m.faces), np.int32)
    info = {"format": "ply", "objects": 1, "missing_textures": [p.name for p in missing]}
    return Mesh(m.xyz, np.asarray(m.faces, np.int64), m.colors, tex, page, pages, extra, info)


def _read_trimesh(path: Path) -> Mesh:
    import trimesh

    loaded = trimesh.load_scene(str(path), process=False) if hasattr(trimesh, "load_scene") \
        else trimesh.load(str(path), process=False)
    geoms = loaded.dump(concatenate=False) if isinstance(loaded, trimesh.Scene) else [loaded]
    geoms = [g for g in geoms if isinstance(g, trimesh.Trimesh) and len(g.faces)]
    if not geoms:
        raise ValueError(f"{path.name}: no triangles in the file")
    xyz, faces, colors, uvs, pages_of = [], [], [], [], []
    images = {}  # id of the image -> (page, RGB array)
    coloured = False
    n = 0
    for g in geoms:
        v = np.asarray(g.vertices, np.float64)
        f = np.asarray(g.faces, np.int64)
        image, uv, colour = _visual(g)
        if image is not None:
            if id(image) not in images:
                images[id(image)] = (len(images), np.ascontiguousarray(np.asarray(image.convert("RGB"))))
            k, rgb = images[id(image)]
            uv = np.asarray(uv, np.float64)
            pages_of.append(np.full(len(f), k, np.int32))
            uvs.append(uv[f].reshape(-1, 6))
            if colour is None:
                colour = _sample(rgb, uv)
        else:
            pages_of.append(np.full(len(f), -1, np.int32))
            uvs.append(None)
        coloured |= colour is not None
        if colour is None:
            colour = np.full((len(v), 3), 200, np.uint8)
        xyz.append(v)
        faces.append(f + n)
        colors.append(np.asarray(colour, np.uint8)[:, :3])
        n += len(v)
    pages = [np.ascontiguousarray(rgb[:, :, ::-1]) for _k, rgb in sorted(images.values(), key=lambda e: e[0])]
    page = np.concatenate(pages_of)
    tex = None
    if pages:
        if (page < 0).any():  # objects without a texture: a page of plain colours, one block each
            pages, uvs, page = _plain_page(pages, uvs, page, faces, colors)
        tex = np.concatenate(uvs).astype(np.float32)
    info = {"format": path.suffix.lower().lstrip("."), "objects": len(geoms), "missing_textures": []}
    return Mesh(np.vstack(xyz), np.vstack(faces), np.vstack(colors) if coloured else None, tex,
                (page if len(pages) > 1 else None) if pages else None, pages, {}, info)


def closed_components(xyz: np.ndarray, faces: np.ndarray) -> np.ndarray:
    """Per vertex, the connected piece of the mesh it belongs to when that piece encloses a volume
    (every edge, vertices at the same position counted as one, shared by an even number of faces of
    the piece: two, or four where a solid pinches - a layer thinning to nothing between cells), -1
    otherwise: solids written side by side in one file (stratigraphic units, proxies)."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    n = len(xyz)
    out = np.full(n, -1, np.int64)
    if not len(faces) or not n:
        return out
    e = np.vstack([faces[:, [0, 1]], faces[:, [1, 2]]])
    graph = coo_matrix((np.ones(len(e), np.int8), (e[:, 0], e[:, 1])), shape=(n, n))
    _count, label = connected_components(graph, directed=False)
    rows = np.ascontiguousarray(np.column_stack([np.asarray(xyz, np.float64), label.astype(np.float64)]))
    _u, pid = np.unique(rows.view(np.dtype((np.void, 32))).ravel(), return_inverse=True)
    f = pid.reshape(-1)[faces]
    edges = np.sort(np.vstack([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]]), axis=1)
    owner = np.tile(label[faces[:, 0]], 3)
    real = edges[:, 0] != edges[:, 1]
    edges, owner = edges[real], owner[real]
    uniq, first, counts = np.unique(edges, axis=0, return_index=True, return_counts=True)
    closed = np.ones(label.max() + 1, bool)
    closed[np.unique(owner[first[counts % 2 == 1]])] = False
    faces_per = np.bincount(label[faces[:, 0]], minlength=len(closed))
    closed &= faces_per >= 4
    hit = closed[label]
    out[hit] = label[hit]
    return out


def _visual(g):
    """``(image, uv per vertex, vertex colours)`` of a trimesh: the texture (PIL) when the object has
    one, otherwise its colours (vertex, face or material colour), None where missing."""
    visual = g.visual
    kind = getattr(visual, "kind", None)
    if kind == "texture":
        uv = getattr(visual, "uv", None)
        mat = getattr(visual, "material", None)
        image = getattr(mat, "image", None)
        if image is None:
            image = getattr(mat, "baseColorTexture", None)
        if image is not None and uv is not None and len(uv) == len(g.vertices):
            return image, uv, None
        colour = getattr(mat, "main_color", None)
        if colour is not None:
            return None, None, np.tile(np.asarray(colour, np.uint8)[:3], (len(g.vertices), 1))
        return None, None, None
    if kind == "vertex":
        return None, None, np.asarray(visual.vertex_colors)[:, :3]
    if kind == "face":
        fc = np.asarray(visual.face_colors, np.float64)[:, :3]
        acc = np.zeros((len(g.vertices), 3))
        cnt = np.zeros(len(g.vertices))
        f = np.asarray(g.faces)
        for k in range(3):
            np.add.at(acc, f[:, k], fc)
            np.add.at(cnt, f[:, k], 1)
        return None, None, np.clip(acc / np.maximum(cnt, 1)[:, None], 0, 255).astype(np.uint8)
    return None, None, None


def _sample(rgb: np.ndarray, uv: np.ndarray) -> np.ndarray:
    """Colours of the texture ``rgb`` at the texture coordinates ``uv`` (repeated outside 0-1)."""
    h, w = rgb.shape[:2]
    u = np.mod(uv[:, 0], 1.0)
    v = np.mod(uv[:, 1], 1.0)
    x = np.clip((u * w).astype(np.int64), 0, w - 1)
    y = np.clip(((1.0 - v) * h).astype(np.int64), 0, h - 1)
    return rgb[y, x, :3].astype(np.uint8)


def _plain_page(pages, uvs, page, faces, colors):
    """A last page of 8 x 8 px blocks, one per object without a texture in its mean colour, its
    faces mapped on the middle of their block (the texture covers every face)."""
    starts = np.cumsum([0] + [len(f) for f in faces])
    plain = [k for k, u in enumerate(uvs) if u is None]
    side = 8
    cols = int(np.ceil(np.sqrt(len(plain))))
    rows = int(np.ceil(len(plain) / cols))
    img = np.full((rows * side, cols * side, 3), 200, np.uint8)
    last = len(pages)
    for j, k in enumerate(plain):
        r, c = divmod(j, cols)
        mean = np.asarray(colors[k], np.float64).mean(axis=0) if len(colors[k]) else np.full(3, 200.0)
        img[r * side:(r + 1) * side, c * side:(c + 1) * side] = np.asarray(mean[::-1], np.uint8)  # BGR
        u = (c * side + side / 2) / img.shape[1]
        v = 1.0 - (r * side + side / 2) / img.shape[0]
        uvs[k] = np.tile([u, v], (starts[k + 1] - starts[k], 3))
        page[starts[k]:starts[k + 1]] = last
    return pages + [img], uvs, page


def _join_duplicates(mesh: Mesh) -> Mesh:
    """Vertices at the same position joined (first colour and properties kept), unused ones left out;
    a closed object's vertices only among themselves (``mesh.groups``)."""
    xyz = np.ascontiguousarray(mesh.xyz, np.float64)
    group = np.full(len(xyz), -1.0) if mesh.groups is None else mesh.groups.astype(np.float64)
    rows = np.ascontiguousarray(np.column_stack([xyz, group]))
    keys = rows.view(np.dtype((np.void, rows.dtype.itemsize * 4))).ravel()
    _u, first, inverse = np.unique(keys, return_index=True, return_inverse=True)
    used = np.zeros(len(first), bool)
    used[inverse.reshape(-1)[mesh.faces.reshape(-1)]] = True
    if len(first) == len(xyz) and used.all():
        return mesh
    order = np.argsort(first)
    order = order[used[order]]
    rank = np.full(len(first), -1, np.int64)
    rank[order] = np.arange(len(order))
    keep = first[order]
    mesh.faces = rank[inverse.reshape(-1)][mesh.faces]
    mesh.xyz = xyz[keep]
    if mesh.groups is not None:
        mesh.groups = mesh.groups[keep]
    if mesh.colors is not None:
        mesh.colors = mesh.colors[keep]
    mesh.extra = {k: v[keep] for k, v in mesh.extra.items()}
    return mesh


def _colours_from_texture(mesh: Mesh) -> np.ndarray | None:
    """Vertex colours read from the texture at the corners of the faces (a reduced copy of each page)."""
    acc = np.zeros((len(mesh.xyz), 3))
    cnt = np.zeros(len(mesh.xyz))
    page = mesh.texnumber if mesh.texnumber is not None else np.zeros(len(mesh.faces), np.int32)
    tex = np.asarray(mesh.texcoords, np.float64).reshape(-1, 3, 2)
    for k, p in enumerate(mesh.pages):
        sel = page == k
        if not sel.any():
            continue
        img = cv2.imread(str(p), cv2.IMREAD_REDUCED_COLOR_4) if isinstance(p, Path) else p
        if img is None:
            continue
        rgb = np.ascontiguousarray(img[:, :, ::-1])
        for c in range(3):
            col = _sample(rgb, tex[sel, c])
            np.add.at(acc, mesh.faces[sel, c], col)
            np.add.at(cnt, mesh.faces[sel, c], 1)
    if not cnt.any():
        return None
    out = np.clip(acc / np.maximum(cnt, 1)[:, None], 0, 255).astype(np.uint8)
    out[cnt == 0] = 200
    return out


# ------------------------------------------------------------------------------- coordinates
def default_up(path) -> str:
    """The vertical axis of a file: Y for glTF (the standard says so), Z otherwise."""
    return "y" if Path(path).suffix.lower() in (".glb", ".gltf") else "z"


def default_offset(path) -> np.ndarray:
    """The origin of a GLB written by this program (in the JSON file beside it), else zeros."""
    side = Path(path).with_suffix(".json")
    if Path(path).suffix.lower() in (".glb", ".gltf") and side.exists():
        try:
            origin = json.loads(side.read_text(encoding="utf-8")).get("origin")
            if origin is not None and len(origin) == 3:
                return np.asarray(origin, np.float64)
        except (ValueError, OSError):
            pass
    return np.zeros(3)


def is_empty(chunk) -> bool:
    """A chunk without photos, products or reference: an imported mesh sets its frame."""
    return (not chunk.cameras and not chunk.markers and not chunk.products
            and np.allclose(chunk.transform, np.eye(4)))


def to_file_frame(xyz: np.ndarray, up: str = "z", scale: float = 1.0, offset=None) -> np.ndarray:
    """Coordinates of the file turned Z-up (``up="y"``: x, -z, y), scaled, then shifted."""
    xyz = np.asarray(xyz, np.float64)
    if up == "y":
        xyz = np.column_stack([xyz[:, 0], -xyz[:, 2], xyz[:, 1]])
    elif up != "z":
        raise ValueError("up: 'z', 'y' or 'auto'")
    xyz = xyz * float(scale)
    if offset is not None:
        xyz = xyz + np.asarray(offset, np.float64).reshape(1, 3)
    return xyz


def _distance_from_data(chunk, internal: np.ndarray) -> float | None:
    """How far (world metres) the mesh lies from the data of the chunk (tie points, cameras, dense
    cloud): 0 when their boxes touch, None without data."""
    ref = None
    tp = chunk.tie_points
    if tp is not None and len(tp):
        ref = np.asarray(tp.xyz, np.float64)
    elif chunk.aligned_cameras:
        ref = np.array([c.transform[:3, 3] for c in chunk.aligned_cameras])
    if ref is None or not len(ref):
        return None
    lo, hi = np.percentile(ref, 2, axis=0), np.percentile(ref, 98, axis=0)
    mlo, mhi = internal.min(0), internal.max(0)
    gap = np.maximum(0.0, np.maximum(lo - mhi, mlo - hi))
    return float(np.linalg.norm(gap) * chunk.scale)


# ------------------------------------------------------------------------------------ import
def import_model(chunk, path, frame: str = "world", offset=None, scale: float = 1.0, up: str = "auto",
                 crs: str | None = None, keep: bool = True, name: str | None = None,
                 progress: Progress | None = None) -> dict:
    """The mesh of ``path`` as the model of ``chunk`` (see the module docstring); ``keep``: the
    current model goes among the versions (it comes back if the import fails), ``name``: the name
    of the new version. Returns a report: vertices, faces, pages, colours, the frame, ``distance``
    from the data of the chunk in metres (0 when they overlap, None without data), ``kept`` (the
    id of the version kept)."""
    from ..core import versions

    progress = as_progress(progress)
    path = Path(path)
    if frame not in ("world", "internal"):
        raise ValueError("frame: 'world' or 'internal'")
    up = default_up(path) if up in (None, "", "auto") else up
    if offset is None:
        offset = default_offset(path) if frame == "world" else np.zeros(3)
    offset = np.asarray(offset, np.float64).reshape(3)
    if not np.isfinite(scale) or scale <= 0:
        raise ValueError("scale must be positive")
    empty = is_empty(chunk)
    if crs and not empty:
        raise ValueError("the chunk already has photos, products or a reference: its coordinate system stays")
    mesh = read_mesh_file(path, progress.sub(0.0, 0.6))
    progress.check()
    xyz = to_file_frame(mesh.xyz, up, scale, offset)
    before = (chunk.transform.copy(), chunk.crs, chunk.meta.get("metric"))
    if empty:  # the mesh sets the frame of the chunk: metres from a round origin under it
        from ..core import crs as crs_mod

        frame = "world"
        chunk.transform = np.eye(4)
        chunk.transform[:3, 3] = np.floor(xyz.min(0))
        chunk.crs = crs or chunk.crs or crs_mod.LOCAL
        chunk.meta["metric"] = True  # metres, also in local coordinates
    internal = chunk.to_internal(xyz) if frame == "world" else xyz
    distance = None if empty else _distance_from_data(chunk, internal)
    with tempfile.TemporaryDirectory() as tmp:
        _detach_pages(mesh, chunk.data_dir, Path(tmp))
        kept = versions.keep_active(chunk, "model", move=True) if keep else None
        try:
            written = _write_model(chunk, mesh, internal, progress.sub(0.6, 0.4))
        except BaseException:
            if kept is not None:
                versions.restore(chunk, "model", kept)
            if empty:
                chunk.transform, chunk.crs = before[0], before[1]
                if before[2] is None:
                    chunk.meta.pop("metric", None)
            raise
    chunk.meta["model"] = {"source": "import", "file": path.name, "format": mesh.info.get("format"),
                           "frame": frame, "up": up, "scale": float(scale), "offset": [float(v) for v in offset]}
    if written["pages"]:
        chunk.meta["texture"] = {"mapping": "import", "size": written["size"], "pages": written["pages"]}
    else:
        chunk.meta.pop("texture", None)
    versions.set_active_name(chunk, "model", name or versions.label("Importato da {0}", path.name))
    report = {"file": str(path), "vertices": int(len(internal)), "faces": int(len(mesh.faces)),
              "pages": written["pages"], "colours": mesh.colors is not None, "objects": mesh.info.get("objects", 1),
              "missing_textures": mesh.info.get("missing_textures", []), "frame": frame, "up": up,
              "scale": float(scale), "offset": [float(v) for v in offset], "distance": distance,
              "kept": kept["id"] if kept else None, "double": written["double"], "new_frame": empty}
    progress(1.0, f"Model imported: {len(mesh.faces):,} faces, {written['pages']} texture pages")
    return report


def _detach_pages(mesh: Mesh, data_dir: Path, tmp: Path) -> None:
    """Texture pages that are files of the chunk (a model of the chunk imported again) copied away
    first: keeping the current model as a version moves them."""
    for k, page in enumerate(mesh.pages):
        if isinstance(page, Path):
            try:
                page.resolve().relative_to(data_dir.resolve())
            except ValueError:
                continue
            copy = tmp / f"{k}_{page.name}"
            shutil.copy2(page, copy)
            mesh.pages[k] = copy


def import_model_chunk(doc, path, label: str | None = None, **kw):
    """:func:`import_model` into a new chunk of ``doc`` (removed again if the import fails);
    returns ``(chunk, report)``."""
    chunk = doc.add_chunk(label or Path(path).stem)
    try:
        report = import_model(chunk, path, keep=False, **kw)
    except BaseException:
        doc.remove_chunk(chunk)
        raise
    return chunk, report


def _write_model(chunk, mesh: Mesh, internal: np.ndarray, progress: Progress) -> dict:
    from .mesh_ply import MeshData, write_mesh

    progress(0.0, "Writing the model")
    data_dir = chunk.data_dir
    names = ["model_texture.jpg" if k == 0 else f"model_texture_{k}.jpg" for k in range(len(mesh.pages))]
    for old in list(data_dir.glob("model_texture_*.jpg")) + [data_dir / "model_texture.jpg",
                                                              data_dir / "model_texture_display.jpg",
                                                              data_dir / "model_preview.ply"]:
        if old.name not in names and old.exists():
            old.unlink()
    size = None
    for k, (page, name) in enumerate(zip(mesh.pages, names)):
        progress.check()
        out = data_dir / name
        if isinstance(page, Path) and page.suffix.lower() in (".jpg", ".jpeg"):
            shutil.copy2(page, out)
            img_size = _image_size(out)
        else:
            img = cv2.imread(str(page), cv2.IMREAD_COLOR) if isinstance(page, Path) else page
            if img is None:
                raise ValueError(f"cannot read the texture {page}")
            cv2.imwrite(str(out), img, [cv2.IMWRITE_JPEG_QUALITY, TEXTURE_QUALITY])
            img_size = [int(img.shape[1]), int(img.shape[0])]
        size = size or img_size
        progress(0.1 + 0.6 * (k + 1) / len(mesh.pages), f"Texture page {k + 1}/{len(mesh.pages)}")
    double = bool(np.abs(internal).max() > DOUBLE_BEYOND)
    ft = "<f8" if double else "<f4"
    fields = [("x", ft), ("y", ft), ("z", ft)]
    if mesh.colors is not None:
        fields += [("red", "u1"), ("green", "u1"), ("blue", "u1")]
    fields += [(n, a.dtype) for n, a in mesh.extra.items()]
    vertices = np.empty(len(internal), fields)
    vertices["x"], vertices["y"], vertices["z"] = internal.T
    if mesh.colors is not None:
        vertices["red"], vertices["green"], vertices["blue"] = mesh.colors.T
    for n, a in mesh.extra.items():
        vertices[n] = a
    data = MeshData(vertices, np.asarray(mesh.faces, np.int64), mesh.texcoords if names else None, [],
                    mesh.texnumber if len(names) > 1 else None)
    if len(names) > 1:
        data.set_texture_files(names)
    elif names:
        data.set_texture_file(names[0])
    chunk._set_product("model", write_mesh(chunk.own_product_path("model"), data))
    if names:
        chunk._set_product("model_textured", data_dir / names[0])
    else:
        chunk.products.pop("model_textured", None)
    progress(1.0, "Model written")
    return {"pages": len(names), "size": size, "double": double}


def _image_size(path: Path) -> list[int]:
    from PIL import Image

    with Image.open(path) as im:
        return [int(im.size[0]), int(im.size[1])]


__all__ = ["Mesh", "read_mesh_file", "import_model", "import_model_chunk", "default_up", "default_offset",
           "is_empty", "to_file_frame", "closed_components", "FORMATS"]
