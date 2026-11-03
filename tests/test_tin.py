"""The TIN of a chunk (engine.tin): a wall kept sharp by its break lines, spot heights that win over
the DEM, exact volumes and contours, the greedy refinement of a DEM, the exports read back, the CLI."""
from __future__ import annotations

import time

import numpy as np
import pytest

from pyarchinit_photogrammetry import _native
from pyarchinit_photogrammetry.core.document import Document
from pyarchinit_photogrammetry.engine import tin as T
from pyarchinit_photogrammetry.engine.dem import NODATA, write_geotiff

X0, Y0 = 500000.0, 4500000.0  # a corner of the site in UTM: the TIN works in local coordinates
CRS = "EPSG:32633"

native = pytest.mark.skipif(not _native.supports("tin"), reason="the native core with the TIN kernels (API 10)")


def _chunk(tmp_path, spots=(), dem=None, res=0.02, origin=(X0, Y0 + 2.0)):
    doc = Document()
    ch = doc.add_chunk("A")
    ch.crs = CRS
    doc.save(tmp_path / "p.ppg")
    if spots:
        ch.meta.setdefault("drawings", {})["quote"] = [{"x": float(x), "y": float(y), "quota_q": float(z),
                                                        "sito_q": "S"} for x, y, z in spots]
    if dem is not None:
        path = write_geotiff(ch.data_dir / "dem.tif", np.where(np.isfinite(dem), dem, NODATA).astype(np.float32),
                             origin, res, CRS, nodata=NODATA)
        ch._set_product("dem", path)
    return doc, ch


def _cells(n_rows, n_cols, res, origin):
    r, c = np.mgrid[0:n_rows, 0:n_cols]
    return origin[0] + (c + 0.5) * res, origin[1] - (r + 0.5) * res


# ------------------------------------------------------------------------------------------- the wall
def wall(x):
    """A wall face: the ground at 0 west of X0, the top at 1 m east of X0 + 0.1, the face between."""
    return np.clip((x - X0) / 0.1, 0, 1)


def _wall_chunk(tmp_path):
    spots = [(X0 + dx, Y0 + dy, float(wall(X0 + dx))) for dx in (-2, -1, 1, 2) for dy in (-2, -1, 0, 1, 2)]
    return _chunk(tmp_path, spots)


WALL_LINES = [{"id": "piede", "kind": "breakline", "coords": [(X0, Y0 - 2, 0.0), (X0, Y0 + 2, 0.0)]},
              {"id": "cresta", "kind": "breakline", "coords": [(X0 + 0.1, Y0 - 2, 1.0), (X0 + 0.1, Y0 + 2, 1.0)]}]


def _raster_error(ch):
    path = T.tin_to_dem(ch, resolution=0.01)
    import rasterio

    with rasterio.open(path) as src:
        z = src.read(1).astype(float)
        z[z == src.nodata] = np.nan
        x, _y = _cells(src.height, src.width, src.res[0], (src.transform.c, src.transform.f))
    ok = np.isfinite(z)
    return np.abs(z[ok] - wall(x[ok]))


def test_break_lines_keep_the_wall_sharp(tmp_path):
    _doc, ch = _wall_chunk(tmp_path)
    smeared = T.build_tin(ch, source="spots")
    assert smeared.info["n_constrained"] == 0
    assert _raster_error(ch).max() > 0.3  # the face spread over a metre of triangles

    tin = T.build_tin(ch, source="spots", breaklines=WALL_LINES)
    assert np.array_equal(tin.origin, [X0, Y0])
    if not _native.supports("tin"):  # scipy cannot hold them: said in the report
        assert T.NOT_CONSTRAINED in tin.info["warnings"]
        return
    assert tin.info["native"] and not tin.info["warnings"]
    # the two constraints are edges of the triangulation
    w = tin.world_xyz
    edges = {tuple(e) for e in tin.constrained.tolist()}
    assert len(edges) == tin.info["n_constrained"] == 2
    for a, b in edges:
        assert abs(w[a, 0] - w[b, 0]) < 1e-9 and abs(w[a, 0] - (X0 if w[a, 2] == 0 else X0 + 0.1)) < 1e-9
    assert _raster_error(ch).max() < 1e-3
    # the TIN as a DEM version, the DEM as it was kept
    assert ch.meta["active_versions"]["dem"]["name"] == "DEM dal TIN"
    assert ch.meta["versions"]["dem"]


