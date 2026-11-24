"""The same elevation of a wall on several chunks - the campaigns of that wall, surveyed in different years and
already placed together - as a **group**: :func:`.elevation.build_like` on each chunk, so their orthophotos and
reliefs lie on the same pixels, and one record that names them with the date of each campaign.

The group is kept in the chunk the wall was traced on (the reference), under ``chunk.meta["elevation_groups"]``::

    {"id": "g" + 8 hex, "name": the name of the reference elevation, "reference": its id,
     "members": [{"chunk": chunk key, "entry": elevation id, "date": "YYYY-MM-DD" | None, "label": chunk label}],
     "composite": None}

Members are ``(chunk key, elevation id)`` pairs: an elevation removed from its chunk leaves its record in the group
(:func:`members` gives it with ``entry`` None) until a build on that chunk replaces it. The date of a campaign is
:func:`campaign_date` unless given; the group keeps its own dates (:func:`set_date`), it never writes the date of a
chunk."""
from __future__ import annotations

import logging
import shutil
import uuid
from datetime import datetime

import numpy as np

from ..core.chunk import Chunk
from ..core.progress import Progress, as_progress
from . import elevation as E

log = logging.getLogger("pyarchinit_photogrammetry")

KEY = "elevation_groups"
ALREADY = "already in the group"


# ---------------------------------------------------------------- dates
def parse_date(text) -> str | None:
    """``YYYY-MM-DD`` of an ISO date or date and time, None for nothing; ``ValueError`` for anything else."""
    if text is None or str(text).strip() == "":
        return None
    try:
        return datetime.fromisoformat(str(text).strip()).date().isoformat()
    except ValueError:
        raise ValueError(f"a date is written YYYY-MM-DD, not {text!r}") from None


