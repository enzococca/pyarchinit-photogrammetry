"""Automatic ties between chunks: the same ground found in the orthophotos (or the relief of the DEMs) of two chunks,
proposed as ties, never stored until accepted.

Several flights of one site (years, seasons, the blocks of a dive) are placed together by their ties
(:mod:`.ties`): the same points in two chunks, then one least squares. Picking them by hand is slow; this module
finds them, in four steps that the window and ``ppg ties auto`` show one by one:

1. **Candidate pairs** (:func:`find_pairs`): for every two chunks with a DEM, the share of the smaller footprint (the
   cells with data of its DEM, on a coarse grid in the system of the first) that the other covers where the chunks
   are placed now, the other's footprint grown by ``margin`` metres (how far off the placement may be: a drone's GPS
   puts flights some metres apart). Pairs below :data:`MIN_OVERLAP` are not candidates; chunks of one group
   (:mod:`..core.groups`) are not paired (they move as one block); a local frame with a system of the world, or two
   frames that the program converts by epoch and GDAL would not (:func:`.mosaic.frame_problem`), are refused.
2. **Matching** (:func:`match`): both rasters read on north-up grids of the same cell in the system of the first
   chunk (a raster built in another transform of its chunk moved through its stamp, as the 2D view and the merged
   products place it), only the part over the other chunk (grown by ``margin``; ``margin=None``: the whole rasters,
   for chunks whose placement says nothing - two unrelated local frames). ``source``: ``"ortho"`` the grey of the
   orthophoto with a local contrast equalisation (CLAHE: other light, other season), ``"relief"`` a shading of the
   DEM from four directions (the shape of the ground when the colours changed: sand moved, water, vegetation). SIFT
   points, the ratio test and the mutual best match; each match a 3D point in each chunk (the DEM under it).
3. **Robust estimate and refinement**: RANSAC of a 3D similarity (three matches, inliers within
   :data:`RANSAC_CELLS` cells), then the image of the second chunk warped onto the first by that estimate and a
   regular grid of patches matched by normalised cross-correlation with sub-pixel peaks (many more, evenly spread
   and more precise points than SIFT's), the similarity fitted again with the outliers (over :data:`OUTLIER_K` NMAD)
   left out; twice.
4. **Residual report**: the similarity that would bring the second chunk onto the first (rotation, scale in ppm,
   shift at the middle of the overlap), the residuals of the points kept (RMS 3D, in plan, in height, NMAD) and the
   verdict: ``"ok"`` or ``"rejected"`` with the reasons (too few matches that agree, a share of agreeing matches that
   chance gives too, points on a line or in a corner, a scale beyond :data:`SCALE_RANGE`). A wrong pair - two chunks
   that do not show the same ground - is rejected, never proposed.

Only :func:`accept` changes the project: up to ``max_ties`` of the points kept, spread over the overlap (farthest
point first), become ties (:func:`.ties.add_tie`, markers "auto N"), with the precision of the fit (the RMS of the
points kept, at least half a cell): then the joint solve, its checks and *Applica* as for the ties made by hand. A
pair accepted again replaces the ties it made before. The pairs accepted are kept in ``doc.meta["auto_ties"]``
(what was found, when, which ties) for the report. No Qt here.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

import cv2
import numpy as np

from ..core import crs as crs_mod
from ..core import geometry
from ..core.progress import Progress, as_progress
from . import ties

if TYPE_CHECKING:  # pragma: no cover
    from ..core.chunk import Chunk
    from ..core.document import Document

META_KEY = "auto_ties"
LABEL = "auto"  # the label of the markers of an automatic tie: data of the project (as "legame"), not UI text
SOURCES = ("ortho", "relief")
MARGIN = 5.0  # metres: how far off the placement of a chunk may be (where matches are looked for)
MIN_OVERLAP = 0.05  # share of the smaller footprint covered by the other: below it no candidate
FOOTPRINT_SIDE = 256  # cells a side of the coarse grid of the footprints
MAX_SIDE = 2500  # pixels a side of a raster read for the matching (the cell grows to stay within it)
SIFT_FEATURES = 8000
RATIO = 0.8  # Lowe's ratio test
RANSAC_CELLS = 4.0  # a match agrees with a similarity within this many cells (3D)
RANSAC_ITERATIONS = 4000
MIN_MATCHES = 12  # matches that agree, at least, for a pair
MIN_SHARE = 0.15  # ... and this share of the matches, unless they are 3 x MIN_MATCHES
SCALE_RANGE = (0.8, 1.25)  # the scale between two chunks of one site
MIN_SPREAD_CELLS = 20.0  # the agreeing points spread at least this many cells across (smaller axis, 2 sigma)
PATCH = 31  # pixels a side of a patch of the refinement
SEARCH = 6  # pixels searched around where the estimate puts a patch
PATCHES = 600  # patches looked for over the overlap (their step between MIN_STEP and PATCH pixels)
MIN_STEP = 8
MIN_NCC = 0.6  # a patch matched with a correlation below this is not used
MIN_TEXTURE = 3.0  # grey levels: a patch flatter than this (standard deviation) is not matched
OUTLIER_K = 3.0  # NMAD: a point further off than this from the fit is left out
REFINE_ROUNDS = 2
MAX_TIES = 12  # the ties an accepted pair gives
MIN_PRECISION = 0.002  # metres: the least precision of an automatic tie
_NUMBERED = re.compile(rf"^{LABEL} (\d+)$")

# code -> (Italian template, English template): as :data:`.ties.MESSAGES` (the window: tr(it, *args))
MESSAGES: dict[str, tuple[str, str]] = {
    "few_matches": ("{0} punti concordano (servono almeno {1})", "{0} points agree (at least {1} are needed)"),
    "by_chance": ("solo {0} punti su {1} concordano ({2:.0%}): un accordo che il caso dà anche fra immagini diverse",
                  "only {0} of {1} points agree ({2:.0%}): an agreement chance gives between different images too"),
    "clustered": ("i punti che concordano sono su una linea o in un angolo (larghezza {0:.2f} m, ne servono "
                  "{1:.2f})",
                  "the points that agree lie on a line or in a corner (width {0:.2f} m, {1:.2f} are needed)"),
    "scale": ("scala {0:.4f} fuori da {1:g}-{2:g}", "scale {0:.4f} outside {1:g}-{2:g}"),
    "not_refined": ("raffinamento non riuscito: restano i punti SIFT ({0})",
                    "refinement failed: the SIFT points are kept ({0})"),
    "no_overlap": ("le due immagini non hanno parti comuni dove i chunk sono ora (prova la ricerca su tutto il "
                   "raster)",
                   "the two images have nothing in common where the chunks are now (try the search over the whole "
                   "raster)"),
    "stale_raster": ("{0}: il DEM o l'ortofoto sono stati costruiti prima di un'inclinazione del chunk e sono collocati "
                     "solo in pianta (fino a {1:.3f} m fuori posto qui): ricostruiscili prima di fidarti di questa "
                     "coppia",
                     "{0}: the DEM or the orthophoto were built before the chunk was tilted and are placed in plan "
                     "only (up to {1:.3f} m off here): build them again before trusting this pair"),
    "no_texture": ("nessun punto trovato nelle immagini (superficie uniforme o immagini vuote)",
                   "no points found in the images (a uniform surface or empty images)"),
}


def message(code: str, *args) -> dict:
    it, en = MESSAGES[code]
    return {"code": code, "it": it, "en": en, "args": [a.item() if isinstance(a, np.generic) else a for a in args],
            "chunk": None}


class AutoTieError(ties.TieError):
    """A pair that cannot be matched (no DEM, no orthophoto, systems that cannot be mixed)."""


# --------------------------------------------------------------------------- results
@dataclass
class PairResult:
    """What :func:`match` found for chunks ``a`` (the reference) and ``b``.

    ``transform``: the 4x4 similarity that brings the points of ``b`` (where it is now, in the system ``crs`` of
    ``a``) onto ``a``; ``rotation_deg``, ``scale`` and ``shift`` (metres, at the middle of the points kept) describe
    it. ``points_a``/``points_b``: the points kept (world of ``a``'s system: ``b`` where it is now),
    ``residuals`` their 3D residuals after the fit; ``rms``/``rms_plan``/``rms_z``/``nmad`` in metres. ``status``
    ``"ok"`` or ``"rejected"`` (``messages`` say why, :func:`message`); ``matches`` the SIFT matches, ``inliers``
    those RANSAC kept, ``refined`` the patches of the refinement kept."""

    a: int
    b: int
    a_label: str
    b_label: str
    source: str
    crs: str | None
    cell: float
    status: str = "rejected"
    messages: list = field(default_factory=list)
    matches: int = 0
    inliers: int = 0
    refined: int = 0
    transform: np.ndarray | None = None
    rotation_deg: float = 0.0
    scale: float = 1.0
    shift: float = 0.0
    rms: float = 0.0
    rms_plan: float = 0.0
    rms_z: float = 0.0
    nmad: float = 0.0
    points_a: np.ndarray = field(default_factory=lambda: np.zeros((0, 3)))
    points_b: np.ndarray = field(default_factory=lambda: np.zeros((0, 3)))
    residuals: np.ndarray = field(default_factory=lambda: np.zeros((0, 3)))
    placed: dict = field(default_factory=dict)  # the transforms of a and b when matched

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def scale_ppm(self) -> float:
        return (self.scale - 1.0) * 1e6

    def summary(self) -> dict:
        """JSON of the result without its points."""
        return {"a": self.a, "b": self.b, "a_label": self.a_label, "b_label": self.b_label, "source": self.source,
                "crs": self.crs, "cell": float(self.cell), "status": self.status, "messages": self.messages,
                "matches": int(self.matches), "inliers": int(self.inliers), "refined": int(self.refined),
                "transform": None if self.transform is None else np.asarray(self.transform, float).tolist(),
                "rotation_deg": float(self.rotation_deg), "scale": float(self.scale),
                "scale_ppm": float(self.scale_ppm), "shift": float(self.shift), "rms": float(self.rms),
                "rms_plan": float(self.rms_plan), "rms_z": float(self.rms_z), "nmad": float(self.nmad),
                "points": int(len(self.points_a))}

    def to_dict(self) -> dict:
        return {**self.summary(), "points_a": self.points_a.tolist(), "points_b": self.points_b.tolist(),
                "residuals": self.residuals.tolist()}


def render(m: dict, language: str = "en") -> str:
    return ties.render(m, language)


def report_text(result: PairResult, language: str = "en") -> str:
    """The residual report of one pair, as text (English, or Italian with the decimal comma)."""
    it = language == "it"

    def f(value: float, spec: str) -> str:
        return format(ties.Comma(value) if it else value, spec)

    head = f"{result.b_label} -> {result.a_label} ({result.source})"
    if not result.ok:
        why = "; ".join(render(m, language) for m in result.messages)
        return f"{head}: {'scartata' if it else 'rejected'} - {why}"
    if it:
        lines = [f"{head}: proposta",
                 f"  corrispondenze SIFT {result.matches}, concordi {result.inliers}, punti raffinati {result.refined}",
                 f"  rotazione {f(result.rotation_deg, '.4f')}°, scala {f(result.scale_ppm, '+.0f')} ppm, spostamento "
                 f"{f(result.shift, '.4f')} m",
                 f"  residui: RMS {f(result.rms, '.4f')} m (pianta {f(result.rms_plan, '.4f')}, quota "
                 f"{f(result.rms_z, '.4f')}), NMAD {f(result.nmad, '.4f')} m, cella {f(result.cell, '.4f')} m"]
    else:
        lines = [f"{head}: proposed",
                 f"  SIFT matches {result.matches}, agreeing {result.inliers}, refined points {result.refined}",
                 f"  rotation {result.rotation_deg:.4f}°, scale {result.scale_ppm:+.0f} ppm, shift {result.shift:.4f} m",
                 f"  residuals: RMS {result.rms:.4f} m (plan {result.rms_plan:.4f}, height {result.rms_z:.4f}), NMAD "
                 f"{result.nmad:.4f} m, cell {result.cell:.4f} m"]
    lines += ["  " + render(m, language) for m in result.messages]
    return "\n".join(lines)


# --------------------------------------------------------------------------- rasters on a grid
def _sources(chunk: "Chunk", system, need_ortho: bool):
    """The DEM (and orthophoto) of ``chunk`` placed in ``system`` (:func:`.mosaic._source`)."""
    from . import mosaic

    try:
        dem = mosaic._source(chunk, 0, "dem", system)
        ortho = mosaic._source(chunk, 0, "orthomosaic", system) if need_ortho else None
    except mosaic.MosaicError as exc:
        raise AutoTieError(exc.english, exc.italian) from None
    return dem, ortho


def _grid(bounds, cell: float):
    from affine import Affine

    left, bottom, right, top = bounds
    w = max(1, int(math.ceil((right - left) / cell)))
    h = max(1, int(math.ceil((top - bottom) / cell)))
    return Affine(cell, 0.0, left, 0.0, -cell, top), w, h


def _read(source, product: str, system, grid, w: int, h: int):
    from rasterio.windows import Window

    from . import mosaic

    reader = mosaic._Reader(source, product, system, grid, w, h)
    try:
        return reader.read(Window(0, 0, w, h))
    finally:
        reader.close()


def _grey(rgb: np.ndarray, valid: np.ndarray) -> np.ndarray:
    g = cv2.cvtColor(np.ascontiguousarray(np.moveaxis(rgb, 0, -1)), cv2.COLOR_RGB2GRAY)
    if valid.any():
        g[~valid] = int(np.median(g[valid]))
    return cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(g)


def _shade(z: np.ndarray, valid: np.ndarray, cell: float) -> np.ndarray:
    """The DEM shaded from four directions (45° high), uint8 stretched on its 1-99 %."""
    zz = np.where(valid, z, np.nanmedian(z[valid]) if valid.any() else 0.0).astype(np.float64)
    gy, gx = np.gradient(zz, cell)
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    alt = math.radians(45.0)
    shade = np.zeros_like(zz)
    for az in (315.0, 45.0, 135.0, 225.0):
        a = math.radians(az)
        shade += np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(a - aspect)
    lo, hi = np.percentile(shade[valid], (1, 99)) if valid.any() else (0.0, 1.0)
    out = np.clip((shade - lo) / max(hi - lo, 1e-9) * 255, 0, 255).astype(np.uint8)
    return cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(out)


def _bilinear(z: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Bilinear heights at pixels ``x, y`` (NaN outside or next to a cell without data); the samples laid out in rows
    (cv2.remap takes maps of fewer than 32767 rows)."""
    n = len(x)
    if not n:
        return np.zeros(0)
    width = 1024
    rows = (n + width - 1) // width
    pad = rows * width - n
    mx = np.concatenate([x, np.zeros(pad)]).astype(np.float32).reshape(rows, width)
    my = np.concatenate([y, np.zeros(pad)]).astype(np.float32).reshape(rows, width)
    out = cv2.remap(z, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=float("nan"))
    return out.ravel()[:n].astype(float)


@dataclass
class _View:
    """One chunk's raster on its grid: the image matched, its valid cells, the heights, the grid."""

    image: np.ndarray
    valid: np.ndarray
    z: np.ndarray
    grid: object

    def world(self, xy: np.ndarray) -> np.ndarray:
        """Pixels (OpenCV: centre of the first at 0, 0) -> world points with the height of the DEM (NaN outside)."""
        xy = np.asarray(xy, float).reshape(-1, 2)
        g = self.grid
        X = g.c + (xy[:, 0] + 0.5) * g.a
        Y = g.f + (xy[:, 1] + 0.5) * g.e
        return np.column_stack([X, Y, _bilinear(self.z, xy[:, 0], xy[:, 1])])

    def pixels(self, world: np.ndarray) -> np.ndarray:
        g = self.grid
        w = np.asarray(world, float).reshape(-1, 3)
        return np.column_stack([(w[:, 0] - g.c) / g.a - 0.5, (w[:, 1] - g.f) / g.e - 0.5])


def _view(dem, ortho, source: str, system, bounds, cell: float) -> _View:
    grid, w, h = _grid(bounds, cell)
    z, zvalid = _read(dem, "dem", system, grid, w, h)
    z = np.where(zvalid, z, np.nan).astype(np.float32)
    if source == "ortho":
        rgb, valid = _read(ortho, "orthomosaic", system, grid, w, h)
        valid = valid & zvalid
        image = _grey(rgb, valid)
    else:
        valid = zvalid
        image = _shade(z, valid, cell)
    return _View(image=image, valid=valid, z=z, grid=grid)


# --------------------------------------------------------------------------- candidate pairs
def _system_problem(ca: "Chunk", cb: "Chunk") -> ties.TieError | None:
    from . import mosaic

    if not ties._compatible(ca.crs, cb.crs):
        return ties._not_same_frame(cb.label, cb.crs, ca.label, ca.crs)
    if mosaic.frame_problem(cb.crs, ca.crs):
        return AutoTieError(f"{cb.label} is in {cb.crs} and {ca.label} in {ca.crs}: between these frames the rasters "
                            "would be placed by PROJ's approximate transformation; put both in one system first",
                            f"{cb.label} è in {cb.crs} e {ca.label} in {ca.crs}: fra questi sistemi i raster sarebbero "
                            "collocati dalla trasformazione approssimata di PROJ; porta prima i due chunk in un "
                            "sistema")
    return None


def _grow(bounds, margin: float):
    return (bounds[0] - margin, bounds[1] - margin, bounds[2] + margin, bounds[3] + margin)


def _cut(a, b):
    out = (max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3]))
    return out if out[2] > out[0] and out[3] > out[1] else None


