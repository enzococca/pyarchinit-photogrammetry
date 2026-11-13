"""A survey as a web site: one folder any static server can publish (since 1.10.0).

:func:`export_site` (``document.export_site``, ``ppg web build``) writes::

    site/
      index.html          the page
      site.json           the manifest: title, languages, frame, the chunks with their extent and layers
      app/                the viewer: plain ES modules and OpenLayers, no CDN and no build step
      data/               (data/1/, data/2/... with several chunks)
        ortho.tif         the orthophoto, Cloud Optimized GeoTIFF, WebP with its alpha
        dem.tif           the DEM, COG float32 (deflate): the heights read on a click
        dem_hillshade.tif the shaded relief, COG 8 bit (WebP, transparent where the DEM is empty)
        units.geojson     the stratigraphic units of the plan with their attributes
        drawings.geojson  spot heights, break lines, measuring points and free drawings, each with its ``layer``

``frame``: ``"local"`` (default) shifts every coordinate to an origin at the centre of the survey (whole metres; the
heights to the whole metre under the lowest cell of the DEMs, read whole, so that no cell is under zero; under the
lowest drawn height where there is no DEM) and writes no reference system anywhere: nothing in the folder says where the site is. The origin is not written either. ``"crs"`` keeps the coordinates and the reference
system of the chunks: an explicit choice. In both frames the rasters go through a description of their pixels alone
(a VRT), so no tag of the source files reaches the site, the photos and their EXIF are never copied, and every text
typed by the user - the attributes and their names, the names of chunks, layers and drawings, the title and the
description - that holds the path of a computer is cut to its last name.

:func:`server` (``ppg web serve``) is a static server with HTTP range requests - the viewer reads the rasters by
parts - bound to this computer alone unless another host is asked for.

The viewer reads the three rasters with its own small reader (``app/cog.js``): the tiles of a WebP COG are WebP
images the browser decodes, those of the DEM are inflated by the browser. It reads what is written here, not any
GeoTIFF.
"""
from __future__ import annotations

import html
import json
import math
import os
import re
import shutil
import tempfile
from datetime import date
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.sax.saxutils import escape

from ..core import crs as crs_mod
from ..core.progress import as_progress

FORMAT = "ppg-site"
FORMAT_VERSION = 1
FRAMES = ("local", "crs")
PRODUCTS = ("orthomosaic", "dem", "units", "drawings")  # the shaded relief goes with the DEM
LANGUAGES = ("it", "en")
MANIFEST = "site.json"
OWN = ("index.html", MANIFEST, "app", "data")  # what a site folder holds: rewritten when the site is built again
VIEWER = Path(__file__).resolve().parents[1] / "web"  # index.html and app/, copied as they are
DECIMALS = 4  # of the coordinates of the drawings: a tenth of a millimetre

TITLES = {  # the layers of the viewer, in the languages of the page
    "ortho": {"it": "Ortofoto", "en": "Orthophoto"},
    "hillshade": {"it": "Rilievo ombreggiato", "en": "Shaded relief"},
    "dem": {"it": "Quote (DEM)", "en": "Heights (DEM)"},
    "units": {"it": "Unità stratigrafiche", "en": "Stratigraphic units"},
    "drawings": {"it": "Disegni", "en": "Drawings"},
}
GROUPS = {  # the kinds of drawings of the plan: the name of their ``layer``, its title, its colour in the program
    "quote": ("Quote", {"it": "Quote", "en": "Spot heights"}, "#f59e0b"),
    "lines": ("Linee di rottura", {"it": "Linee di rottura", "en": "Break lines"}, "#ff7f0e"),
    "points": ("Punti di misura", {"it": "Punti di misura", "en": "Measuring points"}, "#ff5d8f"),
}
UNIT_COLOUR = "#e6194b"
FIELDS = {  # the attributes the viewer names in both languages (the others keep the name of their column)
    "label": {"it": "Etichetta", "en": "Label"}, "us": {"it": "US", "en": "Unit"},
    "area": {"it": "Area", "en": "Area"}, "sito": {"it": "Sito", "en": "Site"}, "tipo": {"it": "Tipo", "en": "Type"},
    "layer": {"it": "Livello", "en": "Layer"}, "name": {"it": "Nome", "en": "Name"},
    "quota": {"it": "Quota (m)", "en": "Height (m)"}, "z": {"it": "Quota (m)", "en": "Height (m)"},
    "z_min": {"it": "Quota minima (m)", "en": "Lowest height (m)"},
    "z_max": {"it": "Quota massima (m)", "en": "Highest height (m)"},
    "area_m2": {"it": "Superficie (m²)", "en": "Surface (m²)"},
}
_TECHNICAL = ("ppg_id", "ppg_sig", "ppg_parte", "basis")  # identities of the program: of no use on a page


