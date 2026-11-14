"""Pottery drawing from 3D models: rotation axis, profile, measurements, drawing.

Works on whole vessels and on sherds. The rotation axis is estimated from the
surface normals (on a surface of revolution every normal line meets the axis)
and, on whole vessels and large sherds, from the centres of the sections across
it, or given explicitly (e.g. the turntable axis when the scan was referenced with
a turntable mat: ``axis=((0, 0, 0), (0, 0, 1))``).

The drawing follows the usual convention: axis in the middle, section (profile,
filled) on the left, exterior view outline on the right, rim and base lines.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np


@dataclass
class Axis:
    point: np.ndarray
    direction: np.ndarray
    residual: float = 0.0  # RMS distance (m) between normal lines and the axis

    def __post_init__(self):
        self.point = np.asarray(self.point, float)
        d = np.asarray(self.direction, float)
        self.direction = d / np.linalg.norm(d)


@dataclass
class Profile:
    axis: Axis
    curves: list[np.ndarray] = field(default_factory=list)  # (N,2): radius r >= 0, height h (base = 0)
    angle: float = 0.0

    @property
    def points(self) -> np.ndarray:
        return np.vstack(self.curves) if self.curves else np.zeros((0, 2))

    def measurements(self) -> dict:
        p = self.points
        if len(p) == 0:
            return {}
        h = p[:, 1]
        top = p[h >= h.max() - 0.003]
        bottom = p[h <= h.min() + 0.003]
        out = {
            "height_m": float(np.ptp(h)),
            "max_diameter_m": float(2 * p[:, 0].max()),
            "rim_diameter_m": float(2 * top[:, 0].max()),
            "base_diameter_m": float(2 * bottom[:, 0].max()),
        }
        t = wall_thickness(self)
        if t is not None:
            out["wall_thickness_m"] = t
        return out


def _dir(params):
    theta, phi = params[0], params[1]
    return np.array([np.sin(theta) * np.cos(phi), np.sin(theta) * np.sin(phi), np.cos(theta)])


def _normal_line_distances(params, centers, normals):
    theta, phi, a, b = params
    d = np.array([np.sin(theta) * np.cos(phi), np.sin(theta) * np.sin(phi), np.cos(theta)])
    u = np.cross(d, [1.0, 0, 0]) if abs(d[0]) < 0.9 else np.cross(d, [0, 1.0, 0])
    u /= np.linalg.norm(u)
    v = np.cross(d, u)
    p = a * u + b * v
    c = np.cross(normals, d)
    nc = np.linalg.norm(c, axis=1)
    return ((centers - p) * c).sum(1) / np.maximum(nc, 1e-9)


def _uv(d):
    u = np.cross(d, [1.0, 0, 0]) if abs(d[0]) < 0.9 else np.cross(d, [0, 1.0, 0])
    u /= np.linalg.norm(u)
    return u, np.cross(d, u)


def _axis_point_for(d, centers, normals, min_angle: float = 0.25, starts: int = 40):
    """Best axis position for a fixed direction ``d`` (robust linear fit).

    Each normal line must cross the axis: the signed distance between the two lines is linear in
    the axis position, so it is solved directly. A handle or the fracture faces of a sherd give
    lines that miss the axis: the start is the position where the most lines pass closest (the
    crossing of two lines, of ``starts`` pairs, with the smallest median distance), then robust
    least squares (Huber, then Tukey's weights, which give the far lines no weight at all)."""
    m = np.cross(normals, d)
    nm = np.linalg.norm(m, axis=1)
    ok = nm > min_angle  # normals almost parallel to the axis carry no information
    if ok.mean() < 0.4:
        return None, np.inf
    mh = m[ok] / nm[ok, None]
    u, v = _uv(d)
    A = np.column_stack([mh @ u, mh @ v])
    y = (centers[ok] * mh).sum(1)
    sol, *_ = np.linalg.lstsq(A, y, rcond=None)
    best = float(np.median(np.abs(y - A @ sol)))
    if starts and len(y) > 20:
        rng = np.random.default_rng(len(y))
        sub = rng.choice(len(y), min(400, len(y)), replace=False)
        for i, j in rng.choice(len(y), (starts, 2)):
            M = A[[i, j]]
            if abs(np.linalg.det(M)) < 0.2:  # nearly parallel lines: no crossing
                continue
            cand = np.linalg.solve(M, y[[i, j]])
            med = float(np.median(np.abs(y[sub] - A[sub] @ cand)))
            if med < best:
                best, sol = med, cand
    for it in range(8):
        res = y - A @ sol
        scale = 1.4826 * np.median(np.abs(res)) + 1e-12
        if it < 3:
            w = 1.0 / np.maximum(1.0, np.abs(res) / (2.0 * scale))  # Huber weights
        else:
            w = np.clip(1.0 - (res / (4.685 * scale)) ** 2, 0.0, None) ** 2  # Tukey's biweight
        if w.sum() < 10:
            break
        sw = np.sqrt(w)
        sol, *_ = np.linalg.lstsq(A * sw[:, None], y * sw, rcond=None)
    res = y - A @ sol
    return sol, float(np.median(np.abs(res)))


def _smooth_normals(mesh, points, face_normals, k: int = 48):
    """Normals from a local plane fit (k nearest vertices): photogrammetric
    meshes are noisy at the triangle scale, the axis needs the shape."""
    from scipy.spatial import cKDTree

    verts = np.asarray(mesh.vertices)
    vn = np.asarray(mesh.vertex_normals)
    k = min(k, len(verts))
    _, nn = cKDTree(verts).query(points, k=k)
    # same-side neighbours only: the opposite wall of a thin sherd is a few mm away
    w = ((vn[nn] * face_normals[:, None, :]).sum(2) > 0.5).astype(float)
    cnt = w.sum(1, keepdims=True)
    mean = (verts[nn] * w[..., None]).sum(1) / np.maximum(cnt, 1)
    nb = (verts[nn] - mean[:, None, :]) * w[..., None]
    cov = np.einsum("nki,nkj->nij", nb, nb)
    _, vecs = np.linalg.eigh(cov)
    n = vecs[:, :, 0]
    n *= np.where((n * face_normals).sum(1) < 0, -1.0, 1.0)[:, None]
    bad = (cnt[:, 0] < 8) | ((n * face_normals).sum(1) < 0.5)
    n[bad] = face_normals[bad]
    return n


def _fibonacci_hemisphere(n: int) -> np.ndarray:
    i = np.arange(n) + 0.5
    z = i / n
    r = np.sqrt(1 - z ** 2)
    phi = np.pi * (1 + 5 ** 0.5) * i
    return np.column_stack([r * np.cos(phi), r * np.sin(phi), z])


def _normal_candidates(centers, normals, size, directions: int, keep: int = 8, apart: float = 10.0):
    """Axis directions where the normal lines meet a line best: the best of all ``directions`` and
    the next best ones at least ``apart`` degrees from those kept (a handle, or a sherd almost a
    piece of a sphere, has more than one axis that fits its normals)."""
    cand = []
    for d in _fibonacci_hemisphere(directions):
        ab, cost = _axis_point_for(d, centers, normals, starts=8)
        if ab is not None and np.hypot(*ab) < 20 * size:
            cand.append((cost, d, ab))
    cand.sort(key=lambda c: c[0])
    out, cos_apart = [], np.cos(np.radians(apart))
    for c in cand:
        if all(abs(c[1] @ o[1]) < cos_apart for o in out):
            out.append(c)
            if len(out) == keep:
                break
    return out


def _refine_normals(d0, ab0, centers, normals):
    """Robust least squares of the normal lines from a candidate axis (fracture faces drop out)."""
    from scipy.optimize import least_squares

    theta = np.arccos(np.clip(d0[2], -1, 1))
    phi = np.arctan2(d0[1], d0[0])
    u0, v0 = _uv(d0)
    x = np.array([theta, phi, 0.0, 0.0])
    # the position of _normal_line_distances is in the frame of the refined direction: start from that of d0
    pt = ab0[0] * u0 + ab0[1] * v0
    uu, vv = _uv(_dir(x))
    x[2], x[3] = pt @ uu, pt @ vv
    for _ in range(3):  # refine on the inliers of the current estimate
        r0 = _normal_line_distances(x, centers, normals)
        m = np.linalg.norm(np.cross(normals, _dir(x)), axis=1) > 0.25
        sigma = 1.4826 * np.median(np.abs(r0[m])) + 1e-9
        m &= np.abs(r0) < 4 * sigma
        if m.sum() < 20:
            break
        try:
            x = least_squares(_normal_line_distances, x, args=(centers[m], normals[m]), loss="cauchy",
                              f_scale=sigma).x
        except Exception:
            break
    d = _dir(x)
    ab, cost = _axis_point_for(d, centers, normals)
    return (d, ab, cost) if ab is not None else None


def _robust_circle(xy: np.ndarray):
    """Circle through the points of a section (both faces of the wall: concentric, the centre is
    the same), points far from it (a handle, a fracture face) left out. Returns centre, radius,
    the arc it covers (degrees) and the spread of the points about it, or None."""
    from scipy.optimize import least_squares

    fit = _fit_circle(xy)
    if fit is None:
        return None
    cx, cy, r = fit
    keep = np.ones(len(xy), bool)
    spread = 0.0
    for _ in range(5):
        res = np.hypot(xy[:, 0] - cx, xy[:, 1] - cy) - r
        spread = 1.4826 * float(np.median(np.abs(res - np.median(res)))) + 1e-6
        keep = np.abs(res - np.median(res)) < 3 * spread + 0.002 * max(r, 0.01) / 0.1
        if keep.sum() < 12:
            return None
        k = xy[keep]
        try:
            cx, cy, r = least_squares(lambda c: np.hypot(k[:, 0] - c[0], k[:, 1] - c[1]) - c[2], [cx, cy, r],
                                      loss="soft_l1", f_scale=spread + 1e-4).x
        except Exception:
            return None
    ang = np.sort(np.degrees(np.arctan2(xy[keep, 1] - cy, xy[keep, 0] - cx)) % 360)
    arc = 360.0 - float(np.diff(np.r_[ang, ang[0] + 360]).max())
    return float(cx), float(cy), abs(float(r)), arc, spread


def _refine_sections(mesh, axis: Axis, slices: int = 20, iterations: int = 12, min_arc: float = 150.0):
    """The axis through the centres of the circles of the sections across it (photogrammetric
    meshes: their positions are steadier than their normals). Sections are cut across the current
    axis, their circles fitted, a line fitted through the centres (centres off it left out), and so
    again. None when fewer than four sections cover ``min_arc`` degrees (a sherd: the circle of a
    short arc is loose, the normals are better there)."""
    import trimesh

    for _ in range(iterations):
        d = axis.direction
        u, v = _uv(d)
        h = (np.asarray(mesh.vertices) - axis.point) @ d
        lo, hi = np.percentile(h, 4), np.percentile(h, 96)
        cs, ws = [], []
        for hh in np.linspace(lo, hi, slices):
            o = axis.point + hh * d
            segs = trimesh.intersections.mesh_plane(mesh, d, o)
            if len(segs) < 8:
                continue
            rel = segs.reshape(-1, 3) - o
            c = _robust_circle(np.column_stack([rel @ u, rel @ v]))
            if c is None or c[3] < min_arc or c[2] > 10 * (hi - lo + 0.05):
                continue
            cs.append(o + c[0] * u + c[1] * v)
            ws.append(c[3] / 360.0)
        if len(cs) < 4:
            return None
        cs, ws = np.array(cs), np.array(ws)
        # least median: the line through two centres that most centres are near (the sections
        # through a badly meshed foot or a handle wander off it), then least squares on those
        best = None
        for i in range(len(cs)):
            for j in range(i + 1, len(cs)):
                e = cs[j] - cs[i]
                if np.linalg.norm(e) < 1e-9:
                    continue
                e = e / np.linalg.norm(e)
                med = float(np.median(np.linalg.norm(np.cross(cs - cs[i], e), axis=1)))
                if best is None or med < best[0]:
                    best = (med, cs[i], e)
        off = np.linalg.norm(np.cross(cs - best[1], best[2]), axis=1)
        keep = off < 3 * 1.4826 * best[0] + 1e-4
        if keep.sum() < 4:
            return None
        m = (cs[keep] * ws[keep, None]).sum(0) / ws[keep].sum()
        _, _, vt = np.linalg.svd((cs[keep] - m) * np.sqrt(ws[keep])[:, None])
        nd = vt[0] * np.sign(vt[0] @ d)
        # a section across a tilted axis of a flaring wall has its centre a little off the true axis,
        # towards the tilt: each round takes off part of it, until the direction stops moving
        moved = float(np.degrees(np.arccos(np.clip(nd @ d, -1.0, 1.0))))
        axis = Axis(m, nd)
        if moved < 0.005:
            break
    return axis


def _spread(points: np.ndarray, axis: Axis, cell: float) -> int:
    """How much room the points take in the (radius, height) half plane of ``axis``: the cells of
    ``cell`` metres they fall in. Around the true axis all the turns of the surface fall on the
    same thin profile; around any other they are smeared."""
    rel = points - axis.point
    h = rel @ axis.direction
    r = np.linalg.norm(rel - h[:, None] * axis.direction, axis=1)
    keys = np.floor(r / cell).astype(np.int64) * 1_000_003 + np.floor(h / cell).astype(np.int64)
    return int(len(np.unique(keys)))


def estimate_axis(mesh, samples: int = 4000, seed: int = 0, directions: int = 1500, sections: bool = True) -> Axis:
    """Rotation axis of a (possibly fragmentary) vessel.

    On a surface of revolution every normal line meets the axis: a search over all directions
    (axis position solved linearly for each) gives the candidates - the best one and the next best
    ones in other directions, so that a handle or a sherd shaped almost like a piece of a sphere
    does not hide the axis of the body - each refined by robust least squares of the normal lines.
    With ``sections`` each candidate is refined also through the centres of the circles of the
    sections across it, where they cover a quarter of a turn or more (whole vessels, large sherds:
    the normals of a mesh made from photos are noisy, its positions are not). The axis kept is the
    one around which the surface is thinnest in the profile (:func:`_spread`): the turns of a
    surface of revolution fall on one profile only around its axis. ``residual`` is the median
    distance of the normal lines from the axis (metres)."""
    import trimesh

    rng = np.random.default_rng(seed)
    area = mesh.area_faces
    idx = rng.choice(len(mesh.faces), min(samples, len(mesh.faces)), replace=False, p=area / area.sum())
    offset = mesh.triangles_center[idx].mean(0)
    centers = mesh.triangles_center[idx] - offset
    normals = _smooth_normals(mesh, mesh.triangles_center[idx], mesh.face_normals[idx])
    size = float(np.ptp(centers, axis=0).max())
    found = []
    for _, d0, ab0 in _normal_candidates(centers, normals, size, directions):
        r = _refine_normals(d0, ab0, centers, normals)
        if r is None:
            continue
        d, (a, b), cost = r
        u, v = _uv(d)
        found.append(Axis(offset + a * u + b * v, d, float(cost)))
    if not found:
        raise ValueError("cannot estimate the rotation axis (surface too flat or too small)")
    dense, _ = trimesh.sample.sample_surface(mesh, 20000, seed=seed)
    cell = max(size / 250.0, 1e-6)
    scored = sorted(found, key=lambda ax: _spread(dense, ax, cell))
    spread = {id(ax): _spread(dense, ax, cell) for ax in found}
    best = min(found, key=lambda ax: spread[id(ax)])
    if sections:
        through = []
        for ax in scored[:3]:  # the sections are slower: the most likely candidates only
            refined = _refine_sections(mesh, ax)
            if refined is not None:
                _, cost = _axis_point_for(refined.direction, centers, normals)
                refined.residual = float(cost) if np.isfinite(cost) else ax.residual
                through.append((_spread(dense, refined, cell), refined))
        # the centres of the sections give the direction to a tenth of a degree where the normals of
        # a mesh from photos give half a degree: kept unless the profile around it is clearly thicker
        ok = [(sp, ax) for sp, ax in through if sp <= 1.03 * spread[id(best)]]
        if ok:
            best = min(ok, key=lambda c: c[0])[1]
    point, d = best.point, best.direction
    # orient the axis from base to rim: the end with a floor across the axis is the base (a jar is
    # narrower at the mouth than at the foot); without one the mouth is usually the wider end
    h = (mesh.vertices - point) @ d
    r = np.linalg.norm(np.cross(mesh.vertices - point, d), axis=1)
    floor = r < 0.25 * r.max()
    mid = 0.5 * (h.min() + h.max())
    if floor.mean() > 0.01 and abs(np.median(h[floor]) - mid) > 0.25 * np.ptp(h):
        if np.median(h[floor]) > mid:
            d = -d
    else:
        lo, hi = h < np.percentile(h, 15), h > np.percentile(h, 85)
        if r[lo].mean() > r[hi].mean():
            d = -d
    return Axis(point, d, best.residual)


def _frame(axis: Axis, angle: float):
    d = axis.direction
    u = np.cross(d, [1.0, 0, 0]) if abs(d[0]) < 0.9 else np.cross(d, [0, 1.0, 0])
    u /= np.linalg.norm(u)
    v = np.cross(d, u)
    a = np.radians(angle)
    radial = np.cos(a) * u + np.sin(a) * v
    return d, radial, np.cross(d, radial)


def profile_at(mesh, axis: Axis, angle: float = 0.0) -> Profile:
    """Cut the mesh with the half-plane containing the axis at ``angle`` (degrees)."""
    import trimesh

    from .sections import _join_segments

    d, radial, normal = _frame(axis, angle)
    segs = trimesh.intersections.mesh_plane(mesh, normal, axis.point)
    prof = Profile(axis, angle=angle)
    if len(segs) == 0:
        return prof
    rel = segs - axis.point
    r = rel @ radial
    h = rel @ d
    keep = (r > 0).all(1)
    seg2d = np.stack([r, h], -1)[keep]
    if len(seg2d) == 0:
        return prof
    base = (mesh.vertices - axis.point) @ d
    seg2d[..., 1] -= base.min()
    prof.curves = _join_segments(seg2d, tol=max(np.ptp(seg2d), 1e-3) * 1e-7)
    return prof


def best_profile(mesh, axis: Axis | None = None, step: float = 5.0) -> Profile:
    """Profile through the angle where the sherd gives the longest section line."""
    axis = axis or estimate_axis(mesh)
    best = None
    for ang in np.arange(0, 360, step):
        p = profile_at(mesh, axis, ang)
        length = sum(np.linalg.norm(np.diff(c, axis=0), axis=1).sum() for c in p.curves)
        if best is None or length > best[0]:
            best = (length, p)
    return best[1]


def rim_diameter_around(mesh, axis: Axis, step: float = 5.0, band: float = 0.003) -> float | None:
    """Rim diameter around ``axis`` from the sections all around it: the middle value (median) of
    the diameters at the top of the sections that reach the rim (their top within a tenth of the
    tallest section of the highest one, so the sections across a fracture do not count).

    One section alone reads the lip where it happens to cut it: on a mesh made from photos the
    lip wanders by millimetres around the circle and the estimated axis leans by a few degrees,
    so the single section of the drawing can be half a centimetre off; the median is not."""
    tops, diameters, heights = [], [], []
    for ang in np.arange(0.0, 360.0, step):
        p = profile_at(mesh, axis, float(ang)).points
        if len(p) < 2:
            continue
        h = p[:, 1]
        tops.append(float(h.max()))
        heights.append(float(np.ptp(h)))
        diameters.append(2.0 * float(p[h >= h.max() - band][:, 0].max()))
    if not tops:
        return None
    tops, diameters = np.array(tops), np.array(diameters)
    reach = tops >= tops.max() - 0.1 * max(heights)
    return float(np.median(diameters[reach]))


def _section_polygon(profile: Profile) -> np.ndarray:
    """Closed section outline in (r, h): outer surface up, inner surface down."""
    curves = sorted([c for c in profile.curves if len(c) > 1], key=len, reverse=True)
    if len(curves) >= 2:
        a, b = curves[0], curves[1]
        outer, inner = (a, b) if a[:, 0].mean() >= b[:, 0].mean() else (b, a)
        if outer[0, 1] > outer[-1, 1]:
            outer = outer[::-1]
        if inner[0, 1] < inner[-1, 1]:
            inner = inner[::-1]
        return np.vstack([outer, inner])
    return curves[0]


def outer_envelope(profile: Profile, step: float = 0.0005) -> np.ndarray:
    """Exterior silhouette: maximum radius per height slice, from base to rim."""
    from scipy.ndimage import maximum_filter1d

    p = profile.points
    bins = np.floor((p[:, 1] - p[:, 1].min()) / step).astype(int)
    rmax = np.full(bins.max() + 1, -np.inf)
    np.maximum.at(rmax, bins, p[:, 0])
    ok = np.isfinite(rmax)
    centers = p[:, 1].min() + (np.arange(len(rmax)) + 0.5) * step
    r = np.interp(centers, centers[ok], rmax[ok])
    r = maximum_filter1d(r, size=9, mode="nearest")  # ignore slices where only the inner face was cut
    return np.column_stack([r, centers])


def wall_thickness(profile: Profile) -> float | None:
    """Median distance from each point of the section to the opposite wall."""
    from scipy.spatial import cKDTree

    poly = _section_polygon(profile) if profile.curves else np.zeros((0, 2))
    n = len(poly)
    if n < 20:
        return None
    seg = np.linalg.norm(np.diff(poly, axis=0), axis=1)
    arclen = np.r_[0, np.cumsum(seg)]
    total = arclen[-1] + np.linalg.norm(poly[0] - poly[-1])
    tree = cKDTree(poly)
    k = min(64, n)
    dist, idx = tree.query(poly, k=k)
    th = []
    for i in range(n):
        # nearest point that is far along the outline = the other face of the wall
        along = np.abs(arclen[idx[i]] - arclen[i])
        along = np.minimum(along, total - along)
        far = along > 3 * dist[i, 1:].max(initial=0) + 1e-9
        cand = dist[i][far]
        if len(cand):
            th.append(cand[0])
    return float(np.median(th)) if th else None


def _fit_circle(xy: np.ndarray) -> tuple[float, float, float] | None:
    """Least-squares circle through 2D points (Kasa): centre and radius, None when degenerate."""
    x, y = xy[:, 0], xy[:, 1]
    a = np.column_stack([x, y, np.ones(len(x))])
    try:
        sol, *_ = np.linalg.lstsq(a, x * x + y * y, rcond=None)
    except np.linalg.LinAlgError:
        return None
    cx, cy = sol[0] / 2, sol[1] / 2
    r2 = sol[2] + cx * cx + cy * cy
    return (float(cx), float(cy), float(np.sqrt(r2))) if r2 > 0 else None


def _outer_circle(xy: np.ndarray, start: np.ndarray | None = None,
                  iterations: int = 4) -> tuple[float, float, float] | None:
    """The circle of the outer edge of a thick arc (the outer lip of a rim): the farthest point
    from the centre in every degree, fitted by geometric least squares, a few times, from
    ``start`` (else an algebraic fit, which on the whole band of a short arc comes out far too
    small)."""
    from scipy.optimize import least_squares

    if start is not None:
        cx, cy = float(start[0]), float(start[1])
        r = float(np.percentile(np.hypot(xy[:, 0] - cx, xy[:, 1] - cy), 95))
    else:
        fit = _fit_circle(xy)
        if fit is None:
            return None
        cx, cy, r = fit
    for _ in range(iterations):
        dist = np.hypot(xy[:, 0] - cx, xy[:, 1] - cy)
        deg = np.floor(np.degrees(np.arctan2(xy[:, 1] - cy, xy[:, 0] - cx))).astype(int) % 360
        order = np.lexsort((-dist, deg))
        first = np.r_[True, deg[order][1:] != deg[order][:-1]]
        pts = xy[order[first]]
        if len(pts) < 5:
            break
        res = least_squares(lambda c: np.hypot(pts[:, 0] - c[0], pts[:, 1] - c[1]) - c[2], [cx, cy, r])
        cx, cy, r = (float(v) for v in res.x)
    return cx, cy, abs(r)


def _edge_rms(xy: np.ndarray, centre, radius: float) -> float:
    """RMS distance of the outer edge (farthest point in every degree) from the circle."""
    dist = np.hypot(xy[:, 0] - centre[0], xy[:, 1] - centre[1])
    # floor first: a hair below 0 degrees modulo 360 is 360.0, outside the 360 bins
    deg = np.floor(np.degrees(np.arctan2(xy[:, 1] - centre[1], xy[:, 0] - centre[0]))).astype(int) % 360
    edge = np.full(360, -np.inf)
    np.maximum.at(edge, deg, dist)
    edge = edge[np.isfinite(edge)]
    return float(np.sqrt(np.mean((edge - radius) ** 2))) if len(edge) else float("nan")


def _span(xy: np.ndarray) -> float:
    """The largest distance between two of the points (the chord of the arc of a rim)."""
    from scipy.spatial import ConvexHull
    from scipy.spatial.distance import pdist

    if len(xy) < 2:
        return 0.0
    try:
        xy = xy[ConvexHull(xy).vertices]
    except Exception:  # all on a line
        pass
    return float(pdist(xy).max())


def _centre_from_normals(xy: np.ndarray, nxy: np.ndarray) -> np.ndarray | None:
    """The point nearest to all the lines through ``xy`` along ``nxy`` (least squares): on a
    surface of revolution the normals meet the axis, so across the axis they meet at its centre,
    even on a short arc."""
    length = np.linalg.norm(nxy, axis=1)
    keep = length > 0.5  # normals nearly along the axis (the top of the lip) say nothing here
    if keep.sum() < 3:
        return None
    n = nxy[keep] / length[keep, None]
    p = xy[keep]
    proj = np.eye(2)[None] - n[:, :, None] * n[:, None, :]  # (I - n n^T) for every line
    weight = np.ones(len(p))
    centre = None
    for _ in range(12):  # reweighted: the lines of the fracture faces (tangential normals) miss the centre
        a = (proj * weight[:, None, None]).sum(axis=0)
        b = ((proj @ p[:, :, None])[:, :, 0] * weight[:, None]).sum(axis=0)
        if np.linalg.cond(a) > 1e8:  # parallel lines: a straight edge
            return None
        centre = np.linalg.solve(a, b)
        off = centre - p
        dist = np.abs(off[:, 0] * n[:, 1] - off[:, 1] * n[:, 0])  # from the centre to every line
        scale = 1.5 * float(np.median(dist)) + 1e-9
        weight = 1.0 / (1.0 + (dist / scale) ** 2)
    return centre


def _angles(xy: np.ndarray, centre) -> np.ndarray:
    return np.degrees(np.arctan2(xy[:, 1] - centre[1], xy[:, 0] - centre[0])) % 360.0


def _bin_width(xy: np.ndarray, centre, mesh) -> float:
    """Angular sectors for the lip (degrees): two degrees, wider when the mesh is coarser there
    (one and a half edges of the mesh across), so that every sector of a whole lip has a vertex."""
    edge = float(np.median(np.asarray(mesh.edges_unique_length))) if len(mesh.edges_unique) else 0.0
    radius = float(np.median(np.hypot(xy[:, 0] - centre[0], xy[:, 1] - centre[1]))) if len(xy) else 0.0
    if radius <= 0:
        return 2.0
    return float(min(30.0, max(2.0, np.degrees(1.5 * edge / radius))))


def _lip(xy: np.ndarray, h: np.ndarray, centre, width: float, band: float):
    """The lip of a rim: the highest point of every sector of ``width`` degrees around ``centre``,
    and the plane of the lip through them (``h = a + b x + c y``, robust: a lip a little tilted
    to the axis, a lip that waves up and down, a chip or a spike of the mesh). Returns the plane,
    the sectors whose highest point is on the lip (within the tolerance) and the tolerance."""
    nbin = max(1, int(round(360.0 / width)))
    sector = np.minimum((_angles(xy, centre) / (360.0 / nbin)).astype(int), nbin - 1)
    order = np.lexsort((-h, sector))
    first = np.r_[True, sector[order][1:] != sector[order][:-1]]
    top = order[first]  # the highest vertex of every sector with vertices
    ts, tx, th = sector[top], xy[top], h[top]
    occupied = np.zeros(nbin, bool)
    occupied[ts] = True
    level = float(np.percentile(th, 75))
    inl = th >= level - band
    coef = np.array([level, 0.0, 0.0])
    tol = band
    span = 360.0 * occupied.mean()
    for _ in range(8):
        if inl.sum() >= 6 and span >= 60.0:  # a plane needs the lip seen over a sixth of a turn
            A = np.column_stack([np.ones(inl.sum()), tx[inl, 0] - centre[0], tx[inl, 1] - centre[1]])
            coef, *_ = np.linalg.lstsq(A, th[inl], rcond=None)
            slope = np.hypot(coef[1], coef[2])
            if slope > np.tan(np.radians(10.0)):  # not a lip: keep it level
                coef = np.array([float(np.median(th[inl])), 0.0, 0.0])
        elif inl.any():
            coef = np.array([float(np.median(th[inl])), 0.0, 0.0])
        res = th - (coef[0] + coef[1] * (tx[:, 0] - centre[0]) + coef[2] * (tx[:, 1] - centre[1]))
        mad = 1.4826 * float(np.median(np.abs(res[inl]))) if inl.any() else 0.0
        tol = band + 3.0 * mad
        new = np.abs(res) <= tol
        if (new == inl).all():
            break
        inl = new
    on_lip = np.zeros(nbin, bool)
    on_lip[ts[inl]] = True
    return coef, on_lip, tol


def _coverage(xy: np.ndarray, centre, rim_mask: np.ndarray, width: float, on_lip: np.ndarray) -> float:
    """Degrees of the rim circle kept: the runs of sectors with lip (a single empty sector between
    two with lip is closed: a coarse mesh), each counted from its first rim point to its last."""
    nbin = len(on_lip)
    if not on_lip.any():
        return 0.0
    filled = on_lip | (np.roll(on_lip, 1) & np.roll(on_lip, -1))
    if filled.all():
        return 360.0
    ang = _angles(xy[rim_mask], centre)
    sector = np.minimum((ang / (360.0 / nbin)).astype(int), nbin - 1)
    total = 0.0
    start = int(np.argmin(filled))  # an empty sector: the runs do not wrap around past it
    i = 0
    while i < nbin:
        k = (start + i) % nbin
        if not filled[k]:
            i += 1
            continue
        run = []
        while i < nbin and filled[(start + i) % nbin]:
            run.append((start + i) % nbin)
            i += 1
        a = ang[np.isin(sector, run)]
        if len(a) == 0:
            continue
        lo = run[0] * 360.0 / nbin  # unwrap the angles of the run from its first sector
        total += float(np.ptp((a - lo) % 360.0))
    return min(360.0, total)


def rim(mesh, axis: Axis | None = None, band: float = 0.003) -> dict:
    """The rim of a vessel or of a sherd and how much of its circle is kept.

    The lip is the highest point of the mesh in every small sector around the axis; the plane
    through them (a lip a little tilted to the axis or waving up and down still lies in it; a chip
    or a spike of the mesh stands off it) gives the rim points: the vertices within ``band`` metres
    under it. Their centre across the axis is where their normals meet, then the circle of the
    outer lip (only the direction of the axis is used, so a shifted axis does not matter): the rim
    diameter. ``rim_preserved_pct`` is the share of the circle the piece keeps: the sectors around
    the centre whose highest point is on the lip, each run of them counted from its first rim point
    to its last (100 for a whole rim, 25 for a quarter; a chip in the lip is not counted); when the
    circle is not reliable, the smaller of the arcs around its centre and around the axis.
    ``reliable`` when the arc is at least 40 degrees and the lip follows the circle (``rim_fit_rms_m``
    under 2% of the radius): on smaller sherds, as with a rim chart, the diameter is uncertain;
    ``centre``: the centre of the circle in 3D (a point of the axis); ``rim_chord_m`` the chord of
    the arc; ``rim_arc_deg`` the arc in degrees. The direction found from the normals is unreliable
    on small sherds: lay the rim flat and give ``Axis(any point, (0, 0, 1))``."""
    axis = axis or estimate_axis(mesh)
    d, u, w = _frame(axis, 0.0)
    centre = np.zeros(2)  # first around the axis, then around the centre of the circle found
    fit = None
    for _ in range(3):
        lip = _rim_points(mesh, axis, band, centre)
        xy = lip["xy"][lip["is_rim"]]
        fc = np.asarray(mesh.triangles_center, float) - axis.point
        fxy = np.column_stack([fc @ u, fc @ w])
        fsel = fc @ d >= lip["plane"](fxy) - 2 * band
        fn = np.asarray(mesh.face_normals, float)[fsel]
        start = _centre_from_normals(fxy[fsel], np.column_stack([fn @ u, fn @ w]))  # pulled towards the arc by noise
        fit = _outer_circle(xy, start) if len(xy) >= 5 else None
        new = np.array(fit[:2]) if fit is not None and fit[2] < 2.0 else (start if start is not None else centre)
        moved = float(np.hypot(*(new - centre)))
        centre = new
        if moved < 1e-4:
            break
    lip = _rim_points(mesh, axis, band, centre)
    xy = lip["xy"][lip["is_rim"]]
    dist = np.hypot(xy[:, 0] - centre[0], xy[:, 1] - centre[1])
    radius = float(fit[2]) if fit is not None and fit[2] < 2.0 else float(np.percentile(dist, 95)) if len(dist) else 0.0
    extent = lip["arc"]
    rms = _edge_rms(xy, centre, radius) if len(xy) else float("nan")
    reliable = bool(extent >= 40.0 and np.isfinite(rms) and rms < 0.02 * max(radius, 1e-9))
    if not reliable:  # the circle of a short arc collapses towards the arc, and the arc around it swells
        extent = min(extent, _rim_points(mesh, axis, band)["arc"])
    return {"rim_diameter_m": 2 * radius, "rim_preserved_pct": max(0.0, min(100.0, extent / 3.6)),
            "rim_arc_deg": extent, "rim_points": int(len(xy)), "rim_fit_rms_m": rms, "rim_chord_m": _span(xy),
            "reliable": reliable, "centre": axis.point + centre[0] * u + centre[1] * w}


def _rim_points(mesh, axis: Axis, band: float, centre=(0.0, 0.0)) -> dict:
    """The lip around ``centre`` (across the axis, in the frame of :func:`_frame`): the coordinates
    across the axis of every vertex (``xy``), which of them are rim points (``is_rim``), the plane
    of the lip (``plane(xy)`` gives its height) and the arc of the circle kept (``arc``, degrees)."""
    centre = np.asarray(centre, float)
    v = np.asarray(mesh.vertices, float) - axis.point
    d, u, w = _frame(axis, 0.0)
    h = v @ d
    xy = np.column_stack([v @ u, v @ w])
    high = h >= np.median(h) if len(h) > 20 else np.ones(len(h), bool)  # the lip is in the upper half
    width = _bin_width(xy[high], centre, mesh)
    coef, on_lip, tol = _lip(xy[high], h[high], centre, width, band)

    def plane(p):
        return coef[0] + coef[1] * (p[:, 0] - centre[0]) + coef[2] * (p[:, 1] - centre[1])

    top = plane(xy)
    is_rim = (h >= top - band) & (h <= top + tol)
    arc = _coverage(xy[high], centre, is_rim[high], width, on_lip) if is_rim[high].any() else 0.0
    return {"xy": xy, "is_rim": is_rim, "plane": plane, "arc": arc}


PERCENT_CLASSES = ((5.0, "<5"), (25.0, "5-25"), (50.0, "25-50"), (101.0, ">50"))  # PyArchInit thesaurus 11.2


def percent_class(pct: float | None) -> str | None:
    """The class of the preserved part in PyArchInit's pottery form (``<5``, ``5-25``, ``25-50``,
    ``>50``) for a percentage."""
    if pct is None:
        return None
    return next(name for upper, name in PERCENT_CLASSES if pct < upper)


def measure(mesh, axis: Axis | None = None, has_rim: bool = True) -> tuple[Profile, dict]:
    """The profile and the measurements of a vessel or a sherd for its pottery form (metres).

    When the lip follows a circle (:func:`rim` ``reliable``) the axis is moved to its centre (only
    the direction of the axis is then needed), the rim diameter is that of the circle and
    ``rim_preserved_pct`` the arc kept. ``height_m`` is the preserved height; ``full_profile``
    when the profile reaches the centre of the base (``base_diameter_m`` and the total height are
    known only then); ``signature`` the shape for the typological search (:func:`signature`).
    Without a reliable circle the rim diameter is measured around the axis (the median of the
    sections that reach the rim, :func:`rim_diameter_around`) and the arc kept is that of
    :func:`rim` (a lip that waves around most of the circle) or, under 60 degrees, computed from
    its chord: both depend on the axis, ``rim_reliable`` is False. ``has_rim=False``
    (a wall or base sherd): no rim measurements and no signature."""
    axis = axis or estimate_axis(mesh)
    ring = rim(mesh, axis) if has_rim else None
    if ring is not None and ring["reliable"]:
        axis = Axis(ring["centre"], axis.direction, axis.residual)
    profile = best_profile(mesh, axis)
    meas = profile.measurements()
    if not meas:
        raise ValueError("empty profile: is the axis right?")
    pts = profile.points
    full = bool(pts[:, 0].min() < 0.25 * pts[:, 0].max())
    out = {"height_m": meas["height_m"], "max_diameter_m": meas["max_diameter_m"], "full_profile": full,
           "base_diameter_m": meas["base_diameter_m"] if full else None,
           "axis": {"point": [float(x) for x in axis.point], "direction": [float(x) for x in axis.direction]}}
    if ring is not None:
        diameter, pct = ring["rim_diameter_m"], ring["rim_preserved_pct"]
        if not ring["reliable"]:  # the circle wanders: radius around the axis
            diameter = rim_diameter_around(mesh, axis) or meas["rim_diameter_m"]
            if ring["rim_arc_deg"] >= 60.0:  # a lip that waves: the sectors with lip (see rim())
                pct = ring["rim_preserved_pct"]
            else:  # a short arc: from its chord, which does not depend on where the axis is
                pct = float(np.degrees(2 * np.arcsin(min(1.0, ring["rim_chord_m"] / max(diameter, 1e-9)))) / 3.6)
        out.update({"rim_diameter_m": diameter, "rim_preserved_pct": pct, "rim_reliable": ring["reliable"],
                    "signature": signature(profile)})
    if "wall_thickness_m" in meas:
        out["wall_thickness_m"] = meas["wall_thickness_m"]
    return profile, out


def signature(profile: Profile, samples: int = 48) -> list[float | None]:
    """A shape of the profile for comparisons that a sherd also has: the outer radius from the rim
    down to one rim diameter below it, in ``samples`` steps, everything divided by the rim radius
    (so the size does not count); None where the profile does not reach."""
    env = outer_envelope(profile)
    if len(env) < 2:
        return [None] * samples
    r_rim = float(env[-1, 0]) or float(env[:, 0].max())
    top = float(env[-1, 1])
    out = []
    for k in range(samples):
        z = top - k * 2 * r_rim / samples
        if z < env[0, 1] - 1e-9:
            out.append(None)
        else:
            out.append(float(np.interp(z, env[:, 1], env[:, 0]) / r_rim))
    return out


def signature_distance(a, b, min_overlap: float = 0.25) -> float | None:
    """RMS difference of two :func:`signature` where both have values (None when they share
    less than ``min_overlap`` of the samples)."""
    pairs = [(x, y) for x, y in zip(a, b) if x is not None and y is not None]
    if len(pairs) < max(2, min_overlap * min(len(a), len(b))):
        return None
    d = np.array([x - y for x, y in pairs])
    return float(np.sqrt(np.mean(d * d)))


def _subtitle(meas: dict, scale: float) -> str:
    parts = [f"1:{scale:g}"]
    if meas.get("rim_diameter_m"):
        parts.append(f"orlo Ø {meas['rim_diameter_m'] * 100:.1f} cm")
    if meas.get("height_m"):
        parts.append(f"h {meas['height_m'] * 100:.1f} cm")
    if meas.get("wall_thickness_m"):
        parts.append(f"spessore {meas['wall_thickness_m'] * 1000:.1f} mm")
    return " - ".join(parts)


def draw(profile: Profile, path: str | Path, scale: float = 1.0, title: str | None = None,
         measures: dict | None = None, dpi: int = 300) -> Path:
    """Archaeological drawing (SVG, DXF or PNG by extension) at 1:``scale`` (millimetres on paper).

    Left half: section filled black; right half: exterior silhouette; rim line; dashed axis.
    ``measures`` (:func:`measure`) for the line under the title, default those of the profile;
    the PNG (``dpi``) is the drawing as an image, for the media of PyArchInit.
    """
    path = Path(path)
    if not any(len(c) > 1 for c in profile.curves):
        raise ValueError("empty profile")
    section = _section_polygon(profile)
    env = outer_envelope(profile)
    rmax, hmax, hmin = profile.points[:, 0].max(), profile.points[:, 1].max(), profile.points[:, 1].min()
    rim_r = env[-1, 0]
    base_r = env[0, 0]
    if path.suffix.lower() == ".dxf":
        import ezdxf

        doc = ezdxf.new("R2010", setup=True)
        doc.units = ezdxf.units.MM
        msp = doc.modelspace()
        for name in ("SEZIONE", "PROSPETTO", "ASSE", "TESTI"):
            doc.layers.add(name)
        k = 1000.0
        sec = np.column_stack([-section[:, 0], section[:, 1]]) * k
        msp.add_lwpolyline(sec.tolist(), close=True, dxfattribs={"layer": "SEZIONE"})
        hatch = msp.add_hatch(color=7, dxfattribs={"layer": "SEZIONE"})
        hatch.paths.add_polyline_path(sec.tolist(), is_closed=True)
        msp.add_lwpolyline((env * k).tolist(), dxfattribs={"layer": "PROSPETTO"})
        msp.add_line((-rim_r * k, hmax * k), (rim_r * k, hmax * k), dxfattribs={"layer": "PROSPETTO"})
        msp.add_line((0, hmin * k - 5), (0, hmax * k + 5), dxfattribs={"layer": "ASSE", "linetype": "DASHED"})
        if title:
            msp.add_text(title, height=4, dxfattribs={"layer": "TESTI"}).set_placement((-rmax * k, hmax * k + 8))
        doc.saveas(path)
        return path
    k = 1000.0 / scale
    m = 12
    w = 2 * rmax * k + 2 * m
    h = (hmax - hmin) * k + 2 * m + 10

    def tx(r, hh):
        return w / 2 + r * k, m + 10 + (hmax - hh) * k

    d_sec = "M " + " L ".join("%.3f,%.3f" % tx(-r, hh) for r, hh in section) + " Z"
    d_env = "M " + " L ".join("%.3f,%.3f" % tx(r, hh) for r, hh in env)
    x0, y0 = tx(0, hmin - 0.003)
    x1, y1 = tx(0, hmax + 0.004)
    xa, ya = tx(-rim_r, hmax)
    xb, yb = tx(rim_r, hmax)
    xc, yc = tx(0, hmin)
    xd, yd = tx(base_r, env[0, 1])
    sub = _subtitle(measures or profile.measurements(), scale)
    base_line = profile.points[:, 0].min() < 0.25 * rmax
    if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".tif", ".tiff"):
        _draw_image(path, (w, h), dpi, [tx(-r, hh) for r, hh in section], [tx(r, hh) for r, hh in env],
                    [((xa, ya), (xb, yb))] + ([((xc, yc), (xd, yd))] if base_line else []), ((x0, y0), (x1, y1)),
                    title or "Profilo", sub, m)
        return path
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.1f}mm" height="{h:.1f}mm" viewBox="0 0 {w:.3f} {h:.3f}">'
           f'<text x="{m}" y="6" font-size="4" font-family="sans-serif">{escape(title or "Profilo")}</text>'
           f'<text x="{m}" y="11" font-size="3" font-family="sans-serif">{escape(sub)}</text>'
           f'<path d="{d_sec}" fill="black" stroke="black" stroke-width="0.25"/>'
           f'<path d="{d_env}" fill="none" stroke="black" stroke-width="0.3"/>'
           f'<line x1="{xa:.3f}" y1="{ya:.3f}" x2="{xb:.3f}" y2="{yb:.3f}" stroke="black" stroke-width="0.3"/>'
           + (f'<line x1="{xc:.3f}" y1="{yc:.3f}" x2="{xd:.3f}" y2="{yd:.3f}" stroke="black" stroke-width="0.3"/>'
              if base_line else "") +
           f'<line x1="{x0:.3f}" y1="{y0:.3f}" x2="{x1:.3f}" y2="{y1:.3f}" stroke="black" stroke-width="0.2" '
           f'stroke-dasharray="3,1.5"/></svg>')
    path.write_text(svg)
    return path


