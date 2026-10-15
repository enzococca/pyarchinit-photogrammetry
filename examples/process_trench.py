"""Process a set of excavation photos with coded targets and GCPs, then publish to PyArchInit.

Usage: python process_trench.py <photo folder> <gcp.csv> <EPSG> <site> [<area>:<us> ...]
"""
import sys

import pyarchinit_photogrammetry as ppg
from pyarchinit_photogrammetry.pyarchinit import PyArchInit

photos, gcp, crs, site = sys.argv[1:5]
links = [tuple(x.split(":")) for x in sys.argv[5:]]

doc = ppg.Document()
chunk = doc.add_chunk("Saggio")
doc.save("saggio.ppg")
chunk.add_photos([photos])
chunk.detect_markers("aruco_4x4_50", progress=ppg.print_progress)
chunk.match_photos(quality="high", progress=ppg.print_progress)
chunk.align_cameras(progress=ppg.print_progress)
chunk.import_reference(gcp, crs=crs)
report = chunk.update_transform()
print("control RMSE (m):", report["control_rmse"])
chunk.build_depth_maps(quality="medium", progress=ppg.print_progress)
chunk.build_point_cloud(progress=ppg.print_progress)
chunk.build_model(progress=ppg.print_progress)
chunk.build_texture(progress=ppg.print_progress)
chunk.build_dem(progress=ppg.print_progress)
chunk.build_orthomosaic(progress=ppg.print_progress)
chunk.generate_report("saggio_report.html")
doc.save()

pa = PyArchInit.connect()
print(pa.publish_orthomosaic(chunk, site, "Ortofoto saggio", link_us=links))
print(pa.publish_dem(chunk, site, "DEM saggio", link_us=links))
print(pa.publish_model(chunk, site, "Modello saggio", link_us=links))
