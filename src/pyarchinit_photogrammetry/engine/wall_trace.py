"""From a line clicked on the plan to the frame of a wall: the side it is seen from, the snap onto its face.

A line clicked on the plan is never on the face of the wall: it runs on its crest, in front of it, a couple of
metres off. :func:`snap_trace` moves it onto the face of the model. Through the vertical plane of each clicked
segment the face is looked for in a band in front of the line and behind it, a line is fitted to what the segment
sees, and each vertex goes where the lines of its two segments meet. :func:`side_of` says from which side of the
line the wall is seen - a word, a point of the plan, or where most photos are - and :func:`path_from_trace` makes
the frame of the wall (:class:`~.wall_path.WallPath`) of the clicked line: smoothed and snapped as asked, put in
the order of the viewer, its fillets sized on the depth kept in front of the face and behind it.
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

SNAP_BAND = 2.5  # m in front of the clicked line and behind it where the face is looked for
MIN_SNAP_SHARE = 0.3  # the share of the columns of a segment that must see a surface for it to be moved
MIN_FIT = 0.5  # m of face a line is fitted to: a shorter stretch gives a shift alone
FIT_MAD = 3.0  # a column further than this many median deviations from the line fitted is left out of it
SAME_WALL_DEG = 5.0  # two fitted lines that turn less are one wall going on: no corner to put where they meet
STRAIGHT = 0.01  # m: a vertex this near the line between its neighbours is a click on a straight wall, not a turn


# ---------------------------------------------------------------- the side
def _sides(line: np.ndarray, points: np.ndarray) -> np.ndarray:
    """Of plan points (M, 2), each at the point of the polyline ``line`` (N, 2) nearest to it (the first of two
    equally near): +1 on the right of the line as it is travelled, -1 on its left, 0 on it. A point whose nearest
    point is a corner lies outside that corner: on the right of a left turn. Past an end, the side of the end
    segment carried on."""
    v = line - line[0]
    q = points - line[0]
    length, t, _turn = _turns(v)
    best = np.full(len(q), np.inf)
    seg, tau = np.zeros(len(q), np.intp), np.zeros(len(q))
    for i in range(len(length)):
        along = np.clip((q - v[i]) @ t[i], 0.0, length[i])
        dist = np.hypot(*(q - v[i] - along[:, None] * t[i]).T)
        nearer = dist < best
        best[nearer], seg[nearer], tau[nearer] = dist[nearer], i, along[nearer]
    foot = v[seg] + tau[:, None] * t[seg]
    heading = t[seg].copy()  # at a corner, the direction half way round it
    before = (tau <= 0.0) & (seg > 0)
    heading[before] += t[seg[before] - 1]
    after = (tau >= length[seg]) & (seg < len(length) - 1)
    heading[after] += t[seg[after] + 1]
    w = q - foot
    return -np.sign(heading[:, 0] * w[:, 1] - heading[:, 1] * w[:, 0])


def side_of(chunk: Chunk, points, side="auto") -> str:
    """The side of the clicked line ``points`` (plan points in the order clicked) the wall is seen from:
    ``"left"`` or ``"right"`` of the direction it was clicked in.

    ``side``: one of those two words, given back as it is; a plan point ``(x, y)`` where the viewer stands - its
    side at the point of the line nearest to it; or ``"auto"`` (None too): the side where most aligned photos of
    ``chunk`` are, one vote each at the point of the line nearest to it (as many on either side: the right).

    :class:`~.wall_path.TraceRefused`: ``"points"`` for a line without two different points; ``"side"`` for
    ``"auto"`` without an aligned photo, a point that lies on the line, anything else that is no point."""
    line = _plan_points(points)
    if side is None or (isinstance(side, str) and side == "auto"):
        cameras = chunk.aligned_cameras
        if not cameras:
            raise TraceRefused("side", "no aligned photo says from which side the wall is seen: say 'left' or "
                                       "'right' of the line as it was clicked, or a point where the viewer stands")
        centres = chunk.to_world(np.array([c.center for c in cameras]))[:, :2]
        return "right" if _sides(line, centres).sum() >= 0 else "left"
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
    where = _sides(line, point[None, :2])[0]
    if where == 0:
        raise TraceRefused("side", "the point given for the side of the viewer lies on the line")
    return "right" if where > 0 else "left"


# ---------------------------------------------------------------- the snap
def _heights_near(chunk: Chunk, line: np.ndarray, band: float) -> tuple[float, float] | None:
    """The 1st and the 99th percentile of the height of the vertices of the model within ``band`` of the polyline
    ``line`` on the plan (a spike of the mesh does not stretch them); None when no vertex is there, or all at one
    height."""
    from ..io.mesh_ply import read_mesh

    xyz = chunk.to_world(read_mesh(chunk.product_path("model")).xyz)
    lo, hi = line.min(axis=0) - band, line.max(axis=0) + band
    box = (xyz[:, 0] >= lo[0]) & (xyz[:, 0] <= hi[0]) & (xyz[:, 1] >= lo[1]) & (xyz[:, 1] <= hi[1])
    xy, z = xyz[box, :2] - line[0], xyz[box, 2]
    near = np.zeros(len(z), bool)
    for a, b in zip(line[:-1] - line[0], line[1:] - line[0]):
        rest = np.nonzero(~near)[0]
        step = b - a
        w = xy[rest] - a
        tau = np.clip(w @ step / (step @ step), 0.0, 1.0)
        near[rest] = ((w - tau[:, None] * step) ** 2).sum(axis=1) <= band * band
    if not near.any():
        return None
    v0, v1 = np.percentile(z[near], [1.0, 99.0])
    return (float(v0), float(v1)) if v1 > v0 else None


def _columns(surface: FrameSurface, start: np.ndarray, t: np.ndarray, length: float, step: float,
             heights: tuple[float, float], samples: int) -> tuple[np.ndarray, np.ndarray]:
    """What a clicked segment sees through its plane: the middles ``x`` (metres from ``start`` along ``t``) of
    its columns, ``step`` wide at most, and in each the median depth of the surface over ``samples`` heights
    between the two of ``heights`` - NaN for a column with surface at fewer than
    :data:`~.elevation.FACE_ROWS` of them (the ground the lowest rays meet anywhere is not a face)."""
    n = max(1, int(np.ceil(length / step - 1e-9)))
    x = (np.arange(n) + 0.5) * length / n
    u = surface.frame.coords(np.column_stack([start + x[:, None] * t, np.zeros(n)]))[:, 0]
    v = heights[0] + (np.arange(samples) + 0.5) * (heights[1] - heights[0]) / samples
    d = surface.depth_at(np.repeat(u, samples), np.tile(v, n)).reshape(n, samples)
    with warnings.catch_warnings():  # a column that sees nothing: "All-NaN slice"
        warnings.simplefilter("ignore", RuntimeWarning)
        column = np.nanmedian(d, axis=1)
    column[np.isfinite(d).mean(axis=1) < FACE_ROWS] = np.nan
    return x, column


def _least_squares(x: np.ndarray, d: np.ndarray) -> tuple[float, float]:
    """``(a, b)`` of the line ``d = a + b x`` through the points, by least squares."""
    xm, dm = x.mean(), d.mean()
    spread = ((x - xm) ** 2).sum()
    b = float(((x - xm) * (d - dm)).sum() / spread) if spread > 0 else 0.0
    return float(dm - b * xm), b


def _fit(x: np.ndarray, d: np.ndarray, width: float) -> tuple[float, float]:
    """The line ``d = a + b x`` of the face a segment sees in its columns (``x`` their middles, ``d`` their
    depths, NaN where they see nothing, ``width`` of a column): fitted twice, the second time without the
    columns further than :data:`FIT_MAD` median deviations from the first (what stands in front of the wall, an
    opening, the other wall past a corner). Where the columns that see a surface span less than
    :data:`MIN_FIT`, their median and no slope: a shift."""
    seen = np.nonzero(np.isfinite(d))[0]
    x, d = x[seen], d[seen]
    if (seen[-1] - seen[0] + 1) * width < MIN_FIT:
        return float(np.median(d)), 0.0
    a, b = _least_squares(x, d)
    off = d - (a + b * x)
    off = np.abs(off - np.median(off))
    kept = off <= FIT_MAD * np.median(off)
    if kept.sum() >= 2:
        a, b = _least_squares(x[kept], d[kept])
    return a, b


def _moved(v: np.ndarray, length: np.ndarray, t: np.ndarray, towards: np.ndarray, fits: np.ndarray,
           found: np.ndarray) -> np.ndarray | None:
    """Where the vertices ``v`` of a clicked line go, given the line fitted to each segment that has one
    (``found``; ``fits`` its depth at the start of the segment and its slope, along ``towards``, the normal of
    the segment towards the viewer). A vertex between two fitted lines goes where they meet, or half way between
    them where they turn by less than :data:`SAME_WALL_DEG` and would meet anywhere; a vertex with one fitted
    line goes onto it along the normal of that segment; the others move as the nearest vertices that have a
    line do, in proportion along the clicked line between two of them. None when no segment has a line, or
    when the lines found would turn a segment back on itself."""
    start = v[:-1] + fits[:, :1] * towards  # each fitted line at the two ends of its segment
    end = v[1:] + (fits[:, 0] + fits[:, 1] * length)[:, None] * towards
    heading = t + fits[:, 1:] * towards
    heading /= np.hypot(heading[:, 0], heading[:, 1])[:, None]
    out = np.full(v.shape, np.nan)
    if found[0]:
        out[0] = start[0]
    if found[-1]:
        out[-1] = end[-1]
    for j in range(1, len(v) - 1):
        a, b = j - 1, j  # the segment that ends at the vertex, the one that starts from it
        if found[a] and found[b]:
            cross = heading[a, 0] * heading[b, 1] - heading[a, 1] * heading[b, 0]
            if abs(cross) >= np.sin(np.radians(SAME_WALL_DEG)):
                gap = start[b] - end[a]
                out[j] = end[a] + heading[a] * (gap[0] * heading[b, 1] - gap[1] * heading[b, 0]) / cross
            else:
                out[j] = (end[a] + start[b]) / 2.0
        elif found[a]:
            out[j] = end[a]
        elif found[b]:
            out[j] = start[b]
    known = np.isfinite(out[:, 0])
    if not known.any():
        return None
    run = np.concatenate([[0.0], np.cumsum(length)])
    out = v + np.column_stack([np.interp(run, run[known], (out - v)[known, k]) for k in (0, 1)])
    if (((out[1:] - out[:-1]) * t).sum(axis=1) <= 0.0).any():
        return None
    return out


def snap_trace(chunk: Chunk, points, side="auto", band: float = SNAP_BAND, heights=None, step: float = 0.05,
               samples: int = 40, progress: Progress | None = None) -> dict:
    """The clicked line ``points`` (plan points in the order clicked, chunk CRS) moved onto the face of the wall
    seen from ``side`` (:func:`side_of`), as the model of ``chunk`` has it.

    Each clicked segment looks at the model through its own vertical plane, from ``band`` metres in front of
    the line to ``band`` behind it (the first surface met from the viewer's side: a click on the crest of the
    wall, or behind it, finds the face all the same; what stands in front of the wall within the band is met
    first - give a narrower band in a narrow room). In columns ``step`` wide along the segment the depth of the
    face is the median of the surface over ``samples`` heights between the two of ``heights`` (default: the 1st
    and the 99th percentile of the height of the vertices of the model within ``band`` of the line); a column
    with surface at fewer than a tenth of them sees nothing. A line is fitted to the columns of the segment
    (:func:`_fit`: twice, the second time without the columns far from the first; a shift alone under
    :data:`MIN_FIT`). A segment with fewer than :data:`MIN_SNAP_SHARE` of its columns on a surface has no line.

    A vertex between two segments goes where their lines meet, an end vertex onto the line of its segment
    along its normal. A segment without a line keeps the direction clicked and goes with its vertices: each is
    moved by the other segment it belongs to, an end that belongs to no other as the vertex next to it.

    Returns ``points`` (N, 2: the line snapped, in the order clicked), ``snapped`` (whether it was moved),
    ``shift`` (N: how far each vertex went, metres), ``median_shift``, ``max_shift`` and ``segments``, for each
    clicked segment ``offset`` (the depth of its fitted line at its first point, towards the viewer), ``slope``
    (per metre along the direction clicked) and ``share`` (of its columns that see a surface); ``offset`` and
    ``slope`` are 0 for a segment without a line. ``snapped`` is False, and the points are those clicked, when
    no segment has a line, and when the lines found would turn a segment of the line back on itself.

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
    v = line - line[0]
    length, t, _turn = _turns(v)
    towards = (1.0 if which == "right" else -1.0) * np.column_stack([t[:, 1], -t[:, 0]])
    fits, share = np.zeros((len(length), 2)), np.zeros(len(length))
    progress(0.0, "Looking for the face of the wall along the line")
    if heights is None:
        heights = _heights_near(chunk, line, band)
    if heights is not None:
        raycaster, lock = raycaster_for_chunk(chunk), threading.Lock()
        if raycaster is None:
            raise RuntimeError("the model of the chunk cannot be read: a trace is snapped onto the face of the model")
        for i in range(len(length)):
            plane = plane_from_line(chunk, line[i], line[i + 1], (line[i] + line[i + 1]) / 2.0 + towards[i])
            surface = FrameSurface(chunk, raycaster, plane, step, lock, (-band, band),
                                   (0.0, heights[0], float(length[i]), heights[1]))
            x, d = _columns(surface, line[i], t[i], float(length[i]), step, heights, samples)
            share[i] = np.isfinite(d).mean()
            if share[i] >= MIN_SNAP_SHARE:
                fits[i] = _fit(x, d, float(length[i]) / len(x))
            progress((i + 1) / len(length))
    progress(1.0)
    moved = _moved(v, length, t, towards, fits, share >= MIN_SNAP_SHARE)
    out = clicked.copy() if moved is None else (line[0] + moved)[place]
    shift = np.hypot(*(out - clicked).T)
    segments = [{"offset": 0.0, "slope": 0.0, "share": 0.0} if place[i] == place[i + 1] else
                {"offset": float(fits[place[i], 0]), "slope": float(fits[place[i], 1]),
                 "share": float(share[place[i]])} for i in range(len(clicked) - 1)]
    return {"points": out, "snapped": moved is not None, "shift": shift, "median_shift": float(np.median(shift)),
            "max_shift": float(shift.max()), "segments": segments}


