"""The frame of a wall traced on the plan (:class:`WallPath`): pure geometry, no chunk, no model.

The line is the trace with a fillet at each corner; ``u`` is the length along it, ``v`` the height, ``d`` the offset
along its normal towards the viewer, who stands on the right of the direction ``u`` grows. The traces: an L seen
from outside its corner and from inside, a room seen from inside, a return of 30 cm, a curve of 61 clicks, a
buttress, two turns of 150 degrees."""
from __future__ import annotations

import json
import warnings

import numpy as np
import pytest

from pyarchinit_photogrammetry.engine import elevation as E
from pyarchinit_photogrammetry.engine.wall_path import (CLEARANCE, DEFAULT_RADIUS, TraceRefused, WallPath,
                                                        corner_radii, ordered, smooth_trace)

L = [[0.0, 0.0], [6.0, 0.0], [6.0, 4.0]]               # the viewer on the right of the travel: outside the corner
INSIDE = L[::-1]                                         # the same wall seen from inside the corner
U = [[4.0, 3.0], [4.0, 0.0], [0.0, 0.0], [0.0, 3.0]]     # a room seen from inside: two re-entrant corners
JOG = [[0.0, 0.0], [3.0, 0.0], [3.0, 0.3], [6.0, 0.3]]   # two corners 30 cm apart
ARC = 3.0 * np.column_stack([np.cos(np.radians(np.linspace(0, 120, 61))), np.sin(np.radians(np.linspace(0, 120, 61)))])
CURVE_OUT, CURVE_IN = ARC, ARC[::-1]                     # 61 clicks on a wall of 3 m radius, from outside and inside
BUTTRESS = [[0.0, 0.0], [3.0, 0.0], [3.0, -0.6], [3.8, -0.6], [3.8, 0.0], [7.0, 0.0]]   # 60 cm towards the viewer
ZIGZAG = [[0.0, 0.0], [4.0, 0.0], [1.4, 1.5], [5.4, 1.5]]                               # two turns of 150 degrees


def test_the_L_has_its_fillet_and_its_length():
    p = WallPath(L, radius=0.5)
    mid = 5.5 + 0.5 * np.pi / 4
    assert p.kind == "path" and abs(p.length - (9.0 + 0.5 * np.pi / 2)) < 1e-12
    assert p.min_radius == pytest.approx(0.5)
    (c,) = p.corners()
    assert abs(c["s"] - mid) < 1e-12 and abs(c["angle_deg"] - 90.0) < 1e-9 and c["radius"] == pytest.approx(0.5)
    assert np.allclose(p.point(mid, 1.0, 0.0), [5.853553390593274, 0.14644660940672627, 1.0], atol=1e-12)
    assert np.allclose(p.view(np.array([1.0, 8.0]), np.zeros(2)), [[0, -1, 0], [1, 0, 0]], atol=1e-12)
    right = np.cross([0, 0, 1], p.view(1.0, 0.0))        # u grows to the right of the viewer
    assert (p.point(1.01, 0.0, 0.0) - p.point(1.0, 0.0, 0.0)) @ right > 0
    assert np.allclose(p.point(1.0, 0.0, 0.1) - p.point(1.0, 0.0, 0.0), 0.1 * p.view(1.0, 0.0))  # d towards the viewer


def test_depth_limits_of_a_convex_and_a_re_entrant_corner():
    out, ins = WallPath(L, radius=0.5), WallPath(INSIDE, radius=0.5)
    mid = 5.5 + 0.5 * np.pi / 4
    assert np.all(np.isinf(out.max_depth_at(np.array([3.0, mid, 7.0])))) and out.max_depth() == np.inf
    assert np.allclose(out.min_depth_at(np.array([3.0, mid])), [-(3.0 - CLEARANCE), -(0.5 - CLEARANCE)], atol=1e-6)
    mid = 3.5 + 0.5 * np.pi / 4
    assert np.allclose(ins.max_depth_at(np.array([1.0, mid])), [3.0 - CLEARANCE, 0.5 - CLEARANCE], atol=1e-6)
    assert np.all(np.isinf(ins.min_depth_at(np.array([1.0, mid]))))


def test_two_walls_facing_each_other_share_the_room():
    assert np.allclose(WallPath(U, radius=0.5).max_depth_at(np.array([0.5, 1.5])),
                       [2.0 - CLEARANCE, 1.5 - CLEARANCE], atol=1e-6)   # half the room, then the corner's bisector


