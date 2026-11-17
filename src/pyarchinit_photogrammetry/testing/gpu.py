"""The GPU adapter the tests of the kernels run on: the one the program takes by itself (``auto``, the best GPU of
the computer) or, on request, a software rasteriser (lavapipe on Linux, WARP on Windows), which ``auto`` never
takes. A CI runner has no GPU: with the request the kernels are tried there on the adapter it has, without it
they are tried, as before, where a GPU is."""
from __future__ import annotations


def pick_test_device(adapters: list[dict], auto: int | None, software: bool) -> int | None:
    """The device a GPU test passes to the kernels (``device=``), or None when it has to skip.

    ``adapters`` is the list of :func:`.._native.gpu_adapters` and ``auto`` the answer of
    :func:`.._native.gpu_device` to ``"auto"``: -1 for the best GPU, None when the computer has none (a software
    rasteriser is no GPU). Without ``software`` the test runs on ``auto``. With it the answer is the first adapter
    of type ``Cpu``, as its position in the list, which is the index the kernels know an adapter by: a GPU beside
    it is left alone, and a computer without a software rasteriser has no device."""
    if not software:
        return auto
    for position, adapter in enumerate(adapters):
        if adapter.get("device_type") == "Cpu":
            return position
    return None
