# Piani di volo

Il plugin *Piani di volo* progetta un rilievo da drone **prima di andare sul campo**: l'area, la camera, la risoluzione
a terra (GSD) e la quota, le sovrapposizioni, le strisciate e gli scatti, i tempi e le batterie, il volo che segue il
terreno sul DEM del progetto, la **copertura prevista** (quante foto vedranno ogni cella del suolo), i target GNSS come
punti dove il drone scende a fotografarli. Poi scrive i file che leggono i droni e le loro app - KMZ WPML DJI, DJI Fly,
Litchi, QGroundControl, Mission Planner, Parrot - insieme a KML, GeoJSON, GeoPackage e una scheda del volo in Excel o
HTML. **Dopo il volo** confronta il piano con le foto scattate e la copertura ottenuta.

!!! warning "Spazio aereo, permessi e sicurezza sono del pilota"
    Il plugin calcola una rotta: non conosce lo spazio aereo, le zone vietate, gli ostacoli (alberi, pali, edifici,
    gru), il meteo né le regole del tuo paese. Controllare che il volo sia consentito e sicuro, e chiedere i permessi,
    spetta al pilota. Guarda la rotta sulla mappa e nell'app di volo prima di decollare.

Questa pagina descrive la versione 0.1.0 del plugin, che **richiede pyarchinit-photogrammetry 1.7.4**. Il pianificatore -
griglie a serpentina, anelli, percorsi punto per punto, tratte che stanno in una batteria, i profili delle camere -
viene dal pianificatore di Photo3D dello stesso autore. Il README e il codice sono nel [repository dei
plugin](https://github.com/enzococca/pyarchinit-photogrammetry-plugins/tree/main/plugins/flightplan).

## Installazione

```bash
ppg plugins install flightplan
```

oppure *Strumenti > Plugin...* nel programma ([Installare un plugin](guides.md#install)). Il plugin non chiede niente
oltre al programma. La rete serve solo per le tegole pubbliche del terreno: quando le scegli, o con il DEM del progetto
dove finisce (vedi [Terreno](#terrain)). Le tegole scaricate restano su disco nella cartella del plugin.

## Cosa serve

- **Un progetto con un chunk.** I piani restano nel chunk (in `chunk.meta`) e i suoi file in
  `<dati del chunk>/flightplan/`. Il caso migliore è un chunk in un sistema proiettato (UTM, una griglia nazionale):
  puoi disegnare l'area sulla vista 2D e calcolare la copertura.
- **Facoltativi: un DEM e un'ortofoto** del chunk, da un rilievo precedente o da un volo veloce di ricognizione. Il DEM
  dà il volo che segue il terreno e la copertura sul suolo vero; l'ortofoto è lo sfondo delle mappe delle tratte. Senza,
  il piano si fa su un suolo piano alla quota del decollo e le mappe hanno lo sfondo grigio.
- **Un chunk in un sistema locale** (senza CRS) si pianifica solo con un'area da un file o da coordinate in gradi; non
  si disegna niente sulla vista e la copertura non si calcola (serve un sistema proiettato).
- **Senza progetto** la riga di comando fa un piano dalle coordinate in un file JSON (vedi
  [Riga di comando](#command-line)).

## Il pannello

*Plugin > Piani di volo > Pannello Piani di volo* apre il pannello **Piani di volo**, agganciato a destra. Le stesse
azioni sono in un gruppo proprio nella colonna degli strumenti della vista 2D (l'icona del quadricottero): *Disegna
l'area del volo*, *Progetta il volo...*, *Copertura prevista*, *Esporta il piano di volo...*, *Copia in DJI Fly...*,
*Confronta con il volo*.

In alto, **Piano:** sceglie il piano attivo del chunk; **Rimuovi** lo toglie (*Modifica > Annulla* lo rimette con i
suoi file; i file si cancellano davvero alla prossima apertura del progetto). Seguono i passi in ordine; sotto ogni
passo una riga ne dice lo stato: cosa c'è, cosa manca, cosa va rifatto.

### 1. Area

- **Disegna sulla vista**: clic sui vertici del poligono sulla vista 2D (ortofoto, DEM), doppio clic o Invio per
  chiudere, Esc per annullare.
- **Da un layer...**: un poligono già disegnato sulla vista 2D.
- **Da un file (KML, GeoJSON, GeoPackage)...**.
- **Coordinate...**: i vertici in gradi, o nel sistema del chunk.
- **Margine**: metri aggiunti attorno all'area nei blocchi del volo, perché i bordi abbiano la stessa copertura del
  centro. Il poligono disegnato resta com'è; il margine va solo nei blocchi.

*Progetta* di nuovo sulla stessa area **sostituisce il piano attivo**; un'area nuova fa un **piano nuovo**.

### 2. Camera

L'elenco contiene le camere del pianificatore, raggruppate per come volano: **DJI (Pilot, app con l'SDK)**, **DJI Fly
(solo dal telecomando)**, **Altri droni** (Autel, Parrot, Skydio...). I valori dei sensori vengono dalle schede
tecniche. Una camera che non è nell'elenco non si inventa mai; prendila:

- **Dalla foto (EXIF)...**: una foto di quella camera; servono sia `FocalLength` sia `FocalLengthIn35mmFilm`
  nell'EXIF, altrimenti viene rifiutata con un messaggio;
- **Dal chunk**: il sensore calibrato del chunk (serve la focale in millimetri);
- **A mano...**: larghezza e altezza del sensore in mm, focale in mm, pixel sul lato lungo. I campi partono dalla camera
  scelta nell'elenco.

**Alta risoluzione** usa la modalità ad alta risoluzione delle camere che l'hanno (48, 50 o 100 MP, come dice l'elenco delle camere); **Batteria** sono i
minuti di volo su cui conti per una batteria: un piano più lungo si divide in tratte.

### 3. Parametri

- **Obiettivo**: *Ortofoto* (il GSD in cm/px), *Modello del terreno (DEM)* (la cella in cm: il GSD è più fine),
  *Modello 3D completo* (nadirale più due obliqui incrociati), *Ispezione ravvicinata*, *Ricognizione veloce*, *GSD a
  mano*; il valore dell'obiettivo.
- **Quota (m)**: una quota sul decollo al posto dell'obiettivo.
- **Direzione**: *Lungo il lato più lungo* dell'area, oppure *Gradi*.
- **Doppia griglia**: una seconda griglia nadirale incrociata a 90 gradi.
- **Obliqui**: secondo l'obiettivo, sì, no.
- Sovrapposizione **Avanti** e **Laterale** (%), **Velocità** (m/s).
- **Scatto per punto** (un waypoint per ogni foto) o **Scatto a intervallo** (la camera scatta ogni tot secondi).
- **Progetta** fa il piano.

Le sovrapposizioni sono quelle delle strisciate su un suolo piano alla quota del decollo. Quello che il piano dà davvero
sul suolo - dove il terreno sale, ai bordi, sulle strisciate corte - è nel passo [Copertura](#coverage): guardalo lì.

### 4. Piano

La tabella elenca i **blocchi** (griglia, obliqui, anello, percorso, target) e le loro **tratte**, con scatti, minuti,
batterie, GSD e quote. La spunta su un blocco lo fa volare e lo mette nei file; toglila per lasciarlo fuori. Sotto la
tabella ci sono gli avvisi del pianificatore: velocità ridotta perché la camera non scatta abbastanza spesso, quote
oltre 120 m sul decollo (il limite di un drone di serie), strisciate con meno di tre scatti, scatti a meno di 10 m dal
suolo, sovrapposizione laterale sotto il 60 % dove il terreno sale, un GSD più grosso di un quarto dove il terreno scende
sotto il decollo.

- **Decollo sulla vista**: clic dove decolla il drone. Le quote di tutti i file sono **sopra il punto di decollo**.
- **Decollo di questa tratta**: un decollo proprio per la tratta scelta nella tabella (ogni tratta di un volo lungo può
  partire da un altro posto); le sue quote si correggono con la differenza del suolo.

Sulla vista 2D i layer *Piano di volo: area*, *strisciate*, *scatti*, *decollo*, *target* mostrano il piano.

### 5. Terreno { #terrain }

**Segui il terreno** dà a ogni punto una quota propria sul decollo, così l'altezza dal suolo - e il GSD - resta la
stessa in salita e in discesa: quota = altezza dal suolo + (suolo nel punto - suolo al decollo), tenuta fra 10 m e
500 m sul decollo.

- **DEM del progetto**: il DEM del chunk. Un DEM sotto tutto il volo non manda nulla in rete. Solo dove il DEM finisce
  il plugin chiede le tegole pubbliche, spostate dello scarto mediano misurato dove ci sono entrambi; se i due non si
  sovrappongono mai sotto il volo il DEM del progetto resta fuori (quote di riferimenti diversi non si mescolano) e un
  avviso lo dice.
- **Tegole pubbliche (rete)**: le tegole libere del terreno di AWS (Terrain Tiles, terrarium di Mapzen), con dettaglio
  *Dettaglio normale*, *Dettaglio fine* o *Dettaglio finissimo*. Serve la rete; restano in una cache.
- **Togli** torna a una quota per blocco.

Il decollo si misura con la stessa sorgente dei waypoint; un decollo a più di 2000 m dal volo si ignora. Quando
cambiano l'area, il decollo o il DEM, le quote diventano vecchie: ogni esportazione **le ricalcola prima** con la
sorgente con cui il piano è stato calcolato (le tegole restano tegole, il DEM del progetto resta il DEM). Le quote di
ogni blocco ricordano il decollo da cui sono contate: se il decollo è stato spostato si ricalcolano tutti i blocchi che
seguono il terreno, anche quelli spenti. Un piano del pianificatore senza sorgente delle quote si ricalcola solo sul DEM
del progetto, mai sulle tegole se non le scegli. Se il suolo sotto un decollo non si riesce a
rimisurare (niente rete per le tegole, il decollo fuori dal DEM, il DEM che non c'è più) l'esportazione **si rifiuta**
e dice perché: un file non porta mai quote, o discese dei punti speciali, contate dal decollo di prima.

Gli avvisi di *Progetta* (GSD, sovrapposizione, distanza dal suolo) leggono il suolo dal DEM del progetto; chiedono le
tegole pubbliche solo quando le quote del piano vengono da lì.

### 6. Copertura { #coverage }

**Calcola** rimette ogni scatto nel sistema di coordinate del chunk, come una camera con la sua direzione e la sua
inclinazione sopra il suolo del DEM (o un suolo piano al decollo senza DEM), e conta su una griglia di celle **quante
foto vedono ogni cella** e il GSD più fine che una foto dà lì. La riga di stato dà la mediana delle foto per cella, il
10° percentile, la parte dell'area sotto la **Soglia** (5 foto di base), il GSD mediano e la cella.

- **Mostra la copertura** sostituisce l'ortofoto sulla vista 2D con la mappa della copertura: rosso nessuna foto,
  arancio sotto la soglia, giallo-verde fino al doppio della soglia, verde oltre. **Mostra l'ortofoto** torna indietro.
  Il layer *Piano di volo: copertura bassa* contorna le celle sotto la soglia.
- La copertura è la verità da guardare prima di volare: dove il terreno sale o le strisciate sono corte mostra la
  sovrapposizione che il piano dà davvero.
- **Limiti**: **nessuna occlusione** - muri e alberi non nascondono il suolo dietro di loro. Le celle fuori dal DEM si
  prendono **piane al decollo**. Un piano cambiato dopo il calcolo rende la copertura *Da rifare*.

### 7. Target

I target GNSS a terra diventano **punti speciali**: il drone va sopra ogni target, scende a **Metri dal suolo** (10 m di
base: da lì un target A4 si legge) e fa una scala di foto con la camera dritta verso il basso.

- **Da CSV...**: nome, coordinate e quota, in gradi, in UTM, o in metri di un altro sistema (dai il suo EPSG). Senza
  intestazione l'ordine delle colonne si capisce dai numeri; quando è solo un'ipotesi una **riga rossa** sotto il passo
  lo dice: controlla latitudine e longitudine.
- **Dai marker del chunk**: i marker con coordinate misurate. In un chunk non georeferenziato le posizioni dei marker
  sono **stimate** dal rilievo: vanno bene per sorvolarli, **mai come GCP**.
- **Togli** toglie i target dal piano.

Le discese hanno bisogno del suolo sotto i target. Si legge dal DEM del progetto; in un chunk senza DEM il plugin
chiede se usare le **tegole pubbliche (rete)** (sulla riga di comando, `--tiles`). Senza l'uno né le altre
l'esportazione si rifiuta e dice di scegliere le tegole o aggiungere un DEM: la rete non si chiede mai senza che tu lo
voglia.

### 8. Esporta

Scegli il formato e premi **Esporta...**; **Scheda Excel** e **Scheda HTML** scrivono la scheda del volo; **Apri in
QGIS** apre il piano in QGIS. *File > Esporta* del programma ne ha cinque - il KMZ DJI, il piano QGroundControl, il CSV
di Litchi, il GeoPackage e la scheda Excel; gli altri formati sono qui e sulla riga di comando. I formati che contengono una
tratta scrivono **un file per tratta** accanto al nome scelto (o uno zip); i file di un'esportazione precedente con gli
stessi nomi vengono sostituiti, e l'esportazione dice quanti. Vedi [Esportazioni per famiglia di droni](#exports).

**Copia in DJI Fly...** mette le tratte nelle missioni di DJI Fly sul telecomando (vedi [DJI Fly](#djifly)).

### 9. Dopo il volo

Aggiungi al chunk le foto del volo (*File > Aggiungi foto*), poi **Confronta**:

- **Distanza massima**: quanto lontana dallo scatto previsto può essere la foto che lo fa; *automatica* = metà del passo
  della tratta.
- La riga di riepilogo dice quanti scatti hanno la loro foto (**abbinati**), quanti **mancano** (nessuna foto vicina),
  le foto **lontane da ogni scatto** (in più) e quelle **ripetute** (una seconda foto su uno scatto già fatto), lo
  scarto mediano in pianta e, per i droni DJI, la differenza mediana di quota (dal `RelativeAltitude` dell'XMP di DJI;
  gli altri droni non danno la differenza di quota).
- Il layer rosso *Piano di volo: scatti mancanti* mostra gli scatti mancanti; nell'albero dell'area di lavoro il piano
  ha un figlio *Confronto con il volo* (doppio clic: il piano attivo, la copertura ottenuta mostrata, il pannello
  aperto).
- Le posizioni sono i riferimenti delle camere del programma: GPS dell'EXIF, un file di posizioni, PPK. Con RTK/PPK gli
  scarti sono veri; con un GPS semplice (± 5 m) lo scarto mediano è soprattutto l'errore del GPS.
- **Copertura ottenuta**: con le foto **allineate** e un sistema proiettato, il plugin conta quante foto allineate
  vedono ogni cella, sulla griglia della copertura prevista; **Mostra la copertura ottenuta** la mostra. *Celle peggio
  del previsto* = prevista alla soglia o sopra e ottenuta sotto. Le celle fuori dalla ricostruzione **non si misurano**:
  se ne dà il numero, e sulla mappa restano senza valore.
- **Limiti**: anche la copertura ottenuta non ha occlusioni, ed è stata provata solo su dati sintetici, mai ancora su un
  volo vero allineato. Il confronto diventa *Da rifare* quando cambiano il piano o le foto (aggiunte, spostate,
  riallineate, spente), dopo un nuovo allineamento, un DEM nuovo o posizioni nuove.

La scheda del volo (Excel/HTML) ha una pagina *Confronto* finché il confronto è attuale.

## Esportazioni per famiglia di droni { #exports }

**Quote.** Tutti i file per i droni hanno quote **sopra il punto di decollo**, e ogni file lo dichiara a modo suo. Un
piano che segue il terreno tiene **una quota per punto** in tutti i formati per i droni.

| Formato | Per | Riferimento delle quote scritto nel file |
|---|---|---|
| KMZ WPML DJI (uno per tratta, o tutte le tratte in uno zip) | DJI Pilot 2, FlightHub 2, DJI Fly (Mini 4 Pro) | `heightMode` / `executeHeightMode` `relativeToStartPoint`, `coordinateMode` `WGS84`, un `executeHeight` per waypoint |
| DJI Fly: cartelle pronte da trascinare (zip) | DJI Fly sull'RC 2 / RC Pro o su un telefono Android | il KMZ qui sopra, rinominato come la missione che sostituisce |
| QGroundControl `.plan` | droni ArduPilot e PX4, QGroundControl | item frame 3 (`MAV_FRAME_GLOBAL_RELATIVE_ALT`), `AltitudeMode` 1, `globalPlanAltitudeMode` 1 |
| Mission Planner `.waypoints` | ArduPilot, Mission Planner | i comandi del `.plan`, frame 3; riga home al decollo con quota 0 |
| Parrot `.mavlink` | Anafi, Anafi USA, Anafi Ai (FreeFlight 7, GroundSDK) | frame 3 |
| Litchi CSV | droni DJI senza waypoint in DJI Fly (Mini 2, Air 2S, Mavic 2, Phantom 4...) | `altitudemode` 0 (sul decollo) |
| KML del piano | Google Earth, QGIS | `altitudeMode` `absolute` = suolo al decollo + quota sul decollo; la quota del decollo e la sua sorgente scritte nel file (0, con una nota, se non si conosce) |
| GeoJSON del piano | GIS | z = quota sul decollo, `riferimento_quote` = `decollo` |
| GeoPackage del piano | GIS, nel sistema del chunk | `quota_decollo_m` = quota sul decollo; Z = suolo al decollo + quella, nel sistema di quote del DEM usato; una colonna `riferimento_quote` dice quale |
| Aree in KML per le app DJI (sperimentale) | app che fanno da sole le strisciate | solo il contorno di una griglia; i valori da reimpostare sono nella sua descrizione |
| Scheda del volo (Excel, HTML), mappe delle tratte (zip) | chi vola | ogni waypoint con la sua quota sul decollo; una mappa per tratta sopra l'ortofoto del chunk (grigia senza) |

Il drone non legge mai KML, GeoJSON o GeoPackage: servono a guardare il piano e a conservarlo.

**Provato e non provato.** Il KMZ di DJI Fly per il Mini 4 Pro è il file con cui il pianificatore è stato provato sul
drone vero. Gli altri file seguono la specifica di ciascun formato e sono controllati dai test, ma **non sono ancora
stati importati nelle app vere**: WPML in DJI Pilot 2 e FlightHub 2, Litchi, QGroundControl, Mission Planner, Parrot e
il KML delle aree. In particolare la **quota per punto** che QGroundControl, Mission Planner, Parrot e Litchi ricevono
da un piano che segue il terreno non è stata provata in quelle app: l'esportazione lo avvisa. Lo stesso vale per DJI
Pilot 2 e FlightHub 2: il KMZ WPML dei droni Enterprise porta la quota di ogni punto sia nella rotta che si vola
(`waylines.wpml`) sia nel modello che Pilot 2 modifica (`template.kml`), ma **le quote per punto in Pilot 2 non sono
provate**. **Prova ogni file nell'app vera, e guarda lì la rotta, prima di volare.**

Come caricarli (i passi delle app possono cambiare con le loro versioni):

- **DJI Pilot 2** (Mavic 3E/3M/3T, Matrice 30, 4E/4T, 300/350): copia il KMZ della tratta sul telecomando (USB o
  microSD), *Flight Route > Import Route (KMZ/KML)*; controlla modo delle quote e velocità prima di volare.
  **FlightHub 2**: importa il KMZ nella libreria delle rotte e mandalo al dock o al telecomando.
- **DJI Fly** (Mini 4 Pro e i droni con waypoint in DJI Fly): DJI Fly non importa file - vedi [DJI Fly](#djifly).
- **Litchi** (iOS, Android): importa il CSV nel Mission Hub di Litchi (sul web), salva la missione, aprila nell'app.
- **QGroundControl**: nella vista *Plan*, *File > Open*, scegli il `.plan`, poi caricalo sul drone.
- **Mission Planner**: *Plan > Load WP File*, scegli il `.waypoints`, poi *Write WPs*.
- **Parrot**: FreeFlight 7 importa il `.mavlink` dal menu Condividi del telefono o del tablet; GroundSDK lo carica
  direttamente.

## DJI Fly { #djifly }

DJI Fly non importa file: tiene ogni missione come KMZ in una cartella con un codice per nome, in
`Android/data/dji.go.v5/files/waypoint/` sull'RC 2, sull'RC Pro e sui telefoni Android (su iPhone non si raggiunge dal
computer). Il plugin sostituisce quei file con le tratte:

1. In DJI Fly crea **una missione fittizia per tratta** (bastano due waypoint) e salvala.
2. Collega il telecomando al computer. **Copia in DJI Fly...** trova da solo la cartella dei waypoint quando il
   dispositivo è visibile come percorso (Linux, una scheda SD, un percorso che incolli); su Windows raggiunge il
   dispositivo MTP attraverso la shell del sistema (PowerShell).
3. Il plugin riempie prima gli spazi sicuri (missioni fittizie, tratte di prima di questo piano), dice cosa contiene
   ogni spazio, chiede prima di sovrascrivere una tua missione o una tratta di un altro piano, e tiene una copia di ogni file che sostituisce in
   `<dati del chunk>/flightplan/djifly_backup/`.
4. Dove non c'è un percorso (un Mac con l'RC 2), esporta **DJI Fly: cartelle pronte da trascinare (zip)** e trascina le
   cartelle nella cartella dei waypoint con un programma per i file dei dispositivi Android.

In DJI Fly le missioni tengono il nome della missione fittizia (DJI Fly le chiama con l'ora in cui sono state fatte):
il plugin dice quale nome contiene quale tratta. La copia su Windows con MTP e PowerShell **non è ancora stata provata
su un telecomando vero**.

## Riga di comando { #command-line }

```bash
ppg plugin flightplan cameras                                   # le camere dell'elenco
ppg plugin flightplan design scavo.ppg --area area.kml --goal orto --value 2 --terrain
ppg plugin flightplan design scavo.ppg --xy "x,y;x,y;x,y" --camera mini4pro --margin 10 --double
ppg plugin flightplan design scavo.ppg --bbox O,S,E,N --exif foto.jpg --takeoff LAT,LON
ppg plugin flightplan show scavo.ppg                            # i numeri del piano (JSON)
ppg plugin flightplan terrain scavo.ppg --detail fine           # --tiles: tegole pubbliche; --off: di nuovo una quota per blocco
ppg plugin flightplan targets scavo.ppg --csv target.csv --epsg 3003 --metres 10
ppg plugin flightplan targets scavo.ppg --markers               # --estimated, --remove, --tiles (suolo sulle tegole)
ppg plugin flightplan coverage scavo.ppg --cell 0.5 --low 5
ppg plugin flightplan export scavo.ppg --format kmz-zip --out volo.zip
ppg plugin flightplan djifly scavo.ppg --zip pronte.zip --missions UUID,UUID
ppg plugin flightplan compare scavo.ppg --max-distance 3
ppg plugin flightplan list scavo.ppg                            # use ID, remove ID, add --plan piano.json
ppg plugin flightplan design --coords "lat,lon;lat,lon;lat,lon" --plan piano.json   # senza progetto
```

`--goal` è uno fra `orto`, `dem`, `modello3d`, `ispezione`, `ricognizione`, `gsd`; `--camera` un id di `cameras`,
oppure `--exif FOTO`, oppure `--manual NOME,LARGHEZZA_MM,ALTEZZA_MM,FOCALE_MM,PIXEL`; `--mode per_punto|intervallo`;
`--obliques` / `--no-obliques`. `--format` è uno fra `kmz`, `kmz-zip`, `djifly-zip`, `plan`, `waypoints`, `mavlink`,
`litchi`, `kml`, `geojson`, `gpkg`, `area-kml`, `sheet-xlsx`, `sheet-html`, `maps-zip`. `--id` sceglie un piano del
progetto (di base quello attivo); `--plan FILE` lavora invece su un file JSON del piano, per `design`, `show`, `terrain`,
`targets`, `export`, `djifly` (e `add` mette quel file nel progetto); un piano JSON del pianificatore di Photo3D si
carica così com'è. `coverage` rifiuta `--plan` (la copertura resta nel progetto), e `compare`, `list`, `use` e `remove`
lavorano solo sul progetto.

## File

In `<dati del chunk>/flightplan/<id del piano>/`: la copertura (`copertura_count.tif`, `copertura_gsd.tif`,
`copertura_rgba.tif`), il confronto (`confronto_confronto.json`, `confronto_mancanti.gpkg` con la tabella
`scatti_mancanti`, `confronto_copertura_ottenuta.tif` e `_rgba.tif`). I file derivati portano il timbro del piano e si
rifanno quando il piano cambia. Le esportazioni vanno dove scegli; le copie delle missioni di DJI Fly in
`<dati del chunk>/flightplan/djifly_backup/`.

## Limiti

- Nessuna **occlusione** nella copertura, prevista o ottenuta; le celle fuori dal DEM sono piane al decollo.
- Le quote per punto di QGroundControl, Mission Planner, Parrot e Litchi, il WPML in DJI Pilot 2 / FlightHub 2, il KML
  delle aree e la copia in DJI Fly con MTP su Windows **non sono ancora provati** nelle app e sui telecomandi veri:
  provali prima di volare.
- La copertura ottenuta non è ancora stata provata su un volo vero allineato.
- Senza un'ortofoto nel progetto le mappe delle tratte hanno lo **sfondo grigio** (nessuna tegola di mappe di terzi).
- Le **riprese ravvicinate** (una trincea, una parete) non sono nella 0.1.0.
- Spazio aereo, permessi, ostacoli e meteo sono del pilota.
