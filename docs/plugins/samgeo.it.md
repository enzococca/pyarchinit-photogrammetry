# Samgeo

Il plugin *Samgeo* trova nell'ortofoto quello che scrivi: *pietre*, *muri*, *mattoni*, *tegole e
coppi*, *frammenti ceramici*, *alberi*, o una frase qualsiasi in inglese. Ogni ricerca è un **layer di
proposte** nella vista 2D: lo controlli, elimini quello che è sbagliato, e solo dopo lo trasformi in
unità stratigrafiche, classi della nuvola densa o un GeoPackage. Segue la ricetta di `LangSAM` di
[segment-geospatial](https://github.com/opengeos/segment-geospatial):
[Grounding DINO](https://huggingface.co/IDEA-Research/grounding-dino-base) trova i riquadri di quello
che è scritto, [SAM 2.1](https://github.com/facebookresearch/sam2) disegna il contorno dentro ogni
riquadro. Girano entrambi sul tuo computer, con pesi aperti e senza account.

Questa pagina descrive la versione 0.1.0 del plugin, che richiede pyarchinit-photogrammetry 1.5.0 o
successivo. Il README e il codice sono nel
[repository dei plugin](https://github.com/enzococca/pyarchinit-photogrammetry-plugins/tree/main/plugins/samgeo).

<figure markdown="span">
  ![Il pannello Samgeo dopo una ricerca di cespugli](../img/plugins/samgeo/pane.png)
  <figcaption>Una ricerca di <em>cespugli</em> sulla parte visibile di un'ortofoto pubblica: 8 proposte
  in 18 secondi sul processore, contornate in arancione nella vista 2D, la ricerca elencata nel
  pannello e, sotto il chunk, nell'area di lavoro. I gruppi di conifere sul versante aperto sono trovati
  con contorni buoni; molti alberelli isolati, pochi pixel a questa risoluzione (46 cm/px), no.
  Ortofoto: NEON, sito WLOU (Colorado), mosaico della camera del 2017, CC0.</figcaption>
</figure>

!!! note "L'esempio di questa pagina"
    La figura usa un'ortofoto pubblica del National Ecological Observatory Network (NEON) degli Stati
    Uniti, rilasciata in CC0, perché le foto del tutorial non sono pubbliche. Mostra come funziona il
    plugin e cosa aspettarsi dai modelli: proposte da controllare, non un disegno finito. Su uno scavo
    gli stessi passi valgono per pietre, mattoni e tegole, a una risoluzione di circa un centimetro.

## In breve

1. Costruisci l'ortofoto del chunk (*Flusso di lavoro > Costruisci ortofoto...*).
2. Apri il pannello *Samgeo* (*Plugin > Samgeo > Pannello Samgeo*). La prima volta, *Installa i
   modelli...* installa PyTorch e i modelli in un ambiente loro (circa 2 GB, qualche minuto).
3. Scegli cosa cercare, il raster e dove - tutto il raster, un poligono che disegni o la parte visibile
   nella vista 2D - e premi *Trova*.
4. Controlla le proposte nella vista: elimina quelle sbagliate con *Seleziona* e Canc.
5. *US...*, *Classi della nuvola* o *GeoPackage...* per quelle rimaste.

## Cosa fa

| Da una ricerca | Che cosa ottieni |
|---|---|
| Il layer `samgeo: <nome>` | gli oggetti trovati, ciascuno con il suo numero, il testo, la frase che l'ha trovato, il punteggio (la confidenza di Grounding DINO) e l'area in m²; ridisegnato su ogni raster che la vista mostra; una proposta eliminata nella vista esce anche dalla ricerca |
| US | le proposte - quelle selezionate nella vista, quelle non ancora US, o tutte - diventano unità stratigrafiche del chunk, del tipo scelto (US, USM...), con il testo e il punteggio come attributi (`samgeo_prompt`, `samgeo_score`); una sola voce di *Modifica > Annulla* per tutte |
| Classi della nuvola densa | i punti dentro le proposte, in pianta, prendono la classe `samgeo: <nome>` |
| GeoPackage | le ricerche scelte, un layer ciascuna |
| La maschera | gli oggetti numerati in un GeoTIFF che il calcolatore raster trova da solo (`samgeo_<ricerca>_mask`) |

Le US fatte dal plugin sono US come quelle che disegni: sono nel collegamento dal vivo con QGIS,
*Strumenti > Proponi US e rapporti (Extended Matrix)...* le numera, e vanno nelle schede US di
PyArchInit.

## Cosa serve prima { #before }

* **Il plugin**, installato dal programma: vedi [Guide ai plugin](guides.md#install).
* **PyTorch e i modelli.** Il programma non carica mai PyTorch - il suo runtime si scontra con quello
  di pycolmap nello stesso processo - ma fa girare i modelli in un processo a parte, con:
    * l'ambiente dei plugin AI, che il plugin installa a richiesta con
      [uv](https://github.com/astral-sh/uv): Python 3.12, PyTorch (la versione per la tua scheda
      grafica: CUDA con una scheda NVIDIA, Metal su Apple Silicon, altrimenti il processore),
      transformers e sam2, circa 2 GB, con internet quella volta. Va in
      `pyarchinit-photogrammetry/geoai-env` nella cartella dei dati del programma (la variabile
      `PPG_GEOAI_ENV` lo mette altrove) e sarà condiviso con il plugin geoai, così PyTorch c'è una
      volta sola. L'applicazione desktop funziona così. *Installa i modelli...* nel pannello,
      *Plugin > Samgeo > Installa i modelli...* o `ppg plugin samgeo install`;
    * il Python del programma, quando vi sono installati PyTorch, transformers e sam2.

    I pesi dei modelli (circa 1 GB) si scaricano da Hugging Face alla prima ricerca e restano nella
    sua cache.

* **Un'ortofoto**, o un altro raster del chunk: qualsiasi raster elencato dal calcolatore raster - una
  visualizzazione del rilievo del DEM, una versione dell'ortofoto - si può usare (su un DEM in via
  sperimentale: i modelli sono stati addestrati su fotografie).
* **Tempo, o una scheda grafica.** Sul processore un tassello di 1024 pixel richiede decine di
  secondi; il numero di tasselli è scritto nella barra di stato quando parte una ricerca.

## I passi nel programma { #program }

### Il pannello

Il pannello *Samgeo* si apre a destra nella finestra, fra le schede degli altri pannelli; si sposta, si
stacca o si chiude e si riapre da *Vista > Pannelli* o da *Plugin > Samgeo > Pannello Samgeo*. Dall'alto:

* una breve descrizione, e dove girano i modelli - o *Installa i modelli...* quando non sono
  installati;
* **Cerca**: una voce pronta o un testo tuo. Le voci pronte sono *pietre*, *muri*, *mattoni*, *tegole
  e coppi*, *frammenti ceramici*, *ossa*, *tessere di mosaico*, *alberi*, *cespugli*, *persone*,
  *attrezzi e secchi*, *teli e coperture*: il plugin le dà ai modelli in inglese (*muri* è
  `stone wall`). Un testo tuo è letto così com'è: in inglese funziona meglio; più cose si separano con
  le virgole (`stone, brick`);
* **Raster**: l'ortofoto, o un altro raster del chunk;
* **Dove**: *Tutto il raster*, *Un poligono da disegnare* (lo strumento di disegno del programma:
  trascina un riquadro, oppure clic sui vertici e doppio clic), *La parte visibile nella vista 2D*;
* **Confidenza minima** (0,30): più alta, proposte meno numerose e più sicure;
* **Area minima** e **Area massima** in m² (vuote: nessun limite);
* **Risoluzione**: automatica - quella del raster, ma non più fine di 1 cm/px, che basta per le
  pietre - o più grossolana, per meno tasselli e una ricerca più veloce;
* **Modelli**: *Normali* (Grounding DINO base, SAM 2.1 small), *Veloci* (tiny, tiny), *Precisi* (base,
  large);
* *Trova*;
* le ricerche del chunk attivo: la casella mostra o nasconde un layer, il doppio clic lo inquadra;
* *US...*, *Classi della nuvola*, *GeoPackage...*, *Inquadra*, *Rimuovi* per la ricerca selezionata.

Le ricerche sono anche sotto il chunk nell'area di lavoro (*Samgeo*), con le stesse azioni nel menu del
clic destro.

### Controllare le proposte { #check }

Le proposte sono disegnate nel colore della loro ricerca; un clic su una in modalità navigazione ne
mostra numero, testo, punteggio e area. Elimina quelle sbagliate come ogni elemento della vista
(*Seleziona*, poi Canc o *Elimina selezionati*): escono dalla ricerca, dal suo GeoPackage e dalla sua
maschera. Una ricerca si può rifare con un altro testo, un'altra soglia o risoluzione: ognuna è un
layer a sé (*pietre*, *pietre 2*...).

### US { #units }

*US...* chiede quali proposte - quelle selezionate nella vista, quelle non ancora US, o tutte - e il
tipo (US, USM, USR, USD, USV; USM per muri e mattoni). Le US si aggiungono al chunk attivo senza
numero: *Strumenti > Proponi US e rapporti (Extended Matrix)...* le numera e ne propone i rapporti. Le
proposte già US restano nel layer con il solo contorno; quando lo sono tutte, il layer viene nascosto.
*Modifica > Annulla* toglie le US, la ricerca resta.

### Classi della nuvola densa { #classes }

*Classi della nuvola* dà ai punti della nuvola densa dentro le proposte (quelle selezionate nella
vista, altrimenti tutte) la classe `samgeo: <nome>`, con un codice da 150 in su; la nuvola viene
mostrata per classi. I punti che hai classificato a mano tengono la loro classe. Rifatta dopo aver
eliminato delle proposte, la classe segue quelle rimaste.

## La riga di comando { #cli }

```bash
ppg plugin samgeo status
ppg plugin samgeo install                                   # PyTorch e i modelli (uv)
ppg plugin samgeo segment scavo.ppg pietre                  # tutta l'ortofoto
ppg plugin samgeo segment scavo.ppg "roof tile" --area 2480000,4500000,2480004,4500003 --units
ppg plugin samgeo segment scavo.ppg muri --resolution 0.02 --min-area 0.5 --models precise --json
ppg plugin samgeo list scavo.ppg
ppg plugin samgeo export scavo.ppg samgeo.gpkg --run pietre
ppg plugin samgeo remove scavo.ppg pietre
```

`--chunk ETICHETTA` lavora su un chunk diverso da quello attivo, `--raster NOME` su un altro raster (il
suo nome nel calcolatore raster), `--threshold` imposta la confidenza minima, `--units` fa US di tutto
quello che trova.

## Come funziona una ricerca { #how }

1. L'area viene letta alla risoluzione della ricerca in tasselli di 1024 pixel sovrapposti di 128. Tre
   bande a 8 bit si leggono come sono; ogni altro raster (16 bit, riflettanze, una banda) viene stirato
   tra il 2° e il 98° percentile dell'area, una banda in grigio.
2. Ogni tassello va ai modelli; ogni maschera diventa un poligono nelle coordinate del chunk.
3. Un oggetto tagliato dal bordo di un tassello viene unito alla sua altra parte del tassello vicino; due
   riquadri sullo stesso oggetto lasciano una sola proposta; con più di un tassello, un riquadro grande
   quanto un tassello intero viene scartato (Grounding DINO risponde spesso "tutta l'immagine" accanto
   agli oggetti). Poi i filtri di punteggio e di area e, con un poligono, solo quello che vi cade dentro.

## Leggere i risultati { #results }

* **Proposte, non un disegno.** I modelli trovano gran parte degli oggetti che riconoscono e ne perdono
  altri - soprattutto i piccoli, di pochi pixel; alcune proposte sono sbagliate. Controllale prima di
  farne US.
* **Il punteggio** è la confidenza di Grounding DINO nel riquadro: su immagini aeree i valori tipici
  sono 0,3-0,5. Alzare la confidenza minima lascia proposte meno numerose e più sicure; abbassarla ne
  trova di più, e più errori.
* **La risoluzione** conta: un oggetto dovrebbe misurare almeno una decina di pixel. Per pietre di
  10-20 cm, 1 cm/px.
* **Oggetti, non terreni.** I modelli trovano oggetti - pietre, mattoni, tegole, alberi, persone,
  attrezzi - molto meglio del contrasto tra due terreni: quello sarà compito del plugin geoai, che
  impara dai tuoi disegni.
* **Un solo tassello.** Con un'area piccola (un tassello) Grounding DINO può rispondere con l'area
  intera: elimina quella proposta, o imposta un'area massima.

## File { #files }

Nella cartella del chunk, `samgeo/`:

* `<ricerca>.gpkg`: gli oggetti (tabella `samgeo`, multipoligoni: `n`, il numero nella maschera;
  `prompt`, il testo; `label`, la frase che l'ha trovato; `score`; `quality`, la stima di SAM sulla
  maschera; `area_m2`);
* `<ricerca>_mask.tif`: gli oggetti numerati (0 dove non ce n'è).

Le ricerche - testo, raster, area, parametri, numero di oggetti, data, dispositivo - restano nel
progetto.
