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
as well; Metashape's own tiled DEM/orthomosaic storage is not read.
"""
from __future__ import annotations

import io
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


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


@dataclass
class MsMarker:
    id: int
    label: str
    reference: list[float] | None = None
    enabled: bool = True
    projections: dict[int, tuple[float, float]] = field(default_factory=dict)  # camera id -> pixel


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


def _floats(text: str | None) -> list[float]:
    return [float(v) for v in (text or "").split()]


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
        for c in doc.findall("cameras/camera"):
            t = c.find("transform")
            m = np.array(_floats(t.text)).reshape(4, 4) if t is not None else None
            cameras.append(MsCamera(int(c.get("id")), c.get("label", ""), int(c.get("sensor_id", 0)), m,
                                    c.get("enabled", "true") != "false"))
        markers = []
        for mk in doc.findall("markers/marker"):
            ref = mk.find("reference")
            loc = None
            if ref is not None and ref.get("x") is not None:
                loc = [float(ref.get("x")), float(ref.get("y")), float(ref.get("z", 0.0))]
            markers.append(MsMarker(int(mk.get("id")), mk.get("label", ""), loc,
                                    ref is None or ref.get("enabled", "true") != "false"))
        tr = np.eye(4)
        te = doc.find("transform")
        if te is not None:
            rot = _floats(te.findtext("rotation"))
            trans = _floats(te.findtext("translation"))
            scale = float(te.findtext("scale") or 1.0)
            if len(rot) == 9:
                tr[:3, :3] = np.array(rot).reshape(3, 3) * scale
            if len(trans) == 3:
                tr[:3, 3] = trans
        wkt = (doc.findtext("reference") or "").strip() or None
        ch = MsChunk(doc.get("label", f"Chunk {ce.get('id')}"), folder, sensors, cameras, markers, tr, wkt, None)
        frame = doc.find("frames/frame")
        if frame is not None:
            fpath = folder / frame.get("path")
            ch.frame_dir = fpath.parent
            fdoc = _xml_from_zip(fpath)
            by_id = {c.id: c for c in cameras}
            for fc in fdoc.findall("cameras/camera"):
                ph = fc.find("photo")
                cam = by_id.get(int(fc.get("camera_id")))
                if cam is not None and ph is not None:
                    cam.path = ph.get("path")
            mk_by_id = {m.id: m for m in markers}
            for fm in fdoc.findall("markers/marker"):
                m = mk_by_id.get(int(fm.get("marker_id")))
                if m is None:
                    continue
                for loc in fm.findall("location"):
                    m.projections[int(loc.get("camera_id"))] = (float(loc.get("x")), float(loc.get("y")))
            for tag in ("point_cloud", "model", "elevation", "orthomosaic", "tie_points"):
                el = fdoc.find(tag)
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


def _read_tie_points(ch: MsChunk):
    """Tie points (internal frame) and their colours from the frame's point cloud archive."""
    from ..core.pointcloud import PointCloud

    rel = ch.frame_paths.get("point_cloud") or ch.frame_paths.get("tie_points")
    if not rel or ch.frame_dir is None:
        return None
    path = ch.frame_dir / rel
    if not path.exists():
        return None
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        pts_name = next((n for n in names if re.fullmatch(r"points\d*\.ply", n)), None)
        if pts_name is None:
            return None
        pts = _ply_vertices(z.read(pts_name))
        xyz = np.column_stack([pts["x"], pts["y"], pts["z"]]).astype(np.float64)
        rgb = None
        if "tracks.ply" in names and "id" in pts.dtype.names:
            tr = _ply_vertices(z.read("tracks.ply"))
            if {"red", "green", "blue"} <= set(tr.dtype.names):
                ids = np.clip(pts["id"].astype(np.int64), 0, len(tr) - 1)
                rgb = np.column_stack([tr["red"][ids], tr["green"][ids], tr["blue"][ids]]).astype(np.uint8)
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


