# pyarchinit-photogrammetry

Motore fotogrammetrico open source per l'archeologia e il BIM dei beni culturali, integrato
con [PyArchInit](https://github.com/pyarchinit/pyarchinit).

Dalle foto a nuvole di punti, mesh, DEM e ortofoto georeferenziati, con gli strumenti che
servono in uno scavo: target codificati, punti di appoggio, sezioni, profili ceramici,
curve di livello, volumi di scavo, classificazione della nuvola, Segment Anything 2.1,
esportazione IFC e salvataggio diretto nel database di PyArchInit. Tutto è utilizzabile da
Python e gira su CPU, anche su Mac Apple Silicon.

> Stato: **alpha (0.3)**. Libreria Python, riga di comando e applicazione desktop (sulla stessa
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
