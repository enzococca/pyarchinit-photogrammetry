"""Import WebODM / ODM results (the task folder or the ``all.zip`` download).

ODM writes, inside the task folder:

* ``odm_orthophoto/odm_orthophoto.tif``          orthomosaic (GeoTIFF)
* ``odm_dem/dsm.tif`` and/or ``odm_dem/dtm.tif``   elevation models (GeoTIFF)
* ``odm_georeferencing/odm_georeferenced_model.laz`` (or ``.las``/``.ply``)  point cloud, world coordinates
* ``odm_georeferencing/coords.txt``              CRS (``WGS84 UTM 33N`` or a proj string) and the offset
* ``odm_texturing/odm_textured_model_geo.obj``    textured mesh, coordinates relative to that offset
* ``cameras.json`` and ``odm_report/shots.geojson`` camera calibrations and poses
* ``odm_georeferencing/ground_control_points.geojson`` (or ``gcp_list_utm.txt``) the GCPs ODM used
* ``images/`` (only in the task folder, not in ``all.zip``)

The chunk uses the orthophoto/DEM CRS; its internal frame is the world shifted by the ODM
offset (small numbers), so the mesh is used as is. The cameras come with their poses and the
GCPs become markers measured in the photos: they are triangulated at once, so the Reference
pane shows their errors and scale bars can be drawn between them (ODM has no scale bars: the
scale of a task comes from the GPS of the photos or from its GCPs).

Conventions (OpenSfM, the engine of ODM): ``cameras.json`` is normalised by the larger side of
the image (focal ``f * size`` pixels, principal point ``w / 2 + c_x * size``); a shot has the
world-to-camera rotation as an axis-angle vector and its ``translation`` is the camera centre
in the projected CRS, offset included; the axes of the camera are those of COLMAP. GCP
measurements are fractions of the image size of OpenSfM pixels (centre of the first pixel at
0, 0.5 in COLMAP), and ODM's ``error`` is the triangulated position minus the surveyed one.
"""
from __future__ import annotations

import json
import os
import re
import zipfile
from pathlib import Path

import numpy as np


def _crs_from_coords(line: str) -> str | None:
    m = re.match(r"WGS84 UTM (\d+)([NS])", line.strip(), re.I)
    if m:
        return f"EPSG:{(32600 if m.group(2).upper() == 'N' else 32700) + int(m.group(1))}"
    if line.strip().startswith("+proj") or line.strip().upper().startswith("EPSG:"):
        from pyproj import CRS

        epsg = CRS.from_user_input(line.strip()).to_epsg()
        return f"EPSG:{epsg}" if epsg else None
    return None


def read_task(folder: str | os.PathLike) -> dict:
    """Paths, CRS and offset of an ODM task folder."""
    folder = Path(folder)

    def first(*rel):
        for r in rel:
            p = folder / r
            if p.exists():
                return p
        return None

    info = {
        "folder": folder,
        "orthomosaic": first("odm_orthophoto/odm_orthophoto.tif"),
        "dem": first("odm_dem/dsm.tif", "odm_dem/dtm.tif"),
        "dtm": first("odm_dem/dtm.tif"),
        "cloud": first("odm_georeferencing/odm_georeferenced_model.laz", "odm_georeferencing/odm_georeferenced_model.las",
                       "odm_georeferencing/odm_georeferenced_model.ply", "odm_filterpoints/point_cloud.ply"),
        "model": first("odm_texturing/odm_textured_model_geo.obj", "odm_texturing_25d/odm_textured_model_geo.obj",
                       "odm_meshing/odm_mesh.ply"),
        "images": first("images"),
        "crs": None, "offset": np.zeros(3),
    }
    coords = first("odm_georeferencing/coords.txt")
    if coords:
        lines = [ln.strip() for ln in coords.read_text().splitlines() if ln.strip()]
        if lines:
            info["crs"] = _crs_from_coords(lines[0])
        if len(lines) > 1:
            e, n = (float(v) for v in lines[1].split()[:2])
            info["offset"] = np.array([e, n, 0.0])
    for key in ("orthomosaic", "dem"):  # the CRS the rasters are written in wins over coords.txt
        if info[key] is not None:
            import rasterio

            with rasterio.open(info[key]) as src:
                if src.crs is not None and src.crs.to_epsg():
                    info["crs"] = f"EPSG:{src.crs.to_epsg()}"
                    break
    if not any(info[k] for k in ("orthomosaic", "dem", "cloud", "model")):
        raise FileNotFoundError(f"{folder} does not look like an ODM/WebODM task (no odm_* products)")
    return info


