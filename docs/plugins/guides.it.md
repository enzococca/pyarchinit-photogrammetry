# Guide ai plugin

Un plugin aggiunge strumenti a pyarchinit-photogrammetry senza modificarlo: un pannello, azioni nei menu,
comandi della riga di comando, formati di esportazione. I plugin sono raccolti nel
[repository dei plugin](https://github.com/enzococca/pyarchinit-photogrammetry-plugins) e il programma
li installa da solo. Queste guide spiegano come usarli - i dati che servono, i passi, come leggere i
risultati - per archeologi che non scrivono codice. Come funzionano i plugin, e come scriverne uno, sta in
[Plugin](../plugins.md).

## Installare un plugin { #install }

Nel programma:

1. *Strumenti > Plugin...* (*Tools > Plugins...* nell'interfaccia inglese).
2. *Aggiorna l'elenco*: la tabella in basso mostra i plugin del repository, con versione, autore,
   descrizione e una nota: se è già installato o se si può aggiornare, oppure che cosa gli serve - per
   esempio una versione più recente del programma.
3. Seleziona il plugin e premi *Installa / aggiorna*: il programma lo scarica, lo controlla e lo mette
   nella propria cartella dei plugin.
4. *Riavvia ora*: il plugin è attivo dall'avvio successivo, con il suo pannello e il suo menu
   (*Plugin > nome del plugin*).

La tabella in alto elenca i plugin installati - attivi, disattivati, o l'errore che ne ha tenuto fuori
uno - con *Attiva / disattiva* e *Rimuovi*. Per aggiornare un plugin, installalo di nuovo quando la nota
dice che si può aggiornare. I plugin sono codice Python con gli stessi diritti del programma: installa
solo quelli di cui ti fidi.

Dalla riga di comando:

```bash
ppg plugins available              # i plugin del repository
ppg plugins install geoarchaeology
ppg plugins list                   # quelli installati, attivi o no
ppg plugin geoarchaeology help     # i comandi di un plugin
```

L'applicazione desktop non ha pip: il programma installa i plugin in una cartella propria. Per
installarli con pip in un ambiente Python, vedi [Plugin](../plugins.md).

## Le guide

| Plugin | Che cosa fa | Guida |
|---|---|---|
| Geoarcheologia (`geoarchaeology`) | sondaggi, carote e sezioni di scavo sul rilievo: colonne stratigrafiche, superfici di contatto e spessori per kriging, sezioni, un modello 3D delle unità con i volumi | [Geoarcheologia](geoarchaeology.md) |
| Terreno (`terrain`) | analisi del DEM con WhiteboxTools: corsi d'acqua e bacini, posizione topografica e forme del terreno, curvatura e rugosità, conche e rilievi annidati con i loro volumi, visibilità | [Terreno](terrain.md) |
| Geofisica (`geophysics`) | griglie di prospezioni magnetometriche, di resistività, GPR ed EM: importazione, georeferenziazione sui picchetti della griglia, filtri, sovrapposizione all'ortofoto e al DEM, esportazioni | [Geofisica](geophysics.md) |
| Archivio GIS (`gis-archive`) | quote, curve di livello, US, sezioni, pietre, marker, DEM e ortofoto di un progetto in un database GeoPackage, PostGIS o SpatiaLite | [Archivio GIS](gis-archive.md) |
| terrio (`terrio`) | nuvole di punti di qualsiasi formato, anche dal web, nel progetto; classificazione del terreno; raster di riferimento; esportazioni E57, EPT, PMTiles e MBTiles | [terrio](terrio.md) |
| Spirula (`spirula`) | Gaussian splatting 3D delle foto allineate con Spirula Studio, una mesh dallo splat, l'installazione di Spirula Studio | [Spirula](spirula.md) |
| Linee elettriche (`powerlines`) | linee aeree nella nuvola densa: conduttori come catenarie con freccia, franco e ostacoli, pali, classi, esportazioni 3D | [Linee elettriche](powerlines.md) |
| Mesh (MeshLab) (`mesh-tools`) | i filtri di MeshLab sul modello del chunk: decimazione che conserva la texture, rimaglia isotropa, levigatura Taubin e HC, buchi chiusi lasciando aperto il contorno, topologia riparata, misure; ogni risultato una nuova versione del modello | [Mesh (MeshLab)](mesh-tools.md) |
| Sentinel-2 (`sentinel2`) | serie temporali Sentinel-2 sul rilievo per cercare i crop mark: ricerca in un catalogo pubblico, nuvole mascherate, NDVI, NDRE e NDMI per data, serie e grafico, compositi e mappe dei crop mark sopra l'ortofoto, QGIS ed esportazioni | [Sentinel-2](sentinel2.md) |
| Blender ed EMtools (`blender-em`) | i proxy 3D delle unità e il rilievo in un progetto Extended Matrix, con i nomi dei nodi dell'em.json di PyArchInit o EMStudio; Blender aperto sul progetto e aggiornato dal vivo quando i proxy cambiano; la selezione condivisa dal vivo con EMtools 1.6 | [Blender ed EMtools](blender-em.md) |

Ogni guida ha le stesse parti: che cosa fa il plugin e che cosa serve prima, i dati e come prepararli, i
passi nel programma e dalla riga di comando, come leggere i risultati, i file che scrive.
