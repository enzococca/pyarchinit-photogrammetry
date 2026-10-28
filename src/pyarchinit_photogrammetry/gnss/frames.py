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
the GPS of a photo - is ``"unknown"``, in WGS 84, as it always was. What carries no frame and epoch in a system of
ITRF2020 - the positions of a project made before the frames, markers, the points of a chunk - converts by PROJ's own
transformation where it has one that is no ballpark, else as a position on WGS 84 (:func:`legacy_transform`), and
:func:`legacy_warning` says so once for each chunk."""
from __future__ import annotations

import functools
import logging
import math
import re
import weakref
from datetime import datetime, timezone
from typing import NamedTuple

import numpy as np

from ..core import crs as crs_mod
from . import heights

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
# what legacy_warning has said: {id of the chunk (None for no chunk): {(what, system)}}, a chunk's set dropped with it
_WARNED: dict[int | None, set[tuple[str, str]]] = {}

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


class Legacy(NamedTuple):
    """Who converts points that carry no frame and epoch (:func:`..core.crs.transform_points` with ``legacy``):
    ``what`` they are, as the warning names them; the ``chunk`` they belong to, for which the warning is said once
    (with each system); ``photos``: they are the positions of photos, which can be given again with their epoch (not
    markers, nor the points of a chunk)."""

    what: str
    chunk: object = None
    photos: bool = False


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


def european_hop(src: str | None, dst: str | None) -> bool:
    """``src`` and ``dst`` are in two different European frames (:data:`EUROPEAN`: ETRF2000 - with RDN2008 and ETRS89 -
    ETRF2014, ETRF2020): a conversion between them goes through ITRF2020 at the epoch of the positions
    (:func:`transform`), never by PROJ's own null through the ETRS89 ensemble, 8-9 cm off at Rome in 2026."""
    source, target = frame_of(src), frame_of(dst)
    return source in EUROPEAN and target in EUROPEAN and source != target


def survey_epoch(chunk) -> float:
    """The epoch of the survey of ``chunk``, for what carries no epoch of its own (markers, the points of a chunk) where
    one is needed - between two European frames (:func:`european_hop`): the median of the epochs of the positions of
    its photos, else of the dates of its photos, else today. An epoch off changes such a conversion by 0.3-0.9 mm a
    year: today (2026.82) for a survey of 2015, from ETRF2014 or ETRF2020 into RDN2008, 4-11 mm - far less than the
    8-9 cm of PROJ's null shift it takes the place of."""
    epochs = [float(e) for cam in getattr(chunk, "cameras", ()) if (e := (cam.meta.get("gnss") or {}).get("epoch"))
              is not None]
    if not epochs:
        epochs = [decimal_year(when) for cam in getattr(chunk, "cameras", ()) if (when := _taken(cam)) is not None]
    return float(np.median(epochs)) if epochs else decimal_year(datetime.now(timezone.utc))


def shot_epoch(cam) -> float | None:
    """The epoch of the position of a photo: the one of its record (``meta["gnss"]["epoch"]``), else the date its
    camera wrote in the EXIF (its local time, to the second: hours off at most, nothing for a conversion between frames
    that move together), else None."""
    epoch = (cam.meta.get("gnss") or {}).get("epoch")
    if epoch is not None:
        return float(epoch)
    when = _taken(cam)
    return None if when is None else decimal_year(when)


