"""Raster calculator: formulas on any raster of the project - the DEM and the orthomosaic and
their kept versions, the relief, slope and aspect rasters, the bands of the reflectance, the
indices, the thermal orthomosaic, the satellite scenes, the rasters of the plugins and the results
of earlier formulas.

Every raster of a chunk has a name for the formulas (:func:`rasters`): a product by its key (``dem``,
``orthomosaic``, ``svf``, ``slope``...), a kept version by its product and name
(``dem_before_excavation``), the reflectance orthomosaic as ``reflectance`` with its bands by
name, an index by its name (``ndvi``), and every other GeoTIFF of the chunk folder - those the
plugins make - by its folder and file (``terrain_twi``), unless a plugin names its rasters itself
(:func:`register`).

The formulas are written as in the raster calculator of QGIS: a band as ``dem@1`` or
``orthomosaic@2`` (also between quotes, ``"dem@1"``, or by the name of the band,
``reflectance@NIR``; a raster of one band also by its name alone), the operators ``+ - * /`` and
``^`` or ``**`` (power), ``%`` and ``//``, the comparisons ``< <= > >= = == != <>`` (1 where true,
0 where false), ``AND OR NOT`` (also ``and or not``), ``if(condition, a, b)`` and the functions of
:data:`FUNCTIONS`. A cell where an input has no data has no data in the result, unless a function
says otherwise (``isnan``, ``coalesce``). The formula is never run as Python: it is read with
:mod:`ast` and only what is listed here is computed.

The result is a GeoTIFF of 32-bit floats (NaN where there is no data) on the grid of one of the
inputs - the first one in the formula unless another is chosen - at its resolution or at another
one; an input on another grid is resampled onto it (bilinear, nearest for integer rasters such as
zones and classes), also from another reference system. It is made block by block, so its size is
limited by the disk, kept as ``<chunk data>/calculator/<name>.tif`` and listed in
``chunk.meta["calculator"]`` with its formula.
"""
from __future__ import annotations

import ast
import keyword
import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..core.progress import Progress, as_progress

log = logging.getLogger(__name__)

FOLDER = "calculator"
STAND_IN_CRS = "EPSG:3857"  # rasters of a local chunk: the same frame on both sides, any projected CRS does
SKIP_DIRS = {"dense", "sfm", "masks", "versions", "elevations", "_work", "frames", "nodes", ".removed"}
PRODUCT_ORDER = ("dem", "orthomosaic", "slope", "aspect", "svf", "openness_pos", "openness_neg", "slrm", "msrm",
                 "multi_hillshade", "dem_precision", "coverage")
# the source of the raster as the window lists it
SOURCES = {"product": "Prodotto", "version": "Versione", "multispectral": "Multispettrale", "thermal": "Termico",
           "calculator": "Calcolatore", "plugin": "Plugin", "file": "File del chunk"}


def _coalesce(*args):
    out = np.full(np.broadcast(*args).shape, np.nan)
    for a in reversed(args):
        out = np.where(np.isnan(a), out, a)
    return out


def _if(cond, a, b):
    return np.where(np.isnan(cond), np.nan, np.where(cond != 0, a, b))


FUNCTIONS = {
    "abs": np.abs, "sqrt": np.sqrt, "exp": np.exp, "ln": np.log, "log": np.log, "log10": np.log10, "log2": np.log2,
    "sin": np.sin, "cos": np.cos, "tan": np.tan, "asin": np.arcsin, "acos": np.arccos, "atan": np.arctan,
    "atan2": np.arctan2, "degrees": np.degrees, "radians": np.radians, "hypot": np.hypot,
    "floor": np.floor, "ceil": np.ceil, "round": np.round,
    "min": lambda *a: np.minimum.reduce(np.broadcast_arrays(*a)),
    "max": lambda *a: np.maximum.reduce(np.broadcast_arrays(*a)),
    "mean": lambda *a: np.mean(np.broadcast_arrays(*a), axis=0),
    "clip": np.clip, "where": _if,
    "isnan": lambda a: np.isnan(a).astype(np.float64),
    "coalesce": _coalesce,
}
ARGS = {"atan2": (2, 2), "hypot": (2, 2), "clip": (3, 3), "where": (3, 3), "min": (2, None), "max": (2, None),
        "mean": (2, None), "coalesce": (2, None)}  # (least, most) arguments; the others take one
