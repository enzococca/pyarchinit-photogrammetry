"""Rotation axis of vessels and sherds (``pottery.estimate_axis``) on the synthetic set: shapes,
sherds of several arcs, handles, roughness, and a whole bowl meshed as from photos."""
from __future__ import annotations

import numpy as np
import pytest
import trimesh
from scipy.spatial.transform import Rotation

from pyarchinit_photogrammetry.archaeo import pottery
from pyarchinit_photogrammetry.testing.synthetic import make_vessel

HEIGHT = {"bowl": 0.12, "jar": 0.20, "plate": 0.03, "beaker": 0.12}


def _placed(mesh, seed):
    t = np.eye(4)
    t[:3, :3] = Rotation.random(random_state=seed).as_matrix()
    t[:3, 3] = [1.0, 2.0, 3.0]
    mesh.apply_transform(t)
    return mesh, t


def axis_error(axis, t, height):
    """Angle (degrees) between the axis found and the true one, and distance (mm) of the true axis
    at mid height from the axis found."""
    d = t[:3, :3] @ [0.0, 0.0, 1.0]
    angle = np.degrees(np.arccos(min(1.0, abs(float(axis.direction @ d)))))
    p = t[:3, :3] @ [0.0, 0.0, height / 2] + t[:3, 3]
    return angle, 1000 * float(np.linalg.norm(np.cross(p - axis.point, axis.direction)))


def photo_bowl(seed: int):
    """A whole bowl as a mesh made from photos: the foot standing on the mat is not seen (below
    2 cm), the surface is rough by half a millimetre and the lip waves in and out by 2 mm."""
    m = make_vessel(sectors=180, above=0.02, noise=0.0005, seed=seed)
    v = np.array(m.vertices)
    ang = np.arctan2(v[:, 1], v[:, 0])
    r = np.hypot(v[:, 0], v[:, 1])
    ramp = np.clip((v[:, 2] - 0.11) / 0.01, 0, 1)
    push = 0.002 * np.sin(3 * ang + np.random.default_rng(seed).uniform(0, 6)) * ramp
    v[:, :2] += (push / np.maximum(r, 1e-9))[:, None] * v[:, :2]
    return trimesh.Trimesh(v, m.faces, process=False)


def test_vessel_shapes_of_the_synthetic_set():
    jar = make_vessel(shape="jar")
    lo, hi = jar.bounds
    assert abs(hi[2] - 0.20) < 0.006 and abs(hi[0] - 0.10) < 2e-3  # 20 cm high, belly 20 cm across
    top = jar.vertices[jar.vertices[:, 2] > 0.199]
    assert abs(np.hypot(top[:, 0], top[:, 1]).max() - 0.055) < 1e-3  # rim 11 cm
    plate = make_vessel(shape="plate")
    assert 0.03 < plate.bounds[1][2] < 0.037 and abs(plate.bounds[1][0] - 0.13) < 1e-3  # the floor is 6 mm thick
    rim = make_vessel(shape="beaker", sherd=(0, 90), above=0.09)
    assert rim.vertices[:, 2].min() > 0.085 and rim.is_watertight
    with_handle = make_vessel(handles=1)
    assert np.hypot(with_handle.vertices[:, 0], with_handle.vertices[:, 1]).max() > 0.095  # the loop sticks out
    with pytest.raises(ValueError):
        make_vessel(shape="amphora")


@pytest.mark.parametrize("shape", ["bowl", "jar", "plate", "beaker"])
def test_axis_of_whole_rough_vessels(shape):
    mesh, t = _placed(make_vessel(shape=shape, sectors=360, noise=0.0003), 7)
    angle, offset = axis_error(pottery.estimate_axis(mesh), t, HEIGHT[shape])
    assert angle < 0.2 and offset < 1.0, (shape, angle, offset)


@pytest.mark.parametrize("shape, arc, extra, max_angle, max_offset", [
    # a handle is a surface of revolution of its own: its lines pulled the axis from the normals of a
    # quarter of a bowl 17 mm off and turned that of a quarter of a jar by 11 degrees
    ("bowl", 90, {"handles": 1}, 0.3, 0.5),
    ("jar", 90, {"handles": 1}, 0.3, 0.5),
    ("plate", 140, {"handles": 1}, 0.3, 0.5),
    # an almost cylindrical beaker: the position of the axis was 17 mm off with a rough surface
    ("beaker", 90, {"noise": 0.0003}, 0.5, 1.0),
    ("bowl", 140, {"noise": 0.0003}, 0.5, 0.5),
    ("jar", 40, {}, 0.3, 1.0),
])
def test_axis_of_sherds(shape, arc, extra, max_angle, max_offset):
    mesh, t = _placed(make_vessel(shape=shape, sectors=360, sherd=(0, arc), **extra), arc + len(shape))
    angle, offset = axis_error(pottery.estimate_axis(mesh), t, HEIGHT[shape])
    assert angle < max_angle and offset < max_offset, (shape, arc, extra, angle, offset)


@pytest.mark.parametrize("seed", [0, 2])
def test_axis_of_a_bowl_meshed_from_photos(seed):
    """The normals of a mesh from photos are rough: the axis from them alone leaned by 0.4-0.75
    degrees here (2-4.6 on the turntable demo); the centres of the sections across it keep it
    within a few tenths."""
    angle, offset = axis_error(pottery.estimate_axis(photo_bowl(seed)), np.eye(4), 0.12)
    assert angle < 0.3 and offset < 0.5, (angle, offset)


@pytest.mark.parametrize("shape", ["jar", "beaker"])
def test_axis_points_from_the_base_to_the_rim(shape):
    """A jar is narrower at the mouth than at the belly: the floor tells the base, not the width."""
    for kw in ({}, {"sherd": (0, 140)}):
        mesh = make_vessel(shape=shape, sectors=180, **kw)
        mesh.apply_transform(np.diag([1.0, -1.0, -1.0, 1.0]))  # upside down
        assert pottery.estimate_axis(mesh, samples=2000, directions=600).direction[2] < -0.99, kw
