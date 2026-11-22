"""The native mapper of ``ppg_core`` (``NativeMapper``, milestone 3) seen from Python.

The bindings come with Task 8 of the plan: until ``_native.native_mapper()`` gives the class, every
test here is skipped. ``test_the_model_is_colmaps`` checks that the model the mapper writes in
COLMAP's binary format is read by pycolmap and written again by it byte for byte.
"""

from __future__ import annotations

import numpy as np
import pytest

from pyarchinit_photogrammetry import _native


def _mapper_class():
    get = getattr(_native, "native_mapper", None)
    return get() if get is not None else None


pytestmark = pytest.mark.skipif(_mapper_class() is None, reason="ppg_core has no NativeMapper (Task 8 builds it)")

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
