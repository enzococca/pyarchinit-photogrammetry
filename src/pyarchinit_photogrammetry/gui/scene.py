"""Display data for the 3D view, in world coordinates shifted by an offset
(projected coordinates are millions of metres: float32 graphics need a local origin)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


@dataclass
class SceneData:
    offset: np.ndarray
    points: np.ndarray | None = None  # (N,3) float32
    colors: np.ndarray | None = None  # (N,3) uint8 RGB
    mesh_vertices: np.ndarray | None = None  # (V,3)
    mesh_faces: np.ndarray | None = None  # (F,3)
    mesh_colors: np.ndarray | None = None  # (V,3) uint8 (per vertex) when not textured
    mesh_uv: np.ndarray | None = None  # (V,2) when textured (vertices unrolled per corner)
    texture: Path | None = None
    cameras: list[np.ndarray] = field(default_factory=list)  # 5x3: centre + 4 frustum corners
    camera_labels: list[str] = field(default_factory=list)
    markers: list[tuple[str, np.ndarray, bool]] = field(default_factory=list)  # label, xyz, is_check
    up: np.ndarray = field(default_factory=lambda: np.array([0.0, 0.0, 1.0]))
    kind: str = "empty"
    point_codes: np.ndarray | None = None  # class code of each displayed point
    class_counts: dict = field(default_factory=dict)  # class code -> points in the whole cloud

    @property
    def extent(self) -> float:
        arrs = [a for a in (self.points, self.mesh_vertices) if a is not None and len(a)]
        if not arrs and self.cameras:
            arrs.append(np.vstack(self.cameras))
        if not arrs:
            return 1.0
        allp = np.vstack(arrs)
        return float(np.linalg.norm(np.ptp(allp, axis=0))) or 1.0

    @property
    def center(self) -> np.ndarray:
        for a in (self.mesh_vertices, self.points):
            if a is not None and len(a):
                return np.median(a, axis=0)
        if self.cameras:
            return np.vstack(self.cameras).mean(0)
        return np.zeros(3)


def available_views(chunk) -> list[str]:
    out = []
    if chunk is None:
        return out
    if chunk.product_path("tie_points"):
        out.append("tie_points")
    if chunk.product_path("point_cloud"):
        out.append("point_cloud")
    if chunk.product_path("model"):
        out.append("model")
    if chunk.product_path("model_textured"):
        out.append("model_textured")
    if chunk.meta.get("proxies"):
        out.append("proxies")
    return out


# colours of the point classes: ASPRS (as in most LAS viewers) and the archaeological 64-71
CLASS_COLORS = {0: (170, 170, 170), 1: (200, 200, 200), 2: (166, 118, 60), 3: (170, 230, 110), 4: (80, 190, 70),
                5: (20, 120, 40), 6: (220, 60, 50), 7: (255, 0, 255), 9: (40, 120, 255), 17: (120, 120, 160),
                18: (255, 120, 255), 64: (230, 140, 40), 65: (190, 160, 110), 66: (120, 40, 160),
                67: (240, 220, 80), 68: (0, 220, 220), 69: (255, 90, 140), 70: (110, 110, 110), 71: (255, 255, 255)}


def class_palette() -> np.ndarray:
    """(256, 3) uint8: a colour for every LAS class code (fixed ones above, the others
    spread around the colour wheel)."""
    import colorsys

    pal = np.zeros((256, 3), np.uint8)
    for code in range(256):
        h = (code * 0.618034) % 1.0
        pal[code] = np.array(colorsys.hsv_to_rgb(h, 0.65, 0.95)) * 255
    for code, rgb in CLASS_COLORS.items():
        pal[code] = rgb
    return pal


def class_name(code: int) -> str:
    from ..core.pointcloud import ARCHAEO_CLASSES, ASPRS_CLASSES

    names = {0: "Mai classificati", 1: "Non classificati", 2: "Terreno", 3: "Vegetazione bassa",
             4: "Vegetazione media", 5: "Vegetazione alta", 6: "Edificio", 7: "Rumore (basso)", 9: "Acqua",
             17: "Ponte", 18: "Rumore (alto)"}
    if code in names:
        return names[code]
    if code in ARCHAEO_CLASSES:
        return ARCHAEO_CLASSES[code].split(" / ")[0]
    return ASPRS_CLASSES.get(code, f"Classe {code}")


def elevation_colors(z: np.ndarray) -> np.ndarray:
    """Hypsometric colours (blue-green-yellow-brown-white) for heights, 2-98 percentile stretch."""
    lo, hi = np.percentile(z, [2, 98]) if len(z) else (0.0, 1.0)
    t = np.clip((z - lo) / max(hi - lo, 1e-9), 0, 1)
    stops = np.array([[30, 90, 200], [40, 170, 120], [230, 220, 90], [150, 100, 50], [250, 250, 250]], float)
    pos = t * (len(stops) - 1)
    i = np.clip(pos.astype(int), 0, len(stops) - 2)
    f = (pos - i)[:, None]
    return (stops[i] * (1 - f) + stops[i + 1] * f).astype(np.uint8)


PROXY_COLORS = np.array([[230, 25, 75], [60, 180, 75], [255, 225, 25], [67, 99, 216], [245, 130, 49], [145, 30, 180],
                         [70, 240, 240], [240, 50, 230], [188, 246, 12], [0, 128, 128], [154, 99, 36], [128, 0, 0]],
                        np.uint8)


def best_view(chunk) -> str:
    v = available_views(chunk)
    for k in ("model_textured", "model", "point_cloud", "tie_points"):
        if k in v:
            return k
    return "cameras"


def _offset(chunk) -> np.ndarray:
    cams = chunk.aligned_cameras
    if cams:
        c = chunk.to_world(np.array([cam.center for cam in cams]))
        return np.round(np.median(c, axis=0), 0)
    return np.zeros(3)


def build_scene(chunk, kind: str | None = None, max_points: int = 400_000, max_faces: int = 600_000,
                frustum_size: float | None = None, color_mode: str = "rgb", hidden_classes=()) -> SceneData:
    """Collect what the 3D view shows for ``kind`` (tie_points, point_cloud, model,
    model_textured, cameras). Point clouds are coloured by ``color_mode`` ("rgb", "class",
    "elevation"); points of ``hidden_classes`` are left out."""
    if chunk is None:
        return SceneData(np.zeros(3))
    kind = kind or best_view(chunk)
    off = _offset(chunk)
    sd = SceneData(off, kind=kind)
    T = chunk.transform
    from ..core import crs as crs_mod

    levelled = (not crs_mod.is_local(chunk.crs)) or chunk.meta.get("levelled") or not np.allclose(T[:3, :3] / chunk.scale,
                                                                                                    np.eye(3))
    sd.up = np.array([0.0, 0.0, 1.0]) if levelled else _up_internal(chunk, T)

    def W(p):
        return (np.asarray(p, float) @ T[:3, :3].T + T[:3, 3] - off).astype(np.float32)

    if kind in ("tie_points", "point_cloud"):
        pc = chunk.tie_points if kind == "tie_points" else chunk.point_cloud
        if pc is not None and len(pc):
            codes = getattr(pc, "classification", None)
            codes = np.asarray(codes, np.uint8) if codes is not None and len(codes) == len(pc) else np.zeros(len(pc),
                                                                                                          np.uint8)
            found, counts = np.unique(codes, return_counts=True)
            sd.class_counts = {int(c): int(n) for c, n in zip(found, counts)}
            idx = np.arange(len(pc))
            if hidden_classes:
                idx = idx[~np.isin(codes, np.asarray(list(hidden_classes), np.uint8))]
            if len(idx) > max_points:
                idx = np.sort(np.random.default_rng(0).choice(idx, max_points, replace=False))
            sd.points = W(pc.xyz[idx])
            sd.point_codes = codes[idx]
            rgb = getattr(pc, "rgb", None)
            if color_mode == "class":
                sd.colors = class_palette()[sd.point_codes]
            elif color_mode == "elevation":
                sd.colors = elevation_colors(sd.points.astype(float) @ sd.up if len(idx) else np.zeros(0))
            else:
                sd.colors = (np.asarray(rgb)[idx].astype(np.uint8) if rgb is not None and len(rgb)
                             else np.full((len(idx), 3), 200, np.uint8))
    elif kind == "model_textured" and chunk.product_path("model_textured"):
        from ..io.exports import _read_textured_obj

        path = chunk.product_path("model_textured")
        verts, faces, uv = _read_textured_obj(path)
        if len(faces) > max_faces:
            keep = np.random.default_rng(0).choice(len(faces), max_faces, replace=False)
            faces = faces[keep]
            uv = None if uv is None else uv[keep]
        if uv is not None:
            sd.mesh_vertices = W(verts[faces].reshape(-1, 3))
            sd.mesh_faces = np.arange(len(faces) * 3).reshape(-1, 3)
            sd.mesh_uv = uv.reshape(-1, 2).astype(np.float32)
            tex = path.parent / "texture.jpg"
            sd.texture = tex if tex.exists() else None
        else:
            sd.mesh_vertices, sd.mesh_faces = W(verts), faces
    elif kind == "proxies":
        import trimesh

        V, F, C, n = [], [], [], 0
        for i, entry in enumerate(chunk.meta.get("proxies", [])):
            path = chunk.data_dir / entry["file"]
            if not path.exists():
                continue
            m = trimesh.load(str(path), process=False, force="mesh")
            V.append((np.asarray(m.vertices, float) - off).astype(np.float32))
            F.append(np.asarray(m.faces) + n)
            C.append(np.repeat(PROXY_COLORS[i % len(PROXY_COLORS)][None], len(m.vertices), 0))
            n += len(m.vertices)
        if V:
            sd.mesh_vertices, sd.mesh_faces, sd.mesh_colors = np.vstack(V), np.vstack(F), np.vstack(C)
    elif kind in ("model", "model_textured"):
        mesh = display_model(chunk, max_faces)
        if mesh is not None:
            faces = np.asarray(mesh.faces)
            if len(faces) > max_faces:
                faces = faces[np.random.default_rng(0).choice(len(faces), max_faces, replace=False)]
            sd.mesh_vertices, sd.mesh_faces = W(mesh.vertices), faces
            vc = getattr(mesh.visual, "vertex_colors", None)
            if vc is not None and len(vc) == len(mesh.vertices) and np.ptp(vc[:, :3]) > 0:
                sd.mesh_colors = np.asarray(vc)[:, :3].astype(np.uint8)
    # cameras as small frusta
    cams = chunk.aligned_cameras
    if cams:
        centers = W(np.array([c.center for c in cams]))
        ext = float(np.linalg.norm(np.ptp(centers, axis=0))) if len(cams) > 1 else 1.0
        size = frustum_size or max(ext * 0.03, 1e-3)
        for cam in cams:
            s = chunk.sensor(cam)
            w2, h2 = s.width / 2, s.height / 2
            f = s.focal_px
            corners = np.array([[-w2, -h2, f], [w2, -h2, f], [w2, h2, f], [-w2, h2, f]]) / f * size / chunk.scale
            pts = np.vstack([cam.center, corners @ cam.transform[:3, :3].T + cam.transform[:3, 3]])
            sd.cameras.append(W(pts))
            sd.camera_labels.append(cam.label)
    for m in chunk.markers:
        if m.position is not None:
            sd.markers.append((m.label, W(m.position[None])[0], bool(m.checkpoint)))
    return sd


def display_model(chunk, max_faces: int = 600_000):
    """The model for the 3D view: a cached coarse copy (``model_preview.ply``, vertex
    clustering) when the model has more than ``max_faces`` faces, made once and refreshed
    when the model changes; the model itself otherwise."""
    import logging

    import trimesh

    path = chunk.product_path("model")
    if not path or not path.exists():
        return None
    preview = chunk.data_dir / "model_preview.ply"
    if preview.exists() and preview.stat().st_mtime >= path.stat().st_mtime:
        return trimesh.load(str(preview), process=False, force="mesh")
    from ..io.mesh_ply import face_count, make_preview

    n = face_count(path)
    if n is not None and n > max_faces:
        try:
            return trimesh.load(str(make_preview(path, preview, max_faces)), process=False, force="mesh")
        except (ValueError, MemoryError, OSError) as exc:
            logging.getLogger(__name__).warning("Anteprima del modello non creata: %s", exc)
    return chunk.model


def _up_internal(chunk, T) -> np.ndarray:
    """In a local, unlevelled chunk the cameras' mean 'up' is the best guess."""
    cams = chunk.aligned_cameras
    if not cams:
        return np.array([0.0, 0.0, 1.0])
    up = -np.mean([c.transform[:3, 1] for c in cams], axis=0)  # OpenCV +Y points down
    up = T[:3, :3] @ up
    n = np.linalg.norm(up)
    return up / n if n > 0 else np.array([0.0, 0.0, 1.0])


