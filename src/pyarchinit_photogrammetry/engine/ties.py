"""Ties between chunks: the same point in two chunks, or a point of a chunk and its known coordinates.

A tie is made of markers: each end is a :class:`~..core.marker.Marker` of its chunk (made by the tie, labelled
"legame N" or the name given, or a marker the user had already), so photos, triangulation, the 3D pick, the
refinement by correlation and the blue flags already work for its ends. The ends are kept as (chunk key, marker key)
and the markers hold their positions in the internal frame of their chunk: a tie survives every new transform of the
chunks it links. The ties belong to the document (``doc.meta["ties"]``), not to a chunk: they link different chunks.

A tie towards known coordinates (a GNSS point, a benchmark) has one end only (``b`` None) and ``known``: world
coordinates in the system ``crs`` of the chunk when the tie was made; its marker also gets them as its reference,
as a ground control point has today.

``precision`` (metres) is the precision of the tie as a whole (both ends together): the weight of the tie in the joint
solve is ``1 / precision**2``. Defaults: an end measured on photos -> :func:`end_precision` (the reprojection error of
its triangulation in metres, at least 1 cm); an end clicked on an orthophoto or a DEM -> 2 cells of the raster
(:func:`raster_precision`); a row of a table without one -> :data:`TABLE_PRECISION`; the ends combined as
``hypot``. Any of them can be changed by hand.
"""
from __future__ import annotations

import csv
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from ..core import geometry
from ..core.marker import Marker

if TYPE_CHECKING:  # pragma: no cover
    from ..core.chunk import Chunk
    from ..core.document import Document

META_KEY = "ties"
LABEL = "legame"  # the label of the markers a tie makes: data of the project (as the names of the layers), not UI text
TABLE_PRECISION = 0.05  # metres: a row of a table without its precision
MIN_PHOTO_PRECISION = 0.01  # metres: the least precision of an end measured on photos
RASTER_PIXELS = 2.0  # an end clicked on an orthophoto or a DEM: two cells of the raster
_NUMBERED = re.compile(rf"^{LABEL} (\d+)$")


@dataclass
class Tie:
    """Two markers in two chunks that are the same point, or one marker and its known world coordinates.

    ``a``/``b``: (chunk key, marker key). ``b`` None -> ``known`` holds the world coordinates of ``a``'s point in
    ``crs`` (the chunk's system; read from the marker's reference, which is the one copy that counts). ``own_markers``:
    the markers of the ends were made by the tie (removed with it); False for markers the user had already
    (:func:`ties_from_markers`, :func:`link_markers`). ``a_uid``/``b_uid``: the identity (``Marker.uid``) of each
    end's marker, so that a key given again to another marker (the old one removed) is never taken for the end.
    ``broken``: set on read when an end's chunk or marker is gone (the tie is then inactive); not stored."""

    id: str
    a: tuple[int, int]
    b: tuple[int, int] | None
    known: list[float] | None
    precision: float
    name: str
    active: bool = True
    crs: str | None = None
    own_markers: bool = True
    a_uid: str | None = None
    b_uid: str | None = None
    broken: str | None = None
    broken_parts: list | None = None  # the reasons of ``broken`` as (message code, *args), for the report; not stored

    def ends(self) -> list[tuple[int, int]]:
        return [self.a] if self.b is None else [self.a, self.b]

    def end_uids(self) -> list[tuple[tuple[int, int], str | None]]:
        """The ends with the identity of their markers: ``[(end, uid), ...]``."""
        return [(self.a, self.a_uid)] if self.b is None else [(self.a, self.a_uid), (self.b, self.b_uid)]

    def chunk_keys(self) -> set[int]:
        return {k for k, _ in self.ends()}

    def to_dict(self) -> dict:
        d = {"id": self.id, "a": list(self.a), "b": None if self.b is None else list(self.b),
             "known": None if self.known is None else [float(v) for v in self.known],
             "precision": float(self.precision), "name": self.name, "active": bool(self.active),
             "own_markers": bool(self.own_markers)}
        for key in ("crs", "a_uid", "b_uid"):
            if getattr(self, key) is not None:
                d[key] = getattr(self, key)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Tie":
        b = d.get("b")
        known = d.get("known")
        return cls(id=str(d["id"]), a=(int(d["a"][0]), int(d["a"][1])),
                   b=None if b is None else (int(b[0]), int(b[1])),
                   known=None if known is None else [float(v) for v in known],
                   precision=float(d.get("precision", TABLE_PRECISION)), name=str(d.get("name", "")),
                   active=bool(d.get("active", True)), crs=d.get("crs"), own_markers=bool(d.get("own_markers", True)),
                   a_uid=d.get("a_uid"), b_uid=d.get("b_uid"))


# --------------------------------------------------------------------------- storage
def ties_of(doc: "Document") -> list[Tie]:
    """The ties of the document (new objects: change them and give them back with :func:`save_ties`), with their
    ends checked against the markers (:func:`_settle`): an end whose marker moved to another key is followed, a tie
    with an end gone is inactive and says why in ``broken``; a known tie takes its coordinates and precision from its
    marker's reference."""
    ties = [Tie.from_dict(d) for d in (doc.meta.get(META_KEY) or [])]
    for t in ties:
        _settle(doc, t)
    return ties


def save_ties(doc: "Document", ties: list[Tie]) -> None:
    """The ties written in ``doc.meta["ties"]`` (plain JSON); no key when there are none. Markers are never touched:
    the reference of a known point belongs to its marker (the user's, in the Riferimenti pane); it is changed only by
    :func:`add_known` and :func:`update_known`."""
    if ties:
        doc.meta[META_KEY] = [t.to_dict() for t in ties]
    else:
        doc.meta.pop(META_KEY, None)


def resolve_end(doc: "Document", end, uid: str | None = None):
    """``(chunk, marker)`` of an end, or None when its chunk or marker is gone. With ``uid`` the marker must be the
    one with that identity: the marker at the key if it has it, else the marker of the chunk that has it (its key
    changed); a marker at the key with another identity is another point, never the end."""
    chunk_key, marker_key = int(end[0]), int(end[1])
    chunk = next((c for c in doc.chunks if c.key == chunk_key), None)
    if chunk is None:
        return None
    at_key = next((m for m in chunk.markers if m.key == marker_key), None)
    if uid is None:
        return None if at_key is None else (chunk, at_key)
    if at_key is not None and at_key.uid == uid:
        return chunk, at_key
    moved = next((m for m in chunk.markers if m.uid == uid), None)
    return None if moved is None else (chunk, moved)


def _settle(doc: "Document", tie: Tie) -> None:
    """The ends of ``tie`` checked against the markers (see :func:`ties_of`)."""
    reasons, parts = [], []
    for which in ("a", "b"):
        end = getattr(tie, which)
        if end is None:
            continue
        uid = getattr(tie, f"{which}_uid")
        found = resolve_end(doc, end, uid)
        if found is None:
            chunk = next((c for c in doc.chunks if c.key == end[0]), None)
            reasons.append(f"{which}: chunk {end[0]} removed" if chunk is None else
                           f"{which}: marker {end[1]} of chunk {chunk.label!r} removed or replaced")
            parts.append(("out_chunk_removed", end[0]) if chunk is None else
                         ("out_marker_removed", end[1], chunk.label))
            continue
        chunk, marker = found
        if marker.key != end[1]:
            setattr(tie, which, (chunk.key, marker.key))
        if which == "a" and tie.b is None:
            if not marker.reference.location:  # cleared by the user: no known point any more (never put back)
                reasons.append(f"a: marker {marker.label!r} of chunk {chunk.label!r} has no reference any more")
                parts.append(("out_reference_cleared", marker.label, chunk.label))
                continue
            tie.known = [float(v) for v in marker.reference.location]
            tie.precision = float(max(marker.reference.accuracy or [tie.precision]))  # read only: per axis it stays
            tie.crs = chunk.crs
    if reasons:
        tie.broken = "; ".join(reasons)
        tie.broken_parts = parts
        tie.active = False


def update_known(doc: "Document", tie_id: str, known=None, precision: float | None = None) -> Tie:
    """An explicit edit of a known tie (the Legami pane): its coordinates and/or precision written to its marker's
    reference (the accuracy becomes ``precision`` on every axis), the only place they are kept."""
    ties = ties_of(doc)
    tie = next((t for t in ties if t.id == tie_id), None)
    if tie is None:
        raise KeyError(f"no tie {tie_id!r} in the document")
    if tie.b is not None:
        raise ValueError("only a tie towards known coordinates has them")
    found = resolve_end(doc, tie.a, tie.a_uid)
    if found is None:
        raise ValueError(f"the tie {tie.name!r} has lost its marker")
    marker = found[1]
    if known is not None:
        marker.reference.location = [float(v) for v in _point(known)]
        marker.reference.enabled = True
    if precision is not None:
        marker.reference.accuracy = [_check_precision(precision)] * 3
    for t in ties:
        _settle(doc, t)
    save_ties(doc, ties)
    return next(t for t in ties if t.id == tie_id)


def check_ends(doc: "Document") -> list[dict]:
    """The ties with an end gone (chunk removed, marker removed or replaced by another under its key, reference of a
    known point cleared): made inactive
    and stored so; returns ``[{"id", "name", "end", "reason"}]`` for the report. Ends whose marker moved to another
    key are stored with the new key."""
    ties = ties_of(doc)
    out = [{"id": t.id, "name": t.name, "end": t.broken.split(":")[0] if t.broken else "", "reason": t.broken}
           for t in ties if t.broken]
    if ties:
        save_ties(doc, ties)
    return out


def tie_by_id(doc: "Document", tie_id: str) -> Tie:
    for t in ties_of(doc):
        if t.id == tie_id:
            return t
    raise KeyError(f"no tie {tie_id!r} in the document")


def _new_id(taken: set[str]) -> str:
    while True:
        i = "t" + uuid.uuid4().hex[:8]
        if i not in taken:
            return i


def next_name(doc: "Document") -> str:
    """``legame N``: N one more than the highest number among the ties and the markers of every chunk."""
    numbers = [0]
    for name in [t.name for t in ties_of(doc)] + [m.label for c in doc.chunks for m in c.markers]:
        found = _NUMBERED.match(name or "")
        if found:
            numbers.append(int(found.group(1)))
    return f"{LABEL} {max(numbers) + 1}"


def _chunk(doc: "Document", item) -> "Chunk":
    from .chunks import resolve_chunk

    return resolve_chunk(doc, item)


def _marker(chunk: "Chunk", ref) -> Marker:
    return chunk._marker_ref(ref)


def _uid(marker: Marker) -> str:
    """The identity of a marker, given now when it has none."""
    if not marker.uid:
        marker.uid = uuid.uuid4().hex
    return marker.uid


def _free_label(chunk: "Chunk", name: str) -> str:
    """``name`` for a new marker of ``chunk``, made unique there ("P1 (legame)", "P1 (legame 2)"): two markers with
    one label would be read as the first by *Allinea chunk* and :func:`ties_from_markers`."""
    taken = {m.label for m in chunk.markers}
    if name not in taken:
        return name
    n, label = 1, f"{name} ({LABEL})"
    while label in taken:
        n += 1
        label = f"{name} ({LABEL} {n})"
    return label


# --------------------------------------------------------------------------- ends
def end_internal(chunk: "Chunk", marker: Marker) -> np.ndarray | None:
    """The internal position of an end, without changing the marker: its own position, else triangulated from two
    aligned photos or more, else - measured on one photo only - where the ray meets the surface (as
    :func:`..markers.gcp.triangulate_markers` places it). None when it has none: never a made-up zero."""
    from ..markers import gcp
    from .chunks import marker_position

    p = marker_position(chunk, marker)
    if p is not None:
        return np.asarray(p, float)
    for cam_key, proj in marker.projections.items():
        if not proj.measured:
            continue
        try:
            cam = chunk.camera(cam_key)
        except KeyError:
            continue
        if cam.enabled and cam.aligned:
            q = gcp.surface_point(chunk, cam, proj.x, proj.y)
            return None if q is None else np.asarray(q, float)
    return None


def end_world(doc: "Document", chunk_key: int, marker_key: int, uid: str | None = None) -> np.ndarray | None:
    """The world coordinates (the chunk's system, its current transform) of an end; None when the chunk, the marker
    or its position is missing. Give ``uid`` (``tie.a_uid``/``tie.b_uid``) so that another marker that took the key
    is never read as the end (:func:`resolve_end`)."""
    found = resolve_end(doc, (chunk_key, marker_key), uid)
    if found is None:
        return None
    chunk, marker = found
    p = end_internal(chunk, marker)
    return None if p is None else chunk.to_world(p[None])[0]


