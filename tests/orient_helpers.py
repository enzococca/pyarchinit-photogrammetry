"""A local, tilted copy of the processed trench and one drawing of each store kept in its coordinates: what the
orientation of a local system (``engine.orient.apply``) must carry along."""
from __future__ import annotations

import json
import shutil

import numpy as np


def rotation(axis, deg: float) -> np.ndarray:
    """The rotation of ``deg`` degrees about ``axis`` (right hand)."""
    a = np.asarray(axis, float)
    a = a / np.linalg.norm(a)
    t = np.radians(deg)
    k = np.array([[0.0, -a[2], a[1]], [a[2], 0.0, -a[0]], [-a[1], a[0], 0.0]])
    return np.eye(3) + np.sin(t) * k + (1 - np.cos(t)) * k @ k


def motion(r=None, t=(0.0, 0.0, 0.0), s: float = 1.0) -> np.ndarray:
    m = np.eye(4)
    m[:3, :3] = s * (np.eye(3) if r is None else np.asarray(r, float))
    m[:3, 3] = t
    return m


def tilt_motion(tilt=(12.0, 40.0), heading=35.0, t=(3.0, -2.0, 1.5)) -> np.ndarray:
    """A rotation that tilts the horizontal by ``tilt[0]`` degrees toward the azimuth ``tilt[1]`` after a turn of
    ``heading`` degrees about z, and a translation ``t``."""
    deg, direction = tilt
    d = np.radians(direction)
    hinge = np.array([np.cos(d), -np.sin(d), 0.0])  # horizontal, perpendicular to the direction of the fall
    return motion(rotation(hinge, deg) @ rotation([0, 0, 1], heading), t)


def local_copy(processed, tmp_path, tilt=(12.0, 40.0), heading=35.0, name="locale"):
    """The processed trench copied (as ``test_mesh_cut._copy``), its GCP references cleared, in a LOCAL system near
    the origin (``T0``: its georeference less a round origin) and then moved by a known rotation ``T_bad``: the chunk's
    transform is ``T_bad @ T0``. Its DEM is a sloping synthetic ground written in those local coordinates; the DEM and
    the orthomosaic are stamped in ``T_bad @ T0``. Returns ``(doc, ch, T_bad)``."""
    import pyarchinit_photogrammetry as ppg
    from pyarchinit_photogrammetry.core import crs as crs_mod
    from pyarchinit_photogrammetry.core import stamps
    from pyarchinit_photogrammetry.core.camera import Reference
    from pyarchinit_photogrammetry.engine import dem as dem_mod

    doc0, _ch, _ = processed
    d = tmp_path / name
    d.mkdir()
    shutil.copy2(doc0.path, d / doc0.path.name)
    shutil.copytree(doc0.files_dir, d / doc0.files_dir.name)
    shutil.copytree(doc0.path.parent / "photos", d / "photos")
    doc = ppg.Document.open(d / doc0.path.name)
    ch = doc.chunk
    for m in ch.markers:
        m.reference = Reference()
    ch.crs = crs_mod.LOCAL
    t0 = np.array(ch.transform, float)
    t0[:3, 3] -= np.floor(t0[:3, 3] / 1000.0) * 1000.0
    t0[2, 3] = t0[2, 3] - np.floor(t0[2, 3] / 100.0) * 100.0
    t_bad = tilt_motion(tilt, heading)
    ch.transform = t_bad @ t0
    ch.meta["levelled"] = True
    c = centre(ch)
    n = 200
    res = 0.05
    # a quarter of a cell off the round offsets the drawings use: no vertex on the edge of two cells
    x0, y0 = c[0] - n * res / 2 + res / 4, c[1] + n * res / 2 + res / 4
    xs = x0 + (np.arange(n) + 0.5) * res
    ys = y0 - (np.arange(n) + 0.5) * res
    gx, gy = np.meshgrid(xs, ys)
    ground = (c[2] - 0.3 + 0.08 * (gx - c[0]) - 0.05 * (gy - c[1])).astype(np.float32)
    dem = dem_mod.write_geotiff(ch.data_dir / "dem_locale.tif", ground, (x0, y0), res, None, nodata=dem_mod.NODATA)
    ch._set_product("dem", dem)
    stamps.stamp(ch, "dem")
    stamps.stamp(ch, "orthomosaic")
    doc.save()
    return doc, ch, t_bad


