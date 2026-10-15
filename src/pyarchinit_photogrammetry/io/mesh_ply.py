"""Large binary PLY meshes without loading them into Python objects.

Metashape's ``mesh.ply`` can hold tens of millions of faces (3 GB for a whole tell). The
vertex block and the face block of a binary little-endian PLY are memory-mapped
(:class:`PlyMesh`), so vertices can be sampled for a point cloud and faces processed in
chunks. :func:`cluster_decimate` makes a coarse copy for display by vertex clustering
(every vertex moves to the mean of its grid cell, degenerate and duplicate faces are
dropped): the shape and the colours are kept, unlike a random subset of faces, which
leaves holes everywhere.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

_TYPES = {"float": "<f4", "float32": "<f4", "double": "<f8", "float64": "<f8", "uchar": "u1", "uint8": "u1",
          "char": "i1", "int8": "i1", "ushort": "<u2", "uint16": "<u2", "short": "<i2", "int16": "<i2",
          "uint": "<u4", "uint32": "<u4", "int": "<i4", "int32": "<i4"}


def read_header(f) -> tuple[int, list[dict]]:
    """Header of a binary little-endian PLY (file object at its start): the byte offset of
    the data and the elements ``{"name", "count", "props"}``; a list property is
    ``(name, ("list", count_type, item_type))``."""
    lines, size = [], 0
    while True:
        line = f.readline()
        if not line:
            raise ValueError("not a PLY file (no end_header)")
        size += len(line)
        text = line.decode("ascii", "replace").strip()
        lines.append(text)
        if text == "end_header":
            break
        if size > 1 << 20:
            raise ValueError("PLY header too long")
    if not lines or lines[0] != "ply":
        raise ValueError("not a PLY file")
    fmt = next((ln.split()[1] for ln in lines if ln.startswith("format ")), "")
    if fmt != "binary_little_endian":
        raise ValueError(f"PLY format {fmt!r}: only binary_little_endian is read in streaming")
    elements: list[dict] = []
    for ln in lines:
        p = ln.split()
        if p[:1] == ["element"]:
            elements.append({"name": p[1], "count": int(p[2]), "props": []})
        elif p[:1] == ["property"] and elements:
            if p[1] == "list":
                elements[-1]["props"].append((p[4], ("list", _TYPES[p[2]], _TYPES[p[3]])))
            else:
                elements[-1]["props"].append((p[2], _TYPES[p[1]]))
    return size, elements


class PlyMesh:
    """Memory-mapped vertices and faces of a binary little-endian triangle mesh whose face
    lists have a constant length (3 indices, and e.g. 6 texture coordinates), as written by
    Metashape, Open3D and this program."""

    def __init__(self, path: str | Path, check: int = 20000):
        self.path = Path(path)
        with open(self.path, "rb") as f:
            offset, elements = read_header(f)
            names = [e["name"] for e in elements]
            if names[:2] != ["vertex", "face"]:
                raise ValueError(f"{self.path.name}: expected vertex and face elements, found {names}")
            vel, fel = elements[0], elements[1]
            if any(isinstance(t, tuple) for _, t in vel["props"]):
                raise ValueError("list properties on vertices are not supported")
            self.vertex_dtype = np.dtype([(n, t) for n, t in vel["props"]])
            f.seek(offset + vel["count"] * self.vertex_dtype.itemsize)
            fields = []
            for name, t in fel["props"]:  # list lengths read from the first face record
                if isinstance(t, tuple):
                    cdt, idt = np.dtype(t[1]), np.dtype(t[2])
                    cnt = int(np.frombuffer(f.read(cdt.itemsize), dtype=cdt)[0])
                    f.seek(cnt * idt.itemsize, 1)
                    fields += [(f"_n_{name}", cdt), (name, idt, (cnt,))]
                else:
                    f.seek(np.dtype(t).itemsize, 1)
                    fields.append((name, t))
        self.face_dtype = np.dtype(fields)
        self.n_vertices, self.n_faces = vel["count"], fel["count"]
        self.data_offset = offset  # byte offset of the vertex block
        self.vertices = np.memmap(self.path, dtype=self.vertex_dtype, mode="r", offset=offset,
                                  shape=(self.n_vertices,))
        self._faces = np.memmap(self.path, dtype=self.face_dtype, mode="r",
                                offset=offset + self.n_vertices * self.vertex_dtype.itemsize, shape=(self.n_faces,))
        idx_name = next((n for n, _ in fel["props"] if n in ("vertex_indices", "vertex_index")), None)
        if idx_name is None or self.face_dtype[idx_name].shape != (3,):
            raise ValueError("only triangle meshes are read in streaming")
        self.index_field = idx_name
        if self.n_faces:  # a variable-length record would shift the following ones: sample them
            probe = np.unique(np.linspace(0, self.n_faces - 1, min(check, self.n_faces)).astype(np.int64))
            for name in self.face_dtype.names:
                if name.startswith("_n_"):
                    expected = self.face_dtype[name[3:]].shape[0]
                    if not np.all(self._faces[name][probe] == expected):
                        raise ValueError("face lists of different lengths: not readable in streaming")

    @property
    def has_colors(self) -> bool:
        return {"red", "green", "blue"} <= set(self.vertex_dtype.names)

    def xyz(self, index=None) -> np.ndarray:
        v = self.vertices if index is None else self.vertices[index]
        return np.column_stack([v["x"], v["y"], v["z"]]).astype(np.float64)

    def colors(self, index=None) -> np.ndarray | None:
        if not self.has_colors:
            return None
        v = self.vertices if index is None else self.vertices[index]
        return np.column_stack([v["red"], v["green"], v["blue"]]).astype(np.uint8)

    def face_chunks(self, size: int = 4_000_000):
        """Vertex indices of the faces, ``size`` faces at a time (int64 arrays)."""
        for start in range(0, self.n_faces, size):
            yield self._faces[self.index_field][start:start + size].astype(np.int64)


def transform_vertices(path: str | Path, fn, block: int = 2_000_000) -> int:
    """Apply ``fn`` ((k, 3) float64 -> (k, 3)) to the vertex coordinates of a binary PLY
    mesh, in place and block by block; faces are not touched. Returns the vertex count."""
    mesh = PlyMesh(path, check=0)
    mm = np.memmap(mesh.path, dtype=mesh.vertex_dtype, mode="r+", offset=mesh.data_offset, shape=(mesh.n_vertices,))
    for start in range(0, mesh.n_vertices, block):
        blk = mm[start:start + block]
        q = np.asarray(fn(np.column_stack([blk["x"], blk["y"], blk["z"]]).astype(np.float64)))
        blk["x"], blk["y"], blk["z"] = q[:, 0], q[:, 1], q[:, 2]
    mm.flush()
    del mm
    return mesh.n_vertices


def face_count(path: str | Path) -> int | None:
    """Number of faces of a binary PLY mesh, from its header (None for other files)."""
    try:
        with open(path, "rb") as f:
            _, elements = read_header(f)
    except (OSError, ValueError, KeyError):
        return None
    return next((e["count"] for e in elements if e["name"] == "face"), None)


def cluster_decimate(xyz: np.ndarray, face_chunks, colors: np.ndarray | None = None, target_faces: int = 600_000,
                     progress=None):
    """Vertex clustering of a mesh to about ``target_faces`` faces. ``face_chunks`` is a
    function returning an iterator of (k, 3) index arrays (called once per pass). Returns
    vertices, faces and colours (or None)."""
    xyz = np.asarray(xyz, np.float64)
    lo, hi = xyz.min(axis=0), xyz.max(axis=0)
    span = np.maximum(hi - lo, 1e-9)
    area = 0.0
    for F in face_chunks():
        a, b, c = xyz[F[:, 0]], xyz[F[:, 1]], xyz[F[:, 2]]
        area += 0.5 * float(np.linalg.norm(np.cross(b - a, c - a), axis=1).sum())
    voxel = np.sqrt(2.0 * area / max(target_faces, 1)) if area > 0 else float(span.max()) / np.sqrt(max(target_faces, 1))
    voxel = max(voxel, float(span.max()) * 1e-6)
    for attempt in range(6):
        dims = np.floor(span / voxel).astype(np.int64) + 1
        q = np.minimum(np.floor((xyz - lo) / voxel).astype(np.int64), dims - 1)
        keys = q[:, 0] + dims[0] * (q[:, 1] + dims[1] * q[:, 2])
        uniq, inv = np.unique(keys, return_inverse=True)
        inv = inv.ravel()
        parts = []
        for F in face_chunks():
            g = inv[F]
            ok = (g[:, 0] != g[:, 1]) & (g[:, 1] != g[:, 2]) & (g[:, 0] != g[:, 2])
            g = g[ok]
            if len(g):
                _, first = np.unique(np.sort(g, axis=1), axis=0, return_index=True)
                parts.append(g[np.sort(first)])
        G = np.concatenate(parts) if parts else np.zeros((0, 3), np.int64)
        if len(G):
            _, first = np.unique(np.sort(G, axis=1), axis=0, return_index=True)
            G = G[np.sort(first)]
        if progress:
            progress(min(0.95, 0.2 + 0.15 * attempt), f"Anteprima: {len(G)} facce")
        if 0.7 * target_faces <= len(G) <= 1.15 * target_faces or attempt == 5:
            break
        voxel *= np.sqrt(max(len(G), 1) / target_faces)  # coarser if too many faces, finer if too few
    n = len(uniq)
    cnt = np.bincount(inv, minlength=n).astype(np.float64)
    V = np.column_stack([np.bincount(inv, weights=xyz[:, k], minlength=n) / np.maximum(cnt, 1) for k in range(3)])
    C = None
    if colors is not None:
        colors = np.asarray(colors, np.float64)
        C = np.column_stack([np.bincount(inv, weights=colors[:, k], minlength=n) / np.maximum(cnt, 1)
                             for k in range(3)])
        C = np.clip(np.round(C), 0, 255).astype(np.uint8)
    used = np.unique(G)
    remap = np.full(n, -1, np.int64)
    remap[used] = np.arange(len(used))
    return V[used], remap[G], (C[used] if C is not None else None)


def write_mesh_ply(path: str | Path, vertices: np.ndarray, faces: np.ndarray, colors: np.ndarray | None = None) -> Path:
    """Binary little-endian PLY with float32 vertices, optional uchar colours, int faces."""
    path = Path(path)
    vfields = [("x", "<f4"), ("y", "<f4"), ("z", "<f4")] + ([("red", "u1"), ("green", "u1"), ("blue", "u1")]
                                                            if colors is not None else [])
    v = np.empty(len(vertices), dtype=vfields)
    v["x"], v["y"], v["z"] = (np.asarray(vertices)[:, k] for k in range(3))
    if colors is not None:
        v["red"], v["green"], v["blue"] = (np.asarray(colors)[:, k] for k in range(3))
    f = np.empty(len(faces), dtype=[("n", "u1"), ("v", "<i4", (3,))])
    f["n"] = 3
    f["v"] = faces
    head = ["ply", "format binary_little_endian 1.0", f"element vertex {len(v)}"]
    head += [f"property {'float' if t == '<f4' else 'uchar'} {n}" for n, t in vfields]
    head += [f"element face {len(f)}", "property list uchar int vertex_indices", "end_header"]
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as out:
        out.write(("\n".join(head) + "\n").encode("ascii"))
        out.write(v.tobytes())
        out.write(f.tobytes())
    tmp.replace(path)
    return path


def make_preview(model: str | Path, out: str | Path, max_faces: int = 600_000, progress=None) -> Path:
    """A display copy of a large binary PLY mesh with about ``max_faces`` faces."""
    mesh = PlyMesh(model)
    xyz = mesh.xyz()
    V, F, C = cluster_decimate(xyz, mesh.face_chunks, mesh.colors(), max_faces, progress=progress)
    return write_mesh_ply(out, V, F, C)


__all__ = ["PlyMesh", "read_header", "face_count", "transform_vertices", "cluster_decimate", "write_mesh_ply",
           "make_preview"]
