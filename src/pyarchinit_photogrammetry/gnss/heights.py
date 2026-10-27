"""Heights above the ellipsoid and heights above the geoid.

GNSS gives heights above the ellipsoid; the heights of a survey are above the sea level, the geoid: EGM2008
(``EPSG:3855``) the world over, some 40-50 m from the ellipsoid in Italy (48 m at Rome). PROJ converts between the two
with the grid of the geoid, ``us_nga_egm08_25.tif`` (EGM2008 every 2.5', public domain, 81 MB), which the program does
not ship: it is downloaded only when the user asks (:func:`download_geoid`) into the GNSS folder of the program
(:func:`grid_folder`), which PROJ searches (:func:`register`). Without the grid PROJ would give the heights as they
are - heights above the ellipsoid passed off as heights above the geoid, without a word - and PROJ never goes to the
network (:func:`.._env.proj_offline`): every conversion between the two kinds of height (:func:`height_kind`), whatever
its route, takes no ballpark and gives finite heights, or is refused (:class:`GridMissing`) with the name of the grid
and how to get it (:func:`..core.crs.by_proj`). A system without heights of its own (2D) keeps the heights as they
are.

A grid of the user's own - ITALGEO2005 bought from the IGM, ``geo_igm_mar06.grd`` of the EPSG operations 9727 and 9729
(heights of Genova 1942, ``EPSG:5214``) - is copied into :func:`grid_folder`, which ``ppg gnss geoid`` prints.

:func:`survey_crs` proposes the coordinate system of a survey where it is, :func:`set_survey_crs` puts a chunk into
one, :func:`check` says what does not fit between the heights of the photos and those of the chunk."""
from __future__ import annotations

import functools
import hashlib
import math
import os
import re
import sys
import warnings
from pathlib import Path

import numpy as np

from ..core import crs as crs_mod

GEOID_GRID = "us_nga_egm08_25.tif"  # EGM2008 every 2.5', as PROJ names it (NGA, public domain)
GEOID_URL = "https://cdn.proj.org/us_nga_egm08_25.tif"
GEOID_BYTES = 80585622
GEOID_SHA256 = "4191d471eefebf24091b56dbc604353cb3b8cf8cc70e448bb9ae56a272bef17a"  # of the file at GEOID_URL
GEOID_CRS = "EPSG:4326+3855"  # longitude, latitude and height above EGM2008 ("EGM2008 height", EPSG:3855)
DOWNLOAD_IN_THE_PROGRAM = "GNSS > Sistema e quote > Scarica il geoide EGM2008"
DOWNLOAD_COMMAND = "ppg gnss geoid --download"
ELLIPSOIDAL, ORTHOMETRIC, NONE = "ellipsoidal", "orthometric", "none"  # the kinds of height of a system
RDN2008 = "EPSG:6706"  # Italy is where RDN2008 is used ...
RDN2008_UTM = {32: "EPSG:7791", 33: "EPSG:7792", 34: "EPSG:7793"}  # ... and its UTM zones
# the products written in the coordinates of the system of the chunk (the clouds, the model and the tie points are in
# its internal frame, which the georeference places)
IN_THE_SYSTEM = ("dem", "orthomosaic", "dem_precision", "coverage")


class GridMissing(ValueError):
    """A conversion between heights above the ellipsoid and heights above a geoid that PROJ cannot make without a
    ballpark: ``grids`` names the grids it lacks for it (none when it has them all, and has no transformation where the
    points are)."""

    def __init__(self, message: str, grids=()):
        super().__init__(message)
        self.grids = tuple(grids)


# --- where PROJ finds the grids --------------------------------------------------------------------------------------


def grid_folder() -> Path:
    """The folder of the grids of PROJ that the program downloads, and of those of the user: ``proj`` in the GNSS folder
    (:func:`.download.gnss_dir`, ``PPG_GNSS_DIR`` for the tests). It is not made."""
    from .download import gnss_dir

    return gnss_dir() / "proj"


