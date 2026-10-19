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
is not, and is installed on first use in a separate environment (see
[integrations.md](integrations.md#segment-anything-21-in-the-packaged-application)).

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
pip install ./native                 # native kernels (Rust toolchain needed), bundled when installed
python packaging/build.py            # icons, PyInstaller, smoke test (--check), dmg/zip in dist/
python packaging/build.py --no-smoke
```

* `packaging/ppg_gui.spec`: PyInstaller spec (one-folder build, windowed, the hooks for
  rasterio, pyproj, Open3D, VTK, qtawesome and pycolmap data, Qt modules that are not used
  are excluded).
* `packaging/launcher.py`: entry point; calls `multiprocessing.freeze_support()` first so the
  isolated worker processes (Poisson reconstruction) work in the frozen application.
* `packaging/build.py`: renders the icons from `gui/resources/logo.svg`, runs PyInstaller,
  rewrites the references of each package's private libraries back to `@loader_path`
  (macOS: rasterio and pyproj ship different builds of `libproj` under the same name, and
  PyInstaller would otherwise make them share one copy), starts the result with `--check`
  (offscreen) and `--doctor`, then writes the `.dmg` (`hdiutil`) or the `.zip`.

The smoke test opens the main window, renders it and exits with code 0; the build fails
when the window does not appear. Its line also says whether the native core is inside
(`native: 0.1.0`).

## Native core (Rust)

`native/` holds `pyarchinit-photogrammetry-core`, imported as `ppg_core`: the kernels that the
Python code ran in numpy and that dominated the CPU time of the depth maps and of the
orthomosaic - fusion of the depth estimates of a photo, disparity back to depth, normals,
projection of points with the COLMAP camera models, scoring of the photos of an orthomosaic
tile, agreement of points with a depth map (confidence). PyO3 + rayon, one abi3 wheel per
platform for Python 3.10 and later. It is optional: without it (or with `PPG_NATIVE=0`) the
numpy code runs, and `tests/test_native.py` checks that both give the same maps, pixels and
counts. Measured on the synthetic trench, CPU depth maps of 48 photos: 5.7 s with numpy one
photo at a time, 1.6 s with the native kernels, 0.9 s with six photos at a time.

```bash
pip install ./native                         # build and install (maturin is fetched by pip)
maturin build --release -m native/Cargo.toml # a wheel in native/target/wheels
PPG_NATIVE=0 ppg ...                         # the numpy code, for comparison
```

The release profile keeps the symbols: the library stripped by Rust's default release profile
is refused by the dynamic loader of macOS 26/27 ("mis-aligned LINKEDIT string pool"). The
`native` workflow builds the wheels (macOS arm64 and x86-64, Windows x64, Linux x86-64 and
aarch64) and, on a `core-v*` tag, publishes them to PyPI; the `tests` workflow runs Python 3.12
with the native core and 3.11 without it; the `desktop` workflow bundles it in the applications.


## Signing, notarization, TestFlight

The builds are unsigned. Two Apple routes exist:

* **Developer ID + notarization** (download from the web/GitHub, no warnings): needs a
  *Developer ID Application* certificate (created by the Account Holder of the Apple
  Developer team) and a notary profile (`xcrun notarytool store-credentials`). Then
  `PPG_CODESIGN_IDENTITY="Developer ID Application: ... (TEAMID)" PPG_NOTARY_PROFILE=<profile> python packaging/build.py`
  signs every binary with the hardened runtime (`packaging/entitlements.plist`), signs and
  notarizes the dmg, and staples the ticket.
* **TestFlight / Mac App Store**: needs an *Apple Distribution* certificate, a provisioning
  profile and the **App Sandbox**. The sandbox forbids what this application relies on:
  loading `mod_spatialite` from Homebrew/QGIS, starting worker processes from other
  interpreters (isolated Poisson, SAM environment), downloading and running `uv`, writing
  next to the user's projects without a file dialog, opening QGIS. A sandboxed edition would
  need those parts redesigned, so Developer ID is the recommended channel.
