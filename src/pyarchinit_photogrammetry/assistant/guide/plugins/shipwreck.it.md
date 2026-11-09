# Relitto

Il plugin *Relitto* fa il disegno tecnico di uno scafo scavato dal suo modello 3D: dove corre l'asse della nave, la forma dello
scafo a ogni stazione, i piani, e un elenco numerato dei legni con le loro misure. Oggi tutto questo si disegna a mano su un'ortofoto.
Il plugin misura quello che c'è. Non ricostruisce quello che manca e non sposta una linea dal modello per carenarla.

Questa pagina descrive la versione 0.5.0 del plugin, che richiede pyarchinit-photogrammetry 1.7.1 o successiva (1.7.7 per i disegni classificati e il livello dell'acqua del chunk). Il README e il codice
sono nel [repository dei plugin](https://github.com/enzococca/pyarchinit-photogrammetry-plugins/tree/main/plugins/shipwreck).

<figure markdown="span">
  ![La pianta dello scafo con le stazioni sull'ortofoto](../img/plugins/shipwreck/plan.jpg)
  <figcaption>La pianta di uno scafo sintetico: la linea di chiglia a tratti, una stazione ogni metro, qualche legno, sull'ortofoto
  girata nel sistema della nave.</figcaption>
</figure>

!!! note "Gli esempi di questa pagina"
    Tutte le figure vengono da uno **scafo sintetico** generato per questa guida, non da un relitto reale: una forma lunga circa
    11 m con ordinate ogni 0,85 m, sedimento rimasto su un lato e una superficie vista attraverso l'acqua, e un'ortofoto disegnata.
    Mostrano che cosa fa il plugin; la superficie vera è nota, ed è quella con cui si confronta la figura dell'acqua.

## In breve

1. Apri il progetto con il modello dello scafo, e l'ortofoto se c'è (il pannello *Relitto* è a destra).
2. **Sistema della nave**: traccia la chiglia da un estremo all'altro, poi di' quale estremo è la prua.
3. **Stazioni**: regolari, o una tabella con i nomi del cantiere.
4. **Sezioni**: trasversali alle stazioni, longitudinali e orizzontali dove le vuoi.
5. **Acqua**: il livello, se parte dello scafo è sott'acqua, e se correggerlo.
6. **Disegni**: piano di costruzione, piano delle linee d'acqua, profilo e pianta, in SVG, PDF, PNG e DXF.
7. **Legni**: disegnali sul raster o importa una tabella; le misure si calcolano da sole; esporta in CSV, Excel o GeoPackage.

<figure markdown="span">
  ![Il pannello del plugin](../img/plugins/shipwreck/pane.it.png)
  <figcaption>Il pannello, sullo scafo sintetico: sei passi, ognuno con i suoi pulsanti e il suo stato.</figcaption>
</figure>

## Che cosa serve

Un chunk con un **modello** in metri: un sistema di coordinate proiettato, o un sistema locale con una scala (una barra di scala,
riferimenti, un modello importato in metri). Un chunk in gradi (WGS 84) viene rifiutato con un messaggio: portalo prima in un sistema
proiettato (*GNSS > Sistema e quote*). Un progetto importato da Metashape in WGS 84 è già in UTM: l'import lo converte da sé.
L'ortofoto serve solo per la pianta, il DEM per niente: le quote si leggono sul modello.

## Il sistema della nave

Coordinate della nave: **x** lungo la chiglia da un punto di partenza, **y** a babordo, **z** in alto, in metri.

* *Traccia la chiglia...* disegna una linea sulla vista raster: il primo e l'ultimo vertice sono gli estremi della linea di chiglia; x
  vale 0 al primo. *Da due marker...* usa due marker con una posizione: è così che si scelgono gli estremi nella vista 3D, con il
  pulsante *Marker* del programma.
* *La prua è l'altro capo*: la prima volta la **prua** è dove hai cominciato a tracciare. Dal modello non si vede quale capo sia,
  quindi lo dici tu: questo pulsante **scambia babordo e tribordo su tutti i disegni**, ed è l'unico a farlo.
* *x dall'altro capo*: x corre nell'altro verso. La prua resta dov'è, y e i lati non cambiano. Lo stesso una chiglia tracciata di nuovo
  nell'altro verso.
* *Origine, datum...*: il valore di x al primo estremo (0 alla prua, o una stazione scelta) e la quota di z = 0.

L'alto è la verticale del mondo e la linea di chiglia è messa in piano: x è la direzione della chiglia in pianta e z la quota sopra il
datum. Una chiglia con una sua pendenza la conserva come numero, mostrato nel pannello e sui fogli, e non tolto. Il sistema è una
trasformazione usata solo dal plugin: il chunk non cambia. In un sistema locale l'alto è l'asse z del modello, che niente verifica
come verticale: ogni foglio lo dice, e un modello non in bolla dà sezioni inclinate.

## Stazioni e sezioni

*Regolari...* dà S1, S2... a una distanza: questi nomi sono nostri, per un rilievo non ancora numerato. *Da tabella...* legge
`nome, x`, con i nomi del cantiere: `F30, 12.4` separata da virgole, col punto decimale; `F30; 12,4` separata da punto e virgola o
tabulazione, col punto o con la virgola. Una riga con troppe celle viene rifiutata col suo numero, perché `F30, 12,4` sono tre celle e
sarebbe una stazione sbagliata. *Dalle ordinate del catalogo* mette una stazione a ogni ordinata del catalogo.

*Calcola...* taglia il modello: le trasversali alle stazioni, le longitudinali alle y che elenchi, le orizzontali alle z che elenchi,
di traverso fin dove arriva il modello (o dentro la metà larghezza che dai) e poco oltre gli estremi della chiglia. Il taglio è quello
del programma, sulla mesh, in doppia precisione; le linee stanno in `<dati del chunk>/shipwreck/sections.json`. Gira in background e si
può annullare. I fogli danno la metà larghezza e dicono quali linee arrivano al suo limite; una stazione fuori dal modello non ha
linea, e pannello e fogli la nominano.

Quando cambiano il sistema della nave, il modello o la georeferenziazione del chunk, il pannello dice che le sezioni sono da rifare e i
disegni rifiutano quelle vecchie. Quando cambia il sistema di coordinate del chunk, il sistema della nave e i legni disegnati sono
ancora nelle coordinate vecchie: il plugin rifiuta finché la chiglia non viene tracciata di nuovo. I disegni già scritti vengono segnati
come da rifare nel pannello e nell'albero.

!!! tip "Piccoli anelli chiusi"
    La sezione di un modello vero non è una linea sola: un sacco di sabbia, una pietra o un buco della mesh danno un piccolo anello
    accanto alla linea principale. Stanno sul modello, quindi si disegnano.

## L'acqua

Il fondo visto attraverso l'acqua è **apparente**: in vista nadirale la profondità vera è circa quella apparente per l'indice di
rifrazione, 1,34 per l'acqua. Dai il livello, con un numero in z della nave o cliccando sul raster il pelo dell'acqua, e accendi la
correzione: un punto sotto il livello si disegna a `livello - (livello - z) * n`.

Dalla versione 0.5.0, con pyarchinit-photogrammetry 1.7.7 o successivo, un livello dell'acqua dato al chunk nel programma
(*Acqua*: a mano, dal bordo dell'acqua o con un clic) è il valore proposto da *Livello e rifrazione...* finché il plugin non ha un
livello suo: lo stato del passo lo dice ("livello dell'acqua del chunk"), la correzione resta spenta e il livello diventa quello del
plugin quando confermi la finestra. Un livello del plugin vince sempre.

<figure markdown="span">
  ![Una sezione come misurata e corretta](../img/plugins/shipwreck/water.it.png)
  <figcaption>Due stazioni dello scafo sintetico come misurate (grigio) e corrette (blu) sotto un livello di -0,10 m, con la
  superficie vera a puntini. Qui la correzione cade sul vero perché la superficie apparente è stata fatta con la stessa formula:
  l'acqua vera è meno gentile.</figcaption>
</figure>

È una prima approssimazione. I raggi obliqui non sono modellati (fanno sembrare il fondo ancora meno profondo: la correzione, semmai, resta
corta), né la superficie in movimento, né la riva dove il livello incontra lo scafo. La linea grezza e quella corretta restano
entrambe: il piano di costruzione e il profilo mostrano quella corretta e lo dicono, e la grezza resta nel layer `RAW` del loro DXF. Nel
piano delle linee d'acqua le linee restano dove sono e le etichette portano la quota corretta. La nota è sempre sul foglio, anche a
correzione spenta.

!!! warning "Quanto fidarsi del fondo sott'acqua"
    Leggi la forma, non le quote. Un'acqua chiara e ferma dà spesso un fondo che sta insieme, con le ordinate visibili nelle
    orizzontali. Ma le quote sono apparenti, il livello è difficile da leggere meglio di qualche centimetro, la correzione non è
    controllata da niente di misurato sott'acqua, e in uno scafo non del tutto svuotato la superficie è il sedimento, non il fasciame.
    Prima di usare una profondità, misura a mano qualche punto.

## I disegni

*Genera...* scrive ogni disegno in `<dati del chunk>/shipwreck/drawings` in SVG, PDF, PNG e DXF: lì si chiedono i campi del cartiglio e
la scala, automatica (la scala standard più dettagliata che sta in un foglio di circa A0) o scelta. Un disegno senza linee (se non
hai chiesto orizzontali, per esempio) si salta. Ogni foglio porta un cartiglio con la x della prua e la quota del datum, la metà
larghezza, la nota sull'acqua e una legenda: le linee di sezione vengono dal modello; i tratteggi sono l'asse del sistema della nave e il
livello; i legni vengono dal catalogo, disegnati o importati, e non sono tagliati dal modello.

* **Piano di costruzione**: le trasversali sovrapposte, viste da poppa, babordo a sinistra, ogni stazione col suo colore. I due lati sono
  disegnati come misurati: niente è specchiato.
* **Piano delle linee d'acqua**: le orizzontali, con le stazioni e la linea di chiglia. Sott'acqua le etichette portano la quota corretta.
* **Profilo**: le longitudinali, con le stazioni e il livello.
* **Pianta**: le stazioni e i legni sull'ortofoto girata nel sistema della nave.

Le piante sono viste dall'alto in qualunque verso corra x: non sono mai speculari. Il DXF ha un layer per tipo (`STATIONS`,
`LONGITUDINALS`, `HORIZONTALS`, `RAW`, `KEEL`, `LEVEL`, `TIMBERS`, `GRID`, `TEXT`), in metri sul foglio.

<figure markdown="span">
  ![Piano di costruzione](../img/plugins/shipwreck/body-plan.it.png)
  <figcaption>Il piano di costruzione dello scafo sintetico: undici stazioni dalla prua (blu) all'estremo largo (rosso), corrette sotto il
  livello tratteggiato. Il gradino piatto a babordo è il sedimento rimasto nello scafo.</figcaption>
</figure>

<figure markdown="span">
  ![Piano delle linee d'acqua](../img/plugins/shipwreck/half-breadth.it.png)
  <figcaption>Le orizzontali dello scafo sintetico, da -0,10 a -0,70 m apparenti (a -0,90 m il foglio dice che non c'è linea): le ordinate si vedono come gobbe regolari.</figcaption>
</figure>

## I legni

Disegna un legno sulla vista raster come linea, poligono o punto e dai tipo (chiglia, paramezzale, ordinata, corso di fasciame, fasciame
interno, serretta, elemento di fissaggio, altro), lato e nome; oppure importa una tabella con l'intestazione: `tipo`, `lato`, `nome`, `stazione`,
`note` (anche in inglese: `kind`, `side`, `name`, `station`, `notes`) e la geometria come `x, y` (un punto) o `x1, y1, x2, y2` (una linea) in
coordinate della nave, o `wkt` nelle coordinate del chunk.

| Misura | Come |
|---|---|
| x, y | la posizione media in coordinate della nave |
| lunghezza | lungo una linea sul modello; per un poligono, il lato lungo del rettangolo che lo contiene |
| larghezza | il lato corto di quel rettangolo; una linea non ne ha |
| z min, z max | le quote del modello sul legno sopra il datum; con l'acqua accesa, anche corrette |

Le quote valgono circa un lato della mesh. Sott'acqua le lunghezze lungo una linea restano come misurate. Senza modello il catalogo funziona
lo stesso, con lunghezze in pianta e senza quote. La maglia delle ordinate viene dalle loro x, lato per lato, nel secondo foglio del file
Excel. Quando il lato di un record non concorda col segno della sua y, il pannello, la tabella e gli export lo dicono (`warning`): dopo
*La prua è l'altro capo*, per esempio. I legni sono un layer della vista raster: cancellarne uno lì lo toglie dal catalogo. *Esporta...*
scrive CSV, XLSX o GeoPackage; lo stesso le voci di *File > Esporta*.

## Fasce a clinker

Il passo 7 del pannello, **Fasce (clinker)**, legge il fasciame di uno scafo a clinker sulle sezioni: dove ogni fascia
si sovrappone a quella sotto, e da lì le fasce. Servono il sistema della nave e le sezioni alle stazioni (passi da 1 a
3); una stazione ogni metro o meno è un buon inizio.

| Pulsante | Che cosa fa |
|---|---|
| *Rileva i corsi...* | I gradini sulla sezione di ogni stazione, le loro linee lungo lo scafo, il layer "Fasce" nella vista raster e nei disegni, `lands.dxf` e `lands.gpkg`. |
| *Modello delle fasce...* | Una fascia fra due linee di gradini: larghezze, lunghezza, spessore e sovrapposizione, una mesh per fascia, le tabelle. |
| *Nel catalogo* | Una scheda `plank` del catalogo dei legni per fascia, con il suo contorno in pianta e la nota *modello delle fasce (ipotesi)*. |
| *Per Clinker Boat Tool...* | I file per lo strumento Houdini di Fröhlich e Nielsen, in una cartella che scegli. |
| *Fasce da Houdini...* | Le tavole che lo strumento esporta, di nuovo nel catalogo con la loro distanza dalla scansione. |
| *Apri la cartella* | `<dati del chunk>/shipwreck/clinker`. |

### Come si trovano i gradini

A ogni stazione il plugin prende la sezione **grezza** (mai quella corretta per l'acqua) e la divide alla chiglia:
babordo (y > 0) e tribordo. Ogni lato si legge dalla battura, la cima del fianco della chiglia, fino all'orlo, e finisce
dove il profilo gira attorno all'orlo o piega nel sedimento, in una trincea o nel fondale. Una media mobile su 12 cm del
profilo è lo scafo "senza tavole"; lo scostamento del profilo grezzo da essa, su uno scafo a clinker, è un dente di sega
che sale piano dentro una fascia e scende del gradino a ogni sovrapposizione. Un gradino è dove quello scostamento cambia
di più del **gradino minimo** entro 3 cm; due gradini distano almeno la **larghezza minima di fascia**. Ogni gradino ha la
sua posizione lungo la sezione dalla battura e il suo salto in millimetri.

Fra le stazioni, il numero di gradini trovato più spesso è quello dello scafo su quel lato. Una stazione con un altro
numero (una fascia che si interrompe, un orlo eroso, un danno) è **incerta**, e così una stazione la cui battura o il cui
torello stanno a più di 2 cm dalla curva per le stazioni vicine (cercati con cinque stazioni o più su un lato: la curva
è una parabola per quattro vicine, e con meno un salto vero non viene preso). I gradini di una stazione incerta si assegnano alle
linee per la loro posizione lungo la sezione; un gradino fuori dalle linee viene elencato. Niente viene carenato o
riempito: un lato con un buco, un pezzo staccato o una chiglia illeggibile non dà gradini e viene elencato con il motivo.

| Parametro | Predefinito | Quando cambiarlo |
|---|---|---|
| Gradino minimo (mm) | 8 | Più basso per un fasciame sottile o consumato, più alto se il rumore del modello dà gradini falsi. |
| Larghezza minima di fascia (m) | 0,08 | Più bassa per fasce molto strette. |
| Metà larghezza della chiglia (m) | 0,10 | Fin dove dal centro della chiglia può arrivare il suo fianco. |
| Larghezza minima della suola (m) | 0,02 | Abbassala se una chiglia stretta e consumata lascia lati non letti a ogni stazione. |

Il pannello dice quante fasce ha ogni lato, quali stazioni sono incerte e perché, i gradini fuori dalle linee e i lati
non letti. Se le sezioni vengono tagliate di nuovo dopo il rilevamento, il pannello dice di rilevare di nuovo i gradini e
le linee nella vista raster si disegnano tratteggiate.

### Il modello delle fasce: un'ipotesi

I gradini sono misurati. Le linee che li uniscono, e le fasce fra di esse, sono **un'ipotesi dalle sezioni, non una
misura**: ogni file lo dice (*modello delle fasce: ipotesi dalle sezioni, non una misura*) e il modello non si mescola mai
con le sezioni.

Su ogni lato la fascia 1 è il torello, fra la battura e la prima linea di gradini; l'ultima fascia arriva all'orlo. Per
fascia: la larghezza a ogni stazione (la lunghezza lungo la sezione fra le sue due linee) con un'incertezza in
millimetri, niente dove la stazione è incerta o non letta; la lunghezza lungo la linea superiore; lo spessore (il gradino
mediano dove si sovrappone a quella sotto, o il valore che dai); la sovrapposizione che dai (20 mm di partenza). Le fasce si
chiamano `B1, B2, ...` a babordo e `T1, T2, ...` a tribordo. File in `<dati del chunk>/shipwreck/clinker/`: `strakes.obj`
(coordinate della nave, un oggetto per fascia), `strakes.ply` (coordinate del chunk, doppia precisione), `strakes.csv` e
`strakes.xlsx`.

Il modello viene rifiutato quando un lato con gradini ha meno di tre stazioni con una larghezza: sarebbe tutto
interpolato. Quando i gradini si rilevano di nuovo, i file del modello precedente (e le loro copie nell'ultima cartella
esportata per il Clinker Boat Tool) prendono il suffisso `.old`, le sue fasce escono dal catalogo dei legni e il pannello
lo dice. Se un file non si può rinominare (aperto in un altro programma), non cambia niente e il messaggio lo nomina.

### Il Clinker Boat Tool

Il Clinker Boat Tool di Fred Fröhlich e Sigurd Solhaug Nielsen è un Houdini Digital Asset che costruisce una barca a
clinker tavola per tavola da una scansione. *Per Clinker Boat Tool...* scrive `clinker_boat_tool/` nella cartella che
scegli, nel sistema della nave girato con Y in alto (l'asse verticale di Houdini) e la chiglia lungo X: `hull.obj`,
`sections.obj`, `stations.json`, `frame.json`, `lands.obj` e `strakes.obj` con le tabelle delle fasce quando ci sono, e un
`README.txt`. In Houdini, con i sei passi dello strumento:

1. **Importa la scansione**: `hull.obj`; `sections.obj`, `lands.obj` e `strakes.obj` come riferimenti.
2. **Calibra e allinea**: già fatto, in metri con la chiglia lungo X e Y in alto.
3. **Definisci le sezioni**: le x delle stazioni sono in `stations.json`; `sections.obj` mostra lì i tagli.
4. **Genera la disposizione delle tavole**: larghezze, spessore e sovrapposizione per fascia da `lands.obj` e
   `strakes.xlsx`.
5. **Piega e allinea** le tavole sulla scansione.
6. **Preset ed esportazione**: esporta le tavole in OBJ, un oggetto o un gruppo per tavola, con un nome tuo
   (`tavole.obj`): mai `strakes.obj` o `lands.obj` nella cartella dell'esportazione, che un nuovo rilevamento mette da parte.

*Fasce da Houdini...* rilegge quell'OBJ: ogni tavola diventa una scheda `plank` del catalogo (il contorno in pianta, il
lato dalla y media o da B/T nel nome), con la distanza dei suoi vertici dalla scansione, mediana e 90° percentile in
millimetri, in `<file>_scostamenti.csv` e `.xlsx`. Importare di nuovo lo stesso file ne sostituisce le tavole, mai le tue.
Un gruppo che contiene tutti gli altri (come *all*) non è una tavola e viene lasciato fuori, che contenga o no anche le
facce fuori da ogni gruppo.

Il plugin scrive i file che i passi dello strumento leggono e rilegge le tavole che esporta: niente dello strumento è
copiato o usato. Da citare:

- Fröhlich, F., Nielsen, S. S. *From Photogrammetry to Construction Knowledge: 3D Documentation and Parametric
  Reconstruction of Historic Clinker Boats*. CC BY 4.0. [10.5281/zenodo.23422398](https://doi.org/10.5281/zenodo.23422398)
- Fröhlich, F., Nielsen, S. S. *Clinker Boat Tool* (Houdini Digital Asset). Licenza MIT.
  [10.5281/zenodo.17488549](https://doi.org/10.5281/zenodo.17488549)

### Limiti delle fasce

- Su sezioni di una mesh con un rumore oltre circa 0,5 mm le larghezze valgono a circa un centimetro. A 1 mm di rumore
  circa un lato su quindici può avere un numero di gradini sbagliato; viene segnalato come stazione incerta.
- I salti si cercano solo con cinque stazioni o più su un lato. Un salto di 2-2,5 cm della battura a una stazione
  d'estremità non viene preso. Tre o più stazioni vicine spostate
  insieme, o un gradino graduale, non vengono segnalati.
- Un gradino permanente della chiglia (uno scafo rotto o spostato, una palella) segnala ogni stazione da un lato. Oltre
  circa 3 mm di dispersione sulla battura una stazione può essere segnalata a torto.
- Senza suola, una V di fasciame più ripida di 80° entro la metà larghezza della chiglia è letta come chiglia finché non
  si apre. Un fianco della chiglia allineato al torello è un lato non letto (`keel_unclear`).
- Uno scafo visto dall'interno (il fasciame sotto le ordinate, come in molti relitti scavati) mostra la faccia interna:
  la chiglia è nascosta dal paramezzale e la lettura per stazione dà stazioni incerte. La prossima versione traccerà i
  gradini sul rilievo dello scafo.

## Rilievo, ricalco, legni 3D e pianta del relitto

Il passo 8 del pannello, **Rilievo e ricalco**, porta il modello 3D di uno scafo scavato al disegno che fa
l'archeologo sul campo: ogni legno contornato, nominato e tipizzato in una pianta, e ogni legno un oggetto 3D. Serve il
sistema della nave (passo 1); stazioni e sezioni non servono.

| Pulsante | Che cosa fa |
|---|---|
| *Rilievo dello scafo...* | Lo scafo visto dall'alto nel sistema della nave: un prospetto del programma su un piano orizzontale (u = x, v = segno × y: y alla sinistra di x, qualunque sia il capo della prua), le sue altezze, l'ombreggiatura e il rilievo locale. |
| *Mostra* | Il rilievo nella vista raster, con le altezze, l'ombreggiatura o il rilievo locale in grigio sopra. |
| *Proponi...* | Comenti, ordinate, paramezzale, serrette e fasce letti sul rilievo: proposte, tratteggiate, un colore per tipo. |
| *Accetta* / *Scarta* | Le proposte selezionate sulla vista nel catalogo dei legni / buttate per sempre (anche *Canc* sulla vista). |
| *Correggi...* / *Aggiungi...* | Un legno ridisegnato, o uno nuovo disegnato a mano, sul rilievo, con gli strumenti di disegno e lo snap del programma. |
| *Nomi da tabella...* | Una tabella `nome, x`: le ordinate prendono i nomi del cantiere. |
| *Sciolto* | I legni selezionati segnati come fuori posto. |
| *Chiglia (dimensioni)...* | Una chiglia da larghezza e altezza date: un'ipotesi. |
| *Legni 3D...* | Ogni poligono ricalcato un oggetto 3D chiuso. |
| *Esporta 3D...* | OBJ, GLB o IFC, un oggetto per legno, con le tabelle. |
| *Pianta del relitto...* | La pianta con ogni legno contornato, colorato per tipo e nominato: SVG, PDF, PNG, DXF. |

Ogni modifica del catalogo, delle proposte o della chiglia è un passo di *Modifica > Annulla* (proposte e legni
eliminati insieme sulla vista sono un passo solo).

### Il rilievo dello scafo

Il modello è letto dall'alto nel sistema della nave: per ogni cella la superficie più alta del modello (l'interno di uno
scafo adagiato sul fondo), un GeoTIFF in metri della nave con il sistema nei tag, e una copia orientata a nord nel sistema
del chunk per il GIS. L'ombreggiatura da quattro direzioni e il rilievo locale si vedono in grigio sopra, come il
programma fa per un DEM. La cella viene dalla densità del modello; 5 mm va bene per un modello ravvicinato di uno scafo
(un modello di 15 milioni di facce richiede circa un minuto). I disegni fatti sul rilievo restano con lui quando lo
rifai, anche dopo un cambio del sistema della nave.

Il rilievo è un campo di altezze: una quota per cella, la più alta. Ciò che dall'alto, nel sistema della nave, non si
vede non c'è, e quindi non c'è nelle proposte né nei legni 3D: un sottosquadro, un fianco più ripido della verticale, la
faccia di sotto di un legno, un legno sotto un altro, la faccia esterna del fasciame. Per un relitto visto dall'interno
significa: il fasciame è la sua faccia interna (lo spessore viene dai gradini dei comenti, dal modello delle fasce o dal
valore che scrivi), la chiglia non si vede (per questo la chiglia di dimensioni date), le ordinate sono la loro parte
visibile sopra il fasciame e ciò che sta sotto le ordinate è interpolato. Uno scafo adagiato su un fianco vuole un
sistema della nave suo, perché la parte che interessa si veda dall'alto.

### Le proposte

![Il rilievo dello scafo sintetico con le proposte](../img/plugins/shipwreck/shipwreck-relief-proposals.png)

Il plugin legge sul raster delle altezze:

- i **comenti** (i gradini del fasciame a clinker): salti di traverso allo scafo, seguiti lungo x, interrotti nelle
  lacune, una linea ciascuno; numerati dalla chiglia (`comento B1`, `B2`, ... a babordo, `T1`, ... a tribordo); un pezzo
  che cade fra due comenti già presenti è un dubbio `comento B?` con punteggio 0,1;
- le **ordinate**, il **paramezzale** e le **serrette**: ciò che sporge dal fasciame (l'altezza sopra un'apertura lungo x,
  tolta prima la salita del fondo lungo x, così che un'ordinata tenga tutto il suo contorno dove lo scafo sale verso
  prua e poppa), come poligoni, classificati per lunghezza e direzione; le ordinate nominate dalla prua, con la loro
  larghezza e il passo per lato. Un elemento di traverso che manca solo la prova dell'allungamento (lungo fra 1,8 e 3
  volte la larghezza: quasi sempre un'ordinata unita alla vicina o a fasciame rialzato) è un dubbio `ordinata ?` con
  punteggio 0,1: accettalo e correggine il contorno;
- le **fasce**: le bande fra comenti consecutivi, un poligono ciascuna, `fascia B1`, ...; la banda 1 va dalla linea di
  chiglia (spostata di metà larghezza della chiglia) al comento 1, e ogni banda comincia e finisce dove cominciano e
  finiscono i pezzi dei suoi comenti, così dove i comenti interni non si leggono le bande sono rettangoli. Una banda
  larga più di 1,5 volte le altre bande fra comenti del suo lato, o stesa per lo più sopra paramezzale e serrette
  proposti, è una dubbia `fascia B?` con punteggio 0,1.

| Parametro | Predefinito | Quando cambiarlo |
|---|---|---|
| Il modello mostra | l'interno | *L'esterno* per uno scafo sollevato: decide il verso del gradino di un comento. |
| Gradino minimo dei comenti (mm) | 8 | Più basso per un fasciame sottile o consumato; più alto se il rumore del modello dà falsi comenti. |
| Apertura (m) | 0,3 | Più lunga della larghezza fra due ordinate, più corta del fasciame fra due ordinate. |
| Altezza minima di un legno sopra il fasciame (mm) | 20 | Più bassa per ordinate consumate, più alta se vengono proposti grumi di sedimento. |

Tutto è una **proposta**: accettala, correggila o scartala a mano. Una nuova lettura non tocca il catalogo e non
ripropone ciò che è stato accettato o scartato. I limiti, detti chiaramente:

- un legno sciolto di cui si legge un solo bordo dà un comento che non lo è;
- un legno più stretto di 5 cm accanto a un comento può sfuggire;
- un modello che mostra l'esterno dello scafo vuole *L'esterno*, altrimenti ogni comento è dubbio;
- un legno lungo appoggiato a filo lungo quasi tutto un comento rinomina i comenti oltre di lui;
- un avanzo dentro una fascia di larghezza normale è proposto come `comento ?`: accettarlo divide la fascia in due;
- un comento che manca lungo tutto lo scafo dà una banda sola su due fasce;
- un rumore del modello oltre 2 mm spezza i comenti e può aggiungere una fascia;
- pezzi spuri a prua possono prendere il rango 1;
- su un relitto vero ordinate vicine, o con il fasciame fra di loro, sono lette come un elemento largo, proposto come
  `ordinata ?` (o *altro* se ancora più corto): correggine il contorno o ricalca le ordinate a mano con *Aggiungi...*;
  separare le ordinate unite è previsto per la versione 0.4.0; zone piatte di sedimento possono essere proposte come
  paramezzale;
- le fasce sono bande fra i comenti letti, non i contorni delle tavole: dove mancano comenti sono rettangoli sopra
  qualsiasi cosa ci sia (sedimento, legni longitudinali); correggile o ricalcale a mano.

### Un disegno classificato per orientamento

Dalla versione 0.5.0, con pyarchinit-photogrammetry 1.7.7 o successivo (con un programma più vecchio il pulsante è nascosto),
un tuo ricalco può riempire il catalogo: *Importa un disegno classificato...* al passo 8 legge un layer di disegni liberi del chunk, oppure
un file GeoPackage, GeoJSON o DXF (portato nel sistema del chunk come fa l'importazione dei disegni liberi), lo porta nella
pianta della nave e lo fa classificare dal programma lungo la chiglia ([Un disegno classificato per orientamento](../gui/drawing.md#un-disegno-classificato-per-orientamento)). Il significato dei gruppi lo dà il plugin:

| Gruppo del programma | Legno | Geometria |
|---|---|---|
| coppie di traverso | ordinata | poligono |
| linee lungo l'asse | comento | linea |
| coppie lungo l'asse | fascia (corso di fasciame) | poligono |

Le linee di traverso singole e quelle in altre direzioni non sono legni e restano fuori (la tavola le mostra con il loro
numero). Il lato viene dalla y media, come per un legno ricalcato a mano; i nomi sono quelli del plugin: `ordinata N` dalla prua
(i due lati di una stessa ordinata hanno lo stesso N), `comento B1`, `B2`, ... e `fascia T1`, ... numerati dalla chiglia su
ogni lato.

Niente entra nel catalogo prima della **tavola di controllo**: mostra ogni elemento colorato per gruppo con i nomi del plugin,
e una casella per tipo con il suo numero. *Nel catalogo* aggiunge i tipi spuntati come legni misurati (sorgente `orient:<disegno>:<id>`),
un passo di *Modifica > Annulla*; *Annulla* lascia il catalogo com'era. Importando di nuovo lo stesso disegno, o un disegno
cresciuto, entra solo il nuovo: un legno già nel catalogo da un disegno classificato (stesso tipo, stessi vertici) viene
saltato, e la finestra e la riga di comando dicono quanti. La tavola resta in `shipwreck/classified/plate.png`.

!!! tip "Ordinate disegnate come linee sciolte"
    Un poligono ricalcato è sempre un elemento solo: quando puoi, disegna ordinate e fasce come poligoni. Ordinate disegnate
    con **due linee sciolte ciascuna, vicine fra loro** (per esempio larghe 12 cm con 13 cm fra l'una e l'altra) sembrano linee
    a passo regolare ed escono come linee di traverso singole, non come ordinate: porta **Isolamento delle coppie** a 1
    (`--pair-isolation 1`), oppure disegnale come poligoni. Abbassalo solo per disegni così e guarda la tavola: con 1 anche
    due comenti sciolti vicini fra loro possono essere letti come una fascia.

La finestra chiede anche la distanza delle coppie (da 2 a 50 cm di partenza) e il tratto più corto letto (5 cm). Dalla riga di
comando:

```bash
ppg plugin shipwreck classified PROGETTO.ppg --layer "Rilievo" --plate tavola.png        # i conteggi e l'elenco; niente salvato
ppg plugin shipwreck classified PROGETTO.ppg --layer "Rilievo" --accept                  # tutto nel catalogo
ppg plugin shipwreck classified PROGETTO.ppg --file disegno.dxf --pair-isolation 1 --accept --kinds frame,plank
ppg plugin shipwreck classified PROGETTO.ppg --layer "Rilievo" --param pair_max=0.4 --accept 7 8 9
```

Senza `--accept` non si scrive niente; `--accept` con degli id prende quegli elementi, senza tutti (dei tipi di `--kinds`). Gli
id sono quelli elencati da un'esecuzione senza `--accept` con lo stesso disegno e gli stessi parametri: se cambia l'uno o
l'altro cambiano.

### I legni 3D

![I legni dello scafo sintetico in 3D](../img/plugins/shipwreck/shipwreck-timbers-3d.png)

*Legni 3D...* fa di ogni legno ricalcato come poligono un oggetto chiuso nel sistema della nave, un PLY per legno in
`<dati del chunk>/shipwreck/timbers/`:

- una **fascia** è la superficie del modello dentro il suo contorno con il suo spessore (il suo, quello del modello delle
  fasce dalle sezioni o il gradino dei suoi comenti; il valore che scrivi vale solo per le fasce senza). Dove un'ordinata,
  il paramezzale o una serretta la coprono, la superficie è interpolata dalla fascia ai due lati e segnata nascosta;
- un'**ordinata**, il **paramezzale**, una **serretta**, il serrettame e ogni altro poligono è un solido dal fasciame sotto
  di lui (interpolato da un anello di celle intorno) fino alla sua cima (la superficie del modello).

Limiti: ricalca tutti i legni che toccano quello che costruisci (uno non ricalcato finisce nell'anello da cui si legge il
fasciame sotto); i file sono grandi sugli scafi lunghi (vedi le dimensioni sotto); un tratto nascosto
con celle da un lato solo è tenuto costante; un legno ricalcato dove sullo scafo non c'è niente dà una mesh di spessore
nullo. Di traverso a un legno lungo lo scafo sotto di lui è una curva per le quote e le pendenze del fasciame ai due
lati (una corda dove le pendenze sono quelle del fianco di un altro legno). Sullo scafo sintetico tutte le 23 ordinate,
da prua a poppa, stanno entro il 2 % del loro volume vero, il paramezzale entro l'1 %, le fasce entro l'1,4 % di quello
sulla superficie a clinker (il torello, una striscia larga 2 cm vista dall'alto, entro il 10 %). Una costruzione
interrotta è detta nel pannello e nell'albero (*interrotto*): i suoi file sono parziali. Quando catalogo, chiglia o
rilievo cambiano dopo la costruzione il pannello dice *da rifare*, e un'esportazione avvisa che i file dicono ciò che il
catalogo diceva allora.

### La chiglia: un'ipotesi

Il modello di uno scafo scavato ne mostra l'interno: la chiglia non si vede. *Chiglia (dimensioni)...* ne fa una da
larghezza e altezza date: un parallelepipedo lungo la linea di chiglia, sotto il fondo del paramezzale (altrimenti sotto
lo scafo a y = 0), lungo tutto il paramezzale (altrimenti tutto il rilievo) o fra due x date. Il suo stato è
*ricostruito: dimensioni date*: è campita nei disegni, più chiara e trasparente (`_ric`) nei file 3D, una riga sua nelle
tabelle; l'intervallo di x resta quello del momento in cui la imposti. Un legno di chiglia ricalcato su un chunk che
mostra l'esterno la sostituisce. *Nessuna chiglia* la toglie.

### Esporta 3D

Un oggetto per legno con il suo nome, nel sistema della nave o nelle coordinate del chunk:

- **OBJ**: `o <nome>` per legno, un materiale per tipo nel `.mtl`;
- **GLB**: un nodo per legno, un colore per tipo;
- **IFC4**: un elemento per legno (IfcPlate per fasciame e serrettame, IfcMember per gli altri) con
  `Pset_PyArchInit_Timber` (nome, tipo, lato, chunk, stato, lunghezza, larghezza, quote, volume, spessore in metri). Un
  chunk in un sistema locale o geografico non ha la IfcMapConversion, e il file lo dice. L'IFC di uno scafo lungo
  richiede da decine di secondi a minuti (sotto);
- le tabelle `timbers_3d.csv` e `.xlsx` accanto (spessore in millimetri).

Dimensioni a 5 mm, misurate su uno scafo sintetico di 12 m (1,9 milioni di celle): proposte 1 s e circa 0,45 GB di
memoria, legni 3D circa 0,75 GB durante la costruzione (le due fasce sui bordi 1,5 milioni di facce ciascuna), file PLY
160 MB, IFC circa 20 s e 290 MB. Uno scafo di 20 m a 5 mm ha circa 16 milioni di celle: conta su diversi GB di memoria
per le proposte (circa 3,5 GB) e i legni 3D, un IFC di qualche GB che richiede minuti e una decina di minuti per il
rilievo di un modello fotogrammetrico. Per le esportazioni 3D di uno scafo lungo costruisci il rilievo a 1 cm (quattro
volte meno celle); la decimazione dei solidi è prevista per la versione 0.4.0.

Le ipotesi finiscono in `_ric`, più chiare e semitrasparenti, con il loro stato nel Pset.

### La pianta del relitto

![La pianta del relitto dello scafo sintetico](../img/plugins/shipwreck/shipwreck-wreck-plan.png)

*Pianta del relitto...* disegna ogni legno del catalogo sopra l'ombreggiatura del rilievo, l'ortofoto o niente:
contornato e campito leggero nel colore del suo tipo, il fasciame più chiaro perché il rilievo sotto resti leggibile,
nominato lungo la sua lunghezza (un'ordinata di traverso, vicino all'estremità lontana dalla chiglia); le parti di una
fascia sotto un'ordinata, il paramezzale o una serretta a trattini; le ricostruzioni (la chiglia data, le fasce dalle
sezioni, le tavole importate dal Clinker Boat Tool) campite a righe oblique (`////`). Legenda per tipo e stato, scala
grafica, nord, cartiglio; SVG, PDF, PNG e DXF con un layer per tipo (`LEGNI_<TIPO>`, `RICOSTRUZIONE_<TIPO>`) in
`<dati del chunk>/shipwreck/drawings`. Con legni ricalcati il piano di costruzione segna le fasce a ogni stazione e il
profilo mostra paramezzale e chiglia. Se il rilievo è da rifare il pannello propone prima di rifarlo.

Il modello delle fasce dalle sezioni (versione 0.2.0) resta disponibile come livello d'ipotesi: una fascia delle sezioni
non è disegnata dove una fascia ricalcata ha il suo nome.

Limiti dei disegni: fasce strette o più serrette che finiscono alla stessa x possono affollare i nomi; un'ordinata che
attraversa la linea di chiglia è nominata all'estremità alta; un legno di chiglia misurato non ha il riquadro sul
profilo; i nomi delle stazioni del piano di costruzione possono sovrapporsi.

## Ricostruzione (ipotesi)

Il passo 9 del pannello, **Ricostruzione (ipotesi)** (versione 0.4.0), completa ciò che il relitto ha perso. Ogni sua
parte è un'ipotesi costruita da ciò che è stato ricalcato e misurato: campita nei disegni, più chiara e trasparente
(`_ric`) nei file 3D, con uno stato suo nelle tabelle; il catalogo dei legni misurati non cambia mai. Servono il rilievo
dello scafo e i legni ricalcati sopra (passo 8); la carenatura vuole anche le stazioni e le loro sezioni (passi 2 e 3).

| Pulsante | Che cosa fa |
|---|---|
| *Prua e poppa...* | Fin dove la ricostruzione può andare oltre la lunghezza rilevata: una x della nave per la prua, per la poppa, o nessuna. |
| *Ricostruisci...* | Simmetria, prolungamento delle fasce, ordinate al passo misurato e carenatura, ognuna facoltativa, in una costruzione sola. |
| *Usa il sistema nave di...* | Il sistema della nave (e le stazioni) di un altro chunk dello stesso relitto; le sezioni rifatte su questo modello. |
| *Tabella...* | Ciò che è misurato e ciò che è ricostruito, legno per legno: `tabella.csv` e `.xlsx`. |
| *Esporta 3D...* | I legni in 3D con la ricostruzione (OBJ, GLB, IFC), e la tabella accanto. |
| *Pianta con ricostruzione...* | La pianta del relitto con la ricostruzione campita. |

Altrove la ricostruzione è disegnata o esportata solo quando la si chiede: *Disegni* (passo 5), *Pianta del relitto* ed
*Esporta 3D* (passo 8) hanno la casella **Con la ricostruzione (ipotesi)**, spenta di partenza (i disegni e i file della
0.3.0); dalla riga di comando `drawings`, `plan` ed `export3d` accettano `--reconstruction`. Il piano di costruzione e
il profilo si disegnano senza la carenatura se la casella non è spuntata.

**Da ricostruire.** La ricostruzione ricorda da che cosa è stata costruita: il sistema della nave, il catalogo, la
chiglia data e il rilievo di questo chunk e degli altri chunk letti, la prua e la poppa date. Quando uno di questi cambia
dopo la costruzione (una fascia corretta, la prua scambiata, il rilievo rifatto...), lo stato del passo 9, le note sotto
i suoi pulsanti e l'albero (*Ricostruzione (n, ipotesi, da rifare)*) dicono **da ricostruire** e perché; la tabella
segna le righe ricostruite (colonna `stale`, il titolo della cartella di lavoro), la pianta disegnata con la
ricostruzione porta una nota e l'esportazione 3D lo dice. Costruita in un altro sistema della nave, i suoi legni in 3D
starebbero altrove: l'esportazione 3D li rifiuta finché non ricostruisci, e la pianta lascia fuori la carenatura.

Ogni costruzione, e ogni modifica di prua e poppa, è un passo di *Modifica > Annulla*: annullare una costruzione
riporta la ricostruzione di prima, record e file. Per questo la cartella della ricostruzione è copiata una volta per
costruzione in `shipwreck/reconstruction.undo/` (tolta alla sessione successiva); le copie sono collegamenti fisici e
non occupano quasi nulla, ma su un disco senza collegamenti fisici (FAT, exFAT, alcune condivisioni di rete) ognuna è
una copia intera della cartella. L'albero dell'area di lavoro mostra *Ricostruzione (n, ipotesi)*;
mentre il rilievo è mostrato, il layer *Ricostruzione (ipotesi)* disegna sopra i legni ricostruiti campiti e a trattini
(non si modificano a mano: eliminarli lì è rifiutato, con l'indicazione di come toglierli).

![La ricostruzione dello scafo sintetico sulla pianta del relitto](../img/plugins/shipwreck/shipwreck-reconstruction-plan.png)

### Che cosa viene ricostruito, e i limiti

- **Simmetria.** Un legno del lato conservato è riflesso rispetto al piano di chiglia dove l'altro lato non ha né il
  modello né un record del suo tipo: così come il relitto giace, quindi uno scafo torto o schiacciato è riflesso torto.
  Dove entrambi i lati sono misurati non si riflette niente e la differenza è elencata (la tabella dei disaccordi dei
  due lati, stazione per stazione): un relitto deformato dà disaccordi, non pezzi riflessi. Pezzi più piccoli di
  25 cm², più stretti di 5 cm (a meno che il legno stesso sia stretto) o sopra il modello non sono riflessi.
- **Prolungamento delle fasce.** Una fascia ricalcata su una parte dello scafo continua lungo i suoi due comenti:
  entro la lunghezza rilevata (il rilievo dei chunk) e oltre solo fino a una prua o a una poppa date. I comenti seguono
  le linee dei gradini delle sezioni (passo 7) dove ci sono, altrimenti la curvatura dei loro punti per un metro al
  massimo, poi tenuti dritti: quei pezzi sono segnati `uncertain` (incerti). Un pezzo prolungato è tagliato dove si
  allarga oltre 1,5 volte la larghezza misurata della fascia, o dove il suo bordo esterno supera di più di quella
  larghezza la mezza larghezza dello scafo a quella x (la cella più lontana del rilievo da quel lato; dove quel lato è
  perso, quella dell'altro lato alla stessa x, come assume la simmetria): linee dei gradini rumorose non aprono più una fascia a ventaglio fuori dal relitto.
  Un pezzo tagliato è `uncertain` e nominato negli avvisi.
- **Ordinate al passo misurato.** Dove lo spazio fra due ordinate ricalcate supera di 1,5 volte lo spazio mediano del
  loro lato, le ordinate mancanti sono rimesse a quel passo, in piedi sullo scafo (dal rilievo, dai solidi riflessi o
  dalla carenatura) alte quanto le due vicine.
- **Carenatura.** Una superficie per le sezioni alle stazioni (una mezza sezione con pochi punti prende quella
  dell'altro lato), tenuta solo dove il relitto non mostra niente. Oltre l'ultima stazione si stringe fino
  all'estremità data con una quadratica dalla larghezza e dalla pendenza dell'ultima stazione, con l'altezza
  dell'ultima sezione: un'ipotesi della sola larghezza. Senza prua o poppa date quell'estremità resta aperta all'ultima
  stazione (la pianta scrive *carenatura aperta*). I legni non ricalcati sul fasciame entrano nelle sezioni:
  ricalcali (il paramezzale prima di tutto), altrimenti lo scafo passa sopra di loro.
- **La chiglia** data al passo 8 resta un solido suo; non entra mai nella carenatura.
- **Il profilo** non mostra l'orlo su un tratto perso da entrambi i lati: il bordo alto della carenatura non è
  disegnato come orlo.
- **Senza la simmetria** (la casella spenta, `--no-mirror`) il prolungamento lascia comunque alla simmetria i tratti
  dove un lato è perso e l'altro conservato: lì è ricostruita solo la carenatura, e la costruzione lo dice.

### Più chunk dello stesso relitto

Altre campagne o altre parti del relitto in altri chunk entrano nella ricostruzione del chunk attivo quando sono
compatibili: allineati a lui (*Flusso di lavoro > Allinea chunk*) con lo stesso sistema della nave (*Usa il sistema
nave di...* lo copia), oppure con un sistema della nave preso da marker degli stessi nomi. Ognuno ha bisogno del suo
rilievo e dei suoi legni ricalcati; le ordinate devono avere i numeri del cantiere (*Nomi da tabella...*). I record
dello stesso tipo, lato e nome sono uniti nella pianta della nave, con il contorno del chunk misurato meglio dove
entrambi hanno rilevato; ordinate numerate in modo diverso sono unite per posizione; le quote vengono da un rilievo
unito, ogni cella dal chunk più fine che la rileva (un modello ravvicinato della prua vince sul relitto intero
rilevato più largo; fra chunk della stessa cella, quello che copre di più); la carenatura passa per le stazioni di tutti
i chunk. I chunk esclusi sono
elencati con il motivo (`chunks` dalla riga di comando) e gli avvisi dell'unione sono detti dopo la costruzione. Si
scrivono solo i file del chunk attivo.

### La tabella

`tabella.csv` (codici) e `tabella.xlsx` (titoli): una riga per record misurato, poi per record ricostruito, poi la
carenatura, con `id`, `name`, `kind`, `side`, `status`, `measured_length`, `measured_area` (un record misurato),
`reconstructed_length`, `reconstructed_area` (ogni altro), `length_kind`: `3D` per una linea misurata lungo il modello,
`pianta` per una lunghezza nella pianta della nave (il lato lungo di un poligono, un record unito da più chunk, la
ricostruzione), `stale` (perché la ricostruzione è da rifare, sulle sue righe; vuota finché è attuale). Le aree sono
nella pianta della nave. Con più chunk la tabella elenca i record uniti, come li usano la costruzione e i file. Il
totale ricostruito (nei totali della tabella e sulla pianta, «ricostruito: x m² senza la carenatura») è l'area
dell'unione dei legni ricostruiti: la carenatura, che sta sotto di loro, è data a parte nella sua riga, e dove due legni
ricostruiti si sovrappongono (un'ordinata riflessa sopra una fascia riflessa) l'area è contata una volta sola.

### Campiture e file 3D

Sulla pianta, una campitura per modo di ricostruire: righe oblique `////` per la simmetria (e per la chiglia data e le
fasce delle sezioni), righe oblique contrarie `\\\\` per il prolungamento, righe incrociate `xxxx` per le ordinate al
passo, righe orizzontali `----` per le tavole importate dal Clinker Boat Tool; la carenatura è solo il contorno a
trattini. Il DXF ha i layer `RICOSTRUZIONE_<TIPO>` con le campiture ANSI31, ANSI32, ANSI37 e ANSI33 e
`RICOSTRUZIONE_HULL`. Nei file 3D ogni oggetto ricostruito finisce in `_ric`, più chiaro e trasparente; nell'IFC il suo
stato è nel Pset, e una mesh non chiusa (la carenatura) è scritta con `Closed=False`.

![I legni dello scafo sintetico in 3D con la ricostruzione (più chiara, a righe)](../img/plugins/shipwreck/shipwreck-reconstruction-3d.png)

Sullo scafo sintetico (lo scafo a clinker con ordinate dei test, il babordo perso da metà nave a poppa ed entrambi i
lati su 0,9 m): i contorni delle fasce riflesse stanno entro 1,1 mm (mediana; p95 4,3 mm) dalle fasce ricalcate sullo
scafo intero, il 99 % entro 1 cm, le loro facce entro 4 mm (p95 8 mm) dalla sua superficie; fasce ricalcate su x 2-9
prolungate fino alle estremità rilevate con i bordi entro 1 mm (p95 3 mm) di traverso e 1 mm (p95 7 mm) in quota dai
comenti veri; le quattro ordinate mancanti rimesse dov'erano; la carenatura entro 0,6 mm (p95) dalla sezione vera; una
costruzione di tutta la ricostruzione 1,4 s, il suo IFC (68 elementi, 33 `_ric`) 7 s. Due chunk dello stesso scafo
(0-7 m e 6-12 m) danno una ricostruzione sola su tutta la lunghezza rilevata.

## Riga di comando

```bash
ppg plugin shipwreck frame PROGETTO.ppg --start X,Y --end X,Y --bow start   # --flip, --other-bow
ppg plugin shipwreck stations PROGETTO.ppg --spacing 1 --count 11 --first 1
ppg plugin shipwreck sections PROGETTO.ppg --longitudinals "0;1;-1" --horizontals "-0.2;-0.5" --half-width 4.5
ppg plugin shipwreck water PROGETTO.ppg --level -0.1 --on
ppg plugin shipwreck drawings PROGETTO.ppg --scale 20
ppg plugin shipwreck timbers PROGETTO.ppg --table legni.csv
ppg plugin shipwreck export PROGETTO.ppg --out legni.xlsx
ppg plugin shipwreck status PROGETTO.ppg
ppg plugin shipwreck clinker detect PROGETTO.ppg [--min-step MM] [--min-strake M]   # stampa le stazioni incerte
ppg plugin shipwreck clinker strakes PROGETTO.ppg [--lap MM] [--thickness MM] [--catalogue]
ppg plugin shipwreck clinker export PROGETTO.ppg --out CARTELLA
ppg plugin shipwreck clinker import PROGETTO.ppg --obj TAVOLE.obj [--side-from y|name] [--no-deviation]
ppg plugin shipwreck clinker status PROGETTO.ppg                                    # dice se i gradini sono superati
ppg plugin shipwreck relief build PROGETTO.ppg [--cell MM]
ppg plugin shipwreck relief export PROGETTO.ppg --kind height|hillshade|slrm --out FILE.tif [--world]
ppg plugin shipwreck propose PROGETTO.ppg [--view inside|outside] [--min-step MM] [--opening M] [--top-hat MM]
ppg plugin shipwreck trace list PROGETTO.ppg
ppg plugin shipwreck trace accept PROGETTO.ppg ID... [--name NOME]     # oppure --kind frame [--min-score 0.5]
ppg plugin shipwreck trace reject PROGETTO.ppg ID...
ppg plugin shipwreck trace names PROGETTO.ppg --table FILE             # nome, x: i nomi delle ordinate
ppg plugin shipwreck keel PROGETTO.ppg --siding MM --moulding MM [--from X --to X]   # oppure --none
ppg plugin shipwreck solids PROGETTO.ppg [--thickness MM]
ppg plugin shipwreck export3d PROGETTO.ppg --out FILE.obj|.glb|.ifc [--world] [--no-tables]
ppg plugin shipwreck plan PROGETTO.ppg [--background relief|ortho|none] [--scale N] [--out CARTELLA]
ppg plugin shipwreck status PROGETTO.ppg                               # con i conteggi dei legni 3D
ppg plugin shipwreck ends PROGETTO.ppg [--stem X] [--stern X]          # oppure --none: solo entro la lunghezza rilevata
ppg plugin shipwreck frame-of PROGETTO.ppg CHUNK [--chunk QUESTO]      # il sistema della nave di un altro chunk
ppg plugin shipwreck chunks PROGETTO.ppg                               # i chunk della ricostruzione, e perché no gli altri
ppg plugin shipwreck reconstruct PROGETTO.ppg [--no-mirror] [--no-extension] [--no-frames] [--no-loft]
ppg plugin shipwreck reconstruct table PROGETTO.ppg [--out CARTELLA]   # tabella.csv e .xlsx
ppg plugin shipwreck classified PROGETTO.ppg --layer NOME | --file FILE [--pair-isolation 1] [--plate FILE] [--accept [ID ...]] [--kinds frame,seam,plank]
ppg plugin shipwreck export3d PROGETTO.ppg --out FILE.obj|.glb|.ifc --reconstruction
ppg plugin shipwreck plan PROGETTO.ppg --reconstruction [--out CARTELLA]
ppg plugin shipwreck drawings PROGETTO.ppg --reconstruction [--out CARTELLA]   # piano e profilo con la carenatura
```

## Che cosa non fa

Idrostatica, sviluppo del fasciame, riconoscimento automatico dei legni oltre alle proposte lette sul rilievo, che accetti o scarti a mano, rifrazione
oltre l'approssimazione nadirale, unione di più giornate. Tutto quello che scrive sta in `<dati del chunk>/shipwreck/`, salvo quello che esporti; la
trasformazione, il modello e i prodotti del chunk non cambiano mai.
