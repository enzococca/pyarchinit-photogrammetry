"""Cutting the model with planes: one side kept, a slice between two parallel planes, the model
split in two, or a series of slices.

A plane is a point and a normal in world coordinates (the coordinate system of the chunk):
:func:`horizontal` at a height, :func:`vertical` through a line of the plan (a section trace),
:func:`through_points` through three points picked on the model, :func:`dipping` with the dip
direction and the dip of geology. "Above" is the side the normal points to (for a horizontal
plane, above it; for a vertical plane through a line from A to B, the left of the walker from A
to B).

Faces crossed by a plane are cut on it: new vertices on the crossed edges (shared by the two faces
of the edge, so the cut edge stays one line) with their colours, confidence and every other vertex
property interpolated, and the texture coordinates of the corners interpolated too, so the
texture stays. :func:`cut_model` makes the part kept the model of the chunk (the old one among the
versions) or writes the parts to files in world coordinates (PLY, OBJ with the texture).
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np

from ..core.progress import Progress, as_progress

KEEP = ("above", "below", "slab", "both", "series")


# --------------------------------------------------------------------------------------- planes
def horizontal(z: float) -> tuple[np.ndarray, np.ndarray]:
    """The horizontal plane at height ``z`` (normal up)."""
    return np.array([0.0, 0.0, float(z)]), np.array([0.0, 0.0, 1.0])


def vertical(a, b) -> tuple[np.ndarray, np.ndarray]:
    """The vertical plane through the line from ``a`` to ``b`` (x, y; a z is ignored), normal to the
    left of the walker from ``a`` to ``b``."""
    a, b = np.asarray(a, np.float64)[:2], np.asarray(b, np.float64)[:2]
    d = b - a
    n = np.hypot(*d)
    if n == 0:
        raise ValueError("the two points of the line coincide")
    return np.array([a[0], a[1], 0.0]), np.array([-d[1] / n, d[0] / n, 0.0])


def through_points(p1, p2, p3) -> tuple[np.ndarray, np.ndarray]:
    """The plane through three points, normal by the right hand (p1, p2, p3 counter-clockwise seen
    from above: normal up)."""
    p1, p2, p3 = (np.asarray(p, np.float64) for p in (p1, p2, p3))
    n = np.cross(p2 - p1, p3 - p1)
    length = np.linalg.norm(n)
    if length < 1e-12 * max(1.0, np.linalg.norm(p2 - p1) * np.linalg.norm(p3 - p1)):
        raise ValueError("the three points are on one line")
    return p1, n / length


def dipping(point, dip_direction: float, dip: float) -> tuple[np.ndarray, np.ndarray]:
    """The plane through ``point`` dipping ``dip`` degrees towards ``dip_direction`` (degrees from
    north, clockwise), normal upwards."""
    az, inc = np.radians(float(dip_direction)), np.radians(float(dip))
    n = np.array([np.sin(az) * np.sin(inc), np.cos(az) * np.sin(inc), np.cos(inc)])
    return np.asarray(point, np.float64), n / np.linalg.norm(n)


def to_internal(chunk, point, normal, near=None) -> tuple[np.ndarray, np.ndarray]:
    """A world plane in the internal frame of the chunk (the frame of model.ply). ``near``: a world
    point by the data; the point of the plane is moved there first (a horizontal plane at (0, 0, z)
    lies thousands of kilometres from a survey in map coordinates: a rounding of the normal there
    would tilt the cut by millimetres over the model). The normal goes through the transpose of the
    linear part of the transform: exact."""
    point = np.asarray(point, np.float64)
    normal = np.asarray(normal, np.float64) / np.linalg.norm(normal)
    if near is not None:
        near = np.asarray(near, np.float64)
        point = near - float((near - point) @ normal) * normal
    n = chunk.transform[:3, :3].T @ normal
    return chunk.to_internal(point[None])[0], n / np.linalg.norm(n)


# ------------------------------------------------------------------------------------- cutting
def clip(mesh, point, normal, keep: str = "above", cap: bool = False):
    """The part of ``mesh`` (:class:`io.mesh_ply.MeshData`, in the frame of ``point``/``normal``) on
    one side of the plane (``keep`` "above", the side of the normal, or "below"), faces crossed by
    the plane cut on it; ``cap``: the solids cut closed on the plane (:func:`cap`). Returns a new
    MeshData (no faces when nothing is on that side)."""
    from ..io.mesh_ply import MeshData

    xyz = mesh.xyz
    n = np.asarray(normal, np.float64)
    d = (xyz - np.asarray(point, np.float64)) @ n
    if keep == "below":
        d = -d
    elif keep != "above":
        raise ValueError("keep: 'above' or 'below'")
    scale = max(float(np.abs(xyz).max()) if len(xyz) else 1.0, 1.0)
    d[np.abs(d) < 1e-12 * scale] = 0.0  # on the plane: kept, never cut again
    inside = d >= 0
    faces = np.asarray(mesh.faces, np.int64)
    count = inside[faces].sum(axis=1)
    whole = count == 3
    one, two = np.nonzero(count == 1)[0], np.nonzero(count == 2)[0]
    tex = None if mesh.texcoords is None else np.asarray(mesh.texcoords, np.float64).reshape(-1, 3, 2)
    page = mesh.texnumber
    # the corners of the cut faces rotated so that the odd one (inside for one, outside for two) is first
    def rotated(rows, odd_inside):
        f = faces[rows]
        flag = inside[f] if odd_inside else ~inside[f]
        first = np.argmax(flag, axis=1)
        order = (first[:, None] + np.arange(3)[None]) % 3
        return np.take_along_axis(f, order, 1), order

    f1, o1 = rotated(one, True)
    f2, o2 = rotated(two, False)
    # new vertices: one per crossed edge, shared by its two faces
    edges = np.vstack([f1[:, [0, 1]], f1[:, [0, 2]], f2[:, [0, 1]], f2[:, [0, 2]]]) if len(one) + len(two) \
        else np.zeros((0, 2), np.int64)
    lo, hi = np.minimum(edges[:, 0], edges[:, 1]), np.maximum(edges[:, 0], edges[:, 1])
    keys, inverse = np.unique(np.column_stack([lo, hi]), axis=0, return_inverse=True)
    inverse = inverse.reshape(-1)
    t = d[keys[:, 0]] / (d[keys[:, 0]] - d[keys[:, 1]]) if len(keys) else np.zeros(0)
    new_vertices = _interpolate(mesh.vertices, keys[:, 0], keys[:, 1], t)
    vid = len(mesh.vertices) + np.arange(len(keys))
    inner = np.where(inside[keys[:, 0]], keys[:, 0], keys[:, 1]) if len(keys) else np.zeros(0, np.int64)
    on_plane = d[inner] == 0  # the edge leaves the plane at a vertex: that vertex, not a copy of it
    vid[on_plane] = inner[on_plane]
    e = vid[inverse]
    n1, n2 = len(one), len(two)
    e1a, e1b = e[:n1], e[n1:2 * n1]  # one inside: edges 0-1 and 0-2
    e2a, e2b = e[2 * n1:2 * n1 + n2], e[2 * n1 + n2:]  # one outside: edges 0-1 and 0-2
    new_faces = [faces[whole],
                 np.column_stack([f1[:, 0], e1a, e1b]),
                 np.column_stack([e2a, f2[:, 1], f2[:, 2]]),
                 np.column_stack([e2a, f2[:, 2], e2b])]
    out_tex = out_page = None
    if tex is not None:
        def corner_uv(rows, order):
            return np.take_along_axis(tex[rows], order[:, :, None].repeat(2, axis=2), 1)

        def along(uv, f, i, j):  # the texture coordinates of the point on corner i -> j of each face
            di, dj = d[f[:, i]], d[f[:, j]]
            s = (di / np.where(di - dj == 0, 1.0, di - dj))[:, None]
            return uv[:, i] + s * (uv[:, j] - uv[:, i])

        u1, u2 = corner_uv(one, o1), corner_uv(two, o2)
        a1, b1 = along(u1, f1, 0, 1), along(u1, f1, 0, 2)
        a2, b2 = along(u2, f2, 0, 1), along(u2, f2, 0, 2)
        out_tex = np.concatenate([tex[whole].reshape(-1, 6),
                                  np.stack([u1[:, 0], a1, b1], 1).reshape(-1, 6),
                                  np.stack([a2, u2[:, 1], u2[:, 2]], 1).reshape(-1, 6),
                                  np.stack([a2, u2[:, 2], b2], 1).reshape(-1, 6)]).astype(np.float32)
        if page is not None:
            page = np.asarray(page)
            out_page = np.concatenate([page[whole], page[one], page[two], page[two]]).astype(np.int32)
    all_vertices = np.concatenate([mesh.vertices, new_vertices]) if len(new_vertices) else mesh.vertices.copy()
    out = MeshData(all_vertices, np.vstack(new_faces).astype(np.int64), out_tex, list(mesh.comments), out_page)
    f = out.faces
    real = (f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 2] != f[:, 0])  # a face touching the plane only
    out = out.subset_faces(real)  # unused vertices left out
    if cap and len(out.faces):
        out = close_cut(out, point, n if keep == "above" else -n)
    return out


def close_cut(mesh, point, inward):
    """Close the solids that a cut opened. The edges on the plane that bound only one face form
    loops; walked as the faces orient them, a loop that turns counter-clockwise seen from the part
    kept (``inward``) is the outline of a solid, a clockwise one the outline of a hollow (the inside
    of a vessel, whose faces look into it). Each outline, less the hollows inside it, is triangulated
    (constrained Delaunay) on its own vertices, facing out of the part kept: solids side by side
    (stratigraphic units) are closed each on its own vertices, in its colour. Open edges of a
    surface (a terrain) do not close a loop and stay open. On a textured mesh each new face takes
    the colour of the texture at one of its corners."""
    import shapely
    from scipy.spatial import cKDTree

    from ..io.mesh_ply import MeshData

    xyz = mesh.xyz
    n = np.asarray(inward, np.float64) / np.linalg.norm(inward)
    p = np.asarray(point, np.float64)
    extent = float(np.ptp(xyz, axis=0).max()) if len(xyz) else 1.0
    on = np.abs((xyz - p) @ n) <= 1e-6 * (float(np.abs(xyz).max()) + extent)  # single-precision vertices too
    f = np.asarray(mesh.faces, np.int64)
    a, b = f.ravel(), f[:, [1, 2, 0]].ravel()
    count = np.int64(len(xyz))
    sel = ~np.isin(b * count + a, a * count + b) & on[a] & on[b]
    if not sel.any():
        return mesh
    e1 = np.cross(n, [1.0, 0.0, 0.0] if abs(n[0]) < 0.9 else [0.0, 1.0, 0.0])
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(n, e1)  # (e1, e2, n) right-handed: counter-clockwise seen from the part kept is positive
    uv = np.column_stack([(xyz - p) @ e1, (xyz - p) @ e2])
    outlines, hollows = [], []
    for ring in _loops(a[sel], b[sel]):
        c = uv[ring]
        signed = 0.5 * float(np.sum(c[:, 0] * np.roll(c[:, 1], -1) - np.roll(c[:, 0], -1) * c[:, 1]))
        poly = shapely.make_valid(shapely.Polygon(c))
        if poly.is_empty or abs(signed) <= (1e-9 * max(extent, 1.0)) ** 2:
            continue
        (outlines if signed > 0 else hollows).append((poly, ring))
    caps = []
    for poly, ring in outlines:
        inner = [(h, r) for h, r in hollows if poly.contains(shapely.point_on_surface(h))]
        region = poly.difference(shapely.union_all([h for h, _r in inner])) if inner else poly
        own = np.concatenate([ring] + [r for _h, r in inner])
        tree = cKDTree(uv[own])
        for t in shapely.get_parts(shapely.constrained_delaunay_triangles(region)):
            corners = np.asarray(shapely.get_coordinates(t))[:3]
            dist, idx = tree.query(corners)
            if dist.max() > 1e-6 * max(extent, 1.0) or len(set(idx.tolist())) < 3:
                continue
            caps.append(own[idx])
    if not caps:
        return mesh
    caps = np.asarray(caps, np.int64)
    c = xyz[caps]
    facing = np.cross(c[:, 1] - c[:, 0], c[:, 2] - c[:, 0]) @ n
    caps[facing > 0] = caps[facing > 0][:, [0, 2, 1]]  # the new faces look out of the part kept
    tex = page = None
    if mesh.texcoords is not None:
        corner_uv = np.zeros((len(xyz), 2), np.float32)
        corner_uv[f.ravel()] = np.asarray(mesh.texcoords, np.float32).reshape(-1, 2)
        flat = np.repeat(corner_uv[caps[:, 0]][:, None, :], 3, axis=1).reshape(-1, 6)
        tex = np.concatenate([np.asarray(mesh.texcoords, np.float32), flat]).astype(np.float32)
        if mesh.texnumber is not None:
            pages = np.zeros(len(xyz), np.int32)
            pages[f.ravel()] = np.repeat(np.asarray(mesh.texnumber, np.int32), 3)
            page = np.concatenate([np.asarray(mesh.texnumber, np.int32), pages[caps[:, 0]]]).astype(np.int32)
    return MeshData(mesh.vertices, np.vstack([f, caps]), tex, list(mesh.comments), page)


def _loops(start: np.ndarray, end: np.ndarray) -> list[np.ndarray]:
    """The closed walks of directed edges ``start -> end`` (each edge used once); chains that do not
    close are left out."""
    nxt: dict[int, list[int]] = {}
    for s0, e0 in zip(start.tolist(), end.tolist()):
        nxt.setdefault(s0, []).append(e0)
    loops = []
    while nxt:
        first = next(iter(nxt))
        ring, v = [first], first
        while True:
            outs = nxt.get(v)
            if not outs:
                break
            w = outs.pop()
            if not outs:
                del nxt[v]
            if w == first:
                loops.append(np.asarray(ring, np.int64))
                break
            ring.append(w)
            v = w
            if len(ring) > len(start) + 1:  # never: every edge is used once
                break
    return [r for r in loops if len(r) >= 3]


def _interpolate(vertices: np.ndarray, a: np.ndarray, b: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Vertices between ``vertices[a]`` and ``vertices[b]`` at ``t`` (0 at a), every property
    interpolated (integers rounded)."""
    out = np.empty(len(a), vertices.dtype)
    for name in vertices.dtype.names or ():
        va = vertices[name][a].astype(np.float64)
        vb = vertices[name][b].astype(np.float64)
        tt = t.reshape((-1,) + (1,) * (va.ndim - 1))
        value = va + tt * (vb - va)
        kind = vertices.dtype[name].base.kind
        if kind in "iub":
            info = np.iinfo(vertices.dtype[name].base) if kind != "b" else None
            value = np.rint(value)
            if info is not None:
                value = np.clip(value, info.min, info.max)
        out[name] = value.astype(vertices.dtype[name].base)
    return out


