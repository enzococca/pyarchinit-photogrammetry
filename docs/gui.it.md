# Interfaccia desktop

```bash
pip install "pyarchinit-photogrammetry[gui]"   # compreso in [all]
ppg-gui [progetto.ppg]                         # oppure: ppg gui, python -m pyarchinit_photogrammetry.gui
ppg-gui --doctor                               # diagnostica dell'ambiente (come ppg doctor)
ppg-gui --check                                # apre, disegna ed esce (smoke test)
ppg-gui --run script.py [argomenti]            # uno script Python con i moduli del programma, senza finestra
```

Oppure l'applicazione impacchettata per macOS e Windows: [packaging.md](packaging.md).

L'interfaccia parla italiano, come PyArchInit, o inglese: si sceglie da *Vista > Lingua / Language*
(la scelta vale dal prossimo avvio, *Riavvia ora* riavvia subito con il progetto aperto); senza una
scelta segue la lingua del sistema, e `PPG_LANG=en` o `it` ne impone una per una sessione. In inglese
sono tradotti menu, finestre, pannelli, messaggi, log e avanzamento del motore, e i numeri sono scritti
all'inglese; ciò che è dato resta in italiano: i valori che PyArchInit rilegge (tipi di US, rapporti),
i nomi dei layer e delle versioni dei prodotti, i disegni e i loro file. Ogni azione chiama la stessa
API Python usata dagli script, così tutto ciò che si fa con il mouse si può ripetere nella console o
in uno script.

## Avvio e apertura di un progetto

`ppg-gui` (e l'applicazione impacchettata) mostra subito uno splash nei colori del logo con una barra
di avanzamento al centro: la percentuale e ciò che si sta caricando (NumPy e Open3D, PROJ e GDAL,
COLMAP, la vista 3D VTK, l'interfaccia). La finestra compare in circa un secondo (l'applicazione
impacchettata ce ne metteva undici); il log registra quanto è durato l'avvio. L'apertura di un
progetto mostra la stessa scheda sopra la finestra (progetto, area di lavoro, vista 3D, ortofoto e
DEM); le miniature delle foto arrivano dopo, in background, dalle piccole anteprime che le fotocamere
salvano nell'EXIF (pochi millisecondi per foto; la barra di stato le conta), così centinaia di foto
di drone non bloccano più la finestra. Le foto che non si possono leggere (file vuoti tenuti solo
online da Dropbox o iCloud) hanno una miniatura scura e un tooltip.

## Pagina iniziale

Senza un progetto l'area centrale mostra la pagina iniziale: *Nuovo progetto*, *Apri
progetto...*, *Progetto demo*, i progetti recenti e i collegamenti alla guida, alla diagnostica
dell'ambiente e alla cartella dei log. *File > Pagina iniziale* la fa tornare.

## Il pannello Multispettrale

La scheda *Multispettrale* del dock del progetto resta aperta ed elenca i sette passi in ordine,
ognuno con il suo stato e il suo pulsante: le riprese, l'allineamento, la superficie (mappe di
profondità, nuvola densa e DEM in un passo solo con le scelte predefinite), il pannello di
calibrazione, l'ortofoto di riflettanza (*Mostra* in falso colore NIR-Red-Green, in colori veri, o
una banda da sola in scala di grigi: NIR, red edge...), l'indice (NDVI, NDRE, GNDVI, SAVI, NDWI,
OSAVI, CIRE, Thermal o una formula propria sui nomi delle bande, calcolato e mostrato sulla scala di
colori scelta lì accanto - rosso-giallo-verde, scala di grigi, viridis o turbo; la scelta ricolora
l'indice mostrato e viene ricordata) e la zonizzazione (classi e metodo, i poligoni in un
GeoPackage, mostrati per classe con i poligoni come layer).

I file di una camera multispettrale vengono riconosciuti quando aggiungi foto (*Foto* o *Aggiungi
cartella*): la finestra propone di aggiungerli come riprese e apre questo pannello, così le bande
non vengono allineate come se fossero viste diverse. Un volo diviso in più cartelle (i rulli di una
camera, 100MEDIA e 101MEDIA, la cui numerazione riparte) si aggiunge una cartella dopo l'altra, o
come cartella madre in un colpo solo: i nomi che si ripetono prendono la cartella come prefisso, i
file già nel chunk restano fuori. L'ortofoto di riflettanza pareggia le foto per impostazione
(*Pareggia la luminosità tra le foto*): un guadagno per foto e banda, trovato dove le foto si
sovrappongono, toglie le strisce tra le linee di volo percorse in direzioni opposte (geometria di
sole e vista) conservando il livello medio; togli la spunta per la riflettanza grezza. I prodotti
compaiono nell'albero del progetto sotto *Multispettrale* (l'ortofoto di riflettanza, ogni indice,
ogni zonizzazione) e si aprono da lì; *Apri in QGIS* crea il progetto QGIS con la riflettanza in
falso colore, gli indici con la loro scala, le zone come raster e come poligoni, oltre a ortofoto,
DEM e disegni.

La scheda **Termico** accanto guida una **camera termica** (e *Strumenti > Termico* ha gli stessi
passi): dopo le riprese, l'allineamento e la superficie vengono i passi delle temperature - i
parametri della misura
(*Parametri...*: emissività, temperatura riflessa e ambiente, umidità, distanza; per impostazione i
valori di fabbrica delle camere DJI), l'ortofoto termica in gradi Celsius (*Costruisci...*, poi
mostrata su una scala - ferro, inferno, arcobaleno, grigi - tra due temperature che scegli tu, come il
raster calculator di Metashape restringe la scala termica; *Auto* torna al 2°-98° percentile), le
anomalie termiche (la temperatura meno quella dei dintorni entro un raggio) e l'esportazione che
conserva i gradi (GeoTIFF float32 in gradi Celsius, GeoTIFF a 16 bit in centikelvin, CSV, oppure JPEG
radiometrici FLIR a tasselli con i world file, che i programmi per le termocamere FLIR aprono come
termogrammi). Gli R-JPEG
DJI si decodificano con il Thermal SDK gratuito di DJI, che va nella cartella `dji_tsdk` dei dati del
programma; sul Mac gira in Docker, quindi Docker Desktop deve essere avviato.

Sotto il mouse la barra di stato legge i prodotti a piena risoluzione: sull'ortofoto termica la quota,
la temperatura e la sua anomalia (`Z 158.231  T 18.34 °C  ΔT +0.42 °C`), sulla riflettanza ogni banda,
su un indice il suo valore, sull'ortofoto e sul DEM anche la temperatura quando c'è. Lo strumento
**Punto di misura** (nella barra *Disegno*, nelle due schede e nei menu) mette un punto dove fai clic
con le coordinate, la quota del DEM, la temperatura e la sua anomalia, la riflettanza di ogni banda e
gli indici: i punti vanno nel layer *Punti di misura*, si salvano con il progetto, arrivano in QGIS con
gli altri layer e si esportano in GeoPackage, CSV o GeoJSON (*Esporta i punti di misura...*).

## Disposizione

