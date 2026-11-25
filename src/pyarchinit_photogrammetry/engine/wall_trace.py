"""From a line clicked on the plan to the frame of a wall: the side it is seen from, the snap onto its face.

A line clicked on the plan is never on the face of the wall: it runs on its crest, in front of it, a couple of
metres off. :func:`snap_trace` moves it onto the face of the model. Through the vertical plane of each clicked
segment the face is looked for from the line, in a band in front of it and behind it, a line is fitted to what
the segment sees, and each vertex goes where the lines of its two segments meet. :func:`side_of` says from which
side of the line the wall is seen - a word, a point of the plan, or where the photos see it better - and
:func:`path_from_trace` makes the frame of the wall (:class:`~.wall_path.WallPath`) of the clicked line: smoothed
and snapped as asked, put in the order of the viewer, its fillets sized on the depth kept in front of the face
and behind it.
"""
from __future__ import annotations

import threading
import warnings

import numpy as np

from ..core.chunk import Chunk
from ..core.progress import Progress, as_progress
from .elevation import FACE_ROWS, FrameSurface, plane_from_line
from .wall_path import (CLEARANCE, DEFAULT_FRONT, TraceRefused, WallPath, _MIN_STEP, _plan_points, _turns, _xy,
                        corner_radii, ordered, smooth_trace)

PHOTO_NEAR = 1.0  # m: a photo nearer the line than this weighs, for the side, as one this far from it
SNAP_BAND = 2.5  # m in front of the clicked line and behind it where the face is looked for
MIN_SNAP_SHARE = 0.3  # the share of the columns of a segment that must lie on its line for it to be moved
MIN_FIT = 0.5  # m of face a line is fitted to: a shorter stretch gives a shift alone
FIT_MAD = 3.0  # a column further than this many median deviations from the line fitted is left out of it
FIT_COLUMNS = 400  # at most, for the slope of the first line (the slopes between them two by two)
FIT_NEAR = 0.1  # m: a column within this of the line of its segment lies on it
OVER_TOP = 1.0  # m over the highest height looked at: what stands higher above a click is a cover, not a crest
SAME_WALL_DEG = 5.0  # two fitted lines that turn less are one wall going on: no corner to put where they meet
STRAIGHT = 0.01  # m: a vertex this near the line between its neighbours is a click on a straight wall, not a turn
APART = 0.5  # m: a line this much further off its click than those of the neighbours of its segment is another face
NEIGHBOURS = 2  # segments on either side of one whose lines say where its neighbours found the face

_OVERLAP = 0.01  # m: a look starts this far on the other side of the clicked line: a face on the line itself is met
_PAST = 0.001  # m past a surface, where a look goes on from


