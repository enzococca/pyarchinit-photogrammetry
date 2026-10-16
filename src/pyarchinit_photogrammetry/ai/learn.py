"""Trainable point cloud classification.

The user labels some points by hand (:func:`.classify.set_class`), a classifier learns
from them (:func:`train_on_chunk`) and classifies the rest of the cloud
(:func:`classify_chunk_with_model`). Saved with :meth:`ClassifierModel.save`, the
classifier also works on the clouds of other projects, like CloudCompare's 3DMASC /
CANUPO or the trainable classification of commercial photogrammetry software.

Features (:func:`point_features`) describe the neighbourhood of each point at three
radii (metres, by default 5, 14 and 40 times the point spacing):

* shape of the local covariance (eigenvalues): linearity, planarity, sphericity,
  omnivariance, eigenentropy, change of curvature; verticality of the normal and of
  the main axis; density; height above the lowest and below the highest neighbour,
  vertical spread; roughness (distance from the local plane);
* height above ground (progressive morphological filter, or a DTM);
* colour: normalised RGB, HSV, excess green, contrast with the neighbours.

They are computed on a *core* cloud (one point per voxel, the centroid of its points)
and each point takes the result of its voxel, so millions of points stay tractable.
Every radius is evaluated on its own voxel level (voxel = radius / ``ratio``) with at
most ``k`` neighbours: the cost per point is the same at every scale.

The classifier is a scikit-learn random forest (default) or histogram gradient
boosting; install scikit-learn with ``pip install pyarchinit-photogrammetry[learn]``.
"""
from __future__ import annotations

import contextlib
import io
import json
import logging
import math
import os
import pickle
import time
import warnings
import zipfile
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from ..core.pointcloud import ARCHAEO_CLASSES, ASPRS_CLASSES, PointCloud
from ..core.progress import ProgressCallback, as_progress

log = logging.getLogger("pyarchinit_photogrammetry")

UNCLASSIFIED = 1
METHODS = ("random_forest", "gradient_boosting")
SCALE_FACTORS = (5.0, 14.0, 40.0)  # default radii, multiples of the core voxel (~ point spacing)
MAX_CORE_POINTS = 1_000_000
N_JOBS = 4
BLOCK = 32_768  # core points per batch (bounds the memory of neighbourhoods and predictions)
TREE_SAMPLES = 10_000  # bootstrap of each tree at most: bounds the size of a forest (and its file)
MODEL_FORMAT = "pyarchinit-photogrammetry/point-classifier"
MODEL_VERSION = 1
MODEL_SUFFIX = ".ppgclf"

GEOMETRIC_FEATURES = ("linearity", "planarity", "sphericity", "omnivariance", "eigenentropy", "curvature",
                      "verticality", "axis_z", "density", "dz_min", "dz_max", "z_std", "roughness")
COLOUR_FEATURES = ("red", "green", "blue", "value", "saturation", "hue_cos", "hue_sin", "excess_green")
CONTEXT_FEATURES = ("excess_green_mean", "value_contrast", "value_std")  # colour of the neighbours (first radius)


def _sklearn():
    try:
        import sklearn
    except ImportError as exc:  # pragma: no cover - depends on the installation
        raise ImportError("the trainable classifier needs scikit-learn: "
                          "pip install 'pyarchinit-photogrammetry[learn]'") from exc
    return sklearn


def _round2(x: float) -> float:
    return float(f"{x:.2g}")


# ---------------------------------------------------------------- feature configuration
@dataclass
class FeatureConfig:
    """How the features are computed; saved with a classifier so that every cloud it sees
    is described in the same way. Lengths in metres."""

    scales: tuple[float, ...] = ()   # neighbourhood radii; empty = from the point spacing
    colour: bool = True
    height: bool = True              # height above ground
    normals: bool = False            # |n_z| of the cloud normals
    ratio: float = 4.0               # radius / voxel of the level each radius is evaluated on
    k: int = 64                      # neighbours per radius at most
    ground_cell: float | None = None  # ground filter cell (None = half the first radius)
    ground_window: float | None = None  # largest object removed from the ground (None = auto)

    def __post_init__(self):
        self.scales = tuple(float(r) for r in self.scales)

    @property
    def names(self) -> list[str]:
        names = [f"{f}@{r:g}m" for r in self.scales for f in GEOMETRIC_FEATURES]
        if self.height:
            names.append("height_above_ground")
        if self.normals:
            names.append("normal_z")
        if self.colour:
            names += list(COLOUR_FEATURES) + [f"{f}@{self.scales[0]:g}m" for f in CONTEXT_FEATURES]
        return names

    def to_dict(self) -> dict:
        d = asdict(self)
        d["scales"] = list(self.scales)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "FeatureConfig":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


def estimate_spacing(xyz: np.ndarray, sample: int = 100_000, seed: int = 0) -> float:
    """Typical distance between points of a surface-like cloud: the square root of the area
    per point, from the median distance to the 8th neighbour (``pi d8^2`` holds 8 points).
    Big clouds are sampled: in a random fraction ``q`` of the points the spacing is
    ``spacing / sqrt(q)``."""
    from scipy.spatial import cKDTree

    xyz = np.asarray(xyz, dtype=float).reshape(-1, 3)
    n = len(xyz)
    if n < 10:
        raise ValueError("the cloud has too few points")
    pts, factor = xyz, 1.0
    if n > sample:
        pts = xyz[np.random.default_rng(seed).choice(n, sample, replace=False)]
        factor = math.sqrt(sample / n)
    pts = pts - pts.min(0)
    d = cKDTree(pts).query(pts, k=9)[0][:, 8]
    d = d[d > 0]
    if not len(d):
        raise ValueError("all the points coincide")
    return float(np.median(d) * math.sqrt(math.pi / 8) * factor)


