"""Synthetic excavation trench for tests and demos.

A textured height field (a small trench with a wall stub) with coded targets
at known coordinates is rendered from a set of cameras by ray casting. The
ground truth (surface, targets in a projected CRS) is returned so that the
whole pipeline can be checked metrically. A larger scene, a mound with targets
around it flown in a ring with noisy GPS in the EXIF, is made the same way
(:func:`make_ring_scene`, :func:`write_ring_dataset`), and a flat one flown once
along a closed circular strip of nadir photos with the targets under it, the
weak ring of the native mapper (:func:`write_weak_ring_dataset`). A wall that follows
a line of the plan round its corners, with marks at known lengths along its face, is a
mesh scene photographed from known poses (:func:`make_wall_scene`,
:func:`write_wall_dataset`, :func:`wall_plan`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from ..core.geometry import look_at
from ..markers.targets import target_image


@dataclass
class Scene:
    origin: tuple[float, float, float]  # world coordinates of local (0,0,0)
    crs: str
    size: tuple[float, float]  # metres (x, y)
    texel: float  # metres per texture pixel
    texture: np.ndarray  # BGR uint8, row 0 = max y
    heights: np.ndarray  # local z per texel
    targets: dict[int, tuple[float, float, float]] = field(default_factory=dict)  # code -> world xyz
    cameras: list[dict] = field(default_factory=list)
    stakes: dict[str, tuple[float, float, float]] = field(default_factory=dict)  # name -> world xyz of the head
    diameter: float | None = None  # metres: the diameter of the circle a ring is flown on (the weak ring)

    def height_at(self, x_local: np.ndarray, y_local: np.ndarray) -> np.ndarray:
        h, w = self.heights.shape
        col = np.clip(x_local / self.texel, 0, w - 1.001)
        row = np.clip((self.size[1] - y_local) / self.texel, 0, h - 1.001)
        return _sample(self.heights.astype(np.float32), np.ravel(col), np.ravel(row)).ravel()

    def world_height(self, X: np.ndarray, Y: np.ndarray) -> np.ndarray:
        return self.height_at(np.asarray(X) - self.origin[0], np.asarray(Y) - self.origin[1]) + self.origin[2]

    def surface(self, step: int = 2) -> tuple[np.ndarray, np.ndarray]:
        """The triangulated height field the photos are rendered from: vertices (local
        coordinates, float32) and faces (uint32), one vertex every ``step`` texels."""
        hs = self.heights[::step, ::step]
        gh, gw = hs.shape
        yy, xx = np.mgrid[0:gh, 0:gw]
        X = (xx * step + 0.5) * self.texel
        Y = self.size[1] - (yy * step + 0.5) * self.texel
        verts = np.column_stack([X.ravel(), Y.ravel(), hs.ravel()]).astype(np.float32)
        idx = np.arange(gh * gw).reshape(gh, gw)
        a, b, c, d = idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel(), idx[1:, :-1].ravel(), idx[1:, 1:].ravel()
        faces = np.vstack([np.column_stack([a, c, b]), np.column_stack([b, c, d])]).astype(np.uint32)
        return verts, faces


def _noise_texture(h: int, w: int, rng: np.random.Generator) -> np.ndarray:
    tex = np.zeros((h, w, 3), np.float32)
    for scale, amp in ((4, 40), (16, 50), (64, 60), (256, 50)):
        small = rng.random((max(2, h // scale), max(2, w // scale), 3)).astype(np.float32)
        tex += cv2.resize(small, (w, h), interpolation=cv2.INTER_CUBIC) * amp
    tex = tex - tex.min()
    tex = tex / tex.max() * 200 + 30
    base = np.array([70, 110, 150], np.float32)  # earthy BGR tint
    return np.clip(tex * 0.6 + base * 0.4, 0, 255).astype(np.uint8)


def make_scene(size=(4.0, 3.0), texel=0.002, crs="EPSG:3004", origin=(2480000.0, 4500000.0, 100.0),
               target_size=0.12, seed=7) -> Scene:
    rng = np.random.default_rng(seed)
    w, h = int(size[0] / texel), int(size[1] / texel)
    tex = _noise_texture(h, w, rng)
    yy, xx = np.mgrid[0:h, 0:w]
    x = (xx + 0.5) * texel
    y = size[1] - (yy + 0.5) * texel
    z = 0.08 * np.sin(x * 1.7) * np.cos(y * 2.1) + 0.04 * np.sin(x * 4.0 + 1.0)
    trench = (x > 1.0) & (x < 3.0) & (y > 0.9) & (y < 2.1)
    z = np.where(trench, z - 0.35, z)  # excavated area
    wall = (x > 1.7) & (x < 2.3) & (y > 1.3) & (y < 1.5)
    z = np.where(wall, -0.35 + 0.30, z)  # wall stub inside the trench
    z = cv2.GaussianBlur(z.astype(np.float32), (0, 0), 2.0)
    targets = {}
    positions = [(0.4, 0.4), (3.6, 0.4), (3.6, 2.6), (0.4, 2.6), (2.0, 0.45), (2.0, 2.55), (0.45, 1.5), (3.55, 1.5)]
    tpx = int(round(target_size / texel))
    for code, (tx, ty) in enumerate(positions):
        cx, cy, zc = _stamp_target(tex, z, texel, size[1], code, tx, ty, tpx)
        targets[code] = (origin[0] + cx, origin[1] + cy, origin[2] + zc)
    return Scene(origin=origin, crs=crs, size=size, texel=texel, texture=tex, heights=z.astype(np.float32),
                 targets=targets)


def _stamp_target(tex: np.ndarray, z: np.ndarray, texel: float, size_y: float, code: int, x_local: float,
                  y_local: float, tpx: int) -> tuple[float, float, float]:
    """Paint ArUco target ``code`` (``tpx`` texels across, a white quiet zone of a quarter of it around) on the
    texture ``tex`` at a local point and flatten the heights ``z`` under its plate. Returns the local (x, y, z) of the
    centre of the plate, which is where the target is."""
    pat = target_image("aruco_4x4_50", code, tpx)
    quiet = int(tpx * 0.25)
    tile = np.full((tpx + 2 * quiet, tpx + 2 * quiet), 255, np.uint8)
    tile[quiet:quiet + tpx, quiet:quiet + tpx] = pat
    c, r = int(x_local / texel), int((size_y - y_local) / texel)
    r0, c0 = r - tile.shape[0] // 2, c - tile.shape[1] // 2
    tex[r0:r0 + tile.shape[0], c0:c0 + tile.shape[1]] = tile[..., None]
    # targets lie on flat plates: flatten the surface under them
    zc = float(z[r, c])
    z[r0:r0 + tile.shape[0], c0:c0 + tile.shape[1]] = zc
    cx = (c0 + tile.shape[1] / 2) * texel
    cy = size_y - (r0 + tile.shape[0] / 2) * texel
    return cx, cy, zc


def make_ring_scene(seed: int = 0, texel: float = 0.04, size=(80.0, 80.0), target_radius: float = 16.0,
                    n_targets: int = 8, mound: bool = True) -> Scene:
    """A mound to fly a ring around: ``size`` metres (80 m x 80 m by default) of the ground of :func:`make_scene` (its
    noise texture and small relief, its synthetic origin and CRS: no real place) without the trench, with a Gaussian
    mound 4 m high and 8 m wide (sigma) at the centre unless ``mound`` is False, and ``n_targets`` ArUco targets
    (codes 0 to ``n_targets`` - 1) on flat plates, their centres on a circle of radius ``target_radius`` around the
    centre, one every ``360 / n_targets`` degrees from the +x direction. A target is 0.96 m across: 24 texels at the
    default ``texel`` of 0.04 m, 4 for each of the 6 cells of the pattern with its border (texture 2000 x 2000
    texels, 12 MB; heights 16 MB). The defaults give the scene of the ring of :func:`write_ring_dataset`."""
    origin, crs = (2480000.0, 4500000.0, 100.0), "EPSG:3004"  # the defaults of make_scene
    size = (float(size[0]), float(size[1]))
    rng = np.random.default_rng(seed)
    w, h = int(round(size[0] / texel)), int(round(size[1] / texel))
    tex = _noise_texture(h, w, rng)
    yy, xx = np.mgrid[0:h, 0:w]
    x = (xx + 0.5) * texel
    y = size[1] - (yy + 0.5) * texel
    z = 0.08 * np.sin(x * 1.7) * np.cos(y * 2.1) + 0.04 * np.sin(x * 4.0 + 1.0)  # the small relief of make_scene
    if mound:
        z = z + 4.0 * np.exp(-((x - size[0] / 2) ** 2 + (y - size[1] / 2) ** 2) / (2 * 8.0 ** 2))  # the mound
    z = z.astype(np.float32)
    targets = {}
    tpx = int(round(0.96 / texel))
    for code in range(n_targets):
        angle = np.radians(360.0 / n_targets * code)
        cx, cy, zc = _stamp_target(tex, z, texel, size[1], code, size[0] / 2 + target_radius * np.cos(angle),
                                   size[1] / 2 + target_radius * np.sin(angle), tpx)
        targets[code] = (origin[0] + cx, origin[1] + cy, origin[2] + zc)
    return Scene(origin=origin, crs=crs, size=size, texel=texel, texture=tex, heights=z, targets=targets)


def _texel_of(scene: Scene, x_local: float, y_local: float) -> tuple[float, float]:
    """(column, row) of the texture at a local point, in OpenCV pixel units (centre of the first texel at 0, 0)."""
    return x_local / scene.texel - 0.5, (scene.size[1] - y_local) / scene.texel - 0.5


def add_stakes(scene: Scene, xy_local, diameter: float = 0.05, value: int = 25) -> dict[str, tuple]:
    """Dark round heads of stakes (or benchmarks) painted on the ground at the local points ``xy_local``: discs of
    ``diameter`` metres and grey ``value``, their edge texels blended by the part of them the disc covers. Returns the
    world xyz of each head (the surface under its centre) by name ``S1``, ``S2``... (numbered after those the scene
    has already), kept in ``scene.stakes`` too."""
    sub = 4  # supersampling of the edge texels
    r = diameter / 2.0
    out = {}
    for x, y in np.asarray(xy_local, float).reshape(-1, 2):
        c, rr = _texel_of(scene, x, y)
        rad = r / scene.texel
        c0, c1 = int(np.floor(c - rad - 1)), int(np.ceil(c + rad + 1)) + 1
        r0, r1 = int(np.floor(rr - rad - 1)), int(np.ceil(rr + rad + 1)) + 1
        offs = (np.arange(sub) + 0.5) / sub - 0.5
        cols = np.arange(c0, c1)[None, :, None, None] + offs[None, None, None, :]
        rows = np.arange(r0, r1)[:, None, None, None] + offs[None, None, :, None]
        cover = (((cols - c) ** 2 + (rows - rr) ** 2) <= rad ** 2).mean(axis=(2, 3))
        patch = scene.texture[r0:r1, c0:c1].astype(np.float32)
        patch = patch * (1 - cover[..., None]) + float(value) * cover[..., None]
        scene.texture[r0:r1, c0:c1] = np.clip(np.round(patch), 0, 255).astype(np.uint8)
        name = f"S{len(scene.stakes) + 1}"
        z = float(scene.height_at(np.array([x]), np.array([y]))[0])
        scene.stakes[name] = (scene.origin[0] + float(x), scene.origin[1] + float(y), scene.origin[2] + z)
        out[name] = scene.stakes[name]
    return out


def add_blotches(scene: Scene, n: int, rng: np.random.Generator) -> list[dict]:
    """``n`` dark patches that are not stake heads: elongated ones (a root, a crack, a shadow: 0.18-0.30 m by 2.5-3.5
    cm) and large irregular ones (0.3-0.5 m across), alternately, kept 0.35 m away from the stakes and the targets of
    the scene. Returns ``[{"kind": "elongated"|"large", "centre": world (x, y), "extent": half its largest size in
    metres}]``."""
    keep_away = [(x - scene.origin[0], y - scene.origin[1]) for x, y, _ in
                 list(scene.stakes.values()) + list(scene.targets.values())]
    out = []
    for i in range(int(n)):
        kind = "elongated" if i % 2 == 0 else "large"
        extent = 0.15 if kind == "elongated" else 0.25
        for _ in range(200):
            x = rng.uniform(extent + 0.1, scene.size[0] - extent - 0.1)
            y = rng.uniform(extent + 0.1, scene.size[1] - extent - 0.1)
            if all(np.hypot(x - a, y - b) > extent + 0.35 for a, b in keep_away):
                break
        else:
            continue
        c, r = _texel_of(scene, x, y)
        value = int(rng.integers(20, 40))
        if kind == "elongated":
            length, width = rng.uniform(0.18, 0.30), rng.uniform(0.025, 0.035)
            extent = length / 2
            cv2.ellipse(scene.texture, (int(round(c)), int(round(r))),
                        (int(round(length / 2 / scene.texel)), int(round(width / 2 / scene.texel))),
                        float(rng.uniform(0, 180)), 0, 360, (value, value, value), -1, cv2.LINE_AA)
        else:
            angles = np.sort(rng.uniform(0, 2 * np.pi, 12))
            radii = rng.uniform(0.15, 0.25, 12)
            extent = float(radii.max())
            poly = np.column_stack([c + radii / scene.texel * np.cos(angles), r + radii / scene.texel * np.sin(angles)])
            cv2.fillPoly(scene.texture, [np.round(poly).astype(np.int32)], (value, value, value), cv2.LINE_AA)
        keep_away.append((x, y))
        out.append({"kind": kind, "centre": (scene.origin[0] + x, scene.origin[1] + y), "extent": extent})
    return out


def camera_poses(scene: Scene, height: float = 2.2, nx: int = 5, ny: int = 4, tilt_ring: bool = True):
    """Nadir grid plus an oblique ring (local coordinates)."""
    poses = []
    sx, sy = scene.size
    for j in range(ny):
        for i in range(nx):
            eye = np.array([sx * (i + 0.5) / nx, sy * (j + 0.5) / ny, height])
            target = eye - np.array([0, 0, height]) + np.array([0.05 * (i - nx / 2), 0.05 * (j - ny / 2), 0])
            poses.append(look_at(eye, target, up=(0, 1, 0)))
    if tilt_ring:
        c = np.array([sx / 2, sy / 2, -0.2])
        for a in np.linspace(0, 2 * np.pi, 8, endpoint=False):
            eye = c + np.array([1.8 * np.cos(a), 1.4 * np.sin(a), 2.0])
            poses.append(look_at(eye, c, up=(0, 0, 1)))
    return poses


def ring_poses(n: int = 72, diameter: float = 60.0, height: float = 12.0) -> list[np.ndarray]:
    """``n`` cameras evenly spaced on a circle of ``diameter`` around (0, 0) at ``height``, the first on the +x
    axis, each with its optical axis through (0, 0, 0): world_from_cam poses as :func:`camera_poses` gives them.
    They are drawn around the origin; :func:`write_ring_dataset` moves them to the centre of its scene."""
    poses = []
    for a in np.linspace(0, 2 * np.pi, n, endpoint=False):
        eye = np.array([diameter / 2 * np.cos(a), diameter / 2 * np.sin(a), height])
        poses.append(look_at(eye, np.zeros(3), up=(0, 0, 1)))
    return poses


def weak_ring_poses(n: int = 80, diameter: float = 60.0, height: float = 15.0) -> list[np.ndarray]:
    """``n`` nadir cameras evenly spaced on a circle of ``diameter`` around (0, 0) at ``height``, the first on the +x
    axis, in the order of the angle (one sense of travel): the optical axis straight down, the x axis of each camera
    (the long side of its image) along the radius, outwards, across the track, and its y axis along the tangent,
    against the sense of travel, so that the top of every image looks ahead to the next photo. world_from_cam poses as
    :func:`ring_poses` gives them, drawn around the origin."""
    poses = []
    for a in np.linspace(0, 2 * np.pi, n, endpoint=False):
        radial = np.array([np.cos(a), np.sin(a), 0.0])
        down = np.array([0.0, 0.0, -1.0])
        pose = np.eye(4)
        pose[:3, 0], pose[:3, 1], pose[:3, 2] = radial, np.cross(down, radial), down
        pose[:3, 3] = (diameter / 2 * np.cos(a), diameter / 2 * np.sin(a), height)
        poses.append(pose)
    return poses


def render(scene: Scene, world_from_cam: np.ndarray, K: np.ndarray, size=(1200, 900), k1: float = -0.04) -> np.ndarray:
    """Ray-cast the textured height field (local frame). Applies radial distortion ``k1``."""
    import open3d as o3d

    if not hasattr(scene, "_rc"):
        verts, faces = scene.surface(step=2)
        rc = o3d.t.geometry.RaycastingScene()
        rc.add_triangles(o3d.core.Tensor(verts), o3d.core.Tensor(faces))
        scene._rc = rc
    W, H = size
    # distorted pixel grid -> undistorted normalised rays
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float64)
    xn = (xx + 0.5 - K[0, 2]) / K[0, 0]
    yn = (yy + 0.5 - K[1, 2]) / K[1, 1]
    xu, yu = xn.copy(), yn.copy()
    for _ in range(8):  # invert x_d = x_u (1 + k1 r^2)
        r2 = xu ** 2 + yu ** 2
        xu, yu = xn / (1 + k1 * r2), yn / (1 + k1 * r2)
    dirs_cam = np.dstack([xu, yu, np.ones_like(xu)]).reshape(-1, 3)
    with np.errstate(all="ignore"):  # numpy + Apple Accelerate can raise spurious FP warnings in matmul
        dirs = dirs_cam @ world_from_cam[:3, :3].T
    dirs /= np.linalg.norm(dirs, axis=1, keepdims=True)
    origins = np.repeat(world_from_cam[:3, 3][None], len(dirs), 0)
    ans = scene._rc.cast_rays(o3d.core.Tensor(np.hstack([origins, dirs]).astype(np.float32)))
    t = ans["t_hit"].numpy()
    hit = np.isfinite(t)
    p = origins + dirs * np.where(hit, t, 0)[:, None]
    col = p[:, 0] / scene.texel - 0.5
    row = (scene.size[1] - p[:, 1]) / scene.texel - 0.5
    img = cv2.remap(scene.texture, col.astype(np.float32).reshape(H, W), row.astype(np.float32).reshape(H, W),
                    cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(90, 90, 90))
    img = img.reshape(-1, 3)
    img[~hit] = (200, 210, 220)
    return img.reshape(H, W, 3)


def write_dataset(out_dir: str | Path, scene: Scene | None = None, n_x: int = 5, n_y: int = 4,
                  size=(1200, 900), focal: float = 1000.0, k1: float = -0.04, tilt_ring: bool = True) -> Scene:
    """Render photos to ``out_dir/photos`` and write ``out_dir/gcp.csv`` (code, X, Y, Z)."""
    out = Path(out_dir)
    (out / "photos").mkdir(parents=True, exist_ok=True)
    scene = scene or make_scene()
    K = np.array([[focal, 0, size[0] / 2], [0, focal, size[1] / 2], [0, 0, 1]])
    for i, pose in enumerate(camera_poses(scene, nx=n_x, ny=n_y, tilt_ring=tilt_ring)):
        img = render(scene, pose, K, size, k1)
        cv2.imwrite(str(out / "photos" / f"IMG_{i:04d}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 95])
        world_pose = pose.copy()
        world_pose[:3, 3] += np.asarray(scene.origin)
        scene.cameras.append({"label": f"IMG_{i:04d}", "world_from_cam": world_pose})
    _write_gcp_csv(scene, out / "gcp.csv")
    return scene


def _write_gcp_csv(scene: Scene, path: Path) -> None:
    """The targets of a scene as a control point file (code, X, Y, Z in the scene's CRS)."""
    with open(path, "w") as fh:
        fh.write("# label,X,Y,Z (" + scene.crs + ")\n")
        for code, (x, y, z) in scene.targets.items():
            fh.write(f"{code},{x:.4f},{y:.4f},{z:.4f}\n")


def write_ring_dataset(out_dir: str | Path, n: int = 72, diameter: float = 60.0, height: float = 12.0,
                       size=(1200, 900), focal: float = 1000.0, k1: float = -0.04, gps_noise=(0.3, 0.5),
                       seed: int = 0) -> Scene:
    """Render a ring flight around the mound of :func:`make_ring_scene` to ``out_dir/photos`` and write
    ``out_dir/gcp.csv`` (code, X, Y, Z), as :func:`write_dataset` does for the trench. The ``n`` cameras of
    :func:`ring_poses` (``diameter``, ``height``) stand around the centre of the scene, with the lens of the trench
    (``k1``). Every photo carries its focal length and a GPS position in the EXIF: the true centre plus Gaussian
    noise of ``gps_noise`` metres (in plan, in height) from a generator seeded with ``seed`` (not the one that draws
    the texture), converted to EPSG:4326 with pyproj, the height written as it is. ``scene.cameras`` holds the true
    world poses. The origin and CRS are the synthetic ones of :func:`make_scene`: no real place."""
    from pyproj import Transformer

    out = Path(out_dir)
    (out / "photos").mkdir(parents=True, exist_ok=True)
    scene = make_ring_scene(seed=seed)
    K = np.array([[focal, 0, size[0] / 2], [0, focal, size[1] / 2], [0, 0, 1]])
    to_wgs84 = Transformer.from_crs(scene.crs, "EPSG:4326", always_xy=True)
    noise = np.random.default_rng(seed)
    sigma = np.array([gps_noise[0], gps_noise[0], gps_noise[1]], float)
    for i, pose in enumerate(ring_poses(n, diameter, height)):
        pose[:2, 3] += np.asarray(scene.size) / 2  # drawn around (0, 0): stood around the centre of the scene
        img = render(scene, pose, K, size, k1)
        world_pose = pose.copy()
        world_pose[:3, 3] += np.asarray(scene.origin)
        gx, gy, gz = world_pose[:3, 3] + noise.normal(0.0, sigma)
        lon, lat = to_wgs84.transform(gx, gy)
        (out / "photos" / f"RING_{i:04d}.jpg").write_bytes(
            _jpeg_with_focal(img, 36.0 * focal / max(size), gps=(lat, lon, gz)))
        scene.cameras.append({"label": f"RING_{i:04d}", "world_from_cam": world_pose})
    _write_gcp_csv(scene, out / "gcp.csv")
    return scene


# The gate of the native mapper on the weak ring (write_weak_ring_dataset with its defaults, the chain "optimize" of
# ppg bench synthetic weak-ring). "shape": the largest residual of the triangulated targets after one similarity
# fitted on all of them, before any optimization, over the diameter (bench.truth.ring_shape; spec 8.4: within 0.5 %).
# "control_m" and "check_m": 1.25 times the better median, of COLMAP's incremental and global mappers (three runs
# each), of the control and check errors after the optimization of the cameras, rounded up to the millimetre.
# Measured on 2026-11-22 (ppg bench synthetic weak-ring --steps optimize, --mapper incremental and --mapper global):
# the incremental mapper is the better one, medians 0.000950 m (control) and 0.010209 m (check); the global mapper
# placed no photo in any run and fell back to the incremental one (medians 0.000972 m and 0.010848 m).
WEAK_RING_GATE = {"shape": 0.005, "control_m": 0.002, "check_m": 0.013}


def write_weak_ring_dataset(out_dir: str | Path, n: int = 80, diameter: float = 60.0, height: float = 15.0,
                            size=(1200, 900), focal: float = 1000.0, k1: float = -0.04, gps_noise=(0.3, 0.5),
                            seed: int = 0) -> Scene:
    """A ring flown once: the ``n`` nadir photos of :func:`weak_ring_poses` (``diameter``, ``height``) along one
    closed circular strip around the centre of a flat scene of 90 m x 90 m (:func:`make_ring_scene` with a texel of
    0.02 m and no mound), each overlapping only its neighbours, with 12 targets on the circle under the strip (one
    every 30 degrees from +x). Written to ``out_dir/photos`` (``WRING_0000.jpg``, ...) with ``out_dir/gcp.csv``
    (code, X, Y, Z), as :func:`write_ring_dataset` writes its ring: the lens of the trench (``k1``), the focal
    length and a GPS position in the EXIF of every photo (the true centre plus Gaussian noise of ``gps_noise``
    metres in plan and in height, from a generator seeded with ``seed``), no accuracy of the position, so the program
    reads its default. ``scene.cameras`` holds the true world poses, ``scene.diameter`` the diameter. At the defaults
    a photo covers 18 m across the track and 13.5 m along it (1.5 cm per pixel), the forward overlap is about 82 %.
    The origin and CRS are the synthetic ones of :func:`make_scene`: no real place."""
    from pyproj import Transformer

    out = Path(out_dir)
    (out / "photos").mkdir(parents=True, exist_ok=True)
    scene = make_ring_scene(seed, texel=0.02, size=(90.0, 90.0), target_radius=diameter / 2, n_targets=12,
                            mound=False)
    scene.diameter = float(diameter)
    K = np.array([[focal, 0, size[0] / 2], [0, focal, size[1] / 2], [0, 0, 1]])
    to_wgs84 = Transformer.from_crs(scene.crs, "EPSG:4326", always_xy=True)
    noise = np.random.default_rng(seed)
    sigma = np.array([gps_noise[0], gps_noise[0], gps_noise[1]], float)
    for i, pose in enumerate(weak_ring_poses(n, diameter, height)):
        pose[:2, 3] += np.asarray(scene.size) / 2  # drawn around (0, 0): stood around the centre of the scene
        img = render(scene, pose, K, size, k1)
        world_pose = pose.copy()
        world_pose[:3, 3] += np.asarray(scene.origin)
        gx, gy, gz = world_pose[:3, 3] + noise.normal(0.0, sigma)
        lon, lat = to_wgs84.transform(gx, gy)
        (out / "photos" / f"WRING_{i:04d}.jpg").write_bytes(
            _jpeg_with_focal(img, 36.0 * focal / max(size), gps=(lat, lon, gz)))
        scene.cameras.append({"label": f"WRING_{i:04d}", "world_from_cam": world_pose})
    _write_gcp_csv(scene, out / "gcp.csv")
    return scene


# outer profiles (radius, height) of the vessel shapes of :func:`make_vessel`, from the base edge to the rim
VESSEL_SHAPES = {
    "jar": ((0.045, 0.0), (0.075, 0.03), (0.10, 0.08), (0.09, 0.13), (0.05, 0.17), (0.045, 0.185), (0.055, 0.20)),
    "plate": ((0.05, 0.0), (0.07, 0.006), (0.10, 0.016), (0.12, 0.025), (0.13, 0.03)),
    "beaker": ((0.035, 0.0), (0.037, 0.03), (0.039, 0.06), (0.041, 0.09), (0.044, 0.12)),
}


def _shape_profile(shape: str, thickness: float) -> np.ndarray:
    """Profile (outer surface from the base centre to the lip, then the inner surface back to the
    axis) of a named shape: the inner surface is the outer one moved inwards by ``thickness``."""
    from scipy.interpolate import PchipInterpolator

    ctrl = np.array(VESSEL_SHAPES[shape], float)
    s = np.r_[0.0, np.cumsum(np.hypot(*np.diff(ctrl, axis=0).T))]
    t = np.linspace(0.0, s[-1], 80)
    wall = np.column_stack([PchipInterpolator(s, ctrl[:, 0])(t), PchipInterpolator(s, ctrl[:, 1])(t)])
    base = np.column_stack([np.linspace(0.0, wall[0, 0], 12, endpoint=False), np.zeros(12)])
    outer = np.vstack([base, wall])
    tang = np.gradient(outer, axis=0)
    tang /= np.linalg.norm(tang, axis=1, keepdims=True)
    inner = outer + thickness * np.column_stack([-tang[:, 1], tang[:, 0]])  # to the left = inside
    inner[0, 0] = 0.0
    inner[:, 0] = np.maximum(inner[:, 0], 0.0)
    return np.vstack([outer, inner[::-1]])


def _handle(prof: np.ndarray, angle: float, thickness: float):
    """A loop handle on the outside of the wall at ``angle`` degrees (a torus in the vertical plane
    through the axis, half sunk into the wall), between a third and two thirds of the height."""
    import trimesh

    outer = prof[: len(prof) // 2]
    h0, h1 = outer[:, 1].max() * 0.35, outer[:, 1].max() * 0.75
    mid = outer[np.argmin(np.abs(outer[:, 1] - (h0 + h1) / 2))]
    major = (h1 - h0) / 2
    tor = trimesh.creation.torus(major_radius=major, minor_radius=max(thickness, 0.004), major_sections=48,
                                 minor_sections=12)
    tor.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))  # into the x-z plane
    tor.apply_translation([mid[0] + 0.3 * major, 0.0, (h0 + h1) / 2])
    tor.apply_transform(trimesh.transformations.rotation_matrix(np.radians(angle), [0, 0, 1]))
    return tor


