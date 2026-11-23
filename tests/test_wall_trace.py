"""From a line clicked on the plan to the frame of a wall (``engine.wall_trace``): the side the wall is seen from -
a word, a point of the plan or the photos - the clicked line snapped onto the face of the model, and the frame made
of it. The walls are those of ``testing.synthetic.make_wall_scene``: their model alone (``wall_helpers.model_doc``),
seen from outside the corner and from inside, or with their photos (``wall_doc``)."""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from pyarchinit_photogrammetry.core.progress import Cancelled, Progress
from pyarchinit_photogrammetry.engine import wall_trace as T
from pyarchinit_photogrammetry.engine.wall_path import TraceRefused, WallPath, corner_radii, smooth_trace
from pyarchinit_photogrammetry.testing.synthetic import make_wall_scene

from wall_helpers import distance_to, model_doc, wall_doc

ARC = 3.0 * np.column_stack([np.cos(np.radians(np.linspace(0, 120, 61))), np.sin(np.radians(np.linspace(0, 120, 61)))])
TURN = [[0.0, 0.0], [4.0, 0.0], [4.0 + 4.0 * np.cos(np.radians(120.0)), 4.0 * np.sin(np.radians(120.0))]]
BUTTRESS = [[0.0, 0.0], [3.0, 0.0], [3.0, -0.6], [3.8, -0.6], [3.8, 0.0], [7.0, 0.0]]   # 60 cm to the right of the line
ZIGZAG = [[0.0, 0.0], [4.0, 0.0], [1.4, 1.5], [5.4, 1.5]]                               # two turns of 150 degrees
CORRIDOR = [[0.6, 3.0], [0.6, 0.0], [0.0, 0.0], [0.0, 3.0]]                             # 60 cm wide, seen from inside
OFF = [[0.10, -0.40], [0.25, -0.20], [0.30, -0.15]]      # clicks up to 0.4 m in front of the L, not parallel to it


@pytest.fixture(scope="module")
def model_outside(tmp_path_factory):
    """The model of the default wall alone, seen from outside its corner: ``(chunk, info)``."""
    _doc, ch, info = model_doc(tmp_path_factory.mktemp("fuori"), make_wall_scene())
    return ch, info


@pytest.fixture(scope="module")
def model_inside(tmp_path_factory):
    """The model of the same wall seen from inside its corner (the viewer on the left of its trace)."""
    _doc, ch, info = model_doc(tmp_path_factory.mktemp("dentro"), make_wall_scene(viewer="left"))
    return ch, info


def _added(scene, vertices, faces):
    """``scene`` with more triangles (``faces`` index ``vertices``, local metres). In place; returns the scene."""
    k = len(scene.vertices)
    scene.vertices = np.vstack([scene.vertices, np.asarray(vertices, np.float64)])
    scene.faces = np.vstack([scene.faces, np.asarray(faces) + k]).astype(scene.faces.dtype)
    scene.face_columns = np.concatenate([scene.face_columns, np.full(len(faces), np.nan)])
    return scene


def _block(scene, lo, hi):
    """``scene`` with a block between the corners ``lo`` and ``hi`` (x, y, z, local metres)."""
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    v = [[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0], [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]]
    f = [[0, 1, 2], [0, 2, 3], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5], [2, 3, 7], [2, 7, 6],
         [3, 0, 4], [3, 4, 7]]
    return _added(scene, v, f)


def _rough_ground(scene, lo, hi, rough: float, cell: float = 0.04, seed: int = 3):
    """``scene`` with a ground that is not a plane between the plan corners ``lo`` and ``hi``: a grid of ``cell``
    whose heights are anywhere between 0 and ``rough``."""
    rng = np.random.default_rng(seed)
    gx, gy = np.meshgrid(np.arange(lo[0], hi[0] + cell / 2, cell), np.arange(lo[1], hi[1] + cell / 2, cell))
    xyz = np.column_stack([gx.ravel(), gy.ravel(), rng.uniform(0.0, rough, gx.size)])
    idx = np.arange(gx.size).reshape(gx.shape)
    a, b, c, d = idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel(), idx[1:, 1:].ravel(), idx[1:, :-1].ravel()
    return _added(scene, xyz, np.vstack([np.column_stack([a, b, c]), np.column_stack([a, c, d])]))


def _in_front(face, viewer: str, by: float) -> np.ndarray:
    """The line of a face moved ``by`` metres towards its viewer, its corners where the moved segments meet."""
    face = np.asarray(face, np.float64)
    t = np.diff(face, axis=0)
    t /= np.hypot(*t.T)[:, None]
    n = (1.0 if viewer == "right" else -1.0) * np.column_stack([t[:, 1], -t[:, 0]])
    out = face.copy()
    out[0], out[-1] = face[0] + by * n[0], face[-1] + by * n[-1]
    for i in range(1, len(face) - 1):
        out[i] = face[i] + by * (n[i - 1] + n[i]) / (1.0 + n[i - 1] @ n[i])
    return out


def _stand(chunk, places):
    """The photos of ``chunk`` taken from ``places`` (plan points, local metres), one each; the others not aligned."""
    for cam in chunk.cameras:
        cam.transform = None
    for cam, (x, y) in zip(chunk.cameras, places):
        pose = np.eye(4)
        pose[:3, 3] = [x, y, 1.5]
        cam.transform = pose


# ---------------------------------------------------------------- the snap
def test_a_trace_clicked_off_the_face_is_snapped_onto_it(model_outside):
    ch, info = model_outside
    face = np.asarray(info["face"])
    clicked = face + [[0.10, -0.40], [0.25, -0.20], [0.30, -0.15]]      # up to 0.4 m off, not parallel to the wall
    r = T.snap_trace(ch, clicked, side="right")
    assert r["snapped"] and distance_to(face, r["points"]).max() < 0.01
    assert np.linalg.norm(r["points"][1] - face[1]) < 0.02               # the corner on the corner
    assert 0.38 < r["max_shift"] < 0.42 and all(s["share"] > 0.9 for s in r["segments"])


def test_two_metres_off_is_found_within_the_band_and_not_beyond_it(model_outside):
    ch, info = model_outside
    face = np.asarray(info["face"])
    clicked = face + [[0.0, -2.0], [2.0, -2.0], [2.0, 0.0]]              # the Khutm case: 2 m in front
    assert distance_to(face, T.snap_trace(ch, clicked, side="right")["points"]).max() < 0.01
    r = T.snap_trace(ch, clicked, side="right", band=0.5)
    assert r["snapped"] is False and np.array_equal(r["points"], clicked)
    assert all(s["share"] == 0.0 for s in r["segments"])


def test_what_a_snap_gives_back(model_outside):
    ch, info = model_outside
    face = np.asarray(info["face"])
    clicked = face + OFF
    r = T.snap_trace(ch, clicked.tolist(), side="right")
    assert set(r) == {"points", "snapped", "shift", "median_shift", "max_shift", "segments"}
    assert r["snapped"] is True and r["points"].shape == (3, 2) and r["shift"].shape == (3,)
    assert np.allclose(r["shift"], np.hypot(*(r["points"] - clicked).T)) and np.allclose(r["shift"], [0.40, 0.32, 0.30],
                                                                                        atol=0.005)
    assert r["median_shift"] == pytest.approx(0.32, abs=0.005) and r["max_shift"] == pytest.approx(r["shift"].max())
    assert [set(s) for s in r["segments"]] == [{"offset", "slope", "share"}] * 2
    # an end goes along the normal of its segment: the first click falls where its segment's normal meets the face
    step = clicked[1] - clicked[0]
    normal = np.array([step[1], -step[0]]) / np.hypot(*step)
    assert np.abs(r["points"][0] - (clicked[0] - 0.4 / abs(normal[1]) * normal)).max() < 1e-3
    # not snapped: the points as clicked, nothing moved
    r = T.snap_trace(ch, clicked, side="right", band=0.1)
    assert r["snapped"] is False and r["max_shift"] == 0.0 == r["median_shift"] and not r["shift"].any()
    assert r["points"] is not clicked and np.array_equal(r["points"], clicked)


