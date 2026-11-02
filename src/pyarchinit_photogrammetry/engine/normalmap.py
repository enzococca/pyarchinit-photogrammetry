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

:func:`normal_map_on_grid` is the other normal map: the one of the surface in plan, on the very
grid of the orthomosaic (red east, green north, blue up, as Metashape's), to light a drawing from
another side or to put the relief under a drawing in QGIS.
"""
from __future__ import annotations

import json
import logging

import cv2
import numpy as np

from ..core.progress import Progress, as_progress
from .texturing import _cell_layout, _fill_empty
from .visibility import sample

log = logging.getLogger(__name__)


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
              full_texcoords=None, full_texture=None, progress: Progress | None = None, full_texnumber=None,
              same_mesh: bool = False, normals: bool = True):
    """Colour texture and normal map of a light mesh from the full one (see the module
    docstring). ``full_texcoords``: ``(F, 6)`` per face of the full mesh with ``full_texture``
    (BGR image, or the images of the pages with ``full_texnumber`` the page of every face); else
    ``full_colors`` per vertex (RGB). ``same_mesh``: the light mesh is the full one, face for
    face: every texel takes the colour of its own face, without casting rays. ``normals=False``:
    the colour only (no normal map: None). Returns ``(colour BGR, normal RGB, uv (F_light, 3, 2),
    hit share)``."""
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
    lvn = _vertex_normals(lv, lf) if normals or not same_mesh else None
    fvn = _vertex_normals(fv, ff) if normals else None
    edge = float(np.median(np.linalg.norm(e1, axis=1))) if n else 1.0
    reach = 4.0 * edge
    if not same_mesh:
        scene = o3d.t.geometry.RaycastingScene()
        scene.add_triangles(o3d.core.Tensor(fv.astype(np.float32)), o3d.core.Tensor(ff.astype(np.uint32)))
    colour = np.zeros((side, side, 3), np.uint8)
    normal = np.zeros((side, side, 3), np.uint8) if normals else None
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
        if lvn is not None:
            nrm = (b[:, :, None] * lvn[lf[f_all]]).sum(1)
            nrm /= np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-12)
        # from outside the light surface inwards, then from inside outwards for what was missed
        dist = np.full(len(b), np.inf)
        prim = np.full(len(b), -1, np.int64)
        uvw = np.zeros((len(b), 2))
        if same_mesh:  # the texel on its own face: (u, v) of its barycentrics, as a ray would give them
            dist[:] = 0.0
            prim = f_all.astype(np.int64)
            uvw = b[:, 1:3]
        else:
            pts = (b[:, :, None] * lv[lf[f_all]]).sum(1)
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
        total += len(b)
        hits += int(hit.sum())
        rows = cy[f_all] * s + local[p_all, 1]
        cols = cx[f_all] * s + local[p_all, 0]
        if hit.any():
            hp, hu = prim[hit], uvw[hit]
            w = np.column_stack([1 - hu[:, 0] - hu[:, 1], hu[:, 0], hu[:, 1]])  # barycentrics of the hit
            if normals:
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
    colour = _fill_empty(colour, valid)
    if normals:
        flat = np.zeros_like(normal)
        flat[:] = (128, 128, 255)
        normal = _fill_empty(np.where(valid[..., None], normal, flat), valid)
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


NORMAL_TILE = 1024  # rays per side of a tile of the normal map cast on the model
NORMAL_BLOCK_ROWS = 1024  # rows of the orthomosaic grid computed and written at a time
NORMAL_BLOCK_CELLS = 16_777_216  # at most this many cells a block: a very wide grid takes fewer rows
DEM_HALO = 3  # rows of DEM above and below a block: Horn's kernel and the nearest-height fill at its edges
STAND_IN_CRS = "EPSG:3857"  # rasters of a local chunk: the same frame on both sides, any projected CRS does


def _block_rows(width: int) -> int:
    """Rows of a block of the grid: :data:`NORMAL_BLOCK_ROWS`, fewer on a grid so wide that the
    block would pass :data:`NORMAL_BLOCK_CELLS` (whole tiles of 256 rows, at least one)."""
    rows = max(256, NORMAL_BLOCK_CELLS // max(int(width), 1) // 256 * 256)
    return max(1, min(NORMAL_BLOCK_ROWS, rows))


def _grid_centres(transform, rows: np.ndarray, cols: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """World x, y of the centres of the cells (rows, cols) of a raster with ``transform``."""
    c, r = cols + 0.5, rows + 0.5
    return (transform.c + transform.a * c + transform.b * r, transform.f + transform.d * c + transform.e * r)


class _DemNormals:
    """Unit normals on blocks of rows of the grid from the DEM resampled on them (bilinear), by
    Horn's kernel; NaN where the DEM has no height. A raster without a CRS (a local chunk) is in
    the frame of the other one; both without one, the same stand-in CRS on both sides."""

    def __init__(self, chunk, transform, shape: tuple[int, int], crs):
        import rasterio

        dem = chunk.product_path("dem")
        if dem is None or not dem.exists():
            raise RuntimeError("build the DEM first (or the model: source='model')")
        if transform.b or transform.d:
            raise RuntimeError("the normal map from the DEM needs a north-up orthomosaic (source='model' works)")
        self.transform, self.shape = transform, shape
        self.ds = rasterio.open(dem)
        src, dst = self.ds.crs or None, crs or None
        if src is None and dst is None:
            src = dst = STAND_IN_CRS
        elif src is None or dst is None:
            src = dst = src or dst
        self.src_crs, self.dst_crs = src, dst
        self.any = False  # a cell of the grid with a height

    def close(self):
        self.ds.close()

    def block(self, r0: int, r1: int) -> np.ndarray:
        import rasterio
        from rasterio.warp import Resampling, reproject
        from rasterio.windows import Window, transform as window_transform
        from scipy import ndimage

        h, w = self.shape
        a0, a1 = max(0, r0 - DEM_HALO), min(h, r1 + DEM_HALO)
        z = np.full((a1 - a0, w), np.nan, np.float32)
        reproject(rasterio.band(self.ds, 1), z, src_nodata=self.ds.nodata, src_crs=self.src_crs,
                  dst_transform=window_transform(Window(0, a0, w, a1 - a0), self.transform), dst_crs=self.dst_crs,
                  dst_nodata=np.nan, resampling=Resampling.bilinear)
        valid = np.isfinite(z)
        inner = slice(r0 - a0, r1 - a0)
        if not valid[inner].any():
            return np.full((r1 - r0, w, 3), np.nan)
        self.any = True
        # the cells without a height take the nearest one: the kernel then reaches the edge of the surface
        _dist, (ri, ci) = ndimage.distance_transform_edt(~valid, return_indices=True)
        pad = np.pad(z[ri, ci].astype(np.float64), 1, mode="edge")
        del ri, ci, _dist
        pad = pad[r0 - a0:r1 - a0 + 2]  # the rows of the block and one on each side
        tl, t, tr = pad[:-2, :-2], pad[:-2, 1:-1], pad[:-2, 2:]
        l, r = pad[1:-1, :-2], pad[1:-1, 2:]
        bl, b, br = pad[2:, :-2], pad[2:, 1:-1], pad[2:, 2:]
        nx = -((tr + 2 * r + br) - (tl + 2 * l + bl)) / 8.0 / self.transform.a  # per column (east when a > 0)
        ny = -((bl + 2 * b + br) - (tl + 2 * t + tr)) / 8.0 / self.transform.e  # per row (south when e < 0)
        length = np.sqrt(nx * nx + ny * ny + 1.0)
        normals = np.stack([nx / length, ny / length, 1.0 / length], axis=-1)
        normals[~valid[inner]] = np.nan
        return normals


class _ModelNormals:
    """Unit normals on blocks of rows of the grid from the model: a ray straight down on every
    cell centre, the normal of the face it meets (Open3D), tiles of at most ``NORMAL_TILE`` rays
    a side; NaN where no ray hits."""

    def __init__(self, chunk, transform, shape: tuple[int, int]):
        try:
            import open3d as o3d
        except ImportError as exc:
            raise RuntimeError("the normal map from the model needs Open3D (source='dem' works without it)") from exc

        from ..io.mesh_ply import read_mesh

        path = chunk.product_path("model")
        if path is None or not path.exists():
            raise RuntimeError("build the model first (or the DEM: source='dem')")
        mesh = read_mesh(path)
        faces = np.asarray(mesh.faces, np.int64)
        if not len(faces):
            raise RuntimeError("the model has no faces")
        world = chunk.to_world(np.asarray(mesh.xyz, np.float64))
        self.offset = world.mean(axis=0)  # float32 rays and vertices: around the model, not at the CRS origin
        local = (world - self.offset).astype(np.float32)
        self.top = float(local[:, 2].max()) + 1.0
        self.scene = o3d.t.geometry.RaycastingScene()
        self.scene.add_triangles(o3d.core.Tensor(local), o3d.core.Tensor(faces.astype(np.uint32)))
        self.o3d, self.transform, self.shape = o3d, transform, shape
        self.any = False

    def close(self):
        pass

    def block(self, r0: int, r1: int) -> np.ndarray:
        w = self.shape[1]
        normals = np.full((r1 - r0, w, 3), np.nan, np.float32)
        step = max(1, NORMAL_TILE * NORMAL_TILE // (r1 - r0))  # columns of a tile: at most NORMAL_TILE^2 rays
        for c0 in range(0, w, step):
            c1 = min(c0 + step, w)
            rows, cols = np.meshgrid(np.arange(r0, r1), np.arange(c0, c1), indexing="ij")
            x, y = _grid_centres(self.transform, rows.ravel().astype(np.float64), cols.ravel().astype(np.float64))
            rays = np.zeros((x.size, 6), np.float32)
            rays[:, 0], rays[:, 1] = x - self.offset[0], y - self.offset[1]
            rays[:, 2], rays[:, 5] = self.top, -1.0
            hit = self.scene.cast_rays(self.o3d.core.Tensor(rays))
            ok = np.isfinite(hit["t_hit"].numpy())
            n = hit["primitive_normals"].numpy()
            n[n[:, 2] < 0] *= -1  # every normal upwards
            n[~ok] = np.nan
            normals[:, c0:c1] = n.reshape(r1 - r0, c1 - c0, 3)
            self.any = self.any or bool(ok.any())
        return normals


def _encode(normals: np.ndarray, opaque: np.ndarray) -> np.ndarray:
    """The RGBA uint8 bands (4, rows, cols) of a block: ``(n + 1) / 2 * 255`` where the surface
    has a normal under an opaque pixel (``opaque``), 0 elsewhere (the alpha too)."""
    valid = np.isfinite(normals).all(axis=-1) & opaque
    out = np.zeros((4,) + valid.shape, np.uint8)
    for k in range(3):
        band = normals[..., k] * 0.5 + 0.5
        band *= 255
        np.rint(band, out=band)
        out[k][valid] = np.clip(band[valid], 0, 255)
    out[3][valid] = 255
    return out


class _Uncovered(RuntimeError):
    """The surface (DEM or model) has a normal on no cell of the grid."""


def _write_normal_map(normals, source: str, ortho, out, transform, shape, crs, progress: Progress,
                      empty: bool) -> int:
    """The normal map of ``normals`` written to ``out`` block by block (a ``.part`` file put in place at the
    end, removed on a failure); ``normals`` closed. A surface covering no cell raises :class:`_Uncovered`
    unless ``empty``: then the map is written all transparent, with a warning. Returns the opaque cells."""
    import rasterio
    from rasterio.windows import Window

    from ..io.files import replace_file
    from .dem import finish_geotiff, geotiff_profile

    h, w = shape
    progress(0.0, f"Mappa delle normali dal {'modello' if source == 'model' else 'DEM'}")
    part = out.with_name(out.stem + ".part" + out.suffix)
    rows = _block_rows(w)
    blocks = [(r0, min(r0 + rows, h)) for r0 in range(0, h, rows)]
    opaque_cells = 0
    try:
        profile = geotiff_profile(h, w, 4, np.uint8, transform, crs, photometric="RGB")
        with rasterio.open(ortho) as o, rasterio.open(part, "w", **profile) as dst:
            for k, (r0, r1) in enumerate(blocks):
                progress(0.05 + 0.85 * k / len(blocks), f"Mappa delle normali: {k + 1} di {len(blocks)} blocchi")
                window = Window(0, r0, w, r1 - r0)
                rgba = _encode(normals.block(r0, r1), o.dataset_mask(window=window) > 0)
                dst.write(rgba, window=window)
                opaque_cells += int(np.count_nonzero(rgba[3]))
            if not normals.any:
                what = "the DEM" if source == "dem" else "the model"
                if not empty:
                    raise _Uncovered(f"{what} does not cover the orthomosaic")
                log.warning("normal map: %s does not cover the orthomosaic, the map is transparent", what)
            progress(0.9, "Mappa delle normali: livelli ridotti")
            finish_geotiff(dst)
        replace_file(part, out)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    finally:
        normals.close()
    return opaque_cells


def normal_map_on_grid(chunk, source: str = "auto", path=None, progress: Progress | None = None):
    """The normal map of the surface on exactly the grid of the orthomosaic (width, height,
    transform and CRS read from it, none for a local chunk): an RGBA uint8 GeoTIFF with R = east,
    G = north, B = up of the unit normal (``(n + 1) / 2 * 255``) and A = 255 where there is a
    surface under an opaque pixel of the orthomosaic (its alpha band, or its mask: nodata or an
    internal mask); RGB is 0 where A is 0. ``source``: ``"dem"`` (the DEM resampled on the grid,
    Horn's kernel), ``"model"`` (a ray straight down on every cell, the normal of the face it
    meets; Open3D) or ``"auto"`` (the model when the chunk has one, Open3D loads, the model can be
    read and covers some cell; else the DEM). A model asked for by name that covers no cell gives a
    map transparent everywhere (with a warning); a DEM that covers none raises. The grid is made and written in blocks of rows (:data:`NORMAL_BLOCK_ROWS`), so the memory is
    the one of a block whatever the size of the orthomosaic. Written to ``path`` (default the
    product ``normal_map`` of the chunk, ``normal_map.tif``); the path returned."""
    from pathlib import Path

    import rasterio

    progress = as_progress(progress)
    if source not in ("auto", "dem", "model"):
        raise ValueError(f"source: 'auto', 'dem' or 'model', not {source!r}")
    ortho = chunk.orthomosaic_path
    if ortho is None or not ortho.exists():
        raise RuntimeError("build the orthomosaic first: the normal map takes its grid")
    with rasterio.open(ortho) as o:
        transform, shape, crs = o.transform, (o.height, o.width), o.crs
    if crs and crs.is_geographic:
        raise RuntimeError("the normal map needs an orthomosaic in a projected CRS (metres), not in degrees")
    h, w = shape
    auto = source == "auto"
    normals = None
    if source in ("auto", "model"):
        model = chunk.product_path("model")
        if source == "model" or (model is not None and model.exists()):
            try:
                normals = _ModelNormals(chunk, transform, shape)
                source = "model"
            except (RuntimeError, ValueError, OSError):  # ValueError, OSError: a model file that cannot be read
                if not auto:
                    raise
                log.warning("normal map: the model cannot be used, the DEM instead", exc_info=True)
    if normals is None:
        source = "dem"
        normals = _DemNormals(chunk, transform, shape, crs)
    out = Path(path) if path is not None else chunk.own_product_path("normal_map")
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        # a model that covers no cell: with "auto" the DEM instead, asked for by name a transparent map
        opaque_cells = _write_normal_map(normals, source, ortho, out, transform, shape, crs, progress,
                                         empty=source == "model" and not auto)
    except _Uncovered:
        if not (auto and source == "model"):
            raise
        log.warning("normal map: the model does not cover the orthomosaic, the DEM instead")
        source = "dem"
        opaque_cells = _write_normal_map(_DemNormals(chunk, transform, shape, crs), source, ortho, out, transform,
                                         shape, crs, progress, empty=False)
    if path is None:
        chunk._set_product("normal_map", out)
        chunk.meta["normal_map"] = {"source": source}
    progress(1.0, f"Mappa delle normali: {w} x {h} pixel, {100 * opaque_cells / max(h * w, 1):.1f} % con superficie")
    return out
