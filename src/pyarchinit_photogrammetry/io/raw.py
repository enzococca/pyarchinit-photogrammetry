"""Camera raw files (DNG, CR2/CR3, NEF, ARW, RAF, ORF, RW2, PEF...) as photos.

A raw file is no image the libraries of the pipeline read: PIL and COLMAP open its thumbnail or its embedded
preview (another size), OpenCV nothing. So a raw is *developed* once into an ordinary image inside the chunk folder
(``<project>.ppg.files/chunk_N/raw/``) and the camera points to that copy - as the reference copies of the
multispectral captures do - while the raw stays where it is, named in ``camera.meta["raw"]``. Every reader then
works unchanged. This module is the one place that knows about raws: extensions, the optional decoder (rawpy,
LibRaw), their EXIF, the development and its cache.

The development is the same for every frame: white balance of the camera, no automatic brightness, sRGB, 8 bit,
the pixels as the sensor read them (no rotation: one sensor model for frames shot upright and across), the full
size (never the embedded preview), one exposure shift and one tone for all the frames of a camera.

The tone (:data:`TONES`) is there for the alignment, not for the look: a linear development has far less contrast
than the JPEG a camera writes, and the key points are found by their contrast. Measured on raws of four cameras,
the linear copies gave from a third to a sixth of the key points of the camera JPEGs; the curve of "camera" gives
about as many. The raws of phones that are no sensor mosaic (linear DNG: Apple ProRAW) are flatter still - the
phone completes them with a local tone map stored in the file, which LibRaw does not apply: eight such frames
that align from their JPEGs did not align with the curve alone, and do with the local contrast of "local". On
the raws of the other cameras "local" is too much (harsh textures), hence "auto". No tone moves a pixel.
"""
from __future__ import annotations

import importlib.util
import json
import os
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Iterable

from ..core.progress import ProgressCallback, as_progress

# what LibRaw develops, by the extensions cameras write (".raw" is left out: too many other files carry it)
RAW_EXTENSIONS = frozenset({
    ".dng", ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".raf", ".orf", ".rw2", ".pef",
    ".srw", ".3fr", ".fff", ".iiq", ".erf", ".kdc", ".dcr", ".mrw", ".mos", ".mef", ".rwl", ".x3f"})
FORMATS = ("jpg", "tif")
# the development of a raw: file of the copy, JPEG quality, exposure shift in EV (the same for every frame),
# half the size (one pixel per 2x2 cell of the sensor: four times less disk and time)
# the tone: "camera" the contrast curve of a camera JPEG (_tone_table), "local" the curve and the contrast evened
# out over the frame (_local_contrast), "linear" neither; "auto" is "local" for a raw that is no sensor mosaic
# (the linear DNG of the phones), "camera" for the others
TONES = ("auto", "camera", "local", "linear")
DEFAULTS = {"format": "jpg", "quality": 95, "exposure": 1.0, "tone": "auto", "half_size": False}
_CURVE = (6.0, 0.4)  # the curve of "camera": the steepness of a logistic curve and the level it turns around
_LOCAL = (3.0, 8)  # the local contrast: the limit of the equalisation and the tiles per side
FOLDER = "raw"  # in the chunk folder
INDEX = "index.json"
PREFER = ("jpeg", "raw")  # which file of a raw + JPEG pair becomes the photo


class RawUnavailable(RuntimeError):
    """A raw file was given and the decoder (rawpy) is not installed."""


def is_raw(path: str | os.PathLike) -> bool:
    return Path(path).suffix.lower() in RAW_EXTENSIONS


def available() -> bool:
    """rawpy is installed (looked up, not imported)."""
    return importlib.util.find_spec("rawpy") is not None


def unavailable_message() -> str:
    from ..gui.i18n import tr

    return tr("I file raw delle fotocamere (DNG, CR2, NEF, ARW...) richiedono il modulo rawpy, che non è "
              "installato: pip install \"pyarchinit-photogrammetry[raw]\". In alternativa esportare le foto in "
              "JPEG o TIFF.")


def _rawpy():
    if not available():
        raise RawUnavailable(unavailable_message())
    import rawpy

    return rawpy


def version() -> str | None:
    """``"rawpy 0.28.0, LibRaw 0.22.2"`` or ``None`` without rawpy."""
    if not available():
        return None
    try:
        import rawpy

        return f"rawpy {rawpy.__version__}, LibRaw {'.'.join(map(str, rawpy.libraw_version))}"
    except Exception as exc:  # installed and broken
        return f"rawpy: {exc}"


