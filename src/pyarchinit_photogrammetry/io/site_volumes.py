"""Unit volumes (proxies) for the published web site.

A proxy built on a DEM at 1 cm has tens of thousands of faces per square metre; the site carries about
:data:`VOLUME_FACES` faces a volume and :data:`CHUNK_VOLUME_FACES` a chunk. Thinning must keep each solid
closed, its volume and its thickness: a layer a few centimetres thick must not collapse. :func:`thin_volume`
keeps a small solid as it is, else tries the quadric decimation of the meshing engine (Open3D, optional) and
accepts it only when it stays sound, else rebuilds the solid on a coarser grid from vertical rays, else
leaves the volume out.
"""
from __future__ import annotations

import math

import numpy as np

VOLUME_FACES = 20_000
CHUNK_VOLUME_FACES = 600_000

_VOLUME_TOLERANCE = 0.1  # relative
_Z_TOLERANCE = 0.005  # metres, on the lowest and the highest point
_FACE_SLACK = 1.1


def _sound(new, old, target_faces: int) -> bool:
    """``new`` may stand for ``old`` on the site: closed, no more than 10 % above the target faces, its volume
    within 10 % and its lowest and highest points within 5 mm."""
    if new is None or len(new.faces) == 0 or len(new.faces) > _FACE_SLACK * target_faces:
        return False
    if not new.is_watertight:
        return False
    v_old, v_new = abs(float(old.volume)), abs(float(new.volume))
    if abs(v_new - v_old) > _VOLUME_TOLERANCE * v_old:
        return False
    lo_old, hi_old = old.bounds[0][2], old.bounds[1][2]
    lo_new, hi_new = new.bounds[0][2], new.bounds[1][2]
    return abs(lo_new - lo_old) <= _Z_TOLERANCE and abs(hi_new - hi_old) <= _Z_TOLERANCE


def _plan_area(mesh) -> float:
    """Area of the solid seen from above: every vertical line through it crosses the surface twice, so the
    projected areas of all faces add up to twice the plan."""
    tri = np.asarray(mesh.triangles)
    e1, e2 = tri[:, 1, :2] - tri[:, 0, :2], tri[:, 2, :2] - tri[:, 0, :2]
    return float(np.abs(e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]).sum() / 4.0)


def _regrid(mesh, target_faces: int):
    """The solid rebuilt as a prism on a grid of side ``sqrt(plan_area * 6 / target_faces)``: a vertical ray
    at each cell centre, cells hit at least twice, top the highest hit and bottom the lowest. ``None`` when
    nothing is left."""
    from ..archaeo.proxies import prism_mesh, surfaces

    plan = _plan_area(mesh)
    if not plan > 0 or target_faces <= 0:
        return None
    side = math.sqrt(plan * 6.0 / target_faces)
    (x_min, y_min, z_min), (x_max, y_max, z_max) = mesh.bounds
    nx = max(1, int(math.ceil((x_max - x_min) / side)))
    ny = max(1, int(math.ceil((y_max - y_min) / side)))
    dx, dy = (x_max - x_min) / nx, -(y_max - y_min) / ny  # north-up rows, as a DEM
    cx = x_min + (np.arange(nx) + 0.5) * dx
    cy = y_max + (np.arange(ny) + 0.5) * dy
    gx, gy = np.meshgrid(cx, cy)
    start = float(z_max) + 1.0
    origins = np.column_stack([gx.ravel(), gy.ravel(), np.full(gx.size, start)])
    dirs = np.tile([0.0, 0.0, -1.0], (gx.size, 1))
    locs, ray_idx, _ = mesh.ray.intersects_location(origins, dirs, multiple_hits=True)
    if len(ray_idx) == 0:
        return None
    count = np.bincount(ray_idx, minlength=gx.size)
    top = np.full(gx.size, -np.inf)
    bottom = np.full(gx.size, np.inf)
    np.maximum.at(top, ray_idx, locs[:, 2])
    np.minimum.at(bottom, ray_idx, locs[:, 2])
    mask = (count >= 2).reshape(gx.shape)
    if not mask.any():
        return None
    top = np.where(mask.ravel(), top, 0.0).reshape(gx.shape)
    bottom = np.where(mask.ravel(), bottom, 0.0).reshape(gx.shape)
    hi, lo = surfaces(top, bottom)
    return prism_mesh(hi, lo, mask, float(x_min), float(y_max), dx, dy)


def thin_volume(mesh, target_faces: int):
    """``(mesh, how)``: ``how`` is ``"kept"`` (already small enough, the same object), ``"quadric"``
    (decimated), ``"regrid"`` (rebuilt on a coarser grid) or ``"dropped"`` (``mesh`` is ``None``: no thinned
    solid was sound, the caller leaves the volume out with a note)."""
    if len(mesh.faces) <= target_faces:
        return mesh, "kept"
    from ..engine import meshing

    try:
        new = meshing._decimate(mesh, target_faces)
    except Exception:  # noqa: BLE001 - Open3D is optional and may fail on any mesh
        new = None
    if new is not None and new is not mesh and _sound(new, mesh, target_faces):
        return new, "quadric"
    try:
        new = _regrid(mesh, target_faces)
    except Exception:  # noqa: BLE001 - a ray engine failure leaves the volume out
        new = None
    if new is not None and new.is_watertight and len(new.faces) <= _FACE_SLACK * target_faces:
        return new, "regrid"
    return None, "dropped"
