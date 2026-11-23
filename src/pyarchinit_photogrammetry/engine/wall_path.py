"""The frame of a wall traced on the plan: an elevation that follows the wall round its corners.

The trace is a line on the plan (x, y of the chunk CRS, metres); the viewer stands on the right of the direction it
is travelled. The line of the frame is the trace with an arc (a fillet) at each corner, so that its normal turns
without a jump: in front of a convex corner the strip fans out, in a re-entrant corner it does not fold. A cell
``(u, v)`` and a depth ``d`` map to the world as on a :class:`~.elevation.Plane`: ``u`` is the length along the line
(through a ``u_map``, the length on the face of the wall), left to right as seen by the viewer, ``v`` the height
(world Z), ``d`` the offset along the normal, towards the viewer.

A world point belongs to the point of the line nearest to it. Where two parts of the line are equally near - the
centre of a fillet, half way across a room - the frame ends: ``max_depth_at(u)`` and ``min_depth_at(u)`` are how far
it goes in front of the line and behind it at ``u``, a clearance short of that place, and ``coords`` answers NaN for
the points beyond. No world point is seen twice.

Everything is computed in offsets from the first vertex: with projected coordinates (millions of metres, where
float64 resolves half a nanometre) the lengths and the fillets lose no digit.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

CLEARANCE = 0.02  # m between the end of the frame and the place where two parts of the line are equally near
DEFAULT_FRONT = 0.5  # m of depth kept in front of the face
DEFAULT_RADIUS = DEFAULT_FRONT + CLEARANCE  # the fillet asked: a re-entrant corner then leaves that depth whole
CORNER_SHARE = 0.8  # the share of the depth kept that the corner as traced may stand off its arc
MAX_TURN_DEG = 170.0  # a sharper turn is the line going back on itself
REASONS = ("points", "crosses", "turns_back", "side")

_MIN_STEP = 1e-9  # m: consecutive points closer than this are one point
_MIN_TURN = 1e-9  # rad: a vertex that turns less has no arc
_REACH_LIMIT = 1000.0  # m: a place of the line that owns its normal this far owns it for good
_REACH_STEP = 1e-7  # m: where the bisection of the reach stops
_SAME_PLACE = 1e-6  # m along the line: two points of it closer than this are one place
_BLOCK = 1 << 20  # points x primitives worked at once


class TraceRefused(ValueError):
    """The line cannot be the trace of a wall; ``reason`` is one of :data:`REASONS`."""

    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason


def _interp(x, xp, fp) -> np.ndarray:
    """Piecewise linear ``fp(xp)`` at ``x``, prolonged past both ends by the end segments (``x`` any shape,
    a scalar too)."""
    x = np.asarray(x, np.float64)
    flat = np.atleast_1d(x).ravel()
    y = np.interp(flat, xp, fp)
    lo, hi = flat < xp[0], flat > xp[-1]
    y[lo] = fp[0] + (flat[lo] - xp[0]) * (fp[1] - fp[0]) / (xp[1] - xp[0])
    y[hi] = fp[-1] + (flat[hi] - xp[-1]) * (fp[-1] - fp[-2]) / (xp[-1] - xp[-2])
    return y.reshape(x.shape)


def _xy(points) -> np.ndarray:
    """Plan points as a new (N, 2) array (a third column, the height, is left out)."""
    p = np.asarray(points, np.float64)
    return np.array(p[:, :2] if p.ndim == 2 and p.shape[1] >= 2 else p.reshape(-1, 2))


def _plan_points(points) -> np.ndarray:
    """The points of a trace, consecutive points within a nanometre taken once."""
    p = _xy(points)
    if not np.isfinite(p).all():
        raise TraceRefused("points", "a point of the line has no valid coordinates")
    keep: list[np.ndarray] = []
    for q in p:
        if not keep or np.hypot(*(q - keep[-1])) > _MIN_STEP:
            keep.append(q)
    if len(keep) < 2:
        raise TraceRefused("points", "the line of a wall needs two different points at least")
    return np.array(keep)


def _turns(offsets: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Of a polyline: the length and the direction of each segment, and the turn (radians, positive to the
    left) at each interior vertex."""
    step = np.diff(offsets, axis=0)
    length = np.hypot(step[:, 0], step[:, 1])
    t = step / length[:, None]
    turn = np.arctan2(t[:-1, 0] * t[1:, 1] - t[:-1, 1] * t[1:, 0], (t[:-1] * t[1:]).sum(axis=1))
    return length, t, turn