def slab(mesh, point, normal, thickness: float, cap: bool = False):
    """The slice of ``mesh`` between the two planes parallel to the given one at
    ``thickness`` / 2 on either side (``cap``: solids closed on both)."""
    n = np.asarray(normal, np.float64)
    p = np.asarray(point, np.float64)
    half = float(thickness) / 2
    if half <= 0:
        raise ValueError("the thickness must be positive")
    part = clip(mesh, p - half * n, n, "above", cap=cap)
    return clip(part, p + half * n, n, "below", cap=cap) if len(part.faces) else part


def series(mesh, point, normal, thickness: float, step: float, count: int | None = None, cap: bool = False):
    """Slices of ``thickness`` every ``step`` along the normal, the first centred on ``point``;
    ``count`` None: as many as the model spans on both sides. Returns ``[(offset, mesh)]`` with
    the slices that have faces."""
    n = np.asarray(normal, np.float64)
    p = np.asarray(point, np.float64)
    if step <= 0:
        raise ValueError("the step must be positive")
    d = (mesh.xyz - p) @ n
    if count is None:
        k0, k1 = int(np.floor((d.min() - thickness / 2) / step)), int(np.ceil((d.max() + thickness / 2) / step))
        offsets = [k * step for k in range(k0, k1 + 1)]
    else:
        offsets = [k * step for k in range(int(count))]
    out = []
    for off in offsets:
        if d.max() < off - thickness / 2 or d.min() > off + thickness / 2:
            continue
        part = slab(mesh, p + off * n, n, thickness, cap=cap)
        if len(part.faces):
            out.append((float(off), part))
    return out


