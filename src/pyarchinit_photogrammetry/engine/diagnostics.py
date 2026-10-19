"""Diagnostics of the alignment: which photos were matched with which (the matching graph), the
points matched between two photos, the residuals of the tie points in a photo.

The matches come from the COLMAP database of :func:`engine.sfm.match_photos` (``keypoints``,
``matches`` = all the candidate matches, ``two_view_geometries`` = those that fit the geometry of
the pair; pair id ``id1 * 2147483647 + id2`` with ``id1 < id2``), so a photo that did not align
can be looked at too; the tie points and their residuals from the aligned model. Pixels in
COLMAP's convention (the centre of the first pixel at 0.5, 0.5), as the projections of the markers.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np

from ..core.chunk import Chunk

PAIR = 2147483647


def _database(chunk: Chunk) -> Path:
    path = chunk.data_dir / "sfm" / "database.db"
    if not path.exists():
        raise RuntimeError("match the photos first (Allinea foto)")
    return path


def _image_ids(con, chunk: Chunk) -> dict[int, int]:
    """COLMAP image id -> camera key."""
    names = {v: int(k) for k, v in ((chunk.meta.get("sfm") or {}).get("names") or {}).items()}
    return {int(i): names[n] for i, n in con.execute("SELECT image_id, name FROM images") if n in names}


def match_graph(chunk: Chunk, verified: bool = True) -> dict[tuple[int, int], int]:
    """``{(camera key a, camera key b): matches}`` of every matched pair (a < b): the matches that
    fit the geometry of the pair (``verified``), else all the candidates."""
    con = sqlite3.connect(str(_database(chunk)))
    try:
        keys = _image_ids(con, chunk)
        table = "two_view_geometries" if verified else "matches"
        out = {}
        for pair_id, rows in con.execute(f"SELECT pair_id, rows FROM {table} WHERE rows > 0"):
            a, b = divmod(int(pair_id), PAIR)
            if a in keys and b in keys:
                ka, kb = sorted((keys[a], keys[b]))
                out[(ka, kb)] = int(rows)
        return out
    finally:
        con.close()


def components(keys, graph: dict[tuple[int, int], int], min_matches: int = 1) -> list[list[int]]:
    """Groups of photos linked by pairs with at least ``min_matches`` matches, largest first."""
    parent = {k: k for k in keys}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for (a, b), n in graph.items():
        if n >= min_matches and a in parent and b in parent:
            parent[find(a)] = find(b)
    groups: dict[int, list[int]] = {}
    for k in keys:
        groups.setdefault(find(k), []).append(k)
    return sorted(groups.values(), key=len, reverse=True)


def pair_matches(chunk: Chunk, key_a: int, key_b: int) -> dict:
    """The points matched between two photos: ``{"a": keypoints of a (n, 2), "b": of b (m, 2),
    "candidates": (k, 2) indices into a and b, "verified": (j, 2) those that fit the geometry}``."""
    con = sqlite3.connect(str(_database(chunk)))
    try:
        ids = {v: k for k, v in _image_ids(con, chunk).items()}
        if key_a not in ids or key_b not in ids:
            raise KeyError("photo not in the matching")
        ia, ib = ids[key_a], ids[key_b]
        swap = ia > ib
        lo, hi = (ib, ia) if swap else (ia, ib)

        def keypoints(i):
            row = con.execute("SELECT rows, cols, data FROM keypoints WHERE image_id = ?", (i,)).fetchone()
            if not row or not row[0]:
                return np.zeros((0, 2))
            return np.frombuffer(row[2], np.float32).reshape(row[0], row[1])[:, :2].astype(float)

        def matches(table):
            row = con.execute(f"SELECT rows, cols, data FROM {table} WHERE pair_id = ?", (lo * PAIR + hi,)).fetchone()
            if not row or not row[0] or not row[2]:
                return np.zeros((0, 2), int)
            m = np.frombuffer(row[2], np.uint32).reshape(row[0], row[1]).astype(int)
            return m[:, ::-1] if swap else m

        return {"a": keypoints(ia), "b": keypoints(ib), "candidates": matches("matches"),
                "verified": matches("two_view_geometries")}
    finally:
        con.close()


def shared_tie_points(chunk: Chunk, key_a: int, key_b: int) -> tuple[np.ndarray, np.ndarray]:
    """Pixels ``(xy in a (n, 2), xy in b (n, 2))`` of the tie points seen by both aligned photos."""
    from .sfm import load_reconstruction

    rec = load_reconstruction(chunk)
    names = (chunk.meta.get("sfm") or {}).get("names") or {}
    by_name = {im.name: im for im in rec.images.values()}
    ia, ib = by_name.get(names.get(str(key_a))), by_name.get(names.get(str(key_b)))
    if ia is None or ib is None:
        return np.zeros((0, 2)), np.zeros((0, 2))
    in_b = {p.point3D_id: np.asarray(p.xy, float) for p in ib.points2D if p.has_point3D()}
    pairs = [(np.asarray(p.xy, float), in_b[p.point3D_id]) for p in ia.points2D
             if p.has_point3D() and p.point3D_id in in_b]
    if not pairs:
        return np.zeros((0, 2)), np.zeros((0, 2))
    return np.array([a for a, _ in pairs]), np.array([b for _, b in pairs])


def residuals(chunk: Chunk, key: int) -> tuple[np.ndarray, np.ndarray]:
    """Of the tie points of an aligned photo: where they were measured and where their 3D point
    projects, ``(measured (n, 2), projected (n, 2))`` pixels."""
    from .sfm import load_reconstruction

    rec = load_reconstruction(chunk)
    name = ((chunk.meta.get("sfm") or {}).get("names") or {}).get(str(key))
    image = next((im for im in rec.images.values() if im.name == name), None)
    if image is None or not image.has_pose:
        return np.zeros((0, 2)), np.zeros((0, 2))
    pts = [(np.asarray(p.xy, float), rec.points3D[p.point3D_id].xyz) for p in image.points2D
           if p.has_point3D() and rec.exists_point3D(p.point3D_id)]
    if not pts:
        return np.zeros((0, 2)), np.zeros((0, 2))
    measured = np.array([m for m, _ in pts])
    X = np.array([x for _, x in pts])
    cfw = image.cam_from_world()
    xc = X @ np.asarray(cfw.rotation.matrix()).T + np.asarray(cfw.translation)
    projected = np.asarray(rec.cameras[image.camera_id].img_from_cam(xc), float)
    return measured, projected
