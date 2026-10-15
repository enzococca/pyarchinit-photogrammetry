# Python API

```python
import pyarchinit_photogrammetry as ppg
```

## Document and chunks

| Call | Description |
| --- | --- |
| `ppg.Document(path=None)`, `Document.open(path)`, `doc.save(path=None)` | project file `.ppg` + folder `.ppg.files/` |
| `doc.add_chunk(label)`, `doc.chunks`, `doc.chunk` | chunks; `doc.chunk` is the active one (assign to change it) |
| `ppg.app.document` | the project shared by scripts, CLI (`ppg run`) and GUI |

## Chunk workflow

| Method | Notes |
| --- | --- |
| `add_photos(paths, recursive=False)` | files or folders; sensors grouped by body, lens and resolution; EXIF GPS becomes camera reference |
| `detect_markers(target_type="aruco_4x4_50")` | creates markers `target <code>` with projections |
| `match_photos(quality, keypoint_limit, pair_selection)` | quality: ultra/high/medium/low/lowest; pairs: auto/exhaustive/sequential/reference |
| `align_cameras()` | resets the transform; re-georeferences automatically when references exist. Photos are aligned with one focal length (square pixels): fitting fx and fy separately can drift into a degenerate solution. The result stays in the sensor model (`OPENCV` with fx = fy, no tangential distortion); a sensor calibrated with fx != fy or tangential distortion is aligned as it is, and `optimize_cameras()` refines every parameter |
| `optimize_cameras(fit_principal_point=False)` | bundle adjustment, returns the reprojection error (px) |
| `import_reference(csv, crs, columns="nxyz")`, `export_reference(csv)` | `n` label, `x y z`, `a` accuracy, `-` skip |
| `add_marker`, `add_marker_projection`, `add_scalebar`, `triangulate_markers` | manual markers and scale bars |
| `update_transform()` | weighted 7-parameter fit; returns control/check RMSE and per-marker errors |
| `build_depth_maps(quality, filter_mode, backend)` | backends: auto/cpu/colmap/openmvs |
| `build_point_cloud()`, `build_model(surface, face_count)`, `build_texture(texture_size)` | |
| `build_dem(resolution, source)`, `build_orthomosaic(resolution, surface, blending)` | GeoTIFF in the chunk CRS |
| `export_point_cloud`, `export_model`, `export_cameras(fmt="csv"/"colmap")`, `export_dem`, `export_orthomosaic`, `generate_report` | world coordinates |

Every processing method accepts `progress=callback(fraction, message)`.

## Modules

| Module | Main functions |
| --- | --- |
| `markers.targets` | `generate_sheet`, `generate_turntable_mat`, `turntable_layout`, `detect` |
| `archaeo.sections` | `section`, `sections_along`, `plan_slice`, `Section.to_dxf/to_svg` |
| `archaeo.pottery` | `estimate_axis`, `profile_at`, `best_profile`, `Profile.measurements`, `draw` |
| `archaeo.terrain` | `contours`, `export_contours`, `volume`, `hillshade`, `slope_degrees` |
| `archaeo.stratigraphy` | `UnitPolygon`, `mask_to_polygons`, `export_units` |
| `bim.ifc` | `export_ifc`, `export_chunk_ifc`, `split_mesh_by_units` |
| `ai.classify` | `classify_chunk`, `assign_class_by_polygons`, `class_statistics` |
| `ai.sam` | `Sam2Segmenter`, `segment_orthomosaic`, `auto_units`, `classify_from_masks`, `segment_photo_and_classify` |
| `pyarchinit` | `PyArchInit`, `register_media`, `connect` |

## Coordinate conventions

* Internal frame = frame of the SfM solution; `chunk.transform` (similarity) maps it to the
  chunk CRS. Dense cloud and mesh are stored internally; rasters and exports are in the CRS.
* Image coordinates follow COLMAP: the centre of the first pixel is (0.5, 0.5).
* Cameras: OpenCV axes (+X right, +Y down, +Z forward); `camera.transform` is
  `world_from_cam` in the internal frame.

## Demo data and interface

| Call | Description |
| --- | --- |
| `ppg.demo.create_demo(folder, process=False)` | synthetic trench photos + GCPs, project, pottery meshes, PyArchInit DB (`DemoData`) |
| `pyarchinit_photogrammetry.gui.main(argv)` | starts the desktop interface (`ppg-gui`) |
| `ppg.core.progress.Progress` | progress callback with `sub()` ranges; `cancel()` stops every sub-step |


## Added in 0.3

* `chunk.crop_point_cloud(zmin=, zmax=, radius=, center=(x, y), bounds=(xmin, ymin, xmax, ymax))`
  keeps the dense points inside the limits (world coordinates) and drops the model products.
* `PyArchInit.connect(url=None, config_path=None, media_root=None)`, `pa.ensure_site(sito, ...)`,
  `pa.site_exists(sito)`, `pa.label`.
* `pyarchinit.demo_db.create_demo_database(home, size="small"|"large", overwrite=False, demo_content=True)`
  with the PyArchInit 5 schema (`pyarchinit5_schema.sql`).
* `demo.create_demo(..., with_pottery=True)` and `demo.process_pottery_demo(doc, profile_svg)`;
  `testing.synthetic.make_turntable_scene()`, `render_mesh()`, `write_pottery_dataset()`.
* `engine.isolate.run_isolated(fn, *args, timeout=, cancel_check=)` runs a module-level
  function in a fresh interpreter (used by the Poisson reconstruction).
