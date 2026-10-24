"""Minimal GeoPackage writer (OGC GeoPackage 1.2) on plain ``sqlite3``: feature tables of 2D
geometries with text, real and integer attributes. No GDAL is needed; QGIS and GDAL read the
result. Tables are rewritten in one transaction, so a reader (QGIS with the file open)
never sees half a table."""
from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

GPKG_APPLICATION_ID = 0x47504B47  # 'GPKG'

_SCHEMA = """
CREATE TABLE IF NOT EXISTS gpkg_spatial_ref_sys (srs_name TEXT NOT NULL, srs_id INTEGER NOT NULL PRIMARY KEY,
  organization TEXT NOT NULL, organization_coordsys_id INTEGER NOT NULL, definition TEXT NOT NULL, description TEXT);
CREATE TABLE IF NOT EXISTS gpkg_contents (table_name TEXT NOT NULL PRIMARY KEY, data_type TEXT NOT NULL,
  identifier TEXT UNIQUE, description TEXT DEFAULT '',
  last_change DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE, srs_id INTEGER,
  CONSTRAINT fk_gc_r_srs_id FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys(srs_id));
CREATE TABLE IF NOT EXISTS gpkg_geometry_columns (table_name TEXT NOT NULL, column_name TEXT NOT NULL,
  geometry_type_name TEXT NOT NULL, srs_id INTEGER NOT NULL, z TINYINT NOT NULL, m TINYINT NOT NULL,
  CONSTRAINT pk_geom_cols PRIMARY KEY (table_name, column_name),
  CONSTRAINT uk_gc_table_name UNIQUE (table_name),
  CONSTRAINT fk_gc_tn FOREIGN KEY (table_name) REFERENCES gpkg_contents(table_name),
  CONSTRAINT fk_gc_srs FOREIGN KEY (srs_id) REFERENCES gpkg_spatial_ref_sys (srs_id))
"""


