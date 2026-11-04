"""Measures on the survey: surface area in 3D, slope and aspect, height above the ground.

* :func:`surface_area`: inside a plan polygon, the area in plan and the area of the surface - from
  the DEM (each cell counted ``sqrt(1 + gx^2 + gy^2)`` times, the cells on the border by the share
  of them inside) or from the faces of the model whose centre is inside;
* :func:`slope_aspect_at`: slope (degrees) and aspect (degrees clockwise from north, the direction
  the ground faces downhill) of the DEM at a point, from its 3 x 3 cells (Horn);
* :func:`aspect_raster`: the aspect of the whole DEM as a GeoTIFF (-1 where flat);
* :func:`ground_surface` / :func:`height_above_ground`: the ground from the points of the ground
  class (2) of the dense cloud, on a grid with the holes filled, and the height of every point
  above it (vegetation, walls, people); :func:`ground_dem_at` reads a DTM kept as a version of the
  DEM, for the height above the ground under the cursor.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

GROUND = 2


def _window(src, polygon, pad: int = 1):
    from rasterio.windows import Window, from_bounds

    win = from_bounds(*polygon.bounds, transform=src.transform).round_offsets().round_lengths()
    col0, row0 = max(int(win.col_off) - pad, 0), max(int(win.row_off) - pad, 0)
    col1 = min(int(win.col_off + win.width) + pad + 1, src.width)
    row1 = min(int(win.row_off + win.height) + pad + 1, src.height)
    return Window(col0, row0, max(col1 - col0, 0), max(row1 - row0, 0))


def surface_area(dem_path: str | Path, polygon, model=None) -> dict:
    """Areas inside ``polygon`` (shapely, plan, the CRS of the DEM): ``plan`` and ``surface``
    from the DEM (square metres), ``mean_slope`` (degrees), ``z_min``/``z_max``; with ``model``
    (a mesh in the same coordinates with ``vertices`` and ``faces``) also ``model_surface`` and
    ``model_plan`` from its faces whose centre is inside."""
    import rasterio
    from shapely import contains_xy

    from .terrain import _coverage

    out = {"plan_polygon": float(polygon.area)}
    with rasterio.open(dem_path) as src:
        win = _window(src, polygon)
        z = src.read(1, window=win, masked=True).astype(np.float64).filled(np.nan)
        tr = src.window_transform(win)
    res_x, res_y = abs(tr.a), abs(tr.e)
    cov = _coverage(polygon, z.shape, tr)
    valid = np.isfinite(z) & (cov > 0)
    if valid.any():
        zf = np.where(np.isfinite(z), z, np.nanmean(z[valid]))
        gy, gx = np.gradient(zf, res_y, res_x)
        factor = np.sqrt(1.0 + gx ** 2 + gy ** 2)
        cell = res_x * res_y
        out.update(plan=float((cov * cell)[valid].sum()), surface=float((cov * cell * factor)[valid].sum()),
                   mean_slope=float(np.degrees(np.arctan(np.sqrt(np.average((factor[valid] ** 2 - 1),
                                                                             weights=cov[valid]))))),
                   z_min=float(z[valid].min()), z_max=float(z[valid].max()))
    if model is not None:
        v = np.asarray(model.vertices, float)
        f = np.asarray(model.faces, int)
        tri = v[f]
        centre = tri.mean(axis=1)
        inside = contains_xy(polygon, centre[:, 0], centre[:, 1])
        cross = np.cross(tri[inside, 1] - tri[inside, 0], tri[inside, 2] - tri[inside, 0])
        out.update(model_surface=float(0.5 * np.linalg.norm(cross, axis=1).sum()),
                   model_plan=float(0.5 * np.abs(cross[:, 2]).sum()), model_faces=int(inside.sum()))
    return out


def slope_aspect_at(dem_path: str | Path, x: float, y: float) -> tuple[float, float] | None:
    """Slope and aspect (degrees) of the DEM at world ``(x, y)``, None outside or at its edge."""
    import rasterio
    from rasterio.windows import Window

    with rasterio.open(dem_path) as src:
        col, row = ~src.transform @ (x, y)
        c, r = int(np.floor(col)), int(np.floor(row))
        if not (1 <= c < src.width - 1 and 1 <= r < src.height - 1):
            return None
        z = src.read(1, window=Window(c - 1, r - 1, 3, 3), masked=True).astype(np.float64).filled(np.nan)
        rx, ry = abs(src.transform.a), abs(src.transform.e)
    if not np.isfinite(z).all():
        return None
    return _slope_aspect(z, rx, ry)


def _slope_aspect(z: np.ndarray, rx: float, ry: float) -> tuple[float, float]:
    """Horn's slope and aspect of the centre of a 3 x 3 window (row 0 to the north)."""
    dzdx = ((z[0, 2] + 2 * z[1, 2] + z[2, 2]) - (z[0, 0] + 2 * z[1, 0] + z[2, 0])) / (8 * rx)
    dzdy = ((z[0, 0] + 2 * z[0, 1] + z[0, 2]) - (z[2, 0] + 2 * z[2, 1] + z[2, 2])) / (8 * ry)  # northwards
    slope = float(np.degrees(np.arctan(np.hypot(dzdx, dzdy))))
    aspect = float((np.degrees(np.arctan2(-dzdx, -dzdy)) + 360.0) % 360.0) if slope > 1e-6 else -1.0
    return slope, aspect


