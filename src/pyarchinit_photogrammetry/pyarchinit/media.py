"""Media registration with PyArchInit's conventions (``media_table``,
``media_thumb_table``, ``media_to_entity_table``, thumbnails and resized copies).

* thumbnail: ``THUMB_PATH/{id}_{name}_thumb.png`` (max 150x150, 100 dpi)
* resized:   ``THUMB_RESIZE/{id}_{name}.png`` (max 2008x1417, 300 dpi)
* 3D models: thumbnail ``{id}_{name}_3d_thumb.png`` and the model copied to
  ``THUMB_RESIZE/{id}_{name}.{ext}`` (as the US form does)
* GeoTIFF rasters (orthophotos, DEMs) get their previews rendered with rasterio,
  so very large or float rasters never go through PIL.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
from PIL import Image

from .config import is_remote

THUMB_SIZE = (150, 150)
RESIZE_SIZE = (2008, 1417)
MODEL_EXT = {".obj", ".ply", ".stl", ".fbx", ".3ds", ".glb", ".gltf"}
ENTITY_TABLES = {  # entity_type -> (table, id column)
    "US": ("us_table", "id_us"),
    "REPERTO": ("inventario_materiali_table", "id_invmat"),
    "CERAMICA": ("pottery_table", "id_rep"),
    "TOMBA": ("tomba_table", "id_tomba"),
    "STRUTTURA": ("struttura_table", "id_struttura"),
    "UT": ("ut_table", "id_ut"),
}


def raster_preview(path: Path, max_size: tuple[int, int]) -> Image.Image:
    """RGB preview of a GeoTIFF (RGB(A) imagery or single-band elevation)."""
    import rasterio

    with rasterio.open(path) as src:
        f = max(1.0, src.width / max_size[0], src.height / max_size[1])
        shape = (src.count, max(1, int(src.height / f)), max(1, int(src.width / f)))
        arr = src.read(out_shape=shape)
        nodata = src.nodata
    if arr.shape[0] >= 3 and arr.dtype == np.uint8:
        rgb = np.moveaxis(arr[:3], 0, -1)
        if arr.shape[0] == 4:
            rgb = np.where(arr[3][..., None] > 0, rgb, 255)
        return Image.fromarray(rgb.astype(np.uint8))
    a = arr[0].astype(float)
    if nodata is not None:
        a[a == nodata] = np.nan
    lo, hi = np.nanpercentile(a, [2, 98]) if np.isfinite(a).any() else (0, 1)
    t = np.nan_to_num(np.clip((a - lo) / max(hi - lo, 1e-9), 0, 1))
    import cv2

    rgb = cv2.applyColorMap((t * 255).astype(np.uint8), cv2.COLORMAP_VIRIDIS)[..., ::-1].copy()
    rgb[~np.isfinite(a)] = 255
    return Image.fromarray(rgb)


def model_preview(path: Path, size: int = 600, max_faces: int = 150_000) -> Image.Image:
    """Software render of a mesh: orthographic view along its thinnest axis,
    painter's algorithm, vertex/texture colours with Lambert shading."""
    import cv2
    import trimesh

    mesh = trimesh.load(str(path), process=False, force="mesh")
    v = np.asarray(mesh.vertices, float)
    f = np.asarray(mesh.faces)
    try:
        colors = np.asarray(mesh.visual.to_color().vertex_colors)[:, :3].astype(float)
    except Exception:
        colors = np.full((len(v), 3), 190.0)
    if len(f) > max_faces:
        f = f[np.random.default_rng(0).choice(len(f), max_faces, replace=False)]
    c = v - v.mean(0)
    _, _, vt = np.linalg.svd(c, full_matrices=False)
    view = vt[2] if (c @ vt[2]).max() > 0 else -vt[2]
    basis = np.vstack([vt[0], np.cross(view, vt[0]), view])
    p = c @ basis.T
    span = np.ptp(p[:, :2], axis=0).max() or 1.0
    xy = (p[:, :2] - p[:, :2].min(0)) / span * (size - 20) + 10
    xy[:, 1] = size - 1 - xy[:, 1]
    tri = v[f]
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    shade = 0.35 + 0.65 * np.abs(n @ view)
    face_col = colors[f].mean(1) * shade[:, None]
    order = np.argsort(p[f, 2].mean(1))  # far to near
    img = np.full((size, size, 3), 255, np.uint8)
    pts = np.round(xy[f] * 16).astype(np.int32)
    for i in order:
        cv2.fillConvexPoly(img, pts[i], tuple(float(x) for x in face_col[i]), lineType=cv2.LINE_AA, shift=4)
    return Image.fromarray(img)


def _save_preview(img: Image.Image, out: str, max_size, dpi) -> None:
    im = img.copy()
    im.thumbnail(max_size, Image.LANCZOS)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    im.save(out, dpi=(dpi, dpi))


