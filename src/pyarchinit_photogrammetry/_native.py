"""Optional native kernels: the ``ppg_core`` extension, written in Rust (``native/`` in the
repository, package ``pyarchinit-photogrammetry-core``).

Every caller keeps its numpy code: it runs when the extension is missing, older than the
function needed, or switched off with ``PPG_NATIVE=0``, and the tests compare the two.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

_module = None
_loaded = False


def _core():
    global _module, _loaded
    if not _loaded:
        _loaded = True
        try:
            import ppg_core
        except ImportError:
            ppg_core = None
        _module = ppg_core
    return _module


def enabled() -> bool:
    return os.environ.get("PPG_NATIVE", "1").strip().lower() not in ("0", "false", "no", "off")


def kernel(name: str):
    """The native function ``name``, or ``None`` (extension missing, without it, or switched off)."""
    core = _core() if enabled() else None
    return getattr(core, name, None) if core is not None else None


# what the extension can do besides the camera models, asked for by name in supports()
FEATURES = {
    "ppk": "ppk_solve",  # RTKLIB's post-processing (API 8)
    "crinex": "crx2rnx",  # Hatanaka-compressed RINEX decoded (API 9)
    "tin": "tin_triangulate",  # constrained Delaunay triangulation and the TIN kernels (API 10)
    "convbin": "raw_to_rinex",  # raw receiver logs converted to RINEX by RTKLIB's convbin (API 11)
}
TIN_API = 10  # tin_triangulate, tin_rasterize, tin_contours, tin_volume
CONVBIN_API = 11  # raw_to_rinex, convert_cancel


def supports(model: str) -> bool:
    """The projection kernels know the camera model ``model`` (a COLMAP model name), or the
    extension has the feature ``model`` of :data:`FEATURES` (``"ppk"``: RTKLIB's post-processing,
    API 8; ``"crinex"``: Hatanaka-compressed RINEX decoded, API 9; ``"tin"``: the TIN kernels, API 10;
    ``"convbin"``: raw receiver logs converted to RINEX, API 11)."""
    core = _core() if enabled() else None
    if core is None:
        return False
    if model == "tin":
        return tin_kernel("tin_triangulate") is not None
    if model in FEATURES:
        return hasattr(core, FEATURES[model])
    return model in core.supported_models()


def has(feature: str) -> bool:
    """The installed extension carries the feature ``feature`` of :data:`FEATURES` (``"ppk"``, ``"crinex"``),
    whether or not ``PPG_NATIVE`` switches it off: what a build holds (the smoke test of the
    packaged application), where :func:`supports` says what the program uses."""
    core = _core()
    return core is not None and feature in FEATURES and hasattr(core, FEATURES[feature])


def tin_kernel(name: str):
    """The TIN kernel ``name`` (``tin_triangulate``, ``tin_rasterize``, ``tin_contours``, ``tin_volume``)
    of an extension of API 10 or later, or ``None``: the caller runs its numpy code."""
    fn = kernel(name)
    if fn is None or (api() or 0) < TIN_API:
        return None
    return fn


def version() -> str | None:
    core = _core()
    return getattr(core, "__version__", None) if core is not None else None


def api() -> int | None:
    """The API level of the extension (what its functions accept), or ``None`` without it."""
    core = _core() if enabled() else None
    return getattr(core, "API", None) if core is not None else None


# ------------------------------------------------------------------------------- the GPU
def gpu_setting() -> str:
    """``PPG_GPU``: ``auto`` (the best GPU), ``off``, the index of an adapter or part of its name."""
    return os.environ.get("PPG_GPU", "auto").strip() or "auto"


def gpu_adapters() -> list[dict]:
    """The GPUs wgpu finds (``index``, ``name``, ``backend``, ``device_type``), [] without the
    extension (API 5) or with the GPU switched off."""
    core = _core() if enabled() and gpu_setting().lower() != "off" else None
    fn = getattr(core, "gpu_adapters", None) if core is not None else None
    if fn is None:
        return []
    try:
        return [dict(a) for a in fn()]
    except Exception:  # pragma: no cover - a broken driver
        return []


def gpu_device(setting: str | None = None) -> int | None:
    """The adapter index for ``setting`` (default :func:`gpu_setting`): -1 for ``auto``, None
    when the GPU is off, unknown or not there."""
    s = (gpu_setting() if setting is None else setting).strip()
    if s.lower() in ("off", "none", "no", "false", ""):
        return None
    adapters = gpu_adapters()
    if not adapters:
        return None
    if s.lower() == "auto":  # a software rasteriser (lavapipe, WARP) is no GPU: only when asked for by name or index
        return -1 if any(a.get("device_type") != "Cpu" for a in adapters) else None
    if s.isdigit() and int(s) < len(adapters):
        return int(s)
    for a in adapters:
        if s.lower() in str(a.get("name", "")).lower():
            return int(a["index"])
    return None


def gpu_devices(setting: str | None = None) -> list[int]:
    """The adapters to work with for ``setting``: ``all`` every GPU of the computer (not the
    software rasterisers), ``auto`` the best one ([-1]), an index or a name one, ``off`` none."""
    s = (gpu_setting() if setting is None else setting).strip()
    if s.lower() == "all":
        return [int(a["index"]) for a in gpu_adapters() if a.get("device_type") != "Cpu"]
    d = gpu_device(s)
    return [] if d is None else [d]


def gpu_match(a, b, max_ratio: float = 0.8, max_distance: float = 0.7, cross_check: bool = True,
              device: int = -1, a_key: int = 0, b_key: int = 0):
    """SIFT matches of two sets of uint8 descriptors (n x 128) on the GPU, as COLMAP's matcher
    picks them: the (m, 2) uint32 index pairs. ``a_key`` / ``b_key`` keep the descriptors on the
    GPU for the next pairs. Raises RuntimeError when no GPU works."""
    core = _core()
    if core is None or not hasattr(core, "gpu_match"):
        raise RuntimeError("the native core with the GPU kernels (API 5) is not installed")
    return core.gpu_match(a, b, max_ratio, max_distance, cross_check, device, a_key, b_key)


def gpu_forget() -> None:
    """Drop what the GPU matchers keep (descriptors, pipelines)."""
    core = _core()
    fn = getattr(core, "gpu_forget", None) if core is not None else None
    if fn is not None:
        fn()


def gpu_fuse(reference, neighbours, min_pixels: int = 3, max_reproj: float = 2.0, max_depth_rel: float = 0.01,
             max_normal_deg: float = 20.0, support_tol: float = 0.01, device: int = -1):
    """The dense points of one depth map fused with its covisible maps on the GPU (API 7): every
    map ``(key, depth (h x w float32) or None when the GPU holds it, (fx, fy, cx, cy), rotation
    3x3, translation, order)``; returns ``(xyz, normals, pixel, pixels, support)``."""
    fn = kernel("gpu_fuse")
    if fn is None:
        raise RuntimeError("the GPU fusion needs pyarchinit-photogrammetry-core 0.4 (API 7)")
    return fn(reference, neighbours, int(min_pixels), float(max_reproj), float(max_depth_rel), float(max_normal_deg),
              float(support_tol), int(device))


def gpu_fuse_cached(device: int = -1) -> list[int]:
    """The keys of the depth maps the GPU keeps for the fusion."""
    fn = kernel("gpu_fuse_cached")
    return list(fn(int(device))) if fn is not None else []


def gpu_fuse_forget(device: int = -1) -> None:
    """Drop the depth maps the GPU keeps for the fusion: their keys are image ids, unique within
    one workspace only."""
    fn = kernel("gpu_fuse_forget")
    if fn is not None:
        fn(int(device))


def gpu_sift(image, max_features: int = 8192, first_octave: int = -1, num_octaves: int = 4, octave_resolution: int = 3,
             peak_threshold: float = 0.02 / 3.0, edge_threshold: float = 10.0, max_orientations: int = 2,
             upright: bool = False, device: int = -1):
    """SIFT key points ``(n, 4)`` float32 (x, y, scale, orientation; the centre of the first pixel
    at (0, 0), as VLFeat) and descriptors ``(n, 128)`` uint8 of a grey float image in [0, 1] on
    the GPU, as COLMAP's CPU extractor computes them (API 6 of the extension)."""
    fn = kernel("gpu_sift")
    if fn is None:
        raise RuntimeError("the GPU SIFT needs pyarchinit-photogrammetry-core 0.4 (API 6)")
    return fn(image, int(max_features), int(first_octave), int(num_octaves), int(octave_resolution),
              float(peak_threshold), float(edge_threshold), int(max_orientations), bool(upright), int(device))


