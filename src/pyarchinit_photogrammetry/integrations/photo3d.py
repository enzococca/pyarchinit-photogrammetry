"""Photo3D integration: process a Photo3D capture session with this engine.

A Photo3D session folder (``<root>/<stamp>_<name>/``) holds ``photos/``, ``masks/``,
``session.json``, and optionally ``gcp_list.txt`` (ODM format, raw pixels),
``target_list.json`` (coded targets with surveyed coordinates) and ``scale.json``
(two taps on one photo and their distance in mm). Photo3D runs each engine as a
subprocess with ``PHOTO3D_SESSION_DIR`` and ``PHOTO3D_PRESET`` and expects:

* ``PROGRESS <stage> <pct>`` lines on stdout (stages: matchPhotos, alignCameras,
  buildDepthMaps, buildPointCloud, buildModel, buildTexture, buildDem, buildOrthomosaic,
  exportModel, exportPointCloud);
* the products in ``<session>/metashape/`` (model.obj/.mtl/.jpg, pointcloud.ply, dem.tif,
  orthophoto.tif) and ``metashape/result.json`` = ``{"outputs": {...}, "stats": {...},
  "scale"?: {...}, "gcp"?: {...}, "targets"?: {...}}``; exit code 0.

:func:`run_session` implements that contract (``ppg photo3d-job`` on the command line);
:func:`import_session` turns a session into a ``.ppg`` project to work on in the interface.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..core import geometry

PHOTO_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".heic", ".dng"}
PRESETS = {  # photo3d preset -> engine settings
    "preview": {"align": "low", "dense": "low", "faces": "low", "texture": 2048},
    "medium": {"align": "high", "dense": "medium", "faces": "medium", "texture": 4096},
    "high": {"align": "high", "dense": "high", "faces": "high", "texture": 8192},
}


@dataclass
class Session:
    folder: Path
    photos: list[Path]
    info: dict = field(default_factory=dict)
    gcp: dict | None = None  # {"crs", "points": [(name, E, N, Z, px, py, photo)]}
    targets: dict | None = None  # {"crs", "targets": [{codice, est, nord, quota, nome}]}
    scale: dict | None = None

    @property
    def name(self) -> str:
        return str(self.info.get("name") or self.folder.name)


def read_session(folder: str | os.PathLike) -> Session:
    folder = Path(folder)
    photos = sorted(p for p in (folder / "photos").iterdir()
                    if p.suffix.lower() in PHOTO_EXT and not p.name.startswith("."))
    if not photos:
        raise FileNotFoundError(f"no photos in {folder / 'photos'}")
    s = Session(folder, photos)
    for name, attr in (("session.json", "info"), ("target_list.json", "targets"), ("scale.json", "scale")):
        f = folder / name
        if f.is_file():
            try:
                setattr(s, attr, json.loads(f.read_text(encoding="utf-8")))
            except ValueError:
                pass
    if s.scale is None and isinstance(s.info.get("scaleBar"), dict):
        s.scale = {"method": "tap", **s.info["scaleBar"]}
    g = folder / "gcp_list.txt"
    if g.is_file():
        s.gcp = parse_gcp_list(g.read_text(encoding="utf-8"))
    return s


def parse_gcp_list(text: str) -> dict:
    """ODM ``gcp_list.txt``: first line the CRS, then ``E N Z px py photo [name]``."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    if not lines:
        return {"crs": None, "points": []}
    crs = odm_crs(lines[0])
    pts = []
    for ln in lines[1:]:
        parts = ln.split()
        if len(parts) < 6:
            continue
        e, n, z, px, py = (float(v) for v in parts[:5])
        photo = parts[5]
        name = parts[6] if len(parts) > 6 else None
        pts.append((name, e, n, z, px, py, photo))
    # points without a name: the same coordinates are the same marker
    keys: dict[tuple, str] = {}
    out = []
    for name, e, n, z, px, py, photo in pts:
        if name is None:
            key = (round(e, 3), round(n, 3), round(z, 3))
            name = keys.setdefault(key, f"GCP {len(keys) + 1}")
        out.append((name, e, n, z, px, py, photo))
    return {"crs": crs, "points": out}


def odm_crs(line: str) -> str | None:
    line = line.strip()
    m = re.match(r"(?i)EPSG:(\d+)", line)
    if m:
        return f"EPSG:{m.group(1)}"
    m = re.search(r"\+proj=utm\s+\+zone=(\d+)(\s+\+south)?", line)
    if m and ("WGS84" in line.upper() or "+ellps=WGS84" in line):
        return f"EPSG:{(32700 if m.group(2) else 32600) + int(m.group(1))}"
    if "longlat" in line or "WGS84" == line.upper():
        return "EPSG:4326"
    return None