CONSTANTS = {"pi": math.pi, "e": math.e, "nan": math.nan}
_BINARY = {ast.Add: np.add, ast.Sub: np.subtract, ast.Mult: np.multiply, ast.Div: np.divide, ast.Pow: np.power,
           ast.Mod: np.mod, ast.FloorDiv: np.floor_divide}
_COMPARE = {ast.Lt: np.less, ast.LtE: np.less_equal, ast.Gt: np.greater, ast.GtE: np.greater_equal,
            ast.Eq: np.equal, ast.NotEq: np.not_equal}
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_WORDS = ("and", "or", "not", "if", "AND", "OR", "NOT")  # of the formulas: never the name of a raster


# ---------------------------------------------------------------- the rasters of a chunk
@dataclass
class Raster:
    """A raster of a chunk as the formulas see it: ``name`` in the formulas, ``label`` for the user,
    the GeoTIFF, its ``source`` (a key of :data:`SOURCES`), the names of its bands ("" where it has
    none), its size and cell."""

    name: str
    label: str
    path: Path
    source: str
    bands: list[str] = field(default_factory=list)
    width: int = 0
    height: int = 0
    resolution: float = 0.0
    integer: bool = False

    def as_dict(self) -> dict:
        return {"name": self.name, "label": self.label, "path": str(self.path), "source": self.source,
                "bands": list(self.bands), "width": self.width, "height": self.height,
                "resolution": self.resolution}


_PROVIDERS: list[tuple[str | None, object]] = []  # (owner, provider(chunk) -> [dict])


def register(provider, owner: str | None = None):
    """``provider(chunk)`` names rasters of a chunk for the calculator (a plugin naming its own):
    a list of dicts ``name`` (made a valid name if needed), ``label`` and ``path``. Returns the
    entry for :func:`unregister`."""
    entry = (owner, provider)
    _PROVIDERS.append(entry)
    return entry


def unregister(entry) -> None:
    _PROVIDERS[:] = [e for e in _PROVIDERS if e is not entry]


def slug(text: str) -> str:
    """A name for the formulas: letters, digits and ``_``, lower case, not starting with a digit."""
    import unicodedata

    s = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_").lower()
    if not s:
        s = "raster"
    if s[0].isdigit():
        s = "r_" + s
    if keyword.iskeyword(s) or s in FUNCTIONS or s in CONSTANTS or s in _WORDS:
        s += "_r"
    return s