# ------------------------------------------------------------------------------------------------ what goes out
def _chunks(document, chunks) -> list:
    """The chunks asked for (objects, keys or labels); None: every chunk with something to show."""
    if chunks is None:
        return [c for c in document.chunks if _has_content(c)]
    if not isinstance(chunks, (list, tuple)):
        chunks = [chunks]
    out = []
    for c in chunks:
        ch = c if hasattr(c, "meta") else next(
            (x for x in document.chunks if x.label == c or str(x.key) == str(c)), None)
        if ch is None:
            raise ValueError(f"no chunk {c!r} in the project")
        if not _has_content(ch):
            raise ValueError(f"chunk {ch.label!r} has nothing to publish: no orthophoto, DEM or drawings")
        out.append(ch)
    return out


def _raster(chunk, product: str) -> Path | None:
    path = chunk.orthomosaic_path if product == "orthomosaic" else chunk.dem_path
    return Path(path) if path is not None and Path(path).exists() else None


def _has_content(chunk) -> bool:
    d = chunk.meta.get("drawings") or {}
    return bool(_raster(chunk, "orthomosaic") or _raster(chunk, "dem")
                or any(d.get(k) for k in ("units", "quote", "lines", "points", "free")))


def _private_roots(document) -> list[str]:
    """Folders of this computer no text of the site may name: the home, the project's, the temporary one."""
    roots = {str(Path.home()), tempfile.gettempdir(), str(Path(tempfile.gettempdir()).resolve())}
    if document.path is not None:
        roots |= {str(Path(document.path).parent), str(Path(document.path).resolve().parent)}
    return sorted((r for r in roots if len(r) > 1), key=len, reverse=True)


# a text that holds the path of a computer whatever its folder: a home, a volume, a drive, a share, a file URL
_PATH = re.compile(r"""(?:^|[\s"'(\[=:;,])(?:~[/\\]|/(?:Users|home|Volumes|private|tmp|var|mnt|media|root|opt"""
                   r"""|Library|Applications)/|[A-Za-z]:[/\\]|\\\\[^\\\s])|file:/""")


def _scrub(value, roots: list[str]):
    """``value`` (JSON-like) without paths of this computer: a text that names one of ``roots``, or that holds
    a path as computers write them, keeps its last name. The keys of a dictionary are texts as well."""
    if isinstance(value, str):
        if any(r in value for r in roots) or _PATH.search(value):
            return value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        return value
    if isinstance(value, dict):
        return {_scrub(str(k), roots): _scrub(v, roots) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v, roots) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _attributes(values: dict, roots: list[str]) -> dict:
    """The attributes of a feature as the page shows them: no private key, no identity, no empty value."""
    out = {}
    for k, v in (values or {}).items():
        if str(k).startswith("_") or k in _TECHNICAL or v is None or v == "":
            continue
        out[_scrub(str(k), roots)] = _scrub(v, roots)
    return out


