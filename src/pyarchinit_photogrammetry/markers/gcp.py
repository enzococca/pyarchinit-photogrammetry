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


def cameras_span_their_accuracy(chunk, factor: float = 10.0) -> bool:
    """The camera references (the GPS of the photos) cover an area at least ``factor`` times
    their accuracy: they can place and turn the survey (a flight over a site), not an object
    on a table or a trench of a few metres, where they are only noise."""
    locs, accs, geographic = [], [], False
    for cam in chunk.aligned_cameras:
        ref = cam.reference
        if ref.enabled and ref.location:
            locs.append(np.asarray(ref.location, float)[:2])
            accs.append(float(np.mean(ref.accuracy[:2])) if ref.accuracy else 5.0)
            geographic |= crs_mod.is_geographic(cam.meta.get("reference_crs", chunk.crs))
    if len(locs) < 3:
        return False
    xy = np.array(locs)
    if geographic:  # longitude, latitude in degrees: metres near the photos
        xy = np.column_stack([xy[:, 0] * 111_320.0 * np.cos(np.radians(np.mean(xy[:, 1]))), xy[:, 1] * 110_540.0])
    span = float(np.linalg.norm(np.ptp(xy, axis=0)))
    return span >= factor * float(np.median(accs))


def _non_collinear(points: np.ndarray) -> bool:
    if len(points) < 3:
        return False
    s = np.linalg.svd(points - points.mean(0), compute_uv=False)
    return s[1] > 1e-6 * max(s[0], 1e-12)


def resolve_marker(chunk, ref) -> Marker:
    """A marker of the chunk from itself, its label, its key or its target code (a coded
    target detected in the photos is named ``target <code>``)."""
    if isinstance(ref, Marker):
        return ref
    text = str(ref).strip()
    for m in chunk.markers:
        if m.label == text:
            return m
    if text.lstrip("-").isdigit():
        n = int(text)
        for m in chunk.markers:
            if m.code is not None and int(m.code) == n:
                return m
        for m in chunk.markers:
            if m.key == n:
                return m
    raise KeyError(f"no marker {ref!r} in chunk {chunk.label}")


def _bar_scale(chunk, bars) -> float | None:
    """The scale (world units per internal unit) the control scale bars ask for, weighted by
    ``1 / accuracy**2``; None without usable bars."""
    ratios, weights = [], []
    for sb in bars:
        a, b = chunk.marker_by_key(sb.marker_a), chunk.marker_by_key(sb.marker_b)
        if a.position is not None and b.position is not None:
            d = float(np.linalg.norm(a.position - b.position))
            if d > 0:
                ratios.append(sb.distance / d)
                weights.append(1.0 / max(sb.accuracy, 1e-6) ** 2)
    return float(np.average(ratios, weights=weights)) if ratios else None


def control_scalebars(chunk) -> list:
    return [sb for sb in chunk.scalebars if sb.enabled and not sb.checkpoint]