def test_crossing_break_lines_meet_at_a_new_vertex(tmp_path):
    _doc, ch = _chunk(tmp_path, [(X0 + x, Y0 + y, 0.0) for x in (-1, 1) for y in (-1, 1)])
    lines = [{"id": 1, "coords": [(X0 - 1, Y0, 0.0), (X0 + 1, Y0, 0.0)]},
             {"id": 2, "coords": [(X0, Y0 - 1, 0.0), (X0, Y0 + 1, 2.0)]}]
    tin = T.build_tin(ch, source="spots", breaklines=lines)
    if not _native.supports("tin"):
        pytest.skip("the crossing needs the native core")
    k = np.nonzero(np.all(np.abs(tin.world_xy - [X0, Y0]) < 1e-6, axis=1))[0]
    assert len(k) == 1 and np.isclose(tin.z[k[0]], 1.0)  # its height along the line it was made on
    assert tin.info["n_constrained"] == 4


# ------------------------------------------------------------------------------------- spot heights
def test_spot_heights_win_over_the_dem(tmp_path):
    res, n = 0.02, 100
    x, y = _cells(n, n, res, (X0, Y0 + 2.0))
    dem = 10 + 0.01 * (x - X0)
    spot = (X0 + 1.013, Y0 + 0.987, 10.5)  # half a metre above the DEM, between cells
    _doc, ch = _chunk(tmp_path, [spot], dem, res)
    tin = T.build_tin(ch, source="dem", max_error=0.002)
    assert tin.info["error_max"] <= 0.002 + 1e-5
    k = np.nonzero(np.all(np.abs(tin.world_xy - spot[:2]) < 1e-6, axis=1))[0]
    assert len(k) == 1 and tin.z[k[0]] == 10.5 and list(tin.spots) == [k[0]]
    near = np.hypot(*(tin.world_xy - spot[:2]).T) < 0.015
    assert near.sum() == 1  # no DEM cell right next to it
    without = T.build_tin(ch, source="dem", max_error=0.002, spots=False)
    assert without.info["n_spots"] == 0 and not np.any(np.isclose(without.z, 10.5))


# ------------------------------------------------------------------------------- volume and contours
def test_the_volume_of_a_pyramid_is_exact(tmp_path):
    corners = [(X0 + dx, Y0 + dy, 0.0) for dx in (-2, 2) for dy in (-2, 2)]
    _doc, ch = _chunk(tmp_path, corners + [(X0, Y0, 3.0)])
    T.build_tin(ch, source="spots")
    v = T.tin_volume(ch)  # the floor at the lowest vertex
    assert v["cut_m3"] == pytest.approx(16 * 3 / 3, rel=1e-12) and v["fill_m3"] == 0
    assert v["area_m2"] == pytest.approx(16, rel=1e-12)
    # a ground at 1 m before: the tip above it was added, the space between the faces and the
    # plane below it removed - as large as the tip, since the pyramid holds 16 m3 and the slab 16
    before = T.tin_volume(ch, reference=1.0)
    tip = 16 * 3 / 3 * (2 / 3) ** 3
    assert before["fill_m3"] == pytest.approx(tip, rel=1e-12)
    assert before["cut_m3"] == pytest.approx(tip, rel=1e-12) and before["net_m3"] == pytest.approx(0, abs=1e-12)
    plane = T.tin_volume(ch, reference=(0.0, 0.0, 1.0))  # the same as a plane in world coordinates
    assert plane["fill_m3"] == pytest.approx(before["fill_m3"], rel=1e-12)
    assert set(v) == {"area_m2", "cut_m3", "fill_m3", "net_m3", "above_m3", "below_m3", "quota_min", "quota_max"}


def test_contours_of_a_plane_are_straight(tmp_path, rng):
    xy = rng.uniform(-5, 5, (60, 2))
    z = 100 + 0.3 * xy[:, 0] + 0.1 * xy[:, 1]
    _doc, ch = _chunk(tmp_path, [(X0 + a, Y0 + b, c) for (a, b), c in zip(xy, z)])
    T.build_tin(ch, source="spots")
    lines = T.tin_contours(ch, interval=0.5)
    assert lines and all(c["elevation"] % 0.5 == 0 for c in lines)
    assert len({c["elevation"] for c in lines}) == len(lines)  # one line per level
    for c in lines:
        p = c["coords"] - [X0, Y0]
        assert np.allclose(100 + 0.3 * p[:, 0] + 0.1 * p[:, 1], c["elevation"], atol=1e-9)
        d, v = p - p[0], p[-1] - p[0]
        assert np.abs(d[:, 0] * v[1] - d[:, 1] * v[0]).max() < 1e-8  # every vertex on one straight line


