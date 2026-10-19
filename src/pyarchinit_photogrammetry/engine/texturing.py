"""Texture of the model: an image beside ``model.ply``, whose faces get the texture
coordinates of their three corners (exported as OBJ + MTL + image, or PLY + image).

Two mappings, as in Metashape:

``generic``
    every face takes its colours from the photo that sees it best (frontal, close, not
    occluded); faces are packed two per square cell of the atlas and the colour of every texel
    comes from projecting its 3D position into that photo. Good for objects and small models.
``orthophoto`` (adaptive)
    for terrain from the air (a tell, a trench seen from a drone): most of the surface faces the
    sky, so the texture is its orthophoto - the plan position of a corner is its texture
    coordinate, the colours come from the orthophoto engine - and the steep faces (walls,
    sections) get their own cells, as in the generic mapping, in a band below.
``auto``
    the orthophoto mapping for large models that mostly face up, the generic one otherwise.
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from ..core.chunk import Chunk
from ..core.progress import Progress
from .visibility import ImageCache, Raycaster, project_valid, readable_cameras, sample

MAPPINGS = ("auto", "orthophoto", "generic")


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


def _face_camera_choice(chunk: Chunk, verts, faces, raycaster, progress: Progress, cams=None):
    cams = chunk.aligned_cameras if cams is None else cams
    tri = verts[faces]
    centroid = tri.mean(1)
    normal = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    area = np.linalg.norm(normal, axis=1)
    normal = normal / np.maximum(area[:, None], 1e-20)
    n = len(faces)
    top_k = 3
    best_s = np.full((top_k, n), -np.inf)
    best_c = np.full((top_k, n), -1)
    from .masks import hidden_by_mask, mask_path

    for i, cam in enumerate(cams):
        px, depth, ok = project_valid(chunk, cam, centroid, margin=8)
        if mask_path(chunk, cam) is not None:
            ok &= ~hidden_by_mask(chunk, cam, px)  # a photo does not see what its mask hides
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


def _bake_cells(chunk: Chunk, verts, faces, face_ids, choice, cams, atlas, y0: int, per_row: int, s: int,
                progress: Progress, cache=None) -> np.ndarray:
    """Colours of the faces ``face_ids`` in cells of ``s`` px (two faces a cell, ``per_row``
    cells a row from row ``y0`` of the atlas), each from its photo ``cams[choice]``. Returns the
    texture coordinates of their corners ``(len(face_ids), 3, 2)``."""
    height, width = atlas.shape[:2]
    n = len(face_ids)
    which, bary, tri = _cell_layout(s)
    local = np.arange(n)
    cell = local // 2
    t = local % 2
    cy, cx = np.divmod(cell, per_row)
    corners = np.stack([tri[0], tri[1]])[t]  # (n, 3, 2)
    uv_px = corners + np.stack([cx * s, y0 + cy * s], 1)[:, None, :]
    uv = np.stack([uv_px[..., 0] / width, 1.0 - uv_px[..., 1] / height], -1)
    cache = cache or ImageCache(chunk, max_items=3)
    local_pix = np.column_stack(np.divmod(np.arange(s * s), s)[::-1])  # (x, y) inside a cell
    used = np.unique(choice[choice >= 0])
    for done, k in enumerate(used):
        fsel = np.nonzero(choice == k)[0]
        batch = max(1, 2_000_000 // max(1, (s * s) // 2))  # ~2 M texels per batch bounds memory
        for start in range(0, len(fsel), batch):
            part = fsel[start:start + batch]
            rows = []
            for tt in (0, 1):
                fp = part[t[part] == tt]
                if len(fp) == 0:
                    continue
                px_idx = np.nonzero(which == tt)[0]
                rows.append((np.repeat(fp, len(px_idx)), np.tile(px_idx, len(fp))))
            if not rows:
                continue
            f_all = np.concatenate([r[0] for r in rows])
            p_all = np.concatenate([r[1] for r in rows])
            pts = (bary[p_all][:, :, None] * verts[faces[face_ids[f_all]]]).sum(1)
            px, _, _ = project_valid(chunk, cams[k], pts, margin=0)
            good = np.isfinite(px).all(1)
            col = np.zeros((len(pts), 3), np.uint8)
            col[good] = sample(cache.get(cams[k]), px[good])
            atlas[y0 + cy[f_all] * s + local_pix[p_all, 1], cx[f_all] * s + local_pix[p_all, 0]] = col
        progress((done + 1) / max(len(used), 1), f"Texturing from {cams[k].label}")
    return uv


def _upward(chunk: Chunk, verts, faces) -> tuple[np.ndarray, np.ndarray]:
    """For every face: the vertical component of its normal (world frame) and its area."""
    w = chunk.to_world(verts)
    tri = w[faces]
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    area = np.linalg.norm(n, axis=1)
    return n[:, 2] / np.maximum(area, 1e-20), 0.5 * area


def choose_mapping(chunk: Chunk, verts, faces) -> str:
    """``orthophoto`` for large models that mostly face the sky (85 % of the area within 60
    degrees of horizontal), ``generic`` otherwise."""
    if len(faces) < 200_000:
        return "generic"
    nz, area = _upward(chunk, verts, faces)
    return "orthophoto" if area[nz > 0.5].sum() >= 0.85 * max(area.sum(), 1e-20) else "generic"


def _fill_empty(rgb: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """Texels without a colour take the colour around them (wider and wider), so that faces at
    the edge of the orthophoto do not sample black."""
    out = rgb.astype(np.float32)
    have = valid.astype(np.float32)
    todo = ~valid
    for k in (5, 17, 65, 257):
        if not todo.any():
            break
        num = cv2.blur(out * have[..., None], (k, k))
        den = cv2.blur(have, (k, k))
        fill = todo & (den > 1e-6)
        out[fill] = num[fill] / den[fill][:, None]
        have[fill] = 1.0
        todo &= ~fill
    if todo.any():
        out[todo] = out[~todo].mean(axis=0) if (~todo).any() else 127
    return np.clip(out + 0.5, 0, 255).astype(np.uint8)


def _orthophoto_atlas(chunk: Chunk, verts, faces, cams, texture_size: int, raycaster, progress: Progress,
                      correction=None):
    from .ortho import render_surface

    nz, _area = _upward(chunk, verts, faces)
    steep = np.nonzero(nz < 0.5)[0]
    flat = np.nonzero(nz >= 0.5)[0]
    w = chunk.to_world(verts)
    corners = w[faces[flat]].reshape(-1, 3) if len(flat) else w
    x0, y0 = corners[:, 0].min(), corners[:, 1].min()
    x1, y1 = corners[:, 0].max(), corners[:, 1].max()
    size = int(texture_size)
    res = max(x1 - x0, y1 - y0, 1e-9) / size
    wo = min(size, int(np.ceil((x1 - x0) / res)) + 1)
    ho = min(size, int(np.ceil((y1 - y0) / res)) + 1)
    cell, per_row = 8, size // 8
    rows = int(np.ceil(np.ceil(len(steep) / 2) / per_row)) if len(steep) else 0
    while rows * cell > size // 2 and cell > 4:  # the walls take at most half more of the texture
        cell //= 2
        per_row = size // cell
        rows = int(np.ceil(np.ceil(len(steep) / 2) / per_row))
    height = ho + rows * cell
    atlas = np.full((height, size, 3), 127, np.uint8)
    progress(0.02, f"Orthophoto of the model {wo}x{ho} px at {res * 100:.2f} cm/px")
    rgba = render_surface(chunk, (x0, y1), (ho, wo), res, cams=cams, raycaster=raycaster,
                          progress=progress.sub(0.02, 0.68), correction=correction)
    rgb = np.moveaxis(rgba[:3], 0, -1)[..., ::-1]  # BGR, as OpenCV writes it
    atlas[:ho, :wo] = _fill_empty(rgb, rgba[3] > 0)
    uv = np.zeros((len(faces), 3, 2))
    if len(flat):
        pw = w[faces[flat]]  # (k, 3, 3)
        uv[flat, :, 0] = (pw[..., 0] - x0) / res / size
        uv[flat, :, 1] = 1.0 - ((y1 - pw[..., 1]) / res) / height
    if len(steep):
        progress(0.72, f"Walls and steep faces: {len(steep):,} faces in cells of {cell} px")
        choice = _face_camera_choice(chunk, verts, faces[steep], raycaster, progress.sub(0.72, 0.13), cams=cams)
        uv[steep] = _bake_cells(chunk, verts, faces, steep, choice, cams, atlas, ho, per_row, cell,
                                progress.sub(0.85, 0.13), ImageCache(chunk, 3, correction))
    return atlas, uv


def _generic_atlas(chunk: Chunk, verts, faces, cams, texture_size: int, raycaster, progress: Progress,
                   correction=None):
    n = len(faces)
    choice = _face_camera_choice(chunk, verts, faces, raycaster, progress.sub(0, 0.5), cams=cams)
    cells = int(np.ceil(n / 2))
    per_side = int(np.ceil(np.sqrt(cells)))
    s = max(4, texture_size // per_side)
    size = per_side * s
    atlas = np.full((size, size, 3), 127, np.uint8)
    uv = _bake_cells(chunk, verts, faces, np.arange(n), choice, cams, atlas, 0, per_side, s, progress.sub(0.5, 0.45),
                     ImageCache(chunk, 3, correction))
    return atlas, uv


def build_texture(chunk: Chunk, texture_size: int = 4096, progress: Progress | None = None,
                  skip_unreadable: bool = False, mapping: str = "auto", colour_correction: bool | None = None) -> Path:
    """Texture of the model (see the module docstring for ``mapping``), written as
    ``model_texture.jpg`` with the texture coordinates in ``model.ply`` (every other property of
    the model kept). Photos that cannot be read stop the step
    (:class:`~.visibility.UnreadablePhotos`) unless ``skip_unreadable``: then the faces take
    their colours from the other photos. ``colour_correction``: gains of the photos and
    vignetting (:mod:`.colour`): None = the one measured in the chunk if any, True = measured
    now when missing, False = none."""
    from ..io.mesh_ply import read_mesh, write_mesh
    from .ortho import correction_for

    progress = progress or Progress()
    path = chunk.product_path("model")
    if path is None or not path.exists():
        raise RuntimeError("build the model first")
    if mapping not in MAPPINGS:
        raise ValueError(f"mapping: one of {', '.join(MAPPINGS)}")
    progress(0.0, "Checking the photos")
    cams, bad = readable_cameras(chunk, skip_unreadable)
    correction = correction_for(chunk, colour_correction, progress.sub(0.0, 0.01))
    mesh = read_mesh(path)
    verts, faces = mesh.xyz, mesh.faces
    raycaster = Raycaster(verts, faces)
    if mapping == "auto":
        mapping = choose_mapping(chunk, verts, faces)
    progress(0.01, f"Texture: {mapping} mapping of {len(faces):,} faces")
    if mapping == "orthophoto":
        atlas, uv = _orthophoto_atlas(chunk, verts, faces, cams, texture_size, raycaster, progress.sub(0.01, 0.95),
                                      correction)
    else:
        atlas, uv = _generic_atlas(chunk, verts, faces, cams, texture_size, raycaster, progress.sub(0.01, 0.95),
                                   correction)
    tex_path = chunk.data_dir / "model_texture.jpg"
    cv2.imwrite(str(tex_path), atlas, [cv2.IMWRITE_JPEG_QUALITY, 92])
    mesh.texcoords = uv.reshape(-1, 6).astype(np.float32)
    mesh.set_texture_file(tex_path.name)
    chunk._set_product("model", write_mesh(chunk.own_product_path("model"), mesh))
    chunk._set_product("model_textured", tex_path)
    chunk.meta["texture"] = {"mapping": mapping, "size": [int(atlas.shape[1]), int(atlas.shape[0])],
                             "skipped_photos": [c.label for c in bad] or None,
                             "colour_correction": correction is not None}
    progress(1.0, f"Texture {atlas.shape[1]}x{atlas.shape[0]} px ({mapping})")
    return tex_path


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
