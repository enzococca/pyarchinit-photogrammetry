"""Process-level fixes for mixed installations (Anaconda, QGIS, Homebrew).

Anaconda and QGIS export ``PROJ_LIB``/``PROJ_DATA`` pointing at their own
``proj.db``. The rasterio wheel bundles a newer PROJ, which then refuses that
database ("proj.db contains DATABASE.LAYOUT.VERSION.MINOR = 2 whereas a number
>= 3 is expected") and every EPSG lookup fails. When rasterio ships its own PROJ
data, point its GDAL/PROJ at it; the environment variables are left untouched,
so other libraries in the same process (conda's own GDAL, QGIS) keep theirs.
pyproj wheels already prefer their bundled data.
"""
from __future__ import annotations

import os
from pathlib import Path


def use_bundled_proj_data() -> str | None:
    """Return the directory now used by rasterio, or ``None`` when nothing changed."""
    env = os.environ.get("PROJ_DATA") or os.environ.get("PROJ_LIB")
    if not env:
        return None
    try:
        import rasterio
        from rasterio.env import set_proj_data_search_path
    except Exception:  # pragma: no cover - rasterio missing or very old
        return None
    bundled = Path(rasterio.__file__).parent / "proj_data"
    if not (bundled / "proj.db").exists():
        return None  # conda-forge rasterio: its PROJ matches the environment
    try:
        if any(Path(p).resolve() == bundled.resolve() for p in env.split(os.pathsep) if p):
            return None
    except OSError:
        pass
    set_proj_data_search_path(str(bundled))
    return str(bundled)
