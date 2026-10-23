"""Precision of the alignment: cameras, tie points, markers and the DEM, from the covariance of
the bundle adjustment.

The adjustment of the chunk is set up as the camera optimisation does it - the control points held
at their reference coordinates (else the gauge fixed on three tie points), the calibration held -
and COLMAP computes the covariance of the camera poses (``pycolmap.estimate_ba_covariance``; in the
tangent space of cam_from_world, the rotation perturbed as ``Exp(2 d) R``, the translation added).
From it:

* a camera: the covariance of its centre;
* a tie point or a marker: the full covariance of a point triangulated from its observations,
  ``s^2 H^-1 + H^-1 H_pc S_cc H_cp H^-1`` with ``H = J_x^T J_x`` - the noise of its measurements
  (``s``: the reprojection error of the adjustment for tie points, 0.5 px for markers) plus what
  the uncertainty of the cameras that see it carries into it;
* the datum: the accuracy of the control points (or of the camera positions when they place the
  chunk), ``a^2 (1 + d^2 / r^2)`` per axis, ``a`` that of their weighted mean, ``d`` the distance
  from their centre and ``r`` their spread - a similarity fitted to them is surer near them.

Everything in metres along the axes of the CRS. :func:`dem_precision` interpolates the vertical
precision of the tie points over the DEM (as the precision maps of James et al., 2017, from the
covariance instead of repeated adjustments). On the synthetic trench the errors of the tie points
against the true surface are 1.09 times their estimated precision (root mean square), those of the
cameras 0.7 times.
"""
from __future__ import annotations

import datetime

import numpy as np

from ..core import crs as crs_mod
from ..core.chunk import Chunk
from ..core.progress import Progress, as_progress

MARKER_PX = 0.5  # pixels: how well a marker is measured in a photo (as sfm.MARKER_PIXEL_ACCURACY)
MAX_PHOTOS = 2000  # the covariance of the poses is a dense matrix of 6 x photos a side


def _skew(v) -> np.ndarray:
    return np.array([[0.0, -v[2], v[1]], [v[2], 0.0, -v[0]], [-v[1], v[0], 0.0]])


def _datum(chunk: Chunk):
    """What places the chunk: ``(kind, locations (n, 3) world, accuracies (n, 3))``, kind
    ``"control points"``, ``"camera positions"`` or ``"internal"`` (nothing: local coordinates)."""
    from ..markers import gcp

    ctrl = [m for m in chunk.markers if not m.checkpoint and m.reference.enabled and m.reference.location
            and m.position is not None]
    if len(ctrl) >= 3:
        loc = np.array([m.reference.location for m in ctrl], float)
        if gcp._non_collinear(loc):
            acc = np.array([np.broadcast_to(np.asarray(m.reference.accuracy, float), 3) for m in ctrl])
            return "control points", loc, acc
    cams = [c for c in chunk.aligned_cameras if c.reference.enabled and c.reference.location]
    if len(cams) >= 3 and gcp.cameras_span_their_accuracy(chunk) and not crs_mod.is_local(chunk.crs):
        loc = chunk.to_world(np.array([c.center for c in cams]))
        acc = np.array([np.broadcast_to(np.asarray(c.reference.accuracy, float), 3) for c in cams])
        return "camera positions", loc, acc
    return "internal", np.zeros((0, 3)), np.zeros((0, 3))


def _datum_variance(points: np.ndarray, loc: np.ndarray, acc: np.ndarray) -> np.ndarray:
    """Variance per axis (n, 3) that the datum adds at world ``points``."""
    if not len(loc):
        return np.zeros((len(points), 3))
    w = 1.0 / np.maximum(acc, 1e-9) ** 2
    var = 1.0 / w.sum(0)  # of the weighted mean, per axis
    centre = (w * loc).sum(0) / w.sum(0)
    spread = max(float(np.mean(np.einsum("ij,ij->i", loc - centre, loc - centre))), 1e-12)
    d2 = np.einsum("ij,ij->i", points - centre, points - centre)
    return var[None, :] * (1.0 + d2[:, None] / spread)


