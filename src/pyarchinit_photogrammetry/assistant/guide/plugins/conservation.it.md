# Conservazione

Il plugin *Conservazione* mappa **lo stato di conservazione di un paramento** sui prospetti del programma, come fa un
restauratore su un fotopiano:

- il **degrado** come aree, ciascuna con il suo termine di un glossario condiviso;
- le **lesioni** come linee, con la loro apertura;
- gli **interventi**: le parti da trattare e quelle già trattate;
- le **note** in un punto.

La mappatura diventa una tavola in scala con la legenda, tabelle di aree e lunghezze, un DXF, GeoPackage per il GIS,
un progetto QGIS, record in PyArchInit ed elementi di superficie in un modello IFC 4.3. Una seconda campagna sullo
stesso paramento, mesi o anni dopo, si confronta con la prima.

Il plugin non giudica al posto tuo. I termini vengono dai glossari delle norme e li scegli tu; le classi di condizione
e di urgenza della EN 16096 sono tue, il plugin mostra solo un *suggerimento* con la regola che lo dà; quello che
trovano i rilevatori automatici è una *proposta* finché non la accetti.

Questa pagina descrive la versione 0.1.0 del plugin, che **richiede pyarchinit-photogrammetry 1.7.1** (i disegni
legati ai prospetti). Il README e il codice sono nel [repository dei
plugin](https://github.com/enzococca/pyarchinit-photogrammetry-plugins/tree/main/plugins/conservation).

## In breve

1. Fai un **prospetto** del paramento con il programma (pulsante *Prospetto* o `ppg elevation`): su un piano per un
   muro, su un cilindro per una torre circolare o un'abside.
2. Nel pannello *Conservazione* (a destra) scegli il prospetto e premi **Nuova campagna...**: data, rilevatore,
   metodo, condizioni meteo, note.
3. **Mappatura**: scegli un termine nell'elenco (per famiglia, con la ricerca in italiano, inglese o francese), poi
   **Area**, **Linea** o **Punto** e disegna sul prospetto. Una scheda chiede intensità, apertura, causa, urgenza e
   note.
4. **Proposte...**: lesioni, mancanze di materiale, classi di colore e segmenti di SAM proposti dall'immagine e dal
   rilievo. **Accetta**, **Accetta tutte** (quelle con un termine: dice quante restano da nominare) o **Scarta**.
5. **Interventi**: **Intervento (area)** / **Intervento (linea)**, oppure **Da restaurare** su un'area di degrado
   scelta nella tabella.
6. **Classi CC e UC...** (EN 16096) per il paramento, **Raccomandazione RC...** per l'oggetto in quella data.
7. **Tavola...**, **Tabelle...**, **Disegni...**, **Apri in QGIS**, **Salva in PyArchInit**, **IFC 4.3...**.
8. **Confronta...** due campagne dello stesso paramento.

## Cosa serve a ogni passo

| Passo | Serve |
|---|---|
| Campagna e mappatura | un prospetto del programma (piano, cilindro o sezione) con la sua ortofoto |
| Proposte dal rilievo (lesioni, mancanze) | il rilievo del prospetto (`relief.tif`, scritto con ogni prospetto): lavorano dove ha valori e dicono quanta parte della finestra hanno coperto |
| Proposte dall'immagine (colori, SAM) | solo l'ortofoto; SAM 2.1 installato dal programma (*Strumenti > SAM*) |
| Foto di un elemento | un chunk con le camere allineate |
| US legate agli elementi | US o USM disegnate sullo stesso prospetto con *Disegna US* del programma |
| QGIS | QGIS installato |
| PyArchInit | il programma collegato a PyArchInit (menu *PyArchInit*) e il sito del progetto scelto |
| IFC 4.3 | un prospetto su piano o su cilindro (le sezioni sono rifiutate); ifcopenshell, presente nell'app |
| Confronto | due campagne dello stesso paramento; per un rilievo successivo, un secondo chunk **già allineato** al primo (*Allinea chunk*) |

## Le norme e cosa dà ciascuna

- **ICOMOS-ISCS 2008**, *Illustrated glossary on stone deterioration patterns*: i sei gruppi (termini generali;
  fessure e deformazioni; distacchi; forme dovute a perdita di materiale; alterazioni cromatiche e depositi;
  colonizzazione biologica), 39 termini principali e i loro sottotipi. I suoi termini sono i **codici** del plugin
  (`ISCS.CRACK`, `ISCS.CRACK.HAIR_CRACK`, `ISCS.ALVEOLIZATION`, `ISCS.CRUST.BLACK_CRUST`...). [Il glossario sul sito
  di ICOMOS](https://www.icomos.org/public/publications/monuments_and_sites/15/pdf/Monuments_and_Sites_15_ISCS_Glossary_Stone.pdf).
- **UNI 11182:2006**, *Materiali lapidei naturali ed artificiali - Descrizione della forma di alterazione*: i 27
  termini italiani (alterazione cromatica, alveolizzazione, crosta, distacco, efflorescenza, erosione, fessurazione,
  lacuna, mancanza, patina biologica...). Ognuno è collegato al termine o ai termini ICOMOS-ISCS a cui corrisponde.
  Una tabella ufficiale non esiste: **la corrispondenza è del plugin**, tenuta come dati. Un elemento conserva
  entrambi i codici; l'interfaccia italiana mostra prima il termine UNI, quella inglese il termine ICOMOS-ISCS; la
  tavola li stampa entrambi.
- **Termini locali** (`LOCAL.*`): sintomi murari e strutturali che nessuno dei due glossari ha (fuori piombo,
  spanciamento, perdita di malta, conci spostati, giunto aperto). In ogni esportazione sono segnalati come non
  normativi. Un progetto può aggiungere i suoi.
- **EN 15898:2019**, *Main general terms and definitions*: le parole dei record (condizione, danno, degrado,
  conservazione, restauro, intervento, trattamento...) e le famiglie degli interventi (preventivi; correttivi: pulitura,
  consolidamento, desalinizzazione, biocida, stuccatura, iniezione, ristilatura dei giunti, protezione, reintegrazione,
  sostituzione, rimozione di materiali precedenti; restauro).
- **EN 16096:2012**, *Condition survey and report of built cultural heritage*: per componente (qui il paramento) una
  **classe di condizione CC 0-3** e una **classe di urgenza UC 0-3**; per l'oggetto intero una **classe di
  raccomandazione RC 0-3**, una per data di rilievo. Le diciture delle classi sono quelle citate dalla norma nella
  letteratura (il testo non è liberamente disponibile; quelle italiane sono una traduzione del plugin): controllale
  sulla tua copia della norma.
- **IFC 4.3 (ISO 16739-1:2024)**: `IfcSurfaceFeature` (DEFECT, TREATMENT) attaccato al muro con
  `IfcRelAdheresToElement`.

Le classi della EN 16096 come le mostra il plugin. **Le diciture inglesi vengono da una fonte secondaria** (una tesi
che cita la norma, il cui testo non è liberamente disponibile); quelle italiane sono una traduzione del plugin:
controllale sulla tua copia della norma prima di stamparle in un rapporto.

| Classe | Condizione (CC) | Urgenza (UC) | Raccomandazione (RC) |
|---|---|---|---|
| 0 | Nessun sintomo | Lungo termine | Nessuna misura |
| 1 | Sintomi lievi | Medio termine | Manutenzione / conservazione preventiva |
| 2 | Sintomi moderatamente forti | Breve termine | Riparazione moderata e/o ulteriori indagini |
| 3 | Sintomi gravi | Urgente e immediato | Interventi importanti basati sulla diagnosi |

I nomi dei termini sono quelli dei glossari. Le definizioni di una riga sono parafrasi del plugin, con la pagina o la
sezione della fonte: per il testo vero leggi i glossari.

### La legenda

**Nessuna norma prescrive colori o retini** (i retini della vecchia NorMaL 1/88 non sono arrivati in una forma
usabile): la legenda qui sotto è quella predefinita del plugin. Colore e retino di un termine si cambiano per il
progetto (nella 0.1.0 dalla riga di comando, `legend --set`), e la legenda è scritta anche come stili QGIS. La
tabella è generata dal vocabolario del plugin (`scripts/legend_table.py`).

<!-- legend:start -->
**Termini dei glossari (ICOMOS-ISCS e UNI 11182)**

| Codice | Nome ICOMOS-ISCS | Nome UNI 11182 | Colore | Retino (matplotlib / DXF) |
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

**Termini locali (murature, non normativi)**

| Codice | Nome inglese | Nome italiano | Colore | Retino (matplotlib / DXF) |
|---|---|---|---|---|
| `LOCAL.OUT_OF_PLUMB` | Out of plumb | fuori piombo | <span style="color:#8c564b">&#9632;</span> `#8c564b` | `//` / `ANSI31` |
| `LOCAL.BULGING` | Bulging | spanciamento | <span style="color:#a0522d">&#9632;</span> `#a0522d` | `\\` / `ANSI32` |
| `LOCAL.LOSS_OF_MORTAR` | Loss of mortar | perdita di malta | <span style="color:#6b3e26">&#9632;</span> `#6b3e26` | `xx` / `ANSI37` |
| `LOCAL.DISPLACED_STONES` | Displaced stones | pietre smosse | <span style="color:#c49c94">&#9632;</span> `#c49c94` | `..` / `DOTS` |
| `LOCAL.OPEN_JOINT` | Open joint | giunto aperto | <span style="color:#7f4f24">&#9632;</span> `#7f4f24` | `++` / `CROSS` |

**Interventi (famiglie di EN 15898)**

| Codice | Nome inglese | Nome italiano | Colore | Retino (matplotlib / DXF) |
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

## Il pannello, passo per passo

### 1. Prospetto e campagna

Il pannello elenca i prospetti del chunk con le loro campagne. Una campagna è un rilievo di un paramento in una data:
viene proposta la data del chunk. **Mostra** mostra il prospetto con gli elementi della campagna nei layer *Degrado*,
*Lesioni*, *Interventi* e *Note* (e *Proposte*, tratteggiate). Quando il paramento appartiene a un muro del plugin
*Murature*, prende il nome del muro.

### 2. Mappatura

Scegli il termine nell'elenco: per famiglia, con la ricerca nelle tre lingue, i preferiti e gli ultimi dieci usati.
Poi:

- **Area**: un poligono (degrado);
- **Linea**: una lesione o un giunto aperto;
- **Punto**: una nota.

L'elemento prende il termine, la sua estensione (m² o m) e la campagna. La scheda chiede l'intensità (bassa, media,
alta), l'apertura di una lesione (mm), la profondità di una mancanza (mm), causa, urgenza e note. **Foto** apre la foto
che vede meglio l'elemento, centrata su di esso: le foto sono quelle le cui camere vedono il centro dell'elemento
sulla superficie.

La tabella elenca gli elementi della campagna (termine, tipo, estensione, intensità, stato, foto, note), con
l'ordinamento e un filtro per famiglia. **Modifica...** cambia il termine e gli attributi; per cambiare una geometria,
ridisegnala. *Seleziona* e Canc nella vista del programma eliminano.

Un elemento è legato alle US o USM disegnate sul paramento che ne coprono almeno **un decimo dell'area**.

### 3. Proposte (i rilevatori)

**Proposte...** fa girare un rilevatore su tutto il paramento, sull'area scelta nella tabella o su una finestra
disegnata sulla vista, in background. Oltre 30 milioni di pixel lesioni e mancanze girano a tasselli (sovrapposti di
20 cm, poi uniti); le classi di colore chiedono una finestra più piccola. Niente entra nella campagna senza
**Accetta**: le proposte sono disegnate tratteggiate, e un elemento accettato conserva `proposed:<rilevatore>` come
fonte, così le tabelle dicono cosa era automatico. Ogni rilevatore scrive i suoi parametri nelle note dell'elemento.

| Rilevatore | Cosa fa | Parametri (predefiniti) |
|---|---|---|
| **Lesioni** | Le valli del rilievo a più scale (hessiana, alla Frangi) unite alle linee scure e sottili dell'ortofoto (black-hat su L), assottigliate e seguite come polilinee. L'apertura è la mediana lungo la lesione della larghezza scura e, dove il rilievo ha valori, della larghezza a metà profondità. Una linea che per più del 60 % della lunghezza sta su un giunto fra due pietre disegnate sul paramento (entro 1 cm) viene scartata. Una linea più larga dell'apertura massima è un giunto di malta, non una lesione: tolta e contata. Senza pietre disegnate il pannello lo dice prima di partire e propone di disegnarle o di cambiare l'apertura massima (un giunto di questo muro, misurato). Il limite vale anche con le pietre disegnate: una vera lesione strutturale più larga viene tolta e contata, a meno di alzare il limite. | scale 2, 5, 10, 20 mm; lunghezza minima 10 cm; apertura massima 12 mm; valle minima 0,5 mm; contrasto minimo 12 L |
| **Mancanze di materiale** | Le depressioni del rilievo rispetto al muro intorno, come poligoni con la loro profondità mediana. Su una finestra con il lato minore di almeno 2 m il riferimento è una superficie di secondo grado (scarpa, spanciamento ampio) e poi una superficie locale (scala 1 m), rifatta senza le depressioni trovate, così un buco non tira il proprio riferimento; su una finestra più piccola il piano robusto della finestra. *Mancanza* oltre 0,05 m², *alveolizzazione* per le parti sotto 0,02 m² quando la finestra ne ha almeno cinque; le altre da nominare. Una depressione oltre 0,25 m² o oltre il 20 % della finestra resta da nominare con la nota «da verificare: può essere la forma del muro». | profondità minima 2 cm; area minima 0,0025 m² nel pannello, 0,01 m² dalla riga di comando; scala del riferimento 1 m |
| **Classi di colore** | k-means su CIELAB della finestra; ogni classe ripulita (apertura di 3 px) e vettorializzata. Le classi che sembrano verdi, scure o chiare arrivano con un termine suggerito (colonizzazione biologica; efflorescenza o deposito...): le classi le nomini tu, la finestra propone soltanto. | 5 classi; area minima 0,005 m² |
| **Aree di SAM** | I segmenti di SAM 2.1 nella finestra, come aree *da nominare* (`PPG.UNNAMED`). Nominane una, poi **Come questo** dà il suo termine ai segmenti con colore e tessitura simili (media e dispersione in Lab, energia del gradiente, un piccolo istogramma di tessitura) entro la distanza indicata. | accuratezza "normale"; sui paramenti oltre 4 m² l'immagine è letta al massimo a 5 mm/px |

Solo lesioni e mancanze di materiale si danno un termine da sole. Un'area ancora da nominare non si può accettare e
resta fuori da ogni esito (i messaggi dicono quante sono).

### 4. Interventi

Gli stessi strumenti con il vocabolario degli interventi (famiglie della EN 15898). Un intervento è un'area, o una
linea per una lesione sigillata, con tecnica, materiali, data ed *eseguito* / *da eseguire*. **Da restaurare** propone
un intervento sulla geometria dell'area di degrado scelta, con una tecnica abituale per quel termine come
suggerimento. Nella tavola gli interventi hanno retini propri; quelli eseguiti un contorno continuo, quelli da eseguire
tratteggiato.

### 5. Valutazione (EN 16096)

**Classi CC e UC...** per il paramento della campagna, **Raccomandazione RC...** per l'oggetto intero in quella data
(un record per chunk e data, mostrato in ogni campagna di quella data). Sotto ogni classe il pannello mostra un
*suggerimento del plugin, soglie modificabili*, con la sua regola:

| CC suggerita | Regola (valori predefiniti del plugin) |
|---|---|
| CC 0 | niente di mappato sul paramento |
| CC 1 | degrado fino al 10 % del paramento, nessuna lesione più larga di 1 mm, nessuna mancanza più profonda di 2 cm |
| CC 2 | degrado sul 10-30 % del paramento, o una lesione di 1-5 mm, o una mancanza di 2-5 cm |
| CC 3 | degrado su più del 30 %, o una lesione più larga di 5 mm, o una mancanza più profonda di 5 cm, o un termine della famiglia dei distacchi su più del 5 % del paramento |

La quota è l'unione delle aree di degrado (non i termini generali né una patina senza degrado visibile) sulla
superficie del paramento. **UC** è suggerita come la più alta urgenza data a un elemento. **RC non è mai
suggerita.** Le soglie sono del plugin, non della norma.

### 6. Esiti

I file di una campagna stanno in `<dati del chunk>/conservation/<id del prospetto>/<id della campagna>/` se non scegli
un'altra cartella; `<nome>` sono i nomi del prospetto e della campagna.

| Pulsante | File |
|---|---|
| **Tavola...** | `<nome>_tavola.pdf` e `.svg` (A3, scala scelta fra 1:10 ... 1:1000) dell'area degli elementi con mezzo metro intorno, di un'area disegnata sulla vista o di tutta la faccia: ortofoto letta alla risoluzione che la scala chiede, elementi con colori e retini, lesioni con l'apertura (oltre 30 lesioni, le 30 più larghe; le altre con il numero della tabella *Lesioni*), la legenda dei termini presenti con i nomi ICOMOS-ISCS e UNI, il cartiglio (data, rilevatore, metodo, CC, UC, RC, fonti), la barra di scala, la direzione verso cui guarda il paramento quando il piano lo permette |
| **Tabelle...** | `<nome>_tabelle.xlsx` (fogli *Elementi*, *Aree per termine*, *Aree per famiglia*, *Lesioni*, *Interventi*, *Rapporto EN 16096*), `<nome>_tabelle_elementi.csv`, `<nome>_tabelle_aree.csv` |
| **Disegni...** | `<nome>.dxf` (un layer per famiglia, `LESIONI`, `INTERVENTI`, `NOTE`, `BORDO`; retini; un blocco per termine con codice e nomi), `<nome>_conservazione.gpkg` (spiegato, metri (u, v), con la colonna `geom_world`), `<nome>_conservazione_mondo.gpkg` (3D nel sistema del rilievo, sul piano del prospetto), gli stili QGIS `degrado.qml`, `lesioni.qml`, `interventi.qml`, `note.qml` |
| **Apri in QGIS** | il progetto QGIS del prospetto del programma con le tabelle del plugin e gli stili della legenda |
| **Salva in PyArchInit** | vedi sotto |
| **IFC 4.3...** | vedi sotto |
| **Apri la cartella** | la cartella della campagna |

L'interfaccia inglese scrive gli stessi file con i nomi inglesi (`_plate`, `_tables`...).

### PyArchInit

In PyArchInit non si scrive niente finché non premi **Salva in PyArchInit**:

- la tavola (SVG, PNG come anteprima, PDF) e il DXF sono registrati come **media** di un documento di tipo
  *Prospetto* con il nome del prospetto e della campagna (i media del programma per quel prospetto restano come
  sono), tag `conservazione`, collegati alle schede US a cui sono legati gli elementi;
- in quelle schede US `stato_di_conservazione` prende la parola della CC della campagna: *buono*, *discreto*,
  *mediocre*, *cattivo* per CC 0-3, oppure le parole del thesaurus del database per quel campo, oppure quelle che
  scrivi nella finestra (al massimo 20 caratteri); in `osservazioni` si scrive una riga
  `[conservazione <data>] <termini con le estensioni>`, sostituita e non ripetuta quando la campagna si salva di nuovo;
- le US legate che nel database non hanno una scheda sono elencate, non create.

### IFC 4.3

**IFC 4.3...** scrive un file (`IFC4X3_ADD2`) con le campagne scelte:

- **muri di appoggio**: il muro del plugin *Murature* quando il paramento ne fa parte e quel plugin è installato
  (stessa geometria e stesso GlobalId del file delle murature); altrimenti un `IfcWall` sostitutivo di nome
  "<prospetto> (parete di appoggio)": una lastra dietro un piano, o una fascia a facce del cilindro, dello spessore che
  indichi (0,5 m se non dici altro). Le sezioni sono rifiutate: una sezione è un taglio, non una faccia;
- **elementi**: ciascuno un `IfcSurfaceFeature` (*DEFECT* per degrado, lesioni e note; *TREATMENT* per gli
  interventi; `ObjectType` = il codice del termine) attaccato al suo muro con `IfcRelAdheresToElement`. La forma è un
  guscio aperto del poligono portato sulla superficie attraverso il rilievo (una lesione: una striscia larga quanto la
  sua apertura, almeno 5 mm; una nota: un disco di 5 cm), posto rispetto al muro;
- **proprietà**: `Qto_SurfaceFeatureBaseQuantities` (Area o Length), `Pset_Condition` (data, "CC n", "EN 16096",
  termine e note) su ogni elemento e sul muro, `Pset_PyArchInit_Conservation` (codici, nomi nelle due lingue,
  famiglia, tipo, intensità, estensione, apertura, profondità, causa, urgenza, campagna, rilevatore, fonte, foto,
  tecnica, materiali, eseguito, US);
- i **GlobalId** sono stabili: la stessa campagna esportata due volte dà gli stessi identificativi, e il plugin
  *Murature* (0.1.2) chiama lo stesso muro con lo stesso GlobalId;
- la georeferenziazione segue l'IFC del programma (`IfcMapConversion` sul pavimento del minimo) quando il chunk ha un
  sistema di riferimento. Nel file non va né ortofoto né texture.

I visualizzatori gestiscono male gli elementi di superficie: la maggior parte mostra i gusci come elementi qualsiasi,
qualcuno per niente.

## Confrontare due campagne

**Confronta...** confronta la campagna **A** (quella scelta nel pannello) con una campagna **B** dello stesso
paramento:

- **nello stesso chunk**: un'altra campagna dello stesso prospetto (una seconda visita sulle stesse foto, o una
  verifica);
- **in un altro chunk**: un rilievo successivo. Il suo prospetto deve stare sullo stesso piano, con la stessa
  risoluzione e gli stessi limiti: quando l'altro chunk non lo ha, il plugin lo costruisce con lo strumento dei
  prospetti del programma sullo stesso piano e con le stesse impostazioni (fusione, correzione del colore, angolo,
  filtro dei fantasmi, buchi riempiti), così le due griglie coincidono. Quando un prospetto trovato lì è stato
  dipinto in altro modo, il riepilogo del confronto dice quali impostazioni sono diverse: cambiano anche il colore.
  Uno sviluppo misurato sul muro (*scala sul muro*) non si rifà uguale: fai il secondo sul cilindro, o confronta
  campagne dello stesso prospetto.

**I due chunk devono essere già nello stesso sistema.** Lo fa il programma con *Flusso di lavoro > Allinea chunk*
(marker, camere o punti), non il plugin. Prima del confronto il plugin lo verifica:

- i **marker** del primo chunk che stanno sul paramento, abbinati per nome ai marker del secondo ovunque siano (un
  marker finito fuori dal paramento è un rifiuto, non un marker lasciato fuori): allineati quando la coppia più
  lontana è entro **1 cm**;
- senza marker in comune, lo spostamento fra le due **ortofoto** per correlazione di fase della loro luminosità sulle
  celle dipinte in entrambe: allineati sotto **2 px** della griglia del prospetto.

Altrimenti il confronto è rifiutato con lo spostamento misurato (in px e cm, lungo u e v) e il rimando ad *Allinea
chunk*, e non si scrive niente.

Cosa dà, in `<dati del chunk>/conservation/<id del prospetto>/compare_<campagna A>_<campagna B>/`:

- per termine (sul codice ICOMOS-ISCS, così un disegno UNI e uno ISCS dello stesso fenomeno si incontrano), l'area
  in A, in B, **guadagnata** e **persa**, dalle differenze dei poligoni;
- le **lesioni** di B nuove, più lunghe (di oltre 2 cm e 5 %) o più larghe (di oltre 0,5 mm) di quelle di A entro
  2 cm, e quelle di A non più viste;
- le **aree trattate** di A che in B mostrano degrado, con l'area e i termini;
- `change.tif`: il cambiamento di colore dell'ortofoto in CIELAB (banda 1 il ΔE con segno: negativo dove B è più
  scura; bande 2-4 ΔL, Δa, Δb), tenuto entro 0,5 m dagli elementi delle due campagne, mostrato sul prospetto con una
  palette divergente;
