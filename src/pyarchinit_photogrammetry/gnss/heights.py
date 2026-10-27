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
RDN2008_UTM = {32: "EPSG:7791", 33: "EPSG:7792", 34: "EPSG:7793"}  # RDN2008 / UTM, the zones of Italy (in_italy)
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
    needed it (downloaded, copied there) would not be seen without this. The transformations :mod:`..core.crs` and
    :mod:`.frames` keep are made again too."""
    import pyproj.datadir

    register()
    pyproj.datadir.set_data_dir(pyproj.datadir.get_data_dir())  # the PROJ of this thread made again with its folders
    _forget_operations()


def _forget_operations() -> None:
    crs_mod.forget_transformers()
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

# Italy, coarsely (longitude, latitude; a few kilometres out to sea): the mainland with its islands near the coast, Elba
# and the Tuscan islands, Ponza and Ventotene, Capri and Ischia, the Tremiti; Sicily with the Aeolian islands, the Egadi
# and Ustica; Sardinia; Pantelleria; the Pelagie. Corsica, Malta, Nice, Ticino, Istria are out.
ITALY = (
    ((7.53, 43.78), (7.67, 44.18), (6.95, 44.32), (6.85, 44.53), (7.05, 44.7), (6.62, 45.1), (7.13, 45.25),
     (6.8, 45.65), (6.8, 45.83), (7.05, 45.93), (7.66, 45.98), (7.87, 45.92), (8.1, 46.25), (8.44, 46.46),
     (8.6, 46.1), (8.85, 45.95), (9.02, 45.82), (9.25, 46.2), (9.33, 46.5), (9.55, 46.3), (10.15, 46.22),
     (10.05, 46.45), (10.15, 46.63), (10.47, 46.62), (10.49, 46.85), (11.1, 46.95), (11.52, 47.02), (12.18, 47.09),
     (12.7, 46.65), (13.72, 46.52), (13.38, 46.32), (13.55, 46.05), (13.65, 45.9), (13.92, 45.68), (13.77, 45.58),
     (13.4, 45.66), (13.1, 45.6), (12.45, 45.3), (12.6, 44.95), (12.4, 44.4), (12.75, 44.0), (13.75, 43.55),
     (14.4, 42.4), (15.05, 42.05), (15.6, 42.2), (16.3, 41.9), (17.0, 41.15), (18.05, 40.65), (18.6, 40.15),
     (18.4, 39.72), (17.9, 40.05), (17.1, 40.45), (16.55, 40.0), (17.25, 39.0), (16.6, 38.6), (16.1, 37.85),
     (15.59, 37.95), (15.595, 38.17), (15.645, 38.27), (15.7, 38.4), (15.85, 38.7), (16.0, 39.4), (15.65, 40.0),
     (14.8, 40.55), (14.15, 40.48), (13.8, 40.65), (13.45, 40.72), (12.8, 40.85), (12.55, 41.4), (12.2, 41.7),
     (11.7, 42.05), (11.15, 42.2), (10.25, 42.15), (9.98, 42.55), (9.95, 42.95), (9.76, 42.98), (9.76, 43.12),
     (9.85, 43.48), (10.2, 43.7), (9.8, 44.05), (9.0, 44.35), (8.4, 44.2), (8.0, 43.82)),
    ((15.63, 38.27), (15.35, 38.9), (14.3, 38.85), (13.05, 38.8), (12.35, 38.15), (11.95, 38.0), (12.3, 37.6),
     (13.05, 37.4), (14.2, 36.95), (15.05, 36.55), (15.4, 37.0), (15.3, 37.8), (15.575, 37.95), (15.58, 38.17)),
    ((8.1, 41.15), (9.2, 41.32), (9.55, 41.3), (9.85, 40.95), (9.9, 40.4), (9.75, 39.9), (9.7, 39.1), (9.05, 39.0),
     (8.55, 38.8), (8.25, 38.9), (8.15, 39.2), (8.3, 39.6), (8.3, 40.1), (8.05, 40.6)),
    ((11.88, 36.7), (12.1, 36.7), (12.1, 36.88), (11.88, 36.88)),
    ((12.25, 35.45), (12.95, 35.45), (12.95, 35.92), (12.25, 35.92)),
)
# what set_survey_crs converts into the new system, and what it lists as kept in the old one
CONVERTED = ("markers", "units", "quote", "points", "stones", "sections", "patches", "texture_patches", "region",
             "colour_chart")
KEPT = ("versions", "elevations", "multispectral", "thermal", "satellite", "relief", "calculator", "proxies", "stones",
        "sections", "section_drawings", "drawn")
DRAWN = "what was drawn on the products of the chunk"  # for the warning of the points that carry no frame and epoch
SECTIONS = Path("sezioni") / "sezioni.json"  # the sections of a project, beside its file


@functools.lru_cache(maxsize=1)
def _italy():
    import shapely

    return shapely.MultiPolygon([shapely.Polygon(ring) for ring in ITALY])


def in_italy(lon: float, lat: float) -> bool:
    """Whether ``lon``, ``lat`` (degrees) are in Italy, by its coarse outline (:data:`ITALY`)."""
    import shapely

    return bool(_italy().covers(shapely.Point(float(lon), float(lat))))


@functools.lru_cache(maxsize=64)
def _known(crs: str) -> bool:
    from pyproj.exceptions import CRSError

    try:
        crs_mod.to_pyproj(crs)
    except CRSError:
        return False
    return True


def survey_crs(lon: float, lat: float, frame: str = "WGS84", geoid: bool = True) -> str:
    """The coordinate system for a survey at ``lon``, ``lat`` (degrees) whose positions are in ``frame`` (one of
    :data:`.frames.FRAMES`, or ``"WGS84"``): the UTM zone of the place, on WGS 84 (``EPSG:326zz``, ``EPSG:327zz`` in
    the south) for WGS 84, a frame nobody knows and ITRF2020 (WGS 84 for the program); for the European frames
    (ETRF2000, the frame of RDN2008 and ETRS89, ETRF2014, ETRF2020) RDN2008 / UTM in Italy (:func:`in_italy`; zones 32,
    33 and 34, ``EPSG:7791``, ``7792``, ``7793``), ETRS89 / UTM where its zone exists (``EPSG:258zz``), else WGS 84 /
    UTM. With ``geoid`` its heights are above EGM2008 (``+3855``), which needs the grid of the geoid
    (:func:`download_geoid`); without, it has no heights of its own. A ``ValueError`` for a frame the program does not
    know and for a place off the earth."""
    from . import frames

    lon, lat = _lonlat(lat, lon)
    frames.crs_of(frame)  # a frame the program knows
    zone = int((lon + 180.0) // 6.0) + 1
    system = None
    if frame in frames.EUROPEAN:
        if zone in RDN2008_UTM and in_italy(lon, lat):
            system = RDN2008_UTM[zone]
        elif lat >= 0.0 and _known(f"EPSG:{25800 + zone}"):
            system = f"EPSG:{25800 + zone}"
    system = system or crs_mod.utm_for_lonlat(lon, lat)
    return f"{system}+3855" if geoid else system


def _survey_system(crs: str) -> None:
    """A ``ValueError`` unless ``crs`` is a system with a projected plan (alone, or the horizontal part of a compound
    one): the products of a survey need metric axes."""
    if crs_mod.is_local(crs):
        raise ValueError(f"{crs!r} is a local frame, not the coordinate system of a survey")
    try:
        system = crs_mod.to_pyproj(crs)
    except Exception as exc:  # noqa: BLE001 - PROJ's own words
        raise ValueError(f"{crs!r} is not a coordinate system PROJ knows ({exc})") from None
    plan = system.sub_crs_list[0] if system.is_compound else system
    if plan.is_bound:
        plan = plan.source_crs
    if not plan.is_projected:
        kind = ("geographic" if plan.is_geographic else "geocentric" if plan.is_geocentric
                else "a vertical system" if plan.is_vertical else "not projected")
        raise ValueError(f"{crs} is {kind}: a survey needs a projected coordinate system (the UTM zone; survey_crs "
                         "proposes one)")


# what was drawn on the products of a chunk, in the coordinates of its system

def _finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _plan(xy, old: str, new: str, chunk) -> np.ndarray:
    """``(N, 2)`` plan coordinates from ``old`` to ``new``: through the horizontal parts of the two systems, no height
    needed."""
    from . import frames

    xy = np.asarray(xy, float).reshape(-1, 2)
    if not len(xy):
        return xy.copy()
    pts = np.column_stack([xy, np.zeros(len(xy))])
    return crs_mod.transform_points(pts, crs_mod.horizontal(old), crs_mod.horizontal(new),
                                    legacy=frames.Legacy(DRAWN, chunk))[:, :2]


def _wkt(text: str, old: str, new: str, chunk) -> str:
    """A geometry (WKT) with its plan from ``old`` to ``new``."""
    import shapely

    return shapely.transform(shapely.from_wkt(text), lambda xy: _plan(xy, old, new, chunk)).wkt


def _move_rows(rows: list[dict], height: str, old: str, new: str, chunk) -> None:
    """Records with ``x``, ``y`` and the height ``height`` from ``old`` to ``new``, in place: the plan, and the height
    by the rule of every conversion (:func:`..core.crs.transform_points`: through a system without heights of its own
    as it is); a record without a height its plan only."""
    from . import frames

    tall = [r for r in rows if _finite(r.get(height))]
    if tall:
        xyz = crs_mod.transform_points([[float(r["x"]), float(r["y"]), float(r[height])] for r in tall], old, new,
                                       legacy=frames.Legacy(DRAWN, chunk))
        for r, (x, y, z) in zip(tall, xyz.tolist()):
            r["x"], r["y"], r[height] = x, y, z
    flat = [r for r in rows if not _finite(r.get(height))]
    if flat:
        for r, (x, y) in zip(flat, _plan([[float(r["x"]), float(r["y"])] for r in flat], old, new, chunk).tolist()):
            r["x"], r["y"] = x, y


def _ortho_name(chunk) -> str | None:
    """The name of the file of the orthomosaic: the stones drawn on it are kept under it."""
    rel = chunk.products.get("orthomosaic")
    return Path(rel).name if rel else None


def _units(drawings: dict) -> list[dict]:
    return [u for u in drawings.get("units") or [] if u.get("wkt")]


def _rows(drawings: dict, key: str) -> list[dict]:
    return [r for r in drawings.get(key) or [] if "x" in r and "y" in r]


def _sections_file(chunk) -> Path | None:
    path = getattr(chunk.document, "path", None)
    return None if path is None else Path(path).parent / SECTIONS


def _project_sections(chunk, old: str) -> tuple[list | None, str]:
    """The sections of the project of ``chunk`` and what becomes of them: ``"convert"``, ``"kept"`` (another chunk of
    the project is still in ``old``, or the file cannot be read) or ``"none"`` (no file)."""
    import json

    path = _sections_file(chunk)
    if path is None or not path.exists():
        return None, "none"
    try:
        sections = [dict(s) for s in json.loads(path.read_text(encoding="utf-8"))]
    except (OSError, ValueError, TypeError):
        return None, "kept"
    others = [c for c in getattr(chunk.document, "chunks", []) if c is not chunk and c.crs == old]
    return sections, ("kept" if others else "convert")


def _counts(chunk, georeferenced: bool, refit: bool, sections: list | None, fate: str) -> dict:
    """How many of each kind of :data:`CONVERTED` :func:`set_survey_crs` converts."""
    meta, counts = chunk.meta, dict.fromkeys(CONVERTED, 0)
    if not georeferenced:
        return counts
    drawings = meta.get("drawings") or {}
    counts["markers"] = sum(1 for m in chunk.markers if m.reference.location)
    counts["units"] = len(_units(drawings))
    counts["quote"] = len(_rows(drawings, "quote"))
    counts["points"] = len(_rows(drawings, "points"))
    counts["stones"] = len((meta.get("stones") or {}).get(_ortho_name(chunk)) or [])
    counts["sections"] = len(sections) if fate == "convert" and sections else 0
    counts["patches"] = len((meta.get("orthomosaic") or {}).get("patches") or [])
    counts["texture_patches"] = sum(1 for p in (meta.get("texture") or {}).get("patches") or [] if p.get("polygon"))
    counts["region"] = int(refit and bool(meta.get("region")))
    counts["colour_chart"] = int(bool((meta.get("colour_chart") or {}).get("bounds")))
    return counts


def _kept(chunk, georeferenced: bool, sections: list | None, fate: str) -> list[dict]:
    """What :func:`set_survey_crs` leaves in the old system, ``[{"what": one of KEPT, "names": [...]}]``."""
    meta, kept = chunk.meta, []

    def add(what: str, names) -> None:
        names = [str(n) for n in names if n is not None and n != ""]
        if names:
            kept.append({"what": what, "names": names})

    versions = meta.get("versions") or {}
    add("versions", [f"{product}: {e.get('name') or e.get('id')}" for product in ("dem", "orthomosaic")
                     for e in versions.get(product) or []])
    add("elevations", [e.get("name") or e.get("id") for e in meta.get("elevations") or []])
    ms = meta.get("multispectral") or {}
    add("multispectral", (["reflectance"] if ms.get("reflectance") else []) + list(ms.get("indices") or {})
        + [f"zones {name}" for name in ms.get("zoning") or {}])
    th = meta.get("thermal") or {}
    add("thermal", (["temperature"] if th.get("file") else []) + (["anomalies"] if th.get("anomaly") else []))
    sat = meta.get("satellite") or {}
    add("satellite", [r.get("name") for kind in ("scenes", "stereo") for r in sat.get(kind) or []
                      if isinstance(r, dict)])
    add("relief", list(meta.get("relief") or {}))
    add("calculator", list(meta.get("calculator") or {}))
    add("proxies", [p.get("name") for p in meta.get("proxies") or []])
    ortho = _ortho_name(chunk) if georeferenced else None
    add("stones", [key for key, rows in (meta.get("stones") or {}).items() if rows and key != ortho])
    if fate == "kept" or (sections and not georeferenced):
        add("sections", [s.get("label") for s in sections or []] or [str(SECTIONS)])
    elif fate == "convert" and sections:
        add("section_drawings", [s.get("label") for s in sections if s.get("svg") or s.get("dxf") or s.get("dxf3d")])
    if not georeferenced:  # a local frame: what was drawn and the markers stay where they are
        counts = _counts(chunk, True, False, None, "none")
        add("drawn", [what for what in CONVERTED if counts[what] and what != "stones"])  # the stones: above
    return kept


def _drawn(chunk, old: str, new: str, sections: list | None) -> tuple[dict, list | None]:
    """What was drawn on the products of ``chunk``, converted from ``old`` to ``new`` aside (nothing of the chunk
    changes; a conversion that cannot be made raises here): the new values of the keys of ``chunk.meta`` and the new
    sections of the project (None to leave them)."""
    import copy

    meta, values = chunk.meta, {}
    if meta.get("drawings"):
        drawings = copy.deepcopy(meta["drawings"])
        for unit in _units(drawings):
            unit["wkt"] = _wkt(unit["wkt"], old, new, chunk)
        _move_rows(_rows(drawings, "quote"), "quota_q", old, new, chunk)
        _move_rows(_rows(drawings, "points"), "z", old, new, chunk)
        values["drawings"] = drawings
    ortho = _ortho_name(chunk)
    if (meta.get("stones") or {}).get(ortho):
        stones = copy.deepcopy(meta["stones"])
        stones[ortho] = [_wkt(w, old, new, chunk) for w in stones[ortho]]
        values["stones"] = stones
    for key, lists in (("orthomosaic", ("patches", "applied_patches")), ("texture", ("patches",))):
        if any((meta.get(key) or {}).get(name) for name in lists):
            record = copy.deepcopy(meta[key])
            for name in lists:
                for patch in record.get(name) or []:
                    if patch.get("polygon"):
                        polygon = np.asarray(patch["polygon"], float)[:, :2]
                        patch["polygon"] = _plan(polygon, old, new, chunk).tolist()
            values[key] = record
    if (meta.get("colour_chart") or {}).get("bounds"):
        chart = copy.deepcopy(meta["colour_chart"])
        x0, y0, x1, y1 = (float(v) for v in chart["bounds"])
        corners = _plan([[x0, y0], [x1, y0], [x0, y1], [x1, y1]], old, new, chunk)
        chart["bounds"] = [float(v) for v in (*corners.min(axis=0), *corners.max(axis=0))]
        values["colour_chart"] = chart
    moved = None
    if sections:
        moved = copy.deepcopy(sections)
        for s in moved:
            ends = _plan([s["p1"], s["p2"]], old, new, chunk)
            s["p1"], s["p2"] = ends[0].tolist(), ends[1].tolist()
            s["length"] = float(np.hypot(*(ends[1] - ends[0])))
    return values, moved


def _region_follows(region: dict, before: np.ndarray, after: np.ndarray) -> dict:
    """The region of the reconstruction where the chunk takes it: from its old world (``before``, the transform of the
    chunk) to its new one (``after``) - the same part of the reconstruction, whatever the heights of the two systems."""
    from ..engine.region import Region

    box = Region.from_dict(region)
    s = np.asarray(after, float) @ np.linalg.inv(np.asarray(before, float))
    scale = float(np.cbrt(abs(np.linalg.det(s[:3, :3]))))
    return Region(s[:3, :3] @ box.center + s[:3, 3], box.size * scale, s[:3, :3] / scale @ box.rotation).to_dict()


def _write_sections(path: Path, sections: list) -> None:
    import json

    work = path.with_name(path.name + ".tmp")
    try:
        work.write_text(json.dumps(sections, indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(work, path)
    finally:
        work.unlink(missing_ok=True)


def _pinned_references(chunk, old: str) -> None:
    """The coordinates of the markers of ``chunk``, in ``old`` (its system until now), converted into the system of
    the chunk (as points that carry no frame and epoch; their heights by the rule of every conversion); the positions
    of photos given in the system of the chunk (without a system of their own) keep ``old``."""
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


def survey_crs_changes(chunk, crs: str) -> dict:
    """What :func:`set_survey_crs` would do to ``chunk`` with ``crs``, nothing changed: ``{"crs", "refit": whether the
    georeference would be fitted again, "convert": {kind: how many} of :data:`CONVERTED`, "rebuild", "kept",
    "heights_unchanged"}`` as :func:`set_survey_crs` describes them - for a window to show before applying. A
    ``ValueError`` for a system that is not one of a survey."""
    _survey_system(crs)
    old = chunk.crs
    if crs == old:
        return {"crs": crs, "refit": False, "convert": dict.fromkeys(CONVERTED, 0), "rebuild": [], "kept": [],
                "heights_unchanged": False}
    georeferenced = not crs_mod.is_local(old)
    refit = georeferenced and bool(chunk.aligned_cameras)
    sections, fate = _project_sections(chunk, old)
    return {"crs": crs, "refit": refit, "convert": _counts(chunk, georeferenced, refit, sections, fate),
            "rebuild": [name for name in IN_THE_SYSTEM if chunk.products.get(name)],
            "kept": _kept(chunk, georeferenced, sections, fate),
            "heights_unchanged": georeferenced and height_kind(old) == NONE and height_kind(crs) != NONE}


def set_survey_crs(chunk, crs: str) -> dict:
    """``chunk`` in the coordinate system ``crs``, one with a projected plan (the products need metric axes; alone or
    with heights above a geoid, as :func:`survey_crs` proposes it). When the chunk is georeferenced (in a system that is
    no local frame):

    * the references of its markers - measured coordinates - are converted into it (as points that carry no frame and
      epoch), the positions of the photos are read from their own system as always, and an aligned chunk has its
      georeference fitted again on them (:func:`..markers.gcp.update_transform`, the precision of the old fit dropped);
    * what was drawn on its products (:data:`CONVERTED`) is converted too: the units, the spot heights and the measuring
      points (``chunk.meta["drawings"]``), the stones of the orthomosaic, the sections of the project
      (``sezioni/sezioni.json`` beside its file, when no other chunk of the project is still in the old system), the
      patches of the orthomosaic and of the texture, the box of the colour chart - their plan by the conversion of the
      two systems, the heights by the rule of every conversion (:func:`..core.crs.transform_points`); the region of the
      reconstruction follows the chunk (from its old georeference to the new one), so that it encloses the same part of
      the reconstruction. A window that holds the drawings stores them into the chunk first and reads them again after.

    The heights of the markers, spot heights and measuring points of a system without heights of its own (2D) pass
    into the new one as they are (``heights_unchanged``): right only if they were heights above the sea level, as the
    new system's are with EGM2008, not heights above the ellipsoid. From a local frame the chunk only takes the system:
    its markers and what was drawn stay where they are (``kept``, ``"drawn"``); fit its georeference after.

    Returns ``{"crs", "updated": whether the georeference was fitted again, "rebuild": the products built in the
    coordinates of the old system (DEM, orthomosaic, precision, coverage) - to build again -, "kept": what stays in the
    old system and cannot be converted, ``[{"what", "names"}]`` - inactive versions of the DEM and the orthomosaic,
    elevations (frames, and the stones drawn on them), multispectral, thermal and satellite rasters, the rasters of the
    relief and of the raster calculator, the proxies of the units, the drawings (SVG, DXF) of the sections, or the
    sections themselves -, "converted": {kind: how many}, "heights_unchanged", "report": the report of the fit or
    None}``; the clouds, the model and the tie points are in the internal frame of the chunk, which the georeference
    places. :func:`survey_crs_changes` tells all of it before. A ``ValueError``, and the chunk, its drawings and the
    file of the sections as they were, for a system that is not one of a survey, when the references or the drawings
    cannot go into it (heights above the geoid without the grid: :class:`GridMissing`) and when nothing fits the
    georeference again (no control markers, no positions of the photos)."""
    from ..markers import gcp

    changes = survey_crs_changes(chunk, crs)
    if crs == chunk.crs:
        return {"crs": crs, "updated": False, "rebuild": [], "kept": [], "converted": changes["convert"],
                "heights_unchanged": False, "report": None}
    old = chunk.crs
    georeferenced, refit = not crs_mod.is_local(old), changes["refit"]
    sections, fate = _project_sections(chunk, old)
    values, moved = _drawn(chunk, old, crs, sections if fate == "convert" else None) if georeferenced else ({}, None)
    before = (chunk.crs, chunk.transform.copy(),
              [(m, m.reference, None if m.position is None else m.position.copy()) for m in chunk.markers],
              [cam for cam in chunk.cameras if "reference_crs" not in cam.meta],
              {key: chunk.meta[key] for key in (*values, "region") if key in chunk.meta})
    report = None
    try:
        chunk.crs = crs
        if georeferenced:
            _pinned_references(chunk, old)
        if refit:
            report = gcp.update_transform(chunk)
            if report["method"] is None or not ("markers" in report["method"] or "cameras" in report["method"]):
                raise ValueError(f"nothing fits the georeference of the chunk again in {crs}: no control markers and "
                                 "no positions of the photos, the system was not changed")
            if chunk.meta.get("region"):
                values["region"] = _region_follows(chunk.meta["region"], before[1], chunk.transform)
        chunk.meta.update(values)
        if moved is not None:
            _write_sections(_sections_file(chunk), moved)
    except BaseException:
        chunk.crs, chunk.transform = before[0], before[1]
        for m, reference, position in before[2]:
            m.reference, m.position = reference, position
        for cam in before[3]:
            cam.meta.pop("reference_crs", None)
        for key in values:
            if key in before[4]:
                chunk.meta[key] = before[4][key]
            else:
                chunk.meta.pop(key, None)
        raise
    if report is not None:
        chunk.forget_precision()
    return {"crs": crs, "updated": report is not None, "rebuild": changes["rebuild"], "kept": changes["kept"],
            "converted": changes["convert"], "heights_unchanged": changes["heights_unchanged"], "report": report}