# ------------------------------------------------------------------ RTKLIB's post-processing
PPK_MISSING = ("PPK needs RTKLIB: install the native core pyarchinit-photogrammetry-core 0.5 or later "
               "(pip install \"pyarchinit-photogrammetry-core>=0.5\"), or set PPG_RNX2RTKP to the rnx2rtkp "
               "program of RTKLIB-EX 2.5 (rtklibexplorer)")

# what RTKLIB decompresses by the extension of an input, with gzip, tar or crx2rnx through a shell
# command (rtk_uncompress): a fork of the program, and the result written beside the user's file
COMPRESSED = (".z", ".gz", ".zip", ".tar", ".crx")


def compressed_name(path) -> bool:
    """RTKLIB would decompress ``path`` through a shell command: the text after the last dot of
    the whole path, as RTKLIB reads it, is .z, .gz, .zip, .tar or .crx (any case), or has a d as
    its third character (Hatanaka RINEX 2: .25d)."""
    text = str(path)
    dot = text.rfind(".")
    if dot < 0:
        return False
    ext = text[dot:]
    return ext.lower() in COMPRESSED or (len(ext) > 3 and ext[3] in "dD")


def ppk_available() -> bool:
    """:func:`ppk_solve` has an RTKLIB to run: the native core (API 8), or the program named by
    ``PPG_RNX2RTKP`` (whether it is there and runs is checked when it is run); otherwise it would
    raise the RuntimeError of :data:`PPK_MISSING`."""
    return kernel("ppk_solve") is not None or bool(os.environ.get("PPG_RNX2RTKP", "").strip())


