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
| `optimize_cameras(fit_principal_point=False, use_markers=True, marker_weight=4)` | bundle adjustment, returns the reprojection error (px). With 3 or more control points (markers with an enabled reference, not check points) a free adjustment is followed by one where the markers are fixed points at their reference coordinates, observed in the photos (each measurement weighs `marker_weight` tie point observations): the block bends to fit them, which a similarity alone cannot do. `meta["sfm"]["optimize"]` reports control and check point RMSE before and after (`ppg optimize`) |
| `import_reference(csv, crs, columns="nxyz")`, `export_reference(csv)` | `n` label, `x y z`, `a` accuracy, `-` skip |
| `add_marker`, `add_marker_projection`, `add_scalebar`, `triangulate_markers` | manual markers and scale bars |
| `update_transform()` | weighted 7-parameter fit; returns control/check RMSE and per-marker errors |
| `build_depth_maps(quality, filter_mode, backend)` | backends: auto/cpu/colmap/openmvs |
| `build_point_cloud()`, `build_model(source, surface, face_count, voxel_size, vertex_colors)`, `build_texture(texture_size)` | source: `point_cloud`, `tie_points` or `depth_maps` (see below) |
| `build_dem(resolution, source)`, `build_orthomosaic(resolution, surface, blending)` | GeoTIFF in the chunk CRS |
| `export_point_cloud`, `export_model`, `export_cameras(fmt="csv"/"colmap")`, `export_dem`, `export_orthomosaic`, `generate_report` | world coordinates |

Every processing method accepts `progress=callback(fraction, message)`.

### Mesh from depth maps

`chunk.build_model(source="depth_maps", surface="arbitrary", face_count="medium", voxel_size=None,
vertex_colors=True)` builds the mesh straight from the depth maps of `build_depth_maps()`, without the
dense cloud (like *source data: depth maps*). The depth maps are fused into a truncated signed distance
field (TSDF in an Open3D `VoxelBlockGrid`: only the blocks near the surface are stored) and the surface
is extracted with marching cubes, with vertex colours averaged from the photos. A voxel counts once as
many depth maps see it as the dense-cloud fusion wants for the filtering mode (3 for `moderate`, 2 for
`mild`/`none`); pieces smaller than 1 % of the largest one are dropped. The mesh is decimated to
`face_count` (`high`/`medium`/`low` = 1/5, 1/15, 1/45 of the points the depth maps give at their own
resolution, or a number) and saved as `model.ply` like the Poisson mesh, so texture, DEM, orthomosaic
and sections work as before; `surface="height_field"` re-triangulates the fused surface in 2.5D.
`voxel_size` is in chunk units (metres once georeferenced): by default twice the size of a depth-map
pixel on the surface, raised (with a warning) when the surface would need more than a quarter of the
memory. The fusion runs in a child process (a crash does not close the program; progress and
cancellation work as usual) and needs depth maps of the `cpu` or `colmap` backend (OpenMVS keeps its
own format). `engine.tsdf.fuse_depth_maps(chunk, voxel_size=, min_views=, colors=, memory_gb=)` returns
the fused surface (`FusedMesh`, internal frame) without saving it. On the synthetic trench of the tests
(depth maps at quality `high`) it takes about 4 s and is closer to the true surface than the Poisson
mesh of the dense cloud: median distance 0.9 mm, 90th percentile 2.2 mm (Poisson 1.0 and 2.8 mm).
CLI: `ppg mesh project.ppg --source depth_maps [--voxel-size 0.005]`.

## Modules

| Module | Main functions |
| --- | --- |
| `markers.targets` | `generate_sheet`, `generate_turntable_mat`, `turntable_layout`, `detect` |
| `archaeo.sections` | `section`, `sections_along`, `plan_slice`, `Section.to_dxf/to_svg`, `drawing_scale`, `grid_step` |
| `archaeo.pottery` | `estimate_axis`, `profile_at`, `best_profile`, `Profile.measurements`, `draw` |
| `archaeo.terrain` | `contours` (simplified, optional smoothing), `suggest_interval`, `dem_range`, `export_contours`, `volume`, `hillshade`, `slope_degrees` |
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
  function in a fresh interpreter (used by the Poisson reconstruction). With `progress=callback`
  it relays what `fn` passes to `engine.isolate.report(fraction, message)` (used by the fusion of
  the depth maps).
