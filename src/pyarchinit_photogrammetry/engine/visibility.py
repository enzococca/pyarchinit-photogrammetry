"""Occlusion tests and camera selection shared by texturing, orthomosaic and SAM projection."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from .. import _native
from ..core.chunk import Chunk


class Raycaster:
    """First-hit ray casting against a mesh (Open3D if available, else trimesh)."""

    def __init__(self, vertices: np.ndarray, faces: np.ndarray):
        self._o3d = None
        try:
            import open3d as o3d

            scene = o3d.t.geometry.RaycastingScene()
            scene.add_triangles(o3d.core.Tensor(np.asarray(vertices, np.float32)),
                                o3d.core.Tensor(np.asarray(faces, np.uint32)))
            self._o3d = (o3d, scene)
        except Exception:
            import trimesh

            self._mesh = trimesh.Trimesh(vertices, faces, process=False)

    def hit_distance(self, origins: np.ndarray, directions: np.ndarray) -> np.ndarray:
        """Distance to the first hit along unit ``directions`` (inf if none)."""
        if self._o3d is not None:
            o3d, scene = self._o3d
            rays = np.hstack([origins, directions]).astype(np.float32)
            ans = scene.cast_rays(o3d.core.Tensor(rays))
            return ans["t_hit"].numpy().astype(np.float64)
        locs, idx_ray, _ = self._mesh.ray.intersects_location(origins, directions, multiple_hits=False)
        out = np.full(len(origins), np.inf)
        if len(idx_ray):
            out[idx_ray] = np.linalg.norm(locs - origins[idx_ray], axis=1)
        return out

    def visible_from(self, center: np.ndarray, points: np.ndarray, rel_tol: float = 2e-3,
                     abs_tol: float = 0.0) -> np.ndarray:
        """True where ``points`` are not hidden by the mesh as seen from ``center``.

        A point counts as visible when the first hit is no closer than its distance
        minus ``rel_tol * distance + abs_tol`` (surfaces built from different data,
        e.g. DEM vs mesh, never coincide exactly)."""
        if len(points) == 0:
            return np.zeros(0, bool)
        d = points - center
        dist = np.linalg.norm(d, axis=1)
        dirs = d / np.maximum(dist[:, None], 1e-12)
        out = np.ones(len(points), bool)
        step = 2_000_000
        for s in range(0, len(points), step):
            hit = self.hit_distance(np.repeat(center[None], min(step, len(points) - s), 0), dirs[s:s + step])
            out[s:s + step] = hit >= dist[s:s + step] * (1 - rel_tol) - abs_tol
        return out


def raycaster_for_chunk(chunk: Chunk) -> Raycaster | None:
    mesh = chunk.model
    if mesh is None:
        return None
    return Raycaster(np.asarray(mesh.vertices), np.asarray(mesh.faces))


class UnreadablePhotos(RuntimeError):
    """Photos of aligned cameras that cannot be read (``cameras``)."""

    def __init__(self, cameras):
        self.cameras = list(cameras)
        names = ", ".join(Path(c.path).name for c in self.cameras[:3])
        super().__init__(f"{len(self.cameras)} photos cannot be read ({names}{'...' if len(self.cameras) > 3 else ''}): "
                         "an empty file is usually a Dropbox, iCloud or OneDrive file kept online only. Make the "
                         "folder available offline, link the photos to another folder, or build without these photos")


def unreadable_photos(chunk: Chunk, cameras=None) -> list:
    """Cameras (the aligned ones by default) whose photo cannot be read: missing, empty
    (Dropbox, iCloud and OneDrive keep "online only" files as empty placeholders) or not an
    image (only the header is read)."""
    from PIL import Image

    bad = []
    for cam in chunk.aligned_cameras if cameras is None else cameras:
        try:
            if Path(cam.path).stat().st_size == 0:
                bad.append(cam)
                continue
            with Image.open(cam.path) as im:
                im.size  # noqa: B018 - the header only
        except Exception:
            bad.append(cam)
    return bad


def readable_cameras(chunk: Chunk, skip_unreadable: bool = False, cameras=None) -> tuple[list, list]:
    """The aligned cameras (or ``cameras``) whose photo can be read, and the others. Unreadable
    photos raise :class:`UnreadablePhotos` unless ``skip_unreadable`` (never when no photo
    at all can be read)."""
    cams = list(chunk.aligned_cameras if cameras is None else cameras)
    bad = unreadable_photos(chunk, cams)
    if bad and (not skip_unreadable or len(bad) == len(cams)):
        raise UnreadablePhotos(bad)
    skipped = {c.key for c in bad}
    return [c for c in cams if c.key not in skipped], bad


def read_photo(cam, flags: int = cv2.IMREAD_COLOR) -> np.ndarray:
    """The photo of ``cam`` as OpenCV reads it (BGR), ignoring the EXIF orientation as the
    camera models do; :class:`UnreadablePhotos` when it cannot be decoded."""
    img = cv2.imread(str(Path(cam.path)), flags | cv2.IMREAD_IGNORE_ORIENTATION)
    if img is None:
        raise UnreadablePhotos([cam])
    return img


class ImageCache:
    """Small LRU of decoded photos (BGR), with a colour correction applied when given
    (:class:`.colour.Correction`)."""

    def __init__(self, chunk: Chunk, max_items: int = 6, correction=None):
        self.chunk = chunk
        self.max_items = max_items
        self.correction = correction or None
        self._items: dict[int, np.ndarray] = {}

    def get(self, cam) -> np.ndarray:
        if cam.key not in self._items:
            if len(self._items) >= self.max_items:
                self._items.pop(next(iter(self._items)))
            img = read_photo(cam)
            self._items[cam.key] = img if self.correction is None else self.correction.correct(cam, img)
        return self._items[cam.key]


def sample(image: np.ndarray, px: np.ndarray) -> np.ndarray:
    """Bilinear sampling of ``image`` at COLMAP pixel positions ``(N,2)`` -> ``(N,C)``.

    COLMAP coordinates put the centre of the first pixel at (0.5, 0.5); OpenCV at (0, 0).
    """
    if len(px) == 0:
        return np.zeros((0, image.shape[2] if image.ndim == 3 else 1), image.dtype)
    channels = image.shape[2] if image.ndim == 3 else 1
    out = np.empty((len(px), channels), image.dtype)
    step, width = 4_000_000, 1024  # remap maps must stay below 32767 rows
    for s in range(0, len(px), step):
        part = px[s:s + step].astype(np.float32) - 0.5
        n = len(part)
        pad = (-n) % width
        part = np.vstack([part, np.zeros((pad, 2), np.float32)]) if pad else part
        mx = part[:, 0].reshape(-1, width)
        my = part[:, 1].reshape(-1, width)
        r = cv2.remap(image, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        out[s:s + n] = r.reshape(-1, channels)[:n]
    return out


def project_valid(chunk: Chunk, cam, points: np.ndarray, margin: float = 2.0):
    """Project internal-frame points into a camera: returns pixels, depth and in-frame mask."""
    sensor = chunk.sensor(cam)
    m = cam.cam_from_world
    if _native.supports(sensor.model):
        return _native.kernel("project_points")(np.ascontiguousarray(np.atleast_2d(points), np.float64), m, sensor.model,
                                                [float(v) for v in sensor.params], float(sensor.width),
                                                float(sensor.height), float(margin))
    pc = np.atleast_2d(points) @ m[:3, :3].T + m[:3, 3]
    depth = pc[:, 2]
    ok = depth > 1e-9
    # reject points far outside the field of view before applying lens distortion
    k = sensor.K
    with np.errstate(divide="ignore", invalid="ignore"):
        u = k[0, 0] * pc[:, 0] / depth + k[0, 2]
        v = k[1, 1] * pc[:, 1] / depth + k[1, 2]
    ok &= (u > -0.25 * sensor.width) & (u < 1.25 * sensor.width) & (v > -0.25 * sensor.height) & (v < 1.25 * sensor.height)
    px = np.full((len(pc), 2), np.nan)
    if ok.any():
        px[ok] = sensor.project(pc[ok])
    ok &= np.isfinite(px).all(1)
    ok &= (px[:, 0] >= margin) & (px[:, 0] < sensor.width - margin)
    ok &= (px[:, 1] >= margin) & (px[:, 1] < sensor.height - margin)
    return px, depth, ok
