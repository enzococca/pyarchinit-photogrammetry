"""Benchmark of pyarchinit-photogrammetry on Tell Sultan: the same steps and resolutions as the
other programs, each timed, in one process (as the Metashape script).

    python bench_ppg.py PHOTOS OUT

Profile: features on images at 1/2 (quality "high"), depth maps at 1/4 ("medium", moderate
filtering), mesh from the dense cloud (medium faces) with a 4096 texture, DEM from the dense cloud,
orthomosaic on the DEM. UTM from the GPS of the photos.
"""
import json
import os
import platform
import sys
import time
from pathlib import Path

import pyarchinit_photogrammetry as ppg

PHOTOS, OUT = Path(sys.argv[1]), Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
times = {}
stats = {"software": f"pyarchinit-photogrammetry {ppg.__version__}", "machine": platform.platform()}
try:
    import ppg_core

    stats["native_core"] = getattr(ppg_core, "__version__", "yes")
except ImportError:
    stats["native_core"] = None


def save():
    json.dump({"times_s": times, "stats": stats}, open(OUT / "ppg_times.json", "w"), indent=1)


def timed(name, function):
    t0 = time.perf_counter()
    result = function()
    times[name] = round(time.perf_counter() - t0, 1)
    print(f"BENCH {name}: {times[name]} s", flush=True)
    doc.save()
    save()
    return result


doc = ppg.Document()
chunk = doc.add_chunk()
doc.chunk = chunk
doc.save(OUT / "tell_sultan_ppg.ppg")
photos = sorted(PHOTOS.glob("*.JPG"))
stats["photos"] = len(photos)
timed("add_photos", lambda: chunk.add_photos([str(p) for p in photos]))
timed("match_photos", lambda: chunk.match_photos("high", 40000, 8000, "auto"))
timed("align_cameras", lambda: chunk.align_cameras())
stats["aligned"] = len(chunk.aligned_cameras)
stats["crs"] = chunk.crs
stats["tie_points"] = len(chunk.tie_points) if chunk.tie_points is not None else 0
import numpy as np  # noqa: E402
import pycolmap  # noqa: E402


def reprojection_rms(model) -> float:
    """RMS of the reprojection errors of all the observations of the tie points, in pixels of the
    photos (the measure of bench_metashape.py)."""
    rec = pycolmap.Reconstruction(str(model))
    xyz = {pid: p.xyz for pid, p in rec.points3D.items()}
    squares, count = 0.0, 0
    for image in rec.images.values():
        seen = [(p.xy, xyz[p.point3D_id]) for p in image.points2D if p.has_point3D()]
        if not seen:
            continue
        pose = image.cam_from_world()
        cam = (pose.rotation.matrix() @ np.array([s[1] for s in seen]).T).T + pose.translation
        err = np.array(rec.cameras[image.camera_id].img_from_cam(cam)) - np.array([s[0] for s in seen])
        squares += float((err ** 2).sum())
        count += len(seen)
    return (squares / count) ** 0.5


stats["reprojection_rms_px"] = round(reprojection_rms(chunk.product_path("reconstruction")), 3)
stats["pairs"] = (chunk.meta.get("sfm") or {}).get("pairs")
stats["mapper"] = (chunk.meta.get("sfm") or {}).get("mapper")
timed("depth_maps", lambda: chunk.build_depth_maps("medium", "moderate", "auto"))
stats["dense_backend"] = (chunk.meta.get("dense") or {}).get("backend")
timed("point_cloud", lambda: chunk.build_point_cloud())
stats["dense_points"] = len(chunk.point_cloud)
timed("model", lambda: chunk.build_model(source="point_cloud", face_count="medium"))
stats["faces"] = int(len(chunk.model.faces))
timed("texture", lambda: chunk.build_texture(4096))
timed("dem", lambda: chunk.build_dem())
timed("ortho", lambda: chunk.build_orthomosaic())
t0 = time.perf_counter()
chunk.export_dem(OUT / "dem.tif")
chunk.export_orthomosaic(OUT / "orthomosaic.tif")
chunk.export_point_cloud(OUT / "point_cloud.ply")
times["export"] = round(time.perf_counter() - t0, 1)
import rasterio  # noqa: E402

for key, path in (("dem", OUT / "dem.tif"), ("ortho", OUT / "orthomosaic.tif")):
    with rasterio.open(path) as src:
        stats[f"{key}_resolution_m"] = round(src.res[0], 4)
        stats[f"{key}_size"] = [src.width, src.height]
doc.save()
save()
print("BENCH done", json.dumps({"times_s": times, "stats": stats}), flush=True)
