"""Colour correction on a survey taken with automatic exposure: the figures it reports, the tone
it leaves to the mosaic, the photos with too few observations and the falloff of a lens that the
observations do not hold."""
from __future__ import annotations

import shutil

import cv2
import numpy as np
import pytest
import rasterio

from pyarchinit_photogrammetry.engine import colour
from pyarchinit_photogrammetry.engine.visibility import project_valid

NOISE = 0.03


def _strip(n_cams: int = 40, n_pts: int = 6000, reach: float = 3.0, seed: int = 0):
    """Observations of points along a strip of photos (photo ``i`` above ``x = i``): every point seen by the
    photos within ``reach``, at a radius that grows with the distance from the photo."""
    rng = np.random.default_rng(seed)
    x = rng.uniform(0, n_cams - 1, n_pts)
    level = rng.uniform(3.5, 5.0, (n_pts, 3))
    point, photo = np.nonzero(np.abs(x[:, None] - np.arange(n_cams)[None]) <= reach)
    radius = np.clip(np.abs(x[point] - photo) / reach + rng.uniform(-0.05, 0.05, len(point)), 0, 1)
    centres = np.column_stack([np.arange(n_cams), np.zeros(n_cams), np.full(n_cams, 10.0)]).astype(float)
    return {"point": point, "photo": photo, "sensor": np.zeros(len(point), int), "radius": radius, "level": level,
            "points": n_pts, "cams": list(range(n_cams)), "sensors": [0], "centres": centres, "rng": rng}


def _observe(obs: dict, gains: np.ndarray, falloff=(0.0, 0.0, 0.0)) -> dict:
    """The log colours of the observations for the true log ``gains`` (photo x 3) and falloff."""
    f = colour._falloff(np.asarray(falloff, float), obs["radius"])
    obs["log"] = (obs["level"][obs["point"]] + gains[obs["photo"]] + f[:, None]
                  + obs["rng"].normal(0, NOISE, (len(obs["point"]), 3)))
    return obs


def _without(obs: dict, drop: np.ndarray) -> dict:
    keep = ~drop
    for k in ("point", "photo", "sensor", "radius", "log"):
        obs[k] = obs[k][keep]
    return obs


def test_reported_scatter_is_the_residual_of_the_fit_with_photos_that_saw_nothing():
    """Photos without observations took the constant of the gauge: the gains of the others moved by it and the
    reported scatter after the correction was that constant (4.6 for 0.06 on a real flight)."""
    obs = _strip()
    true = obs["rng"].normal(0, 0.2, (40, 1)) + obs["rng"].normal(0, 0.03, (40, 3))
    obs = _observe(obs, true)
    blind = np.isin(obs["photo"], [0, 1, 20, 21, 39])  # five photos that saw nothing
    few = np.isin(obs["photo"], [10]) & (obs["rng"].random(len(blind)) > 0.02)  # and one a handful of points
    obs = _without(obs, blind | few)
    sol = colour.solve(obs, vignetting=False)
    assert 0.15 < sol["rms_before"] < 0.3
    assert sol["rms_after"] == pytest.approx(NOISE, rel=0.15)  # the noise of the observations, nothing else
    measured = sol["measured"]
    assert not measured[[0, 1, 10, 20, 21, 39]].any() and measured.sum() == 34
    diff = np.log(sol["gains"])[measured] - true[measured]
    assert np.abs(diff - diff.mean(axis=0)).max() < 0.01  # the true gains, up to the constant of the gauge
    # outliers are found: before, the residuals carried the constant and nothing was ever left out
    obs["log"][::50] += 1.0
    again = colour.solve(obs, vignetting=False)
    assert again["used"] < 0.985 * len(obs["point"]) and again["rms_after"] == pytest.approx(NOISE, rel=0.15)


