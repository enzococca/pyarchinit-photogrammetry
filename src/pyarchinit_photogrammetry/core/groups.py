"""Groups of chunks: chunks gathered under a name in the project (a year of flights, the blocks of one dive, the
sectors of an excavation), kept in ``doc.meta["chunk_groups"]`` as a list of ``{"id", "name", "chunks"}`` (chunk keys,
in the order the user gave them).

A chunk is in one group at most. The groups are data of the project and change nothing by themselves: the program
uses them as a unit - the workspace shows a group as a folder of its chunks (shown or hidden together in the 3D
view), and an operation asked on a group runs on its chunks (merge, export, DEM and orthophoto in a batch). In the ties
between chunks (:func:`..engine.ties.solve`) a chunk whose unknowns are :data:`..engine.ties.GROUP` moves with its
group as one block: the relative poses of the chunks inside the group are kept.

Compatibility: an older version of the program reads the project and ignores the key (it keeps ``doc.meta`` as a
whole, so the groups come back once the project is opened again here); the chunks are never inside the groups, so
nothing of them depends on this key. The keys of chunks that are not in the project any more are kept while the
program runs (a removal undone brings the chunk back into its group) and dropped when the project is opened
(:func:`prune`), so that a later chunk given the same key never joins a group by chance.
"""
from __future__ import annotations

import copy
import re
import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .chunk import Chunk
    from .document import Document

META_KEY = "chunk_groups"
LABEL = "Gruppo"  # the name of a new group: data of the project (as the labels of the ties), not UI text
_NUMBERED = re.compile(rf"^{LABEL} (\d+)$")


class GroupError(ValueError):
    """A group that does not exist, a name taken, a chunk that does not exist."""


def stored(doc: "Document") -> list[dict]:
    """The groups as written in the project (keys of chunks removed included); a broken value reads as none."""
    raw = doc.meta.get(META_KEY)
    if not isinstance(raw, list):
        return []
    out = []
    for g in raw:
        if not isinstance(g, dict) or "id" not in g:
            continue
        keys = []
        for k in g.get("chunks") or []:
            try:
                keys.append(int(k))
            except (TypeError, ValueError):
                continue
        out.append({"id": str(g["id"]), "name": str(g.get("name") or g["id"]), "chunks": keys})
    return out


def _write(doc: "Document", groups: list[dict]) -> None:
    if groups:
        doc.meta[META_KEY] = [{"id": g["id"], "name": g["name"], "chunks": [int(k) for k in g["chunks"]]}
                              for g in groups]
    else:
        doc.meta.pop(META_KEY, None)


def groups_of(doc: "Document") -> list[dict]:
    """The groups of the project (copies): ``{"id", "name", "chunks"}`` with only the chunks that are in the project,
    in the order the group keeps them."""
    present = {c.key for c in doc.chunks}
    out = []
    for g in stored(doc):
        g["chunks"] = [k for k in g["chunks"] if k in present]
        out.append(g)
    return out


def prune(doc: "Document") -> bool:
    """The keys of chunks not in the project dropped from the groups (on open: see the module). True when something
    changed; empty groups stay (a group the user made and has not filled yet)."""
    before = stored(doc)
    after = groups_of(doc)
    if before == after:
        return False
    _write(doc, after)
    return True


def _chunk_key(doc: "Document", item) -> int:
    from ..engine.chunks import resolve_chunk

    try:
        return resolve_chunk(doc, item).key
    except (KeyError, ValueError) as exc:
        raise GroupError(str(exc.args[0] if exc.args else exc)) from None


def group(doc: "Document", item) -> dict:
    """A group by id or by name (a copy, with only the chunks in the project)."""
    if isinstance(item, dict):
        item = item.get("id")
    groups = groups_of(doc)
    found = [g for g in groups if g["id"] == str(item)] or [g for g in groups if g["name"] == str(item)]
    if not found:
        names = ", ".join(repr(g["name"]) for g in groups) or "none"
        raise GroupError(f"no group {item!r}; the groups: {names}")
    return found[0]


def group_of(doc: "Document", chunk) -> dict | None:
    """The group of ``chunk`` (object or key), or None."""
    key = chunk if isinstance(chunk, int) else getattr(chunk, "key", None)
    return next((g for g in groups_of(doc) if key in g["chunks"]), None)


