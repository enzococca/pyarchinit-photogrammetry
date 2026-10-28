# Plugin

Un plugin è un pacchetto Python che aggiunge qualcosa a pyarchinit-photogrammetry senza modificarlo:
azioni nei menu della finestra (menu *Plugin*), comandi della riga di comando (`ppg plugin NOME`),
formati di esportazione (*File > Esporta*), lavori lunghi in background con la barra di avanzamento,
impostazioni proprie. I plugin sono raccolti nel repository
[pyarchinit-photogrammetry-plugins](https://github.com/enzococca/pyarchinit-photogrammetry-plugins),
di cui il programma legge l'indice.

Questa pagina spiega come installare e scrivere i plugin. Come usarli passo per passo - i dati che
servono, i dialoghi, come leggere i risultati - sta nelle [Guide ai plugin](plugins/guides.md).

## Installare i plugin

* Nella finestra: *Strumenti > Plugin...* elenca i plugin installati (attivi, disattivati, o l'errore
  che ne ha tenuto fuori uno) e quelli del repository dei plugin: *Installa / aggiorna*, *Attiva / disattiva*,
  *Rimuovi*. Le modifiche valgono dal prossimo avvio (*Riavvia ora*).
* Dalla riga di comando:

  ```bash
  ppg plugins available          # i plugin dell'indice
  ppg plugins install NOME       # nella cartella dei plugin del programma
  ppg plugins update [--dry-run] # versioni più nuove dei plugin installati (sotto)
  ppg plugins list               # installati, attivi o no
  ppg plugins disable NOME       # conservato, non caricato
  ppg plugins remove NOME
  ppg plugin                     # i comandi che i plugin mettono a disposizione
  ppg plugin COMANDO ARGOMENTI...
  ```

* Con pip, in un ambiente Python: `pip install some-ppg-plugin`: il programma lo trova dal suo
  entry point.

Il programma installa i plugin in una cartella propria (`~/Library/Application Support/pyarchinit-photogrammetry/plugins`
su macOS, `%LOCALAPPDATA%\pyarchinit-photogrammetry\plugins` su Windows, `~/.local/share/...` su
Linux): l'applicazione impacchettata non ha pip. Un plugin che sta lì vince su uno con lo stesso nome
installato con pip. Così si possono installare solo i plugin in puro Python (wheel `-py3-none-any`),
che possono usare i moduli che il programma porta con sé (numpy, SciPy, OpenCV, Open3D, pycolmap, rasterio,
shapely, pyproj, SQLAlchemy, PySide6, ...).

**I plugin sono codice Python con gli stessi diritti del programma: installa solo quelli di cui ti fidi.**
`PPG_NO_PLUGINS=1` avvia il programma senza nessun plugin (`PPG_PLUGINS_ENVIRONMENT=0`: solo quelli
della cartella dei plugin, nessuno installato con pip); un plugin che fallisce all'avvio viene segnalato
(nel log e in *Strumenti > Plugin*) e lasciato fuori, il programma va avanti.

## Aggiornamenti

I plugin installati dal programma si aggiornano da soli. Il programma guarda l'indice dei plugin insieme al suo
controllo di una versione nuova: al massimo una volta al giorno, qualche secondo dopo l'avvio, e subito con
*Aiuto > Controlla aggiornamenti...*. L'indice viene letto in background, la finestra resta libera.
*Aiuto > Controlla gli aggiornamenti all'avvio* e `PPG_NO_UPDATE_CHECK=1` spengono il controllo giornaliero.

* Una versione più nuova di un plugin **installato dal programma** viene scaricata, controllata con il suo SHA-256
  e tenuta da parte. Diventa il plugin al **prossimo avvio** del programma, prima che giri qualunque plugin.
  Ogni copia del programma che parte mette a posto gli aggiornamenti - anche una partita mentre una finestra è
  aperta (un secondo doppio clic su un progetto in Windows, `ppg plugin NOME` in un terminale): la finestra già
  aperta tiene i moduli che ha caricato, quindi riavviala. Un riepilogo dice che cosa è stato scaricato; *Riavvia
  ora* chiude la finestra e la riapre con il progetto aperto.
* Un plugin installato **con pip** non viene mai toccato. Un plugin che hai disattivato viene aggiornato e resta
  disattivato.
* Un aggiornamento che richiede un programma più nuovo aspetta. Non viene scaricato: il riepilogo lo elenca sotto
  *In attesa*, con il motivo (per esempio la versione del programma che serve).
* Un plugin **nuovo** nell'indice viene solo annunciato, una volta. Il programma non installa mai un plugin da
  solo: *Apri il gestore dei plugin* nel riepilogo porta a *Installa / aggiorna*. Il primo controllo registra
  soltanto che cosa contiene l'indice: non annuncia nessun plugin come nuovo (gli aggiornamenti e i plugin che
  aspettano vengono detti, come a ogni controllo).
