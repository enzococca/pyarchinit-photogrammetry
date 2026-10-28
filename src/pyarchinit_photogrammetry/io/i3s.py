"""I3S, the Indexed 3D Scene Layer (Esri, an OGC community standard): the textured model as a Scene
Layer Package (``.slpk``) of an *Integrated Mesh* layer, version 1.7 - for ArcGIS Pro, ArcGIS
Online and Earth, CesiumJS and loaders.gl.

* The model is cut into a quadtree in plan as for 3D Tiles (:func:`.web.export_3dtiles`): a leaf
  holds at most ``max_faces`` faces of the model, every other node the model of its area with that
  many faces (vertex clustering, :func:`.mesh_ply.cluster_decimate`); the root of the tree has no
  mesh, as I3S wants. A node gives way to its children when its box covers more than about 4.5
  pixels a face on the screen (``lodThreshold``, ``maxScreenThresholdSQ``).
* Every node has a texture of its own, a power of two on each side as I3S wants, sized by what it
  shows: a leaf whose faces lie together in the model's texture (an orthophoto mapping, drone
  models) takes that part of it as it is; any other leaf has the colour of the model - texture
  pages, else vertex colours - laid on its faces, each texel from its own face, with about twice
  the texels the model has there; the other nodes cast from their faces onto the model
  (:func:`engine.normalmap.bake_maps`) for a texture of about three texels a pixel of their largest
  size on the screen.
* ``mode="global"`` (the default, what CesiumJS, deck.gl/loaders.gl and the global scenes of
  ArcGIS read): WGS84 longitude and latitude, every node a box oriented east, north, up at its
  centre (its rotation in earth-centred coordinates), the vertices as offsets from the centre in
  degrees and metres, normals earth-centred. ``mode="local"``: the projected CRS of the chunk (in
  metres, with an EPSG code: ArcGIS local scenes in that CRS), axis-aligned boxes, offsets and
  normals in metres. Heights stay those of the chunk (orthometric, as most survey CRSs). The
  triangles are listed corner by corner (position, normal, texture coordinate, colour) as one
  feature.
* The package: the layer, node pages, geometries (gzip) and textures (JPEG as it is) in a zip
  archive that is not compressed itself, ``metadata.json`` and, last, the MD5 hash table of the
  paths that lets clients find the files without reading the whole directory.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import struct
import uuid
import zipfile
from pathlib import Path

import numpy as np

from ..core.progress import Progress, as_progress

NODES_PER_PAGE = 64  # a power of two, as node pages must be
FACES_PER_NODE = 20_000
TEXTURE_SIZE = 2048  # the largest texture of a node
LOD_PIXELS_PER_FACE = 4.5
LEAF_OVERSAMPLING = 2.0  # texels of a baked leaf for a texel of the model (its cells are equal for small and large faces)
NODE_OVERSAMPLING = 3.0  # texels of any other node for a pixel of its largest size on the screen (lodThreshold)
CROP_WASTE = 3.0  # a leaf takes its part of the model's texture as it is when that part has at most 3 times its texels
HASH_TABLE = "@specialIndexFileHASH128@"
GEOMETRY_SCHEMA = {
    "geometryType": "triangles",
    "header": [{"property": "vertexCount", "type": "UInt32"}, {"property": "featureCount", "type": "UInt32"}],
    "topology": "PerAttributeArray",
    "ordering": ["position", "normal", "uv0", "color"],
    "vertexAttributes": {"position": {"valueType": "Float32", "valuesPerElement": 3},
                         "normal": {"valueType": "Float32", "valuesPerElement": 3},
                         "uv0": {"valueType": "Float32", "valuesPerElement": 2},
                         "color": {"valueType": "UInt8", "valuesPerElement": 4}},
    "featureAttributeOrder": ["id", "faceRange"],
    "featureAttributes": {"id": {"valueType": "UInt64", "valuesPerElement": 1},
                          "faceRange": {"valueType": "UInt32", "valuesPerElement": 2}},
}
GEOMETRY_DEFINITION = {
    "topology": "triangle",
    "geometryBuffers": [{"offset": 8, "position": {"type": "Float32", "component": 3},
                         "normal": {"type": "Float32", "component": 3}, "uv0": {"type": "Float32", "component": 2},
                         "color": {"type": "UInt8", "component": 4},
                         "featureId": {"type": "UInt64", "component": 1, "binding": "per-feature"},
                         "faceRange": {"type": "UInt32", "component": 2, "binding": "per-feature"}}],
}


def projected_epsg(crs: str | None) -> int:
    """The EPSG code of a projected CRS in metres - the plan of a system with heights above a geoid
    (``EPSG:32633+3855``: 32633) -, as an I3S layer in local mode needs it; ValueError otherwise."""
    from pyproj import CRS

    from ..core import crs as crs_mod

    if not crs or crs_mod.is_local(crs):
        raise ValueError("I3S needs the model in a projected CRS with an EPSG code (e.g. UTM): georeference the chunk")
    c = CRS.from_user_input(crs_mod.horizontal(crs))
    epsg = c.to_epsg()
    units = {a.unit_name.lower() for a in c.axis_info[:2]}
    if not c.is_projected or epsg is None or not units <= {"metre", "meter"}:
        raise ValueError(f"I3S needs a projected CRS in metres with an EPSG code, not {crs}")
    return int(epsg)


def _pow2(n: int) -> int:
    return int(2 ** np.ceil(np.log2(max(8, n))))


def _gz(obj) -> bytes:
    return gzip.compress(json.dumps(obj, separators=(",", ":")).encode("utf-8"), mtime=0)


def geometry_buffer(positions: np.ndarray, normals: np.ndarray, uv: np.ndarray) -> bytes:
    """The uncompressed geometry of a node (:data:`GEOMETRY_SCHEMA`): header, the attributes of
    every corner, one feature with every triangle."""
    n = len(positions)
    color = np.full((n, 4), 255, np.uint8)  # white: the texture as it is
    return (struct.pack("<II", n, 1) + np.asarray(positions, "<f4").tobytes() + np.asarray(normals, "<f4").tobytes()
            + np.asarray(uv, "<f4").tobytes() + color.tobytes() + struct.pack("<Q", 0)
            + struct.pack("<II", 0, max(n // 3 - 1, 0)))


def read_geometry_buffer(data: bytes) -> dict:
    """The arrays of :func:`geometry_buffer` (for checks)."""
    n, features = struct.unpack_from("<II", data, 0)
    at = 8
    out = {}
    for name, dtype, k in (("position", "<f4", 3), ("normal", "<f4", 3), ("uv0", "<f4", 2), ("color", "u1", 4)):
        size = n * k * np.dtype(dtype).itemsize
        out[name] = np.frombuffer(data, dtype, n * k, at).reshape(n, k)
        at += size
    out["id"] = np.frombuffer(data, "<u8", features, at)
    at += 8 * features
    out["faceRange"] = np.frombuffer(data, "<u4", 2 * features, at).reshape(features, 2)
    return out


def _canonical(name: str) -> str:
    return name.replace("\\", "/").lstrip("/").lower()


def hash_table(entries) -> bytes:
    """The SLPK hash table of ``(path, offset of its local header)``: MD5 of the canonical path and
    the offset (little-endian), sorted as the two halves of the digest read as little-endian
    integers."""
    rows = sorted(((hashlib.md5(_canonical(n).encode("utf-8")).digest(), int(off)) for n, off in entries),
                  key=lambda r: struct.unpack("<QQ", r[0]))
    return b"".join(d + struct.pack("<Q", off) for d, off in rows)


def _corner_normals(v: np.ndarray, f: np.ndarray) -> np.ndarray:
    """Unit normals of the corners ``(F * 3, 3)``: of the vertex (faces around it, weighted by
    their area), else of the face, else up (a vertex only of degenerate faces)."""
    fn = np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]])
    vn = np.zeros_like(v)
    for k in range(3):
        np.add.at(vn, f[:, k], fn)
    corner = vn[f].reshape(-1, 3)
    face = np.repeat(fn, 3, axis=0)
    length = np.linalg.norm(corner, axis=1)
    weak = length < 1e-12
    corner[weak] = face[weak]
    length[weak] = np.linalg.norm(face[weak], axis=1)
    flat = length < 1e-12
    corner[flat] = (0.0, 0.0, 1.0)
    length[flat] = 1.0
    return corner / length[:, None]


class _Global:
    """Nodes in WGS84: oriented boxes east, north, up at their centre, heights above the ellipsoid."""

    def __init__(self, crs: str):
        from pyproj import Transformer

        self.crs = crs
        self.to_ecef = Transformer.from_crs("EPSG:4979", "EPSG:4978", always_xy=True)
        self.from_ecef = Transformer.from_crs("EPSG:4978", "EPSG:4979", always_xy=True)

    def geo(self, v: np.ndarray) -> np.ndarray:
        """Longitude, latitude and height above the ellipsoid of WGS 84 of ``v`` (CRS of the chunk): heights above a
        geoid converted with its grid, refused without it (:func:`..core.crs.transform_points`); those of a system
        without heights of its own as they are."""
        from ..core import crs as crs_mod

        return crs_mod.transform_points(np.asarray(v, np.float64).reshape(-1, 3), self.crs, "EPSG:4979")

    def ecef(self, g: np.ndarray) -> np.ndarray:
        return np.column_stack(self.to_ecef.transform(g[:, 0], g[:, 1], g[:, 2]))

    @staticmethod
    def enu(lon: float, lat: float) -> np.ndarray:
        lo, la = np.radians(lon), np.radians(lat)
        return np.column_stack([[-np.sin(lo), np.cos(lo), 0.0],
                                [-np.sin(la) * np.cos(lo), -np.sin(la) * np.sin(lo), np.cos(la)],
                                [np.cos(la) * np.cos(lo), np.cos(la) * np.sin(lo), np.sin(la)]])

    def place(self, v: np.ndarray, normals: np.ndarray):
        """``(obb, offsets, normals)`` of the vertices ``v`` (CRS of the chunk) and their normals."""
        from scipy.spatial.transform import Rotation

        g = self.geo(v)
        e = self.ecef(g)
        lon0, lat0 = (g[:, 0].min() + g[:, 0].max()) / 2, (g[:, 1].min() + g[:, 1].max()) / 2
        rot = self.enu(lon0, lat0)
        c0 = self.ecef(np.array([[lon0, lat0, (g[:, 2].min() + g[:, 2].max()) / 2]]))[0]
        local = (e - c0) @ rot
        lo, hi = local.min(axis=0), local.max(axis=0)
        centre_ecef = c0 + rot @ ((lo + hi) / 2)
        centre = np.array(self.from_ecef.transform(*centre_ecef))
        tips = self.ecef(self.geo(v + normals))  # one metre along the normal, through the same projection
        n = tips - e
        n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
        box = {"center": [float(x) for x in centre], "halfSize": [float(x) for x in np.maximum((hi - lo) / 2, 1e-3)],
               "quaternion": [float(x) for x in Rotation.from_matrix(rot).as_quat()]}
        return box, g - centre, n


class _Local:
    """Nodes in the projected CRS of the chunk: axis-aligned boxes in metres."""

    @staticmethod
    def place(v: np.ndarray, normals: np.ndarray):
        lo, hi = v.min(axis=0), v.max(axis=0)
        centre = (lo + hi) / 2
        box = {"center": [float(x) for x in centre], "halfSize": [float(x) for x in np.maximum((hi - lo) / 2, 1e-3)],
               "quaternion": [0.0, 0.0, 0.0, 1.0]}
        return box, v - centre, normals


def _face_texels(texcoords, pages, texnumber, n: int) -> np.ndarray:
    """Texels of the model's texture on every face (4 a face with vertex colours)."""
    if texcoords is None or not pages:
        return np.full(n, 4.0)
    uv = np.asarray(texcoords, np.float64).reshape(-1, 3, 2)
    d1, d2 = uv[:, 1] - uv[:, 0], uv[:, 2] - uv[:, 0]
    area = 0.5 * np.abs(d1[:, 0] * d2[:, 1] - d2[:, 0] * d1[:, 1])
    px = np.array([img.shape[0] * img.shape[1] for img in pages], np.float64)
    return area * (px[0] if texnumber is None else px[np.asarray(texnumber, np.int64)])


