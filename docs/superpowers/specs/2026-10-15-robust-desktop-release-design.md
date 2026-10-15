# Robust desktop release: crash fixes, redesigned interface, executables

Date: 2026-10-15. Status: implemented on branch `gui-redesign` (0.3.0).

## What was asked

The desktop interface must install and start without crashing, processing must not
crash, starting it must be simple, the documentation must be updated, Mac and Windows
executables must exist, and the interface must be redesigned with coloured icons, a
pleasant look and a good error log.

Written back as goals:

1. **No crash on start, no crash during processing.** Two crashes were reproduced on
   the author's Mac (macOS 27, Apple Silicon, Qt 6.12, Open3D 0.20.0):
   * every native `QMessageBox` (an `NSAlert`) aborts the process with
     `NSInternalInconsistencyException: targetSizeInPoints.width>0`;
   * Open3D 0.20.0 aborts (`libc++abi: terminating` or a segfault) inside Poisson
     reconstruction on the demo cloud; 0.19.0 exits silently with code 0 on the same
     cloud, and pycolmap's bundled PoissonRecon crashes too. The abort happens in C++,
     outside Python's control.
2. **Simple start.** One command (or one double-click) opens the interface, with a clear
   diagnosis when the environment is wrong (for example a stale install in Anaconda
   that shadows the right one).
3. **Documentation** matching the new behaviour, in English and Italian.
4. **Executables** for macOS (`.app` in a `.dmg`/`.zip`) and Windows (folder with
   `.exe` in a `.zip`), built by GitHub Actions and attached to releases.
5. **Interface redesign**: coloured icons on toolbar, menus and trees; a coherent light
   theme (and a dark one); a start page; an error log that is readable and easy to send.

Assumptions made because the author was not available during the work:

* Italian stays the interface language (project rule). Code and docs in English.
* Icons come from `qtawesome` (Material Design Icons / Font Awesome), coloured per
  workflow stage. No hand-drawn icon set, except the application logo (SVG).
* Message boxes become non-native everywhere (they are the crashing ones); file and
  input dialogs stay native (verified not to crash).
* Version bumped to 0.3.0. No release or PyPI publication is made by this work.
* Windows builds cannot be run locally; they are produced by the CI workflow.

## Design

### Crash isolation in the engine (`engine/meshing.py`, `engine/isolate.py`)

* `isolate.run_isolated(fn, *args)` runs a module-level function in a fresh interpreter
  started with `subprocess` (`posix_spawn`, no `fork`): request and result travel as
  pickle files, the child's output goes to a log file. A child that aborts, segfaults or
  exits without a result raises `IsolatedCrash` with the exit code and the log tail. The
  packaged application re-enters itself with `--ppg-worker`.
* `_poisson` calls Open3D through `run_isolated`. On `IsolatedCrash` or any
  exception it logs a warning and falls back, in order, to Open3D with
  `depth-1`, then to the 2.5D height field. The pipeline therefore always produces a
  mesh; the log says which path was taken.
* `pyproject.toml` excludes the broken wheel: `open3d>=0.18,!=0.20.0`.
* `ppg demo --process` and the test pipeline exercise the path.

### Non-native dialogs and logging (`gui/dialogs.py`, `gui/logging_setup.py`)

* `dialogs.message(parent, kind, title, text, details="", buttons=...)` builds every
  message box with `QMessageBox.Option.DontUseNativeDialog`. `MainWindow._warn`,
  `_error`, `_confirm_discard`, `about` and all `QMessageBox.information` calls use it.
* `ErrorDialog`: summary line, optional "suggestion" (looked up from known error
  signatures: Open3D crash, SpatiaLite missing, PyTorch missing, stale Anaconda
  install), expandable traceback, buttons *Copia*, *Apri cartella log*, *Chiudi*.
* `logging_setup.install(app_name)`: rotating file log in the platform log directory
  (`QStandardPaths.AppLocalDataLocation/logs`), `faulthandler` to a crash file in the
  same folder, `sys.excepthook` and Qt message handler routed to the log and to the
  error dialog. The GUI entry point calls it; `ppg doctor` prints the paths.

### Interface (`gui/theme.py`, `gui/icons.py`, `gui/start_page.py`, `gui/panels.py`)

* `icons.icon(name, color=None)` returns a `QIcon` from qtawesome with the palette
  colour of the stage (photos blue, markers violet, align teal, reference indigo,
  dense green, model orange, texture pink, DEM brown, ortho amber, run red, PyArchInit
  olive, tools grey). Without qtawesome it returns an empty icon, so nothing breaks.
