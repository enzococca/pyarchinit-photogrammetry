# Integrazioni: QGIS, RustGIS, Metashape, WebODM, Photo3D

## QGIS, dal vivo

*QGIS > Apri in QGIS (collegamento dal vivo)* (o *QGIS* nel gruppo *Layer e uscite* della colonna a sinistra della vista 2D)
scrive un progetto QGIS accanto al `.ppg` (`<nome>_qgis/<nome>_ppg.qgs`) e lo apre:

* raster: l'ortofoto, il DEM (ombreggiatura, all'inizio nascosta) e i valori del DEM;
* vettori, nel gruppo *Disegni (aggiornati dal vivo)*: un layer per ogni layer del programma che ha disegni
  (vedi [sotto](#un-layer-in-qgis-per-ogni-layer)), letti dalle tabelle di un GeoPackage,
  `disegni_ppg.gpkg`: `unita` (poligoni delle unità con le colonne di `pyunitastratigrafiche`,
  etichetta = `etichetta`), `poligoni` (zone, pietre, patch, i poligoni dei plugin), `sezioni`
  (tracce di sezione), `curve` (curve di livello, etichetta = quota), `linee` (altre linee), `punti`
  (punti di misura e altri punti) e `quote` (quote con le colonne di `pyarchinit_quote`, etichetta
  = `quota_q`).

Il GeoPackage viene aggiornato 0,4 s dopo ogni modifica nell'interfaccia (disegno nuovo, layer
tolto, attributo modificato), sul posto e in una transazione, e il progetto chiede a QGIS di
ridisegnare quei layer ogni secondo: QGIS rilegge un GeoPackage a ogni ridisegno, così quello che
disegni qui compare in QGIS da solo, senza *Aggiorna*.

**In QGIS vanno solo i layer spuntati.** Un layer a cui si toglie la spunta nel pannello *Layer* (anche con *Nessuno*,
o con un passo di *Modifica > Annulla*) è nascosto anche in QGIS: entro gli stessi 0,4 s i suoi elementi escono dalle
tabelle del GeoPackage, e tornano - le stesse righe, con le modifiche fatte a mano - quando il layer viene spuntato di
nuovo. Nascondere un layer non cancella nulla: i suoi disegni e le modifiche tenute con il chunk restano nel progetto,
e niente viene riletto come eliminato in QGIS. Ciò che è stato salvato in QGIS su un layer un attimo prima di
nasconderlo viene comunque ripreso, e un elemento disegnato in QGIS su un layer nascosto (il suo nome nella colonna
`layer`) entra in quel layer qui e resta nascosto con lui. Lo stesso vale per i layer dei disegni liberi in 3D
(`disegni_3d_*`) e per RustGIS, che legge lo stesso file. Il layer di QGIS resta nel progetto, vuoto finché il suo layer
è nascosto qui; i disegni su prospetti e sezioni (`prospetto_*`) vengono scritti sempre. **Le spunte sono salvate con
il progetto** (quelle della pianta con il chunk, in `layers_hidden`; quelle dei disegni liberi con i loro layer):
riaperto, mostra - e invia - i layer che erano spuntati. Un layer che un plugin ridisegna, o uno nuovo con lo stesso
nome, resta senza spunta finché non la si rimette. Le spunte dei layer di un prospetto mostrato nella vista 2D sono
della vista, e non vengono salvate.

### Un layer in QGIS per ogni layer

Il progetto QGIS ha **un layer per ogni layer del programma che ha disegni**, con il nome che ha nel pannello
*Layer* - *Unita' stratigrafiche*, *Quote*, *PhotoCAD: Ortofoto 1*, *Disegni 3D: Muri* - così ognuno si può vestire,
nascondere, ordinare e modificare per conto suo. Ognuno è una vista della sua tabella filtrata sulla colonna `layer`
(`"layer" = 'PhotoCAD: Ortofoto 1'`: *Proprietà layer > Sorgente > Filtro elementi del provider*), e ha il nome del
layer come valore predefinito di quella colonna: ciò che vi si disegna in QGIS viene salvato con quel nome, e torna
in quel layer qui.

* Un elemento viene scritto nella tabella della sua geometria. Un layer che ha linee e poligoni - un disegno di
  PhotoCAD con polilinee aperte e chiuse - fa due layer in QGIS, *PhotoCAD: Rilievo - linee* e *PhotoCAD: Rilievo -
  poligoni* (e *- punti*): in QGIS un layer ha un solo tipo di geometria.
* Un layer senza disegni non ne fa: un progetto senza sezioni o curve di livello non ha in QGIS un *Sezioni* o un
  *Curve di livello* vuoto. Lo stesso vale per le tabelle dei prospetti (`prospetto_*`).
* Le righe senza un nome in `layer` (un elemento salvato in QGIS senza, in un progetto scritto fino
  alla 1.7.8) stanno in un layer con
  il nome della loro tabella, *Linee (ppg)*, *Poligoni (ppg)*, *Punti (ppg)*.

**Che cosa si aggiorna da solo e che cosa chiede *Apri in QGIS*.** I layer che il progetto QGIS ha si aggiornano dal
vivo, come prima: elementi disegnati, cambiati o eliminati qui, e un layer a cui si toglie la spunta (si svuota) e la
si rimette (si riempie di nuovo). L'elenco dei layer viene scritto con il progetto: un layer che ha avuto i suoi primi
disegni, che è stato rinominato, o che era senza spunta quando il progetto è stato scritto non c'è. Il programma lo
dice una volta, nel log e nella barra di stato (*QGIS: il progetto aperto non mostra ancora «...»*): scegli di nuovo
*QGIS > Apri in QGIS*, che aggiunge al progetto i layer di adesso ([gli stili fatti a mano
restano](#gli-stili-fatti-a-mano-in-qgis-restano)). La prima unità, sezione o quota di un progetto è nello stesso
caso: disegnala qui, poi *Apri in QGIS*.

Un progetto QGIS scritto dalla 1.7.8 o prima - sette layer, uno per tabella - continua a funzionare com'era, nei due
sensi, finché *Apri in QGIS* non lo aggiorna (i suoi layer di tabelle intere sono sostituiti da un layer per layer). Il GeoPackage della 1.7.8 teneva ogni elemento di un layer nella tabella
del primo disegnato (una linea fra i poligoni, che QGIS non poteva disegnare): alla prima scrittura le righe vengono
spostate nella tabella della loro geometria, con le modifiche fatte a mano, e niente viene riletto come eliminato o
disegnato in QGIS. RustGIS legge lo stesso file per tabelle, come prima.

### Gli stili fatti a mano in QGIS restano

Simbologia, etichette, opacità e gli altri layer del progetto QGIS sono dell'utente: *Apri in QGIS* non li riscrive
mai.

* Un progetto che mostra già tutti i layer viene **aperto com'è**: non si scrive niente.
* Un progetto a cui mancano layer (un layer nuovo, uno rinominato, un prodotto nuovo), o che mostra layer che il
  programma non ha più, viene **aggiornato sul posto** dal Python di QGIS: i layer mancanti aggiunti, quelli del
  programma che non ci sono più tolti, i raster puntati di nuovo al loro file. I layer dell'utente (una mappa di base,
  il catasto), i gruppi, i layout e gli stili restano come sono. Salva il progetto in QGIS prima di scegliere *Apri in
  QGIS*: un aggiornamento scritto mentre QGIS ha il progetto aperto si perde quando QGIS lo salva di nuovo.
* Prima di toccare il progetto, lo stile di ogni layer del programma cambiato in QGIS da quando il programma lo ha
  scritto l'ultima volta viene conservato come file `.qml` in `<nome>_qgis/<nome>_ppg_stili/` (con `stili.json`). Un
  layer scritto di nuovo - il progetto cancellato, o il layer tolto e tornato - prende quello stile invece di quello
  del programma.
* Il GeoPackage dal vivo ha nella tabella `layer_styles` lo stile predefinito del programma per ogni tabella, scritto
  una volta sola: QGIS lo usa per un layer del GeoPackage aggiunto a mano. Uno stile predefinito salvato lì in QGIS
  (*Proprietà layer > Stile > Salva come predefinito > Database della sorgente dati*) non viene mai sovrascritto, e i
  layer nuovi di quella tabella lo prendono.
* *QGIS > Ripristina gli stili predefiniti in QGIS...* dimentica gli stili conservati e ridà ai layer del programma
  nel progetto lo stile del programma; gli altri layer restano come sono.

Le righe delle tabelle sono aggiornate sul posto per identità: un elemento tiene il suo `fid` finché esiste, e una
tabella viene rifatta solo quando cambia il sistema di riferimento del chunk.

### Nei due sensi

Con *QGIS > Collegamento dal vivo con QGIS (nei due sensi)* acceso (lo accende *Apri in QGIS*, e
resta acceso per un progetto già collegato), anche ciò che si salva in QGIS torna qui. Modifica in
QGIS i layer *Unita' stratigrafiche*, *Quote*, *Sezioni*, quelli dei disegni liberi (*Disegni 3D: ...*) o quelli dei
plugin come un layer qualsiasi (*Attiva modifiche*, disegna, sposta i vertici, cambia gli attributi nella tabella
attributi, elimina) e salva le modifiche: entro un secondo la finestra le ha.

* **Unità**: il contorno e le colonne di `pyunitastratigrafiche` - `us_s`, `area_s`,
  `unita_tipo_s`, `scavo_s`, `stratigraph_index_us`, `tipo_us_s`, `disegnatore`, `data`... - e le
  altre colonne (anche quelle aggiunte in QGIS) come attributi dell'unità. Un'unità di più
  poligoni è una riga per parte (`ppg_parte`). Un'unità disegnata in QGIS diventa un'unità qui, nel
  layer *Unita' stratigrafiche*, con il sito del progetto quando `scavo_s` è vuoto. Un poligono
  caricato da PyArchInit e cambiato in QGIS aspetta, come quando lo si cambia qui, *Salva
  modifiche in PyArchInit*.
* **Quote**: il punto e le colonne di `pyarchinit_quote` (`quota_q`, `us_q`...).
* **Sezioni**: il nome (`etichetta`) e la traccia (il primo e l'ultimo vertice). Una sezione con
  la traccia spostata, o disegnata in QGIS, viene tagliata di nuovo in background con le opzioni
  del chunk, quando c'è il modello (o la nuvola densa); i disegni di una sezione eliminata in QGIS
  restano in `sezioni/`.
* **Disegni liberi** (un layer *Disegni 3D: <layer>* per ognuno dei loro layer, due o tre - *- linee*, *-
  poligoni*, *- punti* - quando ha più tipi; tabelle `disegni_3d_linee`,
  `disegni_3d_poligoni`, `disegni_3d_punti`): la geometria con le sue quote, il layer (`layer`), il
  nome (`name`), il colore (`colour`, `#rrggbb`) e gli attributi, ognuno nella sua colonna. Una
  colonna aggiunta in QGIS è un attributo nuovo, una svuotata toglie l'attributo; viene letto anche
  il JSON della colonna `attributes`, e una colonna modificata accanto vince. Un elemento disegnato
  in QGIS diventa un disegno libero del layer scritto in `layer` (creato se non esiste; *Disegni*
  se è vuoto); un elemento eliminato lì viene tolto qui. **Quote**: un vertice tiene la Z che ha in
  QGIS. Un vertice che non ne ha - un layer modificato senza Z, o un vertice aggiunto in QGIS, che
  lo scrive con Z 0 o senza alcun numero (NaN: QGIS 3.42, un vertice inserito in una linea con le
  quote) - prende la quota del DEM sotto di sé o, dove il DEM non ne ha, la quota fra i
  suoi vicini lungo il disegno; il log dice quante quote vengono dal DEM. Con un DEM da rifare e
  nessun'altra quota a cui appoggiarsi, l'elemento non viene ripreso (una riga nel log), e il
  prossimo cambiamento fatto qui gli riscrive sopra il disegno del programma: rifai prima il DEM.
  Di un elemento di più parti resta la prima, un poligono perde i fori: il log
  lo dice. Le tre tabelle esistono da quando il chunk ha un disegno libero (fanne uno qui, poi
  *Apri in QGIS*).
* **Gli altri disegni della pianta** (un layer per ognuno, tabelle
  `poligoni`, `linee`, `punti`): i layer che ha solo la vista 2D - quelli disegnati da un plugin
  (*PhotoCAD: Ortofoto 1*...), quelli disegnati in QGIS. Un elemento spostato, cambiato nelle sue
  colonne o eliminato in QGIS viene spostato, cambiato o eliminato nel suo layer qui (lo dice la
  colonna `layer`); le colonne del plugin restano come sono. Un elemento disegnato in QGIS va nel
  layer scritto in `layer`, creato se non esiste (*Disegni da QGIS* se è vuoto). Di un elemento di
  più parti resta la prima, un poligono perde i fori, e una geometria che non è della
  sua tabella (un punto salvato nella tabella delle linee) non viene ripresa: il log lo dice. Una linea disegnata nel
  layer delle linee di un layer cominciato con un poligono, o viceversa, viene ripresa come ogni altra. Queste
  modifiche restano con il chunk (`plan_edits`) e si salvano con il progetto: quando il plugin
  ridisegna il suo layer dai suoi risultati - dopo *Salva*, un passo, un altro raster mostrato -
  vi vengono rimesse, così un elemento eliminato qui o in QGIS resta eliminato. Un attributo di
  un elemento di questi layer cambiato qui, nella tabella del pannello *Layer* o con *Modifica
  etichetta...*, resta allo stesso modo e va in QGIS nella riga dell'elemento. Una nuova
  esecuzione del plugin che fa altri elementi (una nuova conversione) riparte dai suoi nuovi
  risultati: al *Salva* successivo le modifiche fatte sugli elementi che il layer non disegna
  più escono dal progetto (il log dice quante; ciò che è stato disegnato in QGIS sul layer
  resta), mentre un layer soltanto nascosto le tiene tutte. Le tabelle si leggono con la pianta
  del chunk attivo nella vista 2D.

Ogni salvataggio fatto in QGIS è una voce di *Modifica > Annulla* (*Annulla: Da QGIS: US 6
modificata*; più elementi: *Da QGIS: 3 modifiche*) e una riga del log per ogni elemento. I
disegni vengono salvati nel chunk, i layer ridisegnati e il GeoPackage portato alla versione del
programma di quelle righe (l'etichetta, l'area...). Quando cambia il contorno di un'unità che ha un
proxy 3D, il proxy viene ricostruito da solo, con la base con cui era stato fatto (il piano, lo
spessore o il DEM dopo l'asportazione, conservati con il proxy da questa versione; per un proxy di
una versione precedente il valore che manca viene chiesto una volta); quando cambia solo il numero
il proxy prende il nuovo nome. Annullando la voce, i proxy tornano con i poligoni e le sezioni
vengono tagliate di nuovo lungo la traccia che avevano.

Come il programma distingue le modifiche di QGIS dalle sue: ogni elemento ha un'identità, `ppg_id`
(un UUID conservato con l'unità, la quota o la sezione, usato anche dal pacchetto QField), e le
righe vengono scritte una per una con quell'identità, così una riga tiene il suo `fid` finché
il suo elemento esiste e una sessione di modifica di QGIS non perde mai le sue righe. Ogni riga ha
anche `ppg_sig`, la firma di ciò che il programma vi ha scritto l'ultima volta: una riga con
geometria o attributi che non corrispondono più è stata modificata da un altro programma. Le
righe senza identità sono state disegnate lì (ne ricevono una subito); le righe sparite sono state
eliminate lì. La finestra guarda una volta al secondo se un altro programma ha scritto nel file
(`PRAGMA data_version` di una connessione tenuta aperta: le sue scritture non contano), e non
scrive mai sopra una modifica o un'eliminazione che non ha ancora letto. Poiché le firme stanno nel file, le
modifiche fatte in QGIS mentre il programma era chiuso tornano quando il progetto viene riaperto
(tranne le eliminazioni fatte nel frattempo: il programma riscrive quegli elementi).

* **Contorni non validi** (un poligono che si incrocia) vengono resi validi; uno senza più area, o
  un elemento senza geometria, viene saltato, con una riga nel log.
* **Sistema di riferimento**: un layer in un sistema di riferimento diverso da quello del
  progetto viene rifiutato (una riga nel log), come un singolo elemento scritto in un altro.
* **QGIS che tiene il file** mentre salva: la finestra legge un attimo dopo.
* **Solo in un senso**: i layer che il programma disegna dai suoi dati - curve di livello
  (tabella `curve`), zone, pietre, patch, aree di ritaglio e misurate, punti di misura, linee di
  rottura, il TIN, le tracce dei legami - sono ricalcolati a ogni cambiamento, quindi non c'è
  dove tenere una modifica fatta in QGIS: non viene ripresa, il log lo dice una volta per ogni
  layer (*Da QGIS: il layer Pietre è disegnato dal programma dai suoi dati e si modifica nel
  programma...*), e le righe tornano subito com'erano. Modificali qui (pietre, zone, patch hanno
  i loro strumenti), o copiali in un tuo layer in QGIS. Anche i disegni sui prospetti e sulle
  sezioni vanno in un solo senso (sotto), come i disegni liberi e i disegni della pianta in
  RustGIS. Per dare a un disegno di un plugin le quote e gli strumenti dei disegni liberi, fanne
  un disegno libero: per una conversione di PhotoCAD,
  [*Disegni liberi...*](plugins/photocad.md#disegni-liberi) del suo pannello; per gli altri,
  *File > Importa > Disegni 3D...* della loro esportazione.
* **Che cosa si vede in QGIS**: i layer sono ridisegnati ogni secondo dal progetto QGIS che il
  programma scrive (*Apri in QGIS*), anche durante una sessione di modifica; non c'è niente da
  aggiornare a mano. Se QGIS mostra un cambiamento solo dopo *Aggiorna*, il suo progetto ha
  perso il ridisegno automatico (sotto): di nuovo *Apri in QGIS*.

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

### Disegni sui prospetti e sulle sezioni

Il GeoPackage dal vivo contiene anche i disegni fatti sui prospetti e sulle sezioni, collocati sul rilievo in 3D
(tabelle `prospetto_<id>_unita`, `_quote`, `_punti`, `_pietre`, `_profilo`; PolygonZ, PointZ, LineStringZ nel sistema
del chunk), aggiornati con quelli della pianta e in un solo senso. QGIS li mostra nella sua vista 3D, RustGIS nel
gruppo "<chunk> - prospetti". *QGIS > Apri il prospetto in QGIS*, con un prospetto mostrato, apre un progetto del
prospetto sviluppato: la sua ortofoto e i suoi disegni nei suoi metri (u, v), come una pianta della facciata.

## RustGIS

[RustGIS](https://github.com/opengeos/RustGIS) è un GIS desktop che altri programmi dello stesso computer possono
pilotare. *QGIS > Apri in RustGIS (collegamento dal vivo)* lo apre con il chunk attivo, e la voce con la casella
*QGIS > Collegamento dal vivo con RustGIS (nei due sensi)* lo tiene collegato: ciò che disegni qui arriva in RustGIS da
solo, e ciò che lì disegni, cambi o cancelli torna qui, come con QGIS. Il collegamento richiede **RustGIS 0.4.0 o
successivo** (uno più vecchio viene rifiutato, con la versione che serve).

Cosa va a RustGIS:

* le tabelle dei disegni del chunk, lo stesso GeoPackage del collegamento con QGIS (`disegni_ppg.gpkg`: `unita`,
  `poligoni`, `sezioni`, `curve`, `linee`, `punti`, `quote`), come layer in un gruppo con il nome del chunk;
* i raster del progetto QGIS del chunk, ognuno una volta e nascosti tranne l'ortofoto: l'ortofoto, il DEM (i suoi
  valori), i prodotti multispettrali (riflettanza in falso colore, indici, raster delle zone), l'ortofoto termica e le
  sue anomalie, i risultati del calcolatore di raster e i raster dei plugin. L'ombreggiatura è il modo in cui QGIS
  disegna il DEM e non viene inviata. Un grande GeoTIFF a tasselli con piramidi interne, che RustGIS aprirebbe da una
  piramide, viene servito da questo computer e letto sul posto, a piena risoluzione dove la vista lo richiede; gli
  altri vanno come file, e uno che RustGIS non sa leggere così com'è va come copia leggibile (fatta una volta, con una
  finestra di avanzamento per i più grandi).

Cosa torna, nei due sensi: le **unità** (contorno e colonne di `pyunitastratigrafiche`), le **quote** e le **sezioni**
(nome e traccia; una sezione con la traccia spostata viene tagliata di nuovo). Curve di livello, zone, pietre, patch,
punti di misura e i disegni dei plugin li calcola il programma: una modifica fatta loro in RustGIS non viene ripresa.
Ogni gruppo di modifiche è una voce di *Modifica > Annulla* ("Da RustGIS: ..."), come per QGIS, e la finestra guarda
RustGIS ogni due secondi finché il collegamento è acceso. PyArchInit resta dalla parte del programma: le unità
disegnate in RustGIS ci arrivano dalla finestra (*Salva modifiche in PyArchInit*, *Pubblica unita' e quote
disegnate*), mai da RustGIS.

**Accendi il controllo una volta sola.** RustGIS offre i suoi strumenti agli altri programmi solo mentre è spuntato *Let
your Agents Control RustGIS* nelle impostazioni del suo AI Assistant. Quella casella è il tuo consenso, conservato in
RustGIS: il programma non la cambia mai. Se è spenta, *Apri in RustGIS* avvia RustGIS se non è aperto (su macOS, se è
già aperto, lo porta in primo piano) e lo aspetta fino a un minuto (*In attesa di RustGIS (fino a 60 secondi)*),
chiedendo ogni mezzo secondo: spunta la casella entro quel minuto e il collegamento parte senza riprovare; dopo il
minuto un messaggio dice cosa spuntare e chiede di riprovare. Il programma trova il RustGIS in esecuzione dal file che
esso pubblica nella sua cartella delle impostazioni e legge da lì il suo token a ogni richiesta; il token non viene mai
conservato, scritto nel log o mostrato.

Come si comporta:

* **Tempi**: RustGIS non scrive mai le sue modifiche in un file, così la finestra gli chiede i layer ogni pochi
  secondi (quando elenca modifiche nei layer collegati, almeno ogni dieci secondi) e subito quando torni alla finestra
  da RustGIS, quando salvi, chiudi il progetto o cambi il chunk attivo. Le modifiche fatte lì tornano entro pochi
  secondi e quando la finestra viene attivata; le ultime vengono lette prima della domanda sul salvataggio.
* **In attesa di RustGIS**: un RustGIS ridotto a icona, nascosto o con una finestra sua aperta non risponde. La
  finestra dice *In attesa di RustGIS: non risponde (ridotto a icona, nascosto, o una sua finestra aperta); il
  collegamento riprova da solo* e riparte da sola quando RustGIS è di nuovo disponibile. Se RustGIS viene chiuso, o il
  controllo degli agenti viene tolto, il collegamento si ferma e lo dice; *Apri in RustGIS* lo fa ripartire.
* **I chunk locali sono esclusi**: RustGIS mostra i layer solo in un sistema di coordinate con un codice EPSG. Un chunk
  in un sistema locale (o in un sistema senza codice) viene rifiutato con un messaggio che lo dice: georeferenzialo, o
  scegli il suo sistema nel pannello GNSS.
* **Sistemi con uno spostamento di datum** (Monte Mario, per esempio): all'avvio il collegamento controlla che RustGIS
  collochi il sistema del chunk dove lo colloca il programma, con un punto scritto, letto da RustGIS ed esportato di
  nuovo. RustGIS converte questi sistemi con uno scarto sub-millimetrico (0,46 mm misurati su Monte Mario): sotto 1 mm
  il collegamento lo accetta, e un elemento che torna spostato di meno vale come non toccato, mai come una modifica.
  Un sistema che RustGIS colloca a più di 1 mm viene rifiutato, perché i disegni si sposterebbero.
* **Cambiare il chunk o il suo sistema** (*Sistema e quote*) toglie da RustGIS i vecchi layer e collega i nuovi; ciò che
  è stato modificato in RustGIS e non ancora letto viene letto prima. I layer che non si riescono a togliere restano in
  RustGIS, non più collegati, con un messaggio.

QGIS e RustGIS possono essere collegati insieme: entrambi leggono e scrivono lo stesso GeoPackage, e le modifiche
dell'uno e dell'altro arrivano da lì.

## Aggiungere progetti

*File > Aggiungi progetti...* (anche nella barra e nel tasto destro dell'Area di lavoro) funziona come *File > Append*
di Metashape: altri progetti entrano in quello aperto, ogni loro chunk un **chunk nuovo**. Una sola finestra prende
più file insieme - progetti di questo programma (`.ppg`), progetti Metashape (`.psx`), archivi WebODM (`all.zip`) - e
una seconda scelta la cartella di un task WebODM/ODM. Di un `.ppg` o di un `.psx` con più di un chunk si spuntano i
chunk da prendere. Dalla riga di comando: `ppg append progetto.ppg altro.ppg scavo.psx all.zip [--chunks "Chunk
1,Saggio B"] [--webodm]` (il progetto viene salvato alla fine; il codice d'uscita è 1 se una sorgente non è entrata).

* **`.ppg`**: ogni chunk arriva con una copia della sua cartella dei dati (mappe di profondità, nuvole, modello e
  texture, DEM, ortofoto, versioni, prospetti; un file alla volta, su APFS un clone copy-on-write: istantaneo e senza
  occupare spazio), con le foto, i sensori, le camere, i marker, le barre di scala, i riferimenti, il sistema di
  coordinate, la trasformazione, i disegni e le impostazioni. Le foto restano dove sono: vengono salvate relative a
  questo progetto quando stanno nella sua cartella, assolute altrimenti. Anche un DEM o un'ortofoto di un altro
  programma a cui il chunk rimanda resta dov'è.
* **`.psx`** e **WebODM**: quello che portano [l'importazione da Metashape](#metashape-psx) e [quella da
  WebODM](#webodm-odm), in chunk nuovi del progetto aperto. Un `all.zip` viene estratto nella cartella del suo chunk
  nuovo.

I progetti scelti vengono solo letti, mai scritti. Un chunk tiene il suo nome; se il progetto ne ha già uno con quel
nome, davanti va il nome del file (`scavo - Chunk 1`). Il chunk attivo resta lo stesso (se è vuoto, il primo chunk
aggiunto prende il suo posto). Un riepilogo dice che cosa è arrivato da ogni sorgente - chunk, foto, prodotti, sistema
di coordinate, avvisi - e quali sorgenti non sono entrate e perché: una che non riesce non ferma le altre.

**Niente viene allineato**: ogni chunk tiene il suo sistema. Per metterli insieme si usa *Flusso di lavoro > Allinea
chunk* (marker, foto in comune o punti) o *Unisci chunk*.

Che cos'altro arriva con i chunk di un `.ppg`:

* i collegamenti fra chunk aggiunti insieme li seguono: un chunk allineato a un altro (*Flusso di lavoro > Allinea
  chunk*) resta allineato alla sua copia, un DEM coregistrato su un altro chunk nomina la copia, e i file di un chunk
  fratello (il DEM usato da una coregistrazione o da un proxy US) puntano alle copie. Un collegamento a un chunk *non*
  aggiunto viene tolto e detto nel riepilogo (mai un collegamento a un altro chunk di questo progetto); un file di un
  chunk non aggiunto resta dov'è, letto e mai modificato, e il riepilogo lo nomina;
* i [legami fra chunk](gui/ties.md) della sorgente fra chunk aggiunti insieme arrivano con loro, sui chunk nuovi
  (lo stesso passo di *Modifica > Annulla* li toglie con i chunk); un legame con un nome già usato prende dopo il nome
  del file (`picchetto (scavo)`). Un legame verso un chunk *non* aggiunto non viene copiato, e il riepilogo lo nomina;
  i due chunk di un legame devono arrivare nella stessa aggiunta (aggiungendo A e poi B in un'altra, il legame resta
  fuori entrambe le volte: rifallo, o aggiungili insieme);
* i GeoPackage delle zonizzazioni multispettrali, scritti accanto al progetto di origine, vengono copiati nella
  cartella del chunk (QGIS modifica la copia);
* le sezioni del progetto di origine (`sezioni/sezioni.json` accanto a lui) si uniscono a quelle di questo progetto,
  con i loro SVG e DXF copiati nella sua cartella `sezioni` (un nome già usato prende davanti quello della sorgente);
  non quando i due progetti stanno nella stessa cartella (condividono già le sezioni). Come ogni sezione del
  programma, sono scritte subito, non con il progetto; dopo un annullamento o un'interruzione l'elenco delle sezioni
  torna com'era, mentre i file SVG e DXF copiati restano in `sezioni/`;
* il sito di questo progetto (*PyArchInit*) non cambia: il riepilogo dice quando la sorgente è di un altro sito;
* i disegni già pubblicati in PyArchInit tengono il collegamento alle schede che hanno scritto.

**Ortofoto e DEM uniti** del progetto di origine ([*Strumenti > Ortofoto e DEM uniti...*](gui/multi-survey.md#ortofoto-e-dem-uniti))
non vengono copiati: sono file del progetto di origine, fatti dai suoi chunk dove si trovavano allora. Il riepilogo nomina
quelli costruiti da un chunk che aggiungi; costruiscili di nuovo qui una volta agganciati i chunk.

Mentre l'aggiunta lavora, i chunk del progetto non si possono aggiungere, rendere attivi, rinominare, attivare o disattivare.
Il progetto risulta modificato, non salvato. *Modifica > Annulla* toglie in un passo tutti i chunk aggiunti (con le
sezioni che hanno portato) e *Ripeti* li rimette; le loro cartelle seguono il progetto come è stato salvato l'ultima
volta: chiudendo senza salvare vengono cancellate, e dopo un arresto improvviso l'apertura successiva cancella le copie
che il progetto salvato non elenca (l'aggiunta scrive l'elenco dei chunk che crea prima di copiarne le cartelle).

## Metashape (`.psx`)

*File > Importa > Progetto Metashape* o `ppg import-metashape progetto.psx nuovo.ppg` (un progetto nuovo; per aggiungere i suoi chunk a quello aperto, [*File > Aggiungi progetti...*](#aggiungere-progetti)).
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
Metashape). Un GeoTIFF esportato in coordinate geografiche (gradi, per esempio EPSG:4326) o geocentriche non dà il
suo CRS al chunk e non viene collegato come DEM o ortofoto: l'importazione lo segnala, e al suo
posto si usa il DEM conservato dentro il progetto.

Una georeferenziazione inutilizzabile - camere che la trasformazione del chunk mette lontano dalla
superficie terrestre, o una conversione nel CRS che non dà coordinate finite - non ferma
l'importazione: il chunk arriva in coordinate locali, con un avviso che ne dice il motivo. È in
metri quando la scala è nota (quella di Metashape, o quella delle barre di scala), altrimenti nelle
unità interne di Metashape; i riferimenti dei marker restano, senza spunta, in metri (nel CRS
proiettato del progetto o nella zona UTM dei riferimenti, indicati nell'avviso), e i GeoTIFF
esportati non vengono collegati. Per collocare il chunk, impostare quel CRS (*Sistema di
riferimento*), spuntare i riferimenti giusti e lanciare *Aggiorna georeferenziazione*.

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
task; per aggiungerlo come chunk del progetto aperto, [*File > Aggiungi progetti...*](#aggiungere-progetti)). Ortofoto, DSM (o DTM), nuvola di punti georeferenziata (`.laz`/`.las`/`.ply`) e la mesh con
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
