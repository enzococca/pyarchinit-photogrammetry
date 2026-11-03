# Script

Tutto ciò che fa l'interfaccia è una chiamata dell'API Python ([API Python](api.md)): quello che fai nella finestra si
può ripetere in uno script, in un ciclo o su molti chunk. Uno script si lancia in tre modi:

* dalla **console Python** della finestra, un pannello in basso che si porta davanti dal menu *Vista* o con
  ++ctrl+shift+p++ ([Pannelli](gui/panes.md#console-python)): una riga alla volta; un `for` o un `def` prosegue nelle
  righe seguenti, dopo il prompt `...`, e finisce con una riga vuota;
* da **Strumenti > Esegui script Python...** ([Menu](gui/menus.md#strumenti)): il file viene letto ed eseguito in
  secondo piano, la finestra resta viva e *Modifica > Annulla* riprende tutto lo script in un solo passo; la console
  mostra ciò che stampa;
* dalla riga di comando, `ppg run script.py [--project progetto.ppg] [argomenti]`, senza finestra.

## I nomi che uno script trova

Nella console e in **Esegui script Python...** questi nomi sono già pronti, legati al progetto aperto:

| Nome | Che cos'è |
| --- | --- |
| `doc` | il progetto (`ppg.Document`): `doc.chunks`, `doc.chunk`, `doc.save()` |
| `chunk` | il chunk attivo, `doc.chunk` |
| `app` | lo stato dell'applicazione (`ppg.app`): `app.document` è il progetto aperto, lo stesso di `doc` |
| `ppg` | il pacchetto, con `import pyarchinit_photogrammetry as ppg` già fatto |
| `np` | NumPy |
| `window` | la finestra principale: `window.mark_modified()`, `window.refresh_all()`, `window.selected_polygon()`. In **Esegui script Python...** lo script gira in secondo piano e `window` passa ogni attributo e metodo della finestra al thread della finestra, aspettando la risposta, così gli script scritti per la finestra funzionano senza cambiarli; i nomi supportati sono `window.show_raster("dem")`, `window.show_model("model")`, `window.log("testo")` e `window.refresh()` (fatti senza aspettare), `window.selected_polygon()` (la selezione di quando lo script è partito) e `window.mark_modified()` (non serve: la voce dello script in *Modifica > Annulla* segna già il progetto come modificato); `window.runner`, `window.history`, `window.undo` e `window.redo` sono rifiutati (lo script aspetterebbe se stesso o annullerebbe il progetto sotto la sua stessa voce) |

**Esegui script Python...** (dalla 1.7.0) aggiunge questi nomi, perché lo script gira in secondo piano, lontano dalla
finestra:

| Nome | Che cos'è |
| --- | --- |
| `polygon` | i poligoni selezionati nella vista 2D quando lo script è partito, uniti (una geometria shapely), oppure `None` |
| `cameras` | le foto selezionate nel pannello *Foto* quando lo script è partito |
| `progress` | il segnalatore del passo: `progress(0.5, "a metà")` fa avanzare la barra della finestra di avanzamento |
| `cancel` | `cancel()` ferma lo script se nella finestra di avanzamento è stato premuto *Annulla* (altrimenti restituisce `False`): chiamalo nei cicli lunghi |

Ciò che lo script stampa va nella console e, riga per riga, nei dettagli della finestra di avanzamento. Un errore ferma
lo script e il suo traceback va nella console, dopo ciò che aveva stampato; `exit()` o `raise SystemExit(...)` chiude lo
script, mai il programma, e lo stesso vale nella console. Un `KeyboardInterrupt` sollevato dallo script lo ferma come
*Annulla*. Mentre uno script gira non parte un altro passo (lo dice la barra di stato). Viene raccolto solo ciò che
stampa lo script: ciò che stampa la finestra nel frattempo va dove va sempre.

Con `ppg run` non c'è la finestra: c'è solo il progetto, con `ppg.app.document`.

```python
import pyarchinit_photogrammetry as ppg

doc = ppg.app.document
chunk = doc.chunk
print(chunk.label, len(chunk.cameras), "foto")
```

## Che cosa riprende *Modifica > Annulla* di uno script

Uno script lanciato con **Esegui script Python...** è una sola voce di *Modifica > Annulla*, col nome del suo file, e
*Modifica > Ripeti* lo rifà. È una sola voce anche quando lo script si è fermato a metà per un errore: torna indietro
anche ciò che aveva fatto prima dell'errore. Riprende:

* ogni chunk com'era: foto e loro pose, sensori, marker, barre di scala, regione, impostazioni e disegni tenuti nel
  chunk, e le impostazioni del progetto (tra cui il chunk attivo);
* i prodotti che lo script ha ricostruito - nuvola densa, modello e texture, DEM, ortofoto, punti di legame, mappa
  delle normali e modello COLMAP dell'allineamento: prima che lo script ne scriva uno, il prodotto com'era viene
  conservato (come versione per i primi quattro, come copia per gli altri) e Annulla lo rende di nuovo quello attivo,
  con gli stessi file. La versione fatta così compare tra le versioni finché la voce si può annullare, e viene
  cancellata quando la voce esce dalla cronologia, a meno che il progetto sia stato salvato dopo lo script: allora
  resta, perché il progetto salvato la elenca;
* le versioni che lo script ha reso attive o ha cancellato: i loro file restano finché la voce è nella cronologia;
* i chunk che lo script ha aggiunto (fuori dal progetto, con la loro cartella) e quelli che ha tolto (di nuovo al loro
  posto, con la loro cartella);
* i file che lo script ha scritto nella cartella del progetto (`<progetto>.ppg.files`) e che prima non c'erano.

Non riprende ciò che lo script ha fatto fuori dal progetto: schede scritte in PyArchInit, disegni mandati al
GeoPackage dal vivo di QGIS, file esportati altrove. Quando lo script ha fatto chiamate del genere, la finestra le
elenca alla fine ("Non annullabili: ..."). I file che c'erano già e che lo script ha riscritto, a parte i prodotti
elencati sopra (raster del rilievo, mappe di precisione e di copertura, raster multispettrali e termici, file delle
cartelle di elaborazione), restano come li ha lasciati lo script; lo stesso vale per i file fuori dalla cartella del
progetto. Uno script che apre un altro progetto non si può annullare: la finestra mostra il progetto aperto.

Annulla riprende lo script solo dallo stato in cui l'ha lasciato (i numeri ricalcolati entro un milionesimo contano come
uguali, così una modifica fatta e annullata nella finestra dopo lo script non è d'ostacolo): se dopo lo script il progetto è cambiato con un passo
che non è in *Modifica > Annulla* (un prodotto costruito dal menu *Flusso di lavoro*, per esempio), annullarlo
riprenderebbe solo metà delle cose. In quel caso la voce viene tolta dalla cronologia e la barra di stato lo dice.

## Il chunk attivo e i suoi prodotti

```python
chunk = doc.chunk                 # il chunk attivo; doc.chunk = doc.chunks[1] lo cambia
chunk.crs                         # il sistema di coordinate del chunk
chunk.product_path("dem")         # un Path, oppure None se il prodotto non esiste
chunk.dem_path                    # come chunk.product_path("dem")
chunk.orthomosaic_path            # come chunk.product_path("orthomosaic")
chunk.data_dir                    # la cartella del chunk dentro <progetto>.ppg.files/
```

I nomi di `product_path` sono quelli dell'area di lavoro: `point_cloud`, `model`, `dem`, `orthomosaic`, `tie_points` e
così via (`chunk.products` elenca quelli che il chunk ha). Un prodotto è un file nella cartella del chunk; si legge con la
libreria che preferisci (rasterio per i GeoTIFF del DEM e dell'ortofoto):

```python
import rasterio

with rasterio.open(chunk.dem_path) as dem:
    z = dem.read(1, masked=True)
    print("DEM %d x %d celle, %.3f m per cella" % (dem.width, dem.height, dem.res[0]))
    print("quota minima %.2f m, massima %.2f m" % (z.min(), z.max()))
```

## Marker, foto e regione

```python
for m in chunk.markers:                       # i marker, con l'etichetta che si vede nell'interfaccia
    print(m.label, m.position)                # position: il punto nel sistema interno, None se non è posizionato

m = chunk.marker("target 1")                  # per etichetta, chiave o codice del target
mondo = chunk.to_world(np.array([m.position]))   # sistema interno -> coordinate del CRS del chunk

for c in chunk.cameras:                       # tutte le foto del chunk
    print(c.label, c.aligned, c.enabled)
len(chunk.aligned_cameras)                    # le foto attive e allineate: quelle che usano i passi

chunk.region                                  # la regione di ricostruzione (engine.region.Region) oppure None
chunk.reset_region()                          # intorno ai punti di legame
```

Uno script che cambia ciò che sta nel progetto (marker, regione, `chunk.meta`) lo dice alla finestra con
`window.mark_modified()`: il titolo mostra le modifiche non salvate, *Salva* le salva e *Modifica > Annulla* può
riprendere ciò che la cronologia segue (marker, barre di scala, regione, disegni). I file scritti da uno script e
l'attivazione delle foto (`enabled`) la cronologia non li segue.

## Rifare un prodotto

I passi sono metodi del chunk, con i parametri dei dialoghi ([API Python](api.md) li elenca tutti):

```python
chunk.build_dem(resolution=0.02, source="point_cloud", interpolate=True)
chunk.build_orthomosaic(resolution=0.01, surface="dem", blending="mosaic")
```

`build_dem(resolution, source, interpolate, classes)` scrive il DEM, con `classes=[2]` per usare solo i punti del suolo.
`build_orthomosaic(resolution, surface, blending, hide_classes, workers, max_angle, colour_correction, ghosting,
fill_holes)` scrive l'ortofoto sul DEM (`surface="dem"`) o sul modello. Si usano solo le foto attive e allineate. Nella
finestra le stesse chiamate stanno nel menu *Flusso di lavoro*.

## Esempi

I tre script qui sotto sono scritti per la console Python, dove `window` è la finestra stessa. Usano le funzioni che
il programma ha dalla 1.7.0: `chunk.region_from_markers(...)` e `chunk.build_normal_map(...)` (i primi due esempi, una
chiamata ciascuno; `ppg region-markers` e `ppg normal-map` dalla riga di comando, *Strumenti > Regione sui marker...* e
*Strumenti > Mappa delle normali...* nella finestra), poi `doc.duplicate_chunk(...)`, `chunk.set_cameras_enabled(...)` e
`window.selected_polygon()`, il nome pubblico della selezione della vista 2D (in uno script lanciato con *Esegui script
Python...* la stessa area è il nome `polygon`) nel terzo (`ppg duplicate-chunk` e `ppg photos disable` dalla riga di
comando; nella finestra *Duplica chunk...* nel clic destro di un chunk nell'Area di lavoro e *Disattiva le N foto
selezionate* nel clic destro del pannello Foto). Con un prospetto o una sezione mostrati nella vista 2D, la selezione è nei
suoi metri (u, v) (vedi *Su un prospetto o una sezione* nella guida dei plugin).

Due script completi per *Esegui script Python...* sono nella cartella `examples` del repository:
[`foto_area.py`](https://github.com/enzococca/pyarchinit-photogrammetry/blob/main/examples/foto_area.py) trova le foto che inquadrano l'area selezionata nella vista 2D e filtra con quelle
il pannello Foto, disattiva le altre (*Modifica > Annulla* le rimette), le riattiva o ne scrive l'elenco in CSV;
[`controllo_rilievo.py`](https://github.com/enzococca/pyarchinit-photogrammetry/blob/main/examples/controllo_rilievo.py) controlla il chunk attivo - foto, marker, barre di scala, GPS,
prodotti - e scrive un rapporto nella cartella del progetto senza cambiare il progetto. Le impostazioni sono in cima a
ogni file.

### La regione sul piano di tre marker

Per un oggetto o un muro, la regione della ricostruzione è una scatola sottile appoggiata sul piano passante per tre
marker: mappe di profondità, nuvola densa e modello si fanno solo al suo interno. Il piano dei marker diventa il piano
della scatola; la sua estensione nel piano resta quella della regione precedente (o quella dei punti di legame) e si
sceglie lo spessore.

```python
a, b, c = "target 1", "target 2", "target 3"          # le etichette di tre marker con una posizione

regione = chunk.region_from_markers(a, b, c, thickness=0.10)   # 5 cm di regione per lato del piano
window.mark_modified()
print("Regione %.3f x %.3f x %.3f m" % tuple(regione.size))
```

Gli assi della scatola sono le due direzioni del piano e la sua normale, rivolta verso le foto; `thickness` è la
dimensione lungo la normale, centrata sul piano. Con `extent="tie_points"` l'estensione nel piano viene dai punti di
legame anche se esiste già una regione.

### La mappa delle normali sulla griglia dell'ortofoto

Una mappa delle normali sugli stessi pixel e con le stesse coordinate dell'ortofoto: il rosso è la componente verso est
della normale della superficie, il verde quella verso nord, il blu quella verso l'alto, come nelle mappe delle normali di
Metashape. Serve a illuminare un disegno da un altro lato, o a mettere il rilievo sotto un disegno in QGIS.

```python
percorso = chunk.build_normal_map(source="auto")      # "model", "dem" oppure "auto" (il modello, se c'è)
print("Mappa delle normali:", percorso)

import rasterio
with rasterio.open(chunk.orthomosaic_path) as orto, rasterio.open(percorso) as normali:
    assert (orto.width, orto.height, orto.transform, orto.crs) == (
        normali.width, normali.height, normali.transform, normali.crs)
```

Il file è un GeoTIFF a quattro bande (RGB e l'alfa: trasparente dove non c'è superficie), salvato nella cartella del
chunk come prodotto `normal_map`.

### Solo le foto di un'area

Per rifare l'ortofoto di una parte dello scavo non servono tutte le foto. Seleziona l'area nella vista 2D con lo
strumento *Seleziona*, poi tieni solo le foto che la vedono: o un chunk nuovo con quelle foto (l'originale resta com'è),
oppure le altre disattivate nel chunk attivo.

```python
from pyarchinit_photogrammetry.engine.patches import photos_for_area

area = window.selected_polygon()                      # il poligono selezionato nella vista 2D, oppure None
if area is None:
    print("Seleziona prima un poligono nella vista 2D.")
else:
    if area.geom_type == "MultiPolygon":              # più poligoni selezionati: il più grande
        area = max(area.geoms, key=lambda g: g.area)
    anello = np.asarray(area.exterior.coords)         # il contorno (i buchi non contano)
    righe = photos_for_area(chunk, anello, limit=100_000)   # le foto, con quanta area copre ciascuna
    chiavi = {r["key"] for r in righe if r.get("coverage", 0) > 0}
    utili = [c for c in chunk.cameras if c.key in chiavi]
    print(len(utili), "foto su", len(chunk.cameras), "inquadrano l'area")

    CHUNK_NUOVO = True
    if CHUNK_NUOVO:
        nuovo = doc.duplicate_chunk(chunk, cameras=utili, label=chunk.label + " - area")
        print("Chunk nuovo:", nuovo.label, "con", len(nuovo.cameras), "foto")
    else:
        altre = [c for c in chunk.cameras if c.key not in chiavi]
        n = chunk.set_cameras_enabled(altre, False)    # le foto restano, i passi le saltano
        print("Disattivate:", n)
    window.mark_modified()
```

Con le foto disattivate, `chunk.build_orthomosaic(...)` usa solo le altre; per riattivarle,
`chunk.set_cameras_enabled(chunk.cameras, True)`. Il chunk nuovo porta le foto con le loro pose e i marker che
vedono, così mappe di profondità e passi successivi possono girare su di esso.
