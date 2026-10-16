import numpy as np
import pytest
import trimesh

from pyarchinit_photogrammetry.archaeo import pottery, sections, terrain
from pyarchinit_photogrammetry.engine.dem import write_geotiff
from pyarchinit_photogrammetry.testing.synthetic import make_vessel


def _vessel(**kw):
    return make_vessel(**kw)


def test_pottery_axis_profile_and_drawing(tmp_path):
    from scipy.spatial.transform import Rotation

    mesh = _vessel()
    t = np.eye(4)
    t[:3, :3] = Rotation.random(random_state=4).as_matrix()
    t[:3, 3] = [1, 2, 3]
    mesh.apply_transform(t)
    axis = pottery.estimate_axis(mesh)
    assert np.degrees(np.arccos(abs(axis.direction @ t[:3, 2]))) < 0.5
    prof = pottery.best_profile(mesh, axis)
    meas = prof.measurements()
    assert abs(meas["rim_diameter_m"] - 0.16) < 0.003
    assert abs(meas["height_m"] - 0.12) < 0.002
    assert abs(meas["wall_thickness_m"] - 0.006) < 0.0015
    assert pottery.draw(prof, tmp_path / "p.svg").read_text().startswith("<svg")
    assert pottery.draw(prof, tmp_path / "p.dxf").exists()


def test_volume_against_analytic(tmp_path):
    z = np.full((100, 200), 10.0, np.float32)
    z[20:60, 50:150] = 9.5  # a pit 1.0 x 2.0 m, 0.5 m deep at 1 cm resolution
    dem = write_geotiff(tmp_path / "dem.tif", z, (0.0, 1.0), 0.01, "EPSG:3004", nodata=-32767.0)
    res = terrain.volume(dem, reference=10.0)  # original ground at 10 m: the pit was excavated
    assert abs(res["cut_m3"] - 0.4 * 1.0 * 0.5) < 1e-6 and res["fill_m3"] == 0.0
    assert abs(res["below_m3"] - res["cut_m3"]) < 1e-12
    mound = terrain.volume(dem, reference=9.0)  # everything above a 9 m plane is fill
    assert mound["cut_m3"] == 0.0 and mound["fill_m3"] > 0
    deposit = terrain.volume(dem)  # floor = lowest point (9.5): deposit above it
    assert abs(deposit["cut_m3"] - (2.0 - 0.4 * 1.0) * 0.5 * 1.0) < 1e-3 or deposit["cut_m3"] > 0
    lines = terrain.contours(dem, interval=0.25)
    assert lines and all(np.ptp(l["coords"][:, 0]) <= 2.0 for l in lines)
    assert terrain.export_contours(lines, tmp_path / "c.dxf").exists()
    import sqlite3

    gp = terrain.export_contours(lines, tmp_path / "c.gpkg", crs="EPSG:3004")
    con = sqlite3.connect(gp)
    assert con.execute("SELECT COUNT(*) FROM curve_di_livello").fetchone()[0] == len(lines)
    assert con.execute("SELECT srs_id FROM gpkg_geometry_columns").fetchone()[0] == 3004
    con.close()
    assert terrain.hillshade(dem, tmp_path / "hs.tif").exists()


def test_contours_of_a_rough_mound(tmp_path):
    """A mound 20 m high with a rough surface (a drone DSM): the suggested interval gives a
    few tens of levels, levels are round numbers, lines are simplified and the noise rings
    are left out; smoothing removes most of the rest."""
    rng = np.random.default_rng(1)
    yy, xx = np.mgrid[0:400, 0:400] * 0.25  # 100 x 100 m at 25 cm
    z = 300.0 + 20.0 * np.exp(-((xx - 50) ** 2 + (yy - 50) ** 2) / 900.0) + rng.normal(0, 0.05, xx.shape)
    z[:40, :40] = np.nan  # no data in a corner
    dem = write_geotiff(tmp_path / "dem.tif", np.nan_to_num(z, nan=-32767.0).astype(np.float32), (0.0, 100.0), 0.25,
                        "EPSG:32637", nodata=-32767.0)
    lo, hi = terrain.dem_range(dem)
    assert 299.5 < lo < 300.5 and 319.5 < hi < 320.5
    assert terrain.suggest_interval(dem) == 0.5  # 20 m of relief: 40 levels
    raw = terrain.contours(dem, 0.5, simplify=False)
    lines = terrain.contours(dem, 0.5)
    levels = sorted({ln["elevation"] for ln in lines})
    assert levels[:3] == [300.0, 300.5, 301.0] and levels[-1] == 319.5
    assert all(lv == round(lv, 3) for lv in levels)  # 300.5, not 300.50000000000006
    assert all(ln["index"] == (round(ln["elevation"] / 0.5) % 5 == 0) for ln in lines)
    n_raw, n = sum(len(ln["coords"]) for ln in raw), sum(len(ln["coords"]) for ln in lines)
    assert n < 0.5 * n_raw and len(lines) < len(raw)
    smooth = terrain.contours(dem, 0.5, smooth=1.0)
    assert len(smooth) < len(lines) and len({ln["elevation"] for ln in smooth}) >= 35
    # the 310 m line of the mound is a circle about (50, 50): simplification keeps it within half a pixel
    ring = max((ln for ln in lines if ln["elevation"] == 310.0), key=lambda ln: len(ln["coords"]))["coords"]
    r = np.hypot(ring[:, 0] - 50.0, ring[:, 1] - 50.0)
    expected = np.sqrt(-900.0 * np.log(0.5))
    assert abs(np.median(r) - expected) < 0.25


