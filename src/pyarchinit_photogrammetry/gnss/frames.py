"""The reference frame and the epoch of a GNSS position, and the conversions that take them into account.

A position computed against a base whose coordinates are in ITRF2020 (a permanent station of the IGS or of the EPN, a
PPP solution; IGS20, IGb20 and IGc20 are ITRF2020 for the program) is in ITRF2020 at the epoch of those coordinates: a
frame in which Europe moves by about 2.5 cm a year. The European frames - ETRF2000 (the frame of RDN2008, and of ETRS89
in Italy), ETRF2014, ETRF2020 - move with the European plate. From ITRF2020 to them goes the time-dependent Helmert of
the EPSG ("ITRF2020 to ETRF2000 (1)", ...) at the epoch of the positions: about a metre at Rome in 2026. PROJ left to
itself picks a ballpark (no shift at all) from ITRF2020 to RDN2008 / UTM, a route of its own to ETRS89 / UTM that
depends on the grids it finds and, without the epoch, takes 2015.0 (32 cm off at Rome in 2026): every conversion of a
position in ITRF2020 goes through :func:`transform`, which :func:`..core.crs.transform_points` calls for it.

ITRF2020 and WGS 84 are the same frame for the program (their realisations agree within centimetres): a position in
ITRF2020 goes to a system on WGS 84 as it is, at any epoch. A position whose frame nobody knows - the drone's own RTK,
the GPS of a photo - is ``"unknown"``, in WGS 84, as it always was. What carries no frame and epoch - the positions of a
project made before the frames, markers, the points of a chunk - converts as before, by PROJ's own choice
(:func:`..core.crs.transform_points` with ``legacy``), and :func:`legacy_warning` says so once."""
from __future__ import annotations

import functools
import logging
import math
from datetime import datetime, timezone

import numpy as np

from ..core import crs as crs_mod

log = logging.getLogger(__name__)

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
_LEGACY_WARNED: set[tuple[str, str]] = set()  # (who converts, system): what legacy_warning has said already

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


def position_frame(record: dict | None) -> str:
    """The frame the record of a position (the ``meta["gnss"]`` of a camera) names: one of :data:`FRAMES`;
    ``"unknown"`` for a record that names none - a position written before the frames, read from another program - or
    names one the program does not know."""
    frame = (record or {}).get("frame")
    return frame if frame in FRAMES else UNKNOWN


def legacy_warning(where: str, crs: str) -> None:
    """Log, once for each ``where`` (what converts them) and ``crs`` (a system in ITRF2020), that positions carrying no
    frame and epoch were converted as before, by PROJ's own choice (:func:`..core.crs.transform_points` with
    ``legacy``): about a metre off in Europe, where the Helmert at their epoch would be exact."""
    if (where, crs) in _LEGACY_WARNED:
        return
    _LEGACY_WARNED.add((where, crs))
    log.warning("%s in %s carry no reference frame and epoch: they are converted as before, by PROJ's own choice, "
                "which can be about a metre off in Europe. To convert them exactly, import them again with their "
                "epoch, or in the coordinate system of the chunk", where, crs)


def decimal_year(when: datetime) -> float:
    """The epoch of ``when`` as a decimal year: the year plus the time since its 1 January over the length of that year,
    in UTC (a ``when`` without a time zone is UTC)."""
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
    start, end = datetime(when.year, 1, 1), datetime(when.year + 1, 1, 1)
    return when.year + (when - start).total_seconds() / (end - start).total_seconds()


def _not_a_year(epoch) -> ValueError:
    return ValueError(f"an epoch is a decimal year (2026.82 for the end of October 2026), not {epoch!r}")


def valid_epoch(epoch) -> float:
    """``epoch`` as a decimal year: a ``ValueError`` for anything that is not a number between :data:`EPOCHS` (a time
    in seconds, a date)."""
    try:
        value = float(epoch)
    except (TypeError, ValueError):
        raise _not_a_year(epoch) from None
    if not (math.isfinite(value) and EPOCHS[0] <= value <= EPOCHS[1]):
        raise _not_a_year(epoch)
    return value