def end_precision(chunk: "Chunk", marker: Marker) -> float | None:
    """The precision (metres) of an end measured on photos: the RMS of its reprojection errors turned into metres at
    the distance of each photo (pixels x depth / focal length, x the chunk's scale), at least
    :data:`MIN_PHOTO_PRECISION`. Measured on one photo only, the end lies on the surface and its error is the
    surface's: two DEM cells of the chunk (:func:`raster_precision` ``"dem"``), else :data:`TABLE_PRECISION`. None
    when no aligned photo measures it or it has no position."""
    pos = end_internal(chunk, marker)
    if pos is None:
        return None
    errors = []
    for cam_key, proj in marker.projections.items():
        if not proj.measured:
            continue
        try:
            cam = chunk.camera(cam_key)
        except KeyError:
            continue
        if not (cam.enabled and cam.aligned):
            continue
        sensor = chunk.sensor(cam)
        px, depth = cam.project(sensor, pos[None])
        if not depth[0] > 0 or not np.all(np.isfinite(px)):
            continue
        err_px = float(np.hypot(px[0, 0] - proj.x, px[0, 1] - proj.y))
        errors.append(err_px * float(depth[0]) / sensor.focal_px)
    if not errors:
        return None
    if len(errors) < 2:
        return raster_precision(chunk, "dem") or TABLE_PRECISION
    metres = float(np.sqrt(np.mean(np.square(errors)))) * float(chunk.scale)
    return max(MIN_PHOTO_PRECISION, metres)


def raster_precision(chunk: "Chunk", source: str = "auto") -> float | None:
    """Two cells of a raster of the chunk, in metres: the precision of an end clicked on it. ``source`` ``"ortho"``,
    ``"dem"`` or ``"auto"`` (the orthophoto, else the DEM). A raster built in another system of the chunk (to be
    rebuilt) does not count; cells in degrees (a geographic system) are turned into metres (1 degree = 111 320 m:
    the latitude side, close enough for a weight). None without a raster to use."""
    from ..core import crs as crs_mod
    from ..core import stamps

    if source not in ("auto", "ortho", "dem"):
        raise ValueError(f"source is 'auto', 'ortho' or 'dem' (got {source!r})")
    products = {"auto": ("orthomosaic", "dem"), "ortho": ("orthomosaic",), "dem": ("dem",)}[source]
    for product in products:
        path = chunk.product_path(product)
        if path is None or not Path(path).exists() or stamps.is_stale(chunk, product):
            continue
        import rasterio

        with rasterio.open(path) as src:
            cell = float(max(abs(src.res[0]), abs(src.res[1])))
        if crs_mod.is_geographic(chunk.crs):
            cell *= 111_320.0
        return RASTER_PIXELS * cell
    return None


def _combined(*values: float | None) -> float | None:
    vals = [float(v) for v in values if v is not None]
    return float(np.hypot.reduce(vals)) if vals else None


def _default_precision(pairs) -> float:
    """``pairs``: (chunk, marker) of the ends: each end from its photos, else two cells of its chunk's raster;
    combined; :data:`TABLE_PRECISION` when nothing says it."""
    per_end = []
    for chunk, marker in pairs:
        p = end_precision(chunk, marker)
        per_end.append(p if p is not None else raster_precision(chunk))
    return _combined(*per_end) or TABLE_PRECISION


def _check_precision(precision) -> float:
    p = float(precision)
    if not np.isfinite(p) or p <= 0:
        raise ValueError(f"the precision of a tie must be positive (got {precision!r})")
    return p


# --------------------------------------------------------------------------- making ties
def link_markers(doc: "Document", chunk_a, marker_a, chunk_b, marker_b, name: str | None = None,
                 precision: float | None = None) -> Tie:
    """A tie between two markers that exist already (objects, keys or labels) in two different chunks; the markers
    stay theirs (``own_markers`` False). The same pair tied already gives that tie back (its name and precision
    changed when given)."""
    ca, cb = _chunk(doc, chunk_a), _chunk(doc, chunk_b)
    if ca is cb:
        raise ValueError("a tie links two different chunks")
    ma, mb = _marker(ca, marker_a), _marker(cb, marker_b)
    ua, ub = _uid(ma), _uid(mb)
    ties = ties_of(doc)
    ends = {(ca.key, ma.key), (cb.key, mb.key)}
    for t in ties:
        if t.b is not None and set(t.ends()) == ends:
            if name:
                t.name = name
            if precision is not None:
                t.precision = _check_precision(precision)
            save_ties(doc, ties)
            return t
    p = _check_precision(precision) if precision is not None else _default_precision([(ca, ma), (cb, mb)])
    tie = Tie(id=_new_id({t.id for t in ties}), a=(ca.key, ma.key), b=(cb.key, mb.key), known=None, precision=p,
              name=name or ma.label, own_markers=False, a_uid=ua, b_uid=ub)
    save_ties(doc, ties + [tie])
    return tie


def add_tie(doc: "Document", chunk_a, point_a_internal, chunk_b, point_b_internal, name: str | None = None,
            precision: float | None = None) -> Tie:
    """A tie between a point of ``chunk_a`` and the same point of ``chunk_b`` (internal coordinates of each): a new
    marker in each chunk at that position, both labelled ``name`` or "legame N" (N the next free number in the
    document). ``precision`` None -> two raster cells of each chunk (the points are clicked on its orthophoto/DEM),
    combined; :data:`TABLE_PRECISION` when the chunks have no raster."""
    ca, cb = _chunk(doc, chunk_a), _chunk(doc, chunk_b)
    if ca is cb:
        raise ValueError("a tie links two different chunks")
    pa, pb = _point(point_a_internal), _point(point_b_internal)
    if precision is not None:
        precision = _check_precision(precision)
    label = name or next_name(doc)
    ma, mb = ca.add_marker(_free_label(ca, label), pa), cb.add_marker(_free_label(cb, label), pb)
    if precision is None:
        precision = _combined(raster_precision(ca), raster_precision(cb)) or TABLE_PRECISION
    ties = ties_of(doc)
    tie = Tie(id=_new_id({t.id for t in ties}), a=(ca.key, ma.key), b=(cb.key, mb.key), known=None,
              precision=precision, name=label, a_uid=_uid(ma), b_uid=_uid(mb))
    save_ties(doc, ties + [tie])
    return tie


def add_known(doc: "Document", chunk, point_internal, world_xyz, name: str | None = None,
              precision: float | None = None) -> Tie:
    """A tie between a point of ``chunk`` (internal coordinates) and its known world coordinates ``world_xyz`` in
    the chunk's system: a new marker there whose reference is ``world_xyz`` with ``precision`` as its accuracy (as a
    ground control point). ``precision`` None -> :data:`TABLE_PRECISION`."""
    ch = _chunk(doc, chunk)
    p = _point(point_internal)
    known = [float(v) for v in _point(world_xyz)]
    precision = _check_precision(precision) if precision is not None else TABLE_PRECISION
    label = name or next_name(doc)
    m = ch.add_marker(_free_label(ch, label), p, reference=known)
    m.reference.accuracy = [precision] * 3
    ties = ties_of(doc)
    tie = Tie(id=_new_id({t.id for t in ties}), a=(ch.key, m.key), b=None, known=known, precision=precision,
              name=label, crs=ch.crs, a_uid=_uid(m))
    save_ties(doc, ties + [tie])
    return tie


def ties_from_markers(doc: "Document", chunk_a, chunk_b) -> list[Tie]:
    """A tie for every label that names a marker in both chunks (the first of each label, as *Allinea chunk* reads
    them), with no new markers; pairs tied already are left as they are. Returns the new ties."""
    ca, cb = _chunk(doc, chunk_a), _chunk(doc, chunk_b)
    if ca is cb:
        raise ValueError("a tie links two different chunks")
    in_b: dict[str, Marker] = {}
    for m in cb.markers:
        in_b.setdefault(m.label, m)
    ties = ties_of(doc)
    tied = {frozenset(t.ends()) for t in ties if t.b is not None}
    seen, made = set(), []
    for m in ca.markers:
        if m.label in seen or m.label not in in_b:
            continue
        seen.add(m.label)
        other = in_b[m.label]
        if frozenset({(ca.key, m.key), (cb.key, other.key)}) in tied:
            continue
        tie = Tie(id=_new_id({t.id for t in ties + made}), a=(ca.key, m.key), b=(cb.key, other.key), known=None,
                  precision=_default_precision([(ca, m), (cb, other)]), name=m.label, own_markers=False,
                  a_uid=_uid(m), b_uid=_uid(other))
        made.append(tie)
    if made:
        save_ties(doc, ties + made)
    return made


def remove_tie(doc: "Document", tie_id: str, keep_markers: bool = False) -> Tie:
    """The tie out of the document. Its markers go with it unless ``keep_markers``, the tie did not make them (they
    were the user's) or another tie still ends at them. Returns the removed tie."""
    ties = ties_of(doc)
    tie = next((t for t in ties if t.id == tie_id), None)
    if tie is None:
        raise KeyError(f"no tie {tie_id!r} in the document")
    rest = [t for t in ties if t.id != tie_id]
    save_ties(doc, rest)
    if keep_markers or not tie.own_markers:
        return tie
    used = {e for t in rest for e in t.ends()}
    for end, uid in tie.end_uids():
        found = resolve_end(doc, end, uid)  # never another marker that took the key
        if found is None or (found[0].key, found[1].key) in used:
            continue
        found[0].remove_markers([found[1].key])
    return tie


# --------------------------------------------------------------------------- tables
def _point(values) -> np.ndarray:
    p = np.asarray(values, float).reshape(-1)
    if p.shape != (3,) or not np.all(np.isfinite(p)):
        raise ValueError(f"a point has three finite coordinates (got {values!r})")
    return p


def _number(text: str, decimal_comma: bool) -> float | None:
    text = (text or "").strip()
    if not text:
        return None
    if decimal_comma:
        text = text.replace(",", ".")
    return float(text)


def _rows(path: Path) -> tuple[list[list[str]], bool]:
    """The rows of a table (commas, semicolons or tabs; a header line skipped) and whether its numbers may use a
    decimal comma (any delimiter but the comma)."""
    text = Path(path).read_text(encoding="utf-8-sig")
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    delimiter = "\t" if "\t" in first else (";" if ";" in first else ",")
    rows = [r for r in csv.reader(text.splitlines(), delimiter=delimiter) if any(c.strip() for c in r)]
    if rows:
        try:
            _number(rows[0][1], delimiter != ",")
        except (ValueError, IndexError):
            rows = rows[1:]  # a header
    return rows, delimiter != ","


def dem_height(chunk: "Chunk", x: float, y: float) -> float:
    """The height of the chunk's DEM at world ``x, y`` (the chunk's system): the z of a point given without one.
    ValueError (saying why) when the chunk has no DEM, its DEM was built in another system of the chunk, or it has
    no height there."""
    from ..core import stamps
    from ..core.free_drawings import raster_values

    if chunk.dem_path is None or not chunk.dem_path.exists():
        raise ValueError(f"no z and the chunk {chunk.label!r} has no DEM to read it from")
    if stamps.is_stale(chunk, "dem"):
        raise ValueError(f"no z and the DEM of {chunk.label!r} was built in another system of the chunk: rebuild it "
                         "first")
    (z,) = raster_values(chunk.dem_path, [(x, y)])
    if z is None:
        raise ValueError(f"no z and the DEM of {chunk.label!r} has no height at {x:.3f}, {y:.3f}")
    return float(z)


def world_to_internal(chunk: "Chunk", xyz) -> np.ndarray:
    """World coordinates of ``chunk`` (its system, its transform now) to internal; ``xyz[2]`` None -> read from the
    chunk's DEM (:func:`dem_height`, ValueError when it cannot be)."""
    x, y, z = (list(xyz) + [None])[:3]
    if x is None or y is None:
        raise ValueError("x and y are needed")
    if z is None:
        z = dem_height(chunk, float(x), float(y))
    return chunk.to_internal(np.array([[float(x), float(y), float(z)]], float))[0]


