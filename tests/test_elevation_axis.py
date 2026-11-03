"""The axis of a cylindrical development: vertical (the default), the local Z of the chunk's region, the normal of
the crest plane of a tower, two points; the tilt kept with the frame and its entry; a ``u_map`` that turns the angle
around the axis into ``u`` (the arc on the wall instead of on the reference cylinder).

The synthetic scene has no tower: a truncated cone (radius 3 -> 2.6 m over 3 m, axis tilted 5 degrees towards the
east, the crest a plane square to the axis) is written as the model of a copy of the processed project."""
from __future__ import annotations

import re
import shutil

import numpy as np
import pytest

from pyarchinit_photogrammetry.engine import elevation as E
from pyarchinit_photogrammetry.engine.region import Region, set_region
from pyarchinit_photogrammetry.io.mesh_ply import MeshData, write_mesh

from elevation_helpers import ORIGIN

TILT = 5.0
AXIS = np.array([np.sin(np.radians(TILT)), 0.0, np.cos(np.radians(TILT))])  # the top leans east: azimuth 90
BASE = ORIGIN + [1.0, 1.0, 0.0]
HEIGHT = 3.0


def _rings(n_rings: int = 31, per_ring: int = 100):
    """World vertices of the tilted truncated cone, ring by ring (ring 0 at the base)."""
    e1 = np.cross(AXIS, [0.0, 1.0, 0.0])
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(AXIS, e1)
    t = np.linspace(0, 2 * np.pi, per_ring, endpoint=False)
    rings = []
    for h in np.linspace(0, HEIGHT, n_rings):
        r = 3.0 - 0.4 * h / HEIGHT
        rings.append(BASE + h * AXIS + r * (np.cos(t)[:, None] * e1 + np.sin(t)[:, None] * e2))
    return np.stack(rings)  # (rings, per_ring, 3)


def _write_tower(ch, rings):
    n_rings, per = rings.shape[:2]
    xyz = ch.to_internal(rings.reshape(-1, 3))
    v = np.empty(len(xyz), [("x", "<f4"), ("y", "<f4"), ("z", "<f4")])
    v["x"], v["y"], v["z"] = xyz.T
    faces = []
    for i in range(n_rings - 1):
        for j in range(per):
            a, b = i * per + j, i * per + (j + 1) % per
            c, d = a + per, b + per
            faces += [(a, b, d), (a, d, c)]
    path = ch.data_dir / "tower.ply"
    write_mesh(path, MeshData(v, np.asarray(faces, np.int64)))
    ch.products["model"] = "tower.ply"


@pytest.fixture(scope="module")
def tower_base(processed, tmp_path_factory):
    doc, _ch, _ = processed
    d = tmp_path_factory.mktemp("torre") / "base"
    d.mkdir()
    shutil.copy2(doc.path, d / doc.path.name)
    shutil.copytree(doc.files_dir, d / doc.files_dir.name)
    shutil.copytree(doc.path.parent / "photos", d / "photos")
    return d / doc.path.name


