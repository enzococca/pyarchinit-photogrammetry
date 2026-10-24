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
  the DEM (:func:`dsm_from_pair`);
* a **surface model from three or more scenes** (:func:`multi_view_dsm`): the RPCs of different
  acquisitions are not calibrated against each other (a few pixels of pointing error each), so
  they are first corrected with tie points (:func:`relative_bias`: one image shift per scene,
  found together with the ground points in one adjustment); the useful pairs are chosen by their
  intersection angle and the days between them (:func:`choose_pairs`), every pair gives a surface
  on a common grid and the surfaces are fused cell by cell (median of the heights that agree).
  Kept as a version of the DEM (:func:`dsm_from_scenes`).

Conventions: RPC sample and line follow the RPC00B standard, the centre of the first pixel at
(0, 0), as OpenCV counts pixels; heights are ellipsoidal WGS84, so a DEM in orthometric heights
needs the geoid undulation of the place as ``height_offset``. RPCs are read from the GeoTIFF
itself, from a ``_RPC.TXT`` / ``.RPB`` / ``.rpc`` sidecar or from the ``RPC_*.XML`` of a DIMAP
product (Pleiades, SPOT: their 1-based pixel numbering is brought to 0-based), or from the
comma-separated ``rpc_<name>.txt`` of the chips of the JHU/APL multi-view stereo benchmark
(WorldView-3), whose last numbers place the chip in the full image.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import warnings
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
              "RPC_{stem}.XML", "RPC_{core}.XML", "rpc_{stem}.txt")


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


_LIST_ORDER = ("LINE_OFF", "SAMP_OFF", "LAT_OFF", "LONG_OFF", "HEIGHT_OFF", "LINE_SCALE", "SAMP_SCALE", "LAT_SCALE",
               "LONG_SCALE", "HEIGHT_SCALE")


def parse_rpc_list(text: str) -> Rpc:
    """The comma-separated RPC of the chips of the JHU/APL multi-view stereo benchmark: the ten
    offsets and scales (line, sample, latitude, longitude, height offsets, then the scales in the
    same order), the 4 x 20 coefficients (line numerator and denominator, sample numerator and
    denominator), the validity box (min/max longitude and latitude) and the position of the chip in
    the full image (sample offset, line offset): the chip's pixel (c, r) is the image's
    (c + sample offset, r + line offset), so the offsets of the RPC are moved back by them."""
    nums = [float(v) for v in re.split(r"[,\s]+", text.strip()) if v]
    if len(nums) < 90:
        raise ValueError(f"RPC list: {len(nums)} numbers, at least 90 are needed")
    values: dict = dict(zip(_LIST_ORDER, nums[:10]))
    for k, name in enumerate(("LINE_NUM_COEFF", "LINE_DEN_COEFF", "SAMP_NUM_COEFF", "SAMP_DEN_COEFF")):
        values[name] = nums[10 + 20 * k:30 + 20 * k]
    rpc = _rpc_from_values(values)
    if len(nums) >= 96:  # the chip's place in the full image
        rpc.samp_off -= nums[94]
        rpc.line_off -= nums[95]
    return rpc


def _is_number_list(text: str) -> bool:
    head = text.strip()[:4000]
    return ":" not in head and "=" not in head and head.count(",") >= 80


def parse_rpc_text(text: str) -> Rpc:
    """An RPC00B text file (``KEY: value unit`` per line), an ``.RPB`` (``key = value;`` and
    ``key = ( v1, v2, ... );``) or the comma-separated list of :func:`parse_rpc_list`."""
    values: dict = {}
    if _is_number_list(text):
        return parse_rpc_list(text)
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
    scene = Path(scene)
    with _open_raster(scene) as src:
        if src.rpcs is not None:
            return Rpc.from_rasterio(src.rpcs)
    core = re.sub(r"^(IMG_)", "", scene.stem)
    for pattern in _SIDE_CARS:
        p = scene.with_name(pattern.format(stem=scene.stem, core=core))
        if p.exists():
            return read_rpc(scene, p)
    raise ValueError(f"{scene.name}: no RPC in the file and no sidecar (_RPC.TXT, .RPB, RPC_*.XML) beside it")


# ---------------------------------------------------------------------------- the scene
def _open_raster(path: Path):
    """``rasterio.open`` without the warning about a raster with no geotransform: a raw scene
    (or a chip of it) has none, its geometry is the RPC."""
    import rasterio
    from rasterio.errors import NotGeoreferencedWarning

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NotGeoreferencedWarning)
        return rasterio.open(path)


class _Scene:
    """A scene opened once per thread, its RPC, and the transformer to lon/lat of the CRS.
    ``rpc_file`` may be an :class:`Rpc` already (a corrected one)."""

    def __init__(self, path: Path, crs: str, rpc: Rpc | None = None, rpc_file: Path | Rpc | None = None):
        from pyproj import Transformer

        self.path = Path(path)
        if rpc is None and isinstance(rpc_file, Rpc):
            rpc, rpc_file = rpc_file, None
        self.rpc = rpc or read_rpc(self.path, rpc_file)
        with _open_raster(self.path) as src:
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
            s = self._local.src = _open_raster(self.path)
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


def _grid_profile(crs: str, origin, width: int, height: int, res: float) -> dict:
    from rasterio.transform import from_origin

    profile = {"driver": "GTiff", "width": width, "height": height, "count": 1, "dtype": "float32", "crs": crs,
               "transform": from_origin(origin[0], origin[1], res, res), "compress": "deflate", "BIGTIFF": "IF_SAFER",
               "nodata": NODATA}
    if width >= 256 and height >= 256:
        profile.update(tiled=True, blockxsize=256, blockysize=256)
    return profile


