# Conservation

The *Conservation* plugin maps **the state of a wall face** on the program's elevations, as a restorer does on a
rectified photograph:

- **decay** as areas, each with its term from a shared glossary;
- **cracks** as lines, with their opening;
- **interventions**: the parts to treat and the parts already treated;
- **notes** at a point.

The map becomes a plate at scale with the legend, tables of areas and lengths, a DXF, GeoPackages for GIS, a QGIS
project, records in PyArchInit and surface features in an IFC 4.3 model. A second campaign on the same face, months or
years later, is compared with the first.

The plugin never judges for you. Terms come from the standards' glossaries and are chosen by you; the condition and
urgency classes of EN 16096 are yours, the plugin only shows a *suggestion* with the rule that gives it; what the
detectors find is a *proposal* until you accept it.

This page describes version 0.1.0 of the plugin, which **requires pyarchinit-photogrammetry 1.7.1** (drawings kept
with the elevations). The README and the code are in the [plugin
repository](https://github.com/enzococca/pyarchinit-photogrammetry-plugins/tree/main/plugins/conservation).

## In short

1. Make an **elevation** of the face with the program (*Elevation* button or `ppg elevation`): on a plane for a wall,
   on a cylinder for a round tower or an apse.
2. In the pane *Conservazione* / *Conservation* (on the right), choose the elevation and press **New campaign...**:
   date, surveyor, method, weather, notes.
3. **Map**: choose a term in the list (grouped by family, search in Italian, English or French), then **Area**,
   **Line** or **Point** and draw on the elevation. A form asks intensity, opening, cause, urgency and notes.
4. **Proposals...**: cracks, losses of material, colour classes and SAM segments proposed from the image and the
   relief. **Accept**, **Accept all** (those with a term: it says how many are left to name) or **Discard** them.
5. **Interventions**: **Treatment (area)** / **Treatment (line)**, or **To treat** on a decay area chosen in the
   table.
6. **Classes CC and UC...** (EN 16096) for the face, **Recommendation RC...** for the object on that date.
7. **Plate...**, **Tables...**, **Drawings...**, **Open in QGIS**, **Save in PyArchInit**, **IFC 4.3...**.
8. **Compare...** two campaigns of the same face.

## What each step needs

| Step | Needs |
|---|---|
| Campaign and mapping | an elevation of the program (plane, cylinder or section) with its orthophoto |
| Proposals from the relief (cracks, losses) | the elevation's relief (`relief.tif`, written with every elevation): they work where it has values and say how much of the window they covered |
| Proposals from the image (colours, SAM) | the orthophoto only; SAM 2.1 installed by the program (*Strumenti > SAM*) |
| Photos of a feature | a chunk with aligned cameras |
| Units linked to features | US or USM drawn on the same elevation with the program's *Disegna US* |
| QGIS | QGIS installed |
| PyArchInit | the program connected to PyArchInit (menu *PyArchInit*) and the project's site chosen |
| IFC 4.3 | a plane or cylinder elevation (sections are refused); ifcopenshell, present in the app |
| Comparison | two campaigns of the same face; for a later survey, a second chunk **already aligned** with the first (*Allinea chunk*) |

## The standards and what each one gives

- **ICOMOS-ISCS 2008**, *Illustrated glossary on stone deterioration patterns*: the six groups (general terms; crack
  and deformation; detachment; features induced by material loss; discoloration and deposit; biological
  colonization), 39 main terms and their sub-types. Its terms are the plugin's **codes** (`ISCS.CRACK`,
  `ISCS.CRACK.HAIR_CRACK`, `ISCS.ALVEOLIZATION`, `ISCS.CRUST.BLACK_CRUST`...). [The glossary on the ICOMOS
  site](https://www.icomos.org/public/publications/monuments_and_sites/15/pdf/Monuments_and_Sites_15_ISCS_Glossary_Stone.pdf).
- **UNI 11182:2006**, *Materiali lapidei naturali ed artificiali - Descrizione della forma di alterazione*: the 27
  Italian terms (alterazione cromatica, alveolizzazione, crosta, distacco, efflorescenza, erosione, fessurazione,
  lacuna, mancanza, patina biologica...). Each is mapped onto the ICOMOS-ISCS term or terms it corresponds to. No
  official mapping exists: **the mapping is the plugin's**, kept as data. A feature keeps both codes; the Italian
  interface shows the UNI term first, the English one the ICOMOS-ISCS term; the plate prints both.
- **Local terms** (`LOCAL.*`): masonry and structural symptoms that neither glossary has (out of plumb, bulging, loss
  of mortar, displaced stones, open joint). They are flagged as not from a standard in every export. A project can add
  its own.
- **EN 15898:2019**, *Main general terms and definitions*: the words of the records (condition, damage, decay,
  conservation, restoration, intervention, treatment...) and the families of the interventions (preventive, remedial:
  cleaning, consolidation, desalination, biocide, filling, grouting, repointing, protection, reintegration,
  replacement, removal of earlier materials; restoration).
- **EN 16096:2012**, *Condition survey and report of built cultural heritage*: per component (here, the face) a
  **condition class CC 0-3** and an **urgency class UC 0-3**; for the whole object a **recommendation class RC 0-3**,
  once per survey date. The class wordings are those quoted from the standard in the literature (its text is not
  freely available): check them against your copy of the standard.
- **IFC 4.3 (ISO 16739-1:2024)**: `IfcSurfaceFeature` (DEFECT, TREATMENT) attached to the wall with
  `IfcRelAdheresToElement`.

The classes of EN 16096, as the plugin shows them. **These wordings come from a secondary source**, a thesis that
quotes the standard, because the standard's text is not freely available: check them against your copy of the
standard before you print them in a report.

| Class | Condition (CC) | Urgency (UC) | Recommendation (RC) |
|---|---|---|---|
| 0 | No symptoms | Long term | No measures |
| 1 | Minor symptoms | Intermediate term | Maintenance / preventive conservation |
| 2 | Moderately strong symptoms | Short term | Moderate repair and/or further investigation |
| 3 | Major symptoms | Urgent and immediate | Major interventions based on diagnosis |

The names of the terms are the glossaries' own. The one-line definitions are the plugin's paraphrases, with the page
or section of their source: read the glossaries for the real text.

### The legend

**No standard prescribes colours or hatches** (the hatches of the old NorMaL 1/88 have not survived in a usable
form): the legend below is the plugin's default. You can change a term's colour and hatch for the project (0.1.0:
on the command line, `legend --set`), and the legend is also written as QGIS styles. The table is generated from the
plugin's vocabulary (`scripts/legend_table.py`).

<!-- legend:start -->
**Glossary terms (ICOMOS-ISCS and UNI 11182)**

| Code | ICOMOS-ISCS name | UNI 11182 name | Colour | Hatch (matplotlib / DXF) |
|---|---|---|---|---|
| `ISCS.ALTERATION` | Alteration | - | <span style="color:#7f7f7f">&#9632;</span> `#7f7f7f` | `//` / `ANSI31` |
| `ISCS.DAMAGE` | Damage | - | <span style="color:#999999">&#9632;</span> `#999999` | `\\` / `ANSI32` |
| `ISCS.DECAY` | Decay | - | <span style="color:#666666">&#9632;</span> `#666666` | `xx` / `ANSI37` |
| `ISCS.DEGRADATION` | Degradation | - | <span style="color:#b3b3b3">&#9632;</span> `#b3b3b3` | `..` / `DOTS` |
| `ISCS.DETERIORATION` | Deterioration | - | <span style="color:#4d4d4d">&#9632;</span> `#4d4d4d` | `++` / `CROSS` |
| `ISCS.WEATHERING` | Weathering | - | <span style="color:#8c8c8c">&#9632;</span> `#8c8c8c` | `--` / `LINE` |
| `ISCS.CRACK` | Crack | fessurazione | <span style="color:#d62728">&#9632;</span> `#d62728` | `//` / `ANSI31` |
| `ISCS.CRACK.FRACTURE` | Fracture | fessurazione | <span style="color:#e8505b">&#9632;</span> `#e8505b` | `\\` / `ANSI32` |
| `ISCS.CRACK.STAR_CRACK` | Star crack | - | <span style="color:#b2182b">&#9632;</span> `#b2182b` | `xx` / `ANSI37` |
| `ISCS.CRACK.HAIR_CRACK` | Hair crack | - | <span style="color:#ff6f61">&#9632;</span> `#ff6f61` | `..` / `DOTS` |
| `ISCS.CRACK.CRAQUELE` | Craquele | - | <span style="color:#c51b7d">&#9632;</span> `#c51b7d` | `++` / `CROSS` |
| `ISCS.CRACK.SPLITTING` | Splitting | - | <span style="color:#8b0000">&#9632;</span> `#8b0000` | `--` / `LINE` |
| `ISCS.DEFORMATION` | Deformation | deformazione | <span style="color:#ff3355">&#9632;</span> `#ff3355` | `\|\|` / `ANSI33` |
| `ISCS.BLISTERING` | Blistering | rigonfiamento | <span style="color:#ff7f0e">&#9632;</span> `#ff7f0e` | `//` / `ANSI31` |
| `ISCS.BURSTING` | Bursting | - | <span style="color:#fdae61">&#9632;</span> `#fdae61` | `\\` / `ANSI32` |
| `ISCS.DELAMINATION` | Delamination | distacco | <span style="color:#e6550d">&#9632;</span> `#e6550d` | `xx` / `ANSI37` |
| `ISCS.DELAMINATION.EXFOLIATION` | Exfoliation | esfoliazione | <span style="color:#fd8d3c">&#9632;</span> `#fd8d3c` | `..` / `DOTS` |
| `ISCS.DISINTEGRATION` | Disintegration | disgregazione | <span style="color:#d95f0e">&#9632;</span> `#d95f0e` | `++` / `CROSS` |
| `ISCS.DISINTEGRATION.CRUMBLING` | Crumbling | - | <span style="color:#f16913">&#9632;</span> `#f16913` | `--` / `LINE` |
| `ISCS.DISINTEGRATION.GRANULAR_DISINTEGRATION` | Granular disintegration | disgregazione | <span style="color:#feb24c">&#9632;</span> `#feb24c` | `\|\|` / `ANSI33` |
| `ISCS.DISINTEGRATION.POWDERING` | Powdering, chalking | disgregazione | <span style="color:#cc4c02">&#9632;</span> `#cc4c02` | `oo` / `HONEY` |
| `ISCS.DISINTEGRATION.SANDING` | Sanding | - | <span style="color:#ffa64d">&#9632;</span> `#ffa64d` | `**` / `STARS` |
| `ISCS.DISINTEGRATION.SUGARING` | Sugaring | - | <span style="color:#e08214">&#9632;</span> `#e08214` | `///` / `ANSI34` |
| `ISCS.FRAGMENTATION` | Fragmentation | - | <span style="color:#b35806">&#9632;</span> `#b35806` | `\\\` / `ANSI35` |
| `ISCS.FRAGMENTATION.SPLINTERING` | Splintering | - | <span style="color:#f4a582">&#9632;</span> `#f4a582` | `x` / `ANSI38` |
| `ISCS.FRAGMENTATION.CHIPPING` | Chipping | - | <span style="color:#ff9933">&#9632;</span> `#ff9933` | `...` / `AR-SAND` |
| `ISCS.PEELING` | Peeling | - | <span style="color:#d94801">&#9632;</span> `#d94801` | `O` / `ESCHER` |
| `ISCS.SCALING` | Scaling | scagliatura | <span style="color:#fdd0a2">&#9632;</span> `#fdd0a2` | `-` / `DASH` |
| `ISCS.SCALING.FLAKING` | Flaking | scagliatura | <span style="color:#a63603">&#9632;</span> `#a63603` | `\|` / `BRICK` |
| `ISCS.SCALING.CONTOUR_SCALING` | Contour scaling | scagliatura | <span style="color:#ffb366">&#9632;</span> `#ffb366` | `+` / `NET` |
| `ISCS.ALVEOLIZATION` | Alveolization | alveolizzazione | <span style="color:#9467bd">&#9632;</span> `#9467bd` | `//` / `ANSI31` |
| `ISCS.ALVEOLIZATION.COVING` | Coving | - | <span style="color:#6a3d9a">&#9632;</span> `#6a3d9a` | `\\` / `ANSI32` |
| `ISCS.EROSION` | Erosion | erosione | <span style="color:#b39ddb">&#9632;</span> `#b39ddb` | `xx` / `ANSI37` |
| `ISCS.EROSION.DIFFERENTIAL_EROSION` | Differential erosion | degradazione differenziale | <span style="color:#7b4fa0">&#9632;</span> `#7b4fa0` | `..` / `DOTS` |
| `ISCS.EROSION.LOSS_OF_COMPONENTS` | Loss of components | - | <span style="color:#cab2d6">&#9632;</span> `#cab2d6` | `++` / `CROSS` |
| `ISCS.EROSION.LOSS_OF_MATRIX` | Loss of matrix | - | <span style="color:#542788">&#9632;</span> `#542788` | `--` / `LINE` |
| `ISCS.EROSION.ROUNDING` | Rounding | - | <span style="color:#8073ac">&#9632;</span> `#8073ac` | `\|\|` / `ANSI33` |
| `ISCS.EROSION.ROUGHENING` | Roughening | - | <span style="color:#5e3c99">&#9632;</span> `#5e3c99` | `oo` / `HONEY` |
| `ISCS.MECHANICAL_DAMAGE` | Mechanical damage | - | <span style="color:#a68cc4">&#9632;</span> `#a68cc4` | `**` / `STARS` |
| `ISCS.MECHANICAL_DAMAGE.IMPACT_DAMAGE` | Impact damage | - | <span style="color:#3f007d">&#9632;</span> `#3f007d` | `///` / `ANSI34` |
| `ISCS.MECHANICAL_DAMAGE.CUT` | Cut | - | <span style="color:#bcbddc">&#9632;</span> `#bcbddc` | `\\\` / `ANSI35` |
| `ISCS.MECHANICAL_DAMAGE.SCRATCH` | Scratch | - | <span style="color:#756bb1">&#9632;</span> `#756bb1` | `x` / `ANSI38` |
| `ISCS.MECHANICAL_DAMAGE.ABRASION` | Abrasion | - | <span style="color:#9e9ac8">&#9632;</span> `#9e9ac8` | `...` / `AR-SAND` |
| `ISCS.MECHANICAL_DAMAGE.KEYING` | Keying | - | <span style="color:#4a1486">&#9632;</span> `#4a1486` | `O` / `ESCHER` |
| `ISCS.MICROKARST` | Microkarst | - | <span style="color:#dadaeb">&#9632;</span> `#dadaeb` | `-` / `DASH` |
| `ISCS.MISSING_PART` | Missing part | mancanza | <span style="color:#6950a1">&#9632;</span> `#6950a1` | `\|` / `BRICK` |
| `ISCS.MISSING_PART.GAP` | Gap | lacuna | <span style="color:#8e6cbf">&#9632;</span> `#8e6cbf` | `+` / `NET` |
| `ISCS.PERFORATION` | Perforation | - | <span style="color:#2d004b">&#9632;</span> `#2d004b` | `o` / `GRAVEL` |
| `ISCS.PITTING` | Pitting | pitting | <span style="color:#c2a5cf">&#9632;</span> `#c2a5cf` | `*` / `TRIANG` |
| `ISCS.CRUST` | Crust | crosta | <span style="color:#1f77b4">&#9632;</span> `#1f77b4` | `//` / `ANSI31` |
| `ISCS.CRUST.BLACK_CRUST` | Black crust | crosta | <span style="color:#08519c">&#9632;</span> `#08519c` | `\\` / `ANSI32` |
| `ISCS.CRUST.SALT_CRUST` | Salt crust | crosta | <span style="color:#6baed6">&#9632;</span> `#6baed6` | `xx` / `ANSI37` |
| `ISCS.DEPOSIT` | Deposit | deposito superficiale | <span style="color:#3182bd">&#9632;</span> `#3182bd` | `..` / `DOTS` |
| `ISCS.DISCOLOURATION` | Discolouration | alterazione cromatica | <span style="color:#2c7fb8">&#9632;</span> `#2c7fb8` | `++` / `CROSS` |
| `ISCS.DISCOLOURATION.COLOURATION` | Colouration | alterazione cromatica | <span style="color:#253494">&#9632;</span> `#253494` | `--` / `LINE` |
| `ISCS.DISCOLOURATION.BLEACHING` | Bleaching | alterazione cromatica | <span style="color:#41b6c4">&#9632;</span> `#41b6c4` | `\|\|` / `ANSI33` |
| `ISCS.DISCOLOURATION.MOIST_AREA` | Moist area | - | <span style="color:#225ea8">&#9632;</span> `#225ea8` | `oo` / `HONEY` |
| `ISCS.DISCOLOURATION.STAINING` | Staining | macchia | <span style="color:#9ecae1">&#9632;</span> `#9ecae1` | `**` / `STARS` |
| `ISCS.EFFLORESCENCE` | Efflorescence | efflorescenza | <span style="color:#0868ac">&#9632;</span> `#0868ac` | `///` / `ANSI34` |
| `ISCS.ENCRUSTATION` | Encrustation | incrostazione | <span style="color:#4eb3d3">&#9632;</span> `#4eb3d3` | `\\\` / `ANSI35` |
| `ISCS.ENCRUSTATION.CONCRETION` | Concretion | incrostazione | <span style="color:#084081">&#9632;</span> `#084081` | `x` / `ANSI38` |
| `ISCS.FILM` | Film | pellicola | <span style="color:#7bccc4">&#9632;</span> `#7bccc4` | `...` / `AR-SAND` |
| `ISCS.GLOSSY_ASPECT` | Glossy aspect | - | <span style="color:#2b8cbe">&#9632;</span> `#2b8cbe` | `O` / `ESCHER` |
| `ISCS.GRAFFITI` | Graffiti | graffito vandalico | <span style="color:#a6bddb">&#9632;</span> `#a6bddb` | `-` / `DASH` |
| `ISCS.PATINA` | Patina | patina | <span style="color:#045a8d">&#9632;</span> `#045a8d` | `\|` / `BRICK` |
| `ISCS.PATINA.IRON_RICH_PATINA` | Iron rich patina | - | <span style="color:#74a9cf">&#9632;</span> `#74a9cf` | `+` / `NET` |
| `ISCS.PATINA.OXALATE_PATINA` | Oxalate patina | - | <span style="color:#0570b0">&#9632;</span> `#0570b0` | `o` / `GRAVEL` |
| `ISCS.SOILING` | Soiling | deposito superficiale | <span style="color:#c6dbef">&#9632;</span> `#c6dbef` | `*` / `TRIANG` |
| `ISCS.SUBFLORESCENCE` | Subflorescence | - | <span style="color:#3690c0">&#9632;</span> `#3690c0` | `xxx` / `SQUARE` |
| `ISCS.BIOLOGICAL_COLONIZATION` | Biological colonization | colonizzazione biologica | <span style="color:#2ca02c">&#9632;</span> `#2ca02c` | `//` / `ANSI31` |
| `ISCS.ALGA` | Alga | patina biologica | <span style="color:#1b7837">&#9632;</span> `#1b7837` | `\\` / `ANSI32` |
| `ISCS.LICHEN` | Lichen | - | <span style="color:#7fbc41">&#9632;</span> `#7fbc41` | `xx` / `ANSI37` |
| `ISCS.MOSS` | Moss | - | <span style="color:#4d9221">&#9632;</span> `#4d9221` | `..` / `DOTS` |
| `ISCS.MOULD` | Mould | patina biologica | <span style="color:#a6d96a">&#9632;</span> `#a6d96a` | `++` / `CROSS` |
| `ISCS.PLANT` | Plant | presenza di vegetazione | <span style="color:#006837">&#9632;</span> `#006837` | `--` / `LINE` |
| `UNI.ALTERAZIONE_CROMATICA` | Discolouration, Colouration, Bleaching | alterazione cromatica | <span style="color:#1f77b4">&#9632;</span> `#1f77b4` | `//` / `ANSI31` |
| `UNI.ALVEOLIZZAZIONE` | Alveolization | alveolizzazione | <span style="color:#9467bd">&#9632;</span> `#9467bd` | `//` / `ANSI31` |
| `UNI.COLATURA` | Staining | colatura | <span style="color:#08519c">&#9632;</span> `#08519c` | `\\` / `ANSI32` |
| `UNI.COLONIZZAZIONE_BIOLOGICA` | Biological colonization | colonizzazione biologica | <span style="color:#2ca02c">&#9632;</span> `#2ca02c` | `//` / `ANSI31` |
| `UNI.CROSTA` | Crust, Black crust, Salt crust | crosta | <span style="color:#6baed6">&#9632;</span> `#6baed6` | `xx` / `ANSI37` |
| `UNI.DEFORMAZIONE` | Deformation | deformazione | <span style="color:#d62728">&#9632;</span> `#d62728` | `//` / `ANSI31` |
| `UNI.DEGRADAZIONE_DIFFERENZIALE` | Differential erosion | degradazione differenziale | <span style="color:#6a3d9a">&#9632;</span> `#6a3d9a` | `\\` / `ANSI32` |
| `UNI.DEPOSITO_SUPERFICIALE` | Deposit, Soiling | deposito superficiale | <span style="color:#3182bd">&#9632;</span> `#3182bd` | `..` / `DOTS` |
| `UNI.DISGREGAZIONE` | Disintegration, Powdering, chalking, Granular disintegration | disgregazione | <span style="color:#ff7f0e">&#9632;</span> `#ff7f0e` | `//` / `ANSI31` |
| `UNI.DISTACCO` | Delamination, Blistering, Scaling | distacco | <span style="color:#fdae61">&#9632;</span> `#fdae61` | `\\` / `ANSI32` |
| `UNI.EFFLORESCENZA` | Efflorescence, Subflorescence | efflorescenza | <span style="color:#2c7fb8">&#9632;</span> `#2c7fb8` | `++` / `CROSS` |
| `UNI.EROSIONE` | Erosion | erosione | <span style="color:#b39ddb">&#9632;</span> `#b39ddb` | `xx` / `ANSI37` |
| `UNI.ESFOLIAZIONE` | Exfoliation | esfoliazione | <span style="color:#e6550d">&#9632;</span> `#e6550d` | `xx` / `ANSI37` |
| `UNI.FESSURAZIONE` | Crack, Fracture | fessurazione | <span style="color:#e8505b">&#9632;</span> `#e8505b` | `\\` / `ANSI32` |
| `UNI.FRONTE_DI_RISALITA` | Moist area | fronte di risalita | <span style="color:#253494">&#9632;</span> `#253494` | `--` / `LINE` |
| `UNI.GRAFFITO_VANDALICO` | Graffiti | graffito vandalico | <span style="color:#41b6c4">&#9632;</span> `#41b6c4` | `\|\|` / `ANSI33` |
| `UNI.INCROSTAZIONE` | Encrustation, Concretion | incrostazione | <span style="color:#225ea8">&#9632;</span> `#225ea8` | `oo` / `HONEY` |
| `UNI.LACUNA` | Gap, Missing part | lacuna | <span style="color:#7b4fa0">&#9632;</span> `#7b4fa0` | `..` / `DOTS` |
| `UNI.MACCHIA` | Staining | macchia | <span style="color:#9ecae1">&#9632;</span> `#9ecae1` | `**` / `STARS` |
| `UNI.MANCANZA` | Missing part | mancanza | <span style="color:#cab2d6">&#9632;</span> `#cab2d6` | `++` / `CROSS` |
| `UNI.PATINA` | Patina | patina | <span style="color:#0868ac">&#9632;</span> `#0868ac` | `///` / `ANSI34` |
| `UNI.PATINA_BIOLOGICA` | Biological colonization, Alga, Mould | patina biologica | <span style="color:#1b7837">&#9632;</span> `#1b7837` | `\\` / `ANSI32` |
| `UNI.PELLICOLA` | Film | pellicola | <span style="color:#4eb3d3">&#9632;</span> `#4eb3d3` | `\\\` / `ANSI35` |
| `UNI.PITTING` | Pitting | pitting | <span style="color:#542788">&#9632;</span> `#542788` | `--` / `LINE` |
| `UNI.PRESENZA_DI_VEGETAZIONE` | Plant | presenza di vegetazione | <span style="color:#7fbc41">&#9632;</span> `#7fbc41` | `xx` / `ANSI37` |
| `UNI.RIGONFIAMENTO` | Blistering | rigonfiamento | <span style="color:#fd8d3c">&#9632;</span> `#fd8d3c` | `..` / `DOTS` |
| `UNI.SCAGLIATURA` | Scaling, Flaking, Contour scaling | scagliatura | <span style="color:#d95f0e">&#9632;</span> `#d95f0e` | `++` / `CROSS` |

**Local terms (masonry, not in a standard)**

| Code | English name | Italian name | Colour | Hatch (matplotlib / DXF) |
|---|---|---|---|---|
| `LOCAL.OUT_OF_PLUMB` | Out of plumb | fuori piombo | <span style="color:#8c564b">&#9632;</span> `#8c564b` | `//` / `ANSI31` |
| `LOCAL.BULGING` | Bulging | spanciamento | <span style="color:#a0522d">&#9632;</span> `#a0522d` | `\\` / `ANSI32` |
| `LOCAL.LOSS_OF_MORTAR` | Loss of mortar | perdita di malta | <span style="color:#6b3e26">&#9632;</span> `#6b3e26` | `xx` / `ANSI37` |
| `LOCAL.DISPLACED_STONES` | Displaced stones | pietre smosse | <span style="color:#c49c94">&#9632;</span> `#c49c94` | `..` / `DOTS` |
| `LOCAL.OPEN_JOINT` | Open joint | giunto aperto | <span style="color:#7f4f24">&#9632;</span> `#7f4f24` | `++` / `CROSS` |

**Interventions (families of EN 15898)**

| Code | English name | Italian name | Colour | Hatch (matplotlib / DXF) |
|---|---|---|---|---|
| `INT.PREVENTIVE` | Preventive conservation | conservazione preventiva | <span style="color:#17becf">&#9632;</span> `#17becf` | `\|` / `ANSI33` |
| `INT.REMEDIAL` | Remedial conservation | conservazione curativa | <span style="color:#00a0b0">&#9632;</span> `#00a0b0` | `-` / `LINE` |
| `INT.CLEANING` | Cleaning | pulitura | <span style="color:#0e7c86">&#9632;</span> `#0e7c86` | `+` / `NET` |
| `INT.CONSOLIDATION` | Consolidation | consolidamento | <span style="color:#5fd3d6">&#9632;</span> `#5fd3d6` | `x` / `ANSI37` |
| `INT.DESALINATION` | Desalination | desalinizzazione | <span style="color:#007a87">&#9632;</span> `#007a87` | `o` / `GRAVEL` |
| `INT.BIOCIDE` | Biocide treatment | trattamento biocida | <span style="color:#2ec4b6">&#9632;</span> `#2ec4b6` | `.` / `DOTS` |
| `INT.FILLING` | Filling | stuccatura | <span style="color:#00b4d8">&#9632;</span> `#00b4d8` | `/` / `ANSI31` |
| `INT.GROUTING` | Grouting | iniezione | <span style="color:#0096c7">&#9632;</span> `#0096c7` | `\` / `ANSI32` |
| `INT.REPOINTING` | Repointing | ristilatura dei giunti | <span style="color:#48cae4">&#9632;</span> `#48cae4` | `*` / `TRIANG` |
| `INT.PROTECTION` | Protection, coating | protezione | <span style="color:#03045e">&#9632;</span> `#03045e` | `O` / `HONEY` |
| `INT.REINTEGRATION` | Reintegration | reintegrazione | <span style="color:#00838f">&#9632;</span> `#00838f` | `\|\|` / `BRICK` |
| `INT.REPLACEMENT` | Replacement | sostituzione | <span style="color:#26a69a">&#9632;</span> `#26a69a` | `--` / `DASH` |
| `INT.REMOVAL` | Removal of earlier materials | rimozione di materiali precedenti | <span style="color:#4dd0e1">&#9632;</span> `#4dd0e1` | `++` / `CROSS` |
| `INT.RESTORATION` | Restoration | restauro | <span style="color:#006064">&#9632;</span> `#006064` | `xx` / `SQUARE` |
<!-- legend:end -->

## The pane, step by step

### 1. Elevation and campaign

The pane lists the chunk's elevations with their campaigns. A campaign is one survey of one face on a date: the
chunk's date is proposed. **Show** shows the elevation with the campaign's features drawn in the layers *Degrado*,
*Lesioni*, *Interventi* and *Note* (and *Proposte*, dashed). When the face belongs to a wall of the *Masonry* plugin,
it is named after the wall.

### 2. Mapping

Choose the term in the list: grouped by family, with a search in the three languages, the favourites and the last ten
used. Then:

- **Area**: a polygon (decay);
- **Line**: a crack or an open joint;
- **Point**: a note.

The feature takes the term, its extent (m² or m) and the campaign. The form asks intensity (low, medium, high), the
opening of a crack (mm), the depth of a loss (mm), cause, urgency and notes. **Photos** opens the photo that sees the
feature best, centred on it: the photos are those whose cameras see the feature's centre on the surface.

The table lists the campaign's features (term, kind, extent, intensity, status, photos, notes), with sorting and a
filter by family. **Edit...** changes the term and the attributes; to change a geometry, draw it again. *Seleziona*
and Canc in the program's view delete.

A feature is linked to the US or USM drawn on the face that cover at least **a tenth of its area**.

### 3. Proposals (the detectors)

**Proposals...** runs a detector on the whole face, on the area chosen in the table or on a window drawn on the view,
in the background. Beyond 30 million pixels cracks and losses run by tiles (overlapping by 20 cm, then merged); the
colour classes ask for a smaller window. Nothing enters the campaign without **Accept**: the proposals are drawn
dashed, and an accepted feature keeps `proposed:<detector>` as its source, so the tables say what was automatic. Each
detector writes its parameters in the feature's notes.

| Detector | What it does | Parameters (default) |
|---|---|---|
| **Cracks** | Valleys of the relief at several scales (Hessian, Frangi-like) joined with the dark thin lines of the orthophoto (black-hat on L), thinned and followed into polylines. The opening is the median along the crack of the dark width and, where the relief has values, of the width at half depth. A line lying for more than 60 % of its length on a joint between two stones drawn on the face (within 1 cm) is dropped. A line wider than the largest opening is a mortar joint, not a crack: dropped and counted. With no stones drawn the pane says so before running and offers to draw them or to change the largest opening (measure a joint of this wall). The limit applies with the stones drawn too: a real structural crack wider than it is dropped and counted unless you raise the limit. | scales 2, 5, 10, 20 mm; least length 10 cm; largest opening 12 mm; least valley 0.5 mm; least contrast 12 L |
| **Losses of material** | Depressions of the relief against the wall around them, as polygons with their median depth. On a window whose shorter side is at least 2 m the reference is a second-order surface (batter, broad bulge) and then a local surface (scale 1 m), made again without the depressions found, so that a hole does not pull its own reference; on a smaller window the window's robust plane. *Missing part* above 0.05 m², *alveolization* for parts under 0.02 m² when the window holds at least five; the others to be named. A depression over 0.25 m² or over 20 % of the window stays to be named with the note "da verificare: può essere la forma del muro" (to check: it may be the shape of the wall). | least depth 2 cm; least area 0.0025 m² in the pane, 0.01 m² on the command line; reference scale 1 m |
| **Colour classes** | k-means on CIELAB of the window; each class cleaned (opening of 3 px) and vectorised. Classes that look green, dark or bright come with a suggested term (biological colonization; efflorescence or deposit...): you name the classes, the dialog only proposes. | 5 classes; least area 0.005 m² |
| **SAM areas** | The segments of SAM 2.1 in the window, as areas *to be named* (`PPG.UNNAMED`). Name one, then **Like this** gives its term to the segments whose colour and texture resemble it (Lab mean and spread, gradient energy, a small texture histogram) within the distance shown. | thoroughness "normale"; on faces over 4 m² the image is read at 5 mm/px at most |

Only cracks and losses of material claim a term by themselves. An area still to be named cannot be accepted, and it is
left out of every output (the messages say how many).

### 4. Interventions

The same tools with the interventions' vocabulary (families of EN 15898). A treatment is an area, or a line for a
crack sealed, with technique, materials, date and *done* / *to do*. **To treat** proposes a treatment on the
geometry of the decay area chosen, with a usual technique for that term as a suggestion. On the plate treatments have
their own hatches; those done have a solid outline, those to do a dashed one.

### 5. Assessment (EN 16096)

**Classes CC and UC...** for the face of the campaign, **Recommendation RC...** for the whole object on that date (one
record per chunk and date, shown in every campaign of that date). Under each class the pane shows a *suggestion of
the plugin, thresholds can be changed*, with its rule:

| Suggested CC | Rule (plugin's defaults) |
|---|---|
| CC 0 | nothing mapped on the face |
| CC 1 | decay on up to 10 % of the face, no crack wider than 1 mm, no loss deeper than 2 cm |
| CC 2 | decay on 10-30 % of the face, or a crack of 1-5 mm, or a loss of 2-5 cm |
| CC 3 | decay on more than 30 %, or a crack wider than 5 mm, or a loss deeper than 5 cm, or a term of the detachment family on more than 5 % of the face |

The share is the union of the deterioration areas (not the general terms, nor a patina without visible deterioration)
over the face's surface. **UC** is suggested as the highest urgency you gave to a feature. **RC is never
suggested.** The thresholds are the plugin's, not the standard's.

### 6. Outputs

Files of a campaign are in `<chunk data>/conservation/<elevation id>/<campaign id>/` unless you choose another
folder; `<stem>` is the elevation's and the campaign's names.

| Button | Files |
|---|---|
| **Plate...** | `<stem>_tavola.pdf` and `.svg` (A3, scale chosen among 1:10 ... 1:1000) of the features' area with half a metre around, of an area drawn on the view or of the whole face: orthophoto read at the resolution the scale needs, features with colours and hatches, cracks with their opening (beyond 30 cracks, the 30 widest; the others with their number of the *Cracks* table), the legend of the terms present with the ICOMOS-ISCS and UNI names, the title block (date, surveyor, method, CC, UC, RC, sources), scale bar, the direction the face looks to when the frame allows |
| **Tables...** | `<stem>_tabelle.xlsx` (sheets *Elementi*, *Aree per termine*, *Aree per famiglia*, *Lesioni*, *Interventi*, *Rapporto EN 16096*), `<stem>_tabelle_elementi.csv`, `<stem>_tabelle_aree.csv` |
| **Drawings...** | `<stem>.dxf` (a layer per family, `LESIONI`, `INTERVENTI`, `NOTE`, `BORDO`; hatches; a block per term with its code and names), `<stem>_conservazione.gpkg` (unfolded, (u, v) metres, with the `geom_world` column), `<stem>_conservazione_mondo.gpkg` (3D in the survey's CRS, on the plane of the elevation), the QGIS styles `degrado.qml`, `lesioni.qml`, `interventi.qml`, `note.qml` |
| **Open in QGIS** | the elevation's QGIS project of the program with the plugin's tables styled with the legend |
| **Save in PyArchInit** | see below |
| **IFC 4.3...** | see below |
| **Open the folder** | the campaign's folder |

The English interface writes the same files with English names (`_plate`, `_tables`...).

### PyArchInit

Nothing is written to PyArchInit until you press **Save in PyArchInit**:

- the plate (SVG, PNG as its preview, PDF) and the DXF are registered as **media** of a document of type
  *Prospetto* named after the elevation and the campaign (the program's own media of the elevation are left alone),
  tag `conservazione`, linked to the US forms the features are linked to;
- on those US forms, `stato_di_conservazione` takes the word of the campaign's CC: *buono*, *discreto*,
  *mediocre*, *cattivo* for CC 0-3, or the words of the database's thesaurus for that field, or those you type in the
  dialog (20 characters at most); a line `[conservazione <date>] <terms with their extents>` is written in
  `osservazioni` and replaced, not repeated, when the campaign is saved again;
- linked units without a form in the database are listed, not created.

### IFC 4.3

**IFC 4.3...** writes one file (`IFC4X3_ADD2`) with the chosen campaigns:

- **hosts**: the *Masonry* plugin's wall when the face belongs to one and that plugin is installed (same geometry and
  same GlobalId as the masonry file); otherwise a stand-in `IfcWall` named "<elevation> (parete di appoggio)": a
  slab behind a plane, or a faceted band of the cylinder, of the thickness you give (0.5 m by default). Sections are
  refused: a section is a cut, not a face;
- **features**: each one an `IfcSurfaceFeature` (*DEFECT* for decay, cracks and notes; *TREATMENT* for
  interventions; `ObjectType` = the term's code) attached to its host with `IfcRelAdheresToElement`. Its shape is an
  open shell of the polygon lifted on the surface through the relief (a crack: a band as wide as its opening, 5 mm at
  least; a note: a 5 cm disc), placed relative to the host;
- **properties**: `Qto_SurfaceFeatureBaseQuantities` (Area or Length), `Pset_Condition` (date, "CC n", "EN 16096",
  term and notes) on every feature and on the host, `Pset_PyArchInit_Conservation` (codes, names in both languages,
  family, kind, intensity, extent, opening, depth, cause, urgency, campaign, surveyor, source, photos, technique,
  materials, done, US);
- **GlobalIds** are stable: the same campaign exported twice gives the same identifiers, and the *Masonry* plugin
  (0.1.2) names the same wall with the same GlobalId;
- the georeferencing follows the program's IFC (`IfcMapConversion` on the floor of the minimum) when the chunk has a
  CRS. No orthophoto or texture goes into the file.

Viewers support surface features poorly: most show the shells as ordinary elements, some not at all.

## Comparing two campaigns

**Compare...** compares campaign **A** (the one chosen in the pane) with a campaign **B** of the same face:

- **in the same chunk**: another campaign of the same elevation (a second visit on the same photos, or a check);
- **in another chunk**: a later survey. Its elevation must lie on the same frame, with the same resolution and bounds:
  when the other chunk has none, the plugin builds it with the program's elevation tool on the same plane, painted
  with the same settings (blending, colour correction, angle, ghosting filter, filled holes), so the two grids
  coincide. When an elevation found there was painted otherwise, the comparison's summary says which settings
  differ: they change the colour too. A development measured on the wall (*scala sul muro*) cannot be rebuilt the
  same way: make the second one on the cylinder, or compare campaigns of the same elevation.

**The two chunks must already be in the same system.** The program does that with *Workflow > Align chunks*
(markers, cameras or points), not the plugin. Before comparing, the plugin checks it:

- the **markers** of the first chunk that lie on the face, matched by label with the second chunk's markers wherever
  they are (a marker moved off the face is a refusal, not a marker left out): aligned when the farthest pair is
  within **1 cm**;
- without markers in common, the shift between the two **orthophotos** by phase correlation of their lightness on the
  cells both painted: aligned under **2 px** of the elevation's grid.

Otherwise the comparison is refused with the shift measured (in px and cm, along u and v) and the pointer to *Align
chunks*, and nothing is written.

What it gives, in `<chunk data>/conservation/<elevation id>/compare_<campaign A>_<campaign B>/`:

- per term (on the ICOMOS-ISCS code, so a UNI and an ISCS drawing of one phenomenon meet), the area in A, in B,
  **gained** and **lost**, from the differences of the polygons;
- the **cracks** of B that are new, longer (by more than 2 cm and 5 %) or wider (by more than 0.5 mm) than those of
  A within 2 cm, and those of A not seen again;
- the **treated areas** of A that show decay in B, with the area and the terms;
- `change.tif`: the colour change of the orthophoto in CIELAB (band 1 the signed ΔE: negative where B is darker;
  bands 2-4 ΔL, Δa, Δb), kept within 0.5 m of the features of both campaigns, shown over the elevation with a
  diverging palette;
- `relief_change.tif`, when the two campaigns are two surveys with a model each: the surface of B on A's grid minus
  A's relief, in cm, positive towards the viewer, shown as a second overlay;
- `confronto.xlsx` (sheets *Confronto per termine*, *Lesioni*, *Interventi*), `confronto.pdf` (the two orthophotos
  and the change, with a summary) and `confronto.json`.

A comparison is files, never records: run it again whenever the campaigns change.

## Command line

```
ppg plugin conservation list PROJECT
ppg plugin conservation campaign PROJECT [--elevation NAME] --new [--date 2026-05-14] [--name ...] [--surveyor ...]
ppg plugin conservation map PROJECT --term ISCS.CRUST.BLACK_CRUST --wkt "POLYGON ((...))" [--intensity high]
ppg plugin conservation map PROJECT --term ISCS.CRACK --gpkg cracks.gpkg --table lines [--width-mm 3]
ppg plugin conservation detect cracks|loss|colours|sam PROJECT [--bounds u0,v0,u1,v1] [--accept] [--name 2=ISCS.ALGA]
ppg plugin conservation plate|export PROJECT [--campaign NAME] [--scale 20] [--folder FOLDER]
ppg plugin conservation pyarchinit PROJECT [--sito ...] [--words buono,discreto,mediocre,cattivo]
ppg plugin conservation ifc PROJECT --out face.ifc [--campaign A --campaign B] [--thickness 0.5]
ppg plugin conservation compare PROJECT --campaign A --with B [--chunk-b "Survey 2027"] [--folder FOLDER]
ppg plugin conservation legend PROJECT [--set ISCS.ALGA=#2ca02c:xx] [--qml FOLDER]
```

Common options: `--chunk LABEL`, `--elevation ID|NAME`, `--campaign ID|NAME`. Geometry in (u, v) metres of the
elevation, from WKT (text or a file) or a GeoPackage table.

## Limits

- The **detectors** propose; they do not recognise decay. Cracks along mortar joints are dropped only where the
  stones are drawn; colour classes and SAM segments are not terms until you name them.
- The **opening** of a crack is measured on the orthophoto and the relief at their resolution: a hair crack thinner
  than a pixel has no reliable opening.
- **Depth** of a loss is measured against a surface of the wall around it (scale 1 m): a loss larger than that
  scale cannot be told from the shape of the wall and stays "to check".
- The **world GeoPackage** places the features on the elevation's plane (as the program's drawings); the **IFC**
  lifts them on the surface through the relief, on the plane where the relief has no value (said in the log).
- The **EN 16096 classes** are yours; the suggestion's thresholds are the plugin's defaults, not the standard's.
- **Comparison**: the plugin checks the alignment, it does not align. Two campaigns drawn by two people differ also by
  how they drew: read gained and lost areas with the plates side by side.
- Not done in 0.1.0: structural analysis and crack monitoring, material identification, volumes of loss in 3D,
  moisture and thermal mapping, dating of damages, costs.