def test_contours_through_vertices_on_a_level(tmp_path):
    """Heights in whole metres, levels every metre: vertices lie exactly on the levels, the lines
    still join and follow the triangles."""
    pts = [(X0 + x, Y0 + y, float(x + y)) for x in range(5) for y in range(5)]
    _doc, ch = _chunk(tmp_path, pts)
    tin = T.build_tin(ch, source="spots")
    lines = T.contours(tin, 1.0)
    for c in lines:
        p = c["coords"] - [X0, Y0]
        assert np.allclose(p.sum(axis=1), c["elevation"], atol=1e-12)
    levels = sorted({c["elevation"] for c in lines})
    assert levels[0] >= 0 and levels[-1] <= 8
    inner = [c for c in lines if 0 < c["elevation"] < 8]
    assert inner and all(len([d for d in lines if d["elevation"] == c["elevation"]]) == 1 for c in inner)
    with pytest.raises(ValueError, match="index_every"):
        T.contours(tin, 1.0, index_every=0)
    with pytest.raises(ValueError, match="interval"):
        T.contours(tin, 0.0)


def test_break_lines_must_be_finite(tmp_path):
    _doc, ch = _wall_chunk(tmp_path)
    bad = [{"id": "x", "coords": [(X0, Y0, 0.0), (float("nan"), Y0 + 1, 0.0)]}]
    with pytest.raises(ValueError, match="not finite"):
        T.build_tin(ch, source="spots", breaklines=bad)
    with pytest.raises(ValueError, match="kind"):
        T.build_tin(ch, source="spots", breaklines=[{"kind": "hard", "coords": WALL_LINES[0]["coords"]}])


# ------------------------------------------------------------------------------- the DEM's refinement
def _ring_dem(n, res, noise=0.002):
    r, c = np.mgrid[0:n, 0:n]
    x, y = c * res, r * res
    dem = 50 + 0.5 * np.sin(x / 7) * np.cos(y / 5) + noise * np.random.default_rng(0).standard_normal((n, n))
    rr = np.hypot(r - n / 2, c - n / 2)
    dem[(rr < n * 0.3) | (rr > n * 0.5)] = np.nan  # a ring survey: empty in the middle and at the corners
    return dem


@pytest.mark.parametrize("core", ["native", "numpy"])
def test_a_ring_survey_with_and_without_the_core(tmp_path, monkeypatch, core):
    if core == "native" and not _native.supports("tin"):
        pytest.skip("the native core with the TIN kernels (API 10)")
    monkeypatch.setenv("PPG_NATIVE", "1" if core == "native" else "0")
    res, n = 0.05, 400
    dem = _ring_dem(n, res, noise=0.01)
    _doc, ch = _chunk(tmp_path, dem=dem, res=res, origin=(X0, Y0 + n * res))
    tin = T.build_tin(ch, source="dem")
    # the noise is measured on the ring, not in the empty middle (1 cm: above the floor of 1 cm)
    assert tin.info["tolerance"] == "noise" and 0.01 < tin.info["max_error"] < 0.04
    assert not tin.info["warnings"] and tin.info["error_max"] <= tin.info["max_error"] + 1e-5
    T.tin_to_dem(ch)
    import rasterio

    with rasterio.open(ch.product_path("dem")) as src:
        out = src.read(1)
        empty = out == src.nodata
    assert empty[np.isnan(dem)].all()  # nothing over the hole nor outside the ring
    assert empty[np.isfinite(dem)].mean() < 0.001


def test_the_fallback_tolerance_is_said(tmp_path):
    flat = np.full((60, 60), 3.0)  # no noise to measure
    _doc, ch = _chunk(tmp_path, dem=flat, res=0.1, origin=(X0, Y0 + 6.0))
    tin = T.build_tin(ch, source="dem")
    assert tin.info["tolerance"] == "floor" and tin.info["max_error"] == pytest.approx(0.02)
    assert any("could not be measured" in w for w in tin.info["warnings"])
    assert "0.2 of a cell of the DEM" in T.report(tin)