def _write_heights(out: Path, crs: str, origin, width: int, height: int, res: float, tile: int, heights, p,
                   label: str) -> int:
    """A float32 surface model written tile by tile: ``heights(tile)`` gives the heights of a tile
    (NaN = no data); no half-written file if it fails or is cancelled. Returns the tiles."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.windows import Window

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tiles = list(_tiles(height, width, tile))
    tmp = out.with_name(out.name + ".tmp.tif")
    try:
        with rasterio.open(tmp, "w", **_grid_profile(crs, origin, width, height, res)) as dst:
            for k, t in enumerate(tiles):
                r0, c0, th, tw = t
                z = np.asarray(heights(t), np.float32)
                z[~np.isfinite(z)] = NODATA
                dst.write(z[None], window=Window(c0, r0, tw, th))
                p(0.05 + 0.9 * (k + 1) / len(tiles), f"{label}: tassello {k + 1} di {len(tiles)}")
            factors = [f for f in (2, 4, 8, 16, 32) if min(width, height) // f >= 64]
            if factors:
                dst.build_overviews(factors, Resampling.nearest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(out)
    return len(tiles)


def _fill_small_holes(z: np.ndarray, res: float) -> np.ndarray:
    """Single-cell spikes out (:func:`engine.dem.remove_spikes`), holes up to 4 cells filled."""
    from .dem import remove_spikes

    if not np.isfinite(z).any():
        return z
    z, _ = remove_spikes(z, res)
    holes = ~np.isfinite(z)
    if holes.any() and not holes.all():
        from rasterio.fill import fillnodata

        z = np.asarray(fillnodata(z.astype(np.float32), mask=(~holes).astype(np.uint8), max_search_distance=4,
                                  smoothing_iterations=0), np.float64)
    return z


def stereo_dsm(scene_a: Path, scene_b: Path, out: Path, crs: str | None = None, dem: Path | None = None,
               resolution: float | None = None, bounds=None, height_range: tuple[float, float] | None = None,
               rpc_a: Path | Rpc | None = None, rpc_b: Path | Rpc | None = None, height_offset: float = 0.0,
               tile: int = 1024, block: int = 7, progress: ProgressCallback | None = None) -> dict:
    """A surface model from two scenes of the same ground taken from two directions. Tile by
    tile both scenes are resampled on the ground plane at a reference height (the median of
    ``dem`` there, else the RPC's height) along the direction of their baseline; a point above
    or below the plane is displaced along the line of sight of each scene, so the disparity
    found by semi-global matching gives its height, and its true position is recovered by
    taking the displacement back. ``height_range`` (CRS heights) bounds the search (from the
    DEM when there is one, else +-100 m); the result is a float32 GeoTIFF with no data where no
    match (small holes filled). ``rpc_a``/``rpc_b`` are files or :class:`Rpc` (corrected ones,
    see :func:`relative_bias`). Returns the base-to-height ratio and the matched fraction."""
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
    job = _StereoJob(a, b, d, st, origin, res, height_range, height_offset, block)
    n = _write_heights(out, crs, origin, width, height, res, tile, job.heights, p, "DSM stereo")
    p(1.0, f"{width} x {height} celle a {res:g} m")
    return {"path": Path(out), "width": width, "height": height, "resolution": res, "bounds": area,
            "base_to_height": float(np.mean(job.ratios)) if job.ratios else 0.0,
            "matched": job.matched / max(width * height, 1), "tiles": n, "height_reference": h_ref}


# Memory of OpenCV's full-scale SGBM (MODE_HH): a cost volume and its sum, 2 bytes each per pixel and
# disparity. A matching larger than the memory given to it (sgbm_memory()) is split into smaller tiles,
# down to MIN_SPLIT_TILE cells; each part repeats the margin, so splitting costs time: only when needed.
SGBM_BYTES_PER_COST = 4
MIN_SPLIT_TILE = 64


def _physical_memory() -> float:
    """Bytes of RAM of the computer (16 GB when it cannot be read)."""
    try:
        if os.name == "nt":
            import ctypes

            class _Status(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            status = _Status()
            status.dwLength = ctypes.sizeof(_Status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return float(status.ullTotalPhys)
            return 16.0e9
        return float(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, OSError, ValueError):
        return 16.0e9


def sgbm_memory() -> float:
    """Bytes one matching may take: 35 % of the computer's memory, between 1 and 8 GB."""
    return float(np.clip(0.35 * _physical_memory(), 1.0e9, 8.0e9))


class _StereoJob:
    """The tiles of :func:`stereo_dsm`: the reference plane, the rectification of both scenes
    on it along the baseline, the matching, the heights and their true positions. ``fill``
    False leaves the cells without a match empty (the pairs of :func:`multi_view_dsm`, fused
    afterwards)."""

    def __init__(self, a: _Scene, b: _Scene, d: _Dem | None, st: dict, origin, res: float, height_range,
                 height_offset: float, block: int, fill: bool = True):
        self.a, self.b, self.d, self.st = a, b, d, st
        self.origin, self.res, self.height_range, self.height_offset, self.block = origin, res, height_range, height_offset, block
        self.fill = fill
        self.budget = sgbm_memory()  # bytes one matching may take (shared out among the pairs of multi_view_dsm)
        self.matched = 0
        self.ratios: list[float] = []
        self._lock = threading.Lock()

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
            # 2 m beyond the DEM (20 cells at most: on a grid finer than satellites' it would be
            # hundreds of disparities), or a tenth of its range
            margin = max(min(2.0, 20.0 * self.res), 0.1 * (zmax - zmin))
            return h0, zmin - margin, zmax + margin
        if self.height_range:
            zmin, zmax = self.height_range[0] + off, self.height_range[1] + off
            return 0.5 * (zmin + zmax), zmin, zmax
        h0 = self.a.rpc.height_off
        return h0, h0 - 100.0, h0 + 100.0

    def heights(self, t) -> np.ndarray:
        """The heights (CRS, NaN where no match) of a tile ``(r0, c0, rows, cols)``."""
        import cv2

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
        bhat = base / ratio
        nhat = np.array([-bhat[1], bhat[0]])
        span = max(abs(h0 - zmin), abs(h0 - zmax))
        m = int(np.ceil(span * max(np.hypot(*ua), np.hypot(*ub), ratio) / res)) + 32
        H, W = th + 2 * m, tw + 2 * m
        # a point at height h: apparent position g + (h0 - h) u in each scene, so the disparity
        # d = x_a - x_b = (h0 - h) |base| / res
        d_lo = (h0 - zmax) * ratio / res
        d_hi = (h0 - zmin) * ratio / res
        min_disp = int(np.floor(d_lo)) - 1
        num = int(np.ceil((d_hi - min_disp + 1) / 16.0)) * 16
        if SGBM_BYTES_PER_COST * W * H * num > self.budget and min(th, tw) >= 2 * MIN_SPLIT_TILE:
            # the full-scale SGBM keeps a cost volume of W x H x disparities: a tile too large for
            # the memory given to it is matched in four parts (each with its own margin)
            h1, w1 = th // 2, tw // 2
            z = np.empty((th, tw), np.float64)
            for rr, cc, hh, ww in ((0, 0, h1, w1), (0, w1, h1, tw - w1), (h1, 0, th - h1, w1), (h1, w1, th - h1, tw - w1)):
                z[rr:rr + hh, cc:cc + ww] = self.heights((r0 + rr, c0 + cc, hh, ww))
            return z
        jj, ii = np.meshgrid((np.arange(W) - W / 2 + 0.5) * res, (np.arange(H) - H / 2 + 0.5) * res)
        gx = xc + jj * bhat[0] + ii * nhat[0]
        gy = yc + jj * bhat[1] + ii * nhat[1]
        imgs = []
        for s in (a, b):  # both scenes on the plane h0, the x axis along the baseline
            col, row = s.pixels(gx, gy, np.full(gx.shape, h0))
            data, ok = s.resample(col, row, cv2.INTER_LINEAR)
            band = data[0].astype(np.float32) if s.count == 1 else data[:3].astype(np.float32).mean(axis=0)
            imgs.append((_to_uint8(band, ok), ok))
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
        if self.fill:
            z = _fill_small_holes(z, res)
        else:
            from .dem import remove_spikes

            if got.any():
                z, _ = remove_spikes(z, res)
        z = z - self.height_offset
        with self._lock:
            self.ratios.append(ratio)
            self.matched += int(np.isfinite(z).sum())
        return z


# ---------------------------------------------------------------------------- three or more scenes
_MONTHS = {m: i + 1 for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT",
                                           "NOV", "DEC"))}
