"""How much of the rim a vessel or a sherd keeps (``rim_preserved_pct``): whole vessels, sherds of a
known arc, rough surfaces, a lip that waves, an axis a little tilted, a chip and a spike of the mesh."""
from __future__ import annotations

import numpy as np
import pytest
import trimesh
from scipy.spatial.transform import Rotation

from pyarchinit_photogrammetry.archaeo import pottery
from pyarchinit_photogrammetry.testing.synthetic import make_vessel

SHIFTED = (0.003, -0.002, 0.0)  # the axis given 3.6 mm off the true one


def tilted(degrees: float, azimuth: float = 30.0):
    """The axis given leaning by ``degrees`` (a vessel not quite upright on the turntable)."""
    return pottery.Axis(SHIFTED, Rotation.from_euler("zx", [azimuth, degrees], degrees=True).apply([0, 0, 1.0]))


def wavy(mesh, amplitude=0.004):
    """The last 5 mm of the lip pushed in and out by ``amplitude`` (three lobes): a lip meshed from photos."""
    v = np.array(mesh.vertices)
    ang = np.arctan2(v[:, 1], v[:, 0])
    r = np.hypot(v[:, 0], v[:, 1])
    ramp = np.clip((v[:, 2] - (v[:, 2].max() - 0.005)) / 0.005, 0, 1)
    v[:, :2] += (amplitude * np.sin(3 * ang + 0.7) * ramp / np.maximum(r, 1e-9))[:, None] * v[:, :2]
    return trimesh.Trimesh(v, mesh.faces, process=False)


def spike(mesh):
    """One vertex of the lip 4 mm too high (a spike of the mesh)."""
    v = np.array(mesh.vertices)
    v[np.argmax(v[:, 2]), 2] += 0.004
    return trimesh.Trimesh(v, mesh.faces, process=False)


def chipped(mesh, a0, a1, depth=0.008):
    """The lip broken away between ``a0`` and ``a1`` degrees: that part of the rim is lost."""
    v = np.array(mesh.vertices)
    ang = np.degrees(np.arctan2(v[:, 1], v[:, 0])) % 360
    top = v[:, 2].max()
    sel = (ang > a0) & (ang < a1) & (v[:, 2] > top - depth)
    v[sel, 2] = top - depth - 0.0005 * np.random.default_rng(0).random(sel.sum())
    return trimesh.Trimesh(v, mesh.faces, process=False)


@pytest.mark.parametrize("shape", ["bowl", "jar", "plate"])
def test_a_whole_rim_is_whole(shape):
    """Before: 52 % (bowl), 73 % (jar), 39 % (plate) with the axis leaning by 2 degrees (the band
    under the highest point missed the low side of the lip), 0 % with a spike on the lip, 50 % from
    ``measure`` for a rough or waving lip (the arc from the chord cannot pass 180 degrees)."""
    base = make_vessel(shape=shape, sectors=360)
    cases = {"tilted": (base, tilted(2.0)), "spike": (spike(base), pottery.Axis(SHIFTED, (0, 0, 1))),
             "rough": (make_vessel(shape=shape, sectors=360, noise=0.0003), pottery.Axis(SHIFTED, (0, 0, 1))),
             "wavy": (wavy(base), pottery.Axis(SHIFTED, (0, 0, 1)))}
    for name, (mesh, axis) in cases.items():
        assert pottery.rim(mesh, axis)["rim_preserved_pct"] > 99.0, (shape, name)
        _, meas = pottery.measure(mesh, axis)
        assert meas["rim_preserved_pct"] > 99.0, (shape, name, meas["rim_preserved_pct"])


@pytest.mark.parametrize("shape", ["bowl", "jar", "plate"])
@pytest.mark.parametrize("arc", [90, 144, 252])
def test_sherds_of_a_known_arc(shape, arc):
    """A quarter, 40 % and 70 % of the rim, rough and with the axis leaning by 2 degrees."""
    for kw, axis in (({"noise": 0.0003}, pottery.Axis(SHIFTED, (0, 0, 1))), ({}, tilted(2.0))):
        mesh = make_vessel(shape=shape, sectors=360, sherd=(200, 200 + arc), **kw)
        _, meas = pottery.measure(mesh, axis)
        assert abs(meas["rim_preserved_pct"] - arc / 3.6) < 1.5, (shape, arc, kw, meas["rim_preserved_pct"])


def test_a_chip_in_the_lip_is_not_counted():
    """70 % of a rim with 60 degrees of its lip broken away in the middle keeps 53 %: before, the
    arc was the circle less its largest gap (70 %)."""
    mesh = chipped(make_vessel(sectors=360, sherd=(200, 452)), 280, 340)
    r = pottery.rim(mesh, pottery.Axis(SHIFTED, (0, 0, 1)))
    assert abs(r["rim_preserved_pct"] - (252 - 60) / 3.6) < 1.0, r["rim_preserved_pct"]
    whole = chipped(make_vessel(sectors=360), 220, 280)
    assert abs(pottery.rim(whole, pottery.Axis(SHIFTED, (0, 0, 1)))["rim_preserved_pct"] - 300 / 3.6) < 1.0


def test_a_small_sherd_is_not_counted_around_a_collapsed_circle():
    """The circle of a 30 degree arc of a plate collapses towards the arc (6.6 cm across instead of
    25): around its centre the arc read 48-84 %. Not reliable: the smaller arc, around the axis."""
    for kw in ({}, {"noise": 0.0003}):
        mesh = make_vessel(shape="plate", sectors=360, sherd=(200, 230), **kw)
        r = pottery.rim(mesh, pottery.Axis(SHIFTED, (0, 0, 1)))
        assert not r["reliable"] and abs(r["rim_preserved_pct"] - 30 / 3.6) < 1.5, (kw, r["rim_preserved_pct"])
        _, meas = pottery.measure(mesh, pottery.Axis(SHIFTED, (0, 0, 1)))
        assert abs(meas["rim_preserved_pct"] - 30 / 3.6) < 1.0