# ---------------------------------------------------------------- software renderer
def sample_mesh_colors(sd: SceneData, n: int = 350_000, light=(0.3, -0.4, 0.85)) -> tuple[np.ndarray, np.ndarray]:
    """Surface splats for the software view: points sampled on the triangles with
    their texture colour (or Lambert shading)."""
    v, f = sd.mesh_vertices, sd.mesh_faces
    tri = v[f]
    area = 0.5 * np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1)
    if area.sum() <= 0:
        return np.zeros((0, 3), np.float32), np.zeros((0, 3), np.uint8)
    rng = np.random.default_rng(1)
    fi = rng.choice(len(f), n, p=area / area.sum())
    r1, r2 = rng.random(n), rng.random(n)
    s = np.sqrt(r1)
    b = np.column_stack([1 - s, s * (1 - r2), s * r2])
    pts = (tri[fi] * b[:, :, None]).sum(1).astype(np.float32)
    normal = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    normal /= np.maximum(np.linalg.norm(normal, axis=1, keepdims=True), 1e-12)
    lam = np.clip(np.abs(normal[fi] @ (np.asarray(light) / np.linalg.norm(light))), 0, 1) * 0.65 + 0.35
    if sd.mesh_uv is not None and sd.texture is not None:
        import cv2

        img = cv2.imread(str(sd.texture), cv2.IMREAD_COLOR)
        uv = (sd.mesh_uv[f[fi]] * b[:, :, None]).sum(1)
        h, w = img.shape[:2]
        cx = np.clip((uv[:, 0] * w).astype(int), 0, w - 1)
        cy = np.clip(((1 - uv[:, 1]) * h).astype(int), 0, h - 1)
        col = img[cy, cx, ::-1] * (0.45 + 0.55 * lam[:, None])
    elif sd.mesh_colors is not None:
        col = (sd.mesh_colors[f[fi]] * b[:, :, None]).sum(1)
        col = col * lam[:, None]
    else:
        col = np.outer(lam, [205, 200, 190])
    return pts, np.clip(col, 0, 255).astype(np.uint8)


