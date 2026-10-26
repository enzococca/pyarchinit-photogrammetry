"""360-degree photos (equirectangular) as a rig of pinhole photos.

Each panorama becomes the six faces of a cube around its centre (front, right, back, left, up,
down), every one a perspective photo that the whole pipeline - alignment, depth maps,
orthomosaic, texture - handles as any other. The faces cover a little more than 90 degrees
(``fov``) so that neighbouring faces overlap, at the resolution of the panorama in their centre.
They are written beside the panoramas, one folder per face with the name of the panorama
(``facce_360/<face>/<panorama>.jpg``), and share one pinhole sensor known exactly.

In the alignment (:func:`engine.sfm.match_photos`) the faces of every panorama are one frame of
a COLMAP rig (:func:`rig_configs`): their relative rotation is known and kept, they share the
centre of the panorama and their calibration is not refined. A face that sees little (the sky,
a plain vault, the tripod) is placed by the other faces of its panorama. The GPS of the
panorama becomes the reference of each face.

Used for tombs, caves, cisterns and narrow trenches photographed with a 360-degree camera.
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

import cv2
import numpy as np

from ..core.chunk import Chunk
from ..core.progress import Progress, as_progress

# yaw (to the right) and pitch (up), degrees, of the axis of each face; panorama frame: x right,
# y down, z forward (the centre of the panorama)
FACES = {"front": (0.0, 0.0), "right": (90.0, 0.0), "back": (180.0, 0.0), "left": (270.0, 0.0),
         "up": (0.0, 90.0), "down": (0.0, -90.0)}
FOLDER = "facce_360"
EXTENSIONS = (".jpg", ".jpeg", ".png", ".tif", ".tiff")


def is_equirectangular(width: int, height: int) -> bool:
    return height > 0 and abs(width / height - 2.0) < 0.02


def face_rotation(yaw: float, pitch: float) -> np.ndarray:
    """Rotation from the frame of a face camera to the panorama frame."""
    a, b = np.radians(yaw), np.radians(pitch)
    ry = np.array([[np.cos(a), 0.0, np.sin(a)], [0.0, 1.0, 0.0], [-np.sin(a), 0.0, np.cos(a)]])  # turn right
    rx = np.array([[1.0, 0.0, 0.0], [0.0, np.cos(b), -np.sin(b)], [0.0, np.sin(b), np.cos(b)]])  # look up
    return ry @ rx


def face_size(width: int, fov: float = 100.0) -> int:
    """Side of a face with the resolution of the panorama (``width`` px) in its centre."""
    return int(round(np.tan(np.radians(fov) / 2.0) * width / np.pi))


def face_maps(width: int, height: int, size: int, yaw: float, pitch: float, fov: float = 100.0):
    """``cv2.remap`` maps of a face (``size`` x ``size``, pinhole of ``fov`` degrees with the
    principal point in the centre) from an equirectangular image ``width`` x ``height``
    (column 0 at 180 degrees to the left, row 0 at the zenith); returns them and the focal
    length in pixels."""
    f = size / 2.0 / np.tan(np.radians(fov) / 2.0)
    jj, ii = np.meshgrid(np.arange(size) + 0.5, np.arange(size) + 0.5)
    rays = np.dstack([(jj - size / 2.0) / f, (ii - size / 2.0) / f, np.ones_like(jj)])
    rays = rays @ face_rotation(yaw, pitch).T
    x, y, z = rays[..., 0], rays[..., 1], rays[..., 2]
    lon = np.arctan2(x, z)
    lat = np.arctan2(-y, np.hypot(x, z))
    mx = ((lon / (2 * np.pi) + 0.5) * width - 0.5).astype(np.float32)
    my = ((0.5 - lat / np.pi) * height - 0.5).astype(np.float32)
    return mx, my, f


def split_panorama(path: str | os.PathLike, folder: str | os.PathLike | None = None, size: int | None = None,
                   fov: float = 100.0, faces=None) -> tuple[dict[str, Path], float, int]:
    """Write the cube faces of an equirectangular photo (``folder/<face>/<name>.jpg``, by
    default in ``facce_360`` beside it); returns ``{face: path}``, the focal length (pixels) and
    the side of the faces."""
    path = Path(path)
    img = cv2.imread(str(path), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
    if img is None:
        raise RuntimeError(f"cannot read {path}")
    h, w = img.shape[:2]
    if not is_equirectangular(w, h):
        raise ValueError(f"{path.name} is not an equirectangular photo (width twice the height)")
    size = int(size or face_size(w, fov))
    folder = Path(folder) if folder is not None else path.parent / FOLDER
    pad = 2  # the panorama continues across its left and right edges
    wrapped = cv2.copyMakeBorder(img, 0, 0, pad, pad, cv2.BORDER_WRAP)
    out: dict[str, Path] = {}
    f = 0.0
    for name in (faces or FACES):
        mx, my, f = face_maps(w, h, size, *FACES[name], fov=fov)
        face = cv2.remap(wrapped, mx + pad, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        p = folder / name / f"{path.stem}.jpg"
        p.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(p), face, [cv2.IMWRITE_JPEG_QUALITY, 95])
        out[name] = p
    return out, f, size


def _sensor(chunk: Chunk, side: int, f: float, fov: float):
    from ..core.camera import Sensor

    label = f"Foto 360° ({side} px, {fov:g}°)"
    for s in chunk.sensors.values():
        if s.label == label and s.width == side and s.model == "PINHOLE":
            return s
    s = Sensor(key=chunk._next_key(chunk.sensors.values()), label=label, width=side, height=side,
               model="PINHOLE", params=[f, f, side / 2.0, side / 2.0], fixed=True)
    chunk.sensors[s.key] = s
    return s


def add_panoramas(chunk: Chunk, paths, size: int | None = None, fov: float = 100.0, faces=None,
                  progress: Progress | None = None) -> list:
    """Equirectangular photos (files or folders) added to ``chunk`` as the faces of
    :func:`split_panorama`; photos that are not panoramas are left out. Returns the cameras
    added (``meta["panorama"]`` = the panorama, ``meta["face"]`` = the face)."""
    from ..gnss.apply import NO_RTK, set_camera_position
    from ..io.exif import read_exif

    progress = as_progress(progress)
    files: list[Path] = []
    for p in map(Path, paths):
        files += sorted(f for f in p.iterdir() if f.suffix.lower() in EXTENSIONS) if p.is_dir() else [p]
    before = set(chunk.sensors)
    cams = []
    for i, pano in enumerate(files):
        progress.check()
        info = read_exif(pano)
        if not is_equirectangular(info["width"], info["height"]):
            continue
        written, f, side = split_panorama(pano, size=size, fov=fov, faces=faces)
        sensor = _sensor(chunk, side, f, fov)
        added = {Path(c.path): c for c in chunk.add_photos(list(written.values()))}
        for face, path in written.items():
            cam = added.get(path.resolve())
            if cam is None:  # already in the chunk
                continue
            cam.label = f"{pano.stem}_{face}"
            cam.sensor_key = sensor.key
            cam.meta.update(panorama=pano.name, face=face)
            if info.get("gps"):
                set_camera_position(cam, list(info["gps"]), "EPSG:4326", list(NO_RTK), "panorama")
            cams.append(cam)
        progress((i + 1) / len(files), f"360-degree photos {i + 1}/{len(files)}")
    used = {c.sensor_key for c in chunk.cameras}
    for key in set(chunk.sensors) - before - used:  # made by add_photos before the faces got theirs
        del chunk.sensors[key]
    return cams


def rig_configs(chunk: Chunk, names: dict[int, str]) -> list:
    """COLMAP rig configurations for the faces of the panoramas among ``names`` (camera key ->
    COLMAP image name): one rig per ``facce_360`` folder, a camera per face (image prefix
    ``.../facce_360/<face>/``) at its rotation from the first face, no translation; the faces
    with the same file name - one panorama - make a frame."""
    import pycolmap

    groups: dict[str, dict[str, str]] = {}
    for cam in chunk.cameras:
        face = cam.meta.get("face")
        if not cam.meta.get("panorama") or face not in FACES or cam.key not in names:
            continue
        name = PurePosixPath(names[cam.key])
        if name.parent.name != face:  # not in the layout of split_panorama
            continue
        groups.setdefault(str(name.parent.parent), {})[face] = f"{name.parent.as_posix()}/"
    configs = []
    for prefixes in groups.values():
        order = [f for f in FACES if f in prefixes]
        if len(order) < 2:
            continue
        ref = face_rotation(*FACES[order[0]])
        cameras = [pycolmap.RigConfigCamera(ref_sensor=True, image_prefix=prefixes[order[0]])]
        for face in order[1:]:
            cam_from_ref = face_rotation(*FACES[face]).T @ ref
            cameras.append(pycolmap.RigConfigCamera(
                ref_sensor=False, image_prefix=prefixes[face],
                cam_from_rig=pycolmap.Rigid3d(pycolmap.Rotation3d(cam_from_ref), np.zeros(3))))
        configs.append(pycolmap.RigConfig(cameras=cameras))
    return configs


def panorama_cameras(chunk: Chunk) -> list:
    """The cameras that are faces of a panorama."""
    return [c for c in chunk.cameras if c.meta.get("panorama") and c.meta.get("face") in FACES]


def face_names(chunk: Chunk) -> set[str]:
    """COLMAP image names (``meta["sfm"]["names"]``) of the faces of the panoramas."""
    names = (chunk.meta.get("sfm") or {}).get("names") or {}
    return {names[str(c.key)] for c in panorama_cameras(chunk) if str(c.key) in names}
