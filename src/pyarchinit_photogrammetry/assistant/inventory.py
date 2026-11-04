"""The inventory of the open project the assistant gets with every question.

:func:`snapshot` reads what the project holds - the chunks with their photos, products and versions, the rasters of
the raster calculator, the drawings' counts, the elevations - and, given the window, what it shows: the raster of the
2D view and its version, the elevation shown, the 3D view, the selection and the photo filter. Only metadata are read
(``chunk.meta``, the versions, whether a file is there, the header of a raster): never the pixels of a raster nor the
model. The window is read in the interface thread only: the pane takes the snapshot before it hands the question to
its thread.

The rasters of the calculator (an ``os.walk`` of the chunk folder and the header of each GeoTIFF) are read for the
active chunk only: the other chunks carry an empty ``rasters`` list.

The snapshot holds strings and numbers only, no path and no coordinate, so that :func:`filtered` (the privacy
:class:`~.privacy.Filter` of the external models) can turn its names into placeholders. :func:`text` writes it as the
short block ``## Il progetto adesso`` / ``## The project now`` of the system prompt.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

LIMIT = 1500  # characters of the block in the system prompt
MAX_VERSIONS = 5  # version names listed for a product of the active chunk
MAX_RASTERS = 15  # raster names listed for the active chunk
MAX_STALE = 6  # names of the products to build again listed for a chunk
DRAWINGS = ("units", "quote", "points", "lines", "stones", "free")
FREE_KINDS = ("line", "polygon", "point")

_WORDS = {
    "it": {"title": "## Il progetto adesso", "project": "Progetto", "site": "sito", "active": "chunk attivo",
           "none_active": "nessun chunk attivo", "no_chunks": "il progetto non ha nessun chunk",
           "photos": "foto", "aligned": "allineate", "products": "prodotti", "no_products": "nessun prodotto",
           "active_version": "attiva", "versions": "versioni", "rasters": "raster", "drawings": "disegni",
           "elevations": "prospetti", "disabled": "disattivato", "others": "Altri chunk:",
           "more": "... ({0} altri chunk)", "view2d": "Vista 2D", "view3d": "Vista 3D", "version": "versione",
           "elevation": "prospetto", "nothing": "niente", "selection": "Selezione", "polygon": "poligono di {0} m²",
           "no_polygon": "nessun poligono", "selected": "{0} foto selezionate", "filtered": "filtro delle foto: {0} foto",
           "units": "US", "quote": "quote", "points": "punti di misura", "lines": "linee", "stones": "pietre",
           "oriented": "orientato a mano il {0}", "stale": "da rifare: {0}", "free": "disegni 3D",
           "free_layers": "layer dei disegni 3D", "free_line": "linee", "free_polygon": "poligoni",
           "free_point": "punti"},
    "en": {"title": "## The project now", "project": "Project", "site": "site", "active": "active chunk",
           "none_active": "no active chunk", "no_chunks": "the project has no chunk",
           "photos": "photos", "aligned": "aligned", "products": "products", "no_products": "no product",
           "active_version": "active", "versions": "versions", "rasters": "rasters", "drawings": "drawings",
           "elevations": "elevations", "disabled": "disabled", "others": "Other chunks:",
           "more": "... ({0} more chunks)", "view2d": "2D view", "view3d": "3D view", "version": "version",
           "elevation": "elevation", "nothing": "nothing", "selection": "Selection", "polygon": "polygon of {0} m²",
           "no_polygon": "no polygon", "selected": "{0} photos selected", "filtered": "photo filter: {0} photos",
           "units": "units", "quote": "spot heights", "points": "measuring points", "lines": "lines",
           "stones": "stones", "oriented": "oriented by hand on {0}", "stale": "to rebuild: {0}", "free": "3D drawings",
           "free_layers": "3D drawing layers", "free_line": "lines", "free_polygon": "polygons",
           "free_point": "points"},
}


# ------------------------------------------------------------------ the snapshot
def snapshot(document, window=None) -> dict | None:
    """What ``document`` holds and, with ``window`` (the main window), what it shows; None without a project.

    ``active`` is the label of the active chunk and ``active_key`` its key. ``rasters`` (the names of the raster
    calculator) is filled for the active chunk only, an empty list for the others. With a window it must be called in
    the interface thread (a RuntimeError otherwise): its widgets are read."""
    if window is not None:
        from ..gui.gui_thread import in_gui_thread

        if not in_gui_thread():
            raise RuntimeError("inventory.snapshot reads the window: call it in the interface thread")
    if document is None:
        return None
    active = _safe(lambda: document.chunk, None)
    path = getattr(document, "path", None)
    catalog = _catalog(active) if active is not None else []  # read once, for the active chunk only
    chunks = []
    for chunk in list(getattr(document, "chunks", []) or []):
        mine = chunk is active
        live = _window_drawings(window) if window is not None and mine else None
        chunks.append(_chunk(chunk, live, [r.name for r in catalog] if mine else []))
    return {"project": path.stem if path is not None else None,
            "site": (getattr(document, "meta", None) or {}).get("sito") or None,
            "active": active.label if active is not None else None,
            "active_key": active.key if active is not None else None,
            "chunks": chunks,
            "view": _view(window, active, catalog),
            "selection": _selection(window)}


def _safe(fn, default):
    """``fn()``, or ``default`` when it fails: a part of the inventory never stops the question."""
    try:
        return fn()
    except Exception:  # noqa: BLE001
        log.debug("inventario", exc_info=True)
        return default


def _products(chunk) -> dict:
    """The products whose file is there: ``{name: {"versions": [names], "active": name}}`` (versions for the
    products the program keeps versions of: dense cloud, model, DEM, orthophoto)."""
    from ..core import versions
    from .facts import _version_name

    out = {}
    for name in sorted(chunk.products):
        p = _safe(lambda: chunk.product_path(name), None)
        if p is None or not p.exists():
            continue
        entry: dict = {"versions": [], "active": None}
        if name in versions.PRODUCTS:
            entry["versions"] = [str(e.get("name") or e.get("id")) for e in versions.versions(chunk, name)]
            entry["active"] = _safe(lambda: _version_name(chunk, name), None)
        out[name] = entry
    return out


def _catalog(chunk) -> list:
    """The rasters of the raster calculator of ``chunk`` (their headers only)."""
    from ..engine import rastercalc

    return list(_safe(lambda: rastercalc.rasters(chunk), []) or [])


def _elevations(chunk) -> list[dict]:
    return [e for e in chunk.meta.get("elevations") or [] if isinstance(e, dict)]


def _drawings(chunk) -> dict:
    """The counts of the drawings of the plan as the chunk keeps them."""
    d = chunk.meta.get("drawings") or {}
    stones = chunk.meta.get("stones") or {}
    return {"units": len(d.get("units") or []), "quote": len(d.get("quote") or []),
            "points": len(d.get("points") or []), "lines": len(d.get("lines") or []),
            "stones": sum(len(v or []) for v in stones.values()) if isinstance(stones, dict) else 0,
            "free": _free_count(chunk)}


def _free_count(chunk) -> int:
    from ..core import free_drawings

    return len(free_drawings.features(chunk))


def _free_layers(chunk) -> list[dict]:
    """The layers of the free drawings in 3D that hold some, with their counts per kind (names only: no
    coordinates)."""
    from ..core import free_drawings

    feats = free_drawings.features(chunk)
    out = []
    for lay in free_drawings.layers(chunk):
        mine = [f for f in feats if f["layer"] == lay["name"]]
        if mine:
            out.append({"name": lay["name"], **{k: sum(1 for f in mine if f["kind"] == k) for k in FREE_KINDS}})
    return out


def _window_drawings(window) -> dict | None:
    """The counts of the drawings of the plan of the active chunk as the window holds them (not saved yet)."""
    def read():
        units = [u for u in window.units if not window._is_pa_unit(u)]
        return {"units": len(units), "quote": len(window.quote), "points": len(window.probe_points),
                "lines": len(getattr(window, "lines", []) or []),
                "stones": sum(len(v or []) for v in (window.stones or {}).values())}

    return _safe(read, None)


def _chunk(chunk, live: dict | None = None, rasters: list | None = None) -> dict:
    cameras = list(chunk.cameras)
    return {"label": chunk.label, "key": chunk.key, "photos": len(cameras),
            "enabled": bool(chunk.enabled), "aligned": sum(1 for c in cameras if c.aligned),
            "products": _safe(lambda: _products(chunk), {}),
            "rasters": list(rasters or []),
            "drawings": ({**live, "free": _safe(lambda: _free_count(chunk), 0)} if live is not None
                         else _safe(lambda: _drawings(chunk), {})),
            "free_layers": _safe(lambda: _free_layers(chunk), []),
            "elevations": [{"id": str(e.get("id")), "name": str(e.get("name") or e.get("id")),
                            "kind": e.get("kind")} for e in _elevations(chunk)],
            "oriented": _safe(lambda: _oriented(chunk), None),
            "stale": _safe(lambda: _stale(chunk), [])}


def _oriented(chunk) -> str | None:
    """The day (``YYYY-MM-DD``) the local system of ``chunk`` was oriented by hand, None if it never was."""
    made = (chunk.meta.get("orientation") or {}).get("made")
    return str(made)[:10] if made else None


def _stale(chunk) -> list[str]:
    """The names of the products made in an earlier system of the chunk, to be built again (in the order of a
    rebuild, :func:`.core.stamps.stale_products`)."""
    from ..core import stamps

    return [str(item.get("name") or item.get("product")) for item in stamps.stale_products(chunk)]


def _view(window, chunk, catalog: list) -> dict:
    view = {"kind": None, "raster": None, "version": None, "elevation": None, "model": None}
    if window is None:
        return view
    view["model"] = _safe(lambda: window.model_view.kind or None, None)
    ov = _safe(lambda: window.ortho_view, None)
    if ov is None or chunk is None:
        return view
    view["kind"] = ov.kind or None
    shown = _safe(lambda: window._raster_version, None)
    frame = _safe(lambda: window.drawings_frame, None)
    if frame is None and isinstance(shown, tuple) and shown and shown[0] == "elevation":
        frame = shown[1]
    if frame is not None:  # an elevation or a section: its name, not a raster of the plan
        entry = next((e for e in _elevations(chunk) if e.get("id") == frame), None)
        view["elevation"] = str(entry.get("name") or frame) if entry else str(frame)
        return view
    path = ov.path
    if path is None:
        return view
    view["raster"] = _raster_name(catalog, Path(path))
    if isinstance(shown, tuple) and len(shown) == 2:  # a kept version shown
        from ..core import versions

        product, vid = shown
        entry = next((e for e in _safe(lambda: versions.versions(chunk, product), []) if e.get("id") == vid), None)
        view["version"] = str(entry.get("name") or vid) if entry else str(vid)
    elif view["kind"] in ("ortho", "dem"):
        from .facts import _version_name

        view["version"] = _safe(lambda: _version_name(chunk, "orthomosaic" if view["kind"] == "ortho" else "dem"),
                                None)
    return view


def _raster_name(catalog: list, path: Path) -> str:
    """The name of the raster calculator of the file shown; else the neutral ``"raster"`` (a file name the user
    chose would leave the computer as typed)."""
    try:
        wanted = path.resolve()
        for r in catalog:
            if Path(r.path).resolve() == wanted:
                return r.name
    except Exception:  # noqa: BLE001
        log.debug("raster mostrato", exc_info=True)
    return "raster"


def _selection(window) -> dict:
    out = {"polygon_m2": None, "photos_selected": 0, "photos_filtered": None}
    if window is None:
        return out
    polygon = _safe(window.selected_polygon, None)
    if polygon is not None and not polygon.is_empty:
        out["polygon_m2"] = round(float(polygon.area), 3)
    out["photos_selected"] = len(_safe(window.selected_cameras, []) or [])
    photos = _safe(lambda: window.photos, None)
    if photos is not None and getattr(photos, "_filter", None) is not None:
        out["photos_filtered"] = len(_safe(photos.visible_cameras, []) or [])
    return out


# ------------------------------------------------------------------ the filter
def filtered(snap: dict | None, filter_) -> dict | None:
    """``snap`` as it may leave the computer through ``filter_`` (:meth:`~.privacy.Filter.facts`): names as
    placeholders. The name of the elevation shown goes through the filter as a name, not dropped by its key."""
    if snap is None:
        return None
    out = filter_.facts(snap)
    if not filter_.active:
        return out
    elevation = (snap.get("view") or {}).get("elevation")
    if isinstance(out.get("view"), dict):
        out["view"]["elevation"] = filter_.outgoing(elevation) if isinstance(elevation, str) else None
    return out


# ------------------------------------------------------------------ the text
def _number(x: float, lang: str) -> str:
    s = f"{x:,.2f}".rstrip("0").rstrip(".")
    if lang == "it":
        s = s.replace(",", "\0").replace(".", ",").replace("\0", ".")
    return s


def _quoted(name: Any) -> str:
    return f"«{name}»"


def _cap(items: list, n: int, quote: bool = False) -> str:
    shown = [_quoted(x) if quote else str(x) for x in items[:n]]
    if len(items) > n:
        shown.append(f"+{len(items) - n}")
    return ", ".join(shown)


def _chunk_line(c: dict, lang: str, full: bool) -> str:
    w = _WORDS[lang]
    head = f"{_quoted(c.get('label'))} (key {c.get('key')})"
    bits = [f"{c.get('photos', 0)} {w['photos']}, {c.get('aligned', 0)} {w['aligned']}"]
    products = c.get("products") or {}
    if not products:
        bits.append(w["no_products"])
    elif not full:
        bits.append(f"{w['products']}: {', '.join(products)}")
    else:
        parts = []
        for name, info in products.items():
            info = info or {}
            more = []
            if info.get("active"):
                more.append(f"{w['active_version']} {_quoted(info['active'])}")
            if info.get("versions"):
                more.append(f"{w['versions']}: {_cap(list(info['versions']), MAX_VERSIONS, quote=True)}")
            parts.append(f"{name} ({'; '.join(more)})" if more else str(name))
        bits.append(f"{w['products']}: {', '.join(parts)}")
    if full:
        if c.get("rasters"):
            bits.append(f"{w['rasters']}: {_cap(list(c['rasters']), MAX_RASTERS)}")
        drawings = [f"{n} {w[k]}" for k, n in (c.get("drawings") or {}).items() if k in DRAWINGS and n]
        if drawings:
            bits.append(f"{w['drawings']}: {', '.join(drawings)}")
        free = []
        for lay in c.get("free_layers") or []:
            counts = [f"{lay.get(k)} {w['free_' + k]}" for k in FREE_KINDS if lay.get(k)]
            free.append(f"{_quoted(lay.get('name'))}: {', '.join(counts)}")
        if free:
            bits.append(f"{w['free_layers']}: {_cap(free, MAX_VERSIONS)}")
        if c.get("elevations"):
            names = [f"{_quoted(e.get('name'))} ({e.get('kind')})" for e in c["elevations"][:MAX_VERSIONS]]
            if len(c["elevations"]) > MAX_VERSIONS:
                names.append(f"+{len(c['elevations']) - MAX_VERSIONS}")
            bits.append(f"{w['elevations']}: {', '.join(names)}")
    if c.get("oriented"):
        bits.append(w["oriented"].format(c["oriented"]))
    if c.get("stale"):
        bits.append(w["stale"].format(_cap(list(c["stale"]), MAX_STALE)))
    if c.get("enabled") is False:
        bits.append(w["disabled"])
    return f"{head}: {'; '.join(bits)}"


def text(snap: dict, lang: str, limit: int = LIMIT) -> str:
    """The Markdown block of ``snap``: the active chunk in full, the others one line each, at most ``limit``
    characters (the chunks that do not fit counted in the last line)."""
    lang = "it" if (lang or "").lower().startswith("it") else "en"
    w = _WORDS[lang]
    head = [w["title"]]
    first = []
    if snap.get("project"):
        first.append(f"{w['project']} {_quoted(snap['project'])}")
    if snap.get("site"):
        first.append(f"{w['site']} {_quoted(snap['site'])}")
    chunks = list(snap.get("chunks") or [])
    if not chunks:
        first.append(w["no_chunks"])
    elif snap.get("active"):
        first.append(f"{w['active']} {_quoted(snap['active'])}")
    else:
        first.append(w["none_active"])
    head.append(", ".join(first) + ".")
    key = snap.get("active_key")
    active = next((c for c in chunks if c.get("key") == key), None) if key is not None else None
    if active is None and key is None:  # a snapshot without the key: by the label
        active = next((c for c in chunks if c.get("label") == snap.get("active")), None)
    if active is not None:
        head.append(f"- {_chunk_line(active, lang, full=True)}")
    view = snap.get("view") or {}
    if any(view.get(k) for k in ("kind", "raster", "elevation", "model")):
        bits = []
        if view.get("elevation"):
            bits.append(f"{w['view2d']}: {w['elevation']} {_quoted(view['elevation'])}")
        elif view.get("kind") or view.get("raster"):
            shown = " ".join(str(x) for x in (view.get("kind"), _quoted(view["raster"]) if view.get("raster") else None)
                             if x)
            if view.get("version"):
                shown += f", {w['version']} {_quoted(view['version'])}"
            bits.append(f"{w['view2d']}: {shown}")
        bits.append(f"{w['view3d']}: {view.get('model') or w['nothing']}")
        head.append("; ".join(bits) + ".")
    sel = snap.get("selection") or {}
    if sel.get("polygon_m2") is not None or sel.get("photos_selected") or sel.get("photos_filtered") is not None:
        bits = [w["polygon"].format(_number(float(sel["polygon_m2"]), lang)) if sel.get("polygon_m2") is not None
                else w["no_polygon"]]
        if sel.get("photos_selected"):
            bits.append(w["selected"].format(sel["photos_selected"]))
        if sel.get("photos_filtered") is not None:
            bits.append(w["filtered"].format(sel["photos_filtered"]))
        head.append(f"{w['selection']}: {', '.join(bits)}.")
    lines = list(head)
    others = [c for c in chunks if c is not active]
    if others:
        lines.append(w["others"])
        used = sum(len(x) + 1 for x in lines)
        reserve = len(w["more"].format(len(others))) + 1
        shown = 0
        for c in others:
            line = f"- {_chunk_line(c, lang, full=False)}"
            room = limit - (reserve if shown < len(others) - 1 else 0)
            if used + len(line) + 1 > room:
                break
            lines.append(line)
            used += len(line) + 1
            shown += 1
        if shown < len(others):
            if shown == 0:
                lines.pop()  # no other chunk fits: only the count
            lines.append(w["more"].format(len(others) - shown))
    out = "\n".join(lines)
    if len(out) > limit:  # the active chunk alone is longer than the budget
        out = out[:max(0, limit - 1)].rstrip() + "…"
    return out
