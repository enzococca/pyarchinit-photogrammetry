"""The points of a whole cloud for picking on the 3D view: a grid index of the dense cloud (or of the tie points) at
full resolution, built once per file and kept, so that a pick refined on the cloud reads only the points near the
line of sight instead of all of them.

The view shows a sample of a big cloud (at most a few million points); a click is placed first on the points shown,
then refined on the plane of the whole cloud along the line of sight (:meth:`.model_view.ModelView.pick_point`). The
index makes that refinement cost the same on a cloud of a hundred million points as on a small one: the points are
sorted by the cell of a regular grid (about ``CELLS`` cells along the largest side of the cloud) and kept in single
precision relative to their centre (internal coordinates of a site: a tenth of a millimetre at a kilometre); the
points near a line of sight are those of the cells that its tube crosses. A big cloud is indexed in a worker thread
(read from the memory-mapped PLY, never whole in double precision): the view does not wait for it."""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np

CELLS = 192  # cells of the grid along the largest side of the cloud
MAX_QUERY_CELLS = 400_000  # a query never enumerates more cells: a shorter stretch of the line of sight when needed
SYNC_POINTS = 2_000_000  # a cloud with up to this many points: indexed at once; a bigger one in the background
BLOCK = 4_000_000  # points read and keyed at a time


def cloud_source(chunk):
    """(key, path, name) of the cloud a pick is refined on: the dense cloud, else the tie points; (None, None, None)
    without one. The key changes when the file is written again."""
    for name in ("point_cloud", "tie_points"):
        path = chunk.product_path(name) if chunk is not None else None
        if path is not None and path.exists():
            st = path.stat()
            return (name, str(path), st.st_mtime_ns, st.st_size), path, name
    return None, None, None


def point_count(path) -> int | None:
    """The points of a binary PLY cloud from its header, None for another file."""
    from ..core.pointcloud import _read_binary_ply_vertices

    try:
        arr = _read_binary_ply_vertices(Path(path), mmap=True)
    except (OSError, ValueError, KeyError):
        return None
    return None if arr is None else len(arr)


def _blocks(path, chunk=None, name=None):
    """The coordinates of the cloud ``path`` ((k, 3) float64 blocks): strided reads of the memory-mapped PLY, or the
    whole cloud loaded for another format."""
    from ..core.pointcloud import PointCloud, _read_binary_ply_vertices

    arr = None
    try:
        arr = _read_binary_ply_vertices(Path(path), mmap=True)
    except (OSError, ValueError, KeyError):
        arr = None
    if arr is not None and {"x", "y", "z"} <= set(arr.dtype.names or ()):
        n = len(arr)

        def gen():
            for s in range(0, n, BLOCK):
                part = arr[s:s + BLOCK]
                yield np.column_stack([part["x"], part["y"], part["z"]]).astype(np.float64)

        return n, gen
    xyz = np.asarray(PointCloud.load(path).xyz, np.float64)

    def whole():
        for s in range(0, len(xyz), BLOCK):
            yield xyz[s:s + BLOCK]

    return len(xyz), whole


