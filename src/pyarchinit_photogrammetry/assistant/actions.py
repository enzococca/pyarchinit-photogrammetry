"""What the assistant does on the open project - also from a script, as ``ppg.actions``.

Every action is a function ``action(chunk, window, ..., progress=None) -> dict``: ``chunk`` is the chunk it works
on, ``window`` the program's window or None (headless: the command line, a test). From a script the window is
``window`` of the console (the proxy that calls the window in the interface thread), so an action reaches the window
only through what that proxy does: its methods, called and waited for in the interface thread. What an action changes
on a chunk the window shows goes through the window (the stones, the units of SAM, the photos), so that the window
keeps it, draws it and saves it; on another chunk, or without a window, it goes into the chunk as the window would
have saved it. Each action returns the facts of what it did - counts, names of versions, file names - never a path.
A target that is not there or not right (a DEM for the stones, an unknown product or parameter) is a ``ValueError``
with a plain message in Italian: the user reads it and the model gets it as the tool's answer.

:data:`ACTIONS` lists them for the assistant's tools: ``name -> (function, quick, description_it, description_en,
schema)``; ``quick`` = the action runs at once under *Subito le rapide* (:func:`is_quick` also looks at the
parameters: selecting photos is quick only to filter or to select them); ``schema`` is a JSON schema of the parameters
(the window and the chunk aside) with ``description_it`` / ``description_en`` for each of them.
"""
from __future__ import annotations

import contextlib
import fnmatch
import re
from pathlib import Path

from ..core.progress import as_progress

STONES_LAYER = "Pietre"  # as gui.main_window.STONES_LAYER
SAM_LAYER = "Unita' da SAM"  # the layer of the units the SAM tools add
LABELS_SHOWN = 20  # photo labels given back by select_photos

# ------------------------------------------------------------------ parameters of the builds
# product -> {keyword: (JSON type, description it, description en, allowed values or None)}
BUILD_PARAMS: dict[str, dict[str, tuple]] = {
    "dem": {
        "resolution": ("number", "Lato della cella in metri (vuoto: dalla densità dei punti)",
                       "Cell size in metres (empty: from the point density)", None),
        "source": ("string", "Punti da cui si costruisce", "Points it is built from",
                   ["point_cloud", "tie_points", "model"]),
        "interpolate": ("boolean", "Riempire i piccoli buchi", "Fill the small holes", None),
        "classes": ("array", "Solo i punti di queste classi (es. [2] = terreno)",
                    "Only the points of these classes (e.g. [2] = ground)", None)},
    "orthomosaic": {
        "resolution": ("number", "Pixel in metri (vuoto: dalle foto)", "Pixel in metres (empty: from the photos)",
                       None),
        "surface": ("string", "Superficie su cui si proietta", "Surface the photos are projected on", ["dem", "model"]),
        "blending": ("string", "Fusione delle foto", "Blending of the photos", ["mosaic", "average", "disabled"]),
        "hide_classes": ("array", "Classi della nuvola lasciate trasparenti", "Classes of the cloud left transparent",
                         None),
        "max_angle": ("number", "Angolo massimo dalla verticale (gradi)", "Largest angle from the vertical (degrees)",
                      None),
        "colour_correction": ("boolean", "Correzione del colore", "Colour correction", None),
        "ghosting": ("boolean", "Togliere persone e veicoli visti da poche foto",
                     "Leave out people and vehicles seen by a few photos", None),
        "fill_holes": ("boolean", "Riempire i piccoli buchi", "Fill the small holes", None),
        "skip_unreadable": ("boolean", "Saltare le foto illeggibili", "Skip the photos that cannot be read", None)},
    "dense": {
        "quality": ("string", "Qualità delle mappe di profondità", "Quality of the depth maps",
                    ["lowest", "low", "medium", "high", "ultra"]),
        "filter_mode": ("string", "Filtro delle mappe", "Filtering of the maps",
                        ["disabled", "mild", "moderate", "aggressive"]),
        "point_colors": ("boolean", "Colori dei punti", "Colours of the points", None),
        "skip_unreadable": ("boolean", "Saltare le foto illeggibili", "Skip the photos that cannot be read", None)},
    "model": {
        "source": ("string", "Da cosa si costruisce", "What it is built from",
                   ["point_cloud", "depth_maps", "tie_points"]),
        "surface": ("string", "Tipo di superficie", "Kind of surface", ["arbitrary", "height_field"]),
        "face_count": (["string", "integer"], "Numero di facce (low, medium, high o un numero)",
                       "Number of faces (low, medium, high or a number)", None),
        "classes": ("array", "Solo i punti di queste classi", "Only the points of these classes", None)},
    "texture": {
        "texture_size": ("integer", "Lato della texture in pixel", "Side of the texture in pixels", None),
        "mapping": ("string", "Mappatura", "Mapping", ["auto", "orthophoto", "generic"]),
        "colour_correction": ("boolean", "Correzione del colore", "Colour correction", None),
        "skip_unreadable": ("boolean", "Saltare le foto illeggibili", "Skip the photos that cannot be read", None)},
    "relief": {
        "method": ("string", "Visualizzazione del rilievo", "Relief visualisation",
                   ["svf", "openness_pos", "openness_neg", "slrm", "msrm", "multi_hillshade"]),
        "radius": ("number", "Raggio di ricerca in metri", "Search radius in metres", None),
        "directions": ("integer", "Direzioni", "Directions", None),
        "min_size": ("number", "Dimensione minima (msrm, metri)", "Smallest size (msrm, metres)", None),
        "max_size": ("number", "Dimensione massima (msrm, metri)", "Largest size (msrm, metres)", None)},
    "normal_map": {
        "source": ("string", "Da cosa si calcola", "What it is computed from", ["auto", "model", "dem"])},
    "tin": {
        "source": ("string", "Punti del TIN", "Points of the TIN", ["dem", "cloud", "spots"]),
        "max_error": ("number", "Tolleranza dal DEM in metri", "Tolerance from the DEM in metres", None),
        "lines": ("boolean", "Con le linee di rottura disegnate", "With the break lines drawn", None),
        "spots": ("boolean", "Quote puntuali come vertici", "Spot heights as vertices", None),
        "classes": ("array", "Classi del terreno (dalla nuvola)", "Ground classes (from the cloud)", None)},
    "contours": {
        "interval": ("number", "Equidistanza in metri (obbligatoria)", "Contour interval in metres (required)", None),
        "index_every": ("integer", "Curva direttrice ogni", "Index contour every", None),
        "smooth": ("number", "Levigatura del DEM in metri", "Smoothing of the DEM in metres", None)},
}
PRODUCTS = tuple(BUILD_PARAMS)
# the products whose version before is kept (core.versions), by the name of the build
VERSIONED = {"dem": "dem", "orthomosaic": "orthomosaic", "dense": "point_cloud", "model": "model", "texture": "model"}

