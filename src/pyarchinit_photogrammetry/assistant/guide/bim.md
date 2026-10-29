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
| `IfcRelConnectsElements` | one per stratigraphic relationship, named after it (*Copre*, *Taglia*, *Si appoggia a*...) and described by its edge of the Extended Matrix (see below) |
| `GlobalId` | the `node_uuid` of the US form, when it has one (see below) |

So a BIM tool can build schedules (volume per US, per material, per phase), filter by
period, and colour or isolate units; the same volumes are written to PyArchInit's
`computo_metrico_table` by *Volume scavo / riporto*.

## Identifiers: the same unit in PyArchInit, the Extended Matrix and the IFC

PyArchInit gives every US form a `node_uuid`: it is the id of the unit's node in the Extended
Matrix (the graph that PyArchInit and EMStudio build with s3dgraphy, and that StratiGraph shares),
and `entity_uuid` is the persistent identifier of the form. An element of a unit whose form has a
`node_uuid` takes that UUID as its `GlobalId` (the 22 characters IFC uses for a UUID:
`ifcopenshell.guid.expand(GlobalId)` gives it back), and both UUIDs are also in
`Pset_PyArchInit_US` (`node_uuid`, `entity_uuid`). A unit therefore has one identifier in the
database, in the matrix and in the BIM model. Units without a form, or forms without `node_uuid`,
get a new `GlobalId`; if two elements carry the same unit, the second gets a new one.

## Stratigraphic relationships

A relationship is written once, from the unit the edge starts from, whichever form declares it
(*Copre 2* in the form of US 1 and *Coperto da 1* in that of US 2 are one connection, US 1 to US 2).
Its `Description` is the edge of the Extended Matrix:

| US form (it / en / de) | Written as | Extended Matrix edge |
| --- | --- | --- |
| Copre, Coperto da / Covers, Covered by / Liegt über, Liegt unter | Copre | `overlies` |
| Taglia, Tagliato da / Cuts, Cut by / Schneidet, Wird geschnitten | Taglia | `cuts` |
| Riempie, Riempito da / Fills, Filled by / Verfüllt, Wird verfüllt durch | Riempie | `fills` |
| Si appoggia a, Gli si appoggia / Abuts, Supports / Stützt sich auf, Wird gestüzt von | Si appoggia a | `abuts` |
| Posteriore a, Anteriore a | Posteriore a | `is_after` |
| Uguale a / Same as / Entspricht | Uguale a (once per pair) | `equals` |
| Si lega a / Connected to / Bindet an | Si lega a (once per pair) | `bonded_to` |

The survey export with units (*Esporta IFC*, units drawn or from a PyArchInit site) carries the
same identifiers and relationships; the polygons of one unit make one element.

## Limits

* Solids are 2.5D: one top and one bottom height per DEM cell. Overhangs, niches and
  tunnels are not represented; walls get vertical faces along their plan.
* No parametric objects (no `IfcWall` with layers): archaeological units are not
  building components, and an editable parametric wall would be a re-interpretation.
* Relationships use `IfcRelConnectsElements` with the relationship name and the Extended Matrix
  edge; there is no standard IFC vocabulary for stratigraphy, so other tools show them as
  generic connections.
