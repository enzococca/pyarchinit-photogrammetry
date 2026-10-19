"""Munsell colour of the units from the orthomosaic, calibrated with a colour chart.

* :func:`calibrate_chart` - finds an X-Rite / Calibrite ColorChecker Classic (24 patches) in an
  image (OpenCV's ``mcc``) and computes the 3 x 3 matrix that brings its patches, in linear RGB,
  to their reference values (``ccm``, loss as CIEDE2000): the colours of that survey become
  comparable with those of another day and another camera.
* :func:`correct` - colours (sRGB, 0-1) through the matrix.
* :func:`munsell` - the Munsell notation of an sRGB colour (illuminant C, as the renotation
  data; `colour-science <https://www.colour-science.org>`_, extra ``munsell``) and the nearest
  chip of a soil colour chart (hues every 2.5, values 2.5-8, chromas 1-8: ``10YR 4/3``).
* :func:`area_colour` - the colour of the orthomosaic inside a polygon: the median of its pixels
  after leaving out the darkest and the brightest tenth (shadows, glare, labels), calibrated.
"""
from __future__ import annotations

import math
import os

import numpy as np

CHART_VALUES = (2.5, 3, 4, 5, 6, 7, 8)  # the values and chromas of the pages of a soil colour chart
CHART_CHROMAS = (1, 2, 3, 4, 6, 8)
HUE_FAMILIES = ("R", "YR", "Y", "GY", "G", "BG", "B", "PB", "P", "RP")  # in the order of the hue circle


def srgb_to_linear(c: np.ndarray) -> np.ndarray:
    c = np.asarray(c, float)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(c: np.ndarray) -> np.ndarray:
    c = np.clip(np.asarray(c, float), 0, 1)
    return np.where(c <= 0.0031308, 12.92 * c, 1.055 * c ** (1 / 2.4) - 0.055)


def calibrate_chart(image_rgb: np.ndarray) -> dict:
    """The colour correction from a ColorChecker Classic in ``image_rgb`` (uint8 RGB):
    ``{"ccm": 3 x 3 list, "loss": mean CIEDE2000 of the patches after correction, "box":
    corners of the chart in the image}``; ValueError when there is no chart."""
    import cv2

    img = np.ascontiguousarray(np.asarray(image_rgb)[..., :3][..., ::-1])  # OpenCV wants BGR
    det = cv2.mcc.CCheckerDetector_create()
    det.setColorChartType(cv2.mcc.MCC24)
    if not det.process(img, 1):
        raise ValueError("no ColorChecker (24 patches) found in the image")
    checker = det.getBestColorChecker()
    rgb = np.asarray(checker.getChartsRGB(), float)[:, 1].reshape(24, 3) / 255.0  # mean of each patch
    model = cv2.ccm.ColorCorrectionModel(rgb.reshape(24, 1, 3), cv2.ccm.COLORCHECKER_MACBETH)
    model.setColorSpace(cv2.ccm.COLOR_SPACE_SRGB)
    model.setCcmType(cv2.ccm.CCM_LINEAR)
    model.setLinearization(cv2.ccm.LINEARIZATION_GAMMA)
    model.setLinearizationGamma(2.2)
    model.compute()
    return {"ccm": np.asarray(model.getColorCorrectionMatrix(), float).tolist(), "loss": float(model.getLoss()),
            "box": np.asarray(checker.getBox(), float).tolist(), "patches": rgb.tolist()}


def correct(rgb: np.ndarray, ccm) -> np.ndarray:
    """sRGB colours (..., 3) in 0-1 corrected with the matrix of :func:`calibrate_chart`
    (applied in linear light, the linearisation of the calibration)."""
    if ccm is None:
        return np.asarray(rgb, float)
    lin = np.clip(np.asarray(rgb, float), 0, 1) ** 2.2
    return linear_to_srgb(lin @ np.asarray(ccm, float))


def _colour():
    try:
        import colour
    except ImportError as exc:
        raise ImportError("the Munsell notation needs colour-science: pip install "
                          "'pyarchinit-photogrammetry[munsell]'") from exc
    return colour


def _hue_angle(hue: float, family: str) -> float:
    """Hue on a circle of 100 steps: 10RP = 0 = 0R, 5R = 5, 10R = 0YR = 10..."""
    return (HUE_FAMILIES.index(family) * 10 + hue) % 100


def _hue_name(angle: float) -> str:
    angle = round(angle / 2.5) * 2.5 % 100
    family, hue = divmod(angle, 10)
    if hue == 0:  # 0YR is 10R
        family, hue = (family - 1) % 10, 10
    return f"{hue:g}{HUE_FAMILIES[int(family)]}"


