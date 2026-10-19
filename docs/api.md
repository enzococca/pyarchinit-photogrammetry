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
| `optimize_cameras(fit_principal_point=False, use_markers=True, marker_weight=4)` | bundle adjustment, returns the reprojection error (px). With 3 or more control points (markers with an enabled reference, not check points) a free adjustment is followed by one where the markers are fixed points at their reference coordinates, observed in the photos (each measurement weighs `marker_weight` tie point observations): the block bends to fit them, which a similarity alone cannot do. Control scale bars (2 or more, or with control points) enter the same way: their markers are fixed at `engine.sfm.bar_anchors` (see *Scale bars*). `meta["sfm"]["optimize"]` reports `control_points`, `scale_bars` and the control/check point and bar RMSE before and after (`ppg optimize`) |
| `import_reference(csv, crs, columns="nxyz")`, `export_reference(csv)` | `n` label, `x y z`, `a` accuracy, `-` skip |
| `add_marker`, `add_marker_projection`, `add_scalebar`, `triangulate_markers` | manual markers and scale bars (see *Scale bars*) |
| `update_transform(use_cameras=None)` | weighted 7-parameter fit; returns control/check RMSE, per-marker and per-bar errors |
| `build_depth_maps(quality, filter_mode, backend)` | backends: auto/cpu/colmap/openmvs |
| `build_point_cloud()`, `build_model(source, surface, face_count, voxel_size, vertex_colors)`, `build_texture(texture_size)` | source: `point_cloud`, `tie_points` or `depth_maps` (see below) |
| `build_dem(resolution, source)`, `build_orthomosaic(resolution, surface, blending, colour_correction, ghosting, fill_holes)`, `calibrate_colours()` | GeoTIFF in the chunk CRS (see *Seamless orthomosaic*) |
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
size, the tiles in parallel; every cell takes the best of the photos that frame it (the four best
are tried in turn against occlusions), so the choice never depends on the tile around it; cells
that every photo sees from further than `max_angle` degrees from the vertical stay transparent.

## Seamless orthomosaic and colour correction

| Call | Description |
| --- | --- |
| `chunk.build_orthomosaic(..., blending="mosaic", blend_sigma=8.0)` | `mosaic`: the details of every cell from its best photo, the low frequencies blended across the seams over a few `blend_sigma` cells (Gaussian), so different exposures leave no edge and nothing is blurred; `disabled`: one photo per cell; `average`: every photo weighted by how well it sees the cell. A patch never changes the pixels around it: outside the patches the weights come from the photos the cells would take without any patch, inside a patch from its own cells (`engine.ortho._blend`) |
| `chunk.calibrate_colours(vignetting=True, samples=20000)`, `engine.colour.estimate(chunk)` | exposure gain of every photo (per channel) and vignetting of every lens (`v(r) = b1 r^2 + b2 r^4 + b3 r^6` on the radius over the half diagonal), from surface points seen by several photos, least squares on all the observations at once (`scipy.sparse.linalg.lsqr`, outliers left out between rounds, mean log gain zero); stored in `chunk.meta["colour"]` with the colour scatter before/after |
| `build_orthomosaic(colour_correction=None)`, `build_texture(colour_correction=None)` | None: the calibration of the chunk if there is one; True: measured first when missing; False: none (`engine.colour.Correction.correct(cam, image)`, applied as the photos are read) |
| `build_orthomosaic(ghosting=False)` | where three or more of the candidate photos see a cell, the best one that agrees with their median colour (within 30 levels): people and vehicles seen by a few photos left out |
| `build_orthomosaic(fill_holes=False)` | holes of at most 100 cells that no photo sees filled from their edges (`cv2.inpaint`, the cells of the same group only); larger holes stay transparent |

## Image quality and video

| Call | Description |
| --- | --- |
| `engine.quality.estimate_quality(chunk, cams=None)` | sharpness of every photo relative to the sharpest of the chunk (0-1, `camera.meta["quality"]`): per cell of a 6 x 6 grid with texture, the finest detail over the detail a few pixels wide, along x and y, the lower of the two (motion blur in one direction counts), 80th percentile of the cells; independent of how much texture the scene has |
| `engine.quality.low_quality(chunk, threshold=0.5)`, `sharpness(image)` | the blurred photos; the measure alone |
| `engine.video.add_video(chunk, video, folder=None, srt=None, overlap=0.7, every=None, max_frames=None)` | frames added as photos: a new one when the view has moved by `1 - overlap` of its width (median shift of ORB matches, a frame with too few matches only counts when sharp), the sharpest in the next 0.4 s; or one every `every` seconds. The DJI `.SRT` beside the video (`[latitude] [longitude] [abs_alt]`, `GPS(lon,lat,alt)` or `GPS (lon, lat, alt)`) gives every frame its position (EPSG:4326, interpolated in time) |
| `engine.video.extract_frames(video, folder, ...)`, `read_srt(path)`, `position_at(track, t)` | the parts |