class _Shift:
    """The frame of the site: what is taken off every coordinate (nothing in the ``crs`` frame)."""

    def __init__(self, dx: float = 0.0, dy: float = 0.0, dz: float = 0.0):
        self.dx, self.dy, self.dz = float(dx), float(dy), float(dz)

    def xy(self, x, y) -> list[float]:
        return [round(float(x) - self.dx, DECIMALS), round(float(y) - self.dy, DECIMALS)]

    def z(self, z):
        return None if z is None or not math.isfinite(float(z)) else round(float(z) - self.dz, DECIMALS)

    def position(self, p) -> list[float]:
        z = self.z(p[2]) if len(p) > 2 else None
        return self.xy(p[0], p[1]) + ([z] if z is not None else [])

    def geometry(self, geom) -> dict:
        """A shapely geometry as a GeoJSON geometry in the frame."""
        from shapely.geometry import mapping

        def walk(c):
            if c and isinstance(c[0], (int, float)):
                return self.position(c)
            return [walk(x) for x in c]

        m = mapping(geom)
        return {"type": m["type"], "coordinates": walk(m["coordinates"])}


def _unit_features(chunk, shift: _Shift, roots) -> list[dict]:
    from ..archaeo.stratigraphy import UnitPolygon

    feats = []
    for d in (chunk.meta.get("drawings") or {}).get("units") or []:
        try:
            u = UnitPolygon.from_dict(d)
        except Exception:  # a broken record: left out, as the window does
            continue
        if u.geometry is None or u.geometry.is_empty:
            continue
        tipo = u.tipo or "US"
        props = {"label": f"{tipo} {u.us}" if u.us else tipo, "us": u.us, "area": u.area, "sito": u.sito,
                 "tipo": tipo, "area_m2": round(float(u.geometry.area), 3)}
        props.update(_attributes(u.attributes, roots))
        feats.append({"type": "Feature", "properties": _attributes(props, roots),
                      "geometry": shift.geometry(u.geometry)})
    return feats


def _drawing_features(chunk, shift: _Shift, roots) -> tuple[list[dict], list[dict]]:
    """The other drawings of the plan as features, each with its ``layer``, and the layers met:
    ``{name, title, colour, count}``."""
    from ..archaeo import breaklines
    from ..core import free_drawings

    d = chunk.meta.get("drawings") or {}
    feats: list[dict] = []
    groups: dict[str, dict] = {}

    def add(name: str, title: dict, colour: str, geometry: dict, props: dict) -> None:
        g = groups.setdefault(name, {"name": name, "title": title, "colour": colour, "count": 0})
        g["count"] += 1
        feats.append({"type": "Feature", "properties": {"layer": name, **_attributes(props, roots)},
                      "geometry": geometry})

    name, title, colour = GROUPS["quote"]
    for q in d.get("quote") or []:
        try:
            xy = shift.xy(q["x"], q["y"])
        except (KeyError, TypeError, ValueError):
            continue
        try:
            height = shift.z(q.get("quota_q"))
        except (TypeError, ValueError):
            height = None
        props = {"label": f"{height:.3f}" if height is not None else "", "quota": height,
                 **{k: v for k, v in q.items() if k not in ("x", "y", "quota_q")}}
        add(name, title, colour, {"type": "Point", "coordinates": xy}, props)

    name, title, _ = GROUPS["lines"]
    for r in breaklines.records(chunk):
        zs = [shift.z(z) for z in breaklines.heights(r)]
        coords = [shift.xy(c[0], c[1]) + ([z] if all(v is not None for v in zs) else [])
                  for c, z in zip(r["coords"], zs)]
        known = [z for z in zs if z is not None]
        props = {"label": breaklines.label(r), "kind": r["kind"], "tipo": breaklines.KIND_LABELS[r["kind"]],
                 "z_min": min(known) if known else None, "z_max": max(known) if known else None,
                 "colour": breaklines.COLOURS[r["kind"]], **(r.get("attributes") or {})}
        add(name, title, breaklines.COLOURS["breakline"], {"type": "LineString", "coordinates": coords}, props)

    name, title, colour = GROUPS["points"]
    for p in d.get("points") or []:
        try:
            xy = shift.xy(p["x"], p["y"])
            z = shift.z(p.get("z"))
        except (KeyError, TypeError, ValueError):
            continue
        props = {**{k: v for k, v in p.items() if k not in ("x", "y", "z")}, "z": z}
        add(name, title, colour, {"type": "Point", "coordinates": xy + ([z] if z is not None else [])}, props)

    colours = {x["name"]: x.get("colour") or free_drawings.DEFAULT_COLOUR for x in free_drawings.layers(chunk)}
    for rec in free_drawings.features_in_world(chunk):
        xyz = [shift.position(p) for p in rec["xyz"]]
        if rec["kind"] == "line":
            geometry = {"type": "LineString", "coordinates": xyz}
        elif rec["kind"] == "polygon":
            geometry = {"type": "Polygon", "coordinates": [xyz + [xyz[0]]]}
        else:
            geometry = {"type": "Point", "coordinates": xyz[0]}
        colour = colours.get(rec["layer"], free_drawings.DEFAULT_COLOUR)
        layer = _scrub(str(rec["layer"]), roots) or "-"  # a name typed by the user, as every other text
        props = {"label": rec.get("name") or "", "name": rec.get("name") or "", **(rec.get("attributes") or {})}
        if rec.get("colour"):
            props["colour"] = rec["colour"]
        add(layer, {"it": layer, "en": layer}, colour, geometry, props)
    return feats, list(groups.values())


