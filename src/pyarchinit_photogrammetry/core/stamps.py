"""Products stamped with the chunk transform they were built in.

A raster product of a chunk - the DEM, the orthomosaic, the precision of the DEM, the coverage, the TIN - lives in
the coordinates of the chunk's system (``chunk.transform``, internal -> world). When the transform changes later (an
orientation of a local system, markers added), those products no longer sit where the model is: they are *da rifare*.
Each builder stamps its product with the transform it was built in:

* ``chunk.meta["built_in"][product]`` for the products of :data:`GRID_PRODUCTS` (:func:`stamp`);
* ``entry["built_in"]`` for the records of the other rasters - the elevations, the reliefs, the results of the raster
  calculator, the multispectral and thermal rasters, the satellite scenes and stereo models (:func:`stamp_entry`) -
  and for the kept versions of the DEM and the orthomosaic (``core.versions`` copies the stamp when it puts a version
  aside and writes it back when it makes one active).

A product is stale when its stamp differs from the transform of the chunk (:func:`same_transform`); a product without a
stamp (built before 1.7.2) reads as current. Since the stamps live in ``chunk.meta``, undoing a change of the transform
makes the products current again by itself. Products changed in place (an edited DEM, patches) keep the stamp they had.
"""
from __future__ import annotations

import numpy as np

from . import geometry

GRID_PRODUCTS = ("dem", "orthomosaic", "dem_precision", "coverage", "tin")
STAMP_RTOL = 1e-9

NAMES = {"dem": "DEM", "orthomosaic": "Ortofoto", "dem_precision": "Precisione del DEM", "coverage": "Copertura",
         "tin": "TIN"}  # Italian data names, like ``core.versions.PRODUCTS``: the window translates them


def _matrix(m) -> np.ndarray:
    return np.asarray(m, dtype=float).reshape(4, 4)


def stamp(chunk, product: str) -> None:
    """The product ``product`` was built now, in ``chunk.transform``."""
    chunk.meta.setdefault("built_in", {})[product] = _matrix(chunk.transform).tolist()


def stamp_entry(chunk, entry: dict) -> dict:
    """The record ``entry`` of a raster built now carries ``chunk.transform``; returned."""
    entry["built_in"] = _matrix(chunk.transform).tolist()
    return entry


def same_transform(a, b) -> bool:
    """Two transforms equal within :data:`STAMP_RTOL`: the linear part with an absolute tolerance relative to its
    size, the translation relative to its length - so that a rotation still shows when the translation is that of a
    projected system (millions of metres)."""
    a, b = _matrix(a), _matrix(b)
    if not (np.all(np.isfinite(a)) and np.all(np.isfinite(b))):
        return False
    lin = max(1.0, float(np.abs(a[:3, :3]).max()), float(np.abs(b[:3, :3]).max()))
    t = max(1.0, float(np.linalg.norm(a[:3, 3])), float(np.linalg.norm(b[:3, 3])))
    return bool(np.allclose(a[:3, :3], b[:3, :3], rtol=STAMP_RTOL, atol=STAMP_RTOL * lin)
                and np.allclose(a[:3, 3], b[:3, 3], rtol=STAMP_RTOL, atol=STAMP_RTOL * t)
                and np.allclose(a[3], b[3], rtol=STAMP_RTOL, atol=STAMP_RTOL))


def _stale(chunk, built_in, scale_only: bool = False) -> bool:
    if built_in is None:
        return False  # built before 1.7.2: current
    if scale_only:
        s0 = geometry.decompose_similarity(_matrix(built_in))[0]
        s1 = geometry.decompose_similarity(_matrix(chunk.transform))[0]
        return not bool(np.isclose(s0, s1, rtol=STAMP_RTOL, atol=STAMP_RTOL))
    return not same_transform(built_in, chunk.transform)


def is_stale(chunk, product: str) -> bool:
    """The product ``product`` was built in another transform (False without a stamp)."""
    return _stale(chunk, (chunk.meta.get("built_in") or {}).get(product))


def entry_stale(chunk, entry: dict, scale_only: bool = False) -> bool:
    """The record ``entry`` was built in another transform; ``scale_only``: only when the scale changed (an elevation:
    its frame follows a rigid motion, its pixels stay valid)."""
    return _stale(chunk, (entry or {}).get("built_in"), scale_only)


def _item(kind: str, product: str, name: str, eid) -> dict:
    return {"kind": kind, "product": product, "name": name, "id": eid}


def stale_products(chunk) -> list[dict]:
    """What is to be built again, in the order of a rebuild: the DEM, the orthomosaic, the precision of the DEM, the
    coverage, the elevations, the reliefs, the TIN, then the results of the raster calculator, the multispectral, the
    thermal and the satellite rasters and the kept versions of the DEM and the orthomosaic. Each item is
    ``{"kind", "product", "name", "id"}`` (kind "product", "version", "elevation", "relief", "calculator",
    "multispectral", "thermal" or "satellite")."""
    meta = chunk.meta
    products = getattr(chunk, "products", {}) or {}
    out: list[dict] = []

    def product(name: str) -> None:
        if products.get(name) and is_stale(chunk, name):
            out.append(_item("product", name, NAMES[name], None))

    for name in ("dem", "orthomosaic", "dem_precision", "coverage"):
        product(name)
    for e in meta.get("elevations") or []:
        if isinstance(e, dict) and entry_stale(chunk, e, scale_only=True):
            out.append(_item("elevation", "elevation", e.get("name") or e.get("id"), e.get("id")))
    for method, e in (meta.get("relief") or {}).items():
        if isinstance(e, dict) and entry_stale(chunk, e):
            out.append(_item("relief", method, method, method))
    product("tin")
    for name, e in (meta.get("calculator") or {}).items():
        if isinstance(e, dict) and entry_stale(chunk, e):
            out.append(_item("calculator", "calculator", name, name))
    ms = meta.get("multispectral") or {}
    if ms.get("reflectance") and entry_stale(chunk, ms):  # the stamp of the reflectance is the record's own
        out.append(_item("multispectral", "reflectance", "reflectance", "reflectance"))
    for key, kind in (("indices", "index"), ("zoning", "zoning")):
        for name, e in (ms.get(key) or {}).items():
            if isinstance(e, dict) and entry_stale(chunk, e):
                out.append(_item("multispectral", kind, name, name))
    th = meta.get("thermal") or {}
    if th.get("file") and entry_stale(chunk, th):  # the stamp of the temperature orthomosaic is the record's own
        out.append(_item("thermal", "thermal", "temperature", "temperature"))
    if isinstance(th.get("anomaly"), dict) and entry_stale(chunk, th["anomaly"]):
        out.append(_item("thermal", "thermal_anomaly", "anomalies", "anomalies"))
    for kind in ("scenes", "stereo"):
        for r in (meta.get("satellite") or {}).get(kind) or []:
            if isinstance(r, dict) and entry_stale(chunk, r):
                out.append(_item("satellite", kind, r.get("name"), r.get("version")))
    for name in ("dem", "orthomosaic"):
        for e in (meta.get("versions") or {}).get(name) or []:
            if isinstance(e, dict) and entry_stale(chunk, e):
                out.append(_item("version", name, e.get("name") or e.get("id"), e.get("id")))
    return out


__all__ = ["GRID_PRODUCTS", "STAMP_RTOL", "stamp", "stamp_entry", "same_transform", "is_stale", "entry_stale",
           "stale_products"]
