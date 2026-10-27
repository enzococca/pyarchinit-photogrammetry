"""The reference frame and the epoch of a GNSS position, and the conversions that take them into account.

A position measured against a base whose coordinates are in ITRF2020 (a permanent station of the IGS or of the EPN, a
PPP solution; IGS20, IGb20 and IGc20 are ITRF2020 for the program) is in ITRF2020 at the epoch it was measured at: a
frame in which Europe moves by about 2.5 cm a year. The European frames - ETRF2000 (the frame of RDN2008, and of ETRS89
in Italy), ETRF2014, ETRF2020 - move with the European plate. From ITRF2020 to them goes the time-dependent Helmert of
the EPSG ("ITRF2020 to ETRF2000 (1)", ...) at the epoch of the positions: about a metre at Rome in 2026. PROJ left to
itself picks a ballpark (no shift at all) from ITRF2020 to RDN2008 / UTM, a route of its own to ETRS89 / UTM that
depends on the grids it finds and, without the epoch, takes 2015.0 (32 cm off at Rome in 2026): every conversion of a
position in ITRF2020 goes through :func:`transform`, which :func:`..core.crs.transform_points` calls for it.

ITRF2020 and WGS 84 are the same frame for the program (their realisations agree within centimetres): a position in
ITRF2020 goes to a system on WGS 84 as it is, at any epoch. A position whose frame nobody knows - the drone's own RTK,
the GPS of a photo, the projects made before the frames - is ``"unknown"``, in WGS 84, as it always was."""
from __future__ import annotations

import functools
import math
from datetime import datetime, timezone

import numpy as np

from ..core import crs as crs_mod

UNKNOWN = "unknown"
WGS84 = "WGS84"  # what frame_of says of a system on WGS 84; for a position, its frame is "unknown"
# the frames of the positions the program writes, by name, and their geographic 3D systems (longitude, latitude,
# height above the ellipsoid)
FRAMES = {"ITRF2020": "EPSG:9989", "ETRF2000": "EPSG:7931", "ETRF2014": "EPSG:8403", "ETRF2020": "EPSG:10570",
          UNKNOWN: "EPSG:4979"}
DYNAMIC = frozenset({"ITRF2020"})  # the frames in which a position moves with time: it needs its epoch
EUROPEAN = ("ETRF2000", "ETRF2014", "ETRF2020")  # those reached from ITRF2020 by the Helmert of the EPSG
EPOCHS = (1980.0, 2100.0)  # an epoch is a decimal year between these: GPS time starts in 1980
_HELMERT = "ITRF2020 to ETRF20"  # the step of the operations of the EPSG from ITRF2020 to the European frames

# the frame of a datum by its EPSG code: ITRF2020 with its update of 2023 and the realisations of the IGS (IGS20,
# IGb20); ETRF2000 with RDN2008 and the ETRS89 ensemble; ETRF2014; ETRF2020; WGS 84 and its realisations
_DATUM_FRAMES = {1322: "ITRF2020", 1399: "ITRF2020", 1333: "ITRF2020", 1400: "ITRF2020",
                 1186: "ETRF2000", 1132: "ETRF2000", 6258: "ETRF2000", 1206: "ETRF2014", 1382: "ETRF2020",
                 6326: WGS84, 1152: WGS84, 1153: WGS84, 1154: WGS84, 1155: WGS84, 1156: WGS84, 1166: WGS84,
                 1309: WGS84, 1383: WGS84}
# the same by name, for a system that does not give the code of its datum (a WKT without identifiers); the names of
# the realisations of WGS 84 all start with "World Geodetic System 1984"
_NAME_FRAMES = {"International Terrestrial Reference Frame 2020": "ITRF2020",
                "International Terrestrial Reference Frame 2020-u2023": "ITRF2020", "IGS20": "ITRF2020",
                "IGb20": "ITRF2020", "IGc20": "ITRF2020",
                "European Terrestrial Reference Frame 2000": "ETRF2000", "Rete Dinamica Nazionale 2008": "ETRF2000",
                "European Terrestrial Reference System 1989 ensemble": "ETRF2000",
                "European Terrestrial Reference System 1989": "ETRF2000",
                "European Terrestrial Reference Frame 2014": "ETRF2014",
                "European Terrestrial Reference Frame 2020": "ETRF2020"}


def crs_of(frame: str) -> str:
    """The geographic 3D system of ``frame``, one of :data:`FRAMES` or ``"WGS84"`` (what :func:`frame_of` says of a
    system on WGS 84: ``EPSG:4979``, the one of the positions of an ``"unknown"`` frame); a ``ValueError`` for any other
    name."""
    if frame == WGS84:
        return FRAMES[UNKNOWN]
    try:
        return FRAMES[frame]
    except (KeyError, TypeError):
        raise ValueError(f"{frame!r} is not a reference frame the program knows: one of "
                         f"{', '.join(FRAMES)}") from None