def suggest_scales(xyz: np.ndarray, max_core_points: int = MAX_CORE_POINTS) -> tuple[float, tuple[float, ...]]:
    """Core voxel and default neighbourhood radii of a cloud: ``(voxel, (r1, r2, r3))``.

    The voxel is the point spacing, larger when needed to keep about ``max_core_points``
    core points; the radii are 5, 14 and 40 voxels (two significant digits)."""
    xyz = np.asarray(xyz, dtype=float).reshape(-1, 3)
    voxel = estimate_spacing(xyz) * max(1.0, math.sqrt(len(xyz) / max_core_points))
    return voxel, tuple(_round2(voxel * f) for f in SCALE_FACTORS)


# ---------------------------------------------------------------- voxels and core cloud
def _voxel_index(xyz: np.ndarray, offset: np.ndarray, voxel: float, block: int = 4_000_000) -> tuple[int, np.ndarray]:
    """Voxel of every point: ``(n_voxels, index)``, voxels numbered in sorted key order."""
    n = len(xyz)
    ext = np.floor((xyz.max(0) - offset) / voxel).astype(np.int64) + 1
    if float(ext[0]) * float(ext[1]) * float(ext[2]) < 2.0 ** 62:
        keys = np.empty(n, np.int64)
        for s in range(0, n, block):
            ijk = np.floor((xyz[s:s + block] - offset) / voxel).astype(np.int64)
            keys[s:s + block] = (ijk[:, 2] * ext[1] + ijk[:, 1]) * ext[0] + ijk[:, 0]
        uniq, index = np.unique(keys, return_inverse=True)
    else:  # far outliers: keys would overflow
        uniq, index = np.unique(np.floor((xyz - offset) / voxel).astype(np.int64), axis=0, return_inverse=True)
    return len(uniq), index.reshape(-1).astype(np.int32 if n < 2 ** 31 else np.int64)


def _weighted_mean(index: np.ndarray, values: np.ndarray, weights: np.ndarray | None, total: np.ndarray) -> np.ndarray:
    cols = [np.bincount(index, weights=values[:, c] if weights is None else values[:, c] * weights,
                        minlength=len(total)) for c in range(values.shape[1])]
    return np.column_stack(cols) / total[:, None]


def _colour_features(rgb: np.ndarray) -> np.ndarray:
    """Per point (0..255 floats): normalised RGB, value, saturation, hue as a saturation-
    weighted (cos, sin) pair, excess green."""
    c = np.asarray(rgb, dtype=np.float64) / 255.0
    s = c.sum(1, keepdims=True)
    chroma = np.divide(c, s, out=np.full_like(c, 1.0 / 3.0), where=s > 1e-9)
    mx, mn = c.max(1), c.min(1)
    delta = mx - mn
    sat = np.divide(delta, mx, out=np.zeros_like(mx), where=mx > 1e-9)
    r, g, b = c.T
    safe = np.where(delta > 1e-9, delta, 1.0)
    hue = np.where(mx == r, ((g - b) / safe) % 6.0, np.where(mx == g, (b - r) / safe + 2.0, (r - g) / safe + 4.0))
    hue = hue * (np.pi / 3.0)
    exg = 2 * chroma[:, 1] - chroma[:, 0] - chroma[:, 2]
    return np.column_stack([chroma, mx, sat, sat * np.cos(hue), sat * np.sin(hue), exg])


@dataclass
class _Level:
    voxel: float
    xyz: np.ndarray
    tree: Any
    value: np.ndarray | None = None  # brightness 0..1 (colour context)
    exg: np.ndarray | None = None    # excess green


def _neighbourhood(q: np.ndarray, level: _Level, radius: float, k: int, n_jobs: int, context: bool):
    """Geometric features of the neighbourhoods of ``q`` (local coordinates) on a level,
    plus the colour statistics of the neighbours when ``context``."""
    n_pts = len(level.xyz)
    k = int(min(k, n_pts))
    _, idx = level.tree.query(q, k=k, distance_upper_bound=radius, workers=n_jobs)
    if idx.ndim == 1:
        idx = idx[:, None]
    valid = idx < n_pts
    cnt = valid.sum(1)
    safe = np.maximum(cnt, 1).astype(np.float64)
    idx = np.where(valid, idx, 0)
    rel = level.xyz[idx] - q[:, None, :]
    rel[~valid] = 0.0
    mean = rel.sum(1) / safe[:, None]
    cen = rel - mean[:, None, :]
    cen[~valid] = 0.0
    cov = np.matmul(cen.transpose(0, 2, 1), cen) / safe[:, None, None]
    ev, vec = np.linalg.eigh(cov)
    ev = np.clip(ev, 0.0, None)
    l3, l2, l1 = ev[:, 0], ev[:, 1], ev[:, 2]
    zero = np.zeros_like(l1)
    lin = np.divide(l1 - l2, l1, out=zero.copy(), where=l1 > 0)
    pla = np.divide(l2 - l3, l1, out=zero.copy(), where=l1 > 0)
    sph = np.divide(l3, l1, out=zero.copy(), where=l1 > 0)
    total = ev.sum(1)
    e = np.divide(ev, total[:, None], out=np.zeros_like(ev), where=total[:, None] > 0)
    omni = np.cbrt(e.prod(1))
    entropy = -(e * np.log(np.where(e > 0, e, 1.0))).sum(1)
    curvature = e[:, 0]
    normal, axis = vec[:, :, 0], vec[:, :, 2]
    vert = 1.0 - np.abs(normal[:, 2])
    axis_z = np.abs(axis[:, 2])
    rough = np.abs((mean * normal).sum(1))
    count = level.tree.query_ball_point(q, radius, return_length=True, workers=n_jobs)
    density = count * level.voxel ** 2 / (np.pi * radius ** 2)
    dz = rel[..., 2]
    low = np.where(valid, dz, np.inf).min(1)
    high = np.where(valid, dz, -np.inf).max(1)
    dz_min = np.where(cnt > 0, -low, 0.0)
    dz_max = np.where(cnt > 0, high, 0.0)
    z_std = np.sqrt(cov[:, 2, 2])
    geo = np.column_stack([lin, pla, sph, omni, entropy, curvature, vert, axis_z, density, dz_min, dz_max, z_std,
                           rough])
    flat = cnt < 3  # no shape from fewer than three neighbours
    geo[flat] = np.where(np.isin(np.arange(geo.shape[1]), (8, 9, 10, 11)), geo[flat], 0.0)
    ctx = None
    if context:
        w = valid.astype(np.float64)
        val = level.value[idx]
        val_mean = (val * w).sum(1) / safe
        val_std = np.sqrt((((val - val_mean[:, None]) ** 2) * w).sum(1) / safe)
        exg_mean = (level.exg[idx] * w).sum(1) / safe
        ctx = (exg_mean, val_mean, val_std)
    return geo, 1.0 - vert, ctx