@pytest.fixture
def tower(tower_base, tmp_path):
    """A chunk whose model is the tilted tower; returns (chunk, circle) with the circle fitted (vertical) to plan
    points around the middle of the tower."""
    import pyarchinit_photogrammetry as ppg

    d = tmp_path / "progetto"
    d.mkdir()
    shutil.copy2(tower_base, d / tower_base.name)
    files = tower_base.parent / (tower_base.name + ".files")
    shutil.copytree(files, d / files.name)
    (d / "photos").symlink_to(tower_base.parent / "photos")
    doc = ppg.Document.open(d / tower_base.name)
    ch = doc.chunk
    ch.meta.pop("region", None)
    rings = _rings()
    _write_tower(ch, rings)
    mid = rings[len(rings) // 2]
    circle = E.cylinder_from_circle(ch, mid[::10, :2], inside=False)
    return ch, circle, rings


def _angle(a, b) -> float:
    return float(np.degrees(np.arccos(np.clip(np.dot(a, b) / np.linalg.norm(a) / np.linalg.norm(b), -1, 1))))


def test_vertical_is_the_default(tower):
    ch, circle, _ = tower
    axis, info = E.cylinder_axis(ch, circle)
    assert np.allclose(axis, [0, 0, 1])
    assert info["method"] == "vertical" and info["tilt_deg"] == 0.0
    old = E.cylinder_from_circle(ch, _rings()[15][::10, :2], inside=False)
    assert old.tilt is None and old.centre[2] == 0.0 and "tilt" not in old.to_dict() and "u_map" not in old.to_dict()


def test_crest_plane_gives_the_tower_axis(tower):
    ch, circle, _ = tower
    axis, info = E.cylinder_axis(ch, circle, "crest")
    assert _angle(axis, AXIS) < 0.3
    assert info["method"] == "crest"
    assert abs(info["tilt_deg"] - TILT) < 0.3
    assert abs(info["azimuth_deg"] - 90.0) < 3.0
    assert info["residual_m"] < 0.05
    top = BASE + HEIGHT * AXIS
    assert abs(info["centre_z"] - top[2]) < 0.05  # the crest's mean height

    cyl = E.cylinder_from_circle(ch, _rings()[15][::10, :2], inside=False, axis="crest")
    assert _angle(cyl.axis, AXIS) < 0.3 and cyl.tilt["method"] == "crest"
    assert abs(cyl.centre[2] - info["centre_z"]) < 1e-9
    assert cyl.to_dict()["tilt"] == cyl.tilt


def test_crest_not_a_plane_is_refused_with_its_residual(tower):
    ch, circle, rings = tower
    rings = rings.copy()
    top = rings[-1]
    side = np.arctan2(top[:, 1] - BASE[1], top[:, 0] - BASE[0])
    collapsed = np.abs(side) < np.pi / 4  # the east side, a quarter of the crest, fallen by 1 m
    low = int(round((HEIGHT - 1.0) / HEIGHT * (len(rings) - 1)))
    for i in range(low + 1, len(rings)):
        rings[i][collapsed] = rings[low][collapsed]
    _write_tower(ch, rings)
    with pytest.raises(ValueError, match=r"residual \d+\.\d\d m") as err:
        E.cylinder_axis(ch, circle, "crest")
    residual = float(re.search(r"residual (\d+\.\d\d) m", str(err.value)).group(1))
    assert residual > 0.3, str(err.value)
    assert "region" in str(err.value) and "two points" in str(err.value)
    # with a looser tolerance the same crest is accepted, its residual reported
    _axis, info = E.cylinder_axis(ch, circle, "crest", tolerance=2.0)
    assert abs(info["residual_m"] - residual) < 0.01


def test_region_axis(tower):
    ch, circle, _ = tower
    with pytest.raises(ValueError, match="region"):
        E.cylinder_axis(ch, circle, "region")
    a = np.radians(TILT)
    rot = np.array([[np.cos(a), 0, np.sin(a)], [0, 1, 0], [-np.sin(a), 0, np.cos(a)]])  # columns: the box axes
    set_region(ch, Region(BASE + [0, 0, 1.5], [7, 7, 4], rot))
    axis, info = E.cylinder_axis(ch, circle, "region")
    assert _angle(axis, rot[:, 2]) < 1e-6
    assert abs(info["tilt_deg"] - TILT) < 1e-6 and abs(info["azimuth_deg"] - 90.0) < 1e-6
    assert BASE[2] < info["centre_z"] < BASE[2] + HEIGHT


def test_two_points_axis(tower):
    ch, circle, _ = tower
    bottom, top = BASE, BASE + HEIGHT * AXIS
    axis, info = E.cylinder_axis(ch, circle, "points", points=[bottom, top])
    assert _angle(axis, AXIS) < 1e-6
    assert abs(info["centre_z"] - (bottom[2] + top[2]) / 2) < 1e-9
    axis2, _ = E.cylinder_axis(ch, circle, "points", points=[top, bottom])  # given the other way: still up
    assert _angle(axis2, AXIS) < 1e-6
    with pytest.raises(ValueError, match="1 m"):
        E.cylinder_axis(ch, circle, "points", points=[bottom, bottom + [0, 0, 0.5]])


def _oval_map(radius: float, inside: bool):
    """u_map of an oval wall d(theta) = 0.4 cos(2 theta) around a cylinder of ``radius``: inside the cylinder on
    one side, outside on the other; u = the arc (R + d) dtheta, cumulative."""
    th = np.radians(np.arange(0, 361, 1.0))
    d = 0.4 * np.cos(2 * th)
    r = radius - d if inside else radius + d
    u = np.concatenate([[0.0], np.cumsum((r[1:] + r[:-1]) / 2 * np.diff(th))])
    return [[float(t), float(x)] for t, x in zip(np.degrees(th), u)]


@pytest.mark.parametrize("inside", [False, True])
def test_u_map_point_and_coords_are_inverse(inside):
    umap = _oval_map(3.0, inside)
    cyl = E.Cylinder(BASE, AXIS, 3.0, inside, [0.0, 1.0, 0.0], tilt={"method": "points", "tilt_deg": TILT},
                     u_map=umap)
    rng = np.random.default_rng(7)
    u = rng.uniform(0, umap[-1][1] - 1e-6, 500)
    v = rng.uniform(-1, 4, 500)
    d = rng.uniform(-0.5, 0.5, 500)
    p = cyl.point(u, v, d)
    back = cyl.coords(p)
    assert np.abs(back - np.column_stack([u, v, d])).max() < 1e-3
    assert np.linalg.norm(cyl.point(*back.T) - p, axis=1).max() < 1e-3
    # the arc on the wall differs from R theta: longer where the wall is outside, shorter inside
    plain = E.Cylinder(BASE, AXIS, 3.0, inside, [0.0, 1.0, 0.0])
    assert not np.allclose(plain.coords(p)[:, 0] % (2 * np.pi * 3.0), back[:, 0], atol=0.01)
    # the round trip of the dict keeps the tilt and the map
    again = E.frame_from_dict(cyl.to_dict())
    assert again.tilt == cyl.tilt and np.allclose(again.u_map, cyl.u_map)
    assert np.abs(again.coords(p) - back).max() < 1e-9


def test_u_map_must_increase():
    with pytest.raises(ValueError, match="increas"):
        E.Cylinder(BASE, [0, 0, 1], 3.0, u_map=[[0, 0], [10, 0.5], [5, 1.0]])


def test_old_entry_reads_the_old_frame():
    d = {"kind": "cylinder", "centre": [1.0, 2.0, 0.0], "axis": [0, 0, 1], "radius": 2.0, "inside": True,
         "start": [1.0, 0.0, 0.0]}
    f = E.frame_from_dict(d)
    assert f.tilt is None and f.u_map is None and f.to_dict() == {**d, "axis": [0.0, 0.0, 1.0]}
    p = f.point(np.array([1.0]), np.array([0.5]), np.array([0.1]))
    assert np.allclose(f.coords(p), [[1.0, 0.5, 0.1]])


def test_build_elevation_keeps_the_tilt(tower):
    ch, _circle, rings = tower
    cyl = E.cylinder_from_circle(ch, rings[15][::10, :2], inside=False, axis="crest")
    entry = E.build_elevation(ch, cyl, resolution=0.1, depth=(-0.6, 0.6), max_angle=89.0)
    assert entry["tilt"] == cyl.tilt and entry["frame"]["tilt"] == cyl.tilt
    frame = E.frame_from_dict(entry["frame"])
    assert _angle(frame.axis, AXIS) < 0.3
    # on the tower's own axis the crest is a straight line: v of the crest the same all round
    crest = frame.coords(rings[-1])
    assert np.ptp(crest[:, 1]) < 0.1
    assert np.ptp(E.frame_from_dict(E.cylinder_from_circle(ch, rings[15][::10, :2], inside=False).to_dict())
                  .coords(rings[-1])[:, 1]) > 0.4


# ---------------------------------------------------------------- the scale on the wall
A_OVAL, B_OVAL = 3.0, 2.4  # semi-axes of the oval tower (x, y)
PERIMETER = np.pi * (3 * (A_OVAL + B_OVAL) - np.sqrt((3 * A_OVAL + B_OVAL) * (A_OVAL + 3 * B_OVAL)))


def _ellipse(t):
    return np.column_stack([A_OVAL * np.cos(t), B_OVAL * np.sin(t)])


def _along_the_wall(step: float):
    """Plan points of the oval every ``step`` metres measured along the wall."""
    t = np.linspace(0, 2 * np.pi, 200001)
    xy = _ellipse(t)
    s = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(xy, axis=0), axis=1))])
    return _ellipse(np.interp(np.arange(0, s[-1] - step / 2, step), s, t))


