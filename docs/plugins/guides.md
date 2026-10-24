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
updated. Plugins are Python code with the same rights as the program: install only those you trust.

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
| Geophysics (`geophysics`) | grids of magnetometry, resistivity, GPR and EM surveys: import, georeferencing on the pegs of the survey grid, filters, overlay on the orthophoto and the DEM, exports | [Geophysics](geophysics.md) |
| GIS archive (`gis-archive`) | spot heights, contour lines, US, sections, stones, markers, DEM and orthophoto of a project into a GeoPackage, PostGIS or SpatiaLite database | [GIS archive](gis-archive.md) |
| terrio (`terrio`) | point clouds of any format, also from the web, into the project; ground classification; reference rasters; E57, EPT, PMTiles and MBTiles exports | [terrio](terrio.md) |
| Spirula (`spirula`) | 3D Gaussian splatting of the aligned photos with Spirula Studio, a mesh from the splat, the installation of Spirula Studio | [Spirula](spirula.md) |
| Powerlines (`powerlines`) | overhead lines in the dense cloud: conductors as catenaries with sag, clearance and obstacles, poles, classes, 3D exports | [Powerlines](powerlines.md) |
| Mesh (MeshLab) (`mesh-tools`) | MeshLab's filters on the model of the chunk: decimation that keeps the texture, isotropic remeshing, Taubin and HC smoothing, holes closed with the outline left open, topology repaired, measures; every result a new version of the model | [Mesh (MeshLab)](mesh-tools.md) |
| Sentinel-2 (`sentinel2`) | Sentinel-2 time series over the survey to look for crop marks: a public catalog searched, clouds masked, NDVI, NDRE and NDMI per date, series and chart, composites and crop-mark maps over the orthophoto, QGIS and exports | [Sentinel-2](sentinel2.md) |
| Blender and EMtools (`blender-em`) | the 3D proxies of the units and the survey into an Extended Matrix project, named after the nodes of the em.json of PyArchInit or EMStudio; Blender opened on it and kept up to date while the proxies change; the selection shared live with EMtools 1.6 | [Blender and EMtools](blender-em.md) |

Every guide has the same parts: what the plugin does and what it needs first, the data and how to
prepare them, the steps in the program and on the command line, how to read the results, the files it
writes.
