"""The native mapper of ``ppg_core`` (``NativeMapper``, milestone 3) seen from Python.

Every test here is skipped only when ``_native.native_mapper()`` gives no class (a core without the
mapper, API 16, or ``PPG_NATIVE=0``): the stages on a synthetic strip, the progress callback that
raises, the arrays refused, the thresholds taken from pycolmap and the model the mapper writes in
COLMAP's binary format, read by pycolmap and written again by it byte for byte.
"""

from __future__ import annotations

import numpy as np
import pytest

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