def _lowest(feats: list[dict]) -> float | None:
    """The lowest height the features carry: in their vertices and in the heights of their attributes."""
    found = []

    def walk(c):
        if c and isinstance(c[0], (int, float)):
            if len(c) > 2:
                found.append(c[2])
        else:
            for x in c or []:
                walk(x)

    for f in feats:
        walk(f["geometry"]["coordinates"])
        found.extend(f["properties"].get(k) for k in ("quota", "z", "z_min"))
    found = [float(z) for z in found if isinstance(z, (int, float)) and not isinstance(z, bool) and math.isfinite(z)]
    return min(found) if found else None


def _dem_heights(path: Path) -> tuple[float, float] | None:
    """The lowest and the highest height of a DEM, read block by block: the true ones, whatever its size (a
    reduced reading would miss a narrow pit, and the frame of the site must lie under every cell)."""
    import numpy as np
    import rasterio

    lo, hi = math.inf, -math.inf
    with rasterio.open(path) as src:
        for _, window in src.block_windows(1):
            block = src.read(1, window=window, masked=True)
            cells = np.asarray(block.compressed(), dtype=float)
            cells = cells[np.isfinite(cells)]
            if cells.size:
                lo, hi = min(lo, float(cells.min())), max(hi, float(cells.max()))
    return (lo, hi) if lo <= hi else None


def _bounds(feats: list[dict]) -> tuple[float, float, float, float] | None:
    xs, ys = [], []

    def walk(c):
        if c and isinstance(c[0], (int, float)):
            xs.append(c[0])
            ys.append(c[1])
        else:
            for x in c:
                walk(x)

    for f in feats:
        walk(f["geometry"]["coordinates"])
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def _union(boxes) -> tuple[float, float, float, float] | None:
    boxes = [b for b in boxes if b is not None]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def _write_geojson(path: Path, feats: list[dict], crs: str | None) -> None:
    data: dict = {"type": "FeatureCollection"}
    code = crs_mod.epsg_code(crs) if crs else None
    if code:
        data["crs"] = {"type": "name", "properties": {"name": f"urn:ogc:def:crs:EPSG::{code}"}}
    data["features"] = feats
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


# ------------------------------------------------------------------------------------------------ rasters
def _raster_info(path: Path) -> dict:
    import rasterio

    with rasterio.open(path) as src:
        t = src.transform
        if abs(t.b) > 1e-12 or abs(t.d) > 1e-12 or t.e >= 0 or t.a <= 0:
            raise ValueError(f"{path.name}: a rotated or flipped raster cannot be published")
        return {"width": src.width, "height": src.height, "count": src.count, "dtype": src.dtypes[0],
                "nodata": src.nodata, "transform": t, "bounds": tuple(src.bounds),
                "wkt": src.crs.to_wkt() if src.crs else None}


