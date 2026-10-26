"""Import Agisoft Metashape projects (``.psx``) without Metashape.

A ``.psx`` points to ``<name>.files/project.zip`` (the list of chunks); every chunk has
``<n>/chunk.zip`` (sensors with their calibration, camera poses, markers, the chunk
transform and the coordinate system) and ``<n>/0/frame.zip`` (photo paths, marker
projections, and the paths of the tie points, model, DEM and orthomosaic archives).
The importer reads those XML documents and the tie points / model archives and builds a
chunk that is already aligned: the dense cloud, mesh, DEM, orthomosaic, sections,
PyArchInit publishing and IFC export of this program then work on it directly.

Conventions (Metashape 1.x/2.x):

* camera ``transform`` = camera -> chunk internal frame (x right, y down, z forward, as here);
* chunk ``transform`` = internal -> geocentric (for geographic/projected CRSs) or -> local;
* calibration: ``f`` (px), ``cx, cy`` offsets from the image centre, ``b1`` affinity,
  ``k1..k4`` radial, ``p1, p2`` tangential with Metashape's order (OpenCV's p2, p1).

Exported products (DEM and orthomosaic GeoTIFFs) found next to the project are linked
as well. When no exported DEM is usable (none, or an empty file left by a failed export),
the DEM kept inside the project is used: Metashape stores it as GeoTIFF tiles listed in
``elevation.zip``; they are mosaicked and warped to the CRS of the chunk. The orthomosaic
inside the project (image blocks and seamlines) is not read: export it as GeoTIFF.

The model (``mesh.ply`` in ``model.zip``, often millions of faces) is extracted as a stream
and a coarse copy is made for the 3D view; when the project has no dense cloud (Metashape
2.x builds the mesh from depth maps), the coloured mesh vertices become the dense cloud.
Cameras and markers inside groups (folders of the Workspace) are read too.
"""
from __future__ import annotations

import io
import logging
import os
import re
import shutil
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class MsSensor:
    id: int
    label: str
    width: int
    height: int
    calib: dict[str, float] = field(default_factory=dict)


@dataclass
class MsCamera:
    id: int
    label: str
    sensor_id: int
    transform: np.ndarray | None
    enabled: bool = True
    path: str | None = None
    component_id: int | None = None


@dataclass
class MsMarker:
    id: int
    label: str
    reference: list[float] | None = None
    enabled: bool = True
    projections: dict[int, tuple[float, float]] = field(default_factory=dict)  # camera id -> pixel
    accuracy: list[float] | None = None  # of the reference; None: the chunk default


@dataclass
class MsScaleBar:
    id: int
    label: str
    markers: tuple[int, int] | None  # marker ids of the two ends; None: a bar between cameras
    distance: float | None  # None: a bar without a reference distance (a measurement only)
    accuracy: float | None = None  # None: the chunk default
    enabled: bool = True  # unticked in Metashape's Reference pane: a check bar


@dataclass
class MsChunk:
    label: str
    folder: Path  # <name>.files/<n>
    sensors: dict[int, MsSensor]
    cameras: list[MsCamera]
    markers: list[MsMarker]
    transform: np.ndarray  # internal -> world (geocentric or local)
    crs_wkt: str | None
    frame_dir: Path | None
    frame_paths: dict[str, str] = field(default_factory=dict)  # product -> archive path in the frame
    components: dict[int, np.ndarray] = field(default_factory=dict)  # alignment component -> world (4x4)
    scalebars: list[MsScaleBar] = field(default_factory=list)
    settings: dict[str, float] = field(default_factory=dict)  # reference settings (accuracy_markers, ...)

    def component_transform(self, cid: int | None) -> np.ndarray:
        """Frame of a camera or point part: its component's transform, else the chunk's."""
        return self.components.get(cid, self.transform) if cid is not None else self.transform


def _floats(text: str | None) -> list[float]:
    return [float(v) for v in (text or "").split()]


def _transform(te) -> np.ndarray | None:
    """4x4 from a ``<transform>`` (rotation, translation, scale) element. Each part may be
    missing: a chunk in local coordinates scaled by scale bars has the scale alone."""
    if te is None:
        return None
    m = np.eye(4)
    rot = _floats(te.findtext("rotation"))
    trans = _floats(te.findtext("translation"))
    scale = float(te.findtext("scale") or 1.0)
    m[:3, :3] = (np.array(rot).reshape(3, 3) if len(rot) == 9 else np.eye(3)) * scale
    if len(trans) == 3:
        m[:3, 3] = trans
    return m


def _accuracy(ref, names: tuple[str, ...], together: str) -> list[float] | None:
    """Accuracy of a ``<reference>``: one value per axis (``sx``, ``sy``, ``sz``) or one for all
    (``sxyz``; ``sd`` for the distance of a scale bar); None when not set (the chunk default)."""
    if ref is None:
        return None
    if ref.get(together) is not None:
        return [float(ref.get(together))] * max(1, len(names))
    if names and all(ref.get(n) is not None for n in names):
        return [float(ref.get(n)) for n in names]
    return None


def _xml_from_zip(path: Path) -> ET.Element:
    with zipfile.ZipFile(path) as z:
        return ET.fromstring(z.read("doc.xml"))


