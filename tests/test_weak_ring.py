"""The synthetic ring flown once: nadir photos along one closed circular strip, each overlapping only its neighbours,
noisy GPS in the EXIF, targets on the circle under the strip; and the measure of its shape after one similarity."""
import hashlib
import types

import cv2
import numpy as np
from PIL import Image

from pyarchinit_photogrammetry.bench import truth
from pyarchinit_photogrammetry.io.exif import read_exif
from pyarchinit_photogrammetry.testing.synthetic import (WEAK_RING_GATE, make_ring_scene, weak_ring_poses,
                                                          write_weak_ring_dataset)

# SHA-256 of the texture, of the heights and of the sorted targets of make_ring_scene(seed=0), taken from the code
# before its parameters were added: the defaults give the ring of milestone 0 bit for bit
RING_TEXTURE = "f406ed3e7f8e94096d1839c5418aa8fab7f8bda49bf7a37e0f2e96c7f16603dc"
RING_HEIGHTS = "5404fdcb2732492925a9e8b169d45c26a82210e4d339a19f39fd39bf8e904e99"
RING_TARGETS = "9b9edff8359c7bfa44f40392c481a826186a415618c58a641a539eee481d263e"
# the texture's bytes differ in the last bits between platforms (the blur and resampling of OpenCV); where its hash
# is not macOS's, its colours are compared: the mean and spread of each channel and the mean of the red channel on an
# 8 x 8 grid of blocks
RING_TEXTURE_MEAN = (104.823, 120.412, 136.525)
RING_TEXTURE_STD = (15.723, 15.593, 15.448)
RING_TEXTURE_BLOCKS = [
    [109.7, 111.4, 108.0, 110.9, 101.8, 98.0, 99.5, 110.7], [97.9, 100.8, 118.6, 105.7, 106.2, 104.4, 106.2, 103.9],
    [99.6, 104.7, 109.2, 106.9, 99.4, 107.0, 113.2, 106.9], [108.6, 100.1, 99.6, 91.2, 94.6, 96.7, 108.3, 103.4],
    [106.3, 104.0, 107.6, 102.8, 102.0, 108.1, 94.9, 104.3], [109.2, 110.9, 113.0, 101.9, 107.2, 106.3, 102.3, 116.6],
    [114.3, 111.7, 97.3, 99.8, 101.7, 107.4, 98.5, 108.5], [102.2, 116.6, 99.7, 96.6, 92.2, 93.8, 108.2, 119.7]]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def footprint(pose: np.ndarray, focal: float, size) -> np.ndarray:
    """The corners of a photo projected on the plane z = 0 (the scene's ground, without its small relief and the
    distortion of the lens), as a float32 polygon in x, y."""
    w, h = size
    corners = []
    for u, v in ((0, 0), (w, 0), (w, h), (0, h)):
        ray = pose[:3, :3] @ np.array([(u - w / 2) / focal, (v - h / 2) / focal, 1.0])
        t = -pose[2, 3] / ray[2]
        corners.append(pose[:2, 3] + t * ray[:2])
    return np.array(corners, np.float32)


def overlap(a: np.ndarray, b: np.ndarray) -> float:
    area, _ = cv2.intersectConvexConvex(a, b)
    return float(area)


