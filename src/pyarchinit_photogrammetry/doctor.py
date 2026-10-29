"""Environment diagnosis: ``ppg doctor`` on the command line, *Aiuto > Diagnostica* in
the interface. Checks the interpreter, the libraries and the known pitfalls (a stale
install shadowing the right one, the Open3D build that crashes, SciPy wheels that do
not load, missing SpatiaLite) and says what to do."""
from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    name: str
    level: str
    detail: str
    hint: str = ""

    @property
    def mark(self) -> str:
        return {OK: "OK ", WARN: "!! ", FAIL: "XX "}[self.level]


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, level: str, detail: str, hint: str = "") -> None:
        self.checks.append(Check(name, level, detail, hint))

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.level == FAIL]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.level == WARN]

    def text(self) -> str:
        lines = []
        for c in self.checks:
            lines.append(f"[{c.mark.strip()}] {c.name:<22} {c.detail}")
            if c.hint:
                lines.append(f"      -> {c.hint}")
        n_f, n_w = len(self.failures), len(self.warnings)
        lines.append("")
        if n_f:
            lines.append(f"{n_f} problem(s) prevent the interface or the pipeline from working.")
        elif n_w:
            lines.append(f"No blocking problem; {n_w} warning(s).")
        else:
            lines.append("Everything looks fine.")
        return "\n".join(lines)


def _dist_version(dist: str) -> str | None:
    """The installed version of a distribution, read from its metadata: nothing is imported
    (PyTorch must never be imported in the program's process, see ``ai/sam_env.py``)."""
    from importlib import metadata

    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return None


def _version(mod: str) -> str | None:
    try:
        m = __import__(mod)
    except Exception:
        return None
    for attr in ("__version__", "version"):
        v = getattr(m, attr, None)
        if isinstance(v, str):
            return v
    try:
        from importlib.metadata import version

        return version(mod)
    except Exception:
        return "?"


def _requires() -> list[str] | None:
    """The requirements of this install of the program, read from its metadata; None in the packaged application,
    which carries the versions it was built and tested with, or without metadata (a source tree on the path)."""
    from importlib import metadata

    if getattr(sys, "frozen", False):
        return None
    try:
        return metadata.requires("pyarchinit-photogrammetry") or []
    except metadata.PackageNotFoundError:
        return None


def outside(dist: str, version: str | None) -> str | None:
    """The requirement of this install on ``dist`` that ``version`` does not meet (``"pycolmap<4.3,>=4.2.1"``: what
    to install to go back to a version the program was tested with), or None: met, not bounded by the program, the
    version unknown, or no metadata (:func:`_requires`)."""
    requires = _requires()
    if not requires or not version or version == "?":
        return None
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.utils import canonicalize_name
    from packaging.version import InvalidVersion, Version

    for line in requires:
        try:
            req = Requirement(line)
        except InvalidRequirement:
            continue
        if canonicalize_name(req.name) != canonicalize_name(dist):
            continue
        try:
            met = req.specifier.contains(Version(version), prereleases=True)
        except InvalidVersion:
            return None
        return None if met else f"{req.name}{req.specifier}"
    return None


def colmap_gpu_check() -> Check:
    """What COLMAP itself can run on a GPU here (:func:`.engine.upstream.colmap_gpu`): CUDA makes the depth maps with
    COLMAP's PatchMatch; a back-end of a later build is named, and used once measured against the program's own
    kernels (the line "GPU (wgpu)")."""
    from .engine import upstream

    found = upstream.colmap_gpu()
    if not found:
        return Check("COLMAP GPU", OK, "none (COLMAP runs on the CPU)")
    parts = ["CUDA: COLMAP's PatchMatch makes the depth maps"] if "cuda" in found else []
    others = [name.upper() for name in found if name != "cuda"]
    if others:
        parts.append(f"{', '.join(others)}: not used yet (the program's own kernels do that work)")
    return Check("COLMAP GPU", OK, "; ".join(parts))


