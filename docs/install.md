# Installation

## Desktop application

No Python needed: the button downloads the latest build for your system (macOS: the dmg; Windows: the
zip for 64 bit, or for ARM when the browser reports an ARM machine); the arrow lists every build.

<div class="ppg-download" data-ppg-download>
<a class="md-button md-button--primary ppg-download__main" href="https://github.com/enzococca/pyarchinit-photogrammetry/releases/latest" data-ppg-main data-ppg-prefix="Download for ">Download</a><button class="md-button md-button--primary ppg-download__toggle" type="button" aria-haspopup="true" aria-expanded="false" aria-label="Other systems" data-ppg-toggle>&#9662;</button>
<ul class="ppg-download__menu" data-ppg-menu hidden>
<li><a href="https://github.com/enzococca/pyarchinit-photogrammetry/releases/latest/download/PyArchInit-Photogrammetry-macos-arm64.dmg" download data-ppg-system="macos" data-ppg-asset="macos-arm64.dmg" data-ppg-label="macOS (Apple Silicon)">macOS 15 or newer, Apple Silicon (.dmg)</a></li>
<li><a href="https://github.com/enzococca/pyarchinit-photogrammetry/releases/latest/download/PyArchInit-Photogrammetry-windows-x64.zip" download data-ppg-system="windows-x64" data-ppg-asset="windows-x64.zip" data-ppg-label="Windows (64 bit)">Windows 10/11, 64 bit (.zip)</a></li>
<li><a href="https://github.com/enzococca/pyarchinit-photogrammetry/releases/latest/download/PyArchInit-Photogrammetry-windows-arm64.zip" download data-ppg-system="windows-arm64" data-ppg-fallback="windows-x64" data-ppg-asset="windows-arm64.zip" data-ppg-label="Windows on ARM">Windows 11 on ARM, Snapdragon (.zip)</a></li>
<li><a href="https://github.com/enzococca/pyarchinit-photogrammetry/releases">All the versions on GitHub</a></li>
</ul>
</div>

All the builds are on the [releases page](https://github.com/enzococca/pyarchinit-photogrammetry/releases/latest).
If the application is useful to you, [support the project on PayPal](https://paypal.me/enzococca): it is the work of one
person.

| System | File | |
|---|---|---|
| macOS 15 or newer, Apple Silicon | `PyArchInit-Photogrammetry-<version>-macos-arm64.dmg` | open it and drag the app to *Applications* |
| Windows 10/11, 64 bit | `PyArchInit-Photogrammetry-<version>-windows-x64.zip` | unzip, run `PyArchInit Photogrammetry.exe` |
| Windows 11 on ARM (Snapdragon, ARM64 virtual machines) | `PyArchInit-Photogrammetry-<version>-windows-arm64.zip` | native build (from 1.1.0; VTK 3D view and E57 scans from 1.3.5, the software renderer where Windows has no OpenGL 3.2); the x64 zip also runs, under emulation |

The builds are not signed. On macOS right-click the app > *Open* the first time (or
`xattr -dr com.apple.quarantine "/Applications/PyArchInit Photogrammetry.app"`); on Windows choose
*More info > Run anyway*. The application looks for new versions at start (at most once a day) and,
on macOS, installs them and restarts.

The window opens on a start page with *Nuovo progetto*, *Apri progetto* and *Progetto demo*
(the [tutorials](tutorials/index.md) start there). The interface is in Italian or English
(*Vista > Lingua / Language*). If PyArchInit 5 is installed (`~/pyarchinit_5`), the application
connects to its database at start.

## With pip

Python 3.10-3.12, in a virtual environment of its own (not Anaconda's `base`). With
[uv](https://docs.astral.sh/uv/):

```bash
uv venv --seed --python 3.12 ~/venvs/ppg && source ~/venvs/ppg/bin/activate
uv pip install "pyarchinit-photogrammetry[all]"
```

or with the standard tools:

```bash
python3 -m venv ~/venvs/ppg && source ~/venvs/ppg/bin/activate   # Windows: ~\venvs\ppg\Scripts\activate
pip install -U pip && pip install "pyarchinit-photogrammetry[all]"
```

`[all]` brings the mesh (Open3D), IFC, PostgreSQL, the desktop interface, E57 scans and Munsell
colours; `[sam]` adds Segment Anything 2.1 (PyTorch). The native core in Rust (depth maps and
orthophotos several times faster) is built from the source (below); the desktop applications
include it. The PPK of the GNSS positions needs the core 0.5 or later, which holds RTKLIB
(`pip install "pyarchinit-photogrammetry-core>=0.5"`); without it the program says so.

```bash
ppg doctor                       # checks the environment and says what to fix
ppg demo ~/ppg_demo --process    # the demo projects, processed
ppg-gui ~/ppg_demo/scavo.ppg     # the window on the demo trench
```

### Linux

On a server without a display Open3D needs the EGL/GL libraries
(`sudo apt-get install libegl1 libgl1 libgomp1`); writing into a PyArchInit SQLite database needs
SpatiaLite (`sudo apt-get install libsqlite3-mod-spatialite`).

On ARM (aarch64) pycolmap and Open3D publish no wheels: install those attached to the
[release](https://github.com/enzococca/pyarchinit-photogrammetry/releases/latest) for your Python first,
then the package:

```bash
pip install pycolmap-*-cp312-*aarch64.whl open3d-*-cp312-*aarch64.whl
sudo apt-get install libxerces-c-dev       # for the E57 support of [all]
pip install "pyarchinit-photogrammetry[all]"
```

### macOS and Windows

SpatiaLite is found in QGIS, Homebrew (`brew install libspatialite`) or MacPorts; otherwise set
`SPATIALITE_LIBRARY_PATH` to `mod_spatialite`. A global `ppg`/`ppg-gui` without activating anything:
`uv tool install "pyarchinit-photogrammetry[all]"`.

## From the source

```bash
git clone https://github.com/enzococca/pyarchinit-photogrammetry
cd pyarchinit-photogrammetry
uv venv --seed --python 3.12 .venv && source .venv/bin/activate
uv pip install -e ".[all,dev]"
uv pip install ./native        # optional: the native core (needs a Rust toolchain)
pytest -q
```

## When something does not work

* `ppg doctor` (in the window: *Aiuto > Diagnostica ambiente*) lists versions and known problems.
* The log of the window: *Aiuto > Apri cartella dei log*.
* `ppg-gui` does nothing: an older copy installed in another Python comes first on `PATH`
  (`which ppg-gui` must point inside the environment).
* More in [Packaging](packaging.md) and in the
  [README](https://github.com/enzococca/pyarchinit-photogrammetry#troubleshooting).