class _Network:
    """The adjustment of the chunk with the covariance of its camera poses."""

    def __init__(self, chunk: Chunk, kind: str, max_photos: int = MAX_PHOTOS):
        import pycolmap

        from ..markers import gcp
        from . import sfm

        rec = sfm.load_reconstruction(chunk)
        sfm.drop_missing_points(rec)
        sfm._square_pixels(chunk, rec)
        self.images = sorted(i for i, im in rec.images.items() if im.has_pose)
        if len(self.images) < 2:
            raise RuntimeError("the chunk has fewer than two aligned photos")
        if len(self.images) > max_photos:
            raise RuntimeError(f"too many photos for the covariance ({len(self.images)}, at most {max_photos})")
        self.tie_ids = set(rec.points3D)
        fixed = []
        if kind == "control points":
            controls = sfm._control_markers(chunk)
            fixed, _used = sfm._fixed_points(chunk, rec, 1, [(chunk.marker_by_key(k), X) for k, X in controls.items()])
            placed = np.array([rec.points3D[p].xyz for p in fixed]).reshape(-1, 3)
            if len(placed) < 3 or not gcp._non_collinear(placed):
                fixed = sfm._drop_points(rec, fixed)
        config = pycolmap.BundleAdjustmentConfig()
        for iid in self.images:
            config.add_image(iid)
        for pid in fixed:
            config.add_constant_point(pid)
        for cid in {rec.images[i].camera_id for i in self.images}:
            config.set_constant_cam_intrinsics(cid)
        if not fixed:
            config.fix_gauge(pycolmap.BundleAdjustmentGauge.THREE_POINTS)
        self.held = "control points" if fixed else "three tie points"
        adjuster = pycolmap.create_default_ceres_bundle_adjuster(pycolmap.BundleAdjustmentOptions(), config, rec)
        options = pycolmap.BACovarianceOptions()
        options.params = pycolmap.BACovarianceOptionsParams.POSES_AND_POINTS
        cov = pycolmap.estimate_ba_covariance(options, rec, adjuster)
        if cov is None:
            raise RuntimeError("the covariance of the adjustment could not be computed")
        n = len(self.images)
        self.index = {iid: k for k, iid in enumerate(self.images)}
        S = np.zeros((6 * n, 6 * n))
        for a, i in enumerate(self.images):
            S[6 * a:6 * a + 6, 6 * a:6 * a + 6] = cov.get_cam_cov_from_world(i)
            for b in range(a + 1, n):
                block = cov.get_cam_cross_cov_from_world(i, self.images[b])
                S[6 * a:6 * a + 6, 6 * b:6 * b + 6] = block
                S[6 * b:6 * b + 6, 6 * a:6 * a + 6] = block.T
        self.S = S
        self.rec = rec
        self.pose = {}
        for i in self.images:
            cfw = rec.images[i].cam_from_world()
            self.pose[i] = (np.asarray(cfw.rotation.matrix(), float), np.asarray(cfw.translation, float))
        for pid in fixed:  # the control points are not tie points
            self.tie_ids.discard(pid)

    def jacobians(self, iid: int, X: np.ndarray, uv: np.ndarray | None = None):
        """For points ``X`` (n, 3) seen by image ``iid``: the derivatives of their projections
        by the point (n, 2, 3) and by the pose (n, 2, 6), and the residuals (n, 2) when the
        measured pixels ``uv`` are given."""
        im = self.rec.images[iid]
        cam = self.rec.cameras[im.camera_id]
        R, t = self.pose[iid]
        xc = X @ R.T + t
        proj = np.asarray(cam.img_from_cam(xc), float)
        eps = 1e-6 * np.maximum(np.abs(xc[:, 2:3]), 1e-9)
        D = np.zeros((len(xc), 2, 3))
        for k in range(3):
            e = np.zeros(3)
            e[k] = 1.0
            D[:, :, k] = (np.asarray(cam.img_from_cam(xc + eps * e), float) - proj) / eps
        RX = X @ R.T
        dpose = np.zeros((len(X), 3, 6))
        dpose[:, :, :3] = -2.0 * np.stack([_skew(v) for v in RX]) if len(X) else 0.0
        dpose[:, :, 3:] = np.eye(3)
        return D @ R, D @ dpose, (None if uv is None else proj - uv)

    def point_covariance(self, obs, sigma: float, sigma0: float) -> np.ndarray | None:
        """Covariance (internal frame) of a point from its observations ``[(image index, Jx
        (2, 3), Jc (2, 6))]`` measured to ``sigma`` px, the cameras known to ``sigma0``."""
        if len(obs) < 2:
            return None
        Jx = np.concatenate([o[1] for o in obs])
        H = Jx.T @ Jx
        try:
            Hi = np.linalg.inv(H)
        except np.linalg.LinAlgError:
            return None
        A = Hi @ np.hstack([o[1].T @ o[2] for o in obs])
        sel = np.concatenate([np.arange(6 * o[0], 6 * o[0] + 6) for o in obs])
        return sigma ** 2 * Hi + sigma0 ** 2 * A @ self.S[np.ix_(sel, sel)] @ A.T