def _taken(cam) -> datetime | None:
    try:
        return datetime.strptime(str(cam.meta.get("datetime"))[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def position_frame(record: dict | None) -> str:
    """The frame the record of a position (the ``meta["gnss"]`` of a camera) names: one of :data:`FRAMES`;
    ``"unknown"`` for a record that names none - a position written before the frames, read from another program - or
    names one the program does not know."""
    frame = (record or {}).get("frame")
    return frame if frame in FRAMES else UNKNOWN


def legacy_warning(legacy: Legacy, crs: str) -> None:
    """Log that points carrying no frame and epoch in ``crs``, a system of ITRF2020, were converted as
    :func:`legacy_transform` converts them - about a metre off in Europe, where the Helmert at their epoch is exact -
    and what to do about it, which depends on what they are (``legacy.photos``). Said once for each chunk
    (``legacy.chunk``), what is converted and system: a chunk opened again, or another project, hears it again."""
    scope = None if legacy.chunk is None else id(legacy.chunk)
    said = _WARNED.get(scope)
    if said is None:
        said = _WARNED[scope] = set()
        if legacy.chunk is not None:
            try:
                weakref.finalize(legacy.chunk, _WARNED.pop, scope, None)
            except TypeError:  # an object without weak references: what was said of it stays for the session
                pass
    if (legacy.what, crs) in said:
        return
    said.add((legacy.what, crs))
    advice = ("To convert them exactly, import them again with their epoch (the epoch of the positions file: "
              "`ppg gnss positions --epoch`, or its field in the window), or in ETRF2000 (RDN2008)" if legacy.photos
              else "To avoid it, give them in the coordinate system of the chunk (in Italy, ETRF2000 / RDN2008)")
    log.warning("%s in %s carry no reference frame and epoch: they are converted by PROJ's own transformation where it "
                "has one that is no ballpark, else as positions on WGS 84, an approximation - about a metre off in "
                "Europe, more where the transformation of the datum is less accurate. %s", legacy.what, crs, advice)


def legacy_epoch(legacy: Legacy, src: str, dst: str) -> float:
    """The epoch for points that carry none (``legacy``: markers, the points of a chunk) going between two European
    frames: the epoch of the survey of their chunk (:func:`survey_epoch`), logged."""
    epoch = survey_epoch(legacy.chunk)
    log.info("%s go from %s (%s) to %s (%s) through ITRF2020 at the epoch of the survey, %.2f: they carry none of "
             "their own", legacy.what, src, frame_of(src), dst, frame_of(dst), epoch)
    return epoch


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


_ISO_DATE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_DMY_DATE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


def parse_epoch(text) -> float:
    """An epoch as a person writes it, as a decimal year: a year with a point or a comma for the decimals (``2026.82``,
    ``2026,82``) or a day, ``2026-10-27`` or ``27/10/2026`` (its midnight, UTC). A ``ValueError`` for anything else and
    for a year that is no epoch (:func:`valid_epoch`)."""
    written = str(text).strip()
    for pattern, order in ((_ISO_DATE, (1, 2, 3)), (_DMY_DATE, (3, 2, 1))):
        found = pattern.fullmatch(written)
        if found:
            year, month, day = (int(found.group(k)) for k in order)
            try:
                return valid_epoch(decimal_year(datetime(year, month, day)))
            except ValueError:
                raise _not_a_year(text) from None
    return valid_epoch(written.replace(",", ".") if written else text)


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


def _no_epoch(src: str, dst: str, missing: str) -> ValueError:
    """The error for positions converted from or to a system of ITRF2020 without the epochs they need, said of the side
    that is in ITRF2020 (or of the two European frames they go between); ``missing`` says what is missing."""
    if european_hop(src, dst):
        return ValueError(f"positions go from {src} ({frame_of(src)}) to {dst} ({frame_of(dst)}) through ITRF2020 at "
                          "their epoch, by the Helmerts of the EPSG: converting them needs the epoch of their "
                          f"coordinates (a decimal year), {missing}")
    if dynamic(src):
        return ValueError(f"positions in ITRF2020 move with time: converting them from {src} to {dst} needs the epoch "
                          f"of their coordinates (a decimal year), {missing}")
    return ValueError(f"{dst} is a system of ITRF2020, in which positions move with time: converting positions from "
                      f"{src} to it needs the epoch to give them (a decimal year), {missing}")


def _epochs(epoch, count: int, src: str, dst: str) -> np.ndarray:
    """The epoch of every one of ``count`` positions from ``epoch``, one for all or one each; a ``ValueError`` when one
    is missing or is not a year."""
    if epoch is None:
        raise _no_epoch(src, dst, "and none was given")
    values = _epoch_values(epoch)
    if values.size == 1:
        values = np.full(count, values[0])
    elif values.size != count:
        raise ValueError(f"{values.size} epochs for {count} positions: one for all of them or one each")
    missing = int(np.count_nonzero(np.isnan(values)))
    if missing:
        raise _no_epoch(src, dst, f"and {missing} of {count} have none")
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
    """``points`` from ``src`` to ``dst`` by PROJ (a conversion, or its own transformation); between heights above the
    ellipsoid and heights above a geoid never its ballpark (:func:`..core.crs.by_proj`, which refuses without the grid
    of the geoid)."""
    if src == dst:
        return points.copy()
    if heights.through_a_geoid(src, dst):
        return crs_mod.by_proj(points, src, dst)
    return _apply(_transformer(src, dst), points)


def forget_operations() -> None:
    """Forget the operations of PROJ kept for the conversions (:func:`_transformer`, :func:`_operation`,
    :func:`_own_areas`): chosen again with the grids PROJ finds now (:func:`.heights.refresh`)."""
    for kept in (_transformer, _operation, _own_areas):
        kept.cache_clear()


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


@functools.lru_cache(maxsize=64)
def _datum_target(crs: str) -> str:
    """The system the datum of ``crs`` is reached in: ``crs`` itself or, for a compound system, its horizontal part
    (its EPSG code, else its WKT). The transformation to it gives the plan position only: the height above the geoid of
    a compound system comes from the static path (:func:`transform`), so that the transformation of the datum is the
    same with the geoid grid and without, and a height above the ellipsoid of the local datum never stands for one
    above the geoid (:func:`..core.crs.horizontal`)."""
    return crs_mod.horizontal(crs)


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
    what = (f"cannot convert positions in ITRF2020 from {src} to {dst}" if dynamic(src) else
            f"cannot convert positions from {src} to {dst}, a system of ITRF2020")
    raise ValueError(f"{what}: PROJ knows the datum of {crs} only by a ballpark, no transformation from ETRF2000 "
                     "or WGS 84 where the positions are, and a ballpark can be metres off. Choose a coordinate system "
                     "on ETRF2000, ETRS89, RDN2008 or WGS 84")


def transform(points, src: str, dst: str, epoch=None) -> np.ndarray:
    """``(N, 3)`` points from ``src`` to ``dst``, the epochs of the points taken into account when one of the two is in
    ITRF2020 (:data:`DYNAMIC`); ``epoch`` is a decimal year (:func:`decimal_year`) for all of them, or one for each.

    From ITRF2020 to a European frame (ETRF2000 and the systems of RDN2008 and ETRS89, ETRF2014, ETRF2020) the
    time-dependent Helmert of the EPSG (``ITRF2020 to ETRF2000 (1)``...) at the epochs, then PROJ from the geographic
    system of that frame to ``dst`` (a conversion, or PROJ's null transformation from ETRF2000 to RDN2008 and ETRS89);
    to a system on WGS 84 ITRF2020 as it is, with no epoch needed; to another datum through ETRF2000 at the epochs by
    the transformation PROJ ranks first among those that are no ballpark where the points are, else through WGS 84 the
    same way (:func:`_route`) - into a system with heights above a geoid on such a datum, the plan by that
    transformation to its horizontal part and the height as the static path gives it for the point of ETRF2000 (or
    ITRF2020) read as WGS 84. The other way round the same steps backwards; between two systems of ITRF2020
    conversions only. Between heights above the ellipsoid and heights above a geoid every step is the static path's
    (:func:`..core.crs.by_proj`): with the grid of the geoid, or a :class:`.heights.GridMissing` that names it.

    Between two European frames (ETRF2014 or ETRF2020 and ETRF2000 with the systems of RDN2008 and ETRS89, ETRF2014 and
    ETRF2020: :func:`european_hop`) through ITRF2020 at the epochs: the inverse of the Helmert from ITRF2020 to the frame
    of ``src``, then the Helmert to the frame of ``dst`` - never PROJ's own null through the ETRS89 ensemble, 8-9 cm
    off at Rome in 2026.

    A row without a finite number is no position: it stays without one (NaN) and needs no epoch. A ``ValueError``
    without the epoch where the Helmert is applied, for an epoch that is not a year, and for a datum PROJ knows only by
    a ballpark. Between systems that are not in ITRF2020 :func:`..core.crs.transform_points`."""
    pts = np.atleast_2d(np.asarray(points, dtype=np.float64))
    if src == dst or crs_mod.is_local(src) or crs_mod.is_local(dst):
        return crs_mod.transform_points(pts, src, dst)  # a copy, or the refusal of a local frame
    source, target = frame_of(src), frame_of(dst)
    hop = european_hop(src, dst)
    if source not in DYNAMIC and target not in DYNAMIC and not hop:
        return crs_mod.by_proj(pts, src, dst)
    whole = np.isfinite(pts).all(axis=1)
    if not whole.all():
        out = np.full(pts.shape, np.nan)
        if whole.any():
            out[whole] = transform(pts[whole], src, dst, _rows(epoch, whole))
        return out
    if hop:  # ETRF2014 / ETRF2020 / ETRF2000 to another of them: through ITRF2020 at the epochs
        epochs = _epochs(epoch, len(pts), src, dst)
        itrf = _move(_convert(pts, src, FRAMES[source]), source, epochs, inverse=True)
        return _convert(_move(itrf, target, epochs), FRAMES[target], dst)
    if source in DYNAMIC and target in DYNAMIC:
        return _from_itrf2020(_to_itrf2020(pts, src), dst)
    if source in DYNAMIC:
        lonlat = _to_itrf2020(pts, src)
        hub, helmert, operation = _route(dst, target, lambda: lonlat, src, dst)
        moved = _move(lonlat, _european(hub), _epochs(epoch, len(pts), src, dst)) if helmert else lonlat
        if operation is None:
            return _convert(moved, hub, dst)
        across = _apply(operation, moved)  # the plan of dst (of its horizontal part)
        if _datum_target(dst) != dst:  # heights above a geoid: as the static path gives them, the point read as WGS 84
            across[:, 2] = crs_mod.transform_points(moved, FRAMES[UNKNOWN], dst)[:, 2]
        return across
    hub, helmert, operation = _route(src, source, lambda: _convert(pts, src, _geographic(src)), src, dst)
    if operation is None:
        at_hub = _convert(pts, src, hub)
    else:
        at_hub = _apply(operation, pts, inverse=True)  # from the plan of src
        if _datum_target(src) != src:  # heights above a geoid: back as the static path takes them to WGS 84
            at_hub[:, 2] = crs_mod.transform_points(pts, src, FRAMES[UNKNOWN])[:, 2]
    lonlat = _move(at_hub, _european(hub), _epochs(epoch, len(pts), src, dst), inverse=True) if helmert else at_hub
    return _from_itrf2020(lonlat, dst)


def _european(hub: str) -> str:
    """The European frame whose geographic 3D system is ``hub``."""
    return next(frame for frame in EUROPEAN if FRAMES[frame] == hub)


@functools.lru_cache(maxsize=64)
def _own_areas(src: str, dst: str, area: tuple[float, float, float, float] | None) -> tuple:
    """The areas of use (west, south, east, north in degrees; None for everywhere) of PROJ's transformations from
    ``src`` to ``dst`` that are no ballpark, among those valid in ``area``: none when PROJ has only a ballpark."""
    from pyproj.transformer import AreaOfInterest, TransformerGroup

    group = TransformerGroup(_parsed(src), _parsed(dst), always_xy=True, allow_ballpark=False,
                             area_of_interest=AreaOfInterest(*area) if area else None)
    return tuple(None if t.area_of_use is None else tuple(float(v) for v in t.area_of_use.bounds)
                 for t in group.transformers)


def _inside(lonlat: np.ndarray, areas: tuple) -> np.ndarray:
    """For each point (longitude, latitude), whether it is inside one of ``areas`` (west, south, east, north; across
    the 180th meridian when west is east of east; None for everywhere): where PROJ's own choice is one of their
    transformations, not its ballpark."""
    lon = (lonlat[:, 0] + 180.0) % 360.0 - 180.0
    lat = lonlat[:, 1]
    inside = np.zeros(len(lonlat), dtype=bool)
    for bounds in areas:
        if bounds is None:
            return np.ones(len(lonlat), dtype=bool)
        west, south, east, north = bounds
        across = (lon >= west) & (lon <= east) if west <= east else (lon >= west) | (lon <= east)
        inside |= across & (lat >= south) & (lat <= north)
    return inside


def legacy_transform(points, src: str, dst: str, legacy: Legacy) -> np.ndarray:
    """``(N, 3)`` points that carry no frame and epoch from ``src`` to ``dst``, one of the two a system of ITRF2020: by
    PROJ's own transformation for the points inside the area of use of one that is no ballpark - what the 1.6.0 did,
    bit for bit (into ETRS89 a Helmert at 2015.0, into WGS 84 as they are) - the others as positions on WGS 84, whose
    frame nobody knows: the numbers of ITRF2020 taken for those of WGS 84 (``EPSG:4979``), then converted as every
    position on WGS 84 is (into RDN2008 the very numbers of the 1.6.0; into Monte Mario, CH1903+ or DHDN within the
    accuracy of the transformation of the datum, where PROJ's ballpark was 73 to 163 m off). Never a ballpark from
    ITRF2020: PROJ would take its own beyond the edge of the area of its transformations. Between two systems of
    ITRF2020 conversions only, exact. :func:`legacy_warning` says it for the chunk of ``legacy`` (not for a system on
    WGS 84, where nothing is off)."""
    pts = np.atleast_2d(np.asarray(points, dtype=np.float64))
    if dynamic(src) and dynamic(dst):
        return transform(pts, src, dst)
    moving, other = (src, dst) if dynamic(src) else (dst, src)
    lonlat = _to_itrf2020(pts, src) if dynamic(src) else _convert(pts, src, _geographic(src))
    own = _inside(lonlat, _own_areas(src, dst, _area(lonlat)))
    out = np.empty(pts.shape, dtype=np.float64)
    if own.any():
        out[own] = crs_mod.by_proj(pts[own], src, dst)
    if not own.all():
        rest = ~own
        if dynamic(src):
            out[rest] = crs_mod.transform_points(lonlat[rest], FRAMES[UNKNOWN], dst)
        else:
            out[rest] = _from_itrf2020(crs_mod.transform_points(pts[rest], src, FRAMES[UNKNOWN]), dst)
    if frame_of(other) != WGS84:
        legacy_warning(legacy, moving)
    return out
