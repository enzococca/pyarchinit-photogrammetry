"""Align chunks and merge chunks (as *Workflow > Align Chunks* and *Merge Chunks*).

Aligning
--------
:func:`align_chunks` brings chunks into the world frame of a reference chunk. Only
``chunk.transform`` changes (and the CRS, which becomes the reference's): photos, tie
points, dense cloud and mesh stay in the chunk's internal frame. The similarity (rotation,
translation and scale, or no scale with ``fix_scale``) comes from:

``markers``  markers with the same label in both chunks (at least 3), least squares;
``cameras``  photos present in both chunks (the same file, or the same label when the label
             is unique in both): least squares on the camera centres, or the rotation from
             the camera orientations when the common photos are 1-2 or lie on a line;
``points``   registration of the dense clouds (the tie points of a chunk without one): an
             initial guess from the common markers, from the georeference (both chunks in a
             projected CRS, or both referenced in the same local grid) or from the common
             photos, else a global registration (FPFH descriptors from Open3D matched with
             RANSAC), refined by a scaled point-to-plane ICP. A result is accepted when most
             of the overlapping points fit and, with colours, the texture agrees on both sides.

Estimates are computed in local frames (world coordinates minus a rounded offset), so
national grid or UTM coordinates (millions of metres) lose no precision. Residuals are in
the units of the reference's world frame (metres once it is georeferenced or scaled).
A reference that is still an unlevelled local frame is levelled first (as building its DEM
would do later), so that the chunks aligned to it keep its frame. The DEM and orthomosaic
of an aligned chunk were written in its old frame: build them again.

Merging
-------
:func:`merge_chunks` makes a new chunk in the frame of the first chunk (its internal frame,
transform and CRS): sensors and cameras with new keys (a photo present in several chunks is
kept once), markers merged by label (projections combined, positions triangulated again),
scale bars, tie points, dense clouds and (optionally) meshes concatenated, and a COLMAP
model of the merged cameras, so depth maps, dense cloud, mesh, DEM and orthomosaic can be
built again on the merged chunk. DEMs, orthomosaics and textures are not merged. The
chunks should share the world frame (aligned or georeferenced in a CRS) before merging.
"""
from __future__ import annotations

import os
import shutil
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

import numpy as np

from ..core import crs as crs_mod
from ..core import geometry
from ..core.camera import Camera, Reference, Sensor
from ..core.marker import Marker, Projection, ScaleBar
from ..core.pointcloud import PointCloud
from ..core.progress import Progress, as_progress

if TYPE_CHECKING:  # pragma: no cover
    from ..core.chunk import Chunk
    from ..core.document import Document

ALIGN_METHODS = ("markers", "points", "cameras")
INITIAL_GUESSES = ("auto", "markers", "reference", "cameras", "global")

# A registration is accepted when, among the points that fall on the other cloud (within
# OVERLAP_RADIUS x the final ICP distance), this fraction lies within the final distance.
MIN_INLIER_RATIO = 0.6
OVERLAP_RADIUS = 5.0
MIN_OVERLAP = 0.02  # and at least this fraction of the points overlaps
MIN_COLOR_CORR = 0.4  # and, with colours, the brightness of the matched points agrees this much
MAX_SCALE_CHANGE = 1.25  # the ICP may change the scale of its initial position by this factor at most


# ------------------------------------------------------------------ helpers
def affine_inverse(m: np.ndarray) -> np.ndarray:
    """Inverse of a 4x4 affine transform (exact for large translations)."""
    m = np.asarray(m, float)
    a = np.linalg.inv(m[:3, :3])
    out = np.eye(4)
    out[:3, :3] = a
    out[:3, 3] = -a @ m[:3, 3]
    return out


def _translation(t) -> np.ndarray:
    m = np.eye(4)
    m[:3, 3] = t
    return m


def _scale_of(m: np.ndarray) -> float:
    return float(np.cbrt(np.linalg.det(np.asarray(m, float)[:3, :3])))


def _rigid_pose(similarity: np.ndarray, pose: np.ndarray) -> np.ndarray:
    """A camera pose (world_from_cam, rigid) moved by a similarity: the centre goes through
    the similarity, the axes turn with its rotation (the scale stays out of the pose)."""
    s = _scale_of(similarity)
    out = np.eye(4)
    out[:3, :3] = (similarity[:3, :3] / s) @ pose[:3, :3]
    out[:3, 3] = geometry.apply(similarity, pose[:3, 3])
    return out


def _spread(points: np.ndarray, ratio: float = 0.05) -> bool:
    """Whether points span a plane (not all close to a line)."""
    if len(points) < 3:
        return False
    s = np.linalg.svd(points - points.mean(0), compute_uv=False)
    return bool(s[0] > 0 and s[1] > ratio * s[0])


def _path_key(path: str) -> str:
    return os.path.normcase(os.path.realpath(str(path)))


def _list(m: np.ndarray) -> list:
    return np.asarray(m, float).tolist()


def resolve_chunk(doc: "Document", item) -> "Chunk":
    """A chunk of ``doc`` from a Chunk, its key (int) or its label (str; a key as text
    also works when no chunk has that label)."""
    from ..core.chunk import Chunk

    if isinstance(item, Chunk):
        if not any(c is item for c in doc.chunks):
            raise ValueError(f"chunk {item.label!r} does not belong to this document")
        return item
    if isinstance(item, (int, np.integer)) and not isinstance(item, bool):
        found = [c for c in doc.chunks if c.key == int(item)]
    else:
        found = [c for c in doc.chunks if c.label == str(item)] or [c for c in doc.chunks if str(c.key) == str(item)]
    if not found:
        raise KeyError(f"no chunk {item!r} in the document")
    return found[0]


def _resolve_many(doc: "Document", items) -> list["Chunk"]:
    out = []
    for item in items:
        ch = resolve_chunk(doc, item)
        if not any(c is ch for c in out):
            out.append(ch)
    return out


class _Frame:
    """Local frame of a chunk: world coordinates minus a rounded offset near its cameras.
    ``L`` maps internal coordinates to the local frame (a similarity with small numbers)."""

    def __init__(self, chunk: "Chunk"):
        self.chunk = chunk
        T = np.asarray(chunk.transform, float)
        self.offset = np.round(geometry.apply(T, _anchor(chunk)))
        self.L = T.copy()
        self.L[:3, 3] -= self.offset
        self.scale = _scale_of(T)

    def local(self, internal: np.ndarray) -> np.ndarray:
        return geometry.apply(self.L, internal)

    def pose(self, camera: Camera) -> np.ndarray:
        return _rigid_pose(self.L, camera.transform)


def _anchor(chunk: "Chunk") -> np.ndarray:
    cams = chunk.aligned_cameras
    if cams:
        return np.median(np.array([c.center for c in cams]), axis=0)
    pos = [m.position for m in chunk.markers if m.position is not None]
    if pos:
        return np.median(np.array(pos), axis=0)
    tp = chunk.tie_points
    if tp is not None and len(tp):
        return np.median(tp.xyz, axis=0)
    return np.zeros(3)


def _new_transform(W: np.ndarray, frame: _Frame, ref_frame: _Frame) -> np.ndarray:
    """``chunk.transform`` after alignment: internal -> local (frame) -> reference local -> world."""
    T = W @ frame.L
    T[:3, 3] += ref_frame.offset
    return T


def _delta(W: np.ndarray, frame: _Frame, ref_frame: _Frame) -> np.ndarray:
    """The same alignment from the old world coordinates of the chunk to the reference's."""
    D = W.copy()
    D[:3, 3] = W[:3, 3] - W[:3, :3] @ frame.offset + ref_frame.offset
    return D


# ------------------------------------------------------------------ correspondences
def marker_position(chunk: "Chunk", marker: Marker) -> np.ndarray | None:
    """Internal position of a marker: the stored one, else triangulated from its
    projections (at least two aligned photos) without changing the marker."""
    if marker.position is not None:
        return np.asarray(marker.position, float)
    cams = {c.key: c for c in chunk.cameras}
    views, obs = [], []
    for key, proj in marker.projections.items():
        cam = cams.get(key)
        if cam is None or not (cam.enabled and cam.aligned):
            continue
        views.append(geometry.PinholeView(cam.transform))
        obs.append(chunk.sensor(cam).unproject([[proj.x, proj.y]])[0])
    if len(views) < 2:
        return None
    return geometry.triangulate(views, np.asarray(obs))