def rasters(chunk) -> list[Raster]:
    """Every raster of ``chunk`` the calculator can read, each with its name for the formulas (see
    the module docstring); the elevations (a frame of their own) are not among them."""
    found: list[Raster] = []
    seen: set[str] = set()
    names: set[str] = set()

    def add(name: str, label: str, path, source: str, band_names=None) -> None:
        path = Path(path)
        try:
            key = str(path.resolve())
        except OSError:
            return
        if key in seen or not path.is_file():
            return
        info = _read_info(path)
        if info is None:
            return
        seen.add(key)
        base = slug(name)
        unique, k = base, 2
        while unique in names:
            unique, k = f"{base}_{k}", k + 1
        names.add(unique)
        width, height, res, count, descriptions, integer, tag = info
        label = label or tag or path.name
        bands = list(band_names or [])[:count] if band_names else descriptions
        bands = (bands + [""] * count)[:count]
        found.append(Raster(unique, label, path, source, bands, width, height, res, integer))

    data = chunk.data_dir
    keys = sorted(chunk.products, key=lambda k: (PRODUCT_ORDER.index(k) if k in PRODUCT_ORDER else len(PRODUCT_ORDER), k))
    for key in keys:
        path = chunk.product_path(key)
        if path is not None and path.suffix.lower() in (".tif", ".tiff"):
            add(key, key, path, "product")
    for product, entries in (chunk.meta.get("versions") or {}).items():
        for e in entries or []:
            rel = (e.get("files") or {}).get(product)
            if rel and str(rel).lower().endswith((".tif", ".tiff")):
                add(f"{product}_{e.get('name') or e.get('id')}", f"{product}: {e.get('name') or e.get('id')}",
                    data / rel, "version")
    ms = chunk.meta.get("multispectral") or {}
    if ms.get("reflectance"):
        add("reflectance", "Riflettanza", data / ms["reflectance"], "multispectral", ms.get("reflectance_bands"))
    for name, info in (ms.get("indices") or {}).items():
        add(name, name, data / str(info.get("file", "")), "multispectral")
    for name, z in (ms.get("zoning") or {}).items():
        if z.get("raster"):
            add(f"{name}_zone", f"{name} (zone)", data / z["raster"], "multispectral")
    th = chunk.meta.get("thermal") or {}
    if th.get("file"):
        add("thermal", "Termico", data / th["file"], "thermal")
    if (th.get("anomaly") or {}).get("file"):
        add("thermal_anomaly", "Anomalie termiche", data / th["anomaly"]["file"], "thermal")
    for name, info in (chunk.meta.get(FOLDER) or {}).items():
        add(name, info.get("formula") or name, data / str(info.get("file", "")), "calculator")
    for owner, provider in list(_PROVIDERS):
        try:
            items = list(provider(chunk) or [])
        except Exception:  # noqa: BLE001 - a plugin never takes the calculator down
            log.exception("the rasters of %s could not be listed", owner or "a plugin")
            continue
        for it in items:
            if isinstance(it, dict) and it.get("path"):
                add(str(it.get("name") or Path(it["path"]).stem), str(it.get("label") or it.get("name") or ""),
                    it["path"], "plugin")
    for path in _chunk_files(data):  # what nobody named: the GeoTIFFs of the plugins, the satellite scenes
        rel = path.relative_to(data)
        top = rel.parts[0] if len(rel.parts) > 1 else ""
        add(f"{top}_{path.stem}" if top else path.stem, "", path, "file")
    return found


def _chunk_files(data: Path) -> list[Path]:
    out = []
    for root, dirs, files in os.walk(data):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for f in sorted(files):
            if f.lower().endswith((".tif", ".tiff")) and not f.endswith((".tmp.tif", ".part.tif")) \
                    and not f.startswith("."):
                out.append(Path(root) / f)
    return out


def _read_info(path: Path):
    """``(width, height, resolution, bands, band descriptions, integer, its name tag)`` of a raster
    in a map frame, None for anything else (unreadable, an image with no place)."""
    import warnings

    import rasterio
    from rasterio.errors import NotGeoreferencedWarning

    try:
        with warnings.catch_warnings():  # an image with no place is left out, not reported
            warnings.simplefilter("ignore", NotGeoreferencedWarning)
            src = rasterio.open(path)
        with src:
            if src.crs is None and src.transform.is_identity:
                return None
            descriptions = [d or "" for d in src.descriptions]
            integer = np.issubdtype(np.dtype(src.dtypes[0]), np.integer) and src.dtypes[0] != "uint8"
            res = float(math.hypot(src.transform.a, src.transform.d))
            tag = str(src.tags().get("ppg_name") or "")
            return src.width, src.height, res, src.count, descriptions, bool(integer), tag
    except Exception:  # noqa: BLE001 - not a raster rasterio reads
        return None


