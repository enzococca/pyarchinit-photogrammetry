# Elaborazione

Dalle foto allineate alla nuvola densa, al modello, al DEM e all'ortofoto: i comandi del menu *Flusso di
lavoro* nel loro ordine, poi ciò che si può fare dopo con i prodotti. Un esempio completo con foto vere è il
[tutorial](../tutorials/tell.md).

!!! note "Salvataggio e foto che non si leggono"
    Il progetto viene salvato dopo ogni passo, una volta che ha un file. I passi che leggono le foto (mappe di
    profondità, texture, ortofoto, *Elabora tutto*) elencano prima le foto che non si leggono - file vuoti
    tenuti solo online da Dropbox, iCloud o OneDrive - e propongono *Ricollega a un'altra cartella...*,
    *Continua senza queste foto* o *Annulla*.

## Allineare le foto

![La finestra Allinea foto](../img/gui/dialog-align.png)

*Flusso di lavoro > Allinea foto...* è la finestra di *Allinea foto* di Metashape:

| Scelta | Cosa vuol dire |
| --- | --- |
| **Precisione** | la dimensione a cui si leggono le foto: più alta è più lenta e trova più punti |
| **Preselezione generica** | le coppie che si sovrappongono si trovano prima su foto ridotte (fino a 40 foto si provano comunque tutte le coppie) |
| **Preselezione di riferimento** | le coppie scelte dal GPS delle foto, da un allineamento precedente o dall'ordine di scatto |
| **Punti** | *SIFT*, oppure punti appresi abbinati da LightGlue (*DISK* o *ALIKED*) per le foto termiche e le superfici povere di dettagli; se mancano PyTorch e kornia il programma propone di installarli |
| **Azzera l'allineamento attuale** | oppure tienilo: le foto aggiunte dopo vi entrano |

Sotto *Avanzate*:

* i limiti dei punti chiave e dei punti di legame;
* le maschere applicate ai punti chiave o ai punti di legame; *Escludi i punti di legame fermi* (un turntable con
  la camera fissa);
* l'abbinamento guidato, l'adattamento adattivo del modello della camera;
* il GPS delle foto che tiene il blocco mentre cresce una foto alla volta;
* il **metodo**: *Automatico* allinea tutte le camere insieme (globale) da 40 foto di un rilievo da drone (GPS
  sparso sul sito), una foto alla volta sotto questo numero e per oggetti e scene ravvicinate;
* la **GPU** per l'abbinamento: la migliore insieme alla CPU, spenta, o un adattatore scelto. La scelta viene
  ricordata e vale anche per le mappe di profondità PatchMatch.

Poi *Importa riferimenti...* e *Aggiorna georeferenziazione* collocano il rilievo sui punti di controllo, e
*Ottimizza camere* ricalcola il blocco con loro e con le [barre di scala](photos-markers.md#barre-di-scala).

### Ottimizzare le camere

*Flusso di lavoro > Ottimizza camere* compensa insieme pose, calibrazione e punti di legame. Che cosa tiene il
blocco:

* **3 o più punti di controllo**, o 2 o più barre di scala di controllo: fissati alle loro coordinate, il blocco si
  piega per passarci; i check point e le barre di check misurano il risultato;
* altrimenti **le posizioni GNSS delle foto**, quando sono accurate - un fix o un float RTK o PPK, 0,5 m o meglio
  (*Posizioni GNSS delle foto*: *automatico*): ogni foto pesa secondo la sua accuratezza, e la focale trovata
  dall'allineamento resta quella, perché da una sola quota, guardando in basso, le posizioni non distinguono la
  focale dalla distanza dal suolo. La finestra dice quante foto tengono il blocco e con quale stato RTK
  (*Posizioni GNSS usate come vincolo: 82 foto, fix*).

Senza né gli uni né le altre, un blocco nadirale su terreno piatto può piegarsi a cupola mentre l'errore di
riproiezione resta lo stesso: la distorsione radiale lo compensa nelle foto (82 riprese RTK fix di un P4
Multispectral finivano a metri dalle loro posizioni). **Una rete di sicurezza** controlla ogni ottimizzazione:
quando le camere finiscono più lontane dalle loro posizioni GNSS accurate di tre volte lo scarto che avevano, e di
30 cm più di quello, l'ottimizzazione viene annullata e si rimette l'allineamento di prima (anche quando si ferma
per un errore o viene annullata a mano). O piegava il blocco, o le posizioni di una parte del blocco non
concordano con i punti di controllo - un volo spostato di una quantità sua, quote miste fra MRK ed EXIF (uno
spostamento uguale per tutte le posizioni, come una base su un punto sbagliato o un altro sistema di quote, lo
prende la georeferenziazione e non annulla nulla): per tenere un'ottimizzazione così, togli la spunta alle camere
in *Riferimenti* (clic destro: *Non usare i riferimenti di tutte le camere*) e ripetila. *non usate* lascia fuori le posizioni (l'ottimizzazione delle versioni precedenti, sempre sotto la
rete). Il motore *Proprio* in *automatico* tiene il blocco allo stesso modo, focale compresa; *usate con la loro
accuratezza* e *usate, con un bias per volo* lasciano la focale alla compensazione, che la rifinisce dove il
blocco la determina (con un bias per volo la rete giudica ogni posizione meno il bias del suo volo). Dalla riga
di comando: `ppg optimize progetto.ppg --gnss auto|off` (`plain` e `bias` con `--engine ppg`); l'uscita dice
quante foto hanno tenuto il blocco e se la focale è rimasta quella dell'allineamento.

**Ogni parte della calibrazione viene rifinita solo dove il blocco la determina.** Prima di compensare, il
programma misura quanto bene il blocco conosce ogni parte della calibrazione di ogni sensore (il suo scarto
quadratico medio, dalla covarianza della compensazione). Per prima la focale: sopra lo 0,5 % - foto che guardano
dritte in basso su terreno piatto, dove la focale non si distingue dalla quota di volo, nemmeno con punti di
controllo su quel terreno - la focale non viene rifinita. Non resta nemmeno dove l'ha lasciata l'allineamento - un
allineamento che ha rifinito la focale di un blocco così l'ha lasciata in un punto qualunque - ma tra quella stima
e il valore dichiarato dalle foto (il loro EXIF), ciascuno pesato per quanto è noto: la stima per quello che il
blocco ne sa, il valore delle foto al 2 % (le focali dell'EXIF sono arrotondate). Un blocco che della focale non
sa quasi nulla finisce sul valore delle foto; uno che la determina quasi resta vicino alla propria stima, anche
quando le foto dichiarano un valore sbagliato di qualche per cento. Il programma prima porta lì la focale, a
piccoli passi, con il blocco che la segue (*focale portata a 1010 px, tra la stima dell'allineamento (2430 px) e
il valore dichiarato dalle foto (1000 px), e tenuta: il blocco non la determina*; nel log ci sono i valori), e
un'ottimizzazione successiva la lascia lì. Le foto che non dichiarano una focale tengono quella dell'allineamento
(*focale dell'allineamento tenuta: il blocco non la determina*). Rifinita lì, la focale scivola insieme alla quota
delle camere: su un anello volato una volta su terreno piatto finiva dieci volte troppo lunga, con le camere 131 m
troppo in alto. Poi la distorsione e il punto principale, se chiesto, ciascuno per quello che sposta all'angolo
della foto: una parte che il blocco determina peggio di mezzo pixel resta ferma anche lei, le altre vengono
rifinite. Le parti si misurano in un ordine fisso, ciascuna senza quelle già tenute: dopo la focale, da ciò che
serve meno a un obiettivo a ciò che gli serve di più - la distorsione tangenziale, il punto principale, la
distorsione radiale per ultima - così che di due parti che il blocco non distingue si rinuncia a quella che serve
meno, anche quando da sola era passata (alla focale solo per la distorsione radiale). Lo stesso anello, una volta
tenuta la focale, determina la sua distorsione radiale, che viene rifinita (i target di verifica finiscono a 9
mm); una griglia di foto nadirali su terreno piatto no - rifinita lì, la distorsione piega il blocco a cupola tra
i punti di controllo - e tiene quella dell'allineamento. Il risultato nomina che cosa è rimasto fermo (*parti
della calibrazione tenute ferme perché il blocco non le determina: distorsione radiale*). Una focale o un termine
radiale che scappano durante l'ottimizzazione vengono tenuti e l'ottimizzazione ripetuta. Foto oblique, rilievo o
voli a due quote determinano la calibrazione, che viene rifinita come prima. Togli la spunta a *Tieni le parti
della calibrazione che il blocco non determina* per rifinire tutto comunque (`ppg optimize --free-undetermined`;
in un batch, la stessa spunta nel passo). Il controllo gira sul core nativo; senza, la calibrazione viene rifinita
come prima e il log lo dice. Quando il blocco non dà una covarianza con cui misurare una parte, quella parte resta
ferma senza essere stata misurata, e nel log c'è un avviso; una focale tenuta così resta dove l'ha messa
l'allineamento, senza avvicinarla al valore delle foto. Un blocco che nulla colloca e che non si può misurare
con libera la sola focale (lì è singolare), o la misura lì peggio dello 0,5 %, non conosce affatto la focale: quella va al valore delle foto.

**Punti di controllo solo agli angoli di un blocco piatto.** Un blocco di foto nadirali su terreno piatto può
piegarsi a cupola - il centro più alto o più basso dei bordi - mentre la distorsione radiale compensa nelle
foto. L'errore di riproiezione non lo mostra, e nemmeno i punti di controllo agli angoli del blocco: una cupola
passa per tutti. Il controllo qui sopra impedisce all'ottimizzazione di piegare un blocco così (su una griglia
sintetica di 40 foto con quattro punti di controllo agli angoli, un punto nel mezzo finisce a 3 mm con la
distorsione ferma e a 4-6 cm rifinendola), ma non può raddrizzare un blocco che l'allineamento ha già piegato.
Ciò che mostra una cupola è un punto nel mezzo: mettici un punto di controllo, o almeno un punto di verifica, e
guarda il suo errore dopo l'ottimizzazione. Foto inclinate in avanti su strisciate volate nei due sensi, o un
secondo volo a un'altra quota, tolgono l'ambiguità stessa.

*Motore*: *Automatico* (il predefinito, come per gli altri passi: la scelta del programma, per ora COLMAP), *COLMAP* o
*Proprio* (`ppg optimize --engine auto|colmap|ppg`). Senza niente che lo tenga (il GPS di un drone, nessun punto di
controllo) la compensazione può scalare e spostare tutto il blocco nel suo sistema interno; la georeferenziazione lo
segue, così le foto restano dov'erano nel sistema di coordinate (prima finivano fuori posto fino ad *Aggiorna
georeferenziazione*).

