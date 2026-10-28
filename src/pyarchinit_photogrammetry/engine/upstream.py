"""The libraries the engine is built on - COLMAP through pycolmap, Open3D - asked what they can run on a GPU here,
not guessed from their versions: a build that gains a GPU back-end shows it in the diagnosis (``ppg doctor``,
*Aiuto > Diagnostica ambiente*) the day it is installed. Using it is a choice made against the program's own kernels
(wgpu: Metal, Vulkan, DirectX 12), measured on the metric tests; the weekly ``upstream`` workflow tries the newest
versions of both libraries and lists what they add and remove (.github/workflows/upstream.yml)."""
from __future__ import annotations


def colmap_gpu() -> list[str]:
    """The GPU back-ends of this build of COLMAP: the ``has_<name>`` flags of pycolmap that are true - ``["cuda"]``
    with the CUDA wheels of Linux, none with the wheels of PyPI for macOS and Windows - or none when pycolmap does not
    load."""
    try:
        import pycolmap
    except Exception:  # a broken install: the diagnosis says it on the line of pycolmap
        return []
    return sorted(name[4:] for name in dir(pycolmap) if name.startswith("has_") and getattr(pycolmap, name) is True)


def open3d_gpu() -> list[str]:
    """The devices besides the CPU this build of Open3D can use now: each module of ``open3d.core`` whose
    ``is_available()`` says yes (``cuda``, ``sycl``); none without Open3D or on a CPU build."""
    try:
        import open3d.core as o3c
    except Exception:
        return []
    found = []
    for name in dir(o3c):
        probe = getattr(getattr(o3c, name, None), "is_available", None)
        if name.startswith("_") or not callable(probe):
            continue
        try:
            if probe() is True:
                found.append(name)
        except Exception:  # needs arguments (not a device), or the device cannot answer
            continue
    return sorted(found)
