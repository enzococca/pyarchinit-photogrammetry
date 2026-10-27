# Multispectral and thermal

The captures of a multispectral or a thermal camera have panes of their own, *Multispectral* and *Thermal* (tabs on
the right), that walk through the steps in order; every step says what it lacks. *Tools > Multispectral* and *Tools >
Thermal* have the same commands.

<div class="grid" markdown>

![The Multispectral pane](../img/gui/pane-multispectral.png)

![The Thermal pane](../img/gui/pane-thermal.png)

</div>

## Multispectral

### The captures

The files of a multispectral camera (MicaSense, DJI, Sequoia) are recognised when you add photos (*Photos* or *Add
folder*): the window offers to add them as captures and opens this pane, so the bands are not aligned as if they were
different views. *Add captures...* takes a folder with one file per band of every capture: the reference band is
aligned, the others ride on it.

A flight split over several folders (the rolls of a camera, `100MEDIA` and `101MEDIA`, whose numbering starts again)
is added folder after folder, or as the parent folder in one go: names that repeat take their folder as a prefix,
files already in the chunk are left out.

### The steps

| Step | What it does |
| --- | --- |
| **1. Captures** | *Add captures...* |
| **2. Alignment** | the reference band aligned as the photos |
| **3. Surface** | depth maps, dense cloud and DEM in one step, with the default choices |
| **4. Calibration panel** (optional) | the capture of the panel, its box and its reflectance; with a MicaSense camera the panel it found and the reflectances of its QR code, from the tags. *The sun sensor (DLS) follows the light during the flight*, on by default when the captures have it: the panel gives the level, the DLS the changes of light. The state of the step says which method is in use |
| **5. Reflectance orthomosaic** | a float band per band on the DEM; *Show* in false colour NIR-Red-Green, in true colour, or one band on its own in greyscale (NIR, red edge...) |
| **6. Index** | NDVI, NDRE, GNDVI, SAVI, NDWI, OSAVI, CIRE, Thermal, or an own formula over the band names, computed and shown on the colour ramp chosen beside it (red-yellow-green, greyscale, viridis or turbo; the choice recolours the index shown and is remembered) |
| **7. Zoning** | the classes and the method, then the polygons in a GeoPackage (the prescription map), shown by class with the polygons as a layer |
| **8. Measuring points** | the *Measuring point* tool and *Export points...* |

The **zoning methods**:

* **quantiles**: the same number of cells per class;
* **equal intervals**;
* **natural breaks (Jenks)**: the breaks that make the classes as homogeneous as possible - the choice when the index
  has distinct populations, such as bare soil, grass and a crop mark;
* **k-means**: the same aim by successive approximations.

The reflectance orthomosaic **balances the photos** by default (*Balance the brightness of the photos*): a gain
per photo and band, found where the photos overlap, takes away the strips between flight lines flown in opposite
directions (the sun and view geometry) while keeping the mean level; untick it for the raw reflectance.

The products appear in the workspace under *Multispectral* (the reflectance orthomosaic, every index, every zoning)
and open from there; *Open in QGIS* makes the QGIS project with the reflectance in false colour, the indices with
their ramp, the zones as raster and as polygons, besides the orthophoto, the DEM and the drawings. The details are in
the [API](../api.md#multispectral-cameras), *Multispectral cameras*.

## Thermal

### The captures

*Add captures...* in the *Thermal* tab takes the radiometric frames - DJI R-JPEGs, FLIR radiometric JPEGs, TIFFs in
degrees or kelvin, TIFFs of integers of other cameras (WIRIS...), which become degrees with the scale and offset set
in *Parameters...* - and, when there are any, the visible photos of the same flight.

### The steps

After the captures, the alignment and the surface come the steps of the temperatures:

| Step | What it does |
| --- | --- |
| **Measure parameters** (*Parameters...*) | emissivity, reflected and ambient temperature, humidity, distance; the factory values of the DJI cameras by default |
| **Thermal orthomosaic** (*Build...*) | the orthophoto in degrees Celsius, shown on a ramp - iron, inferno, rainbow, greys - between two temperatures you set, as Metashape's raster calculator narrows the thermal scale; *Auto* goes back to the 2nd-98th percentile |
| **Thermal anomalies** | the temperature minus its surroundings within a radius: buried walls and ditches change the soil above them |
| **Export** | keeping the degrees: GeoTIFF float32 in degrees Celsius, 16-bit GeoTIFF in centikelvin, CSV, or a FLIR radiometric JPEG with its world file - one file, or tiles beyond 8.4 million pixels: the program then asks whether to write one file with larger cells or tiles at full resolution - which the programs for FLIR cameras open as thermograms |

!!! note "The DJI Thermal SDK"
    DJI R-JPEGs are decoded with DJI's free Thermal SDK, which is not shipped with the program. *Download DJI Thermal
    SDK...* in the parameters step (and in *Tools > Thermal*) shows DJI's terms and, once accepted, downloads it from
    DJI into the folder `dji_tsdk` of the program's data, checked against the archive DJI published. On the Mac it runs
    in Docker, so Docker Desktop must be running.

## Values under the cursor and measuring points

Under the mouse the status bar reads the products at full resolution: on the thermal orthomosaic the elevation, the
temperature and its anomaly (`Z 158.231  T 18.34 °C  ΔT +0.42 °C`), on the reflectance every band, on an index its
value, on the orthophoto and the DEM the temperature too when there is one.

The **Measuring point** tool (in the drawing bar, in both tabs and in the menus) places a point where you click with
its coordinates, the elevation of the DEM, the temperature and its anomaly, the reflectance of every band and the
indices. The points go into the layer *Measuring points*, are saved with the project, reach QGIS with the other layers
and are exported as GeoPackage, CSV or GeoJSON (*Export the measuring points...*).

## Satellite imagery

*Tools > Satellite imagery* puts satellite scenes on the survey:

| Command | What it makes |
| --- | --- |
| **Orthorectify a scene...** | a scene with its RPCs on the DEM of the survey, kept as a version of the orthophoto |
| **DSM from two or more scenes...** | the surface from two scenes taken from two directions, or from three or more: the RPCs corrected against each other with tie points, the pairs chosen by angle and date, their surfaces fused; kept as a version of the DEM |
| **About the scene...** | size, bands, ground resolution, footprint |

The details are in the [API](../api.md#satellite-imagery), *Satellite imagery*; the
[Sentinel-2 plugin](../plugins/sentinel2.md) brings in the scenes of the Copernicus archive.
