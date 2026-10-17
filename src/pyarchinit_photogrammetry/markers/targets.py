"""Coded targets: printable sheets and automatic detection.

Targets are ArUco / AprilTag squares (OpenCV). The *marker point* is the
centre of the square, computed in the photo as the intersection of the
diagonals of the four detected corners (the projective image of the centre).
On the printed sheet small ticks around each target point to that centre.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from ..core.marker import Marker, Projection
from ..core.progress import Progress

TARGET_TYPES = {
    "aruco_4x4_50": cv2.aruco.DICT_4X4_50,
    "aruco_4x4_100": cv2.aruco.DICT_4X4_100,
    "aruco_4x4_250": cv2.aruco.DICT_4X4_250,
    "aruco_5x5_100": cv2.aruco.DICT_5X5_100,
    "aruco_5x5_250": cv2.aruco.DICT_5X5_250,
    "aruco_6x6_250": cv2.aruco.DICT_6X6_250,
    "aruco_7x7_250": cv2.aruco.DICT_7X7_250,
    "apriltag_16h5": cv2.aruco.DICT_APRILTAG_16h5,
    "apriltag_25h9": cv2.aruco.DICT_APRILTAG_25h9,
    "apriltag_36h11": cv2.aruco.DICT_APRILTAG_36h11,
}

PAGE_SIZES_MM = {"A4": (210, 297), "A3": (297, 420), "A2": (420, 594), "Letter": (216, 279)}


def _dictionary(target_type: str):
    try:
        return cv2.aruco.getPredefinedDictionary(TARGET_TYPES[target_type])
    except KeyError:
        raise ValueError(f"unknown target type {target_type!r}; choose one of {sorted(TARGET_TYPES)}") from None


def dictionary_size(target_type: str) -> int:
    return int(_dictionary(target_type).bytesList.shape[0])


def target_image(target_type: str, code: int, size_px: int = 600) -> np.ndarray:
    """The black-and-white pattern of one target (no quiet zone), uint8."""
    if not 0 <= code < dictionary_size(target_type):
        raise ValueError(f"code {code} out of range for {target_type}")
    return cv2.aruco.generateImageMarker(_dictionary(target_type), int(code), int(size_px), borderBits=1)


def _font(size_px: int):
    for name in ("DejaVuSans.ttf", "Arial.ttf", "Helvetica.ttc"):
        try:
            return ImageFont.truetype(name, size_px)
        except OSError:
            continue
    return ImageFont.load_default()


def _draw_target(canvas: Image.Image, target_type: str, code: int, cx: float, cy: float, size_px: int,
                 label: str | None) -> None:
    pattern = Image.fromarray(target_image(target_type, code, size_px))
    canvas.paste(pattern, (int(round(cx - size_px / 2)), int(round(cy - size_px / 2))))
    d = ImageDraw.Draw(canvas)
    half, gap, tick = size_px / 2, size_px * 0.06, size_px * 0.12
    lw = max(1, size_px // 150)
    for sx, sy in ((1, 0), (-1, 0), (0, 1), (0, -1)):  # ticks pointing to the centre
        x0, y0 = cx + sx * (half + gap), cy + sy * (half + gap)
        d.line([(x0, y0), (x0 + sx * tick, y0 + sy * tick)], fill=0, width=lw)
    if label:
        f = _font(max(10, size_px // 9))
        d.text((cx, cy + half + gap + tick + size_px * 0.05), label, fill=0, font=f, anchor="mt")


def _draw_scale_bar(canvas: Image.Image, x: float, y: float, px_per_mm: float, length_mm: int = 100) -> None:
    d = ImageDraw.Draw(canvas)
    lw = max(1, int(px_per_mm * 0.3))
    d.line([(x, y), (x + length_mm * px_per_mm, y)], fill=0, width=lw)
    for mm in range(0, length_mm + 1):
        h = 3 if mm % 10 == 0 else (2 if mm % 5 == 0 else 1)
        xx = x + mm * px_per_mm
        d.line([(xx, y), (xx, y - h * px_per_mm)], fill=0, width=max(1, lw // 2))
    d.text((x, y + 2 * px_per_mm), f"{length_mm} mm - verify print scale", fill=0, font=_font(int(3 * px_per_mm)))


def generate_sheet(path: str | Path, target_type: str = "aruco_4x4_50", codes: Iterable[int] | None = None,
                   marker_size_mm: float = 40.0, page: str = "A4", dpi: int = 300, margin_mm: float = 12.0,
                   labels: bool = True) -> list[Path]:
    """Write printable sheets (PNG or PDF by extension) with the given target codes.

    Returns the written files (one per page for PNG, one multi-page PDF).
    The physical size of the black square is ``marker_size_mm`` when printed at 100%.
    """
    path = Path(path)
    codes = list(range(min(12, dictionary_size(target_type)))) if codes is None else list(codes)
    pw_mm, ph_mm = PAGE_SIZES_MM[page]
    ppm = dpi / 25.4
    cell_mm = marker_size_mm * 1.9
    cols = max(1, int((pw_mm - 2 * margin_mm) // cell_mm))
    rows = max(1, int((ph_mm - 2 * margin_mm - 20) // cell_mm))
    per_page = cols * rows
    pages = []
    for start in range(0, len(codes), per_page):
        canvas = Image.new("L", (int(pw_mm * ppm), int(ph_mm * ppm)), 255)
        for i, code in enumerate(codes[start:start + per_page]):
            r, c = divmod(i, cols)
            cx = (margin_mm + (c + 0.5) * cell_mm) * ppm
            cy = (margin_mm + (r + 0.45) * cell_mm) * ppm
            _draw_target(canvas, target_type, code, cx, cy, int(round(marker_size_mm * ppm)),
                         f"{code}  {target_type}  {marker_size_mm:g} mm" if labels else None)
        _draw_scale_bar(canvas, margin_mm * ppm, (ph_mm - margin_mm) * ppm, ppm)
        pages.append(canvas)
    return _save_pages(pages, path, dpi)


def scale_bar_codes(target_type: str = "aruco_4x4_50", count: int = 1) -> list[tuple[int, int]]:
    """The code pairs of printed scale bars: the last codes of the dictionary (target sheets and
    turntable mats start from 0)."""
    n = dictionary_size(target_type)
    return [(n - 2 - 2 * i, n - 1 - 2 * i) for i in range(count)]


def generate_scale_bar(path: str | Path, target_type: str = "aruco_4x4_50", codes=None, distance_mm: float = 200.0,
                       marker_size_mm: float = 40.0, page: str = "A4", dpi: int = 300,
                       count: int = 1) -> tuple[list[Path], Path]:
    """Printable scale bars: two coded targets whose centres are ``distance_mm`` apart on a
    landscape page, a 10 cm ruler to check that it was printed at 100 %, and the CSV of the bars
    (``marker_a,marker_b,distance`` with the target codes and metres) to import once the targets
    are detected - glue the page on something rigid. ``codes``: the pairs of codes, default
    :func:`scale_bar_codes`. Returns ``(pages, csv)``."""
    path = Path(path)
    pairs = [tuple(int(c) for c in pair) for pair in codes] if codes else scale_bar_codes(target_type, count)
    ph_mm, pw_mm = PAGE_SIZES_MM[page]  # landscape
    margin = 12.0
    if distance_mm + 1.4 * marker_size_mm > pw_mm - 2 * margin:
        raise ValueError(f"{distance_mm:g} mm between targets of {marker_size_mm:g} mm do not fit on {page}: "
                         f"at most {pw_mm - 2 * margin - 1.4 * marker_size_mm:g} mm")
    ppm = dpi / 25.4
    pages = []
    for a, b in pairs:
        canvas = Image.new("L", (int(pw_mm * ppm), int(ph_mm * ppm)), 255)
        cy = ph_mm / 2 * ppm
        size = int(round(marker_size_mm * ppm))
        for code, x_mm in ((a, pw_mm / 2 - distance_mm / 2), (b, pw_mm / 2 + distance_mm / 2)):
            _draw_target(canvas, target_type, code, x_mm * ppm, cy, size, f"{code}")
        d = ImageDraw.Draw(canvas)
        d.text((pw_mm / 2 * ppm, margin * ppm), f"Barra di scala {a}-{b}: {distance_mm:g} mm tra i centri "
               f"({target_type}, target di {marker_size_mm:g} mm)", fill=0, font=_font(int(4.5 * ppm)), anchor="mt")
        d.text((pw_mm / 2 * ppm, (margin + 7) * ppm), "Stampa al 100% e incolla su un supporto rigido; se il "
               "righello non misura 10 cm, correggi la distanza della barra", fill=0, font=_font(int(3.2 * ppm)),
               anchor="mt")
        _draw_scale_bar(canvas, margin * ppm, (ph_mm - margin) * ppm, ppm)
        pages.append(canvas)
    files = _save_pages(pages, path, dpi)
    csv = path.with_suffix(".csv")
    csv.write_text("marker_a,marker_b,distance\n" + "".join(f"{a},{b},{distance_mm / 1000:.5f}\n" for a, b in pairs),
                   encoding="utf-8")
    return files, csv


def _save_pages(pages: list[Image.Image], path: Path, dpi: int) -> list[Path]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".pdf":
        pages[0].save(path, save_all=True, append_images=pages[1:], resolution=dpi)
        return [path]
    out = []
    for i, p in enumerate(pages):
        f = path if len(pages) == 1 else path.with_name(f"{path.stem}_{i + 1}{path.suffix}")
        p.save(f, dpi=(dpi, dpi))
        out.append(f)
    return out


def turntable_layout(n_targets: int = 12, diameter_mm: float = 180.0, first_code: int = 0) -> dict[int, tuple[float, float, float]]:
    """Code -> (x, y, z) in metres of the targets of a turntable mat (z = 0, origin = rotation axis)."""
    r = diameter_mm / 2000.0
    return {first_code + i: (r * math.cos(2 * math.pi * i / n_targets), r * math.sin(2 * math.pi * i / n_targets), 0.0)
            for i in range(n_targets)}


def generate_turntable_mat(path: str | Path, target_type: str = "aruco_4x4_50", n_targets: int = 12,
                           diameter_mm: float = 180.0, marker_size_mm: float = 22.0, page: str = "A3",
                           dpi: int = 300, first_code: int = 0) -> tuple[list[Path], dict[int, tuple[float, float, float]]]:
    """A ring of coded targets to glue on a turntable. The object stands in the
    middle; the known target layout gives scale, a vertical axis and the rotation
    centre, so the scan comes out levelled and in millimetres.

    Returns the written files and the target coordinates (metres) to use as reference.
    """
    pw_mm, ph_mm = PAGE_SIZES_MM[page]
    ppm = dpi / 25.4
    canvas = Image.new("L", (int(pw_mm * ppm), int(ph_mm * ppm)), 255)
    cx_mm, cy_mm = pw_mm / 2, ph_mm / 2 - 10
    layout = turntable_layout(n_targets, diameter_mm, first_code)
    for code, (x, y, _) in layout.items():
        # image y axis points down: flip so the mat matches the right-handed layout seen from above
        _draw_target(canvas, target_type, code, (cx_mm + x * 1000) * ppm, (cy_mm - y * 1000) * ppm,
                     int(round(marker_size_mm * ppm)), str(code))
    d = ImageDraw.Draw(canvas)
    c = (cx_mm * ppm, cy_mm * ppm)
    d.line([(c[0] - 6 * ppm, c[1]), (c[0] + 6 * ppm, c[1])], fill=0, width=2)
    d.line([(c[0], c[1] - 6 * ppm), (c[0], c[1] + 6 * ppm)], fill=0, width=2)
    d.text((10 * ppm, 10 * ppm), f"Turntable mat {target_type} - ring {diameter_mm:g} mm - targets {marker_size_mm:g} mm",
           fill=0, font=_font(int(4 * ppm)))
    _draw_scale_bar(canvas, 12 * ppm, (ph_mm - 12) * ppm, ppm)
    return _save_pages([canvas], Path(path), dpi), layout


def detect(image: np.ndarray, target_type: str = "aruco_4x4_50") -> list[dict]:
    """Detect targets in an image (BGR or grey). Returns dicts with
    ``code``, ``center`` (x, y) and ``corners`` (4x2), sub-pixel refined."""
    grey = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    params = cv2.aruco.DetectorParameters()
    params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
    params.cornerRefinementWinSize = 5
    detector = cv2.aruco.ArucoDetector(_dictionary(target_type), params)
    corners, ids, _ = detector.detectMarkers(grey)
    out = []
    if ids is None:
        return out
    for c, code in zip(corners, ids.ravel()):
        q = c.reshape(4, 2).astype(float)
        out.append({"code": int(code), "corners": q, "center": _diagonal_intersection(q)})
    return out


def _diagonal_intersection(q: np.ndarray) -> np.ndarray:
    p1, p3, p2, p4 = q[0], q[2], q[1], q[3]
    a = np.array([p3 - p1, p2 - p4]).T
    t = np.linalg.solve(a, p2 - p1)[0]
    return p1 + t * (p3 - p1)


def detect_in_chunk(chunk, target_type: str = "aruco_4x4_50", cameras=None, progress: Progress | None = None) -> list[Marker]:
    progress = progress or Progress()
    cams = [c for c in (cameras or chunk.cameras) if c.enabled]
    by_code = {m.code: m for m in chunk.markers if m.code is not None and m.target_type == target_type}
    touched: dict[int, Marker] = {}
    for i, cam in enumerate(cams):
        img = cv2.imread(str(cam.path), cv2.IMREAD_GRAYSCALE | cv2.IMREAD_IGNORE_ORIENTATION)
        if img is None:
            continue
        for det in detect(img, target_type):
            m = by_code.get(det["code"])
            if m is None:
                m = Marker(key=chunk._next_key(chunk.markers), label=f"target {det['code']}", code=det["code"],
                           target_type=target_type)
                chunk.markers.append(m)
                by_code[det["code"]] = m
            prev = m.projections.get(cam.key)
            if prev is None or not prev.pinned:
                # OpenCV -> COLMAP pixel convention (+0.5)
                m.projections[cam.key] = Projection(float(det["center"][0]) + 0.5, float(det["center"][1]) + 0.5, False)
            touched[m.key] = m
        progress((i + 1) / len(cams), f"Detected targets in {cam.label}")
    if chunk.aligned_cameras:
        chunk.triangulate_markers()
    return list(touched.values())
