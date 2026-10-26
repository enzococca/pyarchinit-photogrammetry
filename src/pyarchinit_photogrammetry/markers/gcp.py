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
    """Triangulate every marker seen in at least ``min_views`` aligned photos. A marker measured
    in fewer photos is placed where the ray of its measurement meets the surface
    (:func:`surface_point`), as Metashape does on its model: it gets coordinates, shows on the
    model and on the orthophoto and can end a scale bar; measured in a second photo it becomes a
    triangulated point."""
    points = _SurfacePoints(chunk)
    for m in chunk.markers:
        if not m.projections:  # placed in 3D where no photo sees it (or given a position): kept
            continue
        measured = {k: p for k, p in m.projections.items() if p.measured}
        views, obs, first = [], [], None
        for cam_key, proj in (measured or m.projections).items():  # estimates count only without measures
            try:
                cam = chunk.camera(cam_key)
            except KeyError:
                continue
            if not (cam.enabled and cam.aligned):
                continue
            norm = chunk.sensor(cam).unproject([[proj.x, proj.y]])[0]
            views.append(geometry.PinholeView(cam.transform))
            obs.append(norm)
            first = first or (cam, proj)
        if len(views) >= min_views:
            m.position = geometry.triangulate(views, np.asarray(obs))
        elif measured:
            m.position = surface_point(chunk, first[0], first[1].x, first[1].y, points) if first else None
        # estimated projections only, fewer than two photos (placed in 3D): the position stays


def views_of(chunk, marker, estimated: bool = False) -> int:
    """How many aligned photos measure a marker (two or more: triangulated; one: on the surface);
    with ``estimated`` the photos where it is only estimated instead (flags to confirm)."""
    n = 0
    for cam_key, proj in marker.projections.items():
        if proj.measured == estimated:
            continue
        try:
            cam = chunk.camera(cam_key)
        except KeyError:
            continue
        n += bool(cam.enabled and cam.aligned)
    return n


def photos_seeing(chunk, point, occlusion: bool = True) -> list[tuple]:
    """The aligned photos that see a point (internal frame): inside the frame, in front of the
    camera and, with ``occlusion``, not hidden by the surface (the dense cloud, else the tie
    points, near the point). ``[(camera, x, y, depth)]`` in COLMAP pixels, the photos where it
    falls nearest the centre first (Metashape's *Filter Photos by Point*)."""
    point = np.asarray(point, float).reshape(3)
    found = []
    for cam in chunk.aligned_cameras:
        sensor = chunk.sensor(cam)
        px, depth = cam.project(sensor, point[None])
        u, v = float(px[0, 0]), float(px[0, 1])
        if depth[0] <= 0 or not (0 <= u < sensor.width and 0 <= v < sensor.height):
            continue
        off = np.hypot(u - sensor.width / 2, v - sensor.height / 2) / np.hypot(sensor.width / 2, sensor.height / 2)
        found.append((off, cam, u, v, float(depth[0])))
    found.sort(key=lambda f: f[0])
    xyz = _SurfacePoints(chunk).xyz() if occlusion and found else None
    near = None
    if xyz is not None:
        reach = 0.2 * float(np.median([d for *_, d in found]))  # occluders that matter lie near the point
        near = xyz[np.einsum("ij,ij->i", xyz - point, xyz - point) < reach ** 2]
    out = []
    for _off, cam, u, v, depth in found:
        if near is not None and len(near) >= 15:
            origin, direction = camera_ray(chunk, cam, u, v)
            radius = depth * 20.0 / max(chunk.sensor(cam).focal_px, 1e-9)
            hit = geometry.point_on_ray(near, origin, direction, radius)
            dist = float(np.linalg.norm(point - origin))
            if hit is not None and float(np.linalg.norm(hit - origin)) < dist - max(0.01 * dist, 3 * radius):
                continue  # something stands between the camera and the point
        out.append((cam, u, v, depth))
    return out


_LAST_SURFACE: dict = {}  # the last surface read ({"key", "xyz"}): placing markers one after another reads it once


