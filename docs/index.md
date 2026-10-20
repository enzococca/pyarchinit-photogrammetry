# pyarchinit-photogrammetry

Open-source photogrammetry for archaeology and heritage BIM, integrated with
[PyArchInit](https://github.com/pyarchinit/pyarchinit).

From photos to georeferenced point clouds, meshes, DEMs and orthophotos, with the tools an
excavation needs: coded targets and ground control, sections and plans, contour lines and volumes,
stratigraphic units and spot heights with the fields of PyArchInit, pottery profiles, point classes,
Segment Anything 2.1, IFC export. A desktop application, a command line (`ppg`) and a Python
library on the same engine, running on the CPU (Apple Silicon included). GPL-2.0-or-later.

[:material-heart: Support the project on PayPal](https://paypal.me/enzococca){ .md-button }

<div class="grid cards" markdown>

-   :material-download: **[Installation](install.md)**

    The desktop application for macOS and Windows, or `pip install` on macOS, Windows and Linux
    (x86-64 and ARM).

-   :material-school: **[Tutorials](tutorials/index.md)**

    Step by step, with the demo project and with real photos of a tell.

-   :material-monitor: **[Desktop interface](gui.md)**

    Every menu, pane and tool of the window.

-   :material-language-python: **[Python API](api.md)**

    Documents, chunks and the workflow in scripts, also inside the window (*Console Python*).

-   :material-database: **[PyArchInit](pyarchinit.md)**

    Orthophotos, DEMs, models, units, spot heights and sections into a PyArchInit database.

-   :material-puzzle-outline: **[Plugins](plugins.md)**

    Add menu actions, commands and export formats; the
    [plugin repository](https://github.com/enzococca/pyarchinit-photogrammetry-plugins).

</div>

![The DEM view with units from PyArchInit, contour lines and a section](img/gui-layers.png)

## What it does

* **Reconstruction**: alignment (COLMAP), depth maps (CPU, PatchMatch in Rust, or COLMAP/OpenMVS),
  dense cloud with confidence, mesh (Poisson, height field or depth map fusion), texture, DEM and
  orthophoto (true orthophoto, seamless blending, patches, at full resolution even on large tells).
* **Georeferencing**: ArUco/AprilTag targets found automatically, ground control and check points,
  scale bars, camera optimisation on the control, report with the errors.
* **Archaeology**: sections and plans to scale (SVG/DXF), contour lines, cut and fill volumes,
  relief visualisations (sky-view factor, openness, multi-hillshade), stratigraphic units and spot
  heights drawn on the orthophoto, proposals of units and relationships, Munsell colours, pottery
  profiles from a turntable, stones and features with SAM.
* **Exchange**: PyArchInit (SQLite/SpatiaLite and PostgreSQL/PostGIS), QGIS live, QField, IFC 4,
  I3S scene layers, COPC, 3D Tiles, glTF; projects of Metashape, WebODM and Photo3D imported.

## Status

Version 1.0 - desktop application, command line and library. The source, the issues and the
releases are on [GitHub](https://github.com/enzococca/pyarchinit-photogrammetry); the plan of the
next versions is in the [roadmap](roadmap.md).
