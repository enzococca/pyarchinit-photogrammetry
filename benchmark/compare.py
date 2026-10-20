"""Comparison of the DEMs and orthophotos of several programs on the same photos.

    python compare.py OUT name=dem.tif,ortho.tif [name=dem.tif,ortho.tif ...]

The first is the reference. Georeferenced only by the GPS of the drone, the products of two
programs can be metres apart and tilted: each DEM is first brought onto the reference (horizontal
shift by phase correlation of the hillshades; offset, tilt and height scale by a robust fit of the
differences), then compared on a common 10 cm grid: NMAD of the differences, 95th percentile of
their size, cells filled. Orthophotos: share of the common footprint covered, sharpness (variance of the
Laplacian of the grey image at 2 cm) in the same tiles. Writes compare.json and figures.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT

OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)
products = []
for arg in sys.argv[2:]:
    name, paths = arg.split("=", 1)
    dem, ortho = paths.split(",")
    products.append({"name": name, "dem": Path(dem), "ortho": Path(ortho)})


def bounds(path):
    with rasterio.open(path) as src:
        return src.bounds, src.crs


def common_box(paths):
    bs = [bounds(p)[0] for p in paths]
    return max(b.left for b in bs), max(b.bottom for b in bs), min(b.right for b in bs), min(b.top for b in bs)


def on_grid(path, box, res, crs, shift=(0.0, 0.0), resampling=Resampling.bilinear, bands=None):
    """``path`` resampled on the grid ``box``/``res``, moved by ``shift`` (metres)."""
    left, bottom, right, top = box
    w, h = int((right - left) / res), int((top - bottom) / res)
    transform = from_origin(left - shift[0], top - shift[1], res, res)
    with rasterio.open(path) as src, WarpedVRT(src, crs=crs, transform=transform, width=w, height=h,
                                               resampling=resampling) as vrt:
        if bands is None:
            a = vrt.read(1, masked=True).astype(np.float64)
            return a.filled(np.nan)
        return vrt.read(bands)


def hillshade(z, res):
    gy, gx = np.gradient(np.nan_to_num(z, nan=np.nanmedian(z)), res)
    slope = np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    az, alt = np.radians(315), np.radians(45)
    return np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect)


def refine(zr, z, res, iterations=12):
    """Least squares surface matching: the sub-pixel shift (and offset, tilt and height scale)
    that brings the grid ``z`` onto ``zr``, linearised on the slopes of ``zr`` and iterated with a
    robust weight. Returns the shifted grid, the shift (rows, cols in pixels) and the residuals."""
    from scipy import ndimage

    gr, gc = np.gradient(np.nan_to_num(zr, nan=np.nanmedian(zr)))
    rows, cols = np.mgrid[0:zr.shape[0], 0:zr.shape[1]]
    valid = np.isfinite(z).astype(float)
    filled = np.nan_to_num(z, nan=np.nanmedian(z))
    total = np.zeros(2)
    wts = None
    for _ in range(iterations):
        cur = ndimage.shift(filled, total, order=1, mode="nearest")
        cur[ndimage.shift(valid, total, order=1, mode="constant", cval=0.0) < 0.999] = np.nan
        d = cur - zr
        ok = np.isfinite(d)
        A = np.column_stack([gc[ok], gr[ok], np.ones(ok.sum()), cols[ok] * res, rows[ok] * res,
                             zr[ok] - np.nanmedian(zr)])
        w = np.ones(ok.sum()) if wts is None or len(wts) != ok.sum() else wts
        coef, *_ = np.linalg.lstsq(A * w[:, None], d[ok] * w, rcond=None)
        res_ = d[ok] - A @ coef
        mad = 1.4826 * np.median(np.abs(res_)) + 1e-6
        wts = 1.0 / np.maximum(1.0, np.abs(res_) / (2 * mad))
        step = np.array([coef[1], coef[0]])  # rows, cols
        total += step
        if np.hypot(*step) < 0.01:
            break
    out = np.full(d.shape, np.nan)
    out[ok] = res_
    return cur, total, out, coef


def phase_shift(a, b):
    """Shift (dx, dy) in pixels that brings b onto a."""
    import cv2

    a = np.nan_to_num(a).astype(np.float32)
    b = np.nan_to_num(b).astype(np.float32)
    win = cv2.createHanningWindow(a.shape[::-1], cv2.CV_32F)
    (dx, dy), response = cv2.phaseCorrelate(a, b, win)
    return dx, dy, response


ref = products[0]
_, crs = bounds(ref["dem"])
box = common_box([p["dem"] for p in products])
RES = 0.10
report = {"reference": ref["name"], "grid_m": RES, "box": box, "dem": {}, "ortho": {}}
zr = on_grid(ref["dem"], box, RES, crs)
hr = hillshade(zr, RES)
figures = {"hillshade": [(ref["name"], hr)], "diff": []}
COARSE = 1.0  # m: the first estimate of the shift on a coarse grid, where a few spikes do not count
zr_c = on_grid(ref["dem"], box, COARSE, crs, resampling=Resampling.average)
hr_c = hillshade(zr_c, COARSE)
for p in products[1:]:
    z0 = on_grid(p["dem"], box, COARSE, crs, resampling=Resampling.average)
    dx, dy, response = phase_shift(hr_c, hillshade(z0, COARSE))
    shift = (-dx * COARSE, dy * COARSE)  # pixels -> metres (rows grow southwards)
    z = on_grid(p["dem"], box, RES, crs, shift=shift)
    d0 = z - zr
    ok0 = np.isfinite(d0)
    dz = float(np.median(d0[ok0]))
    raw_nmad = float(1.4826 * np.median(np.abs(d0[ok0] - dz)))
    # georeferenced by the GPS of the drone only, each program places the survey a little differently:
    # a sub-pixel shift, an offset, a tilt (a few mm per m) and a height scale (a few per cent),
    # estimated together by least squares surface matching before the shapes are compared
    z, extra, d, coef = refine(zr, z, RES)
    res = d[np.isfinite(d)]
    nmad = float(1.4826 * np.median(np.abs(res - np.median(res))))
    shift = (shift[0] + extra[1] * RES, shift[1] - extra[0] * RES)  # content moved by (rows, cols) pixels
    report["dem"][p["name"]] = {
        "horizontal_shift_m": [round(shift[0], 3), round(shift[1], 3)], "phase_response": round(float(response), 3),
        "vertical_offset_m": round(dz, 3), "raw_nmad_m": round(raw_nmad, 4),
        "tilt_mm_per_m": [round(coef[3] * 1000, 2), round(coef[4] * 1000, 2)], "height_scale": round(1 + coef[5], 4),
        "nmad_m": round(nmad, 4), "p95_abs_m": round(float(np.percentile(np.abs(res), 95)), 4),
        "filled_share": round(float(np.isfinite(z).mean()), 4), "reference_filled_share": round(float(np.isfinite(zr).mean()), 4)}
    figures["hillshade"].append((p["name"], hillshade(z, RES)))
    figures["diff"].append((p["name"], d))
    p["shift"] = shift
print(json.dumps(report["dem"], indent=1))

# orthophotos: coverage and sharpness on a 2 cm grid in tiles spread over the common footprint
obox = common_box([p["ortho"] for p in products])
ORES = 0.02
rng = np.random.default_rng(1)
tiles = []
while len(tiles) < 60:
    x = rng.uniform(obox[0], obox[2] - 10)
    y = rng.uniform(obox[1] + 10, obox[3])
    tiles.append((x, y - 10, x + 10, y))
import cv2  # noqa: E402

cover, sharp = {}, {}
for p in products:
    cover[p["name"]], sharp[p["name"]] = [], []
    for t in tiles:
        a = on_grid(p["ortho"], t, ORES, crs, shift=p.get("shift", (0.0, 0.0)), bands=[1, 2, 3, 4])
        alpha = a[3] > 0
        cover[p["name"]].append(alpha.mean())
        grey = cv2.cvtColor(np.moveaxis(a[:3], 0, -1).astype(np.uint8), cv2.COLOR_RGB2GRAY)
        sharp[p["name"]].append(float(cv2.Laplacian(grey, cv2.CV_64F).var()))
# the sharpness of every orthophoto on the same tiles: those all of them cover
common = [i for i in range(len(tiles)) if all(cover[name][i] > 0.95 for name in cover)]
for p in products:
    name = p["name"]
    report["ortho"][name] = {"covered_share": round(float(np.mean(cover[name])), 4),
                             "sharpness_median": round(float(np.median([sharp[name][i] for i in common])), 1)
                             if common else None, "tiles": len(common)}
    with rasterio.open(p["ortho"]) as src:
        report["ortho"][name]["resolution_m"] = round(src.res[0], 4)
print(json.dumps(report["ortho"], indent=1))
json.dump(report, open(OUT / "compare.json", "w"), indent=1)

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

n = len(figures["hillshade"])
fig, axes = plt.subplots(1, n, figsize=(5 * n, 5))
for ax, (name, h) in zip(np.atleast_1d(axes), figures["hillshade"]):
    ax.imshow(h, cmap="gray", vmin=0, vmax=1)
    ax.set_title(name)
    ax.axis("off")
fig.tight_layout()
fig.savefig(OUT / "hillshades.png", dpi=110)
if figures["diff"]:
    fig, axes = plt.subplots(1, len(figures["diff"]), figsize=(5.5 * len(figures["diff"]), 5))
    for ax, (name, d) in zip(np.atleast_1d(axes), figures["diff"]):
        im = ax.imshow(d, cmap="RdBu_r", vmin=-0.3, vmax=0.3)
        ax.set_title(f"{name} - {ref['name']} (m)")
        ax.axis("off")
        fig.colorbar(im, ax=ax, shrink=0.8)
    fig.tight_layout()
    fig.savefig(OUT / "dem_differences.png", dpi=110)
print("written", OUT)
