"""Coordinate reference systems (thin wrapper around pyproj)."""
from __future__ import annotations

import functools

import numpy as np

LOCAL = "LOCAL"  # arbitrary local engineering frame (metres)


def is_local(crs: str | None) -> bool:
    return crs in (None, "", LOCAL)


def to_pyproj(crs: str):
    from pyproj import CRS

    return CRS.from_user_input(crs)


@functools.lru_cache(maxsize=256)
def horizontal(crs: str | None) -> str | None:
    """``crs`` without heights of its own: the horizontal part of a compound system (its EPSG code, else its WKT) -
    through which heights pass as they are - and any other system (or a local frame) as it is. Kept: reading a compound
    system takes a millisecond, and the drawings of a chunk ask it for every geometry."""
    if is_local(crs):
        return crs
    system = to_pyproj(crs)
    if not system.is_compound:
        return crs
    part = system.sub_crs_list[0]
    code = part.to_epsg()
    return f"EPSG:{code}" if code else part.to_wkt()


def epsg_code(crs: str | None) -> int | None:
    """The EPSG integer under which a consumer of 2D geometries - a layer of a database (PyArchInit), a GeoPackage
    (QGIS, QField, zones, measuring points), a QGIS project, IFC - takes the positions of ``crs``: for a compound system
    (``EPSG:7792+3855``, heights above a geoid), which has no code of its own (or one no such consumer knows), the code
    of its horizontal part (:func:`horizontal`), the heights staying in the attributes that carry them (a spot height's
    ``quota_q``, a z column). ``None`` for a local frame, a system PROJ cannot read and a plan without an EPSG code."""
    if is_local(crs):
        return None
    try:
        return to_pyproj(horizontal(crs)).to_epsg()
    except Exception:
        return None


def vertical_part(crs: str | None) -> str | None:
    """The heights of a compound system as their system is named: ``EPSG:3855`` (its EPSG code), else its name; None
    for a system without a vertical part (a local frame, one PROJ cannot read)."""
    if is_local(crs):
        return None
    try:
        system = to_pyproj(crs)
    except Exception:
        return None
    if system.is_bound:
        system = system.source_crs
    part = next((p for p in (system.sub_crs_list if system.is_compound else ()) if p.is_vertical), None)
    if part is None:
        return None
    code = part.to_epsg()
    return f"EPSG:{code}" if code else part.name


def wkt(crs: str | None) -> str | None:
    if is_local(crs):
        return None
    return to_pyproj(crs).to_wkt()


def is_geographic(crs: str | None) -> bool:
    return not is_local(crs) and to_pyproj(crs).is_geographic


def transform_points(points: np.ndarray, src: str, dst: str, epoch=None, legacy=None) -> np.ndarray:
    """Transform ``(N,3)`` points between two CRS. When one of the two is in ITRF2020, a frame where positions move
    with time (a GNSS position against a permanent station or PPP), the points go through
    :func:`..gnss.frames.transform` at their ``epoch``: a decimal year for all of them, or one for each (PROJ alone
    would take a ballpark, or the epoch 2015.0).

    Between two European frames (ETRF2014 or ETRF2020 and ETRF2000 - RDN2008, ETRS89 -: :func:`..gnss.frames.european_hop`)
    the points go through ITRF2020 at their ``epoch`` too (:func:`..gnss.frames.transform`), never by PROJ's own null
    through the ETRS89 ensemble (8-9 cm off at Rome in 2026).

    Points that carry no frame and epoch - the positions of a project made before the frames, markers, the points of a
    chunk - say so with ``legacy`` (a :class:`..gnss.frames.Legacy`: what they are, their chunk): from or to ITRF2020
    they go by PROJ's own transformation where it has one that is no ballpark, else as positions on WGS 84, with a
    warning (:func:`..gnss.frames.legacy_transform`); between two European frames at the epoch of the survey of their
    chunk (:func:`..gnss.frames.survey_epoch`), which the log says.

    The heights go as the two systems have them: between heights above the ellipsoid and heights above a geoid
    (:func:`..gnss.heights.height_kind`), on every route, only by a transformation that is no ballpark and with the
    grid of the geoid - else a :class:`..gnss.heights.GridMissing` (a ``ValueError``) names the grid and how to get it
    (:func:`by_proj`); a system without heights of its own (2D) takes them, and gives them, as they are."""
    if src == dst or (is_local(src) and is_local(dst)):
        return np.asarray(points, float).copy()
    if is_local(src) or is_local(dst):
        raise ValueError("cannot transform between a local frame and a geodetic CRS")
    from ..gnss import frames

    if frames.dynamic(src) or frames.dynamic(dst):
        if legacy is None:
            return frames.transform(points, src, dst, epoch)
        return frames.legacy_transform(points, src, dst, legacy)
    if frames.european_hop(src, dst):
        if epoch is None and legacy is not None:
            epoch = frames.legacy_epoch(legacy, src, dst)
        return frames.transform(points, src, dst, epoch)
    return by_proj(points, src, dst)


