"""Live link with QGIS: the orthomosaic, the DEM and everything drawn in the interface
(stratigraphic units, sections, contour lines, points) open in a QGIS project, and the
drawings are rewritten on every change while QGIS redraws them every second.

The drawings live in one GeoPackage (:data:`LIVE_DB`, a table per kind of drawing), updated
in place, one transaction per changed table. QGIS reads a GeoPackage again at every redraw,
and the project asks QGIS to redraw those layers every second, so what is drawn here
appears in QGIS without pressing anything. Up to 0.4.1 the drawings were GeoJSON files with
"reload data every second": QGIS keeps GeoJSON in memory, and QGIS 3.34-3.42 does not start
the refresh timer of layers read from a project saved with ``autoRefreshMode``, so the map
kept its cached image until *Refresh* (see :func:`_legacy_auto_refresh`).

* :class:`LiveLink` / :func:`export_layers` write the GeoPackage (no GDAL needed).
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
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

LIVE_DB = "disegni_ppg.gpkg"
# fixed tables, so that the QGIS project never needs rebuilding while drawing
LIVE_TABLES = {  # table -> (title in QGIS, geometry type)
    "unita": ("Unita' stratigrafiche (ppg)", "POLYGON"),
    "sezioni": ("Sezioni (ppg)", "LINESTRING"),
    "curve": ("Curve di livello (ppg)", "LINESTRING"),
    "linee": ("Linee (ppg)", "LINESTRING"),
    "punti": ("Punti (ppg)", "POINT"),
    "quote": ("Quote (ppg)", "POINT"),  # the columns of pyarchinit_quote
}
NUMERIC_FIELDS = {"quota", "z", "x", "y", "lunghezza_m", "area_m2", "volume_m3", "quota_q"}  # REAL; the rest is TEXT
INTEGER_FIELDS = {"stratigraph_index_us"}
FLAG_FIELDS = {"direttrice"}  # INTEGER 0/1


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
                # without the prefix QGIS's Python does not find its CRS database (projects
                # were written without a CRS) nor the provider plugins
                env = {"QT_PLUGIN_PATH": str(Path(app) / "Contents" / "PlugIns"),
                       "QGIS_PREFIX_PATH": str(Path(app) / "Contents" / "MacOS"),
                       "PROJ_DATA": str(Path(app) / "Contents" / "Resources" / "proj"),
                       "PROJ_LIB": str(Path(app) / "Contents" / "Resources" / "proj"),
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


def classify_layer(name: str, kind: str) -> str:
    """Which live table a layer of the interface goes to."""
    if kind == "polygon":
        return "unita"
    if kind == "point":
        return "quote" if name == "Quote" else "punti"
    if name.startswith("Sezioni"):
        return "sezioni"
    if name.startswith("Curve di livello"):
        return "curve"
    return "linee"


def _column(key: str) -> str:
    key = str(key)
    return key + "_" if key.lower() in ("fid", "geom") else key


def _sql_type(key: str) -> str:
    return "REAL" if key in NUMERIC_FIELDS else "INTEGER" if key in FLAG_FIELDS | INTEGER_FIELDS else "TEXT"


def _value(key: str, v):
    if v is None or v == "":
        return None
    try:
        if key in NUMERIC_FIELDS:
            return float(v)
        if key in INTEGER_FIELDS:
            return int(float(v))
        if key in FLAG_FIELDS:
            return int(bool(v))
        return str(v)
    except (TypeError, ValueError):
        return None


def _shape(gtype: str, coords):
    """Shapely geometry of a ``_geom`` of the raster view, None when degenerate."""
    from shapely.geometry import LineString, Point, Polygon

    try:
        if gtype == "Polygon":
            ring = [tuple(map(float, c[:2])) for c in coords]
            return Polygon(ring) if len(ring) >= 3 else None
        if gtype == "LineString":
            line = [tuple(map(float, c[:2])) for c in coords]
            return LineString(line) if len(line) >= 2 else None
        return Point(float(coords[0]), float(coords[1]))
    except (TypeError, ValueError, IndexError):
        return None


class LiveLink:
    """The GeoPackage QGIS reads the drawings from. Tables are rewritten in place (QGIS keeps
    the file open, so a replaced file would not be seen) and only when their content
    changed, in one transaction each: QGIS never reads half a table."""

    def __init__(self, folder: str | Path, crs: str):
        self.path = Path(folder) / LIVE_DB
        self.crs = crs or ""
        self._signatures: dict[str, int] = {}

    @staticmethod
    def _group(layers: dict[str, dict]) -> dict[str, list]:
        out: dict[str, list] = {t: [] for t in LIVE_TABLES}
        for name, lay in layers.items():
            table = classify_layer(name, lay["kind"])
            for feat in lay["features"]:
                attrs = feat[-1] if isinstance(feat, (tuple, list)) else feat
                if isinstance(attrs, dict) and attrs.get("_geom"):
                    out[table].append((name, attrs))
        return out

    @staticmethod
    def _signature(feats: list) -> int:
        sig = []
        for name, attrs in feats:
            gtype, coords = attrs["_geom"]
            n = len(coords) if coords is not None else 0
            sig.append((name, id(attrs["_geom"]), gtype, n, str(coords[0]) if n else "", str(coords[-1]) if n else "",
                        tuple(sorted((str(k), str(v)) for k, v in attrs.items() if not str(k).startswith("_")))))
        return hash(tuple(sig))

    def write(self, layers: dict[str, dict], force: bool = False) -> dict[str, Path]:
        """Bring the GeoPackage up to date with the layers of the raster view
        (``{name: {"kind", "features"}}``, features as attribute dicts with ``_geom`` or the
        ``(item, label, attrs)`` triples of :class:`gui.image_views.RasterView`). Every table
        exists even when empty, so QGIS sees deletions."""
        from . import gpkg

        groups = self._group(layers)
        new = not gpkg.is_geopackage(self.path)
        if new:
            self._signatures = {}
        todo = {t: f for t, f in groups.items() if force or new or self._signatures.get(t) != self._signature(f)}
        if todo:
            srs = gpkg.srs_of(self.crs)
            con = gpkg.connect(self.path)
            try:
                con.execute("BEGIN IMMEDIATE")
                if new:
                    gpkg.create(con, srs)
                for table, feats in todo.items():
                    label = STYLES[table][1]
                    keys = ["layer", "etichetta"] + ([label] if label not in ("layer", "etichetta") else [])
                    keys += sorted({_column(k) for _, a in feats for k in a if not str(k).startswith("_")} - set(keys))
                    cols = gpkg.ensure_table(con, table, LIVE_TABLES[table][1], {k: _sql_type(k) for k in keys}, srs[0],
                                             identifier=LIVE_TABLES[table][0])
                    rows = []
                    for name, attrs in feats:
                        vals = {_column(k): v for k, v in attrs.items() if not str(k).startswith("_")}
                        vals["layer"] = name
                        rows.append((_shape(*attrs["_geom"]), {c: _value(c, vals.get(c)) for c in cols}))
                    gpkg.replace_rows(con, table, cols, rows, srs[0])
                con.execute("COMMIT")
            except BaseException:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
            finally:
                con.close()
            for table, feats in todo.items():
                self._signatures[table] = self._signature(feats)
        return {t: self.path for t in LIVE_TABLES}

    def counts(self) -> dict[str, int]:
        """Number of features per table (for checks); zeros before the first write."""
        from . import gpkg

        if not gpkg.is_geopackage(self.path):
            return {t: 0 for t in LIVE_TABLES}
        con = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        try:
            names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            return {t: con.execute(f"SELECT COUNT(*) FROM {gpkg.quote(t)}").fetchone()[0] if t in names else 0
                    for t in LIVE_TABLES}
        finally:
            con.close()


def export_layers(folder: str | Path, layers: dict[str, dict], crs: str) -> dict[str, Path]:
    """Write the live GeoPackage from the layers of the raster view (see :meth:`LiveLink.write`);
    returns ``{table: path of the GeoPackage}``."""
    return LiveLink(folder, crs).write(layers, force=True)


_BUILD_SCRIPT = r'''
import json, sys
from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsPalLayerSettings, QgsProject,
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
    lyr = QgsVectorLayer(v["path"] + "|layername=" + v["table"], v["name"], "ogr")
    if crs.isValid():
        lyr.setCrs(crs)
    sym = lyr.renderer().symbol() if lyr.renderer() is not None else None
    if sym is not None:
        col = QColor(v["color"])
        if v["geometry"] == "POLYGON":
            fill = QColor(col); fill.setAlpha(70)
            sym.setColor(fill)
            sym.symbolLayer(0).setStrokeColor(col)
            sym.symbolLayer(0).setStrokeWidth(0.5)
        else:
            sym.setColor(col)
            if v["geometry"] == "LINESTRING":
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
    lyr.setAutoRefreshEnabled(True)  # redraw: a GeoPackage is read again at every drawing
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

STYLES = {  # table -> (colour, label field, label colour)
    "unita": ("#e6194b", "etichetta", "#ffffff"),
    "sezioni": ("#ffd000", "etichetta", "#7a5c00"),
    "curve": ("#4a3b2a", "quota", "#4a3b2a"),
    "linee": ("#00a0ff", "etichetta", "#003a66"),
    "punti": ("#27ae60", "etichetta", "#14532d"),
    "quote": ("#f59e0b", "quota_q", "#7c2d12"),
}


def build_project(project_path: str | Path, crs: str, layer_files: dict[str, Path], rasters: list[dict],
                  title: str = "pyarchinit-photogrammetry", refresh_ms: int = 1000,
                  qgis: QgisInstall | None = None) -> dict:
    """Write a QGIS project with the live drawings (``{table: GeoPackage}`` from
    :func:`export_layers`, redrawn every ``refresh_ms``) and the rasters
    (``[{"name", "path", "hillshade"?, "opacity"?}]``). Needs QGIS's Python; returns the
    report of the layers QGIS loaded."""
    qgis = qgis or find_qgis()
    if qgis is None or not qgis.python:
        raise RuntimeError("QGIS non trovato: installalo (qgis.org) o indica il percorso con PPG_QGIS")
    vectors = []
    for table, path in layer_files.items():
        title_v, geometry = LIVE_TABLES[table]
        color, label, label_color = STYLES[table]
        vectors.append({"name": title_v, "path": str(path), "table": table, "geometry": geometry, "color": color,
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
    _legacy_auto_refresh(Path(project_path))
    return report


def _legacy_auto_refresh(project: Path) -> None:
    """Write the automatic redraw of the layers in the pre-3.34 form (``autoRefreshEnabled``).

    QGIS 3.34-3.42 (at least) does not start the refresh timer of a layer read from a project
    saved with ``autoRefreshMode``: the mode is set while the interval is still 0 (the style
    is read first) and set again unchanged afterwards, so nothing redraws the layer and the
    map shows the cached image until *Refresh*. With the old attribute only the interval is
    read first, then the mode, and the timer starts. (A project saved again by those QGIS
    versions loses it: open it from here.)"""
    if project.suffix.lower() != ".qgs":
        return
    text = project.read_text(encoding="utf-8")
    text = re.sub(r'autoRefreshMode="(?:RedrawOnly|ReloadData)"', 'autoRefreshEnabled="1"', text)
    project.write_text(text, encoding="utf-8")


def open_in_qgis(project_path: str | Path, qgis: QgisInstall | None = None) -> None:
    qgis = qgis or find_qgis()
    if qgis is None:
        raise RuntimeError("QGIS non trovato")
    if sys.platform == "darwin":
        subprocess.Popen(["/usr/bin/open", "-a", qgis.app, str(project_path)], close_fds=False)  # posix_spawn
    else:
        subprocess.Popen([qgis.app, "--project", str(project_path)], close_fds=False)


__all__ = ["find_qgis", "export_layers", "build_project", "open_in_qgis", "classify_layer", "LiveLink", "LIVE_DB",
           "LIVE_TABLES", "QgisInstall"]