def campaign_date(chunk: Chunk) -> str | None:
    """The date of the campaign of ``chunk`` (``YYYY-MM-DD``): ``chunk.meta["date"]`` when it was set by hand (the
    date of the phase, :func:`..archaeo.timeline.set_phase_date`), else the median of the EXIF dates of its photos
    (the earlier of the two middle ones for an even count), None when there is neither."""
    own = (chunk.meta or {}).get("date")
    if own:
        try:
            return parse_date(own)
        except ValueError:
            pass
    dates = []
    for cam in chunk.cameras:
        s = (cam.meta or {}).get("datetime")
        if not s:
            continue
        try:
            dates.append(datetime.strptime(str(s).strip()[:19], "%Y:%m:%d %H:%M:%S"))
        except ValueError:
            continue
    if not dates:
        return None
    dates.sort()
    return dates[(len(dates) - 1) // 2].date().isoformat()


# ---------------------------------------------------------------- the record
def groups(chunk: Chunk) -> list[dict]:
    """The groups kept in ``chunk`` (those whose wall was traced on it)."""
    return (chunk.meta or {}).get(KEY) or []


def _has(group: dict, key, eid: str) -> bool:
    return any(m.get("chunk") == key and m.get("entry") == eid for m in group.get("members") or [])


def group_of(doc, chunk: Chunk, eid: str):
    """``(reference chunk, group)`` of the group that has the elevation ``eid`` of ``chunk`` among its members,
    looked up in every chunk of ``doc``; None when it is in none."""
    for ref in doc.chunks:
        for group in groups(ref):
            if _has(group, chunk.key, eid):
                return ref, group
    return None


def add_member(group: dict, chunk: Chunk, entry: dict, date=None) -> dict:
    """The member record of the elevation ``entry`` of ``chunk``, appended to ``group``; ``date`` (``YYYY-MM-DD``)
    by default :func:`campaign_date`."""
    record = {"chunk": chunk.key, "entry": entry["id"],
              "date": parse_date(date) if date is not None else campaign_date(chunk), "label": chunk.label}
    group.setdefault("members", []).append(record)
    return record


def new_group(chunk: Chunk, entry: dict) -> dict:
    """The group of the elevation ``entry`` of ``chunk``: the one kept in ``chunk`` that has it among its members,
    else a new one, kept in ``chunk``, with ``entry`` its first member."""
    for group in groups(chunk):
        if _has(group, chunk.key, entry["id"]):
            return group
    group = {"id": "g" + uuid.uuid4().hex[:8], "name": entry.get("name"), "reference": entry["id"],
             "members": [], "composite": None}
    add_member(group, chunk, entry)
    chunk.meta.setdefault(KEY, []).append(group)
    return group


def set_date(group: dict, chunk_key, eid: str, date) -> None:
    """The date of the member ``(chunk_key, eid)`` of ``group`` (``YYYY-MM-DD``, None for none); ``KeyError`` when it
    is no member, ``ValueError`` for a date written otherwise. The date of the chunk itself is left as it is."""
    day = parse_date(date)
    for m in group.get("members") or []:
        if m.get("chunk") == chunk_key and m.get("entry") == eid:
            m["date"] = day
            return
    raise KeyError(f"no member {chunk_key}/{eid} in the group {group.get('id')}")


def members(doc, chunk: Chunk, group: dict) -> list[dict]:
    """The members of ``group`` (kept in ``chunk``) resolved in ``doc``: ``chunk`` (the object, None when gone),
    ``entry`` (the elevation, None when it was removed), ``date``, ``label`` and ``record`` (the member record
    itself), by date - the undated last - and in the order of the chunks of the project for the same date."""
    order = {c.key: i for i, c in enumerate(doc.chunks)}
    by_key = {c.key: c for c in doc.chunks}
    out = []
    for m in group.get("members") or []:
        ch = by_key.get(m.get("chunk"))
        entry = None if ch is None else next((e for e in E.elevations(ch) if e.get("id") == m.get("entry")), None)
        out.append({"chunk": ch, "entry": entry, "date": m.get("date"),
                    "label": ch.label if ch is not None else m.get("label"), "record": m})
    return sorted(out, key=lambda r: (r["date"] is None, r["date"] or "",
                                      order.get(r["record"].get("chunk"), len(order))))


# ---------------------------------------------------------------- the build
def _refusal(source: Chunk, entry: dict, chunk: Chunk, same_frame: bool) -> str | None:
    """Why ``build_like`` would refuse the elevation ``entry`` of ``source`` on ``chunk`` (the same words), None
    when it would not."""
    from ..core import crs as crs_mod

    model = chunk.product_path("model")
    if not model or not model.exists():
        return "the chunk has no model"
    theirs, mine = entry.get("crs") or getattr(source, "crs", None), getattr(chunk, "crs", None)
    if not E.same_crs(theirs, mine):
        return f"the elevation is in {theirs}, the chunk in {mine}: the same grid needs the same reference system"
    if chunk is not source and crs_mod.is_local(mine) and not same_frame:
        return ("both chunks are in a local frame: their worlds need not agree; if they are in the same frame "
                "(aligned, merged, oriented on the same markers) say it (same_frame)")
    return None


def _empty(chunk: Chunk, entry: dict) -> bool:
    """Whether the relief of an elevation has no finite cell."""
    import rasterio

    with rasterio.open(E.elevation_paths(chunk, entry)[1]) as src:
        for _ij, window in src.block_windows(1):
            if np.isfinite(src.read(1, window=window)).any():
                return False
    return True


def _folders(chunk: Chunk) -> set[str]:
    root = chunk.data_dir / "elevations"
    return {p.name for p in root.iterdir()} if root.exists() else set()


def build_on_chunks(doc, chunk: Chunk, entry: dict, chunks, same_frame: bool = False, dates: dict | None = None,
                    progress: Progress | None = None, workers: int | None = None) -> dict:
    """The elevation ``entry`` of ``chunk`` built again on each of ``chunks`` in turn on its frame and grid
    (:func:`.elevation.build_like`), each build a member of the group of ``entry`` (:func:`group_of`, else
    :func:`new_group` on ``chunk`` once a first build is done), the group written after every chunk.

    Returns ``{"group", "built": [(chunk, entry)], "refused": [{"chunk": label, "reason"}], "empty": [labels]}``:
    refused, a chunk whose elevation is already a member (``already in the group``), one in another reference
    system, two local chunks without ``same_frame``, a chunk without a model; empty, a chunk whose relief has no
    finite cell on the grid (the wall is not in its model): that build is removed again. A build that fails or is
    cancelled leaves no entry and no folder behind and its exception goes on; the chunks built before it stay in
    the group. ``dates``: ``{chunk key: "YYYY-MM-DD"}`` in place of :func:`campaign_date`."""
    progress = as_progress(progress)
    dates = dates or {}
    chunks = list(chunks)
    found = group_of(doc, chunk, entry["id"])
    group = found[1] if found is not None else None
    out = {"group": group, "built": [], "refused": [], "empty": []}
    for i, ch in enumerate(chunks):
        taken = group["members"] if group is not None else [{"chunk": chunk.key, "entry": entry["id"]}]
        live = [m for m in taken if m.get("chunk") == ch.key
                and any(e.get("id") == m.get("entry") for e in E.elevations(ch))]
        reason = ALREADY if live else _refusal(chunk, entry, ch, same_frame)
        if reason is not None:
            out["refused"].append({"chunk": ch.label, "reason": reason})
            log.info("Elevation %s on %s refused: %s", entry.get("name"), ch.label, reason)
            continue
        before = _folders(ch)
        n = len(E.elevations(ch))
        try:
            new = E.build_like(ch, entry, progress=progress.sub(i / len(chunks), 1.0 / len(chunks)),
                               workers=workers, source=chunk, same_frame=same_frame)
        except E.NoSurface:
            new = None
        except BaseException:
            _clean(ch, before, n)
            raise
        if new is None or _empty(ch, new):
            if new is not None:
                E.remove_elevation(ch, new["id"])
            _clean(ch, before, n)
            out["empty"].append(ch.label)
            log.info("Elevation %s on %s: the wall is not in its model", entry.get("name"), ch.label)
            continue
        if group is None:
            group = out["group"] = new_group(chunk, entry)
        group["members"] = [m for m in group["members"] if m.get("chunk") != ch.key]  # a removed one replaced
        day = dates.get(ch.key, dates.get(str(ch.key)))
        add_member(group, ch, new, day)
        out["built"].append((ch, new))
    return out


def _clean(chunk: Chunk, before: set[str], count: int) -> None:
    """The folders and entries a build left in ``chunk`` taken away: the folders not in ``before``, the entries
    past the first ``count``."""
    entries = E.elevations(chunk)
    if len(entries) > count:
        chunk.meta["elevations"] = entries[:count]
    for name in _folders(chunk) - before:
        shutil.rmtree(chunk.data_dir / "elevations" / name, ignore_errors=True)