# ---------------------------------------------------------------- the frame
def _without_straight(points, front: float, back: float, tolerance: float = STRAIGHT) -> np.ndarray:
    """The vertices of a trace (in the order of the viewer) without the clicks on a straight wall that take the
    room of a corner. The arc of a corner takes half at most of each of its two segments
    (:class:`~.wall_path.WallPath`): a click 30 cm from a corner leaves it an arc of 15 cm, and the depth the
    frame keeps there shrinks with it. A vertex next to a corner whose arc (:func:`~.wall_path.corner_radii` of
    ``front`` and ``back``) is short of room on that side is left out when the line does without it: when every
    vertex left out between two kept stands within ``tolerance`` of the line between them. The one nearest that
    line goes first, until no corner is short of room or no vertex can go. The two ends stay."""
    p = _plan_points(points)
    kept = list(range(len(p)))
    while len(kept) > 2:
        q = p[kept]
        length, _t, turn = _turns(q - q[0])
        need = np.asarray(corner_radii(q, front, back)) * np.tan(np.abs(turn) / 2.0)  # of the vertices 1..n - 2
        short_before = np.zeros(len(q), bool)  # a corner whose arc asks more than half the segment before it
        short_after = np.zeros(len(q), bool)  # ... than half the segment after it
        short_before[1:-1], short_after[1:-1] = need > length[:-1] / 2.0, need > length[1:] / 2.0
        best = None
        for k in range(1, len(q) - 1):
            if not (short_after[k - 1] or short_before[k + 1]):
                continue
            a, step = p[kept[k - 1]], p[kept[k + 1]] - p[kept[k - 1]]
            w = p[kept[k - 1] + 1:kept[k + 1]] - a
            tau = np.clip(w @ step / (step @ step), 0.0, 1.0)
            off = float(np.hypot(*(w - tau[:, None] * step).T).max())
            if off <= tolerance and (best is None or off < best[0]):
                best = (off, k)
        if best is None:
            break
        del kept[best[1]]
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
    it), ``median_shift`` and ``max_shift`` of its vertices (metres, 0 without a snap), ``length`` of the line of
    the frame and ``corners``, how many it has.

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
    report = {"side": which, "snapped": False, "median_shift": 0.0, "max_shift": 0.0}
    if snap:
        result = snap_trace(chunk, line, which, band=band, heights=heights)
        line = result["points"]
        report.update({key: result[key] for key in ("snapped", "median_shift", "max_shift")})
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
