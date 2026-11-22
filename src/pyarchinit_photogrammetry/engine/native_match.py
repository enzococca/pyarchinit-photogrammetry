"""The matching step of the native engine: ``match_photos(engine="native")``.

The features of the photos are extracted by the native core (``ppg_core`` API 13,
:func:`.._native.native_match_kernels`) into a feature store, the pairs that overlap are found on a small store
(the generic preselection), the pairs are matched in two stages into a match store, and the result is exported to
COLMAP's database - cameras, images, key points, matches and two-view geometries as the classic step writes them -
for the classic mapper (:func:`.sfm.align_cameras`) to run on it. The arguments and the effects are those of
:func:`.sfm.match_photos`: ``database.db`` in the sfm folder of the chunk, ``chunk.meta["sfm"]`` with the same keys
plus ``engine`` (``native``), ``features`` (``gpu``, ``cpu`` or ``gpu+cpu``: who detected the points) and ``routes``
(the pairs by the route of the matching: ``guided``, ``brute_force``, ``dropped``);
``chunk.meta["timings"]["match_photos"]`` with the stages ``features``, ``preselection``, ``pairs`` and ``export``. A
full disk, whichever layer says it (:func:`disk_full`), stops the step as an ``OSError`` of ENOSPC.
``guided_matching`` is ignored: the native matching always matches along the epipolar lines.

Layout, under ``<sfm>/native/``: ``features/`` (the feature store: every photo, in the pixels of the full photo with
COLMAP's centre), ``small/`` (the store of the preselection, deleted at the end of the step), ``matches/`` (the match
store). A fresh matching works in ``<sfm>/native.tmp/`` and ``<sfm>/database.native.tmp.db`` and moves them into
place (``os.replace``) only when it has finished, so a cancel, a failure or a full disk leaves the project as it was;
anything ``.tmp`` left by a crash is removed at the start of the next step. ``reset=False`` continues: the stores of
an earlier native matching - trusted when the record of the chunk says ``native`` with the same quality, point limit,
masks and photo folder, and the alignment can take more photos (:func:`.sfm._continuable`) - take the photos missing
from them and the pairs with one of them, in place; without them, a classic database that can be continued is
imported into fresh stores first (:func:`import_database`); else everything is done again (also when the database
is the export of a native matching, without descriptors: its matches index key points the stores no longer hold). A
continuation too works in ``native.tmp`` - the stores copied there by hard links, nothing in place written - and a
photo counts as done only when it is in the feature store and in the database, so a cancel or a failure at any point
leaves the project as it was and the next run takes up what is missing. At the end the stores go into place first
and the database last, each by one rename, the stores carrying the identity of the database they go with until both
are in place (``commit.json``); a crash between them is swept at the next start so that the stores go with the
database. The export keeps the ids of the photos, cameras, rigs and frames of the database it continues: the model
continues from them.

The thresholds of the two-stage matching live here (:data:`STAGE1_POINTS`, :data:`TRUST_INLIERS`,
:data:`DROP_INLIERS`) and go to the core with the options; every JPEG is decoded whole and reduced as COLMAP reduces
it, ``PPG_NATIVE_DCT=1`` decodes it at the DCT scale its reduction allows instead (faster, fewer tie points),
``PPG_NATIVE_CPU_SHARE=0`` leaves the pairs to the GPUs when there is one (by default the CPU threads do the second
stage and the verification of pairs beside them, their first stage from the GPU). The second stage is tuned at
run time (:func:`guided_settings`): ``PPG_NATIVE_GUIDED=0`` sends the trusted pairs to brute force instead,
``PPG_NATIVE_GUIDED_RATIO`` sets its ratio test (default :data:`GUIDED_RATIO`) and
``PPG_NATIVE_GUIDED_BAND`` the half-width of its band as a multiple of the verification's threshold (default 1.0);
the record of the step (``chunk.meta["sfm"]``) says the values used. The database is read with
:func:`.sfm.read_only_uri` and photos are named as :func:`.sfm.image_root_and_names` names them (POSIX relative
paths, blanks allowed): the stores key them by name and never make a file name of one.
"""
from __future__ import annotations

import errno
import itertools
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .. import _native
from ..core.chunk import QUALITY_DOWNSCALE, Chunk
from ..core.progress import Progress, as_progress
from . import sfm, timing

log = logging.getLogger(__name__)

# the two-stage matching of a pair (native/engine/src/pairs.rs): the points of the largest scale of each photo the
# first stage matches; the inliers of the first stage from which it is trusted and the second stage follows the
# epipolar lines; below that, the inliers from which a pair that was not preselected still goes to brute force
STAGE1_POINTS = 8000
TRUST_INLIERS = 40
DROP_INLIERS = 8
# the ratio test of the matching (COLMAP's default, the core's `max_ratio` unless given)
MAX_RATIO = 0.8
# the ratio test of the second stage (PPG_NATIVE_GUIDED_RATIO): a point there is compared only with the points of its
# band, so the test of the first stage lets through matches a ratio over all the points would refuse. Chosen on
# Sultan 57 (2026-11-22, three runs each, photos decoded whole): 0.8 gave 30,050 tie points a photo at 1.23 px,
# 0.7 29,530 at 1.03 px, 0.6 28,360 at 0.996 px - the classic matching's 28,700 at 0.996 px
GUIDED_RATIO = 0.6

STORES = "native"  # <sfm>/native/{features,small,matches}
TMP = "native.tmp"  # where a fresh matching works, moved into place when it has finished
OLD = "native.old.tmp"  # the stores being replaced, for the moment of the move
TMP_DB = "database.native.tmp.db"  # the database being written, moved into place when it has finished
COMMIT = "commit.json"  # in the stores while they are moved into place: the database they go with
FEATURES, SMALL, MATCHES = "features", "small", "matches"
INDEX = "index.json"
READERS = 4  # the threads that decode and reduce the photos while the kernels work (as the classic GPU path)

RIGS_REASON = "rigs of 360-degree photos: the native engine does not do them"

# what a full disk says through each layer: the system (``(os error N)`` of the core's errors: ENOSPC; on Windows
# also ERROR_HANDLE_DISK_FULL and ERROR_DISK_FULL, which are other errors elsewhere), sqlite (SQLITE_FULL, also
# through pycolmap's RuntimeError)
DISK_FULL_TEXTS = ("(os error 28)", "database or disk is full")
WINDOWS_DISK_FULL_TEXTS = ("(os error 39)", "(os error 112)")
EXPORT_EVERY = 500  # the pairs between two reports of the export and of the import (and their cancel points)


# the mark of an error of the request (an argument, the chunk: :func:`refused`), set on the error itself
REFUSED = "_ppg_native_refused"


def refused(exc: BaseException) -> bool:
    """Whether ``exc`` is the native step refusing the request - an argument the matching does not take, a chunk that
    is not in a project or has no enabled photo - with the error the classic step gives for it (the same type and
    text): an error of the caller, which reaches the caller whichever engine runs, never a reason to fall back."""
    return getattr(exc, REFUSED, False) is True


def _refuse(exc: BaseException) -> BaseException:
    """``exc`` marked as the native step refusing the request (:func:`refused`), for ``raise _refuse(exc)``."""
    setattr(exc, REFUSED, True)
    return exc


def _reference_pairs_or_refuse(chunk: Chunk, names: dict[int, str], reference: str, neighbours: int,
                               pairs: set | None) -> list | None:
    """The pairs of the ``reference`` preselection (``source``, ``estimated``, ``sequential``) as the classic step
    finds them; with none, and no other pairs (``pairs``: those of the generic preselection), the RuntimeError the
    classic step raises - the positions of the photos or an alignment missing -, marked as a refusal."""
    finder = {"source": sfm._reference_pairs, "estimated": sfm._estimated_pairs,
              "sequential": sfm._sequential_pairs}[reference]
    found = finder(chunk, names, neighbours)
    if not found and not pairs:
        raise _refuse(RuntimeError(f"the {reference} preselection needs "
                                   + ("camera reference positions" if reference == "source" else "an alignment")))
    return found