class _SurfacePoints:
    """The surface markers measured in one photo are placed on: the dense cloud, else the tie
    points (internal frame), read when first needed and kept until the file changes."""

    def __init__(self, chunk):
        self.chunk, self._xyz, self.key, self._diag = chunk, None, None, None
        for name in ("point_cloud", "tie_points"):
            path = chunk.product_path(name)
            if path is not None and path.exists():
                st = path.stat()
                self.name, self.key = name, (name, str(path), st.st_mtime_ns, st.st_size)
                break

    def xyz(self) -> np.ndarray | None:
        if self._xyz is None and self.key is not None:
            if _LAST_SURFACE.get("key") == self.key:
                self._xyz = _LAST_SURFACE["xyz"]
            else:
                cloud = self.chunk.point_cloud if self.name == "point_cloud" else self.chunk.tie_points
                xyz = None if cloud is None or len(cloud) < 15 else np.asarray(cloud.xyz)
                if xyz is not None:  # internal coordinates are small: single precision halves the memory
                    xyz = xyz.astype(np.float32 if np.abs(xyz).max() < 1e4 else np.float64)
                _LAST_SURFACE.clear()
                _LAST_SURFACE.update(key=self.key, xyz=xyz)
                self._xyz = xyz
        return self._xyz

    def diag(self) -> float:
        """Diagonal of the bounding box of the surface (0 without one)."""
        if self._diag is None:
            if _LAST_SURFACE.get("key") == self.key and "diag" in _LAST_SURFACE:
                self._diag = _LAST_SURFACE["diag"]
            else:
                xyz = self.xyz()
                self._diag = 0.0 if xyz is None else float(np.linalg.norm(np.ptp(xyz, axis=0)))
                if _LAST_SURFACE.get("key") == self.key:
                    _LAST_SURFACE["diag"] = self._diag
        return self._diag


def excluded_photos(chunk, marker) -> set[int]:
    """Photos where the projection of a marker was removed by hand: no estimate goes back there."""
    return {int(k) for k in chunk.meta.get("marker_excluded", {}).get(str(marker.key), [])}


def exclude_photo(chunk, marker, camera_key: int, excluded: bool = True) -> None:
    """Remember (or forget) that a marker is not to be estimated on a photo (it was removed there)."""
    table = chunk.meta.setdefault("marker_excluded", {})
    keys = excluded_photos(chunk, marker)
    keys = keys | {int(camera_key)} if excluded else keys - {int(camera_key)}
    if keys:
        table[str(marker.key)] = sorted(keys)
    else:
        table.pop(str(marker.key), None)


def project_marker(chunk, marker, point=None, max_photos: int = 60) -> int:
    """Estimated projections of a marker (``pinned`` None: Metashape's blue flags) on the photos
    that see it (:func:`photos_seeing`), at most ``max_photos``, from ``point`` (internal frame;
    default its position): for a marker placed in 3D, or measured in one photo and to be found in
    the others. Photos where it is measured keep their measure; earlier estimates are replaced.
    Dragging a flag in a photo (or confirming it) makes it a measure. Returns how many photos
    show it now as an estimate."""
    point = marker.position if point is None else point
    for k in [k for k, p in marker.projections.items() if not p.measured]:
        del marker.projections[k]
    if point is None:
        return 0
    added = 0
    skip = excluded_photos(chunk, marker)  # removed there by hand
    for cam, u, v, _depth in photos_seeing(chunk, point):
        if added >= max_photos:
            break
        if cam.key in marker.projections or cam.key in skip:  # measured there, or removed by hand
            continue
        chunk.add_marker_projection(marker, cam, u, v, pinned=None)
        added += 1
    return added


def camera_ray(chunk, cam, x: float, y: float) -> tuple[np.ndarray, np.ndarray]:
    """Origin and unit direction (internal frame) of the ray of pixel (x, y) of a photo."""
    xn, yn = chunk.sensor(cam).unproject(np.array([[x, y]]))[0]
    d = cam.transform[:3, :3] @ np.array([xn, yn, 1.0])
    return cam.transform[:3, 3].copy(), d / np.linalg.norm(d)