def _find_exported(psx: Path) -> dict[str, Path]:
    """DEM and orthomosaic GeoTIFFs exported next to the project (common folder names)."""
    out = {}
    base = psx.parent
    tifs = [p for p in base.rglob("*.tif") if ".files" not in p.parts[-3:] and p.stat().st_size > 0][:200]
    for p in sorted(tifs, key=lambda q: -q.stat().st_size):
        low = str(p.relative_to(base)).lower()
        if "dem" not in out and re.search(r"(dem|dsm|dtm|elev)", low):
            out["dem"] = p
        elif "orthomosaic" not in out and re.search(r"(ortho|orto)", low):
            out["orthomosaic"] = p
    return out


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
    exported = _find_exported(psx)
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
    for ci, mc in enumerate(chunks):
        sub = progress.sub(0.05 + 0.9 * ci / len(chunks), 0.9 / len(chunks))
        ch = doc.add_chunk(mc.label)
        sub(0.05, f"{mc.label}: sensori e camere")
        for s in mc.sensors.values():
            model, params, w = sensor_params(s)
            warnings += w
            ch.sensors[s.id] = Sensor(key=s.id, label=s.label, width=s.width, height=s.height, model=model,
                                      params=params, fixed=True)
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
        sub(0.2, f"{mc.label}: punti di legame")
        tie = _read_tie_points(mc)
        samples = np.array([c.transform[:3, 3] for c in aligned]) if aligned else np.zeros((0, 3))
        if tie is not None and len(tie):
            pick = tie.xyz[np.random.default_rng(0).choice(len(tie), min(len(tie), 2000), replace=False)]
            samples = np.vstack([samples, pick]) if len(samples) else pick
        crs = _target_crs(mc, raster_crs)
        if len(samples) >= 3:
            ch.transform, residual = internal_to_crs(mc, crs, samples)
            if residual > 0.01:
                warnings.append(f"{mc.label}: approssimazione della proiezione {residual * 100:.1f} cm")
        ch.crs = crs
        if tie is not None:
            ch.tie_points = tie
        for m in mc.markers:
            mk = ch.add_marker(m.label)
            if m.reference is not None:
                ref = np.array([m.reference])
                if crs != "LOCAL" and mc.crs_wkt:
                    from pyproj import CRS, Transformer

                    t = Transformer.from_crs(CRS.from_wkt(mc.crs_wkt), CRS.from_user_input(crs), always_xy=True)
                    ref = np.array([t.transform(*m.reference)])
                mk.reference.location = [float(v) for v in ref[0]]
                mk.reference.enabled = m.enabled
            for cam_id, (x, y) in m.projections.items():
                try:
                    ch.add_marker_projection(mk, ch.camera(cam_id), x, y, pinned=True)
                except KeyError:
                    pass
        if aligned:
            sub(0.4, f"{mc.label}: modello COLMAP per la nuvola densa")
            try:
                from ..engine.sfm import write_reconstruction_from_chunk

                write_reconstruction_from_chunk(ch)
            except Exception as exc:  # photos missing: products still usable
                warnings.append(f"{mc.label}: modello COLMAP non scritto ({exc})")
            try:
                ch.triangulate_markers()
            except Exception:
                pass
        if with_model and "model" in mc.frame_paths and mc.frame_dir is not None:
            sub(0.6, f"{mc.label}: modello 3D")
            mz = mc.frame_dir / mc.frame_paths["model"]
            if mz.exists():
                with zipfile.ZipFile(mz) as z:
                    ply = next((n for n in z.namelist() if n.endswith(".ply")), None)
                    if ply:
                        dst = ch.data_dir / "model.ply"
                        dst.write_bytes(z.read(ply))
                        ch._set_product("model", dst)
        for key in ("dem", "orthomosaic"):
            if key in exported:
                ch._set_product(key, Path(exported[key]).resolve())
        ch.meta["import"] = {"source": "metashape", "psx": str(psx), "warnings": warnings}
    doc.save()
    progress(1.0, f"Importati {len(chunks)} chunk da Metashape")
    return doc


__all__ = ["read_psx", "import_psx", "sensor_params", "MsChunk"]