def common_markers(chunk: "Chunk", ref: "Chunk") -> tuple[list[str], np.ndarray, np.ndarray]:
    """Labels and internal positions (in ``chunk`` and in ``ref``) of the markers with the
    same label in both chunks and a position in each."""
    ref_by_label: dict[str, Marker] = {}
    for m in ref.markers:
        ref_by_label.setdefault(m.label, m)
    labels, src, dst, seen = [], [], [], set()
    for m in chunk.markers:
        if m.label in seen or m.label not in ref_by_label:
            continue
        seen.add(m.label)
        p, q = marker_position(chunk, m), marker_position(ref, ref_by_label[m.label])
        if p is None or q is None:
            continue
        labels.append(m.label)
        src.append(p)
        dst.append(q)
    return labels, np.reshape(np.array(src, float), (-1, 3)), np.reshape(np.array(dst, float), (-1, 3))


def common_cameras(chunk: "Chunk", ref: "Chunk") -> list[tuple[Camera, Camera]]:
    """Aligned photos present in both chunks: the same file first, then the same label
    when the label is unique in each chunk (photos copied to another folder)."""
    ref_cams = [c for c in ref.cameras if c.enabled and c.aligned]
    by_path: dict[str, Camera] = {}
    for c in ref_cams:
        by_path.setdefault(_path_key(c.path), c)
    pairs, used_ref, used = [], set(), set()
    for c in chunk.cameras:
        if not (c.enabled and c.aligned):
            continue
        r = by_path.get(_path_key(c.path))
        if r is not None and r.key not in used_ref:
            pairs.append((c, r))
            used_ref.add(r.key)
            used.add(c.key)
    n_chunk = Counter(c.label for c in chunk.cameras)
    n_ref = Counter(c.label for c in ref.cameras)
    by_label = {c.label: c for c in ref_cams if n_ref[c.label] == 1 and c.key not in used_ref}
    for c in chunk.cameras:
        if c.key in used or not (c.enabled and c.aligned) or n_chunk[c.label] != 1:
            continue
        r = by_label.pop(c.label, None)
        if r is not None:
            pairs.append((c, r))
    return pairs


def _rotation_mean(rotations: Iterable[np.ndarray]) -> np.ndarray:
    u, _, vt = np.linalg.svd(np.sum(list(rotations), axis=0))
    d = np.sign(np.linalg.det(u @ vt)) or 1.0
    return u @ np.diag([1.0, 1.0, d]) @ vt


def pose_similarity(src: list[np.ndarray], dst: list[np.ndarray], with_scale: bool = True) -> np.ndarray:
    """Similarity mapping camera poses ``src`` onto ``dst`` (world_from_cam, rigid, same
    photos in two frames): least squares on the centres when they span a plane, else the
    mean rotation of the orientations with scale and translation from the centres."""
    cs = np.array([p[:3, 3] for p in src], float)
    cd = np.array([p[:3, 3] for p in dst], float)
    if len(cs) >= 3 and _spread(cs) and _spread(cd):
        return geometry.umeyama(cs, cd, with_scale=with_scale)
    rot = _rotation_mean(d[:3, :3] @ s[:3, :3].T for s, d in zip(src, dst))
    ms, md = cs.mean(0), cd.mean(0)
    xs, xd = cs - ms, cd - md
    scale = 1.0
    if with_scale:
        den = float((xs ** 2).sum())
        if len(cs) < 2 or den <= 1e-20 * max(1.0, float((ms ** 2).sum())):
            raise ValueError("the scale needs at least two common photos taken from different places "
                             "(or fix the scale)")
        scale = float(np.sum(xd * (xs @ rot.T)) / den)
    return geometry.compose_similarity(scale, rot, md - scale * rot @ ms)


def _residuals(W: np.ndarray, labels: list[str], src: np.ndarray, dst: np.ndarray) -> list[dict]:
    d = geometry.apply(W, src) - dst
    return [{"label": lab, "error": float(np.linalg.norm(v)), "dx": float(v[0]), "dy": float(v[1]),
             "dz": float(v[2])} for lab, v in zip(labels, d)]


def _rmse(res: list[dict]) -> float | None:
    errs = [r["error"] for r in res]
    return float(np.sqrt(np.mean(np.square(errs)))) if errs else None


# ------------------------------------------------------------------ methods
def _align_markers(ch, ref, frame, ref_frame, fix_scale) -> tuple[np.ndarray, dict]:
    labels, src, dst = common_markers(ch, ref)
    if len(labels) < 3:
        raise ValueError(f"{ch.label}: {len(labels)} markers in common with {ref.label} "
                         f"(at least 3 with the same label and a position are needed)")
    src, dst = frame.local(src), ref_frame.local(dst)
    if not (_spread(src, 1e-3) and _spread(dst, 1e-3)):
        raise ValueError(f"{ch.label}: the markers in common with {ref.label} lie on a line")
    W = geometry.umeyama(src, dst, with_scale=not fix_scale)
    res = _residuals(W, labels, src, dst)
    return W, {"method": "markers", "correspondences": len(labels), "residuals": res, "rmse": _rmse(res),
               "max_error": max(r["error"] for r in res)}


def _align_cameras(ch, ref, frame, ref_frame, fix_scale) -> tuple[np.ndarray, dict]:
    pairs = common_cameras(ch, ref)
    if not pairs or (len(pairs) < 2 and not fix_scale):
        raise ValueError(f"{ch.label}: {len(pairs)} aligned photos in common with {ref.label} "
                         f"(at least 2 are needed, 1 with a fixed scale)")
    src = [frame.pose(c) for c, _ in pairs]
    dst = [ref_frame.pose(r) for _, r in pairs]
    W = pose_similarity(src, dst, with_scale=not fix_scale)
    labels = [c.label for c, _ in pairs]
    res = _residuals(W, labels, np.array([p[:3, 3] for p in src]), np.array([p[:3, 3] for p in dst]))
    rot = W[:3, :3] / _scale_of(W)
    for r, s, d in zip(res, src, dst):
        delta = d[:3, :3].T @ rot @ s[:3, :3]
        r["angle_deg"] = float(np.degrees(np.arccos(np.clip((np.trace(delta) - 1) / 2, -1.0, 1.0))))
    return W, {"method": "cameras", "correspondences": len(pairs), "residuals": res, "rmse": _rmse(res),
               "max_error": max(r["error"] for r in res),
               "max_angle_deg": max(r["angle_deg"] for r in res)}


def registration_cloud(chunk: "Chunk", max_points: int, seed: int = 0) -> tuple[np.ndarray, np.ndarray | None, str]:
    """Internal coordinates and colours of the points used to register a chunk (dense
    cloud, or tie points when there is none), randomly thinned to ``max_points``."""
    pc, kind = chunk.point_cloud, "point_cloud"
    if pc is None or len(pc) < 100:
        pc, kind = chunk.tie_points, "tie_points"
    if pc is None or len(pc) < 20:
        raise ValueError(f"{chunk.label}: no dense cloud or tie points to register")
    xyz, rgb = pc.xyz, pc.rgb
    if len(xyz) > max_points:
        pick = np.sort(np.random.default_rng(seed).choice(len(xyz), max_points, replace=False))
        xyz, rgb = xyz[pick], (rgb[pick] if rgb is not None else None)
    return xyz, rgb, kind


def point_spacing(xyz: np.ndarray, sample: int = 5000) -> float:
    """Median distance between a point and its nearest neighbour."""
    from scipy.spatial import cKDTree

    pick = xyz if len(xyz) <= sample else xyz[np.random.default_rng(1).choice(len(xyz), sample, replace=False)]
    d, _ = cKDTree(xyz).query(pick, k=2)
    d = d[:, 1]
    d = d[d > 0]
    return float(np.median(d)) if len(d) else 1e-3


