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
| `add_marker`, `add_marker_projection`, `add_scalebar`, `triangulate_markers` | manual markers and scale bars (see *Scale bars*) |
| `update_transform(use_cameras=None)` | weighted 7-parameter fit; returns control/check RMSE, per-marker and per-bar errors |
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

## Align and merge chunks

Chunks are aligned separately (each in its own arbitrary frame), brought into one frame,
then merged, as with *Workflow > Align Chunks* and *Merge Chunks*.

| Call | Description |
| --- | --- |
| `doc.align_chunks(reference, chunks=None, method="markers", fix_scale=False, initial="auto", max_points=200_000, progress=None)` | moves `chunks` (Chunk objects, keys or labels; default: the other enabled chunks) into the world frame and CRS of `reference`; returns a report |
| `doc.merge_chunks(chunks, label=None, merge_markers=True, merge_tie_points=True, merge_point_clouds=True, merge_models=False, progress=None)` | new chunk (default label `Merged chunk`) in the frame of the first chunk; the inputs are kept |
| `ppg align-chunks project.ppg [CHUNKS...] -r REF --method markers\|points\|cameras [--fix-scale] [--initial ...] [--json]` | command line (default reference: the active chunk) |
| `ppg merge-chunks project.ppg [CHUNKS...] [--label L] [--no-markers] [--no-tie-points] [--no-point-clouds] [--models] [--activate]` | command line (default: all enabled chunks) |

Methods of `align_chunks`:

* `markers`: markers with the same label in both chunks (at least 3, with a position),
  least squares (Umeyama).
* `cameras`: photos present in both chunks (the same file, or the same label when it is
  unique in each chunk). Least squares on the camera centres; with 1-2 common photos, or
  photos along a line, the rotation comes from the camera orientations.
* `points`: registration of the dense clouds (tie points for a chunk without one). Initial
  position from the common markers, else the georeference (both chunks in a projected CRS,
  or referenced in the same local grid), else the common photos, else a global registration
  (FPFH descriptors from Open3D, matched with a seeded RANSAC: needs the `[mesh]` extra);
  `initial="markers"|"reference"|"cameras"|"global"` forces one. A scaled point-to-plane
  ICP refines it; the result is accepted when most overlapping points fit and, with colours,
  the brightness of matched points agrees (else `ValueError`).
* `fix_scale=True`: rigid alignment that keeps the scale of each chunk; only for chunks already
  in metres (CRS, references or scale bars), else `ValueError`.

Only `chunk.transform` and `chunk.crs` change; photos, tie points, dense cloud and mesh stay
in the chunk's internal frame. Estimates use local frames (world coordinates minus a rounded
offset), so national grid/UTM coordinates lose no precision. An unlevelled local reference is
levelled first (as `build_dem` would do later). If one chunk cannot be aligned, nothing changes.
Marker references of a chunk in another projected CRS are converted to the new one. The
DEM and orthomosaic of an aligned chunk are in its old frame: build them again
(`stale_products` in the report).

The report: `{"reference": {"key", "label"}, "method", "fix_scale", "crs", "levelled_reference",
"chunks": [...]}`, one entry per chunk with `key`, `label`, `method`, `correspondences`, `rmse`
(reference world units, metres once georeferenced or scaled), `scale` (scale applied),
`transform` (the new `chunk.transform`), `delta` (old world -> reference world), `stale_products`;
`residuals` (per marker or photo: `error`, `dx`, `dy`, `dz`; `angle_deg` for photos) and
`max_error` for markers/cameras; `initial`, `source`/`target` (`point_cloud` or `tie_points`),
`fitness`, `overlap`, `inlier_ratio`, `color_corr`, `attempts` and `check` (residuals of the
common markers, not used by the registration) for points. `chunk.meta["chunk_alignment"]`
keeps a summary.

