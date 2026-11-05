"""The frame of a local system and the motion that sets it (pure numpy)."""
from __future__ import annotations

import json

import numpy as np
import pytest

from pyarchinit_photogrammetry.core import geometry as geo
from pyarchinit_photogrammetry.engine import orient


def _plane_normal(tilt, direction):
    t, d = np.radians(tilt), np.radians(direction)
    return np.array([np.sin(t) * np.sin(d), np.sin(t) * np.cos(d), np.cos(t)])


def _in_plane(n, plan_deg):
    h = np.radians(plan_deg)
    hx, hy = np.cos(h), np.sin(h)
    return np.array([hx, hy, -(n[0] * hx + n[1] * hy) / n[2]])


def test_three_points_give_the_plane_and_x():
    n = _plane_normal(12, 40)
    a = np.array([10.0, -4.0, 2.0])
    b = a + 3.0 * _in_plane(n, 35)
    c = a + 2.0 * _in_plane(n, 130)
    f = orient.frame_from_points(a, b, c)
    m = orient.motion(f)
    assert np.allclose(geo.apply(m, a), 0, atol=1e-9)
    assert np.allclose(geo.apply(m, b), [np.linalg.norm(b - a), 0, 0], atol=1e-9)
    assert abs(geo.apply(m, c)[2]) < 1e-9
    d = orient.describe(m)
    assert d["tilt_deg"] == pytest.approx(12, abs=1e-9)
    assert d["heading_deg"] == pytest.approx(55, abs=1e-9)  # azimuth: 35 deg from +x = 55 from north
    assert d["tilt_direction_deg"] == pytest.approx(40, abs=1e-9)
    assert f.tilt_deg == pytest.approx(12, abs=1e-9)


def test_x_kept_projected_on_the_plane():
    # tilted toward +y the current +x lies in the plane: azimuth 90 (east); toward 40 it does not
    for direction in (0, 40):
        n = _plane_normal(12, direction)
        a = np.zeros(3)
        b = 3.0 * _in_plane(n, 35)
        c = 2.0 * _in_plane(n, 130)
        f = orient.frame_from_points(a, b, c, x_along_ab=False)
        ex = np.array([1.0, 0, 0])
        want = ex - (ex @ n) * n
        want /= np.linalg.norm(want)
        assert np.allclose(f.axes[0], want, atol=1e-12)
        assert abs(f.axes[0] @ f.axes[2]) < 1e-12
        az = np.degrees(np.arctan2(want[0], want[1])) % 360
        assert f.heading_deg == pytest.approx(az, abs=1e-9)
        if direction == 0:
            assert f.heading_deg == pytest.approx(90, abs=1e-9)


def test_two_points_keep_the_horizontal():
    a = np.array([1.0, 2.0, 5.0])
    b = np.array([4.0, 6.0, 9.0])
    f = orient.frame_from_points(a, b)
    assert f.tilt_deg == 0
    m = orient.motion(f)
    pts = np.random.default_rng(1).normal(size=(10, 3))
    out = geo.apply(m, pts)
    assert np.allclose(out[:, 2], pts[:, 2] - a[2])
    assert np.allclose(geo.apply(m, b), [5.0, 0, 4.0], atol=1e-12)


def test_far_side_c_and_flip():
    a = np.zeros(3)
    b = np.array([1.0, 0, 0])
    c = np.array([-5.0, -3.0, 0.2])  # beyond the object, orientation of ABC is clockwise
    f = orient.frame_from_points(a, b, c)
    assert f.axes[2][2] > 0
    g = orient.frame_from_points(a, b, c, flip=True)
    assert g.axes[2][2] < 0
    for fr in (f, g):
        assert np.linalg.det(fr.axes) == pytest.approx(1.0)


def test_collinear_refused():
    with pytest.raises(orient.OrientRefused) as e:
        orient.frame_from_points([0, 0, 0], [1, 0, 0], [2, 0, 0])
    assert e.value.reason == "collinear"
    with pytest.raises(orient.OrientRefused):
        orient.frame_from_points([0, 0, 0], [0, 0, 0])
    for sin, ok in ((5e-4, False), (5e-3, True)):
        c = [0.5, sin, 0]  # AC at angle ~sin from AB, |AB| = |AC| about 1
        c = np.array(c) / np.linalg.norm(c)
        if ok:
            orient.frame_from_points([0, 0, 0], [1, 0, 0], c)
        else:
            with pytest.raises(orient.OrientRefused):
                orient.frame_from_points([0, 0, 0], [1, 0, 0], c)


def test_nan_refused():
    with pytest.raises(orient.OrientRefused) as e:
        orient.frame_from_points([0, 0, np.nan], [1, 0, 0], [0, 1, 0])
    assert e.value.reason == "nan"
    with pytest.raises(orient.OrientRefused):
        orient.frame_from_points([0, 0, 0], [1, np.nan, 0])
    pts, _ = _floor(31, 5, outliers=0)
    pts[0] = np.nan
    f = orient.frame_from_plane(pts, toward=[5, 5, 99])  # 30 finite points left
    assert np.isfinite(f.axes).all()
    pts[1] = np.nan
    with pytest.raises(orient.OrientRefused) as e:
        orient.frame_from_plane(pts, toward=[5, 5, 99])
    assert e.value.reason == "plane_points"


def test_plane_with_nan_a_b_or_toward_refused():
    pts, _ = _floor(60, 5, outliers=0)
    for kw in (
        {"toward": [5, 5, np.nan]},
        {"toward": [5, 5, 99], "a": [np.nan, 0, 0]},
        {"toward": [5, 5, 99], "a": [0, 0, 0], "b": [1, np.inf, 0]},
        {"toward": [5, 5, 99], "b": [np.nan, 0, 0]},
    ):
        with pytest.raises(orient.OrientRefused) as e:
            orient.frame_from_plane(pts, **kw)
        assert e.value.reason == "nan"


def test_degenerate_point_sets_refused():
    rng = np.random.default_rng(5)
    t = rng.uniform(0, 3, 50)
    line = np.c_[t, 2 * t, 0.1 * t]
    same = np.tile([1.0, 2.0, 3.0], (50, 1))
    strip = np.c_[rng.uniform(0, 3, 200), rng.uniform(0, 0.02, 200), rng.normal(0, 0.001, 200)]
    for pts in (line, same, strip):
        with pytest.raises(orient.OrientRefused) as e:
            orient.frame_from_plane(pts, toward=[0, 0, 9])
        assert e.value.reason == "plane_points"


def test_notes_on_replaced_choices():
    pts, n = _floor(100, 5, outliers=0)
    f = orient.frame_from_plane(pts, toward=[5, 5, 99], b=pts[0])
    assert "ignored" in f.note
    a = pts[0]
    f = orient.frame_from_plane(pts, toward=[5, 5, 99], a=a, b=a + n)
    assert "normal" in f.note
    assert orient.frame_from_plane(pts, toward=[5, 5, 99]).note == ""


