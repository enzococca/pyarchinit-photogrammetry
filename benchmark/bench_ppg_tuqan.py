"""Benchmark of pyarchinit-photogrammetry on Tell Tuqan, the flight with the targets: the marker
projections of the surveyor's Metashape project (read by ppg's own import), the alignment and
everything else made from scratch, in one process. The settings profile of the Tell Sultan
benchmark scaled to the 48 Mpx photos: features on images at 1/4 ("medium": 2016 px, as the 1/2
of a 12 Mpx photo), depth maps at 1/8 ("low": 1008 px), moderate filtering, mesh from the dense
cloud (medium faces) with a 4096 texture, DEM from the dense cloud, orthomosaic on the DEM.
Control and check targets from a CSV (label, east, north, height, role) in the UTM zone of the
site; the RTT points (name, east, north, height, ...) checked against the DEM.

    BENCH_CRS=EPSG:xxxxx python bench_ppg_tuqan.py PROJECT.psx PHOTOS TARGETS.csv RTT.csv OUT [SUBSET.txt]
"""
import csv
import json
import os
import platform
import sys
import time
from pathlib import Path

import numpy as np

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.integrations.metashape import import_psx
from pyarchinit_photogrammetry.markers import gcp

PROJECT, PHOTOS, TARGETS, RTT, OUT = (Path(a) for a in sys.argv[1:6])
SUBSET = Path(sys.argv[6]) if len(sys.argv) > 6 else None
OUT.mkdir(parents=True, exist_ok=True)
CRS = os.environ["BENCH_CRS"]  # the projected CRS of the targets (EPSG code of the UTM zone of the site)
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


# the observations of the targets in the photos, from the surveyor's project
src_doc = import_psx(PROJECT, OUT / "import" / "import.ppg", with_model=False)
src = src_doc.chunks[0]
src_cams = {c.key: Path(c.path).name for c in src.cameras}
observations = {m.label: [(src_cams[k], p.x, p.y) for k, p in m.projections.items() if p.pinned is not None]
                for m in src.markers}
targets = {r["label"]: r for r in csv.DictReader(open(TARGETS))}

doc = ppg.Document()
chunk = doc.add_chunk()
doc.chunk = chunk
doc.save(OUT / "tell_tuqan_ppg.ppg")
photos = sorted(PHOTOS.glob("*.JPG"))
if SUBSET:
    names = set(SUBSET.read_text().split())
    photos = [p for p in photos if p.name in names]
stats["photos"] = len(photos)
timed("add_photos", lambda: chunk.add_photos([str(p) for p in photos]))
cams = {Path(c.path).name: c for c in chunk.cameras}
for label, obs in observations.items():
    if label not in targets:
        continue
    m = chunk.add_marker(label)
    for name, x, y in obs:
        if name in cams:
            chunk.add_marker_projection(m, cams[name], x, y, pinned=True)
    m.checkpoint = targets[label]["role"] == "check"
stats["marker_projections"] = {m.label: len(m.projections) for m in chunk.markers}
doc.save()
timed("match_photos", lambda: chunk.match_photos("medium", 40000, 8000, "auto"))
timed("align_cameras", lambda: chunk.align_cameras())
# the coordinates of the targets after the alignment: the chunk is then in the UTM zone of the
# photos' GPS (the import would set the CRS without converting the cameras' positions)
chunk.import_reference(TARGETS, crs=CRS, delimiter=",", columns="nxyz-", skip_rows=1, create_markers=False)
for m in chunk.markers:
    m.reference.accuracy = [0.02, 0.02, 0.03]
stats["control"] = sum(1 for m in chunk.markers if not m.checkpoint and m.reference.location)
stats["check"] = sum(1 for m in chunk.markers if m.checkpoint and m.reference.location)
doc.save()
timed("optimize_cameras", lambda: chunk.optimize_cameras(use_markers=True))
report = chunk.update_transform()
stats["transform"] = {k: v for k, v in report.items() if isinstance(v, (int, float, str, bool, type(None)))}
stats["aligned"] = len(chunk.aligned_cameras)
stats["crs"] = chunk.crs
stats["tie_points"] = len(chunk.tie_points) if chunk.tie_points is not None else 0


def marker_report():
    errors = gcp.marker_errors(chunk)
    stats["markers"] = errors
    for role in ("control", "check"):
        v = [(e["dx"] ** 2 + e["dy"] ** 2, e["dz"] ** 2) for e in errors if e["type"] == role and e["error"] is not None]
        if v:
            stats[f"{role}_rmse_xy_m"] = round(float(np.sqrt(np.mean([a for a, _ in v]))), 4)
            stats[f"{role}_rmse_z_m"] = round(float(np.sqrt(np.mean([b for _, b in v]))), 4)
            stats[f"{role}_rmse_m"] = round(float(np.sqrt(np.mean([a + b for a, b in v]))), 4)


marker_report()
save()
import pycolmap  # noqa: E402


def reprojection_rms(model) -> float:
    rec = pycolmap.Reconstruction(str(model))
    xyz = {pid: p.xyz for pid, p in rec.points3D.items()}
    squares, count = 0.0, 0
    for image in rec.images.values():
        seen = [(p.xy, xyz[p.point3D_id]) for p in image.points2D if p.has_point3D() and p.point3D_id in xyz]
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
save()
timed("depth_maps", lambda: chunk.build_depth_maps("low", "moderate", "auto"))
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
    with rasterio.open(path) as rs:
        stats[f"{key}_resolution_m"] = round(rs.res[0], 4)
        stats[f"{key}_size"] = [rs.width, rs.height]
with rasterio.open(OUT / "dem.tif") as rs:
    dem = rs.read(1, masked=True)
    inv = ~rs.transform
    rtt = []
    for r in csv.DictReader(open(RTT)):
        c, rr = inv * (float(r["east"]), float(r["north"]))
        c, rr = int(np.floor(c)), int(np.floor(rr))
        inside = 0 <= c < rs.width and 0 <= rr < rs.height and not dem.mask[rr, c]
        rtt.append({"name": r["name"], "measured": float(r["height"]),
                    "dem": float(dem[rr, c]) if inside else None,
                    "dz": round(float(dem[rr, c]) - float(r["height"]), 4) if inside else None})
    stats["rtt"] = rtt
    dz = [p["dz"] for p in rtt if p["dz"] is not None]
    if dz:
        stats["rtt_rmse_z_m"] = round(float(np.sqrt(np.mean(np.square(dz)))), 4)
        stats["rtt_median_dz_m"] = round(float(np.median(dz)), 4)
marker_report()
doc.save()
save()
print("BENCH done", json.dumps({"times_s": times, "stats": stats}), flush=True)
