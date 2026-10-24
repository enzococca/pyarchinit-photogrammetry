"""What the products of a chunk hold at a point of the ground: the elevation of the DEM, the
temperature and its anomaly, the reflectance of every band, the vegetation indices - read at full
resolution from the GeoTIFFs, never from the reduced copy the views show.

:class:`Probe` keeps the files open between calls: the readout under the mouse reads one cell of
each file per movement (GDAL keeps the blocks around it in memory). With ``keep_open`` False it
closes them after each reading: while a task may write those products (Windows does not replace or
delete a file another handle holds open). :meth:`Probe.record` makes the
attributes of a measuring point (coordinates, elevation, temperature, bands, indices) and
:func:`write_points` writes such points as a GeoPackage, a CSV or a GeoJSON that QGIS opens with
their reference system.

Field names are short and plain for QGIS and the shapefile limits: ``x``, ``y``, ``z``,
``temp_c`` (degrees Celsius), ``anomaly_c`` (degrees above or below the surroundings),
``refl_<band>`` (reflectance, ``refl_nir``...), the indices in lower case (``ndvi``...).
"""
from __future__ import annotations

import csv
import json
import math
import re
from pathlib import Path

import numpy as np

DECIMALS = {"x": 3, "y": 3, "z": 3, "temp_c": 2, "anomaly_c": 2}  # the others (reflectance, indices): 4


def _field(name: str) -> str:
    return re.sub(r"[^0-9a-z_]+", "_", str(name).strip().lower()).strip("_") or "value"