def read_psx(psx: str | os.PathLike) -> list[MsChunk]:
    psx = Path(psx)
    files = psx.with_suffix(".files")
    project = _xml_from_zip(files / "project.zip")
    chunks = []
    for ce in project.findall("chunks/chunk"):
        cpath = files / ce.get("path")
        doc = _xml_from_zip(cpath)
        folder = cpath.parent
        sensors = {}
        for s in doc.findall("sensors/sensor"):
            res = s.find("resolution")
            cal = s.find("calibration")
            calib = {}
            if cal is not None:
                for k in ("f", "cx", "cy", "b1", "b2", "k1", "k2", "k3", "k4", "p1", "p2", "p3", "p4"):
                    el = cal.find(k)
                    if el is not None:
                        calib[k] = float(el.text)
                cres = cal.find("resolution")
                if res is None and cres is not None:
                    res = cres
            sensors[int(s.get("id"))] = MsSensor(int(s.get("id")), s.get("label", ""), int(res.get("width")),
                                                 int(res.get("height")), calib)
        cameras = []
        for c in doc.findall("cameras//camera"):  # also inside camera groups
            t = c.find("transform")
            m = np.array(_floats(t.text)).reshape(4, 4) if t is not None else None
            cid = c.get("component_id")
            cameras.append(MsCamera(int(c.get("id")), c.get("label", ""), int(c.get("sensor_id", 0)), m,
                                    c.get("enabled", "true") != "false", component_id=None if cid is None else int(cid)))
        markers = []
        for mk in doc.findall("markers//marker"):
            ref = mk.find("reference")
            loc = None
            if ref is not None and ref.get("x") is not None:
                loc = [float(ref.get("x")), float(ref.get("y")), float(ref.get("z", 0.0))]
            markers.append(MsMarker(int(mk.get("id")), mk.get("label", ""), loc,
                                    ref is None or ref.get("enabled", "true") != "false",
                                    accuracy=_accuracy(ref, ("sx", "sy", "sz"), "sxyz")))
        settings = {}
        for prop in doc.findall("settings/property"):
            try:
                settings[prop.get("name", "")] = float(prop.get("value"))
            except (TypeError, ValueError):
                pass
        scalebars = []
        for sb in doc.findall("scalebars//scalebar"):  # also inside scale bar groups
            ends = sb.findall("endpoint")
            ids = [e.get("marker_id") for e in ends]
            ref = sb.find("reference")
            d = ref.get("d") if ref is not None else None
            acc = _accuracy(ref, (), "sd")
            scalebars.append(MsScaleBar(
                int(sb.get("id", len(scalebars))), sb.get("label", ""),
                (int(ids[0]), int(ids[1])) if len(ids) == 2 and None not in ids else None,
                float(d) if d not in (None, "") else None, acc[0] if acc else None,
                ref is None or ref.get("enabled", "true") != "false"))
        components = {int(c.get("id")): _transform(c.find("transform")) for c in doc.findall("components/component")
                      if c.find("transform") is not None}
        tr = _transform(doc.find("transform"))
        if tr is None:  # no chunk transform: the active component's
            active = (doc.find("components").get("active_id") if doc.find("components") is not None else None)
            tr = components.get(int(active)) if active is not None and int(active) in components else np.eye(4)
        wkt = (doc.findtext("reference") or "").strip() or None
        ch = MsChunk(doc.get("label", f"Chunk {ce.get('id')}"), folder, sensors, cameras, markers, tr, wkt, None,
                     components=components, scalebars=scalebars, settings=settings)
        frame = doc.find("frames/frame")
        if frame is not None:
            fpath = folder / frame.get("path")
            ch.frame_dir = fpath.parent
            fdoc = _xml_from_zip(fpath)
            by_id = {c.id: c for c in cameras}
            for fc in fdoc.findall("cameras//camera"):
                ph = fc.find("photo")
                cam = by_id.get(int(fc.get("camera_id")))
                if cam is not None and ph is not None:
                    cam.path = ph.get("path")
            mk_by_id = {m.id: m for m in markers}
            for fm in fdoc.findall("markers//marker"):
                m = mk_by_id.get(int(fm.get("marker_id")))
                if m is None:
                    continue
                for loc in fm.findall("location"):
                    m.projections[int(loc.get("camera_id"))] = (float(loc.get("x")), float(loc.get("y")))
            for tag in ("point_cloud", "model", "elevation", "orthomosaic", "tie_points", "dense_cloud"):
                el = fdoc.find(tag)
                if el is None:  # Metashape 2.x may keep several: <models><model .../></models>
                    group = fdoc.find(tag + "s")
                    el = None if group is None else next((e for e in group if e.get("path")), None)
                if el is not None and el.get("path"):
                    ch.frame_paths[tag] = el.get("path")
        chunks.append(ch)
    return chunks


def sensor_params(s: MsSensor) -> tuple[str, list[float], list[str]]:
    """COLMAP FULL_OPENCV parameters of a Metashape calibration, plus warnings."""
    c = s.calib
    warnings = []
    f = c.get("f", 1.2 * max(s.width, s.height))
    fx, fy = f + c.get("b1", 0.0), f
    cx, cy = s.width / 2.0 + c.get("cx", 0.0), s.height / 2.0 + c.get("cy", 0.0)
    if abs(c.get("b2", 0.0)) > 1e-9:
        warnings.append(f"{s.label}: skew b2 ignorato")
    if abs(c.get("k4", 0.0)) > 1e-12:
        warnings.append(f"{s.label}: k4 radiale ignorato")
    params = [fx, fy, cx, cy, c.get("k1", 0.0), c.get("k2", 0.0), c.get("p2", 0.0), c.get("p1", 0.0),
              c.get("k3", 0.0), 0.0, 0.0, 0.0]
    return "FULL_OPENCV", params, warnings