def _floor(n_points, tilt, outliers=0.1, seed=0):
    rng = np.random.default_rng(seed)
    n = _plane_normal(tilt, 70)
    u = _in_plane(n, 0)
    u /= np.linalg.norm(u)
    v = np.cross(n, u)
    st = rng.uniform(-3, 3, size=(n_points, 2))
    pts = st[:, :1] * u + st[:, 1:] * v + rng.normal(0, 0.001, (n_points, 1)) * n
    k = int(outliers * n_points)
    pts[:k] += rng.uniform(0.05, 0.20, (k, 1)) * n
    return pts + np.array([5.0, 5.0, 1.0]), n


def test_fitted_plane_within_a_tenth_of_a_degree():
    pts, n = _floor(2000, 7.5)
    f = orient.frame_from_plane(pts, toward=pts.mean(0) + [0, 0, 10])
    ang = np.degrees(np.arccos(np.clip(f.axes[2] @ n, -1, 1)))
    assert ang < 0.1


def test_fitted_plane_with_clustered_outliers():
    rng = np.random.default_rng(7)
    n = _plane_normal(7.5, 70)
    u = _in_plane(n, 0)
    u /= np.linalg.norm(u)
    v = np.cross(n, u)
    st = rng.uniform(-3, 3, size=(2000, 2))
    off = rng.normal(0, 0.001, 2000)
    off[(st[:, 0] > 1.5) & (np.arange(2000) % 3 == 0)] += 0.15  # one side of the floor, above it
    pts = st[:, :1] * u + st[:, 1:] * v + off[:, None] * n
    f = orient.frame_from_plane(pts, toward=pts.mean(0) + [0, 0, 10])
    # plain least squares tilts by about half a degree on this data; Huber must stay below 0.05
    assert np.degrees(np.arccos(np.clip(f.axes[2] @ n, -1, 1))) < 0.05


def test_fitted_plane_refusals():
    pts, _ = _floor(29, 5, outliers=0)
    with pytest.raises(orient.OrientRefused) as e:
        orient.frame_from_plane(pts, toward=[0, 0, 9])
    assert e.value.reason == "plane_points"
    rng = np.random.default_rng(3)
    v = rng.normal(size=(500, 3))
    v[:, 2] = np.abs(v[:, 2])
    v /= np.linalg.norm(v, axis=1)[:, None]
    with pytest.raises(orient.OrientRefused) as e:
        orient.frame_from_plane(v, toward=[0, 0, 9])
    assert e.value.reason == "plane_rough"


def test_quota_origin_and_scale():
    n = _plane_normal(12, 40)
    a = np.array([3.0, 1.0, 2.0])
    b = a + 3.0 * _in_plane(n, 35)
    c = a + 2.0 * _in_plane(n, 130)
    f = orient.frame_from_points(a, b, c)
    m = orient.motion(f, quota=10, origin_xy=(100, 200), scale=2)
    assert np.allclose(geo.apply(m, a), [100, 200, 10], atol=1e-9)
    ab = np.linalg.norm(geo.apply(m, b) - geo.apply(m, a))
    assert ab == pytest.approx(2 * np.linalg.norm(b - a))
    assert orient.describe(m)["scale"] == pytest.approx(2)
    assert orient.scale_for(a, b, np.linalg.norm(b - a) * 2) == pytest.approx(2.0)
    with pytest.raises(ValueError):
        orient.scale_for(a, b, 0)


# ---------------------------------------------------------------- a motion applied to a chunk and what it keeps

def _internal(transform, xyz):
    return geo.apply(np.linalg.inv(np.asarray(transform, float)), np.asarray(xyz, float).reshape(-1, 3))


def _same_inside(old_t, old_xyz, new_t, new_xyz, tol=1e-6):
    got, want = _internal(new_t, new_xyz), _internal(old_t, old_xyz)
    assert np.abs(got - want).max() < tol, np.abs(got - want).max()


def _bare(tmp_path, crs="LOCAL", label="A"):
    """A saved document with one chunk and nothing in it but its transform (fast: no processing)."""
    import pyarchinit_photogrammetry as ppg

    doc = ppg.Document()
    ch = doc.add_chunk(label)
    ch.crs = crs
    ch.transform = np.array(motion_of(rotation_of([0, 0, 1], 20), (5.0, -3.0, 2.0)))
    doc.save(tmp_path / "bare.ppg")
    return doc, ch


def rotation_of(axis, deg):
    from orient_helpers import rotation

    return rotation(axis, deg)


def motion_of(r=None, t=(0.0, 0.0, 0.0), s=1.0):
    from orient_helpers import motion

    return motion(r, t, s)


def _typed(ch, n, checkpoint=False):
    from pyarchinit_photogrammetry.core.camera import Reference

    out = []
    for i in range(n):
        m = ch.add_marker(f"M{i}")
        m.position = np.array([float(i), 2.0 * i, 0.5])
        m.reference = Reference(location=[10.0 + i, 20.0 - i, 3.0 + 0.1 * i], accuracy=[0.01] * 3)
        m.checkpoint = checkpoint
        out.append(m)
    return out


