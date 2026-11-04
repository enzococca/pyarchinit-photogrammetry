"""Free drawings in 3D: lines, polygons and points drawn on the model as general features in user layers (not units
of PyArchInit), kept in ``chunk.meta["drawings"]``.

* ``"free"``: ``[{"id" (uuid4 hex), "layer", "kind" line|polygon|point, "name", "xyz" [[x, y, z], ...] (coordinates
  of the chunk, on the model; a polygon open: its first vertex not repeated), "attributes" {}, "created", "modified"
  (ISO, UTC)}]``, with an optional ``"colour"`` over the layer's;
* ``"free_layers"``: ``[{"name", "colour", "visible"}]`` (:data:`DEFAULT_LAYER` is always there);
* ``"free_basis"``: ``{"alignment", "transform"}``, the alignment and the transform of the chunk the coordinates
  were last written in, so that the drawings follow a change of the transform made inside the program
  (:func:`follow`: the georeference fitted again, chunks aligned, the system of the survey, an orientation) and stay
  as they are after a new alignment (its markers put the chunk back in the same frame).

Lengths and areas are computed, not stored. No Qt here. Since 1.7.2."""
from __future__ import annotations

import copy
import logging
import uuid
from datetime import datetime, timezone

import numpy as np

from . import geometry

log = logging.getLogger("pyarchinit_photogrammetry")

DEFAULT_LAYER = "Disegni 3D"
KINDS = ("line", "polygon", "point")
MIN_VERTICES = {"line": 2, "polygon": 3, "point": 1}
DEFAULT_COLOUR = "#ffb000"
KEYS = ("free", "free_layers", "free_basis")  # the keys of ``meta["drawings"]`` kept here
_NAMES = {"line": "Linea", "polygon": "Poligono", "point": "Punto"}
_FIELDS = ("layer", "name", "xyz", "attributes", "colour")
SINGULAR_COND = 1e12  # a basis transform whose linear part is worse conditioned than this: no motion from it
_said: set = set()  # the broken records already written in the log (once per record and session)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _drawings(chunk, create: bool = False) -> dict:
    meta = chunk.meta
    if create:
        return meta.setdefault("drawings", {})
    return meta.get("drawings") or {}


def _stored(chunk) -> list:
    return [f for f in _drawings(chunk).get("free") or [] if isinstance(f, dict)]


def _say_broken(chunk, records=None, valid=None) -> None:
    """The records of ``chunk`` that are not free drawings written in the log, once each per session (``valid``: the
    check of each of ``records`` already made)."""
    broken = []
    if records is None:
        records = _drawings(chunk).get("free") or []
    if valid is None:
        valid = [isinstance(rec, dict) and _valid(rec) for rec in records]
    for rec, ok in zip(records, valid):
        if ok:
            continue
        name = str(rec.get("id")) if isinstance(rec, dict) else repr(rec)[:80]
        key = (id(chunk), name)
        if key not in _said:
            _said.add(key)
            broken.append(name)
    if broken:
        log.warning("Disegni 3D non letti dal progetto: %s", ", ".join(broken))