def import_ties(doc: "Document", path) -> list[Tie]:
    """Ties from a table: ``chunk_a,x_a,y_a,z_a,chunk_b,x_b,y_b,z_b[,precision][,name]`` (commas, or semicolons or tabs
    with decimal commas allowed; a header line is skipped). Chunks by label (or key), points in the world coordinates
    of each chunk, converted to internal; an empty ``z`` is read from the chunk's DEM. A row with ``chunk_b`` empty is
    a point of ``chunk_a`` with known coordinates ``x_b, y_b, z_b`` (:func:`add_known`). Precision default
    :data:`TABLE_PRECISION`. The table is read whole first: a ValueError naming the row, and nothing added, when a
    row is wrong."""
    rows, comma = _rows(Path(path))
    planned = []
    for i, row in enumerate(rows, start=1):
        cells = [c.strip() for c in row] + [""] * 10
        try:
            ca = _chunk(doc, cells[0])
            cb = _chunk(doc, cells[4]) if cells[4] else None
        except (KeyError, ValueError) as exc:
            raise ValueError(f"row {i}: {exc}") from None
        try:
            xyz_a = [_number(c, comma) for c in cells[1:4]]
            xyz_b = [_number(c, comma) for c in cells[5:8]]
            precision = _number(cells[8], comma)
        except ValueError:
            raise ValueError(f"row {i}: a coordinate or the precision is not a number") from None
        if precision is not None and not precision > 0:
            raise ValueError(f"row {i}: the precision must be positive")
        ends = []
        for chunk, xyz in ((ca, xyz_a), (cb, xyz_b)):
            if xyz[0] is None or xyz[1] is None:
                raise ValueError(f"row {i}: x and y are needed")
            if xyz[2] is None:
                if chunk is None:
                    raise ValueError(f"row {i}: the known point needs its z")
                try:
                    xyz = [xyz[0], xyz[1], dem_height(chunk, xyz[0], xyz[1])]
                except ValueError as exc:
                    raise ValueError(f"row {i}: {exc}") from None
            ends.append(np.asarray(xyz, float))
        if cb is ca:
            raise ValueError(f"row {i}: a tie links two different chunks")
        planned.append((ca, ends[0], cb, ends[1], precision, cells[9] or None))
    made = []
    for ca, wa, cb, wb, precision, name in planned:
        pa = ca.to_internal(wa[None])[0]
        if cb is None:
            made.append(add_known(doc, ca, pa, wb, name=name, precision=precision or TABLE_PRECISION))
        else:
            pb = cb.to_internal(wb[None])[0]
            made.append(add_tie(doc, ca, pa, cb, pb, name=name, precision=precision or TABLE_PRECISION))
    return made


# --------------------------------------------------------------------------- the joint solve
KINDS = {"similarity": 7, "rigid": 6, "plan": 4, "shift": 3, "height": 1, "fixed": 0}
HUBER = 2.0  # robust solve: an observation whose residual is above HUBER x its precision weighs HUBER / that ratio
RANK_RATIO = 1e-6  # an unknown the ties do not determine: a singular value of the Jacobian (columns scaled to one)
# below this x the largest
LOG_SCALE_LIMIT = float(np.log(10.0))  # the scale a solve may change, from the first position: x 0.1 to x 10
PRECISION_FACTOR = 3.0  # a chunk placed worse than this x the median precision of its ties: the geometry, not the
# measures, decides its position (ties nearly on a line, all on one side): a warning
TIED_POINTS = "the points of a chunk tied to another"
TIED_MARKERS = "the known points of tied chunks"


# --------------------------------------------------------------------------- messages of the solve and of the checks
# code -> (Italian template, English template). A message is a dict (``message``) that keeps both and its values, so
# the report stays data: the window shows ``tr(m["it"], *m["args"])`` (each Italian template is a format of
# gui/translations/en.json with this English text), the command line ``render(m)``.
MESSAGES: dict[str, tuple[str, str]] = {
    # the solve
    "approximated": ("{0}: {1} -> {2} approssimato entro {3:.3f} m",
                     "{0}: {1} -> {2} approximated within {3:.3f} m"),
    "not_levelled": ("{0}: un sistema locale non ancora livellato - costruire il suo DEM lo ruoterà (e non i chunk "
                     "legati a esso): livellalo prima (costruisci il suo DEM), poi rifai la stima",
                     "{0}: a local frame not levelled yet - building its DEM will turn it (and not the chunks tied "
                     "to it): level it first (build its DEM), then solve"),
    "local_references": ("{0}: i riferimenti dei suoi marker sono in {1}; restano come sono e non tornano più una "
                         "volta in {2}",
                         "{0}: its marker references are in {1}; they stay as they are and no longer match once it "
                         "is in {2}"),
    "tilt_scale_kept": ("{0}: da {1} a {2} con incognite senza inclinazione e scala: inclinazione e scala restano "
                        "quelle del suo sistema",
                        "{0}: from {1} to {2} with unknowns without tilt and scale: its tilt and scale stay those of "
                        "its own frame"),
    "robust_unsettled": ("I pesi robusti non si sono stabilizzati in 30 giri: il risultato può ancora cambiare",
                         "The robust weights did not settle in 30 rounds: the result may still move"),
    "precision_spread": ("{0}: posizionato con una precisione di {1:.3f} m al bordo del chunk, oltre {2:g} volte i "
                         "suoi legami ({3:.3f} m): i legami sono quasi su una linea o tutti da una parte - aggiungi "
                         "legami lontani dagli altri",
                         "{0}: placed with a precision of {1:.3f} m at the edge of the chunk, over {2:g} x its ties "
                         "({3:.3f} m): the ties are nearly on a line or all on one side - add ties far from the "
                         "others"),
    "precision_residuals": ("{0}: posizionato con una precisione di {1:.3f} m al bordo del chunk, oltre {2:g} volte "
                            "i suoi legami ({3:.3f} m): i legami non tornano fra loro (sigma0 {4:.1f}) - cerca prima "
                            "un legame sbagliato",
                            "{0}: placed with a precision of {1:.3f} m at the edge of the chunk, over {2:g} x its "
                            "ties ({3:.3f} m): the ties do not agree with each other (sigma0 {4:.1f}) - look for a "
                            "wrong tie first"),
    "precision_inherited": ("{0}: posizionato con una precisione di {1:.3f} m al bordo del chunk, oltre {2:g} volte "
                            "i suoi legami ({3:.3f} m): i suoi legami sono ben distribuiti (da soli {4:.3f} m), "
                            "l'errore arriva da {5}, a cui è appeso - lega {0} o {5} a un chunk fisso o a punti noti",
                            "{0}: placed with a precision of {1:.3f} m at the edge of the chunk, over {2:g} x its "
                            "ties ({3:.3f} m): its own ties are well spread ({4:.3f} m by themselves), the error "
                            "comes through {5}, which it hangs on - tie {0} or {5} to a fixed chunk or to known "
                            "points"),
    # plausibility (a chunk georeferenced before the solve)
    "implausible_rotation": ("{0}: ruotato di {1:.1f}° rispetto a prima, oltre {2:g}° per un chunk georeferenziato "
                             "({3}): controlla i legami prima di applicare",
                             "{0}: turned by {1:.1f}° from before, over {2:g}° for a georeferenced chunk ({3}): check "
                             "the ties before applying"),
    "implausible_scale": ("{0}: scala {1:.4f} rispetto a prima, fuori da {2:g}-{3:g} per un chunk georeferenziato "
                          "({4}): controlla i legami prima di applicare",
                          "{0}: scale {1:.4f} from before, outside {2:g}-{3:g} for a georeferenced chunk ({4}): check "
                          "the ties before applying"),
    # the report
    "no_check": ("Nessun controllo: gradi di libertà in avanzo 0, un legame sbagliato non si vede - aggiungi legami "
                 "o punti noti prima di applicare",
                 "No check at all: redundancy 0, a wrong tie cannot be seen - add ties or known points before "
                 "applying"),
    "residual_large": ("Legame «{0}»: residuo {1:.3f} m, {2:.1f} volte la sua precisione",
                       "Tie «{0}»: residual {1:.3f} m, {2:.1f} x its precision"),
    "distance_suspect": ("Legame «{0}» probabilmente sbagliato: {1} delle sue {2} coppie di distanze non tornano",
                         "Tie «{0}» probably wrong: {1} of its {2} pairs of distances do not agree"),
    "distance_failed": ("Distanze fra legami: {0} coppie su {1} non tornano entro la tolleranza",
                        "Distances between ties: {0} of {1} pairs do not agree within the tolerance"),
    "loo_large": ("Legame «{0}» tolto: gli altri mettono i suoi estremi a {1:.3f} m l'uno dall'altro (precisione "
                  "{2:.3f} m)",
                  "Tie «{0}» left out: the others put its ends {1:.3f} m apart (precision {2:.3f} m)"),
    "axis_bent": ("Asse dritto: {0} e {1} formano un angolo di {2:.1f}° (in pianta {3:.1f}°, in quota {4:.1f}°)",
                  "Straight axis: {0} and {1} meet at {2:.1f}° (in plan {3:.1f}°, in height {4:.1f}°)"),
    "out_no_such_tie": ("Escluso «{0}»: nessun legame con questo identificativo", "Left out «{0}»: no such tie"),
    "out_broken": ("Escluso «{0}»: un estremo non c'è più", "Left out «{0}»: an end is gone"),
    "out_chunk_removed": ("Escluso «{0}»: il chunk {1} non c'è più", "Left out «{0}»: chunk {1} removed"),
    "out_marker_removed": ("Escluso «{0}»: il marker {1} del chunk {2} è stato tolto o sostituito",
                           "Left out «{0}»: marker {1} of chunk {2} removed or replaced"),
    "out_reference_cleared": ("Escluso «{0}»: il marker {1} del chunk {2} non ha più un riferimento",
                              "Left out «{0}»: marker {1} of chunk {2} has no reference any more"),
    "out_inactive": ("Escluso «{0}»: non attivo", "Left out «{0}»: inactive"),
    "out_outside": ("Escluso «{0}»: il chunk {1} non è nella stima", "Left out «{0}»: chunk {1} not in the solve"),
    "out_no_position": ("Escluso «{0}»: il marker {1} del chunk {2} non ha una posizione",
                        "Left out «{0}»: marker {1} of chunk {2} has no position"),
    "out_reference_disabled": ("Escluso «{0}»: il suo riferimento è disattivato",
                               "Left out «{0}»: its reference is disabled"),
    "out_reference_no_position": ("Escluso «{0}»: riferimento del chunk {1} senza posizione",
                                  "Left out «{0}»: reference of chunk {1} without a position"),
    "out_reference_other_system": ("Escluso «{0}»: riferimento del chunk {1} in {2}, non in {3}",
                                   "Left out «{0}»: reference of chunk {1} in {2}, not in {3}"),
    "out_check_other_system": ("Escluso «{0}»: punto di verifica del chunk {1} in {2}, non in {3}",
                               "Left out «{0}»: check point of chunk {1} in {2}, not in {3}"),
    "distances_no_scale": ("Distanze fra i legami di {0} e {1} non confrontabili: un chunk non ha una scala in metri "
                           "- servono almeno {2} legami",
                           "Distances between the ties of {0} and {1} cannot be compared: a chunk has no metric "
                           "scale - at least {2} ties are needed"),
    "distances_no_scale_known": ("Distanze fra i punti noti di {0} non confrontabili: il chunk non ha una scala in "
                                 "metri - servono almeno {1} punti",
                                 "Distances between the known points of {0} cannot be compared: the chunk has no "
                                 "metric scale - at least {1} points are needed"),
    "distances_degrees": ("Distanze fra i legami di {0} non confrontabili: coordinate in gradi ({1})",
                          "Distances between the ties of {0} cannot be compared: coordinates in degrees ({1})"),
    "distance_scale": ("Le distanze in {1} sono {2:.4f} volte quelle in {0}: le scale dei due chunk differiscono",
                       "Distances in {1} are {2:.4f} times those in {0}: the scales of the two chunks differ"),
    "distance_scale_known": ("Le distanze fra le coordinate note sono {1:.4f} volte quelle in {0}: la scala del chunk "
                             "non è quella dei punti noti",
                             "Distances between the known coordinates are {1:.4f} times those in {0}: the chunk's "
                             "scale is not the known points'"),
    # lines of the report text
    "line_system": ("Sistema della stima: {0}", "System of the solve: {0}"),
    "line_redundancy": ("Gradi di libertà in avanzo: {0}", "Redundancy: {0}"),
    "line_fit": ("Scarto quadratico medio {0:.3f} m, sigma0 {1:.2f}", "RMS {0:.3f} m, sigma0 {1:.2f}"),
    "line_fit_no_sigma": ("Scarto quadratico medio {0:.3f} m", "RMS {0:.3f} m"),
    "line_chunk": ("Chunk {0} ({1}): rotazione {2:.2f}°, spostamento {3:.3f} m, scala {4:.5f}, precisione {5:.3f} m",
                   "Chunk {0} ({1}): rotation {2:.2f}°, shift {3:.3f} m, scale {4:.5f}, precision {5:.3f} m"),
    "line_residual": ("{0} ({1}): in pianta {2:.3f} m, in quota {3:+.3f} m, precisione {4:.3f} m, se tolto {5}",
                      "{0} ({1}): plan {2:.3f} m, height {3:+.3f} m, precision {4:.3f} m, if left out {5}"),
    "line_distances": ("Distanze fra coppie di legami: {0} su {1} entro la tolleranza",
                       "Distances between pairs of ties: {0} of {1} within the tolerance"),
    "line_axis": ("Asse dritto: angolo massimo {0:.2f}° (prima {1:.2f}°)",
                  "Straight axis: largest angle {0:.2f}° (before {1:.2f}°)"),
    "line_axis_no_before": ("Asse dritto: angolo massimo {0:.2f}°", "Straight axis: largest angle {0:.2f}°"),
    "heading_residuals": ("Residui", "Residuals"),
    "heading_chunks": ("Chunk della stima", "Chunks of the solve"),
    "heading_warnings": ("Avvisi", "Warnings"),
    "kind_tie": ("legame", "tie"),
    "kind_known": ("punto noto", "known point"),
    "kind_reference": ("riferimento", "reference"),
    "kind_check": ("verifica", "check"),
    "no_value": ("-", "-"),
    "unknowns_similarity": ("similitudine", "similarity"),
    "unknowns_rigid": ("rigido", "rigid"),
    "unknowns_plan": ("in pianta", "in plan"),
    "unknowns_shift": ("solo spostamento", "shift only"),
    "unknowns_height": ("solo quota", "height only"),
    "unknowns_fixed": ("fisso", "fixed"),
}


