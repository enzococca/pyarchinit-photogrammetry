"""The bindings of the native matching (``ppg_core`` API 14, ``_native.native_match_kernels()``): the
features step into a feature store, the preselection on the small store, the two-stage matching of
the pairs into a match store, the stores read and written from Python, and the photos read and
reduced as COLMAP reads them. Every binding releases the GIL and reports through ``progress(done,
total, message)``, whose exception stops the step. Skipped without a core that carries them."""
from __future__ import annotations

import inspect
import os
from pathlib import Path

import numpy as np
import pytest

from pyarchinit_photogrammetry import _native
from pyarchinit_photogrammetry.core.progress import Cancelled

ppg_core = pytest.importorskip("ppg_core")
pytestmark = pytest.mark.skipif(_native.native_match_kernels() is None,
                                reason="the native core with the native matching (API 14) is not installed")

PHOTOS = 28  # the trench of ``scene_dir``: 20 nadir photos and the 8 of the tilted ring, 1200 x 900
LIMIT = 600  # quality "high": the 1200-pixel photos reduced by two (the CPU SIFT takes tens of ms each)
SMALL = 1024  # sfm.PRESELECTION_SIZE
SMALL_POINTS = 1024  # sfm.PRESELECTION_POINTS
MAX_FEATURES = 40000
OPTIONS = {"stage1_points": 8000, "trust_inliers": 40, "drop_inliers": 8, "max_ratio": 0.8, "max_distance": 0.7,
           "cross_check": True, "max_num_matches": 32768, "ransac_max_error": 4.0, "two_view_min_num_inliers": 15}


def _photos(scene_dir):
    return sorted((scene_dir / "photos").glob("IMG_*.jpg"))


def _jobs(photos, limit=LIMIT, small_limit=SMALL):
    """``(name, path, mask, limit, small_limit)`` of each photo, as ``native_features`` takes them."""
    return [(p.name, str(p), None, limit, small_limit) for p in photos]


def _camera(width=1200, height=900, prior=False):
    """A camera as ``native_preselect`` and ``native_match_pairs`` take it: the focal guessed as COLMAP guesses
    it without a prior (1.2 times the long side), no prior focal length."""
    return ("SIMPLE_PINHOLE", [1.2 * max(width, height), width / 2, height / 2], width, height, prior)


def _grey(path):
    import cv2

    from pyarchinit_photogrammetry.engine import sfm

    return cv2.transform(cv2.imread(str(path), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION), sfm._REC709_BGR)


def _monotonic(calls, total):
    """Every call reported ``total``, a done that never fell and never passed the total, the first at 0 and the
    last at the total."""
    assert calls, "no progress call"
    assert all(t == total and 0 <= d <= t and isinstance(m, str) for d, t, m in calls), calls[:5]
    done = [d for d, _, _ in calls]
    assert done == sorted(done) and done[0] == 0 and done[-1] == total, (done[:5], done[-5:])


@pytest.fixture(scope="module")
def trench_store(scene_dir, tmp_path_factory):
    """The features of the trench photos extracted on the CPU into a store, with the small store of the
    preselection beside it: ``(store, small_store, report, names, progress calls)``."""
    root = tmp_path_factory.mktemp("native_match")
    photos = _photos(scene_dir)
    calls = []
    report = ppg_core.native_features(str(root / "features"), str(root / "small"), _jobs(photos), MAX_FEATURES,
                                      SMALL_POINTS, [], True, True, 2, lambda d, t, m: calls.append((d, t, m)),
                                      min_interval=0)
    return root / "features", root / "small", report, [p.name for p in photos], calls


def test_the_kernels_are_declared_at_api_14(monkeypatch):
    """``_native`` names the native matching as a feature of API 14 (13 with the options of its second stage) and
    gives its bindings as a namespace, None when the extension is switched off."""
    assert _native.NATIVE_MATCH_API == 14 and _native.api() >= 14
    assert _native.has("native_match") and _native.supports("native_match")
    k = _native.native_match_kernels()
    assert k.features is ppg_core.native_features and k.match_pairs is ppg_core.native_match_pairs
    assert k.preselect is ppg_core.native_preselect and k.features_pixels is ppg_core.native_features_pixels
    assert k.store_read is ppg_core.native_store_read and k.store_write is ppg_core.native_store_write
    assert k.match_read is ppg_core.native_match_read and k.match_pairs_list is ppg_core.native_match_pairs_list
    assert k.feature_store is ppg_core.NativeFeatureStore and k.match_store is ppg_core.NativeMatchStore
    monkeypatch.setenv("PPG_NATIVE", "0")
    assert _native.native_match_kernels() is None


# ------------------------------------------------------------------------------- the photos as COLMAP reads them
def test_the_rescale_is_the_numpy_port(scene_dir):
    """``rescale_as_colmap`` gives the bytes of ``sfm._rescale_as_colmap`` (the same sums in single precision in
    the same order), at every reduction and when the size is the photo's own."""
    from pyarchinit_photogrammetry.engine import sfm

    g = _grey(_photos(scene_dir)[0])
    for w, h in [(600, 450), (1024, 768), (333, 250), (97, 73), (1200, 900)]:
        ours = ppg_core.rescale_as_colmap(g, w, h)
        assert ours.dtype == np.uint8 and ours.shape == (h, w)
        assert np.array_equal(ours, sfm._rescale_as_colmap(g, w, h)), (w, h)


def test_read_grey_is_colmaps_grey(scene_dir, tmp_path):
    """``read_grey`` on a PNG the test writes is ``cv2.transform(..., sfm._REC709_BGR)`` of it, every pixel within 1;
    a JPEG is decoded at the DCT scale its reduction allows, the full size reported beside it."""
    import cv2

    photo = _photos(scene_dir)[0]
    bgr = cv2.imread(str(photo), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
    png = tmp_path / "photo.png"
    cv2.imwrite(str(png), bgr)
    grey, (w, h) = ppg_core.read_grey(str(png), 0, True)
    assert (w, h) == (1200, 900) and grey.shape == (900, 1200) and grey.dtype == np.uint8
    assert np.abs(grey.astype(int) - _grey(png).astype(int)).max() <= 1
    reduced, full = ppg_core.read_grey(str(photo), 600, True)
    assert full == (1200, 900) and reduced.shape == (450, 600)
    whole, full = ppg_core.read_grey(str(photo), 600, False)
    assert full == (1200, 900) and whole.shape == (900, 1200)
    (tmp_path / "text.txt").write_bytes(b"hello")
    with pytest.raises(RuntimeError, match="not a JPEG, PNG or TIFF"):
        ppg_core.read_grey(str(tmp_path / "text.txt"), 0, True)
    with pytest.raises(RuntimeError, match="missing.jpg"):
        ppg_core.read_grey(str(tmp_path / "missing.jpg"), 0, True)


# ---------------------------------------------------------------------------------------- the features step
def test_native_features_on_the_trench_photos(trench_store):
    """Every photo done on the CPU, each with its key points inside the photo in COLMAP's pixels and descriptors of
    norm 512; the small store holds at most the preselection's points of each; the progress counts every photo."""
    store, small, report, names, calls = trench_store
    assert report["done"] == PHOTOS and report["unreadable"] == [] and report["unsupported"] == []
    assert report["gpu"] == 0 and report["cpu"] == PHOTOS and isinstance(report["log"], list)
    assert ppg_core.native_store_names(str(store)) == names == ppg_core.native_store_names(str(small))
    for name in names:
        kp, desc, implementation = ppg_core.native_store_read(str(store), name)
        assert implementation == "cpu" and kp.dtype == np.float32 and desc.dtype == np.uint8
        assert kp.shape[1] == 4 and desc.shape == (len(kp), 128) and len(kp) > 200, (name, kp.shape)
        assert kp[:, 0].min() >= 0 and kp[:, 0].max() <= 1200 and kp[:, 1].min() >= 0 and kp[:, 1].max() <= 900
        assert kp[:, 2].min() > 0
        norms = np.linalg.norm(desc.astype(np.float64), axis=1)
        assert np.abs(norms - 512).max() < 0.02 * 512, name
        header = ppg_core.native_store_header(str(store), name)
        assert header["width"] == 1200 and header["height"] == 900 and header["points"] == len(kp)
        assert header["impl"] == "cpu" and header["image_id"] is None and header["max_image_size"] == LIMIT
        small_header = ppg_core.native_store_header(str(small), name)
        assert 0 < small_header["points"] <= SMALL_POINTS and small_header["max_image_size"] == SMALL
    _monotonic(calls, PHOTOS)
    assert len(calls) == PHOTOS + 1  # at the start, then after every photo: min_interval 0


def test_the_gpu_store_has_cpu_twins(trench_store, tmp_path, gpu_test_device, scene_dir):
    """The photos extracted on the GPU (the CPU left out) have the CPU's points as twins (within half a pixel and a
    tenth of the scale) at 95 per cent, the thresholds of the SIFT twins. The same jobs as the CPU store's: with
    a preselection image of 1024 pixels the JPEG is decoded whole and reduced by the triangle filter, where a
    limit of 600 alone would decode it at the half DCT scale, another raster."""
    store, _, _, names, _ = trench_store
    photos = _photos(scene_dir)[:4]
    report = ppg_core.native_features(str(tmp_path / "gpu"), str(tmp_path / "gpu_small"), _jobs(photos), MAX_FEATURES,
                                      SMALL_POINTS, [gpu_test_device], True, False, 2, None)
    assert report["done"] == 4 and report["gpu"] == 4 and report["cpu"] == 0, report["log"]
    for photo in photos:
        ours, _, implementation = ppg_core.native_store_read(str(tmp_path / "gpu"), photo.name)
        theirs, _, _ = ppg_core.native_store_read(str(store), photo.name)
        assert implementation == "gpu"
        d = np.hypot(theirs[:, None, 0] - ours[None, :, 0], theirs[:, None, 1] - ours[None, :, 1])
        j = d.argmin(axis=1)
        twins = (d.min(axis=1) < 0.5) & (np.abs(np.log(ours[j, 2] / theirs[:, 2])) < 0.1)
        assert twins.mean() > 0.95, f"{photo.name}: {twins.mean():.3f} of the CPU's points found on the GPU"


def test_progress_that_raises_stops_the_step(scene_dir, tmp_path):
    """A progress callback that raises on its second call (the first is the start, the second after the first
    photo): the call raises that exception, fewer photos than all are in the store, and no temporary file is left."""
    calls = []

    def progress(done, total, message):
        calls.append((done, total, message))
        if len(calls) == 2:
            raise Cancelled("stop")

    with pytest.raises(Cancelled, match="stop"):
        ppg_core.native_features(str(tmp_path / "f"), None, _jobs(_photos(scene_dir), small_limit=None), MAX_FEATURES,
                                 SMALL_POINTS, [], True, True, 2, progress, min_interval=0)
    names = ppg_core.native_store_names(str(tmp_path / "f"))
    assert 1 <= len(names) < PHOTOS, len(names)
    assert len(calls) == 2 and calls[0][:2] == (0, PHOTOS) and calls[1][0] >= 1  # not called again after it raised
    assert [p for p in tmp_path.rglob("*.tmp")] == []
    for name in names:  # what is in the store is whole
        kp, desc, _ = ppg_core.native_store_read(str(tmp_path / "f"), name)
        assert len(kp) == len(desc) > 0


def test_the_progress_is_throttled(scene_dir, tmp_path):
    """With the default ``min_interval`` of half a second the callback is called at the start, then at most every
    half second (the GIL taken for those calls only, not once per photo), and with the last state when the step
    ends: the first ``(0, n)``, the last ``(n, n)``, no more calls than half seconds elapsed (plus the first and
    the last), half a second at least between two calls but the last, ``done`` never falling - no bound on the
    time the step takes, which a busy computer stretches. A callback that sleeps makes every call slow, so a step
    that called it once per photo would show in the count and in the gaps. A ``min_interval`` that is not a
    number of seconds a duration holds is a ValueError."""
    import time

    calls = []

    def slow_progress(done, total, message):
        calls.append((time.monotonic(), done, total))
        time.sleep(0.02)

    started = time.monotonic()
    ppg_core.native_features(str(tmp_path / "f"), None, _jobs(_photos(scene_dir), small_limit=None), MAX_FEATURES,
                             SMALL_POINTS, [], True, True, 2, slow_progress)
    elapsed = time.monotonic() - started
    assert calls[0][1:] == (0, PHOTOS) and calls[-1][1:] == (PHOTOS, PHOTOS)
    assert 2 <= len(calls) <= elapsed / 0.5 + 2, (len(calls), elapsed)
    gaps = [b[0] - a[0] for a, b in zip(calls, calls[1:-1])]  # between the calls, the last one excepted
    assert all(gap >= 0.45 for gap in gaps), gaps
    done = [d for _, d, _ in calls]
    assert done == sorted(done)
    for absurd in (-1.0, float("nan"), float("inf"), 1e300):
        with pytest.raises(ValueError, match="min_interval"):
            ppg_core.native_features(str(tmp_path / "g"), None, [], MAX_FEATURES, SMALL_POINTS, [], True, True, 2,
                                     None, min_interval=absurd)


def test_a_photo_given_as_pixels(scene_dir, tmp_path):
    """A file the reader does not decode (a GIF) is reported ``unsupported``, and ``native_features_pixels`` with its
    pixels stores it as the step stores a photo it reads itself: the same points as the JPEG of the same pixels."""
    from PIL import Image

    photo = _photos(scene_dir)[0]
    grey, (w, h) = ppg_core.read_grey(str(photo), 0, False)
    Image.fromarray(grey).save(tmp_path / "pattern.gif")
    store, small = str(tmp_path / "features"), str(tmp_path / "small")
    jobs = [(photo.name, str(photo), None, LIMIT, SMALL), ("pattern.gif", str(tmp_path / "pattern.gif"), None, LIMIT,
                                                           SMALL)]
    report = ppg_core.native_features(store, small, jobs, MAX_FEATURES, SMALL_POINTS, [], True, True, 2, None)
    assert report["done"] == 1 and report["unsupported"] == ["pattern.gif"] and report["unreadable"] == []
    assert any("pattern.gif" in line and "not decoded here" in line for line in report["log"]), report["log"]
    given = ppg_core.native_features_pixels(store, small, "pattern.gif", grey, (w, h), LIMIT, SMALL, MAX_FEATURES,
                                            SMALL_POINTS, [])
    assert given["done"] == 1 and given["unsupported"] == [] and given["cpu"] == 1 and given["points"] > 200
    assert ppg_core.native_store_names(store) == ["IMG_0000.jpg", "pattern.gif"]
    kp, desc, implementation = ppg_core.native_store_read(store, "pattern.gif")
    theirs, their_desc, _ = ppg_core.native_store_read(store, photo.name)
    assert implementation == "cpu" and np.array_equal(kp, theirs) and np.array_equal(desc, their_desc)
    assert given["points"] == len(kp)
    small_kp, _, _ = ppg_core.native_store_read(small, "pattern.gif")
    assert 0 < len(small_kp) <= SMALL_POINTS


def test_a_tiny_photo_is_refused_not_an_error(tmp_path):
    """A photo below the 8 x 8 pixels the kernels take is the refusal of that photo - named as ``unsupported`` in
    the result of the step and of ``native_features_pixels`` alike - never a RuntimeError of the step."""
    import cv2

    tiny = tmp_path / "tiny.png"
    cv2.imwrite(str(tiny), np.full((5, 7), 128, np.uint8))
    store = str(tmp_path / "features")
    report = ppg_core.native_features(store, None, [("tiny.png", str(tiny), None, 0, None)], MAX_FEATURES,
                                      SMALL_POINTS, [], True, True, 2, None)
    assert report["done"] == 0 and report["unsupported"] == ["tiny.png"] and report["unreadable"] == []
    assert any("tiny.png" in line and "8 x 8" in line for line in report["log"]), report["log"]
    given = ppg_core.native_features_pixels(store, None, "tiny.png", np.full((5, 7), 128, np.uint8), (7, 5), 0, None,
                                            MAX_FEATURES, SMALL_POINTS, [])
    assert given["done"] == 0 and given["unsupported"] == ["tiny.png"] and given["points"] == 0
    assert given["log"] == [line for line in report["log"] if "tiny.png" in line]  # the words of the step
    assert ppg_core.native_store_names(store) == []
    # a reduction never goes below 8 x 8 (COLMAP clamps it): a thin photo given with a limit is done
    thin = np.random.default_rng(0).integers(0, 255, (12, 400), np.uint8)
    given = ppg_core.native_features_pixels(store, None, "thin.png", thin, (400, 12), 100, None, MAX_FEATURES,
                                            SMALL_POINTS, [])
    assert given["done"] == 1 and given["unsupported"] == []
    assert ppg_core.native_store_header(store, "thin.png")["points"] == given["points"]


def test_the_features_step_refuses_what_it_cannot_do(tmp_path):
    """A missing file is ``unreadable``; a photo given as pixels of the wrong kind, and a store that is not one, are
    errors with their names."""
    store = str(tmp_path / "features")
    report = ppg_core.native_features(store, None, [("gone.jpg", str(tmp_path / "gone.jpg"), None, 0, None)],
                                      MAX_FEATURES, SMALL_POINTS, [], True, True, 2, None)
    assert report["done"] == 0 and report["unreadable"] == ["gone.jpg"] and report["unsupported"] == []
    with pytest.raises(TypeError):
        ppg_core.native_features_pixels(store, None, "float.png", np.zeros((20, 20), np.float32), (20, 20), 0, None,
                                        MAX_FEATURES, SMALL_POINTS, [])
    # pixels smaller than a reduction asked for (the filter would stretch them): refused, naming the two sizes
    with pytest.raises(ValueError, match=r"300 x 225.*600 x 450"):
        ppg_core.native_features_pixels(store, None, "small.png", np.zeros((225, 300), np.uint8), (1200, 900), 600,
                                        None, MAX_FEATURES, SMALL_POINTS, [])
    with pytest.raises(ValueError, match=r"600 x 450.*1024 x 768"):
        ppg_core.native_features_pixels(store, str(tmp_path / "small"), "small.png", np.zeros((450, 600), np.uint8),
                                        (1200, 900), 600, 1024, MAX_FEATURES, SMALL_POINTS, [])
    assert ppg_core.native_store_names(store) == []
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "index.json").write_text("{}")
    with pytest.raises(RuntimeError, match="index.json"):
        ppg_core.native_store_names(str(tmp_path / "broken"))