def sensor_from_opensfm(cam: dict) -> tuple[str, list[float]]:
    """COLMAP model and parameters of an OpenSfM camera of ``cameras.json``; ValueError for the
    models the engine does not use (fisheye, spherical)."""
    w, h = int(cam["width"]), int(cam["height"])
    size = float(max(w, h))

    def p(key: str) -> float:
        return float(cam.get(key) or 0.0)

    kind = str(cam.get("projection_type", "perspective")).lower()
    fx = (p("focal_x") or p("focal")) * size
    fy = (p("focal_y") or p("focal")) * size
    cx, cy = w / 2.0 + p("c_x") * size, h / 2.0 + p("c_y") * size
    if kind == "perspective":  # one focal length, centred, radial k1 k2
        return "RADIAL", [fx, cx, cy, p("k1"), p("k2")]
    if kind == "brown":  # OpenCV's model
        if p("k3"):
            return "FULL_OPENCV", [fx, fy, cx, cy, p("k1"), p("k2"), p("p1"), p("p2"), p("k3"), 0.0, 0.0, 0.0]
        return "OPENCV", [fx, fy, cx, cy, p("k1"), p("k2"), p("p1"), p("p2")]
    if kind == "radial":
        return "OPENCV", [fx, fy, cx, cy, p("k1"), p("k2"), 0.0, 0.0]
    if kind == "simple_radial":
        return "OPENCV", [fx, fy, cx, cy, p("k1"), 0.0, 0.0, 0.0]
    raise ValueError(f"OpenSfM camera model {kind!r} is not supported")


def _camera_id(text: str) -> str:
    return re.sub(r"^v\d+\s+", "", str(text).strip())  # shots.geojson may prefix a version: "v2 "


def read_cameras(folder: str | os.PathLike) -> dict | None:
    """Calibrations and poses of an ODM task: ``{"sensors": {id: (width, height, model, params)},
    "shots": [{"filename", "camera", "rotation" (3x3, world to camera), "centre" (projected CRS)}],
    "warnings": [...]}``; None without ``cameras.json`` and ``odm_report/shots.geojson``."""
    from scipy.spatial.transform import Rotation

    folder = Path(folder)
    cams_f, shots_f = folder / "cameras.json", folder / "odm_report" / "shots.geojson"
    if not (cams_f.exists() and shots_f.exists()):
        return None
    sensors, warnings, unsupported = {}, [], 0
    for cid, cam in json.loads(cams_f.read_text(encoding="utf-8")).items():
        try:
            model, params = sensor_from_opensfm(cam)
        except (ValueError, KeyError) as exc:
            warnings.append(f"camera {_camera_id(cid)}: {exc}")
            continue
        sensors[_camera_id(cid)] = (int(cam["width"]), int(cam["height"]), model, params)
    shots, seen = [], set()
    for feat in json.loads(shots_f.read_text(encoding="utf-8")).get("features", []):
        props = feat.get("properties", {})
        name = os.path.basename(str(props.get("filename") or ""))
        if not name or name in seen or "rotation" not in props or "translation" not in props:
            continue
        seen.add(name)
        cid = _camera_id(props.get("camera", ""))
        if cid not in sensors:
            if len(sensors) == 1 and not unsupported:
                cid = next(iter(sensors))
            else:
                unsupported += 1
                continue
        shots.append({"filename": name, "camera": cid,
                      "rotation": Rotation.from_rotvec(np.asarray(props["rotation"], float)).as_matrix(),
                      "centre": np.asarray(props["translation"], float)})
    if unsupported:
        warnings.append(f"{unsupported} foto con un modello di camera non supportato non importate")
    return {"sensors": sensors, "shots": shots, "warnings": warnings}