def message(code: str, *args, chunk: int | None = None) -> dict:
    """A message of the solve or of the checks: ``{"code", "it", "en", "args", "chunk"}`` (both templates and the
    values, JSON; ``chunk`` the key of the chunk it is about)."""
    it, en = MESSAGES[code]
    plain = [a.item() if isinstance(a, np.generic) else a for a in args]
    return {"code": code, "it": it, "en": en, "args": plain, "chunk": chunk}


class _Comma(float):
    """A number written with the decimal comma (Italian) by any format spec."""

    def __format__(self, spec: str) -> str:
        text = super().__format__(spec)
        return text.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


Comma = _Comma  # public name: the window writes the numbers of a message with the decimal comma too


def render(m: dict, language: str = "en") -> str:
    """A message (:func:`message`) as text: English, or Italian (``"it"``, decimal comma)."""
    template = m["it"] if language == "it" else m["en"]
    args = [_Comma(a) if language == "it" and isinstance(a, float) else a for a in m["args"]]
    return template.format(*args) if args else template


def _text(code: str, language: str, *args) -> str:
    return render(message(code, *args), language)


def _out(tie_id: str | None, name: str, *parts, reason: str) -> dict:
    """An entry of ``Solution.left_out``: ``id``, ``name``, ``reason`` (English, for the command line) and
    ``messages`` (one :func:`message` per reason, the name of the tie - or its id - first)."""
    label = name or tie_id or ""
    return {"id": tie_id, "name": name, "reason": reason,
            "messages": [message(code, label, *args) for code, *args in parts]}


class TieError(ValueError):
    """A solve that cannot be made: ``str()`` is the English message, ``italian`` the Italian one (the window shows
    the Italian one, the command line the English one)."""

    def __init__(self, english: str, italian: str):
        super().__init__(english)
        self.english = english
        self.italian = italian


def _not_same_frame(a_label, a_crs, b_label, b_crs) -> TieError:
    """The words of :func:`..engine.chunks._frame_to` (merging chunks in a local frame and in a projected system)."""
    return TieError(f"chunk {a_label} ({a_crs or 'LOCAL'}) and {b_label} ({b_crs or 'LOCAL'}) are not in the same frame: "
                    "a local frame and a projected system cannot be mixed in one solve (fix the chunk in the other "
                    "system, or leave its known points out)",
                    f"il chunk {a_label} ({a_crs or 'LOCAL'}) e {b_label} ({b_crs or 'LOCAL'}) non sono nello stesso "
                    "sistema: un sistema locale e uno proiettato non si mescolano in una stima (fissa il chunk "
                    "nell'altro sistema, o lascia fuori i suoi punti noti)")


@dataclass
class _Obs:
    """One observation of the solve: the end ``(chunk key, internal xyz)`` against the other end or against known
    world coordinates (``target``, in the system of the solve), with a precision per axis."""

    kind: str  # "tie" | "known" | "reference"
    name: str
    tie_id: str | None
    a: tuple[int, np.ndarray]
    b: tuple[int, np.ndarray] | None
    target: np.ndarray | None
    sigma: np.ndarray
    check: bool = False  # a check point: reported, never fitted


@dataclass
class Solution:
    """What :func:`solve` found; nothing applied (:func:`apply`).

    ``transforms``: the new ``chunk.transform`` of every chunk of the solve (the fixed ones as they are);
    ``residuals``: one entry per observation - ``id`` (tie id, None for a reference), ``name``, ``kind`` ("tie",
    "known", "reference"), ``chunks``, ``dx``/``dy``/``dz``/``d`` (world units of the solve; tie = end A - end B, known
    point = computed - known), ``precision``, ``normalised`` (the residual over its precision), ``weight`` (< 1 when
    the robust solve weighs it down), ``fitted`` (False: reported only - observations between fixed chunks, check
    points); ``redundancy``: equations - unknowns of the fitted observations (0: no check at all); ``rms`` of the fitted
    residuals; ``per_chunk``: ``kind``, ``label``, ``rotation_deg``, ``shift`` (how far its centre moves), ``scale``,
    ``crs_before``, ``observations``; ``crs``: the system of the solve (the fixed chunks', else the known points');
    ``left_out``: ``[{"id", "name", "reason", "messages"}]`` the ties not used (``reason`` in English,
    ``messages`` the same as :func:`message`, one per reason); ``messages``: the warnings as structured
    messages (:func:`message`, Italian and English), ``warnings`` the same in English; ``previous``: the transform and CRS of
    each chunk when it was solved (:func:`apply` refuses when they changed)."""

    transforms: dict[int, np.ndarray]
    residuals: list[dict]
    redundancy: int
    rms: float
    per_chunk: dict[int, dict]
    crs: str | None
    unknowns: dict[int, str]
    left_out: list[dict]
    warnings: list[str]
    previous: dict[int, dict]
    robust: bool = False
    sigma0: float | None = None
    converged: bool = True
    irls_rounds: int = 0
    messages: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"transforms": {str(k): np.asarray(v, float).tolist() for k, v in self.transforms.items()},
                "residuals": self.residuals, "redundancy": int(self.redundancy), "rms": float(self.rms),
                "sigma0": None if self.sigma0 is None else float(self.sigma0),
                "per_chunk": {str(k): v for k, v in self.per_chunk.items()}, "crs": self.crs,
                "unknowns": {str(k): v for k, v in self.unknowns.items()}, "left_out": self.left_out,
                "warnings": list(self.warnings), "messages": self.messages, "robust": bool(self.robust),
                "converged": bool(self.converged),
                "irls_rounds": int(self.irls_rounds)}


def _translate(t) -> np.ndarray:
    m = np.eye(4)
    m[:3, 3] = t
    return m


def _rot_z(theta: float) -> np.ndarray:
    c, s = np.cos(theta), np.sin(theta)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def _correction(kind: str, x: np.ndarray, g: np.ndarray) -> np.ndarray:
    """The 4x4 change of a chunk in the local frame of the solve: ``x' = s R (x - g) + g + t`` (``g`` its centre)."""
    from .chunks import _rodrigues

    R, s, t = np.eye(3), 1.0, np.zeros(3)
    if kind in ("similarity", "rigid"):
        R, t = _rodrigues(x[:3]), x[3:6]
        if kind == "similarity":
            s = float(np.exp(np.clip(x[6], -LOG_SCALE_LIMIT, LOG_SCALE_LIMIT)))
    elif kind == "plan":
        R, t = _rot_z(x[0]), x[1:4]
    elif kind == "shift":
        t = x[:3]
    elif kind == "height":
        t = np.array([0.0, 0.0, x[0]])
    m = np.eye(4)
    m[:3, :3] = s * R
    m[:3, 3] = g + t - s * R @ g
    return m


def _initial(kind: str, src: np.ndarray, dst: np.ndarray, w: np.ndarray) -> np.ndarray:
    """A closed-form first position of a chunk for its kind of unknowns (src -> dst, local frame of the solve), so
    that the least squares start close even for a chunk in a local frame put into a projected system."""
    from .chunks import _spread

    w = w / w.sum()
    if kind in ("similarity", "rigid") and len(src) >= 3 and _spread(src):
        return geometry.umeyama(src, dst, with_scale=kind == "similarity", weights=w)
    if kind in ("similarity", "rigid", "plan"):
        cs, cd = w @ src, w @ dst
        a, b = src[:, :2] - cs[:2], dst[:, :2] - cd[:2]
        theta = float(np.arctan2(np.sum(w * (a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0])),
                                 np.sum(w * (a[:, 0] * b[:, 0] + a[:, 1] * b[:, 1])))) if len(src) > 1 else 0.0
        m = np.eye(4)
        m[:3, :3] = _rot_z(theta)
        m[:3, 3] = cd - m[:3, :3] @ cs
        return m
    d = w @ (dst - src)
    if kind == "height":
        d = np.array([0.0, 0.0, d[2]])
    return _translate(d) if kind == "shift" or kind == "height" else np.eye(4)


def _rotation_deg(m: np.ndarray) -> float:
    a = np.asarray(m, float)[:3, :3]
    s = float(np.cbrt(np.linalg.det(a)))
    c = (np.trace(a / s) - 1.0) / 2.0
    return float(np.degrees(np.arccos(np.clip(c, -1.0, 1.0))))


def _sample_points(chunk: "Chunk") -> np.ndarray:
    """Internal points spread over a chunk (its cameras, markers, tie points and a box around them) to carry it from
    one projected system to another as a similarity (as merging chunks does)."""
    pts = [c.center for c in chunk.aligned_cameras] + [m.position for m in chunk.markers if m.position is not None]
    tp = chunk.tie_points
    if tp is not None and len(tp):
        pts += list(tp.xyz[np.random.default_rng(0).choice(len(tp), min(len(tp), 2000), replace=False)])
    pts = np.array(pts, float).reshape(-1, 3)
    centre = pts.mean(0) if len(pts) else np.zeros(3)
    size = max(float(np.ptp(pts, axis=0).max()) if len(pts) else 0.0, 10.0 / max(float(chunk.scale), 1e-12))
    box = centre + size / 2 * np.array([[x, y, z] for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)], float)
    return np.vstack([pts, box])


def _compatible(a: str | None, b: str | None) -> bool:
    from ..core import crs as crs_mod

    return a == b or (crs_mod.is_local(a) and crs_mod.is_local(b)) or not (crs_mod.is_local(a) or crs_mod.is_local(b))


def _to_system(points: np.ndarray, src: str | None, dst: str | None, chunk, what: str) -> np.ndarray:
    from ..core import crs as crs_mod
    from ..gnss.frames import Legacy

    if src == dst or (crs_mod.is_local(src) and crs_mod.is_local(dst)):
        return np.asarray(points, float)
    return crs_mod.transform_points(np.asarray(points, float), src, dst, legacy=Legacy(what, chunk))


def _sigma3(values, fallback: float) -> np.ndarray:
    v = np.asarray(values if values else [fallback], float).reshape(-1)
    v = np.resize(v, 3) if len(v) != 3 else v
    v = np.where(np.isfinite(v) & (v > 0), v, fallback)
    return v


