"""Our own bundle adjustment: a block recovered from wrong poses, the scale from bars, the bias of
the GNSS positions, the rolling shutter of a moving camera."""
from __future__ import annotations

import numpy as np
import pytest

from pyarchinit_photogrammetry.engine import bundle

pycolmap = pytest.importorskip("pycolmap")

W, H, F = 1200, 900, 1000.0


def _look_down(yaw: float) -> np.ndarray:
    """world -> camera of a camera looking down (nadir), turned by ``yaw``."""
    down = np.array([[1.0, 0, 0], [0, -1.0, 0], [0, 0, -1.0]])  # x east, y south in the image, z down
    c, s = np.cos(yaw), np.sin(yaw)
    return np.array([[c, s, 0], [-s, c, 0], [0, 0, 1.0]]) @ down


def _block(rng, n_points=600, rolling=None):
    """A drone block: 3 strips of 6 photos 30 m above rolling ground, points and markers seen in
    the photos (COLMAP pixels, exact; ``rolling``: velocity per photo, metres per frame read)."""
    rotations, centres = [], []
    for row, y in enumerate((-12.0, 0.0, 12.0)):
        for x in np.linspace(-20, 20, 6):
            rotations.append(_look_down(0.02 * row))
            centres.append([x, y, 30.0])
    rotations, centres = np.array(rotations), np.array(centres)
    xy = rng.uniform([-25, -18], [25, 18], (n_points, 2))
    points = np.column_stack([xy, 0.6 * np.sin(xy[:, 0] / 7) + 0.4 * np.cos(xy[:, 1] / 5)])
    markers = np.array([[-15.0, -10, 0.1], [15, -10, 0.2], [15, 10, -0.1], [-15, 10, 0.3], [0, 0, 0.0]])
    cam = pycolmap.Camera(model="OPENCV", width=W, height=H, params=[F, F, W / 2, H / 2, -0.02, 0.01, 0, 0])
    vel = rolling if rolling is not None else np.zeros((len(centres), 3))

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
    pb = bundle.Problem(rotations.copy(), centres.copy(), np.zeros(len(centres), int),
                        [("OPENCV", [F, F, W / 2, H / 2, -0.02, 0.01, 0, 0], W, H)], points.copy(), oi, op, oxy,
                        markers=markers.copy(), mobs_image=mi, mobs_marker=mm, mobs_xy=mxy,
                        free_intrinsics=[[0, 1, 4, 5]])
    return pb, {"rotations": rotations, "centres": centres, "points": points, "markers": markers}


def _gcps(truth, sigma=0.002, which=(0, 1, 2, 3)):
    return [(m, truth["markers"][m].copy(), np.full(3, sigma)) for m in which]


def _perturb(pb, rng, centre=0.3, angle=0.01, point=0.2):
    from scipy.spatial.transform import Rotation

    pb.centres = pb.centres + rng.normal(0, centre, pb.centres.shape)
    pb.rotations = Rotation.from_rotvec(rng.normal(0, angle, (len(pb.centres), 3))).as_matrix() @ pb.rotations
    pb.points = pb.points + rng.normal(0, point, pb.points.shape)
    pb.markers = pb.markers + rng.normal(0, point, pb.markers.shape)


def test_a_block_comes_back_to_its_control_points():
    rng = np.random.default_rng(1)
    pb, truth = _block(rng)
    pb.gcps = _gcps(truth)
    _perturb(pb, rng)
    pb.cameras[0] = ("OPENCV", [F * 1.01, F * 1.01, W / 2, H / 2, 0, 0, 0, 0], W, H)  # the calibration too
    res = bundle.adjust(pb)
    assert res.success and res.reprojection_before > 10 and res.reprojection_after < 0.01
    assert np.abs(res.centres - truth["centres"]).max() < 0.005
    assert np.abs(res.points - truth["points"]).max() < 0.01
    assert abs(res.camera_params[0][0] - F) < 0.5 and abs(res.camera_params[0][4] + 0.02) < 1e-3
    assert np.linalg.norm(res.markers[4] - truth["markers"][4]) < 0.005  # a marker that is not a control point


