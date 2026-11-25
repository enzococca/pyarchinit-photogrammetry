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
stale (:func:`is_stale`) - never changed in silence; made again it keeps its id, name and drawings. Every campaign
needs a date (:class:`Undated` otherwise): the order decides which one is the surface and the colours."""
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
GAIN_LIMITS = (0.5, 2.0)  # the least and the most the colours of a campaign are stretched (eigenvalues of the gain)
MATCH_MIN_CELLS = 200  # fewer cells in common than this: only the mean of the colours is moved
MAX_SOURCES = 254  # sources.tif is uint8: 0 for none, i + 1 for the i-th source


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


def match_colours(rgb, reference, where, info: dict | None = None) -> np.ndarray:
    """``rgb`` (3, h, w) uint8 with its colours matched to ``reference`` (the same shape) over the cells ``where``
    ((h, w) bool), in Lab: moved and stretched so that the mean and the deviation of L, a and b over ``where`` are
    those of ``reference`` there - the deviations taken together with how the three channels vary with each other
    (the linear transfer of the covariance: for channels that vary independently, the deviation of each channel
    matched on its own; matching them one by one when they do not would scatter the colours), each deviation
    floored at :data:`DEVIATION_FLOOR`. The stretch is held within :data:`GAIN_LIMITS` (shared cells almost flat
    would otherwise stretch everything else to black and white); with fewer than :data:`MATCH_MIN_CELLS` cells in
    ``where`` only the mean is moved. Every cell is changed the same way; ``rgb`` as it is when ``where`` has no
    cell.

    ``info`` (a dict, filled in): ``cells`` (the cells compared) and ``mode``: "none" (no cell), "mean" (too few
    cells: the mean only), "clamped" (the stretch held at its limits) or "full"."""
    rgb = np.asarray(rgb, np.uint8)
    where = np.asarray(where, bool)
    cells = int(where.sum())
    if info is not None:
        info.update(cells=cells, mode="none")
    if not cells:
        return rgb.copy()
    lab, ref = _lab(rgb), _lab(reference)
    mine, theirs = lab[where].astype(np.float64), ref[where].astype(np.float64)
    m_mine, m_theirs = mine.mean(axis=0), theirs.mean(axis=0)
    if cells < MATCH_MIN_CELLS:
        t, mode = np.eye(3), "mean"
    else:
        c_mine = _floored(np.cov(mine.T))
        c_theirs = _floored(np.cov(theirs.T))
        half = _root(c_mine)
        inv = np.linalg.inv(half)
        t = inv @ _root(half @ c_theirs @ half) @ inv
        w, v = np.linalg.eigh((t + t.T) / 2.0)
        held = np.clip(w, *GAIN_LIMITS)
        mode = "clamped" if not np.allclose(held, w) else "full"
        t = (v * held) @ v.T
    if info is not None:
        info["mode"] = mode
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


UNDATED_EN = "give a date to {0}: the composite orders the campaigns by date (the most recent is its surface and " \
             "the reference of the colours)"
UNDATED_IT = "Dai una data a «{0}»: il composito ordina le campagne per data (la più recente ne è la superficie e il " \
             "riferimento dei colori)"


class Undated(ValueError):
    """A composite asked of a group with a campaign without a date: ``str()`` English, ``italian`` Italian; ``labels``
    the campaigns without one."""

    def __init__(self, labels):
        self.labels = list(labels)
        names = ", ".join(str(x) for x in self.labels)
        self.english, self.italian = UNDATED_EN.format(names), UNDATED_IT.format(names)
        super().__init__(self.english)


def undated(doc, chunk: Chunk, group: dict) -> list[str]:
    """The labels of the campaigns of ``group`` (sources of a composite, :func:`_sources`) that have no date."""
    return [str(m["label"]) for m in _sources(doc, chunk, group) if not m.get("date")]


def default_name(group: dict, like: dict, dates) -> str:
    """The name of a composite when none is given: the wall's (the group's, else that of the elevation whose grid
    it takes) and the years of its campaigns - no words, nothing to translate."""
    base = group.get("name") or like.get("name") or group.get("id")
    years = sorted({str(d)[:4] for d in dates if d})
    span = years[0] if len(years) == 1 else f"{years[0]}-{years[-1]}" if years else ""
    return f"{base} {span}".strip()


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
    missing = [str(m["label"]) for m in sources if not m.get("date")]
    if missing:
        raise Undated(missing)
    if len(sources) > MAX_SOURCES:
        raise ValueError(f"a composite takes at most {MAX_SOURCES} campaigns (its sources raster is 8-bit): the "
                         f"group has {len(sources)}")
    like = _like(chunk, group, sources)
    origin, shape = E.grid_of(like)
    resolution = float(like["resolution"])
    read, left_out = [], []
    for i, m in enumerate(sources):
        progress(0.6 * i / len(sources), f"Reading {m['label']}")
        rasters = _read(m["chunk"], m["entry"], origin, shape, resolution)
        if rasters is None:
            log.warning("Composite: %s (%s) is on another grid, left out", m["label"], m["entry"].get("name"))
            left_out.append({"chunk": m["chunk"].key, "entry": m["entry"]["id"], "date": m.get("date"),
                             "stamp": E.source_stamp(m["entry"])})
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
    matched = {}
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
            info = {}
            colours = match_colours(colours, recent_rgba[:3], _join(taken, common), info=info)
            matched[i] = info
            if info["mode"] in ("mean", "none"):
                log.warning("Composite: %s has %d cells in common with the most recent campaign: %s", m["label"],
                            info["cells"], "only the mean of its colours moved" if info["cells"] else
                            "its colours left as they are")
        rgba[:3, taken] = colours[:, taken]
        rgba[3, taken] = old_rgba[3, taken]
        d[taken] = old_d[taken]
        src[taken] = i + 1
    progress(0.92, "Writing the composite")
    record = group.get("composite") or {}
    old = next((e for e in E.elevations(chunk) if e.get("id") == record.get("entry")), None)
    listed = [{"chunk": m["chunk"].key, "entry": m["entry"]["id"]} for m, _r, _d in read]
    extra = {"sources": listed, "threshold": threshold, "group": group["id"], "colours_matched": bool(match)}
    if not name and old is None:
        name = default_name(group, like, [m.get("date") for m, _r, _d in read])
    entry = E.entry_from_rasters(chunk, like, rgba, d, name=name or None, kind="composite", extra=extra,
                                 replace=old)
    path = _write_sources(chunk, entry, src)
    entry["sources_raster"] = str(path.relative_to(chunk.data_dir))
    recorded = []
    for i, (s, (m, _r, _d)) in enumerate(zip(listed, read)):
        r = {**s, "date": m.get("date"), "stamp": E.source_stamp(m["entry"])}
        if i in matched:
            r["colours"] = matched[i]
        recorded.append(r)
    group["composite"] = {"entry": entry["id"], "threshold": threshold, "sources": recorded, "left_out": left_out,
                          "order": [[m["chunk"].key, m["entry"]["id"], m.get("date")] for m in sources]}
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
    (its chunk, its elevation or its files), built again or patched since (its stamp another; also for a campaign it
    left out for another grid, so that one built again on the right grid counts), or the campaigns of the group
    with files no longer those it was made of, in the same order and with the same dates (one added or removed, a
    date changed: the most recent campaign gives the surface and the reference of the colours). A record made before
    the order was kept compares the campaigns without their order."""
    group = _group_of(chunk, entry)
    record = (group or {}).get("composite") or {}
    if group is None or record.get("entry") != entry.get("id"):
        return True
    by_key = {c.key: c for c in doc.chunks}
    for s in (record.get("sources") or []) + [s for s in record.get("left_out") or [] if "stamp" in s]:
        ch = by_key.get(s.get("chunk"))
        source = None if ch is None else next((e for e in E.elevations(ch) if e.get("id") == s.get("entry")), None)
        if source is None or not _files(ch, source) or E.source_stamp(source) != s.get("stamp"):
            return True
    sources = _sources(doc, chunk, group)
    if "order" in record:  # the campaigns in their order: a date that swaps the recent and the older counts
        return [[m["chunk"].key, m["entry"]["id"], m.get("date")] for m in sources] != record["order"]
    now = [(m["chunk"].key, m["entry"]["id"]) for m in sources]
    was = [(s.get("chunk"), s.get("entry")) for s in (record.get("sources") or []) + (record.get("left_out") or [])]
    return sorted(now, key=str) != sorted(was, key=str)


def composites(chunk: Chunk) -> list[dict]:
    """The composites among the elevations of ``chunk``."""
    return [e for e in E.elevations(chunk) if e.get("kind") == "composite"]