# what -> formats (the extension of the file, "cog" = a Cloud Optimized GeoTIFF)
EXPORTS = {"orthomosaic": ("tif", "cog"), "dem": ("tif", "cog"), "model": ("obj", "ply", "glb"),
           "point_cloud": ("las", "laz", "ply"), "units": ("geojson", "dxf"), "stones": ("dxf", "svg", "geojson"),
           "report": ("html", "pdf"), "ifc": ("ifc",), "tables": ("xlsx",)}

BY = ("area", "markers", "names", "all", "none")
THEN = ("filter", "select", "enable", "disable")
SHOW = ("ortho", "orthomosaic", "dem", "model", "cloud", "point_cloud", "elevation")


# ------------------------------------------------------------------ shared helpers
def _document(chunk, window):
    doc = getattr(window, "doc", None) if window is not None else None
    return doc if doc is not None else getattr(chunk, "document", None)


def _chunk(chunk, window, chunk_label: str | None = None):
    """The chunk to work on: ``chunk_label``'s when given, otherwise ``chunk``."""
    if chunk_label:
        doc = _document(chunk, window)
        if doc is None:
            raise ValueError(f"nessun progetto in cui cercare il chunk «{chunk_label}»")
        try:
            return doc.chunk_by_label(chunk_label)
        except KeyError:
            labels = ", ".join(f"«{c.label}»" for c in doc.chunks) or "nessuno"
            raise ValueError(f"nessun chunk «{chunk_label}» nel progetto: i chunk sono {labels}") from None
    if chunk is None:
        raise ValueError("nessun chunk attivo: crea o apri un chunk")
    return chunk


def _active(chunk, window) -> bool:
    """Whether the window shows ``chunk`` (it is the active one)."""
    return window is not None and getattr(window, "chunk", None) is chunk


def _need_window(window, doing: str) -> None:
    if window is None:
        raise ValueError(f"{doing} serve la finestra del programma (qui non c'è: riga di comando o script senza "
                         f"finestra)")


def _activate(chunk, window) -> None:
    if not _active(chunk, window):
        window._activate_chunk(chunk)


def _shown(window) -> dict | None:
    """What the 2D view of the window shows (:meth:`MainWindow.shown_raster`), None without a window."""
    if window is None:
        return None
    fn = getattr(window, "shown_raster", None)
    if fn is None:
        return None
    try:
        return fn()
    except Exception:  # noqa: BLE001 - a window without a 2D view: nothing shown
        return None


def _box(bounds, window):
    """``bounds`` as ``(xmin, ymin, xmax, ymax)``: a box, a shapely geometry, "selection" (the polygon selected in the
    2D view) or None."""
    if bounds is None:
        return None
    if isinstance(bounds, str):
        if bounds.strip().lower() not in ("selection", "selezione"):
            raise ValueError(f"area «{bounds}»: un riquadro (xmin, ymin, xmax, ymax) o \"selection\"")
        polygon = window.selected_polygon() if window is not None else None
        if polygon is None or polygon.is_empty:
            raise ValueError("nessun poligono selezionato: selezionane uno nella vista 2D (strumento Seleziona)")
        return tuple(float(v) for v in polygon.bounds)
    if hasattr(bounds, "bounds") and not isinstance(bounds, (list, tuple)):
        return tuple(float(v) for v in bounds.bounds)
    try:
        x0, y0, x1, y1 = (float(v) for v in bounds)
    except (TypeError, ValueError):
        raise ValueError("l'area è un riquadro (xmin, ymin, xmax, ymax)") from None
    if not (x1 > x0 and y1 > y0):
        raise ValueError("riquadro vuoto: serve xmin < xmax e ymin < ymax")
    return x0, y0, x1, y1


def _elevation(chunk, ref):
    """The entry of the elevation named ``ref`` (its name or its id), None when there is none."""
    ref = str(ref)
    for e in chunk.meta.get("elevations") or []:
        if isinstance(e, dict) and (str(e.get("name")) == ref or str(e.get("id")) == ref):
            return e
    return None


def _calc_names(chunk) -> list:
    from ..engine import rastercalc

    try:
        return rastercalc.rasters(chunk)
    except Exception:  # noqa: BLE001 - a broken raster never stops the action
        return []


def _raster_name(chunk, path: Path) -> str:
    """The name of the raster calculator of ``path``; else its file name without extension."""
    try:
        wanted = Path(path).resolve()
    except OSError:
        return Path(path).stem
    for r in _calc_names(chunk):
        try:
            if Path(r.path).resolve() == wanted:
                return r.name
        except OSError:
            continue
    return Path(path).stem


def _bands(path: Path) -> int:
    import rasterio

    with rasterio.open(path) as src:
        return int(src.count)


