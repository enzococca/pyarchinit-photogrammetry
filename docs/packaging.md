# Packaged desktop application

Ready-to-run builds of the interface, with Python and every library inside, are produced
by PyInstaller:

| Platform | File | Run |
| --- | --- | --- |
| macOS 15 or newer (Apple Silicon) | `PyArchInit-Photogrammetry-<version>-macos-arm64.dmg` | open the disk image, drag the app to *Applications* |
| Windows 10/11 (64 bit) | `PyArchInit-Photogrammetry-<version>-windows-x64.zip` | unzip, run `PyArchInit Photogrammetry.exe` |

They are attached to every [GitHub Release](https://github.com/enzococca/pyarchinit-photogrammetry/releases)
and produced by the `desktop` workflow (`.github/workflows/desktop.yml`, runners `macos-15`
and `windows-latest`). The packaged application contains the same code as the `pip`
package: Open3D, VTK, qtawesome and IfcOpenShell are included; Segment Anything (PyTorch)
is not, so the SAM tools are disabled there.

## First start

* **macOS**: the application is not signed with an Apple developer certificate. Gatekeeper
  shows *"cannot be opened because the developer cannot be verified"* (or *"is damaged"* on
  the newest systems). Right-click the app and choose *Open* once, or remove the quarantine
  attribute from the terminal:

  ```bash
  xattr -dr com.apple.quarantine "/Applications/PyArchInit Photogrammetry.app"
  ```

* **Windows**: SmartScreen may ask for confirmation the first time (*More info > Run anyway*).
  Writing geometries into a PyArchInit SQLite database needs `mod_spatialite.dll`: it is found
  in a QGIS or OSGeo4W installation, or set `SPATIALITE_LIBRARY_PATH`.

Logs are written in `~/Library/Logs/pyarchinit-photogrammetry` (macOS) or
`%LOCALAPPDATA%\pyarchinit-photogrammetry\logs` (Windows); *Aiuto > Apri cartella dei log*
opens the folder, *Aiuto > Diagnostica ambiente* runs the same checks as `ppg doctor`.

## Building locally

```bash
pip install -e ".[all,build]"
python packaging/build.py            # icons, PyInstaller, smoke test (--check), dmg/zip in dist/
python packaging/build.py --no-smoke
```

* `packaging/ppg_gui.spec`: PyInstaller spec (one-folder build, windowed, the hooks for
  rasterio, pyproj, Open3D, VTK, qtawesome and pycolmap data, Qt modules that are not used
  are excluded).
* `packaging/launcher.py`: entry point; calls `multiprocessing.freeze_support()` first so the
  isolated worker processes (Poisson reconstruction) work in the frozen application.
* `packaging/build.py`: renders the icons from `gui/resources/logo.svg`, runs PyInstaller,
  starts the result with `--check` (offscreen) and `--doctor`, then writes the `.dmg`
  (`hdiutil`) or the `.zip`.

The smoke test opens the main window, renders it and exits with code 0; the build fails
when the window does not appear.