def _vrt(dst: Path, info: dict, bands: list[tuple], dtype: str, shift: _Shift, keep_crs: bool,
         nodata=None, lift: float = 0.0) -> Path:
    """A GDAL virtual raster of ``bands`` (``(file, source band, colour)``) on the grid of ``info`` moved by
    ``shift``: the pixels of the sources and nothing else of them - no tag, no name of a file once it is copied.
    ``lift``: added to the values (a DEM brought to the heights of the frame), the empty cells left empty."""
    t = info["transform"]
    lines = [f'<VRTDataset rasterXSize="{info["width"]}" rasterYSize="{info["height"]}">']
    if keep_crs and info["wkt"]:
        lines.append(f"<SRS>{escape(info['wkt'])}</SRS>")
    lines.append(f"<GeoTransform>{t.c - shift.dx!r}, {t.a!r}, 0.0, {t.f - shift.dy!r}, 0.0, {t.e!r}</GeoTransform>")
    size = f'xOff="0" yOff="0" xSize="{info["width"]}" ySize="{info["height"]}"'
    for i, (source, band, colour) in enumerate(bands, 1):
        lines.append(f'<VRTRasterBand dataType="{dtype}" band="{i}">')
        if nodata is not None:
            lines.append(f"<NoDataValue>{nodata!r}</NoDataValue>")
        if colour:
            lines.append(f"<ColorInterp>{colour}</ColorInterp>")
        kind = "ComplexSource" if lift or nodata is not None else "SimpleSource"
        lines.append(f"<{kind}><SourceFilename relativeToVRT=\"0\">{escape(str(source))}</SourceFilename>"
                     f"<SourceBand>{band}</SourceBand><SrcRect {size}/><DstRect {size}/>")
        if kind == "ComplexSource":
            if nodata is not None:
                lines.append(f"<NODATA>{nodata!r}</NODATA>")
            lines.append(f"<ScaleOffset>{lift!r}</ScaleOffset><ScaleRatio>1</ScaleRatio>")
        lines.append(f"</{kind}></VRTRasterBand>")
    lines.append("</VRTDataset>")
    dst.write_text("\n".join(lines), encoding="utf-8")
    return dst


def _image_layer(kind: str, url: str, path: Path, visible: bool) -> dict:
    info = _raster_info(path)
    return {"id": kind, "type": "heights" if kind == "dem" else "image", "url": url, "title": TITLES[kind],
            "visible": visible, "size": [info["width"], info["height"]],
            "resolution": round(float(info["transform"].a), 6)}


# ------------------------------------------------------------------------------------------------ the site
def _prepare(folder: Path) -> None:
    """``folder`` ready for a site: new, empty, or a site built here before (its own files removed). A folder with
    anything else is never touched: :class:`FileExistsError`."""
    if folder.exists() and not folder.is_dir():
        raise FileExistsError(f"{folder} is a file: choose a folder")
    if folder.is_dir() and any(folder.iterdir()):
        try:
            ours = json.loads((folder / MANIFEST).read_text(encoding="utf-8")).get("format") == FORMAT
        except (OSError, ValueError, AttributeError):
            ours = False
        if not ours:
            raise FileExistsError(f"{folder} holds other files and is not a site built by this program: "
                                  "it was left as it is, choose another folder")
        for name in OWN:
            target = folder / name
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
    folder.mkdir(parents=True, exist_ok=True)


def _copy_viewer(folder: Path, title: str, language: str) -> None:
    if not (VIEWER / "index.html").exists():
        raise RuntimeError(f"the viewer is missing from this installation ({VIEWER})")
    shutil.copytree(VIEWER / "app", folder / "app")
    page = (VIEWER / "index.html").read_text(encoding="utf-8")
    page = page.replace("{{title}}", html.escape(title)).replace("{{language}}", html.escape(language))
    (folder / "index.html").write_text(page, encoding="utf-8")