def munsell(rgb) -> dict:
    """Munsell notation of an sRGB colour in 0-1 (see the module docstring): ``notation``
    (``9.1YR 4.0/2.7``), ``chip`` (the nearest of a soil chart, ``10YR 4/3``), ``hue``, ``value``,
    ``chroma``."""
    colour = _colour()
    obs = colour.CCS_ILLUMINANTS["CIE 1931 2 Degree Standard Observer"]
    xyz = colour.sRGB_to_XYZ(np.clip(np.asarray(rgb, float), 1e-6, 1))
    xyz_c = colour.chromatic_adaptation(xyz, colour.xy_to_XYZ(obs["D65"]), colour.xy_to_XYZ(obs["C"]),
                                        method="Von Kries", transform="Bradford")
    xyy = colour.XYZ_to_xyY(xyz_c)
    spec = _specification(colour, xyy)
    if spec is None:  # the iterative conversion did not converge: the nearest chip of the whole circle
        chip = _nearest_chip(colour, xyy)
        spec = colour.notation.munsell.munsell_colour_to_munsell_specification(chip)
    if not np.isfinite(spec[0]) or not np.isfinite(spec[2]):
        notation = f"N{float(spec[1]):.1f}"
    else:
        notation = colour.notation.munsell.munsell_specification_to_munsell_colour(spec, 1, 1, 1)
    hue, value, chroma, code = (float(v) for v in spec)
    v_chip = min(CHART_VALUES, key=lambda v: abs(v - value))
    if not (math.isfinite(hue) and math.isfinite(chroma)) or chroma < 0.5:
        return {"notation": notation, "chip": f"N {v_chip:g}/", "hue": None, "value": value, "chroma": 0.0}
    family = {1: "B", 2: "BG", 3: "G", 4: "GY", 5: "Y", 6: "YR", 7: "R", 8: "RP", 9: "P", 10: "PB"}[int(code)]
    c_chip = min(CHART_CHROMAS, key=lambda c: abs(c - chroma))
    chip = f"{_hue_name(_hue_angle(hue, family))} {v_chip:g}/{c_chip:g}"
    return {"notation": notation, "chip": chip, "hue": f"{hue:.1f}{family}", "value": value, "chroma": chroma}


PERTURBATIONS = ((0, 0, 0), (0, 0, -1e-4), (0, 5e-4, 0), (-5e-4, 0, 0), (0, -5e-4, 0), (5e-4, 0, 0))


def _specification(colour, xyy):
    """``xyY_to_munsell_specification``, tried again a hair away when it does not converge (it
    happens on valid colours: 10R 3/6)."""
    import warnings

    for d in PERTURBATIONS:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                return np.asarray(colour.notation.munsell.xyY_to_munsell_specification(xyy + np.asarray(d)), float)
            except Exception:
                continue
    return None


_CHIPS: list | None = None


def _nearest_chip(colour, xyy) -> str:
    """The chip (hues every 2.5, the values and chromas of a soil chart) nearest in CIELAB."""
    import warnings

    global _CHIPS
    ill = colour.CCS_ILLUMINANTS["CIE 1931 2 Degree Standard Observer"]["C"]
    if _CHIPS is None:
        names, labs = [], []
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            for family in HUE_FAMILIES:
                for hue in (2.5, 5, 7.5, 10):
                    for v in CHART_VALUES:
                        for c in CHART_CHROMAS:
                            name = f"{hue:g}{family} {v:g}/{c:g}"
                            try:
                                lab = colour.XYZ_to_Lab(colour.xyY_to_XYZ(colour.munsell_colour_to_xyY(name)), ill)
                            except Exception:  # outside the renotation data
                                continue
                            names.append(name)
                            labs.append(lab)
        _CHIPS = [names, np.asarray(labs)]
    lab = colour.XYZ_to_Lab(colour.xyY_to_XYZ(np.asarray(xyy, float)), ill)
    return _CHIPS[0][int(np.argmin(np.linalg.norm(_CHIPS[1] - lab, axis=1)))]


def area_colour(raster: str | os.PathLike, polygon, ccm=None, max_pixels: int = 250_000) -> dict:
    """Colour of the raster inside ``polygon`` (see the module docstring): ``rgb`` (0-1,
    calibrated when ``ccm``), ``pixels`` used and the :func:`munsell` fields."""
    import rasterio
    from affine import Affine
    from rasterio.enums import Resampling
    from rasterio.features import geometry_mask
    from rasterio.windows import Window, from_bounds

    with rasterio.open(raster) as src:
        win = from_bounds(*polygon.bounds, transform=src.transform).round_offsets().round_lengths()
        win = win.intersection(Window(0, 0, src.width, src.height))
        f = max(1.0, math.sqrt(win.width * win.height / max_pixels))
        h, w = max(1, int(win.height / f)), max(1, int(win.width / f))
        rgb = src.read([1, 2, 3], window=win, out_shape=(3, h, w), resampling=Resampling.average)
        alpha = src.read(4, window=win, out_shape=(h, w)) > 0 if src.count >= 4 else np.ones((h, w), bool)
        tr = src.window_transform(win) @ Affine.scale(win.width / w, win.height / h)
    inside = ~geometry_mask([polygon], (h, w), tr) & alpha
    px = np.moveaxis(rgb, 0, -1)[inside].astype(float) / 255.0
    if not len(px):
        raise ValueError("no pixel of the orthomosaic inside the polygon")
    light = px.mean(axis=1)
    lo, hi = np.percentile(light, [10, 90])
    keep = px[(light >= lo) & (light <= hi)] if len(px) > 20 else px
    rgb_c = correct(np.median(keep, axis=0), ccm)
    return {"rgb": [float(v) for v in rgb_c], "pixels": int(len(keep)), **munsell(rgb_c)}
