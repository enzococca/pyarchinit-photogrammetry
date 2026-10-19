"""Light models with a normal map: the model reduced to a few faces for the web and game engines,
with the colour and the relief of the full model baked into two textures.

The model is reduced by quadric decimation (Open3D) to ``target_faces``; its faces get cells of an
atlas, two a cell, as in the generic texture (:func:`.texturing._cell_layout`). For every texel of
a face, the point of the light surface there is moved along the light normal onto the full model
(a ray each way, as far as a few edges of the light model): the colour of the full model at the hit
(its texture, else its vertex colours) goes into the colour texture, its normal - in the tangent
space of the light face, the frame that viewers use (x along U, y along V, z the smooth normal) -
into the normal map (RGB = (n + 1) / 2, the OpenGL convention: green up, as glTF wants it).

:func:`light_model` writes ``<data>/light/``: ``light.glb`` (both textures, a PBR material, local
coordinates around the centre as :func:`io.web.export_glb`), ``light.obj`` + ``.mtl`` (world
coordinates; ``map_Kd`` and ``norm``), ``light_color.jpg`` and ``light_normal.png``.
"""
from __future__ import annotations

import json

import cv2
import numpy as np

from ..core.progress import Progress, as_progress
from .texturing import _cell_layout, _fill_empty
from .visibility import sample


def decimate(verts: np.ndarray, faces: np.ndarray, target_faces: int):
    """Quadric decimation (Open3D) to about ``target_faces`` faces: ``(vertices, faces)``."""
    import open3d as o3d

    if len(faces) <= target_faces:
        return np.asarray(verts, float), np.asarray(faces, np.int64)
    m = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(np.asarray(verts, float)),
                                  o3d.utility.Vector3iVector(np.asarray(faces, np.int32)))
    m = m.simplify_quadric_decimation(int(target_faces))
    m.remove_degenerate_triangles()
    m.remove_unreferenced_vertices()
    return np.asarray(m.vertices, float), np.asarray(m.triangles, np.int64)


def _vertex_normals(verts, faces) -> np.ndarray:
    fn = np.cross(verts[faces[:, 1]] - verts[faces[:, 0]], verts[faces[:, 2]] - verts[faces[:, 0]])
    vn = np.zeros_like(verts)
    for k in range(3):
        np.add.at(vn, faces[:, k], fn)  # weighted by the area
    return vn / np.maximum(np.linalg.norm(vn, axis=1, keepdims=True), 1e-12)