def members(doc: "Document", item) -> list["Chunk"]:
    """The chunks of a group, in the order of the group."""
    by_key = {c.key: c for c in doc.chunks}
    return [by_key[k] for k in group(doc, item)["chunks"]]


def next_name(doc: "Document") -> str:
    """"Gruppo N", N the next free number."""
    taken = [int(m.group(1)) for g in stored(doc) if (m := _NUMBERED.match(g["name"]))]
    return f"{LABEL} {max(taken, default=0) + 1}"


def add_group(doc: "Document", name: str | None = None, chunks=()) -> dict:
    """A new group named ``name`` (default "Gruppo N") with ``chunks`` (objects, keys or labels; each taken out of the
    group it was in). :class:`GroupError` when the name is taken."""
    name = (name or "").strip() or next_name(doc)
    groups = stored(doc)
    if any(g["name"] == name for g in groups):
        raise GroupError(f"a group is named {name!r} already")
    keys = list(dict.fromkeys(_chunk_key(doc, c) for c in chunks))
    for g in groups:
        g["chunks"] = [k for k in g["chunks"] if k not in keys]
    taken = {g["id"] for g in groups}
    gid = next(i for i in (uuid.uuid4().hex[:8] for _ in range(100)) if i not in taken)
    new = {"id": gid, "name": name, "chunks": keys}
    _write(doc, groups + [new])
    return copy.deepcopy(new)


def rename_group(doc: "Document", item, name: str) -> dict:
    name = (name or "").strip()
    if not name:
        raise GroupError("a group needs a name")
    gid = group(doc, item)["id"]
    groups = stored(doc)
    if any(g["name"] == name and g["id"] != gid for g in groups):
        raise GroupError(f"a group is named {name!r} already")
    for g in groups:
        if g["id"] == gid:
            g["name"] = name
    _write(doc, groups)
    return group(doc, gid)


def remove_group(doc: "Document", item) -> dict:
    """The group gone; its chunks stay in the project, out of any group. Returns the group removed."""
    gone = group(doc, item)
    _write(doc, [g for g in stored(doc) if g["id"] != gone["id"]])
    return gone


def assign(doc: "Document", chunks, item=None, before=None) -> dict | None:
    """``chunks`` (objects, keys or labels) moved into the group ``item`` (id or name; None: out of every group),
    placed before the chunk ``before`` of that group when given (else at its end). Returns the group (None)."""
    keys = list(dict.fromkeys(_chunk_key(doc, c) for c in chunks))
    target = group(doc, item)["id"] if item is not None else None
    anchor = _chunk_key(doc, before) if before is not None else None
    groups = stored(doc)
    for g in groups:
        g["chunks"] = [k for k in g["chunks"] if k not in keys]
        if g["id"] == target:
            at = g["chunks"].index(anchor) if anchor in g["chunks"] else len(g["chunks"])
            g["chunks"][at:at] = keys
    _write(doc, groups)
    return group(doc, target) if target is not None else None


def leaders(doc: "Document", kinds: dict[int, str], grouped: str = "group") -> dict[int, int]:
    """For a solve of the ties: each chunk whose unknowns are ``grouped`` -> the chunk of its group it moves with (the
    first chunk of the group, in the order of the group, whose unknowns are not ``grouped``; chunks out of the solve -
    not in ``kinds`` - do not count). :class:`GroupError` for a chunk in no group, or in a group with no chunk that
    has unknowns of its own."""
    out: dict[int, int] = {}
    labels = {c.key: c.label for c in doc.chunks}
    for key, kind in kinds.items():
        if kind != grouped:
            continue
        g = group_of(doc, key)
        if g is None:
            raise GroupError(f"chunk {labels.get(key, key)} moves with its group, but it is in no group")
        lead = next((k for k in g["chunks"] if k in kinds and kinds[k] != grouped), None)
        if lead is None:
            raise GroupError(f"group {g['name']!r}: no chunk with unknowns of its own (give one chunk of the group its "
                             "unknowns, the others move with it)")
        out[key] = lead
    return out
