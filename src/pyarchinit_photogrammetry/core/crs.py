"""Coordinate reference systems (thin wrapper around pyproj)."""
from __future__ import annotations

import numpy as np

LOCAL = "LOCAL"  # arbitrary local engineering frame (metres)


def is_local(crs: str | None) -> bool:
    return crs in (None, "", LOCAL)


def to_pyproj(crs: str):
    from pyproj import CRS

    return CRS.from_user_input(crs)


def epsg_code(crs: str | None) -> int | None:
    """EPSG integer of a CRS string, ``None`` for local/unknown frames."""
    if is_local(crs):
        return None
    try:
        return to_pyproj(crs).to_epsg()
    except Exception:
        return None


def wkt(crs: str | None) -> str | None:
    if is_local(crs):
        return None
    return to_pyproj(crs).to_wkt()


def is_geographic(crs: str | None) -> bool:
    return not is_local(crs) and to_pyproj(crs).is_geographic


def transform_points(points: np.ndarray, src: str, dst: str, epoch=None, legacy=None) -> np.ndarray:
    """Transform ``(N,3)`` points between two CRS (heights passed through). When one of the two is in ITRF2020, a
    frame where positions move with time (a GNSS position against a permanent station or PPP), the points go through
    :func:`..gnss.frames.transform` at their ``epoch``: a decimal year for all of them, or one for each (PROJ alone
    would take a ballpark, or the epoch 2015.0).

    Points that carry no frame and epoch - the positions of a project made before the frames, markers, the points of a
    chunk - say so with ``legacy`` (a :class:`..gnss.frames.Legacy`: what they are, their chunk): from or to ITRF2020
    they go by PROJ's own transformation where it has one that is no ballpark, else as positions on WGS 84, with a
    warning (:func:`..gnss.frames.legacy_transform`)."""
    if src == dst or (is_local(src) and is_local(dst)):
        return np.asarray(points, float).copy()
    if is_local(src) or is_local(dst):
        raise ValueError("cannot transform between a local frame and a geodetic CRS")
    from ..gnss import frames

    if frames.dynamic(src) or frames.dynamic(dst):
        if legacy is None:
            return frames.transform(points, src, dst, epoch)
        return frames.legacy_transform(points, src, dst, legacy)
    return by_proj(points, src, dst)


def by_proj(points: np.ndarray, src: str, dst: str) -> np.ndarray:
    """``(N,3)`` points from ``src`` to ``dst`` by PROJ's own choice (longitude first): what :func:`transform_points`
    does between systems that are not in ITRF2020."""
    from pyproj import Transformer

    tr = Transformer.from_crs(to_pyproj(src), to_pyproj(dst), always_xy=True)
    pts = np.atleast_2d(np.asarray(points, float))
    x, y, z = tr.transform(pts[:, 0], pts[:, 1], pts[:, 2])
    return np.column_stack([x, y, z])


def utm_for_lonlat(lon: float, lat: float) -> str:
    """EPSG string of the WGS84/UTM zone containing ``(lon, lat)``."""
    zone = int((lon + 180) // 6) + 1
    return f"EPSG:{(32600 if lat >= 0 else 32700) + zone}"
