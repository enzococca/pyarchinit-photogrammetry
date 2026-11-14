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
    stakes: dict[str, tuple[float, float, float]] = field(default_factory=dict)  # name -> world xyz of the head

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
    with open(out / "gcp.csv", "w") as fh:
        fh.write("# label,X,Y,Z (" + scene.crs + ")\n")
        for code, (x, y, z) in scene.targets.items():
            fh.write(f"{code},{x:.4f},{y:.4f},{z:.4f}\n")
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


def _jpeg_with_focal(img: np.ndarray, focal_35mm: float, quality: int = 95) -> bytes:
    """``img`` as a JPEG whose EXIF says the focal length (35 mm equivalent), as a camera writes it."""
    from PIL import Image

    ok, data = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    exif = Image.Exif()
    exif[0x8769] = {0xA405: int(round(focal_35mm))}  # Exif IFD: FocalLengthIn35mmFilm
    payload = exif.tobytes()
    data = data.tobytes()
    return data[:2] + b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload + data[2:]


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
