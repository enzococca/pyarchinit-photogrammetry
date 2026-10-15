"""Synthetic excavation trench for tests and demos.

A textured height field (a small trench with a wall stub) with coded targets
at known coordinates is rendered from a set of cameras by ray casting. The
ground truth (surface, targets in a projected CRS) is returned so that the
whole pipeline can be checked metrically.
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

    def height_at(self, x_local: np.ndarray, y_local: np.ndarray) -> np.ndarray:
        h, w = self.heights.shape
        col = np.clip(x_local / self.texel, 0, w - 1.001)
        row = np.clip((self.size[1] - y_local) / self.texel, 0, h - 1.001)
        return cv2.remap(self.heights.astype(np.float32), col.astype(np.float32).reshape(-1, 1),
                         row.astype(np.float32).reshape(-1, 1), cv2.INTER_LINEAR).ravel()

    def world_height(self, X: np.ndarray, Y: np.ndarray) -> np.ndarray:
        return self.height_at(np.asarray(X) - self.origin[0], np.asarray(Y) - self.origin[1]) + self.origin[2]


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
        pat = target_image("aruco_4x4_50", code, tpx)
        quiet = int(tpx * 0.25)
        tile = np.full((tpx + 2 * quiet, tpx + 2 * quiet), 255, np.uint8)
        tile[quiet:quiet + tpx, quiet:quiet + tpx] = pat
        c, r = int(tx / texel), int((size[1] - ty) / texel)
        r0, c0 = r - tile.shape[0] // 2, c - tile.shape[1] // 2
        tex[r0:r0 + tile.shape[0], c0:c0 + tile.shape[1]] = tile[..., None]
        # targets lie on flat plates: flatten the surface under them
        zc = float(z[r, c])
        z[r0:r0 + tile.shape[0], c0:c0 + tile.shape[1]] = zc
        cx = (c0 + tile.shape[1] / 2) * texel
        cy = size[1] - (r0 + tile.shape[0] / 2) * texel
        targets[code] = (origin[0] + cx, origin[1] + cy, origin[2] + zc)
    return Scene(origin=origin, crs=crs, size=size, texel=texel, texture=tex, heights=z.astype(np.float32),
                 targets=targets)


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


def render(scene: Scene, world_from_cam: np.ndarray, K: np.ndarray, size=(1200, 900), k1: float = -0.04) -> np.ndarray:
    """Ray-cast the textured height field (local frame). Applies radial distortion ``k1``."""
    import open3d as o3d

    if not hasattr(scene, "_rc"):
        h, w = scene.heights.shape
        step = 2
        hs = scene.heights[::step, ::step]
        gh, gw = hs.shape
        yy, xx = np.mgrid[0:gh, 0:gw]
        X = (xx * step + 0.5) * scene.texel
        Y = scene.size[1] - (yy * step + 0.5) * scene.texel
        verts = np.column_stack([X.ravel(), Y.ravel(), hs.ravel()]).astype(np.float32)
        idx = np.arange(gh * gw).reshape(gh, gw)
        a, b, c, d = idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel(), idx[1:, :-1].ravel(), idx[1:, 1:].ravel()
        faces = np.vstack([np.column_stack([a, c, b]), np.column_stack([b, c, d])]).astype(np.uint32)
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
    with open(out / "gcp.csv", "w") as fh:
        fh.write("# label,X,Y,Z (" + scene.crs + ")\n")
        for code, (x, y, z) in scene.targets.items():
            fh.write(f"{code},{x:.4f},{y:.4f},{z:.4f}\n")
    return scene