def export_site(document, folder: str | os.PathLike, chunks=None, products=None, frame: str = "local",
                title: str | None = None, description: str | None = None, language=LANGUAGES, quality: int = 85,
                progress=None) -> Path:
    """Build the web site of ``document`` in ``folder`` (see the module docstring) and return the path of its
    manifest, ``site.json``.

    ``chunks``: chunk objects, keys or labels (default: every chunk with an orthophoto, a DEM or drawings; they
    must share one reference system); ``products``: among :data:`PRODUCTS` (default all, those the chunk has);
    ``frame``: ``"local"`` (default: shifted coordinates, no reference system written) or ``"crs"``;
    ``title`` and ``description``: of the page (default: the site of the project, else the name of its file);
    ``language``: the languages of the page among :data:`LANGUAGES`, the first shown first; ``quality``: of the
    WebP of the orthophoto. A folder that holds anything but a site built here is refused."""
    from ..archaeo.terrain import hillshade
    from .web import export_cog

    progress = as_progress(progress)
    if frame not in FRAMES:
        raise ValueError(f"frame: one of {', '.join(FRAMES)}")
    languages = [language] if isinstance(language, str) else list(language or LANGUAGES)
    if not languages or any(x not in LANGUAGES for x in languages):
        raise ValueError(f"language: among {', '.join(LANGUAGES)}")
    products = tuple(PRODUCTS if products is None else ([products] if isinstance(products, str) else products))
    if any(p not in PRODUCTS for p in products):
        raise ValueError(f"products: among {', '.join(PRODUCTS)}")
    chosen = _chunks(document, chunks)
    if not chosen:
        raise ValueError("nothing to publish: no chunk has an orthophoto, a DEM or drawings")
    systems = {None if crs_mod.is_local(c.crs) else c.crs for c in chosen}
    if len(systems) > 1:
        raise ValueError("the chunks are in different reference systems: " + ", ".join(sorted(map(str, systems))))
    crs = next(iter(systems))
    if crs is not None and crs_mod.is_geographic(crs):
        raise ValueError(f"{crs} is in degrees: a site needs a chunk in a metric system")
    folder = Path(folder)
    roots = _private_roots(document)

    # what every chunk gives, measured in its own coordinates: the origin of the local frame comes from all of them
    plan = []
    for ch in chosen:
        ortho = _raster(ch, "orthomosaic") if "orthomosaic" in products else None
        dem = _raster(ch, "dem") if "dem" in products else None
        raw = _Shift()
        units = _unit_features(ch, raw, roots) if "units" in products else []
        drawn = _drawing_features(ch, raw, roots)[0] if "drawings" in products else []
        box = _union([_raster_info(p)["bounds"] for p in (ortho, dem) if p is not None]
                     + [_bounds(units), _bounds(drawn)])
        if box is None:
            if chunks is None:
                continue
            raise ValueError(f"chunk {ch.label!r} has none of the products asked for")
        plan.append({"chunk": ch, "ortho": ortho, "dem": dem, "box": box,
                     "heights": _dem_heights(dem) if dem is not None else None,
                     "lowest": _lowest(units + drawn)})
    if not plan:
        raise ValueError("nothing to publish: no chunk has the products asked for")
    shift = _Shift()
    if frame == "local":
        box = _union(p["box"] for p in plan)
        # the heights start at the whole metre under the lowest cell of the DEMs: no cell is under zero and the
        # true heights are not told. The drawn heights decide only where there is no DEM: a drawing made
        # without heights lies at zero, and taking it as the base would publish the true heights of all the rest
        lows = [p["heights"][0] for p in plan if p["heights"]]
        if not lows:
            lows = [p["lowest"] for p in plan if p["lowest"] is not None]
        shift = _Shift(round((box[0] + box[2]) / 2), round((box[1] + box[3]) / 2),
                       float(math.floor(min(lows))) if lows else 0.0)
    written_crs = crs if frame == "crs" else None

    _prepare(folder)  # from here on the folder is written: everything that can be refused was, above
    entries = []
    for n, item in enumerate(plan):
        ch = item["chunk"]
        sub = progress.sub(n / len(plan), 1 / len(plan))
        rel = "data" if len(plan) == 1 else f"data/{n + 1}"
        data = folder / rel
        data.mkdir(parents=True, exist_ok=True)
        layers, boxes = [], []
        with tempfile.TemporaryDirectory(prefix=".ppg-site-", dir=folder) as tmp:
            tmp = Path(tmp)
            if item["ortho"] is not None:
                sub(0.05, f"{ch.label}: orthophoto")
                info = _raster_info(item["ortho"])
                if info["dtype"] != "uint8" or info["count"] < 3:
                    raise ValueError(f"{ch.label}: the orthophoto is not an 8-bit colour image")
                alpha = 4 if info["count"] >= 4 else "mask,1"
                bands = [(item["ortho"], 1, "Red"), (item["ortho"], 2, "Green"), (item["ortho"], 3, "Blue"),
                         (item["ortho"], alpha, "Alpha")]
                vrt = _vrt(tmp / "ortho.vrt", info, bands, "Byte", shift, frame == "crs")
                out = export_cog(vrt, data / "ortho.tif", "webp", quality)
                layers.append(_image_layer("ortho", f"{rel}/ortho.tif", out, True))
                boxes.append(_raster_info(out)["bounds"])
            if item["dem"] is not None:
                sub(0.45, f"{ch.label}: DEM")
                info = _raster_info(item["dem"])
                vrt = _vrt(tmp / "dem.vrt", info, [(item["dem"], 1, None)], "Float32", shift, frame == "crs",
                           nodata=info["nodata"], lift=-shift.dz)
                out = export_cog(vrt, data / "dem.tif", "deflate")
                sub(0.65, f"{ch.label}: shaded relief")
                shade = hillshade(item["dem"], tmp / "shade.tif")
                bands = [(shade, 1, "Red"), (shade, 1, "Green"), (shade, 1, "Blue"), (item["dem"], "mask,1", "Alpha")]
                vrt = _vrt(tmp / "shade.vrt", info, bands, "Byte", shift, frame == "crs")
                relief = export_cog(vrt, data / "dem_hillshade.tif", "webp", 90)
                layers.append(_image_layer("hillshade", f"{rel}/dem_hillshade.tif", relief, item["ortho"] is None))
                layer = _image_layer("dem", f"{rel}/dem.tif", out, True)
                if item["heights"]:
                    lo, hi = item["heights"]
                    layer["range"] = [round(lo - shift.dz, 3), round(hi - shift.dz, 3)]
                layers.append(layer)
                boxes.append(_raster_info(out)["bounds"])
        sub(0.9, f"{ch.label}: drawings")
        if "units" in products:
            feats = _unit_features(ch, shift, roots)
            if feats:
                _write_geojson(data / "units.geojson", feats, written_crs)
                layers.append({"id": "units", "type": "vector", "url": f"{rel}/units.geojson",
                               "title": TITLES["units"], "visible": True, "count": len(feats),
                               "colour": UNIT_COLOUR})
                boxes.append(_bounds(feats))
        if "drawings" in products:
            feats, groups = _drawing_features(ch, shift, roots)
            if feats:
                _write_geojson(data / "drawings.geojson", feats, written_crs)
                layers.append({"id": "drawings", "type": "vector", "url": f"{rel}/drawings.geojson",
                               "title": TITLES["drawings"], "visible": True, "count": len(feats),
                               "groups": groups})
                boxes.append(_bounds(feats))
        entries.append({"id": str(n + 1), "label": _scrub(str(ch.label), roots),
                        "extent": [round(v, DECIMALS) for v in _union(boxes)], "layers": layers})

    name = title or (document.meta or {}).get("sito") or (Path(document.path).stem if document.path else "Rilievo")
    from .. import __version__

    manifest = {
        "format": FORMAT, "version": FORMAT_VERSION,
        "generator": {"name": "pyarchinit-photogrammetry", "version": __version__},
        "created": date.today().isoformat(),
        "title": _scrub(str(name), roots), "description": _scrub(str(description or ""), roots),
        "languages": languages,
        "frame": frame, "crs": written_crs, "unit": "m",
        "fields": FIELDS,
        "chunks": entries,
    }
    progress(0.97, "viewer")
    _copy_viewer(folder, manifest["title"], languages[0])
    path = folder / MANIFEST
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    progress(1.0, f"site: {len(entries)} chunk(s) in {folder.name}")
    return path


