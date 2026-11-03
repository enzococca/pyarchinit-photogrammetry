# Relitto

Il plugin *Relitto* fa il disegno tecnico di uno scafo scavato dal suo modello 3D: dove corre l'asse della nave, la forma dello
scafo a ogni stazione, i piani, e un elenco numerato dei legni con le loro misure. Oggi tutto questo si disegna a mano su un'ortofoto.
Il plugin misura quello che c'è. Non ricostruisce quello che manca e non sposta una linea dal modello per carenarla.

Questa pagina descrive la versione 0.2.0 del plugin, che richiede pyarchinit-photogrammetry 1.7.0 o successiva. Il README e il codice
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
```

## Che cosa non fa

Ricostruzione dello scafo, lati specchiati, superfici lofted, idrostatica, sviluppo del fasciame, riconoscimento automatico dei legni oltre ai gradini del fasciame a clinker, rifrazione
oltre l'approssimazione nadirale, unione di più giornate. Tutto quello che scrive sta in `<dati del chunk>/shipwreck/`, salvo quello che esporti; la
trasformazione, il modello e i prodotti del chunk non cambiano mai.
