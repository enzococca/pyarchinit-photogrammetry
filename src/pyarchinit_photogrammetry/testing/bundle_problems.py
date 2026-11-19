"""Bundle-adjustment problems of a synthetic drone block, for the tests of the two solvers of
:mod:`..engine.bundle` and for the bench.

:func:`block` observes the block exactly with any of the six camera models of the native solver,
:func:`perturb` moves it off its solution, :func:`cases` gives one perturbed problem with noisy
observations per kind of residual (:data:`CASES`), and :func:`parameter_gap` with :func:`sigmas`
measure how far two results are apart in units of the precision of the adjustment.
"""
from __future__ import annotations

import numpy as np

from ..engine import bundle
from ..engine.bundle import Problem, Result

W, H, F = 1200, 900, 1000.0  # the photos (px) and the focal length (px) of every block
# a fixed, realistic calibration of each camera model of the native solver for 1200 x 900 photos, in COLMAP's
# order of parameters: the principal point at the centre; the OPENCV set without tangential distortion, as the
# block of the reference tests has it
MODEL_PARAMS = {
    "SIMPLE_PINHOLE": [F, W / 2, H / 2],
    "PINHOLE": [F, F, W / 2, H / 2],
    "SIMPLE_RADIAL": [F, W / 2, H / 2, -0.02],
    "RADIAL": [F, W / 2, H / 2, -0.02, 0.01],
    "OPENCV": [F, F, W / 2, H / 2, -0.02, 0.01, 0.0, 0.0],
    "FULL_OPENCV": [F, F, W / 2, H / 2, -0.02, 0.01, 0.0005, -0.0003, 0.002, -0.01, 0.004, -0.001],
}
# the parameters a block adjusts: the focal length(s) and the radial distortion; the principal point and the
# tangential distortion held, as the alignment holds them. The denominators of the rational model of FULL_OPENCV
# (k4-k6) are held too: to first order their columns are those of k1-k3, which no block tells apart
FREE_INTRINSICS = {
    "SIMPLE_PINHOLE": [0],
    "PINHOLE": [0, 1],
    "SIMPLE_RADIAL": [0, 3],
    "RADIAL": [0, 3, 4],
    "OPENCV": [0, 1, 4, 5],
    "FULL_OPENCV": [0, 1, 4, 5, 8],
}
_SINGLE_FOCAL = ("SIMPLE_PINHOLE", "SIMPLE_RADIAL", "RADIAL")
FOCAL = {m: ([0] if m in _SINGLE_FOCAL else [0, 1]) for m in MODEL_PARAMS}
PRINCIPAL_POINT = {m: ([1, 2] if m in _SINGLE_FOCAL else [2, 3]) for m in MODEL_PARAMS}

# the cases of cases(): one per kind of residual, one per camera model, and every residual at once
CASES = ("ties", "markers", "control_points", "scale_bars", "gnss_plain", "gnss_bias", "rolling_shutter",
         "fixed_photos", "fixed_intrinsics", "principal_point", "model_SIMPLE_PINHOLE", "model_PINHOLE",
         "model_SIMPLE_RADIAL", "model_RADIAL", "model_OPENCV", "model_FULL_OPENCV", "everything")
GAUGE_KINDS = ("control_points", "gnss_plain", "gnss_bias", "everything")  # the residuals place the block
TILT = 0.25  # rad, the pitch of the cameras of a case along the strips: the focal length told from the height
PIXEL_NOISE = 0.3  # px, on every observation of a case
GCP_SIGMA, BAR_SIGMA = 0.002, 0.001  # m
GNSS_SIGMA, GNSS_NOISE = 0.02, 0.01  # m
ROLLING_SPEED = 0.4  # m per frame read, along the strips, flown one way and back
BIAS = {1: np.array([0.35, -0.2, 0.6]), 2: np.array([-0.1, 0.25, -0.3])}  # of the two flights, m


def _look_down(yaw: float, pitch: float = 0.0) -> np.ndarray:
    """world -> camera of a camera looking down (nadir), turned by ``yaw`` and leaning by ``pitch`` along the
    x axis of the world (the direction of the strips)."""
    down = np.array([[1.0, 0, 0], [0, -1.0, 0], [0, 0, -1.0]])  # x east, y south in the image, z down
    c, s = np.cos(yaw), np.sin(yaw)
    turned = np.array([[c, s, 0], [-s, c, 0], [0, 0, 1.0]]) @ down
    c, s = np.cos(pitch), np.sin(pitch)
    return np.array([[c, 0, s], [0, 1.0, 0], [-s, 0, c]]) @ turned


