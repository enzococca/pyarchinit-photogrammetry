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
and rows deleted; the program never overwrites them before it has read them. The
other tables are computed by the program (contour lines, zones, plugin drawings) and stay
one-way.

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
ID_FIELD = "ppg_id"  # the identity of a feature (a UUID)
SIG_FIELD = "ppg_sig"  # the signature of what the program last wrote in the row
PART_FIELD = "ppg_parte"  # "unita": the part (1, 2...) of a unit drawn as several polygons
TECHNICAL_FIELDS = (ID_FIELD, SIG_FIELD, PART_FIELD)
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
    """Which live table a layer of the interface goes to. Polygons: the layers of stratigraphic
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
        self._known: dict[str, dict[tuple, int]] = {}  # table -> {(identity, part): fid} written or read here
        self._con: sqlite3.Connection | None = None
        self._ident = None  # the file the connection is open on
        self._data_version = None

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
    def _group(layers: dict[str, dict]) -> dict[str, list]:
        out: dict[str, list] = {t: [] for t in LIVE_TABLES}
        for name, lay in layers.items():
            for feat in lay["features"]:
                attrs = feat[-1] if isinstance(feat, (tuple, list)) else feat
                if isinstance(attrs, dict) and attrs.get("_geom"):
                    out[classify_layer(name, lay["kind"], attrs)].append((name, attrs))
        return out

    @staticmethod
    def _signature(feats: list) -> int:
        sig = []
        for name, attrs in feats:
            gtype, coords = attrs["_geom"]
            n = len(coords) if coords is not None else 0
            sig.append((name, id(attrs["_geom"]), id(attrs.get("_shape")), gtype, n, str(coords[0]) if n else "",
                        str(coords[-1]) if n else "",
                        tuple(sorted((str(k), str(v)) for k, v in attrs.items() if not str(k).startswith("_")))))
        return hash(tuple(sig))

    def write(self, layers: dict[str, dict], force: bool = False) -> dict[str, Path]:
        """Bring the GeoPackage up to date with the layers of the raster view
        (``{name: {"kind", "features"}}``, features as attribute dicts with ``_geom`` or the
        ``(item, label, attrs)`` triples of :class:`gui.image_views.RasterView`). Every table
        exists even when empty, so QGIS sees deletions. ``force``: every table compared row by
        row, also those whose features did not change here (an edit made in QGIS to a one-way
        table is undone). Rows of the two-way tables edited or deleted in QGIS and not read back
        yet are left as they are."""
        from . import gpkg

        groups = self._group(layers)
        new = not gpkg.is_geopackage(self.path)
        if new:
            gpkg.refuse_foreign(self.path)  # never write the drawings into another file of that name
            self.close()
        con = self._connection()
        if new:
            self._signatures = {}
        todo = {t: f for t, f in groups.items() if force or new or self._signatures.get(t) != self._signature(f)}
        if todo:
            srs = gpkg.srs_of(self.crs)
            known = {}
            try:
                con.execute("BEGIN IMMEDIATE")
                gpkg.create(con, srs)  # idempotent: the system of the chunk listed, also after it changed
                for table, feats in todo.items():
                    known[table] = self._write_table(con, table, feats, srs[0], force)
                con.execute("COMMIT")
            except BaseException:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
            self._known.update(known)
            for table, feats in todo.items():
                self._signatures[table] = self._signature(feats)
        return {t: self.path for t in LIVE_TABLES}

    def _wanted(self, table: str, feats: list, cols: list[str], types: dict) -> list[_Wanted]:
        import shapely

        counts: dict[str, int] = {}
        out = []
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
            vals[ID_FIELD] = pid
            if table == "unita":
                vals[PART_FIELD] = part
            values = {c: _bind(_affinity(types.get(c, "")), _value(c, vals.get(c))) for c in cols if c != SIG_FIELD}
            out.append(_Wanted((pid, part), geom, raw, values, row_signature(raw, values)))
        return out

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

    def _write_table(self, con, table: str, feats: list, srs_id: int, force: bool) -> dict:
        """The rows of ``table`` brought to ``feats`` one by one; returns ``{key: fid}`` of its rows."""
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
        two_way = table in TWO_WAY_TABLES
        known = {} if recreated else self._known.get(table, {})
        primary, others = self._sort(self._scan(con, table, cols, content=two_way or force), known)
        written = [c for c in cols if c != SIG_FIELD]
        names = "".join(f", {q(c)}" for c in written)
        insert = f"INSERT INTO {q(table)} (geom{names}) VALUES ({', '.join('?' * (len(written) + 1))})"
        update = f"UPDATE {q(table)} SET geom = ?{''.join(f', {q(c)} = ?' for c in written)} WHERE fid = ?"
        touched, after, changed = [], {}, False
        for w in self._wanted(table, feats, cols, types):
            s = primary.pop(w.key, None)
            row = [gpkg.blob(w.geom, srs_id)] + [w.values.get(c) for c in written]
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
        for key, s in primary.items():  # rows of features gone here
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

    def read_changes(self, busy_ms: int = 250) -> dict[str, TableChanges]:
        """What another program (QGIS) changed in the two-way tables since the last write or read:
        ``{table: TableChanges}``, only the tables with changes. The rows drawn there get their
        identity and every row read its signature, in the same transaction: a change is given once.
        :class:`sqlite3.OperationalError` (database locked) when QGIS holds the file: try again."""
        from . import gpkg

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
                for table in TWO_WAY_TABLES:
                    cols = [r[1] for r in con.execute(f"PRAGMA table_info({q(table)})") if r[1] not in ("fid", "geom")]
                    if ID_FIELD not in cols:
                        continue  # not written by this version yet
                    have = gpkg.table_srs(con, table)
                    if have is not None and have != srs:
                        out[table] = TableChanges(refused=(have, srs))
                        continue
                    ch, known[table] = self._read_table(con, table, cols)
                    if ch:
                        out[table] = ch
                con.execute("COMMIT")
            except BaseException:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
        finally:
            con.execute("PRAGMA busy_timeout = 10000")
        self._data_version = version
        self._known.update(known)
        return out

    def _read_table(self, con, table: str, cols: list[str]) -> tuple[TableChanges, dict]:
        from . import gpkg

        q = gpkg.quote
        known = self._known.get(table, {})
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
                       QgsColorRampShader, QgsRasterShader, QgsPalettedRasterRenderer, QgsContrastEnhancement)
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
    "poligoni": ("#8e44ad", "etichetta", "#3b1a4d"),
    "sezioni": ("#ffd000", "etichetta", "#7a5c00"),
    "curve": ("#4a3b2a", "quota", "#4a3b2a"),
    "linee": ("#00a0ff", "etichetta", "#003a66"),
    "punti": ("#27ae60", "etichetta", "#14532d"),
    "quote": ("#f59e0b", "quota_q", "#7c2d12"),
}


def project_crs(crs: str | None) -> str:
    """The system a QGIS project and its layers get for the chunk's ``crs``: ``EPSG:<code>`` of its plan - QGIS 3 does
    not read a compound system such as ``EPSG:7792+3855`` (heights above a geoid), and the layers are 2D
    (:func:`..core.crs.epsg_code`) -, or ``""`` (none) for a local frame and a system without an EPSG code."""
    from ..core import crs as crs_mod

    code = crs_mod.epsg_code(crs)
    return f"EPSG:{code}" if code else ""


def build_project(project_path: str | Path, crs: str, layer_files: dict[str, Path], rasters: list[dict],
                  title: str = "pyarchinit-photogrammetry", refresh_ms: int = 1000,
                  qgis: QgisInstall | None = None, extra_vectors: list[dict] | None = None) -> dict:
    """Write a QGIS project with the live drawings (``{table: GeoPackage}`` from
    :func:`export_layers`, redrawn every ``refresh_ms``), the rasters
    (``[{"name", "path", "hillshade"?, "opacity"?, "bands"? (three, 1-based, stretched), "ramp"?
    ("index" with "limits", or "zones" with "classes")}]``) and other GeoPackage layers
    (``extra_vectors``: ``[{"name", "path", "table", "geometry", "color", "label"?}]``, the zones
    of a multispectral index for one). Needs QGIS's Python; returns the report of the layers
    QGIS loaded."""
    qgis = qgis or find_qgis()
    if qgis is None or not qgis.python:
        raise RuntimeError("QGIS non trovato: installalo (qgis.org) o indica il percorso con PPG_QGIS")
    vectors = []
    for table, path in layer_files.items():
        title_v, geometry = LIVE_TABLES[table]
        color, label, label_color = STYLES[table]
        vectors.append({"name": title_v, "path": str(path), "table": table, "geometry": geometry, "color": color,
                        "label": label, "label_color": label_color})
    for v in extra_vectors or []:
        if v.get("path") and Path(v["path"]).exists():
            vectors.append({"label": "", "label_color": "#202020", **v})
    cfg = {"project": str(project_path), "crs": project_crs(crs),
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


__all__ = ["find_qgis", "export_layers", "build_project", "project_crs", "open_in_qgis", "classify_layer", "LiveLink", "LIVE_DB",
           "LIVE_TABLES", "QgisInstall", "TWO_WAY_TABLES", "ID_FIELD", "SIG_FIELD", "PART_FIELD", "Feature",
           "TableChanges", "new_id", "stable_id", "row_signature"]