# ---------------------------------------------------------------- the formula
class FormulaError(ValueError):
    """What is wrong with a formula or a name: the message, and its ``code`` with the ``values`` in it
    (the window says it in its own language)."""

    def __init__(self, code: str, text: str, **values):
        super().__init__(text)
        self.code, self.values = code, values


@dataclass
class Formula:
    """A formula read against a catalog (:func:`parse`): the inputs it reads, ``(raster, band)`` in
    the order they appear, and :meth:`evaluate`."""

    text: str
    tree: ast.Expression
    inputs: list[tuple[Raster, int]]
    names: dict[str, tuple[str, int]]  # a name of the tree -> (raster name, band)

    @property
    def rasters(self) -> list[Raster]:
        out: list[Raster] = []
        for r, _b in self.inputs:
            if all(r is not o for o in out):
                out.append(r)
        return out

    def evaluate(self, values: dict) -> np.ndarray:
        """The result for ``values``: ``{(raster name, band): float array}``, no data as NaN."""
        with np.errstate(all="ignore"):
            out = np.asarray(_eval(self.tree.body, self.names, values), np.float64)
        return np.where(np.isfinite(out), out, np.nan)


def _prepare(text: str) -> tuple[str, list[tuple[str, str]]]:
    """The formula with its references to bands (``"dem@1"``, ``reflectance@NIR``) as names of
    its own, and the words of QGIS as Python's."""
    refs: list[tuple[str, str]] = []

    def ref(name: str, band: str) -> str:
        refs.append((name.strip(), band.strip()))
        return f" __r{len(refs) - 1} "

    def quoted(m):
        inner = m.group(1)
        name, at, band = inner.rpartition("@")
        return ref(name, band) if at else ref(inner, "")

    s = re.sub(r'"([^"]*)"', quoted, text)
    s = re.sub(r"\b([A-Za-z_][A-Za-z0-9_]*)@([A-Za-z0-9_]+)\b", lambda m: ref(m.group(1), m.group(2)), s)
    s = re.sub(r"\bAND\b", " and ", s)
    s = re.sub(r"\bOR\b", " or ", s)
    s = re.sub(r"\bNOT\b", " not ", s)
    s = re.sub(r"\bif\s*\(", "where(", s)
    s = s.replace("^", "**")  # the power of QGIS, with the power's precedence
    s = s.replace("<>", "!=")
    s = re.sub(r"(?<![<>=!])=(?!=)", "==", s)
    return s, refs


