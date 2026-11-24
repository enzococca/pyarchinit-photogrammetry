"""The comparison sheet of a wall: the campaigns of a group of elevations (:mod:`.elevation_groups`) stacked on the
same ``u`` axis, the oldest on top, in one picture (PNG or JPEG) and the same as a PDF beside it.

Every band is the same window of the same grid, read reduced by one integer factor (so that the sheet is at most
``max_width`` pixels wide) and pasted as read: a column of the sheet is the same ``u`` in every band. On the left of
every band a tick at every multiple of :data:`HEIGHT_TICK` (the height when ``v`` is the height, else ``v``); under
the last band the distance along the wall from the trace's start; at every corner of the trace a dashed line through
all the bands and a mark above the first; the title on top, the label and the date of each campaign above its band.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from ..core.progress import as_progress

HEIGHT_TICK = 0.5  # m between two height ticks
INK = (20, 20, 20)
CORNER = (230, 20, 20)
PAPER = (255, 255, 255)
EPS = 1e-9


def _font(size: int, bold: bool = False):
    from PIL import ImageFont

    from ..io.report_content import font_files

    files = font_files()
    path = files.get("B" if bold else "") or files.get("")
    if path is not None:
        try:
            return ImageFont.truetype(str(path), size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


def _multiples(lo: float, hi: float, step: float) -> list[float]:
    """The multiples of ``step`` from ``lo`` to ``hi``, both included (within a nanometre)."""
    k0, k1 = math.ceil(lo / step - EPS), math.floor(hi / step + EPS)
    return [round(k * step, 9) + 0.0 for k in range(k0, k1 + 1)]


def _number(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".") if value != 0 else "0"


def _read_band(path: Path, grid, shape, factor: int) -> np.ndarray:
    """The orthophoto at ``path`` on the window of the reference grid (its transform ``grid`` and ``shape``
    ``(h, w)``), reduced by ``factor``: RGB (H, W, 3), white where it has no alpha."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.windows import Window

    h, w = shape
    out_h, out_w = math.ceil(h / factor), math.ceil(w / factor)
    with rasterio.open(path) as src:
        t, res = src.transform, abs(grid.a)
        col = int(round((grid.c - t.c) / res))
        row = int(round((t.f - grid.f) / res))
        window = Window(col, row, out_w * factor, out_h * factor)
        inside = col >= 0 and row >= 0 and col + out_w * factor <= src.width and row + out_h * factor <= src.height
        arr = src.read(window=window, out_shape=(src.count, out_h, out_w), boundless=not inside, fill_value=0,
                       resampling=Resampling.average if factor > 1 else Resampling.nearest)
    rgb = np.moveaxis(arr[:3], 0, -1).astype(np.uint8)
    if arr.shape[0] >= 4:
        rgb = np.where(arr[3][..., None] > 0, rgb, 255).astype(np.uint8)
    return rgb


def _corners(entry: dict, u_left: float, u_right: float) -> list[float]:
    """``u`` of the corners of the trace of a wall elevation inside the grid; none for a plane or a cylinder."""
    from .elevation import frame_from_dict

    frame = frame_from_dict(entry["frame"])
    if getattr(frame, "kind", None) != "path":
        return []
    return [float(c["u"]) for c in frame.corners() if u_left - EPS <= c["u"] <= u_right + EPS]