@pytest.mark.parametrize("trace", [L, INSIDE, U, JOG, CURVE_OUT, CURVE_IN])
def test_point_and_coords_are_inverse_within_a_nanometre(trace):
    rng = np.random.default_rng(3)
    p = WallPath(trace, radius=0.5)
    s, v = rng.uniform(-1.0, p.length + 1.0, 4000), rng.uniform(-2.0, 5.0, 4000)   # the ends prolonged too
    lo, hi = p.min_depth_at(s), p.max_depth_at(s)
    d = rng.uniform(np.maximum(lo, -1.5) * 0.95, np.minimum(hi, 1.5) * 0.95)
    assert np.abs(p.coords(p.point(s, v, d)) - np.column_stack([s, v, d])).max() < 1e-9
    # no world point is seen twice: any point of the plan maps to one (u, d) that gives it back, or to none
    q = np.column_stack([rng.uniform(-4, 10, 4000), rng.uniform(-4, 8, 4000), np.zeros(4000)])
    uvd = p.coords(q)
    on = np.isfinite(uvd[:, 0])
    assert on.mean() > 0.5 and np.abs(p.point(*uvd[on].T) - q[on]).max() < 1e-9


@pytest.mark.parametrize("trace", [BUTTRESS, BUTTRESS[::-1], ZIGZAG, ZIGZAG[::-1]])
def test_a_buttress_and_sharp_turns_keep_the_inverse(trace):
    rng = np.random.default_rng(8)
    for p in (WallPath(trace, radius=0.5), WallPath(trace, radii=corner_radii(trace))):
        s, v = rng.uniform(-1.0, p.length + 1.0, 3000), rng.uniform(-2.0, 5.0, 3000)
        lo, hi = p.min_depth_at(s), p.max_depth_at(s)
        assert np.all(lo <= 0.0) and np.all(hi >= 0.0) and np.isfinite(lo).any() and np.isfinite(hi).any()
        d = rng.uniform(np.maximum(lo, -1.5) * 0.95, np.minimum(hi, 1.5) * 0.95)
        assert np.abs(p.coords(p.point(s, v, d)) - np.column_stack([s, v, d])).max() < 1e-9
        # the limits are the ones coords applies: on the frame just inside them, off it (or of another place of
        # the line) just outside
        for side, limit in ((1.0, hi), (-1.0, lo)):
            ends = np.isfinite(limit) & (np.abs(limit) > 1e-3)
            inner = p.coords(p.point(s[ends], v[ends], limit[ends] * (1 - 1e-6)))
            outer = p.coords(p.point(s[ends], v[ends], limit[ends] + side * 1e-5))
            assert ends.any() and np.isfinite(inner).all() and np.abs(inner[:, 0] - s[ends]).max() < 1e-9
            assert not np.any(np.abs(outer[:, 0] - s[ends]) < 1e-6)
        q = np.column_stack([rng.uniform(-3, 10, 3000), rng.uniform(-4, 5, 3000), np.zeros(3000)])
        uvd = p.coords(q)
        on = np.isfinite(uvd[:, 0])
        assert on.mean() > 0.5 and np.abs(p.point(*uvd[on].T) - q[on]).max() < 1e-9


def test_a_place_a_hair_from_a_joint_comes_back_to_itself():
    # by the joint of a run and an arc the distances to the two differ by less than float64 resolves: the place
    # is found by where the foot falls, not by that tie
    p = WallPath(L, radius=0.5)
    joints = p.corners()[0]["s"] + np.array([-0.5, 0.5]) * 0.5 * np.pi / 2
    for hair in (1e-8, -1e-8, 1e-6, -1e-6):
        for depth in (1.4, 50.0, 900.0):
            back = p.coords(p.point(joints + hair, 1.0, depth))
            assert np.abs(back[:, 0] - (joints + hair)).max() < 1e-10 and np.abs(back[:, 2] - depth).max() < 1e-9
        assert np.all(np.isinf(p.max_depth_at(joints + hair)))        # in front of a convex corner: no limit