def ppk_solve(conf, infiles, outfile) -> tuple[int, str]:
    """RTKLIB's post-processing, as ``rnx2rtkp -k conf -o outfile infiles``: the options file
    ``conf``; the input files rover observations first, then the base's observations, then the
    navigation files; the solution written to ``outfile``. Returns RTKLIB's code and its last
    message (the last error it reported, if any). Codes of the core: 0 done, 1 aborted, -1 a
    file named in the options could not be read, -2 the options file could not be read, -3 bad
    arguments, -4 a compressed input, -5 the thread could not be put in the C locale for numbers
    (nothing ran); an external rnx2rtkp: its exit status, 0 or 1 for any failure. RTKLIB returns
    0 also when it computed nothing ("error : no obs data"): read the solution it wrote.

    Compressed inputs (:func:`compressed_name`: gzip, zip, tar, Hatanaka) raise ValueError
    before anything runs: decompress them first. With the native core (API 8) RTKLIB runs
    inside it; without it (missing, older than 0.5 or switched off) the program named by
    ``PPG_RNX2RTKP`` runs; otherwise RuntimeError."""
    files = [str(f) for f in infiles]
    for name in files:
        if compressed_name(name):
            raise ValueError(f"{name} is compressed (gzip, zip, tar or Hatanaka RINEX), and RTKLIB would "
                             "decompress it with a shell command: decompress it first")
    fn = kernel("ppk_solve")
    if fn is not None:
        code, message = fn(str(conf), files, str(outfile))
        return int(code), str(message)
    tool = os.environ.get("PPG_RNX2RTKP", "").strip()
    if not tool:
        raise RuntimeError(PPK_MISSING)
    return _rnx2rtkp(tool, str(conf), files, str(outfile))