def settings(overrides: dict | None = None) -> dict:
    """:data:`DEFAULTS` with ``overrides``, checked (ValueError on a key or a value that is not one of them)."""
    out = dict(DEFAULTS)
    for key, value in (overrides or {}).items():
        if key not in DEFAULTS:
            raise ValueError(f"raw development: unknown setting {key!r} (one of {', '.join(DEFAULTS)})")
        if value is not None:
            out[key] = value
    out["format"] = str(out["format"]).lower().lstrip(".").replace("jpeg", "jpg").replace("tiff", "tif")
    if out["format"] not in FORMATS:
        raise ValueError(f"raw development: format must be one of {', '.join(FORMATS)}")
    out["quality"] = int(out["quality"])
    if not 50 <= out["quality"] <= 100:
        raise ValueError("raw development: quality between 50 and 100")
    out["exposure"] = round(float(out["exposure"]), 3)
    if not -2.0 <= out["exposure"] <= 3.0:
        raise ValueError("raw development: exposure between -2 and +3 EV")
    out["tone"] = str(out["tone"]).lower()
    if out["tone"] not in TONES:
        raise ValueError(f"raw development: tone must be one of {', '.join(TONES)}")
    out["half_size"] = bool(out["half_size"])
    return out


def _tone_table():
    """The curve of the tone "camera" as a table from the 16-bit values of the development to the 8 bits of the
    copy: a logistic curve between black and white, steeper in the middle tones (more contrast there, less in
    the deep shadows and the highlights), the same for every frame."""
    import numpy as np

    k, pivot = _CURVE
    v = np.arange(65536, dtype=np.float64) / 65535.0

    def f(x):
        return 1.0 / (1.0 + np.exp(-k * (x - pivot)))

    return np.clip((f(v) - f(0.0)) / (f(1.0) - f(0.0)) * 255.0 + 0.5, 0, 255).astype(np.uint8)


def _local_contrast(rgb):
    """``rgb`` (8 bit) with its lightness equalised tile by tile under a limit (CLAHE), the colours as they were:
    the detail of the flat areas comes out as the tone map of a phone brings it out. A value is replaced by
    another in its place: nothing moves."""
    import cv2

    clip, tiles = _LOCAL
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB)
    lab[..., 0] = cv2.createCLAHE(clipLimit=clip, tileGridSize=(tiles, tiles)).apply(lab[..., 0])
    return cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)


# ------------------------------------------------------------------ pairs
def photo_files(paths: Iterable[str | os.PathLike], extensions, recursive: bool = False,
                prefer: str = "jpeg") -> tuple[list[Path], list[Path]]:
    """The files ``paths`` (files, or folders searched for ``extensions`` and the raw ones) name, one per shot:
    of a raw and an image with the same name in one folder (what a camera set to raw + JPEG writes) only the one
    ``prefer`` says stays. Returns ``(files, left out)``."""
    if prefer not in PREFER:
        raise ValueError(f"prefer must be one of {', '.join(PREFER)}, not {prefer!r}")
    files: list[Path] = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            it = p.rglob("*") if recursive else p.iterdir()
            # hidden files are not photos: macOS leaves an AppleDouble "._name.JPG" beside each photo on exFAT
            files += sorted(f for f in it if (f.suffix.lower() in extensions or is_raw(f)) and not f.name.startswith("."))
        else:
            files.append(p)
    shots: dict[tuple, dict[bool, Path]] = {}
    for f in files:
        shots.setdefault(shot_key(f), {}).setdefault(is_raw(f), f)
    keep_raw = prefer == "raw"
    out, left = [], []
    seen = set()
    for f in files:
        if f in seen:
            continue
        seen.add(f)
        pair = shots[shot_key(f)]
        if len(pair) == 2 and is_raw(f) != keep_raw:
            left.append(f)
        else:
            out.append(f)
    return out, left


def shot_key(path: str | os.PathLike) -> tuple[str, str]:
    """What tells a shot apart: its folder and its name without the extension (case ignored)."""
    p = Path(path)
    return (os.path.normcase(str(p.parent.resolve())), p.stem.lower())


# ------------------------------------------------------------------- EXIF
def read_raw_exif(path: str | os.PathLike) -> dict:
    """The fields of :func:`..exif.read_exif` read from a raw file, with ``orientation`` (the EXIF value) and
    without ``width`` and ``height`` (those are the developed photo's). The raws built on TIFF (DNG, CR2, NEF,
    ARW, PEF...) are read as such; for the others the EXIF of the embedded preview and what LibRaw says."""
    from PIL import Image

    from .exif import exif_fields

    info: dict = {}
    try:
        with open(path, "rb") as fp:
            if fp.read(4) in (b"II*\x00", b"MM\x00*"):
                fp.seek(0)
                exif = Image.Exif()
                exif.load_from_fp(fp)
                info = exif_fields(exif)
    except Exception:
        info = {}
    if (not info.get("make") or not info.get("focal_length_mm")) and available():
        for key, value in _libraw_fields(path).items():
            info.setdefault(key, value)
            if info[key] is None:
                info[key] = value
    return info


