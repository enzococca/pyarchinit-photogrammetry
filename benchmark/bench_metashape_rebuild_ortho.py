"""The Metashape project of the Tell Tuqan benchmark kept only the sparse cloud: the DEM and the
orthomosaic are built again (not timed: the times of the run stand) and exported as BigTIFF.

    BENCH_CRS=EPSG:xxxxx MetashapePro -r bench_metashape_rebuild_ortho.py PHOTOS OUT
"""
import json
import os
import sys
import time

import Metashape

PHOTOS, OUT = [a for a in sys.argv[1:] if not a.startswith("-")][-2:]
UTM = Metashape.CoordinateSystem("EPSG::" + os.environ["BENCH_CRS"].split(":")[-1])  # the projected CRS of the targets
doc = Metashape.Document()
doc.open(os.path.join(OUT, "tell_tuqan_metashape.psx"))
chunk = doc.chunks[0]
for cam in chunk.cameras:  # the saved project lost the folder of the photos
    cam.photo.path = os.path.join(PHOTOS, os.path.basename(cam.photo.path))
report = json.load(open(os.path.join(OUT, "metashape_times.json")))
times, stats = report["times_s"], report["stats"]
print("BENCH rebuild start: elevation", chunk.elevation is not None, "orthomosaic", chunk.orthomosaic is not None, flush=True)
if chunk.elevation is None:
    # the DEM from the tie points would be poor: from the exported DEM of the run instead
    chunk.importRaster(os.path.join(OUT, "dem.tif"), raster_type=Metashape.ElevationData)
    doc.save()
print("BENCH elevation ready", flush=True)
chunk.buildOrthomosaic(surface_data=Metashape.ElevationData, blending_mode=Metashape.MosaicBlending)
doc.save()
print("BENCH orthomosaic built", flush=True)
projection = Metashape.OrthoProjection()
projection.crs = UTM
big = Metashape.ImageCompression()
big.tiff_big = True
big.tiff_tiled = True
big.tiff_overviews = True
big.tiff_compression = Metashape.ImageCompression.TiffCompressionLZW
t0 = time.perf_counter()
chunk.exportRaster(os.path.join(OUT, "orthomosaic.tif"), source_data=Metashape.OrthomosaicData, projection=projection,
                   save_alpha=True, image_compression=big)
chunk.exportCameras(os.path.join(OUT, "cameras.xml"))
try:
    chunk.exportReport(os.path.join(OUT, "report.pdf"))
except Exception as exc:
    print("report:", exc)
times["export"] = round(time.perf_counter() - t0, 1)
stats["ortho_resolution_m"] = chunk.orthomosaic.resolution if chunk.orthomosaic is not None else None
stats["dem_resolution_m"] = chunk.elevation.resolution if chunk.elevation is not None else None
errors = []
by_track = {p.track_id: p for p in chunk.tie_points.points}
for camera in chunk.cameras:
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
json.dump({"times_s": times, "stats": stats}, open(os.path.join(OUT, "metashape_times.json"), "w"), indent=1)
print("BENCH rebuild done", json.dumps({k: v for k, v in stats.items() if k != "markers"}), flush=True)
Metashape.app.quit()