# --------------------------------------------------------------------------------------------- the stores
def test_the_store_is_written_and_read_from_python(tmp_path):
    """Features written from Python (``Imported``, with COLMAP's image id when known) come back as written; the
    header names the size, the implementation and the id; a photo the store does not hold is a KeyError."""
    rng = np.random.default_rng(1)
    store = str(tmp_path / "store")
    kp = np.column_stack([rng.uniform(0, 1200, 300), rng.uniform(0, 900, 300), rng.uniform(1, 9, 300),
                          rng.uniform(-3, 3, 300)]).astype(np.float32)
    desc = rng.integers(0, 256, (300, 128), np.uint8)
    ppg_core.native_store_write(store, "a photo.jpg", kp, desc, 1200, 900, 7)
    ppg_core.native_store_write(store, "sub/b.jpg", kp[:10], desc[:10], 640, 480, None)
    assert ppg_core.native_store_names(store) == ["a photo.jpg", "sub/b.jpg"]
    got_kp, got_desc, implementation = ppg_core.native_store_read(store, "a photo.jpg")
    assert implementation == "imported" and np.array_equal(got_kp, kp) and np.array_equal(got_desc, desc)
    header = ppg_core.native_store_header(store, "a photo.jpg")
    assert header == {"width": 1200, "height": 900, "points": 300, "impl": "imported", "image_id": 7,
                      "max_image_size": 0, "masked": False}
    assert ppg_core.native_store_header(store, "sub/b.jpg")["image_id"] is None
    # written again: replaced, the store opened (not made again)
    ppg_core.native_store_write(store, "a photo.jpg", kp[:5], desc[:5], 1200, 900, None)
    assert ppg_core.native_store_header(store, "a photo.jpg")["points"] == 5
    with pytest.raises(KeyError, match="nothing.jpg"):
        ppg_core.native_store_read(store, "nothing.jpg")
    with pytest.raises(KeyError, match="nothing.jpg"):
        ppg_core.native_store_header(store, "nothing.jpg")
    with pytest.raises(ValueError, match="128"):
        ppg_core.native_store_write(store, "c.jpg", kp[:5], desc[:5, :64].copy(), 1200, 900, None)
    with pytest.raises(ValueError, match="5 key points"):
        ppg_core.native_store_write(store, "c.jpg", kp[:5], desc[:6], 1200, 900, None)


@pytest.mark.skipif(not os.path.exists("/dev/full"), reason="a device that is always full (Linux's /dev/full)")
def test_a_full_disk_under_the_store_is_an_oserror_of_enospc(tmp_path):
    """A write of the store that the system refuses for a full disk comes out of the core as an ``OSError`` with
    ``errno.ENOSPC`` - a full disk, which no engine can go on with - not as the RuntimeError of the other failures:
    the next feature file of the store is a link to ``/dev/full``, whose every write fails with ENOSPC."""
    import errno

    rng = np.random.default_rng(2)
    store = tmp_path / "store"
    kp = np.column_stack([rng.uniform(0, 640, 50), rng.uniform(0, 480, 50), np.ones(50), np.zeros(50)])
    kp = kp.astype(np.float32)
    desc = rng.integers(0, 256, (50, 128), np.uint8)
    ppg_core.native_store_write(str(store), "a.jpg", kp, desc, 640, 480, None)  # the first file: 00000001.ppgf
    os.symlink("/dev/full", store / "00000002.ppgf")
    with pytest.raises(OSError) as raised:
        ppg_core.native_store_write(str(store), "b.jpg", kp, desc, 640, 480, None)
    assert raised.value.errno == errno.ENOSPC, raised.value
    assert ppg_core.native_store_names(str(store)) == ["a.jpg"]


# ----------------------------------------------------------------------------------------- the preselection
def _cameras(names, prior=False):
    return {name: _camera(prior=prior) for name in names}


@pytest.fixture(scope="module")
def preselected(trench_store):
    """The preselection of the trench on its small store, on the CPU: ``(report, progress calls)``."""
    _, small, _, names, _ = trench_store
    calls = []
    report = ppg_core.native_preselect(str(small), _cameras(names), [], 15, lambda d, t, m: calls.append((d, t, m)),
                                       min_interval=0)
    return report, calls


def test_native_preselect_finds_the_overlapping_pairs(trench_store, preselected):
    """Every pair of the trench tried; the pairs with 15 inliers or more come back as ``(a, b, inliers)`` with
    ``a < b`` and sorted, every photo in some pair; the progress counts on one scale, twice the pairs tried, and
    never falls."""
    _, _, _, names, _ = trench_store
    report, calls = preselected
    tried = PHOTOS * (PHOTOS - 1) // 2
    assert report["pairs_tried"] == tried and isinstance(report["log"], list)
    weights = report["weights"]
    assert weights and weights == sorted(weights)
    assert all(a < b and a in names and b in names and n >= 15 for a, b, n in weights), weights[:3]
    assert len({(a, b) for a, b, _ in weights}) == len(weights)
    assert {a for a, _, _ in weights} | {b for _, b, _ in weights} == set(names)
    _monotonic(calls, 2 * tried)


def test_a_preselection_with_nothing_to_count_reports_no_zero_total(tmp_path):
    """One photo (or none) gives no pair to try: nothing is reported (the engine's scale would be a total of zero,
    which never reaches Python) and the result is empty; a photo without a camera is an error naming it."""
    rng = np.random.default_rng(2)
    small = str(tmp_path / "small")
    kp = np.column_stack([rng.uniform(0, 1200, 50), rng.uniform(0, 900, 50), np.ones(50), np.zeros(50)])
    ppg_core.native_store_write(small, "only.jpg", kp.astype(np.float32), rng.integers(0, 256, (50, 128), np.uint8),
                                1200, 900, None)
    calls = []
    report = ppg_core.native_preselect(small, _cameras(["only.jpg"]), [], 15, lambda d, t, m: calls.append((d, t)),
                                       min_interval=0)
    assert report == {"weights": [], "pairs_tried": 0, "log": []}
    assert calls == []
    with pytest.raises(RuntimeError, match="only.jpg: no camera"):
        ppg_core.native_preselect(small, {}, [], 15, None)
    ppg_core.native_store_write(str(tmp_path / "empty"), "x.jpg", kp[:1].astype(np.float32),
                                rng.integers(0, 256, (1, 128), np.uint8), 10, 10, None)
    (tmp_path / "empty" / "index.json").write_text('{"version": 1, "next": 2, "photos": {}}')
    calls = []
    report = ppg_core.native_preselect(str(tmp_path / "empty"), {}, [], 15, lambda d, t, m: calls.append((d, t)),
                                       min_interval=0)
    assert report["pairs_tried"] == 0 and report["weights"] == [] and calls == []