def estimate_normals(xyz: np.ndarray, tree=None, k: int = 12) -> np.ndarray:
    """Unit normals by principal component analysis of the ``k`` nearest neighbours."""
    from scipy.spatial import cKDTree

    tree = tree or cKDTree(xyz)
    k = min(k, len(xyz))
    _, idx = tree.query(xyz, k=k)
    nb = xyz[idx] - xyz[idx].mean(axis=1, keepdims=True)
    cov = np.einsum("nki,nkj->nij", nb, nb)
    _, vec = np.linalg.eigh(cov)
    return vec[:, :, 0]


def _rodrigues(w: np.ndarray) -> np.ndarray:
    theta = float(np.linalg.norm(w))
    if theta < 1e-15:
        return np.eye(3)
    k = w / theta
    kx = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + np.sin(theta) * kx + (1 - np.cos(theta)) * kx @ kx


def icp(src: np.ndarray, dst: np.ndarray, init: np.ndarray, with_scale: bool = True, start: float = 1.0,
        final: float = 0.01, tree=None, normals: np.ndarray | None = None, tangential: float = 0.2,
        max_iter: int = 30, scale_below: float | None = None, progress: Progress | None = None) -> np.ndarray:
    """Similarity (rigid with ``with_scale=False``) registering ``src`` onto ``dst`` from
    ``init``: point-to-plane ICP with a small tangential term (``tangential`` = its weight
    relative to the normal one, it keeps flat scenes from sliding), Tukey weights, and a
    correspondence distance going down from ``start`` to ``final``. The scale is refined
    only once the distance is below ``scale_below`` (default ``4 * final``): estimated
    from loose correspondences, it can shrink the cloud onto a small part of the target."""
    from scipy.spatial import cKDTree

    tree = tree or cKDTree(dst)
    normals = estimate_normals(dst, tree) if normals is None else normals
    M = np.asarray(init, float).copy()
    stages = max(2, int(np.ceil(np.log2(max(start / final, 1.0)))) + 1)
    lam2 = tangential ** 2
    scale_below = 4.0 * final if scale_below is None else scale_below
    for thr in np.geomspace(max(start, final), final, stages):
        nvar = 7 if with_scale and thr <= scale_below * (1 + 1e-9) else 6
        for _ in range(max_iter):
            if progress is not None:
                progress.check()
            p = geometry.apply(M, src)
            d, idx = tree.query(p, k=1, distance_upper_bound=thr)
            ok = np.isfinite(d)
            if ok.sum() < 6:
                break
            p, q, n = p[ok], dst[idx[ok]], normals[idx[ok]]
            w = (1.0 - (d[ok] / thr) ** 2) ** 2
            c = np.average(p, axis=0, weights=w)
            pc = p - c
            radius = float(np.sqrt(np.average(np.sum(pc ** 2, axis=1), weights=w)))
            e = p - q
            # displacement of a point for the unknowns (rotation w, translation t, scale ds)
            J = np.zeros((len(p), 3, nvar))
            J[:, 0, 1], J[:, 0, 2] = pc[:, 2], -pc[:, 1]
            J[:, 1, 0], J[:, 1, 2] = -pc[:, 2], pc[:, 0]
            J[:, 2, 0], J[:, 2, 1] = pc[:, 1], -pc[:, 0]
            J[:, 0, 3] = J[:, 1, 4] = J[:, 2, 5] = 1.0
            if nvar == 7:
                J[:, :, 6] = pc
            a = np.einsum("ni,nij->nj", n, J)  # normal component of the displacement
            en = np.einsum("ni,ni->n", n, e)
            H = lam2 * np.einsum("n,nai,naj->ij", w, J, J) + (1 - lam2) * np.einsum("n,ni,nj->ij", w, a, a)
            g = lam2 * np.einsum("n,nai,na->i", w, J, e) + (1 - lam2) * np.einsum("n,ni,n->i", w, a, en)
            H += np.eye(nvar) * 1e-9 * max(np.trace(H), 1e-30)
            x = np.linalg.solve(H, -g)
            rot = _rodrigues(x[:3])
            ds = float(np.clip(x[6], -0.05, 0.05)) if nvar == 7 else 0.0
            D = np.eye(4)
            D[:3, :3] = (1.0 + ds) * rot
            D[:3, 3] = c + x[3:6] - (1.0 + ds) * rot @ c
            M = D @ M
            step = max(np.linalg.norm(x[:3]) * radius, np.linalg.norm(x[3:6]), abs(ds) * radius)
            if step < 1e-3 * final:  # the largest displacement of a point
                break
    return M


def registration_quality(src: np.ndarray, dst: np.ndarray, M: np.ndarray, final: float, tree=None,
                         normals: np.ndarray | None = None, src_rgb: np.ndarray | None = None,
                         dst_rgb: np.ndarray | None = None) -> dict:
    """Fit of ``src`` moved by ``M`` on ``dst``: ``fitness`` (fraction of the points within
    ``final``), ``overlap`` (fraction within ``OVERLAP_RADIUS * final``), ``inlier_ratio``
    (fitness / overlap), the point-to-plane RMSE of the inliers and, with colours,
    ``color_corr``: correlation of the brightness of the closest pairs (within ``final / 3``)
    — the same texture on both sides when the registration is right, unrelated otherwise."""
    from scipy.spatial import cKDTree

    tree = tree or cKDTree(dst)
    p = geometry.apply(M, src)
    d, idx = tree.query(p, k=1, distance_upper_bound=OVERLAP_RADIUS * final)
    near = np.isfinite(d)
    inl = near & (d <= final)
    out = {"fitness": float(inl.mean()), "overlap": float(near.mean()),
           "inlier_ratio": float(inl.sum() / max(near.sum(), 1)), "inliers": int(inl.sum()), "rmse": None,
           "color_corr": None}
    if inl.any():
        e = p[inl] - dst[idx[inl]]
        if normals is not None:
            e = np.einsum("ni,ni->n", e, normals[idx[inl]])[:, None]
        out["rmse"] = float(np.sqrt(np.mean(np.sum(e ** 2, axis=1))))
    close = near & (d <= final / 3.0)
    if src_rgb is not None and dst_rgb is not None and close.sum() >= 30:
        luma = np.array([0.299, 0.587, 0.114])
        a = src_rgb[close].astype(float) @ luma
        b = dst_rgb[idx[close]].astype(float) @ luma
        if a.std() > 1.0 and b.std() > 1.0:
            out["color_corr"] = float(np.corrcoef(a, b)[0, 1])
    return out


def _accepted(q: dict) -> bool:
    return (q["inlier_ratio"] >= MIN_INLIER_RATIO and q["overlap"] >= MIN_OVERLAP
            and (q["color_corr"] is None or q["color_corr"] >= MIN_COLOR_CORR))


def _convincing(q: dict) -> bool:
    """A global registration good enough to stop looking: most points fit and, with colours,
    the colours agree (a mirrored or rotated fit of a symmetric scene does not)."""
    return _accepted(q) and q["inlier_ratio"] >= 0.75 and (q["color_corr"] is None or q["color_corr"] >= 0.5)


def _rank(q: dict) -> tuple:
    return _accepted(q), q["color_corr"] if q["color_corr"] is not None else 0.0, q["fitness"]


def scene_depth(chunk: "Chunk", max_cameras: int = 60, max_points: int = 20000) -> float | None:
    """Median distance from the photos to the tie points they see, in world units."""
    tp = chunk.tie_points
    cams = chunk.aligned_cameras
    if tp is None or len(tp) < 10 or not cams:
        return None
    xyz = tp.xyz
    if len(xyz) > max_points:
        xyz = xyz[np.random.default_rng(2).choice(len(xyz), max_points, replace=False)]
    if len(cams) > max_cameras:
        cams = [cams[i] for i in np.linspace(0, len(cams) - 1, max_cameras).astype(int)]
    vals = []
    for cam in cams:
        s = chunk.sensor(cam)
        px, depth = cam.project(s, xyz)
        ok = (depth > 0) & (px[:, 0] >= 0) & (px[:, 1] >= 0) & (px[:, 0] < s.width) & (px[:, 1] < s.height)
        if ok.sum() >= 5:
            vals.append(float(np.median(depth[ok])))
    return float(np.median(vals) * chunk.scale) if vals else None


