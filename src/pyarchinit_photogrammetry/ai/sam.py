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

from ..archaeo.stratigraphy import UnitPolygon, mask_to_polygons, orthomosaic_window

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

    def segment_auto(self, image, points_per_side: int = 32, min_area: int = 500):
        import torch
        from sam2.automatic_mask_generator import SAM2AutomaticMaskGenerator

        gen = SAM2AutomaticMaskGenerator(self._model, points_per_side=points_per_side, min_mask_region_area=min_area)
        with torch.inference_mode():
            res = gen.generate(image)
        return [r["segmentation"].astype(bool) for r in sorted(res, key=lambda r: -r["area"])]


# ------------------------------------------------------------ orthomosaic
def _world_to_pixel(transform, xy: np.ndarray) -> np.ndarray:
    """World XY -> fractional (col, row) for north-up rasters (pixel corners on integers)."""
    xy = np.asarray(xy, float)
    return np.column_stack([(xy[:, 0] - transform.c) / transform.a, (xy[:, 1] - transform.f) / transform.e])


def segment_orthomosaic(chunk, segmenter: Segmenter, points=None, labels=None, box=None, window: int = 2048,
                        simplify: float | None = None, us: str | None = None, area: str | None = None) -> UnitPolygon:
    """Segment one unit on the orthomosaic from world-coordinate prompts.

    ``points``: (N,2) world XY with ``labels`` (1 = inside, 0 = outside); or ``box`` =
    (xmin, ymin, xmax, ymax) world. Only a window around the prompts is processed.
    """
    from rasterio.windows import Window
    from rasterio.windows import transform as win_transform

    import rasterio

    path = chunk.orthomosaic_path
    if path is None:
        raise RuntimeError("build the orthomosaic first")
    with rasterio.open(path) as src:
        prompt_xy = np.asarray(points if points is not None else np.reshape(box, (2, 2)), float)
        px = _world_to_pixel(src.transform, prompt_xy)
        c0 = int(np.clip(px[:, 0].mean() - window / 2, 0, max(src.width - window, 0)))
        r0 = int(np.clip(px[:, 1].mean() - window / 2, 0, max(src.height - window, 0)))
        w = Window(c0, r0, min(window, src.width), min(window, src.height))
        img = np.moveaxis(src.read([1, 2, 3], window=w), 0, -1).copy()
        wtr = win_transform(w, src.transform)
        res = src.res[0]
    local = px - [c0, r0] - 0.5  # corner-based -> pixel-centre coordinates expected by SAM
    if points is not None:
        mask = segmenter.segment_points(img, local, np.ones(len(local)) if labels is None else labels)
    else:
        mask = segmenter.segment_box(img, local.ravel())
    geom = mask_to_polygons(mask, wtr, simplify=simplify if simplify is not None else 2 * res,
                            min_area=(10 * res) ** 2)
    return UnitPolygon(geom, us=us, area=area)


def auto_units(chunk, segmenter: Segmenter, max_side: int = 2048, simplify: float | None = None,
               min_area_m2: float = 0.01) -> list[UnitPolygon]:
    """Automatic segmentation of the whole orthomosaic into candidate units (unlabelled)."""
    import cv2
    from affine import Affine

    img, alpha, tr, _ = orthomosaic_window(chunk.orthomosaic_path)
    f = max(1.0, max(img.shape[:2]) / max_side)
    small = cv2.resize(img, (int(img.shape[1] / f), int(img.shape[0] / f)), interpolation=cv2.INTER_AREA) if f > 1 else img
    sx, sy = img.shape[1] / small.shape[1], img.shape[0] / small.shape[0]
    tr_small = tr @ Affine.scale(sx, sy)
    res = abs(tr.a) * max(sx, sy)
    units = []
    a_small = cv2.resize(alpha, (small.shape[1], small.shape[0]), interpolation=cv2.INTER_NEAREST) > 0
    for m in segmenter.segment_auto(small):
        m = m & a_small
        if m.sum() * res * res < min_area_m2:
            continue
        geom = mask_to_polygons(m, tr_small, simplify=simplify if simplify is not None else 2 * res)
        if not geom.is_empty:
            units.append(UnitPolygon(geom))
    return units


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

    from ..engine.visibility import project_valid

    img = cv2.cvtColor(cv2.imread(str(camera.path), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION), cv2.COLOR_BGR2RGB)
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
                other = cv2.cvtColor(cv2.imread(str(cam.path), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION),
                                     cv2.COLOR_BGR2RGB)
                masks[cam.key] = segmenter.segment_box(other, box)
    return classify_from_masks(chunk, masks, class_code, label=label)
