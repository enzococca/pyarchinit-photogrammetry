# pyarchinit-photogrammetry

Motore fotogrammetrico open source per l'archeologia e il BIM dei beni culturali, integrato
con [PyArchInit](https://github.com/pyarchinit/pyarchinit).

Dalle foto a nuvole di punti, mesh, DEM e ortofoto georeferenziati, con gli strumenti che
servono in uno scavo: target codificati, punti di appoggio, sezioni, profili ceramici,
curve di livello, volumi di scavo, classificazione della nuvola, Segment Anything 2.1,
esportazione IFC e salvataggio diretto nel database di PyArchInit. Tutto è utilizzabile da
Python e gira su CPU, anche su Mac Apple Silicon.

> Stato: **alpha (0.5)**. Libreria Python, riga di comando e applicazione desktop (sulla stessa
> API, con console Python integrata).

## Applicazione desktop (senza installare Python)

Scarica la versione per il tuo sistema dalla pagina
[Releases](https://github.com/enzococca/pyarchinit-photogrammetry/releases):

| Sistema | File |
| --- | --- |
| macOS 15 o successivo (Apple Silicon) | `PyArchInit-Photogrammetry-<versione>-macos-arm64.dmg` |
| Windows 10/11 (64 bit) | `PyArchInit-Photogrammetry-<versione>-windows-x64.zip` |

Su macOS apri l'immagine disco e trascina l'app in *Applicazioni*; su Windows estrai la
cartella e avvia `PyArchInit Photogrammetry.exe`. L'applicazione si apre su una pagina con
*Nuovo progetto*, *Apri progetto* e *Progetto demo*: la demo crea un saggio di scavo
sintetico e una ciotola su turntable e li elabora in pochi minuti. Se PyArchInit 5 è
installato (`~/pyarchinit_5`), l'interfaccia si collega al suo database all'avvio e il database
demo viene scritto dentro `pyarchinit_DB_folder`, così lo apre anche QGIS; i server
PostgreSQL/PostGIS si raggiungono da *PyArchInit > Connetti*. Le build non sono firmate: su macOS la prima
volta tasto destro > *Apri* (oppure `xattr -dr com.apple.quarantine "/Applications/PyArchInit Photogrammetry.app"`),
su Windows *Ulteriori informazioni > Esegui comunque*. Dettagli in [docs/packaging.md](docs/packaging.md).
L'applicazione cerca le nuove versioni all'avvio (al massimo una volta al giorno) e, su macOS, le
installa e si riavvia; vedi *Updates* in [docs/gui.md](docs/gui.md).

## Installazione con pip

Usa un ambiente virtuale dedicato, non l'ambiente `base` di Anaconda (vedi *Problemi
frequenti*). Con [uv](https://docs.astral.sh/uv/):

```bash
uv venv --seed --python 3.12 ~/venvs/ppg && source ~/venvs/ppg/bin/activate
uv pip install "pyarchinit-photogrammetry[all]"      # motore, mesh (Open3D), IFC, PostGIS, interfaccia
```

Con gli strumenti standard:

```bash
python3 -m venv ~/venvs/ppg && source ~/venvs/ppg/bin/activate   # Windows: ~\venvs\ppg\Scripts\activate
pip install -U pip && pip install "pyarchinit-photogrammetry[all]"
```

Opzionale: `pip install "pyarchinit-photogrammetry[sam]"` aggiunge Segment Anything 2.1 (PyTorch).
Dai sorgenti, `uv pip install ./native` (serve Rust) aggiunge il nucleo nativo `ppg_core`: le parti più
pesanti di depth map, ortofoto e confidenza in Rust su tutti i core (depth map su CPU circa sei volte più
veloci); senza, gira lo stesso codice numpy. Le applicazioni desktop lo contengono già.
Per avere `ppg` e `ppg-gui` sempre disponibili senza attivare nulla:
`uv tool install "pyarchinit-photogrammetry[all]"` (o `pipx install`).

Python 3.10-3.12. Per scrivere geometrie in un database PyArchInit **SQLite** serve
`mod_spatialite`: viene cercato automaticamente nelle installazioni di QGIS, MacPorts,
Homebrew e Linux, oppure si indica con la variabile `SPATIALITE_LIBRARY_PATH`.

## Avvio

```bash
ppg doctor                        # controlla l'ambiente e dice cosa sistemare
ppg demo ~/ppg_demo --process     # saggio sintetico con 8 target, ciotola su turntable, ceramica, database PyArchInit
ppg-gui ~/ppg_demo/scavo.ppg      # apre l'interfaccia sul saggio elaborato
ppg-gui ~/ppg_demo/ceramica/ceramica.ppg   # la ciotola (sistema locale in metri dai target del tappetino)
ppg-gui                           # pagina iniziale: nuovo, apri, demo, progetti recenti
```

`ppg-gui` equivale a `ppg gui` e a `python -m pyarchinit_photogrammetry.gui`. L'interfaccia
scrive un log in `~/Library/Logs/pyarchinit-photogrammetry` (macOS),
`%LOCALAPPDATA%\pyarchinit-photogrammetry\logs` (Windows) o
`~/.local/state/pyarchinit-photogrammetry/logs` (Linux); *Aiuto > Apri cartella dei log* la apre.

## Problemi frequenti

* **`ppg-gui` non fa nulla o si chiude all'avvio.** Esegui `ppg doctor`. Spesso la causa è
  una vecchia copia del pacchetto installata in un altro Python (per esempio l'ambiente `base`
  di Anaconda) che viene prima nel `PATH`: attiva l'ambiente giusto (`which ppg-gui` deve
  puntare al suo interno) oppure rimuovi l'altra copia
  (`/opt/anaconda3/bin/python -m pip uninstall pyarchinit-photogrammetry`).
* **Ambiente `base` di Anaconda**: le sue librerie SciPy possono non caricarsi sui macOS
  recenti (`ImportError ... __thread_bss ... zero-fill section`) e i suoi dati PROJ vengono
  letti da rasterio. Usa un ambiente virtuale dedicato; le variabili `PROJ_LIB`/`PROJ_DATA`/
  `GDAL_DATA` esportate da Anaconda o QGIS vengono reindirizzate ai dati inclusi in rasterio.
* **Il passo "Modello" scrive "Poisson reconstruction failed... height field instead".** La
  ricostruzione Poisson gira in un processo separato perché PoissonRecon (Open3D) può
  interrompersi su alcune nuvole; la pipeline riprova e poi costruisce una superficie 2.5D.
  Open3D 0.20.0 si blocca sistematicamente su macOS ed è escluso (`open3d!=0.20.0`); la 0.19 funziona.
* **Finestre di dialogo native che chiudono l'app su macOS 26/27**
  (`NSInternalInconsistencyException ... targetSizeInPoints`): l'interfaccia usa le finestre di
  messaggio di Qt, non serve fare nulla.
* **La vista 3D si blocca su macOS**: pyvistaqt precedente alla 0.13 si blocca all'avvio;
  `pip install -U pyvistaqt`.

## Lavorare con QGIS, Metashape, WebODM e Photo3D

* **QGIS dal vivo**: *QGIS > Apri in QGIS* apre ortofoto, DEM e tutti i layer disegnati in un
  progetto QGIS; disegni e attributi modificati compaiono in QGIS entro un secondo.
* **Metashape**: *File > Importa > Progetto Metashape* legge direttamente il `.psx` (camere
  calibrate anche nei gruppi, marker, punti di legame di tutte le componenti, mesh anche di
  milioni di facce con anteprima per la vista 3D, DEM e ortofoto esportati) e si prosegue qui
  (nuvola densa, sezioni, PyArchInit, IFC) senza riallineare. Se il progetto non ha una nuvola
  densa, i vertici colorati della mesh diventano la nuvola densa.
* **WebODM/ODM**: la cartella del task o `all.zip` diventa un progetto con ortofoto, DEM,
  nuvola e mesh.
* **Photo3D**: `ppg photo3d-job` è un motore pronto per Photo3D; le sessioni si possono anche importare.

Dettagli in [docs/integrations.md](docs/integrations.md). Nell'applicazione pacchettizzata SAM 2.1
si installa al primo uso (PyTorch in un ambiente separato, stessa pagina).

## Rilievo di scavo, classi della nuvola e SAM

* **Un sito per progetto**: lo scavo si sceglie una volta (*PyArchInit > Sito del progetto* o
  la casella in cima al pannello PyArchInit) e vale per disegni, schede e pubblicazioni; se il
  database di PyArchInit ha più siti, si vedono solo quelli del sito del progetto.
* **US e quote con i campi di PyArchInit**: le unità disegnate hanno le colonne di
  `pyunitastratigrafiche` (sito, area, US, tipo di unità, indice stratigrafico, tipo,
  disegnatore, data, rilievo originale); lo strumento *Quota* (separato da SAM) legge la quota
  sul DEM e compila i campi di `pyarchinit_quote`. Restano salvate nel progetto e si pubblicano
  una sola volta.
* **Segment Anything 2.1**: *Tocco* (clic sull'oggetto), *Riquadro* (un oggetto nel riquadro),
  *Area* (tutti gli oggetti nel riquadro) o tutta l'ortofoto scandita a tasselli sovrapposti;
  la maschera si corregge con altri clic prima di salvarla. PyTorch usa la scheda grafica:
  GPU Apple (Metal) sul Mac, CUDA sulle schede NVIDIA (scelto dall'installazione su Windows e Linux).
* **Classi della nuvola visibili e modificabili**: la vista 3D colora la nuvola per classi con
  la legenda (togli la spunta per nascondere una classe); il lazo assegna la classe giusta a
  gruppi di punti; un classificatore addestrato su queste correzioni (random forest su
  geometria a tre scale, altezza sul terreno e colore) classifica il resto, e il modello
  (`.ppgclf`) si riusa su altri scavi.
* **Le classi guidano i prodotti**: modello del terreno (DTM) dal solo terreno, ortofoto senza
  attrezzi, persone o vegetazione, mesh, sezioni ed esportazioni delle classi scelte, e una
  classe per ogni US con punti, area e quote (anche in CSV).
* **Versioni dei prodotti**: rigenerando nuvola densa, mesh, DEM e ortofoto le versioni
  precedenti restano nell'area di lavoro; si mostrano, si rendono attive o si confrontano
  (differenza tra due DEM).
* **Mesh dalle mappe di profondità** (fusione TSDF, come in Metashape), **ottimizzazione delle
  camere sui punti di controllo** (compensazione vincolata ai GCP), **allinea e unisci chunk**
  (con i marker, le foto in comune o le nuvole che si sovrappongono).

## Pulizia, confidenza e ritaglio

* **Confidenza**, come in Metashape: quante mappe di profondità confermano ogni punto della
  nuvola densa e ogni vertice del modello. La nuvola fusa la ha subito; *Flusso di lavoro >
  Pulizia e confidenza > Calcola la confidenza* la aggiunge a qualsiasi altra nuvola (ritagliata,
  unita, importata) e al modello. La vista 3D colora per confidenza con una legenda le cui righe
  nascondono una fascia; i file PLY e LAS la conservano (CloudCompare la legge come campo scalare).
* **Pulire la nuvola densa**: confidenza bassa, punti isolati, punti senza vicini, piccoli gruppi
  staccati dalla superficie (uccelli, polvere, cielo), classi scelte o un colore; i punti trovati
  sono mostrati in rosso nella vista 3D prima di toglierli.
* **Pulire il modello**: facce che nessuna mappa ha visto (confidenza dei vertici), parti
  staccate, triangoli stirati, topologia rotta, buchi chiusi fino a un raggio, levigatura che
  lascia fermi i bordi, semplificazione.
* **Selezione graduale dei punti di legame**: errore di riproiezione, numero di foto, incertezza
  di ricostruzione e accuratezza di proiezione, con i conteggi mentre cambi le soglie, poi
  l'ottimizzazione delle camere.
* **Lazo** nella vista 3D: dare una classe ai punti, eliminarli o tenere solo quelli; sul
  modello, eliminare o tenere facce.
* **Ritaglio con un riquadro o un poligono** disegnato sull'ortofoto o sul DEM (o un'unità
  selezionata): DEM, ortofoto, modello e nuvola densa esportati ritagliati in una cartella (con
  il poligono in GeoJSON) oppure ritagliati nel progetto, con gli originali conservati come
  versioni. I file di altri programmi (DEM e ortofoto esportati da Metashape a cui punta un
  progetto importato) non vengono mai modificati.
* **Foto tenute «solo online»** (segnaposto di Dropbox, iCloud, OneDrive): ogni passo che legge
  le foto le elenca e propone di collegarle a un'altra copia (*Flusso di lavoro > Ricollega
  foto*, per esempio la cartella della missione su un disco esterno) o di procedere senza.
* **Texture leggibili**: per i rilievi dall'alto la texture del modello è la sua ortofoto, con
  spazi propri per pareti e sezioni (mappatura a ortofoto adattiva, come in Metashape; scelta da sola
  per i modelli grandi rivolti verso l'alto); colori e texture restano dopo pulizia, ritaglio e
  calcolo della confidenza. L'import di Metashape porta anche la texture del modello; *File > Importa >
  Modello e texture dal progetto Metashape* la aggiunge ai progetti importati prima.
* **Vista 3D e area di lavoro**: punti piccoli con la dimensione regolabile, milioni di punti con la
  scheda grafica; accanto a ogni prodotto, come in Metashape, punti, facce o pixel tra parentesi.
* **Ortofoto grandi**: costruite a tasselli, in parallelo, con le piramidi (overview); un'ortofoto
  di 45.000 x 46.000 pixel di un tell a 1,35 cm/px richiede 4-6 minuti su un Apple M4 Pro. Le celle
  viste solo di traverso (oltre 70 gradi dalla verticale, regolabile) restano trasparenti invece di
  stirare le foto.
* **Video e qualità delle immagini**: fotogrammi scelti da un video (nitidi, con la sovrapposizione
  che serve alla ricostruzione), con il GPS dei droni DJI dal loro `.SRT`; foto sfocate trovate e
  disattivate.
* **Annulla e ripeti** per disegni, quote, sezioni, marker, barre di scala, patch, regione e le modifiche
  della nuvola densa, del modello e dei punti di legame.
* **Elaborazione batch**: passi su più chunk (allineamento, prodotti, esportazioni), il progetto salvato
  dopo ogni passo; un batch interrotto riparte da dove si era fermato, i passi falliti vengono ritentati.
* **Controllo prima di lasciare il sito**: subito dopo l'allineamento, la mappa delle aree viste da
  meno di tre foto, le foto non allineate o sfocate, i marker in troppo poche foto.
* **Precisione**: deviazione standard di camere, marker e punti di legame dalla covarianza del bundle
  adjustment (con l'accuratezza dei punti di controllo), mappa della precisione delle quote del DEM,
  quote con il loro ± in PyArchInit e nell'IFC.
* **Foto 360° e obiettivi fisheye**: foto equirettangolari divise nelle facce di un cubo e allineate
  come un rig (un solo centro, rotazioni note) - tombe, grotte, cisterne e trincee strette riprese con
  una camera 360°; obiettivi fisheye riconosciuti dall'EXIF e calibrati con il loro modello.
* **Regione di ricostruzione**: un riquadro attorno al soggetto (dai punti di legame, da un poligono in
  pianta o con valori scritti, ruotato come la vista); depth map, nuvola densa, modello, DEM e ortofoto
  ne restano dentro.
* **Modifica del DEM**: un'area disegnata sul DEM riempita dal terreno attorno (piano, lineare, IDW o
  quota costante, linee di discontinuità rispettate): alberi, tende e cumuli fuori da curve e volumi;
  il DEM com'era resta come versione.
* **Prospetti**: ortofoto e rilievo di un muro sul piano verticale di una traccia di sezione o di tre
  marker, sviluppo di un pozzo o di una torre su un cilindro; ciò che sta davanti al muro si può
  escludere.
* **Maschere delle foto**: disegnate sulla foto, importate (anche dal canale alfa), dalle foto dello
  sfondo, da un colore del fondale, dal modello o con SAM 2.1; le parti mascherate non servono per
  allineare, nelle depth map, nell'ortofoto e nella texture. Nell'allineamento si possono escludere i
  punti fermi (turntable con fotocamera fissa).
* **Niente cuciture**: l'ortofoto prende i dettagli di ogni cella dalla foto migliore e sfuma le
  differenze di luce lungo le cuciture (fusione a due bande); l'esposizione di ogni foto e la
  vignettatura di ogni obiettivo si misurano sui punti visti da più foto e si correggono
  (*Strumenti > Calibra i colori delle foto*, anche nella texture); si possono togliere persone e mezzi
  visti da poche foto e riempire i piccoli buchi.

Da terminale: `ppg confidence`, `ppg clean-cloud`, `ppg clean-model`, `ppg clean-tie-points`,
`ppg calibrate-colours`, `ppg crop`, `ppg relink`, `ppg export --bbox/--wkt` (vedi `--help`).

## Ortofoto a piena risoluzione e patch

* **Piena risoluzione nella vista**: un'ortofoto o un DEM grandi si aprono da una copia ridotta (una
  delle loro piramidi) e, ingrandendo, la parte visibile viene riletta dal file alla risoluzione dello
  schermo, fino a quella piena: l'ortofoto di 45.000 pixel di un tell si apre in meno di un secondo e
  mostra i suoi pixel da 1,35 cm in un terzo di secondo.
* **Assegnare le foto a un'area** (patch, come in Metashape): disegna un'area sull'ortofoto (*Patch*
  nella barra di disegno: trascina un riquadro o clicca un poligono) oppure usa un poligono
  selezionato. Le foto che la inquadrano sono elencate dalla migliore, ognuna con l'area come
  verrebbe, l'angolo dalla verticale, il pixel a terra e quanto dell'area l'ortofoto prende ora da
  lei. Usa solo le foto scelte, oppure tutte tranne quelle; sul DEM (ortofoto vera) o sul suolo
  attorno all'area: alberi, veicoli e persone vengono come li mostra la foto invece dei vortici su un
  DEM che non li segue, e i buchi del DEM dentro l'area si riempiono.