def _horizontal(system):
    """The horizontal part of a pyproj CRS: the first of a compound one. A bound one stays as it is: its datum is the
    one of its source, and its ``+towgs84`` is the transformation PROJ knows for it."""
    return system.sub_crs_list[0] if system.is_compound else system


@functools.lru_cache(maxsize=256)
def _parsed(system: str):
    from pyproj import CRS

    return CRS.from_user_input(system)


def _datum_code(datum) -> int | None:
    """The EPSG code of a datum (or datum ensemble), None when it has none."""
    try:
        ident = datum.to_json_dict().get("id") or {}
    except Exception:  # noqa: BLE001 - a datum PROJ cannot write: no code
        return None
    return int(ident["code"]) if ident.get("authority") == "EPSG" and "code" in ident else None


@functools.lru_cache(maxsize=256)
def frame_of(crs: str | None) -> str | None:
    """The frame of the datum of ``crs`` (of its horizontal part in a compound system): ``"ITRF2020"`` (IGS20 and IGb20
    too), ``"ETRF2000"`` (RDN2008 and ETRS89 too), ``"ETRF2014"``, ``"ETRF2020"`` or ``"WGS84"``; None for any other
    datum, for a local frame and for a system PROJ does not know. ``frame_of("EPSG:4979")`` is ``"WGS84"``: the
    positions of an ``"unknown"`` frame are taken as WGS 84."""
    from pyproj.exceptions import CRSError

    if crs_mod.is_local(crs):
        return None
    try:
        datum = _horizontal(_parsed(crs)).datum
    except CRSError:
        return None
    if datum is None:
        return None
    code = _datum_code(datum)
    if code in _DATUM_FRAMES:
        return _DATUM_FRAMES[code]
    if datum.name.startswith("World Geodetic System 1984"):
        return WGS84
    return _NAME_FRAMES.get(datum.name)


def dynamic(crs: str | None) -> bool:
    """``crs`` is in a frame where positions move with time (:data:`DYNAMIC`): a conversion needs their epoch."""
    return frame_of(crs) in DYNAMIC


def decimal_year(when: datetime) -> float:
    """The epoch of ``when`` as a decimal year: the year plus the time since its 1 January over the length of that year,
    in UTC (a ``when`` without a time zone is UTC)."""
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
    start, end = datetime(when.year, 1, 1), datetime(when.year + 1, 1, 1)
    return when.year + (when - start).total_seconds() / (end - start).total_seconds()


def valid_epoch(epoch) -> float:
    """``epoch`` as a decimal year: a ``ValueError`` for anything that is not a number between :data:`EPOCHS` (a time
    in seconds, a date)."""
    try:
        value = float(epoch)
    except (TypeError, ValueError):
        raise ValueError(f"an epoch is a decimal year (2026.82 for the end of October 2026), not {epoch!r}") from None
    if not (math.isfinite(value) and EPOCHS[0] <= value <= EPOCHS[1]):
        raise ValueError(f"an epoch is a decimal year (2026.82 for the end of October 2026), not {epoch!r}")
    return value


def _epochs(epoch, count: int, src: str, dst: str) -> np.ndarray:
    """The epoch of every one of ``count`` positions from ``epoch``, one for all or one each; a ``ValueError`` when one
    is missing or is not a year."""
    if epoch is None:
        raise ValueError(f"positions in ITRF2020 move with time: converting them from {src} to {dst} needs the epoch "
                         "they were measured at (a decimal year), and none was given")
    try:
        values = np.asarray(epoch, dtype=np.float64).reshape(-1)
    except (TypeError, ValueError):
        raise ValueError(f"an epoch is a decimal year (2026.82 for the end of October 2026), not {epoch!r}") from None
    if values.size == 1:
        values = np.full(count, values[0])
    elif values.size != count:
        raise ValueError(f"{values.size} epochs for {count} positions: one for all of them or one each")
    missing = int(np.count_nonzero(np.isnan(values)))
    if missing:
        raise ValueError(f"positions in ITRF2020 move with time: converting them from {src} to {dst} needs the epoch "
                         f"each was measured at (a decimal year), and {missing} of {count} have none")
    for value in np.unique(values):
        valid_epoch(value)
    return values


# --- the steps of a conversion -------------------------------------------------------------------------------------


@functools.lru_cache(maxsize=64)
def _transformer(src: str, dst: str):
    """PROJ's transformation from ``src`` to ``dst`` (longitude first)."""
    from pyproj import Transformer

    return Transformer.from_crs(_parsed(src), _parsed(dst), always_xy=True)


