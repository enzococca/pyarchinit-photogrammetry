"""The styles of the layers of the QGIS projects of the program, and those made by hand in QGIS, kept.

*Apri in QGIS* used to write its project again every time, with the program's colours: the symbology, the labels
and the opacity the user had set in QGIS were lost. Now:

* a project that already shows every layer the program wants is **left alone** - QGIS opens it as the user saved
  it; a project that misses layers, or shows layers the program no longer has, is **updated in place** by QGIS's
  own Python (:data:`..io.qgis_link._BUILD_SCRIPT`, mode ``update``): the missing layers added, the program's
  layers no longer wanted removed, the rasters pointed at their file again; every other layer, its style, the
  groups, the layouts and the map stay as they are. Only a missing or unreadable project is written from scratch;
* before the project is touched the styles of the program's layers are read from it (:func:`harvest`): a layer
  whose style differs from the one the program last wrote (the record :func:`record_project` keeps beside the
  project) was styled by hand, and its style is kept as a ``.qml`` file in the folder ``<project>_stili``
  (:func:`style_dir`). A layer written again from scratch takes that style instead of the program's
  (:func:`qml_for`); *Ripristina gli stili predefiniti* (:func:`reset`) forgets them and gives the program's
  layers their default style again;
* the live GeoPackage holds in its table ``layer_styles`` - where QGIS looks for the default style of a layer of a
  GeoPackage, as *Salva come predefinito* writes it - the program's default style of each table, written once
  (:func:`ensure_default_styles`); a default the user saved there is never overwritten, and the program's projects
  do not paint over it (:func:`user_default_tables`).

The styles are compared and copied as XML, without QGIS: the ``<maplayer>`` of a ``.qgs`` holds the same elements as
a ``.qml`` file (renderer, labels, opacity, blending, field configuration...).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from xml.etree import ElementTree

OWNER = "ppg"  # the owner of the rows the program writes in layer_styles
STYLE_NAME = "ppg (predefinito)"
# children of a <maplayer> that say what and where the layer is, not how it looks
NOT_STYLE = {"id", "datasource", "keywordList", "layername", "srs", "resourceMetadata", "provider", "extent",
             "wgs84extent", "noData", "flags", "temporal", "elevation", "map-layer-style-manager", "metadataUrls",
             "dataUrl", "attribution", "legendUrl", "auxiliaryLayer", "shortname", "title", "abstract",
             "verticalCrs", "profileLineSymbol", "profileFillSymbol", "profileMarkerSymbol", "expressionfields"}
# attributes of a <maplayer> that are not style (the others - labelsEnabled, scales, simplification - are)
NOT_STYLE_ATTRS = {"type", "geometry", "wkbType", "autoRefreshTime", "autoRefreshEnabled", "autoRefreshMode",
                   "refreshOnNotifyEnabled", "refreshOnNotifyMessage", "readOnly", "styleCategories",
                   "legendPlaceholderImage"}


# ------------------------------------------------------------------------------------------ keys of the layers
def vector_key(path, table: str, subset: str | None = None) -> str:
    """The identity of a vector layer of the program: its GeoPackage (the file name: a project moved with its folder
    keeps it), its table and its filter."""
    return f"vector|{Path(str(path).replace(chr(92), '/')).name}|{table}|{subset or ''}"


def raster_key(name: str) -> str:
    """The identity of a raster layer of the program: its name (the shaded and the plain DEM share the file)."""
    return f"raster|{name}"


def layer_key(maplayer) -> str | None:
    """:func:`vector_key` or :func:`raster_key` of a ``<maplayer>`` of a ``.qgs``; None for a layer of another
    provider."""
    kind = maplayer.get("type")
    if kind == "raster":
        name = maplayer.findtext("layername")
        return raster_key(name) if name else None
    if kind != "vector" or (maplayer.findtext("provider") or "").strip() != "ogr":
        return None
    source, _, subset = (maplayer.findtext("datasource") or "").partition("|subset=")
    file, _, rest = source.partition("|layername=")
    table = rest.partition("|")[0]
    return vector_key(file, table, subset) if table else None


def wanted_keys(vectors: list[dict], rasters: list[dict]) -> dict[str, dict]:
    """``{key: layer}`` of the layers a project of the program shows (as ``build_project`` gives them to QGIS)."""
    out = {}
    for v in vectors:
        out[vector_key(v["path"], v["table"], v.get("subset"))] = v
    for r in rasters:
        out[raster_key(r["name"])] = r
    return out


# ------------------------------------------------------------------------------------------ styles in a project
def _maplayers(project: Path) -> list | None:
    try:
        root = ElementTree.parse(str(project)).getroot()
    except (OSError, ElementTree.ParseError):
        return None
    return [m for m in root.iter("maplayer")]


def _style_parts(maplayer) -> tuple[dict, list]:
    attrs = {k: v for k, v in maplayer.attrib.items() if k not in NOT_STYLE_ATTRS}
    return attrs, [c for c in maplayer if c.tag not in NOT_STYLE]


def style_hash(maplayer) -> str:
    """A digest of how a ``<maplayer>`` looks (:data:`NOT_STYLE` left out)."""
    attrs, children = _style_parts(maplayer)
    h = hashlib.sha1(json.dumps(sorted(attrs.items())).encode())
    for c in children:
        h.update(ElementTree.tostring(c, encoding="utf-8"))
    return h.hexdigest()


def style_qml(maplayer, version: str = "") -> str:
    """The ``.qml`` document of the style of a ``<maplayer>``: what QGIS's *Carica stile* reads."""
    attrs, children = _style_parts(maplayer)
    root = ElementTree.Element("qgis", {**attrs, "version": version or "3.34", "styleCategories": "AllStyleCategories"})
    root.extend(children)
    return "<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>\n" + ElementTree.tostring(root, encoding="unicode")