def block(seed, model: str = "OPENCV", n_points: int = 600, rolling=None, tilt: float = 0.0) -> tuple[Problem, dict]:
    """A drone block observed exactly: 3 strips of 6 photos looking down from 30 m above rolling ground
    (the strips 12 m apart, the photos 8 m apart), ``n_points`` tie points drawn by ``seed`` (an int, or a
    generator to go on drawing from) over 50 x 36 m and five markers, each seen in the photos where it
    falls (COLMAP pixel coordinates, no noise); the camera ``model`` (one of :data:`MODEL_PARAMS`) with its
    calibration, its focal length(s) and radial distortion free (:data:`FREE_INTRINSICS`); ``rolling``: a
    velocity per photo (n, 3), metres per frame read, the rows observed at the time they are read;
    ``tilt``: the pitch of the cameras along the strips, radians, forward on the strips flown one way and
    back on the others (a gimbal pitched on a back-and-forth flight). Looking straight down (``tilt`` 0), a
    block over nearly flat ground cannot tell its focal length from the depth of the ground: the same pixels
    come from a shorter focal length and a ground nearer to every camera, whatever holds the cameras.
    Returns the problem and the truth: ``rotations``, ``centres``, ``points``, ``markers``."""
    import pycolmap

    rng = np.random.default_rng(seed)
    rotations, centres = [], []
    for row, y in enumerate((-12.0, 0.0, 12.0)):
        for x in np.linspace(-20, 20, 6):
            rotations.append(_look_down(0.02 * row, tilt if row % 2 == 0 else -tilt))
            centres.append([x, y, 30.0])
    rotations, centres = np.array(rotations), np.array(centres)
    xy = rng.uniform([-25, -18], [25, 18], (n_points, 2))
    points = np.column_stack([xy, 0.6 * np.sin(xy[:, 0] / 7) + 0.4 * np.cos(xy[:, 1] / 5)])
    markers = np.array([[-15.0, -10, 0.1], [15, -10, 0.2], [15, 10, -0.1], [-15, 10, 0.3], [0, 0, 0.0]])
    params = list(MODEL_PARAMS[model])
    cam = pycolmap.Camera(model=model, width=W, height=H, params=params)
    vel = np.asarray(rolling, float) if rolling is not None else np.zeros((len(centres), 3))

    def observe(world):
        img, idx, out = [], [], []
        for i, (r, c) in enumerate(zip(rotations, centres)):
            tau = np.zeros(len(world))
            for _ in range(4):  # the row read depends on where the point falls: a few rounds
                cam_pts = (world - c - vel[i] * tau[:, None]) @ r.T
                px = np.asarray(cam.img_from_cam(cam_pts))[:, :2]
                tau = px[:, 1] / H - 0.5
            inside = (px[:, 0] > 5) & (px[:, 0] < W - 5) & (px[:, 1] > 5) & (px[:, 1] < H - 5) & (cam_pts[:, 2] > 0)
            img += [i] * int(inside.sum())
            idx += list(np.flatnonzero(inside))
            out.append(px[inside])
        return np.array(img), np.array(idx), np.vstack(out)

    oi, op, oxy = observe(points)
    mi, mm, mxy = observe(markers)
    pb = Problem(rotations.copy(), centres.copy(), np.zeros(len(centres), int), [(model, params, W, H)],
                 points.copy(), oi, op, oxy, markers=markers.copy(), mobs_image=mi, mobs_marker=mm, mobs_xy=mxy,
                 free_intrinsics=[list(FREE_INTRINSICS[model])])
    return pb, {"rotations": rotations, "centres": centres, "points": points, "markers": markers}