@pytest.fixture
def oval(tower_base, tmp_path):
    """A chunk whose model is an upright oval tower (ellipse 3 x 2.4 m, 3 m tall) and the cylinder of the circle
    fitted to plan points around it."""
    import pyarchinit_photogrammetry as ppg

    d = tmp_path / "ovale"
    d.mkdir()
    shutil.copy2(tower_base, d / tower_base.name)
    files = tower_base.parent / (tower_base.name + ".files")
    shutil.copytree(files, d / files.name)
    (d / "photos").symlink_to(tower_base.parent / "photos")
    doc = ppg.Document.open(d / tower_base.name)
    ch = doc.chunk
    t = np.linspace(0, 2 * np.pi, 400, endpoint=False)
    xy = _ellipse(t) + BASE[:2]
    rings = np.stack([np.column_stack([xy, np.full(len(t), BASE[2] + z)]) for z in np.linspace(0, HEIGHT, 31)])
    _write_tower(ch, rings)
    circle = E.cylinder_from_circle(ch, xy[::20], inside=False)
    return ch, circle


def test_wall_scale_measures_along_the_wall(oval):
    ch, circle = oval
    cyl = E.build_elevation(ch, circle, resolution=0.05, depth=(-0.8, 0.8), max_angle=89.0)
    wall = E.build_elevation(ch, circle, resolution=0.05, depth=(-0.8, 0.8), max_angle=89.0, scale="wall")
    assert "scale" not in cyl and wall["scale"] == "wall"
    w_cyl = cyl["bounds"][2] - cyl["bounds"][0]
    w_wall = wall["bounds"][2] - wall["bounds"][0]
    assert abs(w_cyl - 2 * np.pi * circle.radius) / (2 * np.pi * circle.radius) < 0.01
    assert abs(w_wall - PERIMETER) / PERIMETER < 0.01, (w_wall, PERIMETER)
    assert abs(E.wall_length(wall) - PERIMETER) / PERIMETER < 0.01
    assert abs(wall["size"][0] * wall["resolution"] - w_wall) < 3 * wall["resolution"]  # the grid follows the wall
    fc, fw = E.frame_from_dict(cyl["frame"]), E.frame_from_dict(wall["frame"])
    assert fc.u_map is None and fw.u_map is not None and fw.u_map[0, 0] == -180.0
    # the seam stays behind start: both developments begin at the same azimuth
    assert abs(np.degrees(fc._turn(cyl["bounds"][0])) - np.degrees(fw._turn(wall["bounds"][0]))) < 2.0
    assert abs(float(fw._u(0.0))) < 1e-9  # u = 0 at start, as without a map
    # "stones" every 20 cm along the wall, at mid height: 20 cm on the wall-scaled development everywhere,
    # 17-24 cm on the cylinder one
    pts = np.column_stack([_along_the_wall(0.2) + BASE[:2], np.full(len(_along_the_wall(0.2)), BASE[2] + 1.5)])
    for frame, lo, hi in ((fw, 0.19, 0.21), (fc, 0.17, 0.24)):
        du = np.diff(frame.coords(pts)[:, 0])
        du = du[np.abs(du) < 1.0]  # not across the seam
        assert du.min() > lo and du.max() < hi, (du.min(), du.max())
    du = np.diff(fc.coords(pts)[:, 0])
    assert np.ptp(du[np.abs(du) < 1.0]) > 0.03


