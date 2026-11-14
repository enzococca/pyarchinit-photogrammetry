"""Voids of a DEM interpolated: the holes without heights inside the surveyed ground.

A DEM keeps holes where the cloud had no points: a puddle, a shadow, a gap of the reconstruction. Contour lines
and volumes stop at them. :func:`fill_voids` gives the holes up to a chosen size new heights from their edges -
the smooth surface that joins the edge without bumps (the harmonic interpolation, solution of Laplace's equation:
a plane stays a plane and no height goes above or below those of the edge). Only holes enclosed by heights are filled: the empty ground around the survey and the bays open
to it are never filled (no extrapolation outside the outer boundary of the data), and the holes larger than the
limits stay empty.

The cells that get an interpolated height are kept in a mask beside the DEM (a GeoTIFF of bytes on the same
grid: 0 measured or empty, :data:`GAP` interpolated between the points when the DEM was built, :data:`VOID`
a void filled here), so that the readouts, the exports and the reports can say which heights are interpolated.
The DEM and its mask carry the same identifier (GeoTIFF tag :data:`TAG`): a DEM built, edited by another tool,
restored from a version or by *Annulla* finds its own mask, or none, never the mask of another DEM.

The existing DEM is read and written window by window (only a mask of bytes and the labels of the empty cells
are kept for the whole grid), so a large DEM stays within memory. Not to be confused with ``fill_holes`` of the
mesh (``ppg process --fill-holes``, the holes of the height-field model) nor with the small holes of the
orthomosaic.
"""
from __future__ import annotations

import datetime
import uuid
from pathlib import Path

import numpy as np

from ..core.progress import Progress, as_progress

TAG = "PPG_INTERPOLATED"  # GeoTIFF tag: the identifier shared by a DEM and its mask
MASK_PREFIX = "dem_interpolated_"
GAP, VOID = 1, 2  # values of the mask: interpolated between the points of the build, a void filled
MAX_SOLVE = 4_000_000  # cells of one hole above which it is left empty (the solver would take gigabytes)
TILE = 1024  # holes are filled in groups whose windows start in the same tile of this many cells
STRIP = 1024  # rows read at a time to find the empty cells


# ------------------------------------------------------------------------------------------ finding the voids
def _labels(empty: np.ndarray):
    """The connected groups (8 neighbours) of empty cells; those that reach the edge of the grid are the ground
    outside the survey. Returns ``(labels, ids of the inner holes)``."""
    from scipy import ndimage

    lab, n = ndimage.label(empty, structure=np.ones((3, 3), bool))
    if n == 0:
        return lab, np.zeros(0, np.int64)
    edge = np.unique(np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]]))
    inner = np.setdiff1d(np.arange(1, n + 1), edge)
    return lab, inner


def _width_cells(hole: np.ndarray) -> float:
    """The width of the widest part of a hole, in cells: twice the distance from its innermost cell to its edge,
    less one (a hole one cell wide is one cell wide)."""
    from scipy import ndimage

    d = ndimage.distance_transform_edt(np.pad(hole, 1))
    return float(max(2.0 * d.max() - 1.0, 1.0))


def plan_voids(empty: np.ndarray, resolution: float, max_width: float | None = None,
               max_area: float | None = None):
    """The holes of ``empty`` (True where the DEM has no height) inside the data that fit the limits: ``max_width``
    (m, the widest part) and ``max_area`` (m2); None = no limit. Returns ``(labels, holes, skipped)``: ``holes`` a
    list of ``(id, slices of its box, cells, width m)``; ``skipped`` the inner holes left empty, as
    ``{"count", "cells"}``."""
    from scipy import ndimage

    lab, inner = _labels(empty)
    holes, skipped_n, skipped_cells = [], 0, 0
    if not len(inner):
        return lab, holes, {"count": 0, "cells": 0}
    boxes = ndimage.find_objects(lab)
    sizes = np.bincount(lab.ravel())
    cell_area = resolution * resolution
    for i in inner:
        sl, n = boxes[i - 1], int(sizes[i])
        ok = n <= MAX_SOLVE and (max_area is None or n * cell_area <= max_area * (1 + 1e-9))
        width = None
        if ok:
            width = _width_cells(lab[sl] == i) * resolution
            ok = max_width is None or width <= max_width * (1 + 1e-9)
        if ok:
            holes.append((int(i), sl, n, float(width)))
        else:
            skipped_n += 1
            skipped_cells += n
    return lab, holes, {"count": skipped_n, "cells": skipped_cells}


