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

A texture larger than :data:`PAGE_SIZE` (the side graphics cards and viewers take) is split into
pages, as Metashape does: ``model_texture.jpg``, ``model_texture_1.jpg``... named in ``model.ply``
(``TextureFile`` comments) with the page of every face (``texnumber``). The generic mapping puts
groups of nearby faces on each page; the orthophoto mapping cuts the plan into tiles, a page each,
and puts the steep faces on pages of their own.

The other computers started as nodes on the project folder (:mod:`.nodes`) take part: the tiles of
the orthophoto as for the orthomosaic, and whole rows of cells - every face is decided on its own,
so a row does not depend on the others.
"""
from __future__ import annotations

import threading
from pathlib import Path

import cv2
import numpy as np

from ..core.chunk import Chunk
from ..core.progress import Progress, as_progress
from .visibility import ImageCache, Raycaster, project_valid, readable_cameras, sample

MAPPINGS = ("auto", "orthophoto", "generic")
PAGE_SIZE = 8192


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
    which, bary, _tri = _cell_layout(s)
    local = np.arange(n)
    cell = local // 2
    t = local % 2
    cy, cx = np.divmod(cell, per_row)
    uv = _cell_uv(n, y0, per_row, s, width, height)
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


def _cell_uv(n: int, y0: int, per_row: int, s: int, width: int, height: int) -> np.ndarray:
    """The texture coordinates ``(n, 3, 2)`` of ``n`` faces in cells of ``s`` px (two faces a cell,
    ``per_row`` cells a row from row ``y0``) of an atlas of ``width`` x ``height`` px."""
    _which, _bary, tri = _cell_layout(s)
    local = np.arange(n)
    cy, cx = np.divmod(local // 2, per_row)
    corners = np.stack([tri[0], tri[1]])[local % 2]  # (n, 3, 2)
    uv_px = corners + np.stack([cx * s, y0 + cy * s], 1)[:, None, :]
    return np.stack([uv_px[..., 0] / width, 1.0 - uv_px[..., 1] / height], -1)


def _bake(chunk: Chunk, verts, faces, ids, cams, atlas, y0: int, per_row: int, s: int, raycaster, progress: Progress,
          correction=None, cache=None) -> np.ndarray:
    """The faces ``ids`` (in this order) in cells of ``s`` px of ``atlas`` (two faces a cell,
    ``per_row`` cells a row from row ``y0``), each coloured from the photo that sees it best;
    returns their texture coordinates. With nodes present (:mod:`.nodes`) they take whole rows of
    cells; ``correction``: the colour correction measured in the chunk, or None."""
    from .. import __version__
    from . import nodes

    ids = np.asarray(ids)
    height, width = atlas.shape[:2]
    rows = int(np.ceil(np.ceil(len(ids) / 2) / per_row))
    band = max(1, nodes.TEXTURE_UNIT_TEXELS // max(1, per_row * s * s))  # rows of cells in a unit
    cache = cache or ImageCache(chunk, 3, correction)
    files_dir = nodes.sharing(chunk, -(-rows // band))
    if files_dir is None:
        choice = _face_camera_choice(chunk, verts, faces[ids], raycaster, progress.sub(0.0, 0.5), cams=cams)
        return _bake_cells(chunk, verts, faces, ids, choice, cams, atlas, y0, per_row, s, progress.sub(0.5, 0.5),
                           cache)
    root = nodes.folder(files_dir)
    state = nodes.share_chunk(files_dir, chunk, texture={"cameras": [c.key for c in cams],
                                                         "colour": correction is not None})
    np.save(nodes.state_file(root, state, "faces.npy"), ids)
    step = 2 * per_row * band
    units = [{"kind": "texture_cells", "version": __version__, "state": state, "start": a,
              "stop": min(len(ids), a + step), "per_row": int(per_row), "s": int(s), "width": int(width)}
             for a in range(0, len(ids), step)]

    def rows_of(start: int, stop: int) -> slice:
        r0 = y0 + (start // (2 * per_row)) * s
        return slice(r0, r0 + int(np.ceil(np.ceil((stop - start) / 2) / per_row)) * s)

    def local(payload, quiet):  # into the atlas itself: the rows of a unit are its own
        a, b = payload["start"], payload["stop"]
        choice = _face_camera_choice(chunk, verts, faces[ids[a:b]], raycaster, quiet, cams=cams)
        _bake_cells(chunk, verts, faces, ids[a:b], choice, cams, atlas[rows_of(a, b)], 0, per_row, s, quiet, cache)
        return {}

    def write_theirs(got):
        res = got.get("result") or {}
        if not res.get("file"):
            return
        path = root / "done" / res["file"]
        with np.load(path) as data:
            atlas[rows_of(res["start"], res["stop"])] = data["cells"]
        path.unlink(missing_ok=True)

    progress(0.0, f"Texture: {len(units)} units of work shared with {len(nodes.wanted(files_dir))} other computer(s)")
    try:
        results = nodes.run(files_dir, units, progress, "units of texture cells", project_path=chunk.document.path,
                            local=local, each=write_theirs)
    finally:
        nodes.forget_state(files_dir, state)
    nodes.forget(files_dir, [r["name"] for r in results])
    return _cell_uv(len(ids), y0, per_row, s, width, height)


_NODE: dict = {}  # on a node: the step whose units it does (kept for its next units)
_NODE_LOCK = threading.Lock()


def node_cells(payload: dict, mapping, out_dir) -> dict:
    """A unit of :func:`_bake` on a node: the photos of its faces chosen and their rows of cells
    coloured, saved as ``<unit>.npz`` for the main program."""
    from ..io.mesh_ply import read_mesh
    from . import nodes
    from .ortho import correction_for

    out_dir = Path(out_dir)
    root = out_dir.parent
    with _NODE_LOCK:
        if _NODE.get("state") != payload["state"]:
            _NODE.clear()  # the photos and the mesh of the step before go first
            chunk, data = nodes.node_chunk(root, payload["state"], mapping)
            spec = data["texture"]
            correction = correction_for(chunk) if spec["colour"] else None
            if spec["colour"] and correction is None:
                raise RuntimeError("the colour correction of the main program is missing")
            path = chunk.product_path("model")
            if path is None or not path.exists():
                raise RuntimeError("the model is not in the project folder of this computer")
            mesh = read_mesh(path)
            by_key = {c.key: c for c in chunk.cameras}
            _NODE.update(state=payload["state"], chunk=chunk, verts=mesh.xyz, faces=mesh.faces,
                         raycaster=Raycaster(mesh.xyz, mesh.faces), cams=[by_key[k] for k in spec["cameras"]],
                         ids=np.load(nodes.state_file(root, payload["state"], "faces.npy")),
                         cache=ImageCache(chunk, 3, correction))
        node = dict(_NODE)
    a, b, per_row, s = int(payload["start"]), int(payload["stop"]), int(payload["per_row"]), int(payload["s"])
    ids = node["ids"][a:b]
    quiet = as_progress(None)
    choice = _face_camera_choice(node["chunk"], node["verts"], node["faces"][ids], node["raycaster"], quiet,
                                 cams=node["cams"])
    cells = np.full((int(np.ceil(np.ceil(len(ids) / 2) / per_row)) * s, int(payload["width"]), 3), 127, np.uint8)
    _bake_cells(node["chunk"], node["verts"], node["faces"], ids, choice, node["cams"], cells, 0, per_row, s, quiet,
                node["cache"])
    out = out_dir / f"{payload['name']}.npz"
    np.savez_compressed(out, cells=cells)
    return {"file": out.name, "start": a, "stop": b}


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
        uv[steep] = _bake(chunk, verts, faces, steep, cams, atlas, ho, per_row, cell, raycaster,
                          progress.sub(0.72, 0.26), correction)
    return atlas, uv


def _generic_atlas(chunk: Chunk, verts, faces, cams, texture_size: int, raycaster, progress: Progress,
                   correction=None):
    n = len(faces)
    cells = int(np.ceil(n / 2))
    per_side = int(np.ceil(np.sqrt(cells)))
    s = max(4, texture_size // per_side)
    size = per_side * s
    atlas = np.full((size, size, 3), 127, np.uint8)
    uv = _bake(chunk, verts, faces, np.arange(n), cams, atlas, 0, per_side, s, raycaster, progress.sub(0, 0.95),
               correction)
    return atlas, uv


def _split_spatially(points: np.ndarray, groups: int) -> list[np.ndarray]:
    """Indices of ``points`` in ``groups`` groups of about the same size, each of points close
    together (median cuts along the longest side, recursively)."""
    def cut(idx, k):
        if k <= 1 or len(idx) <= 1:
            return [idx]
        p = points[idx]
        axis = int(np.argmax(np.ptp(p, axis=0)))
        order = idx[np.argsort(p[:, axis], kind="stable")]
        at = int(round(len(order) * (k // 2) / k))
        return cut(order[:at], k // 2) + cut(order[at:], k - k // 2)

    return [g for g in cut(np.arange(len(points)), max(1, groups)) if len(g)]


def _pages_per_side(texture_size: int, page_size: int) -> int:
    return max(1, int(np.ceil(texture_size / max(page_size, 1))))


def _generic_pages(chunk: Chunk, verts, faces, cams, texture_size: int, page_size: int, raycaster,
                   progress: Progress, correction=None):
    """The generic mapping on pages: groups of nearby faces, a page each, with all the texels of a
    ``texture_size`` square shared among them."""
    side = _pages_per_side(texture_size, page_size)
    if side == 1:
        atlas, uv = _generic_atlas(chunk, verts, faces, cams, texture_size, raycaster, progress, correction)
        return [atlas], uv, None
    n = len(faces)
    groups = _split_spatially(verts[faces].mean(1), side * side)
    uv, page, atlases = np.zeros((n, 3, 2)), np.zeros(n, np.int32), []
    cache = ImageCache(chunk, 3, correction)
    for g, ids in enumerate(groups):
        per_side = int(np.ceil(np.sqrt(np.ceil(len(ids) / 2))))
        s = max(4, page_size // per_side)
        atlas = np.full((per_side * s, per_side * s, 3), 127, np.uint8)
        uv[ids] = _bake(chunk, verts, faces, ids, cams, atlas, 0, per_side, s, raycaster,
                        progress.sub(0.98 * g / len(groups), 0.98 / len(groups)), correction, cache)
        page[ids] = g
        atlases.append(atlas)
    return atlases, uv, page


def _orthophoto_pages(chunk: Chunk, verts, faces, cams, texture_size: int, page_size: int, raycaster,
                      progress: Progress, correction=None):
    """The orthophoto mapping on pages: the plan cut into tiles of ``page_size`` pixels at the
    resolution of a ``texture_size`` texture, each tile the orthophoto of the faces whose centre
    falls in it (so a page reaches a little beyond its tile), the steep faces in cells on pages of
    their own."""
    from .ortho import render_surface

    side = _pages_per_side(texture_size, page_size)
    if side == 1:
        atlas, uv = _orthophoto_atlas(chunk, verts, faces, cams, texture_size, raycaster, progress, correction)
        return [atlas], uv, None
    nz, _area = _upward(chunk, verts, faces)
    steep = np.nonzero(nz < 0.5)[0]
    flat = np.nonzero(nz >= 0.5)[0]
    w = chunk.to_world(verts)
    n = len(faces)
    uv, page, atlases = np.zeros((n, 3, 2)), np.zeros(n, np.int32), []
    if len(flat):
        corners = w[faces[flat]]  # (k, 3, 3)
        x0, y1 = corners[..., 0].min(), corners[..., 1].max()
        res = max(corners[..., 0].max() - x0, y1 - corners[..., 1].min(), 1e-9) / texture_size
        span = page_size * res
        centre = corners.mean(axis=1)
        tx = np.clip(np.floor((centre[:, 0] - x0) / span).astype(int), 0, side - 1)
        ty = np.clip(np.floor((y1 - centre[:, 1]) / span).astype(int), 0, side - 1)
        tiles = [(i, j) for j in range(side) for i in range(side) if np.any((tx == i) & (ty == j))]
        for k, (i, j) in enumerate(tiles):
            mine = (tx == i) & (ty == j)
            sel, pc = flat[mine], corners[mine]
            bx0, by1 = pc[..., 0].min(), pc[..., 1].max()
            wo = int(np.ceil((pc[..., 0].max() - bx0) / res)) + 1
            ho = int(np.ceil((by1 - pc[..., 1].min()) / res)) + 1
            progress(0.02 + 0.68 * k / len(tiles), f"Texture page {len(atlases) + 1}: orthophoto {wo}x{ho} px")
            rgba = render_surface(chunk, (bx0, by1), (ho, wo), res, cams=cams, raycaster=raycaster,
                                  progress=progress.sub(0.02 + 0.68 * k / len(tiles), 0.68 / len(tiles)),
                                  correction=correction)
            atlases.append(_fill_empty(np.moveaxis(rgba[:3], 0, -1)[..., ::-1], rgba[3] > 0))
            uv[sel, :, 0] = (pc[..., 0] - bx0) / res / wo
            uv[sel, :, 1] = 1.0 - ((by1 - pc[..., 1]) / res) / ho
            page[sel] = len(atlases) - 1
    if len(steep):
        cell = 8
        per_row = page_size // cell
        per_page = 2 * per_row * per_row
        progress(0.72, f"Walls and steep faces: {len(steep):,} faces in cells of {cell} px")
        groups = _split_spatially(verts[faces[steep]].mean(1), int(np.ceil(len(steep) / per_page)))
        cache = ImageCache(chunk, 3, correction)
        for g, local in enumerate(groups):
            rows = int(np.ceil(np.ceil(len(local) / 2) / per_row))
            atlas = np.full((rows * cell, page_size, 3), 127, np.uint8)
            uv[steep[local]] = _bake(chunk, verts, faces, steep[local], cams, atlas, 0, per_row, cell, raycaster,
                                     progress.sub(0.72 + 0.26 * g / len(groups), 0.26 / len(groups)), correction,
                                     cache)
            page[steep[local]] = len(atlases)
            atlases.append(atlas)
    return atlases, uv, page


def texture_pages(chunk: Chunk) -> list[Path]:
    """The images of the texture of the model, page by page (one for most models): the pages
    named in ``model.ply`` beside the first one (the ``model_textured`` product); [] without a
    texture (a texture of version 0.5, an OBJ, is its own single entry)."""
    from ..io.mesh_ply import texture_files_of

    first = chunk.product_path("model_textured")
    if first is None or not first.exists():
        return []
    model = chunk.product_path("model")
    if first.suffix.lower() == ".obj" or model is None or not model.exists():
        return [first]
    names = texture_files_of(model)
    return [first.parent / name for name in names] if len(names) > 1 else [first]


def _display_current(out: Path, pages: list[Path], size: tuple[int, int]) -> bool:
    """Whether the image made for the view is still good: newer than every page and of the size wanted
    now (it was made for another limit, or for the pages of an earlier texture)."""
    from PIL import Image

    if not out.exists() or out.stat().st_mtime < max(p.stat().st_mtime for p in pages):
        return False
    try:
        with Image.open(out) as im:
            return tuple(im.size) == tuple(size)
    except OSError:
        return False


def display_texture(chunk: Chunk, texcoords: np.ndarray, texnumber: np.ndarray | None, max_side: int = PAGE_SIZE):
    """One image for showing a texture of several pages (the 3D view, the fly-through): the pages
    side by side, reduced to fit ``max_side``, kept as ``model_texture_display.jpg`` until the
    pages change, and the texture coordinates ``(F, 6)`` in it. One page: itself, or a reduced copy
    when it is larger than ``max_side`` (an imported texture: graphics cards stop at 8192-16384 px)."""
    from PIL import Image

    pages = texture_pages(chunk)
    if len(pages) <= 1 or texnumber is None:
        if not pages:
            return None, texcoords
        with Image.open(pages[0]) as im:
            w, h = im.size
        if max(w, h) <= max_side:
            return pages[0], texcoords
        f = max_side / max(w, h)
        size = (max(1, int(w * f + 1e-9)), max(1, int(h * f + 1e-9)))  # w * (512 / w) can be 511.999...
        out = chunk.data_dir / "model_texture_display.jpg"
        if not _display_current(out, pages, size):
            img = cv2.resize(cv2.imread(str(pages[0]), cv2.IMREAD_COLOR), size, interpolation=cv2.INTER_AREA)
            cv2.imwrite(str(out), img, [cv2.IMWRITE_JPEG_QUALITY, 90])
        return out, texcoords
    sizes = []
    for page in pages:
        with Image.open(page) as im:
            sizes.append(im.size)  # (width, height)
    cols = int(np.ceil(np.sqrt(len(pages))))
    rows = int(np.ceil(len(pages) / cols))
    cw, ch = max(w for w, _ in sizes), max(h for _, h in sizes)
    f = min(1.0, max_side / (cols * cw), max_side / (rows * ch))
    cell_w, cell_h = max(1, int(cw * f + 1e-9)), max(1, int(ch * f + 1e-9))
    scaled = [(max(1, int(round(w * f))), max(1, int(round(h * f)))) for w, h in sizes]
    out = chunk.data_dir / "model_texture_display.jpg"
    if not _display_current(out, pages, (cols * cell_w, rows * cell_h)):
        canvas = np.full((rows * cell_h, cols * cell_w, 3), 127, np.uint8)
        flag = cv2.IMREAD_REDUCED_COLOR_4 if f <= 0.25 else cv2.IMREAD_REDUCED_COLOR_2 if f <= 0.5 else cv2.IMREAD_COLOR
        for k, page in enumerate(pages):
            img = cv2.imread(str(page), flag)
            r, c = divmod(k, cols)
            canvas[r * cell_h:r * cell_h + scaled[k][1], c * cell_w:c * cell_w + scaled[k][0]] = \
                cv2.resize(img, scaled[k], interpolation=cv2.INTER_AREA)
        cv2.imwrite(str(out), canvas, [cv2.IMWRITE_JPEG_QUALITY, 90])
    uv = np.asarray(texcoords, np.float64).reshape(-1, 3, 2).copy()
    page = np.asarray(texnumber)
    width, height = cols * cell_w, rows * cell_h
    for k in range(len(pages)):
        sel = page == k
        r, c = divmod(k, cols)
        x = uv[sel, :, 0] * scaled[k][0] + c * cell_w
        y = (1.0 - uv[sel, :, 1]) * scaled[k][1] + r * cell_h
        uv[sel, :, 0] = x / width
        uv[sel, :, 1] = 1.0 - y / height
    return out, uv.reshape(-1, 6).astype(np.float32)


def build_texture(chunk: Chunk, texture_size: int = 4096, progress: Progress | None = None,
                  skip_unreadable: bool = False, mapping: str = "auto", colour_correction: bool | None = None,
                  page_size: int = PAGE_SIZE) -> Path:
    """Texture of the model (see the module docstring for ``mapping``), written as
    ``model_texture.jpg`` with the texture coordinates in ``model.ply`` (every other property of
    the model kept); a ``texture_size`` above ``page_size`` gives several pages (see the module
    docstring). Photos that cannot be read stop the step
    (:class:`~.visibility.UnreadablePhotos`) unless ``skip_unreadable``: then the faces take
    their colours from the other photos. ``colour_correction``: gains of the photos and
    vignetting (:mod:`.colour`): None = the one measured in the chunk if any, True = measured
    now when missing, False = none."""
    from ..io.mesh_ply import read_mesh, write_mesh
    from .ortho import correction_for

    progress = as_progress(progress)
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
    build = _orthophoto_pages if mapping == "orthophoto" else _generic_pages
    atlases, uv, page = build(chunk, verts, faces, cams, texture_size, page_size, raycaster, progress.sub(0.01, 0.95),
                              correction)
    paths = [chunk.data_dir / ("model_texture.jpg" if k == 0 else f"model_texture_{k}.jpg")
             for k in range(len(atlases))]
    for old in chunk.data_dir.glob("model_texture_*.jpg"):  # pages of an earlier, larger texture
        if old not in paths:
            old.unlink()
    for atlas, path in zip(atlases, paths):
        cv2.imwrite(str(path), atlas, [cv2.IMWRITE_JPEG_QUALITY, 92])
    mesh.texcoords = uv.reshape(-1, 6).astype(np.float32)
    if len(paths) > 1:
        mesh.set_texture_files([p.name for p in paths])
        mesh.texnumber = page.astype(np.int32)
    else:
        mesh.set_texture_file(paths[0].name)
    chunk._set_product("model", write_mesh(chunk.own_product_path("model"), mesh))
    chunk._set_product("model_textured", paths[0])
    first = atlases[0]
    chunk.meta["texture"] = {"mapping": mapping, "size": [int(first.shape[1]), int(first.shape[0])],
                             "pages": len(paths), "texture_size": int(texture_size),
                             "skipped_photos": [c.label for c in bad] or None,
                             "colour_correction": correction is not None}
    pages = f", {len(paths)} pages" if len(paths) > 1 else ""
    progress(1.0, f"Texture {first.shape[1]}x{first.shape[0]} px ({mapping}{pages})")
    return paths[0]


def write_obj(path: Path, verts: np.ndarray, faces: np.ndarray, uv: np.ndarray | None, texture,
              transform: np.ndarray | None = None, pages: np.ndarray | None = None) -> Path:
    """Write an OBJ with per-face-corner UVs (``uv`` shape ``(F,3,2)``); ``texture``: the image,
    or the images of the pages with ``pages`` the page of every face (a material per page)."""
    textures = [texture] if isinstance(texture, str) else list(texture or [])
    v = verts if transform is None else verts @ transform[:3, :3].T + transform[:3, 3]
    mtl = path.with_suffix(".mtl")
    paged = uv is not None and pages is not None and len(textures) > 1
    with open(path, "w") as fh:
        if textures:
            fh.write(f"mtllib {mtl.name}\n" + ("" if paged else "usemtl material0\n"))
        np.savetxt(fh, v, fmt="v %.6f %.6f %.6f")
        if uv is not None:
            np.savetxt(fh, uv.reshape(-1, 2), fmt="vt %.6f %.6f")
            idx = np.column_stack([faces[:, 0] + 1, np.arange(len(faces)) * 3 + 1,
                                   faces[:, 1] + 1, np.arange(len(faces)) * 3 + 2,
                                   faces[:, 2] + 1, np.arange(len(faces)) * 3 + 3])
            if paged:
                for k in range(len(textures)):
                    sel = np.asarray(pages) == k
                    if sel.any():
                        fh.write(f"usemtl material{k}\n")
                        np.savetxt(fh, idx[sel], fmt="f %d/%d %d/%d %d/%d")
            else:
                np.savetxt(fh, idx, fmt="f %d/%d %d/%d %d/%d")
        else:
            np.savetxt(fh, faces + 1, fmt="f %d %d %d")
    if textures:
        mtl.write_text("".join(f"newmtl material{k}\nKa 1 1 1\nKd 1 1 1\nd 1\nillum 1\nmap_Kd {t}\n"
                               for k, t in enumerate(textures)))
    return path