def _libraw_fields(path) -> dict:
    """What the embedded preview's EXIF and LibRaw's own reading say of a raw that is no TIFF (RAF, CR3...)."""
    import io

    from PIL import Image

    from .exif import exif_fields

    out: dict = {}
    rawpy = _rawpy()
    try:
        with rawpy.imread(str(path)) as raw:
            try:
                thumb = raw.extract_thumb()
                if thumb.format == rawpy.ThumbFormat.JPEG:
                    with Image.open(io.BytesIO(thumb.data)) as im:
                        out = exif_fields(im.getexif())
            except Exception:
                pass
            other, lens = getattr(raw, "other", None), getattr(raw, "lens", None)
            if other is not None:
                if getattr(other, "focal_length", 0):
                    out.setdefault("focal_length_mm", round(float(other.focal_length), 3))
                stamp = getattr(other, "timestamp", None)
                if stamp is not None and not out.get("datetime"):
                    out["datetime"] = stamp.strftime("%Y:%m:%d %H:%M:%S")
            if lens is not None and getattr(lens, "model", "") and not out.get("lens"):
                out["lens"] = lens.model
    except Exception:
        pass
    out.pop("width", None)
    out.pop("height", None)
    return out


def _exif_bytes(info: dict):
    """The EXIF of a developed copy: body, lens, focal lengths, time and GPS of the raw. No orientation: the
    pixels are the sensor's and every reader must see them the same way."""
    from PIL import Image

    exif = Image.Exif()
    if info.get("make"):
        exif[0x010F] = str(info["make"])
    if info.get("model"):
        exif[0x0110] = str(info["model"])
    if info.get("datetime"):
        exif[0x0132] = str(info["datetime"])
    sub = exif.get_ifd(0x8769)
    if info.get("datetime"):
        sub[0x9003] = str(info["datetime"])
    if info.get("focal_length_mm"):
        sub[0x920A] = float(info["focal_length_mm"])
    if info.get("focal_length_35mm"):
        sub[0xA405] = int(round(info["focal_length_35mm"]))
    if info.get("lens"):
        sub[0xA434] = str(info["lens"])
    if info.get("gps"):
        lon, lat, alt = info["gps"]
        gps = exif.get_ifd(0x8825)
        gps[1], gps[2] = ("N" if lat >= 0 else "S"), _dms(lat)
        gps[3], gps[4] = ("E" if lon >= 0 else "W"), _dms(lon)
        gps[5], gps[6] = (b"\x00" if alt >= 0 else b"\x01"), abs(float(alt))
    return exif


def _dms(value: float) -> tuple[float, float, float]:
    seconds = round(abs(value) * 3600, 6)
    d, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return (d, m, s)


# ------------------------------------------------------------ development
def develop_file(source: str | os.PathLike, target: str | os.PathLike, options: dict | None = None,
                 info: dict | None = None) -> tuple[int, int]:
    """Develop the raw ``source`` into the image ``target`` (see the module docstring) and return its
    ``(width, height)``. ``info``: the EXIF fields written into the copy (default: read from the raw). The file
    appears whole or not at all. A ``ValueError`` names a file LibRaw cannot read."""
    from PIL import Image

    rawpy = _rawpy()
    opts = settings(options)
    source, target = Path(source), Path(target)
    gain = 2.0 ** opts["exposure"]
    tone = opts["tone"]
    try:
        with rawpy.imread(str(source)) as raw:
            if tone == "auto":  # no mosaic: a raw the phone has already developed, flat until its tone map
                tone = "local" if raw.raw_type != rawpy.RawType.Flat else "camera"
            curve = tone != "linear"
            rgb = raw.postprocess(use_camera_wb=True, no_auto_bright=True, output_bps=16 if curve else 8,
                                  user_flip=0, output_color=rawpy.ColorSpace.sRGB, half_size=opts["half_size"],
                                  exp_shift=gain, exp_preserve_highlights=1.0 if gain > 1.0 else 0.0)
    except rawpy.LibRawError as exc:
        from ..gui.i18n import tr

        raise ValueError(tr("File raw non leggibile: {0} ({1})", source.name, exc)) from exc
    if curve:  # from the 16 bits of the development: no steps in the tones the curve spreads
        rgb = _tone_table()[rgb]
    if tone == "local":
        rgb = _local_contrast(rgb)
    exif = _exif_bytes(read_raw_exif(source) if info is None else info)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    image = Image.fromarray(rgb)
    try:
        if opts["format"] == "jpg":
            image.save(part, "JPEG", quality=opts["quality"], exif=exif)
        else:
            image.save(part, "TIFF", compression="tiff_adobe_deflate", exif=exif)
        os.replace(part, target)
    finally:
        part.unlink(missing_ok=True)
    return int(rgb.shape[1]), int(rgb.shape[0])


