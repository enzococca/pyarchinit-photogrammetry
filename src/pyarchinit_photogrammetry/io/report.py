"""Processing report (standalone HTML, previews embedded)."""
from __future__ import annotations

import base64
import html
import io
from datetime import datetime
from pathlib import Path

import numpy as np


def _png_preview(path: Path, max_side: int = 900, dem: bool = False) -> str | None:
    if path is None or not Path(path).exists():
        return None
    import rasterio
    from PIL import Image

    with rasterio.open(path) as src:
        f = max(1, int(np.ceil(max(src.width, src.height) / max_side)))
        out = (src.count, max(1, src.height // f), max(1, src.width // f))
        arr = src.read(out_shape=out)
        nodata = src.nodata
    if dem:
        a = arr[0].astype(float)
        if nodata is not None:
            a[a == nodata] = np.nan
        lo, hi = np.nanpercentile(a, [2, 98]) if np.isfinite(a).any() else (0, 1)
        t = np.clip((a - lo) / max(hi - lo, 1e-9), 0, 1)
        rgb = np.dstack([t, 0.4 + 0.4 * (1 - abs(t - 0.5) * 2), 1 - t])
        rgb[~np.isfinite(a)] = 1
        img = Image.fromarray((rgb * 255).astype(np.uint8))
    else:
        img = Image.fromarray(np.moveaxis(arr[:4], 0, -1).astype(np.uint8), "RGBA" if arr.shape[0] == 4 else "RGB")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _table(rows: list[dict], cols: list[str]) -> str:
    def fmt(v):
        if v is None:
            return ""
        if isinstance(v, float):
            return f"{v:.4f}"
        return html.escape(str(v))

    head = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{fmt(r.get(c))}</td>" for c in cols) + "</tr>" for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def generate_report(chunk, path) -> Path:
    from ..markers.gcp import marker_errors, scalebar_errors

    path = Path(path)
    cams = chunk.cameras
    aligned = chunk.aligned_cameras
    tp = chunk.tie_points
    sfm = chunk.meta.get("sfm", {})
    sensors = [{"label": s.label, "resolution": f"{s.width}x{s.height}", "model": s.model,
                "focal_px": s.focal_px, "params": ", ".join(f"{p:.5g}" for p in s.params)} for s in chunk.sensors.values()]
    parts = [
        f"<h1>{html.escape(chunk.label)}</h1>",
        f"<p>Report generated {datetime.now():%Y-%m-%d %H:%M} by pyarchinit-photogrammetry</p>",
        "<h2>Survey data</h2>",
        _table([{"photos": len(cams), "aligned": len(aligned), "tie points": len(tp) if tp is not None else 0,
                 "reprojection error (px)": sfm.get("reprojection_error"), "CRS": chunk.crs,
                 "scale": chunk.scale}],
               ["photos", "aligned", "tie points", "reprojection error (px)", "CRS", "scale"]),
        "<h2>Camera calibration</h2>", _table(sensors, ["label", "resolution", "model", "focal_px", "params"]),
    ]
    if chunk.markers:
        parts += ["<h2>Markers</h2>", _table(marker_errors(chunk),
                                             ["label", "type", "projections", "pixel_error", "dx", "dy", "dz", "error"])]
    if chunk.scalebars:
        parts += ["<h2>Scale bars</h2>", _table(scalebar_errors(chunk), ["label", "distance", "estimated", "error"])]
    for name, key, dem in (("Digital elevation model", "dem", True), ("Orthomosaic", "orthomosaic", False)):
        b64 = _png_preview(chunk.product_path(key), dem=dem)
        if b64:
            res = chunk.meta.get(key, {}).get("resolution")
            parts += [f"<h2>{name}</h2>", f"<p>Resolution: {res:.4g} m/px</p>" if res else "",
                      f'<img alt="{name}" src="data:image/png;base64,{b64}">']
    css = ("body{font-family:system-ui,sans-serif;max-width:980px;margin:2em auto;padding:0 1em;color:#222}"
           "table{border-collapse:collapse;margin:.5em 0 1.5em;font-size:14px}"
           "td,th{border:1px solid #ccc;padding:4px 8px;text-align:right}th{background:#f2f2f2}"
           "img{max-width:100%;border:1px solid #ccc}")
    doc = f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(chunk.label)} - report</title>" \
          f"<style>{css}</style></head><body>{''.join(parts)}</body></html>"
    path.write_text(doc, encoding="utf-8")
    return path
