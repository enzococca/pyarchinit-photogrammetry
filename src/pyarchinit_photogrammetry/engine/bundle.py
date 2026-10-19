"""Our own bundle adjustment, for what COLMAP's (pycolmap) cannot take:

* control points with their accuracy - pulled towards their coordinates, as in Metashape, not
  held fixed;
* scale bars as distances between the markers at their ends, weighted by their accuracy;
* camera positions measured by GNSS, with a bias per flight estimated with the block (an RTK
  base set up on a wrong point, an antenna offset not entered, a datum shift);
* rolling shutter: the camera moving and turning while the rows of the sensor are read out (most
  drones), a linear and an angular velocity per photo, the time of an observation from its row.

:func:`adjust` solves a :class:`Problem` (sparse trust-region least squares of scipy; the Jacobian
by finite differences grouped by the structure of the problem - a residual depends on one photo,
one point, one camera - so a few dozen evaluations give all of it; projection with the COLMAP
camera models of pycolmap). :func:`problem_from_chunk` builds it from the alignment of a chunk
and its markers, bars and camera references; :func:`write_back` puts the result in the
reconstruction, the markers and the cameras.

Units are those of the reconstruction (the internal frame of the chunk); accuracies given in
metres are divided by the scale of the chunk.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..core.progress import Progress, as_progress

MARKER_PIXEL_ACCURACY = 0.5  # px, as Metashape's default and :data:`sfm.MARKER_PIXEL_ACCURACY`
# rolling shutter: a weak prior towards a still camera (metres and radians while a frame is read)
ROLLING_SHUTTER_SIGMA_M = 1.0
ROLLING_SHUTTER_SIGMA_RAD = 0.005
# ... for a chunk, as the pixels the motion would shift the image by: a still camera unless the photos say otherwise
ROLLING_SHUTTER_SIGMA_PX = 2.0
FLIGHT_GAP_S = 20 * 60  # photos taken more than this apart belong to different flights


@dataclass
class Problem:
    """A block to adjust (internal frame). Images: ``rotations`` (n, 3, 3) world -> camera,
    ``centres`` (n, 3), ``image_camera`` (n,) index into ``cameras`` [(model, params, width,
    height)]; ``points`` (p, 3) observed by ``obs_image``, ``obs_point``, ``obs_xy`` (COLMAP pixel
    coordinates); ``markers`` (m, 3) observed by ``mobs_image``, ``mobs_marker``, ``mobs_xy``;
    ``gcps`` [(marker, target (3,), sigma (3,))]; ``bars`` [(marker a, marker b, distance, sigma)];
    ``gnss`` [(image, target (3,), sigma (3,), flight)]; ``fixed_images`` / ``fixed_cameras``
    held as they are; ``free_intrinsics`` per camera, the indices of its parameters to adjust."""

    rotations: np.ndarray
    centres: np.ndarray
    image_camera: np.ndarray
    cameras: list
    points: np.ndarray
    obs_image: np.ndarray
    obs_point: np.ndarray
    obs_xy: np.ndarray
    markers: np.ndarray = field(default_factory=lambda: np.zeros((0, 3)))
    mobs_image: np.ndarray = field(default_factory=lambda: np.zeros(0, int))
    mobs_marker: np.ndarray = field(default_factory=lambda: np.zeros(0, int))
    mobs_xy: np.ndarray = field(default_factory=lambda: np.zeros((0, 2)))
    gcps: list = field(default_factory=list)
    bars: list = field(default_factory=list)
    gnss: list = field(default_factory=list)
    fixed_images: set = field(default_factory=set)
    free_intrinsics: list | None = None
    rolling_shutter: bool = False
    gnss_bias: bool = False
    pixel_sigma: float = 1.0
    marker_sigma: float = MARKER_PIXEL_ACCURACY
    rs_sigma: float | np.ndarray = ROLLING_SHUTTER_SIGMA_M  # units of the problem (one, or one per photo)
    rs_sigma_angular: float | np.ndarray = ROLLING_SHUTTER_SIGMA_RAD


def _rodrigues(rotvecs: np.ndarray) -> np.ndarray:
    from scipy.spatial.transform import Rotation

    return Rotation.from_rotvec(rotvecs).as_matrix()


def _skew_apply(w: np.ndarray, v: np.ndarray) -> np.ndarray:
    """w x v, row by row."""
    return np.cross(w, v)


class _Layout:
    """Where each unknown of the problem sits in the vector of parameters."""

    def __init__(self, pb: Problem):
        n_img, n_cam = len(pb.centres), len(pb.cameras)
        self.free_images = [i for i in range(n_img) if i not in pb.fixed_images]
        self.free_intrinsics = pb.free_intrinsics if pb.free_intrinsics is not None else \
            [list(range(len(c[1]))) for c in pb.cameras]
        pos = 0
        self.image_col = np.full(n_img, -1)
        for i in self.free_images:
            self.image_col[i] = pos
            pos += 6
        self.camera_cols = []
        for c in range(n_cam):
            self.camera_cols.append(np.arange(pos, pos + len(self.free_intrinsics[c])))
            pos += len(self.free_intrinsics[c])
        self.point_col = pos
        pos += 3 * len(pb.points)
        self.marker_col = pos
        pos += 3 * len(pb.markers)
        self.rs_col = np.full(n_img, -1)
        if pb.rolling_shutter:
            for i in range(n_img):
                self.rs_col[i] = pos
                pos += 6
        flights = sorted({g[3] for g in pb.gnss}) if pb.gnss_bias else []
        self.bias_col = {}
        for f in flights:
            self.bias_col[f] = pos
            pos += 3
        self.size = pos


class _Model:
    """Residuals of a problem for a vector of parameters (see :class:`Problem`)."""

    def __init__(self, pb: Problem):
        import pycolmap

        self.pb, self.lay = pb, _Layout(pb)
        self.pycolmap = pycolmap
        self.cams = [pycolmap.Camera(model=m, width=int(w), height=int(h), params=list(map(float, p)))
                     for m, p, w, h in pb.cameras]
        self.heights = np.array([float(pb.cameras[c][3]) for c in pb.image_camera])

    def start(self) -> np.ndarray:
        pb, lay = self.pb, self.lay
        x = np.zeros(lay.size)
        for i in lay.free_images:
            x[lay.image_col[i] + 3:lay.image_col[i] + 6] = pb.centres[i]
        for c, cols in enumerate(lay.camera_cols):
            x[cols] = np.asarray(pb.cameras[c][1], float)[lay.free_intrinsics[c]]
        x[lay.point_col:lay.point_col + 3 * len(pb.points)] = pb.points.ravel()
        x[lay.marker_col:lay.marker_col + 3 * len(pb.markers)] = pb.markers.ravel()
        return x

    def unpack(self, x: np.ndarray):
        pb, lay = self.pb, self.lay
        rot = pb.rotations.copy()
        cen = pb.centres.copy()
        if lay.free_images:
            cols = lay.image_col[lay.free_images]
            delta = x[cols[:, None] + np.arange(3)]
            rot[lay.free_images] = _rodrigues(delta) @ pb.rotations[lay.free_images]
            cen[lay.free_images] = x[cols[:, None] + np.arange(3, 6)]
        params = []
        for c, cols in enumerate(lay.camera_cols):
            p = np.asarray(pb.cameras[c][1], float).copy()
            p[lay.free_intrinsics[c]] = x[cols]
            params.append(p)
        pts = x[lay.point_col:lay.point_col + 3 * len(pb.points)].reshape(-1, 3)
        mks = x[lay.marker_col:lay.marker_col + 3 * len(pb.markers)].reshape(-1, 3)
        rs = None
        if pb.rolling_shutter:
            rs = x[lay.rs_col[:, None] + np.arange(6)]
        bias = {f: x[c:c + 3] for f, c in lay.bias_col.items()}
        return rot, cen, params, pts, mks, rs, bias

    def _project(self, rot, cen, params, rs, image, world, xy):
        """Pixels of ``world`` points seen from photos ``image`` (with the rolling shutter, at the
        time of the observed row ``xy[:, 1]``); NaN behind the camera."""
        rel = world - cen[image]
        if rs is not None:
            tau = (xy[:, 1] / self.heights[image] - 0.5)[:, None]
            rel = rel - rs[image, :3] * tau
            cam_pts = np.einsum("nij,nj->ni", rot[image], rel)
            cam_pts = cam_pts + _skew_apply(rs[image, 3:] * tau, cam_pts)
        else:
            cam_pts = np.einsum("nij,nj->ni", rot[image], rel)
        out = np.full((len(image), 2), np.nan)
        cam_of = self.pb.image_camera[image]
        for c in np.unique(cam_of):
            sel = np.flatnonzero(cam_of == c)
            pts = cam_pts[sel]
            front = pts[:, 2] > 1e-9
            if not front.any():
                continue
            cam = self.cams[c]
            cam.params = params[c]
            out[sel[front]] = np.asarray(cam.img_from_cam(pts[front]))[:, :2]
        return out

    def residuals(self, x: np.ndarray) -> np.ndarray:
        pb = self.pb
        rot, cen, params, pts, mks, rs, bias = self.unpack(x)
        parts = []
        uv = self._project(rot, cen, params, rs, pb.obs_image, pts[pb.obs_point], pb.obs_xy)
        r = (uv - pb.obs_xy) / pb.pixel_sigma
        parts.append(np.nan_to_num(r, nan=1e3).ravel())  # behind a camera: a large error, not a hole
        if len(pb.mobs_image):
            uv = self._project(rot, cen, params, rs, pb.mobs_image, mks[pb.mobs_marker], pb.mobs_xy)
            parts.append(np.nan_to_num((uv - pb.mobs_xy) / pb.marker_sigma, nan=1e3).ravel())
        for m, target, sigma in pb.gcps:
            parts.append((mks[m] - target) / sigma)
        for a, b, dist, sigma in pb.bars:
            parts.append(np.array([(np.linalg.norm(mks[a] - mks[b]) - dist) / sigma]))
        for i, target, sigma, flight in pb.gnss:  # measured = true position + bias of the flight
            parts.append((cen[i] + bias.get(flight, 0.0) - target) / sigma)
        if rs is not None:
            n = len(rs)
            sv = np.broadcast_to(np.asarray(pb.rs_sigma, float), (n,))[:, None]
            sw = np.broadcast_to(np.asarray(pb.rs_sigma_angular, float), (n,))[:, None]
            parts.append((rs / np.hstack([np.repeat(sv, 3, 1), np.repeat(sw, 3, 1)])).ravel())
        return np.concatenate(parts)

    def sparsity(self):
        """Which parameters each residual depends on (the structure the Jacobian is grouped by)."""
        from scipy.sparse import coo_matrix

        pb, lay = self.pb, self.lay
        rows, cols = [], []
        row = 0

        def add(r0, nr, cs):
            for k in range(nr):
                rows.extend([r0 + k] * len(cs))
                cols.extend(cs)

        def image_cols(i):
            cs = []
            if lay.image_col[i] >= 0:
                cs += list(range(lay.image_col[i], lay.image_col[i] + 6))
            cs += list(lay.camera_cols[pb.image_camera[i]])
            if lay.rs_col[i] >= 0:
                cs += list(range(lay.rs_col[i], lay.rs_col[i] + 6))
            return cs

        img_cols = [image_cols(i) for i in range(len(pb.centres))]
        for i, p in zip(pb.obs_image, pb.obs_point):
            add(row, 2, img_cols[i] + [lay.point_col + 3 * p + k for k in range(3)])
            row += 2
        for i, m in zip(pb.mobs_image, pb.mobs_marker):
            add(row, 2, img_cols[i] + [lay.marker_col + 3 * m + k for k in range(3)])
            row += 2
        for m, _, _ in pb.gcps:
            for k in range(3):
                add(row, 1, [lay.marker_col + 3 * m + k])
                row += 1
        for a, b, _, _ in pb.bars:
            add(row, 1, [lay.marker_col + 3 * a + k for k in range(3)] + [lay.marker_col + 3 * b + k for k in range(3)])
            row += 1
        for i, _, _, flight in pb.gnss:
            for k in range(3):
                cs = [lay.image_col[i] + 3 + k] if lay.image_col[i] >= 0 else []
                if flight in lay.bias_col:
                    cs.append(lay.bias_col[flight] + k)
                add(row, 1, cs)
                row += 1
        if pb.rolling_shutter:
            for i in range(len(pb.centres)):
                for k in range(6):
                    add(row, 1, [lay.rs_col[i] + k])
                    row += 1
        data = np.ones(len(rows), np.int8)
        return coo_matrix((data, (rows, cols)), shape=(row, lay.size)).tocsr()


@dataclass
class Result:
    rotations: np.ndarray
    centres: np.ndarray
    camera_params: list
    points: np.ndarray
    markers: np.ndarray
    rolling_shutter: np.ndarray | None
    bias: dict
    rms_before: float
    rms_after: float
    reprojection_before: float
    reprojection_after: float
    iterations: int
    success: bool


def _reprojection_rms(model: _Model, x: np.ndarray) -> float:
    pb = model.pb
    rot, cen, params, pts, _, rs, _ = model.unpack(x)
    uv = model._project(rot, cen, params, rs, pb.obs_image, pts[pb.obs_point], pb.obs_xy)
    e = np.linalg.norm(uv - pb.obs_xy, axis=1)
    e = e[np.isfinite(e)]
    return float(np.sqrt(np.mean(e * e))) if len(e) else float("nan")


def adjust(pb: Problem, max_iterations: int = 100, progress: Progress | None = None) -> Result:
    """Least squares of every residual of ``pb`` (see the module docstring)."""
    from scipy.optimize import least_squares

    progress = as_progress(progress)
    model = _Model(pb)
    x0 = model.start()
    r0 = model.residuals(x0)
    rep0 = _reprojection_rms(model, x0)
    progress(0.1, f"Bundle adjustment: {len(pb.centres)} photos, {len(pb.points)} points, "
                  f"{len(pb.obs_image)} observations, {model.lay.size} unknowns")
    sol = least_squares(model.residuals, x0, jac_sparsity=model.sparsity(), method="trf", tr_solver="lsmr",
                        x_scale="jac", max_nfev=max_iterations, xtol=1e-9, ftol=1e-9, gtol=1e-9)
    rot, cen, params, pts, mks, rs, bias = model.unpack(sol.x)
    rms = lambda r: float(np.sqrt(np.mean(r * r)))  # noqa: E731
    out = Result(rot, cen, params, pts, mks, rs, {f: np.asarray(v, float) for f, v in bias.items()}, rms(r0),
                 rms(sol.fun), rep0, _reprojection_rms(model, sol.x), int(sol.nfev), bool(sol.success))
    progress(1.0, f"Reprojection error {out.reprojection_before:.3f} -> {out.reprojection_after:.3f} px")
    return out


# ---------------------------------------------------------------- a chunk as a problem
def flights(cameras) -> dict:
    """Camera key -> flight number: the photos in the order they were taken, a new flight after a
    pause longer than :data:`FLIGHT_GAP_S` (and per sensor); photos without a date: flight 0."""
    from datetime import datetime

    def when(c):
        t = (c.meta or {}).get("datetime")
        try:
            return datetime.strptime(str(t)[:19], "%Y:%m:%d %H:%M:%S").timestamp()
        except (TypeError, ValueError):
            return None

    out, last, number = {}, {}, 0
    dated = sorted(((when(c), c) for c in cameras if when(c) is not None), key=lambda p: p[0])
    for t, c in dated:
        prev = last.get(c.sensor_key)
        if prev is None or t - prev[0] > FLIGHT_GAP_S:
            number += 1
            last[c.sensor_key] = (t, number)
        else:
            last[c.sensor_key] = (t, prev[1])
        out[c.key] = last[c.sensor_key][1]
    for c in cameras:
        out.setdefault(c.key, 0)
    return out


def problem_from_chunk(chunk, rec, use_markers: bool = True, gnss: str = "off", rolling_shutter: bool = False,
                       fit_principal_point: bool = False) -> tuple[Problem, dict]:
    """The adjustment of ``chunk`` (its reconstruction ``rec``, internal frame): tie points; with
    ``use_markers`` the measured projections of the markers, the control points (enabled
    reference, not check points) with their accuracy and the enabled control scale bars; with
    ``gnss`` "plain" or "bias" the camera references (with a bias per flight); rolling shutter.
    Returns the problem and the ids that :func:`write_back` needs."""
    from ..markers import gcp
    from .sfm import _rec_fixed_cameras

    if any(len(list(f.image_ids)) > 1 for f in rec.frames.values()):
        raise ValueError("the reconstruction has rigs (faces of 360-degree photos): use COLMAP's adjustment")
    s = float(chunk.scale) if chunk.transform is not None else 1.0
    names = chunk.meta["sfm"]["names"]
    key_of = {v: int(k) for k, v in names.items()}
    image_ids = [iid for iid, img in rec.images.items() if img.has_pose]
    index = {iid: n for n, iid in enumerate(image_ids)}
    cam_ids = sorted({rec.images[iid].camera_id for iid in image_ids})
    cam_index = {c: n for n, c in enumerate(cam_ids)}
    rotations, centres = [], []
    for iid in image_ids:
        pose = rec.images[iid].cam_from_world()
        m = np.asarray(pose.matrix())
        rotations.append(m[:3, :3])
        centres.append(-m[:3, :3].T @ m[:3, 3])
    cameras = [(rec.cameras[c].model.name, list(rec.cameras[c].params), rec.cameras[c].width, rec.cameras[c].height)
               for c in cam_ids]
    # tie points
    point_ids = list(rec.points3D)
    pindex = {pid: n for n, pid in enumerate(point_ids)}
    oi, op, oxy = [], [], []
    for pid in point_ids:
        for el in rec.points3D[pid].track.elements:
            if el.image_id in index:
                oi.append(index[el.image_id])
                op.append(pindex[pid])
                oxy.append(rec.images[el.image_id].points2D[el.point2D_idx].xy)
    points = np.array([rec.points3D[pid].xyz for pid in point_ids], float).reshape(-1, 3)
    pb = Problem(np.array(rotations), np.array(centres), np.array([cam_index[rec.images[i].camera_id] for i in image_ids]),
                 cameras, points, np.array(oi, int), np.array(op, int), np.array(oxy, float).reshape(-1, 2),
                 rolling_shutter=rolling_shutter, gnss_bias=gnss == "bias")
    if rolling_shutter:  # the prior as pixels of shift: the depth of the scene and the focal length of each photo
        focal = np.array([float(cameras[c][1][0]) for c in pb.image_camera])
        depth = np.ones(len(image_ids))
        for n in range(len(image_ids)):
            sel = pb.obs_point[pb.obs_image == n]
            if len(sel):
                z = (pb.points[sel] - pb.centres[n]) @ pb.rotations[n][2]
                depth[n] = float(np.median(z[z > 0])) if (z > 0).any() else 1.0
        pb.rs_sigma = ROLLING_SHUTTER_SIGMA_PX * depth / focal
        pb.rs_sigma_angular = ROLLING_SHUTTER_SIGMA_PX / focal
    # what stays as it is
    fixed = _rec_fixed_cameras(chunk, rec)
    pb.fixed_images = {index[i] for i in image_ids if i in fixed}
    pb.free_intrinsics = []
    for c in cam_ids:
        model_name = rec.cameras[c].model.name
        n = len(rec.cameras[c].params)
        held = {chunk.sensors[chunk.camera(key_of[rec.images[i].name]).sensor_key].fixed
                for i in image_ids if rec.images[i].camera_id == c and rec.images[i].name in key_of}
        if True in held:
            pb.free_intrinsics.append([])
            continue
        principal = (1, 2) if model_name in ("SIMPLE_PINHOLE", "SIMPLE_RADIAL", "RADIAL", "SIMPLE_RADIAL_FISHEYE",
                                             "RADIAL_FISHEYE") else (2, 3)
        pb.free_intrinsics.append([k for k in range(n) if fit_principal_point or k not in principal])
    marker_keys = []
    if use_markers:
        controls = {m.key: m for m in chunk.markers if not m.checkpoint and m.reference.enabled
                    and m.reference.location}
        bars = gcp.control_scalebars(chunk)
        wanted = set(controls) | {k for sb in bars for k in (sb.marker_a, sb.marker_b)}
        mi, mm, mxy, positions = [], [], [], []
        by_name = {rec.images[i].name: index[i] for i in image_ids}
        for m in chunk.markers:
            if m.key not in wanted:
                continue
            obs = [(by_name[names[str(k)]], p.x, p.y) for k, p in m.projections.items()
                   if p.measured and names.get(str(k)) in by_name]
            if m.position is None and m.key not in controls:
                continue
            start = np.asarray(m.position, float) if m.position is not None else \
                chunk.to_internal(np.asarray([m.reference.location], float))[0]
            if len(obs) == 0 and m.key not in controls:
                continue
            n = len(marker_keys)
            marker_keys.append(m.key)
            positions.append(start)
            for i, x, y in obs:
                mi.append(i)
                mm.append(n)
                mxy.append((x, y))
        pb.markers = np.array(positions, float).reshape(-1, 3)
        pb.mobs_image, pb.mobs_marker = np.array(mi, int), np.array(mm, int)
        pb.mobs_xy = np.array(mxy, float).reshape(-1, 2)
        mpos = {k: n for n, k in enumerate(marker_keys)}
        for k, m in controls.items():
            if k in mpos:
                target = chunk.to_internal(np.asarray([m.reference.location], float))[0]
                sigma = np.maximum(np.asarray(m.reference.accuracy, float), 1e-6) / s
                pb.gcps.append((mpos[k], target, sigma))
        for sb in bars:
            if sb.marker_a in mpos and sb.marker_b in mpos:
                pb.bars.append((mpos[sb.marker_a], mpos[sb.marker_b], sb.distance / s, max(sb.accuracy, 1e-6) / s))
    if gnss in ("plain", "bias"):
        groups = flights(chunk.cameras)
        for iid in image_ids:
            key = key_of.get(rec.images[iid].name)
            if key is None:
                continue
            cam = chunk.camera(key)
            ref = cam.reference
            if ref.enabled and ref.location:
                target = chunk.to_internal(np.asarray([ref.location], float))[0]
                sigma = np.maximum(np.asarray(ref.accuracy, float), 1e-6) / s
                pb.gnss.append((index[iid], target, sigma, groups.get(key, 0)))
    absolute = len(pb.gcps) >= 3 or len(pb.gnss) >= 3
    if not absolute and not pb.fixed_images and len(image_ids) >= 2:
        pb.fixed_images = {0}  # the block needs a place: the first photo holds it
        if not pb.bars:  # and a size: the second one too
            far = int(np.argmax(np.linalg.norm(pb.centres - pb.centres[0], axis=1)))
            pb.fixed_images.add(far)
    ids = {"images": image_ids, "cameras": cam_ids, "points": point_ids, "markers": marker_keys}
    return pb, ids


def write_back(chunk, rec, ids: dict, result: Result) -> None:
    """The adjusted poses, calibrations and points into ``rec``, the markers into the chunk, the
    rolling-shutter velocities into the cameras' ``meta["rolling_shutter"]``."""
    import pycolmap

    for n, iid in enumerate(ids["images"]):
        r = result.rotations[n]
        pose = pycolmap.Rigid3d(pycolmap.Rotation3d(r), -r @ result.centres[n])
        image = rec.images[iid]  # one photo per frame (checked): the frame takes the pose of its camera
        rec.frames[image.frame_id].set_cam_from_world(image.camera_id, pose)
    for n, c in enumerate(ids["cameras"]):
        rec.cameras[c].params = list(map(float, result.camera_params[n]))
    for n, pid in enumerate(ids["points"]):
        rec.points3D[pid].xyz = result.points[n]
    for n, key in enumerate(ids["markers"]):
        chunk.marker_by_key(key).position = np.asarray(result.markers[n], float)
    if result.rolling_shutter is not None:
        names = {v: int(k) for k, v in chunk.meta["sfm"]["names"].items()}
        for n, iid in enumerate(ids["images"]):
            key = names.get(rec.images[iid].name)
            if key is not None:
                chunk.camera(key).meta["rolling_shutter"] = [float(v) for v in result.rolling_shutter[n]]