def bake_maps(full_verts, full_faces, light_verts, light_faces, size: int = 2048, full_colors=None,
              full_texcoords=None, full_texture=None, progress: Progress | None = None, full_texnumber=None):
    """Colour texture and normal map of a light mesh from the full one (see the module
    docstring). ``full_texcoords``: ``(F, 6)`` per face of the full mesh with ``full_texture``
    (BGR image, or the images of the pages with ``full_texnumber`` the page of every face); else
    ``full_colors`` per vertex (RGB). Returns ``(colour BGR, normal RGB, uv (F_light, 3, 2), hit
    share)``."""
    import open3d as o3d

    progress = as_progress(progress)
    fv, ff = np.asarray(full_verts, np.float64), np.asarray(full_faces, np.int64)
    lv, lf = np.asarray(light_verts, np.float64), np.asarray(light_faces, np.int64)
    n = len(lf)
    cells = int(np.ceil(n / 2))
    per_side = int(np.ceil(np.sqrt(cells)))
    s = max(4, size // per_side)
    side = per_side * s
    which, bary, tri = _cell_layout(s)
    t = np.arange(n) % 2
    cy, cx = np.divmod(np.arange(n) // 2, per_side)
    corners = np.stack([tri[0], tri[1]])[t]
    uv_px = corners + np.stack([cx * s, cy * s], 1)[:, None, :]  # (n, 3, 2) pixels, y down
    uv = np.stack([uv_px[..., 0] / side, 1.0 - uv_px[..., 1] / side], -1)
    # the frame of every light face: tangent along U, bitangent along V (texture space y up)
    e1, e2 = lv[lf[:, 1]] - lv[lf[:, 0]], lv[lf[:, 2]] - lv[lf[:, 0]]
    d1, d2 = uv[:, 1] - uv[:, 0], uv[:, 2] - uv[:, 0]
    det = d1[:, 0] * d2[:, 1] - d2[:, 0] * d1[:, 1]
    det = np.where(np.abs(det) < 1e-20, 1e-20, det)
    tangent = (e1 * d2[:, 1:2] - e2 * d1[:, 1:2]) / det[:, None]
    bitangent = (e2 * d1[:, 0:1] - e1 * d2[:, 0:1]) / det[:, None]
    lvn, fvn = _vertex_normals(lv, lf), _vertex_normals(fv, ff)
    edge = float(np.median(np.linalg.norm(e1, axis=1))) if n else 1.0
    reach = 4.0 * edge
    scene = o3d.t.geometry.RaycastingScene()
    scene.add_triangles(o3d.core.Tensor(fv.astype(np.float32)), o3d.core.Tensor(ff.astype(np.uint32)))
    colour = np.zeros((side, side, 3), np.uint8)
    normal = np.zeros((side, side, 3), np.uint8)
    valid = np.zeros((side, side), bool)
    local = np.column_stack(np.divmod(np.arange(s * s), s)[::-1])
    if full_texture is None:
        tex = None
    elif isinstance(full_texture, (list, tuple)):
        tex = [np.asarray(t) for t in full_texture]
    else:
        tex = [np.asarray(full_texture)]
    page = None if full_texnumber is None or tex is None or len(tex) < 2 else np.asarray(full_texnumber)
    hits = total = 0
    batch = max(1, 1_500_000 // max(1, (s * s) // 2))
    for start in range(0, n, batch):
        progress.check()
        part = np.arange(start, min(start + batch, n))
        f_list, p_list = [], []
        for tt in (0, 1):
            fp = part[t[part] == tt]
            px_idx = np.nonzero(which == tt)[0]
            if len(fp):
                f_list.append(np.repeat(fp, len(px_idx)))
                p_list.append(np.tile(px_idx, len(fp)))
        if not f_list:
            continue
        f_all, p_all = np.concatenate(f_list), np.concatenate(p_list)
        b = bary[p_all]
        pts = (b[:, :, None] * lv[lf[f_all]]).sum(1)
        nrm = (b[:, :, None] * lvn[lf[f_all]]).sum(1)
        nrm /= np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-12)
        # from outside the light surface inwards, then from inside outwards for what was missed
        dist = np.full(len(pts), np.inf)
        prim = np.full(len(pts), -1, np.int64)
        uvw = np.zeros((len(pts), 2))
        for sign in (1.0, -1.0):
            todo = ~np.isfinite(dist)
            if not todo.any():
                break
            rays = np.hstack([pts[todo] + sign * nrm[todo] * reach, -sign * nrm[todo]]).astype(np.float32)
            ans = scene.cast_rays(o3d.core.Tensor(rays))
            th = ans["t_hit"].numpy()
            ok = np.isfinite(th) & (th <= 2 * reach)
            idx = np.nonzero(todo)[0][ok]
            dist[idx] = np.abs(th[ok] - reach)
            prim[idx] = ans["primitive_ids"].numpy()[ok].astype(np.int64)
            uvw[idx] = ans["primitive_uvs"].numpy()[ok]
        hit = prim >= 0
        total += len(pts)
        hits += int(hit.sum())
        rows = cy[f_all] * s + local[p_all, 1]
        cols = cx[f_all] * s + local[p_all, 0]
        if hit.any():
            hp, hu = prim[hit], uvw[hit]
            w = np.column_stack([1 - hu[:, 0] - hu[:, 1], hu[:, 0], hu[:, 1]])  # barycentrics of the hit
            fn = (w[:, :, None] * fvn[ff[hp]]).sum(1)
            fn /= np.maximum(np.linalg.norm(fn, axis=1, keepdims=True), 1e-12)
            fi = f_all[hit]
            N = nrm[hit]
            T = tangent[fi] - (tangent[fi] * N).sum(1, keepdims=True) * N
            T /= np.maximum(np.linalg.norm(T, axis=1, keepdims=True), 1e-12)
            B = np.cross(N, T)
            B *= np.sign((B * bitangent[fi]).sum(1, keepdims=True) + 1e-30)
            ts = np.column_stack([(fn * T).sum(1), (fn * B).sum(1), (fn * N).sum(1)])
            normal[rows[hit], cols[hit]] = np.clip((ts + 1) * 127.5, 0, 255).astype(np.uint8)
            if tex is not None and full_texcoords is not None:
                tc = np.asarray(full_texcoords, np.float64).reshape(-1, 3, 2)[hp]
                st = (w[:, :, None] * tc).sum(1)
                on = np.zeros(len(hp), int) if page is None else page[hp]
                hr, hc = rows[hit], cols[hit]
                for k in np.unique(on):  # each texel from the page of its face
                    sel = on == k
                    h, wd = tex[k].shape[:2]
                    colour[hr[sel], hc[sel]] = sample(tex[k], np.column_stack([st[sel, 0] * wd,
                                                                               (1 - st[sel, 1]) * h]))
            elif full_colors is not None:
                c = (w[:, :, None] * np.asarray(full_colors, np.float64)[ff[hp]]).sum(1)
                colour[rows[hit], cols[hit]] = np.clip(c[:, ::-1], 0, 255).astype(np.uint8)  # RGB -> BGR
            else:
                colour[rows[hit], cols[hit]] = 200
            valid[rows[hit], cols[hit]] = True
        progress(min(1.0, (start + len(part)) / n), f"Normal map: {start + len(part):,}/{n:,} faces")
    flat = np.zeros_like(normal)
    flat[:] = (128, 128, 255)
    normal = np.where(valid[..., None], normal, flat)
    colour = _fill_empty(colour, valid)
    normal = _fill_empty(normal, valid)
    return colour, normal, uv, hits / max(total, 1)


def light_model(chunk, target_faces: int = 50_000, texture_size: int = 4096, folder=None,
                progress: Progress | None = None) -> dict:
    """The light model of the chunk with its colour texture and normal map (see the module
    docstring), in ``folder`` (default ``<data>/light``); returns the paths written and the share
    of texels that found the full model."""
    from pathlib import Path

    import trimesh
    from PIL import Image

    from ..io.mesh_ply import read_mesh

    progress = as_progress(progress)
    path = chunk.product_path("model")
    if path is None or not path.exists():
        raise RuntimeError("build the model first")
    mesh = read_mesh(path)
    fv, ff = mesh.xyz, np.asarray(mesh.faces, np.int64)
    from .texturing import display_texture, texture_pages

    pages = texture_pages(chunk)
    image, texcoords = None, None
    if pages and pages[0].suffix.lower() != ".obj" and mesh.texcoords is not None:
        source, texcoords = display_texture(chunk, mesh.texcoords, mesh.texnumber)  # the pages in one image
        image = cv2.imread(str(source), cv2.IMREAD_COLOR)
    progress(0.02, f"Light model: {len(ff):,} -> {target_faces:,} faces")
    lv, lf = decimate(fv, ff, target_faces)
    colour, normal, uv, share = bake_maps(fv, ff, lv, lf, texture_size, mesh.colors,
                                          texcoords if image is not None else None, image,
                                          progress=progress.sub(0.1, 0.8))
    out = Path(folder) if folder is not None else chunk.data_dir / "light"
    out.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out / "light_color.jpg"), colour, [cv2.IMWRITE_JPEG_QUALITY, 92])
    cv2.imwrite(str(out / "light_normal.png"), cv2.cvtColor(normal, cv2.COLOR_RGB2BGR))
    world = lv @ chunk.transform[:3, :3].T + chunk.transform[:3, 3]
    # OBJ in world coordinates
    from .texturing import write_obj

    write_obj(out / "light.obj", lv, lf, uv, "light_color.jpg", transform=chunk.transform)
    mtl = out / "light.mtl"
    if mtl.exists():
        mtl.write_text(mtl.read_text().rstrip() + "\nnorm light_normal.png\n")
    # GLB with a PBR material: local coordinates around the centre, Y up
    centre = (world.min(0) + world.max(0)) / 2
    local = world - centre
    corner = lf.reshape(-1)
    v = np.column_stack([local[:, 0], local[:, 2], -local[:, 1]])[corner]
    material = trimesh.visual.material.PBRMaterial(
        baseColorTexture=Image.open(out / "light_color.jpg"), normalTexture=Image.open(out / "light_normal.png"),
        metallicFactor=0.0, roughnessFactor=1.0)
    visual = trimesh.visual.TextureVisuals(uv=uv.reshape(-1, 2), material=material)
    tm = trimesh.Trimesh(v, np.arange(len(corner)).reshape(-1, 3), visual=visual, process=False)
    scene = trimesh.Scene(tm)
    scene.metadata["extras"] = {"crs": chunk.crs, "origin": [float(x) for x in centre]}
    (out / "light.glb").write_bytes(scene.export(file_type="glb"))
    (out / "light.json").write_text(json.dumps({"crs": chunk.crs, "origin": [float(x) for x in centre],
                                                "faces": int(len(lf)), "full_faces": int(len(ff)),
                                                "texture": int(colour.shape[0]), "hits": share}, indent=2))
    progress(1.0, f"Light model: {len(lf):,} faces, textures {colour.shape[0]} px")
    return {"glb": out / "light.glb", "obj": out / "light.obj", "color": out / "light_color.jpg",
            "normal": out / "light_normal.png", "faces": int(len(lf)), "hits": share}