## Fisheye lenses and 360-degree photos

| Call | Description |
| --- | --- |
| `chunk.add_photos(...)` | a lens whose EXIF name says fisheye (`LensModel`, or the camera model), or a 35 mm equivalent focal length up to 15 mm, gets an `OPENCV_FISHEYE` sensor (Kannala-Brandt: `fx, fy, cx, cy, k1-k4`); aligned with one focal length (`RADIAL_FISHEYE`) unless a calibration says otherwise. Projections far from the axis are no longer cut at the pinhole frame |
| `chunk.set_sensor_model(sensor_key, model)` | another camera model for a sensor (`OPENCV`, `OPENCV_FISHEYE`, `FULL_OPENCV`, `PINHOLE`, any COLMAP model): focal length and principal point kept, distortion reset; align again after it |
| `engine.panorama.add_panoramas(chunk, paths, size=None, fov=100, faces=None)` | equirectangular photos (width twice the height; others left out) become the faces of a cube (`front`, `right`, `back`, `left`, `up`, `down`, or the ones in `faces`): pinhole photos of `fov` degrees (more than 90: neighbouring faces overlap), `size` px a side (default: the resolution of the panorama in their centre), in `facce_360/<face>/<panorama>.jpg` beside the panoramas, one fixed `PINHOLE` sensor; `camera.meta["panorama"]`, `meta["face"]`; the GPS of the panorama is the reference of its faces |
| alignment | the faces of every panorama are one frame of a COLMAP rig (`engine.panorama.rig_configs`: a rig per `facce_360` folder, faces at their known rotation, no translation): they keep one centre and their relative rotation, their calibration is not refined (also in `optimize_cameras`), pairs of faces of the same panorama are not matched. Depth maps pair a face with faces of other panoramas looking up to 50 degrees away (the panoramas are turned anyhow) |
| `engine.panorama.split_panorama(path, folder=None, size=None, fov=100, faces=None)`, `face_maps(...)`, `face_rotation(yaw, pitch)` | the faces alone |

## PyArchInit media

`PyArchInit.us_media(sito, area, us, unita_tipo=None)` and `site_media(sito)`: the media linked to a
stratigraphic unit or a site (`media_to_entity_table`), each with `id_media`, `filename`, `filetype`,
`mediatype`, `descrizione` and the full paths of the original (`path`), the thumbnail (`thumb`) and the
resized copy (`resize`) - None where the file is not on this computer or on a remote store.

## Laser scans

| Call | Description |
| --- | --- |
| `io.scans.read_ptx(path)`, `read_e57(path)` (extra `scans`: pye57), `read_scans(path)` | the scans of a file (`Scan`: registered `xyz`, `rgb`, `intensity` 0-1, scanner `position`); PTX: the grid of each scan with its 4 x 4 registration (empty returns left out); E57: the `data3D` scans with their poses, cartesian or spherical |
| `io.scans.import_scans(doc, paths, label=None, crs=None, max_points=None)` | a new chunk whose dense cloud is all the scans in their registered frame (colours, else the intensity in grey), `meta["scans"]`, `meta["metric"]` (metres even in local coordinates: *Allinea chunk* without scale accepts it) |
| `doc.align_chunks(photo_chunk, [scan_chunk], method="points", fix_scale=True)` | the scan on the photogrammetric chunk (global registration, then ICP); not needed when both are in the same CRS. On the synthetic trench the heights agree to 0.2 mm; in plan the walls of the dense cloud move the fit by a few millimetres |
| `io.scans.colour_from_photos(target, photos)` | the points of `target` take the colour of the aligned photo of `photos` that sees them closest, among those where the model does not hide them (2 % tolerance) |

## Exports for the web

