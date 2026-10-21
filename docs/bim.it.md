# BIM: cosa contiene l'esportazione IFC

L'esportazione è IFC4 (`ifcopenshell`), in metri, georeferenziata con `IfcMapConversion` e
`IfcProjectedCRS` (EPSG del chunk), e si apre in Bonsai/BlenderBIM, Revit, Archicad, Solibri,
BIMcollab Zoom, usBIM o in qualsiasi visualizzatore IFC.

## Due tipi di elementi

* **Superfici del rilievo** — il modello fotogrammetrico, e le unità come superfici drappeggiate:
  un `IfcGeographicElement` (`TERRAIN`) con un `IfcTriangulatedFaceSet`. Vanno bene per il
  contesto e per controllare la geometria, non per le quantità (una superficie non ha volume).
* **Proxy delle US** (*Proxy solidi delle US*) — un solido chiuso per ogni unità stratigrafica,
  costruito tra la superficie superiore dell'unità (DEM) e la sua base (vedi *Strumenti > Proxy 3D
  delle US*). Rendono il file utilizzabile per il computo metrico.

## Cosa porta con sé un elemento proxy

| Entità IFC | Contenuto |
| --- | --- |
| `IfcGeographicElement` (`USERDEFINED`), nome `US12` / `USM3` | `IfcTriangulatedFaceSet` chiuso (watertight), colore per unità |
| `Pset_PyArchInit_US` | sito, area, US, tipo e i campi della scheda US di PyArchInit (descrizione, interpretazione, periodo, fase, formazione, ...) |
| `IfcElementQuantity` `Qto_PyArchInit_US` | `NetVolume` (m³), `GrossArea` (area in pianta, m²), `Height`, `TopElevationMax`, `TopElevationMin`, `BottomElevationMin` |
| `IfcMaterial` | la formazione (`formazione`) o la definizione stratigrafica dell'unità |
| `IfcGroup` | `Periodo X - Fase Y`, uno per ogni fase cronologica |
| `IfcRelConnectsElements` | uno per ogni rapporto stratigrafico, con il suo nome (*Copre*, *Taglia*, *Si appoggia a*...) e descritto dal suo arco dell'Extended Matrix (vedi sotto) |
| `GlobalId` | il `node_uuid` della scheda US, quando ce l'ha (vedi sotto) |

Così uno strumento BIM può costruire abachi (volume per US, per materiale, per fase), filtrare per
periodo, e colorare o isolare le unità; gli stessi volumi vengono scritti nella
`computo_metrico_table` di PyArchInit da *Volume scavo / riporto*.

## Identificatori: la stessa unità in PyArchInit, nell'Extended Matrix e nell'IFC

PyArchInit dà a ogni scheda US un `node_uuid`: è l'id del nodo dell'unità nell'Extended Matrix (il
grafo che PyArchInit ed EMStudio costruiscono con s3dgraphy, e che StratiGraph condivide), e
`entity_uuid` è l'identificatore persistente della scheda. Un elemento di un'unità la cui scheda ha
un `node_uuid` prende quell'UUID come `GlobalId` (i 22 caratteri con cui l'IFC scrive un UUID:
`ifcopenshell.guid.expand(GlobalId)` lo restituisce), ed entrambi gli UUID sono anche in
`Pset_PyArchInit_US` (`node_uuid`, `entity_uuid`). Un'unità ha quindi un solo identificatore nel
database, nella matrice e nel modello BIM. Le unità senza scheda, o le schede senza `node_uuid`,
ricevono un `GlobalId` nuovo; se due elementi portano la stessa unità, il secondo ne riceve uno
nuovo.

## Rapporti stratigrafici

Un rapporto viene scritto una volta sola, dall'unità da cui parte l'arco, qualunque sia la scheda
che lo dichiara (*Copre 2* nella scheda della US 1 e *Coperto da 1* in quella della US 2 sono una
sola connessione, dalla US 1 alla US 2). La sua `Description` è l'arco dell'Extended Matrix:

| Scheda US (it / en / de) | Scritto come | Arco dell'Extended Matrix |
| --- | --- | --- |
| Copre, Coperto da / Covers, Covered by / Liegt über, Liegt unter | Copre | `overlies` |
| Taglia, Tagliato da / Cuts, Cut by / Schneidet, Wird geschnitten | Taglia | `cuts` |
| Riempie, Riempito da / Fills, Filled by / Verfüllt, Wird verfüllt durch | Riempie | `fills` |
| Si appoggia a, Gli si appoggia / Abuts, Supports / Stützt sich auf, Wird gestüzt von | Si appoggia a | `abuts` |
| Posteriore a, Anteriore a | Posteriore a | `is_after` |
| Uguale a / Same as / Entspricht | Uguale a (una volta per coppia) | `equals` |
| Si lega a / Connected to / Bindet an | Si lega a (una volta per coppia) | `bonded_to` |

L'esportazione del rilievo con le unità (*Esporta IFC*, unità disegnate o di un sito PyArchInit)
porta gli stessi identificatori e rapporti; i poligoni di una stessa unità fanno un solo elemento.

## Limiti

* I solidi sono 2,5D: una quota superiore e una inferiore per ogni cella del DEM. Aggetti, nicchie
  e gallerie non sono rappresentati; i muri hanno facce verticali lungo la loro pianta.
* Niente oggetti parametrici (nessun `IfcWall` a strati): le unità archeologiche non sono
  componenti edilizi, e un muro parametrico modificabile sarebbe una reinterpretazione.
* I rapporti usano `IfcRelConnectsElements` con il nome del rapporto e l'arco dell'Extended
  Matrix; non esiste un vocabolario IFC standard per la stratigrafia, così gli altri strumenti li
  mostrano come connessioni generiche.
