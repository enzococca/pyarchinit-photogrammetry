"""Image quality: how sharp each photo is (Metashape's *Estimate Image Quality*).

The sharpness of a photo is that of its sharpest parts: on a grey copy reduced to about 1000 px,
in every cell of a 6 x 6 grid with some texture, the finest detail (second derivative) over the
detail a few pixels wide (the image minus a Gaussian blur of 4 px), along x and along y, the lower
of the two (a photo moved sideways keeps its detail across the motion), and the 80th percentile
of the cells. The ratio does not depend on how much texture a scene has, only on how much of it
survives in the finest scale: blur (motion, focus) takes that away first. The quality is that
sharpness over the one of the sharpest photos of the chunk (their 90th percentile), at most 1:
under 0.5 a photo is blurred compared with the others and is better left out.
"""
from __future__ import annotations

import cv2
import numpy as np

from ..core.chunk import Chunk
from ..core.progress import Progress
from .visibility import read_photo

THRESHOLD = 0.5  # under it a photo is suggested to be left out
GRID = 6


def sharpness(image: np.ndarray, grid: int = GRID, max_side: int = 1000) -> float:
    """Sharpness of an image (BGR or grey): in the cells of a ``grid`` x ``grid`` with some
    texture, the finest detail over the detail a few pixels wide; the 80th percentile of the
    cells, on a copy at most ``max_side`` px."""
    grey = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    h, w = grey.shape
    f = min(1.0, max_side / max(h, w))
    if f < 1:
        grey = cv2.resize(grey, (max(1, int(w * f)), max(1, int(h * f))), interpolation=cv2.INTER_AREA)
    g = grey.astype(np.float32)
    gauss = cv2.getGaussianKernel(25, 4).astype(np.float32)
    one = np.ones((1, 1), np.float32)
    bands = []
    for axis in (0, 1):  # along x, then along y
        fine = cv2.Sobel(g, cv2.CV_32F, 2 if axis == 0 else 0, 0 if axis == 0 else 2, ksize=3)
        smooth = cv2.sepFilter2D(g, cv2.CV_32F, gauss, one) if axis == 0 else cv2.sepFilter2D(g, cv2.CV_32F, one, gauss)
        bands.append((fine, g - smooth))
    h, w = g.shape
    values = []
    for r in range(grid):
        for c in range(grid):
            cell = (slice(r * h // grid, (r + 1) * h // grid), slice(c * w // grid, (c + 1) * w // grid))
            ratios = []
            for fine, mid in bands:
                m = float(mid[cell].std())
                if m > 2.0:  # a uniform cell (sky, a plain floor) says nothing about the focus
                    ratios.append(float(fine[cell].std()) / m)
            if len(ratios) == 2:
                values.append(min(ratios))
    return float(np.percentile(values, 80)) if values else 0.0


def estimate_quality(chunk: Chunk, cams=None, progress: Progress | None = None) -> dict[int, float]:
    """Quality (0-1) of the photos of ``cams`` (all enabled ones by default), stored in
    ``camera.meta["quality"]`` (and the raw sharpness in ``meta["sharpness"]``); the reference is
    the sharpest photos of the whole chunk."""
    progress = progress or Progress()
    cams = [c for c in (chunk.cameras if cams is None else cams)]
    for i, cam in enumerate(cams):
        progress.check()
        try:
            img = read_photo(cam, cv2.IMREAD_REDUCED_GRAYSCALE_2)
        except Exception:  # an unreadable photo: no quality
            continue
        cam.meta["sharpness"] = sharpness(img)
        progress((i + 1) / max(len(cams), 1), f"Image quality {i + 1}/{len(cams)}")
    known = [c.meta["sharpness"] for c in chunk.cameras if c.meta.get("sharpness") is not None]
    ref = float(np.percentile(known, 90)) if known else 0.0
    out = {}
    for c in chunk.cameras:
        s = c.meta.get("sharpness")
        if s is not None and ref > 0:
            c.meta["quality"] = round(min(1.0, s / ref), 3)
            out[c.key] = c.meta["quality"]
    return out


def low_quality(chunk: Chunk, threshold: float = THRESHOLD) -> list:
    """The photos whose quality is under ``threshold`` (those never measured are left out)."""
    return [c for c in chunk.cameras if c.meta.get("quality") is not None and c.meta["quality"] < threshold]
