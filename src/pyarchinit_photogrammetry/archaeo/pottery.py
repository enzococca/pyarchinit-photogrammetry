"""Pottery drawing from 3D models: rotation axis, profile, measurements, drawing.

Works on whole vessels and on sherds. The rotation axis is estimated from the
surface normals (on a surface of revolution every normal line meets the axis)
or given explicitly (e.g. the turntable axis when the scan was referenced with
a turntable mat: ``axis=((0, 0, 0), (0, 0, 1))``).

The drawing follows the usual convention: axis in the middle, section (profile,
filled) on the left, exterior view outline on the right, rim and base lines.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

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


def estimate_axis(mesh, samples: int = 4000, seed: int = 0) -> Axis:
    """Rotation axis of a (possibly fragmentary) vessel from its normals."""
    from scipy.optimize import least_squares

    rng = np.random.default_rng(seed)
    idx = rng.choice(len(mesh.faces), min(samples, len(mesh.faces)), replace=False)
    centers = mesh.triangles_center[idx] - mesh.triangles_center.mean(0)
    normals = mesh.face_normals[idx]
    offset = mesh.triangles_center.mean(0)
    _, _, vt = np.linalg.svd(centers - centers.mean(0), full_matrices=False)
    best = None
    for d0 in vt:
        theta = np.arccos(np.clip(d0[2], -1, 1))
        phi = np.arctan2(d0[1], d0[0])
        res = least_squares(_normal_line_distances, [theta, phi, 0.0, 0.0], args=(centers, normals),
                            loss="soft_l1", f_scale=np.ptp(centers) * 0.01)
        cost = np.sqrt(np.mean(_normal_line_distances(res.x, centers, normals) ** 2))
        if best is None or cost < best[0]:
            best = (cost, res.x)
    cost, (theta, phi, a, b) = best
    d = np.array([np.sin(theta) * np.cos(phi), np.sin(theta) * np.sin(phi), np.cos(theta)])
    u = np.cross(d, [1.0, 0, 0]) if abs(d[0]) < 0.9 else np.cross(d, [0, 1.0, 0])
    u /= np.linalg.norm(u)
    v = np.cross(d, u)
    point = offset + a * u + b * v
    # orient the axis from base to rim: the mouth is usually the wider end
    h = (mesh.vertices - point) @ d
    r = np.linalg.norm(np.cross(mesh.vertices - point, d), axis=1)
    lo, hi = h < np.percentile(h, 15), h > np.percentile(h, 85)
    if r[lo].mean() > r[hi].mean():
        d = -d
    return Axis(point, d, float(cost))


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


def draw(profile: Profile, path: str | Path, scale: float = 1.0, title: str | None = None) -> Path:
    """Archaeological drawing (SVG or DXF by extension) at 1:``scale`` (millimetres on paper).

    Left half: section filled black; right half: exterior silhouette; rim line; dashed axis.
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
    meas = profile.measurements()
    sub = (f"1:{scale:g} - orlo Ø {meas.get('rim_diameter_m', 0) * 100:.1f} cm - h {meas.get('height_m', 0) * 100:.1f} cm"
           + (f" - spessore {meas['wall_thickness_m'] * 1000:.1f} mm" if meas.get("wall_thickness_m") else ""))
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.1f}mm" height="{h:.1f}mm" viewBox="0 0 {w:.3f} {h:.3f}">'
           f'<text x="{m}" y="6" font-size="4" font-family="sans-serif">{title or "Profilo"}</text>'
           f'<text x="{m}" y="11" font-size="3" font-family="sans-serif">{sub}</text>'
           f'<path d="{d_sec}" fill="black" stroke="black" stroke-width="0.25"/>'
           f'<path d="{d_env}" fill="none" stroke="black" stroke-width="0.3"/>'
           f'<line x1="{xa:.3f}" y1="{ya:.3f}" x2="{xb:.3f}" y2="{yb:.3f}" stroke="black" stroke-width="0.3"/>'
           + (f'<line x1="{xc:.3f}" y1="{yc:.3f}" x2="{xd:.3f}" y2="{yd:.3f}" stroke="black" stroke-width="0.3"/>'
              if profile.points[:, 0].min() < 0.25 * rmax else "") +
           f'<line x1="{x0:.3f}" y1="{y0:.3f}" x2="{x1:.3f}" y2="{y1:.3f}" stroke="black" stroke-width="0.2" '
           f'stroke-dasharray="3,1.5"/></svg>')
    path.write_text(svg)
    return path


def chunk_mesh_world(chunk):
    mesh = chunk.model
    if mesh is None:
        raise RuntimeError("build the model first")
    mesh.apply_transform(chunk.transform)
    return mesh