def parse(text: str, catalog) -> Formula:
    """``text`` read against ``catalog`` (rasters by name, :func:`rasters`); ValueError saying what is
    wrong (an unknown raster, a band it has not, something a formula cannot have)."""
    if isinstance(catalog, (list, tuple)):
        catalog = {r.name: r for r in catalog}
    src, refs = _prepare(text or "")
    if not src.strip():
        raise FormulaError("empty", "write a formula")
    try:
        tree = ast.parse(src.strip(), mode="eval")
    except SyntaxError as exc:
        raise FormulaError("syntax", f"the formula cannot be read ({exc.msg})", detail=exc.msg) from None
    names: dict[str, tuple[str, int]] = {}
    inputs: list[tuple[Raster, int]] = []

    def use(raster: Raster, band: int) -> tuple[str, int]:
        if not any(r is raster and b == band for r, b in inputs):
            inputs.append((raster, band))
        return raster.name, band

    def raster_of(name: str) -> Raster:
        r = catalog.get(name)
        if r is None:
            import difflib

            near = difflib.get_close_matches(name, list(catalog), n=1)
            raise FormulaError("unknown", f"no raster named {name}" + (f" (perhaps {near[0]}?)" if near else ""),
                               name=name, near=near[0] if near else "")
        return r

    def band_of(r: Raster, band: str) -> int:
        if band.isdigit():
            k = int(band)
            if 1 <= k <= len(r.bands):
                return k
            raise FormulaError("band_number", f"{r.name} has {len(r.bands)} band(s): {r.name}@{band} does not exist",
                               name=r.name, bands=len(r.bands), band=band)
        for i, b in enumerate(r.bands, 1):
            if b and b.lower() == band.lower():
                return i
        known = ", ".join(b for b in r.bands if b)
        raise FormulaError("band_name", f"{r.name} has no band {band}" + (f" (bands: {known})" if known else ""),
                           name=r.name, band=band, known=known)

    def check(node, call=False):
        if isinstance(node, ast.Expression):
            check(node.body)
        elif isinstance(node, ast.BinOp):
            if isinstance(node.op, (ast.BitAnd, ast.BitOr)):  # they would bind tighter than the comparisons
                raise FormulaError("and_or", "write AND, OR (or and, or) instead of & and |")
            if type(node.op) not in _BINARY:
                raise FormulaError("operator", f"the operator {type(node.op).__name__} is not in the formulas",
                                   operator=type(node.op).__name__)
            check(node.left)
            check(node.right)
        elif isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.Invert):
                raise FormulaError("not", "write NOT (or not) instead of ~")
            if not isinstance(node.op, (ast.USub, ast.UAdd, ast.Not)):
                raise FormulaError("operator", f"the operator {type(node.op).__name__} is not in the formulas",
                                   operator=type(node.op).__name__)
            check(node.operand)
        elif isinstance(node, ast.BoolOp):
            for v in node.values:
                check(v)
        elif isinstance(node, ast.Compare):
            if any(type(op) not in _COMPARE for op in node.ops):
                raise FormulaError("compare", "compare with < <= > >= = != only")
            check(node.left)
            for c in node.comparators:
                check(c)
        elif isinstance(node, ast.IfExp):
            check(node.test)
            check(node.body)
            check(node.orelse)
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
                what = node.func.id if isinstance(node.func, ast.Name) else "this"
                raise FormulaError("function", f"{what} is not a function of the formulas ({', '.join(sorted(FUNCTIONS))})",
                                   function=what)
            if node.keywords or any(isinstance(a, ast.Starred) for a in node.args):
                raise FormulaError("keywords", f"{node.func.id}: give its values in order", function=node.func.id)
            least, most = ARGS.get(node.func.id, (1, 1))
            if len(node.args) < least or (most is not None and len(node.args) > most):
                want = f"{least}" if least == most else f"at least {least}" if most is None else f"{least} to {most}"
                raise FormulaError("arguments", f"{node.func.id} takes {want} value(s)", function=node.func.id,
                                   least=least, most=most)
            for a in node.args:
                check(a)
        elif isinstance(node, ast.Name):
            n = node.id
            if n.startswith("__r") and n[3:].isdigit() and int(n[3:]) < len(refs):
                name, band = refs[int(n[3:])]
                r = raster_of(name)
                if not band:
                    if len(r.bands) != 1:
                        raise FormulaError("bands", f"{r.name} has {len(r.bands)} bands: write {r.name}@1 ... "
                                       f"{r.name}@{len(r.bands)}", name=r.name, bands=len(r.bands))
                    names[n] = use(r, 1)
                else:
                    names[n] = use(r, band_of(r, band))
            elif n in CONSTANTS:
                pass
            elif n in FUNCTIONS:
                raise FormulaError("is_function", f"{n} is a function: {n}(...)", function=n)
            else:
                r = raster_of(n)
                if len(r.bands) != 1:
                    raise FormulaError("bands", f"{r.name} has {len(r.bands)} bands: write {r.name}@1 ... "
                                       f"{r.name}@{len(r.bands)}", name=r.name, bands=len(r.bands))
                names[n] = use(r, 1)
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise FormulaError("numbers", "a formula has numbers and rasters only")
        else:
            raise FormulaError("not_allowed", f"not in a formula: {type(node).__name__}", what=type(node).__name__)

    check(tree)
    return Formula(text, tree, inputs, names)