def by_proj(points: np.ndarray, src: str, dst: str) -> np.ndarray:
    """``(N,3)`` points from ``src`` to ``dst`` by PROJ's own choice (longitude first): what :func:`transform_points`
    does between systems that are not in ITRF2020.

    Between heights above the ellipsoid and heights above a geoid (:func:`..gnss.heights.kinds_differ`), or above two
    geoids (:func:`..gnss.heights.geoids_differ`: EGM96 into EGM2008, metres apart), PROJ's choice is
    never a ballpark - which, without the grid of the geoid, gives the heights as they are: heights above the ellipsoid
    passed off as heights above the geoid, 48 m off at Rome - and every point with numbers comes out with numbers, or a
    :class:`..gnss.heights.GridMissing` (a ``ValueError``) names the grid PROJ lacks and how to get it."""
    from ..gnss import heights

    pts = np.atleast_2d(np.asarray(points, float))
    if heights.through_a_geoid(src, dst):  # ellipsoid and geoid, or two geoids: never a ballpark
        return _between_heights(pts, src, dst)
    x, y, z = _transformer(src, dst).transform(pts[:, 0], pts[:, 1], pts[:, 2])
    return np.column_stack([x, y, z])


@functools.lru_cache(maxsize=128)
def _transformer(src: str, dst: str, ballpark: bool = True):
    """PROJ's transformation from ``src`` to ``dst`` (longitude first), kept: making one can take a tenth of a second
    (into a national system with heights above a geoid), using it a fraction of a millisecond. ``ballpark`` False:
    never a ballpark (a ``ProjError`` when PROJ has no other it can use). A pyproj transformer is made again in each
    thread that uses it."""
    from pyproj import Transformer

    return Transformer.from_crs(to_pyproj(src), to_pyproj(dst), always_xy=True, allow_ballpark=ballpark)


def forget_transformers() -> None:
    """Forget the kept transformations (:func:`_transformer`): made again with the grids PROJ finds now
    (:func:`..gnss.heights.refresh`)."""
    _transformer.cache_clear()


def _between_heights(pts: np.ndarray, src: str, dst: str) -> np.ndarray:
    """:func:`by_proj` between heights above the ellipsoid and heights above a geoid: by a transformation that is no
    ballpark, tried once more after PROJ looks for its grids again (:func:`..gnss.heights.refresh`: a grid downloaded
    or copied after PROJ looked for it in this thread), else the refusal that says which grid is missing."""
    from ..gnss import heights

    out = _no_ballpark(pts, src, dst)
    if out is None:
        heights.refresh()
        out = _no_ballpark(pts, src, dst)
    if out is None:
        raise heights.refusal(src, dst)
    return out


def _no_ballpark(pts: np.ndarray, src: str, dst: str) -> np.ndarray | None:
    """``pts`` by PROJ's choice among its transformations that are no ballpark, or None when it has none it can use,
    or one point with numbers comes out without (where none reaches)."""
    from pyproj.exceptions import ProjError

    try:
        x, y, z = _transformer(src, dst, False).transform(pts[:, 0], pts[:, 1], pts[:, 2])
    except ProjError:
        return None
    out = np.column_stack([x, y, z])
    given = np.isfinite(pts).all(axis=1)
    return out if np.isfinite(out[given]).all() else None


def utm_for_lonlat(lon: float, lat: float) -> str:
    """EPSG string of the WGS84/UTM zone containing ``(lon, lat)``."""
    zone = int((lon + 180) // 6) + 1
    return f"EPSG:{(32600 if lat >= 0 else 32700) + zone}"