def test_the_default_tolerance_has_a_floor_of_a_fifth_of_a_cell(tmp_path):
    """A quiet DEM at 5 cm with 1 mm of noise: twice the noise would be 2 mm, the vertices would
    chase the noise; the tolerance is a fifth of a cell, 1 cm, with far fewer vertices. A given
    max_error is never changed."""
    res, n = 0.05, 200
    x, y = _cells(n, n, res, (X0, Y0 + n * res))
    dem = 20 + 0.3 * np.sin((x - X0) / 3) * np.cos((y - Y0) / 2)
    dem += 0.001 * np.random.default_rng(3).standard_normal(dem.shape)
    _doc, ch = _chunk(tmp_path, dem=dem, res=res, origin=(X0, Y0 + n * res))
    floor = T.build_tin(ch, source="dem")
    assert floor.info["tolerance"] == "floor" and floor.info["max_error"] == pytest.approx(0.01)
    assert 0 < floor.info["noise"] < 0.005 and not floor.info["warnings"]
    tight = T.build_tin(ch, source="dem", max_error=0.002)
    assert tight.info["tolerance"] == "given" and tight.info["max_error"] == 0.002
    assert floor.n_vertices < tight.n_vertices / 3

def test_refinement_reaches_the_tolerance_with_few_vertices(tmp_path):
    res, n = 0.02, 300
    x, y = _cells(n, n, res, (X0, Y0 + 6.0))
    dem = 50 + 0.8 * np.exp(-((x - X0 - 3) ** 2 + (y - Y0 - 3) ** 2) / 2.0) + 0.05 * (x - X0)
    dem[:40, :60] = np.nan  # a hole in a corner: no triangle over it
    _doc, ch = _chunk(tmp_path, dem=dem, res=res, origin=(X0, Y0 + 6.0))
    tin = T.build_tin(ch, source="dem", max_error=0.005)
    cells = int(np.isfinite(dem).sum())
    assert tin.n_vertices < cells / 10
    assert tin.info["error_max"] <= 0.005 + 1e-5 and not tin.info["capped"]
    T.tin_to_dem(ch)  # on the grid of the DEM: the hole stays empty
    import rasterio

    with rasterio.open(ch.product_path("dem")) as src:
        out = src.read(1)
        assert (out[:38, :58] == src.nodata).all()
        assert (out[50:, 70:] != src.nodata).all()
    # the default tolerance: twice the noise of the DEM, at least a fifth of a cell
    default = T.build_tin(ch, source="dem")
    assert default.info["max_error"] >= T.FLOOR_CELLS * res


def test_ground_of_the_dense_cloud(tmp_path, rng):
    from pyarchinit_photogrammetry.core.pointcloud import PointCloud

    _doc, ch = _chunk(tmp_path, [(X0 + 1.0, Y0 + 1.0, 7.0)])
    n = 40_000
    xy = rng.uniform(0, 4, (n, 2))
    ground = 5 + 0.1 * xy[:, 0]
    cls = np.where(rng.random(n) < 0.8, 2, 5).astype(np.uint8)  # a fifth is vegetation, 30 cm up
    z = ground + np.where(cls == 5, 0.3, 0.0)
    xy[200:400] = rng.uniform(2.5, 3.5, (200, 2))  # a gap in the middle stays a gap
    keep = ~((np.abs(xy[:, 0] - 3) < 0.6) & (np.abs(xy[:, 1] - 3) < 0.6))
    world = np.column_stack([xy + [X0, Y0], z])[keep]
    path = PointCloud(world, classification=cls[keep]).save_ply(ch.data_dir / "point_cloud.ply")
    ch._set_product("point_cloud", path)
    tin = T.build_tin(ch, source="cloud")
    assert 0.8 * keep.sum() * 0.5 < tin.n_vertices < keep.sum()
    w = tin.world_xyz
    assert np.abs(w[:, 2] - (5 + 0.1 * (w[:, 0] - X0)))[np.arange(len(w)) != tin.spots[0]].max() < 1e-6
    assert w[tin.spots[0], 2] == 7.0
    centre = T.rasterize(tin.xy, tin.z, tin.triangles, 2.9 + X0 - tin.origin[0], 3.1 + Y0 - tin.origin[1], 0.2, 1, 1)
    assert np.isnan(centre).all()  # no triangle across the gap
    assert tin.info["spacing"] > 0


