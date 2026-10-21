"""Satellite imagery over the survey.

A satellite scene comes with its *rational polynomial coefficients* (RPC): two rational cubic
polynomials that give the image column and row of any ground point (longitude, latitude,
ellipsoidal height). Two things are built from them here:

* the **orthorectification** of a scene on the DEM of the survey (:func:`orthorectify`): every
  cell of the output grid is lifted to the height of the DEM, projected into the scene through
  the RPC and the scene is resampled there. Kept beside the survey products as a version of the
  orthomosaic (:func:`ortho_scene`), so the drone orthomosaic and the satellite image of the same
  ground can be compared cell by cell;
* a **digital surface model from a stereo pair** (:func:`stereo_dsm`): two scenes of the same
  ground taken from two directions are both resampled on the ground plane at a reference height,
  where a point above or below that plane appears displaced along the line of sight of each
  scene; the residual disparity between the two, found by semi-global matching along the
  direction of the baseline, is proportional to the height above the plane. Kept as a version of
  the DEM (:func:`dsm_from_pair`).

Conventions: RPC sample and line follow the RPC00B standard, the centre of the first pixel at
(0, 0), as OpenCV counts pixels; heights are ellipsoidal WGS84, so a DEM in orthometric heights
needs the geoid undulation of the place as ``height_offset``. RPCs are read from the GeoTIFF
itself, from a ``_RPC.TXT`` / ``.RPB`` / ``.rpc`` sidecar or from the ``RPC_*.XML`` of a DIMAP
product (Pleiades, SPOT: their 1-based pixel numbering is brought to 0-based).
"""
from __future__ import annotations

import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

import numpy as np

from ..core import crs as crs_mod
from ..core.progress import ProgressCallback, as_progress

log = logging.getLogger(__name__)

FOLDER = "satellite"
NODATA = -32767.0  # of the surface models (as engine.dem)


# ---------------------------------------------------------------------------- the RPC model
@dataclass
class Rpc:
    """The 90 numbers of an RPC: offsets and scales of the five variables, the 4 x 20
    coefficients (``line_num, line_den, samp_num, samp_den``, RPC00B term order)."""

    line_off: float
    line_scale: float
    samp_off: float
    samp_scale: float
    lat_off: float
    lat_scale: float
    long_off: float
    long_scale: float
    height_off: float
    height_scale: float
    line_num: np.ndarray
    line_den: np.ndarray
    samp_num: np.ndarray
    samp_den: np.ndarray
    err_bias: float | None = None
    err_rand: float | None = None

    @classmethod
    def from_rasterio(cls, r) -> "Rpc":
        return cls(line_off=float(r.line_off), line_scale=float(r.line_scale), samp_off=float(r.samp_off),
                   samp_scale=float(r.samp_scale), lat_off=float(r.lat_off), lat_scale=float(r.lat_scale),
                   long_off=float(r.long_off), long_scale=float(r.long_scale), height_off=float(r.height_off),
                   height_scale=float(r.height_scale), line_num=np.asarray(r.line_num_coeff, float),
                   line_den=np.asarray(r.line_den_coeff, float), samp_num=np.asarray(r.samp_num_coeff, float),
                   samp_den=np.asarray(r.samp_den_coeff, float),
                   err_bias=None if r.err_bias is None else float(r.err_bias),
                   err_rand=None if r.err_rand is None else float(r.err_rand))

    def to_rasterio(self):
        from rasterio.rpc import RPC

        return RPC(height_off=self.height_off, height_scale=self.height_scale, lat_off=self.lat_off,
                   lat_scale=self.lat_scale, line_den_coeff=[float(v) for v in self.line_den],
                   line_num_coeff=[float(v) for v in self.line_num], line_off=self.line_off,
                   line_scale=self.line_scale, long_off=self.long_off, long_scale=self.long_scale,
                   samp_den_coeff=[float(v) for v in self.samp_den], samp_num_coeff=[float(v) for v in self.samp_num],
                   samp_off=self.samp_off, samp_scale=self.samp_scale, err_bias=self.err_bias, err_rand=self.err_rand)

    def to_dict(self) -> dict:
        d = {k: float(getattr(self, k)) for k in ("line_off", "line_scale", "samp_off", "samp_scale", "lat_off",
                                                   "lat_scale", "long_off", "long_scale", "height_off", "height_scale")}
        for k in ("line_num", "line_den", "samp_num", "samp_den"):
            d[k] = [float(v) for v in getattr(self, k)]
        d["err_bias"], d["err_rand"] = self.err_bias, self.err_rand
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Rpc":
        return cls(**{k: (np.asarray(v, float) if k in ("line_num", "line_den", "samp_num", "samp_den") else v)
                      for k, v in d.items()})

    def write_text(self, path: Path) -> Path:
        """The RPC00B text file (``_RPC.TXT``) GDAL and the other programs read."""
        lines = [f"LINE_OFF: {self.line_off:.12g} pixels", f"SAMP_OFF: {self.samp_off:.12g} pixels",
                 f"LAT_OFF: {self.lat_off:.12g} degrees", f"LONG_OFF: {self.long_off:.12g} degrees",
                 f"HEIGHT_OFF: {self.height_off:.12g} meters", f"LINE_SCALE: {self.line_scale:.12g} pixels",
                 f"SAMP_SCALE: {self.samp_scale:.12g} pixels", f"LAT_SCALE: {self.lat_scale:.12g} degrees",
                 f"LONG_SCALE: {self.long_scale:.12g} degrees", f"HEIGHT_SCALE: {self.height_scale:.12g} meters"]
        for name, arr in (("LINE_NUM_COEFF", self.line_num), ("LINE_DEN_COEFF", self.line_den),
                          ("SAMP_NUM_COEFF", self.samp_num), ("SAMP_DEN_COEFF", self.samp_den)):
            lines += [f"{name}_{i + 1}: {v:+.15E}" for i, v in enumerate(arr)]
        Path(path).write_text("\n".join(lines) + "\n", encoding="ascii")
        return Path(path)