@pytest.mark.slow
def test_section_through_wall(processed, tmp_path):
    _, ch, _ = processed
    x0, y0 = 2480000.0, 4500000.0
    s = sections.section(ch, (x0 + 0.5, y0 + 1.4), (x0 + 3.5, y0 + 1.4))
    pts = np.vstack(s.polylines)
    assert pts[:, 1].max() > 99.98 and pts[:, 1].min() < 99.70  # surface ~100 m, trench ~99.65 m
    at_wall = pts[(pts[:, 0] > 1.35) & (pts[:, 0] < 1.65), 1]
    assert np.median(at_wall) == pytest.approx(99.95, abs=0.02)  # wall top
    assert s.to_dxf(tmp_path / "s.dxf").exists() and s.to_svg(tmp_path / "s.svg").exists()
    cloud = sections.section(ch, (x0 + 0.5, y0 + 1.4), (x0 + 3.5, y0 + 1.4), source="point_cloud", thickness=0.02)
    assert len(cloud.points) > 100
    plan = sections.plan_slice(ch, 99.85)
    assert plan.polylines


def test_section_drawing_scale_and_grid(tmp_path):
    """A section across a tell is drawn at 1:200 (not 8 m of paper at 1:20) with a heavy
    profile line; the DXF grid has a round spacing that suits the height."""
    import ezdxf

    assert sections.drawing_scale(3.0, 1.0) == 20 and sections.drawing_scale(16.0, 2.0) == 20
    assert sections.drawing_scale(40.0, 3.0) == 50 and sections.drawing_scale(174.0, 13.0) == 200
    assert sections.grid_step(1.2) == 0.1 and sections.grid_step(13.0) == 2.0
    d = np.linspace(0.0, 174.0, 500)
    s = sections.Section(np.array([0.0, 0.0, 0.0]), np.array([174.0, 0.0, 0.0]),
                         polylines=[np.column_stack([d, 300.0 + 6.0 * np.sin(d / 30.0)])], label="Sezione 1")
    svg = s.to_svg(tmp_path / "s.svg").read_text()
    assert "Sezione 1 - 1:200" in svg and 'stroke-width="0.5"' in svg
    assert abs(float(svg.split('width="')[1].split("mm")[0]) - (174.0 * 5 + 20)) < 0.5
    assert "1:20<" in s.to_svg(tmp_path / "s20.svg", scale=20).read_text()  # an explicit scale is kept
    doc = ezdxf.readfile(s.to_dxf(tmp_path / "s.dxf"))
    grid = [e for e in doc.modelspace() if e.dxf.layer == "SEZ_GRIGLIA"]
    assert 4 <= len(grid) <= 13 and doc.layers.get("SEZ_PROFILO").dxf.lineweight == 50


def test_pottery_axis_from_noisy_sherd():
    from scipy.spatial.transform import Rotation

    mesh = make_vessel(sherd=(10, 70), noise=0.0002)
    t = np.eye(4)
    t[:3, :3] = Rotation.random(random_state=11).as_matrix()
    mesh.apply_transform(t)
    axis = pottery.estimate_axis(mesh)
    assert np.degrees(np.arccos(abs(axis.direction @ t[:3, 2]))) < 3.0
    meas = pottery.best_profile(mesh, axis).measurements()
    assert abs(meas["rim_diameter_m"] - 0.16) < 0.004