### Diagnostica dell'allineamento

*Strumenti > Diagnostica dell'allineamento* dice perché un allineamento non va:

| Strumento | Cosa mostra |
| --- | --- |
| **Grafo delle corrispondenze** | le foto dove sono state scattate (le altre in un anello); linee tra le foto abbinate, più spesse e più verdi con più corrispondenze, rosse sotto 30; gruppi separati e foto isolate elencati; un doppio clic su una linea apre la coppia |
| **Corrispondenze tra due foto** | le due selezionate in *Foto* (o clic destro su di esse): corrispondenze verificate in verde, candidate scartate in rosso, punti di legame in blu |
| **Residui dei punti di legame** | nella vista Foto, dove ogni punto di legame è stato misurato e dove lo rimette il modello: verde sotto 1 px, arancio sotto 3, rosso oltre; frecce 20 volte più lunghe (anche dal clic destro su una foto allineata) |
| **Calibrazione della camera** | i parametri di ogni sensore, modificabili come valori di partenza, il modello, *Calibrazione fissa* (allineamento e ottimizzazione non la cambiano) e un grafico della distorsione (frecce sul fotogramma e lo spostamento radiale verso l'angolo) |

### Controllo prima di lasciare il sito

*Flusso di lavoro > Controllo prima di lasciare il sito*, subito dopo l'allineamento, mentre il saggio è ancora
aperto:

* la **mappa di quante foto inquadrano ogni punto**: rosso scuro nessuna, rosso una, arancio due, giallo tre o
  quattro, verde di più; il numero di foto sotto il cursore; *Copertura delle foto* nell'area di lavoro;
* un **riepilogo**: quota e area viste da meno di tre foto, foto non allineate o sfocate, marker in nessuna foto,
  in una o due soltanto.

### Precisione

*Precisione* in *Riferimenti*, oppure *Flusso di lavoro > Stima la precisione*: la deviazione standard di ogni
posizione di camera, marker e punto di legame dalla covarianza del bundle adjustment, rispetto ai punti di
controllo e alla loro accuratezza (o al GPS delle foto).

* Una colonna *Precisione X/Y/Z (mm)* per marker e camere, la mediana dei punti di legame sotto le tabelle, la
  precisione della camera nel suggerimento di una foto.
* Con un DEM, la mappa della sua precisione verticale si apre nella vista raster (verde piccola, rossa grande,
  sigma Z sotto il cursore; *Precisione del DEM* nell'area di lavoro).
* Una quota presa dopo tiene la sua precisione (anche in `rilievo_originale` per PyArchInit e nell'IFC). Un nuovo
  allineamento o una nuova georeferenziazione tolgono la precisione vecchia.

## I prodotti

### Nuvola densa

![La finestra Costruisci nuvola densa](../img/gui/dialog-dense.png)

*Flusso di lavoro > Costruisci nuvola densa...*: qualità, filtro di profondità e motore; *Solo le mappe di
profondità* fa solo le mappe (per il modello fuso, senza nuvola densa). *Ritaglia nuvola densa...* tiene un
intervallo di quote e/o un cilindro attorno all'asse del turntable: toglie il tappetino e i target prima di fare
la mesh di un oggetto.

### Modello

![La finestra Costruisci modello](../img/gui/dialog-model.png)

*Flusso di lavoro > Costruisci modello...*:

* **Dati sorgente**: le *mappe di profondità* (la superficie dalla loro fusione volumetrica, TSDF, come in
  Metashape: più fedele e con i colori delle foto, senza la nuvola densa) o la *nuvola densa*, di cui si possono
  usare solo le classi spuntate;
* **Tipo di superficie**: *arbitraria* (3D) o *campo di altezze* (2,5D, per il terreno);
* **Numero di facce**, e il voxel della fusione delle mappe di profondità (0 = automatico);
* **Chiudi i buchi fino a** (metri, 0 = nessuno): per il campo di altezze, chiude i buchi con un vuoto tra i
  punti misurati fino a questa misura. La parte chiusa è interpolata, non misurata: il log dice quanti buchi
  sono stati chiusi e quanti metri quadrati in pianta sono. Quello che è fuori dal contorno del rilievo non si
  chiude mai.

Il campo di altezze segue la densità dei punti posto per posto: dove la nuvola è più rada le facce sono più
grandi, senza buchi, e restano aperte solo le aree senza punti ([Campo di altezze](../api.md#campo-di-altezze)).

La ricostruzione di Poisson lavora in un processo separato: se il codice nativo si blocca, il passo riprova a una
profondità minore e alla fine costruisce un campo di altezze 2,5D.

### Texture

![La finestra Costruisci texture](../img/gui/dialog-texture.png)

*Flusso di lavoro > Costruisci texture...*:

* **Correzione colore**: esposizione delle foto e vignettatura, misurate la prima volta;
* **Mappatura**: *ortofoto adattiva* (terreno dall'alto: la texture è l'ortofoto del modello, fatta dal motore
  dell'ortofoto a tasselli; le pareti ripide e le sezioni hanno celle proprie in una fascia sotto, come in
  Metashape); *generica* (ogni faccia la sua cella: oggetti e modelli piccoli); *automatica* sceglie l'ortofoto
  per i modelli grandi rivolti verso l'alto;
* **Dimensione texture** 16384 o 32768 divide la texture in 4 o 16 pagine da 8192 pixel (tasselli della pianta,
  o gruppi di facce vicine), come fa Metashape: ogni pagina resta leggibile dalle schede grafiche e dai
  visualizzatori.

La texture è un'immagine (o una per pagina) accanto a `model.ply`, che porta le coordinate di texture di ogni
faccia; la vista *Modello con texture* mostra tutto il modello.

??? example "Riga di comando e API"
    `chunk.build_texture(size, mapping="auto")`; `integrations.metashape.reimport_model(chunk)`;
    `io.mesh_ply.read_mesh`, `write_mesh`

### DEM

![La finestra Costruisci DEM](../img/gui/dialog-dem.png)

*Flusso di lavoro > Costruisci DEM...*: i dati sorgente, la risoluzione (automatica di base), *Riempi le celle
senza punti tra i punti vicini* e *Interpola i vuoti* fino a una larghezza e un'area ([vuoti del
DEM](#vuoti-del-dem)). Quando la nuvola densa è classificata la finestra elenca le sue classi con i loro punti: spuntare solo
*Terreno* dà un modello del terreno (DTM), con i vuoti sotto ciò che resta fuori interpolati; curve di livello e
volumi poi lo usano. Il rumore (classi 7 e 18) non fa mai un DEM né una mesh.

### Ortofoto

![La finestra Costruisci ortofoto](../img/gui/dialog-ortho.png)

*Flusso di lavoro > Costruisci ortofoto...*:

| Scelta | Cosa fa |
| --- | --- |
| **Superficie** | il DEM, il modello o la nuvola densa su cui si proiettano le foto |
| **Fusione** | *Mosaico senza cuciture* (di base: i dettagli di ogni cella dalla sua foto migliore, le differenze di luce sfumate lungo le cuciture), *Nessuna (una foto per cella)* o *Media delle foto* |
| **Correzione colore** | esposizione di ogni foto e vignettatura di ogni obiettivo, misurate la prima volta su punti visti da più foto, o prima con *Strumenti > Calibra i colori delle foto* |
| **Togli persone e mezzi visti da poche foto** | dove tre o più foto vedono una cella, la migliore che va d'accordo con le altre |
| **Riempi i piccoli buchi** | fino a 100 pixel che nessuna foto vede, dai loro bordi; i buchi più grandi restano trasparenti |
| **Classi nascoste** | con una nuvola classificata, le classi scelte rese trasparenti dove sono la superficie visibile (attrezzi, persone, vegetazione) |

Ogni cella sceglie tra tutte le foto che la inquadrano, così nessuna cucitura segue i tasselli. Sul saggio
sintetico con metà delle foto più scure del 30 % e ogni foto vignettata, il mosaico corretto differisce da quello
delle foto intatte di 0,3 livelli nelle basse frequenze (25 senza correzione).

**Memoria.** Le foto che l'ortofoto rilegge sono tenute in una cache dimensionata sulla memoria del computer (una
sua quota, oltre a un minimo di qualche foto per worker). `PPG_PHOTO_CACHE_MB` nell'ambiente, un numero intero di
megabyte, fissa quella dimensione a mano: più piccola su un computer con poca memoria, più grande quando le foto
vengono rilette troppo spesso; `PPG_PHOTO_CACHE_MB=0` lascia il solo minimo, che è la cache della versione 1.10. Ogni
altro valore è ignorato, con una riga nel log.

Un chunk **senza foto allineate** (un modello importato o unito) ha l'ortofoto dalla texture del modello: vedi
[Ortofoto di un chunk senza foto](multi-survey.md#ortofoto-di-un-chunk-senza-foto).

### Elabora tutto

*Flusso di lavoro > Elabora tutto...* (++ctrl+r++) fa tutti i passi in fila con le scelte predefinite; con un file
di progetto scrive anche il rapporto HTML. Con la superficie a *campo di altezze*, **Chiudi i buchi fino a**
(metri, 0 = nessuno) chiude i buchi del modello come in [*Costruisci modello*](#modello): la parte chiusa è
interpolata, non misurata, e il log dice quanta è.

## Versioni

Le finestre di *Nuvola densa*, *Modello*, *DEM* e *Ortofoto* chiedono, quando il prodotto c'è già, se **conservare
la versione attuale** (per confrontarla) o sovrascriverla, e il nome di quella nuova; una costruzione fallita
rimette quella conservata.

* L'area di lavoro mostra il nome della versione attiva accanto a ogni prodotto ed elenca sotto quelle
  conservate.
* Un doppio clic mostra una versione conservata senza renderla attiva (i raster nella vista ortofoto, nuvole e
  modelli nella vista 3D con la scritta *Versione: ... (non attiva)*; scegliere una vista nell'elenco torna ai
  prodotti attivi).
* Clic destro su una versione conservata: *Rendi attiva* (quella che usano tutti gli strumenti; l'attiva prende
  il suo posto tra le conservate), *Differenza con il DEM attivo* (attivo meno conservato, rosso dove l'attivo è
  più alto, blu dove è più basso, dZ sotto il cursore; un avviso quando i due DEM hanno risoluzioni diverse),
  *Esporta*, *Rinomina*, *Elimina*.
* Clic destro su un prodotto: *Conserva una copia come versione* (per esempio prima di riclassificare la nuvola),
  *Rinomina*.

Le versioni conservate stanno in `versions/` dentro la cartella del chunk.

??? example "Riga di comando e API"
    `core.versions.keep_active`, `activate`, `versions`, `version_file`, `dem_difference`, `rename`, `delete`

## Confidenza e pulizia

![La finestra Pulisci la nuvola densa](../img/gui/dialog-clean-cloud.png)

**Confidenza** (*Flusso di lavoro > Pulizia e confidenza > Calcola la confidenza*): per la nuvola densa e/o i
vertici del modello, il numero di mappe di profondità che confermano ogni punto (entro l'1 % della sua distanza
dalla camera, la tolleranza della fusione; mezzo spigolo per un vertice); la nuvola fusa ce l'ha già. La vista 3D
(*Colori > Confidenza*, anche per il modello) colora gli intervalli 0 (grigio: nessuna mappa, per esempio un buco
che la mesh ha chiuso), 1 (rosso), 2, 3-4, 5-9, 10-19, 20 o più (blu); togliere la spunta a una riga della legenda
nasconde quei punti o quelle facce, un'anteprima di una pulizia. Servono le mappe di profondità del chunk.

| Pulizia | Cosa toglie |
| --- | --- |
| **Pulisci la nuvola densa** | punti sotto una confidenza, punti isolati (filtro statistico: vicini, deviazioni standard), punti senza vicini entro un raggio, gruppi staccati più piccoli di un numero di punti, classi, un colore (con una tolleranza); i punti trovati vengono contati e mostrati in rosso nella vista 3D, poi tolti quando confermi |
| **Pulisci il modello** | pezzi staccati sotto una parte del più grande, facce con bassa confidenza dei vertici, triangoli stirati, topologia, buchi chiusi fino a un raggio, levigatura di Taubin (bordi fissi), riduzione delle facce; colori e confidenza restano, la texture va rifatta |
| **Pulisci i punti di legame (selezione graduale)** | soglie su errore di riproiezione, numero di foto, incertezza della ricostruzione e accuratezza della proiezione con i conteggi dal vivo, poi l'ottimizzazione delle camere (per i modelli scritti da pose importate conta solo l'incertezza) |
| **Lazo** | elimina o tiene i punti e le facce scelti sullo schermo |

Ogni pulizia può prima conservare la versione attuale.

??? example "Riga di comando e API"
    `ppg confidence scavo.ppg --model`, `ppg clean-cloud`, `ppg clean-model`, `ppg clean-tie-points`;
    `chunk.compute_confidence("point_cloud" or "model")`, `engine.confidence.depth_map_support`;
    `chunk.clean_point_cloud(...)`, `clean_model(...)`, `clean_tie_points(...)`, `engine.cleaning.point_filters`,
    `face_filters`, `delete_points`, `delete_faces`

## Ritaglio

*Ritaglio* in *Strumenti > Disegno su ortofoto* (o *File > Esporta > Ritagliati...*, *Pulizia e confidenza >
Ritaglia*):
trascina un riquadro o fai clic su un poligono (doppio clic per chiudere) sull'ortofoto o sul DEM; oppure
seleziona un poligono (un'unità) e *Ritaglia o esporta con il poligono selezionato*. Poi scegli i prodotti:

* **esportati** ritagliati in una cartella: DEM e ortofoto senza dati o trasparenti fuori dal poligono, modello
  PLY con colori, confidenza e coordinate in doppia precisione oppure OBJ con la texture, nuvola LAS con classi e
  confidenza, il poligono come GeoJSON;
* oppure **ritagliati nel progetto**, con gli originali conservati come versioni.

Modello e nuvola si possono anche limitare in quota.

??? example "Riga di comando e API"
    `ppg crop scavo.ppg --polygon "x1,y1 x2,y2 x3,y3" --out ritagli` (oppure `--bbox`, `--wkt`, `--geojson`,
    `--in-project`); `chunk.export_dem(path, region=)`, `export_orthomosaic`, `export_model`, `export_point_cloud`,
    `crop_dem`, `crop_orthomosaic`, `crop_model`, `crop_point_cloud(region=)`

## Tagliare il modello

![La finestra Taglia il modello](../img/gui/dialog-cut-model.png)

*Taglia* nella vista 3D, oppure *Flusso di lavoro > Pulizia e confidenza > Taglia il modello con un piano*. Il
piano:

* **orizzontale** (un clic alla quota), **verticale** (due clic: il piano per la loro linea in pianta), **per tre
  punti cliccati**, **inclinato** (direzione e inclinazione);
* lungo una sezione del pannello *Sezioni*, o per i marker selezionati.

Tieni la parte sopra o sotto (sopra = a sinistra della linea dal punto 1 al punto 2 per un piano verticale), o una
fetta di uno spessore, come modello del chunk (il precedente tra le versioni) o come file; oppure salva le due
parti, o una serie di fette parallele ogni tanto, come PLY (o OBJ con la texture) in coordinate reali.

Le facce attraversate dal piano vengono tagliate su di esso, con colori, confidenza e coordinate di texture
interpolati. *Chiudi i solidi tagliati* riempie la faccia tagliata di ogni solido chiuso nel suo colore (le unità
del plugin di geoarcheologia, i proxy US, un vaso: il suo vuoto resta vuoto); le superfici aperte restano aperte.
I solidi importati affiancati (un modello di unità stratigrafiche) restano solidi separati.

??? example "Riga di comando e API"
    `ppg cut-model scavo.ppg --horizontal 101.5 --keep below` (oppure `--vertical X1,Y1,X2,Y2`, `--points ...`,
    `--plane X,Y,Z,NX,NY,NZ`, `--dip X,Y,Z,IMMERSIONE,INCLINAZIONE`; `--keep above/below/slab/both/series`,
    `--thickness`, `--step`, `--out`); `chunk.cut_model(point, normal, keep=...)`, `engine.mesh_cut.clip`, `slab`,
    `series`, `horizontal`, `vertical`, `through_points`, `dipping`

## Regione di ricostruzione

*Flusso di lavoro > Regione di ricostruzione*: un riquadro (giallo nella vista 3D; *Regione* lo mostra o lo
nasconde) attorno ai punti di legame, girato come la vista 3D, sul poligono selezionato della pianta, o con
centro, dimensioni e rotazione scritti a mano. Mappe di profondità, nuvola densa e modello costruiti dopo restano
dentro di esso - il saggio e non il campo attorno, il vaso e non il tavolo - e così DEM e ortofoto. *Togli la
regione* torna a tutto ciò che vedono le foto.

## Orientare un sistema locale

Un rilievo senza coordinate - niente punti di controllo, niente GPS utilizzabile - esce dall'allineamento in un
sistema tutto suo: il saggio inclinato di qualche grado, un oggetto coricato su un fianco, l'origine da qualche
parte nel mezzo. *Flusso di lavoro > Orienta il sistema locale...* mette quel sistema dove deve stare: un'origine,
un asse x e un piano orizzontale scelti sul rilievo stesso. Il chunk resta LOCALE; nulla viene ricalcolato dalle
foto.

![La finestra Orienta il sistema locale](../img/orient-dialog.it.png)

**Tre punti.** **A** è l'origine, **B** dà la direzione dell'asse x (*x lungo A→B*; senza la spunta l'asse x
tiene la direzione di adesso), **C** è un terzo punto del piano orizzontale (senza C il piano resta com'è e
cambiano solo la rotazione attorno alla verticale e l'origine). Ogni punto viene da:

* **Nel 3D**: un clic sul modello o sulla nuvola della vista 3D;
* **Sull'ortofoto**: un clic sul raster mostrato, con la quota letta dal DEM («Nessun DEM in quel punto» se manca);
* l'**elenco dei marker**: un marker già collocato, nella sua posizione.

**Piano adattato.** Il piano orizzontale adattato a un'area: *Seleziona l'area nel 3D (lazo)* o *Disegna l'area
sull'ortofoto*. La finestra dice quanti punti ha preso e quanto stanno lontani dal piano (NMAD); A e B, se ci sono
entrambi, danno comunque l'origine e l'asse x. Un pavimento, un mosaico, la cresta di un muro.

![Gli assi in anteprima nella vista 3D](../img/orient-preview.it.png)

Mentre si scelgono i punti, la vista 3D mostra i nuovi assi (x rosso, y verde, z blu, il piano orizzontale come
un quadrato blu) e la finestra scrive l'inclinazione, la sua direzione, la rotazione e la traslazione. *Capovolgi*
rovescia l'asse z - la finestra lo suggerisce quando z guarderebbe dalla parte opposta alle foto -; *Quota di A* e
*X, Y di A* danno le coordinate dell'origine. **Distanza vera A-B** scala il rilievo perché A-B misuri quanto
misurato sul campo: diventa una barra di scala di controllo «Orientamento A-B» sui marker di orientamento (spenta
quando le barre di scala di controllo danno già la scala).

**Cosa segue il chunk.** *Applica* sposta tutto ciò che è nelle coordinate del chunk: disegni della pianta e quote,
linee di rottura, disegni liberi, pietre, sezioni e prospetti (con i loro disegni), patch dell'ortofoto, la regione
di ricostruzione, i proxy US, i riferimenti dei marker e, con *Sposta anche i chunk allineati*, i chunk allineati a
questo. Camere, punti di legame, nuvola densa e modello non vengono toccati: seguono da soli.

**Cosa resta dov'era.** Le geometrie già mandate fuori - unità e quote scritte in PyArchInit, un progetto QField, i
file esportati - tengono le coordinate di prima: la finestra del risultato le elenca, per mandarle di nuovo.

**I marker di orientamento.** Tre marker, *Orientamento A*, *B* e *C*, compaiono nel pannello *Riferimenti* con le
nuove coordinate. Lasciali lì: un nuovo allineamento, *Aggiorna georeferenziazione* o *Elabora tutto* (che allinea
di nuovo, ed è sicuro) rimettono il sistema attraverso di loro. Cancellarli riporta, al prossimo allineamento, il
sistema dell'allineamento. Un marker visto in meno di due foto viene segnalato come debole: dopo un nuovo
allineamento non si può ritrovare.

**Le coordinate di campo sostituiscono l'orientamento a mano.** Quando più tardi arriva il sistema dello scavo - tre
o più marker di controllo con le coordinate della stazione totale o del GNSS, oppure, in un sistema scelto per il
chunk, tre o più posizioni delle foto - sono questi a collocare il rilievo e i marker di orientamento si fanno da
parte: non entrano in *Aggiorna georeferenziazione*, il pannello *Riferimenti* li mostra in grigio come
"orientamento, non usato" senza errore (le loro coordinate sono nel sistema orientato a mano), e un nuovo
orientamento a mano viene rifiutato finché i punti di controllo ci sono. Un sistema scelto senza nient'altro che
collochi il rilievo (A messo su X, Y e quota note) lascia ai marker di orientamento il compito di collocarlo.

**Un blocco di foto con il GPS.** Anche quando le foto hanno posizioni GPS (in un sistema vero, come quelle
dell'EXIF), dopo un orientamento a mano il prossimo allineamento tiene l'orientamento a mano: sono i marker di
orientamento a collocare il chunk, che resta LOCALE, e il GPS delle foto non viene usato per collocarlo finché ci sono.
Il GPS torna a collocare il rilievo quando togli i marker di orientamento (il prossimo allineamento mette il chunk nel
sistema del GPS), quando scegli per il chunk un sistema vero, o quando misuri tre o più altri punti di controllo.

**Prodotti da rifare.** DEM, ortofoto e i raster fatti da essi (precisione, copertura, rilievi, pendenza,
esposizione, TIN) sono griglie nella pianta di prima: risultano *da rifare*. La vista raster lo dice in una fascia
gialla sopra l'immagine, e gli strumenti di disegno rifiutano di disegnarci sopra. *Flusso di lavoro > Ricostruisci
i prodotti da rifare* (proposto anche dalla finestra del risultato) li rifà in ordine con i parametri che avevano,
tenendo i disegni; ciò che non può rifare (raster calcolati, multispettrali, termici e satellitari, versioni
conservate) viene elencato da rifare a mano. I prospetti senza scala restano validi: si spostano con il chunk.
La ricostruzione è un solo passo di *Modifica > Annulla* (tutti i prodotti tornano insieme come erano). Il DEM e
l'ortofoto di prima restano fra le versioni solo se nel menu *Flusso di lavoro* è spuntato *Ricostruendo, conserva DEM
e ortofoto attivi come versioni* (spento per impostazione predefinita: sono nel sistema di prima, non si confrontano pixel per pixel con
quelli rifatti, e su un rilievo grande occupano GB).

**Rifiuti.** La finestra non si apre, e dice perché, quando l'orientamento lo dà qualcos'altro: un chunk in un
sistema di coordinate (lo danno i punti di controllo), tre marker di controllo con coordinate (cambia quelle), un
chunk allineato a un altro (orienta il chunk di riferimento: gli altri lo seguono). La distanza vera viene
rifiutata quando le barre di scala di controllo danno già la scala. Le posizioni GPS delle foto vengono sostituite
dai marker di orientamento (il risultato dice quante).

**Annulla.** L'intero orientamento è un solo passo di *Modifica > Annulla*.

??? example "Riga di comando e API"
    `ppg orient scavo.ppg --points "0.12,0.40,1.95;1.80,0.52,2.21;0.30,1.75,1.88" --dry-run` stampa inclinazione,
    rotazione, traslazione e ciò che si sposterebbe, senza salvare; `--markers "1,2,3"`, `--plane-from area.wkt`,
    `--quota`, `--origin X,Y`, `--distance L`, `--flip`, `--x-kept`, `--no-aligned`, `--chunk`. Un plugin chiama
    `context.orient(points=[a, b, c], distance=2.0)`.

## Prospetti

*Flusso di lavoro > Prospetto o sviluppo cilindrico...*, *Prospetto* nel pannello *Sezioni*, oppure il pulsante
**Prospetto** della vista 3D. Ortofoto e rilievo del modello vengono costruiti su un piano, o su un cilindro, che
viene da:

* il **piano verticale di una traccia di sezione**, visto da dove sono le foto o dal lato scelto;
* il **piano di tre o più marker selezionati**, con gli assi scelti come in Metashape: l'asse orizzontale va da un
  marker verso un altro (u = 0 sul primo), quello verticale è perpendicolare e verso l'alto, oppure va fra altri
  due marker. Muri, ma anche superfici inclinate o orizzontali: un pavimento a mosaico, una volta, un muro che
  pende. Il piano guarda sempre verso le foto: quando il primo marker sta a destra per chi guarda, u cresce
  nell'altro verso;
* la **vista 3D com'è ora** (*Prospetto > Ortofoto da questa vista*): il piano perpendicolare alla linea di vista,
  l'alto dello schermo in alto, la destra a destra; quello che si vede nella vista, o tutto il modello;
* **tre punti cliccati** nella vista 3D (*Prospetto > Piano per tre punti*): l'asse orizzontale dal primo al
  secondo;
* il **cilindro di un poligono selezionato**: lo sviluppo di un pozzo o di una torre, visto da dentro o da fuori;
* un **muro tracciato in pianta**: una linea cliccata sull'ortofoto o sul DEM, spezzata o curva, lungo la quale la
  faccia del muro viene distesa girando gli spigoli (*Flusso di lavoro > Muro tracciato in pianta...*, vedi
  [Un muro tracciato in pianta](#un-muro-tracciato-in-pianta)).

**L'asse di uno sviluppo cilindrico.** Il dialogo di un cilindro chiede l'*Asse*:

* **Verticale** (il predefinito): v è la quota, come in una sezione. Una torre che pende, o i cui corsi non sono
  in piano, viene con la cresta e i corsi a onde: la quota è giusta, l'immagine non è diritta.
* **Della regione** (proposto solo quando il chunk ha una regione): l'asse Z della regione, come Metashape sviluppa
  un cilindro lungo la sua regione.
* **Dalla cresta della torre**: la normale al piano che passa per la sommità del muro tutto intorno (il punto più
  alto del modello ogni 5 gradi, un piano adattato due volte lasciando fuori i punti lontani). Una cresta che non è
  un piano - un lato crollato - viene rifiutata con il suo scarto: in quel caso si usa l'asse della regione o due
  punti. Un solo tratto caduto viene lasciato fuori e non inclina l'asse.
* **Due punti nella vista 3D**: dopo il dialogo si cliccano due punti di una stessa linea diritta della struttura
  che corre lungo il suo asse e non è a scarpa (una colonna, un fusto, uno spigolo verticale), prima quello in basso.
  Un clic cade sulla faccia del muro: due clic su una torre a scarpa danno la scarpa, non l'asse - per una torre a
  scarpa si usa la cresta o la regione. La finestra avvisa quando la linea dei due punti non è parallela alla torre: il
  muro guardato lungo la linea, il suo centro al piede e in cima a più di 30 cm l'uno dall'altro, o più di 1 grado
  di scarto.

Qualunque sia il metodo, l'asse passa per il centro della torre: trovata la direzione, il muro viene guardato lungo
l'asse e lì si adatta un cerchio, così un poligono disegnato al piede di una torre che pende non sposta l'asse dal
centro più in alto.

Con un asse inclinato **v è la distanza lungo l'asse**, non la quota, e i corsi e la cresta vengono diritti.
**v = 0** è dove l'asse attraversa la quota media della cresta (*Dalla cresta della torre*), il punto medio dei due
punti (*Due punti*) o la quota media del modello (*Della regione*). Prima
di costruire, la finestra dice l'inclinazione e la direzione ("asse inclinato di 6,6° verso NE", la direzione verso
cui pende la sommità); se non è l'asse della torre si annulla lì. L'inclinazione resta con il prospetto (il suo
suggerimento nell'area di lavoro).

**Scala sulla parete.** Di norma u è l'arco sul cilindro di riferimento (il cerchio adattato al poligono): dove il
muro sta fuori dal cerchio lo sviluppo è più corto del muro, dove sta dentro è più lungo. Con *Scala sulla parete*
u è la lunghezza misurata lungo il muro (una torre ovale, un pozzo irregolare): un primo passaggio misura dove sta
il muro tutto intorno (la mediana di ogni grado, così un pluviale o un albero davanti non conta), poi lo sviluppo
viene costruito su quella lunghezza.

<figure markdown="span">
  ![La vista 3D con il pulsante Prospetto](../img/gui/elevation-3d-view.png)
  <figcaption>La vista 3D che guarda un tell da sud: <em>Prospetto > Ortofoto da questa vista</em> prende questo piano.</figcaption>
</figure>

<figure markdown="span">
  ![Il dialogo dell'ortofoto da questa vista](../img/gui/elevation-view-dialog.png)
  <figcaption>Il dialogo: prospetto o sezione, quello che si vede nella vista o tutto il modello.</figcaption>
</figure>

<figure markdown="span">
  ![L'ortofoto dalla vista nella vista raster](../img/gui/elevation-from-view.png)
  <figcaption>Il risultato: un'ortofoto del modello come lo guardava la vista, in metri del suo piano (Tell Sultan, sistema di riferimento locale).</figcaption>
</figure>

**Prospetto o sezione.** Un prospetto mostra la superficie vista dal piano; *Profondità davanti / dietro* tengono
solo la superficie vicina al piano, così un ponteggio o una sezione davanti al muro non lo nascondono. Una
**sezione** toglie tutto quello che sta davanti al piano e mostra quello che c'è dietro; la linea dove il piano
taglia il modello - il **profilo** - è disegnata in rosso sull'ortofoto ed esportata con lei come DXF.

<figure markdown="span">
  ![Una sezione attraverso un tell con il suo profilo](../img/gui/elevation-section.png)
  <figcaption>Una sezione ovest-est attraverso il tell, vista da sud: il profilo del taglio in rosso, quello che sta dietro al piano a colori; v è la quota.</figcaption>
</figure>

**Coordinate.** L'ortofoto è in metri del piano: u lungo il piano, v verso l'alto. Quando v è verticale (entro un
grado: una traccia di sezione, un muro con i marker in piano, una vista in piano) **v è la quota**, così il
prospetto si legge con le sue quote vere. I marker che stanno sul piano vi sono disegnati con il loro nome.

I prospetti sono elencati nell'area di lavoro (*Prospetti*): un doppio clic mostra l'ortofoto nella vista raster
con il rilievo come Z sotto il cursore (distanza dal piano verso chi guarda: rigonfiamenti e strapiombi di un
muro); il clic destro esporta ortofoto, rilievo, piano e profilo, o li elimina. Le misure su un prospetto sono
nei suoi metri. Dalla riga di comando:

```bash
ppg elevation scavo.ppg --markers M1,M2,M3 [--vertical M4,M5] [--section]
ppg elevation scavo.ppg --trace X1,Y1,X2,Y2 [--side X,Y] --section --back 2
ppg elevation scavo.ppg --facing NX,NY,NZ --centre X,Y,Z [--up 0,0,1]
ppg elevation scavo.ppg --circle X1,Y1,X2,Y2,X3,Y3 --outside [--axis vertical|region|crest|points]
    [--axis-points X,Y,Z,X,Y,Z] [--scale cylinder|wall]
ppg elevation scavo.ppg --like ID [--like-chunk CHUNK] [--same-frame] [--chunk CHUNK] [--name NOME]
```

`--like` costruisce sul sistema e sulla griglia di un prospetto già fatto (il suo id o nome; di `--like-chunk`, se no
dello stesso chunk): lo stesso piano o cilindro - uno sviluppo in scala sulla parete tiene la sua lunghezza lungo il
muro, non una misurata di nuovo -, risoluzione, limiti, profondità (`--front`/`--back` vengono da lì), tipo e
impostazioni del mosaico (correzione del colore attiva o no come lì); non le sue patch né i suoi disegni. Nessun'altra
opzione della superficie va con lui. Un'altra campagna sullo stesso muro, in un altro chunk nello stesso sistema di
riferimento, sta allora sugli stessi pixel e i due si confrontano cella per cella. Due chunk in un sistema LOCAL hanno
mondi propri: `--same-frame` dice che stanno nello stesso sistema (allineati, uniti, orientati sugli stessi marker),
senza il comando rifiuta. Senza `--name` il prospetto nuovo prende il nome di partenza reso distinto ("Torre (2)").

**Foto in controluce.** Ogni dialogo di un prospetto ha *Lascia fuori le foto in controluce (oltre il 2 % di pixel
bruciati)*: una foto scattata contro luce, con il cielo bruciato dietro il muro, non dipinge il prospetto. Una foto è
in controluce quando più del 2 % dei suoi pixel ha tutti e tre i canali a 250 o oltre. Il prospetto dice quante ne
sono state lasciate fuori (il suo tooltip nell'area di lavoro); la correzione del colore di quella costruzione è
misurata sulle foto tenute, e quella del chunk non cambia. Quando tutte le foto sono in controluce la costruzione si
ferma con un errore. Dalla riga di comando: `--skip-backlit`.

### Un muro tracciato in pianta

Un muro che gira gli spigoli, si piega o curva - un recinto, le stanze di un edificio, un muro di terrazza che segue
il pendio di una collina - non sta su un piano solo. *Flusso di lavoro > Muro tracciato in pianta...* (oppure *Muro
tracciato in pianta* come superficie nel dialogo del prospetto) apre a destra il pannello **Muro tracciato in pianta**:
la linea del muro si traccia sulla pianta, e la sua faccia viene distesa lungo quella linea in un prospetto solo,
girando gli spigoli, senza i riquadri di più piani incollati insieme.

**I passi del pannello.** Ogni passo ha un segno (da fare, in corso, fatto) e una riga che dice cosa ha trovato.

1. **Linea del muro sulla pianta.** *Traccia sulla pianta*: clic sui vertici della linea del muro nella vista 2D
   (l'ortofoto o il DEM; se è aperto un prospetto viene mostrata prima la pianta), **Backspace** toglie l'ultimo,
   **Invio** o un doppio clic finisce, **Esc** rinuncia. Un muro curvo si clicca con molti punti: la linea che li
   attraversa è una linea liscia, non a faccette. Clic più vicini di 1 mm sono un punto solo; un ultimo clic entro
   5 cm dal primo, su una linea di quattro punti o più, chiude la linea su di esso (una stanza tracciata tutt'intorno).
   La linea viene controllata subito: una linea che **si incrocia o si tocca**, una che **torna indietro** su se
   stessa (una svolta di oltre 170 gradi: traccia le due facce come due muri) o una senza due punti diversi è
   rifiutata, e il pannello lo dice. Gli altri due modi di avere la linea - seguire il muro fra due clic e i muri
   proposti sulla pianta - sono in [Tracciamento automatico](#tracciamento-automatico) più sotto.
2. **Aggancio alla faccia del muro.** Il prospetto si costruisce sulla linea **come è stata cliccata**, a meno che
   non spunti *Aggancia alla faccia* (non spuntato all'inizio; la spunta resta finché il programma è aperto).
   L'aggancio è una proposta: tracciata la linea, il programma ne sposta una copia sulla faccia del modello e la
   disegna sulla pianta (tratteggiata, "agganciata (proposta)") con di quanto si è spostata, e spunti la casella solo
   se sta sulla faccia che intendevi. Lavora un tratto dritto alla volta: una linea cliccata sulla cresta, in mezzo al muro o
   sul piano davanti finisce sulla faccia. *Fascia di ricerca* (2,5 m) è quanto lontano, davanti e dietro la linea
   cliccata, si cerca la faccia. Ciò su cui sta il clic decide la direzione: sul muro o sul terreno accanto a una
   trincea la faccia è la prima superficie davanti, incontrata da dentro; sul piano davanti al muro o nell'aria di una
   trincea è la prima superficie dietro. L'altro lato di una stanza stretta o di una trincea non viene preso per lei;
   in una trincea o in un corridoio più stretti della fascia, usa una fascia più piccola della loro larghezza. La
   faccia è sempre quella vista dal tuo lato: su un muro addossato a un terreno più alto (sabbia dal tuo lato, una
   trincea profonda dall'altro) viene presa la faccia bassa sopra il terreno, anche se alta 20 cm, mai quella alta
   della trincea; se dal tuo lato non c'è una faccia, resta la linea cliccata. *Smussa
   la linea* (0 m, nessuna) smussa su quella lunghezza un muro curvo cliccato con molti punti incerti, prima
   dell'aggancio. La riga di stato dice di quanto la proposta ha spostato i vertici (mediana e
   massimo, in cm) e quanti tratti ha lasciato come cliccati, lo spostamento di ogni vertice è elencato e la linea
   sulla pianta porta gli stessi valori. La linea su cui si costruisce il prospetto è disegnata piena, l'altra
   tratteggiata; *Aggancia di nuovo* rifà la proposta dopo un cambio di fascia o di smussatura.
   **Lascia la casella vuota** quando la proposta ha spostato un vertice dove non volevi (un cumulo di crollo, un
   contrafforte, un muro davanti, la faccia lontana di una trincea), quando la faccia è così rotta che nessun tratto
   dritto la segue, o quando la linea era già stata disegnata con cura sulla faccia. Quando la faccia non viene trovata
   lungo la linea non c'è proposta e il pannello lo dice.
3. **Lato, quote e profondità.** *Lato da cui guardare*: *Dove sono le foto* (il predefinito: il lato dove sta la
   maggior parte delle foto allineate, ognuna pesata per la distanza e per come guarda il muro, così un volo nadirale
   non vince sulle foto ravvicinate della faccia), *A sinistra della traccia* o *A destra della traccia* (percorrendola
   dal primo clic). Quando il lato più debole pesa il 60 % del più forte o più, la finestra chiede, con i due pesi;
   sono mostrati anche nel pannello. Un cambio di lato rifà la proposta (si cerca un'altra faccia). *Quota
   più bassa* e *Quota più alta* sono lette sul modello lungo la linea usata - le quote del muro, non del terreno
   intorno - arrotondate verso l'esterno a 5 cm; si cambiano liberamente. *Profondità davanti* e *Profondità dietro*
   (0,5 m ciascuna) tengono solo la superficie vicina alla faccia: un'impalcatura o un muro davanti vengono
   attraversati. *Prolunga prima* e *Prolunga dopo* allungano il prospetto oltre il primo e l'ultimo punto, nelle
   direzioni in cui finisce la linea (la linea resta come tracciata): un tratto del muro che la pianta non mostra
   bene, o il terreno ai suoi capi.
4. **Prospetto.** *Risoluzione* (automatica: il GSD), *Fusione*, *Correzione colore*, *Senza le foto in controluce*,
   *Nome* (predefinito "Prospetto del muro"), poi **Costruisci il prospetto**. Viene elencato nell'area di lavoro come
   ogni altro prospetto e mostrato nella vista raster; *Esporta per la pubblicazione...* è descritto
   [più sotto](#finitura-per-una-pubblicazione).
5. **Altre campagne.** Lo stesso muro negli altri chunk: vedi [più sotto](#lo-stesso-muro-in-piu-campagne).

La linea resta con il suo chunk e il suo progetto: un altro chunk reso attivo, o un altro progetto, svuota il pannello.

**`u` corre lungo la faccia.** Su un prospetto di un muro tracciato `u` è la **lunghezza misurata sulla faccia del
muro**, dal primo punto (o dall'inizio del prolungamento) da sinistra a destra per chi guarda, e `v` è la **quota**.
Un muro che si incurva, o la cui linea è stata cliccata un po' fuori, è lungo sul prospetto quanto sul terreno: la
faccia è misurata in colonne di 1 cm, una colonna è della faccia dove il modello è incontrato ad almeno il 10 % delle
sue quote, e una ruvidità sotto i 5 cm non aggiunge lunghezza. Con `--scale trace` (riga di comando) `u` è invece la
lunghezza lungo la linea. Il tooltip del prospetto nell'area di lavoro dice quale. Un muro lungo la cui linea non si
trova nessuna colonna di faccia viene rifiutato con questa regola, nel pannello, e non si costruisce nulla.

**Com'è uno spigolo.** A ogni vertice la linea gira lungo un piccolo arco, così la direzione di vista gira senza
salti. A uno **spigolo convesso** (il fuori di un edificio) la vista si apre a ventaglio intorno allo spigolo: le
pietre dello spigolo si vedono in una fascia continua, senza vuoti. A uno **spigolo rientrante** (il dentro di una
stanza) nulla si ripiega: nessuna pietra si vede due volte. L'arco è grande quanto la profondità davanti più 2 cm,
così uno spigolo rientrante tiene tutta la profondità davanti; a una svolta stretta è più piccolo. La fascia di
profondità finisce dove due parti della linea sono ugualmente vicine - il mezzo di una stanza stretta, il centro di un
arco - 2 cm prima di quel posto: quello che sta oltre appartiene all'altra parte del muro e non viene preso due volte.
Gli spigoli sono contati nel risultato ("corners: 2") e segnati sulla scheda di confronto.

**Disegni attraverso uno spigolo.** Unità, quote e gli altri disegni si disegnano sul prospetto come su ogni altro,
nei suoi metri (u, v), e possono passare uno spigolo. Quando sono esportati in 3D (il `_disegni_mondo.gpkg`, il DXF
3D, il collegamento dal vivo con QGIS o RustGIS) ogni vertice viene messo sulla superficie del muro al suo posto, il
contorno infittito ogni 2 cm (o due volte la risoluzione): un'unità attraverso uno spigolo lo gira, e in pianta cade
sul muro dove è stata disegnata. Un buco del rilievo fino a 0,25 m lungo il muro è scavalcato con le profondità ai
due lati. *Disegni da un altro prospetto...* su un muro tracciato sposta un disegno solo dove cade entro 5 cm (o tre
pixel, quando sono di più) dalla superficie di quel muro; gli altri restano dove sono, e il log lo dice.

**Il layer *Tracce dei prospetti*.** La pianta mostra la traccia di ogni prospetto del chunk - muri tracciati, piani e
cilindri; le sezioni hanno il loro layer - come una linea con il nome del prospetto, solo sulla pianta, mai su un
prospetto. Un doppio clic su una traccia apre il suo prospetto. Il layer va a QGIS con il collegamento dal vivo, in un
verso solo, con i campi `etichetta`, `prospetto` (il nome), `prospetto_id`, `tipo` (`plane`, `cylinder`, `path`) e
`lunghezza_m`; la traccia tiene gli spigoli vivi della linea, non i suoi archi. Il GeoPackage 3D di un prospetto
contiene anche la sua traccia, come tabella `traccia`, a metà altezza.

#### Lo stesso muro in più campagne

Un muro rilevato in più anni, ogni anno un chunk dello stesso progetto già messi insieme (legami fra chunk, gli stessi
marker, lo stesso sistema di riferimento), si costruisce su **una griglia sola**: *Stesso prospetto su altri chunk...*
(clic destro sul prospetto nell'area di lavoro, oppure *Altre campagne* nel pannello) elenca gli altri chunk con un
modello, spuntati quelli nello stesso sistema di riferimento, con una **data** per ciascuno (*AAAA-MM-GG*: la data del
chunk, se no la data di mezzo delle sue foto; anche le date delle campagne già nel gruppo si cambiano lì). Ogni chunk
spuntato riceve il prospetto sullo stesso sistema, griglia, limiti, risoluzione, profondità e impostazioni del mosaico
(come `--like`), così le ortofoto stanno sugli stessi pixel. I prospetti fatti restano insieme come il **gruppo** di
quel muro; il tooltip di ciascuno dice il gruppo, quante campagne contiene e la sua data.

Un chunk viene rifiutato, con il motivo, quando è già nel gruppo, non ha un modello, o è in un altro sistema di
riferimento (sono nominati tutti e due). Due chunk in un sistema **LOCAL** hanno mondi propri: la finestra chiede se
stanno nello stesso sistema (allineati, uniti, orientati sugli stessi marker); se no, non si costruisce nulla. Un
chunk dove il muro non si trova viene detto vuoto. Quello che è stato costruito prima di un errore resta.

#### La scheda di confronto

*Scheda di confronto...* (su un prospetto di un gruppo, oppure nel pannello) scrive un'immagine (PNG, o JPEG) e un PDF
accanto: le campagne **una sopra l'altra sullo stesso asse u**, la più vecchia in alto, ognuna con il suo nome e la sua
data; le quote ogni 0,5 m a sinistra di ogni fascia, le distanze lungo il muro, gli **spigoli** della traccia come
linee rosse tratteggiate attraverso tutte le fasce con un triangolo sopra; il titolo è il nome del gruppo o quello
dato. La griglia è quella del prospetto di riferimento del gruppo; una campagna su un'altra griglia (ricostruita a
un'altra risoluzione, o spostata di una parte di pixel) viene lasciata fuori con il motivo. Un muro lungo viene ridotto
di un fattore intero a 6.000 pixel al massimo. Un'immagine o un PDF che esistono già vengono sostituiti solo dopo una
domanda (dalla riga di comando, `--overwrite`).

#### Il composito delle campagne

*Composito delle campagne...* fa un prospetto del muro **com'era quando era più scoperto**: per ogni cella la
campagna più recente, sostituita da una più vecchia dove quella è **più profonda di oltre 0,15 m** (la soglia si può
cambiare) o dove la recente non ha nulla - un tratto poi coperto da sabbia, crolli o un rinterro. I colori delle
campagne più vecchie vengono accordati a quelli della più recente sulle celle che hanno in comune vicino a dove
subentrano (*Accorda i colori...*, attivo per difetto; il cambio è tenuto entro un guadagno fra 0,5 e 2). Il composito
è un prospetto del chunk su cui è stato tracciato il muro (tipo `composite`, con il nome "<muro> <primo anno>-<ultimo
anno>"): si può disegnare, esportare, rifinire e confrontare; un raster `sources.tif` dice per ogni cella da quale
campagna viene, e il prospetto registra la soglia e le sue fonti. Ogni campagna ha bisogno di una data (la finestra
chiede prima quella che manca).

È **da rifare** quando una campagna viene ricostruita, corretta con una patch, o la sua data cambia l'ordine: il
tooltip e la barra di stato dicono "Composito da rifare"; *Rifai il composito* (clic destro sul composito, oppure nel
pannello) lo rifà al suo posto con la stessa soglia e gli stessi colori. Un composito non ha foto: le patch e la
pittura della texture vi sono rifiutate.

#### Finitura per una pubblicazione

*Esporta per la pubblicazione...* (clic destro su qualsiasi prospetto, oppure nel pannello) scrive una copia per un
libro o un poster - **il prospetto resta come misurato**: ortofoto e rilievo nel progetto non vengono toccati. In
ordine:

* **Riempi i buchi**: un buco largo fino a *Buco più grande da riempire* (0,5 m) viene riempito con colore e trama dai
  bordi; uno più grande solo dove il rilievo vi ha superficie (colore mancante su un muro misurato); uno più grande
  senza superficie è un'**apertura** - una porta, una finestra - e resta aperto. Il messaggio dice quante aperture sono
  rimaste aperte.
* **Fuori dal muro bianco**: quello che sta fuori dall'estensione del muro (il cielo sopra la cresta, frammenti di
  terreno) bianco in un JPEG, trasparente in un TIFF.
* **Uniforma il colore**: i cambi lenti di luce lungo il muro vengono tolti (Lab, su circa 250 pixel). Uniforma anche
  l'ombreggiatura vera fra parti del muro che guardano la luce in modo diverso: toglilo quando conta.
* **Copri le mire**: i marker che stanno sul prospetto, 18 cm intorno a ciascuno.

*Un pixel ogni* N riduce l'immagine. Il file è un JPEG (`.jpg`, bianco fuori) o un GeoTIFF (`.tif`, trasparente fuori,
nei metri del prospetto) e va scritto fuori dalla cartella dei dati del progetto.

#### Tracciamento automatico

Oltre ai clic, il passo *Linea del muro sulla pianta* ha due modi di trovare la linea sul **DEM**:

* **Segui il muro fra due clic**: clic sui due capi del muro (e, se serve, su punti per cui passa, in ordine), doppio
  clic per finire. La linea segue la cresta del muro sul rilievo locale del DEM (e sui bordi dell'ortofoto) ed è
  mostrata come proposta: "Linea proposta: N vertici, L m, P % sulla cresta". Con meno di metà sulla cresta è debole,
  e il pannello lo dice: **Aggiungi punti di passaggio** (un clic dove passa il muro; la linea viene cercata di nuovo
  passando di lì), oppure spunta **Usa SAM dove il muro è basso** (SAM 2.1 ritaglia il muro sull'ortofoto a partire
  dai clic, quando è installato). **Tieni** fa della proposta la linea del muro, con il suo aggancio proposto e le sue
  quote (il lato viene chiesto quando le foto stanno da tutte e due le parti).
* **Proponi i muri**: le creste dei muri di tutta la pianta (un rilievo locale oltre 0,3 m, lunghe almeno 3 m), come
  linee tratteggiate nel layer *Muri proposti*; un clic su una ne fa la linea del muro, controllata e con l'aggancio
  proposto come se fosse stata cliccata.

La linea seguita corre sulla cima del muro e taglia uno spigolo interno attraverso la cima: l'aggancio proposto rimette
la linea sulla faccia e lo spigolo al suo posto. Controllalo sulla pianta e spunta *Aggancia alla faccia* prima di
costruire.

#### Limiti

* La linea è una linea **sulla pianta**: niente tracce in 3D, niente tracce inclinate. `v` è sempre la quota; un muro
  a scarpa è un cambio di profondità, una cresta in pendenza va bene.
* Una **linea chiusa** (una stanza tracciata tutt'intorno) viene distesa come una striscia dal primo punto fino a
  tornarci: i due capi si incontrano in uno spigolo vivo, senza arco, e la striscia non si chiude ad anello. Una
  torre tonda o un pozzo sono un cilindro.
* Un orientamento del chunk che lo **inclina** (una nuova verticale) viene rifiutato finché ha prospetti di muri
  tracciati, con i loro nomi: toglili e traccia di nuovo i muri dopo. Una rotazione intorno alla verticale, uno
  spostamento e una scala li muovono con il chunk.
* Su un muro tracciato non si fa una sezione: si costruisce il prospetto (la finestra lo dice).
* L'aggancio (e la sua proposta) ha bisogno di un modello lungo la linea; la linea seguita e le proposte di un DEM.

#### Dalla riga di comando

```bash
ppg elevation scavo.ppg --path "X1,Y1 X2,Y2 X3,Y3" [--side auto|left|right|X,Y] [--extend 5,5]
    [--heights V0,V1] [--snap] [--snap-band 2.5] [--smooth 0] [--scale wall|trace]
    [--front 0.5] [--back 0.5] [--resolution R] [--skip-backlit] [--name NOME] [--chunk CHUNK]
ppg elevation scavo.ppg --like ID --chunks "2019,2024" [--dates "2019=2019-06-10,2024=2024-07-02"] [--same-frame]
ppg elevation scavo.ppg --like ID --compare confronto.png [--title T] [--overwrite]
ppg elevation scavo.ppg --like ID --composite [--deeper 0.15]
ppg elevation scavo.ppg --like ID --finish prospetto.jpg [--no-fill] [--no-mask] [--no-level] [--no-targets]
    [--reduce N] [--max-hole 0.5]
```

* `--path`: la linea, due punti almeno, nel sistema di riferimento del chunk, separati da spazi. Stampa
  `NOME: W x H px at R m/px; v = height; L m along the wall; corners: N` e, se agganciata, di quanto si sono spostati
  i vertici. `--side` prende una parola o un punto dal lato di chi guarda (anche con `--trace`); `--extend A,B` metri
  prima del primo punto e dopo l'ultimo; `--heights` le quote (predefinite: quelle del muro sul modello); la linea
  si usa come data; `--snap` (spento all'inizio) costruisce sulla linea agganciata alla faccia, `--snap-band` (con
  `--snap`) e `--smooth` come nel pannello; `--scale wall` (predefinito) o `trace`.
  `--section` è rifiutato con `--path`, e le opzioni del percorso senza.
* `--chunks` (con `--like`, non con `--chunk`): una riga per chunk - costruito, rifiutato con il motivo, o vuoto - e
  una per il gruppo; codice di uscita 1 quando non si è costruito nulla. `--dates` dà la data alle campagne.
* `--compare` non costruisce nulla e stampa l'immagine e il PDF, e le campagne lasciate fuori; `--title` il titolo;
  senza `--overwrite` i file che ci sono restano e il comando fallisce.
* `--composite` fa (o rifà) il composito del gruppo di `--like`; `--deeper` la soglia in metri.
* `--finish` esporta il prospetto `--like` rifinito: `--no-fill`, `--no-mask`, `--no-level`, `--no-targets` tolgono
  un passo, `--reduce N` un pixel ogni N, `--max-hole` il buco più grande riempito dappertutto.
* `--skip-backlit` va con una costruzione, non con `--like` (vale l'impostazione di quel prospetto).
* Un prospetto che non è in un gruppo dà un errore con `--compare` e `--composite`. Queste opzioni vogliono `--like`;
  `--chunks`, `--compare`, `--composite` e `--finish` non vanno insieme.

### Disegni sui prospetti e sulle sezioni

Ciò che si disegna mentre è mostrato un prospetto o una sezione - unità (US, USM), quote, punti di misura, pietre, linee -
appartiene a quel prospetto, nei suoi metri (u, v), e non compare mai sulla pianta; ciò che si disegna
sull'ortofoto non compare mai su un prospetto. Aprendo la pianta o un altro prospetto i disegni di quello lasciato
restano con lui e tornano con lui, anche quando il progetto viene riaperto. *Modifica > Annulla* riporta indietro
una modifica sul prospetto in cui è stata fatta.

Gli strumenti di disegno sono quelli della pianta - disegno, *Modifica i vertici*, rimodella, elimina, layer,
attributi, snap, annulla e ripeti - e lavorano sui disegni del prospetto mostrato. Nell'**area di lavoro** ogni
prospetto elenca sotto di sé i suoi disegni (*Unità stratigrafiche (n)*, *Quote (n)*, *Punti di misura (n)*, *Linee
(n)*, *Pietre (n)*); un doppio clic mostra il prospetto.

* Una **quota** cliccata su un prospetto prende la quota della superficie in quel punto (la Z del punto del
  modello, letta attraverso il rilievo) e la sua posizione in pianta; sul prospetto mostra quella quota. Un **punto
  di misura** legge i prodotti del chunk in quello stesso punto della superficie, e un **marker** vi viene messo.
* Gli strumenti che lavorano sulla pianta - traccia di sezione, ritaglio, misura area, modifica del DEM, foto
  di un'area, regione, tavola dei colori - sono rifiutati mentre è mostrato un prospetto, con un messaggio nella
  barra di stato: apri l'ortofoto o il DEM.
* Le **pietre** trovate con SAM su un prospetto sono sue (ogni prospetto ha il suo layer *Pietre*).
* I **progetti fatti prima della 1.7.0** tenevano ciò che era disegnato su un prospetto tra i disegni della pianta,
  nei metri del prospetto, e le pietre di tutti i prospetti sotto un solo nome. Quando un progetto così viene aperto,
  ognuno di quei disegni che sta fuori dalla pianta (le sue foto, i marker, l'ortofoto e il DEM) e dentro
  l'estensione di un solo prospetto passa a quel prospetto (una quota prende il suo punto sulla superficie); quello
  che sta su più prospetti, e tutto in un rilievo locale in cui pianta e prospetti hanno gli stessi numeri, resta
  dov'era. Non si cancella niente, e il file cambia solo quando il progetto viene salvato.
* **Vista > Disegni dei prospetti nella vista 3D** (attivo all'inizio) mostra i disegni di ogni prospetto sul suo
  piano nella vista 3D, in azzurro (da guardare: si modificano sul prospetto). Sulla pianta non compaiono mai.
* **Disegni da un altro prospetto...** (clic destro su un prospetto) copia, o sposta, i disegni di un altro prospetto
  del chunk su questo attraverso i loro punti sul rilievo: un prospetto rifatto su un altro piano prende i disegni
  di quello vecchio. Un passo di *Modifica > Annulla*.
* **Rimuovendo** un prospetto (*Rimuovi*) se ne vanno anche i suoi disegni; *Modifica > Annulla* li riporta insieme.
  Un prospetto ricostruito (*Ricostruisci*) tiene i suoi disegni; scalando, orientando o cambiando il riferimento del
  chunk il prospetto e i suoi disegni si spostano insieme.
* **Esporta** (clic destro sul prospetto) scrive, oltre a ortofoto, rilievo, piano e profilo, i suoi disegni:
  `<nome>_disegni.dxf` nei metri del prospetto (layer US, USM, QUOTE, PIETRE, PUNTI e PROFILO per una sezione),
  `<nome>_disegni.svg` in scala sopra l'ortofoto, `<nome>_disegni.png`, e due GeoPackage: `<nome>_disegni.gpkg`, il
  prospetto **sviluppato** - i disegni in (u, v), senza sistema di riferimento, con la tabella `frame` (il piano) e
  una colonna `geom_world` -, che QGIS e RustGIS caricano sopra `<nome>_ortofoto.tif` come una pianta della
  facciata; e `<nome>_disegni_mondo.gpkg`, i disegni **in 3D sul rilievo** (PolygonZ, PointZ, LineStringZ nel
  sistema di riferimento del chunk), che QGIS 3D e RustGIS collocano dove sono; e `<nome>_disegni_3d.dxf`, gli
  stessi disegni come polilinee e punti 3D nelle coordinate del rilievo.
* **QGIS > Apri il prospetto in QGIS** (con un prospetto mostrato) apre un progetto QGIS del prospetto sviluppato:
  la sua ortofoto e i suoi disegni, nei suoi metri, un layer per tipo, senza sistema di riferimento. Ciò che lì si
  disegna, sposta, cambia o cancella in quei layer torna con **QGIS > Rileggi il prospetto da QGIS** (prima salva i
  layer in QGIS), come un passo di *Modifica > Annulla*; ciò che nel frattempo è stato cambiato qui e non in QGIS
  resta. Riaprendo il prospetto in QGIS quelle modifiche vengono lette prima, così non vengono mai sovrascritte. È
  una lettura a richiesta, non un collegamento dal vivo.
* **Salva in PyArchInit** con un prospetto mostrato scrive le schede US delle sue unità (create o aggiornate, solo
  i campi: un disegno su un prospetto non ha geometria in pianta, e il messaggio lo dice), le quote in
  `pyarchinit_quote` nella loro posizione in pianta, e registra i suoi disegni (DXF e SVG) come media del sito,
  "Prospetto" o "Sezione" in `documentazione_table`, collegati a quelle US: sempre gli stessi media, i loro file e
  le miniature riscritti a ogni salvataggio e le US aggiunte nel frattempo collegate.
* Il **collegamento dal vivo** con QGIS o RustGIS porta, oltre ai disegni della pianta, quelli di ogni prospetto con
  disegni, in 3D sul rilievo (tabelle `prospetto_<id>_unita`, `_quote`, `_punti`, `_pietre`, `_profilo`; in RustGIS
  nel gruppo "<chunk> - prospetti"), in un solo senso: ciò che si modifica lì in quei layer non torna. Un prospetto
  fatto dopo aver creato il progetto QGIS non c'è finché il progetto non viene creato di nuovo (*QGIS > Apri in
  QGIS*). Il pacchetto **QField** porta solo la pianta.
* I disegni della pianta restano caricati qualunque cosa sia mostrata: le funzioni che lavorano sulla pianta
  (proxy, volumi, classi, IFC, proposta di US, PyArchInit, fasi, rapporto) usano sempre i disegni della pianta.

### Patch sui prospetti

Lo strumento *Patch* funziona anche su un prospetto o una sezione, come sull'ortofoto (vedi
[Patch dell'ortofoto](#patch-dellortofoto)): l'area si disegna sul prospetto, nei suoi metri (u, v), e la finestra
propone le foto che la inquadrano lì, le migliori prima. Cambia questo: la superficie è il rilievo del prospetto,
quindi non c'è *Sul suolo attorno all'area* né la texture del modello; una patch appartiene al suo prospetto
(conservata nella sua voce, elencata nel layer *Patch ortofoto* finché è mostrato) e non tocca mai la pianta né un
altro prospetto. *Applica* ridisegna solo le celle della patch; *Elimina* sulla patch la toglie e le celle tornano
com'erano; *Modifica > Annulla* copre entrambe. Casi tipici: una foto con la parete bruciata dal sole, esclusa su una
fascia; una buona foto assegnata dove la mesh è sbagliata e la foto migliore per quelle celle è quella sbagliata.
Le patch restano a quel prospetto: un nuovo prospetto costruito sulla stessa parete parte senza (a differenza
dell'ortofoto, le cui costruzioni successive applicano di nuovo le sue patch).
Dalla riga di comando: `ppg patch scavo.ppg --elevation "Parete sud" --suggest --bbox ...`.

## Modificare il DEM

*Modifica DEM* in *Strumenti > Disegno su ortofoto* (trascina un riquadro o fai clic su un poligono), oppure
*Modifica il DEM in quest'area* nel menu di un poligono selezionato: le quote dentro l'area tornano dal terreno appena fuori - il
piano che lo adatta, un'interpolazione lineare, IDW o una quota costante - e le linee selezionate possono fare da
linee di discontinuità (il bordo di un muro o di un gradino tiene le sue quote). Alberi, tende, mezzi, cumuli di
terra o buchi della ricostruzione lasciano segni in curve di livello e volumi; il DEM com'era resta tra le versioni
(confrontalo, rendilo di nuovo attivo).

## Vuoti del DEM

*Strumenti > Interpola i vuoti del DEM...* (o i due campi *Interpola i vuoti* di *Costruisci DEM*): i buchi senza
quote circondati da quote - una pozzanghera, un'ombra, un vuoto della ricostruzione - ricevono quote dai loro
bordi, la superficie liscia che raccorda il bordo (interpolazione armonica: un piano resta un piano, nessuna quota
sale sopra o scende sotto quelle del bordo). Si riempiono solo i buchi fino alla **larghezza** (m, la parte più
larga) e all'**area** (m2) scelte; i vuoti più grandi, il terreno vuoto attorno al rilievo e le insenature aperte
verso di esso restano vuoti: niente viene estrapolato fuori dai dati. Il DEM è letto e scritto a finestre, così un
DEM grande resta nella memoria.

Le celle interpolate restano segnate in una maschera tenuta col DEM (`dem_interpolated_<id>.tif` nella cartella del
chunk, 1 = riempita tra i punti alla costruzione del DEM, 2 = vuoto riempito): la quota sotto il cursore e quella di
un punto di misura dicono *(interpolata)* (i punti di misura hanno il campo `z_interp`), una quota letta lì lo dice
nella barra di stato, *Esporta DEM* scrive `<nome>_interpolata.tif` accanto al DEM e il report dà la quota di
celle interpolate. *Modifica > Annulla* e le versioni riportano il DEM com'era, con la sua maschera. Non è il
riempimento dei buchi del modello (*Campo di altezze* con i buchi chiusi, `ppg process --fill-holes`) né i piccoli
buchi dell'ortofoto. Da riga di comando: `ppg dem-fill-voids scavo.ppg --max-width 1`,
`ppg dem scavo.ppg --fill-voids 1`.

## Coregistrazione dei DEM

*Strumenti > Coregistra DEM...*: il DEM del chunk attivo viene spostato su un riferimento - il DEM di un altro
chunk o una versione conservata di un DEM qualunque - dove il terreno non è cambiato: spostamento orizzontale
(Nuth e Kääb), verticale e, se spuntato, inclinazione.

* Le celle cambiate (lo scavo, i cumuli di terra) vengono trovate e lasciate fuori da sole; con dei poligoni
  selezionati la finestra può anche escluderli (*Escludili*: le aree scavate) o usare solo il terreno dentro di
  essi (*Solo dentro*).
* I due DEM possono stare a metri di distanza, come due rilievi georeferenziati con GPS diversi: prima si
  confrontano le pendenze dei due a ogni spostamento per trovare all'incirca dove sta il secondo, poi
  l'adattamento lo affina; lo spostamento trovato così viene tenuto solo se lì va d'accordo molto più terreno in
  pendenza.
* Lavora in background; il log dà dx, dy, dz in cm, l'inclinazione e la NMAD sul terreno stabile prima e dopo. Il
  DEM spostato diventa quello attivo, e il DEM com'era resta tra le versioni (*Differenza con il DEM attivo*
  mostra cosa si è spostato).

!!! warning "Quando non può funzionare"
    Su un terreno che si ripete (arature, filari di viti) o uguale lungo una direzione (un lungo fossato, solchi)
    molti spostamenti vanno bene e la ricerca sopra non si usa. Dove la vegetazione è cambiata tra i rilievi,
    coregistra i DTM (la finestra del DEM con solo *Terreno* spuntato). Su un terreno piano, o dove le pendenze
    guardano tutte da una parte, lo spostamento orizzontale non si può misurare: il log lo dice e lo spostamento
    verticale lo assorbe. Quando i DEM non sono lo stesso terreno, il log dice che lo spostamento non si è
    assestato e viene tenuto lo stato più vicino - di solito il DEM com'è.

Le [fasi dello scavo](archaeology.md#fasi-dello-scavo) coregistrano così i loro DEM prima di calcolare i volumi.

## Patch dell'ortofoto

L'ortofoto prende ogni cella dalla foto migliore; una patch sceglie la foto a mano in un'area (la chioma di un
albero sopra un muro, una persona nel saggio, un'ombra).

1. *Patch* nella barra laterale (gruppo *Automatico*) (o *Strumenti > Patch dell'ortofoto*): trascina un riquadro o fai clic sui vertici
   di un poligono e doppio clic; oppure seleziona un poligono (un'unità) e scegli *Assegna immagini a quest'area*
   dal suo menu.
2. La finestra mostra l'area com'è ora e, per la foto scelta nell'elenco, l'area come verrebbe. L'elenco ha le foto
   che inquadrano l'area, le migliori prima (quota inquadrata x vicinanza, verticalità e centralità), con la quota
   inquadrata, l'angolo dalla verticale, il pixel a terra, la parte dell'area che l'ortofoto ne prende ora, e nel
   suggerimento quanto ne nasconde il modello (chiome, muri).
3. Scegli una o più foto (++shift++ / ++ctrl++), *Usa solo* o *Escludi*, *Sul DEM* (una true ortho) o *Sul suolo
   attorno all'area* (quote lungo il poligono interpolate all'interno: alberi, mezzi e persone vengono come nella
   foto, e i buchi del DEM si riempiono), e a scelta la texture del modello nell'area.
4. *Applica* salva la patch e ridisegna solo le sue celle, poi le panoramiche di quella finestra; la vista resta
   dov'era.

Le patch sono un layer, *Patch ortofoto*: selezionane una e premi ++delete++ (o *Elimina selezionati*) e
l'ortofoto viene ridisegnata lì senza di essa. Una costruzione successiva dell'ortofoto applica di nuovo ogni
patch; *Aggiorna con le patch* applica quelle non ancora applicate (dopo un aggiornamento interrotto).
Un'ortofoto di un altro programma (esportata da Metashape) viene copiata nella cartella del chunk alla prima
patch, e i suoi pixel tornano dove una patch viene tolta.

**Patch della texture**: nella vista 3D il lazo sul modello propone *Correggi la texture di queste facce (assegna
le immagini)*: le foto che vedono quelle facce, ognuna con la parte della foto dove si trovano; ogni faccia prende
la migliore delle foto scelte che la vede.

## Elaborazione batch

![La finestra Elaborazione batch](../img/gui/dialog-batch.png)

*Flusso di lavoro > Elaborazione batch...*: un elenco di passi - allineamento, prodotti, esportazioni, la
precisione e il controllo sul campo - ognuno su tutti i chunk o su quelli scelti, con i suoi parametri (*Aggiungi*
per gruppo, *Modifica* o doppio clic, *Su* / *Giù*, *Carica* / *Salva* come JSON), i tentativi per un passo che
fallisce e cosa fare allora.

Il passo *Modello 3D* ha lo stesso **Chiudi i buchi fino a** (metri, 0 = nessuno) di
[*Costruisci modello*](#modello): chiude i buchi solo nel *campo di altezze*, e il log dice quanti buchi sono
stati chiusi e quanti metri quadrati in pianta sono interpolati, non misurati. Un file di batch salvato prima che
il campo esistesse si legge con 0: non si chiude niente.

Il progetto viene salvato dopo ogni passo; se il batch si ferma (mancanza di corrente, arresto improvviso,
*Annulla*) la volta dopo propone di ripartire da dove si era fermato. I passi falliti sono elencati nel log.

## Nodi di rete

![La finestra Nodi di rete](../img/gui/dialog-nodes.png)

*Strumenti > Nodi di rete...*: altri computer che lavorano a questo progetto dalla cartella condivisa del
progetto.

* La finestra dà la cartella da passare a `ppg node` su ognuno, ed elenca i nodi presenti con i loro core e GPU.
* L'interruttore affida loro i punti chiave, le mappe di profondità PatchMatch, i tasselli dell'ortofoto e le celle
  della texture; l'allineamento resta su questo computer. Viene ricordato e applicato al programma in esecuzione
  come `PPG_NODES`.
* `ppg remote` lavora con una macchina via SSH. I dettagli nell'[API](../api.md#altri-computer), *Altri computer*.