def solve(doc: "Document", unknowns: dict, ties=None, references: bool = True, robust: bool = False,
          check_ties=()) -> Solution:
    """Several chunks placed together from their ties, one least squares; nothing is changed (:func:`apply`).

    ``unknowns``: chunk (object, key or label) -> ``"similarity"`` (rotation, shift, scale: 7), ``"rigid"`` (6),
    ``"plan"`` (rotation about the vertical of the world system, shift in x, y, z: 4; tilt and scale stay),
    ``"shift"`` (3), ``"height"`` (1) or ``"fixed"`` (0); only these chunks take part. ``ties``: Tie objects or ids
    (default every tie of the document); inactive or broken ties, ties with an end that has no position and ties to a
    chunk outside the solve are left out and listed in ``Solution.left_out``.

    Known points: a tie towards known coordinates always counts (its marker's reference, per axis); with
    ``references`` the control markers of the chunks (as for the georeference) count too, as ground control points,
    except the markers of known ties (counted once, through the tie). A tie between two markers that both have a
    reference counts as the tie, and each reference once. Check points are reported, never fitted; observations that
    move nothing (between fixed chunks) are reported only.

    The system of the solve is the fixed chunks' (the first fixed chunk in the document), else the known points'.
    As *Allinea chunk* does, a moving chunk in another system goes into that one (its CRS becomes it on apply); between
    two projected systems points and known coordinates are converted; a local frame and a projected system are never
    mixed where both must hold - two fixed chunks, or a known point and the system of the solve - (:class:`TieError`).
    Weights ``1 / precision**2``; ``robust``: Huber weights (:data:`HUBER`) found again until they settle.
    First positions in closed form from the fixed chunks outwards (the chunk with most placed observations first, then
    every chunk again from all its observations), so a chunk turned any way finds its place; the scale may change by
    x0.1-x10 from there. ``per_chunk[...]["precision_m"]``: the largest standard deviation of the corners of the chunk's
    extent from the covariance of its unknowns (a-priori precisions, times sigma0^2 when that is above 1); over
    :data:`PRECISION_FACTOR` x the median precision of its ties (``precision_limit``) a warning names the cause
    (``precision_cause``): ``"residuals"`` when the ties do not agree with each other (the precision is large only
    because sigma0 scales it: look for a wrong tie), ``"spread"`` when the chunk's own ties place it badly (``precision_own_m``: the chunks it is
    tied to held where they are - ties nearly on a line or on one side), ``"inherited"`` when they place it well and
    the error comes through the moving chunks it hangs on (``precision_through``: tie those, or this one, to a fixed
    chunk or to known points).

    :class:`TieError`: an unknown kind; nothing fixes the system (no fixed chunk, no known point); a chunk with fewer
    equations than unknowns ("similarity" with 2 ties); unknowns the ties do not determine (points on a line, chunks
    tied only among themselves); a solve that does not converge; chunks in systems that cannot be mixed; a geographic
    system.

    ``check_ties``: ids of ties reported as check points only, never fitted (:func:`leave_one_out`)."""
    from scipy.optimize import least_squares

    from ..core import crs as crs_mod

    chunks: dict[int, "Chunk"] = {}
    kinds: dict[int, str] = {}
    for item, kind in unknowns.items():
        try:
            ch = _chunk(doc, item)
        except (KeyError, ValueError) as exc:
            raise TieError(f"unknown chunk: {exc}", f"chunk sconosciuto: {exc}") from None
        if kind not in KINDS:
            raise TieError(f"chunk {ch.label}: unknowns {kind!r}: use one of {', '.join(KINDS)}",
                           f"chunk {ch.label}: incognite {kind!r}: usa una di {', '.join(KINDS)}")
        chunks[ch.key], kinds[ch.key] = ch, kind
    order = [c.key for c in doc.chunks if c.key in chunks]
    if not order:
        raise TieError("no chunk in the solve", "nessun chunk nella stima")

    # the ties and the observations they give (internal points of their ends)
    wanted = None if ties is None else {t if isinstance(t, str) else t.id for t in ties}
    every_tie = ties_of(doc)
    if wanted is not None:
        ids = {t.id for t in every_tie}
        missing_ids = [_out(i, "", ("out_no_such_tie",), reason="no such tie") for i in sorted(wanted - ids)]
    # the reference of a known tie's marker counts through the tie only: never also as a reference, even when the
    # tie is off, broken or not among ``ties``
    known_ends = {t.a for t in every_tie if t.b is None}
    left_out, observations = ([] if wanted is None else missing_ids), []
    for t in every_tie:
        if wanted is not None and t.id not in wanted:
            continue
        if t.broken:
            left_out.append(_out(t.id, t.name, *(t.broken_parts or [("out_broken",)]), reason=t.broken))
            continue
        if not t.active:
            left_out.append(_out(t.id, t.name, ("out_inactive",), reason="inactive"))
            continue
        outside = [doc_chunk.label for k, _ in t.ends() if k not in chunks
                   for doc_chunk in doc.chunks if doc_chunk.key == k]
        if outside:
            left_out.append(_out(t.id, t.name, ("out_outside", ", ".join(outside)),
                                 reason=f"chunk {', '.join(outside)} not in the solve"))
            continue
        points, missing = [], []
        for end, uid in t.end_uids():
            chunk, marker = resolve_end(doc, end, uid)
            p = end_internal(chunk, marker)
            if p is None:
                missing.append((marker.label, chunk.label))
            points.append((chunk.key, p))
        if missing:
            left_out.append(_out(t.id, t.name, *[("out_no_position", m, c) for m, c in missing],
                                 reason="no position for " + ", ".join(f"marker {m!r} of chunk {c!r}"
                                                                      for m, c in missing)))
            continue
        if t.b is None:
            chunk, marker = resolve_end(doc, t.a, t.a_uid)
            if not marker.reference.enabled:  # switched off in the Riferimenti pane
                left_out.append(_out(t.id, t.name, ("out_reference_disabled",), reason="its reference is disabled"))
                continue
            sigma = _sigma3(marker.reference.accuracy, t.precision)
            observations.append(("known", t, points[0], None, np.asarray(t.known, float), t.crs, sigma,
                                 bool(marker.checkpoint) or t.id in check_ties, chunk))
        else:
            observations.append(("tie", t, points[0], points[1], None, None, np.full(3, float(t.precision)),
                                 t.id in check_ties, None))
    if references:
        for key in order:
            ch = chunks[key]
            for m in ch.markers:
                if (key, m.key) in known_ends or not (m.reference.enabled and m.reference.location):
                    continue
                p = end_internal(ch, m)
                if p is None:
                    left_out.append(_out(None, m.label, ("out_reference_no_position", ch.label),
                                         reason=f"reference of chunk {ch.label!r} without a position"))
                    continue
                observations.append(("reference", m, (key, p), None, np.asarray(m.reference.location, float), ch.crs,
                                     _sigma3(m.reference.accuracy, TABLE_PRECISION), bool(m.checkpoint), ch))

    # the system of the solve
    fixed = [k for k in order if kinds[k] == "fixed"]
    known_obs = [o for o in observations if o[0] != "tie" and not o[7]]
    if not fixed and not any(kinds[o[2][0]] != "fixed" for o in known_obs):
        raise TieError("nothing fixes the system of the solve: mark a chunk as fixed, or give known points "
                       "(GNSS points, benchmarks, references)",
                       "niente fissa il sistema della stima: segna un chunk come fisso, o dai dei punti noti "
                       "(punti GNSS, capisaldi, riferimenti)")
    if fixed:
        system_chunk = chunks[fixed[0]]
        system = system_chunk.crs
        for k in fixed[1:]:
            if not _compatible(chunks[k].crs, system):
                raise _not_same_frame(chunks[k].label, chunks[k].crs, system_chunk.label, system)
    else:
        first = next(o for o in known_obs if kinds[o[2][0]] != "fixed")
        system_chunk, system = first[8], first[5]
    if crs_mod.is_geographic(system):
        raise TieError(f"the system {system} is geographic; use a projected system (e.g. the UTM zone)",
                       f"il sistema {system} è geografico; usa un sistema proiettato (es. il fuso UTM)")
    for o in list(observations):
        if o[0] != "tie" and not _compatible(o[5], system):
            if o[0] == "known" and not o[7]:  # a known point the user tied explicitly: never dropped silently
                raise _not_same_frame(o[8].label, o[5], system_chunk.label, system)
            # a check point, or a reference of a chunk that leaves its own frame: it cannot be measured here
            observations.remove(o)
            what = "check point" if o[7] else "reference"
            left_out.append(_out(o[1].id if o[0] == "known" else None,
                                 o[1].name if o[0] == "known" else o[1].label,
                                 ("out_check_other_system" if o[7] else "out_reference_other_system", o[8].label,
                                  o[5] or "LOCAL", system or "LOCAL"),
                                 reason=f"{what} of chunk {o[8].label!r} in {o[5] or 'LOCAL'}, not in {system or 'LOCAL'}"))

    messages: list[dict] = []
    # each chunk: internal -> world of the solve, as it is now (a chunk in another projected system carried as a
    # similarity fitted through the projection; one in a local frame placed by the first position below)
    world_of: dict[int, np.ndarray] = {}
    for key in order:
        ch = chunks[key]
        T = np.asarray(ch.transform, float)
        if ch.crs == system or crs_mod.is_local(ch.crs) and crs_mod.is_local(system) or not _compatible(ch.crs, system):
            world_of[key] = T
            continue
        pts = _sample_points(ch)
        dst = _to_system(geometry.apply(T, pts), ch.crs, system, ch, TIED_POINTS)
        off = np.round(dst.mean(0))
        W = geometry.umeyama(pts, dst - off, with_scale=True)
        err = float(np.sqrt(np.mean(np.sum((geometry.apply(W, pts) - (dst - off)) ** 2, axis=1))))
        if err > 0.005:
            messages.append(message("approximated", ch.label, ch.crs, system, err, chunk=key))
        world_of[key] = _translate(off) @ W
    for key in order:
        ch = chunks[key]
        if kinds[key] == "fixed":
            if (crs_mod.is_local(ch.crs) and ch.aligned_cameras and not ch.meta.get("levelled")
                    and np.allclose(geometry.decompose_similarity(ch.transform)[1], np.eye(3))):
                messages.append(message("not_levelled", ch.label, chunk=key))
            continue
        if not _compatible(ch.crs, system):
            if any(m.reference.location for m in ch.markers):
                messages.append(message("local_references", ch.label, ch.crs or "LOCAL", system or "LOCAL", chunk=key))
            if kinds[key] in ("plan", "shift", "height"):
                messages.append(message("tilt_scale_kept", ch.label, ch.crs or "LOCAL", system or "LOCAL", chunk=key))

    # the observations in the local frame of the solve (world minus a rounded offset)
    obs: list[_Obs] = []
    for kind, item, a, b, target, crs, sigma, check, ch in observations:
        if target is not None:
            target = _to_system(target[None], crs, system, ch, TIED_MARKERS)[0]
        name = item.name if kind != "reference" else item.label
        obs.append(_Obs(kind, name, item.id if kind != "reference" else None, a, b, target, sigma, check))
    anchor = [o.target for o in obs if o.target is not None and not o.check]
    for o in obs:
        for end in (o.a, o.b):
            if end is not None and kinds[end[0]] == "fixed":
                anchor.append(geometry.apply(world_of[end[0]], end[1]))
    offset = np.round(np.median(np.array(anchor), axis=0)) if anchor else np.zeros(3)
    base = {k: _translate(-offset) @ world_of[k] for k in order}  # internal -> local frame of the solve

    def moving(o: _Obs) -> bool:
        return any(e is not None and kinds[e[0]] != "fixed" for e in (o.a, o.b))

    fitted = [o for o in obs if not o.check and moving(o)]
    # enough equations for every chunk
    for key in order:
        need = KINDS[kinds[key]]
        if not need:
            continue
        n = 3 * sum(1 for o in fitted for e in (o.a, o.b) if e is not None and e[0] == key)
        if n < need:
            raise TieError(f"chunk {chunks[key].label}: {n} equations for {need} unknowns ({kinds[key]}): not enough "
                           "ties - add ties or known points, or choose fewer unknowns (plan, shift, height)",
                           f"chunk {chunks[key].label}: {n} equazioni per {need} incognite ({kinds[key]}): legami "
                           "insufficienti - aggiungi legami o punti noti, o scegli meno incognite (in pianta, solo "
                           "spostamento, solo quota)")

    # first positions, from the fixed chunks outwards: each time the chunk with the most observations of placed points
    # (enough for a closed form of its unknowns first), then every chunk again from all its observations
    def pairs(key, placed):
        src, dst, w = [], [], []
        for o in fitted:
            ends = [e for e in (o.a, o.b) if e is not None]
            mine = [e for e in ends if e[0] == key]
            if not mine:
                continue
            if o.target is not None:
                there = o.target - offset
            else:
                other = next((e for e in ends if e[0] != key), None)
                if other is None or other[0] not in placed:
                    continue
                there = geometry.apply(base[other[0]], other[1])
            src.append(geometry.apply(base[key], mine[0][1]))
            dst.append(there)
            w.append(1.0 / float(np.mean(o.sigma ** 2)))
        return np.array(src).reshape(-1, 3), np.array(dst).reshape(-1, 3), np.array(w)

    def enough(kind, src) -> bool:
        from .chunks import _spread

        return {"similarity": len(src) >= 3 and _spread(src), "rigid": len(src) >= 3 and _spread(src),
                "plan": len(src) >= 2}.get(kind, len(src) >= 1)

    placed = {k for k in order if kinds[k] == "fixed"}
    sequence = []
    while True:
        best = None
        for key in order:
            if key in placed:
                continue
            src, dst, w = pairs(key, placed)
            if len(src):
                score = (enough(kinds[key], src), len(src))
                if best is None or score > best[0]:
                    best = (score, key, src, dst, w)
        if best is None:
            break
        _score, key, src, dst, w = best
        base[key] = _initial(kinds[key], src, dst, w) @ base[key]
        placed.add(key)
        sequence.append(key)
    for _again in range(2):
        for key in sequence:
            src, dst, w = pairs(key, placed - {key})
            if len(src):
                base[key] = _initial(kinds[key], src, dst, w) @ base[key]

    # the unknowns
    slices, centre, start = {}, {}, 0
    for key in order:
        n = KINDS[kinds[key]]
        slices[key] = slice(start, start + n)
        start += n
        pts = [geometry.apply(base[key], e[1]) for o in fitted for e in (o.a, o.b) if e is not None and e[0] == key]
        centre[key] = np.mean(pts, axis=0) if pts else np.zeros(3)
    n_par = start
    qa = np.array([geometry.apply(base[o.a[0]], o.a[1]) for o in fitted]).reshape(-1, 3)
    qb = np.array([geometry.apply(base[o.b[0]], o.b[1]) if o.b is not None else o.target - offset for o in fitted]).reshape(-1, 3)
    ka = [o.a[0] for o in fitted]
    kb = [o.b[0] if o.b is not None else None for o in fitted]
    sig = np.array([o.sigma for o in fitted]).reshape(-1, 3)

    def moved(x):
        return {k: _correction(kinds[k], x[slices[k]], centre[k]) for k in order}

    def positions(x):
        C = moved(x)
        pa = np.array([geometry.apply(C[k], q) for k, q in zip(ka, qa)]).reshape(-1, 3)
        pb = np.array([q if k is None else geometry.apply(C[k], q) for k, q in zip(kb, qb)]).reshape(-1, 3)
        return pa - pb

    weights = np.ones(len(fitted))

    def residuals(x):
        return ((positions(x) / sig) * np.sqrt(weights)[:, None]).ravel()

    x = np.zeros(n_par)
    lower, upper = np.full(n_par, -np.inf), np.full(n_par, np.inf)
    for key in order:
        if kinds[key] == "similarity":
            lower[slices[key].stop - 1], upper[slices[key].stop - 1] = -LOG_SCALE_LIMIT, LOG_SCALE_LIMIT
    result, rounds, settled, covariance = None, 0, True, np.zeros((n_par, n_par))
    normal, variance_factor = np.zeros((n_par, n_par)), 1.0
    if n_par:
        settled = not robust
        for rounds in range(1, 31 if robust else 2):
            result = least_squares(residuals, x, method="trf", x_scale="jac", xtol=1e-15, ftol=1e-15, gtol=1e-15,
                                   max_nfev=200 * (n_par + 1), bounds=(lower, upper))
            x = result.x
            if not robust:
                break
            norm = np.linalg.norm(positions(x) / sig, axis=1)
            new = np.where(norm > HUBER, HUBER / np.maximum(norm, 1e-300), 1.0)
            if np.allclose(new, weights, atol=1e-6):
                settled = True
                break
            weights = new
        at_bound = [chunks[k].label for k in order if kinds[k] == "similarity"
                    and abs(x[slices[k].stop - 1]) >= LOG_SCALE_LIMIT * (1 - 1e-6)]
        if result.status <= 0 or at_bound:
            why = (f"the scale of {', '.join(at_bound)} went beyond x0.1-x10" if at_bound
                   else "too many iterations")
            raise TieError(f"the solve did not converge ({why}): check the ties (a wrong pair, a chunk turned "
                           "upside down) or choose fewer unknowns",
                           f"la stima non converge ({'la scala di ' + ', '.join(at_bound) + ' è uscita da x0,1-x10' if at_bound else 'troppe iterazioni'}): "
                           "controlla i legami (una coppia sbagliata, un chunk capovolto) o scegli meno incognite")
        # unknowns the ties do not determine: SVD of the Jacobian with its columns scaled to one (rotations, shifts
        # and scales of large and small chunks alike)
        J = np.asarray(result.jac, float)
        norms = np.linalg.norm(J, axis=0)
        dead = np.zeros(n_par, bool)  # a column (nearly) zero next to the other columns of its chunk: an unknown
        for key in order:  # that moves nothing (a rotation about the line of the ties); scaled it would be noise
            sl = slices[key]
            if sl.stop > sl.start:
                dead[sl] = norms[sl] <= RANK_RATIO * norms[sl].max()
        Jn = J / np.where(dead, 1.0, norms)
        sv, vt = np.linalg.svd(Jn, full_matrices=True)[1:]
        top = sv[0] if len(sv) else 0.0
        rank = int(np.sum(sv > RANK_RATIO * top)) if top > 0 else 0
        null = vt[rank:]
        if len(null) or dead.any():
            loose = [chunks[k].label for k in order if KINDS[kinds[k]] and
                     ((len(null) and np.abs(null[:, slices[k]]).max() > 1e-3) or dead[slices[k]].any())]
            raise TieError(f"the ties do not determine chunk {', '.join(loose)}: points on a line, or chunks tied only "
                           "among themselves and not to a fixed chunk or known points",
                           f"i legami non determinano il chunk {', '.join(loose)}: punti su una linea, o chunk legati "
                           "solo fra loro e non a un chunk fisso o a punti noti")
        # covariance of the unknowns: (J^T J)^-1 with the a-priori precisions, scaled by sigma0^2 when the residuals
        # say the ties are worse than their precision (never made smaller by residuals better than it)
        vs = vt[:len(sv)]
        covariance = (vs.T / sv ** 2) @ vs / np.outer(norms, norms)
        normal = J.T @ J
        dof = 3 * len(fitted) - n_par
        if dof > 0:
            variance_factor = max(1.0, float(np.sum(result.fun ** 2) / dof))
            covariance *= variance_factor
    if robust and not settled:
        messages.append(message("robust_unsettled"))

    C = moved(x)
    final = {k: _translate(offset) @ C[k] @ base[k] for k in order}
    transforms = {k: (np.asarray(chunks[k].transform, float).copy() if kinds[k] == "fixed" else final[k])
                  for k in order}

    # residuals of every observation
    weight_of = {id(o): float(w) for o, w in zip(fitted, weights)}
    entries = []
    for o in obs:
        pa = geometry.apply(final[o.a[0]], o.a[1])
        pb = (geometry.apply(final[o.b[0]], o.b[1]) if o.b is not None else o.target)
        r = pa - pb
        entries.append({"id": o.tie_id, "name": o.name, "kind": o.kind,
                        "chunks": [o.a[0]] + ([o.b[0]] if o.b is not None else []),
                        "dx": float(r[0]), "dy": float(r[1]), "dz": float(r[2]), "d": float(np.linalg.norm(r)),
                        "precision": float(np.max(o.sigma)), "normalised": float(np.linalg.norm(r / o.sigma)),
                        "weight": weight_of.get(id(o), 1.0), "fitted": id(o) in weight_of,
                        "check": bool(o.check)})
    used = [e for e in entries if e["fitted"]]
    redundancy = 3 * len(used) - n_par
    rms = float(np.sqrt(np.mean([e["d"] ** 2 for e in used]))) if used else 0.0
    sigma0 = (float(np.sqrt(sum(e["weight"] * e["normalised"] ** 2 for e in used) / redundancy))
              if redundancy > 0 else None)

    def extent(key) -> np.ndarray:
        """Corners of a cube around a chunk's own points (cameras, markers, tie points, else its observations),
        its side the largest extent: in the local frame of the solve, where the unknowns move them."""
        own = _sample_points(chunks[key])[:-8]  # without the box of _sample_points
        if len(own) < 2:
            own = np.array([geometry.apply(world_of[key], e[1]) for o in fitted for e in (o.a, o.b)
                            if e is not None and e[0] == key]).reshape(-1, 3)
            own = geometry.apply(np.linalg.inv(world_of[key]), own) if len(own) else own
        if not len(own):
            return np.zeros((0, 3))
        pts = geometry.apply(base[key], own)
        lo, hi = pts.min(0), pts.max(0)
        half = max(float(np.max(hi - lo)) / 2.0, 1e-9)
        c = (lo + hi) / 2.0
        return c + half * np.array([[i, j, k] for i in (-1, 1) for j in (-1, 1) for k in (-1, 1)], float)

    def chunk_precision(key, own: bool = False) -> float:
        """The largest standard deviation (3D) of the corners of the chunk's extent moved by its unknowns; ``own``:
        with the chunks it is tied to held where they are (its block of the normal matrix alone)."""
        sl = slices[key]
        n = sl.stop - sl.start
        corners = extent(key)
        if not n or not len(corners):
            return 0.0
        cov = np.linalg.pinv(normal[sl, sl]) * variance_factor if own else covariance[sl, sl]
        here = geometry.apply(_correction(kinds[key], x[sl], centre[key]), corners)
        G = np.zeros((len(corners), 3, n))
        for j in range(n):
            step = np.zeros(n)
            step[j] = 1e-7
            G[:, :, j] = (geometry.apply(_correction(kinds[key], x[sl] + step, centre[key]), corners) - here) / 1e-7
        var = np.einsum("pij,jk,pik->p", G, cov, G)
        return float(np.sqrt(max(0.0, var.max())))

    per_chunk, previous = {}, {}
    for key in order:
        ch = chunks[key]
        previous[key] = {"transform": np.asarray(ch.transform, float).copy(), "crs": ch.crs}
        D = final[key] @ np.linalg.inv(world_of[key]) if kinds[key] != "fixed" else np.eye(4)
        g = geometry.apply(world_of[key], _sample_points(ch).mean(0)) if kinds[key] != "fixed" else np.zeros(3)
        n = sum(1 for o in fitted for e in (o.a, o.b) if e is not None and e[0] == key)
        per_chunk[key] = {"label": ch.label, "kind": kinds[key], "rotation_deg": _rotation_deg(D) if n else 0.0,
                          "shift": float(np.linalg.norm(geometry.apply(D, g) - g)),
                          "scale": float(np.cbrt(np.linalg.det(D[:3, :3]))), "crs_before": ch.crs,
                          "crs": ch.crs if kinds[key] == "fixed" else system, "observations": n}
        if kinds[key] == "fixed":
            per_chunk[key]["rotation_deg"] = 0.0
        sigmas = [float(np.max(o.sigma)) for o in fitted for e in (o.a, o.b) if e is not None and e[0] == key]
        precision = chunk_precision(key)
        own = chunk_precision(key, own=True)
        limit = PRECISION_FACTOR * float(np.median(sigmas)) if sigmas else 0.0
        through = [k for k in order if k != key and kinds[k] != "fixed" and any(
            o.b is not None and {o.a[0], o.b[0]} == {key, k} for o in fitted)]
        cause = None
        if kinds[key] != "fixed" and precision > limit > 0:
            # the cause, from the geometry alone (the precisions before the residuals scale them): the ties do not
            # agree with each other (a wrong tie); its own ties alone (the chunks it is tied to held where they are)
            # place it well and the error comes through the chunks it hangs on; or its own ties are badly placed
            spread = np.sqrt(variance_factor)
            if precision / spread <= limit:
                cause = "residuals"
            elif own / spread <= limit and through:
                cause = "inherited"
            else:
                cause = "spread"
        per_chunk[key].update({"precision_m": precision, "precision_limit": limit, "precision_own_m": own,
                               "precision_through": through, "precision_cause": cause})
        if cause == "spread":
            messages.append(message("precision_spread", ch.label, precision, PRECISION_FACTOR, limit, chunk=key))
        elif cause == "residuals":
            messages.append(message("precision_residuals", ch.label, precision, PRECISION_FACTOR, limit,
                                    float(np.sqrt(variance_factor)), chunk=key))
        elif cause == "inherited":
            messages.append(message("precision_inherited", ch.label, precision, PRECISION_FACTOR, limit, own,
                                    ", ".join(chunks[k].label for k in through), chunk=key))
    return Solution(transforms=transforms, residuals=entries, redundancy=int(redundancy), rms=rms,
                    per_chunk=per_chunk, crs=system, unknowns=dict(kinds), left_out=left_out,
                    warnings=[render(m) for m in messages], previous=previous, robust=bool(robust), sigma0=sigma0,
                    converged=bool(settled), irls_rounds=int(rounds if robust else 0), messages=messages)