def test_the_snap_from_inside_the_corner(model_inside):
    ch, info = model_inside
    face = np.asarray(info["face"])
    clicked = face + [[0.10, 0.40], [-0.25, 0.20], [-0.30, -0.15]]       # in the room, up to 0.4 m off its walls
    r = T.snap_trace(ch, clicked, side="left")
    assert r["snapped"] and distance_to(face, r["points"]).max() < 0.01
    assert np.linalg.norm(r["points"][1] - face[1]) < 0.02
    assert 0.38 < r["max_shift"] < 0.42 and all(s["share"] > 0.9 for s in r["segments"])
    far = T.snap_trace(ch, face + [[0.0, 2.0], [-2.0, 2.0], [-2.0, 0.0]], side="left")      # 2 m in the room
    assert np.abs(far["points"] - face).max() < 0.01


def test_a_curve_of_short_segments_is_shifted_segment_by_segment(tmp_path):
    _doc, ch, info = model_doc(tmp_path / "fuori", make_wall_scene(trace=ARC))
    face = np.asarray(info["face"])
    centre = np.asarray(info["shift"][:2])
    clicked = centre + (face - centre) * (3.2 / 3.0)        # 61 clicks, each 0.2 m off the face along its normal
    r = T.snap_trace(ch, clicked, side="right")
    assert r["snapped"] and r["points"].shape == (61, 2) and distance_to(face, r["points"]).max() < 0.02
    assert all(s["slope"] == 0.0 and s["share"] == 1.0 for s in r["segments"])     # 11 cm each: a shift, no slope
    assert np.abs(r["shift"] - 0.2).max() < 0.005
    behind = T.snap_trace(ch, centre + (face - centre) * (2.85 / 3.0), side="right")     # clicked on the crest
    assert distance_to(face, behind["points"]).max() < 0.02 and np.abs(behind["shift"] - 0.15).max() < 0.005
    # the same wall seen from inside its curve
    _doc, ch, info = model_doc(tmp_path / "dentro", make_wall_scene(trace=ARC, viewer="left"))
    r = T.snap_trace(ch, centre + (face - centre) * (2.8 / 3.0), side="left")
    assert r["snapped"] and distance_to(face, r["points"]).max() < 0.02 and np.abs(r["shift"] - 0.2).max() < 0.005


@pytest.mark.parametrize("trace, viewer, by", [
    (CORRIDOR, "right", 0.10),            # 60 cm between its two long walls
    (CORRIDOR, "right", -0.15),           # ... clicked on the crests
    (BUTTRESS, "left", 0.20),             # seen from the other side a buttress is a niche: 80 cm wide, 60 deep
    (BUTTRESS, "left", -0.15),
    (BUTTRESS, "right", 0.20),
    (ZIGZAG, "right", 0.20),              # the next leg comes back 1.5 m in front of this one
    (ZIGZAG, "left", 0.20),
    (ZIGZAG, "left", -0.15)])
def test_a_narrow_place_of_the_line_is_looked_at_from_within(trace, viewer, by, tmp_path):
    """A segment looks at the wall from where another part of the line is as near as itself, when that is
    nearer than the band: half way across a corridor, a niche, the wedge of a zigzag. From 2.5 m in front it would
    first meet the other side of the place - the back of the wall across the corridor, the next leg."""
    _doc, ch, info = model_doc(tmp_path, make_wall_scene(trace=trace, viewer=viewer))
    face = np.asarray(info["face"])
    r = T.snap_trace(ch, _in_front(face, viewer, by), side=viewer)
    assert r["snapped"] and np.abs(r["points"] - face).max() < 0.005
    assert r["max_shift"] == pytest.approx(abs(by) * np.sqrt(2.0) if trace is not ZIGZAG else abs(by) / np.cos(
        np.radians(75.0)), abs=0.01)


@pytest.mark.parametrize("viewer, inside", [("left", 0.20), ("left", -0.15), ("right", -0.20), ("right", 0.15)])
def test_a_room_traced_all_round_stays_closed(viewer, inside, tmp_path):
    """A line that ends on its first point is a room traced all round: that point is one corner, where the lines
    of the last wall and of the first meet - seen from inside or from outside, clicked in front of the faces or on
    the crests. Each end moved onto its own wall, the two would part by 28 cm, or cross."""
    room = np.array([[0.0, 0.0], [4.0, 0.0], [4.0, 3.0], [0.0, 3.0], [0.0, 0.0]])
    _doc, ch, info = model_doc(tmp_path, make_wall_scene(trace=room, viewer=viewer))
    face = np.asarray(info["face"])
    clicked = face[0] + np.array([[inside, inside], [4.0 - inside, inside], [4.0 - inside, 3.0 - inside],
                                  [inside, 3.0 - inside], [inside, inside]])
    r = T.snap_trace(ch, clicked, side=viewer)
    assert r["snapped"] and np.abs(r["points"] - face).max() < 0.005
    assert np.array_equal(r["points"][0], r["points"][-1]) and all(s["share"] > 0.85 for s in r["segments"])
    assert np.allclose(r["shift"], abs(inside) * np.sqrt(2.0), atol=0.01)
    path, report = T.path_from_trace(ch, clicked, side=viewer)
    assert np.array_equal(path.vertices[0], path.vertices[-1]) and len(path.vertices) == 5
    assert report["corners"] == 3 and report["snapped"] and np.array_equal(path.clicked, clicked)
    # a line that stops half a metre short of its first point has two ends, each on its own wall
    short = clicked.copy()
    short[-1] += [0.0, 0.5]
    r = T.snap_trace(ch, short, side=viewer)
    assert np.abs(r["points"][[0, -1]] - (face[0] + [[inside, 0.0], [0.0, inside + 0.5]])).max() < 0.005
    assert np.abs(r["points"][1:4] - face[1:4]).max() < 0.005
    # the fourth wall without its face (11 to 14 m; the band stops short of its back): its two corners go with the
    # walls beside it, each onto that wall's line, and the line still ends on its first point
    scene = make_wall_scene(trace=room, viewer=viewer)
    _doc, holed, _info = model_doc(tmp_path / "senza", scene, drop=(11.0, 14.0))
    r = T.snap_trace(holed, clicked, side=viewer, band=abs(inside) + 0.03, heights=(100.0, 102.0))
    assert r["snapped"] and r["segments"][3]["share"] < T.MIN_SNAP_SHARE < r["segments"][2]["share"]
    assert np.abs(r["points"][1:3] - face[1:3]).max() < 0.005 and np.array_equal(r["points"][0], r["points"][-1])
    assert np.abs(r["points"][[0, 3]] - (face[0] + [[inside, 0.0], [inside, 3.0]])).max() < 0.005


def test_a_wall_traced_alone_in_a_narrow_place_wants_a_narrow_band(tmp_path):
    """What stands in front of a wall within the band and is no part of the line is met first: one wall of a
    corridor of 60 cm traced without the other is looked at from behind that other one."""
    _doc, ch, info = model_doc(tmp_path, make_wall_scene(trace=CORRIDOR))
    face = np.asarray(info["face"])[:2]                       # its first wall, seen from the corridor
    clicked = face + [[-0.1, 0.0], [-0.1, 0.5]]              # 10 cm in front of it
    r = T.snap_trace(ch, clicked, side="right", band=0.3)
    assert r["snapped"] and np.abs(r["points"] - (face + [[0.0, 0.0], [0.0, 0.5]])).max() < 0.005
    wide = T.snap_trace(ch, clicked, side="right")           # 2.5 m: the outside of the wall across the corridor
    assert wide["snapped"] and np.abs(wide["points"] - (face + [[-1.0, 0.0], [-1.0, 0.5]])).max() < 0.005
    assert wide["max_shift"] == pytest.approx(0.9, abs=0.005)       # the snap says how far it went


