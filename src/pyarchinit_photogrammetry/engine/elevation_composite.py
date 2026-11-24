"""The composite of the campaigns of a wall: one elevation of a group (:mod:`.elevation_groups`) made from the
rasters of its campaigns - the wall as it was when it was most exposed.

The campaigns lie on the same grid (:func:`.elevation.build_like`), so their reliefs compare cell by cell. From the
most recent campaign to the oldest, a cell keeps the surface it holds unless an older campaign is deeper there by more
than ``threshold`` (:data:`DEEPER`, 0.15 m: a course taken down since, a fill dug out before it was surveyed again)
or the cell holds nothing yet; then that campaign's surface and colour take it. The colours of an older campaign are
matched to those of the most recent by mean and deviation in Lab (:func:`match_colours`), over the cells both have
within :data:`JOIN_PX` cells of where it joins (all the cells both have when those are under
:data:`JOIN_MIN_CELLS`).

The composite is an elevation of the reference chunk of the group (``kind`` "composite", made from rasters by
:func:`.elevation.entry_from_rasters`): drawable and exportable as any other, it refuses patches
(:class:`.patches.CompositePatched`). Its entry lists its ``sources`` (oldest first), the ``threshold``, the
``group`` and ``sources_raster``, ``sources.tif`` in its folder: uint8, 0 where no campaign has a surface, ``i + 1``
where the ``i``-th source gave it. The group records it (``group["composite"]``, with the stamp of each source,
:func:`.elevation.source_stamp`): a source built again, patched or removed, or a campaign added to the group, makes it
stale (:func:`is_stale`) - never changed in silence; made again it keeps its id, name and drawings.
"""
from __future__ import annotations

import logging

import numpy as np

from ..core.chunk import Chunk
from ..core.progress import Progress, as_progress
from . import elevation as E
from . import elevation_groups as G

log = logging.getLogger("pyarchinit_photogrammetry")

DEEPER = 0.15  # m: an older campaign takes a cell where its surface is deeper than this
JOIN_PX = 64  # cells: the colours are compared within this distance of where an older campaign joins
JOIN_MIN_CELLS = 1000  # fewer cells than this near the join: every cell both campaigns have is compared
DEVIATION_FLOOR = 1.0  # Lab units: the least deviation a colour channel is divided by
GRID_TOLERANCE = 1e-6  # relative: the grid of a campaign must be that of the composite


def _lab(rgb: np.ndarray) -> np.ndarray:
    import cv2

    img = np.ascontiguousarray(np.moveaxis(np.asarray(rgb), 0, -1), np.float32) / 255.0
    return cv2.cvtColor(img, cv2.COLOR_RGB2Lab)


def _rgb(lab: np.ndarray) -> np.ndarray:
    import cv2

    img = cv2.cvtColor(np.ascontiguousarray(lab, np.float32), cv2.COLOR_Lab2RGB)
    return np.moveaxis(np.clip(np.round(img * 255.0), 0, 255).astype(np.uint8), -1, 0)


def _floored(cov: np.ndarray) -> np.ndarray:
    w, v = np.linalg.eigh(cov)
    return (v * np.maximum(w, DEVIATION_FLOOR ** 2)) @ v.T


def _root(m: np.ndarray) -> np.ndarray:
    w, v = np.linalg.eigh((m + m.T) / 2.0)
    return (v * np.sqrt(np.maximum(w, 0.0))) @ v.T


def match_colours(rgb, reference, where) -> np.ndarray:
    """``rgb`` (3, h, w) uint8 with its colours matched to ``reference`` (the same shape) over the cells ``where``
    ((h, w) bool), in Lab: moved and stretched so that the mean and the deviation of L, a and b over ``where`` are
    those of ``reference`` there - the deviations taken together with how the three channels vary with each other
    (the linear transfer of the covariance: for channels that vary independently, the deviation of each channel
    matched on its own; matching them one by one when they do not would scatter the colours), each deviation
    floored at :data:`DEVIATION_FLOOR`. Every cell is changed the same way; ``rgb`` as it is when ``where`` has no
    cell."""
    rgb = np.asarray(rgb, np.uint8)
    where = np.asarray(where, bool)
    if not where.any():
        return rgb.copy()
    lab, ref = _lab(rgb), _lab(reference)
    mine, theirs = lab[where].astype(np.float64), ref[where].astype(np.float64)
    m_mine, m_theirs = mine.mean(axis=0), theirs.mean(axis=0)
    c_mine = _floored(np.cov(mine.T) if len(mine) > 1 else np.zeros((3, 3)))
    c_theirs = _floored(np.cov(theirs.T) if len(theirs) > 1 else np.zeros((3, 3)))
    half = _root(c_mine)
    inv = np.linalg.inv(half)
    t = inv @ _root(half @ c_theirs @ half) @ inv
    out = (lab.reshape(-1, 3).astype(np.float64) - m_mine) @ t + m_theirs
    return _rgb(out.reshape(lab.shape))