`merge_chunks` needs chunks in one frame (aligned, or georeferenced in projected CRSs; another
CRS is converted through a fitted similarity, with a warning above 5 mm). The merged chunk has
the CRS, transform and internal frame of the first chunk, and contains:

* sensors and cameras with new keys; a photo present in several chunks is kept once (the
  first aligned copy). Identical calibrations stay separate sensors.
* markers merged by label (projections combined, positions triangulated again), or all
  kept with `merge_markers=False`; scale bars remapped.
* tie points, dense clouds (colours, normals, classes) and, with `merge_models=True`,
  meshes (vertex colours) concatenated in the common frame.
* a COLMAP model of the merged cameras (`engine.sfm.write_reconstruction_from_chunk`,
  with the merged tie points as 3D points), so depth maps, dense cloud, mesh, DEM and
  orthomosaic can be built again on the merged chunk.

DEMs, orthomosaics and textures are not merged. `merged.meta["merge"]` lists the source
chunks, the photos found in more than one chunk and warnings.

## Products from chosen point classes

The classes of the dense cloud (rules, lasso, trained classifier or stratigraphic units)
choose what goes into the products. Noise (`NOISE_CLASSES` = 7, 18) never makes a DEM or a mesh.

| Call | Description |
| --- | --- |
| `chunk.point_classes()` | points per class of the dense cloud, reading only its class column (`PointCloud.read_class_counts(path)`) |
| `chunk.build_dem(..., classes=[2])` | DEM from these classes only: `[2]` gives a terrain model (DTM) without vegetation, people or tools; the gaps under what is left out are interpolated; `meta["dem"]["classes"]` |
| `chunk.build_orthomosaic(..., hide_classes=[3, 71])` | transparent where the visible surface (highest dense point, gridded at the point spacing and widened by one cell) is of these classes; `engine.ortho.hidden_cells(chunk, origin, shape, resolution, classes)`; `meta["orthomosaic"]["hidden_classes"]` |
| `chunk.build_model(source="point_cloud", ..., classes=[2, 64])` | mesh from these classes (the surface closes over what is left out); only with the dense cloud as source; `meta["model"]["classes"]` |
| `sections.section(chunk, p1, p2, source=, classes=)`, `sections_along(..., classes=)`, `plan_slice(chunk, z, classes=)` | from the dense cloud, only the points of these classes; from the model, only the stretches of the cut whose middle is nearest to a point of these classes and whose ends lie on such points (not the parts the mesh interpolates over what is left out) |
| `chunk.export_point_cloud(path, classes=[64])` | only these classes (LAS keeps the class codes) |
| `PointCloud.class_mask(classes=None, exclude=())` | boolean mask of the points to use |
| `classify.classify_by_units(chunk, units, base_code=100, keep_manual=True)` | one class per stratigraphic unit (`unit_class_name`: `US 12 (area 1)`), points on the edge included; free codes from 100, the same code when run again (the old extent goes back to 1); returns per unit `name`, `code`, `sito`, `area_s`, `us`, `points`, `area_m2` (plan, of the polygons), `z_min`, `z_mean`, `z_max`; `units_table_csv(rows, path)` writes them (semicolons) |

Command line: `ppg dem scavo.ppg --classes 2`, `ppg ortho scavo.ppg --hide-classes 3,71`,
`ppg mesh scavo.ppg --classes 2,64`, `ppg section scavo.ppg x1 y1 x2 y2 --classes 2`,
`ppg export scavo.ppg --cloud muri.las --classes 64`,
`ppg classify-units scavo.ppg [--pyarchinit-site SITO] [--csv us.csv] [--no-keep-manual]`
(the units drawn in the project, or the US polygons of a PyArchInit site).

## Versions of the products

`core.versions` keeps earlier results of the dense cloud, the model, the DEM and the
orthomosaic to compare them. The *active* version is the product every tool uses (the usual
files of the chunk); kept versions are in `versions/<product>/<id>/` inside the chunk folder,
listed in `chunk.meta["versions"]` with name, date and the parameters they were built with.