def test_a_curve_of_many_clicks_is_one_smooth_line():
    p = WallPath(CURVE_IN, radius=0.5)
    step, chord, a = np.radians(2.0), 6.0 * np.sin(np.radians(1.0)), 0.5 * np.tan(np.radians(1.0))
    assert abs(p.length - (60 * chord - 59 * 2 * a + 59 * 0.5 * step)) < 1e-9
    s = np.linspace(0.0, p.length, 2001)
    n = p.view(s, np.zeros_like(s))
    turn = np.arccos(np.clip((n[1:] * n[:-1]).sum(axis=1), -1.0, 1.0))
    assert turn.max() <= (s[1] - s[0]) / 0.5 * (1 + 1e-6)             # the normal never jumps: no facets
    assert p.max_depth_at(s).min() == pytest.approx(0.5 - CLEARANCE, abs=1e-6)


def test_close_corners_share_the_short_side():
    p = WallPath(JOG, radius=0.5)
    assert [c["radius"] for c in p.corners()] == pytest.approx([0.15, 0.15])
    assert [round(c["angle_deg"]) for c in p.corners()] == [90, -90] and p.min_radius == pytest.approx(0.15)
    assert abs(p.length - (6.3 - 4 * 0.15 + 2 * 0.15 * np.pi / 2)) < 1e-12
    s = np.array([c["s"] for c in p.corners()])
    assert np.allclose(p.min_depth_at(s)[0], -(0.15 - CLEARANCE), atol=1e-6)
    assert np.allclose(p.max_depth_at(s)[1], 0.15 - CLEARANCE, atol=1e-6)


def test_corner_radii_keep_the_corner_in_the_band():
    def turn(deg):
        return [[0.0, 0.0], [4.0, 0.0], [4.0 + 4.0 * np.cos(np.radians(deg)), 4.0 * np.sin(np.radians(deg))]]
    assert corner_radii(turn(90)) == pytest.approx([0.52]) and corner_radii(turn(120)) == pytest.approx([0.4])
    assert corner_radii(turn(150)) == pytest.approx([0.13967927448341994])
    p = WallPath(turn(120), radii=corner_radii(turn(120)))
    assert abs(p.coords(np.array([[4.0, 0.0, 0.0]]))[0, 2] - 0.4) < 1e-9   # the real corner 0.4 m in front: in the band
    # a re-entrant corner has the real corner behind the arc: the band behind is the one that counts
    inside = turn(120)[::-1]
    assert corner_radii(inside, front=0.5, back=0.2) == pytest.approx([0.16])
    assert corner_radii(turn(120), front=0.5, back=0.2) == pytest.approx([0.4])
    assert corner_radii([[0.0, 0.0], [2.0, 0.0], [5.0, 0.0]], front=0.3) == pytest.approx([0.3 + CLEARANCE])
    assert corner_radii(L[:2]) == []


def test_projected_coordinates_keep_the_nanometre_on_the_face():
    rng = np.random.default_rng(5)
    shift = np.array([401234.567, 2634567.890])
    p = WallPath(np.asarray(L) + shift, radius=0.5)
    assert abs(p.length - WallPath(L, radius=0.5).length) < 1e-9
    s, v = rng.uniform(0.0, p.length, 4000), rng.uniform(100.0, 104.0, 4000)
    for band, tol in ((0.05, 1e-9), (0.4, 1e-7)):
        d = rng.uniform(-band, band, 4000)
        assert np.abs(p.coords(p.point(s, v, d)) - np.column_stack([s, v, d])).max() < tol


def test_odd_traces():
    for bad, reason in (([[0, 0]], "points"), ([[0, 0], [0, 0]], "points"),
                        ([[0, 0], [4, 0], [4, 2], [2, -2]], "crosses"), ([[0, 0], [4, 0], [0, 0.2]], "turns_back")):
        with pytest.raises(TraceRefused) as e:
            WallPath(bad)
        assert e.value.reason == reason
    room = WallPath([[0, 0], [4, 0], [4, 3], [0, 3], [0, 0]], radius=0.5)   # closed: a strip from its first point
    assert len(room.corners()) == 3 and abs(room.length - (14.0 - 6 * 0.5 + 3 * 0.5 * np.pi / 2)) < 1e-12
    s = np.linspace(0.1, room.length - 0.1, 500)
    back = room.coords(room.point(s, np.zeros(500), np.full(500, 0.2)))
    assert np.abs(back[:, 0] - s).max() < 1e-9
    two = WallPath([[0, 0], [5, 0]])                                         # two points: a plane
    assert two.min_radius == np.inf and two.corners() == [] and np.isinf(two.max_depth_at(np.array([2.0]))[0])