def surface_point(chunk, cam, x: float, y: float, points: _SurfacePoints | None = None) -> np.ndarray | None:
    """Where the ray of pixel (x, y) of a photo meets the surface, in the internal frame: a local
    plane fitted to the points of the dense cloud (else of the tie points) around the ray
    (:func:`geometry.point_on_ray`), first in a wide cylinder to find the depth, then in one of
    about 20 pixels at that depth. None when the ray meets no surface. Kept per measurement."""
    points = points or _SurfacePoints(chunk)
    if points.key is None:
        return None
    sensor = chunk.sensor(cam)
    key = (cam.key, round(float(x), 3), round(float(y), 3), np.asarray(cam.transform).tobytes(),
           tuple(map(float, sensor.params)), points.key)
    cache = chunk._surface_cache
    if key in cache:
        return None if cache[key] is None else cache[key].copy()
    xyz = points.xyz()
    found = None
    if xyz is not None:
        origin, direction = camera_ray(chunk, cam, x, y)
        diag = points.diag()
        v = xyz - origin
        t = v @ direction
        near = (t > 0) & (np.einsum("ij,ij->i", v, v) - t * t < (0.03 * diag) ** 2)
        sub = xyz[near]  # the points around the ray: every later pass looks only at them
        for frac in (0.01, 0.03):
            found = geometry.point_on_ray(sub, origin, direction, frac * diag)
            if found is not None:
                break
        if found is not None:
            depth = float((found - origin) @ direction)
            radius = depth * 20.0 / max(sensor.focal_px, 1e-9)  # about 20 pixels at that depth
            for k in (1, 2, 4, 8):
                fine = geometry.point_on_ray(sub, origin, direction, k * radius)
                if fine is not None:
                    found = fine
                    break
    if len(cache) > 1000:
        cache.clear()
    cache[key] = found
    return None if found is None else found.copy()


def marker_reprojection_errors(chunk, marker: Marker) -> dict[int, float]:
    """Pixel error of each projection of a triangulated marker."""
    out = {}
    if marker.position is None:
        return out
    for cam_key, proj in marker.projections.items():
        if not proj.measured:  # an estimate falls where the marker is: no error to speak of
            continue
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
    """Write markers (reference and estimated coordinates, errors) as CSV, or as an Excel
    workbook when ``path`` ends in ``.xlsx`` (the same columns, numbers as numbers)."""
    path = Path(path)
    errors = {e["label"]: e for e in marker_errors(chunk)}
    header = ["label", "x_ref", "y_ref", "z_ref", "x_est", "y_est", "z_est", "error_m", "projections", "type"]
    rows = []
    for m in chunk.markers:
        ref = m.reference.location or [None] * 3
        est = chunk.to_world(m.position) if m.position is not None else [None] * 3
        e = errors.get(m.label, {})
        rows.append([m.label, *ref, *est, e.get("error"), len(m.projections), "check" if m.checkpoint else "control"])
    if path.suffix.lower() == ".xlsx":
        from ..io.tables import Sheet, write_xlsx

        return write_xlsx(path, {"markers": Sheet(header, rows, [None, 3, 3, 3, 3, 3, 3, 4, 0, None])})
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return path


def camera_reference_errors(chunk) -> dict:
    """``{camera key: {"reference", "estimated", "delta", "error"}}`` for the cameras with a
    reference position (the GPS of the photos): the reference in the chunk CRS (converted from
    the CRS it was read in, as the georeferencing does) and, for an aligned camera of a
    georeferenced chunk, the estimated centre and ``estimated - reference`` in metres. Nothing
    of the chunk changes; references that cannot be compared yet (a chunk still in local
    coordinates) are left out."""
    groups: dict[str, list] = {}
    for cam in chunk.cameras:
        if cam.reference.location:
            groups.setdefault(cam.meta.get("reference_crs", chunk.crs), []).append(cam)
    out = {}
    for ref_crs, cams in groups.items():
        locs = np.array([cam.reference.location for cam in cams], float).reshape(-1, 3)
        if ref_crs != chunk.crs:
            if crs_mod.is_local(ref_crs) or crs_mod.is_local(chunk.crs) or crs_mod.is_geographic(chunk.crs):
                continue
            locs = crs_mod.transform_points(locs, ref_crs, chunk.crs)
        for cam, loc in zip(cams, locs):
            entry = {"reference": loc}
            if cam.aligned and not crs_mod.is_local(chunk.crs):
                est = np.asarray(chunk.to_world(cam.center), float)
                entry.update(estimated=est, delta=est - loc, error=float(np.linalg.norm(est - loc)))
            out[cam.key] = entry
    return out


