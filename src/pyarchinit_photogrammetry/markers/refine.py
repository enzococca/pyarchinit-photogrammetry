"""Estimated marker projections (blue flags) found again in the photos by image correlation.

A marker measured in a photo - placed by hand or detected - is estimated in the others from its
position (:func:`.gcp.project_marker`): the flag falls where the model says, a few pixels from the
point when the surface or the alignment is not exact. :func:`refine_markers` looks for the point
around every flag: a patch of the photo where the marker is measured (the reference) is compared
with the photo of the flag, both sampled on the same grid of points on the surface at the marker
(the plane of the dense cloud, else of the tie points, around it), so perspective, rotation, scale
and lens distortion between the two photos do not matter. Where the normalised correlation has a
clear peak inside the search area, the flag moves there (to a fraction of a pixel) and becomes an
automatic measurement (``pinned`` False, as a detected target), which counts for the
triangulation; the other flags stay estimates, to confirm by hand.
"""
from __future__ import annotations

import cv2
import numpy as np

from ..core.marker import Projection
from ..core.progress import Progress, as_progress
from .gcp import _SurfacePoints, camera_ray, project_marker, triangulate_markers

HALF = 12  # the patch compared: (2 HALF + 1)^2 samples
SEARCH = 10  # the flag looked for within +- SEARCH samples
MIN_SCORE = 0.8  # normalised correlation of an accepted peak
MARGIN = 0.1  # the best peak above any other one by at least this much


def _plane(points: np.ndarray | None, centre: np.ndarray, toward: np.ndarray, scale: float):
    """Unit vectors ``(e1, e2, n)`` of the surface at ``centre``: the plane of the nearest
    points within ``scale`` (n towards ``toward``), else the plane facing ``toward``."""
    n = toward - centre
    n /= np.linalg.norm(n)
    if points is not None and len(points) >= 12:
        d2 = np.einsum("ij,ij->i", points - centre, points - centre)
        k = min(40, len(points) - 1)
        near = np.argpartition(d2, k)[:k]
        near = near[d2[near] < (8 * scale) ** 2]
        if len(near) >= 12:
            p = points[near].astype(float)
            _u, sv, vt = np.linalg.svd(p - p.mean(0), full_matrices=False)
            if sv[2] < 0.35 * sv[1]:  # a surface, not a corner or a scatter
                n = vt[2] if vt[2] @ n > 0 else -vt[2]
    e1 = np.cross(n, [0.0, 0.0, 1.0]) if abs(n[2]) < 0.9 else np.cross(n, [1.0, 0.0, 0.0])
    e1 /= np.linalg.norm(e1)
    return e1, np.cross(n, e1), n


def _on_plane(origin, direction, centre, n) -> np.ndarray | None:
    den = direction @ n
    if abs(den) < 1e-9:
        return None
    t = ((centre - origin) @ n) / den
    return origin + t * direction if t > 0 else None


def _pixels_per_metre(chunk, cam, X, e1, e2) -> float:
    """Pixels of a photo per metre along the plane (the shorter of the two directions)."""
    eps = 1e-3 * max(np.linalg.norm(X - cam.transform[:3, 3]), 1e-6)
    pts = np.array([X, X + eps * e1, X + eps * e2])
    px, depth = cam.project(chunk.sensor(cam), pts)
    if (depth <= 0).any():
        return 0.0
    return min(np.linalg.norm(px[1] - px[0]), np.linalg.norm(px[2] - px[0])) / eps


