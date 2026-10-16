"""Gradual selection of the tie points, as in Metashape.

The tie points of the alignment (the 3D points of the COLMAP model in ``<chunk>/sfm/model``)
are measured with four criteria; the worst ones are removed and the cameras optimized again:

``reprojection_error``
    mean distance in pixels between where a point is measured in the photos and where it
    projects; large values are mismatches.
``image_count``
    number of photos that see the point; 2 means a point checked by a single pair.
``reconstruction_uncertainty``
    ratio of the largest to the smallest semi-axis of the error ellipsoid of the
    triangulated point (unit pixel noise, pinhole projection): 1 for a point seen from
    well spread directions, large when the rays are nearly parallel and the point is badly
    fixed along them (Metashape's recommended threshold is about 10-15).
``projection_accuracy``
    mean size in pixels (the scale of the SIFT keypoints, ``sqrt(|det A|)`` of their affine
    shape in the COLMAP database) of the measurements of the point: points found only on
    blurred or coarse features are located less precisely. Available when the model comes
    from this program's matching; ``None`` for models written from imported poses
    (Metashape, WebODM, Photo3D, merged chunks), which have no keypoints.

On a model written from imported poses the observations are projections of the points
(:func:`.sfm.write_reconstruction_from_chunk`): the reprojection error is zero and the image
count is the number of photos framing the point; the metrics flag it with ``synthetic``.
"""
from __future__ import annotations

import logging
import sqlite3
from collections import Counter
from pathlib import Path

import numpy as np

from ..core.chunk import Chunk
from ..core.progress import Progress
from .sfm import drop_missing_points, import_reconstruction, load_reconstruction

log = logging.getLogger(__name__)

MIN_POINTS = 100  # tie points that must remain in the model
MIN_IMAGE_POINTS = 20  # tie points a photo must keep (or all it has, when it had fewer) to stay registered
CRITERIA = ("reprojection_error", "image_count", "reconstruction_uncertainty", "projection_accuracy")


class _Keypoints:
    """Keypoints of the chunk's COLMAP database, read photo by photo (a large project holds
    hundreds of MB of them): positions and scale in pixels. Empty without a database."""

    def __init__(self, chunk: Chunk):
        self.con, self.ids = None, {}
        db = chunk.data_dir / "sfm" / "database.db"
        if not db.exists():
            return
        try:
            self.con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            self.ids = dict(self.con.execute("SELECT name, image_id FROM images").fetchall())
        except sqlite3.Error as exc:
            log.info("Keypoints not read from %s: %s", db, exc)
            self.close()

    def get(self, name: str) -> tuple[np.ndarray, np.ndarray] | None:
        """Positions (k, 2) and scales (k,) of the keypoints of a photo; ``None`` when the
        photo is not in the database or its keypoints carry no scale."""
        iid = self.ids.get(name)
        if self.con is None or iid is None:
            return None
        try:
            row = self.con.execute("SELECT rows, cols, data FROM keypoints WHERE image_id = ?", (iid,)).fetchone()
        except sqlite3.Error:
            return None
        if row is None or not row[0] or row[2] is None:
            return np.zeros((0, 2), np.float32), np.zeros(0)
        nrows, ncols, data = int(row[0]), int(row[1]), row[2]
        kp = np.frombuffer(data, np.float32).reshape(nrows, ncols).astype(np.float64)
        if ncols == 6:  # x, y and the affine shape a11, a12, a21, a22
            scale = np.sqrt(np.abs(kp[:, 2] * kp[:, 5] - kp[:, 3] * kp[:, 4]))
        elif ncols == 4:  # x, y, scale, orientation (older databases)
            scale = kp[:, 2]
        else:
            return None
        return kp[:, :2], scale

    def close(self) -> None:
        if self.con is not None:
            self.con.close()
        self.con = None