def camera_references(chunk, cameras, crs: str | None = None) -> tuple[dict, str | None]:
    """The reference positions of ``cameras`` (those that have one) in one coordinate system: ``({camera key:
    position}, system)``. Every reference is read in its ``meta["reference_crs"]`` (the chunk's when it has none:
    one chunk can hold the GPS of the EXIF in EPSG:4326 and a positions file in a projected system) and converted to
    ``crs``; without it, to metric axes: the chunk's system when it is projected, else the first projected system of
    the references, else the UTM zone of the geographic ones. A reference that cannot be converted (a local frame
    and a real coordinate system) is left out."""
    groups: dict[str, list] = {}
    for cam in cameras:
        if cam.reference.location:
            groups.setdefault(cam.meta.get("reference_crs", chunk.crs), []).append(cam)
    if crs is None:
        crs = _metric_crs(chunk, groups)
    out = {}
    for ref_crs, cams in groups.items():
        locs = np.array([cam.reference.location for cam in cams], float).reshape(-1, 3)
        if ref_crs != crs and not (crs_mod.is_local(ref_crs) and crs_mod.is_local(crs)):
            if crs_mod.is_local(ref_crs) or crs_mod.is_local(crs):
                continue
            locs = crs_mod.transform_points(locs, ref_crs, crs)
        out.update((cam.key, loc) for cam, loc in zip(cams, locs))
    return out, crs


def _metric_crs(chunk, groups: dict[str, list]) -> str | None:
    """The metric system :func:`camera_references` brings references grouped by system to."""
    if not crs_mod.is_local(chunk.crs) and not crs_mod.is_geographic(chunk.crs):
        return chunk.crs
    real = [g for g in groups if not crs_mod.is_local(g)]
    if not real:
        return next(iter(groups), chunk.crs)
    projected = next((g for g in real if not crs_mod.is_geographic(g)), None)
    return projected or projected_crs_for(real[0], groups[real[0]][0].reference.location)


def _camera_reference_pairs(chunk) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(internal centres, reference positions in chunk CRS, accuracies) of referenced cameras; a chunk still in
    local coordinates takes the metric system of the references."""
    cams = [cam for cam in chunk.aligned_cameras if cam.reference.enabled and cam.reference.location]
    positions, crs = camera_references(chunk, cams, None if crs_mod.is_local(chunk.crs) else chunk.crs)
    if crs_mod.is_local(chunk.crs) and not crs_mod.is_local(crs):
        chunk.crs = crs
    cams = [cam for cam in cams if cam.key in positions]
    return (np.asarray([cam.center for cam in cams]).reshape(-1, 3),
            np.asarray([positions[cam.key] for cam in cams]).reshape(-1, 3),
            np.asarray([float(np.mean(cam.reference.accuracy)) for cam in cams]))


def cameras_span_their_accuracy(chunk, factor: float = 10.0) -> bool:
    """The camera references (the GPS of the photos) cover an area at least ``factor`` times
    their accuracy: they can place and turn the survey (a flight over a site), not an object
    on a table or a trench of a few metres, where they are only noise."""
    cams = [cam for cam in chunk.aligned_cameras if cam.reference.enabled and cam.reference.location]
    positions, _crs = camera_references(chunk, cams)
    if len(positions) < 3:
        return False
    xy = np.array([positions[cam.key][:2] for cam in cams if cam.key in positions])
    accs = [float(np.mean(cam.reference.accuracy[:2])) if cam.reference.accuracy else 5.0
            for cam in cams if cam.key in positions]
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


def _bar_observations(chunk, bars) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(lengths in the internal frame, distances, weights ``1 / accuracy**2``) of the bars whose
    two markers are placed."""
    lengths, distances, weights = [], [], []
    for sb in bars:
        a, b = chunk.marker_by_key(sb.marker_a), chunk.marker_by_key(sb.marker_b)
        if a.position is not None and b.position is not None:
            d = float(np.linalg.norm(a.position - b.position))
            if d > 0:
                lengths.append(d)
                distances.append(sb.distance)
                weights.append(1.0 / max(sb.accuracy, 1e-6) ** 2)
    return np.array(lengths), np.array(distances), np.array(weights)


