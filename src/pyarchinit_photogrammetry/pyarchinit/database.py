"""QGIS-free access to a PyArchInit database (SQLite/SpatiaLite or PostgreSQL/PostGIS).

Tables are reflected with SQLAlchemy, so the bridge follows the schema of the
database it opens (5.x, Stratigraph branch included) instead of hard-coding it.
Conventions mirrored from PyArchInit:

* ids are application-assigned (``max + 1``), there are no sequences;
* ``entity_uuid`` (UUID v4) is filled when the column exists;
* geometries are written with ``GeomFromText`` (SpatiaLite) / ``ST_GeomFromText``
  (PostGIS) in the SRID declared in ``geometry_columns``.
"""
from __future__ import annotations

import glob
import os

import uuid
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import MetaData, Table, create_engine, event, func, select, text

from .config import PyArchInitConfig

SPATIALITE_CANDIDATES = [
    "mod_spatialite",
    "/opt/local/lib/mod_spatialite.dylib",               # MacPorts
    "/opt/homebrew/lib/mod_spatialite.dylib",            # Homebrew (Apple Silicon)
    "/usr/local/lib/mod_spatialite.dylib",               # Homebrew (Intel)
    "/Applications/QGIS*.app/Contents/MacOS/lib/mod_spatialite.so",
    "/Applications/QGIS*.app/Contents/Frameworks/mod_spatialite.so",
    "/usr/lib/x86_64-linux-gnu/mod_spatialite.so",
    "/usr/lib/aarch64-linux-gnu/mod_spatialite.so",
    r"C:\OSGeo4W\bin\mod_spatialite.dll",
    r"C:\Program Files\QGIS*\bin\mod_spatialite.dll",
]


def _spatialite_paths() -> list[str]:
    env = os.environ.get("SPATIALITE_LIBRARY_PATH")
    out = [env] if env else []
    for cand in SPATIALITE_CANDIDATES:
        if "*" in cand:
            out += sorted(glob.glob(cand))
        elif not os.path.isabs(cand) or os.path.exists(cand):
            out.append(cand)
    return out


def _warm_up_proj() -> None:
    """Initialise the PROJ/GDAL bundled with rasterio and pyproj *before* SpatiaLite.

    mod_spatialite (system or QGIS build) brings its own PROJ; if it is loaded first,
    the PROJ context of the rasterio wheel can crash the process (std::bad_alloc).
    Initialising rasterio/pyproj first avoids the clash.
    """
    try:
        import pyproj
        from rasterio.crs import CRS

        CRS.from_epsg(4326).to_wkt()
        pyproj.CRS.from_epsg(4326).to_wkt()
    except Exception:  # pragma: no cover
        pass