def overlap(doc: "Document", a, b, margin: float = MARGIN) -> dict:
    """How much of the smaller footprint of ``a`` and ``b`` (cells with data of their DEMs, on a coarse grid in the
    system of ``a``) the other covers where they are now: ``{"share", "share_margin", "area"}`` (``share_margin``
    with the footprint of ``b`` grown by ``margin`` metres; ``area`` square metres in common)."""
    ca, cb = ties._chunk(doc, a), ties._chunk(doc, b)
    problem = _system_problem(ca, cb)
    if problem is not None:
        raise problem
    sa, _ = _sources(ca, ca.crs, False)
    sb, _ = _sources(cb, ca.crs, False)
    union = (min(sa.bounds[0], sb.bounds[0]) - margin, min(sa.bounds[1], sb.bounds[1]) - margin,
             max(sa.bounds[2], sb.bounds[2]) + margin, max(sa.bounds[3], sb.bounds[3]) + margin)
    cell = max(union[2] - union[0], union[3] - union[1]) / FOOTPRINT_SIDE
    grid, w, h = _grid(union, cell)
    va = _read(sa, "dem", ca.crs, grid, w, h)[1]
    vb = _read(sb, "dem", ca.crs, grid, w, h)[1]
    both = va & vb
    radius = int(math.ceil(margin / cell))
    grown = cv2.dilate(vb.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * radius + 1,) * 2)) > 0 \
        if radius > 0 else vb
    smaller = max(1, min(int(va.sum()), int(vb.sum())))
    return {"share": float(both.sum() / smaller), "share_margin": float((va & grown).sum() / smaller),
            "area": float(both.sum() * cell * cell)}