def quote(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def srs_of(crs: str | None) -> tuple[int, str, str]:
    """``(srs_id, name, WKT)`` of a CRS string; ``-1`` (undefined cartesian) for local frames."""
    from ..core import crs as crs_mod

    code = crs_mod.epsg_code(crs)
    if code is None:
        return -1, "", ""
    from pyproj import CRS

    c = CRS.from_epsg(code)
    return code, c.name, c.to_wkt("WKT1_GDAL")


def blob(geom, srs_id: int) -> bytes:
    """GeoPackage binary geometry: header (little endian, xy envelope) + 2D WKB."""
    import shapely

    minx, miny, maxx, maxy = geom.bounds
    head = b"GP" + bytes([0, 0b00000011]) + struct.pack("<i", srs_id) + struct.pack("<4d", minx, maxx, miny, maxy)
    return head + shapely.to_wkb(geom, byte_order=1, output_dimension=2)


def is_geopackage(path: str | Path) -> bool:
    """Whether ``path`` is a GeoPackage with its metadata tables (read only: never creates it)."""
    path = Path(path)
    if not path.exists():
        return False
    try:
        con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        try:
            return con.execute("SELECT 1 FROM sqlite_master WHERE name = 'gpkg_contents'").fetchone() is not None
        finally:
            con.close()
    except sqlite3.Error:
        return False


def refuse_foreign(path: str | Path) -> None:
    """:class:`FileExistsError` when ``path`` is a file with content that is not a GeoPackage: the
    program never deletes or rewrites a file it did not make."""
    path = Path(path)
    if path.exists() and (path.is_dir() or path.stat().st_size) and not is_geopackage(path):
        raise FileExistsError(f"{path} exists and is not a GeoPackage: it was left as it is, choose another file")


def connect(path: str | Path) -> sqlite3.Connection:
    """Connection in autocommit mode (transactions are explicit), waiting for other writers."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path, timeout=10, isolation_level=None)


def create(con: sqlite3.Connection, srs: tuple[int, str, str]) -> None:
    """GeoPackage metadata tables and spatial reference systems (idempotent)."""
    from pyproj import CRS

    con.execute(f"PRAGMA application_id = {GPKG_APPLICATION_ID}")
    con.execute("PRAGMA user_version = 10200")
    for statement in _SCHEMA.split(";"):  # not executescript(): it would commit an open transaction
        if statement.strip():
            con.execute(statement)
    rows = [("Undefined cartesian SRS", -1, "NONE", -1, "undefined", "undefined cartesian coordinate reference system"),
            ("Undefined geographic SRS", 0, "NONE", 0, "undefined", "undefined geographic coordinate reference system"),
            ("WGS 84 geodetic", 4326, "EPSG", 4326, CRS.from_epsg(4326).to_wkt("WKT1_GDAL"),
             "longitude/latitude coordinates in decimal degrees on the WGS 84 spheroid")]
    if srs[0] > 0 and srs[0] != 4326:
        rows.append((srs[1], srs[0], "EPSG", srs[0], srs[2], ""))
    # the columns named: GDAL and QGIS may add definition_12_063 (CRS WKT extension) to this table
    con.executemany("INSERT OR IGNORE INTO gpkg_spatial_ref_sys (srs_name, srs_id, organization, "
                    "organization_coordsys_id, definition, description) VALUES (?, ?, ?, ?, ?, ?)", rows)


def ensure_table(con: sqlite3.Connection, table: str, geometry_type: str, columns: dict[str, str], srs_id: int,
                 identifier: str | None = None) -> list[str]:
    """Create a feature table (``columns``: name -> SQL type) or add its missing columns;
    returns the attribute columns of the table, in order."""
    have = [r[1] for r in con.execute(f"PRAGMA table_info({quote(table)})")]
    if not have:
        cols = "".join(f", {quote(c)} {t}" for c, t in columns.items())
        con.execute(f"CREATE TABLE {quote(table)} (fid INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, "
                    f"geom {geometry_type}{cols})")
        con.execute("INSERT INTO gpkg_contents (table_name, data_type, identifier, srs_id) VALUES (?, 'features', ?, ?)",
                    (table, identifier or table, srs_id))
        con.execute("INSERT INTO gpkg_geometry_columns VALUES (?, 'geom', ?, ?, 0, 0)", (table, geometry_type, srs_id))
        have = ["fid", "geom", *columns]
    for c, t in columns.items():
        if c not in have:
            con.execute(f"ALTER TABLE {quote(table)} ADD COLUMN {quote(c)} {t}")
            have.append(c)
    return [c for c in have if c not in ("fid", "geom")]


def replace_rows(con: sqlite3.Connection, table: str, columns: list[str], features, srs_id: int) -> int:
    """Replace the content of ``table`` with ``features`` (``(shapely geometry, {column:
    value})``, empty or missing geometries skipped) and update its extent; returns the
    number of rows written. Call inside a transaction."""
    rows, bounds = [], []
    for geom, values in features:
        if geom is None or geom.is_empty:
            continue
        rows.append([blob(geom, srs_id)] + [values.get(c) for c in columns])
        bounds.append(geom.bounds)
    con.execute(f"DELETE FROM {quote(table)}")
    names = "".join(f", {quote(c)}" for c in columns)
    con.executemany(f"INSERT INTO {quote(table)} (geom{names}) VALUES ({', '.join('?' * (len(columns) + 1))})", rows)
    set_extent(con, table, bounds)
    return len(rows)


def write_table(path: str | Path, table: str, geometry_type: str, columns: dict[str, str], features,
                crs: str | None, identifier: str | None = None) -> Path:
    """Write one feature table into a GeoPackage (created when missing; other tables kept). A file
    that exists and is not a GeoPackage - another database, a document chosen by mistake - is never
    touched: :class:`FileExistsError`. An empty file (left by a save dialog) is used."""
    path = Path(path)
    srs = srs_of(crs)
    new = not is_geopackage(path)
    if new and path.exists():
        refuse_foreign(path)
        path.unlink()  # empty: nothing to lose
    con = connect(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        create(con, srs)
        cols = ensure_table(con, table, geometry_type, columns, srs[0], identifier)
        replace_rows(con, table, cols, features, srs[0])
        con.execute("COMMIT")
    except BaseException:
        if con.in_transaction:
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    return path


def wkb(data: bytes | None) -> bytes | None:
    """The WKB of a GeoPackage binary geometry (its header and envelope left out); None when
    empty or not a GeoPackage geometry."""
    if data is None or len(data) < 8 or bytes(data[:2]) != b"GP":
        return None
    if data[3] & 0b10000:  # empty geometry
        return None
    envelope = {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get((data[3] >> 1) & 0b111, 0)
    return bytes(data[8 + envelope:])


def blob_srs(data: bytes | None) -> int | None:
    """The ``srs_id`` written in the header of a GeoPackage binary geometry."""
    if data is None or len(data) < 8 or bytes(data[:2]) != b"GP":
        return None
    return struct.unpack("<i" if data[3] & 1 else ">i", bytes(data[4:8]))[0]


def geometry(data: bytes | None):
    """The shapely geometry of a GeoPackage binary geometry (header, optional envelope, WKB)."""
    import shapely

    raw = wkb(data)
    return shapely.from_wkb(raw) if raw is not None else None


def table_srs(con: sqlite3.Connection, table: str) -> int | None:
    """The ``srs_id`` of a feature table (``gpkg_geometry_columns``), None when it is not one."""
    row = con.execute("SELECT srs_id FROM gpkg_geometry_columns WHERE table_name = ?", (table,)).fetchone()
    return None if row is None else int(row[0])


def set_extent(con: sqlite3.Connection, table: str, bounds) -> None:
    """``gpkg_contents``: the extent of ``table`` (``(minx, miny, maxx, maxy)`` of every feature)
    and the time of the change."""
    bounds = list(bounds)
    ext = (min(b[0] for b in bounds), min(b[1] for b in bounds), max(b[2] for b in bounds),
           max(b[3] for b in bounds)) if bounds else (None, None, None, None)
    con.execute("UPDATE gpkg_contents SET last_change = strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
                "min_x = ?, min_y = ?, max_x = ?, max_y = ? WHERE table_name = ?", (*ext, table))


def read_table(path: str | Path, table: str) -> list[tuple[object, dict]]:
    """The features of a GeoPackage table: ``(shapely geometry or None, {column: value})``."""
    con = sqlite3.connect(str(path))
    try:
        cols = [r[1] for r in con.execute(f"PRAGMA table_info({quote(table)})")]
        if not cols:
            return []
        geom_col = next((r[0] for r in con.execute(
            "SELECT column_name FROM gpkg_geometry_columns WHERE table_name = ?", (table,))), "geom")
        out = []
        for row in con.execute(f"SELECT * FROM {quote(table)}"):
            values = dict(zip(cols, row))
            out.append((geometry(values.pop(geom_col, None)), values))
        return out
    finally:
        con.close()


__all__ = ["write_table", "is_geopackage", "refuse_foreign", "srs_of", "blob", "create", "ensure_table", "replace_rows", "connect",
           "quote", "geometry", "read_table", "wkb", "blob_srs", "table_srs", "set_extent"]