def _texture_side(texels: float, limit: int) -> int:
    """A power of two for about ``texels`` texels, from 64 to ``limit``."""
    return int(min(_pow2(limit), max(64, _pow2(int(np.ceil(np.sqrt(max(texels, 1.0))))))))


def _cut_leaf(uv: np.ndarray, page: np.ndarray, texels: np.ndarray, limit: int, bake_rest):
    """The texture of a leaf from the part of ``page`` under most of its faces, taken as it is:
    ``(image, uv of the corners (F * 3, 2) from the top-left corner)``, or None when its faces do
    not lie together there. ``uv`` ``(F, 3, 2)`` (v up) and ``texels`` ``(F,)`` of the faces in the
    model's texture; the few faces elsewhere (the steep faces in the band of cells of an
    orthophoto mapping) get their colour from ``bake_rest(mask, size)`` -> ``(colour, uv (n, 3, 2)
    v up)``, in an atlas beside or below the cut (whichever makes the smaller texture). Larger
    than ``limit``, the cut is reduced."""
    import cv2

    h, w = page.shape[:2]
    px = np.clip(uv[..., 0] * w, 0, w)  # (F, 3) texels from the top-left corner; beyond the page nothing
    py = np.clip((1.0 - uv[..., 1]) * h, 0, h)
    cx, cy = px.mean(axis=1), py.mean(axis=1)
    (lx, hx), (ly, hy) = np.percentile(cx, [10, 90]), np.percentile(cy, [10, 90])
    gx, gy = 0.5 * (hx - lx) + 4, 0.5 * (hy - ly) + 4
    core = (px.min(axis=1) >= lx - gx) & (px.max(axis=1) <= hx + gx) & (py.min(axis=1) >= ly - gy) & \
        (py.max(axis=1) <= hy + gy)
    if core.sum() < 0.7 * len(uv):
        return None
    x0, x1 = max(0, int(np.floor(px[core].min())) - 2), min(w, int(np.ceil(px[core].max())) + 2)  # 2 texels around
    y0, y1 = max(0, int(np.floor(py[core].min())) - 2), min(h, int(np.ceil(py[core].max())) + 2)
    cw, chh = x1 - x0, y1 - y0
    if cw <= 0 or chh <= 0 or cw * chh > CROP_WASTE * max(float(texels[core].sum()), 16.0):
        return None
    rest = ~core
    top = _pow2(limit)
    rest_side = _texture_side(LEAF_OVERSAMPLING * float(texels[rest].sum()), max(64, top // 4)) if rest.any() else 0

    def fit(beside: bool):
        scale = min(1.0, (top - rest_side * beside) / cw, (top - rest_side * (not beside)) / chh)
        fw, fh = max(1, int(round(cw * scale))), max(1, int(round(chh * scale)))
        return _pow2(max(fw + rest_side * beside, fh + rest_side * (not beside))), -scale, beside, fw, fh

    size, _, beside, sw, sh = min(fit(True), fit(False))
    crop = page[y0:y1, x0:x1]
    if (sw, sh) != (cw, chh):
        crop = cv2.resize(crop, (sw, sh), interpolation=cv2.INTER_AREA)
    image = np.empty((size, size, 3), np.uint8)
    image[:sh, :sw] = crop
    image[:sh, sw:] = crop[:, -1:]  # the rest repeats the edges: nothing samples it, filtering stays clean
    image[sh:] = image[sh - 1:sh]
    corners = np.empty(uv.shape, np.float64)
    corners[core] = np.stack([(px[core] - x0) * (sw / cw) / size, (py[core] - y0) * (sh / chh) / size], axis=-1)
    if rest.any():
        colour, rest_uv = bake_rest(rest, rest_side)
        side = colour.shape[0]
        ox, oy = (sw, 0) if beside else (0, sh)
        image[oy:oy + side, ox:ox + side] = colour
        rest_uv = np.asarray(rest_uv, np.float64).reshape(-1, 3, 2)
        corners[rest] = np.stack([(ox + rest_uv[..., 0] * side) / size, (oy + (1.0 - rest_uv[..., 1]) * side) / size],
                                 axis=-1)
    return image, corners.reshape(-1, 2)


def _texture_source(chunk, mesh):
    """The colour of the model for the baking: ``(texcoords, pages (BGR images), texnumber)`` or
    ``(None, None, None)`` (the vertex colours then)."""
    import cv2

    from ..engine.texturing import texture_pages

    pages = texture_pages(chunk) if mesh.texcoords is not None else []
    if not pages or pages[0].suffix.lower() == ".obj":
        return None, None, None
    images = [cv2.imread(str(p), cv2.IMREAD_COLOR) for p in pages]
    if any(img is None for img in images):
        return None, None, None
    return mesh.texcoords, images, (mesh.texnumber if len(images) > 1 else None)


def export_slpk(chunk, path, max_faces: int = FACES_PER_NODE, texture_size: int = TEXTURE_SIZE, name: str | None = None,
                mode: str = "global", progress: Progress | None = None) -> Path:
    """The model of ``chunk`` as an I3S Integrated Mesh scene layer package (see the module
    docstring; ``mode`` "global" or "local"). Returns the path."""
    import cv2

    from ..engine.normalmap import bake_maps
    from .mesh_ply import cluster_decimate, read_mesh

    progress = as_progress(progress)
    epsg = projected_epsg(chunk.crs)
    if mode not in ("global", "local"):
        raise ValueError("mode: global or local")
    frame = _Global(chunk.crs) if mode == "global" else _Local()
    model = chunk.product_path("model")
    if model is None or not model.exists():
        raise RuntimeError("build the model first")
    progress(0.01, "I3S: reading the model")
    mesh = read_mesh(model)
    world = mesh.xyz @ chunk.transform[:3, :3].T + chunk.transform[:3, 3]
    faces = np.asarray(mesh.faces, np.int64)
    texcoords, pages, texnumber = _texture_source(chunk, mesh)
    colors = mesh.colors
    tri_centre = world[faces].mean(axis=1)
    total = len(faces)
    if not total:
        raise ValueError("the model has no faces")
    face_texels = _face_texels(texcoords, pages, texnumber, total)
    nodes: list = [None]  # 0: the root, without a mesh
    done = [0]  # faces already in a leaf: every face ends in one
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    z = zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED, allowZip64=True)  # the nodes go in as they are made

    def add(sel: np.ndarray, parent: int, depth: int) -> int:
        index = len(nodes)
        nodes.append(None)
        progress.check()
        progress(0.02 + 0.9 * done[0] / total, f"I3S: node {index}")
        used, inv = np.unique(faces[sel], return_inverse=True)
        v, f = world[used], inv.reshape(-1, 3)
        leaf = len(sel) <= max_faces or depth >= 12
        if leaf:
            nv, nf = v, f
        else:
            nv, nf, _ = cluster_decimate(v, lambda: iter([f]), None, target_faces=max_faces)
        texels = float(face_texels[sel].sum())

        def bake(part: np.ndarray, size: int, same_mesh: bool):
            """The colour laid on the faces ``part`` of the node: a leaf has the faces of the model,
            each texel from its own face; any other node bakes it from the model around it
            (coordinates from the centre: float32 for the rays)."""
            sub_faces = sel[part]
            if part.all():
                u, fv, ff = used, v, f
            else:
                u, i = np.unique(faces[sub_faces], return_inverse=True)
                fv, ff = world[u], i.reshape(-1, 3)
            lv, lf = (fv, ff) if same_mesh else (nv, nf)
            c = (lv.min(axis=0) + lv.max(axis=0)) / 2
            colour, _normal, uv, _share = bake_maps(
                fv - c, ff, lv - c, lf, size, None if colors is None else colors[u],
                None if texcoords is None else texcoords[sub_faces], pages,
                full_texnumber=None if texnumber is None else texnumber[sub_faces], same_mesh=same_mesh, normals=False)
            return colour, uv

        cut = None
        if leaf and pages is not None:
            on = None if texnumber is None else np.unique(texnumber[sel])
            if on is None or len(on) == 1:
                cut = _cut_leaf(texcoords[sel].reshape(-1, 3, 2), pages[0 if on is None else int(on[0])],
                                face_texels[sel], texture_size, lambda part, size: bake(part, size, True))
        if cut is not None:
            image, corner_uv = cut
        else:
            need = LEAF_OVERSAMPLING * texels if leaf else NODE_OVERSAMPLING * LOD_PIXELS_PER_FACE * len(nf)
            colour, uv = bake(np.ones(len(sel), bool), _texture_side(need, texture_size), leaf)
            side = colour.shape[0]
            size = _pow2(side)
            image = np.full((size, size, 3), 127, np.uint8)
            image[:side, :side] = colour
            corner_uv = np.asarray(uv, np.float64).reshape(-1, 2)
            corner_uv = np.column_stack([corner_uv[:, 0] * side / size, (1.0 - corner_uv[:, 1]) * side / size])  # top-left
        size = image.shape[0]
        ok, jpg = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 88])
        if not ok:
            raise RuntimeError("I3S: a node texture could not be written")
        corners = nv[nf].reshape(-1, 3)
        box, offsets, normals = frame.place(corners, _corner_normals(nv, nf))
        z.writestr(f"nodes/{index}/geometries/0.bin.gz", gzip.compress(geometry_buffer(offsets, normals, corner_uv),
                                                                        mtime=0))
        z.writestr(f"nodes/{index}/textures/0.jpg", jpg.tobytes())
        node = {"index": index, "parentIndex": parent, "obb": box,
                "mesh": {"material": {"definition": 0, "resource": index, "texelCountHint": size * size},
                         "geometry": {"definition": 0, "resource": index, "vertexCount": int(len(corners)),
                                      "featureCount": 1}}}
        if leaf:
            done[0] += len(sel)
        else:
            node["lodThreshold"] = float(LOD_PIXELS_PER_FACE * len(nf))
            mid = (world[used].min(axis=0) + world[used].max(axis=0)) / 2
            q = (tri_centre[sel, 0] >= mid[0]).astype(int) + 2 * (tri_centre[sel, 1] >= mid[1]).astype(int)
            node["children"] = [add(sel[q == k], index, depth + 1) for k in range(4) if (q == k).any()]
        nodes[index] = node
        return index

    try:
        top = add(np.arange(total), 0, 0)
    except BaseException:  # cancelled or failed: no half-written package
        z.close()
        tmp.unlink(missing_ok=True)
        raise
    nodes[0] = {"index": 0, "obb": nodes[top]["obb"], "children": [top]}
    progress(0.93, f"I3S: {len(nodes)} nodes, writing the package")
    lo, hi = world.min(axis=0), world.max(axis=0)
    wkid = epsg
    if mode == "global":
        corners = np.array([[x, y, 0.0] for x in (lo[0], hi[0]) for y in (lo[1], hi[1])])
        g = frame.geo(corners)
        lo, hi, wkid = g.min(axis=0), g.max(axis=0), 4326
    label = name or chunk.label
    layer = {
        "id": 0, "version": str(uuid.uuid4()).upper(), "name": label, "alias": label, "layerType": "IntegratedMesh",
        "description": f"pyarchinit-photogrammetry: {chunk.label}", "copyrightText": "",
        "capabilities": ["View", "Query"],
        "spatialReference": {"wkid": wkid, "latestWkid": wkid},
        # on the globe the heights are above the ellipsoid of WGS 84 (_Global.geo); in the projected system of the
        # chunk they are its own
        "heightModelInfo": ({"heightModel": "ellipsoidal", "vertCRS": "WGS_84", "heightUnit": "meter"}
                            if mode == "global" else
                            {"heightModel": "gravity_related_height", "vertCRS": "unknown", "heightUnit": "meter"}),
        "store": {"id": "{" + str(uuid.uuid4()).upper() + "}", "profile": "meshpyramids", "version": "1.7",
                  "resourcePattern": ["Geometry", "Texture"], "extent": [float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1])],
                  "indexCRS": f"http://www.opengis.net/def/crs/EPSG/0/{wkid}",
                  "vertexCRS": f"http://www.opengis.net/def/crs/EPSG/0/{wkid}",
                  "normalReferenceFrame": "earth-centered" if mode == "global" else "vertex-reference-frame",
                  "lodType": "MeshPyramid", "lodModel": "node-switching",
                  "defaultGeometrySchema": GEOMETRY_SCHEMA},
        "nodePages": {"nodesPerPage": NODES_PER_PAGE, "rootIndex": 0, "lodSelectionMetricType": "maxScreenThresholdSQ"},
        "materialDefinitions": [{"doubleSided": True, "pbrMetallicRoughness": {
            "baseColorTexture": {"textureSetDefinitionId": 0}, "metallicFactor": 0.0}}],
        "textureSetDefinitions": [{"formats": [{"name": "0", "format": "jpg"}]}],
        "geometryDefinitions": [GEOMETRY_DEFINITION],
    }
    with z:
        for k in range(0, len(nodes), NODES_PER_PAGE):
            z.writestr(f"nodePages/{k // NODES_PER_PAGE}.json.gz", _gz({"nodes": nodes[k:k + NODES_PER_PAGE]}))
        z.writestr("metadata.json", json.dumps({"folderPattern": "basic", "ArchiveCompressionType": "Store",
                                                "ResourceCompressionType": "GZIP", "I3SVersion": "1.7",
                                                "nodeCount": len(nodes)}))
        z.writestr("3dSceneLayer.json.gz", _gz(layer))
        z.writestr(HASH_TABLE, hash_table((i.filename, i.header_offset) for i in z.infolist()))
    tmp.replace(path)
    progress(1.0, f"I3S: {len(nodes)} nodes in {path.name}")
    return path


def read_slpk(path) -> dict:
    """What a package holds, for checks: ``layer``, ``nodes`` (all the node pages), ``files``
    (names) and ``hash`` (path -> offset, from the hash table)."""
    with zipfile.ZipFile(path) as z:
        layer = json.loads(gzip.decompress(z.read("3dSceneLayer.json.gz")))
        nodes = []
        k = 0
        while f"nodePages/{k}.json.gz" in z.namelist():
            nodes += json.loads(gzip.decompress(z.read(f"nodePages/{k}.json.gz")))["nodes"]
            k += 1
        table = z.read(HASH_TABLE) if HASH_TABLE in z.namelist() else b""
        names = z.namelist()
    digests = {hashlib.md5(_canonical(n).encode("utf-8")).digest(): n for n in names}
    hashes = {}
    for at in range(0, len(table), 24):
        d, off = table[at:at + 16], struct.unpack("<Q", table[at + 16:at + 24])[0]
        hashes[digests.get(d, d.hex())] = off
    return {"layer": layer, "nodes": nodes, "files": names, "hash": hashes}
