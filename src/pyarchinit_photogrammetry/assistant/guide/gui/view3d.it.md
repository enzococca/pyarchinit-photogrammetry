# Vista 3D e trackball

La scheda *Modello* mostra il chunk in 3D: punti di legame, nuvola densa, modello ombreggiato o con texture, il TIN,
camere, marker e regione di ricostruzione (*Mostra* sceglie cosa). Gli strumenti della sua barra laterale - marker, foto del
punto, sezione, pianta a quota, taglio, prospetto, esagerazione verticale - sono descritti in
[Le viste centrali](index.md#le-viste-centrali); questa pagina spiega come muoversi nella scena.

![La trackball sul modello con texture](../img/gui/trackball.it.png)

## La trackball

Come in Metashape, intorno al centro di rotazione (il *pivot*, una piccola croce) è disegnata una **sfera con tre
anelli**:

| Dove inizia il trascinamento | Cosa fa |
| --- | --- |
| dentro la sfera | ruota la scena liberamente, come se si facesse rotolare la sfera sotto la mano |
| su un anello (si illumina sotto il mouse) | ruota la scena solo intorno all'asse di quell'anello: il rosso intorno alla x della scena, il verde intorno alla y, il blu (orizzontale) intorno alla verticale |
| fuori dalla sfera | gira la scena intorno alla linea di vista |

* **Spostare**: trascinamento con il tasto destro o centrale, oppure Maiusc + trascinamento con il sinistro. La
  sfera resta ferma sullo schermo: è la scena a scorrerle sotto.
* **Avvicinare**: la rotella avvicina **verso il cursore** - il punto sotto il mouse si avvicina e resta sotto il
  mouse. **Maiusc + rotella** si muove dritto lungo la linea di vista, senza spostare il centro di rotazione.
* **Centro di rotazione**: un **doppio clic** sul modello o sulla nuvola mette il pivot sulla superficie in quel
  punto (come si piazza un marker); la vista non salta. **Centra la vista sul punto** (gruppo *Vista* della colonna) porta poi il
  pivot al centro della vista, alla stessa distanza.
* *Vista iniziale* mostra di nuovo tutta la scena, *Dall'alto* guarda dritto verso il basso.

## Quando si vede la trackball

Il pulsante **Trackball** della barra, o *Vista > Trackball*, ha tre modi, ricordati tra una sessione e l'altra:

* **Quando il mouse è sulla vista** (predefinito): compare appena il mouse si muove sulla vista e sfuma un secondo e
  mezzo dopo che il mouse si è fermato;
* **Sempre**;
* **Mai**: la scena ruota allo stesso modo, solo la sfera non è disegnata.

La sfera non finisce mai in un'immagine presa dalla vista (schermate, lazo, scelta dei punti).

## Punti e assi dell'orientamento

Mentre *Flusso di lavoro > Orienta il sistema locale...* è aperta, i suoi pulsanti **Nel 3D** aspettano un clic sul
modello o sulla nuvola (il punto A, B o C; Esc rinuncia) e *Seleziona l'area nel 3D (lazo)* prende i punti di
un'area per il piano adattato. I nuovi assi sono disegnati nella vista come saranno - x rosso, y verde, z blu, il
piano orizzontale come un quadrato blu trasparente - e seguono ogni cambio dei punti; spariscono quando la finestra
si chiude ([orientare un sistema locale](processing.md#orientare-un-sistema-locale)).

## Disegni liberi

*Disegna* nella barra laterale della vista disegna linee, poligoni e punti sul modello (*Linea 3D*, *Poligono 3D*, *Punto
3D*): la vista resta viva fra un clic e l'altro - un clic è un vertice, trascinare fa ancora ruotare la scena, un
doppio clic finisce il disegno invece di spostare il centro di rotazione. *Seleziona e modifica* sceglie un disegno e
mostra i suoi vertici da trascinare o togliere; *Drappeggia sul modello* aggiunge vertici sulla superficie ogni 20
pixel. I disegni dei layer visibili si vedono nei loro colori, quello scelto più spesso. Vedi
[disegni liberi](drawing.md#disegni-liberi-2d-e-3d).

## Più chunk insieme

Applicazione **1.7.5**. Con più di un chunk nel progetto, ogni chunk dell'*Area di lavoro* ha una casella: **spuntala per
mostrare quel chunk nella vista 3D insieme a quello attivo** (il chunk attivo si vede sempre). I chunk sono disegnati
dove stanno nel mondo - ognuno con la sua trasformazione, convertito nel sistema di coordinate del chunk attivo quando
entrambi sono sistemi del mondo - così **prima di *Allinea chunk*** vedi dove si trovano e se si sovrappongono, e **dopo**
se combaciano. Un chunk in un sistema locale accanto a uno in un sistema del mondo non si può collocare: viene
**sovrapposto** - il suo centro sul centro del chunk attivo, per vederli all'incirca uno accanto all'altro - e la legenda
dice «sistema locale: sovrapposto, non collocato»; non viene misurato.

Ciò che si disegna segue *Mostra*: con la nuvola densa o i punti di legame, una copia diradata dei punti di ogni chunk (mai
la nuvola intera); con il modello, l'anteprima del suo modello (un chunk che non ce l'ha mostra i suoi punti). *Vista
iniziale* li inquadra tutti.

![Tre chunk prima dell'allineamento, un colore ciascuno](../img/gui/chunks-3d-before.it.png)

La riga **Altri chunk** sopra la vista (compare appena si spunta un chunk) sceglie i colori e l'opacità degli altri chunk:

| Colori | Cosa mostrano |
| --- | --- |
| **Un colore per chunk** | ogni chunk - anche quello attivo - nel suo colore, con il nome nella legenda a destra: dove sono le superfici di due campagne, quale sta sopra |
| **Colori veri** | i colori delle foto; il chunk attivo tiene i colori scelti per lui (classi, quota...) |
| **Distanza dal chunk attivo** | il chunk attivo in grigio, gli altri colorati secondo la distanza di ogni punto dalla sua superficie: bianco sopra di essa, rosso più in alto, blu più in basso, saturi oltre la fine della scala (arrotondata a 1, 2 o 5 mm, cm o m dal 95° percentile); grigi i punti che non hanno il chunk attivo né sopra né sotto |

![Dopo *Allinea chunk*: un colore per chunk](../img/gui/chunks-3d-after.it.png)

Con *Distanza dal chunk attivo* la legenda dà, per ogni chunk, la distanza **mediana**, la **NMAD** (la dispersione, come
una deviazione standard che ignora i valori anomali) e la quota dei suoi punti **entro** tre spaziature dei punti del chunk
attivo: dopo un buon allineamento la mediana è di pochi millimetri e quasi tutti i punti sono entro. La distanza si misura
lungo la normale della superficie dove il chunk attivo sta sopra o sotto il punto, altrimenti dal punto più vicino: una
parte oltre il bordo del chunk attivo non è mai «a 0». «Sopra» e «sotto» seguono la verticale del mondo: si leggono su
piani di calpestio e superfici poco inclinate, non sulle pareti. I punti di ogni chunk sono una copia diradata (al massimo
200 000 per il riferimento), quindi la soglia - tre spaziature di quella copia - è più larga della spaziatura di una
nuvola molto densa: `ppg chunk-distances --threshold` la imposta.

![Le distanze dopo l'allineamento](../img/gui/chunks-3d-distance.it.png)

**Opacità** sbiadisce gli altri chunk (anche con il renderer software): con i colori veri al 50 % due rilievi della stessa
trincea si leggono uno attraverso l'altro. Le spunte, i colori e l'opacità si salvano nel progetto; sono scelte della
vista, non passi di *Modifica > Annulla*. Mentre si vede una versione conservata, si disegna solo il chunk attivo.

*Flusso di lavoro > Allinea chunk...* ora finisce con lo stesso controllo: per ogni chunk allineato, la quota dei suoi
punti entro la soglia e la sua distanza mediana dal riferimento ([riga di comando](../api.md#allineare-e-unire-i-chunk):
`ppg align-chunks ... --check`, `ppg chunk-distances`). I DEM e le ortofoto dei chunk si confrontano nella vista 2D:
vedi il [pannello Layer](panes.md#layer).

## Tasti

Con la vista 3D attiva:

| Tasto | Cosa fa |
| --- | --- |
| ++left++ ++right++ | ruota di 15 gradi intorno alla verticale |
| ++up++ ++down++ | ruota di 15 gradi intorno all'asse orizzontale dello schermo |
| ++shift++ + frecce | sposta di un decimo della vista |
| ++plus++ ++minus++ | avvicina e allontana |
| ++home++ | vista iniziale |
| ++t++ | dall'alto |
| ++f++ | di fronte (guardando verso +y) |
| ++l++ | da sinistra (guardando verso +x) |

La stessa trackball funziona con i due motori di disegno, VTK (OpenGL) e quello software (`PPG_RENDERER=software`).

??? example "API"
    `window.model_view.pivot()` (il centro di rotazione, coordinate mondo del chunk),
    `window.model_view.set_pivot(point, recentre=False)`, `window.model_view.set_trackball_mode("auto" | "always" |
    "never")`; negli script lanciati dalla console o dall'[assistente](assistant.md) la finestra è `window`.