def _rnx2rtkp(tool: str, conf: str, files: list[str], outfile: str) -> tuple[int, str]:
    """``tool -k conf -o outfile files``: 0 or 1 for any other exit status (RTKLIB-EX exits 1,
    RTKLIB 2.4.3 with postpos's -1), and its last message on stderr (rnx2rtkp separates them with
    carriage returns), the last error if it reported one."""
    path = shutil.which(tool) or (tool if Path(tool).is_file() else None)
    if path is None:
        raise RuntimeError(f"PPG_RNX2RTKP names {tool}, which is not there. " + PPK_MISSING)
    if not os.access(path, os.X_OK):
        raise RuntimeError(f"PPG_RNX2RTKP names {tool}, which is not a program that can run. " + PPK_MISSING)
    proc = subprocess.run([str(Path(path).resolve()), "-k", conf, "-o", outfile, *files], capture_output=True,
                          text=True, errors="replace", close_fds=False)
    return (0 if proc.returncode == 0 else 1), _last_message(proc.stderr or "")


def _last_message(text: str) -> str:
    """The last error message of RTKLIB's output, or the last message."""
    parts = [p.strip() for p in re.split(r"[\r\n]+", text) if p.strip()]
    errors = [p for p in parts if p.lower().startswith("error")]
    return (errors or parts or [""])[-1]


# ------------------------------------------------------------ Hatanaka-compressed RINEX (CRINEX)
CRINEX_MISSING = ("Hatanaka-compressed RINEX (Compact RINEX) needs the native core pyarchinit-photogrammetry-core "
                  "0.6 or later (pip install -U \"pyarchinit-photogrammetry-core>=0.6\"; the applications include it "
                  "from 1.6.1), or the CRX2RNX program of RNXCMP (https://terras.gsi.go.jp/ja/crx2rnx.html) in the "
                  "PATH or named by PPG_CRX2RNX")


def crinex_decoder() -> str:
    """Who decodes Hatanaka files now: the native core with its version and API, or the CRX2RNX program with its path,
    size and time; ``""`` for nobody. Part of the name of a decoded copy (``gnss.rinex.plain``): a new decoder decodes
    the file again."""
    if kernel("crx2rnx") is not None:
        return f"ppg_core {version()} API {api()}"
    tool = _crx2rnx_program()
    if tool is None:
        return ""
    path = shutil.which(tool) or tool
    try:
        stat = os.stat(path)
    except OSError:
        return f"CRX2RNX {path}"
    return f"CRX2RNX {path} {stat.st_size} {stat.st_mtime_ns}"


def _crx2rnx_program() -> str | None:
    """The CRX2RNX to run without the core: the program named by ``PPG_CRX2RNX`` (whether it is there
    is checked when it runs), otherwise CRX2RNX or crx2rnx in the PATH, otherwise None."""
    named = os.environ.get("PPG_CRX2RNX", "").strip()
    if named:
        return named
    return shutil.which("CRX2RNX") or shutil.which("crx2rnx")


def crx2rnx(src: str | Path, dst: str | Path) -> None:
    """The RINEX observation file of the Hatanaka-compressed (Compact RINEX 1.0, 3.0 or 3.1) file
    ``src``, written to ``dst`` as RNXCMP's CRX2RNX writes it: first beside it, as ``dst`` + ".part",
    renamed when complete, so that a ``dst`` is always whole.

    With the native core (API 9) the decoder runs inside it, without the GIL; a damaged or truncated
    file raises ``ValueError("<name of src>, line <n>: <what is wrong>")``. Without it (missing,
    older than 0.6 or switched off) the program named by ``PPG_CRX2RNX``, or CRX2RNX in the PATH,
    runs with ``src`` on its standard input and ``dst`` on its standard output (ValueError with its
    message when it fails); without either, RuntimeError (:data:`CRINEX_MISSING`)."""
    fn = kernel("crx2rnx")
    if fn is not None:
        fn(str(src), str(dst))
        return
    tool = _crx2rnx_program()
    if tool is None:
        raise RuntimeError(CRINEX_MISSING)
    _run_crx2rnx(tool, Path(src), Path(dst))