def tie_point_metrics(chunk: Chunk, rec=None) -> dict:
    """Metrics of every 3D point of the chunk's COLMAP model (see the module docstring).

    Returns numpy arrays aligned on ``ids`` (the point3D ids): ``xyz`` (internal frame),
    ``reprojection_error`` (px), ``image_count``, ``reconstruction_uncertainty``,
    ``projection_accuracy`` (px, or ``None``); ``observations`` (``{"point", "image"}``:
    the row in ``ids`` and the image id of every measurement), ``mean_reprojection_error``
    (px, over all the measurements), ``images`` (registered photos) and ``synthetic`` (the
    model was written from imported poses)."""
    rec = load_reconstruction(chunk) if rec is None else rec
    drop_missing_points(rec)  # models written from imported poses by earlier versions
    n = rec.num_points3D()
    ids = np.empty(n, np.int64)
    xyz = np.empty((n, 3), np.float64)
    for i, (pid, p) in enumerate(rec.points3D.items()):
        ids[i] = pid
        xyz[i] = p.xyz
    order = np.argsort(ids)
    sorted_ids = ids[order]
    keypoints = _Keypoints(chunk)
    scaled = keypoints.con is not None  # every measurement has the scale of its keypoint
    rows_all, imgs_all, err_all, scale_all = [], [], [], []
    hxx, hxy, hxz, hyy, hyz, hzz = (np.zeros(n) for _ in range(6))
    images = 0
    for iid, image in rec.images.items():
        if not image.has_pose:
            continue
        images += 1
        obs = image.get_observation_points2D()
        if not len(obs):
            continue
        xy = np.array([q.xy for q in obs], np.float64).reshape(-1, 2)
        pid = np.fromiter((q.point3D_id for q in obs), np.int64, count=len(obs))
        rows = order[np.searchsorted(sorted_ids, pid)]
        pose = np.asarray(image.cam_from_world().matrix())
        rot, t = pose[:, :3], pose[:, 3]
        pc = xyz[rows] @ rot.T + t
        cam = rec.cameras[image.camera_id]
        z = pc[:, 2]
        front = z > 1e-12
        err = np.full(len(rows), 1e6)  # a point behind a photo that measures it: as bad as it gets
        if front.any():
            proj = np.asarray(cam.img_from_cam(pc[front]), np.float64).reshape(-1, 2)
            e = np.linalg.norm(proj - xy[front], axis=1)
            err[front] = np.where(np.isfinite(e), e, 1e6)
        # pinhole Jacobian of the projection with respect to the world point: d(u, v)/dX = Jc R
        focal = list(cam.focal_length_idxs())
        fx, fy = float(cam.params[focal[0]]), float(cam.params[focal[-1]])
        zz = np.where(front, z, np.inf)
        j = np.zeros((len(rows), 2, 3))
        j[:, 0, 0] = fx / zz
        j[:, 0, 2] = -fx * pc[:, 0] / zz ** 2
        j[:, 1, 1] = fy / zz
        j[:, 1, 2] = -fy * pc[:, 1] / zz ** 2
        jw = j @ rot  # (k, 2, 3)
        for acc, a, b in ((hxx, 0, 0), (hxy, 0, 1), (hxz, 0, 2), (hyy, 1, 1), (hyz, 1, 2), (hzz, 2, 2)):
            acc += np.bincount(rows, weights=np.einsum("ki,ki->k", jw[:, :, a], jw[:, :, b]), minlength=n)
        rows_all.append(rows)
        imgs_all.append(np.full(len(rows), iid, np.int64))
        err_all.append(err)
        if scaled:
            kp = keypoints.get(image.name)
            idx = np.asarray(image.get_observation_point2D_idxs(), np.int64)
            if (kp is None or len(kp[1]) != image.num_points2D() or len(idx) != len(rows)
                    or np.abs(kp[0][idx] - xy).max(initial=0.0) > 1e-2):
                scaled = False  # the model does not come from these keypoints
            else:
                scale_all.append(kp[1][idx])
    keypoints.close()
    rows = np.concatenate(rows_all) if rows_all else np.zeros(0, np.int64)
    imgs = np.concatenate(imgs_all) if imgs_all else np.zeros(0, np.int64)
    err = np.concatenate(err_all) if err_all else np.zeros(0)
    count = np.bincount(rows, minlength=n).astype(np.float64)
    with np.errstate(invalid="ignore", divide="ignore"):
        reproj = np.bincount(rows, weights=err, minlength=n) / count
    reproj[count == 0] = np.inf
    pairs = np.unique(rows * (int(imgs.max(initial=0)) + 1) + imgs) if len(rows) else np.zeros(0, np.int64)
    image_count = np.bincount(pairs // (int(imgs.max(initial=0)) + 1), minlength=n).astype(np.int64)
    h = np.empty((n, 3, 3))
    h[:, 0, 0], h[:, 0, 1], h[:, 0, 2] = hxx, hxy, hxz
    h[:, 1, 0], h[:, 1, 1], h[:, 1, 2] = hxy, hyy, hyz
    h[:, 2, 0], h[:, 2, 1], h[:, 2, 2] = hxz, hyz, hzz
    uncertainty = np.full(n, np.inf)
    if n:  # semi-axes of the error ellipsoid: 1 / sqrt of the eigenvalues of the normal matrix
        lam = np.linalg.eigvalsh(h)
        ok = (lam[:, 0] > lam[:, 2] * 1e-15) & (lam[:, 2] > 0)
        uncertainty[ok] = np.sqrt(lam[ok, 2] / lam[ok, 0])
    accuracy = None
    if scaled and scale_all:
        with np.errstate(invalid="ignore", divide="ignore"):
            accuracy = np.bincount(rows, weights=np.concatenate(scale_all), minlength=n) / count
        accuracy[count == 0] = np.inf
    finite = err[err < 1e6]
    return {"ids": ids, "xyz": xyz, "reprojection_error": reproj, "image_count": image_count,
            "reconstruction_uncertainty": uncertainty, "projection_accuracy": accuracy,
            "observations": {"point": rows, "image": imgs},
            "mean_reprojection_error": float(finite.mean()) if len(finite) else 0.0,
            "images": images, "synthetic": bool(len(finite) and float(np.max(finite)) < 1e-3)}  # no measurement


def select_tie_points(chunk: Chunk, max_reprojection_error: float | None = None, min_image_count: int | None = None,
                      max_reconstruction_uncertainty: float | None = None,
                      max_projection_accuracy: float | None = None, metrics: dict | None = None) -> dict:
    """Points the criteria select for removal (Metashape's gradual selection), without
    changing anything. Each criterion left ``None`` is not used; ``metrics`` (from
    :func:`tie_point_metrics`) avoids computing them again while thresholds are tried.

    Returns ``mask`` (over ``metrics["ids"]``), ``ids`` (point3D ids to remove),
    ``by_criterion`` (points each criterion selects on its own), ``selected`` (all of them),
    ``total`` and ``remaining`` points, ``percent`` and ``images_below_minimum`` (photos that
    would keep fewer than :data:`MIN_IMAGE_POINTS` tie points: :func:`remove_tie_points`
    refuses such a removal)."""
    m = tie_point_metrics(chunk) if metrics is None else metrics
    n = len(m["ids"])
    mask = np.zeros(n, bool)
    by: dict[str, int | None] = {}
    tests = (("reprojection_error", max_reprojection_error, np.greater),
             ("image_count", min_image_count, np.less),
             ("reconstruction_uncertainty", max_reconstruction_uncertainty, np.greater),
             ("projection_accuracy", max_projection_accuracy, np.greater))
    for name, value, cmp in tests:
        if value is None:
            continue
        if m[name] is None:  # no keypoint scales for this model
            by[name] = None
            continue
        sel = cmp(m[name], value)
        by[name] = int(sel.sum())
        mask |= sel
    obs = m["observations"]
    weak = 0
    if mask.any() and len(obs["image"]):
        imgs, inv = np.unique(obs["image"], return_inverse=True)
        have = np.bincount(inv, minlength=len(imgs))
        lose = np.bincount(inv, weights=mask[obs["point"]].astype(np.float64), minlength=len(imgs))
        weak = int(np.sum(have - lose < np.minimum(MIN_IMAGE_POINTS, have)))
    selected = int(mask.sum())
    return {"mask": mask, "ids": m["ids"][mask], "by_criterion": by, "selected": selected, "total": n,
            "remaining": n - selected, "percent": 100.0 * selected / max(n, 1), "images_below_minimum": weak}


def _drop_from_tie_points(chunk: Chunk, deleted_xyz: np.ndarray) -> None:
    """Remove the deleted points from tie points that are not the model's own (an imported
    project keeps all the tie points of the other program, the model only a sample)."""
    from scipy.spatial import cKDTree

    tp = chunk.tie_points
    if tp is None or not len(deleted_xyz):
        return
    span = float(np.ptp(tp.xyz, axis=0).max()) if len(tp) else 1.0
    dist, idx = cKDTree(tp.xyz).query(deleted_xyz, k=1)
    hit = idx[dist <= 1e-9 * max(span, 1.0)]
    if len(hit):
        keep = np.ones(len(tp), bool)
        keep[hit] = False
        chunk.tie_points = tp.subset(keep)


def remove_tie_points(chunk: Chunk, ids, optimize: bool = False, progress: Progress | None = None) -> dict:
    """Delete the 3D points ``ids`` from the chunk's COLMAP model, write it back and refresh the
    tie points; with ``optimize`` the cameras are optimized again afterwards (and the chunk
    georeferenced again when markers or cameras have reference coordinates).

    Refuses (``ValueError``) a removal that would leave fewer than :data:`MIN_POINTS` points,
    or would leave a registered photo with fewer than :data:`MIN_IMAGE_POINTS` tie points
    (all it has when it had fewer): such a photo could not be optimized nor stay aligned.
    Returns ``removed``, ``remaining``, the mean reprojection error before and after (px)
    and, with ``optimize``, the error of the optimization and the georeference report."""
    progress = progress or Progress()
    rec = load_reconstruction(chunk)
    drop_missing_points(rec)
    before = tie_point_metrics(chunk, rec)["mean_reprojection_error"]
    wanted = {int(i) for i in np.asarray(list(ids) if not isinstance(ids, np.ndarray) else ids).ravel()}
    pids = sorted(i for i in wanted if rec.exists_point3D(i))
    total = rec.num_points3D()
    result = {"removed": 0, "remaining": total, "reprojection_error_before": before,
              "reprojection_error_after": before}
    if not pids:
        return result
    if total - len(pids) < MIN_POINTS:
        raise ValueError(f"only {total - len(pids)} tie points would remain (at least {MIN_POINTS} are needed): "
                         "choose milder thresholds")
    lost: Counter = Counter()
    for pid in pids:
        for el in rec.points3D[pid].track.elements:
            lost[el.image_id] += 1
    weak = []
    for iid, image in rec.images.items():
        if not image.has_pose or iid not in lost:
            continue
        have = image.num_points3D
        if have - lost[iid] < min(MIN_IMAGE_POINTS, have):
            weak.append(image.name)
    if weak:
        raise ValueError(f"{len(weak)} photos would keep fewer than {MIN_IMAGE_POINTS} tie points "
                         f"({', '.join(Path(w).name for w in weak[:3])}{'...' if len(weak) > 3 else ''}) and could not "
                         "stay aligned: choose milder thresholds")
    progress(0.05, f"Removing {len(pids):,} tie points")
    deleted_xyz = np.array([rec.points3D[pid].xyz for pid in pids])
    tp = chunk.tie_points
    own = tp is None or len(tp) == total  # the tie points are the model's own points
    for pid in pids:
        rec.delete_point3D(pid)
    rec.write(str(chunk.product_path("reconstruction")))
    if own:
        import_reconstruction(chunk, rec)
    else:
        _drop_from_tie_points(chunk, deleted_xyz)
    after = tie_point_metrics(chunk, rec)["mean_reprojection_error"]
    result.update({"removed": len(pids), "remaining": rec.num_points3D(), "reprojection_error_after": after})
    history = chunk.meta.setdefault("sfm", {}).setdefault("tie_point_cleaning", [])
    history.append({"removed": len(pids), "remaining": rec.num_points3D(), "reprojection_error_before": before,
                    "reprojection_error_after": after})
    progress(0.3 if optimize else 1.0, f"{len(pids):,} tie points removed, {rec.num_points3D():,} left")
    if optimize:
        result["optimize_error"] = chunk.optimize_cameras(progress=progress.sub(0.3, 0.65))
        # the adjustment moves the internal frame a little: fit the georeference again (as after aligning)
        has_refs = (any(m.reference.location for m in chunk.markers)
                    or sum(1 for c in chunk.aligned_cameras if c.reference.location) >= 3 or chunk.scalebars)
        if has_refs:
            try:
                result["georeference"] = chunk.update_transform()
            except ValueError as exc:  # a geographic CRS: the user fixes it in the reference settings
                log.warning("Georeference not updated after the optimization: %s", exc)
        progress(1.0, f"Cameras optimized: reprojection error {result['optimize_error']:.3f} px")
    return result


__all__ = ["CRITERIA", "MIN_IMAGE_POINTS", "MIN_POINTS", "remove_tie_points", "select_tie_points", "tie_point_metrics"]