def estimate_precision(chunk: Chunk, marker_px: float = MARKER_PX, max_photos: int = MAX_PHOTOS,
                       progress: Progress | None = None) -> dict:
    """Precision of the cameras, the tie points and the markers of an aligned chunk (see the
    module docstring). Stores ``camera.meta["precision"]`` (metres, X Y Z of the CRS), the tie
    points with theirs in ``<data>/precision/tie_points.npz`` and the report in
    ``chunk.meta["precision"]``, which it returns."""
    progress = as_progress(progress)
    progress(0.02, "Precision: covariance of the adjustment")
    kind, loc, acc = _datum(chunk)
    net = _Network(chunk, kind, max_photos)
    progress(0.4, "Precision: tie points")
    M = np.asarray(chunk.transform, float)[:3, :3]  # internal -> world (rotation and scale)
    # observations of the tie points and the reprojection error of the adjustment
    obs: dict[int, list] = {}
    residuals = []
    for iid in net.images:
        progress.check()
        im = net.rec.images[iid]
        pts = [(p.point3D_id, p.xy) for p in im.points2D if p.has_point3D() and p.point3D_id in net.tie_ids]
        if not pts:
            continue
        X = np.array([net.rec.points3D[pid].xyz for pid, _ in pts])
        Jx, Jc, r = net.jacobians(iid, X, np.array([xy for _, xy in pts]))
        residuals.append(r)
        k = net.index[iid]
        for (pid, _xy), jx, jc in zip(pts, Jx, Jc):
            obs.setdefault(pid, []).append((k, jx, jc))
    r = np.concatenate(residuals) if residuals else np.zeros((0, 2))
    dof = max(r.size - 6 * len(net.images) - 3 * len(obs), 1)
    sigma0 = float(np.sqrt((r ** 2).sum() / dof)) if r.size else 1.0
    ids, xyz, sig_net = [], [], []
    for n_done, (pid, o) in enumerate(obs.items()):
        if n_done % 5000 == 0:
            progress(0.4 + 0.45 * n_done / max(len(obs), 1), f"Precision: tie points {n_done:,}/{len(obs):,}")
        c = net.point_covariance(o, sigma0, sigma0)
        if c is None:
            continue
        ids.append(pid)
        xyz.append(net.rec.points3D[pid].xyz)
        sig_net.append(np.sqrt(np.clip(np.diag(M @ c @ M.T), 0, None)))
    world = chunk.to_world(np.array(xyz).reshape(-1, 3))
    sig_net = np.array(sig_net).reshape(-1, 3)
    sig = np.sqrt(sig_net ** 2 + _datum_variance(world, loc, acc))
    folder = chunk.data_dir / "precision"
    folder.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(folder / "tie_points.npz", xyz=world, sigma=sig.astype(np.float32),
                        sigma_network=sig_net.astype(np.float32))
    progress(0.86, "Precision: cameras and markers")
    # cameras: the centre C = -R^T t
    names = {v: int(k) for k, v in (chunk.meta.get("sfm") or {}).get("names", {}).items()}
    cams = {}
    for iid in net.images:
        key = names.get(net.rec.images[iid].name)
        if key is None:
            continue
        R, t = net.pose[iid]
        J = np.hstack([-2.0 * R.T @ _skew(t), -R.T])
        a = 6 * net.index[iid]
        c = sigma0 ** 2 * J @ net.S[a:a + 6, a:a + 6] @ J.T
        cam = chunk.camera(key)
        var = np.diag(M @ c @ M.T) + _datum_variance(chunk.to_world(cam.center[None]), loc, acc)[0]
        cam.meta["precision"] = [float(v) for v in np.sqrt(np.clip(var, 0, None))]
        cams[key] = cam.meta["precision"]
    # markers measured in two photos or more
    by_name = {im.name: iid for iid, im in net.rec.images.items()}
    sfm_names = (chunk.meta.get("sfm") or {}).get("names", {})
    markers = {}
    for m in chunk.markers:
        if m.position is None:
            continue
        o = []
        for cam_key, proj in m.projections.items():
            iid = by_name.get(sfm_names.get(str(cam_key), ""))
            if iid is None or not proj.measured or iid not in net.index:
                continue
            jx, jc, _ = net.jacobians(iid, np.asarray(m.position, float)[None])
            o.append((net.index[iid], jx[0], jc[0]))
        c = net.point_covariance(o, marker_px, sigma0)
        if c is None:
            continue
        var = np.diag(M @ c @ M.T) + _datum_variance(chunk.to_world(np.asarray(m.position, float)[None]), loc, acc)[0]
        markers[str(m.key)] = [float(v) for v in np.sqrt(np.clip(var, 0, None))]
    cam_sig = np.array(list(cams.values())).reshape(-1, 3)
    report = {
        "date": datetime.datetime.now().isoformat(timespec="seconds"),
        "datum": kind, "held": net.held, "sigma0_px": sigma0, "marker_px": float(marker_px),
        "cameras": {"count": len(cams), "median": np.median(cam_sig, 0).tolist() if len(cams) else None,
                    "max": cam_sig.max(0).tolist() if len(cams) else None},
        "tie_points": {"count": int(len(sig)), "median": np.median(sig, 0).tolist() if len(sig) else None,
                       "p90": np.percentile(sig, 90, axis=0).tolist() if len(sig) else None,
                       "median_network": np.median(sig_net, 0).tolist() if len(sig) else None},
        "markers": markers,
    }
    chunk.meta["precision"] = report
    progress(1.0, f"Precision: {len(sig):,} tie points, {len(cams)} cameras, {len(markers)} markers")
    return report