def read_gcps(folder: str | os.PathLike) -> list[dict]:
    """The GCPs an ODM task used: ``[{"id", "lonlatalt" | ("xyz", "crs"), "observations":
    [(photo, x, y)] (COLMAP pixels), "error" (ODM: triangulated - surveyed, metres) | None}]``,
    from ``odm_georeferencing/ground_control_points.geojson`` or else ``gcp_list_utm.txt``.
    Pixels need the image size: fractions are kept as ``("fraction", u, v)`` until then."""
    folder = Path(folder)
    gj = folder / "odm_georeferencing" / "ground_control_points.geojson"
    if gj.exists():
        out = []
        for feat in json.loads(gj.read_text(encoding="utf-8")).get("features", []):
            props = feat.get("properties", {})
            coords = (feat.get("geometry") or {}).get("coordinates") or []
            if len(coords) < 2:
                continue
            obs = [(str(o["shot_id"]), "fraction", float(o["annotated"][0]), float(o["annotated"][1]))
                   for o in props.get("observations", []) if o.get("annotated")]
            err = props.get("error")
            out.append({"id": str(props.get("id")), "lonlatalt": [float(v) for v in coords] + [0.0] * (3 - len(coords)),
                        "observations": obs, "error": [float(v) for v in err] if err else None})
        return out
    txt = folder / "odm_georeferencing" / "gcp_list_utm.txt"
    if txt.exists():
        from .photo3d import parse_gcp_list

        parsed = parse_gcp_list(txt.read_text(encoding="utf-8"))
        by_name: dict[str, dict] = {}
        for name, e, n, z, px, py, photo in parsed["points"]:
            g = by_name.setdefault(name, {"id": name, "xyz": [e, n, z], "crs": parsed["crs"], "observations": [],
                                          "error": None})
            g["observations"].append((photo, "pixel", px + 0.5, py + 0.5))  # OpenSfM pixels -> COLMAP
        return list(by_name.values())
    return []


def _add_cameras(ch, poses: dict, images: Path, offset: np.ndarray) -> dict:
    """Sensors and aligned cameras from :func:`read_cameras`; the internal frame is the
    projected CRS minus ``offset``. Returns {photo name: camera}."""
    from ..core.camera import Camera, Sensor

    keys = {}
    for i, (cid, (w, h, model, params)) in enumerate(poses["sensors"].items()):
        ch.sensors[i] = Sensor(key=i, label=cid, width=w, height=h, model=model, params=list(params), fixed=True)
        keys[cid] = i
    by_name = {}
    for j, shot in enumerate(poses["shots"]):
        cam = Camera(key=j, label=Path(shot["filename"]).stem, path=str(images / shot["filename"]),
                     sensor_key=keys[shot["camera"]])
        t = np.eye(4)
        t[:3, :3] = shot["rotation"].T  # camera to world
        t[:3, 3] = shot["centre"] - offset
        cam.transform = t
        ch.cameras.append(cam)
        by_name[shot["filename"]] = cam
    return by_name


def _add_gcps(ch, gcps: list[dict], cameras: dict) -> dict:
    """Markers for the GCPs of :func:`read_gcps`, with their surveyed coordinates (in the chunk
    CRS) and their measurements in the photos; ODM's errors go into the import report."""
    from pyproj import Transformer

    to_chunk = None
    placed, errors = 0, {}
    for g in gcps:
        if "lonlatalt" in g:
            if to_chunk is None:
                to_chunk = Transformer.from_crs("EPSG:4326", ch.crs, always_xy=True)
            lon, lat, alt = g["lonlatalt"]
            xyz = [*to_chunk.transform(lon, lat), alt]
        else:
            xyz = list(g["xyz"])
            if g.get("crs") and g["crs"] != ch.crs:
                xyz = [*Transformer.from_crs(g["crs"], ch.crs, always_xy=True).transform(xyz[0], xyz[1]), xyz[2]]
        mk = ch.add_marker(g["id"], reference=xyz)
        for photo, kind, a, b in g["observations"]:
            cam = cameras.get(photo) or cameras.get(os.path.basename(photo))
            if cam is None:
                continue
            if kind == "fraction":  # fractions of the image of OpenSfM pixels -> COLMAP pixels
                sensor = ch.sensors[cam.sensor_key]
                a, b = a * sensor.width + 0.5, b * sensor.height + 0.5
            ch.add_marker_projection(mk, cam, a, b, pinned=True)
            placed += 1
        if g.get("error") is not None:
            errors[g["id"]] = g["error"]
    return {"markers": len(gcps), "projections": placed, "odm_errors": errors}