class _CoreCloud:
    """A cloud reduced to voxel centroids (core points) with the voxel level of each radius."""

    def __init__(self, xyz, rgb, normals, config: FeatureConfig, *, voxel: float | None = None, ground=None,
                 max_core_points: int = MAX_CORE_POINTS, n_jobs: int = N_JOBS, progress=None):
        prog = as_progress(progress)
        xyz = np.asarray(xyz, dtype=float).reshape(-1, 3)
        if len(xyz) < 10:
            raise ValueError("the cloud has too few points")
        self.n_jobs = max(1, int(n_jobs))
        self.offset = xyz.min(0)
        prog(0.0, f"Voxel grid of {len(xyz):,} points")
        if voxel is None:
            voxel = estimate_spacing(xyz) * max(1.0, math.sqrt(len(xyz) / max_core_points))
        voxel = float(voxel)
        for attempt in range(8):
            n_vox, self.index = _voxel_index(xyz, self.offset, voxel)
            if n_vox <= max_core_points or attempt == 7:
                break
            voxel *= max(1.1, 1.05 * math.sqrt(n_vox / max_core_points))
        self.voxel = voxel
        self.counts = np.bincount(self.index).astype(np.float64)
        self.n = len(self.counts)
        self.xyz = np.column_stack([np.bincount(self.index, weights=xyz[:, d] - self.offset[d]) for d in range(3)])
        self.xyz /= self.counts[:, None]
        if not config.scales:
            config.scales = tuple(_round2(voxel * f) for f in SCALE_FACTORS)
        if config.ground_cell is None:
            config.ground_cell = _round2(max(config.scales[0] / 2, 2 * voxel))
        if config.ground_window is None:
            config.ground_window = _round2(max(1.0, 10 * config.scales[0]))
        if voxel > config.scales[0] / 2:
            log.warning("core voxel %.3g m is coarse for the first radius %.3g m: few neighbours", voxel,
                        config.scales[0])
        self.config = config
        prog(0.3, f"{self.n:,} core points (voxel {voxel:.3g} m)")
        self.colour = None
        if config.colour:
            if rgb is None:
                raise ValueError("this classifier uses colour, the cloud has none")
            self.colour = _weighted_mean(self.index, np.asarray(rgb).reshape(-1, 3), None, self.counts)
        self.normal_z = None
        if config.normals:
            if normals is None:
                log.warning("the cloud has no normals: using those of the first neighbourhood")
            else:
                nrm = np.asarray(normals, dtype=np.float64).reshape(-1, 3)
                nz = np.abs(nrm[:, 2]) / np.maximum(np.linalg.norm(nrm, axis=1), 1e-12)
                self.normal_z = np.bincount(self.index, weights=nz) / self.counts
        self.levels = self._levels()
        prog(0.6, "Height above ground")
        self.hag = self._height(ground, xyz) if config.height else None
        prog(1.0, "Neighbourhoods ready")

    def _levels(self) -> list[_Level]:
        from scipy.spatial import cKDTree

        value = exg = None
        if self.colour is not None:
            cf = _colour_features(self.colour)
            value, exg = cf[:, 3], cf[:, 7]
        cache: dict[float, _Level] = {}
        levels = []
        for r in self.config.scales:
            v = r / self.config.ratio
            if v <= 1.5 * self.voxel:
                v = self.voxel
            if v not in cache:
                if v == self.voxel:
                    lv = _Level(v, self.xyz, None, value, exg)
                else:
                    n_vox, inv = _voxel_index(self.xyz, np.zeros(3), v)
                    w = np.bincount(inv, weights=self.counts, minlength=n_vox)
                    lv = _Level(v, _weighted_mean(inv, self.xyz, self.counts, w), None)
                    if self.colour is not None:
                        cf = _colour_features(_weighted_mean(inv, self.colour, self.counts, w))
                        lv.value, lv.exg = cf[:, 3], cf[:, 7]
                lv.tree = cKDTree(lv.xyz)
                cache[v] = lv
            levels.append(cache[v])
        return levels

    def _height(self, ground, xyz: np.ndarray) -> np.ndarray:
        """Height of the core points above the ground: from the progressive morphological
        filter (``ground=None``), a DTM GeoTIFF (path) or ground elevations under each point."""
        if ground is None:
            from .classify import ground_surface, height_above_surface, noise_mask

            ok = ~noise_mask(self.xyz) if self.n > 50 else np.ones(self.n, bool)
            surface, origin, cell = ground_surface(self.xyz[ok], cell=self.config.ground_cell,
                                                   max_window=self.config.ground_window)
            return height_above_surface(self.xyz, surface, origin, cell)
        if isinstance(ground, (str, os.PathLike)):
            z = _sample_raster(ground, self.xyz[:, 0] + self.offset[0], self.xyz[:, 1] + self.offset[1])
            hag = self.xyz[:, 2] + self.offset[2] - z
            if not np.isfinite(hag).all():
                log.warning("%d core points outside the DTM: height above ground set to 0",
                            int((~np.isfinite(hag)).sum()))
                hag = np.where(np.isfinite(hag), hag, 0.0)
            return hag
        g = np.asarray(ground, dtype=float).reshape(-1)
        if len(g) != len(xyz):
            raise ValueError("ground must give one elevation per point")
        return np.bincount(self.index, weights=xyz[:, 2] - g) / self.counts

    def features(self, ids: np.ndarray) -> np.ndarray:
        """Feature matrix (float32) of the core points ``ids``; call it on batches of ``BLOCK``."""
        cfg = self.config
        q = self.xyz[ids]
        out = np.empty((len(ids), len(cfg.names)), np.float32)
        g = len(GEOMETRIC_FEATURES)
        normal_z = ctx = None
        for s, (r, level) in enumerate(zip(cfg.scales, self.levels)):
            geo, nz, c = _neighbourhood(q, level, r, cfg.k, self.n_jobs, context=s == 0 and cfg.colour)
            out[:, s * g:(s + 1) * g] = geo
            if s == 0:
                normal_z, ctx = nz, c
        col = len(cfg.scales) * g
        if cfg.height:
            out[:, col] = self.hag[ids]
            col += 1
        if cfg.normals:
            out[:, col] = normal_z if self.normal_z is None else self.normal_z[ids]
            col += 1
        if cfg.colour:
            cf = _colour_features(self.colour[ids])
            out[:, col:col + len(COLOUR_FEATURES)] = cf
            col += len(COLOUR_FEATURES)
            exg_mean, val_mean, val_std = ctx
            out[:, col:col + 3] = np.column_stack([exg_mean, cf[:, 3] - val_mean, val_std])
        return out

    def all_features(self, progress=None) -> np.ndarray:
        prog = as_progress(progress)
        out = np.empty((self.n, len(self.config.names)), np.float32)
        for s in range(0, self.n, BLOCK):
            ids = np.arange(s, min(s + BLOCK, self.n))
            out[ids] = self.features(ids)
            prog(ids[-1] / max(self.n - 1, 1), f"Features {ids[-1] + 1:,}/{self.n:,}")
        return out


