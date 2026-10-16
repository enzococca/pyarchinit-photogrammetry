"""Chunk: a set of photos processed together, with all its products.

The chunk keeps every product in its *internal* frame (the frame of the
structure-from-motion solution). ``chunk.transform`` maps internal
coordinates to the chunk CRS (a similarity: scale, rotation, translation).
Rasters (DEM, orthomosaic) are written directly in the chunk CRS.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

import numpy as np

from . import crs as crs_mod
from . import geometry
from .camera import Camera, Reference, Sensor
from .marker import Marker, Projection, ScaleBar
from .pointcloud import PointCloud
from .progress import ProgressCallback, as_progress

if TYPE_CHECKING:  # pragma: no cover
    from .document import Document

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".dng"}

# Downscale factor per quality level (same scale of values a photogrammetrist expects).
QUALITY_DOWNSCALE = {"ultra": 1, "highest": 1, "high": 2, "medium": 4, "low": 8, "lowest": 16}


@dataclass
class Chunk:
    key: int
    label: str
    document: "Document | None" = None
    enabled: bool = True
    crs: str = crs_mod.LOCAL
    transform: np.ndarray = field(default_factory=geometry.identity)
    sensors: dict[int, Sensor] = field(default_factory=dict)
    cameras: list[Camera] = field(default_factory=list)
    markers: list[Marker] = field(default_factory=list)
    scalebars: list[ScaleBar] = field(default_factory=list)
    products: dict[str, str] = field(default_factory=dict)  # name -> file relative to data_dir
    meta: dict = field(default_factory=dict)
    _tie_points: PointCloud | None = field(default=None, repr=False)

    # ------------------------------------------------------------------ paths
    @property
    def data_dir(self) -> Path:
        if self.document is None:
            raise RuntimeError("chunk is not attached to a document")
        d = self.document.files_dir / f"chunk_{self.key}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def product_path(self, name: str) -> Path | None:
        rel = self.products.get(name)
        return None if rel is None else self.data_dir / rel

    def _set_product(self, name: str, path: Path) -> None:
        self.products[name] = os.path.relpath(path, self.data_dir)

    # ------------------------------------------------------------ properties
    @property
    def aligned_cameras(self) -> list[Camera]:
        return [c for c in self.cameras if c.enabled and c.aligned]

    @property
    def tie_points(self) -> PointCloud | None:
        if self._tie_points is None and self.product_path("tie_points"):
            self._tie_points = PointCloud.load(self.product_path("tie_points"))
        return self._tie_points

    @tie_points.setter
    def tie_points(self, value: PointCloud | None) -> None:
        self._tie_points = value
        if value is not None and self.document is not None:
            path = value.save_ply(self.data_dir / "tie_points.ply")
            self._set_product("tie_points", path)

    @property
    def point_cloud(self) -> PointCloud | None:
        p = self.product_path("point_cloud")
        return PointCloud.load(p) if p and p.exists() else None

    @point_cloud.setter
    def point_cloud(self, value: PointCloud) -> None:
        path = value.save_ply(self.data_dir / "point_cloud.ply")
        self._set_product("point_cloud", path)

    def point_classes(self) -> dict[int, int]:
        """Points per class of the dense cloud (empty without one); reads only the class column."""
        p = self.product_path("point_cloud")
        return PointCloud.read_class_counts(p) if p and p.exists() else {}

    @property
    def model(self):
        """The mesh as a ``trimesh.Trimesh`` in internal coordinates (or ``None``)."""
        p = self.product_path("model")
        if not p or not p.exists():
            return None
        import trimesh

        return trimesh.load(str(p), process=False, force="mesh")

    @property
    def dem_path(self) -> Path | None:
        return self.product_path("dem")

    @property
    def orthomosaic_path(self) -> Path | None:
        return self.product_path("orthomosaic")

    @property
    def scale(self) -> float:
        return geometry.decompose_similarity(self.transform)[0]

    def sensor(self, camera: Camera) -> Sensor:
        return self.sensors[camera.sensor_key]

    def camera(self, label_or_key) -> Camera:
        for c in self.cameras:
            if c.key == label_or_key or c.label == label_or_key:
                return c
        raise KeyError(label_or_key)

    def marker(self, label_or_key) -> Marker:
        for m in self.markers:
            if m.key == label_or_key or m.label == label_or_key or (m.code is not None and m.code == label_or_key):
                return m
        raise KeyError(label_or_key)

    def to_world(self, points: np.ndarray) -> np.ndarray:
        return geometry.apply(self.transform, points)

    def to_internal(self, points: np.ndarray) -> np.ndarray:
        return geometry.apply(np.linalg.inv(self.transform), points)

    # ----------------------------------------------------------------- photos
    def add_photos(self, paths: Iterable[str | os.PathLike], *, recursive: bool = False) -> list[Camera]:
        """Add photos (files or folders). Photos sharing camera body, lens and
        resolution share one :class:`Sensor`. EXIF GPS becomes the camera reference."""
        from ..io.exif import read_exif

        files: list[Path] = []
        for p in paths:
            p = Path(p)
            if p.is_dir():
                it = p.rglob("*") if recursive else p.iterdir()
                files += sorted(f for f in it if f.suffix.lower() in IMAGE_EXTENSIONS)
            else:
                files.append(p)
        added = []
        existing = {Path(c.path).resolve() for c in self.cameras}
        for f in files:
            f = f.resolve()
            if f in existing:
                continue
            info = read_exif(f)
            sensor = self._sensor_for(info)
            cam = Camera(key=self._next_key(self.cameras), label=f.stem, path=str(f), sensor_key=sensor.key,
                         meta={k: v for k, v in info.items() if k != "gps"})
            if info.get("gps"):
                cam.reference = Reference(location=list(info["gps"]), accuracy=[5.0, 5.0, 10.0], enabled=True)
                cam.meta["reference_crs"] = "EPSG:4326"
            self.cameras.append(cam)
            added.append(cam)
        return added

    def _sensor_for(self, info: dict) -> Sensor:
        sig = (info.get("make"), info.get("model"), info["width"], info["height"], info.get("focal_length_mm"))
        for s in self.sensors.values():
            if (s.label, s.width, s.height, s.focal_length_mm) == (f"{sig[0] or ''} {sig[1] or ''}".strip() or "camera", sig[2], sig[3], sig[4]):
                return s
        w, h = info["width"], info["height"]
        f35 = info.get("focal_length_35mm")
        f_px = (f35 / 36.0) * max(w, h) if f35 else 1.2 * max(w, h)
        sensor = Sensor(key=self._next_key(self.sensors.values()),
                        label=f"{sig[0] or ''} {sig[1] or ''}".strip() or "camera",
                        width=w, height=h, model="OPENCV",
                        params=[f_px, f_px, w / 2.0, h / 2.0, 0.0, 0.0, 0.0, 0.0],
                        focal_length_mm=info.get("focal_length_mm"))
        self.sensors[sensor.key] = sensor
        return sensor

    @staticmethod
    def _next_key(items) -> int:
        keys = [i.key for i in items]
        return max(keys, default=-1) + 1

    def remove_cameras(self, cameras: Iterable[Camera]) -> None:
        drop = {c.key for c in cameras}
        self.cameras = [c for c in self.cameras if c.key not in drop]
        for m in self.markers:
            for k in drop:
                m.projections.pop(k, None)

    # ------------------------------------------------------------- alignment
    def match_photos(self, quality: str = "high", keypoint_limit: int = 40000, tiepoint_limit: int = 8000,
                     pair_selection: str = "auto", progress: ProgressCallback | None = None) -> None:
        """Detect and match features (SIFT) between photos."""
        from ..engine import sfm

        sfm.match_photos(self, quality=quality, keypoint_limit=keypoint_limit, tiepoint_limit=tiepoint_limit,
                         pair_selection=pair_selection, progress=as_progress(progress))

    def align_cameras(self, progress: ProgressCallback | None = None) -> int:
        """Estimate camera poses and sparse tie points. Returns the number of aligned photos.

        A new alignment lives in a new arbitrary frame: the chunk transform is reset and,
        when references exist (>= 3 GCPs or GNSS camera positions, or scale bars), the
        chunk is georeferenced/scaled again automatically.
        """
        from ..engine import sfm

        n = sfm.align_cameras(self, progress=as_progress(progress))
        self.transform = geometry.identity()
        self.meta.pop("levelled", None)
        if any(m.projections for m in self.markers):
            self.triangulate_markers()
        has_refs = (sum(1 for m in self.markers if m.reference.location and m.position is not None) >= 3
                    or sum(1 for c in self.aligned_cameras if c.reference.location) >= 3 or self.scalebars)
        if has_refs:
            self.update_transform()
        return n

    def optimize_cameras(self, fit_principal_point: bool = False, use_markers: bool = True, marker_weight: int = 4,
                         progress: ProgressCallback | None = None) -> float:
        """Global bundle adjustment; with 3 or more control points they are part of it (the
        block bends to fit them; see :func:`engine.sfm.optimize_cameras`). Returns the mean
        reprojection error in pixels; the control/check point report is in
        ``meta["sfm"]["optimize"]``."""
        from ..engine import sfm

        err = sfm.optimize_cameras(self, fit_principal_point=fit_principal_point, use_markers=use_markers,
                                   marker_weight=marker_weight, progress=as_progress(progress))
        if any(m.projections for m in self.markers):
            self.triangulate_markers()
        return err

    def reset_alignment(self) -> None:
        self.transform = geometry.identity()
        self.meta.pop("levelled", None)
        for c in self.cameras:
            c.transform = None
        for m in self.markers:
            m.position = None
        self._tie_points = None
        for k in ("tie_points", "reconstruction"):
            self.products.pop(k, None)

    # ---------------------------------------------------------------- markers
    def detect_markers(self, target_type: str = "aruco_4x4_50", cameras: Iterable[Camera] | None = None,
                       progress: ProgressCallback | None = None) -> list[Marker]:
        """Detect coded targets in the photos and create/update markers."""
        from ..markers import targets

        return targets.detect_in_chunk(self, target_type=target_type, cameras=cameras,
                                       progress=as_progress(progress))

    def add_marker(self, label: str, position: np.ndarray | None = None, reference: Iterable[float] | None = None) -> Marker:
        m = Marker(key=self._next_key(self.markers), label=label,
                   position=None if position is None else np.asarray(position, float))
        if reference is not None:
            m.reference = Reference(location=list(map(float, reference)))
        self.markers.append(m)
        return m

    def add_marker_projection(self, marker: Marker, camera: Camera, x: float, y: float, pinned: bool = True) -> None:
        marker.projections[camera.key] = Projection(float(x), float(y), pinned)

    def triangulate_markers(self, min_views: int = 2) -> None:
        """Compute marker positions (internal frame) from their projections."""
        from ..markers import gcp

        gcp.triangulate_markers(self, min_views=min_views)

    def add_scalebar(self, marker_a: Marker, marker_b: Marker, distance: float, accuracy: float = 0.001) -> ScaleBar:
        sb = ScaleBar(key=self._next_key(self.scalebars), label=f"{marker_a.label}_{marker_b.label}",
                      marker_a=marker_a.key, marker_b=marker_b.key, distance=float(distance), accuracy=accuracy)
        self.scalebars.append(sb)
        return sb

    # -------------------------------------------------------------- reference
    def import_reference(self, path: str | os.PathLike, crs: str | None = None, delimiter: str | None = None,
                         columns: str = "nxyz", skip_rows: int = 0, create_markers: bool = True) -> int:
        """Import marker coordinates from a CSV/TXT file. ``columns`` uses
        ``n`` = label, ``x``/``y``/``z`` = coordinates, ``a`` = accuracy, ``-`` = skip."""
        from ..markers import gcp

        return gcp.import_reference(self, path, crs=crs, delimiter=delimiter, columns=columns,
                                    skip_rows=skip_rows, create_markers=create_markers)

    def export_reference(self, path: str | os.PathLike) -> Path:
        from ..markers import gcp

        return gcp.export_reference(self, path)

    def update_transform(self, use_cameras: bool | None = None) -> dict:
        """Georeference (or scale) the chunk from markers, scale bars and camera
        references. Returns an accuracy report (see :func:`markers.gcp.update_transform`)."""
        from ..markers import gcp

        return gcp.update_transform(self, use_cameras=use_cameras)

    def reference_errors(self) -> list[dict]:
        from ..markers import gcp

        return gcp.marker_errors(self)

    # ------------------------------------------------------------ dense & mesh
    def build_depth_maps(self, quality: str = "medium", filter_mode: str = "moderate", backend: str = "auto",
                         progress: ProgressCallback | None = None, skip_unreadable: bool = False) -> None:
        """Depth maps of the aligned photos; photos that cannot be read (e.g. kept online only
        by Dropbox) stop the step unless ``skip_unreadable``."""
        from ..engine import dense

        dense.build_depth_maps(self, quality=quality, filter_mode=filter_mode, backend=backend,
                               progress=as_progress(progress), skip_unreadable=skip_unreadable)

    def build_point_cloud(self, point_colors: bool = True, progress: ProgressCallback | None = None) -> PointCloud:
        from ..engine import dense

        return dense.build_point_cloud(self, point_colors=point_colors, progress=as_progress(progress))

    def crop_point_cloud(self, zmin: float | None = None, zmax: float | None = None, radius: float | None = None,
                         center=(0.0, 0.0), bounds=None) -> "PointCloud":
        """Keep only the dense points inside the given limits (world coordinates): a height
        range, a cylinder around ``center`` (the turntable axis), or ``bounds`` (xmin, ymin,
        xmax, ymax). Returns and stores the cropped cloud."""
        pc = self.point_cloud
        if pc is None:
            raise RuntimeError("build the dense cloud first")
        world = self.to_world(pc.xyz)
        keep = np.ones(len(pc), bool)
        if zmin is not None:
            keep &= world[:, 2] >= zmin
        if zmax is not None:
            keep &= world[:, 2] <= zmax
        if radius is not None:
            keep &= np.hypot(world[:, 0] - center[0], world[:, 1] - center[1]) <= radius
        if bounds is not None:
            x0, y0, x1, y1 = bounds
            keep &= (world[:, 0] >= x0) & (world[:, 0] <= x1) & (world[:, 1] >= y0) & (world[:, 1] <= y1)
        if keep.sum() < 100:
            raise RuntimeError(f"only {int(keep.sum())} points would remain inside the crop")
        self.point_cloud = pc.subset(keep)
        self.products.pop("model", None)
        self.products.pop("model_textured", None)
        return self.point_cloud

    def build_model(self, source: str = "point_cloud", surface: str = "arbitrary", face_count: int | str = "medium",
                    voxel_size: float | None = None, vertex_colors: bool = True, classes=None,
                    progress: ProgressCallback | None = None):
        """Build a mesh. ``surface='arbitrary'`` (Poisson, true 3D) or ``'height_field'`` (2.5D, for terrain).

        ``source='depth_maps'`` fuses the depth maps of :meth:`build_depth_maps` (TSDF, no
        dense cloud needed); ``voxel_size`` (chunk CRS units, e.g. metres) sets its detail,
        by default twice the size of a depth-map pixel on the surface. ``classes``: only the
        dense points of these classes (e.g. without vegetation and excavation tools)."""
        from ..engine import meshing

        return meshing.build_model(self, source=source, surface=surface, face_count=face_count,
                                   voxel_size=voxel_size, vertex_colors=vertex_colors, classes=classes,
                                   progress=as_progress(progress))

    def build_texture(self, texture_size: int = 4096, progress: ProgressCallback | None = None) -> Path:
        from ..engine import texturing

        return texturing.build_texture(self, texture_size=texture_size, progress=as_progress(progress))

    def build_dem(self, resolution: float | None = None, source: str = "point_cloud", interpolate: bool = True,
                  classes=None, progress: ProgressCallback | None = None) -> Path:
        """DEM (GeoTIFF). ``classes``: only the dense points of these classes, e.g. ``[2]`` for a
        terrain model (DTM) without vegetation, people or tools; noise never counts."""
        from ..engine import dem

        return dem.build_dem(self, resolution=resolution, source=source, interpolate=interpolate, classes=classes,
                             progress=as_progress(progress))

    def build_orthomosaic(self, resolution: float | None = None, surface: str = "dem", blending: str = "mosaic",
                          hide_classes=None, progress: ProgressCallback | None = None) -> Path:
        """Orthomosaic (GeoTIFF RGBA). ``hide_classes``: where the visible surface belongs to
        these classes of the dense cloud the orthomosaic is transparent."""
        from ..engine import ortho

        return ortho.build_orthomosaic(self, resolution=resolution, surface=surface, blending=blending,
                                       hide_classes=hide_classes, progress=as_progress(progress))

    # ---------------------------------------------------------------- export
    def export_point_cloud(self, path: str | os.PathLike, source: str = "point_cloud", classes=None) -> Path:
        """Write the cloud in world coordinates (LAS keeps the classes); ``classes``: only
        the points of these classes."""
        pc = self.point_cloud if source == "point_cloud" else self.tie_points
        if pc is None:
            raise RuntimeError(f"no {source} in chunk {self.label}")
        if classes is not None:
            keep = pc.class_mask(classes)
            if not keep.any():
                raise ValueError("no point of the chosen classes")
            pc = pc.subset(keep)
        return pc.transformed(self.transform).save(path, crs_wkt=crs_mod.wkt(self.crs))

    def export_model(self, path: str | os.PathLike) -> Path:
        from ..io import exports

        return exports.export_model(self, path)

    def export_cameras(self, path: str | os.PathLike, fmt: str = "colmap") -> Path:
        from ..io import exports

        return exports.export_cameras(self, path, fmt=fmt)

    def export_dem(self, path: str | os.PathLike) -> Path:
        from ..io import exports

        return exports.copy_raster(self.dem_path, path)

    def export_orthomosaic(self, path: str | os.PathLike) -> Path:
        from ..io import exports

        return exports.copy_raster(self.orthomosaic_path, path)

    def generate_report(self, path: str | os.PathLike) -> Path:
        from ..io import report

        return report.generate_report(self, path)

    # ----------------------------------------------------------- serialisation
    def _base_dir(self) -> Path | None:
        doc = self.document
        return None if doc is None or doc.path is None else doc.path.parent

    def to_dict(self) -> dict:
        base = self._base_dir()
        cams = []
        for c in self.cameras:
            d = c.to_dict()
            d["path"] = _portable(d["path"], base)
            cams.append(d)
        meta = dict(self.meta)
        if "sfm" in meta and "image_root" in meta["sfm"]:
            meta["sfm"] = {**meta["sfm"], "image_root": _portable(meta["sfm"]["image_root"], base)}
        return {
            "key": self.key, "label": self.label, "enabled": self.enabled, "crs": self.crs,
            "transform": np.asarray(self.transform).tolist(),
            "sensors": [s.to_dict() for s in self.sensors.values()],
            "cameras": cams,
            "markers": [m.to_dict() for m in self.markers],
            "scalebars": [s.to_dict() for s in self.scalebars],
            "products": self.products, "meta": meta,
        }

    @classmethod
    def from_dict(cls, d: dict, document: "Document | None" = None) -> "Chunk":
        base = None if document is None or document.path is None else document.path.parent
        d = dict(d)
        d["cameras"] = [{**c, "path": _resolve(c["path"], base)} for c in d.get("cameras", [])]
        meta = dict(d.get("meta", {}))
        if "sfm" in meta and "image_root" in meta["sfm"]:
            meta["sfm"] = {**meta["sfm"], "image_root": _resolve(meta["sfm"]["image_root"], base)}
        d["meta"] = meta
        return cls(
            key=d["key"], label=d["label"], document=document, enabled=d.get("enabled", True),
            crs=d.get("crs", crs_mod.LOCAL), transform=np.asarray(d.get("transform", np.eye(4).tolist()), float),
            sensors={s["key"]: Sensor.from_dict(s) for s in d.get("sensors", [])},
            cameras=[Camera.from_dict(c) for c in d.get("cameras", [])],
            markers=[Marker.from_dict(m) for m in d.get("markers", [])],
            scalebars=[ScaleBar.from_dict(s) for s in d.get("scalebars", [])],
            products=d.get("products", {}), meta=d.get("meta", {}),
        )


def _portable(path: str, base: Path | None) -> str:
    """Store paths inside the project folder as relative (``./...``) so projects can move."""
    if base is None:
        return path
    try:
        rel = Path(path).resolve().relative_to(base.resolve())
    except ValueError:
        return path
    return "./" + rel.as_posix()


def _resolve(path: str, base: Path | None) -> str:
    if base is not None and path.startswith("./"):
        return str((base / path[2:]).resolve())
    return path
