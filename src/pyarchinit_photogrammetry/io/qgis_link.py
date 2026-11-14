"""Live link with QGIS: the orthomosaic, the DEM and everything drawn in the interface
(stratigraphic units, sections, contour lines, points) open in a QGIS project, the
drawings rewritten on every change while QGIS redraws them every second, and what is
edited in QGIS read back.

The drawings live in one GeoPackage (:data:`LIVE_DB`, a table per kind of drawing), updated
in place in one transaction. QGIS reads a GeoPackage again at every redraw, and the project
asks QGIS to redraw those layers every second, so what is drawn here appears in QGIS without
pressing anything. Up to 0.4.1 the drawings were GeoJSON files with "reload data every
second": QGIS keeps GeoJSON in memory, and QGIS 3.34-3.42 does not start the refresh timer of
layers read from a project saved with ``autoRefreshMode``, so the map kept its cached image
until *Refresh* (see :func:`_legacy_auto_refresh`).

Every feature has an identity, :data:`ID_FIELD` (a UUID the program keeps with its unit, spot
height or section), and the rows are updated, inserted and deleted one by one by that identity:
a row keeps its ``fid`` while its feature lives, so an edit session of QGIS keeps its rows.
:data:`SIG_FIELD` holds the signature of what the program last wrote in the row (its geometry
and attributes): a row whose content no longer matches it was edited by another program. The
signature lives in the file, so it also tells the edits made in QGIS while the program was
closed. Of the tables in :data:`TWO_WAY_TABLES` (units, spot heights, sections) such edits come
back (:meth:`LiveLink.read_changes`): rows changed, rows drawn in QGIS (no identity yet: they
get one; or under an identity never known here and never signed by the program, which they keep)
and rows deleted; the program never overwrites them before it has read them. The same holds for
the free drawings in 3D (the three tables ``disegni_3d_*`` of :mod:`.free_drawings`, with Z; since
1.7.8): :func:`two_way_tables`. The other tables are computed by the program (contour lines, zones,
plugin drawings, the drawings of the elevations) and stay one-way.

Only the drawings of the plan are in the file: while an elevation or a section is shown the window leaves the
GeoPackage as it is, and what is drawn there - in the (u, v) of the elevation - goes out with the elevation
(:func:`..engine.elevation.export_drawings`); edits read from QGIS meanwhile go to the plan.

* :class:`LiveLink` / :func:`export_layers` write the GeoPackage (no GDAL needed).
* :func:`build_project` creates the ``.qgs`` with QGIS's own Python (styles, labels, auto refresh).
* :func:`open_in_qgis` starts QGIS on the project.

QGIS is found in the usual places (``/Applications/QGIS*.app``, ``C:\\Program Files\\QGIS*``,
``/usr/bin/qgis``) or through ``PPG_QGIS``.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

LIVE_DB = "disegni_ppg.gpkg"
# fixed tables, so that the QGIS project never needs rebuilding while drawing
LIVE_TABLES = {  # table -> (title in QGIS, geometry type)
    "unita": ("Unita' stratigrafiche (ppg)", "POLYGON"),
    "poligoni": ("Poligoni (ppg)", "POLYGON"),  # zones, stones, patches, the polygons of plugins
    "sezioni": ("Sezioni (ppg)", "LINESTRING"),
    "curve": ("Curve di livello (ppg)", "LINESTRING"),
    "linee": ("Linee (ppg)", "LINESTRING"),
    "punti": ("Punti (ppg)", "POINT"),
    "quote": ("Quote (ppg)", "POINT"),  # the columns of pyarchinit_quote
}
TWO_WAY_TABLES = ("unita", "quote", "sezioni")  # what is edited there in QGIS comes back
# the other drawings of the plan: polygons, lines and points of the layers of the 2D view (those of the plugins, the
# computed ones). What another program edits there is read back too (since 1.7.8), row by row with the layer of the row:
# the window takes the edits of the layers whose features it can change and puts the others back as they were
PLAN_TABLES = ("poligoni", "curve", "linee", "punti")
# the drawings of every elevation and section of the chunk, placed on the survey in 3D (one-way): tables
# ``prospetto_<elevation id>_<unita|quote|punti|pietre|profilo|linee>`` (``engine.elevation.world_tables``)
ELEVATION_PREFIX = "prospetto_"
ELEVATION_TABLES = {"unita": ("Unità", "POLYGON", "#1b7f3b"), "quote": ("Quote", "POINT", "#ffd166"),
                    "punti": ("Punti di misura", "POINT", "#ff5d8f"), "pietre": ("Pietre", "POLYGON", "#5b3a1e"),
                    "profilo": ("Profilo", "LINESTRING", "#ff2d2d"),
                    "linee": ("Linee di rottura", "LINESTRING", "#ff7f0e")}
ID_FIELD = "ppg_id"  # the identity of a feature (a UUID)
SIG_FIELD = "ppg_sig"  # the signature of what the program last wrote in the row
PART_FIELD = "ppg_parte"  # "unita": the part (1, 2...) of a unit drawn as several polygons
TECHNICAL_FIELDS = (ID_FIELD, SIG_FIELD, PART_FIELD)
# name -> value of what the file is: "chunk", the key of the chunk whose drawings it holds (an edit made there belongs
# to that chunk, whichever is active when it is read); not a layer, not listed in gpkg_contents
INFO_TABLE = "ppg_info"
# (name, ppg_id, part, fid): the rows of the two-way tables as the program last wrote or read them, kept in the
# transaction of the write or of the read - what a reader without memory (another LiveLink, the program started
# again, a replaced file) compares the file with, so a row deleted in QGIS meanwhile is seen deleted and never written
# back; ``INFO_TABLE`` "rows:<table>" says the record of that table was made (an empty record is not a missing one).
# Not a layer, not listed in gpkg_contents.
ROWS_TABLE = "ppg_rows"


def two_way_tables() -> tuple[str, ...]:
    """Every table whose edits by another program come back: units, spot heights, sections (:data:`TWO_WAY_TABLES`),
    the other drawings of the plan (:data:`PLAN_TABLES`) and the three tables of the free drawings in 3D
    (:data:`.free_drawings.TABLES`)."""
    from .free_drawings import TABLES

    return (*TWO_WAY_TABLES, *PLAN_TABLES, *(table for table, _gtype in TABLES.values()))


class UnreadEdits(RuntimeError):
    """:meth:`LiveLink.write` of another chunk's drawings refused: the file holds edits another program made to the
    drawings of the chunk before, not read yet (they belong to that chunk): read them first."""
# the columns a two-way table always has, also before the program wrote a feature into it: a unit
# drawn in QGIS gets the columns of pyunitastratigrafiche, a spot height those of pyarchinit_quote
BASE_COLUMNS = {
    "unita": ["scavo_s", "area_s", "us_s", "unita_tipo_s", "stratigraph_index_us", "tipo_us_s", "rilievo_originale",
              "disegnatore", "data", "tipo_doc", "nome_doc", "area_m2"],
    "quote": ["sito_q", "area_q", "us_q", "unita_tipo_q", "quota_q", "unita_misu_q", "data", "disegnatore",
              "rilievo_originale", "x", "y"],
    "sezioni": ["lunghezza_m", "svg"],
}
NUMERIC_FIELDS = {"quota", "z", "x", "y", "lunghezza_m", "area_m2", "volume_m3", "quota_q"}  # REAL; the rest is TEXT
INTEGER_FIELDS = {"stratigraph_index_us", PART_FIELD}
FLAG_FIELDS = {"direttrice"}  # INTEGER 0/1
_NAMESPACE = uuid.UUID("0b5f2a7e-6a3c-5d1e-9f4b-2c8e7d6a1b30")  # of the identities derived from content


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


def classify_layer(name: str, kind: str, attrs: dict | None = None) -> str:
    """Which live table a feature of the layer ``name`` of the interface goes to, ``kind`` the one of its geometry
    (:func:`feature_kind`: a layer may hold lines and polygons). Polygons: the layers of stratigraphic
    units and any feature with the columns of a unit (pyunitastratigrafiche) to "unita", any other
    polygon (zones, stones, patches, the anomalies of a plugin) to "poligoni"."""
    if kind == "polygon":
        units = name == "Unita' stratigrafiche" or name.startswith("US ")
        return "unita" if units or attrs is None or any(k in attrs for k in ("_unit", "us_s", "us")) else "poligoni"
    if kind == "point":
        return "quote" if name == "Quote" else "punti"
    if name.startswith("Sezioni"):
        return "sezioni"
    if name.startswith("Curve di livello"):
        return "curve"
    return "linee"


_KINDS = {"Polygon": "polygon", "MultiPolygon": "polygon", "LineString": "line", "MultiLineString": "line",
          "Point": "point"}


def feature_kind(attrs: dict, default: str = "line") -> str:
    """``"polygon"``, ``"line"`` or ``"point"``: the kind of the geometry of a feature of the 2D view (its ``_shape``,
    else its ``_geom``); ``default`` (the kind of its layer) when it says none."""
    shape = attrs.get("_shape")
    gtype = getattr(shape, "geom_type", None) or (attrs.get("_geom") or (None,))[0]
    return _KINDS.get(str(gtype), default)


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


def _feature_shape(attrs: dict):
    """The geometry a feature goes into the GeoPackage with: its own shape when it carries one
    (``_shape``: a unit part with its holes), else the one of its ``_geom``."""
    shape = attrs.get("_shape")
    if shape is not None:
        return shape
    return _shape(*attrs["_geom"])


def _identified(table: str, feats: list, keep: list | None = None):
    """``((identity, part), geometry, WKB, values)`` of every feature of ``feats`` (``(layer, attributes)``) that goes
    into ``table``, as its row has them: its own identity (``ppg_id`` among its attributes), else one from its content
    (table, layer, geometry, attributes) - the same content, the same row; the same identity twice is another feature
    (of a unit, another part). A feature without geometry has no row; ``keep``: filled with the features that have one,
    in the order of the result."""
    import shapely

    counts: dict[str, int] = {}
    for name, attrs in feats:
        geom = _feature_shape(attrs)
        if geom is None or geom.is_empty:
            continue
        raw = shapely.to_wkb(geom, byte_order=1, output_dimension=2)
        vals = {_column(k): v for k, v in attrs.items() if not str(k).startswith("_")}
        vals["layer"] = name
        pid = str(vals.get(ID_FIELD) or "").strip() or None
        explicit = pid is not None
        if not explicit:  # a feature the program keeps no identity for: one from its content
            pid = stable_id(table, name, hashlib.sha1(raw).hexdigest(),
                            sorted((k, str(v)) for k, v in vals.items() if k not in TECHNICAL_FIELDS))
        n = counts[pid] = counts.get(pid, 0) + 1
        part = 1
        if n > 1 and (table != "unita" or not explicit):  # the same identity twice: another feature
            pid = stable_id(pid, n)
            counts[pid] = counts.get(pid, 0) + 1
        elif table == "unita":
            part = n
        if keep is not None:
            keep.append((name, attrs))
        yield (pid, part), geom, raw, vals


def plan_identities(layers: dict[str, dict]) -> dict[str, tuple[str, str, dict]]:
    """``{identity: (table, layer, attributes)}`` of the features of the layers of the raster view (as
    :meth:`LiveLink.write` takes them) that go into the tables of the other drawings of the plan
    (:data:`PLAN_TABLES`): the identity each has in its row of the live GeoPackage, to find the feature an edit read
    from there (:meth:`LiveLink.read_changes`) belongs to."""
    out = {}
    for table, feats in LiveLink._group(layers).items():
        if table not in PLAN_TABLES:
            continue
        kept: list = []
        for ((pid, _part), _geom, _raw, _vals), (name, attrs) in zip(list(_identified(table, feats, kept)), kept):
            out[pid] = (table, name, attrs)
    return out


def _content_identities(layers: dict[str, dict], legacy: bool) -> dict[int, str]:
    """``{id(attributes): identity}`` of the features of ``layers`` in the tables of the plan that carry no identity of
    their own (theirs is from their content, and from their table)."""
    out = {}
    for table, feats in LiveLink._group(layers, legacy=legacy).items():
        if table not in PLAN_TABLES:
            continue
        kept: list = []
        for ((pid, _part), _geom, _raw, _vals), (_name, attrs) in zip(list(_identified(table, feats, kept)), kept):
            if not str(attrs.get(ID_FIELD) or "").strip():
                out[id(attrs)] = pid
    return out


def legacy_identities(layers: dict[str, dict]) -> dict[str, str]:
    """``{identity up to 1.7.8: identity now}`` of the features of ``layers`` whose identity changed with their table:
    up to 1.7.8 every feature of a layer went to the table of the kind of the layer (its first feature), so the lines
    of a layer that begins with a polygon were rows of ``poligoni`` (and the polygons of one that begins with a line
    rows of ``linee``), known by an identity made with that table. The edits kept under those identities
    (``plan_edits`` of a project saved by 1.7.8) are found again with this. Empty for a layer of one kind."""
    kinds = {name: {feature_kind(a, lay["kind"]) for a in (f[-1] if isinstance(f, (tuple, list)) else f
                                                           for f in lay["features"]) if isinstance(a, dict)}
             for name, lay in layers.items()}
    mixed = {name: lay for name, lay in layers.items() if kinds[name] - {lay["kind"]}}
    if not mixed:
        return {}
    now = _content_identities(mixed, legacy=False)
    return {old: now[key] for key, old in _content_identities(mixed, legacy=True).items()
            if key in now and now[key] != old}


def new_id() -> str:
    """A new identity for a feature."""
    return str(uuid.uuid4())


def stable_id(*parts) -> str:
    """An identity derived from ``parts`` (JSON-able): the same parts, the same identity, in every
    session - for features that had none yet (projects of earlier versions)."""
    return str(uuid.uuid5(_NAMESPACE, json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)))


def row_signature(wkb: bytes | None, values: dict) -> str:
    """The signature of a row: its geometry (WKB) and its attribute values, the empty ones and the
    technical columns left out (a column added in QGIS does not change it)."""
    items = sorted((str(k), v) for k, v in values.items() if k not in TECHNICAL_FIELDS and v is not None)
    h = hashlib.sha1(bytes(wkb or b""))
    h.update(json.dumps(items, ensure_ascii=False, default=str).encode("utf-8"))
    return h.hexdigest()[:24]


def _affinity(declared: str) -> str:
    """The type affinity SQLite gives a column of the ``declared`` type."""
    t = (declared or "").upper()
    if "INT" in t:
        return "INTEGER"
    if any(s in t for s in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if "BLOB" in t or not t:
        return "BLOB"
    if any(s in t for s in ("REAL", "FLOA", "DOUB")):
        return "REAL"
    return "NUMERIC"


def _bind(affinity: str, v):
    """``v`` as SQLite keeps it in a column of that affinity, so that the signature of what is
    written equals the signature of what is read back."""
    if v is None or (isinstance(v, float) and v != v):
        return None
    if affinity == "TEXT":
        return v if isinstance(v, str) else str(v)
    if affinity == "REAL":
        try:
            return float(v)
        except (TypeError, ValueError):
            return v
    if affinity in ("INTEGER", "NUMERIC"):
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, float) and v.is_integer() and abs(v) < 2 ** 62:
            return int(v)
    return v


@dataclass
class _Wanted:  # a row as the program writes it
    key: tuple
    geom: object
    wkb: bytes
    values: dict
    sig: str


@dataclass
class _Stored:  # a row as it is in the GeoPackage
    fid: int
    key: tuple  # (identity or None, part)
    stored_sig: str | None
    content_sig: str | None = None  # None: the content was not read
    wkb: bytes | None = None
    values: dict | None = None
    srs_id: int | None = None

    @property
    def external(self) -> bool:
        """Edited by another program since the program last wrote or read it."""
        return self.content_sig is not None and self.content_sig != self.stored_sig


@dataclass
class Feature:
    """A row of a two-way table, as :meth:`LiveLink.read_changes` gives it back."""
    fid: int
    id: str  # its identity (given now when it was drawn in QGIS)
    part: int  # 1, 2... for the parts of a unit drawn as several polygons
    geometry: object  # shapely, None when it cannot be read
    values: dict  # column -> value (the technical columns included)
    srs_id: int | None = None  # of its geometry


@dataclass
class TableChanges:
    """What another program changed in a two-way table since it was last read."""
    changed: list = field(default_factory=list)  # Feature: geometry or attributes changed
    added: list = field(default_factory=list)  # Feature: drawn (or copied) there, given an identity or with its own
    deleted: list = field(default_factory=list)  # (identity, part) of the rows gone
    rows: list = field(default_factory=list)  # Feature: every row of the table now
    refused: tuple | None = None  # (srs of the table, srs of the project): not read

    def __bool__(self) -> bool:
        return bool(self.changed or self.added or self.deleted or self.refused)


class LiveLink:
    """The GeoPackage QGIS reads the drawings from, and where its edits come back from. Rows are
    written in place by identity, only those that changed, in one transaction (QGIS never reads
    half a table, and keeps the file open: a replaced file would not be seen). One connection is
    kept open: the program's own writes do not change its ``PRAGMA data_version``, the commits of
    another program do (:meth:`external_change`)."""

    def __init__(self, folder: str | Path, crs: str):
        self.path = Path(folder) / LIVE_DB
        self.crs = crs or ""
        self._signatures: dict[str, int] = {}
        # table -> {(identity, part): fid} written or read here: what the file is compared with when it has no record
        # of that table (:data:`ROWS_TABLE`, written by a version before it was kept)
        self._known: dict[str, dict[tuple, int]] = {}
        self._con: sqlite3.Connection | None = None
        self._ident = None  # the file the connection is open on
        self._data_version = None
        self.read_chunk: str | None = None  # the key of the chunk of the file when its changes were last read
        self._styled = None  # the default styles of every table are in the file (qgis_styles.ensure_default_styles)

    # ------------------------------------------------------------------ connection
    def _file_ident(self):
        try:
            st = self.path.stat()
        except OSError:
            return None
        return st.st_dev, st.st_ino

    def _connection(self) -> sqlite3.Connection:
        from . import gpkg

        ident = self._file_ident()
        if self._con is not None and ident != self._ident:
            self.close()  # the file went away or was replaced: what was known of it too
        if self._con is None:
            self._con = gpkg.connect(self.path)
            self._ident = self._file_ident()
            self._known, self._signatures, self._data_version = {}, {}, None
            self._styled = None
        return self._con

    def close(self) -> None:
        if self._con is not None:
            try:
                self._con.close()
            except sqlite3.Error:
                pass
        self._con, self._ident = None, None

    # ------------------------------------------------------------------ writing
    @staticmethod
    def _group(layers: dict[str, dict], legacy: bool = False) -> dict[str, list]:
        """``{table: [(layer, attributes)]}`` of the features of ``layers``: each goes to the table of its own
        geometry (:func:`feature_kind`), so a layer that holds lines and polygons (PhotoCAD) fills two tables.
        ``legacy``: as up to 1.7.8, every feature to the table of the kind of its layer - the kind of the first
        feature drawn - whatever its geometry (:func:`legacy_identities`)."""
        out: dict[str, list] = {t: [] for t in LIVE_TABLES}
        for name, lay in layers.items():
            for feat in lay["features"]:
                attrs = feat[-1] if isinstance(feat, (tuple, list)) else feat
                if isinstance(attrs, dict) and attrs.get("_geom"):
                    kind = lay["kind"] if legacy else feature_kind(attrs, lay["kind"])
                    out[classify_layer(name, kind, attrs)].append((name, attrs))
        return out

    @staticmethod
    def _held(layers: dict[str, dict]) -> frozenset:
        """The layers of ``layers`` that are not sent (``"sent": False``: hidden in the Layers pane)."""
        return frozenset(name for name, lay in layers.items() if lay.get("sent", True) is False)

    @staticmethod
    def _signature(feats: list, held=frozenset()) -> int:
        sig = [tuple(sorted({name for name, _attrs in feats} & held))]  # another choice of layers: another content
        for name, attrs in feats:
            gtype, coords = attrs["_geom"]
            n = len(coords) if coords is not None else 0
            sig.append((name, id(attrs["_geom"]), id(attrs.get("_shape")), gtype, n, str(coords[0]) if n else "",
                        str(coords[n // 2]) if n else "", str(coords[-1]) if n else "",
                        tuple(sorted((str(k), str(v)) for k, v in attrs.items() if not str(k).startswith("_")))))
        return hash(tuple(sig))

    def write(self, layers: dict[str, dict], force: bool = False, chunk=None,
              drop_unread: bool = False, free=None) -> dict[str, Path]:
        """Bring the GeoPackage up to date with the layers of the raster view
        (``{name: {"kind", "features"}}``, features as attribute dicts with ``_geom`` or the
        ``(item, label, attrs)`` triples of :class:`gui.image_views.RasterView`). Every table
        exists even when empty, so QGIS sees deletions. A layer with ``"sent": False`` (hidden in the Layers pane)
        is held: its rows leave the file and its record, so nothing of it is read back as deleted there, and come
        back with the identities they had once it is sent again; what another program saved on them and nobody read
        yet stays until it is read. ``force``: every table compared row by
        row, also those whose features did not change here (an edit made in QGIS to a one-way
        table is undone). Rows of the two-way tables edited or deleted in QGIS and not read back
        yet are left as they are. ``chunk``: the key of the chunk the drawings belong to, kept in
        the file with them (:meth:`stored_chunk`). Another chunk's drawings go in only when the file
        holds no edit of another program not read yet - checked in the write's own transaction, so a
        save of QGIS a moment before is never written over: :class:`UnreadEdits` then, nothing
        written; ``drop_unread``: those rows were kept elsewhere (they could not be read), and go. ``free``: the
        chunk whose free drawings in 3D go into their tables in the same transaction (:meth:`write_free`; the
        tables left as they are when None)."""
        from . import gpkg
        from .free_drawings import FREE_PREFIX

        groups, held = self._group(layers), self._held(layers)
        new = not gpkg.is_geopackage(self.path)
        if new:
            gpkg.refuse_foreign(self.path)  # never write the drawings into another file of that name
            self.close()
        con = self._connection()
        if new:
            self._signatures = {}
        key = None if chunk is None else str(chunk)
        stored = self._stored_key(con)
        rekey = key is not None and key != stored
        if rekey and stored is not None:  # another chunk's drawings: every table compared row by row, whatever the
            force = True  # window wrote last (rows read from another program belong to the chunk before)
        todo = {t: f for t, f in groups.items() if force or new or self._signatures.get(t) != self._signature(f, held)}
        free_sig = None if free is None else self._free_signature(free)
        free_todo = free is not None and (force or new or rekey or self._signatures.get(FREE_PREFIX) != free_sig)
        if todo or rekey or free_todo:
            srs = gpkg.srs_of(self.crs)
            known = {}
            try:
                con.execute("BEGIN IMMEDIATE")
                gpkg.create(con, srs)  # idempotent: the system of the chunk listed, also after it changed
                if rekey and stored is not None and not drop_unread and self._unread(con):
                    raise UnreadEdits(f"the file holds edits of chunk {stored} not read yet")
                for table, feats in todo.items():
                    known[table] = self._write_table(con, table, feats, srs[0], force, drop=drop_unread,
                                                     held=held)
                    if table in _READ_BACK:
                        self._record(con, table, known[table])
                if free_todo:
                    known.update(self._write_free(con, free, srs[0], drop=drop_unread))
                if rekey:
                    con.execute(f"CREATE TABLE IF NOT EXISTS {INFO_TABLE} (name TEXT PRIMARY KEY, value TEXT)")
                    con.execute(f"INSERT OR REPLACE INTO {INFO_TABLE} (name, value) VALUES ('chunk', ?)", (key,))
                if todo and not self._styled:  # the default styles QGIS finds in the file, written once
                    from .qgis_styles import ensure_default_styles

                    ensure_default_styles(con, {t: (LIVE_TABLES[t][1], STYLES[t][0], STYLES[t][1]) for t in todo})
                    self._styled = set(LIVE_TABLES) <= set(todo) or None
                con.execute("COMMIT")
            except BaseException:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
            self._known.update(known)
            for table, feats in todo.items():
                self._signatures[table] = self._signature(feats, held)
            if free_todo:
                self._signatures[FREE_PREFIX] = free_sig
        return {t: self.path for t in LIVE_TABLES}

    def write_elevations(self, elevations: dict[str, tuple[str, dict]]) -> list[str]:
        """The drawings of the elevations and sections of the chunk placed on the survey (``{elevation id: (crs,
        engine.elevation.world_tables(...))}``) as the tables ``prospetto_<id>_<name>`` of the GeoPackage, 3D, each in
        the system the elevation was made in; those of elevations no longer there go. One-way: what QGIS edits there
        does not come back. Nothing is written when they did not change. Returns the tables written."""
        from ..engine.elevation import write_world_tables
        from . import gpkg

        sig = hash(tuple((eid, crs, name, tuple((g.wkb, tuple(sorted((k, str(v)) for k, v in vals.items())))
                                                  for g, vals in rows))
                         for eid, (crs, tables) in sorted(elevations.items())
                         for name, (_g, _c, rows) in sorted(tables.items())))
        if not gpkg.is_geopackage(self.path) or (sig == self._signatures.get(ELEVATION_PREFIX)):
            return []
        con = self._connection()
        written = []
        try:
            con.execute("BEGIN IMMEDIATE")
            for eid, (crs, tables) in elevations.items():
                srs = gpkg.srs_of(crs)
                gpkg.create(con, srs)
                written += write_world_tables(con, tables, srs[0], prefix=f"{ELEVATION_PREFIX}{eid}_")
            stale = [r[0] for r in con.execute("SELECT table_name FROM gpkg_contents WHERE table_name GLOB ?",
                                               (ELEVATION_PREFIX + "*",)) if r[0] not in written]
            for table in stale:
                gpkg.drop_table(con, table)
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        self._signatures[ELEVATION_PREFIX] = sig
        return written

    def elevation_tables(self, filled: bool = False) -> list[tuple[str, str]]:
        """``(table, geometry type)`` of the elevations' tables the GeoPackage holds (:meth:`write_elevations`);
        ``filled``: only those with rows (the layers of a QGIS project)."""
        from . import gpkg

        if not gpkg.is_geopackage(self.path):
            return []
        con = self._connection()
        tables = [(r[0], r[1]) for r in con.execute(
            "SELECT table_name, geometry_type_name FROM gpkg_geometry_columns WHERE table_name GLOB ? ORDER BY "
            "table_name", (ELEVATION_PREFIX + "*",))]
        if filled:
            tables = [(t, g) for t, g in tables
                      if con.execute(f"SELECT 1 FROM {gpkg.quote(t)} LIMIT 1").fetchone() is not None]
        return tables

    def _free_signature(self, chunk) -> int:
        from ..core import free_drawings as fd

        hidden = sorted(x["name"] for x in fd.layers(chunk) if not x.get("visible", True))
        return hash((str(self.crs), json.dumps(fd.features(chunk), sort_keys=True, default=str), tuple(hidden)))

    def write_free(self, chunk) -> list[str]:
        """The free drawings in 3D of ``chunk`` (:mod:`..core.free_drawings`) as the three tables ``disegni_3d_*`` of the
        GeoPackage (:data:`.free_drawings.TABLES`, with Z, in the chunk's system), row by row by identity as the
        two-way tables: a row keeps its ``fid`` while its drawing lives, and a row another program edited, deleted or
        drew and nobody read yet is left as it is (:meth:`read_changes` gives it back). A chunk without free drawings
        has none of those tables (one holding rows not read yet stays until they are). The drawings of a layer hidden
        in the Layers pane are held as the layers of the plan are (:meth:`write`): no rows, nothing read back. Nothing is written when the
        drawings did not change since the last write, nor before the window made the file. The drawings of another
        chunk than the one the file holds go in with :meth:`write` (``free``), which refuses to write over edits
        not read. Returns the tables written."""
        from . import gpkg
        from .free_drawings import FREE_PREFIX

        sig = self._free_signature(chunk)
        if not gpkg.is_geopackage(self.path) or sig == self._signatures.get(FREE_PREFIX):
            return []
        con = self._connection()
        try:
            con.execute("BEGIN IMMEDIATE")
            srs = gpkg.srs_of(self.crs)
            gpkg.create(con, srs)
            known = self._write_free(con, chunk, srs[0])
            con.execute("COMMIT")
        except BaseException:
            if con.in_transaction:
                con.execute("ROLLBACK")
            raise
        self._known.update(known)
        self._signatures[FREE_PREFIX] = sig
        return list(known)

    def _write_free(self, con, chunk, srs_id: int, drop: bool = False) -> dict[str, dict]:
        """The tables of the free drawings of ``chunk`` brought to its drawings, in the transaction under way;
        ``{table: {key: fid}}`` of the tables there are now (all three while the chunk has a free drawing). ``drop``:
        as in :meth:`_write_table`."""
        import shapely

        from ..core import free_drawings as fd
        from . import free_drawings as fio
        from . import gpkg

        q = gpkg.quote
        feats = fd.features(chunk)
        hidden = {x["name"] for x in fd.layers(chunk) if not x.get("visible", True)}  # not sent: held
        technical = {c.lower() for c in TECHNICAL_FIELDS}
        out = {}
        for kind, (table, gtype) in fio.TABLES.items():
            have = [r[1] for r in con.execute(f"PRAGMA table_info({q(table)})")]
            if have and gpkg.table_srs(con, table) not in (None, srs_id):
                gpkg.drop_table(con, table)  # in the system the chunk had before: made again in this one
                self._forget(con, table)
                have = []
            unsigned = bool(have) and not (ID_FIELD in have and SIG_FIELD in have)  # as 1.7.7 wrote it (one-way)
            if not feats:  # none (left): no tables, so that QGIS and RustGIS have no empty layers of them
                if have and (drop or unsigned or not self._table_unread(con, table)):
                    gpkg.drop_table(con, table)
                    self._forget(con, table)
                continue
            held = {(r["id"], 1) for r in feats if r["kind"] == kind and r["layer"] in hidden}
            mine = [r for r in feats if r["kind"] == kind and r["layer"] not in hidden]
            names = {c.lower(): c for c in have}  # a column is the same in any case
            extra = [c for c in fio.attribute_columns(mine) if c.lower() not in technical]
            columns = {c: "TEXT" for c in (*fio.BASE_COLUMNS, *(names.get(c.lower(), c) for c in extra), SIG_FIELD)}
            # the base type in gpkg_geometry_columns (LINESTRING...), z = 1: the standard's way
            cols = gpkg.ensure_table(con, table, gtype.removesuffix("Z"), columns, srs_id,
                                     identifier=fio.TITLES[kind], z=True)
            if unsigned:
                con.execute(f"DELETE FROM {q(table)}")  # rows without signature: written again
            types = {r[1]: r[2] for r in con.execute(f"PRAGMA table_info({q(table)})")}
            names = {c.lower(): c for c in cols}
            wanted = []
            for rec in mine:
                geom = fio._geometry(rec)
                raw = shapely.to_wkb(geom, byte_order=1, output_dimension=3, flavor="iso")
                row = {names.get(k.lower(), k): v for k, v in fio._row(rec, extra).items()}
                values = {c: _bind(_affinity(types.get(c, "")), row.get(c)) for c in cols if c != SIG_FIELD}
                wanted.append(_Wanted((rec["id"], 1), geom, raw, values, row_signature(raw, values)))
            known = {} if unsigned else self._known_rows(con, table)
            out[table] = self._put_rows(con, table, cols, wanted, srs_id, not drop, known, z=True, held=held)
            self._record(con, table, out[table])
        return out

    def free_tables(self) -> list[tuple[str, str]]:
        """``(table, geometry type)`` of the tables of the free drawings the GeoPackage holds (:meth:`write_free`)."""
        from . import gpkg
        from .free_drawings import FREE_PREFIX

        if not gpkg.is_geopackage(self.path):
            return []
        con = self._connection()
        return [(r[0], r[1]) for r in con.execute(
            "SELECT table_name, geometry_type_name FROM gpkg_geometry_columns WHERE table_name GLOB ? ORDER BY "
            "table_name", (FREE_PREFIX + "*",))]

    def free_vectors(self) -> list[dict]:
        """The layers of the free drawings for a QGIS project (``extra_vectors`` of :func:`build_project`): one for
        each layer of free drawings with rows, as those of the plan (:func:`live_vectors`) - two or three when it
        holds lines, polygons and points -, labelled by the name of the drawing."""
        from .free_drawings import TABLES, TITLES

        found = table_layers(self.path, [table for table, _gtype in TABLES.values()])
        colours = {"line": "#ffb000", "polygon": "#ff8c00", "point": "#ffd000"}
        suffix = {"line": "linee", "polygon": "poligoni", "point": "punti"}
        shared: dict[str, int] = {}
        for rows in found.values():
            for name in rows:
                shared[name] = shared.get(name, 0) + 1
        out = []
        for kind, (table, gtype) in TABLES.items():
            for name in found.get(table, {}):
                shown = (TITLES[kind] if not name else
                         f"Disegni 3D: {name}" + (f" - {suffix[kind]}" if shared[name] > 1 else ""))
                out.append({"name": shown, "path": str(self.path), "table": table, "geometry": gtype.rstrip("Z"),
                            "color": colours[kind], "label": "name", "layer": name, "subset": layer_subset(name),
                            **({"defaults": {"layer": name}} if name else {})})
        return out

    def _wanted(self, table: str, feats: list, cols: list[str], types: dict,
                held=frozenset()) -> tuple[list[_Wanted], set]:
        """The rows of ``table`` for ``feats``, and the keys of the features of the layers ``held``: identified with
        the others (the identities are those of all the features), not written."""
        out, kept = [], set()
        for (pid, part), geom, raw, vals in _identified(table, feats):
            if vals["layer"] in held:
                kept.add((pid, part))
                continue
            vals[ID_FIELD] = pid
            if table == "unita":
                vals[PART_FIELD] = part
            values = {c: _bind(_affinity(types.get(c, "")), _value(c, vals.get(c))) for c in cols if c != SIG_FIELD}
            out.append(_Wanted((pid, part), geom, raw, values, row_signature(raw, values)))
        return out, kept

    @staticmethod
    def _scan(con, table: str, cols: list[str], content: bool) -> list[_Stored]:
        """The rows of ``table`` (by fid); ``content``: their geometry and values read and signed."""
        from . import gpkg

        q = gpkg.quote
        part = PART_FIELD in cols
        out = []
        if content:
            names = "".join(f", {q(c)}" for c in cols)
            for row in con.execute(f"SELECT fid, geom{names} FROM {q(table)} ORDER BY fid"):
                values = dict(zip(cols, row[2:]))
                raw = gpkg.wkb(row[1])
                pid = str(values.get(ID_FIELD) or "").strip() or None
                key = (pid, int(values.get(PART_FIELD) or 1) if part and pid else 1)
                out.append(_Stored(int(row[0]), key, values.get(SIG_FIELD), row_signature(raw, values), raw, values,
                                   gpkg.blob_srs(row[1])))
        else:
            extra = f", {q(PART_FIELD)}" if part else ""
            for row in con.execute(f"SELECT fid, {q(ID_FIELD)}, {q(SIG_FIELD)}{extra} FROM {q(table)} ORDER BY fid"):
                pid = str(row[1] or "").strip() or None
                key = (pid, int(row[3] or 1) if part and pid else 1)
                out.append(_Stored(int(row[0]), key, row[2]))
        return out

    @staticmethod
    def _sort(rows: list[_Stored], known: dict) -> tuple[dict, list]:
        """``{key: row}`` of the rows with an identity, and the others: without one, or copies of
        a row (QGIS copies every attribute) - the row known here, else the first, is the original."""
        primary: dict[tuple, _Stored] = {}
        others = []
        for s in rows:
            if s.key[0] is None:
                others.append(s)
                continue
            cur = primary.get(s.key)
            if cur is None:
                primary[s.key] = s
            elif known.get(s.key) == s.fid:
                others.append(cur)
                primary[s.key] = s
            else:
                others.append(s)
        return primary, others

    def _write_table(self, con, table: str, feats: list, srs_id: int, force: bool, drop: bool = False,
                     held=frozenset()) -> dict:
        """The rows of ``table`` brought to ``feats`` one by one; returns ``{key: fid}`` of its rows. ``drop``: rows
        another program edited and nobody read are written over too (kept elsewhere: :meth:`write`). ``held``: the
        layers whose features have no row (:meth:`write`)."""
        from . import gpkg

        q = gpkg.quote
        label = STYLES[table][1]
        keys = ["layer", "etichetta"] + ([label] if label not in ("layer", "etichetta") else [])
        keys += [k for k in BASE_COLUMNS.get(table, []) if k not in keys]
        keys += sorted({_column(k) for _, a in feats for k in a if not str(k).startswith("_")} - set(keys))
        keys += [k for k in (ID_FIELD, SIG_FIELD) + ((PART_FIELD,) if table == "unita" else ()) if k not in keys]
        # a table in another system (the chunk changed its own) is made again in this one: QGIS reads the rows in the
        # system they are written in, and the rows known in it are no more
        recreated = gpkg.table_srs(con, table) not in (None, srs_id)
        before = [] if recreated else [r[1] for r in con.execute(f"PRAGMA table_info({q(table)})")]
        cols = gpkg.ensure_table(con, table, LIVE_TABLES[table][1], {k: _sql_type(k) for k in keys}, srs_id,
                                 identifier=LIVE_TABLES[table][0], recreate=True)
        types = {r[1]: r[2] for r in con.execute(f"PRAGMA table_info({q(table)})")}
        if before and ID_FIELD not in before:
            con.execute(f"DELETE FROM {q(table)}")  # written by an earlier version: rows without identity
        two_way = table in _READ_BACK and not drop  # the rows edited elsewhere kept until read
        known = {} if recreated else self._known_rows(con, table)
        wanted, kept = self._wanted(table, feats, cols, types, held)
        return self._put_rows(con, table, cols, wanted, srs_id, two_way, known,
                              content=two_way or force or drop, held=kept)

    def _put_rows(self, con, table: str, cols: list[str], wanted: list[_Wanted], srs_id: int, two_way: bool,
                  known: dict, content: bool = True, z: bool = False, held=frozenset()) -> dict:
        """The rows of ``table`` brought to ``wanted`` one by one, by identity; returns ``{key: fid}`` of its rows.
        ``two_way``: the rows another program edited, deleted (``known``: those the program last wrote or read) or drew
        and nobody read yet are left as they are. ``content``: the rows compared by what they hold, not only by their
        signature. ``z``: the geometries written with their Z. ``held``: the keys of the features that are in the program
        and not sent - their rows go like those of features gone here, and leave the record with them (nothing read
        back as deleted there); one deleted there and not read yet stays in the record until the reader took it."""
        from . import gpkg

        q = gpkg.quote
        primary, others = self._sort(self._scan(con, table, cols, content=content), known)
        written = [c for c in cols if c != SIG_FIELD]
        names = "".join(f", {q(c)}" for c in written)
        insert = f"INSERT INTO {q(table)} (geom{names}) VALUES ({', '.join('?' * (len(written) + 1))})"
        update = f"UPDATE {q(table)} SET geom = ?{''.join(f', {q(c)} = ?' for c in written)} WHERE fid = ?"
        touched, after, changed = [], {}, False
        for w in wanted:
            s = primary.pop(w.key, None)
            row = [gpkg.blob(w.geom, srs_id, z)] + [w.values.get(c) for c in written]
            if s is None and two_way and w.key in known:
                after[w.key] = known[w.key]  # deleted in QGIS and not read back yet: the reader takes it first
                continue
            if s is None:
                fid = con.execute(insert, row).lastrowid
                touched.append(fid)
            else:
                fid = s.fid
                if two_way and s.external:
                    pass  # edited in QGIS and not read back yet: the reader takes it first
                elif s.stored_sig != w.sig or (s.content_sig is not None and s.content_sig != w.sig):
                    con.execute(update, row + [fid])
                    touched.append(fid)
            after[w.key] = fid
        if two_way:
            for key in held:  # sent before, deleted in QGIS and not read back yet: the reader takes it first
                if key not in primary and key in known:
                    after[key] = known[key]
        for key, s in primary.items():  # rows of features gone here, or held
            if two_way and s.external:
                after[key] = s.fid  # changed in QGIS, deleted here: the reader decides
                continue
            con.execute(f"DELETE FROM {q(table)} WHERE fid = ?", (s.fid,))
            changed = True
        for s in others:  # drawn or copied in QGIS: in a two-way table the reader gives them an identity
            if not two_way:
                con.execute(f"DELETE FROM {q(table)} WHERE fid = ?", (s.fid,))
                changed = True
        if touched:
            self._sign(con, table, cols, touched)
        if touched or changed:
            gpkg.set_extent(con, table, _bounds(con, table))
        return after

    def _unread(self, con) -> bool:
        """Whether a two-way table holds a row another program edited or drew and nobody read yet: its content not
        the one signed, or no identity; or lacks a row the program last wrote or read there (deleted meanwhile)."""
        return any(self._table_unread(con, table) for table in two_way_tables())

    def _table_unread(self, con, table: str) -> bool:
        from . import gpkg

        cols = [r[1] for r in con.execute(f"PRAGMA table_info({gpkg.quote(table)})") if r[1] not in ("fid", "geom")]
        if not _readable(table, cols):
            return False
        rows = self._scan(con, table, cols, content=True)
        if any(s.external or s.key[0] is None for s in rows):
            return True
        here = {s.key for s in rows}
        return any(key not in here for key in self._known_rows(con, table))

    def _forget(self, con, table: str) -> None:
        """The record of the rows of ``table`` (dropped) taken away, in the transaction under way."""
        self._known.pop(table, None)
        for sql, args in ((f"DELETE FROM {ROWS_TABLE} WHERE name = ?", (table,)),
                          (f"DELETE FROM {INFO_TABLE} WHERE name = ?", (f"rows:{table}",))):
            try:
                con.execute(sql, args)
            except sqlite3.OperationalError as exc:
                if "no such table" not in str(exc):
                    raise

    # ------------------------------------------------------------------ the record of the rows
    def _known_rows(self, con, table: str) -> dict[tuple, int]:
        """``{(identity, part): fid}`` of the rows of ``table`` as the program last wrote or read them: the record the
        file keeps (:data:`ROWS_TABLE`: the same for a link that never saw the file, after the program started again,
        or for another link that wrote meanwhile), else what this link remembers (a file written by a version before
        the record was kept, or a one-way table)."""
        recorded = self._recorded(con, table)
        return self._known.get(table, {}) if recorded is None else recorded

    @staticmethod
    def _recorded(con, table: str) -> dict[tuple, int] | None:
        """The record of ``table`` in the file; None when the file has none of it."""
        try:
            row = con.execute(f"SELECT value FROM {INFO_TABLE} WHERE name = ?", (f"rows:{table}",)).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise
            return None
        if row is None:
            return None
        return {(pid, int(part)): int(fid) for pid, part, fid in
                con.execute(f"SELECT ppg_id, part, fid FROM {ROWS_TABLE} WHERE name = ?", (table,))}

    @classmethod
    def _record(cls, con, table: str, rows: dict[tuple, int]) -> None:
        """``rows`` (``{(identity, part): fid}``) as the record of ``table``, in the transaction under way: only the
        entries that differ from the record are written, so a write or a read that changed no row leaves the file as
        it is (nothing for another program to read again, no thousands of rows written at every edit)."""
        want = {(pid, int(part or 1)): int(fid) for (pid, part), fid in rows.items() if pid is not None}
        have = cls._recorded(con, table)
        if have is None:
            con.execute(f"CREATE TABLE IF NOT EXISTS {INFO_TABLE} (name TEXT PRIMARY KEY, value TEXT)")
            con.execute(f"CREATE TABLE IF NOT EXISTS {ROWS_TABLE} (name TEXT NOT NULL, ppg_id TEXT NOT NULL, "
                        "part INTEGER NOT NULL, fid INTEGER NOT NULL, PRIMARY KEY (name, ppg_id, part))")
            con.execute(f"DELETE FROM {ROWS_TABLE} WHERE name = ?", (table,))  # left by a record forgotten
            con.execute(f"INSERT OR REPLACE INTO {INFO_TABLE} (name, value) VALUES (?, '1')", (f"rows:{table}",))
            have = {}
        gone = [(table, pid, part) for pid, part in have.keys() - want.keys()]
        put = [(table, pid, part, fid) for (pid, part), fid in want.items() if have.get((pid, part)) != fid]
        if gone:
            con.executemany(f"DELETE FROM {ROWS_TABLE} WHERE name = ? AND ppg_id = ? AND part = ?", gone)
        if put:
            con.executemany(f"INSERT OR REPLACE INTO {ROWS_TABLE} (name, ppg_id, part, fid) VALUES (?, ?, ?, ?)", put)

    @staticmethod
    def _stored_key(con) -> str | None:
        """The key of the chunk whose drawings the file holds (:data:`INFO_TABLE`); None in a file written before it was
        kept."""
        try:
            row = con.execute(f"SELECT value FROM {INFO_TABLE} WHERE name = 'chunk'").fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                raise  # the file held by another program a moment: the caller tries again
            return None
        return None if row is None or row[0] is None else str(row[0])

    def stored_chunk(self) -> str | None:
        """The key of the chunk whose drawings the GeoPackage holds, as the program last wrote it with them (``chunk``
        of :meth:`write`): the chunk an edit made there belongs to. None while there is no GeoPackage, or in one
        written by a version that did not keep it. Cheap (the connection kept open): asked before every write."""
        if not self.path.exists():
            return None
        return self._stored_key(self._connection())

    @staticmethod
    def _sign(con, table: str, cols: list[str], fids: list[int]) -> None:
        """``ppg_sig`` of the rows just written, from what SQLite keeps of them."""
        from . import gpkg

        q = gpkg.quote
        names = "".join(f", {q(c)}" for c in cols)
        sigs = []
        for k in range(0, len(fids), 500):
            chunk = fids[k:k + 500]
            for row in con.execute(f"SELECT fid, geom{names} FROM {q(table)} WHERE fid IN "
                                   f"({', '.join('?' * len(chunk))})", chunk):
                sigs.append((row_signature(gpkg.wkb(row[1]), dict(zip(cols, row[2:]))), row[0]))
        con.executemany(f"UPDATE {q(table)} SET {q(SIG_FIELD)} = ? WHERE fid = ?", sigs)

    # ------------------------------------------------------------------ reading back
    def external_change(self) -> bool:
        """Whether another program committed to the GeoPackage since it was last read here (the
        program's own writes do not count). False while there is no GeoPackage."""
        if not self.path.exists():
            return False
        con = self._connection()
        return con.execute("PRAGMA data_version").fetchone()[0] != self._data_version

    def read_changes(self, busy_ms: int = 250, any_srs: bool = False,
                     before_commit: Callable[[dict], None] | None = None) -> dict[str, TableChanges]:
        """What another program (QGIS) changed in the two-way tables (:func:`two_way_tables`: units, spot heights,
        sections, the three tables of the free drawings in 3D) since the last write or read:
        ``{table: TableChanges}``, only the tables with changes. The rows drawn there get their
        identity and every row read its signature, in the same transaction: a change is given once.
        :attr:`read_chunk` is then the key of the chunk the file held the drawings of (read in that
        transaction: the chunk the changes belong to). A table in another system than the link's is
        refused, unread - ``any_srs``: read all the same, its features in their own system
        (:attr:`Feature.srs_id`). ``before_commit``: called with the changes in the read's own
        transaction, before the rows are signed as read - what it raises rolls the read back,
        nothing signed, the same changes at the next read (the rows kept beside the project must be
        written before they count as read). :class:`sqlite3.OperationalError` (database locked)
        when QGIS holds the file: try again."""
        from . import gpkg
        from .free_drawings import FREE_PREFIX

        if not gpkg.is_geopackage(self.path):
            return {}
        q = gpkg.quote
        con = self._connection()
        srs = gpkg.srs_of(self.crs)[0]
        out, known = {}, {}
        con.execute(f"PRAGMA busy_timeout = {int(busy_ms)}")
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                version = con.execute("PRAGMA data_version").fetchone()[0]
                owner = self._stored_key(con)
                for table in two_way_tables():
                    cols = [r[1] for r in con.execute(f"PRAGMA table_info({q(table)})") if r[1] not in ("fid", "geom")]
                    if not _readable(table, cols):
                        continue  # not written by this version yet
                    have = gpkg.table_srs(con, table)
                    if have is not None and have != srs and not any_srs:
                        out[table] = TableChanges(refused=(have, srs))
                        continue
                    ch, known[table] = self._read_table(con, table, cols)
                    self._record(con, table, known[table])
                    if ch:
                        out[table] = ch
                if before_commit is not None:
                    before_commit(out)
                con.execute("COMMIT")
            except BaseException:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
        finally:
            con.execute("PRAGMA busy_timeout = 10000")
        self._data_version = version
        self._known.update(known)
        self.read_chunk = owner
        if any(table.startswith(FREE_PREFIX) for table in out):
            self._signatures.pop(FREE_PREFIX, None)  # the next write compares the rows with the drawings again
        for table in out:
            if table in PLAN_TABLES:  # the next write compares its rows with the layers again: what the window did
                self._signatures.pop(table, None)  # not take of them is put back as it was
        return out

    def _read_table(self, con, table: str, cols: list[str]) -> tuple[TableChanges, dict]:
        from . import gpkg

        q = gpkg.quote
        known = self._known_rows(con, table)
        primary, others = self._sort(self._scan(con, table, cols, content=True), known)
        ch = TableChanges()
        part = PART_FIELD in cols
        for s in others:  # drawn in QGIS, or a copy: a feature of its own
            s.key = (new_id(), 1)
            sets = f"{q(ID_FIELD)} = ?" + (f", {q(PART_FIELD)} = 1" if part else "")
            con.execute(f"UPDATE {q(table)} SET {sets}, {q(SIG_FIELD)} = ? WHERE fid = ?",
                        (s.key[0], s.content_sig, s.fid))
            s.values[ID_FIELD] = s.key[0]
            if part:
                s.values[PART_FIELD] = 1
            ch.added.append(s)
        # an identity never known here, none of its rows signed by the program: a feature drawn there under an identity
        # another program gave it (a unit drawn in RustGIS as several polygons, one copied from another project) is
        # added, as it is - a new part of a unit known here is a change of it
        seen = {key[0] for key in known}
        signed = {s.key[0] for s in primary.values() if s.stored_sig is not None}
        for s in primary.values():
            if s.external:
                con.execute(f"UPDATE {q(table)} SET {q(SIG_FIELD)} = ? WHERE fid = ?", (s.content_sig, s.fid))
                (ch.changed if s.key[0] in seen or s.key[0] in signed else ch.added).append(s)
        ch.deleted = [k for k in known if k not in primary]
        rows = list(primary.values()) + others
        ch.rows = [self._feature(s) for s in rows]
        by_fid = {f.fid: f for f in ch.rows}
        ch.changed = [by_fid[s.fid] for s in ch.changed]
        ch.added = [by_fid[s.fid] for s in ch.added]
        return ch, {s.key: s.fid for s in rows}

    @staticmethod
    def _feature(s: _Stored) -> Feature:
        import shapely

        try:
            geom = shapely.from_wkb(s.wkb) if s.wkb is not None else None
        except Exception:  # noqa: BLE001 - a geometry QGIS wrote that shapely cannot read: said by the reader
            geom = None
        return Feature(s.fid, s.key[0], s.key[1], geom, dict(s.values or {}), s.srs_id)

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


_READ_BACK = (*TWO_WAY_TABLES, *PLAN_TABLES)  # the tables of the plan whose rows are kept until read, and recorded


def _readable(table: str, cols: list[str]) -> bool:
    """Whether the rows of a two-way ``table`` with the columns ``cols`` can be told apart and compared: they have
    their identity; those of the free drawings their signature too (1.7.7 wrote them with the identity alone)."""
    if ID_FIELD not in cols:
        return False
    return table in TWO_WAY_TABLES or SIG_FIELD in cols


def _bounds(con, table: str) -> list:
    """``(minx, miny, maxx, maxy)`` of every row of ``table``: the envelope of the header when the
    geometry has one, else the geometry's own."""
    import struct

    from . import gpkg

    out = []
    for (data,) in con.execute(f"SELECT geom FROM {gpkg.quote(table)}"):
        if data is None or len(data) < 8:
            continue
        data = bytes(data)
        if (data[3] >> 1) & 0b111 and not data[3] & 0b10000:
            minx, maxx, miny, maxy = struct.unpack("<4d" if data[3] & 1 else ">4d", data[8:40])
            out.append((minx, miny, maxx, maxy))
        else:
            g = gpkg.geometry(data)
            if g is not None and not g.is_empty:
                out.append(g.bounds)
    return out


def export_layers(folder: str | Path, layers: dict[str, dict], crs: str) -> dict[str, Path]:
    """Write the live GeoPackage from the layers of the raster view (see :meth:`LiveLink.write`);
    returns ``{table: path of the GeoPackage}``."""
    link = LiveLink(folder, crs)
    try:
        return link.write(layers, force=True)
    finally:
        link.close()


_BUILD_SCRIPT = r'''
import json, sys
from qgis.core import (QgsApplication, QgsCoordinateReferenceSystem, QgsPalLayerSettings, QgsProject,
                       QgsRasterLayer, QgsVectorLayer, QgsVectorLayerSimpleLabeling, QgsTextFormat,
                       QgsHillshadeRenderer, QgsMultiBandColorRenderer, QgsSingleBandPseudoColorRenderer,
                       QgsColorRampShader, QgsRasterShader, QgsPalettedRasterRenderer, QgsContrastEnhancement,
                       QgsDefaultValue, QgsProviderRegistry)
from qgis.PyQt.QtGui import QColor


def stretched(lyr, band):
    """A contrast enhancement between the 2nd and 98th percentiles of a band."""
    prov = lyr.dataProvider()
    stats = prov.bandStatistics(band)
    ce = QgsContrastEnhancement(prov.dataType(band))
    ce.setContrastEnhancementAlgorithm(QgsContrastEnhancement.StretchToMinimumMaximum)
    lo, hi = prov.cumulativeCut(band, 0.02, 0.98)
    ce.setMinimumValue(lo if lo is not None else stats.minimumValue)
    ce.setMaximumValue(hi if hi is not None else stats.maximumValue)
    return ce


def index_renderer(lyr, lo, hi):
    """A vegetation index: brown-red to dark green between ``lo`` and ``hi``."""
    stops = [(0.0, "#a50026"), (0.25, "#f46d43"), (0.5, "#fee08b"), (0.75, "#66bd63"), (1.0, "#006837")]
    ramp = QgsColorRampShader(lo, hi, None, QgsColorRampShader.Interpolated)
    ramp.setColorRampItemList([QgsColorRampShader.ColorRampItem(lo + t * (hi - lo), QColor(c), f"{lo + t * (hi - lo):.2f}")
                               for t, c in stops])
    shader = QgsRasterShader()
    shader.setRasterShaderFunction(ramp)
    return QgsSingleBandPseudoColorRenderer(lyr.dataProvider(), 1, shader)


def zones_renderer(lyr, n):
    colours = ["#d6604d", "#f7cb78", "#b2df8a", "#1a9850", "#006d2c", "#4575b4", "#984ea3", "#ff7f00", "#a65628",
               "#787878"]
    classes = [QgsPalettedRasterRenderer.Class(k, QColor(colours[(k - 1) % len(colours)]), f"classe {k}")
               for k in range(1, n + 1)]
    return QgsPalettedRasterRenderer(lyr.dataProvider(), 1, classes)
cfg = json.load(open(sys.argv[1], encoding="utf-8"))
app = QgsApplication([], False)
app.initQgis()
proj = QgsProject.instance()
crs = QgsCoordinateReferenceSystem(cfg["crs"]) if cfg.get("crs") else QgsCoordinateReferenceSystem()
update = cfg.get("mode") == "update"  # the project of the user updated in place, its other layers left as they are
if update:
    if not proj.read(cfg["project"]):
        print("PPG_QGIS_ERROR cannot read " + cfg["project"], flush=True)
        sys.exit(1)
else:
    if crs.isValid():
        proj.setCrs(crs)
    proj.setTitle(cfg.get("title", "pyarchinit-photogrammetry"))
root = proj.layerTreeRoot()


def group(name):
    g = root.findGroup(name)
    return g if g is not None else root.addGroup(name)


def key_of(lyr):
    """The identity the program gives its layers (io.qgis_styles.vector_key / raster_key)."""
    if lyr.type() == lyr.RasterLayer:
        return "raster|" + lyr.name()
    if lyr.providerType() != "ogr":
        return None
    parts = QgsProviderRegistry.instance().decodeUri("ogr", lyr.source())
    path = str(parts.get("path") or "").replace("\\", "/").split("/")[-1]
    table = parts.get("layerName") or ""
    return "vector|%s|%s|%s" % (path, table, lyr.subsetString() or "") if table else None


def load_style(lyr, path):
    """A style kept from QGIS (.qml): True when it could be read."""
    msg, ok = lyr.loadNamedStyle(path)
    return bool(ok)


def vector_style(lyr, v):
    sym = lyr.renderer().symbol() if lyr.renderer() is not None and hasattr(lyr.renderer(), "symbol") else None
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


def raster_style(lyr, r):
    if r.get("hillshade") and lyr.isValid():
        lyr.setRenderer(QgsHillshadeRenderer(lyr.dataProvider(), 1, 315, 45))
    elif r.get("bands") and lyr.isValid():  # three bands of a multi-band raster, stretched
        b = [int(v) for v in r["bands"]][:3]
        ren = QgsMultiBandColorRenderer(lyr.dataProvider(), b[0], b[1], b[2])
        ren.setRedContrastEnhancement(stretched(lyr, b[0]))
        ren.setGreenContrastEnhancement(stretched(lyr, b[1]))
        ren.setBlueContrastEnhancement(stretched(lyr, b[2]))
        lyr.setRenderer(ren)
    elif r.get("ramp") == "index" and lyr.isValid():
        lo, hi = r.get("limits") or lyr.dataProvider().cumulativeCut(1, 0.02, 0.98)
        lyr.setRenderer(index_renderer(lyr, float(lo), float(hi)))
    elif r.get("ramp") == "zones" and lyr.isValid():
        lyr.setRenderer(zones_renderer(lyr, int(r.get("classes") or 3)))
    elif r.get("ramp") in ("grey", "grey_inverted", "diverging", "thermal") and lyr.isValid():  # grids, temperatures
        lo, hi = r.get("limits") or lyr.dataProvider().cumulativeCut(1, 0.02, 0.98)
        lo, hi = float(lo), float(hi)
        if r["ramp"] == "thermal":  # iron, as the thermal cameras show it
            stops = [(0.0, "#000000"), (0.18, "#28006e"), (0.38, "#8c008c"), (0.55, "#dc2828"), (0.72, "#fa8200"),
                     (0.88, "#ffd728"), (1.0, "#ffffe6")]
        elif r["ramp"] == "diverging":
            stops = [(0.0, "#2166ac"), (0.5, "#f7f7f7"), (1.0, "#b2182b")]
        elif r["ramp"] == "grey_inverted":
            stops = [(0.0, "#ffffff"), (1.0, "#000000")]
        else:
            stops = [(0.0, "#000000"), (1.0, "#ffffff")]
        ramp = QgsColorRampShader(lo, hi, None, QgsColorRampShader.Interpolated)
        ramp.setColorRampItemList([QgsColorRampShader.ColorRampItem(lo + t * (hi - lo), QColor(c), f"{lo + t * (hi - lo):.3g}")
                                   for t, c in stops])
        shader = QgsRasterShader()
        shader.setRasterShaderFunction(ramp)
        lyr.setRenderer(QgsSingleBandPseudoColorRenderer(lyr.dataProvider(), int(r.get("band") or 1), shader))
    if r.get("opacity") is not None:
        lyr.setOpacity(float(r["opacity"]))


def styled_vector(lyr, v):
    """The style of a layer of the program: the one kept from QGIS, else the default the user saved in the
    GeoPackage (QGIS loaded it), else the program's."""
    if v.get("qml") and load_style(lyr, v["qml"]):
        return
    if not v.get("db_default"):
        vector_style(lyr, v)


def styled_raster(lyr, r):
    if r.get("qml") and load_style(lyr, r["qml"]):
        return
    raster_style(lyr, r)


def new_vector(v):
    lyr = QgsVectorLayer(v["path"] + "|layername=" + v["table"], v["name"], "ogr")
    if v.get("subset"):  # the rows of one layer of the program
        lyr.setSubsetString(v["subset"])
    for name, value in (v.get("defaults") or {}).items():  # what is drawn in it is of that layer
        idx = lyr.fields().indexOf(name)
        if idx >= 0:
            lyr.setDefaultValueDefinition(idx, QgsDefaultValue("'" + str(value).replace("'", "''") + "'"))
    if crs.isValid():
        lyr.setCrs(crs)
    styled_vector(lyr, v)
    lyr.setAutoRefreshInterval(int(cfg.get("refresh_ms", 1000)))
    lyr.setAutoRefreshEnabled(True)  # redraw: a GeoPackage is read again at every drawing
    proj.addMapLayer(lyr, False)
    group("Disegni (aggiornati dal vivo)").addLayer(lyr)
    return lyr


def new_raster(r):
    lyr = QgsRasterLayer(r["path"], r["name"])
    if crs.isValid():
        lyr.setCrs(crs)
    styled_raster(lyr, r)
    proj.addMapLayer(lyr, False)
    node = group("Prodotti fotogrammetrici").addLayer(lyr)
    if r.get("hidden"):
        node.setItemVisibilityChecked(False)
    return lyr


report = {"vectors": [], "rasters": [], "added": [], "removed": [], "restyled": []}
existing = {}
mine = set(cfg.get("ours", []))
if update:
    for lyr in proj.mapLayers().values():
        k = key_of(lyr)
        if k is not None and k not in existing:
            existing[k] = lyr
    wanted = {v["key"] for v in cfg["vectors"]} | {r["key"] for r in cfg["rasters"]}
    for k in mine:  # the program's layers no longer wanted (a layer renamed, a product deleted)
        if k in existing and k not in wanted:
            report["removed"].append(existing[k].name())
            proj.removeMapLayer(existing.pop(k).id())
if not update:  # the groups in their order: the drawings above the rasters
    group("Disegni (aggiornati dal vivo)")
    group("Prodotti fotogrammetrici")
for v in cfg["vectors"]:
    lyr = existing.get(v["key"])
    if lyr is None:
        lyr = new_vector(v)
        report["added"].append(v["name"])
    elif cfg.get("restyle") and v["key"] in mine:  # Ripristina gli stili predefiniti
        vector_style(lyr, v)
        report["restyled"].append(v["name"])
    report["vectors"].append({"name": v["name"], "valid": lyr.isValid(), "features": lyr.featureCount(),
                              "table": v["table"], "subset": lyr.subsetString()})
for r in cfg["rasters"]:
    lyr = existing.get(r["key"])
    if lyr is None:
        lyr = new_raster(r)
        report["added"].append(r["name"])
    elif r["key"] in mine:  # a raster of the user of the same name is left alone
        if lyr.source() != r["path"]:  # the product is another file now: the layer keeps its style
            lyr.setDataSource(r["path"], r["name"], "gdal")
        if cfg.get("restyle"):
            raster_style(lyr, r)
            report["restyled"].append(r["name"])
    report["rasters"].append({"name": r["name"], "valid": lyr.isValid()})
ok = proj.write(cfg["project"])
report["written"] = bool(ok)
print("PPG_QGIS_REPORT " + json.dumps(report), flush=True)
app.exitQgis()
'''

STYLES = {  # table -> (colour, label field, label colour)
    "unita": ("#e6194b", "etichetta", "#ffffff"),
    "poligoni": ("#8e44ad", "etichetta", "#3b1a4d"),
    "sezioni": ("#ffd000", "etichetta", "#7a5c00"),
    "curve": ("#4a3b2a", "quota", "#4a3b2a"),
    "linee": ("#00a0ff", "etichetta", "#003a66"),
    "punti": ("#27ae60", "etichetta", "#14532d"),
    "quote": ("#f59e0b", "quota_q", "#7c2d12"),
}


# what tells apart, in QGIS, the layers of one name whose drawings are in several tables (a layer of lines and
# polygons: two layers there)
TABLE_SUFFIX = {"unita": "unità", "poligoni": "poligoni", "sezioni": "sezioni", "curve": "curve", "linee": "linee",
                "punti": "punti", "quote": "quote"}
_SUBSET = re.compile(r"""^"layer" = '((?:[^']|'')*)'$""")
_NO_LAYER = """"layer" IS NULL OR "layer" = ''"""


def layer_subset(layer: str | None) -> str:
    """The filter (``setSubsetString``) of the rows of ``layer`` in a table of the live GeoPackage; of those without
    one (drawn in another program in a layer of the whole table) for ``""``/None."""
    return _NO_LAYER if not layer else "\"layer\" = '" + str(layer).replace("'", "''") + "'"


def subset_layer(subset: str | None) -> str | None:
    """The layer a filter of :func:`layer_subset` keeps (``""``: the rows without one); None for no filter - the
    whole table, as the projects up to 1.7.8 - and for another one."""
    subset = (subset or "").strip()
    if subset == _NO_LAYER:
        return ""
    m = _SUBSET.match(subset)
    return m.group(1).replace("''", "'") if m else None


def table_layers(path: str | Path, tables=None) -> dict[str, dict[str, int]]:
    """``{table: {layer: rows}}`` of the tables of the GeoPackage ``path`` that hold rows (``tables``: those looked
    at, by default every table with a column ``layer``), read only; ``""`` the rows without a layer. {} without the
    file."""
    from . import gpkg

    path = Path(path)
    if not gpkg.is_geopackage(path):
        return {}
    out: dict[str, dict[str, int]] = {}
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    try:
        names = [r[0] for r in con.execute("SELECT table_name FROM gpkg_geometry_columns ORDER BY table_name")]
        for table in (names if tables is None else [t for t in tables if t in names]):
            q = gpkg.quote(table)
            if "layer" not in [r[1] for r in con.execute(f"PRAGMA table_info({q})")]:
                continue
            rows = {str(name or ""): n for name, n in
                    con.execute(f"SELECT layer, COUNT(*) FROM {q} GROUP BY layer ORDER BY MIN(fid)")}
            if "" in rows and len(rows) > 1:  # NULL and '' are one
                rows[""] = con.execute(f"SELECT COUNT(*) FROM {q} WHERE layer IS NULL OR layer = ''").fetchone()[0]
            if rows:
                out[table] = rows
    finally:
        con.close()
    return out


def live_vectors(path: str | Path, tables=None) -> list[dict]:
    """The layers of a QGIS project for the drawings of the plan in the live GeoPackage ``path`` (``tables``: those
    of :data:`LIVE_TABLES` taken, by default all): one for each layer of the program that has rows there, named
    after it and filtered on it (``"subset"``), with its name as the default of the column ``layer`` (``"defaults"``:
    what is drawn in it in QGIS comes back in that layer). A layer whose drawings are in several tables - lines and
    polygons - makes one for each, told apart by the table (``Rilievo - linee``, ``Rilievo - poligoni``). A table
    without rows makes none; the rows without a layer one of the table's own name."""
    found = table_layers(path, list(LIVE_TABLES))  # all of them: a name is the same whatever tables are asked
    shared: dict[str, int] = {}
    for rows in found.values():
        for name in rows:
            shared[name] = shared.get(name, 0) + 1
    out = []
    for table in LIVE_TABLES:
        if tables is not None and table not in tables:
            continue
        title, geometry = LIVE_TABLES[table]
        color, label, label_color = STYLES[table]
        for name in found.get(table, {}):
            shown = title if not name else f"{name} - {TABLE_SUFFIX[table]}" if shared[name] > 1 else name
            out.append({"name": shown, "path": str(path), "table": table, "geometry": geometry, "color": color,
                        "label": label, "label_color": label_color, "layer": name, "subset": layer_subset(name),
                        **({"defaults": {"layer": name}} if name else {})})
    return out


def project_layers(project_path: str | Path) -> set[tuple[str, str | None]] | None:
    """``(table, layer)`` of the layers of the QGIS project ``project_path`` that read the live GeoPackage
    (:data:`LIVE_DB`): the layer of their filter (:func:`subset_layer`; None: the whole table, a project made up to
    1.7.8). None when the project cannot be read (no file, a ``.qgz``, not XML)."""
    from xml.etree import ElementTree

    try:
        root = ElementTree.parse(str(project_path)).getroot()
    except (OSError, ElementTree.ParseError):
        return None
    out = set()
    for node in root.iter("datasource"):
        source, _, subset = (node.text or "").partition("|subset=")
        file, _, table = source.partition("|layername=")
        if Path(file.replace("\\", "/")).name == LIVE_DB and table:
            out.add((table.partition("|")[0], subset_layer(subset)))
    return out


def layers_not_in_project(have: set | None, vectors: list[dict]) -> list[str]:
    """The names of the layers ``vectors`` (:func:`live_vectors`, :meth:`LiveLink.free_vectors`) that a QGIS project
    with the layers ``have`` (:func:`project_layers`) does not show: made, ticked or renamed after the project was
    written - *Apri in QGIS* writes it again with them. A table the project shows whole (up to 1.7.8) misses none;
    [] for a project that could not be read."""
    if have is None:
        return []
    whole = {table for table, layer in have if layer is None}
    return [v["name"] for v in vectors if v["table"] not in whole and (v["table"], v.get("layer")) not in have]


def project_crs(crs: str | None) -> str:
    """The system a QGIS project and its layers get for the chunk's ``crs``: ``EPSG:<code>`` of its plan - QGIS 3 does
    not read a compound system such as ``EPSG:7792+3855`` (heights above a geoid), and the layers are 2D
    (:func:`..core.crs.epsg_code`) -, or ``""`` (none) for a local frame and a system without an EPSG code."""
    from ..core import crs as crs_mod

    code = crs_mod.epsg_code(crs)
    return f"EPSG:{code}" if code else ""


def build_project(project_path: str | Path, crs: str, layer_files: dict[str, Path], rasters: list[dict],
                  title: str = "pyarchinit-photogrammetry", refresh_ms: int = 1000,
                  qgis: QgisInstall | None = None, extra_vectors: list[dict] | None = None,
                  restyle: bool = False) -> dict:
    """Write a QGIS project with the live drawings (``{table: GeoPackage}`` from
    :func:`export_layers`, redrawn every ``refresh_ms``: one QGIS layer for each layer of the program that has
    drawings there, :func:`live_vectors` - a table without rows makes none), the rasters
    (``[{"name", "path", "hillshade"?, "opacity"?, "bands"? (three, 1-based, stretched), "ramp"?
    ("index" with "limits", or "zones" with "classes")}]``) and other GeoPackage layers
    (``extra_vectors``: ``[{"name", "path", "table", "geometry", "color", "label"?, "subset"?, "defaults"?}]``, the zones
    of a multispectral index for one). Needs QGIS's Python; returns the report of the layers
    QGIS loaded.

    The project of the user is kept (:mod:`.qgis_styles`): an existing project that shows every layer is not written
    at all (``report["kept"]`` True); one that misses layers is updated in place (the missing ones added, the
    program's layers no longer wanted removed, rasters pointed at their file), its other layers and styles left as
    they are. The styles changed by hand in QGIS are kept first as ``.qml`` files and given to the layers written
    again. ``restyle``: *Ripristina gli stili predefiniti* - the kept styles forgotten, the program's layers in the
    project given the program's style again."""
    from . import qgis_styles as styles

    project_path = Path(project_path)
    vectors = []
    by_file: dict[str, list] = {}
    for table, path in layer_files.items():
        by_file.setdefault(str(path), []).append(table)
    for path, tables in by_file.items():  # one layer for each layer of the program with drawings (live_vectors)
        vectors += live_vectors(path, tables)
    for v in extra_vectors or []:
        if v.get("path") and Path(v["path"]).exists():
            # the style by the 2D type: the 3D tables of the free drawings (LINESTRINGZ...) are drawn as the others
            vectors.append({"label": "", "label_color": "#202020", **v,
                            "geometry": str(v.get("geometry") or "").upper().removesuffix("Z")})
    rasters = [r for r in rasters if r.get("path") and Path(r["path"]).exists()]
    if restyle:
        styles.reset(project_path)
    else:
        styles.harvest(project_path)  # the styles made by hand in QGIS, before anything is written
    # an existing project is updated in place by QGIS (also one this XML reader cannot read: QGIS may)
    present = styles.project_layer_keys(project_path) if project_path.exists() else None
    mine = styles.ours(project_path)
    if not mine and present:  # a project written before 1.9.1 (no record): the layers of the program's files are its
        names = {Path(str(v["path"]).replace(chr(92), "/")).name for v in vectors}
        raster_keys = {styles.raster_key(r["name"]) for r in rasters}
        mine = {k for k in present if (k.startswith("vector|") and k.split("|")[1] in names) or k in raster_keys}
    user_defaults: dict[str, set] = {}
    for v in vectors:
        v["key"] = styles.vector_key(v["path"], v["table"], v.get("subset"))
        qml = styles.qml_for(project_path, v["key"])
        if qml is not None:
            v["qml"] = str(qml)
        if v["path"] not in user_defaults:
            user_defaults[v["path"]] = styles.user_default_tables(v["path"])
        if v["table"] in user_defaults[v["path"]]:
            v["db_default"] = True
    for r in rasters:
        r["key"] = styles.raster_key(r["name"])
        qml = styles.qml_for(project_path, r["key"])
        if qml is not None:
            r["qml"] = str(qml)
    wanted = [v["key"] for v in vectors] + [r["key"] for r in rasters]
    if present is not None and not restyle:
        stale = (mine & set(present)) - set(wanted)
        moved = [r for r in rasters if r["key"] in present and r["key"] in mine
                 and not _same_file(present[r["key"]].findtext("datasource"), r["path"], project_path)]
        if all(k in present for k in wanted) and not stale and not moved:  # nothing to do: the project as it is
            return {"written": False, "kept": True, "added": [], "removed": [], "restyled": [],
                    "vectors": [{"name": v["name"], "valid": True, "table": v["table"], "subset": v.get("subset", "")}
                                for v in vectors],
                    "rasters": [{"name": r["name"], "valid": True} for r in rasters]}
    cfg = {"project": str(project_path), "crs": project_crs(crs), "vectors": vectors, "rasters": rasters,
           "title": title, "refresh_ms": refresh_ms, "mode": "update" if project_path.exists() else "new",
           "ours": sorted(mine), "restyle": bool(restyle)}
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
    styles.record_project(project_path, wanted)  # the styles as written now: a later change is the user's
    return report


def _same_file(datasource: str | None, path: str, project: Path) -> bool:
    """Whether a raster's ``datasource`` in a ``.qgs`` (absolute, or relative to the project) is the file ``path``."""
    if not datasource:
        return False
    src = Path(datasource.split("|")[0])
    if not src.is_absolute():
        src = project.parent / src
    try:
        return src.resolve() == Path(path).resolve()
    except OSError:
        return False


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


__all__ = ["find_qgis", "export_layers", "build_project", "project_crs", "open_in_qgis", "classify_layer", "LiveLink", "LIVE_DB",
           "INFO_TABLE", "UnreadEdits", "ELEVATION_PREFIX", "ELEVATION_TABLES",
           "LIVE_TABLES", "QgisInstall", "TWO_WAY_TABLES", "ID_FIELD", "SIG_FIELD", "PART_FIELD", "Feature",
           "TableChanges", "new_id", "stable_id", "row_signature", "PLAN_TABLES", "plan_identities", "two_way_tables",
           "feature_kind", "legacy_identities", "live_vectors", "table_layers", "layer_subset", "subset_layer",
           "project_layers", "layers_not_in_project", "TABLE_SUFFIX"]