# ------------------------------------------------------------------------------------------------ the local server
_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
          ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
          ".json": "application/json; charset=utf-8", ".geojson": "application/geo+json; charset=utf-8",
          ".tif": "image/tiff", ".tiff": "image/tiff", ".svg": "image/svg+xml", ".png": "image/png",
          ".jpg": "image/jpeg", ".webp": "image/webp", ".txt": "text/plain; charset=utf-8",
          ".md": "text/plain; charset=utf-8", ".glb": "model/gltf-binary", ".laz": "application/octet-stream"}


def _byte_range(header: str, size: int) -> tuple[int, int] | None:
    """``(first, last)`` of a ``Range`` header of one range of bytes within ``size``; None when it cannot be met."""
    unit, _, spec = header.partition("=")
    if unit.strip().lower() != "bytes" or "," in spec:
        return None
    first, dash, last = spec.strip().partition("-")
    try:
        if not dash:
            return None
        if first == "":  # the last n bytes
            n = int(last)
            return (max(0, size - n), size - 1) if n > 0 and size > 0 else None
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
    except ValueError:
        return None
    return (start, end) if 0 <= start <= end < size else None


class _Handler(SimpleHTTPRequestHandler):
    """Files of the folder, with ``Range`` requests of one range (206, ``Content-Range``); no list of a folder.
    ``ranges`` False: as a host that ignores ``Range`` and answers with the whole file."""

    protocol_version = "HTTP/1.1"
    quiet = True
    ranges = True

    def guess_type(self, path):
        return _TYPES.get(Path(str(path)).suffix.lower()) or super().guess_type(path)

    def list_directory(self, path):
        self.send_error(HTTPStatus.NOT_FOUND, "File not found")
        return None

    def end_headers(self):
        if self.ranges:
            self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_head(self):
        self._left = None
        header = self.headers.get("Range")
        path = Path(self.translate_path(self.path))
        if not header or not self.ranges or not path.is_file():
            return super().send_head()
        size = path.stat().st_size
        span = _byte_range(header, size)
        if span is None:
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        try:
            f = open(path, "rb")
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return None
        first, last = span
        f.seek(first)
        self._left = last - first + 1
        self.send_response(HTTPStatus.PARTIAL_CONTENT)
        self.send_header("Content-Type", self.guess_type(str(path)))
        self.send_header("Content-Range", f"bytes {first}-{last}/{size}")
        self.send_header("Content-Length", str(self._left))
        self.send_header("Last-Modified", self.date_time_string(path.stat().st_mtime))
        self.end_headers()
        return f

    def copyfile(self, source, outputfile):
        left = getattr(self, "_left", None)
        if left is None:
            return super().copyfile(source, outputfile)
        while left > 0:
            block = source.read(min(left, 64 * 1024))
            if not block:
                break
            outputfile.write(block)
            left -= len(block)

    def log_message(self, format, *args):  # noqa: A002 - the name of the base class
        if not self.quiet:
            super().log_message(format, *args)


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        import sys

        if isinstance(sys.exc_info()[1], ConnectionError):
            return  # the browser dropped a download it no longer wants: nothing to report
        super().handle_error(request, client_address)


def server(folder: str | os.PathLike, host: str = "127.0.0.1", port: int = 0, quiet: bool = True,
           ranges: bool = True):
    """A static server of ``folder`` with range requests, bound to ``host`` (this computer alone by default) and
    ``port`` (0: a free one, read it from ``server_address``); not started: ``serve_forever()`` runs it,
    ``shutdown()`` and ``server_close()`` end it. ``ranges`` False: the whole file whatever is asked, to see the
    page as on a host without range requests."""
    folder = Path(folder)
    if not folder.is_dir():
        raise FileNotFoundError(f"{folder} is not a folder")
    handler = type("SiteHandler", (_Handler,), {"quiet": quiet, "ranges": ranges})
    return _Server((host, port), partial(handler, directory=str(folder)))


__all__ = ["export_site", "server", "FRAMES", "PRODUCTS", "LANGUAGES", "MANIFEST", "FORMAT", "FORMAT_VERSION"]