def test_every_store_follows_the_motion(processed, tmp_path):
    import json

    import shapely
    from orient_helpers import dem_at, drawn, local_copy, tilt_motion

    from pyarchinit_photogrammetry.engine import elevation
    from pyarchinit_photogrammetry.engine.region import Region
    from pyarchinit_photogrammetry.io.mesh_ply import read_mesh

    doc, ch, _t_bad = local_copy(processed, tmp_path)
    made = drawn(ch)
    t_old = ch.transform.copy()
    before = json.loads(json.dumps(ch.meta))
    markers = {m.label: list(m.reference.location) for m in ch.markers if m.reference.location}
    ply = ch.data_dir / made["proxy"]["file"]
    ply_old = read_mesh(ply).xyz
    sections_old = json.loads((doc.path.parent / "sezioni" / "sezioni.json").read_text())
    m = tilt_motion((7.0, 200.0), 50.0, (-4.0, 6.0, 0.7))
    result = orient.apply(ch, m)
    t_new = ch.transform
    assert np.allclose(t_new, m @ t_old, atol=1e-12)
    assert ch.meta["levelled"] is True
    d0, d1 = before["drawings"], ch.meta["drawings"]
    # 3D items: the same point of the model
    q0, q1 = d0["quote"][0], d1["quote"][0]
    _same_inside(t_old, [q0["x"], q0["y"], q0["quota_q"]], t_new, [q1["x"], q1["y"], q1["quota_q"]])
    p0, p1 = d0["points"][0], d1["points"][0]
    _same_inside(t_old, [p0["x"], p0["y"], p0["z"]], t_new, [p1["x"], p1["y"], p1["z"]])
    _same_inside(t_old, d0["free"][0]["xyz"], t_new, d1["free"][0]["xyz"])
    a0, a1 = d0["lines"][0]["coords"], d1["lines"][0]["coords"]
    _same_inside(t_old, a0[0], t_new, a1[0])
    assert a1[1][2] is None  # without a height again; its plan where the ground there goes
    ground = dem_at(ch, a0[1][0], a0[1][1])
    assert np.allclose(a1[1][:2], geo.apply(m, np.array([[a0[1][0], a0[1][1], ground]]))[0, :2], atol=1e-9)
    assert made["markers"]
    for label in made["markers"]:
        mk = ch.marker(label)
        _same_inside(t_old, markers[label], t_new, mk.reference.location)
    # plan units: their vertices at the height of the DEM there, moved, the height dropped
    xy0 = np.asarray(shapely.from_wkt(d0["units"][0]["wkt"]).exterior.coords)
    xy1 = np.asarray(shapely.from_wkt(d1["units"][0]["wkt"]).exterior.coords)
    want = geo.apply(m, np.column_stack([xy0, [dem_at(ch, x, y) for x, y in xy0]]))[:, :2]
    assert np.abs(xy1 - want).max() < 1e-9
    ortho = [k for k in before["stones"] if k != "dem_locale.tif"][0]
    for key in (ortho, "dem_locale.tif"):  # the stones of every raster of the plan
        assert before["stones"][key] != ch.meta["stones"][key]
    assert before["orthomosaic"]["patches"][0]["polygon"] != ch.meta["orthomosaic"]["patches"][0]["polygon"]
    assert before["texture"]["patches"][0]["polygon"] != ch.meta["texture"]["patches"][0]["polygon"]
    assert before["colour_chart"]["bounds"] != ch.meta["colour_chart"]["bounds"]
    r0, r1 = Region.from_dict(before["region"]), Region.from_dict(ch.meta["region"])
    _same_inside(t_old, r0.corners(), t_new, r1.corners())
    # the section of the project and the elevation: the frame follows, the drawings kept in (u, v)
    sections_new = json.loads((doc.path.parent / "sezioni" / "sezioni.json").read_text())
    assert sections_new[0]["p1"] != sections_old[0]["p1"]
    e0 = made["plane"]
    e1 = [e for e in ch.meta["elevations"] if e["id"] == e0["id"]][0]
    f0, f1 = d0["frames"][e0["id"]], d1["frames"][e0["id"]]
    assert e1["v_is_height"] is False  # tilted: v is no longer the height
    for (u0, v0), (u1, v1) in zip(shapely.from_wkt(f0["units"][0]["wkt"]).exterior.coords,
                                  shapely.from_wkt(f1["units"][0]["wkt"]).exterior.coords):
        for dd in (0.0, -0.2):
            _same_inside(t_old, elevation.world_of(e0, u0, v0, dd), t_new, elevation.world_of(e1, u1, v1, dd),
                         tol=1e-9)
    fq0, fq1 = f0["quote"][0], f1["quote"][0]
    _same_inside(t_old, [fq0["x"], fq0["y"], fq0["quota_q"]], t_new, [fq1["x"], fq1["y"], fq1["quota_q"]])
    saved = json.loads((ch.data_dir / "elevations" / e0["id"] / "frame.json").read_text())
    assert saved["frame"] == e1["frame"] and saved["bounds"] == e1["bounds"]
    import rasterio

    with rasterio.open(ch.data_dir / e1["relief"]) as src:
        assert json.loads(src.tags()["ppg_frame"]) == e1["frame"]
        assert np.isclose(src.transform.f, e1["bounds"][3])
    # the proxy: its PLY and its record
    _same_inside(t_old, ply_old, t_new, read_mesh(ply).xyz)
    p1 = ch.meta["proxies"][0]
    assert p1["volume_m3"] == pytest.approx(made["proxy"]["volume_m3"], rel=1e-9)
    # what the window says
    moved = result["moved"]
    for store in ("units", "quote", "points", "lines", "free", "stones", "patches", "texture_patches",
                  "colour_chart", "region", "elevations", "sections", "proxies", "markers"):
        assert moved.get(store), (store, moved)
    assert {s["product"] for s in result["stale"]} >= {"dem", "orthomosaic"}
    assert not any(s["kind"] == "elevation" for s in result["stale"])  # moved with it: current
    whats = {k["what"] for k in result["kept"]}
    assert "section_drawings" in whats and "vertical_sections" in whats
    sent = {s["what"]: s["names"] for s in result["sent_out"]}
    assert sent["pyarchinit"] and "qfield" in sent and "exports" in sent
    orient.drop(result["state"])


def test_elevation_heights_with_a_turn_about_z(processed, tmp_path):
    import json

    import shapely
    from orient_helpers import centre, local_copy, plane_elevation

    from pyarchinit_photogrammetry.engine import elevation

    doc, ch, _ = local_copy(processed, tmp_path)
    e0 = plane_elevation(ch, centre(ch))
    v = e0["bounds"][1] + 0.5
    patch = {"id": "p1", "polygon": [[0.5, v], [1.5, v], [1.0, v + 0.4]], "photos": [], "mode": "assign",
             "surface": "dem"}
    e0["patches"], e0["applied_patches"] = [dict(patch)], [json.loads(json.dumps(patch))]
    e0 = json.loads(json.dumps(e0))
    f0 = json.loads(json.dumps(ch.meta["drawings"]["frames"][e0["id"]]))
    t_old = ch.transform.copy()
    m = motion_of(rotation_of([0, 0, 1], 30.0), (0.0, 0.0, 2.0))
    orient.drop(orient.apply(ch, m)["state"])
    e1 = ch.meta["elevations"][0]
    f1 = ch.meta["drawings"]["frames"][e0["id"]]
    frame = elevation.frame_from_dict(e1["frame"])
    assert e1["v_is_height"] is True and elevation.heights_measured(frame)
    shifts = []
    for (u0, v0), (u1, v1) in zip(shapely.from_wkt(f0["units"][0]["wkt"]).exterior.coords,
                                  shapely.from_wkt(f1["units"][0]["wkt"]).exterior.coords):
        assert u1 == pytest.approx(u0, abs=1e-12)
        shifts.append(v1 - v0)
        _same_inside(t_old, elevation.world_of(e0, u0, v0, 0.1), ch.transform, elevation.world_of(e1, u1, v1, 0.1),
                     tol=1e-9)
    shifts += [f1["lines"][0]["coords"][0][1] - f0["lines"][0]["coords"][0][1],
               f1["quote"][0]["v"] - f0["quote"][0]["v"], e1["bounds"][1] - e0["bounds"][1]]
    for key in ("patches", "applied_patches"):  # the patches of the elevation with its (u, v)
        for (u0, v0), (u1, v1) in zip(e0[key][0]["polygon"], e1[key][0]["polygon"]):
            shifts.append(v1 - v0)
            _same_inside(t_old, elevation.world_of(e0, u0, v0, 0.0), ch.transform,
                         elevation.world_of(e1, u1, v1, 0.0), tol=1e-9)
    assert np.allclose(shifts, 2.0, atol=1e-9)
    assert f1["quote"][0]["quota_q"] == pytest.approx(f0["quote"][0]["quota_q"] + 2.0, abs=1e-9)


def test_cylinder_frame_follows(processed, tmp_path):
    from orient_helpers import centre, cylinder_elevation, local_copy, tilt_motion

    from pyarchinit_photogrammetry.engine import elevation

    doc, ch, _ = local_copy(processed, tmp_path)
    e0 = cylinder_elevation(ch, centre(ch))
    t_old = ch.transform.copy()
    m = tilt_motion((9.0, 120.0), 70.0, (1.0, 2.0, -0.5))
    orient.drop(orient.apply(ch, m)["state"])
    e1 = ch.meta["elevations"][0]
    frame = elevation.frame_from_dict(e1["frame"])
    assert frame.kind == "cylinder" and frame.u_map is not None
    assert e1["tilt"] == e1["frame"]["tilt"]
    assert np.allclose(e1["frame"]["tilt"]["centre"], geo.apply(m, np.array([e0["frame"]["tilt"]["centre"]], float))[0])
    axis = np.asarray(e1["frame"]["axis"])
    assert e1["frame"]["tilt"]["tilt_deg"] == pytest.approx(np.degrees(np.arccos(axis[2])), abs=1e-9)
    for u, v, d in ((0.5, 0.2, 0.0), (3.0, -0.5, 0.1), (8.2, 0.7, -0.2)):
        _same_inside(t_old, elevation.world_of(e0, u, v, d), ch.transform, elevation.world_of(e1, u, v, d),
                     tol=1e-9)


