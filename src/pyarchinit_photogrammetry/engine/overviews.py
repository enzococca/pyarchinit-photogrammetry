"""Overviews of a GeoTIFF refreshed over a window only.

A patch changes a few tiles of an orthomosaic of tens of thousands of pixels: building every
overview again took a minute for the 45,000 px orthomosaic of a tell, for 2 seconds of patch.
Here the overviews of the changed windows are made again (mean weighted by the alpha band,
level after level) and written into the overview images of the file through the GDAL library
that rasterio has already loaded; when that library cannot be reached, :func:`refresh` says so
and the caller builds every overview again.
"""
from __future__ import annotations

import ctypes
import os
import sys
from functools import lru_cache
from pathlib import Path

import numpy as np

GA_UPDATE, GF_WRITE, GDT_BYTE = 1, 1, 1


def _loaded_gdal_path() -> str | None:
    """The GDAL library rasterio uses (already loaded in this process)."""
    import rasterio  # noqa: F401 - loads it

    try:
        if sys.platform == "darwin":
            libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            libc._dyld_image_count.restype = ctypes.c_uint32
            libc._dyld_get_image_name.restype = ctypes.c_char_p
            libc._dyld_get_image_name.argtypes = [ctypes.c_uint32]
            for i in range(libc._dyld_image_count()):
                name = libc._dyld_get_image_name(i)
                if name and os.path.basename(name).lower().startswith(b"libgdal"):
                    return name.decode()
        elif sys.platform.startswith("linux"):
            with open("/proc/self/maps") as fh:
                for line in fh:
                    path = line.split()[-1] if "/" in line else ""
                    if os.path.basename(path).startswith("libgdal"):
                        return path
        else:  # Windows wheels: the DLLs beside the package
            import rasterio

            base = Path(rasterio.__file__).parent
            for folder in (base, base.parent / "rasterio.libs", base / ".libs"):
                for f in sorted(folder.glob("gdal*.dll")):
                    return str(f)
    except OSError:  # pragma: no cover
        return None
    return None


@lru_cache(1)
def _gdal():
    import rasterio

    path = _loaded_gdal_path()
    if not path:
        return None
    try:
        lib = ctypes.CDLL(path)
        lib.GDALVersionInfo.restype = ctypes.c_char_p
        lib.GDALVersionInfo.argtypes = [ctypes.c_char_p]
        if lib.GDALVersionInfo(b"RELEASE_NAME").decode() != rasterio.__gdal_version__:
            return None  # another GDAL than rasterio's: leave it alone
        lib.GDALAllRegister()
        lib.GDALOpen.restype = ctypes.c_void_p
        lib.GDALOpen.argtypes = [ctypes.c_char_p, ctypes.c_int]
        lib.GDALGetRasterBand.restype = ctypes.c_void_p
        lib.GDALGetRasterBand.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.GDALGetOverviewCount.restype = ctypes.c_int
        lib.GDALGetOverviewCount.argtypes = [ctypes.c_void_p]
        lib.GDALGetOverview.restype = ctypes.c_void_p
        lib.GDALGetOverview.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.GDALGetRasterBandXSize.restype = ctypes.c_int
        lib.GDALGetRasterBandXSize.argtypes = [ctypes.c_void_p]
        lib.GDALGetRasterBandYSize.restype = ctypes.c_int
        lib.GDALGetRasterBandYSize.argtypes = [ctypes.c_void_p]
        lib.GDALRasterIO.restype = ctypes.c_int
        lib.GDALRasterIO.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int]
        lib.GDALClose.restype = None
        lib.GDALClose.argtypes = [ctypes.c_void_p]
        return lib
    except (OSError, AttributeError):  # pragma: no cover
        return None


def available() -> bool:
    return _gdal() is not None


def _halve(arr: np.ndarray, valid: np.ndarray, alpha: int | None, k: int):
    """One overview level from the next finer one: blocks of ``k x k`` pixels averaged over the
    pixels that are not transparent (the alpha band is a mask, as for GDAL: a block with one
    visible pixel is visible); ``valid`` marks the pixels inside the image."""
    c, h, w = arr.shape
    hh, ww = -(-h // k), -(-w // k)
    pad = ((0, 0), (0, hh * k - h), (0, ww * k - w))
    a = np.pad(arr, pad).reshape(c, hh, k, ww, k)
    v = np.pad(valid, pad[1:]).reshape(hh, k, ww, k)
    m = (v & (a[alpha] > 0)) if alpha is not None else v
    n = m.sum(axis=(1, 3))
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(n > 0, (a * m).sum(axis=(2, 4)) / np.maximum(n, 1), 0)
    return out, v.any(axis=(1, 3))


def refresh(path, windows, factors=None) -> bool:
    """Make the overviews of ``path`` again over ``windows`` (``(col, row, width, height)`` of
    the full image), which must have been written and closed. False when it cannot be done
    here (the GDAL library out of reach, factors that are not successive halvings): then build
    every overview again."""
    import rasterio
    from rasterio.enums import ColorInterp
    from rasterio.windows import Window

    lib = _gdal()
    if lib is None or not windows:
        return False
    with rasterio.open(path) as src:
        width, height, count = src.width, src.height, src.count
        factors = sorted(factors or src.overviews(1))
        if not factors or src.dtypes[0] != "uint8":
            return False
        alpha = next((i for i, ci in enumerate(src.colorinterp) if ci == ColorInterp.alpha), None)
        steps = [factors[0]] + [b // a for a, b in zip(factors, factors[1:])]
        if any(b % a for a, b in zip(factors, factors[1:])) or any(k < 2 for k in steps):
            return False
        top = factors[-1]
        blocks = []  # (factor, col, row, data (count, h, w))
        for col, row, w, h in windows:
            c0, r0 = (int(col) // top) * top, (int(row) // top) * top
            c1, r1 = min(-(-(int(col) + int(w)) // top) * top, width), min(-(-(int(row) + int(h)) // top) * top, height)
            if c1 <= c0 or r1 <= r0:
                continue
            cur = src.read(window=Window(c0, r0, c1 - c0, r1 - r0)).astype(np.float64)
            valid = np.ones(cur.shape[1:], bool)
            for f, k in zip(factors, steps):
                cur, valid = _halve(cur, valid, alpha, k)
                blocks.append((f, c0 // f, r0 // f, np.clip(np.round(cur), 0, 255).astype(np.uint8)))
    ds = lib.GDALOpen(os.fsencode(str(path)), GA_UPDATE)
    if not ds:
        return False
    try:
        for b in range(1, count + 1):
            band = lib.GDALGetRasterBand(ds, b)
            levels = {}
            for i in range(lib.GDALGetOverviewCount(band)):
                ov = lib.GDALGetOverview(band, i)
                levels[(lib.GDALGetRasterBandXSize(ov), lib.GDALGetRasterBandYSize(ov))] = ov
            for f, col, row, data in blocks:
                ov = levels.get((-(-width // f), -(-height // f)))
                if ov is None:
                    return False
                ow, oh = lib.GDALGetRasterBandXSize(ov), lib.GDALGetRasterBandYSize(ov)
                w, h = min(data.shape[2], ow - col), min(data.shape[1], oh - row)
                if w <= 0 or h <= 0:
                    continue
                buf = np.ascontiguousarray(data[b - 1, :h, :w])
                if lib.GDALRasterIO(ov, GF_WRITE, col, row, w, h, buf.ctypes.data, w, h, GDT_BYTE, 0, 0) != 0:
                    return False
    finally:
        lib.GDALClose(ds)
    return True


__all__ = ["available", "refresh"]