def _xyz(kind: str, xyz) -> list[list[float]]:
    """``xyz`` checked for ``kind``: finite numbers, three per vertex, enough vertices; a polygon open."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}: one of {', '.join(KINDS)}")
    try:
        a = np.asarray(xyz, float)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"vertices that are not numbers: {exc}") from None
    if a.ndim != 2 or a.shape[1] != 3:
        raise ValueError("the vertices must be [x, y, z] each")
    if not np.all(np.isfinite(a)):
        raise ValueError("vertices with no valid numbers")
    if kind == "polygon" and len(a) > 1 and np.array_equal(a[0], a[-1]):
        a = a[:-1]
    if len(a) < MIN_VERTICES[kind]:
        raise ValueError(f"a {kind} needs at least {MIN_VERTICES[kind]} vertices, {len(a)} given")
    if kind == "point" and len(a) != 1:
        raise ValueError("a point has one vertex")
    return a.tolist()


def _valid(rec: dict) -> bool:
    try:
        return (isinstance(rec.get("id"), str) and rec.get("kind") in KINDS and isinstance(rec.get("layer"), str)
                and bool(_xyz(rec["kind"], rec["xyz"])))
    except (KeyError, ValueError, TypeError):
        return False


def _copied(value):
    """A copy of the JSON-like ``value`` (dicts, lists, scalars) several times faster than ``copy.deepcopy`` on the
    thousands of drawings of a redraw; anything else deep-copied."""
    if isinstance(value, dict):
        return {k: _copied(v) for k, v in value.items()}
    if isinstance(value, list):
        if all(type(v) in (float, int) for v in value):
            return value[:]
        return [_copied(v) for v in value]
    if value is None or type(value) in (str, int, float, bool):
        return value
    return copy.deepcopy(value)


def features(chunk, layer: str | None = None) -> list[dict]:
    """The free drawings of ``chunk`` (copies), of ``layer`` only when given; a broken record is left out, said in
    the log once per record and session."""
    records = _drawings(chunk).get("free") or []
    valid = [isinstance(rec, dict) and _valid(rec) for rec in records]  # each record checked once
    _say_broken(chunk, records, valid)
    return [_copied(rec) for rec, ok in zip(records, valid) if ok and (layer is None or rec["layer"] == layer)]


def layers(chunk) -> list[dict]:
    """The layers of the free drawings: :data:`DEFAULT_LAYER` first (also when not stored), those stored, then those
    named by a feature and not stored (default colour, visible). Copies."""
    stored = [dict(x) for x in _drawings(chunk).get("free_layers") or [] if isinstance(x, dict) and x.get("name")]
    names = {x["name"] for x in stored}
    out = [x for x in stored if x["name"] == DEFAULT_LAYER] or [
        {"name": DEFAULT_LAYER, "colour": DEFAULT_COLOUR, "visible": True}]
    out += [x for x in stored if x["name"] != DEFAULT_LAYER]
    for rec in _stored(chunk):
        name = rec.get("layer")
        if name and name not in names and name != DEFAULT_LAYER and _valid(rec):  # checked for a new name only
            names.add(name)
            out.append({"name": name, "colour": DEFAULT_COLOUR, "visible": True})
    for x in out:
        x.setdefault("colour", DEFAULT_COLOUR)
        x.setdefault("visible", True)
    return out


def _basis(chunk, transform=None) -> None:
    t = chunk.transform if transform is None else transform
    _drawings(chunk, True)["free_basis"] = {"alignment": chunk.meta.get("alignment_id"),
                                           "transform": np.asarray(t, float).tolist()}


def add(chunk, kind: str, xyz, layer: str = DEFAULT_LAYER, name: str = "", attributes: dict | None = None,
        colour: str | None = None, id: str | None = None) -> dict:  # noqa: A002 - the record's own key
    """A new free drawing of ``chunk`` (``xyz`` in its coordinates); without a ``name`` "Linea N", "Poligono N" or
    "Punto N"; the basis set to the transform of the chunk now (the drawings already there brought to it first,
    :func:`follow`). ``id``: the identity to give it (an import keeping the file's; a new uuid4 hex when None).
    Returns a copy of the record. ``ValueError`` for an unknown kind, too few vertices, numbers that are not finite, an
    ``id`` that is empty or already in the chunk."""
    xyz = _xyz(kind, xyz)
    if id is not None:
        if not isinstance(id, str) or not id:
            raise ValueError("an id is a non-empty text")
        if any(f.get("id") == id for f in _stored(chunk)):
            raise ValueError(f"the id {id} is in the chunk already")
    if not isinstance(layer, str) or not layer:
        raise ValueError("a layer needs a name")
    stored = _stored(chunk)
    if not name:
        n = sum(1 for f in stored if f.get("kind") == kind) + 1
        taken = {f.get("name") for f in stored}
        while f"{_NAMES[kind]} {n}" in taken:
            n += 1
        name = f"{_NAMES[kind]} {n}"
    now = _now()
    rec = {"id": id or uuid.uuid4().hex, "layer": layer, "kind": kind, "name": str(name), "xyz": xyz,
           "attributes": dict(attributes or {}), "created": now, "modified": now}
    if colour:
        rec["colour"] = str(colour)
    follow(chunk)  # those drawn before a change of the transform no hook saw: where they are on the model
    _basis(chunk)
    _drawings(chunk, True).setdefault("free", []).append(rec)
    return copy.deepcopy(rec)


def _find(chunk, fid: str) -> dict:
    for rec in _stored(chunk):
        if rec.get("id") == fid:
            return rec
    raise KeyError(fid)


def update(chunk, fid: str, **fields) -> dict:
    """Fields of the free drawing ``fid`` changed (``layer``, ``name``, ``xyz``, ``attributes``, ``colour`` - None
    takes it away), ``modified`` set. Returns a copy. ``KeyError`` for an unknown id, ``ValueError`` for an unknown
    field or vertices that do not make the drawing."""
    unknown = set(fields) - set(_FIELDS)
    if unknown:
        raise ValueError(f"fields that cannot be changed: {', '.join(sorted(unknown))}")
    rec = _find(chunk, fid)
    new = dict(fields)
    if "xyz" in new:
        new["xyz"] = _xyz(rec.get("kind"), new["xyz"])
    if "layer" in new and (not isinstance(new["layer"], str) or not new["layer"]):
        raise ValueError("a layer needs a name")
    if "name" in new and (not isinstance(new["name"], str) or not new["name"].strip()):
        raise ValueError("a free drawing needs a name")
    if "attributes" in new:
        new["attributes"] = dict(new["attributes"] or {})
    if "colour" in new and not new["colour"]:
        new.pop("colour")
        rec.pop("colour", None)
    rec.update(new)
    rec["modified"] = _now()
    return copy.deepcopy(rec)


def remove(chunk, fids) -> int:
    """The free drawings ``fids`` (an id or several) taken away. Returns how many."""
    fids = {fids} if isinstance(fids, str) else set(fids)
    drawings = _drawings(chunk)
    old = drawings.get("free") or []
    kept = [f for f in old if not (isinstance(f, dict) and f.get("id") in fids)]
    if len(kept) != len(old):
        drawings["free"] = kept
    return len(old) - len(kept)


def set_layer(chunk, name: str, colour: str | None = None, visible: bool | None = None,
              rename: str | None = None) -> dict:
    """The layer ``name`` (added when new) with its ``colour``, ``visible`` flag, or renamed to ``rename`` - in every
    free drawing too. Returns a copy. ``ValueError`` when ``rename`` is the name of another layer."""
    if not isinstance(name, str) or not name:
        raise ValueError("a layer needs a name")
    current = layers(chunk)
    if rename is not None and (not isinstance(rename, str) or not rename.strip()):
        raise ValueError("a layer needs a name")
    if rename is not None and rename != name and any(x["name"] == rename for x in current):
        raise ValueError(f"a layer named {rename!r} exists already")
    entry = next((x for x in current if x["name"] == name), None)
    if entry is None:
        entry = {"name": name, "colour": DEFAULT_COLOUR, "visible": True}
        current.append(entry)
    if colour is not None:
        entry["colour"] = str(colour)
    if visible is not None:
        entry["visible"] = bool(visible)
    if rename:
        entry["name"] = rename
        for rec in _stored(chunk):
            if rec.get("layer") == name:
                rec["layer"] = rename
    _drawings(chunk, True)["free_layers"] = current
    return dict(entry)


def colour_of(chunk, rec: dict, layers_: list | None = None) -> str:
    """The colour of the free drawing ``rec``: its own, else its layer's, else :data:`DEFAULT_COLOUR`. ``layers_``:
    the layers already read with :func:`layers` (a redraw reads them once, not once per drawing)."""
    if rec.get("colour"):
        return str(rec["colour"])
    for x in layers(chunk) if layers_ is None else layers_:
        if x["name"] == rec.get("layer"):
            return str(x.get("colour") or DEFAULT_COLOUR)
    return DEFAULT_COLOUR


def move(chunk, M, transform=None) -> int:
    """Every free drawing of ``chunk`` carried by the motion ``M`` (4 x 4, coordinates of the chunk); the basis
    takes ``transform`` (the chunk's transform after the motion; None: the one it has now). Returns how many moved;
    nothing written when there are none."""
    m = np.asarray(M, float).reshape(4, 4)
    moved = 0
    for rec in _stored(chunk):
        if not _valid(rec):
            continue
        xyz = geometry.apply(m, np.asarray(rec["xyz"], float).reshape(-1, 3))
        rec["xyz"] = [[float(v) for v in p] for p in xyz]
        moved += 1
    if _stored(chunk):
        _basis(chunk, transform)
    return moved


def follow(chunk) -> np.ndarray | None:
    """The free drawings of ``chunk`` after a change of its transform made inside the program (the georeference
    fitted again, an optimisation, the levelling of a local chunk, chunks aligned): in the alignment of their basis and
    with another transform, carried by ``M = T_now inv(T_basis)`` - they stay where they were on the model -; returns
    ``M``. In another alignment (a new one: its own frame) they stay in the coordinates of the chunk and the basis
    takes it (the orientation markers put the chunk back in its frame), said in the log. None when nothing moved. The
    listeners of :func:`..engine.orient.on_transform_changed` are not told here: they hear of the motions that carry
    the stores of the chunk (an orientation, its undo)."""
    from . import stamps

    if not _stored(chunk):
        return None
    basis = _drawings(chunk).get("free_basis") or {}
    now = np.asarray(chunk.transform, float)
    if not basis.get("transform"):
        _basis(chunk)
        return None
    if basis.get("alignment") != chunk.meta.get("alignment_id"):
        _basis(chunk)
        log.info("Disegni 3D lasciati nelle coordinate del chunk %s: nuovo allineamento", getattr(chunk, "label", ""))
        return None
    try:
        before = np.asarray(basis["transform"], float).reshape(4, 4)
    except (TypeError, ValueError):
        _basis(chunk)
        return None
    if not np.all(np.isfinite(before)) or not np.linalg.cond(before[:3, :3]) < SINGULAR_COND:
        _basis(chunk)  # no motion from a basis that is not a transform: taken again
        return None
    if stamps.same_transform(now, before):
        return None
    m = now @ np.linalg.inv(before)
    move(chunk, m)
    return m