def test_aligned_chunks_move_with_it(tmp_path):
    doc, ch = _bare(tmp_path)
    other = doc.add_chunk("B")
    other.transform = motion_of(rotation_of([1, 1, 0], 3), (0.2, 0.1, -0.3))
    other.meta["chunk_alignment"] = {"reference": ch.label, "reference_key": ch.key, "method": "markers"}
    assert orient.aligned_to(ch) == [other]
    t2 = other.transform.copy()
    m = motion_of(rotation_of([0, 1, 0], 10), (1.0, 2.0, 3.0))
    result = orient.apply(ch, m)
    assert np.abs(other.transform - m @ t2).max() < 1e-12
    assert result["aligned"] == ["B"] and result["aligned_left"] == []
    orient.put_back(result["state"])
    assert np.array_equal(other.transform, t2)
    result = orient.apply(ch, m, move_aligned=False)
    assert np.array_equal(other.transform, t2)
    assert result["aligned"] == [] and result["aligned_left"] == ["B"]
    with pytest.raises(orient.OrientRefused) as e:
        orient.check(other)
    assert e.value.reason == "aligned_to" and e.value.names == ["A"]


def test_sections_kept_with_another_local_chunk(tmp_path):
    import json

    doc, ch = _bare(tmp_path)
    doc.add_chunk("altro")  # LOCAL too, not aligned to this one
    folder = doc.path.parent / "sezioni"
    folder.mkdir()
    text = json.dumps([{"label": "S1", "p1": [0.0, 0.0], "p2": [2.0, 1.0], "length": 2.236, "kind": "vertical"}])
    (folder / "sezioni.json").write_text(text, encoding="utf-8")
    result = orient.apply(ch, motion_of(rotation_of([0, 0, 1], 15), (1.0, 0.0, 0.0)))
    assert (folder / "sezioni.json").read_text(encoding="utf-8") == text
    assert {"what": "sections", "names": ["S1"]} in result["kept"]
    assert not result["moved"].get("sections")


def test_refusals(tmp_path):
    doc, ch = _bare(tmp_path, crs="EPSG:3004")
    with pytest.raises(orient.OrientRefused) as e:
        orient.apply(ch, np.eye(4))
    assert e.value.reason == "crs"
    (tmp_path / "b").mkdir()
    doc, ch = _bare(tmp_path / "b")
    _typed(ch, 3)
    _typed(ch, 2, checkpoint=True)  # check markers do not give the orientation
    with pytest.raises(orient.OrientRefused) as e:
        orient.check(ch)
    assert e.value.reason == "markers" and e.value.names == ["M0", "M1", "M2"]
    # two control markers: their typed references converted by M
    (tmp_path / "c").mkdir()
    doc, ch = _bare(tmp_path / "c")
    typed = _typed(ch, 2)
    before = [list(mk.reference.location) for mk in typed]
    m = motion_of(rotation_of([1, 0, 0], 8), (0.5, -1.0, 2.0))
    result = orient.apply(ch, m)
    for mk, loc in zip(typed, before):
        assert np.allclose(mk.reference.location, geo.apply(m, np.array([loc], float))[0], atol=1e-12)
    assert result["moved"]["markers"] == 2
    # aligned to another chunk: the reference is to be oriented
    other = doc.add_chunk("B")
    other.meta["chunk_alignment"] = {"reference": ch.label, "reference_key": ch.key}
    with pytest.raises(orient.OrientRefused) as e:
        orient.apply(other, np.eye(4))
    assert e.value.reason == "aligned_to" and e.value.names == [ch.label]


def _project_bytes(doc):
    import json

    return json.dumps([c.to_dict() for c in doc.chunks])


def test_put_back_restores_byte_for_byte(processed, tmp_path):
    from orient_helpers import drawn, local_copy, tilt_motion

    doc, ch, _ = local_copy(processed, tmp_path)
    made = drawn(ch)
    sections = doc.path.parent / "sezioni" / "sezioni.json"
    frames = sorted((ch.data_dir / "elevations").glob("*/frame.json"))
    ply = ch.data_dir / made["proxy"]["file"]
    before = (_project_bytes(doc), sections.read_bytes(), [f.read_bytes() for f in frames], ply.read_bytes())
    t_object = ch.transform
    result = orient.apply(ch, tilt_motion((5.0, 10.0), 20.0))
    assert _project_bytes(doc) != before[0] and ply.read_bytes() != before[3]
    orient.put_back(result["state"])
    after = (_project_bytes(doc), sections.read_bytes(), [f.read_bytes() for f in frames], ply.read_bytes())
    assert after == before
    assert ch.transform is t_object  # the same array: who holds it sees the old values
    clones = list(result["state"].folders)
    assert clones and all(p.exists() for p in clones)
    orient.drop(result["state"])
    assert not any(p.exists() for p in clones)


def _files_state(doc, ch, made):
    """The bytes of the sections, the elevations' frame.json, the proxy PLY, and the tags and grids of the
    elevations' rasters."""
    import rasterio

    sections = doc.path.parent / "sezioni" / "sezioni.json"
    frames = sorted((ch.data_dir / "elevations").glob("*/frame.json"))
    rasters = []
    for path in sorted((ch.data_dir / "elevations").glob("*/*.tif")):
        with rasterio.open(path) as src:
            rasters.append((dict(src.tags()), tuple(src.transform)))
    return (sections.read_bytes(), [f.read_bytes() for f in frames], (ch.data_dir / made["proxy"]["file"]).read_bytes(),
            rasters)


def test_failure_puts_everything_back(processed, tmp_path, monkeypatch):
    from orient_helpers import drawn, local_copy, tilt_motion

    doc, ch, _ = local_copy(processed, tmp_path)
    made = drawn(ch)
    before, files = _project_bytes(doc), _files_state(doc, ch, made)

    def broken(*_a, **_k):
        raise RuntimeError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(orient, "_write_proxies", broken)
        with pytest.raises(RuntimeError):
            orient.apply(ch, tilt_motion())
    assert _project_bytes(doc) == before and _files_state(doc, ch, made) == files
    # a failure on the second chunk (one aligned to it): the first, already moved, comes back too
    other = doc.add_chunk("B")
    other.meta["chunk_alignment"] = {"reference": ch.label, "reference_key": ch.key}
    before = _project_bytes(doc)
    real = orient.move_stores

    def second_fails(chunk, m, sections=None):
        if chunk is other:
            raise RuntimeError("second chunk")
        return real(chunk, m, sections)

    monkeypatch.setattr(orient, "move_stores", second_fails)
    with pytest.raises(RuntimeError, match="second chunk"):
        orient.apply(ch, tilt_motion())
    assert _project_bytes(doc) == before and _files_state(doc, ch, made) == files
    assert not list((ch.data_dir / "undo").glob("orient-*"))


