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
# the same place in the window in English, as the command line says it (in_english)
DOWNLOAD_IN_THE_PROGRAM_EN = "GNSS > System and heights > Download the EGM2008 geoid"
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


def above_egm2008(crs: str | None) -> bool:
    """``crs`` has heights above the geoid EGM2008 (its vertical part is ``EPSG:3855``, as :func:`survey_crs` writes
    it): GNSS heights go into it only with the grid of the geoid (:data:`GEOID_GRID`)."""
    if crs_mod.is_local(crs):
        return False
    try:
        system = crs_mod.to_pyproj(crs)
    except Exception:  # noqa: BLE001 - a system PROJ cannot read: whoever uses it says why
        return False
    parts = system.sub_crs_list if system.is_compound else [system]
    return any(part.is_vertical and part.to_epsg() == 3855 for part in parts)


def kinds_differ(src: str | None, dst: str | None) -> bool:
    """A conversion from ``src`` to ``dst`` turns heights above the ellipsoid into heights above a geoid, or back."""
    kinds = {height_kind(src), height_kind(dst)}
    return kinds == {ELLIPSOIDAL, ORTHOMETRIC}


@functools.lru_cache(maxsize=256)
def _vertical_datum(crs: str) -> str | None:
    """The vertical datum of ``crs``, a system with heights above a geoid, as PROJ names it (its EPSG code, else its
    name): what two systems with heights above the same geoid share, whatever their units."""
    system = crs_mod.to_pyproj(crs)
    if system.is_bound:
        system = system.source_crs
    parts = system.sub_crs_list if system.is_compound else [system]
    vertical = next((part for part in parts if part.is_vertical), None)
    if vertical is None or vertical.datum is None:
        return None
    ident = (vertical.datum.to_json_dict().get("id") or {}) if hasattr(vertical.datum, "to_json_dict") else {}
    return f"{ident.get('authority')}:{ident.get('code')}" if ident.get("code") else vertical.datum.name


def geoids_differ(src: str | None, dst: str | None) -> bool:
    """A conversion from ``src`` to ``dst`` takes heights above one geoid into heights above another (EGM96 into
    EGM2008, ITALGEO2005 into EGM2008): through the heights above the ellipsoid, with the grids of both - without them
    PROJ would pass the heights as they are, metres off."""
    if height_kind(src) != ORTHOMETRIC or height_kind(dst) != ORTHOMETRIC:
        return False
    return _vertical_datum(src) != _vertical_datum(dst)


def through_a_geoid(src: str | None, dst: str | None) -> bool:
    """A conversion from ``src`` to ``dst`` needs the grid of a geoid: between heights above the ellipsoid and above a
    geoid (:func:`kinds_differ`), or above two geoids (:func:`geoids_differ`)."""
    return kinds_differ(src, dst) or geoids_differ(src, dst)


def _label(crs: str) -> str:
    """``crs`` as a message names it: its code, or the name of a system given as WKT."""
    if len(crs) <= 40:
        return crs
    try:
        return crs_mod.to_pyproj(crs).name
    except Exception:  # noqa: BLE001 - the text as it is
        return crs[:40] + "..."


def _missing_grids(src: str, dst: str) -> list[tuple[str, str]]:
    """The grids PROJ lacks to convert from ``src`` to ``dst`` (name, the address where PROJ publishes it or ""): those
    of the first of its operations that are no ballpark that needs the EGM2008 grid - that one first -, else those of
    the first of them it cannot use (from one geoid into another, the grids of both). None when it has one it can use:
    the points are where that one does not reach."""
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
    with_egm2008 = next((missing for missing in lacking if any(name == GEOID_GRID for name, _url in missing)), None)
    if with_egm2008 is not None:  # the grid the program downloads first, then the others of that operation
        return sorted(with_egm2008, key=lambda grid: grid[0] != GEOID_GRID)
    return lacking[0] if lacking else []


def refusal(src: str, dst: str) -> GridMissing:
    """Why heights cannot go from ``src`` to ``dst`` (one of the two with heights above the ellipsoid, the other above
    a geoid; or above two geoids), and what to do: the grid of the geoid PROJ needs and how to get it - EGM2008 from
    the program, another one copied into :func:`grid_folder` - or, when it has its grids, that it has no transformation
    where the points are."""
    above = {ELLIPSOIDAL: "the ellipsoid", ORTHOMETRIC: "the geoid"}
    here, there = above[height_kind(src)], above[height_kind(dst)]
    if geoids_differ(src, dst):  # name the two geoids
        here, there = f"the geoid of {_heights_name(src)}", f"the geoid of {_heights_name(dst)}"
    what = f"heights above {here} ({_label(src)}) cannot become heights above {there} ({_label(dst)})"
    grids = _missing_grids(src, dst)
    names = [name for name, _url in grids]
    if GEOID_GRID in names:
        off = "metres off" if geoids_differ(src, dst) else "48 m off at Rome"
        message = (f"{what}: PROJ needs the grid of the geoid {GEOID_GRID} (EGM2008), which is not installed, and "
                   f"without it would give the heights as they are - heights above {here} passed off as heights "
                   f"above {there}, {off}. Download it with {DOWNLOAD_IN_THE_PROGRAM}, or with "
                   f"`{DOWNLOAD_COMMAND}` (81 MB, public domain), into {grid_folder()}")
        others = [(name, url) for name, url in grids if name != GEOID_GRID]
        if others:
            one = len(others) == 1
            published = [url for _name, url in others if url]
            message += (f"; PROJ needs {' and '.join(name for name, _url in others)} too, which "
                        f"{'is' if one else 'are'} not installed: copy {'it' if one else 'them'} there as well"
                        + (f" (PROJ publishes {'it' if one else 'them'} at {' and '.join(published)})"
                           if published else ""))
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


def _heights_name(crs: str) -> str:
    """The name of the heights of ``crs``, a system with heights above a geoid (``EGM2008 height``)."""
    system = crs_mod.to_pyproj(crs)
    if system.is_bound:
        system = system.source_crs
    parts = system.sub_crs_list if system.is_compound else [system]
    return next((part.name for part in parts if part.is_vertical), system.name)


def in_english(text: str) -> str:
    """``text`` - a message of the engine - with the menu path of the window in English (:data:`DOWNLOAD_IN_THE_PROGRAM`
    is the Italian window's), as the command line, in English, says it."""
    return str(text).replace(DOWNLOAD_IN_THE_PROGRAM, DOWNLOAD_IN_THE_PROGRAM_EN)


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


def exif_heights_in_ellipsoidal_systems(chunk) -> list:
    """The photos of ``chunk`` whose positions, switched on, have heights marked as above the sea level (the EXIF,
    ``meta["gnss"]["height"]``) in a system with heights above the ellipsoid (EPSG:4979): every conversion takes them
    as heights above the ellipsoid."""
    return [cam for cam in chunk.cameras if cam.reference.location and cam.reference.enabled
            and (cam.meta.get("gnss") or {}).get("height") == "exif"
            and height_kind(_reference_crs(chunk, cam)) == ELLIPSOIDAL]