def test_mosaic_keeps_its_tone_when_the_photos_it_prefers_are_the_darker_ones():
    """The gauge: no change, on average, where the mosaic takes its colours - not over the photos. A flight
    whose nadir photos (the ones the mosaic prefers) are darker than the oblique ones came out 20-35 % brighter."""
    obs = _strip()
    nadir = np.arange(40) % 4 == 0
    true = np.where(nadir[:, None], -0.3, 0.1) + obs["rng"].normal(0, 0.02, (40, 3))
    true[:, 2] -= np.where(nadir, 0.05, 0.0)  # and redder (BGR)
    obs = _observe(obs, true)
    obs["score"] = np.where(nadir[obs["photo"]], 10.0, 1.0) - obs["radius"]
    sol = colour.solve(obs, vignetting=False)
    g = np.log(sol["gains"])
    assert np.abs(g[nadir].mean(axis=0)).max() < 0.02  # the photos the mosaic takes stay as they are
    assert g[~nadir].mean() == pytest.approx(0.4 + 0.05 / 3, abs=0.02)
    assert sol["rms_after"] == pytest.approx(NOISE, rel=0.15)
    # with points spread over the surface of the mosaic among the others, only they count
    obs["mosaic"] = np.arange(obs["points"]) % 3 == 0
    obs["score"] = np.where(obs["mosaic"][obs["point"]], obs["score"], -obs["score"])  # elsewhere the oblique ones
    g = np.log(colour.solve(obs, vignetting=False)["gains"])
    assert np.abs(g[nadir].mean(axis=0)).max() < 0.02
    del obs["mosaic"]
    # without scores the photo that sees a point nearest to its centre counts
    del obs["score"]
    plain = np.log(colour.solve(obs, vignetting=False)["gains"])
    best = np.lexsort((obs["radius"], obs["point"]))
    best = best[np.r_[True, np.diff(obs["point"][best]) > 0]]
    assert np.abs(colour._middle(plain[obs["photo"][best]])).max() < 0.02
    # two groups of photos the mosaic takes as much from: between the two, not on one of them
    assert np.allclose(colour._middle(np.repeat([[-0.3], [0.1]], 50, axis=0)), -0.1)
    assert abs(colour._middle(np.repeat([[-0.3], [0.1]], [70, 30], axis=0))[0] + 0.3) < 0.05  # most of them


def test_photos_with_too_few_observations_take_the_gain_of_their_neighbours():
    """Left at gain 1 among photos that moved, their footprints stood out in the mosaic."""
    obs = _strip()
    true = np.where(np.arange(40)[:, None] < 20, -0.35, 0.15) + obs["rng"].normal(0, 0.02, (40, 3))
    obs = _observe(obs, true)
    blind = np.isin(obs["photo"], [0, 1, 39])
    few = np.isin(obs["photo"], [8, 30]) & (obs["rng"].random(len(blind)) > 0.03)
    obs = _without(obs, blind | few)
    sol = colour.solve(obs, vignetting=False)
    g = np.log(sol["gains"])
    assert not sol["measured"][[0, 1, 8, 30, 39]].any()
    shift = (g - true)[sol["measured"]].mean(axis=0)
    for i in (0, 1, 8, 30, 39):  # as their neighbours, not as they were
        assert np.abs(g[i] - shift - true[i]).max() < 0.08, i
    assert abs(g[0].mean() - g[39].mean()) > 0.4
    # no positions: the middle of the photos that were measured
    del obs["centres"]
    g = np.log(colour.solve(obs, vignetting=False)["gains"])
    assert np.abs(g[0] - np.median(g[sol["measured"]], axis=0)).max() < 1e-6