| Call | Description |
| --- | --- |
| `chunk.export_orthomosaic(path, region=None, cog=None, quality=85)`, `chunk.export_dem(path, region=None, cog=None)`, `io.web.export_cog(src, dst, compression="auto")` | Cloud Optimized GeoTIFF (tiled, overviews, read by parts over the network): `jpeg` (the alpha of the orthomosaic becomes a mask), `webp` (keeps the alpha), `zstd`/`deflate`/`lzw` lossless with the predictor (floating point for DEMs) |
| `chunk.export_point_cloud("x.copc.laz")`, `io.copc.write_copc(path, xyz, rgb, classification, confidence, crs_wkt)` | Cloud Optimized Point Cloud 1.0: LAZ 1.4 (point format 7, confidence as an extra byte) arranged in an octree (each node one point per cell of a 128^3 grid, the rest to its children, down to 100 000 points), one LAZ chunk per node, the `copc info` VLR and the hierarchy EVLR; QGIS, Potree and laspy's `CopcReader` read only what they show |
| `chunk.export_model("x.glb")`, `io.web.export_glb(chunk, path)` | binary glTF with the texture (or the vertex colours), coordinates around the centre of the model (single precision keeps millimetres), the centre and the CRS in the scene `extras` and in `x.json` |
| `engine.normalmap.light_model(chunk, target_faces=50000, texture_size=4096, folder=None)` | a light model for the web and game engines: quadric decimation (Open3D), faces in cells of an atlas, and for every texel the light surface moved along its normal onto the full model - its colour (texture or vertex colours) into `light_color.jpg`, its normal in the tangent space of the light face (x along U, y along V, OpenGL green up as glTF wants) into `light_normal.png`; `light.glb` (PBR material with both, local coordinates), `light.obj` + `.mtl` (`map_Kd`, `norm`). `bake_maps(full_verts, full_faces, light_verts, light_faces, ...)` the baking alone: on a rippled surface baked onto two triangles the decoded normals are within 3 degrees (median) |
| `io.flythrough.orbit_video(chunk, path, kind=None, seconds=12, fps=25, size=(1280, 720), pitch=35, zoom=1, turns=1, backend="auto")` | an MP4 of a camera orbiting the model or the cloud: VTK off screen (with the texture) or the software renderer of the 3D view (points sampled on the surface, z-buffered), OpenCV writer (H.264 or MPEG-4) |
| `chunk.export_3dtiles(folder, max_faces=150000)`, `io.web.export_3dtiles` | Cesium 3D Tiles 1.1: `tileset.json` and GLB tiles in a quadtree in plan, each level the model with fewer faces (vertex colours, clustering), the leaves at full detail; the root transform from east-north-up at the centre of the model to earth-centred coordinates (pyproj; heights as they are) |

## Measures

