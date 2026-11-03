# Strumenti per l'archeologia

Gli strumenti che trasformano il rilievo in documentazione di scavo: il collegamento con PyArchInit e QGIS, i solidi
delle unità, la proposta stratigrafica per l'Extended Matrix, le fasi dello scavo, la ceramica, i colori, le
visualizzazioni del rilievo, il calcolatore raster, la classificazione della nuvola e il BIM.

## PyArchInit

PyArchInit resta la fonte delle schede e dei rapporti: il programma scrive nel suo database le unità, le quote e i
prodotti, e rilegge ciò che c'è già. Altro in [PyArchInit](../pyarchinit.md).

### Collegarsi

All'avvio l'interfaccia si collega da sola al database configurato in PyArchInit 5
(`~/pyarchinit_5/pyarchinit_DB_folder/config.cfg`; opzione *Connetti all'avvio al database di PyArchInit*).
*PyArchInit > Connetti...* apre la finestra di connessione:

* la **configurazione di PyArchInit**;
* un **file SQLite / SpatiaLite**;
* un **server PostgreSQL / PostGIS**: host, porta, database, utente, password, *Prova connessione*.

*Usa il database demo* crea `demo.sqlite` con lo schema completo di PyArchInit 5 (tre siti) dentro
`~/pyarchinit_5/pyarchinit_DB_folder` quando quell'installazione c'è, così QGIS può aprire lo stesso file;
`config.cfg` non viene mai toccato. Il [pannello PyArchInit](panes.md#pyarchinit) elenca siti, US e documentazione.

??? example "Riga di comando e API"
    `PyArchInit.connect(url=..., config_path=...)`, `.sites()`, `.stratigraphic_units(sito)`, `.documentation(sito)`

### Il sito del progetto

Ogni unità e ogni quota appartengono a uno scavo: il sito si sceglie una volta - *PyArchInit > Sito del progetto*, la
casella in cima al pannello PyArchInit, o la domanda fatta la prima volta che si disegna o si pubblica qualcosa - e si
salva nel progetto. Un database PyArchInit può avere più siti: il pannello allora elenca solo le US e i documenti del
sito del progetto, e la scheda US propone i siti del database.

Un sito deve avere la sua scheda prima di pubblicare qualcosa: *PyArchInit > Nuovo sito...* (o *Nuovo sito* nel
pannello e nella finestra di pubblicazione) crea il record; pubblicare in un sito che non esiste lo crea da solo.

??? example "Riga di comando e API"
    `doc.meta["sito"]`, `pa.ensure_site(sito, comune=...)`, `pa.site_exists(sito)`

### Pubblicare

| Comando | Cosa scrive |
| --- | --- |
| *PyArchInit > Pubblica ortofoto / DEM / modello...* | scegli il sito, il nome del documento e le US da collegare: i raster vengono copiati accanto alla cartella dei media di PyArchInit, con le miniature e un record in `documentazione_table` |
| *Pubblica unita' e quote disegnate* (*Pubblica* nella barra Disegno) | le unità in `pyunitastratigrafiche`, le quote in `pyarchinit_quote` |
| *Salva modifiche in PyArchInit* (pannello *Layer*) | le modifiche alle unità caricate da PyArchInit, per il loro `gid` |
| *Pubblica in PyArchInit* (pannello *Sezioni*) | le tracce di sezione in `pyarchinit_sezioni` |

??? example "Riga di comando e API"
    `ppg pyarchinit publish scavo.ppg "Sito" "Nome" --dem --model --link-us 1:101`; `pa.publish_orthomosaic`,
    `publish_dem`, `publish_model`

### Schede US

![La scheda US](../img/gui/dialog-us-form.png)

Un doppio clic su una US nel pannello PyArchInit (o *Scheda US* nei pannelli *Layer* e *Proxy US*) apre la scheda US di
PyArchInit (`us_table`): descrizione, cronologia, dati di scavo, caratteri, quote e misure, e i rapporti stratigrafici
come tabella modificabile (*Copre*, *Taglia*, *Si appoggia a*...), salvati nel formato di PyArchInit perché il suo
matrix di Harris li legga.

* *Nuova US* nel pannello, o *PyArchInit > Nuova scheda US...*, crea un record (anche quello del sito se manca); un sito,
  un'area, una US e un tipo già esistenti vengono rifiutati.
* Un database con una `us_table` ridotta (il database demo della versione 0.2, o un file creato da altri strumenti)
  mostra un avviso nella scheda con *Aggiorna struttura del database* (anche nel menu PyArchInit): le colonne mancanti di
  PyArchInit 5 vengono aggiunte dopo una copia di sicurezza del file, i record restano intatti.

??? example "Riga di comando e API"
    `pa.missing_us_columns()`, `pa.upgrade_us_table()`, `pa.us_record`, `pa.create_us(sito, area, us, tipo, **fields)`,
    `pa.update_us(id_us, **fields)`, `parse_relationships`, `format_relationships`

## QGIS dal vivo

*QGIS > Apri in QGIS (collegamento dal vivo)* apre l'ortofoto, il DEM e i disegni in un progetto QGIS; *QGIS >
Collegamento dal vivo con QGIS (nei due sensi)* li tiene collegati nei due sensi:

* ciò che disegni qui arriva in QGIS entro un secondo;
* ciò che salvi in QGIS (il salvataggio delle modifiche del layer, *Save Layer Edits*, o la fine di una sessione di
  modifica) nei layer *Unita' stratigrafiche (ppg)*, *Quote (ppg)* e *Sezioni (ppg)* torna qui entro un secondo:
  contorni e attributi cambiati, elementi disegnati lì (ricevono la loro identità, `ppg_id`), elementi cancellati.

Ogni salvataggio è una voce di *Modifica > Annulla* e una riga del log. Le unità prendono le colonne di
`pyunitastratigrafiche` (`us_s`, `area_s`, `unita_tipo_s`, `scavo_s`, `disegnatore`...); un'unità di PyArchInit
cambiata lì aspetta *Salva modifiche in PyArchInit*; il proxy di un'unità con il contorno cambiato viene ricostruito;
una sezione la cui traccia si è spostata viene tagliata di nuovo.

!!! note
    Un contorno non valido viene reso valido (o saltato), un layer in un altro sistema di riferimento viene rifiutato,
    un file che QGIS tiene mentre salva viene letto un momento dopo. Curve di livello, zone, pietre, patch e i disegni
    dei plugin si calcolano qui e vanno in un solo senso. I dettagli in [QGIS dal vivo](../integrations.md#qgis-dal-vivo).

??? example "Riga di comando e API"
    `io.qgis_link.LiveLink` (`write`, `external_change`, `read_changes`), `export_layers`, `build_project`

## QField in trincea

*File > Esporta > Pacchetto QField* scrive una cartella con l'ortofoto ridotta per il tablet (Cloud Optimized GeoTIFF,
2 cm di base), il DEM come ombreggiatura e un GeoPackage con le unità e le quote del chunk, più un progetto QGIS con
percorsi relativi, etichette e l'elenco dei tipi di US (fatto con il Python di QGIS quando QGIS è installato; senza,
QField apre direttamente il GeoPackage).

1. Copiala sul tablet o su QFieldCloud.
2. Disegna e correggi in trincea.
3. Riporta la cartella: *File > Importa > Modifiche da QField* applica al chunk le unità e le quote aggiunte, cambiate
   e cancellate lì (ogni elemento tiene un'identità, `ppg_id`).
4. *Pubblica in PyArchInit* / *Salva modifiche in PyArchInit* le scrivono nel database.

## Proxy 3D delle US

![La finestra Proxy 3D delle US](../img/gui/dialog-proxies.png)

*Strumenti > Proxy 3D delle US*: ogni poligono di unità (disegnato, o da un sito PyArchInit) diventa un solido chiuso
tra il DEM del progetto (sopra) e una **base**: un piano a una quota data, il DEM rilevato dopo che l'unità è stata
asportata, uno spessore costante o il punto più basso. Il suo volume è uguale al volume di scavo/riporto sulle stesse
celle.

Il [pannello Proxy US](panes.md#proxy-us) elenca volume, area in pianta e quote; *Vedi in 3D* li mostra colorati sopra il
modello; *Esporta (Extended Matrix)* scrive un OBJ con un oggetto per unità (`US12`, `USM3`) più lo spostamento delle
coordinate; *Pubblica in PyArchInit* registra ogni proxy come media 3D legato alla sua US; *Quote nella scheda US*
scrive le quote minime e massime nella scheda.

Ogni proxy tiene la sua base con il suo valore (la quota del piano, lo spessore, il DEM dopo l'asportazione) e
l'identità della sua unità. Quando il poligono dell'unità cambia - *Vertici*, *Rimodella*, o in QGIS con il
collegamento dal vivo - il proxy viene ricostruito da solo allo stesso modo, e quando cambiano solo il numero o il tipo
prende il nuovo nome (`US7`); per un proxy fatto da una versione precedente il valore mancante viene chiesto una volta.
Vedi anche il plugin [Blender ed EMtools](../plugins/blender-em.md).

??? example "Riga di comando e API"
    `archaeo.proxies.unit_proxy`, `proxies_for_units`, `export_extended_matrix`, `save_proxy`

## Proposta di US e rapporti

*Strumenti > Proponi US e rapporti (Extended Matrix)...*, anche nel menu dello strumento SAM: le unità del chunk senza
numero - quelle che SAM ha trovato su tutta l'ortofoto, o disegnate - ricevono un numero di US, prima le unità più alte,
dopo l'ultima US del sito in PyArchInit e del progetto.

* **Tipo e definizione**: USM dove la maggior parte della nuvola densa classificata all'interno è muratura (taglio,
  pavimento, deposito e substrato danno la definizione stratigrafica; vegetazione, reperti e attrezzature di scavo
  vengono proposti senza spunta).
* **Misure**: area in pianta, quote dal DEM e colore Munsell dell'ortofoto (con la calibrazione del colore) vanno nella
  descrizione, nel colore e nei campi delle quote della scheda.
* **Rapporti**, dai contatti in pianta con il gradino del DEM attraverso di essi: l'unità più alta lungo il contatto
  (di almeno il gradino scelto, 2 cm) *Copre* l'altra; un'unità racchiusa in un'altra per tre quarti del suo contorno e
  non più alta è il riempimento di un taglio, che viene proposto con essa (il riempimento *Riempie* il taglio, il taglio
  *Taglia* l'unità attorno; un'unità che la nuvola chiama taglio la taglia direttamente); uno strato contro una muratura
  *Si appoggia a* essa, o la *Copre* quando è più alto.

La finestra elenca unità e rapporti con il motivo di ognuno; numero, tipo e definizione si possono correggere, e un
doppio clic mostra l'unità. *Applica al progetto* numera le unità e aggiunge i tagli (il contorno del loro riempimento,
*2 - Contorno US*); *Applica e scrivi in PyArchInit* crea anche le schede US (con `node_uuid` ed `entity_uuid`, le schede
esistenti restano come sono), scrive i rapporti nei due sensi e i poligoni in `pyunitastratigrafiche`: l'Extended Matrix
che PyArchInit ed EMStudio costruiscono dalle schede ha allora questi nodi e archi.

!!! info "Un'unità disegnata con il suo numero"
    Arriva nel matrix allo stesso modo, attraverso PyArchInit. Con un database PyArchInit collegato, un'unità disegnata
    qui (o in QGIS con il collegamento dal vivo) la cui scheda US non è in PyArchInit viene segnalata nel log, e la
    proposta prende anche lei, tenendo il suo numero, con il taglio che può riempire. Le unità che hanno già la loro
    scheda partecipano solo per i rapporti: una nuova unità dentro la US 2 riceve *Riempie* il suo taglio e il taglio
    *Taglia* la US 2, scritto anche nella scheda della US 2, i cui altri campi restano come sono. Poi esporta di nuovo
    l'em.json del sito da PyArchInit (*Extended Matrix > Esporta sito in em.json*, come ricorda il messaggio alla fine) ed
    EMtools ed EMStudio hanno le nuove unità, abbinate ai loro proxy dal `node_uuid`.

## Fasi dello scavo

*Strumenti > Fasi dello scavo (volumi e rapporti)...*: ogni chunk con un DEM è un rilievo dello scavo - un giorno, la
fine di una US - in un solo sistema (una georeferenziazione comune, o *Allinea chunk*), nell'ordine della sua data
(l'EXIF delle foto, o scritta nella finestra).

* Le unità disegnate su una fase le appartengono (cambiare chunk tiene i disegni di ogni chunk) e sono state messe in
  luce allora.
* La finestra dà il **volume** tolto e aggiunto da ogni fase alla successiva, anche dentro ogni unità.
* **Propone** che un'unità messa in luce prima *copra* le unità che sovrappone in pianta messe in luce dopo; i rapporti
  spuntati vanno nelle schede US di PyArchInit nei due sensi (mai due volte).
* Un doppio clic su una fase la mostra; *Salva CSV* scrive il rapporto.

*Coregistra i DEM delle fasi prima dei volumi* (spuntato di base con più di una fase) sposta prima ogni fase *sulla
prima fase* o *sulla fase precedente* sul terreno che non è cambiato, lasciando fuori le unità disegnate: rilievi di
giorni diversi non sono mai registrati perfettamente, e uno spostamento di pochi centimetri è falso scavo e falso
riporto su ogni pendio. I DEM del progetto non cambiano; la scheda *Coregistrazione* dà lo spostamento di ogni fase (dx,
dy, dz in cm, inclinazione in mm/m, NMAD sul terreno stabile prima e dopo, celle stabili) e *Ricalcola* applica una nuova
scelta. Vedi [Coregistrazione dei DEM](processing.md#coregistrazione-dei-dem).

??? example "Riga di comando e API"
    `ppg coregister scavo.ppg --reference "Giorno 1" --chunk "Giorno 2" --exclude-polygon scavo.geojson`,
    `ppg timeline scavo.ppg --coregister-to previous` (`--no-coregister`); `chunk.coregister_dem(reference)`,
    `archaeo.coreg.coregister(reference_path, dem_path, out_path)`

## Profili ceramici

*Strumenti > Profilo ceramico da mesh...*: scegli la mesh di un vaso o di un frammento (OBJ, PLY, STL), la scala, le
unità e l'asse (stimato dalle normali della superficie, oppure la Z della mesh con l'orlo in alto, come su un turntable,
o in basso, un frammento appoggiato sull'orlo).

* Il **diametro dell'orlo** viene dal cerchio del labbro, che sposta anche l'asse al suo centro (un frammento messo sul
  turntable lontano dal centro si misura giusto), con l'arco conservato (*Orlo conservato*, 34% (25-50) per un terzo
  dell'orlo). Sotto i 40 gradi di arco, o con un labbro irregolare, l'orlo viene segnato *incerto*, misurato attorno
  all'asse, e non scritto in PyArchInit.
* Il **profilo** con diametro massimo e del fondo, altezza totale o conservata e spessore della parete viene disegnato
  come SVG, DXF e PNG, in una finestra che resta aperta.
* *Cerca forme simili* ordina le forme della `pottery_table` di PyArchInit di ogni sito per i diametri e l'altezza in
  comune (l'orlo conta due volte) e, per le forme salvate da questo programma, per la forma del profilo dall'orlo in giù
  (qualunque sia la loro dimensione).
* *Salva in PyArchInit...* scrive le misure in centimetri in una scheda ceramica nuova o scelta (`diametro_rim`,
  `diametro_max`, `diametro_bottom`, `diametro_height` altezza totale, `diametro_preserved` altezza conservata, `percent`
  <5 / 5-25 / 25-50 / >50; forma, parte, forma specifica e impasto dal thesaurus di PyArchInit); i campi già compilati di
  una scheda esistente restano, a meno che non sia spuntato *Sostituisci*. Il disegno va nei media della scheda (PNG
  collegato come CERAMICA, con SVG e DXF accanto e `.profile.json`, il profilo per le ricerche successive).

??? example "Riga di comando e API"
    `ppg pottery vaso.ply --out profilo.svg --scale 1 [--axis up|down] [--units mm]`,
    `ppg pyarchinit pottery-search frammento.ply`, `ppg pyarchinit pottery-save frammento.ply Sito --us 12 --part Orlo`;
    `archaeo.pottery.measure`, `rim`, `signature`, `draw`; `pa.search_pottery`, `pa.save_pottery`

## Colore Munsell

*Misura area e superficie 3D* (un riquadro, un poligono o un'unità selezionata) dà anche il colore dell'ortofoto al suo
interno - la mediana dei suoi pixel senza il decimo più scuro e quello più chiaro (ombre, ciottoli, riflessi) - come
notazione Munsell e tessera più vicina di una tavola dei suoli (`10YR 5/4`).

Fotografa una ColorChecker Classic (24 tasselli) nello scavo e usa *Strumenti > Calibra il colore con la tabella
colori...* (un riquadro attorno a essa sull'ortofoto): la correzione 3 x 3 di quel rilievo (adattata in luce lineare
sui tasselli di riferimento, residuo riportato come CIEDE2000) viene tenuta nel chunk, e i colori di giorni e camere
diversi diventano confrontabili. Serve colour-science (in `[gui]`, `[all]` o `[munsell]`).

## Visualizzazioni del rilievo

![La finestra Visualizzazioni del rilievo](../img/gui/dialog-relief.png)

*Strumenti > Visualizzazioni del rilievo...*:

| Visualizzazione | Cosa mostra |
| --- | --- |
| **Sky-view factor** | quanto cielo vede ogni cella: scuro nei fossati e al piede dei muri |
| **Openness positiva e negativa** | creste e muri, fossati e buche |
| **Rilievo locale** | il DEM meno la sua media attorno: rosso sopra, blu sotto |
| **Rilievo multiscala** (MSRM) | le forme da poche celle al diametro di ricerca insieme |
| **Ombreggiatura da più direzioni** | un'ombreggiatura illuminata da 16 direzioni |

La finestra propone un raggio di ricerca di circa 20 celle; il raster si vede subito (grigio stirato su tutto il raster,
il terreno piano a metà per l'openness), resta nell'area di lavoro e il valore sotto il cursore è nella barra in basso.
Su un tell la griglia degli edifici, i muri e le vecchie trincee escono da un DSM dove i colori non mostrano nulla.

??? example "Riga di comando e API"
    `ppg relief`; `chunk.build_relief`; `archaeo.relief`

## Calcolatore raster

![La finestra Calcolatore raster](../img/gui/dialog-raster-calculator.png)

*Strumenti > Calcolatore raster...*: formule scritte come nel calcolatore raster di QGIS su tutti i raster del chunk -
DEM, ortofoto e le loro versioni, pendenza, esposizione e raster del rilievo, le bande della riflettanza, gli indici,
l'ortofoto termica, le scene satellitari, i GeoTIFF dei plugin (con la cartella e il file, `terrain_twi`, o col nome che
dà loro il plugin) e i risultati precedenti.

* La tabella elenca ogni raster con il nome da usare nelle formule, le sue bande e le sue celle; un doppio clic scrive il
  nome nella formula.
* Una banda si scrive `orthomosaic@2`, `"dem@1"` o `reflectance@NIR` (con il nome della banda); un raster di una banda
  anche con il solo nome. `+ - * / ^`, i confronti `< <= > >= = <>` (1 dove è vero, 0 dove è falso), `AND OR NOT`,
  `if(condizione, a, b)` e le funzioni `sqrt`, `abs`, `min`, `max`, `mean`, `clip`, `coalesce`, `isnan`, `ln`, `log10`,
  `sin`...
* La formula viene controllata mentre la scrivi: la finestra dice cosa legge e su quale griglia, o cosa non va.
* Il risultato è un GeoTIFF sulla griglia del primo raster della formula (o di un altro, alla sua risoluzione o a
  un'altra), gli altri ricampionati su di essa. Si vede su una scala di colori con il valore sotto il cursore, resta
  nell'area di lavoro sotto *Calcolatore raster* (doppio clic per vederlo; tasto destro: calcolalo di nuovo con un'altra
  formula, rimuovilo) e va nel progetto QGIS.

Una cella dove un raster non ha dati resta senza dati nel risultato. Tutto sulle formule è nell'[API](../api.md#calcolatore-raster).

!!! example "Formule"
    * `dem - dem_prima_dello_scavo`: quanto è stato scavato tra due rilievi;
    * `if(slope > 30 AND svf < 0.9, 1, 0)`: le celle ripide che vedono poco cielo;
    * `coalesce(dem, dem_prima_dello_scavo)`: un DEM con i buchi riempiti da un altro.

??? example "Riga di comando e API"
    `ppg calc scavo.ppg "dem - dem_prima_dello_scavo" --name scavato`; `engine.rastercalc.calculate(chunk, formula,
    name)`, `rasters(chunk)`; plugin: `context.add_rasters`, `context.rasters`

## Classificazione della nuvola

*Strumenti > Classificazione della nuvola*:

* **Classificazione automatica** con regole: terreno, vegetazione, murature e strutture, rumore (le classi ASPRS più le
  classi archeologiche 64-71), scritta nella nuvola densa (l'esportazione LAS la tiene).
* La vista 3D colora la nuvola per *Classi* (o *Quota*, o i colori delle foto) con una legenda: togli la spunta a una
  classe per nasconderla.
* Il **lazo** seleziona punti sullo schermo (clic sui vertici o trascinando, doppio clic o ++enter++ per chiudere) e dà
  loro una classe; a un nuovo codice si può dare un nome (per esempio 120 *Crollo*), mostrato nella legenda. Si prendono
  solo i punti visibili davanti, a meno di chiederlo, e le classi nascoste non si prendono mai. I punti corretti a mano
  vengono ricordati: le regole e il classificatore li lasciano come sono.
* **Addestra il classificatore** impara le classi da quei punti (o da tutti i punti classificati): la geometria attorno
  a ogni punto a tre scale, l'altezza dal suolo e il colore, con una random forest (o il gradient boosting per un file
  più piccolo). Riporta l'accuratezza per classe e classifica il resto della nuvola. Il modello viene tenuto nel
  progetto; *Salva / Carica un classificatore* (`.ppgclf`) lo riusa su altri scavi con una spaziatura dei punti simile.
  *Classifica con il classificatore addestrato* prende una confidenza minima (sotto, i punti restano non classificati);
  *Dimentica le correzioni a mano* toglie i segni, le classi restano.
* **Classi dalle US (poligoni)** dà ai punti dentro ogni unità disegnata o caricata (in pianta) la classe di quell'unità
  (`US 12 (area 1)`, codici da 100, tenuti quando lo rifai; le correzioni a mano restano se non togli la spunta) e mostra
  punti, area in pianta e quote per US in una tabella (*Salva CSV...*).

!!! tip "Le classi guidano i prodotti"
    La finestra del DEM elenca le classi della nuvola densa: solo *Terreno* dà un modello del terreno (DTM). La finestra
    dell'ortofoto rende trasparenti le classi scelte dove sono la superficie visibile; quella del modello fa la mesh delle
    sole classi spuntate; *File > Esporta > Nuvola di punti* esporta solo alcune classi; le sezioni e le piante a quota
    tengono solo le classi spuntate. Il rumore (classi 7 e 18) non fa mai un DEM né una mesh.

??? example "Riga di comando e API"
    `ppg classify scavo.ppg`, `ppg classify-train scavo.ppg modello.ppgclf`, `ppg classify-apply altro.ppg modello.ppgclf`,
    `ppg classify-units scavo.ppg --csv us.csv`, `ppg dem scavo.ppg --classes 2`, `ppg ortho --hide-classes 3,71`,
    `ppg mesh --classes 2,64`, `ppg section ... --classes 2`, `ppg export --cloud muri.las --classes 64`;
    `ai.classify.classify_chunk(chunk)`, `set_class(chunk, mask, code)`, `classify_by_units(chunk, units)`,
    `class_statistics`; `ai.learn.train_on_chunk(chunk)`, `classify_chunk_with_model(chunk, model)`

## BIM e IFC

*Strumenti > Esporta IFC (BIM)...*: scegli le unità (disegnate qui, i poligoni US di un sito PyArchInit, o i solidi dei
proxy) e se includere tutto il rilievo. L'esportazione mostra il suo avanzamento e, alla fine, una finestra con gli
elementi, i loro attributi (`Pset_PyArchInit_US`) e una vista 3D di ciò che è stato scritto. IFC4 in metri con
`IfcMapConversion`; con i proxy il file porta quantità, materiali, gruppi e relazioni: vedi [BIM e IFC](../bim.md).

??? example "Riga di comando e API"
    `ppg ifc scavo.ppg rilievo.ifc`; `bim.ifc.export_chunk_ifc(...)`, `export_proxies_ifc(...)`, `read_ifc_elements(path)`
