"""Orthophotos from the texture of the model, for chunks without photos (an imported textured model, a merged
model of :mod:`.model_merge`).

The orthophoto of a chunk with aligned photos is painted from the photos (:mod:`.ortho`); a chunk without them has
only the textured model. Here every cell of the grid sends rays from above (or from the viewer side of an elevation
frame, :mod:`.elevation`) to the model; the first face met gives the texture coordinates at that point (barycentric
interpolation of the corners of the face) and the texture page is sampled there (bilinear). Where the texture is
finer than the cell, several rays per cell (``supersample`` x ``supersample``) are averaged. Cells where no face is
met are transparent.

The grid is made one tile at a time and every tile written at once in the GeoTIFF (tiled, BigTIFF when needed,
overviews), so the size is limited by the disk, not by the memory, nor by the 32,767 pixels of OpenCV's ``remap``
(nothing here passes a whole raster to OpenCV). The texture pages are kept in a small cache (a merged model may
have many pages of 8192 px). No Qt here.
"""
from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from ..core.progress import Progress, as_progress

if TYPE_CHECKING:  # pragma: no cover
    from ..core.chunk import Chunk

log = logging.getLogger("pyarchinit_photogrammetry")

TILE = 1024
MAX_SUPERSAMPLE = 4
PAGES_IN_MEMORY = 4
MESSAGE_IT = "Il chunk non ha foto allineate: l'ortofoto è fatta dalla texture del modello"
MESSAGE_EN = "the chunk has no aligned photos: the orthophoto is made from the texture of the model"


def has_texture(chunk: "Chunk") -> bool:
    """Whether the model of ``chunk`` has a texture of pages beside it (not the OBJ of version 0.5)."""
    from .texturing import texture_pages

    pages = texture_pages(chunk)
    return bool(pages) and pages[0].suffix.lower() != ".obj" and all(p.exists() for p in pages)


def use_texture(chunk: "Chunk", asked: bool | None = None) -> bool:
    """Whether an orthophoto of ``chunk`` comes from the texture: ``asked`` True/False as said; None (auto) when the
    chunk has no aligned photo. ``RuntimeError`` with a clear message when the texture is needed and missing."""
    if asked is False:
        return False
    if asked is None and chunk.aligned_cameras:
        return False
    model = chunk.product_path("model")
    if model is None or not model.exists():
        raise RuntimeError("the chunk has neither aligned photos nor a model: an orthophoto needs one of them"
                           if asked is None else "build or import the model first: the orthophoto is made from its "
                                                 "texture")
    if not has_texture(chunk):
        raise RuntimeError("the chunk has no aligned photos and its model has no texture: import a textured model "
                           "or align the photos" if asked is None else "the model has no texture")
    return True


