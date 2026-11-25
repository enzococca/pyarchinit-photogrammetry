"""The native mapper of ``ppg_core`` (``NativeMapper``, milestone 3) seen from Python.

Every test here is skipped only when ``_native.native_mapper()`` gives no class (a core without the
mapper, API 16, or ``PPG_NATIVE=0``): the stages on a synthetic strip, the progress callback that
raises, the arrays refused, the thresholds taken from pycolmap and the model the mapper writes in
COLMAP's binary format, read by pycolmap and written again by it byte for byte.
"""

from __future__ import annotations

import numpy as np
import pytest
from ring_helpers import reference_rendering

from pyarchinit_photogrammetry import _native


def _mapper_class():
    get = getattr(_native, "native_mapper", None)
    return get() if get is not None else None


pytestmark = pytest.mark.skipif(_mapper_class() is None, reason="ppg_core has no NativeMapper (API 16)")

FOCAL, WIDTH, HEIGHT = 1000.0, 2000, 1500


def _rotation(roll: float, pitch: float, yaw: float) -> np.ndarray:
    """``cam_from_world`` of a photo looking down, turned by small angles (radians)."""
    cr, sr, cp, sp, cy, sy = np.cos(roll), np.sin(roll), np.cos(pitch), np.sin(pitch), np.cos(yaw), np.sin(yaw)
    rx = np.array([[1, 0, 0], [0, cr, -sr], [0, sr, cr]])
    ry = np.array([[cp, 0, sp], [0, 1, 0], [-sp, 0, cp]])
    rz = np.array([[cy, -sy, 0], [sy, cy, 0], [0, 0, 1]])
    down = np.diag([1.0, -1.0, -1.0])
    return rz @ ry @ rx @ down


def strip_arrays(n: int = 12, seed: int = 1) -> dict:
    """The arrays of ``NativeMapper`` for a strip of ``n`` nadir photos 5 m apart 20 m over a ground
    with relief: the key points are the projections of the points each photo sees (with 0.3 px of
    noise), the matches of every pair that shares 30 points or more are its true ones."""
    rng = np.random.default_rng(seed)
    centres = np.array([[5.0 * i, rng.uniform(-1, 1), 20.0 + rng.uniform(-1, 1)] for i in range(n)])
    rotations = [_rotation(*np.radians(rng.uniform(-5, 5, 3))) for _ in range(n)]
    points = np.column_stack([rng.uniform(-22, 5.0 * (n - 1) + 22, 300 * n), rng.uniform(-16, 16, 300 * n),
                              rng.uniform(-3, 3, 300 * n)])
    keypoints, offsets, seen = [], [0], []
    for r, c in zip(rotations, centres):
        x = (points - c) @ r.T
        px = FOCAL * x[:, :2] / x[:, 2:3] + np.array([WIDTH / 2, HEIGHT / 2])
        inside = (x[:, 2] > 0) & (px[:, 0] > 1) & (px[:, 0] < WIDTH - 1) & (px[:, 1] > 1) & (px[:, 1] < HEIGHT - 1)
        ids = np.flatnonzero(inside)
        keypoints.append(px[ids] + rng.normal(0, 0.3, (len(ids), 2)))
        offsets.append(offsets[-1] + len(ids))
        seen.append({int(p): k for k, p in enumerate(ids)})
    pairs, matches, match_offsets = [], [], [0]
    for a in range(n):
        for b in range(a + 1, n):
            common = sorted(set(seen[a]) & set(seen[b]))
            if len(common) < 30:
                continue
            pairs.append((a, b))
            matches.extend((seen[a][p], seen[b][p]) for p in common)
            match_offsets.append(len(matches))
    return {
        "images": [(f"photo_{i:03d}.jpg", i + 1, 0) for i in range(n)],
        "cameras": [(1, "SIMPLE_PINHOLE", [FOCAL, WIDTH / 2, HEIGHT / 2], WIDTH, HEIGHT, True)],
        "keypoints": np.vstack(keypoints).astype(np.float32),
        "keypoint_offsets": np.array(offsets, dtype=np.int64),
        "pairs": np.array(pairs, dtype=np.int64),
        "pair_config": np.full(len(pairs), 2, dtype=np.int32),
        "pair_f": np.full((len(pairs), 9), np.nan),
        "matches": np.array(matches, dtype=np.uint32),
        "match_offsets": np.array(match_offsets, dtype=np.int64),
        "options": {},
    }


def test_the_model_is_colmaps(tmp_path):
    pycolmap = pytest.importorskip("pycolmap")
    mapper = _mapper_class()(**strip_arrays())
    mapper.view_graph(None)
    mapper.rotations(None)
    mapper.tracks(500, None)
    mapper.position(None, None)
    mapper.refine(None)
    rust, again = tmp_path / "rust", tmp_path / "pycolmap"
    rust.mkdir()
    again.mkdir()
    assert mapper.write_model(str(rust)) == 12
    pycolmap.Reconstruction(str(rust)).write(str(again))
    names = sorted(p.name for p in rust.iterdir())
    assert names == ["cameras.bin", "frames.bin", "images.bin", "points3D.bin", "rigs.bin"]
    assert sorted(p.name for p in again.iterdir()) == names
    for name in names:
        assert (rust / name).read_bytes() == (again / name).read_bytes(), name


def _run(mapper, gnss=None) -> dict:
    """Every stage of ``mapper`` in its order, the dict of each by its name."""
    return {
        "view_graph": mapper.view_graph(None),
        "rotations": mapper.rotations(None),
        "tracks": mapper.tracks(500, None),
        "position": mapper.position(gnss, None),
        "refine": mapper.refine(None),
    }


