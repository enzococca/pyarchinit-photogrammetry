"""QField package: the orthomosaic, the DEM and the drawings of a chunk on a tablet in the trench,
and the edits made there brought back.

* :func:`export_package` - a folder for `QField <https://qfield.org>`_ (copy it to the device or
  to QFieldCloud): ``ortofoto.tif`` (Cloud Optimized GeoTIFF, JPEG, at most ``resolution``
  metres per pixel: a drone orthomosaic would not fit a tablet), ``dem.tif`` for the hillshade and
  the heights, ``rilievo.gpkg`` with the editable layers ``unita`` (the units drawn on the chunk:
  sito, area, us, tipo and the columns of ``pyunitastratigrafiche``) and ``quote`` (spot heights
  with the columns of ``pyarchinit_quote``), and ``<name>.qgs`` made with QGIS's Python (relative
  paths, styles, labels). Every feature carries ``ppg_id``; ``ppg_qfield.json`` keeps what was
  exported.
* :func:`read_edits` / :func:`import_edits` - the GeoPackage back from the tablet compared with
  that record: units and spot heights added, changed (outline or attributes) and deleted, applied
  to the drawings of the chunk (``meta["drawings"]``). They reach PyArchInit with the usual
  publishing (*Pubblica in PyArchInit*, *Salva modifiche in PyArchInit*).
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

from ..core.progress import Progress, as_progress

GPKG = "rilievo.gpkg"
RECORD = "ppg_qfield.json"
UNIT_COLUMNS = {"ppg_id": "TEXT", "sito": "TEXT", "area": "TEXT", "us": "TEXT", "tipo": "TEXT",
                "stratigraph_index_us": "INTEGER", "tipo_us_s": "TEXT", "rilievo_originale": "TEXT",
                "disegnatore": "TEXT", "data": "TEXT", "etichetta": "TEXT"}
QUOTE_COLUMNS = {"ppg_id": "TEXT", "sito_q": "TEXT", "area_q": "TEXT", "us_q": "TEXT", "unita_tipo_q": "TEXT",
                 "quota_q": "REAL", "unita_misu_q": "TEXT", "data": "TEXT", "disegnatore": "TEXT",
                 "rilievo_originale": "TEXT"}


def _drawings(chunk) -> dict:
    d = chunk.meta.setdefault("drawings", {})
    d.setdefault("units", [])
    d.setdefault("quote", [])
    return d


def _ids(chunk) -> None:
    """A ``ppg_id`` for every unit and spot height of the chunk that has none (kept with them)."""
    d = _drawings(chunk)
    for u in d["units"]:
        u.setdefault("attributes", {}).setdefault("ppg_id", uuid.uuid4().hex)
    for q in d["quote"]:
        q.setdefault("ppg_id", uuid.uuid4().hex)


def _unit_row(u: dict) -> dict:
    a = u.get("attributes") or {}
    row = {"ppg_id": a.get("ppg_id"), "sito": u.get("sito"), "area": u.get("area"), "us": u.get("us"),
           "tipo": u.get("tipo") or "US", "etichetta": f"{u.get('tipo') or 'US'} {u.get('us') or '?'}"}
    for k in ("stratigraph_index_us", "tipo_us_s", "rilievo_originale", "disegnatore", "data"):
        row[k] = a.get(k)
    return row


def _clean(v):
    if v is None:
        return None
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
        return v
    s = str(v).strip() if isinstance(v, str) else v
    return s if s != "" else None


def _downsample(src: Path, dst: Path, resolution: float) -> Path:
    """``src`` resampled (average) to ``resolution`` metres per pixel when it is finer."""
    import rasterio
    import rasterio.shutil
    from affine import Affine
    from rasterio.enums import Resampling
    from rasterio.vrt import WarpedVRT

    with rasterio.open(src) as s:
        f = resolution / abs(s.res[0])
        if f <= 1.01:
            return src
        w, h = max(1, int(round(s.width / f))), max(1, int(round(s.height / f)))
        tr = s.transform @ Affine.scale(s.width / w, s.height / h)
        with WarpedVRT(s, transform=tr, width=w, height=h, resampling=Resampling.average,
                       crs=s.crs or None) as vrt:
            rasterio.shutil.copy(vrt, dst, driver="GTiff", tiled=True, compress="deflate", BIGTIFF="IF_SAFER")
    return dst


_PROJECT_SCRIPT = r'''
import json, sys
from qgis.core import (Qgis, QgsApplication, QgsCoordinateReferenceSystem, QgsEditorWidgetSetup, QgsHillshadeRenderer,
                       QgsPalLayerSettings, QgsProject, QgsRasterLayer, QgsTextFormat, QgsVectorLayer,
                       QgsVectorLayerSimpleLabeling)
from qgis.PyQt.QtGui import QColor
cfg = json.load(open(sys.argv[1], encoding="utf-8"))
app = QgsApplication([], False)
app.initQgis()
proj = QgsProject.instance()
crs = QgsCoordinateReferenceSystem(cfg["crs"]) if cfg.get("crs") else QgsCoordinateReferenceSystem()
if crs.isValid():
    proj.setCrs(crs)
proj.setTitle(cfg["title"])
try:
    proj.setFilePathStorage(Qgis.FilePathType.Relative)
except AttributeError:  # older QGIS
    proj.writeEntry("Paths", "/Absolute", False)
root = proj.layerTreeRoot()
report = {"vectors": [], "rasters": []}
for v in cfg["vectors"]:
    lyr = QgsVectorLayer(v["path"] + "|layername=" + v["table"], v["name"], "ogr")
    sym = lyr.renderer().symbol() if lyr.renderer() is not None else None
    if sym is not None:
        col = QColor(v["color"])
        if v["geometry"] == "POLYGON":
            fill = QColor(col); fill.setAlpha(60)
            sym.setColor(fill)
            sym.symbolLayer(0).setStrokeColor(col)
            sym.symbolLayer(0).setStrokeWidth(0.6)
        else:
            sym.setColor(col)
            sym.setSize(3)
    pal = QgsPalLayerSettings()
    pal.fieldName = v["label"]
    pal.enabled = True
    fmt = QgsTextFormat(); fmt.setSize(10); fmt.setColor(QColor(v["color"]))
    pal.setFormat(fmt)
    lyr.setLabeling(QgsVectorLayerSimpleLabeling(pal))
    lyr.setLabelsEnabled(True)
    idx = lyr.fields().indexOf("ppg_id")
    if idx >= 0:  # the identity of the feature: never edited on the tablet
        lyr.setEditorWidgetSetup(idx, QgsEditorWidgetSetup("Hidden", {}))
    for field, values in v.get("value_maps", {}).items():
        i = lyr.fields().indexOf(field)
        if i >= 0:
            lyr.setEditorWidgetSetup(i, QgsEditorWidgetSetup("ValueMap", {"map": [{x: x} for x in values]}))
    proj.addMapLayer(lyr, False)
    root.addLayer(lyr)
    report["vectors"].append({"name": v["name"], "valid": lyr.isValid(), "features": lyr.featureCount()})
for r in cfg["rasters"]:
    lyr = QgsRasterLayer(r["path"], r["name"])
    if r.get("hillshade") and lyr.isValid():
        lyr.setRenderer(QgsHillshadeRenderer(lyr.dataProvider(), 1, 315, 45))
        lyr.setOpacity(0.5)
    proj.addMapLayer(lyr, False)
    node = root.addLayer(lyr)
    if r.get("hidden"):
        node.setItemVisibilityChecked(False)
    report["rasters"].append({"name": r["name"], "valid": lyr.isValid()})
report["written"] = bool(proj.write(cfg["project"]))
print("PPG_QGIS_REPORT " + json.dumps(report), flush=True)
app.exitQgis()
'''


def export_package(chunk, folder: str | os.PathLike, resolution: float | None = 0.02, title: str | None = None,
                   project: bool = True, qgis=None, progress: Progress | None = None) -> dict:
    """The QField package of ``chunk`` in ``folder`` (see the module docstring). Returns
    ``{"folder", "project" (or None), "units", "quote", "ortho", "dem"}``."""
    from shapely import wkt
    from shapely.geometry import Point

    from ..core import crs as crs_mod
    from . import gpkg
    from .web import export_cog

    progress = as_progress(progress)
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    _ids(chunk)
    d = _drawings(chunk)
    crs = None if crs_mod.is_local(chunk.crs) else chunk.crs
    gp = folder / GPKG
    if gp.exists():
        gp.unlink()
    progress(0.05, "QField: layers")
    units = [(wkt.loads(u["wkt"]), _unit_row(u)) for u in d["units"]]
    gpkg.write_table(gp, "unita", "POLYGON", UNIT_COLUMNS,
                     [(g if g.geom_type == "Polygon" else max(g.geoms, key=lambda p: p.area), r) for g, r in units],
                     crs, identifier="Unita' stratigrafiche")
    quote = [(Point(float(q["x"]), float(q["y"])), {k: q.get(k) for k in QUOTE_COLUMNS}) for q in d["quote"]]
    gpkg.write_table(gp, "quote", "POINT", QUOTE_COLUMNS, quote, crs, identifier="Quote")
    out = {"folder": folder, "project": None, "units": len(units), "quote": len(quote), "ortho": None, "dem": None}
    rasters = []
    with tempfile.TemporaryDirectory(prefix="ppg-qfield-", dir=folder) as tmp:
        if chunk.orthomosaic_path is not None and chunk.orthomosaic_path.exists():
            progress(0.15, "QField: orthomosaic")
            src = _downsample(chunk.orthomosaic_path, Path(tmp) / "ortho.tif", resolution) if resolution else \
                chunk.orthomosaic_path
            out["ortho"] = export_cog(src, folder / "ortofoto.tif", compression="jpeg")
            rasters.append({"name": "Ortofoto", "path": str(out["ortho"])})
        if chunk.dem_path is not None and chunk.dem_path.exists():
            progress(0.6, "QField: DEM")
            src = _downsample(chunk.dem_path, Path(tmp) / "dem.tif", resolution * 2.5) if resolution else \
                chunk.dem_path
            out["dem"] = export_cog(src, folder / "dem.tif", compression="deflate")
            rasters.append({"name": "DEM (ombreggiato)", "path": str(out["dem"]), "hillshade": True})
    record = {"chunk": chunk.key, "label": chunk.label, "crs": chunk.crs,
              "exported": datetime.now().isoformat(timespec="seconds"),
              "units": {r["ppg_id"]: {"wkt": g.wkt, **{k: _clean(v) for k, v in r.items()}} for g, r in units},
              "quote": {q["ppg_id"]: {"x": float(q["x"]), "y": float(q["y"]),
                                      **{k: _clean(q.get(k)) for k in QUOTE_COLUMNS}} for q in d["quote"]}}
    (folder / RECORD).write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    if project:
        progress(0.85, "QField: QGIS project")
        name = "".join(c if c.isalnum() or c in "-_" else "_" for c in (title or chunk.label)) or "rilievo"
        out["project"] = _write_project(folder, name, crs, rasters, title or chunk.label, qgis)
    progress(1.0, f"QField: {len(units)} units, {len(quote)} spot heights")
    return out


def _write_project(folder: Path, name: str, crs: str | None, rasters: list[dict], title: str, qgis=None) -> Path:
    from ..archaeo.stratigraphy import US_TYPES
    from .qgis_link import find_qgis

    qgis = qgis or find_qgis()
    if qgis is None or not qgis.python:
        raise RuntimeError("QGIS not found: it writes the QField project (install it from qgis.org or give its path "
                           "with PPG_QGIS)")
    project = folder / f"{name}.qgs"
    vectors = [{"name": "Unita' stratigrafiche", "path": str(folder / GPKG), "table": "unita", "geometry": "POLYGON",
                "color": "#e6194b", "label": "etichetta", "value_maps": {"tipo": list(US_TYPES)}},
               {"name": "Quote", "path": str(folder / GPKG), "table": "quote", "geometry": "POINT",
                "color": "#f59e0b", "label": "quota_q", "value_maps": {"unita_tipo_q": list(US_TYPES)}}]
    cfg = {"project": str(project), "crs": crs if str(crs or "").upper().startswith("EPSG:") else "",
           "title": title, "vectors": vectors, "rasters": rasters}
    with tempfile.TemporaryDirectory(prefix="ppg-qgis-") as tmp:
        script, conf = Path(tmp) / "build.py", Path(tmp) / "cfg.json"
        script.write_text(_PROJECT_SCRIPT, encoding="utf-8")
        conf.write_text(json.dumps(cfg), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "PROJ_", "GDAL_", "QT_"))}
        env.update(qgis.env)
        r = subprocess.run([qgis.python, str(script), str(conf)], capture_output=True, text=True, env=env,
                           timeout=300, close_fds=False)
    line = next((ln for ln in r.stdout.splitlines() if ln.startswith("PPG_QGIS_REPORT ")), None)
    if line is None or not json.loads(line.split(" ", 1)[1]).get("written"):
        raise RuntimeError("the QField project was not written:\n" + (r.stderr or r.stdout)[-1500:])
    return project


def _same(a, b) -> bool:
    a, b = _clean(a), _clean(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-9
    return a == b or (a is not None and b is not None and str(a) == str(b))


def read_edits(folder: str | os.PathLike) -> dict:
    """What changed in the package since it was exported: ``{"units": {"added", "changed",
    "deleted"}, "quote": {...}}`` (``added``/``changed``: lists of ``(ppg_id, geometry,
    attributes)``; ``deleted``: ids)."""
    from shapely import wkt

    from . import gpkg

    folder = Path(folder)
    record = json.loads((folder / RECORD).read_text(encoding="utf-8"))
    out = {}
    for table, key in (("unita", "units"), ("quote", "quote")):
        before = record.get(key, {})
        added, changed, seen = [], [], set()
        for geom, values in gpkg.read_table(folder / GPKG, table):
            values.pop("fid", None)
            if geom is None or geom.is_empty:
                continue
            pid = values.get("ppg_id")
            if not pid or pid not in before or pid in seen:  # new on the tablet (or a copy of a feature)
                pid = uuid.uuid4().hex
                values["ppg_id"] = pid
                added.append((pid, geom, values))
                seen.add(pid)
                continue
            seen.add(pid)
            old = before[pid]
            old_geom = wkt.loads(old["wkt"]) if "wkt" in old else None
            if old_geom is None:  # a point: x and y
                from shapely.geometry import Point

                old_geom = Point(old["x"], old["y"])
            moved = not geom.equals_exact(old_geom, 1e-6)
            edited = any(not _same(values.get(k), old.get(k)) for k in values if k not in ("ppg_id", "etichetta"))
            if moved or edited:
                changed.append((pid, geom, values))
        out[key] = {"added": added, "changed": changed, "deleted": [pid for pid in before if pid not in seen]}
    return out


def import_edits(chunk, folder: str | os.PathLike) -> dict:
    """The edits of :func:`read_edits` applied to the drawings of ``chunk`` (the package must
    come from that chunk). Returns the counts ``{"units": {"added", "changed", "deleted"},
    "quote": {...}}``."""
    folder = Path(folder)
    record = json.loads((folder / RECORD).read_text(encoding="utf-8"))
    if record.get("chunk") != chunk.key or record.get("label") != chunk.label:
        raise ValueError(f"the package was made from the chunk «{record.get('label')}», not «{chunk.label}»")
    edits = read_edits(folder)
    d = _drawings(chunk)
    units = {(u.get("attributes") or {}).get("ppg_id"): u for u in d["units"]}
    for pid in edits["units"]["deleted"]:
        units.pop(pid, None)
    for pid, geom, v in edits["units"]["added"] + edits["units"]["changed"]:
        u = units.get(pid) or {"attributes": {"ppg_id": pid}}
        u.update({"wkt": geom.wkt, "sito": _clean(v.get("sito")), "area": _clean(v.get("area")),
                  "us": _clean(v.get("us")), "tipo": _clean(v.get("tipo")) or "US"})
        attrs = u.setdefault("attributes", {})
        for k in ("stratigraph_index_us", "tipo_us_s", "rilievo_originale", "disegnatore", "data"):
            if _clean(v.get(k)) is not None:
                attrs[k] = v.get(k)
        attrs["ppg_id"] = pid
        units[pid] = u
    d["units"] = list(units.values())
    quote = {q.get("ppg_id"): q for q in d["quote"]}
    for pid in edits["quote"]["deleted"]:
        quote.pop(pid, None)
    for pid, geom, v in edits["quote"]["added"] + edits["quote"]["changed"]:
        q = quote.get(pid) or {"ppg_id": pid}
        q.update({k: v.get(k) for k in QUOTE_COLUMNS if k in v})
        q.update({"x": float(geom.x), "y": float(geom.y), "ppg_id": pid})
        quote[pid] = q
    d["quote"] = list(quote.values())
    return {k: {n: len(v[n]) for n in ("added", "changed", "deleted")} for k, v in edits.items()}
