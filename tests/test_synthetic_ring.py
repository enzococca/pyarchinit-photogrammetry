"""The synthetic ring flight: photos on a circle around a mound, noisy GPS in the EXIF, targets on the ground."""
import numpy as np
import pytest

from pyarchinit_photogrammetry.io.exif import read_exif
from pyarchinit_photogrammetry.markers.targets import detect
from pyarchinit_photogrammetry.testing.synthetic import make_ring_scene, render, ring_poses, write_ring_dataset


def centre_of(pose: np.ndarray) -> np.ndarray:
    """The centre of a camera: the translation of its world_from_cam pose."""
    return pose[:3, 3]


def distance_of_axis_from_origin(pose: np.ndarray) -> float:
    """Distance of the origin from the optical axis of a camera: the line through its centre along the +Z column
    of world_from_cam (OpenCV convention, as ``look_at`` builds it)."""
    centre, axis = pose[:3, 3], pose[:3, 2]
    return float(np.linalg.norm(np.cross(centre, axis)) / np.linalg.norm(axis))


def project(point_local: np.ndarray, world_from_cam: np.ndarray, K: np.ndarray, k1: float) -> np.ndarray:
    """A local point in the pixels of the renderer (the centre of the first pixel at (0.5, 0.5)), with the radial
    distortion ``x_d = x_u (1 + k1 r^2)`` the renderer applies."""
    p = world_from_cam[:3, :3].T @ (np.asarray(point_local, float) - world_from_cam[:3, 3])
    xn, yn = p[0] / p[2], p[1] / p[2]
    d = 1 + k1 * (xn ** 2 + yn ** 2)
    return np.array([K[0, 0] * xn * d + K[0, 2], K[1, 1] * yn * d + K[1, 2]])


def test_the_ring_cameras_stand_on_a_circle_and_look_at_the_centre():
    poses = ring_poses(72, diameter=60.0, height=12.0)
    centres = np.array([centre_of(p) for p in poses])
    assert len(poses) == 72
    assert np.allclose(np.hypot(centres[:, 0], centres[:, 1]), 30.0, atol=1e-6)
    assert np.allclose(centres[:, 2], 12.0)
    assert max(distance_of_axis_from_origin(p) for p in poses) < 1e-6
    # evenly spaced, the first on the +x axis, turning the same way as the angle
    angles = np.degrees(np.unwrap(np.arctan2(centres[:, 1], centres[:, 0])))
    assert abs(angles[0]) < 1e-9 and np.allclose(np.diff(angles), 360.0 / 72)
    for p in poses:
        assert np.allclose(p[:3, :3] @ p[:3, :3].T, np.eye(3)) and np.linalg.det(p[:3, :3]) > 0
        assert p[2, 2] < 0  # looking down


def _positions(scene, photos_dir, to_scene_crs) -> list[tuple[np.ndarray, np.ndarray, tuple]]:
    """For every photo of a written ring: the true centre (``scene.cameras``), the EXIF position converted to the
    scene's CRS, and the EXIF position as read (degrees and metres)."""
    out = []
    for cam in scene.cameras:
        info = read_exif(photos_dir / f"{cam['label']}.jpg")
        lon, lat, height = info["gps"]
        x, y = to_scene_crs.transform(lon, lat)
        out.append((cam["world_from_cam"][:3, 3], np.array([x, y, height]), info["gps"]))
    return out


def test_a_small_ring_is_written_with_noisy_gps_and_is_repeatable(tmp_path):
    from pyproj import Transformer

    a = write_ring_dataset(tmp_path / "a", n=8, size=(320, 240), seed=3)
    b = write_ring_dataset(tmp_path / "b", n=8, size=(320, 240), seed=3)
    c = write_ring_dataset(tmp_path / "c", n=8, size=(320, 240), seed=4)
    for name in ("a", "b", "c"):
        assert len(list((tmp_path / name / "photos").glob("*.jpg"))) == 8
        assert (tmp_path / name / "gcp.csv").read_text().startswith("# label,X,Y,Z (" + a.crs + ")\n0,")
    assert len(a.cameras) == len(b.cameras) == len(c.cameras) == 8
    # the ring stands around the centre of the scene, the poses are world poses
    centres = np.array([cam["world_from_cam"][:3, 3] for cam in a.cameras])
    middle = np.asarray(a.origin[:2]) + np.asarray(a.size) / 2
    assert np.allclose(np.hypot(*(centres[:, :2] - middle).T), 30.0)
    assert np.allclose(centres[:, 2], a.origin[2] + 12.0)
    to_scene_crs = Transformer.from_crs("EPSG:4326", a.crs, always_xy=True)
    pos_a = _positions(a, tmp_path / "a" / "photos", to_scene_crs)
    pos_b = _positions(b, tmp_path / "b" / "photos", to_scene_crs)
    pos_c = _positions(c, tmp_path / "c" / "photos", to_scene_crs)
    for (true_a, gps_a, read_a), (true_b, _, read_b), (true_c, gps_c, _) in zip(pos_a, pos_b, pos_c):
        # 2 m in plan and 3 m in height are the order of the error of a drone's own GPS, the noise the ring is
        # written with (0.3 m in plan, 0.5 m in height); the position is never the true one
        assert np.hypot(*(gps_a[:2] - true_a[:2])) < 2.0
        assert abs(gps_a[2] - true_a[2]) < 3.0
        assert np.linalg.norm(gps_a - true_a) > 0.001
        assert np.array_equal(true_a, true_b) and np.array_equal(true_a, true_c)
        assert read_a == read_b  # the same seed writes the same EXIF
        assert np.linalg.norm(gps_c - gps_a) > 0.001  # another seed, another noise
    for cam in a.cameras:
        info = read_exif(tmp_path / "a" / "photos" / f"{cam['label']}.jpg")
        assert info["focal_length_35mm"] == pytest.approx(36.0 * 1000.0 / 320, abs=0.5)  # the tag holds whole mm


def test_a_target_of_the_ring_is_read_in_neighbouring_photos():
    scene = make_ring_scene()
    size, focal, k1 = (1200, 900), 1000.0, -0.04
    K = np.array([[focal, 0, size[0] / 2], [0, focal, size[1] / 2], [0, 0, 1]])
    poses = ring_poses(72)[:3]
    for pose in poses:  # the ring is drawn around (0, 0): it stands around the centre of the scene
        pose[:2, 3] += np.asarray(scene.size) / 2
    seen: dict[int, list] = {}
    for pose in poses:
        for det in detect(render(scene, pose, K, size, k1)):
            seen.setdefault(det["code"], []).append((pose, det["center"]))

    def within_3_px(code: int) -> bool:
        local = np.asarray(scene.targets[code]) - np.asarray(scene.origin)
        # the detector gives OpenCV pixels (the centre of the first pixel at (0, 0)): +0.5 to the renderer's
        return all(np.linalg.norm(centre + 0.5 - project(local, pose, K, k1)) < 3.0 for pose, centre in seen[code])

    read_twice = [code for code, hits in seen.items() if len(hits) >= 2]
    assert any(within_3_px(code) for code in read_twice), f"no target read in two photos within 3 px: {sorted(seen)}"
