# Sentinel-2

Il plugin *Sentinel-2* guarda l'area di un rilievo con i satelliti Sentinel-2 del programma Copernicus
dell'Unione europea, data dopo data, per trovare i **crop mark**: muri e fossati sepolti che si vedono
nella coltura che li copre in alcune settimane di alcuni anni. Sopra un muro le radici trovano meno
acqua e la coltura è più debole; sopra un fossato ne trovano di più e la coltura è più verde. Il plugin
cerca le immagini dell'area in un catalogo pubblico, ne legge solo una piccola finestra intorno al sito,
maschera nuvole e ombre, calcola tre indici di vegetazione per ogni data serena, ne disegna la serie
lungo le stagioni e fa compositi e mappe dei crop mark che si posano sull'ortofoto o sul DEM del rilievo
e vanno in QGIS. Non servono account né chiavi.

Questa pagina descrive la versione 0.1.0 del plugin, che richiede pyarchinit-photogrammetry 1.3.3 o
successiva. Il README e il codice sono nel
[repository dei plugin](https://github.com/enzococca/pyarchinit-photogrammetry-plugins/tree/main/plugins/sentinel2).

<figure markdown="span">
  ![L'NDVI dell'estate e la sua mappa dei crop mark intorno a Stonehenge](../img/plugins/sentinel2/cropmarks.jpg)
  <figcaption>(a) L'NDVI dell'estate 2023 intorno a Stonehenge, la mediana di tre date serene (13 e 25
  giugno, 22 agosto), cella per cella. (b) La sua mappa dei crop mark: gli stessi valori meno il loro
  sfondo liscio (50 m), divisi per la loro dispersione. Le differenze fra un campo e l'altro spariscono;
  restano linee e macchie più deboli (rosso, sotto -2) o più verdi (verde, sopra 2) di ciò che hanno
  intorno: qui le strade, i sentieri e i percorsi intorno alle pietre, tutti moderni. La linea gialla è
  l'area dell'analisi, 400 m di lato. Contains modified Copernicus Sentinel data 2023 (contiene dati
  Copernicus Sentinel modificati, 2023).</figcaption>
</figure>

!!! note "Gli esempi di questa pagina"
    Le figure vengono da un progetto fatto per questa pagina: un chunk nel British National Grid senza
    alcun rilievo dentro, e un'area di 400 x 400 m intorno a Stonehenge, un monumento pubblico, data
    come riquadro in longitudine e latitudine dalla riga di comando. La ricerca sul 2023 ha trovato 23
    acquisizioni; sei (19 gennaio, 4 aprile, 26 maggio, 13 e 25 giugno, 22 agosto) sono state lette,
    circa 110 MB in 22 secondi. Per le immagini del pannello le tre date dell'estate sono state lette
    di nuovo, e il 22 agosto è stato poi tolto dalla cache tenendo i suoi prodotti. Mostrano che cosa fa
    il plugin e non sono uno studio del monumento: con 10 m per pixel, qui i segni più forti sono
    moderni.

## In breve

1. Un chunk in un sistema di riferimento sulla Terra - un fuso UTM, una griglia nazionale - con il suo
   DEM o la sua ortofoto; oppure un riquadro in longitudine e latitudine dalla riga di comando.
2. Nel pannello *Sentinel-2*: *Dal DEM o dall'ortofoto* (oppure *Disegna
   l'area...*), con un margine intorno.
3. *Cerca*: il primo e l'ultimo giorno, le nuvole al massimo. Le acquisizioni riempiono la tabella.
4. *Scarica e calcola*: la finestra di ogni acquisizione viene letta una volta e tenuta nel progetto;
   quelle nuvolose restano fuori; seguono gli indici, la serie e il suo grafico.
5. *Vista*: un indice, una data o un periodo, una palette, un'opacità, poi *Sovrapponi* sull'ortofoto o
   sul DEM.
6. *Mappa dei crop mark* e *Composito del periodo* fanno prodotti da mostrare, esportare o aprire in
   QGIS.
7. *Rimuovi dalla cache* e *Svuota la cache...* tolgono dal progetto le acquisizioni lette; il
   prossimo *Scarica e calcola* le legge di nuovo. Dal programma 1.3.4 i prodotti sono anche
   nell'albero dell'*Area di lavoro*.

## Che cosa fa

| Passo | Che cosa dà | A che cosa serve |
|---|---|---|
| Area | l'impronta del DEM o dell'ortofoto, o un poligono disegnato sulla vista, con un margine; una griglia di celle da 10 m nel fuso UTM del sito | il sito e i campi intorno, che servono allo sfondo di una mappa dei crop mark |
| Ricerca | le acquisizioni Sentinel-2 L2A sull'area fra due giorni, con la copertura nuvolosa dei loro tile | che cosa hanno visto i satelliti, e quando |
| Lettura | le bande della finestra e la classificazione della scena, nuvole e ombre mascherate, tenute nel progetto | una serie che, una volta letta, non chiede più la rete |
| Indici | NDVI, NDRE e NDMI di ogni data serena, sulle celle libere da nuvole | il vigore, la clorofilla e l'acqua della vegetazione |
| Serie | la mediana e la dispersione di ogni indice sull'area, per data e per mese, in CSV e in un grafico | quando i campi rinverdiscono e seccano, le settimane in cui i segni possono comparire |
| Compositi | la mediana di un indice in una stagione, un anno, un mese, un intervallo di giorni | un periodo senza il rumore di una data sola |
| Mappe dei crop mark | l'indice meno il suo sfondo liscio, diviso per la sua dispersione: uno z-score | le linee e le macchie diverse dal campo che hanno intorno |
| Esportazioni | GeoTIFF sulla griglia delle scene e sulla griglia del rilievo, CSV, PNG, il progetto QGIS del programma | il GIS, la relazione, l'archivio |
| Cache | le acquisizioni lette, tenute nel progetto; alcune o tutte tolte a richiesta | un progetto che resta piccolo; date rilette quando servono |
| Albero del progetto (dal programma 1.3.4) | un nodo *Sentinel-2* sotto il chunk: acquisizioni, indici per data, serie, compositi, mappe dei crop mark | tutti i prodotti del chunk sottomano, senza il pannello |

Con 10 m per pixel, Sentinel-2 vede gli elementi più grandi - recinti, fossati e strade larghi qualche
metro, ville, il perimetro di un insediamento - e soprattutto **quando** i campi li mostrano: i
satelliti passano ogni pochi giorni, anno dopo anno, dal 2015. Un muro da solo, una buca di palo, un
fossato stretto sono più piccoli di un pixel: quelli sono per il drone e la sua camera multispettrale
(vedi [Il pannello Multispettrale](../gui.md#il-pannello-multispettrale)).

## Che cosa serve prima { #before }

* **Il plugin**, installato dal programma: vedi [Guide ai plugin](guides.md#install). Non gli serve
  altro: parla con il catalogo con la libreria standard di Python (o con
  [pystac-client](https://github.com/stac-utils/pystac-client), se è installato) e legge le immagini con
  il GDAL del programma.
* **Internet**, per la ricerca e per la prima lettura di ogni acquisizione: il catalogo è
  [Earth Search](https://earth-search.aws.element84.com/v1) di Element 84, le immagini sono i
  Cloud-Optimized GeoTIFF della [collezione Sentinel-2 del Registry of Open Data on
  AWS](https://registry.opendata.aws/sentinel-2-l2a-cogs/), letti via HTTPS. Ciò che è stato letto resta
  nel progetto: una seconda occhiata, un indice nuovo, una mappa nuova non usano la rete.
* **Un chunk con un sistema di riferimento sulla Terra**: un sistema proiettato in metri, come un fuso
  UTM o una griglia nazionale. Un chunk in un sistema locale (*LOCAL*) non ha un posto sulla Terra: il
  plugin lo dice e dalla finestra non lo può cercare. Dalla riga di comando si può comunque cercare un
  riquadro in longitudine e latitudine anche per un chunk così; i suoi prodotti sono allora i GeoTIFF in
  UTM, senza sovrapposizione e senza copie sulla griglia del rilievo.
* **L'area**: la danno il DEM o l'ortofoto del chunk. Se mancano, la prima volta si dà dalla riga di
  comando, come riquadro o poligono; poi si può mostrare una cornice vuota dell'area e disegnarci sopra
  un'area nuova (vedi [L'area](#step-area)).

## I dati { #data }

### Sentinel-2 { #sentinel-2 }

I satelliti Sentinel-2 fotografano le terre emerse in tredici bande, dal blu all'infrarosso a onda
corta. Il plugin legge i prodotti di *livello 2A* - riflettanza al suolo, corretta per l'atmosfera - in
tile di 110 x 110 km, e sette delle loro bande:

| Banda | Luce | Pixel |
|---|---|---|
| B02, B03, B04 | blu, verde, rosso | 10 m |
| B08 | infrarosso vicino | 10 m |
| B05 | bordo del rosso (red edge) | 20 m |
| B8A | infrarosso vicino stretto | 20 m |
| B11 | infrarosso a onda corta | 20 m |

con la *classificazione della scena* (SCL) che l'ESA calcola per ogni pixel: vegetazione, suolo nudo,
acqua, nuvola, cirro, ombra di nuvola, neve. Tutti i tile di un passaggio di un satellite sull'area sono
un'**acquisizione**, che prende il nome dal giorno e dal satellite: `20230613_S2A`.

### L'area e la sua griglia { #area }

L'area è l'impronta del DEM (o altrimenti dell'ortofoto) del chunk, un poligono disegnato sulla vista
o un riquadro in longitudine e latitudine. Intorno c'è un **margine**, 300 m se non lo cambi: lo sfondo
di una mappa dei crop mark si calcola dai campi intorno al sito, e una finestra che si ferma al bordo
del sito uno sfondo non ce l'ha. Tutto si legge su un'unica griglia di celle da 10 m nel fuso UTM del
centro dell'area, con gli angoli su multipli di 20 m, così un pixel da 20 m è sempre quattro celle da
10 m; al massimo 25 km di lato. Quando un'area nuova cambia la griglia, ciò che era stato letto per
la griglia vecchia si rilegge, dalla rete.

### Nuvole e ombre { #clouds }

* **Nella ricerca**, *Nuvole al massimo (%)* filtra le acquisizioni per la copertura nuvolosa dei loro
  tile interi, un dato del catalogo: un tile coperto a metà può essere sereno sul sito, e viceversa.
* **Nella lettura**, per ogni acquisizione si legge prima la classificazione della scena. Nuvole (classi
  8 e 9), cirri sottili (10) e ombre di nuvola (3) vengono mascherate, con 20 m intorno; solo vegetazione
  (4), suolo nudo (5) e acqua (6) restano come sereni. Quando è serena meno della *Parte serena minima
  dell'area (%)* - il 50 % se non la cambi - l'acquisizione è segnata *nuvolosa* e le sue altre bande non
  vengono mai scaricate.
* Dalla riga di comando, `--clear 4,5,6,7` tiene anche i pixel non classificati e `--buffer` cambia i
  metri mascherati intorno a nuvole e ombre.

### Dai numeri alla riflettanza { #reflectance }

I numeri delle immagini diventano riflettanza con la scala e l'offset che dà il catalogo. Dalla
processing baseline 04.00 (gennaio 2022) l'ESA aggiunge 1.000 ai numeri dei prodotti L2A, e un offset
di -0,1 li toglie; Earth Search segna le immagini i cui numeri ha già corretto. Il plugin crede ai
numeri più che ai metadati: un offset che renderebbe negativa la riflettanza nel blu del terreno viene
lasciato cadere.

### I crop mark { #crop-marks }

I crop mark sono una questione di stagione e di annata. Compaiono nei cereali e nell'erba sotto
stress - una primavera secca, l'inizio dell'estate nell'Europa temperata - e possono mancare in un
anno piovoso. Un muro o una strada sepolti trattengono meno acqua: la coltura sopra è più debole e il suo
indice più basso (un segno *negativo*). Un fossato o una fossa ne trattengono di più: la coltura è più
verde (un segno *positivo*). Lo stesso campo può mostrare un segno per due settimane e niente per il
resto dell'anno: per questo la serie conta più di qualsiasi immagine da sola.

I tre indici, ciascuno da due bande (a - b) / (a + b):

| Indice | Bande | Pixel | Che cosa segue |
|---|---|---|---|
| NDVI | B08, B04 | 10 m | il vigore della vegetazione; il più nitido dei tre |
| NDRE | B8A, B05 | 20 m | la clorofilla; satura meno sulle colture fitte |
| NDMI | B8A, B11 | 20 m | l'acqua nelle foglie |

## Passo per passo nel programma { #program }

### Il pannello

Dopo aver installato il plugin e riavviato il programma, il pannello *Sentinel-2* si apre a destra nella finestra, fra le schede di *PyArchInit*; si sposta, si stacca o si chiude come gli altri pannelli e si riapre da *Vista > Pannelli*, oppure con *Plugin > Sentinel-2 > Pannello Sentinel-2*.

<figure markdown="span">
  ![Il pannello Sentinel-2](../img/plugins/sentinel2/pane-parts.png)
  <figcaption>Il pannello intero, in due metà affiancate: a sinistra l'area, la ricerca, le acquisizioni
  trovate e lette, i pulsanti della lettura e della cache e il grafico della serie; a destra la vista
  con la sua legenda, i crop mark, i prodotti fatti e la dicitura dei dati. Il 22 agosto è stato tolto
  dalla cache tenendo i prodotti: i due prodotti fatti anche con quella data sono segnati <em>(date non
  più in cache)</em>, e la serie ha le due date rimaste. Contains modified Copernicus Sentinel data 2023
  (contiene dati Copernicus Sentinel modificati, 2023).</figcaption>
</figure>

Dall'alto:

* una breve descrizione e lo stato: acquisizioni trovate, lette, date serene nella serie;
* **Area**: *Margine (m)*; *Dal DEM o dall'ortofoto*, *Disegna l'area...*, *Mostra l'area intera*;
  l'area, il suo margine e la sua griglia in una riga;
* **Ricerca**: *Dal*, *Al*, *Nuvole al massimo (%)*, *Catalogo STAC*, *Cerca*;
* la tabella delle acquisizioni: *Data*, *Satellite*, *Tile*, *Nuvole %* (dei tile), *Sereno %*
  (dell'area, dopo la lettura), *Stato* - *da leggere*, *letta*, *nuvolosa*, *errore* (il suggerimento
  della data dà l'id dell'acquisizione e l'errore);
* *Parte serena minima dell'area (%)*, *Scarica e calcola*, *Ricalcola*, *Rimuovi dalla cache*,
  *Svuota la cache...*; il tasto destro sulla tabella ha *Mostra (colori naturali)*, *Rimuovi dalla
  cache (N)* e *Svuota la cache...*, e un doppio clic su un'acquisizione letta ne mostra i colori
  naturali;
* il grafico della serie; un doppio clic lo apre a piena grandezza;
* **Vista**: *Indice*, *Data o periodo*, *Palette*, *Opacità*, *Sovrapponi*, *Nascondi*, e i colori
  mostrati;
* **Crop mark**: *Sfondo (m)*, *Metodo*, *Mappa dei crop mark*, *Composito del periodo*;
* la tabella dei prodotti - *Nome*, *Date*, *Fatto* - con *Sovrapponi*, *Esporta...*, *Rimuovi*,
  *QGIS* ed *Esporta tutto...*; un prodotto fatto anche da acquisizioni non più in cache è segnato
  *(date non più in cache)*;
* la dicitura dei dati: *Contiene dati Copernicus Sentinel modificati* e gli anni.

Il menu *Plugin > Sentinel-2* ha *Pannello Sentinel-2*, *Cerca immagini Sentinel-2...*, *Disegna l'area
di Sentinel-2*, *Scarica e calcola*, *Mappa dei crop mark...* e *Nascondi la sovrapposizione*; *File >
Esporta* ha *Serie temporale Sentinel-2 (CSV)...* e *Stack Sentinel-2 (GeoTIFF)...*. Ogni passo gira in
background con la barra di avanzamento; i pulsanti che ne farebbero partire un altro aspettano. Il
pannello segue il chunk attivo: ogni chunk ha la sua area, le sue acquisizioni e i suoi prodotti.

### 1. L'area { #step-area }

* *Dal DEM o dall'ortofoto*: le celle del DEM con un valore (o altrimenti dell'ortofoto) come poligono
  convesso - la scelta di sempre per un rilievo.
* *Disegna l'area...*: trascina un riquadro sulla vista, oppure fai clic sui vertici e chiudi con doppio
  clic o Invio; Esc annulla. Per disegnare la vista ha bisogno dell'ortofoto, del DEM o di una cornice
  vuota.
* *Margine (m)*: impostalo prima; si può cambiare anche nella ricerca.
* *Mostra l'area intera*: una cornice vuota con l'area e il suo margine, a 2 m per pixel: così i
  prodotti si vedono anche oltre l'ortofoto, che di solito copre solo il sito.

L'area (gialla) e la finestra letta (grigia) sono disegnate nel layer *Sentinel-2: area* della vista, e
tornano quando la vista mostra un altro raster. La riga sotto i pulsanti ne dà la misura, il margine e
la griglia - *Area: 412 x 407 m (riquadro), margine 300 m; griglia EPSG:32630: 104 x 104 celle da 10 m*
nell'esempio.

### 2. Ricerca { #search }

| Campo | Predefinito | Che cosa fa |
|---|---|---|
| *Dal*, *Al* | dal primo gennaio di due anni fa a oggi | i giorni cercati |
| *Nuvole al massimo (%)* | 30 | la copertura nuvolosa più alta dei tile |
| *Catalogo STAC* | Earth Search v1 | qualsiasi API STAC con la collezione `sentinel-2-l2a`, dal suo indirizzo |

*Cerca* (oppure *Plugin > Sentinel-2 > Cerca immagini Sentinel-2...*, che chiede le stesse cose in una
finestra insieme al margine) chiede al catalogo le acquisizioni sull'area e sul suo margine. Prendono il
posto di quelle della ricerca precedente; quelle già lette restano lette. Senza un'area, la ricerca
prende l'impronta del DEM o dell'ortofoto. Nell'esempio il 2023 ha dato 23 acquisizioni in due secondi,
tutte sul tile 30UWB.

Un intervallo lungo su una regione nuvolosa dà molte acquisizioni: pochi anni possono essere centinaia.
La ricerca costa poco; è la lettura a chiedere tempo e dati, e si può fare prima su poche date.

### 3. Scarica e calcola { #read }

*Scarica e calcola* legge le acquisizioni selezionate nella tabella - tutte, se non ne selezioni
nessuna - quattro alla volta, poi calcola gli indici e la serie:

* prima la classificazione della scena: un'acquisizione *nuvolosa* si ferma lì;
* poi le sette bande, solo la finestra dell'area e del suo margine. Le immagini si leggono per tasselli
  interni da 1.024 pixel, perciò ogni acquisizione sono circa 16-18 MB da scaricare, qualunque sia la
  grandezza del sito, e circa 10 secondi; ciò che resta nel progetto sono circa 100 KB;
* le acquisizioni già lette e quelle già note come nuvolose non si rileggono;
* *Annulla* ferma le letture alla banda successiva.

La riga di stato dice poi quante acquisizioni sono state lette, trovate già nel progetto, nuvolose o
con un errore, e quante date serene ha la serie. *Ricalcola* rifà gli indici e la serie da ciò che è
stato letto, senza la rete, con la *Parte serena minima dell'area* impostata nel pannello, e la tiene:
alzala per lasciare fuori dalla serie le date più velate, abbassala per riprenderle. Un'acquisizione
segnata *nuvolosa* non è mai stata scaricata: con un valore più basso, il prossimo *Scarica e calcola*
la legge.

### 4. La cache { #cache }

Ogni acquisizione letta resta nel progetto come un piccolo GeoTIFF - la finestra dell'area, le sue
sette bande e la classificazione della scena, circa 100 KB - e gli indici, la serie e i prodotti si
fanno da questi file. Si possono togliere:

* *Rimuovi dalla cache*: le acquisizioni lette selezionate nella tabella (anche dal tasto destro,
  *Rimuovi dalla cache (N)*);
* *Svuota la cache...*: tutte le acquisizioni lette.

Prima arriva una domanda, con le acquisizioni o i file e i megabyte interessati. Se da quelle date sono
stati fatti compositi o mappe dei crop mark, chiede anche che cosa farne: *Rimuovi anche i prodotti*
oppure *Tieni i prodotti*. Poi i file vengono cancellati e le acquisizioni tornano *da leggere* nella
tabella - restano nella ricerca, e il prossimo *Scarica e calcola* le rilegge dalla rete - e indici e
serie vengono ricalcolati con le date rimaste (se non ne resta nessuna, vengono cancellati). Una
sovrapposizione che mostrava qualcosa di tolto viene nascosta.

Un prodotto tenuto è ciò che è stato calcolato allora, e non cambia; ma la cache non può più rifarlo,
perciò la tabella dei prodotti lo segna *(date non più in cache)* e il suo suggerimento dice le
acquisizioni che mancano. Il segno se ne va quando vengono lette di nuovo.

Il tasto destro sulla tabella ha anche *Mostra (colori naturali)* per un'acquisizione letta, che sia
entrata o no nella serie (un doppio clic sulla sua riga fa lo stesso): i colori naturali di quella
data sulla vista.

### 5. Sulla vista { #view }

| Campo | Che cosa fa |
|---|---|
| *Indice* | *NDVI (vigore della vegetazione)*, *NDRE (clorofilla, bordo del rosso)*, *NDMI (acqua nella vegetazione)* oppure *Colori naturali (B04, B03, B02)* |
| *Data o periodo* | prima i periodi - ogni stagione di ogni anno con date, ogni stagione su tutti gli anni, ogni anno, tutto il periodo - poi le date, dalla più recente, con il satellite e la parte serena |
| *Palette* | *Rosso-giallo-verde* (quella degli indici di vegetazione del programma), *Scala di grigi*, *Viridis*, *Turbo*, *Divergente (blu-bianco-rosso)* |
| *Opacità* | dal 10 al 100 %; cambia subito la sovrapposizione mostrata |

*Sovrapponi* posa l'indice della data, o la mediana del periodo, sull'ortofoto o sul DEM; i colori
naturali mostrano la data più serena del periodo. Un indice viene steso fra il 2° e il 98° percentile,
una mappa dei crop mark fra -3 e 3. Una mappa dei crop mark si legge come un indice, nel pannello come
in QGIS: con la palette predefinita il rosso è vegetazione più debole, il verde più vigorosa. La riga
sotto i pulsanti dà i valori ai due estremi e i loro colori, e per una mappa dei crop mark che cosa
significano: *z da -3 (rosso) a 3 (verde): sotto -2 la vegetazione è più debole di quella intorno
(muri, strade, terreno compattato), sopra 2 è più vigorosa (fossati, fosse)*. Ogni pixel di
Sentinel-2 resta un quadrato di 10 m: centinaia di pixel dell'ortofoto. Si mostra una sovrapposizione
alla volta; *Nascondi* la toglie, e viene ridisegnata quando la vista mostra un altro raster dello
stesso chunk.

<figure markdown="span">
  ![La mappa dei crop mark sulla cornice dell'area](../img/plugins/sentinel2/pane.png)
  <figcaption>La mappa dei crop mark dell'estate 2023 posata sulla cornice di <em>Mostra l'area
  intera</em>, con l'area dell'analisi in giallo; nella tabella è selezionato il 13 giugno fra le
  acquisizioni lette. Contains modified Copernicus Sentinel data 2023 (contiene dati Copernicus
  Sentinel modificati, 2023).</figcaption>
</figure>

### 6. Compositi e mappe dei crop mark { #products }

Entrambi prendono l'*Indice* e la *Data o periodo* scelti nel gruppo *Vista* (i colori naturali non ne
hanno).

* *Composito del periodo*: la mediana dell'indice sulle date del periodo, cella per cella, e quante date
  hanno dato un valore lì.
* *Mappa dei crop mark*: l'indice della data, o la mediana del periodo, meno il suo sfondo liscio, diviso
  per la dispersione robusta di ciò che resta (1,4826 volte la deviazione assoluta mediana): uno
  z-score, vicino a 0 sul terreno uniforme, sotto -2 dove la vegetazione è più debole di quella intorno,
  sopra 2 dove è più verde.

| Campo | Predefinito | Che cosa fa |
|---|---|---|
| *Sfondo (m)* | 50 | la scala dello sfondo: il sigma di una gaussiana, che ignora le celle senza valore; la mediana usa una finestra larga il doppio |
| *Metodo* | *Gaussiana* | *Gaussiana* o *Mediana*; la mediana segue più da vicino i bordi dei campi |

*Plugin > Sentinel-2 > Mappa dei crop mark...* chiede le stesse cose in una finestra. Il prodotto viene
posato subito sulla vista ed entra nella tabella; per una mappa dei crop mark il suggerimento dà la
parte dell'area sotto -2 e sopra 2. Un prodotto rifatto con lo stesso indice, periodo, sfondo e metodo
prende il posto di quello vecchio.

I periodi si scrivono come li scrive la riga di comando:

| Periodo | Significa |
|---|---|
| `all` | tutte le date serene |
| `summer` | le estati di tutti gli anni (`spring`, `summer`, `autumn`, `winter`) |
| `2023-summer` | giugno, luglio e agosto 2023; `2023-winter` è dicembre 2022, gennaio e febbraio 2023 |
| `2023` | l'anno |
| `2023-06` | il mese |
| `2023-06-14` | un giorno |
| `2023-04-01/2023-06-30` | un intervallo di giorni |
| `20230613_S2A` | un'acquisizione |

### 7. QGIS ed esportazioni { #export }

* *Sovrapponi* su un prodotto della tabella (o un doppio clic) lo mostra sulla vista.
* *Esporta...*: il prodotto in GeoTIFF sulla griglia delle scene, con accanto la sua copia sulla griglia
  del rilievo (`..._chunk.tif`).
* *Rimuovi* chiede prima, poi cancella i file del prodotto.
* *QGIS*: il progetto QGIS del programma ([QGIS](../integrations.md)) con i prodotti sulla griglia del
  rilievo, nascosti all'inizio, nel rosso-giallo-verde degli indici di vegetazione: i compositi fra il
  loro 10° e 90° percentile, le mappe dei crop mark da -3 (rosso, vegetazione più debole) a 3 (verde,
  più vigorosa), come nel pannello.
* *Esporta tutto...*: in una cartella, lo stack di ogni indice e data in un solo GeoTIFF
  (`sentinel2_stack.tif`, bande con nomi come `NDVI 2023-06-13 20230613_S2A`), un GeoTIFF per indice,
  la serie per data e per mese (CSV) con il grafico, ogni prodotto sulle due griglie e un file di testo
  con la dicitura dei dati e le formule.
* *File > Esporta > Serie temporale Sentinel-2 (CSV)...* scrive la serie per data, accanto quella
  mensile (`..._monthly.csv`) e il grafico (`.png`); *Stack Sentinel-2 (GeoTIFF)...* lo stack.

### 8. Nell'albero del progetto { #tree }

Da pyarchinit-photogrammetry 1.3.4, ogni chunk su cui il plugin ha lavorato ha un nodo *Sentinel-2*
nell'albero dell'*Area di lavoro*, dopo i prodotti del programma; con la 1.3.3 fa tutto il pannello.

| Elemento | Doppio clic | Tasto destro |
|---|---|---|
| *Sentinel-2* | il pannello | *Mostra il pannello*, *Mostra l'area intera*, *Nascondi la sovrapposizione*, *Esporta tutto...*, *Apri in QGIS*, *Svuota la cache...* |
| *Acquisizioni lette (N)*, una riga per data con il satellite e la parte serena | i colori naturali di quella data | *Mostra (colori naturali)*, *Nascondi*, *Esporta...* (la riflettanza delle sette bande e la classificazione della scena, in un GeoTIFF), *Rimuovi dalla cache*; sul gruppo, *Svuota la cache...* |
| *Indici per data (N date)*: NDVI, NDRE e NDMI, ognuno con le sue date | l'indice di quella data | *Mostra*, *Nascondi*, *Esporta...* (quell'indice di quella data, sulle due griglie), *Rimuovi dalla cache*; su un indice, *Mostra la mediana di tutto il periodo* |
| *Serie temporale* | il grafico | *Apri il grafico*, *Esporta la serie (CSV)...* |
| *Compositi (N)*, *Crop mark (N)* | il prodotto sulla vista | *Mostra*, *Nascondi*, *Esporta...*, *Apri in QGIS*, *Rimuovi* |

Un doppio clic rende attivo il chunk dell'elemento e mostra l'elemento sulla vista, e il pannello segue
la scelta. Il nodo si riempie di nuovo dopo ogni cambiamento, con lo stesso segno *(date non più in
cache)* del pannello. Le due esportazioni di una sola data - la sua riflettanza, un indice - ci sono
solo nell'albero.

## Dalla riga di comando { #cli }

Gli stessi passi con `ppg plugin sentinel2 COSA PROGETTO ...`. I comandi lavorano sul chunk attivo
(`--chunk ETICHETTA` per un altro) e salvano il progetto.

```bash
ppg plugin sentinel2 --help
ppg plugin sentinel2 search scavo.ppg --start 2021-01-01 --end 2024-12-31
ppg plugin sentinel2 search scavo.ppg --max-cloud 20 --margin 500
ppg plugin sentinel2 search scavo.ppg \
    --bbox -1.8291,51.1771,-1.8233,51.1807 --start 2023-01-01 --end 2023-12-31
ppg plugin sentinel2 fetch scavo.ppg --min-clear 0.5
ppg plugin sentinel2 fetch scavo.ppg --scenes 20230613_S2A,20230625_S2B
ppg plugin sentinel2 compute scavo.ppg --min-clear 0.8
ppg plugin sentinel2 series scavo.ppg --monthly --index NDVI
ppg plugin sentinel2 composite scavo.ppg --index NDVI --period 2023-summer
ppg plugin sentinel2 cropmarks scavo.ppg --period 2023-summer --background 50
ppg plugin sentinel2 list scavo.ppg
ppg plugin sentinel2 export scavo.ppg esportazione_sentinel2/
ppg plugin sentinel2 remove scavo.ppg cropmarks_NDVI_2023_summer_50m
ppg plugin sentinel2 remove-scene scavo.ppg 20230822_S2A
ppg plugin sentinel2 remove-scene scavo.ppg --all --with-products
```

| Comando | Opzioni |
|---|---|
| `search PROGETTO` | `--start`, `--end` (AAAA-MM-GG; dal primo gennaio di due anni fa a oggi); `--max-cloud` % (30); `--margin` m (300); `--area auto`, `dem`, `orthomosaic`, oppure `keep` l'area già impostata; `--bbox OVEST,SUD,EST,NORD` in gradi (anche come quattro numeri); `--polygon "x,y;x,y;x,y"` nelle coordinate del chunk; `--catalog URL`, `--collection`; `--client auto`, `stdlib` o `pystac` |
| `fetch PROGETTO` | `--scenes ID,ID` solo queste acquisizioni; `--min-clear` 0-1 (0.5); `--clear` classi SCL tenute (4,5,6); `--buffer` m (20); `--workers` acquisizioni lette insieme (4); `--no-compute` |
| `compute PROGETTO` | indici e serie di nuovo da ciò che è stato letto, senza rete; `--min-clear` 0-1, tenuto per le volte successive (quello dell'ultima lettura se non è dato) |
| `series PROGETTO` | la serie per data, oppure `--monthly`; `--index NDVI`, `NDRE` o `NDMI` |
| `composite PROGETTO` | `--index` (NDVI); `--period` (all) |
| `cropmarks PROGETTO` | `--index` (NDVI); `--period` (all); `--background` m (50); `--method gaussian` o `median` |
| `list PROGETTO` | l'area, la griglia, la ricerca, la cache (file, megabyte, acquisizioni), le acquisizioni, le date e i prodotti con le date non più in cache, in JSON |
| `export PROGETTO PERCORSO` | una cartella: tutto, come *Esporta tutto...*; un `.tif`: lo stack; un `.csv`: la serie |
| `remove PROGETTO ID` | cancella un prodotto e i suoi file |
| `remove-scene PROGETTO ID [ID...]` | le acquisizioni fuori dalla cache; `--all` tutte; `--with-products` rimuove anche i prodotti fatti da esse, altrimenti restano, segnati. Un id che non è nella cache viene rifiutato, e il messaggio elenca quelli che ci sono |

Un riquadro a ovest di Greenwich comincia con il segno meno: scrivilo così com'è
(`--bbox -1.8291,51.1771,...`), come `--bbox=...` o come quattro numeri; anche `--polygon` accetta
coordinate negative. Gli id delle acquisizioni sono quelli che stampano `search` e `list`; gli id dei
prodotti - `composite_NDVI_2023_summer`, `cropmarks_NDVI_2023_summer_50m` - quelli di `list`.

## Leggere i risultati { #reading }

### La serie { #reading-series }

<figure markdown="span">
  ![La serie dei tre indici sull'area](../img/plugins/sentinel2/series.png)
  <figcaption>Il grafico che scrive il plugin: per ogni indice, un punto per la mediana dell'area a ogni
  data, una barra spessa dal 25° al 75° percentile e una sottile dal 10° al 90°; la linea unisce le
  mediane mensili. Sei date del 2023 intorno a Stonehenge: la vegetazione più bassa all'inizio di
  aprile, più alta dalla fine di maggio. Contains modified Copernicus Sentinel data 2023 (contiene dati
  Copernicus Sentinel modificati, 2023).</figcaption>
</figure>

* La **mediana** dice come cambia la vegetazione dell'area nell'anno: risveglio, maturazione, raccolto,
  ricrescita. La **dispersione** dice quanto sono diversi i campi in quella data: una barra lunga, come
  ad aprile nel grafico, è il momento in cui colture, prato e terra nuda si separano - e quello in cui i
  segni hanno più probabilità di comparire.
* Il CSV per data ha una riga per data e indice: `date`, `acquisition`, `platform`, `index`,
  `clear_fraction`, `cells`, `median`, `p10`, `p25`, `p75`, `p90`, `mean`. Il CSV mensile mette insieme
  le celle serene delle date di ogni mese (`month`, `index`, `dates`, `cells` e le stesse statistiche).
  Le prime righe, che cominciano con `#`, portano la dicitura dei dati e le formule.
* Conta solo l'area, senza il suo margine, e solo le sue celle serene.

### Una data { #reading-date }

<figure markdown="span">
  ![Colori naturali e NDVI di una data](../img/plugins/sentinel2/ndvi-date.jpg)
  <figcaption>13 giugno 2023. (a) Colori naturali (B04, B03, B02), ogni banda stesa fra il suo 2° e
  98° percentile. (b) NDVI della stessa data: i campi differiscono fra loro molto più di quanto ogni
  segno differisca dentro di essi - la ragione dello sfondo che la mappa dei crop mark toglie. Contains
  modified Copernicus Sentinel data 2023 (contiene dati Copernicus Sentinel modificati,
  2023).</figcaption>
</figure>

### Le mappe dei crop mark { #reading-cropmarks }

* **z sotto -2** (rosso con la palette predefinita, nel pannello e in QGIS): vegetazione più debole del
  terreno intorno - muri e strade sepolti, superfici compattate, ma anche carrarecce, sentieri, chiazze sassose e bordi dei
  campi. **z sopra 2** (verde): più verde - fossati, fosse, ma anche siepi, avvallamenti umidi, una
  striscia seminata più tardi.
* **Leggi linee e forme**, non pixel isolati: un anello, un rettangolo, una retta che attraversa i
  confini dei campi. Un segno che segue un confine o le carreggiate dei trattori è agricolo.
* **Lo sfondo** decide che cosa risalta: 50 m vanno bene per elementi di decine di metri; recinti più
  grandi chiedono 100-200 m. Uno sfondo troppo piccolo si mangia gli elementi; uno troppo grande lascia
  nella mappa le differenze fra i campi.
* **Il periodo.** Una sola data serena nella settimana giusta può mostrare più della mediana di una
  stagione; la mediana di una stagione è più stabile. Guarda alcune date delle stesse settimane in anni
  diversi: un segno che torna negli anni secchi merita un sopralluogo.
* **Gli elementi moderni** sono i più forti: a Stonehenge le strade e i percorsi intorno alle pietre.
  Controlla l'ortofoto, le carte storiche e il terreno prima di leggere un segno come archeologia
  sepolta.

### Limiti { #limits }

* **10 m per pixel.** Gli elementi più stretti di un pixel ne cambiano il valore di poco: un muro di
  1 m in un pixel di 10 m ne è un decimo. Sentinel-2 trova gli elementi grandi e le settimane giuste; il
  drone e lo scavo fanno il resto.
* **Le nuvole.** La classificazione della scena perde qualche nuvola sottile e la sua ombra, che allora
  sembrano segni per una data. Un segno visto in una data sola non è ancora un segno.
* **L'area.** Al massimo 25 km di lato; un'area grande vuol dire più celle da leggere e calcolare, non
  più dati da scaricare per ogni acquisizione.
* **Un chunk locale** non si può cercare dalla finestra: georeferenzialo, oppure lavora dalla riga di
  comando con un riquadro in longitudine e latitudine.
* **Il catalogo.** Il plugin dipende da Earth Search e dalle immagini su AWS in linea; ciò che è stato
  letto resta nel progetto finché non lo togli dalla cache.

## File scritti { #files }

Tutto va nella cartella del chunk, accanto al file del progetto:
`<progetto>.ppg.files/chunk_<n>/sentinel2/`. L'area, la griglia, la ricerca, lo stato di ogni
acquisizione, le date e i prodotti, con le date non più in cache, restano nel progetto stesso.

* `scenes.json`: le acquisizioni dell'ultima ricerca, con gli indirizzi delle loro bande.
* `scenes/<acquisizione>.tif`: la finestra di ogni acquisizione letta - le sette bande e la
  classificazione della scena, con scala e offset - sulla griglia delle scene; circa 100 KB l'una. Sono
  i file che *Rimuovi dalla cache* e *Svuota la cache...* cancellano.
* `indices/NDVI.tif`, `NDRE.tif`, `NDMI.tif`: una banda per data serena, in ordine di tempo, con il nome
  dell'acquisizione; NaN dove la cella non era serena.
* `series.csv`, `series_monthly.csv`, `series.png`: la serie e il suo grafico.
* `products/<id>.tif`: compositi (mediana e numero di date) e mappe dei crop mark sulla griglia delle
  scene, in UTM; `products/<id>_chunk.tif` gli stessi su una griglia nel sistema di riferimento del
  chunk, allineata al suo DEM o alla sua ortofoto, con celle di circa 2,5 m, vuota dove manca il valore.

Ogni GeoTIFF porta la dicitura dei dati (tag `ATTRIBUTION` e `TIFFTAG_COPYRIGHT`); gli indici e i
prodotti portano anche le acquisizioni da cui sono stati fatti.

## Dati e licenze { #licences }

I dati Sentinel-2 sono liberi e aperti per qualsiasi uso secondo la *Legal Notice on the use of
Copernicus Sentinel Data and Service Information* dell'Unione europea. I dati modificati - indici,
compositi, mappe - devono dirlo: **Contains modified Copernicus Sentinel data** seguito dagli anni delle
acquisizioni usate (contiene dati Copernicus Sentinel modificati). Il plugin scrive la dicitura in ogni
GeoTIFF, in testa ai file CSV, ai piedi del grafico e nei suoi metadati, nel pannello e nel file di
testo di *Esporta tutto...*; tienila sotto ogni figura o carta che ne pubblichi, come in questa pagina.
I Cloud-Optimized GeoTIFF sono quelli della [collezione Sentinel-2 L2A del Registry of Open Data on
AWS](https://registry.opendata.aws/sentinel-2-l2a-cogs/), trovati attraverso Earth Search di Element
84.

Il plugin è distribuito con la licenza GPL-2.0-or-later, come il programma.
