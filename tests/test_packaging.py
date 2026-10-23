"""Every data file of the package must be declared as package data, or pip installs miss it."""
from __future__ import annotations

import fnmatch
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "pyarchinit_photogrammetry"


def _package_data_patterns() -> list[str]:
    tomllib = pytest.importorskip("tomllib")  # Python 3.11+
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return cfg["tool"]["setuptools"]["package-data"]["pyarchinit_photogrammetry"]


def test_data_files_are_package_data():
    patterns = _package_data_patterns()
    data = [p.relative_to(PKG).as_posix() for p in PKG.rglob("*")
            if p.is_file() and p.suffix not in (".py", ".pyc") and "__pycache__" not in p.parts
            and not any(part.startswith(".") for part in p.relative_to(PKG).parts)]  # tool caches
    missing = [f for f in data if not any(fnmatch.fnmatch(f, pat) for pat in patterns)]
    assert data and not missing, f"not declared in [tool.setuptools.package-data]: {missing}"


def test_the_application_carries_the_figure_backends_plugins_use():
    """matplotlib loads the backend of a figure's format when it is saved (SVG, PDF, EPS): the
    program itself writes PNG only, so PyInstaller saw Agg alone and a plugin drawing its logs as
    SVG stopped with "No module named 'matplotlib.backends.backend_svg'" in the application 1.3.0.
    The spec names them, and the smoke test of the application saves a figure in each format."""
    spec = (ROOT / "packaging" / "ppg_gui.spec").read_text()
    for backend in ("backend_svg", "backend_pdf", "backend_ps", "backend_qtagg"):
        assert f"matplotlib.backends.{backend}" in spec, backend
    check = (ROOT / "src" / "pyarchinit_photogrammetry" / "gui" / "__init__.py").read_text()
    assert '("png", "svg", "pdf", "eps")' in check

