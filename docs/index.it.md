# pyarchinit-photogrammetry

Fotogrammetria open source per l'archeologia e il BIM dei beni culturali, integrata con
[PyArchInit](https://github.com/pyarchinit/pyarchinit).

Dalle foto a nuvole di punti, modelli, DEM e ortofoto georeferenziati, con gli strumenti che servono
in uno scavo: target codificati e punti di controllo, sezioni e piante, curve di livello e volumi,
unità stratigrafiche e quote con i campi di PyArchInit, profili ceramici, classi dei punti, Segment
Anything 2.1, esportazione IFC. Un'applicazione desktop, una riga di comando (`ppg`) e una libreria
Python sullo stesso motore, che gira sulla CPU (anche Apple Silicon). Licenza GPL-2.0-or-later.

[:material-heart: Sostieni il progetto su PayPal](https://paypal.me/enzococca){ .md-button }

<div class="grid cards" markdown>

-   :material-download: **[Installazione](install.md)**

    L'applicazione per macOS e Windows, oppure `pip install` su macOS, Windows e Linux (x86-64 e
    ARM).

-   :material-school: **[Tutorial](tutorials/index.md)**

    Passo per passo, con il progetto dimostrativo e con le foto vere di un tell.

-   :material-monitor: **[Interfaccia](gui.md)**

    Menu, pannelli e strumenti della finestra (in inglese).

-   :material-language-python: **[API Python](api.md)**

    Documenti, chunk e flusso di lavoro negli script, anche dentro la finestra (*Console Python*).

-   :material-database: **[PyArchInit](pyarchinit.md)**

    Ortofoto, DEM, modelli, US, quote e sezioni in un database PyArchInit.

-   :material-puzzle-outline: **[Plugin](plugins.md)**

    Azioni di menu, comandi e formati di esportazione aggiunti; il
    [repository dei plugin](https://github.com/enzococca/pyarchinit-photogrammetry-plugins).

</div>

![La vista del DEM con le US da PyArchInit, curve di livello e una sezione](img/gui-layers.png)

## Cosa fa

* **Ricostruzione**: allineamento (COLMAP), mappe di profondità (CPU, PatchMatch in Rust, oppure
  COLMAP/OpenMVS), nuvola densa con confidenza, modello (Poisson, campo di altezze o fusione delle
  mappe di profondità), texture, DEM e ortofoto (ortofoto vera, senza cuciture, con patch, a piena
  risoluzione anche su tell grandi).
* **Georeferenziazione**: target ArUco/AprilTag trovati da soli, punti di controllo e di verifica,
  barre di scala, ottimizzazione delle camere sui punti di controllo, rapporto con gli errori.
* **Archeologia**: sezioni e piante in scala (SVG/DXF), curve di livello, volumi di scavo e riporto,
  visualizzazioni del rilievo (sky-view factor, openness, multi-hillshade), US e quote disegnate
  sull'ortofoto, proposte di US e rapporti, colori Munsell, profili ceramici dal piatto rotante,
  pietre ed elementi con SAM.
* **Scambio**: PyArchInit (SQLite/SpatiaLite e PostgreSQL/PostGIS), QGIS dal vivo, QField, IFC 4,
  scene I3S, COPC, 3D Tiles, glTF; importazione di progetti Metashape, WebODM e Photo3D.

## Stato

Versione 1.0: applicazione desktop, riga di comando e libreria. Sorgenti, segnalazioni e versioni
sono su [GitHub](https://github.com/enzococca/pyarchinit-photogrammetry); il piano delle prossime
versioni è nella [roadmap](roadmap.md) (in inglese).