* Si ridisegnano solo le celle della patch, con le piramidi di quella finestra (2 secondi sul tell,
  invece di un minuto per tutte le piramidi). Le patch restano nel progetto (layer *Patch ortofoto*:
  selezionane una e premi Canc per toglierla) e valgono per ogni costruzione successiva; un'ortofoto
  di un altro programma viene prima copiata, e togliendo una patch tornano i suoi pixel.
* **Anche la texture**: la stessa patch può correggere la texture del modello nell'area, e il lazo
  nella vista 3D propone *Correggi la texture di queste facce* (ogni faccia dalla migliore delle foto
  scelte che la vede).

Da terminale: `ppg patch PROGETTO --bbox ... --suggest` elenca le foto di un'area, `--photos A,B`
(con `--exclude`, `--ground`, `--texture`) aggiunge una patch, `--list`, `--remove ID|all`.

## Barre di scala

* **La distanza tra due marker mette in scala tutto il chunk**, come le barre di scala di
  Metashape: seleziona due marker (target rilevati nelle foto o messi a mano) nel pannello
  *Riferimenti*, tasto destro > *Crea barra di scala tra A e B...* (oppure *Barra di scala...*),
  scrivi la distanza in metri e aggiorna. Non servono coordinate: un oggetto sul turntable, un
  reperto, un muro o uno scavo senza GNSS vengono messi in scala in coordinate locali.
