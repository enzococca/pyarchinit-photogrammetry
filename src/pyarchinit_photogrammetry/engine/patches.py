"""Patches of the orthomosaic and of the model texture.

Inside a polygon drawn over the orthomosaic the colours come from photos chosen by hand
(``assign``) or from every photo but some (``exclude``): for trees, vehicles, people or
seamlines that the automatic choice of the photos renders badly - the editing of Metashape's
orthomosaic ("assign images" to a polygon), designed on this engine. A patch is a dict kept in
``chunk.meta["orthomosaic"]["patches"]``::

    {"id": "p1", "polygon": [[x, y], ...], "photos": ["DJI_0236"], "mode": "assign", "surface": "dem"}

(coordinates in the CRS of the chunk, photos by label; a later patch covers an earlier one).
``surface`` is where the cells are projected from: ``"dem"`` (a true orthophoto, the default) or
``"ground"``, the ground around the area - the heights of the surface along the polygon,
interpolated inside it - so that trees, vehicles and people come out as the photo shows them
instead of smeared over a DEM that does not fit them (and holes of the DEM are filled).
Every build of the orthomosaic applies the patches, and :func:`update_orthomosaic` redraws only
the cells of the patches added, changed or removed since the file was written. An orthomosaic
of another program (exported by Metashape) is never changed: the first update copies it into
the chunk folder, and the cells of a removed patch take its pixels again.

The texture of the model is corrected the same way: :func:`retexture` colours again, from the
chosen photos, the faces inside a polygon (in plan) or the faces chosen in the 3D view.
"""
from __future__ import annotations

import shutil
import threading
from contextlib import nullcontext
from pathlib import Path

import numpy as np

from ..core.chunk import Chunk
from ..core.progress import Progress, as_progress

MODES = ("assign", "exclude")
SURFACES = ("dem", "ground")


# ---------------------------------------------------------------- the patches of a chunk
def _geometry(polygon):
    """A valid shapely polygon from ``[[x, y], ...]``."""
    from shapely.geometry import Polygon

    xy = np.asarray(polygon, float)[:, :2]
    if len(xy) < 3:
        raise ValueError("a patch needs a polygon of at least three points")
    geom = Polygon(xy)
    if not geom.is_valid:
        geom = geom.buffer(0)
    if geom.is_empty or geom.area <= 0:
        raise ValueError("the polygon of the patch has no area")
    return geom