| Call | Description |
| --- | --- |
| `versions.keep_active(chunk, product, move=False)` | puts the active version among the kept ones (copied; moved with `move` when its files are inside the chunk folder: a file of another program is always copied); returns the entry |
| `versions.restore(chunk, product, entry)` | undoes a `keep_active(move=True)` (a build that failed) |
| `versions.set_active_name(chunk, product, name)`, `active_info(chunk, product)` | name and date of the active version (by default `DEM del 16/10 11:20 (classi 2)`) |
| `versions.versions(chunk, product)`, `version_file(chunk, product, id)` | kept versions, main file of one |
| `versions.activate(chunk, product, id)` | makes a kept version active; the active one is kept in its place |
| `versions.rename(chunk, product, id_or_None, name)`, `delete(chunk, product, id)` | |
| `versions.dem_difference(chunk, id, out=None)` | active DEM minus a kept one on the grid of the active one (GeoTIFF float32): surface minus terrain = height of walls and vegetation, after minus before = excavated depth |

`chunk.build_depth_maps(..., skip_unreadable=True)` leaves out photos that cannot be read
(`chunk.unreadable_photos()`: missing, empty placeholders of files kept online only, not
images); without it they raise `engine.visibility.UnreadablePhotos` (also importable from
`engine.dense`). `build_texture` and `build_orthomosaic` take the same option;
`chunk.relink_photos(folder, only_unreadable=False)` points the photos to the files with the same
names in another folder (COLMAP then reads them from their common folder, or from a folder of
links when they are split, `engine.sfm.photo_folder`). The COLMAP model written
from imported poses refers only to points with two observations; `engine.sfm.repair_reconstruction(chunk)`
fixes models written by earlier versions (done before the depth maps and the camera optimisation).

## Confidence, cleaning and cropping

| Call | Description |
| --- | --- |
| `chunk.compute_confidence("point_cloud" or "model", tol=0.01)` | depth maps agreeing on each point or vertex (within `tol` of the distance; half an edge for a vertex), stored as the `confidence` property of the PLY (and an extra bytes dimension of LAS exports); returns the statistics |
| `engine.confidence.depth_map_support(chunk, points, tol, abs_tol=None)` | the same count for any points of the internal frame |
| `chunk.clean_point_cloud(min_confidence=, remove_classes=, statistical=(k, std), sparse=(radius, n), groups=(distance, n), color=((r, g, b), tol))` | removes what `engine.cleaning.point_filters` selects (distances 0 = automatic from the point spacing); returns the counts per filter |
| `chunk.clean_model(min_confidence=, min_component=, max_edge=, repair=True, fill_holes=, smooth=, target_faces=)` | model cleaning (`engine.cleaning.face_filters` for the counts) |
| `chunk.clean_tie_points(max_reprojection_error=, min_image_count=, max_reconstruction_uncertainty=, max_projection_accuracy=, optimize=False)` | gradual selection (`engine.tiepoints.tie_point_metrics`, `select_tie_points`, `remove_tie_points`) |
| `engine.cleaning.delete_points(chunk, mask, keep_selected=False)`, `delete_faces(...)` | Metashape's delete / crop selection |
| `chunk.export_dem(path, region=)`, `export_orthomosaic(path, region=)`, `export_model(path, region=, zmin=, zmax=)`, `export_point_cloud(path, region=, zmin=, zmax=)` | exports cut by a polygon in the chunk CRS (an `(N, 2)` ring, a box, WKT, GeoJSON or a shapely geometry: `engine.crop.as_polygon`) |
| `chunk.crop_dem(region)`, `crop_orthomosaic(region)`, `crop_model(region, zmin, zmax)`, `crop_point_cloud(region=)` | the same in the project; a product that is a file of another program is never overwritten (`chunk.own_product_path`) |

