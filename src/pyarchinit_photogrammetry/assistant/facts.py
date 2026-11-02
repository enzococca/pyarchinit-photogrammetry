"""Read-only fact tools over the open project, for the assistant.

The model calls them by name with JSON arguments (:data:`TOOLS` describes them, :func:`run_tool` runs one). They
answer with derived numbers only - resolutions, sizes, coverage, heights above the lowest point, counts, RMSEs -
never with bounds, origins, centres or positions: what the privacy filter (:mod:`.privacy`) lets through to an
external model is what the tools say. A missing product answers ``{"none": "..."}`` (a text for the user, in
Italian like the messages of the engine), a wrong tool or chunk ``{"error": "..."}``, and a tool that fails for
another reason the error as ``{"error": "<type>: <message>"}``: :func:`run_tool` never raises.

Raster statistics read an overview of the file (at most :data:`MAX_SAMPLES` cells on a side), the point clouds
and the meshes only their headers (and the class or confidence column, memory-mapped): a project of a tell answers
in a moment. No GUI here.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .knowledge import LANGUAGES, Index, default_index, guide_folder
from .providers import ToolSpec

log = logging.getLogger(__name__)

MAX_SAMPLES = 2000  # cells on a side of the overview the raster statistics read
API_PAGES = ("scripting", "api")  # the pages scripting_api searches (without language suffix)
_CHUNK_PARAM = {"type": ["string", "integer"],
                "description": "The chunk, by its label or its key; the active chunk when left out."}
# the surface a DEM came from, in words
_SOURCES = {"point_cloud": "dense cloud", "model": "mesh", "tie_points": "tie points", "depth_maps": "depth maps"}
_LANG_PARAM = {"type": "string", "enum": ["it", "en"],
               "description": "The language of the guide to search: 'it' (default) or 'en'."}


def _schema(**props: dict) -> dict:
    required = [k for k, v in props.items() if v.pop("required", False)]
    schema: dict = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


def _chunk_schema() -> dict:
    return _schema(chunk=dict(_CHUNK_PARAM))


TOOLS: list[ToolSpec] = [
    ToolSpec("project_summary",
             "The open project: its name, its site, and every chunk with the numbers of photos and of aligned photos, "
             "its coordinate system and the products it has (dem, orthomosaic, model, point_cloud, tie_points, ...).",
             _schema()),
    ToolSpec("dem_statistics",
             "The DEM of a chunk: resolution in metres per cell, size in cells, coverage, and the heights above its "
             "lowest point (max, median, 5th and 95th percentiles); the active version and the coordinate system.",
             _chunk_schema()),
    ToolSpec("orthomosaic_info",
             "The orthomosaic of a chunk: resolution, size, bands, blending, number of patches, active version, "
             "coordinate system.", _chunk_schema()),
    ToolSpec("alignment_report",
             "The alignment of a chunk: photos and aligned photos, tie points, mean reprojection error in pixels, "
             "matcher and mapper, the RMSE of the control and check points (GCP) and of the scale bars, whether the "
             "cameras were optimised.", _chunk_schema()),
    ToolSpec("markers",
             "The markers of a chunk, one by one: name, number of projections on the photos, role (control or check) "
             "and error in metres against the surveyed reference; no coordinates.", _chunk_schema()),
    ToolSpec("thermal_info",
             "The thermal survey of a chunk: the object parameters (emissivity, reflected and ambient temperature, "
             "humidity, distance), the frames decoded, the temperatures of the thermal orthomosaic (2nd and 98th "
             "percentile, median), the balance between frames.", _chunk_schema()),
    ToolSpec("multispectral_info",
             "The multispectral survey of a chunk: bands, captures, reference band, whether the reflectance "
             "orthomosaic exists, the indices computed, the registration quality of the bands and the spread the "
             "balance took away.", _chunk_schema()),
    ToolSpec("point_cloud_info",
             "The dense point cloud of a chunk: number of points, points per class (with the class names) and per "
             "confidence range (depth maps that agree on the point).", _chunk_schema()),
    ToolSpec("model_info",
             "The 3D model (mesh) of a chunk: faces, vertices, texture pages and their size in pixels.",
             _chunk_schema()),
    ToolSpec("guide_search",
             "Search the user guide of the program: the passages that best answer a question (how a step is done, "
             "what a dialog asks, what a product is). Returns their title, page and text.",
             _schema(query={"type": "string", "description": "The question or the words to look for.",
                            "required": True},
                     k={"type": "integer", "minimum": 1, "maximum": 20, "description": "How many passages (5)."},
                     lang=dict(_LANG_PARAM))),
    ToolSpec("scripting_api",
             "Search the scripting and API pages of the guide only: the Python functions of the program (Chunk and "
             "Document methods, their parameters), how a script is run with ppg-gui --run or the console.",
             _schema(topic={"type": "string", "description": "The function, parameter or subject to look for.",
                            "required": True},
                     k={"type": "integer", "minimum": 1, "maximum": 20, "description": "How many passages (5)."},
                     lang=dict(_LANG_PARAM))),
]


def tool_names() -> list[str]:
    return [t.name for t in TOOLS]


# ------------------------------------------------------------------ helpers
def _num(x, digits: int = 4):
    """A plain Python number rounded to ``digits`` decimals for the model (None stays None)."""
    if x is None:
        return None
    v = float(x)
    if not np.isfinite(v):
        return None
    if v == int(v) and abs(v) < 1e15:
        return int(v)
    return round(v, digits)


def _version_name(chunk, product: str) -> str | None:
    """The name of the active version of a product, when one was named (versions kept or a name given)."""
    info = (chunk.meta.get("active_versions") or {}).get(product) or {}
    if info.get("name"):
        return str(info["name"])
    if (chunk.meta.get("versions") or {}).get(product):
        from ..core import versions

        return str(versions.active_info(chunk, product).get("name") or "") or None
    return None


def _existing(chunk, product: str) -> Path | None:
    p = chunk.product_path(product)
    return p if p is not None and p.exists() else None


def _ply_counts(path: Path) -> dict[str, int]:
    """``{element: count}`` from the header of a binary PLY (empty for other files)."""
    from ..io.mesh_ply import read_header

    try:
        with open(path, "rb") as f:
            _, elements = read_header(f)
    except (OSError, ValueError, KeyError):
        return {}
    return {e["name"]: int(e["count"]) for e in elements}


def _resolution(ds):
    """The cell size of a dataset: one number for square cells, ``[x, y]`` when they differ."""
    x, y = abs(float(ds.res[0])), abs(float(ds.res[1]))
    return _num(x, 10) if abs(x - y) <= 1e-9 else [_num(x, 10), _num(y, 10)]


def _raster_info(ds) -> dict:
    return {"width": int(ds.width), "height": int(ds.height), "bands": int(ds.count), "resolution": _resolution(ds)}


def _raster_sample(path: Path, band: int = 1):
    """The dataset size and resolution and a masked overview of one band with at most :data:`MAX_SAMPLES` cells
    on a side (nodata and non-finite values masked)."""
    import rasterio

    with rasterio.open(path) as ds:
        scale = max(ds.width, ds.height) / MAX_SAMPLES
        h = max(1, int(round(ds.height / scale))) if scale > 1 else ds.height
        w = max(1, int(round(ds.width / scale))) if scale > 1 else ds.width
        data = ds.read(band, masked=True, out_shape=(h, w))
        info = _raster_info(ds)
    data = np.ma.masked_invalid(np.ma.asarray(data, dtype=np.float64))
    return info, data


def _resolve_chunk(document, arguments: dict):
    """The chunk the arguments name (label or key) or the active one; ``(chunk, None)`` or ``(None, answer)``."""
    if document is None:
        return None, {"none": "nessun progetto aperto"}
    labels = ", ".join(f"{c.label!r} (key {c.key})" for c in document.chunks) or "none"
    wanted = arguments.get("chunk")
    if wanted in (None, ""):
        chunk = document.chunk
        if chunk is None:
            if document.chunks:
                return None, {"none": f"nessun chunk attivo (sono tutti disattivati): i chunk sono {labels}"}
            return None, {"none": "il progetto non ha chunk"}
        return chunk, None
    if isinstance(wanted, bool) or not isinstance(wanted, (str, int)):
        return None, {"error": f"chunk must be a label or a key, not {wanted!r}: "
                               f"the chunks of the project are {labels}"}
    for c in document.chunks:
        if c.label == str(wanted):
            return c, None
    try:
        key = int(wanted)
    except (TypeError, ValueError):
        key = None
    for c in document.chunks:
        if key is not None and c.key == key:
            return c, None
    return None, {"error": f"unknown chunk {wanted!r}: the chunks of the project are {labels}"}


# ------------------------------------------------------------------ the tools
def project_summary(document, arguments: dict, index: Index) -> dict:
    if document is None:
        return {"none": "nessun progetto aperto"}
    chunks = []
    empty = True
    for c in document.chunks:
        products = sorted(n for n in c.products if _existing(c, n) is not None)
        if c.cameras or products:
            empty = False
        chunks.append({"label": c.label, "key": c.key, "photos": len(c.cameras), "aligned": len(c.aligned_cameras),
                       "crs": c.crs, "products": products, "enabled": bool(c.enabled),
                       "markers": len(c.markers), "scalebars": len(c.scalebars)})
    out = {"project": document.path.stem if document.path is not None else None, "chunks": chunks,
           "site": document.meta.get("sito") or None}
    if not chunks:
        out["none"] = "il progetto non ha chunk"
    elif empty:
        out["none"] = "il progetto è vuoto: nessuna foto e nessun prodotto"
    return out


def dem_statistics(chunk, arguments: dict, index: Index) -> dict:
    path = _existing(chunk, "dem")
    if path is None:
        return {"none": "il chunk non ha un DEM"}
    info, z = _raster_sample(path)
    src = (chunk.meta.get("dem") or {}).get("source")
    total = int(z.size)
    valid = int(total - np.ma.count_masked(z)) if total else 0
    out = {"resolution": info["resolution"], "width": info["width"], "height": info["height"],
           "coverage_percent": _num(100.0 * valid / total if total else 0.0, 2),
           "nodata_percent": _num(100.0 * (total - valid) / total if total else 100.0, 2),
           "version": _version_name(chunk, "dem"), "crs": chunk.crs,
           "source": _SOURCES.get(src, src)}
    if valid:
        values = np.asarray(z.compressed(), np.float64)
        h = values - values.min()
        p05, med, p95 = np.percentile(h, [5, 50, 95])
        out["height_above_lowest"] = {"min": 0, "max": _num(h.max(), 3), "median": _num(med, 3),
                                      "p05": _num(p05, 3), "p95": _num(p95, 3)}
    else:
        out["height_above_lowest"] = None
    return out


def orthomosaic_info(chunk, arguments: dict, index: Index) -> dict:
    from ..engine.ortho import stored_blending

    path = _existing(chunk, "orthomosaic")
    if path is None:
        return {"none": "il chunk non ha un'ortofoto"}
    import rasterio

    with rasterio.open(path) as ds:
        info = _raster_info(ds)
    meta = chunk.meta.get("orthomosaic") or {}
    blending, sigma = stored_blending(meta)
    return {"resolution": info["resolution"], "width": info["width"], "height": info["height"],
            "bands": info["bands"], "blending": blending,
            "blend_sigma": _num(sigma, 2) if blending == "mosaic" else None,
            "patches": len(meta.get("patches") or []), "surface": meta.get("surface"),
            "colour_correction": bool(meta.get("colour_correction")),
            "version": _version_name(chunk, "orthomosaic"), "crs": chunk.crs}


def _sig(x, digits: int = 5):
    """A plain Python number with ``digits`` significant digits (an error of 0.4 mm keeps its figures)."""
    if x is None or not np.isfinite(float(x)) or float(x) == 0.0:
        return _num(x)
    v = float(x)
    return round(v, max(0, digits - 1 - int(np.floor(np.log10(abs(v))))))


def _rmse(errors: list) -> float | None:
    e = [float(x) for x in errors if x is not None and np.isfinite(x)]
    return _sig(np.sqrt(np.mean(np.square(e)))) if e else None


_REPROJECTION_CACHE: dict[tuple[str, int], float] = {}  # (reconstruction folder, mtime_ns of points3D.bin) -> px


def _reprojection_error(chunk) -> tuple[float | None, str | None]:
    """The mean reprojection error in pixels and where it came from: ``"stored"`` (the alignment or the
    optimisation wrote it in ``meta["sfm"]``) or ``"computed"`` from the COLMAP model, once per model file (a
    block of a thousand photos takes seconds to load: the value is kept until the file changes)."""
    sfm = chunk.meta.get("sfm") or {}
    err = sfm.get("reprojection_error")
    if err is None:
        err = (sfm.get("optimize") or {}).get("reprojection_error")
    if err is not None:
        return _num(err, 4), "stored"
    folder = _existing(chunk, "reconstruction")
    if folder is None:
        return None, None
    points = folder / "points3D.bin"
    stamp = (points if points.exists() else folder).stat().st_mtime_ns
    key = (str(folder), stamp)
    if key not in _REPROJECTION_CACHE:
        from ..engine.sfm import load_reconstruction

        rec = load_reconstruction(chunk)
        rec.update_point_3d_errors()
        _REPROJECTION_CACHE.clear()  # one entry is enough: the open project
        _REPROJECTION_CACHE[key] = float(rec.compute_mean_reprojection_error())
    return _num(_REPROJECTION_CACHE[key], 4), "computed"


def alignment_report(chunk, arguments: dict, index: Index) -> dict:
    from ..markers import gcp

    if not chunk.cameras:
        return {"none": "il chunk non ha foto"}
    sfm = chunk.meta.get("sfm") or {}
    aligned = chunk.aligned_cameras
    if not aligned and not sfm:
        return {"none": "le foto del chunk non sono allineate"}
    tie = _existing(chunk, "tie_points")
    rows = gcp.marker_errors(chunk)
    bars = [r for r in gcp.scalebar_errors(chunk) if r["enabled"]]
    optimize = sfm.get("optimize") or {}
    err, err_source = _reprojection_error(chunk) if aligned else (None, None)
    out = {"photos": len(chunk.cameras), "aligned": len(aligned),
           "tie_points": _ply_counts(tie).get("vertex") if tie is not None else None,
           "reprojection_error_px": err, "reprojection_error_source": err_source,
           "matcher": sfm.get("matcher"), "mapper": sfm.get("mapper"), "features": sfm.get("features"),
           "quality": sfm.get("quality"), "pairs": sfm.get("pairs"),
           "gcp": {"control_rmse": _rmse([r["error"] for r in rows if r["type"] == "control"]),
                   "check_rmse": _rmse([r["error"] for r in rows if r["type"] == "check"]),
                   "n": sum(1 for r in rows if r["error"] is not None),
                   "control_points": sum(1 for r in rows if r["type"] == "control" and r["error"] is not None),
                   "check_points": sum(1 for r in rows if r["type"] == "check" and r["error"] is not None)},
           "scalebars": {"control_rmse": _rmse([r["error"] for r in bars if r["type"] == "control"]),
                         "check_rmse": _rmse([r["error"] for r in bars if r["type"] == "check"]),
                         "n": len(bars)},
           "optimized": bool(optimize),
           "crs": chunk.crs}
    if optimize:
        out["optimization"] = {k: optimize.get(k) for k in ("engine", "reverted", "converged", "iterations",
                                                             "control_points", "scale_bars", "gnss_positions")
                               if optimize.get(k) is not None}
    if sfm.get("global_fallback"):
        out["global_fallback"] = str(sfm["global_fallback"])
    return out


def markers(chunk, arguments: dict, index: Index) -> dict:
    from ..markers import gcp

    if not chunk.markers:
        return {"none": "il chunk non ha marker"}
    rows = []
    for m, e in zip(chunk.markers, gcp.marker_errors(chunk)):
        rows.append({"name": m.label, "projections": int(e["projections"]), "estimated": int(e["estimated"]),
                     "role": e["type"], "error_m": _sig(e["error"]), "pixel_error_px": _num(e["pixel_error"], 3),
                     "surveyed": bool(m.reference.location) and bool(m.reference.enabled),
                     "triangulated": m.position is not None, "code": m.code})
    return {"markers": rows, "n": len(rows)}


def thermal_info(chunk, arguments: dict, index: Index) -> dict:
    from ..engine import thermal

    meta = chunk.meta.get("thermal") or {}
    ms = chunk.meta.get("multispectral") or {}
    has_thermal = bool(meta) or "LWIR" in (ms.get("bands") or [])
    if not has_thermal:
        return {"none": "il chunk non ha riprese termiche"}
    cams = [c for c in chunk.cameras if "LWIR" in (c.meta.get("bands") or {})]
    out: dict[str, Any] = {"params": {k: _num(v, 3) for k, v in thermal.params_of(chunk).items()},
                           "thermal_only": thermal.thermal_only(chunk),
                           "frames": {"captures": len(cams), "decoded": int(meta.get("decoded") or 0)}}
    path = thermal.thermal_path(chunk)
    stats = meta.get("stats") or {}
    if path is None:
        out["orthomosaic"] = None
    elif stats:
        out["orthomosaic"] = {"resolution": _num(meta.get("resolution"), 6),
                              "temperatures_celsius": {"p2": _num(stats.get("p2"), 2), "p98": _num(stats.get("p98"), 2),
                                                       "median": _num(stats.get("median"), 2)}}
    else:
        info, t = _raster_sample(path)
        values = np.asarray(t.compressed(), np.float64)
        temps = ({"p2": _num(np.percentile(values, 2), 2), "p98": _num(np.percentile(values, 98), 2),
                  "median": _num(np.median(values), 2)} if values.size else None)
        out["orthomosaic"] = {"resolution": info["resolution"], "temperatures_celsius": temps}
    offsets = meta.get("offsets") or {}
    out["balance"] = ({"frames": sum(1 for k in offsets if k != "spread"),
                       "offset_spread_celsius": _num(offsets.get("spread"), 3)} if offsets else None)
    reg = meta.get("registration") or {}
    if isinstance(reg, dict) and reg:  # thermal.registrations: factor, cover, fixed (homography), fixed_cc, captures
        caps = reg.get("captures") or {}
        out["registration"] = {"cover": _num(reg.get("cover"), 3), "correlation": _num(reg.get("fixed_cc"), 3),
                               "captures": len(caps),
                               "refined": sum(1 for c in caps.values() if isinstance(c, dict) and c.get("refined"))}
    return out


def multispectral_info(chunk, arguments: dict, index: Index) -> dict:
    ms = chunk.meta.get("multispectral") or {}
    if not ms:
        return {"none": "il chunk non ha riprese multispettrali"}
    bands = list(ms.get("bands") or [])
    reflectance = ms.get("reflectance")
    out: dict[str, Any] = {"bands": bands, "captures": int(ms.get("captures") or 0), "reference": ms.get("reference"),
                           "reflectance": bool(reflectance and (chunk.data_dir / reflectance).exists()),
                           "radiometry": ms.get("radiometry"),
                           "indices": sorted((ms.get("indices") or {}).keys())}
    quality = ms.get("registration_quality") or {}
    per_band: dict[str, list] = {}
    for q in quality.values():
        for band, how in (q or {}).items():
            cc = how.get("correlation") if isinstance(how, dict) else None
            if cc is not None:
                per_band.setdefault(band, []).append(float(cc))
    out["registration_quality"] = ({b: {"median_correlation": _num(np.median(v), 3), "captures": len(v)}
                                    for b, v in sorted(per_band.items())} if per_band else None)
    balance = ms.get("balance") or {}
    spread = balance.get("spread") or {}
    out["balance_spread"] = {b: _num(s, 4) for b, s in spread.items()} if spread else None
    if balance.get("residual"):
        out["balance_residual"] = {b: _num(s, 4) for b, s in balance["residual"].items()}
    return out


def point_cloud_info(chunk, arguments: dict, index: Index) -> dict:
    from ..ai.classify import class_names
    from ..core.pointcloud import _read_binary_ply_vertices
    from ..engine.confidence import BINS, bin_label

    path = _existing(chunk, "point_cloud")
    if path is None:
        return {"none": "il chunk non ha una nuvola densa"}
    counts = _ply_counts(path)
    classes = chunk.point_classes()
    names = class_names(chunk)
    points = counts.get("vertex")
    if points is None:
        points = sum(classes.values())
    bins = None
    arr = _read_binary_ply_vertices(path, mmap=True)
    if arr is not None and "confidence" in (arr.dtype.names or ()):
        hist = np.bincount(np.asarray(arr["confidence"], np.uint8), minlength=256)
        bins = {bin_label(lo, hi): int(hist[lo:hi + 1].sum()) for lo, hi in BINS}
    return {"points": int(points), "classes": {str(c): int(n) for c, n in sorted(classes.items())},
            "class_names": {str(c): names.get(c, f"classe {c}") for c in sorted(classes)},
            "confidence_bins": bins, "version": _version_name(chunk, "point_cloud")}


def model_info(chunk, arguments: dict, index: Index) -> dict:
    from ..engine.texturing import texture_pages

    path = _existing(chunk, "model")
    if path is None:
        return {"none": "il chunk non ha un modello 3D"}
    counts = _ply_counts(path)
    if not counts:  # an OBJ or another format: read through the mesh reader
        from ..io.mesh_ply import read_mesh

        m = read_mesh(path)
        counts = {"vertex": len(m.vertices), "face": len(m.faces)}
    pages = [p for p in texture_pages(chunk) if Path(p).exists()]
    size = None
    if pages:
        from PIL import Image

        with Image.open(pages[0]) as im:
            size = [int(im.width), int(im.height)]
    return {"faces": counts.get("face"), "vertices": counts.get("vertex"), "texture_pages": len(pages),
            "texture_size": size, "textured": bool(pages), "version": _version_name(chunk, "model")}


def _passages(found) -> dict:
    return {"passages": [{"title": p.title, "page": p.page, "anchor": p.anchor, "text": p.text, "score": p.score}
                         for p in found]}


def _index_for(arguments: dict, index: Index | None) -> Index:
    """The index of the language asked (``lang``; unknown or missing: that of the index given, else Italian)."""
    lang = arguments.get("lang")
    if lang not in LANGUAGES:
        lang = index.lang if index is not None and index.lang in LANGUAGES else "it"
    if index is not None and index.lang == lang:
        return index
    return default_index(lang)


def guide_search(document, arguments: dict, index: Index) -> dict:
    query = str(arguments.get("query") or "").strip()
    if not query:
        return {"passages": []}
    k = int(arguments.get("k") or 5)
    return _passages(_index_for(arguments, index).search(query, k=max(1, min(20, k))))


_API_INDEXES: dict[tuple, tuple[Index, Index]] = {}  # (lang, guide folder, index) -> (the index, its sub-index)


def _api_index(full: Index) -> Index:
    """An index over the scripting and API pages of ``full`` only, built once per index (the key names the
    language, the guide folder and the index itself, which the value keeps alive so the key stays its own)."""
    key = (full.lang, str(guide_folder()), id(full))
    if key not in _API_INDEXES:
        wanted = {f"{p}.it.md" if full.lang == "it" else f"{p}.md" for p in API_PAGES}
        _API_INDEXES[key] = (full, Index([p for p in full.passages if p.page in wanted], full.lang))
    return _API_INDEXES[key][1]


def scripting_api(document, arguments: dict, index: Index) -> dict:
    topic = str(arguments.get("topic") or arguments.get("query") or "").strip()
    if not topic:
        return {"passages": []}
    k = int(arguments.get("k") or 5)
    return _passages(_api_index(_index_for(arguments, index)).search(topic, k=max(1, min(20, k))))


# tools that take the chunk (resolved before the call) and tools that take the document
_CHUNK_TOOLS: dict[str, Callable] = {
    "dem_statistics": dem_statistics, "orthomosaic_info": orthomosaic_info, "alignment_report": alignment_report,
    "markers": markers, "thermal_info": thermal_info, "multispectral_info": multispectral_info,
    "point_cloud_info": point_cloud_info, "model_info": model_info,
}
_DOCUMENT_TOOLS: dict[str, Callable] = {"project_summary": project_summary, "guide_search": guide_search,
                                        "scripting_api": scripting_api}


def run_tool(name: str, arguments: dict | None, document, index: Index | None) -> dict:
    """Run the tool ``name`` with its JSON ``arguments`` over ``document`` (None: no project open). Never raises."""
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return {"error": f"arguments must be a JSON object, not {type(arguments).__name__}"}
    try:
        arguments = dict(arguments)
        if name in _DOCUMENT_TOOLS:
            return _DOCUMENT_TOOLS[name](document, arguments, index)
        if name in _CHUNK_TOOLS:
            chunk, answer = _resolve_chunk(document, arguments)
            if answer is not None:
                return answer
            return _CHUNK_TOOLS[name](chunk, arguments, index)
        return {"error": f"unknown tool {name}"}
    except Exception as exc:  # a tool must never stop the conversation
        log.exception("the tool %s failed", name)
        return {"error": f"{type(exc).__name__}: {exc}"}
