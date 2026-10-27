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


def transform_points(points: np.ndarray, src: str, dst: str, epoch=None, legacy: str | None = None) -> np.ndarray:
    """Transform ``(N,3)`` points between two CRS (heights passed through). When one of the two is in ITRF2020, a
    frame where positions move with time (a GNSS position against a permanent station or PPP), the points go through
    :func:`..gnss.frames.transform` at their ``epoch``: a decimal year for all of them, or one for each (PROJ alone
    would take a ballpark, or the epoch 2015.0).

    Points that carry no frame and epoch - the positions of a project made before the frames, markers, the points of a
    chunk - are converted as before, by PROJ's own choice, also from or to ITRF2020, when the caller names itself in
    ``legacy`` (what it converts): :func:`..gnss.frames.legacy_warning` logs it, once for each caller and system."""
    if src == dst or (is_local(src) and is_local(dst)):
        return np.asarray(points, float).copy()
    if is_local(src) or is_local(dst):
        raise ValueError("cannot transform between a local frame and a geodetic CRS")
    from ..gnss import frames

    moving = [system for system in (src, dst) if frames.dynamic(system)]
    if moving and legacy is None:
        return frames.transform(points, src, dst, epoch)
    if moving:
        frames.legacy_warning(legacy, moving[0])
    from pyproj import Transformer

    tr = Transformer.from_crs(to_pyproj(src), to_pyproj(dst), always_xy=True)
    pts = np.atleast_2d(np.asarray(points, float))
    x, y, z = tr.transform(pts[:, 0], pts[:, 1], pts[:, 2])
    return np.column_stack([x, y, z])


def utm_for_lonlat(lon: float, lat: float) -> str:
    """EPSG string of the WGS84/UTM zone containing ``(lon, lat)``."""
    zone = int((lon + 180) // 6) + 1
    return f"EPSG:{(32600 if lat >= 0 else 32700) + zone}"