def _run_crx2rnx(tool: str, src: Path, dst: Path) -> None:
    """``tool < src > dst``, through ``dst`` + ".part"; ValueError with its last message when it fails."""
    path = shutil.which(tool) or (tool if Path(tool).is_file() else None)
    if path is None:
        raise RuntimeError(f"PPG_CRX2RNX names {tool}, which is not there. " + CRINEX_MISSING)
    if not os.access(path, os.X_OK):
        raise RuntimeError(f"PPG_CRX2RNX names {tool}, which is not a program that can run. " + CRINEX_MISSING)
    part = dst.with_name(dst.name + ".part")
    try:
        with open(src, "rb") as stdin, open(part, "wb") as stdout:
            proc = subprocess.run([str(Path(path).resolve())], stdin=stdin, stdout=stdout, stderr=subprocess.PIPE,
                                  close_fds=False)
        if proc.returncode != 0:
            message = _last_message(proc.stderr.decode("utf-8", "replace"))
            raise ValueError(f"{src.name}: CRX2RNX could not decode it: {message or f'exit status {proc.returncode}'}")
        os.replace(part, dst)
    finally:
        part.unlink(missing_ok=True)


# ------------------------------------------------------------ raw receiver logs converted to RINEX (convbin)
CONVBIN_MISSING = ("Raw receiver logs (u-blox, RTCM 3, NovAtel, ...) are converted to RINEX by the native core "
                   "pyarchinit-photogrammetry-core 0.8 or later (pip install -U \"pyarchinit-photogrammetry-core>=0.8\", "
                   "or pyarchinit-photogrammetry[fast]), or by the convbin program of RTKLIB-EX 2.5 (rtklibexplorer) in "
                   "the PATH or named by PPG_CONVBIN")

# the formats raw_to_rinex() takes, with the name of convbin's -r for each; RAW_FORMATS_BUILT: those the core decodes
# (all of them from 0.8: native/vendor/rtklib/VERSION)
RAW_FORMATS = ("ubx", "rtcm3", "novatel", "sbf", "rt17", "unicore")
RAW_FORMATS_BUILT = ("ubx", "rtcm3", "novatel", "sbf", "rt17", "unicore")
_CONVBIN_NAMES = {"ubx": "ubx", "rtcm3": "rtcm3", "novatel": "nov", "sbf": "sbf", "rt17": "rt17", "unicore": "unicore"}
# formats that carry the time of the week only: an approximate time of the log is needed
RAW_NEED_TIME = ("rtcm3", "rt17")
_SYSTEMS = "GREJSCI"
# convrnx's status line: "<times>: O=62 N=9 E=1 T=3" (observation epochs, navigation files N G H Q L C I, SBAS log,
# errors, events)
_STAT = re.compile(r"(?:^|\s)([ONGHQLCISET])=(\d+)")


def _convbin_program() -> str | None:
    """The convbin to run without the core: the program named by ``PPG_CONVBIN`` (whether it is there is checked when
    it runs), otherwise convbin in the PATH, otherwise None."""
    named = os.environ.get("PPG_CONVBIN", "").strip()
    if named:
        return named
    return shutil.which("convbin")


def convbin_available() -> bool:
    """:func:`raw_to_rinex` has a converter: the native core (API 11) or a convbin program."""
    return (kernel("raw_to_rinex") is not None and (api() or 0) >= CONVBIN_API) or _convbin_program() is not None