_DG_TIME = re.compile(r"(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})(\d{2})(\d{2})(\d{2})")
_ISO_DATE = re.compile(r"(?<!\d)(19\d{2}|20\d{2})-?(0[1-9]|1[0-2])-?(0[1-9]|[12]\d|3[01])(?:[T_-]?(\d{2})(\d{2})(\d{2}))?(?!\d)")


def acquisition_date(scene: Path) -> datetime | None:
    """When a scene was taken: the acquisition metadata of the file (NITF, TIFF date) or the
    name (the DigitalGlobe catalogue form ``15SEP01135603`` = 2015-09-01 13:56:03, or an ISO
    date ``2015-09-01`` / ``20150901``); None if nothing says it."""
    scene = Path(scene)
    try:
        with _open_raster(scene) as src:
            tags = {**src.tags()}
            for ns in ("IMD", "IMAGERY"):
                tags.update(src.tags(ns=ns) or {})
    except Exception:  # noqa: BLE001 - a scene rasterio cannot read has its name left
        tags = {}
    for key in ("NITF_STDIDC_ACQUISITION_DATE", "NITF_IDATIM", "ACQUISITIONDATETIME", "FIRSTLINETIME",
                "IMAGE_1.FIRSTLINETIME", "TIFFTAG_DATETIME"):
        v = str(tags.get(key, "")).strip().strip('"')
        if not v:
            continue
        for fmt in ("%Y%m%d%H%M%S", "%Y:%m:%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(v[:len(datetime(2000, 1, 1).strftime(fmt))], fmt)
            except ValueError:
                continue
    name = scene.name.upper()
    m = _DG_TIME.search(name)
    if m:
        yy, mon, dd, hh, mi, ss = m.groups()
        try:
            return datetime(2000 + int(yy), _MONTHS[mon], int(dd), int(hh), int(mi), int(ss))
        except ValueError:
            pass
    m = _ISO_DATE.search(scene.name)
    if m:
        y, mo, dd, hh, mi, ss = m.groups()
        try:
            return datetime(int(y), int(mo), int(dd), int(hh or 0), int(mi or 0), int(ss or 0))
        except ValueError:
            return None
    return None


def _view(s: _Scene, x: float, y: float, h: float) -> tuple[np.ndarray, np.ndarray]:
    """The parallax vector (ground metres per metre of height) and the unit vector towards the
    satellite (east, north, up) of a scene at a point."""
    u = _parallax(s, x, y, h)
    v = np.array([u[0], u[1], 1.0])
    return u, v / np.linalg.norm(v)


def choose_pairs(views: Sequence[np.ndarray], dates: Sequence[datetime | None] | None = None, min_angle: float = 5.0,
                 max_angle: float = 30.0, max_pairs: int = 6, target_angle: float = 15.0) -> list[dict]:
    """The useful stereo pairs among scenes: intersection angle (between the lines of sight)
    between ``min_angle`` and ``max_angle`` degrees, the closest in time first (the ground,
    the shadows and the vegetation change between acquisitions), then the angle nearest
    ``target_angle``; at most ``max_pairs``, spread so that every scene is used before one is used
    twice when possible. ``views`` are unit vectors towards the satellites. Returns
    ``{"pair": (i, j), "angle": degrees, "days": days or None}``, best first."""
    n = len(views)
    dates = list(dates) if dates is not None else [None] * n
    cands = []
    for i in range(n):
        for j in range(i + 1, n):
            ang = float(np.degrees(np.arccos(np.clip(np.dot(views[i], views[j]), -1.0, 1.0))))
            days = (abs((dates[i] - dates[j]).total_seconds()) / 86400.0
                    if dates[i] is not None and dates[j] is not None else None)
            cands.append({"pair": (i, j), "angle": ang, "days": days})
    good = [c for c in cands if min_angle <= c["angle"] <= max_angle]
    if not good:  # nothing in the range: the pairs nearest to it
        good = sorted(cands, key=lambda c: min(abs(c["angle"] - min_angle), abs(c["angle"] - max_angle)))[:1]
        good = [c for c in good if c["angle"] >= 1.0]
    good.sort(key=lambda c: (c["days"] if c["days"] is not None else 0.0, abs(c["angle"] - target_angle)))
    chosen: list[dict] = []
    used: set[int] = set()
    for c in good:  # first the pairs that bring a scene not used yet, then the others, best first
        if len(chosen) < max_pairs and not set(c["pair"]) <= used:
            chosen.append(c)
            used.update(c["pair"])
    for c in good:
        if len(chosen) < max_pairs and c not in chosen:
            chosen.append(c)
    return chosen


def _scene_window(s: _Scene, area, hmin: float, hmax: float, margin: int = 32) -> tuple[int, int, int, int] | None:
    """The pixels of a scene that see the area between two heights: ``(c0, r0, c1, r1)``."""
    xs = np.array([area[0], area[2], area[2], area[0]] * 2, float)
    ys = np.array([area[1], area[1], area[3], area[3]] * 2, float)
    hs = np.array([hmin] * 4 + [hmax] * 4, float)
    col, row = s.pixels(xs, ys, hs)
    if not (np.isfinite(col).all() and np.isfinite(row).all()):
        return None
    c0 = max(int(np.floor(col.min())) - margin, 0)
    r0 = max(int(np.floor(row.min())) - margin, 0)
    c1 = min(int(np.ceil(col.max())) + margin + 1, s.width)
    r1 = min(int(np.ceil(row.max())) + margin + 1, s.height)
    return (c0, r0, c1, r1) if c1 - c0 > 16 and r1 - r0 > 16 else None


def _features(s: _Scene, window, max_side: int, max_points: int):
    """SIFT key points (scene pixels, centre of the first pixel at 0) and descriptors of a
    window of a scene, read reduced to ``max_side`` at most."""
    import cv2

    c0, r0, c1, r1 = window
    data = s.read_window(c0, r0, c1, r1)
    band = data[0].astype(np.float32) if s.count == 1 else data[:3].astype(np.float32).mean(axis=0)
    ok = np.ones(band.shape, bool) if s.nodata is None else (data != s.nodata).all(axis=0)
    img = _to_uint8(band, ok)
    f = max(1.0, max(img.shape) / float(max_side))
    if f > 1.0:
        img = cv2.resize(img, (int(round(img.shape[1] / f)), int(round(img.shape[0] / f))), interpolation=cv2.INTER_AREA)
        fx, fy = band.shape[1] / img.shape[1], band.shape[0] / img.shape[0]
    else:
        fx = fy = 1.0
    sift = cv2.SIFT_create(nfeatures=max_points)
    kps, desc = sift.detectAndCompute(img, None)
    if desc is None or not kps:
        return np.zeros((0, 2)), np.zeros((0, 128), np.float32)
    pts = np.array([k.pt for k in kps], float)
    pts[:, 0] = (pts[:, 0] + 0.5) * fx - 0.5 + c0
    pts[:, 1] = (pts[:, 1] + 0.5) * fy - 0.5 + r0
    return pts, desc


def _match(pa, da, pb, db, ratio: float = 0.75, threshold: float = 1.5) -> np.ndarray:
    """Indices ``(k, 2)`` of the matches of two scenes: ratio test both ways, then the
    epipolar geometry (fundamental matrix with RANSAC: over the chip of a pushbroom scene the
    RPC geometry is nearly affine)."""
    import cv2

    if len(pa) < 8 or len(pb) < 8:
        return np.zeros((0, 2), int)
    bf = cv2.BFMatcher(cv2.NORM_L2)

    def oneway(d1, d2):
        out = {}
        for m in bf.knnMatch(d1, d2, k=2):
            if len(m) == 2 and m[0].distance < ratio * m[1].distance:
                out[m[0].queryIdx] = m[0].trainIdx
        return out

    ab, ba = oneway(da, db), oneway(db, da)
    pairs = np.array([(i, j) for i, j in ab.items() if ba.get(j) == i], int).reshape(-1, 2)
    if len(pairs) < 12:
        return np.zeros((0, 2), int)
    cv2.setRNGSeed(0)  # RANSAC draws from the thread's generator: the same tie points on every run
    _, mask = cv2.findFundamentalMat(pa[pairs[:, 0]].astype(np.float32), pb[pairs[:, 1]].astype(np.float32),
                                     cv2.FM_RANSAC, threshold, 0.999)
    if mask is None:
        return np.zeros((0, 2), int)
    return pairs[mask.ravel() > 0]


def tie_points(scenes: Sequence[_Scene], area, hmin: float, hmax: float, max_side: int = 4000,
               max_points: int = 20000, max_tracks: int = 20000, progress: ProgressCallback | None = None) -> dict:
    """Tie points among scenes over an area: SIFT in the part of every scene that sees it,
    matches of every pair checked by their epipolar geometry, joined in tracks (a track that
    meets twice the same scene is dropped). Returns ``obs`` = ``(track, scene, column, row)``
    rows, the number of tracks and the matches per pair."""
    p = as_progress(progress)
    n = len(scenes)
    feats = []
    for i, s in enumerate(scenes):
        win = _scene_window(s, area, hmin, hmax)
        feats.append(_features(s, win, max_side, max_points) if win else (np.zeros((0, 2)), np.zeros((0, 128), np.float32)))
        p(0.5 * (i + 1) / n, f"Punti di legame: scena {i + 1} di {n}")
    offsets = np.cumsum([0] + [len(f[0]) for f in feats])
    parent = np.arange(offsets[-1])

    def find(k):
        root = k
        while parent[root] != root:
            root = parent[root]
        while parent[k] != root:
            parent[k], k = root, parent[k]
        return root

    per_pair = {}
    pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]
    for q, (i, j) in enumerate(pairs):
        m = _match(feats[i][0], feats[i][1], feats[j][0], feats[j][1])
        per_pair[(i, j)] = len(m)
        for a, b in m:
            ra, rb = find(offsets[i] + a), find(offsets[j] + b)
            if ra != rb:
                parent[rb] = ra
        p(0.5 + 0.5 * (q + 1) / max(len(pairs), 1), f"Punti di legame: coppia {q + 1} di {len(pairs)}")
    roots = np.array([find(k) for k in range(offsets[-1])], int)
    scene_of = np.repeat(np.arange(n), np.diff(offsets))
    pts = np.concatenate([f[0] for f in feats]) if offsets[-1] else np.zeros((0, 2))
    _, inverse, counts = np.unique(roots, return_inverse=True, return_counts=True)
    keep = counts[inverse] >= 2
    # a track seen twice in one scene is a wrong join
    key = inverse * n + scene_of
    _, kinv, kcnt = np.unique(key, return_inverse=True, return_counts=True)
    bad_tracks = np.unique(inverse[kcnt[kinv] > 1])
    keep &= ~np.isin(inverse, bad_tracks)
    tracks = np.unique(inverse[keep])
    rng = np.random.default_rng(0)
    if len(tracks) > max_tracks:
        tracks = np.sort(rng.choice(tracks, max_tracks, replace=False))
        keep &= np.isin(inverse, tracks)
    remap = np.full(inverse.max() + 1 if len(inverse) else 0, -1, int)
    remap[tracks] = np.arange(len(tracks))
    obs = np.column_stack([remap[inverse[keep]], scene_of[keep], pts[keep]]) if keep.any() else np.zeros((0, 4))
    return {"obs": obs, "tracks": int(len(tracks)), "matches": {f"{i}-{j}": v for (i, j), v in per_pair.items()}}


