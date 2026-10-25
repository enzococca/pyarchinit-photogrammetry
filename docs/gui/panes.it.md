# Pannelli

Attorno alle viste centrali la finestra ha i suoi pannelli, ognuno un riquadro a sé come in Metashape. Al primo
avvio sono in tre gruppi:

| Dove | Pannelli |
| --- | --- |
| a sinistra | [Area di lavoro](#area-di-lavoro), [Riferimenti](#riferimenti), [Layer](#layer) - sempre presenti |
| a destra | [PyArchInit](#pyarchinit), [Proxy US](#proxy-us), [Multispettrale e Termico](multispectral.md), i pannelli dei plugin |
| in basso | [Foto](#foto), [Sezioni](#sezioni), [Console Python](#console-python), [Log](#log) |

Le schede di un gruppo sono in alto, ognuna con l'icona del suo pannello; la barra del titolo sotto di esse
dice quale pannello è davanti.

## Spostare i pannelli

| Per... | fai così |
| --- | --- |
| **spostare** un pannello | trascinalo per la sua scheda (o tutto il gruppo per la barra del titolo) su un altro lato della finestra, sopra un altro pannello - diventano schede di un gruppo - o accanto, e il lato si divide |
| **staccarlo** | il primo pulsante della barra del titolo, o un doppio clic su di essa: il pannello diventa una finestra a sé (su un secondo schermo, per esempio); un doppio clic sul suo titolo, o trascinarlo sulla finestra, lo riaggancia |
| **chiuderlo** | il secondo pulsante della barra del titolo. *Area di lavoro*, *Riferimenti* e *Layer* ci sono sempre: si spostano e si staccano, ma non hanno il pulsante di chiusura |
| **riaprirlo** | *Vista > Pannelli* elenca tutti i pannelli con una spunta; spuntare un pannello chiuso lo riporta dove era, davanti |
| **passare da un pannello all'altro** | clic sulle schede, oppure *Vista > Pannelli > Pannello successivo* (++f6++) e *Pannello precedente* (++shift+f6++): girano tra i pannelli raggruppati con quello che ha la tastiera |
| **ricominciare** | *Vista > Pannelli > Ripristina la disposizione dei pannelli*: ogni pannello al suo posto iniziale, tutti aperti |

Il programma riporta anche da solo il pannello che ti mostra - *Riferimenti*, *Layer*, *Sezioni*, *Foto*,
*Multispettrale* quando si aggiungono foto con bande, la *Console Python* con ++ctrl+shift+p++ - chiuso o
staccato che sia.

La disposizione si salva quando la finestra si chiude e torna all'avvio successivo, compresi i pannelli dei
plugin. La disposizione di una versione precedente alla 1.3.4, quando i pannelli erano le schede di due
riquadri, non viene applicata: il primo avvio della 1.3.4 mostra quella iniziale.

## Area di lavoro

![Il pannello Area di lavoro](../img/gui/pane-workspace.png){ align=right width=330 }

Il progetto come lo mostra il Workspace di Metashape: il progetto, i suoi chunk e, sotto ogni chunk:

* il sistema di riferimento (doppio clic per cambiarlo);
* *Camere* (allineate / tutte; camera verde allineata, rossa non allineata), *Marker*, *Barre di scala*;
* *Punti di legame*, *Mappe di profondità* (quante, con quale qualità), *Nuvola densa*, *Modello 3D* e la sua
  texture;
* *DEM* e *Ortofoto* con dimensioni e risoluzione, i raster del rilievo, la copertura delle foto e la
  precisione del DEM;
* i prospetti, i prodotti delle riprese multispettrali e termiche, i risultati del calcolatore raster e i
  prodotti dei plugin.

Il **chunk attivo** - quello su cui lavorano gli strumenti - è in grassetto con *[attivo]*; un chunk
disattivato e una foto disattivata sono grigi.

Un clic seleziona (++ctrl++ + clic e ++shift++ + clic per più elementi); un doppio clic o ++enter++ rende attivo
un chunk, e apre una foto, un prodotto o una versione dopo aver reso attivo il suo chunk.

<div style="clear: both"></div>

**La barra sopra l'albero** ha solo icone (i suggerimenti dicono il nome; in un pannello stretto il » alla fine
tiene i pulsanti che non ci stanno):

| Pulsante | Cosa fa |
| --- | --- |
| *Aggiungi chunk* | un chunk vuoto con il nome che gli dai, che diventa quello attivo |
| *Aggiungi foto...*, *Aggiungi cartella...* | foto, o una cartella di foto, nel chunk attivo |
| *Rendi attivo* | il chunk selezionato diventa quello attivo (lo stesso fa un doppio clic) |
| *Attiva*, *Disattiva* | un chunk disattivato resta nel progetto, fuori dai passi batch e dai chunk che propongono *Allinea chunk* e *Unisci chunk*; una foto disattivata resta fuori dal prossimo allineamento e dalle prossime costruzioni |
| *Rinomina...* | il chunk o la versione selezionati (++f2++) |
| *Rimuovi* | ciò che è selezionato (++delete++ o ++backspace++): vedi sotto |

**Il clic destro** ha i comandi di ogni elemento:

| Su... | Comandi |
| --- | --- |
| un chunk | *Rendi attivo*, *Rinomina...*, *Attiva* o *Disattiva*, *Rimuovi chunk* |
| nuvola densa, modello, DEM, ortofoto | *Mostra*, *Conserva una copia come versione*, *Rinomina questa versione...*, *Rimuovi* |
| una versione conservata | *Mostra*, *Rendi attiva*, *Differenza con il DEM attivo*, *Esporta...*, *Rinomina...*, *Rimuovi* |
| una foto | *Apri*, *Attiva* o *Disattiva*, *Rimuovi la foto* |
| un marker | *Filtra le foto di ...*, *Rimuovi il marker* |
| *Camere*, *Marker*, *Barre di scala* | la rimozione di tutto ciò che contengono |
| più elementi | *Attiva*, *Disattiva*, *Rimuovi N elementi* |
| il progetto | *Aggiungi chunk* |

I comandi lavorano anche sui chunk che non sono quello attivo: rimuovere, rinominare, attivare e disattivare
agiscono sul chunk dell'elemento e lasciano com'è il chunk attivo, mentre mostrare un elemento e i comandi
delle sue versioni rendono prima attivo il suo chunk.

### Rimuovere

Si può rimuovere tutto ciò che l'albero elenca, tranne il sistema di riferimento: chunk interi (anche
l'ultimo: il progetto riceve un chunk vuoto), i prodotti, le mappe di profondità (spesso la maggior parte dello
spazio di un chunk), i raster del rilievo, i prodotti multispettrali e termici, le versioni conservate, i
prospetti, le foto (i loro file non vengono mai toccati; il prossimo allineamento le lascia fuori), i marker
(con le barre di scala che vi terminano) e le barre di scala.

* I punti di legame portano via l'allineamento (le foto non sono più allineate; nuvola densa, modello, DEM e
  ortofoto restano), il modello porta via la sua texture.
