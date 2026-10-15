# pyarchinit-photogrammetry

Motore fotogrammetrico open source per l'archeologia e il BIM dei beni culturali, integrato
con [PyArchInit](https://github.com/pyarchinit/pyarchinit).

Dalle foto a nuvole di punti, mesh, DEM e ortofoto georeferenziati, con gli strumenti che
servono in uno scavo: target codificati, punti di appoggio, sezioni, profili ceramici,
curve di livello, volumi di scavo, classificazione della nuvola, Segment Anything 2.1,
esportazione IFC e salvataggio diretto nel database di PyArchInit. Tutto è utilizzabile da
Python e gira su CPU, anche su Mac Apple Silicon.

> Stato: **alpha (0.2)**. Libreria Python, riga di comando e interfaccia desktop (sulla stessa
> API, con console Python integrata).

## Installazione

```bash
pip install "pyarchinit-photogrammetry[all]"   # mesh, IFC, PostGIS, interfaccia grafica
pip install "pyarchinit-photogrammetry[sam]"   # opzionale: Segment Anything 2.1
```

## Provare subito con i dati demo

```bash
ppg demo ~/ppg_demo --process     # foto sintetiche di un saggio con 8 target, ceramica, database PyArchInit
ppg-gui ~/ppg_demo/scavo.ppg      # apre l'interfaccia sul progetto elaborato
```

Oppure dall'interfaccia: **File > Crea e apri progetto demo**. Il file `LEGGIMI.txt` nella
cartella spiega cosa provare (sezioni, curve di livello, profilo ceramico del frammento,
pubblicazione in PyArchInit).

## Interfaccia grafica

`ppg-gui` (o `ppg gui`) apre l'applicazione desktop: area di lavoro con chunk, camere,
marker e prodotti; vista 3D (VTK, con un renderer software di riserva); ortofoto e DEM con
strumenti di disegno (unità stratigrafiche, tracce di sezione, misure, quote, prompt per
SAM 2.1); foto con posizionamento manuale dei marker; pannello riferimenti con errori sui
GCP e sui punti di controllo; console Python collegata al progetto (`doc`, `chunk`);
elaborazioni in background con avanzamento e annulla; menu PyArchInit per connettersi al
database, pubblicare ortofoto/DEM/modelli, scrivere i poligoni delle US e le quote. Guida in
[docs/gui.md](docs/gui.md).

Meglio un ambiente virtuale dedicato invece dell'ambiente `base` di Anaconda: le vecchie
build di SciPy lì presenti possono non caricarsi sui macOS recenti
(`ImportError ... __thread_bss ... zero-fill section`).

```bash
python3 -m venv ~/venvs/ppg && source ~/venvs/ppg/bin/activate
pip install -U pip && pip install "pyarchinit-photogrammetry[all]"
```

Le variabili `PROJ_LIB`/`PROJ_DATA` impostate da Anaconda o QGIS sono gestite in automatico.

Per scrivere geometrie in un database PyArchInit **SQLite** serve `mod_spatialite`: viene
cercato automaticamente nelle installazioni di QGIS, MacPorts, Homebrew e Linux, oppure si
indica con la variabile `SPATIALITE_LIBRARY_PATH`.

## Uso da Python

```python
import pyarchinit_photogrammetry as ppg

doc = ppg.Document()
chunk = doc.add_chunk("Saggio 1")
chunk.add_photos(["foto/"])
chunk.detect_markers("aruco_4x4_50")
chunk.match_photos(quality="high")
chunk.align_cameras()
chunk.import_reference("gcp.csv", crs="EPSG:3004")
print(chunk.update_transform()["control_rmse"])
chunk.build_depth_maps()
chunk.build_point_cloud()
chunk.build_model()
chunk.build_texture()
chunk.build_dem()
chunk.build_orthomosaic()
doc.save("scavo.ppg")
```

Dalla riga di comando:

```bash
ppg markers sheet target.pdf --codes 0-23 --size-mm 40
ppg process scavo.ppg foto/ --gcp gcp.csv --crs EPSG:3004
ppg pyarchinit publish scavo.ppg "Lagoi" "Ortofoto saggio 1" --dem --model --link-us 1:101,1:102
```

## Collegamento con PyArchInit

La libreria legge `config.cfg` di PyArchInit e apre il database (SQLite/SpatiaLite o
PostgreSQL/PostGIS) senza QGIS. Ortofoto, DEM e modelli 3D vengono registrati come media
con le stesse miniature di PyArchInit, collegati alle US, con scheda in
`documentazione_table` (`Fotopiano`, `DEM`, `Modello tridimensionale`, sorgente
`Fotogrammetria`) e ingombro in `pyarchinit_documentazione`. Si possono scrivere anche
poligoni delle US, quote, tracce di sezione e computi metrici. Dettagli in
[docs/pyarchinit.md](docs/pyarchinit.md).

## Licenza

GNU GPL v2 o successiva, come PyArchInit.

Autore: Enzo Cocca
