# Plugins

A plugin is a Python package that adds to pyarchinit-photogrammetry without changing it: menu
actions in the window (menu *Plugin*), commands of the command line (`ppg plugin NAME`), export
formats (*File > Esporta*), long work in the background with a progress bar, settings of its own.
Plugins are collected in the repository
[pyarchinit-photogrammetry-plugins](https://github.com/enzococca/pyarchinit-photogrammetry-plugins),
whose index the program reads.

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

`progress(fraction, message)` reports 0-1 and raises `Cancelled` when the user cancels: call it
often in long loops. Widgets are touched only in the callbacks of actions and in `done`, never in
the function given to `run` (it runs in another thread). `deactivate()` releases what `activate`
took beyond its registrations (open files, timers).

### Compatibility

`Plugin.api` is the plugin API the plugin was written for (`pyarchinit_photogrammetry.plugins.API_VERSION`,
now 1): a program with an older API refuses the plugin with a message. The index also gives the
oldest version of the program a plugin works with (`min_app`).

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
   the hash, the entry point, and that the plugin starts with the program. A plugin can also live in
   the plugin repository itself, under `plugins/` (see its README); [`template/`](https://github.com/enzococca/pyarchinit-photogrammetry-plugins/tree/main/template)
   there is a plugin to start from.