def apply(doc: "Document", solution: Solution) -> dict:
    """The transforms of ``solution`` given to its chunks (and their CRS: the system of the solve, the marker
    references converted between two projected systems, as *Allinea chunk* does); the report kept in
    ``doc.meta["ties_report"]``. :class:`TieError` when a chunk changed since the solve (solve again). Returns what it
    changed, for :func:`restore` (one Annulla step), with ``stale_products``: chunk key -> the DEM/orthophoto built
    in the old transform, to build again (also in the report, per chunk).

    In a local system the moved chunks are marked ``levelled``: their frame is now the solve's and building their DEM
    must not turn them again. Unlike *Allinea chunk* the fixed chunk is not levelled here (a solve moves no fixed
    chunk; :func:`solve` warns when it would be turned later), so its flag is not copied onto the others."""
    import copy

    from ..core import crs as crs_mod
    from ..core import free_drawings
    from .chunks import _convert_references

    chunks = {c.key: c for c in doc.chunks}
    for key, before in solution.previous.items():
        ch = chunks.get(key)
        if ch is None or ch.crs != before["crs"] or not np.array_equal(np.asarray(ch.transform, float),
                                                                       before["transform"]):
            label = ch.label if ch is not None else key
            raise TieError(f"chunk {label} changed since the solve: solve again",
                           f"il chunk {label} è cambiato dopo la stima: rifai la stima")
    changed = {"chunks": {}, "ties_report": copy.deepcopy(doc.meta.get("ties_report")), "stale_products": {}}
    report = solution.to_dict()
    for key, kind in solution.unknowns.items():
        if kind == "fixed":
            continue
        ch = chunks[key]
        changed["chunks"][key] = {"transform": np.asarray(ch.transform, float).tolist(), "crs": ch.crs,
                                  "references": {m.key: copy.deepcopy(m.reference.location) for m in ch.markers},
                                  "chunk_alignment": copy.deepcopy(ch.meta.get("chunk_alignment")),
                                  "levelled": ch.meta.get("levelled"),
                                  "camera_reference_crs": {c.key: c.meta.get("reference_crs") for c in ch.cameras}}
        if (ch.crs != solution.crs and not crs_mod.is_local(ch.crs) and not crs_mod.is_local(solution.crs)):
            _convert_references(ch, ch.crs, solution.crs)
        ch.transform = np.asarray(solution.transforms[key], float).copy()
        ch.crs = solution.crs
        ch.meta["chunk_alignment"] = {"method": "ties", "kind": kind, "rmse": solution.rms,
                                      "redundancy": solution.redundancy}
        if crs_mod.is_local(solution.crs):
            ch.meta["levelled"] = True  # its frame is now the solve's: building its DEM must not turn it again
        free_drawings.follow(ch)
        stale = [k for k in ("dem", "orthomosaic") if ch.products.get(k)]
        if stale:
            changed["stale_products"][key] = stale
        report["per_chunk"][str(key)]["stale_products"] = stale
    doc.meta["ties_report"] = report
    return changed