def _image(chunk, window, raster: str | None) -> dict:
    """The image to work on: ``path``, ``frame`` (the id of an elevation, None on the plan), ``name``, ``kind``.
    ``raster`` a name of the raster calculator, an elevation (name or id) or None: the raster the window shows
    for ``chunk``, else the orthomosaic."""
    from ..engine import elevation as E

    if raster is None:
        shown = _shown(window) if _active(chunk, window) else None
        if shown and shown.get("path") is not None:
            frame = shown.get("frame")
            if frame is not None:
                entry = _elevation(chunk, frame)
                return {"path": Path(shown["path"]), "frame": frame, "kind": shown.get("kind"),
                        "name": str(entry.get("name") or frame) if entry else str(frame)}
            return {"path": Path(shown["path"]), "frame": None, "kind": shown.get("kind"),
                    "name": _raster_name(chunk, Path(shown["path"]))}
        path = chunk.product_path("orthomosaic")
        if path is None or not Path(path).exists():
            raise ValueError(f"il chunk «{chunk.label}» non ha un'ortofoto: costruiscila o indica un raster")
        return {"path": Path(path), "frame": None, "kind": "ortho", "name": _raster_name(chunk, Path(path))}
    entry = _elevation(chunk, raster)
    if entry is not None:
        ortho, _relief = E.elevation_paths(chunk, entry)
        if not ortho.exists():
            raise ValueError(f"il prospetto «{entry.get('name')}» non ha più i suoi file")
        return {"path": ortho, "frame": entry["id"], "kind": "ortho", "name": str(entry.get("name") or entry["id"])}
    name = {"ortho": "orthomosaic", "ortofoto": "orthomosaic"}.get(str(raster).strip().lower(), str(raster).strip())
    found = _calc_names(chunk)
    r = next((r for r in found if r.name == name), None) or next((r for r in found if r.label == name), None)
    if r is None:
        names = [r.name for r in found] + [str(e.get("name")) for e in chunk.meta.get("elevations") or []
                                          if isinstance(e, dict)]
        listed = ", ".join(names[:30]) + (", ..." if len(names) > 30 else "") if names else "nessuno"
        raise ValueError(f"nessun raster «{raster}» nel chunk «{chunk.label}»: ci sono {listed}")
    kind = "dem" if r.name == "dem" or (r.source == "version" and r.name.startswith("dem_")) else None
    return {"path": Path(r.path), "frame": None, "kind": kind, "name": r.name}


def _image_with_colours(chunk, window, raster, what: str) -> dict:
    """:func:`_image`, refused when it is a DEM or has fewer than three bands."""
    target = _image(chunk, window, raster)
    if target["kind"] == "dem" or _bands(target["path"]) < 3:
        raise ValueError(f"«{target['name']}» è un DEM o un raster a una banda: {what} lavora su un'ortofoto o un "
                         f"prospetto")
    return target


@contextlib.contextmanager
def _segmenter(chunk, window):
    """SAM 2.1: the window's (``window.sam``; made in this thread and given to the window when it has none yet) or,
    without a window, one of its own, closed afterwards."""
    from ..ai import sam_env

    own = None
    try:
        seg = getattr(window, "sam", None) if window is not None else None
        if seg is None and window is not None and hasattr(window, "sam"):
            # made here, in the calling thread (a script's worker): starting the SAM worker and waiting for its model
            # in the interface thread would freeze the window; handed to the window, which uses it from now on
            settings = getattr(window, "settings", None)
            model = str(settings.value("sam/model", "small") or "small") if settings is not None else "small"
            seg = sam_env.make_segmenter(model)
            window.sam = seg
        elif seg is None:
            seg = own = sam_env.make_segmenter()
    except ImportError:
        raise ValueError("SAM 2.1 non è installato: installalo da Strumenti > SAM (Segment Anything) prima") from None
    try:
        yield seg
    finally:
        close = getattr(own, "close", None)
        if close is not None:
            close()


def _thoroughness(value: str) -> str:
    from ..ai.sam import THOROUGHNESS

    if value not in THOROUGHNESS:
        raise ValueError(f"thoroughness «{value}»: {', '.join(THOROUGHNESS)}")
    return value


# ------------------------------------------------------------------ show
def show(chunk, window, what: str, chunk_label: str | None = None, version: str | None = None,
         elevation: str | None = None) -> dict:
    """``what`` in the view of the window: "ortho" (or "orthomosaic"), "dem", "model", "cloud", "elevation" (the one
    named ``elevation``) or a name of the raster calculator; ``version``: a kept version (its name or id) of the
    orthomosaic, the DEM, the model or the cloud; ``chunk_label``: that chunk made the active one first."""
    _need_window(window, "per mostrare qualcosa")
    chunk = _chunk(chunk, window, chunk_label)
    what = str(what or "").strip()
    key = {"ortho": "orthomosaic", "ortofoto": "orthomosaic", "cloud": "point_cloud", "nuvola": "point_cloud",
           "modello": "model", "prospetto": "elevation"}.get(what.lower(), what)
    if key == "elevation" or (elevation is not None and key in ("", "elevation")):
        entry = _elevation(chunk, elevation) if elevation is not None else None
        if entry is None:
            names = [str(e.get("name")) for e in chunk.meta.get("elevations") or [] if isinstance(e, dict)]
            raise ValueError(f"nessun prospetto «{elevation}» nel chunk «{chunk.label}»: "
                             f"{', '.join(names) if names else 'il chunk non ha prospetti'}")
        _activate(chunk, window)
        window.show_elevation(entry)
        return {"shown": "elevation", "elevation": str(entry.get("name") or entry["id"]), "chunk": chunk.label}
    if key in ("orthomosaic", "dem", "model", "point_cloud"):
        if chunk.product_path(key) is None and version is None:
            raise ValueError(f"il chunk «{chunk.label}» non ha {_PRODUCT_NAMES[key]}")
        _activate(chunk, window)
        if version is not None:
            vid, name = _version(chunk, key, version)
            window.show_version(key, vid)
            return {"shown": key, "version": name, "chunk": chunk.label}
        if key in ("orthomosaic", "dem"):
            window._show_raster("ortho" if key == "orthomosaic" else "dem")
        else:
            window._show_model(key)
        return {"shown": key, "version": _active_name(chunk, key), "chunk": chunk.label}
    entry = _elevation(chunk, what) if what else None
    if entry is not None:  # an elevation named as "what"
        return show(chunk, window, "elevation", elevation=str(entry["id"]))
    target = _image(chunk, None, what)  # a raster of the calculator
    _activate(chunk, window)
    _show_raster_of(chunk, window, target)
    return {"shown": target["name"], "chunk": chunk.label}


_PRODUCT_NAMES = {"orthomosaic": "un'ortofoto", "dem": "un DEM", "model": "un modello",
                  "point_cloud": "una nuvola densa"}


def _version(chunk, product: str, ref: str) -> tuple[str, str]:
    from ..core import versions

    entries = versions.versions(chunk, product)
    e = next((e for e in entries if str(e.get("name")) == str(ref) or str(e.get("id")) == str(ref)), None)
    if e is None:
        names = ", ".join(f"«{e.get('name') or e.get('id')}»" for e in entries) or "nessuna"
        raise ValueError(f"nessuna versione «{ref}» di {product} nel chunk «{chunk.label}»: "
                         f"le versioni sono {names}")
    return e["id"], str(e.get("name") or e["id"])