def _rot(e: np.ndarray, angle: np.ndarray) -> np.ndarray:
    """The plan vectors ``e`` (..., 2) turned by ``angle`` (radians, anticlockwise)."""
    c, s = np.cos(angle), np.sin(angle)
    return np.stack([e[..., 0] * c - e[..., 1] * s, e[..., 0] * s + e[..., 1] * c], axis=-1)


@dataclass
class WallPath:
    """The line of a wall traced on the plan, an arc at each corner, as the frame of an elevation.

    ``vertices``: the plan points (N, 2) **in the order ``u`` grows, the viewer on the right of the direction of
    travel** (:func:`ordered` puts a clicked line that way). ``radius``: the radius asked for the arc of every
    corner; ``radii``: one per interior vertex in its place (:func:`corner_radii`). An arc takes half at most of
    each of the two segments it joins: corners close to each other share the short side, with smaller arcs.
    ``u_map``: ``[s, u]`` pairs, both strictly increasing, two at least, ``s`` the length along the line; ``u`` is
    then the length on the face of the wall (linear between the pairs, prolonged past both ends by the end
    pairs); without a map ``u`` = ``s``. ``clicked``: the trace as it was clicked, in the order clicked (kept with
    the frame; the geometry does not read it).

    Past its two ends the line goes on straight, along the end tangents (``u`` below that of the start, above
    that of the end). A closed line - a room traced all round - is a strip from its first point: where it closes
    there is no arc, the two ends meet at a sharp corner and what is as near to one as to the other belongs to
    the start. Refused (:class:`TraceRefused`): fewer than two different points, a turn over
    :data:`MAX_TURN_DEG`, a line that crosses or touches itself.
    """

    vertices: np.ndarray
    radius: float = DEFAULT_RADIUS
    radii: list[float] | None = None
    u_map: np.ndarray | None = None
    clicked: np.ndarray | None = None
    kind: str = field(default="path", init=False)

    def __post_init__(self):
        self.vertices = _plan_points(self.vertices)
        self._origin = self.vertices[0].copy()
        offsets = self.vertices - self._origin
        length, t, turn = _turns(offsets)
        if len(turn) and np.abs(turn).max() > np.radians(MAX_TURN_DEG):
            i = int(np.abs(turn).argmax())
            raise TraceRefused("turns_back", f"the line turns back at point {i + 2} ({abs(np.degrees(turn[i])):.0f} "
                                             f"degrees, {MAX_TURN_DEG:.0f} at most): trace the two faces as two walls")
        from shapely.geometry import LineString

        if not LineString(offsets).is_simple:
            raise TraceRefused("crosses", "the line crosses or touches itself: trace it as two walls")
        self.radius = float(self.radius)
        asked = [self.radius] * len(turn)
        if self.radii is not None:
            self.radii = asked = [float(r) for r in self.radii]
            if len(asked) != len(turn):
                raise ValueError(f"one radius per interior vertex: {len(turn)} needed, {len(asked)} given")
        if not all(np.isfinite(r) and r > 0 for r in [self.radius, *asked]):
            raise ValueError("the radius of a corner must be a positive length")
        if self.u_map is not None:
            m = np.asarray(self.u_map, np.float64).reshape(-1, 2)
            if len(m) < 2 or not (np.all(np.diff(m[:, 0]) > 0) and np.all(np.diff(m[:, 1]) > 0)):
                raise ValueError("the u map needs two pairs at least, s and u both strictly increasing")
            self.u_map = m
        if self.clicked is not None:
            self.clicked = _xy(self.clicked)
        self._build(offsets, length, t, turn, asked)

    def _build(self, v, length, t, turn, asked) -> None:
        """The line as primitives in order - a straight run, then the arc of the next vertex - each with the
        ``s`` of its start. All in offsets from the first vertex."""
        n = len(v)
        cut, rho = np.zeros(n), np.zeros(n)  # what the arc of a vertex takes of its two segments, its radius
        for i in range(1, n - 1):
            half = abs(turn[i - 1]) / 2.0
            if 2.0 * half <= _MIN_TURN:
                continue
            cut[i], rho[i] = asked[i - 1] * np.tan(half), asked[i - 1]
            room = min(length[i - 1], length[i]) / 2.0
            if cut[i] > room:
                cut[i], rho[i] = room, room / np.tan(half)
        zero = np.zeros(2)
        rows: list[dict] = []
        self._corners: list[tuple[int, int]] = []  # (vertex, primitive) of each arc
        vertex_s = np.zeros(n)
        s = 0.0
        for i in range(n - 1):
            a, b = v[i] + cut[i] * t[i], v[i + 1] - cut[i + 1] * t[i]
            run = length[i] - cut[i] - cut[i + 1]
            normal = np.array([t[i, 1], -t[i, 0]])  # towards the viewer, on the right of the travel
            if run > 0.0:  # two arcs that take half a segment each leave no run between them
                rows.append({"seg": True, "s0": s, "len": run, "a": a, "b": b, "t": t[i], "n": normal, "c": zero,
                             "rho": 1.0, "sign": 0.0, "half": 0.0, "e0": zero, "m": zero})
                s += run
            if i + 1 == n - 1:
                break
            vertex_s[i + 1] = s
            if cut[i + 1] > 0.0:  # the arc of vertex i + 1, from the end of this run to the start of the next
                sign, half = np.sign(turn[i]), abs(turn[i]) / 2.0
                e0 = sign * normal  # from the centre to the start of the arc
                arc = 2.0 * half * rho[i + 1]
                self._corners.append((i + 1, len(rows)))
                rows.append({"seg": False, "s0": s, "len": arc, "a": b, "b": v[i + 1] + cut[i + 1] * t[i + 1],
                             "t": zero, "n": zero, "c": b - rho[i + 1] * e0, "rho": rho[i + 1], "sign": sign,
                             "half": half, "e0": e0, "m": _rot(e0, sign * half)})
                vertex_s[i + 1] = s + arc / 2.0
                s += arc
        vertex_s[-1] = s
        self._length, self._vertex_s = float(s), vertex_s  # the s of each vertex: of the middle of its arc
        col = {name: np.array([r[name] for r in rows], np.float64) for name in rows[0] if name != "seg"}
        self._seg = np.array([r["seg"] for r in rows])  # a straight run, or an arc
        self._iseg, self._iarc = np.nonzero(self._seg)[0], np.nonzero(~self._seg)[0]
        self._s0, self._len = col["s0"], col["len"]  # of each primitive: the s of its start, its length
        self._a, self._b = col["a"], col["b"]  # its two ends
        self._t, self._n = col["t"], col["n"]  # of a run: its direction, its normal towards the viewer
        self._c, self._rho = col["c"], col["rho"]  # of an arc: its centre, its radius
        self._sign, self._half = col["sign"], col["half"]  # +1 turning left, -1 right; half its turn (radians)
        self._e0, self._m = col["e0"], col["m"]  # unit vectors from the centre to its start, to its middle
        self._cos_half = np.cos(self._half)

    # ------------------------------------------------------------ the line
    @property
    def length(self) -> float:
        """The length of the line between its two ends, arcs included."""
        return self._length

    @property
    def min_radius(self) -> float:
        """The smallest radius among the arcs as built (infinite for a line without corners)."""
        return float(self._rho[self._iarc].min()) if len(self._iarc) else np.inf

    def corners(self) -> list[dict]:
        """The corners in the order of ``u``: ``s`` and ``u`` of the middle of the arc, ``angle_deg`` the turn
        (positive for a convex corner, a left turn: the viewer outside it; negative for a re-entrant one),
        ``radius`` of the arc as built, ``xy`` the corner as traced (its vertex, plan coordinates)."""
        return [{"s": float(self._vertex_s[i]), "u": float(self.u_of(self._vertex_s[i])),
                 "angle_deg": float(np.degrees(2.0 * self._sign[k] * self._half[k])), "radius": float(self._rho[k]),
                 "xy": self.vertices[i].tolist()} for i, k in self._corners]

    def u_of(self, s) -> np.ndarray:
        """``u`` of the length ``s`` along the line (``s`` itself without a ``u_map``)."""
        if self.u_map is None:
            return np.array(s, np.float64)
        return _interp(s, self.u_map[:, 0], self.u_map[:, 1])

    def s_of(self, u) -> np.ndarray:
        """The length along the line of ``u``."""
        if self.u_map is None:
            return np.array(u, np.float64)
        return _interp(u, self.u_map[:, 1], self.u_map[:, 0])

    def trace_line(self, u0=None, u1=None) -> np.ndarray:
        """The trace as it was drawn, with its sharp corners (M, 2), from ``u0`` to ``u1`` (``u0`` the smaller):
        the frame's point at ``u0``, the vertices whose corner lies between the two, the frame's point at ``u1``.
        Without ``u0`` it starts at the first vertex, without ``u1`` it ends at the last: by default, the
        vertices."""
        if u0 is None and u1 is None:
            return self.vertices.copy()
        s0 = -np.inf if u0 is None else float(self.s_of(u0))
        s1 = np.inf if u1 is None else float(self.s_of(u1))
        inner = self._vertex_s[1:-1]
        return np.vstack([self.vertices[0] if u0 is None else self.point(u0, 0.0, 0.0)[:2],
                          self.vertices[1:-1][(inner > s0) & (inner < s1)],
                          self.vertices[-1] if u1 is None else self.point(u1, 0.0, 0.0)[:2]])

    def _at(self, s: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """The point of the line at each ``s`` (1-D) and its normal towards the viewer; before 0 and past the
        length, on the end tangents."""
        k = np.clip(np.searchsorted(self._s0, s, side="right") - 1, 0, len(self._s0) - 1)
        return self._place(k, s - self._s0[k])

    def _place(self, k: np.ndarray, tau: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """The point of the primitives ``k`` at ``tau`` metres from their start, and its normal towards the
        viewer."""
        p, n = np.empty((len(k), 2)), np.empty((len(k), 2))
        seg = self._seg[k]
        i = k[seg]
        p[seg] = self._a[i] + tau[seg, None] * self._t[i]
        n[seg] = self._n[i]
        i = k[~seg]
        e = _rot(self._e0[i], self._sign[i] * tau[~seg] / self._rho[i])
        p[~seg] = self._c[i] + self._rho[i, None] * e
        n[~seg] = self._sign[i, None] * e
        return p, n

    # ------------------------------------------------------------ the frame
    def point(self, u, v, d) -> np.ndarray:
        """The world point of ``(u, v, d)``: scalars, or arrays of shapes that broadcast."""
        u, v, d = np.broadcast_arrays(*(np.asarray(a, np.float64) for a in (u, v, d)))
        p, n = self._at(self.s_of(u).ravel())
        out = np.empty(u.shape + (3,))
        out[..., :2] = (self._origin + (p + d.reshape(-1, 1) * n)).reshape(u.shape + (2,))
        out[..., 2] = v
        return out

    def view(self, u, v) -> np.ndarray:
        """The direction towards the viewer at ``u``: horizontal, the normal of the line there."""
        u = np.asarray(u, np.float64)
        out = np.zeros(u.shape + (3,))
        out[..., :2] = self._at(self.s_of(u).ravel())[1].reshape(u.shape + (2,))
        return out

    def coords(self, p: np.ndarray) -> np.ndarray:
        """``(u, v, d)`` of world points, taken at the nearest point of the line (the first of two equally
        near; past an end of the line, on its tangent there). A row of NaN for a point that is not on the frame:
        deeper than ``max_depth_at(u)`` or ``min_depth_at(u)`` allow, or without valid coordinates."""
        p = np.asarray(p, np.float64).reshape(-1, 3)
        out = np.full((len(p), 3), np.nan)
        rows = np.nonzero(np.isfinite(p).all(axis=1))[0]
        s, d = self._locate(p[rows, :2] - self._origin)
        # within the limits: with the clearance added to its depth the point is still of the same place of the line
        on = self._owns(s, np.sign(d) * np.minimum(np.abs(d) + CLEARANCE, _REACH_LIMIT))
        past = np.nonzero(on & ((s < 0.0) | (s > self._length)))[0]  # past an end the place may be of another part
        on[past] = self._owns(s[past], np.zeros(len(past)))
        rows, s, d = rows[on], s[on], d[on]
        out[rows] = np.column_stack([self.u_of(s), p[rows, 2], d])
        return out

    def max_depth(self) -> float:
        """No limit for the whole frame: the one applied is ``max_depth_at(u)``."""
        return np.inf

    def max_depth_at(self, u) -> np.ndarray:
        """How far in front of the line (towards the viewer) the frame goes at each ``u``: up to where another
        part of the line is as near, less :data:`CLEARANCE`; infinite where nothing else is ever nearer (a
        straight wall, the outside of a corner)."""
        return np.maximum(self._reach_at(u, 1.0) - CLEARANCE, 0.0)

    def min_depth_at(self, u) -> np.ndarray:
        """How far behind the line the frame goes at each ``u``, as a depth (negative, or minus infinity)."""
        return np.minimum(CLEARANCE - self._reach_at(u, -1.0), 0.0)

    def to_dict(self) -> dict:
        out = {"kind": "path", "vertices": self.vertices.tolist(), "radius": self.radius}
        if self.radii is not None:
            out["radii"] = list(self.radii)
        if self.u_map is not None:
            out["u_map"] = self.u_map.tolist()
        if self.clicked is not None:
            out["clicked"] = self.clicked.tolist()
        return out

    # ------------------------------------------------------------ the nearest point of the line
    def _locate(self, q: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """``s`` and ``d`` of plan points (offsets from the first vertex): the nearest point of the line between
        its ends - the first primitive wins a tie - and the offset from it along its normal; when that point is
        an end of the line, ``s`` goes on along the tangent there."""
        s, d = np.empty(len(q)), np.empty(len(q))
        last = len(self._s0) - 1
        block = max(1, _BLOCK // len(self._s0))
        for i in range(0, len(q), block):
            part = q[i:i + block]
            k = self._nearest(part)
            tau = self._foot(part, k)
            # a point held at the end of its primitive goes to the neighbour when its foot falls there: close to
            # the joint the two distances differ by less than float64 resolves, and the tie went to the first
            for _ in range(last):
                step = (tau > self._len[k]).astype(np.intp) - (tau < 0.0).astype(np.intp)
                step[(k + step < 0) | (k + step > last)] = 0
                move = np.nonzero(step)[0]
                to = k[move] + step[move]
                other = self._foot(part[move], to)
                taken = np.where(step[move] > 0, other > 0.0, other < self._len[to])
                if not taken.any():
                    break
                k[move[taken]], tau[move[taken]] = to[taken], other[taken]
            tau = np.where(k > 0, np.maximum(tau, 0.0), tau)  # only the ends of the whole line are prolonged
            tau = np.where(k < last, np.minimum(tau, self._len[k]), tau)
            p, n = self._place(k, tau)
            s[i:i + block] = self._s0[k] + tau
            d[i:i + block] = ((part - p) * n).sum(axis=1)
        return s, d

    def _nearest(self, q: np.ndarray) -> np.ndarray:
        """The primitive nearest to each point. The ends of a primitive are taken as stored, the same numbers
        for the two primitives that meet there: a tie is a tie, and the first wins it."""
        d2 = np.empty((len(q), len(self._s0)))
        x, y = q[:, 0, None], q[:, 1, None]
        i = self._iseg
        rx, ry = x - self._a[i, 0], y - self._a[i, 1]
        tau = np.clip(rx * self._t[i, 0] + ry * self._t[i, 1], 0.0, self._len[i])
        past = tau >= self._len[i]
        dx = np.where(past, x - self._b[i, 0], rx - tau * self._t[i, 0])
        dy = np.where(past, y - self._b[i, 1], ry - tau * self._t[i, 1])
        d2[:, i] = dx * dx + dy * dy
        i = self._iarc
        if len(i):
            wx, wy = x - self._c[i, 0], y - self._c[i, 1]
            r = np.hypot(wx, wy)
            facing = wx * self._m[i, 0] + wy * self._m[i, 1] >= r * self._cos_half[i]  # within the arc's turn
            ax, ay, bx, by = x - self._a[i, 0], y - self._a[i, 1], x - self._b[i, 0], y - self._b[i, 1]
            d2[:, i] = np.where(facing, (r - self._rho[i]) ** 2, np.minimum(ax * ax + ay * ay, bx * bx + by * by))
        return d2.argmin(axis=1)

    def _foot(self, q: np.ndarray, k: np.ndarray) -> np.ndarray:
        """Where the foot of each point falls on its primitive ``k``, in metres from the start of the primitive:
        below 0 before its start, over its length past its end."""
        tau = np.empty(len(q))
        seg = self._seg[k]
        i = k[seg]
        tau[seg] = (q[seg, 0] - self._a[i, 0]) * self._t[i, 0] + (q[seg, 1] - self._a[i, 1]) * self._t[i, 1]
        i = k[~seg]
        wx, wy = q[~seg, 0] - self._c[i, 0], q[~seg, 1] - self._c[i, 1]
        mx, my = self._m[i, 0], self._m[i, 1]
        turn = self._sign[i] * np.arctan2(mx * wy - my * wx, mx * wx + my * wy)  # from the middle of the arc
        tau[~seg] = self._rho[i] * (turn + self._half[i])
        return tau

    def _owns(self, s: np.ndarray, depth: np.ndarray) -> np.ndarray:
        """Whether the point ``depth`` metres along the normal at ``s`` is still of ``s``: the line has no nearer
        point. True at a depth is true at every smaller one (the circle through the point of the line, centred
        on the nearer point of the normal, lies inside the other)."""
        if len(self._s0) == 1:  # one straight run: a plane
            return np.isfinite(s) & np.isfinite(depth)
        p, n = self._at(s)
        return np.abs(self._locate(p + depth[:, None] * n)[0] - s) <= _SAME_PLACE

    def _reach_at(self, u, side: float) -> np.ndarray:
        """How far along the normal (``side`` 1: towards the viewer, -1: behind) the points belong to each ``u``:
        the largest depth that maps back to it, by bisection between 0 and :data:`_REACH_LIMIT` (infinite when
        it holds there), on the different values of ``u``. Nothing where the place itself is of another part of
        the line: past an end that comes back to the line, as the end of a closed one does."""
        u = np.asarray(u, np.float64)
        values, inverse = np.unique(u.ravel(), return_inverse=True)
        reach = np.full(len(values), np.nan)
        known = np.isfinite(values)
        s = self.s_of(values[known])
        lo, hi = np.zeros(len(s)), np.zeros(len(s))
        here = np.nonzero(self._owns(s, np.zeros(len(s))))[0]
        far = self._owns(s[here], np.full(len(here), side * _REACH_LIMIT))
        hi[here] = np.where(far, np.inf, _REACH_LIMIT)
        rest = here[~far]
        if len(rest):
            for _ in range(int(np.ceil(np.log2(_REACH_LIMIT / _REACH_STEP)))):
                mid = (lo[rest] + hi[rest]) / 2.0
                held = self._owns(s[rest], side * mid)
                lo[rest], hi[rest] = np.where(held, mid, lo[rest]), np.where(held, hi[rest], mid)
        reach[known] = np.where(np.isinf(hi), np.inf, lo)
        return reach[np.ravel(inverse)].reshape(u.shape)


def corner_radii(vertices, front: float = DEFAULT_FRONT, back: float = DEFAULT_FRONT) -> list[float]:
    """The radius to ask for the arc of each interior vertex of a trace (``radii`` of :class:`WallPath`), given
    the depth kept in ``front`` of the face and at its ``back``: ``front`` + :data:`CLEARANCE`, so that a
    re-entrant corner leaves the depth in front whole; less where the turn is sharp. The corner as traced stands
    ``radius * (1 / cos(turn / 2) - 1)`` off its arc - in front of it when convex, behind it when re-entrant -
    and must stay within the depth kept on that side, of which it may take :data:`CORNER_SHARE`."""
    v = _plan_points(vertices)
    out = []
    for turn in _turns(v - v[0])[2]:
        off = 1.0 / np.cos(turn / 2.0) - 1.0  # of the vertex from the arc, per metre of radius
        kept = front if turn > 0 else back
        out.append(float(min(front + CLEARANCE, CORNER_SHARE * kept / off)) if off > 0 else float(front + CLEARANCE))
    return out


def ordered(points, side: str) -> np.ndarray:
    """The points of a clicked line in the order a :class:`WallPath` takes them, the viewer on the ``side`` of
    the line as it was clicked: as given for ``"right"``, reversed for ``"left"``."""
    p = _xy(points)
    if side == "right":
        return p
    if side == "left":
        return p[::-1].copy()
    raise TraceRefused("side", f"the side of the viewer is 'left' or 'right' of the line, not {side!r}")


def smooth_trace(points, sigma: float) -> np.ndarray:
    """The clicked points without the wobble of the hand: the polyline resampled every ``sigma / 2`` metres,
    each point the Gaussian mean (``sigma`` metres along the line) of the line around it; the two ends stay
    where they were clicked. ``sigma`` 0: the points as clicked (what the owner clicked is the line)."""
    p = _xy(points)
    if not sigma > 0 or len(p) < 3:
        return p
    run = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(p, axis=0).T))])
    run, first = np.unique(run, return_index=True)  # a point clicked twice counts once
    if len(run) < 3:
        return p
    line = p[first] - p[0]
    steps = max(int(np.ceil(run[-1] / (sigma / 2.0))), 2)
    # the line cut in pieces a quarter of a step long, each taken at its mean (the integral of the polyline, not
    # a sample of it: a wobble shorter than a step leaves nothing behind)
    pieces = 4 * steps
    width = run[-1] / pieces
    edge = np.linspace(0.0, run[-1], pieces + 1)
    i = np.clip(np.searchsorted(run, edge, side="right") - 1, 0, len(run) - 2)
    tau = (edge - run[i])[:, None]
    slope = (line[i + 1] - line[i]) / (run[i + 1] - run[i])[:, None]
    area = np.concatenate([np.zeros((1, 2)), np.cumsum((line[1:] + line[:-1]) / 2.0 * np.diff(run)[:, None], axis=0)])
    mean = np.diff(area[i] + line[i] * tau + slope * tau ** 2 / 2.0, axis=0) / width
    # past each end the line goes on as its own image through the end: the mean at an end is the end
    reach = min(int(np.ceil(4.0 * sigma / width)), pieces)
    mean = np.concatenate([2.0 * line[0] - mean[reach - 1::-1], mean, 2.0 * line[-1] - mean[:-reach - 1:-1]])
    weight = np.exp(-0.5 * ((np.arange(-reach, reach) + 0.5) * width / sigma) ** 2)
    weight /= weight.sum()
    out = p[0] + np.column_stack([np.convolve(mean[:, k], weight, mode="valid")[::4] for k in (0, 1)])
    out[0], out[-1] = p[0], p[-1]
    return out