def open3d_gpu_check() -> Check:
    """The devices besides the CPU of this build of Open3D (:func:`.engine.upstream.open3d_gpu`): the program runs
    Open3D on the CPU (fusion of the depth maps, Poisson), so a device found is named, not used."""
    from .engine import upstream

    found = upstream.open3d_gpu()
    if not found:
        return Check("Open3D GPU", OK, "none (Open3D runs on the CPU)")
    return Check("Open3D GPU", OK, f"{', '.join(name.upper() for name in found)} available, not used: the program "
                                   "runs Open3D on the CPU (fusion of the depth maps, Poisson)")


RUSTGIS_ON = ("in RustGIS, tick \"Let your Agents Control RustGIS\" in its AI Assistant settings: programs on this "
              "computer can then run commands in RustGIS and read its files, which is why it is yours to switch on")


def rustgis_check() -> Check:
    """RustGIS, the desktop GIS that the products and drawings of a chunk open in, live (:mod:`.io.rustgis_link`):
    installed or not, its version, and whether it listens to programs - it does only while the user ticks *Let your
    Agents Control RustGIS* in its AI Assistant settings, which the program never does for them. The token is never
    shown."""
    from .io import rustgis_link as link

    name = "RustGIS"
    found = link.find_rustgis()
    if found is None or found.app is None:
        return Check(name, OK, "not installed (the link with RustGIS is optional)",
                     f"RustGIS {link.MIN_TEXT} or later, from {link.RELEASES_URL}, opens the products and drawings "
                     "of a chunk and keeps them in step")
    installed = found.version
    update = f"update RustGIS: {link.RELEASES_URL}"

    def idle() -> Check:  # installed, and nobody to talk to
        if link.too_old(installed):
            return Check(name, WARN, f"{installed}: older than {link.MIN_TEXT}, which the link needs", update)
        return Check(name, OK, f"{installed or 'installed'}; agent control is off (or RustGIS is not running)",
                     RUSTGIS_ON)

    where = link.endpoint()
    if where is None:
        return idle()
    try:
        running = link.HttpTransport(where, timeout=4.0).initialize()
    except link.RustGisTooOld as exc:
        return Check(name, WARN, str(exc), update)
    except link.RustGisOff:  # it published an address and nothing answers there (it just quit), or the token is not its
        return idle()
    except link.RustGisBusy:
        return Check(name, OK, f"{installed or 'installed'}; agent control is on at {where.url} but RustGIS is not "
                               "answering now (minimized, hidden or busy)")
    except link.RustGisError as exc:
        return Check(name, WARN, f"agent control is on at {where.url}, but {exc}")
    return Check(name, OK, f"{running}; agent control is on, listening at {where.url}")


PPK_HINT = ("pip install -U \"pyarchinit-photogrammetry-core>=0.5\" (the applications include it), or set PPG_RNX2RTKP "
            "to the rnx2rtkp program of RTKLIB-EX 2.5")


def ppk_check() -> Check:
    """Where the PPK of the GNSS positions runs: RTKLIB inside the native core (its version and API), the rnx2rtkp
    program named by PPG_RNX2RTKP, or nowhere (and why)."""
    from . import _native

    name = "PPK (RTKLIB)"
    version = _native.version()
    if _native.supports("ppk"):
        return Check(name, OK, f"RTKLIB inside the native core {version} (API {_native.api()})")
    tool = os.environ.get("PPG_RNX2RTKP", "").strip()
    if tool:
        path = shutil.which(tool) or (tool if Path(tool).is_file() else None)
        if path and os.access(path, os.X_OK):
            return Check(name, OK, f"external rnx2rtkp named by PPG_RNX2RTKP: {path}")
        return Check(name, WARN, f"PPG_RNX2RTKP names {tool}, which is not a program that can run", PPK_HINT)
    if _native.has("ppk"):
        detail = f"RTKLIB is inside the native core {version}, which is switched off (PPG_NATIVE=0)"
    elif version:
        detail = f"the native core {version} has no RTKLIB (0.5 or later has it)"
    else:
        detail = "no native core, and no rnx2rtkp named by PPG_RNX2RTKP"
    return Check(name, WARN, detail, PPK_HINT)


CRINEX_HINT = ("pip install -U \"pyarchinit-photogrammetry-core>=0.6\" (the applications include it from 1.6.1), or the "
               "CRX2RNX program of RNXCMP in the PATH or named by PPG_CRX2RNX")


