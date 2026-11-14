"""The dense cloud of a chunk on a web site (:mod:`.site`): 3D Tiles of points, and the same points as COPC to download.

The viewer of a site shows the cloud with the renderer that shows the model (3DTilesRendererJS): the points go into
the octree of :mod:`.copc` - a node keeps one point for every cell of a 128 x 128 x 128 grid over it and passes the
others to its eight children - and every node is a tile, a binary glTF of points (``POINTS``: positions, float32,
Y up as glTF wants; colours linear, unsigned 16 bit normalised), refined by adding its children (``"refine": "ADD"``):
the coarse levels are drawn first, the finer ones as the camera comes near. The tiles are small files read whole,
so the cloud needs no range requests from the host.

Why not COPC read in the browser (copc.js with laz-perf in WebAssembly): it needs a WebAssembly compiler allowed by the
page (``'wasm-unsafe-eval'`` in its security policy), range requests from the host (GitHub Pages and many hosts
answer them, a few do not), a third library and its decoder to vendor, and a second way to cull, load and pick
points beside the one of the model. The tiles reuse what the model has: one renderer, one level of detail, one way
to pick a point to measure. The COPC file is still written beside them (``cloud.copc.laz``) for QGIS, CloudCompare
or Potree, in the frame of the site.

Coordinates: in the local frame of a site the tiles and the COPC are in its metres (the coordinates less
``origin``); in the ``crs`` frame the tiles are around the centre of the cloud, placed on the globe as the tiles of
the model (:func:`.web._enu_to_ecef`), the centre in ``extras.origin``, and the COPC keeps the true coordinates and
its reference system.
"""
from __future__ import annotations

import json
import os
import struct
from pathlib import Path

import numpy as np

NODE_POINTS = 60_000  # points of a tile at most (a leaf of the octree)
POINT_BYTES = 21  # bytes of a point in the tiles (position 12, colour 8) and its share of the octree
COPC_BYTES = 5  # bytes of a point in the COPC (LAZ), about


def thin(n: int, keep: int, seed: int = 0) -> np.ndarray:
    """``keep`` indices among ``n``, spread over the whole cloud (a random choice, in order): all when ``keep >= n``."""
    if keep >= n:
        return np.arange(n)
    rng = np.random.default_rng(seed)
    return np.sort(rng.choice(n, int(max(keep, 1)), replace=False))


def _linear16(rgb: np.ndarray) -> np.ndarray:
    """sRGB colours (0-255) as the linear ones of glTF, unsigned 16 bit normalised, with an opaque alpha."""
    srgb = np.asarray(rgb, np.float64) / 255.0
    lin = np.where(srgb <= 0.04045, srgb / 12.92, ((srgb + 0.055) / 1.055) ** 2.4)
    out = np.empty((len(rgb), 4), np.uint16)
    out[:, :3] = np.round(lin * 65535)
    out[:, 3] = 65535
    return out


def points_glb(position: np.ndarray, colour: np.ndarray) -> bytes:
    """A binary glTF of points: ``position`` (n, 3) in glTF axes (Y up), ``colour`` (n, 3) sRGB 0-255."""
    pos = np.ascontiguousarray(position, np.float32)
    col = np.ascontiguousarray(_linear16(colour))
    blob = pos.tobytes() + col.tobytes()
    gltf = {
        "asset": {"version": "2.0", "generator": "pyarchinit-photogrammetry"},
        "extensionsUsed": ["KHR_materials_unlit"], "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "COLOR_0": 1}, "mode": 0, "material": 0}]}],
        "materials": [{"pbrMetallicRoughness": {"baseColorFactor": [1.0, 1.0, 1.0, 1.0], "metallicFactor": 0.0,
                                                "roughnessFactor": 1.0},
                       "extensions": {"KHR_materials_unlit": {}}}],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": int(len(pos)), "type": "VEC3",
             "min": [float(v) for v in pos.min(0)], "max": [float(v) for v in pos.max(0)]},
            {"bufferView": 1, "componentType": 5123, "normalized": True, "count": int(len(pos)), "type": "VEC4"}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": pos.nbytes, "target": 34962},
                        {"buffer": 0, "byteOffset": pos.nbytes, "byteLength": col.nbytes, "target": 34962}],
        "buffers": [{"byteLength": len(blob)}],
    }
    text = json.dumps(gltf, separators=(",", ":")).encode()
    text += b" " * (-len(text) % 4)
    blob += b"\0" * (-len(blob) % 4)
    total = 12 + 8 + len(text) + 8 + len(blob)
    return (struct.pack("<4sII", b"glTF", 2, total) + struct.pack("<I4s", len(text), b"JSON") + text
            + struct.pack("<I4s", len(blob), b"BIN\0") + blob)