def test_falloff_the_observations_do_not_hold_stays_zero():
    obs = _strip(n_cams=30, n_pts=5000)
    true = obs["rng"].normal(0, 0.1, (30, 3))
    obs = _observe(obs, true, falloff=(-0.3, 0.0, 0.0))
    sol = colour.solve(obs)
    b = sol["vignetting"][0]
    for r in (0.3, 0.6, 0.9):
        assert abs(colour._falloff(b, np.array([r]))[0] + 0.3 * r * r) < 0.02
    assert sol["vignetting_radius"][0] == pytest.approx(1.0, abs=0.05)
    # a second sensor that saw the points in a narrow ring only: nothing says how its lens falls off
    narrow = _strip(n_cams=30, n_pts=5000)
    narrow = _observe(narrow, true, falloff=(-0.3, 0.0, 0.0))
    second = narrow["photo"] >= 26
    narrow["sensor"] = second.astype(int)
    narrow["sensors"] = [0, 1]
    narrow = _without(narrow, second & ((narrow["radius"] < 0.4) | (narrow["radius"] > 0.55)))
    sol = colour.solve(narrow)
    assert np.all(sol["vignetting"][1] == 0) and np.any(sol["vignetting"][0] != 0)
    # the centre of the frames only: the falloff is not carried on to the corners
    centre = _strip(n_cams=30, n_pts=5000)
    centre = _without(_observe(centre, true, falloff=(-0.3, 0.0, 0.0)), centre["radius"] > 0.6)
    sol = colour.solve(centre)
    assert 0.5 < sol["vignetting_radius"][0] <= 0.6


def test_falloff_is_held_beyond_the_radius_it_was_measured_to(processed):
    _doc, ch, _ = processed
    cam = ch.aligned_cameras[0]
    key = str(ch.sensor(cam).key)
    img = np.full((ch.sensor(cam).height, ch.sensor(cam).width, 3), 100, np.uint8)
    wild = {"gains": {}, "vignetting": {key: [1.0, -5.05, 7.24]}, "vignetting_radius": {key: 0.6}}
    out = colour.Correction(ch, wild).correct(cam, img)
    at = float(np.exp(-colour._falloff(np.array([1.0, -5.05, 7.24]), np.array([0.6]))[0]) * 100)
    assert abs(float(out[0, 0, 0]) - at) < 2 and abs(float(out[-1, -1, 0]) - at) < 2  # not 100 / e^3.2 = 4
    old = {"gains": {}, "vignetting": {key: [-0.3, 0.0, 0.0]}}  # stored before: the whole frame
    assert abs(float(colour.Correction(ch, old).correct(cam, img)[0, 0, 0]) - 100 * np.exp(0.3)) < 3


def test_calibration_that_does_not_help_is_not_applied():
    obs = _strip()
    obs = _observe(obs, np.zeros((40, 3)))  # the photos already agree: only noise
    sol = colour.solve(obs, vignetting=False)
    assert sol["rms_after"] <= sol["rms_before"] and not sol["applied"]
    assert np.all(sol["gains"] == 1.0) and np.all(sol["vignetting"] == 0)
    obs = _observe(obs, obs["rng"].normal(0, 0.2, (40, 3)))
    assert colour.solve(obs, vignetting=False)["applied"]


# ---- on the trench: a dark middle in a bright surround, photographed with automatic exposure -------------------

RES = 0.004
DARK = 0.45  # albedo of the middle, over that of the surround
TONE = 0.05  # how far the corrected mosaic may move from the tone of the photos (the old gauge: 10-35 %)


def _read(path):
    with rasterio.open(path) as src:
        return src.read(), src.transform