def test_the_line_fitted_to_what_a_segment_sees(model_outside, tmp_path):
    ch, info = model_outside
    face = np.asarray(info["face"])
    clicked = face + OFF
    first, second = T.snap_trace(ch, clicked, side="right")["segments"]
    # the depth is towards the viewer: the face is behind the line clicked, less and less along the first segment
    lean, turn = np.arctan2(0.2, 6.15), np.arctan2(0.05, 4.05)
    assert first["offset"] == pytest.approx(-0.4 / np.cos(lean), abs=1e-3)
    assert first["slope"] == pytest.approx(np.tan(lean), abs=1e-4)
    assert second["offset"] == pytest.approx(-0.25 / np.cos(turn), abs=1e-3)
    assert second["slope"] == pytest.approx(-np.tan(turn), abs=1e-4)
    # a segment under half a metre is shifted as it is: the median of what it sees, no slope
    short = face[0] + np.array([[1.0, -0.3], [1.4, -0.2]])
    r = T.snap_trace(ch, short, side="right")
    (only,) = r["segments"]
    assert only["slope"] == 0.0 and only["offset"] == pytest.approx(-0.25 / np.cos(np.arctan2(0.1, 0.4)), abs=2e-3)
    assert np.abs((r["points"][1] - short[1]) - (r["points"][0] - short[0])).max() < 1e-9
    assert 0.04 < distance_to(face, r["points"]).min() and r["shift"] == pytest.approx([0.2577, 0.2577], abs=2e-3)
    # half a metre and over: a line (the same clicks carried on along their direction)
    longer = face[0] + np.array([[1.0, -0.3], [1.6, -0.15]])
    r = T.snap_trace(ch, longer, side="right")
    assert r["segments"][0]["slope"] == pytest.approx(0.25, abs=1e-3) and distance_to(face, r["points"]).max() < 1e-3


@pytest.mark.parametrize("what, where", [("pile", (0.2, 1.0)), ("pile", (0.2, 2.6)), ("pile", (3.0, 5.4)),
                                         ("door", (0.2, 2.8)), ("door", (3.6, 5.4))])
def test_what_is_not_the_wall_does_not_pull_its_line(what, where, model_outside, tmp_path):
    """A pile of stones half a metre thick against the first leg of the wall, or a doorway in it through which the
    back of the wall is seen: over 0.8 m of its 6 m at its start (a least-squares line is pulled 23 cm off by
    that, with the columns past the corner at the other end), over four tenths of it at either end."""
    _ch, info = model_outside
    face = np.asarray(info["face"])
    if what == "pile":
        scene = _block(make_wall_scene(), (where[0], -0.5, 0.0), (where[1], 0.0, 2.0))
        _doc, ch, _info = model_doc(tmp_path, scene)
    else:
        _doc, ch, _info = model_doc(tmp_path, make_wall_scene(), drop=where)
    r = T.snap_trace(ch, face + OFF, side="right")
    assert distance_to(face, r["points"]).max() < 0.01 and np.linalg.norm(r["points"][1] - face[1]) < 0.02
    assert r["segments"][0]["offset"] == pytest.approx(-0.4002, abs=1e-3)


@pytest.mark.parametrize("seed", range(12))
def test_the_line_of_a_segment_is_fitted_twice(seed):
    """A rough face (2 cm) with a pile half a metre thick over its first fifth: the line most columns lie on is
    found first, then fitted by least squares to the columns of the wall. The first alone is 1.6 to 4.2 cm off at
    an end of the segment for these twelve faces, a least-squares line through all the columns 33 cm."""
    rng = np.random.default_rng(seed)
    x = (np.arange(120) + 0.5) * 0.05
    d = -0.4 + 0.03 * x + rng.normal(0.0, 0.02, 120)
    d[:24] += 0.5
    a, b = T._fit(x, d, 0.05)
    assert max(abs(a + 0.4), abs(a + 6.0 * b + 0.22)) < 0.012


def test_a_line_needs_half_a_metre_of_face():
    x = (np.arange(24) + 0.5) * 0.05                          # a segment of 1.2 m that leans on the face
    d = -0.3 + 0.25 * x
    assert T._fit(x, d, 0.05) == pytest.approx((-0.3, 0.25))
    assert T._fit(x, np.where(x < 0.5, d, np.nan), 0.05) == pytest.approx((-0.3, 0.25))      # 50 cm of it seen
    part = np.where(x < 0.45, d, np.nan)                      # 45 cm: the median of what is seen, no slope
    assert T._fit(x, part, 0.05) == (pytest.approx(-0.3 + 0.25 * 0.225), 0.0)
    two = np.where((x < 0.1) | (x > 1.1), d, np.nan)          # the two ends alone span the segment: a line
    assert T._fit(x, two, 0.05) == pytest.approx((-0.3, 0.25))
    late = np.where((x > 0.7) & (x < 1.15), d, np.nan)        # 45 cm again, far from the start of the segment
    assert T._fit(x, late, 0.05) == (pytest.approx(-0.3 + 0.25 * 0.925), 0.0)
    off = part.copy()
    off[0] += 1.0                                             # ... and the median of them, whatever one sees
    assert T._fit(x, off, 0.05) == (pytest.approx(-0.3 + 0.25 * 0.275), 0.0)
    assert T._fit(x[:1], d[:1], 3.0) == (pytest.approx(d[0]), 0.0)                           # one column
    assert T.MIN_FIT == 0.5 and T.MIN_SNAP_SHARE == 0.3 and T.SNAP_BAND == 2.5


def test_a_segment_without_a_face_goes_with_those_that_have_one(tmp_path):
    # a straight wall of 6 m, and the line clicked 0.3 m in front of it going on 6 m past its end
    _doc, ch, info = model_doc(tmp_path / "dritto", make_wall_scene(trace=[(0.0, 0.0), (6.0, 0.0)]))
    o = np.asarray(info["face"][0])
    clicked = o + [[0.0, -0.3], [5.0, -0.3], [12.0, -0.3]]
    r = T.snap_trace(ch, clicked, side="right")
    assert r["snapped"] and np.abs(r["points"] - (o + [[0.0, 0.0], [5.0, 0.0], [12.0, 0.0]])).max() < 0.005
    assert r["segments"][1] == {"offset": 0.0, "slope": 0.0, "share": pytest.approx(1.0 / 7.0, abs=0.02)}
    assert r["segments"][1]["share"] < T.MIN_SNAP_SHARE < r["segments"][0]["share"]
    # ... or turning where the wall ends: the leg without a wall moves along itself with the corner
    clicked = o + [[0.0, -0.3], [6.3, -0.3], [6.3, 4.0]]
    r = T.snap_trace(ch, clicked, side="right")
    assert np.abs(r["points"] - (o + [[0.0, 0.0], [6.3, 0.0], [6.3, 4.3]])).max() < 0.005
    assert 0.0 < r["segments"][1]["share"] < T.MIN_SNAP_SHARE          # the end of the wall, 40 cm of its 4.3 m
    # the same leg first: the line as clicked the other way, the viewer on its left
    back = T.snap_trace(ch, clicked[::-1], side="left")
    assert np.abs(back["points"] - r["points"][::-1]).max() < 1e-5
    # a part with a face at just over three tenths of its length is fitted, just under is not
    for end, fitted in ((8.9, True), (9.1, False)):         # 2.6 m of wall in 8.5 and in 8.7 m
        r = T.snap_trace(ch, o + [[3.4, -0.3], [end + 3.0, -0.3]], side="right")
        assert r["snapped"] is fitted and (r["segments"][0]["share"] >= T.MIN_SNAP_SHARE) is fitted
    # three columns of ten on the wall, the others past its end: three tenths are enough (a shift: 15 cm of face)
    r = T.snap_trace(ch, o + [[5.85, -0.3], [6.35, -0.3]], side="right")
    assert r["segments"][0]["share"] == 0.3 == T.MIN_SNAP_SHARE and r["snapped"] is True
    assert np.abs(r["points"] - (o + [[5.85, 0.0], [6.35, 0.0]])).max() < 0.005
    # a gap of 2 m, the wall going on 10 cm further back: the part without a face joins the two
    scene = make_wall_scene(trace=[(0.0, 0.0), (3.0, 0.0)])
    more = make_wall_scene(trace=[(5.0, 0.1), (8.0, 0.1)])
    _doc, ch, info = model_doc(tmp_path / "varco", _added(scene, more.vertices, more.faces))
    clicked = o + [[0.0, -0.3], [3.2, -0.3], [4.8, -0.3], [8.0, -0.3]]
    r = T.snap_trace(ch, clicked, side="right")
    assert np.abs(r["points"] - (o + [[0.0, 0.0], [3.2, 0.0], [4.8, 0.1], [8.0, 0.1]])).max() < 0.005
    assert [s["share"] > 0.9 for s in r["segments"]] == [True, False, True] and r["segments"][1]["share"] == 0.0
    # three clicks in the gap: those with no face on either side lie between the two that have one
    clicked = o + [[0.0, -0.3], [3.2, -0.3], [3.6, -0.3], [4.4, -0.3], [4.8, -0.3], [8.0, -0.3]]
    r = T.snap_trace(ch, clicked, side="right")
    assert np.abs(r["points"][2:4] - (o + [[3.6, 0.025], [4.4, 0.075]])).max() < 0.005


