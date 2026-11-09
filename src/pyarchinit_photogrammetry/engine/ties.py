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


__all__ = ["Tie", "ties_of", "save_ties", "tie_by_id", "next_name", "add_tie", "add_known", "link_markers",
           "ties_from_markers", "import_ties", "end_world", "end_internal", "end_precision", "raster_precision",
           "remove_tie", "resolve_end", "check_ends", "update_known", "TABLE_PRECISION", "MIN_PHOTO_PRECISION"]
