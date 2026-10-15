"""Live link with QGIS: the orthomosaic, the DEM and everything drawn in the interface
(stratigraphic units, sections, contour lines, points) open in a QGIS project, and the
vector layers are rewritten on every change while QGIS reloads them every second.

* :func:`export_layers` writes one GeoJSON per kind of drawing (atomic replace, CRS member set).
* :func:`build_project` creates the ``.qgs`` with QGIS's own Python (styles, labels, auto refresh).
* :func:`open_in_qgis` starts QGIS on the project.

QGIS is found in the usual places (``/Applications/QGIS*.app``, ``C:\\Program Files\\QGIS*``,
``/usr/bin/qgis``) or through ``PPG_QGIS``.
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# fixed files, so that the QGIS project never needs rebuilding while drawing
LIVE_FILES = {  # file stem -> (title in QGIS, geometry type)
    "unita": ("Unita' stratigrafiche (ppg)", "Polygon"),
    "sezioni": ("Sezioni (ppg)", "LineString"),
    "curve": ("Curve di livello (ppg)", "LineString"),
    "linee": ("Linee (ppg)", "LineString"),
    "punti": ("Punti e quote (ppg)", "Point"),
}


@dataclass
class QgisInstall:
    app: str  # what to launch (path of the app/executable)
    python: str | None  # interpreter with the qgis module, to build projects
    env: dict = field(default_factory=dict)


def find_qgis() -> QgisInstall | None:
    env_path = os.environ.get("PPG_QGIS")
    if sys.platform == "darwin":
        apps = ([env_path] if env_path else []) + sorted(glob.glob("/Applications/QGIS*.app"), reverse=True)
        for app in apps:
            py = Path(app) / "Contents" / "MacOS" / "bin" / "python3"
            if Path(app).exists():
                env = {"QT_PLUGIN_PATH": str(Path(app) / "Contents" / "PlugIns"),
                       "PROJ_DATA": str(Path(app) / "Contents" / "Resources" / "proj"),
                       "GDAL_DATA": str(Path(app) / "Contents" / "Resources" / "gdal"),
                       "QT_QPA_PLATFORM": "offscreen"}
                return QgisInstall(app, str(py) if py.exists() else None, env)
    elif os.name == "nt":
        roots = ([env_path] if env_path else []) + sorted(glob.glob(r"C:\Program Files\QGIS*"), reverse=True) + \
            [r"C:\OSGeo4W"]
        for root in roots:
            for bat in ("qgis-ltr-bin.exe", "qgis-bin.exe", "qgis.bat"):
                exe = Path(root) / "bin" / bat
                if exe.exists():
                    py = next((str(Path(root) / "bin" / b) for b in ("python-qgis-ltr.bat", "python-qgis.bat")
                               if (Path(root) / "bin" / b).exists()), None)
                    return QgisInstall(str(exe), py, {"QT_QPA_PLATFORM": "offscreen"})
    else:
        exe = env_path or next((p for p in ("/usr/bin/qgis", "/usr/local/bin/qgis") if Path(p).exists()), None)
        if exe:
            return QgisInstall(exe, "/usr/bin/python3", {"QT_QPA_PLATFORM": "offscreen"})
    return None


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") or "layer"


def _crs_urn(crs: str) -> dict | None:
    m = re.match(r"EPSG:(\d+)$", str(crs or ""), re.I)
    return {"type": "name", "properties": {"name": f"urn:ogc:def:crs:EPSG::{m.group(1)}"}} if m else None


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.stem, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def classify_layer(name: str, kind: str) -> str:
    """Which live file a layer of the interface goes to."""
    if kind == "polygon":
        return "unita"
    if kind == "point":
        return "punti"
    if name.startswith("Sezioni"):
        return "sezioni"
    if name.startswith("Curve di livello"):
        return "curve"
    return "linee"


def export_layers(folder: str | Path, layers: dict[str, dict], crs: str) -> dict[str, Path]:
    """Write the live GeoJSON files from the layers of the raster view.

    ``layers``: ``{name: {"kind": "polygon"|"line"|"point", "features": [attrs with "_geom"]}}``;
    the ``(item, label, attrs)`` triples of :class:`gui.image_views.RasterView` are accepted too.
    Every file is written even when empty, so QGIS sees deletions."""
    folder = Path(folder)
    out = {stem: [] for stem in LIVE_FILES}
    for name, lay in layers.items():
        stem = classify_layer(name, lay["kind"])
        for feat in lay["features"]:
            attrs = feat[-1] if isinstance(feat, (tuple, list)) else feat
            if not isinstance(attrs, dict):
                continue
            geom = attrs.get("_geom")
            if not geom:
                continue
            gtype, coords = geom
            if gtype == "Polygon":
                ring = [list(map(float, c)) for c in coords]
                if ring and ring[0] != ring[-1]:
                    ring.append(ring[0])
                geometry = {"type": "Polygon", "coordinates": [ring]}
            elif gtype == "LineString":
                geometry = {"type": "LineString", "coordinates": [list(map(float, c)) for c in coords]}
            else:
                geometry = {"type": "Point", "coordinates": list(map(float, coords))}
            props = {"layer": name}
            props.update({k: (v if isinstance(v, (str, int, float, bool)) or v is None else str(v))
                          for k, v in attrs.items() if not str(k).startswith("_")})
            out[stem].append({"type": "Feature", "properties": props, "geometry": geometry})
    paths = {}
    for stem, feats in out.items():
        fc = {"type": "FeatureCollection", "name": stem, "features": feats}
        urn = _crs_urn(crs)
        if urn:
            fc["crs"] = urn
        paths[stem] = folder / f"{stem}.geojson"
        _write_atomic(paths[stem], fc)
    return paths


_BUILD_SCRIPT = r'''
import json, sys
from qgis.core import (Qgis, QgsApplication, QgsCoordinateReferenceSystem, QgsPalLayerSettings, QgsProject,
                       QgsRasterLayer, QgsVectorLayer, QgsVectorLayerSimpleLabeling, QgsTextFormat,
                       QgsHillshadeRenderer)
from qgis.PyQt.QtGui import QColor
cfg = json.load(open(sys.argv[1], encoding="utf-8"))
app = QgsApplication([], False)
app.initQgis()
proj = QgsProject.instance()
crs = QgsCoordinateReferenceSystem(cfg["crs"]) if cfg.get("crs") else QgsCoordinateReferenceSystem()
if crs.isValid():
    proj.setCrs(crs)
proj.setTitle(cfg.get("title", "pyarchinit-photogrammetry"))
root = proj.layerTreeRoot()
group_v = root.addGroup("Disegni (aggiornati dal vivo)")
group_r = root.addGroup("Prodotti fotogrammetrici")
report = {"vectors": [], "rasters": []}
for v in cfg["vectors"]:
    lyr = QgsVectorLayer(v["path"] + "|geometrytype=" + v["geometry"], v["name"], "ogr")
    if crs.isValid():
        lyr.setCrs(crs)
    sym = lyr.renderer().symbol() if lyr.renderer() is not None else None
    if sym is not None:
        col = QColor(v["color"])
        if v["geometry"] == "Polygon":
            fill = QColor(col); fill.setAlpha(70)
            sym.setColor(fill)
            sym.symbolLayer(0).setStrokeColor(col)
            sym.symbolLayer(0).setStrokeWidth(0.5)
        else:
            sym.setColor(col)
            if v["geometry"] == "LineString":
                sym.setWidth(0.5)
            else:
                sym.setSize(2.5)
    if v.get("label"):
        pal = QgsPalLayerSettings()
        pal.fieldName = v["label"]
        pal.enabled = True
        fmt = QgsTextFormat(); fmt.setSize(9); fmt.setColor(QColor(v.get("label_color", "#202020")))
        pal.setFormat(fmt)
        lyr.setLabeling(QgsVectorLayerSimpleLabeling(pal))
        lyr.setLabelsEnabled(True)
    lyr.setAutoRefreshInterval(int(cfg.get("refresh_ms", 1000)))
    try:
        lyr.setAutoRefreshMode(Qgis.AutoRefreshMode.ReloadData)
    except AttributeError:
        lyr.setAutoRefreshEnabled(True)
    proj.addMapLayer(lyr, False)
    group_v.addLayer(lyr)
    report["vectors"].append({"name": v["name"], "valid": lyr.isValid(), "features": lyr.featureCount()})
for r in cfg["rasters"]:
    lyr = QgsRasterLayer(r["path"], r["name"])
    if crs.isValid():
        lyr.setCrs(crs)
    if r.get("hillshade") and lyr.isValid():
        lyr.setRenderer(QgsHillshadeRenderer(lyr.dataProvider(), 1, 315, 45))
    if r.get("opacity") is not None:
        lyr.setOpacity(float(r["opacity"]))
    proj.addMapLayer(lyr, False)
    node = group_r.addLayer(lyr)
    if r.get("hidden"):
        node.setItemVisibilityChecked(False)
    report["rasters"].append({"name": r["name"], "valid": lyr.isValid()})
ok = proj.write(cfg["project"])
report["written"] = bool(ok)
print("PPG_QGIS_REPORT " + json.dumps(report), flush=True)
app.exitQgis()
'''

STYLES = {  # stem -> (colour, label field, label colour)
    "unita": ("#e6194b", "etichetta", "#ffffff"),
    "sezioni": ("#ffd000", "etichetta", "#7a5c00"),
    "curve": ("#4a3b2a", "quota", "#4a3b2a"),
    "linee": ("#00a0ff", "etichetta", "#003a66"),
    "punti": ("#27ae60", "z", "#14532d"),
}


def build_project(project_path: str | Path, crs: str, layer_files: dict[str, Path], rasters: list[dict],
                  title: str = "pyarchinit-photogrammetry", refresh_ms: int = 1000,
                  qgis: QgisInstall | None = None) -> dict:
    """Write a QGIS project with the live vector files (auto-reloaded) and the rasters
    (``[{"name", "path", "hillshade"?, "opacity"?}]``). Needs QGIS's Python; returns the
    report of the layers QGIS loaded."""
    qgis = qgis or find_qgis()
    if qgis is None or not qgis.python:
        raise RuntimeError("QGIS non trovato: installalo (qgis.org) o indica il percorso con PPG_QGIS")
    vectors = []
    for stem, path in layer_files.items():
        title_v, geometry = LIVE_FILES[stem]
        color, label, label_color = STYLES[stem]
        vectors.append({"name": title_v, "path": str(path), "geometry": geometry, "color": color,
                        "label": label, "label_color": label_color})
    cfg = {"project": str(project_path), "crs": crs if str(crs).upper().startswith("EPSG:") else "",
           "vectors": vectors, "rasters": [r for r in rasters if r.get("path") and Path(r["path"]).exists()],
           "title": title, "refresh_ms": refresh_ms}
    Path(project_path).parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ppg-qgis-") as tmp:
        script, conf = Path(tmp) / "build.py", Path(tmp) / "cfg.json"
        script.write_text(_BUILD_SCRIPT, encoding="utf-8")
        conf.write_text(json.dumps(cfg), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "PROJ_", "GDAL_", "QT_"))}
        env.update(qgis.env)
        r = subprocess.run([qgis.python, str(script), str(conf)], capture_output=True, text=True, env=env,
                           timeout=300, close_fds=False)
    line = next((ln for ln in r.stdout.splitlines() if ln.startswith("PPG_QGIS_REPORT ")), None)
    if line is None:
        raise RuntimeError("creazione del progetto QGIS non riuscita:\n" + (r.stderr or r.stdout)[-1500:])
    report = json.loads(line.split(" ", 1)[1])
    if not report.get("written"):
        raise RuntimeError(f"QGIS non ha potuto scrivere {project_path}")
    return report


def open_in_qgis(project_path: str | Path, qgis: QgisInstall | None = None) -> None:
    qgis = qgis or find_qgis()
    if qgis is None:
        raise RuntimeError("QGIS non trovato")
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-a", qgis.app, str(project_path)], close_fds=False)
    else:
        subprocess.Popen([qgis.app, "--project", str(project_path)], close_fds=False)


__all__ = ["find_qgis", "export_layers", "build_project", "open_in_qgis", "classify_layer", "LIVE_FILES",
           "QgisInstall"]
