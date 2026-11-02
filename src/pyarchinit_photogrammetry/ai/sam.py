"""Segment Anything 2.1 (SAM 2.1) for orthomosaics, photos and point clouds.

SAM 2.1 is optional: ``pip install torch sam2`` (or the ``[sam]`` extra). Weights
are downloaded from the Hugging Face hub on first use (``facebook/sam2.1-hiera-*``)
or loaded from a local checkpoint. Apple Silicon (MPS), CUDA and CPU are supported.

Any object with the same three methods (``segment_points``, ``segment_box``,
``segment_auto``) can replace :class:`Sam2Segmenter`; this keeps the pipeline
testable and lets other models (e.g. YOLOE, SAM 3) plug in.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

import numpy as np

from ..archaeo.stratigraphy import UnitPolygon, mask_to_polygons

MODELS = {
    "tiny": "facebook/sam2.1-hiera-tiny",
    "small": "facebook/sam2.1-hiera-small",
    "base_plus": "facebook/sam2.1-hiera-base-plus",
    "large": "facebook/sam2.1-hiera-large",
}


class Segmenter(Protocol):
    def segment_points(self, image: np.ndarray, points: np.ndarray, labels: np.ndarray) -> np.ndarray: ...

    def segment_box(self, image: np.ndarray, box: np.ndarray) -> np.ndarray: ...

    def segment_auto(self, image: np.ndarray) -> list[np.ndarray]: ...


def best_device() -> str:
    import os

    os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    import torch

    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


class Sam2Segmenter:
    """Thin wrapper around ``sam2`` image predictor and automatic mask generator."""

    def __init__(self, model: str = "small", checkpoint: str | Path | None = None, config: str | None = None,
                 device: str | None = None):
        try:
            import torch  # noqa: F401
            from sam2.sam2_image_predictor import SAM2ImagePredictor
        except ImportError as e:
            raise ImportError("SAM 2.1 needs PyTorch and sam2: pip install 'pyarchinit-photogrammetry[sam]'") from e
        self.device = device or best_device()
        if checkpoint:
            from sam2.build_sam import build_sam2

            cfg = config or f"configs/sam2.1/sam2.1_hiera_{ {'tiny': 't', 'small': 's', 'base_plus': 'b+', 'large': 'l'}[model] }.yaml"
            self._model = build_sam2(cfg, str(checkpoint), device=self.device)
            self.predictor = SAM2ImagePredictor(self._model)
        else:
            self.predictor = SAM2ImagePredictor.from_pretrained(MODELS.get(model, model), device=self.device)
            self._model = self.predictor.model
        self._image_id = None

    def _set(self, image: np.ndarray) -> None:
        key = (image.shape, int(image[::97, ::97].sum()))
        if key != self._image_id:
            self.predictor.set_image(image)
            self._image_id = key

    def segment_points(self, image, points, labels):
        import torch

        self._set(image)
        with torch.inference_mode():
            masks, scores, _ = self.predictor.predict(point_coords=np.asarray(points, float),
                                                      point_labels=np.asarray(labels, int), multimask_output=True)
        return masks[int(np.argmax(scores))].astype(bool)

    def segment_box(self, image, box):
        import torch

        self._set(image)
        with torch.inference_mode():
            masks, scores, _ = self.predictor.predict(box=np.asarray(box, float)[None], multimask_output=False)
        return masks[0].astype(bool)

    def segment_points_box(self, image, points, labels, box):
        """A box with points that add (1) or remove (0) parts of the object."""
        import torch

        self._set(image)
        with torch.inference_mode():
            masks, scores, _ = self.predictor.predict(point_coords=np.asarray(points, float),
                                                      point_labels=np.asarray(labels, int),
                                                      box=np.asarray(box, float)[None], multimask_output=False)
        return masks[0].astype(bool)

    def segment_auto(self, image, points_per_side: int = 32, min_area: int = 500, **options):
        """Every object of the image. ``options`` go to ``SAM2AutomaticMaskGenerator``
        (``crop_n_layers``, ``pred_iou_thresh``, ``stability_score_thresh``, ``use_m2m``...)."""
        import torch
        from sam2.automatic_mask_generator import SAM2AutomaticMaskGenerator

        gen = SAM2AutomaticMaskGenerator(self._model, points_per_side=points_per_side, min_mask_region_area=min_area,
                                         **options)
        with torch.inference_mode():
            res = gen.generate(image)
        return [r["segmentation"].astype(bool) for r in sorted(res, key=lambda r: -r["area"])]


# ------------------------------------------------------------ orthomosaic
# how thoroughly the automatic mode looks for objects: SAM2AutomaticMaskGenerator settings
THOROUGHNESS = {
    "normale": {"points_per_side": 32},
    "alta": {"points_per_side": 32, "crop_n_layers": 1, "pred_iou_thresh": 0.75, "stability_score_thresh": 0.9},
    "massima": {"points_per_side": 48, "crop_n_layers": 1, "pred_iou_thresh": 0.7, "stability_score_thresh": 0.88,
                "use_m2m": True},
}


def _world_to_pixel(transform, xy: np.ndarray) -> np.ndarray:
    """World XY -> fractional (col, row) for north-up rasters (pixel corners on integers)."""
    xy = np.asarray(xy, float)
    return np.column_stack([(xy[:, 0] - transform.c) / transform.a, (xy[:, 1] - transform.f) / transform.e])


def read_orthomosaic(path, bounds, max_side: int | None = None, resolution: float | None = None):
    """The orthomosaic inside world ``bounds`` (xmin, ymin, xmax, ymax): RGB uint8 (H, W, 3),
    valid-data mask and transform, resampled (from the overviews) so that the long side is at
    most ``max_side`` pixels or the pixel is ``resolution`` metres; never finer than the
    source. Only that window is read: a whole drone orthomosaic does not fit in memory."""
    import rasterio
    from affine import Affine
    from rasterio.enums import Resampling
    from rasterio.windows import Window

    with rasterio.open(path) as src:
        inv = ~src.transform
        cols, rows = zip(*((inv.a * x + inv.b * y + inv.c, inv.d * x + inv.e * y + inv.f)
                           for x, y in ((bounds[0], bounds[3]), (bounds[2], bounds[1]))))
        c0, c1 = int(np.floor(max(min(cols), 0))), int(np.ceil(min(max(cols), src.width)))
        r0, r1 = int(np.floor(max(min(rows), 0))), int(np.ceil(min(max(rows), src.height)))
        if c1 <= c0 or r1 <= r0:
            raise ValueError("the area is outside the orthomosaic")
        w, h = c1 - c0, r1 - r0
        f = 1.0
        if resolution:
            f = max(f, resolution / abs(src.res[0]))
        if max_side:
            f = max(f, max(w, h) / max_side)
        ow, oh = max(1, int(round(w / f))), max(1, int(round(h / f)))
        win = Window(c0, r0, w, h)
        rs = Resampling.average if f > 1.0 else Resampling.nearest
        rgb = src.read([1, 2, 3], window=win, out_shape=(3, oh, ow), resampling=rs)
        if src.count >= 4:
            valid = src.read(4, window=win, out_shape=(oh, ow), resampling=Resampling.nearest) > 0
        else:
            valid = src.read_masks(1, window=win, out_shape=(oh, ow), resampling=Resampling.nearest) > 0
        tr = src.window_transform(win) @ Affine.scale(w / ow, h / oh)
    return np.ascontiguousarray(np.moveaxis(rgb, 0, -1)), valid, tr


def segment_orthomosaic(chunk, segmenter: Segmenter, points=None, labels=None, box=None, window: int = 2048,
                        simplify: float | None = None, us: str | None = None, area: str | None = None,
                        bounds=None, max_side: int = 1536) -> UnitPolygon:
    """Segment one unit on the orthomosaic from world-coordinate prompts.

    ``points``: (N,2) world XY with ``labels`` (1 = inside, 0 = outside); and/or ``box`` =
    (xmin, ymin, xmax, ymax) world. With ``bounds`` (e.g. the part of the orthomosaic shown
    on screen) SAM sees that area, reduced to ``max_side`` pixels: what is visible can be
    segmented at any zoom, and keeping the same bounds while adding points reuses the image
    already encoded. Without it, a ``window`` of pixels at full resolution around the prompts.
    """
    path = chunk.orthomosaic_path
    if path is None:
        raise RuntimeError("build the orthomosaic first")
    prompts = [np.asarray(points, float).reshape(-1, 2)] if points is not None else []
    if box is not None:
        prompts.append(np.reshape(np.asarray(box, float), (2, 2)))
    prompt_xy = np.vstack(prompts)
    if bounds is None:
        import rasterio

        with rasterio.open(path) as src:
            px = _world_to_pixel(src.transform, prompt_xy)
            c0 = int(np.clip(px[:, 0].mean() - window / 2, 0, max(src.width - window, 0)))
            r0 = int(np.clip(px[:, 1].mean() - window / 2, 0, max(src.height - window, 0)))
            t = src.transform
            bounds = (t.c + c0 * t.a, t.f + (r0 + min(window, src.height)) * t.e,
                      t.c + (c0 + min(window, src.width)) * t.a, t.f + r0 * t.e)
        img, valid, wtr = read_orthomosaic(path, bounds)
    else:
        img, valid, wtr = read_orthomosaic(path, bounds, max_side=max_side)
    local = _world_to_pixel(wtr, prompt_xy) - 0.5  # corner-based -> pixel-centre coordinates expected by SAM
    if points is not None:
        n = len(prompts[0])
        lab = np.ones(n) if labels is None else np.asarray(labels)
        if box is not None and hasattr(segmenter, "segment_points_box"):
            mask = segmenter.segment_points_box(img, local[:n], lab, local[n:].ravel())
        else:
            mask = segmenter.segment_points(img, local[:n], lab)
    else:
        mask = segmenter.segment_box(img, local.ravel())
    res = abs(wtr.a)
    geom = mask_to_polygons(mask & valid, wtr, simplify=simplify if simplify is not None else 2 * res,
                            min_area=(10 * res) ** 2)
    return UnitPolygon(geom, us=us, area=area)


def _tiles(bounds, res: float, tile: int, overlap: int):
    """World bounds of square tiles of ``tile`` pixels at ``res`` overlapping by ``overlap`` px."""
    x0, y0, x1, y1 = bounds
    size, step = tile * res, (tile - overlap) * res
    nx = max(1, int(np.ceil(max(x1 - x0 - size, 0) / step)) + 1)
    ny = max(1, int(np.ceil(max(y1 - y0 - size, 0) / step)) + 1)
    for j in range(ny):
        for i in range(nx):
            tx0 = min(x0 + i * step, max(x1 - size, x0))
            ty1 = max(y1 - j * step, min(y0 + size, y1))
            yield (tx0, max(ty1 - size, y0), min(tx0 + size, x1), ty1)


def _segment_auto(segmenter, img, options: dict):
    try:
        return segmenter.segment_auto(img, **options)
    except TypeError:  # a segmenter without the options (custom models, tests)
        return segmenter.segment_auto(img)


def auto_units(chunk, segmenter: Segmenter, max_side: int = 2048, simplify: float | None = None,
               min_area_m2: float = 0.01, bounds=None, resolution: float | None = None, tile: int = 1024,
               overlap: int = 128, thoroughness: str = "normale", max_tiles: int = 64, exclusive: bool = True,
               progress=None, path=None, sam_options: dict | None = None) -> list[UnitPolygon]:
    """Automatic segmentation of the orthomosaic (or of world ``bounds``) into candidate
    units (no US yet), scanning it in overlapping tiles of ``tile`` pixels at ``resolution``
    (m/px; default: the finest that keeps at most ``max_tiles`` tiles), so that SAM sees the
    details everywhere instead of a whole drone orthomosaic squeezed into one image.

    Objects cut by a tile border are joined with their other part from the next tile (they
    agree where the tiles overlap); duplicates are dropped. With ``exclusive`` the result is
    a partition: smaller units are kept whole and cut out of the larger ones (stones out of
    a layer), as on a stratigraphic plan. ``thoroughness``: see :data:`THOROUGHNESS`.
    ``path``: another raster than the orthomosaic of ``chunk`` (the orthophoto of an elevation,
    in its own coordinates); ``chunk`` may then be None. ``sam_options`` go to SAM's automatic
    mode over those of the thoroughness (``points_per_side``, ``min_area``...)."""
    import rasterio
    from shapely.strtree import STRtree

    from ..core.progress import as_progress

    progress = as_progress(progress)
    path = path or (chunk.orthomosaic_path if chunk is not None else None)
    if path is None:
        raise RuntimeError("build the orthomosaic first")
    with rasterio.open(path) as src:
        native = abs(src.res[0])
        b = src.bounds
        full = (b.left, b.bottom, b.right, b.top)
    bx = full if bounds is None else (max(bounds[0], full[0]), max(bounds[1], full[1]),
                                      min(bounds[2], full[2]), min(bounds[3], full[3]))
    if bx[2] <= bx[0] or bx[3] <= bx[1]:
        raise ValueError("the area is outside the orthomosaic")
    if resolution is None:  # the finest pixel that keeps the number of tiles within max_tiles
        side = max(bx[2] - bx[0], bx[3] - bx[1])
        per_axis = max(1, int(np.floor(np.sqrt(max_tiles))))
        resolution = max(native, side / (per_axis * (tile - overlap) + overlap))
        if bounds is None and max_side and side / resolution < max_side:
            resolution = max(native, side / max_side)  # small orthomosaics: as before, one image
    res = max(float(resolution), native)
    options = {**THOROUGHNESS.get(thoroughness, THOROUGHNESS["normale"]), **(sam_options or {})}
    tiles = list(_tiles(bx, res, tile, overlap))
    found = []  # (tile index, polygon)
    for k, tb in enumerate(tiles):
        progress(0.9 * k / len(tiles), f"SAM: tassello {k + 1} di {len(tiles)}")
        try:
            img, valid, tr = read_orthomosaic(path, tb, resolution=res)
        except ValueError:
            continue
        if valid.mean() < 0.02:
            continue
        r = abs(tr.a)
        for m in _segment_auto(segmenter, img, options):
            m = m & valid
            if m.sum() * r * r < min_area_m2:
                continue
            geom = mask_to_polygons(m, tr, simplify=simplify if simplify is not None else 1.5 * r)
            for g in getattr(geom, "geoms", [geom]):
                if not g.is_empty and g.area >= min_area_m2:
                    found.append((k, g.buffer(0)))
    progress(0.92, f"SAM: unione di {len(found)} oggetti tra i tasselli")
    geoms = [g for _, g in found]
    owner = [k for k, _ in found]
    parent = list(range(len(geoms)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    from shapely.geometry import box as sbox
    from shapely.ops import unary_union

    tree = STRtree(geoms) if geoms else None
    for i, g in enumerate(geoms):
        for j in tree.query(g, predicate="intersects") if tree is not None else []:
            j = int(j)
            if j <= i or owner[i] == owner[j]:
                continue
            ta, tb = tiles[owner[i]], tiles[owner[j]]
            common = sbox(max(ta[0], tb[0]), max(ta[1], tb[1]), min(ta[2], tb[2]), min(ta[3], tb[3]))
            if common.is_empty:
                continue
            a, c = g.intersection(common), geoms[j].intersection(common)
            if a.is_empty or c.is_empty:
                continue
            if a.intersection(c).area > 0.5 * min(a.area, c.area):  # the same object seen by both tiles
                parent[root(j)] = root(i)
    groups: dict[int, list] = {}
    for i, g in enumerate(geoms):
        groups.setdefault(root(i), []).append(g)
    merged = sorted((unary_union(gs).buffer(0) for gs in groups.values()), key=lambda g: g.area)
    kept = np.zeros(len(merged), bool)
    tree = STRtree(merged) if merged else None
    for i, g in enumerate(merged):  # duplicates: masks of the same object at different granularities
        dup = False
        for j in tree.query(g, predicate="intersects") if tree is not None else []:
            h = merged[int(j)]
            if int(j) < i and kept[int(j)] and abs(g.area - h.area) < 0.15 * g.area \
                    and g.intersection(h).area > 0.85 * h.area:
                dup = True
                break
        kept[i] = not dup
    final: dict[int, object] = {}
    for i in np.nonzero(kept)[0]:  # smallest first: with ``exclusive`` they cut their place out of larger ones
        g = merged[i]
        if exclusive:
            near = [final[int(j)] for j in tree.query(g, predicate="intersects") if int(j) in final]
            if near:
                g = g.difference(unary_union(near)).buffer(0)
        if not g.is_empty and g.area >= min_area_m2:
            final[int(i)] = g
    progress(1.0, f"SAM: {len(final)} unità candidate")
    return [UnitPolygon(g) for g in sorted(final.values(), key=lambda g: -g.area)]


# ------------------------------------------------------------- photos -> cloud
def points_in_masks(chunk, masks: dict[int, np.ndarray], min_votes: float = 0.5) -> np.ndarray:
    """Boolean selection of dense points falling inside photo masks (see :func:`classify_from_masks`)."""
    from ..engine.visibility import project_valid, raycaster_for_chunk

    pc = chunk.point_cloud
    if pc is None:
        raise RuntimeError("build the point cloud first")
    rc = raycaster_for_chunk(chunk)
    inside = np.zeros(len(pc), np.int32)
    seen = np.zeros(len(pc), np.int32)
    for key, mask in masks.items():
        cam = chunk.camera(key)
        px, _, ok = project_valid(chunk, cam, pc.xyz, margin=1)
        idx = np.nonzero(ok)[0]
        if rc is not None and len(idx):
            idx = idx[rc.visible_from(cam.center, pc.xyz[idx], rel_tol=5e-3)]
        cols = np.clip((px[idx, 0] - 0.5).round().astype(int), 0, mask.shape[1] - 1)  # COLMAP -> array index
        rows = np.clip((px[idx, 1] - 0.5).round().astype(int), 0, mask.shape[0] - 1)
        seen[idx] += 1
        inside[idx[mask[rows, cols]]] += 1
    return (seen > 0) & (inside >= np.maximum(1, np.ceil(min_votes * seen)))


def classify_from_masks(chunk, masks: dict[int, np.ndarray], class_code: int, min_votes: float = 0.5,
                        label: str | None = None) -> int:
    """Assign ``class_code`` to dense points that fall inside the photo masks.

    ``masks`` maps camera key -> boolean mask (image size). A point is labelled when,
    among the photos where it is visible, at least ``min_votes`` (fraction) put it
    inside the mask. Visibility uses the mesh when available. Returns the count.
    """
    hit = points_in_masks(chunk, masks, min_votes)
    pc = chunk.point_cloud
    pc.classification[hit] = class_code
    chunk.point_cloud = pc
    if label:
        chunk.meta.setdefault("classes", {})[str(class_code)] = label
    return int(hit.sum())


def segment_photo_and_classify(chunk, segmenter: Segmenter, camera, points_px, class_code: int,
                               labels=None, propagate: bool = True, label: str | None = None) -> int:
    """Click on one photo -> SAM mask -> classify the corresponding dense points.

    ``points_px`` are pixel coordinates in the photo (COLMAP convention). With
    ``propagate=True`` the selection is reprojected into the other photos that see
    it, segmented there with box prompts, and all masks vote together.
    """
    import cv2

    from ..engine.visibility import project_valid, read_photo

    img = cv2.cvtColor(read_photo(camera), cv2.COLOR_BGR2RGB)
    pts = np.asarray(points_px, float) - 0.5  # COLMAP -> array coordinates for SAM
    mask = segmenter.segment_points(img, pts, np.ones(len(pts)) if labels is None else labels)
    masks = {camera.key: mask}
    if propagate:
        sel = points_in_masks(chunk, masks)
        if sel.any():
            xyz = chunk.point_cloud.xyz[sel]
            for cam in chunk.aligned_cameras:
                if cam.key == camera.key:
                    continue
                px, _, ok = project_valid(chunk, cam, xyz)
                if ok.sum() < 30:
                    continue
                p = px[ok] - 0.5
                box = np.r_[np.percentile(p, 1, axis=0), np.percentile(p, 99, axis=0)]
                other = cv2.imread(str(cam.path), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
                if other is None:  # a photo kept online only: the others vote
                    continue
                masks[cam.key] = segmenter.segment_box(cv2.cvtColor(other, cv2.COLOR_BGR2RGB), box)
    return classify_from_masks(chunk, masks, class_code, label=label)