def tie_point_precision(chunk: Chunk):
    """``(xyz world (n, 3), sigma (n, 3))`` of the last :func:`estimate_precision`, or None."""
    path = chunk.data_dir / "precision" / "tie_points.npz"
    if not path.exists():
        return None
    with np.load(path) as f:
        return f["xyz"], f["sigma"]


def dem_precision(chunk: Chunk, max_side: int = 1024, progress: Progress | None = None):
    """Raster of the vertical precision (sigma Z, metres) over the DEM, from the tie points of
    :func:`estimate_precision` (linear between them, the nearest one outside): a GeoTIFF in
    the CRS of the DEM, at most ``max_side`` cells a side (the precision changes slowly), only
    where the DEM has heights. The product ``dem_precision``; returns its path."""
    import rasterio
    from rasterio.enums import Resampling
    from scipy.interpolate import LinearNDInterpolator, NearestNDInterpolator

    progress = as_progress(progress)
    found = tie_point_precision(chunk)
    if found is None:
        raise RuntimeError("estimate the precision first")
    dem = chunk.dem_path
    if dem is None or not dem.exists():
        raise RuntimeError("build the DEM first")
    xyz, sigma = found
    if len(xyz) < 4:
        raise RuntimeError("too few tie points with a precision")
    with rasterio.open(dem) as src:
        f = max(1.0, max(src.width, src.height) / max_side)
        w, h = max(1, int(round(src.width / f))), max(1, int(round(src.height / f)))
        transform = src.transform @ src.transform.scale(src.width / w, src.height / h)
        valid = ~src.read(1, out_shape=(h, w), resampling=Resampling.nearest, masked=True).mask
        valid = np.broadcast_to(valid, (h, w))
        crs = src.crs
    progress(0.3, "Precision of the DEM")
    cols, rows = np.meshgrid(np.arange(w) + 0.5, np.arange(h) + 0.5)
    gx = transform.a * cols + transform.b * rows + transform.c
    gy = transform.d * cols + transform.e * rows + transform.f
    pts, vals = xyz[:, :2], sigma[:, 2].astype(np.float64)
    out = LinearNDInterpolator(pts, vals)(gx, gy)
    holes = ~np.isfinite(out)
    if holes.any():
        out[holes] = NearestNDInterpolator(pts, vals)(gx[holes], gy[holes])
    out = np.where(valid, out, -9999.0).astype(np.float32)
    path = chunk.data_dir / "precision" / "dem_sigma_z.tif"
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", driver="GTiff", width=w, height=h, count=1, dtype="float32", crs=crs,
                       transform=transform, nodata=-9999.0, compress="deflate", BIGTIFF="IF_SAFER") as dst:
        dst.write(out, 1)
    chunk._set_product("dem_precision", path)
    progress(1.0, f"Precision of the DEM: median sigma Z {np.median(out[valid]) * 1000:.1f} mm")
    return path


def precision_at(chunk: Chunk, x: float, y: float) -> float | None:
    """Vertical precision (metres) of the DEM at world ``(x, y)``, None without the raster."""
    import rasterio

    path = chunk.product_path("dem_precision")
    if path is None or not path.exists():
        return None
    with rasterio.open(path) as src:
        col, row = ~src.transform @ (x, y)
        if not (0 <= col < src.width and 0 <= row < src.height):
            return None
        v = float(src.read(1, window=((int(row), int(row) + 1), (int(col), int(col) + 1)))[0, 0])
    return None if v == -9999.0 or not np.isfinite(v) else v
