# Vista 3D e trackball

La scheda *Modello* mostra il chunk in 3D: punti di legame, nuvola densa, modello ombreggiato o con texture, il TIN,
camere, marker e regione di ricostruzione (*Mostra* sceglie cosa). Gli strumenti della sua barra - marker, foto del
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
  punto (come si piazza un marker); la vista non salta. **Centra la vista sul punto** (nella barra) porta poi il
  pivot al centro della vista, alla stessa distanza.
* *Vista iniziale* mostra di nuovo tutta la scena, *Dall'alto* guarda dritto verso il basso.

## Quando si vede la trackball

Il pulsante **Trackball** della barra, o *Vista > Trackball*, ha tre modi, ricordati tra una sessione e l'altra:

* **Quando il mouse è sulla vista** (predefinito): compare appena il mouse si muove sulla vista e sfuma un secondo e
  mezzo dopo che il mouse si è fermato;
* **Sempre**;
* **Mai**: la scena ruota allo stesso modo, solo la sfera non è disegnata.

La sfera non finisce mai in un'immagine presa dalla vista (schermate, lazo, scelta dei punti).

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
