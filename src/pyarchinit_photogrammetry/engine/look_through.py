"""Looking through a photo (Metashape's *Look Through*): the pose and the field of view of an aligned photo for a
3D view, the photo undistorted onto an ideal pinhole camera so that it lies on the model, and the photo that best
matches a view.

Conventions: pixel coordinates as in COLMAP (the centre of the first pixel at (0.5, 0.5)); the ideal camera has one
focal length ``f`` and its principal point in the middle of the image, ``u = f X / Z + width / 2``. The view that
looks through the photo puts the ideal image, scaled by ``s``, in the middle of the view (bands at the sides or above
and below when the shapes differ), so that a point at ideal pixel ``u`` is at widget pixel ``x0 + s u``."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..core import geometry

MAX_SIDE = 1600  # pixels of the longer side of the undistorted photo shown over the view


@dataclass(frozen=True)
class Ideal:
    """The distortion-free camera the photo is resampled to: ``width`` x ``height`` pixels, focal ``f`` (pixels),
    principal point in the middle."""

    width: int
    height: int
    f: float

    def to_pixels(self, xy) -> np.ndarray:
        """Normalised coordinates (X/Z, Y/Z), (N, 2) -> pixels of the ideal image (COLMAP convention)."""
        xy = np.atleast_2d(np.asarray(xy, np.float64))
        return xy * self.f + np.array([self.width / 2.0, self.height / 2.0])

    def corners(self) -> np.ndarray:
        """The four corners of the image as directions in the camera frame (Z = 1): top left, top right, bottom
        right, bottom left."""
        w, h = self.width / 2.0 / self.f, self.height / 2.0 / self.f
        return np.array([[-w, -h, 1.0], [w, -h, 1.0], [w, h, 1.0], [-w, h, 1.0]])


def ideal_camera(sensor, max_side: int = MAX_SIDE) -> Ideal:
    """The ideal camera of ``sensor``: its frame reduced to ``max_side`` pixels at most, its mean focal length."""
    k = min(1.0, float(max_side) / max(sensor.width, sensor.height))
    return Ideal(max(1, int(round(sensor.width * k))), max(1, int(round(sensor.height * k))), sensor.focal_px * k)


def undistortion_maps(sensor, ideal: Ideal, src_width: int, src_height: int) -> tuple[np.ndarray, np.ndarray]:
    """``cv2.remap`` maps from the ideal image to a photo of ``sensor`` read at ``src_width`` x ``src_height`` (the
    sensor's frame or a reduced copy): for every ideal pixel the photo pixel (OpenCV indices) the same ray lands on,
    through the sensor's model (any COLMAP model, fisheye ones too); -1 where it lands on nothing."""
    ii, jj = np.meshgrid(np.arange(ideal.width, dtype=np.float64), np.arange(ideal.height, dtype=np.float64))
    xy = (np.column_stack([ii.ravel(), jj.ravel()]) + 0.5 - [ideal.width / 2.0, ideal.height / 2.0]) / ideal.f
    rays = np.column_stack([xy, np.ones(len(xy))])
    px = np.asarray(sensor.to_pycolmap().img_from_cam(rays), np.float64)
    sx, sy = src_width / float(sensor.width), src_height / float(sensor.height)
    mx = px[:, 0] * sx - 0.5  # COLMAP pixels of the sensor -> OpenCV indices of the copy read
    my = px[:, 1] * sy - 0.5
    bad = ~(np.isfinite(mx) & np.isfinite(my))
    mx[bad] = my[bad] = -1.0
    return (mx.reshape(ideal.height, ideal.width).astype(np.float32),
            my.reshape(ideal.height, ideal.width).astype(np.float32))


def read_reduced(path, max_side: int) -> np.ndarray:
    """The photo as COLMAP sees it (EXIF orientation ignored), BGR, decoded at 1/2, 1/4 or 1/8 when that still
    leaves at least ``max_side`` pixels on the longer side (a 48 MP photo is not decoded whole for a 1600 pixel
    view). Raises OSError when it cannot be read."""
    import cv2

    data = np.fromfile(str(Path(path)), np.uint8)  # not cv2.imread: paths with accents on Windows
    if not len(data):
        raise OSError(f"cannot read {path}")
    eighth = cv2.imdecode(data, cv2.IMREAD_REDUCED_COLOR_8 | cv2.IMREAD_IGNORE_ORIENTATION)
    if eighth is None:
        raise OSError(f"cannot read {path}")
    side = max(eighth.shape[:2]) * 8  # about the longer side of the photo
    if side // 8 >= max_side:
        return eighth
    for factor, flag in ((4, cv2.IMREAD_REDUCED_COLOR_4), (2, cv2.IMREAD_REDUCED_COLOR_2)):
        if side // factor >= max_side:
            img = cv2.imdecode(data, flag | cv2.IMREAD_IGNORE_ORIENTATION)
            if img is not None:
                return img
    img = cv2.imdecode(data, cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
    if img is None:
        raise OSError(f"cannot read {path}")
    return img


def undistort_photo(path, sensor, ideal: Ideal, maps=None) -> np.ndarray:
    """The photo at ``path`` resampled onto the ideal camera: RGBA uint8 (height, width, 4), transparent where the
    ideal frame sees past the photo; ``maps`` as :func:`photo_and_maps` gives them back."""
    return photo_and_maps(path, sensor, ideal, maps)[0]


def photo_and_maps(path, sensor, ideal: Ideal, maps=None):
    """:func:`undistort_photo` and the maps used ``((map_x, map_y), (src_width, src_height))`` for the next photo of
    the same sensor."""
    import cv2

    img = read_reduced(path, max(ideal.width, ideal.height))
    h, w = img.shape[:2]
    if maps is None or maps[1] != (w, h):
        maps = (undistortion_maps(sensor, ideal, w, h), (w, h))
    (mx, my), _size = maps
    bgra = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
    out = cv2.remap(bgra, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
    return np.ascontiguousarray(out[:, :, [2, 1, 0, 3]]), maps


def photo_to_ideal(sensor, ideal: Ideal, pixels) -> np.ndarray:
    """Pixels of the photo (COLMAP convention, sensor frame) -> pixels of the ideal image: where a point measured
    on the photo (a marker) is on the undistorted photo shown."""
    return ideal.to_pixels(sensor.unproject(pixels))


def photo_pose(chunk, camera) -> tuple[np.ndarray, np.ndarray]:
    """The centre of ``camera`` in world coordinates (``chunk.transform`` applied) and its axes in the world as the
    rows of a rotation: right, down, forward (the photo's x, y and viewing direction)."""
    if camera.transform is None:
        raise ValueError(f"the photo {camera.label} is not aligned")
    _s, rot, _t = geometry.decompose_similarity(chunk.transform)
    world_from_cam = rot @ np.asarray(camera.transform, np.float64)[:3, :3]
    centre = chunk.to_world(np.asarray(camera.transform, np.float64)[:3, 3][None])[0]
    basis = world_from_cam.T
    return centre, basis / np.linalg.norm(basis, axis=1, keepdims=True)


def fit_in_view(ideal: Ideal, view_width: float, view_height: float) -> tuple[float, tuple[float, float, float, float]]:
    """The vertical field of view (degrees) of a 3D view of ``view_width`` x ``view_height`` widget pixels that
    looks through the ideal camera, and the rectangle ``(x, y, width, height)`` the ideal image takes in it (centred,
    as large as fits)."""
    s = min(view_width / ideal.width, view_height / ideal.height)
    fov = float(np.degrees(2.0 * np.arctan(0.5 * view_height / (ideal.f * s))))
    w, h = ideal.width * s, ideal.height * s
    return fov, ((view_width - w) / 2.0, (view_height - h) / 2.0, w, h)


def ordered_cameras(chunk) -> list:
    """The aligned, enabled photos in the order they were taken (the EXIF time), then the photos without a time by
    name: the order of *Pagina giù/su* while looking through them."""
    cams = [c for c in chunk.cameras if c.aligned and c.enabled]
    return sorted(cams, key=lambda c: (0, str(c.meta["datetime"]), c.label) if c.meta.get("datetime")
                  else (1, "", c.label))


def best_photo(chunk, eye, forward, size: float, cameras=None):
    """The aligned photo whose viewing direction and position best match a view from ``eye`` (world) looking along
    ``forward``: the smallest angle between the directions (degrees) plus 30 degrees for every ``size`` (the extent
    of the scene) between the centres; photos turned more than 90 degrees away are left out. None: no photo."""
    eye = np.asarray(eye, np.float64)
    fwd = np.asarray(forward, np.float64)
    fwd = fwd / np.linalg.norm(fwd)
    best, best_score = None, np.inf
    for cam in (ordered_cameras(chunk) if cameras is None else cameras):
        centre, basis = photo_pose(chunk, cam)
        cos = float(np.clip(basis[2] @ fwd, -1.0, 1.0))
        if cos <= 0:
            continue
        score = float(np.degrees(np.arccos(cos))) + 30.0 * float(np.linalg.norm(centre - eye)) / max(size, 1e-9)
        if score < best_score:
            best, best_score = cam, score
    return best
