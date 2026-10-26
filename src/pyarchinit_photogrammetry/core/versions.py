"""Versions of the products of a chunk, to compare them.

The dense cloud, the model, the DEM and the orthomosaic can be built again while the earlier
results are kept. The *active* version is the product every tool uses (``chunk.products``,
the usual files of the chunk); the others are kept in ``<chunk data>/versions/<product>/<id>/``
and listed in ``chunk.meta["versions"]`` with their name, date and the parameters they were
built with. A version can be shown, made active (the active one takes its place among the
versions), renamed or deleted; two DEMs can be subtracted.
"""
from __future__ import annotations

import shutil
import time
from datetime import datetime
from pathlib import Path

PRODUCTS = {"point_cloud": "Nuvola densa", "model": "Modello", "dem": "DEM", "orthomosaic": "Ortofoto"}
PARAMS = {"point_cloud": "dense", "model": "model", "dem": "dem", "orthomosaic": "orthomosaic"}  # chunk.meta keys
FILES = {"point_cloud": "point_cloud.ply", "model": "model.ply", "dem": "dem.tif", "orthomosaic": "orthomosaic.tif"}


_translator = None  # the interface's: tr(text, *args); None in scripts and the command line (Italian)


def set_translator(translate) -> None:
    """The names given here to versions are written in the language of the interface: the window
    passes its translation, ``translate(text, *args)``, a template filled with ``args``."""
    global _translator
    _translator = translate


def label(text: str, *args) -> str:
    """A name or a part of one in the language of the interface (Italian without a window)."""
    if _translator is not None:
        return _translator(text, *args)
    return text.format(*args) if args else text


def _check(product: str) -> None:
    if product not in PRODUCTS:
        raise ValueError(f"no versions for {product!r}: {', '.join(PRODUCTS)}")


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except ValueError:
        return False


def active_files(chunk, product: str) -> dict[str, Path]:
    """Files of the active version: ``role -> path`` (the textured model is a folder)."""
    _check(product)
    main = chunk.product_path(product)
    if main is None or not main.exists():
        return {}
    files = {product: main}
    if product == "model":
        preview = chunk.data_dir / "model_preview.ply"
        if preview.exists():
            files["preview"] = preview
        textured = chunk.product_path("model_textured")
        if textured is not None and textured.exists():
            if textured.suffix.lower() == ".obj":  # a texture of version 0.5 and earlier: a folder with its OBJ
                files["textured"] = textured.parent
            else:  # the texture image(s) of the model (texture coordinates inside model.ply), page by page
                from ..engine.texturing import texture_pages

                for k, page in enumerate(texture_pages(chunk) or [textured]):
                    if page.exists():
                        files["texture" if k == 0 else f"texture_{k}"] = page
    return files


def _describe(chunk, product: str) -> str:
    """Short description of how the active product was built (for a default name)."""
    meta = chunk.meta.get(PARAMS[product]) or {}
    bits = []
    if product == "point_cloud":  # where the active cloud comes from (the depth-map settings may be of a later try)
        if chunk.meta.get("point_cloud_source"):
            bits.append(label(str(chunk.meta["point_cloud_source"])))
        elif meta.get("quality"):
            bits.append(label("qualita' {0}", meta["quality"]))
    elif product == "model" and meta.get("source"):
        source = meta["source"]
        bits.append(label("importato da {0}", meta.get("file", label("un file"))) if source == "import" else
                    label({"depth_maps": "mappe di profondita'", "point_cloud": "nuvola densa",
                           "tie_points": "punti di legame"}.get(source, source)))
    if meta.get("classes"):
        bits.append(label("classi {0}", ", ".join(str(c) for c in meta["classes"])))
    if product == "orthomosaic" and meta.get("hidden_classes"):
        bits.append(label("senza classi {0}", ", ".join(str(c) for c in meta["hidden_classes"])))
    return "; ".join(bits)


def active_info(chunk, product: str) -> dict:
    """Name and date of the active version (named after the date when never named)."""
    _check(product)
    info = dict((chunk.meta.get("active_versions") or {}).get(product) or {})
    if not info:
        main = chunk.product_path(product)
        t = main.stat().st_mtime if main is not None and main.exists() else time.time()
        info["created"] = datetime.fromtimestamp(t).isoformat(timespec="seconds")
    if not info.get("name"):
        when = datetime.fromisoformat(info["created"]).strftime("%d/%m %H:%M")
        desc = _describe(chunk, product)
        info["name"] = label("{0} del {1}", label(PRODUCTS[product]), when) + (f" ({desc})" if desc else "")
    return info


def set_active_name(chunk, product: str, name: str | None = None) -> dict:
    """Name the active version (built now): ``chunk.meta["active_versions"][product]``."""
    _check(product)
    info = {"created": datetime.now().isoformat(timespec="seconds")}
    if name:
        info["name"] = name
    chunk.meta.setdefault("active_versions", {})[product] = info
    return active_info(chunk, product)


def versions(chunk, product: str) -> list[dict]:
    """The kept versions of a product (the active one excluded), oldest first."""
    _check(product)
    return list((chunk.meta.get("versions") or {}).get(product, []))


def _entry(chunk, product: str, vid: str) -> dict:
    for e in versions(chunk, product):
        if e["id"] == vid:
            return e
    raise KeyError(f"{product}: no version {vid}")


def version_file(chunk, product: str, vid: str) -> Path:
    """Main file of a kept version (cloud, mesh, DEM or orthomosaic)."""
    e = _entry(chunk, product, vid)
    return chunk.data_dir / e["files"][product]