def _truth(a):
    return np.where(np.isnan(a), np.nan, (a != 0).astype(np.float64))


def _eval(node, names, values):
    if isinstance(node, ast.Constant):
        return float(node.value)
    if isinstance(node, ast.Name):
        if node.id in names:
            return values[names[node.id]]
        return CONSTANTS[node.id]
    if isinstance(node, ast.BinOp):
        return _BINARY[type(node.op)](_eval(node.left, names, values), _eval(node.right, names, values))
    if isinstance(node, ast.UnaryOp):
        a = _eval(node.operand, names, values)
        if isinstance(node.op, ast.USub):
            return -np.asarray(a, np.float64)
        if isinstance(node.op, ast.UAdd):
            return np.asarray(a, np.float64)
        return np.where(np.isnan(a), np.nan, (np.asarray(a) == 0).astype(np.float64))  # not
    if isinstance(node, ast.BoolOp):
        return _logic(node.op, [_eval(v, names, values) for v in node.values])
    if isinstance(node, ast.Compare):
        left = _eval(node.left, names, values)
        out, bad = None, np.isnan(left)
        for op, comp in zip(node.ops, node.comparators):
            right = _eval(comp, names, values)
            bad = bad | np.isnan(right)
            r = _COMPARE[type(op)](left, right)
            out = r if out is None else out & r
            left = right
        return np.where(bad, np.nan, np.asarray(out).astype(np.float64))
    if isinstance(node, ast.IfExp):
        return _if(_eval(node.test, names, values), _eval(node.body, names, values),
                   _eval(node.orelse, names, values))
    if isinstance(node, ast.Call):
        return FUNCTIONS[node.func.id](*[np.asarray(_eval(a, names, values), np.float64) for a in node.args])
    raise ValueError(f"not in a formula: {type(node).__name__}")  # pragma: no cover - parse() checked it


def _logic(op, items):
    items = [np.asarray(v, np.float64) for v in items]
    bad = np.zeros(np.broadcast(*items).shape, bool)
    for v in items:
        bad = bad | np.isnan(v)
    truth = [v != 0 for v in items]
    both = isinstance(op, ast.And)
    out = np.logical_and.reduce(truth) if both else np.logical_or.reduce(truth)
    return np.where(bad, np.nan, np.asarray(out).astype(np.float64))


# ---------------------------------------------------------------- the result
def check_name(chunk, name: str, catalog=None) -> str:
    """``name`` for a new result: a valid name not taken by another raster of the chunk (an earlier
    result of the calculator with that name is replaced)."""
    name = str(name or "").strip()
    if not _NAME.match(name) or name.startswith("__") or keyword.iskeyword(name) or name in FUNCTIONS \
            or name in CONSTANTS or name in _WORDS:
        raise FormulaError("name", f"{name!r}: a name of letters, digits and _, not starting with a digit", name=name)
    catalog = catalog if catalog is not None else rasters(chunk)
    for r in catalog:
        if r.name == name and r.source != "calculator":
            raise FormulaError("name_taken", f"{name}: there is a raster of the chunk with that name", name=name)
    return name


def _grid(ref, resolution: float | None):
    """``(crs, transform, width, height)`` of the result: the grid of ``ref``, at another
    ``resolution`` over the same bounds."""
    from rasterio.transform import from_origin

    if not resolution or resolution <= 0:
        return ref.crs, ref.transform, ref.width, ref.height
    if ref.transform.b != 0 or ref.transform.d != 0:
        raise FormulaError("rotated", "another resolution needs a raster that is not rotated as the grid")
    left, bottom, right, top = ref.bounds
    width = max(1, int(math.ceil((right - left) / resolution - 1e-6)))
    height = max(1, int(math.ceil((top - bottom) / resolution - 1e-6)))
    return ref.crs, from_origin(left, top, resolution, resolution), width, height


