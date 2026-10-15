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
        r.add(label, OK if v else FAIL, v or "missing", "" if v else f"pip install \"pyarchinit-photogrammetry[all]\"")
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
    v = _version("open3d")
    if v is None:
        r.add("Open3D", WARN, "missing (Poisson meshing uses COLMAP's PoissonRecon)",
              "pip install \"pyarchinit-photogrammetry[mesh]\"")
    elif v == "0.20.0":
        r.add("Open3D", WARN, v, "Open3D 0.20.0 crashes in Poisson reconstruction on some systems: the mesh step "
                                 "falls back to a height field. Install 0.19: pip install \"open3d==0.19.0\".")
    else:
        r.add("Open3D", OK, v)
    v = _version("ifcopenshell")
    r.add("IfcOpenShell", OK if v else WARN, v or "missing (IFC export disabled)",
          "" if v else "pip install \"pyarchinit-photogrammetry[bim]\"")
    v = _version("torch")
    r.add("PyTorch (SAM 2.1)", OK if v else WARN, v or "missing (Segment Anything disabled)",
          "" if v else "pip install \"pyarchinit-photogrammetry[sam]\"")

    # PyArchInit SQLite geometries
    try:
        from .pyarchinit.database import spatialite_path

        p = spatialite_path()
        r.add("SpatiaLite", OK if p else WARN, p or "mod_spatialite not found",
              "" if p else "Needed only to write geometries into a PyArchInit SQLite database: install libspatialite "
                           "(QGIS, Homebrew, MacPorts, apt) or set SPATIALITE_LIBRARY_PATH.")
    except Exception as exc:  # pragma: no cover
        r.add("SpatiaLite", WARN, str(exc)[:90])

    if gui:
        v = _version("PySide6")
        r.add("PySide6 (Qt)", OK if v else FAIL, v or "missing", "" if v else "pip install \"pyarchinit-photogrammetry[gui]\"")
        v = _version("qtawesome")
        r.add("qtawesome (icons)", OK if v else WARN, v or "missing (interface without icons)",
              "" if v else "pip install qtawesome")
        pq = _version("pyvistaqt")
        vtk = _version("vtk") or _version("vtkmodules")
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


__all__ = ["run_checks", "Report", "Check", "main", "OK", "WARN", "FAIL"]