`chunk.build_orthomosaic(..., workers=None, max_angle=70.0)` builds the orthomosaic tile by tile
(the surface read one window at a time, every tile written at once, overviews at the end): any
size, the tiles in parallel, each from the photos that see it best; cells that every photo sees
from further than `max_angle` degrees from the vertical stay transparent.

## Patches of the orthomosaic and of the texture

A patch takes the colours of an area of the orthomosaic from photos chosen by hand: a dict in
`chunk.meta["orthomosaic"]["patches"]`, `{"id", "polygon" [[x, y], ...] (chunk CRS), "photos"
[labels], "mode" "assign"|"exclude", "surface" "dem"|"ground"}` (a later patch covers an earlier one).

| Call | Description |
| --- | --- |
| `chunk.add_ortho_patch(polygon, photos, mode="assign")`, `engine.patches.add_patch(chunk, polygon, photos, mode, surface="dem")` | a new patch; `surface="ground"` projects the area on the ground around it (heights along the polygon, inverse-distance weighted inside) instead of the DEM: trees and vehicles as the photo shows them, holes of the DEM filled |
| `chunk.update_orthomosaic(progress=None, skip_unreadable=False)` | redraws only the cells of the patches added, changed or removed since the file was written, then the overviews of those windows (`engine.overviews.refresh`, through rasterio's GDAL; every overview again when it cannot); an orthomosaic of another program is copied into the chunk first and its pixels come back where a patch is removed |
| `chunk.ortho_patches`, `chunk.remove_ortho_patch(id)`, `engine.patches.clear_patches(chunk)`, `changed_patches(chunk)` | the patches |
| `engine.patches.photos_for_area(chunk, polygon, limit=12)` | the photos that frame the area, best first: `label`, `coverage`, `visible` (share not hidden by the model), `angle`, `gsd`, `score`, `used` (share the orthomosaic takes from it now) |
| `engine.patches.previews(chunk, polygon, photos, max_side=320)` | `({(label, surface): RGBA}, now, bounds)`: the area from each photo on each surface, the rest as it is |
| `chunk.patch_texture(photos, polygon=None, faces=None, mode="assign")`, `engine.patches.photos_for_faces(chunk, faces)`, `preview_faces(chunk, faces, photo)` | colour again the faces of the textured model inside a polygon (seen from above) or chosen by index from the chosen photos; texels no chosen photo sees keep their colour |

`chunk.build_orthomosaic(..., patches=None)` applies the patches of the chunk (pass `[]` to leave
them out). The photos of every tile are ranked on all its cells, so an update gives the same
pixels as a build.

## Scale bars

A scale bar is the known distance between two markers (`core.marker.ScaleBar`: `label`, `marker_a`,
`marker_b` (marker keys), `distance` in metres, `accuracy`, `enabled`, `checkpoint`).

| Call | Description |
| --- | --- |
| `chunk.add_scalebar(marker_a, marker_b, distance, accuracy=0.001, checkpoint=False, label=None)` | markers as objects, labels, keys or target codes; the same pair again changes the bar |
| `chunk.scalebar(label_or_key)`, `chunk.remove_scalebar(label_or_key)`, `chunk.scalebars` | the bars |
| `chunk.import_scalebars(path, accuracy=0.001, delimiter=None)` | `marker_a, marker_b, distance[, accuracy[, check]]` per line; returns `{"bars": [labels], "missing": ["A-B"]}` |
| `markers.gcp.scalebar_errors(chunk)` | every bar: `label`, `marker_a`, `marker_b`, `distance`, `accuracy`, `type` (control/check), `enabled`, `estimated`, `error` (metres) |
| `markers.targets.generate_scale_bar(path, target_type="aruco_4x4_50", codes=None, distance_mm=200, marker_size_mm=40, page="A4", count=1)` | printable bars and their CSV, `(pages, csv)` |

`chunk.update_transform()` uses the control bars (enabled, not check) as follows:

* no control point and no camera reference (or camera references that cover less than ten times
  their accuracy, `cameras_span_their_accuracy`): the scale is the least-squares solution of
  `scale * |a - b| = distance` weighted by `1 / accuracy²` (of two bars measured equally well, the
  longer one counts more); the orientation stays, the origin is the internal one and the CRS
  becomes `LOCAL` (`method == "scalebars"`);
* three or more control points, or camera references: the similarity is the joint weighted
  least-squares fit of the points (`1 / accuracy²` on their 3D residuals) and of the bars
  (`1 / accuracy²` on `scale * length - distance`), `markers.gcp.similarity_with_bars`: the
  rotation and the position come from the points, the scale from points and bars together
  (`"markers+scalebars"`, `"cameras+scalebars"`, `"markers+cameras+scalebars"`);
  `use_cameras=True` or `False` forces the use of the camera references.

The report adds `scale` (world units per internal unit), `scalebars` (as `scalebar_errors`),
`scalebar_control_rmse` and `scalebar_check_rmse` (metres, None without bars of that type).

## Modules

| Module | Main functions |
| --- | --- |
| `markers.targets` | `generate_sheet`, `generate_turntable_mat`, `turntable_layout`, `generate_scale_bar`, `scale_bar_codes`, `detect` |
| `markers.gcp` | `import_reference`, `export_reference`, `triangulate_markers`, `update_transform`, `similarity_with_bars`, `marker_errors`, `scalebar_errors`, `import_scalebars`, `resolve_marker`, `cameras_span_their_accuracy` |
| `engine.chunks` | `align_chunks`, `merge_chunks`, `common_markers`, `common_cameras`, `pose_similarity`, `icp`, `global_guesses`, `ransac_rigid`, `registration_quality` |
| `archaeo.sections` | `section`, `sections_along`, `plan_slice`, `Section.to_dxf/to_svg`, `drawing_scale`, `grid_step` |
| `archaeo.pottery` | `estimate_axis`, `profile_at`, `best_profile`, `Profile.measurements`, `draw` |
| `archaeo.terrain` | `contours` (simplified, optional smoothing), `suggest_interval`, `dem_range`, `export_contours`, `volume`, `hillshade`, `slope_degrees` |
| `archaeo.stratigraphy` | `UnitPolygon`, `mask_to_polygons`, `export_units` |
| `bim.ifc` | `export_ifc`, `export_chunk_ifc`, `split_mesh_by_units` |
| `ai.classify` | `classify_chunk`, `classify_by_units`, `unit_class_name`, `units_table_csv`, `assign_class_by_polygons`, `class_statistics`, `set_class`, `clear_manual`, `class_names` |
| `ai.learn` | `point_features`, `train`, `predict`, `ClassifierModel`, `train_on_chunk`, `classify_chunk_with_model` (see below) |
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


## Trainable point classification

Label some points by hand, train a classifier on them, classify the rest of the cloud and,
with the saved model, the clouds of other projects (like CloudCompare's 3DMASC/CANUPO).
Needs scikit-learn (`pip install "pyarchinit-photogrammetry[learn]"`, included in `[gui]` and `[all]`).

```python
from pyarchinit_photogrammetry.ai import classify, learn

classify.set_class(chunk, lasso_mask, 64, label="Muro")   # class + "labelled by hand" flag
model = learn.train_on_chunk(chunk)                        # learns from the hand-labelled points
print(model.summary())                                     # out-of-bag accuracy, precision/recall per class
pc, confidence = learn.classify_chunk_with_model(chunk, model, min_confidence=0.6)
model.save("scavo.ppgclf")
other = learn.ClassifierModel.load("scavo.ppgclf")          # apply it to another project
```

| Call | Description |
| --- | --- |
| `classify.set_class(chunk, mask, code, manual=True, label=None)` | gives the selected dense points (boolean mask or indices) a class and flags them as labelled by hand; `manual=False` removes the flag; returns the count |
| `classify.label_points(pc, mask, code, manual=True)` | the same on a `PointCloud` in memory: label several selections, then store the cloud once (`chunk.point_cloud = pc`) |
| `classify.clear_manual(chunk, mask=None)` | forgets the hand-labelled flag (classes unchanged) |
| `learn.point_features(xyz, rgb=None, normals=None, scales=None, ground=None, progress=None)` | `(N, F)` float32 feature matrix and feature names; world coordinates, z up |
| `learn.suggest_scales(xyz)`, `learn.estimate_spacing(xyz)` | default core voxel and radii: 5, 14 and 40 times the point spacing |
| `learn.train(xyz, codes, rgb=None, ..., method="random_forest", n_estimators=None, max_per_class=20000, random_state=0)` | `ClassifierModel`; `codes` 1-255, 0 or negative = unlabelled; `method="gradient_boosting"` for smaller files |
| `learn.predict(model, xyz, rgb=None, ..., min_confidence=0.0)` | `(codes uint8, confidence float32)`; points below `min_confidence` become 1 (unclassified) |
| `learn.train_on_chunk(chunk, labels="manual")` | trains on the hand-labelled points (`labels="all"`: every point with a class above 1) |
| `learn.classify_chunk_with_model(chunk, model, overwrite_manual=False, min_confidence=0.0)` | classifies and stores the dense cloud; returns `(point_cloud, confidence)` |
| `model.save(path)`, `ClassifierModel.load(path)`, `learn.read_model_info(path)`, `model.summary()` | model files; `read_model_info` reads the metadata only |
| `model.classes`, `class_names`, `feature_names`, `scales`, `oob_score`, `training_accuracy`, `confusion_matrix`, `stats` | training results (confusion matrix: rows true class, columns predicted) |

* **Features** (per radius): linearity, planarity, sphericity, omnivariance, eigenentropy,
  change of curvature, verticality of the normal and of the main axis, density, height above
  the lowest and below the highest neighbour, vertical spread, roughness; then height above
  ground (progressive morphological filter, or `ground=` a DTM GeoTIFF / ground elevations),
  colour (normalised RGB, HSV, excess green) and colour contrast with the neighbours.
  They are computed on a *core* cloud (one point per voxel; voxel = point spacing, coarser
  above `max_core_points`, default 1 M) and every point takes the values of its voxel; each
  radius uses its own voxel level and at most 64 neighbours. About 15 s per million core
  points on 4 threads (`n_jobs`).
* **Hand labels**: `PointCloud.manual` (boolean per point, `None` when nothing was labelled)
  is stored as the PLY property `manual`; files without it load as before. The rule-based
  `classify_chunk` and `classify_chunk_with_model` keep the class of these points unless
  `overwrite_manual=True` (then the flag stays where the new class agrees).
* **Training**: one sample per core voxel (the class most of its labelled points carry), at most
  `max_per_class` per class, classes weighted inversely to their size; the random forest
  (100 trees, each on at most 10,000 samples) is validated out of bag, gradient boosting on
  20 % held out. Deterministic for a given `random_state`.
* **Model file** (`.ppgclf`): a ZIP with `model.json` (format version, classes and names,
  feature names, radii and settings, core voxel, statistics, versions of the package,
  scikit-learn and NumPy, training chunk and CRS) and `estimator.pkl` (the scikit-learn
  estimator). The estimator is unpickled with an allow-list of scikit-learn and NumPy types,
  so a file that tries to run other code is refused. The radii are in metres: apply a model to
  clouds with a similar point spacing and a metric CRS (a local chunk without scale has
  arbitrary units).
* **CLI**: `ppg classify-train scavo.ppg modello.ppgclf [--labels manual|all] [--method
  random_forest|gradient_boosting] [--scales 0.05,0.15,0.4] [--trees 100]` and
  `ppg classify-apply scavo.ppg modello.ppgclf [--min-confidence 0.6] [--overwrite-manual]`.