def write_cloud_tiles(folder: str | os.PathLike, xyz: np.ndarray, rgb: np.ndarray | None, *,
                      transform: list[float] | None = None, extras: dict | None = None,
                      node_points: int = NODE_POINTS, progress=None) -> Path:
    """The points ``xyz`` (east, north, up, in the frame the tiles are in) with their colours as 3D Tiles 1.1 in
    ``folder`` (``tileset.json``, ``tiles/*.glb``; see the module docstring). ``transform``: of the root (column
    major, to the globe); ``extras``: of the tileset. Returns the tileset."""
    from ..core.progress import as_progress
    from .copc import GRID, _octree

    progress = as_progress(progress)
    xyz = np.asarray(xyz, np.float64).reshape(-1, 3)
    if not len(xyz):
        raise ValueError("no points")
    rgb = np.full((len(xyz), 3), 200, np.uint8) if rgb is None else np.asarray(rgb, np.uint8).reshape(-1, 3)
    folder = Path(folder)
    (folder / "tiles").mkdir(parents=True, exist_ok=True)
    lo, hi = xyz.min(0), xyz.max(0)
    centre = (lo + hi) / 2
    half = float(max((hi - lo).max() / 2, 1e-3)) * 1.0001
    nodes = {key: idx for key, idx in _octree(xyz, centre, half, int(node_points))}

    def children(key):
        d, x, y, z = key
        return [k for k in ((d + 1, 2 * x + i, 2 * y + j, 2 * z + m) for i in (0, 1) for j in (0, 1) for m in (0, 1))
                if k in nodes]

    written = [0]

    def box(key) -> list[float]:
        d, x, y, z = key
        size = 2 * half / 2 ** d
        a = centre - half + size * np.array([x, y, z])
        b = np.minimum(a + size, hi)  # the cube of the node within the box of the cloud: it holds its children
        a = np.maximum(a, lo)
        c, h = (a + b) / 2, np.maximum((b - a) / 2, 1e-3)
        return [*map(float, c), float(h[0]), 0.0, 0.0, 0.0, float(h[1]), 0.0, 0.0, 0.0, float(h[2])]

    def node(key) -> dict:
        idx = nodes[key]
        name = f"tiles/{'-'.join(map(str, key))}.glb"
        p = xyz[idx]
        (folder / name).write_bytes(points_glb(np.column_stack([p[:, 0], p[:, 2], -p[:, 1]]), rgb[idx]))
        written[0] += 1
        progress(min(0.95, written[0] / max(len(nodes), 1)), f"cloud: tile {written[0]} of {len(nodes)}")
        kids = children(key)
        out = {"boundingVolume": {"box": box(key)},
               # without its children a node shows the points of its grid: their spacing is its error
               "geometricError": float(2 * half / 2 ** key[0] / GRID) if kids else 0.0,
               "refine": "ADD", "content": {"uri": name}}
        if kids:
            out["children"] = [node(k) for k in kids]
        return out

    root = node((0, 0, 0, 0))
    if transform is not None:
        root["transform"] = list(transform)
    tileset = {"asset": {"version": "1.1", "generator": "pyarchinit-photogrammetry"},
               "geometricError": float(root["geometricError"]) * 2 or 1.0, "root": root}
    if extras:
        tileset["extras"] = extras
    path = folder / "tileset.json"
    path.write_text(json.dumps(tileset, indent=1))
    progress(1.0, f"cloud: {written[0]} tiles")
    return path


def tiles_points(folder: str | os.PathLike) -> int:
    """The points of the tiles of a cloud (every tile adds its own)."""
    total = 0
    for f in Path(folder, "tiles").glob("*.glb"):
        data = f.read_bytes()
        size = struct.unpack_from("<I", data, 12)[0]
        total += json.loads(data[20:20 + size])["accessors"][0]["count"]
    return total


__all__ = ["write_cloud_tiles", "points_glb", "thin", "tiles_points", "NODE_POINTS", "POINT_BYTES", "COPC_BYTES"]