def convbin_identity() -> str:
    """Who converts raw logs now: the native core with its version and API, or the convbin program with its path, size
    and time; ``""`` for nobody. Part of the key of a cached conversion: a new converter converts the log again."""
    if kernel("raw_to_rinex") is not None and (api() or 0) >= CONVBIN_API:
        return f"ppg_core {version()} API {api()}"
    tool = _convbin_program()
    if tool is None:
        return ""
    path = shutil.which(tool) or tool
    try:
        stat = os.stat(path)
    except OSError:
        return f"convbin {path}"
    return f"convbin {path} {stat.st_size} {stat.st_mtime_ns}"


def convert_cancel() -> None:
    """Stops the conversions of the core under way, running or waiting for the lock of RTKLIB (they leave no file);
    nothing without the core or a conversion. An external convbin (the fallback of :func:`raw_to_rinex`) is not
    stopped: it runs to its end."""
    fn = kernel("convert_cancel")
    if fn is not None:
        fn()


def _gps_text(seconds: float) -> tuple[str, str]:
    """GPS seconds since 1980-01-06 as convbin's ``-tr y/m/d h:m:s`` (the same time scale: GPS time)."""
    from datetime import datetime, timedelta

    when = datetime(1980, 1, 6) + timedelta(seconds=float(seconds))
    return when.strftime("%Y/%m/%d"), when.strftime("%H:%M:%S.") + f"{when.microsecond // 1000:03d}"


def _counts(line: str) -> dict:
    found = {k: int(v) for k, v in _STAT.findall(line)}
    return {"epochs": found.get("O", 0), "events": found.get("T", 0),
            "navigation": sum(found.get(k, 0) for k in "NGHQLCI"), "errors": found.get("E", 0)}


def raw_to_rinex(format: str, src, obs, nav, approx_gps_seconds: float | None = None, rinex_version: str = "3.04",
                 systems: str = "GREJC") -> dict:
    """The raw receiver log ``src`` converted into the RINEX observation file ``obs`` and navigation file ``nav``, as
    RTKLIB-EX's ``convbin -r <format> -v <rinex_version> -o obs -n nav src`` converts it. ``format``: one of
    :data:`RAW_FORMATS` ("ubx" u-blox, "rtcm3" RTCM 3, "novatel" NovAtel OEM4/V/6/7, "sbf" Septentrio, "rt17"
    Trimble RT-17, "unicore" Unicore); ``approx_gps_seconds``: an approximate time of the log (GPS seconds since
    1980-01-06), needed by RTCM 3 and RT-17 (time of the week only), convbin's ``-tr``; ``systems``: the letters of
    the satellite systems kept (G R E J S C I; convbin's ``-y`` leaves out the others).

    The core is built with NFREQ=3 and NEXOBS=3, as the post-processing needs, where convbin's makefile has
    NFREQ=4: it keeps at most 6 signals of a satellite in an epoch instead of 7, so on logs of many signals (Galileo
    E1, E5a, E5b, E5ab and E6, BeiDou B1I, B2I, B3I, B1C and B2a...) it may write fewer observation types than
    convbin; on RTKLIB's u-blox, RTCM 3 and NovAtel samples (3 signals at most) the output is the same.

    Returns ``{"code", "message", "epochs", "events", "navigation", "errors"}``: code 0 done, 1 cancelled
    (:func:`convert_cancel`), -1 the log unreadable or an output not written, -2 no observations decoded (not a log of
    that format, or damaged), -3 bad arguments, -5 the thread could not be put in the C locale, -6 a decoder the core
    does not carry and no convbin program to run instead; with an external convbin, 0 or -2 (it failed: the end of its
    messages). :func:`convert_cancel` stops the core only. The
    outputs are written beside ``obs`` and ``nav`` with ".part" after their names and renamed when complete: on any
    code but 0 neither is there.

    With the native core (API 11) RTKLIB's conversion runs inside it, without the GIL, never at the same time as the
    post-processing; otherwise, and for a format whose decoder the core does not carry, the convbin program named by
    ``PPG_CONVBIN`` or in the PATH runs; without either, RuntimeError (:data:`CONVBIN_MISSING`). A format not known,
    a version or systems out of range, or no approximate time where it is needed, raise ValueError before anything
    runs."""
    if format not in RAW_FORMATS:
        raise ValueError(f"the format {format!r} is not one of the raw formats converted: {', '.join(RAW_FORMATS)}")
    if not re.fullmatch(r"3\.0[0-5]", str(rinex_version)):
        raise ValueError(f"RINEX version {rinex_version}: 3.00 to 3.05 are written")
    if not systems or any(s not in _SYSTEMS for s in systems):
        raise ValueError(f"satellite systems {systems!r}: letters among {' '.join(_SYSTEMS)}")
    if format in RAW_NEED_TIME and not (approx_gps_seconds and approx_gps_seconds > 0):
        raise ValueError(f"{Path(src).name}: {format} carries the time of the week only, an approximate time of the "
                         "log (its day) is needed")
    fn = kernel("raw_to_rinex") if (api() or 0) >= CONVBIN_API else None
    if fn is not None:
        code, message, counts = fn(format, str(src), str(obs), str(nav),
                                   float(approx_gps_seconds) if approx_gps_seconds else None, str(rinex_version),
                                   systems)
        if code != -6 or _convbin_program() is None:
            return {"code": int(code), "message": str(message).strip(), **{k: int(v) for k, v in counts.items()}}
    tool = _convbin_program()
    if tool is None:
        raise RuntimeError(CONVBIN_MISSING)
    return _run_convbin(tool, format, Path(src), Path(obs), Path(nav), approx_gps_seconds, str(rinex_version),
                        systems)