@pytest.fixture(scope="module")
def autoexposed(processed, tmp_path_factory):
    """A copy of the processed trench whose middle is darker than its surround, every photo exposed (and white
    balanced) for what it frames: the photos of the middle brighter than those of the edges. With it the true
    gains, the mosaic the scene would give under one exposure, and the mosaic of the photos as they are."""
    import pyarchinit_photogrammetry as ppg

    doc, _ch, _ = processed
    d = tmp_path_factory.mktemp("autoexposure")
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")
    ch = ppg.Document.open(d / doc.path.name).chunk
    ref, tf = _read(ch.build_orthomosaic(resolution=RES, blending="disabled", colour_correction=False))
    h, w = ref.shape[1:]
    cx, cy = tf.c + w * RES / 2, tf.f - h * RES / 2

    def albedo(x, y):
        return DARK + (1 - DARK) / (1 + np.exp(-(np.hypot(x - cx, (y - cy) / 0.75) - 1.0) / 0.08))

    cloud = ch.point_cloud
    world = ch.to_world(cloud.xyz)
    gx, gy = np.meshgrid(np.linspace(cx - 4, cx + 4, 25), np.linspace(cy - 4, cy + 4, 25))
    flat = np.column_stack([gx.ravel(), gy.ravel()])
    plane = ch.to_internal(np.column_stack([flat, np.full(len(flat), float(np.median(world[:, 2])))]))
    images, means = {}, {}
    for cam in ch.aligned_cameras:  # the albedo of the ground under every pixel (the trench as a plane: it is smooth)
        img = cv2.imread(cam.path).astype(np.float32)
        px, depth, _ok = project_valid(ch, cam, plane, margin=-2000.0)
        ok = np.isfinite(px).all(axis=1) & (depth > 0)
        hom, _ = cv2.findHomography(px[ok], flat[ok] - [cx, cy])
        yy, xx = np.mgrid[0:img.shape[0], 0:img.shape[1]]
        q = cv2.perspectiveTransform(np.dstack([xx + 0.5, yy + 0.5]).reshape(-1, 1, 2), hom).reshape(*img.shape[:2], 2)
        images[cam.key] = img * albedo(q[..., 0] + cx, q[..., 1] + cy)[..., None]
        means[cam.key] = float(images[cam.key].mean())
    target = np.exp(np.mean(np.log(list(means.values()))))
    gains = {}
    for cam in ch.aligned_cameras:
        g = (target / means[cam.key]) ** 0.8  # the automatic exposure: towards the same mean
        balance = np.array([1.0, 1.0, 1.0 + 0.25 * (g - 1)])  # and a white balance that follows it (BGR)
        gains[cam.key] = g
        cv2.imwrite(cam.path, np.clip(images[cam.key] * g * balance, 0, 255).astype(np.uint8),
                    [cv2.IMWRITE_JPEG_QUALITY, 95])
    xc, yc = np.meshgrid(tf.c + (np.arange(w) + 0.5) * RES, tf.f - (np.arange(h) + 0.5) * RES)
    truth = np.concatenate([ref[:3].astype(np.float32) * albedo(xc, yc)[None], ref[3:]])
    plain = _read(ch.build_orthomosaic(resolution=RES, blending="disabled", colour_correction=False))[0]
    return ch, cloud, world - [cx, cy, 0.0], gains, truth, plain


def _common(*mosaics):
    return np.logical_and.reduce([m[3] == 255 for m in mosaics])


def _medians(img, m):
    return np.array([np.median(img[c][m]) for c in range(3)])


def _tone(img, m):
    """Median and mean of every channel."""
    return np.concatenate([_medians(img, m), [img[c][m].mean() for c in range(3)]])


def _low_error(img, truth, m) -> float:
    """Mean difference of the low frequencies from the scene under one exposure (after the overall exposure)."""
    out = []
    for c in range(3):
        a = img[c].astype(np.float32) * (np.median(truth[c][m]) / np.median(img[c][m]))
        out.append(np.abs(cv2.GaussianBlur(a, (0, 0), 6) - cv2.GaussianBlur(truth[c], (0, 0), 6))[m].mean())
    return float(np.mean(out))


def _seams(img, truth, m) -> float:
    """The steps of exposure where one photo ends and the next begins: the steepest slopes (99th percentile) of
    the log ratio between the mosaic and the scene."""
    a, t = img[:3].astype(np.float32).mean(axis=0), truth[:3].mean(axis=0)
    ratio = np.log(np.maximum(cv2.GaussianBlur(a, (0, 0), 2), 1) / np.maximum(cv2.GaussianBlur(t, (0, 0), 2), 1))
    ratio = ratio.astype(np.float32)
    slope = np.abs(cv2.Sobel(ratio, cv2.CV_32F, 1, 0, ksize=3)) + np.abs(cv2.Sobel(ratio, cv2.CV_32F, 0, 1, ksize=3))
    inner = cv2.erode(m.astype(np.uint8), np.ones((15, 15), np.uint8)) > 0
    return float(np.percentile(slope[inner], 99))


