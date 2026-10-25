# Disegno e misure

Sull'ortofoto e sul DEM si disegnano le unità stratigrafiche, le tracce di sezione e le quote, si misurano
distanze, aree e volumi, e SAM disegna unità e pietre al posto tuo. Ciò che disegni si salva con il progetto,
arriva in QGIS con il collegamento dal vivo e va in PyArchInit con *Pubblica*.

## La barra Disegno

![La barra Disegno](../img/gui/toolbar-drawing.png)

| | Strumento | Cosa fa |
| --- | --- | --- |
| ![](../img/gui/icons/sposta-interroga.png){ width=20 } | **Sposta** | spostarsi e ingrandire; un clic su un elemento mostra i suoi attributi |
| ![](../img/gui/icons/seleziona.png){ width=20 } | **Seleziona** | scegliere gli elementi da eliminare, unire o ritagliare ([sotto](#selezionare-eliminare-unire)) |
| ![](../img/gui/icons/disegna-unita-stratigrafica.png){ width=20 } | **Disegna US** | un'unità per vertici; *Mano libera* nel suo menu ([sotto](#unita-stratigrafiche)) |
| ![](../img/gui/icons/modifica-i-vertici-delle-unita.png){ width=20 } | **Vertici** | spostare, aggiungere e togliere i vertici di un'unità; *Rimodella* nel suo menu ([sotto](#modificare-le-unita)) |
| ![](../img/gui/icons/snap.png){ width=20 } | **Snap** | i punti sui vertici e sui segmenti dei layer visibili |
| ![](../img/gui/icons/traccia-sezione.png){ width=20 } | **Sezione** | due clic: la sezione del modello lungo la traccia ([sotto](#sezioni)) |
| ![](../img/gui/icons/misura-distanza.png){ width=20 } | **Misura** | distanza e dislivello |
| ![](../img/gui/icons/quota-puntuale-pyarchinit-quote.png){ width=20 } | **Quota** | una quota per `pyarchinit_quote` ([sotto](#quote)) |
| ![](../img/gui/icons/punto-di-misura-quota-temperatura-bande-e-indici-nel-punto.png){ width=20 } | **Punto** | un punto di misura: quota, temperatura, bande e indici |
| ![](../img/gui/icons/marker-clic-su-un-punto-proiettato-sulle-foto-che-lo-vedono.png){ width=20 } | **Marker** | un marker dove fai clic ([marker](photos-markers.md#marker)) |
| ![](../img/gui/icons/segment-anything-sam.png){ width=20 } | **SAM** | unità e pietre disegnate da Segment Anything ([sotto](#segment-anything)) |
| ![](../img/gui/icons/patch-dell-ortofoto-assegna-le-immagini-a-un-area.png){ width=20 } | **Patch** | la foto da usare in un'area dell'ortofoto ([patch](processing.md#patch-dellortofoto)) |
| ![](../img/gui/icons/elimina-selezionati.png){ width=20 } | **Elimina** | gli elementi selezionati (++delete++) |
| ![](../img/gui/icons/unisci-selezionati.png){ width=20 } | **Unisci** | le unità selezionate in un solo poligono, o linee contigue |
| ![](../img/gui/icons/cancella-disegni.png){ width=20 } | **Cancella disegni** | tutti i disegni del chunk |
| ![](../img/gui/icons/layer.png){ width=20 } | **Layer** | il [pannello Layer](panes.md#layer) |
| ![](../img/gui/icons/sezioni.png){ width=20 } | **Sezioni** | il [pannello Sezioni](panes.md#sezioni) |
| ![](../img/gui/icons/apri-in-qgis-collegamento-dal-vivo.png){ width=20 } | **QGIS** | apre i disegni in QGIS ([QGIS dal vivo](archaeology.md#qgis-dal-vivo)) |
| ![](../img/gui/icons/pubblica.png){ width=20 } | **Pubblica** | scrive in PyArchInit le unità disegnate e le quote |

*Strumenti > Disegno su ortofoto* ha gli stessi strumenti e qualcuno in più che lavora su un'area: *Misura area e
superficie 3D*, *Ritaglio*, *Modifica DEM*, *Ritaglia o esporta con il poligono selezionato*.

## Unità stratigrafiche

![Le unità sull'ortofoto](../img/gui/view-ortho-units.png)

1. **Disegna US**: clic sui vertici, doppio clic per chiudere. Con **Mano libera** (nel menu del pulsante, come
   le tecniche di digitalizzazione di QGIS) premi, traccia e rilascia; il pulsante poi mostra la sua icona.
2. La scheda chiede i campi di `pyunitastratigrafiche` di PyArchInit: area, US (o una US esistente del sito), tipo
   di unità (`unita_tipo_s`), indice stratigrafico (`stratigraph_index_us`: 1 caratterizzazione, 2 limite di US),
   tipo (`tipo_us_s`), disegnatore, data, rilievo originale. Il layer mostra le stesse colonne (`scavo_s`,
   `area_s`, `us_s`...).

Le unità disegnate si salvano nel progetto e tornano quando lo si riapre.

**Snap** (il suo menu: vertici, segmenti, tolleranza 6/12/20 px) mette i punti di ogni strumento di disegno
esattamente sui vertici o sui bordi dei layer visibili, segnati da un quadrato (vertice) o da un cerchio (segmento)
magenta: le unità tracciate lungo un limite esistente lo condividono, senza buchi né sovrapposizioni.

*PyArchInit > Pubblica unita' e quote disegnate* scrive `pyunitastratigrafiche`; *Carica poligoni US sull'ortofoto*
mostra le unità già nel database.

??? example "Riga di comando e API"
    `pa.publish_units(units, sito)`, `pa.us_polygons(sito)`, `archaeo.stratigraphy.export_units`

## Modificare le unità

![Lo strumento Vertici](../img/gui/view-vertices.png)

**Vertici** (*Modifica i vertici delle unita'*): fai clic su un'unità e compaiono i suoi vertici (quadrati bianchi)
con la metà di ogni lato (punti azzurri).

| Per... | fai così |
| --- | --- |
| spostare un vertice | trascinalo |
| aggiungere un vertice | trascina la metà di un lato |
| togliere un vertice | ++alt++ + clic o clic destro su di esso, oppure ++delete++ sull'ultimo toccato |
| tenere il contorno | ++enter++, o un clic su un'altra unità |
| rimetterlo com'era | ++esc++ |

*Snap* funziona mentre trascini.

**Rimodella** (*Rimodella un'unita' con una linea*, nel menu di *Vertici*) funziona come *Reshape Features* di
QGIS: fai clic sui vertici di una linea che attraversa due volte il contorno di un'unità, doppio clic o ++enter++, e
il contorno tra i due incroci diventa la linea. Tracciata fuori allarga l'unità, tracciata attraverso toglie la
parte più piccola; i buchi interni restano.

* Ogni modifica è una voce di *Modifica > Annulla* e una riga del log (*Vertici di US 6: area ... m2*, *Rimodellata
  US 6*); un contorno che si incrocerebbe da sé viene rifiutato.
* Il proxy dell'unità (*Proxy US*) viene ricostruito da solo con la sua base, e annullare la modifica riporta anche
  il proxy.
* Un poligono di PyArchInit cambiato così aspetta *Salva modifiche in PyArchInit*. Lo stesso vale per ciò che si
  salva in QGIS con il collegamento dal vivo.

??? example "Riga di comando e API"
    `archaeo.stratigraphy.reshape_polygon(polygon, line)`; `RasterView.begin_vertex_edit`, `move_vertex`,
    `insert_vertex`, `remove_vertex`, `end_vertex_edit`

## Selezionare, eliminare, unire

**Seleziona**: clic su un elemento, ++shift++ / ++ctrl++ + clic per aggiungerne o toglierne uno, trascina un
riquadro per selezionare tutto ciò che tocca (i layer nascosti vengono saltati). La selezione è contornata in ciano
e, per un solo elemento, i suoi attributi compaiono nel pannello *Layer*.

* **Elimina** (o il tasto ++delete++) toglie unità, singole parti di unità in più parti, tracce di sezione con i
  loro disegni, linee di curve e di piante a quota, punti.
* **Unisci** fonde unità in un solo poligono (scegli di chi tenere numero di US, area e tipo; le fessure sottili
  tra unità disegnate affiancate si chiudono) o linee contigue dello stesso layer.
* Il clic destro apre gli stessi comandi; ++esc++ toglie la selezione.

I poligoni caricati da PyArchInit cambiano in `pyunitastratigrafiche` solo con *Salva modifiche in PyArchInit*
(pannello *Layer*), che chiede prima di eliminare.

??? example "Riga di comando e API"
    `pa.update_unit(gid, geometry=..., crs=...)`, `pa.delete_unit(gid)`

## Quote

**Quota** (separata da SAM): un clic legge la quota sul DEM a piena risoluzione, poi i campi di
`pyarchinit_quote`: area, US, tipo di unità, quota (modificabile), unità di misura, disegnatore, data, rilievo
originale. *Non chiedere piu'* li tiene per i clic successivi (scegli di nuovo lo strumento per cambiarli). I punti
vanno nel layer *Quote*, si salvano nel progetto e si scrivono con *Pubblica*.

??? example "Riga di comando e API"
    `pa.publish_quote(points, sito, crs)`, `pa.publish_elevations(chunk, sito, area, us, points)`

## Punti di misura

**Punto** (*Punto di misura*) mette un punto dove fai clic con le sue coordinate, la quota del DEM, la temperatura e
la sua anomalia, la riflettanza di ogni banda e gli indici. I punti vanno nel layer *Punti di misura*, si salvano
con il progetto, arrivano in QGIS con gli altri layer e si esportano come GeoPackage, CSV o GeoJSON (*Esporta i
punti di misura...*). Vedi [Multispettrale e termico](multispectral.md).

## Sezioni

1. **Sezione**, poi due clic sull'ortofoto: il modello viene tagliato lungo la traccia.
2. Il disegno compare nel pannello *Sezioni* (SVG, profilo DXF e DXF 3D in `sezioni/`), e la traccia diventa un
   elemento del layer *Sezioni*.

L'SVG è in scala 1:20, o nella scala normalizzata più dettagliata (1:50, 1:100, 1:200...) in cui sta su circa un
foglio A0 (una sezione di 174 m attraverso un tell: 1:200); la linea del profilo è di 0,5 mm e il passo della
griglia del DXF segue l'altezza della sezione (10 cm in un saggio, 1-2 m su un tell).

Il menu del pulsante, *Opzioni delle sezioni*, sceglie se tagliare il modello o la nuvola densa e quali classi
tenere (dal modello, i tratti che stanno su punti di quelle classi). **Pianta a quota** taglia il modello in
orizzontale in un suo layer, con la stessa scelta.

??? example "Riga di comando e API"
    `ppg section scavo.ppg x1 y1 x2 y2 --out sezione.svg`; `archaeo.sections.section(chunk, p1, p2)`,
    `sections_along(chunk, baseline, interval, half_width)`, `plan_slice(chunk, elevation)`;
    `pa.publish_section_trace`

## Misure

* **Misura**: distanza e dislivello tra due clic.
* **Misura area e superficie 3D** (*Strumenti > Disegno su ortofoto*: trascina un riquadro o fai clic su un
  poligono; oppure *Misura l'area e la superficie* su un poligono selezionato): l'area in pianta e quella della
  superficie dal DEM e dal modello, la pendenza media e le quote; il contorno resta nel layer *Misure*. Dà anche il
  **colore Munsell** dell'ortofoto all'interno: la mediana dei suoi pixel senza il decimo più scuro e quello più
  chiaro (ombre, ciottoli, riflessi), come notazione Munsell e tessera più vicina di una tavola dei suoli (`10YR
  5/4`); vedi [colore Munsell](archaeology.md#colore-munsell).
* Sul DEM la barra di stato mostra la **pendenza** e l'**esposizione** sotto il cursore e, con un DTM conservato
  come versione, l'altezza dal suolo. *Strumenti > Pendenza del DEM* ed *Esposizione del DEM* fanno i due raster
  (dal bianco al rosso; una ruota di colori dal nord) e li mostrano, con i gradi sotto il cursore. La vista 3D
  colora la nuvola densa per *Altezza dal suolo* una volta classificato il suo terreno.

## Curve di livello e volumi

![La finestra Curve di livello](../img/gui/dialog-contours.png)

**Curve di livello** (*Strumenti > Curve di livello...*, anche nella barra del flusso di lavoro) disegna le linee
come un layer da interrogare (quota, curva direttrice), che resta quando passi tra DEM e ortofoto, e le esporta come
DXF, GeoJSON o GeoPackage.

* La finestra mostra l'intervallo di quote del DEM e propone un'equidistanza tonda che dia al massimo circa 50 curve
  (5 cm su un saggio, 50 cm su un tell alto 20 m); chiede prima di un'equidistanza che darebbe più di 300 curve.
* *Levigatura* (gaussiana, in metri) aiuta con i DSM da drone.
* Le linee tengono i loro vertici entro mezzo pixel, e gli anelli chiusi più piccoli di 25 pixel (rumore) restano
  fuori: un tell dà migliaia di linee invece di 158.000, aggiunte alla vista in una volta.

![La finestra Volume scavo / riporto](../img/gui/dialog-volume.png)

**Volume scavo / riporto** (*Strumenti > Volume scavo / riporto...*): il riferimento è la superficie prima del
lavoro - un piano alla quota del piano di campagna originale o il DEM prima dello scavo. *Scavo* è il volume tolto
(ora sotto il riferimento), *riporto* quello aggiunto; con *Quota minima* il deposito sopra il punto più basso. Le
celle parziali lungo il bordo del poligono sono pesate per la parte che coprono. I volumi tra le fasi di uno scavo
sono in [Fasi dello scavo](archaeology.md#fasi-dello-scavo).

??? example "Riga di comando e API"
    `ppg contours`; `archaeo.terrain.contours`, `volume`, `hillshade`, `slope_degrees`; `pa.publish_volume`

## Segment Anything

SAM 2.1 disegna le unità da solo. Si installa a richiesta (*Strumenti > Segment Anything 2.1 > Installa / aggiorna
SAM 2.1...*) nel suo ambiente, con la versione di PyTorch per la scheda grafica: CUDA sulle schede NVIDIA (scelta
dall'installatore su Windows e Linux), la GPU di Apple con MPS sul Mac (circa 3,5 volte più veloce della CPU su un
M4 Pro). La finestra alla fine dell'installazione dice quale si usa.

**SAM** nella barra Disegno lavora nel modo scelto dal suo menu:

| Modo | Cosa fai |
| --- | --- |
| **Tocco** | clic su un oggetto: SAM guarda la parte di ortofoto sullo schermo, quindi ingrandisci per i dettagli e allontanati per le unità grandi |
| **Riquadro** | trascina un riquadro attorno a un oggetto |
| **Area** | trascina un riquadro: tutte le unità all'interno |
| **Pietre** | trascina un riquadro su un paramento o un lastricato: ogni pietra ([sotto](#disegno-pietra-per-pietra)) |

La maschera compare tratteggiata con la scheda dell'unità: *Salva*, *Annulla*, o *Correggi* (++shift++ + clic
aggiunge una parte, clic destro ne toglie una, ++enter++ salva, ++esc++ scarta).

* **Tutta l'ortofoto** la scandisce in tasselli sovrapposti da 1024 pixel, a una risoluzione e a una accuratezza
  scelte (normale, alta, massima: più punti e ritagli per tassello): gli oggetti tagliati dai tasselli si
  riuniscono, i doppioni si scartano e le unità non si sovrappongono (le più piccole vengono tolte da quelle più
  grandi). Le unità automatiche vanno nel layer *Unita' da SAM* senza numero di US, pronte per la
  [proposta di US](archaeology.md#proposta-di-us-e-rapporti).
* Il menu sceglie anche il **modello**, da *Molto veloce (tiny)* a *Molto accurato (large)*.
* *Prompt sulle foto* segmenta un oggetto su una foto.

### Disegno pietra per pietra

**Pietre**, sull'ortofoto o sull'ortofoto di un prospetto: trascina un riquadro su un paramento o un lastricato e SAM
2.1 contorna ogni oggetto all'interno. Le pietre restano (diametro equivalente tra la pietra più piccola e la più
grande richieste, forma compatta), la malta e la terra se ne vanno. Formano il layer *Pietre* di quell'immagine,
salvato con il progetto: un riquadro disegnato di nuovo sostituisce le pietre al suo interno, e quelle sbagliate si
tolgono con *Seleziona* e ++delete++.

*Strumenti > Segment Anything 2.1 > Esporta il disegno delle pietre* le scrive come DXF (polilinee chiuse sul layer
`PIETRE`), SVG in scala o GeoJSON, nelle coordinate dell'immagine (metri lungo e su per il muro per un prospetto).

??? example "Riga di comando e API"
    `ai.sam.segment_orthomosaic(chunk, segmenter, points=, labels=, box=, bounds=)`,
    `auto_units(chunk, segmenter, bounds=, resolution=, thoroughness=)`, `read_orthomosaic`,
    `segment_photo_and_classify(...)`
