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
from dataclasses import dataclass
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
    reasons = []
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
            continue
        chunk, marker = found
        if marker.key != end[1]:
            setattr(tie, which, (chunk.key, marker.key))
        if which == "a" and tie.b is None:
            if not marker.reference.location:  # cleared by the user: no known point any more (never put back)
                reasons.append(f"a: marker {marker.label!r} of chunk {chunk.label!r} has no reference any more")
                continue
            tie.known = [float(v) for v in marker.reference.location]
            tie.precision = float(max(marker.reference.accuracy or [tie.precision]))  # read only: per axis it stays
            tie.crs = chunk.crs
    if reasons:
        tie.broken = "; ".join(reasons)
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


def import_ties(doc: "Document", path) -> list[Tie]:
    """Ties from a table: ``chunk_a,x_a,y_a,z_a,chunk_b,x_b,y_b,z_b[,precision][,name]`` (commas, or semicolons or tabs
    with decimal commas allowed; a header line is skipped). Chunks by label (or key), points in the world coordinates
    of each chunk, converted to internal; an empty ``z`` is read from the chunk's DEM. A row with ``chunk_b`` empty is
    a point of ``chunk_a`` with known coordinates ``x_b, y_b, z_b`` (:func:`add_known`). Precision default
    :data:`TABLE_PRECISION`. The table is read whole first: a ValueError naming the row, and nothing added, when a
    row is wrong."""
    from ..core import stamps
    from ..core.free_drawings import raster_values

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
                if chunk.dem_path is None or not chunk.dem_path.exists():
                    raise ValueError(f"row {i}: no z and the chunk {chunk.label!r} has no DEM to read it from")
                if stamps.is_stale(chunk, "dem"):
                    raise ValueError(f"row {i}: no z and the DEM of {chunk.label!r} was built in another system of the "
                                     "chunk: rebuild it first")
                (z,) = raster_values(chunk.dem_path, [xyz[:2]])
                if z is None:
                    raise ValueError(f"row {i}: no z and the DEM of {chunk.label!r} has no height at "
                                     f"{xyz[0]:.3f}, {xyz[1]:.3f}")
                xyz = [xyz[0], xyz[1], z]
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
    ``left_out``: ``[{"id", "name", "reason"}]`` the ties not used; ``warnings``; ``previous``: the transform and CRS of
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

    def to_dict(self) -> dict:
        return {"transforms": {str(k): np.asarray(v, float).tolist() for k, v in self.transforms.items()},
                "residuals": self.residuals, "redundancy": int(self.redundancy), "rms": float(self.rms),
                "sigma0": None if self.sigma0 is None else float(self.sigma0),
                "per_chunk": {str(k): v for k, v in self.per_chunk.items()}, "crs": self.crs,
                "unknowns": {str(k): v for k, v in self.unknowns.items()}, "left_out": self.left_out,
                "warnings": list(self.warnings), "robust": bool(self.robust), "converged": bool(self.converged),
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


def solve(doc: "Document", unknowns: dict, ties=None, references: bool = True, robust: bool = False) -> Solution:
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
    :data:`PRECISION_FACTOR` x the median precision of its ties (``precision_limit``) a warning says the ties are
    nearly on a line or on one side.

    :class:`TieError`: an unknown kind; nothing fixes the system (no fixed chunk, no known point); a chunk with fewer
    equations than unknowns ("similarity" with 2 ties); unknowns the ties do not determine (points on a line, chunks
    tied only among themselves); a solve that does not converge; chunks in systems that cannot be mixed; a geographic
    system."""
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
        missing_ids = [{"id": i, "name": "", "reason": "no such tie"} for i in sorted(wanted - ids)]
    # the reference of a known tie's marker counts through the tie only: never also as a reference, even when the
    # tie is off, broken or not among ``ties``
    known_ends = {t.a for t in every_tie if t.b is None}
    left_out, observations = ([] if wanted is None else missing_ids), []
    for t in every_tie:
        if wanted is not None and t.id not in wanted:
            continue
        if t.broken:
            left_out.append({"id": t.id, "name": t.name, "reason": t.broken})
            continue
        if not t.active:
            left_out.append({"id": t.id, "name": t.name, "reason": "inactive"})
            continue
        outside = [doc_chunk.label for k, _ in t.ends() if k not in chunks
                   for doc_chunk in doc.chunks if doc_chunk.key == k]
        if outside:
            left_out.append({"id": t.id, "name": t.name,
                             "reason": f"chunk {', '.join(outside)} not in the solve"})
            continue
        points, missing = [], []
        for end, uid in t.end_uids():
            chunk, marker = resolve_end(doc, end, uid)
            p = end_internal(chunk, marker)
            if p is None:
                missing.append(f"marker {marker.label!r} of chunk {chunk.label!r}")
            points.append((chunk.key, p))
        if missing:
            left_out.append({"id": t.id, "name": t.name, "reason": f"no position for {', '.join(missing)}"})
            continue
        if t.b is None:
            chunk, marker = resolve_end(doc, t.a, t.a_uid)
            if not marker.reference.enabled:  # switched off in the Riferimenti pane
                left_out.append({"id": t.id, "name": t.name, "reason": "its reference is disabled"})
                continue
            sigma = _sigma3(marker.reference.accuracy, t.precision)
            observations.append(("known", t, points[0], None, np.asarray(t.known, float), t.crs, sigma,
                                 bool(marker.checkpoint), chunk))
        else:
            observations.append(("tie", t, points[0], points[1], None, None, np.full(3, float(t.precision)), False,
                                 None))
    if references:
        for key in order:
            ch = chunks[key]
            for m in ch.markers:
                if (key, m.key) in known_ends or not (m.reference.enabled and m.reference.location):
                    continue
                p = end_internal(ch, m)
                if p is None:
                    left_out.append({"id": None, "name": m.label,
                                     "reason": f"reference of chunk {ch.label!r} without a position"})
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
            left_out.append({"id": o[1].id if o[0] == "known" else None, "name": o[1].name if o[0] == "known"
                             else o[1].label, "reason": f"{what} of chunk {o[8].label!r} in {o[5] or 'LOCAL'}, "
                             f"not in {system or 'LOCAL'}"})

    warnings: list[str] = []
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
            warnings.append(f"{ch.label}: {ch.crs} -> {system} approximated within {err:.3f} m")
        world_of[key] = _translate(off) @ W
    for key in order:
        ch = chunks[key]
        if kinds[key] == "fixed":
            if (crs_mod.is_local(ch.crs) and ch.aligned_cameras and not ch.meta.get("levelled")
                    and np.allclose(geometry.decompose_similarity(ch.transform)[1], np.eye(3))):
                warnings.append(f"{ch.label}: a local frame not levelled yet - building its DEM will turn it (and not "
                                "the chunks tied to it): level it first (build its DEM), then solve")
            continue
        if not _compatible(ch.crs, system):
            if any(m.reference.location for m in ch.markers):
                warnings.append(f"{ch.label}: its marker references are in {ch.crs or 'LOCAL'}; they stay as they are "
                                f"and no longer match once it is in {system or 'LOCAL'}")
            if kinds[key] in ("plan", "shift", "height"):
                warnings.append(f"{ch.label}: from {ch.crs or 'LOCAL'} to {system or 'LOCAL'} with '{kinds[key]}' "
                                "unknowns: its tilt and scale stay those of its own frame")

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
        dof = 3 * len(fitted) - n_par
        if dof > 0:
            s0 = float(np.sum(result.fun ** 2) / dof)
            covariance *= max(1.0, s0)
    if robust and not settled:
        warnings.append("the robust weights did not settle in 30 rounds: the result may still move")

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

    def chunk_precision(key) -> float:
        """The largest standard deviation (3D) of the corners of the chunk's extent moved by its unknowns."""
        sl = slices[key]
        n = sl.stop - sl.start
        corners = extent(key)
        if not n or not len(corners):
            return 0.0
        cov = covariance[sl, sl]
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
        limit = PRECISION_FACTOR * float(np.median(sigmas)) if sigmas else 0.0
        per_chunk[key]["precision_m"] = precision
        per_chunk[key]["precision_limit"] = limit
        if kinds[key] != "fixed" and precision > limit > 0:
            warnings.append(f"{ch.label}: placed with a precision of {precision:.3f} m at the edge of the chunk, "
                            f"over {PRECISION_FACTOR:g} x its ties ({limit:.3f} m): the ties are nearly on a line or "
                            "all on one side - add ties far from the others")
    return Solution(transforms=transforms, residuals=entries, redundancy=int(redundancy), rms=rms,
                    per_chunk=per_chunk, crs=system, unknowns=dict(kinds), left_out=left_out, warnings=warnings,
                    previous=previous, robust=bool(robust), sigma0=sigma0, converged=bool(settled),
                    irls_rounds=int(rounds if robust else 0))


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


__all__ = ["Tie", "ties_of", "save_ties", "tie_by_id", "next_name", "add_tie", "add_known", "link_markers",
           "ties_from_markers", "import_ties", "end_world", "end_internal", "end_precision", "raster_precision",
           "remove_tie", "resolve_end", "check_ends", "update_known", "TABLE_PRECISION", "MIN_PHOTO_PRECISION",
           "KINDS", "TieError", "Solution", "solve", "apply", "restore"]
