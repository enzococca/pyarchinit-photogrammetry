"""Other chunks seen with the active one, before and after *Allinea chunk*.

3D view: the chunks ticked in the workspace (*Mostra nella vista 3D*) drawn with the active one in its world
coordinates - each through its own ``chunk.transform``, converted with PROJ into the system of the active chunk when
both are systems of the world; a local frame and a system of the world cannot be put together, such a chunk is drawn in
its own frame with a note. What is drawn is a preview: a stride of the points of the dense cloud (or of the tie
points), or the preview of the model when the active chunk shows its model, never the whole data. Colours: one per
chunk (:data:`CHUNK_COLOURS`), their own, or the signed distance to the surface of the active chunk
(:func:`..engine.chunks.DistanceSurface`, a diverging ramp with its legend).

2D view: the DEM or the orthophoto of another chunk under the drawings, over the raster of the active chunk, faded: a
reduced copy (:func:`overlay_image`) placed through the transform of the chunk - a raster built before an alignment is
moved with the chunk (its stamp, :mod:`..core.stamps`), approximately when the chunk was also tilted - and warped with
rasterio into the system of the active chunk when the two differ.

The choices are kept with the project in ``doc.meta["view"]`` (:func:`settings`, :func:`store`).
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

import numpy as np

from .i18n import decimal_comma, tr

# distinct colours (Sasha Trubetskoy's list, the most legible first); the n-th chunk of the project takes the n-th
CHUNK_COLOURS = ("#f58231", "#4363d8", "#3cb44b", "#e6194b", "#911eb4", "#42d4f4", "#f032e6", "#bfef45", "#ffe119",
                 "#9a6324", "#469990", "#800000")
MODES = ("chunk", "rgb", "distance")
MODE_LABELS = {"chunk": "Un colore per chunk", "rgb": "Colori veri", "distance": "Distanza dal chunk attivo"}
UNCOVERED = (150, 150, 150)  # points with no surface of the active chunk over or under them
ACTIVE_GREY = (96, 96, 100)  # the active chunk when the others carry the colours of their distances
# a diverging ramp: below the active surface blue, on it white, above it red (ColorBrewer RdBu)
RAMP = np.array([(33, 102, 172), (103, 169, 207), (247, 247, 247), (239, 138, 98), (178, 24, 43)], float)
OVERLAY_SIDE = 2000  # pixels of the reduced copy of another chunk's raster in the 2D view
DEFAULTS = {"chunks_3d": [], "colour": "chunk", "opacity": 1.0, "chunks_2d": {}}


# ---------------------------------------------------------------- the choices, with the project
def settings(doc) -> dict:
    """The choices of the views for the other chunks, as kept in ``doc.meta["view"]`` (defaults filled in):
    ``chunks_3d`` the keys of the chunks shown in the 3D view, ``colour`` one of :data:`MODES`, ``opacity`` of the other
    chunks in the 3D view (0-1), ``chunks_2d`` ``{key: {"shown": bool, "opacity": float}}`` (keys as strings: JSON)."""
    view = dict((getattr(doc, "meta", None) or {}).get("view") or {})
    out = {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    out.update({k: view[k] for k in DEFAULTS if k in view})
    if out["colour"] not in MODES:
        out["colour"] = "chunk"
    out["opacity"] = float(min(max(float(out["opacity"]), 0.05), 1.0))
    return out


def store(doc, **changes) -> bool:
    """Write ``changes`` into ``doc.meta["view"]``; True when something changed."""
    view = dict(doc.meta.get("view") or {})
    before = dict(view)
    view.update(changes)
    if view == before:
        return False
    doc.meta["view"] = view
    return True


def shown_3d(doc, active) -> list:
    """The other chunks ticked for the 3D view, in the order of the project (the active one never: it is always
    drawn)."""
    if doc is None:
        return []
    keys = set(settings(doc)["chunks_3d"])
    return [ch for ch in doc.chunks if ch.key in keys and ch is not active]


def set_shown_3d(doc, chunk, shown: bool) -> bool:
    """Tick or untick ``chunk`` for the 3D view; True when it changed."""
    keys = [k for k in settings(doc)["chunks_3d"] if any(c.key == k for c in doc.chunks)]
    if shown and chunk.key not in keys:
        keys.append(chunk.key)
    elif not shown and chunk.key in keys:
        keys.remove(chunk.key)
    else:
        return False
    return store(doc, chunks_3d=keys)


def chunk_colour(doc, chunk) -> str:
    """The colour of a chunk in the views: by its place in the project."""
    chunks = list(getattr(doc, "chunks", []) or [])
    i = next((n for n, c in enumerate(chunks) if c is chunk), 0)
    return CHUNK_COLOURS[i % len(CHUNK_COLOURS)]


def rgb_of(colour: str) -> np.ndarray:
    colour = colour.lstrip("#")
    return np.array([int(colour[i:i + 2], 16) for i in (0, 2, 4)], np.uint8)


# ---------------------------------------------------------------- 3D
@dataclass
class OtherLayer:
    """Another chunk in the 3D view: points (or a mesh) in the coordinates of the scene of the active chunk."""

    key: int
    label: str
    colour: str
    what: str = "point_cloud"  # point_cloud, tie_points or model
    placed: bool = True  # False: a local frame with a system of the world, drawn in its own frame
    points: np.ndarray | None = None  # (N, 3) float32
    rgb: np.ndarray | None = None  # (N, 3) uint8: its own colours
    mesh_vertices: np.ndarray | None = None
    mesh_faces: np.ndarray | None = None
    colors: np.ndarray | None = None  # (N, 3) uint8 colours shown, per point or per vertex
    distances: np.ndarray | None = None  # signed distance of each point or vertex to the active surface
    covered: np.ndarray | None = None
    stats: dict | None = None
    extra: dict = field(default_factory=dict)

    def coords(self) -> np.ndarray | None:
        return self.mesh_vertices if self.mesh_vertices is not None else self.points


def build_layer(chunk, active, offset, kind: str, max_points: int = 400_000,
                max_faces: int = 600_000) -> OtherLayer | None:
    """``chunk`` as a layer of the 3D view of ``active`` (scene offset ``offset``): the preview of its model when the
    active chunk shows a model and this one has it, else a stride of its dense cloud, tie points or model vertices.
    ``None`` when it has nothing to show."""
    from ..engine.chunks import preview_points, shown_in
    from .scene import display_model

    doc = chunk.document
    layer = OtherLayer(chunk.key, chunk.label, chunk_colour(doc, chunk))
    off = np.asarray(offset, float)
    path = chunk.product_path("model")
    if kind in ("model", "model_textured") and path is not None and path.exists():
        mesh = display_model(chunk, max_faces)
        if mesh is not None and len(mesh.faces):
            world, layer.placed = shown_in(chunk, np.asarray(mesh.vertices, float), active.crs)
            faces = np.asarray(mesh.faces)
            if len(faces) > max_faces:
                faces = faces[np.random.default_rng(0).choice(len(faces), max_faces, replace=False)]
            layer.what, layer.mesh_vertices, layer.mesh_faces = "model", (world - off).astype(np.float32), faces
            vc = getattr(mesh.visual, "vertex_colors", None)
            if vc is not None and len(vc) == len(mesh.vertices) and np.ptp(np.asarray(vc)[:, :3]) > 0:
                layer.rgb = np.asarray(vc)[:, :3].astype(np.uint8)
            return layer
    wanted = kind if kind in ("point_cloud", "tie_points") else "auto"
    try:
        internal, rgb, layer.what = preview_points(chunk, max_points, wanted)
    except ValueError:
        if wanted == "auto":
            return None
        try:
            internal, rgb, layer.what = preview_points(chunk, max_points, "auto")
        except ValueError:
            return None
    world, layer.placed = shown_in(chunk, internal, active.crs)
    layer.points = (world - off).astype(np.float32)
    layer.rgb = rgb
    return layer


def measure(layer: OtherLayer, surface, offset) -> None:
    """The signed distances of the layer's points (or vertices) to the surface of the active chunk, with their
    statistics (threshold: three spacings of the surface). A chunk drawn in its own frame is not measured."""
    from ..engine.chunks import distance_stats

    pts = layer.coords()
    if pts is None or not layer.placed:
        layer.distances = layer.covered = layer.stats = None
        return
    d, covered = surface.distances(np.asarray(pts, float) + np.asarray(offset, float))
    layer.distances, layer.covered = d, covered
    layer.stats = distance_stats(d, covered, 3.0 * surface.spacing)


def distance_limit(layers) -> float:
    """The end of the colour ramp: the 95th percentile of the distances of the covered points of all the layers,
    rounded up to 1, 2 or 5 times a power of ten."""
    vals = [np.abs(ly.distances[ly.covered]) for ly in layers if ly.distances is not None and ly.covered.any()]
    if not vals:
        vals = [np.abs(ly.distances) for ly in layers if ly.distances is not None and len(ly.distances)]
    if not vals:
        return 1.0
    v = float(np.percentile(np.concatenate(vals), 95))
    if not v > 0:
        return 0.001
    p = 10.0 ** np.floor(np.log10(v))
    for m in (1.0, 2.0, 5.0, 10.0):
        if v <= m * p * (1 + 1e-9):
            return m * p
    return 10.0 * p


def distance_colors(d: np.ndarray, limit: float, covered: np.ndarray | None = None) -> np.ndarray:
    """Diverging colours of signed distances (``-limit`` blue, 0 white, ``+limit`` red, beyond saturated); points with
    nothing of the active chunk over or under them grey."""
    t = np.clip(np.asarray(d, float) / max(float(limit), 1e-12), -1.0, 1.0) * 2.0 + 2.0  # 0..4 along the ramp
    i = np.minimum(np.floor(t).astype(int), 3)
    f = (t - i)[:, None]
    out = (RAMP[i] * (1 - f) + RAMP[i + 1] * f).astype(np.uint8)
    if covered is not None:
        out[~np.asarray(covered, bool)] = UNCOVERED
    return out


def length_text(v: float, metric: bool = True, signed: bool = False) -> str:
    """A distance for the legend: mm, cm or m when the system is in metres, else the plain number; two decimals under
    1, one under 10, none above (a median of hundredths of a millimetre is not "-0")."""
    if not metric:
        s, unit = v, ""
    else:
        a = abs(v)
        s, unit = (v * 1000.0, " mm") if a < 0.1 else (v * 100.0, " cm") if a < 1.0 else (v, " m")
    digits = 2 if abs(s) < 1 else 1 if abs(s) < 10 else 0
    text = f"{s:.{digits}f}"
    if float(text) == 0:
        text = "0"
    elif signed and s > 0:
        text = "+" + text
    return decimal_comma(text) + unit


def colour_scene(sd, layers, mode: str, active_colour: str) -> None:
    """The colours of the scene (the active chunk) and of the other layers for ``mode``: one colour per chunk, their
    own colours, or the distances (the active chunk grey)."""
    if mode == "chunk":  # the active chunk in its colour too, so that the two surfaces read apart
        rgb = rgb_of(active_colour)
        if sd.points is not None:
            sd.colors = np.repeat(rgb[None], len(sd.points), 0)
        if sd.mesh_vertices is not None:
            sd.mesh_uv, sd.texture = None, None
            sd.mesh_colors = np.repeat(rgb[None], len(sd.mesh_vertices), 0)
    elif mode == "distance":  # the active one in grey: the others carry the colours
        grey = np.array(ACTIVE_GREY, np.uint8)
        if sd.points is not None:
            sd.colors = np.repeat(grey[None], len(sd.points), 0)
        if sd.mesh_vertices is not None:
            sd.mesh_uv, sd.texture = None, None
            sd.mesh_colors = np.repeat(grey[None], len(sd.mesh_vertices), 0)
    limit = distance_limit(layers) if mode == "distance" else None
    for ly in layers:
        n = len(ly.coords())
        if mode == "chunk":
            ly.colors = np.repeat(rgb_of(ly.colour)[None], n, 0)
        elif mode == "distance" and ly.distances is not None:
            ly.colors = distance_colors(ly.distances, limit, ly.covered)
        elif ly.rgb is not None and len(ly.rgb) == n:
            ly.colors = ly.rgb
        else:
            ly.colors = np.repeat(np.array([200, 200, 200], np.uint8)[None], n, 0)


def legend_rows(active, layers, mode: str, metric: bool = True) -> list[tuple[str, str]]:
    """The rows of the legend of the other chunks: ``(colour "#rrggbb" or "", text)``."""
    doc = active.document
    rows = []
    if mode == "distance":
        limit = distance_limit(layers)
        rows.append(("#%02x%02x%02x" % ACTIVE_GREY, tr("{0} (attivo)", active.label)))
        for frac in (1.0, 0.5, 0.0, -0.5, -1.0):
            c = distance_colors(np.array([frac * limit]), limit)[0]
            text = length_text(frac * limit, metric, signed=True)
            if frac == 1.0:
                text = tr("{0} o più (sopra)", text)
            elif frac == -1.0:
                text = tr("{0} o meno (sotto)", text)
            rows.append(("#%02x%02x%02x" % tuple(int(v) for v in c), text))
        rows.append(("#%02x%02x%02x" % UNCOVERED, tr("Fuori dal chunk attivo")))
        for ly in layers:
            s = ly.stats
            if not ly.placed:
                rows.append(("", tr("{0}: nel proprio sistema locale, non misurato", ly.label)))
            elif s is None or s["median"] is None:
                rows.append(("", tr("{0}: nessuna sovrapposizione", ly.label)))
            else:
                rows.append(("", tr("{0}: mediana {1}, NMAD {2}, entro {3}: {4} %", ly.label,
                                    length_text(s["median"], metric, signed=True), length_text(s["nmad"], metric),
                                    length_text(s["threshold"], metric), f"{s['overlap'] * 100:.0f}")))
        return rows
    rows.append((chunk_colour(doc, active) if mode == "chunk" else "", tr("{0} (attivo)", active.label)))
    for ly in layers:
        text = ly.label if ly.placed else tr("{0} (nel proprio sistema locale)", ly.label)
        rows.append((ly.colour if mode == "chunk" else "", text))
    return rows


def shown_layer(layer: OtherLayer, to_display) -> OtherLayer:
    """The layer with its coordinates as shown (heights exaggerated by the scene's ``to_display``)."""
    out = dataclasses.replace(layer)
    if layer.points is not None:
        out.points = to_display(layer.points).astype(np.float32)
    if layer.mesh_vertices is not None:
        out.mesh_vertices = to_display(layer.mesh_vertices).astype(np.float32)
    return out


# ---------------------------------------------------------------- 2D
def raster_for(chunk, kind: str):
    """The raster of another chunk to show under the active one's ``kind`` ("ortho" or "dem"): the same kind when it
    has it, else the other; ``(path, kind)`` or ``(None, None)``."""
    order = ("dem", "ortho") if kind == "dem" else ("ortho", "dem")
    for k in order:
        path = chunk.dem_path if k == "dem" else chunk.orthomosaic_path
        if path is not None and path.exists():
            return path, k
    return None, None


def overlay_image(chunk, active, kind: str, dem_range=None, max_side: int = OVERLAY_SIDE):
    """A reduced copy of the DEM or orthophoto of ``chunk`` (:func:`raster_for`) for the 2D view of ``active``: RGBA
    ``(H, W, 4)`` uint8, the affine from its pixels to the world of the active chunk, the kind shown and a note
    ("" when it sits where it should). Placed through the transform of the chunk: a raster built in another transform
    (an alignment after it) is moved with the chunk - in plan only: a tilt of the chunk is noted, its heights are not
    corrected -; warped into the system of the active chunk when they differ (both systems of the world); a local frame
    with a system of the world stays in its own frame (noted). ``None`` when the chunk has no raster to show."""
    import rasterio
    from affine import Affine
    from rasterio.enums import Resampling

    from ..core import crs as crs_mod
    from ..core import geometry, stamps
    from .image_views import dem_colors, dem_limits

    path, k = raster_for(chunk, kind)
    if path is None:
        return None
    notes = []
    with rasterio.open(path) as src:
        f = max(1.0, max(src.width, src.height) / max_side)
        h, w = max(1, int(round(src.height / f))), max(1, int(round(src.width / f)))
        tr_ = src.transform @ Affine.scale(src.width / w, src.height / h)
        if k == "dem":
            z = src.read(1, out_shape=(h, w), resampling=Resampling.average, masked=True)
            z = z.astype(np.float64).filled(np.nan)
            rgba = dem_colors(z, abs(tr_.a), dem_range or dem_limits(z))
        else:
            n = min(src.count, 4)
            arr = np.moveaxis(src.read(list(range(1, n + 1)), out_shape=(n, h, w), resampling=Resampling.average),
                              0, -1)
            if n == 3:
                arr = np.dstack([arr, np.full((h, w), 255, np.uint8)])
            elif n < 3:
                arr = np.dstack([arr[..., 0]] * 3 + [np.full((h, w), 255, np.uint8)])
            rgba = np.ascontiguousarray(arr.astype(np.uint8))
    built = (chunk.meta.get("built_in") or {}).get("dem" if k == "dem" else "orthomosaic")
    if built is not None and not stamps.same_transform(built, chunk.transform):
        move = np.asarray(chunk.transform, float) @ np.linalg.inv(np.asarray(built, float))
        s, R, _t = geometry.decompose_similarity(move)
        tilt = float(np.degrees(np.arccos(np.clip(R[2, 2], -1.0, 1.0))))
        plan = Affine(move[0, 0], move[0, 1], move[0, 3], move[1, 0], move[1, 1], move[1, 3])
        tr_ = plan @ tr_
        notes.append(tr("costruito prima dell'allineamento: spostato con il chunk") if tilt < 0.5 else
                     tr("costruito prima dell'allineamento: spostato in pianta, il chunk è inclinato di {0} gradi",
                        decimal_comma(f"{tilt:.1f}")))
    src_crs, dst_crs = chunk.crs, active.crs
    if src_crs != dst_crs and not (crs_mod.is_local(src_crs) and crs_mod.is_local(dst_crs)):
        if crs_mod.is_local(src_crs) or crs_mod.is_local(dst_crs):
            notes.append(tr("nel proprio sistema locale"))
        else:
            rgba, tr_ = _warp(rgba, tr_, src_crs, dst_crs)
    return rgba, tr_, k, "; ".join(notes)


def _warp(rgba: np.ndarray, transform, src_crs: str, dst_crs: str):
    """A reduced RGBA copy warped into another system (nearest neighbour: colours, not values)."""
    from rasterio.enums import Resampling
    from rasterio.warp import calculate_default_transform, reproject

    from ..core import crs as crs_mod

    h, w = rgba.shape[:2]
    src_h, dst_h = crs_mod.horizontal(src_crs), crs_mod.horizontal(dst_crs)
    xs, ys = zip(*(transform * (c, r) for c, r in ((0, 0), (w, 0), (0, h), (w, h))))  # rotated after an alignment
    dst_t, dw, dh = calculate_default_transform(src_h, dst_h, w, h, left=min(xs), bottom=min(ys), right=max(xs),
                                                top=max(ys))
    out = np.zeros((4, dh, dw), np.uint8)
    reproject(np.moveaxis(rgba, -1, 0), out, src_transform=transform, src_crs=src_h, dst_transform=dst_t,
              dst_crs=dst_h, resampling=Resampling.nearest)
    return np.ascontiguousarray(np.moveaxis(out, 0, -1)), dst_t