def keep_active(chunk, product: str, move: bool = False) -> dict | None:
    """Put the active version among the kept ones (copied, or moved with ``move`` when its
    files are inside the chunk folder: a file of another program, like an orthomosaic
    exported by Metashape, is always copied). Returns the new entry (None without product)."""
    files = active_files(chunk, product)
    if not files:
        return None
    info = active_info(chunk, product)
    vid = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    folder = chunk.data_dir / "versions" / product / vid
    folder.mkdir(parents=True)
    rel = {}
    for role, src in files.items():
        dst = folder / src.name
        if move and _inside(src, chunk.data_dir):
            shutil.move(str(src), str(dst))
        elif src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        rel[role] = str(dst.relative_to(chunk.data_dir))
    if "textured" in rel:  # the OBJ inside the folder
        obj = chunk.product_path("model_textured")
        rel["model_textured"] = str((folder / files["textured"].name / obj.name).relative_to(chunk.data_dir))
    entry = {"id": vid, "name": info["name"], "created": info["created"],
             "params": dict(chunk.meta.get(PARAMS[product]) or {}), "files": rel}
    chunk.meta.setdefault("versions", {}).setdefault(product, []).append(entry)
    if move:
        chunk.products.pop(product, None)
        if product == "model":
            chunk.products.pop("model_textured", None)
    return entry


def restore(chunk, product: str, entry: dict) -> None:
    """Undo :func:`keep_active` with ``move`` (the build of the new version failed)."""
    _activate_files(chunk, product, entry)
    chunk.meta["versions"][product] = [e for e in versions(chunk, product) if e["id"] != entry["id"]]
    shutil.rmtree(chunk.data_dir / "versions" / product / entry["id"], ignore_errors=True)


def _activate_files(chunk, product: str, entry: dict) -> None:
    for role, rel in entry["files"].items():
        src = chunk.data_dir / rel
        if role == product:
            dst = chunk.data_dir / FILES[product]
        elif role == "preview":
            dst = chunk.data_dir / "model_preview.ply"
        elif role == "textured":
            dst = chunk.data_dir / "textured"
        elif role == "texture" or role.startswith("texture_"):  # the first page is the product
            dst = chunk.data_dir / Path(rel).name
        else:
            continue
        if dst.is_dir():
            shutil.rmtree(dst)
        elif dst.exists():
            dst.unlink()
        shutil.move(str(src), str(dst))
        if role == product:
            chunk._set_product(product, dst)
        elif role == "textured" and "model_textured" in entry["files"]:
            chunk._set_product("model_textured", dst / Path(entry["files"]["model_textured"]).name)
        elif role == "texture":
            chunk._set_product("model_textured", dst)
    if entry.get("params"):
        chunk.meta[PARAMS[product]] = dict(entry["params"])
    chunk.meta.setdefault("active_versions", {})[product] = {"name": entry["name"], "created": entry["created"]}


def activate(chunk, product: str, vid: str) -> dict | None:
    """Make a kept version the active one; the active one becomes a kept version (returned)."""
    entry = _entry(chunk, product, vid)
    previous = keep_active(chunk, product, move=True)
    _activate_files(chunk, product, entry)
    chunk.meta["versions"][product] = [e for e in versions(chunk, product) if e["id"] != vid]
    shutil.rmtree(chunk.data_dir / "versions" / product / vid, ignore_errors=True)
    return previous


def rename(chunk, product: str, vid: str | None, name: str) -> None:
    """Rename a kept version (``vid``) or the active one (``vid=None``)."""
    if vid is None:
        info = active_info(chunk, product)
        info["name"] = name
        chunk.meta.setdefault("active_versions", {})[product] = info
    else:
        _entry(chunk, product, vid)["name"] = name


def delete(chunk, product: str, vid: str) -> None:
    """Delete a kept version and its files."""
    _entry(chunk, product, vid)
    chunk.meta["versions"][product] = [e for e in versions(chunk, product) if e["id"] != vid]
    shutil.rmtree(chunk.data_dir / "versions" / product / vid, ignore_errors=True)


def dem_difference(chunk, vid: str, out: str | Path | None = None) -> Path:
    """Active DEM minus a kept DEM version (positive where the active one is higher, e.g. a
    surface model minus a terrain model gives the height of walls and vegetation), on the
    grid of the active DEM; GeoTIFF float32 (``nodata`` where either is empty)."""
    import numpy as np
    import rasterio
    from rasterio.warp import Resampling, reproject

    active = chunk.product_path("dem")
    if active is None or not active.exists():
        raise RuntimeError("the chunk has no DEM")
    other = version_file(chunk, "dem", vid)
    out = Path(out) if out else other.with_name("differenza.tif")
    with rasterio.open(active) as a, rasterio.open(other) as b:
        za = a.read(1, masked=True).astype("float32").filled(np.nan)
        zb = np.full(za.shape, np.nan, "float32")
        reproject(b.read(1, masked=True).astype("float32").filled(np.nan), zb, src_transform=b.transform,
                  src_crs=b.crs or a.crs, dst_transform=a.transform, dst_crs=a.crs or b.crs,
                  src_nodata=np.nan, dst_nodata=np.nan, resampling=Resampling.bilinear)
        diff = np.where(np.isfinite(za) & np.isfinite(zb), za - zb, np.nan).astype("float32")
        profile = a.profile.copy()
    nodata = -32767.0
    profile.update(dtype="float32", count=1, nodata=nodata, BIGTIFF="IF_SAFER")
    with rasterio.open(out, "w", **profile) as dst:
        dst.write(np.where(np.isfinite(diff), diff, nodata).astype("float32"), 1)
    return out


__all__ = ["PRODUCTS", "active_files", "active_info", "set_active_name", "versions", "version_file", "keep_active",
           "restore", "activate", "rename", "delete", "dem_difference"]