class CloudIndex:
    """The points of a cloud sorted by the cell of a regular grid (see the module). ``near_line`` gives the points
    within a distance of a stretch of a line, in internal coordinates."""

    def __init__(self, n: int, blocks, cells: int = CELLS):
        lo, hi, total = np.full(3, np.inf), np.full(3, -np.inf), np.zeros(3)
        for b in blocks():
            if len(b):
                lo, hi = np.minimum(lo, b.min(axis=0)), np.maximum(hi, b.max(axis=0))
                total += b.sum(axis=0)
        self.n = int(n)
        if not self.n or not np.all(np.isfinite(lo)):
            self.centre, self.cell, self.lo = np.zeros(3), 1.0, np.zeros(3)
            self.dims = np.ones(3, np.int64)
            self.points, self.keys, self.starts = np.zeros((0, 3), np.float32), np.zeros(0, np.int64), np.zeros(1, np.int64)
            return
        self.centre = total / self.n
        span = np.maximum(hi - lo, 1e-9)
        self.cell = max(float(span.max()) / cells, 1e-9)
        self.lo = lo
        self.dims = np.floor(span / self.cell).astype(np.int64) + 1
        keys = np.empty(self.n, np.int64)
        s = 0
        for b in blocks():
            keys[s:s + len(b)] = self._key(self._cell_of(b))
            s += len(b)
        order = np.argsort(keys, kind="stable")
        keys = keys[order]
        # where each point of the file goes in the sorted array (the inverse permutation), then the points scattered
        # there block by block: the file is never held whole in double precision
        rank = np.empty(self.n, np.int32 if self.n < 2 ** 31 else np.int64)
        rank[order] = np.arange(self.n, dtype=rank.dtype)
        del order
        self.points = np.empty((self.n, 3), np.float32)
        s = 0
        for b in blocks():
            k = len(b)
            self.points[rank[s:s + k]] = (b - self.centre).astype(np.float32)
            s += k
        del rank
        first = np.concatenate([[0], np.flatnonzero(np.diff(keys)) + 1])
        self.keys = keys[first]
        self.starts = np.concatenate([first, [self.n]]).astype(np.int64)
        del keys

    def _cell_of(self, xyz) -> np.ndarray:
        return np.clip(np.floor((np.asarray(xyz, np.float64) - self.lo) / self.cell).astype(np.int64), 0,
                       self.dims - 1)

    def _key(self, c) -> np.ndarray:
        return c[..., 2] + self.dims[2] * (c[..., 1] + self.dims[1] * c[..., 0])

    @property
    def nbytes(self) -> int:
        return int(self.points.nbytes + self.keys.nbytes + self.starts.nbytes)

    def near_line(self, origin, direction, middle: float, half: float, radius: float) -> np.ndarray:
        """The points (internal, float64) within ``radius`` of the stretch of the line ``origin + t direction`` with
        ``t`` in ``middle +- half`` (a shorter stretch about ``middle`` when it would cross too many cells)."""
        o, d = np.asarray(origin, np.float64), np.asarray(direction, np.float64)
        d = d / max(np.linalg.norm(d), 1e-300)
        if not len(self.points) or radius <= 0:
            return np.zeros((0, 3))
        reach = radius + 0.87 * self.cell  # a cell whose centre is this close may hold a point within radius
        for _ in range(8):
            a, b = o + (middle - half) * d, o + (middle + half) * d
            c0 = self._cell_of(np.minimum(a, b) - reach)
            c1 = self._cell_of(np.maximum(a, b) + reach)
            count = int(np.prod(c1 - c0 + 1))
            if count <= MAX_QUERY_CELLS:
                break
            half /= 2.0
        else:
            return np.zeros((0, 3))
        axes = [np.arange(c0[k], c1[k] + 1) for k in range(3)]
        cx, cy, cz = np.meshgrid(*axes, indexing="ij")
        cells = np.column_stack([cx.ravel(), cy.ravel(), cz.ravel()])
        centres = self.lo + (cells + 0.5) * self.cell
        v = centres - a
        t = np.clip(v @ d, 0.0, 2 * half)
        dist = np.linalg.norm(v - t[:, None] * d, axis=1)
        cells = cells[dist <= reach]
        keys = np.sort(self._key(cells))
        i = np.searchsorted(self.keys, keys)
        found = (i < len(self.keys))
        found[found] = self.keys[i[found]] == keys[found]
        i = i[found]
        if not len(i):
            return np.zeros((0, 3))
        lo, hi = self.starts[i], self.starts[i + 1]
        lens = hi - lo
        idx = np.repeat(lo - np.concatenate([[0], np.cumsum(lens)[:-1]]), lens) + np.arange(int(lens.sum()))
        pts = self.points[idx].astype(np.float64) + self.centre
        v = pts - o
        t = v @ d
        near = (np.linalg.norm(v - t[:, None] * d, axis=1) <= radius) & (np.abs(t - middle) <= half + radius)
        return pts[near]


def build_index(path, chunk=None, name=None) -> CloudIndex | None:
    """The index of the cloud file ``path`` (None when it cannot be read)."""
    try:
        n, blocks = _blocks(path, chunk, name)
        return CloudIndex(n, blocks)
    except (OSError, ValueError, KeyError, MemoryError):
        return None