def _terms(L: np.ndarray, P: np.ndarray, H: np.ndarray) -> np.ndarray:
    """The 20 terms of the RPC00B cubic in the normalised longitude, latitude and height."""
    return np.stack([np.ones_like(L), L, P, H, L * P, L * H, P * H, L * L, P * P, H * H, P * L * H,
                     L * L * L, L * P * P, L * H * H, L * L * P, P * P * P, P * H * H, L * L * H, P * P * H, H * H * H])


def project(rpc: Rpc, lon, lat, h) -> tuple[np.ndarray, np.ndarray]:
    """Image ``(column, row)`` of ground points (degrees, ellipsoidal metres); centre of the
    first pixel at (0, 0)."""
    lon, lat, h = np.broadcast_arrays(np.asarray(lon, float), np.asarray(lat, float), np.asarray(h, float))
    t = _terms((lon - rpc.long_off) / rpc.long_scale, (lat - rpc.lat_off) / rpc.lat_scale,
               (h - rpc.height_off) / rpc.height_scale)
    with np.errstate(divide="ignore", invalid="ignore"):
        col = np.tensordot(rpc.samp_num, t, 1) / np.tensordot(rpc.samp_den, t, 1) * rpc.samp_scale + rpc.samp_off
        row = np.tensordot(rpc.line_num, t, 1) / np.tensordot(rpc.line_den, t, 1) * rpc.line_scale + rpc.line_off
    return col, row


def ground(rpc: Rpc, col, row, h, iterations: int = 12) -> tuple[np.ndarray, np.ndarray]:
    """Longitude and latitude of image points at a known height: the inverse of
    :func:`project`, found by Newton steps with a numerical Jacobian (the RPC is almost
    affine over a scene, a few steps reach 1e-6 px)."""
    col, row, h = np.broadcast_arrays(np.asarray(col, float), np.asarray(row, float), np.asarray(h, float))
    lon = np.full(col.shape, rpc.long_off)
    lat = np.full(col.shape, rpc.lat_off)
    dl, dp = rpc.long_scale * 1e-4, rpc.lat_scale * 1e-4
    for _ in range(iterations):
        c, r = project(rpc, lon, lat, h)
        c1, r1 = project(rpc, lon + dl, lat, h)
        c2, r2 = project(rpc, lon, lat + dp, h)
        a, b = (c1 - c) / dl, (c2 - c) / dp  # d col / d lon, d col / d lat
        d, e = (r1 - r) / dl, (r2 - r) / dp
        det = a * e - b * d
        with np.errstate(divide="ignore", invalid="ignore"):
            inv = np.where(np.abs(det) > 1e-30, 1.0 / det, 0.0)
        ec, er = col - c, row - r
        lon = lon + (e * ec - b * er) * inv
        lat = lat + (-d * ec + a * er) * inv
        if max(np.abs(ec).max(initial=0), np.abs(er).max(initial=0)) < 1e-7:
            break
    return lon, lat


# ---------------------------------------------------------------------------- reading RPCs
_SIDE_CARS = ("{stem}_RPC.TXT", "{stem}_rpc.txt", "{stem}.RPB", "{stem}.rpb", "{stem}.rpc", "{stem}_RPC.XML",
              "RPC_{stem}.XML", "RPC_{core}.XML")


def _coeffs(values: dict, prefix: str) -> np.ndarray:
    keys = [f"{prefix}_{i}" for i in range(1, 21)]
    if all(k in values for k in keys):
        return np.array([values[k] for k in keys], float)
    if prefix in values and len(values[prefix]) == 20:
        return np.asarray(values[prefix], float)
    raise ValueError(f"RPC: the 20 {prefix} coefficients are missing")


def _rpc_from_values(v: dict) -> Rpc:
    return Rpc(line_off=v["LINE_OFF"], line_scale=v["LINE_SCALE"], samp_off=v["SAMP_OFF"], samp_scale=v["SAMP_SCALE"],
               lat_off=v["LAT_OFF"], lat_scale=v["LAT_SCALE"], long_off=v["LONG_OFF"], long_scale=v["LONG_SCALE"],
               height_off=v["HEIGHT_OFF"], height_scale=v["HEIGHT_SCALE"], line_num=_coeffs(v, "LINE_NUM_COEFF"),
               line_den=_coeffs(v, "LINE_DEN_COEFF"), samp_num=_coeffs(v, "SAMP_NUM_COEFF"),
               samp_den=_coeffs(v, "SAMP_DEN_COEFF"), err_bias=v.get("ERR_BIAS"), err_rand=v.get("ERR_RAND"))


_RPB_NAMES = {"lineoffset": "LINE_OFF", "sampoffset": "SAMP_OFF", "latoffset": "LAT_OFF", "longoffset": "LONG_OFF",
              "heightoffset": "HEIGHT_OFF", "linescale": "LINE_SCALE", "sampscale": "SAMP_SCALE", "latscale": "LAT_SCALE",
              "longscale": "LONG_SCALE", "heightscale": "HEIGHT_SCALE", "linenumcoef": "LINE_NUM_COEFF",
              "linedencoef": "LINE_DEN_COEFF", "sampnumcoef": "SAMP_NUM_COEFF", "sampdencoef": "SAMP_DEN_COEFF",
              "errbias": "ERR_BIAS", "errrand": "ERR_RAND"}
_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def parse_rpc_text(text: str) -> Rpc:
    """An RPC00B text file (``KEY: value unit`` per line) or an ``.RPB`` (``key = value;`` and
    ``key = ( v1, v2, ... );``)."""
    values: dict = {}
    if "=" in text and "(" in text:  # RPB
        for key, val in re.findall(r"(\w+)\s*=\s*([^;]+);", text, re.S):
            name = _RPB_NAMES.get(key.lower())
            if name is None:
                continue
            nums = [float(x) for x in _NUMBER.findall(val)]
            values[name] = nums if "(" in val else nums[0]
    else:
        for line in text.splitlines():
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            nums = _NUMBER.findall(val)
            if nums:
                values[key.strip().upper()] = float(nums[0])
    return _rpc_from_values(values)