* **Marker dove li mette Metashape**: un marker misurato su una sola foto sta dove il suo raggio
  incontra la superficie (nuvola densa o punti di legame), quindi ha subito le coordinate, si vede
  sul modello e sull'ortofoto e può fare da estremo di una barra di scala; misurato su una seconda
  foto viene triangolato. Un marker si può anche aggiungere senza foto, come *Aggiungi marker* di
  Metashape: *Marker* nella vista 3D (clic sul modello o sulla nuvola) e nella barra di disegno
  (clic sull'ortofoto, quota dal DEM); viene proiettato sulle foto che lo vedono. *Coordinate
  stimate* nel pannello *Riferimenti* mostra le X, Y, Z stimate dei marker (si accende da solo
  quando non hanno coordinate di riferimento).
* **Posizionamento guidato, come in Metashape**: un marker messo su una foto (o in 3D) riceve le
  bandierine blu, la sua posizione stimata, sulle altre foto che lo vedono, e il pannello *Foto*
  si filtra su di lui (segni: verde = misurato, blu = da confermare). Doppio clic apre una foto
  centrata sul marker: trascina la bandierina blu sul punto (diventa verde) o fai doppio clic se è
  già giusta; Pagina giù/su passano alle altre foto. Le foto si filtrano per marker (tasto destro
  in *Riferimenti*), per punto (tasto destro sull'ortofoto, *Foto del punto* nella vista 3D) o per
  area (un poligono selezionato). Le bandierine blu non sono misure: con due verdi il marker viene
  triangolato da quelle. *Affina le bandierine blu* trova il punto di ogni bandierina blu nella sua
  foto per correlazione d'immagine sulla superficie attorno al marker (a una frazione di pixel): un
  marker messo a mano diventa misurato in tutte le foto che lo mostrano chiaramente.
* **Più barre**: ogni barra di controllo chiede una scala e le barre si mediano, pesate con
  l'accuratezza di ciascuna distanza. Una barra di **check** non scala: misura quanto il modello
  si discosta dalla distanza vera. La tabella riporta per ogni barra la distanza data, quella
  misurata sul modello e l'errore; il riepilogo l'RMSE delle barre di controllo e di quelle di
  check, e la vista 3D disegna le barre (controllo in azzurro, check tratteggiate in arancione)
  con lunghezza ed errore.
* **Foto con GPS** (telefono, drone): su un oggetto o uno scavo di pochi metri le posizioni delle
  foto sono un rumore di diversi metri, quindi con le barre di scala non si usano e il chunk resta
  in coordinate locali. Quando le foto coprono un'area molto più grande della loro accuratezza (un
  volo su un sito), o con tre o più punti di controllo, foto, punti e barre si stimano insieme,
  ognuno pesato con la sua accuratezza, come in Metashape: foto e punti posizionano e orientano il
  rilievo e la scala viene da tutti (una barra misurata al millimetro pesa più del GPS di un
  telefono, posizioni RTK su tutto un sito pesano più di una barra corta). Su un chunk
  georeferenziato una barra nuova è proposta come check; una barra di controllo su un chunk
  georeferenziato altrove (un progetto importato) senza riferimenti per ricalcolarlo lo scala
  attorno alla barra, nel suo sistema, e DEM e ortofoto vanno poi ricostruiti.
* **Barre da un file** (*Importa barre...*): una barra per riga, `marker_a, marker_b, distanza[,
  accuratezza[, check]]`, marker per nome o codice del target, distanze in metri. **Barre di scala
  stampabili** (*Strumenti > Target > Barra di scala stampabile...*): due target codificati a
  distanza nota su un foglio, un righello di 10 cm per controllare la stampa e il CSV delle barre
  da importare dopo aver rilevato i target.
* **Nell'ottimizzazione delle camere**, come in Metashape: con due o più barre di controllo (o
  insieme ai punti di controllo) *Ottimizza camere* fissa i marker alle estremità delle barre dove
  le barre hanno la loro lunghezza, ognuno spostato quanto permette la sua precisione nelle foto, e
  il blocco si piega verso di loro quanto le foto lo consentono; gli errori delle barre prima e
  dopo sono nel resoconto.