def test_the_stages_run_on_a_small_synthetic_input():
    arrays = strip_arrays()
    mapper = _mapper_class()(**arrays)
    out = _run(mapper)
    assert {"edges", "left_out", "focals"} <= set(out["view_graph"])
    assert out["view_graph"]["edges"] == len(arrays["pairs"])
    assert out["view_graph"]["focals"] == []  # the camera has a prior focal length: nothing calibrated
    assert {"registered", "dropped", "components"} <= set(out["rotations"])
    assert out["rotations"]["registered"] == 12 and out["rotations"]["components"] == [12]
    assert {"tracks", "selected", "observations", "dropped_conflicts"} <= set(out["tracks"])
    assert 0 < out["tracks"]["selected"] <= out["tracks"]["tracks"] < out["tracks"]["observations"]
    position = out["position"]
    assert {"centres", "frame", "frame_residual_deg", "sigma_h", "sigma_v", "bias", "bias_sd",
            "gnss_outliers"} <= set(position)
    assert position["frame"] == "free" and position["gnss_outliers"] == []
    centres = position["centres"]
    assert centres.shape == (12, 3) and centres.dtype == np.float64 and np.isfinite(centres).all()
    rounds = out["refine"]["rounds"]
    assert len(rounds) >= 1
    assert {"round", "intrinsics", "cost_initial", "cost", "observations", "filtered", "added",
            "seconds"} <= set(rounds[0])
    report = mapper.report()
    assert report["placed"] == 12 and report["photos"] == 12 and report["points"] > 0
    assert report["frame"] == "free" and len(report["rounds"]) == len(rounds)
    assert report["median_reprojection_px"] < 1.0
    # the same arrays give the same block, bit for bit
    again = _mapper_class()(**strip_arrays())
    assert np.array_equal(_run(again)["position"]["centres"], centres)


def test_the_positions_of_the_photos_hold_the_block():
    """With the positions of the photos (``gnss``) the block is in their frame: the centres the
    positioning gives are near them, the bias of the one flight is reported."""
    arrays = strip_arrays()
    mapper = _mapper_class()(**arrays)
    mapper.view_graph(None)
    mapper.rotations(None)
    mapper.tracks(500, None)
    rng = np.random.default_rng(3)
    truth = np.array([[5.0 * i, 0.0, 20.0] for i in range(12)])
    gnss = {"image": np.arange(12, dtype=np.int64), "xyz": truth + rng.normal(0, 0.02, (12, 3)),
            "sigma": np.full((12, 3), 0.05), "flight": np.ones(12, dtype=np.int64)}
    position = mapper.position(gnss, None)
    assert position["frame"] in ("gnss", "similarity")
    assert position["sigma_h"] == pytest.approx(0.05) and position["sigma_v"] == pytest.approx(0.05)
    assert np.nanmax(np.abs(position["centres"][:, 0] - truth[:, 0])) < 1.5
    mapper.refine(None)
    report = mapper.report()
    assert report["placed"] == 12 and report["frame"] == position["frame"]
    assert isinstance(report["bias"], dict) and isinstance(report["bias_sd"], dict)