def register() -> None:
    """:func:`grid_folder` among the folders where PROJ looks for its grids (:func:`.._env.add_proj_folder`): pyproj's
    list, read by the PROJ of every thread that starts using pyproj from now on and by the current one at once (not
    ``PROJ_DATA``, which rasterio hands to GDAL as one folder: see :mod:`.._env`). Called when the package is imported -
    in the main thread, before any conversion and any other thread - and after a download (the GNSS folder may have
    changed). The folder need not exist: a grid put there later is found (:func:`refresh`). Idempotent."""
    from .. import _env

    if _env.add_proj_folder(os.path.abspath(grid_folder())):
        _forget_operations()


def refresh() -> None:
    """PROJ, in this thread, looks for its grids again - in :func:`grid_folder` as it is now too (:func:`register`):
    it keeps in memory the grids it looked for and did not find, so a grid put where it looks after a conversion that
    needed it (downloaded, copied there) would not be seen without this. The operations :mod:`.frames` keeps are chosen
    again too."""
    import pyproj.datadir

    register()
    pyproj.datadir.set_data_dir(pyproj.datadir.get_data_dir())  # the PROJ of this thread made again with its folders
    _forget_operations()


def _forget_operations() -> None:
    frames = sys.modules.get(f"{__package__}.frames")
    if frames is not None:
        frames.forget_operations()


def geoid_available() -> bool:
    """Whether PROJ has a grid of EGM2008 (:data:`GEOID_GRID` in :func:`grid_folder` or among its own data): heights
    above the geoid can be computed."""
    if _finds_egm2008():
        return True
    if (grid_folder() / GEOID_GRID).is_file():  # put there after PROJ looked for it in this thread
        refresh()
        return _finds_egm2008()
    return False


