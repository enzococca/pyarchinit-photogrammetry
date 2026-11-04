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
FACING = -0.05  # a face is looked at when its unit normal has at least this component along the view direction
EDGE_ON = 1e-6  # below this |cosine| a face is seen edge-on: its segment counts, not its (empty) projection
NEAREST = 32  # face centres shortlisted around a vertex before the exact distances


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


def _point_triangle_distance(p: np.ndarray, tri: np.ndarray) -> np.ndarray:
    """Distance from the point ``p`` (3,) to each triangle of ``tri`` (K, 3, 3): to the plane where the foot
    of the perpendicular falls inside the triangle, to the nearest side otherwise."""
    a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
    ab, ac, ap = b - a, c - a, p - a
    d1, d2, d3 = np.einsum("ij,ij->i", ab, ab), np.einsum("ij,ij->i", ab, ac), np.einsum("ij,ij->i", ac, ac)
    e1, e2 = np.einsum("ij,ij->i", ab, ap), np.einsum("ij,ij->i", ac, ap)
    det = d1 * d3 - d2 * d2
    ok = det > 0
    safe = np.where(ok, det, 1.0)
    v, w = (d3 * e1 - d2 * e2) / safe, (d1 * e2 - d2 * e1) / safe
    inside = ok & (v >= 0) & (w >= 0) & (v + w <= 1)
    normal = np.cross(ab, ac)
    nn = np.linalg.norm(normal, axis=1)
    plane = np.abs(np.einsum("ij,ij->i", ap, normal)) / np.where(nn > 0, nn, 1.0)
    best = np.full(len(tri), np.inf)
    for s, t in ((a, b), (b, c), (c, a)):
        d = t - s
        dd = np.einsum("ij,ij->i", d, d)
        u = np.clip(np.einsum("ij,ij->i", p - s, d) / np.where(dd > 0, dd, 1.0), 0.0, 1.0)
        best = np.minimum(best, np.linalg.norm(p - s - u[:, None] * d, axis=1))
    return np.where(inside, plane, best)


def _nearest_faces(points: np.ndarray, vertices: np.ndarray, faces: np.ndarray, idx: np.ndarray):
    """For each of ``points`` (P, 3) the face nearest to it among ``faces[idx]`` (index into ``faces``) and its
    distance: the NEAREST face centres are shortlisted, the exact distances taken on them, and the shortlist
    widened whenever a face with a farther centre could still come nearer (by its radius)."""
    tri = vertices[faces[idx]]
    centres = tri.mean(axis=1)
    r_max = float(np.linalg.norm(tri - centres[:, None], axis=2).max()) if len(tri) else 0.0
    best, dist = np.zeros(len(points), int), np.zeros(len(points))
    for i, p in enumerate(points):
        dc = np.linalg.norm(centres - p, axis=1)
        short = np.argpartition(dc, NEAREST - 1)[:NEAREST] if len(dc) > NEAREST else np.arange(len(dc))
        d = _point_triangle_distance(p, tri[short])
        j = int(np.argmin(d))
        k, dk = short[j], float(d[j])
        if len(dc) > NEAREST and dk > float(dc[short].max()) - r_max:
            wider = np.flatnonzero(dc <= dk + r_max)
            d = _point_triangle_distance(p, tri[wider])
            j = int(np.argmin(d))
            k, dk = wider[j], float(d[j])
        best[i], dist[i] = idx[k], dk
    return best, dist


def _segment_of(a2: np.ndarray) -> np.ndarray:
    """The two farthest apart of the three projected corners (K, 3, 2) -> (K, 2, 2): the segment a face seen
    edge-on projects to."""
    d = np.stack([np.linalg.norm(a2[:, 1] - a2[:, 0], axis=1), np.linalg.norm(a2[:, 2] - a2[:, 1], axis=1),
                  np.linalg.norm(a2[:, 0] - a2[:, 2], axis=1)])
    which = d.argmax(axis=0)
    k = np.arange(len(a2))
    return np.stack([a2[k, which], a2[k, (which + 1) % 3]], axis=1)


def _face_geoms(a2: np.ndarray, edge_on: np.ndarray, seg: np.ndarray):
    """Shapely geometries of the projected faces: polygons, segments for the faces seen edge-on."""
    import shapely

    g = np.empty(len(a2), object)
    if (~edge_on).any():
        g[~edge_on] = shapely.polygons(a2[~edge_on])
    if edge_on.any():
        g[edge_on] = shapely.linestrings(seg[edge_on])
    return g