def restore(doc: "Document", changed: dict) -> None:
    """What :func:`apply` changed, put back."""
    from ..core import free_drawings

    chunks = {c.key: c for c in doc.chunks}
    for key, before in changed["chunks"].items():
        ch = chunks.get(int(key))
        if ch is None:
            continue
        ch.transform = np.asarray(before["transform"], float)
        ch.crs = before["crs"]
        for m in ch.markers:
            if m.key in before["references"]:
                m.reference.location = before["references"][m.key]
        for c in ch.cameras:
            if c.key in before.get("camera_reference_crs", {}):
                if before["camera_reference_crs"][c.key] is None:
                    c.meta.pop("reference_crs", None)
                else:
                    c.meta["reference_crs"] = before["camera_reference_crs"][c.key]
        for meta_key in ("chunk_alignment", "levelled"):
            if before.get(meta_key) is None:
                ch.meta.pop(meta_key, None)
            else:
                ch.meta[meta_key] = before[meta_key]
        free_drawings.follow(ch, adopt=False)
    if changed.get("ties_report") is None:
        doc.meta.pop("ties_report", None)
    else:
        doc.meta["ties_report"] = changed["ties_report"]


# --------------------------------------------------------------------------- the checks before applying
CHECK_FACTOR = 3.0  # a pair of distances, a leave-one-out move or a residual over this x its precision: suspect
FREE_SCALE_TIES = 4  # from this many ties between two chunks the ratio of their scales is measured and taken out
ROTATION_LIMIT = 5.0  # degrees: a georeferenced chunk turned more than this by a solve - a warning
SCALE_LIMITS = (0.98, 1.02)  # a georeferenced chunk scaled outside these by a solve - a warning
AXIS_LIMIT = 2.0  # degrees: the axes of two chunks that should be one straight axis, further apart - a warning


def _usable_ends(doc: "Document", ties=None) -> list[tuple[Tie, tuple, np.ndarray, np.ndarray]]:
    """Active, whole ties with both ends placed: ``(tie, (chunk A, chunk B or None), world A, world B or known)``,
    the chunks in the order of the document (a tie's ends swapped when needed)."""
    wanted = None if ties is None else {t if isinstance(t, str) else t.id for t in ties}
    order = {c.key: i for i, c in enumerate(doc.chunks)}
    out = []
    for t in ties_of(doc):
        if (wanted is not None and t.id not in wanted) or t.broken or not t.active:
            continue
        if t.b is None:
            pa = end_world(doc, t.a[0], t.a[1], t.a_uid)
            if pa is not None and t.known is not None:
                out.append((t, (t.a[0], None), pa, np.asarray(t.known, float)))
            continue
        pa, pb = end_world(doc, t.a[0], t.a[1], t.a_uid), end_world(doc, t.b[0], t.b[1], t.b_uid)
        if pa is None or pb is None:
            continue
        group = (t.a[0], t.b[0])
        if order.get(t.a[0], 0) > order.get(t.b[0], 0):
            group, pa, pb = (t.b[0], t.a[0]), pb, pa
        out.append((t, group, pa, pb))
    return out


def _metric(chunk: "Chunk") -> bool:
    """World units of the chunk are metres: a projected system, a transform with a scale (references, scale bars, a
    kept georeference) or data measured in metres (``meta["metric"]``), as for *Allinea chunk*."""
    from .chunks import _is_scaled

    return _is_scaled(chunk)


def distance_checks(doc: "Document", ties=None, free_scale: bool | None = None) -> list[dict]:
    """The distances between the points of every two ties that link the same two chunks (or the same chunk and its
    known coordinates), measured in one chunk and in the other: they do not depend on how the chunks are placed, so a
    wrong tie shows before any solve. One dict per pair: ``ties`` (ids), ``names``, ``chunks`` ([A, B], B None for
    known points), ``labels``, ``distance_a``, ``distance_b`` (each in its chunk's world, as it is now), ``scale``,
    ``scale_free``, ``metric``, ``difference`` (``distance_b / scale - distance_a``), ``precision`` (the two ties'
    precisions combined), ``tolerance`` (:data:`CHECK_FACTOR` x it), ``comparable``, ``why``, ``ok``.

    The scale: when both sides are in metres (``metric``) the ratio of the scales of B and A is measured from
    :data:`FREE_SCALE_TIES` ties (the median over the ties of the median ratio of their pairs, pairs shorter than 10 x
    their precision left out of it) and taken out, so that the pairs name the wrong tie; :func:`report` still says
    when the ratio is not 1. Fewer ties: compared as they are (a scale difference makes the pairs fail). ``free_scale``
    False never measures it, True from 3 ties. When a side has no metric scale (a local frame aligned without
    references), the ratio is always measured; with fewer ties the distances cannot be compared: ``comparable``
    False, ``why`` "no_scale", ``ok`` None. A side in a geographic system (degrees): ``why`` "degrees", ``ok`` None.
    Inactive, broken ties and ends without a position are skipped."""
    from ..core import crs as crs_mod

    chunks = {c.key: c for c in doc.chunks}
    groups: dict[tuple, list] = {}
    for item in _usable_ends(doc, ties):
        groups.setdefault(item[1], []).append(item)
    checks = []
    for group, items in groups.items():
        n = len(items)
        if n < 2:
            continue
        ca, cb = chunks[group[0]], chunks.get(group[1]) if group[1] is not None else None
        systems = [ca.crs] + ([cb.crs] if cb is not None else [t.crs for t, *_ in items])
        degrees = any(crs_mod.is_geographic(s) for s in systems)
        metric = _metric(ca) and (cb is None or _metric(cb))
        pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]
        da = {p: float(np.linalg.norm(items[p[0]][2] - items[p[1]][2])) for p in pairs}
        db = {p: float(np.linalg.norm(items[p[0]][3] - items[p[1]][3])) for p in pairs}
        prec = {p: float(np.hypot(items[p[0]][0].precision, items[p[1]][0].precision)) for p in pairs}
        need = 3 if free_scale else FREE_SCALE_TIES
        free = free_scale is not False and n >= need
        comparable, why = not degrees, "degrees" if degrees else None
        if comparable and not metric and not free and free_scale is not False:
            comparable, why = False, "no_scale"
        scale = 1.0
        if free and comparable:
            per_tie = []
            for k in range(n):
                mine = [p for p in pairs if k in p and da[p] > 0]
                long = [p for p in mine if da[p] >= 10 * prec[p]] or mine  # short pairs give noisy ratios
                if long:
                    per_tie.append(float(np.median([db[p] / da[p] for p in long])))
            if per_tie:
                scale = float(np.median(per_tie))
        for p in pairs:
            ti, tj = items[p[0]][0], items[p[1]][0]
            diff = db[p] / scale - da[p]
            checks.append({"ties": [ti.id, tj.id], "names": [ti.name, tj.name], "chunks": [group[0], group[1]],
                           "labels": [ca.label, cb.label if cb is not None else None], "systems": systems,
                           "distance_a": da[p], "distance_b": db[p], "scale": scale,
                           "scale_free": bool(free and comparable), "metric": bool(metric),
                           "difference": float(diff) if comparable else None, "precision": prec[p],
                           "tolerance": CHECK_FACTOR * prec[p], "comparable": bool(comparable), "why": why,
                           "ok": bool(abs(diff) <= CHECK_FACTOR * prec[p]) if comparable else None})
    return checks


def _distance_counts(checks: list[dict]) -> dict[str, list[int]]:
    counts: dict[str, list[int]] = {}
    for ch in checks:
        if ch.get("ok") is None:  # not comparable
            continue
        for tid in ch["ties"]:
            c = counts.setdefault(tid, [0, 0])
            c[1] += 1
            c[0] += 0 if ch["ok"] else 1
    return counts


def suspects(checks: list[dict]) -> list[str]:
    """The ties most likely wrong: those of which more than half the pairs fail (:func:`distance_checks`; pairs that
    cannot be compared do not count), the worst first. One wrong tie among n fails all its n - 1 pairs, a right one
    only its pair with the wrong one; with two ties alone both are named (a single pair cannot say which)."""
    counts = _distance_counts(checks)
    bad = [(tid, f, n) for tid, (f, n) in counts.items() if f and f > n / 2]
    order = {tid: i for i, tid in enumerate(counts)}
    bad.sort(key=lambda x: (-x[1] / x[2], -x[1], order[x[0]]))
    return [tid for tid, _f, _n in bad]


def leave_one_out(doc: "Document", unknowns: dict, ties=None, references: bool = True,
                  robust: bool = False, solution: Solution | None = None) -> dict[str, float]:
    """For each tie of the solve (two-chunk ties and known points), the solve made again without it: how far apart
    the others put its ends (for a known point: from its coordinates). A wrong tie is far from where the others put
    it. Only ties after whose removal some redundancy is left (>= 1); the others are not in the result. Nothing is
    changed. ``solution``: the solve with every tie, when the caller already has it (made with the same arguments).
    :class:`TieError` when the solve with every tie cannot be made.

    One solve per tie (each up to 30 robust rounds with ``robust``): the window runs it in its worker. The value is
    a distance, without the propagated precision of the reduced solve (:func:`report` compares it with 3 x the
    tie's precision and names only the ties near the worst: fix the worst one, solve again)."""
    full = solution if solution is not None else solve(doc, unknowns, ties=ties, references=references,
                                                       robust=robust)
    ids = [r["id"] for r in full.residuals if r["id"] is not None and r["fitted"]]
    out: dict[str, float] = {}
    if full.redundancy < 4:  # a tie takes 3 equations away: nothing would be left to check it
        return out
    for tid in ids:
        try:
            sol = solve(doc, unknowns, ties=ties, references=references, robust=robust, check_ties={tid})
        except TieError:
            continue
        if sol.redundancy < 1:
            continue
        entry = next((r for r in sol.residuals if r["id"] == tid), None)
        if entry is not None:
            out[tid] = float(entry["d"])
    return out


def _held_by_references(chunk: "Chunk") -> bool:
    """The chunk's georeference comes from its own references: at least three enabled camera positions (GPS/GNSS)
    or control markers - not from an earlier solve or *Allinea chunk* only."""
    cams = sum(1 for c in chunk.cameras if c.reference.enabled and c.reference.location)
    ctrl = sum(1 for m in chunk.markers if not m.checkpoint and m.reference.enabled and m.reference.location)
    return cams >= 3 or ctrl >= 3


held_by_references = _held_by_references  # public name (the window proposes the unknowns from it)