def test_listener_and_unaware_plugin_keys(tmp_path):
    doc, ch = _bare(tmp_path)
    ch.meta["shipwreck"] = {"frame": [1, 2, 3]}
    m = motion_of(rotation_of([0, 0, 1], 5))
    calls = []
    result = orient.apply(ch, m)
    assert result["plugins_unaware"] == ["shipwreck"]
    orient.put_back(result["state"])
    stop = orient.on_transform_changed(lambda c, mm: calls.append((c, mm)), keys=("shipwreck",))
    try:
        result = orient.apply(ch, m)
    finally:
        stop()
    assert len(calls) == 1 and calls[0][0] is ch and np.array_equal(calls[0][1], m)
    assert result["plugins_unaware"] == []
    orient.apply(ch, m)
    assert len(calls) == 1  # unregistered


def test_chunks_aligned_to_an_aligned_chunk_move_too(tmp_path):
    doc, a = _bare(tmp_path)
    b, c = doc.add_chunk("B"), doc.add_chunk("C")
    b.meta["chunk_alignment"] = {"reference": "A", "reference_key": a.key}
    c.meta["chunk_alignment"] = {"reference": "B", "reference_key": b.key}
    assert orient.aligned_to(a) == [b, c]
    tb, tc = b.transform.copy(), c.transform.copy()
    m = motion_of(rotation_of([0, 0, 1], 12), (1.0, 0.0, 0.5))
    result = orient.apply(a, m)
    assert result["aligned"] == ["B", "C"]
    assert np.abs(b.transform - m @ tb).max() < 1e-12 and np.abs(c.transform - m @ tc).max() < 1e-12
    orient.put_back(result["state"])
    result = orient.apply(a, m, move_aligned=False)
    assert result["aligned_left"] == ["B", "C"] and np.array_equal(c.transform, tc)
    b.meta["chunk_alignment"]["reference_key"] = c.key  # a cycle B <- C <- B: each chunk once
    a2 = orient.aligned_to(c)
    assert a2 == [b]


def test_photo_positions_that_place_the_chunk_are_refused(tmp_path):
    from pyarchinit_photogrammetry.core.camera import Camera, Reference
    from pyarchinit_photogrammetry.core.marker import ScaleBar

    doc, ch = _bare(tmp_path)
    for k in range(3):
        cam = Camera(key=k, label=f"c{k}", path=f"c{k}.jpg", sensor_key=0)
        cam.transform = np.eye(4)
        cam.transform[:3, 3] = [k * 5.0, k * 2.0, 10.0]
        cam.reference = Reference(location=[100.0 + 5 * k, 50.0 + 2 * k, 10.0], accuracy=[0.05] * 3)
        ch.cameras.append(cam)
    with pytest.raises(orient.OrientRefused) as e:
        orient.check(ch)
    assert e.value.reason == "cameras" and e.value.names == ["c0", "c1", "c2"]
    for cam in ch.cameras:  # a real system (GPS kept in a shifted LOCAL chunk): refused too
        cam.meta["reference_crs"] = "EPSG:4326"
    with pytest.raises(orient.OrientRefused) as e:
        orient.check(ch)
    assert e.value.reason == "cameras"
    ch.cameras[0].reference.enabled = False  # two left: they do not fit the transform
    orient.check(ch)
    ch.cameras[0].reference.enabled = True
    _typed(ch, 1)  # a control marker with a typed reference: the photos are left out of the fit
    orient.check(ch)
    # a scale with control scale bars: the bars give it
    a, b = ch.markers[0], ch.add_marker("N")
    ch.scalebars.append(ScaleBar(key=0, label="A-B", marker_a=a.key, marker_b=b.key, distance=1.0))
    with pytest.raises(orient.OrientRefused) as e:
        orient.apply(ch, motion_of(s=2.0))
    assert e.value.reason == "scalebars"
    orient.drop(orient.apply(ch, motion_of(rotation_of([0, 0, 1], 10)))["state"])  # rigid: allowed


def test_unstamped_elevation_is_stale_after_a_scale(processed, tmp_path):
    from orient_helpers import centre, local_copy, plane_elevation

    doc, ch, _ = local_copy(processed, tmp_path)
    entry = plane_elevation(ch, centre(ch))
    entry.pop("built_in")
    ch.meta.pop("built_in", None)
    result = orient.apply(ch, motion_of(s=2.0))
    stale = {(x["kind"], x["id"]) for x in result["stale"]}
    assert ("elevation", entry["id"]) in stale
    orient.put_back(result["state"])
    orient.drop(result["state"])
    result = orient.apply(ch, motion_of(rotation_of([0, 0, 1], 10)))  # rigid: moved with it, current
    assert not any(x["kind"] == "elevation" for x in result["stale"])
    orient.drop(result["state"])


# keys written on objects that are not chunks (photos, the document)
NOT_CHUNK_KEYS = {"active_chunk", "sito", "bands", "capture", "reference_crs", "rolling_shutter"}


def test_every_chunk_meta_key_of_the_program_is_known():
    import re
    from pathlib import Path

    import pyarchinit_photogrammetry

    root = Path(pyarchinit_photogrammetry.__file__).parent
    pattern = re.compile(r'\b(\w+)\.meta(?:\["(\w+)"\]\s*=(?!=)|\.setdefault\("(\w+)"|\.get\("(\w+)")')
    unknown = {}
    for path in root.rglob("*.py"):
        for m in pattern.finditer(path.read_text(encoding="utf-8")):
            if m.group(4) and m.group(1) not in ("chunk", "ch"):
                continue
            key = m.group(2) or m.group(3) or m.group(4)
            if key not in orient.PROGRAM_KEYS and key not in NOT_CHUNK_KEYS:
                unknown.setdefault(key, []).append(str(path.relative_to(root)))
    assert not unknown, unknown


# ---------------------------------------------------------------- A4: orientation markers, the typed scale, survival

def _surface_points(ch, n=3):
    """Three points of the dense cloud (internal frame) spread over the trench: two along its long side, one across."""
    xyz = np.asarray(ch.point_cloud.xyz, float)
    c = np.median(xyz, axis=0)
    _, _, vt = np.linalg.svd(xyz[::50] - c, full_matrices=False)
    span = np.ptp((xyz - c) @ vt[:2].T, axis=0)
    targets = [c - 0.3 * span[0] * vt[0], c + 0.3 * span[0] * vt[0], c + 0.3 * span[1] * vt[1]]
    return np.array([xyz[np.argmin(np.linalg.norm(xyz - t, axis=1))] for t in targets[:n]])


def _oriented(processed, tmp_path, **kw):
    from orient_helpers import local_copy

    doc, ch, t_bad = local_copy(processed, tmp_path)
    t0 = np.linalg.inv(t_bad) @ np.asarray(ch.transform, float)
    internal = _surface_points(ch)
    points = ch.to_world(internal)
    result = orient.orient(ch, points=points, **kw)
    return doc, ch, t0, internal, points, result


def _orientation_markers(ch):
    return [m for m in ch.markers if m.orientation]