def new_alignment(ch, g=None) -> np.ndarray:
    """What a new alignment does to ``ch``: its cameras and tie points in another internal frame (the rigid motion
    ``g``, default a turn and a shift), the transform reset to the identity and ``levelled`` dropped. Returns ``g``."""
    g = motion(rotation([0.3, -0.2, 1.0], 25.0), (0.7, -1.1, 0.4)) if g is None else np.asarray(g, float)
    for cam in ch.cameras:
        if cam.transform is not None:
            cam.transform = g @ np.asarray(cam.transform, float)
    tie = ch.tie_points
    if tie is not None:
        tie.xyz = tie.xyz @ g[:3, :3].T + g[:3, 3]
        ch.tie_points = tie
    ch.transform = np.eye(4)
    ch.meta.pop("levelled", None)
    return g


def centre(ch) -> np.ndarray:
    """The middle of the model, in the coordinates of the chunk."""
    from pyarchinit_photogrammetry.io.mesh_ply import read_mesh

    return ch.to_world(read_mesh(ch.product_path("model")).xyz.mean(axis=0)[None])[0]


def dem_at(ch, x: float, y: float) -> float:
    import rasterio

    with rasterio.open(ch.dem_path) as src:
        return float(next(src.sample([(x, y)]))[0])


def _write_elevation(ch, frame, eid: str, kind: str, bounds, depth, resolution: float = 0.05) -> dict:
    """A small elevation (orthophoto and relief GeoTIFFs with the frame in their tags, ``frame.json``) and its entry,
    as ``engine.elevation.build_elevation`` writes them, stamped in the chunk's transform."""
    import rasterio
    from rasterio.transform import from_origin

    from pyarchinit_photogrammetry.core import stamps
    from pyarchinit_photogrammetry.engine import elevation

    folder = ch.data_dir / "elevations" / eid
    folder.mkdir(parents=True, exist_ok=True)
    u0, v0, u1, v1 = bounds
    w, h = int(round((u1 - u0) / resolution)), int(round((v1 - v0) / resolution))
    transform = from_origin(u0, v1, resolution, resolution)
    tags = {"ppg_frame": json.dumps(frame.to_dict()), "ppg_depth": json.dumps(list(depth))}
    with rasterio.open(folder / "elevation.tif", "w", driver="GTiff", width=w, height=h, count=4, dtype="uint8",
                       transform=transform) as dst:
        dst.write(np.full((4, h, w), 200, np.uint8))
        dst.update_tags(**tags)
    with rasterio.open(folder / "relief.tif", "w", driver="GTiff", width=w, height=h, count=1, dtype="float32",
                       transform=transform, nodata=float("nan")) as dst:
        dst.write(np.full((1, h, w), 0.01, np.float32))
        dst.update_tags(**tags)
    heights = elevation.heights_measured(frame)
    (folder / "frame.json").write_text(json.dumps({"frame": frame.to_dict(), "depth": list(depth),
                                                   "resolution": resolution, "kind": kind, "bounds": list(bounds),
                                                   "v_is_height": heights}, indent=1))
    entry = {"id": eid, "name": f"Prova {eid}", "kind": kind, "crs": ch.crs, "frame": frame.to_dict(),
             "depth": list(depth), "resolution": resolution, "bounds": list(bounds), "v_is_height": heights,
             "orthophoto": f"elevations/{eid}/elevation.tif", "relief": f"elevations/{eid}/relief.tif", "size": [w, h]}
    if getattr(frame, "tilt", None):
        entry["tilt"] = dict(frame.tilt)
    if kind == "section":
        (folder / "profile.json").write_text(json.dumps({"lines": [[[u0, v0 + 0.1], [u1, v0 + 0.2]]]}))
        entry["profile"] = f"elevations/{eid}/profile.json"
    stamps.stamp_entry(ch, entry)
    ch.meta.setdefault("elevations", []).append(entry)
    return entry


