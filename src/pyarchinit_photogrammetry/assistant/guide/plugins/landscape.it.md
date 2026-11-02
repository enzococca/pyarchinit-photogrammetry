# Paesaggio nel tempo

Il plugin *Paesaggio nel tempo* simula sul DEM del progetto come cambia la superficie negli anni per
due processi lenti: il **creep del suolo**, il terreno che scivola un poco ogni anno giù per ogni
pendio, e l'**acqua che scorre**, che erode dove si raccoglie e depone il suo carico dove rallenta.
Risponde a domande come: come si consuma un tell in mattoni crudi e fin dove arriva il suo colluvio al
piede, dove uno scavo lasciato aperto perde le sue sezioni in dieci anni, dove finisce il sedimento di
un versante. La simulazione è fatta da [Landlab](https://landlab.csdms.io) (CSDMS, Community Surface
Dynamics Modeling System), che il plugin fa girare in un processo a sé; i risultati tornano sulla
griglia del DEM: superficie finale, erosione e deposito, area drenata, superfici intermedie e il
bilancio dei volumi spostati.

Questa pagina descrive la versione 0.1.0 del plugin, che richiede pyarchinit-photogrammetry 1.3.4 o
successiva. Il README e il codice sono nel
[repository dei plugin](https://github.com/enzococca/pyarchinit-photogrammetry-plugins/tree/main/plugins/landscape).

<figure markdown="span">
  ![Erosione e deposito di un tell in 1.000 anni](../img/plugins/landscape/dz.jpg)
  <figcaption>Lo scenario <em>Tell in mattoni crudi</em> per 1.000 anni: in blu dove la superficie si
  abbassa, in rosso dove si alza - i colori del pannello - sopra l'ombreggiatura del DEM. La cima e le
  spalle del tell perdono 20-40 cm, il piede dei pendii e le conche ne guadagnano altrettanti; i tagli
  dei vecchi scavi perdono i loro margini e si riempiono. Il DEM reale di Tell Sultan, in un sistema di
  riferimento locale; colori fra -0,55 e +0,55 m.</figcaption>
</figure>

!!! note "Gli esempi di questa pagina"
    Le figure vengono dal DEM di Tell Sultan - il sito del [tutorial](../tutorials/tell.md) - a 19 cm,
    2.876 x 2.032 celle, in un sistema di riferimento locale, simulato su celle di 1,14 m; il pannello è
    stato fotografato sul progetto dimostrativo. I tempi sono stati misurati su un computer recente (un
    Apple M4 Pro). Mostrano che cosa fa il plugin e non sono uno studio del sito.

!!! warning "Scenari, non previsioni"
    Il risultato dipende dai parametri, e i parametri di un versante reale si conoscono nel migliore dei
    casi entro un ordine di grandezza. Leggi una simulazione come "che cosa farebbero questi processi, a
    queste velocità, a questa superficie", confrontane diverse e, quando hai il DEM di una fase
    successiva, usa il confronto per tarare i parametri. Il tempo va solo in avanti: il plugin non
    ricostruisce una superficie più antica facendo girare il modello all'indietro, cosa che non è fisica
    (la diffusione all'indietro amplifica ogni errore del DEM).

## In breve

1. Costruisci il DEM del chunk (*Flusso di lavoro > Costruisci DEM...*), meglio dalla sola classe
   *Terreno*.
2. Apri il pannello *Paesaggio* (*Plugin > Paesaggio nel tempo (Landlab) > Paesaggio nel tempo...*).
   La prima volta *Installa Landlab...* mette Landlab in un ambiente del plugin (circa 500 MB, qualche
   minuto, una volta sola).
3. Scegli uno scenario, gli anni e le superfici intermedie da conservare; leggi la stima della durata;
   *Avvia la simulazione*.
4. Guarda l'erosione e il deposito sopra l'ortofoto o il DEM; passa alla superficie finale, all'area
   drenata o a una superficie intermedia; leggi il bilancio dei volumi sotto la tabella.
5. Aggiungi la superficie finale fra le versioni del DEM, confrontala con il DEM di un chunk
   successivo, esportala in GeoTIFF.

## Che cosa fa

| Processo | Che cosa fa alla superficie | Componente di Landlab | Parametri |
|---|---|---|---|
| Creep del suolo (diffusione del versante) | il suolo si sposta verso valle in proporzione alla pendenza: cime e margini convessi si abbassano, i piedi concavi si riempiono; i pendii diventano più arrotondati e più dolci | `LinearDiffuser` | diffusività D (m²/anno) |
| Acqua che scorre (SPACE) | l'acqua si raccoglie lungo le linee di maggior drenaggio, erode uno strato di suolo sciolto e, sotto, il corpo del rilievo; il sedimento viaggia e si deposita dove l'acqua rallenta, la parte più fine va via in sospensione | `SpaceLargeScaleEroder` | erodibilità del suolo e del substrato, ruscellamento, spessore del suolo, deposito, porosità, frazione fine |

L'acqua segue la più ripida delle otto celle vicine (D8); le conche della superficie sono riempite nel
percorso dell'acqua, così che l'acqua prosegua, e il sedimento vi si deposita. Si può spegnere uno dei
due processi, non entrambi. Non sono simulati: piogge che cambiano nel tempo, vegetazione, vento, gelo,
animali e persone, solchi che si spostano di lato, tettonica, quello che sta sotto la superficie.

Serve dove la forma del terreno cambia lentamente e la domanda è "quanto, e dove": il consumo di un tell
o di un tumulo, il colluvio al piede di un pendio che seppellisce i livelli sotto, le sezioni e i muri
di uno scavo lasciato aperto, l'effetto di un terrazzo o di un fosso sull'acqua. Non è un modello di
un singolo temporale, né di una frana.

## Che cosa serve prima { #before }

* **Il plugin**, installato dal programma: vedi [Guide ai plugin](guides.md#install).
* **Landlab**, una libreria Python libera (licenza MIT) con i suoi componenti compilati (richdem per il
  percorso dell'acqua, netCDF4). Il programma non la carica mai: ogni simulazione gira in un processo
  separato, con
    * il Python del programma, se Landlab è installato lì (`pip install landlab` nell'ambiente Python
      del programma);
    * altrimenti un ambiente del plugin: Python 3.12 e Landlab 2 da PyPI, installati con
      [uv](https://github.com/astral-sh/uv) - circa 500 MB e qualche minuto, con internet quella volta.
      È così che funziona l'applicazione desktop, che non ha pip. Il pannello lo propone (*Installa
      Landlab...*), e così fanno *Plugin > Paesaggio nel tempo (Landlab) > Installa Landlab...* e
      `ppg plugin landscape install`. L'ambiente va in `plugins/_data/landscape/env/` nella cartella
      dei plugin del programma (vedi [Plugin](../plugins.md)); la variabile `PPG_LANDSCAPE_ENV` lo mette
      altrove.

    Landlab ha versioni per macOS (Apple Silicon; Intel con macOS 15 o successivo), Linux (x86-64 e ARM)
    e Windows su x86-64. Non ce n'è per Windows su ARM: lì il pannello dice che le simulazioni non sono
    disponibili. La riga sotto la descrizione del pannello dice quale Landlab usa il plugin.

* **Un chunk con un DEM** (*Flusso di lavoro > Costruisci DEM...*), o una sua versione conservata. Al
  plugin non serve altro: né foto né nuvola.
* **Metri.** Un sistema di riferimento proiettato, o un sistema locale in metri: i parametri sono in
  metri e anni, e un DEM in gradi viene rifiutato. Il confronto con un altro chunk vuole i due chunk
  nello stesso sistema.

## Il DEM { #dem }

### Superficie o terreno { #dsm-dtm }

Un DEM costruito da tutti i punti della nuvola densa è un modello della superficie: cespugli, alberi,
tende, cumuli di terra di scavo e persone ne fanno parte, e per la simulazione sono terreno - un albero
è un rilievo che scivola, un'auto parcheggiata una diga che l'acqua aggira. Costruisci il DEM dalla
classe *Terreno* (*Strumenti > Classificazione della nuvola*, oppure il plugin [terrio](terrio.md)),
rendilo la versione attiva o sceglilo come *DEM di partenza*.

### La griglia della simulazione { #cell }

La simulazione gira su celle più grandi di quelle del DEM: Landlab procede per passi, e una griglia di
milioni di celle richiederebbe ore. In modo predefinito il plugin ingrandisce la cella del DEM finché il
lato lungo ha al massimo 500 celle (il DEM delle figure, 19 cm, diventa 480 x 339 celle di 1,14 m);
*Cella (m)* ne fissa un'altra. Il DEM viene mediato su quella griglia. Alla fine quello che la
simulazione ha **cambiato** - il dz di ogni cella, e ogni superficie intermedia meno la partenza - viene
interpolato sulla griglia del DEM e **sommato al DEM**: la superficie finale conserva ogni pietra e ogni
margine che la simulazione più grossolana non poteva vedere, e li sposta con il terreno intorno. Lungo
il margine più esterno dei dati, dove l'interpolazione vorrebbe celle senza valore, il DEM resta com'era
(dz 0).

La cella è anche una scelta sui processi: il creep agisce su ogni pendio alla scala della cella,
l'acqua si raccoglie in canali larghi almeno una cella. Una cella di 1-2 m va bene per un tell o un
versante; 10-50 cm per uno scavo, con meno anni.

### Bordi e buchi { #edges }

Le celle senza valore stanno fuori dalla simulazione. Al bordo dei dati scegli:

* **Bordo aperto** (predefinito): acqua e sedimento escono, come su un pendio che continua oltre il
  rilievo. Anche il creep scambia suolo attraverso il bordo.
* **Bordo chiuso**: non esce niente se non dagli sbocchi. Con l'acqua che scorre ogni gruppo di celle
  interne al bordo tiene uno sbocco, la sua cella di bordo più bassa: l'area principale, e anche le
  isole di poche celle che un DEM ha spesso accanto e le parti unite a essa solo da una strozzatura.
  Usalo per un'area chiusa - un cortile, l'interno di una trincea - e con il solo creep, che allora
  conserva il volume.

Su Tell Sultan, *Versante in terra* per 1.000 anni con il bordo chiuso su celle di 2 m (274 x 194) ha
tenuto 8 sbocchi - quello principale e sette per isole e strozzature - e ha richiesto 59 s; ne sono
usciti 4.971 m³ di materiale solido. Un buco dentro i dati (una pozza, un telo rimasto fuori dalla
nuvola) è chiuso: l'acqua non esce di lì. Poche celle con un valore dentro un buco così - un'isola,
frequente su una griglia di simulazione grossolana - sarebbero un lago senza uscita: con tutti e due i
bordi tengono un loro sbocco, la cella più bassa accanto al buco, e quello che esce da lì conta fra le
uscite. Se Landlab trovasse comunque un lago senza uscita, la simulazione viene fermata con un messaggio
(non finirebbe mai) che dice che cosa provare, e non scrive niente.

### Quanto dura { #time }

Prima di una simulazione il pannello dà una stima: la griglia, i passi e i minuti. I passi della
diffusione sono esatti (nessun passo può essere più lungo di 0,14 dx²/D, quindi crescono con il quadrato
della cella: metà cella, quattro volte i passi); quelli dell'acqua sono un limite superiore, dall'erosione
più rapida possibile, e la simulazione vera di solito è molto più breve. Oltre 10 minuti la riga diventa
arancione; oltre 2 ore *Avvia la simulazione* è disattivato (`--force` dalla riga di comando). Su Tell
Sultan lo scenario *Tell in mattoni crudi* per 1.000 anni su 480 x 339 celle aveva una stima di 9 minuti
e ha richiesto 46 s, con 184 passi.

Il DEM non è mai tutto in memoria: viene letto a finestre per fare la griglia della simulazione, e i
risultati tornano sulla sua griglia a blocchi di righe, così un DEM di 100 milioni di celle chiede circa
la memoria di un blocco. Scriverli però richiede tempo: oltre 50 milioni di celle la stima lo dice.

## Passo per passo nel programma { #program }

### Il pannello

Una volta installato il plugin e riavviato il programma, il pannello *Paesaggio* si apre a destra nella
finestra, fra le schede di *PyArchInit*; si sposta, si stacca o si chiude come gli altri pannelli e si
riapre da *Vista > Pannelli*, oppure con *Plugin > Paesaggio nel tempo (Landlab) > Paesaggio nel
tempo...*.

<figure markdown="span">
  ![Il pannello Paesaggio dopo una simulazione](../img/plugins/landscape/pane.png)
  <figcaption>Il pannello dopo una simulazione dello scenario <em>Scavo esposto</em> sul progetto
  dimostrativo: lo scenario, la stima, la simulazione nella tabella con la sua erosione e il suo deposito
  mostrati e il bilancio dei volumi.</figcaption>
</figure>

Dall'alto: una breve descrizione e l'avviso che sono scenari; il Landlab in uso (o *Installa
Landlab...*); lo scenario; la stima e *Avvia la simulazione*; la tabella delle simulazioni del chunk;
*Mostra* e *Opacità*; *Sovrapponi alla vista*, *Nascondi*, *Aggiungi come versione del DEM*, *Confronta
con un altro chunk...*, *Esporta...*, *Rimuovi*; i valori ai due estremi dei colori mostrati e il
bilancio della simulazione scelta.

### 1. Lo scenario { #scenario }

| Campo | Predefinito | Che cosa fa |
|---|---|---|
| *DEM di partenza* | quello attivo | il DEM da cui parte la simulazione: quello attivo o una versione conservata (una fase precedente, il risultato di un'altra simulazione) |
| *Scenario* | *Tell in mattoni crudi* | uno scenario riempie i campi sotto; cambiando un valore diventa *Personalizzato* |
| *Anni* | quelli dello scenario | gli anni simulati |
| *Istantanee (anni)* | un decimo e un centesimo degli anni | superfici intermedie da conservare, separate da `;` o spazi (`10; 100; 500`) |
| *Cella (m)* | automatica | la cella della griglia della simulazione (vedi [sopra](#cell)) |
| *Bordo* | aperto | *Bordo aperto (acqua e sedimento escono)* o *Bordo chiuso (esce solo dal punto più basso)* (vedi [sopra](#edges)) |
| *Passo (anni)* | automatico | il passo più lungo: in modo predefinito gli anni / 100; la simulazione lo accorcia da sé dove serve alla stabilità |
| *Creep del suolo* | acceso | la diffusione del versante, con *Diffusività del versante D (m²/anno)* |
| *Acqua che scorre (SPACE)* | acceso | erosione e deposito, con l'erodibilità del suolo e del substrato, il ruscellamento e lo spessore del suolo sciolto |
| *Parametri avanzati* | spento | mostra la frazione fine, la porosità, la scala dello spessore H\*, la velocità di deposito e i due esponenti dell'acqua |

Ogni campo ha un suggerimento che dice che cos'è. La stima sotto il modulo cambia con i valori. *Avvia
la simulazione* la fa girare in sottofondo con la barra di avanzamento (*Landlab: anno 384 di 1.000*);
*Annulla* ferma subito Landlab e non lascia niente. Alla fine la simulazione è nella tabella, la sua
erosione e il suo deposito sono stesi sulla vista e il progetto risulta modificato: salvalo per tenere l'elenco
delle simulazioni con il progetto.

### 2. Gli strati di una simulazione { #layers }

Scegli una simulazione nella tabella, poi uno strato in *Mostra*:

| Strato | Che cos'è | Colori |
|---|---|---|
| *Erosione e deposito (dz)* | la superficie finale meno il DEM di partenza, m | blu-bianco-rosso, simmetrico intorno a zero (blu giù, rosso su) |
| *Superficie finale* | la superficie dopo gli anni simulati | dal basso all'alto, colori della terra |
| *Area drenata* | l'area che drena attraverso ogni cella della superficie finale, m² | azzurri su scala logaritmica |
| *Superficie dopo N anni* | ogni istantanea | come la superficie finale |
| *Differenza con il DEM di «chunk»* | dopo un confronto, la superficie finale meno quel DEM | blu-bianco-rosso (blu: la simulazione più bassa di quel DEM) |

<figure markdown="span">
  ![L'erosione e il deposito di una simulazione sopra l'ortofoto](../img/plugins/landscape/overlay.png)
  <figcaption>Dieci anni dello scenario <em>Scavo esposto</em> sulla trincea del progetto
  dimostrativo, sopra l'ortofoto: il fondo della trincea si riempie (rosso, al massimo 33 cm), il terreno
  intorno perde materiale (blu; l'erosione più forte è di 18 cm).</figcaption>
</figure>

*Sovrapponi alla vista*, un doppio clic sulla riga o la scelta in *Mostra* lo stende sopra l'ortofoto o
il DEM (il DEM viene caricato se la vista è vuota); *Opacità* lo cambia subito, *Nascondi* (o *Plugin >
Paesaggio nel tempo (Landlab) > Nascondi la sovrapposizione del paesaggio*) lo toglie. I colori vanno
dal 2° al 98° percentile dei valori; la riga sotto i pulsanti dice quali.

Le simulazioni sono anche nell'albero dell'*Area di lavoro*, sotto *Paesaggio nel tempo*: un doppio clic
mostra il dz di una simulazione, il menu del tasto destro ogni strato, *Aggiungi come versione del DEM*
e *Rimuovi*. Il progetto QGIS del programma ([QGIS](../integrations.md)) ha la superficie finale, il dz e
l'area drenata di ogni simulazione (per un chunk georeferenziato); con il programma 1.4.0 o successivo
gli strati sono anche nel calcolatore raster.

### 3. Una versione del DEM { #version }

*Aggiungi come versione del DEM* copia la superficie finale fra le versioni conservate del DEM, con il
nome *Paesaggio dopo 1.000 anni (nome della simulazione)*. Il DEM attivo non cambia. Rendi attiva la
nuova versione - tasto destro su di essa nell'*Area di lavoro* - e curve di livello, sezioni, profili,
volumi e rilievo lavorano sulla superficie simulata; rendi di nuovo attiva quella di prima per tornare
indietro. Una versione fatta da una simulazione resta quando la simulazione viene rimossa.

### 4. Confronto con una fase successiva { #compare }

Quando il progetto ha il DEM di una fase successiva - un altro chunk, rilevato anni dopo - *Confronta con
un altro chunk...* chiede quale chunk e calcola la superficie finale meno il suo DEM attivo, ricampionato
sulla griglia della simulazione. Il riepilogo dà la mediana, la NMAD e il 95° percentile della
differenza assoluta, accanto agli stessi numeri per il DEM **di partenza** meno quello successivo: i
parametri vanno nella direzione giusta quando la differenza si riduce. La differenza è uno strato della
simulazione (*Differenza con il DEM di ...*). I due chunk devono essere nello stesso sistema di
riferimento; le aree cambiate per altri motivi (uno scavo nel frattempo, nuovi cumuli di terra) pesano
sui numeri: confronta sulle parti lasciate stare.

### 5. Esportazione e rimozione { #export }

*Esporta...* scrive lo strato mostrato, o tutti gli strati della simulazione in un unico GeoTIFF con una
banda con nome per ciascuno (`final`, `dz`, `area`, `anni_100`...). *File > Esporta > Paesaggio nel
tempo (GeoTIFF)* esporta tutti gli strati dell'ultima simulazione. *Rimuovi* chiede conferma, poi
cancella i file della simulazione.

## Scenari e parametri { #parameters }

| Scenario | Anni | D (m²/anno) | K_sed | K_br | F_f | phi | H\* (m) | v_s (m/anno) | Ruscellamento (m/anno) | Suolo (m) |
|---|---|---|---|---|---|---|---|---|---|---|
| *Tell in mattoni crudi* | 1.000 | 0,01 | 0,0005 | 0,0001 | 0,5 | 0,3 | 0,5 | 1 | 0,2 | 0,5 |
| *Versante in terra* | 1.000 | 0,005 | 0,001 | 0,0002 | 0,2 | 0,3 | 0,5 | 1 | 0,3 | 0,5 |
| *Scavo esposto* | 10 | 0,02 | 0,002 | 0,0005 | 0,3 | 0,3 | 0,2 | 1 | 0,3 | 0,2 |

Gli esponenti della legge di erosione sono 0,5 (drenaggio) e 1 (pendenza) in tutti gli scenari. I
valori sono punti di partenza difendibili scelti per il plugin, nella parte alta degli intervalli
pubblicati per materiale sciolto e nudo. Sono **indicativi**:

* **D, diffusività del versante.** Il modello del creep come diffusione risale a Culling (1963).
  Misurata su versanti naturali con copertura di suolo, D sta per lo più fra 0,0001 e 0,01 m²/anno:
  circa 0,003 nella Oregon Coast Range (Roering, Kirchner e Dietrich 1999), circa 0,001 dal degrado delle
  scarpate di faglia e delle antiche linee di riva (Hanks et al. 1984). Il materiale nudo e sciolto si
  muove più in fretta del suolo con vegetazione: gli scenari mettono un tell in mattoni crudi a 0,01 e
  uno scavo non protetto a 0,02, apposta sopra l'intervallo naturale, e un versante in terra inerbito a
  0,005.
* **K_sed e K_br, erodibilità del suolo e del substrato.** SPACE (Shobe, Tucker e Barnhart 2017) erode
  uno strato di suolo sciolto a `K_sed Q^m S^n (1 - e^(-H/H*))` e il materiale sotto a
  `K_br Q^m S^n e^(-H/H*)` (Q la portata, S la pendenza, H lo spessore del suolo). Le loro unità
  dipendono dagli esponenti e dalla portata (qui in m³/anno): un valore è confrontabile solo con gli
  stessi esponenti. Il suolo sciolto è più erodibile di quello che sta sotto (5 volte negli scenari).
  Sono i valori meno noti: gli esempi che accompagnano SPACE usano valori da 0,00001 a 1, per bacini che
  cambiano in millenni; gli scenari restano bassi perché i pendii di un tell sono corti e i mattoni crudi
  sono coesivi. Tarali.
* **F_f**, la frazione del materiale eroso sotto il suolo così fine da andare via in sospensione senza
  depositarsi: alta per mattoni crudi e riempimenti limosi.
* **phi**, la porosità del sedimento depositato: 0,3 è comune per sabbie e ghiaie (0,2-0,5).
* **H\***, lo spessore di suolo oltre il quale l'acqua non raggiunge più il materiale sotto.
* **v_s**, la velocità efficace di deposito: molto maggiore del ruscellamento, il sedimento si deposita
  vicino a dove è stato eroso; molto minore, viaggia lontano ed esce dal rilievo (Shobe et al. 2017
  discutono il passaggio fra i due).
* **Ruscellamento**, la pioggia che scorre in superficie in un anno, uniforme (m/anno): 0,2-0,3 m per un
  clima da semiarido a mediterraneo, tolta l'infiltrazione.

## Dalla riga di comando { #cli }

Le stesse cose con `ppg plugin landscape COSA PROGETTO ...`. I comandi lavorano sul chunk attivo
(`--chunk ETICHETTA` per un altro); quelli che cambiano il progetto lo salvano.

```bash
ppg plugin landscape status
ppg plugin landscape install
ppg plugin landscape estimate scavo.ppg --preset mudbrick_tell --years 1000
ppg plugin landscape run scavo.ppg --preset mudbrick_tell --years 1000 --snapshots 100,500 --name "Tell, 1000 anni"
ppg plugin landscape run scavo.ppg --preset custom --years 50 --D 0.02 --no-water --boundary closed
ppg plugin landscape run scavo.ppg --preset earth_slope --cell 2 --K-sed 0.002 --runoff 0.4
ppg plugin landscape list scavo.ppg
ppg plugin landscape show scavo.ppg r20261030-073806
ppg plugin landscape version scavo.ppg r20261030-073806
ppg plugin landscape compare scavo.ppg r20261030-073806 --with "Fase 2"
ppg plugin landscape export scavo.ppg r20261030-073806 paesaggio.tif
ppg plugin landscape export scavo.ppg r20261030-073806 dz.tif --layer dz
ppg plugin landscape remove scavo.ppg r20261030-073806
```

| Comando | Opzioni |
|---|---|
| `status` | dove gira Landlab, e una sua prova |
| `install` | l'ambiente del plugin; `--force` anche se Landlab c'è già |
| `estimate PROGETTO` | le opzioni di `run`: griglia, passi e minuti, senza far girare niente; `--json` |
| `run PROGETTO` | `--preset` `mudbrick_tell` (tell in mattoni crudi), `earth_slope` (versante in terra), `exposed_excavation` (scavo esposto) o `custom`; `--years`; `--snapshots 10,100`; `--cell` m (0: automatica); `--boundary open` o `closed`; `--dt` passo più lungo, anni; `--source` l'id di una versione del DEM; `--no-diffusion`, `--no-water`; i parametri `--D --K-sed --K-br --runoff --soil-depth --F-f --phi --H-star --v-s --m-sp --n-sp`; `--name`; `--force` oltre due ore; `--json` tutta la voce |
| `list PROGETTO` | le simulazioni con id, nome, anni, cella, scenario, DEM di partenza, volumi |
| `show PROGETTO ID` | una simulazione: parametri, bilancio, confronti |
| `version PROGETTO ID` | la superficie finale fra le versioni del DEM |
| `compare PROGETTO ID --with ETICHETTA` | la superficie finale meno il DEM di un altro chunk; `--other-source` una sua versione |
| `export PROGETTO ID USCITA` | `--layer final`, `dz`, `area`, `snap:100` o `all` (predefinito: una banda per strato) |
| `remove PROGETTO ID` | la simulazione e i suoi file, senza chiedere |

## Leggere i risultati { #reading }

<figure markdown="span">
  ![Profilo attraverso il tell prima e dopo](../img/plugins/landscape/profile.jpg)
  <figcaption>Un profilo da ovest a est per la cima del tell: il DEM di partenza e le superfici dopo 100,
  500 e 1.000 anni dello scenario <em>Tell in mattoni crudi</em> (sopra), e il dz lungo il profilo
  (sotto, metri). La cima perde fino a 39 cm, i pendii alti e convessi 20-30 cm; il gradino di un vecchio
  taglio sul pendio ovest (a circa 235 m) si addolcisce, il suo piede si riempie di 55 cm. Distanze in
  metri dal margine ovest del rilievo.</figcaption>
</figure>

### Erosione e deposito { #reading-dz }

dz è la superficie finale meno il DEM di partenza, in metri: negativo dove il materiale se n'è andato,
positivo dove è arrivato. Il creep toglie a ciò che è convesso (cime, spalle, i margini di tagli e muri)
e dà a ciò che è concavo (piedi, conche, il fondo delle trincee); l'acqua che scorre incide linee lungo i
pendii e riempie dove rallenta. Su un tell il disegno è un arrotondamento: la cima abbassata, le spalle
consumate, un grembiule che cresce al piede. I valori più grandi, spesso metri, stanno sui gradini del
DEM - la sezione verticale di uno scavo, un muro, una fossa - che il creep addolcisce per primi: leggili
come "questo margine non resta", non come una misura.

### I volumi { #reading-balance }

Sotto la tabella, e in `balance.json`, una simulazione dà:

* il volume **eroso**, **depositato** e **netto** da dz, m³, vuoti compresi, sulla griglia della
  simulazione: sommando `dz.tif` sulla griglia del DEM eroso e depositato vengono più piccoli di qualche
  per cento, perché l'interpolazione smussa i picchi (su Tell Sultan 6.459 / 5.174 m³ contro
  6.816 / 5.525), mentre il netto è lo stesso;
* i volumi **solidi**: il suolo è poroso (phi), il materiale sotto no, quindi un metro cubo di mattoni
  crudi eroso diventa più di un metro cubo di deposito; i volumi solidi contano solo il materiale;
* quello che è **uscito dal rilievo**: con l'acqua come carico di fondo e come fini in sospensione, e con
  il creep attraverso un bordo aperto (negativo quando il creep ha portato dentro suolo da fuori, dove il
  bordo è più alto dell'interno); la cifra solida del creep comprende anche i vuoti che si formano dove
  il creep intacca il substrato e lo trasforma in suolo;
* la **chiusura**: solido eroso - solido depositato - tutto quello che è uscito. È circa zero quando i
  conti tornano, e i test lo controllano in ogni combinazione di bordo, frazione fine e porosità. La
  parte del creep è misurata come cambiamento solido dei suoi passi, quindi per il creep la chiusura
  torna per costruzione: quello che controlla sono i conti dell'acqua che scorre.

Su Tell Sultan, *Tell in mattoni crudi* per 1.000 anni (bordo aperto, 480 x 339 celle di 1,14 m):

| | m³ |
|---|---|
| Eroso / depositato (apparente) | 6.816 / 5.525 |
| Eroso / depositato (solido) | 5.169 / 3.807 |
| Uscito con l'acqua (fondo + fini) | 1.418 (1.043 + 375) |
| Creep attraverso il bordo, e vuoti formati (solido) | -56 (entrato) |
| Chiusura | 0,0000 |

Il cambiamento tipico è piccolo - la NMAD del dz sul DEM è 8 cm - mentre il 2 % delle celle si è
abbassato di più di 39 cm e il 2 % si è alzato di più di 39 cm.

### Area drenata { #reading-area }

<figure markdown="span">
  ![Area drenata della superficie finale](../img/plugins/landscape/area.jpg)
  <figcaption>L'area drenata della superficie finale dopo 1.000 anni: l'area che drena attraverso ogni
  cella, da 1 m² (bianco) a 38.000 m² (blu scuro), su scala logaritmica. L'acqua scende a raggiera dal
  tell e si raccoglie nelle parti basse e lungo i tagli intorno; le linee dritte in otto direzioni sui
  pendii lisci sono il segno del percorso D8.</figcaption>
</figure>

L'area, in m², che drena attraverso ogni cella della superficie finale: le linee di valori alti sono
dove l'acqua si concentra e dove l'acqua che scorre lavora. Torna sulla griglia del DEM con il vicino
più prossimo, così le linee hanno la larghezza della cella della simulazione.

### Istantanee { #reading-snapshots }

Ogni istantanea è una superficie sulla griglia del DEM, il DEM di partenza più il cambiamento fino a
quell'anno. Il creep rallenta man mano che i pendii si addolciscono: i primi secoli cambiano una
superficie spigolosa più dei successivi.

### Limiti { #limits }

* **Scenari.** Parametri noti entro un ordine di grandezza danno risultati entro un ordine di grandezza.
  Confronta gli scenari, cambia un valore alla volta, tara con un DEM successivo.
* **Due processi.** Niente singoli temporali, vegetazione, vento, gelo, animali, persone, frane o canali
  che si spostano di lato.
* **Solo la superficie.** Lo spessore del suolo e il substrato sono uniformi: il plugin non sa dove
  finiscono i mattoni crudi e comincia il terreno naturale.
* **La cella.** Ciò che è più piccolo della cella della simulazione si sposta con il terreno intorno,
  non è simulato: un muro largo una cella non si consuma da solo.
* **Solo in avanti.** Nessuna ricostruzione di una superficie passata.

## File scritti { #files }

Tutto va nella cartella del chunk, accanto al file del progetto:
`<progetto>.ppg.files/chunk_<n>/landscape/<id della simulazione>/`. L'elenco delle simulazioni, i loro
parametri e bilanci sono conservati nel progetto stesso.

* `final.tif`, `dz.tif`, `area.tif`, `snap_<anni>.tif`: GeoTIFF float32 sulla griglia, nel sistema di
  riferimento del DEM, senza valore fuori dai dati; `dz.tif` è la differenza delle due superfici float32,
  così finale - DEM = dz vale anche nei file.
* `compare_<chunk>.tif`: la differenza con un altro chunk, dopo un confronto.
* `balance.json`: i volumi e i conteggi della simulazione (passi, celle, percorso dell'acqua ed erosore
  usati).
* `_work/`: i file scambiati con Landlab mentre gira; cancellati alla fine, anche quando la simulazione
  fallisce o viene annullata.
* Una versione del DEM, se aggiunta, in `versions/dem/<id>/dem.tif`.
* L'ambiente di Landlab, quando lo installa il plugin: `plugins/_data/landscape/env/` nella cartella dei
  plugin del programma, circa 500 MB.

## Licenze { #licences }

Landlab è distribuito con licenza MIT; richdem, che Landlab usa per il percorso dell'acqua, con la GNU
GPL versione 3. Il plugin è distribuito con licenza GPL-3.0-or-later; il programma e gli altri plugin
sono GPL-2.0-or-later, e il "or later" permette loro di lavorare insieme. Il programma non carica mai
Landlab: lo fa girare come programma separato e scambia file con esso.

**Bibliografia.** Culling, W. E. H. (1963), Soil creep and the development of hillside slopes, *The
Journal of Geology* 71, 127-161. Hanks, T. C., Bucknam, R. C., Lajoie, K. R., Wallace, R. E. (1984),
Modification of wave-cut and faulting-controlled landforms, *Journal of Geophysical Research* 89,
5771-5790. Roering, J. J., Kirchner, J. W., Dietrich, W. E. (1999), Evidence for nonlinear, diffusive
sediment transport on hillslopes and implications for landscape morphology, *Water Resources Research*
35, 853-870. Shobe, C. M., Tucker, G. E., Barnhart, K. R. (2017), The SPACE 1.0 model: a Landlab
component for 2-D calculation of sediment transport, bedrock erosion, and landscape evolution,
*Geoscientific Model Development* 10, 4577-4604.
