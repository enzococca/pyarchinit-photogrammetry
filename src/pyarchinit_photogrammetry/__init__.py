"""pyarchinit-photogrammetry: open-source photogrammetry for archaeology and BIM.

Quick start::

    import pyarchinit_photogrammetry as ppg

    doc = ppg.Document()
    chunk = doc.add_chunk("Saggio 1")
    chunk.add_photos(["foto/"])
    chunk.detect_markers("aruco_4x4_50")
    chunk.match_photos(quality="high")
    chunk.align_cameras()
    chunk.import_reference("gcp.csv", crs="EPSG:3004")
    chunk.update_transform()
    chunk.build_depth_maps(quality="medium")
    chunk.build_point_cloud()
    chunk.build_model()
    chunk.build_dem()
    chunk.build_orthomosaic()
    doc.save("scavo.ppg")
"""
from .app import app
from .core import (ARCHAEO_CLASSES, ASPRS_CLASSES, Camera, Chunk, Document, Marker, PointCloud, Projection,
                   Reference, ScaleBar, Sensor)
from .core.progress import Cancelled, Progress, print_progress

__version__ = "0.1.0"

__all__ = ["app", "Document", "Chunk", "Camera", "Sensor", "Marker", "Projection", "Reference", "ScaleBar",
           "PointCloud", "ASPRS_CLASSES", "ARCHAEO_CLASSES", "Progress", "Cancelled", "print_progress", "__version__"]