def plane_elevation(ch, c, eid: str = "el000001") -> dict:
    """A vertical section through ``c`` facing +y, with heights (v = Z), one unit, one spot height and one break line
    drawn on it."""
    from pyarchinit_photogrammetry.engine import elevation

    frame = elevation.with_heights(elevation.Plane([c[0] - 1.0, c[1], 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]))
    entry = _write_elevation(ch, frame, eid, "section", (0.0, c[2] - 1.0, 2.0, c[2] + 1.0), (-0.5, 0.0))
    import shapely

    u, v = 1.0, c[2]
    world = elevation.world_of(entry, u, v, 0.0)
    frames = ch.meta.setdefault("drawings", {}).setdefault("frames", {})
    frames[eid] = {"units": [{"wkt": shapely.Polygon([(0.5, v - 0.2), (1.5, v - 0.2), (1.5, v + 0.3)]).wkt,
                              "us": "9"}],
                   "quote": [{"u": u, "v": v, "x": float(world[0]), "y": float(world[1]), "quota_q": float(world[2])}],
                   "lines": [{"id": "fl", "kind": "breakline", "coords": [[0.2, v, None], [1.8, v + 0.1, None]]}],
                   "stones": {eid: [shapely.Polygon([(0.3, v), (0.6, v), (0.6, v + 0.2)]).wkt]}}
    return entry


def cylinder_elevation(ch, c, eid: str = "cy000001") -> dict:
    """A development on a leaning cylinder around ``c`` with a map of u along the wall and the tilt of its axis."""
    from pyarchinit_photogrammetry.engine import elevation

    axis = rotation([1.0, 0.0, 0.0], 6.0) @ np.array([0.0, 0.0, 1.0])
    tilt = elevation._axis_info("points", axis, float(c[2]))
    tilt["centre"] = [float(c[0]), float(c[1]), float(c[2])]
    tilt["fitted_radius"] = 1.5
    radius = 1.5
    theta = np.linspace(-180.0, 180.0, 13)
    u_map = np.column_stack([theta, np.radians(theta + 180.0) * radius * 1.07])
    frame = elevation.Cylinder(c, axis, radius, False, [1.0, 0.0, 0.0], tilt=tilt, u_map=u_map)
    entry = _write_elevation(ch, frame, eid, "elevation", (0.0, -1.0, 10.0, 1.0), (-0.3, 0.3))
    entry["scale"] = "wall"
    ch.meta.setdefault("drawings", {}).setdefault("frames", {})[eid] = {
        "quote": [{"u": 3.0, "v": 0.2, "x": float(c[0]), "y": float(c[1]), "quota_q": float(c[2])}]}
    return entry


def proxy(ch, c) -> dict:
    """A closed box under ``c`` as a proxy of a unit (PLY in double, world coordinates) and its record."""
    import trimesh

    from pyarchinit_photogrammetry.io.mesh_ply import write_mesh_ply

    box = trimesh.creation.box(extents=(0.6, 0.4, 0.25))
    box.apply_translation([c[0], c[1], c[2] - 0.4])
    folder = ch.data_dir / "proxies"
    folder.mkdir(parents=True, exist_ok=True)
    path = write_mesh_ply(folder / "US1.ply", np.asarray(box.vertices, float), np.asarray(box.faces), double=True)
    entry = {"name": "US1", "us": "1", "tipo": "US", "file": "proxies/US1.ply", "volume_m3": float(box.volume),
             "area_m2": 0.24, "z_top": [c[2] - 0.275, c[2] - 0.275], "z_bottom": [c[2] - 0.525, c[2] - 0.525],
             "base": "plane", "base_z": float(c[2] - 0.525)}
    ch.meta.setdefault("proxies", []).append(entry)
    assert path.exists()
    return entry


