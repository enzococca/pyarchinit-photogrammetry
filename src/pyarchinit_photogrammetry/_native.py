"""Optional native kernels: the ``ppg_core`` extension, written in Rust (``native/`` in the
repository, package ``pyarchinit-photogrammetry-core``).

Every caller keeps its numpy code: it runs when the extension is missing, older than the
function needed, or switched off with ``PPG_NATIVE=0``, and the tests compare the two.
"""
from __future__ import annotations

import os

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


def supports(model: str) -> bool:
    """The projection kernels know the camera model ``model`` (a COLMAP model name)."""
    core = _core() if enabled() else None
    return core is not None and model in core.supported_models()


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
