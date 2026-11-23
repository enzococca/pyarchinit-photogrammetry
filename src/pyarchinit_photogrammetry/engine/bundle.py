"""Our own bundle adjustment, for what COLMAP's (pycolmap) cannot take:

* control points with their accuracy - pulled towards their coordinates, as in Metashape, not
  held fixed;
* scale bars as distances between the markers at their ends, weighted by their accuracy;
* camera positions measured by GNSS, with a bias per flight estimated with the block (an RTK
  base set up on a wrong point, an antenna offset not entered, a datum shift);
* rolling shutter: the camera moving and turning while the rows of the sensor are read out (most
  drones), a linear and an angular velocity per photo, the time of an observation from its row.

:func:`adjust` solves a :class:`Problem` with one of two solvers. scipy's sparse trust-region least
squares is the reference: the Jacobian by finite differences grouped by the structure of the
problem - a residual depends on one photo, one point, one camera - so a few dozen evaluations give
all of it; projection with the COLMAP camera models of pycolmap. The native solver of the core
(``ppg_core``, API 12) is the same problem solved in Rust: Levenberg-Marquardt over the camera
system reduced by the Schur complement, analytic Jacobians, a robust loss on the tie points; it is
chosen with ``solver=`` (:data:`SOLVERS`), and where it cannot run scipy adjusts the block and the
result says why (:func:`native_ready`). :func:`evaluate` gives the residuals, gradient and
Jacobian of a problem by the native engine; :func:`problem_at` a problem started where a result
ended. :func:`problem_from_chunk` builds the problem from the alignment of a chunk and its markers,
bars and camera references; :func:`write_back` puts the result in the reconstruction, the markers
and the cameras.

Units are those of the reconstruction (the internal frame of the chunk); accuracies given in
metres are divided by the scale of the chunk.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field, replace

import numpy as np

from .. import _native
from ..core.progress import Progress, as_progress

log = logging.getLogger(__name__)

MARKER_PIXEL_ACCURACY = 0.5  # px, as Metashape's default and :data:`sfm.MARKER_PIXEL_ACCURACY`
# rolling shutter: a weak prior towards a still camera (metres and radians while a frame is read)
ROLLING_SHUTTER_SIGMA_M = 1.0
ROLLING_SHUTTER_SIGMA_RAD = 0.005
# ... for a chunk, as the pixels the motion would shift the image by: a still camera unless the photos say otherwise
ROLLING_SHUTTER_SIGMA_PX = 2.0
FLIGHT_GAP_S = 20 * 60  # photos taken more than this apart belong to different flights
# the solvers of adjust(): "auto" is the native one since it passed its gate (NATIVE_BY_DEFAULT; it was scipy
# before, and PPG_ENGINE=native still asks for it by name), scipy where it cannot run (native_ready) or by name
SOLVERS = ("auto", "native", "scipy")
NATIVE_BY_DEFAULT = True
LOSSES = ("none", "huber", "cauchy")  # on the tie points; the scale in the units of the residuals
# relative decrease of the cost that ends the adjustment: scipy's ftol and the native solver's; under Huber and
# Cauchy the native solver applies at least ROBUST_FUNCTION_TOLERANCE (the cost of a wrong track is a plateau
# along which it would gain 1e-8 of the cost per iteration for ever)
FUNCTION_TOLERANCE = 1e-9
ROBUST_FUNCTION_TOLERANCE = 1e-6
NATIVE_MISSING = "the native core with the bundle adjustment (API 12) is not installed"
INTRINSICS_MISSING = "the native core with the variances of the intrinsics (API 17) is not installed"
# the camera optimisation refines each group of the intrinsics of a camera only where the block determines it
# (intrinsic_sds; the rule of the rounds of the native mapper, native/engine/src/mapper/rounds.rs, taken group by
# group): a focal length whose relative marginal sd is above FOCAL_SD_HOLD is held, the distortion still refined;
# a principal point that was asked for is held when its marginal sd is above PRINCIPAL_SD_HOLD of the side of the
# photo. What an adjustment then moves too far is held and the adjustment run again (runaways): a focal length by
# more than FOCAL_MOVE_HOLD of its start, the first radial term by more than K1_MOVE_HOLD
FOCAL_SD_HOLD = 0.005
PRINCIPAL_SD_HOLD = 0.005
FOCAL_MOVE_HOLD = 0.2
K1_MOVE_HOLD = 0.5
# the size of a block nothing places is held, for its covariance, by a position of one photo whose sigma is this
# many times the extent of the block: a gauge that adds no information the block has (the sd of a focal length is
# the same to four digits from a tenth of the extent to a hundred times it). Not looser: the rounds of the native
# mapper use 100, and beside the tie points of a block of hundreds of photos that is below what double precision
# keeps - the normal matrix then fails to invert now and then (400 photos: a pivot that is not positive)
GAUGE_SIGMA_EXTENTS = 1.0


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

    @property
    def absolute(self) -> bool:
        """The references place the block: three control points or more, or three GNSS positions or more
        (scale bars give it a size, not a place). Else :func:`problem_from_chunk` holds a photo - two,
        without bars - and the camera optimisation holds the focal length, which a block nothing places
        cannot tell from the depth of the scene (:func:`hold_focal_length`)."""
        return len(self.gcps) >= 3 or len(self.gnss) >= 3


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
    """What :func:`adjust` gives: the adjusted block (``bias`` per flight, ``rolling_shutter`` (n, 6)
    when it was adjusted), the RMS of the whole residual vector and the reprojection RMS of the tie
    points before and after, and, after ``success``, what the solve was: its ``cost`` (0.5 sum
    rho(r^2) at the end: scipy's ``cost``, the native solver's), the largest entry of the gradient at
    the end, which ``solver`` ran, why the native one did not though it was wanted (``fallback``), the
    ``seconds`` of the solve alone, the native ``linear_solver`` (``dense``, ``sparse``, ``pcg``; None for
    scipy) and the ``termination`` (``function``, ``gradient``, ``parameters``, ``max_iterations``,
    ``no_progress``; scipy's status as a word)."""

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
    cost: float = float("nan")
    gradient_max: float = float("nan")
    solver: str = "scipy"
    fallback: str | None = None
    seconds: float = float("nan")
    linear_solver: str | None = None
    termination: str | None = None


@dataclass
class Evaluation:
    """A problem evaluated at the parameters it holds by the native engine (:func:`evaluate`): the
    cost ``0.5 sum(rho(r^2))``, the residuals (the rows of :meth:`_Model.residuals`), the gradient of
    the cost in the columns of :class:`_Layout` and the Jacobian of the residuals (``scipy.sparse``
    CSR) on the structure of :meth:`_Model.sparsity`."""

    cost: float
    residuals: np.ndarray
    gradient: np.ndarray
    jacobian: object


def _reprojection_rms(model: _Model, x: np.ndarray) -> float:
    pb = model.pb
    rot, cen, params, pts, _, rs, _ = model.unpack(x)
    uv = model._project(rot, cen, params, rs, pb.obs_image, pts[pb.obs_point], pb.obs_xy)
    e = np.linalg.norm(uv - pb.obs_xy, axis=1)
    e = e[np.isfinite(e)]
    return float(np.sqrt(np.mean(e * e))) if len(e) else float("nan")


def _rms(r: np.ndarray) -> float:
    return float(np.sqrt(np.mean(r * r))) if len(r) else float("nan")


# ---------------------------------------------------------------- the solvers
def _native_missing() -> str | None:
    """Why the native solver cannot run at all - switched off, not installed, older than API 12 - or None."""
    if not _native.enabled():
        return "the native core is switched off by PPG_NATIVE=0"
    if not _native.has("bundle") or (_native.api() or 0) < _native.BUNDLE_API:
        return NATIVE_MISSING
    return None


def native_ready(pb: Problem) -> str | None:
    """``None`` when the native solver can take ``pb``, else why not: the core switched off, missing or
    older than API 12, or a camera model that is not among the six of its projection kernels."""
    reason = _native_missing()
    if reason is not None:
        return reason
    for model, *_ in pb.cameras:
        if not _native.supports(model):
            return f"camera model {model} is not in the native solver"
    return None


def _native_function(name: str):
    """``ppg_core.name`` (``bundle_adjust``, ``bundle_evaluate``); RuntimeError when the core cannot run."""
    reason = _native_missing()
    if reason is not None:
        raise RuntimeError(reason)
    return _native.kernel(name)


def _referenced(pb: Problem) -> bool:
    """``pb`` has residuals besides the tie points: measured markers, control points, scale bars, GNSS
    positions or the rolling shutter. scipy would weigh them by a robust loss too (the native solver weighs
    the tie points only), so a robust loss on such a block needs the native solver."""
    return bool(len(pb.mobs_image) or pb.gcps or pb.bars or pb.gnss or pb.rolling_shutter)


def _choose(solver: str, pb: Problem, loss: str) -> tuple[str, str, str | None]:
    """The solver that runs for the word ``solver``, the loss it runs with, and, when the native one was
    wanted and cannot run, the reason: a warning in the log when it was asked for (``"native"``, or
    ``PPG_ENGINE=native``), a line when ``"auto"`` had turned to it on its own. A fallback is never an
    error: a robust loss on a block with references (:func:`_referenced`) is dropped for the squared loss,
    and the reason says so - a warning whichever way the native solver was wanted."""
    asked = os.environ.get("PPG_ENGINE", "").strip().lower() == "native"
    wanted = solver if solver != "auto" else ("native" if NATIVE_BY_DEFAULT or asked else "scipy")
    if wanted == "scipy":
        return "scipy", loss, None
    reason = native_ready(pb)
    if reason is None:
        return "native", loss, None
    dropped = loss != "none" and _referenced(pb)
    if dropped:
        reason += (f"; scipy adjusts the block with the squared loss instead of {loss} (a robust loss on a block "
                   "with references needs the native core)")
        loss = "none"
        line = reason
    else:
        line = f"{reason}; scipy adjusts the block instead"
    level = logging.WARNING if solver == "native" or asked or dropped else logging.INFO
    log.log(level, "Bundle adjustment: %s", line)
    return "scipy", loss, reason


def _adjustable(pb: Problem) -> np.ndarray:
    """Which tie points have at least two observations in front of a camera at the start of ``pb``. A
    point with fewer cannot be adjusted: the native solver holds it, scipy would move it along its ray;
    :func:`adjust` leaves it out of both and in the result where it started."""
    rel = pb.points[pb.obs_point] - pb.centres[pb.obs_image]
    depth = np.einsum("nj,nj->n", pb.rotations[pb.obs_image, 2, :], rel)
    seen = np.bincount(pb.obs_point[depth > 1e-9], minlength=len(pb.points))
    return seen >= 2


def _without_held(pb: Problem, keep: np.ndarray) -> Problem:
    """``pb`` without the tie points not in ``keep`` and their observations."""
    index = np.cumsum(keep) - 1
    sel = keep[pb.obs_point]
    return replace(pb, points=pb.points[keep], obs_image=pb.obs_image[sel], obs_point=index[pb.obs_point[sel]],
                   obs_xy=pb.obs_xy[sel])


def _start(model: _Model, progress: Progress, solver: str, held: int) -> tuple[np.ndarray, np.ndarray, float]:
    """The start of an adjustment: its parameters, residuals and reprojection RMS, reported."""
    pb = model.pb
    x0 = model.start()
    r0 = model.residuals(x0)
    note = f" ({held} seen by fewer than two photos held)" if held else ""
    progress(0.1, f"Bundle adjustment ({solver}): {len(pb.centres)} photos, {len(pb.points)} points{note}, "
                  f"{len(pb.obs_image)} observations, {model.lay.size} unknowns")
    return x0, r0, _reprojection_rms(model, x0)


_SCIPY_STATUS = {-1: "improper_input", 0: "max_iterations", 1: "gradient", 2: "function", 3: "parameters",
                 4: "function_and_parameters"}


def _adjust_scipy(pb: Problem, max_iterations: int, progress: Progress, loss: str, loss_scale: float,
                  held: int) -> Result:
    """The trust-region least squares of scipy (the reference). scipy weighs every residual by a robust
    loss where the native solver weighs the tie points only: a loss is taken on a problem of tie points alone."""
    from scipy.optimize import least_squares

    if loss != "none" and _referenced(pb):
        raise ValueError(f"loss {loss!r} with the scipy solver: scipy would weigh the measured markers, control "
                         "points, scale bars, GNSS positions or rolling shutter by the loss too; it takes a robust "
                         "loss on tie points alone, the native solver on any problem")
    model = _Model(pb)
    x0, r0, rep0 = _start(model, progress, "scipy", held)
    t0 = time.perf_counter()
    sol = least_squares(model.residuals, x0, jac_sparsity=model.sparsity(), method="trf", tr_solver="lsmr",
                        x_scale="jac", max_nfev=max_iterations, xtol=1e-9, ftol=FUNCTION_TOLERANCE, gtol=1e-9,
                        loss="linear" if loss == "none" else loss, f_scale=loss_scale)
    seconds = time.perf_counter() - t0
    rot, cen, params, pts, mks, rs, bias = model.unpack(sol.x)
    out = Result(rot, cen, params, pts, mks, rs, {f: np.asarray(v, float) for f, v in bias.items()}, _rms(r0),
                 _rms(sol.fun), rep0, _reprojection_rms(model, sol.x), int(sol.nfev), bool(sol.success),
                 cost=float(sol.cost), gradient_max=float(np.abs(sol.grad).max()) if sol.grad.size else 0.0,
                 solver="scipy", seconds=seconds, termination=_SCIPY_STATUS.get(int(sol.status), str(sol.status)))
    progress(1.0, f"Reprojection error {out.reprojection_before:.3f} -> {out.reprojection_after:.3f} px "
                  f"(scipy, {out.iterations} evaluations, {out.termination})")
    return out


def _adjust_native(pb: Problem, max_iterations: int, progress: Progress, loss: str, loss_scale: float,
                   held: int) -> Result:
    """The Levenberg-Marquardt of the core (``ppg_core.bundle_adjust``), reported once per iteration."""
    fn = _native_function("bundle_adjust")
    model = _Model(pb)
    x0, r0, rep0 = _start(model, progress, "native", held)
    options = {"max_iterations": int(max_iterations), "loss": loss, "loss_scale": float(loss_scale),
               "linear_solver": "auto", "function_tolerance": FUNCTION_TOLERANCE}
    span = max(int(max_iterations), 1)

    def report(iteration: int, cost: float) -> None:  # raises on cancel, as every step's progress does
        progress(0.1 + 0.8 * min(iteration / span, 1.0), f"Bundle adjustment: iteration {iteration}, cost {cost:.6g}")

    problem = _to_native(pb)
    t0 = time.perf_counter()
    out = fn(problem, options, report)
    seconds = time.perf_counter() - t0
    rot, cen, params, pts, mks, rs, bias = _from_native(out)
    # the residuals at the end as the reference computes them (the velocities and biases set in its vector)
    end = _Model(_problem_at(pb, rot, cen, params, pts, mks))
    x1 = end.start()
    if rs is not None:
        x1[end.lay.rs_col[:, None] + np.arange(6)] = rs
    for flight, col in end.lay.bias_col.items():
        x1[col:col + 3] = bias[flight]
    termination = str(out["termination"])
    res = Result(rot, cen, params, pts, mks, rs, bias, _rms(r0), _rms(end.residuals(x1)), rep0,
                 _reprojection_rms(end, x1), int(out["iterations"]),
                 termination not in ("max_iterations", "no_progress"), cost=float(out["cost"]),
                 gradient_max=float(out["gradient_max"]), solver="native", seconds=seconds,
                 linear_solver=str(out["linear_solver"]), termination=termination)
    tolerance = FUNCTION_TOLERANCE if loss == "none" else max(FUNCTION_TOLERANCE, ROBUST_FUNCTION_TOLERANCE)
    progress(1.0, f"Reprojection error {res.reprojection_before:.3f} -> {res.reprojection_after:.3f} px (native, "
                  f"{res.linear_solver}, {res.iterations} iterations, {termination}, function tolerance {tolerance:g})")
    return res


def _to_native(pb: Problem) -> dict:
    """``pb`` as the dict ``ppg_core.bundle_adjust`` and ``bundle_evaluate`` read: the arrays in the dtypes
    the bindings extract (float64, int64, bool), the lists of tuples as arrays, ``fixed_images`` as a flag
    per photo, ``rs_sigma`` and ``rs_sigma_angular`` one value per photo."""
    n = len(pb.centres)
    free = pb.free_intrinsics if pb.free_intrinsics is not None else [list(range(len(c[1]))) for c in pb.cameras]

    def f64(a, shape):
        return np.ascontiguousarray(np.asarray(a, np.float64)).reshape(shape)

    def i64(a):
        return np.ascontiguousarray(np.asarray(a, np.int64)).reshape(-1)

    def triples(values):
        return f64([np.broadcast_to(np.asarray(v, np.float64), (3,)) for v in values], (-1, 3))

    return {
        "rotations": f64(pb.rotations, (n, 3, 3)), "centres": f64(pb.centres, (n, 3)),
        "image_camera": i64(pb.image_camera),
        "camera_models": [str(c[0]) for c in pb.cameras],
        "camera_params": [[float(v) for v in c[1]] for c in pb.cameras],
        "camera_sizes": f64([[c[2], c[3]] for c in pb.cameras], (-1, 2)),
        "free_intrinsics": [[int(k) for k in f] for f in free],
        "points": f64(pb.points, (-1, 3)), "obs_image": i64(pb.obs_image), "obs_point": i64(pb.obs_point),
        "obs_xy": f64(pb.obs_xy, (-1, 2)),
        "markers": f64(pb.markers, (-1, 3)), "mobs_image": i64(pb.mobs_image), "mobs_marker": i64(pb.mobs_marker),
        "mobs_xy": f64(pb.mobs_xy, (-1, 2)),
        "gcp_marker": i64([m for m, _, _ in pb.gcps]), "gcp_target": triples(t for _, t, _ in pb.gcps),
        "gcp_sigma": triples(s for _, _, s in pb.gcps),
        "bar_a": i64([a for a, *_ in pb.bars]), "bar_b": i64([b for _, b, *_ in pb.bars]),
        "bar_distance": f64([d for _, _, d, _ in pb.bars], (-1,)), "bar_sigma": f64([s for *_, s in pb.bars], (-1,)),
        "gnss_image": i64([i for i, *_ in pb.gnss]), "gnss_target": triples(t for _, t, *_ in pb.gnss),
        "gnss_sigma": triples(s for _, _, s, _ in pb.gnss), "gnss_flight": i64([f for *_, f in pb.gnss]),
        "fixed_images": np.array([i in pb.fixed_images for i in range(n)], bool),
        "rolling_shutter": bool(pb.rolling_shutter), "gnss_bias": bool(pb.gnss_bias),
        "pixel_sigma": float(pb.pixel_sigma), "marker_sigma": float(pb.marker_sigma),
        "rs_sigma": f64(np.broadcast_to(np.asarray(pb.rs_sigma, np.float64), (n,)), (n,)),
        "rs_sigma_angular": f64(np.broadcast_to(np.asarray(pb.rs_sigma_angular, np.float64), (n,)), (n,)),
    }


def _from_native(out: dict) -> tuple:
    """The block of a dict ``ppg_core.bundle_adjust`` returned, as :meth:`_Model.unpack` orders it."""
    rs = out["rolling_shutter"]
    return (np.asarray(out["rotations"], float), np.asarray(out["centres"], float),
            [np.asarray(p, float) for p in out["camera_params"]], np.asarray(out["points"], float).reshape(-1, 3),
            np.asarray(out["markers"], float).reshape(-1, 3), None if rs is None else np.asarray(rs, float),
            {int(f): np.asarray(b, float) for f, b in out["bias"].items()})


def adjust(pb: Problem, max_iterations: int = 100, progress: Progress | None = None, solver: str = "auto",
           loss: str = "none", loss_scale: float = 1.0) -> Result:
    """Least squares of every residual of ``pb`` (see the module docstring) by ``solver``: ``"scipy"``
    the trust-region least squares of scipy (the reference), ``"native"`` the Levenberg-Marquardt of the
    core (API 12), ``"auto"`` the native one since it passed its gate (:data:`NATIVE_BY_DEFAULT`; it was
    scipy before, and ``PPG_ENGINE=native`` still asks for the native one by name). Where the native
    solver cannot run (:func:`native_ready`) scipy adjusts the block, ``Result.fallback`` says why and the
    log has a line (a warning when the native solver was asked for by name): never an error. ``loss`` ``"huber"`` or
    ``"cauchy"`` with ``loss_scale`` (in the units of the residuals: pixels over ``pixel_sigma``) weighs the
    tie points robustly; scipy applies a loss to every residual, so asked for by name it takes a robust
    loss on a problem of tie points alone (ValueError otherwise), and on the fallback from the native
    solver a block with references is adjusted with the squared loss, ``fallback`` saying so. Tie points
    with fewer than two observations in front of a camera cannot be adjusted: they are left out of the
    adjustment and stay where they start (the RMS figures of the result are of the rest)."""
    if solver not in SOLVERS:
        raise ValueError(f"solver {solver!r}: auto, native or scipy")
    if loss not in LOSSES:
        raise ValueError(f"loss {loss!r}: none, huber or cauchy")
    if not loss_scale > 0:
        raise ValueError(f"loss_scale must be positive, not {loss_scale}")
    progress = as_progress(progress)
    chosen, loss, fallback = _choose(solver, pb, loss)
    keep = _adjustable(pb)
    held = int(len(keep) - keep.sum())
    solved = pb if held == 0 else _without_held(pb, keep)
    run = _adjust_native if chosen == "native" else _adjust_scipy
    res = run(solved, max_iterations, progress, loss, loss_scale, held)
    res.fallback = fallback
    if held:
        points = np.array(pb.points, float)
        points[keep] = res.points
        res.points = points
    return res


def evaluate(pb: Problem, loss: str = "none", loss_scale: float = 1.0) -> Evaluation:
    """``pb`` evaluated at the parameters it holds by the native engine (API 12; RuntimeError without
    it): the cost, the residual vector of the reference (:meth:`_Model.residuals`), the gradient of the
    cost in the columns of :class:`_Layout` and the Jacobian of the residuals on the structure of
    :meth:`_Model.sparsity`; ``loss`` with ``loss_scale`` on the tie points, as :func:`adjust` takes them.
    The problem is evaluated as given: the tie points :func:`adjust` leaves out (seen by fewer than two
    photos) count here, so on a problem with such points the cost is not that of a :class:`Result`."""
    from scipy.sparse import csr_matrix

    if loss not in LOSSES:
        raise ValueError(f"loss {loss!r}: none, huber or cauchy")
    fn = _native_function("bundle_evaluate")
    residuals, gradient, rows, cols, values, cost = fn(_to_native(pb), loss, float(loss_scale))
    jacobian = csr_matrix((values, (rows, cols)), shape=(len(residuals), _Layout(pb).size))
    return Evaluation(float(cost), np.asarray(residuals, float), np.asarray(gradient, float), jacobian)


def variances_ready(pb: Problem) -> str | None:
    """``None`` when the native core can give the variances of the free intrinsics of ``pb``
    (:func:`intrinsics_variances`), else why not: the core switched off, missing or older than API 17, or a camera
    model that is not among the six of its projection kernels."""
    if not _native.enabled():
        return "the native core is switched off by PPG_NATIVE=0"
    if not _native.has("intrinsics_variances") or (_native.api() or 0) < _native.INTRINSICS_API:
        return INTRINSICS_MISSING
    for model, *_ in pb.cameras:
        if not _native.supports(model):
            return f"camera model {model} is not in the native core"
    return None


def intrinsics_variances(pb: Problem, loss: str = "none", loss_scale: float = 1.0,
                         reference_flight: int | None = None) -> list[np.ndarray]:
    """The variance of each free intrinsic of each camera of ``pb`` at the parameters it holds, by the native
    engine (API 17; RuntimeError where it cannot run, :func:`variances_ready`): per camera, one value per entry of
    its ``free_intrinsics``, in their order - the diagonal of the inverse of the normal matrix at their columns,
    taken from the system reduced to the cameras; ``loss`` with ``loss_scale`` on the tie points, as
    :func:`adjust` takes them. The accuracies of ``pb`` are taken as they are: a variance factor goes into them
    first (:func:`focal_sds` does). ``reference_flight``: with ``gnss_bias``, the flight whose bias is held at
    zero - without control points the biases of all the flights and a shift of the block trade off. A normal
    matrix that cannot be inverted (a gauge left free, an intrinsic the observations do not hold) is a
    RuntimeError: nothing is regularised."""
    if loss not in LOSSES:
        raise ValueError(f"loss {loss!r}: none, huber or cauchy")
    reason = variances_ready(pb)
    if reason is not None:
        raise RuntimeError(reason)
    fn = _native.kernel("bundle_intrinsics_variances")
    flight = None if reference_flight is None else int(reference_flight)
    return [np.asarray(v, float) for v in fn(_to_native(pb), loss, float(loss_scale), flight)]


def _free_intrinsics(pb: Problem) -> list:
    """The free intrinsics of each camera of ``pb`` (every parameter when the problem names none)."""
    return pb.free_intrinsics if pb.free_intrinsics is not None else [list(range(len(c[1]))) for c in pb.cameras]


def focal_free(pb: Problem) -> list[bool]:
    """Per camera of ``pb``, whether an adjustment of it refines its focal length (fx or fy of a model with two)."""
    free = _free_intrinsics(pb)
    return [any(k in _focal_indices(model) for k in free[c]) for c, (model, *_rest) in enumerate(pb.cameras)]


def runaways(model: str, start, end, free) -> dict:
    """The groups of a camera of ``model`` that an adjustment moved too far, from its parameters ``start`` to
    ``end``, among those it refined (``free``, the indices of its free intrinsics): ``"focal"`` with the largest
    share by which a focal length moved (fx and fy of a model with two) when that is above
    :data:`FOCAL_MOVE_HOLD`, ``"radial"`` with the change of the first radial term when that is above
    :data:`K1_MOVE_HOLD`; infinite for a value that is not a number."""
    groups, out = intrinsic_groups(model), {}
    start, end = np.asarray(start, float), np.asarray(end, float)
    focal = [k for k in groups["focal"] if k in free]
    if focal:
        share = np.abs(end[focal] / start[focal] - 1.0)
        share = float(share.max()) if np.all(np.isfinite(share)) else float("inf")
        if share > FOCAL_MOVE_HOLD:
            out["focal"] = share
    if groups["radial"] and groups["radial"][0] in free:
        change = abs(float(end[groups["radial"][0]] - start[groups["radial"][0]]))
        change = change if np.isfinite(change) else float("inf")
        if change > K1_MOVE_HOLD:
            out["radial"] = change
    return out


def tie_variance_factor(pb: Problem) -> float:
    """The variance factor of the tie points of ``pb`` at the parameters it holds: ``v^T P v / r`` of their own
    residuals (in units of ``pixel_sigma``), ``r`` their rows less the unknowns they determine - six per free
    photo, the free intrinsics, three per point. Observations behind their camera are left out; 1 when there is
    nothing to estimate it from (no redundancy, no residual). The square of what the tie points are worth against
    the accuracy the problem states for them: the factor the covariance of an adjustment is scaled by."""
    model = _Model(pb)
    rot, cen, params, pts, _mks, rs, _bias = model.unpack(model.start())
    uv = model._project(rot, cen, params, rs, pb.obs_image, pts[pb.obs_point], pb.obs_xy)
    r = (uv - pb.obs_xy) / pb.pixel_sigma
    seen = np.isfinite(r).all(axis=1)
    lay = model.lay
    unknowns = 6 * len(lay.free_images) + sum(len(f) for f in lay.free_intrinsics) + 3 * len(pb.points)
    redundancy = 2 * int(seen.sum()) - unknowns
    total = float(np.sum(r[seen] ** 2))
    return total / redundancy if redundancy > 0 and total > 0 else 1.0


def _held_markers(pb: Problem) -> Problem:
    """``pb`` without the markers that nothing holds - measured in fewer than two photos and not a control point
    (the end of a scale bar seen once) -, their observations and the bars that end on them: a marker like that
    has no position of its own."""
    if not len(pb.markers):
        return pb
    keep = np.bincount(np.asarray(pb.mobs_marker, int), minlength=len(pb.markers)) >= 2
    for m, _target, _sigma in pb.gcps:
        keep[m] = True
    if keep.all():
        return pb
    index = np.cumsum(keep) - 1
    sel = keep[np.asarray(pb.mobs_marker, int)]
    return replace(pb, markers=pb.markers[keep], mobs_image=pb.mobs_image[sel],
                   mobs_marker=index[np.asarray(pb.mobs_marker, int)[sel]], mobs_xy=pb.mobs_xy[sel],
                   gcps=[(int(index[m]), target, sigma) for m, target, sigma in pb.gcps],
                   bars=[(int(index[a]), int(index[b]), length, sigma) for a, b, length, sigma in pb.bars
                         if keep[a] and keep[b]])


def _with_gauge(pb: Problem) -> tuple[Problem, int | None]:
    """``pb`` as the covariance of its adjustment is taken (:func:`focal_sds`): the same block, with nothing in it
    that the observations do not hold - the normal matrix must invert - and a gauge that adds nothing the block
    does not have. The tie points that cannot be adjusted are left out (:func:`_adjustable`), as are the markers
    nothing holds (:func:`_held_markers`); a photo with fewer than three observations has no pose of its own and
    is held as it is. What places the block is counted by what the photos see: three control points whose markers
    two photos or more measure, or three positions of photos that are not held (:attr:`Problem.absolute` counts
    what is stated). A block that nothing places has its first photo held, its place and its orientation, and -
    without a scale bar between measured markers - a position of the photo farthest from it whose sigma is
    :data:`GAUGE_SIGMA_EXTENTS` times their distance, its size: the two photos :func:`problem_from_chunk` holds
    for the solvers are more than a gauge, and a sd taken with them is too small. Positions with a bias per flight
    and no measured control point: the bias of one flight is held at zero (the frame of the positions; the biases
    of all the flights and a shift of the block would trade off). Returns the problem and that flight (None:
    none)."""
    keep = _adjustable(pb)
    out = replace(_held_markers(pb if keep.all() else _without_held(pb, keep)), gnss=list(pb.gnss))
    n = len(out.centres)
    seen = np.bincount(out.obs_image, minlength=n) + np.bincount(np.asarray(out.mobs_image, int), minlength=n)
    loose = {int(i) for i in np.flatnonzero(seen < 3)}
    measured = np.bincount(np.asarray(out.mobs_marker, int), minlength=len(out.markers)) >= 2
    placed = (sum(1 for m, _target, _sigma in out.gcps if measured[m]) >= 3
              or sum(1 for g in out.gnss if g[0] not in loose) >= 3)
    held = [i for i in range(n) if i not in loose]
    flight = None
    if placed or len(held) < 2:
        out.fixed_images = set(out.fixed_images) | loose
    else:
        first = held[0]
        distance = np.linalg.norm(out.centres[held] - out.centres[first], axis=1)
        far = held[int(np.argmax(distance))]
        out.fixed_images = loose | {first}
        if not any(measured[a] and measured[b] for a, b, *_rest in out.bars):
            flight = min((g[3] for g in out.gnss), default=1) - 1  # a flight of its own, without a bias
            sigma = GAUGE_SIGMA_EXTENTS * max(float(distance.max()), 1e-3)
            out.gnss.append((far, np.array(out.centres[far], float), np.full(3, sigma), flight))
    if out.gnss_bias and not any(measured[m] for m, _target, _sigma in out.gcps) and flight is None and out.gnss:
        flight = min(g[3] for g in out.gnss)
    return out, (flight if out.gnss_bias else None)


def intrinsic_sds(pb: Problem) -> tuple[list, float | None]:
    """How well an adjustment of ``pb`` determines the intrinsics it is judged by, per camera, at the parameters
    of the problem (the rule of the rounds of the native mapper, group by group): ``{"focal": ..,
    "principal_point": ..}`` - the relative marginal sd of its focal length (the square root of the largest
    variance of its focal columns, fx and fy of a model with two, over its focal length) and the marginal sd of its
    principal point over the side of the photo (cx over the width, cy over the height, the larger); None for a
    group the problem does not free. Marginal: every intrinsic the problem frees is free with them (a conditional
    sd, with the terms that trade with them held, is always smaller). The covariance is scaled by the variance
    factor of the tie points (:func:`tie_variance_factor`: it goes into ``pixel_sigma``; the references keep the
    accuracy they state) and taken with the gauge of :func:`_with_gauge`. Returns the sds and the variance factor
    (None when neither group is free in any camera: the core is not asked). RuntimeError where the core cannot
    give the variances (:func:`variances_ready`) or the normal matrix cannot be inverted (an intrinsic the block
    does not hold at all: photos looking straight down on flat ground)."""
    free = _free_intrinsics(pb)
    judged = []
    for c, (model, *_rest) in enumerate(pb.cameras):
        groups = intrinsic_groups(model)
        judged.append({name: [(j, k) for j, k in enumerate(free[c]) if k in groups[name]]
                       for name in ("focal", "principal_point")})
    if not any(columns for camera in judged for columns in camera.values()):
        return [{"focal": None, "principal_point": None} for _camera in pb.cameras], None
    probe, flight = _with_gauge(pb)
    factor = tie_variance_factor(probe)
    probe.pixel_sigma = float(probe.pixel_sigma) * float(np.sqrt(factor))
    variances = intrinsics_variances(probe, reference_flight=flight)
    sds = []
    for c, camera in enumerate(judged):
        model, params, width, height = pb.cameras[c]
        sd = {"focal": None, "principal_point": None}
        if camera["focal"]:
            worst = max(float(variances[c][j]) for j, _k in camera["focal"])
            sd["focal"] = float(np.sqrt(max(worst, 0.0))) / abs(float(params[0]))
        if camera["principal_point"]:
            cx = intrinsic_groups(model)["principal_point"][0]
            sd["principal_point"] = max(float(np.sqrt(max(float(variances[c][j]), 0.0)))
                                        / float(width if k == cx else height) for j, k in camera["principal_point"])
        sds.append(sd)
    return sds, factor


def focal_sds(pb: Problem) -> tuple[list, float | None]:
    """The relative marginal sd of the focal length of each camera of ``pb`` (:func:`intrinsic_sds`; None for a
    camera whose focal length is not free) and the variance factor of the tie points (None when the core was not
    asked)."""
    sds, factor = intrinsic_sds(pb)
    return [sd["focal"] for sd in sds], factor


def _problem_at(pb: Problem, rotations, centres, camera_params, points, markers) -> Problem:
    cameras = [(model, [float(v) for v in params], width, height)
               for (model, _old, width, height), params in zip(pb.cameras, camera_params)]
    return replace(pb, rotations=np.array(rotations, float), centres=np.array(centres, float), cameras=cameras,
                   points=np.array(points, float).reshape(-1, 3), markers=np.array(markers, float).reshape(-1, 3))


def problem_at(pb: Problem, res: Result) -> Problem:
    """``pb`` started where ``res`` ended: its rotations, centres, intrinsics, points and markers replaced
    by those of ``res`` and nothing else (the other fields are shared with ``pb``; the rolling-shutter
    velocities and the GNSS biases of a result have no place in a problem, which starts them at zero; the
    tie points :func:`adjust` left out are kept, where they started)."""
    return _problem_at(pb, res.rotations, res.centres, res.camera_params, res.points, res.markers)


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


# the COLMAP camera models with one focal length (f, cx, cy, ...): the others have two (fx, fy, cx, cy, ...)
_SINGLE_FOCAL = ("SIMPLE_PINHOLE", "SIMPLE_RADIAL", "RADIAL", "SIMPLE_RADIAL_FISHEYE", "RADIAL_FISHEYE")


# the radial and the tangential terms of the camera models of the native core, by their indices in COLMAP's order
_RADIAL = {"SIMPLE_RADIAL": (3,), "RADIAL": (3, 4), "OPENCV": (4, 5), "FULL_OPENCV": (4, 5, 8, 9, 10, 11)}
_TANGENTIAL = {"OPENCV": (6, 7), "FULL_OPENCV": (6, 7)}


def intrinsic_groups(model_name: str) -> dict:
    """The parameters of a COLMAP camera model by group, by their indices: ``focal`` (one or two),
    ``principal_point``, ``radial`` (the first of them k1) and ``tangential`` - the last two for the six models
    of the native core (empty for another model): what the camera optimisation holds or refines together."""
    return {"focal": _focal_indices(model_name), "principal_point": _principal_point(model_name),
            "radial": _RADIAL.get(model_name, ()), "tangential": _TANGENTIAL.get(model_name, ())}


def _principal_point(model_name: str) -> tuple[int, int]:
    """The indices of the principal point among the parameters of a COLMAP camera model."""
    return (1, 2) if model_name in _SINGLE_FOCAL else (2, 3)


def _focal_indices(model_name: str) -> tuple[int, ...]:
    """The indices of the focal length among the parameters of a COLMAP camera model: one (f) or two (fx, fy)."""
    return (0,) if model_name in _SINGLE_FOCAL else (0, 1)


def hold_focal_length(pb: Problem) -> None:
    """The focal length of every camera of ``pb`` taken out of its free parameters, in place (what ``hold_focal``
    of :func:`problem_from_chunk` does as the problem is built); the rest of the calibration stays as free as it
    was. The camera optimisation does this to a block whose references do not place it (:attr:`Problem.absolute`
    False): photos of flat ground taken from one height cannot tell the focal length from the depth of the scene,
    the two scale together at the same cost, and a Newton solver walks that valley."""
    if pb.free_intrinsics is None:  # the solvers' default: every parameter free
        pb.free_intrinsics = [list(range(len(c[1]))) for c in pb.cameras]
    for n, (model_name, *_rest) in enumerate(pb.cameras):
        focal = _focal_indices(model_name)
        pb.free_intrinsics[n] = [k for k in pb.free_intrinsics[n] if k not in focal]


def problem_from_reconstruction(rec) -> tuple[Problem, dict]:
    """The plain adjustment of a reconstruction (its internal frame): the tie points of its posed images and
    nothing else - no markers, no references -, every camera with its focal length and distortion free and
    its principal point held, the first posed image and the one farthest from it held as the place and the
    size of the block. It is the tie part of :func:`problem_from_chunk`, which starts from it, and the
    problem ``ppg bench adjust`` gives COLMAP, the native solver and scipy alike. Returns the problem and
    the ids :func:`write_back` needs (``markers`` empty). A reconstruction with rigs (faces of 360-degree
    photos) is refused (ValueError)."""
    if any(len(list(f.image_ids)) > 1 for f in rec.frames.values()):
        raise ValueError("the reconstruction has rigs (faces of 360-degree photos): use COLMAP's adjustment")
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
    image_camera = np.array([cam_index[rec.images[i].camera_id] for i in image_ids])
    pb = Problem(np.array(rotations), np.array(centres), image_camera, cameras, points, np.array(oi, int),
                 np.array(op, int), np.array(oxy, float).reshape(-1, 2))
    pb.free_intrinsics = [[k for k in range(len(params)) if k not in _principal_point(model)]
                          for model, params, _width, _height in cameras]
    if len(image_ids) >= 2:  # the block needs a place and a size: the first photo and the farthest hold them
        far = int(np.argmax(np.linalg.norm(pb.centres - pb.centres[0], axis=1)))
        pb.fixed_images = {0, far}
    return pb, {"images": image_ids, "cameras": cam_ids, "points": point_ids, "markers": []}


def problem_from_chunk(chunk, rec, use_markers: bool = True, gnss: str = "off", rolling_shutter: bool = False,
                       fit_principal_point: bool = False, hold_focal: bool = False) -> tuple[Problem, dict]:
    """The adjustment of ``chunk`` (its reconstruction ``rec``, internal frame): tie points
    (:func:`problem_from_reconstruction`, whose gauge and free calibration give way to the chunk's own: the
    photos and sensors held, the focal length and the principal point as asked, a photo held only when the
    block has no references to place it); with ``use_markers`` the measured projections of the markers, the
    control points (enabled reference, not check points) with their accuracy and the enabled control scale
    bars; with ``gnss`` "plain" or "bias" the camera references (with a bias per flight); rolling shutter.
    ``hold_focal``: the focal lengths are not refined (photos looking down from one height cannot
    tell them from the distance to the ground). Returns the problem and the ids that
    :func:`write_back` needs."""
    from ..markers import gcp
    from .sfm import _rec_fixed_cameras

    pb, ids = problem_from_reconstruction(rec)
    s = float(chunk.scale) if chunk.transform is not None else 1.0
    names = chunk.meta["sfm"]["names"]
    key_of = {v: int(k) for k, v in names.items()}
    image_ids, cam_ids, cameras = ids["images"], ids["cameras"], pb.cameras
    index = {iid: n for n, iid in enumerate(image_ids)}
    pb.rolling_shutter, pb.gnss_bias = rolling_shutter, gnss == "bias"
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
    # what stays as it is (in place of the gauge and the free calibration of the plain problem)
    fixed = _rec_fixed_cameras(chunk, rec)  # the COLMAP camera ids whose calibration is held
    pb.fixed_images = set()  # no pose is held here: the gauge is decided below, from the references of the block
    pb.free_intrinsics = []
    for c in cam_ids:
        model_name = rec.cameras[c].model.name
        n = len(rec.cameras[c].params)
        held = {chunk.sensors[chunk.camera(key_of[rec.images[i].name]).sensor_key].fixed
                for i in image_ids if rec.images[i].camera_id == c and rec.images[i].name in key_of}
        if True in held or c in fixed:
            pb.free_intrinsics.append([])
            continue
        principal = _principal_point(model_name)
        focal = _focal_indices(model_name) if hold_focal else ()
        pb.free_intrinsics.append([k for k in range(n) if (fit_principal_point or k not in principal) and k not in focal])
    marker_keys = []
    if use_markers:
        controls = {m.key: m for m in gcp.control_markers(chunk)}
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
        cams = {iid: chunk.camera(key_of[rec.images[iid].name]) for iid in image_ids if rec.images[iid].name in key_of}
        # each position in the coordinate system of the chunk (one read in longitude and latitude is converted; one
        # that cannot be, on a chunk still in local coordinates, is left out)
        positions, _crs = gcp.camera_references(chunk, [c for c in cams.values() if c.reference.enabled], chunk.crs)
        for iid, cam in cams.items():
            if cam.key in positions:
                target = chunk.to_internal(np.asarray([positions[cam.key]], float))[0]
                sigma = np.maximum(np.asarray(cam.reference.accuracy, float), 1e-6) / s
                pb.gnss.append((index[iid], target, sigma, groups.get(cam.key, 0)))
    if not pb.absolute and not pb.fixed_images and len(image_ids) >= 2:  # nothing places the block
        pb.fixed_images = {0}  # the block needs a place: the first photo holds it
        if not pb.bars:  # and a size: the second one too
            far = int(np.argmax(np.linalg.norm(pb.centres - pb.centres[0], axis=1)))
            pb.fixed_images.add(far)
    ids["markers"] = marker_keys
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