def test_rough_ground_past_the_ends_of_the_wall_is_not_its_face(tmp_path):
    scene = _rough_ground(make_wall_scene(trace=[(0.0, 0.0), (6.0, 0.0)]), (-6.0, -3.5), (12.0, 1.5), rough=0.12)
    _doc, ch, info = model_doc(tmp_path, scene)
    o = np.asarray(info["face"][0])
    clicked = o + [[-5.0, -0.3], [11.0, -0.3]]                # 5 m of ground before the wall and 5 m after it
    r = T.snap_trace(ch, clicked, side="right", heights=(100.0, 102.0))
    (seg,) = r["segments"]
    # the two lowest heights of forty meet the ground anywhere, where the rays start: no face. With them the
    # ground would be five eighths of the columns, all at one depth, and the line would go there
    assert seg["share"] == pytest.approx(6.0 / 16.0, abs=0.01)
    assert r["snapped"] and np.abs(r["points"] - (o + [[-5.0, 0.0], [11.0, 0.0]])).max() < 0.005
    # the heights of the model along the line are those of its surface: the ground has 350 times the vertices
    # of this wall, all within 12 cm
    assert T._heights_near(ch, clicked, 2.5) == pytest.approx((100.0, 102.0), abs=0.01)
    r = T.snap_trace(ch, clicked, side="right")
    assert r["snapped"] and np.abs(r["points"] - (o + [[-5.0, 0.0], [11.0, 0.0]])).max() < 0.005


def test_a_tenth_of_the_heights_makes_a_column(tmp_path):
    _doc, ch, info = model_doc(tmp_path, make_wall_scene(trace=[(0.0, 0.0), (6.0, 0.0)]))
    o = np.asarray(info["face"][0])
    clicked = o + [[0.0, -0.3], [6.0, -0.3]]
    # the wall is 2 m high: 6 of 40 heights up to 13 m, 3 of 40 up to 25 m
    found = T.snap_trace(ch, clicked, side="right", heights=(100.0, 113.0))
    assert found["snapped"] and found["segments"][0]["share"] == 1.0
    lost = T.snap_trace(ch, clicked, side="right", heights=(100.0, 125.0))
    assert lost["snapped"] is False and lost["segments"][0]["share"] == 0.0


def test_a_line_has_three_tenths_of_the_columns_on_it(tmp_path):
    # past the end of the wall, 6 m of rubble: blocks of 25 cm, anything up to 2 m high. Every column of the
    # second segment sees something, at any depth: one in ten within 10 cm of the best line through them
    scene = _rough_ground(make_wall_scene(trace=[(0.0, 0.0), (6.0, 0.0)]), (6.3, -3.2), (12.5, 1.0), rough=2.0,
                          cell=0.25)
    _doc, ch, info = model_doc(tmp_path, scene)
    o = np.asarray(info["face"][0])
    r = T.snap_trace(ch, o + [[0.0, -0.3], [6.0, -0.3], [12.0, -0.3]], side="right", heights=(100.0, 102.0))
    first, second = r["segments"]
    assert first["share"] == 1.0 and 0.0 < second["share"] < T.MIN_SNAP_SHARE
    assert (second["offset"], second["slope"]) == (0.0, 0.0)
    assert np.abs(r["points"] - (o + [[0.0, 0.0], [6.0, 0.0], [12.0, 0.0]])).max() < 0.005     # it goes with the first


def test_the_heights_the_face_is_looked_for_at(tmp_path):
    # a bench 30 cm deep and 50 cm high along the first leg of the wall: a quarter of its height
    scene = _block(make_wall_scene(), (0.0, -0.3, 0.0), (6.0, 0.0, 0.5))
    # ... a wall of 3 m one metre behind it, a tower of 60 m four metres in front, a spike of the mesh 40 m high
    _block(scene, (0.0, 1.0, 0.0), (6.0, 1.4, 3.0))
    _block(scene, (2.0, -5.0, 0.0), (3.0, -4.0, 60.0))
    _added(scene, [[1.0, 0.2, 0.0], [1.01, 0.2, 0.0], [1.0, 0.2, 40.0]], [[0, 1, 2]])
    _doc, ch, info = model_doc(tmp_path, scene)
    face = np.asarray(info["face"])
    clicked = face + [[0.0, -0.6], [0.6, -0.6], [0.6, 0.0]]
    # by default the heights of the model within the band of the line, but for a hundredth at either end: those
    # of the wall, of which the bench is a quarter - the wall is what most of the height shows
    r = T.snap_trace(ch, clicked, side="right", band=1.0)
    assert r["snapped"] and distance_to(face, r["points"]).max() < 0.01
    low = T.snap_trace(ch, clicked, side="right", band=1.0, heights=(100.0, 100.5))      # the heights of the bench
    assert np.abs(low["points"][0] - (face[0] + [0.0, -0.3])).max() < 0.01
    # up to 2.9 m, three tenths of the heights pass over the wall and meet the one behind it: the wall still
    tall = T.snap_trace(ch, clicked, side="right", band=2.0, heights=(100.0, 102.9))
    assert tall["snapped"] and distance_to(face, tall["points"]).max() < 0.01
    # a band that takes the tower in: the wall is 2 m of 60, under a tenth of the heights - nothing is found
    wide = T.snap_trace(ch, clicked, side="right", band=4.5)
    assert wide["snapped"] is False
    assert T.snap_trace(ch, clicked, side="right", band=4.5, heights=(100.0, 102.0))["snapped"] is True


def test_the_heights_of_the_model_along_the_line(tmp_path):
    """By default the face is looked for between the 1st and the 99th percentile of the height of the model within
    the band of the line: of its vertices, each counted for a third of the area of the triangles it belongs to."""
    rng = np.random.default_rng(2)

    def level(n, lo, hi, z, area):
        """``n`` level triangles of ``area`` each, anywhere in the box ``lo``-``hi`` of the plan, at heights ``z``."""
        side = np.sqrt(2.0 * area)
        corner = np.column_stack([rng.uniform(lo[0], hi[0] - side, n), rng.uniform(lo[1], hi[1] - side, n),
                                  np.broadcast_to(z, n)])
        return np.stack([corner, corner + [side, 0.0, 0.0], corner + [0.0, side, 0.0]], axis=1).reshape(-1, 3)

    wall = level(327, (0.0, -0.8), (10.0, 0.8), np.linspace(0.0, 2.0, 327), 0.01)    # 95.8 % of the surface
    total = 327 * 0.01 / 0.958
    odd = np.vstack([level(1, (4.0, -0.3), (4.9, 0.3), 3.0, 0.008 * total),          # 0.8 % of it at 3 m
                     level(1, (4.0, -0.3), (4.9, 0.3), 40.0, 0.007 * total),         # 0.7 % at 40 m
                     level(1, (4.0, -0.3), (4.9, 0.3), -5.0, 0.007 * total)])        # 0.7 % at -5 m
    ground = level(3334, (0.0, -0.8), (10.0, 0.8), 0.001, 0.02 * total / 3334)       # 2 %, in ten times the vertices
    away = np.vstack([level(4, (2.0, 1.5), (8.0, 3.0), 90.0, 0.5), level(4, (2.0, -3.0), (8.0, -1.5), 90.0, 0.5),
                      level(4, (-3.5, -0.5), (-1.5, 0.5), 90.0, 0.5), level(4, (11.5, -0.5), (13.5, 0.5), 90.0, 0.5),
                      level(10, (10.75, 0.75), (10.99, 0.99), 90.0, 0.0035)])        # ... in the corner past the end
    xyz = np.vstack([wall, odd, ground, away])
    scene = SimpleNamespace(vertices=xyz, faces=np.arange(len(xyz)).reshape(-1, 3), face=np.zeros((2, 2)),
                            viewer="right", height=2.0, thickness=0.4, marks=[], face_columns=None)
    _doc, ch, info = model_doc(tmp_path, scene)
    shift = np.asarray(info["shift"])
    line = shift[:2] + [[0.0, 0.0], [10.0, 0.0]]
    v0, v1 = T._heights_near(ch, line, 1.0)
    # 3 m: 2.0 at the 98th percentile, 40 at the 99.5th, 1.8 for the vertices counted one each. The ground: -5 at
    # the 0.5th percentile
    assert v1 == pytest.approx(shift[2] + 3.0, abs=1e-4) and v0 == pytest.approx(shift[2] + 0.001, abs=1e-4)
    bent = shift[:2] + [[0.0, 0.0], [5.0, 0.0], [5.0, -0.2], [10.0, -0.2]]     # every segment has its band
    assert T._heights_near(ch, bent, 1.0) == pytest.approx((v0, v1), abs=1e-4)
    assert T._heights_near(ch, line + [0.0, 50.0], 1.0) is None                # no vertex there
    flat = SimpleNamespace(**{**vars(scene), "vertices": xyz * [1.0, 1.0, 0.0]})
    _doc, ch, _info = model_doc(tmp_path / "piano", flat)
    assert T._heights_near(ch, line, 1.0) is None                              # all at one height: no wall