def _adjacency(fa: np.ndarray, allowed: np.ndarray):
    """CSR neighbours (``indptr``, ``nbr``, ``flip``) of the faces ``fa`` (K, 3) through a shared edge, among the
    ``allowed`` faces; ``flip`` says the two faces wind the edge the same way, so the neighbour's normal is to be
    turned over for the two to agree. An edge shared by more faces links them in a chain."""
    k = len(fa)
    e = np.stack([fa[:, [0, 1]], fa[:, [1, 2]], fa[:, [2, 0]]], axis=1).reshape(-1, 2)
    face = np.repeat(np.arange(k), 3)
    lo, hi = e.min(axis=1), e.max(axis=1)
    good = (lo != hi) & allowed[face]
    key = lo[good].astype(np.int64) * (int(fa.max()) + 1) + hi[good]
    face, fwd = face[good], (e[good, 0] < e[good, 1])
    order = np.argsort(key, kind="stable")
    key, face, fwd = key[order], face[order], fwd[order]
    same = np.flatnonzero(key[1:] == key[:-1])
    f1, f2 = face[same], face[same + 1]
    flip = fwd[same] == fwd[same + 1]
    src, dst, fl = np.concatenate([f1, f2]), np.concatenate([f2, f1]), np.concatenate([flip, flip])
    order = np.argsort(src, kind="stable")
    return np.searchsorted(src[order], np.arange(k + 1)), dst[order], fl[order]


def _height_at(t: np.ndarray, h: np.ndarray, q: np.ndarray) -> np.ndarray:
    """Height of the plane of each projected triangle ``t`` (P, 3, 2) with corner heights ``h`` (P, 3) at the
    point ``q`` (P, 2) (barycentric)."""
    e1, e2, d = t[:, 1] - t[:, 0], t[:, 2] - t[:, 0], q - t[:, 0]
    det = e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]
    det = np.where(det != 0, det, 1.0)
    b1 = (d[:, 0] * e2[:, 1] - d[:, 1] * e2[:, 0]) / det
    b2 = (e1[:, 0] * d[:, 1] - e1[:, 1] * d[:, 0]) / det
    return h[:, 0] + b1 * (h[:, 1] - h[:, 0]) + b2 * (h[:, 2] - h[:, 0])


def _nearest_wins(geoms, tris, a2: np.ndarray, depth: np.ndarray, eps: float) -> np.ndarray:
    """Area of each clipped projection ``geoms`` (S,) once every overlap between them is given to the face nearer
    the viewer: ``tris`` the whole projected faces as polygons, ``a2`` (S, 3, 2) their corners, ``depth`` (S, 3)
    the corners' heights along the view. The pairs whose boxes meet come from an STRtree; the separating-axis
    test on the two triangles drops, with ``eps`` of tolerance, the pairs that only touch (every neighbour of a
    face in a sound mesh); the few that overlap are cut exactly, and the face lower at a point of the overlap
    loses it (an overhang hides the ground under it, a fold is counted once)."""
    import shapely

    area = shapely.area(geoms)
    if len(geoms) < 2:
        return area
    i, j = shapely.STRtree(tris).query(tris)
    pair = i < j
    i, j = i[pair], j[pair]
    if not len(i):
        return area
    ta, tb = a2[i], a2[j]
    apart = np.zeros(len(i), bool)
    for t in (ta, tb):
        for k in range(3):
            e = t[:, (k + 1) % 3] - t[:, k]
            axis = np.stack([-e[:, 1], e[:, 0]], axis=1)
            length = np.linalg.norm(axis, axis=1)
            real = length > 0
            axis /= np.where(real, length, 1.0)[:, None]
            pa, pb = np.einsum("pkd,pd->pk", ta, axis), np.einsum("pkd,pd->pk", tb, axis)
            apart |= real & ((pa.max(axis=1) <= pb.min(axis=1) + eps) | (pb.max(axis=1) <= pa.min(axis=1) + eps))
    i, j = i[~apart], j[~apart]
    if not len(i):
        return area
    over = shapely.intersection(geoms[i], geoms[j])
    real = shapely.area(over) > eps * eps
    i, j, over = i[real], j[real], over[real]
    if not len(i):
        return area
    q = shapely.get_coordinates(shapely.point_on_surface(over))
    loser = np.where(_height_at(a2[i], depth[i], q) >= _height_at(a2[j], depth[j], q), j, i)
    order = np.argsort(loser, kind="stable")
    loser, over = loser[order], over[order]
    who, start, count = np.unique(loser, return_index=True, return_counts=True)
    hidden = np.zeros(len(geoms))
    one = count == 1
    hidden[who[one]] = shapely.area(over[start[one]])
    for w, s, c in zip(who[~one], start[~one], count[~one]):
        hidden[w] = shapely.area(shapely.union_all(over[s:s + c]))
    return np.maximum(area - hidden, 0.0)