def test_orient_from_three_points(processed, tmp_path):
    doc, ch, t0, internal, points, result = _oriented(processed, tmp_path)
    want = orient.motion(orient.frame_from_points(*geo.apply(t0, internal))) @ t0
    assert np.abs(np.asarray(ch.transform) - want).max() < 1e-6
    stale = {x["product"] for x in result["stale"]}
    assert {"dem", "orthomosaic"} <= stale, result["stale"]
    marks = _orientation_markers(ch)
    assert [m.label for m in marks] == list(orient.MARKER_LABELS)
    ab = float(np.linalg.norm(points[1] - points[0]))
    a, b, c = (np.asarray(m.reference.location, float) for m in marks)
    assert np.allclose(a, 0, atol=1e-9) and np.allclose(b, [ab, 0, 0], atol=1e-9) and abs(c[2]) < 1e-9
    for m, p in zip(marks, points):  # the references are the picked points carried by the motion
        assert np.allclose(m.reference.location, geo.apply(np.asarray(result["M"]), p[None])[0], atol=1e-9)
        assert np.allclose(m.reference.accuracy, [orient.MARKER_ACCURACY] * 3)
        assert sum(not p.measured for p in m.projections.values()) >= 2
    assert ch.meta["orientation"]["method"] == "points"
    assert ch.meta["orientation"]["markers"] == [m.key for m in marks]
    # the flag goes through the file and nothing is written for the other markers
    assert all("orientation" not in m.to_dict() for m in ch.markers if not m.orientation)
    from pyarchinit_photogrammetry.core.marker import Marker

    assert Marker.from_dict(marks[0].to_dict()).orientation is True


def test_orientation_survives_a_new_alignment(processed, tmp_path):
    from orient_helpers import new_alignment

    doc, ch, t0, internal, points, result = _oriented(processed, tmp_path)
    oriented = np.array(ch.transform, float)
    marks = _orientation_markers(ch)
    before = [np.array(m.position, float) for m in marks]
    g = new_alignment(ch)  # the cameras and tie points in another internal frame, transform = I
    for m in marks:
        assert sum(not p.measured for p in m.projections.values()) >= 2
        m.position = None
    report = ch.update_transform()
    for m, p in zip(marks, before):  # triangulated in the new frame
        assert np.linalg.norm(m.position - geo.apply(g, p[None])[0]) < 1e-3
    assert np.abs(np.asarray(ch.transform) - oriented @ np.linalg.inv(g)).max() < 1e-6
    assert ch.meta.get("levelled") is True
    assert "+orientation" in report["method"]


def test_scale_bars_alone_keep_the_translation(processed, tmp_path):
    from pyarchinit_photogrammetry.markers import gcp

    doc, ch, t0, internal, points, result = _oriented(processed, tmp_path)
    marks = _orientation_markers(ch)
    a = marks[0]
    for m in marks[1:]:  # two orientation markers unplaced: the markers no longer fit the chunk
        m.position, m.projections = None, {}
    others = [m for m in ch.markers if not m.orientation and m.position is not None][:2]
    length = float(np.linalg.norm(ch.to_world(others[0].position[None])[0] - ch.to_world(others[1].position[None])[0]))
    ch.add_scalebar(others[0], others[1], length, 0.001)
    t_before = np.array(ch.transform, float)
    report = ch.update_transform()
    assert report["method"] == "scalebars+orientation"
    assert np.abs(np.asarray(ch.transform)[:3, 3] - t_before[:3, 3]).max() < 1e-9
    assert ch.scale == pytest.approx(gcp._bar_scale(ch, gcp.control_scalebars(ch)), rel=1e-12)
    # a bar that asks for 1.2x the scale: rotation kept, A (the origin) stays where it is
    ch.add_scalebar(others[0], others[1], 1.2 * length, 0.001)
    a_world = ch.to_world(a.position[None])[0]
    _, rot, _ = geo.decompose_similarity(ch.transform)
    report = ch.update_transform()
    assert report["method"] == "scalebars+orientation"
    s, rot2, _ = geo.decompose_similarity(ch.transform)
    assert s == pytest.approx(gcp._bar_scale(ch, gcp.control_scalebars(ch)), rel=1e-12)
    assert s == pytest.approx(1.2 * t_scale(t_before), rel=1e-9)
    assert np.allclose(rot2, rot, atol=1e-12)
    assert np.allclose(ch.to_world(a.position[None])[0], a_world, atol=1e-9)
    assert np.allclose(a_world, a.reference.location, atol=1e-9)
    # A renamed: still the anchor (its role, not its label)
    a.label = "origine"
    ch.add_scalebar(others[0], others[1], 1.3 * length, 0.001)
    ch.update_transform()
    assert np.allclose(ch.to_world(a.position[None])[0], a_world, atol=1e-9)


def t_scale(t):
    return geo.decompose_similarity(np.asarray(t, float))[0]


def test_new_bar_rescales_about_a(processed, tmp_path):
    from pyarchinit_photogrammetry.markers import gcp

    doc, ch, t0, internal, points, result = _oriented(processed, tmp_path)
    a, b = _orientation_markers(ch)[:2]
    ab = float(np.linalg.norm(points[1] - points[0]))
    ch.add_scalebar(a, b, 1.1 * ab, 0.001)
    report = ch.update_transform()
    assert "+orientation" in report["method"] and "scalebars" in report["method"]
    assert np.allclose(ch.to_world(a.position[None])[0], a.reference.location, atol=1e-9)
    assert ch.scale == pytest.approx(gcp._bar_scale(ch, gcp.control_scalebars(ch)), rel=1e-12)
    assert ch.scale == pytest.approx(1.1 * t_scale(t0), rel=1e-6)


def test_typed_distance(processed, tmp_path):
    doc, ch, t0, internal, points, result = _oriented(processed, tmp_path, distance=2.5)
    a, b, _c = _orientation_markers(ch)
    assert np.linalg.norm(ch.to_world(a.position[None])[0] - ch.to_world(b.position[None])[0]) == pytest.approx(
        2.5, abs=1e-9)
    bar = ch.scalebar(orient.BAR_LABEL)
    assert {bar.marker_a, bar.marker_b} == {a.key, b.key} and bar.distance == 2.5 and not bar.checkpoint
    assert bar.accuracy == orient.BAR_ACCURACY
    assert np.allclose(b.reference.location, [2.5, 0, 0], atol=1e-9)
    # a new alignment keeps the typed scale (the bar) and the frame
    oriented = np.array(ch.transform, float)
    ch.transform = np.eye(4)
    for m in (a, b, _c):
        m.position = None
    ch.update_transform()
    assert np.abs(np.asarray(ch.transform) - oriented).max() < 1e-6
    # with a control bar already: refused
    (tmp_path / "x").mkdir()
    doc, ch, t0, internal, points, result = _oriented(processed, tmp_path / "x")
    others = [m for m in ch.markers if not m.orientation and m.position is not None][:2]
    ch.add_scalebar(others[0], others[1], 1.0, 0.001)
    with pytest.raises(orient.OrientRefused) as e:
        orient.orient(ch, points=points, distance=2.5)
    assert e.value.reason == "scalebars"


