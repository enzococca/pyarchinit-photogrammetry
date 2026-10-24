# Terreno

Il plugin *Terreno* esegue le analisi del terreno di [WhiteboxTools](https://www.whiteboxgeo.com/) sul
DEM del progetto e ne riporta i risultati nel progetto: dove l'acqua scorre e si raccoglie, il
reticolo e i bacini; quanto ogni punto sta più in alto o più in basso di ciò che ha intorno, e la forma
del terreno a cui appartiene; la curvatura e la rugosità del suolo; le conche e i rilievi, annidati
l'uno nell'altro, con profondità e volume; che cosa si vede da un punto. Ogni risultato sta sulla
griglia del DEM, nel sistema di riferimento del chunk, sopra l'ortofoto o il DEM della vista.

Questa pagina descrive la versione 0.1.1 del plugin, che richiede pyarchinit-photogrammetry 1.2.2 o
successiva. Il README e il codice sono nel
[repository dei plugin](https://github.com/enzococca/pyarchinit-photogrammetry-plugins/tree/main/plugins/terrain).

<figure markdown="span">
  ![Posizione topografica relativa e geomorphons di un tell](../img/plugins/terrain/tpi-geomorphons.jpg)
  <figcaption>(a) Posizione topografica relativa su una finestra di 10 m: in blu ciò che sta più in
  basso, in rosso ciò che sta più in alto di quanto ha intorno - argini, tagli, buche e cumuli si
  staccano dalla pendenza generale del tell. (b) Geomorphons con una distanza di ricerca di 30 m,
  calcolati su celle di 1 m: gli speroni radiali del tell, la cima come picco e cresta, i campi intorno
  come un mosaico di forme piccole. Il DEM reale di Tell Sultan, in un sistema di riferimento
  locale.</figcaption>
</figure>

!!! note "Gli esempi di questa pagina"
    Le figure dei risultati vengono dal DEM di Tell Sultan - il sito del [tutorial](../tutorials/tell.md) -
    a 19 cm, 2.876 x 2.032 celle, in un sistema di riferimento locale; il pannello è stato fotografato
    su un piccolo DEM sintetico. Mostrano che cosa fa il plugin e non sono uno studio del sito.

## In breve

1. Costruisci il DEM del chunk (*Flusso di lavoro > Costruisci DEM...*); per l'acqua, le conche e i
   rilievi del suolo nudo, un DEM della sola classe *Terreno*.
2. Apri il pannello *Terreno* (*Plugin > Terreno > Analisi del terreno...*).
3. Scegli un'analisi: *Idrologia...*, *Geomorfometria...*, *Depressioni e rilievi...* o
   *Visibilità...*. La prima scarica WhiteboxTools (circa 40 MB) una volta sola.
4. Guarda il risultato sopra l'ortofoto o il DEM, con la palette e l'opacità che preferisci; metti
   reticolo, bacini e conche nei layer della vista.
5. Esporta un risultato in GeoTIFF, le linee e i poligoni in GeoPackage, oppure apri tutto in QGIS.

## Che cosa fa

| Analisi | Che cosa dà | A che cosa serve |
|---|---|---|
| Idrologia | l'accumulo di flusso, il reticolo come linee con area drenata e lunghezza, i bacini come poligoni | i solchi che erodono un tell, dove va la pioggia su uno scavo, il ruscellamento di un versante, antichi corsi d'acqua |
| Posizione topografica relativa (TPI), deviazione dalla media | quanto ogni cella sta più in alto o più in basso delle celle intorno, entro una finestra | muri, argini, fossati, trincee, spoliazioni: il microrilievo, liberato dalla pendenza generale |
| Geomorphons | dieci forme del terreno: piano, picco, cresta, spalla, sperone, versante, impluvio, piede del versante, valle, fossa | la forma di un tell, dei terrazzi, i margini di un rilievo, una carta delle forme di un campo |
| Curvature, rugosità | convesso o concavo lungo la pendenza e di traverso; quanto è scabra la superficie | rotture di pendenza, bordi di terrazzi e trincee, crolli e pietrame contro il terreno liscio |
| Ore di luce | la parte delle ore di luce dell'anno che ogni cella passa fuori dall'ombra del rilievo | l'esposizione di un versante, di un terrazzo, di un ingresso |
| Depressioni e rilievi | le conche e i rilievi, ognuno affettato in livelli annidati, con livello, profondità o altezza, area, volume, allungamento | fosse, cisterne, fornaci, buche di spoliazione, tane; tumuli, cumuli di terra di scavo, dossi in un campo |
| Visibilità | le celle visibili da un punto, con l'occhio a un'altezza sul terreno | che cosa domina una torre, una tomba o un'altura |

Serve ogni volta che la forma del terreno racconta qualcosa: un terrapieno sotto l'erba, l'erosione di
un tell, le conche di un campo prima dello scavo, la visibilità di un monumento. Le analisi descrivono
la superficie: una conca è una conca, che sia una fossa, un cratere di bomba o una trincea moderna. Che
cosa significano le forme resta una decisione dell'archeologo, presa con l'ortofoto, il rilievo e lo
scavo.

## Che cosa serve prima { #before }

* **Il plugin**, installato dal programma: vedi [Guide ai plugin](guides.md#install).
* **WhiteboxTools**, un programma libero di John Lindsay (University of Guelph, licenza MIT). Il plugin
  scarica la versione per il tuo computer da whiteboxgeo.com la prima volta che parte un'analisi (circa
  40 MB: quella volta serve internet) e la tiene nella propria cartella; la riga di stato del pannello
  dice quale versione usa. Ci sono versioni per macOS (Apple Silicon e Intel), Windows (quella x86-64,
  scaricata anche sui computer ARM) e Linux su x86-64. Il plugin usa anche una copia che trova: il
  programma indicato dalla variabile `PPG_WBT` (o la sua cartella), la copia del pacchetto Python
  `whitebox` se è installato nell'ambiente del programma, `whitebox_tools` nel PATH. Su Linux ARM, che
  non ha una versione, installa WhiteboxTools da te e imposta `PPG_WBT`. Dalla riga di comando
  `ppg plugin terrain install` lo scarica in anticipo.
* **Un progetto con un chunk elaborato fino al DEM** (*Flusso di lavoro > Costruisci DEM...*). Il plugin
  lavora sul DEM che il chunk mostra, la sua versione attiva, e non ha bisogno d'altro: né foto né
  nuvola.
* **Un sistema di riferimento proiettato, in metri**, per il chunk - per esempio il fuso UTM del sito.
  Il plugin funziona anche in un sistema locale (un progetto non georeferenziato), con tre differenze:
  le ore di luce non si possono calcolare (serve la latitudine), i GeoTIFF non hanno un sistema di
  riferimento e il progetto QGIS del programma lascia fuori i prodotti.

## Il DEM { #dem }

Il plugin non ha dati propri: legge il DEM. Ciò che il DEM contiene decide che cosa dicono le analisi.

### Superficie o terreno { #dsm-dtm }

Un DEM costruito da tutti i punti della nuvola densa è un modello della superficie: alberi, cespugli,
edifici, veicoli, persone, tende e i cumuli di terra dello scavo ne fanno parte. Per la visibilità del
sito com'è oggi va bene; per l'acqua, le conche, i rilievi e il microrilievo del suolo no: un albero
diventa un rilievo, un'auto parcheggiata una diga di traverso a un solco.

* Classifica la nuvola densa - *Strumenti > Classificazione della nuvola*, oppure la classificazione del
  terreno del plugin [terrio](terrio.md) - e costruisci un DEM della sola classe *Terreno* (*Flusso di
  lavoro > Costruisci DEM...*, classi). Quel DEM è un modello del terreno (DTM): i punti sotto alberi ed
  edifici vengono interpolati.
* Rendilo la versione attiva - tasto destro sulla versione nell'*Area di lavoro*, *Rendi attiva (la
  usano gli altri strumenti)* - prima delle analisi; tieni il modello della superficie come un'altra
  versione per la visibilità.

### La cella { #cell }

Ogni misura che dai è in metri e il plugin la trasforma in celle del DEM: una finestra di 10 m su un
DEM di 19 cm è di 53 celle; un'area drenata minima di 50 m² è di 1.383 celle. Un DEM molto dettagliato
(2-5 cm) mostra ogni zolla, solco e impronta: le curvature e i geomorphons leggono quelli invece delle
forme del terreno. Per questo la finestra *Geomorfometria* ha una *Cella di calcolo*: la metrica viene
calcolata su una copia del DEM ricampionata a quella cella (0,5-1 m per le forme di un campo o di un
tell) e riportata sulla griglia del DEM. Si usa qualsiasi cella più grande di quella del DEM; il prodotto
ha la cella nel nome, così la stessa metrica su due celle resta affiancata.

### Vuoti e bordi { #holes }

Le celle senza valore restano senza valore in ogni prodotto. Il bordo del DEM - e il bordo di ogni
vuoto al suo interno - è dove l'acqua esce dal rilievo: lì finiscono i corsi e i bacini sono le aree
che drenano verso un punto del bordo. Un vuoto in mezzo all'area studiata (una pozza d'acqua, un telo
rimasto fuori dalla nuvola) è uno sbocco in cui l'acqua si getta: costruisci il DEM senza vuoti lì,
oppure leggi con cautela il reticolo intorno.

