"""Texture atlas generation (OBJ + MTL + JPG).

Each face takes its colours from the photo that sees it best (frontal, close,
not occluded). Faces are packed two per square cell of the atlas; the colour of
every atlas texel is obtained by projecting its 3D position into the chosen photo.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from ..core.chunk import Chunk
from ..core.progress import Progress
from .visibility import ImageCache, Raycaster, project_valid, sample


def _cell_layout(s: int):
    """Pixel -> (triangle 0/1, barycentric) for one ``s x s`` cell, and the UV corners."""
    pad = 0.75
    tri = [np.array([[pad, pad], [s - pad - 1.0, pad], [pad, s - pad - 1.0]]),
           np.array([[s - pad, pad + 1.0], [s - pad, s - pad], [pad + 1.0, s - pad]])]
    yy, xx = np.mgrid[0:s, 0:s]
    p = np.column_stack([xx.ravel() + 0.5, yy.ravel() + 0.5])
    which = (p[:, 0] + p[:, 1] > s).astype(int)  # lower-right half -> triangle 1
    bary = np.zeros((len(p), 3))
    for t in (0, 1):
        a, b, c = tri[t]
        m = np.column_stack([b - a, c - a])
        sel = which == t
        l12 = np.linalg.solve(m, (p[sel] - a).T).T
        l = np.column_stack([1 - l12.sum(1), l12])
        l = np.clip(l, 0, None)
        bary[sel] = l / l.sum(1, keepdims=True)
    return which, bary, tri


def _face_camera_choice(chunk: Chunk, verts, faces, raycaster, progress: Progress):
    cams = chunk.aligned_cameras
    tri = verts[faces]
    centroid = tri.mean(1)
    normal = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    area = np.linalg.norm(normal, axis=1)
    normal = normal / np.maximum(area[:, None], 1e-20)
    n = len(faces)
    top_k = 3
    best_s = np.full((top_k, n), -np.inf)
    best_c = np.full((top_k, n), -1)
    for i, cam in enumerate(cams):
        px, depth, ok = project_valid(chunk, cam, centroid, margin=8)
        if not ok.any():
            continue
        to_cam = cam.center - centroid[ok]
        dist = np.linalg.norm(to_cam, axis=1)
        cos = (normal[ok] * to_cam).sum(1) / dist
        s = np.full(n, -np.inf)
        s[np.nonzero(ok)[0]] = np.where(cos > 0.1, cos * (chunk.sensor(cam).focal_px / dist) ** 2, -np.inf)
        # insert into the running top-k
        stack_s = np.vstack([best_s, s])
        stack_c = np.vstack([best_c, np.full(n, i)])
        order = np.argsort(-stack_s, axis=0)[:top_k]
        best_s = np.take_along_axis(stack_s, order, 0)
        best_c = np.take_along_axis(stack_c, order, 0)
        progress((i + 1) / len(cams) * 0.5, f"View selection {i + 1}/{len(cams)}")
    choice = np.full(n, -1)
    pending = np.arange(n)
    for rank in range(top_k):
        if len(pending) == 0:
            break
        cand = best_c[rank, pending]
        ok = np.isfinite(best_s[rank, pending]) & (cand >= 0)
        pending, cand = pending[ok], cand[ok]
        still = []
        for k in np.unique(cand):
            sel = pending[cand == k]
            if raycaster is not None:
                # nudge towards the camera so the face does not occlude itself
                pts = centroid[sel] + normal[sel] * 1e-6 * np.linalg.norm(cams[k].center - centroid[sel], axis=1)[:, None]
                vis = raycaster.visible_from(cams[k].center, pts)
                still.append(sel[~vis])
                sel = sel[vis]
            choice[sel] = k
        pending = np.concatenate(still) if still else np.zeros(0, int)
    return choice


def build_texture(chunk: Chunk, texture_size: int = 4096, progress: Progress | None = None) -> Path:
    progress = progress or Progress()
    mesh = chunk.model
    if mesh is None:
        raise RuntimeError("build the model first")
    verts = np.asarray(mesh.vertices, float)
    faces = np.asarray(mesh.faces, np.int64)
    raycaster = Raycaster(verts, faces)
    choice = _face_camera_choice(chunk, verts, faces, raycaster, progress.sub(0, 0.5))
    cams = chunk.aligned_cameras

    n = len(faces)
    cells = int(np.ceil(n / 2))
    per_side = int(np.ceil(np.sqrt(cells)))
    s = max(4, texture_size // per_side)
    size = per_side * s
    which, bary, tri = _cell_layout(s)
    atlas = np.full((size, size, 3), 127, np.uint8)
    face_ids = np.arange(n)
    cell = face_ids // 2
    t = face_ids % 2
    cy, cx = np.divmod(cell, per_side)

    # UVs of the three corners of each face
    corners = np.stack([tri[0], tri[1]])[t]  # (n, 3, 2)
    uv_px = corners + np.stack([cx * s, cy * s], 1)[:, None, :]
    uv = np.stack([uv_px[..., 0] / size, 1.0 - uv_px[..., 1] / size], -1)

    cache = ImageCache(chunk, max_items=3)
    local_pix = np.column_stack(np.divmod(np.arange(s * s), s)[::-1])  # (x, y) inside a cell
    for k in np.unique(choice[choice >= 0]):
        fsel = np.nonzero(choice == k)[0]
        batch = max(1, 2_000_000 // max(1, (s * s) // 2))  # ~2 M texels per batch bounds memory
        for start in range(0, len(fsel), batch):
            part = fsel[start:start + batch]
            # texels belonging to each face of the batch
            rows = []
            for tt in (0, 1):
                fp = part[t[part] == tt]
                if len(fp) == 0:
                    continue
                px_idx = np.nonzero(which == tt)[0]
                f_rep = np.repeat(fp, len(px_idx))
                p_rep = np.tile(px_idx, len(fp))
                rows.append((f_rep, p_rep))
            if not rows:
                continue
            f_all = np.concatenate([r[0] for r in rows])
            p_all = np.concatenate([r[1] for r in rows])
            pts = (bary[p_all][:, :, None] * verts[faces[f_all]]).sum(1)
            px, _, _ = project_valid(chunk, cams[k], pts, margin=0)
            good = np.isfinite(px).all(1)
            col = np.zeros((len(pts), 3), np.uint8)
            col[good] = sample(cache.get(cams[k]), px[good])
            ax = cx[f_all] * s + local_pix[p_all, 0]
            ay = cy[f_all] * s + local_pix[p_all, 1]
            atlas[ay, ax] = col
        progress(0.5 + 0.45 * (k + 1) / max(len(cams), 1), f"Texturing from {cams[k].label}")

    out_dir = chunk.data_dir / "textured"
    out_dir.mkdir(exist_ok=True)
    tex_path = out_dir / "texture.jpg"
    cv2.imwrite(str(tex_path), atlas, [cv2.IMWRITE_JPEG_QUALITY, 92])
    obj_path = out_dir / "model.obj"
    write_obj(obj_path, verts, faces, uv, "texture.jpg")
    chunk._set_product("model_textured", obj_path)
    progress(1.0, f"Texture {size}x{size} px")
    return obj_path


def write_obj(path: Path, verts: np.ndarray, faces: np.ndarray, uv: np.ndarray | None, texture: str | None,
              transform: np.ndarray | None = None) -> Path:
    """Write an OBJ with per-face-corner UVs (``uv`` shape ``(F,3,2)``)."""
    v = verts if transform is None else verts @ transform[:3, :3].T + transform[:3, 3]
    mtl = path.with_suffix(".mtl")
    with open(path, "w") as fh:
        if texture:
            fh.write(f"mtllib {mtl.name}\nusemtl material0\n")
        np.savetxt(fh, v, fmt="v %.6f %.6f %.6f")
        if uv is not None:
            np.savetxt(fh, uv.reshape(-1, 2), fmt="vt %.6f %.6f")
            idx = np.column_stack([faces[:, 0] + 1, np.arange(len(faces)) * 3 + 1,
                                   faces[:, 1] + 1, np.arange(len(faces)) * 3 + 2,
                                   faces[:, 2] + 1, np.arange(len(faces)) * 3 + 3])
            np.savetxt(fh, idx, fmt="f %d/%d %d/%d %d/%d")
        else:
            np.savetxt(fh, faces + 1, fmt="f %d %d %d")
    if texture:
        mtl.write_text(f"newmtl material0\nKa 1 1 1\nKd 1 1 1\nd 1\nillum 1\nmap_Kd {texture}\n")
    return path