# ------------------------------------------------------------------------------------------ filling one hole
def harmonic_fill(z: np.ndarray, hole: np.ndarray) -> np.ndarray:
    """Heights for the cells of ``hole`` (a group of empty cells inside ``z``, not touching its edge, whose four
    neighbours are either in it or have a height) solving Laplace's equation with the heights around as the
    boundary: each new height the mean of its four neighbours. Returns the heights of ``hole``'s cells in the
    order of ``np.nonzero(hole)``."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.linalg import spsolve

    rows, cols = np.nonzero(hole)
    n = len(rows)
    index = np.full(hole.shape, -1, np.int64)
    index[rows, cols] = np.arange(n)
    ii, jj, vv = [np.arange(n)], [np.arange(n)], [np.full(n, 4.0)]
    b = np.zeros(n)
    for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        r, c = rows + dr, cols + dc
        j = index[r, c]
        inside = j >= 0
        ii.append(np.nonzero(inside)[0])
        jj.append(j[inside])
        vv.append(np.full(int(inside.sum()), -1.0))
        np.add.at(b, np.nonzero(~inside)[0], z[r[~inside], c[~inside]])
    a = coo_matrix((np.concatenate(vv), (np.concatenate(ii), np.concatenate(jj))), shape=(n, n)).tocsr()
    return np.asarray(spsolve(a, b), np.float64)


def _fill_window(z: np.ndarray, lab: np.ndarray, ids) -> np.ndarray:
    """The holes ``ids`` of the window ``z`` (NaN where empty; ``lab`` its labels) filled in place; returns the
    mask of the cells filled."""
    filled = np.zeros(z.shape, bool)
    for i in ids:
        hole = lab == i
        vals = harmonic_fill(z, hole)
        ok = np.isfinite(vals)
        r, c = np.nonzero(hole)
        z[r[ok], c[ok]] = vals[ok]
        filled[r[ok], c[ok]] = True
    return filled


def _groups(holes, margin: int, shape):
    """The holes in groups by the tile where their box starts, each with the window that holds them all and
    ``margin`` cells around: ``(row0, row1, col0, col1, ids)``."""
    by_tile: dict[tuple[int, int], list] = {}
    for i, sl, _n, _w in holes:
        by_tile.setdefault((sl[0].start // TILE, sl[1].start // TILE), []).append((i, sl))
    out = []
    for key in sorted(by_tile):
        items = by_tile[key]
        r0 = max(min(sl[0].start for _i, sl in items) - margin, 0)
        r1 = min(max(sl[0].stop for _i, sl in items) + margin, shape[0])
        c0 = max(min(sl[1].start for _i, sl in items) - margin, 0)
        c1 = min(max(sl[1].stop for _i, sl in items) + margin, shape[1])
        out.append((r0, r1, c0, c1, [i for i, _sl in items]))
    return out


def _check(max_width, max_area) -> None:
    for name, v in (("max_width", max_width), ("max_area", max_area)):
        if v is not None and not v > 0:
            raise ValueError(f"{name} must be positive (None: no limit)")


def fill_voids_array(dem: np.ndarray, resolution: float, max_width: float | None = 1.0,
                     max_area: float | None = None, copy: bool = True):
    """The voids of ``dem`` (NaN where empty) inside the data and within the limits filled, in memory (the DEM
    being built; ``copy`` False: filled in place). Returns ``(dem, filled mask, info)``; ``info`` as
    :func:`fill_voids` returns it."""
    _check(max_width, max_area)
    out = np.array(dem, dtype=np.float64, copy=True) if copy or not np.issubdtype(dem.dtype, np.floating) else dem
    lab, holes, skipped = plan_voids(~np.isfinite(out), resolution, max_width, max_area)
    filled = np.zeros(out.shape, bool)
    for r0, r1, c0, c1, ids in _groups(holes, 1, out.shape):
        win = out[r0:r1, c0:c1]  # a view: filled in place
        filled[r0:r1, c0:c1] |= _fill_window(win, lab[r0:r1, c0:c1], ids)
    del lab
    return out, filled, _info(holes, skipped, filled, resolution, max_width, max_area)


def _info(holes, skipped, filled, resolution, max_width, max_area) -> dict:
    cells = int(filled.sum()) if isinstance(filled, np.ndarray) else int(filled)
    return {"holes": len(holes), "cells": cells, "area": cells * resolution * resolution,
            "skipped": skipped["count"], "skipped_cells": skipped["cells"],
            "skipped_area": skipped["cells"] * resolution * resolution,
            "method": "laplace", "max_width": max_width, "max_area": max_area}


# ------------------------------------------------------------------------------------------ the mask of a DEM
def _token(path: Path) -> str | None:
    import rasterio

    try:
        with rasterio.open(path) as src:
            return src.tags().get(TAG)
    except Exception:  # noqa: BLE001 - a file being written, unreadable: no mask
        return None


def mask_file(chunk, token: str) -> Path:
    return Path(chunk.data_dir) / f"{MASK_PREFIX}{token}.tif"


def mask_path(chunk, dem_path=None) -> Path | None:
    """The mask of the interpolated cells of the DEM in use (or of ``dem_path``, a version of it), None when its
    heights are all measured or it has no mask (built before 1.9.1, by another program, by the TIN)."""
    dem_path = Path(dem_path) if dem_path is not None else chunk.dem_path
    if dem_path is None or not Path(dem_path).exists():
        return None
    token = _token(Path(dem_path))
    if not token:
        return None
    path = mask_file(chunk, token)
    return path if path.exists() else None


def interpolated_at(chunk, x: float, y: float) -> int:
    """What the mask says of the cell at ``x``, ``y``: 0 measured (or no mask), :data:`GAP` or :data:`VOID`."""
    path = mask_path(chunk)
    if path is None:
        return 0
    import rasterio
    from rasterio.windows import Window

    with rasterio.open(path) as src:
        row, col = src.index(x, y)
        if not (0 <= row < src.height and 0 <= col < src.width):
            return 0
        return int(src.read(1, window=Window(col, row, 1, 1))[0, 0])


def mask_stats(chunk, dem_path=None) -> dict | None:
    """``{"gap", "void", "cells", "resolution"}`` - cells interpolated between the points of the build, voids filled,
    cells with a height - of the DEM in use (or ``dem_path``), read strip by strip; None without a mask."""
    path = mask_path(chunk, dem_path)
    if path is None:
        return None
    import rasterio
    from rasterio.windows import Window

    dem_path = Path(dem_path) if dem_path is not None else chunk.dem_path
    gap = void = cells = 0
    with rasterio.open(path) as m, rasterio.open(dem_path) as d:
        for r0 in range(0, m.height, STRIP):
            win = Window(0, r0, m.width, min(STRIP, m.height - r0))
            a = m.read(1, window=win)
            gap += int((a == GAP).sum())
            void += int((a == VOID).sum())
            z = d.read(1, window=win)
            ok = np.isfinite(z)
            if d.nodata is not None:
                ok &= z != d.nodata
            cells += int(ok.sum())
        res = abs(d.transform.a)
    return {"gap": gap, "void": void, "cells": cells, "resolution": res}


def _referenced_tokens(chunk) -> set[str]:
    """The identifiers of the DEMs a mask may still be wanted for: the DEM in use, its versions, the copies kept
    for *Annulla* and for *Annulla lo script*."""
    data = Path(chunk.data_dir)
    paths = [chunk.dem_path] if chunk.dem_path is not None else []
    for folder in ("versions", "undo", ".script_undo"):
        root = data / folder
        if root.exists():
            paths += [p for p in root.rglob("*.tif") if p.name.startswith("dem")]
    return {t for t in (_token(Path(p)) for p in paths if Path(p).exists()) if t}


def prune_masks(chunk) -> int:
    """The masks of DEMs no longer kept anywhere deleted; returns how many."""
    data = Path(chunk.data_dir)
    keep = _referenced_tokens(chunk)
    gone = 0
    for p in data.glob(f"{MASK_PREFIX}*.tif"):
        if p.stem[len(MASK_PREFIX):] not in keep:
            try:
                p.unlink()
                gone += 1
            except OSError:
                pass
    return gone


def _mask_profile(src) -> dict:
    from .dem import geotiff_profile

    return geotiff_profile(src.height, src.width, 1, np.uint8, src.transform, src.crs, nodata=0)


def write_mask(chunk, dem_path: Path, mask: np.ndarray) -> Path | None:
    """The mask (uint8, :data:`GAP`/:data:`VOID`, 0 elsewhere) of the DEM just written at ``dem_path``, on its grid,
    under a new identifier written in both; with no interpolated cell the DEM has no mask."""
    import rasterio

    token = uuid.uuid4().hex[:16]
    with rasterio.open(dem_path, "r+") as dst:
        if mask is None or not mask.any():
            dst.update_tags(**{TAG: ""})
            out = None
        else:
            dst.update_tags(**{TAG: token})
            profile = _mask_profile(dst)
            out = mask_file(chunk, token)
            with rasterio.open(out, "w", **profile) as m:
                m.write(mask.astype(np.uint8), 1)
                m.update_tags(**{TAG: token})
    prune_masks(chunk)
    return out


# ------------------------------------------------------------------------------------------ the existing DEM
def _read_empty(src) -> np.ndarray:
    from rasterio.windows import Window

    empty = np.zeros((src.height, src.width), bool)
    for r0 in range(0, src.height, STRIP):
        win = Window(0, r0, src.width, min(STRIP, src.height - r0))
        z = src.read(1, window=win)
        bad = ~np.isfinite(z)
        if src.nodata is not None:
            bad |= z == src.nodata
        empty[r0:r0 + win.height] = bad
    return empty


def fill_voids(chunk, max_width: float | None = 1.0, max_area: float | None = None, keep_version: bool = True, version_name: str | None = None,
               progress: Progress | None = None) -> dict:
    """The voids of the DEM in use - holes without heights enclosed by heights - up to ``max_width`` (m, the widest
    part) and ``max_area`` (m2; None = no limit) given heights from their edges (the smooth surface of
    :func:`harmonic_fill`). The ground outside the data
    and the larger holes stay empty. The DEM is read and written window by window; the cells filled are added to
    its mask (:func:`mask_path`). ``keep_version``: the DEM as it was is kept among the versions first
    (``version_name``: the name of the filled one). Returns ``{"holes", "cells", "area", "skipped",
    "skipped_cells", "skipped_area", "method", "max_width", "max_area", "version"}``."""
    import shutil

    import rasterio
    from rasterio.windows import Window

    from ..core import versions

    progress = as_progress(progress)
    _check(max_width, max_area)
    src_path = chunk.dem_path
    if src_path is None or not Path(src_path).exists():
        raise RuntimeError("build the DEM first")
    old_mask = mask_path(chunk)
    progress(0.05, "DEM: looking for the voids")
    with rasterio.open(src_path) as src:
        res = abs(src.transform.a)
        empty = _read_empty(src)
    lab, holes, skipped = plan_voids(empty, res, max_width, max_area)
    del empty
    info = _info(holes, skipped, 0, res, max_width, max_area)
    info["version"] = None
    if not holes:
        progress(1.0, "DEM: no void to fill within the limits")
        return info
    progress(0.2, f"DEM: {len(holes):,} voids to fill, {sum(h[2] for h in holes):,} cells")
    kept = None
    chunk._before_product_write("dem")
    if keep_version:
        when = datetime.datetime.now().strftime("%d/%m %H:%M")
        old_name = versions.active_info(chunk, "dem")["name"]
        kept = versions.keep_active(chunk, "dem", move=False)
        versions.rename(chunk, "dem", None, version_name or versions.label("{0}, vuoti interpolati {1}", old_name,
                                                                            when))
        src_path = chunk.dem_path
    own = chunk.own_product_path("dem")
    if Path(own).resolve() != Path(src_path).resolve():  # a DEM of another program: filled as a copy in the chunk
        tmp = own.with_name("dem.tmp.tif")
        shutil.copy2(src_path, tmp)
        tmp.replace(own)
        chunk._set_product("dem", own)
    token = uuid.uuid4().hex[:16]
    new_mask = mask_file(chunk, token)
    groups = _groups(holes, 1, lab.shape)
    windows, cells = [], 0
    with rasterio.open(own, "r+") as dst:
        nodata = dst.nodata
        profile = _mask_profile(dst)
        with rasterio.open(new_mask, "w+", **profile) as m:
            if old_mask is not None:  # the cells interpolated before stay marked
                with rasterio.open(old_mask) as om:
                    for r0 in range(0, dst.height, STRIP):
                        win = Window(0, r0, dst.width, min(STRIP, dst.height - r0))
                        m.write(om.read(1, window=win), 1, window=win)
            for k, (r0, r1, c0, c1, ids) in enumerate(groups):
                win = Window(c0, r0, c1 - c0, r1 - r0)
                z = dst.read(1, window=win).astype(np.float64)
                if nodata is not None:
                    z[z == nodata] = np.nan
                filled = _fill_window(z, lab[r0:r1, c0:c1], ids)
                if not filled.any():
                    continue
                cells += int(filled.sum())
                out = np.where(np.isfinite(z), z, nodata if nodata is not None else np.nan).astype(np.float32)
                dst.write(out, 1, window=win)
                mk = m.read(1, window=win) if old_mask is not None else np.zeros(filled.shape, np.uint8)
                mk[filled] = VOID
                m.write(mk, 1, window=win)
                windows.append((c0, r0, c1 - c0, r1 - r0))
                progress(0.2 + 0.7 * (k + 1) / len(groups), f"DEM: {cells:,} cells filled")
            m.update_tags(**{TAG: token})
        dst.update_tags(**{TAG: token})
        factors = dst.overviews(1)
    del lab
    if factors and windows:
        from . import overviews

        if not overviews.refresh(own, windows, factors):
            from rasterio.enums import Resampling

            with rasterio.open(own, "r+") as dst:
                dst.build_overviews(factors, Resampling.average)
    prune_masks(chunk)
    info.update(cells=cells, area=cells * res * res, version=kept["id"] if kept else None)
    record = {k: v for k, v in info.items() if k != "version"}
    record["when"] = datetime.datetime.now().isoformat(timespec="seconds")
    meta = chunk.meta.setdefault("dem", {})
    meta["voids_filled"] = record
    progress(1.0, f"DEM: {info['holes']:,} voids filled ({cells:,} cells), {info['skipped']:,} left empty")
    return info


def export_mask(chunk, exported: Path, region=None) -> Path | None:
    """Beside a DEM exported to ``exported``, its mask of interpolated cells (``<name>_interpolata.tif``, 1
    interpolated between points, 2 void filled), cut to ``region`` like the DEM; None without a mask."""
    path = mask_path(chunk)
    if path is None:
        return None
    exported = Path(exported)
    out = exported.with_name(exported.stem + "_interpolata.tif")
    if region is not None:
        from .crop import crop_raster

        return crop_raster(path, out, region)
    from ..io import exports

    return exports.copy_raster(path, out)


__all__ = ["fill_voids", "fill_voids_array", "plan_voids", "harmonic_fill", "mask_path", "mask_stats",
           "interpolated_at", "write_mask", "prune_masks", "export_mask", "GAP", "VOID", "TAG"]
