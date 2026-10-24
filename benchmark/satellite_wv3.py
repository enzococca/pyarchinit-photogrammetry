"""Satellite surface models against lidar: the JHU/APL multi-view stereo benchmark (WorldView-3).

The data (public release, unlimited distribution): https://www.jhuapl.edu/satellite-benchmark.html
-- ``crops/<site>/*.tif`` chips with their ``rpc_<chip>.txt``, ``lidar_gt/<site>.tif`` and
``kml/<site>.kml`` (the evaluation polygon). For every site the script builds

* the surface from the chosen scenes (:func:`engine.satellite.multi_view_dsm`), with and without the
  relative correction of the RPCs;
* the surface of every pair of the fusion alone (two scenes, the correction computed on those
  two), with and without the correction: the best of them (completeness after alignment) is
  reported as the single pair;

and measures each against the lidar inside the polygon: median absolute error, NMAD, RMSE and
completeness (share of the lidar cells with a height within 1 m), (a) as they come out and (b) after
the evaluation alignment: the horizontal shift (whole lidar cells, +-3 m) and the vertical offset
(median) that minimise the median absolute error, the same kind of registration as the benchmark's
own metric tool. The alignment is for the evaluation only: nothing in the program uses the lidar.

    python benchmark/satellite_wv3.py DATA OUT [--site Explorer] [--scenes 01SEP15,...] [--resolution 0.3]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import numpy as np

import pyarchinit_photogrammetry  # noqa: F401  (environment: PROJ data, Open3D first)
from pyarchinit_photogrammetry.engine import satellite as sat

# scenes of 2015 taken within a few weeks of each other in the southern summer and late spring,
# from directions 7-30 degrees apart: the default choice (dates as in the chip names)
DEFAULT_SCENES = {
    "Explorer": ["12JUN15", "18JUN15", "19JUN15", "24JUN15", "30JUN15"],
    "MasterProvisional3": ["12JUN15", "18JUN15", "19JUN15", "24JUN15", "30JUN15"],
}
HEIGHT_RANGE = (-20.0, 100.0)  # WGS84 ellipsoid, metres
CRS = "EPSG:32721"  # the lidar's: WGS 84 / UTM 21S


def scenes_of(data: Path, site: str, tags: list[str]) -> list[Path]:
    chips = sorted((data / "crops" / site).glob("*.tif"))
    out = []
    for tag in tags:
        hit = [c for c in chips if f"_{tag}WV" in c.name or tag in c.name]
        if not hit:
            raise SystemExit(f"{site}: no chip for {tag}")
        out.append(hit[0])
    return out


def polygon(data: Path, site: str):
    from pyproj import Transformer
    from shapely.geometry import Polygon

    text = (data / "kml" / f"{site}.kml").read_text()
    nums = [float(v) for v in re.findall(r"[-\d.]+", re.search(r"<coordinates>(.*?)</coordinates>", text, re.S).group(1))]
    lon, lat = np.array(nums[0::3]), np.array(nums[1::3])
    x, y = Transformer.from_crs("EPSG:4326", CRS, always_xy=True).transform(lon, lat)
    return Polygon(zip(x, y))


def truth(data: Path, site: str):
    import rasterio
    from rasterio.features import geometry_mask

    with rasterio.open(data / "lidar_gt" / f"{site}.tif") as src:
        gt = src.read(1).astype(np.float64)
        gt[gt == src.nodata] = np.nan
        inside = ~geometry_mask([polygon(data, site)], gt.shape, src.transform)
        return gt, inside, src.transform, src.crs, tuple(src.bounds)


def on_truth_grid(dsm: Path, transform, crs, shape) -> np.ndarray:
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.warp import reproject

    out = np.full(shape, np.nan, np.float32)
    with rasterio.open(dsm) as src:
        z = src.read(1).astype(np.float32)
        z[z == src.nodata] = np.nan
        reproject(z, out, src_transform=src.transform, src_crs=src.crs, dst_transform=transform, dst_crs=crs,
                  resampling=Resampling.nearest, src_nodata=np.nan, dst_nodata=np.nan)
    return out.astype(np.float64)


def metrics(dsm: np.ndarray, gt: np.ndarray, inside: np.ndarray, dx: int = 0, dy: int = 0, dz: float = 0.0) -> dict:
    moved = np.full_like(dsm, np.nan)
    h, w = dsm.shape
    ys, xs = slice(max(dy, 0), h + min(dy, 0)), slice(max(dx, 0), w + min(dx, 0))
    yd, xd = slice(max(-dy, 0), h + min(-dy, 0)), slice(max(-dx, 0), w + min(-dx, 0))
    moved[ys, xs] = dsm[yd, xd]
    ref = inside & np.isfinite(gt)
    e = moved - dz - gt
    ok = ref & np.isfinite(e)
    ev = e[ok]
    return {"median_abs": float(np.median(np.abs(ev))), "median": float(np.median(ev)),
            "nmad": float(1.4826 * np.median(np.abs(ev - np.median(ev)))), "rmse": float(np.sqrt(np.mean(ev ** 2))),
            "completeness": float((np.abs(e[ref]) < 1.0).sum() / ref.sum()), "coverage": float(ok.sum() / ref.sum()),
            "dx_m": None, "dy_m": None, "dz": dz}


def aligned(dsm: np.ndarray, gt: np.ndarray, inside: np.ndarray, res: float, reach: float = 3.0) -> dict:
    """The evaluation registration: whole-cell shifts within ``reach`` metres, the vertical offset
    = median difference, the shift with the least median absolute error kept."""
    k = int(round(reach / res))
    ref = inside & np.isfinite(gt)
    rows, cols = np.nonzero(ref)
    rng = np.random.default_rng(0)
    pick = rng.choice(len(rows), min(len(rows), 200_000), replace=False)  # the search on a sample of cells
    rows, cols = rows[pick], cols[pick]
    h, w = dsm.shape
    best = None
    for dy in range(-k, k + 1):
        for dx in range(-k, k + 1):
            r, c = rows - dy, cols - dx  # moved[r0, c0] = dsm[r0 - dy, c0 - dx]
            ok = (r >= 0) & (r < h) & (c >= 0) & (c < w)
            e = dsm[r[ok], c[ok]] - gt[rows[ok], cols[ok]]
            e = e[np.isfinite(e)]
            if e.size < 1000:
                continue
            dz = float(np.median(e))
            score = float(np.median(np.abs(e - dz)))
            if best is None or score < best[0]:
                best = (score, dx, dy, dz)
    _, dx, dy, dz = best
    return {**metrics(dsm, gt, ref, dx, dy, dz), "dx_m": dx * res, "dy_m": -dy * res}


def evaluate(path: Path, gt, inside, transform, crs, res) -> dict:
    dsm = on_truth_grid(path, transform, crs, gt.shape)
    return {"raw": metrics(dsm, gt, inside), "aligned": aligned(dsm, gt, inside, res)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("data", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--site", action="append")
    ap.add_argument("--scenes", help="comma-separated date tags of the chips (e.g. 12JUN15,18JUN15)")
    ap.add_argument("--resolution", type=float, default=0.3)
    ap.add_argument("--max-pairs", type=int, default=6)
    ap.add_argument("--tile", type=int, default=512)
    args = ap.parse_args(argv)
    report = {}
    for site in args.site or list(DEFAULT_SCENES):
        out = args.out / site
        out.mkdir(parents=True, exist_ok=True)
        tags = args.scenes.split(",") if args.scenes else DEFAULT_SCENES[site]
        scenes = scenes_of(args.data, site, tags)
        gt, inside, transform, crs, bounds = truth(args.data, site)
        common = dict(crs=CRS, resolution=args.resolution, bounds=bounds, height_range=HEIGHT_RANGE, tile=args.tile)
        r_site = {"scenes": [s.name for s in scenes]}
        for corrected in (True, False):
            tag = "corrected" if corrected else "plain"
            t0 = time.time()
            r = sat.multi_view_dsm(scenes, out / f"multi_{tag}.tif", max_pairs=args.max_pairs, correct_bias=corrected,
                                   **common)
            seconds = time.time() - t0
            r_site[f"multi_{tag}"] = {"seconds": seconds, "pairs": [{k: v for k, v in pr.items() if k != "scenes"}
                                                                   for pr in r["pairs"]],
                                      "bias": None if r["bias"] is None else {k: r["bias"][k] for k in (
                                          "shifts", "tracks", "rms_before", "rms_after", "median_before", "median_after")},
                                      "filled": r["filled"], **evaluate(r["path"], gt, inside, transform, crs, args.resolution)}
            singles = []
            for k, pr in enumerate(r["pairs"]):  # every pair of the fusion alone: the best one is reported
                i, j = pr["pair"]
                t0 = time.time()
                rp = sat.multi_view_dsm([scenes[i], scenes[j]], out / f"pair{k}_{tag}.tif", correct_bias=corrected,
                                        min_count=1, **common)
                singles.append({"seconds": time.time() - t0, "pair": [scenes[i].name, scenes[j].name],
                                "angle": rp["pairs"][0]["angle"], "base_to_height": rp["pairs"][0]["base_to_height"],
                                "days": rp["pairs"][0]["days"], "filled": rp["filled"],
                                **evaluate(rp["path"], gt, inside, transform, crs, args.resolution)})
            best = max(singles, key=lambda x: x["aligned"]["completeness"])
            r_site[f"pair_{tag}"] = {**best, "others": [{"angle": x["angle"], "aligned": x["aligned"]} for x in singles]}
            print(site, tag, json.dumps({k: (v["raw"], v["aligned"]) for k, v in r_site.items()
                                         if isinstance(v, dict) and "raw" in v}, indent=1), flush=True)
        report[site] = r_site
    (args.out / "report.json").write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps(report, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