# ---------------------------------------------------------------- the sources
def _files(chunk, entry) -> bool:
    ortho, relief = E.elevation_paths(chunk, entry)
    return ortho.exists() and relief.exists()


def _sources(doc, chunk: Chunk, group: dict) -> list[dict]:
    """The members of ``group`` that can be a source: chunk and elevation there with their files, not a composite;
    by date, the oldest first (:func:`.elevation_groups.members`)."""
    out = []
    for m in G.members(doc, chunk, group):
        ch, entry = m["chunk"], m["entry"]
        if ch is None or entry is None or entry.get("kind") == "composite" or not _files(ch, entry):
            continue
        out.append(m)
    return out


def _like(chunk: Chunk, group: dict, sources: list[dict]) -> dict:
    """The elevation whose grid the composite takes: the group's reference when it is a source, else the most
    recent source."""
    for m in sources:
        if m["chunk"] is chunk and m["entry"]["id"] == group.get("reference"):
            return m["entry"]
    return sources[-1]["entry"]


def _read(chunk, entry, origin, shape, resolution):
    """The orthophoto (4, h, w) and the relief (h, w) of a campaign on the grid ``origin``/``shape``; None when its
    files are on another grid (another resolution, or shifted)."""
    import rasterio

    ortho, relief = E.elevation_paths(chunk, entry)
    with rasterio.open(ortho) as a, rasterio.open(relief) as b:
        for src in (a, b):
            t = src.transform
            tol = GRID_TOLERANCE * max(1.0, abs(origin[0]), abs(origin[1]))
            if (src.height, src.width) != tuple(shape) or abs(t.c - origin[0]) > tol or abs(t.f - origin[1]) > tol \
                    or abs(abs(t.a) - resolution) > 1e-9 or abs(abs(t.e) - resolution) > 1e-9 or t.b or t.d:
                return None
        rgba = a.read()
        d = b.read(1).astype(np.float32)
        if b.nodata is not None and np.isfinite(b.nodata):
            d[d == b.nodata] = np.nan
    if rgba.shape[0] == 3:
        rgba = np.concatenate([rgba, np.full((1, *rgba.shape[1:]), 255, np.uint8)])
    return rgba.astype(np.uint8), d


def _join(taken: np.ndarray, common: np.ndarray) -> np.ndarray:
    """The cells both campaigns have within :data:`JOIN_PX` cells of the cells ``taken`` (not among them); all
    those both have when these are fewer than :data:`JOIN_MIN_CELLS`."""
    from scipy import ndimage

    near = ndimage.distance_transform_edt(~taken) <= JOIN_PX
    where = common & near & ~taken
    if where.sum() < JOIN_MIN_CELLS:
        where = common
    return where