def _adjust(scenes: Sequence[_Scene], obs: np.ndarray, h0: float, sigma_bias: float, free: bool,
            iterations: int = 8) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Ground points and image shifts that fit the observations: Gauss-Newton on
    ``project_i(X) + b_i = observation`` with the points eliminated (Schur complement on the
    2 N shifts), Huber weights from the third iteration, a weak prior ``b ~ 0`` (``sigma_bias``
    pixels) that fixes what the observations cannot see (a common shift of all the scenes and
    of the heights). ``free`` False keeps the shifts at 0 (the plain triangulation). Returns the
    points (T, 3: x, y, h), the shifts (N, 2) and the residuals (pixels) of the observations."""
    n = len(scenes)
    t_of = obs[:, 0].astype(int)
    i_of = obs[:, 1].astype(int)
    uv = obs[:, 2:4].astype(float)
    T = int(t_of.max()) + 1 if len(t_of) else 0
    X = np.zeros((T, 3))
    first = np.zeros(T, int)
    first[t_of[::-1]] = np.arange(len(t_of))[::-1]
    for i, s in enumerate(scenes):
        sel = i_of[first] == i
        if sel.any():
            k = first[sel]
            x, y = s.ground_xy(uv[k, 0], uv[k, 1], np.full(len(k), h0))
            X[sel] = np.column_stack([x, y, np.full(len(k), h0)])
    bias = np.zeros((n, 2))
    lam = (1.0 / sigma_bias) ** 2 if free else 1e12
    steps = np.eye(3)
    res = np.zeros_like(uv)
    for it in range(iterations):
        P = np.zeros_like(uv)
        J = np.zeros((len(uv), 2, 3))
        for i, s in enumerate(scenes):
            sel = i_of == i
            if not sel.any():
                continue
            Xi = X[t_of[sel]]
            c, r = s.pixels(Xi[:, 0], Xi[:, 1], Xi[:, 2])
            P[sel] = np.column_stack([c, r])
            for k in range(3):
                d = Xi + steps[k]
                ck, rk = s.pixels(d[:, 0], d[:, 1], d[:, 2])
                J[sel, 0, k] = ck - c
                J[sel, 1, k] = rk - r
        res = uv - P - bias[i_of]
        if it >= 2:
            norm = np.hypot(res[:, 0], res[:, 1])
            k_h = max(1.0, 2.5 * 1.4826 * float(np.median(norm)))
            w = np.where(norm <= k_h, 1.0, k_h / np.maximum(norm, 1e-12))
        else:
            w = np.ones(len(uv))
        JT = np.transpose(J, (0, 2, 1)) * w[:, None, None]  # (k, 3, 2) = w J^T
        A = np.zeros((T, 3, 3))
        np.add.at(A, t_of, JT @ J)
        A += np.eye(3) * 1e-9
        gx = np.zeros((T, 3))
        np.add.at(gx, t_of, np.einsum("kab,kb->ka", JT, res))
        F = np.zeros((T, n, 3, 2))
        F[t_of, i_of] = JT
        Ainv = np.linalg.inv(A)
        G = np.einsum("tac,tjcd->tjad", Ainv, F)
        S = np.zeros((n, 2, n, 2))
        wsum = np.bincount(i_of, weights=w, minlength=n)
        for i in range(n):
            S[i, :, i, :] += np.eye(2) * (wsum[i] + lam)
        S -= np.einsum("tiab,tjad->ibjd", F, G)
        rhs = np.zeros((n, 2))
        np.add.at(rhs, i_of, res * w[:, None])
        rhs -= lam * bias
        rhs -= np.einsum("tiab,ta->ib", F, np.einsum("tac,tc->ta", Ainv, gx))
        db = np.linalg.solve(S.reshape(2 * n, 2 * n), rhs.reshape(-1)).reshape(n, 2)
        if not free:
            db[:] = 0.0
        dX = np.einsum("tac,tc->ta", Ainv, gx - np.einsum("tiab,ib->ta", F, db))
        X += dX
        bias += db
        if np.abs(dX).max(initial=0) < 1e-4 and np.abs(db).max(initial=0) < 1e-4:
            break
    P = np.zeros_like(uv)
    for i, s in enumerate(scenes):
        sel = i_of == i
        if sel.any():
            Xi = X[t_of[sel]]
            c, r = s.pixels(Xi[:, 0], Xi[:, 1], Xi[:, 2])
            P[sel] = np.column_stack([c, r])
    res = uv - P - bias[i_of]
    return X, bias, res


def corrected_rpc(rpc: Rpc, shift) -> Rpc:
    """An RPC whose image positions are moved by ``shift`` (columns, rows)."""
    d = rpc.to_dict()
    d["samp_off"] += float(shift[0])
    d["line_off"] += float(shift[1])
    return Rpc.from_dict(d)


def relative_bias(scenes: Sequence[_Scene], area, hmin: float, hmax: float, sigma_bias: float = 10.0,
                  max_side: int = 4000, progress: ProgressCallback | None = None) -> dict:
    """The relative pointing correction of scenes whose RPCs were not adjusted together: one
    image shift (columns, rows) per scene, found with the ground points of the tie points in one
    adjustment (:func:`_adjust`) so that every tie point is one ground point in all the scenes.
    The shifts are relative: the weak prior keeps their mean near zero, so the result stays where
    the RPCs together put it (no absolute correction without ground control). Observations more
    than 4 pixels off after the adjustment are dropped and the adjustment repeated. Returns the
    corrected :class:`Rpc` per scene, the shifts and the residuals (RMS, pixels) before and after."""
    p = as_progress(progress)
    tp = tie_points(scenes, area, hmin, hmax, max_side=max_side, progress=p.sub(0.0, 0.8))
    obs = tp["obs"]
    n = len(scenes)
    seen = np.bincount(obs[:, 1].astype(int), minlength=n) if len(obs) else np.zeros(n, int)
    if len(obs) == 0 or (seen < 20).any():
        log.warning("relative RPC correction: too few tie points (%s): RPCs left as they are", seen.tolist())
        return {"rpcs": [s.rpc for s in scenes], "shifts": [[0.0, 0.0]] * n, "tracks": tp["tracks"],
                "observations": seen.tolist(), "rms_before": None, "rms_after": None, "applied": False,
                "matches": tp["matches"]}
    h0 = 0.5 * (hmin + hmax)
    _, _, res0 = _adjust(scenes, obs, h0, sigma_bias, free=False)
    norm0 = np.hypot(res0[:, 0], res0[:, 1])
    p(0.85, "Correzione relativa degli RPC")
    _, bias, res = _adjust(scenes, obs, h0, sigma_bias, free=True)
    keep = np.hypot(res[:, 0], res[:, 1]) <= max(4.0, 3 * 1.4826 * float(np.median(np.hypot(res[:, 0], res[:, 1]))))
    if keep.sum() < len(keep):
        kept = obs[keep]
        tracks, counts = np.unique(kept[:, 0], return_counts=True)
        kept = kept[np.isin(kept[:, 0], tracks[counts >= 2])]
        _, kept[:, 0] = np.unique(kept[:, 0], return_inverse=True)
        if len(kept) and (np.bincount(kept[:, 1].astype(int), minlength=n) >= 20).all():
            obs = kept
            _, bias, res = _adjust(scenes, obs, h0, sigma_bias, free=True)
    norm = np.hypot(res[:, 0], res[:, 1])
    p(1.0, f"Correzione relativa: {len(np.unique(obs[:, 0]))} punti di legame, "
           f"{float(np.sqrt(np.mean(norm0 ** 2))):.2f} -> {float(np.sqrt(np.mean(norm ** 2))):.2f} px")
    return {"rpcs": [corrected_rpc(s.rpc, b) for s, b in zip(scenes, bias)], "shifts": bias.tolist(),
            "tracks": int(len(np.unique(obs[:, 0]))), "observations": np.bincount(obs[:, 1].astype(int), minlength=n).tolist(),
            "rms_before": float(np.sqrt(np.mean(norm0 ** 2))), "median_before": float(np.median(norm0)),
            "rms_after": float(np.sqrt(np.mean(norm ** 2))), "median_after": float(np.median(norm)),
            "applied": True, "matches": tp["matches"]}


def fuse_heights(stack: np.ndarray, agree: float, min_count: int = 2) -> tuple[np.ndarray, np.ndarray]:
    """Cell by cell fusion of the surfaces of several pairs ``(pairs, rows, cols)`` (NaN = no
    match): the median of the heights within ``agree`` of the median of all, kept where at least
    ``min_count`` agree. Returns the heights and the number that agreed."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        med = np.nanmedian(stack, axis=0)
        close = np.abs(stack - med[None]) <= agree
        n = close.sum(axis=0)
        z = np.nanmedian(np.where(close, stack, np.nan), axis=0)
    z[n < min_count] = np.nan
    return z, n