def crinex_check() -> Check:
    """Where the Hatanaka-compressed RINEX (Compact RINEX) of the permanent stations is decoded - every archive of the
    EPN, the IGS and RING has its observations so: inside the native core (its version and API), by the CRX2RNX
    program named by PPG_CRX2RNX or in the PATH, or nowhere (and why: a station, a baseline or PPP cannot run)."""
    from . import _native

    name = "Hatanaka (Compact RINEX)"
    version = _native.version()
    if _native.supports("crinex"):
        return Check(name, OK, f"decoded inside the native core {version} (API {_native.api()})")
    tool = _native._crx2rnx_program()
    if tool:
        path = shutil.which(tool) or (tool if Path(tool).is_file() else None)
        if path and os.access(path, os.X_OK):
            return Check(name, OK, f"external CRX2RNX: {path}")
        return Check(name, WARN, f"PPG_CRX2RNX names {tool}, which is not a program that can run", CRINEX_HINT)
    if _native.has("crinex"):
        detail = f"the decoder is inside the native core {version}, which is switched off (PPG_NATIVE=0)"
    elif version:
        detail = (f"the native core {version} has no Hatanaka decoder (0.6 or later has it): the files of the "
                  "permanent stations cannot be read")
    else:
        detail = "no native core, and no CRX2RNX: the files of the permanent stations cannot be read"
    return Check(name, WARN, detail, CRINEX_HINT)