* Il riepilogo compare per ciò che è nuovo. Un plugin che aspetta un programma più nuovo, un aggiornamento che si
  potrebbe installare ma non è stato installato (l'interruttore qui sotto è spento) e un aggiornamento fallito
  vengono detti **una volta** per ogni versione - uno fallito, una volta per ogni messaggio - e di nuovo solo quando
  l'indice ha una versione più nuova del plugin. *Aiuto > Controlla aggiornamenti...* li mostra di nuovo tutti, e
  gli aggiornamenti scaricati prima che aspettano il riavvio. Finché è aperta una finestra di dialogo, il riepilogo
  aspetta che si chiuda.
* L'interruttore *Aiuto > Aggiorna i plugin da soli* (acceso finché non lo spegni) decide se le versioni più nuove
  vengono scaricate. Spento, il riepilogo le elenca sotto *Aggiornamenti disponibili* e si installano da
  *Strumenti > Plugin...*, come prima. Spegnerlo toglie anche gli aggiornamenti già scaricati che aspettano il
  riavvio: una riga nel log li nomina.
* In *Strumenti > Plugin...* un aggiornamento già scaricato compare nella colonna *Note* come pronto, attivo dal
  prossimo avvio.

Dalla riga di comando `ppg plugins update` fa lo stesso controllo e scarica le versioni più nuove (con `--dry-run`
dice soltanto quali si potrebbero aggiornare); `ppg plugins available` segna i plugin installati che ne hanno una
più nuova. `ppg plugin NOME` scrive una riga per ogni aggiornamento che mette a posto.

## Iniziare un plugin: `ppg plugins new`

Il programma scrive il progetto di un plugin da cui partire: un piccolo plugin funzionante con le parti
scelte, i suoi test, i README in inglese e in italiano, la nota di licenza, un `.gitignore` e un workflow
GitHub che costruisce la wheel di una release.

```bash
ppg plugins new "Conta foto" --dir ~/progetti --author "Your Name" --email you@example.org \
    --description "counts the photos of a chunk" --parts action,background,command,exporter
```

`--parts` dice con che cosa parte il plugin, ognuna con un esempio funzionante: `action` (un'azione nel
menu *Plugin* che mostra un riepilogo del chunk), `background` (un'azione che legge il DEM in
background con la barra di avanzamento), `command` (`ppg plugin conta-foto PROGETTO`), `exporter` (un
formato in *File > Esporta*). La cartella `ppg-conta-foto/` contiene il pacchetto `ppg_conta_foto` con la
classe `ContaFotoPlugin` e l'entry point `conta-foto`, `tests/`, `README.md`, `README.it.md`, `LICENSE`,
`.github/workflows/release.yml`. Il comando finisce con i passi successivi: `pip install -e .`
nell'ambiente del programma, `pytest -q tests`, poi la wheel e la pull request (sotto).

Nella finestra lo stesso si fa da *Strumenti > Plugin... > Nuovo plugin...*: nome, titolo, descrizione,
autore, cartella e le parti da spuntare; la cartella si apre nel gestore dei file appena è scritta.

## Scrivere un plugin

Un plugin è un pacchetto con un entry point nel gruppo `pyarchinit_photogrammetry.plugins`:

```
ppg-chunk-summary/
    pyproject.toml
    ppg_chunk_summary/__init__.py
```

```toml
[build-system]
requires = ["setuptools>=77"]
build-backend = "setuptools.build_meta"

[project]
name = "ppg-chunk-summary"
version = "0.1.0"
description = "A summary of the chunk: photos, markers, products"
requires-python = ">=3.10"
dependencies = []          # niente che il programma non porti già con sé

[project.entry-points."pyarchinit_photogrammetry.plugins"]
chunk-summary = "ppg_chunk_summary:SummaryPlugin"
```

```python
from pyarchinit_photogrammetry.plugins import Plugin


class SummaryPlugin(Plugin):
    title = "Chunk summary"
    description = "Photos, aligned photos, markers and products of the chunk"

    def activate(self, context):
        self.context = context
        context.add_action("Riepilogo del chunk", self.show)
        context.add_command("summary", self.command, help="summary of the chunk of a project")
        context.add_exporter("Riepilogo (Markdown)", ".md",
                             lambda chunk, path, progress: path.write_text(self.text(chunk)))

    @staticmethod
    def text(chunk) -> str:
        return (f"# {chunk.label}\n\n- photos: {len(chunk.cameras)}\n"
                f"- aligned: {len(chunk.aligned_cameras)}\n- markers: {len(chunk.markers)}\n")

    def show(self):
        from pyarchinit_photogrammetry.gui.dialogs import message

        chunk = self.context.app.document.chunk
        message(self.context.window, "info", self.title, self.text(chunk))

    def command(self, project):
        import pyarchinit_photogrammetry as ppg

        print(self.text(ppg.Document.open(project).chunk))
        return 0
```

Mentre sviluppi, `pip install -e .` nell'ambiente del programma mette in uso il plugin dal
prossimo avvio (`ppg plugin` elenca subito il suo comando).

### Che cosa riceve `activate`

| `context.` | |
|---|---|
| `app` | lo stato condiviso con gli script: `context.app.document` è il progetto aperto, `.chunk` il suo chunk attivo ([API](api.md)) |
| `window` | la finestra principale, o `None` nella riga di comando |
| `add_action(title, callback, menu="", tip="", processing=False)` | un'azione di menu sotto *Plugin* (nel sottomenu del plugin, o in `menu`); `processing=True` la disattiva mentre gira un passo dell'elaborazione |
| `add_command(name, function, help="")` | `ppg plugin NOME ARGOMENTI...` chiama `function(*args)` con gli argomenti come stringhe; un int restituito è il codice d'uscita |
| `add_exporter(title, suffix, function)` | *File > Esporta > title...*: `function(chunk, path, progress)` scrive `path` in background |
| `store_drawings()` | scrive nel progetto quello che la finestra mostra (unità e quote disegnate, pietre, sezioni) prima che un plugin legga `chunk.meta`; solo nel thread dell'interfaccia (un'azione, `done`), niente nella riga di comando; la finestra lo fa da sola prima di eseguire un esportatore |
| `run(title, function, done=None)` | `function(progress)` in background con la barra di avanzamento della finestra (subito, nella riga di comando), poi `done(result)` nel thread dell'interfaccia |
| `data_dir` | una cartella per i file del plugin |
| `settings`, `save_settings()` | un dizionario conservato da una sessione all'altra |
| `log` | un logger: i suoi messaggi vanno nel log del programma |
| `request_polygon(on_finished, on_cancelled=None, hint="")` | dalla 1.3.2: l'utente disegna un poligono sulla vista 2D con lo strumento del programma (trascina un riquadro, oppure clic per i vertici e doppio clic o Invio; Backspace toglie l'ultimo vertice, Esc annulla); `on_finished(xy)` riceve un array `(N, 2)` nelle coordinate del chunk; `on_cancelled()` quando l'utente sceglie un altro strumento o arriva un'altra richiesta; `False` se non c'è niente su cui disegnare |
| `request_line(...)`, `request_point(...)` | lo stesso per una linea di due o più vertici e per un punto (`(1, 2)`) |
| `on_raster_loaded(callback)` | dalla 1.3.2: `callback()` ogni volta che la vista 2D mostra un altro raster, una cornice vuota o niente: lì si ridisegnano i layer e le sovrapposizioni del plugin (si scollega da sola quando il plugin viene tolto) |
| `on_refresh(callback)` | dalla 1.3.3: `callback()` ogni volta che la finestra mostra di nuovo il progetto (un altro progetto, un altro chunk attivo, un'elaborazione finita, un annulla): lì un pannello del plugin segue il chunk attivo (si scollega da sola quando il plugin viene tolto) |
| `show_frame(bounds, resolution, text="")` | dalla 1.3.2: una cornice vuota sulla vista 2D (`xmin, ymin, xmax, ymax` nelle coordinate del chunk, metri per pixel) su cui disegnare quando il chunk non ha ortofoto né DEM |
| `mark_modified()` | dalla 1.3.2: il progetto ha modifiche da salvare (`chunk.meta` o i file del plugin sono cambiati) |
| `call_in_gui(function, *args)` | dalla 1.3.2: `function(*args)` nel thread dell'interfaccia, dalla funzione data a `run` o da un esportatore |
| `activate_chunk(chunk)` | dalla 1.3.4: `chunk` diventa quello attivo, come quando l'utente lo sceglie nell'area di lavoro (i disegni restano col loro chunk, le viste mostrano il nuovo, le callback di `on_refresh` vengono chiamate); nella riga di comando, il chunk attivo del suo progetto |
| `show_raster(kind=None, chunk=None, path=None)` | dalla 1.3.4: l'ortofoto (`"ortho"`) o il DEM (`"dem"`) di `chunk` (quello attivo, oppure diventa quello attivo) nella vista 2D con i disegni della finestra, come li mostra la finestra; `None`: l'ortofoto, altrimenti il DEM; `path`: un altro GeoTIFF di quel tipo (una versione conservata, un raster del plugin); `False` se non c'è niente di quel tipo (la vista resta com'era) e nella riga di comando |
| `show_model(kind=None, chunk=None, path=None, title="")` | dalla 1.3.4: `"model"`, `"model_textured"`, `"point_cloud"`, `"tie_points"`, `"cameras"` o `"proxies"` di `chunk` nella vista 3D (`None`: il meglio che ha); `path`: una mesh (`"model"`) o una nuvola di punti (`"point_cloud"`) del plugin, un PLY nel sistema interno del chunk, col nome `title` accanto all'elenco *Mostra* finché l'utente non sceglie lì un'altra vista; `False` se non c'è niente di quel tipo e nella riga di comando |
| `on_features_deleted(layer, callback)` | dalla 1.3.4: `callback(features)` quando l'utente elimina elementi di `layer` sulla vista 2D (*Seleziona* e Canc, *Elimina selezionati*): i loro attributi, dizionari come li teneva il layer (`_geom`: tipo e coordinate), perché il plugin tolga i suoi record; un layer tolto dalla vista non è un'eliminazione (si scollega da sola quando il plugin viene tolto) |
| `add_tree_items(provider)` | dalla 1.3.4: i prodotti del plugin nell'area di lavoro, sotto ogni chunk dopo quelli del programma: `provider(chunk)` restituisce una lista di dizionari `{"label", "icon", "tooltip", "children", "activate", "actions", "expanded"}` (`icon`: un nome come `"index"`, `"zones"`, `"multispectral"`, o una QIcon; `activate()` al doppio clic o con Invio; `actions`: `[(testo, callback), ...]`, il menu del tasto destro; una chiave che manca è vuota). Viene chiamato per ogni chunk ogni volta che l'albero si riempie; le callback girano con il chunk dell'elemento come chunk attivo; un nodo tiene l'espansione che gli ha dato l'utente; un provider che fallisce finisce nel log e viene saltato; gli elementi se ne vanno con il plugin |
| `refresh_tree()` | dalla 1.3.4: l'area di lavoro riempita di nuovo, i provider interrogati di nuovo (dopo che il plugin ha creato o eliminato prodotti) |
| `add_pane(widget, title, icon=None, area="right")` | dalla 1.3.4: `widget` come pannello della finestra, un dock a sé raggruppato a schede con i pannelli di `area` (`"left"`, `"bottom"`, `"right"`): l'utente lo sposta, lo stacca, lo chiude e lo riapre da *Vista > Pannelli*, e la finestra ricorda dov'era. `icon` è una `QIcon` o il nome di un'icona del programma (`"plugin"`, `"relief"`...); restituisce il dock (nome dell'oggetto `pane_plugin_` + il nome del plugin), `None` nella riga di comando; il pannello se ne va con il plugin |
| `show_pane(widget)` | dalla 1.3.4: un pannello del plugin in primo piano, anche se l'utente lo ha chiuso o staccato |
| `add_rasters(provider)` | dalla 1.4.0: i raster del plugin con un nome nel calcolatore raster (*Strumenti > Calcolatore raster*, `engine.rastercalc`): `provider(chunk)` restituisce dizionari `{"name", "label", "path"}` (`name` nelle formule: lettere, cifre e `_`, reso tale se non lo è). Senza di esso il calcolatore trova comunque ogni GeoTIFF della cartella del chunk, con il nome della cartella e del file (`terrain_twi`) o del suo tag `ppg_name`; tolto con il plugin, anche dalla riga di comando |
| `rasters(chunk=None)` | dalla 1.4.0: tutti i raster di `chunk` (quello attivo) che il calcolatore legge, con il loro nome nelle formule: dizionari `name`, `label`, `path`, `source`, `bands`, `width`, `height`, `resolution`; una formula su di essi: `engine.rastercalc.calculate(chunk, formula, name)` |
| `add_units(polygons, tipo="US", attributes=None, chunk=None)` | dalla 1.5.0: unità stratigrafiche fatte dal plugin, come se fossero disegnate nella finestra - nel collegamento dal vivo con QGIS, numerate da *Proponi US e rapporti*, nelle schede US di PyArchInit. `polygons`: (Multi)Polygon di shapely o liste di vertici `(N, 2)` nelle coordinate del chunk (un contorno non valido viene reso valido, uno senza area lasciato fuori); `attributes`: un dizionario per tutte o uno per poligono - `us`, `area`, `tipo`, `sito` e le colonne di `pyunitastratigrafiche`, ogni altra chiave un attributo dell'unità; il sito è quello del progetto quando non è dato. Sul chunk attivo vengono disegnate e sono una voce di *Modifica > Annulla*; su un altro chunk, e dalla riga di comando, vanno in `chunk.meta["drawings"]`. Solo nel thread dell'interfaccia; restituisce quante ne ha aggiunte |