# ---------------------------------------------------------------- the side
def _sides(line: np.ndarray, points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Of plan points (M, 2), each at the point of the polyline ``line`` (N, 2) nearest to it (the first of two
    equally near): its side - +1 on the right of the line as it is travelled, -1 on its left, 0 on it - and that
    nearest point. A point whose nearest point is a corner lies outside that corner: on the right of a left turn.
    Past an end, the side of the end segment carried on; the point a closed line starts and ends on is a corner
    like the others."""
    v = line - line[0]
    q = points - line[0]
    length, t, _turn = _turns(v)
    last = len(length) - 1
    best = np.full(len(q), np.inf)
    seg, tau = np.zeros(len(q), np.intp), np.zeros(len(q))
    for i in range(len(length)):
        along = np.clip((q - v[i]) @ t[i], 0.0, length[i])
        dist = np.hypot(*(q - v[i] - along[:, None] * t[i]).T)
        nearer = dist < best
        best[nearer], seg[nearer], tau[nearer] = dist[nearer], i, along[nearer]
    foot = v[seg] + tau[:, None] * t[seg]
    heading = t[seg].copy()  # at a corner, the direction half way round it
    closed = last > 1 and float(np.hypot(*v[-1])) <= _MIN_STEP
    before, after = tau <= 0.0, tau >= length[seg]
    for at, other in ((before & (seg > 0), seg - 1), (after & (seg < last), seg + 1),
                      (before & (seg == 0) & closed, np.full(len(q), last)),
                      (after & (seg == last) & closed, np.zeros(len(q), np.intp))):
        heading[at] += t[other[at]]
    w = q - foot
    return -np.sign(heading[:, 0] * w[:, 1] - heading[:, 1] * w[:, 0]), foot + line[0]


def side_votes(chunk: Chunk, points) -> dict:
    """How well the aligned photos of ``chunk`` see the wall of the clicked line ``points`` from either side of
    it: ``{"left": weight, "right": weight}``, for :func:`side_of` and for a caller that asks when the two are
    close.

    Each photo counts for the side it stands on, at the point of the line nearest to it. It weighs the cosine of
    the angle between its view and the normal of the face there, over the distance at which its view meets the
    plane of the face: the cosine squared over its distance from the line on the plan (:data:`PHOTO_NEAR` at
    least). A photo that looks straight down, along the wall or away from it weighs nothing: a flight over the
    site does not outvote the close-ups of the face, nor do photos taken from far. Both weights are 0 without an
    aligned photo."""
    line = _plan_points(points)
    cameras = chunk.aligned_cameras
    if not cameras:
        return {"left": 0.0, "right": 0.0}
    centres = chunk.to_world(np.array([c.center for c in cameras]))[:, :2]
    views = np.array([c.transform[:3, 2] for c in cameras]) @ np.asarray(chunk.transform, np.float64)[:3, :3].T
    level = views[:, :2] / np.linalg.norm(views, axis=1)[:, None]  # of each unit view, its part on the plan
    side, foot = _sides(line, centres)
    off = centres - foot
    far = np.hypot(off[:, 0], off[:, 1])
    cosine = np.where(far > 0.0, -(level * off).sum(axis=1) / np.maximum(far, 1e-300), 0.0)  # looking at the line
    weight = np.where(cosine > 0.0, cosine ** 2 / np.maximum(far, PHOTO_NEAR), 0.0)
    return {"left": float(weight[side < 0].sum()), "right": float(weight[side > 0].sum())}


def side_of(chunk: Chunk, points, side="auto") -> str:
    """The side of the clicked line ``points`` (plan points in the order clicked) the wall is seen from:
    ``"left"`` or ``"right"`` of the direction it was clicked in.

    ``side``: one of those two words, given back as it is; a plan point ``(x, y)`` where the viewer stands - its
    side at the point of the line nearest to it; or ``"auto"`` (None too): the side from which the aligned
    photos of ``chunk`` see the wall better (:func:`side_votes`; as well from either side: the right).

    :class:`~.wall_path.TraceRefused`: ``"points"`` for a line without two different points; ``"side"`` for
    ``"auto"`` without an aligned photo that looks at the wall, a point that lies on the line, anything else that
    is no point."""
    line = _plan_points(points)
    if side is None or (isinstance(side, str) and side == "auto"):
        votes = side_votes(chunk, line)
        if not chunk.aligned_cameras or not max(votes.values()) > 0.0:
            raise TraceRefused("side", ("no aligned photo" if not chunk.aligned_cameras else "no aligned photo "
                                        "looks at the wall: none") + " says from which side the wall is seen: say "
                               "'left' or 'right' of the line as it was clicked, or a point where the viewer stands")
        return "right" if votes["right"] >= votes["left"] else "left"
    if isinstance(side, str):
        if side in ("left", "right"):
            return side
        raise TraceRefused("side", f"the side of the viewer is 'left', 'right', 'auto' or a point of the plan, "
                                   f"not {side!r}")
    try:
        point = np.asarray(side, np.float64).reshape(-1)
    except (TypeError, ValueError):
        point = np.zeros(0)
    if len(point) not in (2, 3) or not np.isfinite(point[:2]).all():
        raise TraceRefused("side", f"the side of the viewer is 'left', 'right', 'auto' or a point of the plan, "
                                   f"not {side!r}")
    where = _sides(line, point[None, :2])[0][0]
    if where == 0:
        raise TraceRefused("side", "the point given for the side of the viewer lies on the line")
    return "right" if where > 0 else "left"


# ---------------------------------------------------------------- the snap
def _heights_near(chunk: Chunk, line: np.ndarray, band: float) -> tuple[float, float] | None:
    """The heights of the model along the polyline ``line``: the 1st and the 99th percentile of the height of its
    vertices within ``band`` of the line on the plan, each counted for the surface it stands for - a third of
    the area of every triangle it belongs to. A ground meshed finer than the wall does not outweigh it, and a
    spike of the mesh does not stretch them. None when no vertex is there, or all at one height."""
    from ..io.mesh_ply import read_mesh

    mesh = read_mesh(chunk.product_path("model"))
    xyz = chunk.to_world(mesh.xyz)
    lo, hi = line.min(axis=0) - band, line.max(axis=0) + band
    box = np.nonzero((xyz[:, 0] >= lo[0]) & (xyz[:, 0] <= hi[0]) & (xyz[:, 1] >= lo[1]) & (xyz[:, 1] <= hi[1]))[0]
    xy = xyz[box, :2] - line[0]
    near = np.zeros(len(box), bool)
    for a, b in zip(line[:-1] - line[0], line[1:] - line[0]):
        rest = np.nonzero(~near)[0]
        low, high = np.minimum(a, b) - band, np.maximum(a, b) + band  # the box of this segment first
        p = xy[rest]
        rest = rest[(p[:, 0] >= low[0]) & (p[:, 0] <= high[0]) & (p[:, 1] >= low[1]) & (p[:, 1] <= high[1])]
        step = b - a
        w = xy[rest] - a
        tau = np.clip(w @ step / (step @ step), 0.0, 1.0)
        near[rest] = ((w - tau[:, None] * step) ** 2).sum(axis=1) <= band * band
    rows = box[near]
    there = np.zeros(len(xyz), bool)
    there[rows] = True
    faces = np.asarray(mesh.faces)
    faces = faces[there[faces].any(axis=1)]
    a, b, c = (xyz[faces[:, k]] for k in range(3))
    area = np.linalg.norm(np.cross(b - a, c - a), axis=1) / 2.0
    weight = np.bincount(faces.ravel(), weights=np.repeat(area / 3.0, 3), minlength=len(xyz))[rows]
    if not weight.sum() > 0.0:
        return None
    order = np.argsort(xyz[rows, 2])
    height, share = xyz[rows, 2][order], np.cumsum(weight[order]) / weight.sum()
    v0, v1 = height[np.searchsorted(share, 0.01)], height[min(np.searchsorted(share, 0.99), len(height) - 1)]
    return (float(v0), float(v1)) if v1 > v0 else None


class _Sight:
    """The plane of a clicked segment as the frame of a :class:`~.elevation.FrameSurface` that says how far in
    front of the line it goes at each ``u`` (``front``, at the ``u`` of its columns): the rays start there."""

    kind = "plane"

    def __init__(self, plane, u: np.ndarray, front: np.ndarray):
        order = np.argsort(u)
        self.plane, self._u, self._front = plane, u[order], front[order]
        self.point, self.view, self.coords, self.max_depth = plane.point, plane.view, plane.coords, plane.max_depth

    def max_depth_at(self, u) -> np.ndarray:
        return np.interp(u, self._u, self._front)

    def min_depth_at(self, u) -> np.ndarray:
        return np.full(np.shape(u), -np.inf)


def _reach(v: np.ndarray, i: int, x: np.ndarray, towards: np.ndarray, band: float) -> np.ndarray:
    """How far in front of segment ``i`` of the clicked line ``v`` (towards its viewer) each of its columns ``x``
    looks from, when it looks from the band: ``band``, or less where another segment of the line is as near as
    its own before that - half way across a room both walls of which are traced, in a niche, by a corner seen
    from inside. A segment that looked from further would look through that other part of the wall."""
    step = v[i + 1] - v[i]
    p = v[i] + x[:, None] * step / np.hypot(*step)
    low, high = np.minimum(v[i], v[i + 1]) - 2.0 * band, np.maximum(v[i], v[i + 1]) + 2.0 * band
    others = np.array([j for j in range(len(v) - 1) if j != i and (np.maximum(v[j], v[j + 1]) >= low).all()
                       and (np.minimum(v[j], v[j + 1]) <= high).all()], np.intp)
    reach = np.full(len(x), band)
    if not len(others):
        return reach
    a, side = v[others], v[others + 1] - v[others]  # (others, 2): their starts, their steps
    square = (side * side).sum(axis=1)

    def own(depth: np.ndarray, rows: np.ndarray) -> np.ndarray:
        """Whether the columns ``rows``, that far in front, are still nearer to their segment than to any other."""
        w = (p[rows] + depth[:, None] * towards)[:, None, :] - a  # (rows, others, 2)
        tau = np.clip((w * side).sum(axis=2) / square, 0.0, 1.0)
        return np.hypot(*np.moveaxis(w - tau[..., None] * side, 2, 0)).min(axis=1) >= depth

    rows = np.nonzero(~own(reach, np.arange(len(x))))[0]  # the distance to another segment less the depth only falls
    lo, hi = np.zeros(len(rows)), np.full(len(rows), band)
    for _ in range(24):
        mid = (lo + hi) / 2.0
        held = own(mid, rows)
        lo, hi = np.where(held, mid, lo), np.where(held, hi, mid)
    reach[rows] = lo
    return reach


def _cast(chunk: Chunk, raycaster, lock, start: np.ndarray, direction: np.ndarray) -> np.ndarray:
    """How far (world metres, infinite for nothing) the first surface of the model is from each of the world
    points ``start``, along the world direction ``direction`` (one for all of them)."""
    inverse = np.linalg.inv(chunk.transform)
    along = inverse[:3, :3] @ np.asarray(direction, np.float64)
    along = np.tile(along / np.linalg.norm(along), (len(start), 1))
    with lock:
        t = raycaster.hit_distance(start @ inverse[:3, :3].T + inverse[:3, 3], along)
    return t * chunk.scale


def _from_line(chunk: Chunk, raycaster, lock, plane, u: np.ndarray, v: np.ndarray, top: float,
               band: float) -> np.ndarray:
    """The depth of the face at the samples ``(u, v)`` of the plane of a clicked segment, looked for from the
    clicked line itself: the surface nearest the line that faces the viewer, within ``band`` (NaN for none).

    A ray straight up says what the click stands on. With a surface above the sample, no higher than
    :data:`OVER_TOP` over ``top`` (the highest height looked at), the sample is under the model - the crest of the
    wall, the ground beside a trench - and the face is the first surface in front of the line, met from inside.
    With nothing above it the sample is in the open - the floor in front of the wall, the air of a trench - and
    the face is the first surface behind the line; or, when that is nearer, the way out of what stands in front:
    where the look goes into something and comes out of it again, a thin wall clicked past its back. A wall and a
    trench are each the mould of the other: only the vertical tells which side of a surface is the air."""
    normal = plane.normal  # towards the viewer
    on = plane.point(u, v, np.zeros(len(u)))
    under = _under(chunk, raycaster, lock, on, top)
    depth = np.full(len(u), np.nan)
    rows = np.nonzero(under)[0]
    if len(rows):  # under the model: the way out towards the viewer
        t = _cast(chunk, raycaster, lock, on[rows] - _OVERLAP * normal, normal) - _OVERLAP
        depth[rows] = np.where(t <= band, t, np.nan)
    rows = np.nonzero(~under)[0]
    if len(rows):  # in the open: the first surface behind the line ...
        t = _cast(chunk, raycaster, lock, on[rows] + _OVERLAP * normal, -normal) - _OVERLAP
        behind = np.where(t <= band, -t, np.nan)
        out = np.full(len(rows), np.nan)  # ... or the way out of what stands in front of it
        into = _cast(chunk, raycaster, lock, on[rows], normal)
        some = np.nonzero(into < band)[0]
        if len(some):
            far = into[some] + _PAST
            far = far + _cast(chunk, raycaster, lock, on[rows[some]] + far[:, None] * normal, normal)
            out[some] = np.where(far <= band, far, np.nan)
        depth[rows] = np.where(np.isnan(out) | (np.abs(behind) <= out), behind, out)
    return depth


def _under(chunk: Chunk, raycaster, lock, at: np.ndarray, top: float) -> np.ndarray:
    """Whether each of the world points ``at`` is under the model: a surface above it, no higher than
    :data:`OVER_TOP` over ``top`` (what stands higher is a cover, not the ground or the crest of a wall)."""
    return at[:, 2] + _cast(chunk, raycaster, lock, at, np.array([0.0, 0.0, 1.0])) <= top + OVER_TOP


def _columns(d: np.ndarray, open_rows: np.ndarray | None = None) -> np.ndarray:
    """The depth of the face in each column of a clicked segment, of the depths ``d`` (columns, heights) its
    samples see: their median - NaN for a column with surface at fewer than :data:`~.elevation.FACE_ROWS` of
    its heights (the ground the lowest rays meet anywhere is not a face). With ``open_rows`` (columns, heights:
    the samples that are not under the model on the viewer's side), a column with surface at that share of its
    open heights is of the face too: a low face, of a wall against high ground, only stands over that ground."""
    with warnings.catch_warnings():  # a column that sees nothing: "All-NaN slice"
        warnings.simplefilter("ignore", RuntimeWarning)
        column = np.nanmedian(d, axis=1)
    seen = np.isfinite(d)
    enough = seen.mean(axis=1) >= FACE_ROWS
    if open_rows is not None:
        out = open_rows.sum(axis=1)
        enough |= (out >= 2) & ((seen & open_rows).sum(axis=1) >= FACE_ROWS * np.maximum(out, 1))
    column[~enough] = np.nan
    return column


def _least_squares(x: np.ndarray, d: np.ndarray) -> tuple[float, float]:
    """``(a, b)`` of the line ``d = a + b x`` through the points, by least squares."""
    xm, dm = x.mean(), d.mean()
    spread = ((x - xm) ** 2).sum()
    b = float(((x - xm) * (d - dm)).sum() / spread) if spread > 0 else 0.0
    return float(dm - b * xm), b


def _median_line(x: np.ndarray, d: np.ndarray) -> tuple[float, float]:
    """``(a, b)`` of the line ``d = a + b x`` most of the points lie on: ``b`` the median, point by point, of the
    median slope from that point to all the others (the repeated median: it holds with nearly half the points
    off the line, wherever they are along it), ``a`` the median of what is left. The slope is taken on
    :data:`FIT_COLUMNS` points at most, picked evenly."""
    pick = np.unique(np.round(np.linspace(0, len(x) - 1, min(len(x), FIT_COLUMNS))).astype(np.intp))
    run = x[pick][None, :] - x[pick][:, None]
    np.fill_diagonal(run, np.nan)
    b = float(np.median(np.nanmedian((d[pick][None, :] - d[pick][:, None]) / run, axis=1)))
    return float(np.median(d - b * x)), b


def _fit(x: np.ndarray, d: np.ndarray, width: float) -> tuple[float, float]:
    """The line ``d = a + b x`` of the face a segment sees in its columns (``x`` their middles, ``d`` their
    depths, NaN where they see nothing, ``width`` of a column), fitted twice: first the line most columns lie on
    (:func:`_median_line`), then by least squares without the columns further than :data:`FIT_MAD` median
    deviations from it - what stands in front of the wall, an opening, the other wall past a corner. Where the
    columns that see a surface span less than :data:`MIN_FIT`, their median and no slope: a shift."""
    seen = np.nonzero(np.isfinite(d))[0]
    x, d = x[seen], d[seen]
    if (seen[-1] - seen[0] + 1) * width < MIN_FIT or len(x) < 2:
        return float(np.median(d)), 0.0
    a, b = _median_line(x, d)
    off = np.abs(d - (a + b * x))
    kept = off <= FIT_MAD * np.median(off)
    if kept.sum() >= 2:
        a, b = _least_squares(x[kept], d[kept])
    return a, b


def _apart(fits: np.ndarray, length: np.ndarray, found: np.ndarray, closed: bool = False) -> np.ndarray:
    """The segments of a clicked line whose fitted lines (``fits``, of those ``found``) are another face than that
    of their neighbours: at one end of its segment at least, the depth of a line off the clicked line is further
    than :data:`APART` from the median of the depths at both ends of the :data:`NEIGHBOURS` segments on either side
    of it that have a line - two at least. One goes at a time, the furthest first, and is no neighbour of the
    others after that: on a wall clicked as short segments, one that took the face of a tower beside it, or of a
    wall behind, does not make a zig-zag of the line, nor turn its neighbours out with it. On a ``closed`` line the
    neighbours go round."""
    ends = np.column_stack([fits[:, 0], fits[:, 0] + fits[:, 1] * length])  # the depth of each line at its ends
    n = len(ends)
    kept, out = found.copy(), np.zeros(n, bool)
    while True:
        far = np.zeros(n)
        for i in np.nonzero(kept)[0]:
            near = [i + k for k in range(-NEIGHBOURS, NEIGHBOURS + 1) if k]
            near = sorted({j % n for j in near} - {i}) if closed else [j for j in near if 0 <= j < n]
            near = [j for j in near if kept[j]]
            if len(near) >= 2:
                far[i] = np.abs(ends[i] - np.median(ends[near])).max()
        worst = int(np.argmax(far))
        if not far[worst] > APART:
            return out
        kept[worst], out[worst] = False, True


def _moved(v: np.ndarray, length: np.ndarray, t: np.ndarray, towards: np.ndarray, fits: np.ndarray,
           found: np.ndarray, closed: bool = False, band: float = np.inf) -> np.ndarray | None:
    """Where the vertices ``v`` of a clicked line go, given the line fitted to each segment that has one
    (``found``; ``fits`` its depth at the start of the segment and its slope, along ``towards``, the normal of
    the segment towards the viewer).

    A vertex between two fitted lines goes where they meet. Where the two turn by less than
    :data:`SAME_WALL_DEG` they are one wall going on and would meet anywhere along it: the vertex is moved off
    the clicked line by the mean of its distances from the two, along the bisector of their normals - the point
    as far from the one line as from the other, which is where they meet when the vertex is as far from both
    (a click on a straight wall, the chords of a curve). A vertex with one fitted line goes onto it along the
    normal of that segment. The others move as the nearest vertices that have a line do, in proportion along
    the clicked line between two of them. On a ``closed`` line - one that ends on its first point, a room traced
    all round - that point is one vertex, between the last segment and the first.

    The vertices keep the order clicked. Where a segment would be turned back - two lines that meet past the
    next vertex or before the one before, as do those of the short chords of a smoothed corner, each at another
    depth - its vertices are put between their two lines, as above, and not where they meet.

    Two lines that each pass within ``band`` of a vertex meet within ``band`` over the cosine of half the angle
    between them: the band itself for two lines that turn a little, 1.41 times it at a right angle. Where they meet
    further - lines that turn a little, found at different depths, meet far along the wall - the vertex goes
    between them, as above. A turn of a right angle or more (a hairpin), whose lines can meet far beyond the band,
    keeps the bound of the right angle: where they meet further than 1.41 times the band its vertex goes half way
    between them.

    None when no segment has a line, and when the order cannot be kept even so."""
    start = v[:-1] + fits[:, :1] * towards  # each fitted line at the two ends of its segment
    end = v[1:] + (fits[:, 0] + fits[:, 1] * length)[:, None] * towards
    slope = np.hypot(1.0, fits[:, 1])[:, None]
    heading, across = (t + fits[:, 1:] * towards) / slope, (towards - fits[:, 1:] * t) / slope  # along it, off it
    last = len(v) - 1
    meet = np.full(v.shape, np.nan)  # of a vertex between two segments: where their two lines meet
    between = np.full(v.shape, np.nan)  # ... and the place between them; of an end, the foot on its line
    joints = [(j - 1, j, j) for j in range(1, last)]  # the segment that ends at a vertex, the one that starts, it
    if closed:
        joints.append((last - 1, 0, 0))
    else:
        if found[0]:
            between[0] = start[0]
        if found[-1]:
            between[-1] = end[-1]
    for a, b, j in joints:
        if found[a] and found[b]:
            cos = across[a] @ across[b]
            if cos > 0.0:  # as far from each line as the mean of how far the vertex is from the two
                off = ((end[a] - v[j]) @ across[a] + (start[b] - v[j]) @ across[b]) / 2.0
                between[j] = v[j] + off * (across[a] + across[b]) / (1.0 + cos)
            else:  # a turn of a right angle and more has no such place nearby: half way between its two feet
                between[j] = (end[a] + start[b]) / 2.0
            cross = heading[a, 0] * heading[b, 1] - heading[a, 1] * heading[b, 0]
            if abs(cross) >= np.sin(np.radians(SAME_WALL_DEG)):
                gap = start[b] - end[a]
                meet[j] = end[a] + heading[a] * (gap[0] * heading[b, 1] - gap[1] * heading[b, 0]) / cross
                # at a right angle and beyond (cos <= 0) the bound is that of the right angle: a hairpin's lines can
                # meet far beyond the band, and its vertex then goes between them
                limit = band / np.sqrt((1.0 + cos) / 2.0) if cos > 0.0 else np.sqrt(2.0) * band
                if np.hypot(*(meet[j] - v[j])) > limit:
                    meet[j] = np.nan  # further than two lines within the band of the vertex can meet: not there
        elif found[a]:
            between[j] = end[a]
        elif found[b]:
            between[j] = start[b]
    met = np.isfinite(meet[:, 0])  # the vertices put where their two lines meet
    placed = met | np.isfinite(between[:, 0])
    if not placed.any():
        return None
    run = np.concatenate([[0.0], np.cumsum(length)])
    while True:
        to = np.where(met[:, None], meet, between) - v
        out = v + np.column_stack([np.interp(run, run[placed], to[placed, k], period=run[-1] if closed else None)
                                   for k in (0, 1)])
        back = ((out[1:] - out[:-1]) * t).sum(axis=1) <= 0.0  # the segments turned back on themselves
        if not back.any():
            return out
        ends = np.zeros(len(v), bool)
        ends[:-1] |= back
        ends[1:] |= back
        if closed:
            ends[0] |= ends[-1]
        if not (ends & met).any():
            return None
        met &= ~ends


def snap_trace(chunk: Chunk, points, side="auto", band: float = SNAP_BAND, heights=None, step: float = 0.05,
               samples: int = 40, progress: Progress | None = None) -> dict:
    """The clicked line ``points`` (plan points in the order clicked, chunk CRS) moved onto the face of the wall
    seen from ``side`` (:func:`side_of`), as the model of ``chunk`` has it.

    Each clicked segment looks for the face from its own line, through its vertical plane, within ``band``
    metres in front of the line and behind it (:func:`_from_line`). What the click stands on says which way.
    Under the model - on the crest of the wall, on the ground beside a trench - the face is the first surface in
    front of the line, met from inside. In the open - on the floor in front of the wall, in the air of a trench -
    it is the first surface behind the line, or the way out of what stands in front of it when that is nearer (a
    thin wall clicked past its back). The other side of a narrow room, the side of the trench a wall stands in,
    are not taken for it. A segment that finds no line that way looks from ``band`` in front of the line at the
    first surface met, as far as the band behind it (a model that is the face alone, clicked behind it) - or from
    where another part of the line is as near as itself, when that is nearer than the band (:func:`_reach`).

    The face is the one seen from the viewer's side. A look from the band that starts under the model - under the
    high ground a wall stands against - meets the surfaces from inside what they bound, facing away from the
    viewer: the face of the trench on the other side of the wall, however tall, is never taken; a segment that
    finds only that keeps its click. Where the viewer's side is high ground, the heights of a column that count are
    those over it (in the open where the band starts): a low face, 20 cm over the ground, is a face.

    In columns ``step`` wide along the segment the depth of the face is the median of the surface over
    ``samples`` heights between the two of ``heights`` (default: those of the model along the line,
    :func:`_heights_near`); a column with surface at fewer than a tenth of them sees nothing. A line is fitted to
    the columns (:func:`_fit`: twice, the second time without the columns far from the first; a shift alone under
    :data:`MIN_FIT`), and the segment has that line when :data:`MIN_SNAP_SHARE` of its columns lie on it, within
    :data:`FIT_NEAR` - not when it sees the wall for a short stretch, or rubble and bushes at any depth.

    A line found is not taken - the segment keeps the line clicked - where it faces away from the viewer (above),
    where it goes further than ``band`` from the clicked segment anywhere along it, and where it lies off its click further than those of its neighbours do
    (:func:`_apart`): the face of a tower beside the wall, of another wall behind it. No vertex goes further than
    ``band`` from where it was clicked, but a corner, where two lines meet within the band of each (:func:`_moved`:
    1.41 times the band at most at a right angle).

    A vertex between two segments goes where their lines meet (between the two where they are one wall going on,
    or would meet out of the order of the vertices: :func:`_moved`), an end vertex onto the line of its segment
    along its normal; the two ends of a line that ends on its first point, a room traced all round, are one
    vertex. A segment without a line keeps the direction clicked and goes with its vertices: each is moved by the
    other segment it belongs to, an end that belongs to no other as the vertex next to it.

    Returns ``points`` (N, 2: the line snapped, in the order clicked), ``snapped`` (whether it was moved),
    ``shift`` (N: how far each vertex went, metres), ``median_shift``, ``max_shift`` and ``segments``: for each
    clicked segment ``offset`` (the depth of its line at its first point, towards the viewer), ``slope`` (per
    metre along the direction clicked) and ``share`` (of its columns, those that lie on that line); ``offset``
    and ``slope`` are 0 for a segment without a line - and ``kept``, whether the line it found was not taken (its
    ``offset`` and ``slope`` are those of that line); ``kept`` says of how many segments. ``snapped`` is False and
    the points are those clicked when no segment has a line, and when the lines found cannot keep the vertices in
    the order clicked.

    :class:`~.wall_path.TraceRefused` as :func:`side_of`; ``RuntimeError`` without a model."""
    from .visibility import raycaster_for_chunk

    progress = as_progress(progress)
    clicked = _xy(points)
    which = side_of(chunk, clicked, side)
    band, step, samples = float(band), float(step), int(samples)
    if not (np.isfinite(band) and band > 0.0):
        raise ValueError("band: the depth in front of the line and behind it where the face is looked for, in "
                         "metres, over zero")
    if not (np.isfinite(step) and step > 0.0):
        raise ValueError("step: the width of the columns along the line, in metres, over zero")
    if samples < 1:
        raise ValueError("samples: the number of heights the face is looked for at, one at least")
    if heights is not None:
        if not (len(heights) == 2 and np.isfinite(heights).all() and heights[0] < heights[1]):
            raise ValueError("heights: the lowest and the highest height the face is looked for at, in metres")
        heights = (float(heights[0]), float(heights[1]))
    if chunk.product_path("model") is None:
        raise RuntimeError("build the model first: a trace is snapped onto the face of the model")
    # the different points of the line: a point clicked twice is one vertex
    place, kept = np.zeros(len(clicked), np.intp), [0]
    for i in range(1, len(clicked)):
        if np.hypot(*(clicked[i] - clicked[kept[-1]])) > _MIN_STEP:
            kept.append(i)
        place[i] = len(kept) - 1
    line = clicked[kept]
    closed = len(line) > 3 and float(np.hypot(*(line[-1] - line[0]))) <= _MIN_STEP  # it ends on its first point
    v = line - line[0]
    length, t, _turn = _turns(v)
    towards = (1.0 if which == "right" else -1.0) * np.column_stack([t[:, 1], -t[:, 0]])
    fits, share = np.zeros((len(length), 2)), np.zeros(len(length))
    away = np.zeros(len(length), bool)  # the line found faces away from the viewer
    progress(0.0, "Looking for the face of the wall along the line")
    if heights is None:
        heights = _heights_near(chunk, line, band)
    if heights is not None:
        raycaster, lock = raycaster_for_chunk(chunk), threading.Lock()
        if raycaster is None:
            raise RuntimeError("the model of the chunk cannot be read: a trace is snapped onto the face of the model")
        rows = heights[0] + (np.arange(samples) + 0.5) * (heights[1] - heights[0]) / samples  # at their middles
        for i in range(len(length)):
            n = max(1, int(np.ceil(length[i] / step - 1e-9)))  # columns of step at most, taken at their middles
            x = (np.arange(n) + 0.5) * length[i] / n
            plane = plane_from_line(chunk, line[i], line[i + 1], (line[i] + line[i + 1]) / 2.0 + towards[i])
            u = plane.coords(np.column_stack([line[i] + x[:, None] * t[i], np.zeros(n)]))[:, 0]
            uu, hh = np.repeat(u, samples), np.tile(rows, n)

            def from_line() -> np.ndarray:
                return _from_line(chunk, raycaster, lock, plane, uu, hh, heights[1], band)

            reach = _reach(v, i, x, towards[i], band)
            # the samples in the open on the viewer's side, where the band starts: not under the high ground. A look
            # from the band that starts under it goes through what the model bounds, and the first surface it meets
            # faces away from the viewer - the back of the wall, however tall: it is not taken
            open_rows = ~_under(chunk, raycaster, lock, plane.point(uu, hh, np.repeat(reach, samples)), heights[1])

            met = {}

            def from_band() -> np.ndarray:  # the first surface met from the band in front of the line
                surface = FrameSurface(chunk, raycaster, _Sight(plane, u, reach), step, lock, (-band, band),
                                       (0.0, heights[0], float(length[i]), heights[1]))
                depth = surface.depth_at(uu, hh)
                met["away"] = np.where(open_rows, np.nan, depth)
                return np.where(open_rows, depth, np.nan)

            def from_under() -> np.ndarray:  # what the band met from under the model: said, never taken
                return met["away"]

            for look in (from_line, from_band, from_under):  # each only when those before it found no line
                d = _columns(look().reshape(n, samples), None if look is from_under else open_rows.reshape(n, samples))
                if np.isfinite(d).any():
                    a, b = _fit(x, d, float(length[i]) / n)
                    on = float((np.abs(d - (a + b * x)) <= FIT_NEAR).mean())
                    share[i] = max(share[i], on)
                    if on >= MIN_SNAP_SHARE:
                        fits[i], share[i], away[i] = (a, b), on, look is from_under
                        break
            progress((i + 1) / len(length))
    progress(1.0)
    found = (share >= MIN_SNAP_SHARE) & ~away
    beyond = found & ((np.abs(fits[:, 0]) > band) | (np.abs(fits[:, 0] + fits[:, 1] * length) > band))
    apart = _apart(fits, length, found & ~beyond, closed)
    kept = beyond | apart | away  # a line was found, but it is not taken: the segment keeps the line clicked
    moved = _moved(v, length, t, towards, fits, found & ~kept, closed, band)
    out = clicked if moved is None else (line[0] + moved)[place]
    shift = np.hypot(*(out - clicked).T)
    segments = [{"offset": 0.0, "slope": 0.0, "share": 0.0, "kept": False} if place[i] == place[i + 1] else
                {"offset": float(fits[place[i], 0]), "slope": float(fits[place[i], 1]),
                 "share": float(share[place[i]]), "kept": bool(kept[place[i]])} for i in range(len(clicked) - 1)]
    return {"points": out, "snapped": moved is not None, "shift": shift, "median_shift": float(np.median(shift)),
            "max_shift": float(shift.max()), "segments": segments, "kept": int(kept.sum())}


# ---------------------------------------------------------------- the frame
def _without_straight(points, front: float, back: float, tolerance: float = STRAIGHT) -> np.ndarray:
    """The vertices of a trace (in the order of the viewer) without the clicks on a straight wall that take the
    room of a corner. The arc of a corner takes half at most of each of its two segments
    (:class:`~.wall_path.WallPath`): a click 30 cm from a corner leaves it an arc of 15 cm, and the depth the
    frame keeps there shrinks with it. A vertex next to a corner whose arc (:func:`~.wall_path.corner_radii` of
    ``front`` and ``back``) is short of room on that side is left out when the line does without it: when every
    vertex left out between two kept stands within ``tolerance`` of the line between them. One goes at a time,
    until no corner is short of room or no vertex can go. The two ends stay."""
    p = _plan_points(points)
    kept = list(range(len(p)))
    while len(kept) > 2:
        q = p[kept]
        length, _t, turn = _turns(q - q[0])
        need = np.asarray(corner_radii(q, front, back)) * np.tan(np.abs(turn) / 2.0)  # of the vertices 1..n - 2
        short_before = np.zeros(len(q), bool)  # a corner whose arc asks more than half the segment before it
        short_after = np.zeros(len(q), bool)  # ... than half the segment after it
        short_before[1:-1], short_after[1:-1] = need > length[:-1] / 2.0, need > length[1:] / 2.0
        for k in range(1, len(q) - 1):
            if not (short_after[k - 1] or short_before[k + 1]):
                continue
            a, step = p[kept[k - 1]], p[kept[k + 1]] - p[kept[k - 1]]
            w = p[kept[k - 1] + 1:kept[k + 1]] - a
            tau = np.clip(w @ step / (step @ step), 0.0, 1.0)
            if np.hypot(*(w - tau[:, None] * step).T).max() <= tolerance:
                del kept[k]
                break
        else:
            break
    return p[kept]


def path_from_trace(chunk: Chunk, points, side="auto", front: float = DEFAULT_FRONT, back: float = DEFAULT_FRONT,
                    snap: bool = True, band: float = SNAP_BAND, heights=None, smooth: float = 0.0,
                    clicked=None) -> tuple[WallPath, dict]:
    """The frame of the wall whose line was clicked on the plan (``points``, in the order clicked), and a report.

    The line is smoothed when ``smooth`` (metres, :func:`~.wall_path.smooth_trace`) is over zero, snapped onto
    the face of the model when ``snap`` (:func:`snap_trace` within ``band``, at ``heights``), put in the order
    of the viewer on ``side`` (:func:`side_of`, asked of the points as clicked), and the clicks on a straight
    wall that take the room of a corner are left out (:func:`_without_straight`). The arcs of its corners are
    those of :func:`~.wall_path.corner_radii` for the depth kept in ``front`` of the face and at its ``back``.

    The frame keeps the line as it was clicked (``WallPath.clicked``) whenever its vertices are not those
    points: ``points`` themselves, or ``clicked`` when the caller says it - one that snapped the line already
    and gives it snapped, with ``snap`` off.

    The report: ``side`` (``"left"`` or ``"right"`` of the line as clicked), ``snapped`` (whether the snap moved
    it), ``median_shift`` and ``max_shift`` of its vertices (metres, 0 without a snap; ``band`` at most, but at a
    corner), ``kept`` (of how many clicked segments the snap did not take the line it found, :func:`snap_trace`),
    ``length`` of the line of the frame and ``corners``, how many it has.

    :class:`~.wall_path.TraceRefused` as :func:`side_of` and :class:`~.wall_path.WallPath`; for a line the snap
    made that the frame refuses, the refusal says so."""
    asked = _xy(points)
    which = side_of(chunk, asked, side)
    front, back, smooth = float(front), float(back), float(smooth)
    if not (np.isfinite(front) and front > 0.0 and np.isfinite(back) and back > 0.0):
        raise ValueError("front and back: the depth kept in front of the face and behind it, in metres, over zero")
    if not (np.isfinite(smooth) and smooth >= 0.0):
        raise ValueError("smooth: the length the clicked line is smoothed over, in metres, zero for none")
    line = smooth_trace(asked, smooth)
    report = {"side": which, "snapped": False, "median_shift": 0.0, "max_shift": 0.0, "kept": 0}
    if snap:
        result = snap_trace(chunk, line, which, band=band, heights=heights)
        line = result["points"]
        report.update({key: result[key] for key in ("snapped", "median_shift", "max_shift", "kept")})
    vertices = _without_straight(ordered(line, which), front, back)
    if clicked is not None:
        clicked = _xy(clicked)
    elif not np.array_equal(ordered(vertices, which), asked):
        clicked = asked
    try:
        path = WallPath(vertices, radius=front + CLEARANCE, radii=corner_radii(vertices, front, back),
                        clicked=clicked)
    except TraceRefused as refused:
        if not report["snapped"]:
            raise
        raise TraceRefused(refused.reason, f"the line snapped onto the wall cannot be its trace - {refused} - click "
                                           f"nearer to the face, or keep the line without the snap") from refused
    report.update({"length": path.length, "corners": len(path.corners())})
    return path, report