def test_u_map_is_the_length_on_the_face():
    arc = 0.5 * np.pi / 2
    p = WallPath(L, radius=0.5, u_map=[[0, 0], [5.5, 5.5], [5.5 + arc, 6.5], [9.0 + arc, 10.0]])
    assert abs(p.u_of(p.length) - 10.0) < 1e-12 and abs(p.s_of(10.0) - p.length) < 1e-12
    assert abs(p.s_of(p.u_of(3.3)) - 3.3) < 1e-12 and abs(p.u_of(-2.0) + 2.0) < 1e-12   # prolonged past both ends
    assert np.allclose(p.point(6.0, 0.0, 0.0), [5.853553390593274, 0.14644660940672627, 0.0], atol=1e-12)
    assert abs(p.corners()[0]["u"] - 6.0) < 1e-12 and abs(p.corners()[0]["s"] - (5.5 + arc / 2)) < 1e-12
    assert np.allclose(p.view(np.array([1.0, 8.5]), np.zeros(2)), [[0, -1, 0], [1, 0, 0]], atol=1e-12)
    rng = np.random.default_rng(4)
    u, v, d = rng.uniform(-1.0, 11.0, 2000), rng.uniform(-2.0, 5.0, 2000), rng.uniform(-0.45, 0.45, 2000)
    assert np.abs(p.coords(p.point(u, v, d)) - np.column_stack([u, v, d])).max() < 1e-9
    # the limits are asked in u as well: behind the middle of the arc (u = 6) the frame ends at its centre
    assert np.allclose(p.min_depth_at(np.array([3.0, 6.0])), [-(3.0 - CLEARANCE), -(0.5 - CLEARANCE)], atol=1e-6)
    for bad in ([[0, 0], [5, 5], [4, 6]], [[0, 0], [5, 5], [6, 5]], [[0, 0]]):
        with pytest.raises(ValueError, match="increas"):
            WallPath(L, radius=0.5, u_map=bad)


def test_dict_round_trip_and_unknown_kinds(rng):
    clicked = [[6.3, 0.2], [2.9, 0.4], [3.1, -0.1], [0.2, 0.1]]              # as clicked: the other way round
    p = WallPath(JOG, radii=[0.12, 0.1], u_map=[[0.0, 0.0], [3.0, 3.2], [6.2, 6.6]], clicked=clicked)
    d = json.loads(json.dumps(p.to_dict()))
    again = E.frame_from_dict(d)
    assert E.WallPath is WallPath and isinstance(again, WallPath) and again.to_dict() == p.to_dict()
    assert d["kind"] == "path" and d["vertices"] == JOG and d["radius"] == DEFAULT_RADIUS
    assert d["radii"] == [0.12, 0.1] and d["clicked"] == clicked and d["u_map"][1] == [3.0, 3.2]
    assert [c["radius"] for c in again.corners()] == pytest.approx([0.12, 0.1])
    pts = np.column_stack([rng.uniform(-2, 8, 100), rng.uniform(-3, 3, 100), rng.uniform(0, 2, 100)])
    before = p.coords(pts)
    assert np.isfinite(before).all(axis=1).sum() > 50 and np.array_equal(before, again.coords(pts), equal_nan=True)
    plain = WallPath(L).to_dict()
    assert sorted(plain) == ["kind", "radius", "vertices"] and plain["radius"] == DEFAULT_RADIUS == 0.52
    with pytest.raises(ValueError, match="unknown frame kind"):
        E.frame_from_dict({"kind": "spline"})
    flat = {"origin": [1.0, 2.0, 0.0], "u_axis": [1.0, 0.0, 0.0], "v_axis": [0.0, 0.0, 1.0]}
    assert isinstance(E.frame_from_dict(flat), E.Plane) and isinstance(E.frame_from_dict({**flat, "kind": "plane"}),
                                                                       E.Plane)
    cy = E.Cylinder([1.0, 2.0, 0.0], [0, 0, 1], 2.5, inside=True, start=[0, 1, 0],
                    u_map=[[-180.0, -8.0], [0.0, 0.0], [180.0, 8.5]])
    assert isinstance(E.frame_from_dict(cy.to_dict()), E.Cylinder)
    assert E.frame_from_dict(cy.to_dict()).to_dict() == cy.to_dict()