def _finds_egm2008() -> bool:
    from pyproj.transformer import TransformerGroup

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # "Best transformation is not available": what is asked here
        group = TransformerGroup("EPSG:4979", GEOID_CRS, always_xy=True, allow_ballpark=False)
    return bool(group.transformers)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_the_published_grid(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size == GEOID_BYTES and _sha256(path) == GEOID_SHA256
    except OSError:
        return False


def download_geoid(progress=None) -> Path:
    """The grid of EGM2008 downloaded from :data:`GEOID_URL` (cdn.proj.org, public domain) into :func:`grid_folder`,
    where PROJ finds it (:func:`register`, :func:`refresh`); returns its path. Only when asked: the program never
    downloads it by itself. The file is written aside and takes its name only when it has the size
    (:data:`GEOID_BYTES`) and the SHA-256 (:data:`GEOID_SHA256`) of the published grid: PROJ never sees half a grid,
    and a failure leaves nothing (a :class:`.download.DownloadError` that names the address). A grid already there and
    whole is not downloaded again. ``progress`` is the usual callback; the download can be cancelled through it."""
    from ..core.progress import as_progress
    from .download import DownloadError, fetch

    progress = as_progress(progress)
    target = grid_folder() / GEOID_GRID
    if not _is_the_published_grid(target):
        work = target.with_name(f"{GEOID_GRID}.download")
        try:
            fetch(GEOID_URL, work, progress, sha256=GEOID_SHA256)
            size = work.stat().st_size
            if size != GEOID_BYTES:
                raise DownloadError(f"{GEOID_URL}: {size} bytes, not the {GEOID_BYTES} bytes of the published grid",
                                    GEOID_URL)
            os.replace(work, target)
        finally:
            work.unlink(missing_ok=True)
    refresh()  # the folder in PROJ's search, and PROJ of this thread looking for the grid again
    progress(1.0, f"EGM2008 geoid in {target}")
    return target


# --- the kinds of height ---------------------------------------------------------------------------------------------


@functools.lru_cache(maxsize=256)
def height_kind(crs: str | None) -> str:
    """The kind of the heights of ``crs``: ``"ellipsoidal"`` (a geographic 3D or geocentric system, a projected one
    with heights above the ellipsoid), ``"orthometric"`` (a compound system with heights above a geoid, the sea level,
    or a vertical one) or ``"none"`` for a system without heights of its own (2D) and for a local frame: through such
    a system a height passes as it is."""
    if crs_mod.is_local(crs):
        return NONE
    system = crs_mod.to_pyproj(crs)
    if system.is_bound:
        system = system.source_crs
    if system.is_vertical:  # a vertical system, or a compound one with a vertical part
        return ORTHOMETRIC
    if system.is_geocentric or len(system.axis_info) >= 3:
        return ELLIPSOIDAL
    return NONE


def kinds_differ(src: str | None, dst: str | None) -> bool:
    """A conversion from ``src`` to ``dst`` turns heights above the ellipsoid into heights above a geoid, or back."""
    kinds = {height_kind(src), height_kind(dst)}
    return kinds == {ELLIPSOIDAL, ORTHOMETRIC}


def _label(crs: str) -> str:
    """``crs`` as a message names it: its code, or the name of a system given as WKT."""
    if len(crs) <= 40:
        return crs
    try:
        return crs_mod.to_pyproj(crs).name
    except Exception:  # noqa: BLE001 - the text as it is
        return crs[:40] + "..."


def _missing_grids(src: str, dst: str) -> list[tuple[str, str]]:
    """The grids PROJ lacks to convert from ``src`` to ``dst`` (name, the address where PROJ publishes it or ""): the
    EGM2008 grid when one of its operations that are no ballpark needs it, else those of the first of them it cannot
    use. None when it has one it can use: the points are where that one does not reach."""
    from pyproj.transformer import TransformerGroup

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # "Best transformation is not available": what the message says
        try:
            group = TransformerGroup(crs_mod.to_pyproj(src), crs_mod.to_pyproj(dst), always_xy=True,
                                     allow_ballpark=False)
        except Exception:  # noqa: BLE001 - PROJ cannot tell: nothing more to say than that it cannot convert
            return []
    if group.transformers:
        return []
    lacking = [[(grid.short_name, grid.url or "") for grid in operation.grids if not grid.available]
               for operation in group.unavailable_operations]
    lacking = [missing for missing in lacking if missing]
    egm2008 = next(((name, url) for missing in lacking for name, url in missing if name == GEOID_GRID), None)
    if egm2008 is not None:  # the grid the program downloads, before another grid of the same geoid
        return [egm2008]
    return lacking[0] if lacking else []


def refusal(src: str, dst: str) -> GridMissing:
    """Why heights cannot go from ``src`` to ``dst`` (one of the two with heights above the ellipsoid, the other above
    a geoid), and what to do: the grid of the geoid PROJ needs and how to get it - EGM2008 from the program, another one
    copied into :func:`grid_folder` - or, when it has its grids, that it has no transformation where the points are."""
    above = {ELLIPSOIDAL: "the ellipsoid", ORTHOMETRIC: "the geoid"}
    here, there = above[height_kind(src)], above[height_kind(dst)]
    what = f"heights above {here} ({_label(src)}) cannot become heights above {there} ({_label(dst)})"
    grids = _missing_grids(src, dst)
    names = [name for name, _url in grids]
    if GEOID_GRID in names:
        message = (f"{what}: PROJ needs the grid of the geoid {GEOID_GRID} (EGM2008), which is not installed, and "
                   f"without it would give the heights as they are - heights above {here} passed off as heights "
                   f"above {there}, 48 m off at Rome. Download it with {DOWNLOAD_IN_THE_PROGRAM}, or with "
                   f"`{DOWNLOAD_COMMAND}` (81 MB, public domain), into {grid_folder()}")
    elif names:
        published = [url for _name, url in grids if url]
        one = len(names) == 1
        them = "it" if one else "them"
        message = (f"{what}: PROJ needs the {'grid' if one else 'grids'} {' and '.join(names)}, which "
                   f"{'is' if one else 'are'} not installed. Copy {them} into {grid_folder()}, where the program has "
                   f"PROJ look for {them} (`ppg gnss geoid` prints the folder)"
                   + (f"; PROJ publishes {them} at {' and '.join(published)}" if published else ""))
    else:
        message = f"{what}: PROJ has no transformation between them that is no ballpark where the points are"
    return GridMissing(message, names)


def _lonlat(lat, lon) -> tuple[float, float]:
    """Longitude and latitude as numbers of the earth (the longitude within [-180, 180)); a ``ValueError`` otherwise."""
    lat, lon = float(lat), float(lon)
    if not (math.isfinite(lat) and -90.0 <= lat <= 90.0):
        raise ValueError(f"{lat} is not a latitude (degrees between -90 and 90)")
    if not math.isfinite(lon):
        raise ValueError(f"{lon} is not a longitude (degrees)")
    return (lon + 180.0) % 360.0 - 180.0, lat


def undulation(lat: float, lon: float) -> float:
    """The height of the geoid EGM2008 above the ellipsoid of WGS 84 at ``lat``, ``lon`` (degrees), in metres: N = h -
    H, the height above the ellipsoid less the height above the geoid (48 m at Rome). A :class:`GridMissing` without
    the grid (:func:`download_geoid`), a ``ValueError`` for a place off the earth."""
    lon, lat = _lonlat(lat, lon)
    out = crs_mod.transform_points([[lon, lat, 0.0]], "EPSG:4979", GEOID_CRS)  # no ballpark (core.crs.by_proj)
    return float(0.0 - out[0, 2])


# --- the heights of a chunk ------------------------------------------------------------------------------------------


def _photos(count: int) -> str:
    return f"{count} photo has" if count == 1 else f"{count} photos have"


def _reference_crs(chunk, camera) -> str:
    return camera.meta.get("reference_crs", chunk.crs)


def _grid_missing_at(camera, chunk) -> GridMissing | None:
    """The refusal of a height above the ellipsoid where ``camera`` is going into the system of ``chunk`` - the grid
    PROJ lacks for it - or None: PROJ has it, or the place of the photo cannot be told (then its own conversion says
    why)."""
    try:
        where = crs_mod.transform_points([camera.reference.location], _reference_crs(chunk, camera), "EPSG:4326",
                                         epoch=(camera.meta.get("gnss") or {}).get("epoch"))[0]
    except ValueError:
        return None
    try:
        crs_mod.transform_points([[where[0], where[1], 0.0]], "EPSG:4979", chunk.crs)
    except GridMissing as exc:
        return exc
    except ValueError:
        return None
    return None


def check(chunk) -> list[str]:
    """What does not fit between the heights of the positions of the photos of ``chunk`` and those of its coordinate
    system, a sentence each (nothing for a chunk in a local frame):

    * photos with heights above the ellipsoid (GNSS: an MRK, a PPK, a positions file of such heights) and a system of
      the chunk without heights of its own (2D): the heights of the chunk are above the ellipsoid, some 40-50 m from
      those above the sea level in Italy - with markers on sea-level heights, a system with EGM2008 heights;
    * a system with heights above the geoid whose grid PROJ does not have, for photos with heights above the
      ellipsoid: converting them is refused, with the grid and how to get it;
    * photos with heights above the ellipsoid given in a system without heights of its own (``EPSG:4326``) and a
      system of the chunk with heights above the geoid: their heights go into it as they are."""
    if crs_mod.is_local(chunk.crs):
        return []
    kind = height_kind(chunk.crs)
    placed = [cam for cam in chunk.cameras if cam.reference.location and cam.reference.enabled]
    ellipsoidal = [cam for cam in placed if (cam.meta.get("gnss") or {}).get("height") == ELLIPSOIDAL]
    out = []
    if kind == NONE and ellipsoidal:
        choice = f" ({chunk.crs}+3855, for one)" if re.fullmatch(r"EPSG:\d+", chunk.crs) else ""
        out.append(f"{_photos(len(ellipsoidal))} heights above the ellipsoid (GNSS) and the coordinate system of the "
                   f"chunk, {chunk.crs}, has no heights of its own: the heights of the chunk are above the ellipsoid, "
                   "some 40-50 m from those above the sea level in Italy. With markers on sea-level heights choose a "
                   f"coordinate system with EGM2008 heights{choice}")
    if kind == ORTHOMETRIC:
        given = [cam for cam in placed if height_kind(_reference_crs(chunk, cam)) == ELLIPSOIDAL]
        missing = _grid_missing_at(given[0], chunk) if given else None
        if missing is not None:
            out.append(str(missing))
        flat = [cam for cam in ellipsoidal if height_kind(_reference_crs(chunk, cam)) == NONE]
        if flat:
            systems = ", ".join(sorted({_reference_crs(chunk, cam) for cam in flat}))
            out.append(f"{_photos(len(flat))} heights above the ellipsoid given in a system without heights of its "
                       f"own ({systems}): they go into the heights above the geoid of the chunk ({chunk.crs}) as they "
                       "are, some 40-50 m off in Italy. Give them in a system with heights above the ellipsoid "
                       "(EPSG:4979 for longitude, latitude and height)")
    return out


# --- the coordinate system of a survey -------------------------------------------------------------------------------


@functools.lru_cache(maxsize=64)
def _bounds(crs: str) -> tuple[float, float, float, float] | None:
    area = crs_mod.to_pyproj(crs).area_of_use
    return None if area is None else tuple(area.bounds)


@functools.lru_cache(maxsize=64)
def _known(crs: str) -> bool:
    from pyproj.exceptions import CRSError

    try:
        crs_mod.to_pyproj(crs)
    except CRSError:
        return False
    return True


def _inside(crs: str, lon: float, lat: float) -> bool:
    bounds = _bounds(crs)
    return bounds is not None and bounds[0] <= lon <= bounds[2] and bounds[1] <= lat <= bounds[3]


def survey_crs(lon: float, lat: float, frame: str = "WGS84", geoid: bool = True) -> str:
    """The coordinate system for a survey at ``lon``, ``lat`` (degrees) whose positions are in ``frame`` (one of
    :data:`.frames.FRAMES`, or ``"WGS84"``): the UTM zone of the place, on WGS 84 (``EPSG:326zz``, ``EPSG:327zz`` in
    the south) for WGS 84, a frame nobody knows and ITRF2020 (WGS 84 for the program); for the European frames
    (ETRF2000, the frame of RDN2008 and ETRS89, ETRF2014, ETRF2020) RDN2008 / UTM in Italy - where RDN2008 is used:
    zones 32, 33 and 34, ``EPSG:7791``, ``7792``, ``7793`` -, ETRS89 / UTM where its zone exists (``EPSG:258zz``),
    else WGS 84 / UTM. With ``geoid`` its heights are above EGM2008 (``+3855``), which needs the grid of the geoid
    (:func:`download_geoid`); without, it has no heights of its own. A ``ValueError`` for a frame the program does not
    know and for a place off the earth."""
    from . import frames

    lon, lat = _lonlat(lat, lon)
    frames.crs_of(frame)  # a frame the program knows
    zone = int((lon + 180.0) // 6.0) + 1
    system = None
    if frame in frames.EUROPEAN:
        if zone in RDN2008_UTM and _inside(RDN2008, lon, lat):
            system = RDN2008_UTM[zone]
        elif lat >= 0.0 and _known(f"EPSG:{25800 + zone}"):
            system = f"EPSG:{25800 + zone}"
    system = system or crs_mod.utm_for_lonlat(lon, lat)
    return f"{system}+3855" if geoid else system


def _pinned_references(chunk, old: str) -> None:
    """The coordinates of the markers of ``chunk``, in ``old`` (its system until now), converted into the system of
    the chunk (as points that carry no frame and epoch); the positions of photos given in the system of the chunk
    (without a system of their own) keep ``old``."""
    from ..core.camera import Reference
    from ..markers import gcp
    from . import frames

    marked = [m for m in chunk.markers if m.reference.location]
    if marked:
        moved = crs_mod.transform_points(np.array([m.reference.location for m in marked], float), old, chunk.crs,
                                         legacy=frames.Legacy(gcp.MARKERS_READ, chunk))
        for m, location in zip(marked, moved):
            m.reference = Reference(location=[float(v) for v in location], accuracy=list(m.reference.accuracy),
                                    enabled=m.reference.enabled)
    for cam in chunk.cameras:
        if cam.reference.location and "reference_crs" not in cam.meta:
            cam.meta["reference_crs"] = old


def set_survey_crs(chunk, crs: str) -> dict:
    """``chunk`` in the coordinate system ``crs``, a projected one (the products need metric axes; :func:`survey_crs`
    proposes one): the coordinates of its markers converted into it (as points that carry no frame and epoch), the
    positions of the photos read from their own system, as always; when the chunk is aligned and georeferenced (in a
    system that is no local frame) its georeference fitted again on them (:func:`..markers.gcp.update_transform`), and
    the precision of the old one dropped. From a local frame the chunk only takes the system (its markers are where they
    are): fit its georeference after.

    Returns ``{"crs", "updated": whether the georeference was fitted again, "rebuild": the products built before in the
    coordinates of the old system (DEM, orthomosaic, precision, coverage) - to build again -, "report": the report of
    the fit or None}``; the clouds, the model and the tie points are in the internal frame of the chunk, which the new
    georeference places. A ``ValueError``, and the chunk as it was, for a system PROJ does not know, a geographic one
    or a local frame, when the references cannot go into it (heights above the geoid without the grid:
    :class:`GridMissing`) and when nothing fits the georeference again (no control markers, no positions of the
    photos)."""
    from ..markers import gcp

    if crs_mod.is_local(crs):
        raise ValueError(f"{crs!r} is a local frame, not the coordinate system of a survey")
    try:
        system = crs_mod.to_pyproj(crs)
    except Exception as exc:  # noqa: BLE001 - PROJ's own words
        raise ValueError(f"{crs!r} is not a coordinate system PROJ knows ({exc})") from None
    if system.is_geographic:
        raise ValueError(f"{crs} is geographic: a survey needs a projected coordinate system (the UTM zone, "
                         "survey_crs proposes one)")
    old = chunk.crs
    rebuild = [name for name in IN_THE_SYSTEM if chunk.products.get(name)]
    if crs == old:
        return {"crs": crs, "updated": False, "rebuild": [], "report": None}
    aligned, georeferenced = bool(chunk.aligned_cameras), not crs_mod.is_local(old)
    before = (chunk.crs, chunk.transform.copy(),
              [(m, m.reference, None if m.position is None else m.position.copy()) for m in chunk.markers],
              [cam for cam in chunk.cameras if "reference_crs" not in cam.meta])
    report = None
    try:
        chunk.crs = crs
        if georeferenced:
            _pinned_references(chunk, old)
        if aligned and georeferenced:
            report = gcp.update_transform(chunk)
            if report["method"] is None or not ("markers" in report["method"] or "cameras" in report["method"]):
                raise ValueError(f"nothing fits the georeference of the chunk again in {crs}: no control markers and "
                                 "no positions of the photos, the system was not changed")
    except BaseException:
        chunk.crs, chunk.transform = before[0], before[1]
        for m, reference, position in before[2]:
            m.reference, m.position = reference, position
        for cam in before[3]:
            cam.meta.pop("reference_crs", None)
        raise
    if report is not None:
        chunk.forget_precision()
    return {"crs": crs, "updated": report is not None, "rebuild": rebuild, "report": report}
