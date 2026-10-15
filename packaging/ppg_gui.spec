# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the desktop application. Build with ``python packaging/build.py``."""
import os
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).resolve().parent
NAME = "PyArchInit Photogrammetry"
ICON = {"darwin": ROOT / "packaging" / "build" / "icon.icns",
        "win32": ROOT / "packaging" / "build" / "icon.ico"}.get(sys.platform)
ICON = str(ICON) if ICON and ICON.exists() else None

hidden = (collect_submodules("pyarchinit_photogrammetry") + collect_submodules("rasterio")
          + collect_submodules("pyproj") + collect_submodules("shapely") + collect_submodules("laspy")
          + ["qtawesome", "pyvistaqt", "pyvista", "sqlalchemy.dialects.sqlite",
             "sqlalchemy.dialects.postgresql", "scipy.special._cdflib", "ezdxf", "trimesh"])
for optional in ("open3d", "ifcopenshell", "psycopg2"):
    try:
        __import__(optional)
    except Exception:
        continue
    hidden += collect_submodules(optional)

datas = ([(str(ROOT / "src" / "pyarchinit_photogrammetry" / "ai" / "sam_worker.py"), "pyarchinit_photogrammetry/ai")]
         + collect_data_files("pyarchinit_photogrammetry") + collect_data_files("qtawesome")
         + collect_data_files("pyvista") + collect_data_files("rasterio") + collect_data_files("pyproj")
         + collect_data_files("ezdxf") + collect_data_files("trimesh"))
binaries = collect_dynamic_libs("pycolmap")
for optional in ("open3d", "ifcopenshell"):
    try:
        __import__(optional)
    except Exception:
        continue
    datas += collect_data_files(optional)
    binaries += collect_dynamic_libs(optional)

excludes = ["torch", "torchvision", "sam2", "tkinter", "PyQt5", "PyQt6", "PySide2", "IPython", "jupyter",
            "notebook", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
            "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.QtQuick3D", "PySide6.QtMultimedia",
            "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtLocation", "PySide6.QtPdf",
            "PySide6.QtPdfWidgets", "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtBluetooth",
            "PySide6.QtNfc", "PySide6.QtRemoteObjects", "PySide6.QtSensors", "PySide6.QtSerialPort",
            "PySide6.QtTextToSpeech", "PySide6.QtWebSockets", "PySide6.QtWebChannel", "PySide6.QtHttpServer",
            "PySide6.QtGraphs", "PySide6.QtSpatialAudio", "PySide6.QtWebView"]

a = Analysis([str(ROOT / "packaging" / "launcher.py")], pathex=[str(ROOT / "src")], binaries=binaries, datas=datas,
             hiddenimports=hidden, hookspath=[], runtime_hooks=[], excludes=excludes, noarchive=False)
# Open3D's TensorFlow/PyTorch operator libraries are useless here and pull missing dependencies.
a.binaries = [b for b in a.binaries if not any(k in b[0] for k in ("open3d_tf_ops", "open3d_torch_ops"))]
# rasterio and pyproj both ship a libproj with the same name but different exports: see
# fix_private_dylibs() in build.py, which makes every package load its own private libraries.
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=NAME, debug=False, bootloader_ignore_signals=False,
          strip=False, upx=False, console=False, icon=ICON)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=NAME)
if sys.platform == "darwin":
    from pyarchinit_photogrammetry import __version__

    app = BUNDLE(coll, name=f"{NAME}.app", icon=ICON, bundle_identifier="org.pyarchinit.photogrammetry",
                 info_plist={"CFBundleShortVersionString": __version__, "CFBundleVersion": __version__,
                             "NSHighResolutionCapable": True, "LSMinimumSystemVersion": "12.0",
                             "CFBundleDocumentTypes": [{"CFBundleTypeExtensions": ["ppg"],
                                                        "CFBundleTypeName": "pyarchinit-photogrammetry project",
                                                        "CFBundleTypeRole": "Editor"}]})