`progress(fraction, message)` riporta un valore tra 0 e 1 e solleva `Cancelled` quando l'utente annulla:
chiamala spesso nei cicli lunghi. I widget si toccano solo nelle callback delle azioni e in `done`, mai
nella funzione data a `run` (gira in un altro thread). `deactivate()` rilascia quello che `activate`
ha preso oltre alle sue registrazioni (file aperti, timer).

### Compatibilità

`Plugin.api` è l'API dei plugin per cui il plugin è stato scritto (`pyarchinit_photogrammetry.plugins.API_VERSION`,
ora 1): un programma con un'API più vecchia rifiuta il plugin con un messaggio. L'indice dà anche la
versione più vecchia del programma con cui un plugin funziona (`min_app`): un plugin che usa i metodi
indicati "dalla 1.3.2" chiede `min_app` 1.3.2, uno che usa `on_refresh` 1.3.3, uno che usa quelli "dalla 1.3.4"
1.3.4, `add_rasters` e `rasters` 1.4.0, `add_units` 1.5.0. I plugin scritti prima della 1.3.4 mettono il loro pannello fra le schede della
finestra con `window.left_tabs.addTab(pane, icon, title)`, lo mostrano con
`window.left_tabs.setCurrentWidget(pane)` seguito da `window.d_work.show()` e `window.d_work.raise_()`,
e lo tolgono con `removeTab(indexOf(pane))`: dalla 1.3.4 ogni pannello è un dock a sé, e queste
chiamate funzionano ancora - creano, mostrano e tolgono il dock del pannello.

