# Blender ed EMtools

Il plugin *Blender ed EMtools* porta i proxy 3D delle unità stratigrafiche e il rilievo fotogrammetrico
in [Blender](https://www.blender.org) e nell'Extended Matrix di
[EMtools](https://github.com/zalmoxes-laran/EM-blender-tools), l'add-on di Blender dell'Extended
Matrix (E. Demetrescu, CNR-ISPC), e li tiene aggiornati mentre il lavoro va avanti, come il
collegamento con QGIS tiene aggiornati i disegni in QGIS. La matrice resta dove viene scritta:
l'em.json del sito, esportato da PyArchInit o scritto da EMStudio ed EMtools. Il plugin lo legge, dà a
ogni proxy il nome della sua unità nella matrice (il nome con cui EMtools abbina un proxy) e scrive i
file dove un progetto EM li tiene.

Questa pagina descrive la versione 0.1.0 del plugin, che richiede pyarchinit-photogrammetry 1.3.4 o
successiva. README e codice sorgente sono nel
[repository dei plugin](https://github.com/enzococca/pyarchinit-photogrammetry-plugins/tree/main/plugins/blender-em).

!!! note "Extended Matrix, EMtools, em.json"
    L'[Extended Matrix](https://www.extendedmatrix.org) è il grafo della stratigrafia di un sito: unità,
    rapporti, epoche, fonti e ricostruzioni. Il suo file di lavoro è l'**em.json** (da s3dgraphy 1.6):
    un file con uno o più grafi, letto da EMStudio, EMtools 1.6 ed Heriverse. Il GraphML (yEd) resta la
    via d'ingresso per le matrici disegnate a mano. In Blender un *proxy* è il solido di un'unità, un
    oggetto con il nome del suo nodo: EMtools lo colora per epoca, lo mostra e lo nasconde con la
    matrice e lo esporta per il web.

## In breve

1. Costruisci i proxy delle unità: *Strumenti > Proxy 3D delle US...* (le US disegnate, o quelle di un
   sito PyArchInit, tra il DEM e una base).
2. In PyArchInit (QGIS) esporta la matrice del sito: *Extended Matrix → Esporta progetto EM per
   EMStudio…* scrive `<sito>.em.json` nella cartella `EM` di un progetto EM, l'albero in cui il plugin
   scrive (*Extended Matrix → Esporta sito in em.json…* scrive il file da solo).
3. Nel programma, *Plugin > Blender ed EMtools > Collega l'em.json del sito...*: il pannello elenca le
   unità della matrice e il proxy abbinato a ciascuna.
4. *Apri in Blender (aggiornato dal vivo)*: i proxy vanno in `EM/proxies`, il rilievo in `RB`, e
   Blender si apre con loro; con EMtools 1.6 l'em.json è caricato e i proxy sono abbinati alle unità.
5. Continua a lavorare nel programma (ricostruisci un proxy, togline un altro, ricostruisci il
   modello): Blender segue entro un paio di secondi.
6. In Blender avvia la sincronizzazione di EMtools (*Sync*) e spunta *Sincronizza con EMtools* nel
   pannello: un proxy scelto in Blender seleziona la sua unità nel pannello, un'unità scelta nel
   pannello seleziona e inquadra il suo proxy in Blender.

## Che cosa fa

| Passo | Che cosa dà | Utile per |
|---|---|---|
| Collega l'em.json | le unità della matrice con la loro epoca, e il proxy del chunk abbinato a ciascuna; i proxy che la matrice non ha, e quelli ambigui, detti | controllare che ogni proxy abbia la sua unità prima di andare in Blender |
| Esporta nel progetto EM | `EM/proxies/<unità>.glb` per ogni proxy, con il nome del suo nodo; `RB/<chunk>/<chunk>.glb` il rilievo con i suoi colori o la texture; `ppg_scene.json` | EMtools, Heriverse, qualsiasi visualizzatore glTF; un progetto EM che tiene la sua geometria anche fuori da Blender |
| Apri in Blender | Blender sul progetto EM con lo script dal vivo del plugin; l'em.json caricato in EMtools 1.6 | modellare la ricostruzione (le unità virtuali) sui proxy reali e sul rilievo; render; il flusso di lavoro EM |
| Aggiornamento dal vivo | ogni proxy ricostruito, ogni modello cambiato riscritti, e ricaricati da Blender | lavorare nei due programmi insieme senza esportare a mano |
| Sincronizza con EMtools | la selezione condivisa con EMtools 1.6 | leggere la scheda di un'unità scelta in 3D; trovare in 3D l'unità della matrice |

Che cosa resta altrove: la matrice si modifica in EMStudio, in EMtools o nelle schede di PyArchInit; le
ricostruzioni si modellano in Blender; i proxy li costruisce il programma (*Strumenti > Proxy 3D delle
US...*), come solidi tra due superfici con il loro volume.

## Che cosa serve prima { #before }

* **Il plugin**, installato dal programma: vedi [Guide ai plugin](guides.md#install).
* **I proxy delle unità nel chunk**, fatti con *Strumenti > Proxy 3D delle US...* (vedi
  [Interfaccia](../gui.md)), e/o **un modello**. Senza nessuno dei due non c'è niente da mandare.
* **L'em.json del sito**, consigliato. Lo scrive PyArchInit (*Extended Matrix → Esporta sito in em.json…*, s3dgraphy 1.6), come
  EMStudio ed EMtools 1.6. Senza, i proxy tengono i nomi del programma (`US12`, `USM3`) ed EMtools li
  abbina solo se la sua matrice usa quei nomi.
* **Blender** 4.2 o successivo (provato con la 5.2), cercato dove è installato o scelto con *Scegli il
  programma Blender...* (o con la variabile `PPG_BLENDER`).
* **EMtools** in Blender, facoltativo. Con la **1.6** l'em.json è caricato da solo e la
  sincronizzazione dal vivo funziona; con la **1.5** (solo GraphML) i file e lo script dal vivo
  funzionano lo stesso, e la matrice si carica in EMtools a mano.
* **Un database PyArchInit collegato** nel programma, facoltativo: dà a ogni proxy il `node_uuid` della
  sua scheda US, l'id che PyArchInit dà all'unità nell'em.json, così l'abbinamento non dipende dai
  nomi.

## I dati { #data }

### L'em.json { #emjson }

Il plugin legge l'em.json, non lo scrive mai. Del file usa:

* le **unità** (i nodi stratigrafici: US, USVs, USVn, SF, VSF, USD, TSU, serie...): il loro id, il nome
  (PyArchInit scrive `area.tipoNUMERO`, `1.US12`) e US, area e tipo della scheda che PyArchInit
  conserva con loro;
* la loro **epoca** (la prima), il cui colore diventa il colore del proxy;
* lo **spostamento** del progetto (il suo nodo di georeferenziazione), quando c'è.

Un file con più grafi (PyArchInit ne scrive uno per saggio, se richiesto) è letto tutto; lo scaffale
degli asset e il corpus della documentazione sono lasciati fuori. È letto anche un file nella forma di
prima del contenitore (un grafo solo).

### L'abbinamento dei proxy con le unità { #pairing }

Ogni proxy del chunk è abbinato a una sola unità, in quest'ordine:

1. con il **`node_uuid` della sua scheda US**, quando un database PyArchInit è collegato: l'id
   dell'unità nell'em.json di PyArchInit;
2. con la **scheda**: lo stesso numero di US e lo stesso tipo (`USM` non è `US`), e la stessa area
   quando entrambi ne hanno una;
3. con il **nome**: `US12`, `1.US12`, o un nome di nodo che finisce con `.US12`.

Un'unità va a un solo proxy. Due unità possibili per un proxy non sono una scommessa: il pannello
elenca i candidati, e il proxy tiene il nome del programma finché l'area del proxy o delle schede non
dice quale. I proxy senza unità sono scritti con il nome del programma e segnati *non nella matrice*.

### Il progetto EM { #project }

```
<progetto>/EM/<sito>.em.json         la matrice (di PyArchInit, di EMStudio)
<progetto>/EM/proxies/<unità>.glb    un proxy per unità, con il nome del suo nodo
<progetto>/RB/<chunk>/<chunk>.glb    il rilievo (reality-based)
<progetto>/ppg_scene.json            il manifest che lo script dal vivo segue
```

È l'albero standard di un progetto EM (s3dgraphy, EMtools 1.6). Quando l'em.json sta nella cartella
`EM` di un progetto, i file vanno in quel progetto; un em.json altrove li ha accanto a sé (`proxies/`,
`RB/`), come fa EMtools. Senza em.json il plugin chiede una volta la cartella del progetto e la
conserva con il chunk.

**Coordinate.** glTF ha la Y in alto; Blender lo riporta con la Z in alto all'importazione. Le
coordinate sono **spostate di un numero intero di metri**, perché un float32 tiene un quarto di metro a
quattromila chilometri: dello spostamento dell'em.json quando c'è, altrimenti dell'angolo più basso di
ciò che si scrive, arrotondato per difetto (per esempio `300000, 4500000, 99`). Una volta scelto, lo
spostamento resta per il progetto, così un aggiornamento non sposta mai ciò che Blender mostra; la scena
lo conserva (`ppg_shift`, e i `BL_x_shift`... che EMtools legge, quando non sono ancora impostati).

**File di altri programmi.** Un file è riscritto solo quando cambia ciò da cui viene (il proxy, il
modello, lo spostamento, il nome, il colore dell'epoca), e **mai sopra un file che un altro programma
ha scritto o cambiato** nello stesso posto: EMtools scrive in `EM/proxies` anche i proxy modellati in
Blender. Un file così resta com'è e il resoconto lo dice. Il file di un proxy tolto dal chunk è
cancellato solo se è ancora quello scritto dal plugin.

## Passo per passo nel programma { #program }

Il pannello *Blender ed EMtools* (*Plugin > Blender ed EMtools > Pannello Blender ed EMtools*, a destra)
mostra l'em.json collegato, il progetto EM, Blender e lo stato della sincronizzazione, i pulsanti e la
tabella delle unità: unità, tipo, epoca, proxy (il suo volume). La voce *Progetto EM (Blender)* sotto il
chunk nell'area di lavoro apre il pannello e ha le stesse azioni nel suo menu.

### 1. Collega l'em.json { #link }

*Collega em.json...*: il `.em.json` del sito. Il pannello elenca le unità della matrice con il proxy
abbinato a ciascuna, poi i proxy che la matrice non ha; un messaggio li nomina. Il collegamento è
conservato con il chunk (anche relativo al progetto, così un progetto spostato insieme al suo progetto
EM lo ritrova), e l'em.json è riletto ogni volta che cambia: esportato di nuovo da PyArchInit, salvato
da EMStudio.

### 2. Esporta nel progetto EM { #export }

*Esporta nel progetto EM* scrive i proxy e il rilievo e riporta ciò che ha scritto, ciò che era
invariato, ciò che ha lasciato com'era (file di un altro programma), i proxy non nella matrice e lo
spostamento. Il rilievo è il modello attivo del chunk, con la sua texture quando ce l'ha.

### 3. Apri in Blender { #blender }

*Apri in Blender (aggiornato dal vivo)* esporta, poi avvia Blender sul progetto: il `.blend` più recente
nella cartella del progetto quando c'è, altrimenti una scena nuova (salvala lì, e si aprirà la volta
dopo). In Blender:

* i proxy sono nella collezione **PPG Proxy**, il rilievo in **PPG Rilievo**; ogni oggetto conserva
  unità, area, tipo, epoca, id del nodo e volume come proprietà personalizzate (*Proprietà oggetto >
  Proprietà personalizzate*);
* la scheda **PPG** della barra laterale della vista 3D (tasto N) mostra il progetto, la revisione del
  manifest, *Dal vivo: sì/no*, *Aggiorna ora*, *Carica l'em.json in EMtools* e gli oggetti cambiati in
  Blender;
* con EMtools 1.6, in una scena nuova, l'em.json è caricato in EMtools da solo; i proxy sono abbinati
  alle unità per nome.

**Ciò che cambi in Blender non è mai sovrascritto.** Un oggetto i cui vertici non sono più quelli
importati dal plugin (un proxy modellato a mano) è *staccato*: la nuova versione del programma non lo
sostituisce, e il pannello lo elenca. Il materiale dato da EMtools (il colore dell'epoca) resta quando
la mesh di un proxy è sostituita.

### 4. Aggiornamento dal vivo { #live }

*Aggiornamento dal vivo* (acceso anche da *Apri in Blender*): ogni due secondi il programma guarda i
proxy, il modello e l'em.json del chunk attivo; quando qualcosa è cambiato riscrive il progetto EM in
background (senza finestra di avanzamento, e non mentre gira un'elaborazione) e Blender, che legge il
manifest ogni secondo, ricarica ciò che è cambiato. Un proxy ricostruito sostituisce la mesh del suo
oggetto, un proxy tolto scompare, il rilievo di un modello ricostruito sostituisce quello vecchio.

### 5. Sincronizza con EMtools { #sync }

EMtools 1.6 *ospita* la sincronizzazione dal vivo dell'Extended Matrix, quella che usa EMStudio: in
Blender avviala dal pannello di EMtools (*Sync*). Poi spunta **Sincronizza con EMtools** nel pannello.
Il programma si collega a Blender (`127.0.0.1`, porta 8788; un'altra porta con l'impostazione `port` del
plugin), dice quale matrice ha aperto (EMtools avverte quando i due non sono sullo stesso studio) e
chiede il grafo di EMtools: senza un em.json collegato, il pannello e i nomi dei proxy seguono quel
grafo.

* **Un proxy scelto in Blender** seleziona la sua unità nella tabella del pannello, con tipo, epoca e le
  misure del suo proxy (volume, area, quote) sotto la tabella.
* **Un'unità scelta nel pannello** seleziona e inquadra il suo proxy in Blender. EMtools deve accettare
  la selezione (il suo default: *Accept: everything* o *selection*).

Il programma manda solo la selezione: non manda mai modifiche al grafo né comandi, che sono di EMtools
e di EMStudio. Quando Blender si chiude e si riapre, il programma si ricollega da solo.

## Tempo reale: come funziona { #realtime }

Il collegamento con Blender funziona come il [collegamento con QGIS](../integrations.md#qgis-dal-vivo):
file che l'altro programma rilegge, più un canale per ciò che deve essere immediato.

| | Collegamento con QGIS | Blender ed EMtools |
|---|---|---|
| Che cosa viaggia | i disegni, in un GeoPackage | i proxy e il rilievo, in file glTF del progetto EM |
| Chi guarda | QGIS ridisegna i layer ogni secondo | lo script del plugin in Blender legge il manifest ogni secondo |
| Quando il programma scrive | a ogni modifica dei disegni | quando cambiano proxy, modello o em.json (al massimo ogni due secondi) |
| Canale immediato | - | il WebSocket di EMtools 1.6: la selezione, nei due sensi |
| Ciò che l'altro lato cambia | - | conservato: oggetti staccati, materiali di EMtools, file scritti da EMtools |

## Dalla riga di comando { #cli }

```bash
# le unità dell'em.json e i proxy abbinati
ppg plugin blender-em status scavo.ppg --emjson Scavo/EM/Scavo.em.json

# il progetto EM dell'em.json (EM/proxies, RB, ppg_scene.json)
ppg plugin blender-em export scavo.ppg --emjson Scavo/EM/Scavo.em.json

# senza em.json, in una cartella; solo i proxy
ppg plugin blender-em export scavo.ppg --out Scavo --no-survey

# esporta e apri Blender (l'em.json collegato al chunk quando non è indicato)
ppg plugin blender-em open scavo.ppg --blender /Applications/Blender.app/Contents/MacOS/Blender

# i Blender trovati
ppg plugin blender-em blender
```

`--chunk ETICHETTA` lavora su un chunk diverso da quello attivo. Lo script che gira in Blender si può
anche avviare a mano: `blender --python blender_live.py -- ppg_scene.json` (il file è nella cartella del
plugin), o dal pannello in Blender, *Scegli il manifest...*.

## Leggere i risultati { #reading }

* **Non nella matrice.** Un proxy senza unità tiene il nome del programma: o l'unità manca
  dall'em.json (esporta di nuovo l'em.json da PyArchInit dopo averne creato la scheda), o numero, tipo
  o area sono diversi.
* **Più unità possibili.** Lo stesso numero di US in due aree, e un proxy senza area: dai l'area
  all'unità disegnata (o costruisci i proxy dal sito PyArchInit), o collega il database per il
  `node_uuid`.
* **Lasciati com'erano.** Un file con lo stesso nome scritto da EMtools: cancellalo nel progetto EM se il
  proxy del programma deve sostituirlo.
* **Staccati in Blender.** Il proxy è stato modellato in Blender: la versione del programma non è
  applicata. Per riaverla, cancella l'oggetto in Blender e fai *Aggiorna ora* nel pannello PPG: l'oggetto
  è importato di nuovo.

### Limiti { #limits }

* **Il programma non rilegge ciò che si modella in Blender**: le ricostruzioni e i proxy modellati lì
  restano in Blender e nel progetto di EMtools.
* **Un chunk**: quello attivo, con i suoi proxy e il suo modello.
* **Il rilievo in un solo file**: un modello grande fa un glTF grande; decimalo prima (il plugin *Mesh
  (MeshLab)*) quando Blender deve restare leggero.
* **La sincronizzazione richiede EMtools 1.6** (una versione di sviluppo a ottobre 2026); EMtools 1.5
  legge GraphML e non ha la sincronizzazione: lì la matrice si carica a mano.

## File scritti { #files }

* `EM/proxies/<unità>.glb` (o `proxies/` accanto all'em.json): un glTF binario per proxy, un nodo e una
  mesh con il nome dell'unità, un materiale nel colore della sua epoca; nei suoi extras unità, area,
  tipo, id del nodo, volume, sistema di riferimento e spostamento.
* `RB/<chunk>/<chunk>.glb`: il rilievo, con i suoi colori o la sua texture.
* `ppg_scene.json` nella cartella del progetto: il manifest (revisione, sistema di riferimento,
  spostamento, em.json, gli oggetti con i loro file, le loro sorgenti e i loro checksum), letto dallo
  script in Blender.
* Il progetto conserva con il chunk l'em.json collegato e la cartella del progetto EM.
* Le impostazioni del plugin (Blender, aggiornamento dal vivo, sincronizzazione, porta) nella sua
  cartella del programma (vedi [Plugin](../plugins.md)).