def model_area_inside(model, xyz, toward=None) -> dict:
    """Surface of the model that the polygon ``xyz`` was drawn on: the connected patch of its faces reached
    from the faces under the vertices.

    The faces are clipped by the polygon's vertical projection (per face, plan area / |n_z|); for a steep
    polygon (best plane with |n_z| < STEEP_COS, a wall) the same in the polygon's own plane, said by
    ``"projection": "plane"``, looked at along the plane's normal from the side of ``toward`` (a point, e.g.
    the cameras' centre) or, without it, from the side the faces under the vertices face (by area). Each
    vertex seeds the face nearest to it; from the seeds the patch grows through shared edges, into faces
    whose projection meets the footprint and that do not face away from the viewer (cosine with the view
    direction >= FACING: the vertical sides of a block pass, a ceiling, the underside of an overhang and the
    back of a closed object do not; the normals are made to agree along the way, so the winding of the
    mesh does not matter). Nothing in front of the drawing is tested: a roof over a floor or a screen before
    a wall is measured only if the mesh joins it to the drawn surface within the footprint; a curved wall
    drawn by its corners is followed whole. Where two faces of the patch overlap in the projection (the
    ground under a ledge reached through the ledge's front, a fold of a noisy mesh) the overlap goes to the
    face nearer the viewer, so the projected area never exceeds the footprint. A face seen edge-on
    (|cosine| < EDGE_ON, an exactly vertical face in plan) counts its 3D area by the share of its projected
    segment inside the footprint (on the outline: none).
    Returns ``surface``, ``plan`` (the clipped area in the projection used: horizontal, or the plane's;
    edge-on faces add nothing to it), ``coverage`` (``plan`` / the polygon's area in that projection, at most
    1), ``faces`` (faces with something counted) and ``projection``."""
    import shapely

    p = _pts(xyz, 3)
    centre, n = best_plane(p)
    if abs(n[2]) < STEEP_COS:
        mode = "plane"
        u, v = _plane_frame(n)
        axes = np.stack([u, v, n])
    else:
        mode = "plan"
        axes = np.eye(3)
    out = {"surface": 0.0, "plan": 0.0, "coverage": 0.0, "faces": 0, "projection": mode}
    pl = (p - centre) @ axes.T
    poly = _valid(pl[:, :2])
    faces = np.asarray(model.faces, int).reshape(-1, 3)
    if poly.is_empty or poly.area == 0 or not len(faces):
        return out
    vl = (np.asarray(model.vertices, float).reshape(-1, 3) - centre) @ axes.T
    x0, y0, x1, y1 = poly.bounds
    fx, fy = vl[faces, 0], vl[faces, 1]
    lo_x, hi_x, lo_y, hi_y = fx.min(axis=1), fx.max(axis=1), fy.min(axis=1), fy.max(axis=1)
    del fx, fy

    def within(margin: float) -> np.ndarray:
        return (hi_x >= x0 - margin) & (lo_x <= x1 + margin) & (hi_y >= y0 - margin) & (lo_y <= y1 + margin)

    under = within(0.0)
    # the seeds: the face nearest to each vertex, looked for under the footprint and, for a vertex farther from
    # those faces than nothing, among the faces within that distance of the footprint (the nearest face of
    # all is among them)
    search = np.flatnonzero(under) if under.any() else np.arange(len(faces))
    seed, dist = _nearest_faces(pl, vl, faces, search)
    far = dist > 1e-9 * max(1.0, x1 - x0, y1 - y0)
    if far.any() and len(search) < len(faces):
        wider = np.flatnonzero(within(float(dist[far].max())))
        if len(wider) > len(search):
            seed[far], dist[far] = _nearest_faces(pl[far], vl, faces, wider)
    if mode == "plane":
        if toward is not None:
            side = 1.0 if (np.asarray(toward, float).reshape(3) - centre) @ n >= 0 else -1.0
        else:
            st = vl[faces[seed]]
            side = 1.0 if np.cross(st[:, 1] - st[:, 0], st[:, 2] - st[:, 0])[:, 2].sum() >= 0 else -1.0
    else:
        side = 1.0
    is_seed = np.zeros(len(faces), bool)
    is_seed[seed] = True
    active = np.flatnonzero(under | is_seed)
    local = np.full(len(faces), -1)
    local[active] = np.arange(len(active))
    fa = faces[active]
    tri = vl[fa]
    nrm = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    nn = np.linalg.norm(nrm, axis=1)
    cosine = side * nrm[:, 2] / np.where(nn > 0, nn, 1.0)  # along the view, with the stored winding
    edge_on = np.abs(cosine) < EDGE_ON
    a2 = np.ascontiguousarray(tri[:, :, :2])
    seg = _segment_of(a2)
    seg_len = np.linalg.norm(seg[:, 1] - seg[:, 0], axis=1)
    inside3 = shapely.contains_xy(poly, a2[:, :, 0].ravel(), a2[:, :, 1].ravel()).reshape(-1, 3).all(axis=1)
    shapely.prepare(poly)
    meets = inside3.copy()
    need = ~inside3 & ~(edge_on & (seg_len == 0))
    if need.any():
        meets[need] = shapely.intersects(poly, _face_geoms(a2[need], edge_on[need], seg[need]))
    # the patch: from each seed (the most frontal first) through shared edges, into the faces meeting the
    # footprint that do not face away once their normal is made to agree with the face they are entered from
    indptr, nbr, flip = _adjacency(fa, meets | is_seed[active])
    roots = local[seed]
    roots = roots[np.sort(np.unique(roots, return_index=True)[1])]
    roots = roots[np.argsort(-np.abs(cosine[roots]), kind="stable")]
    kept, tested = np.zeros(len(active), bool), np.zeros(len(active), bool)
    orient = np.zeros(len(active), np.int8)
    for root in roots:
        if tested[root]:
            continue
        kept[root] = tested[root] = True
        orient[root] = -1 if cosine[root] < -EDGE_ON else 1  # a seed faces the viewer: the polygon was drawn on it
        frontier = np.array([root])
        while len(frontier):
            start, count = indptr[frontier], indptr[frontier + 1] - indptr[frontier]
            total = int(count.sum())
            if not total:
                break
            at = np.repeat(start - (np.cumsum(count) - count), count) + np.arange(total)
            nb = nbr[at]
            o = (orient[np.repeat(frontier, count)] * np.where(flip[at], -1, 1)).astype(np.int8)
            ok = ~kept[nb] & (cosine[nb] * o >= FACING)
            tested[nb] = True
            nb, first = np.unique(nb[ok], return_index=True)
            kept[nb] = True
            orient[nb] = o[ok][first]
            frontier = nb
    # the areas: the clipped projection / |cosine| of the faces seen face-on, exact where a face may cross the
    # outline, the overlaps between them (an overhang over the ground, a fold) counted once, for the nearer face;
    # the 3D area of the faces seen edge-on by the share of their segment inside the outline
    clip = np.zeros(len(active))
    solid = np.flatnonzero(kept & ~edge_on)
    if len(solid):
        g = shapely.polygons(a2[solid])
        if poly.geom_type == "Polygon" and poly.convex_hull.area <= poly.area * (1 + 1e-9):
            whole = inside3[solid]
        else:
            whole = shapely.contains_properly(poly, g)
        cg = g.copy()
        cg[~whole] = shapely.intersection(g[~whole], poly)
        clip[solid] = _nearest_wins(cg, g, a2[solid], side * tri[solid, :, 2], 1e-9 * max(x1 - x0, y1 - y0))
    surface = float((clip[solid] / np.abs(cosine[solid])).sum())
    counted = clip > 0
    sharp = np.flatnonzero(kept & edge_on & (seg_len > 0))
    if len(sharp):
        lines = shapely.linestrings(seg[sharp])
        inside = (shapely.length(shapely.intersection(lines, poly))
                  - shapely.length(shapely.intersection(lines, poly.boundary)))
        share = np.clip(inside / seg_len[sharp], 0.0, 1.0)
        surface += float((nn[sharp] / 2.0 * share).sum())
        counted[sharp] = share > 0
    plan = float(clip.sum())
    out.update(surface=surface, plan=plan, coverage=min(1.0, plan / poly.area), faces=int(counted.sum()))
    return out


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
