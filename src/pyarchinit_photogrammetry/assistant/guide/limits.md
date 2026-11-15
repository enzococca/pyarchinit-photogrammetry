# Limits by system

The program is the same on every system, but not every part of it is available everywhere: some libraries
have no build for a system, some tools of other makers exist only for some, and not every system is tested
in the same way. This page lists what you meet on each one, as of version 1.10.1, and the way around it when
there is one. `ppg doctor` (in the window: *Help > Environment diagnosis...*) says what this computer has.

## Summary

| | macOS, Apple Silicon | macOS, Intel | Windows x64 | Windows on ARM64 | Linux x86-64 | Linux aarch64 |
|---|---|---|---|---|---|---|
| Desktop application | yes (dmg, signed and notarized) | no: pip | yes (zip) | yes (zip, native from 1.1.0) | no: pip | no: pip |
| pip installation | yes | yes, not tested | yes | not with the PyPI wheels alone | yes | with the project's wheels of pycolmap and Open3D |
| Updates from the window | downloaded, installed, restarted | pip command | zip downloaded, replaced by hand | zip downloaded, replaced by hand | pip command | pip command |
| Native core on PyPI (speed, PatchMatch, PPK) | yes | yes, not tested | yes | in the application; pip: built from source | yes | yes, not tested |
| GPU of the program (wgpu) | Metal | Metal, not tested | DirectX 12, Vulkan | DirectX 12, Vulkan | Vulkan | Vulkan |
| COLMAP depth maps on CUDA | no | no | no | no | only with a CUDA build of pycolmap | no |
| SAM 2.1 runs on | Apple GPU (MPS) | not checked | NVIDIA GPU (CUDA), else CPU | CPU | NVIDIA GPU (CUDA), else CPU | not checked |
| SpatiaLite (geometries in a PyArchInit SQLite database) | QGIS, Homebrew, MacPorts | QGIS, Homebrew, MacPorts | QGIS, OSGeo4W | **no** (PostGIS works) | apt | apt |
| DJI R-JPEG thermal (DJI Thermal SDK) | in Docker Desktop | in Docker Desktop | native | not checked | native | not checked |
| VTK 3D view | yes | yes | yes | yes from 1.3.5, needs OpenGL 3.2 | yes | yes |
| E57 scans | yes | yes | yes | yes from 1.3.5 | yes | compiled when installed |
| Camera raw files (rawpy) | yes | with pip | yes | **not in the application**; pip with Python 3.11+ | yes | with pip |
| Tests in the project's CI | the application is built and started | none | whole test suite, application built and started | application built and started, test suite on demand | whole test suite | test suite on demand |

## On every system

* **Python 3.10 to 3.12** for a pip installation, in a virtual environment of its own (not Anaconda's `base`, whose
  compiled libraries - SciPy, VTK - may not load or may crash: `ppg doctor` warns of it). The package does not
  refuse a newer Python, but none is tested: the automatic tests run Python 3.11 and 3.12 on Linux and 3.12 on
  Windows; 3.10 is not run there.
* **Open3D 0.20.0 is excluded**: its Poisson reconstruction crashes on some systems. pip takes Open3D 0.18 or 0.19,
  and pycolmap 4.2; newer versions reach pip users only after they have been tried
  ([Packaging](packaging.md#new-versions-of-pycolmap-and-open3d)).
* **Without the native core** (a pip installation without `pyarchinit-photogrammetry-core`) the depth maps and
  orthophotos are several times slower, there is no PatchMatch depth backend and no GPU, the PPK of the GNSS
  positions needs the `rnx2rtkp` program of RTKLIB named by `PPG_RNX2RTKP`, and the Hatanaka files of the permanent
  stations need the `CRX2RNX` program. The desktop applications include the core;
  `pip install "pyarchinit-photogrammetry[fast]"` adds it where PyPI has a wheel (see the table).
* **COLMAP's own depth maps need CUDA** (NVIDIA): the pycolmap wheels of PyPI for macOS and Windows have none, nor
  have those the project builds for ARM. The program's own depth maps (SGBM, PatchMatch on the CPU and on the GPU
  through wgpu) run everywhere; without a Metal, Vulkan or DirectX 12 device the matching runs on the CPU.
* **Segment Anything and the learned key points** are not inside the applications: the first use installs PyTorch
  in a separate environment (1-2 GB, downloaded).
* **DEM coregistration with xdem** is never in the applications: only `pip install "pyarchinit-photogrammetry[coreg]"`.
* **QGIS** is found in its usual places: `/Applications/QGIS*.app` on macOS, `C:\Program Files\QGIS*` or `C:\OSGeo4W`
  on Windows, `/usr/bin/qgis` or `/usr/local/bin/qgis` on Linux. Elsewhere set `PPG_QGIS`.
* **The assistant's keys** are kept in the keychain of the system; without one (a Linux server with no desktop
  session) set `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` before starting the program
  ([Errors and updates](gui/help.md)).