* **Da Metashape e WebODM**: le barre di scala di un progetto Metashape arrivano con l'import
  (distanza, accuratezza, barre di check). WebODM non ha barre di scala, ma i suoi task ora portano
  le pose delle camere e i GCP come marker misurati nelle foto, così si possono tracciare barre tra
  di loro.

Da terminale: `ppg scalebar PROGETTO --markers "target 3,target 9" --distance 0.18` (`--check`,
`--accuracy`, `--import barre.csv`, `--list`, `--remove ETICHETTA|all`); `ppg markers scalebar
barra.pdf --distance-mm 200` stampa le barre.

## Schede US, proxy 3D e BIM

* **Schede US**: doppio clic su una US nel pannello PyArchInit apre la scheda (`us_table`) con
  descrizione, cronologia, dati di scavo, quote e rapporti stratigrafici modificabili;
  *Nuova US* crea un nuovo record. I rapporti sono salvati nel formato di PyArchInit. Se il
  database ha una scheda US ridotta (per esempio il database demo della versione 0.2), la scheda
  lo segnala e *Aggiorna struttura del database* aggiunge i campi mancanti, dopo una copia di
  sicurezza del file, senza toccare i record.
* **Disegno**: *Disegna US* con clic sui vertici o *Mano libera* (tieni premuto, traccia, rilascia);
  *Snap* aggancia i punti a vertici e/o segmenti dei layer visibili (tolleranza 6/12/20 px), così
  le unità disegnate lungo un confine lo condividono senza buchi né sovrapposizioni.