def test_what_the_snap_refuses_and_a_point_clicked_twice(model_outside):
    import pyarchinit_photogrammetry as ppg

    ch, info = model_outside
    face = np.asarray(info["face"])
    clicked = face + OFF
    for bad in ([clicked[0]], [clicked[0], clicked[0]], [clicked[0], [np.nan, 0.0]]):
        with pytest.raises(TraceRefused) as e:
            T.snap_trace(ch, bad, side="right")
        assert e.value.reason == "points"
    for side in ("north", "auto", (1.0,), clicked[0]):      # no such word, no photo, not a point, a point of the line
        with pytest.raises(TraceRefused) as e:
            T.snap_trace(ch, clicked, side=side)
        assert e.value.reason == "side"
    for bad, word in ((dict(band=0.0), "band"), (dict(band=np.inf), "band"), (dict(step=0.0), "step"),
                      (dict(samples=0), "samples"), (dict(heights=(102.0, 100.0)), "heights"),
                      (dict(heights=(100.0,)), "heights")):
        with pytest.raises(ValueError, match=word):
            T.snap_trace(ch, clicked, side="right", **bad)
    with pytest.raises(RuntimeError, match="model"):
        T.snap_trace(ppg.Document().add_chunk("Vuoto"), clicked, side="right")
    # a point clicked twice is one vertex: both go to the corner
    r = T.snap_trace(ch, clicked[[0, 1, 1, 2]], side="right")
    assert r["points"].shape == (4, 2) and np.array_equal(r["points"][1], r["points"][2])
    assert np.linalg.norm(r["points"][1] - face[1]) < 0.02 and len(r["segments"]) == 3
    assert r["segments"][1] == {"offset": 0.0, "slope": 0.0, "share": 0.0} and r["shift"][1] == r["shift"][2]
    r = T.snap_trace(ch, clicked[[0, 0, 1, 2]], side="right")
    assert np.array_equal(r["points"][0], r["points"][1]) and r["segments"][0]["share"] == 0.0
    assert [round(q["offset"], 2) for q in r["segments"]] == [0.0, -0.4, -0.25]


def test_two_lines_that_are_one_wall_and_two_that_make_a_corner(tmp_path):
    # a wall, then another 10 cm further back that turns by 2 degrees: their lines meet 2.9 m before the point
    # where the one ends and the other starts. The vertex clicked there stays there, between the two
    c, s = np.cos(np.radians(2.0)), np.sin(np.radians(2.0))
    scene = make_wall_scene(trace=[(0.0, 0.0), (4.0, 0.0)])
    more = make_wall_scene(trace=[(4.0, 0.1), (4.0 + 4.0 * c, 0.1 + 4.0 * s)])
    _doc, ch, info = model_doc(tmp_path / "scalino", _added(scene, more.vertices, more.faces))
    o = np.asarray(info["face"][0])
    r = T.snap_trace(ch, o + [[0.0, -0.3], [4.0, -0.3], [4.0 + 4.0 * c, -0.3 + 4.0 * s]], side="right")
    assert r["snapped"] and all(q["share"] > 0.95 for q in r["segments"])
    assert np.abs(r["points"] - (o + [[0.0, 0.0], [3.994, 0.05], [3.984 + 4.0 * c, 0.1 + 4.0 * s]])).max() < 0.005
    # a wall that turns by 20 degrees, its corner clicked 20 cm in front of one face and 29 of the other: the
    # corner is where the two lines meet, not between them
    bend = [(0.0, 0.0), (4.0, 0.0), (4.0 + 4.0 * np.cos(np.radians(20.0)), 4.0 * np.sin(np.radians(20.0)))]
    _doc, ch, info = model_doc(tmp_path / "piega", make_wall_scene(trace=bend))
    face = np.asarray(info["face"])
    r = T.snap_trace(ch, face + [[0.0, -0.3], [0.3, -0.2], [0.2, -0.35]], side="right")
    assert r["snapped"] and np.linalg.norm(r["points"][1] - face[1]) < 0.005
    assert distance_to(face, r["points"]).max() < 0.1 and r["shift"][1] == pytest.approx(np.hypot(0.3, 0.2), abs=0.005)


def test_the_vertices_keep_the_order_clicked(tmp_path):
    # two faces whose lines meet past the end of the second segment - the first leans, the second stands 2.2 m
    # in front of the line: put where they meet, the corner would turn that segment back
    scene = make_wall_scene(trace=[(0.0, 0.1), (2.0, 1.1)])
    more = make_wall_scene(trace=[(4.2, 0.0), (4.2, 2.0)])
    _doc, ch, info = model_doc(tmp_path, _added(scene, more.vertices, more.faces))
    o = np.asarray(info["shift"][:2])
    lean, front = o + [[0.0, 0.1], [2.0, 1.1]], o + [[4.2, -9.0], [4.2, 9.0]]
    clicked = o + [[0.0, 0.0], [2.0, 0.0], [2.0, 2.0]]
    r = T.snap_trace(ch, clicked, side="right")
    assert r["snapped"] is True and all(s["share"] > 0.9 and s["offset"] != 0.0 for s in r["segments"])
    assert distance_to(lean, r["points"][:1])[0] < 1e-3 and distance_to(front, r["points"][2:])[0] < 1e-3
    step = np.diff(r["points"], axis=0)
    assert step[0, 0] > 0 and step[1, 1] > 0                       # on as clicked: east, then north
    corner = r["points"][1:2]                                      # as far from the one face as from the other
    assert distance_to(o + [[-9.0, -4.4], [9.0, 4.6]], corner)[0] == pytest.approx(distance_to(front, corner)[0],
                                                                                 abs=1e-3)
    assert np.abs(corner[0] - (o + [4.2, 2.2])).max() > 1.0
    # a second segment that goes on past where they meet: the corner is there
    longer = T.snap_trace(ch, o + [[0.0, 0.0], [2.0, 0.0], [2.0, 3.0]], side="right")
    assert longer["snapped"] is True and np.abs(longer["points"][1] - (o + [4.2, 2.2])).max() < 0.01
    # an L with legs of 1 m whose two faces are found 5 m behind it: they meet 4 m back along both legs, and
    # between them is no better - there is no way to keep the order, and the line is left as clicked
    v = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]])
    legs = dict(length=np.array([1.0, 1.0]), t=np.array([[1.0, 0.0], [0.0, 1.0]]),
                towards=np.array([[0.0, -1.0], [1.0, 0.0]]), found=np.array([True, True]))
    assert T._moved(v, fits=np.array([[-5.0, 0.0], [-5.0, 0.0]]), **legs) is None
    near = T._moved(v, fits=np.array([[-0.5, 0.0], [-0.5, 0.0]]), **legs)           # half a metre behind it
    assert np.allclose(near, [[0.0, 0.5], [0.5, 0.5], [0.5, 1.0]])
    assert T._moved(v, fits=np.zeros((2, 2)), **{**legs, "found": np.array([False, False])}) is None
    # a turn of 120 degrees whose two lines meet behind the first point: the corner goes between them - at a
    # right angle and over, half way between where each line alone would put it
    turn = np.array([[0.0, 0.0], [1.0, 0.0], [0.5, np.sqrt(0.75)]])
    wide = dict(length=np.array([1.0, 1.0]), t=np.array([[1.0, 0.0], [-0.5, np.sqrt(0.75)]]),
                towards=np.array([[0.0, -1.0], [np.sqrt(0.75), 0.5]]), found=np.array([True, True]))
    out = T._moved(turn, fits=np.array([[-0.2, 0.0], [-2.0, 0.0]]), **wide)
    assert np.allclose(out, [[0.0, 0.2], [0.134, -0.4], [-1.232, -0.134]], atol=1e-3)
    # a square traced all round whose first wall is found leaning, so that it meets the last one a metre past the
    # corner before: the point the line starts and ends on goes between the two, as one corner
    square = np.array([[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]])
    round_ = dict(length=np.ones(4), t=np.array([[1.0, 0.0], [0.0, 1.0], [-1.0, 0.0], [0.0, -1.0]]),
                  towards=np.array([[0.0, -1.0], [1.0, 0.0], [0.0, 1.0], [-1.0, 0.0]]), found=np.ones(4, bool))
    lean = np.array([[-2.0, 2.0], [0.0, 0.0], [0.0, 0.0], [0.0, 0.0]])
    out = T._moved(square, fits=lean, closed=True, **round_)
    assert np.allclose(out, [[0.447, 0.106], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.447, 0.106]], atol=1e-3)
    assert np.allclose(T._moved(square, fits=np.zeros((4, 2)), closed=True, **round_), square)


