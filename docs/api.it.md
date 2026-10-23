# API Python

```python
import pyarchinit_photogrammetry as ppg
```

## Documento e chunk

| Chiamata | Descrizione |
| --- | --- |
| `ppg.Document(path=None)`, `Document.open(path)`, `doc.save(path=None)` | file di progetto `.ppg` + cartella `.ppg.files/` |
| `doc.add_chunk(label)`, `doc.chunks`, `doc.chunk` | i chunk; `doc.chunk` è quello attivo (assegnalo per cambiarlo) |
| `ppg.app.document` | il progetto condiviso da script, CLI (`ppg run`) e interfaccia |

## Flusso di lavoro del chunk

Una riga per metodo di `Chunk`, nell'ordine del flusso di lavoro; i parametri che contano di più. Le
spiegazioni più lunghe seguono la tabella.

| Metodo | Che cosa fa |
| --- | --- |
| `add_photos(paths, recursive=False)` | file o cartelle; sensori raggruppati per corpo, obiettivo e risoluzione; il GPS dell'EXIF diventa il riferimento della camera |
| `detect_markers(target_type="aruco_4x4_50")` | marker `target <codice>` con le loro proiezioni |
| `match_photos(quality, keypoint_limit, tiepoint_limit, generic=None, reference=None, apply_masks="keypoints", guided_matching=False, exclude_stationary=False, reset=True, gpu=None, matcher="sift")` | punti chiave e corrispondenze, con le scelte di *Allinea foto* di Metashape (*Coppie*, sotto); SIFT o punti appresi (*Punti appresi*, sotto) |
| `align_cameras(mapper="auto", use_gps=None, adaptive_fitting=False, reset=True, tiepoint_limit=None, ba_global_ratio=None, ba_global_refinements=None)` | le foto collocate, dal mapper globale, da quello incrementale o da quello gerarchico (*Mapper*, sotto), tenute al GPS delle foto; la trasformazione viene azzerata e la georeferenziazione rifatta quando ci sono riferimenti |
| `optimize_cameras(fit_principal_point=False, use_markers=True, marker_weight=4)` | compensazione di pose, calibrazione e punti di legame, sui punti di controllo e sulle barre di scala quando ce ne sono (*Ottimizzazione delle camere*, sotto); restituisce l'errore di riproiezione (px) |
| `import_reference(csv, crs, columns="nxyz")` | riferimenti di marker e camere da un file di testo: `n` etichetta, `x y z`, `a` accuratezza, `-` salta |
| `export_reference(csv)` | i riferimenti in un file di testo |
| `add_marker(...)`, `add_marker_projection(...)` | marker a mano (come vengono collocati: *Barre di scala*) |
| `add_scalebar(marker_a, marker_b, distance)` | una barra di scala tra due marker (*Barre di scala*) |
| `triangulate_markers()` | i marker collocati dalle loro proiezioni (due foto o più), o sulla superficie da una sola |
| `update_transform(use_cameras=None)` | la similarità sui riferimenti (pesata, 7 parametri); restituisce l'RMSE di controllo e di verifica e l'errore di ogni marker e di ogni barra |
| `build_depth_maps(quality, filter_mode, backend)` | mappe di profondità; backend `auto`, `cpu`, `patchmatch`, `colmap`, `openmvs` |
| `build_point_cloud()` | la nuvola densa dalle mappe di profondità |
| `build_model(source, surface, face_count, voxel_size, vertex_colors)` | la mesh da `point_cloud`, `tie_points` o `depth_maps` (*Mesh dalle mappe di profondità*, sotto) |
| `build_texture(texture_size, page_size=8192)` | la texture del modello, a pagine sopra `page_size` (*Pagine della texture*, sotto) |
| `build_dem(resolution, source)` | il DEM, un GeoTIFF nel CRS del chunk |
| `build_orthomosaic(resolution, surface, blending, colour_correction, ghosting, fill_holes)` | l'ortofoto sul DEM o sul modello (*Ortofoto senza cuciture e correzione del colore*) |
| `calibrate_colours()` | esposizione e vignettatura delle foto (*Ortofoto senza cuciture e correzione del colore*) |
| `export_point_cloud(path)`, `export_model(path)`, `export_dem(path)`, `export_orthomosaic(path)` | i prodotti in coordinate mondo (formati e opzioni: *Esportazioni per il web*, *Confidenza, pulizia e ritaglio*) |
| `export_cameras(fmt="csv")` | le camere, `csv` o `colmap` |
| `generate_report(path)` | il rapporto HTML del chunk |

Ogni metodo di elaborazione accetta `progress=callback(fraction, message)`.

### Coppie

Le scelte sono quelle di *Allinea foto* di Metashape. `quality` è la precisione: `ultra`, `high`,
`medium`, `low` o `lowest`, le foto a piena dimensione o ridotte a 1/2, 1/4, 1/8, 1/16 del lato.
`generic` è la preselezione generica: le coppie che si sovrappongono vengono trovate prima sulle foto
ridotte a 1024 pixel e le 20 coppie più forti di ogni foto vengono abbinate per intero (`None`: da 40
foto in su; sotto, tutte le coppie costano poco). `reference` è la preselezione di riferimento:
`source` (ogni foto con le `neighbours` più vicine per posizione di riferimento, il GPS del drone),
`estimated` (per le posizioni stimate da un allineamento precedente; le foto non ancora allineate con
tutte le altre), `sequential` (le foto scattate prima e dopo, nell'ordine dei tempi di scatto: un
video, un giro intorno a un oggetto). Con entrambe le preselezioni accese si abbinano le coppie di
entrambe; con nessuna, tutte le coppie. La vecchia parola `pair_selection` (`auto`, `exhaustive`,
`generic`, `sequential`, `reference`) vale quando `generic` e `reference` non vengono dati.

`apply_masks` applica le maschere delle foto ai `keypoints` (nelle parti mascherate non si cercano
punti), ai `tiepoints` (un punto mascherato in una foto viene tolto ovunque dopo l'allineamento:
bastano poche foto mascherate) o a `none`. `guided_matching` cerca di nuovo le corrispondenze con la
geometria di ogni coppia (più punti di legame, più lento); `exclude_stationary` toglie le
corrispondenze che non si muovono tra due foto (lo sfondo fermo di un turntable). `reset=False`
tiene le corrispondenze e l'allineamento che ci sono e abbina solo le foto aggiunte dopo;
`align_cameras(reset=False)` poi le colloca nel blocco, una foto dopo l'altra (quando non si può
tenere niente, si rifà tutto). `meta["sfm"]` registra cosa è stato fatto: `pair_selection`
(`exhaustive`, `generic`, `generic+source`, ...), `pairs`, `apply_masks`, `masked_tie_points`, `added`,
`matcher`.

### Punti appresi