* **Selezione dei vettori**: *Seleziona* nella barra Disegno (clic, Maiusc/Cmd+clic, riquadro);
  *Elimina* (o Canc) cancella unità, parti, tracce di sezione, linee e punti; *Unisci* fonde le
  unità in un solo poligono o linee contigue dello stesso layer. Per i poligoni caricati da
  PyArchInit le modifiche vanno nel database con *Salva modifiche in PyArchInit*.
* **Proxy 3D**: *Strumenti > Proxy 3D delle US* trasforma ogni poligono US in un solido chiuso
  tra il DEM e una base (piano, DEM dopo lo scavo, spessore o quota minima), con volume, area
  e quote; esportazione OBJ per l'Extended Matrix, pubblicazione come media 3D collegato alla US.
* **BIM**: l'IFC dei proxy contiene quantità per il computo (volume, area, altezza, quote),
  materiali, gruppi per periodo/fase e rapporti stratigrafici. Dettagli in [docs/bim.md](docs/bim.md).

## Interfaccia grafica

`ppg-gui` apre l'applicazione desktop: pagina iniziale con i progetti recenti; barra degli
strumenti con il flusso di lavoro (foto, marker, allineamento, riferimenti, nuvola densa,
modello, texture, DEM, ortofoto, *Elabora tutto*); area di lavoro con chunk, camere, marker e
prodotti; vista 3D (VTK, con un renderer software di riserva); ortofoto e DEM con strumenti di
disegno (unità stratigrafiche, tracce di sezione, misure, quote, prompt per SAM 2.1); foto
con posizionamento manuale dei marker; pannello riferimenti con errori sui GCP e sui punti di
controllo; console Python collegata al progetto (`doc`, `chunk`); elaborazioni in background
con avanzamento, annulla e indicatore nella barra di stato; barra degli strumenti di disegno
e pannello *Layer* (mostra/nascondi, zoom, attributi al clic) per unità, sezioni, curve di
livello e quote; pannello *Sezioni* con i disegni; pannello log con filtro per
livello, ricerca, copia e salvataggio; finestre di errore con suggerimento, traceback e
pulsante *Copia*; tema chiaro e scuro; *Aiuto > Diagnostica ambiente* (gli stessi controlli di
`ppg doctor`); menu PyArchInit per connettersi al database, pubblicare ortofoto/DEM/modelli,
scrivere i poligoni delle US e le quote. Guida in [docs/gui.md](docs/gui.md).