def find_pairs(doc: "Document", chunks=None, margin: float = MARGIN, min_overlap: float = MIN_OVERLAP,
               same_group: bool = False, progress: Progress | None = None) -> list[dict]:
    """Step 1: every two of ``chunks`` (default every chunk with a DEM, in the order of the project; the first of a
    pair is the reference, so give the chunk to keep first) with ``{"a", "b", "a_label", "b_label", "share",
    "share_margin", "area", "candidate", "reason"}``: ``candidate`` when ``share_margin >= min_overlap``; ``reason``
    a message for the others (no DEM, systems that cannot be mixed, no overlap, the same group)."""
    from ..core import groups

    progress = as_progress(progress)
    if chunks is None:
        chosen = [c for c in doc.chunks if c.dem_path is not None and c.dem_path.exists()]
    else:
        chosen = [ties._chunk(doc, c) for c in chunks]
    pairs = [(chosen[i], chosen[j]) for i in range(len(chosen)) for j in range(i + 1, len(chosen))]
    out = []
    for n, (ca, cb) in enumerate(pairs):
        progress(n / max(1, len(pairs)), f"Overlap of {cb.label} and {ca.label}")
        row = {"a": ca.key, "b": cb.key, "a_label": ca.label, "b_label": cb.label, "share": 0.0, "share_margin": 0.0,
               "area": 0.0, "candidate": False, "reason": None}
        ga, gb = groups.group_of(doc, ca), groups.group_of(doc, cb)
        if not same_group and ga is not None and gb is not None and ga["id"] == gb["id"]:
            row["reason"] = {"it": "stesso gruppo ({0}): si muovono insieme", "en": "same group ({0}): they move "
                             "together", "args": [ga["name"]], "code": "same_group", "chunk": None}
            out.append(row)
            continue
        try:
            row.update(overlap(doc, ca, cb, margin))
        except ties.TieError as exc:
            row["reason"] = {"it": exc.italian.replace("{", "{{").replace("}", "}}"),
                             "en": exc.english.replace("{", "{{").replace("}", "}}"), "args": [], "code": "error",
                             "chunk": None}
            out.append(row)
            continue
        row["candidate"] = row["share_margin"] >= min_overlap
        if not row["candidate"]:
            row["reason"] = {"it": "sovrapposizione {0:.0%} (sotto {1:.0%})", "en": "overlap {0:.0%} (below {1:.0%})",
                             "args": [row["share_margin"], min_overlap], "code": "small_overlap", "chunk": None}
        out.append(row)
    progress(1.0, f"{sum(r['candidate'] for r in out)} candidate pairs of {len(out)}")
    return out