def test_trace_line_and_order():
    p = WallPath(L, radius=0.5)
    assert np.array_equal(p.trace_line(), L)
    ends = [[-1.0, 0.0], [6.0, 0.0], [6.0, 6.0]]         # 1 m before the start, the corner as traced, 2 m past the end
    assert np.abs(p.trace_line(p.u_of(-1.0), p.u_of(p.length + 2.0)) - ends).max() < 1e-12
    assert np.abs(p.trace_line(2.0, 5.0) - [[2.0, 0.0], [5.0, 0.0]]).max() < 1e-12    # no corner in between
    assert np.abs(p.trace_line(u1=5.0) - [[0.0, 0.0], [5.0, 0.0]]).max() < 1e-12
    assert np.abs(p.trace_line(u0=7.0) - [[6.0, 7.0 - p.length + 4.0], [6.0, 4.0]]).max() < 1e-12
    m = WallPath(L, radius=0.5, u_map=[[0.0, 0.0], [p.length, 12.0]])
    assert m.u_of(-1.0) < -1.2 and m.u_of(m.length + 2.0) > 14.4
    assert np.abs(m.trace_line(m.u_of(-1.0), m.u_of(m.length + 2.0)) - ends).max() < 1e-12
    shift = np.array([401234.567, 2634567.890])                               # the trace is in plan coordinates
    far = WallPath(np.asarray(L) + shift, radius=0.5)
    assert np.abs(far.trace_line(-1.0, far.length + 2.0) - (np.asarray(ends) + shift)).max() < 1e-9
    assert np.array_equal(ordered(L, "left"), L[::-1]) and np.array_equal(ordered(L, "right"), L)
    with pytest.raises(TraceRefused) as e:
        ordered(L, "auto")                                # the side is chosen before the line is ordered
    assert e.value.reason == "side"


def test_smoothing_is_off_unless_asked():
    x = np.linspace(0.0, 10.0, 21)
    wobble = 0.05 * (-1.0) ** np.arange(21)               # a click every 0.5 m, 5 cm off the wall on alternate sides
    wobble[[0, -1]] = 0.0                                 # the two ends are where the wall ends
    pts = np.column_stack([x, wobble])
    assert np.array_equal(smooth_trace(pts, 0.0), pts)
    out = smooth_trace(pts, 1.0)
    assert np.array_equal(out[0], pts[0]) and np.array_equal(out[-1], pts[-1]) and len(out) >= 21
    assert np.abs(out[:, 1]).max() < 0.02 and np.all(np.diff(out[:, 0]) > 0)
    dense = np.column_stack([np.linspace(0.0, 10.0, 41), 0.05 * (-1.0) ** np.arange(41)])   # a click every 0.25 m
    dense[[0, -1], 1] = 0.0
    assert np.abs(smooth_trace(dense, 1.0)[:, 1]).max() < 0.005   # shorter than a step: its mean, not a sample of it
    # noise without a pattern is left at about a third (five clicks under the bell)
    off = np.random.default_rng(0).uniform(-0.05, 0.05, 21)
    off[[0, -1]] = 0.0
    out = smooth_trace(np.column_stack([x, off]), 1.0)
    assert np.sqrt((out[:, 1] ** 2).mean()) < 0.5 * np.sqrt((off ** 2).mean())
    assert WallPath(out).length == pytest.approx(10.0, abs=0.01)
    shift = np.array([401234.567, 2634567.890])
    assert np.abs(smooth_trace(pts + shift, 1.0) - shift - smooth_trace(pts, 1.0)).max() < 1e-9


def test_points_off_the_frame_are_nan_rows():
    room = WallPath(U, radius=0.5)
    pts = np.array([[2.0, 1.5, 7.0],      # in front of the wall at the bottom, 1.5 m of the 2 m it reaches
                    [2.0, 1.99, 7.0],     # the same column, within the clearance of where the side walls take over
                    [2.0, 2.5, 7.0],      # half way between the two side walls: of neither
                    [3.0, 2.5, 7.0],      # 1 m in front of the first wall
                    [np.nan, 0.0, 0.0], [np.inf, 0.0, 0.0]])
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        uvd = room.coords(pts)
    assert uvd.shape == (6, 3) and np.isfinite(uvd).all(axis=1).tolist() == [True, False, False, True, False, False]
    assert np.isnan(uvd[[1, 2, 4, 5]]).all()
    assert np.allclose(uvd[0], [room.corners()[0]["s"] + 0.5 * np.pi / 4 + 1.5, 7.0, 1.5], atol=1e-12)
    assert np.allclose(uvd[3], [0.5, 7.0, 1.0], atol=1e-12)
    assert room.coords(np.zeros((0, 3))).shape == (0, 3)