def multi_view_dsm(scenes: Sequence[Path], out: Path, crs: str | None = None, dem: Path | None = None,
                   resolution: float | None = None, bounds=None, height_range: tuple[float, float] | None = None,
                   rpcs: Sequence[Path | Rpc | None] | None = None, height_offset: float = 0.0,
                   pairs: Sequence[tuple[int, int]] | None = None, max_pairs: int = 6, min_angle: float = 5.0,
                   max_angle: float = 30.0, dates: Sequence[datetime | None] | None = None, correct_bias: bool = True,
                   min_count: int | None = None, agree: float | None = None, tile: int = 1024, block: int = 7,
                   workers: int | None = None, progress: ProgressCallback | None = None) -> dict:
    """A surface model from two or more scenes. The RPCs are first corrected against each other
    with tie points (:func:`relative_bias`, ``correct_bias``); the pairs are those given or
    those :func:`choose_pairs` finds (intersection angle between ``min_angle`` and ``max_angle``,
    closest dates first; dates from :func:`acquisition_date` unless given); tile by tile every pair
    gives its heights on the common grid (as :func:`stereo_dsm`, without filling) and the heights
    are fused (:func:`fuse_heights`: the median of those within ``agree`` metres of the median,
    default 2.5 cells over the median base-to-height ratio; at least ``min_count`` pairs, default
    2 with three pairs or more), then small holes are filled. Returns, beside the grid, the pairs
    (angle, days, base to height, matched fraction, median offset from the fused surface), the
    correction and the fraction of cells with a height."""
    p = as_progress(progress)
    scenes = [Path(s) for s in scenes]
    if len(scenes) < 2:
        raise ValueError("at least two scenes are needed")
    rpcs = list(rpcs) if rpcs is not None else [None] * len(scenes)
    d = _Dem(dem) if dem is not None else None
    crs = crs or (d.crs if d is not None else None)
    if not crs or crs_mod.is_local(crs) or crs_mod.is_geographic(crs):
        raise ValueError("a projected coordinate system (metres) is needed: the DEM's or crs=")
    ss = [_Scene(s, crs, rpc_file=r) for s, r in zip(scenes, rpcs)]
    st = d.stats() if d is not None else {"median": None, "min": None, "max": None}
    if st["median"] is not None:
        h_ref = st["median"] + height_offset
        hmin, hmax = st["min"] + height_offset, st["max"] + height_offset
    elif height_range:
        hmin, hmax = height_range[0] + height_offset, height_range[1] + height_offset
        h_ref = 0.5 * (hmin + hmax)
    else:
        h_ref = ss[0].rpc.height_off
        hmin, hmax = h_ref - 100.0, h_ref + 100.0
    if height_range:
        hmin, hmax = height_range[0] + height_offset, height_range[1] + height_offset
    area = _intersect([s.footprint(h_ref) for s in ss], [d.bounds if d is not None else None, bounds])
    xc, yc = 0.5 * (area[0] + area[2]), 0.5 * (area[1] + area[3])
    views = [_view(s, xc, yc, h_ref)[1] for s in ss]
    dates = list(dates) if dates is not None else [acquisition_date(s) for s in scenes]
    if pairs is None:
        chosen = choose_pairs(views, dates, min_angle=min_angle, max_angle=max_angle, max_pairs=max_pairs)
    else:
        chosen = []
        for i, j in pairs:
            ang = float(np.degrees(np.arccos(np.clip(np.dot(views[i], views[j]), -1, 1))))
            days = (abs((dates[i] - dates[j]).total_seconds()) / 86400.0
                    if dates[i] is not None and dates[j] is not None else None)
            chosen.append({"pair": (int(i), int(j)), "angle": ang, "days": days})
    if not chosen:
        raise ValueError("no pair of the scenes sees the ground from directions different enough")
    used = sorted({i for c in chosen for i in c["pair"]})
    bias = None
    if correct_bias:
        sub = [ss[i] for i in used]
        bias = relative_bias(sub, area, hmin, hmax, progress=p.sub(0.0, 0.25))
        for i, r in zip(used, bias["rpcs"]):
            ss[i] = _Scene(scenes[i], crs, rpc=r)
        bias["scenes"] = used
    p0 = 0.25 if correct_bias else 0.0
    res = float(resolution) if resolution else _round_res(max(ss[i].gsd(h_ref) for i in used))
    origin, width, height = _grid(area, res)
    jobs = [_StereoJob(ss[c["pair"][0]], ss[c["pair"][1]], d, st, origin, res, height_range, height_offset, block,
                       fill=False) for c in chosen]
    ratios = [float(np.hypot(*(_parallax(ss[c["pair"][0]], xc, yc, h_ref) - _parallax(ss[c["pair"][1]], xc, yc, h_ref))))
              for c in chosen]
    agree = float(agree) if agree else max(2.5 * res / float(np.median(ratios)), 2.0 * res)
    if min_count is None:
        min_count = 2 if len(chosen) >= 3 else 1
    offsets: list[list[float]] = [[] for _ in chosen]
    agreed = [0]
    n_workers = workers or min(len(jobs), os.cpu_count() or 1, 8)
    for j in jobs:  # the pairs matched at the same time share the memory of one matching
        j.budget = max(j.budget / n_workers, 2.0e8)
    pool = ThreadPoolExecutor(max_workers=n_workers)

    def heights(t):
        stack = np.stack(list(pool.map(lambda j: j.heights(t), jobs)))
        z, n = fuse_heights(stack, agree, min_count)
        for k in range(len(jobs)):  # how each pair sits against the fused surface (a sample)
            ok = np.isfinite(stack[k]) & np.isfinite(z)
            if ok.sum() > 50:
                offsets[k].append(float(np.median((stack[k] - z)[ok])))
        agreed[0] += int(np.isfinite(z).sum())
        return _fill_small_holes(z, res)

    try:
        n_tiles = _write_heights(out, crs, origin, width, height, res, tile, heights, p.sub(p0, 1.0 - p0),
                                 f"DSM da {len(chosen)} coppie")
    finally:
        pool.shutdown(wait=True)
    import rasterio

    with rasterio.open(out) as src:
        filled = float((src.read(1) != NODATA).mean())
    cells = max(width * height, 1)
    pair_info = [{**c, "scenes": [str(scenes[c["pair"][0]]), str(scenes[c["pair"][1]])], "base_to_height": r,
                  "matched": j.matched / cells, "offset": float(np.median(o)) if o else None}
                 for c, r, j, o in zip(chosen, ratios, jobs, offsets)]
    p(1.0, f"{width} x {height} celle a {res:g} m da {len(chosen)} coppie")
    return {"path": Path(out), "width": width, "height": height, "resolution": res, "bounds": area,
            "pairs": pair_info, "bias": bias, "agree": agree, "min_count": min_count,
            "fused": agreed[0] / cells, "filled": filled, "tiles": n_tiles, "height_reference": h_ref,
            "base_to_height": float(np.median(ratios))}


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