def _active_name(chunk, product: str) -> str | None:
    from ..core import versions

    try:
        return versions.active_info(chunk, product).get("name")
    except Exception:  # noqa: BLE001
        return None


def _show_raster_of(chunk, window, target: dict) -> None:
    """A raster of the calculator in the 2D view, through the window's own way of showing it when it has one."""
    from ..core import versions
    from ..engine import rastercalc

    path = Path(target["path"]).resolve()

    def same(p) -> bool:
        try:
            return p is not None and Path(p).resolve() == path
        except OSError:
            return False

    for product in ("orthomosaic", "dem"):
        if same(chunk.product_path(product)):
            window._show_raster("ortho" if product == "orthomosaic" else "dem")
            return
        for e in versions.versions(chunk, product):
            if same(chunk.data_dir / (e.get("files") or {}).get(product, "")):
                window.show_version(product, e["id"])
                return
    for name in chunk.meta.get(rastercalc.FOLDER) or {}:
        if same(rastercalc.result_path(chunk, name)):
            window.show_calc(name)
            return
    for kind in ("svf", "openness_pos", "openness_neg", "slrm", "msrm", "multi_hillshade"):
        if same(chunk.product_path(kind)):
            window.show_relief(kind)
            return
    window.show_raster_path(str(path), "ortho" if _bands(path) >= 3 else "dem")


# ------------------------------------------------------------------ photos
def select_photos(chunk, window, by: str, then: str = "filter", area=None, markers=None, pattern: str | None = None,
                  enabled: bool | None = None) -> dict:
    """Photos of ``chunk`` chosen ``by`` "area" (those that frame ``area`` - a box or a polygon - or the polygon
    selected in the 2D view), "markers" (where the markers ``markers`` are: labels, keys or codes), "names" (the
    labels matching the glob ``pattern``, any case), "all" or "none"; ``enabled`` True / False keeps only the photos
    switched on / off. ``then``: "filter" (the Photos pane shows only them), "select" (selected in the pane),
    "enable" or "disable" (switched on or off for the next alignment and builds). Without the window only
    "enable" and "disable"."""
    if by not in BY:
        raise ValueError(f"by «{by}»: {', '.join(BY)}")
    if then not in THEN:
        raise ValueError(f"then «{then}»: {', '.join(THEN)}")
    chunk = _chunk(chunk, window)
    if then in ("filter", "select"):
        _need_window(window, "per filtrare o selezionare le foto")
    cams = _photos_by(chunk, window, by, area, markers, pattern)
    if enabled is not None:
        cams = [c for c in cams if bool(c.enabled) == bool(enabled)]
    keys = {c.key for c in cams}
    out = {"by": by, "then": then, "photos": len(cams), "total": len(chunk.cameras),
           "labels": [c.label for c in cams[:LABELS_SHOWN]], "chunk": chunk.label}
    if then == "filter":
        _activate(chunk, window)
        from ..gui.i18n import tr

        title = {"area": tr("Foto dell'area"), "markers": tr("Foto dei marker"), "names": tr("Foto «{0}»", pattern),
                 "all": "", "none": tr("Nessuna foto")}[by]
        window.photos.set_filter(None if by == "all" and enabled is None else keys, title)
        window._show_pane(window.bottom_tabs, window.photos_panel)
    elif then == "select":
        _activate(chunk, window)
        window.select_photo_keys(keys)
    else:
        flag = then == "enable"
        out["changed"] = sum(1 for c in cams if bool(c.enabled) != flag)
        if _active(chunk, window):
            window.enable_photos(cams, flag)
        elif cams:
            chunk.set_cameras_enabled(cams, flag)
    return out


def _photos_by(chunk, window, by: str, area, markers, pattern) -> list:
    cams = list(chunk.cameras)
    if by == "all":
        return cams
    if by == "none":
        return []
    if by == "names":
        if not pattern:
            raise ValueError("per scegliere le foto per nome serve pattern (es. \"DJI_01*\")")
        pat = str(pattern).lower()
        return [c for c in cams if fnmatch.fnmatchcase(c.label.lower(), pat)]
    if by == "markers":
        return _photos_of_markers(chunk, markers)
    return _photos_of_area(chunk, window, area)


def _photos_of_markers(chunk, markers) -> list:
    from ..markers import gcp

    if not markers:
        raise ValueError("per scegliere le foto per marker servono i marker (markers: nomi, chiavi o codici)")
    refs = [markers] if isinstance(markers, (str, int)) else list(markers)
    found = []
    for ref in refs:
        try:
            found.append(gcp.resolve_marker(chunk, ref))
        except KeyError:
            names = ", ".join(m.label for m in chunk.markers) or "nessuno"
            raise ValueError(f"nessun marker «{ref}» nel chunk «{chunk.label}»: i marker sono {names}") from None
    keys = set()
    for m in found:
        keys |= set(m.projections)
        if m.position is not None:
            keys |= {c.key for c, *_ in gcp.photos_seeing(chunk, m.position)}
    return [c for c in chunk.cameras if c.key in keys]


def _photos_of_area(chunk, window, area) -> list:
    from shapely.geometry import box

    from ..engine.patches import photos_for_area

    frame = None
    if area is None or (isinstance(area, str) and area.strip().lower() in ("selection", "selezione")):
        if window is None or not _active(chunk, window):
            raise ValueError("per scegliere le foto dell'area serve l'area (un riquadro) o il poligono selezionato "
                             "nella finestra")
        frame = (_shown(window) or {}).get("frame")  # an elevation shown: the selection is in its (u, v)
        geom = window.selected_polygon()
        if geom is None or geom.is_empty:
            raise ValueError("nessun poligono selezionato: selezionane uno nella vista 2D (strumento Seleziona)")
    elif hasattr(area, "geom_type"):
        geom = area
    else:
        geom = box(*_box(area, window))
    rows = photos_for_area(chunk, geom, limit=100_000, frame=frame)
    keys = {r["key"] for r in rows if r.get("coverage", 0) > 0}
    return [c for c in chunk.cameras if c.key in keys]


