"""Benchmark of Metashape Pro 2.1 on Tell Tuqan, the flight with the targets: the marker
projections of the surveyor's project, the alignment and everything else made again from scratch.
The settings profile of the Tell Sultan benchmark scaled to the 48 Mpx photos: features on images
at 1/4 (downscale 4: 2016 px, as the 1/2 of a 12 Mpx photo), depth maps at 1/8 (downscale 8:
1008 px), moderate filtering, mesh from the depth maps (medium faces), texture 4096, DEM from the
dense cloud, orthomosaic on the DEM. Control and check targets from a CSV (label, east, north,
height, role) in the UTM zone of the site. Run with:

    BENCH_CRS=EPSG:xxxxx MetashapePro -r bench_metashape_tuqan.py PROJECT.psx PHOTOS TARGETS.csv OUT
"""
import csv
import json
import os
import platform
import sys
import time

import Metashape

args = [a for a in sys.argv[1:] if not a.startswith("-")]
PROJECT, PHOTOS, TARGETS, OUT = args[-4:]
os.makedirs(OUT, exist_ok=True)
WGS84 = Metashape.CoordinateSystem("EPSG::4326")
UTM = Metashape.CoordinateSystem("EPSG::" + os.environ["BENCH_CRS"].split(":")[-1])  # the projected CRS of the targets
times, stats = {}, {"software": "Metashape Pro " + Metashape.app.version, "machine": platform.platform()}


def save_stats():
    json.dump({"times_s": times, "stats": stats}, open(os.path.join(OUT, "metashape_times.json"), "w"), indent=1)


def timed(name, function):
    t0 = time.perf_counter()
    function()
    times[name] = round(time.perf_counter() - t0, 1)
    print(f"BENCH {name}: {times[name]} s", flush=True)
    doc.save()
    save_stats()


doc = Metashape.Document()
doc.open(PROJECT)
chunk = doc.chunks[0]
doc.save(os.path.join(OUT, "tell_tuqan_metashape.psx"))
for cam in chunk.cameras:  # the photos as the surveyor keeps them (read only)
    cam.photo.path = os.path.join(PHOTOS, os.path.basename(cam.photo.path))
stats["photos"] = len(chunk.cameras)
targets = {r["label"]: r for r in csv.DictReader(open(TARGETS))}
for m in list(chunk.markers):
    r = targets.get(m.label)
    if r is None:  # target 14: one projection, nothing can be measured
        chunk.remove([m])
        continue
    m.reference.location = Metashape.Vector([float(r["east"]), float(r["north"]), float(r["height"])])
    m.reference.accuracy = Metashape.Vector([0.02, 0.02, 0.03])
    m.reference.enabled = r["role"] == "control"
stats["control"] = sum(1 for m in chunk.markers if m.reference.enabled)
stats["check"] = sum(1 for m in chunk.markers if not m.reference.enabled)
if hasattr(chunk, "camera_crs"):
    chunk.camera_crs = WGS84  # the GPS of the photos
if hasattr(chunk, "marker_crs"):
    chunk.marker_crs = None
chunk.crs = UTM
for s in chunk.sensors:  # calibrated again from scratch
    s.user_calib = None
    s.fixed = False
timed("match_photos", lambda: chunk.matchPhotos(downscale=4, generic_preselection=True, reference_preselection=True,
                                                keypoint_limit=40000, tiepoint_limit=8000, reset_matches=True))
timed("align_cameras", lambda: chunk.alignCameras(reset_alignment=True))
timed("optimize_cameras", lambda: chunk.optimizeCameras())
chunk.updateTransform()
stats["aligned"] = sum(1 for c in chunk.cameras if c.transform is not None)
stats["tie_points"] = len(chunk.tie_points.points) if chunk.tie_points is not None else 0