### Quanto ci vuole { #time }

Sul DEM delle figure (2.876 x 2.032 celle di 19 cm) ogni analisi ha richiesto da meno di un secondo a
sei secondi su un computer recente (un Apple M4 Pro). Su un DEM di 151 milioni di celle (5,5 cm su un grande tell)
l'idrologia ha richiesto 70 s, la posizione topografica 56 s, le depressioni 87 s, i rilievi 107 s, i
geomorphons a 1 m pochi secondi, con circa 6 GB di memoria. La barra di avanzamento segue ogni strumento
di WhiteboxTools mentre lavora, con la fase in cui si trova (*FillDepressions: Filling depressions 35%*), e
*Annulla* ferma lo strumento mentre lavora, non alla sua fine.

## Passo per passo nel programma { #program }

### Il pannello

Una volta installato il plugin e riavviato il programma, il pannello *Terreno* si apre a destra nella finestra, fra le schede di *PyArchInit*; si sposta, si stacca o si chiude come gli altri pannelli e si riapre da *Vista > Pannelli*, oppure con *Plugin > Terreno > Analisi del terreno...*. Anche il menu *Plugin > Terreno* ha le quattro
analisi, e *Nascondi la sovrapposizione*.

<figure markdown="span">
  ![Il pannello Terreno con i prodotti di un DEM](../img/plugins/terrain/pane.png)
  <figcaption>Il pannello dopo le analisi di un DEM sintetico: l'accumulo di flusso selezionato e
  sovrapposto alla vista.</figcaption>