| Area | Contenuto |
| --- | --- |
| Barra degli strumenti | il flusso di lavoro con icone colorate: apri/salva, foto, marker, allineamento, riferimenti, nuvola densa, modello, texture, DEM, ortofoto, *Elabora tutto*, pubblicazione in PyArchInit. Le azioni di elaborazione sono disattivate mentre gira un'attività |
| Barra di disegno (*Disegno e layer*) | sempre visibile sotto il flusso di lavoro: le modalità di disegno (*Sposta / interroga*, *Disegna US*, *Traccia sezione*, *Misura*, *Quota*, *SAM* con il suo menu), *Cancella disegni*, curve di livello, volumi, piante a quota e le scorciatoie ai pannelli *Layer* e *Sezioni* e a *Pubblica* (unità e quote) |
| Sinistra: **Area di lavoro** | progetto, chunk (clic per renderne uno attivo), CRS (doppio clic per cambiarlo), camere (camera verde = allineata, rossa = non allineata), marker (GCP, punti di verifica), prodotti; un doppio clic apre una foto o un prodotto |
| Sinistra: **Riferimenti** | marker con le spunte usa/check, coordinate e accuratezza modificabili, errori (m e px), posizioni delle camere; *Importa CSV*, *Aggiorna* (georeferenziazione), *Ottimizza camere* (con 3 o più punti di controllo entrano nella compensazione e piegano il blocco; i punti di verifica restano fuori; errori di controllo e di verifica prima e dopo nel log); *Barra di scala...* e *Importa barre...*; *Coordinate stimate* (come *View Estimated* di Metashape: le colonne X, Y, Z mostrano le coordinate stimate dei marker invece di quelle di riferimento; si attiva da sola quando nessun marker ha coordinate di riferimento); le barre di scala sotto i marker (vedi *Barre di scala*); RMSE di controllo e di verifica di punti e barre. Il tooltip di un marker dice come è collocato: triangolato da N foto, sulla superficie da una foto, o aggiunto sul modello o sull'ortofoto |
| Sinistra: **Layer** (modifica) | doppio clic su un valore in *Attributi* per cambiarlo (US, area, tipo, etichetta, qualsiasi campo libero; i campi calcolati come m2 e lunghezza sono di sola lettura). Le unità caricate da PyArchInit conservano il loro `gid`: *Salva modifiche in PyArchInit* riscrive le modifiche in `pyunitastratigrafiche`; le unità disegnate e le quote si scrivono con *Pubblica*; gli elementi scritti conservano il loro `gid`, così niente viene scritto due volte |
| Sinistra: **Layer** | tutto ciò che si disegna sull'ortofoto/DEM è un layer: unità stratigrafiche, poligoni US caricati da PyArchInit, tracce di sezione, curve di livello, piante a quota, quote. Spunta per mostrare/nascondere, doppio clic per lo zoom (sul layer o sul singolo elemento), *Rimuovi*; in modalità *Sposta / interroga* un clic su un elemento ne mostra gli attributi (US, area, m2, quota di una curva...) nel pannello e nella barra di stato |
| Centro: **Modello** | punti di legame, nuvola densa, mesh ombreggiata o con texture, piramidi delle camere, marker. Trascina con il tasto sinistro per orbitare, con il destro o il centrale per spostare, rotella per lo zoom |
| Centro: **Ortofoto / DEM** | ortofoto o DEM (tinte ipsometriche e ombreggiatura) in coordinate mondo; X, Y, Z del cursore nella barra di stato |
| Centro: **Foto** | una foto con i suoi marker, come in Metashape: le bandierine verdi sono misurate (messe a mano o rilevate), le blu sono stimate dal modello e da confermare (con l'etichetta "?"), le croci rosse sono dove il rilievo mette un marker non collocato in quella foto. Trascina una bandierina sul punto (diventa verde; la bandierina si sposta quanto il mouse, da qualunque parte la si prenda), doppio clic su una bandierina blu per confermarla dov'è; tasto destro per collocare, spostare, confermare o togliere; Pagina giù/su (o `]`/`[`) aprono la foto successiva/precedente dell'elenco mostrato, centrata sul marker del filtro |
| In basso: **Foto** | miniature con il distintivo allineata/non allineata e, in fondo, i marker di ogni foto (verde: misurato, blu: da confermare; il tooltip li elenca). La barra mostra il filtro in uso - *Foto di point 1*, *Foto con marker*, le foto di un punto o di un'area - con *Mostra tutte* e *Precedente*/*Successiva*. I filtri vengono dal tasto destro sui marker in *Riferimenti* (*Filtra le foto di ...*), dal tasto destro sull'ortofoto in modalità *Sposta* (*Filtra le foto che vedono questo punto*, *Aggiungi un marker qui*), dal menu di selezione di un poligono (*Filtra le foto che inquadrano quest'area*) e da *Foto del punto* nella vista 3D; un marker nuovo filtra le foto su di sé |
| In basso: **Sezioni** | le sezioni del progetto: elenco, anteprima del disegno SVG (adattata al pannello, con linee ed etichette ingrossate perché un disegno lungo metri sulla carta resti leggibile), *Zoom sulla traccia*, *Apri cartella* (profilo DXF e DXF 3D in `sezioni/`), *Pubblica in PyArchInit* (`pyarchinit_sezioni`), *Elimina*. Le sezioni si salvano in `sezioni/sezioni.json` accanto al progetto e tornano quando lo si riapre |
| In basso: **Console Python** | `doc`, `chunk`, `app`, `ppg`, `np`, `window` |
| In basso: **Log** | ora, livello e messaggio di tutto ciò che è accaduto; filtro (tutto / avvisi ed errori / errori), ricerca, *Copia*, *Salva...*, *Cartella log*, *Pulisci*; il traceback di un errore è nel tooltip della riga e nel file salvato |
| Destra: **PyArchInit** | connessione, siti, unità stratigrafiche e documentazione |
| Barra di stato | avanzamento dell'attività con un pulsante di arresto mentre qualcosa gira; coordinate del cursore |

La finestra sta nello schermo di un portatile, fino a circa 1000 x 650 pixel: i pulsanti dei
pannelli vanno a capo su più righe quando un pannello è stretto, un pannello troppo basso per il suo
contenuto scorre, e una barra che non ci sta perde i testi (la barra del flusso di lavoro tutta
insieme, quella di disegno a partire dagli ultimi pulsanti, così gli strumenti di disegno tengono i
nomi più a lungo; i tooltip li conservano).

La vista 3D usa VTK (pyvista/pyvistaqt) quando OpenGL è disponibile, altrimenti un renderer software
(lo si impone con `PPG_RENDERER=software`, o VTK con `PPG_RENDERER=vtk`).
*Vista > Tema* passa dal tema chiaro a quello scuro e viceversa (la scelta viene ricordata).

## Errori e log

I passi lunghi girano in background con una finestra di avanzamento e *Annulla*. La finestra mostra
la barra, il messaggio corrente, il tempo trascorso e una stima del tempo restante (dal ritmo degli
ultimi minuti: un passo che rallenta la vede crescere, non scendere a zero) e, sotto *Dettagli*, una
console con ogni messaggio del passo e la sua ora - le foto di cui si estraggono i punti, i blocchi
di coppie abbinati, le foto messe nel blocco una per una con i punti che vedono, le compensazioni
dell'intero blocco, le mappe di profondità fuse - letti dal log di COLMAP mentre gira, come la console
di Metashape. Il pannello resta aperto o chiuso come lo hai lasciato. Quando un passo
fallisce, una finestra di errore mostra un riassunto di una riga, un suggerimento per i problemi noti
(SpatiaLite o PyTorch mancanti, una ricostruzione andata in crash, un'installazione Anaconda
vecchia...), il traceback sotto *Dettagli* e i pulsanti *Copia* (riassunto + traceback, pronti per
una segnalazione) e *Apri cartella log*. Lo stesso testo va nel pannello Log e nel file di log
(`ppg-gui.log`, ruotato a 2 MB) nella cartella dei log della piattaforma; i crash del codice nativo
li scrive il faulthandler di Python in `crash.log` nella stessa cartella.

*Aiuto > Diagnostica ambiente* elenca Python, ogni libreria con la sua versione, i problemi noti
(un altro `ppg-gui` prima nel `PATH`, l'ambiente base di Anaconda, Open3D 0.20.0, pyvistaqt vecchio,
SpatiaLite mancante) e cosa fare; *Copia* mette il rapporto negli appunti.

## Aggiornamenti

Qualche secondo dopo l'avvio l'applicazione chiede a GitHub l'ultima release, al massimo una volta
al giorno: una richiesta HTTPS all'API di GitHub che porta la versione nello User-Agent e nient'altro
(*Aiuto > Controlla gli aggiornamenti all'avvio* la disattiva; anche `PPG_NO_UPDATE_CHECK=1`).
Quando esiste una versione più nuova una finestra ne mostra le note e propone:

* **Scarica e installa** (applicazione macOS): il dmg viene scaricato nella cartella Download, la
  nuova applicazione viene copiata fuori da lì e, quando il programma si chiude (subito con *Riavvia
  ora*, dopo la solita richiesta di salvare il progetto), sostituisce la vecchia nella sua cartella
  e si apre. La copia vecchia resta finché la nuova non è al suo posto. Se l'utente non può scrivere
  nella cartella, l'immagine disco si apre per trascinare a mano l'app in *Applicazioni*;
* **Scarica** (applicazione Windows): lo zip va nella cartella Download, da estrarre sopra la
  cartella del programma dopo averlo chiuso;
* **Copia il comando** (installazione Python): `pip install --upgrade pyarchinit-photogrammetry`
  (o `git pull` in una copia dei sorgenti);
* *Pagina della versione*, *Salta questa versione* (non viene più proposta all'avvio, ma *Aiuto >
  Controlla aggiornamenti* la trova ancora) e *Piu' tardi*.

Dalla riga di comando `ppg update` dice se esiste una versione più nuova e `ppg update
--download` scarica l'installatore per il sistema.

## Menu

* **File**: nuovo/apri/salva, progetti recenti, esportazione (nuvola, modello, DEM, ortofoto, camere,
  riferimenti, e tutti insieme ritagliati con un riquadro o un poligono), rapporto HTML, *Crea e apri
  progetto demo*, *Pagina iniziale*.
* **Flusso di lavoro**: aggiungi foto/cartella, *Ricollega foto* (le foto con gli stessi nomi in
  un'altra cartella e nelle sue sottocartelle: una copia su disco esterno di una cartella Dropbox
  tenuta solo online, una cartella spostata), aggiungi chunk, *Allinea chunk* (porta altri chunk nel sistema di un chunk di riferimento tramite i marker con lo stesso nome, le foto in comune o le nuvole che si sovrappongono), *Unisci chunk* (un nuovo chunk con le foto, i marker, i punti di legame, le nuvole e, a scelta, le mesh dei chunk allineati), rileva i marker codificati, allinea - la finestra di
  *Allinea foto* di Metashape: precisione, preselezione generica (le coppie che si sovrappongono trovate
  prima su foto ridotte; fino a 40 foto comunque tutte le coppie), preselezione di riferimento (il GPS
  delle foto, un allineamento precedente o l'ordine di scatto), i punti (*Punti*: SIFT, oppure punti
  appresi abbinati da LightGlue - DISK o ALIKED - per le foto termiche e le superfici povere di dettagli;
  se mancano PyTorch e kornia il programma propone di installarli), azzera o tieni l'allineamento attuale (le
  foto aggiunte dopo vi entrano); sotto *Avanzate* i limiti, le maschere applicate ai punti chiave o ai
  punti di legame, i punti di legame fermi, l'abbinamento guidato, l'adattamento adattivo del modello
  della camera, il GPS delle foto che tiene il blocco mentre cresce una foto alla volta, il metodo -
  automatico: tutte le camere insieme (globale) da 40 foto, una foto alla volta sotto - e la GPU per
  l'abbinamento (la migliore insieme alla CPU, spenta, o un adattatore scelto; la scelta viene
  ricordata e vale anche per le mappe di profondità PatchMatch) - importa riferimenti, aggiorna la georeferenziazione, *Nuova barra di scala (distanza tra due marker)*, *Importa barre di scala (CSV)*, ottimizza camere, nuvola
  densa (qualità, filtro, motore), *Ritaglia nuvola densa* (intervallo di quote e/o un cilindro
  attorno all'asse del turntable: toglie il tappetino e i target prima di fare la mesh di un oggetto), modello
  (sorgente, superficie, facce), texture, DEM, ortofoto, *Elabora tutto* (Ctrl+R); *Pulizia e
  confidenza* (confidenza della nuvola densa e del modello, pulizia della nuvola densa, del modello e
  dei punti di legame, il lazo, lo strumento di ritaglio). Il progetto viene salvato dopo ogni
  passo, una volta che ha un file. I passi che leggono le foto (mappe di profondità, texture, ortofoto,
  *Elabora tutto*) elencano prima le foto che non si possono leggere (file vuoti tenuti solo online da
  Dropbox, iCloud o OneDrive) e propongono *Ricollega a un'altra cartella*, *Continua senza
  queste foto* o *Annulla*.
* **Strumenti**: fogli di target stampabili, tappetini per turntable (con il CSV delle coordinate dei
  target) e barre di scala; *Calibra i colori delle foto* (guadagno di esposizione di ogni foto e
  vignettatura di ogni obiettivo, misurati su punti visti da più foto e usati da quel momento da
  ortofoto e texture); *Maschere delle foto*: disegna una maschera sulla foto mostrata (clic sui
  vertici, doppio clic o Invio chiude, Esc annulla; *Togli la maschera* o Maiusc + doppio clic restituisce
  l'area; le parti mascherate si vedono in rosso, *Mostra le maschere* le nasconde), importale da immagini
  o dal canale alfa delle foto, creale da foto dello sfondo senza l'oggetto, da un colore di fondo,
  dal modello o con SAM 2.1 (l'oggetto al centro di ogni foto), esportale, toglile (foto selezionate
  o tutte). Le parti mascherate non si usano per allineare, nelle mappe di profondità, nell'ortofoto
  o nella texture; *Allinea foto* può anche lasciare fuori i punti che non si muovono tra le foto
  (turntable con camera fissa); disegno sull'ortofoto: unità stratigrafiche (clic sui vertici, doppio
  clic per chiudere), tracce di sezione (due clic: disegno SVG, profilo DXF e DXF 3D in
  `sezioni/`), distanza e dislivello, quote / prompt per SAM; curve di livello;
  volumi di scavo/riporto (unità disegnata, piano o secondo DEM); pianta a una quota data;
  profilo ceramico da una mesh (vaso o frammento); classificazione della nuvola di punti; Segment Anything
  2.1 (unità candidate automatiche, unità da punti cliccati, prompt sulle foto); esportazione IFC
  (un elemento per unità disegnata, attributi PyArchInit se collegato); esecuzione di uno script Python;
  *Multispettrale*: gli stessi passi del pannello *Multispettrale* del dock del progetto (vedi sotto); *Aggiungi riprese multispettrali...* (una cartella con un file per banda di ogni
  scatto: la banda di riferimento viene allineata, le altre le si sovrappongono), *Pannello di
  calibrazione...* (la ripresa del pannello, il suo riquadro e la sua riflettanza), *Ortofoto di
  riflettanza...* (una banda a virgola mobile per banda sul DEM), *Indice e zonizzazione...* (NDVI e gli
  altri indici o una formula, le zone come poligoni in un GeoPackage: vedi la pagina dell'API, *Camere
  multispettrali*);
  *Immagini satellitari*: *Ortorettifica una scena...* (una scena con i suoi RPC sul DEM del rilievo, conservata
  come versione dell'ortofoto), *DSM da una coppia stereo...* (la superficie da due scene prese da due
  direzioni, conservata come versione del DEM), *Informazioni sulla scena...* (dimensioni, bande,
  risoluzione a terra, impronta: vedi la pagina dell'API, *Immagini satellitari*);
  *Nodi di rete...*: gli altri computer che lavorano a questo progetto dalla cartella condivisa (la
  cartella da dare a `ppg node` su ognuno, i nodi presenti con core e GPU, l'interruttore che affida
  loro punti chiave e mappe di profondità PatchMatch, ricordato e applicato al programma in esecuzione come
  `PPG_NODES`; `ppg remote` per una macchina via SSH: vedi la
  pagina dell'API, *Altri computer*);
  *Plugin...*: i plugin installati e quelli del repository dei plugin (installa, attiva, disattiva,
  rimuovi; le loro azioni vanno nel menu *Plugin*, i loro formati in *File > Esporta*: vedi
  [plugins.md](plugins.md));
  *Patch dell'ortofoto (assegna immagini)*: disegna un'area e scegline le foto, assegna foto al
  poligono selezionato, aggiorna l'ortofoto con le patch, togli tutte le patch (vedi sotto).
* **PyArchInit**: connessione tramite `config.cfg` (o un percorso SQLite / un URL PostgreSQL), uso del
  database demo, pubblicazione di ortofoto/DEM/modello con i collegamenti alle US, pubblicazione delle
  unità disegnate in `pyunitastratigrafiche`, caricamento dei poligoni delle unità esistenti
  sull'ortofoto, pubblicazione delle quote in `pyarchinit_quote`.
* **Vista**: pannelli, viste (F2 modello, F3 ortofoto/DEM, F4 foto), console (Ctrl+Maiusc+P), tema.
* **Aiuto**: documentazione in linea, diagnostica dell'ambiente, cartella dei log, *Controlla aggiornamenti*,
  *Controlla gli aggiornamenti all'avvio*, informazioni.

## Ortofoto a piena risoluzione e patch

* **Foto da un video** (*Flusso di lavoro > Aggiungi video*): i fotogrammi si scelgono quando la vista
  si è spostata di una frazione della sua larghezza (il più nitido attorno a quel momento) o uno ogni
  pochi secondi, scritti in una cartella accanto al video e aggiunti come foto; con il `.SRT` di un
  drone DJI accanto al video ogni fotogramma riceve la sua posizione GPS. **Qualità delle immagini**
  (tasto destro nel pannello Foto): la nitidezza di ogni foto rispetto alle più nitide (nel tooltip,
  *sfocata* sotto 0,5), *Disattiva le foto sfocate*, attiva o disattiva le foto selezionate,
  maschere delle foto selezionate.
* **Media nel pannello PyArchInit**: selezionando una US (o un sito) nell'albero compaiono le
  miniature dei media collegati in PyArchInit (`media_to_entity_table`); un doppio clic apre il file
  (l'originale, altrimenti la sua copia ridotta) con il programma del sistema.
* **Strumenti nella vista 3D**: *Sezione* (clic su due punti del modello o della nuvola: la sezione
  della loro traccia in pianta, come disegnata sull'ortofoto), *Pianta a quota* (clic su un punto: la
  pianta alla sua quota) e, in *Riferimenti*, il tasto destro *Sposta nella vista 3D* ricolloca un
  marker con un clic sul modello o sulla nuvola (per i marker messi in 3D o sull'ortofoto; quelli
  misurati nelle foto si spostano nelle foto).
* **Scansioni laser** (*File > Importa > Scansioni laser (E57, PTX)*): una o più scansioni diventano
  la nuvola densa di un nuovo chunk; *Flusso di lavoro > Allinea chunk* (punti, senza scala) le porta
  sul chunk delle foto quando non sono nel suo CRS, *Colori dalle foto (scansioni)* le colora con le
  sue foto, *Unisci chunk* le unisce; DEM, sezioni e misure funzionano su di esse come su qualsiasi
  nuvola densa.
* **Esportazioni per il web** (*File > Esporta*): l'ortofoto e il DEM anche come *COG - Cloud Optimized
  GeoTIFF* (JPEG o WebP con trasparenza per l'ortofoto, ZSTD per il DEM), la nuvola densa come
  *COPC* (`.copc.laz`), il modello come *glTF binario* (`.glb`, con la sua texture), *Modello in Cesium
  3D Tiles* (una cartella con `tileset.json` da aprire in CesiumJS) e *Modello per ArcGIS e Cesium (I3S,
  .slpk)* (un pacchetto di scene layer Integrated Mesh con livelli di dettaglio, ogni nodo con la
  propria texture: in WGS84 per ArcGIS Online, le scene globali e CesiumJS, o nel CRS proiettato del
  progetto per le scene locali di ArcGIS Pro; si possono scegliere le facce per nodo e la dimensione
  della texture di un nodo), il *Modello leggero con normal map*
  (poche facce, una texture di colore e una normal map cotte dal modello completo: GLB e OBJ per il
  web, Sketchfab e i motori di gioco) e il *Video di sorvolo* (un MP4 di una camera che orbita attorno
  al modello o alla nuvola: risoluzione fino a 4K, durata, giri, altezza della camera, zoom). Quelle
  grandi girano in background.
* **Misure**: lo strumento *Area* della barra di disegno (trascina un riquadro o clicca un poligono;
  oppure *Misura l'area e la superficie* su un poligono selezionato) dà l'area in pianta e quella
  della superficie dal DEM e dal modello, la pendenza media e le quote, e tiene il contorno nel layer
  *Misure*. Sul DEM la barra in basso mostra pendenza ed esposizione sotto il cursore e, con un DTM
  conservato come versione, l'altezza dal suolo. *Strumenti > Pendenza / Esposizione del DEM* creano
  i due raster (dal bianco al rosso; una ruota di colori dal nord) e li mostrano, con i gradi sotto il
  cursore. La vista 3D colora la nuvola densa per *Altezza dal suolo* una volta classificato il
  terreno.
* **Fasi dello scavo** (*Strumenti > Fasi dello scavo*): ogni chunk con un DEM è un rilievo dello
  scavo - una giornata, la fine di una US - in un unico sistema (una georeferenziazione comune, o
  *Allinea chunk*), nell'ordine della sua data (l'EXIF delle foto, o scritta nella finestra). Le unità
  disegnate su una fase le appartengono (cambiando chunk ogni chunk tiene i suoi disegni) ed erano in
  luce allora: la finestra dà il volume asportato e aggiunto da ogni fase alla successiva, anche
  dentro ogni unità, e propone che un'unità messa in luce prima *copra* le unità che sovrappone in
  pianta messe in luce dopo; i rapporti spuntati vanno nelle schede US di PyArchInit in entrambe le
  direzioni (mai due volte). Un doppio clic su una fase la mostra; *Salva CSV* scrive il resoconto.
* **Proposta di US** (*Strumenti > Proponi US e rapporti (Extended Matrix)*, anche nel menu dello
  strumento SAM): le unità del chunk senza numero - quelle che SAM ha trovato sull'intera ortofoto, o
  disegnate - ricevono un numero di US, prima le più alte, dopo l'ultima US del sito in PyArchInit e
  del progetto; USM dove la maggior parte della nuvola densa classificata al loro interno è muratura
  (taglio, pavimento, deposito e roccia danno la definizione stratigrafica; vegetazione, reperti e
  attrezzatura di scavo sono proposti senza spunta); area in pianta, quote dal DEM e colore Munsell
  dell'ortofoto (con la calibrazione dei colori) vanno nei campi descrizione, colore e quote della
  scheda. I rapporti vengono dai contatti in pianta, con il gradino del DEM attraverso di essi:
  l'unità più alta lungo il contatto (almeno del gradino scelto, 2 cm) *Copre* l'altra; un'unità
  chiusa in un'altra per tre quarti del suo contorno e non più alta è il riempimento di un taglio,
  che viene proposto con lei (il riempimento *Riempie* il taglio, il taglio *Taglia* l'unità attorno;
  un'unità che la nuvola dice taglio la taglia direttamente); uno strato contro una muratura *Si
  appoggia a* essa, o la *Copre* se più alto. La finestra elenca unità e rapporti con il motivo di
  ciascuno; numero, tipo e definizione si possono correggere, un doppio clic mostra l'unità. *Applica
  al progetto* numera le unità e aggiunge i tagli (il contorno del loro riempimento, *Contorno US*);
  *Applica e scrivi in PyArchInit* crea anche le schede US (con `node_uuid` ed `entity_uuid`, le
  schede esistenti restano come sono), scrive i rapporti in entrambe le direzioni e i poligoni in
  `pyunitastratigrafiche`: l'Extended Matrix che PyArchInit ed EMStudio costruiscono dalle schede ha
  allora questi nodi e questi archi.
* **Paradati**: ogni esportazione della nuvola, del modello, del DEM o dell'ortofoto riceve accanto un
  `<file>.prov.jsonld`, e così la copia pubblicata nei media di PyArchInit (la cui nota di
  documentazione riceve un riassunto di una riga: foto allineate, RMSE di controllo e di verifica,
  risoluzione, CRS, versione). È W3C PROV in JSON-LD: il file con il suo SHA-256, il passo che l'ha
  prodotto con impostazioni e ora, e i passi precedenti fino alle foto e alla georeferenziazione, il
  programma con le sue librerie e l'operatore (il *Disegnatore* delle schede PyArchInit) - leggibile
  da qualsiasi strumento per i linked data e dall'Extended Matrix.
* **QField in trincea** (*File > Esporta > Pacchetto QField*): una cartella con l'ortofoto ridotta per
  il tablet (Cloud Optimized GeoTIFF, 2 cm per impostazione predefinita), il DEM mostrato come
  ombreggiatura e un GeoPackage con le unità e le quote del chunk, più un progetto QGIS con percorsi
  relativi, etichette e l'elenco dei tipi di US (creato con il Python di QGIS quando QGIS è
  installato; senza, QField apre direttamente il GeoPackage). Copiala sul tablet o su QFieldCloud,
  disegna e correggi in trincea, riporta indietro la cartella: *File > Importa > Modifiche da QField*
  applica al chunk le unità e le quote aggiunte, modificate e cancellate lì (ogni elemento conserva
  un'identità, `ppg_id`), poi *Pubblica in PyArchInit* / *Salva modifiche in PyArchInit* le scrivono
  nel database.
* **Colore Munsell**: *Misura area* (un riquadro, un poligono o un'unità selezionata) dà anche il
  colore dell'ortofoto al suo interno - la mediana dei suoi pixel senza il decimo più scuro e quello
  più chiaro (ombre, sassolini, riflessi) - come notazione Munsell e chip più vicino di una tavola dei
  suoli (`10YR 5/4`). Fotografa un ColorChecker Classic (24 tasselli) in trincea e usa *Strumenti >
  Calibra il colore con la tabella colori* (un riquadro attorno a esso sull'ortofoto): la correzione
  3 x 3 di quel rilievo (stimata in luce lineare rispetto ai tasselli di riferimento, residuo riportato
  come CIEDE2000) resta nel chunk, e i colori di giornate e fotocamere diverse diventano confrontabili.
  Serve colour-science (in `[gui]`, `[all]` o `[munsell]`).
* **Disegno pietra a pietra** (strumento SAM *Pietre*, sull'ortofoto o sull'ortofoto di un prospetto):
  trascina un riquadro su un paramento murario o un lastricato e SAM 2.1 contorna ogni oggetto al suo
  interno; restano le pietre (diametro equivalente tra la pietra più piccola e la più grande richieste,
  forma compatta), vanno via la malta e la terra. Formano il layer *Pietre* di quell'immagine
  (conservato con il progetto; un riquadro disegnato di nuovo sostituisce le pietre al suo interno,
  quelle sbagliate si tolgono con *Seleziona* e *Canc*); *Segment Anything 2.1 > Esporta il disegno
  delle pietre* le scrive come DXF (polilinee chiuse sul layer `PIETRE`), SVG in scala o GeoJSON,
  nelle coordinate dell'immagine (metri lungo il muro e in altezza per un prospetto).
* **Visualizzazioni del rilievo** (*Strumenti > Visualizzazioni del rilievo*): sky-view factor (quanto
  cielo vede ogni cella: scuro nei fossati e ai piedi dei muri), openness positiva e negativa (creste e
  muri, fossati e buche), rilievo locale (il DEM meno la sua media attorno: rosso sopra, blu sotto),
  rilievo multiscala (MSRM, forme da poche celle fino al diametro di ricerca, tutte insieme) e
  un'ombreggiatura illuminata da 16 direzioni. La finestra propone un raggio di ricerca di circa 20
  celle; il raster si vede subito (grigi stirati sull'intero raster, terreno piatto al centro per
  l'openness), resta nell'area di lavoro e il valore sotto il cursore è nella barra in basso. Su un
  tell la griglia degli edifici, i muri e le vecchie trincee escono da un DSM dove i colori non
  mostrano nulla.
* **Diagnostica dell'allineamento** (*Strumenti > Diagnostica dell'allineamento*): il *Grafo delle
  corrispondenze* (le foto dove sono state scattate, le altre in cerchio; linee tra le foto abbinate,
  più spesse e più verdi con più corrispondenze, rosse sotto 30; gruppi separati e foto isolate
  elencati; un doppio clic su una linea apre la coppia), le *Corrispondenze tra due foto* (le due
  selezionate in *Foto*, o tasto destro su di esse: corrispondenze verificate in verde, candidate
  scartate in rosso, punti di legame in blu), i *Residui dei punti di legame* nella vista della foto
  (verdi sotto 1 px, arancioni sotto 3, rossi oltre; frecce 20 volte più lunghe; anche dal tasto
  destro su una foto allineata) e la *Calibrazione della camera*: i parametri di ogni sensore,
  modificabili come valori di partenza, il modello, *Calibrazione fissa* (l'allineamento e
  l'ottimizzazione non la cambiano) e un grafico della distorsione (frecce sul fotogramma e lo
  spostamento radiale verso l'angolo). In *Riferimenti* il tasto destro inoltre *unisce* i marker
  selezionati (*Unisci i marker...*: un target codificato e il GCP messo a mano sopra di esso) o li
  *elimina* (*Elimina marker...*).
* **Annulla e ripeti** (*Modifica > Annulla / Ripeti*, Cmd/Ctrl+Z e Cmd/Ctrl+Maiusc+Z; il menu dice
  cosa verrà annullato): disegni (unità, quote, sezioni, modifiche in attesa di essere salvate in
  PyArchInit), marker e le loro misure nelle foto, barre di scala, patch dell'ortofoto (l'ortofoto
  viene aggiornata di nuovo), la regione di ricostruzione e le modifiche dei prodotti sul posto -
  classi date a mano, punti eliminati o tenuti con il lazo, pulizia e ritaglio della nuvola densa,
  facce del modello, punti di legame, patch della texture (i loro file vengono copiati da parte prima
  della modifica; su macOS un clone, istantaneo e senza spazio). I passi di elaborazione
  (allineamento, nuvola densa...) non si annullano. La storia ricomincia quando si apre un altro
  progetto o chunk.
* **Elaborazione batch** (*Flusso di lavoro > Elaborazione batch*): un elenco di passi - allineamento,
  prodotti, esportazioni, la precisione e il controllo sul campo - ciascuno su tutti i chunk o su
  quelli scelti, con i suoi parametri (*Aggiungi* per gruppo, *Modifica* o doppio clic, *Su*/*Giu'*,
  *Carica*/*Salva* come JSON), i tentativi per un passo che fallisce e cosa fare poi. Il progetto
  viene salvato dopo ogni passo; se il batch si ferma (corrente mancata, crash, *Annulla*) la volta
  dopo propone di riprendere da dove si era fermato. I passi falliti sono elencati nel log.
* **Controllo sul campo** (*Flusso di lavoro > Controllo prima di lasciare il sito*), subito dopo
  l'allineamento, mentre la trincea è ancora aperta: la mappa di quante foto inquadrano ogni punto
  (rosso scuro nessuna, rosso una, arancione due, giallo tre o quattro, verde di più; il numero di
  foto sotto il cursore; *Copertura delle foto* nell'area di lavoro) e un riepilogo - percentuale e
  area viste da meno di tre foto, foto non allineate o sfocate, marker in nessuna foto, in una o due
  soltanto.
* **Precisione** (*Precisione* in *Riferimenti*, o *Flusso di lavoro > Stima la precisione*): la
  deviazione standard di ogni posizione di camera, marker e punto di legame dalla covarianza della
  compensazione, rispetto ai punti di controllo e alla loro accuratezza (o al GPS delle foto); una
  colonna *Precisione X/Y/Z (mm)* per marker e camere, la mediana dei punti di legame sotto le
  tabelle, la precisione della camera nel tooltip di una foto. Con un DEM, la mappa della sua
  precisione verticale si apre nella vista raster (verde piccola, rosso grande, sigma Z sotto il
  cursore; *Precisione del DEM* nell'area di lavoro). Una quota presa dopo conserva la sua precisione
  (anche in `rilievo_originale` per PyArchInit e nell'IFC). Un nuovo allineamento o una nuova
  georeferenziazione cancellano la precisione vecchia.
* **Foto a 360 gradi e obiettivi fisheye** (*Flusso di lavoro > Aggiungi foto 360°*): ogni foto
  equirettangolare diventa le sei facce di un cubo (le facce da tenere - *Sotto* spesso mostra solo
  il treppiede - la loro ampiezza in gradi e la dimensione), scritte in `facce_360` accanto alle foto;
  nell'allineamento le facce di una foto restano insieme come un rig. Un obiettivo fisheye si
  riconosce dall'EXIF; altrimenti *Modello dell'obiettivo...* (tasto destro nel pannello Foto) lo
  imposta per i sensori delle foto selezionate (normale, fisheye, distorsione forte, nessuna). Il
  tooltip di una faccia dice la sua foto.
* **Regione di ricostruzione** (*Flusso di lavoro > Regione di ricostruzione*): una scatola (gialla
  nella vista 3D, *Regione* la mostra o la nasconde) attorno ai punti di legame, girata verso la vista
  3D, sopra il poligono selezionato della pianta, o con centro, dimensioni e rotazione scritti a
  mano; mappe di profondità, nuvola densa e modello costruiti dopo restano al suo interno - la trincea
  e non il campo attorno, il vaso e non il tavolo - e così DEM e ortofoto. *Togli la regione* torna a
  tutto ciò che le foto vedono.
* **Modifica del DEM** (barra di disegno: *Modifica DEM*, trascina un riquadro o clicca un poligono;
  oppure *Modifica il DEM in quest'area* nel menu di un poligono selezionato): le quote dentro l'area
  vengono ricalcolate dal terreno appena fuori - il piano che lo approssima, un'interpolazione
  lineare, IDW o una quota costante - e le linee selezionate possono fare da breakline (il bordo di un
  muro o di un gradino tiene le sue quote). Alberi, tende, veicoli, cumuli di terra o buchi della
  ricostruzione escono da curve di livello e volumi; il DEM com'era resta tra le versioni (confrontalo,
  rendilo di nuovo attivo).
* **Prospetti** (*Flusso di lavoro > Prospetto o sviluppo cilindrico*, o *Prospetto* nel pannello
  Sezioni): l'ortofoto e il rilievo di un muro sul piano verticale di una traccia di sezione (visto da
  dove stanno le foto, o dal lato scelto), sul piano di tre o più marker selezionati, o lo sviluppo di
  un pozzo o di una torre sul cilindro di un poligono selezionato (visto da dentro o da fuori).
  *Profondita' davanti / dietro* tengono solo la superficie vicina al piano: un'impalcatura o una
  sezione davanti al muro non lo nasconde. I prospetti sono elencati nell'area di lavoro
  (*Prospetti*): un doppio clic mostra l'ortofoto nella vista raster con il rilievo come Z sotto il
  cursore (distanza dal piano verso chi guarda: rigonfiamenti e strapiombi di un muro); il tasto destro
  esporta ortofoto, rilievo e piano, o li elimina.
* **Costruzione dell'ortofoto** (*Flusso di lavoro > Ortofoto*): *Fusione* *Mosaico senza cuciture*
  (predefinita: i dettagli di ogni cella dalla sua foto migliore, le differenze di luce sfumate lungo
  le cuciture), *Nessuna (una foto per cella)* o *Media delle foto*; *Correzione colore* (esposizione
  di ogni foto e vignettatura di ogni obiettivo, misurate la prima volta su punti visti da più foto, o
  prima con *Strumenti > Calibra i colori delle foto*); *Togli persone e mezzi visti da poche foto*
  (dove tre o più foto vedono una cella, la migliore che concorda con le altre); *Riempi i piccoli
  buchi* (fino a 100 pixel che nessuna foto vede, dai loro bordi; i buchi più grandi restano
  trasparenti). Ogni cella sceglie tra tutte le foto che la inquadrano, così nessuna cucitura segue i
  tasselli. Sulla trincea sintetica con metà delle foto più scure del 30 % e tutte vignettate, il
  mosaico corretto differisce da quello delle foto intatte di 0,3 livelli nelle basse frequenze (25
  senza correzione).
* **Zoom**: la vista ortofoto/DEM apre un raster grande da una copia ridotta (al massimo 6.000 px di
  lato, una delle overview del file) e, quando lo zoom si ferma oltre quella risoluzione, rilegge
  l'area visibile (più un margine) alla risoluzione dello schermo, fino a quella piena, in un thread.
  Il dettaglio del DEM tiene i limiti di colore dell'intero DEM. Tell Abu adh-Dhuhur (44.952 px):
  0,7 s per aprire, 10,8 cm/px prima del dettaglio, 1,38 cm/px dopo 0,37 s.
* **Patch** (barra di disegno, o *Strumenti > Patch dell'ortofoto*): trascina un riquadro o clicca i
  vertici di un poligono e fai doppio clic, oppure seleziona un poligono (un'unità) e scegli *Assegna
  immagini a quest'area* dal suo menu. La finestra mostra l'area com'è ora e, per la foto scelta
  nell'elenco, l'area come verrebbe (il resto com'è); l'elenco ha le foto che inquadrano l'area, le
  migliori per prime (frazione inquadrata x vicinanza, verticalità e centralità), con la frazione
  inquadrata, l'angolo dalla verticale, il pixel al suolo, la parte dell'area che l'ortofoto prende
  ora da quella foto e, nel tooltip, quanto di essa il modello nasconde (chiome degli alberi, muri).
  Scegli una o più foto (Maiusc/Cmd), *Usa solo* o *Escludi*, *Sul DEM* (una true orthophoto) o *Sul
  suolo attorno all'area* (le quote lungo il poligono interpolate all'interno: alberi, veicoli e
  persone escono come nella foto, e i buchi del DEM vengono riempiti), e a scelta la texture del
  modello nell'area. *Applica* salva la patch e ridisegna solo le sue celle, poi le overview di quella
  finestra; la vista resta dov'era.
* Le patch sono un layer, *Patch ortofoto*: selezionane una e premi Canc (o *Elimina selezionati*) e
  l'ortofoto viene ridisegnata lì senza di essa. Una costruzione successiva dell'ortofoto riapplica
  tutte le patch; *Aggiorna l'ortofoto con le patch* applica quelle non ancora applicate (dopo un
  aggiornamento interrotto). Un'ortofoto di un altro programma (esportata da Metashape) viene copiata
  nella cartella del chunk alla prima patch, e i suoi pixel tornano dove si toglie una patch.
* **Texture**: nella vista 3D, il lazo sul modello propone *Correggi la texture di queste facce
  (assegna le immagini)*: le foto che vedono quelle facce, ciascuna con la parte della foto dove
  stanno; ogni faccia prende la migliore delle foto scelte che la vede.

## Barre di scala

**Marker.** Un marker misurato in una sola foto sta dove il suo raggio incontra la superficie (la
nuvola densa, altrimenti i punti di legame: un piano locale adattato attorno al raggio, largo circa
20 pixel), quindi ha coordinate, si vede sul modello e sull'ortofoto e può subito essere l'estremo di
una barra di scala; misurato in una seconda foto viene triangolato (sulla trincea sintetica i marker
da una sola foto stanno entro 0,6 mm dai target rilevati). I marker si possono aggiungere anche senza
una foto, come *Add Marker* di Metashape: *Marker* nella vista 3D (la vista si blocca; clicca un
punto del modello o della nuvola: il punto mostrato più vicino sotto il cursore, affinato lungo la
linea di vista sulla nuvola densa completa) e *Marker* nella barra di disegno (clicca l'ortofoto o il
DEM: la quota dal DEM a piena risoluzione, altrimenti dalla nuvola lungo la verticale). Il nuovo
marker (`point 1`, `point 2`, ...) viene proiettato sulle foto che lo vedono, non nascosto dalla
superficie (bandierine blu: trascinane una in una foto per fissarla); i marker sono disegnati
sull'ortofoto come layer *Marker*.

**Posizionamento guidato.** Come in Metashape, un marker collocato su una foto (o in 3D) riceve
bandierine blu sulle altre foto che lo vedono (al massimo 60, prima quelle dove cade più vicino al
centro), e il pannello *Foto* viene filtrato su di esso. Apri una foto (doppio clic): è centrata sul
marker a piena risoluzione; trascina la bandierina blu sul punto o fai doppio clic su di essa quando
è giusta, poi Pagina giù per la foto successiva. Ogni conferma ricolloca il marker (due bandierine
verdi: triangolato) e sposta le bandierine blu delle altre foto dove sta ora. Le bandierine blu sono
stime, non misure: non spostano mai un marker che ne ha una verde, né entrano nell'ottimizzazione
delle camere. *Affina le bandierine blu* (tasto destro in *Riferimenti*, per i marker selezionati o
per tutti, o *Flusso di lavoro > Affina le bandierine blu dei marker*) cerca il punto di ogni
bandierina blu nella sua foto per correlazione d'immagine, sulla superficie attorno al marker: le
bandierine trovate diventano misure (automatiche, come i target rilevati), le altre restano blu da
confermare a mano.

Una barra di scala è la distanza nota tra due marker, come in Metashape. In *Riferimenti* seleziona
due marker (Cmd/Maiusc + clic) e scegli *Crea barra di scala tra A e B...* dal menu del tasto destro,
oppure premi *Barra di scala...* (i marker selezionati vengono per primi nella finestra). La finestra
chiede la distanza in metri (tra i centri dei target), la sua accuratezza (pesa la barra quando ce ne
sono più di una), se è una barra di check e se aggiornare subito la scala.

* **Le barre di controllo** scalano il chunk: la scala è la soluzione ai minimi quadrati di *scala x
  lunghezza nel modello = distanza*, ogni barra pesata con `1 / accuracy²` (di due barre misurate
  ugualmente bene conta di più la più lunga). Senza punti di controllo il chunk viene scalato in
  coordinate locali (CRS `LOCAL`): un oggetto, un reperto, un muro, una trincea.
  La distanza di una barra è quella 3D: con un dislivello è maggiore della distanza in pianta
  misurata sull'ortofoto.
* **Chunk georeferenziati**: una barra nuova viene proposta come check (misura la scala senza
  cambiarla). Una barra di controllo su un chunk georeferenziato altrove (un progetto importato) senza
  riferimenti con cui ricalcolarlo lo scala attorno alla barra, nel suo CRS; quando la scala cambia,
  il log dice che il DEM e l'ortofoto sono ancora in quella vecchia e vanno ricostruiti.
* **Le barre di check** (*Check* spuntato) non scalano: il loro errore dice quanto è buona la scala.
* La tabella sotto i marker ha una riga per barra: *Usa* (togli la spunta per lasciarla fuori), nome,
  marker, *Distanza* e *Accur.* (doppio clic per cambiarle: l'errore si aggiorna subito), *Check*, la
  distanza misurata sul modello (*Stimata*) e l'errore, in rosso quando supera tre volte
  l'accuratezza (almeno 5 mm). Il tasto destro sulle righe elimina le barre. Il riepilogo mostra
  *RMSE barre* (di controllo) e *RMSE barre check*, e come è stato collocato il chunk: *scala dalle
  barre, coordinate locali* o *scala da riferimenti e barre insieme*.
* **Foto con GPS**: quando le loro posizioni coprono meno di dieci volte la loro accuratezza (un
  telefono o un drone sopra un oggetto o una trincea) vengono lasciate fuori e le barre scalano il
  chunk in coordinate locali. Su un'area più grande (un volo su un sito), o con tre o più punti di
  controllo, le posizioni delle foto, i punti di controllo e le barre vengono compensati insieme,
  ciascuno pesato con `1 / accuracy²`: posizioni e punti collocano e orientano il rilievo, la scala
  viene da tutti.
* La vista 3D disegna le barre tra i loro marker, le barre di controllo in azzurro e quelle di check
  tratteggiate in arancione, con lunghezza ed errore; l'area di lavoro le elenca sotto il chunk.
* *Importa barre...* (o *Flusso di lavoro > Importa barre di scala (CSV)*): un file di testo con
  `marker_a, marker_b, distance[, accuracy[, check]]` per riga, separati da virgole, punti e virgola,
  tabulazioni o spazi; marker per nome (`target 12`) o codice del target (`12`); una riga di
  intestazione viene saltata e le coppie i cui marker non sono nel chunk sono elencate nel log.
* *Ottimizza camere* usa anche le barre, come fa Metashape: con due o più barre di controllo, o con
  punti di controllo, i marker alle estremità delle barre vengono fissati dove le barre hanno la loro
  lunghezza (ciascuno spostato quanto permette la sua precisione nelle foto, `MARKER_PIXEL_ACCURACY`
  0,5 px) e il blocco si piega verso di loro quanto i punti di legame consentono; una sola barra dà
  soltanto la scala. Il log riporta gli errori delle barre prima e dopo. Con il motore *Proprio* le
  barre sono distanze tra i loro marker dentro la compensazione stessa, i punti di controllo vengono
  avvicinati alle loro coordinate secondo la loro accuratezza invece di essere fissati, possono
  entrare le posizioni GNSS delle foto (con un bias per volo stimato insieme al blocco: una base RTK
  su un punto sbagliato, un offset dell'antenna) e il rolling shutter dei droni (una velocità di ogni
  foto durante la lettura del sensore). Su terreno piatto con sole foto nadirali un bias verticale si
  confonde con la focale: servono GCP o foto oblique. Il log riporta il bias di ogni volo e la
  velocità mediana.
* I progetti importati da Metashape portano con sé le barre di scala (distanza, accuratezza, barre di
  check); i task di WebODM portano le pose delle camere e i loro GCP come marker, così si possono
  tracciare barre tra di essi.
* *Strumenti > Target > Barra di scala stampabile...*: una pagina per barra con due target codificati
  a distanza nota (gli ultimi codici del dizionario, 48-49 per `aruco_4x4_50`, così non si confondono
  mai con i fogli di target e i tappetini per turntable), un righello di 10 cm per controllare che la
  stampa sia al 100 % e il CSV delle barre accanto al PDF: incolla la pagina su qualcosa di rigido,
  fotografala con l'oggetto, rileva i marker e importa il CSV.

## Demo

**File > Crea e apri progetto demo** (o `ppg demo <folder> --process`) scrive una trincea sintetica
di 4 x 3 m con un moncone di muro, 28 foto e 8 target ArUco a coordinate EPSG:3004, una ciotola e un
frammento come mesh, target stampabili e un database PyArchInit (schema completo di PyArchInit 5)
con il sito *Saggio dimostrativo* (US 1-3, poligoni, quote, una traccia di sezione, ceramica e
reperti) più una villa romana e una necropoli con decine di unità, poi apre il progetto e si collega
al database.

La **demo della ceramica** (`ceramica/ceramica.ppg`, 32 foto della ciotola su un tappetino per
turntable con 12 target ArUco in un sistema metrico locale) mostra il flusso di lavoro a distanza
ravvicinata: *Elabora tutto* senza DEM e ortofoto, poi *Strumenti > Profilo ceramico da mesh* sul
modello esportato; con `--process` la demo contiene già `ceramica/profilo.svg` (attesi: orlo 16 cm,
altezza 12 cm, parete 6 mm). La finestra della demo ti lascia scegliere quale progetto aprire alla
fine.

## Note di robustezza

* Le foto tenute "solo online" da Dropbox, iCloud o OneDrive sono file segnaposto vuoti: il passo della nuvola densa le elenca
  prima di partire e propone di proseguire senza di esse (`build_depth_maps(skip_unreadable=True)`).
* Le finestre di messaggio sono quelle di Qt (non native): l'`NSAlert` nativo di macOS 26/27
  abortisce il processo con Qt 6.
* La ricostruzione di Poisson gira in un processo figlio (`engine/isolate.py`): un crash del codice
  nativo diventa un errore nel log, il passo riprova a una profondità minore e alla fine costruisce
  un campo di altezze 2,5D.
* Open3D viene importato prima di rasterio/SciPy (`_env.load_open3d_first`) e i dati PROJ esportati
  da Anaconda o QGIS sono sostituiti da quelli di rasterio, anche per i thread di lavoro.

## Dove sono gli strumenti di archeologia, IA e BIM

| Attività | Interfaccia | Riga di comando / API |
| --- | --- | --- |
| Collegare PyArchInit | all'avvio l'interfaccia si collega da sola al database configurato in PyArchInit 5 (`~/pyarchinit_5/pyarchinit_DB_folder/config.cfg`, opzione *Connetti all'avvio*). *PyArchInit > Connetti...* apre la finestra di connessione: la configurazione di PyArchInit, un file SQLite/SpatiaLite o un server PostgreSQL/PostGIS (host, porta, database, utente, password, *Prova connessione*). *Usa il database demo* crea `demo.sqlite` con lo schema completo di PyArchInit 5 (tre siti) dentro `~/pyarchinit_5/pyarchinit_DB_folder` quando quell'installazione esiste, così QGIS può aprire lo stesso file; `config.cfg` non viene mai toccato. Il pannello di destra elenca siti, US e documentazione | `PyArchInit.connect(url=..., config_path=...)`, `.sites()`, `.stratigraphic_units(sito)`, `.documentation(sito)` |
| Siti | un sito deve avere la sua *scheda Sito* prima di pubblicare qualsiasi cosa: *PyArchInit > Nuovo sito...* (o *Nuovo sito* nel pannello e nella finestra di pubblicazione) crea il record; pubblicare su un sito che non esiste lo crea automaticamente | `pa.ensure_site(sito, comune=...)`, `pa.site_exists(sito)` |
| Pubblicare i prodotti | *PyArchInit > Pubblica ortofoto / DEM / modello*: scegli il sito, il nome del documento e le US da collegare; i raster vengono copiati accanto alla cartella dei media di PyArchInit, con le miniature e un record in `documentazione_table` | `ppg pyarchinit publish scavo.ppg "Sito" "Nome" --dem --model --link-us 1:101`; `pa.publish_orthomosaic/dem/model` |
| Sito del progetto | ogni unità e quota appartiene a uno scavo: il sito si sceglie una volta (*PyArchInit > Sito del progetto*, la casella in cima al pannello PyArchInit, o la domanda posta la prima volta che si disegna o si pubblica qualcosa) e si salva nel progetto. Un database PyArchInit può contenere più siti: il pannello elenca allora solo le US e i documenti del sito del progetto; la scheda US ha il sito tra i siti del database | `doc.meta["sito"]` |
| Unità stratigrafiche | *Disegna US* nella barra di disegno (clic sui vertici, doppio clic per chiudere; con *Mano libera* premi, traccia e rilascia), poi i campi di `pyunitastratigrafiche` di PyArchInit: area, US (o una US esistente del sito), tipo di unità (`unita_tipo_s`), indice stratigrafico (`stratigraph_index_us`: 1 caratterizzazione, 2 contorno US), tipo (`tipo_us_s`), disegnatore, data, rilievo originale; il layer mostra le stesse colonne (`scavo_s`, `area_s`, `us_s`...). Le unità disegnate si salvano nel progetto e tornano quando lo si riapre. *Snap* (con il suo menu: vertici, segmenti, tolleranza 6/12/20 px) mette i punti di ogni strumento di disegno esattamente sui vertici o sui lati dei layer visibili, segnalati da un quadrato magenta (vertice) o un cerchio (segmento): le unità tracciate lungo un confine esistente lo condividono, senza vuoti né sovrapposizioni; *PyArchInit > Pubblica unita' e quote disegnate* scrive `pyunitastratigrafiche`; *Carica poligoni US* mostra quelle esistenti | `pa.publish_units(units, sito)`, `pa.us_polygons(sito)`, `archaeo.stratigraphy.export_units` |
| Selezionare, eliminare, unire | *Seleziona* nella barra di disegno: clic su un elemento, Maiusc/Cmd + clic per aggiungere o togliere, trascina un riquadro per selezionare tutto ciò che tocca (i layer nascosti sono saltati); la selezione è contornata in azzurro e, per un singolo elemento, i suoi attributi compaiono nel pannello Layer. *Elimina* (o il tasto Canc) toglie unità, singole parti di unità multiparte, tracce di sezione con i loro disegni, linee di curve/piante a quota e punti; *Unisci* fonde le unità in un solo poligono (scegli tu di quale tenere numero di US, area e tipo; le fessure sottili tra unità disegnate fianco a fianco vengono chiuse) o linee contigue dello stesso layer. Il tasto destro apre gli stessi comandi, Esc svuota la selezione. I poligoni caricati da PyArchInit cambiano in `pyunitastratigrafiche` solo con *Salva modifiche in PyArchInit* (pannello Layer), che chiede conferma prima di eliminare | `pa.update_unit(gid, geometry=..., crs=...)`, `pa.delete_unit(gid)` |
| Quote | *Quota* nella barra di disegno, separata da SAM: un clic legge la quota sul DEM a piena risoluzione, poi i campi di `pyarchinit_quote` (area, US, tipo di unità, quota (modificabile), unità di misura, disegnatore, data, rilievo originale); *Non chiedere piu'* li tiene per i clic successivi (scegli di nuovo lo strumento per cambiarli). I punti vanno nel layer *Quote*, si salvano nel progetto e si scrivono con *Pubblica* | `pa.publish_quote(points, sito, crs)`, `pa.publish_elevations(chunk, sito, area, us, points)` |
| Segment Anything 2.1 | installato su richiesta in un ambiente proprio, con la build di PyTorch per la scheda grafica (CUDA sulle schede NVIDIA, scelta dall'installatore su Windows e Linux; la GPU Apple tramite MPS sul Mac, circa 3,5 volte più veloce della CPU su un M4 Pro); la finestra alla fine dell'installazione dice quale viene usata. *SAM* nella barra di disegno, tre modi scelti dal suo menu: **Tocco** (clic su un oggetto: SAM guarda la parte di ortofoto sullo schermo, quindi ingrandisci per i dettagli e riduci per le unità grandi), **Riquadro** (trascina un riquadro attorno a un oggetto), **Area** (trascina un riquadro: tutte le unità al suo interno). La maschera compare tratteggiata con la scheda dell'unità: *Salva*, *Annulla* o *Correggi* (Maiusc + clic aggiunge una parte, il tasto destro ne toglie una, Invio salva, Esc scarta). *Tutta l'ortofoto* esamina l'intera ortofoto a tasselli di 1024 pixel che si sovrappongono, alla risoluzione e alla scansione scelte (normale / alta / massima: più punti e ritagli per tassello): gli oggetti tagliati dai tasselli vengono riuniti, i doppioni scartati e le unità non si sovrappongono (le più piccole vengono ritagliate dalle più grandi). Le unità automatiche vanno nel layer *Unita' da SAM* senza numero di US. Il menu sceglie anche il modello (da tiny a large). *Prompt sulle foto* segmenta un oggetto su una foto | `ai.sam.segment_orthomosaic(chunk, segmenter, points=, labels=, box=, bounds=)`, `auto_units(chunk, segmenter, bounds=, resolution=, thoroughness=)`, `read_orthomosaic`, `segment_photo_and_classify(...)` |
| Classificazione | *Strumenti > Classificazione della nuvola*: regole automatiche (terreno, vegetazione, muratura/strutture, rumore; classi ASPRS più le classi archeologiche 64-71), scritte nella nuvola densa (l'esportazione LAS conserva le classi). La vista 3D colora la nuvola per *Classi* (o *Quota*, o con i colori delle foto) con una legenda: togli la spunta a una classe per nasconderla. *Lazo* seleziona punti sullo schermo (clic sui vertici o trascina, doppio clic o Invio per chiudere) e dà loro una classe (a un codice nuovo si può dare un nome, per esempio 120 *Crollo*, mostrato nella legenda); si prendono solo i punti visibili davanti, salvo richiesta, e mai le classi nascoste. I punti corretti a mano vengono ricordati: le regole automatiche e il classificatore li lasciano come sono. *Addestra il classificatore* impara le classi da quei punti (o da tutti i punti classificati): la geometria attorno a ogni punto a tre scale, l'altezza dal suolo e il colore, con una random forest (o gradient boosting per un file più piccolo); riporta l'accuratezza per classe e classifica il resto della nuvola. Il modello resta nel progetto; *Salva il classificatore...* / *Carica un classificatore (.ppgclf)...* lo riusano su altri scavi con una spaziatura dei punti simile. *Classifica con il classificatore* chiede una confidenza minima (sotto, i punti restano non classificati); *Dimentica le correzioni a mano* toglie i contrassegni, classi invariate. *Classi dalle US (poligoni)* dà ai punti dentro ogni unità disegnata o caricata (in pianta) la classe di quell'unità (`US 12 (area 1)`, codici da 100, conservati se lo si rilancia; correzioni a mano conservate salvo togliere la spunta) e mostra punti, area in pianta e quote per US in una tabella (*Salva CSV...*). **Le classi guidano i prodotti**: la finestra del DEM elenca le classi della nuvola densa con i loro punti (solo *Terreno* dà un modello del terreno, DTM, con i vuoti sotto ciò che resta fuori interpolati; curve e volumi lo usano poi); la finestra dell'ortofoto rende trasparenti le classi scelte dove sono la superficie visibile (attrezzi, persone, vegetazione); la finestra del modello fa la mesh solo delle classi spuntate; *File > Esporta > Nuvola di punti* esporta solo alcune classi; il menu dello strumento *Sezione* (*Opzioni delle sezioni*) taglia il modello o la nuvola densa e tiene solo le classi spuntate (dal modello, i tratti che poggiano su punti di quelle classi); *Pianta a quota* ha la stessa scelta. Il rumore (classi 7 e 18) non fa mai un DEM o una mesh | `ppg classify scavo.ppg`, `ppg classify-train scavo.ppg modello.ppgclf`, `ppg classify-apply altro.ppg modello.ppgclf`, `ppg classify-units scavo.ppg --csv us.csv`, `ppg dem scavo.ppg --classes 2`, `ppg ortho --hide-classes 3,71`, `ppg mesh --classes 2,64`, `ppg section ... --classes 2`, `ppg export --cloud muri.las --classes 64`; `ai.classify.classify_chunk(chunk)`, `set_class(chunk, mask, code)`, `classify_by_units(chunk, units)`, `class_statistics`; `ai.learn.train_on_chunk(chunk)`, `classify_chunk_with_model(chunk, model)` |
| Sezioni | *Traccia sezione* nella barra di disegno, poi due clic sull'ortofoto: il modello viene tagliato lungo la traccia, il disegno compare nel pannello *Sezioni* (SVG, profilo DXF e DXF 3D in `sezioni/`), la traccia diventa un elemento del layer *Sezioni*. L'SVG è in scala 1:20, o nella scala standard più dettagliata (1:50, 1:100, 1:200...) in cui sta più o meno in un foglio A0 (una sezione di 174 m attraverso un tell: 1:200); la linea del profilo è di 0,5 mm e il passo della griglia DXF si adatta all'altezza della sezione (10 cm in una trincea, 1-2 m su un tell). *Pianta a quota* taglia il modello in orizzontale in un layer proprio | `ppg section scavo.ppg x1 y1 x2 y2 --out sezione.svg`; `archaeo.sections.section(chunk, p1, p2)`, `sections_along(chunk, baseline, interval, half_width)`, `plan_slice(chunk, elevation)`; `pa.publish_section_trace` |
| Curve di livello e volumi | *Curve di livello* (DXF/GeoJSON/GeoPackage) disegna le linee come layer interrogabile (quota, linea direttrice) che sopravvive al passaggio tra DEM e ortofoto. La finestra mostra l'intervallo di quote del DEM e propone un'equidistanza tonda che dà al massimo una cinquantina di livelli (5 cm su una trincea, 50 cm su un tell alto 20 m), chiede conferma prima di un'equidistanza che darebbe più di 300 livelli e offre una *Levigatura* (lisciatura gaussiana del DEM, in metri) per i DSM da drone; le linee tengono i vertici entro mezzo pixel e gli anelli chiusi più piccoli di 25 pixel (rumore) restano fuori, così un tell dà migliaia di linee invece di 158.000, aggiunte alla vista in una volta sola; *Volume scavo / riporto*: il riferimento è la superficie prima del lavoro (un piano alla quota originale del terreno o il DEM prima dello scavo): *scavo* = volume asportato (ora sotto il riferimento), *riporto* = volume aggiunto; con "Quota minima" il deposito sopra il punto più basso. Le celle parziali lungo il bordo del poligono sono pesate per la frazione coperta | `ppg contours`; `archaeo.terrain.contours`, `volume`, `hillshade`, `slope_degrees`; `pa.publish_volume` |
| Visualizzazioni del rilievo | *Visualizzazioni del rilievo*: sky-view factor, openness positiva e negativa, rilievo locale, rilievo multiscala (MSRM), ombreggiatura da 16 direzioni, mostrate subito e conservate nell'area di lavoro | `ppg relief`; `chunk.build_relief`; `archaeo.relief` |
| Profili ceramici | *Strumenti > Profilo ceramico da mesh*: scegli la mesh di un vaso o di un frammento (OBJ/PLY/STL), la scala, le unità e l'asse (stimato dalle normali della superficie, oppure la Z della mesh con l'orlo in alto, come su un turntable, o in basso, un frammento appoggiato sull'orlo). Il diametro dell'orlo viene dal cerchio del labbro, che sposta anche l'asse nel suo centro (così un frammento posato sul turntable lontano dal centro è misurato giusto), con l'arco conservato (*Orlo conservato*, 34 % (25-50) per un terzo dell'orlo); sotto 40 gradi di arco, o con un labbro irregolare, l'orlo è segnato *incerto*, misurato attorno all'asse e non scritto in PyArchInit. Il profilo con diametro massimo/di base, altezza totale o conservata e spessore della parete viene disegnato come SVG/DXF e PNG, in una finestra che resta aperta. *Cerca forme simili* ordina le schede della `pottery_table` di PyArchInit di tutti i siti per i diametri e l'altezza che hanno in comune (l'orlo conta doppio) e, per le schede salvate da questo programma, per la forma del profilo dall'orlo in giù (indipendente dalla dimensione). *Salva in PyArchInit...* scrive le misure in centimetri in una scheda ceramica nuova o scelta (`diametro_rim`, `diametro_max`, `diametro_bottom`, `diametro_height` altezza totale, `diametro_preserved` altezza conservata, `percent` <5 / 5-25 / 25-50 / >50; forma, parte, sagoma e impasto dal thesaurus di PyArchInit); i campi già compilati di una scheda esistente restano salvo spuntare *Sostituisci*. Il disegno va nei media della scheda (PNG collegato come CERAMICA, con SVG e DXF accanto e `.profile.json`, il profilo per le ricerche successive) | `ppg pottery vaso.ply --out profilo.svg --scale 1 [--axis up\|down] [--units mm]`, `ppg pyarchinit pottery-search frammento.ply`, `ppg pyarchinit pottery-save frammento.ply Sito --us 12 --part Orlo`; `archaeo.pottery.measure`, `rim`, `signature`, `draw`; `pa.search_pottery`, `pa.save_pottery` |
| Schede US | un doppio clic su una US nel pannello PyArchInit (o *Scheda US* nei pannelli Layer e Proxy) apre la scheda US di PyArchInit (`us_table`): descrizione, cronologia, dati di scavo, caratteri, quote e misure, e i rapporti stratigrafici come tabella modificabile (*Copre*, *Taglia*, *Si appoggia a*...), salvati nel formato di PyArchInit così che il suo matrix di Harris li legga. *Nuova US* / *PyArchInit > Nuova scheda US* crea un record (anche quello del sito se manca); una combinazione sito/area/US/tipo già esistente viene rifiutata. Un database con una `us_table` ridotta (per esempio il database demo della versione 0.2, o un file creato da altri strumenti) mostra un avviso nella scheda con *Aggiorna struttura del database* (anche nel menu PyArchInit): le colonne mancanti di PyArchInit 5 vengono aggiunte dopo una copia di backup del file, record intatti | `pa.missing_us_columns()`, `pa.upgrade_us_table()`, `pa.us_record`, `pa.create_us(sito, area, us, tipo, **fields)`, `pa.update_us(id_us, **fields)`, `parse_relationships`, `format_relationships` |
| Proxy delle US | *Strumenti > Proxy 3D delle US*: ogni poligono di unità (disegnato, o da un sito PyArchInit) diventa un solido chiuso tra il DEM del progetto (sopra) e una base: un piano a una quota data, il DEM rilevato dopo l'asportazione dell'unità, uno spessore costante o il punto più basso. Il suo volume è uguale al volume di scavo/riporto sulle stesse celle. Il pannello *Proxy US* elenca volume, area in pianta e quote; *Vedi in 3D* li mostra colorati sopra il modello; *Esporta (Extended Matrix)* scrive un OBJ con un oggetto per unità (`US12`, `USM3`) più l'offset delle coordinate; *Pubblica in PyArchInit* registra ogni proxy come media 3D collegato alla sua US; *Quote nella scheda US* scrive le quote min/max nella scheda | `archaeo.proxies.unit_proxy`, `proxies_for_units`, `export_extended_matrix`, `save_proxy` |
| BIM / IFC | *Strumenti > Esporta IFC (BIM)*: scegli le unità (disegnate qui, i poligoni US di un sito PyArchInit, o i *proxy solidi*) e se includere l'intero rilievo; l'esportazione mostra l'avanzamento e, alla fine, una finestra con gli elementi, i loro attributi (`Pset_PyArchInit_US`) e una vista 3D di ciò che è stato scritto. IFC4 in metri con `IfcMapConversion`. Con i proxy il file porta quantità, materiali, gruppi e rapporti: vedi [bim.md](bim.md) | `ppg ifc scavo.ppg rilievo.ifc`; `bim.ifc.export_chunk_ifc(...)`, `export_proxies_ifc(...)`, `read_ifc_elements(path)` |
| Versioni | le finestre di *Nuvola densa*, *Modello*, *DEM* e *Ortofoto* chiedono, quando il prodotto esiste, se conservare la versione attuale (per confrontarla) o sovrascriverla, e il nome della nuova; una costruzione fallita rimette a posto quella conservata. L'area di lavoro mostra il nome della versione attiva accanto a ogni prodotto ed elenca sotto quelle conservate: un doppio clic mostra una versione conservata senza renderla attiva (i raster nella vista ortofoto, nuvole e modelli nella vista 3D con un'etichetta *Versione «...» (non attiva)*; scegliere una vista nell'elenco torna ai prodotti attivi). Tasto destro su una versione conservata: *Rendi attiva* (quella che ogni strumento usa; l'attiva prende il suo posto tra le conservate), *Differenza con il DEM attivo* (attivo meno conservato, rosso dove l'attivo è più alto, blu dove è più basso, dZ sotto il cursore; un avviso quando i due DEM hanno risoluzioni diverse), *Esporta*, *Rinomina*, *Elimina*. Tasto destro su un prodotto: *Conserva una copia come versione* (per esempio prima di classificare di nuovo la nuvola), *Rinomina*. Le versioni conservate stanno in `versions/` dentro la cartella del chunk | `core.versions.keep_active`, `activate`, `versions`, `version_file`, `dem_difference`, `rename`, `delete` |
| Confidenza | *Flusso di lavoro > Pulizia e confidenza > Calcola la confidenza*: per la nuvola densa e/o i vertici del modello, il numero di mappe di profondità che confermano ogni punto (entro l'1 % della sua distanza dalla camera, la tolleranza della fusione; mezzo lato per un vertice); la nuvola fusa ce l'ha già. La vista 3D (*Colori > Confidenza*, anche per il modello) colora le fasce 0 (grigio: nessuna mappa, per esempio un buco chiuso dalla mesh), 1 (rosso), 2, 3-4, 5-9, 10-19, 20 o più (blu); togliere la spunta a una riga della legenda nasconde quei punti o quelle facce, un'anteprima di una pulizia. Servono le mappe di profondità del chunk (*Nuvola densa*, anche *Solo le mappe di profondita'*) | `ppg confidence scavo.ppg --model`; `chunk.compute_confidence("point_cloud" or "model")`, `engine.confidence.depth_map_support` |
| Pulizia | *Pulisci la nuvola densa*: punti sotto una confidenza, punti isolati (filtro statistico: vicini, deviazioni standard), punti senza vicini entro un raggio, gruppi staccati più piccoli di un numero di punti, classi, un colore (con una tolleranza); i punti trovati vengono contati e mostrati in rosso nella vista 3D, poi tolti alla conferma. *Pulisci il modello*: pezzi staccati sotto una frazione del più grande, facce con una confidenza dei vertici bassa, triangoli stirati, topologia, buchi chiusi fino a un raggio, lisciatura di Taubin (bordi fissi), decimazione; colori e confidenza conservati, la texture da rifare. *Pulisci i punti di legame (selezione graduale)*: soglie su errore di riproiezione, numero di foto, incertezza della ricostruzione e accuratezza della proiezione con i conteggi dal vivo, poi l'ottimizzazione delle camere (per i modelli scritti da pose importate conta solo l'incertezza). Il *Lazo* elimina o tiene punti e facce. Ogni pulizia può prima conservare la versione attuale | `ppg clean-cloud`, `ppg clean-model`, `ppg clean-tie-points`; `chunk.clean_point_cloud(...)`, `clean_model(...)`, `clean_tie_points(...)`, `engine.cleaning.point_filters`, `face_filters`, `delete_points`, `delete_faces` |
| Texture | *Costruisci texture*: *Correzione colore* (esposizione delle foto e vignettatura, misurate la prima volta), *Mappatura* automatica, ortofoto adattiva (terreno dall'alto: la texture è l'ortofoto del modello, fatta dal motore dell'ortofoto a tasselli; muri e sezioni hanno celle proprie in una fascia in basso) o generica (ogni faccia una cella propria: oggetti e modelli piccoli). *Dimensione texture* 16384 o 32768 divide la texture in 4 o 16 pagine di 8192 pixel (tasselli della pianta, o gruppi di facce vicine), come fa Metashape: ogni pagina resta leggibile da schede grafiche e visualizzatori. La texture è un'immagine (o una per pagina) accanto a `model.ply`, che porta le coordinate texture di ogni faccia; la vista *Modello con texture* mostra l'intero modello. *File > Importa > Modello e texture dal progetto Metashape* rilegge il modello del `.psx` con colori, confidenza e texture (il modello in uso viene conservato come versione) | `chunk.build_texture(size, mapping="auto")`; `integrations.metashape.reimport_model(chunk)`; `io.mesh_ply.read_mesh`, `write_mesh` |
| Ritaglio | *Ritaglio* tra gli strumenti di disegno (o *File > Esporta > Ritagliati...*, *Pulizia e confidenza > Ritaglia*): trascina un riquadro o clicca un poligono (doppio clic per chiudere) sull'ortofoto o sul DEM; oppure seleziona un poligono (un'unità) e *Ritaglia o esporta con il poligono selezionato*. Poi scegli i prodotti: esportati ritagliati in una cartella (DEM e ortofoto con nodata o trasparenti fuori dal poligono, modello PLY con colori, confidenza e coordinate in doppia precisione o OBJ con la texture, nuvola LAS con classi e confidenza, il poligono come GeoJSON) o ritagliati nel progetto con gli originali conservati come versioni; modello e nuvola si possono anche limitare in altezza | `ppg crop scavo.ppg --polygon "x1,y1 x2,y2 x3,y3" --out ritagli` (o `--bbox`, `--wkt`, `--geojson`, `--in-project`); `chunk.export_dem(path, region=)`, `export_orthomosaic`, `export_model`, `export_point_cloud`, `crop_dem`, `crop_orthomosaic`, `crop_model`, `crop_point_cloud(region=)` |
| QGIS | *QGIS > Apri in QGIS (collegamento dal vivo)*: vedi [integrations.md](integrations.md) | `io.qgis_link.export_layers`, `build_project` |
| Importazione | *File > Importa*: Metashape `.psx`, WebODM/ODM, sessioni Photo3D: vedi [integrations.md](integrations.md) | `ppg import-metashape`, `ppg import-webodm`, `ppg import-photo3d`, `ppg photo3d-job` |

Tutto ciò che fa l'interfaccia è disponibile anche nella console (*Console Python*: `doc`, `chunk`,
`ppg`) e negli script (`ppg run script.py --project scavo.ppg`). Vedi [api.md](api.md) e
[pyarchinit.md](pyarchinit.md).