def test_the_closing_corner_of_a_ring_belongs_to_its_start():
    room = WallPath([[0, 0], [4, 0], [4, 3], [0, 3], [0, 0]], radius=0.5)   # seen from outside
    # both ends of the line are the nearest point of what lies in front of that corner: the first wins the tie
    uvd = room.coords(np.array([[-0.3, -0.4, 1.0], [-2.0, -1e-3, 1.0]]))
    assert np.allclose(uvd, [[-0.3, 1.0, 0.4], [-2.0, 1.0, 1e-3]], atol=1e-12)
    assert np.all(np.isinf(room.max_depth_at(np.array([-0.5, 2.0]))))
    assert room.min_depth_at(np.array([-0.5]))[0] == 0.0                       # behind it stands the last wall
    # past its end the line runs into its own start: nothing there is of the end
    past = np.array([room.length + 0.5])
    assert room.max_depth_at(past)[0] == 0.0 and room.min_depth_at(past)[0] == 0.0
    assert np.allclose(room.coords(room.point(past, 1.0, 0.0)), [[0.0, 1.0, 0.5]], atol=1e-12)


def test_past_an_end_that_runs_under_the_start_there_is_no_frame():
    # a room traced from outside that stops 1 m short of closing: its last wall, prolonged, passes 30 cm from the start
    hook = WallPath([[0.0, 0.3], [0.0, 3.0], [-4.0, 3.0], [-4.0, 0.0], [-1.0, 0.0]], radius=0.5)
    near, far = np.array([hook.length + 0.1]), np.array([hook.length + 1.0])
    assert np.isinf(hook.max_depth_at(near)[0]) and hook.max_depth_at(far)[0] == 0.0 == hook.min_depth_at(far)[0]
    # 2 m in front of the two places: the second is nearer to the end of the line than to its start, but the
    # place it would be given is itself of the start: it is not on the frame
    uvd = hook.coords(np.array([[-0.9, -2.0, 0.0], [0.0, -2.0, 0.0]]))
    assert np.allclose(uvd[0], [near[0], 0.0, 2.0], atol=1e-12) and np.isnan(uvd[1]).all()


def test_shapes_and_arguments():
    p = WallPath(L, radius=0.5)
    assert p.point(1.0, 0.0, 0.0).shape == (3,) and p.view(1.0, 0.0).shape == (3,)
    u = np.linspace(0.0, 9.0, 10).reshape(2, 5)
    assert p.point(u, 1.0, 0.0).shape == (2, 5, 3) and p.view(u, np.zeros((2, 5))).shape == (2, 5, 3)
    assert p.max_depth_at(u).shape == (2, 5) and p.min_depth_at(u).shape == (2, 5)
    assert np.allclose(p.point(u, 1.5, 0.0)[..., 2], 1.5) and np.allclose(np.linalg.norm(p.view(u, 0.0), axis=-1), 1.0)
    assert np.array_equal(WallPath([[0, 0, 5.0], [0, 0, 5.0], [6, 0, 5.2], [6, 4, 5.1]]).vertices, L)  # x, y only
    straight = WallPath([[0.0, 0.0], [2.0, 0.0], [5.0, 0.0]], radius=0.5)   # a click on a straight wall: no corner
    assert straight.corners() == [] and straight.length == 5.0 and straight.min_radius == np.inf
    assert np.all(np.isinf(straight.max_depth_at(np.array([1.0, 2.0, 3.0]))))
    with pytest.raises(ValueError, match="interior"):
        WallPath(JOG, radii=[0.2])
    for radius in (0.0, -0.1, np.nan):
        with pytest.raises(ValueError, match="radius"):
            WallPath(L, radius=radius)
    with pytest.raises(TraceRefused) as e:
        WallPath([[0.0, 0.0], [np.nan, 1.0]])
    assert e.value.reason == "points"
