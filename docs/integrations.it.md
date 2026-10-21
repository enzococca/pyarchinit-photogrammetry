# Integrazioni: QGIS, Metashape, WebODM, Photo3D

## QGIS, dal vivo

*QGIS > Apri in QGIS (collegamento dal vivo)* (o il pulsante QGIS nella barra del disegno)
scrive un progetto QGIS accanto al `.ppg` (`<nome>_qgis/<nome>_ppg.qgs`) e lo apre:

* raster: l'ortofoto, il DEM (ombreggiatura, all'inizio nascosta) e i valori del DEM;
* vettori, nel gruppo *Disegni (aggiornati dal vivo)*: le tabelle di un GeoPackage,
  `disegni_ppg.gpkg`: `unita` (poligoni delle unità, etichetta = `etichetta`), `sezioni` (tracce
  di sezione), `curve` (curve di livello, etichetta = quota), `linee` (altre linee), `punti`
  (quote, etichetta = `z`).

Il GeoPackage viene aggiornato 0,4 s dopo ogni modifica nell'interfaccia (disegno nuovo, layer
tolto, attributo modificato), sul posto e con una transazione per ogni tabella cambiata, e il
progetto chiede a QGIS di ridisegnare quei layer ogni secondo: QGIS rilegge un GeoPackage a ogni
ridisegno, così quello che disegni qui compare in QGIS da solo, senza *Aggiorna*.