def disk_full(exc: BaseException) -> bool:
    """Whether ``exc`` says that the disk is full: an ``OSError`` of ENOSPC, or the error of the core, of pycolmap
    or of sqlite whose text says so (:data:`DISK_FULL_TEXTS`, and :data:`WINDOWS_DISK_FULL_TEXTS` on Windows). No
    engine can go on on a full disk: it is never a reason to fall back."""
    if isinstance(exc, OSError):
        return exc.errno == errno.ENOSPC
    if isinstance(exc, (RuntimeError, sqlite3.Error)):
        texts = DISK_FULL_TEXTS + (WINDOWS_DISK_FULL_TEXTS if sys.platform == "win32" else ())
        return any(text in str(exc) for text in texts)
    return False


def as_disk_full(exc: BaseException) -> OSError:
    """``exc`` (a full disk, :func:`disk_full`) as the ``OSError`` of ENOSPC the callers stop on."""
    if isinstance(exc, OSError):
        return exc
    return OSError(errno.ENOSPC, f"the disk is full: {exc}")


def dct_enabled() -> bool:
    """``PPG_NATIVE_DCT`` (default off): a JPEG decoded at the DCT scale its reduction allows, not whole. Off by
    default: the reduced DCT gives another raster than COLMAP's reduction of the whole photo, and fewer tie points
    per photo, for a small saving of time in the features stage."""
    return os.environ.get("PPG_NATIVE_DCT", "0").strip() not in ("", "0")


def cpu_share_enabled() -> bool:
    """``PPG_NATIVE_CPU_SHARE`` (default on): the CPU takes pairs beside the GPUs."""
    return os.environ.get("PPG_NATIVE_CPU_SHARE", "1").strip() != "0"


def _positive_env(name: str, default: float, upper: float | None = None) -> float:
    """The number in the environment variable ``name`` (``default`` when it is not set or blank): a ValueError
    naming it when it is not a number above 0 (and at most ``upper``)."""
    text = os.environ.get(name, "").strip()
    if not text:
        return default
    try:
        value = float(text)
    except ValueError:
        raise ValueError(f"{name} must be a number, not {text!r}") from None
    if not (value > 0 and math.isfinite(value)) or (upper is not None and value > upper):
        bound = f" and at most {upper}" if upper is not None else ""
        raise ValueError(f"{name} must be above 0{bound}, not {text!r}")
    return value


def guided_settings() -> dict:
    """The settings of the second stage from the environment: ``guided`` (``PPG_NATIVE_GUIDED``, default on: the
    trusted pairs are matched along the epipolar lines; ``0`` sends them to brute force), ``guided_max_ratio``
    (``PPG_NATIVE_GUIDED_RATIO``, default :data:`GUIDED_RATIO`) and ``guided_band``
    (``PPG_NATIVE_GUIDED_BAND``, default 1.0: the half-width of the band, a multiple of the verification's
    threshold) and ``guided_check`` (``PPG_NATIVE_GUIDED_CHECK``, default on: a match of the second stage must also
    pass the first stage's ratio test against the rival its points had in the first stage, over the whole photo,
    which refuses a candidate alone in its band whose descriptor has a twin elsewhere - a repetitive texture, a point
    at the wrong depth; ``0`` for trials). A value that is not one is a ValueError naming its variable."""
    return {"guided": os.environ.get("PPG_NATIVE_GUIDED", "1").strip() != "0",
            "guided_max_ratio": _positive_env("PPG_NATIVE_GUIDED_RATIO", GUIDED_RATIO, upper=1.0),
            "guided_band": _positive_env("PPG_NATIVE_GUIDED_BAND", 1.0),
            "guided_check": os.environ.get("PPG_NATIVE_GUIDED_CHECK", "1").strip() != "0"}


def pair_options(tiepoint_limit: int, guided: dict) -> dict:
    """The options of the pairs stage for the core: the thresholds of the routes, the cap of the matches from the
    tie point limit and the settings of the second stage (:func:`guided_settings`); those that keep the core's
    defaults are left out (the core's ratio of the second stage is the first stage's, :data:`MAX_RATIO`)."""
    options = {"stage1_points": STAGE1_POINTS, "trust_inliers": TRUST_INLIERS, "drop_inliers": DROP_INLIERS,
               "max_num_matches": max(int(tiepoint_limit) * 4, 8192)}
    if not guided["guided"]:
        options["guided"] = False
    if guided["guided_max_ratio"] != MAX_RATIO:
        options["guided_max_ratio"] = float(guided["guided_max_ratio"])
    if guided["guided_band"] != 1.0:
        options["guided_band"] = float(guided["guided_band"])
    if not guided["guided_check"]:
        options["guided_check"] = False
    return options


def _kernels():
    k = _native.native_match_kernels()
    if k is None:
        raise RuntimeError(f"the native core with the native matching (API {_native.NATIVE_MATCH_API}) is not "
                           "available")
    return k


def core_unavailable() -> str | None:
    """Why the native core cannot do the matching on this computer, or None: missing, switched off
    (``PPG_NATIVE=0``) or below the API of the native matching - the reasons of :func:`cannot_run` that are of the
    installation, not of the request (``auto`` says them in the log)."""
    if not _native.enabled():
        return "the native core is switched off (PPG_NATIVE=0)"
    if _native.native_match_kernels() is None:
        api = _native.api()  # None without the core
        if api is None:
            return "the native core (ppg_core) is not installed"
        return (f"the native core {_native.version()} (API {api}) has no native matching: API "
                f"{_native.NATIVE_MATCH_API} is needed")
    return None


def cannot_run(chunk: Chunk, matcher: str, reset: bool) -> str | None:
    """Why the native step cannot do the matching of ``chunk``, or None when nothing keeps it: the native core
    missing, switched off (``PPG_NATIVE=0``) or below API 13; a learned ``matcher``; rigs of 360-degree photos;
    processing nodes that would share the points (:mod:`.nodes`, as the classic step counts them: present, and
    enough photos to detect); a camera model the engine does not know (the model the photos are aligned with,
    :func:`.sfm.mapping_camera`). A chunk without enabled photos or without a folder is not the engine's to refuse:
    the step says so itself, whichever engine runs it."""
    from . import nodes, panorama

    reason = core_unavailable()
    if reason is not None:
        return reason
    if matcher != "sift":
        return f"learned matcher '{matcher}': the native engine matches SIFT points only"
    try:
        _root, names = sfm.image_root_and_names(chunk)
    except RuntimeError:
        return None
    if panorama.rig_configs(chunk, names):
        return RIGS_REASON
    for skey in sorted({c.sensor_key for c in chunk.cameras if c.enabled}):
        sensor = chunk.sensors.get(skey)
        if sensor is None:  # a camera without its sensor: not the engine's to refuse, the step says what it does
            continue
        model = sfm.mapping_camera(sensor)[0]
        if not _native.supports(model):
            return f"camera model {model} of sensor '{sensor.label}': the native engine does not know it"
    try:
        files_dir = chunk.data_dir.parent
    except RuntimeError:
        return None
    if nodes.wanted(files_dir):
        db_path = chunk.data_dir / "sfm" / "database.db"
        existing = sfm._db_image_names(db_path) if not reset and db_path.exists() else set()
        if len([n for n in names.values() if n not in existing]) >= 2 * nodes.FEATURE_UNIT_PHOTOS:
            return "processing nodes share the points of the photos: the native engine does not use them"
    return None