def frame_and_epoch(frame: str, epoch=None) -> tuple[str, float | None]:
    """The ``frame`` and the ``epoch`` of a position, checked: the frame one of :data:`FRAMES` (``"WGS84"`` is
    ``"unknown"``, the frame nobody knows), the epoch a decimal year (:func:`valid_epoch`) or None when it is not known
    - which a position in a frame that moves with time (ITRF2020) cannot be: it could not be converted to any other
    system. A ``ValueError`` otherwise."""
    crs_of(frame)  # a frame the program knows
    frame = UNKNOWN if frame == WGS84 else frame
    if epoch is not None:
        return frame, valid_epoch(epoch)
    if frame in DYNAMIC:
        raise ValueError(f"a position in {frame} moves with time: it needs the epoch of its coordinates (a decimal "
                         "year), or it could not be converted to any other system")
    return frame, None


def _epoch_values(epoch) -> np.ndarray:
    try:
        return np.asarray(epoch, dtype=np.float64).reshape(-1)
    except (TypeError, ValueError):
        raise _not_a_year(epoch) from None


def _epochs(epoch, count: int, src: str, dst: str) -> np.ndarray:
    """The epoch of every one of ``count`` positions from ``epoch``, one for all or one each; a ``ValueError`` when one
    is missing or is not a year."""
    if epoch is None:
        raise ValueError(f"positions in ITRF2020 move with time: converting them from {src} to {dst} needs the epoch "
                         "of their coordinates (a decimal year), and none was given")
    values = _epoch_values(epoch)
    if values.size == 1:
        values = np.full(count, values[0])
    elif values.size != count:
        raise ValueError(f"{values.size} epochs for {count} positions: one for all of them or one each")
    missing = int(np.count_nonzero(np.isnan(values)))
    if missing:
        raise ValueError(f"positions in ITRF2020 move with time: converting them from {src} to {dst} needs the epoch "
                         f"of the coordinates of each (a decimal year), and {missing} of {count} have none")
    for value in np.unique(values):
        valid_epoch(value)
    return values


def _rows(epoch, keep: np.ndarray):
    """The epochs of the rows ``keep`` keeps: one epoch for all of them stays as it is."""
    if epoch is None or np.ndim(epoch) == 0:
        return epoch
    values = _epoch_values(epoch)
    return values[keep] if values.size == keep.size else values


# --- the steps of a conversion -------------------------------------------------------------------------------------


@functools.lru_cache(maxsize=64)
def _transformer(src: str, dst: str):
    """PROJ's transformation from ``src`` to ``dst`` (longitude first)."""
    from pyproj import Transformer

    return Transformer.from_crs(_parsed(src), _parsed(dst), always_xy=True)


def _apply(transformer, points: np.ndarray, inverse: bool = False) -> np.ndarray:
    from pyproj.enums import TransformDirection

    direction = TransformDirection.INVERSE if inverse else TransformDirection.FORWARD
    x, y, z = transformer.transform(points[:, 0], points[:, 1], points[:, 2], direction=direction)
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
    return np.column_stack([x, y, z]).astype(np.float64)


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
def _operation(hub: str, target: str, area: tuple[float, float, float, float] | None):
    """The transformation PROJ ranks first from ``hub`` to ``target`` among those that are no ballpark and are valid in
    ``area`` (the largest area of use there, then the best accuracy), made again from its pipeline (the transformers of
    a group are not safe to share between threads); None when there is none."""
    from pyproj import Transformer
    from pyproj.transformer import AreaOfInterest, TransformerGroup

    group = TransformerGroup(_parsed(hub), _parsed(target), always_xy=True, allow_ballpark=False,
                             area_of_interest=AreaOfInterest(*area) if area else None)
    return Transformer.from_pipeline(group.transformers[0].to_proj4()) if group.transformers else None


def _datum_target(crs: str) -> str:
    """The system the datum of ``crs`` is reached in: ``crs`` itself or, for a compound system, the geographic 3D system
    of its horizontal datum, from which a conversion gives the plan and the heights (a missing geoid grid does not hide
    a transformation of the datum)."""
    return _geographic(crs) if _parsed(crs).is_compound else crs