def dsm_from_scenes(chunk, scenes: Sequence[Path], name: str | None = None, rpcs: Sequence[Path | None] | None = None,
                    resolution: float | None = None, height_range: tuple[float, float] | None = None,
                    height_offset: float = 0.0, use_dem: bool = True, bounds=None, correct_bias: bool = True,
                    max_pairs: int = 6, min_angle: float = 5.0, max_angle: float = 30.0,
                    progress: ProgressCallback | None = None) -> dict:
    """A surface model from two or more scenes (:func:`multi_view_dsm`: relative correction of the
    RPCs, pairs chosen by angle and date, fusion), in the CRS of the chunk, kept as a version of the
    DEM and in ``<data>/satellite/<name>_dsm.tif``; the pairs and the correction noted in
    ``meta["satellite"]["stereo"]``."""
    import shutil

    p = as_progress(progress)
    scenes = [Path(s) for s in scenes]
    dem = chunk.dem_path if hasattr(chunk, "dem_path") else chunk.product_path("dem")
    dem = Path(dem) if (use_dem and dem is not None and Path(dem).exists()) else None
    crs = chunk.crs
    if dem is None and (not crs or crs_mod.is_local(crs)):
        raise ValueError("the chunk needs a projected coordinate system (or a DEM) for a stereo DSM")
    label = name or " + ".join(s.stem[:20] for s in scenes[:3]) + (f" + {len(scenes) - 3}" if len(scenes) > 3 else "")
    safe = re.sub(r"[^\w.-]+", "_", label)[:60] or "stereo"
    out = _folder(chunk) / f"{safe}_dsm.tif"
    result = multi_view_dsm(scenes, out, crs=None if dem is not None else crs, dem=dem, resolution=resolution,
                            bounds=bounds, height_range=height_range, rpcs=rpcs, height_offset=height_offset,
                            max_pairs=max_pairs, min_angle=min_angle, max_angle=max_angle, correct_bias=correct_bias,
                            progress=p.sub(0.0, 0.95))
    copy = out.with_name(out.stem + "_version.tif")
    shutil.copy2(out, copy)
    pairs = [{"scenes": list(c["pair"]), "angle": c["angle"], "days": c["days"], "base_to_height": c["base_to_height"],
              "matched": c["matched"], "offset": c["offset"]} for c in result["pairs"]]
    bias = result["bias"]
    correction = None if bias is None else {k: bias[k] for k in ("shifts", "tracks", "rms_before", "rms_after", "applied",
                                                                  "scenes")}
    params = {"source": "satellite_multiview", "scenes": [str(s) for s in scenes], "name": label,
              "resolution": result["resolution"], "height_offset": height_offset, "pairs": pairs,
              "correction": correction, "file": str(out.relative_to(chunk.data_dir))}
    entry = _version_entry(chunk, "dem", copy, f"DSM stereo: {label}", params)
    rec = {"name": label, "scenes": [str(s) for s in scenes], "dsm": str(out.relative_to(chunk.data_dir)),
           "version": entry["id"], "resolution": result["resolution"], "base_to_height": result["base_to_height"],
           "matched": result["filled"], "pairs": pairs, "correction": correction, "bounds": list(result["bounds"]),
           "created": entry["created"]}
    _record(chunk, "stereo", rec)
    p(1.0, f"{label}: {result['width']} x {result['height']} celle da {len(pairs)} coppie")
    return {**result, "version": entry["id"], "record": rec, "matched": result["filled"]}
