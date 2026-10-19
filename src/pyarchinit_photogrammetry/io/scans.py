"""Laser scans: E57 and PTX files as point clouds of the project.

* :func:`read_ptx` reads the scans of a PTX (Leica, Faro, Trimble exports in text): for every scan
  its grid of points in scanner coordinates, the 4 x 4 matrix that registers it and the scanner
  position; points at the origin (no return) are left out.
* :func:`read_e57` reads the ``data3D`` scans of an E57 (ASTM E2807) through pye57 (extra
  ``scans``): cartesian (or spherical) coordinates, colours, intensity, the pose of each scan.
* :func:`import_scans` makes a chunk of them: one cloud with every scan in its registered frame,
  written as the dense cloud of the new chunk (``point_cloud.ply``: colours, and the intensity in
  grey where there is no colour), its CRS the one given (else local); ``chunk.meta["scans"]`` lists
  the scans. The chunk is then brought onto the photogrammetric one with *Allinea chunk* (points,
  rigid) - or not at all when both are in the same CRS - merged, measured, used for DEM and
  sections as any dense cloud.
* :func:`colour_from_photos` gives the points of a cloud the colour of the aligned photos of
  another chunk: for every point the photo that sees it most squarely and closest, among those
  where it is not hidden by the surface (the model or the depth of the dense cloud).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..core.progress import Progress, as_progress


@dataclass
class Scan:
    name: str
    xyz: np.ndarray  # (N, 3) registered coordinates
    rgb: np.ndarray | None = None  # (N, 3) uint8
    intensity: np.ndarray | None = None  # (N,) 0..1
    position: np.ndarray = field(default_factory=lambda: np.zeros(3))  # scanner centre, registered

    def __len__(self) -> int:
        return len(self.xyz)


def read_ptx(path: str | os.PathLike) -> list[Scan]:
    """The scans of a PTX file (see the module docstring)."""
    path = Path(path)
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    scans, i = [], 0
    while i < len(lines):
        if not lines[i].strip():
            i += 1
            continue
        cols, rows = int(float(lines[i])), int(float(lines[i + 1]))
        position = np.array(lines[i + 2].split(), float)
        matrix = np.array([lines[i + 6 + k].split() for k in range(4)], float)  # row-major, translation last row
        n = cols * rows
        block = " ".join(lines[i + 10:i + 10 + n])
        values = np.array(block.split(), float)
        width = len(values) // max(n, 1)
        data = values[:n * width].reshape(n, width)
        xyz = data[:, :3]
        keep = np.any(xyz != 0, axis=1)
        T = matrix.T  # PTX writes the transposed (column vector) matrix
        reg = xyz[keep] @ T[:3, :3].T + T[:3, 3]
        rgb = data[keep, 4:7].astype(np.uint8) if width >= 7 else None
        inten = np.clip(data[keep, 3], 0, 1) if width >= 4 else None
        scans.append(Scan(f"{path.stem}_{len(scans) + 1}", reg, rgb, inten, position))  # already registered
        i += 10 + n
    return scans


def read_e57(path: str | os.PathLike) -> list[Scan]:
    """The scans of an E57 file (see the module docstring)."""
    try:
        import pye57
    except ImportError as exc:  # pragma: no cover - optional
        raise ImportError("E57 files need pye57: pip install 'pyarchinit-photogrammetry[scans]'") from exc
    e57 = pye57.E57(str(path))
    scans = []
    try:
        for k in range(e57.scan_count):
            raw = e57.read_scan_raw(k)
            header = e57.get_header(k)
            if "cartesianX" in raw:
                xyz = np.column_stack([raw["cartesianX"], raw["cartesianY"], raw["cartesianZ"]]).astype(float)
            else:  # spherical
                r, az, el = raw["sphericalRange"], raw["sphericalAzimuth"], raw["sphericalElevation"]
                xyz = np.column_stack([r * np.cos(el) * np.cos(az), r * np.cos(el) * np.sin(az), r * np.sin(el)])
            keep = np.isfinite(xyz).all(1)
            if "cartesianInvalidState" in raw:
                keep &= np.asarray(raw["cartesianInvalidState"]) == 0
            R = np.asarray(header.rotation_matrix, float)
            t = np.asarray(header.translation, float)
            reg = xyz[keep] @ R.T + t
            rgb = None
            if "colorRed" in raw:
                rgb = np.column_stack([raw["colorRed"], raw["colorGreen"], raw["colorBlue"]])[keep]
                rgb = np.clip(rgb, 0, 255).astype(np.uint8)
            inten = None
            if "intensity" in raw:
                v = np.asarray(raw["intensity"], float)[keep]
                lo, hi = (np.min(v), np.max(v)) if len(v) else (0.0, 1.0)
                inten = (v - lo) / max(hi - lo, 1e-12)
            try:
                name = header.node["name"].value()
            except Exception:  # an E57 without names
                name = None
            name = name or f"{Path(path).stem}_{k + 1}"
            scans.append(Scan(str(name), reg, rgb, inten, t))
    finally:
        e57.close()
    return scans


def read_scans(path: str | os.PathLike) -> list[Scan]:
    suffix = Path(path).suffix.lower()
    if suffix == ".ptx":
        return read_ptx(path)
    if suffix == ".e57":
        return read_e57(path)
    raise ValueError(f"{Path(path).name}: E57 or PTX")


def import_scans(doc, paths, label: str | None = None, crs: str | None = None, max_points: int | None = None,
                 progress: Progress | None = None):
    """A new chunk with the scans of ``paths`` as its dense cloud (see the module docstring);
    ``max_points``: a random subset when there are more. Returns the chunk."""
    from ..core import crs as crs_mod
    from ..core.pointcloud import PointCloud

    progress = as_progress(progress)
    scans = []
    for i, p in enumerate(paths):
        progress.check()
        progress(0.8 * i / max(len(paths), 1), f"Reading {Path(p).name}")
        scans += read_scans(p)
    if not scans:
        raise ValueError("no scan in the files")
    xyz = np.concatenate([s.xyz for s in scans])
    rgb = np.concatenate([s.rgb if s.rgb is not None else
                          np.repeat(((s.intensity if s.intensity is not None else np.full(len(s), 0.7)) * 255)
                                    .astype(np.uint8)[:, None], 3, axis=1) for s in scans])
    if max_points and len(xyz) > max_points:
        keep = np.sort(np.random.default_rng(0).choice(len(xyz), int(max_points), replace=False))
        xyz, rgb = xyz[keep], rgb[keep]
    chunk = doc.add_chunk(label or f"Scansioni laser ({len(scans)})")
    chunk.crs = crs or crs_mod.LOCAL
    origin = np.floor(xyz.min(0))  # the internal frame: metres from a round origin (float precision)
    chunk.transform = np.eye(4)
    chunk.transform[:3, 3] = origin
    pc = PointCloud(xyz - origin, rgb)
    path = chunk.data_dir / "point_cloud.ply"
    pc.save(path)
    chunk._set_product("point_cloud", path)
    chunk.meta["scans"] = [{"name": s.name, "points": int(len(s)), "position": [float(v) for v in s.position]}
                           for s in scans]
    chunk.meta["levelled"] = True
    chunk.meta["metric"] = True  # measured in metres even in local coordinates
    progress(1.0, f"{len(scans)} scans, {len(xyz):,} points")
    return chunk


def colour_from_photos(target, photos, max_photos: int | None = None, progress: Progress | None = None) -> dict:
    """The points of the dense cloud of ``target`` coloured from the aligned photos of ``photos``
    (a chunk in the same world frame); returns how many points got a colour."""
    from ..engine.visibility import ImageCache, Raycaster, project_valid, sample

    progress = as_progress(progress)
    pc = target.point_cloud
    if pc is None:
        raise RuntimeError("the chunk has no point cloud")
    cams = [c for c in photos.aligned_cameras]
    if not cams:
        raise RuntimeError(f"{photos.label} has no aligned photos")
    if max_photos:
        cams = cams[:max_photos]
    world = target.to_world(pc.xyz)
    pts = photos.to_internal(world)  # the frame of the photos
    mesh = photos.model if photos.product_path("model") is not None else None
    ray = Raycaster(np.asarray(mesh.vertices, float), np.asarray(mesh.faces, np.int64)) if mesh is not None else None
    best = np.full(len(pts), -np.inf)
    who = np.full(len(pts), -1, np.int64)
    pix = np.zeros((len(pts), 2), np.float32)
    for k, cam in enumerate(cams):
        progress.check()
        px, depth, ok = project_valid(photos, cam, pts)
        ok = np.asarray(ok, bool) & (np.asarray(depth) > 0)
        if not ok.any():
            continue
        idx = np.nonzero(ok)[0]
        centre = cam.transform[:3, 3]
        d = np.linalg.norm(pts[idx] - centre, axis=1)
        if ray is not None:  # hidden by the surface of the photos' model (scan and model differ a little)
            visible = ray.visible_from(centre, pts[idx], rel_tol=0.02)
            idx, d = idx[visible], d[visible]
        score = 1.0 / np.maximum(d, 1e-9)
        better = score > best[idx]
        sel = idx[better]
        best[sel], who[sel], pix[sel] = score[better], k, np.asarray(px, np.float32)[sel]
        progress(0.8 * (k + 1) / len(cams), f"Colours from the photos: {k + 1}/{len(cams)}")
    rgb = np.asarray(pc.rgb, np.uint8).copy() if pc.rgb is not None else np.full((len(pts), 3), 180, np.uint8)
    cache = ImageCache(photos, max_items=4)
    for k in np.unique(who[who >= 0]):
        sel = np.nonzero(who == k)[0]
        rgb[sel] = sample(cache.get(cams[k]), pix[sel])[:, ::-1]  # BGR -> RGB
    pc.rgb = rgb
    target._set_product("point_cloud", pc.save(target.own_product_path("point_cloud")))
    n = int((who >= 0).sum())
    progress(1.0, f"Colours from {len(cams)} photos for {n:,} of {len(pts):,} points")
    return {"coloured": n, "points": int(len(pts)), "photos": len(cams)}