def project_layer_keys(project) -> dict[str, object] | None:
    """``{key: <maplayer>}`` of the layers of the program's kind in a ``.qgs``; None when it cannot be read."""
    layers = _maplayers(Path(project))
    if layers is None:
        return None
    out = {}
    for m in layers:
        key = layer_key(m)
        if key is not None and key not in out:
            out[key] = m
    return out


def style_dir(project) -> Path:
    project = Path(project)
    return project.with_name(f"{project.stem}_stili")


def _record_path(project) -> Path:
    return style_dir(project) / "stili.json"


def _read_record(project) -> dict:
    try:
        return json.loads(_record_path(project).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_record(project, record: dict) -> None:
    path = _record_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _qml_name(key: str) -> str:
    return hashlib.sha1(key.encode()).hexdigest()[:16] + ".qml"


def ours(project) -> set[str]:
    """The keys of the layers the program put in the project (those it may add, update and remove)."""
    return set((_read_record(project).get("layers") or {}))


def record_project(project, keys) -> None:
    """After the program wrote ``project``: the style each of its layers ``keys`` has there now, so that a later
    change made in QGIS is told apart (:func:`harvest`)."""
    layers = project_layer_keys(project) or {}
    record = _read_record(project)
    rec = record.setdefault("layers", {})
    for key in keys:
        if key in layers:
            rec.setdefault(key, {})["hash"] = style_hash(layers[key])
            rec[key]["name"] = layers[key].findtext("layername") or ""
    _write_record(project, record)


def harvest(project) -> list[str]:
    """The styles of the program's layers changed in QGIS since the program last wrote ``project``, kept as ``.qml``
    files (one per layer, the last one kept). Layers the record does not know - a project written before 1.9.1, the
    user's own layers - are not looked at. Returns the keys of the styles kept now."""
    project = Path(project)
    layers = project_layer_keys(project)
    record = _read_record(project)
    rec = record.get("layers") or {}
    if layers is None or not rec:
        return []
    root = None
    try:
        root = ElementTree.parse(str(project)).getroot()
    except (OSError, ElementTree.ParseError):
        pass
    version = root.get("version", "") if root is not None else ""
    kept = []
    for key, m in layers.items():
        if key not in rec:
            continue
        h = style_hash(m)
        if h == rec[key].get("hash"):
            continue
        folder = style_dir(project)
        folder.mkdir(parents=True, exist_ok=True)
        name = _qml_name(key)
        (folder / name).write_text(style_qml(m, version), encoding="utf-8")
        rec[key].update(hash=h, qml=name, name=m.findtext("layername") or rec[key].get("name", ""))
        kept.append(key)
    if kept:
        _write_record(project, record)
    return kept


def qml_for(project, key: str) -> Path | None:
    """The style kept for the layer ``key`` of ``project`` (made by hand in QGIS), None for the program's."""
    entry = (_read_record(project).get("layers") or {}).get(key) or {}
    if not entry.get("qml"):
        return None
    path = style_dir(project) / entry["qml"]
    return path if path.exists() else None


def kept_styles(project) -> list[str]:
    """The names of the layers whose hand-made style is kept for ``project``."""
    return sorted(e.get("name") or k for k, e in (_read_record(project).get("layers") or {}).items() if e.get("qml"))


def reset(project) -> int:
    """The hand-made styles kept for ``project`` forgotten (*Ripristina gli stili predefiniti*): its layers take the
    program's style when it is written next. Returns how many were kept."""
    record = _read_record(project)
    rec = record.get("layers") or {}
    n = 0
    for entry in rec.values():
        if entry.pop("qml", None):
            n += 1
        entry.pop("hash", None)
    for qml in style_dir(project).glob("*.qml"):
        qml.unlink(missing_ok=True)
    if rec:
        _write_record(project, record)
    return n


# ------------------------------------------------------------------------------------------ layer_styles of a GeoPackage
_LAYER_STYLES = """CREATE TABLE IF NOT EXISTS layer_styles (id INTEGER PRIMARY KEY AUTOINCREMENT,
  f_table_catalog TEXT(256), f_table_schema TEXT(256), f_table_name TEXT(256), f_geometry_column TEXT(256),
  styleName TEXT(30), styleQML TEXT, styleSLD TEXT, useAsDefault BOOLEAN, description TEXT, owner TEXT(30),
  ui TEXT(30), update_time DATETIME DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))"""


def _symbol(geometry: str, colour: str) -> str:
    r, g, b = (int(colour[i:i + 2], 16) for i in (1, 3, 5))
    if geometry == "POLYGON":
        return (f'<symbol type="fill" name="0" alpha="1" clip_to_extent="1" force_rhr="0">'
                f'<layer class="SimpleFill" enabled="1" locked="0" pass="0">'
                f'<Option type="Map"><Option name="color" type="QString" value="{r},{g},{b},70"/>'
                f'<Option name="outline_color" type="QString" value="{r},{g},{b},255"/>'
                f'<Option name="outline_width" type="QString" value="0.5"/>'
                f'<Option name="style" type="QString" value="solid"/></Option></layer></symbol>')
    if geometry == "LINESTRING":
        return (f'<symbol type="line" name="0" alpha="1" clip_to_extent="1" force_rhr="0">'
                f'<layer class="SimpleLine" enabled="1" locked="0" pass="0">'
                f'<Option type="Map"><Option name="line_color" type="QString" value="{r},{g},{b},255"/>'
                f'<Option name="line_width" type="QString" value="0.5"/></Option></layer></symbol>')
    return (f'<symbol type="marker" name="0" alpha="1" clip_to_extent="1" force_rhr="0">'
            f'<layer class="SimpleMarker" enabled="1" locked="0" pass="0">'
            f'<Option type="Map"><Option name="color" type="QString" value="{r},{g},{b},255"/>'
            f'<Option name="size" type="QString" value="2.5"/><Option name="name" type="QString" value="circle"/>'
            f'</Option></layer></symbol>')


def default_qml(geometry: str, colour: str, label: str | None = None) -> str:
    """The program's default style of a table (single symbol of ``colour``, labels from ``label``) as ``.qml``."""
    labels = ""
    if label:
        labels = (f'<labeling type="simple"><settings calloutType="simple"><text-style fieldName="{label}" '
                  f'fontSize="9" textColor="32,32,32,255" isExpression="0"/></settings></labeling>')
    return ("<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>\n"
            f'<qgis version="3.34" styleCategories="Symbology|Labeling" labelsEnabled="{1 if label else 0}">'
            f'<renderer-v2 type="singleSymbol" symbollevels="0" enableorderby="0" forceraster="0">'
            f'<symbols>{_symbol(geometry, colour)}</symbols></renderer-v2>{labels}</qgis>')


def ensure_default_styles(con: sqlite3.Connection, tables: dict[str, tuple[str, str, str | None]]) -> list[str]:
    """The program's default style of each of ``tables`` (``{table: (geometry, colour, label field)}``) into
    ``layer_styles``, once: a table that has a default style there already - the program's, or one saved in QGIS
    with *Salva come predefinito* - is left as it is, and so is a table whose style the program wrote once (the user
    may have deleted it on purpose: ``ppg_info`` "style:<table>"). Call inside a transaction; returns the tables
    written."""
    present = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    con.execute(_LAYER_STYLES)
    if "layer_styles" not in present or not con.execute(
            "SELECT 1 FROM gpkg_contents WHERE table_name = 'layer_styles'").fetchone():
        con.execute("INSERT OR IGNORE INTO gpkg_contents (table_name, data_type, identifier, description) "
                    "VALUES ('layer_styles', 'attributes', 'layer_styles', '')")
    con.execute("CREATE TABLE IF NOT EXISTS ppg_info (name TEXT PRIMARY KEY, value TEXT)")
    done = {r[0] for r in con.execute("SELECT name FROM ppg_info WHERE name LIKE 'style:%'")}
    written = []
    for table, (geometry, colour, label) in tables.items():
        if f"style:{table}" in done:
            continue
        has_default = con.execute("SELECT 1 FROM layer_styles WHERE f_table_name = ? AND useAsDefault",
                                  (table,)).fetchone()
        if not has_default:
            con.execute("INSERT INTO layer_styles (f_table_catalog, f_table_schema, f_table_name, f_geometry_column, "
                        "styleName, styleQML, styleSLD, useAsDefault, description, owner, ui) "
                        "VALUES ('', '', ?, 'geom', ?, ?, '', 1, ?, ?, '')",
                        (table, STYLE_NAME, default_qml(geometry, colour, label),
                         "pyarchinit-photogrammetry", OWNER))
            written.append(table)
        con.execute("INSERT OR REPLACE INTO ppg_info (name, value) VALUES (?, '1')", (f"style:{table}",))
    return written


def user_default_tables(path) -> set[str]:
    """The tables of the GeoPackage ``path`` whose default style in ``layer_styles`` was saved in QGIS (not the
    program's): QGIS loads it for a new layer, and the program does not paint over it."""
    try:
        con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    except sqlite3.Error:
        return set()
    try:
        return {r[0] for r in con.execute("SELECT f_table_name FROM layer_styles WHERE useAsDefault AND "
                                          "(owner IS NULL OR owner != ?)", (OWNER,))}
    except sqlite3.Error:
        return set()
    finally:
        con.close()


__all__ = ["vector_key", "raster_key", "layer_key", "wanted_keys", "style_hash", "style_qml", "project_layer_keys",
           "style_dir", "ours", "record_project", "harvest", "qml_for", "kept_styles", "reset", "default_qml",
           "ensure_default_styles", "user_default_tables", "OWNER"]