def make_vessel(rim_r: float = 0.08, height: float = 0.12, thickness: float = 0.006, sectors: int = 128,
                sherd: tuple[float, float] | None = None, noise: float = 0.0, seed: int = 3,
                shape: str = "bowl", above: float | None = None, handles: int = 0):
    """A wheel-made vessel as a :class:`trimesh.Trimesh` (metres, axis = +Z, base centre at the origin).

    ``shape``: ``bowl`` (the default, an open bowl of rim radius ``rim_r`` and ``height``), ``jar``
    (closed form, belly 20 cm across, neck 9 cm, everted rim 11 cm, 20 cm high), ``plate`` (26 cm
    across, 3.5 cm high) or ``beaker`` (an almost cylindrical cup, rim 8.8 cm, 12 cm high); the
    named shapes ignore ``rim_r`` and ``height``. ``sherd=(a0, a1)`` keeps only the angular sector
    between ``a0`` and ``a1`` degrees (a fragment); ``above`` keeps only the part above that height
    (metres: a rim sherd, or a vessel broken below the shoulder); ``handles`` loop handles evenly
    spaced around the vessel (inside the sherd when there is one), ``noise`` Gaussian roughness
    (metres).
    """
    import trimesh

    if shape == "bowl":
        hs = np.linspace(0, height, 60)
        r_out = 0.04 + (rim_r - 0.04) * np.sin(hs / height * np.pi / 2) ** 0.7 + 0.01 * np.sin(hs / height * np.pi)
        prof = np.vstack([[0, 0], np.column_stack([r_out, hs]), np.column_stack([(r_out - thickness)[::-1], hs[::-1]]),
                          [0, thickness]])
    elif shape in VESSEL_SHAPES:
        prof = _shape_profile(shape, thickness)
    else:
        raise ValueError(f"unknown vessel shape {shape!r}: bowl, {', '.join(VESSEL_SHAPES)}")
    full = prof
    ring = above is not None
    if ring:  # outer and inner surfaces are cut at the same index: the profile stays symmetric
        half = len(prof) // 2
        outer, inner = prof[:half], prof[half:][::-1]
        keep = (outer[:, 1] >= above) & (inner[:, 1] >= above)
        prof = np.vstack([outer[keep], inner[keep][::-1]])
    if sherd is None:
        a = np.linspace(0, 2 * np.pi, sectors, endpoint=False)
        closed = True
    else:
        a = np.radians(np.linspace(sherd[0], sherd[1], max(8, int(sectors * abs(sherd[1] - sherd[0]) / 360))))
        if not ring:
            prof = prof[1:-1]  # a fragment has no base centre
        closed = False
    v = np.vstack([np.column_stack([prof[:, 0] * np.cos(x), prof[:, 0] * np.sin(x), prof[:, 1]]) for x in a])
    m = len(prof)
    f = []
    n_seg = len(a) if closed else len(a) - 1
    for i in range(n_seg):
        j = (i + 1) % len(a)
        for k in range(m - 1):
            f += [[i * m + k, j * m + k, i * m + k + 1], [i * m + k + 1, j * m + k, j * m + k + 1]]
        if ring:  # the lower fracture: from the inner surface back to the outer one
            f += [[i * m + m - 1, j * m + m - 1, i * m], [i * m, j * m + m - 1, j * m]]
    if not closed:  # cap the two fracture faces
        for i, flip in ((0, False), (len(a) - 1, True)):
            for k in range(m // 2 - 1):
                p, q, r, s_ = i * m + k, i * m + k + 1, i * m + m - 1 - k, i * m + m - 2 - k
                t1, t2 = [p, q, r], [q, s_, r]
                f += [t1[::-1], t2[::-1]] if not flip else [t1, t2]
    if noise > 0:
        v = v + np.random.default_rng(seed).normal(0, noise, v.shape)
    mesh = trimesh.Trimesh(v, np.array(f), process=True)
    if handles:
        a0, a1 = (sherd if sherd is not None else (0.0, 360.0))
        step = (a1 - a0) / (handles + (0 if sherd is None else 1))
        parts = [mesh]
        for i in range(handles):
            tor = _handle(full, a0 + step * (i + (0 if sherd is None else 1)), thickness)
            if noise > 0:
                tor.vertices = tor.vertices + np.random.default_rng(seed + 1 + i).normal(0, noise, tor.vertices.shape)
            parts.append(tor)
        mesh = trimesh.util.concatenate(parts)
    return mesh


# ---------------------------------------------------------------- pottery on a turntable
@dataclass
class MeshScene:
    """A textured triangle mesh scene rendered by ray casting (local metric frame).

    ``mat_faces`` triangles take their colour from ``mat_texture`` by XY position (a flat
    mat with coded targets at z = 0); the other triangles use ``surface_color(points)``."""
    vertices: np.ndarray
    faces: np.ndarray
    mat_faces: int  # the first ``mat_faces`` triangles are the mat
    mat_size: float
    mat_texel: float
    mat_texture: np.ndarray  # BGR, row 0 = +y edge of the mat
    surface_color: object  # callable (N,3) points -> (N,3) BGR float
    targets: dict[int, tuple[float, float, float]] = field(default_factory=dict)
    cameras: list[dict] = field(default_factory=list)


def _sample(texture: np.ndarray, col: np.ndarray, row: np.ndarray) -> np.ndarray:
    """Bilinear lookup of ``texture`` at float pixel coordinates for any number of samples
    (cv2.remap wants maps with fewer than 32767 rows, so the samples are laid out in rows)."""
    n = len(col)
    width = 1024
    rows = (n + width - 1) // width
    pad = rows * width - n
    c = np.concatenate([col, np.zeros(pad)]).astype(np.float32).reshape(rows, width)
    r = np.concatenate([row, np.zeros(pad)]).astype(np.float32).reshape(rows, width)
    out = cv2.remap(texture, c, r, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return out.reshape(rows * width, -1)[:n]


def _pottery_texture(rng: np.random.Generator, h: int = 768, w: int = 1536) -> np.ndarray:
    """Terracotta surface with fired-colour variations, inclusions and speckles (BGR float).

    Full-contrast multi-scale noise, as the trench texture: the matchers need it."""
    tex = _noise_texture(h, w, rng).astype(np.float32)  # 30..230, earthy tint
    gray = tex.mean(axis=2, keepdims=True) / 255.0
    for _ in range(4000):  # grog and lime inclusions
        r, c = int(rng.integers(0, h)), int(rng.integers(0, w))
        cv2.circle(gray, (c, r), int(rng.integers(1, 4)), float(rng.choice([0.08, 0.98])), -1)
    base = np.array([55, 90, 170], np.float32)
    return np.clip(base[None, None, :] * (0.35 + 0.95 * gray), 0, 255)


def make_turntable_scene(n_targets: int = 12, diameter_mm: float = 180.0, marker_mm: float = 22.0,
                         mat_size: float = 0.32, texel: float = 0.0004, vessel=None, seed: int = 11) -> MeshScene:
    """A bowl (``make_vessel``) standing on a turntable mat with a ring of ArUco targets,
    the same layout as ``ppg markers turntable`` (``turntable_layout``)."""
    from ..markers.targets import turntable_layout

    rng = np.random.default_rng(seed)
    n = int(round(mat_size / texel))
    tex = _noise_texture(n, n, rng).astype(np.float32)
    tex = np.clip(tex.mean(axis=2, keepdims=True) * 0.7 + 70, 0, 255)  # printed grey grain, full contrast
    tex = np.repeat(tex, 3, axis=2)
    speck = rng.random((n, n)) < 0.006  # printed speckles between the targets
    tex[speck] = 25
    tex = tex.astype(np.uint8)
    layout = turntable_layout(n_targets, diameter_mm)
    tpx = int(round(marker_mm / 1000.0 / texel))
    for code, (tx, ty, _) in layout.items():
        pat = target_image("aruco_4x4_50", code, tpx)
        quiet = int(tpx * 0.3)
        tile = np.full((tpx + 2 * quiet, tpx + 2 * quiet), 255, np.uint8)
        tile[quiet:quiet + tpx, quiet:quiet + tpx] = pat
        c = int((tx + mat_size / 2) / texel)
        r = int((mat_size / 2 - ty) / texel)
        r0, c0 = r - tile.shape[0] // 2, c - tile.shape[1] // 2
        tex[r0:r0 + tile.shape[0], c0:c0 + tile.shape[1]] = tile[..., None]
    # a printed ring and some ink marks give the matcher texture between the targets
    yy, xx = np.mgrid[0:n, 0:n]
    rr = np.hypot((xx + 0.5) * texel - mat_size / 2, mat_size / 2 - (yy + 0.5) * texel)
    ring = np.abs(rr - diameter_mm / 2000.0) < texel * 1.5
    tex[ring] = (40, 40, 40)
    half = mat_size / 2
    mat_v = np.array([[-half, -half, 0], [half, -half, 0], [half, half, 0], [-half, half, 0]], np.float32)
    mat_f = np.array([[0, 1, 2], [0, 2, 3]], np.uint32)
    vessel = vessel if vessel is not None else make_vessel()
    vv = np.asarray(vessel.vertices, np.float32)
    vf = np.asarray(vessel.faces, np.uint32) + len(mat_v)
    pottery = _pottery_texture(rng)

    def surface_color(p: np.ndarray) -> np.ndarray:
        theta = np.arctan2(p[:, 1], p[:, 0])
        u = (theta + np.pi) / (2 * np.pi) * (pottery.shape[1] - 1)
        v = np.clip(p[:, 2], 0, 0.13) / 0.13 * (pottery.shape[0] - 1)
        return _sample(pottery, u, v)

    return MeshScene(np.vstack([mat_v, vv]), np.vstack([mat_f, vf]), len(mat_f), mat_size, texel, tex,
                     surface_color, targets=dict(layout))


def turntable_poses(radius: float = 0.42, elevations=(24.0, 50.0), counts=(20, 12), target=(0.0, 0.0, 0.05)):
    """Camera rings around the object, looking at ``target``: a low ring for the body and a
    high one for the rim and the inside (32 photos by default). The base on the mat is never
    seen, as on a real turntable."""
    poses = []
    for elev, count in zip(elevations, counts):
        e = np.radians(elev)
        for k in range(count):
            a = 2 * np.pi * k / count + (0.1 if elev > 45 else 0.0)
            eye = np.array([radius * np.cos(e) * np.cos(a), radius * np.cos(e) * np.sin(a), radius * np.sin(e) + target[2]])
            poses.append(look_at(eye, np.asarray(target, float), up=(0, 0, 1)))
    return poses


def render_mesh(scene: MeshScene, world_from_cam: np.ndarray, K: np.ndarray, size=(1200, 900), k1: float = -0.03,
                light=(0.3, -0.5, 0.8)) -> np.ndarray:
    """Ray-cast a :class:`MeshScene` with Lambert shading (radial distortion ``k1``)."""
    import open3d as o3d

    if not hasattr(scene, "_rc"):
        rc = o3d.t.geometry.RaycastingScene()
        rc.add_triangles(o3d.core.Tensor(scene.vertices.astype(np.float32)), o3d.core.Tensor(scene.faces.astype(np.uint32)))
        scene._rc = rc
    W, H = size
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float64)
    xn = (xx + 0.5 - K[0, 2]) / K[0, 0]
    yn = (yy + 0.5 - K[1, 2]) / K[1, 1]
    xu, yu = xn.copy(), yn.copy()
    for _ in range(8):
        r2 = xu ** 2 + yu ** 2
        xu, yu = xn / (1 + k1 * r2), yn / (1 + k1 * r2)
    dirs_cam = np.dstack([xu, yu, np.ones_like(xu)]).reshape(-1, 3)
    with np.errstate(all="ignore"):
        dirs = dirs_cam @ world_from_cam[:3, :3].T
    dirs /= np.linalg.norm(dirs, axis=1, keepdims=True)
    origins = np.repeat(world_from_cam[:3, 3][None], len(dirs), 0)
    ans = scene._rc.cast_rays(o3d.core.Tensor(np.hstack([origins, dirs]).astype(np.float32)))
    t = ans["t_hit"].numpy()
    hit = np.isfinite(t)
    p = origins + dirs * np.where(hit, t, 0)[:, None]
    prim = ans["primitive_ids"].numpy()
    normals = ans["primitive_normals"].numpy()
    img = np.full((len(dirs), 3), (120, 120, 120), np.float32)
    on_mat = hit & (prim < scene.mat_faces)
    if on_mat.any():
        col = (p[on_mat, 0] + scene.mat_size / 2) / scene.mat_texel - 0.5
        row = (scene.mat_size / 2 - p[on_mat, 1]) / scene.mat_texel - 0.5
        img[on_mat] = _sample(scene.mat_texture, col, row)
    on_obj = hit & ~on_mat
    if on_obj.any():
        img[on_obj] = scene.surface_color(p[on_obj])
    lv = np.asarray(light, float)
    lv /= np.linalg.norm(lv)
    shade = np.abs(normals @ lv)
    shade = np.where(hit, 0.45 + 0.55 * shade, 1.0)
    img = np.clip(img * shade[:, None], 0, 255).astype(np.uint8)
    return img.reshape(H, W, 3)


def _jpeg_with_focal(img: np.ndarray, focal_35mm: float, quality: int = 95,
                     gps: tuple[float, float, float] | None = None) -> bytes:
    """``img`` as a JPEG whose EXIF says the focal length (35 mm equivalent), as a camera writes it, and, with
    ``gps`` = (latitude, longitude, height), the GPS position (see :func:`_gps_ifd`)."""
    from PIL import Image

    ok, data = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    exif = Image.Exif()
    exif[0x8769] = {0xA405: int(round(focal_35mm))}  # Exif IFD: FocalLengthIn35mmFilm
    if gps is not None:
        exif[0x8825] = _gps_ifd(*gps)  # GPS IFD
    payload = exif.tobytes()
    data = data.tobytes()
    return data[:2] + b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload + data[2:]


def _gps_ifd(latitude: float, longitude: float, height: float) -> dict:
    """The GPS IFD of a photo taken at ``latitude``, ``longitude`` (degrees) and ``height`` (metres): degrees,
    minutes and seconds, the seconds as a count of millionths over 1 000 000 (a millionth of a second of arc is
    0.03 mm on the ground), the height over 1 000 (half a millimetre at most) with the reference below sea level
    when it is negative."""
    from PIL.TiffImagePlugin import IFDRational

    def dms(degrees: float):
        total = round(abs(degrees) * 3600 * 10**6)
        d, rest = divmod(total, 3600 * 10**6)
        m, s = divmod(rest, 60 * 10**6)
        return IFDRational(d, 1), IFDRational(m, 1), IFDRational(s, 10**6)

    return {1: "N" if latitude >= 0 else "S", 2: dms(latitude), 3: "E" if longitude >= 0 else "W", 4: dms(longitude),
            5: 0 if height >= 0 else 1, 6: IFDRational(round(abs(height) * 1000), 1000)}


def write_pottery_dataset(out_dir: str | Path, scene: MeshScene | None = None, size=(1200, 900), focal: float = 1100.0,
                          k1: float = -0.03, exif_focal: bool | float = True) -> MeshScene:
    """Render turntable photos of a bowl to ``out_dir/photos`` and write ``out_dir/targets.csv``
    (code, X, Y, Z in metres, local frame: origin on the rotation axis, z up). The photos carry
    their focal length in the EXIF, as those of a camera do: the mat is flat and the alignment
    started from a guessed focal length sometimes stops at a handful of photos. ``exif_focal``:
    ``False`` writes no focal length (a scan or a phone app that drops the EXIF), a number writes
    that 35 mm equivalent instead of the true one (a wrong EXIF)."""
    out = Path(out_dir)
    (out / "photos").mkdir(parents=True, exist_ok=True)
    scene = scene or make_turntable_scene()
    K = np.array([[focal, 0, size[0] / 2], [0, focal, size[1] / 2], [0, 0, 1]])
    for i, pose in enumerate(turntable_poses()):
        img = render_mesh(scene, pose, K, size, k1)
        name = out / "photos" / f"VASO_{i:04d}.jpg"
        if exif_focal is False:
            cv2.imwrite(str(name), img, [cv2.IMWRITE_JPEG_QUALITY, 95])
        else:
            f35 = 36.0 * focal / max(size) if exif_focal is True else float(exif_focal)
            name.write_bytes(_jpeg_with_focal(img, f35))
        scene.cameras.append({"label": f"VASO_{i:04d}", "world_from_cam": pose.copy()})
    with open(out / "targets.csv", "w") as fh:
        fh.write("# label,X,Y,Z (LOCAL, metres)\n")
        for code, (x, y, z) in scene.targets.items():
            fh.write(f"{code},{x:.5f},{y:.5f},{z:.5f}\n")
    return scene


def make_classified_cloud(size=(6.0, 4.0), spacing: float = 0.02, origin=(2480000.0, 4500000.0, 100.0),
                          noise_fraction: float = 0.005, seed: int = 0):
    """Dense cloud of a small excavation with known classes, for the trainable classifier.

    Gently undulating ground (class 2), a masonry wall 3 x 0.4 m standing to 0.75 m (64),
    five bushes (3) and isolated noise points above and below the surfaces (7), sampled at
    random about ``spacing`` metres apart. Returns ``(xyz, rgb, codes)``, ``xyz`` in world
    coordinates (``origin`` + local metres, z up), points in random order.
    """
    rng = np.random.default_rng(seed)
    sx, sy = size
    density = 1.0 / spacing ** 2

    def ground_z(x, y):
        return 0.05 * np.sin(1.1 * x) + 0.04 * np.cos(0.9 * y)

    def count(area):
        return int(rng.poisson(area * density))

    parts = []  # (xyz, rgb, class)
    wx0, wx1, wy0, wy1, top = 1.5, 4.5, 1.8, 2.2, 0.75
    bushes = [((0.8, 0.8), 0.30), ((5.2, 3.2), 0.35), ((0.8, 3.2), 0.25), ((5.2, 0.8), 0.30), ((3.0, 0.7), 0.28)]
    # ground, without the wall footprint and the soil hidden under the bushes
    n = count(sx * sy)
    x, y = rng.uniform(0, sx, n), rng.uniform(0, sy, n)
    keep = ~((x > wx0) & (x < wx1) & (y > wy0) & (y < wy1))
    for (cx, cy), r in bushes:
        keep &= np.hypot(x - cx, y - cy) > 0.7 * r
    x, y = x[keep], y[keep]
    soil = np.array([125.0, 95.0, 65.0]) + 25 * np.sin(3 * x + 2 * y)[:, None] + rng.normal(0, 14, (len(x), 3))
    parts.append((np.column_stack([x, y, ground_z(x, y) + rng.normal(0, 0.002, len(x))]), soil, 2))
    # wall: two long faces, two short faces and the top, stones of slightly different colour
    tints = rng.uniform(-30, 30, (64, 3))
    faces = []
    for yf in (wy0, wy1):
        t = rng.uniform(wx0, wx1, count((wx1 - wx0) * top))
        faces.append((t, np.full_like(t, yf), t))
    for xf in (wx0, wx1):
        t = rng.uniform(wy0, wy1, count((wy1 - wy0) * top))
        faces.append((np.full_like(t, xf), t, t))
    for fx, fy, along in faces:
        z = rng.uniform(ground_z(fx, fy), top)
        stone = (np.floor(along / 0.25) * 31 + np.floor(z / 0.15) * 17).astype(int) % 64
        rgb = np.array([160.0, 150.0, 135.0]) + tints[stone] + rng.normal(0, 10, (len(z), 3))
        parts.append((np.column_stack([fx, fy, z]) + rng.normal(0, 0.003, (len(z), 3)), rgb, 64))
    n = count((wx1 - wx0) * (wy1 - wy0))
    fx, fy = rng.uniform(wx0, wx1, n), rng.uniform(wy0, wy1, n)
    stone = (np.floor(fx / 0.25) * 31 + np.floor(fy / 0.2) * 17).astype(int) % 64
    rgb = np.array([160.0, 150.0, 135.0]) + tints[stone] + rng.normal(0, 10, (n, 3))
    parts.append((np.column_stack([fx, fy, np.full(n, top)]) + rng.normal(0, 0.003, (n, 3)), rgb, 64))
    # bushes: thick, irregular shells of leaves resting on the ground
    for (cx, cy), r in bushes:
        n = count(4 * np.pi * r ** 2 * 1.3)
        d = rng.normal(size=(n, 3))
        d /= np.linalg.norm(d, axis=1, keepdims=True)
        p = d * (r * rng.uniform(0.75, 1.0, n))[:, None] * np.array([1.0, 1.0, 0.8])
        p += np.array([cx, cy, ground_z(cx, cy) + 0.8 * r])
        p = p[p[:, 2] > ground_z(p[:, 0], p[:, 1]) + 0.01]
        rgb = np.array([55.0, 125.0, 45.0]) + rng.uniform(-15, 15, 3) + rng.normal(0, 18, (len(p), 3))
        parts.append((p, rgb, 3))
    # isolated noise above and below the surfaces
    n = int(noise_fraction * sum(len(p[0]) for p in parts))
    x, y = rng.uniform(0, sx, n), rng.uniform(0, sy, n)
    below = rng.random(n) < 0.5
    z = np.where(below, ground_z(x, y) - rng.uniform(0.1, 0.4, n), rng.uniform(0.95, 1.5, n))
    parts.append((np.column_stack([x, y, z]), rng.uniform(0, 255, (n, 3)), 7))
    xyz = np.concatenate([p[0] for p in parts]) + np.asarray(origin, float)
    rgb = np.clip(np.concatenate([p[1] for p in parts]), 0, 255).astype(np.uint8)
    codes = np.concatenate([np.full(len(p[0]), p[2], np.uint8) for p in parts])
    order = rng.permutation(len(xyz))
    return xyz[order], rgb[order], codes[order]


# ---------------------------------------------------------------- a chamber for 360-degree photos
def make_chamber_scene(size=(4.0, 3.0, 2.4), texel: float = 0.004, seed: int = 5) -> MeshScene:
    """A closed rectangular chamber (a tomb, a cistern): floor, walls and ceiling, each with its
    own texture, and a block in the middle. Local frame: floor at z = 0, a corner at (0, 0)."""
    rng = np.random.default_rng(seed)
    sx, sy, sz = size
    v = np.array([[x, y, z] for z in (0, sz) for y in (0, sy) for x in (0, sx)], np.float32)
    quads = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    bx, by, bz = (1.6, 2.4), (1.2, 1.8), 0.7  # the block (a sarcophagus)
    v = np.vstack([v, np.array([[x, y, z] for z in (0, bz) for y in by for x in bx], np.float32)])
    quads += [(8 + a, 8 + b, 8 + c, 8 + d) for a, b, c, d in quads]
    faces = np.array([t for a, b, c, d in quads for t in ((a, b, c), (a, c, d))], np.uint32)
    planes = []  # (axis, position, the two axes of the texture, extents) of the textured planes
    for lo, hi in ((0.0, size), (np.array([bx[0], by[0], 0.0]), np.array([bx[1], by[1], bz]))):
        lo, hi = np.broadcast_to(lo, 3), np.asarray(hi, float)
        for axis in range(3):
            u, w = [a for a in range(3) if a != axis]
            for pos in (lo[axis], hi[axis]):
                ext = (hi[u] - lo[u], hi[w] - lo[w])
                tex = _noise_texture(max(2, int(ext[1] / texel)), max(2, int(ext[0] / texel)), rng)
                planes.append((axis, float(pos), u, w, lo, tex))

    def surface_color(p: np.ndarray) -> np.ndarray:
        out = np.zeros((len(p), 3), np.float32)
        best = np.full(len(p), np.inf)
        which = np.zeros(len(p), int)
        for i, (axis, pos, *_rest) in enumerate(planes):
            d = np.abs(p[:, axis] - pos)
            closer = d < best
            best[closer], which[closer] = d[closer], i
        for i, (_axis, _pos, u, w, lo, tex) in enumerate(planes):
            sel = which == i
            if sel.any():
                out[sel] = _sample(tex, (p[sel, u] - lo[u]) / texel - 0.5, (p[sel, w] - lo[w]) / texel - 0.5)
        return out

    return MeshScene(v, faces, 0, 0.0, 1.0, np.zeros((1, 1, 3), np.uint8), surface_color)


def _cast(scene: MeshScene, centre, dirs: np.ndarray, light=(0.3, -0.5, 0.8)) -> np.ndarray:
    """Colours (uint8) of the rays ``dirs`` (N, 3) from ``centre`` (no mat), Lambert shading."""
    import open3d as o3d

    if not hasattr(scene, "_rc"):
        rc = o3d.t.geometry.RaycastingScene()
        rc.add_triangles(o3d.core.Tensor(scene.vertices.astype(np.float32)), o3d.core.Tensor(scene.faces.astype(np.uint32)))
        scene._rc = rc
    origins = np.repeat(np.asarray(centre, float)[None], len(dirs), 0)
    ans = scene._rc.cast_rays(o3d.core.Tensor(np.hstack([origins, dirs]).astype(np.float32)))
    t = ans["t_hit"].numpy()
    hit = np.isfinite(t)
    img = np.full((len(dirs), 3), 120.0, np.float32)
    img[hit] = scene.surface_color(origins[hit] + dirs[hit] * t[hit, None])
    lv = np.asarray(light, float) / np.linalg.norm(light)
    shade = np.where(hit, 0.45 + 0.55 * np.abs(ans["primitive_normals"].numpy() @ lv), 1.0)
    return np.clip(img * shade[:, None], 0, 255).astype(np.uint8)


def render_equirectangular(scene: MeshScene, centre, width: int = 1600, yaw: float = 0.0) -> np.ndarray:
    """A 360-degree photo of a :class:`MeshScene` from ``centre``: equirectangular, ``width`` x
    ``width / 2``, level, its centre looking ``yaw`` degrees east of north (+y)."""
    h = width // 2
    lon = ((np.arange(width) + 0.5) / width - 0.5) * 2 * np.pi + np.radians(yaw)
    lat = (0.5 - (np.arange(h) + 0.5) / h) * np.pi
    lon, lat = np.meshgrid(lon, lat)
    dirs = np.dstack([np.cos(lat) * np.sin(lon), np.cos(lat) * np.cos(lon), np.sin(lat)]).reshape(-1, 3)
    return _cast(scene, centre, dirs).reshape(h, width, 3)


def render_fisheye(scene: MeshScene, world_from_cam: np.ndarray, focal: float, size=(800, 600),
                   k1: float = 0.0) -> np.ndarray:
    """A fisheye photo of a :class:`MeshScene` (COLMAP ``OPENCV_FISHEYE`` with ``fx = fy =
    focal``, principal point in the centre, ``k1`` only): the distance from the centre grows
    with the angle from the axis, ``r = focal * theta * (1 + k1 * theta^2)``."""
    w, h = size
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float64)
    x, y = (xx + 0.5 - w / 2) / focal, (yy + 0.5 - h / 2) / focal
    rd = np.hypot(x, y)
    theta = rd.copy()
    for _ in range(20):  # Newton on theta * (1 + k1 theta^2) = rd
        theta -= (theta * (1 + k1 * theta ** 2) - rd) / (1 + 3 * k1 * theta ** 2)
    with np.errstate(invalid="ignore", divide="ignore"):
        scale = np.where(rd > 0, np.sin(theta) / rd, 1.0)
    dirs_cam = np.dstack([x * scale, y * scale, np.cos(theta)]).reshape(-1, 3)
    return _cast(scene, world_from_cam[:3, 3], dirs_cam @ world_from_cam[:3, :3].T).reshape(h, w, 3)