class _Patch:
    """A patch ready for the tiles: its polygon, and its photos among the readable ones."""

    def __init__(self, data: dict, cams):
        from shapely import prepare as prepare_geom

        self.data = data
        self.mode = data.get("mode", "assign")
        self.surface = data.get("surface", "dem")
        self.geom = _geometry(data["polygon"])
        prepare_geom(self.geom)
        self.bounds = self.geom.bounds
        self.ground = None  # (x, y, z) of the surface along the polygon, for surface "ground"
        labels = set(data.get("photos") or [])
        self.cams = [c for c in cams if c.label in labels or str(c.key) in labels]
        self.keys = {c.key for c in self.cams}

    def photos(self, near):
        """The photos of the cells of this patch, given the photos of the tile."""
        if self.mode == "exclude":
            return [c for c in near if c.key not in self.keys]
        return self.cams

    def ground_from(self, heights, resolution: float) -> None:
        """Sample the surface (``heights(x, y)``) along the polygon, every few cells."""
        from shapely import segmentize

        rings = [self.geom.exterior, *self.geom.interiors] if self.geom.geom_type == "Polygon" else \
            [r for g in self.geom.geoms for r in (g.exterior, *g.interiors)]
        step = max(4 * resolution, sum(r.length for r in rings) / 600)
        xy = np.vstack([np.asarray(segmentize(r, step).coords)[:-1, :2] for r in rings])
        z = heights(xy[:, 0], xy[:, 1])
        ok = np.isfinite(z)
        if ok.sum() >= 3:
            self.ground = (xy[ok, 0], xy[ok, 1], z[ok])

    def ground_heights(self, x, y) -> np.ndarray:
        """The ground inside the polygon: the heights along it, weighted by the inverse square
        distance (smooth, and equal to the surface along the polygon)."""
        bx, by, bz = self.ground
        out = np.empty(len(x))
        step = max(1, 4_000_000 // len(bx))
        for s in range(0, len(x), step):
            dx = x[s:s + step, None] - bx[None]
            dy = y[s:s + step, None] - by[None]
            w = 1.0 / np.maximum(dx * dx + dy * dy, 1e-12)
            out[s:s + step] = (w * bz).sum(axis=1) / w.sum(axis=1)
        return out


def prepare(chunk: Chunk, patches, cams) -> list[_Patch]:
    return [_Patch(dict(p), cams) for p in patches or []]


def _meta(chunk: Chunk) -> dict:
    return chunk.meta.setdefault("orthomosaic", {})


def patches(chunk: Chunk) -> list[dict]:
    """The patches of the orthomosaic of a chunk (oldest first)."""
    return [dict(p) for p in (chunk.meta.get("orthomosaic") or {}).get("patches") or []]


def add_patch(chunk: Chunk, polygon, photos, mode: str = "assign", surface: str = "dem") -> dict:
    """A new patch: inside ``polygon`` (CRS of the chunk) the colours come only from
    ``photos`` (labels) with ``mode="assign"``, from every photo but them with ``"exclude"``;
    ``surface`` "dem" or "ground" (see the module). Call :func:`update_orthomosaic` (or build
    the orthomosaic) to apply it."""
    if mode not in MODES:
        raise ValueError(f"mode: one of {', '.join(MODES)}")
    if surface not in SURFACES:
        raise ValueError(f"surface: one of {', '.join(SURFACES)}")
    labels = []
    for p in photos:
        labels.append(chunk.camera(p).label)  # KeyError for a photo that is not in the chunk
    if not labels:
        raise ValueError("choose at least one photo")
    geom = _geometry(polygon)
    ring = np.asarray(geom.exterior.coords if geom.geom_type == "Polygon" else polygon, float)[:, :2]
    used = [int(p["id"][1:]) for p in patches(chunk) if str(p.get("id", "")).startswith("p")
            and str(p["id"])[1:].isdigit()]
    patch = {"id": f"p{max(used, default=0) + 1}", "polygon": ring.round(4).tolist(), "photos": labels,
             "mode": mode, "surface": surface}
    _meta(chunk).setdefault("patches", []).append(patch)
    return dict(patch)


def remove_patch(chunk: Chunk, patch_id: str) -> None:
    """Take a patch away (:func:`update_orthomosaic` then redraws its cells without it)."""
    meta = _meta(chunk)
    before = meta.get("patches") or []
    meta["patches"] = [p for p in before if p.get("id") != patch_id]
    if len(meta["patches"]) == len(before):
        raise KeyError(patch_id)


def clear_patches(chunk: Chunk) -> None:
    _meta(chunk)["patches"] = []


# ---------------------------------------------------------------- updating the orthomosaic
def changed_patches(chunk: Chunk) -> list[dict]:
    """Patches added, changed or removed since the orthomosaic file was written."""
    meta = chunk.meta.get("orthomosaic") or {}
    current = [dict(p) for p in meta.get("patches") or []]
    applied = [] if _external(chunk) else [dict(p) for p in meta.get("applied_patches") or []]
    return [p for p in current if p not in applied] + [p for p in applied if p not in current]


def _external(chunk: Chunk) -> bool:
    """The active orthomosaic is a file of another program (outside the chunk folder)."""
    path = chunk.orthomosaic_path
    return path is not None and path.resolve() != chunk.own_product_path("orthomosaic").resolve()


def update_orthomosaic(chunk: Chunk, progress: Progress | None = None, workers: int | None = None,
                       skip_unreadable: bool = False) -> Path:
    """Redraw the cells of the patches added, changed or removed since the orthomosaic was
    written (the rest of the file is left as it is), then its overviews."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.windows import Window

    from .ortho import _context, _on_grid, _run_tiles, _surface, correction_for, stored_blending
    from .visibility import raycaster_for_chunk, readable_cameras

    progress = as_progress(progress)
    path = chunk.orthomosaic_path
    if path is None or not path.exists():
        raise RuntimeError("build the orthomosaic first")
    changed = changed_patches(chunk)
    meta = _meta(chunk)
    if not changed:
        progress(1.0, "Orthomosaic: no patch to apply")
        return path
    progress(0.0, "Checking the photos")
    cams, _bad = readable_cameras(chunk, skip_unreadable)
    own = chunk.own_product_path("orthomosaic")
    if _external(chunk):
        progress(0.005, f"Copy of {path.name} into the project (the original file is not changed)")
        tmp = own.with_name("orthomosaic.tmp.tif")
        shutil.copy2(path, tmp)
        tmp.replace(own)
        meta["base"] = str(path)
        chunk._set_product("orthomosaic", own)
    base = meta.get("base")
    if base and not Path(base).exists():
        base = None  # the original is gone: the cells of a removed patch are made again
    with rasterio.open(own) as src:
        t, h, w = src.transform, src.height, src.width
        factors = src.overviews(1)
    resolution = abs(t.a)
    progress(0.01, "Surface for the orthomosaic")
    raycaster = raycaster_for_chunk(chunk)
    lock = threading.Lock()
    surf = _on_grid(_surface(chunk, resolution, meta.get("surface", "dem"), raycaster, lock), (t.c, t.f),
                    (h, w), resolution)
    current = [dict(p) for p in meta.get("patches") or []]
    blending, sigma = stored_blending(meta)  # as the file was made, so that the patches match the rest
    ctx = _context(chunk, resolution, cams, surf, raycaster, blending,
                   meta.get("max_angle", 70.0), meta.get("max_cameras", 8), workers, meta.get("hidden_classes"),
                   current, 512, lock, sigma, correction_for(chunk, bool(meta.get("colour_correction"))))
    ctx.redo = [_geometry(p["polygon"]) for p in changed]
    ctx.base = base
    boxes = [g.bounds for g in ctx.redo]
    tile = ctx.tile
    tiles = []
    for r in range(0, h, tile):
        for c in range(0, w, tile):
            x0, y1 = t.c + c * resolution, t.f - r * resolution
            x1, y0 = x0 + tile * resolution, y1 - tile * resolution
            if any(b[0] <= x1 and b[2] >= x0 and b[1] <= y1 and b[3] >= y0 for b in boxes):
                tiles.append((r, c))
    progress(0.02, f"Orthomosaic: {len(changed)} patches changed, {len(tiles)} tiles")
    written = []
    with rasterio.Env(GDAL_NUM_THREADS="ALL_CPUS"), rasterio.open(own, "r+") as dst, \
            (rasterio.open(base) if base else nullcontext()) as bsrc:

        def put(r0, c0, result):
            rgba, done, from_base = result
            th, tw = done.shape
            win = Window(c0, r0, tw, th)
            cur = dst.read(window=win)
            if bsrc is not None and from_base.any():
                n = min(bsrc.count, 4)
                b = bsrc.read(list(range(1, n + 1)), window=win, boundless=True, fill_value=0)
                if n < 4:
                    b = np.concatenate([b, np.full((4 - n, th, tw), 255, np.uint8)])
                cur[:, from_base] = b[:4, from_base]
            cur[:, done] = rgba[:, done]
            dst.write(cur, window=win)
            written.append((c0, r0, tw, th))

        if tiles:
            _run_tiles(ctx, tiles, ctx.workers, progress, put, start=0.03, span=0.82)
    # the overviews of the changed tiles only (a minute for all of them on a 45,000 px orthomosaic)
    from . import overviews

    progress(0.86, "Overviews of the orthomosaic")
    if not (factors and (not written or overviews.refresh(own, written, factors))):
        factors = factors or [f for f in (2, 4, 8, 16, 32, 64, 128) if min(h, w) // f >= 64]
        if factors:
            with rasterio.Env(GDAL_NUM_THREADS="ALL_CPUS"), rasterio.open(own, "r+") as dst:
                dst.build_overviews(factors, Resampling.average)
    meta["applied_patches"] = current
    progress(1.0, f"Orthomosaic updated: {len(current)} patches")
    return own


# ---------------------------------------------------------------- choosing the photos
def _surface_kind(chunk: Chunk) -> str:
    return (chunk.meta.get("orthomosaic") or {}).get("surface", "dem")


def _area_grid(geom, cells: int):
    x0, y0, x1, y1 = geom.bounds
    res = max(x1 - x0, y1 - y0, 1e-9) / cells
    rows, cols = max(1, int(np.ceil((y1 - y0) / res - 1e-6))), max(1, int(np.ceil((x1 - x0) / res - 1e-6)))
    return (x0, y1), (rows, cols), res


def photos_for_area(chunk: Chunk, polygon, limit: int = 12, skip_unreadable: bool = True,
                    cells: int = 32) -> list[dict]:
    """The photos that frame the surface inside ``polygon``, best first (the share of the area
    framed times how well: close, from above, near the centre): ``{"label", "key", "coverage"
    (share of the area in the photo), "visible" (share of it not hidden by the model, e.g. the
    ground under trees), "angle" (degrees from the vertical), "gsd" (world units per pixel),
    "score", "used" (share of the area the orthomosaic takes from it now)}``. A patch with one
    photo takes it wherever the photo frames the area, hidden or not, so that a tree or a
    vehicle comes out whole, as that photo shows it."""
    from shapely import contains_xy

    from .ortho import _colour, _context, _on_grid, _surface, stored_blending
    from .visibility import project_valid, raycaster_for_chunk, readable_cameras

    geom = _geometry(polygon)
    cams, _bad = readable_cameras(chunk, skip_unreadable)
    raycaster = raycaster_for_chunk(chunk)
    lock = threading.Lock()
    origin, shape, res = _area_grid(geom, cells)
    surf = _on_grid(_surface(chunk, res, _surface_kind(chunk), raycaster, lock), origin, shape, res)
    z = surf.tile(0, 0, *shape)
    rows, cols = np.mgrid[0:shape[0], 0:shape[1]]
    X = origin[0] + (cols.ravel() + 0.5) * res
    Y = origin[1] - (rows.ravel() + 0.5) * res
    ok = contains_xy(geom, X, Y) & np.isfinite(z.ravel())
    if not ok.any():
        return []
    world = np.column_stack([X[ok], Y[ok], z.ravel()[ok]])
    meta = chunk.meta.get("orthomosaic") or {}
    ctx = _context(chunk, res, cams, surf, raycaster, stored_blending(meta)[0], meta.get("max_angle", 70.0),
                   meta.get("max_cameras", 8), 1, None, None, 512, lock)
    pts = world @ ctx.inv[:3, :3].T + ctx.inv[:3, 3]
    x0, y0, x1, y1 = geom.bounds
    near = [ctx.cams[i] for i in ctx.footprints.candidates((x0, y0, x1, y1), float(np.median(world[:, 2])))]
    _c, got, keys = _colour(ctx, pts, near, want_choice=True, colours=False)
    used = {int(k): float(np.mean(keys == k)) for k in np.unique(keys[got])}
    out = []
    for cam in near:
        px, depth, inside = project_valid(chunk, cam, pts)
        if not inside.any():
            continue
        visible = 1.0
        if raycaster is not None:
            idx = np.nonzero(inside)[0]
            visible = float(raycaster.visible_from(cam.center, pts[idx], abs_tol=ctx.abs_tol).mean())
        sensor = chunk.sensor(cam)
        ray = pts[inside] - cam.center
        ray /= np.linalg.norm(ray, axis=1, keepdims=True)
        nadir = np.clip(-(ray @ ctx.up_int), 0, 1)
        off = np.hypot(px[inside, 0] / sensor.width - 0.5, px[inside, 1] / sensor.height - 0.5)
        gsd = depth[inside] / sensor.focal_px * chunk.scale
        out.append({"label": cam.label, "key": cam.key, "coverage": float(inside.mean()), "visible": visible,
                    "angle": float(np.degrees(np.arccos(np.clip(np.mean(nadir), 0, 1)))),
                    "gsd": float(np.median(gsd)),
                    "score": float(np.mean((nadir ** 2) * (1 - off) / np.maximum(gsd, 1e-12))),
                    "used": used.get(int(cam.key), 0.0)})
    out.sort(key=lambda d: -d["coverage"] * d["score"])
    return out[:limit]


def preview_grid(polygon, max_side: int = 320, margin: float = 0.1):
    """The grid of the previews of an area: bounds (xmin, ymin, xmax, ymax) of ``polygon`` and
    ``margin`` around it, shape (rows, columns) with at most ``max_side`` cells a side, cell size."""
    geom = _geometry(polygon)
    x0, y0, x1, y1 = geom.bounds
    m = margin * max(x1 - x0, y1 - y0)
    x0, y0, x1, y1 = x0 - m, y0 - m, x1 + m, y1 + m
    res = max(x1 - x0, y1 - y0) / max_side
    shape = (max(1, int(np.ceil((y1 - y0) / res - 1e-6))), max(1, int(np.ceil((x1 - x0) / res - 1e-6))))
    return (x0, y1 - shape[0] * res, x0 + shape[1] * res, y1), shape, res


def preview_area(chunk: Chunk, polygon, photos=None, max_side: int = 320, margin: float = 0.1,
                 patches_too: bool = True) -> tuple[np.ndarray, tuple[float, float, float, float]]:
    """The orthophoto of the area of ``polygon`` (and ``margin`` around it) from ``photos``
    only (labels; None: the photos the orthomosaic would choose, with its patches), RGBA
    ``(h, w, 4)``, and its bounds (xmin, ymin, xmax, ymax)."""
    from .ortho import correction_for, render_area, stored_blending

    bounds, shape, res = preview_grid(polygon, max_side, margin)
    cams = None if photos is None else [chunk.camera(p) for p in photos]
    meta = chunk.meta.get("orthomosaic") or {}
    blending, sigma = stored_blending(meta)
    rgba = render_area(chunk, (bounds[0], bounds[3]), shape, res, cams=cams, surface=_surface_kind(chunk),
                       max_angle=89.0 if photos is not None else meta.get("max_angle", 70.0),
                       patches=patches(chunk) if (photos is None and patches_too) else None,
                       blending=blending, blend_sigma=sigma,
                       max_cameras=len(cams) if cams else meta.get("max_cameras", 8),
                       correction=correction_for(chunk, bool(meta.get("colour_correction"))))
    return np.moveaxis(rgba, 0, -1), bounds


def previews(chunk: Chunk, polygon, photos, max_side: int = 320, surfaces=SURFACES, progress=None):
    """What a patch of ``polygon`` would look like with each photo: ``({(label, surface): RGBA
    (h, w, 4)}, now, bounds)`` - inside the polygon the area from that photo alone, outside it
    the orthomosaic as it is (``now``, None without one), over :func:`preview_grid`. Quick: one
    photo frames every cell it sees, so no occlusion is tested."""
    from shapely import contains_xy

    from .ortho import _context, _on_grid, _PhotoCache, _surface, _tile, correction_for

    progress = progress or (lambda *_a, **_k: None)
    bounds, shape, res = preview_grid(polygon, max_side)
    now = current_area(chunk, bounds, shape)
    rows, cols = np.mgrid[0:shape[0], 0:shape[1]]
    inside = contains_xy(_geometry(polygon), bounds[0] + (cols + 0.5) * res, bounds[3] - (rows + 0.5) * res)
    kind = _surface_kind(chunk)
    raycaster = None
    if kind == "model" or chunk.dem_path is None:
        from .visibility import raycaster_for_chunk

        raycaster = raycaster_for_chunk(chunk)  # the surface itself is the model
    lock = threading.Lock()
    surf = _on_grid(_surface(chunk, res, kind, raycaster, lock), (bounds[0], bounds[3]), shape, res)
    cams = [chunk.camera(p) for p in photos]
    meta = chunk.meta.get("orthomosaic") or {}
    cache = _PhotoCache(len(cams) + 2, correction_for(chunk, bool(meta.get("colour_correction"))))
    out = {}
    jobs = [(cam, s) for cam in cams for s in surfaces]
    for i, (cam, s) in enumerate(jobs):
        progress(i / max(len(jobs), 1), f"Area vista da {cam.label}")
        patch = {"id": "anteprima", "polygon": np.asarray(polygon, float)[:, :2].tolist(), "photos": [cam.label],
                 "mode": "assign", "surface": s}
        ctx = _context(chunk, res, [cam], surf, raycaster, "disabled", 89.0, 1, 1, None, [patch],
                       max(shape), lock)  # one photo: nothing to blend
        ctx.cache, ctx.occlusion = cache, False
        block = _tile(ctx, 0, 0)
        rgba = np.zeros((*shape, 4), np.uint8) if block is None else np.moveaxis(block, 0, -1)
        img = (now.copy() if now is not None else np.zeros((*shape, 4), np.uint8))
        img[inside] = rgba[inside]
        out[(cam.label, s)] = img
    return out, now, bounds


def current_area(chunk: Chunk, bounds, shape) -> np.ndarray | None:
    """The orthomosaic of the project over ``bounds`` (xmin, ymin, xmax, ymax) at ``shape``,
    RGBA ``(h, w, 4)`` (None without orthomosaic)."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.windows import from_bounds

    path = chunk.orthomosaic_path
    if path is None or not path.exists():
        return None
    with rasterio.open(path) as src:
        win = from_bounds(*bounds, transform=src.transform)
        n = min(src.count, 4)
        arr = src.read(list(range(1, n + 1)), window=win, out_shape=(n, *shape), boundless=True, fill_value=0,
                       resampling=Resampling.average)
    if n < 4:
        arr = np.concatenate([arr, np.full((4 - n, *shape), 255, np.uint8)])
    return np.moveaxis(arr, 0, -1).astype(np.uint8)


# ---------------------------------------------------------------- the texture
def _texture_mesh(chunk: Chunk):
    from ..io.mesh_ply import read_mesh

    path = chunk.product_path("model")
    tex = chunk.product_path("model_textured")
    if path is None or not path.exists() or tex is None or not tex.exists():
        raise RuntimeError("build the texture of the model first")
    if tex.suffix.lower() not in (".jpg", ".jpeg", ".png", ".tif", ".tiff"):
        raise RuntimeError("the texture of this model is an old OBJ: build the texture again to correct it")
    mesh = read_mesh(path)
    if mesh.texcoords is None:
        raise RuntimeError("the model has no texture coordinates: build the texture again")
    return mesh, tex


def faces_in_polygon(chunk: Chunk, polygon, mesh=None) -> np.ndarray:
    """Faces of the model whose centre lies inside ``polygon`` seen from above (CRS)."""
    from shapely import contains_xy

    from ..io.mesh_ply import read_mesh

    geom = _geometry(polygon)
    if mesh is None:
        path = chunk.product_path("model")
        if path is None or not path.exists():
            raise RuntimeError("build the model first")
        mesh = read_mesh(path)
    x0, y0, x1, y1 = geom.bounds
    out = []
    step = 2_000_000
    for s in range(0, len(mesh.faces), step):
        f = mesh.faces[s:s + step]
        c = chunk.to_world(mesh.xyz[f].mean(axis=1))
        box = (c[:, 0] >= x0) & (c[:, 0] <= x1) & (c[:, 1] >= y0) & (c[:, 1] <= y1)
        idx = np.nonzero(box)[0]
        out.append(s + idx[contains_xy(geom, c[idx, 0], c[idx, 1])])
    return np.concatenate(out) if out else np.zeros(0, int)


def _texels(tri: np.ndarray, width: int, height: int, margin: float = 0.75):
    """Texels of triangles in the texture (pixel corners ``(k, 3, 2)``, x right, y down): the
    index of the triangle, the column and row of every texel whose centre is inside it (or less
    than ``margin`` px outside, so that no seam is left) and its barycentric coordinates."""
    lo = np.floor(tri.min(axis=1)).astype(np.int64) - 1
    hi = np.ceil(tri.max(axis=1)).astype(np.int64) + 1
    side = (hi - lo).max(axis=1)
    order = np.argsort(side, kind="stable")
    sorted_side = side[order]
    faces_out, cols_out, rows_out, bary_out = [], [], [], []
    start = 0
    while start < len(order):
        stop = int(np.searchsorted(sorted_side, 2 * sorted_side[start], side="right"))
        group = order[start:stop]
        n = int(side[group].max())
        gy, gx = np.mgrid[0:n, 0:n]
        batch = max(1, 1_000_000 // (n * n))
        for b0 in range(0, len(group), batch):
            f = group[b0:b0 + batch]
            a, b, c = tri[f, 0], tri[f, 1], tri[f, 2]
            v0, v1 = b - a, c - a
            d00, d01, d11 = (v0 * v0).sum(1), (v0 * v1).sum(1), (v1 * v1).sum(1)
            den = d00 * d11 - d01 ** 2
            good = np.abs(den) > 1e-12
            if not good.any():
                continue
            f, a, v0, v1, d00, d01, d11, den = f[good], a[good], v0[good], v1[good], d00[good], d01[good], \
                d11[good], den[good]
            cx = lo[f, 0][:, None, None] + gx[None] + 0.5
            cy = lo[f, 1][:, None, None] + gy[None] + 0.5
            px, py = cx - a[:, 0, None, None], cy - a[:, 1, None, None]
            d20 = px * v0[:, 0, None, None] + py * v0[:, 1, None, None]
            d21 = px * v1[:, 0, None, None] + py * v1[:, 1, None, None]
            v = (d11[:, None, None] * d20 - d01[:, None, None] * d21) / den[:, None, None]
            w = (d00[:, None, None] * d21 - d01[:, None, None] * d20) / den[:, None, None]
            u = 1.0 - v - w
            area2 = np.abs(v0[:, 0] * v1[:, 1] - v0[:, 1] * v1[:, 0])
            # the margin in barycentric units: margin / height of the triangle on each edge
            e_bc = np.linalg.norm(tri[f, 2] - tri[f, 1], axis=1)
            e_ca = np.linalg.norm(tri[f, 0] - tri[f, 2], axis=1)
            e_ab = np.linalg.norm(tri[f, 1] - tri[f, 0], axis=1)
            mu, mv, mw = (margin * e / np.maximum(area2, 1e-12) for e in (e_bc, e_ca, e_ab))
            inside = (u >= -mu[:, None, None]) & (v >= -mv[:, None, None]) & (w >= -mw[:, None, None])
            inside &= (cx >= 0) & (cx < width) & (cy >= 0) & (cy < height)
            k, r, q = np.nonzero(inside)
            if not len(k):
                continue
            bary = np.clip(np.stack([u[k, r, q], v[k, r, q], w[k, r, q]], 1), 0, None)
            bary /= np.maximum(bary.sum(1, keepdims=True), 1e-12)
            faces_out.append(f[k])
            cols_out.append(cx[k, r, q].astype(np.int64))
            rows_out.append(cy[k, r, q].astype(np.int64))
            bary_out.append(bary)
        start = stop
    if not faces_out:
        return np.zeros(0, int), np.zeros(0, int), np.zeros(0, int), np.zeros((0, 3))
    return np.concatenate(faces_out), np.concatenate(cols_out), np.concatenate(rows_out), np.concatenate(bary_out)


def photos_for_faces(chunk: Chunk, faces, limit: int = 12, skip_unreadable: bool = True) -> list[dict]:
    """The photos that see the faces ``faces`` of the model, best first (``label``, ``key``,
    ``coverage``, ``angle`` between the view and the faces, ``gsd``, ``score``)."""
    from ..io.mesh_ply import read_mesh
    from .visibility import Raycaster, project_valid, readable_cameras

    path = chunk.product_path("model")
    if path is None or not path.exists():
        raise RuntimeError("build the model first")
    mesh = read_mesh(path)
    faces = np.unique(np.asarray(faces, int))
    if len(faces) > 3000:  # a sample is enough to rank the photos
        faces = faces[np.linspace(0, len(faces) - 1, 3000).astype(int)]
    tri = mesh.xyz[mesh.faces[faces]]
    centre = tri.mean(axis=1)
    normal = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    normal /= np.maximum(np.linalg.norm(normal, axis=1, keepdims=True), 1e-20)
    cams, _bad = readable_cameras(chunk, skip_unreadable)
    rc = Raycaster(mesh.xyz, mesh.faces)
    out = []
    for cam in cams:
        px, depth, inside = project_valid(chunk, cam, centre)
        if not inside.any():
            continue
        to_cam = cam.center - centre
        dist = np.linalg.norm(to_cam, axis=1)
        cos = (normal * to_cam).sum(1) / np.maximum(dist, 1e-20)
        seen = inside & (cos > 0.1)
        if seen.any():
            idx = np.nonzero(seen)[0]
            pts = centre[idx] + normal[idx] * 1e-6 * dist[idx, None]
            seen[idx[~rc.visible_from(cam.center, pts)]] = False
        if not seen.any():
            continue
        sensor = chunk.sensor(cam)
        gsd = depth[seen] / sensor.focal_px * chunk.scale
        out.append({"label": cam.label, "key": cam.key, "coverage": float(seen.mean()),
                    "angle": float(np.degrees(np.arccos(np.clip(np.mean(cos[seen]), -1, 1)))),
                    "gsd": float(np.median(gsd)),
                    "score": float(np.mean(cos[seen] * (sensor.focal_px / dist[seen]) ** 2))})
    out.sort(key=lambda d: -d["coverage"] * d["score"])
    return out[:limit]


def preview_faces(chunk: Chunk, faces, photo, max_side: int = 320) -> np.ndarray | None:
    """The part of a photo where the faces ``faces`` are seen, with their outline, RGBA
    ``(h, w, 4)`` (None when the photo does not see them)."""
    import cv2

    from ..io.mesh_ply import read_mesh
    from .visibility import project_valid, read_photo

    cam = chunk.camera(photo)
    mesh = read_mesh(chunk.product_path("model"))
    faces = np.unique(np.asarray(faces, int))
    pts = mesh.xyz[mesh.faces[faces]].reshape(-1, 3)
    px, _depth, ok = project_valid(chunk, cam, pts, margin=0)
    if not ok.any():
        return None
    img = read_photo(cam)
    good = px[ok]
    (x0, y0), (x1, y1) = good.min(axis=0), good.max(axis=0)
    m = 0.15 * max(x1 - x0, y1 - y0, 16)
    x0, y0 = int(max(0, np.floor(x0 - m))), int(max(0, np.floor(y0 - m)))
    x1, y1 = int(min(img.shape[1], np.ceil(x1 + m))), int(min(img.shape[0], np.ceil(y1 + m)))
    crop = img[y0:y1, x0:x1].copy()
    tri = px.reshape(-1, 3, 2)
    seen = ok.reshape(-1, 3).all(axis=1)
    poly = [np.round(t - [x0 + 0.5, y0 + 0.5]).astype(np.int32) for t in tri[seen][:20000]]
    overlay = crop.copy()
    cv2.fillPoly(overlay, poly, (255, 0, 255))
    crop = cv2.addWeighted(overlay, 0.25, crop, 0.75, 0)
    k = max_side / max(crop.shape[:2])
    if k < 1:
        crop = cv2.resize(crop, (max(1, int(crop.shape[1] * k)), max(1, int(crop.shape[0] * k))),
                          interpolation=cv2.INTER_AREA)
    rgba = np.dstack([crop[..., ::-1], np.full(crop.shape[:2], 255, np.uint8)])
    return rgba


def retexture(chunk: Chunk, photos, faces=None, polygon=None, mode: str = "assign",
              progress: Progress | None = None, skip_unreadable: bool = True) -> dict:
    """Colour again the faces ``faces`` of the textured model (or those inside ``polygon``
    seen from above) from ``photos`` (labels; ``mode="exclude"``: from every photo but
    them). Every face takes the best of those photos that sees it; texels no chosen photo sees
    keep their colour. The texture image is written in the chunk folder (a texture of another
    program is copied first). Returns ``{"faces", "coloured", "texels"}``."""
    import cv2

    from ..io.mesh_ply import write_mesh
    from .texturing import _face_camera_choice
    from .visibility import ImageCache, Raycaster, project_valid, readable_cameras, sample

    progress = as_progress(progress)
    if mode not in MODES:
        raise ValueError(f"mode: one of {', '.join(MODES)}")
    mesh, tex = _texture_mesh(chunk)
    if faces is None:
        if polygon is None:
            raise ValueError("give the faces or a polygon")
        faces = faces_in_polygon(chunk, polygon, mesh=mesh)
    faces = np.unique(np.asarray(faces, int))
    if not len(faces):
        raise ValueError("no face of the model in the area")
    labels = {chunk.camera(p).label for p in photos}
    cams_all, _bad = readable_cameras(chunk, skip_unreadable)
    cams = [c for c in cams_all if (c.label in labels) == (mode == "assign")]
    if not cams:
        raise ValueError("none of these photos can be used")
    verts, tris = mesh.xyz, mesh.faces
    progress(0.02, f"Texture: {len(faces):,} faces from {len(cams)} photos")
    raycaster = Raycaster(verts, tris)
    choice = _face_camera_choice(chunk, verts, tris[faces], raycaster, progress.sub(0.02, 0.38), cams=cams)
    atlas = cv2.imread(str(tex), cv2.IMREAD_COLOR)
    if atlas is None:
        raise RuntimeError(f"cannot read the texture {tex}")
    height, width = atlas.shape[:2]
    uv = mesh.texcoords[faces].reshape(-1, 3, 2).astype(np.float64)
    tri_px = np.stack([uv[..., 0] * width, (1.0 - uv[..., 1]) * height], -1)
    progress(0.42, "Texels of the faces")
    fi, cols, rows, bary = _texels(tri_px, width, height)
    from .ortho import correction_for

    cache = ImageCache(chunk, 3, correction_for(chunk, bool((chunk.meta.get("texture") or {}).get("colour_correction"))))
    used = np.unique(choice[choice >= 0])
    written = 0
    for done, k in enumerate(used):
        sel = np.nonzero(choice[fi] == k)[0]
        pts = (bary[sel, :, None] * verts[tris[faces[fi[sel]]]]).sum(axis=1)
        px, _d, ok = project_valid(chunk, cams[k], pts, margin=0)
        if ok.any():
            col = sample(cache.get(cams[k]), px[ok]).astype(np.float64)
            atlas[rows[sel[ok]], cols[sel[ok]]] = np.clip(col + 0.5, 0, 255).astype(np.uint8)
            written += int(ok.sum())
        progress(0.45 + 0.5 * (done + 1) / max(len(used), 1), f"Texture from {cams[k].label}")
    own = chunk.data_dir / "model_texture.jpg"
    out = tex if tex.resolve().parent == chunk.data_dir.resolve() else own
    ext = out.suffix.lower()
    params = [cv2.IMWRITE_JPEG_QUALITY, 95] if ext in (".jpg", ".jpeg") else []
    tmp = out.with_name(out.stem + ".tmp" + out.suffix)
    if not cv2.imwrite(str(tmp), atlas, params):
        raise RuntimeError(f"cannot write {out}")
    tmp.replace(out)
    if out != tex:  # a texture of another program: the model now points at the copy
        mesh.set_texture_file(out.name)
        chunk._set_product("model", write_mesh(chunk.own_product_path("model"), mesh))
        chunk._set_product("model_textured", out)
    record = {"faces": int(len(faces)), "photos": sorted(labels), "mode": mode,
              "polygon": None if polygon is None else np.asarray(polygon, float)[:, :2].round(4).tolist()}
    chunk.meta.setdefault("texture", {}).setdefault("patches", []).append(record)
    progress(1.0, f"Texture corrected: {len(faces):,} faces")
    return {"faces": int(len(faces)), "coloured": int((choice >= 0).sum()), "texels": written}


__all__ = ["MODES", "SURFACES", "add_patch", "changed_patches", "clear_patches", "current_area", "faces_in_polygon",
           "patches", "photos_for_area", "photos_for_faces", "preview_area", "preview_faces", "preview_grid", "previews",
           "remove_patch", "retexture", "update_orthomosaic"]