# --------------------------------------------------------------------------- matching and the robust estimate
def _sift(image: np.ndarray, valid: np.ndarray):
    mask = cv2.erode(valid.astype(np.uint8) * 255, np.ones((9, 9), np.uint8))
    sift = cv2.SIFT_create(nfeatures=SIFT_FEATURES, contrastThreshold=0.02)
    kp, desc = sift.detectAndCompute(image, mask)
    if desc is None or not len(kp):
        return np.zeros((0, 2), np.float32), None
    return np.array([k.pt for k in kp], np.float32), desc


def _mutual(da, db) -> np.ndarray:
    """Pairs ``(i, j)``: ratio test both ways and each the other's best."""
    bf = cv2.BFMatcher(cv2.NORM_L2)
    ab = bf.knnMatch(da, db, k=2)
    ba = bf.knnMatch(db, da, k=2)
    best_ba = {}
    for m in ba:
        if len(m) == 2 and m[0].distance < RATIO * m[1].distance:
            best_ba[m[0].queryIdx] = m[0].trainIdx
    out = [(m[0].queryIdx, m[0].trainIdx) for m in ab
           if len(m) == 2 and m[0].distance < RATIO * m[1].distance and best_ba.get(m[0].trainIdx) == m[0].queryIdx]
    return np.array(out, int).reshape(-1, 2)