| Call | Description |
| --- | --- |
| `archaeo.measures.surface_area(dem_path, polygon, model=None)` | inside a plan polygon: `plan` and `surface` areas from the DEM (each cell `sqrt(1 + gx^2 + gy^2)` times, border cells by the share inside), `mean_slope`, `z_min`, `z_max`; with a mesh in the same coordinates also `model_surface`, `model_plan`, `model_faces` (faces whose centre is inside) |
| `measures.slope_aspect_at(dem_path, x, y)` | slope and aspect (degrees; aspect clockwise from north, where the ground faces downhill, -1 flat) from the 3 x 3 cells (Horn) |
| `measures.aspect_raster(dem_path, out)`, `archaeo.terrain.slope_degrees(dem_path, out)` | rasters of aspect and slope |
| `measures.ground_surface(xyz, classes, cell=None)`, `height_above_ground(xyz, classes)` | the ground on a grid from the points of class 2 (median of each cell, holes interpolated) and the height of every point above it |
| `archaeo.timeline.phases(doc)`, `phase_date(chunk)`, `set_phase_date(chunk, date)` | the chunks with a DEM as phases of the excavation in date order (`meta["date"]`, else the earliest EXIF date of the photos) |
| `timeline.removed(doc, polygon=None)`, `suggested_relations(doc)`, `timeline(doc)`, `timeline_csv(report)` | volume removed and added from each phase to the next, and inside each unit drawn on the earlier phase; units exposed earlier that overlap in plan units exposed later as `Copre` / `Coperto da`; the whole report and its CSV |
| `timeline.write_relations(pa, relations)`, `pa.add_relationships(sito, area, us, rels)` | the suggested relations added to the `rapporti` of the US forms (both ways; those already there left out) |
| `archaeo.us_proposal.propose_for_chunk(chunk, units, first_us, area=None, sito=None, step=0.02, cuts=True, lang="it")` | US proposed for units without a number: `measure_units` (area, perimeter, DEM elevations, Munsell colour, majority class of the classified cloud), `contacts` (pairs that touch: length, share of each outline, enclosure, DEM step across the contact from a strip on each side), `propose` (numbers from the highest unit, USM from the masonry class, fills of cuts with their cut, *Copre* from the step, *Si appoggia a* against masonry; each relation with `why`) |
| `us_proposal.write_proposal(pa, proposal, sito)`, `pa.next_us(sito)` | the chosen units as US forms (`create_us`: `node_uuid`, `entity_uuid`; existing forms left) and their relations in both directions; the number after the highest US of a site |
| `chunk.provenance(product, path=None, operator=None)`, `core.paradata.provenance(...)` | W3C PROV-O in JSON-LD: the product (file, SHA-256, size, CRS, resolution, active version) as `prov:Entity`, the `prov:Activity` that made it with its settings, the entities it derives from back to the photos and the georeference (control/check RMSE), the program and the operator as agents |
| `paradata.write_provenance(chunk, product, path)`, `paradata.summary(chunk, product)` | `<file>.prov.jsonld` beside an export or a published copy; a one-line summary (PyArchInit's note). `pa.publish_orthomosaic/dem/model` write both |
| `io.qfield.export_package(chunk, folder, resolution=0.02, project=True)` | QField package: `ortofoto.tif` (COG JPEG at `resolution`), `dem.tif`, `rilievo.gpkg` (layers `unita` and `quote` with `ppg_id` and the PyArchInit columns), `<chunk>.qgs` (QGIS's Python, relative paths, value maps) and `ppg_qfield.json` (what was exported) |
| `qfield.read_edits(folder)`, `qfield.import_edits(chunk, folder)` | the units and spot heights added, changed and deleted on the tablet; applied to `chunk.meta["drawings"]` |
| `io.gpkg.read_table(path, table)`, `gpkg.geometry(blob)` | features of a GeoPackage table read with `sqlite3` (geometry from the GeoPackage binary format) |
| `archaeo.munsell.calibrate_chart(rgb)`, `correct(rgb, ccm)` | a ColorChecker Classic found in an image (OpenCV `mcc`), the 3 x 3 correction matrix in linear light (OpenCV `ccm`, loss as CIEDE2000), applied to colours |
| `munsell.munsell(rgb)`, `area_colour(raster, polygon, ccm=None)` | Munsell notation (illuminant C, colour-science; retried and, if it does not converge, the nearest chip in CIELAB) and nearest soil chart chip; the median colour of a raster inside a polygon, shadows and glare left out |
| `archaeo.stones.find_stones(raster, segmenter, bounds=None, min_size=0.05, max_size=1.0)` | stone-by-stone drawing: SAM's automatic masks over any RGB raster (orthomosaic, orthophoto of an elevation) by tiles, a partition, then `keep_stones` (equivalent diameter between the sizes, solidity >= 0.75) |
| `stones.export_stones(stones, path, scale=20)` | the outlines as DXF (layer `PIETRE`), SVG at 1:`scale` (millimetres on paper) or GeoJSON; `ai.sam.auto_units(..., path=raster)` segments any raster |
| `chunk.build_relief(method="svf", radius=None, directions=16, min_size=None, max_size=None, scaling=2.0)` | relief visualisation of the DEM, product `method` in `<data>/terrain/` (parameters and the 2nd/98th percentiles in `meta["relief"]`): `svf` sky-view factor (1 - mean sin of the horizon angle), `openness_pos` / `openness_neg` (mean zenith angle of the horizon, degrees), `slrm` (DEM minus its mean over the radius, metres), `msrm` (multi-scale relief: mean difference of low-pass levels of radius `k ** scaling` from `min_size` to `max_size`), `multi_hillshade` (0-1); radius default about 20 cells |
| `archaeo.relief.build_relief(dem_path, out, method, ...)`, `relief.compute(z, resolution, method, ...)` | the same on any DEM file (tiles with the margin each method needs, several threads; the result is that of the whole DEM) or array |
| `measures.ground_dem(chunk)`, `ground_dem_at(path, x, y)` | a DTM kept as a version of the DEM (built from class 2 only) and its height at a point |

## Diagnostics of the alignment, calibration, markers

| Call | Description |
| --- | --- |
| `engine.diagnostics.match_graph(chunk, verified=True)` | `{(camera key a, b): matches}` of every matched pair, from the COLMAP database (the matches that fit the geometry of the pair, else all candidates): also for photos that did not align |
| `diagnostics.components(keys, graph, min_matches=1)` | groups of photos linked by pairs with enough matches, largest first (separate groups, isolated photos) |
| `diagnostics.pair_matches(chunk, a, b)` | keypoints of the two photos and the candidate and verified matches (index pairs); `shared_tie_points(chunk, a, b)` the tie points seen by both; `residuals(chunk, key)` measured and reprojected pixels of the tie points of a photo |
| `Sensor.converted(model)` | the parameters in another camera model (focal length and principal point kept); `Chunk.set_sensor_model` uses it |
| `Sensor.fixed` | the calibration is held: alignment (`constant_cameras`) and camera optimisation do not refine it, as for the faces of 360-degree photos. Imports no longer set it (their calibration is where the alignment starts) |
| `chunk.merge_markers(markers, label=None)` | markers that are the same point made one: the first keeps its key, takes the measurements of all (in a photo measured in several, the one placed by hand), the first reference, the excluded photos; scale bars that ended at the others end at it |
| `chunk.remove_markers(markers)` | markers (objects, keys or labels) removed with the scale bars that end at them |

## Undo and redo (interface)

`gui.undo.UndoHistory(get_state, set_state, get_chunk, limit=50, file_limit=10)`: `record(text=None)`
after a change keeps the parts of `get_state()` (JSON-able) that differ from the last snapshot;
`backup(products)` before an edit copies the files of those products of the chunk (`gui.undo.clone`:
`clonefile` on macOS, else a copy) into `<chunk data>/undo/`, and the next `record` keeps them;
`undo()`/`redo()` hand the parts back to `set_state(parts, names, products)` and swap the files;
`rebase()` after processing (not an edit); `reset()` for another project or chunk (copies deleted).
The window records on every change of the project, `run_task(..., undo=True)` for edits made in
the background.

## Batch processing

| Call | Description |
| --- | --- |
| `batch.step(op, chunks="all", **params)` | a step: one of `batch.OPERATIONS` (`detect_markers`, `import_reference`, `align_photos`, `match_photos`, `align_cameras`, `update_transform`, `optimize_cameras`, `field_check`, `build_depth_maps`, `build_point_cloud`, `build_model`, `build_texture`, `build_dem`, `build_orthomosaic`, `calibrate_colours`, `estimate_precision`, `export_point_cloud`, `export_model`, `export_dem`, `export_orthomosaic`, `generate_report`) with its parameters (the defaults completed, unknown names refused); `chunks`: `"all"`, `"enabled"` or labels/keys. Paths may use `{project}`, `{name}`, `{chunk}` |
| `batch.run_batch(doc, steps, resume=True, retries=1, on_error="continue")` | every step on all its chunks, then the next; the project saved after each step of each chunk with the state in `<project>.ppg.files/batch.json`; `resume`: an unfinished batch with the same steps goes on (done steps are not run again, failed ones are tried again); a failing step is retried `retries` times, then the next steps of its chunk are skipped (or `BatchError` with `on_error="stop"`). Returns `done`, `failed` (`"chunk:step"` -> error), `skipped`, `seconds` |
| `batch.load_steps(path)`, `save_steps(steps, path)`, `pending(doc, steps=None)`, `targets(doc, chunks)`, `run_step(doc, chunk, op, params)` | batch files (JSON), the state of an unfinished batch, the parts |

## Field check before leaving the site

| Call | Description |
| --- | --- |
| `engine.fieldcheck.field_check(chunk, min_photos=3, resolution=None, quality=True)` | right after the alignment: the photos not aligned, the blurred ones (image quality estimated where missing), the markers measured in no photo (with a reference), one photo or two only, and the coverage: on a grid over the middle 98 % of the tie points (400 cells along the longer side by default), at the height of the DEM or of the tie points of the cell (the empty cells interpolated), how many aligned photos frame each cell (no occlusion: fast); a GeoTIFF of counts (product `coverage`, -1 outside the area). Report in `chunk.meta["field_check"]` (`weak_fraction`, `weak_area` m2, `median_photos`, `unaligned`, `blurred`, `markers`, `tie_points_weak`) |
| `engine.fieldcheck.coverage(chunk, resolution=None)` | the counts alone: `(counts, transform, heights)` |

## Precision of the alignment

| Call | Description |
| --- | --- |
| `chunk.estimate_precision(marker_px=0.5)`, `engine.precision.estimate_precision(chunk, ...)` | the adjustment set up as the camera optimisation does it (control points held at their reference, else three tie points; calibration held) and COLMAP's covariance of the camera poses (`pycolmap.estimate_ba_covariance`; tangent space of cam_from_world, rotation perturbed as `Exp(2d) R`, translation added). Cameras: covariance of the centre. Tie points and markers (two photos or more): the full covariance of the triangulated point, `s^2 H^-1 + H^-1 H_pc S_cc H_cp H^-1` - its measurements (`s` = the reprojection error of the adjustment, `marker_px` for markers) and the uncertainty of the cameras that see it. Datum: the accuracy of the control points (or of the camera positions that place the chunk), `a^2 (1 + d^2 / r^2)` per axis. Standard deviations in metres along X, Y, Z of the CRS: `camera.meta["precision"]`, `chunk.meta["precision"]` (`datum`, `sigma0_px`, medians, `markers`), tie points in `<data>/precision/tie_points.npz`. With a DEM, also the raster below. A new alignment, optimisation or georeference drops it (`chunk.forget_precision()`). Synthetic trench: tie point height errors / estimated sigma = 1.07 (RMS), cameras 0.5-0.7 |
| `engine.precision.dem_precision(chunk, max_side=1024)` | sigma Z of the tie points interpolated over the DEM (linear, the nearest outside), GeoTIFF in the CRS of the DEM at most 1024 cells a side, product `dem_precision` (as the precision maps of James et al., 2017, from the covariance) |
| `engine.precision.precision_at(chunk, x, y)`, `tie_point_precision(chunk)` | sigma Z of the DEM at a point; the tie points with theirs |
| spot heights | the precision of the DEM where a spot height is taken is kept with it (`precisione_q`) and written in `rilievo_originale` ("Fotogrammetria (±1,2 mm)", PyArchInit has no column for it); `bim.ifc.export_chunk_ifc(..., quote=...)` writes the spot heights as `IfcAnnotation` (`SpotHeight`) with `Pset_PyArchInit_Quota` (Quota, Precisione, US, ...); `bim.ifc.spot_heights(quote)` |

## Reconstruction region

| Call | Description |
| --- | --- |
| `chunk.region = engine.region.Region(center, size, rotation)`, `chunk.region = None` | an oriented box in world coordinates (`chunk.meta["region"]`): depth maps (CPU and, afterwards, COLMAP's) have no depth outside it, the dense cloud and the mesh are cut to it (a Poisson surface no longer balloons beyond the object), so DEM and orthomosaic cover it only |
| `chunk.reset_region(source="tie_points", margin=0.05, angle_deg=0)` | around the middle 98 % of the tie points (or the dense cloud), turned about the vertical |
| `engine.region.region_from_box(chunk, (xmin, ymin, xmax, ymax), zmin=None, zmax=None)` | over a plan rectangle, the heights from the points inside |
| `Region.contains(world)`, `corners()`, `footprint()`, `inside_internal(chunk, pts)`, `depth_mask(...)`, `crop_mesh(...)` | the box and its uses |

## Editing the DEM

| Call | Description |
| --- | --- |
| `chunk.edit_dem(polygon, method="plane", value=None, breaklines=None, keep_version=True, version_name=None)`, `engine.dem_edit.fill_area(...)` | new heights for the cells inside `polygon` (chunk CRS) from those in a ring of three cells around it: `plane` (the best-fit plane), `linear` (triangulation of the ring, IDW outside it), `idw`, `constant` (`value`, default the median of the ring); `breaklines` (lines (N, 2) or (N, 3); without z the DEM at their vertices) keep their heights. The DEM as it was is kept as a version first; a DEM of another program is edited as a copy in the chunk folder; overviews refreshed in the window; `chunk.meta["dem_edits"]` lists the edits. Returns `cells`, `dz_mean`, `dz_max`, `version` |
| `engine.dem_edit.new_heights(known, target, method, value=None)` | the interpolation alone |

## Elevations: orthophoto and relief on a plane or a cylinder

A frame maps a raster cell `(u, v)` (u along the surface, left to right as the viewer sees it, v up,
metres) and a depth `d` (towards the viewer) to the world: `engine.elevation.Plane(origin, u_axis,
v_axis)` (`normal = u x v`) or `Cylinder(centre, axis, radius, inside, start)` (inside: seen from the
axis, a well or an apse; outside: a tower or a column). The surface of a cell is the first hit of a
ray from the viewer side on the model, kept within `depth=(dmin, dmax)` (what stands in front of
`dmax` - scaffolding, a section left in front of the wall - is passed through).

| Call | Description |
| --- | --- |
| `engine.elevation.plane_from_line(chunk, p1, p2, side=None)` | the vertical plane through a wall trace (plan points), seen from `side` (default: where the photos are) |
| `plane_from_points(points, toward=None)` | the plane of three or more world points (markers on a wall) |
| `cylinder_from_circle(chunk, points, inside=True)` | the vertical cylinder through a circle fitted to plan points |
| `build_elevation(chunk, frame, resolution=None, name=None, depth=None, blending="mosaic", colour_correction=None, ghosting=False, fill_holes=True, max_angle=80)` | orthophoto (the orthomosaic engine on the frame: same photo choice, blending, colour correction) and relief (`d`, float32) as GeoTIFF in frame metres in `<chunk data>/elevations/<id>/` (frame in the tags and in `frame.json`), listed in `chunk.meta["elevations"]` |
| `elevations(chunk)`, `elevation_paths(chunk, entry)`, `remove_elevation(chunk, id)` | the elevations of a chunk |

## Masks of the photos

A mask is a single-channel PNG as large as its photo, white where the photo counts and black where it
is masked (Metashape's and COLMAP's convention), kept in `<chunk data>/masks/<camera key>.png`. Masked
areas are left out when matching (COLMAP ignores their keypoints; the photos without a mask get a
white one, since COLMAP skips a photo whose mask is missing), in the depth maps (no depth there, also
after COLMAP's PatchMatch: `engine.dense.mask_depth_maps`), in the orthomosaic and in the texture (a
photo does not see a masked cell or face).

| Call | Description |
| --- | --- |
| `engine.masks.import_masks(chunk, folder, template=None, invert=False)` | from image files named after the photos (`{stem}_mask.png`, `{stem}.png`, `{name}.png`... or `template` with `{name}`, `{stem}`, `{label}`); `folder=None`: the alpha channel of the photos |
| `export_masks(chunk, folder, template="{stem}_mask.png")`, `read_mask(chunk, cam)`, `write_mask(chunk, cam, keep)`, `remove_masks(chunk, cams=None)`, `masked_cameras(chunk)` | files and arrays (`keep`: True where the photo counts) |
| `masks_from_background(chunk, folder, tolerance=12)` | photos of the background without the object from the same positions (one per photo, same names, or a single one for a fixed camera): the object is what differs |
| `masks_from_colour(chunk, colour, tolerance=40)` | a backdrop of one colour (RGB) masked |
| `masks_from_model(chunk, cams=None)` | only what the model covers is kept (after a first reconstruction) |
| `masks_with_sam(chunk, segmenter, cams=None)` | SAM 2.1: the object at the centre of each photo |
| `paint_polygon(chunk, cam, polygon, masked=True)` | mask (or give back) a polygon of pixels by hand |
| `chunk.match_photos(..., exclude_stationary=True)`, `engine.sfm.remove_stationary_matches(db)` | verified matches that do not move between two photos (less than 2 px or 0.15 % of the photo) dropped: the background of a turntable photographed from a fixed camera |

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
* the same on a chunk georeferenced elsewhere (an imported project, CRS not `LOCAL`) with no
  enabled reference: the bars scale it about their centre, keeping its position, orientation
  and CRS;
* three or more control points, or camera references: the similarity is the joint weighted
  least-squares fit of the points (`1 / accuracy²` on their 3D residuals) and of the bars
  (`1 / accuracy²` on `scale * length - distance`), `markers.gcp.similarity_with_bars`: the
  rotation and the position come from the points, the scale from points and bars together
  (`"markers+scalebars"`, `"cameras+scalebars"`, `"markers+cameras+scalebars"`);
  `use_cameras=True` or `False` forces the use of the camera references.

The report adds `scale` (world units per internal unit), `scalebars` (as `scalebar_errors`),
`scalebar_control_rmse` and `scalebar_check_rmse` (metres, None without bars of that type).

Markers are placed by `chunk.triangulate_markers()` (`markers.gcp.triangulate_markers`): from two
photos or more they are triangulated; measured in one photo only they lie where its ray meets the
surface, `gcp.surface_point(chunk, camera, x, y)` (the dense cloud, else the tie points, through
`core.geometry.point_on_ray`: a plane fitted to the points around the ray, first in a wide cylinder,
then in one of about 20 pixels at the depth found; kept per measurement); a marker with no
projection keeps its position. `gcp.views_of(chunk, marker)` counts the aligned photos that measure
it, `gcp.camera_ray(chunk, camera, x, y)` is the ray of a pixel. A marker placed in 3D (internal
frame) is projected on the photos that see it with `gcp.project_marker(chunk, marker, point,
max_photos=60)`: inside the frame, in front of the camera, not hidden by the surface near the
marker, as estimated projections.

A projection (`core.marker.Projection`) is `pinned` True when placed or confirmed by hand, False
when detected (coded targets), None when only estimated from the model (Metashape's blue flags;
older versions read None as a detection). `Projection.measured` is True for the first two: a
marker with measured projections is placed from them only, estimates are used only for a marker
placed in 3D, and they never enter the camera optimisation. `gcp.photos_seeing(chunk, point,
occlusion=True)` lists `(camera, x, y, depth)` of the photos that see a point, nearest the centre
first (*Filter Photos by Point*); `gcp.project_marker(chunk, marker, point=None, max_photos=60)`
replaces the estimated projections of a marker from its position; `gcp.views_of(chunk, marker,
estimated=False)` counts the photos that measure it (or, with `estimated`, that only estimate it).

`markers.refine.refine_markers(chunk, markers=None, min_score=0.8)` looks for every blue flag in its
photo by image correlation: a patch of the photo where the marker is measured (placed by hand
first, else the sharpest view) and the photo of the flag are both sampled on the same grid of
points on the surface at the marker (the plane of the dense cloud, else of the tie points, around
it; one sample per pixel of the coarser photo, smoothed against aliasing), so perspective,
rotation, scale and lens distortion do not matter; a clear peak of the normalised correlation
(at least `min_score`, 0.1 above any other peak, inside +-10 samples) moves the flag there to a
fraction of a pixel and makes it an automatic measurement (`pinned` False); the marker is
triangulated again and the flags left are estimated from its new place. Returns `{marker key:
{"refined": {camera key: (dx, dy, score)}, "failed": [camera keys], "reference": key}}`;
`refine_marker(chunk, marker, ...)` does one marker. On the synthetic trench, flags 2-6 px off
the target come back within 0.3 px of the detected centre.

In the camera optimisation (`chunk.optimize_cameras`) the control bars are fixed points as the
control points are: `engine.sfm.bar_anchors(chunk, bars, fixed=None)` moves the markers at their
ends so that the bars get their lengths, least squares of `scale * length - distance` (weighted
by `1 / accuracy²`) and of the shifts, weighted by `engine.sfm.marker_precision(chunk, marker)`
(the pixel accuracy of its measurements, 0.5 px, carried to its distance from each camera and
combined over the photos); the adjustment then holds those positions, observed in the photos.
It needs three fixed points or more, not on a line: two bars, or bars and control points. On
the synthetic trench, markers measured to 0.5 mm, a bar 6 mm too long with an accuracy of
0.1 mm bends the block by 0.2 mm: the photos hold it, as in a joint adjustment.

Imports: `integrations.metashape.import_psx` brings the scale bars of the project
(`MsScaleBar`: two `endpoint marker_id`, `reference d`, accuracy `sd` or the chunk's
`accuracy_scalebars`, unticked = check bar); `integrations.webodm.import_task` brings the camera
poses (`read_cameras`: `cameras.json`, `odm_report/shots.geojson`) and the GCPs as markers
(`read_gcps`), triangulated at once.

## Modules

| Module | Main functions |
| --- | --- |
| `markers.targets` | `generate_sheet`, `generate_turntable_mat`, `turntable_layout`, `generate_scale_bar`, `scale_bar_codes`, `detect` |
| `markers.gcp` | `import_reference`, `export_reference`, `triangulate_markers`, `update_transform`, `similarity_with_bars`, `marker_errors`, `scalebar_errors`, `import_scalebars`, `resolve_marker`, `cameras_span_their_accuracy` |
| `engine.chunks` | `align_chunks`, `merge_chunks`, `common_markers`, `common_cameras`, `pose_similarity`, `icp`, `global_guesses`, `ransac_rigid`, `registration_quality` |
| `archaeo.sections` | `section`, `sections_along`, `plan_slice`, `Section.to_dxf/to_svg`, `drawing_scale`, `grid_step` |
| `archaeo.pottery` | `estimate_axis`, `profile_at`, `best_profile`, `Profile.measurements`, `rim` (circle of the lip: diameter, arc kept, reliability), `measure` (profile and measurements of a vessel or sherd, axis moved to the centre of the rim), `percent_class`, `signature`, `signature_distance`, `draw` (SVG, DXF, PNG) |
| `archaeo.terrain` | `contours` (simplified, optional smoothing), `suggest_interval`, `dem_range`, `export_contours`, `volume`, `hillshade`, `slope_degrees` |
| `archaeo.stratigraphy` | `UnitPolygon`, `mask_to_polygons`, `export_units` |
| `bim.ifc` | `export_ifc`, `export_chunk_ifc`, `export_proxies_ifc`, `split_mesh_by_units`, `global_id` (IFC GlobalId from the `node_uuid` of a US form), `read_ifc_elements` |
| `archaeo.stratigraphy` (relations) | `RELATION_EDGES` (the relations of PyArchInit's forms in Italian, English and German as Extended Matrix edges), `stratigraphic_edges` (each declared relation once, from the start of its edge) |
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