# ------------------------------------------------------------------ stones
def stones(chunk, window, raster: str | None = None, bounds=None, min_size: float = 0.05, max_size: float = 1.0,
           thoroughness: str = "alta", progress=None) -> dict:
    """The stones SAM 2.1 finds on ``raster`` (a name of the raster calculator or an elevation; None: the raster
    the window shows, else the orthomosaic) inside ``bounds`` (a box in its coordinates, "selection" or None: all
    of it), between ``min_size`` and ``max_size`` metres across; kept as the tool *Pietre (SAM)* keeps them (layer
    «Pietre» of that image: the stones there before inside the box are found again, those outside kept)."""
    from ..archaeo.stones import find_stones

    _thoroughness(thoroughness)
    if not (0 < float(min_size) < float(max_size)):
        raise ValueError("serve 0 < min_size < max_size (metri)")
    chunk = _chunk(chunk, window)
    target = _image_with_colours(chunk, window, raster, "lo strumento delle pietre")
    box_ = _box(bounds, window)
    progress = as_progress(progress)
    with _segmenter(chunk, window) as seg:
        found = find_stones(target["path"], seg, bounds=box_, min_size=float(min_size), max_size=float(max_size),
                            thoroughness=thoroughness, progress=progress)
    key = target["frame"] if target["frame"] is not None else target["path"].name
    if _active(chunk, window):
        total = window.set_stones(key, found, box_)
    else:
        total = _store_stones(chunk, key, target["frame"], found, box_)
    return {"stones": len(found), "total": total, "layer": STONES_LAYER, "raster": target["name"],
            "chunk": chunk.label}


def _store_stones(chunk, key: str, frame: str | None, found: list, bounds) -> int:
    """The stones into ``chunk`` as the window saves them: ``meta["stones"][file name]`` on the plan, the frame's
    ``stones[id]`` for an elevation."""
    from shapely import wkt
    from shapely.geometry import box

    if frame is None:
        rows = chunk.meta.setdefault("stones", {})
    else:
        frames = chunk.meta.setdefault("drawings", {}).setdefault("frames", {})
        rows = frames.setdefault(frame, {"units": [], "quote": []}).setdefault("stones", {})
    area = box(*bounds) if bounds is not None else None
    kept = [r for r in rows.get(key, []) if area is not None and not wkt.loads(r).intersects(area)]
    rows[key] = kept + [g.wkt for g in found]
    total = len(rows[key])
    if not rows[key]:
        rows.pop(key)
    if frame is None and not rows:
        chunk.meta.pop("stones", None)
    return total


# ------------------------------------------------------------------ SAM units
def segment(chunk, window, bounds=None, prompt: str | None = None, thoroughness: str = "normale",
            progress=None) -> dict:
    """The units SAM 2.1 finds (automatic mode, in tiles) on the raster the window shows - the orthomosaic without
    a window - inside ``bounds`` (a box, "selection"; None: the polygon selected in the window, else all of it),
    added as candidates without a US number to the layer «Unita' da SAM». ``prompt``: their kind (US, USM... as the
    US forms have it) or a description kept as ``tipo_us_s``."""
    from ..ai.sam import auto_units
    from ..archaeo.stratigraphy import US_TYPES

    _thoroughness(thoroughness)
    chunk = _chunk(chunk, window)
    target = _image_with_colours(chunk, window, None, "la segmentazione")
    if bounds is None and _active(chunk, window):
        polygon = window.selected_polygon()
        box_ = tuple(float(v) for v in polygon.bounds) if polygon is not None and not polygon.is_empty else None
    else:
        box_ = _box(bounds, window)
    progress = as_progress(progress)
    with _segmenter(chunk, window) as seg:
        try:
            units = auto_units(chunk if target["frame"] is None else None, seg, bounds=box_, thoroughness=thoroughness,
                               path=target["path"], progress=progress)
        except (RuntimeError, ValueError) as exc:
            raise ValueError(f"segmentazione non riuscita: {exc}") from None
    kind = next((t for t in US_TYPES if prompt and t.lower() == str(prompt).strip().lower()), None)
    for u in units:
        if kind:
            u.tipo = kind
        elif prompt:
            u.attributes = {**(u.attributes or {}), "tipo_us_s": str(prompt)}
    if _active(chunk, window):
        n = window.add_sam_units(units)
    else:
        n = _store_units(chunk, target["frame"], units)
    return {"units": n, "layer": SAM_LAYER, "raster": target["name"], "chunk": chunk.label}


def _store_units(chunk, frame: str | None, units: list) -> int:
    """The units into ``chunk`` as the window saves them (``meta["drawings"]``, the plan or the frame)."""
    doc = getattr(chunk, "document", None)
    site = ((getattr(doc, "meta", None) or {}).get("sito") or None) if doc is not None else None
    drawings = chunk.meta.setdefault("drawings", {})
    if frame is None:
        rows = drawings.setdefault("units", [])
        drawings.setdefault("quote", [])
    else:
        fr = drawings.setdefault("frames", {}).setdefault(frame, {"units": [], "quote": []})
        rows = fr.setdefault("units", [])
    for u in units:
        u.sito = site
        u.attributes = {**(u.attributes or {}), "stratigraph_index_us": 1}
        rows.append({**u.to_dict(), "layer": SAM_LAYER})
    return len(units)


