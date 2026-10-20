"""Benchmark of Metashape Pro 2.1 on Tell Sultan: the same steps and resolutions as the other
programs, each timed. Run with:

    /Applications/MetashapePro.app/Contents/MacOS/MetashapePro -platform offscreen -r bench_metashape.py PHOTOS OUT

Profile: features on images at 1/2 (downscale 2), depth maps at 1/4 (downscale 4, moderate
filtering), mesh from the depth maps (medium faces), texture 4096, DEM from the dense cloud,
orthomosaic on the DEM; everything in the UTM zone of the photos (from their GPS).
"""
import glob
import json
import os
import platform
import sys
import time

import Metashape

args = [a for a in sys.argv[1:] if not a.startswith("-")]
PHOTOS, OUT = args[-2], args[-1]
os.makedirs(OUT, exist_ok=True)
WGS84 = Metashape.CoordinateSystem("EPSG::4326")
times, stats = {}, {"software": "Metashape Pro " + Metashape.app.version, "machine": platform.platform()}


def timed(name, function):
    t0 = time.perf_counter()
    function()
    times[name] = round(time.perf_counter() - t0, 1)
    print(f"BENCH {name}: {times[name]} s", flush=True)
    doc.save()
    json.dump({"times_s": times, "stats": stats}, open(os.path.join(OUT, "metashape_times.json"), "w"), indent=1)


doc = Metashape.Document()
doc.save(os.path.join(OUT, "tell_sultan_metashape.psx"))
chunk = doc.addChunk()
photos = sorted(glob.glob(os.path.join(PHOTOS, "*.JPG")))
stats["photos"] = len(photos)
timed("add_photos", lambda: chunk.addPhotos(photos))
lon, lat = next(c.reference.location for c in chunk.cameras if c.reference.location is not None)[:2]
UTM = Metashape.CoordinateSystem(f"EPSG::{(32600 if lat >= 0 else 32700) + int((lon + 180) // 6) + 1}")
if hasattr(chunk, "camera_crs"):
    chunk.camera_crs = WGS84
chunk.crs = UTM
timed("match_photos", lambda: chunk.matchPhotos(downscale=2, generic_preselection=True, reference_preselection=True,
                                                keypoint_limit=40000, tiepoint_limit=8000))
timed("align_cameras", lambda: chunk.alignCameras())
chunk.updateTransform()
stats["aligned"] = sum(1 for c in chunk.cameras if c.transform is not None)
stats["tie_points"] = len(chunk.tie_points.points) if chunk.tie_points is not None else 0
timed("depth_maps", lambda: chunk.buildDepthMaps(downscale=4, filter_mode=Metashape.ModerateFiltering))
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
doc.save()
json.dump({"times_s": times, "stats": stats}, open(os.path.join(OUT, "metashape_times.json"), "w"), indent=1)
print("BENCH done", json.dumps({"times_s": times, "stats": stats}), flush=True)
Metashape.app.quit()
