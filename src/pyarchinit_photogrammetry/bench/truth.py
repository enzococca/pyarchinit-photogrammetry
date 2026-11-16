"""What ``ppg bench synthetic`` measures against the truth of its sets, as the extra results of a run (numbers only,
the keys that start with ``truth_``): the trench against the targets of its ``gcp.csv`` with the control/check split of
the metric tests (:func:`trench_truth`), the ring against the true targets and poses of its scene after the
georeferencing by the positions of its photos (:func:`ring_truth`)."""
from __future__ import annotations

import numpy as np

# the check points of the trench: the split of tests/test_pipeline.py::test_checkpoints (targets 4 and 5 left out of
# the fit and measured, the other six control)
TRENCH_CHECK = ("target 4", "target 5")


def _rms(values) -> float:
    return float(np.sqrt(np.mean(np.square(np.asarray(values, float)))))


def trench_truth(doc) -> dict:
    """The errors of the trench against its ``gcp.csv``, the references its chain imported, with the split of the
    metric tests (``tests/test_pipeline.py::test_checkpoints``): :data:`TRENCH_CHECK` as check points and the fit
    done again on the other targets, in memory. ``truth_control_rmse`` and ``truth_check_rmse``, in metres.

    Afterwards the transform and the check flags of the chunk are put back and nothing is saved; what else
    ``update_transform`` does on the way (the markers triangulated again, the precision of the chunk forgotten, the
    free drawings moved with the fit) is not undone - the document is the finished project of a run, opened for this
    measurement and discarded. A ValueError when that fit cannot be made (``method`` is not ``markers``) or a group
    has no error (targets not found)."""
    chunk = doc.chunk
    if chunk is None:
        raise ValueError("the project has no chunk")
    before = np.array(chunk.transform, float)
    flags = {m.key: m.checkpoint for m in chunk.markers}
    try:
        for m in chunk.markers:
            m.checkpoint = m.label in TRENCH_CHECK
        report = chunk.update_transform()
    finally:
        for m in chunk.markers:
            m.checkpoint = flags[m.key]
        chunk.transform = before
    if report.get("method") != "markers":
        raise ValueError(f"the fit on the control targets was not made (method {report.get('method')})")
    if report.get("control_rmse") is None or report.get("check_rmse") is None:
        raise ValueError("the control or the check targets have no error: targets not found")
    return {"truth_control_rmse": float(report["control_rmse"]), "truth_check_rmse": float(report["check_rmse"])}


def ring_truth(doc, scene) -> dict:
    """The errors of the ring against the truth of its scene, after the georeferencing by the positions of its photos:
    the targets the chain found and triangulated against ``scene.targets`` (``truth_target_rmse`` in metres, over
    ``truth_targets`` targets; no RMSE when none was found) and the centres of the aligned cameras against the true
    poses of ``scene.cameras`` (``truth_camera_rmse``; of the heights alone ``truth_camera_height_mean``, estimated
    minus true, which keeps the sign of heights that landed too high or too low, and ``truth_camera_height_rmse``).
    The truth goes the way the positions of the photos went: from the scene's system to EPSG:4326 as the set was
    written, then to the chunk's system as the program reads them. A ValueError when the chunk is not georeferenced
    or no aligned camera has a true pose."""
    from pyproj import Transformer

    from ..core import crs as crs_mod

    chunk = doc.chunk
    if chunk is None or crs_mod.is_local(chunk.crs) or crs_mod.is_geographic(chunk.crs):
        raise ValueError("the ring is not georeferenced: nothing to compare with the truth")
    to_wgs84 = Transformer.from_crs(scene.crs, "EPSG:4326", always_xy=True)

    def in_chunk_crs(points) -> np.ndarray:
        pts = np.atleast_2d(np.asarray(points, float))
        lon, lat = to_wgs84.transform(pts[:, 0], pts[:, 1])
        return crs_mod.transform_points(np.column_stack([lon, lat, pts[:, 2]]), "EPSG:4326", chunk.crs)

    poses = {cam["label"]: np.asarray(cam["world_from_cam"], float)[:3, 3] for cam in scene.cameras}
    cameras = [c for c in chunk.aligned_cameras if c.label in poses]
    if not cameras:
        raise ValueError("no aligned camera of the ring has a true pose")
    markers = [m for m in chunk.markers if m.code in scene.targets and m.position is not None]
    results = {"truth_targets": len(markers)}
    if markers:
        estimated = chunk.to_world(np.array([m.position for m in markers], float).reshape(-1, 3))
        d = estimated - in_chunk_crs([scene.targets[m.code] for m in markers])
        results["truth_target_rmse"] = _rms(np.linalg.norm(d, axis=1))
    estimated = chunk.to_world(np.array([c.center for c in cameras], float).reshape(-1, 3))
    d = estimated - in_chunk_crs([poses[c.label] for c in cameras])
    results.update(truth_camera_rmse=_rms(np.linalg.norm(d, axis=1)), truth_camera_height_mean=float(np.mean(d[:, 2])),
                   truth_camera_height_rmse=_rms(d[:, 2]))
    return results
