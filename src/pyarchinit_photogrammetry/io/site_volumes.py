"""Unit volumes (proxies) for the published web site.

A proxy built on a DEM at 1 cm has tens of thousands of faces per square metre; the site carries about
:data:`VOLUME_FACES` faces a volume and :data:`CHUNK_VOLUME_FACES` a chunk. Thinning must keep each solid
closed, its volume and its thickness: a layer a few centimetres thick must not collapse. :func:`thin_volume`
keeps a small solid as it is, else tries the quadric decimation of the meshing engine (Open3D, optional) and
accepts it only when it stays sound, else rebuilds the solid on a coarser grid from vertical rays, else
leaves the volume out.

In a site (product ``proxies`` of :func:`.site.export_site`) the volumes of a chunk are its entries of
``chunk.meta["proxies"]`` (:func:`entries`) read from their PLY (:func:`load`), thinned and packed
(:func:`prepare`, before the records of the site are matched, so that a volume left out has no record) and
written as ``data/[<n>/]proxies.glb`` and ``proxies.json`` (:func:`write`): no path, no id, no ``sito`` of an
entry.
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
    """The solid rebuilt as a prism on a grid of side ``sqrt(plan_area * 6 / target_faces)`` (:func:`_prism_on`);
    the walls take faces too, so a solid over the target is built again on a grid made coarser by the excess (a
    few times at most). ``None`` when nothing is left."""
    plan = _plan_area(mesh)
    if not plan > 0 or target_faces <= 0:
        return None
    side = math.sqrt(plan * 6.0 / target_faces)
    new = None
    for _ in range(4):
        new = _prism_on(mesh, side)
        if new is None or len(new.faces) <= target_faces:
            return new
        side *= math.sqrt(len(new.faces) / target_faces) * 1.02
    return new


def _prism_on(mesh, side: float):
    """The solid rebuilt as a prism on a grid of cells of ``side``: a vertical ray at each cell centre, cells hit
    at least twice, top the highest hit and bottom the lowest. ``None`` when nothing is left."""
    from ..archaeo.proxies import prism_mesh, surfaces

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


# ------------------------------------------------------------------------------------------------ the volumes of a chunk
def path(chunk, entry: dict):
    """The PLY of ``entry`` (written relative to ``chunk.data_dir``)."""
    return chunk.data_dir / str(entry["file"])


def entries(chunk) -> list[dict]:
    """The well-formed entries of ``chunk.meta["proxies"]``: a dict with a ``file`` relative to the folder of the
    chunk's data, inside it. Whether the file is there is :func:`load`'s to say."""
    from pathlib import PurePath

    out = []
    for e in chunk.meta.get("proxies") or []:
        if not isinstance(e, dict) or not isinstance(e.get("file"), str) or not e["file"].strip():
            continue
        p = PurePath(e["file"].replace("\\", "/"))
        if p.is_absolute() or ".." in p.parts:
            continue
        out.append(e)
    return out


def present(chunk) -> list[dict]:
    """The entries (:func:`entries`) whose file is there."""
    return [e for e in entries(chunk) if path(chunk, e).is_file()]


def _name(entry: dict) -> str:
    from ..archaeo.proxies import proxy_name

    return str(entry.get("name") or proxy_name(entry.get("tipo"), entry.get("us")))


def _pair(value) -> bool:
    """A height pair of an entry (``z_top``, ``z_bottom``) as the program writes it: absent, or a list."""
    return value is None or isinstance(value, (list, tuple))


def load(chunk, notes: list[str]) -> list[tuple[dict, object]]:
    """``[(entry, mesh), ...]``: the volumes of ``chunk`` read from their PLY (world coordinates); an entry that is
    malformed (its heights not pairs), a file that is missing or unreadable, or a mesh that is not closed is left
    out with a note in ``notes``."""
    import trimesh

    out = []
    for e in entries(chunk):
        if not (_pair(e.get("z_top")) and _pair(e.get("z_bottom"))):
            notes.append(f"{chunk.label}: volume {_name(e)} left out (malformed entry)")
            continue
        p = path(chunk, e)
        if not p.is_file():
            notes.append(f"{chunk.label}: volume {_name(e)} left out (missing)")
            continue
        try:
            mesh = trimesh.load(str(p), force="mesh", process=True)
        except Exception:  # noqa: BLE001 - any broken file leaves the volume out, never the site
            mesh = None
        if not isinstance(mesh, trimesh.Trimesh) or len(mesh.faces) == 0:
            notes.append(f"{chunk.label}: volume {_name(e)} left out (unreadable)")
            continue
        if not mesh.is_watertight:
            notes.append(f"{chunk.label}: volume {_name(e)} left out (not a closed mesh)")
            continue
        out.append((e, mesh))
    return out


def lowest(loaded) -> float | None:
    """The lowest point of the volumes ``loaded`` (:func:`load`), None without any."""
    lows = [float(m.bounds[0][2]) for _e, m in loaded]
    return min(lows) if lows else None


def bounds(loaded) -> tuple[float, float, float, float] | None:
    """The plan box of the volumes ``loaded`` (:func:`load`)."""
    if not loaded:
        return None
    lo = np.min([m.bounds[0] for _e, m in loaded], axis=0)
    hi = np.max([m.bounds[1] for _e, m in loaded], axis=0)
    return float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1])


def _height(value, dz: float):
    try:
        z = float(value)
    except (TypeError, ValueError):
        return None
    return round(z - dz, 3) if math.isfinite(z) else None


def _number(value, digits: int):
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return round(x, digits) if math.isfinite(x) else None


def _thinned(chunk, loaded, target: int, notes: list[str]) -> list[tuple[dict, object]]:
    out = []
    for e, mesh in loaded:
        new, _how = thin_volume(mesh, target)
        if new is None:
            notes.append(f"{chunk.label}: volume {_name(e)} left out (it could not be thinned to {target} faces "
                         "and stay closed)")
            continue
        out.append((e, new))
    return out


def prepare(chunk, loaded, shift, frame: str, scrub, notes: list[str]) -> dict | None:
    """The volumes ``loaded`` (:func:`load`) of ``chunk`` thinned and packed as ``proxies.glb``, before anything
    is written: ``{"kept": [(entry, thinned mesh), ...], "keys", "blob", "lo", "hi", "origin"}``, None when no
    volume is left. Run before the records are matched (``kept`` are the volumes the site publishes).

    Each volume is thinned to ``min(VOLUME_FACES, CHUNK_VOLUME_FACES // n)`` faces (:func:`thin_volume`); a
    ``proxies.glb`` over the limit of a Pages file has its targets halved once, else the volumes are left out with
    a note. ``shift``: the local frame (``frame == "local"``); in ``"crs"`` the positions are around the centre of
    the volumes, the ``origin`` of the layer. ``scrub``: what removes the paths of the computer from a text (the
    keys, the names of the nodes)."""
    from .site_publish import PAGES_FILE_BYTES
    from .web import _glb

    if not loaded:
        return None
    target = min(VOLUME_FACES, CHUNK_VOLUME_FACES // len(loaded))
    for attempt in (target, target // 2):
        tried: list[str] = []
        kept = _thinned(chunk, loaded, attempt, tried)
        if not kept:
            notes.extend(tried)
            return None
        lo = np.min([m.bounds[0] for _e, m in kept], axis=0)
        hi = np.max([m.bounds[1] for _e, m in kept], axis=0)
        if frame == "crs":
            centre = (lo + hi) / 2
            origin = [float(v) for v in centre]
        else:
            centre = np.array([shift.dx, shift.dy, shift.dz], float)
            origin = None
        keys, prims = [], []
        for e, mesh in kept:
            key = scrub(_name(e)) or "volume"
            base, k = key, 2
            while key in keys:
                key, k = f"{base}-{k}", k + 1
            keys.append(key)
            v = np.asarray(mesh.vertices, float) - centre
            prims.append({"position": np.column_stack([v[:, 0], v[:, 2], -v[:, 1]]),
                          "index": np.asarray(mesh.faces, np.uint32).ravel()})
        blob = _glb(prims, names=keys)
        if len(blob) <= PAGES_FILE_BYTES:
            notes.extend(tried)
            return {"kept": kept, "keys": keys, "blob": blob, "lo": lo, "hi": hi, "origin": origin}
    notes.append(f"{chunk.label}: volumes left out (proxies.glb over {PAGES_FILE_BYTES // 2**20} MiB a file "
                 "even with half the faces)")
    return None


def write(chunk, prepared, data, rel: str, shift, scrub, record_of, groups, notes: list[str]) -> dict | None:
    """Write the volumes ``prepared`` (:func:`prepare`) of ``chunk`` in ``data``: ``proxies.glb`` (one mesh and one
    node per volume, named by its key, Y-up as the tiles of the model) and ``proxies.json`` (what the viewer shows
    of each), and return their layer of ``site.json``; None when no volume is left (``prepared`` None).

    Heights less ``shift.dz``. ``scrub``: what removes the paths of the computer from a text; ``record_of(entry)``:
    the key of the record of a volume, None: no record (None: the site has no records, no ``record`` is written);
    ``groups``: the unit groups a plugin gives (:func:`apply_groups`; None: each volume the colour of the program by
    its index). The ``sito`` of an entry, its file and its ids are never written."""
    import json

    from ..archaeo.proxies import PROXY_COLOURS
    from .site import DECIMALS, TITLES

    if not prepared:
        return None
    every = chunk.meta.get("proxies") or []
    kept, keys, lo, hi = prepared["kept"], prepared["keys"], prepared["lo"], prepared["hi"]
    volumes = []
    for n, ((e, _mesh), key) in enumerate(zip(kept, keys)):
        index = next((i for i, x in enumerate(every) if x is e), n)
        tipo = str(e.get("tipo") or "US")
        us = e.get("us")
        v = {"key": key, "label": scrub(f"{tipo} {us}" if us not in (None, "") else tipo),
             "us": None if us in (None, "") else scrub(str(us)),
             "area": None if e.get("area") in (None, "") else scrub(str(e["area"])), "tipo": scrub(tipo),
             "volume_m3": _number(e.get("volume_m3"), 4), "area_m2": _number(e.get("area_m2"), 3),
             "z_top": [_height(z, shift.dz) for z in list(e.get("z_top") or [None, None])[:2]],
             "z_bottom": [_height(z, shift.dz) for z in list(e.get("z_bottom") or [None, None])[:2]],
             "colour": PROXY_COLOURS[index % len(PROXY_COLOURS)], "group": None}
        if record_of is not None:
            record = record_of(e)
            if record:
                v["record"] = record
        volumes.append(v)
    legend = apply_groups(volumes, groups)
    (data / "proxies.glb").write_bytes(prepared["blob"])
    (data / "proxies.json").write_text(json.dumps({"title": TITLES["volumes"], "volumes": volumes,
                                                   "groups": legend},
                                                  ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    box_lo, box_hi = lo - np.array([shift.dx, shift.dy, shift.dz]), hi - np.array([shift.dx, shift.dy, shift.dz])
    layer = {"id": "volumes", "type": "volumes", "url": f"{rel}/proxies.glb", "data": f"{rel}/proxies.json",
             "title": TITLES["volumes"], "visible": True, "count": len(volumes),
             "box": [round(float(x), DECIMALS) for x in (*box_lo, *box_hi)],
             "bytes": (data / "proxies.glb").stat().st_size + (data / "proxies.json").stat().st_size}
    if legend:
        layer["groups"] = legend
    if prepared["origin"] is not None:
        layer["origin"] = prepared["origin"]
    return layer


NO_GROUP = {"it": "Senza gruppo", "en": "No group"}


def apply_groups(volumes: list[dict], groups: dict | None) -> list[dict]:
    """Set ``group`` and ``colour`` of each of ``volumes`` (as :func:`write` makes them) by the unit groups of a
    plugin (``{"plugin", "id", "title", "groups": [{"name", "title", "colour", "units": [{"tipo", "us",
    "area"}]}]}``, :func:`.site_plugins.write`): a volume goes to the first group naming its type and number, and
    its area when both have one (trimmed text, the type in capitals). Returns the legend ``[{"name", "title",
    "colour", "count"}]``, one row a group, and last ``{"name": "", "title": Senza gruppo / No group, "colour":
    None}`` for the volumes in no group, which keep the colours of the program; ``[]`` without groups."""
    from .site_records import _type, unit_text

    if not groups or not groups.get("groups"):
        return []
    legend = [{"name": g["name"], "title": g["title"], "colour": g["colour"], "count": 0} for g in groups["groups"]]
    rest = 0
    for v in volumes:
        tipo, us, area = _type(v.get("tipo")), unit_text(v.get("us")), unit_text(v.get("area"))
        found = None
        for n, g in enumerate(groups["groups"]):
            if any(_type(u.get("tipo")) == tipo and unit_text(u.get("us")) == us
                   and (not area or not unit_text(u.get("area")) or unit_text(u.get("area")) == area)
                   for u in g.get("units") or ()):
                found = n
                break
        if not us or found is None:
            rest += 1
            continue
        v["group"], v["colour"] = legend[found]["name"], legend[found]["colour"]
        legend[found]["count"] += 1
    if rest:
        legend.append({"name": "", "title": dict(NO_GROUP), "colour": None, "count": rest})
    return legend
