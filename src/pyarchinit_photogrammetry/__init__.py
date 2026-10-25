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
from ._env import glog_flushes_each_line as _glog_flushes_each_line
from ._env import load_open3d_first as _load_open3d_first
from ._env import use_bundled_proj_data as _use_bundled_proj_data

_glog_flushes_each_line()  # before pycolmap loads: its log is tailed for the progress (see _env.py)

_load_open3d_first()  # Open3D's OpenMP runtime must be bound before rasterio/scipy (see _env.py)

_use_bundled_proj_data()  # before anything opens a CRS (see _env.py)

from .app import app  # noqa: E402
from .core import (ARCHAEO_CLASSES, ASPRS_CLASSES, Camera, Chunk, Document, Marker, PointCloud, Projection,
                   Reference, ScaleBar, Sensor)
from .core.progress import Cancelled, Progress, print_progress
from . import demo  # noqa: E402  (ppg.demo.create_demo)

__version__ = "1.5.1"

__all__ = ["app", "Document", "Chunk", "Camera", "Sensor", "Marker", "Projection", "Reference", "ScaleBar",
           "PointCloud", "ASPRS_CLASSES", "ARCHAEO_CLASSES", "Progress", "Cancelled", "print_progress", "__version__"]
