# Interfaccia

L'interfaccia fa con il mouse tutto quello che l'[API Python](../api.md) fa negli script: ogni comando chiama
le stesse funzioni, quindi ciò che si fa nella finestra si può ripetere nella console o in uno script. Questa
pagina mostra la finestra e come è organizzata; le altre pagine di questa sezione entrano in ogni parte.

<div class="grid cards" markdown>

- :material-menu: **[Menu](menus.md)** - tutti i comandi, menu per menu
- :material-dock-left: **[Pannelli](panes.md)** - area di lavoro, riferimenti, layer, foto, sezioni, log
- :material-camera-marker: **[Foto e marker](photos-markers.md)** - foto, maschere, marker, barre di scala
- :material-cog-play: **[Elaborazione](processing.md)** - dall'allineamento all'ortofoto, versioni, pulizia
- :material-draw: **[Disegno e misure](drawing.md)** - unità, sezioni, quote, misure, SAM
- :material-shovel: **[Strumenti per l'archeologia](archaeology.md)** - PyArchInit, QGIS, proxy, matrix, fasi, ceramica
- :material-leaf: **[Multispettrale e termico](multispectral.md)** - riflettanza, indici, temperature
- :material-export: **[Esportazioni e rapporti](exports.md)** - rapporto PDF, tabelle Excel, formati web, importazioni
- :material-lifebuoy: **[Errori e aggiornamenti](help.md)** - log, diagnostica, aggiornamenti

</div>

## Avviare il programma

```bash
pip install "pyarchinit-photogrammetry[gui]"   # compreso in [all]
ppg-gui [progetto.ppg]                         # oppure: ppg gui, python -m pyarchinit_photogrammetry.gui
ppg-gui --doctor                               # diagnostica dell'ambiente (come ppg doctor)
ppg-gui --check                                # apre, disegna ed esce (test di avvio)
ppg-gui --run script.py [argomenti]            # uno script Python con i moduli del programma, senza finestra
```

Oppure si installa l'applicazione pronta per macOS e Windows: vedi [Pacchetti](../packaging.md).

All'avvio una schermata nei colori del logo mostra una barra di avanzamento: la percentuale e cosa si sta
caricando (NumPy e Open3D, PROJ e GDAL, COLMAP, la vista 3D, l'interfaccia). La finestra compare in circa un
secondo; il log registra quanto è durato l'avvio.

Aprire un progetto mostra la stessa scheda sopra la finestra (progetto, area di lavoro, vista 3D, ortofoto e
DEM). Le miniature delle foto arrivano dopo, in background, dalle piccole anteprime che le fotocamere
scrivono nell'EXIF: centinaia di foto da drone non bloccano la finestra, e la barra di stato le conta. Una foto
che non si legge (un file vuoto tenuto solo online da Dropbox o iCloud) ha una miniatura scura e un
suggerimento.

## La pagina iniziale

![La pagina iniziale](../img/gui/start-page.png)

Senza progetto il centro della finestra mostra la pagina iniziale:

* **Nuovo progetto**, **Apri progetto...** e **Progetto demo** (il [progetto demo](#il-progetto-demo));
* i **progetti recenti**, un clic per aprirne uno;
* *Guida* (questo sito), *Diagnostica ambiente*, *Cartella dei log* e *Sostieni il progetto*.

*File > Pagina iniziale* la riporta.

## La finestra

![Le aree della finestra](../img/gui/window-areas.png)

| | Area | Cosa contiene |
| --- | --- | --- |
| **1** | Barra dei menu | tutti i comandi: vedi [Menu](menus.md) |
| **2** | Barra del flusso di lavoro | i passi dalle foto ai prodotti, in ordine: vedi [sotto](#le-barre-degli-strumenti) |
| **3** | Barra *Disegno* | gli strumenti che disegnano e misurano su ortofoto e DEM: vedi [Disegno e misure](drawing.md) |
| **4** | Pannelli a sinistra | *Area di lavoro*, *Riferimenti*, *Layer*: il progetto, i suoi marker, ciò che è disegnato ([Pannelli](panes.md)) |
| **5** | Viste centrali | *Modello* (3D), *Ortofoto / DEM*, *Foto*: vedi [sotto](#le-viste-centrali) |
| **6** | Pannelli in basso | *Foto* (miniature), *Sezioni*, *Console Python*, *Log* |
| **7** | Pannelli a destra | *PyArchInit*, *Proxy US*, *Multispettrale*, *Termico* e i pannelli dei plugin |
| **8** | Barra di stato | l'avanzamento di un passo con il pulsante per fermarlo; sotto il cursore coordinate, quota, pendenza e i valori dei raster |

Ogni pannello si può spostare, staccare o chiudere, e *Vista > Pannelli* lo riporta: vedi
[Spostare i pannelli](panes.md#spostare-i-pannelli). La finestra sta sullo schermo di un portatile, fino a
circa 1000 x 650 pixel: i pulsanti vanno a capo, un pannello troppo basso per il suo contenuto scorre, una barra
che non ci sta toglie prima i testi (i suggerimenti tengono i nomi).

## Le barre degli strumenti

### La barra del flusso di lavoro

![La barra del flusso di lavoro](../img/gui/toolbar-workflow.png)

I pulsanti seguono l'ordine del lavoro, da sinistra a destra. Mentre un passo lavora, i pulsanti di
elaborazione sono disattivati.

| | Pulsante | Cosa fa |
| --- | --- | --- |
| ![](../img/gui/icons/apri.png){ width=20 } | Apri un progetto | come *File > Apri...* |
| ![](../img/gui/icons/salva.png){ width=20 } | Salva il progetto | come *File > Salva* |
| ![](../img/gui/icons/foto.png){ width=20 } | Aggiungi foto al chunk | le foto nel chunk attivo |
| ![](../img/gui/icons/marker.png){ width=20 } | Rileva i target codificati | trova i target stampati nelle foto |
| ![](../img/gui/icons/allinea.png){ width=20 } | Allinea le foto (SfM) | posizioni e orientamenti delle camere |
| ![](../img/gui/icons/riferimenti.png){ width=20 } | Importa le coordinate dei marker | i punti di controllo da un CSV |
| ![](../img/gui/icons/nuvola-densa.png){ width=20 } | Depth map e nuvola densa | la nuvola densa |
| ![](../img/gui/icons/modello.png){ width=20 } | Mesh 3D | il modello |
| ![](../img/gui/icons/texture.png){ width=20 } | Texture del modello | la texture del modello |
| ![](../img/gui/icons/dem.png){ width=20 } | Modello digitale di elevazione | il DEM |
| ![](../img/gui/icons/ortofoto.png){ width=20 } | Ortofoto (true ortho) | l'ortofoto |
| ![](../img/gui/icons/elabora-tutto.png){ width=20 } | Tutti i passi in sequenza | *Elabora tutto* (++ctrl+r++) |
| ![](../img/gui/icons/pyarchinit.png){ width=20 } | Pubblica in PyArchInit | ortofoto, DEM e modello nei media di PyArchInit |
| ![](../img/gui/icons/curve-di-livello.png){ width=20 } | Curve di livello | le curve del DEM |
| ![](../img/gui/icons/volume-scavo-riporto.png){ width=20 } | Volume scavo / riporto | il volume tolto e aggiunto |
| ![](../img/gui/icons/pianta-a-quota-slice-orizzontale.png){ width=20 } | Pianta a quota | una pianta del modello a una quota |

### La barra Disegno

![La barra Disegno](../img/gui/toolbar-drawing.png)

Sempre visibile sotto il flusso di lavoro: le modalità di disegno (*Sposta*, *Seleziona*, *Disegna US* con
*Mano libera* nel suo menu, *Vertici* con *Rimodella* nel suo menu, *Snap*, *Sezione*, *Misura*, *Quota*,
*Punto di misura*, *Marker*, *SAM*), *Patch*, *Elimina*, *Unisci*, *Cancella disegni*, e le scorciatoie ai
pannelli *Layer* e *Sezioni*, a QGIS e a *Pubblica*. Ogni strumento è descritto in
[Disegno e misure](drawing.md#la-barra-disegno).

## Le viste centrali

Le tre schede al centro mostrano lo stesso chunk in tre modi; ++f2++, ++f3++ e ++f4++ passano dall'una
all'altra (nell'area di lavoro ++f2++ invece rinomina).

=== "Modello"

    <div class="grid" markdown>

    ![Il modello con texture nella vista 3D](../img/gui/view-model.png)

    ![La nuvola densa nella vista 3D](../img/gui/view-cloud.png)

    </div>

    Punti di legame, nuvola densa, modello ombreggiato o con texture, camere e marker. *Mostra* sceglie cosa;
    trascinare con il tasto sinistro ruota, con il destro o il centrale sposta, la rotella ingrandisce.

    La barra sopra la vista ha gli strumenti che lavorano in 3D:

    * **Marker**: clic su un punto del modello o della nuvola per mettervi un marker;
    * **Foto del punto**: le foto che vedono il punto cliccato;
    * **Sezione** (due punti del modello o della nuvola) e **Pianta a quota** (un punto: la pianta alla sua
      quota);
    * **Taglia**: il modello tagliato con un piano ([tagliare il modello](processing.md#tagliare-il-modello));
    * **Esagerazione verticale** (da x0,1 a x50): le quote moltiplicate attorno al centro della scena, così si
      legge il rilievo di un terreno quasi piano. Cambia solo la vista: marker, sezioni e tagli prendono sempre
      le coordinate vere;
    * **Dall'alto** e **Vista iniziale**.

    La vista usa VTK (pyvista) quando c'è OpenGL, altrimenti un renderer software: `PPG_RENDERER=software` lo
    impone, `PPG_RENDERER=vtk` impone VTK.

=== "Ortofoto / DEM"

    ![L'ortofoto con le unità](../img/gui/view-ortho-units.png)

    L'ortofoto o il DEM (tinte ipsometriche sopra un'ombreggiatura) in coordinate reali, con tutti i layer
    disegnati sopra: unità, sezioni, quote, marker. Sotto il cursore la barra di stato legge i prodotti a piena
    risoluzione: X, Y, Z, pendenza ed esposizione, l'altezza dal suolo quando c'è un DTM conservato, la
    temperatura, le bande e gli indici.

    Un raster grande si apre da una copia ridotta (al massimo 6.000 pixel per lato) e, quando lo zoom si ferma
    oltre quella risoluzione, l'area visibile (con un margine) viene riletta alla risoluzione dello schermo,
    fino a quella piena, in background. Il dettaglio del DEM tiene i colori di tutto il DEM. Tell Abu
    adh-Dhuhur (44.952 px): 0,7 s per aprire, 10,8 cm/px prima del dettaglio, 1,38 cm/px dopo 0,37 s.

=== "Foto"

    ![Una foto con i suoi marker](../img/gui/view-photo.png)

    Una foto con i suoi marker, come in Metashape: le bandierine verdi sono misurate, le blu stimate e da
    confermare, le croci rosse sono dove il rilievo mette un marker non messo in quella foto. Si trascina una
    bandierina sul punto, si fa doppio clic su una blu per confermarla; ++page-down++ e ++page-up++ (oppure `]` e
    `[`) aprono la foto successiva e la precedente dell'elenco mostrato. Vedi [Marker](photos-markers.md#marker).

## Annulla e ripeti

*Modifica > Annulla* (++ctrl+z++) e *Ripeti* (++ctrl+y++, oppure ++ctrl+shift+z++) lavorano sui disegni, i
marker, le barre di scala, le patch dell'ortofoto, la regione di ricostruzione, le modifiche ai prodotti
(classi date a mano, punti tolti con il lazo, pulizie, facce del modello, patch della texture) e ciò che
l'area di lavoro rimuove. Il menu dice cosa verrà annullato.

* Prima di modificare un prodotto i suoi file vengono copiati a parte (sul Mac un clone: istantaneo e senza
  spazio), così l'annullamento li rimette esattamente com'erano.
* Ciò che torna da QGIS con il collegamento dal vivo è una voce per ogni salvataggio fatto lì (*Da QGIS: ...*),
  e annullandolo i proxy delle unità seguono i loro poligoni.
* I passi di elaborazione (allineamento, nuvola densa...) non si annullano: si usano le
  [versioni](processing.md#versioni).
* La cronologia riparte quando si apre un altro progetto; quando un altro chunk diventa attivo tiene solo le
  rimozioni (le altre modifiche appartengono al loro chunk).

## Lingua e tema

L'interfaccia parla italiano, come PyArchInit, o inglese: si sceglie da *Vista > Lingua / Language*; il
cambio vale dal prossimo avvio, e *Riavvia ora* riavvia subito con il progetto aperto. Senza una scelta segue
la lingua del sistema; `PPG_LANG=it` o `PPG_LANG=en` ne impone una per una sessione.

Sono tradotti menu, finestre, pannelli, messaggi, il log e l'avanzamento dei calcoli. Ciò che è dato resta
in italiano: i valori che PyArchInit rilegge (tipi di US, rapporti), i nomi dei layer e delle versioni dei
prodotti, i disegni e i loro file.

*Vista > Tema* passa dal tema chiaro a quello scuro (viene ricordato).

## Il progetto demo

**File > Crea e apri progetto demo** (oppure `ppg demo <cartella> --process`) scrive:

* un **saggio** sintetico di 4 x 3 m con un tratto di muro, 28 foto e 8 target ArUco in coordinate
  EPSG:3004;
* una **ciotola** e un **frammento** come mesh, e i target da stampare;
* un **database PyArchInit** con lo schema completo di PyArchInit 5 e il sito *Saggio dimostrativo* (US 1-3,
  poligoni, quote, una traccia di sezione, ceramica e reperti), più una villa romana e una necropoli con
  decine di unità;

poi apre il progetto e collega il database. Le schermate di questa guida vengono da lì.

Il **demo della ceramica** (`ceramica/ceramica.ppg`, 32 foto della ciotola su un tappetino per turntable con 12
target ArUco in un sistema locale in metri) mostra il lavoro ravvicinato: *Elabora tutto* senza DEM e ortofoto,
poi *Strumenti > Profilo ceramico da mesh* sul modello esportato; con `--process` il demo contiene già
`ceramica/profilo.svg` (orlo atteso 16 cm, altezza 12 cm, parete 6 mm). La finestra del demo fa scegliere quale
progetto aprire alla fine.

## Console e script

Tutto ciò che fa l'interfaccia si fa anche nel pannello *Console Python* (`doc`, `chunk`, `app`, `ppg`, `np`,
`window`) e negli script (`ppg run script.py --project scavo.ppg`, oppure *Strumenti > Esegui script
Python...*). Vedi l'[API Python](../api.md) e [PyArchInit](../pyarchinit.md).