class PyArchInitDB:
    def __init__(self, url: str | None = None, config: PyArchInitConfig | None = None, echo: bool = False):
        if config is None:
            try:  # thumbnails/media folders come from PyArchInit settings even with an explicit URL
                config = PyArchInitConfig.load()
            except (FileNotFoundError, OSError, SyntaxError, ValueError):
                if url is None:
                    raise
        self.config = config
        url = url or config.url()
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        self.url = url
        self.is_postgres = self.url.startswith("postgres")
        self.spatialite = False
        if not self.is_postgres:
            _warm_up_proj()
        self.engine = create_engine(self.url, echo=echo, future=True)
        if not self.is_postgres:
            event.listen(self.engine, "connect", self._load_spatialite)
        self.metadata = MetaData()
        self._tables: dict[str, Table] = {}
        with self.engine.connect():
            pass

    # ------------------------------------------------------------ plumbing
    def _load_spatialite(self, dbapi_conn, _record):
        dbapi_conn.enable_load_extension(True)
        for path in _spatialite_paths():
            try:
                dbapi_conn.load_extension(path)
                self.spatialite = True
                break
            except Exception:
                continue
        dbapi_conn.enable_load_extension(False)

    @classmethod
    def from_sqlite(cls, path: str | Path) -> "PyArchInitDB":
        return cls(f"sqlite:///{Path(path).resolve()}")

    def table(self, name: str) -> Table:
        if name not in self._tables:
            self._tables[name] = Table(name, self.metadata, autoload_with=self.engine)
        return self._tables[name]

    def has_table(self, name: str) -> bool:
        from sqlalchemy import inspect

        return inspect(self.engine).has_table(name)

    def columns(self, name: str) -> set[str]:
        return set(self.table(name).c.keys())

    @contextmanager
    def begin(self):
        with self.engine.begin() as conn:
            yield conn

    def next_id(self, conn, table: str, column: str) -> int:
        t = self.table(table)
        cur = conn.execute(select(func.max(t.c[column]))).scalar()
        return int(cur or 0) + 1

    def srid(self, table: str, column: str = "the_geom") -> int:
        with self.engine.connect() as conn:
            if self.is_postgres:
                q = text("SELECT srid FROM geometry_columns WHERE f_table_name = :t AND f_geometry_column = :c")
            else:
                q = text("SELECT srid FROM geometry_columns WHERE lower(f_table_name) = lower(:t) "
                         "AND lower(f_geometry_column) = lower(:c)")
            v = conn.execute(q, {"t": table, "c": column}).scalar()
        return int(v) if v is not None else -1

    def _prepare(self, table: str, values: dict) -> dict:
        cols = self.columns(table)
        row = {k: v for k, v in values.items() if k in cols}
        if "entity_uuid" in cols and not row.get("entity_uuid"):
            row["entity_uuid"] = str(uuid.uuid4())
        return row

    def _uses_sequence(self, table: str, id_column: str) -> bool:
        """PostgreSQL id columns backed by a sequence must take ``nextval`` (QGIS relies on it)."""
        if not self.is_postgres:
            return False
        col = self.table(table).c[id_column]
        return col.server_default is not None and "nextval" in str(getattr(col.server_default, "arg", ""))

    def _assign_id(self, conn, table: str, id_column: str | None, row: dict):
        """Returns ``(id or None, use_returning)``; fills ``row[id_column]`` with max+1 when needed."""
        if not id_column:
            return None, False
        if self._uses_sequence(table, id_column):
            return None, True
        new_id = self.next_id(conn, table, id_column)
        row[id_column] = new_id
        return new_id, False

    def insert(self, conn, table: str, values: dict, id_column: str | None = None) -> int | None:
        """Insert a row; assigns ``id_column = max + 1`` (or the PostgreSQL sequence). Returns the id."""
        row = self._prepare(table, values)
        new_id, returning = self._assign_id(conn, table, id_column, row)
        stmt = self.table(table).insert().values(**row)
        if returning:
            return int(conn.execute(stmt.returning(self.table(table).c[id_column])).scalar())
        conn.execute(stmt)
        return new_id

    def insert_geometry(self, conn, table: str, values: dict, wkt: str, srid: int | None = None,
                        geom_column: str = "the_geom", id_column: str | None = "gid", source_srid: int | None = None,
                        multi: bool = False) -> int | None:
        """Insert a row with a geometry given as WKT (in ``source_srid``, default = layer SRID)."""
        if not self.is_postgres and not self.spatialite:
            raise RuntimeError("writing geometries in SQLite needs mod_spatialite; set SPATIALITE_LIBRARY_PATH "
                               "to the library shipped with QGIS")
        target = self.srid(table, geom_column) if srid is None else srid
        if source_srid and target and target > 0 and source_srid != target:
            wkt = reproject_wkt(wkt, source_srid, target)  # pyproj: no dependency on spatial_ref_sys
        if self.is_postgres:
            expr = "ST_GeomFromText(:wkt, :srid)"
            if multi:
                expr = f"ST_Multi({expr})"
        else:
            expr = "GeomFromText(:wkt, :srid)"
            if multi:
                expr = f"CastToMulti({expr})"
        row = self._prepare(table, values)
        new_id, returning = self._assign_id(conn, table, id_column, row)
        cols = ", ".join(f'"{c}"' for c in row) + f', "{geom_column}"'
        params = ", ".join(f":{c}" for c in row)
        sql = f'INSERT INTO "{table}" ({cols}) VALUES ({params}, {expr})'
        if returning:
            sql += f' RETURNING "{id_column}"'
            return int(conn.execute(text(sql), {**row, "wkt": wkt, "srid": int(target)}).scalar())
        conn.execute(text(sql), {**row, "wkt": wkt, "srid": int(target)})
        return new_id

    def geometry_columns(self, table: str) -> set[str]:
        """Names of the geometry columns of a table (excluded from plain queries:
        e.g. SQLite reflects ``LINESTRING`` as an integer type because of "INT")."""
        if not hasattr(self, "_geom_cols"):
            with self.engine.connect() as conn:
                try:
                    rows = conn.execute(text("SELECT f_table_name, f_geometry_column FROM geometry_columns")).all()
                except Exception:
                    rows = []
            self._geom_cols: dict[str, set[str]] = {}
            for t_name, col in rows:
                self._geom_cols.setdefault(str(t_name).lower(), set()).add(str(col))
        return self._geom_cols.get(table.lower(), set())

    def query(self, table: str, **filters) -> list[dict]:
        t = self.table(table)
        geom = self.geometry_columns(table)
        stmt = select(*[c for c in t.c if c.name not in geom])
        for k, v in filters.items():
            if v is not None:
                stmt = stmt.where(t.c[k] == v)
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(stmt)]

    def geometries_wkt(self, table: str, geom_column: str = "the_geom", **filters) -> list[dict]:
        fn = "ST_AsText" if self.is_postgres else "AsText"
        where = " AND ".join(f'"{k}" = :{k}' for k, v in filters.items() if v is not None)
        sql = f'SELECT *, {fn}("{geom_column}") AS wkt FROM "{table}"' + (f" WHERE {where}" if where else "")
        with self.engine.connect() as conn:
            rows = [dict(r._mapping) for r in conn.execute(text(sql), {k: v for k, v in filters.items() if v is not None})]
        for r in rows:
            r.pop(geom_column, None)
        return rows


def reproject_wkt(wkt: str, src_epsg: int, dst_epsg: int) -> str:
    from pyproj import Transformer
    from shapely import wkt as swkt
    from shapely.ops import transform

    tr = Transformer.from_crs(int(src_epsg), int(dst_epsg), always_xy=True)
    return transform(tr.transform, swkt.loads(wkt)).wkt


def connect(url: str | None = None, config_path: str | Path | None = None) -> PyArchInitDB:
    """Open the database configured in PyArchInit (``config.cfg``) or a given URL/SQLite path."""
    if url and not url.startswith(("sqlite:", "postgresql:", "postgres:")):
        return PyArchInitDB.from_sqlite(url)
    if url:
        return PyArchInitDB(url)
    return PyArchInitDB(config=PyArchInitConfig.load(config_path))


__all__ = ["PyArchInitDB", "connect"]