# ---------------------------------------------------------------- a wall traced on the plan
WALL_TRACE = ((0.0, 0.0), (6.0, 0.0), (6.0, 4.0))  # the face of the wall on the plan: an L with its corner at 6 m
WALL_MARKS = ((1.0, 1.0), (3.0, 0.6), (5.85, 1.2), (6.15, 1.2), (8.0, 1.0))  # (face length, height): two by the corner
WALL_MAT = 30.0  # metres a side, the ground the wall stands on
WALL_MAT_TEXEL = 0.03
WALL_BODY = (125.0, 120.0, 115.0)  # BGR of what is not the face: the top, the back, the two ends
WALL_FAN_TURN = 15.0  # degrees: a corner that turns more is photographed by a fan of stations


def _towards_viewer(face: np.ndarray, viewer: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Of the face of a wall on the plan (N, 2), seen from the ``viewer`` side of its direction: the length and
    the direction of each segment, and its horizontal normal towards the viewer."""
    if viewer not in ("right", "left"):
        raise ValueError(f"the viewer is on the 'right' or on the 'left' of the trace, not {viewer!r}")
    step = np.diff(face, axis=0)
    length = np.hypot(step[:, 0], step[:, 1])
    t = step / length[:, None]
    side = 1.0 if viewer == "right" else -1.0
    return length, t, side * np.column_stack([t[:, 1], -t[:, 0]])


def _on_polyline(line: np.ndarray, xy: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Of plan points (M, 2): the length along the polyline ``line`` (N, 2) of the nearest point of it, and the
    distance from it."""
    step = np.diff(line, axis=0)
    length = np.hypot(step[:, 0], step[:, 1])
    start = np.concatenate([[0.0], np.cumsum(length)])
    best, along = np.full(len(xy), np.inf), np.zeros(len(xy))
    for i in range(len(step)):
        q = xy - line[i]
        tau = np.clip(q @ step[i] / length[i], 0.0, length[i])
        dist = np.hypot(*(q - tau[:, None] * step[i] / length[i]).T)
        closer = dist < best
        best[closer], along[closer] = dist[closer], start[i] + tau[closer]
    return along, best


def _at_length(line: np.ndarray, length: float) -> np.ndarray:
    """The point of the polyline ``line`` at ``length`` along it (within its two ends)."""
    step = np.diff(line, axis=0)
    seg = np.hypot(step[:, 0], step[:, 1])
    start = np.concatenate([[0.0], np.cumsum(seg)])
    i = int(np.clip(np.searchsorted(start, length, side="right") - 1, 0, len(seg) - 1))
    return line[i] + np.clip(length - start[i], 0.0, seg[i]) * step[i] / seg[i]


def make_wall_scene(trace=WALL_TRACE, viewer: str = "right", height: float = 2.0, thickness: float = 0.4,
                    marks=WALL_MARKS, mark_radius: float = 0.04, face_step: float = 0.2, texel: float = 0.004,
                    seed: int = 11) -> MeshScene:
    """A wall whose **face** follows ``trace`` (plan points, local metres) with sharp corners, from z = 0 to
    ``height``, seen from the ``viewer`` side (``"right"`` or ``"left"`` of the direction of the trace); its body
    is ``thickness`` behind the face, on the side away from the viewer, and it stands on a ground mat of
    :data:`WALL_MAT` metres. The face is textured by its own coordinates - the length along the trace and the
    height - with noise between 150 and 230 and a black disc (value 10, ``mark_radius``) at each of ``marks``
    (``(face length, height)``; those that do not fall on the face are left out); the rest of the body is plain.
    The face is meshed in columns ``face_step`` wide along each segment, so that a piece of it can be left out
    of a model.

    Beside the fields of a :class:`MeshScene`: ``face`` (N, 2), ``back`` (N, 2: the back of the body, mitred),
    ``viewer``, ``height``, ``thickness``, ``marks`` (``[(face length, height, (x, y, z))]``), ``targets``
    (``{i: (x, y, z)}``, the same points) and ``face_columns``, for every triangle of the scene the face length of
    the middle of the column it belongs to (NaN for the triangles that are not of the face)."""
    face = np.array(trace, np.float64).reshape(-1, 2)
    seg, t, towards = _towards_viewer(face, viewer)
    rng = np.random.default_rng(seed)
    start = np.concatenate([[0.0], np.cumsum(seg)])
    # the back: the face moved away from the viewer, the corners mitred
    away = -towards
    back = np.empty_like(face)
    back[0], back[-1] = face[0] + thickness * away[0], face[-1] + thickness * away[-1]
    for i in range(1, len(face) - 1):
        back[i] = face[i] + thickness * (away[i - 1] + away[i]) / (1.0 + away[i - 1] @ away[i])
    half = WALL_MAT / 2.0
    vertices = [[-half, -half, 0.0], [half, -half, 0.0], [half, half, 0.0], [-half, half, 0.0]]
    faces = [[0, 1, 2], [0, 2, 3]]
    columns = [np.nan, np.nan]

    def quad(a, b, c, d, column=np.nan):
        k = len(vertices)
        vertices.extend([list(a), list(b), list(c), list(d)])
        faces.extend([[k, k + 1, k + 2], [k, k + 2, k + 3]])
        columns.extend([column, column])

    def wall(p, q, column=np.nan):  # a vertical rectangle on the plan segment p-q
        quad([*p, 0.0], [*q, 0.0], [*q, height], [*p, height], column)

    for i in range(len(seg)):
        n = max(1, int(np.ceil(seg[i] / face_step - 1e-9)))
        edges = np.minimum(np.arange(n + 1) * face_step, seg[i])
        for a, b in zip(edges[:-1], edges[1:]):
            wall(face[i] + a * t[i], face[i] + b * t[i], start[i] + (a + b) / 2.0)
        wall(back[i], back[i + 1])
        quad([*face[i], height], [*face[i + 1], height], [*back[i + 1], height], [*back[i], height])  # the top
    wall(face[0], back[0])
    wall(face[-1], back[-1])
    # the texture of the face: columns along the face length, rows down from the top
    th, tw = max(2, int(round(height / texel))), max(2, int(round(start[-1] / texel)))
    noise = _noise_texture(th, tw, rng).astype(np.float32)
    tex = 150.0 + 80.0 * (noise - noise.min()) / max(float(noise.max() - noise.min()), 1.0)
    placed = []
    for length, z in marks:
        if not (0.0 <= length <= start[-1] and 0.0 <= z <= height):  # a mark past the end of a shorter wall
            continue
        cx, cy, r = length / texel - 0.5, (height - z) / texel - 0.5, mark_radius / texel
        r0, r1 = max(0, int(np.floor(cy - r - 1))), min(th, int(np.ceil(cy + r + 2)))
        c0, c1 = max(0, int(np.floor(cx - r - 1))), min(tw, int(np.ceil(cx + r + 2)))
        yy, xx = np.mgrid[r0:r1, c0:c1]
        cover = np.clip(r - np.hypot(xx - cx, yy - cy) + 0.5, 0.0, 1.0)[..., None]  # the disc where it is, smooth
        tex[r0:r1, c0:c1] = tex[r0:r1, c0:c1] * (1.0 - cover) + 10.0 * cover
        x, y = _at_length(face, length)
        placed.append((float(length), float(z), (float(x), float(y), float(z))))
    mat_n = int(round(WALL_MAT / WALL_MAT_TEXEL))
    mat = _noise_texture(mat_n, mat_n, rng)

    def surface_color(p: np.ndarray) -> np.ndarray:
        p = np.asarray(p, np.float64).reshape(-1, 3)
        out = np.tile(np.asarray(WALL_BODY, np.float32), (len(p), 1))
        along, dist = _on_polyline(face, p[:, :2])
        on = (dist < 1e-3) & (p[:, 2] < height - 1e-4)
        if on.any():
            out[on] = _sample(tex, along[on] / texel - 0.5, (height - p[on, 2]) / texel - 0.5)
        return out

    faces = orient_outward(np.array(vertices, np.float64), np.array(faces, np.int64))
    scene = MeshScene(np.array(vertices, np.float64), np.array(faces, np.uint32), 2, WALL_MAT, WALL_MAT_TEXEL, mat,
                      surface_color, targets={i: m[2] for i, m in enumerate(placed)})
    scene.face, scene.back, scene.viewer = face, back, viewer
    scene.height, scene.thickness, scene.marks = float(height), float(thickness), placed
    scene.face_columns = np.array(columns, np.float64)
    return scene


def orient_outward(vertices: np.ndarray, faces: np.ndarray, gap: float = 0.002, seed: int = 0) -> np.ndarray:
    """``faces`` (M, 3) turned so that each triangle's normal (by its winding, counter-clockwise seen from outside)
    points to the air, as the normals of a model built from photos do. A point is inside what the scene bounds when
    a ray straight up from it crosses the surfaces an odd number of times: the ground, a block, the body of a wall
    under its crest. Of each triangle, the points ``gap`` on either side of five points of it are looked at, and the
    most of them decide (a ray up along an edge of another triangle counts it twice); where both sides are inside
    or both outside (a face alone, without a crest), its winding is kept."""
    import open3d as o3d

    v, f = np.asarray(vertices, np.float64), np.asarray(faces, np.int64).copy()
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(o3d.core.Tensor(v.astype(np.float32)), o3d.core.Tensor(f.astype(np.uint32)))
    a, b, c = (v[f[:, k]] for k in range(3))
    normal = np.cross(b - a, c - a)
    normal /= np.maximum(np.linalg.norm(normal, axis=1, keepdims=True), 1e-12)
    rng = np.random.default_rng(seed)

    def inside(p: np.ndarray) -> np.ndarray:
        rays = np.hstack([p, np.tile([0.0, 0.0, 1.0], (len(p), 1))]).astype(np.float32)
        return scene.count_intersections(o3d.core.Tensor(rays)).numpy() % 2 == 1

    votes = np.zeros(len(f), np.intp)
    for _ in range(5):
        w = rng.uniform(0.1, 0.45, (len(f), 2))
        at = a + w[:, :1] * (b - a) + w[:, 1:] * (c - a)
        votes += (inside(at + gap * normal) & ~inside(at - gap * normal)).astype(np.intp)
        votes -= (inside(at - gap * normal) & ~inside(at + gap * normal)).astype(np.intp)
    flip = votes > 0
    f[flip] = f[flip][:, ::-1]
    return f


def wall_poses(scene: MeshScene, distance: float = 2.5, spacing: float = 0.6, heights=(0.5, 1.5)) -> list[np.ndarray]:
    """Where the wall of :func:`make_wall_scene` is photographed from (``world_from_cam``, the camera level): at
    each of ``heights``, a station every ``spacing`` metres of the face, ``distance`` in front of it on the
    viewer's side and facing it (a station on a corner faces the segment that ends there); at each corner that
    turns more than :data:`WALL_FAN_TURN` degrees a fan of three stations ``distance`` from the corner, looking
    at it, a quarter, a half and three quarters of the way round from one face to the next. A station nearer
    than ``0.6 distance`` to the face is left out: seen from outside, a corner keeps its fan; seen from inside,
    it keeps the station in the middle of the room and loses those that would stand by the other wall. The
    stations follow the face, the fan of a corner in its place among them."""
    face = scene.face
    seg, t, towards = _towards_viewer(face, scene.viewer)
    start = np.concatenate([[0.0], np.cumsum(seg)])
    stations = []  # (face length, place in the fan, where the camera stands, what it looks at)
    for k in range(int(np.floor(start[-1] / spacing + 1e-9)) + 1):
        length = min(k * spacing, start[-1])
        i = int(np.clip(np.searchsorted(start, length - 1e-9, side="right") - 1, 0, len(seg) - 1))
        p = face[i] + (length - start[i]) * t[i]
        stations.append((length, 0, p + distance * towards[i], p))
    for i in range(len(seg) - 1):
        a, b = towards[i], towards[i + 1]
        turn = np.arctan2(a[0] * b[1] - a[1] * b[0], a @ b)
        if abs(np.degrees(turn)) > WALL_FAN_TURN:
            for place, share in enumerate((0.25, 0.5, 0.75)):
                c, s = np.cos(share * turn), np.sin(share * turn)
                eye = face[i + 1] + distance * np.array([a[0] * c - a[1] * s, a[0] * s + a[1] * c])
                stations.append((start[i + 1], 1 + place, eye, face[i + 1]))
    stations.sort(key=lambda st: st[:2])
    near = _on_polyline(face, np.array([st[2] for st in stations]))[1]
    kept = [st for st, d in zip(stations, near) if d >= 0.6 * distance - 1e-9]
    return [look_at(np.array([*eye, z]), np.array([*target, z]), up=(0, 0, 1)) for z in heights
            for _length, _place, eye, target in kept]


def write_wall_dataset(out_dir: str | Path, scene: MeshScene | None = None, size=(800, 600), focal: float = 700.0,
                       **poses) -> MeshScene:
    """The photos of a wall (:func:`make_wall_scene`, the default one without ``scene``) from the poses of
    :func:`wall_poses` (``poses``: its arguments) in ``out_dir/photos`` (``WALL_000.jpg``..., no distortion, the
    focal length in their EXIF), the mesh of the scene as ``model.ply`` (local metres) and ``poses.json``:
    ``size``, ``focal`` (pixels), ``poses`` (``world_from_cam``, one per photo in the order of their names),
    ``face``, ``viewer``, ``height``, ``thickness`` and ``marks`` (``[face length, height, [x, y, z]]``)."""
    import json

    from ..io.mesh_ply import MeshData, write_mesh

    out = Path(out_dir)
    (out / "photos").mkdir(parents=True, exist_ok=True)
    scene = scene or make_wall_scene()
    K = np.array([[focal, 0, size[0] / 2], [0, focal, size[1] / 2], [0, 0, 1]])
    cams = wall_poses(scene, **poses)
    for i, pose in enumerate(cams):
        img = render_mesh(scene, pose, K, size, k1=0.0)
        (out / "photos" / f"WALL_{i:03d}.jpg").write_bytes(_jpeg_with_focal(img, 36.0 * focal / max(size)))
        scene.cameras.append({"label": f"WALL_{i:03d}", "world_from_cam": pose.copy()})
    v = np.empty(len(scene.vertices), [("x", "<f4"), ("y", "<f4"), ("z", "<f4")])
    v["x"], v["y"], v["z"] = np.asarray(scene.vertices, np.float64).T
    write_mesh(out / "model.ply", MeshData(v, np.asarray(scene.faces, np.int64)))
    (out / "poses.json").write_text(json.dumps({
        "size": [int(size[0]), int(size[1])], "focal": float(focal), "poses": [p.tolist() for p in cams],
        "face": scene.face.tolist(), "viewer": scene.viewer, "height": scene.height, "thickness": scene.thickness,
        "marks": [[length, z, list(xyz)] for length, z, xyz in scene.marks]}, indent=1))
    return scene


def wall_plan(scene: MeshScene, cell: float = 0.02) -> tuple[np.ndarray, np.ndarray, tuple[float, float]]:
    """The plan of a wall scene (:func:`make_wall_scene`) over its whole ground mat: ``(dem, rgb, top_left)`` -
    ``dem`` (h, w) float32, the height of the wall on its footprint and 0 on the ground; ``rgb`` (3, h, w) uint8,
    the ground with the plain top of the wall on it; ``top_left`` the (x, y) of the corner of the first cell, row
    0 at the largest y. Local metres, cells of ``cell``."""
    from shapely import contains_xy
    from shapely.geometry import Polygon

    n = int(round(scene.mat_size / cell))
    half = scene.mat_size / 2.0
    rgb = cv2.resize(scene.mat_texture, (n, n), interpolation=cv2.INTER_LINEAR)[..., ::-1].copy()
    dem = np.zeros((n, n), np.float32)
    footprint = Polygon(np.vstack([scene.face, scene.back[::-1]]))
    x0, y0, x1, y1 = footprint.bounds
    c0, c1 = max(0, int((x0 + half) / cell) - 1), min(n, int((x1 + half) / cell) + 2)
    r0, r1 = max(0, int((half - y1) / cell) - 1), min(n, int((half - y0) / cell) + 2)
    rows, cols = np.mgrid[r0:r1, c0:c1]
    inside = contains_xy(footprint, -half + (cols + 0.5) * cell, half - (rows + 0.5) * cell)
    dem[r0:r1, c0:c1][inside] = scene.height
    rgb[r0:r1, c0:c1][inside] = np.asarray(WALL_BODY[::-1], np.uint8)
    return dem, np.ascontiguousarray(np.moveaxis(rgb, -1, 0)), (-half, half)
