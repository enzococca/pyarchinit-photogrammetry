# pyarchinit-photogrammetry

Open-source photogrammetry engine for archaeology and heritage BIM, integrated with
[PyArchInit](https://github.com/pyarchinit/pyarchinit).

From photos to georeferenced point clouds, meshes, DEMs and orthophotos, with the tools an
excavation needs: coded targets, ground control, sections, pottery profiles, contour lines,
cut/fill volumes, point-cloud classification, Segment Anything 2.1, IFC export, and direct
publishing into a PyArchInit database. Everything is scriptable in Python and runs on CPU,
Apple Silicon included.

> Status: **alpha (0.2)**. Python library, command line and desktop interface (on the same API,
> with a built-in Python console). Italian version: [README.it.md](README.it.md).

## Installation

```bash
pip install "pyarchinit-photogrammetry[all]"      # mesh (Open3D), IFC, PostGIS, desktop GUI
pip install "pyarchinit-photogrammetry[sam]"      # optional: Segment Anything 2.1 (PyTorch)
```

From source:

```bash
git clone https://github.com/enzococca/pyarchinit-photogrammetry
cd pyarchinit-photogrammetry
pip install -e ".[all,dev]"
pytest -q
```

Python 3.10+. Writing geometries into a PyArchInit **SQLite** database needs `mod_spatialite`
(found automatically in QGIS, MacPorts, Homebrew and Linux locations, or set
`SPATIALITE_LIBRARY_PATH`).

On headless Linux servers Open3D needs the EGL/GL libraries:
`sudo apt-get install libegl1 libgl1 libgomp1`.

Use a dedicated virtual environment, not the Anaconda `base` environment: old SciPy builds
there can fail to load on recent macOS (`ImportError ... __thread_bss ... zero-fill section`).

```bash
python3 -m venv ~/venvs/ppg && source ~/venvs/ppg/bin/activate
pip install -U pip && pip install "pyarchinit-photogrammetry[all]"
```

`PROJ_LIB`/`PROJ_DATA` exported by Anaconda or QGIS are handled automatically (the PROJ data
bundled with rasterio is used).

## Try it with the demo data

```bash
ppg demo ~/ppg_demo --process     # synthetic trench photos + 8 targets, pottery meshes, PyArchInit DB
ppg-gui ~/ppg_demo/scavo.ppg      # open the processed project in the interface
```

Or from the interface: **File > Crea e apri progetto demo**. `LEGGIMI.txt` in the folder lists
what to try (sections, contours, pottery profile of the sherd, publishing to PyArchInit).

## Desktop interface

`ppg-gui` (or `ppg gui`) opens the desktop application (Italian UI): workspace with chunks,
cameras, markers and products; 3D view (VTK, with a software fallback renderer); orthomosaic
and DEM view with drawing tools (stratigraphic units, section traces, measurements, spot
heights, SAM 2.1 prompts); photo view with manual marker placement; reference pane with
GCP and check point errors; Python console bound to the project (`doc`, `chunk`); background
processing with progress and cancel; a PyArchInit menu to connect, publish orthophotos, DEMs
and models, and write unit polygons and spot heights. See [docs/gui.md](docs/gui.md).

## Quick start (Python)

```python
import pyarchinit_photogrammetry as ppg

doc = ppg.Document()
chunk = doc.add_chunk("Saggio 1")
chunk.add_photos(["foto/"])
chunk.detect_markers("aruco_4x4_50")          # coded targets, decoded automatically
chunk.match_photos(quality="high")
chunk.align_cameras()
chunk.import_reference("gcp.csv", crs="EPSG:3004")
report = chunk.update_transform()             # GCP georeferencing + accuracy report
print(report["control_rmse"])

chunk.build_depth_maps(quality="medium")
chunk.build_point_cloud()
chunk.build_model()                           # Poisson; surface="height_field" for terrain
chunk.build_texture()
chunk.build_dem()                             # GeoTIFF in the chunk CRS
chunk.build_orthomosaic()                     # GeoTIFF RGBA, true-ortho with occlusion test
chunk.generate_report("report.html")
doc.save("scavo.ppg")
```

The same workflow from the command line:

```bash
ppg markers sheet targets.pdf --codes 0-23 --size-mm 40      # print the targets
ppg process scavo.ppg foto/ --gcp gcp.csv --crs EPSG:3004   # whole pipeline + report
ppg export scavo.ppg --ortho ortho.tif --dem dem.tif --model model.obj --cloud cloud.las
```

Scripts can work on an open project, as in the GUI: `ppg run myscript.py --project scavo.ppg`,
then `ppg.app.document` inside the script (in the GUI: **Strumenti > Esegui script Python**).

## What it does

| Area | Features |
| --- | --- |
| Alignment | SIFT features and matching, incremental SfM (COLMAP via pycolmap), camera calibration, bundle adjustment, tie points, pair selection (exhaustive, sequential, by GNSS position) |
| Dense | Depth maps and fusion on CPU (built-in), COLMAP PatchMatch on NVIDIA GPUs, OpenMVS when installed |
| Products | Point cloud (PLY/LAS 1.4 with classes), mesh (Poisson or 2.5D), texture atlas (OBJ+MTL+JPG), DEM, orthomosaic, hillshade, slope, HTML report |
| Markers | Printable ArUco/AprilTag sheets with scale bar, turntable mats with known layout, sub-pixel detection, triangulation, GCP/check points, scale bars, weighted 7-parameter transformation, any EPSG CRS (geographic GCPs go to the UTM zone) |
| Archaeology | Vertical sections and parallel cross sections, plan slices at a given elevation, DXF/SVG drawings at scale, contour lines, cut/fill volumes, pottery: rotation axis from sherds, profile, rim/base diameter, height, wall thickness, drawing with filled section |
| BIM | IFC4 export with IfcMapConversion + IfcProjectedCRS georeferencing, one element per stratigraphic unit with `Pset_PyArchInit_US` |
| AI | Rule-based classification (ground, vegetation, masonry, noise) with archaeological LAS classes 64-71; Segment Anything 2.1 on orthomosaics and photos, masks projected to the point cloud, automatic candidate units |
| PyArchInit | Reads `config.cfg` and the database (SQLite/SpatiaLite or PostgreSQL/PostGIS) without QGIS; publishes orthophotos, DEMs and 3D models as media with PyArchInit thumbnails, documentation records and footprints, US links, US polygons, spot heights, section traces, volumes |

## PyArchInit integration

```python
from pyarchinit_photogrammetry.pyarchinit import PyArchInit

pa = PyArchInit.connect()          # the database configured in PyArchInit
pa.publish_orthomosaic(chunk, sito="Lagoi", nome_doc="Ortofoto saggio 1",
                       link_us=[("1", "101"), ("1", "102")])
pa.publish_dem(chunk, sito="Lagoi", nome_doc="DEM saggio 1")
pa.publish_model(chunk, sito="Lagoi", nome_doc="Modello saggio 1")
units = pa.us_polygons("Lagoi")    # US polygons as shapely geometries
```

Conventions follow PyArchInit 5 (branch `Stratigraph_00001`): `media_table`,
`media_thumb_table` (`{id}_{name}_thumb.png` 150 px, `{id}_{name}.png` 2008x1417),
`media_to_entity_table` (`US`/`us_table`, `REPERTO`, `CERAMICA`, ...), `documentazione_table`
(`Fotopiano`, `DEM`, `Modello tridimensionale`, source `Fotogrammetria`), footprints in
`pyarchinit_documentazione`, polygons in `pyunitastratigrafiche`, heights in
`pyarchinit_quote`, sections in `pyarchinit_sezioni`, volumes in `computo_metrico_table`.
`entity_uuid` is filled when the column exists. Rasters are copied next to the thumbnail
folder (`<media>/fotogrammetria/<site>/`). See [docs/pyarchinit.md](docs/pyarchinit.md).

## Pottery from a turntable

```bash
ppg markers turntable mat.pdf --n 12 --diameter-mm 180     # also writes mat.csv
```

Photograph the vessel or sherd on the mat, process with `--gcp mat.csv` (local frame, mm
accuracy), then:

```bash
ppg pottery model.obj --out profilo.svg --scale 1 --title "US 101 n. 34"
```

The axis is estimated from the surface normals (works on sherds); with the turntable you
can impose it with `--axis 0,0,0,0,0,1`.

## Accuracy check

The test suite renders a synthetic trench (4 x 3 m, a wall stub, 8 targets at known
EPSG:3004 coordinates, 28 photos with lens distortion) and checks the products
metrically: all photos aligned, control RMSE < 2 mm, independent check points < 3 mm, DEM
median error < 3 mm, orthophoto target positions < 5 mm.

## Architecture

```
pyarchinit_photogrammetry/
  core/        Document, Chunk, Camera, Sensor, Marker, ScaleBar, PointCloud, CRS
  engine/      sfm (pycolmap), dense, meshing, texturing, dem, ortho, visibility
  markers/     coded targets, GCP import/export, georeferencing
  archaeo/     sections, pottery, terrain (contours, volumes), stratigraphy (units)
  bim/         IFC export
  ai/          classification, SAM 2.1
  pyarchinit/  config.cfg, database, media, high-level bridge
  io/          EXIF, exports, report
  gui/         desktop interface (PySide6, pyvista)
  demo.py      demo data set
  cli.py       `ppg` command
```

Projects are a JSON file (`name.ppg`) plus a folder (`name.ppg.files/`) with the data of each
chunk; photo paths inside the project folder are stored relative, so projects can be moved.

## License

GNU GPL v2 or later, like PyArchInit. Third-party engines keep their licences (COLMAP BSD,
OpenMVS AGPL when used as an external program, SAM 2.1 Apache 2.0).

Author: Enzo Cocca