# ------------------------------------------------------------------ builds
def build(chunk, window, product: str, chunk_label: str | None = None, progress=None, **params) -> dict:
    """``product`` of the chunk built again with ``params`` (the keywords of :data:`BUILD_PARAMS`): "dem",
    "orthomosaic", "dense" (depth maps and dense cloud), "model", "texture", "relief", "normal_map", "tin",
    "contours" (lines on the DEM: ``interval`` required, drawn on the window, nothing kept in the project). The
    product as it was is kept as a version first (the DEM, the orthomosaic, the cloud, the model)."""
    from ..core import versions

    if product not in BUILD_PARAMS:
        raise ValueError(f"prodotto «{product}»: {', '.join(PRODUCTS)}")
    known = BUILD_PARAMS[product]
    unknown = sorted(set(params) - set(known))
    if unknown:
        raise ValueError(f"parametri sconosciuti per «{product}»: {', '.join(unknown)}; quelli validi sono "
                         f"{', '.join(known)}")
    for k, v in params.items():
        if v is None:
            continue
        kinds = known[k][0] if isinstance(known[k][0], (list, tuple)) else [known[k][0]]
        if not any(_of_type(v, t) for t in kinds):
            raise ValueError(f"{k} per «{product}»: serve {' o '.join(_TYPE_NAMES[t] for t in kinds)}, non {v!r}")
        allowed = known[k][3]
        if allowed is not None and v not in allowed:
            raise ValueError(f"{k} «{v}» per «{product}»: {', '.join(map(str, allowed))}")
    chunk = _chunk(chunk, window, chunk_label)
    params = {k: v for k, v in params.items() if v is not None}
    if product == "dense":  # the GPU the window is set to use (Allinea foto > GPU), else the program's default
        settings = getattr(window, "settings", None) if window is not None else None
        params["gpu"] = str(settings.value("sfm/gpu", "auto") or "auto") if settings is not None else None
    progress = as_progress(progress)
    if product == "contours":
        return _contours(chunk, window, progress, **params)
    keeping = contextlib.nullcontext() if chunk.keep_versions else versions.keep_during(chunk)
    try:
        with keeping:
            facts = _BUILDERS[product](chunk, progress, params)
    except RuntimeError as exc:  # "build the DEM first"...
        raise ValueError(f"«{product}» non costruito: {exc}") from None
    out = {"product": product, "chunk": chunk.label, **facts}
    kept_as = VERSIONED.get(product)
    out["version_kept"] = None
    if kept_as is not None:
        vid = (chunk._kept_during or {}).get(kept_as)
        entry = next((e for e in versions.versions(chunk, kept_as) if e["id"] == vid), None) if vid else None
        out["version_kept"] = str(entry.get("name") or vid) if entry else None
    return out


_TYPE_NAMES = {"number": "un numero", "integer": "un intero", "boolean": "true o false", "string": "un testo",
               "array": "una lista"}


_PYTHON_TYPES = {"number": (int, float), "integer": int, "string": str, "array": (list, tuple)}


def _of_type(value, kind: str) -> bool:
    """Whether ``value`` is of the JSON type ``kind``."""
    if kind == "boolean":
        return isinstance(value, bool)
    return not isinstance(value, bool) and isinstance(value, _PYTHON_TYPES[kind])


def _raster_facts(path) -> dict:
    import rasterio

    if path is None or not Path(path).exists():
        return {}
    with rasterio.open(path) as src:
        return {"resolution": round(float(abs(src.res[0])), 6), "width": int(src.width), "height": int(src.height),
                "bands": int(src.count)}


def _ply_facts(path) -> dict:
    """The counts of the elements of a PLY from its header."""
    if path is None or not Path(path).exists():
        return {}
    out = {}
    with open(path, "rb") as f:
        for _ in range(200):
            line = f.readline().decode("ascii", "replace").strip()
            if line.startswith("element "):
                _e, name, count = line.split()[:3]
                out[{"vertex": "vertices", "face": "faces"}.get(name, name)] = int(count)
            if line == "end_header" or not line:
                break
    return out


def _build_dem(chunk, progress, params):
    return _raster_facts(chunk.build_dem(progress=progress, **params))


def _build_orthomosaic(chunk, progress, params):
    return _raster_facts(chunk.build_orthomosaic(progress=progress, **params))


def _build_dense(chunk, progress, params):
    depth = {k: params[k] for k in ("quality", "filter_mode", "skip_unreadable") if k in params}
    cloud = {k: params[k] for k in ("point_colors",) if k in params}
    gpu = params.get("gpu")
    chunk.build_depth_maps(progress=progress.sub(0.0, 0.7), gpu=gpu, **depth)
    chunk.build_point_cloud(progress=progress.sub(0.7, 0.3), gpu=gpu, **cloud)
    return {"points": _ply_facts(chunk.product_path("point_cloud")).get("vertices")}


def _build_model(chunk, progress, params):
    chunk.build_model(progress=progress, **params)
    return _ply_facts(chunk.product_path("model"))


def _build_texture(chunk, progress, params):
    from ..engine import texturing

    image = chunk.build_texture(progress=progress, **params)
    pages = len(texturing.texture_pages(chunk)) if hasattr(texturing, "texture_pages") else 1
    return {"texture": Path(image).name, "pages": pages}


def _build_relief(chunk, progress, params):
    params = {"method": "svf", **params}
    return {"method": params["method"], **_raster_facts(chunk.build_relief(progress=progress, **params))}


def _build_normal_map(chunk, progress, params):
    return _raster_facts(chunk.build_normal_map(progress=progress, **params))


def _build_tin(chunk, progress, params):
    from ..archaeo import breaklines
    from ..engine import tin as T

    lines = params.pop("lines", True)
    tin = T.build_tin(chunk, breaklines=breaklines.records(chunk) if lines else None, progress=progress, **params)
    return {"vertices": int(len(tin.xy)), "triangles": int(len(tin.triangles)), "source": tin.source}


_BUILDERS = {"dem": _build_dem, "orthomosaic": _build_orthomosaic, "dense": _build_dense, "model": _build_model,
             "texture": _build_texture, "relief": _build_relief, "normal_map": _build_normal_map, "tin": _build_tin}


def _contours(chunk, window, progress, interval=None, index_every: int = 5, smooth: float = 0.0) -> dict:
    from ..archaeo import terrain

    if not interval or float(interval) <= 0:
        raise ValueError("per le curve di livello serve interval (equidistanza in metri, > 0)")
    dem = chunk.dem_path
    if dem is None or not Path(dem).exists():
        raise ValueError(f"il chunk «{chunk.label}» non ha un DEM: costruiscilo prima")
    lo, hi = terrain.dem_range(dem)
    if (hi - lo) / float(interval) > 2000:
        raise ValueError(f"con equidistanza {interval} m il DEM (dislivello {hi - lo:.1f} m) darebbe troppi livelli: "
                         f"usane una più grande")
    progress(0.1, "Curve di livello")
    lines = terrain.contours(dem, float(interval), index_every=int(index_every), smooth=float(smooth))
    name = f"Curve di livello ({float(interval):g} m)"
    if _active(chunk, window):
        window._show_raster("dem")
        window._draw_contours(name, lines)
    progress(1.0, f"{len(lines)} curve")
    return {"product": "contours", "chunk": chunk.label, "layer": name, "lines": len(lines),
            "interval": float(interval), "version_kept": None}