class Probe:
    """The products of ``chunk`` that hold a value per cell, open for reading single cells."""

    def __init__(self, chunk):
        self.sources: list[tuple[Path, list[str]]] = []  # (GeoTIFF, field of each band)
        self.labels: dict[str, str] = {}  # field -> what to call it on screen
        self._open: dict[Path, object] = {}
        self.keep_open = True
        if chunk is None:
            return
        data = Path(chunk.data_dir)

        def add(path: Path, fields: list[str], labels: list[str]) -> None:
            self.sources.append((path, fields))
            self.labels.update(zip(fields, labels))

        if chunk.dem_path is not None and Path(chunk.dem_path).exists():
            add(Path(chunk.dem_path), ["z"], ["Z"])
        th = chunk.meta.get("thermal") or {}
        if th.get("file") and (data / th["file"]).exists():
            add(data / th["file"], ["temp_c"], ["T"])
        an = th.get("anomaly") or {}
        if an.get("file") and (data / an["file"]).exists():
            add(data / an["file"], ["anomaly_c"], ["ΔT"])
        ms = chunk.meta.get("multispectral") or {}
        if ms.get("reflectance") and (data / ms["reflectance"]).exists():
            bands = list(ms.get("reflectance_bands") or [])
            path = data / ms["reflectance"]
            if not bands:
                bands = self._descriptions(path)
            add(path, ["refl_" + _field(b) for b in bands], [str(b) for b in bands])
        for name, info in (ms.get("indices") or {}).items():
            if info.get("file") and (data / info["file"]).exists():
                add(data / info["file"], [_field(name)], [str(name)])

    @staticmethod
    def _descriptions(path: Path) -> list[str]:
        import rasterio

        with rasterio.open(path) as src:
            return [d or f"b{i + 1}" for i, d in enumerate(src.descriptions)]

    @property
    def fields(self) -> list[str]:
        """The value fields this chunk can give, in order."""
        return [f for _p, fields in self.sources for f in fields]

    def _dataset(self, path: Path):
        ds = self._open.get(path)
        if ds is None:
            import rasterio

            ds = rasterio.open(path)
            self._open[path] = ds
        return ds

    def fields_of(self, path) -> list[str]:
        """The fields read from one of the products (a raster shown in a view)."""
        path = Path(path)
        return next((f for p, f in self.sources if p == path), [])

    def at(self, x: float, y: float, fields=None) -> dict[str, float]:
        """The values at the point ``x``, ``y`` (the chunk's reference system): only those the
        products have there (outside a raster, or on its empty cells, a field is left out);
        ``fields``: only the products holding these."""
        try:
            return self._at(x, y, fields)
        finally:
            if not self.keep_open:
                self.close()

    def _at(self, x: float, y: float, fields) -> dict[str, float]:
        from rasterio.windows import Window

        out: dict[str, float] = {}
        wanted = set(fields) if fields is not None else None
        for path, names in self.sources:
            if wanted is not None and not wanted & set(names):
                continue
            fields_here = names
            try:
                ds = self._dataset(path)
                row, col = ds.index(x, y)
                if not (0 <= row < ds.height and 0 <= col < ds.width):
                    continue
                vals = ds.read(window=Window(col, row, 1, 1), masked=True)[:, 0, 0]
            except Exception:  # noqa: BLE001 - a file being rewritten: that value is missing this time
                continue
            for field, v in zip(fields_here, vals):
                if v is np.ma.masked:
                    continue
                v = float(v)
                if math.isfinite(v) and not (ds.nodata is not None and v == ds.nodata):
                    out[field] = v
        return out

    def text(self, values: dict[str, float], fields=None) -> str:
        """The values as the status bar shows them: ``Z 158.231  T 18.34 °C  NIR 0.381``."""
        parts = []
        for f in (fields or list(values)):
            if f not in values:
                continue
            v, name = values[f], self.labels.get(f, f)
            if f == "z":
                parts.append(f"Z {v:.3f}")
            elif f in ("temp_c", "anomaly_c"):
                parts.append(f"{name} {v:+.2f} °C" if f == "anomaly_c" else f"{name} {v:.2f} °C")
            else:
                parts.append(f"{name} {v:.4f}" if abs(v) < 10 else f"{name} {v:.2f}")
        return "  ".join(parts)

    def record(self, x: float, y: float, **extra) -> dict:
        """The attributes of a measuring point at ``x``, ``y``: coordinates, then every value
        the products have there, rounded to what they mean (millimetres, hundredths of a degree,
        four decimals of reflectance and indices), then ``extra``."""
        rec = {"x": round(float(x), 3), "y": round(float(y), 3)}
        for k, v in self.at(x, y).items():
            rec[k] = round(v, DECIMALS.get(k, 4))
        rec.update(extra)
        return rec

    def close(self) -> None:
        for ds in self._open.values():
            try:
                ds.close()
            except Exception:  # noqa: BLE001
                pass
        self._open.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def write_points(points: list[dict], path, crs: str | None, fmt: str | None = None, table: str = "punti_misura") -> Path:
    """Measuring points (dicts with ``x``, ``y`` and their values, :meth:`Probe.record`) into a
    GeoPackage (``gpkg``: a point table with ``z`` too, the reference system of the chunk), a CSV
    (``csv``: one line per point, QGIS reads it as delimited text with x and y), a GeoJSON
    (``geojson``; coordinates as they are, the CRS in its ``crs`` member) or an Excel workbook
    (``xlsx``: a sheet of the points, numbers as numbers, the CRS as the title of the workbook).
    The columns are the union of the points' fields."""
    path = Path(path)
    fmt = fmt or {".csv": "csv", ".geojson": "geojson", ".json": "geojson", ".xlsx": "xlsx"}.get(path.suffix.lower(),
                                                                                                 "gpkg")
    fields: list[str] = []
    for p in points:
        for k in p:
            if not str(k).startswith("_") and k not in fields:
                fields.append(k)
    fields = ["x", "y"] + [k for k in fields if k not in ("x", "y")]
    if fmt == "csv":
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if crs:
                f.write(f"# {crs}\n")
            w.writerow(fields)
            for p in points:
                w.writerow(["" if p.get(k) is None else p.get(k) for k in fields])
        return path
    if fmt == "xlsx":  # a sheet of the points; the reference system is the title of the workbook
        from ..io.tables import Sheet, write_xlsx

        decimals = [DECIMALS.get(k, 4) if k != "label" else None for k in fields]
        return write_xlsx(path, {table: Sheet(fields, [[p.get(k) for k in fields] for p in points], decimals)},
                          title=crs or None)
    if fmt == "geojson":
        feats = [{"type": "Feature", "geometry": {"type": "Point", "coordinates":
                  [p["x"], p["y"]] + ([p["z"]] if p.get("z") is not None else [])},
                  "properties": {k: p.get(k) for k in fields if k not in ("x", "y")}} for p in points]
        doc = {"type": "FeatureCollection", "features": feats}
        if crs:
            doc["crs"] = {"type": "name", "properties": {"name": crs}}
        path.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
        return path
    if fmt != "gpkg":
        raise ValueError(f"unknown format {fmt!r}: gpkg, csv, geojson or xlsx")
    from shapely.geometry import Point

    from ..io import gpkg

    columns = {}
    for k in fields:
        vals = [p.get(k) for p in points if p.get(k) is not None]
        columns[k] = "REAL" if vals and all(isinstance(v, (int, float)) for v in vals) else "TEXT"
    rows = [(Point(p["x"], p["y"], p["z"]) if p.get("z") is not None else Point(p["x"], p["y"]),
             {k: p.get(k) for k in fields}) for p in points]
    return gpkg.write_table(path, table, "POINT", columns, rows, crs)


__all__ = ["Probe", "write_points", "DECIMALS"]
