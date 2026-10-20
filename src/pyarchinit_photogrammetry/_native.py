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