@dataclass
class OrbitCamera:
    target: np.ndarray
    distance: float
    yaw: float = -60.0  # degrees around the up axis
    pitch: float = 35.0  # degrees above the horizon
    fov: float = 40.0
    up: np.ndarray = field(default_factory=lambda: np.array([0.0, 0.0, 1.0]))

    def basis(self) -> np.ndarray:
        """Rows: right, down, forward (OpenCV-like camera axes) in world."""
        up = self.up / np.linalg.norm(self.up)
        ref = np.array([1.0, 0, 0]) if abs(up[0]) < 0.9 else np.array([0.0, 1, 0])
        e1 = np.cross(up, ref)
        e1 /= np.linalg.norm(e1)
        e2 = np.cross(up, e1)
        y, p = np.radians(self.yaw), np.radians(self.pitch)
        horiz = np.cos(y) * e1 + np.sin(y) * e2
        back = np.cos(p) * horiz + np.sin(p) * up  # from target towards the eye
        fwd = -back
        right = np.cross(fwd, up)
        if np.linalg.norm(right) < 1e-9:
            right = e1
        right /= np.linalg.norm(right)
        down = np.cross(fwd, right)
        return np.vstack([right, down, fwd])

    @property
    def eye(self) -> np.ndarray:
        return self.target - self.basis()[2] * self.distance

    def project(self, pts: np.ndarray, width: int, height: int):
        R = self.basis()
        pc = (np.asarray(pts, np.float64) - self.eye) @ R.T
        f = 0.5 * height / np.tan(np.radians(self.fov) / 2)
        z = pc[:, 2]
        ok = z > self.distance * 1e-3
        with np.errstate(divide="ignore", invalid="ignore"):
            x = f * pc[:, 0] / z + width / 2
            y = f * pc[:, 1] / z + height / 2
        return x, y, z, ok