def _target_crs(ch: MsChunk, prefer: str | None) -> str:
    """CRS of the imported chunk: ``prefer`` (e.g. of an exported DEM), the projected CRS of
    the project, the UTM zone for geographic ones, LOCAL for local frames."""
    if prefer:
        return prefer
    if not ch.crs_wkt or ch.crs_wkt.startswith("LOCAL_CS") or "LOCAL" in ch.crs_wkt[:12]:
        return "LOCAL"
    from pyproj import CRS

    crs = CRS.from_wkt(ch.crs_wkt)
    if crs.is_projected and crs.to_epsg():
        return f"EPSG:{crs.to_epsg()}"
    centre = ch.transform[:3, 3]
    from pyproj import Transformer

    lon, lat, _ = Transformer.from_crs("EPSG:4978", "EPSG:4326", always_xy=True).transform(*centre)
    zone = int((lon + 180) // 6) + 1
    return f"EPSG:{(32600 if lat >= 0 else 32700) + zone}"


def crs_mapper(ch: MsChunk, crs: str, transform: np.ndarray | None = None):
    """Exact internal -> ``crs`` conversion, point by point, through the project's
    geocentric frame (a function of (n, 3) arrays); ``transform``: the frame of an
    alignment component instead of the chunk's."""
    from pyproj import CRS, Transformer
    from pyproj.crs import GeocentricCRS

    from ..core.geometry import apply

    src = CRS.from_wkt(ch.crs_wkt) if ch.crs_wkt else CRS.from_epsg(4326)
    geocentric = GeocentricCRS(name="geocentric", datum=src.datum) if src.datum is not None else CRS.from_epsg(4978)
    t = Transformer.from_crs(geocentric, CRS.from_user_input(crs), always_xy=True)

    T = ch.transform if transform is None else transform

    def to_crs(points: np.ndarray) -> np.ndarray:
        geo = apply(T, np.asarray(points, float))
        x, y, z = t.transform(geo[:, 0], geo[:, 1], geo[:, 2])
        return np.column_stack([x, y, z])

    return to_crs


def reframe_cameras(transforms: list[np.ndarray], frame, unit: float) -> list[np.ndarray]:
    """Camera poses (camera -> internal) through a point-by-point conversion ``frame``:
    the centre is converted exactly and the rotation is turned by the closest rotation to
    the local Jacobian of the conversion (finite differences over ``unit``)."""
    if not transforms:
        return []
    C = np.array([t[:3, 3] for t in transforms])
    E = np.eye(3)
    pts = np.concatenate([C] + [C + unit * E[k] for k in range(3)] + [C - unit * E[k] for k in range(3)])
    W = np.asarray(frame(pts)).reshape(7, len(C), 3)
    J = np.stack([(W[1 + k] - W[4 + k]) / (2 * unit) for k in range(3)], axis=2)  # column k: d frame / d e_k
    U, _, Vt = np.linalg.svd(J)
    flip = np.linalg.det(U @ Vt) < 0
    U[flip, :, -1] *= -1
    Q = U @ Vt
    out = []
    for t, q, w in zip(transforms, Q, W[0]):
        m = np.eye(4)
        m[:3, :3] = q @ t[:3, :3]
        m[:3, 3] = w
        out.append(m)
    return out


def internal_to_crs(ch: MsChunk, crs: str, samples: np.ndarray) -> tuple[np.ndarray, float]:
    """Similarity internal -> ``crs`` fitted on ``samples`` (internal points) through the
    geocentric frame of the project; returns the 4x4 matrix and the fit residual (m)."""
    from ..core.geometry import apply, umeyama

    geo = apply(ch.transform, samples)
    if crs == "LOCAL":
        return ch.transform.copy(), 0.0
    from pyproj import CRS, Transformer
    from pyproj.crs import GeocentricCRS

    src = CRS.from_wkt(ch.crs_wkt) if ch.crs_wkt else CRS.from_epsg(4326)
    geocentric = GeocentricCRS(name="geocentric", datum=src.datum) if src.datum is not None else CRS.from_epsg(4978)
    t = Transformer.from_crs(geocentric, CRS.from_user_input(crs), always_xy=True)
    x, y, z = t.transform(geo[:, 0], geo[:, 1], geo[:, 2])
    world = np.column_stack([x, y, z])
    m = umeyama(samples, world, with_scale=True)
    res = float(np.sqrt(np.mean(np.sum((apply(m, samples) - world) ** 2, axis=1))))
    return m, res


def _cloud_parts(path: Path, valid: set[int] | None = None):
    """Parts of a Metashape point cloud archive: ``([(component_id, xyz, rgb)], is_tie_points,
    skipped_points)``. Tie points come with ``tracks.ply`` (colours) and per-camera
    projections; there is one ``points<N>.ply`` per alignment component. Parts of components
    that are no longer in the chunk (``valid``) are left out: they are in a stale frame."""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        comp_of = {}
        if "doc.xml" in names:
            for el in ET.fromstring(z.read("doc.xml")).findall("points"):
                if el.get("path") and el.get("component_id") is not None:
                    comp_of[el.get("path")] = int(el.get("component_id"))
        files = sorted((n for n in names if re.fullmatch(r"points\d*\.ply", n)),
                       key=lambda n: int(re.sub(r"\D", "", n) or 0))
        tracks = _ply_vertices(z.read("tracks.ply")) if "tracks.ply" in names else None
        parts, skipped = [], 0
        for name in files:
            cid = comp_of.get(name)
            pts = _ply_vertices(z.read(name))
            if valid and cid is not None and cid not in valid:
                skipped += len(pts)
                continue
            xyz = np.column_stack([pts["x"], pts["y"], pts["z"]]).astype(np.float64)
            rgb = None
            if tracks is not None and "id" in pts.dtype.names and {"red", "green", "blue"} <= set(tracks.dtype.names):
                ids = np.clip(pts["id"].astype(np.int64), 0, len(tracks) - 1)
                rgb = np.column_stack([tracks["red"][ids], tracks["green"][ids], tracks["blue"][ids]]).astype(np.uint8)
            elif {"red", "green", "blue"} <= set(pts.dtype.names):
                rgb = np.column_stack([pts["red"], pts["green"], pts["blue"]]).astype(np.uint8)
            parts.append((cid, xyz, rgb))
    return parts, tracks is not None, skipped


def _read_clouds(ch: MsChunk):
    """``(tie point parts, dense cloud parts, skipped points)``, each a list of
    ``(component_id, xyz, rgb)`` in the component frames. Archives are told apart by their
    content, not by their name (``point_cloud`` holds the tie points in the files of
    Metashape 1.x and 2.x; dense clouds stored as PLY parts are read when present)."""
    tie, dense, skipped = None, None, 0
    seen = set()
    valid = set(ch.components) or None
    for tag in ("tie_points", "point_cloud", "dense_cloud"):
        rel = ch.frame_paths.get(tag)
        if not rel or ch.frame_dir is None or rel in seen:
            continue
        seen.add(rel)
        path = ch.frame_dir / rel
        if not path.exists():
            continue
        try:
            parts, is_tie, lost = _cloud_parts(path, valid)
        except (KeyError, ValueError, OSError, zipfile.BadZipFile, ET.ParseError):
            continue
        if not parts:
            continue
        if is_tie and tie is None:
            tie, skipped = parts, lost
        elif not is_tie and dense is None:
            dense = parts
    return tie, dense, skipped


def _merge_parts(parts, convert, max_points: int | None = None):
    """One PointCloud from converted parts (``convert(component_id, xyz)``)."""
    from ..core.pointcloud import PointCloud

    if not parts:
        return None
    xyz = np.vstack([convert(cid, p) for cid, p, _ in parts])
    rgb = np.vstack([c for _, _, c in parts]) if all(c is not None for _, _, c in parts) else None
    if max_points and len(xyz) > max_points:
        idx = np.sort(np.random.default_rng(0).choice(len(xyz), max_points, replace=False))
        xyz, rgb = xyz[idx], (rgb[idx] if rgb is not None else None)
    return PointCloud(xyz, rgb)


_PLY_TYPES = {"float": "<f4", "float32": "<f4", "double": "<f8", "float64": "<f8", "uchar": "u1", "uint8": "u1",
              "char": "i1", "int8": "i1", "ushort": "<u2", "uint16": "<u2", "short": "<i2", "int16": "<i2",
              "uint": "<u4", "uint32": "<u4", "int": "<i4", "int32": "<i4"}


def _ply_vertices(data: bytes) -> np.ndarray:
    """Vertex element of a binary little-endian PLY made only of a vertex element."""
    head_end = data.index(b"end_header") + len(b"end_header")
    head_end += 2 if data[head_end:head_end + 2] == b"\r\n" else 1
    header = data[:head_end].decode("ascii", "replace").splitlines()
    n, props, in_vertex = 0, [], False
    for ln in header:
        parts = ln.split()
        if parts[:2] == ["element", "vertex"]:
            n, in_vertex = int(parts[2]), True
        elif parts and parts[0] == "element":
            in_vertex = False
        elif in_vertex and parts[:1] == ["property"] and parts[1] != "list":
            props.append((parts[2], _PLY_TYPES[parts[1]]))
    return np.frombuffer(data, dtype=np.dtype(props), count=n, offset=head_end)


def _find_exported(psx: Path, max_depth: int = 3, empty: list | None = None) -> dict[str, Path]:
    """DEM and orthomosaic GeoTIFFs exported next to the project (common folder names).
    Project data folders (``*.files``) and sub-folders holding another ``.psx`` (another
    project) are not searched; files named after the project come first. Empty files (an
    export that failed) are skipped and listed in ``empty``."""
    base = psx.parent
    tifs = []
    for root, dirs, files in os.walk(base):
        rel_depth = len(Path(root).relative_to(base).parts)
        here = Path(root)
        if rel_depth and any(f.lower().endswith(".psx") for f in files):
            dirs[:] = []  # another project: its exports are not ours
            continue
        dirs[:] = [d for d in dirs if not d.endswith(".files") and not d.startswith(".") and rel_depth < max_depth]
        for f in files:
            if f.lower().endswith((".tif", ".tiff")) and not f.startswith("."):
                p = here / f
                try:
                    if p.stat().st_size > 0:
                        tifs.append(p)
                    elif empty is not None and re.search(r"(dem|dsm|dtm|elev|ortho|orto)", str(p.relative_to(base)).lower()):
                        empty.append(p)
                except OSError:
                    continue
    stem = re.sub(r"[^a-z0-9]+", "", psx.stem.lower())

    def rank(p: Path):
        name = re.sub(r"[^a-z0-9]+", "", p.name.lower())
        return (0 if stem and stem in name else 1, -p.stat().st_size)

    out = {}
    for p in sorted(tifs, key=rank):
        low = str(p.relative_to(base)).lower()
        if "dem" not in out and re.search(r"(dem|dsm|dtm|elev)", low):
            out["dem"] = p
        elif "orthomosaic" not in out and re.search(r"(ortho|orto)", low):
            out["orthomosaic"] = p
    return out


def _raster_covers(path: Path, xy: np.ndarray, crs: str) -> bool:
    """Whether a GeoTIFF covers at least half of the points ``xy`` (given in ``crs``)."""
    import rasterio

    try:
        with rasterio.open(path) as src:
            b = src.bounds
            pts = np.asarray(xy, float)
            if src.crs is not None and crs not in ("", "LOCAL") and src.crs.to_epsg() and f"EPSG:{src.crs.to_epsg()}" != crs:
                from pyproj import Transformer

                t = Transformer.from_crs(crs, src.crs, always_xy=True)
                pts = np.column_stack(t.transform(pts[:, 0], pts[:, 1]))
    except Exception:
        return False
    inside = (pts[:, 0] >= b.left) & (pts[:, 0] <= b.right) & (pts[:, 1] >= b.bottom) & (pts[:, 1] <= b.top)
    return bool(len(pts)) and inside.mean() >= 0.5


def internal_dem(mc: MsChunk) -> dict | None:
    """The DEM kept inside the project (``elevation.zip`` of the frame): its CRS (WKT),
    no-data value, resolution and the GeoTIFF tiles of the full-resolution level, or None."""
    rel = mc.frame_paths.get("elevation")
    if not rel or mc.frame_dir is None:
        return None
    archive = mc.frame_dir / rel
    try:
        root = _xml_from_zip(archive)
    except (OSError, KeyError, zipfile.BadZipFile, ET.ParseError):
        return None
    tiles = []
    for t in root.findall("tiles/tile"):
        path = t.findtext("path")
        if t.get("level") in (None, "0") and path and (archive.parent / path).is_file():
            tiles.append(archive.parent / path)  # other levels are coarser copies for display
    if not tiles:
        return None
    params = root.find("params")
    meta = {p.get("name"): p.get("value") for p in root.findall("meta/property")}
    try:
        res = float(meta.get("BuildDem/resolution") or 0) or None
    except ValueError:
        res = None
    nodata = params.findtext("nodataValue") if params is not None else None
    return {"crs": (params.findtext("crs") or "").strip() if params is not None else "",
            "nodata": float(nodata) if nodata else -32767.0, "resolution": res, "tiles": tiles}


def mosaic_dem(info: dict, out: str | os.PathLike, crs: str, progress=None) -> Path:
    """Metashape's tiled DEM (see :func:`internal_dem`) as one GeoTIFF in ``crs``, written by
    blocks of rows, so its size is not limited by memory. Bilinear resampling; the grid
    keeps Metashape's resolution. Nothing is written next to the tiles."""
    import tempfile

    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_origin
    from rasterio.vrt import WarpedVRT
    from rasterio.warp import calculate_default_transform, transform_bounds
    from rasterio.windows import Window

    from ..core import crs as crs_mod
    from ..core.progress import as_progress
    from ..engine.dem import NODATA

    progress = as_progress(progress)
    out = Path(out)
    nodata = float(info.get("nodata", -32767.0))
    # GDAL_PAM_ENABLED=NO: no .aux.xml next to the tiles (the Metashape project is left as it is)
    with rasterio.Env(GDAL_PAM_ENABLED="NO"), tempfile.TemporaryDirectory(prefix="ppg-dem-") as tmp:
        tiles = []
        for p in info["tiles"]:
            with rasterio.open(p) as src:
                tiles.append((Path(p), src.transform, src.width, src.height, src.crs))
        t0 = tiles[0][1]
        rx, ry = t0.a, -t0.e  # tiles share pixel size and alignment
        left = min(t.c for _, t, *_ in tiles)
        top = max(t.f for _, t, *_ in tiles)
        right = max(t.c + w * t.a for _, t, w, _, _ in tiles)
        bottom = min(t.f + h * t.e for _, t, _, h, _ in tiles)
        width, height = int(round((right - left) / rx)), int(round((top - bottom) / ry))
        src_crs = tiles[0][4] or (rasterio.crs.CRS.from_wkt(info["crs"]) if info.get("crs") else None)
        sources = "".join(
            f'<ComplexSource><SourceFilename relativeToVRT="0">{_xml_escape(str(p))}</SourceFilename>'
            f"<SourceBand>1</SourceBand><SrcRect xOff=\"0\" yOff=\"0\" xSize=\"{w}\" ySize=\"{h}\"/>"
            f'<DstRect xOff="{int(round((t.c - left) / rx))}" yOff="{int(round((top - t.f) / ry))}" '
            f'xSize="{w}" ySize="{h}"/><NODATA>{nodata:g}</NODATA></ComplexSource>'
            for p, t, w, h, _ in tiles)
        vrt = Path(tmp) / "mosaic.vrt"
        vrt.write_text(
            f'<VRTDataset rasterXSize="{width}" rasterYSize="{height}">'
            + (f"<SRS>{_xml_escape(src_crs.to_wkt())}</SRS>" if src_crs is not None else "")
            + f"<GeoTransform>{left!r}, {rx!r}, 0, {top!r}, 0, {-ry!r}</GeoTransform>"
            f'<VRTRasterBand dataType="Float32" band="1"><NoDataValue>{nodata:g}</NoDataValue>{sources}'
            "</VRTRasterBand></VRTDataset>", encoding="utf-8")
        with rasterio.open(vrt) as mosaic:
            local = crs_mod.is_local(crs) or src_crs is None
            if local:
                dst_crs, dst_tr, dst_w, dst_h = src_crs, mosaic.transform, width, height
            else:
                dst_crs = rasterio.crs.CRS.from_user_input(crs)
                res = info.get("resolution")
                if not res:
                    tr, _, _ = calculate_default_transform(src_crs, dst_crs, width, height, *mosaic.bounds)
                    res = abs(tr.a)
                l, b, r, t = transform_bounds(src_crs, dst_crs, *mosaic.bounds, densify_pts=21)
                x0, y1 = np.floor(l / res) * res, np.ceil(t / res) * res
                dst_w, dst_h = int(np.ceil((r - x0) / res)), int(np.ceil((y1 - b) / res))
                dst_tr = from_origin(x0, y1, res, res)
            profile = {"driver": "GTiff", "width": dst_w, "height": dst_h, "count": 1, "dtype": "float32",
                       "transform": dst_tr, "nodata": NODATA, "compress": "deflate", "tiled": True,
                       "blockxsize": 256, "blockysize": 256, "BIGTIFF": "IF_SAFER"}
            if dst_crs is not None:
                profile["crs"] = dst_crs
            out.parent.mkdir(parents=True, exist_ok=True)
            vrt_args = {} if local else {"crs": dst_crs, "transform": dst_tr, "width": dst_w, "height": dst_h,
                                         "resampling": Resampling.bilinear,
                                         "NUM_THREADS": "ALL_CPUS"}
            with WarpedVRT(mosaic, src_nodata=nodata, nodata=NODATA, dtype="float32", **vrt_args) as src, \
                    rasterio.open(out, "w", **profile) as dst:
                rows = 1024
                for row in range(0, dst_h, rows):
                    win = Window(0, row, dst_w, min(rows, dst_h - row))
                    dst.write(src.read(1, window=win), 1, window=win)
                    progress(0.95 * min(1.0, (row + rows) / dst_h), f"DEM di Metashape: riga {row} di {dst_h}")
                factors = [f for f in (2, 4, 8, 16, 32) if min(dst_w, dst_h) // f >= 64]
                if factors:
                    dst.build_overviews(factors, Resampling.average)
    progress(1.0, "DEM di Metashape pronto")
    return out


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _dem_offset(dem: Path, xyz: np.ndarray) -> float | None:
    """Median of (point z - DEM z) at the points inside the DEM: a check that the DEM heights
    are in the same vertical reference as the imported points."""
    import rasterio

    with rasterio.open(dem) as src:
        vals = np.array([v[0] for v in src.sample(xyz[:, :2])], float)
        if src.nodata is not None:
            vals[vals == src.nodata] = np.nan
    ok = np.isfinite(vals)
    return float(np.median(xyz[ok, 2] - vals[ok])) if ok.sum() >= 20 else None


PREVIEW_FACES = 600_000  # models larger than this get a coarse copy for the 3D view


def _texture_order(names: list[str], mesh: Path) -> list[str]:
    """The texture pages of a model archive in the order of the faces' ``texnumber``: as the
    ``TextureFile`` comments of the mesh name them, else by the number in the name
    (``texture.tif``, ``texture1.tif``, ``texture2.tif``...)."""
    from ..io.mesh_ply import texture_files_of

    try:
        listed = texture_files_of(mesh)
    except OSError:
        listed = []
    by_name = {Path(n).name: n for n in names}
    if listed and all(Path(t).name in by_name for t in listed):
        return [by_name[Path(t).name] for t in listed]

    def number(n):
        m = re.search(r"(\d+)(?=\.[^.]+$)", Path(n).name)
        return int(m.group(1)) if m else -1

    return sorted(names, key=lambda n: (number(n), n))


def _has_texnumber(mesh: Path) -> bool:
    from ..io.mesh_ply import read_header

    try:
        with open(mesh, "rb") as f:
            _, elements = read_header(f)
    except (OSError, ValueError, KeyError):
        return False
    face = next((e for e in elements if e["name"] == "face"), None)
    return face is not None and any(n == "texnumber" for n, _ in face["props"])


def _import_texture(data: bytes, out: Path) -> Path | None:
    """The texture image of a Metashape model (TIFF, PNG or JPEG) as a JPEG next to the model."""
    import cv2

    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    cv2.imwrite(str(out), img, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return out


def _import_model(mc: MsChunk, ch, warnings: list, progress, dense_from_mesh: bool, frame=None,
                  max_cloud: int = 8_000_000):
    """``mesh.ply`` of the model archive -> ``model.ply`` (streamed; vertices converted in
    place by ``frame`` when given), a coarse copy for the 3D view, and the coloured vertices
    as dense cloud when the project has none."""
    from ..core.pointcloud import PointCloud
    from ..io.mesh_ply import PlyMesh, cluster_decimate, transform_vertices, write_mesh_ply

    mz = mc.frame_dir / mc.frame_paths["model"]
    if not mz.exists():
        warnings.append(f"{mc.label}: archivio del modello non trovato ({mz.name})")
        return False
    with zipfile.ZipFile(mz) as z:
        names = z.namelist()
        mesh_name = "mesh.ply" if "mesh.ply" in names else next((n for n in names if n.endswith(".ply")), None)
        if mesh_name is None:
            warnings.append(f"{mc.label}: il modello non contiene una mesh PLY leggibile")
            return False
        size = z.getinfo(mesh_name).file_size
        progress(0.0, f"{mc.label}: estrazione del modello ({size / 1e6:.0f} MB)")
        dst = ch.data_dir / "model.ply"
        with z.open(mesh_name) as src, open(dst, "wb") as out:
            shutil.copyfileobj(src, out, 16 << 20)
        textures = [n for n in names if Path(n).name.lower().startswith("texture")
                    and Path(n).suffix.lower() in (".tif", ".tiff", ".png", ".jpg", ".jpeg")]
        textures = _texture_order(textures, dst)
        paged = len(textures) > 1
        if paged and not _has_texnumber(dst):
            warnings.append(f"{mc.label}: texture del modello in {len(textures)} pagine senza la pagina di ogni "
                            "faccia: non importata (il modello ha i colori ai vertici)")
            textures = []
        pages = []
        for k, name in enumerate(textures):  # the texture of the model, page by page
            progress(0.15, f"{mc.label}: texture del modello" + (f", pagina {k + 1} di {len(textures)}" if paged else ""))
            page = _import_texture(z.read(name), ch.data_dir / ("model_texture.jpg" if k == 0
                                                                 else f"model_texture_{k}.jpg"))
            if page is None:
                warnings.append(f"{mc.label}: texture del modello non leggibile ({name})")
                pages = []
                break
            pages.append(page)
        if pages:
            ch._set_product("model_textured", pages[0])
            if paged:  # the pages named in the header, in the order of texnumber
                from ..io.mesh_ply import _header_comments, set_header_comments

                keep = [c for c in _header_comments(dst) if not c.lower().startswith("texturefile")]
                set_header_comments(dst, keep + [f"TextureFile {p.name}" for p in pages])
    ch._set_product("model", dst)
    try:
        if frame is not None:
            progress(0.25, f"{mc.label}: modello nel sistema di riferimento")
            transform_vertices(dst, frame)
        mesh = PlyMesh(dst)
    except ValueError as exc:  # unusual layout: the full model is still there, read by trimesh
        warnings.append(f"{mc.label}: anteprima del modello non creata ({exc})")
        return True
    if mesh.n_faces > PREVIEW_FACES:
        progress(0.4, f"{mc.label}: anteprima per la vista 3D ({mesh.n_faces / 1e6:.1f} M facce)")
        V, F, C = cluster_decimate(mesh.xyz(), mesh.face_chunks, mesh.colors(), PREVIEW_FACES)
        write_mesh_ply(ch.data_dir / "model_preview.ply", V, F, C)
    if dense_from_mesh and mesh.n_vertices:
        progress(0.8, f"{mc.label}: nuvola densa dai vertici del modello")
        idx = None
        if mesh.n_vertices > max_cloud:
            idx = np.sort(np.random.default_rng(0).choice(mesh.n_vertices, max_cloud, replace=False))
        ch.point_cloud = PointCloud(mesh.xyz(idx), mesh.colors(idx))
        ch.meta["point_cloud_source"] = "vertici del modello Metashape"
    return True


def _import_scalebars(mc: MsChunk, ch, by_ms_id: dict) -> list[str]:
    """The scale bars of a Metashape chunk between its markers: distance, accuracy (the bar's or
    the chunk's ``accuracy_scalebars``) and, for a bar unticked in Metashape's Reference pane, a
    check bar. Metashape already scaled the chunk with them: the transform is not fitted again,
    the Reference pane shows every bar with its error. Returns warnings."""
    warnings, skipped = [], {"cameras": 0, "distance": 0}
    default = mc.settings.get("accuracy_scalebars", 0.001)
    for sb in mc.scalebars:
        if sb.markers is None or any(i not in by_ms_id for i in sb.markers):
            skipped["cameras"] += 1
            continue
        if not sb.distance or sb.distance <= 0:
            skipped["distance"] += 1
            continue
        a, b = (by_ms_id[i] for i in sb.markers)
        ch.add_scalebar(a, b, sb.distance, sb.accuracy or default, checkpoint=not sb.enabled, label=sb.label or None)
    if skipped["cameras"]:
        warnings.append(f"{mc.label}: {skipped['cameras']} barre di scala tra camere non importate (servono due marker)")
    if skipped["distance"]:
        warnings.append(f"{mc.label}: {skipped['distance']} barre di scala senza distanza non importate")
    return warnings


def reimport_model(ch, psx: str | os.PathLike | None = None, progress=None) -> dict:
    """The model of the Metashape project again - with its vertex colours, its confidence and
    its texture - into a chunk imported earlier (projects imported before the texture was read,
    or whose model lost its colours). The model in use is kept among the versions. Returns
    ``{"warnings", "faces", "texture"}``."""
    from ..core import versions
    from ..core.progress import as_progress

    progress = as_progress(progress)
    info = ch.meta.get("import") or {}
    psx = Path(psx or info.get("psx") or "")
    if not psx.name or not psx.exists():
        raise FileNotFoundError(f"Metashape project not found: {psx}")
    progress(0.02, "Lettura del progetto Metashape")
    chunks = [mc for mc in read_psx(psx) if "model" in mc.frame_paths and mc.frame_dir is not None]
    mc = next((m for m in chunks if m.label == ch.label), None) or (chunks[0] if len(chunks) == 1 else None)
    if mc is None:
        raise RuntimeError(f"no model in the chunk {ch.label!r} of the Metashape project")
    frame = None
    if info.get("frame") == "exact":  # the same point-by-point conversion as the import
        if not np.allclose(ch.transform[:3, :3], np.eye(3), atol=1e-9):
            raise RuntimeError("the reference of the chunk changed after the import: import the project again")
        offset = ch.transform[:3, 3].copy()
        mapper = crs_mapper(mc, ch.crs, mc.component_transform(None))

        def frame(pts):
            return mapper(np.asarray(pts, float)) - offset

    versions.keep_active(ch, "model", move=True)
    warnings: list = []
    if not _import_model(mc, ch, warnings, progress.sub(0.05, 0.95), dense_from_mesh=False, frame=frame):
        raise RuntimeError("; ".join(warnings) or "model not imported")
    versions.set_active_name(ch, "model", versions.label("Modello di Metashape"))
    from ..io.mesh_ply import face_count

    progress(1.0, "Modello di Metashape reimportato")
    return {"warnings": warnings, "faces": face_count(ch.product_path("model")),
            "texture": ch.product_path("model_textured")}


def import_psx(psx: str | os.PathLike, project: str | os.PathLike, chunk_index: int | None = None,
               dem: str | os.PathLike | None = None, orthomosaic: str | os.PathLike | None = None,
               with_model: bool = True, progress=None):
    """Build a ``.ppg`` project from a Metashape project: photos, calibrated sensors, aligned
    cameras, markers with references and projections, tie points, the model, and exported
    DEM/orthomosaic. Returns the Document."""
    from ..core.camera import Camera, Sensor
    from ..core.document import Document
    from ..core.progress import as_progress

    progress = as_progress(progress)
    psx = Path(psx)
    progress(0.02, "Lettura del progetto Metashape")
    chunks = read_psx(psx)
    if chunk_index is not None:
        chunks = [chunks[chunk_index]]
    doc = Document()
    doc.save(project)
    empty_exports: list[Path] = []
    exported = _find_exported(psx, empty=empty_exports)
    if dem:
        exported["dem"] = Path(dem)
    if orthomosaic:
        exported["orthomosaic"] = Path(orthomosaic)
    raster_crs = None
    for k in ("dem", "orthomosaic"):
        if k in exported:
            import rasterio

            with rasterio.open(exported[k]) as src:
                if src.crs is not None and src.crs.to_epsg():
                    raster_crs = f"EPSG:{src.crs.to_epsg()}"
                    break
    warnings = []
    empty = [mc for mc in chunks if not mc.cameras and "model" not in mc.frame_paths
             and "point_cloud" not in mc.frame_paths]
    for mc in empty:
        warnings.append(f"{mc.label}: chunk vuoto, non importato")
    chunks = [mc for mc in chunks if mc not in empty] or chunks[:1]
    best, best_n = None, -1
    for ci, mc in enumerate(chunks):
        sub = progress.sub(0.05 + 0.9 * ci / len(chunks), 0.9 / len(chunks))
        ch = doc.add_chunk(mc.label)
        sub(0.05, f"{mc.label}: sensori e camere")
        for s in mc.sensors.values():
            model, params, w = sensor_params(s)
            warnings += w
            ch.sensors[s.id] = Sensor(key=s.id, label=s.label, width=s.width, height=s.height, model=model,
                                      params=params)
        base = mc.frame_dir or psx.parent
        for c in mc.cameras:
            if c.path is None:
                continue
            photo = (base / c.path).resolve() if not os.path.isabs(c.path) else Path(c.path)
            cam = Camera(key=c.id, label=c.label, path=str(photo), sensor_key=c.sensor_id, enabled=c.enabled)
            cam.transform = c.transform.copy() if c.transform is not None else None
            ch.cameras.append(cam)
        missing = sum(1 for c in ch.cameras if not Path(c.path).exists())
        if missing:
            warnings.append(f"{mc.label}: {missing} foto non trovate (servono per nuvola densa e texture)")
        aligned = [c for c in ch.cameras if c.transform is not None]
        comp_of = {c.id: c.component_id for c in mc.cameras}
        sub(0.2, f"{mc.label}: punti di legame")
        tie_parts, dense_parts, lost = _read_clouds(mc)
        if lost:
            warnings.append(f"{mc.label}: {lost} punti di legame di componenti non piu' presenti nel progetto ignorati")

        def comp_key(cid):
            return cid if cid in mc.components else None

        crs = _target_crs(mc, raster_crs)
        frame = None  # Metashape chunk frame -> internal frame of the new chunk, point by point (for the model)
        if crs != "LOCAL" and mc.crs_wkt and aligned:
            # exact conversion, point by point: no global similarity to approximate the projection
            mappers: dict = {}

            def world(cid, pts):
                key = comp_key(cid)
                if key not in mappers:
                    mappers[key] = crs_mapper(mc, crs, mc.component_transform(key))
                return mappers[key](np.asarray(pts, float))

            by_comp: dict = {}
            for cam in aligned:
                by_comp.setdefault(comp_of.get(cam.key), []).append(cam)
            centres = np.vstack([world(cid, np.array([c.transform[:3, 3] for c in cams])) for cid, cams in by_comp.items()])
            offset = np.round(centres.mean(axis=0))
            for cid, cams in by_comp.items():
                T = mc.component_transform(comp_key(cid))
                unit = 1.0 / max(abs(float(np.linalg.det(T[:3, :3]))) ** (1.0 / 3.0), 1e-12)  # one metre
                moved = reframe_cameras([c.transform for c in cams], lambda pts, _c=cid: world(_c, pts) - offset, unit)
                for cam, t in zip(cams, moved):
                    cam.transform = t

            def convert(cid, pts):
                return world(cid, pts) - offset

            def frame(pts):
                return world(None, pts) - offset

            m = np.eye(4)
            m[:3, 3] = offset
            ch.transform = m
            tie = _merge_parts(tie_parts, convert)
        else:
            from ..core.geometry import apply

            inv = np.linalg.inv(mc.transform)

            def relative(cid):  # component frame -> chunk frame (None: the same frame)
                rel = inv @ mc.component_transform(comp_key(cid))
                # absolute tolerance: geocentric translations are millions of metres
                return None if np.allclose(rel, np.eye(4), rtol=0.0, atol=1e-9) else rel

            def convert(cid, pts):  # other components into the chunk frame
                rel = relative(cid)
                return np.asarray(pts, float) if rel is None else apply(rel, pts)

            for cam in aligned:
                rel = relative(comp_of.get(cam.key))
                if rel is not None:
                    t = rel @ cam.transform
                    t[:3, :3] /= abs(float(np.linalg.det(rel[:3, :3]))) ** (1.0 / 3.0)
                    cam.transform = t
            tie = _merge_parts(tie_parts, convert)
            samples = np.array([c.transform[:3, 3] for c in aligned]) if aligned else np.zeros((0, 3))
            if tie is not None and len(tie):
                pick = tie.xyz[np.random.default_rng(0).choice(len(tie), min(len(tie), 2000), replace=False)]
                samples = np.vstack([samples, pick]) if len(samples) else pick
            if len(samples) >= 3:
                ch.transform, residual = internal_to_crs(mc, crs, samples)
                if residual > 0.01:
                    warnings.append(f"{mc.label}: approssimazione della proiezione {residual * 100:.1f} cm")
        dense = _merge_parts(dense_parts, convert, 8_000_000)
        ch.crs = crs
        if tie is not None:
            ch.tie_points = tie
        by_ms_id = {}
        for m in mc.markers:
            mk = ch.add_marker(m.label)
            by_ms_id[m.id] = mk
            if m.reference is not None:
                ref = np.array([m.reference])
                if crs != "LOCAL" and mc.crs_wkt:
                    from pyproj import CRS, Transformer

                    t = Transformer.from_crs(CRS.from_wkt(mc.crs_wkt), CRS.from_user_input(crs), always_xy=True)
                    ref = np.array([t.transform(*m.reference)])
                mk.reference.location = [float(v) for v in ref[0]]
                mk.reference.enabled = m.enabled
                acc = m.accuracy or ([mc.settings["accuracy_markers"]] * 3 if "accuracy_markers" in mc.settings else None)
                if acc:
                    mk.reference.accuracy = [float(v) for v in acc]
            for cam_id, (x, y) in m.projections.items():
                try:
                    ch.add_marker_projection(mk, ch.camera(cam_id), x, y, pinned=True)
                except KeyError:
                    pass
        warnings += _import_scalebars(mc, ch, by_ms_id)
        if aligned:
            sub(0.3, f"{mc.label}: modello COLMAP per la nuvola densa")
            try:
                from ..engine.sfm import write_reconstruction_from_chunk

                write_reconstruction_from_chunk(ch)
            except Exception as exc:  # photos missing: products still usable
                warnings.append(f"{mc.label}: modello COLMAP non scritto ({exc})")
            try:
                ch.triangulate_markers()
            except Exception:
                pass
        if dense is not None and len(dense):
            sub(0.45, f"{mc.label}: nuvola densa ({len(dense)} punti)")
            ch.point_cloud = dense
            ch.meta["point_cloud_source"] = "nuvola densa Metashape"
        if with_model and "model" in mc.frame_paths and mc.frame_dir is not None:
            _import_model(mc, ch, warnings, progress.sub(0.05 + 0.9 * (ci + 0.5) / len(chunks), 0.45 / len(chunks)),
                          dense_from_mesh=dense is None, frame=frame)
        centres = None  # camera centres in the CRS: exported rasters must cover them
        if aligned:
            from ..core.geometry import apply

            centres = apply(ch.transform, np.array([c.transform[:3, 3] for c in aligned]))[:, :2]
        dem_source = None
        for key in ("dem", "orthomosaic"):
            if key in exported:
                path = Path(exported[key]).resolve()
                if centres is not None and not _raster_covers(path, centres, ch.crs):
                    warnings.append(f"{mc.label}: {path.name} non copre l'area del progetto, non collegato")
                    continue
                ch._set_product(key, path)
                if key == "dem":
                    dem_source = f"esportato da Metashape ({path.name})"
        for p in empty_exports:
            warnings.append(f"{mc.label}: {p.name} e' vuoto (0 byte): esportazione da Metashape non riuscita?")
        if "dem" not in ch.products:
            info = internal_dem(mc)
            if info is not None:
                sub(0.9, f"{mc.label}: DEM interno di Metashape ({len(info['tiles'])} tasselli)")
                try:
                    out_dem = mosaic_dem(info, ch.data_dir / "dem.tif", ch.crs,
                                         progress=progress.sub(0.05 + 0.9 * (ci + 0.9) / len(chunks), 0.09 / len(chunks)))
                except Exception as exc:  # the rest of the import is still good
                    log.warning("DEM interno di Metashape non letto: %s", exc, exc_info=True)
                    warnings.append(f"{mc.label}: DEM interno di Metashape non letto ({exc})")
                else:
                    ch._set_product("dem", out_dem)
                    dem_source = "DEM interno del progetto Metashape"
                    pts = ch.tie_points
                    off = _dem_offset(out_dem, ch.to_world(pts.xyz[:: max(1, len(pts) // 5000)])) \
                        if pts is not None and len(pts) else None
                    if off is not None and abs(off) > 0.5:
                        warnings.append(f"{mc.label}: le quote del DEM di Metashape differiscono di {off:+.2f} m dai "
                                        "punti di legame (altro riferimento verticale?)")
        ch.meta["import"] = {"source": "metashape", "psx": str(psx), "warnings": list(dict.fromkeys(warnings)),
                             "frame": "exact" if frame is not None else "similarity"}
        if dem_source:
            ch.meta["import"]["dem"] = dem_source
        if len(aligned) > best_n:
            best, best_n = ch, len(aligned)
    if best is not None:
        doc.chunk = best
    doc.save()
    progress(1.0, f"Importati {len(chunks)} chunk da Metashape")
    return doc


__all__ = ["read_psx", "import_psx", "sensor_params", "crs_mapper", "reframe_cameras", "internal_dem", "mosaic_dem",
           "MsChunk", "MsScaleBar"]