def _sample(chunk, cam, image, grid: np.ndarray, step_px: float) -> np.ndarray | None:
    """The grey values of ``image`` at the projections of the 3D points ``grid`` (h, w, 3),
    smoothed first when the samples are wider apart than the pixels (no aliasing)."""
    h, w = grid.shape[:2]
    px, depth = cam.project(chunk.sensor(cam), grid.reshape(-1, 3))
    if (depth <= 0).any() or not np.isfinite(px).all():
        return None
    u, v = px[:, 0] - 0.5, px[:, 1] - 0.5  # COLMAP pixel centres -> OpenCV
    H, W = image.shape[:2]
    if u.min() < 1 or v.min() < 1 or u.max() > W - 2 or v.max() > H - 2:
        return None
    pad = int(np.ceil(3 * max(step_px, 1.0))) + 2
    x0, y0 = max(int(u.min()) - pad, 0), max(int(v.min()) - pad, 0)
    x1, y1 = min(int(u.max()) + pad + 1, W), min(int(v.max()) + pad + 1, H)
    crop = image[y0:y1, x0:x1].astype(np.float32)
    if step_px > 1.5:
        crop = cv2.GaussianBlur(crop, (0, 0), 0.5 * step_px)
    out = cv2.remap(crop, (u - x0).astype(np.float32).reshape(h, w), (v - y0).astype(np.float32).reshape(h, w),
                    cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return out


def _peak(score: np.ndarray) -> tuple[float, float, float, float] | None:
    """Best correlation, its sub-pixel offset (x, y) from the centre and the best other peak."""
    i, j = np.unravel_index(int(np.argmax(score)), score.shape)
    best = float(score[i, j])
    r = score.shape[0] // 2
    if not (0 < i < score.shape[0] - 1 and 0 < j < score.shape[1] - 1):
        return None  # on the border: the point may be farther
    other = score.copy()
    other[max(i - 2, 0):i + 3, max(j - 2, 0):j + 3] = -1.0

    def vertex(a, b, c):
        den = a - 2 * b + c
        return 0.0 if den >= 0 else float(np.clip(0.5 * (a - c) / den, -0.5, 0.5))

    dx = vertex(score[i, j - 1], best, score[i, j + 1])
    dy = vertex(score[i - 1, j], best, score[i + 1, j])
    return best, j - r + dx, i - r + dy, float(other.max())


def refine_marker(chunk, marker, images: dict | None = None, half: int = HALF, search: int = SEARCH,
                  min_score: float = MIN_SCORE, reference=None) -> dict:
    """Look for ``marker`` around its estimated projections (see the module docstring); the flags
    found become automatic measurements. ``images``: a cache {camera key: grey photo} shared
    between markers. Returns ``{"refined": {camera key: (dx, dy, score)}, "failed": [keys],
    "reference": key}`` (dx, dy: how far the flag moved, pixels)."""
    from ..engine.visibility import read_photo

    images = {} if images is None else images
    out = {"refined": {}, "failed": [], "reference": None}
    measured = [(k, p) for k, p in marker.projections.items() if p.measured]
    estimated = [k for k, p in marker.projections.items() if not p.measured]
    if not measured or not estimated or marker.position is None:
        return out
    X = np.asarray(marker.position, float)

    def aligned(key):
        try:
            cam = chunk.camera(key)
        except KeyError:
            return None
        return cam if cam.enabled and cam.aligned else None

    def image(cam):
        if cam.key not in images:
            try:
                images[cam.key] = read_photo(cam, cv2.IMREAD_GRAYSCALE)
            except Exception:
                images[cam.key] = None
        return images[cam.key]

    # the reference: placed by hand first, then the photo that sees the surface there best
    refs = []
    for key, proj in measured:
        cam = aligned(key)
        if cam is None or (reference is not None and key != reference):
            continue
        dist = float(np.linalg.norm(X - cam.transform[:3, 3]))
        refs.append((proj.pinned is not True, dist / chunk.sensor(cam).focal_px, cam, proj))
    if not refs:
        return out
    refs.sort(key=lambda r: r[:2])
    _manual, gsd, ref_cam, ref_proj = refs[0]
    surface = _SurfacePoints(chunk).xyz()
    e1, e2, n = _plane(surface, X, ref_cam.transform[:3, 3], 4 * half * gsd)
    origin, direction = camera_ray(chunk, ref_cam, ref_proj.x, ref_proj.y)
    centre = _on_plane(origin, direction, X, n)
    ref_img = image(ref_cam)
    if centre is None or ref_img is None:
        return out
    out["reference"] = ref_cam.key
    ref_ppm = _pixels_per_metre(chunk, ref_cam, centre, e1, e2)
    for key in estimated:
        cam = aligned(key)
        img = image(cam) if cam is not None else None
        ppm = _pixels_per_metre(chunk, cam, centre, e1, e2) if img is not None else 0.0
        if img is None or ppm <= 0 or ref_ppm <= 0:
            out["failed"].append(key)
            continue
        step = 1.0 / min(ppm, ref_ppm)  # metres between samples: a pixel of the coarser photo
        a = np.arange(-(half + search), half + search + 1) * step
        grid = centre + a[None, :, None] * e1 + a[:, None, None] * e2
        inner = grid[search:-search, search:-search]
        template = _sample(chunk, ref_cam, ref_img, inner, step * ref_ppm)
        window = _sample(chunk, cam, img, grid, step * ppm)
        if template is None or window is None or template.std() < 2.0:
            out["failed"].append(key)
            continue
        found = _peak(cv2.matchTemplate(window, template, cv2.TM_CCOEFF_NORMED))
        if found is None or found[0] < min_score or found[3] > found[0] - MARGIN:
            out["failed"].append(key)
            continue
        score, da, db, _second = found
        px, depth = cam.project(chunk.sensor(cam), (centre + da * step * e1 + db * step * e2)[None])
        old = marker.projections[key]
        x, y = float(px[0, 0]), float(px[0, 1])
        sensor = chunk.sensor(cam)
        if depth[0] <= 0 or not (0 <= x < sensor.width and 0 <= y < sensor.height):
            out["failed"].append(key)
            continue
        marker.projections[key] = Projection(x, y, False)
        out["refined"][key] = (x - old.x, y - old.y, score)
    return out


def refine_markers(chunk, markers=None, min_score: float = MIN_SCORE, progress: Progress | None = None) -> dict:
    """:func:`refine_marker` for ``markers`` (all by default), then their positions again and the
    flags left on the other photos. Returns ``{marker key: result}``."""
    progress = as_progress(progress)
    markers = [m for m in (chunk.markers if markers is None else markers)
               if any(p.measured for p in m.projections.values())]
    if any(m.position is None for m in markers):
        triangulate_markers(chunk)  # a marker measured in one photo is placed on the surface
    images: dict = {}
    report = {}
    for i, m in enumerate(markers):
        progress.check()
        if not any(not p.measured for p in m.projections.values()):
            project_marker(chunk, m)  # its flags first, when it has none yet
        report[m.key] = refine_marker(chunk, m, images, min_score=min_score)
        if len(images) > 24:  # photos are large: keep the cache small
            for k in list(images)[:-12]:
                del images[k]
        progress((i + 1) / max(len(markers), 1), f"Marker {m.label}: {len(report[m.key]['refined'])} found")
    triangulate_markers(chunk)
    for m in markers:
        if report[m.key]["refined"]:
            project_marker(chunk, m)  # the flags still to confirm, from where the marker is now
    return report