# ------------------------------------------------------------------ exports
def export(chunk, window, what: str, format: str, path=None, progress=None) -> dict:  # noqa: A002 - the tool's word
    """``what`` of the chunk (:data:`EXPORTS`: "orthomosaic", "dem", "model", "point_cloud", "units", "stones",
    "report", "ifc", "tables") written as ``format`` in ``path``; without ``path`` the window asks where (a save
    dialog), and headless ``path`` is required. Returns the file name and its size."""
    if what not in EXPORTS:
        raise ValueError(f"esportazione «{what}»: {', '.join(EXPORTS)}")
    fmt = str(format or "").strip().lower().lstrip(".")
    if fmt == "tiff":
        fmt = "tif"
    if fmt not in EXPORTS[what]:
        raise ValueError(f"formato «{format}» per «{what}»: {', '.join(EXPORTS[what])}")
    chunk = _chunk(chunk, window)
    ext = ".tif" if fmt == "cog" else f".{fmt}"
    if path is None:
        if window is None:
            raise ValueError("senza la finestra serve il percorso del file (path) in cui esportare")
        path = window.ask_save_path("Esporta", f"{what}{ext}", f"{fmt.upper()} (*{ext})")
        if not path:
            return {"what": what, "format": fmt, "cancelled": True, "chunk": chunk.label}
    p = Path(path)
    if p.suffix == "":
        p = p.with_suffix(ext)
    elif p.suffix.lower() not in (ext, ".tiff" if ext == ".tif" else ext):
        raise ValueError(f"il file «{p.name}» non ha l'estensione di {fmt} ({ext})")
    progress = as_progress(progress)
    try:
        out = Path(_EXPORTERS[what](chunk, p, fmt, progress) or p)
    except RuntimeError as exc:  # "no orthomosaic in chunk"...
        raise ValueError(f"«{what}» non esportato: {exc}") from None
    size = out.stat().st_size if out.is_file() else sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    return {"what": what, "format": fmt, "file": out.name, "bytes": int(size), "chunk": chunk.label}


def _plan_units(chunk) -> list:
    from ..archaeo.stratigraphy import UnitPolygon

    units = []
    for d in (chunk.meta.get("drawings") or {}).get("units") or []:
        try:
            units.append(UnitPolygon.from_dict(d))
        except Exception:  # noqa: BLE001 - a broken record is left out, as the window does
            continue
    return units


def _export_units(chunk, p, fmt, progress):
    from ..archaeo.stratigraphy import export_units

    units = _plan_units(chunk)
    if not units:
        raise ValueError(f"il chunk «{chunk.label}» non ha unità disegnate sulla pianta")
    return export_units(units, p, crs=chunk.crs)


def _export_stones(chunk, p, fmt, progress):
    from shapely import wkt

    from ..archaeo.stones import export_stones

    rows = [wkt.loads(r) for items in (chunk.meta.get("stones") or {}).values() for r in items or []]
    if not rows:
        raise ValueError(f"il chunk «{chunk.label}» non ha pietre disegnate sulla pianta")
    return export_stones(rows, p, title=p.stem)


def _export_report(chunk, p, fmt, progress):
    if fmt == "pdf":
        return chunk.generate_pdf_report(p, progress=progress)
    return chunk.generate_report(p, language=None)


def _export_ifc(chunk, p, fmt, progress):
    from ..bim.ifc import export_chunk_ifc

    return export_chunk_ifc(chunk, p, units=_plan_units(chunk) or None, progress=progress)


_EXPORTERS = {
    "orthomosaic": lambda ch, p, fmt, pr: ch.export_orthomosaic(p, cog="auto" if fmt == "cog" else None),
    "dem": lambda ch, p, fmt, pr: ch.export_dem(p, cog="auto" if fmt == "cog" else None),
    "model": lambda ch, p, fmt, pr: ch.export_model(p),
    "point_cloud": lambda ch, p, fmt, pr: ch.export_point_cloud(p),
    "units": _export_units, "stones": _export_stones, "report": _export_report, "ifc": _export_ifc,
    "tables": lambda ch, p, fmt, pr: ch.export_tables(p, progress=pr),
}


# ------------------------------------------------------------------ scripts
def run_script(chunk, window, source: str, progress=None) -> dict:
    """``source`` run as *Esegui* runs a script (:func:`.scripts.run_script`) with the names of the console: ``doc``,
    ``chunk``, ``app``, ``ppg``, ``np``, ``window``, ``polygon`` (the selection), ``cameras`` (the photos selected).
    Returns the end of what it printed, its error and the calls outside the project it made."""
    import numpy as np

    import pyarchinit_photogrammetry as ppg

    from .scripts import run_script as run

    if not str(source or "").strip():
        raise ValueError("nessuno script da eseguire")
    polygon, cameras = None, []
    if window is not None:
        with contextlib.suppress(Exception):
            polygon = window.selected_polygon()
        with contextlib.suppress(Exception):
            cameras = list(window.selected_cameras())
    namespace = {"doc": _document(chunk, window), "chunk": chunk, "app": ppg.app, "ppg": ppg, "np": np,
                 "window": window, "polygon": polygon, "cameras": cameras, "__name__": "__main__"}
    result = run(source, namespace, as_progress(progress), name="assistant")
    return {"output": result.output[-4000:], "error": _no_paths(result.error),
            "traceback": _no_paths(result.traceback[-2000:]) if result.traceback else None,
            "external": list(dict.fromkeys(result.external)), "seconds": round(result.elapsed, 2)}


_PATH = re.compile(r"""(?<![\w.])(?:[A-Za-z]:)?(?:[\\/][^\s"'<>\\/]+)*[\\/]([^\s"'<>\\/]+)""")


def _no_paths(text: str | None) -> str | None:
    """``text`` with every absolute path reduced to its file name (what reaches the model never says where)."""
    return None if text is None else _PATH.sub(r"\1", text)


# ------------------------------------------------------------------ the registry for the tools
def _prop(kind: str | list, it: str, en: str, enum=None, default=None, items: str | None = None) -> dict:
    out = {"type": list(kind) if isinstance(kind, (list, tuple)) else kind, "description_it": it,
           "description_en": en}
    if enum is not None:
        out["enum"] = list(enum)
    if default is not None:
        out["default"] = default
    if items is not None:
        out["items"] = {"type": items}
    return out


_CHUNK_LABEL = _prop("string", "Il chunk (per nome); vuoto: quello attivo",
                     "The chunk (by label); empty: the active one")
