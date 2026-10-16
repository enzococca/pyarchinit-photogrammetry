"""Point cloud container with PLY and LAS I/O.

Classification codes follow ASPRS LAS 1.4. Codes 64-255 are user defined:
the archaeological classes registered in :data:`ARCHAEO_CLASSES` live there.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

ASPRS_CLASSES = {
    0: "Created, never classified",
    1: "Unclassified",
    2: "Ground",
    3: "Low vegetation",
    4: "Medium vegetation",
    5: "High vegetation",
    6: "Building",
    7: "Low point (noise)",
    9: "Water",
    17: "Bridge deck",
    18: "High noise",
}

NOISE_CLASSES = (7, 18)  # low and high noise (ASPRS): never part of a surface

# User-defined classes for archaeological contexts (LAS codes 64-255).
ARCHAEO_CLASSES = {
    64: "Struttura muraria / Masonry structure",
    65: "Deposito stratigrafico / Stratigraphic deposit",
    66: "Taglio (US negativa) / Cut",
    67: "Pavimento / Floor",
    68: "Reperto / Find",
    69: "Sezione esposta / Exposed section",
    70: "Roccia naturale / Bedrock",
    71: "Attrezzatura di scavo / Excavation equipment",
}


@dataclass
class PointCloud:
    xyz: np.ndarray
    rgb: np.ndarray | None = None          # uint8 (N,3)
    normals: np.ndarray | None = None      # float (N,3)
    classification: np.ndarray | None = None  # uint8 (N,)
    confidence: np.ndarray | None = None   # uint8 (N,): depth maps that agree on the point (0 = unknown)
    labels: dict[int, str] = field(default_factory=dict)  # class code -> name (user classes)
    manual: np.ndarray | None = None       # bool (N,): class assigned by hand, kept by automatic classifiers

    def __post_init__(self):
        self.xyz = np.asarray(self.xyz, dtype=np.float64).reshape(-1, 3)
        n = len(self.xyz)
        if self.rgb is not None:
            self.rgb = np.asarray(self.rgb, dtype=np.uint8).reshape(n, 3)
        if self.normals is not None:
            self.normals = np.asarray(self.normals, dtype=np.float32).reshape(n, 3)
        if self.classification is None:
            self.classification = np.ones(n, dtype=np.uint8)
        else:
            self.classification = np.array(self.classification, dtype=np.uint8).reshape(n)
        if self.manual is not None:
            self.manual = np.array(self.manual, dtype=bool).reshape(n)
        if self.confidence is not None:
            self.confidence = np.clip(np.asarray(self.confidence), 0, 255).astype(np.uint8).reshape(n)

    def __len__(self) -> int:
        return len(self.xyz)

    def class_mask(self, classes=None, exclude=()) -> np.ndarray:
        """True for the points whose class is in ``classes`` (every class when None) and not
        in ``exclude``: the points a product built "from the chosen classes" uses."""
        keep = np.ones(len(self), bool) if classes is None else np.isin(self.classification, _codes(classes))
        if len(exclude):
            keep &= ~np.isin(self.classification, _codes(exclude))
        return keep

    @staticmethod
    def read_column(path: str | Path, name: str) -> np.ndarray | None:
        """One property of the points of a binary PLY (e.g. ``confidence``), read without loading
        the cloud; ``None`` when the file has no such property."""
        arr = _read_binary_ply_vertices(Path(path), mmap=True)
        if arr is None:
            return getattr(PointCloud.load(path), name, None)
        return np.array(arr[name]) if name in arr.dtype.names else None

    @staticmethod
    def read_class_counts(path: str | Path) -> dict[int, int]:
        """Points per class of a point cloud file, reading only its class column when it is a
        binary PLY (a large dense cloud is not loaded)."""
        arr = _read_binary_ply_vertices(Path(path), mmap=True)
        if arr is None:
            codes = PointCloud.load(path).classification
        elif "classification" not in arr.dtype.names:
            return {1: len(arr)}
        else:
            codes = arr["classification"]
        counts = np.bincount(np.asarray(codes, np.uint8), minlength=256)
        return {int(c): int(n) for c, n in enumerate(counts) if n}

    def subset(self, mask: np.ndarray) -> "PointCloud":
        pick = lambda a: None if a is None else a[mask]  # noqa: E731
        return PointCloud(self.xyz[mask], pick(self.rgb), pick(self.normals), pick(self.classification),
                          pick(self.confidence), dict(self.labels), pick(self.manual))

    def transformed(self, m: np.ndarray) -> "PointCloud":
        xyz = self.xyz @ m[:3, :3].T + m[:3, 3]
        normals = None
        if self.normals is not None:
            r = m[:3, :3] / np.cbrt(np.linalg.det(m[:3, :3]))
            normals = (self.normals @ r.T).astype(np.float32)
        manual = None if self.manual is None else self.manual.copy()
        confidence = None if self.confidence is None else self.confidence.copy()
        return PointCloud(xyz, self.rgb, normals, self.classification.copy(), confidence, dict(self.labels), manual)

    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        return self.xyz.min(0), self.xyz.max(0)

    # ---------------------------------------------------------------- PLY
    def save_ply(self, path: str | Path) -> Path:
        path = Path(path)
        n = len(self)
        fields = [("x", "f8"), ("y", "f8"), ("z", "f8")]
        if self.normals is not None:
            fields += [("nx", "f4"), ("ny", "f4"), ("nz", "f4")]
        if self.rgb is not None:
            fields += [("red", "u1"), ("green", "u1"), ("blue", "u1")]
        fields += [("classification", "u1")]
        if self.confidence is not None:
            fields += [("confidence", "u1")]  # as Metashape writes it: CloudCompare shows it as a scalar field
        if self.manual is not None:
            fields += [("manual", "u1")]  # extra property: readers that do not know it skip it
        arr = np.empty(n, dtype=fields)
        arr["x"], arr["y"], arr["z"] = self.xyz.T
        if self.normals is not None:
            arr["nx"], arr["ny"], arr["nz"] = self.normals.T
        if self.rgb is not None:
            arr["red"], arr["green"], arr["blue"] = self.rgb.T
        arr["classification"] = self.classification
        if self.confidence is not None:
            arr["confidence"] = self.confidence
        if self.manual is not None:
            arr["manual"] = self.manual
        type_names = {"f8": "double", "f4": "float", "u1": "uchar"}
        header = ["ply", "format binary_little_endian 1.0", f"element vertex {n}"]
        header += [f"property {type_names[t]} {name}" for name, t in fields]
        header += ["end_header"]
        with open(path, "wb") as fh:
            fh.write(("\n".join(header) + "\n").encode("ascii"))
            fh.write(arr.astype(arr.dtype.newbyteorder("<")).tobytes())
        return path

    @classmethod
    def load_ply(cls, path: str | Path) -> "PointCloud":
        props = _read_binary_ply_vertices(Path(path))
        if props is None:  # ASCII or exotic PLY: let trimesh handle it
            import trimesh

            data = trimesh.load(str(path), process=False)
            xyz = np.asarray(data.vertices)
            colors = getattr(data, "colors", None)
            rgb = np.asarray(colors)[:, :3] if colors is not None and len(colors) == len(xyz) else None
            return cls(xyz, rgb)
        names = set(props.dtype.names)
        xyz = np.column_stack([props["x"], props["y"], props["z"]])
        normals = np.column_stack([props["nx"], props["ny"], props["nz"]]) if {"nx", "ny", "nz"} <= names else None
        rgb = np.column_stack([props["red"], props["green"], props["blue"]]) if {"red", "green", "blue"} <= names else None
        cls_arr = props["classification"] if "classification" in names else None
        manual = props["manual"] != 0 if "manual" in names else None  # older files have no such property
        confidence = None
        if "confidence" in names:  # ours and Metashape's (uchar), or a float written by other programs
            confidence = np.clip(np.round(props["confidence"].astype(np.float64)), 0, 255).astype(np.uint8)
        return cls(xyz, rgb, normals, cls_arr, confidence=confidence, manual=manual)

    # ---------------------------------------------------------------- LAS
    def save_las(self, path: str | Path, crs_wkt: str | None = None) -> Path:
        import laspy

        path = Path(path)
        header = laspy.LasHeader(point_format=7, version="1.4")
        header.offsets = self.xyz.min(0)
        header.scales = np.array([0.0001, 0.0001, 0.0001])
        if crs_wkt:
            try:
                from pyproj import CRS

                header.add_crs(CRS.from_user_input(crs_wkt))
            except Exception:  # pragma: no cover - CRS is optional metadata
                pass
        las = laspy.LasData(header)
        las.x, las.y, las.z = self.xyz.T
        if self.rgb is not None:
            las.red, las.green, las.blue = (self.rgb.astype(np.uint16) * 257).T
        las.classification = self.classification
        if self.confidence is not None:  # extra bytes dimension, as Metashape exports it
            las.add_extra_dim(laspy.ExtraBytesParams(name="confidence", type=np.uint8,
                                                     description="depth maps agreeing on the point"))
            las.confidence = self.confidence
        las.write(str(path))
        return path

    @classmethod
    def load_las(cls, path: str | Path) -> "PointCloud":
        import laspy

        las = laspy.read(str(path))
        xyz = np.column_stack([las.x, las.y, las.z])
        rgb = None
        if hasattr(las, "red"):
            rgb = (np.column_stack([las.red, las.green, las.blue]) // 257).astype(np.uint8)
        confidence = None
        if "confidence" in las.point_format.extra_dimension_names:
            confidence = np.clip(np.asarray(las.confidence, np.float64), 0, 255).astype(np.uint8)
        return cls(xyz, rgb, None, np.asarray(las.classification), confidence=confidence)

    @classmethod
    def load(cls, path: str | Path) -> "PointCloud":
        suffix = Path(path).suffix.lower()
        if suffix in (".las", ".laz"):
            return cls.load_las(path)
        return cls.load_ply(path)

    def save(self, path: str | Path, crs_wkt: str | None = None) -> Path:
        suffix = Path(path).suffix.lower()
        if suffix in (".las", ".laz"):
            return self.save_las(path, crs_wkt)
        return self.save_ply(path)

    def voxel_downsample(self, voxel: float) -> "PointCloud":
        """Keep one point (the first) per voxel of side ``voxel``."""
        keys = np.floor((self.xyz - self.xyz.min(0)) / voxel).astype(np.int64)
        _, idx = np.unique(keys, axis=0, return_index=True)
        return self.subset(np.sort(idx))


_PLY_TYPES = {
    "char": "i1", "int8": "i1", "uchar": "u1", "uint8": "u1", "short": "i2", "int16": "i2",
    "ushort": "u2", "uint16": "u2", "int": "i4", "int32": "i4", "uint": "u4", "uint32": "u4",
    "float": "f4", "float32": "f4", "double": "f8", "float64": "f8",
}


def _codes(classes) -> np.ndarray:
    return np.asarray([int(c) for c in classes], np.uint8)


def _read_binary_ply_vertices(path: Path, mmap: bool = False) -> np.ndarray | None:
    """Read the vertex element of a binary little-endian PLY as a structured array
    (memory-mapped, read only, with ``mmap``).

    Returns ``None`` for ASCII files or files where vertices are not the first element.
    """
    with open(path, "rb") as fh:
        if fh.readline().strip() != b"ply":
            return None
        fmt = fh.readline().decode("ascii").split()
        if len(fmt) < 2 or fmt[1] != "binary_little_endian":
            return None
        count, fields, element = 0, [], None
        while True:
            line = fh.readline().decode("ascii").strip()
            if line == "end_header":
                break
            parts = line.split()
            if not parts or parts[0] in ("comment", "obj_info"):
                continue
            if parts[0] == "element":
                if element == "vertex":
                    break  # vertex properties collected; stop at next element
                element = parts[1]
                if element == "vertex":
                    count = int(parts[2])
                elif not fields:
                    return None
            elif parts[0] == "property" and element == "vertex":
                if parts[1] == "list":
                    return None
                fields.append((parts[2], "<" + _PLY_TYPES[parts[1]]))
        if line != "end_header":
            while fh.readline().strip() != b"end_header":
                pass
        if mmap:
            return np.memmap(path, dtype=fields, mode="r", offset=fh.tell(), shape=(count,))
        return np.frombuffer(fh.read(count * np.dtype(fields).itemsize), dtype=fields, count=count).copy()