Fino alla 0.4.1 i disegni erano file GeoJSON impostati per *ricaricare i dati ogni secondo*, e i
disegni nuovi comparivano solo dopo *Aggiorna*: QGIS tiene il GeoJSON in memoria, e QGIS 3.34-3.42
non avvia il timer di aggiornamento di un layer letto da un progetto salvato con
`autoRefreshMode` (lo stile, letto per primo, imposta la modalità mentre l'intervallo è ancora 0;
poi la modalità viene impostata di nuovo, uguale), così la mappa continuava a mostrare l'immagine
che aveva in cache. Il progetto viene perciò scritto con il vecchio attributo `autoRefreshEnabled`,
che QGIS legge nell'ordine giusto. Un progetto salvato di nuovo da quelle versioni di QGIS prende
l'attributo nuovo e alla prossima apertura perde il ridisegno automatico: aprilo da qui (*Apri in
QGIS* lo riscrive). Usa *Apri in QGIS* una volta per portare sul GeoPackage un progetto collegato
con la 0.4.1. Un progetto che è stato aperto in QGIS resta collegato quando viene riaperto qui; i
raster fatti dopo il progetto QGIS (un DEM nuovo, un'ortofoto nuova) compaiono dopo un altro *Apri
in QGIS*. Il progetto è creato dal Python di QGIS stesso (`/Applications/QGIS*.app`,
`C:\Program Files\QGIS*`, `/usr/bin/qgis`, o `PPG_QGIS`), così stili ed etichette sono quelli
nativi di QGIS. Le unità pubblicate in PyArchInit (`pyunitastratigrafiche`) sono visibili,
naturalmente, anche nei layer PyArchInit di QGIS.

## Metashape (`.psx`)

*File > Importa > Progetto Metashape* o `ppg import-metashape progetto.psx nuovo.ppg`.
Il progetto viene letto direttamente (Metashape non serve): i sensori con la loro calibrazione
(convertita nel modello COLMAP `FULL_OPENCV`: `f`, `cx/cy` dal centro, `b1`, `k1..k3`, `p1/p2`
scambiati nell'ordine di OpenCV), le camere allineate, i marker con riferimenti, accuratezze e
proiezioni, le barre di scala, i punti di legame con i colori, il modello (`model.zip`) e i GeoTIFF
di DEM e ortofoto esportati accanto al progetto (cartelle o nomi che contengono *dem/dsm* e
*orto/ortho*). Senza un DEM esportato utilizzabile (nessuno, o un file vuoto lasciato da
un'esportazione fallita, che viene segnalato), si usa il DEM conservato dentro il progetto:
Metashape lo tiene come tasselli GeoTIFF elencati in `elevation.zip`, che vengono uniti in mosaico
e riproiettati nel CRS del chunk alla risoluzione di Metashape (`dem.tif` nel progetto; Tell Abu
adh-Dhuhur: 8 tasselli, 10.241 x 10.545 px a 5,9 cm in 30 s, 2 cm dai punti di legame, mediana).
Il chunk viene collocato nel CRS di quei GeoTIFF, altrimenti nel CRS proiettato del progetto, o
nella zona UTM per i progetti geografici (similarità stimata passando per il sistema geocentrico di
Metashape).

Dalle pose importate viene scritto un modello COLMAP, così *Nuvola densa*, *Modello*, *DEM* e
*Ortofoto* girano qui senza allineare di nuovo (le foto devono stare dove dice il progetto).

Dettagli dell'importazione:

* le barre di scala tra marker arrivano con la loro distanza, la loro accuratezza (quella della
  barra, altrimenti la *Scale bar accuracy* del chunk) e il loro tipo: una barra non spuntata nel
  pannello *Reference* di Metashape è una barra di verifica. Metashape ha già scalato il chunk con
  loro, così la trasformazione non viene stimata di nuovo: il pannello *Riferimenti* mostra ogni
  barra con il suo errore (su quattro progetti di oggetti di una campagna, barre da 0,5 m sono
  tornate a 0,5000 m). Le barre tra camere, o senza distanza, vengono segnalate e lasciate fuori.
  Le accuratezze dei marker vengono dai marker o dalla *Marker accuracy* del chunk;
* un chunk in coordinate locali scalato con le barre di scala ha una trasformazione con la sola
  scala (niente rotazione, niente traslazione): viene applicata (fino alla 0.5.1 la scala si
  perdeva e quei progetti arrivavano 3-14 volte troppo grandi);
* camere e marker dentro i gruppi (le cartelle del Workspace di Metashape) vengono letti; i chunk
  vuoti vengono saltati e il chunk con più camere allineate diventa quello attivo;
* i punti di legame vengono letti da ogni componente dell'allineamento, ciascuno con la propria
  trasformazione; i punti lasciati da componenti che non esistono più nel progetto (un frame
  obsoleto, centinaia di metri lontano) vengono lasciati fuori con un avviso;
* camere, punti di legame e vertici del modello vengono convertiti nel CRS punto per punto (le
  rotazioni delle camere seguono la rotazione locale della conversione), non con una sola
  similarità;
* il modello (`mesh.ply` in `model.zip`, fino a decine di milioni di facce) viene estratto in
  streaming e una copia grossolana fatta per raggruppamento dei vertici (`model_preview.ply`,
  circa 600.000 facce) è quella che la vista 3D mostra; i colori dei vertici e la texture (un
  `texture.tif` a pagina singola, scritto come `model_texture.jpg`) arrivano con lui, e *File >
  Importa > Modello e texture dal progetto Metashape* li aggiunge ai progetti importati prima;
* Metashape 2.x costruisce la mesh dalle mappe di profondità, così la maggior parte dei progetti
  non ha la nuvola densa: i vertici colorati della mesh diventano la nuvola densa (al massimo
  8 milioni di punti). Per una vera nuvola densa lancia *Nuvola densa* qui (usa le pose di
  Metashape), oppure costruiscila in Metashape;
* i GeoTIFF di DEM e ortofoto esportati vengono cercati accanto al progetto, non dentro le
  cartelle di altri progetti, e collegati solo se coprono le camere.

Verificato sui progetti da drone di una campagna di rilievo (29-3.265 foto): Tell Dlamah importato
in 6 s (punti di legame +3 mm e vertici della mesh +4 mm dal DEM di Metashape, mediana), un
progetto di 425 foto con un gruppo di camere, un progetto di 3.265 foto con una componente
cancellata (+13 mm). Non letti: il formato interno dell'ortofoto di Metashape (blocchi di immagine
e linee di cucitura: esportala come GeoTIFF), le nuvole dense nel formato octree di Metashape, le
maschere, lo skew `b2`, `k4`.

## WebODM / ODM

*File > Importa > Progetto WebODM* o `ppg import-webodm all.zip nuovo.ppg` (o la cartella del
task). Ortofoto, DSM (o DTM), nuvola di punti georeferenziata (`.laz`/`.las`/`.ply`) e la mesh con
texture (`odm_textured_model_geo.obj`, relativa all'offset in `coords.txt`) diventano i prodotti di
un chunk nel CRS dei raster.

Le camere arrivano con la loro calibrazione e la loro posa (`cameras.json` e
`odm_report/shots.geojson`, entrambi in `all.zip`), e i GCP usati da ODM diventano marker
(`odm_georeferencing/ground_control_points.geojson`, o `gcp_list_utm.txt`), con le coordinate
misurate a terra e i punti in cui sono stati misurati nelle foto. Vengono triangolati subito: il
pannello *Riferimenti* mostra i loro errori (gli stessi che calcola ODM; il suo valore è nel
rapporto dell'importazione), e tra loro si possono tracciare barre di scala per verificare la
scala. ODM non ha barre di scala sue: la scala di un task viene dal GPS delle foto o dai suoi GCP.
Con le foto (la cartella del task le ha, `all.zip` no) nuvola densa, modello e texture si possono
ricostruire qui dalle pose di ODM. Modelli di camera di OpenSfM: `perspective`, `brown`, `radial`,
`simple_radial`; le camere fisheye e sferiche vengono lasciate fuori. Le pose di un task non
georeferenziato non vengono importate.

## Photo3D

Due modi:

* **come motore di Photo3D** — il server di Photo3D lancia i motori come sottoprocessi con
  `PHOTO3D_SESSION_DIR` e `PHOTO3D_PRESET`; `ppg photo3d-job` segue quel contratto: righe
  `PROGRESS <stage> <pct>`, prodotti in `<session>/metashape/` (`model.obj`,
  `pointcloud.ply`, `dem.tif`, `orthophoto.tif`, più `ppg_project.ppg`) e
  `metashape/result.json`. Legge `gcp_list.txt` (formato ODM, pixel grezzi), `target_list.json`
  (target codificati con le coordinate misurate a terra: ArUco/AprilTag vengono riconosciuti,
  quelli circolari a 12 bit di Metashape no) e `scale.json` (due tocchi e una lunghezza in mm: i
  tocchi vengono intersecati con un piano locale adattato alla nuvola densa; errore mediano di
  0,5 mm su distanze di 50-180 mm nella prova del turntable). Preset: preview / medium / high.
* **come importazione** — *File > Importa > Sessione Photo3D* o `ppg import-photo3d <session>`:
  un `.ppg` con foto, target e GCP, da elaborare nell'interfaccia.

Per aggiungere il motore a Photo3D (repository `photo3d`, server in `mac/photo3d_server/`):
`config.py` (`ENGINE_NAMES` += `"ppg"`), `queue.py` (`build_command()`: lancia
`ppg photo3d-job` con l'ambiente della sessione), `engines/registry.py` (disponibilità:
`ppg` nel PATH o l'app impacchettata), `api.py` (`health()` → `ppgAvailable`), e la scelta del
motore nelle app iOS (`NewSessionSheet.swift`) e Android (`MainActivity.kt`). Gli endpoint
esistenti servono già i prodotti da `metashape/` con i loro nomi fissi.

## Segment Anything 2.1 nell'applicazione desktop

PyTorch non è dentro l'applicazione. La prima volta che si usa uno strumento SAM, l'interfaccia
propone di installarlo (*Strumenti > Segment Anything 2.1 > Installa / aggiorna SAM 2.1*): `uv`
(dal PATH o scaricato dalla sua release ufficiale su GitHub) crea un ambiente Python 3.12 in
`~/Library/Application Support/pyarchinit-photogrammetry/sam-env` (macOS),
`%LOCALAPPDATA%\pyarchinit-photogrammetry\sam-env` (Windows) o `~/.local/share/...` (Linux),
e installa PyTorch e `sam2` (1-2 GB). SAM gira poi in un processo di lavoro di quell'ambiente
(`ai/sam_worker.py`), sulla GPU Apple (MPS), su CUDA o sulla CPU; i pesi del modello vengono
scaricati da Hugging Face al primo uso. Un'installazione con pip con l'extra `[sam]` continua a
eseguire SAM nello stesso processo.