* `theme.apply(app, mode)` sets a Fusion-based palette and a QSS for light and dark
  modes (menu *Vista > Tema*), stored in `QSettings`.
* Toolbar: icon + text under, 28 px icons, grouped with separators; menus and the
  workspace tree get the same icons (chunk, aligned/unaligned camera, marker, GCP,
  product types).
* Start page replaces the central tabs when no project is open: *Nuovo*, *Apri*,
  *Demo*, recent projects, and the log/doctor shortcuts.
* Log pane: table with time, level, message; level filter; search; *Copia*, *Salva*,
  *Apri cartella*; errors in red, warnings in amber; task failures land here with the
  traceback in the row tooltip.
* Progress: the modal dialog is kept but restyled; the status bar shows a permanent
  progress bar with the task label and an *Annulla* button.

### Diagnosis (`cli.py: ppg doctor`, *Aiuto > Diagnostica*)

Prints Python, package versions, the executable in use, whether another
`ppg-gui` shadows it on `PATH`, Open3D/pyvistaqt version checks, SciPy import,
SpatiaLite availability, VTK availability, log folder. Exit code 1 when something
would prevent the GUI from running.

### Executables (`packaging/`, `.github/workflows/desktop.yml`)

* `packaging/ppg_gui.spec` (PyInstaller, one-folder build, windowed) with the hooks
  needed for rasterio, pyproj, Open3D, VTK/pyvista, qtawesome, pycolmap data.
* `packaging/build.py` builds, then produces `PyArchInit-Photogrammetry-<ver>-macos-arm64.dmg`
  (or `.zip`) and `PyArchInit-Photogrammetry-<ver>-windows-x64.zip`.
* Workflow `desktop.yml`: `macos-14` and `windows-latest`, on `workflow_dispatch`,
  tags `v*` and published releases; uploads artifacts and attaches them to the release.
* The frozen app runs the same entry point (`pyarchinit_photogrammetry.gui:main`) and
  the smoke test starts it with `--check` (opens, renders, exits 0).

### Documentation

README.md / README.it.md: installation with `uv` or `venv`, "avvio" section, the
executables, troubleshooting (Anaconda shadowing, SciPy, Open3D 0.20, native dialogs),
`ppg doctor`. `docs/gui.md` describes the new layout; `docs/packaging.md` the build.

### Tests

* `tests/test_isolate.py`: a crashing child raises `IsolatedCrash`; a normal child
  returns its result.
* `tests/test_meshing_fallback.py`: Poisson failure falls back to the height field.
* `tests/test_gui.py`: icons resolve, theme applies, error dialog is non-native, log
  pane records levels and filters, start page shows when no project, doctor output.

## Findings during implementation

* PoissonRecon fails non-deterministically on the demo cloud with Open3D 0.19 too (silent
  `exit(0)` or segmentation fault, "Failed to close loop") and COLMAP's PoissonRecon throws
  the same error deterministically. Smooth PCA normals oriented towards the viewing camera,
  `linear_fit=False`, two attempts per depth and the shallower depth make Open3D succeed in
  practice; the height field remains the last resort. Open3D 0.20.0 also aborts at depth 9
  with that recipe and stays excluded.
* Open3D must be imported before rasterio/SciPy (`_env.load_open3d_first`), otherwise its
  OpenMP runtime binds after theirs and PoissonRecon crashes even more often.
* With `PROJ_DATA`/`PROJ_LIB` exported by Anaconda, every EPSG lookup in a *worker thread*
  failed although the main thread worked (PROJ creates a context per thread from the
  environment). `_env.use_bundled_proj_data` now rewrites the variables; regression test
  `tests/test_env.py`.
* Native `QFileDialog` and `QInputDialog` do not crash on macOS 27; only `QMessageBox`
  (NSAlert) does, so only message boxes are forced to Qt's implementation.
* The macOS bundle built locally passes the `--check` smoke test and opens on screen; the
  DMG is about 0.5 GB (Open3D, VTK, Qt).
* `multiprocessing` (even `spawn`) forks the parent before `exec`; after rasterio/PROJ,
  pycolmap and SpatiaLite are loaded in the same process (as in the GUI after connecting
  PyArchInit, or in the test suite after `ppg doctor`), that fork crashes the *parent*
  with SIGSEGV/SIGBUS, deterministically. `subprocess` with `close_fds=False` uses
  `posix_spawn` and is unaffected: the isolation now uses it (`tests/test_isolate.py`).