def perturb(pb: Problem, rng, centre: float = 0.3, angle: float = 0.01, point: float = 0.2) -> None:
    """``pb`` moved off its solution: every centre by ``centre`` metres, every rotation by ``angle`` radians,
    every point and marker by ``point`` metres (Gaussian, drawn from ``rng``)."""
    from scipy.spatial.transform import Rotation

    pb.centres = pb.centres + rng.normal(0, centre, pb.centres.shape)
    pb.rotations = Rotation.from_rotvec(rng.normal(0, angle, (len(pb.centres), 3))).as_matrix() @ pb.rotations
    pb.points = pb.points + rng.normal(0, point, pb.points.shape)
    pb.markers = pb.markers + rng.normal(0, point, pb.markers.shape)


# ---------------------------------------------------------------- one case per kind of residual
def _ties_only(pb: Problem) -> None:
    pb.markers = np.zeros((0, 3))
    pb.mobs_image, pb.mobs_marker, pb.mobs_xy = np.zeros(0, int), np.zeros(0, int), np.zeros((0, 2))


def _gauge(pb: Problem, truth: dict, fixed: set) -> None:
    """The photos ``fixed`` held at their true pose: the place, the orientation and the size of the block."""
    pb.fixed_images = set(fixed)
    for i in fixed:
        pb.rotations[i] = truth["rotations"][i]
        pb.centres[i] = truth["centres"][i]


def _farthest(centres: np.ndarray) -> int:
    """The photo farthest from the first one (with it, the gauge of a free block)."""
    return int(np.argmax(np.linalg.norm(centres - centres[0], axis=1)))


def _start_calibration(pb: Problem, model: str) -> None:
    """The free calibration where an adjustment starts: the focal length 1 % long, the distortion at zero
    (the principal point, when free, where it is)."""
    params = list(MODEL_PARAMS[model])
    for k in pb.free_intrinsics[0]:
        if k in FOCAL[model]:
            params[k] *= 1.01
        elif k not in PRINCIPAL_POINT[model]:
            params[k] = 0.0
    pb.cameras[0] = (model, params, W, H)