def aspect_raster(dem_path: str | Path, out_path: str | Path, crs: str | None = None) -> Path:
    """Aspect of every cell of the DEM (degrees clockwise from north, downhill; -1 where flat)."""
    from ..engine.dem import NODATA, read_dem, write_geotiff

    z, tr, _, res = read_dem(Path(dem_path))
    gy, gx = np.gradient(z, res, res)  # gy: towards the south (rows go down)
    dzdx, dzdy = gx, -gy
    aspect = (np.degrees(np.arctan2(-dzdx, -dzdy)) + 360.0) % 360.0
    flat = np.hypot(dzdx, dzdy) < 1e-6
    out = np.where(flat, -1.0, aspect)
    out = np.where(np.isfinite(z), out, NODATA).astype(np.float32)
    return write_geotiff(Path(out_path), out, (tr.c, tr.f), res, crs, nodata=NODATA)


def ground_surface(xyz: np.ndarray, classes: np.ndarray, cell: float | None = None, ground=(GROUND,)):
    """Ground on a grid from the points of the ``ground`` classes: the median of each cell (a
    few points of vegetation taken for ground do not move it), the empty cells interpolated.
    Returns ``(z (h, w), x0, y0, cell)``, row 0 at ``y0`` (south)."""
    from scipy.interpolate import LinearNDInterpolator, NearestNDInterpolator

    sel = np.isin(classes, list(ground))
    pts = np.asarray(xyz, float)[sel]
    if len(pts) < 10:
        raise ValueError("too few ground points: classify the cloud first (class 2)")
    lo, hi = pts[:, :2].min(0), pts[:, :2].max(0)
    if cell is None:
        cell = float(max(np.sqrt(np.prod(np.maximum(hi - lo, 1e-9)) / len(pts)) * 3, 1e-4))
    w, h = int((hi[0] - lo[0]) / cell) + 1, int((hi[1] - lo[1]) / cell) + 1
    col = ((pts[:, 0] - lo[0]) / cell).astype(int)
    row = ((pts[:, 1] - lo[1]) / cell).astype(int)
    idx = row * w + col
    order = np.lexsort((pts[:, 2], idx))
    cells, start, count = np.unique(idx[order], return_index=True, return_counts=True)
    z = np.full(h * w, np.inf)
    z[cells] = pts[order[start + count // 2], 2]
    z = z.reshape(h, w)
    known = np.isfinite(z)
    if not known.all():
        rr, cc = np.nonzero(known)
        inter = LinearNDInterpolator(np.column_stack([cc, rr]), z[known])
        hr, hc = np.nonzero(~known)
        fill = inter(hc, hr)
        miss = ~np.isfinite(fill)
        if miss.any():
            fill[miss] = NearestNDInterpolator(np.column_stack([cc, rr]), z[known])(hc[miss], hr[miss])
        z[~known] = fill
    return z, float(lo[0]), float(lo[1]), cell


def height_above_ground(xyz: np.ndarray, classes: np.ndarray, cell: float | None = None, ground=(GROUND,)):
    """Height of every point above the ground of :func:`ground_surface` (metres; bilinear
    between the cells, the nearest cell outside the ground grid)."""
    z, x0, y0, cell = ground_surface(xyz, classes, cell, ground)
    h, w = z.shape
    p = np.asarray(xyz, float)
    fx = np.clip((p[:, 0] - x0) / cell - 0.5, 0, w - 1)
    fy = np.clip((p[:, 1] - y0) / cell - 0.5, 0, h - 1)
    c0, r0 = np.floor(fx).astype(int), np.floor(fy).astype(int)
    c1, r1 = np.minimum(c0 + 1, w - 1), np.minimum(r0 + 1, h - 1)
    tx, ty = fx - c0, fy - r0
    g = (z[r0, c0] * (1 - tx) * (1 - ty) + z[r0, c1] * tx * (1 - ty) + z[r1, c0] * (1 - tx) * ty
         + z[r1, c1] * tx * ty)
    return p[:, 2] - g


def ground_dem(chunk) -> Path | None:
    """A DEM of the ground (DTM) of the chunk: a kept version of the DEM built from the ground
    class only, else None."""
    from ..core import versions

    for e in reversed(versions.versions(chunk, "dem")):
        classes = (e.get("params") or {}).get("classes")
        if classes and set(int(c) for c in classes) <= {GROUND}:
            path = versions.version_file(chunk, "dem", e["id"])
            if path is not None and Path(path).exists():
                return Path(path)
    active = (chunk.meta.get("dem") or {}).get("classes")
    if active and set(int(c) for c in active) <= {GROUND} and chunk.dem_path is not None:
        return chunk.dem_path
    return None


def ground_dem_at(path: str | Path, x: float, y: float) -> float | None:
    import rasterio

    with rasterio.open(path) as src:
        col, row = ~src.transform @ (x, y)
        if not (0 <= col < src.width and 0 <= row < src.height):
            return None
        v = src.read(1, window=((int(row), int(row) + 1), (int(col), int(col) + 1)), masked=True)
    return None if np.ma.is_masked(v[0, 0]) else float(v[0, 0])


# --- free drawings in 3D ----------------------------------------------------------------------

STEEP_COS = 0.5  # a polygon whose best plane has |n_z| below this is measured in its own plane
FRONT_MARGIN = 0.5  # metres in front of the drawing (or the vertices' spread, if more) where the model is looked at
DIAMETER_SHARE = 0.25  # ... or this share of the polygon's diameter in the view plane, if more
MIN_CELL = 1e-3  # metres: the smallest piece a partly hidden face is split into


def _pts(xyz, minimum: int = 0) -> np.ndarray:
    p = np.asarray(xyz, float).reshape(-1, 3)
    if len(p) < minimum:
        raise ValueError(f"at least {minimum} points are needed, got {len(p)}")
    return p


def _segments(p: np.ndarray, closed: bool) -> np.ndarray:
    return np.diff(np.vstack([p, p[:1]]) if closed and len(p) > 1 else p, axis=0)


def length_3d(xyz, closed: bool = False) -> float:
    """Length of the straight segments through ``xyz`` (closed: back to the first vertex)."""
    return float(np.linalg.norm(_segments(_pts(xyz), closed), axis=1).sum())


def length_plan(xyz, closed: bool = False) -> float:
    """The same length projected on the horizontal plane."""
    return float(np.linalg.norm(_segments(_pts(xyz), closed)[:, :2], axis=1).sum())


def best_plane(xyz) -> tuple[np.ndarray, np.ndarray]:
    """Centre and unit normal (z >= 0) of the least-squares plane through ``xyz`` (at least 3 points
    not on one line: ValueError otherwise)."""
    p = _pts(xyz, 3)
    centre = p.mean(axis=0)
    _, sv, vt = np.linalg.svd(p - centre)
    if sv[1] <= 1e-12 * max(sv[0], 1e-300):
        raise ValueError("the points lie on one line: no plane through them")
    n = vt[-1]
    if n[2] < 0 or (n[2] == 0 and (n[1] < 0 or (n[1] == 0 and n[0] < 0))):
        n = -n
    return centre, n / np.linalg.norm(n)


def _valid(uv: np.ndarray):
    """The polygon through ``uv`` made valid (a bow-tie keeps both lobes), the one geometry every measure uses."""
    import shapely

    return shapely.make_valid(shapely.Polygon(uv))


def _plane_frame(n: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """u horizontal along the plane, v up the plane."""
    u = np.cross([0.0, 0.0, 1.0], n)
    norm = np.linalg.norm(u)
    u = np.array([1.0, 0.0, 0.0]) if norm < 1e-12 else u / norm
    return u, np.cross(n, u)


def area_3d(xyz) -> float:
    """Area of the polygon in its best-fit plane."""
    p = _pts(xyz, 3)
    centre, n = best_plane(p)
    u, v = _plane_frame(n)
    d = p - centre
    return float(_valid(np.column_stack([d @ u, d @ v])).area)


def area_plan(xyz) -> float:
    """Area of the polygon projected on the horizontal plane."""
    p = _pts(xyz, 3)
    return float(_valid(p[:, :2] - p[:, :2].mean(axis=0)).area)


def slope_aspect_of(xyz) -> tuple[float, float]:
    """Slope (degrees) and aspect (clockwise from north, downhill; -1 when flat) of the best plane."""
    n = best_plane(xyz)[1]
    slope = float(np.degrees(np.arccos(min(1.0, abs(n[2])))))
    aspect = float((np.degrees(np.arctan2(n[0], n[1])) + 360.0) % 360.0) if slope > 1e-6 else -1.0
    return slope, aspect


SAMPLES = np.array([(i + 1 / 3, j + 1 / 3) if up else (i + 2 / 3, j + 2 / 3)
                    for i in range(3) for j in range(3 - i) for up in (True, False) if up or i + j < 2]) / 3
"""Barycentric (b1, b2) of the centres of the 9 equal sub-triangles of a face: where its visibility is tested."""


def _covering(a2, h, pts, exclude=None, eps=1e-9):
    """For the projected faces ``a2`` (F, 3, 2) with heights ``h`` (F, 3) and the points ``pts`` (P, 2):
    the pairs (point, face) whose face contains the point (strictly with ``eps`` > 0, its edges too
    with ``eps`` < 0), and the face's height there. ``exclude`` (P,): a face never paired with that point.
    The faces are binned on a grid of about their edge length, so each point is tested against a few."""
    lo, hi = a2.min(axis=1), a2.max(axis=1)
    edges = np.linalg.norm(a2 - np.roll(a2, 1, axis=1), axis=2)
    span_lo, span_hi = np.minimum(lo.min(0), pts.min(0)), np.maximum(hi.max(0), pts.max(0))
    extent = np.maximum(span_hi - span_lo, 1e-12)
    cell = max(float(np.median(edges)), float(np.sqrt(extent.prod() / (4.0 * len(a2)))), 1e-12)
    nx, ny = (np.floor(extent / cell).astype(int) + 1).tolist()
    i0, j0 = np.floor((lo - span_lo) / cell).astype(int).T
    i1, j1 = np.minimum(np.floor((hi - span_lo) / cell).astype(int), [nx - 1, ny - 1]).T
    wi, wj = i1 - i0 + 1, j1 - j0 + 1
    count = wi * wj
    face = np.repeat(np.arange(len(a2)), count)
    local = np.arange(count.sum()) - np.repeat(np.cumsum(count) - count, count)
    cells = (i0[face] + local // wj[face]) * ny + j0[face] + local % wj[face]
    order = np.argsort(cells, kind="stable")
    cells, face = cells[order], face[order]
    pi, pj = np.minimum(np.floor((pts - span_lo) / cell).astype(int), [nx - 1, ny - 1]).T
    pcell = pi * ny + pj
    start = np.searchsorted(cells, pcell, "left")
    many = np.searchsorted(cells, pcell, "right") - start
    point = np.repeat(np.arange(len(pts)), many)
    cand = face[np.repeat(start, many) + np.arange(many.sum()) - np.repeat(np.cumsum(many) - many, many)]
    if exclude is not None:
        other = cand != exclude[point]
        point, cand = point[other], cand[other]
    t = a2[cand]
    q = pts[point] - t[:, 0]
    e1, e2 = t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]
    det = e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]
    ok = np.abs(det) > 0
    det = np.where(ok, det, 1.0)
    b1 = (q[:, 0] * e2[:, 1] - q[:, 1] * e2[:, 0]) / det
    b2 = (e1[:, 0] * q[:, 1] - e1[:, 1] * q[:, 0]) / det
    inside = ok & (b1 > eps) & (b2 > eps) & (b1 + b2 < 1 - eps)
    hh = h[cand]
    height = hh[:, 0] + b1 * (hh[:, 1] - hh[:, 0]) + b2 * (hh[:, 2] - hh[:, 0])
    return point[inside], cand[inside], height[inside]


def model_area_inside(model, xyz, toward=None) -> dict:
    """Surface of the model under the polygon ``xyz``: the first layer met looking at the drawing.

    The faces are clipped by the polygon's vertical projection (per face, plan area / |n_z|) and seen from
    above; for a steep polygon (best plane with |n_z| < STEEP_COS, a wall) the same in the polygon's own
    plane, said by ``"projection": "plane"``, looked at along the plane's normal from the side of
    ``toward`` (a point, e.g. the cameras' centre) or, without it, from the side whose front-most surface
    lies closest to the polygon's vertices. The look starts just in front of the drawing: faces more than
    max(FRONT_MARGIN, the vertices' spread along the viewing axis, DIAMETER_SHARE x the polygon's diameter
    in the view plane) in front of the front-most vertex are neither measured nor in the way (a shelter roof
    over the floor, a scaffold before a wall; a screen nearer than that is the first surface and is
    measured). A face counts for the share of it that nothing in front hides, tested at the centres of its
    9 sub-triangles inside the polygon and at a point of its clipped part; a face whose points disagree, or
    that faces in front overlap without hiding all of it, is split in 4 and tested again, down to a third
    of the median edge (at least MIN_CELL); where the faces over a piece lie wholly in front of it (or at
    that cell), the piece's hidden part is their projection. The back of a closed wall, box or tower, the
    ground under a stone, are never counted; a curved wall bulging between the drawn corners is, while its
    bulge is within the margin (a quarter of the diameter covers a cylinder drawn by its corners up to about
    +-0.9 rad from the middle, more for a tall patch). Exactly vertical faces count 0 in plan mode (from
    above they are not seen): measure them with a wall polygon.
    Returns ``surface``, ``plan`` (the clipped visible area in the projection used: horizontal, or the
    plane's), ``coverage`` (``plan`` / the polygon's area in that projection, at most 1), ``faces`` (faces with a
    non-empty visible clip) and ``projection``."""
    import shapely

    p = _pts(xyz, 3)
    centre, n = best_plane(p)
    tri = np.asarray(model.vertices, float)[np.asarray(model.faces, int)] - centre
    pc = p - centre
    if abs(n[2]) < STEEP_COS:
        mode = "plane"
        u, v = _plane_frame(n)
        axes = np.stack([u, v, n])
    else:
        mode = "plan"
        axes = np.eye(3)
    loc = tri @ axes.T  # (F, 3, 3): a, b, height along the axis
    poly_ab = pc @ axes[:2].T
    out = {"surface": 0.0, "plan": 0.0, "coverage": 0.0, "faces": 0, "projection": mode}
    poly = _valid(poly_ab)
    if poly.is_empty or poly.area == 0:
        return out
    cross = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    norm = np.linalg.norm(cross, axis=1)
    cosine = np.abs(cross @ axes[2]) / np.where(norm > 0, norm, 1.0)
    x0, y0, x1, y1 = poly.bounds
    a2 = loc[:, :, :2]
    keep = np.flatnonzero((a2[:, :, 0].max(axis=1) >= x0) & (a2[:, :, 0].min(axis=1) <= x1)
                          & (a2[:, :, 1].max(axis=1) >= y0) & (a2[:, :, 1].min(axis=1) <= y1)
                          & (norm > 0) & (cosine > 1e-6))
    if not len(keep):
        return out
    a2, h = a2[keep], loc[keep, :, 2]
    if mode == "plane":
        if toward is not None:
            side = 1.0 if (np.asarray(toward, float).reshape(3) - centre) @ n >= 0 else -1.0
        else:
            pt, _, hv = _covering(a2, h, poly_ab, eps=-1e-9)
            hp = pc @ n
            near = []
            for s in (1.0, -1.0):
                front = np.full(len(p), -np.inf)
                np.maximum.at(front, pt, s * hv)
                seen = np.isfinite(front)
                near.append(np.median(np.abs(front[seen] - s * hp[seen])) if seen.any() else np.inf)
            side = 1.0 if near[0] <= near[1] else -1.0
        h = side * h
    else:
        side = 1.0
    # look from just in front of the drawing: anything farther in front than this is neither measured nor
    # in the way (a shelter over the floor, a scaffold before the wall)
    hp = side * (pc @ axes[2])
    diameter = float(np.linalg.norm(poly_ab[:, None] - poly_ab[None], axis=2).max())
    limit = float(hp.max() + max(FRONT_MARGIN, float(np.ptp(hp)), DIAMETER_SHARE * diameter))
    edges = np.linalg.norm(a2 - np.roll(a2, 1, axis=1), axis=2)
    cell = max(float(np.median(edges)) / 3.0, MIN_CELL)
    tol = max(1e-9, 1e-9 * float(np.abs(loc[keep]).max()))
    bary = np.column_stack([1 - SAMPLES.sum(axis=1), SAMPLES])  # (9, 3)
    area = np.zeros(len(keep))
    # a piece of a face counts for the share of its tested points (the 9 sub-triangle centres inside the
    # polygon, plus a point of its clipped part when it crosses the outline) that are within the limit and
    # that no other face within the limit covers from closer to the viewer; a piece whose points disagree
    # is split in 4 and tested again, down to the cell
    all_shapes = shapely.make_valid(shapely.polygons(a2))
    tree = shapely.STRtree(all_shapes)
    h_max, h_min = h.max(axis=1), h.min(axis=1)
    t, th, face = a2, h, np.arange(len(keep))
    for _ in range(40):
        shapes = shapely.make_valid(shapely.polygons(t))
        clips = shapely.intersection(shapes, poly)
        clip_area = shapely.area(clips)
        live = clip_area > 0
        t, th, face, shapes, clips, clip_area = t[live], th[live], face[live], shapes[live], clips[live], clip_area[live]
        if not len(t):
            break
        border = np.flatnonzero(clip_area < shapely.area(shapes) * (1 - 1e-9))
        pts = np.einsum("sk,fkd->fsd", bary, t).reshape(-1, 2)
        pts_h = (th @ bary.T).reshape(-1)
        item = np.repeat(np.arange(len(t)), len(bary))
        use = np.ones(len(pts), bool)
        on_border = np.isin(item, border)
        use[on_border] = shapely.contains_xy(poly, pts[on_border, 0], pts[on_border, 1])
        extra = shapely.get_coordinates(shapely.point_on_surface(clips[border]))
        pts, pts_h, item = pts[use], pts_h[use], item[use]
        if len(border):
            tb = t[border]
            e1, e2, q = tb[:, 1] - tb[:, 0], tb[:, 2] - tb[:, 0], extra - tb[:, 0]
            det = e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]
            b1 = (q[:, 0] * e2[:, 1] - q[:, 1] * e2[:, 0]) / det
            b2 = (e1[:, 0] * q[:, 1] - e1[:, 1] * q[:, 0]) / det
            hb = th[border]
            extra_h = hb[:, 0] + b1 * (hb[:, 1] - hb[:, 0]) + b2 * (hb[:, 2] - hb[:, 0])
            pts, pts_h, item = np.vstack([pts, extra]), np.concatenate([pts_h, extra_h]), np.concatenate([item, border])
        pt, _, hv = _covering(a2, h, pts, exclude=face[item], eps=-1e-9)
        hidden = np.zeros(len(pts), bool)
        hidden[pt[(hv > pts_h[pt] + tol) & (hv <= limit)]] = True
        good = ~hidden & (pts_h <= limit)
        tested = np.bincount(item, minlength=len(t))
        share = np.bincount(item, weights=good, minlength=len(t)) / np.maximum(tested, 1)
        longest = np.linalg.norm(t - np.roll(t, 1, axis=1), axis=2).max(axis=1)
        unsure = (share > 0) & (share < 1)
        # the points can all miss a smaller face in front (a stone on a big floor triangle): a piece
        # overlapped by faces in front is split unless they hide all of it or none of its points is hidden
        ii, ff = tree.query(clips, predicate="intersects")
        front = (ff != face[ii]) & (h_max[ff] > th[ii].min(axis=1) + tol) & (h_min[ff] <= limit)
        ii, ff = ii[front], ff[front]
        over = shapely.area(shapely.intersection(clips[ii], all_shapes[ff])) > 1e-9 * clip_area[ii]
        ii, ff = ii[over], ff[over]
        overlapped = np.bincount(ii, minlength=len(t)) > 0
        unsure |= overlapped & (share == 1)
        # wholly in front (and within the limit) over all of the piece: the hidden part is exactly their
        # projection; so at the cell too, the best left there
        clear = np.bincount(ii, weights=(h_min[ff] <= th[ii].max(axis=1) + tol) | (h_max[ff] > limit),
                            minlength=len(t)) == 0
        for k in np.flatnonzero(overlapped & ((share == 0) | unsure)):
            cover = shapely.union_all(all_shapes[ff[ii == k]])
            seen_area = shapely.area(shapely.difference(clips[k], cover))
            if clear[k] or longest[k] <= cell:
                share[k], unsure[k] = seen_area / clip_area[k], False
            elif share[k] == 0 and seen_area > clip_area[k] * 1e-7:
                unsure[k] = True
        done = ~unsure | (longest <= cell)
        np.add.at(area, face[done], clip_area[done] * share[done])
        split = ~done
        if not split.any():
            break
        t, th, face = _split(t[split], th[split], face[split])
    w = 1.0 / cosine[keep]
    plan = float(area.sum())
    out.update(surface=float((area * w).sum()), plan=plan, coverage=min(1.0, plan / poly.area),
               faces=int((area > 0).sum()))
    return out


def _split(t, h, face):
    """Each triangle (and its heights) in 4 at the middles of its sides."""
    m = (t + np.roll(t, -1, axis=1)) / 2  # m0 = (0, 1), m1 = (1, 2), m2 = (2, 0)
    mh = (h + np.roll(h, -1, axis=1)) / 2
    tri = [(t[:, 0], m[:, 0], m[:, 2]), (m[:, 0], t[:, 1], m[:, 1]), (m[:, 2], m[:, 1], t[:, 2]),
           (m[:, 0], m[:, 1], m[:, 2])]
    hgt = [(h[:, 0], mh[:, 0], mh[:, 2]), (mh[:, 0], h[:, 1], mh[:, 1]), (mh[:, 2], mh[:, 1], h[:, 2]),
           (mh[:, 0], mh[:, 1], mh[:, 2])]
    return (np.concatenate([np.stack(c, axis=1) for c in tri]), np.concatenate([np.stack(c, axis=1) for c in hgt]),
            np.tile(face, 4))


def feature_measures(rec: dict, model=None, toward=None) -> dict:
    """Measures of a free-drawing record (``kind`` line | polygon | point, ``xyz`` in chunk coordinates);
    ``toward`` (a point on the viewer's side, e.g. the cameras' centre) goes to :func:`model_area_inside`.
    ValueError for a line under 2 points, a polygon under 3 or on one line, a point without one."""
    kind = rec["kind"]
    p = _pts(rec["xyz"], {"line": 2, "polygon": 3}.get(kind, 1))
    if kind == "line":
        return {"length_3d": length_3d(p), "length_plan": length_plan(p), "dz": float(p[-1, 2] - p[0, 2])}
    if kind == "polygon":
        slope, aspect = slope_aspect_of(p)
        out = {"perimeter_3d": length_3d(p, closed=True), "area_3d": area_3d(p), "area_plan": area_plan(p),
               "slope": slope, "aspect": aspect}
        if model is not None:
            out["model_surface"] = model_area_inside(model, p, toward=toward)["surface"]
        return out
    return {"z": float(p[0, 2])}
