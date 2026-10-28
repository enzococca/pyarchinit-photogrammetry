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


def test_the_application_carries_the_pdf_report_and_the_excel_tables(tmp_path):
    """The PDF report (fpdf2) and the Excel tables (openpyxl) are dependencies of the program and
    parts of the application: fontTools, which reads the fonts for fpdf2, loads the tables of a font
    by their tags (``__import__``, not seen by PyInstaller) and fpdf2 reads its colour profile as
    package data. The smoke test of the application writes a PDF and a workbook and reads it back."""
    tomllib = pytest.importorskip("tomllib")
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    assert any(d.startswith("fpdf2") for d in deps) and any(d.startswith("openpyxl") for d in deps)
    spec = (ROOT / "packaging" / "ppg_gui.spec").read_text()
    for name in ('"fpdf"', '"openpyxl"', 'collect_submodules("fontTools.ttLib.tables")', 'collect_data_files("fpdf")'):
        assert name in spec, name
    check = (ROOT / "src" / "pyarchinit_photogrammetry" / "gui" / "__init__.py").read_text()
    assert "sample_pdf(" in check and "write_xlsx(" in check and "read_xlsx(" in check
    pytest.importorskip("fpdf")
    pytest.importorskip("openpyxl")
    from pyarchinit_photogrammetry.io.pdf_report import sample_pdf
    from pyarchinit_photogrammetry.io.tables import read_xlsx, write_xlsx

    assert sample_pdf(tmp_path / "check.pdf").read_bytes().startswith(b"%PDF")
    assert read_xlsx(write_xlsx(tmp_path / "check.xlsx", {"check": (["a", "b"], [[1, 2.5]])}))["check"][1] == [1, 2.5]



def test_the_libraries_of_the_engine_have_a_ceiling():
    """pycolmap and Open3D are taken up to the versions the program was tested with, the same range wherever they are
    named: a release of theirs reaches pip users after the weekly upstream workflow has tried it and a commit has
    raised the ceiling (.github/workflows/upstream.yml)."""
    tomllib = pytest.importorskip("tomllib")
    from packaging.requirements import Requirement

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    named = [Requirement(r) for r in project["dependencies"]]
    named += [Requirement(r) for extra in project["optional-dependencies"].values() for r in extra]
    for name in ("pycolmap", "open3d"):
        found = [r for r in named if r.name == name]
        assert found, f"{name} is not a requirement"
        assert len({str(r.specifier) for r in found}) == 1, f"{name} has different ranges: {found}"
        assert any(s.operator == "<" for s in found[0].specifier), f"{name} has no ceiling: {found[0]}"
