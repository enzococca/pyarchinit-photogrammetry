"""Stakes and benchmarks found near known points: proposals of known ties, never stored until accepted.

A survey with points of known coordinates on the ground (the heads of stakes, benchmarks, nails: measured with a GNSS
receiver or a total station) is placed on them by known ties (:func:`.ties.add_known`). Finding each head by hand on
the orthophoto is slow; :func:`find` looks for them near where the known coordinates fall:

* **orthophoto**: a window of ``radius`` metres around the point (its world position carried to the file through the
  stamp of the orthophoto: one built in another transform of the chunk is read where it was built); grey = luminance,
  background = the median over 5 head diameters (the largest head), darkness = background - grey, cells darker than
  ``min_contrast`` grey levels, connected components whose equivalent diameter lies in ``head`` (metres) and whose
  circularity ``4 pi A / P^2`` is at least :data:`MIN_CIRCULARITY`: roots, cracks and shadows (long) and large patches
  are left out. The centre is the darkness-weighted centroid; the height comes from the DEM.
* **photos**: for each candidate the aligned photos that see it (:func:`..markers.gcp.photos_seeing`, readable ones
  only: missing photos and empty "online only" placeholders are skipped, :func:`.visibility.readable_cameras`; at most
  :data:`MAX_PHOTOS`), in each a window of 4 head diameters around its projection searched with the same detector at
  the photo's pixels per metre; the observations are triangulated with RANSAC over pairs, keeping those within
  ``max(2 px, 0.25 head px)`` of the point. With ``min_photos`` photos agreeing the candidate is placed by them
  (precision as an end measured on photos, :func:`.ties.end_precision`, at least 1 cm); otherwise it is *solo
  ortofoto*: placed on the orthophoto and the DEM, precision two orthophoto cells (:data:`.ties.RASTER_PIXELS`).

The result is a :class:`Proposal` per known point (its best candidate and up to ``max_candidates - 1`` others);
:func:`check` compares the distances between the proposals with those between the known points (as
:func:`.ties.distance_checks` does for ties) and the displacement of each from the median one; :func:`preview` makes a
picture to look at; only :func:`accept` changes the project (known ties, whose markers get the agreeing photo
measurements as pinned projections). No Qt here.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import cv2
import numpy as np

from ..core import crs as crs_mod
from ..core import geometry, stamps
from ..core.progress import Progress, as_progress
from . import ties

if TYPE_CHECKING:  # pragma: no cover
    from ..core.chunk import Chunk
    from ..core.document import Document

MIN_CIRCULARITY = 0.6  # 4 pi A / P^2 of a head: a disc is 1, a square 0.79, a stick far less
MAX_FILL = 0.9  # the area of a head over that of its smallest enclosing rectangle: a disc 0.79, a square (a black
#                 cell of a coded target) 1
MIN_SOLIDITY = 0.8  # the dark cells over the area within the outline: a head is solid, a target's frame is not
TARGET_CLEARANCE = 0.10  # metres: no candidate this close to a coded target detected in the chunk (its cells)
BACKGROUND_HEADS = 5  # the median background is taken over this many (largest) head diameters
PHOTO_WINDOW_HEADS = 4  # the window searched in a photo around the projection of a candidate: heads across
MAX_PHOTOS = 12  # at most this many photos looked at per candidate
AGREE_PX = 2.0  # an observation agrees within max(AGREE_PX, AGREE_HEAD x the head in pixels) of the point
AGREE_HEAD = 0.25
MIN_PRECISION = ties.MIN_PHOTO_PRECISION
CONTRAST_FULL = 100.0  # grey levels: a head this dark against its background scores fully


# --------------------------------------------------------------------------- records
@dataclass
class KnownPoint:
    """A point of known coordinates: ``xyz`` (z NaN when not known) in ``crs`` (None: the chunk's own system),
    ``precision`` in metres."""

    name: str
    xyz: np.ndarray
    crs: str | None
    precision: float

    def to_dict(self) -> dict:
        return {"name": self.name, "xyz": [None if not np.isfinite(v) else float(v) for v in self.xyz],
                "crs": self.crs, "precision": float(self.precision)}

    @classmethod
    def from_dict(cls, d: dict) -> "KnownPoint":
        return cls(str(d["name"]), np.array([np.nan if v is None else float(v) for v in d["xyz"]], float),
                   d.get("crs"), float(d.get("precision", ties.TABLE_PRECISION)))


@dataclass
class Candidate:
    """A dark round head near a known point. ``xy_world``/``world``: where it is in the chunk's world now;
    ``internal``: in its internal frame; ``contrast`` (grey levels), ``circularity``, ``diameter`` (metres) as the
    orthophoto shows it; ``photos``: ``[{"camera", "x", "y", "residual_px", "agrees"}]`` (COLMAP pixels); ``agreeing``:
    how many agree; ``precision`` metres; ``ortho_only``: placed by the orthophoto and the DEM (fewer photos agree
    than asked); ``score`` (higher first)."""

    xy_world: np.ndarray
    internal: np.ndarray
    contrast: float
    circularity: float
    diameter: float
    photos: list[dict] = field(default_factory=list)
    agreeing: int = 0
    precision: float = 0.0
    ortho_only: bool = True
    score: float = 0.0
    world: np.ndarray | None = None

    def to_dict(self) -> dict:
        return {"xy_world": [float(v) for v in self.xy_world], "world": [float(v) for v in self.world],
                "internal": [float(v) for v in self.internal], "contrast": float(self.contrast),
                "circularity": float(self.circularity), "diameter": float(self.diameter),
                "photos": [dict(p) for p in self.photos], "agreeing": int(self.agreeing),
                "precision": float(self.precision), "ortho_only": bool(self.ortho_only), "score": float(self.score)}

    @classmethod
    def from_dict(cls, d: dict) -> "Candidate":
        return cls(xy_world=np.asarray(d["xy_world"], float), internal=np.asarray(d["internal"], float),
                   contrast=float(d["contrast"]), circularity=float(d["circularity"]), diameter=float(d["diameter"]),
                   photos=[dict(p) for p in d.get("photos", [])], agreeing=int(d.get("agreeing", 0)),
                   precision=float(d["precision"]), ortho_only=bool(d.get("ortho_only", True)),
                   score=float(d.get("score", 0.0)), world=np.asarray(d["world"], float))


@dataclass
class Proposal:
    """What :func:`find` proposes for a known point in chunk ``chunk`` (its key): ``best`` (None: no head found),
    ``others``, ``displacement`` (best - known, world metres; z NaN without a known z), ``flags`` (``no_candidate``,
    ``ortho_only``, ``no_aligned_photos``, ``photos_unreadable``, ``outside_orthophoto``, ``no_height``), the
    alignment of the chunk when found (an accepted proposal must be in the same internal frame) and ``known``, the
    known point in the chunk's system (what :func:`check` compares: one system for both distances)."""

    point: KnownPoint
    chunk: int
    best: Candidate | None
    others: list[Candidate] = field(default_factory=list)
    displacement: np.ndarray | None = None
    flags: list[str] = field(default_factory=list)
    alignment: str | None = None
    known: np.ndarray | None = None  # the known point in the chunk's system (z NaN when not known)

    def to_dict(self) -> dict:
        return {"point": self.point.to_dict(), "chunk": int(self.chunk),
                "known": None if self.known is None else [None if not np.isfinite(v) else float(v)
                                                          for v in self.known],
                "best": None if self.best is None else self.best.to_dict(),
                "others": [c.to_dict() for c in self.others],
                "displacement": None if self.displacement is None else
                [None if not np.isfinite(v) else float(v) for v in self.displacement],
                "flags": list(self.flags), "alignment": self.alignment}

    @classmethod
    def from_dict(cls, d: dict) -> "Proposal":
        disp = d.get("displacement")
        return cls(point=KnownPoint.from_dict(d["point"]), chunk=int(d["chunk"]),
                   best=None if d.get("best") is None else Candidate.from_dict(d["best"]),
                   others=[Candidate.from_dict(c) for c in d.get("others", [])],
                   displacement=None if disp is None else np.array([np.nan if v is None else v for v in disp], float),
                   flags=list(d.get("flags", [])), alignment=d.get("alignment"),
                   known=None if d.get("known") is None else
                   np.array([np.nan if v is None else v for v in d["known"]], float))


# --------------------------------------------------------------------------- known points
def _rows_of_csv(path: Path, crs: str | None) -> list[KnownPoint]:
    from ..markers import gcp

    text = Path(path).read_text(encoding="utf-8-sig")
    first = next((ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")), "")
    delimiter = "\t" if "\t" in first else ";" if ";" in first else "," if "," in first else " "
    comma = delimiter != ","  # a decimal comma is possible
    out = []
    for i, row in enumerate(gcp._parse_rows(Path(path), delimiter, 0)):
        cells = [c.strip().replace(",", ".") if comma and k > 0 else c.strip() for k, c in enumerate(row)]
        if len(cells) < 3:
            raise ValueError(f"{Path(path).name}: a row is name,x,y[,z][,precision] (got {row!r})")
        try:
            x, y = float(cells[1]), float(cells[2])
        except ValueError:
            if i == 0:
                continue  # a header
            raise ValueError(f"{Path(path).name}: not a number in {row!r}") from None
        z = float(cells[3]) if len(cells) > 3 and cells[3] else np.nan
        precision = float(cells[4]) if len(cells) > 4 and cells[4] else ties.TABLE_PRECISION
        if not precision > 0:
            raise ValueError(f"{Path(path).name}: the precision of {cells[0]!r} must be positive")
        out.append(KnownPoint(cells[0], np.array([x, y, z], float), crs, precision))
    return out


def known_points(source, crs: str | None = None, doc: "Document | None" = None) -> list[KnownPoint]:
    """The known points of ``source``: a CSV file (``name,x,y[,z][,precision]``: commas, semicolons, tabs or spaces;
    a header line skipped; the coordinates in ``crs``, None = the chunk's system; precision default
    :data:`.ties.TABLE_PRECISION`), a chunk (its markers with a reference: their label, coordinates and largest
    accuracy), or ``"ties"`` / a document (the known ties of ``doc``: name, coordinates, precision)."""
    from ..core.chunk import Chunk
    from ..core.document import Document

    if isinstance(source, Document) or (isinstance(source, str) and source == "ties"):
        d = source if isinstance(source, Document) else doc
        if d is None:
            raise ValueError("the known ties need the document")
        return [KnownPoint(t.name, np.asarray(t.known, float), t.crs, float(t.precision))
                for t in ties.ties_of(d) if t.b is None and t.known is not None and not t.broken]
    if isinstance(source, Chunk):
        out = []
        for m in source.markers:
            loc = m.reference.location
            if loc is None or not m.reference.enabled:
                continue
            xyz = np.array([np.nan if v is None else float(v) for v in (list(loc) + [None])[:3]], float)
            out.append(KnownPoint(m.label, xyz, source.crs, float(max(m.reference.accuracy or [ties.TABLE_PRECISION]))))
        return out
    path = Path(source)
    if not path.exists():
        raise FileNotFoundError(f"no file {path}")
    return _rows_of_csv(path, crs)


def _in_chunk_system(chunk: "Chunk", point: KnownPoint) -> np.ndarray:
    """The known point in the chunk's system (z NaN kept)."""
    xyz = np.asarray(point.xyz, float).copy()
    if point.crs is None or point.crs == chunk.crs:
        return xyz
    if not ties._compatible(point.crs, chunk.crs):
        raise ties._not_same_frame(point.name, point.crs, chunk.label, chunk.crs)
    z = xyz[2]
    q = ties._to_system(np.array([[xyz[0], xyz[1], 0.0 if not np.isfinite(z) else z]]), point.crs, chunk.crs, chunk,
                        "the known points")[0]
    if not np.isfinite(z):
        q[2] = np.nan
    return q


# --------------------------------------------------------------------------- rasters in the world
def _move(chunk: "Chunk", product: str) -> np.ndarray | None:
    """world now -> coordinates of the file of ``product`` (built in another transform), None when they agree."""
    built = stamps.stamp_of(chunk, product)
    if built is None or stamps.same_transform(built, chunk.transform):
        return None
    return np.asarray(built, float) @ np.linalg.inv(np.asarray(chunk.transform, float))


def _to_file(chunk: "Chunk", product: str, xyz) -> np.ndarray:
    m = _move(chunk, product)
    p = np.asarray(xyz, float)
    return p if m is None else geometry.apply(m, p[None])[0]


def _from_file(chunk: "Chunk", product: str, xyz) -> np.ndarray:
    m = _move(chunk, product)
    p = np.asarray(xyz, float)
    return p if m is None else geometry.apply(np.linalg.inv(m), p[None])[0]


class _Surface:
    """Heights of the chunk's DEM in its world now (a DEM built in another transform moved with the chunk)."""

    def __init__(self, chunk: "Chunk"):
        self.chunk = chunk
        path = chunk.dem_path
        self.src = None
        if path is not None and Path(path).exists():
            import rasterio

            self.src = rasterio.open(path)

    def close(self):
        if self.src is not None:
            self.src.close()

    def _file_height(self, x: float, y: float) -> float | None:
        v = float(next(self.src.sample([(x, y)]))[0])
        nodata = self.src.nodata
        if not np.isfinite(v) or (nodata is not None and v == nodata):
            return None
        return v

    def height(self, x: float, y: float, z0: float = 0.0) -> float | None:
        """The world height of the DEM at world ``x, y`` (None: no DEM or no value there)."""
        if self.src is None:
            return None
        if _move(self.chunk, "dem") is None:
            return self._file_height(x, y)
        z = z0
        for _ in range(4):
            f = _to_file(self.chunk, "dem", (x, y, z))
            zf = self._file_height(f[0], f[1])
            if zf is None:
                return None
            w = _from_file(self.chunk, "dem", (f[0], f[1], zf))
            if abs(w[2] - z) < 1e-6:
                return float(w[2])
            z = float(w[2])
        return z

    def on_ortho(self, fx: float, fy: float, z_world: float) -> np.ndarray | None:
        """The world point of the surface seen at orthophoto-file ``fx, fy`` (``z_world`` a first guess of its
        world height); None where the DEM has no height."""
        zf = float(_to_file(self.chunk, "orthomosaic", _from_file(self.chunk, "orthomosaic", (fx, fy, 0.0))[:2]
                            .tolist() + [z_world])[2])
        for _ in range(4):
            w = _from_file(self.chunk, "orthomosaic", (fx, fy, zf))
            zd = self.height(w[0], w[1], w[2])
            if zd is None:
                return None
            f2 = _to_file(self.chunk, "orthomosaic", (w[0], w[1], zd))
            if abs(f2[2] - zf) < 1e-6:
                break
            zf = float(f2[2])
        return _from_file(self.chunk, "orthomosaic", (fx, fy, zf))


def _grey(rgb: np.ndarray) -> np.ndarray:
    if rgb.ndim == 2:
        return rgb.astype(np.uint8)
    return cv2.cvtColor(np.ascontiguousarray(rgb[..., :3]), cv2.COLOR_RGB2GRAY)


def _read_ortho(src, centre, half: float):
    """``(grey uint8, valid bool, rgb uint8, window affine)`` of the orthophoto ``src`` within ``half`` metres
    (file units) of ``centre`` (file x, y); None when that misses the file."""
    from rasterio.windows import Window, from_bounds

    win = from_bounds(centre[0] - half, centre[1] - half, centre[0] + half, centre[1] + half, transform=src.transform)
    c0, r0 = max(0, int(math.floor(win.col_off))), max(0, int(math.floor(win.row_off)))
    c1 = min(src.width, int(math.ceil(win.col_off + win.width)))
    r1 = min(src.height, int(math.ceil(win.row_off + win.height)))
    if c1 - c0 < 3 or r1 - r0 < 3:
        return None
    window = Window(c0, r0, c1 - c0, r1 - r0)
    bands = [1, 2, 3] if src.count >= 3 else [1]
    data = src.read(bands, window=window)
    if data.dtype != np.uint8:
        lo, hi = np.nanpercentile(data, (1, 99)) if data.size else (0, 1)
        data = np.clip((data.astype(np.float32) - lo) / max(hi - lo, 1e-9) * 255, 0, 255).astype(np.uint8)
    rgb = np.moveaxis(data, 0, -1) if len(bands) == 3 else data[0]
    valid = src.read_masks(1, window=window) > 0
    if src.count >= 4:
        valid &= src.read(4, window=window) > 0
    return _grey(rgb), valid, (rgb if rgb.ndim == 3 else np.dstack([rgb] * 3)), src.window_transform(window)


# --------------------------------------------------------------------------- the detector
def _odd(n: float, lo: int = 3) -> int:
    k = max(lo, int(round(n)))
    return k if k % 2 else k + 1


def detect(grey: np.ndarray, valid: np.ndarray | None, head_px: tuple[float, float], background_px: float,
           min_contrast: float) -> list[dict]:
    """Dark round blobs of ``grey`` (uint8): ``[{"x", "y" (array pixels, the centre of the first pixel at 0, 0),
    "contrast", "circularity", "diameter" (pixels)}]``; components touching the edge of the image or of the valid
    area are left out (a head cut in half)."""
    g = grey.copy()
    valid = np.ones(g.shape, bool) if valid is None else valid
    if not valid.any():
        return []
    g[~valid] = np.uint8(np.median(g[valid]))  # no dark edge where the raster ends
    bg = cv2.medianBlur(g, _odd(background_px))
    dark = bg.astype(np.int16) - g.astype(np.int16)
    mask = ((dark >= min_contrast) & valid).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    h, w = g.shape
    edge = np.zeros_like(valid)
    edge[0, :] = edge[-1, :] = edge[:, 0] = edge[:, -1] = True
    edge |= cv2.dilate((~valid).astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
    out = []
    lo, hi = head_px
    for i in range(1, n):
        x0, y0, bw, bh, area = (int(v) for v in stats[i])
        d_eq = math.sqrt(4 * area / math.pi)
        if d_eq < lo or d_eq > hi or bw > 3 * hi or bh > 3 * hi:
            continue
        comp = labels[y0:y0 + bh, x0:x0 + bw] == i
        if (comp & edge[y0:y0 + bh, x0:x0 + bw]).any():
            continue
        pad = np.pad(comp.astype(np.uint8), 1)
        contours, _ = cv2.findContours(pad, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        if not contours:
            continue
        cnt = max(contours, key=cv2.contourArea)
        per = cv2.arcLength(cnt, True)
        # the area within the outline through the pixel centres plus half a pixel all around: a disc of pixels
        area_c = cv2.contourArea(cnt) + per / 2.0 + 1.0
        per_c = per + math.pi  # the outline half a pixel further out
        circ = min(1.0, 4 * math.pi * area_c / max(per_c, 1e-9) ** 2)
        if circ < MIN_CIRCULARITY:
            continue
        if area < MIN_SOLIDITY * area_c:  # holes: the frame of a coded target, a ring
            continue
        (_c, (rw, rh), _a) = cv2.minAreaRect(cnt)
        if area_c / max((rw + 1.0) * (rh + 1.0), 1e-9) > MAX_FILL:  # a square outline: cells of a coded target
            continue
        wts = np.where(comp, dark[y0:y0 + bh, x0:x0 + bw], 0).astype(np.float64)
        ys, xs = np.mgrid[0:bh, 0:bw]
        tot = wts.sum()
        cx = x0 + float((wts * xs).sum() / tot)
        cy = y0 + float((wts * ys).sum() / tot)
        out.append({"x": cx, "y": cy, "contrast": float(dark[y0:y0 + bh, x0:x0 + bw][comp].mean()),
                    "circularity": float(circ), "diameter": float(d_eq)})
    return out


# --------------------------------------------------------------------------- photos
def _plane_axes(chunk: "Chunk") -> tuple[np.ndarray, np.ndarray]:
    """Internal unit vectors of the world x and y directions."""
    r = np.asarray(chunk.transform, float)[:3, :3]
    inv = np.linalg.inv(r)
    e1, e2 = inv @ np.array([1.0, 0.0, 0.0]), inv @ np.array([0.0, 1.0, 0.0])
    return e1 / np.linalg.norm(e1), e2 / np.linalg.norm(e2)


def _photo_crop(img: np.ndarray, u: float, v: float, half: int):
    """The part of the grey photo ``img`` within ``half`` pixels of COLMAP ``u, v``: ``(crop, valid, x0, y0)``."""
    h, w = img.shape[:2]
    cx, cy = int(round(u - 0.5)), int(round(v - 0.5))
    x0, y0 = max(0, cx - half), max(0, cy - half)
    x1, y1 = min(w, cx + half + 1), min(h, cy + half + 1)
    if x1 - x0 < 3 or y1 - y0 < 3:
        return None
    crop = img[y0:y1, x0:x1]
    return crop, np.ones(crop.shape, bool), x0, y0


def _observe(chunk, cam, img, point, diameter: float, head: tuple, min_contrast: float, axes) -> dict | None:
    """The head nearest the projection of ``point`` (internal) in a photo: ``{"camera", "x", "y", "head_px"}``."""
    from ..markers.refine import _pixels_per_metre

    sensor = chunk.sensor(cam)
    px, depth = cam.project(sensor, np.asarray(point, float)[None])
    if not depth[0] > 0 or not np.all(np.isfinite(px)):
        return None
    ppm = _pixels_per_metre(chunk, cam, np.asarray(point, float), *axes) / max(float(chunk.scale), 1e-12)
    if not ppm > 0:
        return None
    u, v = float(px[0, 0]), float(px[0, 1])
    window = PHOTO_WINDOW_HEADS * diameter * ppm / 2.0
    bgpx = BACKGROUND_HEADS * head[1] * ppm
    got = _photo_crop(img, u, v, int(math.ceil(window + bgpx / 2.0)) + 2)
    if got is None:
        return None
    crop, valid, x0, y0 = got
    found = detect(crop, valid, (head[0] * ppm, head[1] * ppm), bgpx, min_contrast)
    best, best_d = None, None
    for f in found:
        x, y = x0 + f["x"] + 0.5, y0 + f["y"] + 0.5  # COLMAP pixels
        if abs(x - u) > window or abs(y - v) > window:
            continue
        d = math.hypot(x - u, y - v)
        if best_d is None or d < best_d:
            best, best_d = (x, y, f["diameter"]), d
    if best is None:
        return None
    return {"camera": cam.key, "x": best[0], "y": best[1], "head_px": max(diameter * ppm, best[2])}


def _residuals(chunk, cams: dict, obs: list[dict], X: np.ndarray) -> np.ndarray:
    out = np.full(len(obs), np.inf)
    for i, o in enumerate(obs):
        cam = cams[o["camera"]]
        px, depth = cam.project(chunk.sensor(cam), X[None])
        if depth[0] > 0 and np.all(np.isfinite(px)):
            out[i] = math.hypot(px[0, 0] - o["x"], px[0, 1] - o["y"])
    return out


def _triangulate(chunk, cams: dict, obs: list[dict]) -> np.ndarray | None:
    views, norm = [], []
    for o in obs:
        cam = cams[o["camera"]]
        views.append(geometry.PinholeView(cam.transform))
        norm.append(chunk.sensor(cam).unproject([[o["x"], o["y"]]])[0])
    try:
        X = geometry.triangulate(views, np.asarray(norm))
    except (ValueError, np.linalg.LinAlgError):
        return None
    return X if np.all(np.isfinite(X)) else None


def _consensus(chunk, cams: dict, obs: list[dict]) -> tuple[np.ndarray | None, np.ndarray, np.ndarray]:
    """RANSAC over the pairs of observations: ``(point, residuals px, agrees)``."""
    tol = np.array([max(AGREE_PX, AGREE_HEAD * o["head_px"]) for o in obs])
    best = (None, np.full(len(obs), np.inf), np.zeros(len(obs), bool))
    best_key = None
    for i in range(len(obs)):
        for j in range(i + 1, len(obs)):
            X = _triangulate(chunk, cams, [obs[i], obs[j]])
            if X is None:
                continue
            res = _residuals(chunk, cams, obs, X)
            agree = res <= tol
            key = (int(agree.sum()), -float(np.sum(np.where(agree, res, 0.0))))
            if best_key is None or key > best_key:
                best_key, best = key, (X, res, agree)
    X, res, agree = best
    if X is not None and agree.sum() >= 2:
        sub = [o for o, a in zip(obs, agree) if a]
        X2 = _triangulate(chunk, cams, sub)
        if X2 is not None:
            res2 = _residuals(chunk, cams, obs, X2)
            if (res2 <= tol).sum() >= agree.sum():
                X, res, agree = X2, res2, res2 <= tol
    return X, res, agree


def _photo_precision(chunk, cams: dict, obs: list[dict], res: np.ndarray, agree: np.ndarray, X: np.ndarray) -> float:
    errors = []
    for o, r, a in zip(obs, res, agree):
        if not a:
            continue
        cam = cams[o["camera"]]
        _px, depth = cam.project(chunk.sensor(cam), X[None])
        errors.append(float(r) * float(depth[0]) / chunk.sensor(cam).focal_px)
    metres = float(np.sqrt(np.mean(np.square(errors)))) * float(chunk.scale) if errors else 0.0
    return max(MIN_PRECISION, metres)


# --------------------------------------------------------------------------- find
class _GreyPhotos:
    """The last :data:`MAX_PHOTOS` photos read, in grey (a photo of 48 MP: 48 MB, not three times that)."""

    def __init__(self):
        self._items: dict[int, np.ndarray] = {}

    def get(self, cam) -> np.ndarray:
        from .visibility import read_photo

        if cam.key not in self._items:
            if len(self._items) >= MAX_PHOTOS:
                self._items.pop(next(iter(self._items)))
            self._items[cam.key] = read_photo(cam, cv2.IMREAD_GRAYSCALE)
        return self._items[cam.key]


def _score(contrast: float, circularity: float, distance: float, radius: float) -> float:
    return (min(contrast, CONTRAST_FULL) / CONTRAST_FULL) * circularity * (1.0 - 0.5 * min(distance / radius, 1.0))


def find(doc: "Document", chunk, points, radius: float = 3.0, head=(0.03, 0.12), min_contrast: float = 25,
         min_photos: int = 3, max_candidates: int = 5, progress: Progress | None = None) -> list[Proposal]:
    """A :class:`Proposal` for each known point of ``points`` (a list of :class:`KnownPoint`, or a source of
    :func:`known_points`) in ``chunk`` (see the module docstring). Nothing in the project changes. ``ValueError``
    when the chunk has no orthophoto or its system is in degrees; :class:`.ties.TieError` when the points are in a
    local frame and the chunk in a projected system (or the other way round)."""
    import rasterio

    from ..markers import gcp
    from .visibility import UnreadablePhotos, readable_cameras

    progress = as_progress(progress)
    ch = ties._chunk(doc, chunk)
    if not isinstance(points, (list, tuple)):
        points = known_points(points, doc=doc)
    radius, head = float(radius), (float(head[0]), float(head[1]))
    if not (radius > 0 and 0 < head[0] < head[1]):
        raise ValueError("the radius must be positive and the head range (smallest, largest) increasing")
    if crs_mod.is_geographic(ch.crs):
        raise ValueError(f"the chunk {ch.label} is in a system in degrees: stakes are looked for in metres")
    ortho = ch.product_path("orthomosaic")
    if ortho is None or not Path(ortho).exists():
        raise ValueError(f"the chunk {ch.label} has no orthophoto: build it first")
    aligned = list(ch.aligned_cameras)
    unreadable: set[int] = set()
    if aligned:
        try:
            _ok, bad = readable_cameras(ch, skip_unreadable=True)
        except UnreadablePhotos as exc:
            bad = exc.cameras
        unreadable = {c.key for c in bad}
    cams = {c.key: c for c in aligned}
    images = _GreyPhotos()
    axes = _plane_axes(ch)
    surface = _Surface(ch)
    alignment = ties._alignment(ch)
    out = []
    try:
        with rasterio.open(ortho) as src:
            move = _move(ch, "orthomosaic")
            per_metre = 1.0 if move is None else float(np.linalg.norm(move[:3, 0]))  # file units per world metre
            cell = float(max(abs(src.res[0]), abs(src.res[1]))) / per_metre  # metres
            for k, point in enumerate(points):
                progress.check()
                progress(k / max(len(points), 1), point.name)
                out.append(_find_one(ch, point, src, cell, per_metre, surface, radius, head, min_contrast, min_photos,
                                     max_candidates, cams, unreadable, images, axes, alignment, gcp))
    finally:
        surface.close()
    progress(1.0, "")
    return out


def _find_one(ch, point, src, cell, per_metre, surface, radius, head, min_contrast, min_photos, max_candidates, cams,
              unreadable, images, axes, alignment, gcp) -> Proposal:
    from .visibility import UnreadablePhotos

    flags: list[str] = []
    known = _in_chunk_system(ch, point)
    z_guess = known[2] if np.isfinite(known[2]) else surface.height(known[0], known[1])
    if z_guess is None:
        return Proposal(point, ch.key, None, flags=["no_height"], alignment=alignment, known=known)
    centre = _to_file(ch, "orthomosaic", (known[0], known[1], z_guess))
    margin = BACKGROUND_HEADS * head[1] / 2.0
    got = _read_ortho(src, centre, (radius + margin) * per_metre)
    if got is None:
        return Proposal(point, ch.key, None, flags=["outside_orthophoto"], alignment=alignment, known=known)
    grey, valid, _rgb, tr = got
    found = detect(grey, valid, (head[0] / cell, head[1] / cell), BACKGROUND_HEADS * head[1] / cell, min_contrast)
    targets = [ch.to_world(np.asarray(m.position, float)[None])[0] for m in ch.markers
               if m.code is not None and m.position is not None]
    clearance = max(TARGET_CLEARANCE, head[1])
    cands = []
    for f in found:
        fx, fy = tr @ (f["x"] + 0.5, f["y"] + 0.5)
        dist = math.hypot(fx - centre[0], fy - centre[1]) / per_metre
        if dist > radius:
            continue
        w = surface.on_ortho(fx, fy, z_guess) if surface.src is not None else _from_file(
            ch, "orthomosaic", (fx, fy, centre[2]))
        if w is None:  # a hole of the DEM under the head: at the height of the known point
            w = _from_file(ch, "orthomosaic", (fx, fy, centre[2]))
        if any(math.hypot(w[0] - t[0], w[1] - t[1]) < clearance for t in targets):
            continue  # a cell of a coded target
        internal = ch.to_internal(np.asarray(w, float)[None])[0]
        diameter = f["diameter"] * cell
        cands.append(Candidate(xy_world=np.asarray(w[:2], float), internal=internal, contrast=f["contrast"],
                               circularity=f["circularity"], diameter=diameter, precision=ties.RASTER_PIXELS * cell,
                               score=_score(f["contrast"], f["circularity"], dist, radius), world=np.asarray(w, float)))
    cands.sort(key=lambda c: -c.score)
    cands = cands[:max_candidates]
    if not cams:
        flags.append("no_aligned_photos")
    for c in cands:
        if not cams:
            break
        seeing = gcp.photos_seeing(ch, c.internal)
        if any(cam.key in unreadable for cam, *_ in seeing):
            if "photos_unreadable" not in flags:
                flags.append("photos_unreadable")
        usable = [cam for cam, *_ in seeing if cam.key not in unreadable][:MAX_PHOTOS]
        obs = []
        for cam in usable:
            try:
                grey_img = images.get(cam)
            except (UnreadablePhotos, OSError, cv2.error):  # cannot be decoded after all: as unreadable
                if "photos_unreadable" not in flags:
                    flags.append("photos_unreadable")
                continue
            o = _observe(ch, cam, grey_img, c.internal, c.diameter, head, min_contrast, axes)
            if o is not None:
                obs.append(o)
        if len(obs) < 2:
            c.photos = [{"camera": o["camera"], "x": o["x"], "y": o["y"], "residual_px": None, "agrees": False}
                        for o in obs]
            continue
        X, res, agree = _consensus(ch, cams, obs)
        c.photos = [{"camera": o["camera"], "x": float(o["x"]), "y": float(o["y"]),
                     "residual_px": None if not np.isfinite(r) else float(r), "agrees": bool(a)}
                    for o, r, a in zip(obs, res, agree)]
        c.agreeing = int(agree.sum())
        if X is not None and c.agreeing >= max(2, int(min_photos)):
            c.internal = np.asarray(X, float)
            c.world = ch.to_world(c.internal[None])[0]
            c.xy_world = c.world[:2].copy()
            c.precision = _photo_precision(ch, cams, obs, res, agree, X)
            c.ortho_only = False
            c.score += 1.0 + c.agreeing / MAX_PHOTOS
    cands.sort(key=lambda c: -c.score)
    if not cands:
        flags.append("no_candidate")
        return Proposal(point, ch.key, None, flags=flags, alignment=alignment, known=known)
    best = cands[0]
    if best.ortho_only:
        flags.append("ortho_only")
    disp = best.world - known
    return Proposal(point, ch.key, best, cands[1:], disp, flags, alignment, known)


# --------------------------------------------------------------------------- check
def _distance(a: np.ndarray, b: np.ndarray) -> float:
    if np.isfinite(a[2]) and np.isfinite(b[2]):
        return float(np.linalg.norm(a - b))
    return float(np.linalg.norm(a[:2] - b[:2]))


def _proposal_precision(p: Proposal) -> float:
    return float(np.hypot(p.best.precision, p.point.precision))


def check(proposals: list[Proposal]) -> dict:
    """The proposals checked against each other before any is accepted: ``pairs`` (one dict per two proposals, as
    :func:`.ties.distance_checks`: ``ties`` and ``names`` = the two point names, ``distance_known``,
    ``distance_found``, ``difference``, ``precision`` (the two combined, each its candidate's and its point's),
    ``tolerance`` (:data:`.ties.CHECK_FACTOR` x it), ``ok``; heights only when both points have one), ``suspects``
    (:func:`.ties.suspects`: the points most likely wrong), ``displacement_median`` (the median of found - known,
    x, y, z; None without proposals) and ``outliers`` (names whose displacement differs from the median by more than
    :data:`.ties.CHECK_FACTOR` x their precision: a whole survey shifted shows as the median, a wrong point as an
    outlier)."""
    items = [p for p in proposals if p.best is not None]
    pairs = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            a, b = items[i], items[j]
            ka, kb = (np.array(a.known if a.known is not None else a.point.xyz, float),
                      np.array(b.known if b.known is not None else b.point.xyz, float))  # the chunk's system
            fa, fb = a.best.world.copy(), b.best.world.copy()
            if not (np.isfinite(ka[2]) and np.isfinite(kb[2])):
                fa[2] = fb[2] = ka[2] = kb[2] = np.nan
            dk, df = _distance(ka, kb), _distance(fa, fb)
            prec = float(np.hypot(_proposal_precision(a), _proposal_precision(b)))
            diff = df - dk
            pairs.append({"ties": [a.point.name, b.point.name], "names": [a.point.name, b.point.name],
                          "distance_known": dk, "distance_found": df, "difference": float(diff),
                          "precision": prec, "tolerance": ties.CHECK_FACTOR * prec, "comparable": True,
                          "ok": bool(abs(diff) <= ties.CHECK_FACTOR * prec)})
    disps = [np.asarray(p.displacement, float) for p in items if p.displacement is not None]
    median = None
    outliers = []
    if disps:
        stack = np.vstack(disps)
        with np.errstate(all="ignore"):
            import warnings

            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                median = np.nanmedian(stack, axis=0)
        for p in items:
            d = np.asarray(p.displacement, float) - median
            d = np.where(np.isfinite(d), d, 0.0)
            if float(np.linalg.norm(d)) > ties.CHECK_FACTOR * _proposal_precision(p):
                outliers.append(p.point.name)
    return {"pairs": pairs, "suspects": ties.suspects(pairs),
            "displacement_median": None if median is None else [None if not np.isfinite(v) else float(v)
                                                                for v in median],
            "outliers": outliers}


# --------------------------------------------------------------------------- preview
def _cross(img: np.ndarray, x: float, y: float, colour=(255, 40, 40)) -> None:
    s = max(6, img.shape[0] // 16)
    cx, cy = int(round(x)), int(round(y))
    cv2.line(img, (cx - s, cy), (cx - 2, cy), colour, 1, cv2.LINE_AA)
    cv2.line(img, (cx + 2, cy), (cx + s, cy), colour, 1, cv2.LINE_AA)
    cv2.line(img, (cx, cy - s), (cx, cy - 2), colour, 1, cv2.LINE_AA)
    cv2.line(img, (cx, cy + 2), (cx, cy + s), colour, 1, cv2.LINE_AA)


def preview(chunk: "Chunk", proposal: Proposal, size: int = 256) -> np.ndarray:
    """A picture of a proposal (RGB uint8, ``size`` x ``2 size``): on the left the orthophoto around the proposed
    point, on the right the photo that sees it best (the agreeing one nearest to the point, else the first seeing
    it; grey when none can be read), a red cross on the proposed point (on the known point when there is no
    proposal)."""
    import rasterio

    from ..markers import gcp
    from ..markers.refine import _pixels_per_metre
    from .visibility import UnreadablePhotos, read_photo, unreadable_photos

    size = int(size)
    left = np.full((size, size, 3), 128, np.uint8)
    right = np.full((size, size, 3), 128, np.uint8)
    best = proposal.best
    half = max(4 * (best.diameter if best is not None else 0.05), 0.15)
    if best is not None:
        world = np.asarray(best.world, float)
    else:
        world = _in_chunk_system(chunk, proposal.point)
        if not np.isfinite(world[2]):
            world[2] = 0.0
    ortho = chunk.product_path("orthomosaic")
    if ortho is not None and Path(ortho).exists():
        with rasterio.open(ortho) as src:
            f = _to_file(chunk, "orthomosaic", world)
            got = _read_ortho(src, f, half)
            if got is not None:
                _g, _v, rgb, tr = got
                inv = ~tr
                col, row = inv @ (f[0], f[1])
                h, w = rgb.shape[:2]
                k = size / max(h, w)
                img = cv2.resize(rgb, (max(1, int(round(w * k))), max(1, int(round(h * k)))),
                                 interpolation=cv2.INTER_AREA if k < 1 else cv2.INTER_LINEAR)
                left[:img.shape[0], :img.shape[1]] = img
                _cross(left, col * k, row * k)
    internal = chunk.to_internal(world[None])[0]
    cam = None
    if best is not None:
        agreeing = sorted([p for p in best.photos if p.get("agrees")], key=lambda p: p.get("residual_px") or 0.0)
        for p in agreeing:
            try:
                cam = chunk.camera(p["camera"])
                break
            except KeyError:
                continue
    if cam is None and chunk.aligned_cameras:
        seeing = [c for c, *_ in gcp.photos_seeing(chunk, internal)][:MAX_PHOTOS]
        bad = {c.key for c in unreadable_photos(chunk, seeing)}
        seeing = [c for c in seeing if c.key not in bad]
        cam = seeing[0] if seeing else None
    if cam is not None:
        try:
            photo = read_photo(cam)
        except (UnreadablePhotos, OSError, cv2.error):  # unreadable after all: the grey half stays
            photo = None
        if photo is not None:
            sensor = chunk.sensor(cam)
            px, depth = cam.project(sensor, internal[None])
            ppm = _pixels_per_metre(chunk, cam, internal, *_plane_axes(chunk)) / max(float(chunk.scale), 1e-12)
            if depth[0] > 0 and np.all(np.isfinite(px)) and ppm > 0:
                hp = max(8, int(round(half * ppm)))
                u, v = float(px[0, 0]) - 0.5, float(px[0, 1]) - 0.5
                x0, y0 = int(round(u)) - hp, int(round(v)) - hp
                canvas = np.full((2 * hp + 1, 2 * hp + 1, 3), 128, np.uint8)
                sx0, sy0 = max(0, x0), max(0, y0)
                sx1, sy1 = min(photo.shape[1], x0 + 2 * hp + 1), min(photo.shape[0], y0 + 2 * hp + 1)
                if sx1 > sx0 and sy1 > sy0:
                    canvas[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = photo[sy0:sy1, sx0:sx1][..., ::-1]
                k = size / canvas.shape[0]
                right = cv2.resize(canvas, (size, size), interpolation=cv2.INTER_AREA if k < 1 else cv2.INTER_LINEAR)
                _cross(right, (u - x0 + 0.5) * k - 0.5, (v - y0 + 0.5) * k - 0.5)
    return np.hstack([left, right])


# --------------------------------------------------------------------------- accept
def accept(doc: "Document", chunk, proposals: list[Proposal], names=None) -> list:
    """The proposals accepted (all with a candidate, or those of the points named in ``names``) as known ties
    (:func:`.ties.add_known`: a marker at the candidate, its reference the known coordinates, the precision the
    candidate's and the point's combined; a point without a known z takes the candidate's), the agreeing photo
    measurements written as pinned projections of the tie's marker, then those new markers alone triangulated
    (:func:`..markers.gcp.triangulate_markers`). ``ValueError`` when a proposal belongs to another chunk, the chunk
    was aligned again since it was found (another internal frame), or a name asked has no candidate."""
    from ..markers import gcp

    ch = ties._chunk(doc, chunk)
    wanted = None if names is None else [str(n) for n in names]
    chosen = []
    for p in proposals:
        if wanted is not None and p.point.name not in wanted:
            continue
        if p.chunk != ch.key:
            raise ValueError(f"the proposal for {p.point.name!r} belongs to another chunk (key {p.chunk})")
        if p.best is None:
            if wanted is not None:
                raise ValueError(f"no head was found for {p.point.name!r}")
            continue
        if p.alignment is not None and p.alignment != ties._alignment(ch):
            raise ValueError(f"the chunk {ch.label} was aligned again since the proposal for {p.point.name!r} was "
                             "found: look for the stakes again")
        chosen.append(p)
    if wanted is not None:
        missing = [n for n in wanted if n not in {p.point.name for p in chosen}]
        if missing:
            raise ValueError(f"no proposal for {', '.join(repr(n) for n in missing)}")
    made, measured = [], []
    for p in chosen:
        known = _in_chunk_system(ch, p.point)
        if not np.isfinite(known[2]):
            known[2] = float(p.best.world[2])
        tie = ties.add_known(doc, ch, p.best.internal, known, name=p.point.name,
                             precision=_proposal_precision(p))
        marker = next(m for m in ch.markers if m.key == tie.a[1])
        if not p.best.ortho_only:
            for ph in p.best.photos:
                if not ph.get("agrees"):
                    continue
                try:
                    cam = ch.camera(int(ph["camera"]))
                except KeyError:
                    continue
                ch.add_marker_projection(marker, cam, float(ph["x"]), float(ph["y"]), pinned=True)
                if marker not in measured:
                    measured.append(marker)
        made.append(tie)
    if measured:  # the new markers only: tie ends, ground control points and adjusted positions stay as they are
        gcp.triangulate_markers(ch, only=measured)
    return made


def file_name(name: str) -> str:
    """A file name for a point name (the preview of the command line)."""
    return re.sub(r"[^\w.-]+", "_", name).strip("_") or "punto"