- `relief_change.tif`, quando le due campagne sono due rilievi ciascuno con il suo modello: la superficie di B sulla
  griglia di A meno il rilievo di A, in cm, positivo verso chi guarda, mostrato come secondo strato;
- `confronto.xlsx` (fogli *Confronto per termine*, *Lesioni*, *Interventi*), `confronto.pdf` (le due ortofoto e il
  cambiamento, con un riepilogo) e `confronto.json`.

Un confronto è fatto di file, mai di record: rifallo quando le campagne cambiano.

## Riga di comando

```
ppg plugin conservation list PROGETTO
ppg plugin conservation campaign PROGETTO [--elevation NOME] --new [--date 2026-05-14] [--name ...] [--surveyor ...]
ppg plugin conservation map PROGETTO --term ISCS.CRUST.BLACK_CRUST --wkt "POLYGON ((...))" [--intensity high]
ppg plugin conservation map PROGETTO --term ISCS.CRACK --gpkg lesioni.gpkg --table linee [--width-mm 3]
ppg plugin conservation detect cracks|loss|colours|sam PROGETTO [--bounds u0,v0,u1,v1] [--accept] [--name 2=ISCS.ALGA]
ppg plugin conservation plate|export PROGETTO [--campaign NOME] [--scale 20] [--folder CARTELLA]
ppg plugin conservation pyarchinit PROGETTO [--sito ...] [--words buono,discreto,mediocre,cattivo]
ppg plugin conservation ifc PROGETTO --out paramento.ifc [--campaign A --campaign B] [--thickness 0.5]
ppg plugin conservation compare PROGETTO --campaign A --with B [--chunk-b "Rilievo 2027"] [--folder CARTELLA]
ppg plugin conservation legend PROGETTO [--set ISCS.ALGA=#2ca02c:xx] [--qml CARTELLA]
```