def _stem(name: str) -> str:
    return Path(name).stem


def prepare_chunk(session: Session, doc, progress=print) -> object:
    """Photos, coded targets, GCPs (as markers with their projections) in a new chunk."""
    import tempfile

    ch = doc.add_chunk(session.name)
    ch.add_photos(session.photos)
    by_stem = {_stem(c.label): c for c in ch.cameras}
    info = {}
    if session.targets and session.targets.get("targets"):
        markers = ch.detect_markers("aruco_4x4_50")
        crs = session.targets.get("crs") or "LOCAL"
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as f:
            for t in session.targets["targets"]:
                f.write(f"{int(t['codice'])},{float(t['est'])},{float(t['nord'])},{float(t['quota'])}\n")
        n = ch.import_reference(f.name, crs=crs, create_markers=False)
        os.unlink(f.name)
        info["targets"] = {"measured": len(session.targets["targets"]), "detected": len(markers), "matched": n,
                           "note": "riconosciuti solo target ArUco/AprilTag; i target circolari di Metashape no"}
    if session.gcp and session.gcp["points"]:
        crs = session.gcp["crs"] or "LOCAL"
        names: dict[str, tuple] = {}
        for name, e, n, z, *_ in session.gcp["points"]:
            names[name] = (e, n, z)
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8") as f:
            for name, (e, n, z) in names.items():
                f.write(f"{name},{e},{n},{z}\n")
        ch.import_reference(f.name, crs=crs)
        os.unlink(f.name)
        placed = 0
        for name, e, n, z, px, py, photo in session.gcp["points"]:
            cam = by_stem.get(_stem(photo))
            marker = ch.marker(name)
            if cam is not None:
                ch.add_marker_projection(marker, cam, px, py, pinned=True)
                placed += 1
        info["gcp"] = {"markers": len(names), "projections": placed, "crs": crs}
    return ch, info


def _ray(ch, cam, px: float, py: float) -> tuple[np.ndarray, np.ndarray]:
    sensor = ch.sensor(cam)
    xn, yn = sensor.unproject(np.array([[px, py]]))[0]
    d = cam.transform[:3, :3] @ np.array([xn, yn, 1.0])
    return cam.transform[:3, 3].copy(), d / np.linalg.norm(d)


_point_on_ray = geometry.point_on_ray  # where a ray meets the surface of a cloud


def apply_scale(ch, scale: dict, log=print) -> dict | None:
    """Scale a local reconstruction from two taps on one photo and their distance in mm."""
    if not scale or scale.get("method") not in ("tap", "ruler", None) or not scale.get("lengthMM"):
        return None
    cams = [c for c in ch.aligned_cameras if _stem(c.label) == _stem(str(scale.get("photo", "")))]
    pts = ch.point_cloud.xyz if ch.point_cloud is not None else (ch.tie_points.xyz if ch.tie_points is not None else None)
    if not cams or pts is None or len(pts) < 10:
        log("INFO scala: foto della stecca non allineata o nuvola assente, modello senza scala")
        return None
    cam = cams[0]
    sensor = ch.sensor(cam)

    def px(p):
        x, y = (p["x"], p["y"]) if isinstance(p, dict) else p
        return (x * sensor.width, y * sensor.height) if max(abs(x), abs(y)) <= 1.0 else (x, y)

    pts = np.asarray(pts, float)
    diag = float(np.linalg.norm(np.ptp(pts, axis=0)))
    ends = []
    for key in ("a", "b"):
        o, d = _ray(ch, cam, *px(scale[key]))
        p = None
        for frac in (0.01, 0.02, 0.04):  # narrowest cylinder with enough points for a plane
            p = _point_on_ray(pts, o, d, diag * frac)
            if p is not None:
                break
        if p is None:
            log(f"INFO scala: il punto {key} cade nel vuoto, modello senza scala")
            return None
        ends.append(p)
    dist = float(np.linalg.norm(ends[0] - ends[1]))
    factor = float(scale["lengthMM"]) / 1000.0 / dist
    ch.transform = np.diag([factor, factor, factor, 1.0])
    ch.crs = "LOCAL"
    log(f"INFO scala: {scale['lengthMM']} mm = {dist:.4f} unita' -> fattore {factor:.6f}")
    return {"factor": factor, "lengthMM": float(scale["lengthMM"]), "method": scale.get("method", "tap")}