def _is_scaled(chunk: "Chunk") -> bool:
    """World units are metres: a CRS, a transform fitted to references or scale bars, or data
    measured in metres (laser scans: ``meta["metric"]``)."""
    return not crs_mod.is_local(chunk.crs) or abs(chunk.scale - 1.0) > 1e-9 or bool(chunk.meta.get("metric"))


def _is_referenced(chunk: "Chunk") -> bool:
    """Georeferenced in a CRS, or referenced in a local grid (control markers or cameras)."""
    if not crs_mod.is_local(chunk.crs):
        return True
    ctrl = sum(1 for m in chunk.markers if not m.checkpoint and m.reference.enabled and m.reference.location)
    cams = sum(1 for c in chunk.cameras if c.reference.enabled and c.reference.location)
    return abs(chunk.scale - 1.0) > 1e-9 and (ctrl >= 3 or cams >= 3)


def _reference_guess(ch, ref, frame, ref_frame, samples_internal: np.ndarray) -> np.ndarray | None:
    """Similarity between the local frames given by the two georeferences."""
    if not (_is_referenced(ch) and _is_referenced(ref)):
        return None
    if ch.crs == ref.crs:
        return _translation(frame.offset - ref_frame.offset)
    if crs_mod.is_local(ch.crs) or crs_mod.is_local(ref.crs):
        return None
    pts = samples_internal
    if len(pts) > 2000:
        pts = pts[np.random.default_rng(3).choice(len(pts), 2000, replace=False)]
    world = crs_mod.transform_points(geometry.apply(ch.transform, pts), ch.crs, ref.crs)
    src, dst = frame.local(pts), world - ref_frame.offset
    if not (_spread(src, 1e-3) and _spread(dst, 1e-3)):
        return None
    return geometry.umeyama(src, dst, with_scale=True)


def _open3d():
    try:
        import open3d as o3d
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("the global registration of point clouds needs Open3D "
                           "(pip install 'pyarchinit-photogrammetry[mesh]'); markers, common photos "
                           "or a georeference can give the initial position instead") from exc
    return o3d


def fpfh_features(xyz: np.ndarray, voxel: float) -> tuple[np.ndarray, np.ndarray]:
    """Points thinned on a ``voxel`` grid and their FPFH descriptors (Open3D): ``(points
    (n, 3), features (n, 33))``."""
    o3d = _open3d()
    pc = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(np.asarray(xyz, float)))
    pc = pc.voxel_down_sample(voxel)
    pc.estimate_normals(o3d.geometry.KDTreeSearchParamHybrid(radius=2.5 * voxel, max_nn=30))
    feat = o3d.pipelines.registration.compute_fpfh_feature(
        pc, o3d.geometry.KDTreeSearchParamHybrid(radius=5.0 * voxel, max_nn=100))
    return np.asarray(pc.points, float), np.asarray(feat.data, float).T


def _feature_matches(fs: np.ndarray, fd: np.ndarray) -> np.ndarray:
    """Pairs (source index, target index) of nearest descriptors, mutual ones when there are
    enough of them."""
    from scipy.spatial import cKDTree

    _, s2d = cKDTree(fd).query(fs, k=1)
    _, d2s = cKDTree(fs).query(fd, k=1)
    pairs = np.column_stack([np.arange(len(fs)), s2d])
    mutual = d2s[s2d] == np.arange(len(fs))
    return pairs[mutual] if mutual.sum() >= 30 else pairs


def _distinct(a: np.ndarray, b: np.ndarray, min_angle: float, min_shift: float) -> bool:
    """Two rigid motions differ by more than ``min_angle`` degrees or ``min_shift``."""
    c = (np.trace(a[:3, :3].T @ b[:3, :3]) - 1.0) / 2.0
    angle = np.degrees(np.arccos(np.clip(c, -1.0, 1.0)))
    return angle > min_angle or np.linalg.norm(a[:3, 3] - b[:3, 3]) > min_shift