def test_scale_bars_give_the_size_of_a_free_block():
    rng = np.random.default_rng(2)
    pb, truth = _block(rng)
    for name in ("centres", "points", "markers"):  # the block 7% too big
        setattr(pb, name, getattr(pb, name) * 1.07)
    m = truth["markers"]
    pb.bars = [(0, 2, float(np.linalg.norm(m[0] - m[2])), 0.001), (1, 3, float(np.linalg.norm(m[1] - m[3])), 0.001)]
    pb.fixed_images = {0}  # no control point: the first photo holds the place
    res = bundle.adjust(pb)
    got = np.linalg.norm(res.markers[0] - res.markers[2])
    assert abs(got - np.linalg.norm(m[0] - m[2])) < 0.002
    d_true = np.linalg.norm(truth["points"][10] - truth["points"][500])
    assert abs(np.linalg.norm(res.points[10] - res.points[500]) - d_true) < 0.005 * d_true


def test_the_bias_of_the_gnss_positions():
    """A bias common to the photos of a flight, found with the block. The camera is calibrated:
    over flat ground a nadir block cannot tell its height from the focal length, so a free
    calibration and a free vertical bias would trade one for the other."""
    rng = np.random.default_rng(3)
    pb, truth = _block(rng)
    pb.free_intrinsics = [[]]
    bias = np.array([0.35, -0.2, 0.6])
    pb.gcps = _gcps(truth, which=(0, 1, 2))
    pb.gnss = [(i, c + bias + rng.normal(0, 0.01, 3), np.full(3, 0.02), 1) for i, c in enumerate(truth["centres"])]
    pb.gnss_bias = True
    _perturb(pb, rng, centre=0.1, point=0.05)
    res = bundle.adjust(pb)
    assert np.linalg.norm(res.bias[1] - bias) < 0.02
    assert np.abs(res.centres - truth["centres"]).max() < 0.03  # each GNSS position has 1 cm of noise
    plain = bundle.Problem(**{**pb.__dict__, "gnss_bias": False})  # without the bias the block is pulled away
    assert np.abs(bundle.adjust(plain).centres - truth["centres"]).max() > 0.05


def test_rolling_shutter_of_a_moving_drone():
    rng = np.random.default_rng(4)
    # 0.4 m along the strips while a frame is read, the strips flown one way and back
    rolling = np.array([[0.4 if (i // 6) % 2 == 0 else -0.4, 0.0, 0.0] for i in range(18)])
    pb, truth = _block(rng, rolling=rolling)
    pb.gcps = _gcps(truth)
    still = bundle.adjust(bundle.Problem(**pb.__dict__))
    pb.rolling_shutter = True
    moving = bundle.adjust(pb)
    assert still.reprojection_after > 1.0 and moving.reprojection_after < 0.05
    assert np.abs(moving.rolling_shutter[:, :3] - rolling).max() < 0.02
    assert np.abs(moving.centres - truth["centres"]).max() < 0.01


def test_own_adjustment_of_the_trench(processed, tmp_path):
    """``optimize_cameras(engine="ppg")`` on the synthetic trench: control points pulled by their
    accuracy, two of them left as check points, the metric thresholds of the pipeline kept."""
    import shutil

    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.markers import gcp

    doc, _, _ = processed
    d = tmp_path / "copia"
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")
    copy = ppg.Document.open(d / doc.path.name)
    ch = copy.chunk
    for m in ch.markers[:2]:
        m.checkpoint = True
    gcp.update_transform(ch)
    err = ch.optimize_cameras(engine="ppg")
    rep = ch.meta["sfm"]["optimize"]
    assert rep["engine"] == "ppg" and rep["control_points"] >= 4 and err < 0.5
    assert rep["after"]["control"] < 0.002 and rep["after"]["check"] < 0.003
    # the photos of the trench have a global shutter: asked for, the velocities stay close to nothing
    ch.optimize_cameras(engine="ppg", rolling_shutter=True)
    rep = ch.meta["sfm"]["optimize"]
    assert rep["rolling_shutter_m"]["median"] < 0.005
    assert rep["after"]["control"] < 0.002 and rep["after"]["check"] < 0.003
    copy.save()
    again = ppg.Document.open(d / doc.path.name).chunk  # the adjusted block is saved
    assert len(again.aligned_cameras) == len(ch.aligned_cameras)