def parse_rpc_xml(text: str) -> Rpc:
    """The ``RPC_*.XML`` of a DIMAP product (Pleiades, SPOT 6/7): the ``Inverse_Model`` (ground
    to image) and the validity block; the 1-based pixel numbering of those products is brought
    to 0-based."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(text)
    values: dict = {}
    inverse = root.find(".//Inverse_Model")
    scope = inverse if inverse is not None else root
    for el in scope.iter():
        if el.text and re.fullmatch(r"(LINE|SAMP)_(NUM|DEN)_COEFF_\d+|ERR_BIAS_(ROW|COL)", el.tag):
            values[el.tag] = float(el.text)
    for el in root.iter():
        if el.text and re.fullmatch(r"(LINE|SAMP|LAT|LONG|HEIGHT)_(OFF|SCALE)", el.tag):
            values[el.tag] = float(el.text)
    if "ERR_BIAS_ROW" in values:
        values["ERR_BIAS"] = float(np.hypot(values["ERR_BIAS_ROW"], values.get("ERR_BIAS_COL", 0.0)))
    rpc = _rpc_from_values(values)
    if root.tag.endswith("Dimap_Document") or root.find(".//Rational_Function_Model") is not None:
        rpc.line_off -= 1.0
        rpc.samp_off -= 1.0
    return rpc


def read_rpc(scene: Path, rpc: Path | None = None) -> Rpc:
    """The RPC of a scene: a file given, the RPC of the GeoTIFF itself, or a sidecar beside it."""
    if rpc is not None:
        p = Path(rpc)
        text = p.read_text(encoding="utf-8", errors="replace")
        return parse_rpc_xml(text) if p.suffix.lower() == ".xml" or text.lstrip().startswith("<") else parse_rpc_text(text)
    import rasterio

    scene = Path(scene)
    with rasterio.open(scene) as src:
        if src.rpcs is not None:
            return Rpc.from_rasterio(src.rpcs)
    core = re.sub(r"^(IMG_)", "", scene.stem)
    for pattern in _SIDE_CARS:
        p = scene.with_name(pattern.format(stem=scene.stem, core=core))
        if p.exists():
            return read_rpc(scene, p)
    raise ValueError(f"{scene.name}: no RPC in the file and no sidecar (_RPC.TXT, .RPB, RPC_*.XML) beside it")


# ---------------------------------------------------------------------------- the scene
class _Scene:
    """A scene opened once per thread, its RPC, and the transformer to lon/lat of the CRS."""

    def __init__(self, path: Path, crs: str, rpc: Rpc | None = None, rpc_file: Path | None = None):
        import rasterio
        from pyproj import Transformer

        self.path = Path(path)
        self.rpc = rpc or read_rpc(self.path, rpc_file)
        with rasterio.open(self.path) as src:
            self.width, self.height, self.count = src.width, src.height, src.count
            self.dtype = np.dtype(src.dtypes[0])
            self.nodata = src.nodata
            self.descriptions = list(src.descriptions)
        self.crs = crs
        self.to_lonlat = Transformer.from_crs(crs_mod.to_pyproj(crs), "EPSG:4326", always_xy=True)
        self.to_crs = Transformer.from_crs("EPSG:4326", crs_mod.to_pyproj(crs), always_xy=True)
        self._local = threading.local()

    def src(self):
        s = getattr(self._local, "src", None)
        if s is None:
            import rasterio

            s = self._local.src = rasterio.open(self.path)
        return s

    def pixels(self, x: np.ndarray, y: np.ndarray, h: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Columns and rows of points of the CRS (heights ellipsoidal)."""
        lon, lat = self.to_lonlat.transform(x, y)
        return project(self.rpc, lon, lat, h)

    def ground_xy(self, col, row, h) -> tuple[np.ndarray, np.ndarray]:
        lon, lat = ground(self.rpc, col, row, h)
        return self.to_crs.transform(lon, lat)

    def gsd(self, h: float | None = None) -> float:
        """Metres on the ground of one pixel at the centre of the scene."""
        h = self.rpc.height_off if h is None else h
        c, r = self.width / 2, self.height / 2
        x, y = self.ground_xy(np.array([c, c + 1, c]), np.array([r, r, r + 1]), h)
        return float(0.5 * (np.hypot(x[1] - x[0], y[1] - y[0]) + np.hypot(x[2] - x[0], y[2] - y[0])))

    def footprint(self, h: float | None = None):
        """Polygon (CRS) of the scene at a height."""
        from shapely.geometry import Polygon

        h = self.rpc.height_off if h is None else h
        cols = np.array([0, self.width - 1, self.width - 1, 0]) + np.array([-0.5, 0.5, 0.5, -0.5])
        rows = np.array([0, 0, self.height - 1, self.height - 1]) + np.array([-0.5, -0.5, 0.5, 0.5])
        x, y = self.ground_xy(cols, rows, h)
        return Polygon(zip(x, y))

    def read_window(self, c0: int, r0: int, c1: int, r1: int) -> np.ndarray | None:
        """``(bands, rows, cols)`` of the pixels ``[c0, c1) x [r0, r1)`` (clipped), None if empty."""
        from rasterio.windows import Window

        c0, r0 = max(c0, 0), max(r0, 0)
        c1, r1 = min(c1, self.width), min(r1, self.height)
        if c1 <= c0 or r1 <= r0:
            return None
        return self.src().read(window=Window(c0, r0, c1 - c0, r1 - r0))

    def resample(self, col: np.ndarray, row: np.ndarray, interpolation: int, pad: int = 3) -> tuple[np.ndarray, np.ndarray]:
        """The scene at fractional pixel positions (same shape): ``(bands, h, w)`` and the mask
        of the positions that fall on data."""
        import cv2

        ok = np.isfinite(col) & np.isfinite(row)
        shape = col.shape
        out = np.zeros((self.count,) + shape, self.dtype)
        if not ok.any():
            return out, ok
        c0 = int(np.floor(col[ok].min())) - pad
        c1 = int(np.ceil(col[ok].max())) + pad + 1
        r0 = int(np.floor(row[ok].min())) - pad
        r1 = int(np.ceil(row[ok].max())) + pad + 1
        c0, r0 = max(c0, 0), max(r0, 0)
        data = self.read_window(c0, r0, c1, r1)
        if data is None:
            return out, np.zeros(shape, bool)
        mapx = np.where(ok, col - c0, -1e4).astype(np.float32)
        mapy = np.where(ok, row - r0, -1e4).astype(np.float32)
        kind = data.dtype
        work = data if kind in (np.uint8, np.uint16, np.int16, np.float32) else data.astype(np.float32)
        valid = np.ones(data.shape[1:], np.uint8)
        if self.nodata is not None:
            valid &= (data != self.nodata).all(axis=0)
        inside = cv2.remap(valid, mapx, mapy, cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT, borderValue=0) > 0
        for b in range(self.count):
            res = cv2.remap(work[b], mapx, mapy, interpolation, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
            out[b] = res if res.dtype == self.dtype else np.clip(res, *_limits(self.dtype)).astype(self.dtype)
        return out, ok & inside


def _limits(dtype: np.dtype) -> tuple[float, float]:
    if dtype.kind in "ui":
        info = np.iinfo(dtype)
        return float(info.min), float(info.max)
    return -np.inf, np.inf


def _nodata_for(dtype: np.dtype, nodata) -> float:
    if nodata is not None:
        return nodata
    return float("nan") if dtype.kind == "f" else 0.0


# ---------------------------------------------------------------------------- the DEM
class _Dem:
    """Heights of a DEM at points of its CRS (bilinear, NaN outside and beside no data)."""

    def __init__(self, path: Path):
        import rasterio

        self.path = Path(path)
        with rasterio.open(self.path) as src:
            self.transform, self.nodata = src.transform, src.nodata
            self.width, self.height = src.width, src.height
            self.crs = src.crs.to_wkt() if src.crs else None
            self.bounds = tuple(float(v) for v in src.bounds)
            self.resolution = float(abs(src.transform.a))
        self._local = threading.local()

    def src(self):
        s = getattr(self._local, "src", None)
        if s is None:
            import rasterio

            s = self._local.src = rasterio.open(self.path)
        return s

    def sample(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        import cv2
        from rasterio.windows import Window

        t = self.transform
        colf = ((x - t.c) / t.a - 0.5).astype(np.float64)
        rowf = ((y - t.f) / t.e - 0.5).astype(np.float64)
        a0 = max(int(np.floor(np.nanmin(colf))) - 1, 0)
        a1 = min(int(np.ceil(np.nanmax(colf))) + 2, self.width)
        b0 = max(int(np.floor(np.nanmin(rowf))) - 1, 0)
        b1 = min(int(np.ceil(np.nanmax(rowf))) + 2, self.height)
        if a1 <= a0 or b1 <= b0:
            return np.full(x.shape, np.nan)
        dem = self.src().read(1, window=Window(a0, b0, a1 - a0, b1 - b0)).astype(np.float32)
        if self.nodata is not None:
            dem[dem == self.nodata] = np.nan
        z = cv2.remap(dem, (colf - a0).astype(np.float32), (rowf - b0).astype(np.float32), cv2.INTER_LINEAR,
                      borderMode=cv2.BORDER_CONSTANT, borderValue=float("nan"))
        return z.astype(np.float64)

    def stats(self, bounds=None) -> dict:
        """Median, min and max of the heights (of a coarse reading, inside ``bounds``)."""
        from rasterio.enums import Resampling
        from rasterio.windows import from_bounds

        src = self.src()
        win = None
        if bounds is not None:
            w = from_bounds(*bounds, transform=self.transform)
            win = w.intersection(w.__class__(0, 0, self.width, self.height))
            if win.width < 1 or win.height < 1:
                return {"median": None, "min": None, "max": None}
        h = int(win.height if win else self.height)
        w_ = int(win.width if win else self.width)
        f = max(1, int(np.ceil(max(h, w_) / 1024)))
        z = src.read(1, window=win, out_shape=(max(h // f, 1), max(w_ // f, 1)), resampling=Resampling.nearest,
                     masked=True).astype(np.float64).filled(np.nan)
        if self.nodata is not None:
            z[z == self.nodata] = np.nan
        ok = np.isfinite(z)
        if not ok.any():
            return {"median": None, "min": None, "max": None}
        return {"median": float(np.median(z[ok])), "min": float(z[ok].min()), "max": float(z[ok].max())}


def _grid(bounds, resolution: float):
    """Origin (top-left) and size of a grid of ``resolution`` cells covering ``bounds``,
    snapped to multiples of the resolution."""
    x0 = np.floor(bounds[0] / resolution) * resolution
    y1 = np.ceil(bounds[3] / resolution) * resolution
    width = int(np.ceil((bounds[2] - x0) / resolution - 1e-9))
    height = int(np.ceil((y1 - bounds[1]) / resolution - 1e-9))
    return (float(x0), float(y1)), width, height


def _round_res(v: float) -> float:
    return float(f"{v:.3g}")


def _tiles(height: int, width: int, tile: int):
    for r0 in range(0, height, tile):
        for c0 in range(0, width, tile):
            yield r0, c0, min(tile, height - r0), min(tile, width - c0)


def _intersect(polys, bounds_list) -> tuple[float, float, float, float]:
    from shapely.geometry import box

    area = None
    for p in polys:
        area = p if area is None else area.intersection(p)
    for b in bounds_list:
        if b is not None:
            area = box(*b) if area is None else area.intersection(box(*b))
    if area is None or area.is_empty or area.area <= 0:
        raise ValueError("the scene does not cover the area of the survey")
    return tuple(float(v) for v in area.bounds)


# ---------------------------------------------------------------------------- orthorectification
def scene_info(scene: Path, crs: str, rpc: Path | None = None) -> dict:
    """Size, bands, type, RPC validity block, ground sampling distance and footprint (CRS) of a scene."""
    s = _Scene(scene, crs, rpc_file=rpc)
    fp = s.footprint()
    return {"path": str(scene), "width": s.width, "height": s.height, "bands": s.count, "dtype": str(s.dtype),
            "nodata": s.nodata, "gsd": s.gsd(), "footprint": [list(map(float, p)) for p in fp.exterior.coords],
            "bounds": tuple(float(v) for v in fp.bounds), "height_off": s.rpc.height_off,
            "height_scale": s.rpc.height_scale, "err_bias": s.rpc.err_bias, "err_rand": s.rpc.err_rand,
            "descriptions": s.descriptions}


def orthorectify(scene: Path, dem: Path, out: Path, resolution: float | None = None, bounds=None,
                 rpc: Path | None = None, height_offset: float = 0.0, resampling: str = "cubic", tile: int = 1024,
                 workers: int | None = None, progress: ProgressCallback | None = None) -> dict:
    """Resample a scene on the grid of the DEM's CRS: every cell lifted to the DEM height (plus
    ``height_offset``, the geoid undulation when the DEM is orthometric), projected into the
    scene through its RPC, the scene read there (``cubic``, ``linear`` or ``nearest``). The
    output keeps the bands and the type of the scene, no data where the DEM has none or the
    scene does not reach; default resolution = the scene's GSD; ``bounds`` (CRS) restrict it."""
    import cv2
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_origin
    from rasterio.windows import Window

    p = as_progress(progress)
    d = _Dem(dem)
    if d.crs is None or crs_mod.is_geographic(d.crs):
        raise ValueError("the DEM needs a projected coordinate system (metres)")
    s = _Scene(scene, d.crs, rpc_file=rpc)
    st = d.stats()
    h_ref = (st["median"] if st["median"] is not None else s.rpc.height_off - height_offset) + height_offset
    res = float(resolution) if resolution else _round_res(s.gsd(h_ref))
    area = _intersect([s.footprint(h_ref)], [d.bounds, bounds])
    origin, width, height = _grid(area, res)
    if width < 1 or height < 1:
        raise ValueError("the scene does not cover the area of the survey")
    interp = {"cubic": cv2.INTER_CUBIC, "linear": cv2.INTER_LINEAR, "nearest": cv2.INTER_NEAREST}[resampling]
    nodata = _nodata_for(s.dtype, s.nodata)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    profile = {"driver": "GTiff", "width": width, "height": height, "count": s.count, "dtype": str(s.dtype),
               "crs": d.crs, "transform": from_origin(origin[0], origin[1], res, res), "compress": "deflate",
               "BIGTIFF": "IF_SAFER", "nodata": nodata}
    if width >= 256 and height >= 256:
        profile.update(tiled=True, blockxsize=256, blockysize=256)
    tiles = list(_tiles(height, width, tile))
    lock = threading.Lock()
    done = [0]
    filled = [0]
    tmp = out.with_name(out.name + ".tmp.tif")

    def work(t):
        r0, c0, th, tw = t
        x = origin[0] + (c0 + np.arange(tw) + 0.5) * res
        y = origin[1] - (r0 + np.arange(th) + 0.5) * res
        X, Y = np.meshgrid(x, y)
        z = d.sample(X, Y) + height_offset
        col, row = s.pixels(X, Y, z)
        col[~np.isfinite(z)] = np.nan
        data, ok = s.resample(col, row, interp)
        data[:, ~ok] = nodata
        return t, data, int(ok.sum())

    try:
        with rasterio.open(tmp, "w", **profile) as dst:
            for b, name in enumerate(s.descriptions):
                if name:
                    dst.set_band_description(b + 1, name)
            with ThreadPoolExecutor(max_workers=workers or min(8, os.cpu_count() or 1)) as pool:
                futures = [pool.submit(work, t) for t in tiles]
                try:
                    for f in as_completed(futures):
                        (r0, c0, th, tw), data, n = f.result()
                        with lock:
                            dst.write(data, window=Window(c0, r0, tw, th))
                        done[0] += 1
                        filled[0] += n
                        p(0.05 + 0.85 * done[0] / len(tiles), f"Ortorettifica: tassello {done[0]} di {len(tiles)}")
                except BaseException:  # cancelled or failed: no half-written file
                    pool.shutdown(cancel_futures=True)
                    raise
            factors = [f for f in (2, 4, 8, 16, 32, 64) if min(width, height) // f >= 64]
            if factors:
                dst.build_overviews(factors, Resampling.average)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(out)
    p(1.0, f"{width} x {height} px a {res:g} m")
    return {"path": out, "width": width, "height": height, "resolution": res, "bounds": area, "gsd": s.gsd(h_ref),
            "bands": s.count, "dtype": str(s.dtype), "nodata": nodata, "filled": filled[0] / max(width * height, 1),
            "height_reference": h_ref, "tiles": len(tiles)}


# ---------------------------------------------------------------------------- stereo
def _to_uint8(a: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """A band stretched between its 1st and 99th percentiles over the valid pixels."""
    v = a[valid]
    if v.size < 16:
        return np.zeros(a.shape, np.uint8)
    lo, hi = np.percentile(v, (1, 99))
    if hi <= lo:
        hi = lo + 1
    return np.clip((a.astype(np.float32) - lo) / (hi - lo) * 255, 0, 255).astype(np.uint8)


def _parallax(s: _Scene, x: float, y: float, h: float) -> np.ndarray:
    """Ground displacement (metres of the CRS) of the line of sight of a scene per metre of
    height at a point: where the pixel that sees (x, y, h) hits the plane h + 1."""
    col, row = s.pixels(np.array([x]), np.array([y]), np.array([h]))
    gx, gy = s.ground_xy(col, row, np.array([h + 1.0]))
    return np.array([gx[0] - x, gy[0] - y])


def stereo_dsm(scene_a: Path, scene_b: Path, out: Path, crs: str | None = None, dem: Path | None = None,
               resolution: float | None = None, bounds=None, height_range: tuple[float, float] | None = None,
               rpc_a: Path | None = None, rpc_b: Path | None = None, height_offset: float = 0.0, tile: int = 1024,
               block: int = 7, progress: ProgressCallback | None = None) -> dict:
    """A surface model from two scenes of the same ground taken from two directions. Tile by
    tile both scenes are resampled on the ground plane at a reference height (the median of
    ``dem`` there, else the RPC's height) along the direction of their baseline; a point above
    or below the plane is displaced along the line of sight of each scene, so the disparity
    found by semi-global matching gives its height, and its true position is recovered by
    taking the displacement back. ``height_range`` (CRS heights) bounds the search (from the
    DEM when there is one, else +-100 m); the result is a float32 GeoTIFF with no data where no
    match (small holes filled). Returns the base-to-height ratio and the matched fraction."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_origin

    p = as_progress(progress)
    d = _Dem(dem) if dem is not None else None
    crs = crs or (d.crs if d is not None else None)
    if not crs or crs_mod.is_local(crs) or crs_mod.is_geographic(crs):
        raise ValueError("a projected coordinate system (metres) is needed: the DEM's or crs=")
    a = _Scene(scene_a, crs, rpc_file=rpc_a)
    b = _Scene(scene_b, crs, rpc_file=rpc_b)
    st = d.stats() if d is not None else {"median": None, "min": None, "max": None}
    h_ref = (st["median"] + height_offset) if st["median"] is not None else a.rpc.height_off
    res = float(resolution) if resolution else _round_res(max(a.gsd(h_ref), b.gsd(h_ref)))
    area = _intersect([a.footprint(h_ref), b.footprint(h_ref)], [d.bounds if d is not None else None, bounds])
    origin, width, height = _grid(area, res)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    profile = {"driver": "GTiff", "width": width, "height": height, "count": 1, "dtype": "float32", "crs": crs,
               "transform": from_origin(origin[0], origin[1], res, res), "compress": "deflate", "BIGTIFF": "IF_SAFER",
               "nodata": NODATA}
    if width >= 256 and height >= 256:
        profile.update(tiled=True, blockxsize=256, blockysize=256)
    tiles = list(_tiles(height, width, tile))
    job = _StereoJob(a, b, d, st, origin, res, height_range, height_offset, block)
    tmp = out.with_name(out.name + ".tmp.tif")
    try:
        with rasterio.open(tmp, "w", **profile) as dst:
            for k, t in enumerate(tiles):
                job.tile(dst, t)
                p(0.05 + 0.9 * (k + 1) / len(tiles), f"DSM stereo: tassello {k + 1} di {len(tiles)}")
            factors = [f for f in (2, 4, 8, 16, 32) if min(width, height) // f >= 64]
            if factors:
                dst.build_overviews(factors, Resampling.nearest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(out)
    p(1.0, f"{width} x {height} celle a {res:g} m")
    return {"path": out, "width": width, "height": height, "resolution": res, "bounds": area,
            "base_to_height": float(np.mean(job.ratios)) if job.ratios else 0.0,
            "matched": job.matched / max(width * height, 1), "tiles": len(tiles), "height_reference": h_ref}


class _StereoJob:
    """The tiles of :func:`stereo_dsm`: the reference plane, the rectification of both scenes
    on it along the baseline, the matching, the heights and their true positions."""

    def __init__(self, a: _Scene, b: _Scene, d: _Dem | None, st: dict, origin, res: float, height_range,
                 height_offset: float, block: int):
        self.a, self.b, self.d, self.st = a, b, d, st
        self.origin, self.res, self.height_range, self.height_offset, self.block = origin, res, height_range, height_offset, block
        self.matched = 0
        self.ratios: list[float] = []

    def _plane(self, tb) -> tuple[float, float, float]:
        """Reference height and search range (RPC heights) of a tile."""
        d, st, off = self.d, self.st, self.height_offset
        if d is not None:
            ts = d.stats(tb)
            h0 = (ts["median"] if ts["median"] is not None else st["median"]) + off
            if self.height_range:
                zmin, zmax = self.height_range
            else:
                zmin = ts["min"] if ts["min"] is not None else st["min"]
                zmax = ts["max"] if ts["max"] is not None else st["max"]
            zmin, zmax = zmin + off, zmax + off
            margin = max(2.0, 0.1 * (zmax - zmin))
            return h0, zmin - margin, zmax + margin
        h0 = self.a.rpc.height_off
        if self.height_range:
            return h0, self.height_range[0] + off, self.height_range[1] + off
        return h0, h0 - 100.0, h0 + 100.0

    def tile(self, dst, t) -> None:
        import cv2
        from rasterio.windows import Window

        from .dem import remove_spikes

        a, b, res = self.a, self.b, self.res
        r0, c0, th, tw = t
        x0, y1 = self.origin[0] + c0 * res, self.origin[1] - r0 * res
        tb = (x0, y1 - th * res, x0 + tw * res, y1)
        xc, yc = 0.5 * (tb[0] + tb[2]), 0.5 * (tb[1] + tb[3])
        h0, zmin, zmax = self._plane(tb)
        ua, ub = _parallax(a, xc, yc, h0), _parallax(b, xc, yc, h0)
        base = ua - ub
        ratio = float(np.hypot(*base))
        if ratio < 0.05:
            raise ValueError(f"the two scenes look from almost the same direction (base to height {ratio:.3f})")
        self.ratios.append(ratio)
        bhat = base / ratio
        nhat = np.array([-bhat[1], bhat[0]])
        span = max(abs(h0 - zmin), abs(h0 - zmax))
        m = int(np.ceil(span * max(np.hypot(*ua), np.hypot(*ub), ratio) / res)) + 32
        H, W = th + 2 * m, tw + 2 * m
        jj, ii = np.meshgrid((np.arange(W) - W / 2 + 0.5) * res, (np.arange(H) - H / 2 + 0.5) * res)
        gx = xc + jj * bhat[0] + ii * nhat[0]
        gy = yc + jj * bhat[1] + ii * nhat[1]
        imgs = []
        for s in (a, b):  # both scenes on the plane h0, the x axis along the baseline
            col, row = s.pixels(gx, gy, np.full(gx.shape, h0))
            data, ok = s.resample(col, row, cv2.INTER_LINEAR)
            band = data[0].astype(np.float32) if s.count == 1 else data[:3].astype(np.float32).mean(axis=0)
            imgs.append((_to_uint8(band, ok), ok))
        # a point at height h: apparent position g + (h0 - h) u in each scene, so the disparity
        # d = x_a - x_b = (h0 - h) |base| / res
        d_lo = (h0 - zmax) * ratio / res
        d_hi = (h0 - zmin) * ratio / res
        min_disp = int(np.floor(d_lo)) - 1
        num = int(np.ceil((d_hi - min_disp + 1) / 16.0)) * 16
        k = self.block
        sgbm = cv2.StereoSGBM_create(minDisparity=min_disp, numDisparities=num, blockSize=k, P1=8 * k * k,
                                     P2=32 * k * k, disp12MaxDiff=1, uniquenessRatio=10, speckleWindowSize=100,
                                     speckleRange=2, mode=cv2.STEREO_SGBM_MODE_HH)
        disp = sgbm.compute(imgs[0][0], imgs[1][0]).astype(np.float32) / 16.0
        valid = (disp > min_disp) & imgs[0][1] & imgs[1][1]
        hgt = h0 - disp * res / ratio
        shift = h0 - hgt  # the true position: the apparent one minus the displacement in scene A
        tx = gx - shift * ua[0]
        ty = gy - shift * ua[1]
        cols = np.floor((tx - x0) / res).astype(np.int64)
        rows = np.floor((y1 - ty) / res).astype(np.int64)
        inside = valid & (cols >= 0) & (cols < tw) & (rows >= 0) & (rows < th)
        idx = rows[inside] * tw + cols[inside]
        sums = np.bincount(idx, weights=hgt[inside], minlength=th * tw)
        cnt = np.bincount(idx, minlength=th * tw)
        z = np.full(th * tw, np.nan, np.float64)
        got = cnt > 0
        z[got] = sums[got] / cnt[got]
        z = z.reshape(th, tw)
        if got.any():
            z, _ = remove_spikes(z, res)
            holes = ~np.isfinite(z)
            if holes.any() and not holes.all():
                from rasterio.fill import fillnodata

                z = np.asarray(fillnodata(z.astype(np.float32), mask=(~holes).astype(np.uint8), max_search_distance=4,
                                          smoothing_iterations=0), np.float64)
        z = (z - self.height_offset).astype(np.float32)
        self.matched += int(np.isfinite(z).sum())
        z[~np.isfinite(z)] = NODATA
        dst.write(z[None], window=Window(c0, r0, tw, th))


# ---------------------------------------------------------------------------- in the project
def _folder(chunk) -> Path:
    f = chunk.data_dir / FOLDER
    f.mkdir(parents=True, exist_ok=True)
    return f


def _record(chunk, kind: str, entry: dict) -> None:
    chunk.meta.setdefault(FOLDER, {}).setdefault(kind, []).append(entry)


def rgb_preview(src_path: Path, dst_path: Path, rgb: Sequence[int] | None = None, tile: int = 2048,
                progress: ProgressCallback | None = None) -> Path:
    """An 8-bit RGBA GeoTIFF of a multi-band raster for the viewers: the three bands ``rgb``
    (1-based; the first three by default, the only one repeated) stretched between their 2nd and
    98th percentiles, alpha where there is data."""
    import rasterio
    from rasterio.enums import ColorInterp, Resampling
    from rasterio.windows import Window

    p = as_progress(progress)
    with rasterio.open(src_path) as src:
        bands = list(rgb) if rgb else ([1, 2, 3] if src.count >= 3 else [1, 1, 1])
        f = max(1, int(np.ceil(max(src.width, src.height) / 2048)))
        small = src.read(bands, out_shape=(3, max(src.height // f, 1), max(src.width // f, 1)),
                         resampling=Resampling.nearest).astype(np.float64)
        nodata = src.nodata
        valid = np.isfinite(small).all(axis=0)
        if nodata is not None and np.isfinite(nodata):
            valid &= (small != nodata).all(axis=0)
        limits = []
        for k in range(3):
            v = small[k][valid]
            lo, hi = (np.percentile(v, (2, 98)) if v.size > 16 else (0.0, 1.0))
            limits.append((float(lo), float(max(hi, lo + 1e-9))))
        profile = src.profile.copy()
        profile.update(count=4, dtype="uint8", nodata=None, compress="deflate", photometric="RGB", BIGTIFF="IF_SAFER")
        profile.pop("blockxsize", None)
        profile.pop("blockysize", None)
        if src.width >= 256 and src.height >= 256:
            profile.update(tiled=True, blockxsize=256, blockysize=256)
        else:
            profile.update(tiled=False)
        dst_path = Path(dst_path)
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        tiles = list(_tiles(src.height, src.width, tile))
        with rasterio.open(dst_path, "w", **profile) as dst:
            dst.colorinterp = [ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha]
            for k, (r0, c0, th, tw) in enumerate(tiles):
                win = Window(c0, r0, tw, th)
                data = src.read(bands, window=win).astype(np.float64)
                ok = np.isfinite(data).all(axis=0)
                if nodata is not None and np.isfinite(nodata):
                    ok &= (data != nodata).all(axis=0)
                rgba = np.zeros((4, th, tw), np.uint8)
                for i, (lo, hi) in enumerate(limits):
                    rgba[i] = np.clip((data[i] - lo) / (hi - lo) * 255, 0, 255).astype(np.uint8)
                rgba[3] = np.where(ok, 255, 0).astype(np.uint8)
                dst.write(rgba, window=win)
                p((k + 1) / len(tiles), "Anteprima a 8 bit")
            factors = [f for f in (2, 4, 8, 16, 32, 64) if min(src.width, src.height) // f >= 64]
            if factors:
                dst.build_overviews(factors, Resampling.average)
    return dst_path


def _version_entry(chunk, product: str, file: Path, name: str, params: dict) -> dict:
    vid = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    folder = chunk.data_dir / "versions" / product / vid
    folder.mkdir(parents=True)
    from ..core.versions import FILES

    dst = folder / FILES[product]
    import shutil

    shutil.move(str(file), str(dst))
    entry = {"id": vid, "name": name[:80], "created": datetime.now().isoformat(timespec="seconds"),
             "params": params, "files": {product: str(dst.relative_to(chunk.data_dir))}}
    chunk.meta.setdefault("versions", {}).setdefault(product, []).append(entry)
    return entry


def ortho_scene(chunk, scene: Path, name: str | None = None, rpc: Path | None = None, resolution: float | None = None,
                height_offset: float = 0.0, rgb: Sequence[int] | None = None, bounds=None, resampling: str = "cubic",
                progress: ProgressCallback | None = None) -> dict:
    """Orthorectify a scene on the DEM of the chunk: the full result (all the bands, the type of
    the scene) in ``<data>/satellite/<name>_ortho.tif``, an 8-bit RGBA rendition kept as a version
    of the orthomosaic named after the scene, both noted in ``meta["satellite"]["scenes"]``."""
    p = as_progress(progress)
    dem = chunk.dem_path if hasattr(chunk, "dem_path") else chunk.product_path("dem")
    if dem is None or not Path(dem).exists():
        raise ValueError("build the DEM first: the scene is orthorectified on it")
    scene = Path(scene)
    label = name or scene.stem
    safe = re.sub(r"[^\w.-]+", "_", label)[:60] or "scena"
    out = _folder(chunk) / f"{safe}_ortho.tif"
    result = orthorectify(scene, Path(dem), out, resolution=resolution, bounds=bounds, rpc=rpc,
                          height_offset=height_offset, resampling=resampling, progress=p.sub(0.0, 0.8))
    preview = _folder(chunk) / f"{safe}_rgb.tif"
    rgb_preview(out, preview, rgb=rgb, progress=p.sub(0.8, 0.18))
    params = {"source": "satellite", "scene": str(scene), "name": label, "resolution": result["resolution"],
              "height_offset": height_offset, "rpc": str(rpc) if rpc else "", "resampling": resampling,
              "gsd": result["gsd"], "bands": result["bands"], "dtype": result["dtype"],
              "file": str(out.relative_to(chunk.data_dir))}
    entry = _version_entry(chunk, "orthomosaic", preview, f"Satellite: {label}", params)
    rec = {"name": label, "scene": str(scene), "ortho": str(out.relative_to(chunk.data_dir)), "version": entry["id"],
           "resolution": result["resolution"], "gsd": result["gsd"], "bands": result["bands"], "dtype": result["dtype"],
           "bounds": list(result["bounds"]), "height_offset": height_offset, "filled": result["filled"],
           "created": entry["created"]}
    _record(chunk, "scenes", rec)
    p(1.0, f"{label}: {result['width']} x {result['height']} px")
    return {**result, "version": entry["id"], "preview": chunk.data_dir / entry["files"]["orthomosaic"], "record": rec}


def dsm_from_pair(chunk, scene_a: Path, scene_b: Path, name: str | None = None, rpc_a: Path | None = None,
                  rpc_b: Path | None = None, resolution: float | None = None, height_range: tuple[float, float] | None = None,
                  height_offset: float = 0.0, use_dem: bool = True, bounds=None,
                  progress: ProgressCallback | None = None) -> dict:
    """A surface model from a stereo pair, in the CRS of the chunk (the DEM of the chunk, when
    there is one and ``use_dem``, gives the reference heights and the search range), kept as a
    version of the DEM and in ``<data>/satellite/<name>_dsm.tif``."""
    p = as_progress(progress)
    dem = chunk.dem_path if hasattr(chunk, "dem_path") else chunk.product_path("dem")
    dem = Path(dem) if (use_dem and dem is not None and Path(dem).exists()) else None
    crs = chunk.crs
    if dem is None and (not crs or crs_mod.is_local(crs)):
        raise ValueError("the chunk needs a projected coordinate system (or a DEM) for a stereo DSM")
    label = name or f"{Path(scene_a).stem} + {Path(scene_b).stem}"
    safe = re.sub(r"[^\w.-]+", "_", label)[:60] or "stereo"
    out = _folder(chunk) / f"{safe}_dsm.tif"
    result = stereo_dsm(Path(scene_a), Path(scene_b), out, crs=None if dem is not None else crs, dem=dem,
                        resolution=resolution, bounds=bounds, height_range=height_range, rpc_a=rpc_a, rpc_b=rpc_b,
                        height_offset=height_offset, progress=p.sub(0.0, 0.95))
    import shutil

    copy = out.with_name(out.stem + "_version.tif")
    shutil.copy2(out, copy)
    params = {"source": "satellite_stereo", "scenes": [str(scene_a), str(scene_b)], "name": label,
              "resolution": result["resolution"], "height_offset": height_offset,
              "base_to_height": result["base_to_height"], "matched": result["matched"],
              "file": str(out.relative_to(chunk.data_dir))}
    entry = _version_entry(chunk, "dem", copy, f"DSM stereo: {label}", params)
    rec = {"name": label, "scenes": [str(scene_a), str(scene_b)], "dsm": str(out.relative_to(chunk.data_dir)),
           "version": entry["id"], "resolution": result["resolution"], "base_to_height": result["base_to_height"],
           "matched": result["matched"], "bounds": list(result["bounds"]), "created": entry["created"]}
    _record(chunk, "stereo", rec)
    p(1.0, f"{label}: {result['width']} x {result['height']} celle, base/altezza {result['base_to_height']:.2f}")
    return {**result, "version": entry["id"], "record": rec}