def ransac_hypotheses(src: np.ndarray, dst: np.ndarray, threshold: float, samples: int = 60_000, seed: int = 0,
                      count: int = 3, min_angle: float = 15.0) -> list[tuple[np.ndarray, int]]:
    """The ``count`` best rigid motions mapping the putative pairs ``src[i] -> dst[i]`` within
    ``threshold`` that differ from each other (by ``min_angle`` degrees or ``10 x threshold``),
    most inliers first: random triplets with consistent side lengths, solved in batches (Kabsch)
    and scored on every pair, each kept motion refined on its inliers. Deterministic (``seed``).
    A scene with a symmetry (a rectangular trench) gives a wrong motion with nearly as many
    inliers as the right one: keeping several lets the ICP and the colours choose."""
    rng = np.random.default_rng(seed)
    m = len(src)
    if m < 3:
        return [(np.eye(4), 0)]
    batch = max(16, int(4_000_000 // (3 * m)))  # about 100 MB of moved points per batch
    tri = rng.integers(0, m, size=(samples, 3))
    a, b = src[tri], dst[tri]
    la = np.linalg.norm(a - a[:, [1, 2, 0]], axis=2)
    lb = np.linalg.norm(b - b[:, [1, 2, 0]], axis=2)
    ok = (np.minimum(la, lb) >= 0.9 * np.maximum(la, lb)).all(axis=1) & (la.min(axis=1) > 2 * threshold)
    a, b = a[ok], b[ok]
    pool = []  # (inliers, order, matrix): the best motions of every batch
    for start in range(0, len(a), batch):
        aa, bb = a[start:start + batch], b[start:start + batch]
        ca, cb = aa.mean(axis=1), bb.mean(axis=1)
        H = np.einsum("kni,knj->kij", bb - cb[:, None], aa - ca[:, None])
        U, _, Vt = np.linalg.svd(H)
        flip = np.linalg.det(U @ Vt) < 0
        U[flip, :, 2] *= -1
        R = U @ Vt
        t = cb - np.einsum("kij,kj->ki", R, ca)
        moved = np.einsum("kij,nj->kni", R, src) + t[:, None, :]
        inl = (np.sum((moved - dst[None]) ** 2, axis=2) < threshold ** 2).sum(axis=1)
        rest = np.argsort(-inl, kind="stable")
        rest = rest[inl[rest] >= 3]
        for _ in range(3 * count):  # the best motion, then the best one unlike it, and so on
            if not len(rest):
                break
            k = rest[0]
            M = np.eye(4)
            M[:3, :3], M[:3, 3] = R[k], t[k]
            pool.append((int(inl[k]), len(pool), M))
            cos = (np.einsum("ij,kij->k", R[k], R[rest]) - 1.0) / 2.0
            angle = np.degrees(np.arccos(np.clip(cos, -1.0, 1.0)))
            rest = rest[(angle > min_angle) | (np.linalg.norm(t[rest] - t[k], axis=1) > 10 * threshold)]
    pool.sort(key=lambda p: (-p[0], p[1]))
    shift = 10 * threshold
    picked = []
    for n, _order, M in pool:
        if all(_distinct(M, P, min_angle, shift) for P, _ in picked):
            picked.append((M, n))
            if len(picked) == 3 * count:
                break
    out = []
    for M, n in picked:
        for _ in range(2):  # least squares on the inliers
            inl = np.sum((geometry.apply(M, src) - dst) ** 2, axis=1) < threshold ** 2
            if inl.sum() < 3:
                break
            M = geometry.umeyama(src[inl], dst[inl], with_scale=False)
            n = int(inl.sum())
        if all(_distinct(M, P, min_angle, shift) for P, _ in out):
            out.append((M, n))
    out.sort(key=lambda p: -p[1])
    return out[:count] or [(np.eye(4), 0)]


def ransac_rigid(src: np.ndarray, dst: np.ndarray, threshold: float, samples: int = 60_000,
                 seed: int = 0) -> tuple[np.ndarray, int]:
    """The rigid motion mapping most of the putative pairs ``src[i] -> dst[i]`` within
    ``threshold`` (see :func:`ransac_hypotheses`). Returns ``(matrix, inliers)``."""
    return ransac_hypotheses(src, dst, threshold, samples, seed, count=1)[0]


def global_guesses(src: np.ndarray, dst: np.ndarray, ratio: float = 1.0, divisions=(50, 30, 80, 40, 65, 20),
                   progress: Progress | None = None):
    """Coarse similarities ``src -> dst`` without any initial position: FPFH descriptors
    (Open3D) on voxel grids of ``extent / division``, matched and filtered with RANSAC. ``ratio``
    is the expected scale between the two clouds (RANSAC fits rigid motions; the ICP refines
    the scale). Yields ``(matrix, voxel)``: the few best distinct motions of every division."""
    cs, cd = src.mean(0), dst.mean(0)
    s0, d0 = (src - cs) * ratio, dst - cd
    extent = float(max(np.ptp(d0, axis=0).max(), np.ptp(s0, axis=0).max()))
    for i, div in enumerate(divisions):
        if progress is not None:
            progress.check()
        voxel = extent / div
        ps, fs = fpfh_features(s0, voxel)
        pd, fd = fpfh_features(d0, voxel)
        if len(ps) < 10 or len(pd) < 10:
            continue
        pairs = _feature_matches(fs, fd)
        for T, _n in ransac_hypotheses(ps[pairs[:, 0]], pd[pairs[:, 1]], 1.5 * voxel, seed=i):
            M = _translation(cd) @ T @ geometry.compose_similarity(ratio, np.eye(3), -ratio * cs)
            yield M, voxel


def _align_points(ch, ref, frame, ref_frame, fix_scale, initial, max_points, progress) -> tuple[np.ndarray, dict]:
    from scipy.spatial import cKDTree

    src_int, src_rgb, src_kind = registration_cloud(ch, max(2000, max_points // 4))
    dst_int, dst_rgb, dst_kind = registration_cloud(ref, max_points)
    src, dst = frame.local(src_int), ref_frame.local(dst_int)
    progress(0.05, f"{ch.label}: {len(src):,} {src_kind} on {len(dst):,} {dst_kind} of {ref.label}")
    tree = cKDTree(dst)
    normals = estimate_normals(dst, tree)
    final = 3.0 * point_spacing(dst)
    extent = float(np.ptp(dst, axis=0).max())
    with_scale = not fix_scale

    def candidates():
        if initial in ("auto", "markers"):
            labels, ms, md = common_markers(ch, ref)
            if len(labels) >= 3:
                ms, md = frame.local(ms), ref_frame.local(md)
                if _spread(ms, 1e-3) and _spread(md, 1e-3):
                    W0 = geometry.umeyama(ms, md, with_scale=with_scale)
                    err = float(np.max(np.linalg.norm(geometry.apply(W0, ms) - md, axis=1)))
                    yield "markers", W0, max(10 * final, 3 * err)
            elif initial == "markers":
                raise ValueError(f"{ch.label}: fewer than 3 markers in common with {ref.label}")
        if initial in ("auto", "reference"):
            W0 = _reference_guess(ch, ref, frame, ref_frame, src_int)
            if W0 is not None:
                if fix_scale:
                    W0 = geometry.compose_similarity(1.0, *geometry.decompose_similarity(W0)[1:])
                yield "reference", W0, max(10 * final, 0.05 * extent)
            elif initial == "reference":
                raise ValueError(f"{ch.label} and {ref.label} do not share a georeference")
        if initial in ("auto", "cameras"):
            pairs = common_cameras(ch, ref)
            if len(pairs) >= (1 if fix_scale else 2):
                ps, pd = [frame.pose(c) for c, _ in pairs], [ref_frame.pose(r) for _, r in pairs]
                W0 = pose_similarity(ps, pd, with_scale=with_scale)
                err = float(np.max(np.linalg.norm(geometry.apply(W0, np.array([p[:3, 3] for p in ps]))
                                                  - np.array([p[:3, 3] for p in pd]), axis=1)))
                yield "cameras", W0, max(10 * final, 3 * err)
            elif initial == "cameras":
                raise ValueError(f"{ch.label}: not enough photos in common with {ref.label}")
        if initial in ("auto", "global"):
            ratio = 1.0
            if with_scale and not (_is_scaled(ch) and _is_scaled(ref)):
                da, db = scene_depth(ref), scene_depth(ch)
                ratio = da / db if da and db else 1.0
            for M, voxel in global_guesses(src, dst, ratio, progress=progress):
                yield "global", M, max(2.0 * voxel, 10 * final)

    best = None
    tried = []
    for i, (name, W0, start) in enumerate(candidates()):
        progress(min(0.9, 0.15 + 0.1 * i), f"{ch.label}: ICP from the {name} position")
        W = icp(src, dst, W0, with_scale=with_scale, start=start, final=final, tree=tree, normals=normals,
                progress=progress)
        q = registration_quality(src, dst, W, final, tree, normals, src_rgb, dst_rgb)
        q["scale_change"] = _scale_of(W) / _scale_of(W0)
        if abs(np.log(q["scale_change"])) > np.log(MAX_SCALE_CHANGE):  # collapsed or blown up: not a fit
            q["inlier_ratio"] = 0.0
        tried.append({"initial": name, **{k: q[k] for k in ("fitness", "overlap", "inlier_ratio", "color_corr",
                                                            "scale_change")}})
        if best is None or _rank(q) > _rank(best[2]):
            best = (name, W, q)
        if _accepted(q) and (name != "global" or _convincing(q)):  # else every global candidate is tried
            break
    if best is None:
        raise ValueError(f"{ch.label}: no way to place it on {ref.label} (no markers, georeference or photos "
                         f"in common, and no global registration)")
    name, W, q = best
    if not _accepted(q):
        colour = "" if q["color_corr"] is None else f", colour agreement {q['color_corr']:.2f}"
        raise ValueError(f"{ch.label}: the point clouds could not be registered on {ref.label} "
                         f"(best: {q['inlier_ratio'] * 100:.0f}% of the overlapping points fit, "
                         f"overlap {q['overlap'] * 100:.0f}%{colour}); add markers in common or align by cameras")
    info = {"method": "points", "initial": name, "source": src_kind, "target": dst_kind,
            "correspondences": q["inliers"], "rmse": q["rmse"], "fitness": q["fitness"], "overlap": q["overlap"],
            "inlier_ratio": q["inlier_ratio"], "color_corr": q["color_corr"], "distance": final, "attempts": tried}
    labels, ms, md = common_markers(ch, ref)
    if labels:  # independent check of the result
        res = _residuals(W, labels, frame.local(ms), ref_frame.local(md))
        info["check"] = {"markers": res, "rmse": _rmse(res)}
    return W, info


# ------------------------------------------------------------------ align
def _convert_references(chunk: "Chunk", src_crs: str, dst_crs: str) -> None:
    """Marker references to another CRS; camera references keep theirs (converted on use)."""
    for m in chunk.markers:
        if m.reference.location:
            loc = crs_mod.transform_points(np.array([m.reference.location], float), src_crs, dst_crs)[0]
            m.reference.location = [float(v) for v in loc]
    for c in chunk.cameras:
        if c.reference.location and "reference_crs" not in c.meta:
            c.meta["reference_crs"] = src_crs


def align_chunks(doc: "Document", reference, chunks=None, method: str = "markers", fix_scale: bool = False,
                 initial: str = "auto", max_points: int = 200_000, progress=None) -> dict:
    """Bring ``chunks`` (default: every other enabled chunk) into the world frame of ``reference``.

    ``method``: ``"markers"``, ``"points"`` or ``"cameras"`` (see the module documentation);
    ``fix_scale``: rigid alignment, the scale of each chunk stays as it is (chunks already
    scaled or georeferenced); ``initial`` (points only): the initial position, ``"auto"``
    (markers, then georeference, then common photos, then global registration) or one of
    ``"markers"``, ``"reference"``, ``"cameras"``, ``"global"``; ``max_points``: points of the
    reference cloud used by the registration (a quarter of it for the moving clouds).

    Nothing changes when one chunk cannot be aligned (``ValueError``). Returns a report:
    ``{"reference": {...}, "method", "fix_scale", "crs", "levelled_reference", "chunks": [...]}``
    with, per chunk, ``key``, ``label``, ``method``, ``correspondences``, ``rmse``, ``max_error``
    (markers/cameras), ``residuals`` (per marker or photo, reference world units), ``scale``
    (scale applied), ``transform`` (the new ``chunk.transform``), ``delta`` (old world ->
    reference world) and, for points, ``initial``, ``fitness``, ``overlap``, ``check``
    (residuals of the common markers, when there are any).
    """
    progress = as_progress(progress)
    if method not in ALIGN_METHODS:
        raise ValueError(f"unknown method {method!r}: use one of {', '.join(ALIGN_METHODS)}")
    if initial not in INITIAL_GUESSES:
        raise ValueError(f"unknown initial position {initial!r}: use one of {', '.join(INITIAL_GUESSES)}")
    ref = resolve_chunk(doc, reference)
    items = [c for c in doc.chunks if c is not ref and c.enabled] if chunks is None else chunks
    todo = [c for c in _resolve_many(doc, items) if c is not ref]
    if not todo:
        raise ValueError("no chunk to align: choose chunks other than the reference")
    for ch in [ref] + todo:
        if method == "cameras" and not ch.aligned_cameras:
            raise ValueError(f"chunk {ch.label} is not aligned")
        if fix_scale and not _is_scaled(ch):
            raise ValueError(f"fix_scale keeps the scale of each chunk, but {ch.label} is in arbitrary units "
                             f"(no CRS, references or scale bars): align it with the scale")
    saved = np.asarray(ref.transform, float).copy(), ref.meta.get("levelled")
    levelled = _level_reference(ref)
    results = []
    try:
        ref_frame = _Frame(ref)
        for i, ch in enumerate(todo):
            sub = progress.sub(i / len(todo), 1.0 / len(todo))
            sub(0.0, f"Aligning {ch.label} to {ref.label} ({method})")
            frame = _Frame(ch)
            if method == "markers":
                W, info = _align_markers(ch, ref, frame, ref_frame, fix_scale)
            elif method == "cameras":
                W, info = _align_cameras(ch, ref, frame, ref_frame, fix_scale)
            else:
                W, info = _align_points(ch, ref, frame, ref_frame, fix_scale, initial, max_points, sub)
            T = _new_transform(W, frame, ref_frame)
            info = {"key": ch.key, "label": ch.label, **info, "scale": _scale_of(W),
                    "transform": _list(T), "delta": _list(_delta(W, frame, ref_frame))}
            results.append((ch, T, info))
            sub(1.0, f"{ch.label}: aligned to {ref.label}" + (f", RMSE {info['rmse']:.4g}" if info.get("rmse") else ""))
    except BaseException:  # nothing changes, the reference is not levelled either
        ref.transform = saved[0]
        if saved[1] is None:
            ref.meta.pop("levelled", None)
        else:
            ref.meta["levelled"] = saved[1]
        raise
    for ch, T, info in results:
        if not crs_mod.is_local(ch.crs) and not crs_mod.is_local(ref.crs) and ch.crs != ref.crs:
            _convert_references(ch, ch.crs, ref.crs)
        ch.transform = T
        ch.crs = ref.crs
        if ref.meta.get("levelled"):
            ch.meta["levelled"] = True
        else:
            ch.meta.pop("levelled", None)
        info["stale_products"] = [k for k in ("dem", "orthomosaic") if ch.products.get(k)]
        ch.meta["chunk_alignment"] = {"reference": ref.label, "reference_key": ref.key, "method": info["method"],
                                      "rmse": info.get("rmse"), "correspondences": info.get("correspondences"),
                                      "fix_scale": bool(fix_scale)}
    progress(1.0, f"{len(results)} chunks aligned to {ref.label}")
    return {"reference": {"key": ref.key, "label": ref.label}, "method": method, "fix_scale": bool(fix_scale),
            "crs": ref.crs, "levelled_reference": levelled, "chunks": [info for _, _, info in results]}


def _level_reference(ref: "Chunk") -> bool:
    """Level an unlevelled local reference now (building its DEM would do it later and
    move it away from the chunks aligned to it). True when its transform changed."""
    from .dem import ensure_level

    before = np.asarray(ref.transform, float).copy()
    if not ref.aligned_cameras:
        return False
    ensure_level(ref)
    return not np.allclose(before, ref.transform)


# ------------------------------------------------------------------ merge
def _frame_to(chunk: "Chunk", first: "Chunk", warnings: list[str]) -> np.ndarray:
    """Similarity from the internal frame of ``chunk`` to the internal frame of ``first``."""
    if chunk is first:
        return np.eye(4)
    if chunk.crs == first.crs:
        return affine_inverse(first.transform) @ chunk.transform
    if crs_mod.is_local(chunk.crs) or crs_mod.is_local(first.crs):
        raise ValueError(f"chunk {chunk.label} ({chunk.crs}) and {first.label} ({first.crs}) are not in the same "
                         f"frame: align them first (align_chunks)")
    # two CRSs: fitted similarity through the projection, on the cameras and tie points
    pts = [c.center for c in chunk.aligned_cameras]
    tp = chunk.tie_points
    if tp is not None and len(tp):
        pick = tp.xyz[np.random.default_rng(0).choice(len(tp), min(len(tp), 2000), replace=False)]
        pts = list(pts) + list(pick)
    pts = np.array(pts, float).reshape(-1, 3)
    if len(pts) < 3:
        raise ValueError(f"chunk {chunk.label}: nothing to place in {first.crs}")
    world = crs_mod.transform_points(geometry.apply(chunk.transform, pts), chunk.crs, first.crs)
    frame = _Frame(first)
    target = world - frame.offset
    W = geometry.umeyama(pts, target, with_scale=True)
    err = float(np.sqrt(np.mean(np.sum((geometry.apply(W, pts) - target) ** 2, axis=1))))
    if err > 0.005:
        warnings.append(f"{chunk.label}: {chunk.crs} -> {first.crs} approximated within {err:.3f} m")
    return affine_inverse(frame.L) @ W


def _concat_clouds(clouds: list[PointCloud]) -> PointCloud | None:
    clouds = [c for c in clouds if c is not None and len(c)]
    if not clouds:
        return None
    xyz = np.vstack([c.xyz for c in clouds])
    rgb = None
    if any(c.rgb is not None for c in clouds):
        rgb = np.vstack([c.rgb if c.rgb is not None else np.full((len(c), 3), 128, np.uint8) for c in clouds])
    normals = np.vstack([c.normals for c in clouds]) if all(c.normals is not None for c in clouds) else None
    cls = np.concatenate([c.classification for c in clouds])
    conf = None
    if all(c.confidence is not None for c in clouds):
        conf = np.concatenate([np.asarray(c.confidence, np.float32) for c in clouds])
    labels = {}
    for c in clouds:
        labels.update(c.labels)
    manual = None
    if any(c.manual is not None for c in clouds):  # classes corrected by hand
        manual = np.concatenate([c.manual if c.manual is not None else np.zeros(len(c), bool) for c in clouds])
    return PointCloud(xyz, rgb, normals, cls, conf, labels, manual)


def _cloud_info(path: Path) -> tuple[int, set[str]]:
    """Number of points and vertex properties of a point cloud file (from the header of a
    binary PLY, else by reading it)."""
    from ..io.mesh_ply import read_header

    try:
        with open(path, "rb") as f:
            _, elements = read_header(f)
        vertex = next(e for e in elements if e["name"] == "vertex")
        return vertex["count"], {n for n, _ in vertex["props"]}
    except (OSError, ValueError, KeyError, StopIteration):
        pc = PointCloud.load(path)
        props = {"x", "y", "z"} | ({"red", "green", "blue"} if pc.rgb is not None else set())
        return len(pc), props | ({"nx", "ny", "nz"} if pc.normals is not None else set())


def _write_merged_cloud(parts: list[tuple[Path, np.ndarray]], out: Path, progress: Progress) -> int:
    """Dense clouds moved into one frame and written one after the other (one cloud in
    memory at a time), with colours (grey where a cloud has none), normals (when every
    cloud has them), classes and the flags of the classes corrected by hand."""
    infos = [_cloud_info(p) for p, _ in parts]
    with_normals = all({"nx", "ny", "nz"} <= props for _, props in infos)
    with_rgb = any({"red", "green", "blue"} <= props for _, props in infos)
    with_manual = any("manual" in props for _, props in infos)
    fields = [("x", "<f8"), ("y", "<f8"), ("z", "<f8")]
    if with_normals:
        fields += [("nx", "<f4"), ("ny", "<f4"), ("nz", "<f4")]
    if with_rgb:
        fields += [("red", "u1"), ("green", "u1"), ("blue", "u1")]
    fields += [("classification", "u1")]
    if with_manual:
        fields += [("manual", "u1")]
    names = {"<f8": "double", "<f4": "float", "u1": "uchar"}
    total = sum(n for n, _ in infos)
    header = ["ply", "format binary_little_endian 1.0", f"element vertex {total}"]
    header += [f"property {names[t]} {n}" for n, t in fields] + ["end_header"]
    tmp = out.with_suffix(".ply.tmp")
    written = 0
    with open(tmp, "wb") as fh:
        fh.write(("\n".join(header) + "\n").encode("ascii"))
        for i, ((path, S), (count, _)) in enumerate(zip(parts, infos)):
            progress(i / len(parts), f"Dense cloud {i + 1}/{len(parts)}")
            pc = PointCloud.load(path).transformed(S)
            if len(pc) != count:
                raise RuntimeError(f"{path}: {len(pc)} points read, {count} in the header")
            arr = np.empty(len(pc), dtype=fields)
            arr["x"], arr["y"], arr["z"] = pc.xyz.T
            if with_normals:
                arr["nx"], arr["ny"], arr["nz"] = pc.normals.T
            if with_rgb:
                rgb = pc.rgb if pc.rgb is not None else np.full((len(pc), 3), 128, np.uint8)
                arr["red"], arr["green"], arr["blue"] = rgb.T
            arr["classification"] = pc.classification
            if with_manual:
                arr["manual"] = pc.manual if pc.manual is not None else 0
            fh.write(arr.tobytes())
            written += len(pc)
            del pc, arr
    tmp.replace(out)
    return written


def _write_merged_mesh(parts: list[tuple[Path, np.ndarray]], out: Path, progress: Progress) -> tuple[int, int]:
    """Meshes moved into one frame and concatenated (vertex colours kept), streamed from the
    binary PLY files; other layouts are read whole with trimesh."""
    from ..io.mesh_ply import PlyMesh

    meshes = []
    for path, S in parts:
        try:
            meshes.append((PlyMesh(path), S))
        except (ValueError, KeyError, OSError):
            meshes.append((_trimesh_as_ply(path), S))
    colors = any(m.has_colors for m, _ in meshes)
    nv = sum(m.n_vertices for m, _ in meshes)
    nf = sum(m.n_faces for m, _ in meshes)
    vfields = [("x", "<f4"), ("y", "<f4"), ("z", "<f4")] + ([("red", "u1"), ("green", "u1"), ("blue", "u1")]
                                                            if colors else [])
    head = ["ply", "format binary_little_endian 1.0", f"element vertex {nv}"]
    head += [f"property {'float' if t == '<f4' else 'uchar'} {n}" for n, t in vfields]
    head += [f"element face {nf}", "property list uchar int vertex_indices", "end_header"]
    tmp = out.with_suffix(".ply.tmp")
    block = 2_000_000
    with open(tmp, "wb") as fh:
        fh.write(("\n".join(head) + "\n").encode("ascii"))
        for i, (m, S) in enumerate(meshes):
            progress(0.5 * i / len(meshes), f"Model {i + 1}/{len(meshes)}: vertices")
            for start in range(0, m.n_vertices, block):
                idx = slice(start, min(start + block, m.n_vertices))
                xyz = geometry.apply(S, m.xyz(idx))
                v = np.empty(len(xyz), dtype=vfields)
                v["x"], v["y"], v["z"] = xyz.T
                if colors:
                    rgb = m.colors(idx)
                    rgb = rgb if rgb is not None else np.full((len(xyz), 3), 128, np.uint8)
                    v["red"], v["green"], v["blue"] = rgb.T
                fh.write(v.tobytes())
        base = 0
        for i, (m, _) in enumerate(meshes):
            progress(0.5 + 0.5 * i / len(meshes), f"Model {i + 1}/{len(meshes)}: faces")
            for F in m.face_chunks():
                f = np.empty(len(F), dtype=[("n", "u1"), ("v", "<i4", (3,))])
                f["n"] = 3
                f["v"] = F + base
                fh.write(f.tobytes())
            base += m.n_vertices
    tmp.replace(out)
    return nv, nf


class _ArrayMesh:
    """In-memory mesh with the reading interface of :class:`io.mesh_ply.PlyMesh`."""

    def __init__(self, vertices: np.ndarray, faces: np.ndarray, colors: np.ndarray | None):
        self._v = np.asarray(vertices, np.float64)
        self._f = np.asarray(faces, np.int64)
        self._c = None if colors is None else np.asarray(colors)[:, :3].astype(np.uint8)
        self.n_vertices, self.n_faces = len(self._v), len(self._f)

    @property
    def has_colors(self) -> bool:
        return self._c is not None

    def xyz(self, index=None):
        return self._v if index is None else self._v[index]

    def colors(self, index=None):
        if self._c is None:
            return None
        return self._c if index is None else self._c[index]

    def face_chunks(self, size: int = 4_000_000):
        for start in range(0, self.n_faces, size):
            yield self._f[start:start + size]


def _trimesh_as_ply(path: Path) -> _ArrayMesh:
    import trimesh

    mesh = trimesh.load(str(path), process=False, force="mesh")
    colors = None
    vc = getattr(mesh.visual, "vertex_colors", None)
    if getattr(mesh.visual, "kind", None) == "vertex" and vc is not None and len(vc) == len(mesh.vertices):
        colors = vc
    return _ArrayMesh(mesh.vertices, mesh.faces, colors)


def merge_chunks(doc: "Document", chunks, label: str | None = None, merge_markers: bool = True,
                 merge_tie_points: bool = True, merge_point_clouds: bool = True, merge_models: bool = False,
                 progress=None) -> "Chunk":
    """A new chunk with the content of ``chunks`` (at least two; the inputs are kept), in the
    frame of the first one (internal frame, transform and CRS).

    Sensors and cameras get new keys (identical calibrations stay separate sensors); a
    photo present in several chunks is kept once (the first aligned copy) and its marker
    projections are combined. ``merge_markers``: markers with the same label become one
    (projections combined, position triangulated again in the merged frame), else every
    marker is kept. ``merge_tie_points``, ``merge_point_clouds``, ``merge_models``: tie
    points, dense clouds (colours, normals and classes kept) and meshes (vertex colours)
    concatenated in the merged frame. A COLMAP model of the merged cameras is written, so
    the dense cloud, mesh, DEM and orthomosaic can be built again; DEMs, orthomosaics and
    textures are not merged. ``merged.meta["merge"]`` lists the sources and warnings.
    """
    progress = as_progress(progress)
    sources = _resolve_many(doc, chunks)
    if len(sources) < 2:
        raise ValueError("merge_chunks needs at least two chunks")
    first = sources[0]
    warnings: list[str] = []
    frames = [_frame_to(ch, first, warnings) for ch in sources]
    merged = doc.add_chunk(label or "Merged chunk")
    try:
        shutil.rmtree(merged.data_dir, ignore_errors=True)  # a fresh folder (a removed chunk may have left one)
        _merge_into(merged, sources, frames, warnings, merge_markers, merge_tie_points, merge_point_clouds,
                    merge_models, progress)
    except BaseException:
        doc.remove_chunk(merged)
        raise
    return merged


def _merge_into(merged, sources, frames, warnings, merge_markers, merge_tie_points, merge_point_clouds,
                merge_models, progress: Progress) -> None:
    first = sources[0]
    merged.crs = first.crs
    merged.transform = np.asarray(first.transform, float).copy()
    if first.meta.get("levelled"):
        merged.meta["levelled"] = True
    progress(0.0, f"Merging {len(sources)} chunks")

    # sensors and cameras
    sensor_map: dict[tuple[int, int], int] = {}
    camera_map: dict[tuple[int, int], int] = {}
    kept: dict[str, Camera] = {}  # file -> camera of the merged chunk
    for ci, (ch, S) in enumerate(zip(sources, frames)):
        convert = not crs_mod.is_local(ch.crs) and ch.crs != merged.crs
        for s in ch.sensors.values():
            key = len(merged.sensors)
            merged.sensors[key] = Sensor(key=key, label=s.label, width=s.width, height=s.height, model=s.model,
                                         params=[float(v) for v in s.params], pixel_size_mm=s.pixel_size_mm,
                                         focal_length_mm=s.focal_length_mm, fixed=s.fixed)
            sensor_map[(ci, s.key)] = key
        for c in ch.cameras:
            path_key = _path_key(c.path)
            prev = kept.get(path_key)
            if prev is not None and (prev.aligned or not c.aligned):
                camera_map[(ci, c.key)] = prev.key
                continue
            ref = Reference(location=None if c.reference.location is None else [float(v) for v in c.reference.location],
                            accuracy=list(c.reference.accuracy), enabled=c.reference.enabled)
            meta = dict(c.meta)
            if convert and ref.location and "reference_crs" not in meta:
                meta["reference_crs"] = ch.crs
            new = Camera(key=len(merged.cameras) if prev is None else prev.key, label=c.label, path=c.path,
                         sensor_key=sensor_map[(ci, c.sensor_key)], enabled=c.enabled,
                         transform=None if c.transform is None else _rigid_pose(S, c.transform),
                         reference=ref, meta=meta)
            if prev is None:
                merged.cameras.append(new)
            else:  # an aligned copy replaces an unaligned one
                merged.cameras[[k.key for k in merged.cameras].index(prev.key)] = new
            kept[path_key] = new
            camera_map[(ci, c.key)] = new.key
    used = {c.sensor_key for c in merged.cameras}
    merged.sensors = {k: s for k, s in merged.sensors.items() if k in used}
    duplicates = sum(len(ch.cameras) for ch in sources) - len(merged.cameras)
    progress(0.05, f"{len(merged.cameras)} photos ({duplicates} in more than one chunk)")

    # markers and scale bars
    marker_map: dict[tuple[int, int], int] = {}
    by_label: dict[str, Marker] = {}
    mapped_positions: dict[int, list[np.ndarray]] = {}
    for ci, (ch, S) in enumerate(zip(sources, frames)):
        convert = not crs_mod.is_local(ch.crs) and ch.crs != merged.crs

        def reference(m: Marker) -> Reference:
            loc = m.reference.location
            if loc and convert:
                loc = crs_mod.transform_points(np.array([loc], float), ch.crs, merged.crs)[0]
            return Reference(location=[float(v) for v in loc] if loc is not None and len(loc) else None,
                             accuracy=list(m.reference.accuracy), enabled=m.reference.enabled)

        for m in ch.markers:
            target = by_label.get(m.label) if merge_markers else None
            if target is None:
                target = Marker(key=len(merged.markers), label=m.label, code=m.code, target_type=m.target_type,
                                reference=reference(m), checkpoint=m.checkpoint)
                merged.markers.append(target)
                if merge_markers:
                    by_label[m.label] = target
            else:
                if target.code is None and m.code is not None:
                    target.code, target.target_type = m.code, m.target_type
                if not target.reference.location and m.reference.location:
                    target.reference = reference(m)
            marker_map[(ci, m.key)] = target.key
            for cam_key, proj in m.projections.items():
                new_key = camera_map.get((ci, cam_key))
                if new_key is None:
                    continue
                old = target.projections.get(new_key)
                if old is None or (proj.pinned and not old.pinned):
                    target.projections[new_key] = Projection(proj.x, proj.y, proj.pinned)
            if m.position is not None:
                mapped_positions.setdefault(target.key, []).append(geometry.apply(S, m.position))
    seen_bars = set()
    for ci, ch in enumerate(sources):
        for sb in ch.scalebars:
            a, b = marker_map.get((ci, sb.marker_a)), marker_map.get((ci, sb.marker_b))
            if a is None or b is None or frozenset((a, b)) in seen_bars:
                continue
            seen_bars.add(frozenset((a, b)))
            merged.scalebars.append(ScaleBar(key=len(merged.scalebars), label=sb.label, marker_a=a, marker_b=b,
                                             distance=sb.distance, accuracy=sb.accuracy, enabled=sb.enabled,
                                             checkpoint=sb.checkpoint))

    # tie points (also the 3D points of the COLMAP model)
    progress(0.1, "Tie points")
    tie = _concat_clouds([ch.tie_points.transformed(S) if ch.tie_points is not None else None
                          for ch, S in zip(sources, frames)])
    if tie is None:
        warnings.append("no tie points: the COLMAP model has no 3D points (the depth maps need them)")
    elif merge_tie_points:
        merged.tie_points = tie

    # dense clouds
    parts = [(ch.product_path("point_cloud"), S) for ch, S in zip(sources, frames)]
    parts = [(p, S) for p, S in parts if p is not None and p.exists()]
    if merge_point_clouds and parts:
        n = _write_merged_cloud(parts, merged.data_dir / "point_cloud.ply", progress.sub(0.15, 0.4))
        merged._set_product("point_cloud", merged.data_dir / "point_cloud.ply")
        if len(parts) < len(sources):
            warnings.append(f"dense cloud of {len(parts)} chunks out of {len(sources)}")
        progress(0.55, f"Dense cloud: {n:,} points")
    classes = {}
    for ch in sources:
        classes.update(ch.meta.get("classes", {}))
    if classes:
        merged.meta["classes"] = classes

    # meshes
    if merge_models:
        mparts = [(ch.product_path("model"), S) for ch, S in zip(sources, frames)]
        mparts = [(p, S) for p, S in mparts if p is not None and p.exists()]
        if mparts:
            nv, nf = _write_merged_mesh(mparts, merged.data_dir / "model.ply", progress.sub(0.55, 0.3))
            merged._set_product("model", merged.data_dir / "model.ply")
            if len(mparts) < len(sources):
                warnings.append(f"model of {len(mparts)} chunks out of {len(sources)}")
            progress(0.85, f"Model: {nf:,} faces")

    # COLMAP model of the merged cameras, for the dense step
    progress(0.88, "COLMAP model of the merged cameras")
    if merged.aligned_cameras:
        from .sfm import write_reconstruction_from_chunk

        try:
            write_reconstruction_from_chunk(merged, points=tie)
        except Exception as exc:  # photos moved or unreadable: the merged products are still usable
            warnings.append(f"COLMAP model not written ({exc})")
    else:
        warnings.append("no aligned photo: align the merged chunk before building products")

    # marker positions in the merged frame
    for m in merged.markers:
        pos = marker_position(merged, _without_position(m))
        if pos is None and m.key in mapped_positions:
            pos = np.mean(mapped_positions[m.key], axis=0)
        m.position = pos
    merged.meta["merge"] = {"chunks": [{"key": ch.key, "label": ch.label} for ch in sources],
                            "duplicate_photos": duplicates, "markers_merged": bool(merge_markers),
                            "warnings": warnings}
    progress(1.0, f"Merged chunk {merged.label}: {len(merged.cameras)} photos, {len(merged.markers)} markers")


def _without_position(m: Marker) -> Marker:
    return Marker(key=m.key, label=m.label, projections=m.projections)


__all__ = ["align_chunks", "merge_chunks", "resolve_chunk", "common_markers", "common_cameras", "marker_position",
           "pose_similarity", "icp", "global_guesses", "fpfh_features", "ransac_rigid", "ransac_hypotheses",
           "registration_quality",
           "registration_cloud", "scene_depth", "affine_inverse", "ALIGN_METHODS", "INITIAL_GUESSES"]
