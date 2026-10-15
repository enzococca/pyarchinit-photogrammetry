"""Sensors (camera intrinsics) and cameras (photos with a pose)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

# COLMAP camera models supported by the engine and the number of parameters.
CAMERA_MODELS = {
    "SIMPLE_PINHOLE": 3,
    "PINHOLE": 4,
    "SIMPLE_RADIAL": 4,
    "RADIAL": 5,
    "OPENCV": 8,
    "FULL_OPENCV": 12,
}


@dataclass
class Reference:
    """Measured (a-priori) coordinates of a camera or a marker."""

    location: list[float] | None = None  # x, y, z in the chunk CRS
    accuracy: list[float] = field(default_factory=lambda: [0.005, 0.005, 0.005])
    enabled: bool = True

    def to_dict(self) -> dict:
        return {"location": self.location, "accuracy": self.accuracy, "enabled": self.enabled}

    @classmethod
    def from_dict(cls, d: dict | None) -> "Reference":
        if not d:
            return cls()
        return cls(location=d.get("location"), accuracy=d.get("accuracy", [0.005] * 3),
                   enabled=d.get("enabled", True))


@dataclass
class Sensor:
    """Camera intrinsics (one per physical camera/lens/resolution)."""

    key: int
    label: str
    width: int
    height: int
    model: str = "OPENCV"
    params: list[float] = field(default_factory=list)
    pixel_size_mm: float | None = None
    focal_length_mm: float | None = None
    fixed: bool = False

    def __post_init__(self):
        if self.model not in CAMERA_MODELS:
            raise ValueError(f"unsupported camera model {self.model!r}")
        if not self.params:
            f = 1.2 * max(self.width, self.height)
            cx, cy = self.width / 2.0, self.height / 2.0
            defaults = {
                "SIMPLE_PINHOLE": [f, cx, cy],
                "PINHOLE": [f, f, cx, cy],
                "SIMPLE_RADIAL": [f, cx, cy, 0.0],
                "RADIAL": [f, cx, cy, 0.0, 0.0],
                "OPENCV": [f, f, cx, cy, 0.0, 0.0, 0.0, 0.0],
                "FULL_OPENCV": [f, f, cx, cy] + [0.0] * 8,
            }
            self.params = defaults[self.model]

    # -- conversions --------------------------------------------------
    def to_pycolmap(self):
        import pycolmap

        return pycolmap.Camera(model=self.model, width=int(self.width), height=int(self.height),
                               params=list(map(float, self.params)))

    @property
    def K(self) -> np.ndarray:
        """3x3 calibration matrix (distortion ignored)."""
        p = self.params
        if self.model in ("SIMPLE_PINHOLE", "SIMPLE_RADIAL", "RADIAL"):
            fx = fy = p[0]
            cx, cy = p[1], p[2]
        else:
            fx, fy, cx, cy = p[0], p[1], p[2], p[3]
        return np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1.0]])

    @property
    def focal_px(self) -> float:
        k = self.K
        return float((k[0, 0] + k[1, 1]) / 2)

    def project(self, points_cam: np.ndarray) -> np.ndarray:
        """Project ``(N,3)`` points in the camera frame to pixels (with distortion)."""
        pts = np.atleast_2d(np.asarray(points_cam, dtype=float))
        return np.asarray(self.to_pycolmap().img_from_cam(pts))

    def unproject(self, pixels: np.ndarray) -> np.ndarray:
        """Pixels ``(N,2)`` -> normalised undistorted coordinates ``(N,2)``."""
        px = np.atleast_2d(np.asarray(pixels, dtype=float))
        out = np.asarray(self.to_pycolmap().cam_from_img(px))
        return out[:, :2] if out.shape[1] == 3 else out

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("key", "label", "width", "height", "model", "params",
                                              "pixel_size_mm", "focal_length_mm", "fixed")}

    @classmethod
    def from_dict(cls, d: dict) -> "Sensor":
        return cls(**d)


@dataclass
class Camera:
    """A photo in a chunk. ``transform`` is ``world_from_cam`` in chunk coordinates
    (OpenCV convention: +X right, +Y down, +Z forward). ``None`` = not aligned."""

    key: int
    label: str
    path: str
    sensor_key: int
    enabled: bool = True
    transform: np.ndarray | None = None
    reference: Reference = field(default_factory=Reference)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def aligned(self) -> bool:
        return self.transform is not None

    @property
    def center(self) -> np.ndarray | None:
        return None if self.transform is None else self.transform[:3, 3].copy()

    @property
    def cam_from_world(self) -> np.ndarray:
        if self.transform is None:
            raise RuntimeError(f"camera {self.label} is not aligned")
        return np.linalg.inv(self.transform)

    def project(self, sensor: Sensor, points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Project chunk-frame points. Returns ``(pixels, depth)``."""
        m = self.cam_from_world
        pc = np.atleast_2d(points) @ m[:3, :3].T + m[:3, 3]
        depth = pc[:, 2]
        px = np.full((len(pc), 2), np.nan)
        ok = depth > 1e-9
        if ok.any():
            px[ok] = sensor.project(pc[ok])
        return px, depth

    def image_path(self, base: Path | None = None) -> Path:
        p = Path(self.path)
        return p if p.is_absolute() or base is None else (base / p)

    def to_dict(self) -> dict:
        return {
            "key": self.key, "label": self.label, "path": self.path, "sensor_key": self.sensor_key,
            "enabled": self.enabled,
            "transform": None if self.transform is None else np.asarray(self.transform).tolist(),
            "reference": self.reference.to_dict(), "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Camera":
        t = d.get("transform")
        return cls(key=d["key"], label=d["label"], path=d["path"], sensor_key=d["sensor_key"],
                   enabled=d.get("enabled", True), transform=None if t is None else np.asarray(t, float),
                   reference=Reference.from_dict(d.get("reference")), meta=d.get("meta", {}))