# ------------------------------------------------------------------------------------- the matching of pairs
def test_native_match_pairs_fills_the_match_store(trench_store, preselected, tmp_path):
    """The strongest preselected pairs and two that were not: every pair done on the CPU and in the store with its
    matches, its verification and its route; the list of the pairs; the progress per pair; a second run on the
    store adds to it."""
    store, _, _, names, _ = trench_store
    report, _ = preselected
    strongest = sorted(report["weights"], key=lambda w: -w[2])[:8]
    pairs = [(a, b, True) for a, b, _ in strongest]
    chosen = {(a, b) for a, b, _ in report["weights"]}
    others = [(a, b) for i, a in enumerate(names) for b in names[i + 1:] if (a, b) not in chosen][:2]
    pairs += [(a, b, False) for a, b in others]
    matches = str(tmp_path / "matches")
    calls = []
    out = ppg_core.native_match_pairs(str(store), matches, pairs, _cameras(names), [], OPTIONS,
                                      lambda d, t, m: calls.append((d, t, m)), min_interval=0)
    assert out["done"] == len(pairs) == out["guided"] + out["brute_force"] + out["dropped"]
    assert out["by_device"] == {"CPU": len(pairs)} and isinstance(out["log"], list)
    _monotonic(calls, len(pairs))
    assert len(calls) == len(pairs) + 1
    assert ppg_core.native_match_pairs_list(matches) == sorted((a, b) for a, b, _ in pairs)
    for a, b, preselected_pair in pairs:
        r = ppg_core.native_match_read(matches, a, b)
        assert set(r) == {"matches", "inliers", "config", "F", "E", "H", "qvec", "tvec", "route"}
        assert r["matches"].dtype == np.uint32 and r["matches"].shape[1] == 2 and r["inliers"].shape[1] == 2
        assert r["route"] in ("guided", "brute_force", "dropped") and r["E"] is None  # no prior focal: no E
        assert r["qvec"] is None and r["tvec"] is None
        if r["route"] == "dropped":
            assert not preselected_pair and len(r["matches"]) == 0 and r["config"] == 0 and r["F"] is None
            continue
        rows = {tuple(m) for m in r["matches"]}
        assert all(tuple(m) in rows for m in r["inliers"])
        if preselected_pair:
            assert r["config"] in (2, 3, 4, 5, 6) and len(r["inliers"]) >= 15, (a, b, r["config"])
            assert r["F"].shape == (3, 3) and r["F"].dtype == np.float64
    assert any(r == "guided" for r in (ppg_core.native_match_read(matches, a, b)["route"] for a, b, _ in pairs))
    assert ppg_core.native_match_read(matches, names[0], "nothing.jpg") is None
    # the store opened again by the next run: the pairs of the first stay
    again = ppg_core.native_match_pairs(str(store), matches, pairs[:1], _cameras(names), [], OPTIONS, None)
    assert again["done"] == 1 and len(ppg_core.native_match_pairs_list(matches)) == len(pairs)


def test_the_matching_refuses_what_it_cannot_do(trench_store, tmp_path):
    """An option that is not one, a pair of a photo the store does not hold or without a camera: errors naming
    them, before any work."""
    store, _, _, names, _ = trench_store
    pairs = [(names[0], names[1], True)]
    with pytest.raises(ValueError, match="stage_one"):
        ppg_core.native_match_pairs(str(store), str(tmp_path / "m"), pairs, _cameras(names), [],
                                    {**OPTIONS, "stage_one": 3}, None)
    with pytest.raises(ValueError, match="two_view_min_inliers"):
        ppg_core.native_match_pairs(str(store), str(tmp_path / "m"), pairs, _cameras(names), [],
                                    {**OPTIONS, "two_view_min_inliers": 3}, None)
    with pytest.raises(RuntimeError, match="nothing.jpg: not in the feature store"):
        ppg_core.native_match_pairs(str(store), str(tmp_path / "m"), [(names[0], "nothing.jpg", True)],
                                    {**_cameras(names), "nothing.jpg": _camera()}, [], OPTIONS, None)
    with pytest.raises(RuntimeError, match=f"{names[1]}: no camera"):
        ppg_core.native_match_pairs(str(store), str(tmp_path / "m"), pairs, {names[0]: _camera()}, [], OPTIONS, None)
    with pytest.raises(ValueError, match="FISHEYE"):
        ppg_core.native_match_pairs(str(store), str(tmp_path / "m"), pairs,
                                    {n: ("OPENCV_FISHEYE", [1.0] * 8, 1200, 900, False) for n in names}, [], OPTIONS,
                                    None)


def test_the_second_stage_can_be_switched_off_and_tuned(trench_store, preselected, tmp_path):
    """``guided`` false: the trusted pairs go to brute force (none guided), the routes otherwise the first stage's;
    ``guided_max_ratio`` and ``guided_band`` are taken; a band that is not a positive number is refused."""
    store, _, _, names, _ = trench_store
    report, _ = preselected
    pairs = [(a, b, True) for a, b, _ in sorted(report["weights"], key=lambda w: -w[2])[:6]]
    on = ppg_core.native_match_pairs(str(store), str(tmp_path / "on"), pairs, _cameras(names), [], OPTIONS, None)
    assert on["guided"] > 0
    off = ppg_core.native_match_pairs(str(store), str(tmp_path / "off"), pairs, _cameras(names), [],
                                      {**OPTIONS, "guided": False}, None)
    assert off["guided"] == 0 and off["brute_force"] == on["guided"] + on["brute_force"]
    assert off["dropped"] == on["dropped"]
    tuned = ppg_core.native_match_pairs(str(store), str(tmp_path / "tuned"), pairs, _cameras(names), [],
                                        {**OPTIONS, "guided_max_ratio": 0.7, "guided_band": 1.5}, None)
    assert tuned["done"] == len(pairs)
    for band in (0, -1.0, float("nan")):
        with pytest.raises(ValueError, match="guided_band"):
            ppg_core.native_match_pairs(str(store), str(tmp_path / "bad"), pairs, _cameras(names), [],
                                        {**OPTIONS, "guided_band": band}, None)


def test_the_settings_of_the_second_stage_come_from_the_environment(monkeypatch):
    """``PPG_NATIVE_DCT`` is off unless set to a value other than 0. ``PPG_NATIVE_GUIDED``, ``PPG_NATIVE_GUIDED_RATIO`` and ``PPG_NATIVE_GUIDED_BAND`` reach the options of the
    pairs stage; the defaults add nothing to them (a core that predates them takes the same dict), the default ratio
    is the matcher's; a value that is not one names its variable."""
    from pyarchinit_photogrammetry.engine import native_match

    for name in ("PPG_NATIVE_GUIDED", "PPG_NATIVE_GUIDED_RATIO", "PPG_NATIVE_GUIDED_BAND", "PPG_NATIVE_DCT"):
        monkeypatch.delenv(name, raising=False)
    # the JPEG decoded whole unless PPG_NATIVE_DCT asks for the DCT scale
    assert native_match.dct_enabled() is False
    for value, enabled in (("1", True), (" 1 ", True), ("0", False), ("", False)):
        monkeypatch.setenv("PPG_NATIVE_DCT", value)
        assert native_match.dct_enabled() is enabled, value
    monkeypatch.delenv("PPG_NATIVE_DCT")
    defaults = native_match.guided_settings()
    assert defaults == {"guided": True, "guided_max_ratio": native_match.GUIDED_RATIO, "guided_band": 1.0}
    assert native_match.MAX_RATIO == inspect.signature(_native.gpu_match).parameters["max_ratio"].default
    plain = native_match.pair_options(8000, defaults)
    assert plain == {"stage1_points": native_match.STAGE1_POINTS, "trust_inliers": native_match.TRUST_INLIERS,
                     "drop_inliers": native_match.DROP_INLIERS, "max_num_matches": 32000,
                     "guided_max_ratio": native_match.GUIDED_RATIO}
    monkeypatch.setenv("PPG_NATIVE_GUIDED", "0")
    monkeypatch.setenv("PPG_NATIVE_GUIDED_RATIO", " 0.7 ")
    monkeypatch.setenv("PPG_NATIVE_GUIDED_BAND", "1.5")
    settings = native_match.guided_settings()
    assert settings == {"guided": False, "guided_max_ratio": 0.7, "guided_band": 1.5}
    assert native_match.pair_options(8000, settings) == {**plain, "guided": False, "guided_max_ratio": 0.7,
                                                         "guided_band": 1.5}
    for name, value in (("PPG_NATIVE_GUIDED_RATIO", "much"), ("PPG_NATIVE_GUIDED_RATIO", "1.2"),
                        ("PPG_NATIVE_GUIDED_BAND", "0"), ("PPG_NATIVE_GUIDED_BAND", "-2")):
        monkeypatch.setenv(name, value)
        with pytest.raises(ValueError, match=name):
            native_match.guided_settings()
        monkeypatch.delenv(name)


def test_a_progress_that_raises_stops_the_matching(trench_store, tmp_path):
    """The exception of the progress callback comes out of ``native_match_pairs`` too, the pairs done before it on
    disk. Every pair of the trench is given: the CPU threads (one per core) each hold a pair when the cancel
    comes, and there are more pairs than cores on any computer."""
    store, _, _, names, _ = trench_store
    pairs = [(a, b, True) for i, a in enumerate(names) for b in names[i + 1:]]
    calls = []

    def progress(done, total, message):
        calls.append(done)
        if len(calls) == 2:
            raise Cancelled("enough")

    matches = str(tmp_path / "matches")
    with pytest.raises(Cancelled, match="enough"):
        ppg_core.native_match_pairs(str(store), matches, pairs, _cameras(names), [], OPTIONS, progress, min_interval=0)
    done = ppg_core.native_match_pairs_list(matches)
    assert 1 <= len(done) < len(pairs) and [p for p in tmp_path.rglob("*.tmp")] == []


# ------------------------------------------------------------------- the step behind engine="native" (native_match.py)
def _verified_pairs(db_path) -> dict:
    """The pairs of ``db_path`` with a verified geometry (15 inliers or more, a configuration that is not undefined,
    degenerate or a watermark), by the names of their photos in the order of the names (not of the image ids, which
    COLMAP's CPU extractor gives in the order its threads finish the photos): ``(config, inliers)``."""
    import sqlite3

    from pyarchinit_photogrammetry.engine import sfm

    con = sqlite3.connect(sfm.read_only_uri(db_path), uri=True)
    try:
        names = dict(con.execute("SELECT image_id, name FROM images"))
        out = {}
        for pair_id, rows, config in con.execute("SELECT pair_id, rows, config FROM two_view_geometries"):
            a, b = divmod(int(pair_id), sfm.COLMAP_MAX_IMAGES)
            if rows >= 15 and config not in (0, 1, 7):
                out[tuple(sorted((names[a], names[b])))] = (int(config), int(rows))
        return out
    finally:
        con.close()


def _geometries(db_path) -> dict:
    """Every two-view geometry of ``db_path`` by the names of its photos: ``(config, the bytes of its inliers)``."""
    import sqlite3

    from pyarchinit_photogrammetry.engine import sfm

    con = sqlite3.connect(sfm.read_only_uri(db_path), uri=True)
    try:
        names = dict(con.execute("SELECT image_id, name FROM images"))
        out = {}
        for pair_id, rows, config, data in con.execute("SELECT pair_id, rows, config, data FROM two_view_geometries"):
            a, b = divmod(int(pair_id), sfm.COLMAP_MAX_IMAGES)
            out[names[a], names[b]] = (int(config), int(rows), bytes(data or b""))
        return out
    finally:
        con.close()


def _image_ids(db_path) -> dict:
    import sqlite3

    from pyarchinit_photogrammetry.engine import sfm

    con = sqlite3.connect(sfm.read_only_uri(db_path), uri=True)
    try:
        return {str(n): int(i) for i, n in con.execute("SELECT image_id, name FROM images")}
    finally:
        con.close()


