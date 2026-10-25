# GeoAI

GeoAI 0.2 learns pixel classes from **reviewed archaeological unit drawings** and applies the
trained model to another survey. RGB remains the default; optional DEM, multispectral, thermal or
other numeric raster bands can be used alone or combined with RGB. Its results are proposals: check them on the view, delete
incorrect polygons, then convert the remaining or selected proposals into units or export a
GeoPackage. It does not infer stratigraphic relationships.

Requires pyarchinit-photogrammetry 1.5.0. GeoAI can be installed independently; when SamGeo is also
installed, both use the `geoai-env` environment and preserve each other's installed features.
PyTorch and GeoAI run in a separate process, never alongside the application's pycolmap runtime.
`Plugin > GeoAI > Install GeoAI...` installs the ML dependencies on request. A GPU is recommended;
CPU is suitable for small trials. CUDA and Metal support depend on the installed PyTorch/GeoAI stack.

## Desktop

1. Open `Plugin > GeoAI > GeoAI pane` with the orthomosaic and checked unit drawings.
2. Pick a unit field, such as `tipo` or an attribute. In the table, assign each value a class name
   and a unit type (`US`, `USM`, ...). Values with the same class name are grouped. An empty class
   excludes that value from training; its pixels are unknown, not background.
3. **Draw reviewed area**: explicitly outline the area that has been completely annotated. Ground
   without units *inside that area* becomes background. Outside it, unmapped units, conflicting
   labels and raster nodata are ignored. Do not enclose unreviewed ground.
4. **Prepare dataset...** chooses a new folder. Default RGB uses the first three uint8 bands; select
   optional raster inputs for other data as described below. The reference grid must have square,
   north-up metre pixels. Tiles default to 512 px; real U-Net training requires at least 64 px.
   At least four tile columns across the
   reviewed area are needed: the right-hand part is validation, with a complete unused tile column
   separating it from training. Each class, including background, must occur in both sets.
5. **Train model...** selects that dataset and a new output folder (conventionally `wall.ppgseg`).
   A U-Net/ResNet-34 is created through `geoai.train.get_smp_model` with one channel per selected
   band. Default RGB uses ImageNet encoder weights and uint8/255 normalization. Optional numeric
   inputs start without ImageNet weights and use mean/std calculated only on labelled training
   pixels. Training ignores unknown pixels; validation
   reports IoU per class, mean IoU and a confusion matrix. The checkpoint with best validation mean
   IoU is kept. Validation is used for model selection, not an independent final test estimate.
6. **Apply model...** selects a model for the active chunk. The reference must have the training
   resolution. For optional raster inputs, select new survey files with the model's source names,
   order, bands and units. Resample the reference beforehand if needed. The model is applied in tiles with
   overlapping context; output is a class GeoTIFF and a GeoPackage of proposals.
7. Review the cyan proposals. The application's Select/Delete removes proposals from the GeoPackage.
   **US** converts selected proposals, or all remaining ones if none are selected. Existing conversions
   are detected from the actual unit drawings, so Undo works. **GeoPackage...** exports the reviewed
   proposals. The class raster remains the original model output when proposals are deleted.

The model folder contains `model.json`, `weights.pt` and `history.json`. Keep the directory together
when transferring a model. Metadata records classes, unit types, bands, resolution, normalization,
spatial split, package versions and metrics. Loading uses a weights-only state dictionary.
Dataset folders are immutable; failed preparation/training has no ready manifest and requires a new
output folder. Cancellation stops the worker; a partial output may remain for inspection.

## Optional DEM, multispectral and thermal inputs

Enable **Optional rasters: DEM, multispectral, thermal** and open **Choose rasters and bands...**.
Add rasters from the chunk or external
GeoTIFF files. Each source has a name, kind, selected bands (numbered from 1), resampling method and
physical unit when known. The first source defines the reference grid and must use the same CRS as
the chunk's unit drawings. DEM-only and thermal-only workflows are supported; a visible map is still
needed to draw the reviewed area. Up to 64 channels can be selected.

Examples include RGB + DEM for appearance and elevation, original multispectral reflectance bands,
and thermal radiometry or temperatures. Kinds describe the channels: they do not automatically
calculate NDVI, slope or other indices. Those can be prepared in the raster calculator and selected
as additional inputs.

Additional sources are reprojected/resampled onto the reference grid while reading windows; no full
stacked raster copy is written. A raster with a known CRS cannot be combined with one lacking a CRS.
When both are undefined, they must already share local metre coordinates. Grid alignment does not
correct survey registration errors. A pixel is ignored if any selected band has nodata or a nonfinite
value. GeoTIFF band scales/offsets are applied; sensor calibration and conversions such as Kelvin to
Celsius are not automatic. Keep calibration and units consistent between training and application.

The model stores its ordered channel schema and training normalization. Application reuses those
statistics and requires fresh source selections instead of reusing training paths. Existing 0.1
RGB datasets and models remain readable.

## Command line

```sh
ppg plugin geoai install
ppg plugin geoai status
ppg plugin geoai prepare excavation.ppg classes.json dataset
ppg plugin geoai train dataset walls.ppgseg --epochs 20 --batch-size 2 --device cuda
ppg plugin geoai apply another.ppg walls.ppgseg --min-area 0.01
ppg plugin geoai apply another.ppg multiband-walls.ppgseg --inputs another-inputs.json
ppg plugin geoai export another.ppg RUN_ID reviewed.gpkg
```

`classes.json` uses chunk coordinates, for example:

```json
{
  "field": "tipo",
  "mapping": {"USM": "masonry", "US": "soil"},
  "kinds": {"masonry": "USM", "soil": "US"},
  "reviewed_area": "POLYGON ((0 0, 50 0, 50 50, 0 50, 0 0))",
  "tile_size": 512,
  "validation_fraction": 0.2
}
```

The example area must be replaced with the actual fully reviewed area of your project.

For optional raster inputs add `reference` and `inputs` to the preparation JSON. The same shape is
accepted in `another-inputs.json` for application; use the new survey's files:

```json
{
  "reference": "/survey/ortho.tif",
  "inputs": [
    {"name": "colour", "kind": "rgb", "path": "/survey/ortho.tif", "bands": [1, 2, 3], "resampling": "bilinear"},
    {"name": "elevation", "kind": "dem", "path": "/survey/dem.tif", "bands": [1], "resampling": "bilinear", "unit": "m"},
    {"name": "temperature", "kind": "thermal", "path": "/survey/thermal.tif", "bands": [1], "resampling": "bilinear", "unit": "Celsius"}
  ]
}
```

## Scope and validation

One chunk is prepared at a time. Combining multiple chunks and change detection between dates are
subsequent steps. Planned change detection will compare two coregistered surveys and, when present,
DEM height differences; it is not part of 0.2. Spatial separation reduces
leakage but does not establish transfer to a different site: test on a separate survey before relying
on a model. A classification is a material/appearance hypothesis, not a stratigraphic interpretation.

Synthetic tests cover class mapping, unknown and conflicting labels, nodata, spatial separation,
worker isolation, fake training/application, polygons across tile seams and conversion attributes.
`PPG_GEOAI_FAKE=1` is exclusively a test engine; its models are explicitly labelled and refused in
normal mode. Real GPU training and archaeological model quality require validation on actual data.
A real four-channel U-Net smoke test on synthetic data verified CPU training and application;
it does not estimate accuracy on archaeological survey imagery.

API reference: [GeoAI training](https://opengeoai.org/train/). Plugin code: GPL-2.0-or-later;
third-party software and pretrained weights retain their respective licenses. This plugin does not
need SAM 3 weights or a SAM 3 access token.