def plausibility(doc: "Document", solution: Solution) -> list[dict]:
    """Warnings (:func:`message`) for a chunk georeferenced before the solve by its own references (a projected
    system and GPS/GNSS camera positions or control markers: :func:`_held_by_references`) that the solve turns by
    more than :data:`ROTATION_LIMIT` degrees or scales outside :data:`SCALE_LIMITS`; none for a chunk in a local
    frame (its rotation and scale were arbitrary) or one placed in its system only by an earlier solve. The rotation
    is measured after the chunk is carried into the system of the solve (a chunk from another UTM zone does not
    count the grid convergence)."""
    from ..core import crs as crs_mod

    chunks = {c.key: c for c in doc.chunks}
    out = []
    for key, kind in solution.unknowns.items():
        pc = solution.per_chunk.get(key, {})
        before = pc.get("crs_before")
        ch = chunks.get(key)
        if kind == "fixed" or not before or crs_mod.is_local(before) or ch is None or not _held_by_references(ch):
            continue
        if pc["rotation_deg"] > ROTATION_LIMIT:
            out.append(message("implausible_rotation", pc["label"], float(pc["rotation_deg"]), ROTATION_LIMIT, before,
                               chunk=key))
        if not SCALE_LIMITS[0] <= pc["scale"] <= SCALE_LIMITS[1]:
            out.append(message("implausible_scale", pc["label"], float(pc["scale"]), SCALE_LIMITS[0], SCALE_LIMITS[1],
                               before, chunk=key))
    return out


def _axis_angles(u: np.ndarray, v: np.ndarray) -> tuple[float, float, float]:
    """The angle between two axes (directions without a sense), in plan and in height, degrees."""
    if u @ v < 0:
        v = -v
    angle = float(np.degrees(np.arccos(np.clip(abs(u @ v), -1.0, 1.0))))
    hu, hv = u[:2], v[:2]
    if np.linalg.norm(hu) < 1e-9 or np.linalg.norm(hv) < 1e-9:
        plan = 0.0
    else:
        plan = abs(float(np.degrees(np.arctan2(hu[0] * hv[1] - hu[1] * hv[0], hu @ hv))))
        plan = min(plan, 180.0 - plan)
    height = abs(float(np.degrees(np.arcsin(np.clip(u[2], -1, 1)) - np.arcsin(np.clip(v[2], -1, 1)))))
    return angle, plan, height


def axis_check(doc: "Document", solution: Solution, axes: dict, limit: float = AXIS_LIMIT) -> dict:
    """A shape check: an axis that must stay straight across the chunks (the axis of a hull, of a wall). ``axes``:
    chunk (key or label) -> two points of the axis in the chunk's internal frame. The direction of each axis in the
    world of the solve (``solution.transforms``) and before (the chunks' transforms now, only between chunks in the
    same system); one entry per pair of chunks: ``chunks``, ``angle_deg`` (axes without a sense), ``plan_deg``,
    ``height_deg``, ``angle_before_deg`` (None between systems), ``ok`` (within ``limit`` degrees); ``max_deg``,
    ``max_before_deg``, ``ok``, ``limit_deg``. :class:`TieError` with fewer than two axes, an axis of zero length or a
    chunk not in the solve."""
    order = [c.key for c in doc.chunks]
    dirs, before = {}, {}
    for item, (p, q) in axes.items():
        try:
            ch = _chunk(doc, item)
        except (KeyError, ValueError) as exc:
            raise TieError(f"unknown chunk: {exc}", f"chunk sconosciuto: {exc}") from None
        if ch.key not in solution.transforms:
            raise TieError(f"chunk {ch.label} is not in the solve", f"il chunk {ch.label} non è nella stima")
        pts = np.array([p, q], float).reshape(2, 3)
        for store, T in ((dirs, solution.transforms[ch.key]), (before, ch.transform)):
            w = geometry.apply(np.asarray(T, float), pts)
            d = w[1] - w[0]
            if np.linalg.norm(d) <= 0:
                raise TieError(f"the axis of chunk {ch.label} has zero length",
                               f"l'asse del chunk {ch.label} ha lunghezza zero")
            store[ch.key] = d / np.linalg.norm(d)
    if len(dirs) < 2:
        raise TieError("a straight axis needs its direction in two chunks at least",
                       "un asse dritto richiede la sua direzione in almeno due chunk")
    keys = sorted(dirs, key=order.index)
    crs = {c.key: c.crs for c in doc.chunks}
    pairs = []
    for i, ka in enumerate(keys):
        for kb in keys[i + 1:]:
            angle, plan, height = _axis_angles(dirs[ka], dirs[kb])
            prior = _axis_angles(before[ka], before[kb])[0] if crs[ka] == crs[kb] else None
            pairs.append({"chunks": [ka, kb], "angle_deg": angle, "plan_deg": plan, "height_deg": height,
                          "angle_before_deg": prior, "ok": bool(angle <= limit)})
    priors = [p["angle_before_deg"] for p in pairs if p["angle_before_deg"] is not None]
    return {"directions": {k: dirs[k].tolist() for k in keys}, "pairs": pairs,
            "max_deg": max(p["angle_deg"] for p in pairs), "max_before_deg": max(priors) if priors else None,
            "ok": all(p["ok"] for p in pairs), "limit_deg": float(limit)}


def report(solution: Solution, checks: list[dict], loo: dict[str, float], warnings: list[dict],
           axis: dict | None = None) -> dict:
    """Everything the checks say about a solve, as data (JSON) for the window and the command line: the solve
    (``crs``, ``redundancy``, ``rms``, ``sigma0``, ``robust``), ``chunks`` (moves and precision), ``residuals``,
    ``ties`` (one row per tie: residual, ``loo``, ``distance_pairs``/``distance_failed``, ``suspect``), ``checks``,
    ``suspects``, ``loo``, ``left_out``, ``axis`` and ``messages`` (:func:`message`, the most important first:
    no check at all, suspect ties, large leave-one-out moves, large residuals, implausible moves, a bent axis, the
    warnings of the solve, what was left out). ``warnings``: :func:`plausibility`."""
    from ..core import crs as crs_mod

    suspect = suspects(checks)
    counts = _distance_counts(checks)
    labels = {k: v["label"] for k, v in solution.per_chunk.items()}
    rows = []
    for r in solution.residuals:
        if r["id"] is None:
            continue
        f, n = counts.get(r["id"], [0, 0])
        rows.append({"id": r["id"], "name": r["name"], "kind": r["kind"], "chunks": r["chunks"], "d": r["d"],
                     "plan": float(np.hypot(r["dx"], r["dy"])), "dz": r["dz"], "precision": r["precision"],
                     "normalised": r["normalised"], "fitted": r["fitted"], "check": r["check"],
                     "loo": loo.get(r["id"]), "distance_pairs": n, "distance_failed": f,
                     "suspect": r["id"] in suspect})
    names = {r["id"]: r["name"] for r in rows}
    for ch in checks:
        for tid, name in zip(ch["ties"], ch["names"]):
            names.setdefault(tid, name)
    msgs = []
    if solution.redundancy <= 0:
        msgs.append(message("no_check"))
    for tid in suspect:
        f, n = counts[tid]
        msgs.append(message("distance_suspect", names.get(tid, tid), f, n))
    comparable = [ch for ch in checks if ch.get("ok") is not None]
    failed = sum(1 for ch in comparable if not ch["ok"])
    if failed and not suspect:
        msgs.append(message("distance_failed", failed, len(comparable)))
    groups: dict[tuple, list] = {}
    for ch in checks:
        groups.setdefault(tuple(ch["chunks"]), []).append(ch)
    for (ka, kb), pairs_of in groups.items():
        first = pairs_of[0]
        la, lb = first.get("labels") or [ka, kb]
        if first.get("why") == "degrees":
            msgs.append(message("distances_degrees", la if kb is None else f"{la} / {lb}",
                                ", ".join(sorted({str(x) for x in first.get("systems", [])
                                                  if crs_mod.is_geographic(x)}))))
        elif first.get("why") == "no_scale":
            msgs.append(message("distances_no_scale_known", la, FREE_SCALE_TIES) if kb is None else
                        message("distances_no_scale", la, lb, FREE_SCALE_TIES))
        elif first.get("scale_free") and first.get("metric"):
            # the ratio was taken out to name the wrong tie: a real difference of scale is said here
            spread = abs(first["scale"] - 1.0) * max(r["distance_a"] for r in pairs_of)
            if spread > float(np.median([r["tolerance"] for r in pairs_of])):
                msgs.append(message("distance_scale_known", la, first["scale"]) if kb is None else
                            message("distance_scale", la, lb, first["scale"]))
    precision = {r["id"]: r["precision"] for r in rows}
    worst = max(loo.values()) if loo else 0.0
    for tid, value in sorted(loo.items(), key=lambda kv: -kv[1]):
        # a wrong tie drags the others too: only those near the worst one are named
        if value > CHECK_FACTOR * precision.get(tid, 0.0) and value >= 0.5 * worst:
            msgs.append(message("loo_large", names.get(tid, tid), value, precision.get(tid, 0.0)))
    large = sorted((r for r in rows if r["fitted"] and r["normalised"] > CHECK_FACTOR), key=lambda r: -r["normalised"])
    for r in large:  # as above: those near the worst one
        if r["normalised"] >= 0.5 * large[0]["normalised"]:
            msgs.append(message("residual_large", r["name"], r["d"], r["normalised"]))
    msgs.extend(warnings)
    if axis is not None:
        for p in axis["pairs"]:
            if not p["ok"]:
                msgs.append(message("axis_bent", labels.get(p["chunks"][0], p["chunks"][0]),
                                    labels.get(p["chunks"][1], p["chunks"][1]), p["angle_deg"], p["plan_deg"],
                                    p["height_deg"]))
    msgs.extend(solution.messages)
    for x in solution.left_out:
        msgs.extend(x.get("messages") or [])
    chunks = [{"key": k, "label": v["label"], "kind": v["kind"], "rotation_deg": v["rotation_deg"],
               "shift": v["shift"], "scale": v["scale"], "precision_m": v.get("precision_m", 0.0),
               "precision_cause": v.get("precision_cause"), "crs_before": v["crs_before"], "crs": v["crs"]}
              for k, v in solution.per_chunk.items()]
    return {"crs": solution.crs, "redundancy": int(solution.redundancy), "rms": float(solution.rms),
            "sigma0": solution.sigma0, "robust": bool(solution.robust), "chunks": chunks,
            "residuals": solution.residuals, "ties": rows, "checks": checks, "suspects": suspect,
            "loo": dict(loo), "left_out": list(solution.left_out), "axis": axis, "messages": msgs}


def _metres(value: float, language: str, decimals: int = 3) -> str:
    text = f"{value:.{decimals}f}"
    return text.replace(".", ",") if language == "it" else text


def report_text(solution: Solution, checks: list[dict], loo: dict[str, float], warnings: list[dict],
                axis: dict | None = None, language: str = "en") -> str:
    """The report (:func:`report`) as plain text, English or Italian (``language="it"``), for the command line."""
    rep = report(solution, checks, loo, warnings, axis)
    lines = [_text("line_system", language, rep["crs"] or "LOCAL"),
             _text("line_redundancy", language, rep["redundancy"])]
    if rep["sigma0"] is None:
        lines.append(_text("line_fit_no_sigma", language, rep["rms"]))
    else:
        lines.append(_text("line_fit", language, rep["rms"], float(rep["sigma0"])))
    lines += ["", _text("heading_chunks", language)]
    for c in rep["chunks"]:
        lines.append("  " + _text("line_chunk", language, c["label"], _text("unknowns_" + c["kind"], language),
                                  float(c["rotation_deg"]), float(c["shift"]), float(c["scale"]),
                                  float(c["precision_m"])))
    lines += ["", _text("heading_residuals", language)]
    for r in rep["residuals"]:
        kind = _text("kind_check" if r["check"] else "kind_" + r["kind"], language)
        value = loo.get(r["id"]) if r["id"] is not None else None
        left = _text("no_value", language) if value is None else _metres(value, language) + " m"
        lines.append("  " + _text("line_residual", language, r["name"], kind, float(np.hypot(r["dx"], r["dy"])),
                                  float(r["dz"]), float(r["precision"]), left))
    comparable = [c for c in checks if c.get("ok") is not None]
    if comparable:
        lines += ["", _text("line_distances", language, sum(1 for c in comparable if c["ok"]), len(comparable))]
    if axis is not None:
        if axis["max_before_deg"] is None:
            lines.append(_text("line_axis_no_before", language, float(axis["max_deg"])))
        else:
            lines.append(_text("line_axis", language, float(axis["max_deg"]), float(axis["max_before_deg"])))
    if rep["messages"]:
        lines += ["", _text("heading_warnings", language)]
        lines += ["  " + render(m, language) for m in rep["messages"]]
    return "\n".join(lines)


__all__ = ["Tie", "ties_of", "save_ties", "tie_by_id", "next_name", "add_tie", "add_known", "link_markers",
           "ties_from_markers", "import_ties", "end_world", "end_internal", "end_precision", "raster_precision",
           "remove_tie", "resolve_end", "dem_height", "world_to_internal", "check_ends", "update_known", "TABLE_PRECISION", "MIN_PHOTO_PRECISION",
           "KINDS", "TieError", "Solution", "solve", "Comma", "held_by_references", "apply", "restore", "MESSAGES", "message", "render",
           "distance_checks", "suspects", "leave_one_out", "plausibility", "axis_check", "report", "report_text"]