def _table_count(db_path, table: str) -> int:
    import sqlite3

    from pyarchinit_photogrammetry.engine import sfm

    con = sqlite3.connect(sfm.read_only_uri(db_path), uri=True)
    try:
        return int(con.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # noqa: S608 - a table name of ours
    finally:
        con.close()


def _project(scene_dir, folder, photos=None, markers=False):
    """A project on the trench photos (all of them, or ``photos``), saved in ``folder``."""
    import pyarchinit_photogrammetry as ppg

    doc = ppg.Document()
    ch = doc.add_chunk("Saggio")
    doc.save(folder / "project.ppg")
    ch.add_photos(photos if photos is not None else [scene_dir / "photos"])
    if markers:
        ch.detect_markers("aruco_4x4_50")
    return doc, ch


def _tie_points_per_photo(ch) -> float:
    from pyarchinit_photogrammetry.engine import sfm

    rec = sfm.load_reconstruction(ch)
    counts = [im.num_points3D for im in rec.images.values() if im.has_pose]
    return sum(counts) / len(counts)


@pytest.fixture(scope="module")
def processed_native(scene_dir, tmp_path_factory):
    """The chain of the session's ``processed`` fixture with the matching on the native engine: alignment,
    georeferencing, dense cloud, mesh, texture, DEM and orthomosaic. ``(doc, chunk, report of update_transform)``.

    With the defaults of the program (photos decoded whole, a ratio of 0.6 in the second stage): about 740 tie points
    a photo on these rendered photos and a median error of the points 1.07 times the classic one; the ratio of the
    first stage there instead (0.8) gives 1.35 times, over the bound of 1.1."""
    doc, ch = _project(scene_dir, tmp_path_factory.mktemp("native_chain"), markers=True)
    ch.match_photos(quality="high", engine="native")
    ch.align_cameras()
    ch.import_reference(scene_dir / "gcp.csv", crs="EPSG:3004")
    report = ch.update_transform()
    ch.build_depth_maps(quality="high")
    ch.build_point_cloud()
    ch.build_model()
    ch.build_texture(texture_size=2048)
    ch.build_dem()
    ch.build_orthomosaic()
    doc.save()
    return doc, ch, report


@pytest.fixture(scope="module")
def classic_trench(scene_dir, tmp_path_factory):
    """The trench matched and aligned on the classic engine (COLMAP's RANSAC seeded), the yardstick of the native
    matching: the chunk of a plain matching and the chunk of one with COLMAP's guided matching (the native matching
    always matches along the epipolar lines: that is its like for like)."""
    import os

    from pyarchinit_photogrammetry.engine import sfm

    chunks = []
    previous = os.environ.get(sfm.RANSAC_SEED_ENV)
    os.environ[sfm.RANSAC_SEED_ENV] = "1"
    try:
        for guided in (False, True):
            _, ch = _project(scene_dir, tmp_path_factory.mktemp("classic_trench"), markers=True)
            ch.match_photos(quality="high", engine="classic", guided_matching=guided)
            chunks.append(ch)
    finally:
        if previous is None:
            os.environ.pop(sfm.RANSAC_SEED_ENV, None)
        else:
            os.environ[sfm.RANSAC_SEED_ENV] = previous
    for ch in chunks:
        ch.align_cameras()
    return tuple(chunks)


def _median_point_error(ch) -> float:
    from pyarchinit_photogrammetry.engine import sfm

    rec = sfm.load_reconstruction(ch)
    return float(np.median([p.error for p in rec.points3D.values()]))


def test_native_matching_feeds_the_classic_mapper_on_the_trench(processed_native, classic_trench):
    """``match_photos(engine="native")`` then ``align_cameras()``: every photo aligned from the database the native
    step exported, the record of the engine says native with no fallback, the record of the matching names the
    engine, its features, its routes and its stages, and the block is as good as the classic one: the tie points per
    photo at least 0.9 times the plain classic matching's, the median error of the points within 1.1 times its, and
    the mean reprojection error within 1.05 times the classic matching with COLMAP's guided matching - the native
    matching always matches along the epipolar lines, which (as COLMAP's guided matching) finds many more points
    and, with them, more marginal ones: on this trench 1.5 times the tie points of the plain matching, a mean
    reprojection error 1.2 times the plain matching's and 0.83 times the guided one's, the median error unchanged."""
    from pyarchinit_photogrammetry.engine import native_match, sfm

    _, ch, _ = processed_native
    classic, classic_guided = classic_trench
    assert len(ch.aligned_cameras) == len(ch.cameras) == PHOTOS
    assert ch.meta["engine"]["match_photos"] == {"requested": "native", "used": "native", "fallback": None}
    assert ch.meta["engine"]["align_cameras"] == {"requested": "auto", "used": "classic", "fallback": None}
    info = ch.meta["sfm"]
    assert info["engine"] == "native" and info["features"] in ("gpu", "cpu", "gpu+cpu") and info["matcher"] == "sift"
    assert set(info["routes"]) == {"guided", "brute_force", "dropped"} and info["routes"]["guided"] > 0
    assert sum(info["routes"].values()) == info["pairs"] == PHOTOS * (PHOTOS - 1) // 2
    assert info["pairs_unmatched"] == 0 and info["rigs"] == 0 and info["nodes"] == [] and not info["continued"]
    assert info["cpu_pairs"] + info["gpu_pairs"] == info["pairs"]
    # the settings of the run read from the environment, in the record (the fixture's DCT, the defaults of the
    # second stage)
    assert info["native_dct"] is False
    assert (info["native_guided"], info["native_guided_ratio"], info["native_guided_band"]) == \
        (True, native_match.GUIDED_RATIO, 1.0)
    for key in ("image_root", "names", "quality", "pair_selection", "generic", "reference", "neighbours", "masks",
                "apply_masks", "guided_matching", "stationary_removed", "keypoint_limit", "tiepoint_limit", "added",
                "gpu", "gpu_shares"):
        assert key in info, key
    assert len(info["names"]) == PHOTOS and info["pair_selection"] == "exhaustive"
    stages = ch.meta["timings"]["match_photos"]["stages"]
    assert list(stages) == ["features", "pairs", "export"] and all(v >= 0 for v in stages.values())
    assert sum(stages.values()) <= ch.meta["timings"]["match_photos"]["seconds"] + 0.01
    work = ch.data_dir / "sfm"
    assert (work / native_match.STORES / "features" / "index.json").is_file()
    assert (work / native_match.STORES / "matches" / "index.json").is_file()
    assert not (work / native_match.STORES / "small").exists()  # the preselection store goes at the end of the step
    assert not (work / native_match.TMP).exists() and not (work / native_match.TMP_DB).exists()
    # as good as the classic block: the points of a guided matching, the error of the plain one at the median
    ours, plain, guided = (info["reprojection_error"], classic.meta["sfm"]["reprojection_error"],
                           classic_guided.meta["sfm"]["reprojection_error"])
    assert ours <= 1.05 * guided, (ours, plain, guided)
    medians = _median_point_error(ch), _median_point_error(classic)
    assert medians[0] <= 1.1 * medians[1], medians
    mine, classic_points = _tie_points_per_photo(ch), _tie_points_per_photo(classic)
    assert mine >= 0.9 * classic_points, (mine, classic_points)
    assert classic_guided.meta["sfm"]["guided_matching"] and len(classic_guided.aligned_cameras) == PHOTOS
    # the pairs the classic database verified are verified here too, nearly all of them
    verified = _verified_pairs(work / "database.db")
    classic_verified = _verified_pairs(classic.data_dir / "sfm" / "database.db")
    lost = set(classic_verified) - set(verified)
    assert len(lost) <= 0.02 * len(classic_verified), (len(lost), len(classic_verified))
    # an exhaustive matching tries every pair in full: the first stage drops none (the few classic-verified pairs
    # lost, within the 2 per cent above, fail in the verification of their weak overlap, not to the first stage)
    assert info["routes"]["dropped"] == 0
    assert len(sfm._db_image_names(work / "database.db")) == PHOTOS


@pytest.mark.slow
def test_the_metric_gates_hold_with_native_matching(processed_native, scene):
    """The metric chain of the trench with the matching on the native engine: the control points within 2 mm, the
    check points within 3 mm, the DEM median within 3 mm, the targets in the orthophoto within 5 mm (the assertions
    of ``tests/test_pipeline.py`` on the classic chain)."""
    import rasterio

    from pyarchinit_photogrammetry.markers.targets import detect

    _, ch, report = processed_native
    assert len(ch.aligned_cameras) == len(ch.cameras) == 28 and len(ch.markers) == 8
    assert report["method"] == "markers" and report["control_rmse"] < 0.002
    before = np.array(ch.transform, float)
    for label in ("target 4", "target 5"):
        ch.marker(label).checkpoint = True
    check = ch.update_transform()
    assert check["check_rmse"] < 0.003
    for m in ch.markers:
        m.checkpoint = False
    ch.update_transform()
    ch.transform = before
    with rasterio.open(ch.dem_path) as src:
        dem = src.read(1)
        rows, cols = np.nonzero(dem != src.nodata)
        sel = np.random.default_rng(1).choice(len(rows), 4000, replace=False)
        xs, ys = rasterio.transform.xy(src.transform, rows[sel], cols[sel])
        assert src.crs.to_epsg() == 3004
    err = dem[rows[sel], cols[sel]] - scene.world_height(np.array(xs), np.array(ys))
    assert abs(np.median(err)) < 0.003
    assert 1.4826 * np.median(np.abs(err - np.median(err))) < 0.005
    assert np.percentile(np.abs(err), 90) < 0.02
    with rasterio.open(ch.orthomosaic_path) as src:
        img = np.moveaxis(src.read([3, 2, 1]), 0, -1).copy()
        tr = src.transform
        assert (src.read(4) > 0).mean() > 0.7
    dets = detect(img)
    assert len(dets) >= 6
    for d in dets:
        x, y = rasterio.transform.xy(tr, d["center"][1], d["center"][0])
        tx, ty, _ = scene.targets[d["code"]]
        assert np.hypot(x - tx, y - ty) < 0.005


def test_the_exported_database_imports_back_as_the_stores(processed_native, tmp_path):
    """The database the step exports holds no descriptors (the mapper needs none): exported again with them,
    ``import_database`` gives back the stores it was exported from - the key points of every photo (with COLMAP's
    image id in the header), the matches, inliers and configuration of every pair, imported; what the database
    holds is what the stores hold. A database without descriptors imports no photo."""
    from pyarchinit_photogrammetry.engine import native_match

    _, ch, _ = processed_native
    work = ch.data_dir / "sfm"
    db_path = work / "database.db"
    assert _table_count(db_path, "descriptors") == 0 and _table_count(db_path, "keypoints") == PHOTOS
    assert native_match.import_database(db_path, tmp_path / "none_f", tmp_path / "none_m") == 0
    full = tmp_path / "full.db"
    native_match.export_database(ch, full, descriptors=True)
    assert _table_count(full, "descriptors") == PHOTOS and _image_ids(full).keys() == _image_ids(db_path).keys()
    assert native_match.import_database(full, tmp_path / "f", tmp_path / "m") == PHOTOS
    k = _native.native_match_kernels()
    ours, theirs = k.feature_store(str(work / native_match.STORES / "features")), k.feature_store(str(tmp_path / "f"))
    ids = _image_ids(full)
    assert theirs.names() == ours.names() and len(ids) == PHOTOS
    for name in ours.names():
        kp, desc, implementation = ours.read(name)
        got_kp, got_desc, got_impl = theirs.read(name)
        assert got_impl == "imported" and implementation in ("cpu", "gpu")
        assert np.array_equal(kp, got_kp) and np.array_equal(desc, got_desc), name
        header = theirs.header(name)
        assert header["image_id"] == ids[name] and header["width"] == 1200 and header["height"] == 900
    our_pairs = k.match_store(str(work / native_match.STORES / "matches"))
    their_pairs = k.match_store(str(tmp_path / "m"))
    pairs = our_pairs.pairs()
    assert len(pairs) == PHOTOS * (PHOTOS - 1) // 2
    assert {tuple(p) for p in their_pairs.pairs()} == {tuple(p) for p in pairs}
    for a, b in pairs:
        ours_r, theirs_r = our_pairs.read(a, b), their_pairs.read(a, b)
        assert theirs_r["route"] == "imported" and ours_r["route"] in ("guided", "brute_force", "dropped")
        assert np.array_equal(ours_r["matches"], theirs_r["matches"])
        assert np.array_equal(ours_r["inliers"], theirs_r["inliers"])
        assert ours_r["config"] == theirs_r["config"]
        for key in ("F", "E", "H"):
            if ours_r[key] is None:
                assert theirs_r[key] is None, (a, b, key)
            else:
                assert np.allclose(ours_r[key], theirs_r[key]), (a, b, key)


def test_native_preselection_finds_the_classic_pairs(scene_dir, tmp_path, monkeypatch):
    """``generic=True`` on the trench, classic then native: of the pairs verified by the classic database at most
    2 per cent are not verified in the native one when both found the points with one SIFT (the classic step with
    the core's GPU kernel, the native step with it or its CPU twin), at most 4 per cent when the classic step had no
    GPU and found them with COLMAP's CPU SIFT, and the timings hold the stage ``preselection``. The preselection keeps
    the 20 strongest pairs of every photo, here of the 25 or so it overlaps, and another detector reorders that cut:
    the classic step itself, with the GPU kernel against COLMAP's CPU SIFT, loses 7 of these 326 pairs (2.1 per
    cent; with 45, 51, 75 and 77 inliers in full the strongest of them), the native step with the CPU twin the same
    four and five weak ones (9, 2.8 per cent), against the classic step with the GPU kernel 3 (0.9 per cent)."""
    from pyarchinit_photogrammetry.engine import sfm

    monkeypatch.setenv(sfm.RANSAC_SEED_ENV, "1")
    (tmp_path / "classic").mkdir()
    (tmp_path / "native").mkdir()
    _, classic = _project(scene_dir, tmp_path / "classic")
    classic.match_photos(quality="high", generic=True, engine="classic")
    _, ours = _project(scene_dir, tmp_path / "native")
    ours.match_photos(quality="high", generic=True, engine="native")
    for ch in (classic, ours):
        info = ch.meta["sfm"]
        assert info["generic"] and info["pair_selection"] == "generic"
        assert 2 * PHOTOS <= info["pairs"] < PHOTOS * (PHOTOS - 1) // 2
    assert ours.meta["sfm"]["engine"] == "native"
    assert list(ours.meta["timings"]["match_photos"]["stages"]) == ["features", "preselection", "pairs", "export"]
    assert ours.meta["timings"]["match_photos"]["stages"]["preselection"] > 0
    classic_verified = _verified_pairs(classic.data_dir / "sfm" / "database.db")
    verified = _verified_pairs(ours.data_dir / "sfm" / "database.db")
    lost = set(classic_verified) - set(verified)
    bound = 0.02 if classic.meta["sfm"]["features"] == "gpu" else 0.04  # one SIFT, or COLMAP's CPU SIFT beside it
    assert classic_verified and len(lost) <= bound * len(classic_verified), (sorted(lost), len(classic_verified))
    assert not (ours.data_dir / "sfm" / "native" / "small").exists()
    assert ours.align_cameras(mapper="incremental") == PHOTOS


def test_a_learned_matcher_or_rigs_fall_back(scene_dir, tmp_path, monkeypatch, caplog):
    """A learned matcher and the rigs of 360-degree photos are not the native engine's: ``match_photos`` with
    ``native`` runs the classic step, and the reason kept names the cause."""
    from pyarchinit_photogrammetry.engine import native_match, sfm

    ran = []
    monkeypatch.setattr(sfm, "match_photos", lambda chunk, **kwargs: ran.append(kwargs.get("matcher")))
    native_ran = []
    monkeypatch.setattr(native_match, "match_photos", lambda chunk, **kwargs: native_ran.append(kwargs))
    _, ch = _project(scene_dir, tmp_path)
    assert native_match.cannot_run(ch, "sift", True) is None
    learned = "learned matcher 'disk': the native engine matches SIFT points only"
    assert native_match.cannot_run(ch, "disk", True) == learned
    with caplog.at_level("INFO"):
        ch.match_photos(quality="high", matcher="disk", engine="native")
    assert ran == ["disk"] and native_ran == []
    assert ch.meta["engine"]["match_photos"] == {"requested": "native", "used": "classic",
                                                 "fallback": learned}
    assert any("learned matcher 'disk'" in r.getMessage() for r in caplog.records if r.levelname == "WARNING")
    # a chunk with rigs: two faces of one panorama in the layout of split_panorama
    photos = _photos(scene_dir)
    faces = []
    for face, photo in zip(("front", "right"), photos[:2]):
        folder = tmp_path / "pano" / "facce_360" / face
        folder.mkdir(parents=True)
        (folder / "P.jpg").write_bytes(photo.read_bytes())
        faces.append(folder / "P.jpg")
    (tmp_path / "rig").mkdir()
    _, rig = _project(scene_dir, tmp_path / "rig", photos=faces)
    assert len(rig.cameras) == 2
    for cam in rig.cameras:
        cam.meta.update(panorama="P.jpg", face=Path(cam.path).parent.name)
    assert native_match.cannot_run(rig, "sift", True) == native_match.RIGS_REASON
    assert native_match.RIGS_REASON == "rigs of 360-degree photos: the native engine does not do them"
    rig.match_photos(quality="high", engine="native")
    assert ran == ["disk", "sift"] and native_ran == []
    assert rig.meta["engine"]["match_photos"]["fallback"] == native_match.RIGS_REASON
    # the core switched off, and a camera model the engine does not know
    monkeypatch.setenv("PPG_NATIVE", "0")
    assert "PPG_NATIVE" in native_match.cannot_run(ch, "sift", True)
    monkeypatch.delenv("PPG_NATIVE")
    sensor = next(iter(ch.sensors.values()))
    sensor.model, sensor.params = "FOV", [1000.0, 1000.0, 600.0, 450.0, 0.5]
    reason = native_match.cannot_run(ch, "sift", True)
    assert reason is not None and "FOV" in reason and sensor.label in reason


def test_a_failure_falls_back_and_leaves_no_half_project(scene_dir, tmp_path, monkeypatch, caplog):
    """``native_match_pairs`` raising after the features: the classic result, the fallback ``native matching
    failed: boom`` in the record and in the log, and nothing of the native step left in the project: no
    ``native.tmp``, no ``database.native.tmp.db``, no stores."""
    from pyarchinit_photogrammetry.engine import native_match

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(ppg_core, "native_match_pairs", boom)
    _, ch = _project(scene_dir, tmp_path)
    with caplog.at_level("INFO"):
        ch.match_photos(quality="high", engine="native")
    assert ch.meta["engine"]["match_photos"] == {"requested": "native", "used": "classic",
                                                 "fallback": "native matching failed: boom"}
    assert any("native matching failed: boom" in r.getMessage() for r in caplog.records if r.levelname == "WARNING")
    info = ch.meta["sfm"]
    assert info.get("engine") is None and info["features"] in ("cpu", "gpu") and info["matcher"] == "sift"
    work = ch.data_dir / "sfm"
    assert (work / "database.db").is_file()
    assert not (work / native_match.TMP).exists() and not (work / native_match.TMP_DB).exists()
    assert not (work / native_match.STORES).exists()
    assert [p for p in work.rglob("*.tmp")] == []
    assert "failed" not in ch.meta["timings"]["match_photos"]  # the record of the step that ended: the classic one
    assert ch.align_cameras(mapper="incremental") == PHOTOS


def test_cancel_and_full_disk_leave_the_project_as_it_was(scene_dir, tmp_path, monkeypatch):
    """After a native match, a second one cancelled at the pairs and a third with ``os.replace`` failing for a full
    disk under the sfm folder: ``Cancelled`` and ``OSError`` come out as they are (no fallback), the bytes of
    ``database.db`` and of the index files of the stores are unchanged, the records of the finished step stay, no
    temporary folder or database is left, and the alignment still runs on what there is."""
    import errno
    import os

    from pyarchinit_photogrammetry.core.progress import Progress
    from pyarchinit_photogrammetry.engine import native_match

    _, ch = _project(scene_dir, tmp_path)
    ch.match_photos(quality="high", engine="native")
    work = ch.data_dir / "sfm"
    stores = work / native_match.STORES
    watched = [work / "database.db", stores / "features" / "index.json", stores / "matches" / "index.json"]
    before = [p.read_bytes() for p in watched]
    kept = ({k: (dict(v) if isinstance(v, dict) else v) for k, v in ch.meta["sfm"].items()},
            dict(ch.meta["engine"]["match_photos"]))

    def leftovers():
        return sorted(str(p.relative_to(work)) for p in work.iterdir() if ".tmp" in p.name)

    # cancelled at the pairs
    progress = Progress()
    seen = []

    def watch(_fraction, message):
        seen.append(message)
        if message.startswith(native_match.PROGRESS_PAIRS + " ("):  # the step's own line before the pairs
            progress.cancel()

    progress.callback = watch
    with pytest.raises(Cancelled):
        ch.match_photos(quality="high", engine="native", progress=progress)
    assert any(m.startswith(native_match.PROGRESS_FEATURES) for m in seen)
    assert [p.read_bytes() for p in watched] == before and leftovers() == []
    assert ch.meta["timings"]["match_photos"]["failed"] == "Cancelled"
    # a full disk when the result is moved into place
    real_replace = os.replace

    def replace(src, dst, *args, **kwargs):
        if str(src).startswith(str(work)) or str(dst).startswith(str(work)):
            raise OSError(errno.ENOSPC, "No space left on device", str(dst))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(native_match.os, "replace", replace)
    with pytest.raises(OSError) as full:
        ch.match_photos(quality="high", engine="native")
    assert full.value.errno == errno.ENOSPC
    monkeypatch.undo()
    assert [p.read_bytes() for p in watched] == before and leftovers() == []
    assert ch.meta["timings"]["match_photos"]["failed"] == "OSError"
    assert ({k: (dict(v) if isinstance(v, dict) else v) for k, v in ch.meta["sfm"].items()},
            dict(ch.meta["engine"]["match_photos"])) == kept
    assert ch.align_cameras(mapper="incremental") == PHOTOS


def test_a_lost_gpu_gives_the_same_matches(scene_dir, tmp_path, monkeypatch, gpu_test_device, caplog):
    """A GPU that fails in the middle of the matching (``PPG_TEST_GPU_FAIL_AFTER``, the test hook of the scheduler:
    every GPU worker retired at its N-th unit) gives its pairs back to the CPU, and the result is the one of
    ``gpu="off"`` pair for pair - the same verified pairs with the same inliers, since the kernels are exact on every
    device and the RANSAC is seeded by the pair - with a log line naming the GPU. The features are kept on the CPU
    (the hook set to 1 while the points are detected: the GPU and CPU SIFT are twins, not the same bytes) and the
    hook moved to 3 as the pairs start, so the GPU does the first stage of two pairs before it is lost."""
    import os

    from pyarchinit_photogrammetry.engine import native_match

    gpu = "auto" if gpu_test_device == -1 else str(gpu_test_device)
    monkeypatch.setenv("PPG_TEST_GPU_FAIL_AFTER", "1")

    def progress(_fraction, message):
        if message.startswith(native_match.PROGRESS_PAIRS + " ("):  # the step's own line, before the core's pairs
            os.environ["PPG_TEST_GPU_FAIL_AFTER"] = "3"

    (tmp_path / "gpu").mkdir()
    (tmp_path / "cpu").mkdir()
    _, with_gpu = _project(scene_dir, tmp_path / "gpu")
    with caplog.at_level("INFO"):
        with_gpu.match_photos(quality="high", engine="native", gpu=gpu, progress=progress)
    monkeypatch.delenv("PPG_TEST_GPU_FAIL_AFTER")
    _, cpu_only = _project(scene_dir, tmp_path / "cpu")
    cpu_only.match_photos(quality="high", engine="native", gpu="off")
    info, plain = with_gpu.meta["sfm"], cpu_only.meta["sfm"]
    assert info["features"] == "cpu" and plain["features"] == "cpu"
    # The device is shared: a CPU worker's request can make it lost between the stage 1 and a brute force of
    # the GPU worker's pair, which then goes back to the queue - so one or two pairs end on the GPU.
    assert 1 <= info["gpu_pairs"] <= 2 and info["gpu"] and info["gpu_shares"]
    assert sum(info["gpu_shares"].values()) == info["gpu_pairs"]
    assert plain["gpu_pairs"] == 0 and plain["gpu"] is None and plain["cpu_pairs"] == plain["pairs"]
    assert info["gpu_pairs"] + info["cpu_pairs"] == info["pairs"] == plain["pairs"]
    assert info["routes"] == plain["routes"]
    assert (_geometries(with_gpu.data_dir / "sfm" / "database.db")
            == _geometries(cpu_only.data_dir / "sfm" / "database.db"))
    retired = [r.getMessage() for r in caplog.records if "PPG_TEST_GPU_FAIL_AFTER" in r.getMessage()]
    assert retired and all("GPU" in line for line in retired), retired


def test_continuing_a_classic_project_imports_it(scene_dir, tmp_path, monkeypatch):
    """A classic match and alignment of 24 trench photos, then 4 more and ``match_photos(engine="native",
    reset=False)``: the database is imported into the stores (24 photos, their key points and image ids kept),
    only the pairs with one of the 4 are matched, the exported database keeps the image ids of the 24 (the model
    continues from them), and ``align_cameras(reset=False)`` aligns all 28."""
    from pyarchinit_photogrammetry.engine import native_match

    photos = _photos(scene_dir)
    _, ch = _project(scene_dir, tmp_path, photos=photos[:24])
    ch.match_photos(quality="high", engine="classic")
    assert ch.align_cameras(mapper="incremental", use_gps=False) == 24
    work = ch.data_dir / "sfm"
    ids_before = _image_ids(work / "database.db")
    assert len(ids_before) == 24
    imported = []
    real_import = native_match.import_database
    monkeypatch.setattr(native_match, "import_database",
                        lambda *a, **k: imported.append(real_import(*a, **k)) or imported[-1])
    ch.add_photos(photos[24:])
    ch.match_photos(quality="high", engine="native", reset=False)
    assert imported == [24]
    info = ch.meta["sfm"]
    assert info["engine"] == "native" and info["continued"] and sorted(info["added"]) == [p.name for p in photos[24:]]
    assert info["pairs"] == 4 * 24 + 4 * 3 // 2 and info["pairs_unmatched"] == 0
    assert ch.meta["engine"]["match_photos"] == {"requested": "native", "used": "native", "fallback": None}
    k = _native.native_match_kernels()
    store = k.feature_store(str(work / native_match.STORES / "features"))
    assert store.names() == [p.name for p in photos]
    for name in ids_before:
        header = store.header(name)
        assert header["impl"] == "imported" and header["image_id"] == ids_before[name], name
    for photo in photos[24:]:
        assert store.header(photo.name)["impl"] in ("cpu", "gpu")
    ids_after = _image_ids(work / "database.db")
    assert len(ids_after) == 28 and {n: ids_after[n] for n in ids_before} == ids_before
    assert _table_count(work / "database.db", "descriptors") == 0  # the mapper needs none: the stores keep them
    # the imported key points are COLMAP's six columns reduced to four: the same x, y, a scale and an orientation
    import sqlite3

    from pyarchinit_photogrammetry.engine import sfm

    matches = k.match_store(str(work / native_match.STORES / "matches"))
    pairs = matches.pairs()
    assert len(pairs) == 24 * 23 // 2 + info["pairs"]
    routes = {matches.read(a, b)["route"] for a, b in pairs}
    assert "imported" in routes and routes & {"guided", "brute_force", "dropped"}
    con = sqlite3.connect(sfm.read_only_uri(work / "database.db"), uri=True)
    try:
        rows = {int(i): int(c) for i, c in con.execute("SELECT image_id, cols FROM keypoints")}
    finally:
        con.close()
    assert set(rows.values()) == {4}  # the export writes four columns for every photo
    assert ch.align_cameras(reset=False, use_gps=False) == 28 and ch.meta["sfm"]["mapper"] == "incremental"


def test_classic_continuation_of_a_native_project(scene_dir, tmp_path, monkeypatch):
    """A native match and alignment of 24, then 4 added and ``match_photos(engine="classic", reset=False)``: the
    descriptors of the 24 are exported first (the classic matcher needs them), the classic matcher matches only the
    new pairs, and all 28 are aligned."""
    from pyarchinit_photogrammetry.engine import native_match

    photos = _photos(scene_dir)
    _, ch = _project(scene_dir, tmp_path, photos=photos[:24])
    ch.match_photos(quality="high", engine="native")
    assert ch.align_cameras(mapper="incremental", use_gps=False) == 24
    work = ch.data_dir / "sfm"
    assert _table_count(work / "database.db", "descriptors") == 0
    ids_before = _image_ids(work / "database.db")
    exports = []
    real_export = native_match.export_database
    monkeypatch.setattr(native_match, "export_database",
                        lambda chunk, db_path, descriptors=False, **k: exports.append(descriptors) or real_export(
                            chunk, db_path, descriptors, **k))
    ch.add_photos(photos[24:])
    ch.match_photos(quality="high", engine="classic", reset=False)
    assert exports == [True]
    info = ch.meta["sfm"]
    assert info.get("engine") is None and info["continued"] and len(info["added"]) == 4
    assert info["pairs"] == 4 * 24 + 4 * 3 // 2
    assert ch.meta["engine"]["match_photos"] == {"requested": "classic", "used": "classic", "fallback": None}
    assert _table_count(work / "database.db", "descriptors") == 28
    ids_after = _image_ids(work / "database.db")
    assert {n: ids_after[n] for n in ids_before} == ids_before
    assert ch.align_cameras(reset=False, use_gps=False) == 28
    # the stores of the native step are no longer trusted by a native continuation: a classic record has no engine
    assert not (work / native_match.STORES).exists() or info.get("engine") != "native"


def test_masks_apply_to_the_native_key_points(scene_dir, tmp_path):
    """A mask black on the left half of one photo: no key point of that photo in its left half in the exported
    database, the other photos untouched."""
    import sqlite3

    from pyarchinit_photogrammetry.engine import sfm
    from pyarchinit_photogrammetry.engine.masks import write_mask

    _, ch = _project(scene_dir, tmp_path)
    cam0 = ch.cameras[0]
    keep = np.ones((900, 1200), bool)
    keep[:, :600] = False
    write_mask(ch, cam0, keep)
    ch.match_photos(quality="high", engine="native")
    info = ch.meta["sfm"]
    assert info["masks"] and info["apply_masks"] == "keypoints" and info["engine"] == "native"
    name = info["names"][str(cam0.key)]
    con = sqlite3.connect(sfm.read_only_uri(ch.data_dir / "sfm" / "database.db"), uri=True)
    try:
        ids = {str(n): int(i) for i, n in con.execute("SELECT image_id, name FROM images")}
        points = {}
        for image_id, rows, cols, data in con.execute("SELECT image_id, rows, cols, data FROM keypoints"):
            points[image_id] = np.frombuffer(data, np.float32).reshape(rows, cols)
    finally:
        con.close()
    masked = points[ids[name]]
    assert len(masked) > 100 and masked[:, 0].min() >= 600
    other = points[ids[info["names"][str(ch.cameras[1].key)]]]
    assert other[:, 0].min() < 600
    k = _native.native_match_kernels()
    header = k.feature_store(str(ch.data_dir / "sfm" / "native" / "features")).header(name)
    assert header["masked"] and header["points"] == len(masked)
    assert ch.align_cameras(mapper="incremental", use_gps=False) == PHOTOS


def test_the_store_handles_read_and_write_hundreds_of_items_with_one_open(tmp_path):
    """``NativeFeatureStore`` and ``NativeMatchStore`` hold a store open: hundreds of photos and pairs written and
    read through one handle (the export of a project reads every item: a store opened per item would check every
    file of it each time), what one handle writes another sees, the pairs flushed to disk on request and read back
    by the readers that open the store on their own; what the handles refuse is named."""
    k = _native.native_match_kernels()
    assert k.feature_store is ppg_core.NativeFeatureStore and k.match_store is ppg_core.NativeMatchStore
    rng = np.random.default_rng(3)
    store = k.feature_store(str(tmp_path / "features"))
    photos = {}
    for i in range(300):
        n = 20 + i % 7
        kp = np.column_stack([rng.uniform(0, 1200, n), rng.uniform(0, 900, n), rng.uniform(1, 9, n),
                              rng.uniform(-3, 3, n)]).astype(np.float32)
        desc = rng.integers(0, 256, (n, 128), np.uint8)
        photos[f"folder {i % 3}/IMG_{i:04d}.jpg"] = (kp, desc)
        store.write(f"folder {i % 3}/IMG_{i:04d}.jpg", kp, desc, 1200, 900, i + 1 if i % 2 else None)
    assert store.names() == sorted(photos)
    for i, (name, (kp, desc)) in enumerate(sorted(photos.items())):
        got_kp, got_desc, implementation = store.read(name)
        assert implementation == "imported" and np.array_equal(got_kp, kp) and np.array_equal(got_desc, desc)
        only_kp, nothing, _ = store.read(name, descriptors=False)
        assert nothing is None and np.array_equal(only_kp, kp)
    header = store.header("folder 1/IMG_0001.jpg")
    assert header == {"width": 1200, "height": 900, "points": 21, "impl": "imported", "image_id": 2,
                      "max_image_size": 0, "masked": False}
    assert store.header("folder 0/IMG_0000.jpg")["image_id"] is None
    another = k.feature_store(str(tmp_path / "features"))  # the same store, opened again: everything there
    assert another.names() == store.names()
    assert ppg_core.native_store_names(str(tmp_path / "features")) == store.names()
    with pytest.raises(KeyError, match="nothing.jpg"):
        store.read("nothing.jpg")
    with pytest.raises(KeyError, match="nothing.jpg"):
        store.header("nothing.jpg")
    with pytest.raises(ValueError, match="128"):
        store.write("c.jpg", photos["folder 0/IMG_0000.jpg"][0], np.zeros((20, 64), np.uint8), 1200, 900)
    with pytest.raises(RuntimeError, match="index.json"):
        (tmp_path / "broken").mkdir()
        (tmp_path / "broken" / "index.json").write_text("{}")
        k.feature_store(str(tmp_path / "broken"))
    # the match store
    matches = k.match_store(str(tmp_path / "matches"))
    names = sorted(photos)
    pairs = {}
    for i in range(400):
        a, b = names[i % 300], names[(i * 7 + 1) % 300]
        if a == b or (a, b) in pairs:
            continue
        m = rng.integers(0, 20, (10 + i % 5, 2)).astype(np.uint32)
        inl = m[: 3 + i % 4]
        f = rng.normal(size=(3, 3)) if i % 2 else None
        h = rng.normal(size=(3, 3)) if i % 3 == 0 else None
        q = np.array([1.0, 0.0, 0.0, 0.0]) if i % 5 == 0 else None
        t = np.array([0.1, 0.2, 0.3]) if i % 5 == 0 else None
        pairs[a, b] = (m, inl, 2 + i % 5, f, h, q, t)
        matches.write(a, b, m, inl, 2 + i % 5, F=f, E=None, H=h, qvec=q, tvec=t)
    assert len(pairs) >= 300
    assert matches.pairs() == sorted(pairs)
    for (a, b), (m, inl, config, f, h, q, t) in pairs.items():  # read back from the buffer, before any flush
        r = matches.read(a, b)
        assert r["route"] == "imported" and r["config"] == config
        assert np.array_equal(r["matches"], m) and np.array_equal(r["inliers"], inl) and r["E"] is None
        assert (r["F"] is None) == (f is None) and (f is None or np.allclose(r["F"], f))
        assert (r["H"] is None) == (h is None) and (h is None or np.allclose(r["H"], h))
        assert (r["qvec"] is None) == (q is None)
        assert q is None or (np.allclose(r["qvec"], q) and np.allclose(r["tvec"], t))
    assert matches.read(names[0], "nothing.jpg") is None
    matches.flush()  # on disk: the readers that open the store on their own see every pair
    assert ppg_core.native_match_pairs_list(str(tmp_path / "matches")) == sorted(pairs)
    a, b = next(iter(sorted(pairs)))
    assert np.array_equal(ppg_core.native_match_read(str(tmp_path / "matches"), a, b)["matches"], pairs[a, b][0])
    assert k.match_store(str(tmp_path / "matches")).pairs() == sorted(pairs)
    with pytest.raises(ValueError, match="9"):
        matches.write(a, b, pairs[a, b][0], pairs[a, b][1], 9)
    with pytest.raises(ValueError, match=r"\(n, 2\)"):
        matches.write(a, b, np.zeros((3, 3), np.uint32), pairs[a, b][1], 2)
    with pytest.raises(ValueError, match="3 x 3"):
        matches.write(a, b, pairs[a, b][0], pairs[a, b][1], 2, F=np.zeros((2, 2)))
    # a pair written again replaces its result
    matches.write(a, b, pairs[a, b][0][:2], pairs[a, b][0][:1], 3)
    assert len(matches.read(a, b)["matches"]) == 2 and matches.read(a, b)["config"] == 3


def _gps_references(ch, scene, noise: float = 0.05, seed: int = 3) -> None:
    """The true positions of the synthetic cameras, a little noise added, as the GPS of the photos (as the classic
    tests of the reference preselections set them)."""
    from pyarchinit_photogrammetry.core.camera import Reference
    from pyarchinit_photogrammetry.testing.synthetic import camera_poses

    rng = np.random.default_rng(seed)
    poses = camera_poses(scene)
    for cam in ch.cameras:
        i = int(cam.label.split("_")[-1])
        xyz = poses[i][:3, 3] + np.asarray(scene.origin) + rng.normal(0.0, noise, 3)
        cam.reference = Reference(location=[float(v) for v in xyz], accuracy=[0.1, 0.1, 0.2], enabled=True)
        cam.meta["reference_crs"] = scene.crs


def test_a_cancelled_or_failed_continuation_leaves_the_block_as_it_was(scene_dir, tmp_path, monkeypatch):
    """A native match and alignment of 24, 4 added, then ``match_photos(engine="native", reset=False)`` cancelled at
    the pairs and again failing for a full disk at the export: the stores in place and ``database.db`` keep their
    bytes (the continuation works on a copy of the stores), nothing is left behind, and the next continuation takes
    the 4 photos into the database (the ids of the 24 kept) and the alignment registers them."""
    import errno
    import os

    from pyarchinit_photogrammetry.core.progress import Progress
    from pyarchinit_photogrammetry.engine import native_match

    photos = _photos(scene_dir)
    _, ch = _project(scene_dir, tmp_path, photos=photos[:24])
    ch.match_photos(quality="high", engine="native")
    assert ch.align_cameras(mapper="incremental", use_gps=False) == 24
    work = ch.data_dir / "sfm"
    stores = work / native_match.STORES
    watched = [work / "database.db", stores / "features" / "index.json", stores / "matches" / "index.json"]
    before = [p.read_bytes() for p in watched]
    ids_before = _image_ids(work / "database.db")
    first = [p.name for p in photos[:24]]

    def leftovers():
        return sorted(str(p.relative_to(work)) for p in work.iterdir() if ".tmp" in p.name)

    ch.add_photos(photos[24:])
    progress = Progress()

    def watch(_fraction, message):
        if message.startswith(native_match.PROGRESS_PAIRS + " ("):
            progress.cancel()

    progress.callback = watch
    with pytest.raises(Cancelled):
        ch.match_photos(quality="high", engine="native", reset=False, progress=progress)
    assert [p.read_bytes() for p in watched] == before and leftovers() == []
    assert ppg_core.native_store_names(str(stores / "features")) == first  # no new photo in the stores in place
    real_replace = os.replace

    def replace(src, dst, *args, **kwargs):
        if str(src).startswith(str(work)) or str(dst).startswith(str(work)):
            raise OSError(errno.ENOSPC, "No space left on device", str(dst))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(native_match.os, "replace", replace)
    with pytest.raises(OSError) as full:
        ch.match_photos(quality="high", engine="native", reset=False)
    assert full.value.errno == errno.ENOSPC
    monkeypatch.undo()
    assert [p.read_bytes() for p in watched] == before and leftovers() == []
    assert ppg_core.native_store_names(str(stores / "features")) == first
    assert ch.meta["sfm"]["continued"] is False and ch.meta["sfm"]["added"] is None  # the record of the first step
    # the next continuation finishes
    ch.match_photos(quality="high", engine="native", reset=False)
    info = ch.meta["sfm"]
    assert info["continued"] and sorted(info["added"]) == [p.name for p in photos[24:]]
    assert info["pairs"] == 4 * 24 + 4 * 3 // 2 and leftovers() == []
    ids_after = _image_ids(work / "database.db")
    assert len(ids_after) == 28 and {n: ids_after[n] for n in ids_before} == ids_before
    assert ppg_core.native_store_names(str(stores / "features")) == [p.name for p in photos]
    assert len(ppg_core.native_match_pairs_list(str(stores / "matches"))) == 24 * 23 // 2 + info["pairs"]
    assert ch.align_cameras(reset=False, use_gps=False) == 28


def test_a_classic_continuation_without_the_core_matches_everything_again(scene_dir, tmp_path, monkeypatch, caplog):
    """A native match and alignment of 24, 4 added, the native core switched off (``PPG_NATIVE=0``) and
    ``match_photos(engine="classic", reset=False)``: the descriptors of the 24 cannot be put into the database, so
    the classic step says so and matches every photo again instead of continuing without them; the stores go, the
    database holds the descriptors of all 28, and the alignment runs."""
    from pyarchinit_photogrammetry.engine import native_match

    photos = _photos(scene_dir)
    _, ch = _project(scene_dir, tmp_path, photos=photos[:24])
    ch.match_photos(quality="high", engine="native")
    assert ch.align_cameras(mapper="incremental", use_gps=False) == 24
    work = ch.data_dir / "sfm"
    ch.add_photos(photos[24:])
    monkeypatch.setenv("PPG_NATIVE", "0")
    seen = []
    with caplog.at_level("INFO"):
        ch.match_photos(quality="high", engine="classic", reset=False, progress=lambda f, m: seen.append(m))
    said = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("descriptors of the native matching cannot be put into the database" in m and "every photo again" in m
               for m in said), said
    assert any("every photo again" in m for m in seen)
    info = ch.meta["sfm"]
    assert info.get("engine") is None and info["continued"] is False and info["added"] is None
    assert ch.meta["engine"]["match_photos"] == {"requested": "classic", "used": "classic", "fallback": None}
    assert len(_image_ids(work / "database.db")) == 28 and _table_count(work / "database.db", "descriptors") == 28
    assert not (work / native_match.STORES).exists()
    assert ch.align_cameras(reset=False, use_gps=False) == 28


def test_the_reference_preselections_pick_the_pairs_natively(scene_dir, scene, tmp_path):
    """The reference preselections on the native path, as the classic test has them: ``source`` (the GPS of the
    photos, every photo with its nearest), ``estimated`` (the positions of the alignment before), ``sequential`` (the
    photos before and after in the capture order); each pair chosen by them is matched in full (none dropped), the
    record names the preselection and the stage, and the block aligns on the source pairs."""
    from pyarchinit_photogrammetry.engine import sfm

    _, ch = _project(scene_dir, tmp_path)
    _gps_references(ch, scene)
    n = PHOTOS
    every = n * (n - 1) // 2
    ch.match_photos(quality="high", generic=False, reference="source", neighbours=4, engine="native")
    info = ch.meta["sfm"]
    names = {int(k): v for k, v in info["names"].items()}
    assert info["engine"] == "native" and info["pair_selection"] == "source" and info["reference"] == "source"
    assert not info["generic"] and info["neighbours"] == 4
    assert info["pairs"] == len(sfm._reference_pairs(ch, names, neighbors=4)) and 2 * n <= info["pairs"] < every
    assert info["routes"]["dropped"] == 0 and sum(info["routes"].values()) == info["pairs"]
    assert list(ch.meta["timings"]["match_photos"]["stages"]) == ["features", "preselection", "pairs", "export"]
    assert ch.align_cameras(mapper="incremental", use_gps=False) == n
    ch.match_photos(quality="high", generic=False, reference="estimated", neighbours=4, engine="native")
    info = ch.meta["sfm"]
    assert info["pair_selection"] == "estimated" and info["pairs"] == len(sfm._estimated_pairs(ch, names, 4))
    assert 2 * n <= info["pairs"] < every and info["routes"]["dropped"] == 0
    ch.match_photos(quality="high", generic=False, reference="sequential", neighbours=3, engine="native")
    info = ch.meta["sfm"]
    assert info["pair_selection"] == "sequential" and info["neighbours"] == 3
    assert info["pairs"] == sum(min(3, n - 1 - i) for i in range(n)) and info["routes"]["dropped"] == 0
    with pytest.raises(ValueError):
        ch.match_photos(quality="high", reference="nowhere", engine="native")


def test_photo_names_with_blanks_go_through_the_stores_and_the_database(scene_dir, tmp_path):
    """Photos in a folder with a blank in its name, some in a subfolder with another, named with blanks: the native
    step keys the stores by those names, exports them to the database as they are (no alias, no record of aliases
    beside it), the export with descriptors imports back under the same names, and the alignment reads them."""
    from pyarchinit_photogrammetry.engine import native_match, sfm

    folder = tmp_path / "foto scavo"
    sub = folder / "giorno 2"
    sub.mkdir(parents=True)
    copies = []
    for i, photo in enumerate(_photos(scene_dir)[:8]):
        copy = (folder if i < 4 else sub) / f"IMG {i:02d}.jpg"
        copy.write_bytes(photo.read_bytes())
        copies.append(copy)
    (tmp_path / "p").mkdir()
    _, ch = _project(scene_dir, tmp_path / "p", photos=copies)
    ch.match_photos(quality="high", engine="native")
    info = ch.meta["sfm"]
    names = sorted(info["names"].values())
    assert names == sorted([f"IMG {i:02d}.jpg" for i in range(4)] + [f"giorno 2/IMG {i:02d}.jpg" for i in range(4, 8)])
    assert info["image_root"] == str(folder.resolve()) and info["pairs"] == 8 * 7 // 2
    work = ch.data_dir / "sfm"
    assert set(_image_ids(work / "database.db")) == set(names) and not (work / sfm.LIST_NAMES_FILE).exists()
    k = _native.native_match_kernels()
    assert k.feature_store(str(work / native_match.STORES / "features")).names() == names
    pairs = k.match_store(str(work / native_match.STORES / "matches")).pairs()
    assert len(pairs) == 28 and {n for pair in pairs for n in pair} == set(names)
    full = tmp_path / "full.db"
    native_match.export_database(ch, full, descriptors=True)
    assert native_match.import_database(full, tmp_path / "f", tmp_path / "m") == 8
    assert k.feature_store(str(tmp_path / "f")).names() == names
    assert k.match_store(str(tmp_path / "m")).pairs() == pairs
    assert ch.align_cameras(mapper="incremental", use_gps=False) >= 6


# ------------------------------------------------------------------------------------- the final fix wave of M2
def _native_block(scene_dir, tmp_path, count=24, **kwargs):
    """A native match and alignment of the first ``count`` trench photos, then the others added: ``(chunk, sfm
    folder, the photos)``."""
    photos = _photos(scene_dir)
    _, ch = _project(scene_dir, tmp_path, photos=photos[:count])
    ch.match_photos(quality="high", engine="native", **kwargs)
    assert ch.align_cameras(mapper="incremental", use_gps=False) == count
    ch.add_photos(photos[count:])
    return ch, ch.data_dir / "sfm", photos


def _leftovers(work):
    return sorted(str(p.relative_to(work)) for p in work.iterdir() if ".tmp" in p.name)


def test_continuing_with_other_settings_matches_everything_again(scene_dir, tmp_path, monkeypatch):
    """A native match and alignment of 24, 4 added, then ``reset=False`` with another point limit: the stores are
    not trusted, and the database is the native export - key points without descriptors - so nothing of it is
    imported (its matches index the old key points): every photo again, all 28 with points of the new limit, and
    the import of such a database writes no pair either."""
    from pyarchinit_photogrammetry.engine import native_match

    ch, work, photos = _native_block(scene_dir, tmp_path)
    # the import of the native export: no photo has descriptors, so no pair goes into the stores
    imported = native_match.import_database(work / "database.db", tmp_path / "f", tmp_path / "m")
    assert imported == 0
    assert ppg_core.native_match_pairs_list(str(tmp_path / "m")) == []
    calls = []
    real_import = native_match.import_database
    monkeypatch.setattr(native_match, "import_database", lambda *a, **k: calls.append(a) or real_import(*a, **k))
    seen = []
    ch.match_photos(quality="high", engine="native", reset=False, keypoint_limit=20000,
                    progress=lambda _f, m: seen.append(m))
    assert calls == [] and any("every photo again" in m for m in seen)
    info = ch.meta["sfm"]
    assert info["engine"] == "native" and info["continued"] is False and info["added"] is None
    assert info["keypoint_limit"] == 20000 and info["pairs"] == PHOTOS * (PHOTOS - 1) // 2
    store = ppg_core.native_store_names(str(work / native_match.STORES / "features"))
    assert store == [p.name for p in photos]
    assert {ppg_core.native_store_header(str(work / "native" / "features"), n)["impl"] for n in store} <= {"cpu", "gpu"}
    assert len(_image_ids(work / "database.db")) == PHOTOS


@pytest.mark.parametrize("change", ["ratio", "dct", "record_before_the_settings"])
def test_continuing_with_other_matching_settings_matches_everything_again(scene_dir, tmp_path, monkeypatch, change):
    """A native match and alignment of 24, 4 added, then ``reset=False`` with the stores made under other settings
    of the decoding or of the second stage - another ratio, the DCT scale, or a record written before those
    settings were recorded (the DCT scale and the first stage's ratio were the program's then): the stores are not
    trusted, every photo again, and the record says the settings that made all of them."""
    from pyarchinit_photogrammetry.engine import native_match

    ch, work, photos = _native_block(scene_dir, tmp_path)
    if change == "ratio":
        monkeypatch.setenv("PPG_NATIVE_GUIDED_RATIO", "0.7")
    elif change == "dct":
        monkeypatch.setenv("PPG_NATIVE_DCT", "1")
    else:
        for key in ("native_dct", "native_guided", "native_guided_ratio", "native_guided_band"):
            ch.meta["sfm"].pop(key, None)
    seen = []
    ch.match_photos(quality="high", engine="native", reset=False, progress=lambda _f, m: seen.append(m))
    assert any("every photo again" in m for m in seen)
    info = ch.meta["sfm"]
    assert info["continued"] is False and info["pairs"] == PHOTOS * (PHOTOS - 1) // 2
    assert info["native_dct"] is (change == "dct")
    assert info["native_guided_ratio"] == (0.7 if change == "ratio" else native_match.GUIDED_RATIO)


def test_a_full_disk_in_the_database_stops_both_engines_and_keeps_the_block(scene_dir, tmp_path, monkeypatch):
    """A native match and alignment of 24, 4 added; the export to COLMAP's database failing as a full disk fails
    there - pycolmap's RuntimeError and sqlite's OperationalError, with the words sqlite gives a full disk -: the
    native continuation raises ``OSError(ENOSPC)`` without falling back to the classic step, and the classic
    continuation, whose descriptors cannot be put into the database, raises it too instead of throwing the
    database and the stores away. Both leave the block as it was, and the classic continuation goes on once there
    is room."""
    import errno
    import sqlite3

    from pyarchinit_photogrammetry.engine import native_match, sfm

    ch, work, photos = _native_block(scene_dir, tmp_path)
    stores = work / native_match.STORES
    watched = [work / "database.db", stores / "features" / "index.json", stores / "matches" / "index.json"]
    before = [p.read_bytes() for p in watched]
    kept = dict(ch.meta["engine"]["match_photos"])
    classic_runs = []
    real_classic = sfm.match_photos
    monkeypatch.setattr(sfm, "match_photos", lambda *a, **k: classic_runs.append(k) or real_classic(*a, **k))
    pycolmap_full = RuntimeError("[database_sqlite.cc:1350] SQLite error: database or disk is full")

    def export_full(*_args, **_kwargs):
        raise pycolmap_full

    monkeypatch.setattr(native_match, "export_database", export_full)
    with pytest.raises(OSError) as full:
        ch.match_photos(quality="high", engine="native", reset=False)
    assert full.value.errno == errno.ENOSPC and full.value.__cause__ is pycolmap_full
    assert classic_runs == [] and dict(ch.meta["engine"]["match_photos"]) == kept
    assert [p.read_bytes() for p in watched] == before and _leftovers(work) == []

    def export_sqlite_full(*_args, **_kwargs):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(native_match, "export_database", export_sqlite_full)
    with pytest.raises(OSError) as full:
        ch.match_photos(quality="high", engine="classic", reset=False)
    assert full.value.errno == errno.ENOSPC
    assert [p.read_bytes() for p in watched] == before and _leftovers(work) == []
    monkeypatch.undo()
    ch.match_photos(quality="high", engine="classic", reset=False)
    info = ch.meta["sfm"]
    assert info["continued"] and sorted(info["added"]) == [p.name for p in photos[24:]]


def test_disk_full_is_told_from_the_errors_of_every_layer():
    """What a full disk raises, through each layer, is a full disk; other errors are not."""
    import errno
    import sqlite3

    from pyarchinit_photogrammetry.engine import native_match

    assert native_match.disk_full(OSError(errno.ENOSPC, "No space left on device"))
    assert native_match.disk_full(sqlite3.OperationalError("database or disk is full"))
    assert native_match.disk_full(RuntimeError("[database_sqlite.cc:1350] SQLite error: database or disk is full"))
    assert native_match.disk_full(RuntimeError("x.ppgf: No space left on device (os error 28)"))
    assert not native_match.disk_full(OSError(errno.EACCES, "Permission denied"))
    assert not native_match.disk_full(sqlite3.OperationalError("no such table: images"))
    assert not native_match.disk_full(RuntimeError("boom"))
    assert not native_match.disk_full(ValueError("database or disk is full"))


def test_the_moves_into_place_are_undone_or_swept_whichever_fails(scene_dir, tmp_path, monkeypatch):
    """A native match of 8 photos, then a fresh one whose third move (the database into place) fails: the stores
    are moved back and the project is as it was. Again with the undo of the stores failing as well: the sweep of
    the step (:func:`_remove_leftovers`) puts the old stores back, since the database was not replaced."""
    import os
    from pathlib import Path

    from pyarchinit_photogrammetry.engine import native_match

    _, ch = _project(scene_dir, tmp_path, photos=_photos(scene_dir)[:8])
    ch.match_photos(quality="high", engine="native")
    work = ch.data_dir / "sfm"
    stores = work / native_match.STORES
    watched = [work / "database.db", stores / "features" / "index.json", stores / "matches" / "index.json"]
    before = [p.read_bytes() for p in watched]
    real_replace = os.replace

    def failing(also_undo: bool):
        def replace(src, dst, *args, **kwargs):
            if Path(src).name == native_match.TMP_DB:
                raise PermissionError("the database cannot be replaced")
            if also_undo and Path(src) == stores and Path(dst).name == native_match.TMP:
                raise PermissionError("the stores cannot be moved back")
            return real_replace(src, dst, *args, **kwargs)
        return replace

    for also_undo in (False, True):
        monkeypatch.setattr(native_match.os, "replace", failing(also_undo))
        with pytest.raises(PermissionError):
            native_match.match_photos(ch, quality="high")
        monkeypatch.undo()
        assert [p.read_bytes() for p in watched] == before, also_undo
        assert _leftovers(work) == [] and not (stores / native_match.COMMIT).exists(), also_undo


def _stores_like(folder, tag: str):
    (folder / "features").mkdir(parents=True)
    (folder / "matches").mkdir()
    (folder / "features" / "index.json").write_text(tag)
    (folder / "matches" / "index.json").write_text(tag)


def test_the_sweep_tells_a_replaced_database_from_one_that_was_not(tmp_path):
    """The states a crash can leave between the moves of the stores and of the database: the stores being
    replaced beside no stores (put back); beside new stores whose commit record names the database in place (the
    database was replaced: the old ones go) or another (it was not: the new ones go, the old come back); beside
    new stores without a record (no telling: both go, and the next step does every photo again)."""
    from pyarchinit_photogrammetry.engine import native_match

    def state(name):
        work = tmp_path / name
        work.mkdir()
        (work / "database.db").write_bytes(b"the database in place")
        _stores_like(work / native_match.OLD, "old")
        return work, work / native_match.STORES

    def tag(stores):
        return (stores / "features" / "index.json").read_text()

    work, stores = state("no new stores")
    native_match._remove_leftovers(work)
    assert tag(stores) == "old" and _leftovers(work) == []

    work, stores = state("database replaced")
    _stores_like(stores, "new")
    native_match._write_commit(stores, work / "database.db")
    native_match._remove_leftovers(work)
    assert tag(stores) == "new" and _leftovers(work) == [] and not (stores / native_match.COMMIT).exists()

    work, stores = state("database not replaced")
    _stores_like(stores, "new")
    (work / "other.db").write_bytes(b"the database that was to come")
    native_match._write_commit(stores, work / "other.db")
    native_match._remove_leftovers(work)
    assert tag(stores) == "old" and _leftovers(work) == [] and not (stores / native_match.COMMIT).exists()

    work, stores = state("no record")
    _stores_like(stores, "new")
    native_match._remove_leftovers(work)
    assert not stores.exists() and _leftovers(work) == []


def test_a_continuation_reports_forward_and_its_export_can_be_cancelled(scene_dir, tmp_path):
    """A native match with the generic preselection, an alignment, 4 added and ``reset=False``: the progress never
    goes back (the points of the 4 and the small images of the 24 on two spans), the export counts its pairs, and a
    cancel during the export leaves the block as it was."""
    from pyarchinit_photogrammetry.core.progress import Progress
    from pyarchinit_photogrammetry.engine import native_match

    ch, work, photos = _native_block(scene_dir, tmp_path, generic=True)
    stores = work / native_match.STORES
    watched = [work / "database.db", stores / "features" / "index.json", stores / "matches" / "index.json"]
    before = [p.read_bytes() for p in watched]
    progress = Progress()

    def watch(_fraction, message):
        if message.startswith(native_match.PROGRESS_EXPORT + ":"):
            progress.cancel()

    progress.callback = watch
    with pytest.raises(Cancelled):
        ch.match_photos(quality="high", engine="native", reset=False, generic=True, progress=progress)
    assert [p.read_bytes() for p in watched] == before and _leftovers(work) == []
    seen = []
    ch.match_photos(quality="high", engine="native", reset=False, generic=True,
                    progress=lambda f, m: seen.append((f, m)))
    fractions = [f for f, _ in seen]
    assert fractions == sorted(fractions), [(f, m) for f, m in seen]
    assert any(m.startswith("Reducing photos for the pairs") for _, m in seen)
    assert any(m.startswith(native_match.PROGRESS_EXPORT + ":") and "pairs" in m for _, m in seen)
    assert ch.meta["sfm"]["continued"]


def test_a_tiny_photo_is_logged_as_too_small_not_unreadable(scene_dir, tmp_path, caplog):
    """A photo the kernels refuse for its size (7 x 5 pixels, not reduced at the quality ``highest``) is said to be
    too small, not unreadable: OpenCV reads it, the kernels refuse it."""
    import cv2

    tiny = tmp_path / "tiny.png"
    cv2.imwrite(str(tiny), np.full((5, 7), 128, np.uint8))
    (tmp_path / "project").mkdir()
    _, ch = _project(scene_dir, tmp_path / "project", photos=_photos(scene_dir)[:6] + [tiny])
    with caplog.at_level("INFO"):
        ch.match_photos(quality="highest", engine="native")
    said = [r.getMessage() for r in caplog.records if "tiny.png" in r.getMessage() and r.levelname == "WARNING"]
    assert said and all("too small" in m and "unreadable" not in m for m in said), said
    assert ch.meta["engine"]["match_photos"]["used"] == "native"


def test_the_classic_step_sweeps_what_a_native_step_left(scene_dir, tmp_path):
    """The temporary folders and databases a native step that died left in the sfm folder go when the classic step
    matches every photo again."""
    from pyarchinit_photogrammetry.engine import native_match

    _, ch = _project(scene_dir, tmp_path, photos=_photos(scene_dir)[:6])
    work = ch.data_dir / "sfm"
    work.mkdir(parents=True, exist_ok=True)
    for folder in (native_match.TMP, native_match.OLD):
        _stores_like(work / folder, "left")
    for name in (native_match.TMP_DB, native_match.TMP_DB + "-wal", "database.db.tmp"):
        (work / name).write_bytes(b"left")
    ch.match_photos(quality="high", engine="classic")
    assert _leftovers(work) == [] and not (work / native_match.STORES).exists()


def test_a_lost_gpu_without_the_cpu_share_leaves_its_pairs_to_the_cpu(scene_dir, tmp_path, monkeypatch,
                                                                     gpu_test_device, caplog):
    """``PPG_NATIVE_CPU_SHARE=0`` leaves the pairs to the GPU; when it is lost in the middle of the matching the CPU
    standing by takes the pairs left, so the step ends natively with the matches of ``gpu="off"``."""
    import os

    from pyarchinit_photogrammetry.engine import native_match

    gpu = "auto" if gpu_test_device == -1 else str(gpu_test_device)
    monkeypatch.setenv("PPG_NATIVE_CPU_SHARE", "0")
    monkeypatch.setenv("PPG_TEST_GPU_FAIL_AFTER", "1")

    def progress(_fraction, message):
        if message.startswith(native_match.PROGRESS_PAIRS + " ("):
            os.environ["PPG_TEST_GPU_FAIL_AFTER"] = "3"

    photos = _photos(scene_dir)[:8]
    (tmp_path / "gpu").mkdir()
    (tmp_path / "cpu").mkdir()
    _, with_gpu = _project(scene_dir, tmp_path / "gpu", photos=photos)
    with caplog.at_level("INFO"):
        with_gpu.match_photos(quality="high", engine="native", gpu=gpu, progress=progress)
    monkeypatch.delenv("PPG_TEST_GPU_FAIL_AFTER")
    monkeypatch.delenv("PPG_NATIVE_CPU_SHARE")
    assert with_gpu.meta["engine"]["match_photos"]["used"] == "native"
    _, cpu_only = _project(scene_dir, tmp_path / "cpu", photos=photos)
    cpu_only.match_photos(quality="high", engine="native", gpu="off")
    info = with_gpu.meta["sfm"]
    assert info["gpu_pairs"] == 2 and info["cpu_pairs"] == info["pairs"] - 2
    # the GPU named as the classic record names it: the adapter's name
    named = _native.kernel("gpu_device_name")(_native.gpu_devices(gpu)[0])
    assert info["gpu"] == named and info["gpu_shares"] == {named: 2}
    assert (_geometries(with_gpu.data_dir / "sfm" / "database.db")
            == _geometries(cpu_only.data_dir / "sfm" / "database.db"))
    said = [r.getMessage() for r in caplog.records]
    assert any("the CPU takes them" in m for m in said), said


def test_the_windows_codes_of_a_full_disk_count_only_on_windows(monkeypatch):
    """``(os error 39)`` and ``(os error 112)`` are a full disk on Windows only: elsewhere 39 and 112 are other errors
    (ENOTEMPTY, EHOSTDOWN on Linux), which fall back instead of stopping both engines."""
    from pyarchinit_photogrammetry.engine import native_match

    for code in (39, 112):
        error = RuntimeError(f"x: Directory not empty (os error {code})")
        monkeypatch.setattr(native_match.sys, "platform", "linux")
        assert not native_match.disk_full(error), code
        monkeypatch.setattr(native_match.sys, "platform", "win32")
        assert native_match.disk_full(error), code
    monkeypatch.setattr(native_match.sys, "platform", "linux")
    assert native_match.disk_full(RuntimeError("x: No space left on device (os error 28)"))


def test_a_classic_photo_without_features_does_not_stop_the_import(tmp_path):
    """A classic database where SIFT found nothing on one photo (0-row key points and descriptors) is still one to
    import; one with key points but no descriptors (a native export) is not."""
    import sqlite3

    from pyarchinit_photogrammetry.engine import native_match

    db = tmp_path / "database.db"
    con = sqlite3.connect(str(db))
    con.executescript("CREATE TABLE images (image_id INTEGER, name TEXT, camera_id INTEGER);"
                      "CREATE TABLE keypoints (image_id INTEGER, rows INTEGER, cols INTEGER, data BLOB);"
                      "CREATE TABLE descriptors (image_id INTEGER, rows INTEGER, cols INTEGER, data BLOB);")
    con.executemany("INSERT INTO images VALUES (?, ?, 1)", [(1, "a.jpg"), (2, "sky.jpg")])
    con.executemany("INSERT INTO keypoints VALUES (?, ?, 4, ?)", [(1, 2, b"x" * 32), (2, 0, b"")])
    con.executemany("INSERT INTO descriptors VALUES (?, ?, 128, ?)", [(1, 2, b"d" * 256), (2, 0, b"")])
    con.commit()
    assert native_match._photos_without_descriptors(db) == 0
    con.execute("DELETE FROM descriptors WHERE image_id = 1")
    con.commit()
    con.close()
    assert native_match._photos_without_descriptors(db) == 1


def test_the_commit_record_survives_a_copy_that_keeps_the_times(tmp_path):
    """The record of a commit names the database by its size and time of change, not by its inode: a copy of the
    project that keeps the times (a new inode) is still judged replaced."""
    import shutil

    from pyarchinit_photogrammetry.engine import native_match

    work = tmp_path / "work"
    stores = work / native_match.STORES
    stores.mkdir(parents=True)
    (work / "database.db").write_bytes(b"the database in place")
    native_match._write_commit(stores, work / "database.db")
    copy = tmp_path / "copy"
    shutil.copytree(work, copy, copy_function=shutil.copy2)
    assert native_match._committed(copy / native_match.STORES, copy / "database.db") is True
    (copy / "database.db").write_bytes(b"another database of another size")
    assert native_match._committed(copy / native_match.STORES, copy / "database.db") is False


def test_the_verified_pairs_of_two_databases_compare_whatever_their_image_ids(tmp_path):
    """COLMAP's CPU extractor numbers the photos in the order its threads finish them, the native export in the order
    of their names: a pair is keyed by its two names whatever ids, and so whatever order, its photos have."""
    import sqlite3

    from pyarchinit_photogrammetry.engine import sfm

    def database(path, ids):
        con = sqlite3.connect(str(path))
        con.executescript("CREATE TABLE images (image_id INTEGER, name TEXT);"
                          "CREATE TABLE two_view_geometries (pair_id INTEGER, rows INTEGER, config INTEGER);")
        con.executemany("INSERT INTO images VALUES (?, ?)", [(i, n) for n, i in ids.items()])
        a, b = sorted((ids["IMG_0002.jpg"], ids["IMG_0003.jpg"]))
        con.execute("INSERT INTO two_view_geometries VALUES (?, 40, 2)", (a * sfm.COLMAP_MAX_IMAGES + b,))
        con.commit()
        con.close()
        return path

    by_name = database(tmp_path / "native.db", {"IMG_0002.jpg": 1, "IMG_0003.jpg": 2})
    by_thread = database(tmp_path / "classic.db", {"IMG_0003.jpg": 1, "IMG_0002.jpg": 2})
    assert _verified_pairs(by_name) == _verified_pairs(by_thread) == {("IMG_0002.jpg", "IMG_0003.jpg"): (2, 40)}
