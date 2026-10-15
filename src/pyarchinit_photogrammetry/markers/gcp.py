"""Ground control points, scale bars and georeferencing."""
from __future__ import annotations

import csv
from pathlib import Path

import numpy as np

from ..core import crs as crs_mod
from ..core import geometry
from ..core.camera import Reference
from ..core.marker import Marker


def triangulate_markers(chunk, min_views: int = 2) -> None:
    """Triangulate every marker seen in at least ``min_views`` aligned photos."""
    for m in chunk.markers:
        views, obs = [], []
        for cam_key, proj in m.projections.items():
            try:
                cam = chunk.camera(cam_key)
            except KeyError:
                continue
            if not (cam.enabled and cam.aligned):
                continue
            norm = chunk.sensor(cam).unproject([[proj.x, proj.y]])[0]
            views.append(geometry.PinholeView(cam.transform))
            obs.append(norm)
        m.position = geometry.triangulate(views, np.asarray(obs)) if len(views) >= min_views else None


def marker_reprojection_errors(chunk, marker: Marker) -> dict[int, float]:
    """Pixel error of each projection of a triangulated marker."""
    out = {}
    if marker.position is None:
        return out
    for cam_key, proj in marker.projections.items():
        cam = chunk.camera(cam_key)
        if not cam.aligned:
            continue
        px, depth = cam.project(chunk.sensor(cam), marker.position[None, :])
        if depth[0] > 0:
            out[cam_key] = float(np.hypot(px[0, 0] - proj.x, px[0, 1] - proj.y))
    return out


def _parse_rows(path: Path, delimiter: str | None, skip_rows: int):
    text = path.read_text(encoding="utf-8-sig")
    lines = [ln for ln in text.splitlines()[skip_rows:] if ln.strip() and not ln.lstrip().startswith("#")]
    if delimiter is None:
        sample = "\n".join(lines[:5])
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t ").delimiter
        except csv.Error:
            delimiter = ","
    if delimiter == " ":
        return [ln.split() for ln in lines]
    return [list(r) for r in csv.reader(lines, delimiter=delimiter)]


def projected_crs_for(crs: str | None, sample_lonlat: np.ndarray | None = None) -> str | None:
    """Photogrammetric products need metric axes: geographic CRS become the local UTM zone."""
    if crs is None or crs_mod.is_local(crs) or not crs_mod.is_geographic(crs):
        return crs
    if sample_lonlat is None:
        raise ValueError(f"{crs} is geographic: give a projected CRS or coordinates to pick the UTM zone")
    lon, lat = np.asarray(sample_lonlat, float)[:2]
    return crs_mod.utm_for_lonlat(lon, lat)


def import_reference(chunk, path, crs: str | None = None, delimiter: str | None = None, columns: str = "nxyz",
                     skip_rows: int = 0, create_markers: bool = True) -> int:
    """Read marker coordinates. ``crs`` is the CRS of the file; geographic coordinates
    (e.g. EPSG:4326, columns x = longitude, y = latitude) are converted to the chunk CRS,
    which becomes the local UTM zone when the chunk has no metric CRS yet."""
    path = Path(path)
    rows = _parse_rows(path, delimiter, skip_rows)
    src_crs = crs or chunk.crs
    parsed = []
    for row in rows:
        vals = dict(zip(columns, row))
        if not {"n", "x", "y"} <= set(vals):
            continue
        try:
            xyz = np.array([float(vals["x"]), float(vals["y"]), float(vals.get("z", 0.0) or 0.0)])
        except ValueError:
            continue  # header row
        parsed.append((vals, xyz))
    if not parsed:
        return 0
    if crs_mod.is_local(chunk.crs) or crs_mod.is_geographic(chunk.crs):
        if not crs_mod.is_local(src_crs):
            chunk.crs = projected_crs_for(src_crs, parsed[0][1]) if crs_mod.is_geographic(src_crs) else src_crs
    count = 0
    for vals, xyz in parsed:
        if not crs_mod.is_local(src_crs) and src_crs != chunk.crs:
            xyz = crs_mod.transform_points(xyz[None], src_crs, chunk.crs)[0]
        label = vals["n"].strip()
        marker = _find_marker(chunk, label)
        if marker is None:
            if not create_markers:
                continue
            marker = chunk.add_marker(label)
        acc = float(vals["a"]) if vals.get("a") else marker.reference.accuracy[0]
        marker.reference = Reference(location=xyz.tolist(), accuracy=[acc, acc, acc], enabled=True)
        count += 1
    return count


def _find_marker(chunk, label: str) -> Marker | None:
    for m in chunk.markers:
        if m.label == label:
            return m
    try:
        code = int(label)
    except ValueError:
        return None
    for m in chunk.markers:
        if m.code == code:
            return m
    return None


def export_reference(chunk, path) -> Path:
    """Write markers (reference and estimated coordinates, errors) as CSV."""
    path = Path(path)
    errors = {e["label"]: e for e in marker_errors(chunk)}
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["label", "x_ref", "y_ref", "z_ref", "x_est", "y_est", "z_est", "error_m", "projections", "type"])
        for m in chunk.markers:
            ref = m.reference.location or [None] * 3
            est = chunk.to_world(m.position) if m.position is not None else [None] * 3
            e = errors.get(m.label, {})
            w.writerow([m.label, *ref, *est, e.get("error"), len(m.projections),
                        "check" if m.checkpoint else "control"])
    return path