_BOUNDS = _prop(["array", "string"],
                "Riquadro [xmin, ymin, xmax, ymax] nelle coordinate del raster, o il testo \"selection\": il poligono "
                "selezionato nella vista 2D",
                "Box [xmin, ymin, xmax, ymax] in the raster's coordinates, or the text \"selection\": the polygon "
                "selected in the 2D view", items="number")
_THOROUGH = ["normale", "alta", "massima"]

ACTIONS: dict[str, tuple] = {
    "show": (show, True,
             "Mostra nella finestra l'ortofoto, il DEM, il modello, la nuvola, un prospetto o un raster del "
             "calcolatore (anche una versione conservata o un altro chunk)",
             "Shows the orthophoto, the DEM, the model, the cloud, an elevation or a raster of the calculator in the "
             "window (also a kept version or another chunk)",
             {"type": "object", "required": ["what"], "properties": {
                 "what": _prop("string", "Cosa: ortho, dem, model, cloud, elevation o il nome di un raster del "
                               "calcolatore", "What: ortho, dem, model, cloud, elevation or the name of a raster of "
                               "the calculator"),
                 "chunk_label": _CHUNK_LABEL,
                 "version": _prop("string", "Una versione conservata (nome o id); vuoto: quella attiva",
                                  "A kept version (name or id); empty: the active one"),
                 "elevation": _prop("string", "Il prospetto o la sezione (nome o id) quando what è elevation",
                                    "The elevation or section (name or id) when what is elevation")}}),
    "select_photos": (select_photos, True,
                      "Sceglie foto (per area, marker, nome, tutte, nessuna) e le filtra o seleziona nel pannello "
                      "Foto, o le attiva/disattiva",
                      "Chooses photos (by area, markers, name, all, none) and filters or selects them in the Photos "
                      "pane, or switches them on/off",
                      {"type": "object", "required": ["by"], "properties": {
                          "by": _prop("string", "Come scegliere le foto", "How to choose the photos", BY),
                          "then": _prop("string", "Cosa farne", "What to do with them", THEN, "filter"),
                          "area": _BOUNDS,
                          "markers": _prop(["array", "string", "integer"],
                                           "I marker: una lista di nomi, chiavi o codici, o uno solo",
                                           "The markers: a list of labels, keys or codes, or one of them",
                                           items="string"),
                          "pattern": _prop("string", "Schema dei nomi (es. DJI_01*)", "Pattern of the names "
                                           "(e.g. DJI_01*)"),
                          "enabled": _prop("boolean", "Solo le foto attive (true) o disattivate (false)",
                                           "Only the photos switched on (true) or off (false)")}}),
    "stones": (stones, False,
               "Disegna le pietre (SAM) su un'ortofoto o un prospetto, nel layer «Pietre»",
               "Draws the stones (SAM) on an orthophoto or an elevation, in the layer «Pietre»",
               {"type": "object", "properties": {
                   "raster": _prop("string", "Raster del calcolatore o prospetto; vuoto: quello mostrato",
                                   "Raster of the calculator or elevation; empty: the one shown"),
                   "bounds": _BOUNDS,
                   "min_size": _prop("number", "Pietra più piccola (m)", "Smallest stone (m)", default=0.05),
                   "max_size": _prop("number", "Pietra più grande (m)", "Largest stone (m)", default=1.0),
                   "thoroughness": _prop("string", "Scansione", "Thoroughness", _THOROUGH, "alta")}}),
    "build": (build, False,
              "Ricostruisce un prodotto del chunk con i parametri dati (la versione di prima resta conservata)",
              "Builds a product of the chunk again with the parameters given (the version before is kept)",
              {"type": "object", "required": ["product"], "properties": {
                  "product": _prop("string", "Il prodotto", "The product", PRODUCTS),
                  "chunk_label": _CHUNK_LABEL,
                  "params": {"type": "object", "description_it": "Parametri del prodotto (vedi BUILD_PARAMS)",
                             "description_en": "Parameters of the product (see BUILD_PARAMS)",
                             "per_product": {p: {k: _prop(t, it, en, enum) for k, (t, it, en, enum) in ks.items()}
                                             for p, ks in BUILD_PARAMS.items()}}}}),
    "segment": (segment, False,
                "Trova con SAM le unità nell'area (sul raster mostrato) e le aggiunge come candidate nel layer "
                "«Unita' da SAM»",
                "Finds the units in the area with SAM (on the raster shown) and adds them as candidates to the layer "
                "«Unita' da SAM»",
                {"type": "object", "properties": {
                    "bounds": _BOUNDS,
                    "prompt": _prop("string", "Tipo delle unità (US, USM...) o una descrizione",
                                    "Kind of the units (US, USM...) or a description"),
                    "thoroughness": _prop("string", "Scansione", "Thoroughness", _THOROUGH, "normale")}}),
    "export": (export, False,
               "Esporta un prodotto o i disegni in un file (la finestra chiede dove)",
               "Exports a product or the drawings to a file (the window asks where)",
               {"type": "object", "required": ["what", "format"], "properties": {
                   "what": _prop("string", "Cosa esportare", "What to export", list(EXPORTS)),
                   "format": _prop("string", "Formato (" + "; ".join(f"{k}: {', '.join(v)}" for k, v in EXPORTS.items())
                                   + ")", "Format (" + "; ".join(f"{k}: {', '.join(v)}" for k, v in EXPORTS.items())
                                   + ")", sorted({f for v in EXPORTS.values() for f in v})),
                   "path": _prop("string", "Il file; vuoto: lo chiede la finestra",
                                 "The file; empty: the window asks")}}),
    "run_script": (run_script, False,
                   "Esegue uno script Python sul progetto, come Esegui nell'editor",
                   "Runs a Python script on the project, as Run in the editor does",
                   {"type": "object", "required": ["source"], "properties": {
                       "source": _prop("string", "Il codice Python", "The Python code")}}),
}


def is_quick(name: str, params: dict | None = None) -> bool:
    """Whether the action ``name`` with ``params`` runs at once under *Subito le rapide*: showing, and selecting
    photos to filter or select them (not to switch them on or off)."""
    if name == "select_photos":
        return (params or {}).get("then", "filter") in ("filter", "select")
    entry = ACTIONS.get(name)
    return bool(entry and entry[1])


__all__ = ["ACTIONS", "BUILD_PARAMS", "EXPORTS", "PRODUCTS", "build", "export", "is_quick", "run_script", "segment",
           "select_photos", "show", "stones"]