def match_photos(chunk: Chunk, quality: str = "high", keypoint_limit: int = 40000, tiepoint_limit: int = 8000,
                 pair_selection: str = "auto", progress: Progress | None = None, exclude_stationary: bool = False,
                 generic: bool | None = None, reference: str | None = None,
                 neighbours: int = sfm.REFERENCE_NEIGHBOURS, apply_masks: str = "keypoints",
                 guided_matching: bool = False, reset: bool = True, gpu: str | None = None) -> None:
    """The matching of :func:`.sfm.match_photos` done by the native engine (the module's docstring): the same
    arguments, the same ``database.db`` for the alignment, the same record in ``chunk.meta["sfm"]`` with ``engine``,
    ``features`` and ``routes`` beside, the seconds of the step and of its stages (``features``, ``preselection``,
    ``pairs``, ``export``) in ``chunk.meta["timings"]["match_photos"]``, also when the step fails or is cancelled
    (then with the name of the type of the error under ``failed``). ``guided_matching`` is ignored, with a line in the
    log. What stops the step comes out as it is: :class:`Cancelled`, the errors of the core; a full disk, whichever
    layer says so (:func:`disk_full`), as an ``OSError`` of ENOSPC; the project is left as it was."""
    stages, started = timing.Stages(), time.perf_counter()
    failed = None
    try:
        _match_photos(chunk, stages, quality=quality, keypoint_limit=keypoint_limit, tiepoint_limit=tiepoint_limit,
                      pair_selection=pair_selection, progress=progress, exclude_stationary=exclude_stationary,
                      generic=generic, reference=reference, neighbours=neighbours, apply_masks=apply_masks,
                      guided_matching=guided_matching, reset=reset, gpu=gpu)
    except Exception as exc:
        if disk_full(exc) and not isinstance(exc, OSError):  # pycolmap's, sqlite's: an OSError as the system's
            failed = "OSError"
            raise as_disk_full(exc) from exc
        failed = type(exc).__name__
        raise
    except BaseException as exc:
        failed = type(exc).__name__
        raise
    finally:
        timing.store(chunk, "match_photos", time.perf_counter() - started, stages.as_dict(),
                     **({"failed": failed} if failed else {}))


# the texts the step reports its parts with (the tests watch them to act at a given moment)
PROGRESS_FEATURES = "Detecting points"
PROGRESS_PRESELECTION = "Finding the pairs of photos that overlap"
PROGRESS_PAIRS = "Matching photos"
PROGRESS_EXPORT = "Writing the matches for the alignment"
GUIDED_LINE = "the native matching always matches along the epipolar lines (guided)"


def _identity(path: Path) -> list[int] | None:
    """What tells a file from another one of the same name: its size and time of change, which a rename keeps and so
    does a copy that keeps the times (not the inode: a copy, a remount of FAT or exFAT, a sync client re-creating
    the file change it); None when it is not there."""
    try:
        st = path.stat()
    except OSError:
        return None
    return [int(st.st_size), int(st.st_mtime_ns)]


def _write_commit(stores: Path, db_path: Path) -> None:
    """The record of a commit in ``stores`` (:data:`COMMIT`): the identity of the database they go with, written
    before the stores and the database are moved into place, removed when both are."""
    (stores / COMMIT).write_text(json.dumps({"database": _identity(db_path)}))