def _sample_raster(path, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Values of band 1 at the given world coordinates (nearest cell, NaN outside or nodata)."""
    import rasterio

    with rasterio.open(path) as src:
        band = src.read(1).astype(np.float64)
        if src.nodata is not None:
            band[band == src.nodata] = np.nan
        inv = ~src.transform
    cols = np.floor(inv.a * x + inv.b * y + inv.c).astype(np.int64)
    rows = np.floor(inv.d * x + inv.e * y + inv.f).astype(np.int64)
    inside = (rows >= 0) & (rows < band.shape[0]) & (cols >= 0) & (cols < band.shape[1])
    out = np.full(len(x), np.nan)
    out[inside] = band[rows[inside], cols[inside]]
    return out


def point_features(xyz: np.ndarray, rgb: np.ndarray | None = None, normals: np.ndarray | None = None,
                   scales=None, ground=None, progress: ProgressCallback | None = None, *,
                   voxel: float | None = None, max_core_points: int = MAX_CORE_POINTS,
                   n_jobs: int = N_JOBS) -> tuple[np.ndarray, list[str]]:
    """Feature matrix ``(N, F)`` (float32) of every point and the feature names.

    ``xyz`` with z up (world coordinates: large offsets are removed internally);
    ``rgb`` uint8 enables the colour features, ``normals`` the ``normal_z`` feature.
    ``scales``: neighbourhood radii in metres (default :func:`suggest_scales`).
    ``ground``: ``None`` = height above ground from the progressive morphological filter,
    ``False`` = no height feature, a GeoTIFF path = DTM in the same CRS, an array = ground
    elevation under each point. Points of the same core voxel (``voxel``, default the point
    spacing, coarser beyond ``max_core_points``) share their features. The full matrix
    takes ``4 * N * F`` bytes: :func:`train` and :func:`predict` work in batches instead."""
    config = FeatureConfig(scales=tuple(scales or ()), colour=rgb is not None, height=ground is not False,
                           normals=normals is not None)
    prog = as_progress(progress)
    cc = _CoreCloud(xyz, rgb, normals, config, voxel=voxel, ground=None if ground is False else ground,
                    max_core_points=max_core_points, n_jobs=n_jobs, progress=prog.sub(0.0, 0.3))
    feats = cc.all_features(prog.sub(0.3, 0.7))
    return feats[cc.index], config.names


# ---------------------------------------------------------------- model
@dataclass
class ClassifierModel:
    """A trained point classifier: the scikit-learn estimator and what is needed to describe
    a new cloud exactly as the training one (feature configuration, core voxel)."""

    estimator: Any
    config: FeatureConfig
    voxel: float                     # core voxel of the training cloud (m)
    classes: list[int]
    class_names: dict[int, str]
    feature_names: list[str]
    stats: dict = field(default_factory=dict)  # training statistics (see train())
    info: dict = field(default_factory=dict)   # versions, date, training data

    @property
    def scales(self) -> list[float]:
        """Neighbourhood radii in metres."""
        return list(self.config.scales)

    @property
    def method(self) -> str:
        return "gradient_boosting" if type(self.estimator).__name__.startswith("HistGradient") else "random_forest"

    @property
    def oob_score(self) -> float | None:
        """Out-of-bag accuracy (random forest), ``None`` otherwise."""
        return self.stats.get("validation_accuracy") if self.stats.get("validation") == "out-of-bag" else None

    @property
    def validation_accuracy(self) -> float | None:
        """Accuracy on samples not used to fit (out-of-bag or held out)."""
        return self.stats.get("validation_accuracy")

    @property
    def training_accuracy(self) -> float | None:
        return self.stats.get("training_accuracy")

    @property
    def confusion_matrix(self) -> np.ndarray:
        """Validation confusion matrix: rows = true class, columns = predicted (order of ``classes``)."""
        return np.asarray(self.stats.get("confusion_matrix", []), dtype=np.int64)

    def predict(self, xyz, rgb=None, normals=None, ground=None, **kwargs) -> tuple[np.ndarray, np.ndarray]:
        """Same as :func:`predict` with this model."""
        return predict(self, xyz, rgb, normals, ground, **kwargs)

    def metadata(self) -> dict:
        return {
            "format": MODEL_FORMAT, "format_version": MODEL_VERSION, "method": self.method,
            "classes": [{"code": int(c), "name": self.class_names.get(int(c), f"Class {c}")} for c in self.classes],
            "feature_names": list(self.feature_names), "features": self.config.to_dict(), "voxel": self.voxel,
            "stats": self.stats, "info": self.info,
        }

    def save(self, path: str | os.PathLike) -> Path:
        """Write the classifier to one file (suggested suffix ``.ppgclf``): a ZIP archive with
        ``model.json`` (format version, classes, features, radii, statistics, versions) and
        ``estimator.pkl`` (the scikit-learn estimator, pickled)."""
        path = Path(path)
        payload = pickle.dumps(self.estimator, protocol=4)
        tmp = path.with_name(path.name + ".part")
        try:
            with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.comment = MODEL_FORMAT.encode("ascii")
                zf.writestr("model.json", json.dumps(self.metadata(), indent=1, ensure_ascii=False))
                zf.writestr("estimator.pkl", payload)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        return path

    @classmethod
    def load(cls, path: str | os.PathLike) -> "ClassifierModel":
        """Read a file written by :meth:`save`. The estimator is unpickled with an allow-list
        (scikit-learn classes and NumPy arrays only): a file that tries to run anything else
        is refused. Files from another scikit-learn version are attempted with a warning."""
        sklearn = _sklearn()
        from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier

        path = Path(path)
        meta = read_model_info(path)
        with zipfile.ZipFile(path) as zf:
            payload = zf.read("estimator.pkl")
        trained_with = meta.get("info", {}).get("sklearn_version", "?")
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                est = _ModelUnpickler(io.BytesIO(payload)).load()
        except pickle.UnpicklingError as exc:
            raise ValueError(f"{path.name}: refused, {exc}") from exc
        except Exception as exc:
            raise ValueError(f"{path.name}: cannot read the estimator (saved with scikit-learn {trained_with}, "
                             f"installed {sklearn.__version__}): {exc}") from exc
        for w in caught:
            log.warning("%s: %s", path.name, w.message)
        if not isinstance(est, (RandomForestClassifier, HistGradientBoostingClassifier)):
            raise ValueError(f"{path.name}: unexpected estimator {type(est).__name__}")
        classes = [int(c["code"]) for c in meta["classes"]]
        if [int(c) for c in est.classes_] != classes:
            raise ValueError(f"{path.name}: the classes of the estimator do not match the metadata")
        config = FeatureConfig.from_dict(meta["features"])
        if config.names != list(meta["feature_names"]):
            raise ValueError(f"{path.name}: unknown feature set (made by a newer version?)")
        return cls(est, config, float(meta["voxel"]), classes, {int(c["code"]): c["name"] for c in meta["classes"]},
                   list(meta["feature_names"]), meta.get("stats", {}), meta.get("info", {}))

    def summary(self) -> str:
        """Readable description: method, radii, validation accuracy, per-class figures."""
        st = self.stats
        method = "Random forest" if self.method == "random_forest" else "Gradient boosting"
        lines = [f"{method}, {len(self.feature_names)} features, radii "
                 + " / ".join(f"{r:g}" for r in self.scales) + f" m (core voxel {self.voxel:.3g} m)"]
        if st:
            acc = st.get("validation_accuracy")
            lines.append(f"Trained on {st.get('n_samples', 0):,} samples from {st.get('n_labelled_points', 0):,} "
                         "labelled points" + ("" if acc is None else f"; {st.get('validation')} accuracy {acc:.1%}"))
        per = st.get("per_class", {})
        lines.append(f"{'class':<44}{'samples':>9}{'precision':>11}{'recall':>8}")
        for c in self.classes:
            p = per.get(str(c), {})
            name = f"{c:>3} {self.class_names.get(c, '')}"[:43]
            pr, rc = p.get("precision"), p.get("recall")
            lines.append(f"{name:<44}{p.get('samples', 0):>9,}{'' if pr is None else f'{pr:.1%}':>11}"
                         f"{'' if rc is None else f'{rc:.1%}':>8}")
        imp = st.get("feature_importance")
        if imp:
            top = sorted(imp.items(), key=lambda kv: -kv[1])[:5]
            lines.append("Most informative features: " + ", ".join(f"{k} ({v:.2f})" for k, v in top))
        return "\n".join(lines)


def read_model_info(path: str | os.PathLike) -> dict:
    """Metadata of a classifier file (classes, features, radii, statistics) without loading
    the estimator: cheap and safe, for file dialogs."""
    path = Path(path)
    try:
        with zipfile.ZipFile(path) as zf:
            meta = json.loads(zf.read("model.json").decode("utf-8"))
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path.name} is not a classifier file") from exc
    if meta.get("format") != MODEL_FORMAT:
        raise ValueError(f"{path.name} is not a classifier file")
    if int(meta.get("format_version", 0)) > MODEL_VERSION:
        raise ValueError(f"{path.name} was written by a newer version of pyarchinit-photogrammetry")
    return meta


# Objects a classifier file may contain: scikit-learn classes (and the helpers that rebuild
# their Cython parts), NumPy arrays, dtypes and random generators. Nothing that can run code.
_ALLOWED_GLOBALS = {
    ("numpy", "dtype"), ("numpy", "ndarray"),
    ("numpy.core.multiarray", "_reconstruct"), ("numpy._core.multiarray", "_reconstruct"),
    ("numpy.core.multiarray", "scalar"), ("numpy._core.multiarray", "scalar"),
    ("numpy.core.numeric", "_frombuffer"), ("numpy._core.numeric", "_frombuffer"),
    ("numpy.random._pickle", "__randomstate_ctor"), ("numpy.random._pickle", "__bit_generator_ctor"),
    ("numpy.random._pickle", "__generator_ctor"), ("numpy.random.bit_generator", "__pyx_unpickle_SeedSequence"),
    ("builtins", "set"), ("builtins", "frozenset"), ("builtins", "slice"), ("builtins", "complex"),
    ("builtins", "bytearray"), ("builtins", "object"), ("copyreg", "_reconstructor"),
    ("collections", "OrderedDict"),
}


class _ModelUnpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str):
        # no dotted names: they would reach the attributes of a module (sklearn.x, "os.system")
        if "." not in name:
            cython = module.startswith("sklearn.") and name.startswith("__pyx_unpickle_")
            if (module, name) in _ALLOWED_GLOBALS or cython:
                return super().find_class(module, name)
            if module.startswith(("sklearn.", "numpy.random.")):
                obj = super().find_class(module, name)
                if isinstance(obj, type) and obj.__module__.startswith(("sklearn.", "numpy.random")):
                    return obj
        raise pickle.UnpicklingError(f"{module}.{name} is not allowed in a classifier file")


# ---------------------------------------------------------------- training
def _core_labels(index: np.ndarray, codes: np.ndarray, labelled: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """One sample per core point holding labelled points: the class most of them carry."""
    pair = index[labelled].astype(np.int64) * 256 + codes[labelled].astype(np.int64)
    uniq, cnt = np.unique(pair, return_counts=True)
    core, lab = uniq // 256, uniq % 256
    order = np.lexsort((-cnt, core))
    core, lab = core[order], lab[order]
    first = np.r_[True, core[1:] != core[:-1]]
    return core[first], lab[first]


def _thread_limit(n_jobs: int):
    try:
        from threadpoolctl import threadpool_limits

        return threadpool_limits(limits=n_jobs, user_api="openmp")
    except Exception:  # pragma: no cover - threadpoolctl comes with scikit-learn
        return contextlib.nullcontext()


def _fit_forest(X, y, classes, n_estimators, balance, n_jobs, random_state, params, progress):
    from sklearn.ensemble import RandomForestClassifier

    # "balanced_subsample" weighs the classes inside each tree; "balanced" would draw the
    # bootstrap by weight and leave the samples of small classes almost never out of bag
    options = dict(min_samples_leaf=3, max_features="sqrt", class_weight="balanced_subsample" if balance else None,
                   max_samples=TREE_SAMPLES if len(y) > TREE_SAMPLES else None)
    options.update(params)
    est = RandomForestClassifier(n_estimators=1, warm_start=True, n_jobs=n_jobs, random_state=random_state, **options)
    total = int(n_estimators or 100)
    step = max(1, total // 10)
    sizes = list(range(step, total, step)) + [total]
    for i, n in enumerate(sizes):  # trees added in batches: progress and cancellation while fitting
        est.set_params(n_estimators=n, oob_score=n == total)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)  # class_weight with warm_start; few trees for out-of-bag
            est.fit(X, y)
        progress((i + 1) / len(sizes), f"Random forest: {n}/{total} trees")
    est.set_params(warm_start=False)
    oob = est.oob_decision_function_
    ok = np.isfinite(oob).all(1) & (oob.sum(1) > 0)
    return est, y[ok], est.classes_[oob[ok].argmax(1)], "out-of-bag"


def _fit_boosting(X, y, classes, n_estimators, balance, n_jobs, random_state, params, progress):
    from sklearn.ensemble import HistGradientBoostingClassifier

    rng = np.random.default_rng(random_state)
    test = np.zeros(len(y), bool)
    for c in classes:  # stratified hold-out (20 %) for the statistics
        sel = np.nonzero(y == c)[0]
        if len(sel) >= 5:
            test[rng.choice(sel, max(1, len(sel) // 5), replace=False)] = True
    options = dict(max_iter=int(n_estimators or 200), class_weight="balanced" if balance else None,
                   random_state=random_state)
    options.update(params)
    progress(0.0, "Gradient boosting")
    est = HistGradientBoostingClassifier(**options)
    with _thread_limit(n_jobs):
        est.fit(X[~test], y[~test])
        pred = est.predict(X[test]) if test.any() else np.zeros(0, y.dtype)
    progress(1.0, f"Gradient boosting: {est.n_iter_} iterations")
    return est, y[test], pred, "held-out 20%"


def _statistics(est, X, y, classes, y_true, y_pred, validation, names, n_jobs) -> dict:
    order = np.asarray(classes)
    cm = np.zeros((len(classes), len(classes)), np.int64)
    np.add.at(cm, (np.searchsorted(order, y_true), np.searchsorted(order, y_pred)), 1)
    with _thread_limit(n_jobs):
        train_acc = float((est.predict(X) == y).mean())
    per = {}
    for i, c in enumerate(classes):
        tp = cm[i, i]
        per[str(c)] = {"samples": int((y == c).sum()),
                       "precision": float(tp / cm[:, i].sum()) if cm[:, i].sum() else None,
                       "recall": float(tp / cm[i].sum()) if cm[i].sum() else None}
        p, r = per[str(c)]["precision"], per[str(c)]["recall"]
        per[str(c)]["f1"] = float(2 * p * r / (p + r)) if p and r else None
    stats = {"training_accuracy": train_acc, "validation": validation,
             "validation_accuracy": float(np.trace(cm) / cm.sum()) if cm.sum() else None,
             "confusion_matrix": cm.tolist(), "per_class": per}
    if hasattr(est, "feature_importances_"):
        stats["feature_importance"] = {n: round(float(v), 5) for n, v in zip(names, est.feature_importances_)}
    return stats


def train(xyz: np.ndarray, codes: np.ndarray, rgb: np.ndarray | None = None, normals: np.ndarray | None = None,
          scales=None, ground=None, *, method: str = "random_forest", n_estimators: int | None = None,
          max_per_class: int = 20_000, balance: bool = True, class_names: dict[int, str] | None = None,
          voxel: float | None = None, max_core_points: int = MAX_CORE_POINTS, n_jobs: int = N_JOBS,
          random_state: int = 0, progress: ProgressCallback | None = None, **params) -> ClassifierModel:
    """Train a classifier on the labelled points of a cloud.

    ``codes``: class (1-255) of each point, 0 or negative = unlabelled. Features are those
    of :func:`point_features` (same ``rgb``, ``normals``, ``scales``, ``ground``). Each
    core voxel gives one sample (the class most of its labelled points carry); classes are
    balanced by keeping at most ``max_per_class`` samples each and, with ``balance``,
    weighting them inversely to their size. ``method``: ``"random_forest"``
    (``n_estimators`` trees, default 100, each grown on at most ``TREE_SAMPLES`` samples;
    out-of-bag validation) or ``"gradient_boosting"`` (``n_estimators`` iterations, default
    200 with early stopping; validated on 20 % held out; smaller files, often more accurate
    on hard cases, slower to train). Extra keyword arguments go to the scikit-learn
    estimator. Deterministic for a given ``random_state``.
    """
    sklearn = _sklearn()
    if method not in METHODS:
        raise ValueError(f"method must be one of {METHODS}")
    prog = as_progress(progress)
    xyz = np.asarray(xyz, dtype=float).reshape(-1, 3)
    codes = np.asarray(codes).reshape(-1)
    if len(codes) != len(xyz):
        raise ValueError("one class code per point is needed")
    labelled = codes > 0
    if labelled.any() and codes[labelled].max() > 255:
        raise ValueError("class codes go from 1 to 255")
    if len(np.unique(codes[labelled])) < 2:
        raise ValueError("label points of at least two classes")
    t0 = time.perf_counter()
    config = FeatureConfig(scales=tuple(scales or ()), colour=rgb is not None, height=ground is not False,
                           normals=normals is not None)
    cc = _CoreCloud(xyz, rgb, normals, config, voxel=voxel, ground=None if ground is False else ground,
                    max_core_points=max_core_points, n_jobs=n_jobs, progress=prog.sub(0.0, 0.25))
    ids, y = _core_labels(cc.index, codes, labelled)
    rng = np.random.default_rng(random_state)
    keep = []
    for c in np.unique(y):
        sel = np.nonzero(y == c)[0]
        if len(sel) > max_per_class:
            sel = rng.choice(sel, max_per_class, replace=False)
        keep.append(sel)
    keep = np.sort(np.concatenate(keep))
    ids, y = ids[keep], y[keep].astype(np.int64)
    classes = [int(c) for c in np.unique(y)]
    if len(classes) < 2:
        raise ValueError("label points of at least two classes")
    fprog = prog.sub(0.25, 0.35)
    X = np.empty((len(ids), len(config.names)), np.float32)
    for s in range(0, len(ids), BLOCK):
        X[s:s + BLOCK] = cc.features(ids[s:s + BLOCK])
        fprog(min(1.0, (s + BLOCK) / len(ids)), f"Features of {min(s + BLOCK, len(ids)):,}/{len(ids):,} samples")
    fit = _fit_forest if method == "random_forest" else _fit_boosting
    est, y_true, y_pred, validation = fit(X, y, classes, n_estimators, balance, n_jobs, random_state, params,
                                          prog.sub(0.6, 0.38))
    stats = _statistics(est, X, y, classes, y_true, y_pred, validation, config.names, n_jobs)
    lab_codes, lab_counts = np.unique(codes[labelled], return_counts=True)
    for c, n in zip(lab_codes, lab_counts):
        stats["per_class"].setdefault(str(int(c)), {"samples": 0})["labelled_points"] = int(n)
    stats.update(method=method, n_labelled_points=int(labelled.sum()), n_samples=int(len(y)),
                 n_points=int(len(xyz)), training_time_s=round(time.perf_counter() - t0, 2))
    from .. import __version__

    names = {**ASPRS_CLASSES, **ARCHAEO_CLASSES, **(class_names or {})}
    model = ClassifierModel(est, config, cc.voxel, classes, {c: names.get(c, f"Class {c}") for c in classes},
                            config.names, stats,
                            {"created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                             "package_version": __version__, "sklearn_version": sklearn.__version__,
                             "numpy_version": np.__version__})
    acc = stats["validation_accuracy"]
    prog(1.0, f"Classifier trained: {len(classes)} classes, {len(y):,} samples"
         + ("" if acc is None else f", {validation} accuracy {acc:.1%}"))
    return model


def predict(model: ClassifierModel, xyz: np.ndarray, rgb: np.ndarray | None = None,
            normals: np.ndarray | None = None, ground=None, *, min_confidence: float = 0.0,
            voxel: float | None = None, max_core_points: int = MAX_CORE_POINTS, n_jobs: int = N_JOBS,
            progress: ProgressCallback | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Classify a cloud: ``(codes uint8 (N,), confidence float32 (N,))``.

    The confidence is the probability of the chosen class (fraction of the trees of a
    forest). Points below ``min_confidence`` get class 1 (unclassified). The features use
    the model radii; the core voxel defaults to the training one (coarser beyond
    ``max_core_points``). ``ground`` as in :func:`point_features`."""
    _sklearn()
    if model.config.height and ground is False:
        raise ValueError("this classifier uses the height above ground")
    prog = as_progress(progress)
    config = FeatureConfig.from_dict(model.config.to_dict())
    cc = _CoreCloud(xyz, rgb, normals, config, voxel=voxel or model.voxel, ground=ground,
                    max_core_points=max_core_points, n_jobs=n_jobs, progress=prog.sub(0.0, 0.2))
    if config.names != model.feature_names:
        raise ValueError("the features of this cloud do not match the classifier")
    est = model.estimator
    if hasattr(est, "n_jobs"):
        est.n_jobs = n_jobs
    classes = np.asarray(model.classes, dtype=np.uint8)
    core_codes = np.empty(cc.n, np.uint8)
    core_conf = np.empty(cc.n, np.float32)
    p = prog.sub(0.2, 0.8)
    with _thread_limit(n_jobs):
        for s in range(0, cc.n, BLOCK):
            ids = np.arange(s, min(s + BLOCK, cc.n))
            proba = est.predict_proba(cc.features(ids))
            best = proba.argmax(1)
            core_codes[ids] = classes[best]
            core_conf[ids] = proba[np.arange(len(ids)), best]
            p((ids[-1] + 1) / cc.n, f"Classified {ids[-1] + 1:,}/{cc.n:,} core points")
    codes, conf = core_codes[cc.index], core_conf[cc.index]
    if min_confidence > 0:
        codes[conf < min_confidence] = UNCLASSIFIED
    return codes, conf


# ---------------------------------------------------------------- chunk helpers
def _world_cloud(chunk) -> tuple[PointCloud, PointCloud]:
    from ..engine.dem import ensure_level

    ensure_level(chunk)
    pc = chunk.point_cloud
    if pc is None:
        raise RuntimeError("build the point cloud first")
    return pc, pc.transformed(chunk.transform)


def train_on_chunk(chunk, *, labels: str = "manual", use_normals: bool = False, ground=None,
                   progress: ProgressCallback | None = None, **kwargs) -> ClassifierModel:
    """Train a classifier on the dense cloud of a chunk (world coordinates, z up).

    ``labels="manual"`` learns from the points labelled by hand (:func:`.classify.set_class`),
    ``"all"`` from every classified point (class above 1, or labelled by hand). Other
    arguments as in :func:`train`."""
    from .classify import class_names

    pc, world = _world_cloud(chunk)
    manual = pc.manual if pc.manual is not None else np.zeros(len(pc), bool)
    if labels == "manual":
        if not manual.any():
            raise RuntimeError("no point was labelled by hand")
        use = manual
    elif labels == "all":
        use = manual | (pc.classification > UNCLASSIFIED)
    else:
        raise ValueError("labels must be 'manual' or 'all'")
    codes = np.where(use, pc.classification, 0)
    model = train(world.xyz, codes, rgb=pc.rgb, normals=world.normals if use_normals else None, ground=ground,
                  class_names=class_names(chunk), progress=progress, **kwargs)
    doc = getattr(chunk, "document", None)
    model.info["trained_on"] = {"project": Path(doc.path).name if doc is not None and doc.path else None,
                                "chunk": chunk.label, "crs": chunk.crs, "points": len(pc), "labels": labels}
    return model


def classify_chunk_with_model(chunk, model: ClassifierModel, *, overwrite_manual: bool = False,
                              min_confidence: float = 0.0, ground=None, progress: ProgressCallback | None = None,
                              **kwargs) -> tuple[PointCloud, np.ndarray]:
    """Classify the chunk dense cloud with a trained classifier and store it.

    Points labelled by hand keep their class (confidence 1) unless ``overwrite_manual``.
    Returns the classified cloud (internal frame, as stored) and the confidence of each
    point. Other arguments as in :func:`predict`."""
    from .classify import keep_manual_labels

    pc, world = _world_cloud(chunk)
    normals = world.normals if model.config.normals else None
    codes, conf = predict(model, world.xyz, pc.rgb, normals, ground, min_confidence=min_confidence,
                          progress=progress, **kwargs)
    keep_manual_labels(pc, codes, overwrite_manual)
    if pc.manual is not None and not overwrite_manual:
        conf[pc.manual] = 1.0
    pc.classification = codes
    chunk.point_cloud = pc
    names = chunk.meta.setdefault("classes", {})
    for c, name in model.class_names.items():
        names.setdefault(str(c), name)
    return pc, conf


__all__ = ["FeatureConfig", "ClassifierModel", "estimate_spacing", "suggest_scales", "point_features", "train",
           "predict", "read_model_info", "train_on_chunk", "classify_chunk_with_model", "MODEL_SUFFIX"]