def _run_convbin(tool: str, format: str, src: Path, obs: Path, nav: Path, approx_gps_seconds: float | None,
                 rinex_version: str, systems: str) -> dict:
    """``tool -r <format> -v <version> [-tr <day> <time>] [-y X ...] -o obs.part -n nav.part src``, renamed when it
    exits with 0 and wrote observations; the counts from its last status line on the standard error."""
    path = shutil.which(tool) or (tool if Path(tool).is_file() else None)
    if path is None:
        raise RuntimeError(f"PPG_CONVBIN names {tool}, which is not there. " + CONVBIN_MISSING)
    if not os.access(path, os.X_OK):
        raise RuntimeError(f"PPG_CONVBIN names {tool}, which is not a program that can run. " + CONVBIN_MISSING)
    obs_part, nav_part = obs.with_name(obs.name + ".part"), nav.with_name(nav.name + ".part")
    args = [str(Path(path).resolve()), "-r", _CONVBIN_NAMES[format], "-v", rinex_version]
    if approx_gps_seconds:
        args += ["-tr", *_gps_text(approx_gps_seconds)]
    for letter in _SYSTEMS:
        if letter not in systems:
            args += ["-y", letter]
    args += ["-o", str(obs_part), "-n", str(nav_part), str(src)]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, errors="replace", close_fds=False)
        lines = [p.strip() for p in re.split(r"[\r\n]+", proc.stderr or "") if p.strip()]
        stats = [line for line in lines if _STAT.search(line)]
        line = stats[-1] if stats else _last_message(proc.stderr or "")
        counts = _counts(stats[-1]) if stats else _counts("")
        if proc.returncode != 0:  # -2: a failure (1 would read as cancelled)
            tail = " | ".join(lines[-3:]) if lines else ""
            return {"code": -2, "message": f"convbin failed on {src.name} (exit status {proc.returncode})"
                                           + (f": {tail}" if tail else ""), **counts}
        if not obs_part.exists() or (stats and counts["epochs"] <= 0):
            return {"code": -2, "message": f"no observations decoded from {src.name}: not a log of the {format} format, "
                                           f"or damaged (convbin: {line})", **counts}
        os.replace(obs_part, obs)
        if nav_part.exists():
            os.replace(nav_part, nav)
        else:
            nav.unlink(missing_ok=True)
        return {"code": 0, "message": line, **counts}
    finally:
        obs_part.unlink(missing_ok=True)
        nav_part.unlink(missing_ok=True)
