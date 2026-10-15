"""Sections, profiles and plan slices for archaeological drawings.

All inputs and outputs are in world (chunk CRS) coordinates. A vertical
section is described by its trace ``p1 -> p2`` in plan; the result is drawn in
section coordinates ``(distance along the trace, elevation)``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


@dataclass
class Section:
    p1: np.ndarray
    p2: np.ndarray
    polylines: list[np.ndarray] = field(default_factory=list)  # each (N,2): distance, elevation
    points: np.ndarray | None = None  # (N,2) projected cloud points inside the slab
    label: str = "Sezione"
    kind: str = "vertical"  # or "plan" for horizontal slices (then coordinates are X, Y)

    @property
    def length(self) -> float:
        return float(np.linalg.norm(self.p2[:2] - self.p1[:2]))

    def to_world(self, poly: np.ndarray) -> np.ndarray:
        """Section coordinates back to 3D world coordinates."""
        if self.kind == "plan":
            return np.column_stack([poly, np.full(len(poly), self.p1[2])])
        d = (self.p2[:2] - self.p1[:2]) / self.length
        xy = self.p1[:2] + poly[:, :1] * d
        return np.column_stack([xy, poly[:, 1]])

    # ----------------------------------------------------------- drawing
    def to_dxf(self, path: str | Path, world: bool = False, grid: float | None = 0.1) -> Path:
        """DXF in metres. ``world=False``: section coordinates (x = distance, y = elevation);
        ``world=True``: true 3D polylines placed in the CRS."""
        import ezdxf

        doc = ezdxf.new("R2010", setup=True)
        doc.units = ezdxf.units.M
        msp = doc.modelspace()
        for name, color in (("SEZ_PROFILO", 7), ("SEZ_PUNTI", 8), ("SEZ_GRIGLIA", 9), ("SEZ_TESTI", 1)):
            if name not in doc.layers:
                doc.layers.add(name, color=color)
        for poly in self.polylines:
            if len(poly) < 2:
                continue
            if world:
                msp.add_polyline3d(self.to_world(poly).tolist(), dxfattribs={"layer": "SEZ_PROFILO"})
            else:
                msp.add_lwpolyline(poly.tolist(), dxfattribs={"layer": "SEZ_PROFILO"})
        if not world and self.polylines:
            allp = np.vstack(self.polylines)
            (x0, y0), (x1, y1) = allp.min(0), allp.max(0)
            if grid:
                for z in np.arange(np.floor(y0 / grid) * grid, y1 + grid, grid):
                    msp.add_line((x0, z), (x1, z), dxfattribs={"layer": "SEZ_GRIGLIA"})
                    msp.add_text(f"{z:.2f}", height=grid * 0.15,
                                 dxfattribs={"layer": "SEZ_TESTI"}).set_placement((x0 - grid * 0.9, z))
            msp.add_text(self.label, height=grid * 0.3 if grid else 0.05,
                         dxfattribs={"layer": "SEZ_TESTI"}).set_placement((x0, y1 + (grid or 0.1) * 0.6))
        path = Path(path)
        doc.saveas(path)
        return path

    def to_svg(self, path: str | Path, scale: float = 20.0, stroke_mm: float = 0.35) -> Path:
        """SVG at the given drawing scale (1:``scale``), units in millimetres."""
        if not self.polylines:
            raise ValueError("empty section")
        allp = np.vstack(self.polylines)
        lo, hi = allp.min(0), allp.max(0)
        k = 1000.0 / scale  # metres -> paper millimetres
        margin = 10
        w = (hi[0] - lo[0]) * k + 2 * margin
        h = (hi[1] - lo[1]) * k + 2 * margin + 8

        def tr(p):
            return np.column_stack([(p[:, 0] - lo[0]) * k + margin, (hi[1] - p[:, 1]) * k + margin + 8])

        paths = []
        for poly in self.polylines:
            q = tr(poly)
            d = "M " + " L ".join(f"{x:.3f},{y:.3f}" for x, y in q)
            paths.append(f'<path d="{d}" fill="none" stroke="black" stroke-width="{stroke_mm}"/>')
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.1f}mm" height="{h:.1f}mm" '
               f'viewBox="0 0 {w:.3f} {h:.3f}">'
               f'<text x="{margin}" y="6" font-size="4" font-family="sans-serif">{self.label} - 1:{scale:g}</text>'
               + "".join(paths) + "</svg>")
        path = Path(path)
        path.write_text(svg)
        return path


def _join_segments(segments: np.ndarray, tol: float) -> list[np.ndarray]:
    """Chain 2D segments ``(N,2,2)`` into polylines."""
    if len(segments) == 0:
        return []
    from collections import defaultdict

    q = np.round(segments / tol).astype(np.int64)
    ends = defaultdict(list)
    for i, (a, b) in enumerate(q):
        ends[tuple(a)].append((i, 0))
        ends[tuple(b)].append((i, 1))
    used = np.zeros(len(segments), bool)
    lines = []
    for start in range(len(segments)):
        if used[start]:
            continue
        used[start] = True
        chain = [segments[start, 0], segments[start, 1]]
        for direction in (1, 0):  # grow forward then backward
            key = tuple(q[start, direction])
            while True:
                nxt = next(((i, e) for i, e in ends[key] if not used[i]), None)
                if nxt is None:
                    break
                i, e = nxt
                used[i] = True
                other = segments[i, 1 - e]
                if direction == 1:
                    chain.append(other)
                else:
                    chain.insert(0, other)
                key = tuple(q[i, 1 - e])
        lines.append(np.asarray(chain))
    return lines


def _world_mesh(chunk):
    mesh = chunk.model
    if mesh is None:
        raise RuntimeError("build the model first")
    mesh.apply_transform(chunk.transform)
    return mesh


def section(chunk, p1, p2, source: str = "model", thickness: float = 0.01, label: str = "Sezione") -> Section:
    """Vertical section along the trace ``p1 -> p2`` (world XY, optional Z ignored)."""
    p1 = np.asarray(p1, float)[:2]
    p2 = np.asarray(p2, float)[:2]
    length = np.linalg.norm(p2 - p1)
    if length <= 0:
        raise ValueError("p1 and p2 coincide")
    d = (p2 - p1) / length
    normal = np.array([-d[1], d[0], 0.0])
    origin = np.array([p1[0], p1[1], 0.0])
    sec = Section(np.r_[p1, 0], np.r_[p2, 0], label=label)
    if source == "model":
        import trimesh

        mesh = _world_mesh(chunk)
        origin[2] = mesh.vertices[:, 2].mean()
        segs = trimesh.intersections.mesh_plane(mesh, normal, origin)
        if len(segs):
            s = (segs[..., :2] - p1) @ d
            seg2d = np.stack([s, segs[..., 2]], -1)
            keep = (seg2d[..., 0] >= -1e-9).all(1) & (seg2d[..., 0] <= length + 1e-9).all(1)
            scale = max(np.ptp(seg2d[keep][..., 0]) if keep.any() else 1.0, 1e-3)
            sec.polylines = _join_segments(seg2d[keep], tol=scale * 1e-7)
    else:
        pc = chunk.point_cloud if source == "point_cloud" else chunk.tie_points
        xyz = chunk.to_world(pc.xyz)
        off = (xyz - origin) @ normal
        s = (xyz[:, :2] - p1) @ d
        m = (np.abs(off) <= thickness / 2) & (s >= 0) & (s <= length)
        pts = np.column_stack([s[m], xyz[m, 2]])
        sec.points = pts
        if len(pts):
            step = max(thickness, length / 2000)
            bins = (pts[:, 0] / step).astype(int)
            top = np.full(bins.max() + 1, -np.inf)
            np.maximum.at(top, bins, pts[:, 1])
            ok = np.isfinite(top)
            sec.polylines = [np.column_stack([(np.nonzero(ok)[0] + 0.5) * step, top[ok]])]
    return sec


def sections_along(chunk, baseline, interval: float, half_width: float, source: str = "model",
                   prefix: str = "Sez.") -> list[Section]:
    """Cross sections perpendicular to a baseline polyline (world XY), every ``interval`` metres."""
    base = np.asarray(baseline, float)[:, :2]
    seg_len = np.linalg.norm(np.diff(base, axis=0), axis=1)
    total = seg_len.sum()
    out = []
    for i, dist in enumerate(np.arange(0, total + 1e-9, interval)):
        k = min(np.searchsorted(np.cumsum(seg_len), dist, side="right"), len(seg_len) - 1)
        before = np.cumsum(seg_len)[k] - seg_len[k]
        t = (dist - before) / seg_len[k]
        p = base[k] + t * (base[k + 1] - base[k])
        dirv = (base[k + 1] - base[k]) / seg_len[k]
        perp = np.array([-dirv[1], dirv[0]])
        out.append(section(chunk, p - perp * half_width, p + perp * half_width, source=source,
                           label=f"{prefix} {i + 1}"))
    return out


def plan_slice(chunk, elevation: float, label: str | None = None) -> Section:
    """Horizontal slice of the model at ``elevation`` (e.g. the plan of a wall at a given level)."""
    import trimesh

    mesh = _world_mesh(chunk)
    segs = trimesh.intersections.mesh_plane(mesh, [0, 0, 1.0], [0, 0, float(elevation)])
    sec = Section(np.array([0, 0, elevation], float), np.array([1, 0, elevation], float),
                  label=label or f"Pianta a quota {elevation:.3f}", kind="plan")
    if len(segs):
        seg2d = segs[..., :2]
        scale = max(np.ptp(seg2d[..., 0]), 1e-3)
        sec.polylines = _join_segments(seg2d, tol=scale * 1e-7)
    return sec