class TexturedModel:
    """The model of a chunk for ray casting with its texture: :meth:`colour` gives the RGBA seen along rays."""

    def __init__(self, chunk: "Chunk", max_pages: int = PAGES_IN_MEMORY):
        import open3d as o3d

        from ..io.mesh_ply import read_mesh
        from .texturing import texture_pages

        m = read_mesh(chunk.product_path("model"))
        self.chunk = chunk
        self.xyz = m.xyz
        self.faces = np.asarray(m.faces, np.int64)
        if m.texcoords is None:
            raise RuntimeError("the model has no texture coordinates")
        self.tex = np.asarray(m.texcoords, np.float32).reshape(-1, 3, 2)
        self.page = (np.asarray(m.texnumber, np.int32) if m.texnumber is not None
                     else np.zeros(len(self.faces), np.int32))
        self.pages = texture_pages(chunk)
        self.centre = self.xyz.mean(axis=0) if len(self.xyz) else np.zeros(3)
        self._o3d = o3d
        self.scene = o3d.t.geometry.RaycastingScene()
        self.scene.add_triangles(o3d.core.Tensor((self.xyz - self.centre).astype(np.float32)),
                                 o3d.core.Tensor(self.faces.astype(np.uint32)))
        self._cache: OrderedDict = OrderedDict()
        self._max = max(1, int(max_pages))
        self._lock = threading.Lock()

    def texel(self) -> float:
        """The size of a texel on the surface (world units, median over the faces): the default cell."""
        rng = np.random.default_rng(0)
        pick = rng.choice(len(self.faces), min(len(self.faces), 20000), replace=False)
        world = self.chunk.to_world(self.xyz)
        p = world[self.faces[pick]]
        area = 0.5 * np.linalg.norm(np.cross(p[:, 1] - p[:, 0], p[:, 2] - p[:, 0]), axis=1)
        sizes = []
        from PIL import Image

        for k, path in enumerate(self.pages):
            with Image.open(path) as im:
                w, h = im.size
            sel = self.page[pick] == k
            t = self.tex[pick[sel]].astype(np.float64) * [w, h]
            e1, e2 = t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]
            uv_area = 0.5 * np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0])
            ok = (uv_area > 1e-6) & (area[sel] > 0)
            sizes.append(np.sqrt(area[sel][ok] / uv_area[ok]))
        sizes = np.concatenate(sizes) if sizes else np.zeros(0)
        if not len(sizes):
            raise RuntimeError("the texture of the model covers no face")
        return float(np.median(sizes))

    def _image(self, k: int) -> np.ndarray:
        import cv2

        with self._lock:
            img = self._cache.get(k)
            if img is not None:
                self._cache.move_to_end(k)
                return img
        img = cv2.imread(str(self.pages[k]), cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError(f"cannot read the texture page {self.pages[k]}")
        with self._lock:
            self._cache[k] = img
            while len(self._cache) > self._max:
                self._cache.popitem(last=False)
        return img

    def cast(self, start_int: np.ndarray, dir_int: np.ndarray):
        """First hit along the rays (internal frame): distance (internal units, inf without a hit), face (-1) and
        the barycentric coordinates ``(u, v)`` of the hit on it."""
        o3d = self._o3d
        rays = np.hstack([start_int - self.centre, dir_int]).astype(np.float32)
        ans = self.scene.cast_rays(o3d.core.Tensor(rays))
        t = ans["t_hit"].numpy().astype(np.float64)
        face = ans["primitive_ids"].numpy().astype(np.int64)
        bary = ans["primitive_uvs"].numpy().astype(np.float64)
        miss = ~np.isfinite(t) | (face >= len(self.faces)) | (face < 0)
        face[miss] = -1
        t[miss] = np.inf
        return t, face, bary

    def colour(self, start_int: np.ndarray, dir_int: np.ndarray) -> np.ndarray:
        """RGBA (n, 4) float seen along the rays: the texture at the first hit, alpha 0 without one."""
        _t, face, bary = self.cast(start_int, dir_int)
        out = np.zeros((len(face), 4), np.float64)
        hit = face >= 0
        if not hit.any():
            return out
        f = face[hit]
        b1, b2 = bary[hit, 0], bary[hit, 1]
        t = self.tex[f].astype(np.float64)
        uv = (1.0 - b1 - b2)[:, None] * t[:, 0] + b1[:, None] * t[:, 1] + b2[:, None] * t[:, 2]
        rgb = np.zeros((len(f), 3))
        pages = self.page[f]
        for k in np.unique(pages):
            sel = pages == k
            rgb[sel] = _bilinear(self._image(int(k)), uv[sel])
        out[hit, :3] = rgb
        out[hit, 3] = 255.0
        return out


def _bilinear(img: np.ndarray, uv: np.ndarray) -> np.ndarray:
    """RGB of the BGR page ``img`` at texture coordinates ``uv`` (origin bottom left, texel centres at half
    integers), bilinear, the coordinates clamped to the page."""
    h, w = img.shape[:2]
    x = np.clip(uv[:, 0] * w - 0.5, 0.0, w - 1.0)
    y = np.clip((1.0 - uv[:, 1]) * h - 0.5, 0.0, h - 1.0)
    x0, y0 = np.floor(x).astype(np.int64), np.floor(y).astype(np.int64)
    x1, y1 = np.minimum(x0 + 1, w - 1), np.minimum(y0 + 1, h - 1)
    fx, fy = (x - x0)[:, None], (y - y0)[:, None]
    c = (img[y0, x0] * (1 - fx) * (1 - fy) + img[y0, x1] * fx * (1 - fy)
         + img[y1, x0] * (1 - fx) * fy + img[y1, x1] * fx * fy)
    return c[:, ::-1]


def render_tile(model: TexturedModel, rays, r0: int, c0: int, th: int, tw: int, k: int) -> np.ndarray:
    """RGBA uint8 ``(4, th, tw)`` of the cells ``r0:r0+th, c0:c0+tw``: ``rays(r, c)`` (fractional rows and columns
    of the grid, cell centres at half integers) gives the starts and directions in the internal frame; ``k`` x ``k``
    rays per cell averaged (transparent rays left out of the mean)."""
    acc = np.zeros((th * tw, 4))
    offs = (np.arange(k) + 0.5) / k
    rr0, cc0 = np.meshgrid(np.arange(th, dtype=np.float64), np.arange(tw, dtype=np.float64), indexing="ij")
    for dy in offs:
        for dx in offs:
            start, direction = rays(r0 + rr0.ravel() + dy, c0 + cc0.ravel() + dx)
            rgba = model.colour(start, direction)
            acc[:, :3] += rgba[:, :3] * (rgba[:, 3:] > 0)
            acc[:, 3] += rgba[:, 3] > 0
    seen = acc[:, 3] > 0
    out = np.zeros((th * tw, 4), np.uint8)
    out[seen, :3] = np.clip(np.rint(acc[seen, :3] / acc[seen, 3:]), 0, 255)
    out[:, 3] = np.where(acc[:, 3] >= 0.5 * k * k, 255, 0)  # a cell half covered or more is opaque
    return np.ascontiguousarray(out.reshape(th, tw, 4).transpose(2, 0, 1))


def top_grid(chunk: "Chunk", model: TexturedModel, resolution: float, bounds=None):
    """``origin`` (top left, world) and ``shape`` of the grid over the model seen from above (``bounds``
    ``(xmin, ymin, xmax, ymax)`` in the world: only that part)."""
    world = chunk.to_world(model.xyz)
    lo, hi = world.min(axis=0), world.max(axis=0)
    if bounds is not None:
        lo[:2] = np.maximum(lo[:2], bounds[:2])
        hi[:2] = np.minimum(hi[:2], bounds[2:4])
        if (hi[:2] <= lo[:2]).any():
            raise ValueError("the box does not meet the model")
    x0 = np.floor(lo[0] / resolution) * resolution
    y1 = np.ceil(hi[1] / resolution) * resolution
    shape = (max(1, int(np.ceil((y1 - lo[1]) / resolution - 1e-9))), max(1, int(np.ceil((hi[0] - x0) / resolution - 1e-9))))
    return (float(x0), float(y1)), shape, float(hi[2])


def top_rays(chunk: "Chunk", origin, resolution: float, top: float):
    """The ray maker of a grid seen from above: vertical rays (in the world) from above ``top``."""
    inv = np.linalg.inv(np.asarray(chunk.transform, float))
    down = inv[:3, :3] @ np.array([0.0, 0.0, -1.0])
    down = down / np.linalg.norm(down)
    z = float(top) + 1.0

    def rays(r, c):
        x = origin[0] + c * resolution
        y = origin[1] - r * resolution
        start = np.column_stack([x, y, np.full(len(x), z)]) @ inv[:3, :3].T + inv[:3, 3]
        return start, np.repeat(down[None], len(x), 0)

    return rays


def supersample_for(resolution: float, texel: float) -> int:
    return int(np.clip(np.ceil(resolution / max(texel, 1e-12) - 1e-6), 1, MAX_SUPERSAMPLE))


def write_tiles(path: Path, model: TexturedModel, rays, shape, transform, crs_wkt, k: int, progress: Progress,
                tile: int = TILE, tags: dict | None = None, start: float = 0.05, span: float = 0.9) -> None:
    """The grid rendered tile by tile into the GeoTIFF ``path`` (RGBA, tiled, BigTIFF when needed, overviews)."""
    import rasterio
    from rasterio.enums import ColorInterp, Resampling
    from rasterio.windows import Window

    h, w = shape
    profile = {"driver": "GTiff", "height": h, "width": w, "count": 4, "dtype": "uint8", "transform": transform,
               "compress": "deflate", "photometric": "RGB", "BIGTIFF": "IF_SAFER"}
    if h >= 256 and w >= 256:
        profile.update(tiled=True, blockxsize=256, blockysize=256)
    elif w >= 16:  # a strip: blocks of whole rows
        profile.update(tiled=False, blockysize=max(1, min(h, 64)))
    if crs_wkt:
        profile["crs"] = crs_wkt
    tiles = [(r, c) for r in range(0, h, tile) for c in range(0, w, tile)]
    with rasterio.open(path, "w", **profile) as dst:
        dst.colorinterp = [ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha]
        if tags:
            dst.update_tags(**tags)
        for i, (r0, c0) in enumerate(tiles):
            progress.check()
            th, tw = min(tile, h - r0), min(tile, w - c0)
            dst.write(render_tile(model, rays, r0, c0, th, tw, k), window=Window(c0, r0, tw, th))
            progress(start + span * (i + 1) / len(tiles), f"Orthophoto from the texture: tile {i + 1}/{len(tiles)}")
        factors = [f for f in (2, 4, 8, 16, 32, 64, 128) if min(h, w) // f >= 64]
        if factors:
            dst.build_overviews(factors, Resampling.average)


def build_orthomosaic(chunk: "Chunk", resolution: float | None = None, progress: Progress | None = None,
                      supersample: int | None = None, bounds=None, out_path: Path | None = None,
                      tile: int = TILE) -> Path:
    """The orthophoto of ``chunk`` from the texture of its model, seen from above (see the module): the product
    ``orthomosaic`` (as :func:`.ortho.build_orthomosaic` writes it: ``orthomosaic.tif`` RGBA in the chunk CRS,
    ``chunk.meta["orthomosaic"]`` with ``source`` "texture"), or the file ``out_path`` only. ``resolution``:
    the cell (default the texel of the texture); ``supersample``: rays per cell side (default: as many as texels
    fit in a cell, at most 4); ``bounds``: ``(xmin, ymin, xmax, ymax)`` in the world, only that part."""
    from rasterio.transform import from_origin

    from ..core import crs as crs_mod
    from ..core import stamps
    from ..io.files import replace_file

    progress = as_progress(progress)
    if not has_texture(chunk):
        raise RuntimeError("the model has no texture")
    progress(0.0, "Model and texture")
    model = TexturedModel(chunk)
    texel = model.texel()
    resolution = float(resolution or texel)
    if not (np.isfinite(resolution) and resolution > 0):
        raise ValueError("resolution: a positive number of metres")
    k = int(supersample) if supersample else supersample_for(resolution, texel)
    origin, shape, top = top_grid(chunk, model, resolution, None if bounds is None else np.asarray(bounds, float))
    h, w = shape
    progress(0.02, f"Orthophoto from the texture {w}x{h} px at {resolution:.4g} m/px ({k}x{k} rays per cell)")
    crs_wkt = None if crs_mod.is_local(chunk.crs) else crs_mod.to_pyproj(chunk.crs).to_wkt()
    product = out_path is None
    out_path = Path(out_path) if out_path is not None else chunk.data_dir / "orthomosaic.tif"
    tmp = out_path.with_name(out_path.stem + ".tmp.tif")
    try:
        write_tiles(tmp, model, top_rays(chunk, origin, resolution, top), shape,
                    from_origin(origin[0], origin[1], resolution, resolution), crs_wkt, k, progress, tile=tile)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if product:
        chunk._before_product_write("orthomosaic")
    replace_file(tmp, out_path)
    if not product:
        progress(1.0, f"Orthophoto {w}x{h} px")
        return out_path
    chunk._set_product("orthomosaic", out_path)
    stamps.inherit(chunk, "orthomosaic")
    chunk.meta["orthomosaic"] = {"resolution": resolution, "surface": "model", "blending": "texture",
                                 "source": "texture", "texel": texel, "supersample": k, "colour_correction": False,
                                 "ghosting": False, "fill_holes": False, "hidden_classes": None, "photos": 0}
    progress(1.0, f"Orthophoto {w}x{h} px")
    return out_path


def frame_rays(chunk: "Chunk", surface):
    """The ray maker of an elevation frame (:class:`.elevation.FrameSurface`): from the viewer side of the frame,
    at ``dmax``, towards it."""
    inv = np.linalg.inv(np.asarray(chunk.transform, float))
    res = surface.resolution

    def rays(r, c):
        u = surface.origin[0] + c * res
        v = surface.origin[1] - r * res
        start = surface.frame.point(u, v, np.full(len(u), surface.dmax))
        direction = -surface.frame.view(u, v)
        s = start @ inv[:3, :3].T + inv[:3, 3]
        d = direction @ inv[:3, :3].T
        return s, d / np.linalg.norm(d, axis=1, keepdims=True)

    return rays


__all__ = ["MESSAGE_EN", "MESSAGE_IT", "TexturedModel", "build_orthomosaic", "frame_rays", "has_texture",
           "render_tile", "supersample_for", "top_grid", "top_rays", "use_texture", "write_tiles"]