def case(name: str) -> Problem:
    """The problem ``name`` of :data:`CASES`: the block of :func:`block` (the camera model of a ``model_*``
    case, else OPENCV; the cameras pitched by :data:`TILT` along the strips, so that the focal length is
    told from the height) with its tie points seen by two photos at least, every observation with
    :data:`PIXEL_NOISE` of noise, the residuals of its kind from the truth, perturbed (:func:`perturb`), its
    free calibration started off (the focal length 1 % long, the distortion at zero). A case whose residuals
    do not place the block (:data:`GAUGE_KINDS`) holds its first and farthest photos at their true pose, as
    :func:`bundle.problem_from_chunk` holds a free block; ``fixed_photos`` holds three more; ``everything``
    holds one, with its control points, bars, GNSS positions with a bias per flight and rolling shutter.
    ``gnss_plain``, ``gnss_bias`` and ``fixed_intrinsics`` keep the calibration, as the camera optimisation
    keeps the focal length of a block held by its positions."""
    if name not in CASES:
        raise ValueError(f"case {name!r}: one of {', '.join(CASES)}")
    rng = np.random.default_rng(CASES.index(name))
    model = name[len("model_"):] if name.startswith("model_") else "OPENCV"
    rolling = None
    if name in ("rolling_shutter", "everything"):
        rolling = np.array([[ROLLING_SPEED if (i // 6) % 2 == 0 else -ROLLING_SPEED, 0.0, 0.0] for i in range(18)])
    pb, truth = block(rng, model=model, rolling=rolling, tilt=TILT)
    seen = np.bincount(pb.obs_point, minlength=len(pb.points)) >= 2
    pb = bundle._without_held(pb, seen)
    pb.obs_xy = pb.obs_xy + rng.normal(0, PIXEL_NOISE, pb.obs_xy.shape)
    pb.mobs_xy = pb.mobs_xy + rng.normal(0, PIXEL_NOISE, pb.mobs_xy.shape)
    m = truth["markers"]
    flights = [1 + i // 9 for i in range(len(pb.centres))]

    def gcps(which):
        return [(k, m[k].copy(), np.full(3, GCP_SIGMA)) for k in which]

    def bars():
        return [(a, b, float(np.linalg.norm(m[a] - m[b])), BAR_SIGMA) for a, b in ((0, 2), (1, 3))]

    def gnss(bias: bool):
        return [(i, c + (BIAS[flights[i]] if bias else 0.0) + rng.normal(0, GNSS_NOISE, 3), np.full(3, GNSS_SIGMA),
                 flights[i]) for i, c in enumerate(truth["centres"])]

    if name == "control_points":
        pb.gcps = gcps((0, 1, 2, 3))
    elif name == "scale_bars":
        pb.bars = bars()
    elif name == "gnss_plain":
        _ties_only(pb)
        pb.gnss, pb.free_intrinsics = gnss(False), [[]]
    elif name == "gnss_bias":
        pb.gcps, pb.gnss, pb.gnss_bias, pb.free_intrinsics = gcps((0, 1, 2)), gnss(True), True, [[]]
    elif name == "everything":
        pb.gcps, pb.bars, pb.gnss, pb.gnss_bias, pb.rolling_shutter = gcps((0, 1, 2)), bars(), gnss(True), True, True
    elif name != "markers":  # tie points alone
        _ties_only(pb)
        if name == "rolling_shutter":
            pb.rolling_shutter = True
        elif name == "fixed_intrinsics":
            pb.free_intrinsics = [[]]
        elif name == "principal_point":
            pb.free_intrinsics = [sorted(FREE_INTRINSICS[model] + PRINCIPAL_POINT[model])]
    perturb(pb, rng)
    _start_calibration(pb, model)
    if name == "everything":
        _gauge(pb, truth, {3})
    elif name == "fixed_photos":
        _gauge(pb, truth, {0, _farthest(truth["centres"]), 4, 9, 13})
    elif name not in GAUGE_KINDS:
        _gauge(pb, truth, {0, _farthest(truth["centres"])})
    return pb


def cases() -> dict[str, Problem]:
    """Every case of :data:`CASES`, by name (:func:`case`)."""
    return {name: case(name) for name in CASES}


# ---------------------------------------------------------------- two results apart
def parameter_gap(pb: Problem, a: Result, b: Result) -> np.ndarray:
    """``a - b`` in the columns of the parameter vector of ``pb`` (:class:`bundle._Layout`): for each free
    photo the rotation vector of ``R_a R_b^T`` and the difference of the centres, then the free intrinsics
    of each camera, the points, the markers, the rolling-shutter velocities and the GNSS biases."""
    from scipy.spatial.transform import Rotation

    lay = bundle._Layout(pb)
    gap = np.zeros(lay.size)
    for i in lay.free_images:
        col = lay.image_col[i]
        gap[col:col + 3] = Rotation.from_matrix(a.rotations[i] @ b.rotations[i].T).as_rotvec()
        gap[col + 3:col + 6] = a.centres[i] - b.centres[i]
    for c, cols in enumerate(lay.camera_cols):
        free = lay.free_intrinsics[c]
        gap[cols] = np.asarray(a.camera_params[c], float)[free] - np.asarray(b.camera_params[c], float)[free]
    n, m = 3 * len(pb.points), 3 * len(pb.markers)
    gap[lay.point_col:lay.point_col + n] = (np.asarray(a.points) - np.asarray(b.points)).ravel()
    gap[lay.marker_col:lay.marker_col + m] = (np.asarray(a.markers) - np.asarray(b.markers)).ravel()
    if pb.rolling_shutter:
        for i in range(len(pb.centres)):
            col = lay.rs_col[i]
            gap[col:col + 6] = np.asarray(a.rolling_shutter[i]) - np.asarray(b.rolling_shutter[i])
    for flight, col in lay.bias_col.items():
        gap[col:col + 3] = np.asarray(a.bias[flight]) - np.asarray(b.bias[flight])
    return gap


def sigmas(pb: Problem, at: Result) -> np.ndarray:
    """The standard deviation of every parameter of ``pb`` at the result ``at``: ``sqrt(diag(inv(J^T J)))``,
    ``J`` the Jacobian of the native evaluation of the problem started at ``at`` (:func:`bundle.evaluate`),
    dense. The precision of a block that has a gauge (held photos, control points or GNSS positions): without
    one the normal matrix is singular, and the inverse says so rather than hiding it."""
    jac = bundle.evaluate(bundle.problem_at(pb, at)).jacobian
    normal = (jac.T @ jac).toarray()
    return np.sqrt(np.diag(np.linalg.inv(normal)))