def _ransac(src: np.ndarray, dst: np.ndarray, threshold: float, seed: int = 0) -> np.ndarray:
    """The inliers (bool) of the best 3D similarity src -> dst over 3-point samples."""
    n = len(src)
    best = np.zeros(n, bool)
    if n < 3:
        return best
    rng = np.random.default_rng(seed)
    needed = RANSAC_ITERATIONS
    i = 0
    while i < needed:
        i += 1
        idx = rng.choice(n, 3, replace=False)
        s = src[idx]
        if np.linalg.norm(np.cross(s[1] - s[0], s[2] - s[0])) < (threshold * 4) ** 2:
            continue  # nearly on a line
        try:
            M = geometry.umeyama(s, dst[idx], with_scale=True)
        except (ValueError, np.linalg.LinAlgError):
            continue
        sc = float(np.cbrt(np.linalg.det(M[:3, :3])))
        if not SCALE_RANGE[0] <= sc <= SCALE_RANGE[1]:
            continue
        inl = np.linalg.norm(geometry.apply(M, src) - dst, axis=1) < threshold
        if inl.sum() > best.sum():
            best = inl
            share = inl.sum() / n
            if share > 0:  # adaptive: enough samples for 99 % of drawing three inliers
                needed = min(RANSAC_ITERATIONS, int(math.log(0.01) / math.log(max(1e-12, 1 - share ** 3))) + 1)
    return best


def _fit(src: np.ndarray, dst: np.ndarray, floor: float, rounds: int = 6) -> tuple[np.ndarray, np.ndarray]:
    """The similarity src -> dst with the points beyond ``OUTLIER_K`` NMAD (at least ``floor``) left out: ``(M,
    used)``."""
    used = np.ones(len(src), bool)
    M = geometry.umeyama(src, dst, with_scale=True)
    for _ in range(rounds):
        r = np.linalg.norm(geometry.apply(M, src) - dst, axis=1)
        nmad = 1.4826 * float(np.median(np.abs(r[used] - np.median(r[used]))))
        limit = max(float(np.median(r[used])) + OUTLIER_K * nmad, floor)
        new = r <= limit
        if new.sum() < 3:
            break
        M = geometry.umeyama(src[new], dst[new], with_scale=True)
        if np.array_equal(new, used):
            break
        used = new
    return M, used


