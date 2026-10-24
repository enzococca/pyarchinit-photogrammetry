# Geofisica

Il plugin *Geofisica* porta le griglie di una prospezione geofisica - magnetometria, resistività, le
slice in tempo o in profondità di un GPR, prospezioni elettromagnetiche e di suscettività - nel
progetto fotogrammetrico dello stesso terreno. Legge le griglie esportate dal programma di
elaborazione, le posa sul rilievo con i picchetti della griglia di prospezione, applica i filtri soliti
(despike, destripe, passa-alto...), le sovrappone all'ortofoto o al DEM con la trasparenza che scegli,
ti fa disegnare le anomalie che vi leggi e le esporta per un GIS. Anomalia e scavo, mappa magnetica e ortofoto finiscono nello stesso posto e
nello stesso sistema di riferimento.

Questa pagina descrive la versione 0.1.2 del plugin, che richiede pyarchinit-photogrammetry 1.3.3 o
successiva. Il README e il codice sono nel
[repository dei plugin](https://github.com/enzococca/pyarchinit-photogrammetry-plugins/tree/main/plugins/geophysics).

<figure markdown="span">
  ![Il pannello Geofisica e una griglia magnetometrica sopra il DEM](../img/plugins/geophysics/pane.png)
  <figcaption>Il pannello con le griglie di una prospezione magnetometrica - la griglia grezza, le sue
  copie filtrate e una seconda area letta da una tabella XYZ - e l'ultima filtrata sovrapposta al DEM
  della vista, in grigi al 75 % di opacità: un'anomalia lineare positiva e un dipolo, contornato come
  anomalia A1. Dati sintetici: la griglia dei test del plugin sopra un DEM inventato.</figcaption>
</figure>

!!! note "Gli esempi di questa pagina"
    Le griglie, il DEM e le figure di questa pagina sono sintetici: la prospezione magnetometrica con
    cui il plugin viene provato (un muro, una fossa, strisce fra le traverse e tre picchi di ferro),
    posta in un fuso UTM con coordinate inventate.

## In breve

1. Esporta ogni griglia dal tuo programma di elaborazione come numeri - Surfer GRD, XYZ o GeoTIFF -
   non come immagine.
2. Nel pannello *Geofisica*: *Importa griglia...*, con il tipo di prospezione e le unità.
3. *Georeferenzia...*: con i marker posati sui picchetti degli angoli nell'ortofoto, con origine e
   azimut della griglia, oppure con le coordinate dei picchetti misurate sul campo.
4. *Filtri...*: destripe, despike, passa-alto, ognuno in una copia.
5. *Sovrapponi alla vista* sopra l'ortofoto o il DEM, con banda, palette e opacità.
6. *Disegna anomalia*: i poligoni di ciò che leggi sulla griglia, con una nota.
7. *Esporta...* in GeoTIFF o Surfer GRD, *File > Esporta* in GeoPackage, oppure *Apri in QGIS*.

## Che cosa fa

| Passo | Che cosa dà | A che cosa serve |
|---|---|---|
| Importazione | ogni griglia come GeoTIFF di valori in virgola mobile dentro il progetto, una banda per slice di un GPR | tenere la geofisica con il rilievo e lo scavo a cui appartiene |
| Georeferenziazione | la griglia posata nel sistema di riferimento del chunk, ruotata e spostata, con il residuo dei picchetti | portare sulla carta una prospezione fatta con fettucce e picchetti, in metri locali |
| Filtri | despike, destripe, clip, passa-basso, passa-alto, riempimento, ricampionamento, in copie, ognuno registrato | pulire una griglia grezza: picchi di ferro, strisce di un magnetometro portato avanti e indietro, il fondo geologico |
| Sovrapposizione | la griglia sopra l'ortofoto o il DEM, con banda, palette e opacità | leggere le anomalie insieme a muri, trincee, confini dei campi e rilievo |
| Anomalie | poligoni disegnati sulla vista, ognuno con la griglia, la banda e una nota, conservati nel progetto | l'interpretazione, accanto al rilievo e allo scavo |
| Esportazioni | GeoTIFF e Surfer GRD di una griglia, un GeoPackage con i contorni delle griglie e le anomalie, le griglie nel progetto QGIS del programma | l'archivio, il GIS, la relazione |

Non è un programma di elaborazione: non riduce i dati grezzi registrati dallo strumento, non unisce le
griglie di una prospezione in un mosaico e non interpreta: le anomalie le disegni tu. Elabora la prospezione nel suo programma,
esporta le griglie, poi usa il plugin per metterle sul terreno.

## Che cosa serve prima { #before }

* **Il plugin**, installato dal programma: vedi [Guide ai plugin](guides.md#install). Non serve
  nient'altro.
* **Un progetto il cui chunk ha un sistema di riferimento proiettato, in metri** - per esempio il fuso
  UTM del sito. Una griglia in metri locali si può importare in qualsiasi progetto, ma si può
  georeferenziare solo in un chunk con un sistema di riferimento: in un progetto locale il plugin
  rifiuta.
* **Un'ortofoto o un DEM** del chunk su cui vedere le griglie; senza, una griglia georeferenziata viene
  mostrata da sola, in un riquadro della sua misura.
* **Le griglie** come file: la sezione che segue.
* Per la georeferenziazione, una di queste: i **picchetti degli angoli** visibili nell'ortofoto (un
  target o un segno dipinto su ognuno), le **coordinate dei picchetti** misurate con il GNSS o la
  stazione totale nel sistema di riferimento del chunk, oppure **origine e azimut** della griglia.

## I dati { #data }

### Formati { #formats }

| Formato | File | Che cosa legge il plugin |
|---|---|---|
| Griglia Surfer | `.grd` | ASCII (`DSAA`), binario di Surfer 6 (`DSBB`) e di Surfer 7 (`DSRB`); il valore di vuoto di Surfer è assenza di valore; celle più lunghe su un lato diventano quadrate sul lato più corto |
| Tabella XYZ | `.xyz`, `.csv`, `.txt`, `.dat` | una lettura per riga, x, y e valore nelle prime tre colonne; i valori fittizi non sono letture |
| Raster | `.tif`, `.tiff`, `.asc`, `.img` e ogni formato raster che GDAL legge | tutte le sue bande; il suo valore di nodata è assenza di valore; il suo sistema di riferimento se ce l'ha |
| Cartella di slice | una cartella di `.tif`, `.tiff`, `.png`, `.jpg`, `.asc` o `.grd` | le slice di un GPR: una banda per file, nell'ordine naturale dei nomi |

Esporta numeri, non immagini: un PNG o un JPEG fatti per una relazione contengono livelli di grigio o
colori, non nT o ohm metro; un'immagine a colori diventa tre bande - rosso, verde e blu - e in una
cartella di slice se ne legge solo il canale rosso.

### Tabelle XYZ { #xyz }

Le esportazioni di Geoplot, TerraSurveyor, del programma di Bartington e della maggior parte degli
altri si leggono così come sono:

```text
x y nT
0.0625 0.2500 0.412
0.1875 0.2500 0.367
0.3125 0.2500 -0.051
```

* x, y e il valore sono le prime tre colonne; le altre vengono ignorate. Le righe che non sono numeri -
  un'intestazione, un commento - vengono saltate.
* Le colonne possono essere separate da spazi, tabulazioni, virgole o punti e virgola. Le **virgole
  decimali** vengono lette quando le colonne sono separate da punti e virgola, tabulazioni o spazi
  (`0,125;0,250;3,7` oppure `0,125 0,250 3,7`); con le virgole fra le colonne, i decimali vanno scritti con
  il punto.
* I **valori fittizi** non sono letture: 2047.5 (il valore fittizio di Geoplot di Geoscan), -9999, 32767,
  -32768 e il vuoto di Surfer (1.70141e38). *Valore fittizio per XYZ* nella finestra di importazione
  (`--dummy` dalla riga di comando) aggiunge quello che scrive il tuo programma. L'importazione dice
  quanti ne ha tolti.
* **La cella.** Il plugin trova la distanza fra le letture lungo x e lungo y. Quando sono uguali, quella è
  la cella e ogni cella ha una lettura. Quando le letture lungo le traverse sono più fitte delle traverse
  - ogni 12,5 cm su linee distanti 50 cm - la cella è la distanza più fine e le righe fra due traverse
  vengono riempite per interpolazione lineare fra le due traverse accanto: mai oltre la prima e l'ultima
  traversa, mai attraverso una lettura mancante (lì le celle restano vuote). La finestra di importazione e
  la riga sotto la tabella del pannello lo dicono (*righe interpolate fra traverse a 0.5 m (letture ogni
  0.125 m)*).
* *Cella per XYZ* dà invece una cella tua, di solito la distanza fra le traverse (0,5 m): le letture che
  cadono in una cella vengono mediate, una traversa per riga di celle.
* **Le celle sono centrate sulle letture**: la griglia comincia mezza distanza prima della prima lettura.
  Con letture ogni 0,125 m da 0,0625 m e una cella di 0,5 m, la griglia comincia a 0 e ogni cella contiene
  quattro letture: traverse lunghe 30 m danno 60 celle, e gli angoli della griglia sono i picchetti.
* Con la cella fine e l'interpolazione, la griglia va dalla prima all'ultima traversa: di traverso alle
  traverse è più corta della prospezione della distanza fra le traverse meno una cella (29,625 m per 30 m
  di traverse distanti 0,5 m, celle di 0,125 m), quindi i suoi angoli non sono i picchetti. Posa una
  griglia così con coppie digitate misurate dal suo angolo in basso a sinistra, oppure importala con la
  distanza fra le traverse come cella quando gli angoli devono essere i picchetti.

### Dove sta la griglia { #coordinates }

Una griglia il cui file non ha un sistema di riferimento - una griglia Surfer, una tabella XYZ,
un'immagine - è in metri locali: il plugin mette l'**angolo in basso a sinistra in 0, 0**, con la x che
cresce lungo le righe della griglia come stanno nel file (verso destra) e la y verso l'alto, e ne
conserva le dimensioni. Le coordinate scritte nel file non vengono usate: gli angoli della griglia sono
0, 0 / L, 0 / L, H / 0, H, dove L e H sono larghezza e altezza in metri (la finestra di
georeferenziazione le mostra). Gli angoli sono i bordi esterni delle celle, mezza cella oltre la prima e
l'ultima lettura, cioè dove stanno di solito i picchetti di una griglia di prospezione.

Un raster con un sistema di riferimento - un GeoTIFF esportato già georeferenziato - lo tiene e non va
georeferenziato; deve stare nello **stesso sistema di riferimento del chunk**, perché il plugin non
riproietta (riproiettalo prima in QGIS). Un'immagine senza world file ha 1 m per pixel: posala con i
marker o con le coppie digitate e la scala libera.

### Tipi e unità { #kinds }

Il tipo scelto all'importazione decide le unità, se non ne dai, e come la griglia viene mostrata:

| Tipo | Unità | Palette nella vista | Limiti dei colori | Scala di colori in QGIS |
|---|---|---|---|---|
| *Magnetometria* | nT | grigi | mediana ± 2,5 deviazioni standard robuste | blu-rosso |
| *Resistività* | ohm m | terra | 2° e 98° percentile | grigi |
| *GPR* | - | grigi | mediana ± 2,5 deviazioni standard robuste | grigi |
| *Elettromagnetico* | mS/m | terra | mediana ± 2,5 deviazioni standard robuste | blu-rosso |
| *Suscettività* | SI | calore | 2° e 98° percentile | grigi |
| *Altro* | - | grigi | 2° e 98° percentile | grigi |

La deviazione standard robusta è 1,4826 volte la deviazione mediana assoluta dalla mediana: qualche
picco di ferro non allarga i colori.

### Slice GPR { #slices }

Una cartella di slice diventa una griglia con una banda per file, ogni banda con il nome del suo file
come etichetta (`slice_045cm.tif` dà l'etichetta `slice_045cm`).

* I file vengono presi nell'ordine naturale dei nomi, con i numeri contati come numeri: `slice_2`
  prima di `slice_10`.
* Tutte le slice vengono portate alla misura della prima; dai loro la stessa griglia.
* Di ogni file si legge la prima banda: esporta le slice come valori, o come livelli di grigio.
* Come tipo scegli *GPR*.

### Sul campo { #field }

* Misura i picchetti degli angoli della griglia di prospezione con il GNSS o la stazione totale, nel
  sistema di riferimento del rilievo fotogrammetrico, e annota quale angolo è quale.
* Oppure rendili visibili dall'alto: un target, una croce dipinta o una piastra su ogni picchetto
  d'angolo, dentro l'area del volo del drone.
* Annota quale angolo è l'origine della prospezione e la direzione della prima traversa: il plugin
  prende l'angolo in basso a sinistra della griglia esportata come 0, 0 e le righe come asse x.
* Esporta ogni area della prospezione come una griglia a sé, in metri, con le celle della prospezione.

### Errori frequenti e avvisi { #mistakes }

| Che cosa è successo | Che cosa fa o dice il plugin | Che cosa fare |
|---|---|---|
| virgole decimali con le virgole anche fra le colonne di una tabella XYZ | le virgole spezzano i numeri: una griglia con valori senza senso | punti e virgola, tabulazioni o spazi fra le colonne |
| un valore fittizio diverso dai soliti | letto come una lettura: macchie bianche o nere | *Valore fittizio per XYZ*, oppure togli quelle righe |
| un'immagine a colori di una slice | se ne legge il canale rosso | esporta i valori, o i grigi |
| un'immagine senza world file | 1 m per pixel | marker o coppie digitate, *Scala fissa 1* non spuntata |
| un GeoTIFF in un altro sistema di riferimento | la griglia finisce nel posto sbagliato | riproiettalo nel sistema del chunk |
| georeferenziazione in un chunk senza sistema di riferimento | *Il chunk non è georeferenziato: la griglia resterebbe in metri locali.* | georeferenzia prima il rilievo |
| *Sovrapponi alla vista* con una griglia locale | *La griglia è in metri locali: georeferenziala prima.* | *Georeferenzia...* |
| *Sovrapponi alla vista* con una griglia lontana dal raster mostrato | *La griglia sta fuori dal raster mostrato nella vista...* | controlla la posa, o mostra un altro raster |
| un marker sull'angolo sbagliato, o angoli scambiati | un residuo di metri | controlla quale marker sta su quale angolo e posa di nuovo la griglia |
| virgole decimali nelle coppie digitate | i numeri vengono spezzati | punti decimali |

## Passo per passo nel programma { #program }

### Il pannello

Una volta installato il plugin e riavviato il programma, la scheda *Geofisica* compare nel dock
*Progetto* a sinistra; se le schede non ci stanno, usa le frecce in fondo alla barra delle schede,
oppure *Plugin > Geofisica > Griglie geofisiche...*. Il menu *Plugin > Geofisica* ha anche *Importa
griglia geofisica...*, *Sovrapponi la griglia alla vista*, *Nascondi la griglia* e *Disegna un'anomalia*.

Dall'alto: una breve descrizione; *Importa griglia...*, *Georeferenzia...*, *Filtri...*; la tabella
delle griglie del chunk; *Banda* (per le slice di un GPR, con l'etichetta della slice), *Palette*,
*Opacità*; *Sovrapponi alla vista*, *Nascondi*, *Disegna anomalia*, *Esporta...*, *Apri in QGIS*,
*Rimuovi*; in fondo, una riga sulla griglia selezionata (dimensioni, cella, posizione, mediana, filtri e
come è stata letta una tabella XYZ), oppure che cosa è mostrato e i valori ai due estremi dei colori.

La tabella ha una riga per griglia: *Nome*, *Tipo*, *Unità*, *Cella (m)*, *Celle* (colonne x righe),
*Bande*, *Posizione* (*locale*, oppure il sistema di riferimento una volta posata) e *Filtri* (quelli
applicati, in ordine). I pulsanti lavorano sulla riga selezionata.

### 1. Importare una griglia { #import }

*Importa griglia...*

| Campo | Che cosa fa |
|---|---|
| *File (GRD, XYZ, CSV, GeoTIFF, raster)* | il file della griglia |
| *Oppure una cartella di slice GPR* | una cartella con un file per slice, al posto del file |
| *Nome (vuoto = nome del file)* | il nome nella tabella e nelle esportazioni; le copie filtrate vi aggiungono il nome del filtro |
| *Tipo* | *Magnetometria*, *Resistività*, *GPR*, *Elettromagnetico*, *Suscettività*, *Altro* |
| *Unità (vuote = quelle del tipo)* | nT, ohm m, mS/m, SI... |
| *Cella per XYZ (m, vuota = dai dati)* | solo per le tabelle XYZ: la cella della griglia; vuota, la distanza fra le letture, con le righe fra le traverse interpolate ([Tabelle XYZ](#xyz)) |
| *Valore fittizio per XYZ (vuoto = i soliti)* | solo per le tabelle XYZ: un valore che il tuo programma scrive per una lettura mancante, oltre a 2047.5, -9999, 32767, -32768 e al vuoto di Surfer |

La griglia viene copiata nel progetto come GeoTIFF; una finestra la riassume - *Griglia Area 1: 60 x 80
celle di 0.5 m, 1 banda/e, valori -3.51 .. 59.8 nT; in metri locali: georeferenziala*, con come è stata
letta una tabella XYZ (interpolazione, valori fittizi tolti, virgole decimali). Minimo e massimo
sono il primo controllo: un valore di migliaia in una prospezione magnetometrica è un valore fittizio o
un picco.

### 2. Georeferenziare { #georef }

*Georeferenzia...* posa sul rilievo la griglia selezionata. Serve un chunk con un sistema di
riferimento.

<figure markdown="span">
  ![Il sistema locale di una griglia e l'azimut del suo asse x](../img/plugins/geophysics/georef.png)
  <figcaption>La griglia nel plugin: l'angolo in basso a sinistra in 0, 0, la x lungo le righe della
  griglia come stanno nel file, la y verso l'alto; L e H larghezza e altezza. L'azimut dell'asse x si
  conta in senso orario dal nord: 90 quando le righe vanno verso est.</figcaption>
</figure>

| Campo | Che cosa fa |
|---|---|
| *Metodo* | *Angoli della griglia = marker*, *Origine e azimut* oppure *Coppie di coordinate digitate* |
| *Marker dell'angolo in basso a sinistra (origine)*, *... in basso a destra*, *... in alto a destra*, *... in alto a sinistra* | i marker del chunk sugli angoli: almeno due, meglio tre o quattro; *(nessuno)* per gli altri |
| *Scala fissa 1 (griglia in metri)* | spuntata: la griglia tiene le sue dimensioni, viene solo ruotata e spostata; non spuntata: vengono adattate anche le dimensioni (un'immagine, una griglia con la cella sbagliata) |
| *Origine E (m)*, *Origine N (m)* | l'angolo in basso a sinistra nel sistema di riferimento del chunk |
| *Azimut dell'asse x (gradi dal nord)* | la direzione delle righe, in senso orario dal nord come su una bussola: 90 est, 0 nord |
| *Coppie: 'x_locale y_locale E N' per riga* | un punto per riga: i suoi metri locali nella griglia e le sue coordinate nel sistema di riferimento del chunk |

La finestra dice quanto è grande la griglia (*La griglia è in metri locali con l'origine nell'angolo in
basso a sinistra (30 x 40 m)*); i campi dei metodi non scelti vengono ignorati.

* **Angoli della griglia = marker.** Mostra l'ortofoto, prendi lo strumento *Marker* della barra
  *Disegno* e fai clic su ogni picchetto d'angolo: i marker si chiamano *point 1*, *point 2*...
  (rinominali nel pannello *Riferimenti* se vuoi). Nella finestra scegli il marker di ogni angolo. Un
  marker con coordinate digitate nel pannello *Riferimenti* dà quelle coordinate, gli altri la posizione
  stimata dal rilievo.
* **Origine e azimut.** L'angolo in basso a sinistra e la direzione delle righe: basta quando la
  griglia è stata tracciata con la bussola o lungo una linea nota.
* **Coppie digitate.** Il modo migliore quando i picchetti sono stati misurati: una riga per picchetto,
  i metri locali del picchetto nella griglia e le sue coordinate, per esempio
  `0 0 500100.00 4500800.00`, `30 0 500129.89 4500802.61` e `0 40 500096.53 4500839.86`. I metri
  locali si misurano dall'angolo in basso a sinistra della griglia come la vede il plugin, non nelle
  coordinate del file originale. Spazi, virgole o punti e virgola separano i numeri; decimali con il
  punto.

Con i marker o le coppie il plugin trova la rotazione e lo spostamento (e la scala, se libera) che
portano i punti locali più vicini alle loro coordinate, ai minimi quadrati. La finestra dà il risultato
- con le tre coppie sopra, *Griglia Area 1 posata: rotazione 4.98 gradi, scala 1.0000, residuo 0.005 m*.
La rotazione si conta in senso antiorario da est (un azimut di 90 è una rotazione di 0); il residuo è
lo scarto quadratico medio fra i punti e dove li mette la griglia posata: qualche centimetro è normale,
decimetri o metri vogliono dire un picchetto nel posto sbagliato o due angoli scambiati. Salva il
progetto (*File > Salva*) per tenere la posizione.

Una griglia si può posare di nuovo in ogni momento, con uno qualsiasi dei tre metodi: il plugin parte
ogni volta dai metri locali della griglia, così la nuova posa sostituisce la vecchia (una griglia posata
con la scala libera riprende prima anche le sue dimensioni).

Georeferenzia una griglia prima di filtrarla: le copie filtrate tengono la posizione della griglia da
cui vengono, mentre le copie di una griglia locale restano locali e vanno posate una per una.

### 3. Filtri { #filters }

*Filtri...* applica un filtro a tutte le bande della griglia selezionata.

| Campo | Predefinito | Che cosa fa |
|---|---|---|
| *Filtro* | *Destripe* | il filtro, dalla tabella qui sotto |
| *Despike: lato della finestra (celle)* | 3 | |
| *Despike: soglia (volte il rumore)* | 4 | |
| *Destripe: traverse lungo* | *le righe (x)* | *le righe (x)* o *le colonne (y)*: la direzione in cui è stato portato lo strumento |
| *Destripe: statistica* | *mediana* | *mediana* o *media* di ogni traversa |
| *Clip: percentile basso*, *Clip: percentile alto* | 1, 99 | |
| *Passa-basso: sigma (celle)* | 1 | la larghezza del lisciamento gaussiano |
| *Passa-alto: sigma (celle)* | 10 | la larghezza del lisciamento gaussiano che viene tolto (5 m su celle di 0,5 m) |
| *Riempi: distanza massima (celle)* | 3 | |
| *Ricampiona: cella (m)* | quella della griglia | |
| *In una copia (la griglia originale resta)* | spuntato | il risultato è una nuova griglia, chiamata con il nome del filtro dopo quello della griglia; non spuntato, cambia la griglia stessa |

Contano solo i campi del filtro scelto.

| Filtro | Che cosa fa |
|---|---|
| *Despike (spike sostituiti dalla mediana locale)* | una lettura più lontana dalla mediana della sua finestra della soglia moltiplicata per il rumore della griglia (1,4826 volte la distanza mediana delle letture dalle loro mediane locali) viene sostituita da quella mediana: ferro, una lettura presa mentre lo strumento urtava qualcosa |
| *Destripe (media/mediana di ogni traversa a zero)* | la mediana (o la media) di ogni riga - o colonna - viene sottratta, così tutte le traverse hanno lo stesso zero: le strisce di un magnetometro portato avanti e indietro, di un resistivimetro che deriva |
| *Clip ai percentili* | i valori sotto il percentile basso e sopra quello alto vengono portati a quei valori |
| *Passa-basso (gaussiano)* | un lisciamento gaussiano che salta le celle vuote: meno rumore, bordi più morbidi |
| *Passa-alto (residuo dal gaussiano)* | la griglia meno il suo lisciamento gaussiano: se ne va il fondo geologico ampio, restano le anomalie più piccole di qualche sigma |
| *Riempi i vuoti* | le celle vuote prendono un valore dalle letture entro la distanza, pesate con la distanza |
| *Ricampiona* | celle di un'altra misura sulla stessa area: medie se più grandi, bilineare se più piccole |

<figure markdown="span">
  ![Una griglia magnetometrica attraverso destripe, despike e passa-alto](../img/plugins/geophysics/filters.png)
  <figcaption>(a) La griglia grezza: un'anomalia lineare positiva, un dipolo, strisce fra le traverse e
  tre picchi di ferro. (b) Destripe sulle righe. (c) Despike, 3 celle, soglia 4. (d) Passa-alto, sigma 8
  celle (4 m). La stessa scala di grigi da -3 a +5 nT per i quattro riquadri. Dati
  sintetici.</figcaption>
</figure>

Una sequenza abituale per la magnetometria è destripe, poi despike, poi - quando la geologia è forte -
un passa-alto, ognuno in una copia così i passi restano nella tabella. Sui dati con strisce forti fai
prima il destripe: altrimenti la finestra del despike mescola letture di traverse vicine. Ogni filtro
viene registrato nella griglia (la colonna *Filtri*, le esportazioni); i filtri girano in background e
alla fine il progetto viene salvato.

### 4. Sulla vista { #overlay }

Seleziona una griglia e premi *Sovrapponi alla vista* (o *Plugin > Geofisica > Sovrapponi la griglia
alla vista*). La griglia va sopra ciò che mostra la vista *Ortofoto / DEM*; quando non mostra nulla, prima
viene mostrata l'ortofoto (o il DEM) del chunk, e quando il chunk non ha né l'una né l'altro, la griglia da
sola in un riquadro della sua misura:

* *Banda*: la slice di un GPR, con la sua etichetta accanto al cursore;
* *Palette*: *Grigi*, *Grigi invertiti*, *Divergente (blu-rosso)*, *Terra*, *Calore*; segue il tipo
  della griglia selezionata fino al primo *Sovrapponi alla vista*, poi resta come la imposti;
* *Opacità*: cambia subito la griglia mostrata; banda e palette valgono dal successivo *Sovrapponi alla
  vista*.

La riga in fondo dice che cosa è mostrato e i valori ai due estremi dei colori, nelle unità della
griglia (*Area 1 despike highpass: banda 1, colori da -0.941 a 0.657 nT*). Si mostra una griglia alla
volta; *Nascondi* la toglie. Quando la vista mostra un altro raster (l'ortofoto al posto del DEM, un
rilievo), la griglia vi viene sovrapposta di nuovo con la stessa banda, palette e opacità, fino a
*Nascondi*; disegnare nella vista, o cancellare un disegno, la lascia dov'è.

### 5. Anomalie { #anomalies }

*Disegna anomalia* (o *Plugin > Geofisica > Disegna un'anomalia*) disegna sulla vista ciò che leggi sulla
griglia selezionata:

1. Seleziona la griglia e, se vuoi, sovrapponila con la banda che stai leggendo (una vista vuota mostra
   prima la griglia).
2. Premi *Disegna anomalia* e trascina un riquadro sulla vista, oppure fai clic sui vertici del poligono;
   Backspace toglie l'ultimo vertice, il tasto destro ricomincia il poligono. Il disegno è quello del
   programma, prestato al plugin.
3. Doppio clic, oppure Invio, per chiuderlo; Esc annulla, e così la scelta di un altro strumento della
   barra *Disegno*.
4. Una finestra chiede un'*Etichetta* (A1, A2... in automatico) e una *Nota*, l'interpretazione (*muro*,
   *fossa*, *fornace?*).

L'anomalia tiene il nome della griglia, la banda mostrata (l'etichetta della slice di un GPR) e la sua
area. Va nel progetto (salvalo con *File > Salva*) e nel layer *Anomalie geofisiche* della vista, in
arancione: con lo strumento *Sposta* un clic ne mostra gli attributi, con lo strumento *Seleziona* e Canc
esce dalla vista e dal progetto. Il layer torna ogni volta che la vista mostra un altro raster; toglierlo
dalla vista nel pannello *Layer* lascia le anomalie nel progetto. Lo strumento poligono del programma
disegna unità stratigrafiche, non anomalie.

*File > Esporta > Griglie e anomalie geofisiche (GeoPackage)...* scrive le anomalie nella tabella
`anomalie`. Con il collegamento dal vivo del programma con QGIS compaiono anche fra i disegni del
programma, nella tabella *Poligoni (ppg)* (dal programma 1.3.1; prima, nella tabella delle unità), con
*Anomalie geofisiche* nella colonna `layer`.

### 6. Esportare, QGIS, togliere { #export }

* *Esporta...*: la griglia selezionata come GeoTIFF (tutte le bande, nella sua posizione, rotazione
  compresa) o come Surfer GRD (la banda mostrata nel pannello). Una griglia Surfer non ha rotazione: una
  griglia ruotata viene ricampionata su una griglia con il nord in alto e la stessa cella che la
  contiene, vuota fuori dalla griglia, e la finestra lo dice; il GeoTIFF tiene la griglia com'è.
* *File > Esporta > Griglie e anomalie geofisiche (GeoPackage)...*: un GeoPackage con i contorni delle
  griglie georeferenziate (tabella `griglie`: nome, tipo, unità, cella, righe, colonne, bande, filtri,
  file), un GeoTIFF di ciascuna accanto e le anomalie disegnate (tabella `anomalie`: etichetta, griglia,
  banda, nota, area in m²). Le altre tabelle di un GeoPackage esistente restano.
* *Apri in QGIS*: il progetto QGIS del programma ([QGIS](../integrations.md)) con le griglie
  georeferenziate, nascoste all'inizio, magnetometria ed EM in blu-rosso, le altre in grigi.
* *Rimuovi*: dopo una conferma, la griglia lascia il progetto e il suo file viene cancellato; le
  anomalie disegnate su di essa restano.

## Dalla riga di comando { #cli }

Gli stessi passi con `ppg plugin geophysics COSA PROGETTO ...`. Ogni comando lavora sul chunk attivo
(`--chunk NOME` per un altro) e salva il progetto. Una griglia si indica con il nome o con l'id.

```bash
ppg plugin geophysics --help
ppg plugin geophysics import scavo.ppg area1.grd --kind magnetometry --name "Area 1"
ppg plugin geophysics import scavo.ppg area2.xyz --kind magnetometry --name "Area 2" --cell 0.5
ppg plugin geophysics import scavo.ppg area3.csv --kind resistivity --name "Area 3" --dummy -1
ppg plugin geophysics import scavo.ppg slice_gpr --kind gpr --name "GPR 2026"
ppg plugin geophysics list scavo.ppg
ppg plugin geophysics georef scavo.ppg "Area 1" --origin 500100.0,4500800.0 --azimuth 95
ppg plugin geophysics georef scavo.ppg "Area 2" --pairs "0 0 500200 4500800; 30 0 500229.9 4500802.6; 0 20 500198.3 4500819.9"
ppg plugin geophysics filter scavo.ppg "Area 1" destripe --copy
ppg plugin geophysics filter scavo.ppg "Area 1 destripe" despike size=3 k=4
ppg plugin geophysics filter scavo.ppg "Area 1 destripe" highpass sigma=8 --copy
ppg plugin geophysics filter scavo.ppg "Area 1" clip low=-20 high=20 absolute=1 --copy
ppg plugin geophysics export scavo.ppg "Area 1 destripe highpass" area1.tif
ppg plugin geophysics export scavo.ppg "GPR 2026" slice_3.grd --band 2
```

| Comando | Opzioni |
|---|---|
| `import PROGETTO FILE` | un file o una cartella di slice; `--kind` magnetometry, resistivity, gpr, em, susceptibility, other (magnetometry); `--name`; `--units`; per XYZ, `--cell` m e `--dummy` (ripetibile) |
| `list PROGETTO` | una riga per griglia: nome, tipo, unità, dimensioni, cella, bande, posizione, mediana, filtri, come è stata letta una tabella XYZ; il numero di anomalie disegnate - nella lingua del programma |
| `georef PROGETTO GRIGLIA` | `--origin E,N` con `--azimuth` (90), oppure `--pairs "lx ly E N; lx ly E N; ..."`, con `--free-scale` per adattare anche le dimensioni |
| `filter PROGETTO GRIGLIA FILTRO [k=v ...]` | `FILTRO`: `despike` (`size`, `k`), `destripe` (`axis=rows` o `cols`, `stat=median` o `mean`), `clip` (`low`, `high`, `absolute=1` per dare valori invece di percentili), `lowpass` (`sigma`, 1), `highpass` (`sigma`, 10), `fill` (`distance`), `resample` (`cell`); `--copy` in una nuova griglia |
| `export PROGETTO GRIGLIA USCITA` | `.tif`: tutte le bande; `.grd`: una banda, `--band` contata da 0, una griglia ruotata ricampionata con il nord in alto |

I valori predefiniti sono quelli della finestra: passa-basso sigma 1 cella, passa-alto sigma 10 celle. Ogni
comando stampa che cosa ha fatto: la griglia importata (con come è stata letta una tabella XYZ), la nuova
posizione con il residuo, i filtri e le statistiche del risultato.

## Leggere i risultati { #reading }

### Colori e valori { #reading-colours }

La griglia viene colorata fra due valori, scritti in fondo al pannello nelle unità della griglia: per
magnetometria, EM e GPR la mediana più e meno 2,5 deviazioni standard robuste, per gli altri tipi il 2°
e il 98° percentile. Ciò che sta oltre viene disegnato con il colore dell'estremo: il nucleo di un
dipolo forte è bianco e nero pieno. In *Grigi* i valori alti sono bianchi; *Grigi invertiti* dà la
convenzione opposta di alcuni programmi di elaborazione.

* Dopo il *Destripe* ogni traversa ha la mediana a zero, e così la griglia; dopo un *Passa-alto* la sua
  media è zero. I valori allora sono differenze da ciò che sta intorno, non valori del campo.
* Il passa-alto lascia bande più scure ai lati di un'anomalia positiva forte, come nel riquadro (d)
  della figura: sono il filtro, non il terreno. Un sigma di qualche volta la larghezza delle anomalie
  cercate le conserva intere.
* *Ricampiona* e *Passa-basso* lisciano: misura l'intensità di un'anomalia sulla griglia grezza o dopo
  il destripe.

### Controllare la posa { #reading-placement }

Sovrapponi la griglia con un po' di trasparenza e cerca ciò che si vede in tutte e due: un confine di
campo, un sentiero, una trincea, un muro nell'ortofoto che anche la geofisica vede. Uno spostamento di
qualche decimetro in una direzione indica un picchetto o un marker nel posto sbagliato, una rotazione
l'angolo sbagliato; il residuo della georeferenziazione dice quanto i picchetti sono d'accordo fra
loro, non se sono i picchetti giusti.

### Limiti { #limits }

* **Nessuna riproiezione.** Una griglia con un sistema di riferimento deve stare in quello del chunk.
* **Righe intere.** Il *Destripe* sottrae la mediana di ogni riga della griglia importata: in un mosaico
  di più griglie di prospezione una riga le attraversa tutte. Le griglie percorse separatamente è meglio
  passarle al destripe nel programma di elaborazione, o importarle una per una.
* **Due dimensioni.** Le slice di un GPR sono bande di una griglia, viste una alla volta; non si
  costruisce un volume.
* **Nessuna interpretazione.** Il plugin disegna la griglia dove deve stare: le anomalie le leggono
  l'archeologo e il geofisico, e le disegnano con *Disegna anomalia* o in QGIS.

## File scritti { #files }

Tutto va nella cartella del chunk, accanto al file del progetto:
`<progetto>.ppg.files/chunk_<n>/geophysics/`. L'elenco delle griglie - nome, tipo, unità, cella,
posizione, slice, file di origine, filtri, come è stata letta una tabella XYZ - e le anomalie disegnate
stanno nel progetto stesso.

* `<id>.tif`: ogni griglia, float32 senza valore dove non ci sono letture, una banda per slice
  (l'etichetta della slice come descrizione della banda), con il suo sistema di riferimento una volta
  georeferenziata; l'id è fatto con data e ora dell'importazione (`g20261023163609...`).
* I GeoTIFF e i GRD esportati con *Esporta...* o `export`, dove scegli.
* Il GeoPackage di *File > Esporta*, con accanto `<nome>_<griglia>.tif` per ogni griglia
  georeferenziata.
