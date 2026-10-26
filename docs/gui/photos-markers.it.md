# Foto e marker

Tutto ciò che entra nell'allineamento: le foto e le loro maschere, i marker misurati su di esse, le barre di
scala tra i marker.

## Aggiungere le foto

| Comando | Cosa aggiunge |
| --- | --- |
| *Flusso di lavoro > Aggiungi foto...* / *Aggiungi cartella...* | le foto nel chunk attivo; un volo diviso in più cartelle (i rullini di una camera, `100MEDIA` e `101MEDIA`, la cui numerazione ricomincia) si aggiunge cartella dopo cartella o come cartella madre in una volta: i nomi che si ripetono prendono la loro cartella come prefisso, i file già nel chunk restano fuori |
| *Flusso di lavoro > Aggiungi video...* | i fotogrammi scelti quando la vista si è spostata di una parte della sua larghezza (il più nitido attorno a quel momento) o uno ogni pochi secondi, scritti in una cartella accanto al video; con il `.SRT` di un drone DJI accanto al video ogni fotogramma ha la sua posizione GPS |
| *Flusso di lavoro > Aggiungi foto 360°...* | ogni foto equirettangolare diventa le sei facce di un cubo (le facce da tenere - *Sotto* spesso mostra solo il treppiede - la loro ampiezza in gradi e la dimensione), scritte in `facce_360` accanto alle foto; nell'allineamento le facce di una foto restano insieme come un rig |
| *Flusso di lavoro > Ricollega foto (altra cartella)...* | le foto con lo stesso nome in un'altra cartella e nelle sue sottocartelle: una copia su disco esterno di una cartella Dropbox tenuta solo online, una cartella spostata |

I file di una **camera multispettrale** vengono riconosciuti quando si aggiungono le foto: la finestra propone di
aggiungerli come riprese e apre il pannello *Multispettrale*, così le bande non vengono allineate come se fossero
viste diverse (vedi [Multispettrale e termico](multispectral.md)).

Un **obiettivo fisheye** viene riconosciuto dall'EXIF; altrimenti *Modello dell'obiettivo...* (clic destro nel
pannello *Foto*) lo imposta per i sensori delle foto selezionate: normale, fisheye, distorsione forte, nessuna.
Il suggerimento di una faccia di una foto 360° dice da quale foto viene.

!!! tip "Qualità delle immagini"
    Clic destro nel pannello *Foto*: *Qualità delle immagini* dà la nitidezza di ogni foto confrontata con le più
    nitide (nel suggerimento; *sfocata* sotto 0,5), e *Disattiva le foto sfocate* le lascia fuori dal prossimo
    allineamento.

## Posizioni GNSS delle foto

Ogni foto ha una posizione, sa da dove viene e quanto è buona. Il pannello *GNSS* (*Strumenti > GNSS > Pannello
GNSS*, a destra con *Multispettrale* e *Termico*) le mostra e ha i comandi che danno alle foto una posizione
migliore del GPS dell'EXIF.

| Fonte | Cosa dà |
| --- | --- |
| Le foto stesse | Il GPS dell'EXIF, con un'accuratezza di 5, 5, 10 m (nessun dato RTK). Le foto di un drone DJI con RTK portano anche lo stato nell'XMP (`RtkFlag`: 50 fix, 34 float, 16 singola, 0 nessuno) e le deviazioni standard, lette quando le foto vengono aggiunte; quelle di una camera multispettrale le prendono dalle loro bande |
| *Strumenti > GNSS > File MRK di DJI...* | Il `Timestamp.MRK` che il drone scrive sulla scheda: la posizione di ogni scatto, della camera e non dell'antenna (si applica il braccio di leva dello scatto), con la quota ellissoidica, lo stato RTK e le deviazioni standard |
| *Strumenti > GNSS > File di posizioni...* | Un file di testo con una riga per foto, per un drone costruito in casa o posizioni calcolate da un altro programma (una soluzione PPK, una stazione totale): nome, x, y, quota e, se ci sono, `sx`, `sy`, `sz` (deviazioni standard) o `accuracy`, `status`, `yaw`, `pitch`, `roll`; un'intestazione dà i nomi delle colonne in qualsiasi ordine (`name`, `lon`/`lat`/`ellh`, `easting`/`northing`, `std`, `q`...); virgola, punto e virgola, tabulazione o spazi |