def test_progress_that_raises_stops_the_mapper(tmp_path):
    from pyarchinit_photogrammetry.core.progress import Cancelled

    mapper = _mapper_class()(**strip_arrays())
    seen = []
    mapper.view_graph(lambda done, total, message: seen.append((done, total, message)))
    # at once when the stage begins, then every quarter of a second while it runs, and when it has ended
    assert seen[0] == (0, 5, "view graph") and seen[-1] == (1, 5, "view graph")
    mapper.rotations(None)
    mapper.tracks(500, None)
    calls = []

    def progress(done, total, message):
        calls.append(message)
        if len(calls) == 2:
            raise Cancelled("stopped by the user")

    with pytest.raises(Cancelled) as raised:
        mapper.position(None, progress)
    assert type(raised.value) is Cancelled and str(raised.value) == "stopped by the user"
    assert len(calls) == 2  # not called again after it raised
    with pytest.raises(RuntimeError) as refused:
        mapper.refine(None)
    assert not isinstance(refused.value, Cancelled) and "stopped" in str(refused.value)
    with pytest.raises(RuntimeError):
        mapper.write_model(str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_bad_arrays_are_refused():
    cls = _mapper_class()
    arrays = strip_arrays()
    offsets = arrays["match_offsets"].copy()
    offsets[3], offsets[4] = offsets[4], offsets[3]
    with pytest.raises(RuntimeError, match="match_offsets"):
        cls(**{**arrays, "match_offsets": offsets})
    pairs = arrays["pairs"].copy()
    pairs[2, 1] = 99
    with pytest.raises(RuntimeError, match="pair 2: image 99"):
        cls(**{**arrays, "pairs": pairs})
    with pytest.raises(RuntimeError, match="keypoints"):
        cls(**{**arrays, "keypoints": arrays["keypoints"].astype(np.float64)})
    with pytest.raises(RuntimeError, match="keypoint_offsets"):
        cls(**{**arrays, "keypoint_offsets": arrays["keypoint_offsets"][:-1]})
    with pytest.raises(RuntimeError, match="pair_f"):
        cls(**{**arrays, "pair_f": arrays["pair_f"][:, :8]})
    # a mapper made of good arrays still runs after those
    assert cls(**arrays).view_graph(None)["edges"] > 0


def test_arrays_not_contiguous_are_read():
    """A view with strides (a column of a wider array) is taken as well as a packed array."""
    arrays = strip_arrays()
    wide = np.zeros((len(arrays["keypoints"]), 4), dtype=np.float32)
    wide[:, ::2] = arrays["keypoints"]
    strided = wide[:, ::2]
    assert not strided.flags.c_contiguous
    a = _mapper_class()(**{**arrays, "keypoints": strided})
    b = _mapper_class()(**arrays)
    assert a.view_graph(None) == b.view_graph(None)


def test_the_filter_thresholds_are_colmaps():
    pycolmap = pytest.importorskip("pycolmap")
    import ppg_core

    defaults = ppg_core.native_mapper_defaults()
    mapper = pycolmap.GlobalPipelineOptions().mapper
    assert defaults["max_normalized_reprojection"] == mapper.max_normalized_reproj_error
    assert defaults["min_triangulation_deg"] == mapper.min_tri_angle_deg
    # the filter of the last adjustment is that of COLMAP's retriangulation (its incremental mapper's)
    retriangulation = pycolmap.IncrementalPipelineOptions().mapper
    assert defaults["final_max_reprojection_px"] == retriangulation.filter_max_reproj_error
    assert defaults["final_min_triangulation_deg"] == retriangulation.filter_min_tri_angle


def test_the_options_are_checked():
    import ppg_core

    defaults = ppg_core.native_mapper_defaults()
    assert defaults["rounds"] == 3 and defaults["opencv_sensor"] == []
    arrays = strip_arrays()
    with pytest.raises(ValueError, match="no_such_option"):
        _mapper_class()(**{**arrays, "options": {"no_such_option": 1}})
    fewer = _mapper_class()(**{**arrays, "options": {"rounds": 1, "min_inliers": 20}})
    _run(fewer)
    usual = _mapper_class()(**arrays)
    _run(usual)
    assert 1 <= len(fewer.report()["rounds"]) < len(usual.report()["rounds"])


def test_the_switch_and_the_api(monkeypatch):
    import ppg_core

    assert _native.MAPPER_API == 16 and _native.api() >= 16
    assert _native.FEATURES["native_mapper"] == "NativeMapper" and _native.has("native_mapper")
    assert _mapper_class() is ppg_core.NativeMapper
    monkeypatch.setenv("PPG_NATIVE", "0")
    assert _native.native_mapper() is None
    monkeypatch.delenv("PPG_NATIVE")
    monkeypatch.setattr(_native, "api", lambda: 15)
    assert _native.native_mapper() is None


def test_the_callback_may_read_the_report_but_not_run_the_mapper():
    """Inside a stage, ``report()`` answers (what the stages ended so far said) and does not stop the
    stage; any other method is a RuntimeError that says the callback must not call the mapper, and
    that exception, raised by the callback, stops the mapper as any other."""
    mapper = _mapper_class()(**strip_arrays())
    reports = []
    mapper.view_graph(lambda *_: reports.append(mapper.report()))
    assert reports and reports[0]["photos"] == 12 and reports[0]["edges"] == 0  # before the view graph ended
    assert mapper.report()["edges"] > 0
    rotations = mapper.rotations(lambda *_: reports.append(mapper.report()))
    assert rotations["registered"] == 12 and reports[-1]["rotations_placed"] == 12

    def runs_a_stage(*_):
        mapper.tracks(500, None)

    with pytest.raises(RuntimeError, match="progress callback must not call the mapper") as raised:
        mapper.tracks(500, runs_a_stage)
    assert "report()" in str(raised.value)
    with pytest.raises(RuntimeError, match="stopped"):
        mapper.tracks(500, None)


def test_items_and_options_of_the_wrong_kind_are_refused():
    cls = _mapper_class()
    arrays = strip_arrays()
    images = list(arrays["images"])
    images[4] = ("photo_004.jpg", -5, 0)
    with pytest.raises(RuntimeError, match=r"images\[4\]"):
        cls(**{**arrays, "images": images})
    images[4] = ("photo_004.jpg", 5, -1)
    with pytest.raises(RuntimeError, match=r"images\[4\]"):
        cls(**{**arrays, "images": images})
    camera = list(arrays["cameras"][0])
    camera[0] = -1
    with pytest.raises(RuntimeError, match=r"cameras\[0\]"):
        cls(**{**arrays, "cameras": [tuple(camera)]})
    with pytest.raises(RuntimeError, match=r"cameras\[0\]"):
        cls(**{**arrays, "cameras": [("SIMPLE_PINHOLE",)]})
    for key, value in (("rounds", "three"), ("rounds", -1), ("max_rotation_error_deg", "wide"),
                       ("opencv_sensor", 1)):
        with pytest.raises(RuntimeError, match=f"option {key}"):
            cls(**{**arrays, "options": {key: value}})
    mapper = cls(**arrays)
    mapper.view_graph(None)
    mapper.rotations(None)
    with pytest.raises(RuntimeError, match="per_photo"):
        mapper.tracks(-1, None)
    assert mapper.tracks(500, None)["selected"] > 0  # a refusal of the arguments does not stop the mapper


# ------------------------------------------------------------------- the native mapper behind align_cameras
# The reduced weak ring of these tests (Task 1 measured that 48 photos of 600 x 450 with a focal length of 500 px
# align; 24 photos, or 400 x 300 with 1000 px, do not overlap enough)
RING_PHOTOS, RING_SIZE, RING_FOCAL = 48, (600, 450), 500.0
# the accuracy of the GPS of the ring (its noise, write_weak_ring_dataset's gps_noise): the default of a position
# without one, 5 m, spreads more than a tenth of the ring's 60 m, and the positions then cannot orient the block (the
# mapper puts the block of the rays on them by a similarity: frame "similarity")
RING_ACCURACY = [0.3, 0.3, 0.5]


def _with_accuracy(ch, accuracy=RING_ACCURACY):
    for cam in ch.cameras:
        cam.reference.accuracy = list(accuracy)
    return ch


def _matched_project(folder, photos, name: str, **match) -> "Path":
    """A project ``folder/name.ppg`` of a copy of ``photos`` (its folder ``folder/photos``), its targets detected
    and its photos matched (``match``: the arguments of ``match_photos``); the path of the project."""
    import shutil

    import pyarchinit_photogrammetry as ppg

    shutil.copytree(photos, folder / "photos")
    doc = ppg.Document()
    ch = doc.add_chunk(name)
    doc.save(folder / f"{name}.ppg")
    ch.add_photos([folder / "photos"])
    ch.detect_markers("aruco_4x4_50")
    ch.match_photos(**match)
    doc.save()
    return folder / f"{name}.ppg"


def _copy(project, tmp_path, name: str = "copy"):
    """The matched project ``project`` copied into ``tmp_path/name`` (the project, its files, its photos) and
    opened."""
    import shutil

    import pyarchinit_photogrammetry as ppg

    d = tmp_path / name
    d.mkdir()
    shutil.copy2(project, d / project.name)
    shutil.copytree(project.with_name(project.name + ".files"), d / (project.name + ".files"))
    shutil.copytree(project.parent / "photos", d / "photos")
    return ppg.Document.open(d / project.name)


@pytest.fixture(scope="module")
def matched_trench(scene_dir, tmp_path_factory):
    """The trench of the session, its photos matched once (the engine ``auto`` gives); each test aligns a copy."""
    return _matched_project(tmp_path_factory.mktemp("native_trench"), scene_dir / "photos", "trench", quality="high")


@pytest.fixture(scope="module")
def matched_ring(tmp_path_factory):
    """The reduced weak ring (:data:`RING_PHOTOS` photos, a GPS position in the EXIF of each), its photos matched
    once; ``(project, scene)``. Each test aligns a copy."""
    from pyarchinit_photogrammetry.testing.synthetic import write_weak_ring_dataset

    root = tmp_path_factory.mktemp("native_ring")
    scene = write_weak_ring_dataset(root / "set", n=RING_PHOTOS, size=RING_SIZE, focal=RING_FOCAL)
    (root / "work").mkdir()
    return _matched_project(root / "work", root / "set" / "photos", "ring", quality="highest"), scene


class _Spy:
    """A stand-in for ``ppg_core.NativeMapper`` (a frozen class: its methods cannot be patched) that runs the real
    mapper and counts the calls of each method; ``fail`` names a method that raises ``RuntimeError("boom")``."""

    calls: dict = {}
    fail: str | None = None
    options: dict = {}  # the options of the last mapper built
    fail_report: bool = False  # report() raises too
    real: type | None = None  # the class of the core

    def __init__(self, *args, **kwargs):
        _Spy.options = dict(kwargs.get("options") or {})
        self._real = _Spy.real(*args, **kwargs)

    def __getattr__(self, name):
        method = getattr(self._real, name)

        def call(*args, **kwargs):
            _Spy.calls[name] = _Spy.calls.get(name, 0) + 1
            if name == _Spy.fail or (name == "report" and _Spy.fail_report):
                raise RuntimeError("boom" if name == _Spy.fail else "the report broke")
            return method(*args, **kwargs)

        return call


@pytest.fixture
def spy(monkeypatch):
    monkeypatch.setattr(_Spy, "real", _mapper_class())
    monkeypatch.setattr(_native, "native_mapper", lambda: _Spy)
    monkeypatch.setattr(_Spy, "calls", {})
    monkeypatch.setattr(_Spy, "fail", None)
    monkeypatch.setattr(_Spy, "fail_report", False)
    return _Spy


@pytest.mark.slow
def test_the_trench_aligned_by_the_native_mapper_keeps_the_metric_gates(processed_native, scene):
    """The trench matched and aligned by the native engine keeps the metric gates of the project (control < 2 mm,
    check < 3 mm, DEM median < 3 mm, targets in the orthomosaic < 5 mm); the record says native, the mapper worked
    in a free frame (no GPS on the photos of the trench)."""
    import rasterio

    from pyarchinit_photogrammetry.markers.targets import detect

    _doc, ch, report = processed_native
    assert ch.meta["engine"]["align_cameras"] == {"requested": "native", "used": "native", "fallback": None}
    native = ch.meta["sfm"]["native_mapper"]
    assert native["engine"] == "native" and native["position"]["frame"] == "free"
    assert ch.meta["sfm"]["mapper"] == "global"
    assert len(ch.aligned_cameras) == len(ch.cameras) == 28
    assert report["control_rmse"] < 0.002
    before = np.array(ch.transform, float)
    for label in ("target 4", "target 5"):
        ch.marker(label).checkpoint = True
    try:
        assert ch.update_transform()["check_rmse"] < 0.003
    finally:
        for m in ch.markers:
            m.checkpoint = False
        ch.update_transform()
        ch.transform = before
    with rasterio.open(ch.dem_path) as src:
        dem = src.read(1)
        rows, cols = np.nonzero(dem != src.nodata)
        sel = np.random.default_rng(1).choice(len(rows), 4000, replace=False)
        xs, ys = rasterio.transform.xy(src.transform, rows[sel], cols[sel])
    err = dem[rows[sel], cols[sel]] - scene.world_height(np.array(xs), np.array(ys))
    assert abs(np.median(err)) < 0.003
    with rasterio.open(ch.orthomosaic_path) as src:
        img = np.moveaxis(src.read([3, 2, 1]), 0, -1).copy()
        tr = src.transform
    dets = detect(img)
    assert len(dets) >= 6
    for d in dets:
        x, y = rasterio.transform.xy(tr, d["center"][1], d["center"][0])
        tx, ty, _ = scene.targets[d["code"]]
        assert np.hypot(x - tx, y - ty) < 0.005


@pytest.mark.slow
def test_the_reduced_weak_ring(matched_ring, tmp_path):
    """The reduced weak ring aligned by the native mapper: every photo placed, in the frame of the GPS of the photos,
    the stages of the mapper timed, and the ring keeps its shape (the gate of the weak ring)."""
    from pyarchinit_photogrammetry.bench import truth
    from pyarchinit_photogrammetry.testing.synthetic import WEAK_RING_GATE

    project, scene = matched_ring
    doc = _copy(project, tmp_path)
    ch = _with_accuracy(doc.chunk)
    assert ch.align_cameras(engine="native") == RING_PHOTOS
    assert ch.meta["engine"]["align_cameras"] == {"requested": "native", "used": "native", "fallback": None}
    native = ch.meta["sfm"]["native_mapper"]
    assert native["position"]["frame"] == "gnss" and native["report"]["placed"] == RING_PHOTOS
    stages = ch.meta["timings"]["align_cameras"]["stages"]
    assert {"view_graph", "rotation_averaging", "track_establishment", "global_positioning", "bundle_adjustment",
            "write"} <= set(stages)
    report = native["report"]
    shape = truth.ring_shape(doc, scene)
    reference = reference_rendering(scene)
    print(f"reduced weak ring: reference {reference}, focal sd {report['focal_sd']}, held {report['intrinsics_held']}, "
          f"shape {shape['truth_target_shape']:.5f}")
    from pyarchinit_photogrammetry.engine import bundle

    for cam, sd in report["focal_sd"]:  # on any rendering (ruling R29): held where its sd is over the line, and said
        if sd is None or sd > bundle.FOCAL_SD_HOLD:
            assert cam in report["intrinsics_held"]
    # the notes number the cameras of the mapper (from 0), the report the cameras of the model: one note each
    said = {note.split(":")[0] for note in report["notes"] if ": focal held: " in note}
    assert len(said) == len(report["intrinsics_held"]), report["notes"]
    if not reference:
        return
    assert report["intrinsics_held"] == [cam[0] for cam in report["focal_sd"]]  # the flat ring: focal held (R16)
    # the marginal sd (R18: every term the last round frees free with it; 17.6 % measured on a GPU's tie points,
    # 4.5 % on the CPU's of a Linux runner, 4.9-17.6 % over rings of this size): four times the line it is held at
    # (2 %), which is also above the conditional one with them held (1.8 %)
    assert report["focal_sd"][0][1] > 4 * bundle.FOCAL_SD_HOLD
    assert shape["truth_targets"] >= 6
    assert shape["truth_target_shape"] < WEAK_RING_GATE["shape"]


@pytest.mark.slow
def test_what_the_native_mapper_cannot_do_goes_classic(matched_trench, tmp_path, monkeypatch, spy):
    """Each request the native mapper does not take goes to the classic alignment with its reason, word for word, and
    the alignment of today places every photo; the native mapper is never built."""
    def aligned(ch, reason, **kwargs):
        assert ch.align_cameras(engine="native", **kwargs) == len(ch.cameras), reason
        assert ch.meta["engine"]["align_cameras"] == {"requested": "native", "used": "classic", "fallback": reason}
        assert "native_mapper" not in ch.meta["sfm"] and "native_fallback" not in ch.meta["sfm"]

    ch = _copy(matched_trench, tmp_path).chunk
    aligned(ch, "mapper 'incremental' asked", mapper="incremental")
    aligned(ch, "mapper 'hierarchical' asked", mapper="hierarchical")
    aligned(ch, "photos added to an alignment are registered by the classic incremental mapper", reset=False)
    ch.meta["sfm"]["rigs"] = 1
    aligned(ch, "rigs are aligned by the classic engine")
    ch.meta["sfm"]["rigs"] = 0
    (sensor,) = ch.sensors.values()
    sensor.fixed = True
    aligned(ch, "calibrations held fixed are aligned by the classic engine")
    sensor.fixed = False
    model, params = sensor.model, list(sensor.params)
    sensor.model = "OPENCV_FISHEYE"  # aligned as RADIAL_FISHEYE (sfm.mapping_camera)
    aligned(ch, "camera model RADIAL_FISHEYE is not in the native mapper")
    sensor.model, sensor.params = model, params
    monkeypatch.setenv("PPG_NATIVE", "0")
    aligned(ch, "PPG_NATIVE=0")
    monkeypatch.delenv("PPG_NATIVE")
    monkeypatch.setattr(_native, "native_mapper", lambda: None)
    aligned(ch, f"the native core with the mapper (API {_native.MAPPER_API}) is not installed")
    assert spy.calls == {}


@pytest.mark.slow
def test_a_rejected_block_goes_to_the_classic_chain(matched_ring, tmp_path, monkeypatch, spy):
    """A block whose cameras miss the GPS of the photos (patched: 99 m) is refused right after its positioning: the
    classic chain does the alignment with its own rules (global, refused by the same check, then one photo at a
    time), the record keeps the reason, and the model on disk is the classic one (the native mapper wrote nothing).
    A view graph in two parts places half of the ring: refused as well, with the count."""
    import sqlite3

    from pyarchinit_photogrammetry.engine import sfm

    project, _scene = matched_ring
    ch = _copy(project, tmp_path, "a").chunk
    monkeypatch.setattr(sfm, "fits_references", lambda *args, **kwargs: (False, 99.0))
    n = ch.align_cameras(engine="native")
    record = ch.meta["engine"]["align_cameras"]
    assert (record["requested"], record["used"]) == ("native", "classic") and "99 m" in record["fallback"]
    info = ch.meta["sfm"]
    assert info["native_fallback"] == record["fallback"]
    assert info["mapper"] == "incremental" and "99 m" in info["global_fallback"]
    assert spy.calls.get("position") == 1 and "refine" not in spy.calls and "write_model" not in spy.calls
    assert sfm.load_reconstruction(ch).num_reg_images() == n >= 0.9 * RING_PHOTOS
    monkeypatch.undo()  # the check as it is, and a fresh spy
    monkeypatch.setattr(_Spy, "real", _mapper_class())
    monkeypatch.setattr(_native, "native_mapper", lambda: _Spy)
    monkeypatch.setattr(_Spy, "calls", {})
    # the matches between the two halves of the ring taken out of a copy of its database
    ch = _copy(project, tmp_path, "b").chunk
    db = ch.data_dir / "sfm" / "database.db"
    con = sqlite3.connect(str(db))
    try:
        half = {int(i) for i, name in con.execute("SELECT image_id, name FROM images")
                if int(name.split("_")[-1].split(".")[0]) < RING_PHOTOS // 2}
        cut = [pid for (pid,) in con.execute("SELECT pair_id FROM two_view_geometries")
               if (divmod(pid, sfm.COLMAP_MAX_IMAGES)[0] in half) != (divmod(pid, sfm.COLMAP_MAX_IMAGES)[1] in half)]
        assert cut
        con.executemany("DELETE FROM two_view_geometries WHERE pair_id = ?", [(p,) for p in cut])
        con.executemany("DELETE FROM matches WHERE pair_id = ?", [(p,) for p in cut])
        con.commit()
    finally:
        con.close()
    ch.align_cameras(engine="native")
    record = ch.meta["engine"]["align_cameras"]
    assert record["used"] == "classic" and record["fallback"].startswith("placed ")
    assert f"of {RING_PHOTOS} photos" in record["fallback"]
    assert "refine" not in _Spy.calls


@pytest.mark.slow
def test_a_failure_of_the_mapper_goes_to_the_classic_chain(matched_trench, tmp_path, spy, caplog):
    """The native mapper failing half way (its adjustment raises): one WARNING line (the switch's), the classic chain
    aligns every photo, the record keeps the failure."""
    ch = _copy(matched_trench, tmp_path).chunk
    spy.fail = "refine"
    with caplog.at_level("WARNING"):
        assert ch.align_cameras(engine="native") == len(ch.cameras)
    assert ch.meta["engine"]["align_cameras"] == {"requested": "native", "used": "classic",
                                                  "fallback": "native mapper failed: boom"}
    assert spy.calls["refine"] == 1 and "write_model" not in spy.calls
    assert ch.meta["sfm"]["mapper"] in ("incremental", "global")
    if ch.meta["sfm"]["mapper"] == "incremental":  # the verdict on the native block given up is not kept
        assert "global_check" not in ch.meta["sfm"]
    warned = [r for r in caplog.records if r.levelname == "WARNING" and "native mapper failed: boom" in r.getMessage()]
    assert [r.name for r in warned] == ["pyarchinit_photogrammetry.engine.switch"]  # one WARNING: the switch's


@pytest.mark.slow
def test_cancelling_the_native_mapper_leaves_the_project_as_it_was(matched_trench, tmp_path, monkeypatch):
    """After a native alignment of the trench, a second one cancelled while its global positioning runs: the cancel
    comes out, the stage ended early (fewer beats of the progress than in the full run), no classic mapper ran, and
    the model of the first alignment and the database are the same, byte for byte."""
    import pycolmap

    from pyarchinit_photogrammetry.core.progress import Cancelled, Progress
    from pyarchinit_photogrammetry.engine import native_map, sfm

    ch = _copy(matched_trench, tmp_path).chunk
    full: list[str] = []
    ch.align_cameras(progress=lambda value, message: full.append(message), engine="native")
    assert ch.meta["engine"]["align_cameras"]["used"] == "native"
    positioning = native_map.PROGRESS_STAGES["positioning"]

    def beats(messages):
        """The calls of the progress from the start of the positioning to the start of the next stage."""
        start = messages.index(positioning)
        rest = [m for m in messages[start + 1:] if m]
        end = messages.index(rest[0], start + 1) if rest else len(messages)
        return end - start

    assert beats(full) >= 2  # the beat at its start and the one at its end at least
    sfm_dir = ch.data_dir / "sfm"
    files = [sfm_dir / "database.db", *sorted((sfm_dir / "model").iterdir())]
    before = {p: p.read_bytes() for p in files}
    import copy

    meta_before = {k: copy.deepcopy(v) for k, v in ch.meta.items() if k != "timings"}
    classic = []
    monkeypatch.setattr(sfm, "_global_mapping", lambda *a, **k: classic.append("global"))
    monkeypatch.setattr(pycolmap, "incremental_mapping", lambda *a, **k: classic.append("incremental"))
    seen: list[str] = []
    progress = Progress()

    def callback(value, message):
        seen.append(message)
        if message == positioning:
            progress.cancel()  # on the beat at the start of the stage: the stage is running

    progress.callback = callback
    with pytest.raises(Cancelled):
        ch.align_cameras(progress=progress, engine="native")
    assert classic == []
    assert positioning in seen and beats(seen) < beats(full)
    assert seen[-1] == positioning  # nothing after the cancel
    assert ch.meta["timings"]["align_cameras"]["failed"] == "Cancelled"
    assert sorted((sfm_dir / "model").iterdir()) == files[1:]
    for p, data in before.items():
        assert p.read_bytes() == data, p.name
    # the record of the project as it was too (the timings say the cancel, by design)
    assert {k: v for k, v in ch.meta.items() if k != "timings"} == meta_before


@pytest.mark.slow
def test_photos_without_gps_are_aligned(matched_ring, tmp_path):
    """The reduced ring with the GPS of five photos taken out of their references: the native mapper still places
    every photo, in the frame of the others."""
    from pyarchinit_photogrammetry.core.camera import Reference

    project, _scene = matched_ring
    ch = _with_accuracy(_copy(project, tmp_path).chunk)
    for cam in ch.cameras[::10][:5]:
        cam.reference = Reference()
    assert sum(1 for c in ch.cameras if c.reference.location) == RING_PHOTOS - 5
    assert ch.align_cameras(engine="native") == RING_PHOTOS
    assert ch.meta["engine"]["align_cameras"]["used"] == "native"
    assert ch.meta["sfm"]["native_mapper"]["position"]["frame"] == "gnss"


@pytest.mark.slow
def test_the_native_report_holds_no_names(matched_ring, tmp_path):
    """What the native mapper keeps in the project (``meta["sfm"]["native_mapper"]``) is numbers and words: no name
    of a photo, no path."""
    import json

    project, _scene = matched_ring
    doc = _copy(project, tmp_path)
    ch = doc.chunk
    assert ch.align_cameras(engine="native") == RING_PHOTOS
    text = json.dumps(ch.meta["sfm"]["native_mapper"])
    for cam in ch.cameras:
        assert cam.label not in text and str(cam.path) not in text
    for folder in (doc.path.parent, ch.data_dir, project.parent, tmp_path):
        assert str(folder) not in text and folder.name not in text
    assert "WRING" not in text and ".jpg" not in text.lower()


@pytest.mark.slow
def test_the_tie_point_limit_and_the_variables_of_the_mapper(matched_trench, tmp_path, monkeypatch, spy):
    """The tracks per photo: a quarter of an explicit ``tiepoint_limit`` (as the classic mappers take it), else of
    the limit of the matching, 500 at least; ``PPG_NATIVE_TRACKS_PER_PHOTO`` over both. The last adjustment with every
    track unless ``PPG_NATIVE_FINAL_ALL_TRACKS=0``."""
    from pyarchinit_photogrammetry.engine import native_map

    ch = _copy(matched_trench, tmp_path).chunk
    monkeypatch.delenv(native_map.TRACKS_ENV, raising=False)
    monkeypatch.delenv(native_map.FINAL_ALL_TRACKS_ENV, raising=False)
    assert ch.align_cameras(engine="native", tiepoint_limit=4000) == len(ch.cameras)
    assert ch.meta["sfm"]["native_mapper"]["tracks"]["per_photo"] == 1000
    assert spy.options["final_all_tracks"] is True
    ch.align_cameras(engine="native", tiepoint_limit=0)
    assert ch.meta["sfm"]["native_mapper"]["tracks"]["per_photo"] == 500
    ch.align_cameras(engine="native")
    limit = ch.meta["sfm"]["tiepoint_limit"]
    assert ch.meta["sfm"]["native_mapper"]["tracks"]["per_photo"] == max(500, limit // 4)
    monkeypatch.setenv(native_map.TRACKS_ENV, "700")
    monkeypatch.setenv(native_map.FINAL_ALL_TRACKS_ENV, "0")
    ch.align_cameras(engine="native", tiepoint_limit=4000)
    assert ch.meta["sfm"]["native_mapper"]["tracks"]["per_photo"] == 700
    assert spy.options["final_all_tracks"] is False
    assert native_map.tracks_per_photo(ch, 4000) == 700


@pytest.mark.slow
def test_the_database_is_read_as_the_mapper_takes_it(matched_trench, tmp_path):
    """``read_database`` keeps the verified pairs only - ``MIN_INLIERS`` inlier matches or more, a configuration other
    than UNDEFINED, DEGENERATE or WATERMARK - and the photos of ``names`` only."""
    import sqlite3

    from pyarchinit_photogrammetry.engine import native_map

    ch = _copy(matched_trench, tmp_path).chunk
    db = ch.data_dir / "sfm" / "database.db"
    names = {int(k): v for k, v in ch.meta["sfm"]["names"].items()}
    full = native_map.read_database(db, names)
    assert len(full["images"]) == len(names) and len(full["keypoint_offsets"]) == len(names) + 1
    assert full["keypoints"].dtype == np.float32 and full["keypoints"].shape[1] == 2
    assert len(full["pairs"]) == len(full["pair_config"]) == len(full["pair_f"]) == len(full["match_offsets"]) - 1
    con = sqlite3.connect(str(db))
    try:
        rows = con.execute("SELECT pair_id, rows, data FROM two_view_geometries WHERE rows >= 15 AND config NOT IN "
                           "(0, 1, 7) ORDER BY pair_id").fetchall()
        assert len(rows) == len(full["pairs"]) >= 5
        changed = [pid for pid, _r, _d in rows[:4]]
        for pid, config in zip(changed[:3], (0, 1, 7)):
            con.execute("UPDATE two_view_geometries SET config = ? WHERE pair_id = ?", (config, pid))
        pid, _r, data = rows[3]
        con.execute("UPDATE two_view_geometries SET rows = 14, data = ? WHERE pair_id = ?", (data[:14 * 8], pid))
        con.commit()
    finally:
        con.close()
    read = native_map.read_database(db, names)
    assert len(read["pairs"]) == len(full["pairs"]) - 4
    left_out = names.pop(min(names))  # a photo not in names: neither it nor its pairs
    fewer = native_map.read_database(db, names)
    assert left_out not in [image[0] for image in fewer["images"]] and len(fewer["images"]) == len(names)
    assert len(fewer["pairs"]) < len(read["pairs"])
    assert fewer["pairs"].max() < len(fewer["images"])


@pytest.mark.slow
def test_the_positions_go_to_the_mapper_from_three(matched_ring, tmp_path):
    """``gnss_input``: the positions of the photos in the mapper's order, on metric axes less a round offset, the
    default accuracy for a position without one; None (and an empty report) with fewer than three."""
    from pyarchinit_photogrammetry.core.camera import Reference
    from pyarchinit_photogrammetry.engine import native_map

    project, _scene = matched_ring
    ch = _copy(project, tmp_path).chunk
    names = [ch.meta["sfm"]["names"][str(c.key)] for c in ch.cameras]
    gnss, frame = native_map.gnss_input(ch, names)
    assert len(gnss["image"]) == RING_PHOTOS and sorted(frame) == ["crs", "offset"]
    assert all(float(v).is_integer() for v in frame["offset"])
    assert np.abs(gnss["xyz"]).max() < 100.0  # the offset taken out: the ring is 60 m across
    for cam in ch.cameras[3:]:
        cam.reference = Reference()
    ch.cameras[0].reference.accuracy = []
    gnss, frame = native_map.gnss_input(ch, names)
    assert gnss["image"].tolist() == [0, 1, 2] and gnss["sigma"][0].tolist() == native_map.DEFAULT_ACCURACY
    ch.cameras[2].reference = Reference()
    assert native_map.gnss_input(ch, names) == (None, {})


@pytest.mark.slow
def test_a_broken_report_hides_neither_a_failure_nor_a_cancel(matched_trench, tmp_path, spy):
    """``report()`` failing on the way out of the mapper (``_keep_report``): the failure that stopped the mapper is
    still the reason of the fallback, and the report keeps the type of the error of ``report()``."""
    from pyarchinit_photogrammetry.engine import native_map

    ch = _copy(matched_trench, tmp_path).chunk
    spy.fail, spy.fail_report = "refine", True
    assert ch.align_cameras(engine="native") == len(ch.cameras)
    assert ch.meta["engine"]["align_cameras"]["fallback"] == "native mapper failed: boom"
    assert ch.meta["sfm"]["native_mapper"]["report"] == {"error": "RuntimeError"}

    class Broken:
        def report(self):
            raise RuntimeError("no")

    kept = {}
    native_map._keep_report(kept, Broken())
    assert kept == {"report": {"error": "RuntimeError"}}


@pytest.mark.slow
def test_a_cancel_before_the_mapper_starts_puts_the_record_back(matched_trench, tmp_path):
    """A cancel already asked when the native alignment begins: it comes out, and the record of the alignment before
    (the report of the earlier native mapper among it) is as it was."""
    import copy

    from pyarchinit_photogrammetry.core.progress import Cancelled, Progress

    ch = _copy(matched_trench, tmp_path).chunk
    ch.align_cameras(engine="native")
    assert "native_mapper" in ch.meta["sfm"]
    before = {k: copy.deepcopy(v) for k, v in ch.meta.items() if k != "timings"}
    progress = Progress()
    progress.cancel()
    with pytest.raises(Cancelled):
        ch.align_cameras(progress=progress, engine="native")
    assert {k: v for k, v in ch.meta.items() if k != "timings"} == before


class _Panic(BaseException):
    """What pyo3 raises for a panic of the engine (``PanicException``): no Exception."""


@pytest.mark.slow
@pytest.mark.parametrize("stop", [KeyboardInterrupt, _Panic])
def test_an_interrupt_or_a_panic_in_the_mapper_puts_the_record_back(matched_trench, tmp_path, monkeypatch, stop):
    """Whatever stops the native alignment beyond an Exception (an interrupt, a panic of the engine): it comes out
    as it is, and the record of the alignment before is as it was - as after a cancel."""
    import copy

    ch = _copy(matched_trench, tmp_path).chunk
    ch.align_cameras(engine="native")
    before = {k: copy.deepcopy(v) for k, v in ch.meta.items() if k != "timings"}

    def stopped(*args, **kwargs):
        raise stop("stopped")

    from pyarchinit_photogrammetry.engine import native_map

    monkeypatch.setattr(native_map, "global_mapping", stopped)
    with pytest.raises(stop):
        ch.align_cameras(engine="native")
    assert {k: v for k, v in ch.meta.items() if k != "timings"} == before


@pytest.mark.slow
def test_without_gps_and_with_gps_the_database_is_the_matchings(matched_ring, tmp_path):
    """``use_gps=False``: the native mapper positions the ring without the positions of the photos (a free frame, no
    pose priors recorded). With them it positions on them and leaves ``database.db`` as the matching wrote it (the
    pose priors are the classic mappers', written only when the classic chain aligns)."""
    project, _scene = matched_ring
    ch = _copy(project, tmp_path, "free").chunk
    assert ch.align_cameras(engine="native", use_gps=False) == RING_PHOTOS
    position = ch.meta["sfm"]["native_mapper"]["position"]
    assert position["frame"] == "free" and position["positions"] == 0
    assert ch.meta["sfm"]["gps_priors"] is None
    ch = _with_accuracy(_copy(project, tmp_path, "gps").chunk)
    db = ch.data_dir / "sfm" / "database.db"
    data = db.read_bytes()
    assert ch.align_cameras(engine="native") == RING_PHOTOS
    assert ch.meta["sfm"]["native_mapper"]["position"]["frame"] == "gnss"
    assert ch.meta["sfm"]["gps_priors"]["count"] == RING_PHOTOS
    assert db.read_bytes() == data