def test_projected_and_local_coordinates_snap_alike(model_outside, tmp_path):
    ch, info = model_outside
    _doc, local, here = model_doc(tmp_path, make_wall_scene(), shift=(0.0, 0.0, 0.0))
    shift = np.asarray(info["shift"][:2])
    assert np.abs(shift).min() >= 500000.0 and here["shift"] == [0.0, 0.0, 0.0]
    clicked = np.asarray(here["face"]) + OFF
    a, b = T.snap_trace(local, clicked, side="right"), T.snap_trace(ch, clicked + shift, side="right")
    assert a["snapped"] and np.abs(b["points"] - shift - a["points"]).max() < 1e-5
    assert np.abs(b["shift"] - a["shift"]).max() < 1e-5
    for here, there in zip(a["segments"], b["segments"]):
        assert here["share"] == there["share"]
        assert abs(here["offset"] - there["offset"]) < 1e-5 and abs(here["slope"] - there["slope"]) < 1e-5


def test_the_snap_says_how_far_it_is_and_can_be_stopped(model_outside):
    ch, info = model_outside
    clicked = np.asarray(info["face"]) + OFF
    seen = []
    T.snap_trace(ch, clicked, side="right", progress=lambda fraction, message: seen.append((fraction, message)))
    fractions = [f for f, _m in seen]
    assert fractions == sorted(fractions) and fractions[0] == 0.0 and fractions[-1] == 1.0 and len(seen) >= 3
    assert seen[0][1] and all(isinstance(m, str) for _f, m in seen)
    stopped = Progress()
    stopped.cancel()
    with pytest.raises(Cancelled):
        T.snap_trace(ch, clicked, side="right", progress=stopped)
    seen.clear()                                              # no model within the band of the line: said all the same
    r = T.snap_trace(ch, clicked + [0.0, 50.0], side="right", progress=lambda f, m: seen.append(f))
    assert seen == [0.0, 1.0] and r["snapped"] is False


def test_the_columns_and_the_heights_a_segment_looks_at(model_outside, model_inside, monkeypatch, tmp_path):
    ch, info = model_outside
    clicked = np.asarray(info["face"]) + OFF
    cast = []
    depth_at = T.FrameSurface.depth_at

    def spy(self, u, v):
        columns = np.unique(u)
        cast.append(SimpleNamespace(u=columns, v=np.unique(v), rays=len(u), band=(self.dmin, self.dmax),
                                    front=self.band(columns)[1], behind=self.band(columns)[0]))
        return depth_at(self, u, v)

    monkeypatch.setattr(T.FrameSurface, "depth_at", spy)
    T.snap_trace(ch, clicked, side="right", band=1.5, heights=(100.4, 101.6), step=0.5, samples=6)
    first, second = cast
    assert first.band == (-1.5, 1.5) and first.rays == 13 * 6 and second.rays == 9 * 6   # 6.15, 4.05 m by 0.5 m
    assert np.allclose(first.u, (np.arange(13) + 0.5) * np.hypot(6.15, 0.2) / 13)   # ... at most, at their middles
    assert np.allclose(first.v, 100.5 + 0.2 * np.arange(6))                         # the middles of six equal parts
    assert (first.front == 1.5).all() and (first.behind == -1.5).all()              # outside a corner: the band
    T.snap_trace(ch, clicked, side="right")                                         # 5 cm, forty heights, 2.5 m
    assert cast[2].band == (-2.5, 2.5) and cast[2].rays == 124 * 40
    assert np.allclose(cast[2].v, 100.025 + 0.05 * np.arange(40)) and (cast[2].front == 2.5).all()
    T.snap_trace(ch, clicked[::-1], side="left", step=0.5, samples=6)               # the line clicked the other way
    assert np.allclose(cast[4].u, (np.arange(9) + 0.5) * np.hypot(0.05, 4.05) / 9) and cast[4].rays == 9 * 6
    # inside a corner each leg looks from where the other is as near: the line that halves the corner
    ch, info = model_inside
    del cast[:]
    T.snap_trace(ch, np.asarray(info["face"]), side="left", step=0.5)
    first, second = cast
    # (u runs from the viewer's left: from the corner along the first leg, towards it along the second)
    assert np.allclose(first.front, np.minimum(2.5, first.u), atol=1e-6) and (first.behind == -2.5).all()
    assert np.allclose(second.front, np.minimum(2.5, 4.0 - second.u), atol=1e-6)
    # in a room 4 m wide seen from inside, half way to the wall across it, 2 m, and no further than the corner
    room = [[4.0, 3.0], [4.0, 0.0], [0.0, 0.0], [0.0, 3.0]]
    _doc, ch, info = model_doc(tmp_path, make_wall_scene(trace=room))
    del cast[:]
    r = T.snap_trace(ch, np.asarray(info["face"]), side="right", step=0.5)
    assert r["snapped"] and np.allclose(cast[0].front, np.minimum(2.0, 3.0 - cast[0].u), atol=1e-6)
    assert np.allclose(cast[1].front, np.minimum(2.5, np.minimum(cast[1].u, 4.0 - cast[1].u)), atol=1e-6)


# ---------------------------------------------------------------- the side
def test_the_side_by_the_photos_a_word_or_a_point(wall_dataset, model_outside, tmp_path):
    _doc, (ch,), info = wall_doc(tmp_path, wall_dataset)
    face = np.asarray(info["face"])
    assert T.side_of(ch, face, "auto") == "right" and T.side_of(ch, face) == "right"
    assert T.side_of(ch, face[::-1], "auto") == "left" and T.side_of(ch, face[::-1], None) == "left"
    assert T.side_of(ch, face, face[0] + [3.0, -3.0]) == "right"          # a point 3 m south of the first leg
    assert T.side_of(ch, face, tuple(face[0] + [3.0, 3.0])) == "left"     # ... and 3 m north of it
    assert T.side_of(ch, face[::-1], [*(face[0] + [3.0, -3.0]), 101.0]) == "left"        # a height with it
    assert T.side_of(ch, face, "left") == "left" and T.side_of(ch, face[::-1], "right") == "right"   # as it is said
    model, _info = model_outside
    with pytest.raises(TraceRefused) as e:
        T.side_of(model, face, "auto")
    assert e.value.reason == "side" and "photo" in str(e.value)
    # the snap and the frame take the side of the photos, whichever way the line was clicked
    clicked = face + OFF
    r = T.snap_trace(ch, clicked)
    assert r["snapped"] and distance_to(face, r["points"]).max() < 0.01
    path, report = T.path_from_trace(ch, clicked)
    back, reverse = T.path_from_trace(ch, clicked[::-1])
    assert report["side"] == "right" and reverse["side"] == "left"
    assert np.abs(back.vertices - path.vertices).max() < 1e-5 and distance_to(face, back.vertices).max() < 0.01
    assert np.array_equal(back.clicked, clicked[::-1])
    assert reverse["max_shift"] == pytest.approx(report["max_shift"], abs=1e-5)


