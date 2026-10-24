# Plugins

A plugin is a Python package that adds to pyarchinit-photogrammetry without changing it: menu
actions in the window (menu *Plugin*), commands of the command line (`ppg plugin NAME`), export
formats (*File > Esporta*), long work in the background with a progress bar, settings of its own.
Plugins are collected in the repository
[pyarchinit-photogrammetry-plugins](https://github.com/enzococca/pyarchinit-photogrammetry-plugins),
whose index the program reads.

This page is about installing and writing plugins. How to use them step by step - the data they
need, the dialogs, how to read the results - is in the [Plugin guides](plugins/guides.md).

## Installing plugins

* In the window: *Strumenti > Plugin...* lists the plugins installed (active, disabled, or the error
  that kept one out) and those of the plugin repository: *Installa / aggiorna*, *Attiva / disattiva*,
  *Rimuovi*. Changes apply from the next start (*Riavvia ora*).
* From the command line:

  ```bash
  ppg plugins available          # the plugins of the index
  ppg plugins install NAME       # into the plugin folder of the program
  ppg plugins list               # installed, enabled or not
  ppg plugins disable NAME       # kept, not loaded
  ppg plugins remove NAME
  ppg plugin                     # the commands the plugins give
  ppg plugin COMMAND ARGS...
  ```

* With pip, in a Python environment: `pip install some-ppg-plugin` - the program finds it by its
  entry point.

The program installs plugins into its own folder (`~/Library/Application Support/pyarchinit-photogrammetry/plugins`
on macOS, `%LOCALAPPDATA%\pyarchinit-photogrammetry\plugins` on Windows, `~/.local/share/...` on
Linux): the packaged application has no pip. A plugin there wins over one of the same name
installed with pip. Only pure-Python plugins (`-py3-none-any` wheels) can be installed that way,
and they may use the modules the program carries (numpy, SciPy, OpenCV, Open3D, pycolmap, rasterio,
shapely, pyproj, SQLAlchemy, PySide6, ...).

**Plugins are Python code with the same rights as the program: install only those you trust.**
`PPG_NO_PLUGINS=1` starts the program without any plugin (`PPG_PLUGINS_ENVIRONMENT=0`: only those
of the plugin folder, none installed with pip); a plugin that fails when it starts is reported (in
the log and in *Strumenti > Plugin*) and left out, the program goes on.

## Starting a plugin: `ppg plugins new`

The program writes the project of a plugin to start from: a small working plugin with the parts
chosen, its tests, READMEs in English and Italian, the licence notice, a `.gitignore` and a GitHub
workflow that builds the wheel of a release.

```bash
ppg plugins new "Conta foto" --dir ~/progetti --author "Your Name" --email you@example.org \
    --description "counts the photos of a chunk" --parts action,background,command,exporter
```

`--parts` says what the plugin starts with, each with a working example: `action` (an action in the
menu *Plugin* that shows a summary of the chunk), `background` (an action that reads the DEM in the
background with the progress bar), `command` (`ppg plugin conta-foto PROJECT`), `exporter` (a format
in *File > Esporta*). The folder `ppg-conta-foto/` holds the package `ppg_conta_foto` with the class
`ContaFotoPlugin` and the entry point `conta-foto`, `tests/`, `README.md`, `README.it.md`, `LICENSE`,
`.github/workflows/release.yml`. The command ends with the next steps: `pip install -e .` in the
environment of the program, `pytest -q tests`, then the wheel and the pull request (below).

In the window the same is *Strumenti > Plugin... > Nuovo plugin...*: name, title, description,
author, folder and the parts to tick; the folder opens in the file manager when it is written.

## Writing a plugin

A plugin is a package with an entry point in the group `pyarchinit_photogrammetry.plugins`:

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
dependencies = []          # nothing the program does not already carry

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

While developing, `pip install -e .` in the environment of the program puts the plugin in use at
the next start (`ppg plugin` lists its command at once).

### What `activate` receives

| `context.` | |
|---|---|
| `app` | the state shared with scripts: `context.app.document` is the open project, `.chunk` its active chunk ([API](api.md)) |
| `window` | the main window, or `None` in the command line |
| `add_action(title, callback, menu="", tip="", processing=False)` | a menu action under *Plugin* (in the plugin's submenu, or in `menu`); `processing=True` disables it while a step runs |
| `add_command(name, function, help="")` | `ppg plugin NAME ARGS...` calls `function(*args)` with the arguments as strings; an int returned is the exit code |
| `add_exporter(title, suffix, function)` | *File > Esporta > title...*: `function(chunk, path, progress)` writes `path` in the background |
| `store_drawings()` | writes what the window shows (units and spot heights drawn, stones, sections) into the project before a plugin reads `chunk.meta`; in the interface thread only (an action, `done`), nothing in the command line; the window does it by itself before an exporter runs |
| `run(title, function, done=None)` | `function(progress)` in the background with the progress bar of the window (at once in the command line), then `done(result)` in the interface thread |
| `data_dir` | a folder for the plugin's files |
| `settings`, `save_settings()` | a dictionary kept between sessions |
| `log` | a logger: its messages go to the log of the program |
| `request_polygon(on_finished, on_cancelled=None, hint="")` | since 1.3.2: the user draws a polygon on the 2D view with the program's own tool (drag a box, or click the vertices and double click or Enter; Backspace drops the last vertex, Esc cancels); `on_finished(xy)` gets an `(N, 2)` array in the coordinates of the chunk; `on_cancelled()` when the user picks another tool or another request comes; `False` when there is nothing to draw on |
| `request_line(...)`, `request_point(...)` | the same for a line of two or more vertices and for one point (`(1, 2)`) |
| `on_raster_loaded(callback)` | since 1.3.2: `callback()` each time the 2D view shows another raster, an empty frame or nothing: draw your layers and overlays again there (disconnected by itself when the plugin goes away) |
| `on_refresh(callback)` | since 1.3.3: `callback()` each time the window shows the project again (another project, another active chunk, a processing step done, an undo): a pane of the plugin follows the active chunk there (disconnected by itself when the plugin goes away) |
| `show_frame(bounds, resolution, text="")` | since 1.3.2: an empty frame on the 2D view (`xmin, ymin, xmax, ymax` in the coordinates of the chunk, metres per pixel) to draw on when the chunk has no orthophoto or DEM |
| `mark_modified()` | since 1.3.2: the project has changes to save (`chunk.meta` or the plugin's files changed) |
| `call_in_gui(function, *args)` | since 1.3.2: `function(*args)` in the interface thread, from the function given to `run` or from an exporter |
| `activate_chunk(chunk)` | since 1.3.4: `chunk` becomes the active one, as when the user picks it in the workspace (the drawings stay with their chunk, the views show the new one, the `on_refresh` callbacks are called); in the command line, the active chunk of its project |
| `show_raster(kind=None, chunk=None, path=None)` | since 1.3.4: the orthophoto (`"ortho"`) or the DEM (`"dem"`) of `chunk` (the active one, or it becomes the active one) in the 2D view with the drawings of the window, as the window shows them; `None`: the orthophoto, else the DEM; `path`: another GeoTIFF of that kind (a kept version, a raster of the plugin); `False` when there is nothing of that kind (the view stays as it was) and in the command line |
| `show_model(kind=None, chunk=None, path=None, title="")` | since 1.3.4: `"model"`, `"model_textured"`, `"point_cloud"`, `"tie_points"`, `"cameras"` or `"proxies"` of `chunk` in the 3D view (`None`: the best it has); `path`: a mesh (`"model"`) or a point cloud (`"point_cloud"`) of the plugin, a PLY in the internal frame of the chunk, named `title` beside the list *Mostra* until the user picks another view there; `False` when there is nothing of that kind and in the command line |
| `on_features_deleted(layer, callback)` | since 1.3.4: `callback(features)` when the user deletes features of `layer` on the 2D view (*Seleziona* and Canc, *Elimina selezionati*): their attributes, dicts as the layer held them (`_geom`: type and coordinates), for the plugin to drop its records of them; a layer taken out of the view is not a deletion (disconnected by itself when the plugin goes away) |
| `add_tree_items(provider)` | since 1.3.4: the plugin's products in the workspace, under each chunk after those of the program: `provider(chunk)` returns a list of dicts `{"label", "icon", "tooltip", "children", "activate", "actions", "expanded"}` (`icon`: a name such as `"index"`, `"zones"`, `"multispectral"`, or a QIcon; `activate()` on a double click or Enter; `actions`: `[(text, callback), ...]`, the menu of the right click; a key left out is empty). It is called for every chunk each time the tree is filled; the callbacks run with the chunk of the item the active one; a node keeps the expansion the user gave it; a provider that fails is written in the log and left out; the items go away with the plugin |
| `refresh_tree()` | since 1.3.4: the workspace filled again, the providers asked again (after the plugin made or deleted products) |

`progress(fraction, message)` reports 0-1 and raises `Cancelled` when the user cancels: call it
often in long loops. Widgets are touched only in the callbacks of actions and in `done`, never in
the function given to `run` (it runs in another thread). `deactivate()` releases what `activate`
took beyond its registrations (open files, timers).

### Compatibility

`Plugin.api` is the plugin API the plugin was written for (`pyarchinit_photogrammetry.plugins.API_VERSION`,
now 1): a program with an older API refuses the plugin with a message. The index also gives the
oldest version of the program a plugin works with (`min_app`): a plugin that uses the methods marked
"since 1.3.2" asks for `min_app` 1.3.2, one that uses `on_refresh` 1.3.3, one that uses those "since 1.3.4"
1.3.4.

## Publishing a plugin

1. Build a pure-Python wheel: `python -m build --wheel` (`dist/ppg_chunk_summary-0.1.0-py3-none-any.whl`).
2. Attach it to a release of your repository (or publish it on PyPI).
3. Open a pull request on the [plugin repository](https://github.com/enzococca/pyarchinit-photogrammetry-plugins)
   adding your entry to `index.json`:

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

   `name` is the name of the entry point, `requires` lists the modules the plugin imports beyond
   those of the program, `i18n` gives title and description in other languages (shown when the
   interface speaks them). `python scripts/make_entry.py WHEEL_URL --write` in the plugin repository
   makes the entry from the wheel. The repository checks the entry when the pull request is opened:
   the hash, the entry point, and that the plugin starts with the program. Only the maintainer
   merges: that is the approval. A plugin can also live in the plugin repository itself, under
   `plugins/` (see its README); [`template/`](https://github.com/enzococca/pyarchinit-photogrammetry-plugins/tree/main/template)
   there is the plugin `ppg plugins new` starts from.
