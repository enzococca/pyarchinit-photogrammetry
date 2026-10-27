"""Process-level fixes for mixed installations (Anaconda, QGIS, Homebrew).

Anaconda and QGIS export ``PROJ_LIB``/``PROJ_DATA`` (and ``GDAL_DATA``) pointing at
their own ``proj.db``. The rasterio wheel bundles a newer PROJ, which then refuses that
database ("proj.db contains DATABASE.LAYOUT.VERSION.MINOR = 2 whereas a number
>= 3 is expected") and every EPSG lookup fails. When rasterio ships its own PROJ
data, point its GDAL/PROJ at it *and* rewrite the environment variables: PROJ
creates one context per thread and each new context reads ``PROJ_DATA`` from the
environment, so without the rewrite every EPSG lookup in a worker thread (all the
background tasks of the interface) would still fail. pyproj wheels already prefer
their bundled data. Nothing happens when rasterio has no bundled data (conda-forge
or QGIS builds, whose PROJ matches the environment).

PROJ never goes to the network (:func:`proj_offline`): with ``PROJ_NETWORK=ON`` (the shell of Anaconda) PROJ downloads
the geoid grids it lacks in silence, and on a computer without the network - the application started from the Finder -
it gives heights above the ellipsoid for heights above the geoid. The grids the program downloads go in a folder of its
own, which :func:`add_proj_folder` puts in pyproj's search (:func:`..gnss.heights.register`) - not in ``PROJ_DATA``:
rasterio hands ``PROJ_DATA`` to GDAL as one folder at every ``rasterio.open`` (``GDALEnv.start``), and two folders
there leave GDAL without ``proj.db``, in this process and in those it starts. Every conversion of coordinates goes
through pyproj; rasterio's GDAL needs no grid of the geoid.
"""
from __future__ import annotations

import os
from pathlib import Path


def proj_offline() -> None:
    """PROJ never goes to the network: ``PROJ_NETWORK=OFF`` for every PROJ that reads the environment (rasterio's,
    those of the processes the program starts) and the network of pyproj switched off, whatever the shell said. Called
    when the package is imported, before anything opens a coordinate system: the grids PROJ needs are local, or a
    conversion that needs one says it is missing."""
    os.environ["PROJ_NETWORK"] = "OFF"
    try:
        from pyproj import network
    except Exception:  # pragma: no cover - pyproj missing
        return
    network.set_network_enabled(False)


def add_proj_folder(folder: str | os.PathLike) -> bool:
    """``folder`` (absolute) among the folders where pyproj's PROJ looks for its grids, after its own data: read by the
    PROJ of every thread that starts using pyproj from now on, and by the current one at once (a thread that used pyproj
    before keeps the folders it had: :func:`..gnss.heights.refresh`). ``PROJ_DATA`` stays as it is (see the module).
    Returns whether the folder was not there yet."""
    try:
        import pyproj.datadir
    except Exception:  # pragma: no cover - pyproj missing
        return False
    folder = str(folder)
    if folder in pyproj.datadir.get_data_dir().split(os.pathsep):
        return False
    pyproj.datadir.append_data_dir(folder)
    return True


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
    os.environ["PROJ_DATA"] = str(bundled)
    os.environ["PROJ_LIB"] = str(bundled)
    gdal_data = Path(rasterio.__file__).parent / "gdal_data"
    if gdal_data.is_dir() and os.environ.get("GDAL_DATA"):
        os.environ["GDAL_DATA"] = str(gdal_data)
    return str(bundled)


def load_open3d_first() -> None:
    """Import Open3D before any other native library.

    Open3D ships its own OpenMP runtime. When it is loaded after the ones that come
    with rasterio, SciPy or scikit-learn, PoissonRecon (``create_from_point_cloud_poisson``)
    crashes the process on macOS (segmentation fault or a silent ``exit(0)``).
    Importing it first, in every process that imports this package, keeps the
    reconstruction stable. Costs under a second; nothing happens without Open3D."""
    import os

    if os.environ.get("PPG_NO_OPEN3D"):
        return
    skip_open3d_extras()
    try:
        import open3d  # noqa: F401
    except Exception:  # pragma: no cover - optional dependency
        pass


def skip_open3d_extras() -> None:
    """``import open3d`` also loads Open3D-ML (its datasets import scikit-learn, SciPy's
    statistics and pandas) and the plotly viewer (dash): most of its import time, a few
    seconds in the packaged application. Nothing here uses them: placeholders take their
    place (scikit-learn is still imported when the classifier needs it)."""
    import sys
    import types

    if "open3d" in sys.modules:
        return
    sys.modules.setdefault("open3d.ml", types.ModuleType("open3d.ml"))
    if "open3d.visualization.draw_plotly" not in sys.modules:
        plotly = types.ModuleType("open3d.visualization.draw_plotly")

        def unavailable(*_args, **_kwargs):
            raise RuntimeError("Open3D's plotly viewer is not loaded by pyarchinit-photogrammetry")

        plotly.draw_plotly = plotly.draw_plotly_server = unavailable
        sys.modules[plotly.__name__] = plotly


def glog_flushes_each_line() -> None:
    """COLMAP's log (glog) written to a file line by line: by default glog keeps 30 seconds of
    lines in a buffer, and :mod:`engine.colmap_log` tails that file to show the progress of a
    step while COLMAP runs it. Read when the library loads: call before importing pycolmap."""
    os.environ.setdefault("GLOG_logbufsecs", "0")