## Uso da Python

```python
import pyarchinit_photogrammetry as ppg

doc = ppg.Document()
chunk = doc.add_chunk("Saggio 1")
chunk.add_photos(["foto/"])
chunk.detect_markers("aruco_4x4_50")
chunk.match_photos(quality="high")
chunk.align_cameras()
chunk.import_reference("gcp.csv", crs="EPSG:3004")
print(chunk.update_transform()["control_rmse"])
chunk.build_depth_maps()
chunk.build_point_cloud()
chunk.build_model()
chunk.build_texture()
chunk.build_dem()
chunk.build_orthomosaic()
doc.save("scavo.ppg")
```

Dalla riga di comando:

```bash
ppg markers sheet target.pdf --codes 0-23 --size-mm 40
ppg process scavo.ppg foto/ --gcp gcp.csv --crs EPSG:3004
ppg pyarchinit publish scavo.ppg "Lagoi" "Ortofoto saggio 1" --dem --model --link-us 1:101,1:102
```

## Collegamento con PyArchInit

La libreria legge `config.cfg` di PyArchInit e apre il database (SQLite/SpatiaLite o
PostgreSQL/PostGIS) senza QGIS. Ortofoto, DEM e modelli 3D vengono registrati come media
con le stesse miniature di PyArchInit, collegati alle US, con scheda in
`documentazione_table` (`Fotopiano`, `DEM`, `Modello tridimensionale`, sorgente
`Fotogrammetria`) e ingombro in `pyarchinit_documentazione`. Si possono scrivere anche
poligoni delle US, quote, tracce di sezione e computi metrici. Dettagli in
[docs/pyarchinit.md](docs/pyarchinit.md).

## Licenza

GNU GPL v2 o successiva, come PyArchInit.

Autore: Enzo Cocca
