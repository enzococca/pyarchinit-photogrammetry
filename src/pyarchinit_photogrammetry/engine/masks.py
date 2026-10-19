"""Masks of the photos: the parts of a photo to leave out (background, sky, people, the turntable).

A mask is a single-channel PNG as large as its photo, white where the photo counts and black
where it is masked (the convention of Metashape and COLMAP), kept as
``<chunk data>/masks/<camera key>.png``. Masked areas are left out when the photos are matched
(COLMAP ignores their keypoints), in the depth maps (no depth there, so no dense points), in the
orthomosaic and in the texture (a photo does not see what its mask hides).

Masks come from files (:func:`import_masks`, also the alpha channel of the photos), from a photo of
the background taken without the object (:func:`masks_from_background`), from a colour such as a
chroma backdrop (:func:`masks_from_colour`), from the model (:func:`masks_from_model`: what is not
the model), or from SAM 2.1 (:func:`masks_with_sam`: the object at the centre of the photo); they
are drawn or corrected by hand in the photo view (:func:`paint_polygon`).
"""
from __future__ import annotations

import os
import shutil
import threading
from collections import OrderedDict
from pathlib import Path

import cv2
import numpy as np

from ..core.chunk import Chunk
from ..core.progress import Progress, as_progress
from .visibility import read_photo

TEMPLATES = ("{stem}_mask.png", "{stem}.png", "{name}.png", "{stem}_mask.jpg", "{stem}.jpg")


def mask_dir(chunk: Chunk) -> Path:
    return chunk.data_dir / "masks"


def mask_path(chunk: Chunk, cam) -> Path | None:
    """The mask file of ``cam``, None when it has none (or the chunk has no folder yet)."""
    try:
        p = mask_dir(chunk) / f"{cam.key}.png"
    except (AttributeError, RuntimeError):  # a chunk not saved in a document: no masks
        return None
    return p if p.exists() else None


def masked_cameras(chunk: Chunk) -> list:
    return [c for c in chunk.cameras if mask_path(chunk, c) is not None]


def read_mask(chunk: Chunk, cam, shape: tuple[int, int] | None = None) -> np.ndarray | None:
    """True where the photo counts (h x w bool), None without a mask; resized (nearest) to
    ``shape`` when given."""
    p = mask_path(chunk, cam)
    if p is None:
        return None
    m = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
    if m is None:
        return None
    if shape is not None and m.shape != tuple(shape):
        m = cv2.resize(m, (shape[1], shape[0]), interpolation=cv2.INTER_NEAREST)
    return m > 127


def write_mask(chunk: Chunk, cam, keep: np.ndarray) -> Path:
    """Store the mask of ``cam`` (``keep``: True where the photo counts)."""
    folder = mask_dir(chunk)
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / f"{cam.key}.png"
    tmp = p.with_name(f".{p.name}")
    cv2.imwrite(str(tmp), np.where(np.asarray(keep, bool), 255, 0).astype(np.uint8))
    os.replace(tmp, p)
    _CACHE.forget(p)
    return p


def remove_masks(chunk: Chunk, cams=None) -> int:
    """Remove the masks of ``cams`` (all by default); returns how many there were."""
    n = 0
    for cam in chunk.cameras if cams is None else cams:
        p = mask_path(chunk, cam)
        if p is not None:
            p.unlink()
            _CACHE.forget(p)
            n += 1
    return n


def _photo_size(chunk: Chunk, cam) -> tuple[int, int]:
    s = chunk.sensor(cam)
    return int(s.height), int(s.width)


def _find(folder: Path, cam, template: str | None) -> Path | None:
    name = Path(cam.path).name
    stem = Path(cam.path).stem
    for t in ([template] if template else TEMPLATES):
        p = folder / t.format(name=name, stem=stem, label=cam.label, key=cam.key)
        if p.exists():
            return p
    return None