def run_session(session_dir: str | os.PathLike, preset: str = "medium", out=None) -> dict:
    """Process a Photo3D session following its engine contract; returns ``result.json``."""
    from ..core.document import Document

    out = out or sys.stdout

    def progress(stage: str, pct: float) -> None:
        print(f"PROGRESS {stage} {pct:.1f}", file=out, flush=True)

    def info(msg: str) -> None:
        print(msg if msg.startswith("INFO ") else "INFO " + msg, file=out, flush=True)

    def sub(stage: str):
        return lambda f, m="": progress(stage, 100.0 * max(0.0, min(1.0, f)))

    t0 = time.time()
    cfg = PRESETS.get(preset, PRESETS["medium"])
    session = read_session(session_dir)
    ms = Path(session_dir) / "metashape"
    ms.mkdir(exist_ok=True)
    doc = Document()
    doc.save(ms / "ppg_project.ppg")
    ch, ref_info = prepare_chunk(session, doc, info)
    info(f"{len(ch.cameras)} foto, preset {preset}")
    ch.match_photos(cfg["align"], progress=sub("matchPhotos"))
    n = ch.align_cameras(progress=sub("alignCameras"))
    info(f"allineate {n}/{len(ch.cameras)} foto")
    georef = any(m.reference.location for m in ch.markers) and len([m for m in ch.markers if m.reference.location]) >= 3
    report = ch.update_transform() if georef else None
    ch.build_depth_maps(cfg["dense"], progress=sub("buildDepthMaps"))
    ch.build_point_cloud(progress=sub("buildPointCloud"))
    scale_info = None if georef else apply_scale(ch, session.scale or {}, info)
    ch.build_model(face_count=cfg["faces"], progress=sub("buildModel"))
    ch.build_texture(texture_size=cfg["texture"], progress=sub("buildTexture"))
    outputs = {"project": str(doc.path), "directory": str(ms)}
    outputs["model"] = str(ch.export_model(ms / "model.obj"))
    progress("exportModel", 100.0)
    outputs["pointCloud"] = str(ch.export_point_cloud(ms / "pointcloud.ply"))
    progress("exportPointCloud", 100.0)
    if georef or scale_info:
        ch.build_dem(progress=sub("buildDem"))
        outputs["dem"] = str(ch.export_dem(ms / "dem.tif"))
        ch.build_orthomosaic(progress=sub("buildOrthomosaic"))
        outputs["orthophoto"] = str(ch.export_orthomosaic(ms / "orthophoto.tif"))
    doc.save()
    stats = {"engine": "pyarchinit-photogrammetry", "photos": len(ch.cameras), "aligned": len(ch.aligned_cameras),
             "points": len(ch.point_cloud) if ch.point_cloud is not None else 0,
             "faces": int(len(ch.model.faces)) if ch.model is not None else 0, "seconds": round(time.time() - t0, 1)}
    result = {"outputs": outputs, "stats": stats}
    if scale_info:
        result["scale"] = scale_info
    if report:
        result["gcp"] = {"control_rmse_m": report.get("control_rmse"), "check_rmse_m": report.get("check_rmse"),
                         "crs": ch.crs, **ref_info.get("gcp", {})}
    if "targets" in ref_info:
        result["targets"] = ref_info["targets"]
    (ms / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("RESULT " + json.dumps(result), file=out, flush=True)
    return result


def import_session(session_dir: str | os.PathLike, project: str | os.PathLike | None = None):
    """A ``.ppg`` project with the session photos, targets and GCPs (no processing yet)."""
    from ..core.document import Document

    session = read_session(session_dir)
    doc = Document()
    project = Path(project) if project else Path(session_dir) / f"{session.name}.ppg"
    doc.save(project)
    prepare_chunk(session, doc, lambda m: None)
    doc.save()
    return doc


def main(argv: list[str] | None = None) -> int:
    """Entry point for Photo3D: ``PHOTO3D_SESSION_DIR`` and ``PHOTO3D_PRESET`` from the environment."""
    argv = list(sys.argv[1:] if argv is None else argv)
    session = argv[0] if argv else os.environ.get("PHOTO3D_SESSION_DIR")
    if not session:
        print("usage: ppg photo3d-job SESSION_DIR [PRESET]  (or PHOTO3D_SESSION_DIR)", file=sys.stderr)
        return 2
    preset = argv[1] if len(argv) > 1 else os.environ.get("PHOTO3D_PRESET", "medium")
    try:
        run_session(session, preset)
    except Exception as exc:  # the server shows the last lines of the log
        print(f"ERROR {type(exc).__name__}: {exc}", flush=True)
        import traceback

        traceback.print_exc()
        return 1
    return 0


__all__ = ["read_session", "parse_gcp_list", "run_session", "import_session", "apply_scale", "main", "Session"]