def big(path) -> bool:
    """True when the cloud is indexed in the background (more than ``SYNC_POINTS`` points, or an unknown count of a
    file that is not a binary PLY and larger than about that many points would be)."""
    n = point_count(path)
    if n is not None:
        return n > SYNC_POINTS
    try:
        return os.path.getsize(path) > SYNC_POINTS * 16
    except OSError:
        return False


PIECE_FACES = 500_000  # a model is cast on in pieces of this many faces (see PiecewiseCaster)


def _spread10(v: np.ndarray) -> np.ndarray:
    """The 10 low bits of ``v`` spread to every third bit (for a Morton code)."""
    v = v.astype(np.int64) & 0x3FF
    v = (v | (v << 16)) & 0x030000FF
    v = (v | (v << 8)) & 0x0300F00F
    v = (v | (v << 4)) & 0x030C30C3
    v = (v | (v << 2)) & 0x09249249
    return v


class PiecewiseCaster:
    """First-hit ray casting on a big mesh split in spatially compact pieces of ``PIECE_FACES`` faces (Morton order
    of their centroids), each with its own :class:`..engine.visibility.Raycaster` and its bounding box.

    Open3D builds the search tree of a mesh holding the interpreter lock (about 0.2 s per million faces here): one
    tree for 50 million faces would freeze the interface for about ten seconds even when built in a worker thread.
    Built piece by piece, the interface waits at most the time of one piece (~0.1 s), between which it runs. A ray
    is cast only on the pieces whose box it crosses; the first hit is the nearest of theirs."""

    def __init__(self, vertices, faces, piece: int = PIECE_FACES):
        from ..engine.visibility import Raycaster

        vertices = np.asarray(vertices, np.float32)
        faces = np.asarray(faces)
        self.pieces = []  # (lo, hi, raycaster)
        if len(faces) <= piece:
            self._add(Raycaster, vertices, faces)
            return
        lo, hi = vertices.min(axis=0), vertices.max(axis=0)
        span = np.maximum(hi - lo, 1e-9)
        keys = np.empty(len(faces), np.int64)
        for s in range(0, len(faces), BLOCK):
            c = vertices[faces[s:s + BLOCK]].mean(axis=1)
            q = np.clip(((c - lo) / span * 1023).astype(np.int64), 0, 1023)
            keys[s:s + len(c)] = _spread10(q[:, 0]) | (_spread10(q[:, 1]) << 1) | (_spread10(q[:, 2]) << 2)
        order = np.argsort(keys)
        del keys
        for s in range(0, len(faces), piece):
            part = faces[order[s:s + piece]]
            used, inverse = np.unique(part, return_inverse=True)
            self._add(Raycaster, vertices[used], inverse.reshape(-1, 3).astype(np.uint32))

    def _add(self, Raycaster, v, f) -> None:
        caster = Raycaster(v, f)
        caster.hit_distance(np.zeros((1, 3)), np.array([[0.0, 0.0, 1.0]]))  # its tree built now
        self.pieces.append((v.min(axis=0).astype(np.float64), v.max(axis=0).astype(np.float64), caster))

    def hit_distance(self, origins: np.ndarray, directions: np.ndarray) -> np.ndarray:
        """Distance to the first hit along unit ``directions`` (inf if none)."""
        o = np.asarray(origins, np.float64).reshape(-1, 3)
        d = np.asarray(directions, np.float64).reshape(-1, 3)
        best = np.full(len(o), np.inf)
        if len(self.pieces) == 1:
            return np.minimum(best, self.pieces[0][2].hit_distance(o, d))
        with np.errstate(divide="ignore", invalid="ignore"):
            inv = 1.0 / d
        for lo, hi, caster in self.pieces:
            pad = 1e-6 * max(float(np.max(hi - lo)), 1.0)
            with np.errstate(invalid="ignore"):
                t1, t2 = (lo - pad - o) * inv, (hi + pad - o) * inv
            near = np.nan_to_num(np.minimum(t1, t2), nan=-np.inf).max(axis=1)
            far = np.nan_to_num(np.maximum(t1, t2), nan=np.inf).min(axis=1)
            m = (far >= np.maximum(near, 0.0)) & (np.maximum(near, 0.0) < best)
            if m.any():
                best[m] = np.minimum(best[m], caster.hit_distance(o[m], d[m]))
        return best