def test_the_default_ring_scene_is_unchanged():
    a = make_ring_scene(seed=0)
    if sha(a.texture.tobytes()) != RING_TEXTURE:
        t = a.texture.astype(np.float64)
        h, w = t.shape[:2]
        blocks = t[:h // 8 * 8, :w // 8 * 8, 0].reshape(8, h // 8, 8, w // 8).mean(axis=(1, 3))
        assert np.allclose(t.reshape(-1, 3).mean(axis=0), RING_TEXTURE_MEAN, atol=0.05)
        assert np.allclose(t.reshape(-1, 3).std(axis=0), RING_TEXTURE_STD, atol=0.05)
        assert np.allclose(blocks, RING_TEXTURE_BLOCKS, atol=0.15)
    assert sha(a.heights.tobytes()) == RING_HEIGHTS
    assert sha(repr(sorted(a.targets.items())).encode()) == RING_TARGETS
    assert (a.size, a.texel, a.origin, a.crs) == ((80.0, 80.0), 0.04, (2480000.0, 4500000.0, 100.0), "EPSG:3004")


def test_the_ring_scene_takes_its_size_targets_and_mound():
    s = make_ring_scene(seed=1, texel=0.08, size=(90.0, 90.0), target_radius=30.0, n_targets=12, mound=False)
    assert s.texture.shape == (1125, 1125, 3) and s.heights.shape == (1125, 1125) and s.size == (90.0, 90.0)
    assert sorted(s.targets) == list(range(12))
    xy = np.array([s.targets[c][:2] for c in range(12)]) - np.asarray(s.origin[:2]) - 45.0
    assert np.allclose(np.hypot(xy[:, 0], xy[:, 1]), 30.0, atol=0.3)  # on the circle, to a texel or so
    angles = np.degrees(np.arctan2(xy[:, 1], xy[:, 0])) % 360.0
    assert np.allclose(angles, 30.0 * np.arange(12), atol=0.6)  # every 30 degrees from +x
    assert float(s.heights.max() - s.heights.min()) < 0.5  # no mound: the small relief alone


def test_the_weak_ring_cameras_look_down_and_follow_the_circle():
    poses = weak_ring_poses(80, diameter=60.0, height=15.0)
    assert len(poses) == 80
    centres = np.array([p[:3, 3] for p in poses])
    assert np.allclose(np.hypot(centres[:, 0], centres[:, 1]), 30.0, atol=1e-9)
    assert np.allclose(centres[:, 2], 15.0)
    assert all(np.allclose(p[:3, 2], [0, 0, -1], atol=1e-9) for p in poses)  # optical axis down
    angles = np.unwrap(np.arctan2(centres[:, 1], centres[:, 0]))
    assert abs(angles[0]) < 1e-12 and np.allclose(np.diff(angles), 2 * np.pi / 80)
    for p, a in zip(poses, angles):
        assert np.allclose(p[:3, :3] @ p[:3, :3].T, np.eye(3), atol=1e-12) and np.linalg.det(p[:3, :3]) > 0
        radial, tangent = np.array([np.cos(a), np.sin(a), 0.0]), np.array([-np.sin(a), np.cos(a), 0.0])
        assert np.allclose(p[:3, 0], radial, atol=1e-9)  # the long side of the image across the track
        # the y axis of each camera along the tangent, the same sense of travel for all: the top of the image
        # (-y) ahead, towards the next photo
        assert np.allclose(p[:3, 1], -tangent, atol=1e-9)


def test_a_small_weak_ring_is_written_and_its_neighbours_overlap(tmp_path):
    # a third of the focal length for a third of the width: the footprint of the full ring (18 m x 13.5 m at 15 m)
    focal, size = 1000.0 / 3, (400, 300)
    scene = write_weak_ring_dataset(tmp_path, n=24, size=size, focal=focal, seed=3)
    photos = sorted((tmp_path / "photos").glob("*.jpg"))
    assert [p.name for p in photos] == [f"WRING_{i:04d}.jpg" for i in range(24)]
    assert [c["label"] for c in scene.cameras] == [p.stem for p in photos]
    for p in photos:
        info = read_exif(p)
        assert info["gps"] is not None and info["focal_length_35mm"] == round(36.0 * focal / 400)
        with Image.open(p) as im:  # no accuracy (GPSHPositioningError, tag 31): the program reads its default
            assert set(im.getexif().get_ifd(0x8825)) == {1, 2, 3, 4, 5, 6}
    assert (tmp_path / "gcp.csv").is_file()
    assert scene.diameter == 60.0 and sorted(scene.targets) == list(range(12))
    xy = np.array([scene.targets[c][:2] for c in range(12)]) - np.asarray(scene.origin[:2]) - 45.0
    assert np.allclose(np.hypot(xy[:, 0], xy[:, 1]), 30.0, atol=0.1)
    # the true poses, around the centre of the scene in its CRS
    centres = np.array([c["world_from_cam"][:3, 3] for c in scene.cameras]) - np.asarray(scene.origin)
    assert np.allclose(np.hypot(centres[:, 0] - 45.0, centres[:, 1] - 45.0), 30.0)
    local = []
    for c in scene.cameras:
        pose = np.array(c["world_from_cam"], float)
        pose[:3, 3] -= np.asarray(scene.origin)
        local.append(footprint(pose, focal, size))
    for i in range(24):
        assert overlap(local[i], local[(i + 1) % 24]) > 0.0
        assert overlap(local[i], local[(i + 6) % 24]) == 0.0


def ring_doc(points: dict) -> types.SimpleNamespace:
    """A stand-in document whose chunk has one triangulated marker per code of ``points`` (internal positions)."""
    markers = [types.SimpleNamespace(code=code, position=np.asarray(p, float)) for code, p in points.items()]
    markers.append(types.SimpleNamespace(code=40, position=None))  # not triangulated: left out
    return types.SimpleNamespace(chunk=types.SimpleNamespace(markers=markers))


def test_the_shape_of_a_ring_is_free_of_its_placement():
    rng = np.random.default_rng(5)
    angles = np.radians(30.0 * np.arange(12))
    world = np.column_stack([2480045.0 + 30.0 * np.cos(angles), 4500045.0 + 30.0 * np.sin(angles),
                             100.0 + rng.normal(0.0, 0.05, 12)])
    scene = types.SimpleNamespace(targets={c: tuple(world[c]) for c in range(12)}, diameter=60.0)
    turn = np.radians(30.0)
    R = np.array([[np.cos(turn), -np.sin(turn), 0.0], [np.sin(turn), np.cos(turn), 0.0], [0.0, 0.0, 1.0]])
    internal = 0.5 * (world - world.mean(axis=0)) @ R.T + np.array([3.0, -7.0, 1.5])
    out = truth.ring_shape(ring_doc(dict(enumerate(internal))), scene)
    assert out["truth_targets"] == 12
    assert out["truth_target_shape"] < 1e-9 and out["truth_target_shape_rmse"] < 1e-9
    bent = world.copy()
    bent[4, 0] += 0.6  # 1 % of the diameter
    internal = 0.5 * (bent - world.mean(axis=0)) @ R.T + np.array([3.0, -7.0, 1.5])
    out = truth.ring_shape(ring_doc(dict(enumerate(internal))), scene)
    # the similarity spreads part of the move over the other targets
    assert 0.005 < out["truth_target_shape"] < 0.01
    assert out["truth_target_shape_rmse"] < out["truth_target_shape"]
    assert truth.ring_shape(ring_doc({0: internal[0], 1: internal[1]}), scene) == {}  # fewer than 3 targets


def test_the_check_targets_and_the_gate_of_the_weak_ring():
    assert truth.WEAK_RING_CHECK == (1, 3, 5, 7, 9, 11)
    assert WEAK_RING_GATE["shape"] == 0.005
    assert set(WEAK_RING_GATE) == {"shape", "control_m", "check_m"}
    assert all(isinstance(WEAK_RING_GATE[k], float) and WEAK_RING_GATE[k] > 0 for k in ("control_m", "check_m"))


def test_the_full_weak_ring_overlaps_forward_about_82_percent():
    poses = weak_ring_poses(80, diameter=60.0, height=15.0)
    spacing = np.linalg.norm(poses[1][:3, 3] - poses[0][:3, 3])
    along = 900 * 15.0 / 1000.0  # the short side of the image along the track
    assert 0.80 < 1 - spacing / along < 0.84
