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

* **Windows on ARM** (Snapdragon laptops, Windows 11 in Parallels on a Mac with Apple Silicon):
  a native application, `...-windows-arm64.zip`, built by the `build-arm64` job of the `desktop`
  workflow with the win_arm64 wheels of the `windows-arm` workflow (lazrs, OpenCV, pycolmap, Open3D
  and, from 1.3.5, VTK 9.7.1 for the 3D view and pye57 for E57 scans, as in the x64 application).
  VTK draws with OpenGL 3.2 or later: Snapdragon computers have it from their graphics driver or
  from Microsoft's *OpenCL, OpenGL, and Vulkan Compatibility Pack* (Microsoft Store); where Windows
  offers only its own OpenGL 1.1 (virtual machines without a 3D driver) the program finds it out
  before opening the view and uses the software renderer (*Help > Environment diagnosis...* shows the
  OpenGL found). The x64 application runs under the emulation of Windows 11 too - checked on
  Windows 11 ARM64 with `--check` and `--doctor` and with the window.

Logs are written in `~/Library/Logs/pyarchinit-photogrammetry` (macOS) or
`%LOCALAPPDATA%\pyarchinit-photogrammetry\logs` (Windows); *Help > Open the log folder*
opens the folder, *Help > Environment diagnosis...* runs the same checks as `ppg doctor`.

The application runs Python scripts too, with its own Python and modules and without the
window, as Metashape does with `-r` (the exit code is the script's):

```bash
"/Applications/PyArchInit Photogrammetry.app/Contents/MacOS/PyArchInit Photogrammetry" --run script.py args
"PyArchInit Photogrammetry.exe" --run script.py args > out.txt 2>&1    # Windows: no console, redirect
```

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

From 0.2.0 the core also holds PatchMatch multi-view stereo (`patchmatch_depth`, the depth-map
backend `patchmatch`, which the automatic choice takes for surveys from the air: photos whose GPS
positions spread over the site). It has no numpy version (it would be far too slow): without the core
`auto` falls back to two-view SGBM, the backend is not offered in the window and the step stops with a
message when asked for it by name.

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


## New versions of pycolmap and Open3D

The program takes pycolmap and Open3D up to the versions it was tested with (`pycolmap>=4.2.1,<4.3` and
`open3d>=0.18,!=0.20.0,<0.21` in `pyproject.toml`): a release of theirs reaches pip users only after it has been
tried. The packaged applications carry the versions they were built with and never change on their own.

The `upstream` workflow tries the newest ones every Monday, and on demand with a version to try. It installs the
newest version of each on PyPI beyond the installed one - pre-releases included, the versions excluded with `!=`
left out - compares their public API with the installed one, and runs the tests with them, the failed ones a second
time. The summary of the run lists what they remove that the program uses (with files and lines), what they add (a
GPU back-end or device among it), the tests that failed, and what to do:

* the tests pass and the version is beyond the ceiling: raise the ceiling in `pyproject.toml`, then release;
* they fail and the version is beyond the ceiling: pip users are safe; adapt the program before raising it;
* they fail and the version is within the range (a patch release): pip users get it already; exclude it
  (`!=X.Y.Z`) and release.

A new version of either also means new ARM wheels (the next section) and a new build of the applications.

What the two libraries can run on a GPU is asked of them, not read from their versions: `ppg doctor` (*Help >
Environment diagnosis...*) has the lines "COLMAP GPU" (the `has_<name>` flags of pycolmap: CUDA with the CUDA wheels
of Linux, none with those of PyPI for macOS and Windows) and "Open3D GPU" (the devices of `open3d.core` that are
available), and warns of a version of either that this install was not tested with. A back-end that a later build
adds - Metal, say - shows there the day it is installed; using it is a choice measured against the program's own GPU
kernels (wgpu: Metal, Vulkan, DirectX 12), which stay for the systems the library does not cover.


## Linux on ARM (aarch64): pycolmap and Open3D

pycolmap 4.2.1 and Open3D 0.19.0 publish no wheels for Linux on ARM (Open3D 0.20.0 does, but its
Poisson reconstruction crashes and the requirements exclude it). `packaging/arm/build-linux.sh`
builds them in Docker on an arm64 machine - a Mac with Apple Silicon, an arm64 Linux host - and
the `arm-wheels` workflow does the same on GitHub's arm64 runners, then runs the tests of this
project with them (on demand; the wheels stay as an artifact, or are attached to a release whose
tag is given).

```bash
packaging/arm/build-linux.sh                       # both, Python 3.10-3.12, wheels in dist/arm
PYTHONS=3.12 JOBS=3 packaging/arm/build-linux.sh pycolmap
packaging/arm/build-linux.sh test                  # the tests with them (Ubuntu 24.04, Python 3.12)
pip install dist/arm/pycolmap-*-cp312-*.whl dist/arm/open3d-*-cp312-*.whl
pip install "pyarchinit-photogrammetry[all]"
```

* pycolmap: COLMAP's own recipe for its x86-64 wheels (the manylinux_2_28 image, vcpkg, GCC 12)
  with an arm64 triplet; `manylinux_2_28_aarch64` wheels (glibc 2.28 or later).
* Open3D: Open3D's own arm64 recipe (Ubuntu 20.04, OpenBLAS, VTK and Embree built from source)
  without its new GUI (Filament), which this program does not use; `manylinux_2_31_aarch64`
  wheels (glibc 2.31 or later), package name `open3d`.
* The work goes to folders of the host, not to Docker's own disk (which only keeps the two
  builder images); on a Mac to a case-sensitive disk image, `build/arm.sparseimage`, because
  Linux packages hold names that differ only in case (`hdiutil detach build/arm` closes it).
* A few files of COLMAP and Open3D need several GB each to compile: `JOBS` compilers at a time
  (3 by default, for a Docker with 8 GB), the ones that run out of memory compiled again alone.
* Open3D on ARM links the shared `libgfortran` (put into the wheel by auditwheel): with the static
  one, as Open3D's own ARM build does, the module cannot be imported ("cannot allocate memory in
  static TLS block").
* `pye57` (E57 scans, in `[all]`) has no wheel for aarch64 either: it is compiled when installed and
  needs the Xerces-C headers (`sudo apt install libxerces-c-dev` on Debian and Ubuntu).


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