def _relative(ch, info, gains) -> np.ndarray:
    """Measured over true gain of every photo, the constant of the gauge taken away."""
    ratio = np.array([np.mean(info["gains"][str(c.key)]) / gains[c.key] for c in ch.aligned_cameras])
    return ratio / np.exp(np.mean(np.log(ratio)))


def test_mosaic_of_autoexposed_photos_keeps_its_tone_and_loses_its_seams(autoexposed):
    """The cloud covers the dark middle only, the mosaic also the bright surround: with the gauge on the photos
    (or on the points of the cloud) the surround, most of the mosaic, came out brighter and washed out."""
    ch, cloud, local, gains, truth, plain = autoexposed
    ch.point_cloud = cloud.subset((np.abs(local[:, 0]) < 1.2) & (np.abs(local[:, 1]) < 0.9))
    info = ch.calibrate_colours()
    assert info["applied"] and info["rms_after"] < 0.3 * info["rms_before"] and info["from_neighbours"] == []
    assert np.abs(_relative(ch, info, gains) - 1).max() < 0.04
    fixed = _read(ch.build_orthomosaic(resolution=RES, blending="disabled", colour_correction=True))[0]
    m = _common(fixed, plain, truth)
    # the tone of the photos is kept: median and mean of every channel within 5 % of the mosaic without correction
    assert np.abs(_tone(fixed, m) / _tone(plain, m) - 1).max() < TONE
    # no highlights washed out
    assert (fixed[:3].max(axis=0)[m] >= 250).mean() <= (plain[:3].max(axis=0)[m] >= 250).mean() + 0.001
    # and the photos no longer show: against the scene under one exposure
    assert _low_error(fixed, truth, m) < 0.2 * _low_error(plain, truth, m)
    assert _seams(fixed, truth, m) < 0.3 * _seams(plain, truth, m)


def test_photos_that_do_not_see_the_cloud_are_measured_on_the_surface(autoexposed):
    """The cloud in a corner: half of the photos had no observation, kept gain 1 among photos that moved and
    showed as rectangles. The points spread over the DEM give them their observations."""
    ch, cloud, local, gains, truth, plain = autoexposed
    ch.point_cloud = cloud.subset((np.abs(local[:, 0] + 1.5) < 0.5) & (np.abs(local[:, 1] + 1.0) < 0.4))
    cams = ch.aligned_cameras
    seen = np.bincount(colour.observations(ch, samples=20000, cams=cams)["photo"], minlength=len(cams))
    assert (seen >= colour.MIN_OBSERVATIONS).all()
    only_cloud = colour._mosaic_points
    try:  # as it was: the points of the cloud alone
        colour._mosaic_points = lambda *a, **k: None
        blind = np.bincount(colour.observations(ch, cams=cams)["photo"], minlength=len(cams)) < colour.MIN_OBSERVATIONS
    finally:
        colour._mosaic_points = only_cloud
    assert blind.sum() >= 5
    info = ch.calibrate_colours()
    assert info["applied"] and info["from_neighbours"] == []
    assert np.abs(_relative(ch, info, gains) - 1)[blind].max() < 0.04
    fixed = _read(ch.build_orthomosaic(resolution=RES, blending="disabled", colour_correction=True))[0]
    m = _common(fixed, plain, truth)
    assert np.abs(_tone(fixed, m) / _tone(plain, m) - 1).max() < TONE
    assert _low_error(fixed, truth, m) < 0.2 * _low_error(plain, truth, m)