def import_masks(chunk: Chunk, folder: str | os.PathLike | None = None, template: str | None = None,
                 invert: bool = False, cams=None, progress: Progress | None = None) -> int:
    """Masks from image files in ``folder`` named after the photos (``template`` with ``{name}``,
    ``{stem}``, ``{label}``; by default ``<stem>_mask.png``, ``<stem>.png`` and the like), or,
    without a folder, from the alpha channel of the photos. White (or opaque) counts, unless
    ``invert``. Returns the masks written."""
    progress = as_progress(progress)
    cams = list(chunk.cameras if cams is None else cams)
    n = 0
    for i, cam in enumerate(cams):
        progress.check()
        if folder is None:
            img = cv2.imread(str(cam.path), cv2.IMREAD_UNCHANGED | cv2.IMREAD_IGNORE_ORIENTATION)
            if img is None or img.ndim != 3 or img.shape[2] != 4:
                continue
            m = img[..., 3]
        else:
            p = _find(Path(folder), cam, template)
            if p is None:
                continue
            m = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE | cv2.IMREAD_IGNORE_ORIENTATION)
            if m is None:
                continue
        h, w = _photo_size(chunk, cam)
        if m.shape != (h, w):
            m = cv2.resize(m, (w, h), interpolation=cv2.INTER_NEAREST)
        keep = m > 127
        write_mask(chunk, cam, ~keep if invert else keep)
        n += 1
        progress((i + 1) / len(cams), f"Masks {i + 1}/{len(cams)}")
    return n