# ----------------------------------------------------------------------------------- the model
def cut_model(chunk, point, normal, keep: str = "above", thickness: float | None = None,
              step: float | None = None, count: int | None = None, out=None, as_version: bool = True,
              name: str | None = None, cap: bool = True, progress: Progress | None = None) -> dict:
    """Cut the model of ``chunk`` with the world plane ``point``/``normal``:

    * ``keep`` "above" / "below": one side; "slab": the slice of ``thickness`` around the plane;
      without ``out`` it becomes the model of the chunk (the old one kept as a version when
      ``as_version``, named ``name``), with ``out`` (a .ply or .obj file) it is written there and the
      model stays as it is;
    * "both": the two sides, written as ``<out>_sopra`` and ``<out>_sotto`` (``out`` a file name);
    * "series": slices of ``thickness`` every ``step`` (``count`` of them, None = across the whole
      model), written in the folder ``out`` as ``fetta_+0.500.ply`` and so on (offset along the
      normal in metres).

    ``cap``: the solids cut (stratigraphic units, proxies, a vessel) are closed on the plane, each in
    its colour; open surfaces stay open. Returns a report: faces before and after, the files
    written, the version kept."""
    from ..core import versions
    from ..engine.cleaning import store_model
    from ..io.mesh_ply import read_mesh

    progress = as_progress(progress)
    if keep not in KEEP:
        raise ValueError(f"keep: one of {', '.join(KEEP)}")
    path = chunk.product_path("model")
    if path is None or not path.exists():
        raise RuntimeError("build the model first")
    progress(0.0, "Reading the model")
    mesh = read_mesh(path)
    centre = chunk.to_world(mesh.xyz.mean(axis=0)[None])[0] if len(mesh.xyz) else None
    p, n = to_internal(chunk, point, normal, near=centre)
    scale = chunk.scale or 1.0
    report = {"faces": int(len(mesh.faces)), "files": [], "kept": None, "keep": keep}
    progress(0.2, "Cutting the model")
    if keep in ("above", "below", "slab"):
        if keep == "slab":
            if not thickness:
                raise ValueError("a slab needs its thickness")
            part = slab(mesh, p, n, float(thickness) / scale, cap=cap)
        else:
            part = clip(mesh, p, n, keep, cap=cap)
        report["faces_kept"] = int(len(part.faces))
        if not len(part.faces):
            raise ValueError("no face of the model on that side of the plane")
        if out is None:
            kept = versions.keep_active(chunk, "model", move=False) if as_version else None
            try:
                store_model(chunk, part, texture_kept=True)
            except BaseException:
                if kept is not None:
                    versions.delete(chunk, "model", kept["id"])
                raise
            chunk.meta.setdefault("model", {})["cut"] = {"keep": keep, "point": [float(v) for v in point],
                                                          "normal": [float(v) for v in normal],
                                                          "thickness": thickness}
            versions.set_active_name(chunk, "model", name or _default_name(keep))
            report["kept"] = kept["id"] if kept else None
        else:
            report["files"].append(str(write_part(chunk, part, out)))
    elif keep == "both":
        if out is None:
            raise ValueError("both parts are written to files: give out")
        out = Path(out)
        suffix = out.suffix or ".ply"
        for side, label in (("above", "sopra"), ("below", "sotto")):
            part = clip(mesh, p, n, side, cap=cap)
            if len(part.faces):
                report["files"].append(str(write_part(chunk, part, out.with_name(f"{out.stem}_{label}{suffix}"))))
        report["faces_kept"] = None
    else:
        if out is None or not thickness or not step:
            raise ValueError("a series needs thickness, step and the folder out")
        folder = Path(out)
        folder.mkdir(parents=True, exist_ok=True)
        parts = series(mesh, p, n, float(thickness) / scale, float(step) / scale, count, cap=cap)
        for k, (off, part) in enumerate(parts):
            progress.check()
            progress(0.3 + 0.7 * k / max(len(parts), 1), f"Slice {k + 1}/{len(parts)}")
            report["files"].append(str(write_part(chunk, part, folder / f"fetta_{off * scale:+.3f}.ply")))
        report["faces_kept"] = None
    progress(1.0, f"Model cut: {len(report['files'])} files" if report["files"] else "Model cut")
    return report


def _default_name(keep: str) -> str:
    from ..core import versions

    return versions.label({"above": "Tagliato: parte sopra il piano", "below": "Tagliato: parte sotto il piano",
                           "slab": "Tagliato: fetta"}[keep])


def write_part(chunk, mesh, path) -> Path:
    """A part of the model (internal frame) written in world coordinates: PLY with its properties and
    texture pages, OBJ with the texture, other formats through trimesh (see
    :func:`io.exports.export_mesh`)."""
    from ..io.exports import export_mesh

    path = Path(path)
    os.makedirs(path.parent, exist_ok=True)
    return export_mesh(chunk, mesh, path)


__all__ = ["horizontal", "vertical", "through_points", "dipping", "to_internal", "clip", "close_cut", "slab",
           "series", "cut_model", "write_part", "KEEP"]
