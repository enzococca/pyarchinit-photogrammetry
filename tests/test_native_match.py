"""The bindings of the native matching (``ppg_core`` API 13, ``_native.native_match_kernels()``): the
features step into a feature store, the preselection on the small store, the two-stage matching of
the pairs into a match store, the stores read and written from Python, and the photos read and
reduced as COLMAP reads them. Every binding releases the GIL and reports through ``progress(done,
total, message)``, whose exception stops the step. Skipped without a core that carries them."""
from __future__ import annotations

import numpy as np
import pytest

from pyarchinit_photogrammetry import _native
from pyarchinit_photogrammetry.core.progress import Cancelled

ppg_core = pytest.importorskip("ppg_core")
pytestmark = pytest.mark.skipif(_native.native_match_kernels() is None,
                                reason="the native core with the native matching (API 13) is not installed")

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


def test_the_kernels_are_declared_at_api_13(monkeypatch):
    """``_native`` names the native matching as a feature of API 13 and gives its bindings as a namespace, None
    when the extension is switched off."""
    assert _native.NATIVE_MATCH_API == 13 and _native.api() >= 13
    assert _native.has("native_match") and _native.supports("native_match")
    k = _native.native_match_kernels()
    assert k.features is ppg_core.native_features and k.match_pairs is ppg_core.native_match_pairs
    assert k.preselect is ppg_core.native_preselect and k.features_pixels is ppg_core.native_features_pixels
    assert k.store_read is ppg_core.native_store_read and k.store_write is ppg_core.native_store_write
    assert k.match_read is ppg_core.native_match_read and k.match_pairs_list is ppg_core.native_match_pairs_list
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
    ends: fewer calls than photos, the first ``(0, n)``, the last ``(n, n)``, half a second at least between two
    calls but the last, ``done`` never falling. A callback that sleeps makes every call slow, so a step that
    called it once per photo would show in the count and in the time."""
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
    assert 2 <= len(calls) <= elapsed / 0.5 + 2 < PHOTOS + 1, (len(calls), elapsed)
    gaps = [b[0] - a[0] for a, b in zip(calls, calls[1:-1])]  # between the calls, the last one excepted
    assert all(gap >= 0.45 for gap in gaps), gaps
    done = [d for _, d, _ in calls]
    assert done == sorted(done)
    with pytest.raises(ValueError, match="min_interval"):
        ppg_core.native_features(str(tmp_path / "g"), None, [], MAX_FEATURES, SMALL_POINTS, [], True, True, 2, None,
                                 min_interval=-1.0)


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