def export_masks(chunk: Chunk, folder: str | os.PathLike, template: str = "{stem}_mask.png") -> int:
    """Write the masks into ``folder`` named after the photos; returns how many."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    n = 0
    for cam in masked_cameras(chunk):
        name, stem = Path(cam.path).name, Path(cam.path).stem
        shutil.copy2(mask_path(chunk, cam), folder / template.format(name=name, stem=stem, label=cam.label,
                                                                     key=cam.key))
        n += 1
    return n


def _clean(keep: np.ndarray, size: int) -> np.ndarray:
    """Morphological cleaning of a mask: specks removed and small gaps closed."""
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
    m = cv2.morphologyEx(keep.astype(np.uint8), cv2.MORPH_OPEN, k)
    return cv2.morphologyEx(m, cv2.MORPH_CLOSE, k) > 0


def _largest(keep: np.ndarray, share: float = 0.02) -> np.ndarray:
    """The connected parts of a mask larger than ``share`` of the largest one, holes filled."""
    count, labels, stats, _ = cv2.connectedComponentsWithStats(keep.astype(np.uint8), connectivity=8)
    if count <= 1:
        return keep
    areas = stats[1:, cv2.CC_STAT_AREA]
    big = np.zeros(count, bool)
    big[1:] = areas >= share * areas.max()
    out = big[labels]
    # holes inside the object (texture similar to the background) are filled
    filled = out.astype(np.uint8).copy()
    h, w = filled.shape
    flood = np.zeros((h + 2, w + 2), np.uint8)
    cv2.floodFill(filled, flood, (0, 0), 2)
    return out | (filled == 0)


def masks_from_background(chunk: Chunk, folder: str | os.PathLike, tolerance: float = 12.0, cams=None,
                          template: str | None = None, progress: Progress | None = None) -> int:
    """Masks from photos of the background without the object, taken from the same positions
    (a turntable with a fixed camera: one background photo, or one per photo, named as the photos
    or as ``template``; a single file in the folder serves every photo): what differs from the
    background by more than ``tolerance`` levels is the object."""
    progress = as_progress(progress)
    folder = Path(folder)
    cams = list(chunk.cameras if cams is None else cams)
    files = [p for p in folder.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".tif", ".tiff")]
    single = files[0] if len(files) == 1 else None
    n = 0
    backgrounds: dict[Path, np.ndarray] = {}
    for i, cam in enumerate(cams):
        progress.check()
        p = single or _find(folder, cam, template or "{name}")
        if p is None:
            continue
        if p not in backgrounds:
            backgrounds[p] = cv2.GaussianBlur(cv2.imread(str(p), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION),
                                              (5, 5), 0).astype(np.int16)
        bg = backgrounds[p]
        img = cv2.GaussianBlur(read_photo(cam), (5, 5), 0).astype(np.int16)
        if img.shape != bg.shape:
            continue
        diff = np.abs(img - bg).max(axis=2)
        size = max(3, int(round(min(img.shape[:2]) / 300)) | 1)
        keep = _largest(_clean(diff > tolerance, size))
        write_mask(chunk, cam, keep)
        n += 1
        progress((i + 1) / len(cams), f"Masks from the background {i + 1}/{len(cams)}")
    return n


def masks_from_colour(chunk: Chunk, colour, tolerance: float = 40.0, cams=None,
                      progress: Progress | None = None) -> int:
    """Masks that leave out a backdrop of one colour (``colour`` RGB): every pixel within
    ``tolerance`` of it (distance in RGB) is masked."""
    progress = as_progress(progress)
    cams = list(chunk.cameras if cams is None else cams)
    bgr = np.array(colour[::-1][:3] if len(colour) >= 3 else colour, np.float32)
    for i, cam in enumerate(cams):
        progress.check()
        img = cv2.GaussianBlur(read_photo(cam), (5, 5), 0).astype(np.float32)
        near = np.linalg.norm(img - bgr, axis=2) <= tolerance
        size = max(3, int(round(min(img.shape[:2]) / 300)) | 1)
        write_mask(chunk, cam, _clean(~near, size))
        progress((i + 1) / len(cams), f"Masks from the colour {i + 1}/{len(cams)}")
    return len(cams)


def masks_from_model(chunk: Chunk, cams=None, step: int = 4, margin_px: int = 6,
                     progress: Progress | None = None) -> int:
    """Masks that keep only the model: a pixel counts where its ray meets the mesh (grown by
    ``margin_px``). Useful after a first reconstruction to leave out the background."""
    from .visibility import Raycaster
    from ..io.mesh_ply import read_mesh

    progress = as_progress(progress)
    path = chunk.product_path("model")
    if path is None:
        raise RuntimeError("build the model first")
    mesh = read_mesh(path)
    ray = Raycaster(mesh.xyz, mesh.faces)
    cams = [c for c in (chunk.aligned_cameras if cams is None else cams) if c.aligned]
    for i, cam in enumerate(cams):
        progress.check()
        sensor = chunk.sensor(cam)
        h, w = int(sensor.height), int(sensor.width)
        ys, xs = np.mgrid[step // 2:h:step, step // 2:w:step]
        px = np.column_stack([xs.ravel() + 0.5, ys.ravel() + 0.5])  # COLMAP pixel centres
        rays = sensor.unproject(px)
        d = np.column_stack([rays, np.ones(len(rays))]) @ cam.transform[:3, :3].T
        d /= np.linalg.norm(d, axis=1, keepdims=True)
        hit = np.isfinite(ray.hit_distance(np.repeat(cam.center[None], len(d), 0), d))
        coarse = hit.reshape(xs.shape).astype(np.uint8) * 255
        keep = cv2.resize(coarse, (w, h), interpolation=cv2.INTER_NEAREST) > 127
        if margin_px > 0:
            keep = cv2.dilate(keep.astype(np.uint8), np.ones((2 * margin_px + 1,) * 2, np.uint8)) > 0
        write_mask(chunk, cam, keep)
        progress((i + 1) / len(cams), f"Masks from the model {i + 1}/{len(cams)}")
    return len(cams)


def masks_with_sam(chunk: Chunk, segmenter, cams=None, box_share: float = 0.0,
                   progress: Progress | None = None) -> int:
    """Masks that keep the object at the centre of each photo, segmented by SAM 2.1 (a point at the
    centre; with ``box_share`` > 0 also a centred box of that share of the photo)."""
    progress = as_progress(progress)
    cams = list(chunk.cameras if cams is None else cams)
    for i, cam in enumerate(cams):
        progress.check()
        img = read_photo(cam)
        h, w = img.shape[:2]
        rgb = img[..., ::-1].copy()
        pts = np.array([[w / 2.0, h / 2.0]])
        if box_share > 0 and hasattr(segmenter, "segment_points_box"):
            bw, bh = w * box_share / 2, h * box_share / 2
            m = segmenter.segment_points_box(rgb, pts, np.array([1]),
                                             np.array([w / 2 - bw, h / 2 - bh, w / 2 + bw, h / 2 + bh]))
        else:
            m = segmenter.segment_points(rgb, pts, np.array([1]))
        keep = _largest(np.asarray(m, bool))
        write_mask(chunk, cam, keep)
        progress((i + 1) / len(cams), f"Masks with SAM {i + 1}/{len(cams)}")
    return len(cams)


def paint_polygon(chunk: Chunk, cam, polygon, masked: bool = True) -> Path:
    """Mask (``masked``) or unmask the area of ``polygon`` (pixel vertices, COLMAP coordinates)
    in the photo of ``cam``, starting from its mask or from none."""
    h, w = _photo_size(chunk, cam)
    keep = read_mask(chunk, cam, (h, w))
    keep = np.ones((h, w), bool) if keep is None else keep.copy()
    m = np.zeros((h, w), np.uint8)
    pts = np.round(np.asarray(polygon, np.float64)[:, :2] - 0.5).astype(np.int32)
    cv2.fillPoly(m, [pts], 1)
    keep[m > 0] = not masked
    return write_mask(chunk, cam, keep)


# ---------------------------------------------------------------- use by the engine
class _MaskCache:
    """Masks read once and shared by the threads (least recently used dropped)."""

    def __init__(self, max_items: int = 16):
        self.max_items = max_items
        self._items: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def get(self, path: Path) -> np.ndarray | None:
        key = (str(path), path.stat().st_mtime_ns)
        with self._lock:
            if key in self._items:
                self._items.move_to_end(key)
                return self._items[key]
        m = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        keep = None if m is None else m > 127
        with self._lock:
            self._items[key] = keep
            while len(self._items) > self.max_items:
                self._items.popitem(last=False)
        return keep

    def forget(self, path: Path) -> None:
        with self._lock:
            for key in [k for k in self._items if k[0] == str(path)]:
                del self._items[key]


_CACHE = _MaskCache()


def hidden_by_mask(chunk: Chunk, cam, px: np.ndarray) -> np.ndarray:
    """True for the pixel positions ``px`` (COLMAP coordinates) that the mask of ``cam`` hides
    (all False without a mask)."""
    out = np.zeros(len(px), bool)
    p = mask_path(chunk, cam)
    if p is None or not len(px):
        return out
    keep = _CACHE.get(p)
    if keep is None:
        return out
    h, w = keep.shape
    sx, sy = w / chunk.sensor(cam).width, h / chunk.sensor(cam).height
    col = np.floor(px[:, 0] * sx).astype(np.int64)
    row = np.floor(px[:, 1] * sy).astype(np.int64)
    inside = (col >= 0) & (col < w) & (row >= 0) & (row < h) & np.isfinite(px).all(axis=1)
    out[inside] = ~keep[row[inside], col[inside]]
    return out


def colmap_mask_folder(chunk: Chunk, names: dict[int, str], folder: Path) -> Path | None:
    """The masks laid out as COLMAP reads them (``<folder>/<image name>.png``), None without masks.
    COLMAP skips a photo whose mask is missing, so the photos without one get a white mask (one
    small file per photo size, linked)."""
    shutil.rmtree(folder, ignore_errors=True)
    cams = [c for c in chunk.cameras if c.key in names]
    if not any(mask_path(chunk, c) is not None for c in cams):
        return None
    white: dict[tuple[int, int], Path] = {}
    for cam in cams:
        p = mask_path(chunk, cam)
        if p is None:
            size = _photo_size(chunk, cam)
            if size not in white:
                white[size] = folder / f".white_{size[1]}x{size[0]}.png"
                white[size].parent.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(str(white[size]), np.full(size, 255, np.uint8))
            p = white[size]
        out = folder / f"{names[cam.key]}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(p, out)
        except OSError:
            shutil.copy2(p, out)
    return folder


def mask_for_undistorted(chunk: Chunk, cam, K: np.ndarray, shape: tuple[int, int], step: int = 4) -> np.ndarray | None:
    """The mask of ``cam`` on the grid of a depth map of it (undistorted pinhole ``K``, ``shape``
    h x w; pixel (col, row) is the ray through ``K^-1 (col, row, 1)`` as in COLMAP's MVS): True
    where the photo counts."""
    p = mask_path(chunk, cam)
    if p is None:
        return None
    keep = _CACHE.get(p)
    if keep is None:
        return None
    sensor = chunk.sensor(cam)
    h, w = shape
    ys, xs = np.mgrid[0:h:step, 0:w:step].astype(np.float64)
    x = (xs - K[0, 2]) / K[0, 0]
    y = (ys - K[1, 2]) / K[1, 1]
    px = sensor.project(np.column_stack([x.ravel(), y.ravel(), np.ones(x.size)]))
    hidden = hidden_by_mask(chunk, cam, px).reshape(xs.shape)
    coarse = np.where(hidden, 0, 255).astype(np.uint8)
    return cv2.resize(coarse, (w, h), interpolation=cv2.INTER_NEAREST) > 127
