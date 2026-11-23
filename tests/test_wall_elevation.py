"""Elevations on the line of a wall traced on the plan (a :class:`WallPath` frame): the orthophoto and the relief
of the synthetic wall with a corner (``testing.synthetic.make_wall_scene``) seen from outside and from inside,
``u`` the length on the face of the wall, the depth band limited at each ``u`` by the frame, tiles, patches, the
project opened again, the local system oriented again."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import numpy as np
import pytest
import rasterio

from pyarchinit_photogrammetry.core import geometry as geo
from pyarchinit_photogrammetry.core import stamps
from pyarchinit_photogrammetry.engine import elevation as E
from pyarchinit_photogrammetry.engine import orient, orient_lines, texture_ortho
from pyarchinit_photogrammetry.engine import patches as P
from pyarchinit_photogrammetry.engine.visibility import raycaster_for_chunk
from pyarchinit_photogrammetry.engine.wall_path import CLEARANCE, WallPath, corner_radii, ordered
from pyarchinit_photogrammetry.testing.synthetic import make_wall_scene

from wall_helpers import dark_blobs, model_doc, wall_doc

RES = 0.01
L = [[0.0, 0.0], [6.0, 0.0], [6.0, 4.0]]
JOG = [[0.0, 0.0], [3.0, 0.0], [3.0, 0.3], [6.0, 0.3]]                      # two corners 30 cm apart
TURN = [[0.0, 0.0], [4.0, 0.0], [4.0 + 4.0 * np.cos(np.radians(120.0)), 4.0 * np.sin(np.radians(120.0))]]
BUTTRESS = [[0.0, 0.0], [3.0, 0.0], [3.0, -0.6], [3.8, -0.6], [3.8, 0.0], [7.0, 0.0]]   # 60 cm towards the viewer
ZIGZAG = [[0.0, 0.0], [4.0, 0.0], [1.4, 1.5], [5.4, 1.5]]                               # two turns of 150 degrees
ARC = 3.0 * np.column_stack([np.cos(np.radians(np.linspace(0, 120, 61))), np.sin(np.radians(np.linspace(0, 120, 61)))])
CORRIDOR = [[0.6, 3.0], [0.6, 0.0], [0.0, 0.0], [0.0, 3.0]]                 # seen from inside: 60 cm wide


def path_of(info) -> WallPath:
    """The frame of a wall: its face in the order the viewer sees it, the fillets that keep its corners in the
    band."""
    face = ordered(info["face"], info["viewer"])
    return WallPath(face, radii=corner_radii(face))


def _read(path):
    with rasterio.open(path) as src:
        return src.read(), src.transform


def relief_of(ch, entry) -> np.ndarray:
    return _read(E.elevation_paths(ch, entry)[1])[0][0]


def ortho_of(ch, entry) -> np.ndarray:
    return _read(E.elevation_paths(ch, entry)[0])[0]


def ortho_with_transform(ch, entry):
    return _read(E.elevation_paths(ch, entry)[0])


def _origin(entry) -> tuple[float, float]:
    """The top left corner of the grid of an elevation (as ``FrameSurface`` places it on its bounds)."""
    res = entry["resolution"]
    return float(np.floor(entry["bounds"][0] / res) * res), float(np.ceil(entry["bounds"][3] / res) * res)


def cols_of(entry, u0: float, u1: float) -> tuple[int, int]:
    """The columns (first, one past the last) of the cells of an elevation whose centre lies between two ``u``."""
    res, left = entry["resolution"], _origin(entry)[0]
    return int(np.ceil((u0 - left) / res - 0.5 - 1e-6)), int(np.floor((u1 - left) / res - 0.5 + 1e-6)) + 1


def rows_of(entry, v0: float, v1: float) -> tuple[int, int]:
    """The rows (first, one past the last) of the cells of an elevation whose centre lies between two heights."""
    res, top = entry["resolution"], _origin(entry)[1]
    return int(np.ceil((top - v1) / res - 0.5 - 1e-6)), int(np.floor((top - v0) / res - 0.5 + 1e-6)) + 1


def window(ch, entry, box):
    """The relief and the colours (h, w, 3) of the cells of an elevation whose centres lie in ``box`` (u0, v0, u1,
    v1)."""
    rows, cols = slice(*rows_of(entry, box[1], box[3])), slice(*cols_of(entry, box[0], box[2]))
    return SimpleNamespace(relief=relief_of(ch, entry)[rows, cols],
                           rgb=np.moveaxis(ortho_of(ch, entry)[:3, rows, cols], 0, -1))


def _added(scene, vertices, faces):
    """``scene`` with more triangles (``faces`` index ``vertices``, local metres): a block, a panel, a ground. In
    place; returns the scene."""
    k = len(scene.vertices)
    scene.vertices = np.vstack([scene.vertices, np.asarray(vertices, np.float64)])
    scene.faces = np.vstack([scene.faces, np.asarray(faces) + k]).astype(scene.faces.dtype)
    scene.face_columns = np.concatenate([scene.face_columns, np.full(len(faces), np.nan)])
    return scene


def _on_rough_ground(scene, rough: float = 0.01, beyond=(3.5, 0.7), cell: float = 0.02, seed: int = 3):
    """``scene`` with a ground that is not a plane: a grid of ``cell`` whose heights are anywhere between 0 and
    ``rough``, over the box of the trace and ``beyond`` it (metres in x, in y)."""
    rng = np.random.default_rng(seed)
    lo, hi = scene.face.min(axis=0) - beyond, scene.face.max(axis=0) + beyond
    gx, gy = np.meshgrid(np.arange(lo[0], hi[0] + cell / 2, cell), np.arange(lo[1], hi[1] + cell / 2, cell))
    xyz = np.column_stack([gx.ravel(), gy.ravel(), rng.uniform(0.0, rough, gx.size)])
    idx = np.arange(gx.size).reshape(gx.shape)
    a, b, c, d = idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel(), idx[1:, 1:].ravel(), idx[1:, :-1].ravel()
    return _added(scene, xyz, np.vstack([np.column_stack([a, b, c]), np.column_stack([a, c, d])]))


def _model_of(folder, xyz, faces, label: str = "Modello"):
    """A chunk in a local system whose model is the mesh ``xyz``, ``faces`` (metres), and nothing else."""
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.io.mesh_ply import MeshData, write_mesh

    doc = ppg.Document()
    ch = doc.add_chunk(label)
    ch.meta["metric"] = True
    folder.mkdir(parents=True, exist_ok=True)
    doc.save(folder / "modello.ppg")
    v = np.empty(len(xyz), [("x", "<f4"), ("y", "<f4"), ("z", "<f4")])
    v["x"], v["y"], v["z"] = np.asarray(xyz, np.float64).T
    ch._set_product("model", write_mesh(ch.data_dir / "model.ply", MeshData(v, np.asarray(faces, np.int64))))
    return ch


@pytest.fixture(scope="module")
def wall(wall_dataset, tmp_path_factory):
    """The wall seen from outside its corner, with its photos: ``(chunk, info)``."""
    _doc, (ch,), info = wall_doc(tmp_path_factory.mktemp("muro_fuori"), wall_dataset)
    return ch, info


@pytest.fixture(scope="module")
def built(wall, wall_dataset_inside, tmp_path_factory):
    """The L built with ``scale="wall"`` from outside and from inside: ``{which: (chunk, info, entry)}``."""
    ch, info = wall
    out = {"outside": (ch, info, E.build_elevation(ch, path_of(info), resolution=RES, max_angle=89.0))}
    _doc, (ch,), info = wall_doc(tmp_path_factory.mktemp("muro_dentro"), wall_dataset_inside)
    out["inside"] = (ch, info, E.build_elevation(ch, path_of(info), resolution=RES, max_angle=89.0))
    return out


@pytest.fixture(scope="module")
def wall_entry(built):
    return built["outside"][2]


@pytest.fixture(scope="module")
def model(tmp_path_factory):
    """The model of the default wall alone, seen from outside: ``(chunk, info)``. Its elevations are relief only."""
    _doc, ch, info = model_doc(tmp_path_factory.mktemp("modello"), make_wall_scene())
    return ch, info


def test_a_two_point_path_is_the_plane_elevation(wall):          # wall: (chunk, info) of wall_dataset
    ch, info = wall
    a, b = np.asarray(info["face"][0]), np.asarray(info["face"][1])
    side = (a + b) / 2 + [0.0, -3.0]
    box = (0.5, 100.2, 4.5, 101.8)
    kw = dict(resolution=RES, depth=(-0.3, 0.5), max_angle=89.0, bounds=box)
    plane = E.build_elevation(ch, E.plane_from_line(ch, a, b, side), **kw)
    path = E.build_elevation(ch, E.WallPath([a, b]), scale="trace", **kw)
    assert path["size"] == plane["size"] and path["frame"]["kind"] == "path" and path["v_is_height"] is True
    dp, dq = relief_of(ch, plane), relief_of(ch, path)
    assert np.array_equal(np.isfinite(dp), np.isfinite(dq)) and np.nanmax(np.abs(dp - dq)) < 1e-4
    diff = np.abs(ortho_of(ch, plane).astype(int) - ortho_of(ch, path).astype(int))
    assert (diff > 2).mean() < 0.001 and (diff > 0).mean() < 0.01


def test_the_straight_part_of_the_L_equals_its_plane(wall, wall_entry):   # wall_entry: the L built with scale="wall"
    ch, info = wall
    a, b = np.asarray(info["face"][0]), np.asarray(info["face"][1])
    plane = E.build_elevation(ch, E.plane_from_line(ch, a, b, (a + b) / 2 + [0.0, -3.0]), resolution=RES,
                              depth=(-0.5, 0.5), max_angle=89.0, bounds=(0.5, 100.2, 4.5, 101.8))
    p, q = window(ch, plane, (0.8, 100.3, 4.2, 101.7)), window(ch, wall_entry, (0.8, 100.3, 4.2, 101.7))
    assert np.nanmax(np.abs(p.relief - q.relief)) < 1e-3
    diff = np.abs(p.rgb.astype(int) - q.rgb.astype(int))
    assert diff.mean() < 1.0 and np.percentile(diff, 99) <= 3      # identical within interpolation


@pytest.mark.parametrize("which", ["outside", "inside"])
def test_marks_at_their_face_length_once_each(which, built):      # built[which]: (chunk, info, entry)
    ch, info, entry = built[which]
    rgba, t = ortho_with_transform(ch, entry)
    found = dark_blobs(rgba)
    assert len(found) == len(info["marks"])                         # no mark twice, none lost: no fold at the corner
    for length, height, _xyz in info["marks"]:
        u = length if info["viewer"] == "right" else 10.0 - length   # u starts at the viewer's left end of the wall
        r, c = min(found, key=lambda rc: abs(t.c + (rc[1] + 0.5) * RES - u))
        assert abs(t.c + (c + 0.5) * RES - u) <= RES and abs(t.f - (r + 0.5) * RES - (100.0 + height)) <= RES
    assert abs(E.wall_length(entry) - 10.0) < 0.01 and entry["scale"] == "wall" and entry["v_is_height"] is True
    d = relief_of(ch, entry)
    rows = slice(*rows_of(entry, 100.2, 101.8))
    cols = slice(*cols_of(entry, 0.05, 9.95))
    assert np.isfinite(d[rows, cols]).mean(axis=0).min() > 0.98     # no gap in any column, the corner's too


@pytest.mark.parametrize("which", ["outside", "inside"])
def test_the_wall_is_painted_round_its_corner(which, built):
    """The orthophoto has its colours wherever the relief has the wall, the corner included, and the relief of the
    corner is the corner as traced: in front of the arc seen from outside, behind it seen from inside."""
    ch, info, entry = built[which]
    assert entry["name"] == "Prospetto del muro" and entry["depth"] == [-0.5, 0.5] and "extend" not in entry
    assert entry["bounds"] == pytest.approx([0.0, 100.0, 10.0, 102.0], abs=2e-3)
    frame = E.frame_from_dict(entry["frame"])
    assert frame.kind == "path" and frame.u_map is not None and E.heights_measured(frame)
    rows, cols = slice(*rows_of(entry, 100.2, 101.8)), slice(*cols_of(entry, 0.05, 9.95))
    assert (ortho_of(ch, entry)[3, rows, cols] > 0).mean() > 0.995
    corner = frame.corners()[0]
    assert corner["u"] == pytest.approx(6.0 if which == "outside" else 4.0, abs=2e-3)
    d = relief_of(ch, entry)[rows, slice(*cols_of(entry, corner["u"] - 0.005, corner["u"] + 0.005))]
    off = 0.52 * (np.sqrt(2.0) - 1.0)                                # of the corner from its arc
    assert np.nanmedian(d) == pytest.approx(off if which == "outside" else -off, abs=0.005)
    # away from the corner the face is the line: no relief
    for a, b in ((0.5, 3.0), (7.0, 9.5)):
        assert np.nanmax(np.abs(relief_of(ch, entry)[rows, slice(*cols_of(entry, a, b))])) < 1e-3


def test_the_band_follows_the_limits_of_the_frame(tmp_path):
    # the wall seen from inside, with a merlon 3 m high on its last metres
    scene = _added(make_wall_scene(viewer="left"), [[0.9, 0.0, 2.0], [1.1, 0.0, 2.0], [1.0, 0.0, 3.0]], [[0, 1, 2]])
    _doc, ch, info = model_doc(tmp_path, scene)
    frame = WallPath(ordered(info["face"], "left"), radius=0.5)
    rc, lock = raycaster_for_chunk(ch), threading.Lock()
    surf = E.FrameSurface(ch, rc, frame, RES, lock, (-0.5, 0.5), None)
    # without bounds: u from the trace's own ends, v from the vertices of the model in the band between them
    assert surf.bounds == pytest.approx((0.0, 100.0, frame.length, 103.0), abs=1e-6)
    # a trace shorter than the wall, its ends between two vertices of the model: still its own ends, not the wall's
    # (which goes on past both) nor the vertices next to them; the merlon, past its end, is not of its heights
    short = WallPath(np.array([[6.0, 3.07], [6.0, 0.0], [2.07, 0.0]]) + info["shift"][:2], radius=0.5)
    part = E.FrameSurface(ch, rc, short, RES, lock, (-0.5, 0.5), None)
    assert part.bounds == pytest.approx((0.0, 100.0, short.length, 102.0), abs=1e-6)
    mid = frame.corners()[0]["u"]
    lo, hi = surf.band(np.array([1.0, mid, np.nan]))
    assert np.allclose(lo[:2], -0.5) and np.allclose(hi[:2], [0.5, 0.5 - CLEARANCE], atol=1e-6)
    d = surf.depth_at(np.array([1.0, mid, np.nan]), np.array([101.0, 101.0, 101.0]))
    assert abs(d[0]) < 1e-4 and abs(d[1] + 0.5 * (np.sqrt(2.0) - 1.0)) < 1e-4 and np.isnan(d[2])
    # a band the frame closes in the corner: no surface there, and none taken from the other wall
    front = E.FrameSurface(ch, rc, frame, RES, lock, (0.49, 3.0), (0.0, 100.0, frame.length, 102.0))
    lo, hi = front.band(np.array([1.0, mid]))
    assert lo[1] > hi[1] and hi[0] == pytest.approx(3.0 - CLEARANCE, abs=1e-6)
    assert np.isnan(front.depth_at(np.array([1.0, mid]), np.array([101.0, 101.0]))).all()
    # a plane or a cylinder: the scalars of the surface at every u
    a, b = np.asarray(info["face"][0]), np.asarray(info["face"][1])
    plane = E.FrameSurface(ch, rc, E.plane_from_line(ch, a, b, (a + b) / 2 + [0.0, 3.0]), RES, lock, (-0.3, 0.4),
                           (0.0, 100.0, 1.0, 101.0))
    lo, hi = plane.band(np.array([0.2, 0.7, 5.0]))
    assert np.array_equal(lo, [-0.3] * 3) and np.array_equal(hi, [0.4] * 3)
    well = E.FrameSurface(ch, rc, E.Cylinder([a[0] - 1.0, a[1] + 1.0, 0.0], [0, 0, 1], 0.8, inside=True), RES, lock,
                          (-0.5, 2.0), (0.0, 100.0, 1.0, 101.0))
    assert np.array_equal(well.band(np.array([0.3]))[1], [0.8])     # from inside a ray starts on the axis at most
    # each u is asked of the frame once: the columns of a grid come back tile after tile, and for the relief
    asked = []
    limit = frame.max_depth_at
    frame.max_depth_at = lambda u: (asked.append(len(u)), limit(u))[1]
    grid = E.FrameSurface(ch, rc, frame, RES, lock, (-0.5, 0.5), (0.0, 100.0, frame.length, 102.0))
    first = grid.tile(0, 300, 20, 64)
    assert asked == [64] and np.array_equal(grid.tile(0, 300, 20, 64), first, equal_nan=True) and asked == [64]
    grid.tile(10, 332, 20, 64)
    assert asked == [64, 32]                                         # the 32 columns that are new
    lo, hi = grid.band(np.array([mid, 1.0, np.nan, mid]))            # in any order, twice, not a number
    assert np.allclose(hi[[0, 1, 3]], [0.5 - CLEARANCE, 0.5, 0.5 - CLEARANCE], atol=1e-6) and np.isnan(hi[2])
    assert asked == [64, 32, 2] and np.allclose(lo[[0, 1, 3]], -0.5) and np.isnan(lo[2])


def test_nothing_is_kept_behind_the_end_of_the_frame(tmp_path):
    """Behind a convex corner the frame ends at the centre of its arc: what lies further belongs to the other
    face of the wall, and is not kept even when the band asked goes deeper. A wall without its face for 0.4 m
    either side of the corner: the rays of the arc go through to the back of the wall."""
    _doc, ch, info = model_doc(tmp_path, make_wall_scene(), drop=(5.6, 6.4))
    frame = WallPath(np.asarray(info["face"]), radius=0.2)
    rc = raycaster_for_chunk(ch)
    surf = E.FrameSurface(ch, rc, frame, RES, threading.Lock(), (-0.8, 0.5), (0.0, 100.0, frame.length, 102.0))
    mid = frame.corners()[0]["u"]
    u, v = np.array([mid - 0.05, mid + 0.05]), np.array([101.0, 101.0])
    lo, hi = surf.band(u)
    assert np.allclose(lo, -(0.2 - CLEARANCE), atol=1e-6) and np.allclose(hi, 0.5)
    # the model has a surface on those rays, inside the band asked and behind the end of the frame
    found = hi - rc.hit_distance(ch.to_internal(frame.point(u, v, hi)), -frame.view(u, v))
    assert ((found > -0.8) & (found < lo - 0.3)).all()
    assert np.isnan(surf.depth_at(u, v)).all()
    # where the wall has its face the depth is the face's, and the limit behind is the band's
    assert surf.depth_at(np.array([3.0]), np.array([101.0])) == pytest.approx([0.0], abs=1e-4)
    assert surf.band(np.array([3.0]))[0] == pytest.approx([-0.8])


def test_u_is_the_length_on_the_face(model):
    ch, info = model
    frame = path_of(info)
    rc, lock = raycaster_for_chunk(ch), threading.Lock()
    part = (-1.0, 100.0, frame.length + 1.0, 102.0)
    pairs = np.array(E.face_map(E.FrameSurface(ch, rc, frame, RES, lock, (-0.5, 0.5), part)))
    assert pairs.shape[1] == 2 and (np.diff(pairs, axis=0) > 0).all() and len(pairs) < 200
    assert pairs[0, 0] == pytest.approx(-1.0) and pairs[-1, 0] == pytest.approx(frame.length + 1.0)
    mapped = WallPath(frame.vertices, radii=frame.radii, u_map=pairs)
    corner = frame.corners()[0]
    start, end = corner["s"] - 0.52 * np.pi / 4, corner["s"] + 0.52 * np.pi / 4       # where the arc joins the runs
    s = np.array([-1.0, 0.0, 3.0, start, corner["s"], end, 8.0 - (10.0 - frame.length), frame.length,
                  frame.length + 1.0])
    assert np.abs(mapped.u_of(s) - [-1.0, 0.0, 3.0, 5.48, 6.0, 6.52, 8.0, 10.0, 11.0]).max() < 1e-3
    # the wall where the model has a hole, and past its ends, is the line itself: u does not jump
    _doc, holed, _info = model_doc(ch.document.path.parent / "buco", make_wall_scene(), drop=(2.0, 2.6))
    again = np.array(E.face_map(E.FrameSurface(holed, raycaster_for_chunk(holed), frame, RES, lock, (-0.3, 0.5),
                                               part)))
    assert np.abs(np.interp(s, again[:, 0], again[:, 1]) - mapped.u_of(s)).max() < 2e-3
    assert np.abs(np.interp([2.0, 2.3, 2.6], again[:, 0], again[:, 1]) - [2.0, 2.3, 2.6]).max() < 1e-3
    # a trace clicked 0.2 m in front of the face: u is still the length on the face, corner included
    clicked = np.asarray(info["face"]) + [[0.0, -0.2], [0.2, -0.2], [0.2, 0.0]]
    off = WallPath(clicked, radii=corner_radii(clicked))
    pairs = np.array(E.face_map(E.FrameSurface(ch, rc, off, RES, lock, (-0.5, 0.5),
                                               (0.0, 100.0, off.length, 102.0))))
    marks = np.array([m[2] for m in info["marks"]])
    u = WallPath(clicked, radii=off.radii, u_map=pairs).coords(marks)[:, 0]
    assert np.abs(u - [m[0] for m in info["marks"]]).max() < 2e-3
    # no surface in the band at all, a frame that is not a path, a path that has its map already
    with pytest.raises(ValueError, match="no surface"):
        E.face_map(E.FrameSurface(ch, rc, frame, RES, lock, (0.6, 0.9), part))
    a, b = np.asarray(info["face"][0]), np.asarray(info["face"][1])
    with pytest.raises(ValueError, match="path"):
        E.face_map(E.FrameSurface(ch, rc, E.plane_from_line(ch, a, b, (a + b) / 2 + [0.0, -3.0]), RES, lock,
                                  (-0.5, 0.5), (0.0, 100.0, 1.0, 101.0)))
    with pytest.raises(ValueError, match="map"):
        E.face_map(E.FrameSurface(ch, rc, mapped, RES, lock, (-0.5, 0.5), part))


def test_the_heights_of_the_wall_are_read_on_the_model(tmp_path):
    _doc, ch, info = model_doc(tmp_path, make_wall_scene(height=1.83))
    frame = path_of(info)
    assert E.height_range(ch, frame, (-0.5, 0.5)) == pytest.approx((100.0, 101.85), abs=1e-9)   # outward, to 5 cm
    assert E.height_range(ch, frame, (-0.5, 0.5), span=(1.0, 2.0)) == pytest.approx((100.0, 101.85), abs=1e-9)
    assert E.height_range(ch, frame, (-0.1, 0.1), span=(-3.0, 1.0)) == pytest.approx((100.0, 101.85), abs=1e-9)
    with pytest.raises(ValueError, match="band"):
        E.height_range(ch, frame, (0.6, 0.9))                        # nothing of the model in front of the face
    with pytest.raises(ValueError, match="band"):
        E.height_range(ch, frame, (-0.5, 0.5), span=(frame.length + 1.0, frame.length + 2.0))   # past the end
    # the 0.5 and the 99.5 percentiles of the vertices along the line: of 10,000, the 30 far below and the 30 far
    # above are not the wall, the 170 of a footing under it and the 170 of a pier over it are, down to the 20th of
    # them from each end (0.44 m under, 1.88 m over, rounded outward). The vertices past the end of the line and
    # those behind its band, all 9 m up, are not of it
    z = np.concatenate([np.full(30, -1.0), np.linspace(-0.5, 0.0, 170), np.linspace(0.0, 1.0, 9600),
                        np.linspace(1.0, 2.0, 170), np.full(30, 3.5)])
    x = np.random.default_rng(5).permutation(np.linspace(0.05, 5.95, len(z)))
    past = np.column_stack([np.linspace(6.05, 6.45, 80), np.zeros(80), np.full(80, 9.0)])
    behind = np.column_stack([np.linspace(1.0, 5.0, 80), np.full(80, 0.4), np.full(80, 9.0)])
    cloud = _model_of(tmp_path / "vertici", np.vstack([np.column_stack([x, np.zeros(len(z)), z]), past, behind]),
                      [[0, 1, 2]])
    line = WallPath([[0.0, 0.0], [6.0, 0.0]])
    assert E.height_range(cloud, line, (-0.2, 0.5)) == pytest.approx((-0.45, 1.9), abs=1e-9)
    assert E.height_range(cloud, line, (-0.2, 0.5), span=(0.0, 6.5))[1] == pytest.approx(9.0)    # with those past
    assert E.height_range(cloud, line, (-0.5, 0.5))[1] == pytest.approx(9.0)                     # with those behind


def test_a_path_is_built_as_asked(model):
    ch, info = model
    frame = path_of(info)
    kw = dict(resolution=0.05)
    wall = E.build_elevation(ch, frame, **kw)
    assert wall["scale"] == "wall" and wall["depth"] == [-0.5, 0.5] and wall["name"] == "Prospetto del muro"
    assert wall["v_is_height"] is True and "extend" not in wall and wall["kind"] == "elevation"
    assert wall["bounds"] == pytest.approx([0.0, 100.0, 10.0, 102.0], abs=2e-3)
    measured = E.frame_from_dict(wall["frame"])
    assert measured.u_map is not None and np.array_equal(measured.vertices, frame.vertices)
    saved = json.loads((ch.data_dir / "elevations" / wall["id"] / "frame.json").read_text())
    assert saved["frame"] == wall["frame"] and saved["v_is_height"] is True and saved["bounds"] == wall["bounds"]
    with rasterio.open(E.elevation_paths(ch, wall)[1]) as src:
        assert json.loads(src.tags()["ppg_frame"]) == wall["frame"] and src.crs is None
    # on the arc length of the trace: no map, u ends at the length of the line
    trace = E.build_elevation(ch, measured, scale="trace", **kw)
    assert "scale" not in trace and "u_map" not in trace["frame"]
    assert trace["bounds"][2] == pytest.approx(frame.length) and E.wall_length(trace) == pytest.approx(frame.length)
    # the frame as it is: with its map, and without one
    kept = E.build_elevation(ch, measured, scale="keep", **kw)
    assert kept["scale"] == "wall" and kept["frame"] == wall["frame"] and kept["bounds"] == wall["bounds"]
    plain = E.build_elevation(ch, frame, scale="keep", **kw)
    assert "scale" not in plain and plain["frame"] == trace["frame"] and plain["size"] == trace["size"]
    # a map given is the grid kept, whatever the wall measured again would say
    given = [[0.0, 0.0], [frame.length, 20.0]]
    stretched = E.build_elevation(ch, frame, u_map=given, **kw)
    assert stretched["scale"] == "wall" and stretched["frame"]["u_map"] == given
    assert stretched["bounds"][2] == pytest.approx(20.0) and E.wall_length(stretched) == pytest.approx(20.0)
    # the part asked: heights and an extension of the trace, or bounds in metres of u
    part = E.build_elevation(ch, frame, heights=(100.5, 101.5), extend=(0.5, 2.0), **kw)
    assert part["bounds"] == pytest.approx([-0.5, 100.5, 12.0, 101.5], abs=2e-3) and part["extend"] == [0.5, 2.0]
    assert E.wall_length(part) == pytest.approx(10.0, abs=2e-3)      # of the wall, not of the part built
    boxed = E.build_elevation(ch, frame, bounds=(2.0, 100.5, 7.0, 101.0), name="Angolo", **kw)
    assert boxed["bounds"] == [2.0, 100.5, 7.0, 101.0] and boxed["name"] == "Angolo" and boxed["scale"] == "wall"
    assert E.frame_from_dict(boxed["frame"]).corners()[0]["u"] == pytest.approx(6.0, abs=2e-3)
    assert np.isfinite(relief_of(ch, boxed)).mean() > 0.95


def test_gaps_in_the_model_and_an_extension_beyond_it(tmp_path):
    _doc, ch, info = model_doc(tmp_path, make_wall_scene(), drop=(2.0, 2.6))
    # the back of the wall, 0.4 m behind its face, is out of the band: the hole is a hole
    entry = E.build_elevation(ch, path_of(info), resolution=RES, depth=(-0.3, 0.5), extend=(1.0, 1.0))
    u0, v0, u1, v1 = entry["bounds"]
    assert abs((u1 - u0) - 12.0) < 0.02 and abs(u0 + 1.0) < 0.01 and (v0, v1) == pytest.approx((100.0, 102.0))
    assert abs(entry["size"][0] * RES - 12.0) < 0.05 and entry["extend"] == [1.0, 1.0]
    d = relief_of(ch, entry)
    for a, b in ((u0, -0.02), (2.02, 2.58), (10.02, u1)):          # before the wall, its hole, past its end
        cols = slice(*cols_of(entry, a, b))
        assert cols.stop - cols.start >= 50 and np.isnan(d[:, cols]).all()
    rows = slice(*rows_of(entry, 100.05, 101.95))
    for a, b in ((0.02, 1.98), (2.62, 9.98)):                       # the wall, up to the hole and to its ends
        assert np.isfinite(d[rows, slice(*cols_of(entry, a, b))]).all()
    frame = E.frame_from_dict(entry["frame"])
    marks = {m[0]: m[2] for m in info["marks"]}
    u = frame.coords(np.array([marks[3.0], marks[8.0]]))[:, 0]
    assert np.abs(u - [3.0, 8.0]).max() <= RES                      # u does not jump across the gap
    assert abs(E.wall_length(entry) - 10.0) < 0.01


@pytest.mark.parametrize("viewer", ["right", "left"])
@pytest.mark.parametrize("trace, length",
                         [(JOG, 6.3), (TURN, 8.0), (BUTTRESS, 8.2), (ZIGZAG, 8.0 + np.hypot(2.6, 1.5))],
                         ids=["jog", "turn", "buttress", "zigzag"])
def test_a_jog_and_a_sharp_turn_have_no_gap(trace, length, viewer, tmp_path):
    _doc, ch, info = model_doc(tmp_path, make_wall_scene(trace=trace, viewer=viewer))
    entry = E.build_elevation(ch, path_of(info), resolution=RES)
    assert abs(E.wall_length(entry) - length) < 0.01
    d = relief_of(ch, entry)
    rows, cols = slice(*rows_of(entry, 100.05, 101.95)), slice(*cols_of(entry, 0.02, length - 0.02))
    assert cols.stop - cols.start > 100 * (length - 0.1)
    assert np.isfinite(d[rows, cols]).mean(axis=0).min() > 0.98     # every column between the ends has the wall
    # the face is where the frame says, all along it: u is the length on the face within a millimetre or two
    along = np.arange(0.03, length - 0.03, 0.05)
    face = np.asarray(info["face"])
    run = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(face, axis=0).T))])
    xyz = np.column_stack([np.interp(along, run, face[:, 0]), np.interp(along, run, face[:, 1]),
                           np.full(len(along), 101.0)])
    u = E.frame_from_dict(entry["frame"]).coords(xyz)[:, 0]
    assert np.abs(u - (along if viewer == "right" else length - along)).max() < 0.002


@pytest.mark.parametrize("viewer", ["right", "left"])
def test_a_curved_wall_keeps_its_length(viewer, tmp_path):
    """A wall of 3 m radius clicked 61 times, from outside and from inside: the marks at their face length within
    a cell on a curve too. The chords of the simplified face alone would make the wall 2 cm shorter."""
    _doc, ch, info = model_doc(tmp_path, make_wall_scene(trace=ARC, viewer=viewer))
    entry = E.build_elevation(ch, path_of(info), resolution=RES)
    length = 60 * 6.0 * np.sin(np.radians(1.0))
    assert abs(E.wall_length(entry) - length) < 0.002
    frame = E.frame_from_dict(entry["frame"])
    marks = [m for m in info["marks"]]
    assert [m[0] for m in marks] == [1.0, 3.0, 5.85, 6.15]
    u = frame.coords(np.array([m[2] for m in marks]))[:, 0]
    want = np.array([m[0] if viewer == "right" else length - m[0] for m in marks])
    assert np.abs(u - want).max() <= RES and np.abs(u - want).max() < 0.002
    d = relief_of(ch, entry)
    rows, cols = slice(*rows_of(entry, 100.05, 101.95)), slice(*cols_of(entry, 0.02, length - 0.02))
    assert np.isfinite(d[rows, cols]).all() and np.abs(d[rows, cols]).max() < 1e-3


def test_a_narrow_room_is_not_seen_through_its_other_wall(tmp_path):
    """A corridor 60 cm wide seen from inside, with a band asked 1 m deep in front of the face: the frame ends
    half way across, so each wall is seen from there and never from behind the wall that faces it - no stone
    twice."""
    _doc, ch, info = model_doc(tmp_path, make_wall_scene(trace=CORRIDOR))
    frame = path_of(info)
    assert [c["radius"] for c in frame.corners()] == pytest.approx([0.3, 0.3])       # they share the short side
    entry = E.build_elevation(ch, frame, resolution=RES, depth=(-0.5, 1.0))
    assert abs(E.wall_length(entry) - 6.6) < 0.01
    d = relief_of(ch, entry)
    rows, cols = slice(*rows_of(entry, 100.05, 101.95)), slice(*cols_of(entry, 0.02, 6.58))
    assert np.isfinite(d[rows, cols]).mean(axis=0).min() > 0.98
    # the face is on the line, the two corners 12 cm behind their arcs: nothing in front of the line is taken for it
    assert np.nanmax(d[rows, cols]) < 1e-3 and np.nanmin(d[rows, cols]) > -0.3 * (np.sqrt(2.0) - 1.0) - 1e-3
    for a, b in ((0.1, 2.6), (4.0, 6.5)):
        assert np.abs(d[rows, slice(*cols_of(entry, a, b))]).max() < 1e-3


def test_a_narrow_room_painted_from_its_texture_shows_each_wall_once(tmp_path):
    """The same corridor as a textured model without photos, each of its three walls of one colour: the
    orthophoto from the texture has every wall in its own colour - a ray never starts behind the wall that faces
    it."""
    from engine_gaps_helpers import checker, grid_mesh, new_doc, textured_chunk

    colours = [(220, 60, 60), (60, 200, 60), (60, 60, 220)]
    xyz, faces, uv, page = [], [], [], []
    for k, (p, q) in enumerate(zip(CORRIDOR[:-1], CORRIDOR[1:])):
        square, f, t = grid_mesh(0.0, 0.0, 1.0, 1.0, 12, 8)         # along the wall, up the wall
        p, q = np.asarray(p), np.asarray(q)
        faces.append(f + sum(len(v) for v in xyz))
        xyz.append(np.column_stack([p[0] + square[:, 0] * (q[0] - p[0]), p[1] + square[:, 0] * (q[1] - p[1]),
                                    2.0 * square[:, 1]]))
        uv.append(t)
        page.append(np.full(len(f), k))
    ch = textured_chunk(new_doc(tmp_path), "Corridoio", np.vstack(xyz), np.vstack(faces), np.vstack(uv),
                        [checker(64, 64, c, cells=1) for c in colours], np.concatenate(page))
    entry = E.build_elevation(ch, WallPath(CORRIDOR, radii=corner_radii(CORRIDOR)), resolution=0.02,
                              depth=(-0.3, 1.0), heights=(0.0, 2.0))
    assert entry["source"] == "texture" and entry["scale"] == "wall" and abs(E.wall_length(entry) - 6.6) < 0.01
    rgba = ortho_of(ch, entry)
    rows = slice(*rows_of(entry, 0.1, 1.9))
    for (a, b), colour in zip(((0.1, 2.8), (3.1, 3.5), (3.8, 6.5)), colours):
        cells = rgba[:, rows, slice(*cols_of(entry, a, b))].reshape(4, -1)
        assert (cells[3] == 255).all() and np.abs(cells[:3].T.astype(int) - colour).max() < 12


def test_a_ray_along_the_end_of_the_wall_adds_no_length(tmp_path):
    """The column on the very end of a wall may find any depth: its rays run along the return of the wall. With a
    sliver 0.2 m behind the face that the last column alone sees, and with a first column of the wall that answers
    0.3 m, the wall is as long as its face and ``u`` does not jump where the wall starts."""
    scene = _added(make_wall_scene(), [[5.8, 4.0005, 0.0], [5.8, 4.0015, 0.0], [5.8, 4.0015, 2.0],
                                       [5.8, 4.0005, 2.0]], [[0, 1, 2], [0, 2, 3]])
    _doc, ch, info = model_doc(tmp_path, scene)
    face = np.asarray(info["face"]) + [[0.0, 0.0], [0.0, 0.0], [0.0, 0.001]]     # the trace ends 1 mm past the wall
    frame = WallPath(face, radii=corner_radii(face))
    rc, lock = raycaster_for_chunk(ch), threading.Lock()
    surf = E.FrameSurface(ch, rc, frame, RES, lock, (-0.5, 0.5), (0.0, 100.0, frame.length, 102.0))
    ends = surf.depth_at(np.array([frame.length - 0.005, frame.length]), np.array([101.0, 101.0]))
    assert ends == pytest.approx([0.0, -0.2], abs=1e-4)             # the face, then the sliver
    assert abs(E.face_map(surf)[-1][1] - 10.001) < 0.002
    # the first column of the wall, in a part prolonged before it: s = 0 is one of its columns
    plain = path_of(info)
    part = E.FrameSurface(ch, rc, plain, RES, lock, (-0.5, 0.5), (-1.0, 100.0, 9.0, 102.0))
    real = part.depth_at

    def grazing(u, v):
        d = real(u, v)
        d[np.abs(u) < 1e-9] = -0.3
        return d

    part.depth_at = grazing
    pairs = np.array(E.face_map(part))
    assert (part.depth_at(np.zeros(1), np.full(1, 101.0)) == -0.3).all()
    want = [-1.0, 0.0, 3.0, 9.0 + (10.0 - plain.length)]
    assert np.abs(np.interp([-1.0, 0.0, 3.0, 9.0], pairs[:, 0], pairs[:, 1]) - want).max() < 0.002


def test_rough_ground_in_a_gap_and_past_the_ends_is_not_the_wall(tmp_path):
    """The ground is not a plane: the rays of the lowest row of an elevation meet its bumps, anywhere in the band.
    Where the wall has a gap, and where the trace is prolonged past the wall, that one row is not the face: the
    column is empty, ``u`` goes on as the line does and does not wander with the ground."""
    scene = _on_rough_ground(make_wall_scene(trace=[[0.0, 0.0], [8.0, 0.0]]))
    _doc, ch, info = model_doc(tmp_path / "varco", scene, drop=(3.4, 4.4))            # a gap of a metre
    entry = E.build_elevation(ch, path_of(info), resolution=RES, depth=(-0.3, 0.5), extend=(0.5, 0.5),
                              heights=(100.0, 102.0))
    assert abs(E.wall_length(entry) - 8.0) < 0.01
    assert entry["bounds"][0] == pytest.approx(-0.5, abs=0.01) and entry["bounds"][2] == pytest.approx(8.5, abs=0.01)
    marks = [m for m in info["marks"] if m[0] > 4.4]                                   # those past the gap
    u = E.frame_from_dict(entry["frame"]).coords(np.array([m[2] for m in marks]))[:, 0]
    assert [m[0] for m in marks] == [5.85, 6.15, 8.0] and np.abs(u - [5.85, 6.15, 8.0]).max() <= RES
    d = relief_of(ch, entry)
    gap = slice(*cols_of(entry, 3.42, 4.38))
    assert np.isfinite(d[-3:, gap]).any()                                              # the ground is there
    assert np.isnan(d[slice(*rows_of(entry, 100.05, 101.95)), gap]).all()              # and nothing over it
    # a wall of 6 m, its trace prolonged by 3 m either way over the ground
    _doc, ch, info = model_doc(tmp_path / "oltre", _on_rough_ground(make_wall_scene(trace=[[0.0, 0.0], [6.0, 0.0]])))
    entry = E.build_elevation(ch, path_of(info), resolution=RES, extend=(3.0, 3.0), heights=(100.0, 102.0))
    assert abs(E.wall_length(entry) - 6.0) < 0.01
    assert entry["bounds"][0] == pytest.approx(-3.0, abs=0.02) and entry["bounds"][2] == pytest.approx(9.0, abs=0.02)


def test_a_tenth_of_the_heights_makes_a_column_of_the_face(tmp_path):
    """Two gaps in a wall. In the first what is left of it stands over 15 % of the heights asked, 0.2 m in front
    of the line: it is the face there, and the wall runs out to it and back. In the second it stands over 5 % of
    them: no more a face than the ground is, the line is carried across."""
    scene = make_wall_scene(trace=[[0.0, 0.0], [8.0, 0.0]])
    gone = (scene.face_columns >= 5.0) & (scene.face_columns < 6.0)
    scene.faces, scene.face_columns = scene.faces[~gone], scene.face_columns[~gone]
    for x0, x1, top in ((2.0, 3.0, 0.3), (5.0, 6.0, 0.1)):
        _added(scene, [[x0, -0.2, 0.0], [x1, -0.2, 0.0], [x1, -0.2, top], [x0, -0.2, top]], [[0, 1, 2], [0, 2, 3]])
    _doc, ch, info = model_doc(tmp_path, scene, drop=(2.0, 3.0))
    entry = E.build_elevation(ch, path_of(info), resolution=RES, depth=(-0.3, 0.5), heights=(100.0, 102.0))
    on_the_face = np.array([[1.0, 0.0, 1.0], [4.0, 0.0, 1.0], [7.0, 0.0, 1.0]]) + info["shift"]
    u = E.frame_from_dict(entry["frame"]).coords(on_the_face)[:, 0]
    assert np.abs(u - [1.0, 4.0 + 0.4, 7.0 + 0.4]).max() < 0.003 and abs(E.wall_length(entry) - 8.4) < 0.005


def test_the_face_of_a_column_is_what_most_of_its_height_shows(tmp_path):
    """The depth of a column is the median over its heights. A bench against the foot of the wall, a quarter of its
    height, stands in front of the face and is not the face: the wall is no longer for it. A pier three quarters
    of its height is the face there: the wall runs round it, 0.3 m out and 0.3 m back."""
    scene = make_wall_scene(trace=[[0.0, 0.0], [8.0, 0.0]])
    for x0, x1, top in ((2.0, 4.0, 0.5), (5.0, 6.0, 1.5)):                             # 0.3 m in front of the face
        _added(scene, [[x0, -0.3, 0.0], [x1, -0.3, 0.0], [x1, -0.3, top], [x0, -0.3, top]], [[0, 1, 2], [0, 2, 3]])
    _doc, ch, info = model_doc(tmp_path, scene)
    entry = E.build_elevation(ch, path_of(info), resolution=RES, heights=(100.0, 102.0))
    on_the_face = np.array([[1.0, 0.0, 1.9], [4.5, 0.0, 1.9], [7.0, 0.0, 1.9]]) + info["shift"]
    u = E.frame_from_dict(entry["frame"]).coords(on_the_face)[:, 0]
    assert np.abs(u - [1.0, 4.5, 7.0 + 0.6]).max() < 0.003
    assert abs(E.wall_length(entry) - 8.6) < 0.005


def test_the_edge_of_a_stretch_of_surface_takes_the_depth_inside_it():
    """The column on each edge of a stretch of surface takes the depth of the column next to it, inside the
    stretch; a stretch of one or two columns has no inside, and stays as measured."""
    nan = np.nan
    measured = np.array([nan, 1.0, 2.0, nan, 3.0, 4.0, 5.0, 6.0, nan, 7.0, nan, 8.0, 9.0, 10.0])
    settled = np.array([nan, 1.0, 2.0, nan, 4.0, 4.0, 5.0, 5.0, nan, 7.0, nan, 9.0, 9.0, 9.0])
    assert np.array_equal(E._settled(measured), settled, equal_nan=True)
    assert np.array_equal(E._settled(np.array([1.0, 2.0])), [1.0, 2.0]) and np.isnan(E._settled(np.full(3, nan))).all()


def test_a_column_halved_onto_the_edge_of_the_wall_is_an_edge_too(tmp_path):
    """In front of a corner the columns are halved, the one between the last empty column and the first of the
    wall too: the new column, on the very edge of the wall, may find any depth as the first one may. Depths given
    by hand: nothing before an edge in the arc of the corner, 0.2 m in front of the line after it and, on the
    edge itself, nothing or 0.25 m behind the line. The length on the face is the same."""
    _doc, ch, info = model_doc(tmp_path, make_wall_scene())                            # any chunk: no ray is cast
    frame = WallPath(np.asarray(info["face"]), radius=0.3)
    columns = np.linspace(0.0, frame.length, int(np.ceil(frame.length / E.FACE_COLUMN - 1e-9)) + 1)
    k = int(np.searchsorted(columns, frame.corners()[0]["s"]))
    edge = (columns[k - 1] + columns[k]) / 2.0                     # half way between two columns of the first pass
    surf = E.FrameSurface(ch, raycaster_for_chunk(ch), frame, RES, threading.Lock(), (-0.5, 0.5),
                          (0.0, 100.0, frame.length, 102.0))
    asked = []

    def by_hand(on_the_edge):
        def depth_at(u, v):
            asked.extend(np.unique(u).tolist())
            d = np.where(u > edge, 0.2, np.nan)
            d[np.abs(u - edge) < 1e-9] = on_the_edge
            return d
        return depth_at

    surf.depth_at = by_hand(np.nan)
    clean = np.array(E.face_map(surf))
    assert np.abs(np.asarray(asked) - edge).min() < 1e-9             # the edge was halved onto
    surf.depth_at = by_hand(-0.25)
    found = np.array(E.face_map(surf))
    s = np.linspace(0.0, frame.length, 500)
    assert np.abs(np.interp(s, found[:, 0], found[:, 1]) - np.interp(s, clean[:, 0], clean[:, 1])).max() < 1e-3


def test_the_face_line_bows_with_the_wall_not_with_its_roughness():
    """Between two points the simplification keeps, the face line is as long as the wall that curves between
    them, and as long as their chord where the face only has a bump."""
    turn = np.radians(np.linspace(-10.0, 10.0, 201))                # 20 degrees of a wall of 3 m radius
    arc = 3.0 * np.column_stack([np.sin(turn), np.cos(turn)])
    u = E._along_face(arc, np.array([0, 200]))
    assert u[0] == 0.0 and (np.diff(u) > 0).all()
    assert np.abs(u - 3.0 * (turn - turn[0])).max() < 1e-4          # the arc, where its chord is 5.3 mm short
    assert 3.0 * (turn[-1] - turn[0]) - np.hypot(*(arc[-1] - arc[0])) > 5e-3
    x = np.linspace(0.0, 0.2, 101)
    bump = np.column_stack([x, 0.04 * np.sin(np.pi * x / 0.2)])     # 4 cm over 20 cm: a stone, not a curve
    assert np.allclose(E._along_face(bump, np.array([0, 100])), x, atol=1e-12)
    # several pieces: each on its own chord, the lengths added
    corner = np.array([[0.0, 0.0], [1.0, 0.0], [2.0, 0.0], [2.0, 1.0], [2.0, 3.0]])
    assert np.allclose(E._along_face(corner, np.array([0, 2, 4])), [0.0, 1.0, 2.0, 3.0, 5.0])


def _ribbed(folder, depth: float, length: float = 8.0, every: float = 0.2):
    """A chunk whose model is a straight wall along x, 2 m high, with vertical ribs: its face goes ``depth`` in and
    out of the line y = 0 once every ``every`` metres. Returns the chunk and the length of the face."""
    x = np.linspace(0.0, length, int(round(length / 0.005)) + 1)
    y = depth * np.sin(2.0 * np.pi * x / every)
    xyz = np.column_stack([np.tile(x, 2), np.tile(y, 2), np.repeat([0.0, 2.0], len(x))])
    low, high = np.arange(len(x) - 1), np.arange(len(x) - 1) + len(x)
    faces = np.vstack([np.column_stack([low, low + 1, high + 1]), np.column_stack([low, high + 1, high])])
    return _model_of(folder, xyz, faces, "Muro a coste"), float(np.hypot(np.diff(x), np.diff(y)).sum())


def test_roughness_under_five_centimetres_adds_no_length(tmp_path):
    frame = WallPath([[0.0, 0.0], [8.0, 0.0]])
    for depth, counted in ((0.02, False), (0.06, True)):           # 4 cm and 12 cm from crest to trough
        ch, face = _ribbed(tmp_path / f"{depth}", depth)
        surf = E.FrameSurface(ch, raycaster_for_chunk(ch), frame, RES, threading.Lock(), (-0.5, 0.5),
                              (0.0, 0.0, 8.0, 2.0))
        pairs = np.array(E.face_map(surf))
        assert face > 8.5 and pairs[0].tolist() == [0.0, 0.0] and (np.diff(pairs, axis=0) > 0).all()
        if counted:                                                 # ribs deeper than the tolerance are the wall
            assert pairs[-1, 1] > 8.5
        else:                                                       # the wall is as long as its line
            assert abs(pairs[-1, 1] - 8.0) < 0.002 and len(pairs) == 2


def test_tile_follows_the_frame_and_patches_paint_across_the_corner(built, monkeypatch):
    from shapely import contains_xy
    from shapely.geometry import box

    from elevation_helpers import colour_of, plain, prepare

    assert E.frame_tile(E.Plane([0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]), 0.005) == 512
    assert E.frame_tile(E.Cylinder([0.0, 0.0, 0.0], [0.0, 0.0, 1.0], 5.0), 0.005) == 150
    assert E.frame_tile(WallPath(L, radius=0.52), 0.005) == 64
    assert E.frame_tile(WallPath(L, radius=0.5), 0.0007) == 214    # 0.3 of the smallest fillet
    assert E.frame_tile(WallPath(L, radius=5.0), 0.001) == 512     # a fillet of 2 m, half the short side: 600
    assert E.frame_tile(WallPath(L[:2]), 0.005) == 512
    ch, _info, entry = built["outside"]
    prepare(ch)
    plain(monkeypatch)                                              # every photo a plain colour of its own
    from pyarchinit_photogrammetry.engine import ortho

    tiles, context = [], ortho._context                             # the tile the mosaic is given, each time
    monkeypatch.setattr(ortho, "_context", lambda *a, **k: (tiles.append(a[11]), context(*a, **k))[1])
    part = E.build_elevation(ch, E.frame_from_dict(entry["frame"]), resolution=RES, max_angle=89.0, scale="keep",
                             bounds=(5.0, 100.2, 7.0, 101.8), name="Angolo")
    assert tiles == [64]                                            # the frame's, not the 512 of a plane
    try:
        eid = part["id"]
        poly = box(5.6, 100.5, 6.4, 101.5)                          # 0.4 m of each face, across the corner
        first = P.photos_for_area(ch, poly, frame=eid)[0]
        assert first["coverage"] > 0.9
        before, t = ortho_with_transform(ch, part)
        P.add_patch(ch, poly, [first["label"]], frame=eid)
        del tiles[:]
        after = _read(ch.update_elevation(eid))[0]
        assert tiles == [64]                                        # as built
        rows, cols = np.mgrid[0:after.shape[1], 0:after.shape[2]]
        u, v = t.c + (cols + 0.5) * RES, t.f - (rows + 0.5) * RES
        inside = contains_xy(poly.buffer(-0.015), u, v) & np.isfinite(relief_of(ch, part))
        colour = np.array(colour_of(ch.camera(first["label"]).key)[::-1])

        def share(img):
            return (np.abs(img[:3, inside].T.astype(int) - colour).max(axis=1) <= 1).mean()

        assert inside.sum() > 6000 and share(before) < 0.8          # the mosaic takes each face from its photos
        assert share(after) > 0.9
        changed = (after != before).any(axis=0)
        assert changed[inside].any() and not changed[~contains_xy(poly.buffer(2 * RES), u, v)].any()
    finally:
        E.remove_elevation(ch, part["id"])


def test_reopened_project_keeps_the_grid(tmp_path):
    import pyarchinit_photogrammetry as ppg

    doc, ch, info = model_doc(tmp_path, make_wall_scene())
    entry = E.build_elevation(ch, path_of(info), resolution=0.02, extend=(0.5, 0.0))
    first = relief_of(ch, entry)
    doc.save()
    ch = ppg.Document.open(doc.path).chunk
    (again,) = E.elevations(ch)
    assert again == json.loads(json.dumps(entry)) and again["frame"] == entry["frame"]
    surf = E.frame_grid(ch, again, raycaster_for_chunk(ch), threading.Lock())         # the file is the entry's grid
    assert surf.frame.kind == "path" and list(surf.shape) == again["size"][::-1]
    assert np.array_equal(surf.frame.u_map, E.frame_from_dict(entry["frame"]).u_map)
    like = E.build_like(ch, again)
    assert like["size"] == again["size"] and like["bounds"] == again["bounds"] and like["frame"] == again["frame"]
    assert like["grid_of"] == again["id"] and like["scale"] == "wall"
    with rasterio.open(E.elevation_paths(ch, again)[1]) as a, rasterio.open(E.elevation_paths(ch, like)[1]) as b:
        assert a.transform == b.transform and a.shape == b.shape
    assert np.array_equal(relief_of(ch, like), first, equal_nan=True)


def test_orient_moves_an_upright_path_and_refuses_a_tilt(tmp_path):
    import shapely
    from orient_helpers import motion, rotation

    _doc, ch, info = model_doc(tmp_path, make_wall_scene(), crs="LOCAL", shift=(0.0, 0.0, 0.0))
    entry = E.build_elevation(ch, path_of(info), resolution=0.02, extend=(0.5, 0.5), name="Muro nord")
    eid = entry["id"]
    unit = shapely.Polygon([(5.6, 0.5), (6.4, 0.5), (6.4, 1.5), (5.6, 1.5)])          # across the corner
    ch.meta.setdefault("drawings", {}).setdefault("frames", {})[eid] = {"units": [{"wkt": unit.wkt, "us": "7"}]}
    before = json.loads(json.dumps(entry))
    old = E.frame_from_dict(before["frame"])
    grid = _read(E.elevation_paths(ch, entry)[1])[1]
    m = motion(rotation([0.0, 0.0, 1.0], 30.0), (1.0, 2.0, 0.5))
    orient.drop(orient.apply(ch, m)["state"])
    (moved,) = ch.meta["elevations"]
    new = E.frame_from_dict(moved["frame"])
    assert new.kind == "path" and moved["v_is_height"] is True and not stamps.entry_stale(ch, moved)
    assert np.allclose(new.u_map, old.u_map, rtol=0.0, atol=1e-12) and new.radii == pytest.approx(old.radii, abs=1e-12)
    assert moved["extend"] == pytest.approx([0.5, 0.5], abs=1e-12)
    drawn = shapely.from_wkt(ch.meta["drawings"]["frames"][eid]["units"][0]["wkt"])
    assert np.allclose(np.asarray(drawn.exterior.coords) - np.asarray(unit.exterior.coords), [0.0, 0.5], atol=1e-12)
    for u, v in unit.exterior.coords:
        for depth in (0.0, 0.2):
            want = geo.apply(m, old.point(u, v, depth)[None])[0]
            assert np.abs(new.point(u, v + 0.5, depth) - want).max() < 1e-9
    assert np.allclose(np.asarray(moved["bounds"]) - before["bounds"], [0.0, 0.5, 0.0, 0.5], atol=1e-12)
    for path in E.elevation_paths(ch, moved):
        with rasterio.open(path) as src:
            t = src.transform
            assert json.loads(src.tags()["ppg_frame"]) == moved["frame"]
        assert (t.a, t.e, t.c, t.f) == pytest.approx((grid.a, grid.e, grid.c, grid.f + 0.5), abs=1e-12)
    saved = json.loads((ch.data_dir / "elevations" / eid / "frame.json").read_text())
    assert saved["frame"] == moved["frame"] and saved["bounds"] == moved["bounds"]
    # a motion that does not keep the vertical is refused, and nothing is changed
    transform, meta = np.array(ch.transform), json.dumps(ch.meta, sort_keys=True)
    files = {p: p.read_bytes() for p in (ch.data_dir / "elevations" / eid).iterdir()}
    for tilt, dry in ((motion(rotation([1.0, 0.0, 0.0], 10.0)), False),
                      (motion(rotation([0.0, 1.0, 0.0], 180.0)), True)):         # a tilt, and upside down
        with pytest.raises(orient.OrientRefused) as refused:
            orient.apply(ch, tilt, dry_run=dry)
        assert refused.value.reason == "path_elevations" and refused.value.names == ["Muro nord"]
        assert np.array_equal(ch.transform, transform) and json.dumps(ch.meta, sort_keys=True) == meta
        assert {p: p.read_bytes() for p in (ch.data_dir / "elevations" / eid).iterdir()} == files
    assert "path_elevations" in orient.REASONS
    # the same from three points that tilt the plane; two points keep the horizontal, and the wall follows
    with pytest.raises(orient.OrientRefused) as refused:
        orient.orient(ch, points=[[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.3]])
    assert refused.value.reason == "path_elevations" and refused.value.names == ["Muro nord"]
    assert np.array_equal(ch.transform, transform) and json.dumps(ch.meta, sort_keys=True) == meta
    assert not ch.markers and {p: p.read_bytes() for p in (ch.data_dir / "elevations" / eid).iterdir()} == files
    # with a scale the lengths of the frame grow with it
    orient.drop(orient.apply(ch, motion(None, (0.0, 0.0, 0.0), 2.0))["state"])
    scaled = E.frame_from_dict(ch.meta["elevations"][0]["frame"])
    assert np.allclose(scaled.u_map, 2.0 * new.u_map) and np.allclose(scaled.radii, 2.0 * np.asarray(new.radii))
    assert np.abs(scaled.point(2.0 * 6.2, 3.0, 0.2) - 2.0 * new.point(6.2, 1.5, 0.1)).max() < 1e-9
    assert ch.meta["elevations"][0]["extend"] == pytest.approx([1.0, 1.0], abs=1e-12)


def test_the_refusal_of_a_tilt_is_said_in_the_window():
    pytest.importorskip("PySide6")
    from pyarchinit_photogrammetry.gui import i18n
    from pyarchinit_photogrammetry.gui.orient_dialog import refusal_text, refusal_texts

    assert set(refusal_texts()) >= set(orient.REASONS) and "path_elevations" in orient.REASONS
    text = refusal_text(orient.OrientRefused("path_elevations", "tilted", ["Muro nord", "Muro est"]))
    assert "verticale" in text and text.endswith("Muro nord, Muro est")
    i18n.set_current("en")
    try:
        assert "vertical" in refusal_texts()["path_elevations"]
    finally:
        i18n.set_current("it")


def test_what_a_path_is_not(model):
    ch, info = model
    frame = path_of(info)
    kw = dict(resolution=0.05)
    count = len(E.elevations(ch))
    with pytest.raises(ValueError, match="plane"):
        E.build_elevation(ch, frame, kind="section", **kw)
    with pytest.raises(ValueError, match="path"):
        E.build_elevation(ch, frame, scale="cylinder", **kw)
    with pytest.raises(ValueError, match="unknown scale"):
        E.build_elevation(ch, frame, scale="face", **kw)
    with pytest.raises(ValueError, match="bounds"):
        E.build_elevation(ch, frame, bounds=(0.0, 100.0, 1.0, 101.0), extend=(1.0, 0.0), **kw)
    with pytest.raises(ValueError, match="bounds"):
        E.build_elevation(ch, frame, bounds=(0.0, 100.0, 1.0, 101.0), heights=(100.0, 101.0), **kw)
    with pytest.raises(ValueError, match="extend"):
        E.build_elevation(ch, frame, extend=(-1.0, 0.0), **kw)
    with pytest.raises(ValueError, match="heights"):
        E.build_elevation(ch, frame, heights=(101.0, 100.0), **kw)
    entry = E.build_elevation(ch, frame, **kw)
    with pytest.raises(orient_lines.OrientLinesError):
        orient_lines.elevation_axis(ch, entry["id"])
    a, b = np.asarray(info["face"][0]), np.asarray(info["face"][1])
    plane = E.plane_from_line(ch, a, b, (a + b) / 2 + [0.0, -3.0])
    with pytest.raises(ValueError, match="cylindrical"):
        E.build_elevation(ch, plane, scale="wall", **kw)
    with pytest.raises(ValueError, match="cylindrical"):
        E.build_elevation(ch, plane, u_map=[[0.0, 0.0], [1.0, 1.0]], **kw)
    for other in (plane, E.Cylinder([a[0], a[1], 0.0], [0.0, 0.0, 1.0], 3.0)):
        with pytest.raises(ValueError, match="path"):
            E.build_elevation(ch, other, scale="trace", **kw)
        with pytest.raises(ValueError, match="path"):
            E.build_elevation(ch, other, extend=(1.0, 1.0), **kw)
        with pytest.raises(ValueError, match="path"):
            E.build_elevation(ch, other, heights=(100.0, 101.0), **kw)
    assert len(E.elevations(ch)) == count + 1                       # the refusals built nothing
    assert E.heights_measured(frame) and np.isnan(E.wall_length({"frame": plane.to_dict()}))


def test_the_rays_of_a_textured_path_stop_at_its_band(tmp_path):
    _doc, ch, info = model_doc(tmp_path, make_wall_scene(viewer="left"))
    frame = WallPath(ordered(info["face"], "left"), radius=0.5)
    surf = E.FrameSurface(ch, raycaster_for_chunk(ch), frame, RES, threading.Lock(), (-0.5, 0.5),
                          (0.0, 100.0, frame.length, 102.0))
    rays = texture_ortho.frame_rays(ch, surf)
    mid = frame.corners()[0]["u"]
    u = np.array([1.0, mid])
    rows, cols = np.array([50.5, 120.25]), (u - surf.origin[0]) / RES
    v = surf.origin[1] - rows * RES
    start, direction, max_t = rays(rows, cols)
    assert isinstance(max_t, np.ndarray) and max_t.shape == (2,)
    assert max_t[0] == pytest.approx(1.0 / ch.scale) and max_t[1] == pytest.approx((0.5 - CLEARANCE + 0.5) / ch.scale,
                                                                                    abs=1e-6)
    assert np.allclose(ch.to_world(start), frame.point(u, v, np.array([0.5, 0.5 - CLEARANCE])), atol=1e-6)
    assert np.allclose(direction, -frame.view(u, v))                 # the chunk is the world moved, not turned
    # where the frame leaves no band nothing is seen
    front = E.FrameSurface(ch, raycaster_for_chunk(ch), frame, RES, threading.Lock(), (0.49, 3.0),
                           (0.0, 100.0, frame.length, 102.0))
    assert texture_ortho.frame_rays(ch, front)(rows, cols)[2][1] < 0
    # a plane keeps its one limit for every ray
    a, b = np.asarray(info["face"][0]), np.asarray(info["face"][1])
    flat = E.FrameSurface(ch, raycaster_for_chunk(ch), E.plane_from_line(ch, a, b, (a + b) / 2 + [0.0, 3.0]), RES,
                          threading.Lock(), (-0.2, 0.4), (0.0, 100.0, 1.0, 101.0))
    assert np.allclose(texture_ortho.frame_rays(ch, flat)(rows, cols)[2], 0.6 / ch.scale)