def _apply(transformer, points: np.ndarray) -> np.ndarray:
    x, y, z = transformer.transform(points[:, 0], points[:, 1], points[:, 2])
    return np.column_stack([x, y, z]).astype(np.float64)


def _convert(points: np.ndarray, src: str, dst: str) -> np.ndarray:
    return points.copy() if src == dst else _apply(_transformer(src, dst), points)


@functools.lru_cache(maxsize=64)
def _geographic(crs: str) -> str:
    """The geographic 3D system (longitude, latitude, height above the ellipsoid) of the datum of ``crs``: its EPSG code
    when it has one, else its WKT. Converting to it from ``crs`` is a conversion only, never a change of datum."""
    from pyproj.crs import GeographicCRS
    from pyproj.crs.coordinate_system import Ellipsoidal3DCS

    system = _horizontal(_parsed(crs)).geodetic_crs
    if system.is_geocentric:
        system = GeographicCRS(name=system.name, datum=system.datum, ellipsoidal_cs=Ellipsoidal3DCS())
    system = system.to_3d()
    code = system.to_epsg()
    return f"EPSG:{code}" if code else system.to_wkt()


def _to_itrf2020(points: np.ndarray, src: str) -> np.ndarray:
    """``points`` of ``src``, a system of ITRF2020 (or of IGS20, IGb20: the same frame), as longitude, latitude and
    height in ITRF2020 (``EPSG:9989``): a conversion to the geographic 3D system of its own datum, whose numbers are
    then those of ITRF2020. PROJ is never asked from one realisation of ITRF2020 to another: it has a ballpark for
    some, a transformation for others, and nothing for IGS20."""
    return _convert(points, src, _geographic(src))


def _from_itrf2020(points: np.ndarray, dst: str) -> np.ndarray:
    """Longitude, latitude and height in ITRF2020 as points of ``dst``, a system of ITRF2020 (:func:`_to_itrf2020` the
    other way)."""
    return _convert(points, _geographic(dst), dst)


@functools.lru_cache(maxsize=8)
def _helmert(european: str):
    """The time-dependent Helmert of the EPSG from ITRF2020 to the European frame ``european`` (one of
    :data:`EUROPEAN`), as PROJ has it: the first of its operations from ``EPSG:9989`` to the system of that frame that
    is no ballpark and has the step ``ITRF2020 to ETRF20..`` (``ITRF2020 to ETRF2000 (1)``...), made again from its
    pipeline: the transformers of a group are not safe to share between threads."""
    from pyproj import Transformer
    from pyproj.transformer import TransformerGroup

    group = TransformerGroup(_parsed(FRAMES["ITRF2020"]), _parsed(FRAMES[european]), always_xy=True,
                             allow_ballpark=False)
    for transformer in group.transformers:
        steps = [op.name for op in (transformer.operations or ())] or [transformer.description]
        if any((step or "").startswith(_HELMERT) for step in steps):
            return Transformer.from_pipeline(transformer.to_proj4())
    raise RuntimeError(f"PROJ has no Helmert from ITRF2020 to {european} (its database is older than the EPSG's "
                       "ITRF2020 to ETRF transformations): update pyproj")


def _move(points: np.ndarray, european: str, epochs: np.ndarray, inverse: bool = False) -> np.ndarray:
    """Longitude, latitude and height of ``points`` from ITRF2020 to ``european`` (or back, ``inverse``) by the Helmert
    of the EPSG at the ``epochs`` of the points (decimal years, one each)."""
    from pyproj.enums import TransformDirection

    direction = TransformDirection.INVERSE if inverse else TransformDirection.FORWARD
    x, y, z, _t = _helmert(european).transform(points[:, 0], points[:, 1], points[:, 2], epochs, direction=direction)
    moved = np.column_stack([x, y, z]).astype(np.float64)
    if not np.isfinite(moved).all():
        raise ValueError(f"the Helmert from ITRF2020 to {european} gave no position for some of the points: are they "
                         "longitude, latitude and height?")
    return moved


def _area(lonlat: np.ndarray) -> tuple[float, float, float, float] | None:
    """The area of the points (west, south, east, north in degrees), widened to the hundredth of a degree; None for
    points without a finite longitude and latitude."""
    lonlat = lonlat[np.isfinite(lonlat[:, :2]).all(axis=1)]
    if not len(lonlat):
        return None
    lon = (lonlat[:, 0] + 180.0) % 360.0 - 180.0
    lat = np.clip(lonlat[:, 1], -90.0, 90.0)
    return (max(math.floor(lon.min() * 100.0) / 100.0, -180.0), max(math.floor(lat.min() * 100.0) / 100.0, -90.0),
            min(math.ceil(lon.max() * 100.0) / 100.0, 180.0), min(math.ceil(lat.max() * 100.0) / 100.0, 90.0))