def composite(doc, chunk: Chunk, group: dict, threshold: float = DEEPER, name: str | None = None,
              match: bool = True, progress: Progress | None = None) -> dict:
    """The composite of the campaigns of ``group`` (kept in ``chunk``, the reference chunk), written in ``chunk``:
    per cell the surface of the most recent campaign, replaced by an older one deeper by more than ``threshold``
    (metres) or where the cell holds nothing; ``match``: the colours of the older campaigns matched to the most
    recent (:func:`match_colours`). Made again (``group["composite"]`` names an elevation of ``chunk``), it replaces
    that elevation, its id, name (unless ``name``) and drawings kept. Returns the entry; ``ValueError`` with fewer
    than two campaigns with their files on the grid."""
    progress = as_progress(progress)
    threshold = float(threshold)
    if not np.isfinite(threshold) or threshold < 0.0:
        raise ValueError("the threshold is a depth in metres, zero or more")
    sources = _sources(doc, chunk, group)
    if not sources:
        raise ValueError("a composite needs two campaigns with their files: the group has none")
    like = _like(chunk, group, sources)
    origin, shape = E.grid_of(like)
    resolution = float(like["resolution"])
    read, left_out = [], []
    for i, m in enumerate(sources):
        progress(0.6 * i / len(sources), f"Reading {m['label']}")
        rasters = _read(m["chunk"], m["entry"], origin, shape, resolution)
        if rasters is None:
            log.warning("Composite: %s (%s) is on another grid, left out", m["label"], m["entry"].get("name"))
            left_out.append({"chunk": m["chunk"].key, "entry": m["entry"]["id"]})
            continue
        read.append((m, *rasters))
    if len(read) < 2:
        raise ValueError(f"a composite needs two campaigns with their files on the same grid: the group has "
                         f"{len(read)}")
    n = len(read)
    _m, recent_rgba, recent_d = read[-1]
    rgba = recent_rgba.copy()
    d = recent_d.copy()
    src = np.where(np.isfinite(d), n, 0).astype(np.uint8)
    for i in range(n - 2, -1, -1):  # from the most recent to the oldest
        m, old_rgba, old_d = read[i]
        progress(0.6 + 0.3 * (n - 2 - i) / max(1, n - 1), f"Composite: {m['label']}")
        have = np.isfinite(old_d)
        with np.errstate(invalid="ignore"):
            taken = have & (~np.isfinite(d) | (old_d < d - threshold))
        if not taken.any():
            continue
        colours = old_rgba[:3]
        if match:
            common = (old_rgba[3] > 0) & (recent_rgba[3] > 0)
            colours = match_colours(colours, recent_rgba[:3], _join(taken, common))
        rgba[:3, taken] = colours[:, taken]
        rgba[3, taken] = old_rgba[3, taken]
        d[taken] = old_d[taken]
        src[taken] = i + 1
    progress(0.92, "Writing the composite")
    record = group.get("composite") or {}
    old = next((e for e in E.elevations(chunk) if e.get("id") == record.get("entry")), None)
    listed = [{"chunk": m["chunk"].key, "entry": m["entry"]["id"]} for m, _r, _d in read]
    extra = {"sources": listed, "threshold": threshold, "group": group["id"], "colours_matched": bool(match)}
    entry = E.entry_from_rasters(chunk, like, rgba, d, name=name or (None if old is not None else
                                                                       f"{group.get('name') or 'Muro'} - composito"),
                                 kind="composite", extra=extra, replace=old)
    path = _write_sources(chunk, entry, src)
    entry["sources_raster"] = str(path.relative_to(chunk.data_dir))
    group["composite"] = {"entry": entry["id"], "threshold": threshold,
                          "sources": [{**s, "stamp": E.source_stamp(m["entry"])} for s, (m, _r, _d) in
                                      zip(listed, read)], "left_out": left_out}
    progress(1.0, f"Composite of {n} campaigns")
    return entry


def _write_sources(chunk: Chunk, entry: dict, src: np.ndarray):
    """``sources.tif`` beside the orthophoto of ``entry``: which source gave each cell (uint8, 0 none)."""
    import rasterio

    ortho = E.elevation_paths(chunk, entry)[0]
    path = ortho.parent / "sources.tif"
    with rasterio.open(ortho) as ref:
        transform, tags = ref.transform, ref.tags()
    h, w = src.shape
    tiled = {"tiled": True, "blockxsize": 256, "blockysize": 256} if h >= 256 and w >= 256 else {}
    with rasterio.open(path, "w", driver="GTiff", height=h, width=w, count=1, dtype="uint8", transform=transform,
                       compress="deflate", BIGTIFF="IF_SAFER", **tiled) as dst:
        dst.update_tags(**{k: v for k, v in tags.items() if k.startswith("ppg_")})
        dst.write(src[None])
    return path


# ---------------------------------------------------------------- stale
def _group_of(chunk: Chunk, entry: dict):
    return next((g for g in G.groups(chunk) if g.get("id") == entry.get("group")), None)


def is_stale(doc, chunk: Chunk, entry: dict) -> bool:
    """Whether the composite ``entry`` of ``chunk`` is to be made again: its group or its record gone, a source gone
    (its chunk, its elevation or its files), built again or patched since (its stamp another), or the campaigns of
    the group with files no longer its sources (one added, one removed from the group; those it left out for
    another grid are not counted)."""
    group = _group_of(chunk, entry)
    record = (group or {}).get("composite") or {}
    if group is None or record.get("entry") != entry.get("id"):
        return True
    by_key = {c.key: c for c in doc.chunks}
    for s in record.get("sources") or []:
        ch = by_key.get(s.get("chunk"))
        source = None if ch is None else next((e for e in E.elevations(ch) if e.get("id") == s.get("entry")), None)
        if source is None or not _files(ch, source) or E.source_stamp(source) != s.get("stamp"):
            return True
    now = [(m["chunk"].key, m["entry"]["id"]) for m in _sources(doc, chunk, group)]
    was = [(s.get("chunk"), s.get("entry")) for s in (record.get("sources") or []) + (record.get("left_out") or [])]
    return sorted(now, key=str) != sorted(was, key=str)


def composites(chunk: Chunk) -> list[dict]:
    """The composites among the elevations of ``chunk``."""
    return [e for e in E.elevations(chunk) if e.get("kind") == "composite"]