</figure>

Dall'alto: una breve descrizione; il WhiteboxTools in uso (o l'avviso che verrà scaricato alla prima
analisi); i pulsanti *Idrologia...*, *Geomorfometria...*, *Depressioni e rilievi...* e *Visibilità...*;
la tabella dei prodotti del chunk; *Palette* e *Opacità*; *Sovrapponi alla vista*, *Nascondi*, *Vettori
nei layer*, *Esporta...*, *QGIS* e *Rimuovi*; in fondo, i valori ai due estremi dei colori mostrati.

La tabella ha una riga per prodotto: *Nome*, *Tipo* (*accumulo*, *classi / id*, *± 0* per i valori
intorno allo zero, *valori*, *sì / no*, *forme*), *Celle* con un valore, *Min*, *Max* e *Fatto* (data e
ora). Il suggerimento sul nome dà l'id del prodotto e i suoi parametri (e una nota, quando la cella di
calcolo chiesta non è stata usata). Un prodotto rifatto con gli stessi parametri sostituisce il vecchio:
idrologia, depressioni e rilievi hanno un gruppo di prodotti ciascuno, le altre metriche uno per finestra
e cella, la visibilità uno per punto.

| Nome nella tabella | Prodotto | Valori |
|---|---|---|
| *Accumulo di flusso (log10 celle)* | accumulo di flusso | log10 del numero di celle che drenano nella cella |
| *Reticolo (tratti)* | reticolo idrografico | il numero del tratto (il pezzo fra due confluenze) |
| *Bacini* | bacini | il numero del bacino |
| *Posizione topografica relativa (TPI) 10 m* | posizione topografica relativa, finestra di 10 m | da -1 a +1 |
| *Deviazione dalla quota media 10 m* | deviazione dalla quota media, finestra di 10 m | deviazioni standard |
| *Geomorphons (forme del terreno) 30 m, cella 1 m* | geomorphons, distanza di ricerca 30 m, calcolati su celle di 1 m | la forma, da 1 a 10 |
| *Indice di rugosità (TRI)* | indice di rugosità | metri |
| *Curvatura di profilo* | curvatura di profilo | 1/m |
| *Curvatura planare* | curvatura planare | 1/m |
| *Ore di luce (frazione dell'anno)* | ore di luce | frazione, da 0 a 1 |
| *Profondità delle depressioni* | profondità delle depressioni | metri sotto il livello a cui la conca si riempie |
| *Livelli delle depressioni* | livelli delle depressioni | quante depressioni annidate contengono la cella |
| *Altezza dei rilievi* | altezza dei rilievi | metri sopra il livello che si chiude intorno al rilievo |
| *Livelli dei rilievi* | livelli dei rilievi | quanti rilievi annidati contengono la cella |
| *Visibilità da (x, y), occhio a 1.7 m* | visibilità | 1 visibile, 0 nascosta |

Il progetto conserva i nomi in italiano, come dati (al pari dei nomi dei layer del programma), e così li
scrivono anche i GeoTIFF e i GeoPackage; l'interfaccia inglese li mostra in inglese nel pannello, nei
layer della vista e nel progetto QGIS.

Le analisi girano in background con la barra di avanzamento. Quando una finisce, il suo prodotto
principale viene subito sovrapposto alla vista e il progetto risulta modificato: salvalo (*File >
Salva*) per tenere l'elenco dei prodotti con il progetto.

### 1. Idrologia { #hydrology }

*Idrologia...*

| Campo | Predefinito | Che cosa fa |
|---|---|---|
| *Area drenata minima di un corso (m²)* | 50 | una cella fa parte del reticolo quando almeno quest'area drena in essa: valori più piccoli danno un reticolo più fitto |
| *Apertura delle depressioni: distanza massima (m)* | 10 | quanto lontano il plugin può scavare un canale per far uscire l'acqua da una conca |

L'acqua non si ferma nelle conche di un DEM: prima il plugin apre ogni conca con il canale più
economico verso l'esterno (una breccia, fino alla distanza data) e riempie ciò che non si può aprire;
poi ogni cella cede la sua acqua alla più bassa delle otto vicine (il metodo D8) e il plugin conta
quante celle drenano in ciascuna. Le celle con abbastanza area drenata sono il reticolo, diviso in
tratti alle confluenze; i bacini sono le aree che drenano verso lo stesso punto del bordo del DEM.

Alla fine l'accumulo di flusso viene sovrapposto alla vista e il reticolo viene disegnato come layer
*Terreno: reticolo*; la barra di stato dice quanti tratti e bacini sono stati trovati. Per vedere i
bacini, seleziona *Reticolo (tratti)* o *Bacini* nella tabella e premi *Vettori nei layer*: il layer
*Terreno: bacini* si aggiunge al reticolo. Leggi i [risultati](#reading-hydrology).

### 2. Geomorfometria { #geomorphometry }

*Geomorfometria...*

| Campo | Predefinito | Che cosa fa |
|---|---|---|
| *Metrica* | *Posizione topografica relativa (TPI)* | la metrica, dalla tabella qui sotto |
| *Finestra o distanza di ricerca (m)* | 10 | il lato della finestra per la posizione topografica e la deviazione; la distanza di ricerca per i geomorphons; le altre metriche non la usano |
| *Geomorphons: angolo di piattezza (gradi)* | 1 | le pendenze sotto quest'angolo contano come piano |
| *Cella di calcolo (m, 0 = quella del DEM; 1 m per le forme di un campo)* | 0 | la metrica viene calcolata su una copia del DEM con celle di questa misura e riportata sulla griglia del DEM; si usa qualsiasi cella più grande di quella del DEM, una più piccola lascia la cella del DEM e il messaggio finale lo dice |

| Metrica | Finestra | Valori | Prodotto |
|---|---|---|---|
| *Posizione topografica relativa (TPI)* | sì | -1 nella cella più bassa della finestra, +1 nella più alta, 0 alla sua media | `tpi_<misura>` |
| *Deviazione dalla quota media* | sì | la differenza dalla media della finestra divisa per la sua deviazione standard | `dev_<misura>` |
| *Geomorphons (forme del terreno)* | distanza di ricerca | 1 piano, 2 picco, 3 cresta, 4 spalla, 5 sperone, 6 versante, 7 impluvio, 8 piede del versante, 9 valle, 10 fossa | `geomorphons_<misura>` |
| *Indice di rugosità (TRI)* | no | metri: quanto una cella differisce dalle otto vicine | `ruggedness` |
| *Curvatura di profilo* | no | 1/m lungo la pendenza: positiva convessa, negativa concava | `profile_curvature` |
| *Curvatura planare* | no | 1/m di traverso alla pendenza: positiva dove le curve di livello si gonfiano verso l'esterno (creste, speroni), negativa dove rientrano (valli, impluvi) | `plan_curvature` |
| *Ore di luce (frazione dell'anno)* | no | la parte del tempo di luce dell'anno in cui la cella non sta nell'ombra del rilievo entro 100 m | `daylight` |

La misura nel nome di un prodotto è la finestra (`tpi_10` è la posizione topografica su 10 m, `tpi_2_5`
su 2,5 m), e c'è anche una cella di calcolo più grande di quella del DEM (`geomorphons_30_c1` è calcolato
su celle di 1 m, `plan_curvature_c0_5` su 0,5 m): finestre e celle diverse restano affiancate.

Le ore di luce vogliono un chunk georeferenziato: prendono la latitudine del centro del DEM e seguono il
sole lungo l'anno, ogni 10 minuti. Per ogni direzione del sole, a passi di 15 gradi, WhiteboxTools dà
l'angolo dell'orizzonte di ogni cella entro 100 m (il suo strumento *HorizonAngle*), e la cella è in ombra
finché il sole sta più in basso. Il plugin non usa lo strumento *TimeInDaylight* di WhiteboxTools: nella
versione 2.4 il suo almanacco solare prende il tempo nell'unità sbagliata (microsecondi dove si aspetta
millisecondi), quindi le sue posizioni del sole non sono quelle delle ore del giorno, e un terreno aperto
e piano veniva circa 0,79 a 40 gradi nord invece di 1.

### 3. Depressioni e rilievi { #depressions }

*Depressioni e rilievi...*

| Campo | Predefinito | Che cosa fa |
|---|---|---|
| *Rilievi (dossi, tumuli) invece delle depressioni* | no | spuntato: la stessa ricerca sul DEM capovolto |
| *Area minima (m²)* | 1 | le conche (o i rilievi) più piccole non diventano poligoni |
| *Profondità (altezza) minima (m)* | 0,05 | quelle meno profonde non vengono tenute |
| *Intervallo dei livelli (m, 0 = un decimo della profondità)* | 0 | l'altezza fra due fette; con 0 ogni conca viene tagliata in una decina di fette, mai più sottili della profondità minima |

Il DEM viene riempito come lo riempirebbe l'acqua; ogni punto in cui la superficie riempita sta sopra
il terreno è una conca. Ogni conca viene poi affettata dal livello a cui traboccherebbe (il livello di
sfioro) fino al fondo: a ogni livello le parti del terreno che stanno sotto, unite fra loro, sono le
depressioni di quel livello, annidate in quelle del livello sopra. Una conca con due fosse sul fondo
diventa un poligono al livello più alto e due poligoni più in basso. I rilievi si trovano allo stesso
modo sul DEM capovolto: le loro fette vanno dalla curva più bassa che si chiude ancora intorno al
rilievo fino alla cima.

Vengono affettati al massimo 20 livelli per conca e le 2.000 conche più grandi del DEM. Alla fine la
profondità (o l'altezza) viene sovrapposta alla vista, i poligoni vengono disegnati come layer
*Terreno: depressioni* (rosso) o *Terreno: rilievi* (verde) e la barra di stato dice quante conche e
quanti poligoni annidati sono stati trovati. Una nuova ricerca sostituisce la precedente dello stesso
tipo.

### 4. Visibilità { #viewshed }

*Visibilità...*

| Campo | Predefinito | Che cosa fa |
|---|---|---|
| *X del punto (coordinate del DEM)* | il centro del DEM | nel sistema di riferimento del chunk: muovi il mouse sulla vista e leggi le coordinate nella barra di stato |
| *Y del punto* | il centro del DEM | |
| *Altezza dell'occhio sul terreno (m)* | 1,7 | l'osservatore sta sul DEM nel punto, con l'occhio a quest'altezza |

Il prodotto vale 1 dove il terreno della cella si vede dall'occhio, 0 dove è nascosto; la barra di
stato dà la parte del terreno visibile. Si crea un prodotto per ogni punto.

### 5. I prodotti sulla vista { #overlay }

Seleziona un prodotto nella tabella, poi:

* *Palette*: *Del tipo di prodotto* (blu-bianco-rosso per i valori intorno allo zero, azzurri per
  l'accumulo di flusso, un colore per valore per reticolo, bacini e livelli, i colori delle forme per i
  geomorphons, dal nero al bianco per visibile e nascosto, grigi per il resto), *Grigi*, *Grigi
  invertiti*, *Divergente (blu-rosso)*, *Terra*, *Calore*, *Acqua*, *Classi (un colore per valore)*,
  *Forme del terreno (geomorphons)*;
* *Opacità*: cambia subito il prodotto mostrato;
* *Sovrapponi alla vista*, o un doppio clic sulla riga: il prodotto sopra l'ortofoto o il DEM (se la
  vista è vuota viene caricato il DEM). Una nuova palette vale dal successivo *Sovrapponi alla vista*.
  I colori vanno dal 2° al 98° percentile dei valori - simmetrici intorno allo zero per i valori con
  segno, da 0 a 1 per la visibilità, su tutti i numeri per reticolo, bacini, livelli e forme - e la
  riga in fondo al pannello dice quali. Si mostra un prodotto alla volta; *Nascondi* (o *Plugin >
  Terreno > Nascondi la sovrapposizione*) lo toglie;
* *Vettori nei layer*, per i prodotti che hanno linee o poligoni (*Reticolo*, *Bacini*, profondità e
  livelli di depressioni e rilievi): i layer *Terreno: reticolo* (linee blu), *Terreno: bacini*
  (marrone), *Terreno: depressioni* (rosso) e *Terreno: rilievi* (verde) nella vista e nel pannello
  *Layer*. Con lo strumento *Sposta* un clic su una linea o un poligono ne mostra gli attributi. Il
  reticolo resta quando la vista passa fra DEM e ortofoto, i poligoni no - premi di nuovo il pulsante -
  e per ogni layer vengono disegnati al massimo 5.000 poligoni (il GeoPackage li ha tutti);
* *Esporta...*: il prodotto come GeoTIFF;
* *QGIS*: il progetto QGIS del programma ([QGIS](../integrations.md)) con tutti i prodotti (nascosti
  all'inizio, in grigi o blu-rosso) e le linee e i poligoni; solo per un chunk georeferenziato;
* *Rimuovi*: chiede conferma, poi cancella il GeoTIFF del prodotto. Reticolo e bacini condividono un
  GeoPackage, e così profondità e livelli delle depressioni (o dei rilievi): il GeoPackage viene cancellato
  con l'ultimo dei due - la finestra dice quando l'altro lo usa ancora - e allora i suoi layer escono dalla
  vista.

*File > Esporta > Analisi del terreno (GeoPackage)...* scrive tutte le linee e i poligoni del chunk -
reticolo, bacini, depressioni, rilievi - in un solo GeoPackage.

## Dalla riga di comando { #cli }

Le stesse analisi con `ppg plugin terrain COSA PROGETTO ...`. I comandi lavorano sul chunk attivo
(`--chunk NOME` per un altro); quelli che cambiano il progetto lo salvano.

```bash
ppg plugin terrain --help
ppg plugin terrain install
ppg plugin terrain hydrology scavo.ppg --threshold 20 --breach 10
ppg plugin terrain geomorph scavo.ppg tpi --size 10
ppg plugin terrain geomorph scavo.ppg geomorphons --size 30 --cell 1 --flatness 1
ppg plugin terrain geomorph scavo.ppg profile_curvature --cell 0.5
ppg plugin terrain depressions scavo.ppg --min-area 1 --min-depth 0.1
ppg plugin terrain depressions scavo.ppg --min-area 1 --min-depth 0.3 --mounds
ppg plugin terrain viewshed scavo.ppg 500120.5 4500812.0 --height 1.7
ppg plugin terrain list scavo.ppg
ppg plugin terrain export scavo.ppg tpi_10 tpi_10m.tif
ppg plugin terrain export scavo.ppg terreno.gpkg
ppg plugin terrain remove scavo.ppg tpi_10
```

| Comando | Opzioni |
|---|---|
| `install` | scarica subito WhiteboxTools e dice dove sta |
| `hydrology PROGETTO` | `--threshold` area drenata minima di un corso, m² (50); `--breach` distanza massima di apertura, m (10) |
| `geomorph PROGETTO METRICA` | `METRICA`: `tpi`, `dev`, `geomorphons`, `ruggedness`, `profile_curvature`, `plan_curvature`, `daylight`; `--size` finestra o distanza di ricerca, m (10); `--flatness` geomorphons, gradi (1); `--cell` cella di calcolo, m, usata se più grande di quella del DEM (0: quella del DEM) |
| `depressions PROGETTO` | `--min-area` m² (1); `--min-depth` m (0.05); `--interval` m (0: un decimo della profondità); `--mounds` |
| `viewshed PROGETTO X Y` | il punto nel sistema di riferimento del chunk; `--height` dell'occhio, m (1.7) |
| `list PROGETTO` | ogni prodotto con id, nome, tipo, file, parametri e statistiche |
| `export PROGETTO [ID] USCITA` | `USCITA` che finisce in `.gpkg`: tutte le linee e i poligoni in un GeoPackage; altrimenti il prodotto `ID` come GeoTIFF |
| `remove PROGETTO ID` | elimina il prodotto senza chiedere, e il suo GeoPackage quando nessun altro prodotto lo usa; stampa i file cancellati |

Ogni analisi stampa il suo risultato: il numero di tratti e bacini, la voce del nuovo prodotto, la
depressione o il rilievo più grande trovato (e una nota quando la cella chiesta non è stata usata). Gli id
dei prodotti - `flow_accumulation`, `streams`, `basins`, `depth`, `depression_levels`, `mound_height`,
`mound_levels`, `tpi_10`, `geomorphons_30`, `geomorphons_30_c1`, `viewshed_<x>_<y>`... - sono quelli che
`list` mostra.

## Leggere i risultati { #reading }

### Accumulo di flusso, reticolo e bacini { #reading-hydrology }

<figure markdown="span">
  ![Reticolo e bacini di un tell](../img/plugins/terrain/hydrology.jpg)
  <figcaption>L'idrologia del DEM con un'area drenata minima di 20 m²: il reticolo in blu, più spesso
  dove drena più terreno, i bacini in colori tenui. I solchi scendono a raggiera dal tell; nei campi
  piani intorno il reticolo segue i solchi dell'aratura. Il DEM reale di Tell Sultan, in un sistema di
  riferimento locale.</figcaption>
</figure>

* **L'accumulo di flusso** è il logaritmo in base 10 del numero di celle che drenano in ogni cella,
  compresa la cella stessa: 0 è una cella che non riceve acqua, 2 sono 100 celle, 4 sono 10.000.
  Moltiplica il numero di celle per l'area di una cella per avere l'area drenata (100 celle di 19 cm
  drenano 3,6 m²). I valori più alti disegnano le linee in cui l'acqua si concentra; quelli deboli
  mostrano l'andamento del versante.
* **Il reticolo** nel GeoPackage ha una linea per tratto, dalla prima cella del tratto alla prima del
  tratto successivo, così le linee si uniscono: `link` (il suo numero), `cells` e `area_m2` (le celle e
  l'area che drenano alla sua fine), `length_m`.
* **I bacini** hanno `basin` e `area_m2`. Sono le aree che drenano verso lo stesso punto del bordo del
  DEM, quindi molti piccoli bacini orlano il bordo del rilievo: su un sito vanno letti i più grandi.
* L'area drenata minima decide fin dove il reticolo risale i versanti: pochi metri quadrati danno una
  linea in ogni rivolo, centinaia solo i solchi principali. Fai l'idrologia con due o tre valori.
* Un modello della superficie fa girare l'acqua intorno agli alberi e sopra i tetti, e ogni zolla o
  solco di un DEM molto dettagliato la devia: per il paesaggio va letto il reticolo di un DTM, o di un
  DEM di qualche decimetro.

### Posizione topografica e deviazione { #reading-tpi }

La posizione topografica relativa confronta ogni cella con le celle di una finestra quadrata intorno:
+1 è la cella più alta della finestra, -1 la più bassa, 0 la quota media della finestra. La deviazione
dalla media è la differenza dalla quota media della finestra divisa per la sua deviazione standard (un
valore di 2 sta due deviazioni standard sopra la media). Tutte e due tolgono la pendenza generale: un
muro su un versante è positivo per tutta la sua lunghezza, un fossato negativo.

La finestra decide che cosa risalta: una finestra qualche volta più larga di ciò che si cerca - 2-5 m
per muri e fossati, 10-20 m per edifici e rilievi, di più per la forma di un sito intero. Finestre
piccole su un DEM dettagliato colgono ogni pietra; quelle grandi confondono le cose piccole con ciò che
hanno intorno. Nella figura in cima alla pagina, 10 m fanno risaltare argini, tagli e cumuli sui
versanti del tell.

### Geomorphons { #reading-geomorphons }

I geomorphons guardano da ogni cella lungo otto direzioni fino alla distanza di ricerca e confrontano
l'orizzonte con l'angolo di piattezza: più alto, più basso o in piano. La combinazione delle otto
risposte dà una di dieci forme, disegnate nel pannello con questi colori:

| Valore | Forma | Colore |
|---|---|---|
| 1 | piano | <span style="display:inline-block;width:1.4em;height:0.9em;background:#c8c8c8"></span> grigio chiaro |
| 2 | picco | <span style="display:inline-block;width:1.4em;height:0.9em;background:#a00000"></span> rosso scuro |
| 3 | cresta | <span style="display:inline-block;width:1.4em;height:0.9em;background:#f05a00"></span> arancione |
| 4 | spalla | <span style="display:inline-block;width:1.4em;height:0.9em;background:#fabe00"></span> ambra |
| 5 | sperone | <span style="display:inline-block;width:1.4em;height:0.9em;background:#dcf03c"></span> verde giallo |
| 6 | versante | <span style="display:inline-block;width:1.4em;height:0.9em;background:#5ab43c"></span> verde |
| 7 | impluvio | <span style="display:inline-block;width:1.4em;height:0.9em;background:#5adcdc"></span> azzurro |
| 8 | piede del versante | <span style="display:inline-block;width:1.4em;height:0.9em;background:#5a96f0"></span> blu |
| 9 | valle | <span style="display:inline-block;width:1.4em;height:0.9em;background:#2846c8"></span> blu scuro |
| 10 | fossa | <span style="display:inline-block;width:1.4em;height:0.9em;background:#781ea0"></span> viola |

La distanza di ricerca è la scala delle forme: pochi metri per terrazzi e argini, decine di metri per un
tell e le sue valli. Calcolali su celle di 0,5-1 m (*Cella di calcolo*): su un DEM di pochi centimetri
le forme sono quelle delle zolle. Sul terreno quasi piano ogni piccolo dosso diventa una forma, come nei
campi della figura; alza l'angolo di piattezza a 2-3 gradi per vederli come piano.

### Curvature e rugosità { #reading-curvature }

* **Curvatura di profilo**, in 1/m, nella direzione della pendenza: positiva dove la pendenza aumenta
  scendendo (il bordo convesso in cima a una scarpata, la sommità di un muro di terrazzo, la spalla di un
  rilievo), negativa dove si addolcisce (il piede di un versante, il fondo di un fossato). 0,1 è una
  curva con un raggio di 10 m.
* **Curvatura planare**, in 1/m, di traverso alla pendenza: positiva dove le curve di livello si
  gonfiano verso l'esterno e l'acqua si disperde (creste, speroni, i fianchi di un rilievo), negativa
  dove rientrano e l'acqua si raccoglie (solchi, impluvi, la linea di un fossato).
* **Rugosità**, in metri: lo scarto quadratico medio fra una cella e le sue otto vicine. Pietrame, crolli
  e cumuli di terra sono scabri; il terreno arato o spianato è liscio.

Tutte e tre si calcolano da una cella e dalle vicine: su un DEM dettagliato misurano il rumore del
rilievo quanto il terreno. Usa la *Cella di calcolo* (0,25-1 m) per la forma del suolo.

### Ore di luce { #reading-daylight }

La parte del tempo di luce dell'anno - il tempo in cui il sole sta sopra l'orizzonte - in cui la cella
non è in ombra per il rilievo entro 100 m. Il terreno aperto e piano vale 1; un DEM vero, con le sue
piccole irregolarità, poco meno (una zolla nasconde il sole per qualche minuto all'alba e al tramonto). Il
lato nord di un muro o di un rilievo, il fondo di una trincea o di una fossa hanno valori più bassi: sul
DEM sintetico dei test, a 40 gradi nord, il fondo di una fossa profonda 2 m e larga 4 m di raggio vale
0,58, il fianco nord di un rilievo 0,79 contro 0,95 del fianco sud.

### Depressioni e rilievi { #reading-depressions }

<figure markdown="span">
  ![Depressioni e rilievi di un tell](../img/plugins/terrain/depressions-mounds.jpg)
  <figcaption>I poligoni esterni delle depressioni (rosso, almeno 1 m² e 10 cm di profondità) e dei
  rilievi (verde, almeno 30 cm di altezza) sopra la profondità delle depressioni in blu. Il tell stesso
  è il poligono verde più grande; le depressioni sono i tagli sul tell, le conche dei campi e le parti
  basse lungo i bordi del rilievo. Il DEM reale di Tell Sultan, in un sistema di riferimento
  locale.</figcaption>
</figure>

I poligoni di `depressioni.gpkg` (e di `rilievi.gpkg`) hanno questi attributi:

| Colonna | Che cos'è |
|---|---|
| `id` | il numero del poligono |
| `sink` | la conca a cui appartiene |
| `rank` | il suo livello, da 1 al livello di sfioro verso il basso (per i rilievi, dalla base verso l'alto) |
| `parent` | il poligono del livello sopra che lo contiene; 0 per il poligono esterno di una conca |
| `level` | la quota della fetta, nel sistema delle quote del DEM |
| `depth_max`, `depth_mean` | la profondità massima e media sotto il livello (per i rilievi: altezza sopra), m |
| `area_m2` | la sua area |
| `volume_m3` | il volume fra il livello e il terreno: l'acqua che la conca terrebbe fino a quel livello; per un rilievo, la terra sopra la sua base |
| `elongation` | la lunghezza del poligono divisa per la sua larghezza (dalla distribuzione delle sue celle): 1 tondo, 3 una trincea allungata |
| `x`, `y` | il centro delle sue celle |

* Il poligono esterno di una conca (`rank` 1) è l'intera conca al livello a cui traboccherebbe; i
  poligoni con `rank` più alto sono le sue parti più profonde. Una cisterna dà una pila di poligoni
  quasi uguali, una fossa dentro un bacino più grande una pila piccola dentro una più grande, due fosse
  nella stessa conca due rami. Seleziona `rank = 1` in QGIS per i contorni delle conche.
* Il raster della profondità (*Profondità delle depressioni*) copre ogni conca del DEM, anche quelle
  più piccole o meno profonde del minimo, che non hanno un poligono; *Livelli* conta quanti poligoni
  annidati contengono ogni cella.
* Un rilievo si misura dalla curva più bassa che si chiude ancora intorno a esso: su un versante quella
  curva sta dal lato a monte, e l'altezza è minore di quella del rilievo visto da valle.
* La profondità minima conta: su un modello della superficie, o su un DEM dettagliato, ogni impronta è
  una conca. 5-10 cm e uno o pochi metri quadrati vanno bene per uno scavo; 30-50 cm e decine di metri
  quadrati per un paesaggio.

### Visibilità { #reading-viewshed }

Una cella è visibile quando dall'occhio si vede il suo terreno. Una persona o un muro su una cella
nascosta possono essere visti lo stesso da più lontano: la visibilità mostra il terreno, non ciò che vi
sta sopra. Su un modello della superficie alberi ed edifici chiudono la vista, come oggi; per la vista
di un'altra epoca usa un modello del terreno e immagina ciò che vi stava sopra. Nella dimostrazione,
dalla cima del tell con l'occhio a 1,7 m, si vedeva il 44 % del terreno: i versanti convessi nascondono
le proprie parti basse, e i campi più lontani tornano visibili.

### Limiti { #limits }

* **La superficie, non il significato.** Una conca, una cresta o un solco sono forme: moderno e antico,
  naturale e costruito si somigliano.
* **Decide il DEM.** Vuoti, rumore e vegetazione del DEM entrano in ogni prodotto: costruisci il DEM per
  la domanda che fai (cella, classe del terreno).
* **Un DEM intero in memoria.** I raster vengono letti per intero: un DEM di centinaia di milioni di
  celle vuole diversi gigabyte di memoria.

## File scritti { #files }

Tutto va nella cartella del chunk, accanto al file del progetto:
`<progetto>.ppg.files/chunk_<n>/terrain/`. L'elenco dei prodotti e dei loro parametri sta nel progetto
stesso.

* `<id>.tif`: ogni prodotto sulla griglia del DEM, come GeoTIFF: float32 senza valore fuori dai dati,
  oppure interi a 32 bit con 0 come assenza di valore per reticolo, bacini, livelli e geomorphons.
* `hydrology.gpkg`: tabelle `reticolo` (linee: `link`, `cells`, `area_m2`, `length_m`) e `bacini`
  (poligoni: `basin`, `area_m2`).
* `depressioni.gpkg`, `rilievi.gpkg`: una tabella ciascuno (`depressioni`, `rilievi`) con i poligoni
  annidati e le colonne descritte sopra.
* `_work/`: le copie del DEM che legge WhiteboxTools (float32, con un sistema di riferimento: uno di
  comodo per un progetto locale) e i raster intermedi. Può pesare qualche volta il DEM; si può
  cancellare e viene rifatta quando serve.

WhiteboxTools sta nella cartella propria del plugin, `plugins/_data/terrain/wbt/` dentro la cartella dei
plugin del programma (vedi [Plugin](../plugins.md)).