def comparison_sheet(doc, chunk, group: dict, out_path, title: str | None = None, max_width: int = 6000,
                     dpi: int = 200, progress=None) -> dict:
    """The comparison sheet of ``group`` (kept in ``chunk``) written as ``out_path`` (JPEG for ``.jpg``/``.jpeg``,
    else PNG) and, beside it, the same as ``.pdf``. Returns the layout: ``image``, ``pdf`` (paths), ``factor`` (the
    grid read one pixel in ``factor``), ``px_per_m``, ``bands`` (``label``, ``date``, ``left``, ``top``,
    ``width``, ``height`` of each campaign, the oldest first), ``v_ticks`` (``(value, y)`` on the first band),
    ``u_ticks`` (``(u, x)``), ``corners`` (``(u, x)``) and ``skipped`` (the labels of the members without files).
    ``ValueError`` when no member has its files."""
    import rasterio
    from PIL import Image, ImageDraw

    from ..archaeo.sections import grid_step
    from . import elevation_groups as G
    from .elevation import elevation_paths

    progress = as_progress(progress)
    out_path = Path(out_path)
    usable, skipped = [], []
    for m in G.members(doc, chunk, group):
        ortho = elevation_paths(m["chunk"], m["entry"])[0] if m["chunk"] is not None and m["entry"] else None
        if ortho is None or not ortho.exists():
            skipped.append(m["label"])
        else:
            usable.append((m, ortho))
    if not usable:
        raise ValueError(f"no campaign of the group {group.get('name')!r} has its elevation files")
    ref_entry = usable[0][0]["entry"]
    with rasterio.open(usable[0][1]) as src:
        grid, shape = src.transform, (src.height, src.width)
    res = abs(grid.a)
    h, w = shape
    factor = max(1, math.ceil(w / max(1, int(max_width))))
    band_w, band_h = math.ceil(w / factor), math.ceil(h / factor)
    px_per_m = 1.0 / (res * factor)
    u_left, u_right = float(grid.c), float(grid.c) + w * res
    v_top, v_bottom = float(grid.f), float(grid.f) - h * res

    images = []
    for i, (m, ortho) in enumerate(usable):
        progress(0.8 * i / len(usable), f"Comparison sheet: reading {m['label']}")
        images.append(_read_band(ortho, grid, shape, factor))

    # sizes: the fonts follow the width of the bands
    fs = int(max(12, min(48, round(band_w / 50))))
    font, title_font = _font(fs), _font(int(fs * 1.6), bold=True)
    pad, tick = fs, max(4, fs // 2)
    line_w = max(1, fs // 12)
    # the ticks over the bounds of the elevation (half a cell either way: an end at 9.99997 m has its 10 m), on
    # the grid
    b0, b1, b2, b3 = (float(x) for x in (ref_entry.get("bounds") or (u_left, v_bottom, u_right, v_top)))
    near = res / 2
    v_values = _multiples(max(b1, v_bottom) - near, min(b3, v_top) + near, HEIGHT_TICK)
    u_step = grid_step(b2 - b0, 20)
    u_values = _multiples(max(b0, u_left) - near, min(b2, u_right) + near, u_step)
    corners = _corners(ref_entry, u_left, u_right)
    v_labels = [f"{v:.1f}" for v in v_values]
    label_w = int(max([font.getlength(t) for t in v_labels] + [0]))
    left = pad + label_w + pad // 2 + tick
    u_text = max([font.getlength(_number(u)) for u in u_values] + [0.0])
    every = next(n for n in (1, 2, 4, 5, 10, 20, 50, 100, 10 ** 9)  # a label on one tick in ``every``: no overlap
                 if n * u_step * px_per_m >= u_text + fs)
    width = int(left + band_w + max(pad, u_text / 2 + font.getlength(" m") + pad))
    title = (group.get("name") or "") if title is None else title
    title_h = int(fs * 1.6 * 1.3)
    label_h = int(fs * 1.5)
    mark_h = int(fs * 1.2) if corners else 0
    y = pad + title_h + pad // 2
    bands = []
    for i, (m, _ortho) in enumerate(usable):
        y += label_h + (mark_h if i == 0 else 0)
        bands.append({"label": m["label"], "date": m["date"], "left": int(left), "top": int(y), "width": band_w,
                      "height": band_h})
        y += band_h + pad
    height = int(y - pad + tick + fs * 1.6 + pad)

    sheet = Image.new("RGB", (width, height), PAPER)
    draw = ImageDraw.Draw(sheet)
    draw.text((left, pad), title, fill=INK, font=title_font)
    for band, rgb in zip(bands, images):
        sheet.paste(Image.fromarray(rgb), (band["left"], band["top"]))
        text = band["label"] + (f"  {band['date']}" if band["date"] else "")
        draw.text((band["left"], band["top"] - (mark_h if band is bands[0] else 0) - label_h + fs // 6), text,
                  fill=INK, font=font)
        for v, label in zip(v_values, v_labels):
            ty = band["top"] + (v_top - v) * px_per_m
            iy = int(round(min(ty, band["top"] + band_h - 1)))
            draw.line([(band["left"] - tick, iy), (band["left"] - 1, iy)], fill=INK, width=line_w)
            draw.text((band["left"] - tick - pad // 4, iy), label, fill=INK, font=font, anchor="rm")
        draw.rectangle([band["left"] - 1, band["top"] - 1, band["left"] + band_w, band["top"] + band_h],
                       outline=INK, width=1)
    first, last = bands[0], bands[-1]
    v_ticks = [(v, first["top"] + (v_top - v) * px_per_m) for v in v_values]
    bottom = last["top"] + band_h
    u_ticks = []
    for u in u_values:
        x = first["left"] + (u - u_left) * px_per_m
        u_ticks.append((u, x))
        ix = int(round(min(x, first["left"] + band_w - 1)))
        draw.line([(ix, bottom + 1), (ix, bottom + tick)], fill=INK, width=line_w)
        if round(u / u_step) % every == 0:
            draw.text((ix, bottom + tick + fs // 4), _number(u), fill=INK, font=font, anchor="mt")
    if u_values:  # the unit after the band's end
        draw.text((first["left"] + band_w + u_text / 2 + fs // 4, bottom + tick + fs // 4), "m", fill=INK,
                  font=font, anchor="lt")
    corner_out = []
    dash = max(4, fs // 2)
    for u in corners:
        x = first["left"] + (u - u_left) * px_per_m
        corner_out.append((u, x))
        ix = int(round(min(x, first["left"] + band_w - 1)))
        y0 = first["top"]
        while y0 < bottom:
            draw.line([(ix, y0), (ix, min(y0 + dash, bottom) - 1)], fill=CORNER, width=line_w)
            y0 += 2 * dash
        s = max(3, int(mark_h * 0.7))
        draw.polygon([(ix - s // 2, first["top"] - s - 1), (ix + s // 2, first["top"] - s - 1),
                      (ix, first["top"] - 2)], fill=CORNER)

    progress(0.9, "Comparison sheet: writing")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.suffix.lower() in (".jpg", ".jpeg"):
        sheet.save(out_path, "JPEG", quality=92, dpi=(dpi, dpi))
    else:
        sheet.save(out_path, "PNG", dpi=(dpi, dpi))
    pdf = out_path.with_suffix(".pdf")
    sheet.save(pdf, "PDF", resolution=float(dpi))
    progress(1.0, "Comparison sheet written")
    return {"image": str(out_path), "pdf": str(pdf), "factor": factor, "px_per_m": px_per_m, "bands": bands,
            "v_ticks": v_ticks, "u_ticks": u_ticks, "corners": corner_out, "skipped": skipped}