# ----------------------------------------------------------------------------------------- exports
def test_exports_read_back(tmp_path):
    _doc, ch = _wall_chunk(tmp_path)
    tin = T.build_tin(ch, source="spots", breaklines=WALL_LINES)
    w = tin.world_xyz
    # DXF: a 3DFACE per triangle, the break lines on their layer, the spot heights as points
    import ezdxf

    doc = ezdxf.readfile(T.export(tin, tmp_path / "tin.dxf", CRS))
    msp = doc.modelspace()
    faces = msp.query("3DFACE")
    assert len(faces) == tin.n_triangles
    assert all(f.dxf.layer == "TIN" for f in faces)
    first = np.array([faces[0].dxf.vtx0, faces[0].dxf.vtx1, faces[0].dxf.vtx2])
    assert np.allclose(first, w[tin.triangles[0]], atol=1e-9)
    assert len(msp.query("POINT[layer=='QUOTE']")) == len(tin.spots) == 20
    if len(tin.constrained):
        assert len(msp.query("POLYLINE[layer=='LINEE_DI_ROTTURA']")) >= 2
    # LandXML
    pts, faces = T.read_landxml(T.export(tin, tmp_path / "tin.xml", CRS))
    assert np.allclose(pts, w, atol=1e-4) and np.array_equal(faces, tin.triangles)
    text = (tmp_path / "tin.xml").read_text()
    assert 'surfType="TIN"' in text and 'epsgCode="32633"' in text
    # GeoPackage: triangles with z, the spot heights
    from pyarchinit_photogrammetry.io import gpkg

    path = T.export(tin, tmp_path / "tin.gpkg", CRS)
    tris = gpkg.read_table(path, "tin_triangoli")
    assert len(tris) == tin.n_triangles and tris[0][0].has_z
    import sqlite3

    with sqlite3.connect(path) as con:
        zflags = dict(con.execute("SELECT table_name, z FROM gpkg_geometry_columns").fetchall())
    assert zflags == {"tin_triangoli": 1, "tin_spigoli": 1, "tin_quote": 1}
    quote = gpkg.read_table(path, "tin_quote")
    assert sorted(round(g.z, 6) for g, _ in quote) == sorted(round(float(v), 6) for v in tin.z[tin.spots])
    assert len(gpkg.read_table(path, "tin_spigoli")) == (2 if len(tin.constrained) else 0)
    # meshes
    from pyarchinit_photogrammetry.io.mesh_ply import read_mesh

    mesh = read_mesh(T.export(tin, tmp_path / "tin.ply"))
    assert np.allclose(mesh.xyz, w) and len(mesh.faces) == tin.n_triangles
    obj = T.export(tin, tmp_path / "tin.obj").read_text().splitlines()
    assert sum(r.startswith("v ") for r in obj) == tin.n_vertices
    with pytest.raises(ValueError):
        T.export(tin, tmp_path / "tin.txt")
    # kept with the chunk and read again
    again = T.chunk_tin(ch)
    assert np.array_equal(again.triangles, tin.triangles) and np.array_equal(again.xy, tin.xy)
    assert again.info["n_triangles"] == tin.n_triangles


def test_command_line(tmp_path):
    import json

    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    doc, _ch = _wall_chunk(tmp_path)
    doc.save()
    lines = tmp_path / "linee.geojson"
    lines.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"kind": ln["kind"]},
         "geometry": {"type": "LineString", "coordinates": ln["coords"]}} for ln in WALL_LINES]}))
    project = str(tmp_path / "p.ppg")
    run = CliRunner().invoke
    r = run(app, ["tin", "build", project, "--source", "spots", "--breaklines", str(lines)])
    assert r.exit_code == 0, r.output
    assert "TIN (spots)" in r.output
    r = run(app, ["tin", "volume", project, "--reference", "0"])
    assert r.exit_code == 0 and "fill" in r.output, r.output
    r = run(app, ["tin", "contours", project, str(tmp_path / "c.gpkg"), "--interval", "0.25"])
    assert r.exit_code == 0 and (tmp_path / "c.gpkg").exists(), r.output
    r = run(app, ["tin", "export", project, str(tmp_path / "t.xml")])
    assert r.exit_code == 0 and (tmp_path / "t.xml").exists(), r.output
    r = run(app, ["tin", "dem", project, "--resolution", "0.05"])
    assert r.exit_code == 0, r.output
    assert Document.open(tmp_path / "p.ppg").chunk.meta["dem"]["source"] == "tin"


# ----------------------------------------------------------------------------------------- speed
@native
def test_a_million_points_in_less_than_ten_seconds(rng):
    xy = rng.uniform(0, 1000, (1_000_000, 2))
    z = rng.uniform(0, 5, len(xy))
    t0 = time.perf_counter()
    t = T.triangulate(xy)
    grid = T.rasterize(xy, z, t.triangles, 0, 1000, 0.5, 2000, 2000)
    segs, _ = T.contour_segments(xy, z, t.triangles, np.arange(0.5, 5, 0.5))
    T.prism_volume(xy, z, t.triangles, 0.0)
    elapsed = time.perf_counter() - t0
    assert len(t.triangles) > 1_990_000 and np.isfinite(grid).mean() > 0.99 and len(segs)
    assert elapsed < 10, elapsed
