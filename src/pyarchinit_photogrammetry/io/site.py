"""A survey as a web site: one folder any static server can publish (since 1.10.0).

:func:`export_site` (``document.export_site``, ``ppg web build``) writes::

    site/
      index.html          the page
      site.json           the manifest: title, languages, frame, the chunks with their extent and layers
      app/                the viewer: plain ES modules, OpenLayers, three.js, 3DTilesRendererJS; no CDN, no build
      data/               (data/1/, data/2/... with several chunks)
        ortho.tif         the orthophoto, Cloud Optimized GeoTIFF, WebP with its alpha
        dem.tif           the DEM, COG float32 (deflate): the heights read on a click
        dem_hillshade.tif the shaded relief, COG 8 bit (WebP, transparent where the DEM is empty)
        units.geojson     the stratigraphic units of the plan with their attributes
        drawings.geojson  spot heights, break lines, measuring points and free drawings, each with its ``layer``
        model/            the 3D model as 3D Tiles 1.1 (tileset.json, tiles/*.glb, textures/*.jpg): the 3D view

``frame``: ``"local"`` (default) shifts every coordinate to an origin at the centre of the survey (whole metres; the
heights to the whole metre under the lowest cell of the DEMs, read whole, so that no cell is under zero; under the
lowest vertex of the models where there is no DEM, under the lowest drawn height where there is neither) and
writes no reference system anywhere: nothing in the folder says where the site is. The origin is not written
either. ``"crs"`` keeps the coordinates and the reference system of the chunks: an explicit choice. The model is in
the same frame: in the local one its tiles are in the metres of the plan, with no transform to the globe, so that a
point measured on the model and on the plan has the same coordinates; in the ``crs`` one its tileset is the one of
:func:`.web.export_3dtiles`, placed on the globe around the centre of the model, whose coordinates the manifest
gives (``origin`` of the layer) for the viewer. In both frames the rasters go through a description of their pixels
alone (a VRT), so no tag of the source files reaches the site, the photos and their EXIF are never copied, and every
text typed by the user - the attributes and their names, the names of chunks, layers and drawings, the title and the
description - that holds the path of a computer is cut to its last name.

:func:`server` (``ppg web serve``) is a static server with HTTP range requests - the viewer reads the rasters by
parts - bound to this computer alone unless another host is asked for; :class:`Preview` runs it in a thread of its
own and stops it (the window's pane *Pubblica sul web*).

The site built last from a project is kept in ``document.meta["web_site"]`` (:func:`record_site`): its folder
(relative to the project's when it lies there), the options, the time, the size and the state of the products it
holds; :func:`site_stale` says which of them were built again or changed since.

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
import threading
from datetime import date
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np

from ..core import crs as crs_mod
from ..core.progress import as_progress

FORMAT = "ppg-site"
FORMAT_VERSION = 1
FRAMES = ("local", "crs")
PRODUCTS = ("orthomosaic", "dem", "units", "drawings", "model", "cloud", "sections")  # the relief goes with the DEM
DEFAULT_PRODUCTS = ("orthomosaic", "dem", "units", "drawings", "model", "sections")  # the cloud only when asked
MODEL_TEXTURE = 4096  # the side of a texture page of the model on the site: a trench stays within a few hundred MB
MODEL_FACES = 150_000  # faces of a tile of the model
BUDGET_MB = 500.0  # the size of a whole site the build keeps to by thinning the model and the cloud
CLOUD_POINTS = 3_000_000  # points of a cloud at most: what a browser draws smoothly
MAX_MODEL_FACES = 4_000_000  # faces of a model at full detail a browser takes
MAX_TEXTURE = 8192  # side of a texture page a browser takes everywhere
MAX_TILE_FACES = 500_000  # faces of one tile
MIN_MODEL_FACES = 50_000  # the fewest a model is thinned to
MIN_CLOUD_POINTS = 100_000
MODEL_FACE_BYTES = 80.0  # bytes of a face of the model on the site (leaves and coarser levels), measured
TEXTURE_PIXEL_BYTES = 0.3  # bytes of a pixel of a texture page (JPEG 85), about
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
    "model": {"it": "Modello 3D", "en": "3D model"},
    "cloud": {"it": "Nuvola di punti", "en": "Point cloud"},
    "sections": {"it": "Sezioni e prospetti", "en": "Sections and elevations"},
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
            raise ValueError(f"chunk {ch.label!r} has nothing to publish: no orthophoto, DEM, model or drawings")
        out.append(ch)
    return out


def _raster(chunk, product: str) -> Path | None:
    path = chunk.orthomosaic_path if product == "orthomosaic" else chunk.dem_path
    return Path(path) if path is not None and Path(path).exists() else None


def _model(chunk) -> Path | None:
    path = chunk.product_path("model")
    return Path(path) if path is not None and Path(path).exists() else None


def _model_extent(chunk) -> tuple[tuple[float, float, float, float], float]:
    """The plan box of the model in the coordinates of the chunk and its lowest height."""
    return _model_info(chunk)[:2]


def _model_info(chunk) -> tuple[tuple[float, float, float, float], float, int, list[int]]:
    """The plan box of the model, its lowest height, its faces and the largest side of each texture page."""
    from PIL import Image

    from .web import _world_mesh

    world, faces, _c, uv, pages, _t = _world_mesh(chunk)
    lo, hi = world.min(0), world.max(0)
    sides = []
    for p in pages if uv is not None else []:
        try:
            with Image.open(p) as im:
                sides.append(max(im.size))
        except OSError:
            pass
    return (float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1])), float(lo[2]), int(len(faces)), sides


def _cloud(chunk) -> Path | None:
    path = chunk.product_path("point_cloud")
    return Path(path) if path is not None and Path(path).exists() else None


def _has_content(chunk) -> bool:
    from . import site_sections

    d = chunk.meta.get("drawings") or {}
    return bool(_raster(chunk, "orthomosaic") or _raster(chunk, "dem") or _model(chunk)
                or any(d.get(k) for k in ("units", "quote", "lines", "points", "free"))
                or site_sections.entries(chunk) or _cloud(chunk))


def _private_roots(document, others=()) -> list[str]:
    """Folders of this computer no text of the site may name: the home, the project's (and those of the projects of
    ``others``, chunks of other projects), the temporary one."""
    roots = {str(Path.home()), tempfile.gettempdir(), str(Path(tempfile.gettempdir()).resolve())}
    docs = [document] + [getattr(c, "document", None) for c in others]
    for doc in docs:
        if doc is not None and doc.path is not None:
            roots |= {str(Path(doc.path).parent), str(Path(doc.path).resolve().parent)}
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


def _tiles_box(node: dict) -> tuple[list[float], list[float]]:
    """The box of a tile (its ``box`` bounding volume, axis aligned as :func:`.web.export_3dtiles` writes it)."""
    b = node["boundingVolume"]["box"]
    h = (abs(b[3]), abs(b[7]), abs(b[11]))
    return [b[i] - h[i] for i in range(3)], [b[i] + h[i] for i in range(3)]


def _model_faces(folder: Path, tileset: dict) -> int:
    """The faces of the model at full detail: those of the leaves."""
    import struct

    total = 0

    def walk(node):
        nonlocal total
        if node.get("children"):
            for k in node["children"]:
                walk(k)
            return
        data = (folder / node["content"]["uri"]).read_bytes()
        size = struct.unpack_from("<I", data, 12)[0]
        gltf = json.loads(data[20:20 + size])
        for mesh in gltf["meshes"]:
            for prim in mesh["primitives"]:
                total += gltf["accessors"][prim["indices"]]["count"] // 3 if "indices" in prim else \
                    gltf["accessors"][prim["attributes"]["POSITION"]]["count"] // 3

    walk(tileset["root"])
    return total


# ------------------------------------------------------------------------------------------------ the site
def folder_kind(folder: str | os.PathLike) -> str:
    """What ``folder`` is for a site: ``"new"`` (not there), ``"empty"``, ``"site"`` (a site built by this program,
    which a build rewrites), ``"file"`` (not a folder) or ``"other"`` (a folder with other files: never written)."""
    folder = Path(folder)
    if not folder.exists():
        return "new"
    if not folder.is_dir():
        return "file"
    if not any(folder.iterdir()):
        return "empty"
    try:
        ours = json.loads((folder / MANIFEST).read_text(encoding="utf-8")).get("format") == FORMAT
    except (OSError, ValueError, AttributeError):
        ours = False
    return "site" if ours else "other"


def _prepare(folder: Path) -> None:
    """``folder`` ready for a site: new, empty, or a site built here before (its own files removed). A folder with
    anything else is never touched: :class:`FileExistsError`."""
    kind = folder_kind(folder)
    if kind == "file":
        raise FileExistsError(f"{folder} is a file: choose a folder")
    if kind == "other":
        raise FileExistsError(f"{folder} holds other files and is not a site built by this program: "
                              "it was left as it is, choose another folder")
    if kind == "site":
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


def _lookup(mapping, chunk):
    """The value ``mapping`` (a dict, or ``(chunk, value)`` pairs: a chunk object is no key of a dict) gives
    ``chunk``: by the chunk itself, its label or its key."""
    if not mapping:
        return None
    for k, v in (mapping.items() if isinstance(mapping, dict) else mapping):
        if k is chunk or (isinstance(k, str) and k in (str(chunk.label), str(chunk.key))):
            return v
    return None


def _iso_date(value) -> str | None:
    """A date (a ``date``, a ``datetime`` or an ISO text) as ``YYYY-MM-DD``; ValueError for a text that is not one."""
    from datetime import datetime

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        raise ValueError(f"date {text!r}: write it as YYYY-MM-DD") from None


def chunk_date(chunk) -> str | None:
    """When ``chunk`` was surveyed (``YYYY-MM-DD``): the date set on it, else the earliest date of its photos
    (:func:`..archaeo.timeline.phase_date`); None when neither is known."""
    from ..archaeo.timeline import phase_date

    try:
        d = phase_date(chunk)
    except Exception:  # noqa: BLE001 - photos that cannot be read give no date
        d = None
    return d.date().isoformat() if d is not None else None


def _model_bytes(faces: int, sides: list[int], side: int | None) -> float:
    tex = sum(min(s, side) ** 2 for s in sides) * TEXTURE_PIXEL_BYTES if side else 0.0
    return faces * MODEL_FACE_BYTES + tex


def _fit_model(share: float, faces: int, sides: list[int], side: int | None) -> tuple[int, int | None]:
    """The faces and the side of the texture pages of a model that fit in ``share`` bytes: the texture halved
    first (down to 1024 pixels) while it takes more than half of the share, then the faces."""
    while side and side > 1024 and sides and \
            sum(min(s, side) ** 2 for s in sides) * TEXTURE_PIXEL_BYTES > share / 2:
        side //= 2
    left = share - _model_bytes(0, sides, side)
    return int(min(faces, max(MIN_MODEL_FACES, left / MODEL_FACE_BYTES))), side


def _size(path: Path) -> int:
    return sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file()) if Path(path).is_dir() else \
        (Path(path).stat().st_size if Path(path).exists() else 0)


LAYER_ORDER = ("ortho", "hillshade", "dem", "model", "cloud", "sections", "units", "drawings")


def export_site(document, folder: str | os.PathLike, chunks=None, products=None, frame: str = "local",
                title: str | None = None, description: str | None = None, language=LANGUAGES, quality: int = 85,
                model_texture: int | None = MODEL_TEXTURE, model_faces: int = MODEL_FACES,
                budget_mb: float | None = BUDGET_MB, cloud_points: int | None = CLOUD_POINTS,
                cloud_copc: bool = True, labels: dict | None = None, dates: dict | None = None,
                timeline: bool | None = None, progress=None) -> Path:
    """Build the web site of ``document`` in ``folder`` (see the module docstring) and return the path of its
    manifest, ``site.json``.

    ``chunks``: chunk objects, keys or labels (default: every chunk with something to publish; they must share one
    reference system); chunk objects of other projects are taken as they are (several surveys of one site in one
    page: they must be in one frame, a common georeference or aligned chunks). ``products``: among :data:`PRODUCTS`
    (default :data:`DEFAULT_PRODUCTS`, those the chunk has: the point cloud only when asked for);
    ``frame``: ``"local"`` (default: shifted coordinates, no reference system written) or ``"crs"``;
    ``title`` and ``description``: of the page (default: the site of the project, else the name of its file);
    ``language``: the languages of the page among :data:`LANGUAGES`, the first shown first; ``quality``: of the
    WebP of the orthophotos; ``model_texture``: the largest side of a texture page of the model (None or 0: the
    vertex colours alone); ``model_faces``: the faces of a tile of the model.

    ``budget_mb``: the size of the whole site in MB (None or 0: no limit). The rasters, the sections and the
    drawings go as they are; the model and the cloud share what is left: the texture pages are made smaller and the
    model has fewer faces (:func:`.web.export_3dtiles` ``max_total_faces``), the cloud fewer points. A model is
    never above :data:`MAX_MODEL_FACES` faces, :data:`MAX_TEXTURE` pixels a page, :data:`MAX_TILE_FACES` faces a
    tile, a cloud above ``cloud_points`` points (default :data:`CLOUD_POINTS`), whatever the budget. What was
    reduced is said in ``build`` of ``site.json`` (``notes``, ``reduced``). ``cloud_copc``: the cloud also as COPC
    to download.

    ``labels``: ``{chunk: text}`` (by label or key; or ``[(chunk, text), ...]`` with chunk objects, those of other
    projects) the name of a chunk on the page; ``dates``: ``{chunk: "YYYY-MM-DD"}`` (or pairs) the date of its
    survey. ``timeline``: the page shows one date at a time with a
    selector of the dates (default: when ``dates`` is given); every chunk needs a date then - given, set on the
    chunk, or that of its photos (:func:`chunk_date`); chunks of one date are shown together. A folder that holds
    anything but a site built here is refused."""
    from ..archaeo.terrain import hillshade
    from . import site_cloud, site_plugins, site_sections
    from .web import export_3dtiles, export_cog

    progress = as_progress(progress)
    if frame not in FRAMES:
        raise ValueError(f"frame: one of {', '.join(FRAMES)}")
    languages = [language] if isinstance(language, str) else list(language or LANGUAGES)
    if not languages or any(x not in LANGUAGES for x in languages):
        raise ValueError(f"language: among {', '.join(LANGUAGES)}")
    products = tuple(DEFAULT_PRODUCTS if products is None else ([products] if isinstance(products, str)
                                                                 else products))
    if any(p not in PRODUCTS for p in products):
        raise ValueError(f"products: among {', '.join(PRODUCTS)}")
    chosen = _chunks(document, chunks)
    if not chosen:
        raise ValueError("nothing to publish: no chunk has an orthophoto, a DEM, a model or drawings")
    systems = {None if crs_mod.is_local(c.crs) else c.crs for c in chosen}
    if len(systems) > 1:
        raise ValueError("the chunks are in different reference systems: " + ", ".join(sorted(map(str, systems))))
    crs = next(iter(systems))
    if crs is not None and crs_mod.is_geographic(crs):
        raise ValueError(f"{crs} is in degrees: a site needs a chunk in a metric system")
    timeline = bool(dates) if timeline is None else bool(timeline)
    when = {}
    for ch in chosen:
        when[id(ch)] = _iso_date(_lookup(dates, ch)) or (chunk_date(ch) if timeline else None)
        if timeline and when[id(ch)] is None:
            raise ValueError(f"chunk {ch.label!r} has no date: give one (dates, --date) or set it on the chunk")
    folder = Path(folder)
    roots = _private_roots(document, chosen)
    budget = float(budget_mb) * 1e6 if budget_mb else None
    tile_faces = int(min(max(int(model_faces), 1000), MAX_TILE_FACES))
    texture = int(min(model_texture, MAX_TEXTURE)) if model_texture else None
    notes: list[str] = []
    reduced: list[dict] = []
    if model_texture and model_texture > MAX_TEXTURE:
        notes.append(f"texture pages capped at {MAX_TEXTURE} pixels a side, the largest every browser takes")

    # what every chunk gives, measured in its own coordinates: the origin of the local frame comes from all of them
    plan = []
    for ch in chosen:
        ortho = _raster(ch, "orthomosaic") if "orthomosaic" in products else None
        dem = _raster(ch, "dem") if "dem" in products else None
        raw = _Shift()
        units = _unit_features(ch, raw, roots) if "units" in products else []
        drawn = _drawing_features(ch, raw, roots)[0] if "drawings" in products else []
        model = _model_info(ch) if "model" in products and _model(ch) is not None else None
        cloud = _cloud(ch) if "cloud" in products else None
        sections = site_sections.entries(ch) if "sections" in products else []
        traces = []
        for entry in sections:
            from ..engine.elevation import bounds_of

            g = site_sections.trace(entry, bounds_of(ch, entry))
            if g is not None:
                traces.append({"type": "Feature", "properties": {}, "geometry": g})
        box = _union([_raster_info(p)["bounds"] for p in (ortho, dem) if p is not None]
                     + [_bounds(units), _bounds(drawn), model[0] if model else None, _bounds(traces)])
        cloud_box = None
        if cloud is not None:
            pc = ch.point_cloud.transformed(ch.transform)
            if len(pc):
                lo, hi = pc.xyz.min(0), pc.xyz.max(0)
                cloud_box = (float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1]))
                cloud = (pc, float(lo[2]))
            else:
                cloud = None
        if box is None:
            box = cloud_box
        if box is None and not sections:
            if chunks is None:
                continue
            raise ValueError(f"chunk {ch.label!r} has none of the products asked for")
        plan.append({"chunk": ch, "ortho": ortho, "dem": dem, "box": box, "model": model, "cloud": cloud,
                     "sections": sections, "heights": _dem_heights(dem) if dem is not None else None,
                     "lowest": _lowest(units + drawn)})
    if not plan:
        raise ValueError("nothing to publish: no chunk has the products asked for")
    shift = _Shift()
    if frame == "local":
        box = _union(p["box"] for p in plan) or (0.0, 0.0, 0.0, 0.0)
        # the heights start at the whole metre under the lowest cell of the DEMs: no cell is under zero and the
        # true heights are not told. The drawn heights decide only where there is no DEM: a drawing made
        # without heights lies at zero, and taking it as the base would publish the true heights of all the rest
        lows = [p["heights"][0] for p in plan if p["heights"]]
        if not lows:
            lows = [p["model"][1] for p in plan if p["model"]]
        if not lows:
            lows = [p["cloud"][1] for p in plan if p["cloud"]]
        if not lows:
            lows = [p["lowest"] for p in plan if p["lowest"] is not None]
        shift = _Shift(round((box[0] + box[2]) / 2), round((box[1] + box[3]) / 2),
                       float(math.floor(min(lows))) if lows else 0.0)
    written_crs = crs if frame == "crs" else None

    def clean(values):
        return _attributes(values, roots)

    def scrub(text):
        return _scrub(text, roots)

    _prepare(folder)  # from here on the folder is written: everything that can be refused was, above
    entries = []
    several = len(plan) > 1
    # first what goes as it is: the rasters, the sections, the drawings, what the plugins add
    for n, item in enumerate(plan):
        ch = item["chunk"]
        sub = progress.sub(0.6 * n / len(plan), 0.6 / len(plan))
        rel = "data" if not several else f"data/{n + 1}"
        data = folder / rel
        data.mkdir(parents=True, exist_ok=True)
        item["rel"], item["data"] = rel, data
        layers, boxes = {}, []
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
                layers["ortho"] = _image_layer("ortho", f"{rel}/ortho.tif", out, True)
                boxes.append(_raster_info(out)["bounds"])
            if item["dem"] is not None:
                sub(0.4, f"{ch.label}: DEM")
                info = _raster_info(item["dem"])
                vrt = _vrt(tmp / "dem.vrt", info, [(item["dem"], 1, None)], "Float32", shift, frame == "crs",
                           nodata=info["nodata"], lift=-shift.dz)
                out = export_cog(vrt, data / "dem.tif", "deflate")
                sub(0.6, f"{ch.label}: shaded relief")
                shade = hillshade(item["dem"], tmp / "shade.tif")
                bands = [(shade, 1, "Red"), (shade, 1, "Green"), (shade, 1, "Blue"), (item["dem"], "mask,1", "Alpha")]
                vrt = _vrt(tmp / "shade.vrt", info, bands, "Byte", shift, frame == "crs")
                relief = export_cog(vrt, data / "dem_hillshade.tif", "webp", 90)
                layers["hillshade"] = _image_layer("hillshade", f"{rel}/dem_hillshade.tif", relief,
                                                   item["ortho"] is None)
                layer = _image_layer("dem", f"{rel}/dem.tif", out, True)
                if item["heights"]:
                    lo, hi = item["heights"]
                    layer["range"] = [round(lo - shift.dz, 3), round(hi - shift.dz, 3)]
                layers["dem"] = layer
                boxes.append(_raster_info(out)["bounds"])
        section_list = []
        if item["sections"]:
            sub(0.75, f"{ch.label}: sections")
            section_list, traces, _ = site_sections.write_sections(
                ch, data, rel, shift, quality, clean, scrub, progress=sub.sub(0.75, 0.15),
                prefix=f"{n + 1}-" if several else "")
            if traces:
                _write_geojson(data / "sections.geojson", traces, written_crs)
                layers["sections"] = {"id": "sections", "type": "vector", "url": f"{rel}/sections.geojson",
                                      "title": TITLES["sections"], "visible": True, "count": len(traces),
                                      "colour": site_sections.COLOUR}
                boxes.append(_bounds(traces))
        sub(0.9, f"{ch.label}: drawings")
        if "units" in products:
            feats = _unit_features(ch, shift, roots)
            if feats:
                _write_geojson(data / "units.geojson", feats, written_crs)
                layers["units"] = {"id": "units", "type": "vector", "url": f"{rel}/units.geojson",
                                   "title": TITLES["units"], "visible": True, "count": len(feats),
                                   "colour": UNIT_COLOUR}
                boxes.append(_bounds(feats))
        if "drawings" in products:
            feats, groups = _drawing_features(ch, shift, roots)
            if feats:
                _write_geojson(data / "drawings.geojson", feats, written_crs)
                layers["drawings"] = {"id": "drawings", "type": "vector", "url": f"{rel}/drawings.geojson",
                                      "title": TITLES["drawings"], "visible": True, "count": len(feats),
                                      "groups": groups}
                boxes.append(_bounds(feats))
        plugin_layers, plugin_list, _ = site_plugins.write(ch, data, rel, shift, clean, scrub, notes)
        for layer in plugin_layers:
            layers[layer["id"]] = layer
            path = folder / layer["url"]
            boxes.append(_bounds(json.loads(path.read_text(encoding="utf-8"))["features"]))
        label = _lookup(labels, ch)
        entry = {"id": str(n + 1), "label": scrub(str(label if label else ch.label)), "layers": layers,
                 "boxes": boxes}
        if when.get(id(ch)):
            entry["date"] = when[id(ch)]
        if section_list:
            entry["sections"] = section_list
        if plugin_list:
            entry["plugins"] = plugin_list
        entries.append(entry)

    # then the model and the cloud, in what the budget leaves them
    viewer = _size(VIEWER)
    fixed = _size(folder) + viewer
    wants = []
    for item in plan:
        model = item["model"]
        if model is not None:
            faces = model[2]
            if faces > MAX_MODEL_FACES:
                reduced.append({"chunk": item["chunk"].label, "product": "model", "what": "faces",
                                "from": faces, "to": MAX_MODEL_FACES, "why": "browser"})
                faces = MAX_MODEL_FACES
            item["model_plan"] = [faces, texture if model[3] else None]
            wants.append(("model", item, _model_bytes(faces, model[3], item["model_plan"][1])))
        if item["cloud"] is not None:
            per = site_cloud.POINT_BYTES + (site_cloud.COPC_BYTES if cloud_copc else 0)
            n_all = len(item["cloud"][0])
            points = min(n_all, int(cloud_points or CLOUD_POINTS))
            if points < n_all:
                reduced.append({"chunk": item["chunk"].label, "product": "cloud", "what": "points", "from": n_all,
                                "to": points, "why": "browser"})
            item["cloud_plan"] = points
            wants.append(("cloud", item, points * per))
    total = sum(w for _, _, w in wants)
    left = (budget - fixed) if budget else None
    if budget and fixed > budget:
        notes.append(f"the orthophotos, DEMs, sections and drawings alone take {fixed / 1e6:.0f} MB, over the "
                     f"budget of {budget / 1e6:.0f} MB: the model and the cloud are kept to their least")
    if left is not None and total > left:
        for kind, item, want in wants:
            share = max(left, 0.0) * want / total
            label = item["chunk"].label
            if kind == "model":
                faces, side = item["model_plan"]
                new_faces, new_side = _fit_model(share, faces, item["model"][3], side)
                if new_side != side:
                    reduced.append({"chunk": label, "product": "model", "what": "texture", "from": side,
                                    "to": new_side, "why": "budget"})
                if new_faces < faces:
                    reduced.append({"chunk": label, "product": "model", "what": "faces", "from": faces,
                                    "to": new_faces, "why": "budget"})
                item["model_plan"] = [new_faces, new_side]
            else:
                per = site_cloud.POINT_BYTES + (site_cloud.COPC_BYTES if cloud_copc else 0)
                points = int(max(MIN_CLOUD_POINTS, share / per))
                if points < item["cloud_plan"]:
                    reduced.append({"chunk": label, "product": "cloud", "what": "points",
                                    "from": item["cloud_plan"], "to": points, "why": "budget"})
                    item["cloud_plan"] = points
    for n, (item, entry) in enumerate(zip(plan, entries)):
        ch, rel, data = item["chunk"], item["rel"], item["data"]
        sub = progress.sub(0.6 + 0.35 * n / len(plan), 0.35 / len(plan))
        layers, boxes = entry["layers"], entry.pop("boxes")
        if item["model"] is not None:
            sub(0.0, f"{ch.label}: 3D model")
            faces, side = item["model_plan"]
            tiles = data / "model"
            # local frame: the tiles in the metres of the plan, nothing that places them on the globe; crs frame:
            # the tileset of the program, around the centre of the model, whose coordinates the viewer adds
            tileset = export_3dtiles(ch, tiles, max_faces=tile_faces, progress=sub.sub(0.0, 0.6),
                                     origin=(shift.dx, shift.dy, shift.dz) if frame == "local" else None,
                                     texture_size=side if model_texture else None,
                                     max_total_faces=faces if faces < item["model"][2] else None)
            ts = json.loads(tileset.read_text(encoding="utf-8"))
            layer = {"id": "model", "type": "tiles", "url": f"{rel}/model/tileset.json", "title": TITLES["model"],
                     "visible": True, "faces": _model_faces(tiles, ts),
                     "textured": bool((ts.get("extras") or {}).get("textures"))}
            if frame == "crs":
                layer["origin"] = (ts.get("extras") or {}).get("origin")
            lo, hi = _tiles_box(ts["root"])
            o = layer.get("origin") or [0.0, 0.0, 0.0]
            layer["box"] = [round(lo[i] + o[i], DECIMALS) for i in range(3)] + \
                [round(hi[i] + o[i], DECIMALS) for i in range(3)]
            layer["bytes"] = _size(tiles)
            layers["model"] = layer
            boxes.append((layer["box"][0], layer["box"][1], layer["box"][3], layer["box"][4]))
        if item["cloud"] is not None:
            sub(0.6, f"{ch.label}: point cloud")
            pc = item["cloud"][0]
            keep = site_cloud.thin(len(pc), item["cloud_plan"])
            xyz = pc.xyz[keep]
            rgb = pc.rgb[keep] if pc.rgb is not None else None
            if frame == "local":
                local = xyz - np.array([shift.dx, shift.dy, shift.dz])
                transform, extras, origin = None, None, None
            else:
                from .web import _enu_to_ecef

                centre = (xyz.min(0) + xyz.max(0)) / 2
                local = xyz - centre
                transform = _enu_to_ecef(ch, centre)
                origin = [float(v) for v in centre]
                extras = {"crs": ch.crs, "origin": origin}
            tileset = site_cloud.write_cloud_tiles(data / "cloud", local, rgb, transform=transform, extras=extras,
                                                   progress=sub.sub(0.6, 0.3))
            ts = json.loads(tileset.read_text(encoding="utf-8"))
            layer = {"id": "cloud", "type": "points", "url": f"{rel}/cloud/tileset.json", "title": TITLES["cloud"],
                     "visible": "model" not in layers, "points": int(len(keep)), "bytes": _size(data / "cloud")}
            if origin is not None:
                layer["origin"] = origin
            lo, hi = _tiles_box(ts["root"])
            o = origin or [0.0, 0.0, 0.0]
            layer["box"] = [round(lo[i] + o[i], DECIMALS) for i in range(3)] + \
                [round(hi[i] + o[i], DECIMALS) for i in range(3)]
            if cloud_copc:
                from .copc import write_copc

                copc = write_copc(data / "cloud.copc.laz", local if frame == "local" else xyz, rgb,
                                  crs_wkt=crs_mod.wkt(ch.crs) if frame == "crs" else None)
                layer["download"] = f"{rel}/cloud.copc.laz"
                layer["bytes"] += copc.stat().st_size
            layers["cloud"] = layer
            boxes.append((layer["box"][0], layer["box"][1], layer["box"][3], layer["box"][4]))
        order = [k for k in LAYER_ORDER if k in layers] + [k for k in layers if k not in LAYER_ORDER]
        entry["layers"] = [layers[k] for k in order]
        extent = _union(boxes) or _union([(s["trace"]["coordinates"][0][0], s["trace"]["coordinates"][0][1]) * 2
                                          for s in entry.get("sections", []) if "trace" in s]) or (-1, -1, 1, 1)
        entry["extent"] = [round(v, DECIMALS) for v in extent]
        entry_keys = ["id", "label", "date", "extent", "layers", "sections", "plugins"]
        entries[n] = {k: entry[k] for k in entry_keys if k in entry}

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
    if timeline:
        days: dict[str, list[str]] = {}
        for e in entries:
            days.setdefault(e["date"], []).append(e["id"])
        manifest["timeline"] = [{"date": d, "label": " · ".join(e["label"] for e in entries if e["id"] in ids),
                                 "chunks": ids} for d, ids in sorted(days.items())]
    progress(0.97, "viewer")
    _copy_viewer(folder, manifest["title"], languages[0])
    size = _size(folder)
    for r in reduced:
        what = {"faces": "faces", "texture": "pixels a side of the texture", "points": "points"}[r["what"]]
        why = "the size budget" if r["why"] == "budget" else "what a browser takes"
        notes.append(f"{r['chunk']}: {r['product']} reduced from {r['from']} to {r['to']} {what} ({why})")
    if budget and size > budget:
        notes.append(f"the site is {size / 1e6:.0f} MB, over the budget of {budget / 1e6:.0f} MB")
    manifest["build"] = {"budget": int(budget) if budget else None, "size": size,
                         "reduced": [{k: (scrub(str(v)) if k == "chunk" else v) for k, v in r.items()}
                                     for r in reduced],
                         "notes": [scrub(x) for x in notes]}
    path = folder / MANIFEST
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    progress(1.0, f"site: {len(entries)} chunk(s) in {folder.name}")
    return path


def build_notes(manifest: str | os.PathLike) -> list[str]:
    """What the build of the site of ``manifest`` (its ``site.json``) reduced or could not keep to."""
    try:
        return list((json.loads(Path(manifest).read_text(encoding="utf-8")).get("build") or {}).get("notes") or [])
    except (OSError, ValueError):
        return []


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


class Preview:
    """A :func:`server` of ``folder`` running in a thread of its own, to look at a site from a program that goes on
    (the window's *Pubblica sul web*): :meth:`start` binds and serves, :meth:`stop` ends the thread and frees the
    port. Bound to this computer alone unless another host is asked for."""

    def __init__(self, folder: str | os.PathLike, host: str = "127.0.0.1", port: int = 0, ranges: bool = True):
        self.folder = Path(folder)
        self.host = host
        self.httpd = server(self.folder, host=host, port=port, ranges=ranges)
        self.thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    @property
    def url(self) -> str:
        local = self.host in ("127.0.0.1", "localhost", "::1")
        return f"http://{'127.0.0.1' if local else self.host}:{self.port}/"

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> "Preview":
        if self.thread is None:
            self.thread = threading.Thread(target=self.httpd.serve_forever, name="ppg-site-preview", daemon=True)
            self.thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        """The server ended, its socket closed and its thread joined; again: nothing."""
        if self.thread is not None:
            self.httpd.shutdown()
            self.thread.join(timeout)
            self.thread = None
        self.httpd.server_close()


# ------------------------------------------------------------------------------------------------ the site of a project
META_KEY = "web_site"  # ``document.meta[META_KEY]``: the site built last from the project (:func:`record_site`)
_DRAWN = ("quote", "lines", "points", "free")  # the drawings of the plan that go in ``drawings``


def chunk_products(chunk) -> list[str]:
    """The products of :data:`PRODUCTS` that ``chunk`` has now, in their order."""
    d = chunk.meta.get("drawings") or {}
    have = {"orthomosaic": _raster(chunk, "orthomosaic") is not None, "dem": _raster(chunk, "dem") is not None,
            "units": bool(d.get("units")), "drawings": any(d.get(k) for k in _DRAWN),
            "model": _model(chunk) is not None, "cloud": _cloud(chunk) is not None,
            "sections": bool(_section_entries(chunk))}
    return [p for p in PRODUCTS if have[p]]


def _section_entries(chunk) -> list[dict]:
    from .site_sections import entries

    return entries(chunk)


def _file_state(path: Path | None) -> list | None:
    try:
        return [path.name, int(path.stat().st_mtime_ns)] if path is not None else None
    except OSError:
        return None


def _drawn_state(chunk, keys) -> str:
    import hashlib

    d = chunk.meta.get("drawings") or {}
    text = json.dumps([d.get(k) for k in keys], sort_keys=True, default=str)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def product_state(chunk, product: str):
    """What tells ``product`` of ``chunk`` from the same product built again or changed later: the name and the time
    of change of its files (a raster, the cloud; the model with its texture pages; the orthophotos of the sections),
    a digest of the records (units, drawings, the sections and their drawings)."""
    if product in ("orthomosaic", "dem"):
        return _file_state(_raster(chunk, product))
    if product == "cloud":
        return _file_state(_cloud(chunk))
    if product == "sections":
        from ..engine.elevation import elevation_paths

        import hashlib

        found = _section_entries(chunk)
        digest = hashlib.sha1(json.dumps(found, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        return [_drawn_state(chunk, ("frames",)), digest] + [_file_state(elevation_paths(chunk, e)[0]) for e in found]
    if product == "model":
        from ..engine.texturing import texture_pages

        model = _model(chunk)
        return None if model is None else [_file_state(model)] + [_file_state(Path(p)) for p in texture_pages(chunk)]
    return _drawn_state(chunk, ("units",) if product == "units" else _DRAWN)


def site_sources(document, chunks=None, products=None) -> dict:
    """The state of what a site of ``chunks`` with ``products`` (as :func:`export_site` takes them) is made of, chunk
    by chunk (by key): the transform of the chunk and :func:`product_state` of each of its products that goes in."""
    products = tuple(DEFAULT_PRODUCTS if products is None else ([products] if isinstance(products, str)
                                                                 else products))
    out = {}
    for ch in _chunks(document, chunks):
        had = [p for p in chunk_products(ch) if p in products]
        if not had:
            continue
        out[str(ch.key)] = {"label": str(ch.label), "placed": np.asarray(ch.transform, float).tolist(),
                            "products": {p: product_state(ch, p) for p in had}}
    return out


def _stored_folder(document, folder: Path) -> str:
    """``folder`` relative to the folder of the project (posix) when it lies in it - the project and its site move
    together -, else absolute."""
    folder = Path(folder).resolve()
    if document.path is not None:
        try:
            return folder.relative_to(Path(document.path).resolve().parent).as_posix()
        except ValueError:
            pass
    return str(folder)


def record_site(document, manifest: str | os.PathLike, options: dict, sources: dict) -> dict:
    """The site just built (``manifest``, its ``site.json``) kept with the project in ``document.meta``: its folder
    (:func:`_stored_folder`), the ``options`` it was built with, when, its size, the chunks and the products it holds
    and their ``sources`` (:func:`site_sources`, taken before the build). Returned."""
    from datetime import datetime

    folder = Path(manifest).parent
    try:
        data = json.loads(Path(manifest).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    record = {"folder": _stored_folder(document, folder), "options": dict(options),
              "when": datetime.now().isoformat(timespec="seconds"), "size": site_size(folder),
              "frame": data.get("frame", options.get("frame")), "sources": sources}
    document.meta[META_KEY] = record
    return record


def site_size(folder: str | os.PathLike) -> int:
    """The bytes of the files of a site folder."""
    return sum(f.stat().st_size for f in Path(folder).rglob("*") if f.is_file())


def site_record(document) -> dict | None:
    """The site built last from the project (:func:`record_site`), None without one."""
    record = document.meta.get(META_KEY)
    return record if isinstance(record, dict) and record.get("folder") else None


def site_folder(document, record: dict) -> Path:
    """The folder of ``record``: a relative one is read against the folder of the project."""
    folder = Path(str(record.get("folder")))
    if not folder.is_absolute() and document.path is not None:
        folder = Path(document.path).parent / folder
    return folder


def site_stale(document, record: dict) -> list[str]:
    """What changed since the site of ``record`` was built, as ``"<chunk>: <product>"`` (or the chunk alone when it
    was removed or placed elsewhere): a product built again, changed or removed. Empty: the site is current."""
    from ..core.stamps import same_transform

    chunks = {str(c.key): c for c in document.chunks}
    out = []
    for key, src in (record.get("sources") or {}).items():
        ch = chunks.get(str(key))
        label = str(ch.label if ch is not None else src.get("label", key))
        placed = src.get("placed")
        if ch is None or placed is None or not same_transform(placed, ch.transform):
            out.append(label)
            continue
        for product, state in (src.get("products") or {}).items():
            if json.loads(json.dumps(product_state(ch, product))) != state:
                out.append(f"{label}: {product}")
    return out


__all__ = ["export_site", "build_notes", "chunk_date", "server", "Preview", "folder_kind", "chunk_products",
           "site_sources", "record_site", "site_record", "site_folder", "site_stale", "site_size", "FRAMES",
           "PRODUCTS", "DEFAULT_PRODUCTS", "BUDGET_MB", "LANGUAGES", "MANIFEST", "FORMAT", "FORMAT_VERSION",
           "META_KEY"]