* Le versioni conservate di un prodotto restano quando il prodotto viene rimosso: si tolgono una per una con
  il clic destro.
* Prima una domanda dice cosa se ne va e cosa succede ai file; lì *Annulla* non cambia nulla.

!!! tip "Rimuovere si annulla"
    *Modifica > Annulla* rimette ciò che è stato rimosso com'era - un chunk al suo posto e di nuovo attivo se
    lo era, i file dove erano - e *Ripeti* lo toglie di nuovo. I file vengono spostati da parte dentro la
    cartella dei file del progetto (`<progetto>.ppg.files/.removed/`: stesso disco, istantaneo), mai
    cancellati subito. Un file fuori dalla cartella del chunk (un DEM esportato da Metashape a cui punta un
    progetto importato) non viene mai spostato né cancellato: esce solo dal progetto. Quando il progetto si
    chiude, o se ne apre un altro, i file seguono il progetto come salvato l'ultima volta: ciò che non elenca
    più viene cancellato, ciò che elenca ancora torna al suo posto. Dopo un arresto improvviso, la prossima
    apertura del progetto fa lo stesso.

## Riferimenti

![Il pannello Riferimenti](../img/gui/pane-reference.png){ align=right width=330 }

I marker e le camere con le loro coordinate, come il pannello Reference di Metashape:

* i marker con *Usa* e *Check*, coordinate e accuratezza (doppio clic per cambiarle), gli errori in metri e in
  pixel; le posizioni delle camere;
* le barre di scala sotto i marker ([barre di scala](photos-markers.md#barre-di-scala));
* gli RMSE dei punti di controllo e di check e delle barre.

Il suggerimento di un marker dice come è stato posizionato: triangolato da N foto, sulla superficie da una
foto, o aggiunto sul modello o sull'ortofoto.

<div style="clear: both"></div>

| Pulsante | Cosa fa |
| --- | --- |
| *Importa CSV...* | le coordinate dei marker |
| *Aggiorna* | la georeferenziazione dai punti di controllo |
| *Ottimizza camere* | con 3 o più punti di controllo entrano nel bundle adjustment e piegano il blocco; i check restano fuori; gli errori prima e dopo vanno nel log |
| *Precisione* | la precisione di camere, marker e punti di legame ([precisione](processing.md#precisione)) |
| *Barra di scala...*, *Importa barre...* | una distanza nota tra due marker, o un elenco da un CSV |
| *Coordinate stimate* | come *View Estimated* di Metashape: le colonne X, Y, Z mostrano le coordinate stimate invece di quelle di riferimento (attivo da solo quando nessun marker ha coordinate di riferimento) |

Il clic destro sui marker filtra le foto che li vedono, affina le loro bandierine blu, li unisce (un target
codificato e il GCP messo a mano sopra), ne sposta uno nella vista 3D, crea una barra di scala tra due di loro e
li elimina. Ogni tabella ha *Esporta tabella in Excel...* nel clic destro.

## Layer

![Il pannello Layer](../img/gui/pane-layers.png){ align=right width=330 }

Tutto ciò che si disegna su ortofoto e DEM è un layer: unità stratigrafiche, i poligoni US caricati da
PyArchInit, tracce di sezione, curve di livello, piante a quota, quote, marker, punti di misura, misure, pietre,
patch.

* La spunta mostra o nasconde un layer, il doppio clic ingrandisce su di esso (un layer o un singolo
  elemento), *Rimuovi* lo toglie; *Tutti* e *Nessuno* spuntano tutti i layer.
* In modalità *Sposta / interroga* un clic su un elemento mostra i suoi attributi (US, area, m2, quota di una
  curva...) qui e nella barra di stato.
* **Modificare gli attributi**: doppio clic su un valore sotto *Attributi* per cambiarlo (US, area, tipo,
  etichetta, qualunque campo libero; i campi calcolati come m2 e lunghezza sono in sola lettura).
* *Scheda US* apre la scheda PyArchInit dell'unità selezionata.

<div style="clear: both"></div>

!!! note "Unità di PyArchInit"
    Le unità caricate da PyArchInit tengono il loro `gid`: *Salva modifiche in PyArchInit* (compare sotto
    l'elenco quando c'è qualcosa da salvare) riscrive le modifiche in `pyunitastratigrafiche`. Le unità
    disegnate e le quote si scrivono con *Pubblica*; gli elementi scritti tengono il loro `gid`, così nulla
    viene scritto due volte.

## Foto

![Il pannello Foto](../img/gui/pane-photos.png)

Le miniature delle foto, con un segno per allineata o non allineata e, in basso, i marker su ogni foto (verde:
misurato, blu: da confermare; il suggerimento li elenca). Un doppio clic apre la foto nella vista centrale
*Foto*.

La barra sopra mostra il **filtro** in uso - *Foto di point 1*, *Foto con marker*, le foto di un punto o di
un'area - con *Mostra tutte* e *Precedente* / *Successiva*. I filtri vengono da:

* il clic destro sui marker in *Riferimenti* (*Filtra le foto di ...*);
* il clic destro sull'ortofoto in modalità *Sposta* (*Filtra le foto che vedono questo punto*, *Aggiungi un
  marker qui*);
* il menu di selezione di un poligono (*Filtra le foto che inquadrano quest'area*);
* *Foto del punto* nella vista 3D;
* un nuovo marker, che filtra le foto su di sé.

Il clic destro sulle miniature misura anche la **qualità delle immagini** (la nitidezza di ogni foto confrontata
con le più nitide; *sfocata* sotto 0,5), disattiva le foto sfocate, attiva o disattiva quelle selezionate,
imposta le loro maschere e il loro modello di obiettivo (vedi [Foto e marker](photos-markers.md)).

## Sezioni

Le sezioni del progetto: l'elenco, l'anteprima del disegno SVG (adattata al pannello, con linee e scritte
ingrossate perché un disegno lungo metri sulla carta resti leggibile), e i pulsanti:

| Pulsante | Cosa fa |
| --- | --- |
| *Zoom sulla traccia* | la traccia sull'ortofoto |
| *Apri cartella* | il profilo DXF e il DXF 3D in `sezioni/` |
| *Prospetto* | l'ortofoto del muro sul piano della traccia ([prospetti](processing.md#prospetti)) |
| *Pubblica in PyArchInit* | la traccia in `pyarchinit_sezioni` |
| *Elimina* | la sezione e i suoi disegni |

Le sezioni si salvano in `sezioni/sezioni.json` accanto al progetto e tornano quando lo si riapre. Come si
disegnano è in [Disegno e misure](drawing.md#sezioni).

## Console Python

![La console Python](../img/gui/pane-console.png)

Una console Python con il progetto a portata di mano: `doc` (il progetto), `chunk` (il chunk attivo), `app`,
`ppg` (la libreria), `np` e `window`. Ciò che cambia si vede nella finestra. ++ctrl+shift+p++ la porta davanti.

## Log

![Il pannello Log](../img/gui/pane-log.png)

Ora, livello e messaggio di tutto ciò che è successo. *Mostra* filtra (tutto, avvisi ed errori, errori), la
casella di ricerca trova una parola, e i pulsanti *Copia*, *Salva...*, *Cartella log* e *Pulisci*. Il traceback
di un errore è nel suggerimento della sua riga e nel file salvato. Vedi [Errori e aggiornamenti](help.md).

## PyArchInit

![Il pannello PyArchInit](../img/gui/pane-pyarchinit.png){ align=right width=280 }

Il database dello scavo:

* **Sito del progetto**: lo scavo a cui appartengono le unità e le quote del progetto;
* *Connetti...*, *Database demo*, *Nuovo sito...*, *Nuova US...*;
* l'albero dei siti con le loro unità stratigrafiche e la documentazione: un doppio clic su una US apre la sua
  scheda PyArchInit;
* i **media** della US (o del sito) selezionata, dalla `media_to_entity_table` di PyArchInit: un doppio clic
  apre il file (l'originale, altrimenti la sua copia ridotta) con il programma del sistema.

Cosa si può fare con PyArchInit è in [Strumenti per l'archeologia](archaeology.md#pyarchinit).

<div style="clear: both"></div>

## Proxy US

![Il pannello Proxy US](../img/gui/pane-proxies.png){ align=right width=280 }

I solidi 3D delle unità (*Strumenti > Proxy 3D delle US...*), con volume, area in pianta e quote:

* *Crea proxy...*, *Vedi in 3D* (colorati sopra il modello);
* *Esporta (Extended Matrix)*: un OBJ con un oggetto per unità (`US12`, `USM3`) e lo spostamento delle
  coordinate;
* *Pubblica in PyArchInit* (un media 3D legato a ogni US), *Quote nella scheda US*, *Scheda US*, *Elimina*.

Come si fanno i solidi e come seguono le loro unità è in
[Strumenti per l'archeologia](archaeology.md#proxy-3d-delle-us).

<div style="clear: both"></div>