def _camera_reference_pairs(chunk) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(internal centres, reference positions in chunk CRS, accuracies) of referenced cameras."""
    src, dst, acc = [], [], []
    for cam in chunk.aligned_cameras:
        ref = cam.reference
        if not (ref.enabled and ref.location):
            continue
        loc = np.asarray(ref.location, float)
        ref_crs = cam.meta.get("reference_crs", chunk.crs)
        if not crs_mod.is_local(ref_crs) and ref_crs != chunk.crs:
            if crs_mod.is_local(chunk.crs):
                chunk.crs = projected_crs_for(ref_crs, loc) if crs_mod.is_geographic(ref_crs) else ref_crs
            loc = crs_mod.transform_points(loc[None], ref_crs, chunk.crs)[0]
        src.append(cam.center)
        dst.append(loc)
        acc.append(float(np.mean(ref.accuracy)))
    return np.asarray(src).reshape(-1, 3), np.asarray(dst).reshape(-1, 3), np.asarray(acc)


def _non_collinear(points: np.ndarray) -> bool:
    if len(points) < 3:
        return False
    s = np.linalg.svd(points - points.mean(0), compute_uv=False)
    return s[1] > 1e-6 * max(s[0], 1e-12)


def update_transform(chunk, use_cameras: bool | None = None) -> dict:
    """Fit ``chunk.transform`` and return an accuracy report.

    Priority: control markers (>= 3) -> camera references (>= 3) -> scale bars
    (scale only). Observations are weighted by ``1 / accuracy**2``, so millimetric
    GCPs dominate metre-level GNSS camera positions. The report lists control/check
    point and scale-bar errors.
    """
    if crs_mod.is_geographic(chunk.crs):
        raise ValueError(f"chunk CRS {chunk.crs} is geographic; use a projected CRS (e.g. the UTM zone)")
    chunk.triangulate_markers()
    ctrl = [m for m in chunk.markers if not m.checkpoint and m.reference.enabled and m.reference.location
            and m.position is not None]
    src = np.array([m.position for m in ctrl]).reshape(-1, 3)
    dst = np.array([m.reference.location for m in ctrl], float).reshape(-1, 3)
    acc = np.array([np.mean(m.reference.accuracy) for m in ctrl], float)
    method = None
    if use_cameras is None:
        use_cameras = len(ctrl) < 3
    if use_cameras and ctrl and crs_mod.is_local(chunk.crs):
        use_cameras = False  # local marker frame: GNSS camera positions live in another frame
    if use_cameras:
        cs, cd, ca = _camera_reference_pairs(chunk)
        src, dst, acc = np.vstack([src, cs]), np.vstack([dst, cd]), np.r_[acc, ca]
    if _non_collinear(dst) and len(src) >= 3:
        chunk.transform = geometry.umeyama(src, dst, with_scale=True, weights=1.0 / np.maximum(acc, 1e-6) ** 2)
        method = "markers+cameras" if use_cameras and len(ctrl) else ("cameras" if use_cameras else "markers")
    elif chunk.scalebars:
        ratios, weights = [], []
        for sb in chunk.scalebars:
            a, b = chunk.marker(sb.marker_a), chunk.marker(sb.marker_b)
            if sb.enabled and a.position is not None and b.position is not None:
                ratios.append(sb.distance / np.linalg.norm(a.position - b.position))
                weights.append(1.0 / max(sb.accuracy, 1e-6) ** 2)
        if ratios:
            s = float(np.average(ratios, weights=weights))
            _, rot, _ = geometry.decompose_similarity(chunk.transform)
            chunk.transform = geometry.compose_similarity(s, rot, np.zeros(3))
            method = "scalebars"
    report = {"method": method, "crs": chunk.crs, "markers": marker_errors(chunk),
              "scalebars": scalebar_errors(chunk)}
    for kind in ("control", "check"):
        errs = [e["error"] for e in report["markers"] if e["type"] == kind and e["error"] is not None]
        report[f"{kind}_rmse"] = float(np.sqrt(np.mean(np.square(errs)))) if errs else None
    return report


def marker_errors(chunk) -> list[dict]:
    out = []
    for m in chunk.markers:
        e = {"label": m.label, "type": "check" if m.checkpoint else "control", "error": None,
             "dx": None, "dy": None, "dz": None, "projections": len(m.projections),
             "pixel_error": None}
        px = marker_reprojection_errors(chunk, m)
        if px:
            e["pixel_error"] = float(np.sqrt(np.mean(np.square(list(px.values())))))
        if m.position is not None and m.reference.location:
            d = chunk.to_world(m.position) - np.asarray(m.reference.location, float)
            e.update(dx=float(d[0]), dy=float(d[1]), dz=float(d[2]), error=float(np.linalg.norm(d)))
        out.append(e)
    return out


def scalebar_errors(chunk) -> list[dict]:
    out = []
    s = chunk.scale
    for sb in chunk.scalebars:
        a, b = chunk.marker(sb.marker_a), chunk.marker(sb.marker_b)
        if a.position is None or b.position is None:
            out.append({"label": sb.label, "distance": sb.distance, "estimated": None, "error": None})
            continue
        est = float(s * np.linalg.norm(a.position - b.position))
        out.append({"label": sb.label, "distance": sb.distance, "estimated": est, "error": est - sb.distance})
    return out