Opzioni comuni: `--chunk ETICHETTA`, `--elevation ID|NOME`, `--campaign ID|NOME`. Geometrie in metri (u, v) del
prospetto, da WKT (testo o file) o da una tabella di un GeoPackage.

## Limiti

- I **rilevatori** propongono, non riconoscono il degrado. Le lesioni lungo i giunti di malta sono scartate solo dove
  le pietre sono disegnate; classi di colore e segmenti di SAM non sono termini finché non li nomini.
- L'**apertura** di una lesione è misurata sull'ortofoto e sul rilievo alla loro risoluzione: un capello più sottile
  di un pixel non ha un'apertura affidabile.
- La **profondità** di una mancanza è misurata rispetto a una superficie del muro intorno (scala 1 m): una mancanza
  più grande di quella scala non si distingue dalla forma del muro e resta «da verificare».
- Il **GeoPackage nel mondo** mette gli elementi sul piano del prospetto (come i disegni del programma); l'**IFC** li
  porta sulla superficie attraverso il rilievo, sul piano dove il rilievo non ha valore (lo dice il log).
- Le **classi EN 16096** sono tue; le soglie del suggerimento sono valori predefiniti del plugin, non della norma.
- **Confronto**: il plugin verifica l'allineamento, non allinea. Due campagne disegnate da due persone differiscono
  anche per come sono state disegnate: leggi aree guadagnate e perse con le tavole affiancate.
- Non fatto nella 0.1.0: analisi strutturale e monitoraggio delle lesioni, riconoscimento dei materiali, volumi di
  perdita in 3D, mappe di umidità e termiche, datazione dei danni, costi.