def _same_grid(src, crs, transform, width, height) -> bool:
    """Whether ``src`` is on the grid of the result (its cells read as they are)."""
    a, b = src.transform, transform
    cell = max(abs(b.a), abs(b.e), 1e-12)
    return src.crs == crs and src.width == width and src.height == height \
        and abs(a.a - b.a) <= 1e-9 * cell and abs(a.e - b.e) <= 1e-9 * cell and a.b == b.b and a.d == b.d \
        and abs(a.c - b.c) <= 1e-3 * cell and abs(a.f - b.f) <= 1e-3 * cell


def _block_values(src, band: int, integer: bool, crs, transform, window, shape, same: bool) -> np.ndarray:
    """``band`` of ``src`` on the block ``window`` of the result grid: read as it is when the grid is
    the same, else the part of ``src`` around the block resampled onto it (NaN where no data)."""
    from rasterio.enums import Resampling
    from rasterio.warp import reproject, transform_bounds
    from rasterio.windows import Window, from_bounds, transform as window_transform

    if same:
        return src.read(band, window=window, masked=True).astype(np.float64).filled(np.nan)
    dst_transform = window_transform(window, transform)
    h, w = shape
    left, top = dst_transform.c, dst_transform.f
    right, bottom = left + dst_transform.a * w, top + dst_transform.e * h
    src_crs = src.crs or crs or STAND_IN_CRS
    dst_crs = crs or src.crs or STAND_IN_CRS
    if src.crs is None and crs is not None or src.crs is not None and crs is None:
        src_crs = dst_crs  # one of them in no system: the same frame as the other (the chunk's)
    bounds = (min(left, right), min(top, bottom), max(left, right), max(top, bottom))
    if src_crs != dst_crs:
        bounds = transform_bounds(dst_crs, src_crs, *bounds, densify_pts=21)
    out = np.full(shape, np.nan)
    try:
        win = from_bounds(*bounds, transform=src.transform)
    except Exception:  # noqa: BLE001 - a rotated raster (a geophysical grid): all of it, they are small
        win = Window(0, 0, src.width, src.height)
    else:
        win = Window(math.floor(win.col_off) - 2, math.floor(win.row_off) - 2, math.ceil(win.width) + 5,
                     math.ceil(win.height) + 5)
        try:
            win = win.intersection(Window(0, 0, src.width, src.height))
        except Exception:  # noqa: BLE001 - the block is outside this raster
            return out
    if win.width <= 0 or win.height <= 0:
        return out
    data = src.read(band, window=win, masked=True).astype(np.float64).filled(np.nan)
    reproject(data, out, src_transform=window_transform(win, src.transform), src_crs=src_crs, src_nodata=np.nan,
              dst_transform=dst_transform, dst_crs=dst_crs, dst_nodata=np.nan,
              resampling=Resampling.nearest if integer else Resampling.bilinear)
    return out


