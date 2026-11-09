# Plugin guides

A plugin adds tools to pyarchinit-photogrammetry without changing it: a pane, menu actions, commands of
the command line, export formats. The plugins are collected in the
[plugin repository](https://github.com/enzococca/pyarchinit-photogrammetry-plugins) and the program
installs them by itself. These guides explain how to use them - the data they need, the steps, how to
read the results - for archaeologists who do not write code. How plugins work, and how to write one, is
in [Plugins](../plugins.md).

## Installing a plugin { #install }

In the program:

1. *Tools > Plugins...* (*Strumenti > Plugin...* in the Italian interface).
2. *Refresh the list*: the lower table shows the plugins of the repository, with version, author,
   description and a note: whether it is installed or can be updated, or what the plugin needs - for
   example a newer version of the program.
3. Select the plugin and click *Install / update*: the program downloads it, checks it and puts it in its
   own plugin folder.
4. *Restart now*: the plugin is active from the next start, with its pane and its menu
   (*Plugins > name of the plugin*).

The upper table lists the plugins installed - active, disabled, or the error that kept one out - with
*Enable / disable* and *Remove*. To update a plugin, install it again when its note says it can be
updated - or leave it to the program, which updates the plugins it installed by itself
([Updates](../plugins.md#updates)). Plugins are Python code with the same rights as the program: install
only those you trust.

From the command line:

```bash
ppg plugins available              # the plugins of the repository
ppg plugins install geoarchaeology
ppg plugins list                   # those installed, enabled or not
ppg plugin geoarchaeology help     # the commands of a plugin
```

The desktop application has no pip: the program installs plugins into a folder of its own. To install
them with pip in a Python environment, see [Plugins](../plugins.md).

## The guides

| Plugin | What it does | Guide |
|---|---|---|
| Geoarchaeology (`geoarchaeology`) | boreholes, cores and trench logs on the survey: stratigraphic columns, contact surfaces and thicknesses by kriging, sections, a 3D model of the units with their volumes | [Geoarchaeology](geoarchaeology.md) |
| Terrain (`terrain`) | analyses of the DEM with WhiteboxTools: water courses and basins, topographic position and landforms, curvature and roughness, nested hollows and mounds with their volumes, viewshed | [Terrain](terrain.md) |
| Geophysics (`geophysics`) | grids of magnetometry, resistivity, GPR and EM surveys: import, georeferencing on the pegs of the survey grid, filters, overlay on the orthophoto and the DEM, exports; electrical resistivity tomography with pyGIMLi (sections in 2D and in the 3D view) | [Geophysics](geophysics.md) |
| GIS archive (`gis-archive`) | spot heights, contour lines, US, sections, stones, markers, DEM and orthophoto of a project into a GeoPackage, PostGIS or SpatiaLite database | [GIS archive](gis-archive.md) |
| terrio (`terrio`) | point clouds of any format, also from the web, into the project; ground classification; reference rasters; E57, EPT, PMTiles and MBTiles exports | [terrio](terrio.md) |
| Spirula (`spirula`) | 3D Gaussian splatting of the aligned photos with Spirula Studio, a mesh from the splat, the installation of Spirula Studio | [Spirula](spirula.md) |
| Powerlines (`powerlines`) | overhead lines in the dense cloud: conductors as catenaries with sag, clearance and obstacles, poles, classes, 3D exports | [Powerlines](powerlines.md) |
| Mesh (MeshLab) (`mesh-tools`) | MeshLab's filters on the model of the chunk: decimation that keeps the texture, isotropic remeshing, Taubin and HC smoothing, holes closed with the outline left open, topology repaired, measures; every result a new version of the model | [Mesh (MeshLab)](mesh-tools.md) |
| Sentinel-2 (`sentinel2`) | Sentinel-2 time series over the survey to look for crop marks: a public catalog searched, clouds masked, NDVI, NDRE and NDMI per date, series and chart, composites and crop-mark maps over the orthophoto, QGIS and exports | [Sentinel-2](sentinel2.md) |
| Blender and EMtools (`blender-em`) | the 3D proxies of the units, the survey and the cameras with their photos into an Extended Matrix project, named after the nodes of the em.json of PyArchInit or EMStudio; Blender opened on it and kept up to date while the proxies change; what is modelled in Blender back into the program as proxies with their volume; the selection shared live with EMtools 1.6, which makes the proxies of the units without one | [Blender and EMtools](blender-em.md) |
| Samgeo (`samgeo`) | text search with Grounding DINO + SAM 2.1 or optional SAM 3; reviewable proposals, bulk identifiers/names, freehand-selected groups and survey UT under Topographic units in the TOC | [Samgeo](samgeo.md) |
| GeoAI (`geoai`) | train from reviewed drawings with RGB and optional DEM, multispectral or thermal inputs, alone or combined; apply models as proposals to review | [GeoAI](geoai.md) |
| Underwater (`underwater`) | surveys under water: blurred, hazy and flat photos flagged and excluded, sharp frames from videos and fast ROV sequences, colours restored with the range of every pixel (after Sea-thru) or a quick correction before the alignment, depth logs of ROVs and dive computers that level and scale the chunk | [Underwater](underwater.md) |
| Landscape through time (`landscape`) | how the surface changes over the years with Landlab: soil creep and running water (erosion and deposition) simulated on the DEM, presets for a mudbrick tell, an earth slope and an exposed excavation, final surface, erosion and deposition, drainage area, the balance of the volumes, a version of the DEM, the comparison with a later phase; scenarios, not predictions | [Landscape through time](landscape.md) |
| Shipwreck (`shipwreck`) | the technical drawing of an excavated hull from its 3D model: ship frame, stations, cross-sections, longitudinals and horizontals, a stated water correction, body plan, half-breadth plan, profile, plan over the orthophoto, timber catalogue with measures; clinker lands read on the sections, a strake model stated as a hypothesis, the file link with the Clinker Boat Tool (Houdini); the hull's relief from above with the timbers proposed and traced on it, every timber a 3D object (OBJ, GLB, IFC), the plan of the wreck; it measures, it does not reconstruct (the keel from typed dimensions is a stated hypothesis). Requires 1.7.1 | [Shipwreck](shipwreck.md) |
| Architectural elements (`architecture`) | parametric elements from the model for HBIM: columns by revolution (axis, measured profile, spline through knots, entasis), mouldings and cornices swept along a path traced on the plan, an elevation or between markers, arches and barrel vaults fitted as semicircular, segmental, pointed, elliptical or as measured; STEP, parametric IFC and DXF, each with its distance from the model | [Architectural elements](architecture.md) |
| Masonry (`masonry`) | walls, buildings and stone towers recorded by their faces (elevations on planes, cylinders and the sides of polygonal towers): lean, bulge, horizontal sections, the axis of a round tower, two surveys compared; stones, courses and mortar share with the type the measures suggest; USM relationships proposed from the geometry for you to check, saved in PyArchInit; plate at scale, DXF, IFC wall. Requires 1.7.0 | [Masonry](masonry.md) |
| Conservation (`conservation`) | the state of a wall face mapped on an elevation as a restorer does: decay areas, cracks with their opening, interventions and notes, with a shared glossary (ICOMOS-ISCS, UNI 11182); proposals from the relief and the orthophoto (material loss, cracks, colour classes, SAM areas) to name and accept; plate at scale with legend, tables, DXF, GeoPackages, QGIS, PyArchInit records, IFC 4.3 surface features; two campaigns on the same face compared. Requires 1.7.1 | [Conservation](conservation.md) |
| PhotoCAD (`photocad`) | the orthophoto, the DEM or an elevation vectorised by PhotoCAD (photocad.ai, an external paid service, one credit a conversion, confirmed before each upload): the drawing comes back in place as a layer, DXF, GeoPackage and SVG, stones polygonised from the lines, outlines into US/USM, free drawings; a docked pane. Requires 1.7.1 | [PhotoCAD](photocad.md) |
| Flight plans (`flightplan`) | drone flight plans designed before going to the field: area, camera, GSD and height, overlaps, strips, shots, time and batteries, terrain following on the project DEM, predicted coverage, GNSS targets as descents; DJI WPML and DJI Fly KMZ, Litchi, QGroundControl, Mission Planner, Parrot, KML, GeoPackage, the flight sheet; after the flight, the plan compared with the photos and the coverage obtained. Requires 1.7.4 | [Flight plans](flightplan.md) |

## Number and group many units

With application **1.5.1** and Samgeo **0.3**, converting proposals includes a preview of identifiers,
names and kinds. To edit existing units, open **Layers > Identify / group...**. In the **Select**
button's menu, enable **Freehand selection (lasso)** and trace around a group on the canvas;
Shift/Cmd adds and Alt subtracts. Choose **Selected units only** and **Shared name / group**:
units share a name while retaining separate identifiers and geometries.

For survey records choose **UT** and enter a project: the TOC shows **Topographic units**, optionally
followed by the group name. Excavation workflows retain US, USM and the other kinds. Numbering skips
occupied identifiers; **Edit > Undo** restores the entire operation. UT records use PyArchInit's
separate `ut_table` forms and are excluded from US form publication.

From application **1.5.2**, the **Labels** menu in the Layer pane lets you show or hide a layer's
labels without hiding its geometries, or show or hide every layer's labels at once. Select a feature
and choose **Edit label...** to customize its map text without changing its US or UT number. These
choices are saved with the project for each chunk; see the [Layer pane](../gui/panes.md#layer).

Every guide has the same parts: what the plugin does and what it needs first, the data and how to
prepare them, the steps in the program and on the command line, how to read the results, the files it
writes.
