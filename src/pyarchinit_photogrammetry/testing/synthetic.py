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
        return _sample(self.heights.astype(np.float32), np.ravel(col), np.ravel(row)).ravel()

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
    with open(out / "gcp.csv", "w") as fh:
        fh.write("# label,X,Y,Z (" + scene.crs + ")\n")
        for code, (x, y, z) in scene.targets.items():
            fh.write(f"{code},{x:.4f},{y:.4f},{z:.4f}\n")
    return scene


def make_vessel(rim_r: float = 0.08, height: float = 0.12, thickness: float = 0.006, sectors: int = 128,
                sherd: tuple[float, float] | None = None, noise: float = 0.0, seed: int = 3):
    """A wheel-made bowl as a closed :class:`trimesh.Trimesh` (metres, axis = +Z).

    ``sherd=(a0, a1)`` keeps only the angular sector between ``a0`` and ``a1``
    degrees (a fragment); ``noise`` adds Gaussian roughness (metres).
    """
    import trimesh

    hs = np.linspace(0, height, 60)
    r_out = 0.04 + (rim_r - 0.04) * np.sin(hs / height * np.pi / 2) ** 0.7 + 0.01 * np.sin(hs / height * np.pi)
    prof = np.vstack([[0, 0], np.column_stack([r_out, hs]), np.column_stack([(r_out - thickness)[::-1], hs[::-1]]),
                      [0, thickness]])
    if sherd is None:
        a = np.linspace(0, 2 * np.pi, sectors, endpoint=False)
        closed = True
    else:
        a = np.radians(np.linspace(sherd[0], sherd[1], max(8, int(sectors * abs(sherd[1] - sherd[0]) / 360))))
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
        if not closed:
            continue
    if not closed:  # cap the two fracture faces
        for i, flip in ((0, False), (len(a) - 1, True)):
            for k in range(m // 2 - 1):
                p, q, r, s_ = i * m + k, i * m + k + 1, i * m + m - 1 - k, i * m + m - 2 - k
                t1, t2 = [p, q, r], [q, s_, r]
                f += [t1[::-1], t2[::-1]] if not flip else [t1, t2]
    if noise > 0:
        v = v + np.random.default_rng(seed).normal(0, noise, v.shape)
    return trimesh.Trimesh(v, np.array(f), process=True)


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


def write_pottery_dataset(out_dir: str | Path, scene: MeshScene | None = None, size=(1200, 900), focal: float = 1100.0,
                          k1: float = -0.03) -> MeshScene:
    """Render turntable photos of a bowl to ``out_dir/photos`` and write ``out_dir/targets.csv``
    (code, X, Y, Z in metres, local frame: origin on the rotation axis, z up)."""
    out = Path(out_dir)
    (out / "photos").mkdir(parents=True, exist_ok=True)
    scene = scene or make_turntable_scene()
    K = np.array([[focal, 0, size[0] / 2], [0, focal, size[1] / 2], [0, 0, 1]])
    for i, pose in enumerate(turntable_poses()):
        img = render_mesh(scene, pose, K, size, k1)
        cv2.imwrite(str(out / "photos" / f"VASO_{i:04d}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 95])
        scene.cameras.append({"label": f"VASO_{i:04d}", "world_from_cam": pose.copy()})
    with open(out / "targets.csv", "w") as fh:
        fh.write("# label,X,Y,Z (LOCAL, metres)\n")
        for code, (x, y, z) in scene.targets.items():
            fh.write(f"{code},{x:.5f},{y:.5f},{z:.5f}\n")
    return scene