def _route(crs: str, frame: str | None, lonlat, src: str, dst: str) -> tuple[str, bool, object | None]:
    """How positions in ITRF2020 go to ``crs`` (or come from it), whose frame is ``frame``: the geographic 3D system
    they pass through, whether the Helmert of the EPSG takes them there (``True``: at their epochs) or ITRF2020 is taken
    as it is (WGS 84), and the operation from that system to the datum of ``crs`` (:func:`_datum_target`), or None for
    PROJ's own from there (a conversion, or its null transformation from ETRF2000 to RDN2008 and ETRS89).

    A European frame is its own system and WGS 84 is ``EPSG:4979``. Any other datum is reached from ETRF2000 by the
    transformation PROJ ranks first among those that are no ballpark where the points are (``lonlat``: a callable that
    gives their longitude and latitude), else from WGS 84 the same way, else a ``ValueError``: PROJ knows that datum
    only by a ballpark, which can be metres off."""
    if frame in EUROPEAN:
        return FRAMES[frame], True, None
    if frame == WGS84:
        return FRAMES[UNKNOWN], False, None
    area, target = _area(lonlat()), _datum_target(crs)
    for hub, helmert in ((FRAMES["ETRF2000"], True), (FRAMES[UNKNOWN], False)):
        operation = _operation(hub, target, area)
        if operation is not None:
            return hub, helmert, operation
    raise ValueError(f"cannot convert positions in ITRF2020 from {src} to {dst}: PROJ knows the datum of {crs} only by "
                     "a ballpark, no transformation from ETRF2000 or WGS 84 where the positions are, and a ballpark "
                     "can be metres off. Choose a coordinate system on ETRF2000, ETRS89, RDN2008 or WGS 84")


def transform(points, src: str, dst: str, epoch=None) -> np.ndarray:
    """``(N, 3)`` points from ``src`` to ``dst``, the epochs of the points taken into account when one of the two is in
    ITRF2020 (:data:`DYNAMIC`); ``epoch`` is a decimal year (:func:`decimal_year`) for all of them, or one for each.

    From ITRF2020 to a European frame (ETRF2000 and the systems of RDN2008 and ETRS89, ETRF2014, ETRF2020) the
    time-dependent Helmert of the EPSG (``ITRF2020 to ETRF2000 (1)``...) at the epochs, then PROJ from the geographic
    system of that frame to ``dst`` (a conversion, or PROJ's null transformation from ETRF2000 to RDN2008 and ETRS89);
    to a system on WGS 84 ITRF2020 as it is, with no epoch needed; to another datum through ETRF2000 at the epochs by
    the transformation PROJ ranks first among those that are no ballpark where the points are, else through WGS 84 the
    same way (:func:`_route`). The other way round the same steps backwards; between two systems of ITRF2020
    conversions only. A row without a finite number is no position: it stays without one (NaN) and needs no epoch. A
    ``ValueError`` without the epoch where the Helmert is applied, for an epoch that is not a year, and for a datum PROJ
    knows only by a ballpark. Between systems that are not in ITRF2020 :func:`..core.crs.transform_points`."""
    pts = np.atleast_2d(np.asarray(points, dtype=np.float64))
    if src == dst or crs_mod.is_local(src) or crs_mod.is_local(dst):
        return crs_mod.transform_points(pts, src, dst)  # a copy, or the refusal of a local frame
    source, target = frame_of(src), frame_of(dst)
    if source not in DYNAMIC and target not in DYNAMIC:
        return crs_mod.transform_points(pts, src, dst)
    whole = np.isfinite(pts).all(axis=1)
    if not whole.all():
        out = np.full(pts.shape, np.nan)
        if whole.any():
            out[whole] = transform(pts[whole], src, dst, _rows(epoch, whole))
        return out
    if source in DYNAMIC and target in DYNAMIC:
        return _from_itrf2020(_to_itrf2020(pts, src), dst)
    if source in DYNAMIC:
        lonlat = _to_itrf2020(pts, src)
        hub, helmert, operation = _route(dst, target, lambda: lonlat, src, dst)
        moved = _move(lonlat, _european(hub), _epochs(epoch, len(pts), src, dst)) if helmert else lonlat
        if operation is None:
            return _convert(moved, hub, dst)
        return _convert(_apply(operation, moved), _datum_target(dst), dst)
    hub, helmert, operation = _route(src, source, lambda: _convert(pts, src, _geographic(src)), src, dst)
    if operation is None:
        at_hub = _convert(pts, src, hub)
    else:
        at_hub = _apply(operation, _convert(pts, src, _datum_target(src)), inverse=True)
    lonlat = _move(at_hub, _european(hub), _epochs(epoch, len(pts), src, dst), inverse=True) if helmert else at_hub
    return _from_itrf2020(lonlat, dst)


def _european(hub: str) -> str:
    """The European frame whose geographic 3D system is ``hub``."""
    return next(frame for frame in EUROPEAN if FRAMES[frame] == hub)