def check(chunk) -> list[str]:
    """What does not fit between the heights of the positions of the photos of ``chunk`` and those of its coordinate
    system, a sentence each (nothing for a chunk in a local frame):

    * photos with heights above the ellipsoid (GNSS: an MRK, a PPK, a positions file of such heights) and a system of
      the chunk without heights of its own (2D): the heights of the chunk are above the ellipsoid, some 40-50 m from
      those above the sea level in Italy - with markers on sea-level heights, a system with EGM2008 heights;
    * a system with heights above the geoid whose grid PROJ does not have, for photos with heights above the
      ellipsoid: converting them is refused, with the grid and how to get it;
    * photos with heights above the ellipsoid given in a system without heights of its own (``EPSG:4326``) and a
      system of the chunk with heights above the geoid: their heights go into it as they are;
    * photos with heights marked as above the sea level (EXIF) in a system with heights above the ellipsoid
      (``EPSG:4979``, :func:`exif_heights_in_ellipsoidal_systems`): every conversion takes them as heights above the
      ellipsoid;
    * a system on WGS 84 holding positions in ITRF2020 and references in a European frame together
      (:func:`mixed_frames`): about a metre apart in Europe."""
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
    marked = exif_heights_in_ellipsoidal_systems(chunk)
    if marked:
        systems = ", ".join(sorted({_reference_crs(chunk, cam) for cam in marked}))
        out.append(f"{_photos(len(marked))} heights marked as heights above the sea level (EXIF) in a system whose "
                   f"heights are above the ellipsoid ({systems}): every conversion takes them as heights above the "
                   "ellipsoid, some 40-50 m off in Italy. Give them in a system without heights of its own (EPSG:4326 "
                   "for longitude, latitude and height) or mark them as heights above the ellipsoid")
    mixed = mixed_frames(chunk)
    if mixed is not None:
        out.append(mixed_frames_text(mixed))
    return out


MIXED_FRAMES = "a system on WGS 84"  # in the sentence of mixed_frames_text, which the window says in its own words


def mixed_frames(chunk) -> dict | None:
    """Positions in ITRF2020 and references in a European frame together in a chunk whose system is on WGS 84: the
    photos placed against a permanent station, by a baseline or PPP (ITRF2020) go into it as they are - ITRF2020 is WGS
    84 for the program -, those in ETRF2000 (a base typed in RDN2008), ETRF2014 or ETRF2020 by PROJ through the ETRS89
    ensemble, about a metre apart in Europe in 2026; markers read from a file in a European system count too
    (``meta["marker_frames"]``, :func:`..markers.gcp.import_reference`). In a system of ETRF2000 both arrive exactly.
    ``{"crs", "itrf2020": photos, "photos": {frame: photos}, "markers": {frame: markers}, "proposal"}`` - the proposal
    the system of ETRF2000 where the survey is (:func:`survey_crs`), None when the place cannot be told -, or None."""
    from collections import Counter

    from . import frames

    if crs_mod.is_local(chunk.crs) or frames.frame_of(chunk.crs) != frames.WGS84:
        return None
    placed = [cam for cam in chunk.cameras if cam.reference.location and cam.reference.enabled]
    kinds = [frames.position_frame(cam.meta.get("gnss")) for cam in placed]
    itrf = sum(1 for frame in kinds if frame in frames.DYNAMIC)
    photos = Counter(frame for frame in kinds if frame in frames.EUROPEAN)
    recorded = chunk.meta.get("marker_frames") or {}
    markers = Counter(recorded[m.label] for m in chunk.markers
                      if m.reference.location and recorded.get(m.label) in frames.EUROPEAN)
    if not itrf or not (photos or markers):
        return None
    try:
        place = survey_place(chunk)
        proposal = survey_crs(place[0], place[1], "ETRF2000", geoid=False) if place else None
    except ValueError:  # the place cannot be told: no proposal
        proposal = None
    return {"crs": chunk.crs, "itrf2020": itrf, "photos": dict(photos), "markers": dict(markers),
            "proposal": proposal}


def mixed_frames_text(mixed: dict) -> str:
    """The sentence of :func:`check` for ``mixed`` (:func:`mixed_frames`)."""
    european = [f"{n} photo{'s' if n != 1 else ''} in {frame}" for frame, n in sorted(mixed["photos"].items())]
    european += [f"{n} marker{'s' if n != 1 else ''} read in {frame}" for frame, n in sorted(mixed["markers"].items())]
    advice = (f" ({mixed['proposal']} here: Sistema del rilievo, `ppg gnss crs --survey`)" if mixed["proposal"] else
              " (Sistema del rilievo, `ppg gnss crs --survey`)")
    return (f"{_photos(mixed['itrf2020'])} positions in ITRF2020 (a permanent station, a baseline, PPP) and "
            f"{', '.join(european)} of a European frame, together in {mixed['crs']}, {MIXED_FRAMES}: the ITRF2020 "
            "positions go into it as they are, the European ones through ETRS89, about a metre apart in Europe, and "
            "positions in ITRF2020 of different years drift 2-3 cm a year apart. Put the chunk into a system of "
            f"ETRF2000{advice}, where both arrive exactly")


# --- the coordinate system of a survey -------------------------------------------------------------------------------