def _pixel_affine(va: _View, vb: _View, W: np.ndarray) -> np.ndarray:
    """2x3 from pixels of ``vb`` to pixels of ``va`` through ``W`` at the median height of ``vb``."""
    h, w = vb.valid.shape
    xs, ys = np.meshgrid(np.linspace(0, w - 1, 5), np.linspace(0, h - 1, 5))
    px = np.column_stack([xs.ravel(), ys.ravel()])
    zmed = float(np.nanmedian(vb.z)) if np.isfinite(vb.z).any() else 0.0
    world = vb.world(px)
    world[:, 2] = zmed
    pa = va.pixels(geometry.apply(W, world))
    A = np.column_stack([px, np.ones(len(px))])
    coef = np.linalg.lstsq(A, pa, rcond=None)[0]
    return coef.T.astype(np.float64)


def _subpixel(res: np.ndarray, x: int, y: int) -> tuple[float, float]:
    def para(m1, c0, p1):
        d = m1 - 2 * c0 + p1
        return 0.0 if abs(d) < 1e-12 else float(0.5 * (m1 - p1) / d)
    dx = para(res[y, x - 1], res[y, x], res[y, x + 1])
    dy = para(res[y - 1, x], res[y, x], res[y + 1, x])
    return float(np.clip(dx, -0.5, 0.5)), float(np.clip(dy, -0.5, 0.5))


def _refine(va: _View, vb: _View, W: np.ndarray, progress: Progress) -> tuple[np.ndarray, np.ndarray]:
    """Patches of ``va`` found in ``vb`` warped by ``W``: ``(points of a, points of b)`` (world)."""
    M = _pixel_affine(va, vb, W)
    h, w = va.valid.shape
    warped = cv2.warpAffine(vb.image, M, (w, h), flags=cv2.INTER_LINEAR, borderValue=0)
    wvalid = cv2.warpAffine(vb.valid.astype(np.uint8), M, (w, h), flags=cv2.INTER_NEAREST, borderValue=0) > 0
    Minv = cv2.invertAffineTransform(M)
    half = PATCH // 2
    reach = half + SEARCH
    both = va.valid & wvalid
    pa, pb = [], []
    step = int(np.clip(math.sqrt(max(1, int(both.sum())) / PATCHES), MIN_STEP, PATCH))
    ys = range(reach + 1, h - reach - 1, step)
    for n, cy in enumerate(ys):
        if n % 20 == 0:
            progress(n / max(1, len(ys)), "")
        for cx in range(reach + 1, w - reach - 1, step):
            if not both[cy - reach:cy + reach + 1, cx - reach:cx + reach + 1].all():
                continue
            tpl = va.image[cy - half:cy + half + 1, cx - half:cx + half + 1]
            if float(tpl.std()) < MIN_TEXTURE:
                continue
            area = warped[cy - reach:cy + reach + 1, cx - reach:cx + reach + 1]
            res = cv2.matchTemplate(area, tpl, cv2.TM_CCOEFF_NORMED)
            _mn, peak, _lmn, (x, y) = cv2.minMaxLoc(res)
            if peak < MIN_NCC or x in (0, res.shape[1] - 1) or y in (0, res.shape[0] - 1):
                continue
            sx, sy = _subpixel(res, x, y)
            u, v = cx + (x + sx - SEARCH), cy + (y + sy - SEARCH)
            bx, by = Minv @ np.array([u, v, 1.0])
            pa.append((cx, cy))
            pb.append((bx, by))
    if not pa:
        return np.zeros((0, 3)), np.zeros((0, 3))
    wa, wb = va.world(np.array(pa, float)), vb.world(np.array(pb, float))
    good = np.isfinite(wa[:, 2]) & np.isfinite(wb[:, 2])
    return wa[good], wb[good]


def _spread(points: np.ndarray) -> float:
    """The width of the points in plan: 4 x the standard deviation along their thinnest direction."""
    if len(points) < 3:
        return 0.0
    cov = np.cov(points[:, :2].T)
    return 4.0 * float(np.sqrt(max(0.0, np.linalg.eigvalsh(cov).min())))


