"""Point cloud classification (ASPRS codes + archaeological classes 64-71).

Geometric/radiometric rules that need no training data:

* noise           statistical outlier removal                  -> 7
* ground          progressive morphological filter             -> 2
* vegetation      excess-green index + height above ground     -> 3 / 4 / 5
* structures      above ground, near-vertical planar surfaces  -> 64 (masonry)

Classes can also be assigned from stratigraphic-unit polygons or from image
masks (SAM 2.1, see :mod:`pyarchinit_photogrammetry.ai.sam`).
"""
from __future__ import annotations

import numpy as np

from ..core.pointcloud import ARCHAEO_CLASSES, PointCloud

NOISE, GROUND, LOW_VEG, MED_VEG, HIGH_VEG, BUILDING = 7, 2, 3, 4, 5, 6
MASONRY = 64


def noise_mask(xyz: np.ndarray, k: int = 8, std_ratio: float = 3.0) -> np.ndarray:
    from scipy.spatial import cKDTree

    d, _ = cKDTree(xyz).query(xyz, k=k + 1)
    mean = d[:, 1:].mean(1)
    return mean > mean.mean() + std_ratio * mean.std()


def ground_mask(xyz: np.ndarray, cell: float = 0.05, max_window: float = 1.0, slope: float = 0.3,
                dh0: float = 0.02, dh_max: float = 0.5) -> tuple[np.ndarray, np.ndarray]:
    """Progressive morphological filter (Zhang et al. 2003) on a minimum-elevation grid.

    Returns ``(is_ground, height_above_ground)``.
    """
    from scipy import ndimage

    from ..engine.dem import grid_points

    area = max(np.ptp(xyz[:, 0]) * np.ptp(xyz[:, 1]), 1e-9)
    cell = max(cell, float(np.sqrt(area / 16e6)))  # keep the grid below ~16 M cells
    zmin, (x0, y1), (h, w) = grid_points(xyz, cell, reducer="min")
    surf = np.where(np.isfinite(zmin), zmin, np.nanmax(zmin))
    filled = ndimage.grey_closing(surf, size=3)
    surf = np.where(np.isfinite(zmin), surf, filled)
    ground_surf = surf.copy()
    win = 3
    dh = dh0
    prev = win
    while win * cell <= max_window:
        opened = ndimage.grey_opening(ground_surf, size=(win, win))
        nonground = (ground_surf - opened) > dh
        ground_surf = np.where(nonground, opened, ground_surf)
        dh = min(dh_max, slope * (win - prev) * cell + dh0)
        prev = win
        win = 2 * win - 1
    col = np.clip(((xyz[:, 0] - x0) / cell).astype(int), 0, w - 1)
    row = np.clip(((y1 - xyz[:, 1]) / cell).astype(int), 0, h - 1)
    hag = xyz[:, 2] - ground_surf[row, col]
    return hag <= max(dh0 * 1.5, 0.03), hag


def excess_green(rgb: np.ndarray) -> np.ndarray:
    c = rgb.astype(np.float64)
    s = c.sum(1) + 1e-9
    r, g, b = c[:, 0] / s, c[:, 1] / s, c[:, 2] / s
    return 2 * g - r - b


def normals_verticality(xyz: np.ndarray, k: int = 12) -> np.ndarray:
    """|n_z| of PCA normals (0 = vertical surface, 1 = horizontal)."""
    from scipy.spatial import cKDTree

    _, idx = cKDTree(xyz).query(xyz, k=k)
    nb = xyz[idx] - xyz[idx].mean(1, keepdims=True)
    cov = np.einsum("nki,nkj->nij", nb, nb)
    _, vecs = np.linalg.eigh(cov)
    return np.abs(vecs[:, :, 0][:, 2])


def classify(pc_world: PointCloud, cell: float = 0.05, vegetation: bool = True, structures: bool = True,
             structure_min_height: float = 0.15, exg_threshold: float = 0.08) -> PointCloud:
    """Classify a point cloud expressed in world coordinates (Z up). Returns a copy."""
    out = pc_world.subset(np.ones(len(pc_world), bool))
    cls = np.ones(len(out), np.uint8)
    noise = noise_mask(out.xyz)
    cls[noise] = NOISE
    good = ~noise
    is_ground, hag = ground_mask(out.xyz[good], cell=cell)
    g_idx = np.nonzero(good)[0]
    cls[g_idx[is_ground]] = GROUND
    above = np.zeros(len(out), bool)
    above[g_idx[~is_ground]] = True
    full_hag = np.zeros(len(out))
    full_hag[g_idx] = hag
    if vegetation and out.rgb is not None:
        veg = above & (excess_green(out.rgb) > exg_threshold)
        cls[veg & (full_hag < 0.5)] = LOW_VEG
        cls[veg & (full_hag >= 0.5) & (full_hag < 2.0)] = MED_VEG
        cls[veg & (full_hag >= 2.0)] = HIGH_VEG
        above &= ~veg
    if structures and above.any():
        # solid, non-green masses standing above the ground surface (walls, pillars, collapses)
        cand = np.nonzero(above & (full_hag >= structure_min_height))[0]
        if len(cand) > 20:
            cls[cand] = MASONRY
    out.classification = cls
    out.labels = {**{c: n for c, n in ARCHAEO_CLASSES.items()}, **out.labels}
    return out


def classify_chunk(chunk, **kwargs) -> PointCloud:
    """Classify the chunk dense cloud in place (world frame used for "up")."""
    from ..engine.dem import ensure_level

    ensure_level(chunk)
    pc = chunk.point_cloud
    if pc is None:
        raise RuntimeError("build the point cloud first")
    world = pc.transformed(chunk.transform)
    classified = classify(world, **kwargs)
    pc.classification = classified.classification
    chunk.point_cloud = pc
    chunk.meta.setdefault("classes", {}).update({str(k): v for k, v in classified.labels.items()})
    return pc


def assign_class_by_polygons(chunk, units, class_code: int | None = None, base_code: int = 100) -> dict[str, int]:
    """Give points inside each unit polygon (plan view) a class. Without ``class_code``
    each unit gets its own code (``base_code``, ``base_code + 1``...). Returns US -> code."""
    from shapely import contains_xy

    pc = chunk.point_cloud
    world = chunk.to_world(pc.xyz)
    mapping = {}
    for i, u in enumerate(units):
        code = class_code if class_code is not None else base_code + i
        inside = contains_xy(u.geometry, world[:, 0], world[:, 1])
        pc.classification[inside] = code
        mapping[str(u.us)] = code
        chunk.meta.setdefault("classes", {})[str(code)] = f"{u.tipo} {u.us}"
    chunk.point_cloud = pc
    return mapping


def class_statistics(pc: PointCloud) -> dict[int, int]:
    codes, counts = np.unique(pc.classification, return_counts=True)
    return {int(c): int(n) for c, n in zip(codes, counts)}