def run_checks(gui: bool = True) -> Report:
    from . import __version__

    r = Report()
    frozen = bool(getattr(sys, "frozen", False))
    r.add("pyarchinit-photogr.", OK, f"{__version__}" + (" (packaged application)" if frozen else ""))
    r.add("Python", OK if sys.version_info >= (3, 10) else FAIL,
          f"{platform.python_version()}  {sys.executable}",
          "" if sys.version_info >= (3, 10) else "Python 3.10 or newer is required")
    r.add("System", OK, f"{platform.system()} {platform.release()} ({platform.machine()})")

    # another ppg-gui earlier on PATH than this interpreter's one (stale install)
    if not frozen:
        here = Path(sys.executable).resolve().parent
        found = shutil.which("ppg-gui")
        if found and Path(found).resolve().parent != here:
            r.add("ppg-gui on PATH", WARN, found,
                  f"Typing 'ppg-gui' in a terminal runs this file, not the one in {here}. Activate the "
                  f"environment of this install ('source .venv/bin/activate') or remove the other one "
                  f"('{Path(found).parent / 'python'} -m pip uninstall pyarchinit-photogrammetry').")
        elif found:
            r.add("ppg-gui on PATH", OK, found)
        else:
            r.add("ppg-gui on PATH", WARN, "not found",
                  "Activate the virtual environment, or run 'python -m pyarchinit_photogrammetry.gui'.")
    if "anaconda3" in sys.executable.lower() or "miniconda" in sys.executable.lower():
        r.add("Interpreter", WARN, "Anaconda/Miniconda base environment",
              "Use a dedicated virtual environment: Anaconda's own compiled wheels (SciPy, VTK) may not load "
              "or may crash on recent systems.")

    # core libraries
    for mod, label in (("numpy", "numpy"), ("pycolmap", "pycolmap"), ("cv2", "OpenCV"), ("rasterio", "rasterio"),
                       ("pyproj", "pyproj"), ("shapely", "shapely"), ("trimesh", "trimesh"), ("laspy", "laspy"),
                       ("ezdxf", "ezdxf"), ("sqlalchemy", "SQLAlchemy")):
        v = _version(mod)
        req = outside(mod, v) if mod == "pycolmap" else None
        if req:
            r.add(label, WARN, f"{v}: not a version this install was tested with", f'pip install "{req}"')
        else:
            r.add(label, OK if v else FAIL, v or "missing", "" if v else "pip install \"pyarchinit-photogrammetry[all]\"")
    try:
        from scipy.optimize import least_squares  # noqa: F401
        from scipy.sparse.linalg import svds  # noqa: F401

        r.add("SciPy", OK, _version("scipy") or "?")
    except Exception as exc:
        hint = ("The SciPy extension modules of this environment do not load on this system (typical of the "
                "Anaconda base environment on recent macOS): use a dedicated virtual environment.")
        r.add("SciPy", FAIL, f"import error: {str(exc).splitlines()[0][:90]}", hint)
    try:
        from rasterio.crs import CRS

        CRS.from_epsg(4326).to_wkt()
        r.add("PROJ data", OK, "EPSG database readable")
    except Exception as exc:
        r.add("PROJ data", FAIL, str(exc)[:90], "Unset PROJ_LIB / PROJ_DATA exported by Anaconda or QGIS.")

    # optional engines
    from . import _native

    v = _native.version()
    r.add("Native core (Rust)", OK if v else WARN, v or "missing (numpy code, slower)",
          "" if v else "pip install ./native from the source tree (a Rust toolchain is needed), or "
          "pip install pyarchinit-photogrammetry-core")
    if v:
        gpus = _native.gpu_adapters()
        setting = _native.gpu_setting()
        if setting.lower() == "off":
            r.add("GPU (wgpu)", WARN, "switched off (PPG_GPU=off)", "")
        elif (_native.api() or 0) < 5:
            r.add("GPU (wgpu)", WARN, "the native core is older than the GPU kernels (API 5)",
                  "pip install -U pyarchinit-photogrammetry-core")
        elif gpus:
            chosen = _native.gpu_device()
            names = ", ".join(f"{a['index']}: {a['name']} ({a['backend']}, {a['device_type']})" for a in gpus)
            r.add("GPU (wgpu)", OK, names + (f"; in use: {setting}" if chosen is not None and chosen >= 0 and setting != 'auto'
                                            else "; in use: the best one" if chosen == -1 else "; in use: none"),
                  "" if chosen is not None else f"PPG_GPU={setting!r} matches no adapter: auto, off, an index or part of a name")
        else:
            r.add("GPU (wgpu)", WARN, "no Metal, Vulkan or DirectX 12 device: the matching runs on the CPU", "")
    r.checks.append(ppk_check())
    r.checks.append(crinex_check())
    v = _version("open3d")
    if v is None:
        r.add("Open3D", WARN, "missing (Poisson meshing uses COLMAP's PoissonRecon)",
              "pip install \"pyarchinit-photogrammetry[mesh]\"")
    elif v == "0.20.0":
        r.add("Open3D", WARN, v, "Open3D 0.20.0 crashes in Poisson reconstruction on some systems: the mesh step "
                                 "falls back to a height field. Install 0.19: pip install \"open3d==0.19.0\".")
    elif req := outside("open3d", v):
        r.add("Open3D", WARN, f"{v}: not a version this install was tested with", f'pip install "{req}"')
    else:
        r.add("Open3D", OK, v)
    r.checks.append(colmap_gpu_check())
    r.checks.append(open3d_gpu_check())
    r.checks.append(rustgis_check())
    v = _version("ifcopenshell")
    r.add("IfcOpenShell", OK if v else WARN, v or "missing (IFC export disabled)",
          "" if v else "pip install \"pyarchinit-photogrammetry[bim]\"")
    v = _version("pye57")
    if v == "?":  # no metadata in the packaged application: the version is a submodule of pye57
        try:
            from pye57.__version__ import __version__ as v
        except Exception:
            pass
    r.add("E57 scans (pye57)", OK if v else WARN, v or "missing (E57 import and export disabled; PTX works)",
          "" if v else "pip install \"pyarchinit-photogrammetry[scans]\"")
    from .ai import sam_env

    mode = sam_env.available()
    if mode == "local":
        r.add("SAM 2.1", OK, f"PyTorch {_dist_version('torch')} in this environment (worker process)")
    elif mode == "external":
        r.add("SAM 2.1", OK, f"separate environment {sam_env.env_dir()}")
    else:
        r.add("SAM 2.1", WARN, "not installed",
              "Strumenti > Segment Anything 2.1 > Installa (the application installs PyTorch and SAM 2.1 "
              "in a separate environment), or pip install \"pyarchinit-photogrammetry[sam]\"")
    from .engine import learned

    mode = learned.available()
    if mode == "local":
        r.add("Learned features (LightGlue)", OK, f"kornia {_dist_version('kornia')} in this environment (worker process)")
    elif mode == "external":
        r.add("Learned features (LightGlue)", OK, f"kornia in the separate environment {sam_env.env_dir()}")
    else:
        r.add("Learned features (LightGlue)", WARN, "not installed (SIFT only)",
              "Flusso di lavoro > Allinea foto > Punti: install from there (PyTorch and kornia in the separate "
              "environment of SAM 2.1), or pip install \"pyarchinit-photogrammetry[learned]\"")

    # PyArchInit SQLite geometries: probed in a separate process, so that loading the extension
    # (which brings its own PROJ/GEOS/sqlite) can never destabilise this one
    try:
        from .engine.isolate import IsolatedCrash, run_isolated
        from .pyarchinit.database import spatialite_path

        p = run_isolated(spatialite_path, timeout=120)
        r.add("SpatiaLite", OK if p else WARN, p or "mod_spatialite not found",
              "" if p else "Needed only to write geometries into a PyArchInit SQLite database: install libspatialite "
                           "(QGIS, Homebrew, MacPorts, apt) or set SPATIALITE_LIBRARY_PATH.")
    except IsolatedCrash as exc:
        r.add("SpatiaLite", WARN, f"loading mod_spatialite crashes (exit {exc.exit_code})",
              "The installed mod_spatialite is incompatible with the libraries of this environment: writing "
              "geometries into a PyArchInit SQLite database will not work (PostGIS is unaffected).")
    except Exception as exc:  # pragma: no cover
        r.add("SpatiaLite", WARN, str(exc)[:90])

    if gui:
        v = _version("PySide6")
        r.add("PySide6 (Qt)", OK if v else FAIL, v or "missing", "" if v else "pip install \"pyarchinit-photogrammetry[gui]\"")
        v = _version("qtawesome")
        r.add("qtawesome (icons)", OK if v else WARN, v or "missing (interface without icons)",
              "" if v else "pip install qtawesome")
        pq = _version("pyvistaqt")
        try:  # not "import vtk": the full package also loads VTK's PROJ/SQL modules
            from vtkmodules.vtkCommonCore import vtkVersion

            vtk = vtkVersion.GetVTKVersion()
        except Exception:
            vtk = None
        if pq is None or vtk is None:
            r.add("3D view (VTK)", WARN, "pyvistaqt/VTK missing: software renderer",
                  "pip install \"pyarchinit-photogrammetry[gui]\"")
        else:
            try:
                major, minor = (int(x) for x in pq.split(".")[:2])
                old = (major, minor) < (0, 13)
            except ValueError:
                old = False
            if old and sys.platform == "darwin":
                r.add("3D view (VTK)", FAIL, f"pyvistaqt {pq}, VTK {vtk}",
                      "pyvistaqt before 0.13 hangs on start on recent macOS: pip install -U pyvistaqt")
            elif sys.platform == "win32" and platform.machine().upper() == "ARM64":
                from .gui.wgl import VTK_MINIMUM, opengl_version  # Windows on ARM: is there OpenGL 3.2?

                found = opengl_version()
                if found is None:
                    r.add("3D view (VTK)", OK, f"pyvistaqt {pq}, VTK {vtk} (OpenGL not probed)")
                elif found[0] < VTK_MINIMUM:
                    r.add("3D view (VTK)", WARN, f"pyvistaqt {pq}, VTK {vtk}; OpenGL {found[1]} ({found[2]}): "
                                                 "software renderer",
                          "VTK needs OpenGL 3.2: install the graphics driver of the computer or the \"OpenCL, "
                          "OpenGL, and Vulkan Compatibility Pack\" from the Microsoft Store")
                else:
                    r.add("3D view (VTK)", OK, f"pyvistaqt {pq}, VTK {vtk}; OpenGL {found[1]} ({found[2]})")
            else:
                r.add("3D view (VTK)", OK, f"pyvistaqt {pq}, VTK {vtk}")
        try:
            from .gui.logging_setup import log_dir

            r.add("Log folder", OK, str(log_dir()))
        except Exception as exc:  # pragma: no cover
            r.add("Log folder", WARN, str(exc)[:90])
    return r


def main(argv: list[str] | None = None) -> int:
    rep = run_checks(gui=True)
    print(rep.text())
    return 1 if rep.failures else 0


__all__ = ["run_checks", "ppk_check", "rustgis_check", "Report", "Check", "main", "OK", "WARN", "FAIL"]