* **Tools > Open a terminal** (since 1.10.1) opens the terminal of the system, not one inside the program: Terminal.app
  on macOS (always Terminal, even when another is the default), Windows Terminal or else a new `cmd` console on
  Windows, on Linux `$TERMINAL` or the first of `x-terminal-emulator`, `gnome-terminal`, `konsole`,
  `xfce4-terminal`, `mate-terminal`, `tilix`, `kitty`, `alacritty`, `wezterm`, `lxterminal`, `xterm` (none found:
  a message says so). From a pip installation `ppg` comes first on the PATH. **The applications have no `ppg`
  command**: their terminal offers `ppg-run script.py` (the script run by the application, as `--run`) and
  `ppg-doctor`, and `PPG_APP` names the executable; on Windows their output appears when they end. The shell's own
  start-up files (`.zshrc`, `.bashrc`, Anaconda's initialisation) may still put other folders, and another `ppg`,
  before them: `which ppg` tells.

## macOS on Apple Silicon

* **The application needs macOS 15 or later** (the rasterio libraries it carries are built for it).
* **The dmg is signed with a Developer ID and notarized by Apple** (from 1.1.0): it opens without warnings.
  A dmg built by the project's GitHub workflow is not signed; the one attached to each release is.
* **Updates install themselves**: the new version is downloaded, copied into place and the program restarts.
* **DJI R-JPEG thermal photos**: DJI publishes its Thermal SDK for Windows and Linux only. On the Mac the program
  runs its Linux x86-64 build in **Docker Desktop**, which must be installed and running (the image is prepared
  the first time). FLIR JPEGs and thermal TIFFs need nothing.
* **COLMAP's PatchMatch** (CUDA) does not exist on the Mac: the PatchMatch of the native core runs on the CPU and
  on the Apple GPU instead.
* **SpatiaLite** comes from QGIS, Homebrew (`brew install libspatialite`) or MacPorts; otherwise set
  `SPATIALITE_LIBRARY_PATH`.
* **Not covered by the automatic tests**: on macOS the project's CI builds the application and starts it (window
  and diagnosis), but the test suite runs on Linux and Windows only.

## macOS on Intel

* **No application**: the dmg is for Apple Silicon only, and the program's update check finds no installer for
  this system. Install with pip (Python 3.10-3.12).
* The native core has a wheel for Intel Macs on PyPI, built but **not tested**; nothing else is tested on Intel
  Macs either.
* SpatiaLite from Homebrew (`/usr/local/lib`), MacPorts or QGIS; DJI thermal photos in Docker Desktop, as on Apple
  Silicon.

## Windows x64

* **The zip is not signed**: SmartScreen may ask the first time (*More info > Run anyway*).
* **Updates are not installed by the program**: it downloads the new zip into the Downloads folder; close the
  program, extract it and replace the program folder.
* **SpatiaLite** is found in a QGIS or OSGeo4W installation (`C:\Program Files\QGIS*`, `C:\OSGeo4W`), or set
  `SPATIALITE_LIBRARY_PATH` to `mod_spatialite.dll`. Without it only writing geometries into a PyArchInit SQLite
  database is missing; PostGIS is not affected.
* **The PPK** runs RTKLIB, which opens files with the ANSI functions of Windows: a project in a folder whose path
  has letters outside plain ASCII (accents, other alphabets) works only when Windows gives that folder a short
  (8.3) name; otherwise the PPK stops and asks to move the project to a plain ASCII path.
* **Scripts with `--run`**: the application has no console, so redirect the output
  (`"PyArchInit Photogrammetry.exe" --run script.py > out.txt 2>&1`).