def _extract(zip_path: Path, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        for m in z.infolist():
            target = (dest / m.filename).resolve()
            if not str(target).startswith(str(dest.resolve())):  # zip slip
                raise ValueError(f"unsafe path in {zip_path}: {m.filename}")
        z.extractall(dest)
    return dest


def import_task(source: str | os.PathLike, project: str | os.PathLike, label: str | None = None, progress=None):
    """A ``.ppg`` project with the products of a WebODM/ODM task (folder or ``all.zip``)."""
    from ..core.document import Document
    from ..core.pointcloud import PointCloud
    from ..core.progress import as_progress

    progress = as_progress(progress)
    source = Path(source)
    project = Path(project)
    doc = Document()
    doc.save(project)
    if source.suffix.lower() == ".zip":
        progress(0.05, "Estrazione dell'archivio WebODM")
        source = _extract(source, project.parent / f"{project.stem}_odm")
    info = read_task(source)
    ch = doc.add_chunk(label or source.name)
    ch.crs = info["crs"] or "LOCAL"
    off = info["offset"]
    m = np.eye(4)
    m[:3, 3] = off
    ch.transform = m  # internal = world - offset
    report, warnings, cameras = {}, [], {}
    poses = read_cameras(source)
    if poses and poses["shots"] and info["crs"]:  # poses of a georeferenced task: in its projected CRS
        progress(0.1, f"Camere ({len(poses['shots'])})")
        cameras = _add_cameras(ch, poses, info["images"] or source / "images", off)
        warnings += poses["warnings"]
        report["cameras"] = len(cameras)
        missing = sum(1 for c in ch.cameras if not Path(c.path).exists())
        if missing:
            warnings.append(f"{missing} foto non presenti (servono per nuvola densa e texture; l'all.zip non le contiene)")
    elif info["images"] is not None:
        if poses and not info["crs"]:
            warnings.append("pose delle camere non importate: il task non e' georeferenziato")
        progress(0.1, "Foto")
        try:
            ch.add_photos([info["images"]])
        except Exception:
            pass
    gcps = read_gcps(source) if info["crs"] else []
    if gcps:
        report["gcp"] = _add_gcps(ch, gcps, cameras)
    if info["cloud"] is not None:
        progress(0.2, f"Nuvola di punti ({info['cloud'].name})")
        pc = PointCloud.load(info["cloud"])
        pc = PointCloud(np.asarray(pc.xyz, float) - off, pc.rgb, pc.normals, pc.classification)
        ch.point_cloud = pc
    if info["model"] is not None:
        progress(0.55, f"Modello ({info['model'].name})")
        import trimesh

        mesh = trimesh.load(str(info["model"]), force="mesh", process=False)
        if info["model"].suffix.lower() == ".ply" and np.abs(np.asarray(mesh.vertices)).max() > 1e5:
            mesh.vertices = np.asarray(mesh.vertices) - off  # odm_mesh.ply is in world coordinates
        dst = ch.data_dir / "model.ply"
        mesh.export(str(dst))
        ch._set_product("model", dst)
    for key in ("dem", "orthomosaic"):
        if info[key] is not None:
            ch._set_product(key, info[key].resolve())
    if cameras:
        ch.triangulate_markers()
        try:
            from ..engine.sfm import write_reconstruction_from_chunk

            write_reconstruction_from_chunk(ch, points=ch.point_cloud)
        except Exception as exc:  # photos missing: the poses and the markers are still there
            warnings.append(f"modello COLMAP non scritto ({exc})")
    ch.meta["import"] = {"source": "webodm", "folder": str(source), "offset": off.tolist(), **report,
                         "warnings": warnings}
    doc.save()
    progress(1.0, "Progetto WebODM importato")
    return doc


__all__ = ["read_task", "import_task", "read_cameras", "read_gcps", "sensor_from_opensfm"]