## Pubblicare un plugin

1. Costruisci una wheel in puro Python: `python -m build --wheel` (`dist/ppg_chunk_summary-0.1.0-py3-none-any.whl`).
2. Allegala a una release del tuo repository (o pubblicala su PyPI).
3. Apri una pull request sul [repository dei plugin](https://github.com/enzococca/pyarchinit-photogrammetry-plugins)
   aggiungendo la tua voce a `index.json`:

   ```json
   {
     "name": "chunk-summary",
     "title": "Chunk summary",
     "description": "Photos, aligned photos, markers and products of the chunk",
     "i18n": {"it": {"title": "Riepilogo del chunk", "description": "Foto, foto allineate, marker e prodotti del chunk"}},
     "version": "0.1.0",
     "author": "Your Name",
     "homepage": "https://github.com/you/ppg-chunk-summary",
     "license": "GPL-2.0-or-later",
     "api": 1,
     "min_app": "1.1.0",
     "requires": [],
     "wheel": "https://github.com/you/ppg-chunk-summary/releases/download/v0.1.0/ppg_chunk_summary-0.1.0-py3-none-any.whl",
     "sha256": "the output of: shasum -a 256 ppg_chunk_summary-0.1.0-py3-none-any.whl"
   }
   ```

   `name` è il nome dell'entry point, `requires` elenca i moduli che il plugin importa oltre a
   quelli del programma, `i18n` dà titolo e descrizione in altre lingue (mostrati quando l'interfaccia
   le parla). `python scripts/make_entry.py WHEEL_URL --write` nel repository dei plugin
   crea la voce dalla wheel. Il repository controlla la voce quando la pull request viene aperta:
   l'hash, l'entry point, e che il plugin parta con il programma. Solo il manutentore
   fa il merge: quella è l'approvazione. Un plugin può anche stare nel repository dei plugin stesso, sotto
   `plugins/` (vedi il suo README); [`template/`](https://github.com/enzococca/pyarchinit-photogrammetry-plugins/tree/main/template),
   lì, è il plugin da cui parte `ppg plugins new`.