# Italy, coarsely (longitude, latitude; a few kilometres out to sea, less in the strait of Messina, which the mainland
# and Sicily share along its middle): the mainland with its islands near the coast, Elba and the Tuscan islands, Ponza
# and Ventotene, Capri and Ischia, the Tremiti; Sicily with the Aeolian islands, the Egadi and Ustica; Sardinia;
# Pantelleria; the Pelagie. Corsica, Malta, Nice, Ticino, Istria are out.
ITALY = (
    ((7.53, 43.78), (7.67, 44.18), (6.95, 44.32), (6.85, 44.53), (7.05, 44.7), (6.62, 45.1), (7.13, 45.25),
     (6.8, 45.65), (6.8, 45.83), (7.05, 45.93), (7.66, 45.98), (7.87, 45.92), (8.1, 46.25), (8.44, 46.46),
     (8.6, 46.1), (8.85, 45.95), (9.02, 45.82), (9.25, 46.2), (9.33, 46.5), (9.55, 46.3), (10.15, 46.22),
     (10.05, 46.45), (10.15, 46.63), (10.47, 46.62), (10.49, 46.85), (11.1, 46.95), (11.52, 47.02), (12.18, 47.09),
     (12.7, 46.65), (13.72, 46.52), (13.38, 46.32), (13.55, 46.05), (13.65, 45.9), (13.92, 45.68), (13.77, 45.58),
     (13.4, 45.66), (13.1, 45.6), (12.45, 45.3), (12.6, 44.95), (12.4, 44.4), (12.75, 44.0), (13.75, 43.55),
     (14.4, 42.4), (15.05, 42.05), (15.6, 42.2), (16.3, 41.9), (17.0, 41.15), (18.05, 40.65), (18.6, 40.15),
     (18.4, 39.72), (17.9, 40.05), (17.1, 40.45),
     # the gulf of Taranto, Cirò and Punta Alice, Capo Colonna and Capo Rizzuto, Punta Stilo, Capo Spartivento
     (16.75, 40.12), (16.68, 39.95), (16.58, 39.8), (16.7, 39.68), (16.97, 39.55), (17.2, 39.42), (17.2, 39.25),
     (17.27, 39.02), (17.14, 38.85), (16.64, 38.44), (16.52, 38.31), (16.3, 38.2), (16.19, 38.03), (16.08, 37.89),
     (15.78, 37.88), (15.66, 37.92),
     # the strait of Messina, along its middle
     (15.59, 37.95), (15.605, 38.17), (15.625, 38.22), (15.665, 38.258), (15.72, 38.3),
     # Capo Vaticano, the Tyrrhenian coast of Calabria, the gulf of Policastro, the Cilento
     (15.7, 38.4), (15.78, 38.62), (15.85, 38.72), (15.98, 39.36), (15.88, 39.52), (15.79, 39.63), (15.76, 39.7),
     (15.73, 39.85), (15.66, 39.98), (15.37, 39.955), (15.24, 39.99), (15.12, 40.11), (15.0, 40.15), (14.86, 40.24),
     (14.93, 40.35), (14.8, 40.55), (14.15, 40.48), (13.8, 40.65), (13.45, 40.72), (12.8, 40.85), (12.55, 41.4),
     (12.2, 41.7), (11.7, 42.05), (11.15, 42.2), (10.25, 42.15), (9.98, 42.55), (9.95, 42.95), (9.76, 42.98),
     (9.76, 43.12), (9.85, 43.48), (10.2, 43.7), (9.8, 44.05), (9.0, 44.35), (8.4, 44.2), (8.0, 43.82)),
    ((15.665, 38.275), (15.35, 38.9), (14.3, 38.85), (13.05, 38.8), (12.35, 38.15), (11.95, 38.0), (12.3, 37.6),
     (13.05, 37.4), (14.2, 36.95), (14.45, 36.74), (14.85, 36.66), (15.05, 36.55), (15.4, 37.0), (15.3, 37.8),
     (15.575, 37.95), (15.585, 38.17), (15.61, 38.22), (15.655, 38.255)),
    ((8.1, 41.15), (9.2, 41.32), (9.55, 41.3), (9.85, 40.95), (9.9, 40.4), (9.75, 39.9), (9.7, 39.1), (9.05, 39.0),
     (8.55, 38.8), (8.25, 38.9), (8.15, 39.2), (8.3, 39.6), (8.3, 40.1), (8.05, 40.6)),
    ((11.88, 36.7), (12.1, 36.7), (12.1, 36.88), (11.88, 36.88)),
    ((12.25, 35.45), (12.95, 35.45), (12.95, 35.92), (12.25, 35.92)),
)
# Europe where ETRS89 is the datum (the area of use of ETRS89 in the EPSG, coarsely: tens of kilometres along the land
# borders, the coasts with their sea; longitude, latitude): the European Union without Cyprus, the EFTA, the United
# Kingdom, the Balkans, Moldova; not Turkey, Ukraine, Belarus, Russia (Kaliningrad is a hole), Iceland, the Canary
# Islands, Madeira, the Azores. There ITRF2020 converts exactly into the systems of ETRF2000 (the Helmert of the EPSG at
# the epoch of each position), which move with the plate: the survey proposes them (survey_crs). Italy is in_italy.
EUROPE = (
    (  # the mainland with the British Isles, the islands of the Mediterranean and the Baltic, Scandinavia
     # the Mediterranean, west to east: north of Morocco, Algeria, Tunisia and Libya, south of Malta and Crete
     (-5.75, 35.96), (-5.35, 36.02), (-4.5, 36.1), (-2.0, 36.3), (0.0, 37.3), (3.0, 37.6), (8.0, 38.1),
     (9.6, 37.95), (11.8, 37.55), (12.1, 36.3), (14.0, 35.45), (15.5, 35.3), (20.0, 35.4), (23.5, 34.6),
     (26.5, 34.85), (27.5, 35.2), (28.5, 35.85),
     # the Aegean between the Greek islands and Turkey: Rhodes, Symi, Kos, Samos, Chios, Lesbos, Lemnos, Samothrace
     (28.45, 36.3), (28.3, 36.55), (28.0, 36.6), (27.85, 36.665), (27.35, 36.64), (27.3, 36.93), (27.18, 36.98), (27.12, 37.35),
     (27.05, 37.69), (26.9, 37.85), (26.22, 38.15), (26.21, 38.55), (26.35, 38.75), (26.6, 38.85), (26.72, 39.05),
     (26.65, 39.33), (26.52, 39.42), (26.07, 39.43), (25.75, 39.8), (25.55, 40.1), (25.85, 40.32), (26.04, 40.65),
     # Greece and Bulgaria against Turkey, the Black Sea of Bulgaria and Romania, the Danube against Ukraine
     (26.34, 40.94), (26.61, 41.33), (26.52, 41.63), (26.36, 41.71), (26.57, 41.92), (27.0, 42.08), (27.9, 42.0),
     (28.03, 41.98), (29.5, 42.1), (30.8, 43.4), (31.2, 44.4), (30.5, 45.05), (29.75, 45.25), (29.62, 45.38),
     (29.27, 45.43), (28.85, 45.32), (28.2, 45.47),
     # Moldova, Romania, Hungary, Slovakia and Poland against Ukraine; Poland and Lithuania against Belarus
     (28.5, 45.75), (28.95, 46.05), (29.6, 46.4), (30.13, 46.42), (29.9, 46.8), (29.65, 47.35), (29.2, 47.9),
     (28.8, 48.1), (28.1, 48.25), (27.6, 48.48), (27.2, 48.38), (26.62, 48.26), (26.15, 47.98), (25.5, 47.88),
     (24.9, 47.73), (24.0, 47.95), (23.5, 48.0), (22.9, 47.95), (22.6, 48.12), (22.15, 48.41), (22.4, 48.8),
     (22.56, 49.08), (22.68, 49.4), (22.95, 49.8), (23.5, 50.25), (24.1, 50.85), (23.6, 51.53), (23.65, 52.05),
     (23.9, 52.7), (23.9, 53.15), (23.5, 53.94), (24.4, 53.95), (25.0, 54.15), (25.55, 54.3), (25.75, 54.55),
     (26.3, 55.0), (26.65, 55.2), (26.63, 55.68),
     # Latvia against Belarus and Russia, Estonia against Russia (lake Peipus, the Narva), the gulf of Finland
     (27.6, 55.8), (28.17, 56.15), (28.24, 56.42), (27.9, 56.85), (27.75, 57.3), (27.35, 57.52), (27.5, 57.75), (27.75, 57.9),
     (27.5, 58.05), (27.55, 58.4), (27.7, 58.9), (28.21, 59.36), (28.05, 59.47), (27.8, 60.55),
     # Finland and Norway against Russia
     (29.3, 61.2), (31.3, 62.4), (31.58, 62.9), (30.0, 64.0), (29.9, 65.2), (29.1, 66.8), (29.9, 67.6),
     (28.4, 68.5), (28.93, 69.05), (29.2, 69.3), (30.15, 69.62), (30.85, 69.78),
     # the Arctic coast of Norway, its Atlantic coast, the Shetland, the Orkney and the Hebrides, Ireland, Cornwall,
     # Brittany, the Bay of Biscay, Galicia and Portugal
     (31.6, 70.35), (31.0, 70.7), (28.5, 71.35), (25.8, 71.45), (22.0, 71.0), (19.5, 70.5), (16.0, 69.6),
     (13.5, 68.6), (11.6, 67.4), (11.0, 66.0), (9.5, 64.6), (7.5, 63.6), (5.0, 62.4), (0.5, 61.1), (-1.8, 61.0),
     (-3.5, 59.6), (-7.5, 59.3), (-9.0, 57.8), (-11.0, 55.6), (-11.0, 51.2), (-6.8, 49.8), (-6.0, 49.6),
     (-6.0, 47.8), (-10.0, 43.8), (-10.0, 37.0), (-9.5, 36.6), (-6.8, 36.1)),
    ((-8.0, 61.3), (-6.0, 61.3), (-6.0, 62.5), (-8.0, 62.5)),  # the Faroe Islands
    ((10.0, 74.0), (34.0, 74.0), (34.0, 81.0), (10.0, 81.0)),  # Svalbard and Bear Island
    ((-9.2, 70.8), (-7.8, 70.8), (-7.8, 71.2), (-9.2, 71.2)),  # Jan Mayen
)
NOT_EUROPE = (((19.5, 54.43), (22.8, 54.36), (22.95, 54.75), (21.4, 55.27), (20.95, 55.27), (19.6, 54.95)),)  # Kaliningrad
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