`matcher` sceglie i punti: `sift` (il SIFT di COLMAP, sulla GPU quando c'è) oppure punti chiave
trovati da una piccola rete neurale e abbinati da LightGlue, `disk` o `aliked` (`engine.learned`,
kornia su PyTorch: l'extra `[learned]`, oppure l'ambiente separato di SAM 2.1 che l'applicazione
installa su richiesta). Trovano corrispondenze dove SIFT ne trova poche - fotogrammi termici, intonaco
liscio, sabbia - e sono più lenti. Le reti non sono invarianti alla rotazione, SIFT sì: ogni foto viene
prima girata di quarti di giro verso la direzione comune alle foto (`quarter_turns`: l'imbardata che il
drone scrive nella foto, quella del gimbal o del velivolo nell'XMP di DJI oppure `GPSImgDirection`
dell'EXIF; la rotta delle strisciate modulo 90 gradi, così le strisciate di andata e ritorno e quelle
incrociate finiscono tutte dritte allo stesso modo), e i suoi punti vengono riportati sulla foto com'è.
Senza questo, i fotogrammi di un volo termico abbinati tra strisciate percorse in versi opposti
fallivano quasi sempre. Le reti girano sempre in un processo a parte (`learned_worker.py`): PyTorch e
pycolmap portano ciascuno un runtime OpenMP e il secondo che parte fa abortire il processo
(`OMP: Error #15`); lo stesso vale per SAM 2.1. Per le reti le foto si riducono a 1600 pixel al
massimo; i descrittori restano in `sfm/learned/<tipo>/` accanto al database di COLMAP, le
corrispondenze vanno nel database e la verifica geometrica di COLMAP decide gli inlier. Non per i rig
delle foto a 360 gradi, né con l'abbinamento guidato (entrambi di COLMAP). `ppg align PROGETTO
--matcher disk`.

Sui 407 fotogrammi termici di un volo di un Mavic 2 Enterprise Advanced a 20 m (foto a 320 pixel,
`quality="high"`), SIFT ne ha allineati 405 una foto alla volta (la sua soluzione globale mancava il GPS
di 78 m) dopo 108 s di abbinamento e 229 s di allineamento; DISK li ha allineati tutti e 407 insieme
(513 s e 22 s), ALIKED anche (1187 s e 23 s); le ortofoto delle temperature sono venute nitide allo
stesso modo e le camere vicine al GPS allo stesso modo (1,7-1,9 m di mediana). Senza girare le foto
DISK ne allineava 267. SIFT resta la scelta predefinita: i punti appresi servono per le foto che SIFT
non riesce a tenere insieme.

### La GPU

`gpu` (predefinito: la variabile d'ambiente `PPG_GPU`, altrimenti `auto`) mette l'abbinamento anche
sulla GPU: `auto` prende la GPU migliore del computer, `all` tutte quelle che ha (ognuna con il
suo flusso di coppie), `off` lascia tutto alla CPU, un indice di adattatore o parte del suo nome ne
sceglie una (`ppg doctor` le elenca). I kernel sono scritti una
volta in WGSL e girano attraverso wgpu - Metal su macOS, Vulkan su Linux e Windows (anche ARM),
DirectX 12 su Windows - nel nucleo nativo (`pyarchinit-photogrammetry-core` 0.3,
`ppg_core.gpu_match`): un passaggio su una coppia di foto dà i prodotti scalari migliore e secondo
in entrambe le direzioni, così il test del rapporto, quello della distanza e il controllo incrociato
di COLMAP ne escono esatti (i descrittori sono quelli uint8 di COLMAP, i prodotti scalari interi
esatti; un Apple M4 Pro fa una coppia da 40.000 descrittori in 0,2 s). Le coppie formano una coda
sola: la GPU le prende una alla volta dalla testa, la CPU (il matcher di COLMAP) a blocchi dalla
coda, ognuna alla sua velocità, così la divisione segue la velocità delle due; le corrispondenze
della GPU vanno nel database e passano la verifica geometrica di COLMAP, e l'allineamento non vede
differenze. L'abbinamento guidato e i rig di foto a 360 gradi restano sulla CPU.
`meta["sfm"]["gpu"]`, `gpu_pairs` e `cpu_pairs` dicono chi ha fatto cosa.

Anche i punti: con il nucleo nativo 0.4 il SIFT di COLMAP (quello di VLFeat) è a sua volta un
insieme di kernel WGSL (`ppg_core.gpu_sift`) - la piramide gaussiana, le sue differenze, gli estremi
con il loro raffinamento, le orientazioni e i descrittori di una foto, un'ottava per invio. Le foto
vengono lette e ridotte sulla CPU, in thread, esattamente come le legge COLMAP (il suo grigio Rec.
709, il raster del file, il filtro triangle di OpenImageIO in precisione singola, il lato lungo alla
misura della qualità), così la GPU trova i punti che trova l'estrattore di COLMAP, con gli stessi
descrittori (il 99 % sulla trincea sintetica). Un Apple M4 Pro fa una foto da 2016 pixel in 0,19 s
dove un core dell'estrattore di COLMAP ne impiega 3,2; contro tutti i suoi core il guadagno è
minore (57 foto di Tell Sultan: 11 s contro 19), maggiore su una GPU dedicata. Le maschere valgono
come prima; le facce delle foto a 360 gradi restano all'estrattore di COLMAP, come una foto che la
GPU non riesce a fare. `meta["sfm"]["features"]` dice `gpu` o `cpu`.

Anche la fusione delle mappe di profondità (`build_point_cloud(gpu=...)`, `ppg_core.gpu_fuse`):
un thread per pixel di ogni mappa invece della traversata di COLMAP, le mappe covisibili con essa
(fino a 64, tenute sulla GPU in una cache) controllate con i test stessi di COLMAP - il pixel
dell'altra mappa riproiettato entro 2 px, la profondità entro l'1 %, le normali entro 20 gradi -, i
pixel coerenti mediati nel punto, che emette la prima mappa che lo contiene (nessuna mappa ripete
un punto che un'altra ha già fatto), tenuto con i `min_num_pixels` pixel della modalità di filtro.
Nessun pixel viene assorbito da un punto vicino, così la nuvola è densa quanto le mappe (sulla
trincea sintetica 2,7 volte quella di COLMAP, con il 90° percentile degli errori in quota a 2,4 mm
contro 3,0), e la confidenza di ogni punto - le mappe che concordano sulla sua profondità entro
l'1 % - esce dallo stesso passaggio, senza rifare `depth_map_support` (57 foto di Tell Sultan,
mappe da 1008 pixel: 1,8 s contro 4,5 s della fusione di COLMAP più la confidenza; più il rilievo è
grande, più cresce il guadagno, perché la traversata di COLMAP rallenta col numero di mappe).
`meta["dense"]["fusion"]` dice `gpu` o `colmap`; una GPU che fallisce lascia il lavoro alla
fusione di COLMAP.

Le mappe di profondità PatchMatch prendono la stessa scelta (`build_depth_maps(gpu=...)`): il
kernel del nucleo nativo gira sulla GPU, un thread per pixel, con lo stesso costo (la correlazione
incrociata normalizzata della finestra attraverso il piano del pixel in fino a sei foto, contando
le tre migliori), la stessa propagazione e la stessa partenza, l'intera corsa di una foto in un solo
invio. Le foto formano una coda servita da un lavoratore per GPU e uno per il kernel CPU (che usa
tutti i core), così GPU e CPU se le dividono secondo la loro velocità; una foto che una GPU non
riesce a fare passa al kernel CPU. `meta["dense"]["gpu"]` dice quali GPU sono state usate.

### Camere multispettrali

`engine.multispectral`: una camera multispettrale (MicaSense RedEdge / Altum, DJI Mavic 3
Multispectral, Parrot Sequoia, DJI P4 Multispectral) scrive un file per banda di ogni scatto, fianco
a fianco; le bande si riconoscono dai suffissi (`_1`..`_6`, `_MS_G` ..., `_GRE` ...) o dal `BandName`
dell'XMP, gli scatti di un P4 Multispectral (sei file con numeri propri) dal loro `CaptureUUID`, una
numerazione che riparte in una seconda cartella dal nome della cartella (`detect_bands(paths,
recursive=False)`; `looks_multispectral(paths)` dice dai primi file se una cartella contiene riprese,
ciò su cui la finestra fa la domanda). `add_captures(chunk, paths, reference=None, recursive=False)`
fa della banda di riferimento (la rossa, se c'è) la foto di ogni scatto - una copia a 8 bit stirata,
con i tag dell'originale, allineata e ricostruita come una foto qualsiasi - e tiene le altre bande
in `camera.meta["bands"]`; chiamata di nuovo con un'altra cartella del volo aggiunge alle riprese
che ci sono (i nomi che si ripetono prendono la cartella, la banda di riferimento resta, i file già
nel chunk restano fuori). La radiometria (`radiance_image`, `reflectance_image`) segue i tag: il
modello MicaSense (livello di nero, guadagno ed esposizione, i coefficienti di
`RadiometricCalibration`, il polinomio di vignettatura), altrimenti numeri normalizzati per
esposizione e guadagno; la riflettanza viene da un pannello di calibrazione ripreso nel volo
(`calibrate_panel(chunk, camera, region, reflectance)`: la riflettanza nota per banda sulla
radianza del pannello) o dall'irradianza del sensore di luce nei tag. Le bande di uno scatto si
portano sulla banda di riferimento con un'omografia trovata sulle immagini (`register_bands`: lo
spostamento per correlazione di fase sulle magnitudini del gradiente, un'affine a dimensione
ridotta, l'omografia a dimensione piena), a catena per lunghezza d'onda: ogni banda viene abbinata
alle bande già registrate più vicine in lunghezza d'onda (una foglia è chiara e piena di texture nel
NIR, scura e liscia nel rosso: NIR sul rosso converge di rado, NIR sul red-edge sempre; su un P4
Multispectral sulla macchia le correlazioni passano da 0,1-0,3 a 0,9). Il modello radiometrico DJI
(polinomio di vignettatura, livello di nero dell'EXIF, guadagno, esposizione, correzione del
guadagno, irradianza del sensore di luce) segue la loro guida. `build_reflectance(chunk, bands=None, resolution=None, balance=True)`
fa girare il motore dell'ortofoto sulla geometria della banda di riferimento con le bande
registrate come valori a virgola mobile: `<dati>/multispectral/reflectance.tif`, una banda float32
per banda spettrale, NaN dove non c'è niente. `balance` porta prima le foto alla stessa riflettanza
dove si sovrappongono (`balance_gains`: punti della superficie proiettati in ogni scatto e letti
nelle sue bande, i logaritmi dei valori divisi in un valore del punto e un guadagno della foto,
minimi quadrati per alternanza con guadagni a media zero, scarti oltre tre MAD esclusi): le strisce
tra le linee di volo percorse in direzioni opposte spariscono, il livello medio resta; guadagni e
loro dispersione per banda stanno in `meta["multispectral"]["balance"]`. `index_raster(chunk, "NDVI")` (anche NDRE, GNDVI,
SAVI, NDWI, OSAVI, CIRE, Thermal, o una formula qualsiasi sui nomi delle bande) fa l'indice sulla
stessa griglia; `export_zoning(chunk, "NDVI", "zone.gpkg", classes=3, method="quantiles" | "equal"
| "kmeans")` lo taglia in classi (le macchie piccole assorbite dal contorno) e scrive i poligoni
con classe, intervallo, media e area in un GeoPackage - la mappa di prescrizione dell'agricoltura;
in archeologia le tracce sulle colture sopra muri e fossati sepolti. `ppg multispectral PROGETTO
add|panel|reflectance|index|zoning|bands`.

### Camere termiche

Una camera termica da drone scatta insieme una foto visibile e un fotogramma termico (DJI Mavic 2
Enterprise Advanced, M30T, Mavic 3 Thermal, H20T: `DJI_0517_W.JPG` e `DJI_0518_T.JPG`; MicaSense
Altum: la sesta banda, LWIR). `add_captures` li riconosce: le coppie si trovano dal numero quando è
lo stesso, altrimenti dall'ora e dal luogo dello scatto (queste camere numerano ogni file, e i due
tipi possono stare in cartelle diverse), la foto visibile è la foto dello scatto così com'è, il
fotogramma termico la sua banda `LWIR`. I fotogrammi termici senza foto visibili sono scatti a sé:
si allinea una copia a 8 bit di ciascuno, tutte con un unico stiramento dei conteggi grezzi.

`engine.thermal` porta i fotogrammi in gradi Celsius (`temperature(chunk, cam)`, `decode_file`):
gli R-JPEG DJI con il Thermal SDK di DJI (`dji_irp -a measure --measurefmt float32`, tutti i
fotogrammi in una sola esecuzione; Windows e Linux direttamente, sul Mac in Docker con un'immagine
che il programma prepara la prima volta: Docker Desktop deve essere avviato), i TIFF LWIR di
MicaSense in centikelvin, i TIFF già in gradi o in kelvin. L'SDK va nella cartella `dji_tsdk` dei
dati del programma (o `PPG_DJI_TSDK`); è gratuito su dji.com. I conteggi grezzi di un R-JPEG
(`raw_counts`, i suoi segmenti APP3) crescono con la temperatura ma non sono gradi: servono solo
alla geometria. I parametri della misura (`params_of`, `set_params`) valgono per tutti i
fotogrammi: emissività 0,95, temperatura riflessa e ambiente 22 °C (i valori di fabbrica delle camere
DJI), umidità 70 %, la distanza uguale all'altezza di volo limitata a 1-25 m come vuole l'SDK;
cambiandoli i fotogrammi vengono decodificati di nuovo.

`build_thermal(chunk, resolution=None, balance=True)` crea `<dati>/multispectral/thermal.tif`, una
banda float32 in gradi Celsius con i parametri nei tag, NaN dove non c'è niente, alla dimensione a
terra di un pixel termico se non si chiede altro. Con le foto visibili il fotogramma termico di ogni
scatto va sulla sua foto con un'omografia (`registrations`): la prima stima dalle focali, o una
ricerca della parte di foto che il fotogramma copre, affinata con ECC sulle magnitudini del
gradiente; un'omografia per il volo (la mediana degli scatti che si accordano meglio: la camera è
rigida), affinata scatto per scatto solo dove si accorda chiaramente meglio. Il motore legge le
temperature su una griglia ridotta della foto visibile (`thermal_stack`), e una foto il cui
fotogramma non arriva a una cella la lascia alla successiva che ci arriva. `balance_offsets` toglie
uno scostamento per fotogramma trovato dove i fotogrammi si sovrappongono (le camere non raffreddate
derivano tra una correzione di campo piatto e l'altra, e il terreno si scalda durante il volo: in un
volo di 28 minuti di un Mavic 2 Enterprise Advanced gli scostamenti hanno una deviazione di circa un
grado), conservando il livello medio. `anomalies(chunk, radius_m)` è la temperatura meno quella dei
dintorni (`thermal_anomaly.tif`, gradi attorno allo zero); `difference` sottrae un'altra ortofoto
termica dello stesso terreno (notte contro giorno). `export(chunk, path, "tif" | "tif16" | "csv" |
"flir")` conserva le temperature: GeoTIFF float32 in gradi, GeoTIFF a 16 bit in centikelvin (gradi =
valore / 100 - 273,15, con scala e offset impostati), CSV di x, y e temperatura, i parametri nei tag o
nell'intestazione, oppure JPEG radiometrici FLIR (`export_flir`, `write_flir_jpeg`): tasselli di 2048
celle di lato (`tile`, al massimo 2800: un file FLIR sta in 256 segmenti APP1) chiamati
`<nome>_r<riga>_c<colonna>.jpg`, ognuno con il suo world file (`.jgw`) e il sistema di riferimento
(`.aux.xml`), e un indice `<nome>_tiles.csv`. L'immagine è la resa con la tavolozza; i conteggi grezzi
sono costruiti con l'inversa del modello di FLIR (raw2temp di Thermimage) per i parametri della misura
come li stampa ExifTool, così ExifTool, i programmi di FLIR e quelli costruiti su di essi
(Thermal-Image-Analysis, flirimageextractor, flyr) rileggono gli stessi gradi, entro 0,01 °C. Il
formato non ha un valore vuoto: le celle senza temperatura prendono la più fredda del tassello (nere
nell'immagine). I JPEG radiometrici FLIR si leggono anche (`read_flir`, `decode_flir`: i dati grezzi
come conteggi a 16 bit o come PNG, le costanti di Planck della camera, i parametri della misura del
chunk). `ppg thermal PROGETTO status|params|build|anomalies|export [--format flir --tile 2048]`.

`engine.probe` legge cosa contengono i prodotti in un punto, a piena risoluzione: `Probe(chunk)` tiene
aperti il DEM, l'ortofoto termica e le sue anomalie, la riflettanza e gli indici; `probe.at(x, y,
fields=None)` dà `{"z", "temp_c", "anomaly_c", "refl_<banda>", "<indice>"}` (solo i valori che i
prodotti hanno lì), `probe.record(x, y, label=...)` un punto di misura arrotondato a ciò che i valori
significano, `write_points(punti, percorso, crs)` un GeoPackage (tabella `punti_misura`), un CSV o un
GeoJSON. La finestra tiene i suoi punti di misura in `chunk.meta["drawings"]["points"]`.

### Immagini satellitari

`engine.satellite`: una scena satellitare arriva con i suoi *coefficienti polinomiali razionali*
(RPC), due razionali cubiche che danno colonna e riga di un punto a terra qualsiasi (longitudine,
latitudine, quota ellissoidica). `read_rpc(scena, rpc=None)` li legge dal GeoTIFF, da un file
`_RPC.TXT` / `.RPB` / `.rpc` accanto o dall'`RPC_*.XML` di un prodotto DIMAP (Pléiades, SPOT: la
loro numerazione dei pixel da 1 viene portata a 0); `project` e `ground` sono il modello e il suo
inverso (centro del primo pixel in (0, 0)). `scene_info` dà dimensioni, bande, risoluzione a terra
e impronta nel CRS del chunk. `ortho_scene(chunk, scena, name=None, rpc=None, resolution=None,
height_offset=0, rgb=None)` ortorettifica la scena sul DEM del chunk (`orthorectify`: ogni cella
portata alla quota del DEM, proiettata nella scena, la scena ricampionata lì, a tasselli in
thread): il risultato completo, tutte le bande nel tipo della scena, in
`<dati>/satellite/<nome>_ortho.tif`, e una resa RGBA a 8 bit conservata come versione
dell'ortofoto, così l'ortofoto del drone e l'immagine satellitare dello stesso terreno si
confrontano cella per cella. Le quote degli RPC sono ellissoidiche: con un DEM in quote
ortometriche indica l'ondulazione del geoide in `height_offset`. `dsm_from_pair(chunk, scena_a,
scena_b, name=None, resolution=None, height_range=None, use_dem=True)` costruisce un modello della
superficie da una coppia stereo (`stereo_dsm`): a tasselli, le due scene vengono ricampionate sul
piano del terreno a una quota di riferimento (la mediana del DEM lì, altrimenti quella degli RPC)
lungo la direzione della loro base; un punto sopra o sotto il piano si sposta lungo la linea di
vista di ogni scena, così la disparità trovata con l'abbinamento semi-globale (SGBM di OpenCV) dà
la sua quota e la posizione vera si recupera togliendo lo spostamento; il risultato, un GeoTIFF
float32 con i buchi riempiti, resta come versione del DEM e in `<dati>/satellite/`. Sulla trincea
sintetica vista da due "satelliti" a proiezione parallela a 25 gradi dal nadir (base/altezza 0,93)
il DSM sta entro 0,6 mm (mediana) dal DEM. `ppg satellite PROGETTO info|ortho|dsm SCENA [SCENA_B]
[--rpc] [--resolution] [--height-offset] [--height-range zmin,zmax] [--rgb 3,2,1]`.

### Altri computer

Il lavoro che si divide per foto va agli altri computer della rete attraverso la cartella del
progetto (`engine.nodes`): su ognuno, con la cartella del progetto montata, `ppg node
<progetto>.ppg.files` prende unità di lavoro da `nodes/todo` (poche foto ciascuna, prese con una
rinomina atomica, restituite se un nodo muore) e lascia i risultati in `nodes/done`. Punti chiave:
il nodo fa girare l'estrattore di COLMAP in un database suo e rimanda punti e descrittori, il
programma principale li scrive nel proprio database, così l'allineamento non vede differenze.
Mappe di profondità PatchMatch: il nodo apre il progetto in sola lettura sulla cartella condivisa
e calcola le sue unità nello spazio di lavoro lì, con le sue GPU e la sua CPU. Il programma
principale è un nodo anche lui (prende quello che nessuno ha preso), quindi senza nodi non cambia
niente; `PPG_NODES=0` tiene tutto qui. Un nodo mappa i percorsi del programma principale sui
propri (la cartella del progetto da sola, `--map SRC=DST` per le foto). Via SSH, `ppg remote
utente@host:/cartella progetto.ppg [--photos ...]` copia lì la cartella del progetto (e le foto)
con rsync, avvia il nodo e copia avanti e indietro unità e risultati mentre un passo gira qui.
`meta["sfm"]["nodes"]` e `meta["dense"]["nodes"]` dicono quali computer hanno partecipato.
L'allineamento in sé resta su una macchina sola.

### Mapper

`mapper` è `incremental` (una foto dopo l'altra, COLMAP), `global` (tutte le camere insieme,
GLOMAP: rotazioni, posizioni, poi la compensazione su `tiepoint_limit / 4` tracce per foto,
ritriangolate; sulle 57 foto del tutorial di Tell Sultan 58 s contro 293 s, le stesse camere
entro 2 cm) o `auto`: globale da 40 foto, incrementale sotto, per i rig a 360 gradi e le
calibrazioni tenute fisse, quando il globale colloca meno del 90 % delle foto e quando le sue
camere non si accordano con il GPS delle foto. `meta["sfm"]["mapper"]` dice quale ha girato. Le foto si
allineano con una sola focale (pixel quadrati): stimare fx e fy separatamente può scivolare in una
soluzione degenere. Il risultato resta nel modello del sensore (`OPENCV` con fx = fy, senza
distorsione tangenziale); un sensore calibrato con fx != fy o con distorsione tangenziale viene
allineato com'è, e `optimize_cameras()` affina ogni parametro.

`use_gps` (`None`: quando le foto ce l'hanno) dà al mapper incrementale le posizioni di riferimento
delle foto, con la loro accuratezza, come prior di posa: il blocco viene tenuto su di esse mentre
cresce, come l'allineamento di Metashape lo tiene sui riferimenti delle camere, e una lunga catena o
un anello di foto non deriva più (una posizione GPS sbagliata viene smorzata da una loss robusta); la
ricostruzione sta poi sugli assi metrici dei riferimenti meno un offset tondo,
`meta["sfm"]["gps_priors"]`. `adaptive_fitting` è l'adattamento adattivo del modello della camera
di Metashape, fatto a modo nostro: i parametri che solo un blocco sano regge - punto principale,
distorsione tangenziale, due focali - vengono affinati su una copia della ricostruzione e tenuti solo
se l'errore medio di riproiezione scende almeno dell'1 % e ogni camera resta entro limiti fisici
(`meta["sfm"]["adaptive_fitting"]`).

`tiepoint_limit` (predefinito: il limite dato a `match_photos`) è il limite dei tie point di
Metashape alla maniera del mapper: i mapper incrementale e gerarchico leggono una copia del database
che tiene, per ogni foto, solo i suoi punti chiave più abbinati (`meta["sfm"]["tie_point_limit"]` conta
cosa è rimasto; 0 tiene tutto). `ba_global_ratio` e `ba_global_refinements` diradano le compensazioni
globali del mapper incrementale, che per impostazione compensa l'intero blocco ogni volta che cresce del
10 % con fino a 5 raffinamenti ciascuna (`meta["sfm"]["ba_schedule"]`): su un blocco di mille foto è la
gran parte del tempo di allineamento. `mapper="hierarchical"` è la pipeline gerarchica di COLMAP:
gruppi di foto allineati sui core, poi uniti.

### Ottimizzazione delle camere

Con 3 o più punti di controllo (marker con un riferimento abilitato, non punti di verifica) a una
compensazione libera ne segue una in cui i marker sono punti fissi alle loro coordinate di
riferimento, osservati nelle foto (ogni misura pesa `marker_weight` osservazioni di punti di legame):
il blocco si piega per adattarsi a loro, cosa che una similarità da sola non può fare. I punti fissi si
raggiungono per continuazione: spostati a passi da dove il blocco li vede fino ai loro riferimenti, con
una compensazione a ogni passo, così che nessun passo parta lontano dalla sua soluzione (una sola
compensazione con i punti fissati a metri di distanza diverge, con la calibrazione che scappa; sulle
997 foto di Tell Tuqan i target partono a 1,8 m e vengono raggiunti in 20 passi). Sui blocchi grandi
(più di 2,5 milioni di osservazioni) i passi girano su una copia con le tracce più forti, ogni foto
con la sua quota; poi le pose tornano al blocco intero, ogni punto di legame viene triangolato di
nuovo, i punti che non tornano più vengono scartati e una compensazione di tutto chiude. Le barre di
scala di controllo (2 o più, o insieme a punti di controllo) entrano allo stesso modo: i loro marker
sono fissati a `engine.sfm.bar_anchors` (vedi *Barre di scala*).
`meta["sfm"]["optimize"]` riporta `control_points`, `scale_bars`, `steps` e l'RMSE di punti e barre di
controllo e di verifica prima e dopo (`ppg optimize`).

`engine="ppg"` usa invece la nostra compensazione (sotto), con `gnss="plain"` o `"bias"` e
`rolling_shutter=True`.

### La nostra compensazione

`engine.bundle.problem_from_chunk(chunk, rec, use_markers, gnss, rolling_shutter)`,
`bundle.adjust(problem)` e `bundle.write_back(...)`: minimi quadrati sparsi a regione di fiducia
(scipy; lo jacobiano per differenze finite raggruppate secondo la struttura: un residuo dipende da una
foto, un punto, una camera), proiezione con i modelli di camera di COLMAP. Punti di controllo tirati
verso il loro riferimento secondo la loro accuratezza (non fissi), barre di scala di controllo come
distanze tra i loro marker, posizioni GNSS delle foto con un bias per volo (`bundle.flights`: per
sensore, un nuovo volo dopo 20 minuti senza foto), rolling shutter (una velocità lineare e una
angolare per foto, l'istante di un'osservazione dalla sua riga; un prior debole verso una camera ferma,
grande quanto 2 pixel di spostamento alla profondità della foto: sulle foto con otturatore globale le
velocità restano vicine a zero). Rapporto: `gnss_bias` (per volo, nel CRS), `rolling_shutter_m`. I rig
(facce delle foto a 360 gradi) vengono rifiutati: la compensazione di COLMAP li tiene.

### Pagine della texture

Una texture più grande di `page_size` viene divisa in pagine come in Metashape (`model_texture.jpg`,
`model_texture_1.jpg`...; commenti `TextureFile` e il `texnumber` di ogni faccia in `model.ply`,
come li legge MeshLab): la mappatura generica mette su ogni pagina gruppi di facce vicine, la
mappatura ortofoto un tassello della pianta (le facce ripide su pagine a parte).
`engine.texturing.texture_pages(chunk)` elenca le pagine; le esportazioni scrivono un materiale per
pagina (OBJ), le pagine e il `texnumber` (PLY), una mesh per pagina (GLB); la vista 3D le mostra una
accanto all'altra in un'unica immagine ridotta (`display_texture`); versioni, annulla, patch della
texture e l'importazione da Metashape (pagine e `texnumber` di `mesh.ply`) portano con sé ogni pagina.

### Mesh dalle mappe di profondità

`chunk.build_model(source="depth_maps", surface="arbitrary", face_count="medium", voxel_size=None,
vertex_colors=True)` costruisce la mesh direttamente dalle mappe di profondità di `build_depth_maps()`,
senza la nuvola densa (come *dati di origine: mappe di profondità*). Le mappe di profondità vengono
fuse in un campo di distanza con segno troncato (TSDF in un `VoxelBlockGrid` di Open3D: si conservano
solo i blocchi vicini alla superficie) e la superficie viene estratta con i marching cubes, con i
colori dei vertici mediati dalle foto. Un voxel conta quando lo vedono tante mappe di profondità
quante ne vuole la fusione della nuvola densa per la modalità di filtro (3 per `moderate`, 2 per
`mild`/`none`); i pezzi più piccoli dell'1 % del più grande vengono scartati. La mesh viene decimata a
`face_count` (`high`/`medium`/`low` = 1/5, 1/15, 1/45 dei punti che le mappe di profondità danno alla
loro risoluzione, oppure un numero) e salvata come `model.ply` come la mesh Poisson, così texture,
DEM, ortofoto e sezioni funzionano come prima; `surface="height_field"` ritriangola la superficie fusa
in 2,5D. `voxel_size` è in unità del chunk (metri una volta georeferenziato): per impostazione
predefinita il doppio di un pixel di mappa di profondità sulla superficie, alzato (con un avviso)
quando la superficie richiederebbe più di un quarto della memoria. La fusione gira in un processo
figlio (un crash non chiude il programma; avanzamento e annullamento funzionano come sempre) e ha
bisogno di mappe di profondità del backend `cpu`, `patchmatch` o `colmap` (OpenMVS tiene il suo
formato). `engine.tsdf.fuse_depth_maps(chunk, voxel_size=, min_views=, colors=, memory_gb=)`
restituisce la superficie fusa (`FusedMesh`, sistema interno) senza salvarla. Sulla trincea sintetica
dei test (mappe di profondità a qualità `high`) impiega circa 4 s ed è più vicina alla superficie vera
della mesh Poisson della nuvola densa: distanza mediana 0,9 mm, 90° percentile 2,2 mm (Poisson 1,0 e
2,8 mm). CLI: `ppg mesh project.ppg --source depth_maps [--voxel-size 0.005]`.

## Allineare e unire i chunk

I chunk si allineano separatamente (ciascuno nel suo sistema arbitrario), si portano in un unico
sistema, poi si uniscono, come con *Flusso di lavoro > Allinea chunk* e *Unisci chunk*.

| Chiamata | Descrizione |
| --- | --- |
| `doc.align_chunks(reference, chunks=None, method="markers", fix_scale=False, initial="auto", max_points=200_000, progress=None)` | porta `chunks` (oggetti Chunk, chiavi o etichette; predefinito: gli altri chunk abilitati) nel sistema mondo e nel CRS di `reference`; restituisce un rapporto |
| `doc.merge_chunks(chunks, label=None, merge_markers=True, merge_tie_points=True, merge_point_clouds=True, merge_models=False, progress=None)` | nuovo chunk (etichetta predefinita `Merged chunk`) nel sistema del primo chunk; quelli di partenza restano |
| `ppg align-chunks project.ppg [CHUNKS...] -r REF --method markers\|points\|cameras [--fix-scale] [--initial ...] [--json]` | riga di comando (riferimento predefinito: il chunk attivo) |
| `ppg merge-chunks project.ppg [CHUNKS...] [--label L] [--no-markers] [--no-tie-points] [--no-point-clouds] [--models] [--activate]` | riga di comando (predefinito: tutti i chunk abilitati) |

Metodi di `align_chunks`:

* `markers`: marker con la stessa etichetta nei due chunk (almeno 3, con una posizione),
  minimi quadrati (Umeyama).
* `cameras`: foto presenti in entrambi i chunk (lo stesso file, o la stessa etichetta quando è
  unica in ciascun chunk). Minimi quadrati sui centri delle camere; con 1-2 foto in comune, o
  foto lungo una linea, la rotazione viene dagli orientamenti delle camere.
* `points`: registrazione delle nuvole dense (i punti di legame per un chunk che non ne ha una).
  Posizione iniziale dai marker in comune, altrimenti dalla georeferenziazione (entrambi i chunk
  in un CRS proiettato, o riferiti alla stessa griglia locale), altrimenti dalle foto in comune,
  altrimenti da una registrazione globale (descrittori FPFH di Open3D, abbinati con un RANSAC
  seminato: serve l'extra `[mesh]`); `initial="markers"|"reference"|"cameras"|"global"` ne impone
  una. Un ICP point-to-plane con scala la affina; il risultato è accettato quando la maggior parte
  dei punti sovrapposti torna e, con i colori, la luminosità dei punti abbinati concorda
  (altrimenti `ValueError`).
* `fix_scale=True`: allineamento rigido che conserva la scala di ogni chunk; solo per chunk già
  in metri (CRS, riferimenti o barre di scala), altrimenti `ValueError`.

Cambiano solo `chunk.transform` e `chunk.crs`; foto, punti di legame, nuvola densa e mesh restano
nel sistema interno del chunk. Le stime usano sistemi locali (coordinate mondo meno un offset
arrotondato), così le coordinate di griglia nazionale/UTM non perdono precisione. Un riferimento
locale non livellato viene livellato prima (come farebbe poi `build_dem`). Se un chunk non si può
allineare, non cambia nulla. I riferimenti dei marker di un chunk in un altro CRS proiettato vengono
convertiti nel nuovo. DEM e ortofoto di un chunk allineato sono nel suo vecchio sistema: costruiscili
di nuovo (`stale_products` nel rapporto).

Il rapporto: `{"reference": {"key", "label"}, "method", "fix_scale", "crs", "levelled_reference",
"chunks": [...]}`, una voce per chunk con `key`, `label`, `method`, `correspondences`, `rmse`
(unità mondo del riferimento, metri una volta georeferenziato o scalato), `scale` (scala applicata),
`transform` (il nuovo `chunk.transform`), `delta` (vecchio mondo -> mondo del riferimento),
`stale_products`; `residuals` (per marker o foto: `error`, `dx`, `dy`, `dz`; `angle_deg` per le
foto) e `max_error` per marker/camere; `initial`, `source`/`target` (`point_cloud` o `tie_points`),
`fitness`, `overlap`, `inlier_ratio`, `color_corr`, `attempts` e `check` (residui dei marker in
comune; quando l'ICP è partito da loro ne sono anche le ancore - termini punto-punto che insieme
pesano quanto la nuvola - così una nuvola piatta o rumorosa non può scivolare via da loro,
`anchored`) per i punti. `chunk.meta["chunk_alignment"]` ne conserva un riassunto.

`merge_chunks` vuole chunk in un unico sistema (allineati, o georeferenziati in CRS proiettati; un
altro CRS viene convertito con una similarità stimata, con un avviso sopra i 5 mm). Il chunk unito
ha CRS, trasformazione e sistema interno del primo chunk, e contiene:

* sensori e camere con chiavi nuove; una foto presente in più chunk viene tenuta una volta (la
  prima copia allineata). Calibrazioni identiche restano sensori separati.
* marker uniti per etichetta (proiezioni combinate, posizioni triangolate di nuovo), oppure tutti
  conservati con `merge_markers=False`; barre di scala rimappate.
* punti di legame, nuvole dense (colori, normali, classi) e, con `merge_models=True`, mesh (colori
  dei vertici) concatenati nel sistema comune.
* un modello COLMAP delle camere unite (`engine.sfm.write_reconstruction_from_chunk`, con i punti
  di legame uniti come punti 3D), così mappe di profondità, nuvola densa, mesh, DEM e ortofoto si
  possono costruire di nuovo sul chunk unito.

DEM, ortofoto e texture non vengono uniti. `merged.meta["merge"]` elenca i chunk di origine, le
foto trovate in più di un chunk e gli avvisi.

## Prodotti da classi di punti scelte

Le classi della nuvola densa (regole, lazo, classificatore addestrato o unità stratigrafiche)
scelgono che cosa entra nei prodotti. Il rumore (`NOISE_CLASSES` = 7, 18) non fa mai un DEM o una mesh.

| Chiamata | Descrizione |
| --- | --- |
| `chunk.point_classes()` | punti per classe della nuvola densa, letti dalla sola colonna delle classi (`PointCloud.read_class_counts(path)`) |
| `chunk.build_dem(..., classes=[2])` | DEM solo da queste classi: `[2]` dà un modello del terreno (DTM) senza vegetazione, persone o attrezzi; `meta["dem"]["classes"]` |
| `chunk.build_orthomosaic(..., hide_classes=[3, 71])` | trasparente dove la superficie visibile è di queste classi; `meta["orthomosaic"]["hidden_classes"]` |
| `chunk.build_model(source="point_cloud", ..., classes=[2, 64])` | mesh da queste classi (la superficie si chiude sopra ciò che resta fuori); solo con la nuvola densa come sorgente; `meta["model"]["classes"]` |
| `sections.section(chunk, p1, p2, source=, classes=)` | una sezione attraverso la nuvola densa o il modello, limitata a queste classi |
| `sections.sections_along(..., classes=)` | sezioni trasversali perpendicolari a una linea di base, con lo stesso filtro di classi |
| `sections.plan_slice(chunk, z, classes=)` | una pianta a quota `z` tagliata nel modello, con lo stesso filtro di classi |
| `chunk.export_point_cloud(path, classes=[64])` | solo queste classi (il LAS conserva i codici delle classi) |
| `PointCloud.class_mask(classes=None, exclude=())` | maschera booleana dei punti da usare |
| `classify.classify_by_units(chunk, units, base_code=100, keep_manual=True)` | una classe per unità stratigrafica (`unit_class_name`: `US 12 (area 1)`), punti sul bordo compresi; restituisce una riga per unità |
| `classify.units_table_csv(rows, path)` | scrive le righe di `classify_by_units` in un CSV (punti e virgola) |

**`chunk.build_dem(..., classes=[2])`** interpola i vuoti sotto ciò che resta fuori.

**`chunk.build_orthomosaic(..., hide_classes=[3, 71])`** prende come superficie visibile il punto
denso più alto, su una griglia al passo dei punti e allargato di una cella;
`engine.ortho.hidden_cells(chunk, origin, shape, resolution, classes)` dà quelle celle.

**`sections.section(...)`**, `sections_along` e `plan_slice` con `classes=` prendono, dalla nuvola
densa, solo i punti di queste classi; dal modello, solo i tratti del taglio il cui punto medio è più
vicino a un punto di queste classi e i cui estremi stanno su punti di quelle classi (non le parti che
la mesh interpola sopra ciò che resta fuori).

**`classify.classify_by_units(...)`** prende codici liberi da 100 e dà lo stesso codice quando viene
lanciata di nuovo (la vecchia estensione torna a 1); restituisce per unità `name`, `code`, `sito`,
`area_s`, `us`, `points`, `area_m2` (in pianta, dei poligoni), `z_min`, `z_mean`, `z_max`.

Riga di comando: `ppg dem scavo.ppg --classes 2`, `ppg ortho scavo.ppg --hide-classes 3,71`,
`ppg mesh scavo.ppg --classes 2,64`, `ppg section scavo.ppg x1 y1 x2 y2 --classes 2`,
`ppg export scavo.ppg --cloud muri.las --classes 64`,
`ppg classify-units scavo.ppg [--pyarchinit-site SITO] [--csv us.csv] [--no-keep-manual]`
(le unità disegnate nel progetto, o i poligoni US di un sito PyArchInit).

## Versioni dei prodotti

`core.versions` conserva i risultati precedenti di nuvola densa, modello, DEM e ortofoto per
confrontarli. La versione *attiva* è il prodotto che ogni strumento usa (i file soliti del chunk); le
versioni conservate stanno in `versions/<prodotto>/<id>/` dentro la cartella del chunk, elencate in
`chunk.meta["versions"]` con nome, data e i parametri con cui sono state costruite.

| Chiamata | Descrizione |
| --- | --- |
| `versions.keep_active(chunk, product, move=False)` | mette la versione attiva tra quelle conservate; restituisce la voce |
| `versions.restore(chunk, product, entry)` | annulla un `keep_active(move=True)` (una costruzione fallita) |
| `versions.set_active_name(chunk, product, name)` | dà un nome alla versione attiva (predefinito `DEM del 16/10 11:20 (classi 2)`) |
| `versions.active_info(chunk, product)` | nome e data della versione attiva |
| `versions.versions(chunk, product)` | le versioni conservate |
| `versions.version_file(chunk, product, id)` | il file principale di una versione conservata |
| `versions.activate(chunk, product, id)` | rende attiva una versione conservata; quella attiva viene conservata al suo posto |
| `versions.rename(chunk, product, id_or_None, name)` | rinomina una versione conservata (`None`: quella attiva) |
| `versions.delete(chunk, product, id)` | elimina una versione conservata e i suoi file |
| `versions.dem_difference(chunk, id, out=None)` | DEM attivo meno uno conservato, sulla griglia di quello attivo (GeoTIFF float32) |

**`versions.keep_active(...)`** copia la versione attiva, oppure la sposta con `move` quando i suoi
file stanno dentro la cartella del chunk: un file di un altro programma viene sempre copiato.

**`versions.dem_difference(...)`**: superficie meno terreno = altezza di muri e vegetazione, dopo
meno prima = profondità scavata.

`chunk.build_depth_maps(..., skip_unreadable=True)` lascia fuori le foto che non si possono leggere
(`chunk.unreadable_photos()`: mancanti, segnaposto vuoti di file tenuti solo online, non immagini);
senza, sollevano `engine.visibility.UnreadablePhotos` (importabile anche da `engine.dense`).
`build_texture` e `build_orthomosaic` prendono la stessa opzione;
`chunk.relink_photos(folder, only_unreadable=False)` fa puntare le foto ai file con gli stessi nomi
in un'altra cartella (COLMAP poi le legge dalla loro cartella comune, o da una cartella di link quando
sono sparse in più cartelle, `engine.sfm.photo_folder`). Il modello COLMAP scritto da pose importate
fa riferimento solo a punti con due osservazioni; `engine.sfm.repair_reconstruction(chunk)` ripara i
modelli scritti dalle versioni precedenti (fatto prima delle mappe di profondità e
dell'ottimizzazione delle camere).

## Confidenza, pulizia e ritaglio

| Chiamata | Descrizione |
| --- | --- |
| `chunk.compute_confidence("point_cloud" or "model", tol=0.01)` | le mappe di profondità che concordano su ogni punto o vertice, conservate come proprietà `confidence` della PLY; restituisce le statistiche |
| `engine.confidence.depth_map_support(chunk, points, tol, abs_tol=None)` | lo stesso conteggio per punti qualsiasi del sistema interno |
| `chunk.clean_point_cloud(min_confidence=, remove_classes=, statistical=(k, std), sparse=(radius, n), groups=(distance, n), color=((r, g, b), tol))` | toglie ciò che `engine.cleaning.point_filters` seleziona (distanze 0 = automatiche dal passo dei punti); restituisce i conteggi per filtro |
| `chunk.clean_model(min_confidence=, min_component=, max_edge=, repair=True, fill_holes=, smooth=, target_faces=)` | pulizia del modello (`engine.cleaning.face_filters` per i conteggi) |
| `chunk.clean_tie_points(max_reprojection_error=, min_image_count=, max_reconstruction_uncertainty=, max_projection_accuracy=, optimize=False)` | selezione graduale (`engine.tiepoints.tie_point_metrics`, `select_tie_points`, `remove_tie_points`) |
| `engine.cleaning.delete_points(chunk, mask, keep_selected=False)` | l'elimina / ritaglia selezione di Metashape sulla nuvola densa |
| `engine.cleaning.delete_faces(...)` | lo stesso sulle facce del modello |
| `chunk.export_dem(path, region=)` | il DEM tagliato con un poligono nel CRS del chunk |
| `chunk.export_orthomosaic(path, region=)` | l'ortofoto tagliata con un poligono |
| `chunk.export_model(path, region=, zmin=, zmax=)` | il modello tagliato con un poligono e tra due quote |
| `chunk.export_point_cloud(path, region=, zmin=, zmax=)` | la nuvola densa tagliata con un poligono e tra due quote |
| `chunk.crop_dem(region)` | lo stesso taglio, scritto nel progetto |
| `chunk.crop_orthomosaic(region)` | lo stesso per l'ortofoto |
| `chunk.crop_model(region, zmin, zmax)` | lo stesso per il modello |
| `chunk.crop_point_cloud(region=)` | lo stesso per la nuvola densa |
| `chunk.build_orthomosaic(..., workers=None, max_angle=70.0)` | l'ortofoto tassello per tassello, i tasselli in parallelo |

**`chunk.compute_confidence(...)`** conta le mappe di profondità che concordano entro `tol` della
distanza (mezzo lato per un vertice); il conteggio è anche una dimensione extra bytes delle
esportazioni LAS.

**`chunk.export_dem(path, region=)`** e le altre esportazioni prendono la regione come anello
`(N, 2)`, riquadro, WKT, GeoJSON o geometria shapely (`engine.crop.as_polygon`).

**`chunk.crop_dem(region)`** e gli altri ritagli non sovrascrivono mai un prodotto che è un file di
un altro programma (`chunk.own_product_path`).

**`chunk.build_orthomosaic(..., workers=None, max_angle=70.0)`** costruisce l'ortofoto tassello per
tassello (la superficie letta una finestra alla volta, ogni tassello scritto in una volta, le overview
alla fine): qualsiasi dimensione, i tasselli in parallelo; ogni cella prende la migliore delle foto
che la inquadrano (le quattro migliori vengono provate una dopo l'altra contro le occlusioni), così la
scelta non dipende mai dal tassello intorno; le celle che ogni foto vede da oltre `max_angle` gradi
dalla verticale restano trasparenti.

## Ortofoto senza cuciture e correzione del colore

| Chiamata | Descrizione |
| --- | --- |
| `chunk.build_orthomosaic(..., blending="mosaic", blend_sigma=8.0)` | come le foto vengono fuse: `mosaic`, `disabled` (una foto per cella) o `average` |
| `chunk.calibrate_colours(vignetting=True, samples=20000)` | guadagno di esposizione di ogni foto (per canale) e vignettatura di ogni obiettivo, conservati in `chunk.meta["colour"]` |
| `engine.colour.estimate(chunk)` | la stessa misura come funzione del modulo |
| `build_orthomosaic(colour_correction=None)` | None: la calibrazione del chunk se c'è; True: misurata prima se manca; False: nessuna |
| `build_texture(colour_correction=None)` | la stessa opzione per la texture |
| `build_orthomosaic(ghosting=False)` | con `True`, persone e veicoli visti da poche foto vengono lasciati fuori |
| `build_orthomosaic(fill_holes=False)` | i buchi di al massimo 100 celle che nessuna foto vede riempiti dai loro bordi (`cv2.inpaint`, solo le celle dello stesso gruppo); i buchi più grandi restano trasparenti |

**`chunk.build_orthomosaic(..., blending="mosaic", blend_sigma=8.0)`**: `mosaic` prende i dettagli
di ogni cella dalla sua foto migliore, le basse frequenze sfumate attraverso le cuciture su qualche
cella (`blend_sigma`, gaussiana), così esposizioni diverse non lasciano un bordo e niente viene
sfocato; `disabled`: una foto per cella; `average`: ogni foto pesata per quanto bene vede la cella.
Una patch non cambia mai i pixel intorno a sé: fuori dalle patch i pesi vengono dalle foto che le
celle prenderebbero senza alcuna patch, dentro una patch dalle sue celle (`engine.ortho._blend`).

**`chunk.calibrate_colours(...)`** stima la vignettatura come `v(r) = b1 r^2 + b2 r^4 + b3 r^6` sul
raggio rapportato alla mezza diagonale, da punti della superficie visti da più foto, minimi quadrati
su tutte le osservazioni insieme (`scipy.sparse.linalg.lsqr`, valori anomali lasciati fuori tra un
giro e l'altro, guadagno logaritmico medio zero); `chunk.meta["colour"]` conserva il risultato con la
dispersione dei colori prima/dopo.

**`build_orthomosaic(colour_correction=None)`** e `build_texture` applicano la correzione
(`engine.colour.Correction.correct(cam, image)`) man mano che le foto vengono lette.

**`build_orthomosaic(ghosting=False)`**: dove tre o più foto candidate vedono una cella, si prende la
migliore che concorda con il loro colore mediano (entro 30 livelli).

## Qualità delle immagini e video

| Chiamata | Descrizione |
| --- | --- |
| `engine.quality.estimate_quality(chunk, cams=None)` | nitidezza di ogni foto rispetto alla più nitida del chunk (0-1, `camera.meta["quality"]`) |
| `engine.quality.low_quality(chunk, threshold=0.5)` | le foto mosse o sfocate |
| `engine.quality.sharpness(image)` | la sola misura |
| `engine.video.add_video(chunk, video, folder=None, srt=None, overlap=0.7, every=None, max_frames=None)` | fotogrammi di un video aggiunti come foto, uno nuovo quando la vista si sposta oppure uno ogni `every` secondi |
| `engine.video.extract_frames(video, folder, ...)` | i soli fotogrammi, scritti come file |
| `engine.video.read_srt(path)` | le posizioni di una traccia `.SRT` DJI |
| `engine.video.position_at(track, t)` | la posizione della traccia a un istante, interpolata |

**`engine.quality.estimate_quality(...)`** misura, per cella di una griglia 6 x 6 con texture, il
dettaglio più fine rispetto al dettaglio largo qualche pixel, lungo x e lungo y, il minore dei due
(il mosso in una direzione conta), 80° percentile delle celle; indipendente da quanta texture ha la
scena.

**`engine.video.add_video(...)`** aggiunge un nuovo fotogramma quando la vista si è spostata di
`1 - overlap` della sua larghezza (spostamento mediano delle corrispondenze ORB; un fotogramma con
troppo poche corrispondenze conta solo se nitido), il più nitido nei 0,4 s successivi; oppure uno
ogni `every` secondi. Il `.SRT` DJI accanto al video (`[latitude] [longitude] [abs_alt]`,
`GPS(lon,lat,alt)` o `GPS (lon, lat, alt)`) dà a ogni fotogramma la sua posizione (EPSG:4326,
interpolata nel tempo).

## Obiettivi fisheye e foto a 360 gradi

| Chiamata | Descrizione |
| --- | --- |
| `chunk.add_photos(...)` | un obiettivo fisheye riceve un sensore `OPENCV_FISHEYE` (Kannala-Brandt: `fx, fy, cx, cy, k1-k4`) |
| `chunk.set_sensor_model(sensor_key, model)` | un altro modello di camera per un sensore (`OPENCV`, `OPENCV_FISHEYE`, `FULL_OPENCV`, `PINHOLE`, qualsiasi modello COLMAP): focale e punto principale conservati, distorsione azzerata; dopo, allinea di nuovo |
| `engine.panorama.add_panoramas(chunk, paths, size=None, fov=100, faces=None)` | le foto equirettangolari diventano le facce di un cubo: foto pinhole con un unico sensore `PINHOLE` fisso |
| allineamento | le facce di ogni panorama sono un frame di un rig COLMAP (`engine.panorama.rig_configs`) |
| `engine.panorama.split_panorama(path, folder=None, size=None, fov=100, faces=None)` | le facce di un solo panorama, scritte come file |
| `engine.panorama.face_maps(...)` | le mappe `cv2.remap` di una faccia |
| `engine.panorama.face_rotation(yaw, pitch)` | la rotazione dalla camera di una faccia al sistema del panorama |

**`chunk.add_photos(...)`** riconosce un obiettivo il cui nome nell'EXIF dice fisheye (`LensModel`,
o il modello della fotocamera), oppure una focale equivalente a 35 mm fino a 15 mm; il sensore
viene allineato con una sola focale (`RADIAL_FISHEYE`) a meno che una calibrazione non dica
altrimenti. Le proiezioni lontane dall'asse non vengono più tagliate al riquadro pinhole.

**`engine.panorama.add_panoramas(...)`** prende foto equirettangolari (larghezza doppia
dell'altezza; le altre restano fuori) e scrive le facce di un cubo (`front`, `right`, `back`,
`left`, `up`, `down`, o quelle in `faces`): foto pinhole di `fov` gradi (più di 90: le facce vicine
si sovrappongono), `size` px di lato (predefinito: la risoluzione del panorama al loro centro), in
`facce_360/<faccia>/<panorama>.jpg` accanto ai panorami, un unico sensore `PINHOLE` fisso;
`camera.meta["panorama"]`, `meta["face"]`; il GPS del panorama è il riferimento delle sue facce.

**allineamento**: un rig per cartella `facce_360`, facce alla loro rotazione nota, senza
traslazione: tengono un solo centro e la loro rotazione relativa, la loro calibrazione non viene
affinata (nemmeno in `optimize_cameras`), le coppie di facce dello stesso panorama non vengono
abbinate. Le mappe di profondità accoppiano una faccia con le facce di altri panorami che guardano
fino a 50 gradi più in là (i panorami sono girati in qualunque modo).

## Media di PyArchInit

`PyArchInit.us_media(sito, area, us, unita_tipo=None)` e `site_media(sito)`: i media collegati a
un'unità stratigrafica o a un sito (`media_to_entity_table`), ciascuno con `id_media`, `filename`,
`filetype`, `mediatype`, `descrizione` e i percorsi completi dell'originale (`path`), della miniatura
(`thumb`) e della copia ridotta (`resize`) - None dove il file non è su questo computer o su un
archivio remoto.

## Scansioni laser

| Chiamata | Descrizione |
| --- | --- |
| `io.scans.read_ptx(path)` | le scansioni di un file PTX: la griglia di ogni scansione con la sua registrazione 4 x 4 (i ritorni vuoti restano fuori) |
| `io.scans.read_e57(path)` | le scansioni `data3D` di un file E57 con le loro pose, cartesiane o sferiche (extra `scans`: pye57) |
| `io.scans.read_scans(path)` | l'uno o l'altro lettore, dall'estensione del file |
| `io.scans.import_scans(doc, paths, label=None, crs=None, max_points=None)` | un nuovo chunk la cui nuvola densa è l'insieme delle scansioni nel loro sistema registrato; `meta["scans"]`, `meta["metric"]` |
| `doc.align_chunks(photo_chunk, [scan_chunk], method="points", fix_scale=True)` | la scansione sul chunk fotogrammetrico (registrazione globale, poi ICP); non serve quando entrambi sono nello stesso CRS |
| `io.scans.colour_from_photos(target, photos)` | i punti di `target` prendono il colore della foto allineata di `photos` che li vede più da vicino |

**`io.scans.read_ptx(path)`**, `read_e57` e `read_scans` restituiscono le scansioni di un file come
oggetti `Scan`: `xyz` registrate, `rgb`, `intensity` 0-1, `position` dello scanner.

**`io.scans.import_scans(...)`** conserva i colori, altrimenti l'intensità in grigio;
`meta["metric"]` vuol dire metri anche in coordinate locali (*Allinea chunk* senza scala lo accetta).

**`doc.align_chunks(photo_chunk, [scan_chunk], method="points", fix_scale=True)`**: sulla trincea
sintetica le quote concordano a 0,2 mm; in pianta i muri della nuvola densa spostano l'adattamento di
qualche millimetro.

**`io.scans.colour_from_photos(...)`** sceglie tra le foto dove il modello non nasconde il punto
(tolleranza 2 %).

## Esportazioni per il web

| Chiamata | Descrizione |
| --- | --- |
| `chunk.export_orthomosaic(path, region=None, cog=None, quality=85)` | l'ortofoto come Cloud Optimized GeoTIFF con la compressione `cog` |
| `chunk.export_dem(path, region=None, cog=None)` | il DEM come Cloud Optimized GeoTIFF |
| `io.web.export_cog(src, dst, compression="auto")` | qualsiasi GeoTIFF come Cloud Optimized GeoTIFF |
| `chunk.export_point_cloud("x.copc.laz")` | la nuvola densa come Cloud Optimized Point Cloud 1.0 |
| `io.copc.write_copc(path, xyz, rgb, classification, confidence, crs_wkt)` | lo scrittore COPC per punti qualsiasi |
| `chunk.export_model("x.glb")` | glTF binario con la texture (o i colori dei vertici) |
| `io.web.export_glb(chunk, path)` | lo stesso come funzione del modulo |
| `engine.normalmap.light_model(chunk, target_faces=50000, texture_size=4096, folder=None)` | un modello leggero per il web e i motori di gioco: `light.glb`, `light.obj` + `.mtl`, `light_color.jpg`, `light_normal.png` |
| `engine.normalmap.bake_maps(full_verts, full_faces, light_verts, light_faces, ...)` | la sola cottura delle mappe |
| `io.flythrough.orbit_video(chunk, path, kind=None, seconds=12, fps=25, size=(1280, 720), pitch=35, zoom=1, turns=1, backend="auto")` | un MP4 di una camera che orbita attorno al modello o alla nuvola |
| `chunk.export_3dtiles(folder, max_faces=150000)` | Cesium 3D Tiles 1.1: `tileset.json` e tasselli GLB in un quadtree in pianta |
| `io.web.export_3dtiles` | lo stesso come funzione del modulo |
| `chunk.export_i3s(path, mode="global", max_faces=20000, texture_size=2048)` | pacchetto di scene layer I3S 1.7 Integrated Mesh (`.slpk`: ArcGIS Pro, ArcGIS Online ed Earth, CesiumJS, loaders.gl) |
| `io.i3s.export_slpk(chunk, path, ...)` | lo stesso come funzione del modulo |
| `io.i3s.read_slpk(path)` | rilegge un pacchetto (layer, nodi, geometrie) |

**`chunk.export_orthomosaic(..., cog=)`**, `export_dem` ed `export_cog` scrivono un Cloud Optimized
GeoTIFF (a tasselli, con overview, letto a pezzi attraverso la rete): `jpeg` (l'alfa dell'ortofoto
diventa una maschera), `webp` (conserva l'alfa), `zstd`/`deflate`/`lzw` senza perdita con il
predittore (in virgola mobile per i DEM).

**`chunk.export_point_cloud("x.copc.laz")`** scrive un LAZ 1.4 (formato di punto 7, confidenza come
extra byte) ordinato in un octree (ogni nodo un punto per cella di una griglia 128^3, il resto ai
suoi figli, fino a 100.000 punti), un chunk LAZ per nodo, il VLR `copc info` e l'EVLR della
gerarchia; QGIS, Potree e il `CopcReader` di laspy leggono solo ciò che mostrano.

**`chunk.export_model("x.glb")`** tiene le coordinate attorno al centro del modello (la precisione
singola conserva i millimetri), il centro e il CRS negli `extras` della scena e in `x.json`.

**`engine.normalmap.light_model(...)`**: decimazione quadrica (Open3D), facce in celle di un atlante
e, per ogni texel, la superficie leggera spostata lungo la sua normale sul modello completo - il suo
colore (texture o colori dei vertici) in `light_color.jpg`, la sua normale nello spazio tangente
della faccia leggera (x lungo U, y lungo V, verde verso l'alto come OpenGL, come vuole il glTF) in
`light_normal.png`; `light.glb` (materiale PBR con entrambe, coordinate locali), `light.obj` + `.mtl`
(`map_Kd`, `norm`).

**`engine.normalmap.bake_maps(...)`**: su una superficie increspata cotta su due triangoli le normali
decodificate stanno entro 3 gradi (mediana).

**`io.flythrough.orbit_video(...)`** disegna con VTK fuori schermo (con la texture) o con il renderer
software della vista 3D (punti campionati sulla superficie, con z-buffer), scrittore OpenCV (H.264 o
MPEG-4).

**`chunk.export_3dtiles(...)`**: a ogni livello il modello con meno facce (colori dei vertici,
clustering), le foglie a pieno dettaglio; la trasformazione della radice da est-nord-alto al centro
del modello alle coordinate geocentriche (pyproj; quote così come sono).

**`chunk.export_i3s(...)`** costruisce un quadtree in pianta con livelli di dettaglio (foglie con al
massimo `max_faces` facce, gli altri nodi raggruppati a quel numero, una radice senza mesh), ogni
nodo con una texture propria, una potenza di due dimensionata su ciò che mostra fino a
`texture_size` (una foglia le cui facce stanno insieme nella texture del modello, come con una
mappatura ortofoto, prende quella parte così com'è e cuoce accanto solo le sue poche altre facce; le
altre foglie cuociono le pagine della texture o i colori dei vertici faccia per faccia; gli altri nodi
proiettano sul modello), pagine di nodi, buffer di geometria gzip, la tabella degli hash MD5 dei
percorsi per ultima; `mode="global"` WGS84 con riquadri orientati est-nord-alto e normali
geocentriche, `mode="local"` il CRS proiettato del chunk (serve un codice EPSG in metri).

## Misure

| Chiamata | Descrizione |
| --- | --- |
| `archaeo.measures.surface_area(dem_path, polygon, model=None)` | dentro un poligono in pianta: aree `plan` e `surface` dal DEM, `mean_slope`, `z_min`, `z_max`; con una mesh anche `model_surface`, `model_plan`, `model_faces` |
| `measures.slope_aspect_at(dem_path, x, y)` | pendenza ed esposizione (gradi; esposizione in senso orario da nord, verso dove il terreno scende, -1 piatto) dalle celle 3 x 3 (Horn) |
| `measures.aspect_raster(dem_path, out)` | raster dell'esposizione |
| `archaeo.terrain.slope_degrees(dem_path, out)` | raster della pendenza |
| `measures.ground_surface(xyz, classes, cell=None)` | il terreno su una griglia dai punti della classe 2 (mediana di ogni cella, buchi interpolati) |
| `measures.height_above_ground(xyz, classes)` | l'altezza di ogni punto sopra quel terreno |
| `archaeo.timeline.phases(doc)` | i chunk con un DEM come fasi dello scavo in ordine di data |
| `timeline.phase_date(chunk)` | la data di una fase (`meta["date"]`, altrimenti la data EXIF più vecchia delle foto) |
| `timeline.set_phase_date(chunk, date)` | la data di una fase impostata a mano |
| `timeline.removed(doc, polygon=None)` | volume asportato e aggiunto da ogni fase alla successiva, e dentro ogni unità disegnata sulla fase precedente |
| `timeline.suggested_relations(doc)` | unità esposte prima che si sovrappongono in pianta a unità esposte dopo, come `Copre` / `Coperto da` |
| `timeline.timeline(doc)` | il rapporto completo |
| `timeline.timeline_csv(report)` | il rapporto come CSV |
| `timeline.write_relations(pa, relations)` | i rapporti suggeriti aggiunti ai `rapporti` delle schede US (nei due versi; quelli già presenti restano fuori) |
| `pa.add_relationships(sito, area, us, rels)` | rapporti aggiunti ai `rapporti` di una scheda US |
| `archaeo.us_proposal.propose_for_chunk(chunk, units, first_us, area=None, sito=None, step=0.02, cuts=True, lang="it")` | US proposte per le unità senza numero: misure, contatti, numeri e rapporti, ogni rapporto con il suo `why` |
| `us_proposal.write_proposal(pa, proposal, sito)` | le unità scelte come schede US (`create_us`: `node_uuid`, `entity_uuid`; le schede esistenti restano) e i loro rapporti nei due versi |
| `pa.next_us(sito)` | il numero dopo la US più alta di un sito |
| `chunk.provenance(product, path=None, operator=None)` | W3C PROV-O in JSON-LD: il prodotto, l'attività che l'ha fatto, da che cosa deriva, gli agenti |
| `core.paradata.provenance(...)` | lo stesso come funzione del modulo |
| `paradata.write_provenance(chunk, product, path)` | `<file>.prov.jsonld` accanto a un'esportazione o a una copia pubblicata |
| `paradata.summary(chunk, product)` | un riassunto di una riga (la nota di PyArchInit) |
| `io.qfield.export_package(chunk, folder, resolution=0.02, project=True)` | pacchetto QField: `ortofoto.tif`, `dem.tif`, `rilievo.gpkg`, `<chunk>.qgs` e `ppg_qfield.json` |
| `qfield.read_edits(folder)` | le unità e le quote aggiunte, cambiate ed eliminate sul tablet |
| `qfield.import_edits(chunk, folder)` | quelle modifiche applicate a `chunk.meta["drawings"]` |
| `io.gpkg.read_table(path, table)` | gli elementi di una tabella GeoPackage letti con `sqlite3` |
| `gpkg.geometry(blob)` | una geometria dal formato binario GeoPackage |
| `archaeo.munsell.calibrate_chart(rgb)` | un ColorChecker Classic trovato in un'immagine (`mcc` di OpenCV): la matrice di correzione 3 x 3 in luce lineare (`ccm` di OpenCV, perdita CIEDE2000) |
| `munsell.correct(rgb, ccm)` | la matrice applicata ai colori |
| `munsell.munsell(rgb)` | notazione Munsell (illuminante C, colour-science) e chip più vicino della tavola dei suoli |
| `munsell.area_colour(raster, polygon, ccm=None)` | il colore mediano di un raster dentro un poligono, ombre e riflessi lasciati fuori |
| `archaeo.stones.find_stones(raster, segmenter, bounds=None, min_size=0.05, max_size=1.0)` | disegno pietra a pietra: le maschere automatiche di SAM su qualsiasi raster RGB a tasselli, una partizione, poi `keep_stones` |
| `stones.export_stones(stones, path, scale=20)` | i contorni come DXF (layer `PIETRE`), SVG in scala 1:`scale` (millimetri sulla carta) o GeoJSON |
| `ai.sam.auto_units(..., path=raster)` | segmenta qualsiasi raster |
| `chunk.build_relief(method="svf", radius=None, directions=16, min_size=None, max_size=None, scaling=2.0)` | visualizzazione del rilievo del DEM, prodotto `method` in `<data>/terrain/`: `svf`, `openness_pos`, `openness_neg`, `slrm`, `msrm`, `multi_hillshade` |
| `archaeo.relief.build_relief(dem_path, out, method, ...)` | lo stesso su qualsiasi file DEM (tasselli con il margine che ogni metodo richiede, più thread; il risultato è quello dell'intero DEM) |
| `relief.compute(z, resolution, method, ...)` | lo stesso su un array |
| `measures.ground_dem(chunk)` | un DTM conservato come versione del DEM (costruito dalla sola classe 2) |
| `measures.ground_dem_at(path, x, y)` | la sua quota in un punto |

**`archaeo.measures.surface_area(...)`** conta ogni cella `sqrt(1 + gx^2 + gy^2)` volte, le celle di
bordo per la frazione interna; la mesh deve essere nelle stesse coordinate e `model_faces` sono le
facce con il centro all'interno.

**`archaeo.us_proposal.propose_for_chunk(...)`** esegue `measure_units` (area, perimetro, quote dal
DEM, colore Munsell, classe maggioritaria della nuvola classificata), `contacts` (coppie che si
toccano: lunghezza, frazione di ogni contorno, inclusione, gradino del DEM attraverso il contatto da
una striscia su ciascun lato) e `propose` (numeri dall'unità più alta, USM dalla classe della
muratura, riempimenti dei tagli con il loro taglio, *Copre* dal gradino, *Si appoggia a* contro la
muratura; ogni rapporto con il suo `why`).

**`chunk.provenance(...)`** descrive il prodotto (file, SHA-256, dimensione, CRS, risoluzione,
versione attiva) come `prov:Entity`, la `prov:Activity` che l'ha fatto con le sue impostazioni, le
entità da cui deriva risalendo fino alle foto e alla georeferenziazione (RMSE di controllo e di
verifica), il programma e l'operatore come agenti.

**`paradata.write_provenance(...)`** e `summary`: `pa.publish_orthomosaic/dem/model` scrivono
entrambi.

**`io.qfield.export_package(...)`**: `ortofoto.tif` è un COG JPEG alla `resolution`, `rilievo.gpkg`
ha i layer `unita` e `quote` con `ppg_id` e le colonne di PyArchInit, `<chunk>.qgs` è scritto dal
Python di QGIS (percorsi relativi, mappe di valori) e `ppg_qfield.json` dice che cosa è stato
esportato.

**`munsell.munsell(rgb)`** riprova e, se non converge, prende il chip più vicino in CIELAB.

**`archaeo.stones.find_stones(...)`** legge l'ortofoto o l'ortofoto di un prospetto; `keep_stones`
tiene un diametro equivalente compreso tra le due dimensioni e una solidità >= 0,75.

**`chunk.build_relief(...)`** conserva i parametri e i percentili 2°/98° in `meta["relief"]`: `svf`
sky-view factor (1 - seno medio dell'angolo dell'orizzonte), `openness_pos` / `openness_neg`
(angolo zenitale medio dell'orizzonte, gradi), `slrm` (DEM meno la sua media sul raggio, metri),
`msrm` (rilievo multiscala: differenza media dei livelli passa-basso di raggio `k ** scaling` da
`min_size` a `max_size`), `multi_hillshade` (0-1); raggio predefinito circa 20 celle.

## Diagnostica dell'allineamento, calibrazione, marker

| Chiamata | Descrizione |
| --- | --- |
| `engine.diagnostics.match_graph(chunk, verified=True)` | `{(chiave camera a, b): corrispondenze}` di ogni coppia abbinata, dal database COLMAP; anche per le foto che non si sono allineate |
| `diagnostics.components(keys, graph, min_matches=1)` | gruppi di foto collegate da coppie con abbastanza corrispondenze, il più grande per primo (gruppi separati, foto isolate) |
| `diagnostics.pair_matches(chunk, a, b)` | i punti chiave delle due foto e le corrispondenze candidate e verificate (coppie di indici) |
| `diagnostics.shared_tie_points(chunk, a, b)` | i punti di legame visti da entrambe le foto |
| `diagnostics.residuals(chunk, key)` | pixel misurati e riproiettati dei punti di legame di una foto |
| `Sensor.converted(model)` | i parametri in un altro modello di camera (focale e punto principale conservati); `Chunk.set_sensor_model` lo usa |
| `Sensor.fixed` | la calibrazione è bloccata: allineamento (`constant_cameras`) e ottimizzazione delle camere non la affinano, come per le facce delle foto a 360 gradi |
| `chunk.merge_markers(markers, label=None)` | marker che sono lo stesso punto fusi in uno: il primo tiene la sua chiave |
| `chunk.remove_markers(markers)` | marker (oggetti, chiavi o etichette) tolti insieme alle barre di scala che finiscono su di loro |

**`engine.diagnostics.match_graph(...)`** conta le corrispondenze coerenti con la geometria della
coppia, altrimenti (`verified=False`) tutte le candidate.

**`Sensor.fixed`**: le importazioni non lo impostano più (la loro calibrazione è il punto di partenza
dell'allineamento).

**`chunk.merge_markers(...)`**: il primo marker prende le misure di tutti (in una foto misurata in
più d'uno, quella messa a mano), il primo riferimento, le foto escluse; le barre di scala che
finivano sugli altri finiscono su di lui.

## Annulla e ripeti (interfaccia)

`gui.undo.UndoHistory(get_state, set_state, get_chunk, limit=50, file_limit=10)`: `record(text=None)`
dopo una modifica conserva le parti di `get_state()` (serializzabili in JSON) che differiscono
dall'ultima istantanea; `backup(products)` prima di una modifica copia i file di quei prodotti del
chunk (`gui.undo.clone`: `clonefile` su macOS, altrimenti una copia) in `<dati chunk>/undo/`, e il
`record` successivo li conserva; `undo()`/`redo()` restituiscono le parti a
`set_state(parts, names, products)` e scambiano i file; `rebase()` dopo un'elaborazione (non una
modifica); `reset()` per un altro progetto o chunk (copie eliminate). La finestra registra a ogni
cambiamento del progetto, `run_task(..., undo=True)` per le modifiche fatte in background.

## Elaborazione batch

| Chiamata | Descrizione |
| --- | --- |
| `batch.step(op, chunks="all", **params)` | un passo: una delle `batch.OPERATIONS` con i suoi parametri (i predefiniti completati, i nomi sconosciuti rifiutati); `chunks`: `"all"`, `"enabled"` o etichette/chiavi |
| `batch.run_batch(doc, steps, resume=True, retries=1, on_error="continue")` | ogni passo su tutti i suoi chunk, poi il successivo; restituisce `done`, `failed` (`"chunk:step"` -> errore), `skipped`, `seconds` |
| `batch.load_steps(path)` | i passi di un file batch (JSON) |
| `batch.save_steps(steps, path)` | scrive un file batch |
| `batch.pending(doc, steps=None)` | lo stato di un batch non finito |
| `batch.targets(doc, chunks)` | i chunk su cui gira un passo |
| `batch.run_step(doc, chunk, op, params)` | un'operazione su un chunk |

**`batch.step(...)`**: le `batch.OPERATIONS` sono `detect_markers`, `import_reference`, `align_photos`,
`match_photos`, `align_cameras`, `update_transform`, `optimize_cameras`, `field_check`,
`build_depth_maps`, `build_point_cloud`, `build_model`, `build_texture`, `build_dem`,
`build_orthomosaic`, `calibrate_colours`, `estimate_precision`, `export_point_cloud`,
`export_model`, `export_dem`, `export_orthomosaic`, `generate_report`. I percorsi possono usare
`{project}`, `{name}`, `{chunk}`.

**`batch.run_batch(...)`** salva il progetto dopo ogni passo di ogni chunk con lo stato in
`<progetto>.ppg.files/batch.json`; `resume`: un batch non finito con gli stessi passi prosegue (i
passi fatti non vengono rilanciati, quelli falliti vengono riprovati); un passo che fallisce viene
riprovato `retries` volte, poi i passi successivi del suo chunk vengono saltati (oppure `BatchError`
con `on_error="stop"`).

## Controllo sul campo prima di lasciare il sito

| Chiamata | Descrizione |
| --- | --- |
| `engine.fieldcheck.field_check(chunk, min_photos=3, resolution=None, quality=True)` | subito dopo l'allineamento: le foto non allineate o mosse, i marker misurati in troppo poche foto, la copertura di ogni cella; rapporto in `chunk.meta["field_check"]` |
| `engine.fieldcheck.coverage(chunk, resolution=None)` | i soli conteggi: `(counts, transform, heights)` |

**`engine.fieldcheck.field_check(...)`** elenca le foto non allineate, quelle mosse (qualità delle
immagini stimata dove manca), i marker misurati in nessuna foto (con un riferimento), in una sola o
in due soltanto, e la copertura: su una griglia sopra il 98 % centrale dei punti di legame (400 celle
lungo il lato più lungo per impostazione predefinita), alla quota del DEM o dei punti di legame della
cella (le celle vuote interpolate), quante foto allineate inquadrano ogni cella (senza occlusione:
veloce); un GeoTIFF di conteggi (prodotto `coverage`, -1 fuori dall'area). Il rapporto in
`chunk.meta["field_check"]` ha `weak_fraction`, `weak_area` m2, `median_photos`, `unaligned`,
`blurred`, `markers`, `tie_points_weak`.

## Precisione dell'allineamento

| Chiamata | Descrizione |
| --- | --- |
| `chunk.estimate_precision(marker_px=0.5)` | deviazioni standard in metri lungo X, Y, Z del CRS di camere, punti di legame e marker, dalla covarianza della compensazione di COLMAP |
| `engine.precision.estimate_precision(chunk, ...)` | lo stesso come funzione del modulo |
| `engine.precision.dem_precision(chunk, max_side=1024)` | sigma Z dei punti di legame interpolata sul DEM, prodotto GeoTIFF `dem_precision` |
| `engine.precision.precision_at(chunk, x, y)` | sigma Z del DEM in un punto |
| `engine.precision.tie_point_precision(chunk)` | i punti di legame con la loro |
| quote | la precisione del DEM nel punto in cui si prende una quota viene conservata insieme alla quota (`precisione_q`) |
| `bim.ifc.export_chunk_ifc(..., quote=...)` | le quote nell'IFC come `IfcAnnotation` (`SpotHeight`) con `Pset_PyArchInit_Quota` (Quota, Precisione, US, ...) |
| `bim.ifc.spot_heights(quote)` | le quote del progetto come punti per l'esportazione IFC |

**`chunk.estimate_precision(...)`** imposta la compensazione come fa l'ottimizzazione delle camere
(punti di controllo tenuti al loro riferimento, altrimenti tre punti di legame; calibrazione
bloccata) e prende la covarianza delle pose delle camere di COLMAP
(`pycolmap.estimate_ba_covariance`; spazio tangente di cam_from_world, rotazione perturbata come
`Exp(2d) R`, traslazione sommata). Camere: covarianza del centro. Punti di legame e marker (due foto o
più): la covarianza completa del punto triangolato, `s^2 H^-1 + H^-1 H_pc S_cc H_cp H^-1` - le sue
misure (`s` = l'errore di riproiezione della compensazione, `marker_px` per i marker) e
l'incertezza delle camere che lo vedono. Datum: l'accuratezza dei punti di controllo (o delle
posizioni delle camere che collocano il chunk), `a^2 (1 + d^2 / r^2)` per asse. Deviazioni standard
in metri lungo X, Y, Z del CRS: `camera.meta["precision"]`, `chunk.meta["precision"]` (`datum`,
`sigma0_px`, mediane, `markers`), punti di legame in `<data>/precision/tie_points.npz`. Con un DEM,
anche il raster descritto sotto. Un nuovo allineamento, ottimizzazione o georeferenziazione la cancella
(`chunk.forget_precision()`). Trincea sintetica: errori di quota dei punti di legame / sigma stimata
= 1,07 (RMS), camere 0,5-0,7.

**`engine.precision.dem_precision(...)`** interpola linearmente (il più vicino all'esterno), nel CRS
del DEM, al massimo 1024 celle per lato (come le mappe di precisione di James et al., 2017, dalla
covarianza).

**quote**: la precisione viene scritta in `rilievo_originale` ("Fotogrammetria (±1,2 mm)",
PyArchInit non ha una colonna apposita).

## Regione di ricostruzione

| Chiamata | Descrizione |
| --- | --- |
| `chunk.region = engine.region.Region(center, size, rotation)` | una scatola orientata in coordinate mondo (`chunk.meta["region"]`) che delimita ogni prodotto |
| `chunk.region = None` | nessuna regione |
| `chunk.reset_region(source="tie_points", margin=0.05, angle_deg=0)` | attorno al 98 % centrale dei punti di legame (o della nuvola densa), girata attorno alla verticale |
| `engine.region.region_from_box(chunk, (xmin, ymin, xmax, ymax), zmin=None, zmax=None)` | sopra un rettangolo in pianta, le quote dai punti al suo interno |
| `Region.contains(world)` | quali punti mondo stanno dentro la scatola |
| `Region.corners()` | gli 8 spigoli della scatola |
| `Region.footprint()` | l'estensione della scatola in pianta |
| `engine.region.inside_internal(chunk, pts)` | quali punti del sistema interno stanno dentro la regione |
| `engine.region.depth_mask(...)` | i pixel di una mappa di profondità la cui superficie sta dentro la regione |
| `engine.region.crop_mesh(...)` | una mesh senza le facce con il centro fuori dalla regione |

**`chunk.region = engine.region.Region(...)`**: le mappe di profondità (CPU e, dopo, quelle di COLMAP)
non hanno profondità fuori di essa, la nuvola densa e la mesh vengono tagliate su di essa (una
superficie Poisson non si gonfia più oltre l'oggetto), così DEM e ortofoto coprono solo quella.

## Modifica del DEM

| Chiamata | Descrizione |
| --- | --- |
| `chunk.edit_dem(polygon, method="plane", value=None, breaklines=None, keep_version=True, version_name=None)` | nuove quote per le celle dentro `polygon` (CRS del chunk) da quelle in un anello di tre celle intorno; restituisce `cells`, `dz_mean`, `dz_max`, `version` |
| `engine.dem_edit.fill_area(...)` | lo stesso come funzione del modulo |
| `engine.dem_edit.new_heights(known, target, method, value=None)` | la sola interpolazione |

**`chunk.edit_dem(...)`**: `plane` (il piano che meglio si adatta), `linear` (triangolazione
dell'anello, IDW fuori di essa), `idw`, `constant` (`value`, predefinito la mediana dell'anello);
`breaklines` (linee (N, 2) o (N, 3); senza z, il DEM ai loro vertici) conservano le loro quote. Il
DEM com'era viene prima conservato come versione; un DEM di un altro programma viene modificato come
copia nella cartella del chunk; overview aggiornate nella finestra; `chunk.meta["dem_edits"]` elenca
le modifiche.

## Prospetti: ortofoto e rilievo su un piano o un cilindro

Un sistema di riferimento porta una cella raster `(u, v)` (u lungo la superficie, da sinistra a destra
come la vede l'osservatore, v verso l'alto, metri) e una profondità `d` (verso l'osservatore) nel
mondo: `engine.elevation.Plane(origin, u_axis, v_axis)` (`normal = u x v`) o `Cylinder(centre, axis,
radius, inside, start)` (inside: visto dall'asse, un pozzo o un'abside; outside: una torre o una
colonna). La superficie di una cella è il primo punto in cui un raggio dal lato dell'osservatore
colpisce il modello, tenuto entro `depth=(dmin, dmax)` (ciò che sta davanti a `dmax` - un ponteggio,
una sezione lasciata davanti al muro - viene attraversato).

| Chiamata | Descrizione |
| --- | --- |
| `engine.elevation.plane_from_line(chunk, p1, p2, side=None)` | il piano verticale per la traccia di un muro (punti in pianta), visto da `side` (predefinito: da dove stanno le foto) |
| `plane_from_points(points, toward=None)` | il piano di tre o più punti mondo (marker su un muro) |
| `cylinder_from_circle(chunk, points, inside=True)` | il cilindro verticale per un cerchio adattato a punti in pianta |
| `build_elevation(chunk, frame, resolution=None, name=None, depth=None, blending="mosaic", colour_correction=None, ghosting=False, fill_holes=True, max_angle=80)` | ortofoto e rilievo (`d`, float32) del sistema come GeoTIFF in metri del sistema, elencati in `chunk.meta["elevations"]` |
| `elevations(chunk)` | i prospetti di un chunk |
| `elevation_paths(chunk, entry)` | i file dell'ortofoto e del rilievo di uno |
| `remove_elevation(chunk, id)` | ne toglie uno |

**`build_elevation(...)`** fa girare il motore dell'ortofoto sul sistema del prospetto (stessa scelta
delle foto, fusione, correzione del colore) e scrive i file in `<dati chunk>/elevations/<id>/`
(sistema nei tag e in `frame.json`).

## Maschere delle foto

Una maschera è un PNG a un canale grande quanto la sua foto, bianco dove la foto conta e nero dove è
mascherata (la convenzione di Metashape e di COLMAP), conservato in `<dati chunk>/masks/<chiave
camera>.png`. Le aree mascherate restano fuori dall'abbinamento (COLMAP ignora i loro punti chiave;
le foto senza maschera ne ricevono una bianca, perché COLMAP salta una foto la cui maschera manca),
dalle mappe di profondità (nessuna profondità lì, anche dopo il PatchMatch di COLMAP:
`engine.dense.mask_depth_maps`), dall'ortofoto e dalla texture (una foto non vede una cella o una
faccia mascherata).

| Chiamata | Descrizione |
| --- | --- |
| `engine.masks.import_masks(chunk, folder, template=None, invert=False)` | da file immagine con il nome delle foto (`{stem}_mask.png`, `{stem}.png`, `{name}.png`... o `template` con `{name}`, `{stem}`, `{label}`); `folder=None`: il canale alfa delle foto |
| `export_masks(chunk, folder, template="{stem}_mask.png")` | le maschere scritte come file con il nome delle foto |
| `read_mask(chunk, cam)` | la maschera di una foto come array (`keep`: True dove la foto conta) |
| `write_mask(chunk, cam, keep)` | conserva un array come maschera di una foto |
| `remove_masks(chunk, cams=None)` | toglie le maschere (tutte per impostazione predefinita) |
| `masked_cameras(chunk)` | le foto che hanno una maschera |
| `masks_from_background(chunk, folder, tolerance=12)` | l'oggetto è ciò che differisce da foto dello sfondo scattate dalle stesse posizioni |
| `masks_from_colour(chunk, colour, tolerance=40)` | un fondale di un solo colore (RGB) mascherato |
| `masks_from_model(chunk, cams=None)` | si tiene solo ciò che il modello copre (dopo una prima ricostruzione) |
| `masks_with_sam(chunk, segmenter, cams=None)` | SAM 2.1: l'oggetto al centro di ogni foto |
| `paint_polygon(chunk, cam, polygon, masked=True)` | maschera (o restituisce) un poligono di pixel a mano |
| `chunk.match_photos(..., exclude_stationary=True)` | scartate le corrispondenze verificate che non si muovono tra due foto: lo sfondo di un turntable fotografato da una camera fissa |
| `engine.sfm.remove_stationary_matches(db)` | lo stesso su un database COLMAP |

**`masks_from_background(...)`** prende una foto dello sfondo per foto (stessi nomi), oppure una
sola per una camera fissa.

**`chunk.match_photos(..., exclude_stationary=True)`** scarta le corrispondenze che si muovono meno
di 2 px o dello 0,15 % della foto.

## Patch dell'ortofoto e della texture

Una patch prende i colori di un'area dell'ortofoto da foto scelte a mano: un dict in
`chunk.meta["orthomosaic"]["patches"]`, `{"id", "polygon" [[x, y], ...] (CRS del chunk), "photos"
[etichette], "mode" "assign"|"exclude", "surface" "dem"|"ground"}` (una patch successiva copre una
precedente).

| Chiamata | Descrizione |
| --- | --- |
| `chunk.add_ortho_patch(polygon, photos, mode="assign")` | una nuova patch |
| `engine.patches.add_patch(chunk, polygon, photos, mode, surface="dem")` | lo stesso; `surface="ground"` proietta l'area sul terreno circostante invece che sul DEM |
| `chunk.update_orthomosaic(progress=None, skip_unreadable=False)` | ridisegna solo le celle delle patch aggiunte, cambiate o tolte da quando il file è stato scritto, poi le overview di quelle finestre |
| `chunk.ortho_patches` | le patch del chunk |
| `chunk.remove_ortho_patch(id)` | ne toglie una |
| `engine.patches.clear_patches(chunk)` | le toglie tutte |
| `engine.patches.changed_patches(chunk)` | le patch aggiunte, cambiate o tolte da quando il file dell'ortofoto è stato scritto |
| `engine.patches.photos_for_area(chunk, polygon, limit=12)` | le foto che inquadrano l'area, le migliori per prime: `label`, `coverage`, `visible`, `angle`, `gsd`, `score`, `used` |
| `engine.patches.previews(chunk, polygon, photos, max_side=320)` | `({(label, surface): RGBA}, now, bounds)`: l'area da ogni foto su ogni superficie, il resto com'è |
| `chunk.patch_texture(photos, polygon=None, faces=None, mode="assign")` | colora di nuovo le facce del modello con texture dentro un poligono (visto dall'alto) o scelte per indice, dalle foto scelte |
| `engine.patches.photos_for_faces(chunk, faces)` | le foto che vedono quelle facce, le migliori per prime |
| `engine.patches.preview_faces(chunk, faces, photo)` | la parte di una foto in cui si vedono le facce |

**`engine.patches.add_patch(..., surface="ground")`** prende le quote lungo il poligono, pesate con
l'inverso della distanza all'interno: alberi e veicoli come li mostra la foto, buchi del DEM
riempiti.

**`chunk.update_orthomosaic(...)`** aggiorna le overview con `engine.overviews.refresh` (attraverso
il GDAL di rasterio; ogni overview rifatta quando non ci riesce); un'ortofoto di un altro programma
viene prima copiata nel chunk e i suoi pixel tornano dove si toglie una patch.

**`engine.patches.photos_for_area(...)`**: `visible` è la frazione non nascosta dal modello, `used`
la frazione che l'ortofoto prende adesso da quella foto.

**`chunk.patch_texture(...)`**: i texel che nessuna foto scelta vede tengono il loro colore.

`chunk.build_orthomosaic(..., patches=None)` applica le patch del chunk (passa `[]` per lasciarle
fuori). Le foto di ogni tassello sono ordinate su tutte le sue celle, così un aggiornamento dà gli
stessi pixel di una costruzione.

## Barre di scala

Una barra di scala è la distanza nota tra due marker (`core.marker.ScaleBar`: `label`, `marker_a`,
`marker_b` (chiavi dei marker), `distance` in metri, `accuracy`, `enabled`, `checkpoint`).

| Chiamata | Descrizione |
| --- | --- |
| `chunk.add_scalebar(marker_a, marker_b, distance, accuracy=0.001, checkpoint=False, label=None)` | marker come oggetti, etichette, chiavi o codici dei target; la stessa coppia di nuovo modifica la barra |
| `chunk.scalebar(label_or_key)` | una barra |
| `chunk.remove_scalebar(label_or_key)` | ne toglie una |
| `chunk.scalebars` | tutte le barre |
| `chunk.import_scalebars(path, accuracy=0.001, delimiter=None)` | `marker_a, marker_b, distance[, accuracy[, check]]` per riga; restituisce `{"bars": [etichette], "missing": ["A-B"]}` |
| `markers.gcp.scalebar_errors(chunk)` | ogni barra: `label`, `marker_a`, `marker_b`, `distance`, `accuracy`, `type` (control/check), `enabled`, `estimated`, `error` (metri) |
| `markers.targets.generate_scale_bar(path, target_type="aruco_4x4_50", codes=None, distance_mm=200, marker_size_mm=40, page="A4", count=1)` | barre stampabili e il loro CSV, `(pages, csv)` |

`chunk.update_transform()` usa le barre di controllo (abilitate, non di verifica) così:

* nessun punto di controllo e nessun riferimento delle camere (o riferimenti delle camere che
  coprono meno di dieci volte la loro accuratezza, `cameras_span_their_accuracy`): la scala è la
  soluzione ai minimi quadrati di `scale * |a - b| = distance` pesata con `1 / accuracy²` (di due
  barre misurate ugualmente bene conta di più la più lunga); l'orientamento resta, l'origine è
  quella interna e il CRS diventa `LOCAL` (`method == "scalebars"`);
* lo stesso su un chunk georeferenziato altrove (un progetto importato, CRS non `LOCAL`) senza
  riferimenti abilitati: le barre lo scalano attorno al loro centro, conservando posizione,
  orientamento e CRS;
* tre o più punti di controllo, o riferimenti delle camere: la similarità è l'adattamento congiunto
  pesato ai minimi quadrati dei punti (`1 / accuracy²` sui loro residui 3D) e delle barre
  (`1 / accuracy²` su `scale * length - distance`), `markers.gcp.similarity_with_bars`: rotazione e
  posizione vengono dai punti, la scala da punti e barre insieme (`"markers+scalebars"`,
  `"cameras+scalebars"`, `"markers+cameras+scalebars"`); `use_cameras=True` o `False` impone l'uso
  dei riferimenti delle camere.

Il rapporto aggiunge `scale` (unità mondo per unità interna), `scalebars` (come `scalebar_errors`),
`scalebar_control_rmse` e `scalebar_check_rmse` (metri, None senza barre di quel tipo).

I marker vengono collocati da `chunk.triangulate_markers()` (`markers.gcp.triangulate_markers`): da
due foto o più vengono triangolati; misurati in una sola foto stanno dove il suo raggio incontra la
superficie, `gcp.surface_point(chunk, camera, x, y)` (la nuvola densa, altrimenti i punti di legame,
attraverso `core.geometry.point_on_ray`: un piano adattato ai punti attorno al raggio, prima in un
cilindro largo, poi in uno di circa 20 pixel alla profondità trovata; conservato per misura); un
marker senza proiezioni tiene la sua posizione. `gcp.views_of(chunk, marker)` conta le foto allineate
che lo misurano, `gcp.camera_ray(chunk, camera, x, y)` è il raggio di un pixel. Un marker collocato
in 3D (sistema interno) viene proiettato sulle foto che lo vedono con `gcp.project_marker(chunk,
marker, point, max_photos=60)`: dentro il fotogramma, davanti alla camera, non nascosto dalla
superficie vicino al marker, come proiezioni stimate.

Una proiezione (`core.marker.Projection`) ha `pinned` True quando è messa o confermata a mano, False
quando è rilevata (target codificati), None quando è solo stimata dal modello (le bandierine blu di
Metashape; le versioni precedenti leggono None come un rilevamento). `Projection.measured` è True per
le prime due: un marker con proiezioni misurate viene collocato solo da quelle, le stime servono solo
per un marker collocato in 3D e non entrano mai nell'ottimizzazione delle camere.
`gcp.photos_seeing(chunk, point, occlusion=True)` elenca `(camera, x, y, depth)` delle foto che
vedono un punto, prima le più vicine al centro (*Filtra le foto che vedono questo punto*);
`gcp.project_marker(chunk, marker, point=None, max_photos=60)` sostituisce le proiezioni stimate di
un marker dalla sua posizione; `gcp.views_of(chunk, marker, estimated=False)` conta le foto che lo
misurano (o, con `estimated`, che lo stimano soltanto).

`markers.refine.refine_markers(chunk, markers=None, min_score=0.8)` cerca ogni bandierina blu nella
sua foto per correlazione d'immagine: un ritaglio della foto in cui il marker è misurato (messo a mano
per primo, altrimenti la vista più nitida) e la foto della bandierina vengono campionati entrambi
sulla stessa griglia di punti sulla superficie al marker (il piano della nuvola densa, altrimenti dei
punti di legame, intorno a lui; un campione per pixel della foto più grossolana, levigato contro
l'aliasing), così prospettiva, rotazione, scala e distorsione dell'obiettivo non contano; un picco
netto della correlazione normalizzata (almeno `min_score`, 0,1 sopra ogni altro picco, entro +-10
campioni) sposta lì la bandierina a una frazione di pixel e ne fa una misura automatica (`pinned`
False); il marker viene triangolato di nuovo e le bandierine rimaste vengono stimate dalla sua nuova
posizione. Restituisce `{chiave marker: {"refined": {chiave camera: (dx, dy, score)}, "failed":
[chiavi camera], "reference": chiave}}`; `refine_marker(chunk, marker, ...)` fa un solo marker. Sulla
trincea sintetica, bandierine a 2-6 px dal target tornano entro 0,3 px dal centro rilevato.

Nell'ottimizzazione delle camere (`chunk.optimize_cameras`) le barre di controllo sono punti fissi
come i punti di controllo: `engine.sfm.bar_anchors(chunk, bars, fixed=None)` sposta i marker alle
loro estremità in modo che le barre abbiano la loro lunghezza, minimi quadrati di
`scale * length - distance` (pesati con `1 / accuracy²`) e degli spostamenti, pesati con
`engine.sfm.marker_precision(chunk, marker)` (l'accuratezza in pixel delle sue misure, 0,5 px,
portata alla sua distanza da ogni camera e combinata sulle foto); la compensazione poi tiene quelle
posizioni, osservate nelle foto. Servono tre punti fissi o più, non allineati: due barre, o barre e
punti di controllo. Sulla trincea sintetica, con marker misurati a 0,5 mm, una barra 6 mm troppo
lunga con un'accuratezza di 0,1 mm piega il blocco di 0,2 mm: le foto lo tengono, come in una
compensazione congiunta.

Importazioni: `integrations.metashape.import_psx` porta le barre di scala del progetto
(`MsScaleBar`: due `endpoint marker_id`, `reference d`, accuratezza `sd` o l'`accuracy_scalebars`
del chunk, senza spunta = barra di verifica); `integrations.webodm.import_task` porta le pose delle
camere (`read_cameras`: `cameras.json`, `odm_report/shots.geojson`) e i GCP come marker
(`read_gcps`), triangolati subito.

## Moduli

| Modulo | Funzioni principali |
| --- | --- |
| `markers.targets` | `generate_sheet`, `generate_turntable_mat`, `turntable_layout`, `generate_scale_bar`, `scale_bar_codes`, `detect` |
| `markers.gcp` | `import_reference`, `export_reference`, `triangulate_markers`, `update_transform`, `similarity_with_bars`, `marker_errors`, `scalebar_errors`, `import_scalebars`, `resolve_marker`, `cameras_span_their_accuracy` |
| `engine.chunks` | `align_chunks`, `merge_chunks`, `common_markers`, `common_cameras`, `pose_similarity`, `icp`, `global_guesses`, `ransac_rigid`, `registration_quality` |
| `archaeo.sections` | `section`, `sections_along`, `plan_slice`, `Section.to_dxf/to_svg`, `drawing_scale`, `grid_step` |
| `archaeo.pottery` | `estimate_axis`, `profile_at`, `best_profile`, `Profile.measurements`, `rim` (cerchio dell'orlo: diametro, arco conservato, affidabilità), `measure` (profilo e misure di un vaso o di un frammento, asse spostato al centro dell'orlo), `percent_class`, `signature`, `signature_distance`, `draw` (SVG, DXF, PNG) |
| `archaeo.terrain` | `contours` (semplificate, levigatura facoltativa), `suggest_interval`, `dem_range`, `export_contours`, `volume`, `hillshade`, `slope_degrees` |
| `archaeo.stratigraphy` | `UnitPolygon`, `mask_to_polygons`, `export_units` |
| `bim.ifc` | `export_ifc`, `export_chunk_ifc`, `export_proxies_ifc`, `split_mesh_by_units`, `global_id` (GlobalId IFC dal `node_uuid` di una scheda US), `read_ifc_elements` |
| `archaeo.stratigraphy` (rapporti) | `RELATION_EDGES` (i rapporti delle schede di PyArchInit in italiano, inglese e tedesco come archi dell'Extended Matrix), `stratigraphic_edges` (ogni rapporto dichiarato una volta sola, dall'inizio del suo arco) |
| `ai.classify` | `classify_chunk`, `classify_by_units`, `unit_class_name`, `units_table_csv`, `assign_class_by_polygons`, `class_statistics`, `set_class`, `clear_manual`, `class_names` |
| `ai.learn` | `point_features`, `train`, `predict`, `ClassifierModel`, `train_on_chunk`, `classify_chunk_with_model` (vedi sotto) |
| `ai.sam` | `Sam2Segmenter`, `segment_orthomosaic`, `auto_units`, `classify_from_masks`, `segment_photo_and_classify` |
| `pyarchinit` | `PyArchInit`, `register_media`, `connect` |

## Convenzioni delle coordinate

* Sistema interno = sistema della soluzione SfM; `chunk.transform` (similarità) lo porta nel CRS
  del chunk. Nuvola densa e mesh sono conservate nel sistema interno; raster ed esportazioni sono
  nel CRS.
* Le coordinate immagine seguono COLMAP: il centro del primo pixel è (0.5, 0.5).
* Camere: assi OpenCV (+X a destra, +Y in basso, +Z in avanti); `camera.transform` è
  `world_from_cam` nel sistema interno.

## Dati demo e interfaccia

| Chiamata | Descrizione |
| --- | --- |
| `ppg.demo.create_demo(folder, process=False)` | foto di una trincea sintetica + GCP, progetto, mesh di ceramica, database PyArchInit (`DemoData`) |
| `pyarchinit_photogrammetry.gui.main(argv)` | avvia l'interfaccia desktop (`ppg-gui`) |
| `ppg.core.progress.Progress` | callback di avanzamento con intervalli `sub()`; `cancel()` ferma ogni sotto-passo |


## Aggiunte nella 0.3

* `chunk.crop_point_cloud(zmin=, zmax=, radius=, center=(x, y), bounds=(xmin, ymin, xmax, ymax))`
  tiene i punti densi dentro i limiti (coordinate mondo) e scarta i prodotti del modello.
* `PyArchInit.connect(url=None, config_path=None, media_root=None)`, `pa.ensure_site(sito, ...)`,
  `pa.site_exists(sito)`, `pa.label`.
* `pyarchinit.demo_db.create_demo_database(home, size="small"|"large", overwrite=False, demo_content=True)`
  con lo schema di PyArchInit 5 (`pyarchinit5_schema.sql`).
* `demo.create_demo(..., with_pottery=True)` e `demo.process_pottery_demo(doc, profile_svg)`;
  `testing.synthetic.make_turntable_scene()`, `render_mesh()`, `write_pottery_dataset()`.
* `engine.isolate.run_isolated(fn, *args, timeout=, cancel_check=)` esegue una funzione a livello
  di modulo in un interprete nuovo (usata dalla ricostruzione Poisson). Con `progress=callback`
  inoltra ciò che `fn` passa a `engine.isolate.report(fraction, message)` (usata dalla fusione delle
  mappe di profondità).


## Classificazione addestrabile dei punti

Etichetta a mano alcuni punti, addestra su di loro un classificatore, classifica il resto della nuvola
e, con il modello salvato, le nuvole di altri progetti (come 3DMASC/CANUPO di CloudCompare).
Serve scikit-learn (`pip install "pyarchinit-photogrammetry[learn]"`, compreso in `[gui]` e `[all]`).

```python
from pyarchinit_photogrammetry.ai import classify, learn

classify.set_class(chunk, lasso_mask, 64, label="Muro")   # classe + contrassegno "etichettato a mano"
model = learn.train_on_chunk(chunk)                        # impara dai punti etichettati a mano
print(model.summary())                                     # accuratezza out-of-bag, precision/recall per classe
pc, confidence = learn.classify_chunk_with_model(chunk, model, min_confidence=0.6)
model.save("scavo.ppgclf")
other = learn.ClassifierModel.load("scavo.ppgclf")          # applicalo a un altro progetto
```

| Chiamata | Descrizione |
| --- | --- |
| `classify.set_class(chunk, mask, code, manual=True, label=None)` | dà ai punti densi selezionati (maschera booleana o indici) una classe e li contrassegna come etichettati a mano; `manual=False` toglie il contrassegno; restituisce il conteggio |
| `classify.label_points(pc, mask, code, manual=True)` | lo stesso su un `PointCloud` in memoria: etichetta più selezioni, poi conserva la nuvola una volta sola (`chunk.point_cloud = pc`) |
| `classify.clear_manual(chunk, mask=None)` | dimentica il contrassegno "etichettato a mano" (classi invariate) |
| `learn.point_features(xyz, rgb=None, normals=None, scales=None, ground=None, progress=None)` | matrice delle feature `(N, F)` float32 e nomi delle feature; coordinate mondo, z verso l'alto |
| `learn.suggest_scales(xyz)` | voxel del nucleo e raggi predefiniti: 5, 14 e 40 volte il passo dei punti |
| `learn.estimate_spacing(xyz)` | il passo dei punti di una nuvola |
| `learn.train(xyz, codes, rgb=None, ..., method="random_forest", n_estimators=None, max_per_class=20000, random_state=0)` | `ClassifierModel`; `codes` 1-255, 0 o negativo = non etichettato; `method="gradient_boosting"` per file più piccoli |
| `learn.predict(model, xyz, rgb=None, ..., min_confidence=0.0)` | `(codes uint8, confidence float32)`; i punti sotto `min_confidence` diventano 1 (non classificato) |
| `learn.train_on_chunk(chunk, labels="manual")` | addestra sui punti etichettati a mano (`labels="all"`: ogni punto con una classe sopra 1) |
| `learn.classify_chunk_with_model(chunk, model, overwrite_manual=False, min_confidence=0.0)` | classifica e conserva la nuvola densa; restituisce `(point_cloud, confidence)` |
| `model.save(path)` | scrive il file del modello |
| `ClassifierModel.load(path)` | legge un file di modello |
| `learn.read_model_info(path)` | legge solo i metadati |
| `model.summary()` | una descrizione leggibile: accuratezza out-of-bag, precision/recall per classe |
| `model.classes` | i codici delle classi del modello |
| `model.class_names` | i loro nomi |
| `model.feature_names` | i nomi delle feature |
| `model.scales` | i raggi dei vicinati (metri) |
| `model.oob_score` | l'accuratezza out-of-bag (random forest) |
| `model.training_accuracy` | l'accuratezza sui campioni di addestramento |
| `model.confusion_matrix` | la matrice di confusione (righe classe vera, colonne classe predetta) |
| `model.stats` | le statistiche dell'addestramento |

* **Feature** (per raggio): linearità, planarità, sfericità, omnivarianza, entropia degli
  autovalori, variazione di curvatura, verticalità della normale e dell'asse principale, densità,
  altezza sopra il vicino più basso e sotto il più alto, dispersione verticale, rugosità; poi
  altezza dal suolo (filtro morfologico progressivo, oppure `ground=` un GeoTIFF DTM / quote del
  terreno), colore (RGB normalizzato, HSV, excess green) e contrasto di colore con i vicini.
  Si calcolano su una nuvola *nucleo* (un punto per voxel; voxel = passo dei punti, più grossolano
  sopra `max_core_points`, predefinito 1 M) e ogni punto prende i valori del suo voxel; ogni raggio
  usa il proprio livello di voxel e al massimo 64 vicini. Circa 15 s per milione di punti del
  nucleo su 4 thread (`n_jobs`).
* **Etichette a mano**: `PointCloud.manual` (booleano per punto, `None` quando non è stato
  etichettato nulla) è conservato come proprietà PLY `manual`; i file che non ce l'hanno si
  caricano come prima. Il `classify_chunk` a regole e `classify_chunk_with_model` conservano la
  classe di questi punti a meno di `overwrite_manual=True` (allora il contrassegno resta dove la
  nuova classe concorda).
* **Addestramento**: un campione per voxel del nucleo (la classe della maggior parte dei suoi
  punti etichettati), al massimo `max_per_class` per classe, classi pesate in modo inverso alla loro
  dimensione; la random forest (100 alberi, ciascuno su al massimo 10.000 campioni) è validata
  out of bag, il gradient boosting sul 20 % tenuto da parte. Deterministico per un dato
  `random_state`.
* **File del modello** (`.ppgclf`): uno ZIP con `model.json` (versione del formato, classi e nomi,
  nomi delle feature, raggi e impostazioni, voxel del nucleo, statistiche, versioni del pacchetto,
  di scikit-learn e di NumPy, chunk di addestramento e CRS) ed `estimator.pkl` (lo stimatore di
  scikit-learn). Lo stimatore viene deserializzato con una lista di tipi ammessi di scikit-learn e
  NumPy, così un file che prova a eseguire altro codice viene rifiutato. I raggi sono in metri:
  applica un modello a nuvole con un passo dei punti simile e un CRS metrico (un chunk locale senza
  scala ha unità arbitrarie).
* **CLI**: `ppg classify-train scavo.ppg modello.ppgclf [--labels manual|all] [--method
  random_forest|gradient_boosting] [--scales 0.05,0.15,0.4] [--trees 100]` e
  `ppg classify-apply scavo.ppg modello.ppgclf [--min-confidence 0.6] [--overwrite-manual]`.