def test_reorient_replaces_the_markers(processed, tmp_path):
    doc, ch, t0, internal, points, result = _oriented(processed, tmp_path, distance=2.0)
    again = orient.orient(ch, points=ch.to_world(internal), distance=3.0)  # its own bar does not refuse
    marks = _orientation_markers(ch)
    assert len(marks) == 3 and [m.key for m in marks] == again["markers"]
    assert len([sb for sb in ch.scalebars if sb.label == orient.BAR_LABEL]) == 1
    assert ch.scalebar(orient.BAR_LABEL).distance == 3.0
    orient.orient(ch, points=ch.to_world(internal))  # no distance: the typed bar kept on the new A and B
    marks = _orientation_markers(ch)
    bars = [sb for sb in ch.scalebars if sb.label == orient.BAR_LABEL]
    assert len(bars) == 1 and bars[0].distance == 3.0 and bars[0].accuracy == orient.BAR_ACCURACY
    assert {bars[0].marker_a, bars[0].marker_b} == {marks[0].key, marks[1].key}
    assert again.get("bar_dropped") is None
    # other points: the typed distance was of the earlier A and B, dropped and said
    other = ch.to_world(internal[[2, 0, 1]])
    result = orient.orient(ch, points=other)
    assert not [sb for sb in ch.scalebars if sb.label == orient.BAR_LABEL]
    assert result["bar_dropped"] == {"distance": 3.0, "accuracy": orient.BAR_ACCURACY}
    oriented = np.array(ch.transform, float)
    ch.update_transform()
    assert np.abs(np.asarray(ch.transform) - oriented).max() < 1e-6


def test_motion_from_a_plugin(processed, tmp_path):
    from orient_helpers import local_copy, tilt_motion

    doc, ch, _ = local_copy(processed, tmp_path)
    t = np.array(ch.transform, float)
    m = tilt_motion((4.0, 100.0), 15.0, (1.0, 2.0, -0.5))
    result = ch.orient(M=m)
    assert np.abs(np.asarray(ch.transform) - m @ t).max() < 1e-12
    marks = _orientation_markers(ch)
    assert len(marks) == 3 and ch.meta["orientation"]["method"] == "motion"
    for mk in marks:  # references = where the points are now
        assert np.allclose(ch.to_world(mk.position[None])[0], mk.reference.location, atol=1e-9)
    assert result["describe"]["tilt_deg"] == pytest.approx(4.0, abs=1e-9)


def test_dry_run_changes_nothing(processed, tmp_path):
    from orient_helpers import drawn, local_copy

    doc, ch, _ = local_copy(processed, tmp_path)
    drawn(ch)
    internal = _surface_points(ch)
    before = _project_bytes(doc)
    result = orient.orient(ch, points=ch.to_world(internal), distance=2.0, dry_run=True)
    assert _project_bytes(doc) == before
    assert "state" not in result and "markers" not in result
    assert {"tilt_deg", "tilt_direction_deg", "heading_deg"} <= set(result["describe"])
    assert result["stale"]


def test_undo_puts_the_earlier_orientation_back(processed, tmp_path):
    doc, ch, t0, internal, points, first = _oriented(processed, tmp_path, distance=2.0)
    before = _project_bytes(doc)
    second = orient.orient(ch, points=ch.to_world(internal), quota=10.0)
    assert _project_bytes(doc) != before
    orient.undo(ch, second)
    assert _project_bytes(doc) == before
    assert [m.key for m in _orientation_markers(ch)] == first["markers"]


def _referenced_photos(ch, n=4):
    from pyarchinit_photogrammetry.core.camera import Reference

    cams = ch.aligned_cameras[:n]
    for cam in cams:
        cam.reference = Reference(location=list(ch.to_world(cam.center[None])[0]), accuracy=[0.01] * 3)
    return [c.label for c in cams]


def test_orient_replaces_the_photo_positions(processed, tmp_path):
    from orient_helpers import local_copy

    doc, ch, _ = local_copy(processed, tmp_path)
    labels = _referenced_photos(ch)
    with pytest.raises(orient.OrientRefused) as e:  # apply alone keeps the refusal
        orient.apply(ch, np.eye(4))
    assert e.value.reason == "cameras"
    internal = _surface_points(ch)
    before = _project_bytes(doc)
    dry = orient.orient(ch, points=ch.to_world(internal), dry_run=True)
    assert sorted(dry["cameras_replaced"]) == sorted(labels) and _project_bytes(doc) == before
    result = orient.orient(ch, points=ch.to_world(internal))
    assert sorted(result["cameras_replaced"]) == sorted(labels)
    oriented = np.array(ch.transform, float)
    report = ch.update_transform()
    assert "cameras" not in report["method"]
    assert np.abs(np.asarray(ch.transform) - oriented).max() < 1e-6
    (tmp_path / "n").mkdir()
    doc, ch, _ = local_copy(processed, tmp_path / "n")
    assert orient.orient(ch, points=ch.to_world(_surface_points(ch)))["cameras_replaced"] == []


def test_weak_markers_are_said(processed, tmp_path, monkeypatch):
    from orient_helpers import local_copy

    from pyarchinit_photogrammetry.markers import gcp

    doc, ch, _ = local_copy(processed, tmp_path)
    real = gcp.project_marker

    def one_photo(chunk, marker, point=None, max_photos=60):
        return real(chunk, marker, point, max_photos=1 if marker.label == orient.MARKER_LABELS[2] else max_photos)

    monkeypatch.setattr(gcp, "project_marker", one_photo)
    t = np.array(ch.transform, float)
    result = orient.orient(ch, points=ch.to_world(_surface_points(ch)))
    assert result["weak_markers"] == [orient.MARKER_LABELS[2]]
    assert np.abs(np.asarray(ch.transform) - np.asarray(result["M"]) @ t).max() < 1e-12


def test_cloud_sample_is_bounded(processed, tmp_path, monkeypatch):
    from orient_helpers import local_copy

    doc, ch, _ = local_copy(processed, tmp_path)
    monkeypatch.setattr(orient, "SAMPLE", 1000)
    assert len(orient._cloud(ch)) <= 1000


def test_undo_tells_the_listeners(processed, tmp_path):
    from orient_helpers import local_copy

    doc, ch, _ = local_copy(processed, tmp_path)
    seen = []
    stop = orient.on_transform_changed(lambda c, m: seen.append(m))
    try:
        result = orient.orient(ch, points=ch.to_world(_surface_points(ch)))
        orient.undo(ch, result)
    finally:
        stop()
    assert len(seen) == 2
    assert np.allclose(seen[1] @ seen[0], np.eye(4), atol=1e-12)