def test_u_map_covers_a_full_turn_and_takes_scalars():
    with pytest.raises(ValueError, match="full turn"):
        E.Cylinder(BASE, [0, 0, 1], 3.0, u_map=[[0, 0], [180, 9.0]])
    cyl = E.Cylinder(BASE, [0, 0, 1], 3.0, u_map=_oval_map(3.0, False))
    p = cyl.point(0.0, 0.0, 0.0)
    assert p.shape == (3,) and np.allclose(p - BASE, [3.0, 0, 0], atol=1e-9)
    q = cyl.point(5.0, 1.0, 0.2)
    assert q.shape == (3,) and np.allclose(cyl.coords(q[None])[0], [5.0, 1.0, 0.2], atol=1e-9)


def test_wall_scale_only_on_a_cylinder(tower):
    ch, _circle, _ = tower
    with pytest.raises(ValueError, match="cylindrical"):
        E.build_elevation(ch, E.Plane(BASE, [1, 0, 0], [0, 0, 1]), scale="wall")


# ---------------------------------------------------------------- the command line
def test_cli_cylinder_axis_and_scale(tower):
    import pyarchinit_photogrammetry as ppg
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    ch, _circle, rings = tower
    ch.document.save()
    path = ch.document.path
    circle = ",".join(f"{x},{y}" for x, y in rings[15][::10, :2])
    runner = CliRunner()
    common = ["elevation", str(path), "--circle", circle, "--outside", "--resolution", "0.1", "--back", "0.6",
              "--front", "0.6"]
    r = runner.invoke(app, common + ["--axis", "crest", "--scale", "wall"])
    assert r.exit_code == 0, r.output
    assert "inclined" in r.output or "tilt" in r.output
    entry = ppg.Document.open(path).chunk.meta["elevations"][-1]
    assert entry["tilt"]["method"] == "crest" and abs(entry["tilt"]["tilt_deg"] - TILT) < 0.3
    assert entry["scale"] == "wall" and entry["frame"]["inside"] is False
    bottom, top = BASE, BASE + HEIGHT * AXIS
    r = runner.invoke(app, common + ["--axis", "points", "--axis-points", ",".join(str(v) for v in [*bottom, *top])])
    assert r.exit_code == 0, r.output
    entry = ppg.Document.open(path).chunk.meta["elevations"][-1]
    assert entry["tilt"]["method"] == "points" and "scale" not in entry
    assert _angle(entry["frame"]["axis"], AXIS) < 1e-6
    r = runner.invoke(app, common + ["--axis", "region"])  # no region: the engine's refusal, nothing built
    assert r.exit_code != 0 and "region" in r.output
    r = runner.invoke(app, ["elevation", str(path), "--trace", "1,2,3,4", "--scale", "wall"])
    assert r.exit_code != 0 and "--circle" in r.output


