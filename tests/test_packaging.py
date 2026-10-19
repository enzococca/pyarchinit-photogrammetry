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