**L'MRK si abbina alle foto con il numero dello scatto nel loro nome** (`DJI_0001.JPG` è la riga 1 del file),
così una foto cancellata dalla scheda non sposta mai le posizioni delle altre; per ordine solo quando l'MRK ha
tante righe quante sono le foto della cartella, altrimenti non si fa niente e la finestra dice perché. Ogni coppia
si controlla poi con l'ora che la camera ha scritto nella foto (la sua ora locale, a un fuso orario dall'UTC
dell'MRK): una foto scattata a più di 3 secondi dal suo scatto tiene la posizione che aveva, così le foto di un
altro volo copiate nella stessa cartella, con gli stessi numeri di scatto, restano come sono; quando le ore non
tornano con nessun fuso orario, o la maggior parte delle foto non è all'ora del suo scatto, non si fa niente. Gli
scatti che non hanno trovato una foto sono elencati nel registro. Le foto sono quelle della cartella dell'MRK; se l'MRK è stato copiato altrove la finestra chiede la cartella (`--photos`
da riga di comando). **Il file di posizioni** chiede il suo sistema di coordinate (`EPSG:4979` è longitudine,
latitudine e quota ellissoidica, come nell'MRK; un sistema proiettato come `EPSG:32633`; `LOCAL`) e il tipo di
quota (*ellissoidica*, come la dà il GNSS, o *dell'EXIF*, sul livello del mare); una riga si abbina alla sua foto
con il nome del file, con o senza estensione (`100MEDIA/DJI_0001` quando lo stesso nome sta in due cartelle).
Applicare le posizioni sostituisce quelle che le foto avevano, e farlo due volte non cambia nulla.

**L'accuratezza** di una posizione è il doppio delle sue deviazioni standard dichiarate (quelle di una soluzione
RTK sono ottimistiche), mai sotto il minimo del suo stato: 1 cm per un fix, 20 cm per un float, 1 m per una
posizione singola. Senza dati RTK resta 5, 5, 10 m. In un file di posizioni `accuracy` vale così com'è; una riga
senza accuratezza né deviazioni prende 5, 5, 10 cm, portati al minimo del suo `status` se ce l'ha. Due passi
pesano ogni foto per la sua accuratezza, così un blocco di fix e qualche posizione singola non è trattato come
fatto di pari: *Allinea foto...* tiene il blocco alle posizioni durante l'allineamento incrementale (l'opzione
*Posizioni GPS delle foto nell'allineamento*, attiva quando le foto hanno posizioni; il metodo globale non le usa,
e la scelta automatica torna all'incrementale quando il blocco globale le manca), e *Ottimizza camere* con il
motore *Proprio* quando *Posizioni GNSS delle foto (motore proprio)* è su *usate con la loro accuratezza* o
*usate, con un bias per volo*. Il motore predefinito di *Ottimizza camere*, COLMAP, ignora le posizioni delle
camere, e quell'opzione è su *non usate* finché non la scegli.

**La tabella** ha una riga per volo (una pausa di oltre 20 minuti ne comincia un altro; le foto senza data sono
la riga *senza data*): le foto con una posizione fix, float, singola, con una posizione e nessun dato RTK, senza
alcuna posizione, e quelle il cui riferimento è disattivato; l'accuratezza mediana per asse; il tipo di quota
delle posizioni (*ellissoidica* dal GNSS e dall'MRK, *EXIF*, *sconosciuta* per posizioni lette da un altro
programma); e, quando il chunk è allineato e georeferenziato, quanto le camere sono finite lontano dalle loro
posizioni (quadratica media per asse e distanza maggiore). Un volo i cui scarti sono molto più grandi della sua
accuratezza ha un bias (una base su un punto sbagliato) o posizioni che non gli appartengono. Quando le posizioni
in uso, accurate in quota (sotto il metro), hanno quote di tipo diverso - quelle ellissoidiche di un MRK e quelle
dell'EXIF di altre foto, che possono distare decine di metri - il pannello lo dice sotto le posizioni (e `ppg gnss
status` stampa un avviso): controllale prima di usarle insieme.

**Solo le posizioni fix** disattiva i riferimenti delle foto la cui posizione non è un RTK fissato: restano nel
chunk e si allineano, ma non tirano più le camere. Una posizione senza stato RTK (il GPS dell'EXIF, un file di
posizioni senza la colonna `status`) conta come non fix. Le foto disattivate si ricordano: togliendo la spunta si
riattivano proprio quelle, un riferimento disattivato a mano resta disattivato, e riapplicando le posizioni la
scelta si mantiene.

Da riga di comando:

```bash
ppg gnss status rilievo.ppg                                   # la tabella, per volo
ppg gnss mrk rilievo.ppg /scheda/DCIM/100MEDIA/Timestamp.MRK  # --photos CARTELLA se le foto stanno altrove
ppg gnss positions rilievo.ppg camere.csv --crs EPSG:4979     # --height ellipsoidal|exif
```

Tutti e tre accettano `--chunk` e dicono cosa hanno fatto; `mrk` e `positions` salvano il progetto, e si fermano
con un errore, senza cambiare nulla, quando il file non si può usare.

## Maschere

Le parti mascherate di una foto (in rosso) non si usano per allineare, nelle mappe di profondità, nell'ortofoto
o nella texture. *Strumenti > Maschere delle foto*:

* **disegna** una maschera sulla foto mostrata: clic sui vertici, doppio clic o ++enter++ chiude, ++esc++
  annulla; *Togli la maschera* o ++shift++ + doppio clic restituisce l'area; *Mostra le maschere* nasconde il
  rosso;
* **importale** da immagini, o dal canale alfa delle foto;
* **creale** da foto dello sfondo senza l'oggetto, dal colore di un fondale, dal modello (resta solo l'oggetto)
  o con SAM 2.1 (l'oggetto al centro di ogni foto);
* **esportale**, o toglile (dalle foto selezionate o da tutte).

*Allinea foto* può anche lasciare fuori i punti che non si muovono tra le foto: il turntable con la camera fissa.

## Marker

![Una foto con i suoi marker](../img/gui/view-photo.png)

I marker sono i target e i punti misurati nelle foto; *Flusso di lavoro > Rileva marker codificati...* trova da
solo i target codificati stampati.

**Un marker misurato in una sola foto** sta dove il suo raggio incontra la superficie (la nuvola densa,
altrimenti i punti di legame: un piano locale adattato attorno al raggio, largo circa 20 pixel). Così ha subito
delle coordinate, si vede sul modello e sull'ortofoto e può chiudere una barra di scala; misurato in una seconda
foto viene triangolato. Sul saggio sintetico i marker da una foto stanno entro 0,6 mm dai target rilevati.

**Marker senza foto**, come *Add Marker* di Metashape:

* *Marker* nella vista 3D: la vista si ferma; clic su un punto del modello o della nuvola (il punto mostrato più
  vicino sotto il cursore, affinato lungo la linea di vista sulla nuvola densa completa);
* *Marker* nella barra Disegno: clic sull'ortofoto o sul DEM (la quota dal DEM a piena risoluzione, altrimenti
  dalla nuvola lungo la verticale).

Il nuovo marker (`point 1`, `point 2`...) viene proiettato sulle foto che lo vedono, non nascosto dalla
superficie: bandierine blu, da trascinare in una foto per fissarlo. I marker sono disegnati sull'ortofoto come
layer *Marker*.

### Posizionamento guidato

Come in Metashape, un marker messo su una foto (o in 3D) riceve le **bandierine blu** sulle altre foto che lo
vedono (al massimo 60, prima quelle dove cade più vicino al centro), e il pannello *Foto* viene filtrato su di
esso.

1. Apri una foto con un doppio clic: è centrata sul marker a piena risoluzione.
2. Trascina la bandierina blu sul punto, o fai doppio clic su di essa quando è giusta: diventa verde.
3. ++page-down++ apre la foto successiva.

Ogni conferma posiziona di nuovo il marker (due bandierine verdi: triangolato) e sposta le bandierine blu delle
altre foto dove ora si trova. Le bandierine blu sono stime, non misure: non spostano mai un marker che ne ha una
verde, né entrano nell'ottimizzazione delle camere.

*Affina le bandierine blu* (clic destro in *Riferimenti*, per i marker selezionati o tutti, oppure *Flusso di
lavoro > Affina le bandierine blu dei marker*) cerca il punto di ogni bandierina blu nella sua foto per
correlazione d'immagine, sulla superficie attorno al marker: le bandierine trovate diventano misure
(automatiche, come i target rilevati), le altre restano blu da confermare a mano.

In *Riferimenti* il clic destro anche **unisce** i marker selezionati (un target codificato e il GCP messo a
mano sopra) o li **elimina**, e *Sposta nella vista 3D* riposiziona un marker con un clic sul modello o sulla
nuvola (per i marker messi in 3D o sull'ortofoto; quelli misurati nelle foto si spostano nelle foto).

## Barre di scala

Una barra di scala è la distanza nota tra due marker, come in Metashape.

1. In *Riferimenti* seleziona due marker (++ctrl++ o ++shift++ + clic) e scegli *Crea barra di scala tra A e
   B...* dal clic destro, oppure premi *Barra di scala...* (i marker selezionati vengono per primi nella
   finestra).
2. La finestra chiede la distanza in metri (tra i centri dei target), la sua accuratezza (pesa la barra quando
   ce ne sono più), se è una barra di check, e se aggiornare subito la scala.

| Tipo di barra | Cosa fa |
| --- | --- |
| **Barra di controllo** | dà la scala al chunk: la scala è la soluzione ai minimi quadrati di *scala x lunghezza nel modello = distanza*, ogni barra pesata con `1 / accuratezza²` (di due barre misurate ugualmente bene, conta di più la più lunga). Senza punti di controllo il chunk viene scalato in coordinate locali (CRS `LOCAL`): un oggetto, un reperto, un muro, un saggio |
| **Barra di check** (*Check* spuntato) | non dà la scala: il suo errore dice quanto è buona la scala |

!!! note "La distanza di una barra è in 3D"
    Con un dislivello è più della distanza in pianta misurata sull'ortofoto.

**Chunk georeferenziati**: una nuova barra viene proposta come check (misura la scala senza cambiarla). Una barra
di controllo su un chunk georeferenziato altrove (un progetto importato) senza riferimenti per rifarne
l'adattamento lo scala attorno alla barra, nel suo CRS; quando la scala cambia, il log dice che DEM e ortofoto
sono ancora nella vecchia e vanno ricostruiti.

**Foto con GPS**: quando le loro posizioni coprono meno di dieci volte la loro accuratezza (un telefono o un drone
sopra un oggetto o un saggio) restano fuori e le barre scalano il chunk in coordinate locali. Su un'area più
grande (un volo su un sito), o con tre o più punti di controllo, posizioni delle foto, punti di controllo e barre
vengono adattati insieme, ognuno pesato con `1 / accuratezza²`: posizioni e punti collocano e orientano il
rilievo, la scala viene da tutti.

**La tabella delle barre**, sotto i marker in *Riferimenti*, ha una riga per barra: *Usa* (togli la spunta per
lasciarla fuori), nome, marker, *Distanza* e *Accur.* (doppio clic per cambiarle: l'errore segue subito),
*Check*, la distanza misurata sul modello (*Stimata*) e l'errore, in rosso quando supera tre volte l'accuratezza
(almeno 5 mm). Il clic destro elimina le barre. Il riepilogo mostra *RMSE barre* (controllo) e *RMSE barre
check*, e come è stato collocato il chunk: scala dalle barre, coordinate locali, o scala da riferimenti e barre
insieme.

La vista 3D disegna le barre tra i loro marker, quelle di controllo in ciano e quelle di check tratteggiate in
arancio, con la loro lunghezza e l'errore; l'area di lavoro le elenca sotto il chunk.

### Importare e stampare le barre

* *Importa barre...* (oppure *Flusso di lavoro > Importa barre di scala (CSV)*): un file di testo con
  `marker_a, marker_b, distance[, accuracy[, check]]` per riga, separati da virgole, punti e virgola,
  tabulazioni o spazi; i marker per nome (`target 12`) o codice del target (`12`); una riga di intestazione viene
  saltata e le coppie i cui marker non sono nel chunk sono elencate nel log.
* *Strumenti > Target > Barra di scala stampabile...*: una pagina per barra con due target codificati a distanza
  nota (gli ultimi codici del dizionario, 48-49 per `aruco_4x4_50`, così non si confondono con i fogli di target
  e i tappetini), un righello di 10 cm per controllare che la stampa sia al 100 %, e il CSV delle barre accanto
  al PDF. Incolla la pagina su qualcosa di rigido, fotografala con l'oggetto, rileva i marker e importa il CSV.
* I progetti importati da Metashape portano le loro barre di scala (distanza, accuratezza, barre di check); i
  task di WebODM portano le pose delle camere e i loro GCP come marker, così si possono tracciare barre tra di
  essi.

### Le barre nell'ottimizzazione

*Ottimizza camere* usa anche le barre, come Metashape: con due o più barre di controllo, o con punti di
controllo, i marker agli estremi delle barre vengono fissati dove le barre hanno la loro lunghezza (ognuno
spostato quanto poco permette la sua precisione nelle foto, `MARKER_PIXEL_ACCURACY` 0,5 px) e il blocco si piega
verso di essi quanto permettono i punti di legame; una sola barra dà solo la scala. Il log riporta gli errori
delle barre prima e dopo.

??? info "Con il motore *Proprio*"
    Le barre sono distanze tra i loro marker dentro l'aggiustamento stesso, e i punti di controllo sono tirati
    verso le loro coordinate secondo la loro accuratezza invece di essere fissati. Possono partecipare le
    posizioni GNSS delle foto (con uno scostamento per volo stimato insieme al blocco: una base RTK su un punto
    sbagliato, un offset dell'antenna) e il rolling shutter dei droni (una velocità di ogni foto mentre il sensore
    viene letto). Su un terreno piano con sole foto nadirali uno scostamento verticale si scambia con la focale:
    servono GCP o foto oblique. Il log riporta lo scostamento di ogni volo e la velocità mediana.