def register_media(db, path: str | Path, mediatype: str | None = None, descrizione: str = "",
                   tags: str = "['fotogrammetria']", links: list[tuple[str, int]] | None = None,
                   copy_to: str | Path | None = None, overwrite: bool = False, thumb_dir: str | None = None,
                   resize_dir: str | None = None) -> int:
    """Register a file as PyArchInit media and link it to entities.

    ``links``: ``[(entity_type, id_entity)]`` e.g. ``[("US", 12)]``.
    ``copy_to``: folder where the original is copied (the stored path is the copy).
    A file already registered (same ``filepath``) raises ``FileExistsError`` unless
    ``overwrite=True``: then the file and its previews are refreshed and the existing
    ``id_media`` is reused (missing links are added). Everything is written in one
    transaction: if a preview fails nothing is left in the database.
    Returns ``id_media``.
    """
    src = Path(path)
    target = Path(copy_to) / src.name if copy_to else src
    ext = target.suffix.lower()
    if mediatype is None:
        mediatype = "3d_model" if ext in MODEL_EXT else "image"
    cfg = db.config
    thumb_dir = thumb_dir or (cfg.thumb_dir if cfg else str(target.parent / "thumbs") + "/")
    resize_dir = resize_dir or (cfg.resize_dir if cfg else str(target.parent / "resize") + "/")
    if is_remote(thumb_dir) or is_remote(resize_dir):
        raise NotImplementedError("remote media storage is handled by PyArchInit itself; use a local path")
    existing = db.query("media_table", filepath=str(target))
    if existing and not overwrite:
        raise FileExistsError(f"{target} is already media {existing[0]['id_media']} (use overwrite=True)")
    if copy_to:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.resolve() != src.resolve():
            shutil.copy2(src, target)
    name = target.stem
    written: list[str] = []
    try:
        with db.begin() as conn:
            if existing:
                id_media = int(existing[0]["id_media"])
            else:
                id_media = db.insert(conn, "media_table", {
                    "mediatype": mediatype, "filename": name, "filetype": ext.lstrip("."), "filepath": str(target),
                    "descrizione": descrizione, "tags": tags}, id_column="id_media")
            if mediatype == "3d_model":
                thumb_name = f"{id_media}_{name}_3d_thumb.png"
                resize_name = f"{id_media}_{name}{ext}"
                _save_preview(model_preview(target), thumb_dir + thumb_name, THUMB_SIZE, 100)
                Path(resize_dir).mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, resize_dir + resize_name)
                written += [thumb_dir + thumb_name, resize_dir + resize_name]
                for extra in (target.with_suffix(".mtl"), target.with_name(target.stem + "_texture.jpg")):
                    if extra.exists():
                        shutil.copy2(extra, resize_dir + extra.name)
            else:
                thumb_name = f"{id_media}_{name}_thumb.png"
                resize_name = f"{id_media}_{name}.png"
                if ext in (".tif", ".tiff"):
                    prev = raster_preview(target, RESIZE_SIZE)
                else:
                    with Image.open(target) as im:
                        prev = im.convert("RGB")
                _save_preview(prev, thumb_dir + thumb_name, THUMB_SIZE, 100)
                _save_preview(prev, resize_dir + resize_name, RESIZE_SIZE, 300)
                written += [thumb_dir + thumb_name, resize_dir + resize_name]
            if not db.query("media_thumb_table", id_media=id_media) or not existing:
                db.insert(conn, "media_thumb_table", {
                    "id_media": id_media, "mediatype": mediatype, "media_filename": name,
                    "media_thumb_filename": thumb_name, "filetype": ext.lstrip("."), "filepath": thumb_name,
                    "path_resize": resize_name}, id_column="id_media_thumb")
            linked = {(r["entity_type"], int(r["id_entity"])) for r in db.query("media_to_entity_table", id_media=id_media)}
            for entity_type, id_entity in links or []:
                if (entity_type, int(id_entity)) in linked:
                    continue
                table = ENTITY_TABLES.get(entity_type, (entity_type.lower() + "_table",))[0]
                db.insert(conn, "media_to_entity_table", {
                    "id_entity": int(id_entity), "entity_type": entity_type, "table_name": table,
                    "id_media": id_media, "filepath": str(target), "media_name": name}, id_column="id_mediaToEntity")
    except Exception:
        if not existing:
            for f in written:
                Path(f).unlink(missing_ok=True)
        raise
    return id_media


def link_media(db, id_media: int, entity_type: str, id_entity: int) -> None:
    rec = db.query("media_table", id_media=id_media)
    if not rec:
        raise KeyError(f"media {id_media} not found")
    table = ENTITY_TABLES.get(entity_type, (entity_type.lower() + "_table",))[0]
    with db.begin() as conn:
        db.insert(conn, "media_to_entity_table", {
            "id_entity": int(id_entity), "entity_type": entity_type, "table_name": table, "id_media": id_media,
            "filepath": rec[0]["filepath"], "media_name": rec[0]["filename"]}, id_column="id_mediaToEntity")