def calculate(chunk, formula: str, name: str, grid: str | None = None, resolution: float | None = None,
              progress: Progress | None = None, block: int = 1024) -> dict:
    """The result of ``formula`` (see the module docstring) as the raster ``name`` of the chunk:
    on the grid of the raster ``grid`` (default: the first one of the formula), at ``resolution``
    (default: its own); returns its entry of ``chunk.meta["calculator"]`` (file, formula, inputs,
    grid, size, resolution, stats)."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.windows import Window

    progress = as_progress(progress)
    catalog = {r.name: r for r in rasters(chunk)}
    name = check_name(chunk, name, list(catalog.values()))
    f = parse(formula, catalog)
    if grid is not None and grid not in catalog:
        raise FormulaError("grid", f"no raster named {grid} for the grid", name=grid)
    if grid is None and not f.inputs:
        raise FormulaError("no_grid", "a formula with no raster needs a raster for the grid")
    ref_raster = catalog[grid] if grid is not None else f.inputs[0][0]
    with rasterio.open(ref_raster.path) as ref:
        crs, transform, width, height = _grid(ref, resolution)
    out_dir = chunk.data_dir / FOLDER
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{name}.tif"
    tmp = out.with_name(f"{name}.tmp.tif")
    profile = {"driver": "GTiff", "height": height, "width": width, "count": 1, "dtype": "float32",
               "nodata": float("nan"), "transform": transform, "compress": "deflate", "predictor": 3,
               "BIGTIFF": "IF_SAFER"}
    if crs is not None:
        profile["crs"] = crs
    if height >= 256 and width >= 256:
        profile.update(tiled=True, blockxsize=256, blockysize=256)
    blocks = [(r, c) for r in range(0, height, block) for c in range(0, width, block)]
    progress(0.0, f"Raster calculator: {name} = {formula} ({width}x{height} cells)")
    n, total, lo, hi, sample = 0, 0.0, math.inf, -math.inf, []
    step = max(1, int(width * height / 1_000_000))
    sources = {}
    try:
        for r in f.rasters:
            sources[r.name] = rasterio.open(r.path)
        same = {k: _same_grid(s, crs, transform, width, height) for k, s in sources.items()}
        with rasterio.open(tmp, "w", **profile) as dst:
            dst.set_band_description(1, name)
            dst.update_tags(ppg_formula=formula, ppg_name=name)
            for i, (r0, c0) in enumerate(blocks):
                h, w = min(block, height - r0), min(block, width - c0)
                win = Window(c0, r0, w, h)
                values = {(r.name, b): _block_values(sources[r.name], b, r.integer, crs, transform, win, (h, w),
                                                     same[r.name]) for r, b in f.inputs}
                res = np.broadcast_to(f.evaluate(values), (h, w)).astype(np.float32)
                dst.write(res, 1, window=win)
                ok = np.isfinite(res)
                if ok.any():
                    v = res[ok].astype(np.float64)
                    n += v.size
                    total += float(v.sum())
                    lo, hi = min(lo, float(v.min())), max(hi, float(v.max()))
                    sample.append(v[::step])
                progress((i + 1) / len(blocks) * 0.9, f"Raster calculator: block {i + 1}/{len(blocks)}")
            factors = [k for k in (2, 4, 8, 16, 32, 64) if min(height, width) // k >= 64]
            if factors:
                progress(0.92, "Overviews of the result")
                dst.build_overviews(factors, Resampling.average)
    except BaseException:
        tmp.unlink(missing_ok=True)  # cancelled or failed: no half-written file left behind
        raise
    finally:
        for s in sources.values():
            s.close()
    tmp.replace(out)
    stats = {"cells": int(n)}
    if n:
        values = np.concatenate(sample)
        p2, p50, p98 = (float(v) for v in np.percentile(values, [2, 50, 98]))
        stats.update(min=lo, max=hi, mean=total / n, p2=p2, median=p50, p98=p98)
    res = float(math.hypot(transform.a, transform.d))
    entry = {"file": out.relative_to(chunk.data_dir).as_posix(), "formula": formula,
             "inputs": [f"{r.name}@{b}" for r, b in f.inputs], "grid": ref_raster.name, "resolution": res,
             "size": [int(width), int(height)], "stats": stats, "made": time.strftime("%Y-%m-%dT%H:%M:%S")}
    chunk.meta.setdefault(FOLDER, {})[name] = entry
    progress(1.0, f"Raster calculator: {name} ({stats.get('min', float('nan')):.4g} to "
                  f"{stats.get('max', float('nan')):.4g})")
    return entry


def result_path(chunk, name: str) -> Path | None:
    """The GeoTIFF of a result of the calculator, None when there is none of that name."""
    info = (chunk.meta.get(FOLDER) or {}).get(name)
    return None if not info else chunk.data_dir / info["file"]


__all__ = ["FUNCTIONS", "Formula", "FormulaError", "Raster", "calculate", "check_name", "parse", "rasters", "register",
           "result_path", "slug", "unregister"]
