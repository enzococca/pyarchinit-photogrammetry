# terrio

Il plugin *terrio* porta nel progetto ciò che fanno altri strumenti e altri programmi, e manda fuori i
prodotti del progetto nei loro formati. È costruito su [terrio](https://github.com/opengeos/terrio), una
libreria di lettura e scrittura di dati geografici scritta in Rust: non c'è altro - GDAL, PROJ, PDAL - da
installare. Importa nuvole di punti di tutti i formati più comuni, da un file o dal web; separa il
terreno dal resto della nuvola densa con i filtri usati per il lidar aereo; prende un raster di
riferimento - un DEM regionale, un'ortofoto più vecchia - sopra il rilievo; esporta la nuvola per i
programmi di laser scanning e l'ortofoto come tasselli per le mappe web.

Questa pagina descrive la versione 0.1.2 del plugin, che richiede pyarchinit-photogrammetry 1.1.0 o
successiva e terrio 0.4 (vengono accettate la 0.3.2 e le versioni 0.4). Il README e il codice sono nel
[repository dei plugin](https://github.com/enzococca/pyarchinit-photogrammetry-plugins/tree/main/plugins/terrio).

<figure markdown="span">
  ![La superficie di tutti i punti e dei soli punti del terreno](../img/plugins/terrio/dsm-dtm.jpg)
  <figcaption>Il terreno di una nuvola densa di 3,1 milioni di punti da 57 foto di drone, classificata
  con SMRF (cella 0,5 m, pendenza 0,2, soglia 0,3 m, prima il rumore basso). (a) La superficie di tutti
  i punti, con i 74.536 punti lasciati fuori dal terreno in rosso; (b) la superficie dei soli punti del
  terreno: le case e gran parte della macchia sui versanti non ci sono più. Ombreggiature dei punti messi
  su una griglia di 30 cm, in un sistema locale; Tell Sultan, il sito del
  tutorial.</figcaption>
</figure>

## In breve

1. Installa il plugin; la prima volta che un passo ha bisogno di terrio, il programma propone di
   scaricarlo (circa 17 MB), oppure *Plugin > terrio > Installa terrio...*.
2. *Importa nuvola di punti...*: un file LAS, LAZ, COPC, EPT, PLY o E57, o un indirizzo web, in un nuovo
   chunk o al posto della nuvola densa di quello attivo.
3. *Classifica il terreno (SMRF, CSF, PMF)...*: i punti senza un'altra classe diventano terreno (classe 2)
   o resto (classe 1); poi
   costruisci un DEM della classe *Terreno*: un modello del terreno.
4. *Raster di riferimento dal web...*: un DEM regionale o un'ortofoto sopra il rilievo, tenuti come
   versione del DEM o dell'ortofoto, da confrontare con i tuoi.
5. *File > Esporta*: la nuvola densa in E57 o EPT, l'ortofoto in PMTiles o MBTiles.

## Che cosa fa

| Passo | Che cosa dà | A che cosa serve |
|---|---|---|
| Importa nuvola di punti | una nuvola LAS, LAZ, COPC, EPT, PLY o E57 - anche in uno zip, anche sul web - come nuvola densa di un nuovo chunk o di quello attivo, con i suoi colori (o l'intensità come grigi) e le sue classi | una scansione laser, il lidar di un rilievo regionale, la nuvola di un altro programma, accanto alla fotogrammetria |
| Classifica il terreno | terreno (classe 2) e altri punti (classe 1), rumore basso (7), vegetazione per altezza (3, 4, 5); i punti di altre classi restano come sono | un DTM sotto alberi ed edifici, il terreno per i plugin [Terreno](terrain.md) e [Linee elettriche](powerlines.md) |
| Raster di riferimento | un raster sul web o sul disco, letto solo sopra il rilievo, riproiettato nel sistema del chunk, tenuto come versione | confrontare il rilievo con un DEM regionale o un'ortofoto più vecchia, il paesaggio intorno al sito |
| Esportazioni | E57 ed EPT della nuvola (LAS, LAZ, COPC e PLY dalla riga di comando), PMTiles e MBTiles dell'ortofoto | i programmi di laser scanning, i visualizzatori web di nuvole, le mappe web, le mappe su un tablet sul campo |

## Che cosa serve prima { #before }

* **Il plugin**, installato dal programma: vedi [Guide ai plugin](guides.md#install).
* **terrio stesso.** Installato con pip, il plugin se lo porta dietro. L'applicazione desktop non ha
  pip: la prima volta che un passo ha bisogno di terrio, il programma chiede se scaricarlo da PyPI (circa
  17 MB, controllato con il suo SHA-256) nella cartella propria del plugin; dopo lo scaricamento, rifai
  il passo. *Plugin > terrio > Installa terrio...* fa lo stesso in ogni momento, `ppg plugin terrio
  install` dalla riga di comando. terrio esiste per macOS (Apple Silicon e Intel), Windows (x64 e ARM64)
  e Linux (x86-64 e ARM64).
* **Un progetto.** Un'importazione in un nuovo chunk non vuole altro; la classificazione e
  l'esportazione della nuvola vogliono un chunk con una nuvola densa; i tasselli un'ortofoto; il raster
  di riferimento un chunk con un sistema di riferimento (altrimenti *the chunk needs a reference system
  (align it with GPS or markers first)*).
* **Memoria.** I filtri del terreno lavorano su tutta la nuvola insieme: decine di milioni di punti
  vogliono qualche gigabyte.
* **Internet**, per le sorgenti sul web.

## I dati { #data }

### File di nuvole di punti { #clouds }

| Formato | File | Note |
|---|---|---|
| LAS, LAZ | `.las`, `.laz` | i formati del lidar aereo e della maggior parte dei programmi; compresso in LAZ |
| COPC | `.copc.laz` | un LAZ ordinato per leggerne delle parti: sul web si leggono solo le parti che servono |
| EPT | l'`ept.json` di una cartella EPT | il formato di Entwine e di molti archivi lidar aperti; letto sul posto dal web |
| PLY | `.ply` | le nuvole della fotogrammetria e di molti programmi |
| E57 | `.e57` | laser scanner: ogni scansione posizionata con la sua posa, tutte in una nuvola |
| ZIP | `.zip` | uno di questi dentro |

* **Sul web**: un indirizzo che comincia con `https://`, `s3://`, `gs://` (Google Cloud) o `az://`
  (Azure) al posto di un file.
* **Che cosa entra**: le coordinate, i colori (i colori a 16 bit vengono portati a 8 bit), oppure
  l'intensità come grigi quando il file non ha colori, e i codici di classificazione. Gli altri
  attributi (ritorni, tempo GPS) non vengono tenuti.
* **Sistema di riferimento**: LAS, LAZ, COPC ed EPT di solito portano il loro; E57 e PLY spesso no.
  Dallo nella finestra di importazione (per esempio `EPSG:32633`) quando il file non ce l'ha; senza, il
  nuovo chunk è locale.
* **Punti al massimo**: una nuvola più grande di quanto serve - un tassello di lidar regionale, una
  scansione densa - si può sfoltire all'importazione: da COPC ed EPT si leggono meno livelli, poi i punti
  vengono scelti a caso.
* **Solo le classi**: i codici ASPRS delle classi da tenere, per esempio `2` per il terreno di un lidar
  classificato, `2,6` terreno ed edifici.

### Nuovo chunk o al posto della nuvola { #destination }

* **Un nuovo chunk**, chiamato come il file (o con il nome dato), nel sistema di riferimento del file o
  in quello dato. Nella finestra diventa il chunk attivo; dalla riga di comando il chunk attivo del
  progetto resta quello di prima (`--activate` rende attivo il nuovo). I suoi punti sono tenuti da
  un'origine tonda, l'intero sotto le loro coordinate più piccole, come fa il programma per le scansioni
  laser.
* **Al posto della nuvola densa del chunk attivo**: la nuvola che c'era resta come versione
  (nell'*Area di lavoro*, sotto la nuvola densa; tasto destro, *Rendi attiva (la usano gli altri
  strumenti)*, per tornarci). Quando il file ha un altro sistema di riferimento i punti vengono
  riproiettati in quello del chunk; un chunk senza sistema di riferimento rifiuta un file che ne ha uno
  (*... has no reference system and the file has one (EPSG:...): import it into a new chunk*).

### Prima di classificare { #before-ground }

* La classificazione cambia solo i punti delle classi **0** (mai classificati), **1** (non classificati)
  e **2** (terreno) - e **3, 4, 5** quando si chiedono le classi della vegetazione: il terreno prende il 2,
  il resto l'1 (7 il rumore basso, 3, 4 e 5 la vegetazione se richiesto). Ogni altra classe resta com'è:
  gli edifici (6), l'acqua (9), i muri e le altre classi archeologiche del programma (dal 64 in su), il
  rumore già trovato (7 e 18, lasciati anche fuori dai filtri: un punto sbagliato molto sotto il terreno
  non può tirarlo giù), le classi di un lidar importato; e così i punti classificati a mano. Una nuvola
  arrivata già classificata tiene ciò che ha: per riclassificarla tutta, dai prima ai suoi punti la
  classe 1.
* Con le classi di vegetazione, ogni punto sopra il terreno che non ha un'altra classe viene classificato
  per l'altezza sul punto di terreno più vicino - sotto 0,5 m vegetazione bassa (3), sotto 2 m media (4),
  più in alto alta (5): anche muri in piedi, sezioni, cumuli di terra ed edifici non classificati
  altrimenti diventano "vegetazione" (lo dice anche il suggerimento dell'opzione). Classificali prima - i
  muri nella classe 64, gli edifici nella 6: quelle classi restano - o correggi a mano dopo. Usala per le
  piante di un campo, e guarda il risultato.
* La nuvola deve essere in metri con la z verso l'alto: un chunk con un sistema di riferimento, oppure
  livellato.

### Raster di riferimento { #reference-data }

* **Un Cloud Optimized GeoTIFF sul web**, con il suo indirizzo `https://`: si legge solo la parte sopra
  il rilievo. Molti DEM nazionali e globali (Copernicus, per esempio) sono pubblicati come COG.
* **Una ricerca STAC**, scritta `STACIT:...` come la vuole terrio (vedi la documentazione di terrio).
* **Un file** sul disco, qualsiasi GeoTIFF.
* L'area letta è l'estensione del rilievo - del DEM, altrimenti dell'ortofoto, della nuvola densa o
  delle foto allineate - più il margine; il raster viene riproiettato nel sistema del chunk.
* **Quote.** Un DEM nazionale o globale può dare quote sul geoide (sul livello del mare) mentre un
  rilievo da drone con il GNSS dà quote sull'ellissoide, o il contrario: la differenza è l'ondulazione del
  geoide, decine di metri in molti luoghi (in una prova un DEM globale di 30 m stava 37,6 m sotto il DEM
  del drone). Confronta le forme, oppure correggi lo scarto prima di confrontare le quote.

## Passo per passo nel programma { #program }

Il menu *Plugin > terrio* ha *Importa nuvola di punti...*, *Classifica il terreno (SMRF, CSF, PMF)...*,
*Raster di riferimento dal web...* e *Installa terrio...*; *File > Esporta* ha *Nuvola di punti E57
(terrio)...*, *Nuvola di punti EPT (terrio)...*, *Ortofoto PMTiles (terrio)...* e *Ortofoto MBTiles
(terrio)...*. I passi che cambiano il progetto girano in background con la barra di avanzamento, poi il
progetto viene salvato e le viste aggiornate. La maggior parte dei campi tiene il valore usato l'ultima
volta.

### 1. Importare una nuvola di punti { #import }

*Importa nuvola di punti...*

| Campo | Che cosa fa |
|---|---|
| *File* | LAS, LAZ, COPC, PLY, E57, uno zip di questi o l'`ept.json` di un EPT |
| *Oppure URL (https, s3, gs, az)* | una nuvola sul web, al posto del file |
| *Destinazione* | *un nuovo chunk*, oppure *la nuvola densa del chunk attivo (quella di ora resta fra le versioni)* |
| *Nome del nuovo chunk (vuoto = nome del file)* | |
| *Punti al massimo (0 = tutti)* | |
| *Solo le classi (es. 2,6; vuoto = tutte)* | i codici ASPRS delle classi da tenere |
| *Sistema di riferimento se il file non lo dice (es. EPSG:32633)* | |

Alla fine una finestra dà i punti importati e il chunk (*7.400 punti in laser.*); un nuovo chunk
diventa quello attivo. Per vedere la nuvola: la vista *Modello*, *Nuvola densa*.

### 2. Classificare il terreno { #ground }

*Classifica il terreno (SMRF, CSF, PMF)...* lavora sulla nuvola densa del chunk attivo.

| Campo | Predefinito | Che cosa fa |
|---|---|---|
| *Filtro* | SMRF | *SMRF (Simple Morphological Filter)*, *CSF (Cloth Simulation Filter)* o *PMF (Progressive Morphological Filter)* |
| *Cella (m) - SMRF e PMF* | 1,0 | la griglia su cui si costruisce la superficie più bassa |
| *Pendenza - SMRF* | 0,15 | il terreno più ripido che ci si aspetta, come rapporto fra salita e distanza (0,15 = 15 %) |
| *Soglia di quota (m) - SMRF e CSF* | 0,5 | quanto sopra il terreno stimato un punto può stare ed essere ancora terreno (SMRF), la sua distanza dal telo (CSF) |
| *Pendenza - PMF* | 1,0 | la pendenza di PMF, che dice quanto in fretta la sua soglia di quota cresce con le finestre |
| *Distanza iniziale (m) - PMF* | 0,15 | il primo gradino di quota di PMF |
| *Finestra massima (m; vuoto = del filtro)* | vuoto | SMRF e PMF: la misura dell'oggetto più grande da togliere, come l'edificio più grande |
| *Risoluzione del telo (m) - CSF* | 1,0 | la misura delle celle del telo |
| *Rigidita' del telo (1-3) - CSF* | 3 | 3 per il terreno piano, 2 per il rilievo, 1 per i versanti ripidi |
| *Prima il rumore basso (classe 7, ELM)* | no | i punti isolati sotto il terreno (profondità sbagliate, riflessi) trovati per primi e lasciati fuori dal terreno |
| *Altri punti in classi 3/4/5 per altezza dal suolo (0,5 e 2 m)* | no | le classi della vegetazione, [con cautela](#before-ground): anche muri, sezioni e cumuli senza un'altra classe diventano vegetazione |

I tre filtri sono quelli dell'elaborazione del lidar aereo (PDAL), eseguiti da terrio:

* **SMRF** costruisce la superficie più bassa su una griglia e ne allarga le finestre passo dopo passo,
  tenendo come terreno i punti entro la soglia. Un buon inizio per una nuvola fotogrammetrica: una cella
  da mezzo metro a un metro, la pendenza del terreno vero più ripido (0,2-0,3 per i versanti di un tell),
  una soglia di qualche decimetro.
* **CSF** capovolge la nuvola e vi lascia cadere sopra un telo: i punti vicini al telo sono terreno. Poco
  da impostare: la risoluzione del telo e la sua rigidità.
* **PMF** apre la superficie con finestre sempre più grandi e una soglia di quota che cresce con esse;
  ha campi suoi, con i suoi predefiniti - pendenza 1,0, distanza iniziale 0,15 m - separati da quelli di
  SMRF.

Alla fine una finestra conta le classi, con i nomi che usa il programma, nella sua lingua:

```text
Filtro SMRF: 7.400 punti, 7.350 cambiati
  50 punti di altre classi o classificati a mano lasciati come erano
  2 Terreno: 6.105
  3 Vegetazione bassa: 140
  4 Vegetazione media: 553
  5 Vegetazione alta: 550
  7 Rumore (basso): 2
  64 Struttura muraria: 50
```

### 3. Raster di riferimento { #reference }

*Raster di riferimento dal web...*

| Campo | Predefinito | Che cosa fa |
|---|---|---|
| *URL del COG, ricerca STAC (STACIT:...) o file* | | la sorgente |
| *Tenerlo come versione di* | DEM | *DEM* o *Ortofoto* |
| *Margine intorno al rilievo (m)* | 20 | fin dove oltre il rilievo il raster viene letto |
| *Risoluzione (m; vuoto = della sorgente)* | vuoto | la cella del risultato |
| *Nome della versione (vuoto = dal file)* | vuoto | `Riferimento: <nome del file>` se vuoto (`Reference: ...` quando il programma parla inglese) |

Il raster viene letto sopra il rilievo e il margine, riproiettato nel sistema del chunk (bilineare per un
DEM, cubico per un'ortofoto) e tenuto come versione: il DEM o l'ortofoto attivi non cambiano. Una
finestra ne dà le dimensioni (*Versione "Riferimento: dem_regionale.tif": 60 x 60 pixel a 1 m.*). La
versione sta nell'*Area di lavoro* sotto il DEM (o l'ortofoto): tasto destro, *Mostra*; per un DEM anche
*Differenza con il DEM attivo*.

### 4. Esportazioni { #exports }

* *File > Esporta > Nuvola di punti E57 (terrio)...*: la nuvola densa nelle coordinate del rilievo, con
  colori e classi, come una scansione sola.
* *File > Esporta > Nuvola di punti EPT (terrio)...*: un dataset EPT - una cartella con `ept.json`,
  `ept-data/` ed `ept-hierarchy/` - per i visualizzatori web e per QGIS.
* *File > Esporta > Ortofoto PMTiles (terrio)...* e *... MBTiles (terrio)...*: l'ortofoto attiva come
  tasselli di mappa web, in Web Mercator. PMTiles è un solo file per le mappe web; MBTiles si legge in
  QGIS, QField e molte applicazioni di mappe.
* LAS, LAZ, COPC e PLY: dalla riga di comando (`export`).

## Dalla riga di comando { #cli }

`ppg plugin terrio COSA PROGETTO ...`. I comandi lavorano sul chunk attivo (`--chunk NOME` per un
altro); quelli che cambiano il progetto lo salvano.

```bash
ppg plugin terrio version
ppg plugin terrio install
ppg plugin terrio import scavo.ppg scansione.e57 --label laser --crs EPSG:32633
ppg plugin terrio import scavo.ppg https://host/lidar.copc.laz --chunk rilievo --max-points 5000000 --classes 2
ppg plugin terrio ground scavo.ppg --method smrf --cell 0.5 --slope 0.2 --threshold 0.3 --noise
ppg plugin terrio ground scavo.ppg --method pmf
ppg plugin terrio ground scavo.ppg --method csf --resolution 0.5 --rigidness 2
ppg plugin terrio reference scavo.ppg https://host/dem_regionale.tif --product dem --margin 50 --resolution 1
ppg plugin terrio reference scavo.ppg ortofoto_2010.tif --product orthomosaic --name "Ortofoto 2010"
ppg plugin terrio export scavo.ppg nuvola.e57
ppg plugin terrio export scavo.ppg nuvola.ept
ppg plugin terrio export scavo.ppg nuvola.copc.laz
ppg plugin terrio export scavo.ppg ortofoto.pmtiles
```

| Comando | Opzioni |
|---|---|
| `version` | il terrio in uso (codice di uscita 1 se non c'è) |
| `install` | scarica terrio nella cartella del plugin |
| `import PROGETTO SORGENTE` | un file o un indirizzo web; senza `--chunk` un nuovo chunk (`--label` il suo nome) - il chunk attivo del progetto resta, `--activate` rende attivo il nuovo; `--chunk NOME`: al posto della nuvola densa di quel chunk; `--max-points`; `--classes 2,6`; `--crs EPSG:...` |
| `ground PROGETTO` | `--method` smrf, csf o pmf; `--cell` (1.0); `--slope` (del filtro: SMRF 0.15, PMF 1.0); `--threshold` (del filtro: SMRF e CSF 0.5, PMF 0.15 - la sua distanza iniziale); `--window`; `--resolution` (1.0, CSF); `--rigidness` (3, CSF); `--noise`; `--vegetation` |
| `reference PROGETTO URL` | `--product` dem o orthomosaic; `--margin` (20); `--resolution`; `--name` |
| `export PROGETTO USCITA` | `.e57`, `.ept` (una cartella), `.las`, `.laz`, `.copc.laz`, `.ply`: la nuvola densa; `.pmtiles`, `.mbtiles`: l'ortofoto |

## Leggere i risultati { #reading }

### Le classi { #reading-classes }

* La finestra - o il comando - conta i punti di ogni classe, quanti sono cambiati e quanti di altre
  classi (o classificati a mano) sono rimasti come erano.
* Guardali: la vista *Modello*, *Nuvola densa*, colori *Classi*.
* Poi il modello del terreno: *Flusso di lavoro > Costruisci DEM...* con la sola classe *Terreno*;
  rendilo la versione attiva per gli strumenti che vogliono il suolo nudo.
* Leggi il DTM come ombreggiatura o con il plugin *Terreno*. Dossi dove c'erano edifici o alberi vogliono
  dire che alcuni dei loro punti sono rimasti terreno: abbassa la soglia, dai una finestra più grande
  (SMRF, PMF) o un telo più fine (CSF). Buchi e gradini su terreno ripido vero - i versanti di un tell, le
  pareti di una trincea - vogliono dire che del terreno è stato tolto: alza la pendenza o la soglia,
  oppure usa CSF con rigidità 1 o 2.
* Strutture sottili sul terreno nudo - un muretto basso, un argine - spesso vengono classificate come
  terreno: per il modello del terreno può andare bene, per una pianta dei muri no.

### Raster di riferimento { #reading-reference }

La versione mostra la sorgente sopra il rilievo sulla griglia che hai chiesto. *Differenza con il DEM
attivo* mostra dove i due DEM differiscono: una differenza uniforme è un diverso sistema delle quote o
uno spostamento del rilievo; un disegno è un cambiamento vero (scavo, erosione, un edificio) o l'errore
di uno dei due. Un DEM regionale di decine di metri riduce un sito a poche celle: descrive il paesaggio
intorno, non il sito.

### Limiti { #limits }

* **Tutto in memoria.** Importazione e classificazione tengono tutta la nuvola.
* **Una classificazione per tutta la nuvola.** Parti diverse di un sito possono volere parametri
  diversi: classifica chunk separati, oppure correggi a mano (il programma tiene le classi date a mano).
* **E57 in un pezzo.** Le scansioni di un E57 vengono unite in una nuvola all'importazione e scritte
  come una scansione all'esportazione; l'importazione E57 del programma le tiene separate.
* **terrio è giovane.** Le sue funzioni possono ancora cambiare prima della versione 1.0; il plugin
  accetta le versioni con cui è stato provato.

## File scritti { #files }

* Una nuvola importata è la nuvola densa del suo chunk, nella cartella del chunk
  (`<progetto>.ppg.files/chunk_<n>/point_cloud.ply`); una nuvola sostituita resta in
  `versions/point_cloud/`.
* Un raster di riferimento: `versions/dem/<id>/dem.tif` o `versions/orthomosaic/<id>/orthomosaic.tif`
  nella cartella del chunk, un GeoTIFF a tasselli, compresso, con le panoramiche.
* Le classi vengono scritte nella nuvola densa.
* Il progetto registra la sorgente di un'importazione, i parametri e i conteggi dell'ultima
  classificazione e la sorgente di ogni versione di riferimento.
* terrio, quando lo scarica il plugin, va in `plugins/_data/terrio/lib/` dentro la cartella dei plugin
  del programma (vedi [Plugin](../plugins.md)).
* Le esportazioni vanno dove scegli; un'esportazione EPT è una cartella.
