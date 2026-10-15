# BIM: what the IFC export contains

The export is IFC4 (`ifcopenshell`), in metres, georeferenced with `IfcMapConversion` and
`IfcProjectedCRS` (EPSG of the chunk), and can be opened in Bonsai/BlenderBIM, Revit, Archicad,
Solibri, BIMcollab Zoom, usBIM or any IFC viewer.

## Two kinds of elements

* **Survey surfaces** — the photogrammetric model, and units as draped surfaces: an
  `IfcGeographicElement` (`TERRAIN`) with an `IfcTriangulatedFaceSet`. Good for context and
  for checking geometry, not for quantities (a surface has no volume).
* **US proxies** (*Proxy solidi delle US*) — one closed solid per stratigraphic unit, built
  between the unit's top surface (DEM) and its base (see *Strumenti > Proxy 3D delle US*).
  These make the file usable for quantity take-off.

## What a proxy element carries

| IFC entity | Content |
| --- | --- |
| `IfcGeographicElement` (`USERDEFINED`), name `US12` / `USM3` | watertight `IfcTriangulatedFaceSet`, colour per unit |
| `Pset_PyArchInit_US` | site, area, US, type and the fields of the PyArchInit US form (description, interpretation, period, phase, formation, ...) |
| `IfcElementQuantity` `Qto_PyArchInit_US` | `NetVolume` (m³), `GrossArea` (plan area, m²), `Height`, `TopElevationMax`, `TopElevationMin`, `BottomElevationMin` |
| `IfcMaterial` | the formation (`formazione`) or the stratigraphic definition of the unit |
| `IfcGroup` | `Periodo X - Fase Y`, one per chronological phase |
| `IfcRelConnectsElements` | one per stratigraphic relationship of the US form, named after it (*Copre*, *Taglia*, *Si appoggia a*...) |

So a BIM tool can build schedules (volume per US, per material, per phase), filter by
period, and colour or isolate units; the same volumes are written to PyArchInit's
`computo_metrico_table` by *Volume scavo / riporto*.

## Limits

* Solids are 2.5D: one top and one bottom height per DEM cell. Overhangs, niches and
  tunnels are not represented; walls get vertical faces along their plan.
* No parametric objects (no `IfcWall` with layers): archaeological units are not
  building components, and an editable parametric wall would be a re-interpretation.
* Relationships use `IfcRelConnectsElements` with the relationship name; there is no
  standard IFC vocabulary for stratigraphy, so other tools show them as generic
  connections.