def splat(pts: np.ndarray, colors: np.ndarray, cam: OrbitCamera, width: int, height: int, size: int = 2,
          background=(52, 56, 64)) -> np.ndarray:
    """Z-buffered point splatting into an RGB image (nearest point wins per pixel)."""
    img = np.empty((height, width, 3), np.uint8)
    img[:] = background
    if pts is None or len(pts) == 0:
        return img
    x, y, z, ok = cam.project(pts, width, height)
    xi, yi = np.floor(x[ok]).astype(np.int64), np.floor(y[ok]).astype(np.int64)
    zz, col = z[ok], colors[ok]
    offs = [(0, 0)] if size <= 1 else [(dx, dy) for dx in range(size) for dy in range(size)]
    X = np.concatenate([xi + dx for dx, _ in offs])
    Y = np.concatenate([yi + dy for _, dy in offs])
    Z = np.tile(zz, len(offs))
    C = np.tile(np.arange(len(zz)), len(offs))
    inside = (X >= 0) & (X < width) & (Y >= 0) & (Y < height)
    X, Y, Z, C = X[inside], Y[inside], Z[inside], C[inside]
    if len(X) == 0:
        return img
    pix = Y * width + X
    order = np.lexsort((Z, pix))  # by pixel, nearest first
    pix_s = pix[order]
    first = np.ones(len(pix_s), bool)
    first[1:] = pix_s[1:] != pix_s[:-1]
    sel = order[first]
    img.reshape(-1, 3)[pix[sel]] = col[C[sel]]
    return img