def match(doc: "Document", a, b, source: str = "ortho", margin: float | None = MARGIN, cell: float | None = None,
          progress: Progress | None = None) -> PairResult:
    """Steps 2-4 for the pair ``a`` (reference) and ``b``: the matching, the robust estimate, the refinement and the
    residual report (:class:`PairResult`; nothing is changed). ``margin`` None: the whole rasters (no use of where the
    chunks are now); ``cell``: the cell of the matching (default the coarser of the two rasters, grown so that a
    raster read stays within :data:`MAX_SIDE` pixels). :class:`AutoTieError` (a :class:`.ties.TieError`) for a chunk
    without DEM (or orthophoto with ``source="ortho"``) or systems that cannot be mixed."""
    if source not in SOURCES:
        raise ValueError(f"source {source!r}: use one of {', '.join(SOURCES)}")
    progress = as_progress(progress)
    ca, cb = ties._chunk(doc, a), ties._chunk(doc, b)
    if ca is cb:
        raise ValueError("a pair is two different chunks")
    problem = _system_problem(ca, cb)
    if problem is not None:
        raise problem
    system = ca.crs
    dem_a, ortho_a = _sources(ca, system, source == "ortho")
    dem_b, ortho_b = _sources(cb, system, source == "ortho")
    src_a, src_b = (ortho_a, ortho_b) if source == "ortho" else (dem_a, dem_b)
    if margin is None:
        ba, bb = src_a.bounds, src_b.bounds
    else:
        ba, bb = _cut(src_a.bounds, _grow(src_b.bounds, margin)), _cut(src_b.bounds, _grow(src_a.bounds, margin))
    result = PairResult(a=ca.key, b=cb.key, a_label=ca.label, b_label=cb.label, source=source, crs=system,
                        cell=0.0, placed={"a": np.asarray(ca.transform, float).tolist(),
                                          "b": np.asarray(cb.transform, float).tolist()})
    if ba is None or bb is None:
        result.messages.append(message("no_overlap"))
        return result
    side = max(ba[2] - ba[0], ba[3] - ba[1], bb[2] - bb[0], bb[3] - bb[1])
    c = max(float(cell or 0.0), src_a.cell, src_b.cell, side / MAX_SIDE)
    result.cell = c
    progress(0.02, f"Automatic ties {cb.label} -> {ca.label}: reading the rasters ({c:.4f} m cells)")
    va = _view(dem_a, ortho_a, source, system, ba, c)
    vb = _view(dem_b, ortho_b, source, system, bb, c)
    progress(0.15, f"Automatic ties {cb.label} -> {ca.label}: SIFT points")
    ka, da = _sift(va.image, va.valid)
    kb, db = _sift(vb.image, vb.valid)
    if da is None or db is None or len(ka) < 3 or len(kb) < 3:
        result.messages.append(message("no_texture"))
        return result
    pairs = _mutual(da, db)
    result.matches = len(pairs)
    wa = va.world(ka[pairs[:, 0]]) if len(pairs) else np.zeros((0, 3))
    wb = vb.world(kb[pairs[:, 1]]) if len(pairs) else np.zeros((0, 3))
    good = np.isfinite(wa[:, 2]) & np.isfinite(wb[:, 2])
    wa, wb = wa[good], wb[good]
    progress(0.3, f"Automatic ties {cb.label} -> {ca.label}: {len(wa)} matches, robust estimate")
    inl = _ransac(wb, wa, RANSAC_CELLS * c)
    result.inliers = int(inl.sum())
    if result.inliers < MIN_MATCHES:
        result.messages.append(message("few_matches", result.inliers, MIN_MATCHES))
        return result
    if result.inliers < 3 * MIN_MATCHES and result.inliers < MIN_SHARE * len(wa):
        result.messages.append(message("by_chance", result.inliers, len(wa), result.inliers / max(1, len(wa))))
        return result
    W, used = _fit(wb[inl], wa[inl], RANSAC_CELLS * c)
    pts_a, pts_b = wa[inl][used], wb[inl][used]
    refined = False
    for r in range(REFINE_ROUNDS):
        progress.check()
        fa, fb = _refine(va, vb, W, progress.sub(0.4 + 0.25 * r, 0.25))
        if len(fa) < MIN_MATCHES:
            break
        W2, used2 = _fit(fb, fa, 0.5 * c)
        if used2.sum() < MIN_MATCHES:
            break
        W, pts_a, pts_b, refined = W2, fa[used2], fb[used2], True
        result.refined = int(used2.sum())
    if not refined:
        result.messages.append(message("not_refined", len(pts_a)))
    progress(0.95, f"Automatic ties {cb.label} -> {ca.label}: residuals")
    res = geometry.apply(W, pts_b) - pts_a
    norm = np.linalg.norm(res, axis=1)
    scale = float(np.cbrt(np.linalg.det(W[:3, :3])))
    centre = pts_b.mean(0)
    result.transform = W
    result.rotation_deg = ties._rotation_deg(W)
    result.scale = scale
    result.shift = float(np.linalg.norm(geometry.apply(W, centre[None])[0] - centre))
    result.rms = float(np.sqrt(np.mean(norm ** 2)))
    result.rms_plan = float(np.sqrt(np.mean(np.sum(res[:, :2] ** 2, axis=1))))
    result.rms_z = float(np.sqrt(np.mean(res[:, 2] ** 2)))
    result.nmad = 1.4826 * float(np.median(np.abs(norm - np.median(norm))))
    result.points_a, result.points_b, result.residuals = pts_a, pts_b, res
    reasons = []
    if not SCALE_RANGE[0] <= scale <= SCALE_RANGE[1]:
        reasons.append(message("scale", scale, *SCALE_RANGE))
    width = _spread(pts_a)
    if width < MIN_SPREAD_CELLS * c:
        reasons.append(message("clustered", width, MIN_SPREAD_CELLS * c))
    if reasons:
        result.messages = reasons + result.messages
        return result
    result.status = "ok"
    zmed = float(np.median(np.abs(pts_a[:, 2])))
    for ch, srcs in ((ca, (dem_a, src_a)), (cb, (dem_b, src_b))):
        off = max((math.hypot(s.move[0, 2], s.move[1, 2]) * zmed for s in srcs if s.move is not None), default=0.0)
        if off > 0.5 * c:  # the 2D placement of a stale raster leaves the tilt's plan term out (as the 2D view)
            result.messages.append(message("stale_raster", ch.label, off))
    progress(1.0, f"Automatic ties {cb.label} -> {ca.label}: {len(pts_a)} points, RMS {result.rms:.4f} m")
    return result