@functools.lru_cache(maxsize=64)
def _known(hub: str, crs: str, area: tuple[float, float, float, float] | None) -> bool:
    """PROJ has a transformation that is no ballpark between ``hub`` and the horizontal part of ``crs`` valid in
    ``area``."""
    from pyproj.transformer import AreaOfInterest, TransformerGroup

    group = TransformerGroup(_parsed(hub), _horizontal(_parsed(crs)), always_xy=True, allow_ballpark=False,
                             area_of_interest=AreaOfInterest(*area) if area else None)
    return bool(group.transformers)


def _hub(crs: str, frame: str | None, lonlat, src: str, dst: str) -> tuple[str, bool]:
    """The geographic 3D system a position in ITRF2020 passes through on its way to (or from) ``crs``, whose frame is
    ``frame``, and whether the Helmert of the EPSG takes it there (``True``: at the epoch) or ITRF2020 is taken as it
    is (WGS 84). A European frame is its own system; WGS 84 is ``EPSG:4979``; any other datum is reached from ETRF2000
    when PROJ has a transformation that is no ballpark between ETRF2000 and ``crs`` where the points are (``lonlat``:
    a callable that gives their longitude and latitude), else from WGS 84 the same way, else a ``ValueError``: PROJ
    knows that datum only by a ballpark, which can be metres off."""
    if frame in EUROPEAN:
        return FRAMES[frame], True
    if frame == WGS84:
        return FRAMES[UNKNOWN], False
    area = _area(lonlat())
    if _known(FRAMES["ETRF2000"], crs, area):
        return FRAMES["ETRF2000"], True
    if _known(FRAMES[UNKNOWN], crs, area):
        return FRAMES[UNKNOWN], False
    raise ValueError(f"cannot convert positions in ITRF2020 from {src} to {dst}: PROJ knows the datum of {crs} only by "
                     "a ballpark, no transformation from ETRF2000 or WGS 84 where the positions are, and a ballpark "
                     "can be metres off. Choose a coordinate system on ETRF2000, ETRS89, RDN2008 or WGS 84")


def transform(points, src: str, dst: str, epoch=None) -> np.ndarray:
    """``(N, 3)`` points from ``src`` to ``dst``, the epochs of the points taken into account when one of the two is in
    ITRF2020 (:data:`DYNAMIC`); ``epoch`` is a decimal year (:func:`decimal_year`) for all of them, or one for each.

    From ITRF2020 to a European frame (ETRF2000 and the systems of RDN2008 and ETRS89, ETRF2014, ETRF2020) the
    time-dependent Helmert of the EPSG (``ITRF2020 to ETRF2000 (1)``...) at the epochs, then PROJ from the geographic
    system of that frame to ``dst`` (a conversion, or PROJ's null transformation from ETRF2000 to RDN2008 and ETRS89);
    to a system on WGS 84 ITRF2020 as it is, with no epoch needed; to another datum through ETRF2000 at the epochs when
    PROJ has a transformation that is no ballpark from ETRF2000 to it where the points are, else through WGS 84 the same
    way. The other way round the same steps backwards; between two systems of ITRF2020 conversions only. A
    ``ValueError`` without the epoch where the Helmert is applied, for an epoch that is not a year, and for a datum PROJ
    knows only by a ballpark. Between systems that are not in ITRF2020 :func:`..core.crs.transform_points`."""
    pts = np.atleast_2d(np.asarray(points, dtype=np.float64))
    if src == dst or crs_mod.is_local(src) or crs_mod.is_local(dst):
        return crs_mod.transform_points(pts, src, dst)  # a copy, or the refusal of a local frame
    source, target = frame_of(src), frame_of(dst)
    if source not in DYNAMIC and target not in DYNAMIC:
        return crs_mod.transform_points(pts, src, dst)
    if source in DYNAMIC and target in DYNAMIC:
        return _from_itrf2020(_to_itrf2020(pts, src), dst)
    if source in DYNAMIC:
        lonlat = _to_itrf2020(pts, src)
        hub, helmert = _hub(dst, target, lambda: lonlat, src, dst)
        moved = _move(lonlat, _european(hub), _epochs(epoch, len(pts), src, dst)) if helmert else lonlat
        return _convert(moved, hub, dst)
    hub, helmert = _hub(src, source, lambda: _convert(pts, src, _geographic(src)), src, dst)
    at_hub = _convert(pts, src, hub)
    lonlat = _move(at_hub, _european(hub), _epochs(epoch, len(pts), src, dst), inverse=True) if helmert else at_hub
    return _from_itrf2020(lonlat, dst)


def _european(hub: str) -> str:
    """The European frame whose geographic 3D system is ``hub``."""
    return next(frame for frame in EUROPEAN if FRAMES[frame] == hub)