def test_each_photo_votes_at_its_nearest_point_of_the_line(wall_dataset, tmp_path):
    _doc, (ch,), info = wall_doc(tmp_path, wall_dataset)
    face = np.asarray(info["face"])
    # three photos in the room, between the wall and the line from its first point to its last - on the left of
    # both legs, on the right of that line - and two in front of the first leg
    _stand(ch, [(5.0, 1.2), (5.0, 2.0), (4.0, 1.0), (2.0, -2.5), (4.0, -2.5)])
    assert T.side_of(ch, face) == "left" and T.side_of(ch, face[::-1]) == "right"
    # three east of the second leg - on its right, and on the left of the first leg carried on - two in the room
    _stand(ch, [(8.5, 2.0), (8.5, 3.0), (8.5, 3.5), (2.0, 2.5), (3.0, 2.5)])
    assert T.side_of(ch, face) == "right"
    # a photo switched off has no vote, nor one that is not aligned; as many on either side: the right
    _stand(ch, [(2.0, -2.5), (2.0, 2.5), (3.0, 2.5)])
    assert T.side_of(ch, face) == "left"
    ch.cameras[2].enabled = False
    assert T.side_of(ch, face) == "right" and T.side_of(ch, face[::-1]) == "right"
    ch.cameras[0].transform = None
    assert T.side_of(ch, face) == "left"


def test_the_side_of_a_point_is_taken_at_its_nearest_point_of_the_line(model_outside):
    ch, _info = model_outside
    turn = np.asarray(TURN)                                    # a left turn of 120 degrees: its outside on the right
    # past the end of the first leg and a little to its left, yet outside the corner: the corner is its nearest point
    assert T.side_of(ch, turn, (5.0, 0.3)) == "right" and T.side_of(ch, turn[::-1], (5.0, 0.3)) == "left"
    assert T.side_of(ch, turn, (3.0, 0.5)) == "left" and T.side_of(ch, turn, (3.0, -0.5)) == "right"
    assert T.side_of(ch, turn, (-1.0, 0.2)) == "left" and T.side_of(ch, turn, (-1.0, -0.2)) == "right"   # before it
    assert T.side_of(ch, turn, np.array([1.0, 4.0])) == "left"                  # past its end, on the tangent's left
    assert T.side_of(ch, turn, np.array([2.0, 5.0])) == "right"
    # all round the outside of the corner, from the right of the first leg to the right of the second (the two
    # legs are as near as each other there, to the last digit: either may be taken for the nearest)
    for angle in np.radians(np.linspace(-89.0, 29.0, 60)):
        for far in (0.3, 1.0, 2.5):
            point = turn[1] + far * np.array([np.cos(angle), np.sin(angle)])
            assert T.side_of(ch, turn, point) == "right" and T.side_of(ch, turn[::-1], point) == "left"
    for bad in ((2.0, 0.0), (4.0, 0.0), (1.0,), (np.nan, 1.0), "sopra", None):  # on the line, on its corner, no point
        with pytest.raises(TraceRefused) as e:
            T.side_of(ch, turn, bad)
        assert e.value.reason == "side"
    with pytest.raises(TraceRefused) as e:
        T.side_of(ch, [turn[0]], "right")                      # the line itself is asked first
    assert e.value.reason == "points"


def test_outside_a_corner_whichever_leg_is_taken_for_the_nearest():
    """Outside a corner its two legs are as near as each other - both at the corner - and which of the two is
    found the nearer is a matter of the last digit: for one point in sixteen of these it is the second, whose own
    line would put one in nine of them on the wrong side."""
    rng = np.random.default_rng(0)
    seen = 0
    for _ in range(60):
        line = np.cumsum(rng.uniform(-3.0, 3.0, (4, 2)), axis=0) + rng.choice([0.0, 1.0]) * np.array([5e5, 4.4e6])
        step = np.diff(line, axis=0)
        heading = np.arctan2(step[:, 1], step[:, 0])
        turn = (np.diff(heading) + np.pi) % (2.0 * np.pi) - np.pi
        if np.abs(turn).max() > np.radians(160.0) or np.abs(turn).min() < np.radians(5.0):
            continue
        for k in (1, 2):                                      # round the outside of each corner, leg to leg
            angle = heading[k - 1] - np.sign(turn[k - 1]) * np.pi / 2.0 + rng.uniform(0.02, 0.98, 50) * turn[k - 1]
            points = line[k] + rng.uniform(0.1, 3.0, 50)[:, None] * np.column_stack([np.cos(angle), np.sin(angle)])
            here = distance_to(line, points) > np.hypot(*(points - line[k]).T) - 1e-9    # the corner is the nearest
            assert (T._sides(line, points[here]) == np.sign(turn[k - 1])).all()         # a left turn: on its right
            seen += int(here.sum())
    assert seen > 2000


# ---------------------------------------------------------------- the frame
def test_the_frame_of_a_trace(model_outside):
    ch, info = model_outside
    face = np.asarray(info["face"])
    clicked = face + OFF
    path, report = T.path_from_trace(ch, clicked, side="right")
    assert isinstance(path, WallPath) and path.kind == "path"
    assert distance_to(face, path.vertices).max() < 0.01 and np.array_equal(path.clicked, clicked)
    assert path.radii == corner_radii(path.vertices) and path.radii == pytest.approx([0.52])
    assert report["side"] == "right" and report["snapped"] is True and 0.38 < report["max_shift"] < 0.42
    assert set(report) == {"side", "snapped", "median_shift", "max_shift", "length", "corners"}
    assert report["median_shift"] == pytest.approx(0.32, abs=0.005)
    assert report["length"] == path.length and report["corners"] == 1 == len(path.corners())
    # without the snap the vertices are the clicked ones, in the order of the viewer
    path, report = T.path_from_trace(ch, clicked, side="right", snap=False)
    assert np.array_equal(path.vertices, clicked) and path.clicked is None
    assert report["snapped"] is False and report["max_shift"] == 0.0 == report["median_shift"]
    path, report = T.path_from_trace(ch, clicked, side="left", snap=False)
    assert np.array_equal(path.vertices, clicked[::-1]) and path.clicked is None and report["side"] == "left"
    assert path.corners()[0]["angle_deg"] < -85.0                       # the same corner seen from inside
    # the depth kept in front of the face and behind it sizes the fillets
    path, _report = T.path_from_trace(ch, clicked, side="right", front=1.0, back=0.3)
    assert path.radii == corner_radii(path.vertices, 1.0, 0.3) and path.radii == pytest.approx([1.02])
    assert path.radius == pytest.approx(1.02)
    path, _report = T.path_from_trace(ch, clicked, side="left", snap=False, front=1.0, back=0.3)
    assert path.radii == corner_radii(path.vertices, 1.0, 0.3) and 0.5 < path.radii[0] < 0.7    # by the depth behind
    for bad in (dict(front=0.0), dict(back=-0.1), dict(front=np.inf), dict(smooth=-1.0)):
        with pytest.raises(ValueError, match="front|back|smooth"):
            T.path_from_trace(ch, clicked, side="right", **bad)
    # a caller that snapped already gives the snapped line and says what was clicked
    snapped = T.snap_trace(ch, clicked, side="right")["points"]
    path, report = T.path_from_trace(ch, snapped, side="right", snap=False, clicked=clicked.tolist())
    assert np.array_equal(path.vertices, snapped) and np.array_equal(path.clicked, clicked)
    assert report["snapped"] is False
    # the band and the heights of the snap are those asked
    assert T.path_from_trace(ch, clicked, side="right", band=0.1)[1]["snapped"] is False
    assert T.path_from_trace(ch, clicked, side="right", heights=(105.0, 106.0))[1]["snapped"] is False
    # smoothed when asked: a wave of 4 m keeps 0.29 of its 5 cm under a bell of 1 m (1.38 cm as clicked every 0.5 m)
    x = np.linspace(0.0, 16.0, 33)
    wave = face[0] + np.column_stack([x, 0.05 * np.sin(2.0 * np.pi * x / 4.0)])
    path, report = T.path_from_trace(ch, wave, side="right", snap=False, smooth=1.0)
    assert 0.011 < np.abs(path.vertices[:, 1] - face[0, 1]).max() < 0.016 and len(path.vertices) == 34
    assert np.array_equal(path.clicked, wave) and report["snapped"] is False
    assert report["corners"] == len(path.corners()) > 20
    assert np.array_equal(T.path_from_trace(ch, wave, side="right", snap=False)[0].vertices, wave)   # off unless asked


