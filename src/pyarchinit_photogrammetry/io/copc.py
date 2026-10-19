"""Cloud Optimized Point Cloud (COPC 1.0): a LAZ 1.4 file whose points are arranged in an octree, so
that viewers (QGIS 3.32+, Potree, plas.io, CesiumJS through a converter) read only the part and the
level of detail they show.

The points go into the nodes of an octree over the bounding cube of the cloud: a node keeps one
point for every cell of a 128 x 128 x 128 grid over it (the coarse level of detail) and passes the
others to its eight children, down to nodes with at most ``max_points`` points; every node is one
LAZ chunk (point format 7: XYZ, RGB, classification; the confidence as an extra byte). The ``copc
info`` VLR (the first, at byte 375) says where the octree is and where its hierarchy page is; the
hierarchy (an EVLR) lists every node: key (depth, x, y, z), offset and size of its chunk, points.
Written with laspy and lazrs, ending a chunk at every node.
"""
from __future__ import annotations

import os
import struct
from pathlib import Path

import numpy as np

GRID = 128  # cells of a node along each axis: one point each at that level of detail


def _octree(xyz: np.ndarray, centre: np.ndarray, half: float, max_points: int):
    """Nodes ``[(depth, x, y, z), indices]`` (keys as in COPC: integer coordinates of the node
    among the 2^depth along each axis)."""
    nodes = []
    stack = [((0, 0, 0, 0), np.arange(len(xyz)))]
    while stack:
        (d, x, y, z), idx = stack.pop()
        size = 2 * half / 2 ** d
        lo = centre - half + size * np.array([x, y, z])
        if len(idx) <= max_points or d >= 20:
            nodes.append(((d, x, y, z), idx))
            continue
        cell = np.clip(((xyz[idx] - lo) / size * GRID).astype(np.int64), 0, GRID - 1)
        flat = (cell[:, 0] * GRID + cell[:, 1]) * GRID + cell[:, 2]
        _, first = np.unique(flat, return_index=True)  # one point per cell stays here
        keep = np.zeros(len(idx), bool)
        keep[first] = True
        nodes.append(((d, x, y, z), idx[keep]))
        rest = idx[~keep]
        if not len(rest):
            continue
        child = ((xyz[rest] - lo) >= size / 2).astype(np.int64)
        code = child[:, 0] * 4 + child[:, 1] * 2 + child[:, 2]
        for c in np.unique(code):
            cx, cy, cz = (c >> 2) & 1, (c >> 1) & 1, c & 1
            stack.append(((d + 1, 2 * x + cx, 2 * y + cy, 2 * z + cz), rest[code == c]))
    return nodes


class _VariableChunks:
    """A laspy LAZ backend (lazrs, one thread) whose chunks end where the writer says: one chunk
    per node, as COPC wants (laspy's own ends one every 50000 points)."""

    def is_available(self) -> bool:
        return True

    def create_writer(self, dest, header):
        import lazrs
        from laspy._compression.lazrsbackend import LazrsPointWriter

        w = LazrsPointWriter(dest, header.point_format, parallel=False)
        w.vlr = lazrs.LazVlr.new_for_compression(header.point_format.id, header.point_format.num_extra_bytes, True)
        return w


def write_copc(path: str | os.PathLike, xyz: np.ndarray, rgb: np.ndarray | None = None,
               classification: np.ndarray | None = None, confidence: np.ndarray | None = None,
               crs_wkt: str | None = None, max_points: int = 100_000, scale: float = 0.0005) -> Path:
    """Write a COPC file of the points ``xyz`` (world coordinates, metres) with their colours,
    classes and confidence; ``scale``: the precision of the coordinates (half a millimetre)."""
    import laspy

    path = Path(path)
    xyz = np.asarray(xyz, float).reshape(-1, 3)
    n = len(xyz)
    if n == 0:
        raise ValueError("no points")
    lo, hi = xyz.min(0), xyz.max(0)
    centre = (lo + hi) / 2
    half = float(max((hi - lo).max() / 2, 1e-6)) * 1.0001
    header = laspy.LasHeader(point_format=7, version="1.4")
    if confidence is not None:
        header.add_extra_dim(laspy.ExtraBytesParams(name="confidence", type=np.uint8,
                                                    description="depth maps confirming"))
    header.scales = np.array([scale] * 3)
    header.offsets = np.floor(lo)
    info = laspy.VLR(user_id="copc", record_id=1, description="copc info", record_data=bytes(160))
    header.vlrs.insert(0, info)  # it must be the first VLR
    if crs_wkt:
        from laspy.vlrs.known import WktCoordinateSystemVlr

        header.vlrs.append(WktCoordinateSystemVlr(crs_wkt))
        header.global_encoding.wkt = True
    nodes = _octree(xyz, centre, half, max_points)
    record = laspy.ScaleAwarePointRecord.zeros(n, header=header)
    record.x, record.y, record.z = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    if rgb is not None:
        c = np.asarray(rgb, np.uint16).reshape(n, 3) * 257
        record.red, record.green, record.blue = c[:, 0], c[:, 1], c[:, 2]
    if classification is not None:
        record.classification = np.asarray(classification, np.uint8)
    if confidence is not None:
        record.confidence = np.asarray(confidence, np.uint8)
    entries = []
    with open(path, "wb") as f:
        writer = laspy.LasWriter(f, header, do_compress=True, laz_backend=_VariableChunks())
        compressor = writer.point_writer.compressor
        first = True
        for key, idx in nodes:
            if not len(idx):
                continue
            # the first chunk begins after the 8 bytes that point to the chunk table
            start = f.tell() + (8 if first else 0)
            first = False
            writer.write_points(record[idx])
            compressor.finish_current_chunk()
            entries.append((key, start, f.tell() - start, len(idx)))
        from laspy.vlrs.vlrlist import VLRList

        hierarchy = VLRList()
        hierarchy.append(laspy.VLR(user_id="copc", record_id=1000, description="EPT hierarchy",
                                   record_data=b"".join(struct.pack("<iiiiQii", *k, o, s, c)
                                                        for k, o, s, c in entries)))
        writer.write_evlrs(hierarchy)
        writer.close()
    # the info VLR now that the hierarchy has a place: centre, half size, spacing, its page
    with open(path, "r+b") as f:
        f.seek(235)  # header: start of the first EVLR (LAS 1.4)
        evlr = struct.unpack("<Q", f.read(8))[0]
        data = struct.pack("<dddddQQdd", *centre, half, 2 * half / GRID, evlr + 60, 32 * len(entries), 0.0, 0.0)
        f.seek(375 + 54)
        f.write(data + bytes(160 - len(data)))
    return path


def copc_summary(path: str | os.PathLike) -> dict:
    """What a COPC reader sees: points, nodes, depth, bounds (with laspy's reader)."""
    import laspy

    with laspy.CopcReader.open(str(path)) as r:
        info = r.copc_info
        pts = r.query()
        nodes = r.root_page.entries if hasattr(r, "root_page") else None
        return {"points": len(pts), "center": list(info.center), "halfsize": info.halfsize,
                "spacing": info.spacing, "nodes": None if nodes is None else len(nodes)}