def run(doc: "Document", pairs, source: str = "ortho", margin: float | None = MARGIN, cell: float | None = None,
        progress: Progress | None = None) -> list[PairResult]:
    """:func:`match` for each pair (``(a, b)`` or the dicts of :func:`find_pairs`); a pair that cannot be matched
    becomes a rejected result with its error."""
    progress = as_progress(progress)
    pairs = [(p["a"], p["b"]) if isinstance(p, dict) else tuple(p) for p in pairs]
    out = []
    for i, (a, b) in enumerate(pairs):
        sub = progress.sub(i / max(1, len(pairs)), 1 / max(1, len(pairs)))
        try:
            out.append(match(doc, a, b, source=source, margin=margin, cell=cell, progress=sub))
        except ties.TieError as exc:
            ca, cb = ties._chunk(doc, a), ties._chunk(doc, b)
            r = PairResult(a=ca.key, b=cb.key, a_label=ca.label, b_label=cb.label, source=source, crs=ca.crs, cell=0.0)
            r.messages.append({"it": exc.italian.replace("{", "{{").replace("}", "}}"),
                               "en": exc.english.replace("{", "{{").replace("}", "}}"), "args": [], "code": "error",
                               "chunk": None})
            out.append(r)
    return out


# --------------------------------------------------------------------------- accepting
def _spread_pick(points: np.ndarray, residuals: np.ndarray, n: int) -> list[int]:
    """Up to ``n`` indices spread in plan: the better half of the residuals, the point furthest from their middle
    first, then each time the one furthest from those taken."""
    norm = np.linalg.norm(residuals, axis=1)
    pool = np.flatnonzero(norm <= np.median(norm) * 1.5 + 1e-12)
    if len(pool) < min(n, len(points)):
        pool = np.argsort(norm)[:max(n, len(pool))]
    xy = points[pool, :2]
    first = int(np.argmax(np.linalg.norm(xy - xy.mean(0), axis=1)))
    chosen = [first]
    dist = np.linalg.norm(xy - xy[first], axis=1)
    while len(chosen) < min(n, len(pool)):
        k = int(np.argmax(dist))
        if dist[k] <= 0:
            break
        chosen.append(k)
        dist = np.minimum(dist, np.linalg.norm(xy - xy[k], axis=1))
    return [int(pool[k]) for k in chosen]


def records_of(doc: "Document") -> list[dict]:
    raw = doc.meta.get(META_KEY)
    return [dict(r) for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []


def _next_number(doc: "Document") -> int:
    taken = [int(m.group(1)) for t in ties.ties_of(doc) if (m := _NUMBERED.match(t.name))]
    return max(taken, default=0) + 1


def accept(doc: "Document", result: PairResult, max_ties: int = MAX_TIES, precision: float | None = None,
           replace: bool = True) -> list[ties.Tie]:
    """The pair ``result`` (status ``"ok"``) made into up to ``max_ties`` ties spread over the overlap (markers "auto
    N" in both chunks, :func:`.ties.add_tie`), with ``precision`` (default the RMS of the points kept, at least half a
    cell and :data:`MIN_PRECISION`); ``replace``: the ties this pair made before (still there) removed first. The
    pair is recorded in ``doc.meta["auto_ties"]``. :class:`AutoTieError` for a rejected result or chunks moved since
    the matching (match again)."""
    if not result.ok:
        raise AutoTieError(f"the pair {result.b_label} -> {result.a_label} was rejected: nothing to accept",
                           f"la coppia {result.b_label} -> {result.a_label} è stata scartata: niente da accettare")
    ca, cb = ties._chunk(doc, result.a), ties._chunk(doc, result.b)
    for key, ch in (("a", ca), ("b", cb)):
        if result.placed and not np.allclose(np.asarray(result.placed[key], float), np.asarray(ch.transform, float),
                                             rtol=0.0, atol=1e-9):
            raise AutoTieError(f"chunk {ch.label} moved after the matching: match the pair again",
                               f"il chunk {ch.label} si è spostato dopo l'abbinamento: abbina di nuovo la coppia")
    if precision is None:
        precision = max(result.rms, 0.5 * result.cell, MIN_PRECISION)
    records = records_of(doc)
    if replace:
        existing = {t.id for t in ties.ties_of(doc)}
        for r in records:
            if {r.get("a"), r.get("b")} == {ca.key, cb.key}:
                for tid in r.get("ties") or []:
                    if tid in existing:
                        ties.remove_tie(doc, tid)
                        existing.discard(tid)
        records = [r for r in records if {r.get("a"), r.get("b")} != {ca.key, cb.key}]
    picks = _spread_pick(result.points_a, result.residuals, max_ties)
    pa = result.points_a[picks]
    pb = ties._to_system(result.points_b[picks], result.crs, cb.crs, cb, "the points of an automatic tie")
    ia, ib = ca.to_internal(pa), cb.to_internal(pb)
    n = _next_number(doc)
    made = [ties.add_tie(doc, ca, ia[i], cb, ib[i], name=f"{LABEL} {n + i}", precision=float(precision))
            for i in range(len(picks))]
    records.append({**result.summary(), "ties": [t.id for t in made], "precision": float(precision),
                    "when": datetime.now().isoformat(timespec="seconds")})
    doc.meta[META_KEY] = records
    return made