# ---------------------------------------------------------------- the axis through the tower's own centre
@pytest.mark.parametrize("method", ["crest", "region", "points"])
def test_tilted_axis_passes_through_the_tower(tower, method):
    """The plan circle picked at the base of the leaning tower: its centre is off the axis higher up; the axis
    line goes through the centre of the wall seen along the axis, so the wall keeps its depth all round."""
    from pyarchinit_photogrammetry.engine.region import Region, set_region

    ch, _circle, rings = tower
    a = np.radians(TILT)
    set_region(ch, Region(BASE + [0, 0, 1.5], [7, 7, 4], [[np.cos(a), 0, np.sin(a)], [0, 1, 0],
                                                          [-np.sin(a), 0, np.cos(a)]]))
    ends = [BASE + 0.2 * AXIS, BASE + 2.8 * AXIS]  # two points on the axis itself (a shaft)
    cyl = E.cylinder_from_circle(ch, rings[0][::10, :2], inside=False, axis=method, axis_points=ends)
    d = np.stack([cyl.coords(ring)[:, 2] for ring in rings])
    assert np.ptp(d, axis=1).max() < 0.05, np.ptp(d, axis=1).max()
    # the plan circle lifted to the crest's height misses the axis: the depth swings round the wall
    lifted = E.Cylinder([*cyl.tilt["centre"][:2], cyl.tilt["centre_z"]], cyl.axis, cyl.radius, False)
    lifted.centre[:2] = E.cylinder_from_circle(ch, rings[0][::10, :2], inside=False).centre[:2]
    assert np.ptp(np.stack([lifted.coords(r)[:, 2] for r in rings]), axis=1).max() > 0.1
    assert abs(cyl.centre[2] - cyl.tilt["centre_z"]) < 1e-9  # v = 0 at centre_z
    assert abs(cyl.tilt["fitted_radius"] - 2.8) < 0.25
    if method == "points":
        assert cyl.tilt["points_offset_m"] < 0.05


def test_points_on_a_battered_face_are_flagged(tower):
    ch, circle, rings = tower
    _axis, info = E.cylinder_axis(ch, circle, "points", points=[rings[0][0], rings[-1][0]])
    assert info["points_offset_m"] > 0.3  # the face of the wall, not the axis


def test_axis_needs_a_plain_circle(tower):
    ch, circle, _ = tower
    with pytest.raises(ValueError, match="u map"):
        E.cylinder_axis(ch, E.Cylinder(circle.centre, circle.axis, circle.radius, False, circle.start,
                                        u_map=_oval_map(circle.radius, False)), "crest")


def test_scale_of_the_entry_agrees_with_the_frame(oval):
    """A frame that carries a u map built on the cylinder scale: the development is on the cylinder, as asked."""
    ch, circle = oval
    wall = E.build_elevation(ch, circle, resolution=0.1, depth=(-0.8, 0.8), max_angle=89.0, scale="wall")
    mapped = E.frame_from_dict(wall["frame"])
    plain = E.build_elevation(ch, mapped, resolution=0.1, depth=(-0.8, 0.8), max_angle=89.0)
    assert "scale" not in plain and "u_map" not in plain["frame"]
    assert abs((plain["bounds"][2] - plain["bounds"][0]) - 2 * np.pi * circle.radius) < 0.02 * 2 * np.pi * circle.radius