def _font_file() -> Path | None:
    """A font with the diameter sign: DejaVu Sans of matplotlib, when it is installed."""
    import importlib.util

    spec = importlib.util.find_spec("matplotlib")
    for base in (spec.submodule_search_locations or []) if spec else []:
        f = Path(base) / "mpl-data" / "fonts" / "ttf" / "DejaVuSans.ttf"
        if f.exists():
            return f
    return None


def _draw_image(path: Path, size_mm, dpi: int, section, envelope, lines, axis, title: str, sub: str,
                margin: float) -> None:
    """The drawing of :func:`draw` as an image (coordinates in millimetres on paper)."""
    from PIL import Image, ImageDraw, ImageFont

    k = dpi / 25.4  # pixels in a millimetre

    def px(pts):
        return [(x * k, y * k) for x, y in pts]

    face = _font_file()
    if face is None:  # the font of Pillow has no diameter sign
        sub = sub.replace("Ø", "diam.")

    def font(mm):
        if face is not None:
            return ImageFont.truetype(str(face), max(6, int(mm * k)))
        try:
            return ImageFont.load_default(size=max(6, int(mm * k)))
        except TypeError:  # Pillow < 10.1: a fixed bitmap font
            return ImageFont.load_default()

    img = Image.new("RGB", (int(np.ceil(size_mm[0] * k)), int(np.ceil(size_mm[1] * k))), "white")
    g = ImageDraw.Draw(img)
    g.polygon(px(section), fill="black", outline="black")
    g.line(px(envelope), fill="black", width=max(1, round(0.3 * k)), joint="curve")
    for a, b in lines:
        g.line(px([a, b]), fill="black", width=max(1, round(0.3 * k)))
    (x0, y0), (x1, y1) = axis
    y = min(y0, y1)
    while y < max(y0, y1):  # dashed: 3 mm drawn, 1.5 mm left out
        g.line(px([(x0, y), (x0, min(y + 3.0, max(y0, y1)))]), fill="black", width=max(1, round(0.2 * k)))
        y += 4.5
    g.text((margin * k, 6 * k), title, fill="black", font=font(4), anchor="ls")
    g.text((margin * k, 11 * k), sub, fill="black", font=font(3), anchor="ls")
    img.save(path, dpi=(dpi, dpi))


def chunk_mesh_world(chunk):
    mesh = chunk.model
    if mesh is None:
        raise RuntimeError("build the model first")
    mesh.apply_transform(chunk.transform)
    return mesh
