"""The presentation finishing of an elevation: an EXPORT of any elevation for a publication, never written into the
measured elevation (its orthophoto and relief stay as they were measured, and its entry is not touched).

In this order, on the orthophoto of the elevation:

1. **the survey targets painted over**: the square of :data:`TARGET_HALF_SIDE` metres around every marker of the
   chunk that lies on the elevation (:func:`.elevation.markers_on`) made a hole;
2. **the holes filled** inside the wall's span (:func:`span`: per column, from its lowest to its highest cell with
   surface, specks of fewer than :data:`SPECK_CELLS` cells left out): the colour from the pyramid of the cells that
   have one (push-pull), plus the detail of the texture mirrored through the nearest cell with colour
   (:func:`fill_holes`). **An opening is never made into wall**: a hole up to :data:`MAX_HOLE` metres across is
   always filled (a hole in the mesh, a gap in the colour); a larger one only when the relief has a depth in the band
   somewhere in it (a gap in the texture on measured geometry); a larger hole with no surface - a door, a window -
   stays open (:func:`fillable`);
3. **the colour levelled** in Lab: each channel less its Gaussian mean of :data:`LEVEL_SIGMA_PX` pixels over the cells
   with colour, plus its global mean - the slow changes of light along a wall taken away, its texture kept
   (:func:`level_colours`);
4. **what lies outside the span masked**: white in a JPEG, transparent in a TIFF;

written at full resolution (or reduced by an integer factor) as a JPEG or an RGBA GeoTIFF in the elevation's frame
metres, with its transform and tags (:func:`finish`)."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np

from ..core.chunk import Chunk
from ..core.progress import Progress, as_progress

log = logging.getLogger("pyarchinit_photogrammetry")

LEVEL_SIGMA_PX = 250.0  # pixels: the colour changes slower than this are levelled
TARGET_HALF_SIDE = 0.18  # m: half the side of the square painted over around a survey target
SPECK_CELLS = 64  # cells: a piece of surface smaller than this is a speck, not of the wall's span
MAX_HOLE = 0.5  # m: a hole larger than this across is an opening (a door, a window), never filled
MAX_PIXELS = 400_000_000  # the largest image finished in one piece (about 6 GB of memory at work)
DETAIL_SIGMA = 2.0  # cells: the low pass the detail of the texture is measured against
JPEG_SIDE = 65_500  # pixels: the longest side a JPEG takes
LEVEL_STRIP = 4_000_000  # cells levelled at a time (the image is not converted to Lab in one piece)


# ------------------------------------------------------------------ the span of the wall
def span(valid) -> np.ndarray:
    """The wall's span: per column, the cells from the lowest to the highest one of ``valid`` (cells with surface),
    pieces of fewer than :data:`SPECK_CELLS` connected cells left out. A boolean array of the shape of ``valid``."""
    import cv2

    valid = np.asarray(valid, bool)
    h = valid.shape[0]
    count, labels, stats, _c = cv2.connectedComponentsWithStats(valid.astype(np.uint8), connectivity=8)
    big = stats[:, cv2.CC_STAT_AREA] >= SPECK_CELLS
    big[0] = False  # the background
    kept = big[labels]
    del labels
    has = kept.any(axis=0)
    first = np.argmax(kept, axis=0)
    last = h - 1 - np.argmax(kept[::-1], axis=0)
    rows = np.arange(h)[:, None]
    return (rows >= first[None, :]) & (rows <= last[None, :]) & has[None, :]


# ------------------------------------------------------------------ holes
def _half(a: np.ndarray) -> np.ndarray:
    """The sums of the 2 x 2 blocks of ``a`` (h, w[, c]); an odd side padded with zeros."""
    h, w = a.shape[:2]
    if h % 2 or w % 2:
        pad = [(0, h % 2), (0, w % 2)] + [(0, 0)] * (a.ndim - 2)
        a = np.pad(a, pad)
    h2, w2 = a.shape[0] // 2, a.shape[1] // 2
    return a.reshape(h2, 2, w2, 2, *a.shape[2:]).sum(axis=(1, 3))


def _push_pull(rgb: np.ndarray, weight: np.ndarray) -> np.ndarray:
    """Colour everywhere (h, w, 3) float32 from the cells of ``weight`` (0 or 1): the pyramid of the weighted
    colours pushed down to one cell, then pulled up - a cell without colour takes the colour of the coarser level."""
    import cv2

    levels = []
    c = rgb.astype(np.float32) * weight[..., None]
    w = weight.astype(np.float32)
    while True:
        levels.append((c, w))
        if max(c.shape[:2]) <= 1:
            break
        cs, ws = _half(c), _half(w)
        colour = cs / np.maximum(ws, 1e-12)[..., None]
        w = np.minimum(ws, 1.0)
        c = colour * w[..., None]
    c, w = levels[-1]
    full = c / np.maximum(w, 1e-12)[..., None]
    for c, w in reversed(levels[:-1]):
        up = cv2.resize(full, (c.shape[1], c.shape[0]), interpolation=cv2.INTER_LINEAR)
        if up.ndim == 2:
            up = up[..., None]
        full = c + (1.0 - w)[..., None] * up
    return full


def _low_pass(rgb: np.ndarray, valid: np.ndarray, sigma: float) -> np.ndarray:
    """The Gaussian mean of ``rgb`` over the ``valid`` cells only (normalised convolution)."""
    import cv2

    m = valid.astype(np.float32)
    num = cv2.GaussianBlur(rgb.astype(np.float32) * m[..., None], (0, 0), sigma, borderType=cv2.BORDER_CONSTANT)
    den = cv2.GaussianBlur(m, (0, 0), sigma, borderType=cv2.BORDER_CONSTANT)
    return num / np.maximum(den, 1e-6)[..., None]


def _fill_piece(rgb: np.ndarray, valid: np.ndarray, hole: np.ndarray) -> np.ndarray | None:
    """The colours (n, 3) float of the cells of ``hole`` in a piece of the image, or None when the piece has no cell
    with colour to take them from."""
    from scipy.ndimage import distance_transform_edt

    if not valid.any():
        return None
    base = _push_pull(rgb, valid.astype(np.float32))
    detail = rgb.astype(np.float32) - _low_pass(rgb, valid, DETAIL_SIGMA)
    _d, (ir, ic) = distance_transform_edt(~valid, return_indices=True)
    rows, cols = np.nonzero(hole)
    nr, nc = ir[rows, cols], ic[rows, cols]
    mr, mc = 2 * nr - rows, 2 * nc - cols  # the cell mirrored through the nearest cell with colour
    h, w = valid.shape
    inside = (mr >= 0) & (mr < h) & (mc >= 0) & (mc < w)
    ok = np.zeros(len(rows), bool)
    ok[inside] = valid[mr[inside], mc[inside]]
    add = np.zeros((len(rows), 3), np.float32)
    add[ok] = detail[mr[ok], mc[ok]]
    return base[rows, cols] + add


def _fill(rgb: np.ndarray, valid: np.ndarray, holes: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """:func:`fill_holes`, and the cells it filled."""
    import cv2

    out = np.array(rgb, copy=True)
    holes = np.asarray(holes, bool) & ~np.asarray(valid, bool)
    done = np.zeros(holes.shape, bool)
    count, labels, stats, _c = cv2.connectedComponentsWithStats(holes.astype(np.uint8), connectivity=8)
    h, w = holes.shape
    for i in range(1, count):
        x, y, bw, bh = (int(v) for v in stats[i, :4])
        margin = max(bw, bh) + 4 * int(np.ceil(DETAIL_SIGMA)) + 2
        r0, r1, c0, c1 = max(0, y - margin), min(h, y + bh + margin), max(0, x - margin), min(w, x + bw + margin)
        piece = labels[r0:r1, c0:c1] == i
        colours = _fill_piece(rgb[r0:r1, c0:c1], valid[r0:r1, c0:c1], piece)
        if colours is None:
            continue
        pr, pc = np.nonzero(piece)
        out[pr + r0, pc + c0] = np.clip(np.round(colours), 0, 255).astype(out.dtype)
        done[pr + r0, pc + c0] = True
    return out, done


def fillable(holes, measured, cell: tuple[float, float], max_hole: float = MAX_HOLE) -> tuple[np.ndarray, int]:
    """``(the cells of the holes that may be filled, how many holes are openings)``. A hole (a connected piece of
    ``holes``) whose largest extent - ``cell`` (width, height of a cell, metres) times its bounding box - is at most
    ``max_hole`` is always filled; a larger one only when ``measured`` (the cells with a depth in the band: the
    relief) has a cell in it - a gap in the texture on measured geometry. A larger hole with no surface is an opening
    (a door, a window) and stays open. ``measured`` None: the size alone decides."""
    import cv2

    holes = np.asarray(holes, bool)
    count, labels, stats, _c = cv2.connectedComponentsWithStats(holes.astype(np.uint8), connectivity=8)
    if count <= 1:
        return np.zeros(holes.shape, bool), 0
    extent = np.maximum(stats[:, cv2.CC_STAT_WIDTH] * abs(cell[0]), stats[:, cv2.CC_STAT_HEIGHT] * abs(cell[1]))
    ok = extent <= max_hole + 1e-9
    if measured is not None:
        ok |= np.bincount(labels[np.asarray(measured, bool) & holes], minlength=count) > 0
    ok[0] = False
    return ok[labels], int(count - 1 - ok[1:].sum())


def fill_holes(rgb, valid, holes) -> np.ndarray:
    """``rgb`` (h, w, 3) uint8 with the cells of ``holes`` filled from those of ``valid`` (the cells whose colour is
    known): the colour of a hole from the pyramid of the valid cells (push-pull), plus the detail of the texture - the
    source's value less its own low pass - at the cell mirrored through the nearest valid cell (zero where that cell
    is not valid). Each hole is filled from a piece of the image around it. A copy; the valid cells are unchanged."""
    return _fill(np.asarray(rgb), np.asarray(valid, bool), holes)[0]


# ------------------------------------------------------------------ colour levelling
def _lab(rgb: np.ndarray) -> np.ndarray:
    import cv2

    return cv2.cvtColor(rgb.astype(np.float32) / 255.0, cv2.COLOR_RGB2Lab)


def _rgb(lab: np.ndarray) -> np.ndarray:
    import cv2

    out = cv2.cvtColor(lab.astype(np.float32), cv2.COLOR_Lab2RGB)
    return np.clip(np.round(out * 255.0), 0, 255).astype(np.uint8)


def level_colours(rgb, valid, sigma: float = LEVEL_SIGMA_PX) -> np.ndarray:
    """``rgb`` (h, w, 3) uint8 levelled in Lab: each channel less its Gaussian mean of ``sigma`` pixels over the
    ``valid`` cells (the cells that are not valid do not pull those beside them), plus its global mean over them. The
    mean is computed on a grid ``sigma / 8`` times coarser and interpolated back; the image is converted to Lab a
    strip at a time. A copy; the cells that are not valid are unchanged."""
    from scipy.ndimage import gaussian_filter

    rgb = np.asarray(rgb)
    valid = np.asarray(valid, bool)
    out = np.array(rgb, copy=True)
    h, w = valid.shape
    if not valid.any():
        return out
    f = max(1, int(sigma // 8))
    ch, cw = -(-h // f), -(-w // f)
    step = max(f, (LEVEL_STRIP // max(w, 1)) // f * f)
    sums = np.zeros((ch, cw, 3), np.float64)
    counts = np.zeros((ch, cw), np.float64)
    for r0 in range(0, h, step):
        r1 = min(h, r0 + step)
        m = valid[r0:r1]
        lab = _lab(rgb[r0:r1]) * m[..., None]
        k0 = r0 // f
        s = _half_f(lab, f)
        c = _half_f(m.astype(np.float32), f)
        sums[k0:k0 + s.shape[0]] += s
        counts[k0:k0 + c.shape[0]] += c
    mean = sums.sum(axis=(0, 1)) / counts.sum()
    s = sigma / f
    num = np.stack([gaussian_filter(sums[..., i], s, mode="constant") for i in range(3)], axis=-1)
    den = gaussian_filter(counts, s, mode="constant")
    for r0 in range(0, h, step):
        r1 = min(h, r0 + step)
        m = valid[r0:r1]
        if not m.any():
            continue
        lab = _lab(rgb[r0:r1])
        low = _up(num, f, r0, r1, w) / np.maximum(_up(den[..., None], f, r0, r1, w), 1e-9)
        lab = lab - low + mean.astype(np.float32)
        out[r0:r1][m] = _rgb(lab)[m]
    return out


def _half_f(a: np.ndarray, f: int) -> np.ndarray:
    """The sums of the f x f blocks of ``a`` (h, w[, c]); the last ones padded with zeros."""
    h, w = a.shape[:2]
    ph, pw = -h % f, -w % f
    if ph or pw:
        a = np.pad(a, [(0, ph), (0, pw)] + [(0, 0)] * (a.ndim - 2))
    return a.reshape(a.shape[0] // f, f, a.shape[1] // f, f, *a.shape[2:]).sum(axis=(1, 3), dtype=np.float64)


def _up(coarse: np.ndarray, f: int, r0: int, r1: int, w: int) -> np.ndarray:
    """Rows ``r0:r1`` of the full grid (``w`` columns) interpolated (bilinear) from the ``coarse`` grid of blocks of
    ``f`` cells (its values at the blocks' centres)."""
    import cv2

    ch = coarse.shape[0]
    y = np.clip((np.arange(r0, r1) + 0.5) / f - 0.5, 0.0, ch - 1)
    i0 = np.floor(y).astype(int)
    i1 = np.minimum(i0 + 1, ch - 1)
    t = (y - i0)[:, None, None]
    rows = (coarse[i0] * (1.0 - t) + coarse[i1] * t).astype(np.float32)
    full = cv2.resize(rows, (coarse.shape[1] * f, rows.shape[0]), interpolation=cv2.INTER_LINEAR)
    if full.ndim == 2:
        full = full[..., None]
    return full[:, :w]


# ------------------------------------------------------------------ the export
def _load(ortho: Path, relief: Path, reduce: int, rows: int = 2048):
    """``(rgb (h, w, 3) uint8, coloured, measured)`` of the elevation reduced by ``reduce`` (read a strip of rows at a
    time): a cell's colour the mean of the coloured cells of its block (alpha over 0), coloured or measured (a depth in
    the relief) when one of them is; ``measured`` None for an elevation without its relief."""
    import rasterio
    from rasterio.windows import Window

    with rasterio.open(ortho) as src:
        h, w = src.height, src.width
        oh, ow = -(-h // reduce), -(-w // reduce)
        rgb = np.empty((oh, ow, 3), np.uint8)
        coloured = np.empty((oh, ow), bool)
        surface = np.empty((oh, ow), bool)
        dem = rasterio.open(relief) if relief.exists() else None
        try:
            if dem is not None and (dem.height, dem.width) != (h, w):
                dem.close()
                dem = None
            step = max(1, rows // reduce) * reduce
            for r0 in range(0, h, step):
                win = Window(0, r0, w, min(step, h - r0))
                block = src.read([1, 2, 3, 4], window=win)
                c = np.moveaxis(block[:3], 0, -1)
                m = block[3] > 0
                s = np.isfinite(dem.read(1, window=win)) if dem is not None else np.zeros_like(m)
                k0, k1 = r0 // reduce, r0 // reduce + -(-win.height // reduce)
                if reduce == 1:
                    rgb[k0:k1], coloured[k0:k1], surface[k0:k1] = c, m, s
                    continue
                mf = m.astype(np.float64)
                n = _half_f(mf, reduce)
                sums = _half_f(c.astype(np.float64) * mf[..., None], reduce)
                rgb[k0:k1] = np.clip(np.round(sums / np.maximum(n, 1)[..., None]), 0, 255).astype(np.uint8)
                coloured[k0:k1] = n > 0
                surface[k0:k1] = _half_f(s.astype(np.float32), reduce) > 0
            known = dem is not None
        finally:
            if dem is not None:
                dem.close()
    return rgb, coloured, (surface if known else None)


def _target_cells(chunk: Chunk, entry: dict, transform, shape) -> tuple[np.ndarray, int]:
    """The cells of the squares of :data:`TARGET_HALF_SIDE` around the markers of the chunk on the elevation, and how
    many markers have cells in the grid."""
    from .elevation import markers_on

    h, w = shape
    cells = np.zeros(shape, bool)
    n = 0
    for _label, u, v, _d in markers_on(chunk, entry):
        cu = transform.c + (np.arange(w) + 0.5) * transform.a
        cv = transform.f + (np.arange(h) + 0.5) * transform.e
        cols = np.nonzero(np.abs(cu - u) <= TARGET_HALF_SIDE)[0]
        rows = np.nonzero(np.abs(cv - v) <= TARGET_HALF_SIDE)[0]
        if len(cols) and len(rows):
            cells[rows[0]:rows[-1] + 1, cols[0]:cols[-1] + 1] = True
            n += 1
    return cells, n


def inside_folder(path, folder) -> bool:
    """Whether ``path`` is ``folder`` or lies in it (both resolved): the finishing refuses the folder of a chunk."""
    path, folder = Path(path).resolve(), Path(folder).resolve()
    return path == folder or folder in path.parents


def finish(chunk: Chunk, entry: dict, out_path, fill: bool = True, mask: bool = True, level: bool = True,
           targets: bool = True, reduce: int = 1, quality: int = 92, max_hole: float = MAX_HOLE,
           progress: Progress | None = None) -> dict:
    """The elevation ``entry`` of ``chunk`` finished for a publication and written to ``out_path`` - a JPEG
    (``.jpg``/``.jpeg``, ``quality``) or an RGBA GeoTIFF (``.tif``/``.tiff``) in frame metres with the elevation's
    transform and tags - at full resolution, or reduced by the integer ``reduce``. In this order: the square of
    :data:`TARGET_HALF_SIDE` around every marker on the elevation made a hole (``targets``); the holes inside the
    wall's span filled (``fill``, :func:`fill_holes`) - every one up to ``max_hole`` metres across, a larger one when
    the relief has surface in it; a larger one without surface is an opening and stays open (:func:`fillable`); the
    colour
    levelled in Lab (``level``, :func:`level_colours`,
    :data:`LEVEL_SIGMA_PX` pixels of the full resolution); what lies outside the span masked (``mask``: white in a
    JPEG, transparent in a TIFF; without it a cell without colour is still white or transparent).

    An export: nothing is written in the chunk's folder (``ValueError`` for a path there) and the entry and the
    chunk's meta are not changed. ``ValueError`` over :data:`MAX_PIXELS` (with the ``reduce`` that fits). Returns
    ``{"path", "size" ([w, h] written), "filled" (cells), "masked" (cells outside the span), "targets" (markers
    painted over), "openings" (holes left open), "levelled"}``."""
    import rasterio
    from rasterio.enums import ColorInterp
    from rasterio.transform import Affine

    from .elevation import elevation_paths

    progress = as_progress(progress)
    out = Path(out_path)
    suffix = out.suffix.lower()
    if suffix not in (".jpg", ".jpeg", ".tif", ".tiff"):
        raise ValueError("the finished elevation is written as a JPEG (.jpg) or a TIFF (.tif), not "
                         f"{suffix or 'a file without extension'!r}")
    if int(reduce) != reduce or int(reduce) < 1:
        raise ValueError(f"reduce: a whole number, 1 or more (it was {reduce!r})")
    reduce = int(reduce)
    if inside_folder(out, chunk.data_dir):
        raise ValueError("the finishing is an export: it is not written in the folder of the chunk, where the "
                         "measured elevation stays as it was measured - choose another folder")
    ortho, relief = elevation_paths(chunk, entry)
    with rasterio.open(ortho) as src:
        h, w = src.height, src.width
        transform = src.transform
        tags = src.tags()
    oh, ow = -(-h // reduce), -(-w // reduce)
    if oh * ow > MAX_PIXELS:
        need = int(np.ceil(np.sqrt(h * w / MAX_PIXELS)))
        while -(-h // need) * -(-w // need) > MAX_PIXELS:
            need += 1
        raise ValueError(f"the finished elevation would be {ow} x {oh} px, over {MAX_PIXELS:,} pixels: reduce it "
                         f"(reduce={need} or more)")
    jpeg = suffix in (".jpg", ".jpeg")
    if jpeg and max(oh, ow) > JPEG_SIDE:
        need = int(np.ceil(max(h, w) / JPEG_SIDE))
        raise ValueError(f"a JPEG takes at most {JPEG_SIDE} px a side, the elevation would be {ow} x {oh}: reduce it "
                         f"(reduce={need} or more) or write a TIFF")
    progress(0.0, f"Finishing {entry.get('name')}: reading {w} x {h} px")
    rgb, coloured, measured = _load(ortho, relief, reduce)
    surface = coloured | measured if measured is not None else coloured.copy()
    transform = transform @ Affine.scale(reduce, reduce)
    n_targets = 0
    if targets:
        cells, n_targets = _target_cells(chunk, entry, transform, coloured.shape)
        coloured &= ~cells
    progress(0.1, "Span of the wall")
    sp = span(surface)
    filled = 0
    to_fill, openings = fillable(sp & ~coloured, measured, (transform.a, transform.e), max_hole)
    if fill:
        progress(0.2, "Filling the holes")
        rgb, done = _fill(rgb, coloured, to_fill)
        filled = int(done.sum())
        coloured |= done
    keep = coloured & sp if mask else coloured
    levelled = False
    if level and keep.any():
        progress(0.5, "Levelling the colour")
        rgb = level_colours(rgb, keep, max(LEVEL_SIGMA_PX / reduce, 8.0))
        levelled = True
    masked = int((~sp).sum()) if mask else 0
    rgb[~keep] = 255
    progress(0.85, f"Writing {out.name}")
    if jpeg:
        import cv2

        ok, data = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR),
                                [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
        if not ok:
            raise OSError(f"the JPEG {out} could not be encoded")
        out.write_bytes(data.tobytes())
    else:
        oh, ow = keep.shape
        tiled = {"tiled": True, "blockxsize": 256, "blockysize": 256} if oh >= 256 and ow >= 256 else {}
        profile = {"driver": "GTiff", "height": oh, "width": ow, "count": 4, "dtype": "uint8", "transform": transform,
                   "compress": "deflate", "photometric": "RGB", "BIGTIFF": "IF_SAFER", **tiled}
        with rasterio.open(out, "w", **profile) as dst:
            dst.colorinterp = [ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha]
            finish_tag = json.dumps({"elevation": entry.get("id"), "fill": bool(fill), "mask": bool(mask),
                                     "level": bool(level), "targets": bool(targets), "reduce": reduce,
                                     "max_hole": float(max_hole), "openings": openings})
            dst.update_tags(**{k: v for k, v in tags.items() if k.startswith("ppg_")}, ppg_finish=finish_tag)
            dst.write(np.moveaxis(rgb, -1, 0), [1, 2, 3])
            dst.write((keep * np.uint8(255)).astype(np.uint8), 4)
    progress(1.0, f"Finished: {out.name}")
    oh, ow = keep.shape
    log.info("Elevation %s finished: %s (%d x %d px; %d cells filled, %d openings left open, %d targets painted "
             "over)", entry.get("name"), out, ow, oh, filled, openings, n_targets)
    return {"path": str(out), "size": [ow, oh], "filled": filled, "masked": masked, "targets": n_targets,
            "openings": openings, "levelled": levelled}