def _committed(stores: Path, db_path: Path) -> bool | None:
    """Whether the database in place is the one the record of ``stores`` names (the commit replaced it), or None
    without a record that can be read."""
    try:
        recorded = json.loads((stores / COMMIT).read_text())["database"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return recorded is not None and recorded == _identity(db_path)


def _remove_leftovers(work: Path) -> None:
    """What a step that died left in the sfm folder, swept so that the stores in place go with the database in place
    as far as the record of the commit can tell (:func:`_identity`). The stores being replaced (``native.old.tmp``)
    are put back when the new ones never came into place, or came but the database was not replaced (their commit
    record names another database: they are removed); they are removed when it was replaced; with new stores and
    no record there is no telling, and both go (the next step does every photo again). New stores of a first
    matching beside a database that is not theirs go too. Then the work folder, the database being written, and
    the store of the preselection (made again by the step that needs it) are removed."""
    stores, old, db_path = work / STORES, work / OLD, work / "database.db"
    if old.is_dir():
        if not stores.exists():
            os.replace(old, stores)
        else:
            replaced = _committed(stores, db_path)
            if replaced is None:
                log.warning("%s: the stores of the native matching beside the ones they replaced, without a record "
                            "of the commit: both removed, every photo is matched again", work)
                shutil.rmtree(stores, ignore_errors=True)
            elif not replaced:
                shutil.rmtree(stores, ignore_errors=True)
                os.replace(old, stores)
    elif (stores / COMMIT).exists() and not _committed(stores, db_path):
        shutil.rmtree(stores, ignore_errors=True)  # a first matching that died before its database was in place
    (stores / COMMIT).unlink(missing_ok=True)
    for folder in (work / TMP, old, stores / SMALL):
        shutil.rmtree(folder, ignore_errors=True)
    for pattern in (f"{TMP_DB}*", "database.db.tmp*"):
        for path in work.glob(pattern):
            path.unlink(missing_ok=True)


def remove_work(work: Path) -> None:
    """Everything of the native matching in the sfm folder ``work`` removed, nothing put back: the stores, the work
    folder, the stores being replaced, the databases being written - for a step that matches every photo again."""
    for folder in (work / STORES, work / TMP, work / OLD):
        shutil.rmtree(folder, ignore_errors=True)
    for pattern in (f"{TMP_DB}*", "database.db.tmp*"):
        for path in work.glob(pattern):
            path.unlink(missing_ok=True)


def _stores_present(stores: Path) -> bool:
    return (stores / FEATURES / INDEX).is_file() and (stores / MATCHES / INDEX).is_file()


def _stores_trusted(chunk: Chunk, stores: Path, root: Path, quality: str, keypoint_limit: int,
                    apply_masks: str, dct: bool, guided: dict) -> bool:
    """Whether the stores in ``stores`` are those of a native matching of these photos with these settings: the
    record of the chunk says so (an earlier classic matching, or other settings, made the database from other
    points, and the stores would be trusted over it). The decoding (``dct``) and the second stage (``guided``,
    :func:`guided_settings`) count too; a record written before they were recorded had the DCT scale and the first
    stage's ratio, and one written before the check of the second stage had none (its stores are not continued:
    their matches hold the points at the wrong depth the check refuses)."""
    info = chunk.meta.get("sfm") or {}
    made = (info.get("native_dct", True), info.get("native_guided", True),
            info.get("native_guided_ratio", MAX_RATIO), info.get("native_guided_band", 1.0),
            info.get("native_guided_check", False))
    return (_stores_present(stores) and info.get("engine") == "native" and info.get("quality") == quality
            and info.get("keypoint_limit") == int(keypoint_limit) and info.get("apply_masks") == apply_masks
            and info.get("image_root") == str(root)
            and made == (bool(dct), guided["guided"], guided["guided_max_ratio"], guided["guided_band"],
                         guided["guided_check"]))


def _stage_stores(stores: Path, wd: Path) -> None:
    """A copy of the feature and match stores of ``stores`` made in ``wd`` for a continuation to add to, so that the
    stores in place are never written: every file of a store hard-linked (a store never writes over a file - a put
    writes a new file and rewrites its index to a new name -, so what the copy does leaves the originals as they
    are), copied where a link cannot be made, the index copied."""
    for name in (FEATURES, MATCHES):
        src, dst = stores / name, wd / name
        dst.mkdir(parents=True, exist_ok=True)
        for path in src.iterdir():
            if path.name == INDEX or not path.is_file():
                continue
            try:
                os.link(path, dst / path.name)
            except OSError:
                shutil.copy2(path, dst / path.name)
        shutil.copyfile(src / INDEX, dst / INDEX)


def _camera(sensor) -> tuple:
    """A camera as the kernels take it: the model the photos are aligned with (:func:`.sfm.mapping_camera`), the
    size of the sensor, a prior focal length (as both classic paths give every camera)."""
    model, params = sfm.mapping_camera(sensor)
    return model, [float(p) for p in params], int(sensor.width), int(sensor.height), True


def _relay(sub: Progress, text: str, noun: str):
    """The progress callback of a step of the core (``(done, total, message)``) onto ``sub``; an exception of
    ``sub`` (a cancel) stops the step."""

    def call(done, total, _message):
        sub(done / total if total else 1.0, f"{text}: {done} of {total}" + (f" {noun}" if noun else ""))

    return call


def _features_word(gpu: int, cpu: int) -> str:
    return "gpu+cpu" if gpu and cpu else ("gpu" if gpu else "cpu")


def _gpu_devices(gpu: str | None) -> tuple[list[int], str | None]:
    """The GPUs the kernels work on for the setting ``gpu`` (:func:`.._native.gpu_devices`) and their names for the
    log; one that does not answer is left out with a warning, as the classic step leaves it."""
    devices, names = [], []
    for d in _native.gpu_devices(gpu):
        try:
            names.append(_native.kernel("gpu_device_name")(d))
            devices.append(d)
        except Exception as exc:  # noqa: BLE001 - a GPU that does not work: the others, or the CPU alone
            log.warning("GPU %s unavailable for the native matching (%s)", d, exc)
    return devices, (", ".join(names) if devices else None)


@dataclass
class _Run:
    """What the parts of one run of the step share: the kernels, the chunk, its photos and their folders, the
    devices and the settings read once; and what the continuation decided - ``keep`` (a block continued), ``done``
    (the photos the block has: in the feature store *and* in the database), ``to_extract`` (the photos the store
    lacks), ``new`` (the photos not done: their pairs are matched and they go into the database) and
    ``small_size`` (the size of the preselection images, None without a preselection). The step works in
    :attr:`wd` (``native.tmp``) and moves it into :attr:`stores` when it has finished."""

    k: object
    chunk: Chunk
    work: Path
    root: Path
    names: dict[int, str]
    downscale: int
    devices: list[int] = field(default_factory=list)
    gpu_names: str | None = None
    dct: bool = False
    cpu_share: bool = True
    guided: dict = field(default_factory=lambda: {"guided": True, "guided_max_ratio": GUIDED_RATIO, "guided_band": 1.0})
    masks: Path | None = None
    keep: bool = False
    done: set[str] = field(default_factory=set)
    to_extract: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    small_size: int | None = None

    def __post_init__(self) -> None:
        self.sensor_of = {self.names[c.key]: c.sensor_key for c in self.chunk.cameras if c.enabled}
        self.everyone = sorted(self.names.values())
        # the photos in the order of the chunk, by sensor, as the classic step lists the new ones
        by_sensor: dict[int, list[str]] = {}
        for c in self.chunk.cameras:
            if c.enabled:
                by_sensor.setdefault(c.sensor_key, []).append(self.names[c.key])
        self.ordered = [n for group in by_sensor.values() for n in group]

    @property
    def stores(self) -> Path:
        return self.work / STORES

    @property
    def wd(self) -> Path:
        return self.work / TMP

    @property
    def db_path(self) -> Path:
        return self.work / "database.db"

    @property
    def tmp_db(self) -> Path:
        return self.work / TMP_DB

    @property
    def features_dir(self) -> Path:
        return self.wd / FEATURES

    @property
    def small_dir(self) -> Path:
        return self.wd / SMALL

    @property
    def matches_dir(self) -> Path:
        return self.wd / MATCHES

    def long_side(self, name: str) -> int:
        s = self.chunk.sensors[self.sensor_of[name]]
        return max(int(s.width), int(s.height))

    def limit_of(self, name: str) -> int:
        """COLMAP's ``max_image_size`` of the photo for the quality: its long side over the downscale."""
        return int(self.long_side(name) / self.downscale)

    def small_limit_of(self, name: str) -> int | None:
        return min(int(self.small_size), self.long_side(name)) if self.small_size else None

    def camera(self, name: str) -> tuple:
        return _camera(self.chunk.sensors[self.sensor_of[name]])

    def mask_of(self, name: str) -> str | None:
        return str(self.masks / f"{name}.png") if self.masks is not None else None

    def job(self, name: str, limit: int, small_limit: int | None) -> tuple:
        """A photo as ``native_features`` takes it: ``(name, path, mask, limit, small_limit)``."""
        return name, str(self.root / name), self.mask_of(name), limit, small_limit

    def names_in(self, store_dir: Path) -> set[str]:
        """The photos of a store, opened for the question: a handle kept open would not see what the kernels wrote
        since."""
        return set(self.k.store_names(str(store_dir))) if (store_dir / INDEX).is_file() else set()


def _continuation(run: _Run, *, reset: bool, quality: str, keypoint_limit: int, apply_masks: str) -> str | None:
    """How the step continues what the chunk has, for ``reset=False``: ``"stores"`` - the stores of an earlier native
    matching, trusted (:func:`_stores_trusted`) and holding no photo that left the chunk, copied to add to -,
    ``"import"`` - a classic database that can be continued (:func:`.sfm._continuable`), imported into fresh stores
    first - or None: everything again (also when the database has photos without descriptors: the export of a
    native matching whose stores are not trusted)."""
    if reset or not sfm._continuable(run.chunk, run.db_path, run.root, run.names, "sift"):
        return None
    if (_stores_trusted(run.chunk, run.stores, run.root, quality, keypoint_limit, apply_masks, run.dct, run.guided)
            and set(run.k.store_names(str(run.stores / FEATURES))) <= set(run.everyone)):
        return "stores"
    if _photos_without_descriptors(run.db_path):
        # the export of a native matching (the mapper needs no descriptors): its matches index key points the new
        # settings would not keep, and nothing could be matched with its photos
        return None
    return "import"


def _photos_without_descriptors(db_path: Path) -> int:
    """How many images of COLMAP's database ``db_path`` have key points and no descriptors - the mark of the export
    of a native matching; a photo on which the classic SIFT found nothing (no key points either) is not one."""
    con = sqlite3.connect(sfm.read_only_uri(db_path), uri=True)
    try:
        return int(con.execute("SELECT COUNT(*) FROM keypoints WHERE rows > 0 AND image_id NOT IN "
                               "(SELECT image_id FROM descriptors WHERE rows > 0)").fetchone()[0])
    finally:
        con.close()


def _begin(run: _Run, how: str | None, progress: Progress) -> None:
    """The work folder of the step made from what is continued (``how`` of :func:`_continuation`), and the photos
    sorted: ``done`` are those in the feature store *and* in the database - a photo a cancelled continuation left in
    a store is not done -, ``to_extract`` those the store lacks, ``new`` those not done."""
    if how == "stores":
        progress(0.0, "Continuing the matching: the stores of the alignment copied to add to")
        _stage_stores(run.stores, run.wd)
    elif how == "import":
        progress(0.0, "Importing the matches of the alignment to continue")
        imported = import_database(run.db_path, run.features_dir, run.matches_dir, progress=progress)
        log.info("%d photos of the database imported into the stores of the native matching", imported)
    run.keep = how is not None
    in_store = run.names_in(run.features_dir)
    run.done = (in_store & sfm._db_image_names(run.db_path)) if run.keep else set()
    run.to_extract = [n for n in run.ordered if n not in in_store]
    run.new = [n for n in run.ordered if n not in run.done]


def _detect_features(run: _Run, progress: Progress, keypoint_limit: int, generic: bool) -> str:
    """The features stage: the photos the store lacks extracted into the feature store (and the small store when a
    preselection runs), files the core does not decode given as pixels, unreadable ones logged; with a
    preselection, the small store completed for the photos continued. Returns who detected the points: ``gpu``,
    ``cpu`` or ``gpu+cpu``."""
    k = run.k
    sub = progress.sub(0.0, 0.4 if generic else 0.45)  # with a preselection, the small images of the rest after it
    progress(0.0, f"{PROGRESS_FEATURES} in {len(run.to_extract)} photos" + (" (with masks)" if run.masks else ""))
    jobs = [run.job(name, run.limit_of(name), run.small_limit_of(name)) for name in run.to_extract]
    small_store = str(run.small_dir) if run.small_size else None
    report = k.features(str(run.features_dir), small_store, jobs, int(keypoint_limit), sfm.PRESELECTION_POINTS,
                        run.devices, run.dct, run.cpu_share, READERS, _relay(sub, PROGRESS_FEATURES, "photos"))
    for line in report["log"]:
        log.info("native features: %s", line)
    counts = {"gpu": report["gpu"], "cpu": report["cpu"]}
    for name in report["unsupported"]:  # files the core does not decode: OpenCV does, the kernels take pixels
        given, refused = _features_from_pixels(k, run.root, name, run.masks, run.features_dir,
                                               run.small_dir if run.small_size else None, run.limit_of(name),
                                               run.small_limit_of(name), int(keypoint_limit), run.devices)
        if given is not None:
            counts[given] += 1
        elif refused is not None:
            log.warning("%s: too small for the kernels, no key points (%s)", name, refused)
        else:
            log.warning("%s: unreadable, no key points", name)
    for name in report["unreadable"]:
        log.warning("%s: unreadable, no key points", name)
    with_features = run.names_in(run.features_dir)
    if generic:  # the photos continued have no preselection image: theirs, into the small store
        present_small = run.names_in(run.small_dir)
        small_jobs = [run.job(name, min(sfm.PRESELECTION_SIZE, run.long_side(name)), None)
                      for name in run.everyone if name not in present_small and name in with_features]
        if small_jobs:
            more = k.features(str(run.small_dir), None, small_jobs, sfm.PRESELECTION_POINTS, sfm.PRESELECTION_POINTS,
                              run.devices, run.dct, run.cpu_share, READERS,
                              _relay(progress.sub(0.4, 0.05), "Reducing photos for the pairs", "photos"))
            for line in more["log"]:
                log.info("native features: %s", line)
    features = _features_word(counts["gpu"], counts["cpu"])
    progress(0.45, f"Points detected in {len(with_features)} photos"
        + (f" on the {features.upper()}" if counts["gpu"] else ""))
    return features


def _preselect(run: _Run, progress: Progress, cams: dict) -> set[tuple[str, str]]:
    """The generic preselection on the small store (:func:`.sfm._preselected_pairs` done by the core): the pairs with
    enough inliers on the small images, the strongest of every photo kept (:func:`.sfm.strongest_pairs`) and the
    nearest neighbours by the reference positions added; the small store removed after."""
    k = run.k
    small_names = [name for name in sorted(run.names_in(run.small_dir)) if name in cams]
    sub = progress.sub(0.45, 0.1)
    report = k.preselect(str(run.small_dir), {name: cams[name] for name in small_names}, run.devices,
                         sfm.PRESELECTION_INLIERS, _relay(sub, PROGRESS_PRESELECTION, ""))
    for line in report["log"]:
        log.info("native preselection: %s", line)
    weights = {(a, b): int(w) for a, b, w in report["weights"]}
    per_photo = sfm.PRESELECTION_PAIRS_PER_PHOTO
    found = sfm.strongest_pairs(weights, per_photo) if per_photo else set(weights)
    found |= set(sfm._reference_pairs(run.chunk, run.names, neighbors=8) or [])
    shutil.rmtree(run.small_dir, ignore_errors=True)  # its only use
    sub(1.0, f"{len(found):,} pairs of photos overlap")
    return found


def _select_pairs(run: _Run, stages: timing.Stages, progress: Progress, generic: bool, reference: str | None,
                  neighbours: int) -> tuple[list[tuple[str, str, bool]], str, dict]:
    """The pairs to match, as the classic step chooses them: the generic preselection (stage ``preselection``), the
    reference preselection (``source``, ``estimated``, ``sequential``), every pair when neither applies; on a
    continuation only the pairs with a new photo; only photos with features. Each pair carries whether it was chosen
    - by a preselection, or by an exhaustive matching, which tries every pair in full: the first stage never drops a
    chosen pair. Returns the pairs, the mode (``generic``, ``source``, ``generic+source``, ..., ``exhaustive``) and
    the cameras of the photos with features."""
    present = sorted(run.names_in(run.features_dir) & set(run.everyone))
    cams = {name: run.camera(name) for name in present}
    pairs: set[tuple[str, str]] | None = None
    chosen: set[tuple[str, str]] = set()
    parts = []
    if generic:
        progress(0.45, PROGRESS_PRESELECTION)
        with stages.stage("preselection"):
            found = _preselect(run, progress, cams)
        if found:  # nothing found on the small images: every pair
            pairs, parts = set(found), ["generic"]
            chosen |= set(found)
    if reference:
        with stages.stage("preselection"):
            found = _reference_pairs_or_refuse(run.chunk, run.names, reference, max(int(neighbours), 1), pairs)
        if found:
            pairs = (pairs or set()) | set(found)
            chosen |= set(found)
            parts.append(reference)
    mode = "+".join(parts) if pairs else "exhaustive"
    if pairs is None:  # exhaustive: every pair in full, as the classic step matches it
        pairs = {(a, b) for i, a in enumerate(run.everyone) for b in run.everyone[i + 1:]}
        chosen = set(pairs)
    if run.keep:  # only the pairs with a photo new to the block
        added = set(run.new)
        pairs = {p for p in pairs if p[0] in added or p[1] in added}
    pair_list = [(a, b, (a, b) in chosen) for a, b in sorted(pairs) if a in cams and b in cams]
    return pair_list, mode, cams


def _match_pairs(run: _Run, progress: Progress, pair_list: list, cams: dict, mode: str,
                 tiepoint_limit: int) -> tuple[dict, dict]:
    """The pairs stage: ``pair_list`` matched in two stages by the core into the match store, on the GPUs and the
    CPU. Returns the pairs by device (``by_device``) and by route (``routes``)."""
    progress(0.55, f"{PROGRESS_PAIRS} ({mode}, {len(pair_list):,} pairs)"
             + (f" on {run.gpu_names} and the CPU" if run.devices and run.cpu_share
                else f" on {run.gpu_names}" if run.devices else ""))
    options = pair_options(tiepoint_limit, run.guided)
    sub = progress.sub(0.55, 0.35)
    matched = run.k.match_pairs(str(run.features_dir), str(run.matches_dir), pair_list, cams, run.devices, options,
                                _relay(sub, PROGRESS_PAIRS, "pairs"), run.cpu_share)
    for line in matched["log"]:
        log.warning("native matching: %s", line)
    by_device = {str(name): int(count) for name, count in matched["by_device"].items()}
    routes = {"guided": int(matched["guided"]), "brute_force": int(matched["brute_force"]),
              "dropped": int(matched["dropped"])}
    sub(1.0, f"Matched {matched['done']:,} pairs: {routes['guided']:,} along the epipolar lines, "
             f"{routes['brute_force']:,} by brute force, {routes['dropped']:,} dropped")
    return by_device, routes


def _commit(run: _Run, progress: Progress, exclude_stationary: bool, old_info: dict) -> int | None:
    """The export stage: COLMAP's database written from the work folder (the ids of a continued database kept), the
    stationary matches removed when asked, then everything into place - the stores first, the database last, each
    by one rename, each undone on its own when a later one fails. The new stores carry the identity of the database
    they go with (:data:`COMMIT`) until both are in place, so a crash or an undo that fails between the renames is
    swept by :func:`_remove_leftovers` into a state where the stores go with the database: the old stores back
    when the database was not replaced, removed when it was. Returns the stationary matches removed (the count of
    the step continued when it did not run again)."""
    progress(0.9, PROGRESS_EXPORT)
    export_database(run.chunk, run.tmp_db, descriptors=False, stores=run.wd,
                    previous=run.db_path if run.keep else None, stationary_rule=not exclude_stationary,
                    progress=progress.sub(0.9, 0.1))
    if exclude_stationary:
        removed = sfm.remove_stationary_matches(run.tmp_db)
    else:  # a continuation keeps the count of the step it continues (the export applied its removal again)
        removed = old_info.get("stationary_removed") if run.keep else None
    old = run.work / OLD
    _write_commit(run.wd, run.tmp_db)  # goes with the stores: the sweep tells by it whether the database followed
    moved_old = moved_new = False
    try:
        if run.stores.exists():
            os.replace(run.stores, old)
            moved_old = True
        os.replace(run.wd, run.stores)
        moved_new = True
        os.replace(run.tmp_db, run.db_path)
    except BaseException:
        # each undone on its own: one that fails leaves a state the sweep of the caller puts right (the record)
        if moved_new:
            try:
                os.replace(run.stores, run.wd)
            except OSError as exc:
                log.warning("the new stores of the native matching could not be moved back (%s)", exc)
        if moved_old:
            try:
                os.replace(old, run.stores)
            except OSError as exc:
                log.warning("the stores of the native matching could not be put back yet (%s)", exc)
        raise
    shutil.rmtree(old, ignore_errors=True)
    if not run.keep:  # the learned key points of a classic database (``learned/``) went with it
        shutil.rmtree(run.work / "learned", ignore_errors=True)
    (run.stores / COMMIT).unlink(missing_ok=True)
    shutil.rmtree(run.stores / SMALL, ignore_errors=True)
    return removed


def _adapter_name(key: str) -> str:
    """The name of a GPU as the classic record gives it (``gpu_device_name``: the adapter's name) from the name the
    core gives the device in the counts of the pairs (``GPU <index> (<adapter's name>)``, ``#<n>`` after a twin)."""
    match = re.fullmatch(r"GPU \d+ \((.*)\)( #\d+)?", key)
    return match.group(1) + (match.group(2) or "") if match else key


def _record(run: _Run, *, features: str, mode: str, pairs: int, by_device: dict, routes: dict, removed: int | None,
            old_info: dict, quality: str, keypoint_limit: int, tiepoint_limit: int, generic: bool,
            reference: str | None, neighbours: int, apply_masks: str, guided_matching: bool) -> None:
    """``chunk.meta["sfm"]``: the keys of the classic record, plus ``engine``, ``features``, ``routes`` and
    ``preselection_size``; the GPUs named as the classic record names them (:func:`_adapter_name`)."""
    gpu_shares: dict[str, int] = {}
    for key, count in by_device.items():
        if key == "CPU":
            continue
        name = _adapter_name(key)
        if name in gpu_shares:  # two adapters of one name: each keeps its count
            name = f"{name} #{sum(1 for n in gpu_shares if n == name or n.startswith(name + ' #')) + 1}"
        gpu_shares[name] = count
    run.chunk.meta["sfm"] = {"engine": "native", "image_root": str(run.root),
                             "names": {str(k): v for k, v in run.names.items()}, "features": features,
                             "matcher": "sift", "quality": quality, "pair_selection": mode, "pairs": pairs,
                             "pairs_unmatched": 0, "generic": bool(generic), "reference": reference,
                             "neighbours": int(neighbours), "masks": run.masks is not None, "apply_masks": apply_masks,
                             "guided_matching": bool(guided_matching), "stationary_removed": removed, "rigs": 0,
                             "keypoint_limit": int(keypoint_limit), "tiepoint_limit": int(tiepoint_limit),
                             "continued": run.keep, "added": run.new if run.keep else None, "nodes": [],
                             "gpu": ", ".join(gpu_shares) if gpu_shares else None,
                             "gpu_pairs": sum(gpu_shares.values()), "gpu_shares": gpu_shares or None,
                             "cpu_pairs": by_device.get("CPU", 0), "routes": routes,
                             "preselection_size": run.small_size, "native_dct": run.dct,
                             "native_guided": run.guided["guided"],
                             "native_guided_ratio": run.guided["guided_max_ratio"],
                             "native_guided_band": run.guided["guided_band"],
                             "native_guided_check": run.guided["guided_check"]}
    if run.keep and old_info.get("gps_priors"):
        run.chunk.meta["sfm"]["gps_priors"] = old_info["gps_priors"]  # the priors already in the database: same axes


def _match_photos(chunk: Chunk, stages: timing.Stages, *, quality: str, keypoint_limit: int, tiepoint_limit: int,
                  pair_selection: str, progress: Progress | None, exclude_stationary: bool, generic: bool | None,
                  reference: str | None, neighbours: int, apply_masks: str, guided_matching: bool, reset: bool,
                  gpu: str | None) -> None:
    """The matching of :func:`match_photos`: the arguments checked, the chunk read, the continuation decided
    (:func:`_continuation`, :func:`_begin`), then the stages ``features`` (:func:`_detect_features`),
    ``preselection`` (:func:`_select_pairs`), ``pairs`` (:func:`_match_pairs`) and ``export`` (:func:`_commit`),
    and the record (:func:`_record`). Whatever stops the step, the work folder goes and the project is as it was."""
    from ..markers import gcp
    from . import panorama
    from .masks import colmap_mask_folder

    k = _kernels()
    progress = as_progress(progress)
    try:  # the request checked as the classic step checks it: what it refuses reaches the caller (refused)
        if apply_masks not in sfm.MASK_MODES:
            raise ValueError(f"apply_masks: one of {', '.join(sfm.MASK_MODES)}")
        generic, reference = sfm._preselection(pair_selection, generic, reference)
        if guided_matching:
            log.info(GUIDED_LINE)
        try:  # the matching needs no system: what the alignment would refuse is only said here
            gcp.preflight(chunk)
        except Exception as exc:  # noqa: BLE001 - whatever it is: the references may still be switched off before it
            log.warning("the alignment will not place the chunk by the positions of the photos while they are "
                        "switched on: %s", exc)
        work = sfm._sfm_dir(chunk)
        root, names = sfm.image_root_and_names(chunk)
        nearest = max(int(neighbours), 1)  # what the classic step takes, and refuses (ValueError, TypeError)
        if reference and not (generic if generic is not None else len(names) > sfm.EXHAUSTIVE_UP_TO):
            # without the generic preselection a reference one that finds no pairs is refused before any point is
            # detected (the classic step refuses it after its points)
            _reference_pairs_or_refuse(chunk, names, reference, nearest, None)
    except (ValueError, TypeError, RuntimeError) as exc:
        raise _refuse(exc)
    _remove_leftovers(work)
    sfm._interrupted_names_back(work / "database.db")  # the names a classic matching that died in COLMAP left aliased
    if panorama.rig_configs(chunk, names):
        raise RuntimeError(RIGS_REASON)
    run = _Run(k, chunk, work, root, names, QUALITY_DOWNSCALE.get(quality, 2))
    run.devices, run.gpu_names = _gpu_devices(gpu)
    run.dct, run.cpu_share, run.guided = dct_enabled(), cpu_share_enabled(), guided_settings()
    if generic is None:
        generic = len(names) > sfm.EXHAUSTIVE_UP_TO  # every pair is cheap enough up to there
    old_info = chunk.meta.get("sfm") or {}
    how = _continuation(run, reset=reset, quality=quality, keypoint_limit=keypoint_limit, apply_masks=apply_masks)
    if not reset and how is None:
        progress(0.0, "No alignment to continue: every photo again")
    # the size of the preselection images: the one the stores were made with when continuing them (the raster of a
    # JPEG decoded at a DCT scale depends on it), else the preselection's when it runs
    run.small_size = (old_info.get("preselection_size") if how == "stores"
                      else sfm.PRESELECTION_SIZE if generic else None)
    try:
        _begin(run, how, progress)
        with stages.stage("features"):
            run.masks = colmap_mask_folder(chunk, names, work / "masks") if apply_masks == "keypoints" else None
            features = _detect_features(run, progress, keypoint_limit, generic)
        pair_list, mode, cams = _select_pairs(run, stages, progress, generic, reference, neighbours)
        with stages.stage("pairs"):
            by_device, routes = _match_pairs(run, progress, pair_list, cams, mode, tiepoint_limit)
        with stages.stage("export"):
            removed = _commit(run, progress, exclude_stationary, old_info)
    except BaseException:
        _remove_leftovers(work)  # the work folder and the database being written; the stores moved aside put back
        raise
    _record(run, features=features, mode=mode, pairs=len(pair_list), by_device=by_device, routes=routes,
            removed=removed, old_info=old_info, quality=quality, keypoint_limit=keypoint_limit,
            tiepoint_limit=tiepoint_limit, generic=generic, reference=reference, neighbours=neighbours,
            apply_masks=apply_masks, guided_matching=guided_matching)
    progress(1.0, "Matching finished" + (f" ({removed:,} stationary matches left out)" if removed else ""))


def _features_from_pixels(k, root: Path, name: str, masks: Path | None, features_dir: Path, small_dir: Path | None,
                          limit: int, small_limit: int | None, keypoint_limit: int,
                          devices: list[int]) -> tuple[str | None, str | None]:
    """The features of a photo the core does not decode (a format outside JPEG, PNG and TIFF, or a photo too small
    for the kernels), from the pixels OpenCV reads - COLMAP's grey of the raster with the EXIF orientation ignored,
    as the classic GPU path reads them -: ``(who, None)`` with ``gpu`` or ``cpu`` for who extracted them, ``(None,
    why)`` when the kernels refuse the photo for its size, ``(None, None)`` when OpenCV does not read the file
    either."""
    import cv2

    img = cv2.imread(str(root / name), cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION)
    if img is None:
        return None, None
    if img.ndim == 3:
        img = cv2.transform(img, sfm._REC709_BGR)
    h, w = img.shape
    mask = str(masks / f"{name}.png") if masks is not None else None
    given = k.features_pixels(str(features_dir), str(small_dir) if small_dir is not None else None, name,
                              np.ascontiguousarray(img, np.uint8), (w, h), limit, small_limit, keypoint_limit,
                              sfm.PRESELECTION_POINTS, devices, mask=mask)
    for line in given["log"]:
        log.info("native features: %s", line)
    if given["done"] != 1:
        return None, ("; ".join(given["log"]) or "refused by the kernels") if given["unsupported"] else None
    return ("gpu" if given["gpu"] else "cpu"), None


# ----------------------------------------------------------------------------------- COLMAP's database
def _clear_features_and_matches(db_path: Path) -> None:
    """The key points, descriptors, matches and two-view geometries of a copied database gone: they are written
    again from the stores (COLMAP's documented sqlite layout, as :func:`.sfm.remove_stationary_matches` edits it)."""
    con = sqlite3.connect(str(db_path))
    try:
        for table in ("keypoints", "descriptors", "matches", "two_view_geometries"):
            con.execute(f"DELETE FROM {table}")  # noqa: S608 - a table name of COLMAP's layout
        con.commit()
    finally:
        con.close()


def _new_camera(db, sensor) -> tuple[int, int]:
    """A camera for ``sensor`` and the rig it is the reference sensor of, written to ``db`` as the classic GPU path
    writes them: ``(camera_id, rig_id)``."""
    import pycolmap

    model, params, width, height, _prior = _camera(sensor)
    camera = pycolmap.Camera(model=model, width=width, height=height, params=params)
    camera.has_prior_focal_length = True
    camera_id = db.write_camera(camera)
    rig = pycolmap.Rig()
    rig.add_ref_sensor(pycolmap.sensor_t(pycolmap.SensorType.CAMERA, camera_id))
    return camera_id, db.write_rig(rig)


def _new_image(db, name: str, camera_id: int, rig_id: int) -> int:
    """An image for the photo ``name`` and its frame in ``rig_id``, written to ``db``: the image id."""
    import pycolmap

    image_id = db.write_image(pycolmap.Image(name=name, camera_id=camera_id))
    frame = pycolmap.Frame()
    frame.rig_id = rig_id
    frame.add_data_id(pycolmap.data_t(pycolmap.sensor_t(pycolmap.SensorType.CAMERA, camera_id), image_id))
    db.write_frame(frame)
    return image_id


def _image_ids(db, chunk: Chunk, sensor_of: dict[str, int], held: set[str], continued: bool) -> dict[str, int]:
    """The image id of every photo to export (``held``), in ``db``: of a continued database (``continued``), the ids
    its images have, and a new image - with the camera and the rig of its sensor, or new ones - for each photo of
    ``held`` it lacks; of a fresh database, a camera and a rig per sensor and an image per photo, as the classic
    step writes them."""
    import pycolmap

    ids: dict[str, int] = {}
    if continued:
        images = {im.name: im for im in db.read_all_images()}
        camera_of_sensor = {sensor_of[n]: int(im.camera_id) for n, im in images.items() if n in sensor_of}
        rig_of_camera = {int(r.ref_sensor_id.id): int(r.rig_id) for r in db.read_all_rigs()}
        ids = {n: int(im.image_id) for n, im in images.items() if n in held}
        for name in sorted(held - set(images)):
            skey = sensor_of[name]
            if skey not in camera_of_sensor:  # a sensor new to the database: its camera and rig
                camera_of_sensor[skey], rig_id = _new_camera(db, chunk.sensors[skey])
                rig_of_camera[camera_of_sensor[skey]] = rig_id
            camera_id = camera_of_sensor[skey]
            if camera_id not in rig_of_camera:  # a camera of the database that is the reference sensor of no rig
                rig = pycolmap.Rig()
                rig.add_ref_sensor(pycolmap.sensor_t(pycolmap.SensorType.CAMERA, camera_id))
                rig_of_camera[camera_id] = db.write_rig(rig)
            ids[name] = _new_image(db, name, camera_id, rig_of_camera[camera_id])
        return ids
    by_sensor: dict[int, list[str]] = {}
    for name in sorted(held):
        by_sensor.setdefault(sensor_of[name], []).append(name)
    for skey, photos in by_sensor.items():
        camera_id, rig_id = _new_camera(db, chunk.sensors[skey])
        for name in photos:
            ids[name] = _new_image(db, name, camera_id, rig_id)
    return ids


def _two_view_geometry(r: dict):
    """COLMAP's two-view geometry of a pair of the match store: the configuration, the inlier matches, the models
    estimated and the relative pose when there is one."""
    import pycolmap

    tvg = pycolmap.TwoViewGeometry()
    tvg.config = pycolmap.TwoViewGeometryConfiguration(int(r["config"]))
    tvg.inlier_matches = np.ascontiguousarray(r["inliers"], np.uint32)
    for key in ("F", "E", "H"):
        if r[key] is not None:
            setattr(tvg, key, np.ascontiguousarray(r[key], np.float64))
    if r["qvec"] is not None:
        w, x, y, z = (float(v) for v in r["qvec"])
        tvg.cam2_from_cam1 = pycolmap.Rigid3d(pycolmap.Rotation3d(np.array([x, y, z, w])),
                                              np.asarray(r["tvec"], np.float64))
    return tvg


def export_database(chunk: Chunk, db_path: Path, descriptors: bool = False, *, stores: Path | None = None,
                    previous: Path | None = None, stationary_rule: bool = True,
                    progress: Progress | None = None) -> None:
    """COLMAP's database of the stores of the native matching (``stores``: ``<sfm>/native`` of the chunk when not
    given), written whole to ``db_path`` - a temporary file beside it, moved into place when complete -: the cameras
    by sensor as the classic step writes them (the model of :func:`.sfm.mapping_camera`, a prior focal length), a
    rig and a frame per photo, the key points as four columns, the matches and two-view geometries of every pair of
    the match store whose photos are in the database, the descriptors only when asked (the mapper needs none). The
    photos are those of the chunk that the feature store holds. ``previous`` (a database to continue; ``db_path``
    itself when it is there and none is given): copied first, its features and matches cleared and written again,
    so that its images, cameras, rigs, frames and pose priors keep their ids (the model of the alignment names them)
    and the photos of the store it lacks come after them, with the camera and the rig of their sensor; the stationary
    matches the step it continues left out are left out again (:func:`.sfm.remove_stationary_matches`, when the
    record of the chunk has a count and ``stationary_rule`` is not switched off by a caller that runs the removal
    itself). ``progress`` counts the pairs written (and stops the export by raising, :class:`Cancelled`); the
    temporary file goes whatever stops it. A chunk without the feature store is a FileNotFoundError."""
    import pycolmap

    progress = as_progress(progress)
    k = _kernels()
    db_path = Path(db_path)
    stores = Path(stores) if stores is not None else sfm._sfm_dir(chunk) / STORES
    if not (stores / FEATURES / INDEX).is_file():
        raise FileNotFoundError(f"{stores / FEATURES}: no feature store of the native matching")
    features = k.feature_store(str(stores / FEATURES))
    matches = k.match_store(str(stores / MATCHES)) if (stores / MATCHES / INDEX).is_file() else None
    _root, names = sfm.image_root_and_names(chunk)
    sensor_of = {names[c.key]: c.sensor_key for c in chunk.cameras if c.key in names}
    held = set(features.names()) & set(names.values())
    tmp = db_path.with_name(db_path.name + ".tmp")

    def remove_tmp():
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(str(tmp) + suffix).unlink(missing_ok=True)

    remove_tmp()
    if previous is None and db_path.exists():
        previous = db_path
    continued = previous is not None and Path(previous).exists()
    try:
        if continued:
            shutil.copyfile(previous, tmp)
            _clear_features_and_matches(tmp)
        db = pycolmap.Database.open(str(tmp))
        try:
            with pycolmap.DatabaseTransaction(db):
                ids = _image_ids(db, chunk, sensor_of, held, continued)
                for name, image_id in ids.items():
                    kp, desc, _implementation = features.read(name, descriptors)
                    db.write_keypoints(image_id, np.ascontiguousarray(kp, np.float32))
                    if descriptors:
                        db.write_descriptors(image_id, pycolmap.FeatureDescriptors(
                            pycolmap.FeatureExtractorType.SIFT, np.ascontiguousarray(desc, np.uint8)))
                pairs = [(a, b) for a, b in matches.pairs() if a in ids and b in ids] if matches is not None else []
                for i, (a, b) in enumerate(pairs):
                    if i % EXPORT_EVERY == 0:
                        progress(i / len(pairs), f"{PROGRESS_EXPORT}: {i:,} of {len(pairs):,} pairs")
                    r = matches.read(a, b)
                    if r is None:
                        continue
                    db.write_matches(ids[a], ids[b], np.ascontiguousarray(r["matches"], np.uint32))
                    db.write_two_view_geometry(ids[a], ids[b], _two_view_geometry(r))
                progress(1.0, f"{PROGRESS_EXPORT}: {len(pairs):,} of {len(pairs):,} pairs")
        finally:
            db.close()
        if continued and stationary_rule and (chunk.meta.get("sfm") or {}).get("stationary_removed") is not None:
            sfm.remove_stationary_matches(tmp)
        os.replace(tmp, db_path)
    except BaseException:
        remove_tmp()
        raise


def _four_columns(kp: np.ndarray) -> np.ndarray:
    """COLMAP's key points as the four columns of the store: the six of its extractor (x, y and the affine shape
    a11, a12, a21, a22) become ``(x, y, sqrt(a11^2 + a21^2), atan2(a21, a11))``; four are kept; two get a scale of 1
    and no orientation."""
    kp = np.asarray(kp, np.float32)
    if kp.shape[1] == 6:
        a11, a21 = kp[:, 2], kp[:, 4]
        return np.column_stack([kp[:, 0], kp[:, 1], np.hypot(a11, a21), np.arctan2(a21, a11)]).astype(np.float32)
    if kp.shape[1] >= 4:
        return np.ascontiguousarray(kp[:, :4])
    return np.column_stack([kp[:, 0], kp[:, 1], np.ones(len(kp), np.float32), np.zeros(len(kp), np.float32)])


def _blob(data, count: int) -> np.ndarray | None:
    """A float64 blob of ``count`` numbers of COLMAP's two-view geometries, or None when it is NULL or empty."""
    if data is None or len(data) < 8 * count:
        return None
    return np.frombuffer(bytes(data), np.float64, count).copy()


def import_database(db_path: Path, features_dir: Path, matches_dir: Path, progress: Progress | None = None) -> int:
    """The features and the matches of COLMAP's database ``db_path`` (a classic matching) into the feature store
    ``features_dir`` and the match store ``matches_dir`` (made when missing): every photo with key points and
    descriptors, its key points as four columns (:func:`_four_columns`) and COLMAP's image id in its header, as
    ``imported``; every pair of the two-view geometries (and of the matches) of two photos imported, with its
    matches, inliers, configuration, models and pose, as ``imported`` - a pair of a photo left out (no descriptors)
    is left out too, since its matches index key points the stores do not hold. The pairs are read one at a time
    (the database of a large block does not fit in memory); ``progress`` counts them (and stops the import by
    raising, :class:`Cancelled`). The names a classic matching that died left aliased are put back first
    (:func:`.sfm._interrupted_names_back`); the database is read read-only. Returns the photos imported."""
    progress = as_progress(progress)
    k = _kernels()
    db_path = Path(db_path)
    sfm._interrupted_names_back(db_path)
    features = k.feature_store(str(features_dir))
    matches = k.match_store(str(matches_dir))
    empty = np.zeros((0, 2), np.uint32)

    def array(rows, cols, data) -> np.ndarray:
        return np.frombuffer(bytes(data), np.uint32).reshape(int(rows), int(cols)) if rows and data else empty

    con = sqlite3.connect(sfm.read_only_uri(db_path), uri=True)
    try:
        images = {int(i): (str(n), int(c)) for i, n, c in con.execute("SELECT image_id, name, camera_id FROM images")}
        sizes = {int(c): (int(w), int(h)) for c, w, h in con.execute("SELECT camera_id, width, height FROM cameras")}
        without_descriptors = 0
        imported: dict[int, str] = {}
        for image_id, rows, cols, data in con.execute("SELECT image_id, rows, cols, data FROM keypoints"):
            if image_id not in images or not rows or not data:
                continue
            row = con.execute("SELECT rows, cols, data FROM descriptors WHERE image_id = ?", (image_id,)).fetchone()
            if row is None or not row[0] or not row[2]:
                without_descriptors += 1  # a database the native step exported for the mapper: nothing to match with
                continue
            kp = _four_columns(np.frombuffer(bytes(data), np.float32).reshape(int(rows), int(cols)))
            desc = np.frombuffer(bytes(row[2]), np.uint8).reshape(int(row[0]), int(row[1]))
            if len(desc) != len(kp):
                log.warning("%s: %d key points and %d descriptors in the database: not imported", images[image_id][0],
                            len(kp), len(desc))
                continue
            name, camera_id = images[image_id]
            width, height = sizes.get(camera_id, (0, 0))
            features.write(name, kp, np.ascontiguousarray(desc), width, height, int(image_id))
            imported[int(image_id)] = name
        total = int(con.execute("SELECT (SELECT COUNT(*) FROM two_view_geometries) + (SELECT COUNT(*) FROM matches "
                                "WHERE pair_id NOT IN (SELECT pair_id FROM two_view_geometries))").fetchone()[0])
        # the pairs with a two-view geometry and their matches, then the pairs with matches alone
        with_geometry = con.execute(
            "SELECT g.pair_id, m.rows, m.cols, m.data, g.rows, g.cols, g.data, g.config, g.F, g.E, g.H, g.qvec, "
            "g.tvec FROM two_view_geometries g LEFT JOIN matches m ON m.pair_id = g.pair_id ORDER BY g.pair_id")
        rows = ((pair_id, array(mr, mc, md), array(gr, gc, gd), int(config), _blob(f, 9), _blob(e, 9), _blob(h, 9),
                 _blob(qvec, 4), _blob(tvec, 3))
                for pair_id, mr, mc, md, gr, gc, gd, config, f, e, h, qvec, tvec in with_geometry)
        alone = con.cursor().execute("SELECT pair_id, rows, cols, data FROM matches WHERE pair_id NOT IN "
                                     "(SELECT pair_id FROM two_view_geometries) ORDER BY pair_id")
        rows_alone = ((pair_id, array(mr, mc, md), empty, 0, None, None, None, None, None)
                      for pair_id, mr, mc, md in alone)
        unknown_configs = skipped = 0
        for i, (pair_id, raw, inliers, config, f, e, h, qvec, tvec) in enumerate(itertools.chain(rows, rows_alone)):
            if i % EXPORT_EVERY == 0:
                progress(0.0, f"Importing the matches of the alignment: {i:,} of {total:,} pairs")
            id1, id2 = divmod(int(pair_id), sfm.COLMAP_MAX_IMAGES)
            if id1 not in imported or id2 not in imported:
                skipped += 1
                continue
            if config > 7:  # MULTIPLE, CALIBRATED_RIG: not configurations the verification gives; the inliers count
                unknown_configs += 1
                config = 0
            pose = (qvec is not None and tvec is not None
                    and not (np.allclose(qvec, [1, 0, 0, 0]) and np.allclose(tvec, 0)))
            matches.write(imported[id1], imported[id2], np.ascontiguousarray(raw), np.ascontiguousarray(inliers),
                          config, F=None if f is None or not f.any() else f.reshape(3, 3),
                          E=None if e is None or not e.any() else e.reshape(3, 3),
                          H=None if h is None or not h.any() else h.reshape(3, 3),
                          qvec=qvec if pose else None, tvec=tvec if pose else None)
    finally:
        con.close()
    matches.flush()
    if without_descriptors:
        log.warning("%d photos of the database have key points but no descriptors: not imported, their points are "
                    "detected again", without_descriptors)
    if skipped:
        log.warning("%d pairs of the database with a photo that was not imported: left out", skipped)
    if unknown_configs:
        log.warning("%d pairs of the database had a configuration the native engine does not give: kept as undefined, "
                    "with their inliers", unknown_configs)
    return len(imported)