def test_photo_positions_are_carried_by_the_motion(processed, tmp_path):
    from orient_helpers import local_copy

    from pyarchinit_photogrammetry.core.camera import Reference

    doc, ch, _ = local_copy(processed, tmp_path)
    _referenced_photos(ch)
    gps = ch.cameras[-1]  # a position in a real system (the GPS of the EXIF): not in the local frame, left as it is
    gps.reference = Reference(location=[14.25, 41.1, 120.0], accuracy=[5.0, 5.0, 10.0])
    gps.meta["reference_crs"] = "EPSG:4326"
    cams = [c for c in ch.cameras if c.reference.location and c is not gps]
    old = {c.key: np.array(c.reference.location, float) for c in cams}
    old_accuracy = {c.key: list(c.reference.accuracy) for c in cams}
    result = orient.orient(ch, points=ch.to_world(_surface_points(ch)))
    m = np.asarray(result["M"], float)
    assert result["moved"]["camera_references"] == len(cams)
    for cam in cams:
        assert np.allclose(cam.reference.location, geo.apply(m, old[cam.key][None])[0], atol=1e-9)
    assert gps.reference.location == [14.25, 41.1, 120.0]
    gps.reference = Reference()  # the fit below in the local system alone
    oriented = np.array(ch.transform, float)
    for mk in _orientation_markers(ch):  # the markers lose their positions: the photos place the chunk again
        mk.position = None
        mk.projections = {}
    report = ch.update_transform(use_cameras=True)
    assert "cameras" in report["method"]
    assert np.abs(np.asarray(ch.transform) - oriented).max() < 1e-6
    orient.put_back(result["state"])
    for cam in cams:
        assert np.allclose(cam.reference.location, old[cam.key], atol=0)
        assert cam.reference.accuracy == old_accuracy[cam.key]


def test_control_points_typed_after_an_orientation_replace_it(processed, tmp_path):
    """The total station's points arrive after a hand orientation: four control markers in the site grid (turned by
    0.5 rad and shifted by kilometres) place the chunk; the orientation markers step aside (not in the fit, listed as
    ``orientation``, no error in another frame) and a new hand orientation stays refused."""
    from pyarchinit_photogrammetry.core.camera import Reference
    from pyarchinit_photogrammetry.markers import gcp

    doc, ch, t0, internal, points, result = _oriented(processed, tmp_path)
    grid = motion_of(rotation_of([0, 0, 1], np.degrees(0.5)), (1000.0, 2000.0, 50.0))
    field = [m for m in ch.markers if not m.orientation and m.position is not None][:4]
    assert len(field) == 4
    for m in field:
        m.reference = Reference(location=geo.apply(grid, ch.to_world(m.position[None]))[0].tolist(),
                                accuracy=[0.005] * 3)
    want = grid @ np.asarray(ch.transform, float)
    report = ch.update_transform()
    assert report["control_rmse"] < 0.005, report["control_rmse"]
    assert "+orientation" not in report["method"]
    assert np.abs(np.asarray(ch.transform) - want).max() < 1e-3
    rows = {e["label"]: e for e in report["markers"]}
    for m in _orientation_markers(ch):
        assert rows[m.label]["type"] == "orientation" and rows[m.label]["error"] is None
    assert sorted(report["orientation_not_used"]) == sorted(m.label for m in _orientation_markers(ch))
    assert gcp.orientation_set_aside(ch)
    with pytest.raises(orient.OrientRefused):
        orient.check(ch)
    # the optimisation holds the block on the field points only, never on the hand frame
    from pyarchinit_photogrammetry.engine import sfm

    assert set(sfm._control_markers(ch)) == {m.key for m in field}
    assert {m.key for m in gcp.control_markers(ch)} == {m.key for m in field}
    # the report's table names the type in both languages
    from pyarchinit_photogrammetry.io import report_content as rc

    for lang in ("it", "en"):
        text = rc.Words(lang)("type_orientation")
        assert text != "type_orientation" and "orient" in text


def test_orientation_markers_step_aside_in_a_real_system(processed, tmp_path):
    """Oriented on a known point (X, Y and height of A, the taped A-B), then declared in a real system: nothing else
    places it, so the orientation markers keep placing it - A stays on its reference, the system kept; with three
    photo positions that the fit uses in that system, they step aside."""
    from pyarchinit_photogrammetry.markers import gcp

    doc, ch, t0, internal, points, result = _oriented(processed, tmp_path, origin_xy=(2480010.0, 4500020.0),
                                                      quota=100.0,
                                                      distance=float(np.linalg.norm(points_ab(processed, tmp_path))))
    ch.crs = "EPSG:3004"
    assert not gcp.orientation_set_aside(ch)
    report = ch.update_transform()
    assert "+orientation" in report["method"]
    assert ch.crs == "EPSG:3004"
    a = next(m for m in ch.markers if m.label == orient.MARKER_LABELS[0])
    assert np.abs(ch.to_world(a.position[None])[0] - [2480010.0, 4500020.0, 100.0]).max() < 1e-6
    # photo positions in the system, spanning their accuracy: they place it, the hand frame steps aside
    _referenced_photos(ch, n=len(ch.aligned_cameras))
    assert gcp.orientation_set_aside(ch)
    rows = ch.update_transform()["markers"]
    assert all(e["type"] == "orientation" for e in rows if e["label"] in orient.MARKER_LABELS)


def points_ab(processed, tmp_path):
    """The distance A-B of :func:`_oriented`'s points (a scratch copy of the trench)."""
    from orient_helpers import local_copy

    (tmp_path / "ab").mkdir()
    _doc, ch, _ = local_copy(processed, tmp_path / "ab")
    p = ch.to_world(_surface_points(ch))
    return p[1] - p[0]


def test_a_real_system_keeps_the_frame_through_a_new_alignment(processed, tmp_path):
    """Oriented by hand, declared in a real system, no bar: a new alignment and the fit put the frame back."""
    from orient_helpers import new_alignment

    doc, ch, t0, internal, points, result = _oriented(processed, tmp_path, origin_xy=(2480010.0, 4500020.0),
                                                      quota=100.0)
    ch.crs = "EPSG:3004"
    oriented = np.array(ch.transform, float)
    g = new_alignment(ch)
    for m in ch.markers:
        m.position = None
    report = ch.update_transform()
    assert "+orientation" in report["method"] and ch.crs == "EPSG:3004"
    assert np.abs(np.asarray(ch.transform) - oriented @ np.linalg.inv(g)).max() < 1e-6


def _files_under(folder):
    import hashlib
    from pathlib import Path

    out = {}
    for path in sorted(Path(folder).rglob("*")):
        if path.is_file():
            st = path.stat()
            out[str(path.relative_to(folder))] = (st.st_mtime_ns, st.st_size,
                                                  hashlib.sha1(path.read_bytes()).hexdigest())
    return out


def test_a_dry_run_touches_no_file(processed, tmp_path):
    """Sections, the elevations' frame.json and rasters, the proxies: computed, never written (not even put back)."""
    from orient_helpers import drawn, local_copy

    doc, ch, _ = local_copy(processed, tmp_path)
    drawn(ch)
    doc.save()
    folder = doc.path.parent
    before = _files_under(folder)
    assert any(k.endswith("frame.json") for k in before) and any(k.endswith(".ply") for k in before)
    internal = _surface_points(ch)
    dry = orient.orient(ch, points=ch.to_world(internal), distance=2.0, dry_run=True)
    assert _files_under(folder) == before
    # the result is the one of the orientation made
    made = orient.orient(ch, points=ch.to_world(internal), distance=2.0)
    for key in ("M", "describe", "moved", "stale", "kept", "cameras_replaced", "weak_markers", "bar_dropped"):
        assert json.dumps(dry[key], sort_keys=True, default=str) == json.dumps(made[key], sort_keys=True, default=str)
    orient.undo(ch, made)
