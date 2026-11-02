# Architectural elements

The *Architectural elements* plugin makes parametric elements from the photogrammetric model: a column by revolution, a
moulding or a cornice swept along a path, an arch or a barrel vault from a section. Each one is a solid with parameters
(radius, height, span, rise, thickness) written as STEP and as a parametric IFC solid beside the mesh, for an HBIM
model; each one keeps the profile measured on the model and always says **how far the model is from the solid**, so
that nobody takes the ideal for the real.

This page describes version 0.1.0 of the plugin, which needs pyarchinit-photogrammetry 1.3.4 or later. The README and the
code are in the [plugin repository](https://github.com/enzococca/pyarchinit-photogrammetry-plugins/tree/main/plugins/architecture).

<figure markdown="span">
  ![A wall with a pointed archivolt and a cornice: the model in grey, the solids in colour](../img/plugins/architecture/wall-3d.jpg)
  <figcaption>The model of a synthetic wall made by the program from 40 photos (grey) and the two solids fitted on it:
  the pointed archivolt (orange) and the cornice along the top (blue).</figcaption>
</figure>

!!! note "The examples of this page"
    The figures and the numbers come from two **synthetic scenes** made for this guide: a column with entasis and a wall
    with a pointed arch and a cornice, textured, photographed by the program's renderer, then aligned, densified and
    meshed by the program as real photos would be. The truth is known, so the guide can say how close the plugin came
    to it. The script is `proof/make_proof.py` in the plugin's folder.

## In short

1. Install CadQuery once from the pane *Elementi architettonici* / *Architectural elements* (on the right).
2. **Column...**: draw the region of the column in plan, give the two heights and the number of knots.
3. **Moulding...**: trace the path on the plan, on an elevation or a section, or take two markers; give the window
   around the path where the section is cut.
4. **Arch / vault...**: choose a section of the project and click inside the opening, or trace a vertical plane in plan.
5. Read the **fit** in the table, show the solid in the 3D view, edit the parameters, export STEP, IFC or DXF.

<figure markdown="span">
  ![The pane of the plugin](../img/plugins/architecture/pane.png)
  <figcaption>The pane with the three elements of the examples: the measures and the fit of each.</figcaption>
</figure>

## What you need

A chunk with a **model** in metres: a projected coordinate system, or a local frame with a scale (references, a scale
bar, a model imported in metres). A local frame without a scale is refused: the numbers would not be metres. The
elements are measured on the model, never on the dense cloud; an orthophoto or a DEM is only needed to draw on in plan,
and an elevation or a section of the project (*Tools > Elevations and sections*) to draw on in elevation.

## CadQuery and its environment

The solids are made by [CadQuery](https://cadquery.readthedocs.io) and OpenCascade, in a process of their own: the
program never loads them. *Install CadQuery* in the pane (or `ppg plugin architecture install`) makes an environment
with `uv` in the plugin's folder: Python 3.12, CadQuery, OpenCascade (OCP) and the VTK that OpenCascade is linked
against, without the parts CadQuery uses only for drawing and its assembly solver (casadi, numba, scipy, matplotlib,
trame). It takes **about 900 MB** and a few minutes; **the first run after the installation takes about a minute**,
while Python prepares CadQuery's modules. A Python that already has CadQuery (`pip install cadquery`) is used as it is.

OpenCascade has builds for macOS (Apple Silicon and Intel), Linux (x86-64 and aarch64) and Windows x64. There is none
for **Windows on ARM**: there the pane says that the elements are not available.

## The column

*Column...* asks for a region drawn in plan; the dialog proposes the lowest and the highest height of the model inside
it. The part of the model inside the region and between the heights is the column, or a part of it.

* **The axis.** Two starts: the axis found from the normals of the surface (as the pottery of the program finds the axis
  of a vessel: any direction, a fallen shaft works) and the vertical through the middle of the part. Each is refined on
  circles fitted to slices across it; the one whose slices are rounder wins. The circles leave out what is not on them:
  the fins of flying pixels that a photogrammetric mesh grows along a silhouette, a chip of the stone.
* **The measured profile.** The model is cut by 36 half-planes through the axis; for each height (a step from the
  resolution of the mesh) the radius is the densest of the radii found there, and its spread is kept. Beds and tops
  (where the cut runs flat) are left out.
* **The solid.** A spline through K knots (12 by default) evenly spaced from the base to the top, their radii fitted by
  least squares to the measured profile; or, with *As measured*, the measured profile itself. The profile is revolved
  360 degrees. The **entasis** is the largest radius over the chord from the base radius to the top radius.
* **Several parts.** Base, shaft and capital are separate elements: *Same axis as* gives a part the axis of a column
  already made.

*Edit parameters...* changes the radius of each knot, the number of knots or the choice *As measured*: the measured
profile and the axis stay as they are, only the solid changes, and the fit is measured again.

<figure markdown="span">
  ![The profile of the column](../img/plugins/architecture/column-profile.png)
  <figcaption>The column of the examples: the true profile (grey), the radii measured on the program's model (red),
  the 12 knots and the spline of the solid (blue).</figcaption>
</figure>

<figure markdown="span">
  ![The column: model and solid](../img/plugins/architecture/column-3d.jpg)
  <figcaption>The model of the column (grey, cut away on the right) and its solid (orange).</figcaption>
</figure>

## The moulding

A cornice, a moulding, a string course: a section carried along a path.

* **The path**: traced on the plan (its height is the one you give, by default the top of the model at its first point);
  traced on an **elevation or a section** of the project, shown in the 2D view for the purpose (each point goes back to
  the world through the elevation's frame, at the depth of its relief or, where the relief has no value, of the model's
  surface nearest the viewer: such a path keeps its heights, a raking cornice too); or straight **between two markers**.
* **The section**: the model cut by the plane normal to the path at a station (by default the middle of the first
  segment), kept inside a **window** around the path's point (below, above, largest projection), the line nearest the
  path, closed against the wall by a back line *Depth into the wall* behind it, simplified (Douglas-Peucker, in metres).
  It projects towards the photos, or the side you choose.
* **The solid**: the section swept along the path with **mitred corners**, as a carved cornice turns a corner; an
  extrusion when the path is straight.

*Edit parameters...* changes the simplification and the depth into the wall, from the cut kept in the element.

<figure markdown="span">
  ![The section of the cornice](../img/plugins/architecture/cornice-section.png)
  <figcaption>The cornice of the examples: the section of the solid (blue) against the true one (red). The top is
  higher because the program's model is: the top of the wall was seen only obliquely.</figcaption>
</figure>

## The arch and the vault

* **The plane**: a section of the project (an elevation of kind *section*), or a vertical plane traced now with two
  points in plan. The model is cut by it.
* **The point in the opening**: clicked on the section shown in the 2D view, or typed as (u, v); the dialog proposes
  the middle of the largest opening it finds. From the point the plugin looks around: what it sees first is the
  **intrados** and the jambs, where each ray leaves the ring is the **extrados**.
* **The fit**, by least squares on the intrados: a circle (**semicircular**, when the centre is on the springing line,
  or **segmental**), a **pointed** arch (two circles centred on the springing line, meeting at the apex), an
  axis-aligned **ellipse**, or **as measured**. The jambs below the springing line are part of the fit, as vertical
  lines. With *the best (fit)* the simplest type within 25 % of the best RMS wins: a circle before a pointed arch
  before an ellipse.
* **Thickness**: the median distance of the extrados from the intrados, or given. **Depth**: the extent of the model
  behind the ring along the plane's normal, or given. *Barrel vault* writes the same solid as a vault.

The ring above the springing line is written with true arcs (three points each) in STEP and IFC.

<figure markdown="span">
  ![The section of the arch](../img/plugins/architecture/arch-section.png)
  <figcaption>The archivolt of the examples on its plane: the cut of the model (grey), the intrados seen from the point
  (red), the ring of the fitted pointed arch (blue).</figcaption>
</figure>

## Reading the fit

Every element carries its **fit**: the distance of the model's points from the solid, as the **median**, the **90th
percentile** and the **maximum**, with the number of points. A median of a few millimetres says that the solid follows
the model; a large maximum points at what the solid does not have (a chip, a cramp, a later patch). The points are those
of the region of the element: for a column the part chosen, for an arch the ring and the depth, for a moulding the box
of the solid without what lies behind its back line (the wall), whose count is given.

The fit compares the solid with the **model**, not with the monument: a model is itself a few millimetres from the real
surface, and the solid inherits that.

## Parameters

| Element | Parameter | Meaning |
|---|---|---|
| Column | lowest and highest height | the part of the model between them |
| | knots | 12; 2 to 60 |
| | as measured | the measured profile instead of the spline |
| | same axis as | the axis of a column already made |
| Moulding | height | of a path traced on the plan |
| | section at | metres from the start of the path |
| | window below, above, largest projection | around the path's point, in metres |
| | projects towards | the photos, left or right of the path |
| | depth into the wall | of the back line, 0.1 m |
| | simplification | Douglas-Peucker tolerance, 2 mm |
| Arch | point in the opening | clicked or (u, v) |
| | type | the best, semicircular, segmental, pointed, elliptical, as measured |
| | thickness, depth | 0 = measured |
| | barrel vault | the same solid as a vault |

## Files and formats

Everything of an element is in `<chunk data>/architecture/<id>/`: `solid/solid.step` (AP214), `solid/solid.stl`
(tessellated at 0.5 mm), `model.ply` (what the 3D view shows) and `element.json` with everything: source, parameters,
measured profile, geometry, fit, solid, files. The chunk's model, transform and products are never changed.

The coordinates of the STEP, STL and IFC are those of the chunk's system minus a round **origin** (kept per chunk, in
`element.json`): all the elements of a chunk line up with each other and with the mesh, with small numbers. *Export
STEP...* copies the files; *DXF...* writes the profile or the section at 1:1 in metres, on the layers SOLIDO,
MISURATO, ASSE and IMPOSTA.

### IFC

*Export IFC...* (also *File > Export > Architectural elements (IFC)*) writes IFC4 as the program's IFC export does:
project, site, metres, `IfcMapConversion` and `IfcProjectedCRS` on the chunk's origin; the survey mesh beside the
elements on request (`IfcGeographicElement`). The solids are **parametric**:

| Element | IFC class | Solid |
|---|---|---|
| Column | `IfcColumn` (COLUMN) | `IfcRevolvedAreaSolid` of the half-section (`IfcArbitraryClosedProfileDef`) |
| Moulding, straight | `IfcMember` (USERDEFINED, "Moulding") | `IfcExtrudedAreaSolid` |
| Moulding, along a path | `IfcMember` (USERDEFINED, "Moulding") | `IfcFixedReferenceSweptAreaSolid` on an `IfcIndexedPolyCurve` |
| Arch, vault | `IfcBuildingElementProxy` ("Arch", "Vault") | `IfcExtrudedAreaSolid` of lines and `IfcArcIndex` |

`Pset_PyArchInit_Element`: `Kind`, `Method`, `Name`, every parameter as `P_<name>` (metres, degrees), `Fit_median_m`,
`Fit_p90_m`, `Fit_max_m`, `Fit_points`, `Source`, `Representation` and a note that the solid is an interpretation of
the measured model; `Qto_PyArchInit_Element` with the volume and the area. An IFC read back by IfcOpenShell gives the
volume of CadQuery's solid within 1 % (tested, raking mouldings included).

## Command line

```bash
ppg plugin architecture install
ppg plugin architecture column PROJECT.ppg --polygon "POLYGON((x y, ...))" --zmin 0.05 --zmax 1.95 --knots 12
ppg plugin architecture moulding PROJECT.ppg --path "LINESTRING(x y, x y)" --height 3.35 --station 1 \
    --window -0.4,-0.03,0.02,0.3 --out-side -1
ppg plugin architecture moulding PROJECT.ppg --elevation "South elevation" --path "LINESTRING(u v, u v)"
ppg plugin architecture arch PROJECT.ppg --section "Section A" --pick 1.8,0.8 --type auto
ppg plugin architecture list PROJECT.ppg
ppg plugin architecture export PROJECT.ppg --out elements.ifc --mesh
ppg plugin architecture export PROJECT.ppg --out arch.dxf --ids Arco
```

## The examples, against the truth

The program made the models from 64 photos of the column and 40 of the wall. "Model against truth" is the distance of
the program's model from the true surface in the region: the error of the photogrammetry, which the solid inherits.

| Column (2 m, radius 0.30 m) | plugin | truth |
|---|---|---|
| tilt of the axis | 0.0005° | 0° |
| distance of the axis from the true one | 0.1 mm | 0 |
| radius at the base / at the top | 300.7 / 273.6 mm | 300.6 / 272.1 mm |
| entasis | 14.1 mm | 14.1 mm |
| knots: largest / median error | 1.6 / 0.7 mm | |
| volume | 0.5246 m³ | 0.5220 m³ (+0.49 %) |
| fit (median / p90) | 0.3 / 0.7 mm | |
| model against truth (median / p90) | 0.8 / 1.7 mm | |

| Pointed archivolt (span 1.6 m, ring 0.25 m) | plugin | truth |
|---|---|---|
| type | pointed | pointed |
| span | 1596.0 mm | 1600.0 mm |
| rise | 1389.4 mm | 1385.6 mm |
| radius | 1608.6 mm | 1600.0 mm |
| springing line | 1391.1 mm | 1400.0 mm |
| thickness of the ring | 253.6 mm | 250.0 mm |
| fit (median / p90) | 3.2 / 11.1 mm | |
| model against truth (median / p90) | 2.2 / 8.2 mm | |

| Cornice (projection 0.18 m) | plugin | truth |
|---|---|---|
| projection | 190.9 mm | 180.0 mm |
| area of the section in front of the wall | 329.9 cm² | 299.0 cm² (+10.3 %) |
| fit (median / p90) | 1.7 / 5.1 mm | |
| model against truth (median / p90 / max) | 2.0 / 5.6 / 20.4 mm | |

The cornice's section follows the model: its extra projection and area come from the top of the wall and the cornice,
which the program reconstructed up to 2 cm higher (it is seen only obliquely by the highest photos), not from the plugin.

## Limits

* **Mitred corners only.** A moulding turns a corner as a carved one does; rounded corners are not offered (the IFC
  would describe another solid).
* **Raking mouldings.** A sloped or turning path is swept by CadQuery with its own frame and written in the IFC as a
  sweep with the reference up: the two agree in volume and extent (tested), but the section of a steep raking cornice
  is the section normal to the path, not a vertical one.
* **The environment** is about 900 MB, and the first run after its installation takes about a minute.
* **Windows on ARM**: not available (no OpenCascade build).
* It does not detect elements by itself, does not model the carving of a capital, nor cross or domical vaults, nor
  walls and openings, and edits nothing in the 3D view. The solid is as good as the model it is fitted on: the fit says
  how far apart they are.

## Licences

The plugin is GPL-2.0-or-later. In its environment: CadQuery and OCP (Apache-2.0), OpenCascade (LGPL-2.1 with an
exception), VTK (BSD-3-Clause), ezdxf (MIT), nlopt (MIT and LGPL), numpy (BSD).