def test_the_line_is_smoothed_then_snapped(model_outside, monkeypatch):
    ch, info = model_outside
    face = np.asarray(info["face"])
    x = np.linspace(0.0, 6.0, 25)
    wobble = 0.05 * (-1.0) ** np.arange(25)                   # a click every 0.25 m, 5 cm either side of a line
    wobble[[0, -1]] = 0.0
    clicked = face[0] + np.column_stack([x, -0.3 + wobble])   # ... 0.3 m in front of the first leg
    given = []
    snap = T.snap_trace

    def spy(chunk, points, side="auto", **options):
        given.append((np.array(points), side, options))
        return snap(chunk, points, side, **options)

    monkeypatch.setattr(T, "snap_trace", spy)
    path, report = T.path_from_trace(ch, clicked, side=face[0] + [3.0, -3.0], smooth=1.0, band=1.5,
                                     heights=(100.2, 101.8))
    ((points, side, options),) = given
    assert np.array_equal(points, smooth_trace(clicked, 1.0)) and len(points) != len(clicked)
    assert side == "right" and options == {"band": 1.5, "heights": (100.2, 101.8)}    # the side is found once
    assert report["snapped"] and report["side"] == "right" and np.array_equal(path.clicked, clicked)
    assert distance_to(face, path.vertices).max() < 0.01
    T.path_from_trace(ch, clicked, side="right", snap=False, smooth=1.0)
    assert len(given) == 1                                     # no snap unless asked


@pytest.mark.parametrize("sigma", [0.5, 1.0])
def test_a_smoothed_line_is_snapped_round_its_corner(sigma, model_outside):
    """Smoothed, the line is short chords round the corner it cut: each sees a different depth, and the lines
    of two chords next to each other meet anywhere along them. The vertices stay in their order all the same."""
    ch, info = model_outside
    face = np.asarray(info["face"])
    path, report = T.path_from_trace(ch, face + OFF, side="right", smooth=sigma)
    assert report["snapped"] is True and len(path.vertices) > 10
    near = distance_to(face, path.vertices)
    straight = np.hypot(*(path.vertices - face[1]).T) > 3.0 * sigma       # away from the corner it rounded
    assert straight.sum() > 5 and near[straight].max() < 0.01 and near.max() < 0.05
    step = np.diff(path.vertices, axis=0)
    assert (step @ [1.0, 1.0] > 0).all()                                   # on along the wall, never back


def test_clicks_on_a_straight_wall_by_a_corner_leave_its_fillet_whole(model_outside, tmp_path):
    ch, info = model_outside
    face = np.asarray(info["face"])
    # two clicks more on the straight walls, 30 cm either side of the corner, all 0.2 m in front of the face
    clicked = face[0] + np.array([[0.0, -0.2], [5.7, -0.2], [6.2, -0.2], [6.2, 0.3], [6.2, 4.0]])
    path, report = T.path_from_trace(ch, clicked, side="right")
    assert len(path.vertices) == 3 and distance_to(face, path.vertices).max() < 0.01
    assert np.linalg.norm(path.vertices[1] - face[1]) < 0.02 and np.array_equal(path.clicked, clicked)
    (corner,) = path.corners()
    assert corner["radius"] == pytest.approx(0.52) and path.radii == corner_radii(path.vertices)
    assert report["corners"] == 1 and report["length"] == path.length
    kept = WallPath(T.snap_trace(ch, clicked, side="right")["points"])        # with the two clicks: half of 30 cm
    assert [c["radius"] for c in kept.corners() if c["angle_deg"] > 45.0] == pytest.approx([0.15], abs=0.01)
    # the same on a line given as it is: a click on the wall within a centimetre of the line between its neighbours
    on = face[0] + np.array([[0.0, 0.0], [5.7, 0.008], [6.0, 0.0], [6.0, 0.3], [6.0, 4.0]])
    path, report = T.path_from_trace(ch, on, side="right", snap=False)
    assert np.array_equal(path.vertices, on[[0, 2, 4]]) and np.array_equal(path.clicked, on)
    assert path.corners()[0]["radius"] == pytest.approx(0.52) and report["snapped"] is False
    back, _report = T.path_from_trace(ch, on, side="left", snap=False)        # seen from the other side
    assert np.array_equal(back.vertices, on[[4, 2, 0]]) and np.array_equal(back.clicked, on)
    # over a centimetre off that line it is a turn of the wall: it stays, and the corner has half of the 30 cm
    bent = on.copy()
    bent[1, 1] += 0.03 - 0.008
    path, _report = T.path_from_trace(ch, bent, side="right", snap=False)
    assert np.array_equal(path.vertices, bent[[0, 1, 2, 4]])
    assert path.corners()[1]["radius"] == pytest.approx(0.136, abs=0.005)
    # the room a corner asks for is that of the depth kept: with more in front, a click further off gives way
    wide = face[0] + np.array([[0.0, 0.0], [4.8, 0.0], [6.0, 0.0], [6.0, 4.0]])
    assert len(T.path_from_trace(ch, wide, side="right", snap=False)[0].vertices) == 4
    assert np.array_equal(T.path_from_trace(ch, wide, side="right", snap=False, front=1.5)[0].vertices, wide[[0, 2, 3]])
    # ... with less behind a corner seen from inside, a click nearer stays
    near = face[0] + np.array([[0.0, 0.0], [5.7, 0.0], [6.0, 0.0], [6.0, 4.0]])
    assert len(T.path_from_trace(ch, near, side="left", snap=False)[0].vertices) == 3
    assert np.array_equal(T.path_from_trace(ch, near, side="left", snap=False, back=0.05)[0].vertices, near[::-1])
    # the line a caller says was clicked is kept, whatever the vertices
    path, _report = T.path_from_trace(ch, on, side="right", snap=False, clicked=bent)
    assert len(path.vertices) == 3 and np.array_equal(path.clicked, bent)
    # a click that takes no room from a corner stays where it was clicked
    far = face[0] + np.array([[0.0, 0.0], [3.0, 0.0], [6.0, 0.0], [6.0, 2.0], [6.0, 4.0]])
    path, _report = T.path_from_trace(ch, far, side="right", snap=False)
    assert np.array_equal(path.vertices, far) and path.clicked is None
    # ... and so do the clicks of a curve, by a corner too: only those the line does without are left out
    _doc, curved, info = model_doc(tmp_path, make_wall_scene(trace=ARC))
    arc = np.asarray(info["face"])
    path, _report = T.path_from_trace(curved, arc, side="right", snap=False)
    assert np.array_equal(path.vertices, arc)
    hooked = np.vstack([arc[0] + [[1.0, -3.0]], arc])        # a straight wall of 3.2 m, a corner, then the curve
    path, _report = T.path_from_trace(curved, hooked, side="right", snap=False)
    assert np.array_equal(path.vertices, hooked[np.r_[:2, 3:62]])       # the first click of the curve gives way
    assert path.corners()[0]["radius"] == pytest.approx(0.52)           # 0.34 with every click of the curve kept
    # a right angle asks for a metre: three clicks of the curve give way, the fourth would move the line by more
    # than a centimetre from one of them - each stands 2 to 9 mm from the line between its own two neighbours
    square = np.vstack([arc[0] + [[3.0, 0.0]], arc])
    path, _report = T.path_from_trace(curved, square, side="right", snap=False)
    assert np.array_equal(path.vertices, square[np.r_[:2, 5:62]])
    assert distance_to(path.vertices, square).max() <= 0.01
    assert path.corners()[0]["radius"] == pytest.approx(0.21, abs=0.02)   # 0.05 with every click kept


def test_a_snapped_line_the_frame_refuses_is_said(tmp_path):
    # the two faces of one wall traced in a single line: clicked it turns by 169 degrees, snapped by more than 170
    _doc, ch, info = model_doc(tmp_path, make_wall_scene(trace=[(0.0, 0.0), (6.0, 0.0)]))
    o = np.asarray(info["face"][0])
    clicked = o + [[0.0, -1.0], [4.0, -1.0], [0.5, -0.3]]
    assert WallPath(clicked).kind == "path"                    # as clicked it is a line the frame takes
    with pytest.raises(TraceRefused) as e:
        T.path_from_trace(ch, clicked, side="right")
    assert e.value.reason == "turns_back" and "snap" in str(e.value)
    path, report = T.path_from_trace(ch, clicked, side="right", snap=False)
    assert np.array_equal(path.vertices, clicked) and report["snapped"] is False
    with pytest.raises(TraceRefused) as e:                     # a line refused as clicked is refused in its own words
        T.path_from_trace(ch, o + [[0.0, 0.0], [4.0, 0.0], [0.0, 0.1]], side="right", snap=False)
    assert e.value.reason == "turns_back" and "snap" not in str(e.value)