* COLMAP's PatchMatch needs CUDA, which the PyPI pycolmap for Windows does not have; SAM 2.1 installs the CUDA
  build of PyTorch when there is an NVIDIA driver, otherwise the CPU one.

## Windows on ARM64

* **The native ARM64 application** (from 1.1.0; Snapdragon computers, ARM64 virtual machines) is built with
  libraries the project compiles itself (lazrs, OpenCV, pycolmap, Open3D, VTK, pye57). The x64 application also
  runs, under the emulation of Windows 11.
* **No SpatiaLite**: its only Windows build comes with QGIS, which is x64, and an ARM64 program cannot load it.
  Writing geometries into a PyArchInit **SQLite** database is not available; **PostGIS** works (through
  psycopg 3).
* **No camera raw files in the application**: it is built without rawpy, so DNG, CR2, NEF, ARW... cannot be
  added; export the photos to JPEG or TIFF. With pip, rawpy needs Python 3.11 or later here.
* **The VTK 3D view needs OpenGL 3.2** (from 1.3.5): Snapdragon computers have it from their graphics driver or
  from Microsoft's *OpenCL, OpenGL, and Vulkan Compatibility Pack* (Microsoft Store). Where Windows offers only
  OpenGL 1.1 (virtual machines without a 3D driver) the program uses the software renderer, and the diagnosis
  says which OpenGL it found.
* **SAM 2.1 runs on the CPU**: PyTorch for Windows on ARM has only CPU builds.
* **pip**: pycolmap, Open3D, OpenCV and lazrs publish no Windows ARM64 wheels on PyPI; the project builds them
  only for the application. Use the ARM64 application, or the x64 one.
* OpenCV in the application holds only the modules the program uses: a plugin that needs another OpenCV module
  may not work there.
* Unsigned zip, updates replaced by hand, PPK paths in plain ASCII: as on Windows x64.

## Linux x86-64

* **No application**: install with pip (Python 3.10-3.12). The update check gives the pip command.
* **On a server without a display** Open3D needs the EGL/GL libraries:
  `sudo apt-get install libegl1 libgl1 libgomp1`.
* **SpatiaLite**: `sudo apt-get install libsqlite3-mod-spatialite`.
* **COLMAP's PatchMatch on CUDA** only with a CUDA build of pycolmap and an NVIDIA GPU (`ppg doctor` shows it on
  the line "COLMAP GPU"). The program's own GPU kernels use Vulkan.
* The assistant's keys need the Secret Service of the desktop (GNOME Keyring, KWallet); on a server use the
  environment variables.

## Linux aarch64

* **No application**: pip only.
* **pycolmap 4.2.1 and Open3D 0.19.0 publish no wheels for Linux on ARM**: install those the project builds (they
  are attached to its releases, and `packaging/arm/build-linux.sh` builds them) before the package. They need
  glibc 2.28 (pycolmap) and 2.31 (Open3D) or later, and have no CUDA.
* **E57 scans**: pye57 has no ARM wheel and is compiled when installed; it needs the Xerces-C headers
  (`sudo apt-get install libxerces-c-dev`).
* The native core has a wheel for aarch64 on PyPI, built but **not tested** in CI; the test suite runs with the
  project's ARM wheels only on demand.

## Plugins

Some plugins rely on libraries or programs of other makers that do not exist on every system. Their guides say
it in full; in short:

| Plugin | What is missing, and where |
|---|---|
| [Architectural elements](plugins/architecture.md) | the solids (OpenCascade): Windows on ARM |
| [Mesh (MeshLab)](plugins/mesh-tools.md) | the MeshLab tools (pymeshlab): Windows on ARM; Linux needs system libraries |
| [Landscape through time](plugins/landscape.md) | the simulations (Landlab): Windows on ARM; Intel Macs need macOS 15 |
| [Geophysics](plugins/geophysics.md) | the resistivity tomography (pyGIMLi): Windows and Linux on ARM; macOS needs 26 or later |
| [Terrain](plugins/terrain.md) | WhiteboxTools: no build for Linux on ARM (install it and set `PPG_WBT`); Windows on ARM uses the x64 build |
| [Spirula](plugins/spirula.md) | Spirula Studio: no build for Intel Macs nor for ARM computers with Windows or Linux |
| [Flight plans](plugins/flightplan.md) | the copy into DJI Fly through MTP on Windows is not yet tried on a real controller; on a Mac with the RC 2, drag the exported folders |