def marker_report():
    out, sq = [], {"control": [], "check": []}
    T = chunk.transform.matrix
    for m in chunk.markers:
        role = "control" if m.reference.enabled else "check"
        e = {"label": m.label, "role": role, "projections": len([c for c in chunk.cameras if m.projections[c] is not None])}
        if m.position is not None and m.reference.location is not None:
            est = chunk.crs.project(T.mulp(m.position))
            d = est - m.reference.location
            e.update(dx=round(d.x, 4), dy=round(d.y, 4), dz=round(d.z, 4), error=round(d.norm(), 4))
            sq[role].append((d.x ** 2 + d.y ** 2, d.z ** 2))
        out.append(e)
    stats["markers"] = out
    for role, v in sq.items():
        if v:
            stats[f"{role}_rmse_xy_m"] = round((sum(a for a, _ in v) / len(v)) ** 0.5, 4)
            stats[f"{role}_rmse_z_m"] = round((sum(b for _, b in v) / len(v)) ** 0.5, 4)
            stats[f"{role}_rmse_m"] = round((sum(a + b for a, b in v) / len(v)) ** 0.5, 4)


marker_report()
save_stats()
timed("depth_maps", lambda: chunk.buildDepthMaps(downscale=8, filter_mode=Metashape.ModerateFiltering))
timed("point_cloud", lambda: chunk.buildPointCloud(source_data=Metashape.DepthMapsData, point_colors=True,
                                                   point_confidence=True))
stats["dense_points"] = chunk.point_cloud.point_count if chunk.point_cloud is not None else 0
timed("model", lambda: chunk.buildModel(surface_type=Metashape.Arbitrary, source_data=Metashape.DepthMapsData,
                                        face_count=Metashape.MediumFaceCount, interpolation=Metashape.EnabledInterpolation))
stats["faces"] = len(chunk.model.faces) if chunk.model is not None else 0


def texture():
    chunk.buildUV(mapping_mode=Metashape.GenericMapping, texture_size=4096)
    chunk.buildTexture(blending_mode=Metashape.MosaicBlending, texture_size=4096)


timed("texture", texture)
timed("dem", lambda: chunk.buildDem(source_data=Metashape.PointCloudData, interpolation=Metashape.EnabledInterpolation))
timed("ortho", lambda: chunk.buildOrthomosaic(surface_data=Metashape.ElevationData,
                                              blending_mode=Metashape.MosaicBlending))
projection = Metashape.OrthoProjection()
projection.crs = UTM
t0 = time.perf_counter()
chunk.exportRaster(os.path.join(OUT, "dem.tif"), source_data=Metashape.ElevationData, projection=projection)
chunk.exportRaster(os.path.join(OUT, "orthomosaic.tif"), source_data=Metashape.OrthomosaicData, projection=projection,
                   save_alpha=True)
chunk.exportPointCloud(os.path.join(OUT, "point_cloud.ply"), source_data=Metashape.PointCloudData, crs=UTM,
                       save_point_color=True, save_point_confidence=True)
chunk.exportCameras(os.path.join(OUT, "cameras.xml"))
chunk.exportReport(os.path.join(OUT, "report.pdf"))
times["export"] = round(time.perf_counter() - t0, 1)
stats["dem_resolution_m"] = chunk.elevation.resolution if chunk.elevation is not None else None
stats["ortho_resolution_m"] = chunk.orthomosaic.resolution if chunk.orthomosaic is not None else None
errors = []
by_track = {p.track_id: p for p in chunk.tie_points.points}
for camera in chunk.cameras:  # reprojection error of the tie points, in pixels
    if camera.transform is None:
        continue
    for proj in chunk.tie_points.projections[camera]:
        point = by_track.get(proj.track_id)
        if point is None or not point.valid:
            continue
        p = camera.project(point.coord)
        if p is not None:
            errors.append((p - proj.coord).norm())
if errors:
    stats["reprojection_rms_px"] = round((sum(e * e for e in errors) / len(errors)) ** 0.5, 3)
marker_report()
doc.save()
save_stats()
print("BENCH done", json.dumps({"times_s": times, "stats": stats}), flush=True)
Metashape.app.quit()