def drawn(ch) -> dict:
    """One of each store in the coordinates of ``ch``: a unit of the plan (sent to PyArchInit: a gid), a spot height,
    a measuring point, a break line with one vertex without a height, a section with heights (v = Z) with a drawing,
    a section of the project, the region, an ortho and a texture patch, the colour chart, a stone of the orthomosaic,
    a free line, a proxy and two markers with typed references. Returns what it drew."""
    import shapely

    from pyarchinit_photogrammetry.core.camera import Reference
    from pyarchinit_photogrammetry.engine.region import Region
    from pyarchinit_photogrammetry.gnss import heights

    c = centre(ch)
    x, y, z = (float(v) for v in c)
    square = [(x - 0.4, y - 0.3), (x + 0.5, y - 0.3), (x + 0.5, y + 0.4), (x - 0.4, y + 0.4)]
    d = ch.meta.setdefault("drawings", {})
    d["units"] = [{"wkt": shapely.Polygon(square).wkt, "us": "1", "tipo": "US", "attributes": {"gid": 7}}]
    d["quote"] = [{"x": x + 0.1, "y": y, "quota_q": z + 0.02, "gid": 3, "us_q": "1"}]
    d["points"] = [{"x": x - 0.2, "y": y + 0.1, "z": z - 0.05, "label": "P1"}]
    d["lines"] = [{"id": "l1", "kind": "breakline", "coords": [[x - 0.5, y, z], [x + 0.5, y + 0.2, None]],
                   "attributes": {}}]
    d["free"] = [{"id": "f1", "layer": "Disegni 3D", "kind": "line", "name": "",
                  "xyz": [[x, y, z], [x + 0.3, y + 0.1, z + 0.2], [x + 0.6, y - 0.1, z - 0.1]], "attributes": {}}]
    plane = plane_elevation(ch, c)
    ortho = heights._ortho_name(ch)
    ch.meta["stones"] = {ortho: [shapely.Polygon([(x, y), (x + 0.2, y), (x + 0.2, y + 0.15)]).wkt],
                         "dem_locale.tif": [shapely.Polygon([(x, y), (x - 0.2, y), (x - 0.2, y - 0.15)]).wkt]}
    ch.meta.setdefault("orthomosaic", {})["patches"] = [
        {"id": "p1", "polygon": [[x, y], [x + 0.3, y], [x + 0.3, y + 0.3]], "photos": [], "mode": "assign",
         "surface": "dem"}]
    ch.meta.setdefault("texture", {})["patches"] = [{"id": "t1", "polygon": [[x, y], [x - 0.3, y], [x, y + 0.3]]}]
    ch.meta["colour_chart"] = {"bounds": [x - 0.1, y - 0.1, x + 0.1, y + 0.05]}
    ch.meta["region"] = Region([x, y, z], [4.0, 3.0, 2.0], np.eye(3)).to_dict()
    box = proxy(ch, c)
    folder = ch.document.path.parent / "sezioni"
    folder.mkdir(exist_ok=True)
    sections = [{"label": "S1", "p1": [x - 1.0, y], "p2": [x + 1.0, y + 0.5], "length": float(np.hypot(2.0, 0.5)),
                 "kind": "vertical", "svg": "S1.svg"}]
    (folder / "sezioni.json").write_text(json.dumps(sections, indent=1), encoding="utf-8")
    typed = []
    for m in [m for m in ch.markers if m.position is not None][:2]:
        m.reference = Reference(location=[float(v) for v in ch.to_world(np.asarray(m.position)[None])[0]],
                                accuracy=[0.005] * 3)
        typed.append(m.label)
    ch.document.save()
    return {"centre": c, "square": square, "plane": plane, "proxy": box, "markers": typed}