def update_transform(chunk, use_cameras: bool | None = None) -> dict:
    """Fit ``chunk.transform`` and return an accuracy report.

    Priority: control markers (>= 3) -> camera references (>= 3) -> scale bars
    (scale only). Observations are weighted by ``1 / accuracy**2``, so millimetric
    GCPs dominate metre-level GNSS camera positions. With camera references and no control
    point, the control scale bars set the scale (GNSS positions place and turn the survey; over
    a trench their metre-level noise would make its scale wrong). Check bars and check points
    only measure the result. The report lists control/check point and scale-bar errors.
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
        if use_cameras and control_scalebars(chunk) and not cameras_span_their_accuracy(chunk):
            use_cameras = False  # an object, a trench: local coordinates scaled by the bars
    if use_cameras and ctrl and crs_mod.is_local(chunk.crs):
        use_cameras = False  # local marker frame: GNSS camera positions live in another frame
    if use_cameras:
        cs, cd, ca = _camera_reference_pairs(chunk)
        src, dst, acc = np.vstack([src, cs]), np.vstack([dst, cd]), np.r_[acc, ca]
    bars = control_scalebars(chunk)
    if _non_collinear(dst) and len(src) >= 3:
        chunk.transform = geometry.umeyama(src, dst, with_scale=True, weights=1.0 / np.maximum(acc, 1e-6) ** 2)
        method = "markers+cameras" if use_cameras and len(ctrl) else ("cameras" if use_cameras else "markers")
        s = _bar_scale(chunk, bars) if (use_cameras and not len(ctrl)) else None
        if s:  # the GNSS cameras place and turn the survey, the bars give its scale
            ends = [chunk.marker_by_key(k).position for sb in bars for k in (sb.marker_a, sb.marker_b)
                    if chunk.marker_by_key(k).position is not None]
            c = chunk.to_world(np.mean(ends, axis=0)[None])[0]  # the bars keep their place
            k = s / chunk.scale
            about = np.eye(4)
            about[:3, :3] *= k
            about[:3, 3] = c - k * c
            chunk.transform = about @ chunk.transform
            method += "+scalebars"
    elif bars:
        s = _bar_scale(chunk, bars)
        if s:
            _, rot, _ = geometry.decompose_similarity(chunk.transform)
            chunk.transform = geometry.compose_similarity(s, rot, np.zeros(3))
            method = "scalebars"
            if not crs_mod.is_local(chunk.crs):  # a scale alone does not place the chunk in a CRS
                chunk.crs = crs_mod.LOCAL
    report = {"method": method, "crs": chunk.crs, "markers": marker_errors(chunk),
              "scalebars": scalebar_errors(chunk), "scale": float(chunk.scale)}
    for kind in ("control", "check"):
        errs = [e["error"] for e in report["markers"] if e["type"] == kind and e["error"] is not None]
        report[f"{kind}_rmse"] = float(np.sqrt(np.mean(np.square(errs)))) if errs else None
        errs = [e["error"] for e in report["scalebars"] if e["type"] == kind and e["error"] is not None]
        report[f"scalebar_{kind}_rmse"] = float(np.sqrt(np.mean(np.square(errs)))) if errs else None
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
    """Every scale bar: its distance, the distance between its markers in the chunk now and
    the difference (``type`` control or check; check bars and disabled bars do not scale)."""
    out = []
    s = chunk.scale
    for sb in chunk.scalebars:
        a, b = chunk.marker_by_key(sb.marker_a), chunk.marker_by_key(sb.marker_b)
        e = {"label": sb.label, "marker_a": a.label, "marker_b": b.label, "distance": sb.distance,
             "accuracy": sb.accuracy, "type": "check" if sb.checkpoint else "control", "enabled": sb.enabled,
             "estimated": None, "error": None}
        if a.position is not None and b.position is not None:
            est = float(s * np.linalg.norm(a.position - b.position))
            e.update(estimated=est, error=est - sb.distance)
        out.append(e)
    return out


def import_scalebars(chunk, path, accuracy: float = 0.001, delimiter: str | None = None) -> dict:
    """Scale bars from a text file: ``marker_a, marker_b, distance[, accuracy[, check]]`` per
    line (markers by label or target code, metres; separated by commas, semicolons, tabs or
    spaces; a first line that is not numbers is a header, ``#`` starts a comment). Returns
    ``{"bars": [labels], "missing": [pairs whose markers are not in the chunk]}``."""
    lines = [ln for ln in Path(path).read_text(encoding="utf-8-sig").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    if delimiter is None and lines:
        delimiter = next((d for d in (";", "\t", ",") if d in lines[0]), None)  # None: spaces
    bars, missing = [], []
    for i, line in enumerate(lines):
        row = [c.strip() for c in (line.split(delimiter) if delimiter else line.split())]
        if len(row) < 3:
            continue
        number = row[2].replace(",", ".") if delimiter != "," else row[2]
        try:
            distance = float(number)
        except ValueError:
            if i == 0:
                continue  # a header
            raise ValueError(f"line {i + 1}: the distance {row[2]!r} is not a number") from None
        acc = float(row[3].replace(",", ".")) if len(row) > 3 and row[3] else accuracy
        check = len(row) > 4 and row[4].lower() in ("1", "true", "check", "si", "sì", "yes", "x")
        try:
            a, b = resolve_marker(chunk, row[0]), resolve_marker(chunk, row[1])
        except KeyError:
            missing.append(f"{row[0]}-{row[1]}")
            continue
        bars.append(chunk.add_scalebar(a, b, distance, acc, checkpoint=check).label)
    return {"bars": bars, "missing": missing}