def _bar_scale(chunk, bars) -> float | None:
    """The scale (world units per internal unit) the control scale bars ask for: least squares of
    ``scale * length - distance`` weighted by ``1 / accuracy**2`` (of two bars measured equally
    well, the longer one gives the scale more precisely); None without usable bars."""
    d, dist, w = _bar_observations(chunk, bars)
    return float(np.sum(w * dist * d) / np.sum(w * d * d)) if len(d) else None


def similarity_with_bars(src, dst, weights, lengths=(), distances=(), bar_weights=()) -> np.ndarray:
    """Weighted similarity mapping the points ``src`` onto ``dst`` with scale bars as further
    observations of the scale (``scale * length = distance``): the joint least-squares solution,
    every observation weighted by ``1 / accuracy**2``. The rotation and the centroid come from the
    points, the scale from points and bars together: a bar measured to the millimetre outweighs
    camera positions known to metres, positions known to centimetres over a whole site outweigh
    a short bar."""
    src, dst, w = np.asarray(src, float), np.asarray(dst, float), np.asarray(weights, float)
    m = geometry.umeyama(src, dst, with_scale=True, weights=w)
    d, dist, wb = (np.asarray(v, float) for v in (lengths, distances, bar_weights))
    if not len(d):
        return m
    _, rot, _ = geometry.decompose_similarity(m)  # the best rotation for any positive scale
    mu_s, mu_d = w @ src / w.sum(), w @ dst / w.sum()
    xs, xd = src - mu_s, dst - mu_d
    scale = ((np.sum(w * np.einsum("ij,ij->i", xd, xs @ rot.T)) + np.sum(wb * dist * d))
             / (np.sum(w * np.einsum("ij,ij->i", xs, xs)) + np.sum(wb * d * d)))
    return geometry.compose_similarity(float(scale), rot, mu_d - scale * rot @ mu_s)


def _has_references(chunk) -> bool:
    """The chunk has enabled coordinates to be placed by: control points or camera positions."""
    return (any(not m.checkpoint and m.reference.enabled and m.reference.location for m in chunk.markers)
            or any(c.reference.enabled and c.reference.location for c in chunk.aligned_cameras))


def control_scalebars(chunk) -> list:
    return [sb for sb in chunk.scalebars if sb.enabled and not sb.checkpoint]


def update_transform(chunk, use_cameras: bool | None = None) -> dict:
    """Fit ``chunk.transform`` and return an accuracy report.

    Priority: control markers (>= 3) -> camera references (>= 3) -> scale bars
    (scale only). Observations are weighted by ``1 / accuracy**2``, so millimetric
    GCPs dominate metre-level GNSS camera positions, and the control scale bars join the fit
    as observations of the scale (:func:`similarity_with_bars`). With scale bars, camera
    positions that cover less than ten times their accuracy (a phone or a drone over an object
    or a trench) are left out: the bars scale the chunk in local coordinates. Check bars and
    check points only measure the result. The report lists control/check point and scale-bar
    errors.
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
        d, dist, wb = _bar_observations(chunk, bars)
        chunk.transform = similarity_with_bars(src, dst, 1.0 / np.maximum(acc, 1e-6) ** 2, d, dist, wb)
        method = "markers+cameras" if use_cameras and len(ctrl) else ("cameras" if use_cameras else "markers")
        if len(d):
            method += "+scalebars"
    elif bars:
        s = _bar_scale(chunk, bars)
        if s and not crs_mod.is_local(chunk.crs) and not _has_references(chunk):
            # georeferenced elsewhere (an imported project) with nothing to fit it again: the bars
            # scale it where it is, about their centre, in its CRS
            ends = [chunk.marker_by_key(k).position for sb in bars for k in (sb.marker_a, sb.marker_b)
                    if chunk.marker_by_key(k).position is not None]
            c = chunk.to_world(np.mean(ends, axis=0)[None])[0]
            k = s / chunk.scale
            about = np.eye(4)
            about[:3, :3] *= k
            about[:3, 3] = c - k * c
            chunk.transform = about @ chunk.transform
            method = "scalebars"
        elif s:
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
             "dx": None, "dy": None, "dz": None, "projections": sum(p.measured for p in m.projections.values()),
             "estimated": sum(not p.measured for p in m.projections.values()), "pixel_error": None}
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