def stamp(source: str | os.PathLike) -> dict:
    """What tells whether a raw is still the file that was developed: its size and its time (whole seconds)."""
    st = os.stat(source)
    return {"size": int(st.st_size), "mtime": int(st.st_mtime)}


def _read_index(folder: Path) -> dict:
    try:
        data = json.loads((folder / INDEX).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_index(folder: Path, index: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    part = folder / (INDEX + ".part")
    part.write_text(json.dumps(index, indent=1), encoding="utf-8")
    os.replace(part, folder / INDEX)


def _same(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _name_for(source: Path, suffix: str, index: dict, taken: set[str], folder: Path) -> str:
    """The name of the copy of ``source``: the one it has in the index, else its own name with the extension of
    the copies; with the folder's name, then a number, when another raw has it (or a file the index does not
    know is there: the copies of a duplicated chunk)."""
    for name, entry in index.items():
        if _same(entry.get("source", ""), str(source)) and name.endswith(suffix) and name not in taken:
            return name
    stems = [source.stem, f"{source.parent.name}_{source.stem}"]
    k = 2
    while True:
        for stem in stems:
            name = stem + suffix
            if name not in taken and name not in index and not (folder / name).exists():
                return name
        stems = [f"{source.parent.name}_{source.stem}_{k}"]
        k += 1


def workers_default() -> int:
    return max(1, min(4, (os.cpu_count() or 2) // 2))


def develop(folder: str | os.PathLike, sources: Iterable[str | os.PathLike], options: dict | None = None,
            progress: ProgressCallback | None = None, workers: int | None = None, force: bool = False,
            names: dict | None = None) -> dict[Path, dict]:
    """Develop the raws ``sources`` into ``folder`` (the ``raw`` folder of a chunk), several at a time, and return
    for each ``{"path", "width", "height", "info", "record"}``: the copy, its size, the EXIF fields of the raw and
    what ``camera.meta["raw"]`` keeps (source, size, time, settings). A copy made before from the same file (size
    and time) with the same settings is kept (``force``: made again). ``names``: the file name to give to the
    copy of a source (the one a camera already points to). Cancelling stops between files; the copies finished
    stay and are found again."""
    from PIL import Image

    _rawpy()
    opts = settings(options)
    progress = as_progress(progress)
    folder = Path(folder)
    sources = [Path(s) for s in sources]
    index = {name: entry for name, entry in _read_index(folder).items() if (folder / name).is_file()}
    suffix = "." + opts["format"]
    taken: set[str] = set()
    plan = []
    for src in sources:
        if not src.is_file() or src.stat().st_size == 0:
            raise FileNotFoundError(str(src))
        name = (names or {}).get(src) or _name_for(src, suffix, index, taken, folder)
        taken.add(name)
        plan.append((src, name, {"source": str(src), **stamp(src), "settings": opts}))
    out: dict[Path, dict] = {}

    def one(src: Path, name: str, record: dict) -> dict:
        info = read_raw_exif(src)
        target = folder / name
        # the copy of this name is of this raw (the index or the camera says so): a raw that was only moved, with
        # its size and time, keeps its copy
        entry = {k: v for k, v in (index.get(name) or {}).items() if k != "source"}
        if (not force and entry == {k: v for k, v in record.items() if k != "source"} and target.is_file()
                and target.stat().st_size > 0):
            with Image.open(target) as im:
                width, height = im.size
        else:
            width, height = develop_file(src, target, opts, info)
        return {"path": target, "width": width, "height": height, "info": info,
                "record": {"path": record["source"], "size": record["size"], "mtime": record["mtime"],
                           "settings": dict(opts)}}

    from ..gui.i18n import tr

    done = 0
    pool = ThreadPoolExecutor(max_workers=workers or workers_default(), thread_name_prefix="ppg-raw")
    try:
        progress(0.0, tr("Sviluppo dei file raw: {0}", len(plan)))
        pending = {pool.submit(one, *item): item for item in plan}
        while pending:
            finished, _ = wait(list(pending), timeout=0.2, return_when=FIRST_COMPLETED)
            progress.check()
            for fut in finished:
                src, name, record = pending.pop(fut)
                out[src] = fut.result()
                index[name] = record
                done += 1
                told = done == len(plan) or done % max(1, len(plan) // 10) == 0  # every message goes to the log
                progress(done / len(plan), tr("Sviluppo dei file raw: {0} di {1}", done, len(plan)) if told else "")
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        for fut, (_src, name, record) in pending.items():  # cancelled or failed: the files that finished meanwhile
            if fut.done() and not fut.cancelled() and fut.exception() is None:
                index[name] = record
                done += 1
        if done:
            _write_index(folder, index)
    return {src: out[src] for src in sources if src in out}  # in the order asked, not the one they finished in