@functools.lru_cache(maxsize=1)
def _europe():
    import shapely

    land = shapely.MultiPolygon([shapely.Polygon(ring) for ring in EUROPE])
    return land.difference(shapely.MultiPolygon([shapely.Polygon(ring) for ring in NOT_EUROPE]))


def in_europe(lon: float, lat: float) -> bool:
    """Whether ``lon``, ``lat`` (degrees) are where ETRS89 is the datum in Europe, by a coarse outline
    (:data:`EUROPE`), Italy included (:func:`in_italy`)."""
    import shapely

    return in_italy(lon, lat) or bool(_europe().covers(shapely.Point(float(lon), float(lat))))


@functools.lru_cache(maxsize=64)
def _known(crs: str) -> bool:
    from pyproj.exceptions import CRSError

    try:
        crs_mod.to_pyproj(crs)
    except CRSError:
        return False
    return True


def survey_crs(lon: float, lat: float, frame: str | None = None, geoid: bool = True) -> str:
    """The coordinate system for a survey at ``lon``, ``lat`` (degrees). Without ``frame``, as the place decides: in
    Europe (:func:`in_europe`) a system of ETRF2000 - RDN2008 / UTM in Italy (:func:`in_italy`; zones 32, 33 and 34,
    ``EPSG:7791``, ``7792``, ``7793``, the national datum), ETRS89 / UTM elsewhere (``EPSG:258zz``) -, where positions
    in ITRF2020 (a permanent station, a baseline, PPP) arrive exactly at their epoch, as those in the European frames do
    (a base typed in RDN2008, markers of a network RTK), and nothing drifts with the plate between flights; beyond
    Europe the UTM zone on WGS 84 (``EPSG:326zz``, ``EPSG:327zz`` in the south).

    A ``frame`` (one of :data:`.frames.FRAMES`, or ``"WGS84"``) chooses instead, as the frame of the positions would:
    WGS 84 / UTM for WGS 84, a frame nobody knows and ITRF2020 (WGS 84 for the program); for the European frames
    (ETRF2000, the frame of RDN2008 and ETRS89, ETRF2014, ETRF2020) RDN2008 / UTM in Italy, ETRS89 / UTM where its zone
    exists, else WGS 84 / UTM. With ``geoid`` its heights are above EGM2008 (``+3855``), which needs the grid of the
    geoid (:func:`download_geoid`); without, it has no heights of its own. A ``ValueError`` for a frame the program does
    not know and for a place off the earth."""
    from . import frames

    lon, lat = _lonlat(lat, lon)
    if frame is not None:
        frames.crs_of(frame)  # a frame the program knows
    zone = int((lon + 180.0) // 6.0) + 1
    system = None
    european = in_europe(lon, lat) if frame is None else frame in frames.EUROPEAN
    if european:
        if zone in RDN2008_UTM and in_italy(lon, lat):
            system = RDN2008_UTM[zone]
        elif lat >= 0.0 and _known(f"EPSG:{25800 + zone}"):
            system = f"EPSG:{25800 + zone}"
    system = system or crs_mod.utm_for_lonlat(lon, lat)
    return f"{system}+3855" if geoid else system


def survey_place(chunk) -> tuple[float, float, str] | None:
    """Where the survey of ``chunk`` is, and in which frame its positions are, for :func:`survey_crs`: the longitude and
    latitude (degrees) of the median of the positions of its photos that are switched on - of its markers when no photo
    has one - and the frame of most of those photo positions (:func:`.frames.position_frame`, ``"WGS84"`` for those
    whose frame nobody knows), or for the markers the frame of the system of the chunk. None when nothing of the chunk
    has a position on the earth (a local frame)."""
    from collections import Counter

    from ..markers import gcp
    from . import frames

    cams = [cam for cam in chunk.cameras if cam.reference.location and cam.reference.enabled]
    found, _crs = gcp.camera_references(chunk, cams, "EPSG:4326") if cams else ({}, None)
    placed = [cam for cam in cams if cam.key in found]
    if placed:
        lonlat = np.array([found[cam.key][:2] for cam in placed], float)
        frame = Counter(frames.position_frame(cam.meta.get("gnss")) for cam in placed).most_common(1)[0][0]
    else:
        marked = [m for m in chunk.markers if m.reference.location]
        if not marked or crs_mod.is_local(chunk.crs):
            return None
        lonlat = crs_mod.transform_points(np.array([m.reference.location for m in marked], float), chunk.crs,
                                          "EPSG:4326", legacy=frames.Legacy(gcp.MARKERS_READ, chunk))[:, :2]
        frame = frames.frame_of(chunk.crs) or frames.WGS84
    lonlat = lonlat[np.isfinite(lonlat).all(axis=1)]
    if not len(lonlat):
        return None
    lon, lat = (float(v) for v in np.median(lonlat, axis=0))
    return lon, lat, frames.WGS84 if frame == frames.UNKNOWN else frame


def _survey_system(crs: str) -> None:
    """A ``ValueError`` unless ``crs`` is a system with a projected plan in metres (alone, or the horizontal part of a
    compound one) and heights, when it has them, in metres: the products of a survey need metric axes, and so do the
    limits of :func:`set_survey_crs`."""
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
    metres = {"metre", "meter"}
    units = {axis.unit_name for axis in plan.axis_info[:2]}
    if not units or not units <= metres:
        raise ValueError(f"{crs} has its coordinates in {', '.join(sorted(units)) or 'units PROJ does not name'}: a "
                         "survey needs a projected coordinate system in metres (the UTM zone; survey_crs proposes one)")
    upright = ([axis for part in system.sub_crs_list[1:] for axis in part.axis_info] if system.is_compound
               else plan.axis_info[2:])
    units = {axis.unit_name for axis in upright}
    if not units <= metres:
        raise ValueError(f"{crs} has its heights in {', '.join(sorted(units))}: a survey needs heights in metres "
                         "(EGM2008, EPSG:3855, as survey_crs proposes it)")


# what was drawn on the products of a chunk, in the coordinates of its system: it goes with the georeference

SIMILARITY_RMS = 0.005  # metres: how far the similarity that converts a georeference may miss (set_survey_crs)
MIN_EXTENT = 1.0  # metres: the least side of the box that similarity is fitted on
GROUND_CELLS = 1024  # the DEM read for the ground under what was drawn on the plan: at most so many cells a side
GEOREFERENCE = "the points over the extent of the chunk"  # converted for that similarity, as the warning names them
# the keys of chunk.meta set_survey_crs writes: what was drawn, the region
MOVED = ("drawings", "stones", "orthomosaic", "texture", "colour_chart", "region")


def _finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def plan(xy, old: str, new: str, chunk) -> np.ndarray:
    """``(N, 2)`` plan coordinates from ``old`` to ``new``: through the horizontal parts of the two systems, no height
    needed - what was drawn on a chunk with nothing to follow, and the units the window loaded from PyArchInit (in
    the system of its layer: their coordinates, not the model)."""
    from . import frames

    xy = np.asarray(xy, float).reshape(-1, 2)
    if not len(xy):
        return xy.copy()
    pts = np.column_stack([xy, np.zeros(len(xy))])
    return crs_mod.transform_points(pts, crs_mod.horizontal(old), crs_mod.horizontal(new),
                                    legacy=frames.Legacy(DRAWN, chunk))[:, :2]


def _same_plan(raster_crs, system: str) -> bool:
    """Whether a raster whose system is ``raster_crs`` (rasterio's; None when it says none) lies on the plan of
    ``system``: a DEM left in a system the chunk had before, and not built again, does not."""
    if raster_crs is None:
        return True
    try:
        theirs, ours = (part.sub_crs_list[0] if part.is_compound else part
                        for part in (crs_mod.to_pyproj(raster_crs.to_wkt()), crs_mod.to_pyproj(system)))
        codes = theirs.to_epsg(), ours.to_epsg()
        if None not in codes:
            return codes[0] == codes[1]
        return theirs.equals(ours, ignore_axis_order=True)
    except Exception:  # noqa: BLE001 - a system PROJ cannot compare: taken as the chunk's, as the program writes it
        return True


class _Ground:
    """The height of the ground under points of the plan of a chunk, in its old system: its DEM there - read once, at
    most :data:`GROUND_CELLS` cells a side, which is plenty: the height of such a point moves its plan only by the tilt
    the new georeference gives the model, micro-radians as a rule - else ``level()``, the median height of its
    extent."""

    def __init__(self, path: Path | None, old: str, level):
        self.path, self.old, self.level, self._grid = path, old, level, None

    def at(self, xy: np.ndarray) -> np.ndarray:
        z = np.full(len(xy), np.nan)
        if self._grid is None:
            self._grid = self._read() or ()
        if self._grid:
            values, to_cells = self._grid
            cols, rows = (np.floor(v) for v in to_cells * (xy[:, 0], xy[:, 1]))
            inside = (cols >= 0) & (rows >= 0) & (cols < values.shape[1]) & (rows < values.shape[0])
            z[inside] = values[rows[inside].astype(int), cols[inside].astype(int)]
        missing = ~np.isfinite(z)
        if missing.any():
            z[missing] = self.level()
        return z

    def _read(self):
        """The heights of the DEM, decimated, and the affine from the plan to their cells; None without a DEM."""
        if self.path is None or not self.path.exists():
            return None
        try:
            import rasterio
            from affine import Affine
            from rasterio.enums import Resampling

            with rasterio.open(self.path) as src:
                if not _same_plan(src.crs, self.old):
                    return None
                step = max(1, math.ceil(max(src.width, src.height) / GROUND_CELLS))
                shape = (math.ceil(src.height / step), math.ceil(src.width / step))
                values = src.read(1, out_shape=shape, resampling=Resampling.nearest, masked=True)
                values = np.ma.filled(values.astype(float), np.nan)
                transform = src.transform * Affine.scale(src.width / shape[1], src.height / shape[0])
        except Exception:  # noqa: BLE001 - a DEM that cannot be read gives no height: the level does
            return None
        return values, ~transform


class _Follow:
    """What was drawn on a chunk carried by the change of its georeference, ``matrix`` - from the old world to the new
    one: the new georeference after the old one undone, a similarity -, so that it stays on the model whatever the
    references of the new georeference are (their kinds of height, frames and epochs): a point with a height as it is,
    a point of the plan at the height of the ground there (:class:`_Ground`). What it carried is kept for
    :func:`_said`."""

    mode = "follow"

    def __init__(self, matrix: np.ndarray, ground: _Ground):
        self.matrix, self.ground, self.before, self.after = np.asarray(matrix, float), ground, [], []

    def tall(self, xyz) -> np.ndarray:
        xyz, m = np.asarray(xyz, float).reshape(-1, 3), self.matrix
        # element by element, not a product of matrices: BLAS may give the same point another last bit in another row,
        # and the first and the last point of a ring of a polygon must stay the same
        out = xyz[:, :1] * m[:3, 0] + xyz[:, 1:2] * m[:3, 1] + xyz[:, 2:3] * m[:3, 2] + m[:3, 3]
        self.before.append(xyz)
        self.after.append(out)
        return out

    def flat(self, xy) -> np.ndarray:
        xy = np.asarray(xy, float).reshape(-1, 2)
        if not len(xy):
            return xy.copy()
        return self.tall(np.column_stack([xy, self.ground.at(xy)]))[:, :2]


class _Convert:
    """What was drawn on a chunk whose georeference places nothing - nothing lives in its internal frame, no model to
    stay on - converted as coordinates, as its markers are (:func:`..core.crs.transform_points`: the heights by the rule
    of every conversion, from a system without heights of its own as they are; the plan of what has none through the
    horizontal parts of the two systems). ``heights``: some had heights."""

    mode = "convert"

    def __init__(self, chunk, old: str, new: str):
        self.chunk, self.old, self.new, self.heights = chunk, old, new, False

    def tall(self, xyz) -> np.ndarray:
        from . import frames

        xyz = np.asarray(xyz, float).reshape(-1, 3)
        if not len(xyz):
            return xyz.copy()
        self.heights = True
        return crs_mod.transform_points(xyz, self.old, self.new, legacy=frames.Legacy(DRAWN, self.chunk))

    def flat(self, xy) -> np.ndarray:
        return plan(xy, self.old, self.new, self.chunk)


def _wkt(text: str, mover) -> str:
    """A geometry (WKT) carried by ``mover``: one with heights (``Z``) with them, another as a drawing of the plan."""
    import shapely

    shape = shapely.from_wkt(text)
    if shape.has_z:
        return shapely.transform(shape, mover.tall, include_z=True).wkt
    return shapely.transform(shape, mover.flat).wkt


def _move_rows(rows: list[dict], height: str, mover) -> None:
    """Records with ``x``, ``y`` and the height ``height`` carried by ``mover``, in place; a record without a height
    as a point of the plan."""
    tall = [r for r in rows if _finite(r.get(height))]
    if tall:
        xyz = mover.tall([[float(r["x"]), float(r["y"]), float(r[height])] for r in tall])
        for r, (x, y, z) in zip(tall, xyz.tolist()):
            r["x"], r["y"], r[height] = x, y, z
    flat = [r for r in rows if not _finite(r.get(height))]
    if flat:
        for r, (x, y) in zip(flat, mover.flat([[float(r["x"]), float(r["y"])] for r in flat]).tolist()):
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


def _counts(chunk, georeferenced: bool, moves: bool, sections: list | None, fate: str) -> dict:
    """How many of each kind of :data:`CONVERTED` :func:`set_survey_crs` converts (``moves``: the georeference is
    fitted again or converted, and the region goes with it)."""
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
    counts["region"] = int(moves and bool(meta.get("region")))
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


def _drawn(chunk, mover, sections: list | None) -> tuple[dict, list | None]:
    """What was drawn on the products of ``chunk``, carried by ``mover`` aside (nothing of the chunk changes; a
    conversion that cannot be made raises here): the new values of the keys of ``chunk.meta`` and the new sections of
    the project (None to leave them)."""
    import copy

    meta, values = chunk.meta, {}
    if meta.get("drawings"):
        drawings = copy.deepcopy(meta["drawings"])
        for unit in _units(drawings):
            unit["wkt"] = _wkt(unit["wkt"], mover)
        _move_rows(_rows(drawings, "quote"), "quota_q", mover)
        _move_rows(_rows(drawings, "points"), "z", mover)
        values["drawings"] = drawings
    ortho = _ortho_name(chunk)
    if (meta.get("stones") or {}).get(ortho):
        stones = copy.deepcopy(meta["stones"])
        stones[ortho] = [_wkt(w, mover) for w in stones[ortho]]
        values["stones"] = stones
    for key, lists in (("orthomosaic", ("patches", "applied_patches")), ("texture", ("patches",))):
        if any((meta.get(key) or {}).get(name) for name in lists):
            record = copy.deepcopy(meta[key])
            for name in lists:
                for patch in record.get(name) or []:
                    if patch.get("polygon"):
                        patch["polygon"] = mover.flat(np.asarray(patch["polygon"], float)[:, :2]).tolist()
            values[key] = record
    if (meta.get("colour_chart") or {}).get("bounds"):
        chart = copy.deepcopy(meta["colour_chart"])
        x0, y0, x1, y1 = (float(v) for v in chart["bounds"])
        corners = mover.flat([[x0, y0], [x1, y0], [x0, y1], [x1, y1]])
        chart["bounds"] = [float(v) for v in (*corners.min(axis=0), *corners.max(axis=0))]
        values["colour_chart"] = chart
    moved = None
    if sections:
        moved = copy.deepcopy(sections)
        for s in moved:
            ends = mover.flat([list(s["p1"])[:2], list(s["p2"])[:2]])
            s["p1"], s["p2"] = ends[0].tolist(), ends[1].tolist()
            s["length"] = float(np.hypot(*(ends[1] - ends[0])))
    return values, moved


def _said(mover, old: str, new: str) -> tuple[float | None, list[float] | None]:
    """What happened to what ``mover`` carried along the georeference (:class:`_Follow`): how far in plan it is now from
    where the conversion of its coordinates from ``old`` to ``new`` would put it (metres, the most; not said from or
    into a system of ITRF2020, whose conversion of points without an epoch is itself an approximation) and how much its
    heights changed (metres, the least and the most); None, None for what was converted, and for nothing carried."""
    from . import frames

    if mover.mode != "follow" or not mover.before:
        return None, None
    before, after = np.vstack(mover.before), np.vstack(mover.after)
    keep = np.isfinite(before).all(axis=1) & np.isfinite(after).all(axis=1)
    if not keep.any():
        return None, None
    before, after = before[keep], after[keep]
    lift = after[:, 2] - before[:, 2]
    shift = None
    if not (frames.dynamic(old) or frames.dynamic(new)):
        try:
            plan = crs_mod.transform_points(np.column_stack([before[:, :2], np.zeros(len(before))]),
                                            crs_mod.horizontal(old), crs_mod.horizontal(new))[:, :2]
        except ValueError:
            plan = None
        if plan is not None and np.isfinite(plan).all():
            shift = float(np.max(np.hypot(after[:, 0] - plan[:, 0], after[:, 1] - plan[:, 1])))
    return shift, [float(lift.min()), float(lift.max())]


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


KEEP = 98.0  # percent of the points of a cloud, along each axis, that mark the extent of a chunk: not a stray point
_BOXES: dict = {}  # (file, size, time) -> its box: the change takes the box its preview read


def _file_box(path: Path) -> tuple[np.ndarray, np.ndarray] | None:
    """The box of the middle :data:`KEEP` % of the points of a cloud, or of the vertices of a model, along each axis
    (internal coordinates: a stray point does not decide it), from at most a million of them read through the file
    (:meth:`..core.pointcloud.PointCloud.sample_xyz`); kept for the file as it is, so that the change takes the box its
    preview read; None when the file cannot be read."""
    from ..core.pointcloud import PointCloud

    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    box = _BOXES.get(key)
    if box is None and key not in _BOXES:
        try:
            xyz = np.asarray(PointCloud.sample_xyz(path), float).reshape(-1, 3)
        except Exception:  # noqa: BLE001 - a file that cannot be read marks no extent
            xyz = np.empty((0, 3))
        xyz = xyz[np.isfinite(xyz).all(axis=1)]
        cut = (100.0 - KEEP) / 2
        box = (np.percentile(xyz, cut, axis=0), np.percentile(xyz, 100.0 - cut, axis=0)) if len(xyz) else None
        if len(_BOXES) >= 32:
            _BOXES.clear()
        _BOXES[key] = box
    return box


def _internal_box(chunk) -> tuple[np.ndarray, np.ndarray] | None:
    """The box (internal coordinates) of what lives in the internal frame of ``chunk``: its model, its dense cloud and
    its tie points (:func:`_file_box`); without them its aligned cameras and its placed markers - not with them: the
    cameras fly above the ground. None when nothing lives there."""
    boxes = []
    for name in ("model", "point_cloud", "tie_points"):
        path = chunk.product_path(name)
        box = _file_box(path) if path is not None and path.exists() else None
        if box is not None:
            boxes.append(box)
    if not boxes:
        placed = ([cam.center for cam in chunk.aligned_cameras]
                  + [m.position for m in chunk.markers if m.position is not None])
        if placed:
            xyz = np.asarray(placed, float).reshape(-1, 3)
            boxes.append((xyz.min(axis=0), xyz.max(axis=0)))
    if not boxes:
        return None
    return np.min([lo for lo, _hi in boxes], axis=0), np.max([hi for _lo, hi in boxes], axis=0)


_GRID = np.array([[i, j, k] for i in (-0.5, 0.0, 0.5) for j in (-0.5, 0.0, 0.5) for k in (-0.5, 0.0, 0.5)])


def _extent_points(chunk, transform: np.ndarray | None = None) -> np.ndarray | None:
    """Points spread over the extent of ``chunk`` in the coordinates of its system: a grid of 3 x 3 x 3 over its region,
    else over the box of what lives in its internal frame, taken through ``transform`` (its georeference unless given;
    sides of at least :data:`MIN_EXTENT`); None when nothing lives there."""
    from ..core import geometry
    from ..engine.region import get_region

    region = get_region(chunk)
    if region is not None:
        return region.center + (_GRID * np.maximum(region.size, MIN_EXTENT)) @ region.rotation.T
    box = _internal_box(chunk)
    if box is None:
        return None
    transform = chunk.transform if transform is None else np.asarray(transform, float)
    lo, hi = box
    least = MIN_EXTENT / max(geometry.decompose_similarity(transform)[0], 1e-12)  # in the units of the internal frame
    return geometry.apply(transform, (lo + hi) / 2 + _GRID * np.maximum(hi - lo, least))


def _level(points: np.ndarray | None, transform: np.ndarray) -> float:
    """The height a drawing of the plan takes where no DEM says one: the median height of the extent of its chunk
    (``points``, :func:`_extent_points`), else that of the origin of its internal frame (``transform``)."""
    return float(np.median(points[:, 2])) if points is not None else float(np.asarray(transform)[2, 3])


def _similarity(chunk, old: str, new: str, points: np.ndarray | None) -> dict | None:
    """The georeference of ``chunk`` from ``old`` into ``new`` when nothing fits it again there: a similarity (with
    scale) fitted from the plan of ``points`` (:func:`_extent_points`), laid on their median height, to its conversion.
    ``{"matrix", "rms": how far it misses that plan (metres), "relief": the height of the extent, "vertical": how much,
    over that relief, the heights it gives part from those of the conversion, peak to peak - the change of the scale of
    the plan between the two systems, which every georeference in the new system carries over the heights: the relief
    of the model changes by as much, and the largest gap at one point is half of it, the fit being centred}``; None
    when nothing lives in the internal frame of the chunk (its georeference places nothing). A ``ValueError`` when it
    misses the plan by more than :data:`SIMILARITY_RMS` - a site too large for one similarity between the two systems
    -, and the refusal of a conversion that cannot be made."""
    from ..core import geometry
    from . import frames

    if points is None:
        return None
    legacy = frames.Legacy(GEOREFERENCE, chunk)
    level = points.copy()
    level[:, 2] = float(np.median(points[:, 2]))  # the plan of the extent, on one height
    moved = crs_mod.transform_points(level, old, new, legacy=legacy)
    fit = geometry.umeyama(level, moved, with_scale=True)
    rms = float(np.sqrt(np.mean(np.sum((geometry.apply(fit, level) - moved)[:, :2] ** 2, axis=1))))
    if not math.isfinite(rms) or rms > SIMILARITY_RMS:
        span = float(np.linalg.norm(np.ptp(points[:, :2], axis=0)))
        raise ValueError(f"{_label(new)} cannot take the chunk: no one similarity converts its georeference from "
                         f"{_label(old)} over a site of {span:.0f} m across (what lives in its internal frame - the "
                         f"model, the clouds - and its region): in plan it misses by {rms * 1000:.1f} mm, more than "
                         f"{SIMILARITY_RMS * 1000:.0f} mm; nothing was changed")
    tall = crs_mod.transform_points(points, old, new, legacy=legacy)
    vertical = float(np.ptp(geometry.apply(fit, points)[:, 2] - tall[:, 2]))
    return {"matrix": fit, "rms": rms, "relief": float(np.ptp(points[:, 2])), "vertical": vertical}


def _snapshot(chunk) -> tuple:
    """What :func:`_put` and :func:`set_survey_crs` change on ``chunk``, for :func:`_restore`: its system and
    georeference, the references and positions of its markers, the photos without a system of their own, the keys of
    :data:`MOVED`."""
    return (chunk.crs, chunk.transform, chunk.transform.copy(), [(m, m.reference, m.position) for m in chunk.markers],
            [cam for cam in chunk.cameras if "reference_crs" not in cam.meta],
            {key: chunk.meta[key] for key in MOVED if key in chunk.meta})


def _restore(chunk, state: tuple) -> None:
    """``chunk`` as :func:`_snapshot` found it."""
    crs, transform, values, markers, unpinned, meta = state
    transform[...] = values
    chunk.crs, chunk.transform = crs, transform
    for m, reference, position in markers:
        m.reference, m.position = reference, position
    for cam in unpinned:
        cam.meta.pop("reference_crs", None)
    for key in MOVED:
        if key in meta:
            chunk.meta[key] = meta[key]
        else:
            chunk.meta.pop(key, None)


def _put(chunk, crs: str) -> dict:
    """``chunk`` put into ``crs`` - its system, the references of its markers, its georeference fitted again or
    converted - and what was drawn on it carried along aside: what :func:`set_survey_crs` does, but for writing what
    was drawn. ``{"said": what :func:`survey_crs_changes` says, "values": the new values of keys of ``chunk.meta``,
    "sections": the new sections of the project or None, "report": the report of the fit or None}``. The caller puts
    the chunk back (:func:`_snapshot`, :func:`_restore`) after a preview, and on any exception."""
    from ..markers import gcp

    _survey_system(crs)
    old = chunk.crs
    said = {"crs": crs, "refit": False, "method": None, "convert": dict.fromkeys(CONVERTED, 0), "rebuild": [],
            "kept": [], "drawings": None, "drawings_shift": None, "drawings_lift": None, "heights_unchanged": False,
            "transform_rms": None, "transform_relief": None, "transform_vertical": None, "markers_apart": None,
            "model_shift": None, "model_lift": None}
    done = {"said": said, "values": {}, "sections": None, "report": None}
    if crs == old:
        return done
    georeferenced = not crs_mod.is_local(old)
    sections, fate = _project_sections(chunk, old)
    said.update(rebuild=[name for name in IN_THE_SYSTEM if chunk.products.get(name)],
                kept=_kept(chunk, georeferenced, sections, fate))
    if not georeferenced:  # it only takes the system: what was drawn and the markers stay where they are
        chunk.crs = crs
        said.update(convert=_counts(chunk, False, False, sections, fate), drawings="kept")
        return done
    before, positions, extent = chunk.transform.copy(), [(m, m.position) for m in chunk.markers], []
    apart_before = _markers_apart(chunk, before)

    def spread() -> np.ndarray | None:  # the extent of the chunk in the old system, read when needed, once
        if not extent:
            extent.append(_extent_points(chunk, before))
        return extent[0]

    chunk.crs = crs
    _pinned_references(chunk, old)
    report = gcp.update_transform(chunk)
    method, fitted = report["method"] or "", {}
    if not ("markers" in method or "cameras" in method):
        # nothing fits it again in the new system (no references left, markers in a line or that the alignment does
        # not place, scale bars alone): its old georeference converted instead
        chunk.crs, chunk.transform, report = crs, before.copy(), None
        for m, position in positions:
            m.position = position
        fitted = _similarity(chunk, old, crs, spread()) or {}
        if fitted:
            chunk.transform = fitted["matrix"] @ before
    if report is None and not fitted:  # its georeference places nothing: nothing to stay on
        mover = _Convert(chunk, old, crs)
    else:
        mover = _Follow(chunk.transform @ np.linalg.inv(before),
                        _Ground(chunk.product_path("dem"), old, lambda: _level(spread(), before)))
    values, moved = _drawn(chunk, mover, sections if fate == "convert" else None)
    if mover.mode == "follow" and chunk.meta.get("region"):
        values["region"] = _region_follows(chunk.meta["region"], before, chunk.transform)
    shift, lift = _said(mover, old, crs)
    model_shift = model_lift = None
    if mover.mode == "follow" and spread() is not None:  # the model itself, drawn on or not
        probe = _Follow(mover.matrix, mover.ground)
        probe.tall(spread())
        model_shift, model_lift = _said(probe, old, crs)
    apart_after = _markers_apart(chunk, chunk.transform)
    typed = any(m.reference.location for m in chunk.markers)
    said.update(refit=report is not None, method=None if report is None else report["method"],
                convert=_counts(chunk, True, mover.mode == "follow", sections, fate), drawings=mover.mode,
                drawings_shift=shift, drawings_lift=lift,
                heights_unchanged=(height_kind(old) == NONE and height_kind(crs) != NONE
                                   and (typed or (mover.mode == "convert" and mover.heights))),
                transform_rms=fitted.get("rms"), transform_relief=fitted.get("relief"),
                transform_vertical=fitted.get("vertical"), model_shift=model_shift, model_lift=model_lift,
                markers_apart=None if apart_before is None or apart_after is None else [apart_before, apart_after])
    done.update(values=values, sections=moved, report=report)
    return done


def _markers_apart(chunk, transform: np.ndarray) -> float | None:
    """How far in plan (metres, the most) the references of the markers of ``chunk`` are from where the model, placed
    by ``transform``, puts them (their triangulated positions): the typed references are converted from system to
    system, the model follows its georeference, and the two can part. None without a marker that has both."""
    from ..core import geometry

    pairs = [(m.position, m.reference.location) for m in chunk.markers
             if m.position is not None and m.reference.location]
    if not pairs:
        return None
    model = geometry.apply(np.asarray(transform, float), np.array([p for p, _ in pairs], float).reshape(-1, 3))
    typed = np.array([r for _, r in pairs], float).reshape(-1, 3)
    far = np.hypot(model[:, 0] - typed[:, 0], model[:, 1] - typed[:, 1])
    far = far[np.isfinite(far)]
    return float(far.max()) if len(far) else None


def survey_crs_changes(chunk, crs: str) -> dict:
    """What :func:`set_survey_crs` would do to ``chunk`` with ``crs``, nothing changed: it is done - the georeference
    fitted again on the references of the chunk, what was drawn carried along - and undone, so that what it says is
    what the change then does. ``{"crs", "refit": whether the georeference is fitted again, "method": on what (the
    method of :func:`..markers.gcp.update_transform`) or None, "convert": {kind: how many} of :data:`CONVERTED`,
    "rebuild", "kept", "drawings", "drawings_shift", "drawings_lift", "heights_unchanged", "transform_rms",
    "transform_relief", "transform_vertical"}`` as :func:`set_survey_crs` describes them - for a window to show before
    applying. A ``ValueError`` for a system that is not one of a survey, and for what :func:`set_survey_crs` refuses
    (the references or what was drawn that cannot go into it, the georeference of a site too large for one
    similarity)."""
    state = _snapshot(chunk)
    try:
        return dict(_put(chunk, crs)["said"])
    finally:
        _restore(chunk, state)


def set_survey_crs(chunk, crs: str) -> dict:
    """``chunk`` in the coordinate system ``crs``, one with a projected plan in metres (the products need metric axes;
    alone or with heights above a geoid, as :func:`survey_crs` proposes it). When the chunk is georeferenced (in a
    system that is no local frame):

    * the references of its markers - measured coordinates, survey data - are converted into it (as points that carry
      no frame and epoch; their heights by the rule of every conversion: from a system without heights of its own as
      they are, ``heights_unchanged``, right only if they were heights above the sea level), the positions of the photos
      are read from their own system as always;
    * its georeference follows: fitted again on its references (:func:`..markers.gcp.update_transform`, the precision
      of the old fit dropped) when they fit it - three control markers, or the positions of its aligned photos -, else
      converted (a scan, a model or a cloud imported, a satellite chunk, a chunk whose references are gone): a
      similarity fitted from points spread over its extent - its region, else what lives in its internal frame, taken
      through the georeference - on one height to their conversion, composed with it, which takes the model, the
      clouds and the tie points along (``transform_rms``: how far it misses the plan of those points, refused, nothing
      changed, beyond :data:`SIMILARITY_RMS`: a site too large for one similarity between the two systems;
      ``transform_vertical``: by how much, over the relief of the extent ``transform_relief``, the new scale of the plan
      changes the relief of the model against the conversion, peak to peak - at one point at most half of it -, which
      every georeference in the new system carries, a georeference fitted again too); a chunk with nothing in its
      internal frame keeps its georeference, which places nothing;
    * what was drawn on its products (:data:`CONVERTED`) - the units, the spot heights and the measuring points
      (``chunk.meta["drawings"]``), the stones of the orthomosaic, the sections of the project (``sezioni/sezioni.json``
      beside its file, when no other chunk of the project is still in the old system), the patches of the orthomosaic
      and of the texture, the box of the colour chart - and the region of the reconstruction go with the georeference
      (``drawings`` "follow"): each point by the new georeference after the old one undone, a point of the plan at the
      height of the ground there (the DEM, else the median height of the extent), so that it stays on the model whatever
      the references are - their kinds of height, frames and epochs; ``drawings_lift``, the least and the most its
      heights change, and ``drawings_shift``, how far in plan it goes from the conversion of its coordinates, say what
      the new georeference does to it. On a chunk whose georeference places nothing it is converted as coordinates, as
      the markers are (``drawings`` "convert"). A window that holds the drawings stores them into the chunk first and
      reads them again after.

    From a local frame the chunk only takes the system: its markers and what was drawn stay where they are
    (``drawings`` "kept", ``kept``, ``"drawn"``); fit its georeference after.

    Returns ``{"crs", "updated": whether the georeference was fitted again, "method": on what (the method of the fit)
    or None, "transform_rms": how far the similarity that converted it misses the plan (metres) or None,
    "transform_relief" and "transform_vertical" (metres, or None), "rebuild": the products built in the coordinates of
    the old system (DEM, orthomosaic, precision, coverage) - to build again -, "kept": what stays in the old system and
    cannot be converted, ``[{"what", "names"}]`` - inactive versions of the DEM and the orthomosaic, elevations (frames,
    and the stones drawn on them), multispectral, thermal and satellite rasters, the rasters of the relief and of the
    raster calculator, the proxies of the units, the drawings (SVG, DXF) of the sections, or the sections themselves -,
    "converted": {kind: how many}, "drawings": "follow", "convert" or "kept", "drawings_shift" (metres) and
    "drawings_lift" (metres, the least and the most) or None, "heights_unchanged", "report": the report of the fit or
    None}``; the clouds, the model and the tie points are in the internal frame of the chunk, which the georeference
    places. :func:`survey_crs_changes` tells all of it before. A ``ValueError``, and the chunk, its drawings and the
    file of the sections as they were, for a system that is not one of a survey, when the references, the georeference
    or the drawings cannot go into it (heights above the geoid without the grid: :class:`GridMissing`; a site too large
    for one similarity; a system not in metres)."""
    state = _snapshot(chunk)
    try:
        done = _put(chunk, crs)
        chunk.meta.update(done["values"])
        if done["sections"] is not None:
            _write_sections(_sections_file(chunk), done["sections"])
    except BaseException:
        _restore(chunk, state)
        raise
    said = done["said"]
    if done["report"] is not None:
        chunk.forget_precision()
    return {"crs": crs, "updated": said["refit"], "method": said["method"], "transform_rms": said["transform_rms"],
            "transform_relief": said["transform_relief"], "transform_vertical": said["transform_vertical"],
            "rebuild": said["rebuild"], "kept": said["kept"], "converted": said["convert"],
            "drawings": said["drawings"], "drawings_shift": said["drawings_shift"],
            "drawings_lift": said["drawings_lift"], "heights_unchanged": said["heights_unchanged"],
            "markers_apart": said["markers_apart"], "model_shift": said["model_shift"],
            "model_lift": said["model_lift"], "report": done["report"]}
