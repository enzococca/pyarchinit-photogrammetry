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
}


def supports(model: str) -> bool:
    """The projection kernels know the camera model ``model`` (a COLMAP model name), or the
    extension has the feature ``model`` of :data:`FEATURES` (``"ppk"``: RTKLIB's post-processing,
    API 8; ``"crinex"``: Hatanaka-compressed RINEX decoded, API 9)."""
    core = _core() if enabled() else None
    if core is None:
        return False
    if model in FEATURES:
        return hasattr(core, FEATURES[model])
    return model in core.supported_models()


def has(feature: str) -> bool:
    """The installed extension carries the feature ``feature`` of :data:`FEATURES` (``"ppk"``, ``"crinex"``),
    whether or not ``PPG_NATIVE`` switches it off: what a build holds (the smoke test of the
    packaged application), where :func:`supports` says what the program uses."""
    core = _core()
    return core is not None and feature in FEATURES and hasattr(core, FEATURES[feature])


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
                  "0.6 or later (pip install \"pyarchinit-photogrammetry[fast]\"), or the CRX2RNX program of "
                  "RNXCMP (https://terras.gsi.go.jp/ja/crx2rnx.html) in the PATH or named by PPG_CRX2RNX")


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
